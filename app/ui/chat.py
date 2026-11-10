"""ИИ-помощник «Альфа-Курс» на базе Gemini 2.5 Flash с надежным локальным фоллбэком."""

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__, url_prefix="/api")

SYSTEM_PROMPT = (
    "Ты — официальный виртуальный ИИ-помощник образовательной платформы «Альфа-Курс» от Альфа-Банка. "
    "Ты консультируешь предпринимателей: начинающих ИП, соло-фаундеров и владельцев малого бизнеса. "
    "Твои темы: запуск дела, регистрация бизнеса (ИП/ООО), налоги (УСН, патент), маркетинг, продажи, "
    "маркетплейсы (Wildberries, Ozon), охваты, реклама, юнит-экономика, финансовый учет (ДДС, P&L), найм сотрудников и обучение на «Альфа-Курсе». "
    "Правила:\n"
    "1. Отвечай сразу по существу заданного вопроса, без лишней воды и без шаблонных приветствий.\n"
    "2. Давай 2-3 конкретных практических шага прямо под нишу и ситуацию пользователя.\n"
    "3. Порекомендуй подходящий формат обучения на Альфа-Курсе (практический курс, готовая таблица-шаблон, пошаговая статья или калькулятор).\n"
    "4. Для платных материалов не раскрывай закрытые уроки, а назови практическую пользу и предложи перейти к изучению на платформе.\n"
    "5. Поддерживай деловой, конструктивный и надежный тон Альфа-Банка."
)


def _load_env_credentials() -> tuple[str, str]:
    api_key = os.environ.get("ALFA_LLM_API_KEY", "").strip()
    model = os.environ.get("ALFA_LLM_MODEL", "gemini-2.5-flash").strip()
    if api_key:
        return api_key, model

    # Поиск файла .env в корне репозитория, если переменные не заданы в окружении
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


def _call_gemini(api_key: str, model: str, message: str, history: list[dict]) -> str | None:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    contents = []
    for item in history[-6:]:
        role = "user" if item.get("role") == "user" else "model"
        text = (item.get("text") or "").strip()
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})

    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 600,
        },
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "AlfaCourseChat/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=9) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            candidates = res_data.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts and "text" in parts[0]:
                    return parts[0]["text"].strip()
    except Exception as exc:
        logger.warning("Ошибка при обращении к Gemini API (%s): %s", model, exc)

    return None


def _fallback_reply(message: str) -> str:
    msg = message.casefold()

    if any(w in msg for w in ("прода", "охват", "трафик", "клиент", "конверс", "маркетплейс", "wildberries", "ozon", "wb", "ctr", "реклам", "товар", "носк", "одежд", "лид")):
        return (
            "Для увеличения охватов и роста продаж ваших товаров рекомендуем сфокусироваться на трех ключевых действиях:\n\n"
            "1. **Оптимизация точки контакта (визуал и CTR):** сочная инфографика, качественные фото и четкое позиционирование (для товарного бизнеса — состав, посадка, комплекты по 3–5 штук) повышают кликабельность в выдаче.\n"
            "2. **SEO и поисковые алгоритмы:** заполните все доступные характеристики в карточке, чтобы алгоритм маркетплейса или поисковика выдавал товар по широкому пулу запросов.\n"
            "3. **Платный трафик с контролем юнит-экономики:** запустите автоматические рекламные кампании на тест и отслеживайте окупаемость каждого вложенного рубля.\n\n"
            "На платформе «Альфа-Курс» вам помогут:\n"
            "• Практический курс **«Продвижение на маркетплейсах»**\n"
            "• Готовый инструмент **«Калькулятор юнит-экономики»**\n"
            "• Чек-лист **«Оптимизация карточки товара»**"
        )

    if any(w in msg for w in ("начать", "старт", "открыть", "ип", "ооо", "регистрац", "с чего")):
        return (
            "Чтобы грамотно запустить бизнес без лишних рисков, двигайтесь по проверенным шагам:\n\n"
            "1. **Анализ спроса (CustDev):** пообщайтесь с первыми 10 потенциальными клиентами до закупки товара или аренды.\n"
            "2. **Юнит-экономика:** заранее посчитайте маржинальность с учетом налогов, логистики и комиссии.\n"
            "3. **Регистрация:** для старта оптимально открыть ИП на УСН «Доходы» 6% — простая отчетность и свободный вывод прибыли на карту.\n\n"
            "На «Альфа-Курсе» рекомендуем вводный курс **«Просто о сложном: первые шаги в бизнесе»** и готовые шаблоны для старта."
        )

    if any(w in msg for w in ("налог", "усн", "патент", "бухгалтер", "ддс", "учет", "отчетност", "касс")):
        return (
            "По финансовому и налоговому учету главные правила безопасности бизнеса:\n\n"
            "1. **Разделяйте личные финансы и деньги бизнеса:** не оплачивайте личные покупки напрямую с расчетного счета без вывода дохода.\n"
            "2. **Контролируйте ДДС:** фиксируйте движение денежных средств еженедельно, чтобы вовремя заметить риск кассового разрыва.\n"
            "3. **Выбор режима:** УСН «Доходы» 6% выгодна при марже выше 40%; если доля подтвержденных расходов превышает 65–70%, выгоднее УСН «Доходы минус расходы».\n\n"
            "В разделе «Шаблоны» на платформе доступны готовые таблицы учета финансов и налоговые календари."
        )

    if any(w in msg for w in ("моё обучение", "мое обучение", "где учиться", "профиль", "прогресс")):
        return (
            "Ваша история обучения и сохраненные материалы отображаются в разделе **«Профиль»** (в правом верхнем углу меню).\n\n"
            "Вы можете указать интересующую бизнес-задачу на главной странице, и навигатор мгновенно подберет персональный практический маршрут."
        )

    if any(w in msg for w in ("техническ", "ошибк", "не открыва", "поддержк", "проблем", "повис")):
        return (
            "Если страница или материал не загружаются:\n\n"
            "1. Обновите страницу (Ctrl+F5 или Cmd+Shift+R).\n"
            "2. Убедитесь в стабильности интернет-соединения.\n"
            "3. При сохранении проблемы напишите в чат поддержки Альфа-Банка, операторы помогут оперативно."
        )

    return (
        "Рад помочь! На «Альфа-Курсе» мы помогаем предпринимателям решать практические бизнес-задачи без лишней теории.\n\n"
        "Вы можете написать мне, какой у вас бизнес (например, розница, маркетплейсы, услуги) и какую цель хотите закрыть — я предложу конкретные шаги и подберу проверенные материалы."
    )


@chat_bp.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    history = data.get("history") or []

    if not message:
        return jsonify({"ok": False, "error": "Пустое сообщение"}), 400

    if len(message) > 1000:
        message = message[:1000]

    api_key, model = _load_env_credentials()
    reply = None

    if api_key:
        reply = _call_gemini(api_key, model, message, history)

    if not reply:
        reply = _fallback_reply(message)

    return jsonify({"ok": True, "reply": reply})
