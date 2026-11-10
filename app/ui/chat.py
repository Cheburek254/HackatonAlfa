"""Демонстрационный помощник с персонализацией и ссылками на каталог."""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, session

from app.catalog.subscriptions import entitled_catalog
from app.contracts import Profile
from app.recommender import recommend
from app.recommender.baseline import recommend_popular_courses
from app.recommender.hybrid import DEFAULT_BM25_WEIGHT, acceptable, cached_bm25, constraints, merge_orders
from app.ui.accounts import current_account, recent_interactions, recommendation_mode, saved_profile
from app.ui.experiments import experiment_variant

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__, url_prefix="/api")
MATERIAL_LINK = re.compile(r"\[[^\]\n]+\]\(/materials/([a-zA-Z0-9_-]+)\)")

BASE_SYSTEM_PROMPT = (
    "Ты — виртуальный помощник демонстрационного проекта по подбору материалов «Альфа-Курса». "
    "Ты консультируешь предпринимателей: начинающих ИП, соло-фаундеров и владельцев малого бизнеса.\n"
    "Твои темы: запуск дела, регистрация бизнеса (ИП/ООО), налоги (УСН, патент), маркетинг, продажи, "
    "маркетплейсы (Wildberries, Ozon), охваты, реклама, юнит-экономика, финансовый учет (ДДС, P&L), найм сотрудников и обучение на «Альфа-Курсе».\n\n"
    "Правила:\n"
    "1. Отвечай сразу по существу заданного вопроса, без лишней воды и шаблонных вступлений.\n"
    "2. Давай 2-3 конкретных практических шага прямо под нишу и ситуацию пользователя.\n"
    "3. Порекомендуй подходящие материалы с платформы «Альфа-Курс».\n"
    "4. Для платных материалов не раскрывай закрытые уроки, а назови практическую пользу и предложи перейти к изучению на платформе.\n"
    "5. Поддерживай деловой, конструктивный и надежный тон Альфа-Банка.\n"
    "6. Рекомендуй материалы только из переданного списка. Для них используй указанные ссылки Markdown."
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
    model = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3.5-super-vl:free").strip()
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
        bm25_ids = cached_bm25(tuple(catalog))(query) if bm25_weight else ()
        core_res = recommend(catalog, rec_profile, (), 30, explain_with_llm=False)
        core_ids = tuple(r.material_id for r in core_res.items)
        valid_bm25 = tuple(mid for mid in bm25_ids if acceptable(materials_map[mid], filters))
        valid_core = tuple(mid for mid in core_ids if acceptable(materials_map[mid], filters))
        hybrid_ids = merge_orders((valid_core, valid_bm25), weights=(1 - bm25_weight, bm25_weight))
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
            desc = (getattr(m, "description", "") or "").strip()
            if desc:
                cand_lines.append(f"{i}. [{title}](/materials/{mid}) ({fmt}): {desc[:160]}")
            else:
                cand_lines.append(f"{i}. [{title}](/materials/{mid}) ({fmt})")
        prompt += (
            "\n[Рекомендуемые материалы платформы «Альфа-Курс» для ссылки в ответе]:\n"
            + "\n".join(cand_lines)
            + "\nИспользуй только ссылки из этого списка. Если пользователь просит рассказать о конкретном материале или номере (например «расскажи о 1 статье»), кратко объясни его суть и дай ссылку.\n"
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


def _call_gemini(api_key: str, model: str, message: str, history: list[dict], system_prompt: str = BASE_SYSTEM_PROMPT) -> str | None:
    candidate_models = [model] if model else ["gemini-3.5-flash-lite"]
    if "gemini-3.5-flash-lite" not in candidate_models:
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
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent"
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "AlfaCourseChat/1.0", "x-goog-api-key": api_key},
        )

        try:
            with urllib.request.urlopen(req, timeout=9) as response:
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
) -> str | None:
    if not api_key:
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
        "model": model or "nvidia/nemotron-3.5-super-vl:free",
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 700,
        "reasoning": {"effort": "none"},
    }
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
        with urllib.request.urlopen(req, timeout=12) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            choices = res_data.get("choices") or []
            if choices:
                msg_obj = choices[0].get("message", {})
                content = msg_obj.get("content") or msg_obj.get("reasoning") or ""
                if content:
                    return content.strip()
    except Exception as exc:
        logger.warning("Ошибка при обращении к OpenRouter API (%s): %s", model, exc)
    return None


