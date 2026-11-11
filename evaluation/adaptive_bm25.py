"""Офлайн-сравнение адаптивной доли BM25 с двумя фиксированными гибридами."""

import argparse
import json
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from statistics import fmean

from app.recommender import recommend
from app.recommender.engine import ADAPTIVE_BM25_WEIGHT
from app.recommender.hybrid import B, DEFAULT_BM25_WEIGHT, FIELD_WEIGHTS, K1
from evaluation.catalog_quality import catalog_fingerprint
from evaluation.runner import (
    EVALUATION_DIR, FIXTURES_DIR, TOP_K, _ranking_scores, build_cases, load_evaluation_inputs, load_real_catalog,
)
from evaluation.tune_deep import _compare
from evaluation.tune_weights import _specialized

MODES = {
    "fixed_25": (DEFAULT_BM25_WEIGHT, FIELD_WEIGHTS, B, K1, False),
    "fixed_749_tuned": (0.749, (3, 0.01, 2.5), 0.1, 1.2, False),
    "adaptive": (DEFAULT_BM25_WEIGHT, FIELD_WEIGHTS, B, K1, True),
}
LABELS = {"fixed_25": "BM25 25%", "fixed_749_tuned": "BM25 74,9% (подобранные поля)",
          "adaptive": "Адаптивный BM25"}
METRICS = ("hit_at_3", "mrr_at_3", "precision_at_3", "ndcg_at_3", "map_at_3")


def _stress(backup, catalog_path, mode):
    weight, fields, b, k1, adaptive = MODES[mode]
    return _specialized(backup, catalog_path, weight, fields, b, k1, adaptive=adaptive)


def _rank(catalog, cases, directory, mode):
    weight, fields, b, k1, adaptive = MODES[mode]
    rank = partial(recommend, explain_with_llm=False, balance_access=True, links_directory=directory,
                   bm25_weight=weight, bm25_field_weights=fields, bm25_length_penalty=b,
                   bm25_saturation=k1, bm25_adaptive=adaptive)
    results = {}
    for case in cases:
        ids = tuple(item.material_id for item in rank(catalog, case.profile, case.history, TOP_K).items)
        rr, precision, ndcg, ap = _ranking_scores(ids, set(case.relevant_ids))
        results[case.case_id] = dict(zip(METRICS, (int(rr > 0), rr, precision, ndcg, ap), strict=True)) | {
            "ids": ids,
        }
    return results


def _summarize(ids, current, results):
    scores = [results[case_id] for case_id in ids]
    return {"cases": len(scores), "hits": sum(row["hit_at_3"] for row in scores),
            **{metric: fmean(row[metric] for row in scores) for metric in METRICS},
            "changed_top3": sum(results[case_id]["ids"] != current[case_id]["ids"] for case_id in ids)}


def build_report(backup, catalog_path, cases_path):
    catalog = load_real_catalog(catalog_path)
    raw = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = build_cases(raw, catalog)
    if not cases or len({case.case_id for case in cases}) != len(cases) or any(not case.relevant_ids for case in cases):
        raise ValueError("Для сравнения нужны сценарии с уникальными ID и размеченными ответами.")
    groups = {row["id"]: row["group"] for row in raw}
    demo_catalog, demo_cases = load_evaluation_inputs("demo")

    with ProcessPoolExecutor(max_workers=len(MODES)) as pool:
        specialized = {mode: pool.submit(_stress, backup, catalog_path, mode) for mode in MODES}
        fresh = {mode: _rank(catalog, cases, catalog_path.parent, mode) for mode in MODES}
        synthetic = {mode: _rank(demo_catalog, tuple(case for case in demo_cases if case.relevant_ids),
                                 FIXTURES_DIR, mode) for mode in MODES}
        rows = {}
        for mode, future in specialized.items():
            current = fresh["fixed_25"]
            rows[mode] = {
                "settings": MODES[mode],
                "new": _summarize(groups, current, fresh[mode]),
                "groups": {group: _summarize([key for key in groups if groups[key] == group], current, fresh[mode])
                           for group in dict.fromkeys(groups.values())},
                "synthetic": _summarize(synthetic[mode], synthetic["fixed_25"], synthetic[mode]),
                "specialized": future.result(),
            }
            print(f"Готово · {LABELS[mode]}", flush=True)
    for row in rows.values():
        _compare(row, rows["fixed_25"])
    return {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "catalog_fingerprint": catalog_fingerprint(catalog), "adaptive_high_weight": ADAPTIVE_BM25_WEIGHT,
            "groups": dict(Counter(groups.values())), "modes": rows,
            "cases": [{"id": case.case_id, "group": groups[case.case_id], "goal": case.profile.goal,
                       "relevant_ids": case.relevant_ids, "note": case.note,
                       "scores": {mode: results[case.case_id] for mode, results in fresh.items()}} for case in cases]}


def markdown(report):
    lines = ["# Адаптивный вес BM25", "", "## Новая разметка", "",
             "| Группа | Алгоритм | Попадания | Hit@3 | MRR@3 | nDCG@3 | Изменённых топ-3 к 25% |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    rows = report["modes"]
    for group in (None, *report["groups"]):
        for mode, row in rows.items():
            scores = row["new"] if group is None else row["groups"][group]
            lines.append(f"| {group or 'Все'} | {LABELS[mode]} | {scores['hits']}/{scores['cases']} | "
                         f"{scores['hit_at_3']:.2%} | {scores['mrr_at_3']:.4f} | {scores['ndcg_at_3']:.4f} | "
                         f"{scores['changed_top3']} |")
    lines.extend(["", "## Сложные сценарии", "",
                  "| Набор | Метрика | BM25 25% | BM25 74,9% | Адаптивный BM25 |",
                  "| --- | --- | ---: | ---: | ---: |"])
    for suite_group in zip(*(row["specialized"]["suites"] for row in rows.values()), strict=True):
        for metric in suite_group[0]["metrics"]:
            values = [suite["metrics"][metric] for suite in suite_group]
            cells = " | ".join("—" if value is None else f"{value:.2%}" for value in values)
            lines.append(f"| {suite_group[0]['title']} | {metric} | {cells} |")
    for metric in rows["fixed_25"]["specialized"]["graph"]:
        values = " | ".join(f"{row['specialized']['graph'][metric]:.2%}" for row in rows.values())
        lines.append(f"| Экспертный граф | {metric} | {values} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=EVALUATION_DIR / "profiles.adaptive.json")
    parser.add_argument("--output", type=Path, required=True, help="Путь к отчёту JSON")
    args = parser.parse_args()
    report = build_report(args.backup, args.catalog, args.cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
