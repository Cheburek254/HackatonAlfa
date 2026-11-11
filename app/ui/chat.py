"""Демонстрационный помощник с персонализацией и ссылками на каталог."""

import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from time import monotonic, time

from flask import Blueprint, current_app, jsonify, request, session
from itsdangerous import BadData, URLSafeTimedSerializer

from app.catalog import material_format
from app.catalog.subscriptions import entitled_catalog
from app.contracts import InteractionEvent, Profile
from app.recommender import recommend
from app.recommender.hybrid import DEFAULT_BM25_WEIGHT, acceptable, constraints, material_text_terms, tokens
from app.ui.accounts import _consume_attempts, _db, csrf_token, current_account, recent_interactions, saved_profile
from app.ui.chat_help import help_reply
from app.ui.chat_metrics import record_open, record_response
from app.ui.descriptions import clean_plain_text

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__, url_prefix="/api")
MATERIAL_LINK = re.compile(r"\[[^\]\n]+\]\(/materials/([a-zA-Z0-9_-]+)\)")
CARD_REFERENCE = re.compile(
    r"^(?:а\s+)?(?:(?:расскажи(?:те)?(?: подробнее)?|подробнее|покажи(?:те)?|что известно)\s+)?"
    r"(?:(?:о|об|про)\s+)?(?P<ordinal>[1-5](?:[-‑]?(?:й|м|ю|я|ую|ом))?|"
    r"перв\w*|втор\w*|трет\w*|четв[её]рт\w*|пят\w*)"
    r"(?:\s+(?:материал\w*|стать\w*|курс\w*|карточк\w*|шаблон\w*|вебинар\w*|подкаст\w*|вариант\w*))?[?!. ]*$"
)
CARD_CONTEXT_AGE = 86400
CHAT_REQUESTS_PER_MINUTE = 16
CHAT_MODEL_REQUESTS_PER_MINUTE = 120
CHAT_PROVIDER_BUDGET = 8.0
CHAT_CLARIFICATION = "Не понял ваш запрос, уточните."
GREETING = re.compile(r"^(?:привет(?:ствую)?|здравствуй(?:те)?|доброе утро|добрый (?:день|вечер)|спасибо|благодарю)[!,.? ]*$")
BUSINESS_QUERY = re.compile(
    r"\b(?:бизнес\w*|предприним\w*|ип|ооо|налог\w*|усн|ндс|ндфл|патент\w*|финанс\w*|"
    r"бухгалтер\w*|уч[её]т\w*|маркетинг\w*|продаж\w*|клиент\w*|реклам\w*|маркетплейс\w*|"
    r"поставк\w*|поставщик\w*|договор\w*|отч[её]т\w*|сотрудник\w*|зарплат\w*|розниц\w*|"
    r"авито|озон|ozon|wildberries|wb|командиров\w*|плат[её]ж\w*|касс\w*|сверк\w*|гпх|сoут|соут|"
    r"регламент\w*|закуп\w*|цен\w*|прибыл\w*|рентабельн\w*|дтс|кадр\w*|воронк\w*|"
    r"задолжен\w*|контрагент\w*|свери\w*|концесси\w*|франшиз\w*|"
    r"стартап\w*|товар\w*|скидк\w*|подряд\w*|производств\w*|команд\w*|себестоимост\w*)\b"
)
SEARCH_REQUEST = re.compile(r"\b(?:подбери\w*|посоветуй\w*|найди\w*|ищу|нуж\w*|дайте|скачать|курс\w*|стать\w*|"
                            r"бланк\w*|шаблон\w*|калькулятор\w*|вебинар\w*|подкаст\w*|как|зачем)\b")
GENERIC_QUERY_TERMS = frozenset(tokens(
    "пожалуйста расскажи подробно курс курсы статья статьи шаблон бланк калькулятор материал материалы "
    "покажи подбери подобрать посоветуй ищу дайте нужен нужна нужно узнать научиться понять завтра сегодня"
))