def _fallback_reply(
    message: str,
    profile: dict | None = None,
    viewed_titles: list[str] | None = None,
    candidates: list | None = None,
) -> str:
    msg = message.casefold()
    prof = profile or {}
    viewed = viewed_titles or []
    cand = candidates or []

    # Персонализированный префикс, если есть профиль или история
    prefix = ""
    if viewed:
        prefix = f"Вижу, что вы уже ознакомились с материалом **«{viewed[0]}»**. "
    elif prof.get("industry"):
        prefix = f"С учетом вашей сферы деятельности (**{prof['industry']}**): "

    if any(w in msg for w in ("прода", "охват", "трафик", "клиент", "конверс", "маркетплейс", "wildberries", "ozon", "wb", "ctr", "реклам", "товар", "носк", "одежд", "лид")):
        return (
            f"{prefix}Для увеличения охватов и роста продаж товаров рекомендуем сфокусироваться на трех ключевых действиях:\n\n"
            "1. **Оптимизация точки контакта (визуал и CTR):** сочная инфографика, качественные фото и четкое позиционирование (для товарного бизнеса — комплекты по 3–5 штук, состав) повышают кликабельность в выдаче.\n"
            "2. **SEO и поисковые алгоритмы:** заполните все доступные характеристики в карточке, чтобы алгоритм маркетплейса выдавал товар по широкому пулу запросов.\n"
            "3. **Платный трафик с контролем юнит-экономики:** запустите автоматические рекламные кампании на тест и отслеживайте окупаемость каждого вложенного рубля.\n\n"
            "На платформе «Альфа-Курс» вам помогут:\n"
            "• Практический курс [«Маркетплейсы: с чего начать и как преуспеть»](/materials/d0aa4598-3da2-478b-bbf8-bda81b231b58)\n"
            "• Практический курс [«Бизнес на Авито: лёгкая продажа товаров»](/materials/a8184af0-7007-4bc3-98c1-1ff55c949a97)\n"
            "• Готовый инструмент [«Калькулятор юнит-экономики и процентов»](/materials/c25dc6f4-de0e-4f66-95cc-53b92612da34)"
        )

    if any(w in msg for w in ("начать", "старт", "открыть", "ип", "ооо", "регистрац", "с чего")):
        return (
            f"{prefix}Чтобы грамотно запустить бизнес без лишних рисков, двигайтесь по проверенным шагам:\n\n"
            "1. **Анализ спроса (CustDev):** пообщайтесь с первыми 10 потенциальными клиентами до закупки товара или аренды.\n"
            "2. **Юнит-экономика:** заранее посчитайте маржинальность с учетом налогов, логистики и комиссии.\n"
            "3. **Регистрация:** сравните доступные формы и налоговые режимы с учётом вида деятельности, доходов и расходов.\n\n"
            "На «Альфа-Курсе» рекомендуем изучить:\n"
            "• Вводный курс [«Просто о сложном: первые шаги в бизнесе»](/materials/3d17b03e-0e69-44cf-aa04-110e92437016)\n"
            "• Практический курс [«Как самозанятому стать ИП»](/materials/2155a4c8-2293-4b16-b928-cf72f2de72b5)\n"
            "• Курс [«ИПэшники: строим некринжовый бизнес»](/materials/552c492d-fac3-43f6-97f4-7a2d2625744c)"
        )

    if any(w in msg for w in ("налог", "усн", "патент", "бухгалтер", "ддс", "учет", "отчетност", "касс")):
        return (
            f"{prefix}По финансовому и налоговому учету главные правила безопасности бизнеса:\n\n"
            "1. **Разделяйте личные финансы и деньги бизнеса:** не оплачивайте личные покупки напрямую с расчетного счета без вывода дохода.\n"
            "2. **Контролируйте ДДС:** фиксируйте движение денежных средств еженедельно, чтобы вовремя заметить риск кассового разрыва.\n"
            "3. **Выбор режима:** если рассматриваете УСН, сравните варианты на ваших доходах и подтверждённых расходах. Ставки и условия проверьте по актуальным данным для вашего региона.\n\n"
            "На платформе «Альфа-Курс» используйте готовые инструменты:\n"
            "• Практический шаблон [«Калькулятор НДФЛ»](/materials/9766af8e-1923-4313-a4a8-e78a218d4b81)\n"
            "• Практический шаблон [«Калькулятор НДС»](/materials/d39c24d8-102e-4829-af72-ed57c1bcbff2)\n"
            "• Обучающий курс [«Самое важное o 115-ФЗ»](/materials/1c402043-808d-4604-9f45-0565ed4f79ba)"
        )

    if any(w in msg for w in ("моё обучение", "мое обучение", "где учиться", "профиль", "прогресс")):
        history_note = ""
        if viewed:
            history_note = f"\n\nНедавно вы просматривали: **{', '.join(viewed[:3])}**."
        return (
            "Ваша персональная история обучения и сохраненные материалы собраны в разделе [«Профиль»](/profile).\n\n"
            "Там можно настроить сферу, стадию бизнеса и цели, чтобы персональные рекомендации на платформе стали еще точнее."
            f"{history_note}"
        )

    if any(w in msg for w in ("техническ", "ошибк", "не открыва", "поддержк", "проблем", "повис")):
        return (
            "Если страница или материал не загружаются:\n\n"
            "1. Обновите страницу (Ctrl+F5 или Cmd+Shift+R).\n"
            "2. Убедитесь в стабильности интернет-соединения.\n"
            "3. При сохранении проблемы напишите в поддержку Альфа-Банка, специалисты оперативно помогут решить вопрос."
        )

    # Проверка запроса на подробности о конкретном материале из списка (например: "расскажи о 1 статье")
    item_match = re.search(
        r"(?:расскажи|подробн\w*|что в|о чем|покажи|прочитай|разбер\w*)\s+(?:о|об|про)?\s*"
        r"(?:(\d+)\s*(?:материал\w*|стать\w*|курс\w*|шаблон\w*)?|(перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*)\s*(?:материал\w*|стать\w*|курс\w*|шаблон\w*)?)",
        msg,
    )
    if item_match and cand:
        idx = None
        if item_match.group(1):
            try:
                idx = int(item_match.group(1)) - 1
            except ValueError:
                idx = None
        elif item_match.group(2):
            words_map = {"перв": 0, "втор": 1, "трет": 2, "четверт": 3, "пят": 4}
            for w, i in words_map.items():
                if item_match.group(2).startswith(w):
                    idx = i
                    break
        if idx is not None and 0 <= idx < len(cand):
            target = cand[idx]
            desc = getattr(target, "description", "") or "Практический обучающий материал от экспертов «Альфа-Курса»."
            lessons = getattr(target, "lessons", ()) or ()
            lessons_info = ""
            if lessons:
                lessons_info = f"\n\nВ программе курса ({len(lessons)} уроков): " + ", ".join(getattr(l, "title", str(l)) for l in lessons[:3]) + "..."
            return (
                f"Материал **[«{target.title}»](/materials/{target.id})** ({getattr(target, 'format', 'материал')}):\n\n"
                f"{desc}{lessons_info}\n\n"
                f"Изучить материал можно здесь: [«{target.title}»](/materials/{target.id})."
            )

    # Если есть подходящие кандидаты под нестандартный запрос
    if cand:
        cand_links = "\n".join(
            f"• [{getattr(m, 'title', '')}](/materials/{getattr(m, 'id', '')}) ({getattr(m, 'format', 'материал')})"
            for m in cand[:3]
        )
        return (
            f"{prefix}На платформе «Альфа-Курс» собраны проверенные практические материалы для развития вашего дела.\n\n"
            f"По вашей теме рекомендуем ознакомиться со следующими материалами:\n{cand_links}\n\n"
            "Опишите подробнее вашу текущую задачу, и я предложу конкретные шаги!"
        )

    return (
        f"{prefix}Рад помочь! На «Альфа-Курсе» мы помогаем предпринимателям решать практические бизнес-задачи без лишней теории.\n\n"
        "Рекомендуем начать с базового курса [«Просто о сложном: первые шаги в бизнесе»](/materials/3d17b03e-0e69-44cf-aa04-110e92437016) "
        "или курса [«Маркетплейсы: с чего начать и как преуспеть»](/materials/d0aa4598-3da2-478b-bbf8-bda81b231b58).\n\n"
        "Напишите мне, какой у вас бизнес (розница, услуги, производство) и какая цель — я сразу подберу следующие шаги!"
    )


