"""Сессионная панель и пресеты для живой проверки алгоритма жюри."""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from time import time
from typing import Any

from flask import session

from app.contracts import InteractionEvent, Material, Profile
from app.recommender import RANKING_MODES
from app.recommender.engine import _normalize_text
from app.ui.accounts import PROFILE_FIELDS, profile_values

SESSION_KEY = "jury_lab"
MAX_SESSION_HISTORY = 10
KIND_LABELS = {
    "view": "Просмотр",
    "click": "Клик",
    "complete": "Завершён",
}
KIND_WEIGHTS = {"complete": 3, "click": 2, "view": 1}

SCENARIO_LABELS = {
    "popular_baseline": "Бейзлайн · популярные курсы",
    "onboarding": "Онбординг · безопасный старт для новичка",
    "history_context": "Продолжение 2 недавних переходов",
    "hybrid": "Гибрид · BM25 + recommend",
    "weak_match": "Слабое совпадение → базовые материалы",
    "cold_start": "Холодный старт (цель, профиль и история пусты)",
    "history": "Персонализация по задаче, профилю и истории",
    "profile_goal": "Персонализация по задаче и профилю",
    "custom_recommender": "Пользовательский рекомендер",
}

TOPIC_SYNONYMS: dict[str, tuple[str, ...]] = {
    "маркетинг": ("маркетинг", "реклам", "продвиж", "смм", "smm", "бренд", "трафик"),
    "продажи": ("продаж", "клиент", "воронк", "сделк", "переговор", "лид"),
    "финансы": ("финанс", "налог", "учет", "учёт", "бухгалтер", "деньг", "прибыл", "марж", "бюджет"),
    "запуск бизнеса": ("запуск", "старт", "идея", "регистрац", "ип", "ооо", "бизнес-план"),
    "операционная работа": ("операцион", "планирован", "процесс", "команд", "управлен", "персонал"),
}

JURY_PRESETS: dict[str, dict[str, Any]] = {
    "cold_beginner": {
        "label": "Новичок · с нуля",
        "badge": "Холодный старт",
        "summary": "Идея, услуги, без истории — проверяем безопасный старт с базовых шагов.",
        "goal": "",
        "profile": {
            "stage": "идея",
            "industry": "услуги",
            "experience": "начинающий",
            "team_size": "работаю один",
            "learning_topic": "запуск бизнеса",
            "preferred_format": "чек-лист",
        },
        "course_plus": False,
        "history_specs": (),
    },
    "growth_retail_plus": {
        "label": "Рост · Курс+ и опыт",
        "badge": "Рост + подписка",
        "summary": "Розница, опытный владелец, команда 6–15 чел., активный Курс+ и 2 события.",
        "goal": "Увеличить продажи и выстроить повторную работу с клиентами",
        "profile": {
            "stage": "рост",
            "industry": "розница",
            "experience": "опытный",
            "team_size": "6–15 человек",
            "learning_topic": "продажи",
            "preferred_format": "курс",
        },
        "course_plus": True,
        "history_specs": (("продажи", "complete", "course"), ("маркетинг", "click", None)),
    },
    "finance_template": {
        "label": "Финансы · учёт и прибыль",
        "badge": "Задача + профиль",
        "summary": "Работающий общепит, запрос по финансам и марже, ориентир на шаблоны.",
        "goal": "Навести порядок в финансах и посчитать рентабельность",
        "profile": {
            "stage": "работающий бизнес",
            "industry": "общепит",
            "experience": "есть опыт",
            "team_size": "2–5 человек",
            "learning_topic": "финансы",
            "preferred_format": "шаблон",
        },
        "course_plus": False,
        "history_specs": (("финансы", "click", None),),
    },
    "history_context": {
        "label": "Поведение 2x · без цели",
        "badge": "История в приоритете",
        "summary": "Поле задачи пустое — 2 перехода по маркетингу запускают продолжение темы.",
        "goal": "",
        "profile": {
            "stage": "работающий бизнес",
            "industry": "интернет-торговля",
            "experience": "",
            "team_size": "2–5 человек",
            "learning_topic": "любая тема",
            "preferred_format": "любой формат",
        },
        "course_plus": False,
        "history_specs": (("маркетинг", "click", None), ("маркетинг", "click", None)),
    },
    "free_exact_bm25": {
        "label": "Бесплатные · поиск BM25",
        "badge": "Фильтр цены",
        "summary": "Запрос со словом «бесплатно» — наглядно видно отличие BM25 + ограничения.",
        "goal": "Первые продажи и поиск клиентов бесплатно",
        "profile": {
            "stage": "запуск",
            "industry": "услуги",
            "experience": "начинающий",
            "team_size": "работаю один",
            "learning_topic": "продажи",
            "preferred_format": "любой формат",
        },
        "course_plus": False,
        "history_specs": (),
    },
}

