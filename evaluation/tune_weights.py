"""Офлайн-подбор долей BM25 и recommend на размеченных сценариях."""

import json
import sys
from argparse import ArgumentParser
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from math import isfinite
from pathlib import Path
from statistics import fmean

from app.catalog import catalog_source
from app.recommender import RANKING_MODES, recommend
from app.recommender.hybrid import B, DEFAULT_BM25_WEIGHT, FIELD_WEIGHTS, K1
from evaluation.catalog_quality import build_quality_report, catalog_fingerprint
from evaluation.runner import FIXTURES_DIR, TOP_K, _ranking_scores, load_evaluation_inputs, load_real_catalog

METRICS = ("hit_at_3", "mrr_at_3", "precision_at_3", "ndcg_at_3", "map_at_3")
QUALITY_LIMITS = (
    ("Холодный старт", "Критерий выполнен ↑", True),
    ("Смена контекста", "Hit@3", True),
    ("Учёт негатива", "Кейсы с нежелательным ↓", False),
    ("Редкие форматы и отрасли", "Hit@3", True),
    ("Педагогическая прогрессия", "2+ базовых/средних ↑", True),
    ("От теории к инструменту", "Hit@3", True),
    ("Устойчивость к случайным кликам", "Ядро среди исходно релевантных", True),
    ("Устойчивость к случайным кликам", "Переход после 2–3 ↑", True),
    ("Семантические дубли", "Списки с дублем ↓", False),
)
FIELD_CANDIDATES = (
    (1, 1, 1), (2, 1, 1), (4, 1, 1), (6, 1, 1), (3, .5, 1), (3, 2, 1),
    (3, 1, .5), (3, 1, 2), (3, 2, 2), (1, 2, 1), (1, 1, 2), (3, 0, 1), (3, 1, 0),
)
LENGTH_CANDIDATES = (0.0, .25, .5, .6, .9, 1.0)
SATURATION_CANDIDATES = (0.0, .4, .8, 1.6, 2.0, 3.0)


def split_by_topic(cases, fraction, seed):
    topics = defaultdict(list)
    for case in cases:
        topics[case.case_id.split("-", 1)[0]].append(case)
    if len(topics) < 2:
        raise ValueError("Для разделения нужны хотя бы две группы тем.")
    order = sorted(topics, key=lambda topic: sha256(f"{seed}:{topic}".encode()).digest())
    target = round(len(cases) * fraction)
    validation_topics = set()
    count = 0
    for topic in order[:-1]:
        validation_topics.add(topic)
        count += len(topics[topic])
        if count >= target:
            break
    tuning = tuple(case for case in cases if case.case_id.split("-", 1)[0] not in validation_topics)
    validation = tuple(case for case in cases if case.case_id.split("-", 1)[0] in validation_topics)
    if {mid for case in tuning for mid in case.relevant_ids} & {mid for case in validation for mid in case.relevant_ids}:
        raise ValueError("Разделение содержит одинаковые размеченные материалы. Выберите другой seed или объедините связанные темы.")
    return tuning, validation, sorted(topics.keys() - validation_topics), sorted(validation_topics)


def measure(weight, catalog, cases, links_directory, field_weights=FIELD_WEIGHTS, length_penalty=B, saturation=K1):
    rank = partial(recommend, explain_with_llm=False, balance_access=True, links_directory=links_directory,
                   bm25_weight=weight, bm25_field_weights=field_weights, bm25_length_penalty=length_penalty,
                   bm25_saturation=saturation)
    scores = []
    for case in cases:
        if case.relevant_ids:
            ids = tuple(item.material_id for item in rank(catalog, case.profile, case.history, limit=TOP_K).items)
            scores.append(_ranking_scores(ids, set(case.relevant_ids)))
    if not scores:
        raise ValueError("В наборе нет сценариев с размеченным ответом.")
    hits = sum(row[0] > 0 for row in scores)
    return {
        "cases": len(cases), "eligible": len(scores), "hits": hits, "hit_at_3": hits / len(scores),
        **dict(zip(METRICS[1:], map(fmean, zip(*scores)), strict=True)),
    }


