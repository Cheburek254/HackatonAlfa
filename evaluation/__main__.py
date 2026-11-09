"""Запуск бейзлайна на синтетическом каталоге и размеченных профилях."""

import json
from pathlib import Path

from app.catalog import load_catalog, load_history
from app.contracts import Profile, Recommender
from evaluation.topic_baseline import recommend_by_topic

EVALUATION_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVALUATION_DIR.parent / "fixtures"


def main() -> None:
    catalog = load_catalog(FIXTURES_DIR / "catalog.demo.json")
    catalog_ids = {material.id for material in catalog}
    cases = json.loads((EVALUATION_DIR / "profiles.demo.json").read_text(encoding="utf-8"))
    recommender: Recommender = recommend_by_topic
    hits = 0
    eligible = 0

    for case in cases:
        case_id = case["id"]
        relevant = set(case["relevant_ids"])
        if unknown_ids := relevant - catalog_ids:
            raise ValueError(f"{case_id} ссылается на отсутствующие материалы {sorted(unknown_ids)}")
        fixture = case.get("history_fixture")
        history = load_history(FIXTURES_DIR / fixture) if fixture else ()
        result = recommender(catalog, Profile(**case["profile"]), history, limit=3)
        returned_ids = [item.material_id for item in result.items]
        if relevant:
            eligible += 1
            hit = bool(relevant.intersection(returned_ids))
            hits += hit
            verdict = "попадание" if hit else "промах"
        else:
            verdict = "вне Hit@3, нет подходящих материалов"
        print(f"{case_id} — {', '.join(returned_ids) or 'пустая выдача'} ({verdict})")

    metric = f"{hits / eligible:.1%} ({hits}/{eligible})" if eligible else "нет сценариев с релевантными материалами"
    print(f"Hit@3 — {metric}")


if __name__ == "__main__":
    main()