BASE_SYSTEM_PROMPT = (
    "Ты — виртуальный помощник демонстрационного проекта по подбору материалов «Альфа-Курса». "
    "Ты консультируешь предпринимателей: начинающих ИП, соло-фаундеров и владельцев малого бизнеса.\n"
    "Твои темы: запуск дела, регистрация бизнеса (ИП/ООО), налоги (УСН, патент), маркетинг, продажи, "
    "маркетплейсы (Wildberries, Ozon), охваты, реклама, юнит-экономика, финансовый учет (ДДС, P&L), найм сотрудников, обучение на «Альфа-Курсе» и помощь по платформе.\n\n"
    "Правила:\n"
    "1. Отвечай сразу по существу заданного вопроса, без лишней воды и шаблонных вступлений.\n"
    "2. Давай 2-3 конкретных практических шага прямо под нишу и ситуацию пользователя.\n"
    "3. Порекомендуй подходящие материалы с платформы «Альфа-Курс».\n"
    "4. Для платных материалов не раскрывай закрытые уроки, а назови практическую пользу и предложи перейти к изучению на платформе.\n"
    "5. Поддерживай деловой, конструктивный и надежный тон Альфа-Банка.\n"
    "6. Рекомендуй материалы только из переданного списка. Для них используй указанные ссылки Markdown.\n"
    "7. Курс+ открывает материалы с пометкой «По подписке Курс+». Остальные платные материалы приобретаются отдельно. "
    "Для вопросов о профиле, подписке и работе сайта используй справку приложения и не выдумывай инструкции.\n"
    "8. Если вопрос неясен, попроси уточнить. Не приписывай пользователю задачу или проект, которые он не упоминал."
)

def _load_env_credentials() -> tuple[str, str]:
    api_key = os.environ.get("ALFA_LLM_API_KEY", "").strip()
    model = os.environ.get("ALFA_LLM_MODEL", "gemini-3.5-flash-lite").strip()
    if api_key:
        return api_key, model

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.is_file():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "ALFA_LLM_API_KEY" and not api_key:
                    api_key = v
                elif k == "ALFA_LLM_MODEL" and v:
                    model = v
        except Exception as exc:
            logger.warning("Не удалось прочитать .env: %s", exc)

    return api_key, model


def _load_openrouter_credentials() -> tuple[str, str]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = os.environ.get("OPENROUTER_MODEL", "google/gemini-3.5-flash-lite").strip()
    if api_key:
        return api_key, model

    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.is_file():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k == "OPENROUTER_API_KEY" and not api_key:
                    api_key = v
                elif k == "OPENROUTER_MODEL" and v:
                    model = v
        except Exception as exc:
            logger.warning("Не удалось прочитать .env для OpenRouter: %s", exc)

    return api_key, model
def _find_relevant_materials(
    query: str,
    profile: dict,
    catalog: tuple | list,
    viewed_titles: list[str],
    limit: int = 6,
    *,
    bm25_weight: float = DEFAULT_BM25_WEIGHT,
    history: Sequence[InteractionEvent] = (),
    links_directory: Path | None = None,
) -> list:
    if not catalog:
        return []

    materials_map = {m.id: m for m in catalog}
    viewed_set = {t.casefold() for t in viewed_titles}
    filters = constraints(query)

    # 1. Оптимальный гибридный алгоритм (BM25 + recommend + RRF + constraints)
    try:
        rec_profile = Profile(goal=query, **{field: profile.get(field) for field in (
            "industry", "stage", "experience", "team_size", "learning_topic", "preferred_format",
        )})
        result = recommend(catalog, rec_profile, history, max(30, limit), explain_with_llm=False,
                           links_directory=links_directory, bm25_weight=bm25_weight, bm25_adaptive=True)
        hybrid_ids = tuple(item.material_id for item in result.items if acceptable(materials_map[item.material_id], filters))
        if hybrid_ids:
            ordered = [materials_map[mid] for mid in hybrid_ids]
            ordered.sort(key=lambda item: item.title.casefold() in viewed_set)
            return ordered[:limit]
    except Exception as exc:
        logger.warning("Ошибка в гибридном поиске чата, откат к эвристике: %s", exc)

    # 2. Запасной эвристический подбор (fallback)
    words = set(re.findall(r"\w{3,}", query.casefold()))
    industry = (profile.get("industry") or "").casefold()
    topic = (profile.get("learning_topic") or "").casefold()
    stage = (profile.get("stage") or "").casefold()
    pref_format = (profile.get("preferred_format") or "").casefold()

    scored = []
    for m in catalog:
        if not acceptable(m, filters):
            continue
        score = 0.0
        m_title = getattr(m, "title", "").casefold()
        m_desc = getattr(m, "description", "").casefold()
        m_topics = " ".join(getattr(m, "topics", ())).casefold()
        m_industries = [ind.casefold() for ind in getattr(m, "industries", ())]
        m_stages = [st.casefold() for st in getattr(m, "stages", ())]
        m_format = (getattr(m, "format", "") or "").casefold()

        # Query matches
        for w in words:
            if w in m_title:
                score += 8.0
            elif w in m_topics:
                score += 4.0
            elif w in m_desc:
                score += 2.0

        # Profile matches
        if industry and any(industry in ind for ind in m_industries):
            score += 3.5
        if topic and topic in m_topics:
            score += 3.5
        if stage and any(stage in st for st in m_stages):
            score += 2.0
        if pref_format and pref_format in m_format:
            score += 1.5

        # Popularity prior
        pop = getattr(m, "popularity", 0.0) or 0.0
        score += min(pop / 3000.0, 2.0)

        # Demote materials the user has already viewed
        if m_title in viewed_set:
            score -= 15.0

        scored.append((score, m))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for s, m in scored[:limit]]


