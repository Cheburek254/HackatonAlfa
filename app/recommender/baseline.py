"""Контрольный порядок курсов по известной популярности."""

from collections.abc import Sequence

from app.catalog import material_format
from app.contracts import Material, Recommendation, RecommendationResult


def recommend_popular_courses(catalog: Sequence[Material], limit: int) -> RecommendationResult:
    if limit <= 0:
        return RecommendationResult((), "")
    courses = sorted(
        (m for m in catalog if m.id and m.title and m.url and m.popularity is not None
         and (m.entity_type == "course" or material_format(m) == "курс")),
        key=lambda m: (-m.popularity, m.id),
    )
    ids = tuple(dict.fromkeys(m.id for m in courses))[:limit]
    items = tuple(Recommendation(mid, "Один из популярных курсов в выбранном каталоге.",
                                 "Откройте программу и выберите подходящий урок.") for mid in ids)
    return RecommendationResult(items, "Курсы отсортированы по популярности." if items else "")