@chat_bp.route("/chat", methods=["POST"])
def chat():
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

    # Сбор персонального контекста пользователя
    account = current_account()
    profile = saved_profile(account) or {}
    has_plus = bool(account and account["has_course_plus"])
    catalog = entitled_catalog(current_app.config["CHAT_CATALOG"], has_plus)

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

    # Подбор наиболее релевантных материалов каталога под вопрос и профиль
    variant = experiment_variant(account)
    mode = variant.mode if variant else recommendation_mode()
    weight = variant.bm25_weight if variant else current_app.config["BM25_WEIGHT"]
    if mode == "popular":
        by_id = {m.id: m for m in catalog}
        candidates = [by_id[item.material_id] for item in recommend_popular_courses(catalog, 5).items]
    else:
        candidates = _find_relevant_materials(
            message, profile, catalog, viewed_titles, limit=5,
            bm25_weight=weight if mode == "hybrid" else 0.0,
        )

    api_key, model = _load_env_credentials()
    openrouter_key, openrouter_model = _load_openrouter_credentials()
    reply = None

    if api_key or openrouter_key:
        system_prompt = _build_system_prompt(
            profile=profile,
            has_plus=has_plus,
            viewed_titles=viewed_titles,
            candidates=candidates,
        )
        if api_key:
            reply = _call_gemini(api_key, model, message, history, system_prompt=system_prompt)
        if not reply and openrouter_key:
            reply = _call_openrouter(openrouter_key, openrouter_model, message, history, system_prompt=system_prompt)

    if not reply:
        reply = _fallback_reply(message, profile=profile, viewed_titles=viewed_titles, candidates=candidates)

    # Автоматическое преобразование названий материалов в кликабельные ссылки
    reply = _keep_available_links(_linkify_materials(reply, candidates), set(catalog_titles), candidates)

    return jsonify({"ok": True, "reply": reply})