def _build_system_prompt(
    profile: dict,
    has_plus: bool,
    viewed_titles: list[str],
    candidates: list,
) -> str:
    prompt = BASE_SYSTEM_PROMPT

    profile_parts = []
    if profile.get("industry"):
        profile_parts.append(f"- Сфера бизнеса: {profile['industry']}")
    if profile.get("stage"):
        profile_parts.append(f"- Стадия бизнеса: {profile['stage']}")
    if profile.get("experience"):
        profile_parts.append(f"- Опыт предпринимателя: {profile['experience']}")
    if profile.get("team_size"):
        profile_parts.append(f"- Команда: {profile['team_size']}")
    if profile.get("learning_topic"):
        profile_parts.append(f"- Интересующая тема: {profile['learning_topic']}")
    if profile.get("preferred_format"):
        profile_parts.append(f"- Предпочитаемый формат: {profile['preferred_format']}")
    profile_parts.append(f"- Тариф: {'Курс+ (бесплатные и подписочные материалы)' if has_plus else 'Базовый тариф'}")

    prompt += "\n\n[Профиль текущего клиента в системе]:\n" + "\n".join(profile_parts) + "\n"
    prompt += "Учитывай профиль при подборе советов: предлагай решения с учетом его сферы, опыта и масштаба.\n"

    if viewed_titles:
        prompt += (
            "\n[Ранее просмотренные / завершенные материалы клиента]:\n"
            + "\n".join(f"• {t}" for t in viewed_titles[:5])
            + "\nКлиент уже знает эти материалы. Не советуй их заново, предлагай следующий шаг!\n"
        )

    if candidates:
        cand_lines = []
        for i, m in enumerate(candidates, 1):
            title = getattr(m, "title", "")
            mid = getattr(m, "id", "")
            fmt = getattr(m, "format", "материал")
            desc = clean_plain_text(getattr(m, "description", ""))
            if desc:
                cand_lines.append(f"{i}. [{title}](/materials/{mid}) ({fmt}): {desc[:160]}")
            else:
                cand_lines.append(f"{i}. [{title}](/materials/{mid}) ({fmt})")
        prompt += (
            "\n[Рекомендуемые материалы платформы «Альфа-Курс» для ссылки в ответе]:\n"
            + "\n".join(cand_lines)
            + "\nИнтерфейс покажет карточки отдельно и в этом порядке. Объясни, с чего начать. "
            "Используй только ссылки из списка, когда упоминаешь конкретный материал.\n"
        )

    return prompt


