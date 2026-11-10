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
    "Ты — официальный виртуальный помощник образовательной платформы «Альфа-Курс» от Альфа-Банка. "
    "Ты консультируешь предпринимателей: начинающих ИП, соло-фаундеров и владельцев малого бизнеса. "
    "Твои темы: запуск дела, регистрация бизнеса (ИП/ООО), налоги (УСН, патент), маркетинг, продажи, "
    "маркетплейсы (Wildberries, Ozon), юнит-экономика, финансовый учет (ДДС, P&L), найм сотрудников и обучение на «Альфа-Курсе». "
    "Правила:\n"
    "1. Отвечай кратко, практично, структурированно (списки, выделения) и доброжелательно.\n"
    "2. Предлагай 1-2 конкретных действия и рекомендуй полезные форматы (курс, готовый шаблон, статья или калькулятор).\n"
    "3. Для платных материалов не раскрывай закрытые уроки, а кратко назови пользу и предложи изучить материал на платформе.\n"
    "4. Держи деловой, ободряющий и надежный тон Альфа-Банка."
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

    # Ограничиваем историю последними 6 сообщениями для компактности
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
        with urllib.request.urlopen(req, timeout=12) as response:
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

    if any(w in msg for w in ("начать", "старт", "открыть", "ип", "ооо", "регистрац")):
        return (
            "Для успешного старта в бизнесе рекомендуем двигаться по проверенным шагам:\n\n"
            "1. **Выбор ниши и анализ спроса:** пообщайтесь с потенциальными клиентами (CustDev) до вложения денег.\n"
            "2. **Юнит-экономика:** рассчитайте базовую себестоимость и маржинальность.\n"
            "3. **Регистрация:** для соло-бизнеса оптимально ИП на УСН 6% («Доходы») — простая отчетность и легкий вывод прибыли.\n\n"
            "На платформе «Альфа-Курс» вам поможет вводный курс **«Просто о сложном: первые шаги в бизнесе»** и готовые шаблоны расчетов."
        )

    if any(w in msg for w in ("продаж", "клиент", "конверс", "маркетплейс", "wildberries", "ozon", "ctr", "реклам")):
        return (
            "Для роста продаж и привлечения клиентов эффективнее всего работать в двух направлениях:\n\n"
            "1. **Оптимизация точки контакта:** качественная инфографика и продающее описание товара/услуги кратно повышают конверсию из просмотра в покупку.\n"
            "2. **Трафик:** протестируйте таргетированную или внутреннюю рекламу на маркетплейсах на небольшом бюджете.\n\n"
            "В каталоге «Альфа-Курса» рекомендуем практический курс **«Продвижение на маркетплейсах»** и готовый **«Калькулятор юнит-экономики»**."
        )

    if any(w in msg for w in ("налог", "усн", "патент", "бухгалтер", "ддс", "учет", "отчетност")):
        return (
            "По финансовому и налоговому учету ключевые правила:\n\n"
            "1. **Разделяйте деньги:** никогда не смешивайте личные средства и оборот бизнеса.\n"
            "2. **Контролируйте ДДС:** ведите отчет о движении денежных средств еженедельно, чтобы не допустить кассового разрыва.\n"
            "3. **Налоговый режим:** УСН «Доходы» (6%) выгодна при рентабельности от 40%; если расходов более 65–70%, выгоднее УСН «Доходы минус расходы».\n\n"
            "В разделе «Шаблоны» на Альфа-Курсе доступны готовые таблицы учета финансов и налоговые календари."
        )

    if any(w in msg for w in ("моё обучение", "мое обучение", "где учиться", "профиль", "прогресс")):
        return (
            "Ваша история обучения и сохраненные материалы отображаются в разделе **«Профиль»** (в правом верхнем углу меню).\n\n"
            "Если вы хотите скорректировать траекторию — выберите интересующую задачу на главной странице подбора, и навигатор предложит следующий практический шаг!"
        )

    if any(w in msg for w in ("техническ", "ошибк", "не открыва", "поддержк", "проблем")):
        return (
            "Если материал не открывается или возникла задержка при загрузке:\n\n"
            "1. Обновите страницу или проверьте интернет-соединение.\n"
            "2. Убедитесь, что авторизованы под своим аккаунтом.\n"
            "3. Служба поддержки Альфа-Банка также всегда на связи в чате мобильного банка и по телефону горячей линии."
        )

    return (
        "«Альфа-Курс» создан, чтобы помогать предпринимателям быстро находить применимые ответы без лишней теории.\n\n"
        "Вы можете спросить меня о:\n"
        "• Открытии и регистрации бизнеса (ИП / ООО)\n"
        "• Выборе налогового режима и финансовом учете\n"
        "• Продвижении на маркетплейсах и росте продаж\n"
        "• Подборе практических курсов, статей и шаблонов\n\n"
        "Опишите вашу текущую задачу, и я предложу конкретные шаги!"
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
