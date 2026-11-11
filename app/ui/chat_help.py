"""Справка о возможностях демо. Ответы строятся из состояния приложения."""

import re

from flask import current_app, url_for

from app.ui.accounts import DEMO_USERNAME, PASSWORD_MIN_LENGTH


SUBSCRIPTION = re.compile(r"\bкурс\s*\+|\bподписк\w*.*альфа[ -]?курс|\bальфа[ -]?курс.*подписк\w*|"
                          r"^(?:что (?:да[её]т|открывает)|как (?:оформить|активировать)) (?:ваша |вашу )?подписк\w*[?!. ]*$")
PASSWORD = re.compile(r"\b(?:парол\w*|код подтверждения)\b")
SIGN_IN = re.compile(r"\b(?:войти|вход|зарегистрироваться|регистрация|создать аккаунт|авторизоваться)\b")
ACCOUNT = re.compile(r"\b(?:аккаунт\w*|профил\w*|личн\w+ кабинет\w*|мо[её] обучение|истори\w+ обучения)\b")
SUPPORT = re.compile(r"\b(?:не открыва\w*|не загружа\w*|не работает|не воспроизвод\w*|"
                     r"не могу открыть|не уда[её]тся открыть|техническ\w+ (?:проблем\w*|ошибк\w*|поддержк\w*)|"
                     r"(?:ошибк\w+|сбой) (?:на сайте|при загрузке|при открытии)|"
                     r"где поддержка|как (?:написать|обратиться) в поддержку)\b")
SITE_CONTEXT = re.compile(r"\b(?:курс\w*|урок\w*|материал\w*|видео|страниц\w*|сайт\w*|чат\w*|"
                          r"кнопк\w*|карточк\w*|поддержк\w*|техническ\w*)\b")
EXTERNAL_PROFILE = re.compile(r"\b(?:маркетплейс\w*|озон|авито|ozon|wildberries|телеграм\w*|продавц\w*)\b")


def help_reply(message, account):
    text = message.casefold().strip()
    login = url_for("accounts.login")
    profile = url_for("profile_page")
    if SUBSCRIPTION.search(text):
        summary = ("Курс+ открывает материалы с пометкой **«По подписке Курс+»**. "
                   "Бесплатные материалы доступны и без подписки, а остальные платные покупаются отдельно.")
        if account and account["has_course_plus"]:
            status = "Подписка в вашем аккаунте активна."
        elif account:
            status = "Подписка в вашем аккаунте не активна."
        else:
            status = f"[Войдите]({login}), чтобы проверить статус в своём аккаунте."
        if current_app.config["COURSE_PLUS_ACTIVATOR"] is None:
            status += " В демо подключение подписки через сайт пока недоступно."
        return "subscription", f"{summary}\n\n{status} [Раздел Курс+]({profile}#course-plus)."

    if PASSWORD.search(text) and (ACCOUNT.search(text) or not any(
        marker in text for marker in ("сотрудник", "хранить", "генератор")
    )):
        if any(word in text for word in ("забыл", "восстанов", "сбросить", "не помню")):
            return "account", ("Для самостоятельной смены нужен текущий пароль. Если забыли его, "
                               f"обратитесь к администратору демо. После восстановления [войдите]({login}).")
        if account and account["username"] == DEMO_USERNAME:
            return "account", ("У общего тестового аккаунта пароль не меняется. "
                               f"[Создайте личный аккаунт]({url_for('accounts.register')}), чтобы задать свой пароль.")
        settings = url_for("profile_settings", _anchor="password-settings")
        code = ("Код появится в форме демо." if current_app.config["PASSWORD_CODE_SENDER"] is None
                else "Код придёт на почту из профиля.")
        return "account", (f"Откройте [Смену пароля]({settings}). Укажите текущий пароль, дважды введите новый "
                           f"от {PASSWORD_MIN_LENGTH} символов и подтвердите смену кодом. {code} "
                           "После смены нужно войти заново.")

    if (text.rstrip("?! .") in {"поддержка", "служба поддержки", "не работает"} or
            (SUPPORT.search(text) and SITE_CONTEXT.search(text) and not (SIGN_IN.search(text) and ACCOUNT.search(text)))):
        return "support", ("Если курс или страница не открываются, обновите страницу и проверьте, выполнен ли вход. "
                           "Для материала с пометкой «По подписке Курс+» проверьте статус в "
                           f"[профиле]({profile}#course-plus). Какой материал вы открываете и какое сообщение видите?")

    if SIGN_IN.search(text) and (ACCOUNT.search(text) or "на сайт" in text or "альфа" in text or "ошибка входа" in text or
                                re.fullmatch(r"(?:как |не могу )?(?:войти|зарегистрироваться)[?!. ]*", text)):
        return "account", (f"[Войдите]({login}) по логину и паролю. Для первого входа можно "
                           f"[зарегистрироваться]({url_for('accounts.register')}) или выбрать тестовый аккаунт на странице входа.")
    if ACCOUNT.search(text) and not EXTERNAL_PROFILE.search(text):
        settings = url_for("profile_settings")
        if account and account["username"] == DEMO_USERNAME:
            return "account", f"Данные общего тестового аккаунта можно посмотреть в [профиле]({profile})."
        return "account", (f"В [профиле]({profile}) доступны параметры бизнеса. Просмотры учитываются при подборе. "
                           f"Изменить данные можно в [настройках профиля]({settings}).")
    return None
