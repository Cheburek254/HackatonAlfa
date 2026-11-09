"""Подбор по буквальному совпадению с темами каталога."""

import re
from collections.abc import Sequence
from functools import lru_cache

from app.contracts import (
    InteractionEvent,
    Material,
    Profile,
    Recommendation,
    RecommendationResult,
)


@lru_cache(maxsize=4096)
def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.casefold().replace("ё", "е")))


def recommend_by_topic(
    catalog: Sequence[Material],
    profile: Profile,
    history: Sequence[InteractionEvent],
    limit: int,
) -> RecommendationResult:
    """Ранжировать по числу тем, встречающихся в задаче пользователя."""
    query = _normalize(profile.goal or "")
    if not query or limit <= 0:
        return RecommendationResult((), "")

    padded_query = f" {query} "
    ranked = []
    for material in catalog:
        matches = {}
        for topic in material.topics:
            normalized = _normalize(topic)
            if normalized and f" {normalized} " in padded_query:
                matches.setdefault(normalized, topic)
        if matches:
            item = Recommendation(
                material_id=material.id,
                reason=f"В запросе совпали темы «{', '.join(matches.values())}».",
                next_step="Откройте материал и выберите подходящий раздел.",
            )
            ranked.append((len(matches), item))

    ranked.sort(key=lambda match: match[0], reverse=True)
    items = tuple(item for _, item in ranked[:limit])
    ordering_reason = (
        "Выше материалы с большим числом совпавших тем. "
        "При равенстве действует порядок каталога."
    )
    return RecommendationResult(items, ordering_reason if items else "")