SIMULATION_EVENTS: dict[str, tuple[str, str, str, str | None]] = {
    "click_marketing": ("+ Клик: Маркетинг", "маркетинг", "click", None),
    "complete_sales": ("+ Завершён: Продажи", "продажи", "complete", "course"),
    "click_finance": ("+ Клик: Финансы", "финансы", "click", None),
    "view_launch": ("+ Просмотр: Запуск", "запуск бизнеса", "view", None),
    "complete_ops": ("+ Завершён: Операционка", "операционная работа", "complete", None),
}


def get_jury_state() -> dict[str, Any] | None:
    """Возвращает сохранённое состояние пульта текущей сессии без лишних мутаций."""
    raw = session.get(SESSION_KEY)
    return dict(raw) if isinstance(raw, dict) else None


def _ensure_jury_state() -> dict[str, Any]:
    current = get_jury_state() or {}
    return {
        "mode": current.get("mode") if current.get("mode") in RANKING_MODES else None,
        "bm25_adaptive": current.get("bm25_adaptive") if isinstance(current.get("bm25_adaptive"), bool) else None,
        "course_plus": current.get("course_plus") if isinstance(current.get("course_plus"), bool) else None,
        "preset": current.get("preset") if current.get("preset") in JURY_PRESETS else None,
        "goal": current.get("goal") if isinstance(current.get("goal"), str) else None,
        "profile": profile_values(current.get("profile")) if isinstance(current.get("profile"), dict) else None,
        "history_active": bool(current.get("history_active")),
        "history": _sanitize_history_rows(current.get("history")),
    }


def _save_jury_state(state: dict[str, Any]) -> None:
    compact: dict[str, Any] = {}
    if state.get("mode") in RANKING_MODES:
        compact["mode"] = state["mode"]
    if isinstance(state.get("bm25_adaptive"), bool):
        compact["bm25_adaptive"] = state["bm25_adaptive"]
    if isinstance(state.get("course_plus"), bool):
        compact["course_plus"] = state["course_plus"]
    if state.get("preset") in JURY_PRESETS:
        compact["preset"] = state["preset"]
    if isinstance(state.get("goal"), str):
        compact["goal"] = state["goal"]
    if isinstance(state.get("profile"), dict):
        compact["profile"] = {k: v for k, v in state["profile"].items() if k in PROFILE_FIELDS and v}
    if state.get("history_active"):
        compact["history_active"] = True
        compact["history"] = _sanitize_history_rows(state.get("history"))
    if compact:
        session[SESSION_KEY] = compact
    else:
        session.pop(SESSION_KEY, None)


def _sanitize_history_rows(rows: Any) -> list[list[Any]]:
    if not isinstance(rows, list):
        return []
    cleaned: list[list[Any]] = []
    for item in rows[-MAX_SESSION_HISTORY:]:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        mid, kind, ts = item
        if isinstance(mid, str) and mid and kind in KIND_LABELS and isinstance(ts, (int, float)):
            cleaned.append([mid, kind, int(ts)])
    return cleaned


