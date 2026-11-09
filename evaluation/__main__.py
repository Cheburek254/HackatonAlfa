"""Сравнение бейзлайна и recommend на одной разметке."""

import json
from collections.abc import Sequence
from pathlib import Path

from app import recommender as recommender_module
from app.catalog import load_catalog, load_history
from app.contracts import Profile, Recommender
from app.ui.stub import recommend as demo_recommend
from evaluation.topic_baseline import recommend_by_topic

EVALUATION_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVALUATION_DIR.parent / "fixtures"
TOP_K = 3


def _comparison_target() -> tuple[str, Recommender]:
    candidate = getattr(recommender_module, "recommend", demo_recommend)
    if not callable(candidate):
        raise TypeError("app.recommender.recommend должен быть вызываемой функцией")
    label = "recommend (демо)" if candidate is demo_recommend else "recommend"
    return label, candidate


def _reciprocal_rank(returned_ids: Sequence[str], relevant_ids: set[str]) -> float:
    return next(
        (1 / rank for rank, material_id in enumerate(returned_ids[:TOP_K], start=1)
         if material_id in relevant_ids),
        0.0,
    )


def main() -> None:
    catalog = load_catalog(FIXTURES_DIR / "catalog.demo.json")
    catalog_ids = {material.id for material in catalog}
    cases = json.loads((EVALUATION_DIR / "profiles.demo.json").read_text(encoding="utf-8"))
    target_label, target = _comparison_target()
    methods = (("Тематический бейзлайн", recommend_by_topic), (target_label, target))
    ranks: dict[str, list[float]] = {name: [] for name, _ in methods}
    rows = []

    for case in cases:
        case_id = case["id"]
        relevant = set(case["relevant_ids"])
        if unknown_ids := relevant - catalog_ids:
            raise ValueError(f"{case_id} ссылается на отсутствующие материалы {sorted(unknown_ids)}")
        fixture = case.get("history_fixture")
        history = load_history(FIXTURES_DIR / fixture) if fixture else ()
        profile = Profile(**case["profile"])
        for name, recommender in methods:
            result = recommender(catalog, profile, history, limit=TOP_K)
            returned_ids = [item.material_id for item in result.items[:TOP_K]]
            hit_text = rr_text = "—"
            if relevant:
                score = _reciprocal_rank(returned_ids, relevant)
                ranks[name].append(score)
                hit_text, rr_text = str(int(score > 0)), f"{score:.3f}"
            rows.append(
                f"| {case_id} | {name} | {', '.join(returned_ids) or 'пусто'} | {hit_text} | {rr_text} |"
            )

    source = "app.ui.stub.recommend, демонстрационная версия" if target is demo_recommend else "app.recommender.recommend"
    print(f"Сравниваем с {source} на каталоге fixtures/catalog.demo.json.\n")
    print("| Метод | Hit@3 | MRR@3 |")
    print("| --- | ---: | ---: |")
    for name, scores in ranks.items():
        hit = mrr = "—"
        if scores:
            hits = sum(score > 0 for score in scores)
            hit = f"{hits / len(scores):.1%} ({hits}/{len(scores)})"
            mrr = f"{sum(scores) / len(scores):.3f}"
        print(f"| {name} | {hit} | {mrr} |")

    print("\n| Профиль | Метод | Топ-3 в порядке выдачи | Hit@3 | RR@3 |")
    print("| --- | --- | --- | ---: | ---: |")
    for row in rows:
        print(row)
    print("\nСценарии без релевантных материалов помечены «—» и исключены из обеих метрик.")


if __name__ == "__main__":
    main()