def _grid_row(configuration, *, catalog, tuning, source, demo_catalog, demo, backup):
    weight, field_weights, length_penalty, saturation = configuration
    row = {
        "bm25_weight": weight,
        "recommend_weight": round(1 - weight, 10),
        "field_weights": field_weights,
        "length_penalty": length_penalty,
        "saturation": saturation,
        "tuning": measure(weight, catalog, tuning, source.parent, field_weights, length_penalty, saturation),
        "synthetic": measure(weight, demo_catalog, demo, FIXTURES_DIR, field_weights, length_penalty, saturation),
    }
    if backup:
        row["specialized"] = _specialized(backup, source, weight, field_weights, length_penalty, saturation)
    return row


def _grid(weights, jobs, evaluate):
    def collect(results):
        rows = []
        for row in results:
            rows.append(row)
            print(f"Сетка {len(rows)}/{len(weights)} · {_label(row)} · "
                  f"nDCG@3 {row['tuning']['ndcg_at_3']:.4f}", file=sys.stderr, flush=True)
        return rows

    if jobs == 1:
        return collect(map(evaluate, weights))
    with ProcessPoolExecutor(max_workers=min(jobs, len(weights))) as pool:
        return collect(pool.map(evaluate, weights))


def _choice(row, reference, metric="ndcg_at_3"):
    scores = row["tuning"]
    return (scores[metric], scores["ndcg_at_3"], scores["mrr_at_3"], scores["hit_at_3"],
            -abs(row["bm25_weight"] - reference), row["field_weights"] == FIELD_WEIGHTS,
            -abs(row["length_penalty"] - B), -abs(row["saturation"] - K1), -row["bm25_weight"])


def _combine(first, second):
    eligible = first["eligible"] + second["eligible"]
    return {
        "cases": first["cases"] + second["cases"], "eligible": eligible, "hits": first["hits"] + second["hits"],
        **{metric: (first[metric] * first["eligible"] + second[metric] * second["eligible"]) / eligible
           for metric in METRICS},
    }


def _specialized(backup, source, weight, field_weights, length_penalty, saturation, *, adaptive=False):
    from evaluation import learning, stress

    method = RANKING_MODES["hybrid"]

    def metrics(row, labels):
        values = next(item["values"] for item in row["methods"] if item["label"] == method)
        return dict(zip(labels, values, strict=True))

    settings = {"bm25_weight": weight, "bm25_field_weights": field_weights,
                "bm25_length_penalty": length_penalty, "bm25_saturation": saturation, "bm25_adaptive": adaptive}
    behavioral = stress.build_report(backup, source, **settings)
    learning_report = learning.build_report(backup, source, **settings)
    return {
        "suites": [
            {"title": suite["title"], "cases": suite["overall"]["count"],
             "metrics": metrics(suite["overall"], suite["columns"])}
            for report in (behavioral, learning_report) for suite in report["suites"]
        ],
        "graph": metrics(behavioral["graph"], ("Hit@3", "MRR@3", "Первое место в ловушке")),
    }


def _catalog_quality(backup, source, row):
    quality = build_quality_report(backup, source, 300, bm25_weight=row["bm25_weight"],
                                   bm25_field_weights=row["field_weights"], bm25_length_penalty=row["length_penalty"],
                                   bm25_saturation=row["saturation"])
    return {
        "catalog": {key: quality["catalog"][key] for key in ("size", "coverage", "gini", "type_diversity")},
        "history": quality["history"],
        "access": [{key: row[key] for key in ("mode", "any_restricted")} for row in quality["synthetic_paywall"]["rows"]],
    }


def quality_regressions(row, reference, limits=QUALITY_LIMITS):
    values = {suite["title"]: suite["metrics"] for suite in row["specialized"]["suites"]}
    current = {suite["title"]: suite["metrics"] for suite in reference["specialized"]["suites"]}
    regressions = []
    for suite, metric, higher_is_better in limits:
        before, after = current[suite][metric], values[suite][metric]
        if before is None:
            continue
        if after is None or (after < before if higher_is_better else after > before):
            regressions.append({"suite": suite, "metric": metric, "reference": before, "observed": after})
    return regressions


