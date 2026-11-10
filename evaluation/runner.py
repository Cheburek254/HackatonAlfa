"""Один набор сценариев для CLI и админ-панели."""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.catalog import DEMO_CATALOG_PATH, load_catalog, load_history
from app.contracts import InteractionEvent, Material, Profile, Recommender
from evaluation.topic_baseline import recommend_by_topic

EVALUATION_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = EVALUATION_DIR.parent / "fixtures"
TOP_K = 3
SUITE_LABELS = {"demo": "Синтетический каталог", "real": "Реальный каталог"}


class EvaluationDataError(ValueError):
    """Разметку нельзя оценить на текущем каталоге."""


@lru_cache(maxsize=len(SUITE_LABELS))
def _load_cases(suite: str) -> list[dict]:
    return json.loads((EVALUATION_DIR / f"profiles.{suite}.json").read_text(encoding="utf-8"))


def evaluation_scenario_counts() -> dict[str, int]:
    return {suite: len(_load_cases(suite)) for suite in SUITE_LABELS}


def load_real_catalog(source: Path) -> tuple[Material, ...]:
    if source.resolve() == DEMO_CATALOG_PATH.resolve():
        raise EvaluationDataError("Для запуска реальных сценариев подключите экспорт каталога.")
    if not source.is_file():
        raise EvaluationDataError(f"Файл каталога не найден: {source}")
    return load_catalog(source)


@dataclass(frozen=True)
class EvaluationRow:
    case_id: str
    note: str
    goal: str | None
    relevant_ids: tuple[str, ...]
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


def run_evaluation(
    recommender: Recommender, *, suite: str = "demo", real_catalog: tuple[Material, ...] | None = None,
) -> EvaluationReport:
    if suite == "demo":
        catalog = load_catalog(DEMO_CATALOG_PATH) + load_catalog(EVALUATION_DIR / "catalog.extra.json")
    elif suite == "real":
        if not real_catalog:
            raise EvaluationDataError("Для запуска реальных сценариев подключите экспорт каталога.")
        catalog = real_catalog
    else:
        raise EvaluationDataError("Неизвестный набор сценариев.")
    catalog_ids = {material.id for material in catalog}
    cases = _load_cases(suite)
    for case in cases:
        referenced = set(case["relevant_ids"]) | {event["material_id"] for event in case.get("history", ())}
        if unknown_ids := referenced - catalog_ids:
            raise EvaluationDataError(
                f"Каталог не совпадает с разметкой. В сценарии {case['id']} отсутствуют материалы: "
                f"{', '.join(sorted(unknown_ids))}."
            )
    methods = (("Тематический бейзлайн", recommend_by_topic), ("recommend", recommender))
    ranks: dict[str, list[float]] = {name: [] for name, _ in methods}
    rows = []

    for case in cases:
        relevant = set(case["relevant_ids"])
        fixture = case.get("history_fixture")
        history = load_history(FIXTURES_DIR / fixture) if fixture else tuple(
            InteractionEvent(**event) for event in case.get("history", ())
        )
        profile = Profile(**case["profile"])
        for name, recommend in methods:
            result = recommend(catalog, profile, history, limit=TOP_K)
            ids = tuple(item.material_id for item in result.items[:TOP_K])
            score = None
            if relevant:
                score = next((1 / rank for rank, mid in enumerate(ids, 1) if mid in relevant), 0.0)
                ranks[name].append(score)
            rows.append(EvaluationRow(case["id"], case["note"], profile.goal, tuple(case["relevant_ids"]), name, ids, score))

    summaries = tuple(
        EvaluationSummary(name, len(scores), sum(score > 0 for score in scores), sum(scores) / len(scores) if scores else None)
        for name, scores in ranks.items()
    )
    return EvaluationReport(summaries, tuple(rows), len(cases))
