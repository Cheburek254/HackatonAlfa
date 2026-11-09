"""Один набор сценариев для CLI и админ-панели."""

import json
from dataclasses import dataclass
from pathlib import Path

from app.catalog import load_catalog, load_history
from app.contracts import Profile, Recommender
from evaluation.topic_baseline import recommend_by_topic

EVALUATION_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVALUATION_DIR.parent / "fixtures"
TOP_K = 3


@dataclass(frozen=True)
class EvaluationRow:
    case_id: str
    note: str
    method: str
    material_ids: tuple[str, ...]
    reciprocal_rank: float | None


@dataclass(frozen=True)
class EvaluationSummary:
    method: str
    eligible: int
    hits: int
    mrr: float | None

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.eligible if self.eligible else None


@dataclass(frozen=True)
class EvaluationReport:
    summaries: tuple[EvaluationSummary, ...]
    rows: tuple[EvaluationRow, ...]
    scenario_count: int


def run_evaluation(recommender: Recommender) -> EvaluationReport:
    catalog = load_catalog(FIXTURES_DIR / "catalog.demo.json") + load_catalog(EVALUATION_DIR / "catalog.extra.json")
    catalog_ids = {material.id for material in catalog}
    cases = json.loads((EVALUATION_DIR / "profiles.demo.json").read_text(encoding="utf-8"))
    methods = (("Тематический бейзлайн", recommend_by_topic), ("recommend", recommender))
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
        for name, recommend in methods:
            result = recommend(catalog, profile, history, limit=TOP_K)
            ids = tuple(item.material_id for item in result.items[:TOP_K])
            score = None
            if relevant:
                score = next((1 / rank for rank, mid in enumerate(ids, 1) if mid in relevant), 0.0)
                ranks[name].append(score)
            rows.append(EvaluationRow(case_id, case["note"], name, ids, score))

    summaries = tuple(
        EvaluationSummary(name, len(scores), sum(score > 0 for score in scores), sum(scores) / len(scores) if scores else None)
        for name, scores in ranks.items()
    )
    return EvaluationReport(summaries, tuple(rows), len(cases))