def _fields_label(fields):
    return ":".join(f"{weight:g}" for weight in fields)


def _label(row):
    return (f"BM25 {100 * row['bm25_weight']:g}% · {_fields_label(row['field_weights'])} · "
            f"b={row['length_penalty']:g} · k1={row['saturation']:g}")


def _parse_fields(value):
    try:
        weights = tuple(float(part) for part in value.split(","))
    except ValueError:
        return None
    if len(weights) != 3 or not all(isfinite(weight) and weight >= 0 for weight in weights) or not any(weights):
        return None
    return weights


def _reuse_outer(path, *, fingerprint, seed, reference, weights, train_topics, test_topics, specialized):
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = report["grid"]
    if (report["catalog_fingerprint"] != fingerprint or report["seed"] != seed
            or report["reference_bm25_weight"] != reference or report["tuning_topics"] != train_topics
            or report["validation_topics"] != test_topics or [row["bm25_weight"] for row in rows] != weights
            or any(tuple(row.get("field_weights", FIELD_WEIGHTS)) != FIELD_WEIGHTS
                   or row.get("length_penalty", B) != B
                   or row.get("saturation", K1) != K1
                   or ("specialized" in row) != specialized for row in rows)):
        raise ValueError("Отчёт по долям BM25 не совпадает с параметрами нового прогона.")
    for row in rows:
        row["field_weights"] = FIELD_WEIGHTS
        row["length_penalty"] = B
        row["saturation"] = K1
    return rows


