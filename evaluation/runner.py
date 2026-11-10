"""Один набор сценариев для CLI и админ-панели."""

import json
from dataclasses import dataclass
from functools import lru_cache
from math import log2
from pathlib import Path

from app.catalog import DEMO_CATALOG_PATH, load_catalog, load_history, material_format
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
    precision: float | None
    ndcg: float | None
    average_precision: float | None


@dataclass(frozen=True)
class EvaluationSummary:
    method: str
    eligible: int
    hits: int
    mrr: float | None
    precision: float | None
    ndcg: float | None
    map_at_k: float | None

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.eligible if self.eligible else None


@dataclass(frozen=True)
class EvaluationReport:
    summaries: tuple[EvaluationSummary, ...]
    rows: tuple[EvaluationRow, ...]
    scenario_count: int


def _ranking_scores(ids: tuple[str, ...], relevant: set[str]) -> tuple[float, float, float, float]:
    hit_ranks = []
    remaining = relevant.copy()
    for rank, mid in enumerate(ids[:TOP_K], 1):
        if mid in remaining:
            hit_ranks.append(rank)
            remaining.remove(mid)
    ideal_count = min(TOP_K, len(relevant))
    ideal_gain = sum(1 / log2(rank + 1) for rank in range(1, ideal_count + 1))
    reciprocal_rank = 1 / hit_ranks[0] if hit_ranks else 0.0
    precision = len(hit_ranks) / TOP_K
    ndcg = sum(1 / log2(rank + 1) for rank in hit_ranks) / ideal_gain
    average_precision = sum(hit / rank for hit, rank in enumerate(hit_ranks, 1)) / ideal_count
    return reciprocal_rank, precision, ndcg, average_precision


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
    popular_courses = sorted(
        (m for m in catalog if (m.entity_type == "course" or material_format(m) == "курс") and m.popularity is not None),
        key=lambda m: (-m.popularity, m.id),
    )
    popular_ids = tuple(dict.fromkeys(m.id for m in popular_courses))[:TOP_K]
    methods = ("Популярные курсы", "Тематический бейзлайн", "recommend")
    scores_by_method: dict[str, list[tuple[float, float, float, float]]] = {name: [] for name in methods}
    rows = []

    for case in cases:
        relevant = set(case["relevant_ids"])
        fixture = case.get("history_fixture")
        history = load_history(FIXTURES_DIR / fixture) if fixture else tuple(
            InteractionEvent(**event) for event in case.get("history", ())
        )
        profile = Profile(**case["profile"])
        topic = recommend_by_topic(catalog, profile, history, limit=TOP_K)
        personalized = recommender(catalog, profile, history, limit=TOP_K)
        orders = (popular_ids, *(tuple(item.material_id for item in result.items[:TOP_K]) for result in (topic, personalized)))
        for name, ids in zip(methods, orders, strict=True):
            scores = (None, None, None, None)
            if relevant and (name != methods[0] or popular_ids):
                scores = _ranking_scores(ids, relevant)
                scores_by_method[name].append(scores)
            rows.append(EvaluationRow(case["id"], case["note"], profile.goal, tuple(case["relevant_ids"]), name, ids, *scores))

    summaries = []
    for name, scores in scores_by_method.items():
        averages = tuple(sum(metric) / len(scores) for metric in zip(*scores)) if scores else (None,) * 4
        summaries.append(EvaluationSummary(name, len(scores), sum(row[0] > 0 for row in scores), *averages))
    return EvaluationReport(tuple(summaries), tuple(rows), len(cases))
