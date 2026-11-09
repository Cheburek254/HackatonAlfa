"""Фиксированная выдача для разработки интерфейса."""

from collections.abc import Sequence

from app.contracts import (
    InteractionEvent,
    Material,
    Profile,
    Recommendation,
    RecommendationResult,
)


def recommend(
    catalog: Sequence[Material],
    profile: Profile,
    history: Sequence[InteractionEvent],
    limit: int,
) -> RecommendationResult:
    items = tuple(
        Recommendation(
            material_id=material.id,
            reason="Пример объяснения для разработки интерфейса.",
            next_step="Открыть карточку материала.",
        )
        for material in catalog[: max(0, limit)]
    )
    return RecommendationResult(
        items=items,
        ordering_reason="Карточки показаны в порядке тестового каталога." if items else "",
    )
