"""Ручная стартовая подборка для пустого профиля."""

from collections.abc import Sequence

from app.contracts import Material, Profile, Recommendation, RecommendationResult


ONBOARDING_ITEMS = (
    Recommendation("3d17b03e-0e69-44cf-aa04-110e92437016", "Вводный курс помогает понять, с чего начать бизнес.",
                   "Посмотрите первые уроки и выберите ближайшую задачу."),
    Recommendation("441225ac-0947-4951-baa3-f065f645d667", "Обзор помогает найти и проверить идею бизнеса.",
                   "Запишите две идеи и сравните, какую проще проверить."),
    Recommendation("9766af8e-1923-4313-a4a8-e78a218d4b81", "Популярный калькулятор помогает быстро рассчитать НДФЛ.",
                   "Попробуйте ввести сумму и проверить результат."),
)


def onboarding(catalog: Sequence[Material], profile: Profile, limit: int) -> RecommendationResult | None:
    fields = (profile.goal, profile.stage, profile.industry, profile.experience,
              profile.team_size, profile.learning_topic, profile.preferred_format)
    if any((value or "").strip().casefold() not in {"", "любая тема", "любой формат", "другое"} for value in fields):
        return None
    available = {item.id for item in catalog if item.access == "free" and not item.subscriptions}
    if not all(item.material_id in available for item in ONBOARDING_ITEMS):
        return None
    return RecommendationResult(
        ONBOARDING_ITEMS[:limit], "Онбординг-пакет: вводный курс, обзорная статья и практический калькулятор.",
        "Чем вы хотите заняться в первую очередь?",
    )
