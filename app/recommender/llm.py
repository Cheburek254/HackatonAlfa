"""Опциональное обогащение объяснений через внешний LLM API (с безопасным фоллбэком)."""

import json
import os
import urllib.request
from collections.abc import Sequence

from app.contracts import Material, Profile, Recommendation, RecommendationResult


def refine_with_llm_if_configured(
    selected_materials: Sequence[Material],
    profile: Profile,
    draft_result: RecommendationResult,
) -> RecommendationResult:
    """Если задан ключ ALFA_LLM_API_KEY, уточняет тексты объяснений через совместимый API.

    Не передаёт закрытое содержимое платных материалов и персональные данные.
    При отсутствии ключа или любой ошибке сети мгновенно возвращает draft_result.
    """
    api_key = os.environ.get("ALFA_LLM_API_KEY", "").strip()
    if not api_key or not selected_materials:
        return draft_result

    api_url = os.environ.get(
        "ALFA_LLM_API_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    ).strip()
    model = os.environ.get("ALFA_LLM_MODEL", "gemini-2.5-flash").strip()

    cards_payload = [
        {
            "id": m.id,
            "title": m.title,
            "format": m.format or "материал",
            "access": m.access,
            "topics": list(m.topics[:5]),
            "short_description": (m.description or "")[:180],
        }
        for m in selected_materials
    ]
    profile_payload = {
        "goal": profile.goal,
        "stage": profile.stage,
        "industry": profile.industry,
        "experience": profile.experience,
        "learning_topic": getattr(profile, "learning_topic", None),
        "preferred_format": getattr(profile, "preferred_format", None),
    }

    prompt = (
        "Ты — рекомендательный движок образовательной платформы «Альфа-Курс» для предпринимателей.\n"
        "Для уже отобранных карточек сформируй краткие, полезные пояснения на русском языке.\n"
        "Для платных материалов (access='paid') НЕ выдумывай внутреннее содержание уроков — "
        "кратко опиши бизнес-пользу и предложи перейти по ссылке.\n"
        "Верни строго JSON-объект вида:\n"
        '{"ordering_reason": "...", "follow_up_question": "..." или null, '
        '"items": [{"material_id": "...", "reason": "...", "next_step": "..."}]}\n\n'
        f"Профиль запроса: {json.dumps(profile_payload, ensure_ascii=False)}\n"
        f"Отобранные материалы по порядку: {json.dumps(cards_payload, ensure_ascii=False)}"
    )

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(
        api_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
        content = resp_data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        by_id = {item["material_id"]: item for item in parsed.get("items", []) if "material_id" in item}
        new_items = []
        for orig in draft_result.items:
            llm_item = by_id.get(orig.material_id, {})
            reason = str(llm_item.get("reason") or orig.reason).strip()
            next_step = str(llm_item.get("next_step") or orig.next_step).strip()
            new_items.append(Recommendation(material_id=orig.material_id, reason=reason, next_step=next_step))
        ordering_reason = str(parsed.get("ordering_reason") or draft_result.ordering_reason).strip()
        follow_up = parsed.get("follow_up_question", draft_result.follow_up_question)
        return RecommendationResult(
            items=tuple(new_items),
            ordering_reason=ordering_reason,
            follow_up_question=str(follow_up).strip() if follow_up else draft_result.follow_up_question,
        )
    except Exception:
        return draft_result