def _table(rows, key):
    print("| BM25 | recommend | Заголовок / описание / теги | b | k1 | Сценариев | Hit@3 | MRR@3 | Precision@3 | nDCG@3 | MAP@3 |")
    print("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in rows:
        scores = row[key]
        numbers = " | ".join(f"{scores[m]:.4f}" for m in METRICS[1:])
        print(f"| {100 * row['bm25_weight']:g}% | {100 * row['recommend_weight']:g}% | "
              f"{_fields_label(row['field_weights'])} | {row['length_penalty']:g} | {row['saturation']:g} | {scores['eligible']} | "
              f"{scores['hit_at_3']:.2%} | {numbers} |")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, help="Экспорт реального каталога SQLite или JSON")
    parser.add_argument("--weights", type=float, nargs="+", default=[i / 20 for i in range(21)],
                        help="Доли BM25 от 0 до 1. По умолчанию шаг 0.05")
    parser.add_argument("--reference-weight", type=float, default=DEFAULT_BM25_WEIGHT, help="Вес текущего режима для сравнения")
    parser.add_argument("--holdout", type=float, default=.30, help="Примерная доля тематического контроля")
    parser.add_argument("--seed", default="alfahack-bm25-v1", help="Идентификатор воспроизводимого разделения тем")
    parser.add_argument("--jobs", type=int, default=1, help="Число процессов для перебора весов")
    parser.add_argument("--backup", type=Path, help="Бэкап для специальных и сложных сценариев для всех весов")
    parser.add_argument("--tune-fields", action="store_true", help="Подобрать веса заголовка, описания и тегов")
    parser.add_argument("--field-candidates", nargs="+", help="Своя сетка полей, например 3,1,2 3,2,1")
    parser.add_argument("--tune-length", action="store_true", help="Подобрать нормализацию длины b внутри BM25")
    parser.add_argument("--length-candidates", type=float, nargs="+", help="Своя сетка b от 0 до 1")
    parser.add_argument("--tune-saturation", action="store_true", help="Подобрать насыщение по частоте k1")
    parser.add_argument("--saturation-candidates", type=float, nargs="+", help="Своя сетка неотрицательных k1")
    parser.add_argument("--reuse-outer", type=Path, help="Прочитать ранее рассчитанную сетку со стандартными весами полей")
    parser.add_argument("--output", type=Path, help="Путь к JSON-отчёту")
    args = parser.parse_args()
    reference = args.reference_weight
    weights = sorted({0.0, reference, *args.weights})
    if not all(0 <= w <= 1 for w in weights) or not 0 < args.holdout < 1 or args.jobs < 1:
        parser.error("Проверьте диапазоны весов и доли контроля, а также положительное число процессов.")
    if args.backup and not args.backup.is_file():
        parser.error("Файл бэкапа не найден.")
    fields = FIELD_CANDIDATES if not args.field_candidates else tuple(map(_parse_fields, args.field_candidates))
    if None in fields or (args.field_candidates and not args.tune_fields):
        parser.error("--field-candidates ожидает тройки неотрицательных чисел и флаг --tune-fields.")
    lengths = args.length_candidates or LENGTH_CANDIDATES
    if not all(0 <= value <= 1 for value in lengths) or (args.length_candidates and not args.tune_length):
        parser.error("--length-candidates ожидает числа от 0 до 1 и флаг --tune-length.")
    saturations = args.saturation_candidates or SATURATION_CANDIDATES
    if not all(isfinite(value) and value >= 0 for value in saturations) or (
        args.saturation_candidates and not args.tune_saturation
    ):
        parser.error("--saturation-candidates ожидает неотрицательные числа и флаг --tune-saturation.")

    try:
        source = args.catalog or catalog_source()
        catalog, cases = load_evaluation_inputs("real", load_real_catalog(source))
        demo_catalog, demo = load_evaluation_inputs("demo")
        tuning, validation, train_topics, test_topics = split_by_topic(cases, args.holdout, args.seed)
        fingerprint = catalog_fingerprint(catalog)
        evaluate = partial(_grid_row, catalog=catalog, tuning=tuning, source=source,
                           demo_catalog=demo_catalog, demo=demo, backup=args.backup)
        if args.reuse_outer:
            outer = _reuse_outer(args.reuse_outer, fingerprint=fingerprint, seed=args.seed, reference=reference,
                                 weights=weights, train_topics=train_topics, test_topics=test_topics,
                                 specialized=bool(args.backup))
        else:
            outer = _grid([(weight, FIELD_WEIGHTS, B, K1) for weight in weights], args.jobs, evaluate)
        reference_row = next(row for row in outer if row["bm25_weight"] == reference)

        def evaluate_quality(rows):
            if args.backup:
                for row in rows:
                    row["quality_regressions"] = quality_regressions(row, reference_row)

        def best_row(rows):
            return max(rows, key=lambda row: _choice(row, reference))

        def best_qualified(rows):
            return best_row(row for row in rows if not row.get("quality_regressions"))

        def best_positive(rows):
            positive = [row for row in rows if row["bm25_weight"] > 0]
            if not positive:
                parser.error("Для настройки BM25 нужен хотя бы один ненулевой вес гибрида.")
            qualified = [row for row in positive if not row.get("quality_regressions")]
            return best_row(qualified or positive)

        evaluate_quality(outer)
        outer_winner = best_qualified(outer)
        outer_ndcg = best_row(outer)
        inner = []
        if args.tune_fields:
            seed_weight = best_positive(outer)["bm25_weight"]
            configurations = [(seed_weight, field, B, K1) for field in dict.fromkeys(fields) if field != FIELD_WEIGHTS]
            if not configurations:
                parser.error("Укажите хотя бы один вариант полей, отличный от стандартного.")
            inner = _grid(configurations, args.jobs, evaluate)
            evaluate_quality(inner)
            qualified = [row for row in inner if not row.get("quality_regressions")]
            field_candidate = best_row(qualified or inner)
            crossover = [(weight, field_candidate["field_weights"], B, K1)
                         for weight in sorted({reference, outer_ndcg["bm25_weight"]} - {0.0, seed_weight})]
            if crossover:
                extra = _grid(crossover, args.jobs, evaluate)
                evaluate_quality(extra)
                inner.extend(extra)

        length_rows = []
        if args.tune_length:
            base = best_positive(outer + inner)
            length_configs = [(base["bm25_weight"], base["field_weights"], value, K1)
                              for value in dict.fromkeys(lengths) if value != B]
            if not length_configs:
                parser.error("Укажите хотя бы одно значение b, отличное от стандартного.")
            length_rows = _grid(length_configs, args.jobs, evaluate)
            evaluate_quality(length_rows)

        saturation_rows = []
        if args.tune_saturation:
            base = best_positive(outer + inner + length_rows)
            saturation_configs = [(base["bm25_weight"], base["field_weights"], base["length_penalty"], value)
                                  for value in dict.fromkeys(saturations) if value != K1]
            if not saturation_configs:
                parser.error("Укажите хотя бы одно значение k1, отличное от стандартного.")
            saturation_rows = _grid(saturation_configs, args.jobs, evaluate)
            evaluate_quality(saturation_rows)

        rows = outer + inner + length_rows + saturation_rows
        winner = best_qualified(rows)
        ndcg_winner = best_row(rows)
        baseline = next(row for row in outer if row["bm25_weight"] == 0)
        finalists_by_config = {_label(row): row for row in (baseline, reference_row, outer_winner, outer_ndcg, winner, ndcg_winner)}
        for stage in (inner, length_rows, saturation_rows):
            if stage:
                stage_winner = best_row(stage)
                finalists_by_config[_label(stage_winner)] = stage_winner
        finalists = list(finalists_by_config.values())
        for row in finalists:
            row["validation"] = measure(row["bm25_weight"], catalog, validation, source.parent,
                                        row["field_weights"], row["length_penalty"], row["saturation"])
            row["real_all"] = _combine(row["tuning"], row["validation"])
        report = {
            "catalog_fingerprint": fingerprint, "seed": args.seed, "objective": "ndcg_at_3",
            "reference_bm25_weight": reference, "selected_bm25_weight": winner["bm25_weight"],
            "selected_field_weights": winner["field_weights"],
            "selected_length_penalty": winner["length_penalty"],
            "selected_saturation": winner["saturation"],
            "unconstrained_bm25_weight": ndcg_winner["bm25_weight"], "unconstrained_field_weights": ndcg_winner["field_weights"],
            "unconstrained_length_penalty": ndcg_winner["length_penalty"],
            "unconstrained_saturation": ndcg_winner["saturation"],
            "tuning_topics": train_topics, "validation_topics": test_topics, "grid": outer, "field_grid": inner,
            "length_grid": length_rows,
            "saturation_grid": saturation_rows,
            "best_tuning_weights": {m: max(rows, key=lambda row: _choice(row, reference, m))["bm25_weight"] for m in METRICS},
        }
        if args.backup:
            report["quality_limits"] = QUALITY_LIMITS
            report["catalog_quality"] = {label: _catalog_quality(args.backup, source, row)
                                         for label, row in {_label(item): item for item in (reference_row, winner, ndcg_winner)}.items()}
        report["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, KeyError, TypeError, ModuleNotFoundError) as error:
        parser.exit(1, f"{error}\n")

    print(f"Настройка: {len(tuning)} сценариев. Тематический контроль: {len(validation)}. Синтетика: {len(demo)}.")
    print(f"Выбрана конфигурация {_label(winner)}. Максимум nDCG@3 на наборе настройки дал {_label(ndcg_winner)}.\n")
    for title, batch, key in (
        ("Доли BM25 и recommend", outer, "tuning"),
        ("Веса полей BM25", inner, "tuning"),
        ("Нормализация длины BM25", length_rows, "tuning"),
        ("Насыщение частоты BM25", saturation_rows, "tuning"),
        ("Тематический контроль", finalists, "validation"),
        ("Весь реальный каталог, описательно", finalists, "real_all"),
        ("Синтетика для финалистов", finalists, "synthetic"),
    ):
        if not batch:
            continue
        print(f"## {title}\n")
        _table(batch, key)
        print()
    if args.backup:
        print("## Специальные и сложные сценарии\n")
        print("| Конфигурация | Проверено сценариев | Метрик ниже текущего качества |")
        print("| ---: | ---: | ---: |")
        for row in rows:
            count = sum(suite["cases"] for suite in row["specialized"]["suites"])
            print(f"| {_label(row)} | {count} | {len(row['quality_regressions'])} |")


if __name__ == "__main__":
    main()
