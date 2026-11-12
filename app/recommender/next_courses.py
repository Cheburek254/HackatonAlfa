"""Подборка «Следующий курс»: доступные неначатые курсы по недавней популярности."""

from collections.abc import Sequence
from dataclasses import dataclass

from app.catalog import material_format
from app.catalog.subscriptions import access_category
from app.contracts import InteractionEvent, Material

NEXT_COURSES_LIMIT = 6


@dataclass(frozen=True)
class NextCoursePick:
    material: Material
    reason: str


def next_course_picks(
    catalog: Sequence[Material],
    history: Sequence[InteractionEvent],
    available: tuple[str, ...],
    *,
    limit: int = NEXT_COURSES_LIMIT,
) -> tuple[NextCoursePick, ...]:
    """Подобрать доступные курсы, которые пользователь ещё не начинал, по недавней популярности."""
    if limit <= 0:
        return ()
    started_ids = {event.material_id for event in history if event.material_id}
    candidates = [
        m
        for m in catalog
        if m.id
        and m.title
        and m.url
        and m.id not in started_ids
        and (m.entity_type == "course" or material_format(m) == "курс")
        and access_category(m) in available
    ]
    candidates.sort(
        key=lambda m: (
            -(m.recent_popularity if m.recent_popularity is not None else (m.popularity or 0.0)),
            -(m.popularity or 0.0),
            -(m.rating or 0.0),
            m.id,
        )
    )
    return tuple(
        NextCoursePick(
            material=m,
            reason="Популярный среди новых стартов курс, который вы ещё не начинали.",
        )
        for m in candidates[:limit]
    )