def _material_topic_score(material: Material, topic: str, preferred_kind: str | None) -> tuple[int, int, int, str]:
    topic_norm = _normalize_text(topic)
    stems = TOPIC_SYNONYMS.get(topic_norm, (topic_norm,))
    topics_norm = [_normalize_text(item) for item in material.topics]
    title_norm = _normalize_text(material.title)
    desc_norm = _normalize_text(material.description)

    score = 0
    if topic_norm in topics_norm:
        score += 12
    if any(stem in topic_text for topic_text in topics_norm for stem in stems):
        score += 6
    if any(stem in title_norm for stem in stems):
        score += 4
    if any(stem in desc_norm for stem in stems):
        score += 2
    if preferred_kind and material.entity_type == preferred_kind:
        score += 3
    free_accessible = 1 if (material.access == "free" and not material.subscriptions) else 0
    if free_accessible:
        score += 1
    return (
        score,
        free_accessible if not preferred_kind else (1 if material.entity_type == preferred_kind else 0),
        1 if material.entity_type == "course" else 0,
        int(material.popularity or 0),
        material.id,
    )


def _pick_material_for_topic(
    catalog: Sequence[Material], topic: str, *, exclude_ids: set[str], preferred_kind: str | None = None,
) -> Material | None:
    if not catalog:
        return None
    pool = [mat for mat in catalog if mat.id not in exclude_ids] or list(catalog)
    return max(pool, key=lambda mat: _material_topic_score(mat, topic, preferred_kind))


def _events_from_rows(rows: list[list[Any]]) -> tuple[InteractionEvent, ...]:
    return tuple(
        InteractionEvent(material_id=mid, kind=kind, occurred_at=datetime.fromtimestamp(ts, UTC))
        for mid, kind, ts in rows
    )


def jury_mode_override() -> str | None:
    state = get_jury_state()
    if state and state.get("mode") in RANKING_MODES:
        return state["mode"]
    return None


def jury_bm25_adaptive_override() -> bool | None:
    state = get_jury_state()
    if state and isinstance(state.get("bm25_adaptive"), bool):
        return state["bm25_adaptive"]
    return None


def effective_course_plus(account) -> tuple[bool, bool]:
    """Возвращает (has_course_plus, is_overridden_in_session)."""
    state = get_jury_state()
    if state and isinstance(state.get("course_plus"), bool):
        return state["course_plus"], True
    return bool(account and account["has_course_plus"]), False


def jury_history_override() -> tuple[InteractionEvent, ...] | None:
    """Если жюри переключил историю или пресет в сессии, возвращает симулированную историю."""
    state = get_jury_state()
    if state and state.get("history_active"):
        return _events_from_rows(_sanitize_history_rows(state.get("history")))
    return None


def sync_jury_interaction(material_id: str, kind: str) -> None:
    """Отражает реальный просмотр/клик карточки в активной симулированной истории сессии."""
    if kind not in KIND_LABELS:
        return
    state = get_jury_state()
    if not state or not state.get("history_active"):
        return
    next_state = _ensure_jury_state()
    rows = next_state["history"]
    rows.append([material_id, kind, int(time())])
    next_state["history"] = rows[-MAX_SESSION_HISTORY:]
    _save_jury_state(next_state)


def sync_jury_form_inputs(
    *, goal: str, values: dict[str, str], skip_profile: bool, has_profile_fields: bool, force: bool = False,
) -> None:
    """Синхронизирует ручные правки цели/профиля на странице, если панель жюри активна в сессии или включён force."""
    state = get_jury_state()
    if not state and not force:
        return
    next_state = _ensure_jury_state()
    if skip_profile:
        next_state["goal"] = ""
        next_state["profile"] = {}
        next_state["preset"] = None
        next_state["history_active"] = True
        next_state["history"] = []
        _save_jury_state(next_state)
        return
    if has_profile_fields:
        next_state["goal"] = goal
        next_state["profile"] = profile_values(values)
        preset_id = next_state.get("preset")
        if preset_id in JURY_PRESETS:
            preset = JURY_PRESETS[preset_id]
            if goal.strip() != preset["goal"] or profile_values(values) != profile_values(preset["profile"]):
                next_state["preset"] = None
        _save_jury_state(next_state)