def _linkify_materials(text: str, candidates: list) -> str:
    """Гарантирует, что любые упомянутые названия курсов превращаются в кликабельные ссылки."""
    items: list[tuple[str, str]] = [(getattr(m, "id", ""), getattr(m, "title", "")) for m in candidates]

    for mid, title in items:
        if not mid or not title or f"/materials/{mid}" in text:
            continue
        core_title = title.replace("\xa0", " ").strip()
        escaped = re.escape(core_title).replace(r"\ ", r"\s+")
        try:
            pattern = re.compile(rf"(?<!\[)(?:[«\"]|\*\*)?({escaped})(?:[»\"]|\*\*)?(?!\]|\()", re.IGNORECASE)
            text = pattern.sub(f"[{core_title}](/materials/{mid})", text)
        except Exception:
            pass

    return text


def _keep_available_links(reply: str, allowed_ids: set[str], candidates: list) -> str:
    blocked = any(match[1] not in allowed_ids for match in MATERIAL_LINK.finditer(reply))
    if not blocked:
        return reply
    lines = []
    for line in reply.splitlines():
        filtered = MATERIAL_LINK.sub(lambda match: match[0] if match[1] in allowed_ids else "", line)
        if filtered != line and line.lstrip().startswith(("•", "- ", "* ")):
            continue
        lines.append(filtered)
    reply = "\n".join(lines).strip()
    if candidates and not MATERIAL_LINK.search(reply):
        reply += "\n\n" + "\n".join(f"• [{item.title}](/materials/{item.id})" for item in candidates[:3])
    return reply


def _memory_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="chat-cards-v1")


def _click_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="chat-open-v1")


def _remembered_cards(token, subject):
    if not isinstance(token, str) or not token or len(token) > 2048:
        return ()
    try:
        state = _memory_signer().loads(token, max_age=CARD_CONTEXT_AGE)
    except BadData:
        return ()
    return tuple(state["ids"]) if state["subject"] == subject else ()


def _card_position(message):
    match = CARD_REFERENCE.fullmatch(message.casefold())
    if not match:
        return None
    ordinal = match["ordinal"]
    if ordinal[0].isascii() and ordinal[0].isdigit():
        return int(ordinal[0]) - 1
    return ("пер", "вто", "тре", "чет", "пят").index(ordinal[:3])


def _card_details(material):
    title = material.title.replace("[", "(").replace("]", ")")
    description = clean_plain_text(material.description)
    if len(description) > 500:
        description = description[:500].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    details = [value for value in (material.format, material.duration,
                                   f"Уроков: {material.lesson_count}" if material.lesson_count else None) if value]
    lines = [f"[{title}](/materials/{material.id})"]
    if details:
        lines.append(" · ".join(details))
    if description:
        lines.append(description)
    return "\n\n".join(lines)


def _chat_subject(account):
    return f"user:{account['id']}" if account else "guest:" + sha256(csrf_token().encode()).hexdigest()


def _supported_candidates(message, candidates):
    terms = set(tokens(message)) - GENERIC_QUERY_TERMS
    if not terms:
        return []
    business = BUSINESS_QUERY.search(message.casefold())
    supported = []
    for material in candidates:
        headings, text = material_text_terms(material.title, material.description, material.topics)
        if business and (terms & headings or len(terms & text) >= 2):
            supported.append(material)
        elif not business and len(terms & headings) >= 2:
            supported.append(material)
    return supported


def _provider_timeout(deadline, limit):
    return limit if deadline is None else min(limit, max(0.0, deadline - monotonic()))


def _call_gemini(api_key: str, model: str, message: str, history: list[dict], system_prompt: str = BASE_SYSTEM_PROMPT,
                 *, deadline: float | None = None, retry_default: bool = True) -> str | None:
    candidate_models = [model] if model else ["gemini-3.5-flash-lite"]
    if retry_default and "gemini-3.5-flash-lite" not in candidate_models:
        candidate_models.append("gemini-3.5-flash-lite")

    contents = []
    for item in history[-6:]:
        role = "user" if item.get("role") == "user" else "model"
        text = (item.get("text") or "").strip()
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})

    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 650,
        },
    }

    data = json.dumps(payload).encode("utf-8")

    for current_model in candidate_models:
        timeout = _provider_timeout(deadline, 4.0)
        if timeout < 0.25:
            return None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent"
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "AlfaCourseChat/1.0", "x-goog-api-key": api_key},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                candidates = res_data.get("candidates") or []
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts and "text" in parts[0]:
                        return parts[0]["text"].strip()
        except urllib.error.HTTPError as exc:
            logger.warning("HTTP ошибка Gemini API (%s, code %s): %s", current_model, exc.code, exc)
            if exc.code == 429:
                continue
        except Exception as exc:
            logger.warning("Ошибка при обращении к Gemini API (%s): %s", current_model, exc)

    return None


