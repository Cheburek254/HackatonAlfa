"""Аккаунты, сессии и профили веб-демо."""

import json
import os
import re
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import digest
from pathlib import Path
from time import time
from typing import Literal

from flask import (
    Blueprint, Flask, abort, current_app, flash, g, redirect, render_template, request, session, url_for,
)
from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

from app.contracts import InteractionEvent
from app.recommender import DEFAULT_RANKING_MODE

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
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
PASSWORD_METHOD = "scrypt:32768:8:3"
DUMMY_HASH = generate_password_hash(secrets.token_hex(16), method=PASSWORD_METHOD)
ATTEMPT_WINDOW = 600
SESSION_LIFETIME = timedelta(days=7)
HISTORY_WINDOW = timedelta(days=90)
HISTORY_LIMIT = 100
DEMO_USERNAME = "demo"
DEMO_PASSWORD_CODE = "000000"
PASSWORD_CODE_LIFETIME = 600
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
CREATE TABLE IF NOT EXISTS admin_accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS recommendation_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT NOT NULL CHECK (mode IN ('popular', 'recommend', 'hybrid'))
);
CREATE TABLE IF NOT EXISTS ab_experiments (
    id INTEGER PRIMARY KEY,
    mode_a TEXT NOT NULL CHECK (mode_a IN ('popular', 'recommend', 'hybrid')),
    mode_b TEXT NOT NULL CHECK (mode_b IN ('popular', 'recommend', 'hybrid') AND mode_b <> mode_a),
    share_a INTEGER NOT NULL CHECK (share_a BETWEEN 1 AND 99),
    traffic INTEGER NOT NULL CHECK (traffic BETWEEN 1 AND 100),
    bm25_weight REAL NOT NULL CHECK (bm25_weight BETWEEN 0 AND 1),
    bm25_adaptive INTEGER NOT NULL DEFAULT 0 CHECK (bm25_adaptive IN (0, 1)),
    seed TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ab_one_running ON ab_experiments ((1)) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS ab_enrollments (
    experiment_id INTEGER NOT NULL REFERENCES ab_experiments (id),
    subject TEXT NOT NULL,
    user_id INTEGER REFERENCES users (id) ON DELETE CASCADE,
    arm TEXT NOT NULL CHECK (arm IN ('A', 'B', 'outside')),
    PRIMARY KEY (experiment_id, subject),
    UNIQUE (experiment_id, user_id)
);
CREATE TABLE IF NOT EXISTS ab_activity_days (
    experiment_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    day INTEGER NOT NULL,
    PRIMARY KEY (experiment_id, subject, day),
    FOREIGN KEY (experiment_id, subject) REFERENCES ab_enrollments (experiment_id, subject) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS course_plus_accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS password_change_codes (
    user_id INTEGER PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    email TEXT NOT NULL,
    expires INTEGER NOT NULL,
    nonce TEXT NOT NULL,
    code_digest TEXT NOT NULL,
    old_password_hash TEXT NOT NULL,
    new_password_hash TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS recommendation_impressions (
    batch_id TEXT NOT NULL,
    material_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    algorithm TEXT NOT NULL,
    result_order TEXT NOT NULL,
    audience TEXT NOT NULL CHECK (audience IN ('guest', 'demo', 'account')),
    user_id INTEGER REFERENCES users (id) ON DELETE CASCADE,
    shown_at INTEGER NOT NULL,
    clicked_at INTEGER,
    experiment_id INTEGER,
    experiment_subject TEXT,
    previewed_at INTEGER,
    PRIMARY KEY (batch_id, material_id, position)
);
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
    app.jinja_env.globals.update(password_min=PASSWORD_MIN_LENGTH, password_max=PASSWORD_MAX_LENGTH)
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
            experiment_columns = {row["name"] for row in connection.execute("PRAGMA table_info(ab_experiments)")}
            if "bm25_adaptive" not in experiment_columns:
                connection.execute("ALTER TABLE ab_experiments ADD COLUMN bm25_adaptive INTEGER NOT NULL DEFAULT 0 "
                                   "CHECK (bm25_adaptive IN (0, 1))")
            impression_columns = {row["name"] for row in connection.execute("PRAGMA table_info(recommendation_impressions)")}
            for name, statement in (
                ("experiment_id", "ALTER TABLE recommendation_impressions ADD COLUMN experiment_id INTEGER"),
                ("experiment_subject", "ALTER TABLE recommendation_impressions ADD COLUMN experiment_subject TEXT"),
                ("previewed_at", "ALTER TABLE recommendation_impressions ADD COLUMN previewed_at INTEGER"),
            ):
                if name not in impression_columns:
                    connection.execute(statement)
            connection.execute("""CREATE INDEX IF NOT EXISTS ab_impressions_by_subject
                                  ON recommendation_impressions (experiment_id, experiment_subject)
                                  WHERE experiment_id IS NOT NULL""")
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


def recommendation_mode() -> str:
    row = _db().execute("SELECT mode FROM recommendation_settings WHERE id = 1").fetchone()
    return row["mode"] if row is not None else DEFAULT_RANKING_MODE


def set_recommendation_mode(mode: str) -> None:
    with _db() as connection:
        connection.execute(
            "INSERT INTO recommendation_settings (id, mode) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET mode = excluded.mode",
            (mode,),
        )


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
                  first_name, last_name, company, email, phone, avatar IS NOT NULL AS has_avatar,
                  EXISTS (SELECT 1 FROM admin_accounts WHERE user_id = users.id) AS is_admin,
                  EXISTS (SELECT 1 FROM course_plus_accounts WHERE user_id = users.id) AS has_course_plus
           FROM account_sessions
           JOIN users ON users.id = account_sessions.user_id
           LEFT JOIN account_details ON account_details.user_id = users.id
           WHERE token_hash = ? AND expires > ? AND (username = ? OR ?)""",
        (token_hash, int(time()), DEMO_USERNAME, _password_access_allowed()),
    ).fetchone()
    if account is None:
        session.clear()
    return account


def admin_usernames() -> tuple[str, ...]:
    rows = _db().execute(
        "SELECT username FROM admin_accounts JOIN users ON users.id = user_id ORDER BY username"
    )
    return tuple(row["username"] for row in rows)


def set_course_plus(username: str, active: bool) -> bool:
    connection = _db()
    with connection:
        row = connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            return False
        if active:
            connection.execute("INSERT OR IGNORE INTO course_plus_accounts (user_id) VALUES (?)", (row["id"],))
        else:
            connection.execute("DELETE FROM course_plus_accounts WHERE user_id = ?", (row["id"],))
    return True


def grant_admin(username: str) -> bool:
    connection = _db()
    with connection:
        row = connection.execute(
            """INSERT INTO admin_accounts (user_id)
               SELECT id FROM users WHERE username = ? AND password_hash IS NOT NULL
               ON CONFLICT(user_id) DO UPDATE SET user_id = excluded.user_id
               RETURNING user_id""",
            (username,),
        ).fetchone()
    return row is not None


def create_admin_account(username: str, password: str) -> bool:
    password_hash = generate_password_hash(password, method=PASSWORD_METHOD)
    connection = _db()
    with connection:
        row = connection.execute(
            INSERT_USER, {**profile_values({}), "username": username, "password_hash": password_hash},
        ).fetchone()
        if row is None:
            return False
        connection.execute("INSERT INTO admin_accounts (user_id) VALUES (?)", (row["id"],))
    return True


def reset_admin_password(username: str, password: str) -> bool:
    password_hash = generate_password_hash(password, method=PASSWORD_METHOD)
    connection = _db()
    with connection:
        row = connection.execute(
            """UPDATE users SET password_hash = ? WHERE username = ?
               AND id IN (SELECT user_id FROM admin_accounts) RETURNING id""",
            (password_hash, username),
        ).fetchone()
        if row is None:
            return False
        connection.execute("DELETE FROM account_sessions WHERE user_id = ?", (row["id"],))
    return True


def saved_profile(account: sqlite3.Row | None) -> dict[str, str | None]:
    if account is not None:
        return profile_values(dict(account))
    return legacy_profile()


def record_interaction(
    account: sqlite3.Row | None, material_id: str, kind: Literal["view", "click"],
) -> None:
    if account is None:
        entry = [material_id, kind, int(time())]
        history = [event for event in session.get("guest_history", []) if event[:2] != entry[:2]]
        session["guest_history"] = [entry, *history[:7]]
        return
    if account["username"] == DEMO_USERNAME:
        return
    connection = _db()
    with connection:
        connection.execute(
            "INSERT INTO interaction_events (user_id, material_id, kind, occurred_at) VALUES (?, ?, ?, ?)",
            (account["id"], material_id, kind, int(time())),
        )


def save_recommendation_impressions(
    account: sqlite3.Row | None,
    placements: Sequence[tuple[str, str, int, str, str, int | None, str | None]],
    *,
    clicked: bool,
    previewed: bool = False,
) -> None:
    audience = "guest"
    user_id = None
    if account is not None:
        audience = "demo" if account["username"] == DEMO_USERNAME else "account"
        if audience == "account":
            user_id = account["id"]
    now = int(time())
    connection = _db()
    with connection:
        connection.executemany(
            """INSERT INTO recommendation_impressions (
                   batch_id, material_id, position, algorithm, result_order, experiment_id, experiment_subject,
                   audience, user_id, shown_at, clicked_at, previewed_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(batch_id, material_id, position) DO UPDATE SET
                   clicked_at = COALESCE(recommendation_impressions.clicked_at, excluded.clicked_at),
                   previewed_at = COALESCE(recommendation_impressions.previewed_at, excluded.previewed_at)""",
            [(*placement, audience, user_id, now, now if clicked else None, now if previewed else None)
             for placement in placements],
        )


def recent_interactions(account: sqlite3.Row | None) -> tuple[InteractionEvent, ...]:
    cutoff = int(time() - HISTORY_WINDOW.total_seconds())
    if account is None:
        return tuple(InteractionEvent(mid, kind, datetime.fromtimestamp(seen, UTC))
                     for mid, kind, seen in session.get("guest_history", []) if seen >= cutoff)
    if account["username"] == DEMO_USERNAME:
        return ()
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


def clear_interactions(user_id: int) -> int:
    connection = _db()
    with connection:
        return connection.execute("DELETE FROM interaction_events WHERE user_id = ?", (user_id,)).rowcount


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


def _start_session(user_id: int, *, admin_login: bool = False, attach_experiment: bool = False):
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
    if attach_experiment:
        from app.ui.experiments import attach_guest

        attach_guest(user_id)
    session.clear()
    session.permanent = True
    session["account_token"] = token
    if admin_login:
        session["admin_authenticated"] = True
        return redirect(url_for("admin.dashboard"), code=303)
    return redirect(url_for("profile_page"), code=303)


@auth.route("/login", methods=["GET", "POST"], endpoint="login")
@auth.route("/register", methods=["GET", "POST"], endpoint="register")
@auth.route("/admin/login", methods=["GET", "POST"], endpoint="admin_login")
def credentials():
    mode = request.endpoint.split(".")[-1]
    admin_login = mode == "admin_login"
    if request.method == "GET":
        return _auth_form(mode)
    if not _password_access_allowed():
        return _auth_form(mode, "Вход с паролем доступен по HTTPS.", status=403)
    check_csrf()

    username = request.form.get("username", "").strip().casefold()
    password = request.form.get("password", "")
    address = request.remote_addr or "unknown"
    valid_login = bool(LOGIN_PATTERN.fullmatch(username))
    rate_mode = "register" if mode == "register" else "login"
    scopes = ((f"{rate_mode}:ip:{address}", 25),)
    if rate_mode == "login" and valid_login:
        scopes += ((f"login:user:{username}", 8),)
    if not _consume_attempts(scopes):
        return _auth_form(mode, "Слишком много попыток. Повторите позже.", status=429)

    if mode == "register":
        if not valid_login or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
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
        return _start_session(account["id"], attach_experiment=True)

    account = _db().execute(
        """SELECT id, password_hash, EXISTS (SELECT 1 FROM admin_accounts WHERE user_id = users.id) AS is_admin
           FROM users WHERE username = ? AND password_hash IS NOT NULL""",
        (username,),
    ).fetchone() if valid_login else None
    password_hash = account["password_hash"] if account is not None else DUMMY_HASH
    if 0 < len(password) <= PASSWORD_MAX_LENGTH and check_password_hash(password_hash, password) and account is not None:
        if not admin_login or account["is_admin"]:
            return _start_session(account["id"], admin_login=admin_login, attach_experiment=not account["is_admin"])
    error = "Проверьте данные админ-аккаунта." if admin_login else "Проверьте логин и пароль."
    return _auth_form(mode, error, username[:32], 401)


@auth.post("/login/demo")
def demo_login():
    check_csrf()
    account = _db().execute("SELECT id FROM users WHERE username = ?", (DEMO_USERNAME,)).fetchone()
    return _start_session(account["id"])


@auth.post("/profile/course-plus/activate")
def activate_course_plus():
    account = current_account()
    if account is None:
        return redirect(url_for("accounts.login"), code=303)
    check_csrf()
    destination = url_for("profile_page", _anchor="course-plus")
    activate = current_app.config["COURSE_PLUS_ACTIVATOR"]
    if account["has_course_plus"]:
        message = "Подписка Курс+ уже активна."
    elif activate is None:
        message = "Это деморежим. Сервис оформления подписки Курс+ пока не подключён."
    elif not _consume_attempts(((f"course-plus:user:{account['id']}", 5),)):
        message = "Слишком много попыток. Повторите позже."
    else:
        try:
            confirmed = activate(account["id"])
        except Exception:
            current_app.logger.warning("Course Plus activation provider failed")
            message = "Не удалось проверить подписку. Повторите позже."
        else:
            if confirmed is True and set_course_plus(account["username"], active=True):
                message = "Подписка Курс+ активирована."
            else:
                message = "Сервис пока не подтвердил доступ к Курс+."
    flash(message, "course_plus")
    return redirect(destination, code=303)


def _password_code_digest(nonce: str, code: str) -> str:
    mode = "demo" if current_app.config["PASSWORD_CODE_SENDER"] is None else "email"
    return digest(current_app.secret_key.encode(), f"{mode}:{nonce}:{code}".encode(), "sha256").hex()


def password_code_pending(account: sqlite3.Row) -> bool:
    return _db().execute(
        """SELECT 1 FROM password_change_codes
           WHERE user_id = ? AND token_hash = ? AND email = ? AND expires > ?""",
        (account["id"], _session_hash(), account["email"], int(time())),
    ).fetchone() is not None


@auth.post("/profile/password/request-code")
def request_password_code():
    account = current_account()
    if account is None:
        return redirect(url_for("accounts.login"), code=303)
    if account["username"] == DEMO_USERNAME:
        abort(403)
    check_csrf()
    destination = url_for("profile_settings", _anchor="password-settings")
    email = account["email"]
    if not email:
        flash("Укажите и сохраните электронную почту в настройках профиля.", "error")
        return redirect(destination, code=303)
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    if not PASSWORD_MIN_LENGTH <= len(new) <= PASSWORD_MAX_LENGTH or new != request.form.get("confirm_password"):
        flash(f"Введите одинаковый новый пароль длиной от {PASSWORD_MIN_LENGTH} до {PASSWORD_MAX_LENGTH} символов.", "error")
        return redirect(destination, code=303)
    if not _consume_attempts(((f"password:user:{account['id']}", 8),)):
        flash("Слишком много попыток. Повторите позже.", "error")
        return redirect(destination, code=303)

    connection = _db()
    row = connection.execute("SELECT password_hash FROM users WHERE id = ?", (account["id"],)).fetchone()
    if not row or not 0 < len(current) <= PASSWORD_MAX_LENGTH or not check_password_hash(row["password_hash"], current):
        flash("Текущий пароль указан неверно.", "error")
        return redirect(destination, code=303)

    sender = current_app.config["PASSWORD_CODE_SENDER"]
    code = f"{secrets.randbelow(1_000_000):06d}" if sender is not None else DEMO_PASSWORD_CODE
    nonce = secrets.token_hex(16)
    now = int(time())
    password_hash = generate_password_hash(new, method=PASSWORD_METHOD)
    with connection:
        connection.execute("DELETE FROM password_change_codes WHERE expires <= ?", (now,))
        connection.execute(
            """INSERT INTO password_change_codes VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (user_id) DO UPDATE SET token_hash = excluded.token_hash, email = excluded.email,
                   expires = excluded.expires, nonce = excluded.nonce, code_digest = excluded.code_digest,
                   old_password_hash = excluded.old_password_hash, new_password_hash = excluded.new_password_hash""",
            (account["id"], _session_hash(), email, now + PASSWORD_CODE_LIFETIME, nonce,
             _password_code_digest(nonce, code), row["password_hash"], password_hash),
        )
    if sender is not None:
        try:
            sender(email, code)
        except Exception:
            with connection:
                connection.execute("DELETE FROM password_change_codes WHERE user_id = ? AND nonce = ?", (account["id"], nonce))
            current_app.logger.warning("Password confirmation email delivery failed")
            flash("Не удалось отправить код. Повторите запрос позже.", "error")
            return redirect(destination, code=303)
    flash("Код отправлен на почту." if sender is not None else "Демо-код готов. Письмо не отправляется.", "success")
    return redirect(destination, code=303)


@auth.post("/profile/password")
def change_password():
    account = current_account()
    if account is None:
        return redirect(url_for("accounts.login"), code=303)
    if account["username"] == DEMO_USERNAME:
        abort(403)
    check_csrf()
    destination = url_for("profile_settings", _anchor="password-settings")
    user_id, token_hash = account["id"], _session_hash()
    if not _consume_attempts(((f"password-code:user:{user_id}", 5),)):
        flash("Слишком много попыток. Повторите позже.", "error")
        return redirect(destination, code=303)
    code = request.form.get("email_code", "").strip()
    connection = _db()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        now = int(time())
        pending = connection.execute(
            """SELECT codes.* FROM password_change_codes AS codes
               JOIN account_details AS details ON details.user_id = codes.user_id AND details.email = codes.email
               JOIN account_sessions AS active ON active.token_hash = codes.token_hash AND active.user_id = codes.user_id
               WHERE codes.user_id = ? AND codes.token_hash = ? AND codes.expires > ? AND active.expires > ?""",
            (user_id, token_hash, now, now),
        ).fetchone()
        if pending is None:
            flash("Запросите новый код для этого сеанса.", "error")
            return redirect(destination, code=303)
        if not re.fullmatch(r"[0-9]{6}", code) or not secrets.compare_digest(
            pending["code_digest"], _password_code_digest(pending["nonce"], code),
        ):
            flash("Код указан неверно.", "error")
            return redirect(destination, code=303)
        connection.execute("DELETE FROM password_change_codes WHERE user_id = ?", (user_id,))
        updated = connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ? AND password_hash = ?",
            (pending["new_password_hash"], user_id, pending["old_password_hash"]),
        ).rowcount
        if not updated:
            flash("Пароль уже изменился. Запросите новый код.", "error")
            return redirect(destination, code=303)
        connection.execute("DELETE FROM account_sessions WHERE user_id = ?", (account["id"],))
        connection.execute("DELETE FROM auth_attempts WHERE scope IN (?, ?)",
                           (f"password:user:{user_id}", f"password-code:user:{user_id}"))
    session.clear()
    flash("Пароль изменён. Войдите снова.", "success")
    return redirect(url_for("accounts.login"), code=303)


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
