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
    if limit <= len(ONBOARDING_ITEMS):
        return RecommendationResult(
            ONBOARDING_ITEMS[:limit], "Онбординг-пакет: вводный курс, обзорная статья и практический калькулятор.",
            "Чем вы хотите заняться в первую очередь?",
        )

    from app.recommender.engine import _build_card_explanation

    by_id = {item.id: item for item in catalog}
    seen_ids = {item.material_id for item in ONBOARDING_ITEMS}
    seen_urls = {by_id[mid].url for mid in seen_ids if mid in by_id}
    rest_materials: list[Material] = []
    for mat in catalog:
        if not mat.id or not mat.title.strip() or not mat.url.strip():
            continue
        if mat.id in seen_ids or mat.url in seen_urls:
            continue
        seen_ids.add(mat.id)
        seen_urls.add(mat.url)
        rest_materials.append(mat)

    rest_materials.sort(key=lambda m: (m.popularity or 0.0, m.rating or 0.0), reverse=True)
    empty_signals = {
        "matched_goal_topics": [],
        "goal_direct": False,
        "matched_learning_topic": False,
        "stage_matched": False,
        "industry_matched": False,
        "exp_matched": False,
        "fmt_matched": False,
        "history_continuation_from": None,
    }
    extra_items = tuple(
        Recommendation(
            mat.id,
            *_build_card_explanation(mat, profile, empty_signals, is_cold_start=True, is_weak_match=False),
        )
        for mat in rest_materials[: limit - len(ONBOARDING_ITEMS)]
    )
    return RecommendationResult(
        ONBOARDING_ITEMS + extra_items,
        "Онбординг-пакет: вводный курс, обзорная статья, практический калькулятор и самые популярные материалы каталога.",
        "Чем вы хотите заняться в первую очередь?",
    )