def _call_openrouter(
    api_key: str,
    model: str,
    message: str,
    history: list[dict],
    system_prompt: str = BASE_SYSTEM_PROMPT,
    *, deadline: float | None = None,
) -> str | None:
    timeout = _provider_timeout(deadline, 4.0)
    if not api_key or timeout < 0.25:
        return None
    url = "https://openrouter.ai/api/v1/chat/completions"
    messages = [{"role": "system", "content": system_prompt}]
    for item in history[-6:]:
        role = "user" if item.get("role") == "user" else "assistant"
        text = (item.get("text") or "").strip()
        if text:
            messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": message})

    payload = {
        "model": model or "google/gemini-3.5-flash-lite",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 700,
    }
    if "nemotron" in (model or "").lower():
        payload["reasoning"] = {"effort": "none"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://alfakurs.alfa-bank.ru",
            "X-Title": "AlfaKurs Assistant",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            choices = res_data.get("choices") or []
            if choices:
                msg_obj = choices[0].get("message", {})
                content = msg_obj.get("content") or ""
                if content:
                    return content.strip()
    except urllib.error.HTTPError as exc:
        logger.warning("HTTP ошибка OpenRouter API (%s, code %s)", model, exc.code)
    except Exception as exc:
        logger.warning("Ошибка при обращении к OpenRouter API (%s): %s", model, exc)
    return None


def _fallback_reply(candidates):
    if not candidates:
        return CHAT_CLARIFICATION
    first = candidates[0]
    description = clean_plain_text(first.description)
    intro = "Подобрал материалы по вашей задаче. Начните с первого в подборке."
    if description:
        intro += "\n\n" + description[:240].strip()
    links = "\n".join(f"• [{item.title}](/materials/{item.id})" for item in candidates)
    return f"{intro}\n\n{links}"


@chat_bp.route("/chat", methods=["POST"])
def chat():
    started = monotonic()
    request.max_content_length = 24 * 1024
    if request.content_length and request.content_length > request.max_content_length:
        return jsonify(ok=False, error="Сообщение слишком длинное."), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("message"), str):
        return jsonify({"ok": False, "error": "Ожидалось текстовое сообщение"}), 400
    message = data["message"].strip()[:1000]
    supplied_history = data.get("history")
    history = [
        {"role": item["role"], "text": item["text"][:1000]}
        for item in supplied_history[-6:]
        if isinstance(item, dict) and item.get("role") in ("user", "model") and isinstance(item.get("text"), str)
    ] if isinstance(supplied_history, list) else []

    if not message:
        return jsonify({"ok": False, "error": "Пустое сообщение"}), 400
    if history and history[-1] == {"role": "user", "text": message}:
        history.pop()

    account = current_account()
    subject = _chat_subject(account)
    if not _consume_attempts(((f"chat:{subject}", CHAT_REQUESTS_PER_MINUTE),), window=60):
        return jsonify(ok=False, error="Слишком много сообщений. Попробуйте через минуту."), 429, {"Retry-After": "60"}
    token = data.get("card_context")
    remembered = _remembered_cards(token, subject)

    def respond(reply, source, intent, *, materials=(), replace_context=False):
        ids = tuple(item.id for item in materials)
        cards = [{"id": item.id, "title": item.title, "format": material_format(item), "url": f"/materials/{item.id}",
                  "description": clean_plain_text(item.description)[:160]} for item in materials]
        context = token if remembered else None
        if replace_context and ids:
            context = _memory_signer().dumps({"subject": subject, "ids": ids})
        turn_id = secrets.token_urlsafe(16)
        record_response(turn_id, source, intent, round((monotonic() - started) * 1000), len(cards))
        click_context = _click_signer().dumps({"subject": subject, "ids": ids, "turn": turn_id}) if ids else None
        return jsonify(ok=True, reply=reply, source=source, intent=intent, cards=cards, card_context=context,
                       click_context=click_context)

    if help_response := help_reply(message, account):
        intent, reply = help_response
        return respond(reply, "help", intent)
    if GREETING.fullmatch(message.casefold()):
        return respond("Могу подобрать учебные материалы или помочь с аккаунтом и Курс+. Чем помочь?",
                       "local", "greeting")

    profile = saved_profile(account) or {}
    has_plus = bool(account and account["has_course_plus"])
    catalog = entitled_catalog(current_app.config["CHAT_CATALOG"], has_plus)
    if (position := _card_position(message)) is not None:
        if not remembered:
            return respond("Сначала попросите подборку, а затем укажите номер карточки.", "memory", "card_detail")
        if position >= len(remembered):
            return respond(f"В последней подборке было карточек: {len(remembered)}. Укажите номер из этого списка.",
                           "memory", "card_detail")
        material = next((item for item in catalog if item.id == remembered[position]), None)
        if material is None:
            return respond("Эта карточка сейчас недоступна. Укажите другой номер или попросите новую подборку.",
                           "memory", "card_detail")
        return respond(_card_details(material), "memory", "card_detail", materials=(material,))

    if not (set(tokens(message)) - GENERIC_QUERY_TERMS and
            (BUSINESS_QUERY.search(message.casefold()) or SEARCH_REQUEST.search(message.casefold()))):
        return respond(CHAT_CLARIFICATION, "local", "clarification")

    # Сбор истории взаимодействий пользователя с материалами
    interactions = recent_interactions(account)
    if not interactions and "guest_history" in session:
        interactions = recent_interactions(None)

    catalog_titles = {item.id: item.title for item in catalog}
    viewed_titles: list[str] = []
    seen = set()
    for ev in interactions:
        mid = getattr(ev, "material_id", None)
        if mid and mid in catalog_titles:
            title = catalog_titles[mid]
            if title not in seen:
                seen.add(title)
                viewed_titles.append(title)

    candidates = _find_relevant_materials(
        message, profile, catalog, viewed_titles, limit=5, bm25_weight=current_app.config["CHAT_BM25_WEIGHT"],
        history=interactions[::-1], links_directory=current_app.config["CATALOG_LINKS_DIR"],
    )
    candidates = _supported_candidates(message, candidates)
    if not candidates:
        return respond(CHAT_CLARIFICATION, "local", "clarification")

    api_key, model = _load_env_credentials()
    openrouter_key, openrouter_model = _load_openrouter_credentials()
    reply = None
    source = "fallback"

    if (api_key or openrouter_key) and _consume_attempts((("chat:model:global", CHAT_MODEL_REQUESTS_PER_MINUTE),), window=60):
        deadline = monotonic() + CHAT_PROVIDER_BUDGET
        system_prompt = _build_system_prompt(
            profile=profile,
            has_plus=has_plus,
            viewed_titles=viewed_titles,
            candidates=candidates,
        )
        if api_key:
            reply = _call_gemini(api_key, model, message, history, system_prompt=system_prompt, deadline=deadline,
                                 retry_default=not openrouter_key)
            if reply:
                source = "gemini"
        if not reply and openrouter_key and _provider_timeout(deadline, 4.0) >= 0.25:
            reply = _call_openrouter(openrouter_key, openrouter_model, message, history, system_prompt=system_prompt,
                                     deadline=deadline)
            if reply:
                source = "openrouter"

    if not reply:
        reply = _fallback_reply(candidates)

    reply = _keep_available_links(_linkify_materials(reply, candidates), {item.id for item in candidates}, candidates)
    return respond(reply, source, "material_search", materials=candidates, replace_context=True)


@chat_bp.post("/chat/card-open")
def chat_card_open():
    request.max_content_length = 4 * 1024
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(token := data.get("click_context"), str) or len(token) > 2048:
        return "", 400
    try:
        state = _click_signer().loads(token, max_age=CARD_CONTEXT_AGE)
    except BadData:
        return "", 400
    if state["subject"] != _chat_subject(current_account()) or data.get("material_id") not in state["ids"]:
        return "", 403
    record_open(state["turn"])
    return "", 204