def apply_jury_action(form: Any, catalog: Sequence[Material], *, goal_max_length: int) -> tuple[bool, bool]:
    """Применяет действие из пульта жюри. Возвращает (sync_hero_fields, is_reset)."""
    action = form.get("lab_action", "")
    if action == "reset_all":
        session.pop(SESSION_KEY, None)
        return True, True
    if action == "sync_view":
        return False, False

    state = _ensure_jury_state()
    sync_hero = False

    if action == "set_mode":
        mode = form.get("mode", "")
        state["mode"] = mode if mode in RANKING_MODES else None
    elif action == "toggle_bm25_adaptive":
        flag = form.get("bm25_adaptive", "on")
        state["bm25_adaptive"] = flag == "on"
    elif action == "toggle_course_plus":
        setting = form.get("course_plus", "default")
        state["course_plus"] = True if setting == "on" else False if setting == "off" else None
    elif action == "apply_preset":
        preset_id = form.get("preset_id", "")
        preset = JURY_PRESETS.get(preset_id)
        if preset is None:
            return False, False
        state["preset"] = preset_id
        state["goal"] = str(preset["goal"])[:goal_max_length]
        state["profile"] = profile_values(preset["profile"])
        state["course_plus"] = bool(preset["course_plus"])
        state["history_active"] = True
        state["history"] = []
        base_ts = int(time()) - len(preset["history_specs"]) * 120
        used_ids: set[str] = set()
        for idx, (topic, kind, preferred_kind) in enumerate(preset["history_specs"]):
            picked = _pick_material_for_topic(catalog, topic, exclude_ids=used_ids, preferred_kind=preferred_kind)
            if picked is not None:
                used_ids.add(picked.id)
                state["history"].append([picked.id, kind, base_ts + idx * 60])
        sync_hero = True
    elif action == "add_event":
        spec = SIMULATION_EVENTS.get(form.get("sim_event", ""))
        if spec is None:
            return False, False
        _, topic, kind, preferred_kind = spec
        exclude_ids = {row[0] for row in state["history"] if row[1] == kind}
        picked = _pick_material_for_topic(catalog, topic, exclude_ids=exclude_ids, preferred_kind=preferred_kind)
        if picked is not None:
            state["history_active"] = True
            state["history"].append([picked.id, kind, int(time())])
            state["history"] = state["history"][-MAX_SESSION_HISTORY:]
            state["preset"] = None
    elif action == "remove_event":
        try:
            idx = int(form.get("event_index", "-1"))
        except ValueError:
            return False, False
        if 0 <= idx < len(state["history"]):
            state["history_active"] = True
            state["history"].pop(idx)
            state["preset"] = None
    elif action == "clear_history":
        state["history_active"] = True
        state["history"] = []
        state["preset"] = None
    elif action == "restore_live_history":
        state["history_active"] = False
        state["history"] = []
        state["preset"] = None

    _save_jury_state(state)
    return sync_hero, False


def _trace_payload(
    *,
    mode: str,
    bm25_weight: float,
    adaptive_bm25: bool,
    recommender_is_custom: bool,
    trace: dict[str, Any],
    profile: Profile,
    history: Sequence[InteractionEvent],
    has_course_plus: bool,
) -> dict[str, Any]:
    if mode == "popular":
        scenario_key = "popular_baseline"
    elif recommender_is_custom:
        scenario_key = "custom_recommender"
    else:
        scenario_key = trace.get("scenario") or ("history" if history else "profile_goal")

    constraints = list(trace.get("constraints") or ())
    bm25_applied = float(trace.get("bm25_applied") or 0.0)
    paid_target = trace.get("paid_target")
    top_signals = trace.get("top_signals") or []
    return {
        "mode": mode,
        "mode_label": RANKING_MODES.get(mode, mode),
        "scenario": scenario_key,
        "scenario_label": SCENARIO_LABELS.get(scenario_key, scenario_key),
        "bm25_base": round(bm25_weight, 2),
        "bm25_applied": round(bm25_applied, 2),
        "used_hybrid": bool(trace.get("used_hybrid")),
        "bm25_adaptive": bool(adaptive_bm25),
        "confirmed_topics": list(trace.get("confirmed_topics") or ()),
        "constraints": constraints,
        "paid_target": paid_target,
        "dedup_swapped": int(trace.get("dedup_swapped") or 0),
        "history_count": int(trace.get("history_count") if trace else len(history)),
        "course_plus": has_course_plus,
        "goal": profile.goal or "",
        "top_signals": top_signals,
    }


