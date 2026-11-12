"""Ядро персональных рекомендаций образовательных материалов «Альфа-Курса»."""

import html
import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import lru_cache
from math import log1p, sqrt
from pathlib import Path

from app.catalog import catalog_data_dir
from app.catalog.subscriptions import access_category
from app.contracts import (
    InteractionEvent,
    Material,
    Profile,
    Recommendation,
    RecommendationResult,
)
from app.recommender.hybrid import (
    B, FIELD_WEIGHTS, K1, acceptable, cached_bm25, constraints as hybrid_constraints, material_text_terms,
    merge_orders, rare_query_terms,
)
from app.recommender.llm import refine_with_llm_if_configured
from app.recommender.onboarding import onboarding
from app.recommender.pacing import balance_first_page

HISTORY_HALF_LIFE_DAYS = 28
CONFIRMATION_WINDOW_DAYS = 14
SECONDS_PER_DAY = 86_400
HISTORY_EVENT_WEIGHTS = {"view": 0.08, "click": 1.0, "complete": 1.25}
UNCONFIRMED_TOPIC_WEIGHT = 0.45
TITLE_HISTORY_WEIGHT = 8.0
STRONG_TASK_SCORE = 19.0
ADAPTIVE_BM25_WEIGHT = 0.749
BEGINNER_GOAL = re.compile(r"\b(?:с нуля|я новичок|без опыта|нет опыта|ничего не знаю|только начинаю)\b")
PRACTICE_AFTER_THEORY = re.compile(
    r"\b(?:после теории|после прочтения|от теории к практике|перейти к практике|применить на практике|"
    r"(?:прочитал\w*|изучил\w*|разобрал\w*) .{0,90}\b(?:теперь|на практике))\b"
)
SESSION_GOAL_WORDS = frozenset((
    "шаг", "дальше", "начать", "план", "задача", "работа", "проверка", "система", "способ",
    "подход", "порядок", "проблема", "решение", "случай", "материал", "коллега", "команда",
    "неделя", "месяц", "квартал", "сбой", "рост", "этап", "результат", "процесс",
    "чем", "здесь", "меня", "может", "стоит", "заранее", "подготовиться", "сломаться",
    "следующий", "поможет", "понять", "меньше", "них", "усложнит", "ближайший", "составить", "почитать",
))

STOPWORDS = {
    "и", "в", "во", "на", "по", "с", "со", "к", "ко", "от", "до", "из", "за", "для", "про",
    "о", "об", "при", "без", "под", "над", "не", "ни", "но", "а", "или", "что", "как", "где",
    "когда", "почему", "зачем", "какой", "какая", "какие", "мне", "мой", "моя", "мои", "свой",
    "своего", "своей", "бизнес", "бизнеса", "бизнесу", "бизнесом", "дело", "малого", "малый",
    "хочу", "нужно", "надо", "только", "желательно", "можно", "чтобы", "это", "все", "еще",
    "найти", "сделать", "узнать", "получить", "работать", "выбрать", "считать", "посчитать",
    "первый", "первая", "первые", "первых", "первым", "первую", "один", "одна", "одной",
}

ACTION_VERB_STEMS = {
    "улучш", "повыс", "увелич", "сниз", "подня", "выраст", "расти", "налад",
    "исправ", "созда", "запуст", "откры", "выбра", "вест", "сдела", "найти",
    "хот", "получ", "узна", "посчита", "рассчита", "счита", "помоч", "провер",
}

SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    # 1. Трафик, интернет-маркетинг, клики, CTR, переходы, реклама
    (
        "продвижен", "маркетинг", "реклам", "привлечен", "трафик", "клик", "кликабельн",
        "ctr", "переход", "лид", "лидогенерац", "лиды", "директ", "контекст", "таргет",
        "смм", "smm", "сео", "seo", "охват", "посещаем", "просмотр", "креатив", "объявлен",
        "баннер", "визит", "канал", "блог", "контент"
    ),
    # 2. Продажи, конверсия, воронка, клиенты, возвращаемость
    (
        "продаж", "сбыт", "конверс", "cr", "воронк", "сделк", "переговор", "клиент",
        "покупател", "заказ", "заявк", "чек", "средн чек", "допродаж", "апсейл", "upsell",
        "кросс", "повторн", "возвращаем", "удержан", "retention", "лояльност", "баз клиент"
    ),
    # 3. Маркетплейсы, карточки товаров, e-commerce, селлеры
    (
        "маркетплейс", "wildberries", "вайлдберриз", "валдберис", "вб", "wb", "ozon", "озон",
        "яндекс маркет", "мегамаркет", "селлеp", "селлер", "карточк", "карточка товар", "товарн бизнес",
        "интернет магазин", "ecom", "онлайн продаж", "фбо", "фбс", "fbo", "fbs"
    ),
    # 4. Финансы, управленческий учет, ДДС (Cash Flow), кассовые разрывы, бюджет
    (
        "финанс", "учет", "управленк", "управленческ", "бухгалтер", "ддс", "cash flow",
        "денежн поток", "кассов разрыв", "касс", "платеж", "поступлен", "расход", "доход",
        "бюджет", "бюджетирован", "финмодел", "себестоимост", "остаток", "деньг", "фонд"
    ),
    # 5. Прибыль, маржа, рентабельность, ценообразование, окупаемость
    (
        "прибыл", "рентабел", "маржинальн", "марж", "наценк", "ценообразован", "цен",
        "скидк", "доходн", "окупаем", "точк безубыточн", "сравнен направлен", "ebitda", "экономик"
    ),
    # 6. Налоги, спецрежимы (УСН, Патент, АУСН, НПД), отчетность, ФНС
    (
        "налог", "налогообложен", "усн", "доход минус расход", "патент", "псн", "ндс",
        "ндфл", "самозанят", "нпд", "аусн", "фнс", "налогов", "декларац", "страхов взнос",
        "фиксирован взнос", "вычет", "штраф", "кудир", "книг учет", "отчетност"
    ),
    # 7. Открытие бизнеса, регистрация (ИП vs ООО), ниша, старт
    (
        "запуск", "старт", "открыт", "открыт бизнес", "открыт ип", "регистрац ип", "открыт ооо",
        "регистрац ооо", "начат", "ниша", "идея", "бизнес план", "оквэд", "перв шаг", "госпошлин"
    ),
    # 8. Операционка, процессы, планирование, рутина, регламенты
    (
        "операцион", "операцион работ", "планирован", "недел", "недельн план", "приоритет",
        "задач", "процесс", "бизнес процесс", "регламент", "инструкц", "эффективн", "тайм менеджмент",
        "график", "рутин", "автоматизац", "делегирован"
    ),
    # 9. Команда, персонал, найм, мотивация, KPI, зарплаты
    (
        "команд", "сотрудник", "найм", "наем", "персонал", "работник", "собеседован", "ваканс",
        "резюме", "мотивац", "kpi", "кпи", "зарплат", "оклад", "преми", "увольнен", "трудов договор",
        "кадр", "штат", "онбординг", "адаптац"
    ),
    # 10. Юриспруденция, договоры, безопасность, 115-ФЗ, проверки
    (
        "закон", "юрист", "прав", "договор", "оферт", "контракт", "115-фз", "финмониторинг",
        "блокировк счет", "проверк", "роспотребнадзор", "персданн", "152-фз", "товарн знак",
        "интеллектуальн собственност", "претенз", "суд", "ответственност"
    ),
    # 11. Розница, торговля, точка продаж, офлайн-магазин, склад
    (
        "розниц", "магазин", "торговл", "точк продаж", "офлайн", "шоурум", "витрин",
        "ритейл", "ретейл", "склад", "инвентаризац", "остатк", "мерчандайзинг", "аренд"
    ),
    # 12. Сфера услуг, клиентский сервис, B2B, обслуживание
    (
        "услуг", "сервис", "клиентск сервис", "качеств обслуживан", "клиентоориентированност",
        "консалтинг", "агентств", "салон", "мастер", "b2b"
    ),
    # 13. Общепит, рестораны, кафе, кофейни, HoReCa, доставка еды
    (
        "общепит", "кафе", "ресторан", "кофейн", "пекарн", "бар", "horeca", "хорек",
        "доставк ед", "меню", "фудкост", "техкарт", "санпин"
    ),
    # 14. Производство, поставщики, логистика, себестоимость товара
    (
        "производств", "цех", "поставщик", "закупк", "логистик", "доставк", "сырь",
        "брак", "контрол качеств", "упаковк", "кита"
    ),
    # 15. Нейросети, AI, автоматизация, CRM
    (
        "нейросет", "ии", "ai", "искусствен интеллект", "промпт", "chatgpt", "gpt",
        "автоматизац", "crm", "битрикс", "амосрм", "amocrm", "бот"
    ),
)

