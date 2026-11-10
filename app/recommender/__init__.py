"""Экспорт функции персональных рекомендаций по общему контракту."""

from app.recommender.engine import recommend

RANKING_MODES = {
    "popular": "Бейзлайн · популярные курсы",
    "recommend": "recommend",
    "hybrid": "BM25 + recommend",
}
DEFAULT_RANKING_MODE = "hybrid"

__all__ = ["recommend"]