def build_jury_view_context(
    *,
    mode: str,
    mode_is_overridden: bool,
    bm25_weight: float,
    adaptive_bm25: bool,
    has_course_plus: bool,
    course_plus_overridden: bool,
    recommender_is_custom: bool,
    trace: dict[str, Any],
    profile: Profile,
    profile_values_map: dict[str, str],
    history: Sequence[InteractionEvent],
    materials_by_id: dict[str, Material],
    cards: Sequence[tuple[Material, Any]],
) -> dict[str, Any]:
    """Собирает данные для панели пульта жюри и телеметрии текущего ответа."""
    state = get_jury_state() or {}
    history_items: list[dict[str, Any]] = []
    # Показываем последние события от новых к старым, сохраняя индекс для удаления.
    for index in range(len(history) - 1, -1, -1):
        event = history[index]
        mat = materials_by_id.get(event.material_id)
        history_items.append({
            "index": index,
            "material_id": event.material_id,
            "title": mat.title if mat else event.material_id,
            "kind": event.kind,
            "kind_label": KIND_LABELS.get(event.kind, event.kind),
        })

    top_preview: list[dict[str, Any]] = []
    top_signals = trace.get("top_signals") or []
    for pos, (mat, _rec) in enumerate(cards[:3], start=1):
        signals = top_signals[pos - 1] if pos - 1 < len(top_signals) else (
            ["Популярный курс"] if mode == "popular" else []
        )
        top_preview.append({
            "pos": pos,
            "id": mat.id,
            "title": mat.title,
            "format": mat.format,
            "signals": signals,
        })

    telemetry = _trace_payload(
        mode=mode,
        bm25_weight=bm25_weight,
        adaptive_bm25=adaptive_bm25,
        recommender_is_custom=recommender_is_custom,
        trace=trace,
        profile=profile,
        history=history,
        has_course_plus=has_course_plus,
    )
    presets = [
        {
            "id": preset_id,
            "label": data["label"],
            "badge": data["badge"],
            "summary": data["summary"],
            "active": state.get("preset") == preset_id,
        }
        for preset_id, data in JURY_PRESETS.items()
    ]
    sim_buttons = [
        {"id": key, "label": spec[0], "kind": spec[2]}
        for key, spec in SIMULATION_EVENTS.items()
    ]
    profile_summary = [val for val in profile_values_map.values() if val]
    return {
        "active": bool(state),
        "mode": mode,
        "mode_label": RANKING_MODES.get(mode, mode),
        "mode_is_overridden": mode_is_overridden,
        "modes": tuple(RANKING_MODES.items()),
        "adaptive_bm25": adaptive_bm25,
        "course_plus": has_course_plus,
        "course_plus_overridden": course_plus_overridden,
        "active_preset": state.get("preset"),
        "presets": presets,
        "sim_buttons": sim_buttons,
        "history_simulated": bool(state.get("history_active")),
        "history_items": history_items,
        "history_count": len(history),
        "telemetry": telemetry,
        "top_preview": top_preview,
        "profile_summary": profile_summary,
        "state_json_payload": {
            "mode": mode,
            "mode_label": telemetry["mode_label"],
            "preset": state.get("preset"),
            "goal": profile.goal or "",
            "profile": profile_values_map,
            "course_plus": has_course_plus,
            "adaptive_bm25": adaptive_bm25,
            "history_count": len(history),
            "history_simulated": bool(state.get("history_active")),
            "top_ids": [mat.id for mat, _ in cards[:6]],
            "scenario_label": telemetry["scenario_label"],
        },
    }
