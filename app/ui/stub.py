"""Демонстрационная выдача с учётом истории до подключения основного ранжирования."""

import re
from collections.abc import Sequence

from app.contracts import (
    InteractionEvent,
    Material,
    Profile,
    Recommendation,
    RecommendationResult,
)

EVENT_WEIGHTS = {"view": 1, "click": 2, "complete": 3}


def _words(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", text.casefold().replace("ё", "е")))


def recommend(
    catalog: Sequence[Material],
    profile: Profile,
    history: Sequence[InteractionEvent],
    limit: int,
) -> RecommendationResult:
    if limit <= 0 or not catalog:
        return RecommendationResult((), "")

    topics_by_material = {material.id: set(material.topics) for material in catalog}
    signals = [
        (event.material_id, topics_by_material[event.material_id], EVENT_WEIGHTS.get(event.kind, 0))
        for event in history if event.material_id in topics_by_material
    ]
    query = f" {_words(profile.goal or profile.learning_topic or '')} "
    query_topics = {
        topic for material in catalog for topic in material.topics
        if topic and f" {_words(topic)} " in query
    } if signals else set()

    ranked = []
    for material in catalog:
        topics = topics_by_material[material.id]
        matches_query = bool(topics & query_topics)
        history_score = 0
        if matches_query or (not profile.goal and not query_topics):
            history_score = max(
                (weight for source, source_topics, weight in signals
                 if source != material.id and topics & source_topics),
                default=0,
            )
        reason = "Материал из стартовой подборки."
        if history_score:
            reason = "Продолжает тему материалов, с которыми вы уже взаимодействовали."
        elif matches_query:
            reason = "Совпадает с темой вашего запроса или профиля."
        ranked.append(((matches_query, history_score), Recommendation(
            material.id, reason, "Откройте карточку и посмотрите описание материала.",
        )))

    ranked.sort(key=lambda entry: entry[0], reverse=True)
    selected = ranked[:limit]
    explanations = []
    if query_topics:
        explanations.append("Сначала материалы по указанной теме.")
    if any(score for (_, score), _ in selected):
        explanations.append("Выше материалы по темам ваших просмотров и переходов.")
    explanation = " ".join(explanations) or "Карточки показаны в порядке тестового каталога."
    return RecommendationResult(tuple(item for _, item in selected), explanation)
