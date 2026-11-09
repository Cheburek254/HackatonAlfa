"""Аккаунты, сессии и профили веб-демо."""

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Literal

from flask import (
    Blueprint, Flask, abort, current_app, g, redirect, render_template, request, session, url_for,
)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

from app.contracts import InteractionEvent

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
PROFILE_COOKIE = "alfahack_profile"
LOGIN_PATTERN = re.compile(r"[a-z0-9_.-]{3,32}")
PASSWORD_METHOD = "scrypt:32768:8:3"
DUMMY_HASH = generate_password_hash(secrets.token_hex(16), method=PASSWORD_METHOD)
ATTEMPT_WINDOW = 600
SESSION_LIFETIME = timedelta(days=7)
HISTORY_WINDOW = timedelta(days=90)
HISTORY_LIMIT = 100
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
CREATE TABLE IF NOT EXISTS account_details (
    user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    company TEXT NOT NULL,
    avatar BLOB,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS account_sessions (
    token_hash TEXT PRIMARY KEY NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    expires INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS account_sessions_expires ON account_sessions (expires);
CREATE TABLE IF NOT EXISTS interaction_events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    material_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('view', 'click')),
    occurred_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS interaction_events_user_time ON interaction_events (user_id, occurred_at DESC);
"""

INSERT_USER = """
INSERT INTO users (
    username, password_hash, stage, industry, experience, team_size, learning_topic, preferred_format
) VALUES (
    :username, :password_hash, :stage, :industry, :experience, :team_size, :learning_topic, :preferred_format
)
ON CONFLICT (username) DO NOTHING
RETURNING id
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
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
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
            connection.execute("BEGIN IMMEDIATE")
            detail_columns = {row["name"] for row in connection.execute("PRAGMA table_info(account_details)")}
            if "email" not in detail_columns:
                connection.execute("ALTER TABLE account_details ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            if "phone" not in detail_columns:
                connection.execute("ALTER TABLE account_details ADD COLUMN phone TEXT NOT NULL DEFAULT ''")
            connection.execute(
                INSERT_USER,
                {
                    "username": DEMO_USERNAME, "password_hash": None,
                    "stage": "рост", "industry": "розница",
                    "experience": "опытный", "team_size": "2–5 человек",
                    "learning_topic": "продажи", "preferred_format": "курс",
                },
            ).fetchone()


def _db() -> sqlite3.Connection:
    if "accounts_db" not in g:
        connection = sqlite3.connect(current_app.config["ACCOUNT_DATABASE"])
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        g.accounts_db = connection
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


def _session_hash() -> str | None:
    token = session.get("account_token")
    return sha256(token.encode()).hexdigest() if isinstance(token, str) else None


def current_account() -> sqlite3.Row | None:
    token_hash = _session_hash()
    if token_hash is None:
        return None
    account = _db().execute(
        """SELECT users.id, username, stage, industry, experience, team_size, learning_topic, preferred_format,
                  first_name, last_name, company, email, phone, avatar IS NOT NULL AS has_avatar
           FROM account_sessions
           JOIN users ON users.id = account_sessions.user_id
           LEFT JOIN account_details ON account_details.user_id = users.id
           WHERE token_hash = ? AND expires > ? AND (username = ? OR ?)""",
        (token_hash, int(time()), DEMO_USERNAME, _password_access_allowed()),
    ).fetchone()
    if account is None:
        session.clear()
    return account


def saved_profile(account: sqlite3.Row | None) -> dict[str, str | None]:
    if account is not None:
        return profile_values(dict(account))
    return legacy_profile()


def record_interaction(
    account: sqlite3.Row | None, material_id: str, kind: Literal["view", "click"],
) -> None:
    if account is None or account["username"] == DEMO_USERNAME:
        return
    connection = _db()
    with connection:
        connection.execute(
            "INSERT INTO interaction_events (user_id, material_id, kind, occurred_at) VALUES (?, ?, ?, ?)",
            (account["id"], material_id, kind, int(time())),
        )


def recent_interactions(account: sqlite3.Row | None) -> tuple[InteractionEvent, ...]:
    if account is None or account["username"] == DEMO_USERNAME:
        return ()
    cutoff = int(time() - HISTORY_WINDOW.total_seconds())
    rows = _db().execute(
        """SELECT material_id, kind, MAX(occurred_at) AS latest_at
           FROM interaction_events WHERE user_id = ? AND occurred_at >= ?
           GROUP BY material_id, kind
           ORDER BY latest_at DESC, MAX(id) DESC LIMIT ?""",
        (account["id"], cutoff, HISTORY_LIMIT),
    )
    return tuple(
        InteractionEvent(row["material_id"], row["kind"], datetime.fromtimestamp(row["latest_at"], UTC))
        for row in rows
    )


def account_avatar(account: sqlite3.Row) -> bytes | None:
    details = _db().execute(
        "SELECT avatar FROM account_details WHERE user_id = ?", (account["id"],),
    ).fetchone()
    return details["avatar"] if details is not None else None


def update_profile(
    account: sqlite3.Row,
    values: dict[str, str | None],
    *,
    details: dict[str, str] | None = None,
    avatar: bytes | None = None,
    remove_avatar: bool = False,
) -> None:
    if account["username"] == DEMO_USERNAME:
        abort(403)
    connection = _db()
    with connection:
        connection.execute(
            """UPDATE users
               SET stage = :stage, industry = :industry, experience = :experience,
                   team_size = :team_size, learning_topic = :learning_topic,
                   preferred_format = :preferred_format
               WHERE id = :user_id""",
            {**values, "user_id": account["id"]},
        )
        if details is not None:
            connection.execute(
                """INSERT INTO account_details (user_id, first_name, last_name, company, avatar, email, phone)
                   VALUES (:user_id, :first_name, :last_name, :company, :avatar, :email, :phone)
                   ON CONFLICT(user_id) DO UPDATE SET first_name = excluded.first_name,
                       last_name = excluded.last_name, company = excluded.company,
                       email = excluded.email, phone = excluded.phone,
                       avatar = CASE WHEN :replace_avatar THEN excluded.avatar ELSE account_details.avatar END""",
                {**details, "user_id": account["id"], "avatar": avatar,
                 "replace_avatar": remove_avatar or avatar is not None},
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
    with connection:
        connection.execute("DELETE FROM auth_attempts WHERE expires <= ?", (now,))
        for scope, limit in scopes:
            attempts = connection.execute(
                """INSERT INTO auth_attempts (scope, attempts, expires) VALUES (?, 1, ?)
                   ON CONFLICT(scope) DO UPDATE SET attempts = attempts + 1
                   RETURNING attempts""",
                (scope, now + ATTEMPT_WINDOW),
            ).fetchone()[0]
            if attempts > limit:
                return False
    return True


def _auth_form(mode: str, error: str = "", username: str = "", status: int = 200):
    values = profile_values(request.form) if request.method == "POST" else legacy_profile()
    return render_template(
        "auth.html", mode=mode, error=error, username=username,
        password_access_allowed=_password_access_allowed(),
        profile_fields=PROFILE_FIELDS, profile_values=values, required_profile=True,
    ), status


def _start_session(user_id: int):
    token = secrets.token_urlsafe(32)
    now = int(time())
    expires = now + int(SESSION_LIFETIME.total_seconds())
    connection = _db()
    with connection:
        connection.execute(
            "DELETE FROM account_sessions WHERE token_hash = ? OR expires <= ?",
            (_session_hash(), now),
        )
        connection.execute(
            "INSERT INTO account_sessions (token_hash, user_id, expires) VALUES (?, ?, ?)",
            (sha256(token.encode()).hexdigest(), user_id, expires),
        )
    session.clear()
    session.permanent = True
    session["account_token"] = token
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
        if username == DEMO_USERNAME:
            return _auth_form(mode, "Выберите другой логин.", username, 409)
        connection = _db()
        with connection:
            account = connection.execute(
                INSERT_USER,
                {**values, "username": username,
                 "password_hash": generate_password_hash(password, method=PASSWORD_METHOD)},
            ).fetchone()
        if account is None:
            return _auth_form(mode, "Выберите другой логин.", username, 409)
        return _start_session(account["id"])

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
    token_hash = _session_hash()
    if token_hash is not None:
        connection = _db()
        with connection:
            connection.execute("DELETE FROM account_sessions WHERE token_hash = ?", (token_hash,))
    session.clear()
    return redirect(url_for("index"), code=303)