STAGE_ORDER = ("идея", "запуск", "работающий бизнес", "рост")

EXPERIENCE_ALIASES = {
    "начинающий": {"начинающий", "без опыта"},
    "без опыта": {"начинающий", "без опыта"},
    "есть опыт": {"есть опыт", "с опытом", "опытный"},
    "опытный": {"опытный", "с опытом", "есть опыт"},
    "с опытом": {"есть опыт", "опытный", "с опытом"},
}


@lru_cache(maxsize=4)
def _load_recommended_links(directory: Path) -> dict[str, list[str]]:
    links_path = directory / "recommended_links.json"
    try:
        links = json.loads(links_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return links if isinstance(links, dict) else {}


@lru_cache(maxsize=8192)
def _normalize_text(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", (text or "").casefold().replace("ё", "е")))


def _stem_ru(word: str) -> str:
    if word in ("выбрать", "выбора", "выбору", "выборе", "выбором"):
        return "выбор"
    if word in ("считать", "посчитать", "рассчитать", "расчет", "расчета", "расчетный"):
        return "расчет"
    if len(word) <= 3:
        return word
    for suffix in (
        "иями", "ями", "ами", "иях", "иям", "ием", "ией", "ого", "его", "ому", "ему",
        "ыми", "ими", "ать", "ять", "ить", "еть", "уть", "ость", "ости", "ение", "ание",
        "ения", "ания", "ений", "аний", "овка", "овки", "ый", "ий", "ой", "ая", "яя",
        "ое", "ее", "ые", "ие", "ов", "ев", "ей", "ам", "ям", "ах", "ях", "ом", "ем",
        "ую", "юю", "ью", "ия", "ья", "ии", "ьи", "а", "я", "о", "е", "ы", "и", "у", "ю",
    ):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


@lru_cache(maxsize=8192)
def _tokenize_stems(text: str) -> frozenset[str]:
    norm = _normalize_text(text)
    tokens = [w for w in norm.split() if w not in STOPWORDS and len(w) >= 2]
    return frozenset(_stem_ru(w) for w in tokens)


@lru_cache(maxsize=1)
def _stemmed_synonyms() -> tuple[tuple[tuple[str, ...], ...], ...]:
    return tuple(tuple(tuple(_stem_ru(word) for word in term.split()) for term in group)
                 for group in SYNONYM_GROUPS)


def _matches_synonym(word: str, synonym: str) -> bool:
    return word == synonym or (
        min(len(word), len(synonym)) >= 4 and (word.startswith(synonym) or synonym.startswith(word))
    )


def _expand_synonyms(stems: frozenset[str], raw_norm: str) -> set[str]:
    query_words = tuple(_stem_ru(word) for word in raw_norm.split())
    expanded = set()
    for group in _stemmed_synonyms():
        for term in group:
            if len(term) == 1:
                matched = any(_matches_synonym(word, term[0]) for word in stems)
            else:
                matched = any(
                    all(_matches_synonym(word, synonym) for word, synonym in zip(query_words[start:], term))
                    for start in range(len(query_words) - len(term) + 1)
                )
            if matched:
                expanded.update(word for phrase in group for word in phrase)
                break
    return expanded


def _parse_hard_constraints(goal: str | None) -> dict:
    """Извлекает жёсткие фильтры из текста задачи (бесплатно, бюджет до N, только шаблоны, ИП vs ООО)."""
    norm = _normalize_text(goal or "")
    words = set(norm.split())

    only_free = any(p in norm for p in ("только бесплатн", "бесплатн"))
    max_budget: int | None = None
    m_budget = re.search(r"до\s*(\d[\d\s]*)\s*(?:р|руб|₽)", (goal or "").casefold())
    if m_budget:
        digits = re.sub(r"\D", "", m_budget.group(1))
        if digits:
            max_budget = int(digits)
            if max_budget == 0:
                only_free = True

    only_format: str | None = None
    if any(p in norm for p in ("только шаблон", "нужен шаблон", "шаблон документ")):
        only_format = "шаблон"
    elif any(p in norm for p in ("только чек лист", "только чеклист")):
        only_format = "чек-лист"
    elif any(p in norm for p in ("только курс", "нужен курс")):
        only_format = "курс"
    elif any(p in norm for p in ("только стат", "нужна стат")):
        only_format = "статья"

    prefer_video = any(p in norm for p in ("видео", "вебинар", "посмотреть"))

    strict_ip = ("ип" in words) and ("ооо" not in words)
    strict_ooo = ("ооо" in words) and ("ип" not in words)

    return {
        "only_free": only_free,
        "max_budget": max_budget,
        "only_format": only_format,
        "prefer_video": prefer_video,
        "strict_ip": strict_ip,
        "strict_ooo": strict_ooo,
    }


def _passes_hard_filters(material: Material, constraints: dict) -> bool:
    if not material.id or not material.title.strip() or not material.url.strip():
        return False

    if constraints["only_free"] and access_category(material) != "free":
        return False

    max_budget = constraints["max_budget"]
    if max_budget is not None and access_category(material) != "free":
        cost = material.cost
        if cost is None or cost > max_budget:
            return False

    only_fmt = constraints["only_format"]
    if only_fmt:
        m_fmt = _normalize_text(material.format or "")
        if only_fmt.replace("-", " ") not in m_fmt:
            return False

    # Защита от противоречия ИП vs ООО
    title_words = set(_normalize_text(material.title).split())
    topic_words = set(_normalize_text(" ".join(material.topics)).split())
    all_mat_words = title_words | topic_words

    if constraints["strict_ip"]:
        if "ооо" in all_mat_words and "ип" not in all_mat_words:
            return False
    if constraints["strict_ooo"]:
        if "ип" in all_mat_words and "ооо" not in all_mat_words:
            return False

    return True


def _stage_score(material: Material, profile_stage: str | None) -> tuple[float, bool]:
    if not profile_stage:
        return 0.0, False
    p_stage = _normalize_text(profile_stage)
    m_stages = [_normalize_text(s) for s in material.stages if s]
    if not m_stages:
        return 0.5, False  # Универсальный материал выше несовпадения
    if p_stage in m_stages:
        return 8.0, True
    if p_stage in STAGE_ORDER:
        p_idx = STAGE_ORDER.index(p_stage)
        for ms in m_stages:
            if ms in STAGE_ORDER and abs(STAGE_ORDER.index(ms) - p_idx) == 1:
                return 2.0, False
    return -6.0, False


def _industry_score(material: Material, profile_industry: str | None) -> tuple[float, bool]:
    if not profile_industry or _normalize_text(profile_industry) == "другое":
        return 0.0, False
    p_ind = _normalize_text(profile_industry)
    m_inds = [_normalize_text(i) for i in material.industries if i]
    if not m_inds:
        return 0.4, False  # Универсальный материал выше явного несовпадения
    if any(p_ind in mi or mi in p_ind for mi in m_inds):
        return 6.0, True
    return -4.0, False


def _experience_score(material: Material, profile_exp: str | None) -> tuple[float, bool]:
    if not profile_exp:
        return 0.0, False
    p_exp = _normalize_text(profile_exp)
    accepted = EXPERIENCE_ALIASES.get(p_exp, {p_exp})
    m_exps = {_normalize_text(e) for e in material.experience_levels if e}
    if not m_exps:
        return 0.3, False
    if m_exps & accepted:
        return 5.0, True
    return -4.0, False


def _format_score(material: Material, preferred_format: str | None, prefer_video: bool) -> tuple[float, bool]:
    score = 0.0
    matched = False
    m_fmt = _normalize_text(material.format or "")
    if preferred_format and _normalize_text(preferred_format) not in ("", "любой формат"):
        p_fmt = _normalize_text(preferred_format)
        if p_fmt in m_fmt or m_fmt in p_fmt:
            score += 1.8
            matched = True
    if prefer_video and any(v in m_fmt for v in ("вебинар", "видео", "курс", "подкаст")):
        score += 1.0
        matched = True
    return score, matched


def _clean_plain_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<(?:br|/p|/div|/li|/tr|/h[1-6])[^>]*>", " ", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _build_card_explanation(
    material: Material,
    profile: Profile,
    signals: dict,
    is_cold_start: bool,
    is_weak_match: bool,
) -> tuple[str, str]:
    """Формирует понятные reason и next_step с опорой на реальные признаки материала и профиля."""
    fmt_label = (material.format or "материал").lower()
    reasons: list[str] = []

    if is_weak_match:
        topic_str = ", ".join(material.topics[:2]) if material.topics else "базовым бизнес-процессам"
        reasons.append(
            f"Точного материала по вашему узкому запросу в каталоге пока нет, поэтому предлагаем проверенный материал по теме «{topic_str}»"
        )
    elif is_cold_start:
        topic_str = ", ".join(material.topics[:2]) if material.topics else "старту и планированию"
        reasons.append(f"Отличный стартовый {fmt_label} по теме «{topic_str}», с которого удобно начать работу на платформе")
    else:
        if signals.get("matched_goal_topics"):
            t_list = ", ".join(signals["matched_goal_topics"][:2])
            reasons.append(f"Прямо отвечает на вашу задачу (темы: «{t_list}»)")
        elif signals.get("goal_direct"):
            clean_goal = (profile.goal or "").strip()
            if len(clean_goal.split()) <= 3 and not clean_goal.casefold().startswith("хочу"):
                reasons.append(f"Покрывает вопрос «{clean_goal}»")
            elif material.topics:
                reasons.append(f"Практический материал по теме «{', '.join(material.topics[:2])}» для вашей задачи")
            else:
                reasons.append("Прикладной материал, помогающий решить вашу задачу")
        elif signals.get("matched_learning_topic"):
            reasons.append(f"Соответствует выбранной теме обучения «{profile.learning_topic}»")

        profile_parts = []
        if signals.get("stage_matched") and profile.stage:
            profile_parts.append(f"стадии «{profile.stage}»")
        if signals.get("industry_matched") and profile.industry:
            profile_parts.append(f"сферы «{profile.industry}»")
        if signals.get("exp_matched") and profile.experience:
            profile_parts.append(f"уровня «{profile.experience}»")
        if profile_parts:
            reasons.append(f"адаптирован для {', '.join(profile_parts)}")

        if signals.get("history_continuation_from"):
            prev_title = signals["history_continuation_from"]
            reasons.append(f"логичное практическое продолжение после изучения «{prev_title}»")

    clean_desc = _clean_plain_text(material.description)
    if material.access == "paid":
        short_benefit = (clean_desc or "Системная программа с разбором на практике.").rstrip(".")
        if len(short_benefit) > 130:
            short_benefit = short_benefit[:127].rstrip(" ,.;:") + "..."
        reasons.append(f"Платный продукт: {short_benefit}")
    elif not reasons and clean_desc:
        short_desc = clean_desc.rstrip(".")
        if len(short_desc) > 140:
            short_desc = short_desc[:137].rstrip(" ,.;:") + "..."
        reasons.append(short_desc)

    reason_text = ". ".join(r[0].upper() + r[1:] for r in (reasons if reasons else ["Подходит под ваш профиль предпринимателя"])).rstrip(".") + "."

    # Формирование next_step
    if material.access == "paid":
        next_step = "Перейдите на страницу курса по ссылке в карточке, чтобы ознакомиться с программой и условиями доступа."
    elif "шаблон" in fmt_label:
        next_step = "Откройте шаблон по ссылке, скачайте документ и подставьте показатели своего бизнеса."
    elif "чек-лист" in fmt_label:
        next_step = "Пройдите по пунктам чек-листа и отметьте 1–2 задачи, которые внедрите на этой неделе."
    elif "калькулятор" in fmt_label or "расчет" in _normalize_text(material.title):
        next_step = "Откройте калькулятор по ссылке, введите свои вводные данные и сохраните расчёт."
    elif "курс" in fmt_label:
        next_step = "Откройте бесплатный курс по ссылке и пройдите первый вводный урок."
    elif "вебинар" in fmt_label or "подкаст" in fmt_label:
        next_step = "Перейдите по ссылке к записи и выпишите ключевые приёмы для внедрения."
    else:
        next_step = "Прочитайте материал за 10–15 минут и перейдите к практическому шаблону из подборки."

    return reason_text, next_step


def _build_follow_up_question(
    profile: Profile,
    has_history: bool,
    is_weak_match: bool,
    is_cold_start: bool,
) -> str | None:
    if is_weak_match:
        return (
            "По вашему запросу в каталоге нет точного совпадения. "
            "Уточните, какую прикладную задачу сейчас важнее закрыть: "
            "налоги и отчётность, договорную базу, привлечение клиентов или финансовый учёт?"
        )
    if is_cold_start:
        return (
            "Чтобы сделать подборку точнее, уточните: вы только планируете запуск бизнеса "
            "или уже работаете и хотите увеличить продажи / навести порядок в учёте?"
        )
    goal_norm = _normalize_text(profile.goal or "")
    if goal_norm and not profile.stage and not profile.industry and not has_history:
        if any(k in goal_norm for k in ("продвижен", "маркетинг", "продаж", "клиент")):
            return "Уточните: вам сейчас важнее привлечь первых клиентов на старте или выстроить повторные продажи в действующем бизнесе?"
        if any(k in goal_norm for k in ("финанс", "учет", "налог")):
            return "Уточните: вы ищете базовую таблицу учёта денежных потоков для старта или расчёт рентабельности направлений?"
    return None


def _timestamp(value: datetime) -> float:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()


@lru_cache(maxsize=12)
def _affinity_index(records):
    titles = {mid: _tokenize_stems(title) - ACTION_VERB_STEMS for mid, title, _ in records}
    topics = {mid: frozenset(filter(None, map(_normalize_text, tags))) for mid, _, tags in records}
    title_counts = Counter(term for terms in titles.values() for term in terms if not term.isdigit())
    topic_counts = Counter(topic for tags in topics.values() for topic in tags)
    count = len(records)
    return (
        titles, topics,
        {term: log1p(count / freq) for term, freq in title_counts.items()},
        {topic: log1p(count / freq) for topic, freq in topic_counts.items()},
    )


def _affinity_scores(materials, clicks, seen, links, *, browse_from_seed):
    if len(clicks) < 2 and not browse_from_seed:
        return {}, ()
    index = tuple((m.id, m.title, m.topics) for m in materials.values())
    titles, topics, title_weights, topic_weights = _affinity_index(index)
    small_catalog = len(materials) <= 20

    def similarity(source, candidate):
        shared_terms = sum(title_weights.get(t, 0.) for t in titles[source] & titles[candidate])
        shared_topics = sum(topic_weights[t] for t in topics[source] & topics[candidate]
                            if topic_weights[t] >= 2.23 or small_catalog)
        return (shared_terms + .7 * shared_topics) / sqrt(4 + len(titles[candidate]))

    eligible = materials.keys() - {mid for mid, kind in seen.items() if kind in {"click", "complete"}}
    if browse_from_seed:
        seed = clicks[0]
        neighbors = links.get(seed, ())
        through_neighbors = defaultdict(float)
        for mid in neighbors:
            adjacent = links.get(mid, ())
            for candidate in adjacent:
                through_neighbors[candidate] += 1 / sqrt(len(adjacent))
        direct = set(neighbors)
        return {
            mid: similarity(seed, mid)
                 + 2.0 * through_neighbors[mid] / sqrt(max(1, len(links.get(mid, ()))))
                 + (0.6 if mid in direct else 0.)
            for mid in eligible
        }, (seed,)

    def coherent(left, right):
        if small_catalog:
            return bool(topics[left] & topics[right] or titles[left] & titles[right])
        return (any(title_weights.get(t, 0.) >= 3 for t in titles[left] & titles[right])
                or any(topic_weights[t] >= 2.35 for t in topics[left] & topics[right]))

    # При одиночном выбросе остаётся предыдущая подтверждённая пара.
    for left, right in zip(clicks[:4], clicks[1:5]):
        if coherent(left, right):
            scores = {}
            for mid in eligible:
                a, b = similarity(left, mid), similarity(right, mid)
                score = min(a, b) + .7 * max(a, b)
                if score >= .6:
                    scores[mid] = score
            return scores, (left, right)
    return {}, ()


def _explore_after_seed(scored, seed):
    remaining = [row for row in scored if row[1].id != seed.id]
    if len(remaining) < 3:
        return scored

    def material_type(material):
        return material.entity_type or material.format

    first_two = [row[1] for row in remaining[:2]]
    formats = {material_type(material) for material in first_two}
    clicked_topics = set(map(_normalize_text, seed.topics))

    def has_new_topic(material):
        return bool(set(map(_normalize_text, material.topics)) - clicked_topics)

    for index, (_, material, _) in enumerate(remaining[2:], 2):
        varied_format = len(formats) >= 2 or material_type(material) not in formats
        varied_topic = any(map(has_new_topic, first_two)) or has_new_topic(material)
        if varied_format and varied_topic:
            remaining.insert(2, remaining.pop(index))
            break
    return remaining + [row for row in scored if row[1].id == seed.id]


def _history_signals(history, materials, links, as_of):
    """Два разных материала подтверждают тему. Повторы и открытия превью не суммируются."""
    latest = {}
    timestamps = []
    for index, event in enumerate(history):
        when = _timestamp(event.occurred_at) if event.occurred_at else None
        if when is not None:
            timestamps.append(when)
        key = HISTORY_EVENT_WEIGHTS[event.kind], when if when is not None else float("-inf"), index
        previous = latest.get(event.material_id)
        if previous is None or key > previous[0]:
            latest[event.material_id] = (key, event, when)

    reference = _timestamp(as_of) if as_of else max(timestamps, default=None)
    ordered = sorted(latest.values(), key=lambda row: row[0][2])
    topics, industries, title_terms = defaultdict(list), defaultdict(list), defaultdict(list)
    signals, recent_clicks = [], []
    for offset, (_, event, when) in enumerate(reversed(ordered)):
        if when is not None and reference is not None:
            age = max(0., (reference - when) / SECONDS_PER_DAY)
            decay = 2 ** (-age / HISTORY_HALF_LIFE_DAYS)
            recent = age <= CONFIRMATION_WINDOW_DAYS
        else:
            decay = 1.0 if offset < 3 else 0.4 if offset < 10 else 0.1
            recent = offset < 10
        source = materials[event.material_id]
        source_topics = set(filter(None, map(_normalize_text, source.topics)))
        source_industries = set(filter(None, map(_normalize_text, source.industries)))
        weight = HISTORY_EVENT_WEIGHTS[event.kind] * decay
        confirmed = 2 if event.kind == "complete" else int(event.kind == "click")
        evidence = (weight, confirmed if recent else 0, confirmed, source.title)
        for topic in source_topics:
            topics[topic].append(evidence)
        for industry in source_industries:
            industries[industry].append(evidence)
        if confirmed:
            signals.append((event.material_id, source_topics, source.title, decay, recent and confirmed == 2))
            if recent:
                if event.kind == "click":
                    recent_clicks.append(event.material_id)
                for term in _tokenize_stems(source.title) - ACTION_VERB_STEMS:
                    if not term.isdigit():
                        title_terms[term].append((len(signals) - 1, decay))

    def strength(evidence):
        total = sum(row[0] for row in evidence)
        if sum(row[1] for row in evidence) >= 2:
            return min(total, 3.0) * 6.0
        if sum(row[2] for row in evidence) >= 2:
            return min(total, 3.0) * 1.3
        return min(total, 1.0) * UNCONFIRMED_TOPIC_WEIGHT

    topic_scores = {topic: (strength(rows), max(rows, key=lambda row: (row[1] > 0, row[0]))[3])
                    for topic, rows in topics.items()}
    industry_scores = {industry: strength(rows) * 0.22 for industry, rows in industries.items()}
    term_scores = {
        term: TITLE_HISTORY_WEIGHT * min(2.0, sum(decay if position < 3 else decay * 0.4 for position, decay in rows))
        for term, rows in title_terms.items() if len(rows) >= 2
    }
    linked_scores = {}
    for mid, source_topics, title, decay, completed in signals:
        confirmed = completed or any(topic_scores[topic][0] >= 6.0 for topic in source_topics)
        boost = (2.8 if confirmed else 0.35) * decay
        for linked in links.get(mid, ()):
            if boost > linked_scores.get(linked, (0., ""))[0]:
                linked_scores[linked] = (boost, title)
    seen = {mid: row[1].kind for mid, row in latest.items()}
    return topic_scores, industry_scores, term_scores, linked_scores, seen, recent_clicks


def _is_beginner(profile: Profile) -> bool:
    return _normalize_text(profile.stage or "") in {"идея", "запуск"} or (
        _normalize_text(profile.experience or "") in EXPERIENCE_ALIASES["начинающий"]
    )


def _adaptive_bm25_weight(base, profile, goal_norm, goal_clauses, recent_clicks, catalog_by_id, top_text, catalog):
    if _is_beginner(profile) or BEGINNER_GOAL.search(goal_norm) or PRACTICE_AFTER_THEORY.search(goal_norm):
        return base
    theory_reads = sum(catalog_by_id[mid].entity_type in {"article", "lesson"} for mid in recent_clicks[:3])
    if theory_reads >= 2:
        return base
    rare = rare_query_terms(tuple(catalog))
    for material in top_text:
        title_terms = _tokenize_stems(material.title)
        headings, text_terms = material_text_terms(material.title, material.description, material.topics, material.search_text)
        for terms in goal_clauses:
            title_hits = len(terms & title_terms)
            if title_hits >= 2 and title_hits * 3 >= len(terms) * 2:
                return max(base, ADAPTIVE_BM25_WEIGHT)
            matches = terms & text_terms
            rare_matches = matches & rare
            if len(matches) >= 3 and len(matches) * 2 >= len(terms) and (
                len(rare_matches) >= 3 or (len(rare_matches) >= 2 and matches & headings)
            ):
                return max(base, ADAPTIVE_BM25_WEIGHT)
    return base


def _is_tool_material(mat: Material) -> bool:
    fmt = _normalize_text(mat.format or "")
    return mat.entity_type in {"document", "calculator"} or fmt in {"шаблон", "чек-лист", "калькулятор", "документ"}


def _are_near_duplicates(left: Material, right: Material) -> bool:
    if _is_tool_material(left) != _is_tool_material(right):
        return False
    t_left = _tokenize_stems(left.title) - ACTION_VERB_STEMS
    t_right = _tokenize_stems(right.title) - ACTION_VERB_STEMS
    t_union = t_left | t_right
    t_shared = t_left & t_right
    jaccard_title = (len(t_shared) / len(t_union)) if t_union else 0.0
    if len(t_shared) >= 2 and jaccard_title >= 0.50:
        return True
    if not left.description.strip() or not right.description.strip():
        return False
    d_left = _tokenize_stems(f"{left.title} {left.description}") - ACTION_VERB_STEMS
    d_right = _tokenize_stems(f"{right.title} {right.description}") - ACTION_VERB_STEMS
    d_union = d_left | d_right
    jaccard_desc = (len(d_left & d_right) / len(d_union)) if d_union else 0.0
    return jaccard_desc >= 0.45 or (jaccard_title >= 0.35 and jaccard_desc >= 0.25)


def _deduplicate_top(ordered: list[tuple[float, Material, dict]], limit: int) -> list[tuple[float, Material, dict]]:
    window = min(limit, 3)
    if window < 2 or len(ordered) <= 1:
        return ordered
    chosen: list[tuple[float, Material, dict]] = []
    deferred: list[tuple[float, Material, dict]] = []
    for row in ordered:
        if len(chosen) >= window:
            deferred.append(row)
            continue
        mat, sig = row[1], row[2]
        dup_idx = next((i for i, prev in enumerate(chosen) if _are_near_duplicates(prev[1], mat)), None)
        if dup_idx is None:
            chosen.append(row)
        elif sig.get("fmt_matched") and not chosen[dup_idx][2].get("fmt_matched"):
            deferred.append(chosen[dup_idx])
            chosen[dup_idx] = row
        else:
            deferred.append(row)
    return chosen + deferred


def recommend(
    catalog: Sequence[Material],
    profile: Profile,
    history: Sequence[InteractionEvent],
    limit: int,
    *,
    explain_with_llm: bool = True,
    links_directory: Path | None = None,
    use_expert_links: bool = True,
    expert_links: dict[str, list[str]] | None = None,
    history_as_of: datetime | None = None,
    balance_access: bool = False,
    bm25_weight: float = 0.0,
    bm25_field_weights: tuple[float, float, float] = FIELD_WEIGHTS,
    bm25_length_penalty: float = B,
    bm25_saturation: float = K1,
    bm25_adaptive: bool = False,
) -> RecommendationResult:
    """Ранжирует материалы каталога с учётом задачи, профиля, истории и популярности."""
    if not 0 <= bm25_weight <= 1:
        raise ValueError("bm25_weight must be between 0 and 1")
    if not catalog or limit <= 0:
        return RecommendationResult((), "")

    rec_links = expert_links if expert_links is not None else (
        _load_recommended_links(links_directory or catalog_data_dir()) if use_expert_links else {}
    )
    constraints = _parse_hard_constraints(profile.goal)

    # 1. Фильтрация и дедупликация
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    candidates: list[tuple[int, Material]] = []
    for idx, mat in enumerate(catalog):
        if mat.id in seen_ids or mat.url in seen_urls:
            continue
        if not _passes_hard_filters(mat, constraints):
            continue
        seen_ids.add(mat.id)
        seen_urls.add(mat.url)
        candidates.append((idx, mat))

    # При пустой выдаче можно предложить другой формат, сохранив заявленные условия оплаты.
    if not candidates and not constraints["only_free"] and constraints["max_budget"] is None:
        for idx, mat in enumerate(catalog):
            if mat.id not in seen_ids and mat.id and mat.title.strip() and mat.url.strip():
                seen_ids.add(mat.id)
                candidates.append((idx, mat))
    if not candidates:
        return RecommendationResult((), "")

    catalog_by_id = {mat.id: mat for mat in catalog}

    # 2. Разбор сигналов истории (только по существующим в каталоге ID)
    valid_history = [ev for ev in history if ev.material_id in catalog_by_id]
    if not valid_history and (starter := onboarding(catalog, profile, limit)) is not None:
        return starter
    topic_scores, industry_scores, term_scores, linked_scores, seen_events, recent_clicks = _history_signals(
        valid_history, catalog_by_id, rec_links, history_as_of,
    )

    # 3. Разбор текстовой задачи (goal) и темы из профиля (learning_topic)
    goal_raw = (profile.goal or "").strip()
    goal_norm = _normalize_text(goal_raw)
    goal_stems = _tokenize_stems(goal_raw)
    goal_expanded_stems = _expand_synonyms(goal_stems, goal_norm) if goal_stems else set()

    learning_topic_raw = (getattr(profile, "learning_topic", None) or "").strip()
    learning_topic_norm = _normalize_text(learning_topic_raw)
    if learning_topic_norm == "любая тема":
        learning_topic_norm = ""
    learning_topic_stems = _tokenize_stems(learning_topic_raw) if learning_topic_norm else set()

    has_profile_params = any(
        bool(val and _normalize_text(val) not in ("", "любая тема", "любой формат", "другое"))
        for val in (
            profile.stage,
            profile.industry,
            profile.experience,
            getattr(profile, "team_size", None),
            getattr(profile, "preferred_format", None),
        )
    )
    is_cold_start = not goal_norm and not learning_topic_norm and not valid_history and not has_profile_params
    browse_from_seed = (
        len(recent_clicks) == 1 and not goal_norm and not learning_topic_norm and not has_profile_params
        and sum(kind in {"click", "complete"} for kind in seen_events.values()) == 1
    )
    affinity, affinity_sources = _affinity_scores(
        catalog_by_id, recent_clicks, seen_events, rec_links, browse_from_seed=browse_from_seed,
    )
    if len(affinity_sources) == 2 and recent_clicks[0] not in affinity_sources:
        # Ещё один клик вне подтверждённой пары остаётся в журнале, но не смещает подборку.
        established_history = [event for event in valid_history if event.material_id != recent_clicks[0]]
        topic_scores, industry_scores, term_scores, linked_scores, _, _ = _history_signals(
            established_history, catalog_by_id, rec_links, history_as_of,
        )
    generic_goal_terms = ACTION_VERB_STEMS | _tokenize_stems(" ".join(SESSION_GOAL_WORDS))
    goal_details = goal_stems - generic_goal_terms
    last_clause = re.split(r"[.!?]+\s*", goal_raw.rstrip(".!?"))[-1]
    exploratory_goal = len(_tokenize_stems(last_clause) - generic_goal_terms) <= 1
    short_goal = len(goal_norm.split()) <= 4
    has_concrete_goal = False

    scored_items: list[tuple[float, Material, dict]] = []
    max_task_score = 0.0

    for idx, mat in candidates:
        title_norm = _normalize_text(mat.title)
        desc_norm = _normalize_text(mat.description)
        topics_norm_map = {_normalize_text(t): t for t in mat.topics if _normalize_text(t)}
        title_stems = _tokenize_stems(mat.title)
        if len(affinity_sources) == 2 and goal_details and not has_concrete_goal:
            shared = len(goal_details & title_stems)
            has_concrete_goal |= (
                (short_goal and goal_details <= title_stems)
                or (shared >= 2 and shared * 3 >= len(goal_details) * 2)
            )
        desc_stems = _tokenize_stems(mat.description)
        search_stems = _tokenize_stems(mat.search_text) if mat.search_text else frozenset()
        topic_stems = _tokenize_stems(" ".join(mat.topics))
        ind_stems = _tokenize_stems(" ".join(mat.industries))
        all_mat_stems = title_stems | desc_stems | topic_stems | ind_stems

        signals: dict = {
            "matched_goal_topics": [],
            "goal_direct": False,
            "matched_learning_topic": False,
            "stage_matched": False,
            "industry_matched": False,
            "exp_matched": False,
            "fmt_matched": False,
            "history_continuation_from": None,
        }

        # А. Оценка соответствия главной задаче (goal)
        task_score = 0.0
        if goal_norm:
            # Точное вхождение темы в текст запроса или наоборот
            for t_norm, t_orig in topics_norm_map.items():
                if t_norm in goal_norm or goal_norm in t_norm:
                    task_score += 4.5
                    signals["matched_goal_topics"].append(t_orig)

            # Пересечение стеммов: заголовок важнее общих тегов, но слово сферы не дублируется
            non_ind_goal_stems = goal_stems - ind_stems
            title_hits = len(non_ind_goal_stems & title_stems)
            topic_hits = len((goal_stems - title_stems) & (topic_stems | ind_stems))
            desc_hits = len((goal_stems - title_stems - topic_stems) & desc_stems)

            # Проверяем, есть ли совпадение по содержательным сущностям (не чисто глаголам действия)
            content_goal_stems = {s for s in goal_stems if s not in ACTION_VERB_STEMS}
            content_title_hits = len(content_goal_stems & title_stems)
            content_topic_hits = len(content_goal_stems & (topic_stems | ind_stems))
            content_desc_hits = len(content_goal_stems & desc_stems)

            if content_title_hits or content_topic_hits or content_desc_hits or not content_goal_stems:
                base_hits = len(goal_stems & (title_stems | topic_stems | ind_stems))
                task_score += base_hits * 4.5 + title_hits * 4.0 + (content_desc_hits * 2.5 + (desc_hits - content_desc_hits) * 0.4)
                if content_title_hits or content_topic_hits or (content_desc_hits >= 2) or (not content_goal_stems and title_hits):
                    signals["goal_direct"] = True
            elif title_hits:
                task_score += title_hits * 1.5
                if not signals["matched_goal_topics"]:
                    for t_norm, t_orig in topics_norm_map.items():
                        if _tokenize_stems(t_norm) & goal_stems:
                            signals["matched_goal_topics"].append(t_orig)

            if search_stems:
                extra_search_hits = len((content_goal_stems - title_stems - topic_stems - desc_stems) & search_stems)
                if extra_search_hits:
                    task_score += min(extra_search_hits * 1.8, 5.4)

            # Синонимическое расширение (полезный шаг по широкой/смежной теме)
            unmatched_syn_stems = goal_expanded_stems - goal_stems
            syn_primary_hits = len(unmatched_syn_stems & (title_stems | topic_stems | ind_stems))
            syn_desc_hits = len((unmatched_syn_stems - title_stems - topic_stems) & desc_stems)
            if syn_primary_hits or syn_desc_hits:
                task_score += min(syn_primary_hits * 2.5 + syn_desc_hits * 1.2, 6.0)
                if not signals["matched_goal_topics"]:
                    for t_norm, t_orig in topics_norm_map.items():
                        if _tokenize_stems(t_norm) & goal_expanded_stems:
                            signals["matched_goal_topics"].append(t_orig)

        # Б. Сигнал темы обучения из профиля (если задана)
        learning_score = 0.0
        if learning_topic_norm:
            if learning_topic_norm in topics_norm_map or any(
                learning_topic_norm in tn or tn in learning_topic_norm for tn in topics_norm_map
            ):
                learning_score += 6.0 if not goal_norm else 2.5
                signals["matched_learning_topic"] = True
            elif learning_topic_stems & all_mat_stems:
                learning_score += 3.5 if not goal_norm else 1.5
                signals["matched_learning_topic"] = True

        # В. Сигнал параметров профиля (стадия -> сфера -> опыт -> команда -> формат)
        st_score, st_matched = _stage_score(mat, profile.stage)
        ind_score, ind_matched = _industry_score(mat, profile.industry)
        exp_score, exp_matched = _experience_score(mat, profile.experience)
        fmt_score, fmt_matched = _format_score(
            mat, getattr(profile, "preferred_format", None), constraints["prefer_video"]
        )
        signals["stage_matched"] = st_matched
        signals["industry_matched"] = ind_matched
        signals["exp_matched"] = exp_matched
        signals["experience_score"] = exp_score
        signals["fmt_matched"] = fmt_matched

        team_score = 0.0
        team_size = _normalize_text(getattr(profile, "team_size", None) or "")
        if team_size:
            if "один" in team_size and any(k in title_norm or k in desc_norm for k in ("самозанят", "соло", "ип", "одиночк", "приоритет")):
                team_score += 1.0
            elif any(k in team_size for k in ("2", "5", "6", "15", "16")) and any(
                k in title_norm or k in " ".join(topics_norm_map) for k in ("команд", "сотрудник", "найм", "наем", "мотивац", "зарплат")
            ):
                team_score += 1.2

        # Г. Сигнал истории действий
        hist_score = 0.0
        history_title_score = 0.0
        if valid_history:
            seen = seen_events.get(mat.id)
            if seen == "complete":
                hist_score -= 15.0
            else:
                topic_score, topic_source = max((topic_scores[t] for t in topics_norm_map if t in topic_scores), default=(0., ""))
                industry_score = max((industry_scores.get(_normalize_text(ind), 0.) for ind in mat.industries), default=0.)
                graph_score, graph_source = linked_scores.get(mat.id, (0., ""))
                if seen != "click":
                    history_title_score = max((term_scores.get(term, 0.) for term in title_stems), default=0.)
                hist_score = topic_score + industry_score + history_title_score + graph_score
                if browse_from_seed:
                    hist_score += 8.0 * affinity.get(mat.id, 0.)
                if seen == "click":
                    hist_score -= 1.2
                elif topic_score >= 2 or graph_score >= 2:
                    signals["history_continuation_from"] = graph_source or topic_source

        # Д. Холодный старт и практическая применимость
        cold_score = 0.0
        fmt_norm = _normalize_text(mat.format or "")
        if is_cold_start:
            if mat.access == "free":
                cold_score += 4.0
            elif mat.access == "unknown":
                cold_score += 2.0
            else:
                cold_score -= 3.0
            if any(t in topics_norm_map for t in ("запуск бизнеса", "планирование", "операционная работа")):
                cold_score += 3.5
            if "чек-лист" in fmt_norm or "шаблон" in fmt_norm or "статья" in fmt_norm:
                cold_score += 2.0
            if "запуск" in [_normalize_text(s) for s in mat.stages]:
                cold_score += 1.5
        elif not profile.stage and not profile.experience and not valid_history:
            # Новый пользователь указал только задачу (например, «продвижение бизнеса»):
            # вводные бесплатные материалы для старта выше дорогих продвинутых курсов
            if mat.access == "free":
                cold_score += 2.2
            if "запуск" in [_normalize_text(s) for s in mat.stages] or "начинающий" in [_normalize_text(e) for e in mat.experience_levels]:
                cold_score += 1.8

        # Практические форматы и фильтрация утилитарных шаблонов
        actionable_bonus = 0.0
        is_template_mat = "шаблон" in fmt_norm or "документ" in fmt_norm or mat.format in ("шаблон", "документ")
        is_calc_mat = mat.entity_type == "calculator"
        is_doc_mat = mat.entity_type == "document"
        is_calc_requested = any(
            w in goal_norm
            for w in (
                "калькулятор", "рассчита", "посчита", "подсчита", "вычисл", "расчёт", "расчет",
                "пересчита", "перевести одно в другое", "без ндс", "с налогом и без",
                "выделить налог", "на руки", "фонд оплаты", "до вычета", "налоговая разбивк",
                "марж", "рентабельност", "безубыточност", "окупаемост",
            )
        )
        is_doc_requested = any(
            w in goal_norm
            for w in (
                "шаблон", "документ", "бланк", "форма", "акт", "журнал", "образец", "договор",
                "скачать", "таблиц", "файл", "заявлен", "приказ", "устав", "кудир",
                "декларац", "ордер", "чек-лист", "чеклист", "памятк",
            )
        ) or bool(profile.preferred_format and "шаблон" in _normalize_text(profile.preferred_format))
        theory_to_practice = bool(PRACTICE_AFTER_THEORY.search(goal_norm))
        practical_cue = any(
            w in goal_norm
            for w in ("что взять", "что заполн", "что оформ", "как вести", "как сверить", "как оформит", "как зафиксир", "какой документ", "какая форма")
        )
        theory_reads_count = sum(catalog_by_id[mid].entity_type in {"article", "lesson"} for mid in recent_clicks[:3])

        if task_score > 0 and ("шаблон" in fmt_norm or "чек-лист" in fmt_norm):
            if signals["goal_direct"] or st_matched or ind_matched or signals["history_continuation_from"]:
                actionable_bonus += 1.2

        if is_calc_requested and is_calc_mat:
            actionable_bonus += 18.0 if task_score > 0 else 4.0
        elif is_doc_requested and is_doc_mat:
            actionable_bonus += 5.0 if task_score >= 4.0 else 2.5
        elif is_doc_requested and is_template_mat:
            actionable_bonus += 3.5
        elif (theory_to_practice or (theory_reads_count >= 2 and practical_cue)) and (is_doc_mat or is_calc_mat) and task_score > 0:
            actionable_bonus += 4.5
        elif is_template_mat and not is_doc_requested and not is_calc_requested and not theory_to_practice and not (theory_reads_count >= 2 and practical_cue) and not signals["history_continuation_from"] and hist_score < 2:
            # Понижаем утилитарные документы при общих запросах на обучение/рост
            actionable_bonus -= 2.8

        # Популярность как тай-брейкер при близкой полезности
        pop_raw = mat.popularity or 0.0
        pop_bonus = min(pop_raw / 2500.0, 1.5) + max(0.0, (1200 - idx) / 12000.0)

        max_task_score = max(max_task_score, task_score)
        total_score = (
            task_score * 3.0
            + learning_score * 2.2
            + st_score
            + ind_score
            + exp_score
            + team_score
            + fmt_score
            + hist_score
            + cold_score
            + actionable_bonus
            + pop_bonus
        )
        signals["task_score"] = task_score
        signals["history_title_score"] = history_title_score
        scored_items.append((total_score, mat, signals))

    is_weak_match = bool(goal_norm and max_task_score < 2.5)

    # При наличии явной задачи и хороших совпадений отбрасываем совсем нерелевантные материалы,
    # если хватает релевантных кандидатов
    # Недавняя подтверждённая тема получает отдельные места при расплывчатом запросе.
    # Кандидаты уже прошли ограничения формата, цены и доступа.
    context_items = []
    hard_goal = constraints["max_budget"] is not None or any(
        constraints[key] for key in ("only_free", "only_format", "strict_ip", "strict_ooo")
    )
    if len(affinity_sources) == 2 and exploratory_goal and not has_concrete_goal and not hard_goal:
        ranked_context = sorted(
            (row for row in scored_items
             if row[1].id in affinity and row[2]["experience_score"] >= 0),
            key=lambda row: (
                -(affinity[row[1].id] + .1 * row[2]["experience_score"]
                  + .2 * row[2]["fmt_matched"]), -row[0],
            ),
        )
        if len(ranked_context) >= 2:
            context_items = ranked_context[:min(2, limit)]

    if goal_norm and not is_weak_match:
        relevant_pool = [item for item in scored_items if item[2]["task_score"] > 0
                         or (max_task_score < STRONG_TASK_SCORE and item[2]["history_title_score"] >= TITLE_HISTORY_WEIGHT)]
        if len(relevant_pool) >= min(limit, 2):
            scored_items = relevant_pool

    scored_items.sort(key=lambda x: x[0], reverse=True)
    if browse_from_seed and limit >= 3:
        scored_items = _explore_after_seed(scored_items, catalog_by_id[affinity_sources[0]])

    # Выбираем топ-limit материалов
    selected_ids = {mat.id for _, mat, _ in context_items}
    ordered = context_items + [row for row in scored_items if row[1].id not in selected_ids]
    used_hybrid = False
    if bm25_weight and goal_norm and not is_weak_match and not context_items:
        filters = hybrid_constraints(goal_raw)
        rankable = {row[1].id: row for row in ordered if acceptable(row[1], filters)}
        core_ids = tuple(rankable)
        text_order = cached_bm25(catalog, field_weights=bm25_field_weights, length_penalty=bm25_length_penalty,
                                 saturation=bm25_saturation)
        text_ids = tuple(mid for mid in text_order(goal_raw) if mid in rankable)
        if text_ids:
            if bm25_adaptive:
                goal_clauses = tuple(_tokenize_stems(clause) - generic_goal_terms
                                     for clause in re.split(r"[.!?;]+\s*", goal_raw))
                bm25_weight = _adaptive_bm25_weight(
                    bm25_weight, profile, goal_norm, goal_clauses,
                    recent_clicks, catalog_by_id, (catalog_by_id[mid] for mid in text_ids[:5]), catalog,
                )
            ranked_ids = merge_orders((core_ids, text_ids), weights=(1 - bm25_weight, bm25_weight))
            fused_ids = set(ranked_ids)
            replacement = iter(ranked_ids + [mid for mid in core_ids if mid not in fused_ids])
            ordered = [rankable[next(replacement)] if row[1].id in rankable else row for row in ordered]
            used_hybrid = True
    if balance_access and not browse_from_seed and not is_weak_match and not any(
        (constraints["only_free"], constraints["max_budget"] is not None, constraints["only_format"])
    ):
        beginner = _is_beginner(profile)
        experienced = _normalize_text(profile.experience or "") in EXPERIENCE_ALIASES["опытный"]
        engaged = sum(kind in {"click", "complete"} for kind in seen_events.values()) >= 2
        growing = _normalize_text(profile.stage or "") in {"работающий бизнес", "рост"}
        paid_target = 1
        if not beginner:
            paid_target += int(experienced or engaged or growing) + int(experienced and growing and engaged)
        ordered = balance_first_page(ordered, paid_target=paid_target, seen=set(seen_events))
    ordered = _deduplicate_top(ordered, limit)
    selected = ordered[:limit]

    recommendations: list[Recommendation] = []
    selected_materials: list[Material] = []
    for _, mat, sig in selected:
        reason, next_step = _build_card_explanation(
            mat, profile, sig, is_cold_start=is_cold_start, is_weak_match=is_weak_match
        )
        recommendations.append(Recommendation(material_id=mat.id, reason=reason, next_step=next_step))
        selected_materials.append(mat)

    if not recommendations:
        return RecommendationResult((), "")

    first_mat = selected_materials[0]
    if context_items:
        ordering_reason = "Первые материалы продолжают тему двух ваших недавних переходов. Остальная подборка учитывает запрос и профиль."
    elif used_hybrid:
        ordering_reason = "Подборка учитывает совпадения запроса с заголовками, описаниями и темами, а также ваш профиль и историю."
    elif is_weak_match:
        ordering_reason = (
            f"Поскольку точного совпадения по запросу «{goal_raw}» в каталоге нет, "
            f"на первое место вынесен наиболее близкий базовый материал «{first_mat.title}», "
            "а далее предложены универсальные инструменты для продолжения."
        )
    elif is_cold_start:
        ordering_reason = (
            f"Для быстрого старта на первое место поставлен практический материал «{first_mat.title}», "
            "который можно применить сразу, а следом идут материалы для планирования и развития бизнеса."
        )
    elif valid_history:
        ordering_reason = (
            f"Порядок учитывает вашу текущую задачу и историю действий: сначала «{first_mat.title}» "
            "как наиболее точный следующий шаг, затем материалы для закрепления и расширения темы."
        )
    else:
        ordering_reason = (
            f"На первом месте — «{first_mat.title}» с максимальным совпадением по вашей задаче и профилю бизнеса, "
            "далее материалы выстроены по убыванию прикладной пользы и популярности."
        )

    follow_up = _build_follow_up_question(
        profile,
        has_history=bool(valid_history),
        is_weak_match=is_weak_match,
        is_cold_start=is_cold_start,
    )

    draft = RecommendationResult(
        items=tuple(recommendations),
        ordering_reason=ordering_reason,
        follow_up_question=follow_up,
    )
    return refine_with_llm_if_configured(selected_materials, profile, draft) if explain_with_llm else draft
