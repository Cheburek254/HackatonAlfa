"""Аккаунты, сессии и профили веб-демо."""

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from time import time

from flask import (
    Blueprint, Flask, abort, current_app, g, redirect, render_template, request, session, url_for,
)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

PROFILE_FIELDS = {
    "stage": ("Стадия бизнеса", ("идея", "запуск", "работающий бизнес", "рост")),
    "industry": ("Сфера", (
        "розница", "услуги", "интернет-торговля", "общепит", "производство",
        "строительство и недвижимость", "информационные технологии", "образование",
        "логистика", "туризм", "другое",
    )),
    "experience": ("Опыт", ("начинающий", "есть опыт", "опытный")),
    "team_size": ("Размер команды", ("работаю один", "2–5 человек", "6–15 человек", "16 и больше")),
    "learning_topic": ("Что хотите изучить", (
        "запуск бизнеса", "маркетинг", "продажи", "финансы", "операционная работа", "любая тема",
    )),
    "preferred_format": ("Формат материалов", ("статья", "чек-лист", "шаблон", "курс", "любой формат")),
}
PROFILE_COLUMNS = ", ".join(PROFILE_FIELDS)
PROFILE_PARAMETERS = ", ".join(f":{field}" for field in PROFILE_FIELDS)
PROFILE_COOKIE = "alfahack_profile"
LOGIN_PATTERN = re.compile(r"[a-z0-9_.-]{3,32}")
PASSWORD_METHOD = "scrypt:32768:8:3"
DUMMY_HASH = generate_password_hash(secrets.token_hex(16), method=PASSWORD_METHOD)
ATTEMPT_WINDOW = 600
DEMO_USERNAME = "demo"
auth = Blueprint("accounts", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    stage TEXT,
    industry TEXT,
    experience TEXT,
    team_size TEXT,
    learning_topic TEXT,
    preferred_format TEXT,
    CHECK ((username = 'demo') = (password_hash IS NULL))
);
CREATE TABLE IF NOT EXISTS auth_attempts (
    scope TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    expires INTEGER NOT NULL
);
"""


class AccountSessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        return request.is_secure


def configure_accounts(app: Flask, database_path: Path) -> None:
    secret_key = os.environ.get("ALFA_SECRET_KEY") or secrets.token_hex(32)
    if len(secret_key) < 32:
        raise ValueError("ALFA_SECRET_KEY must contain at least 32 characters")
    app.config.update(
        SECRET_KEY=secret_key,
        ACCOUNT_DATABASE=database_path,
        SESSION_COOKIE_NAME="alfahack_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    app.session_interface = AccountSessionInterface()
    app.register_blueprint(auth)
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.teardown_appcontext(_close_db)

    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_path.touch(mode=0o600, exist_ok=True)
    database_path.chmod(0o600)
    with app.app_context():
        connection = _db()
        connection.executescript(SCHEMA)
        with connection:
            connection.execute(
                f"""INSERT INTO users (username, {PROFILE_COLUMNS})
                    VALUES (:username, {PROFILE_PARAMETERS}) ON CONFLICT(username) DO NOTHING""",
                {
                    "username": DEMO_USERNAME, "stage": "рост", "industry": "розница",
                    "experience": "опытный", "team_size": "2–5 человек",
                    "learning_topic": "продажи", "preferred_format": "курс",
                },
            )


def _db() -> sqlite3.Connection:
    if "accounts_db" not in g:
        g.accounts_db = sqlite3.connect(current_app.config["ACCOUNT_DATABASE"])
        g.accounts_db.row_factory = sqlite3.Row
    return g.accounts_db


def _close_db(_error: BaseException | None) -> None:
    connection = g.pop("accounts_db", None)
    if connection is not None:
        connection.close()


def profile_values(values: Mapping[str, object]) -> dict[str, str | None]:
    profile = {}
    for field, (_, options) in PROFILE_FIELDS.items():
        value = values.get(field)
        profile[field] = value if isinstance(value, str) and value in options else None
    return profile


def legacy_profile() -> dict[str, str | None]:
    try:
        values = json.loads(request.cookies.get(PROFILE_COOKIE, "{}"))
    except ValueError:
        values = {}
    return profile_values(values if isinstance(values, dict) else {})


def current_account() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return _db().execute(
        f"SELECT id, username, {PROFILE_COLUMNS} FROM users WHERE id = ?", (user_id,),
    ).fetchone()


def saved_profile(account: sqlite3.Row | None) -> dict[str, str | None]:
    if account is not None:
        return profile_values(dict(account))
    return legacy_profile()


def update_profile(account: sqlite3.Row, values: dict[str, str | None]) -> None:
    assignments = ", ".join(f"{field} = :{field}" for field in PROFILE_FIELDS)
    connection = _db()
    with connection:
        connection.execute(
            f"UPDATE users SET {assignments} WHERE id = :user_id",
            {**values, "user_id": account["id"]},
        )


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf() -> None:
    token = request.form.get("csrf_token", "")
    expected = session.get("csrf", "")
    if not expected or not secrets.compare_digest(token.encode(), expected.encode()):
        abort(400, "Откройте форму заново и повторите отправку.")


def _password_access_allowed() -> bool:
    return request.is_secure or bool(
        re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?", request.host, re.I)
    )


def _consume_attempts(scopes: tuple[tuple[str, int], ...]) -> bool:
    now = int(time())
    connection = _db()
    allowed = True
    with connection:
        connection.execute("DELETE FROM auth_attempts WHERE expires <= ?", (now,))
        for scope, limit in scopes:
            attempts = connection.execute(
                """INSERT INTO auth_attempts (scope, attempts, expires) VALUES (?, 1, ?)
                   ON CONFLICT(scope) DO UPDATE SET attempts = attempts + 1
                   RETURNING attempts""",
                (scope, now + ATTEMPT_WINDOW),
            ).fetchone()[0]
            allowed = allowed and attempts <= limit
    return allowed


def _auth_form(mode: str, error: str = "", username: str = "", status: int = 200):
    values = profile_values(request.form) if request.method == "POST" else legacy_profile()
    return render_template(
        "auth.html", mode=mode, error=error, username=username,
        password_access_allowed=_password_access_allowed(),
        profile_fields=PROFILE_FIELDS, profile_values=values, required_profile=True,
    ), status


def _start_session(user_id: int):
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    return redirect(url_for("profile_page"), code=303)


@auth.route("/login", methods=["GET", "POST"], endpoint="login")
@auth.route("/register", methods=["GET", "POST"], endpoint="register")
def credentials():
    mode = "register" if request.endpoint == "accounts.register" else "login"
    if request.method == "GET":
        return _auth_form(mode)
    if not _password_access_allowed():
        return _auth_form(mode, "Вход с паролем доступен по HTTPS.", status=403)
    check_csrf()

    username = request.form.get("username", "").strip().casefold()
    password = request.form.get("password", "")
    address = request.remote_addr or "unknown"
    valid_login = bool(LOGIN_PATTERN.fullmatch(username))
    scopes = ((f"{mode}:ip:{address}", 25),)
    if mode == "login" and valid_login:
        scopes += ((f"login:user:{username}", 8),)
    if not _consume_attempts(scopes):
        return _auth_form(mode, "Слишком много попыток. Повторите позже.", status=429)

    if mode == "register":
        if not valid_login or not 15 <= len(password) <= 128:
            return _auth_form(mode, "Проверьте формат логина и длину пароля.", username[:32], 400)
        values = profile_values(request.form)
        if not all(values.values()):
            return _auth_form(mode, "Заполните все поля профиля.", username, 400)
        connection = _db()
        try:
            with connection:
                user_id = connection.execute(
                    f"""INSERT INTO users (username, password_hash, {PROFILE_COLUMNS})
                        VALUES (:username, :password_hash, {PROFILE_PARAMETERS})""",
                    {**values, "username": username,
                     "password_hash": generate_password_hash(password, method=PASSWORD_METHOD)},
                ).lastrowid
        except sqlite3.IntegrityError:
            return _auth_form(mode, "Выберите другой логин.", username, 409)
        return _start_session(user_id)

    account = _db().execute(
        "SELECT id, password_hash FROM users WHERE username = ? AND password_hash IS NOT NULL",
        (username,),
    ).fetchone() if valid_login else None
    password_hash = account["password_hash"] if account is not None else DUMMY_HASH
    if 0 < len(password) <= 128 and check_password_hash(password_hash, password) and account is not None:
        return _start_session(account["id"])
    return _auth_form(mode, "Проверьте логин и пароль.", username[:32], 401)


@auth.post("/login/demo")
def demo_login():
    check_csrf()
    account = _db().execute("SELECT id FROM users WHERE username = ?", (DEMO_USERNAME,)).fetchone()
    return _start_session(account["id"])


@auth.post("/logout")
def logout():
    check_csrf()
    session.clear()
    return redirect(url_for("index"), code=303)
