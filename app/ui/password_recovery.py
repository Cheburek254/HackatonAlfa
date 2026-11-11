"""Восстановление пароля по одноразовому коду на сохранённую почту."""

import os
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from hmac import compare_digest, digest
from time import time

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

from app.ui.accounts import (
    DEMO_PASSWORD_CODE, LOGIN_PATTERN, PASSWORD_CODE_LIFETIME, PASSWORD_CODE_PATTERN,
    PASSWORD_MAX_LENGTH, PASSWORD_METHOD, PASSWORD_MIN_LENGTH,
    _consume_attempts, _db, _password_access_allowed, auth, check_csrf,
)

SESSION_KEY = "password_recovery"


def smtp_code_sender():
    host = os.environ.get("ALFA_SMTP_HOST", "").strip()
    sender = os.environ.get("ALFA_SMTP_FROM", "").strip()
    if not host or not sender:
        return None
    port = int(os.environ.get("ALFA_SMTP_PORT", "587"))
    user = os.environ.get("ALFA_SMTP_USER", "")
    password = os.environ.get("ALFA_SMTP_PASSWORD", "")

    def send(recipient, code):
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = "Код подтверждения Альфа-Курс"
        message.set_content(f"Код подтверждения: {code}. Он действует 10 минут. "
                            "Если вы не запрашивали код, проигнорируйте это письмо.")
        tls = ssl.create_default_context()
        connection = (smtplib.SMTP_SSL(host, port, timeout=8, context=tls) if port == 465
                      else smtplib.SMTP(host, port, timeout=8))
        with connection as mail:
            if port != 465:
                mail.starttls(context=tls)
            if user:
                mail.login(user, password)
            mail.send_message(message)

    return send


def _digest(request_id, code):
    return digest(current_app.secret_key.encode(), f"recovery:{request_id}:{code}".encode(), "sha256").hex()


def _screen(*, confirm=False, error=None, status=200):
    return render_template("password_recovery.html", confirm=confirm, error=error,
                           available=_password_access_allowed(),
                           demo=current_app.config["PASSWORD_CODE_SENDER"] is None), status


@auth.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return _screen()
    if not _password_access_allowed():
        abort(403)
    check_csrf()
    sender = current_app.config["PASSWORD_CODE_SENDER"]
    username = request.form.get("username", "").strip().casefold()
    email = request.form.get("email", "").strip().casefold()
    if not LOGIN_PATTERN.fullmatch(username) or not 3 <= len(email) <= 120 or "@" not in email:
        return _screen(error="Укажите логин и сохранённую электронную почту.", status=400)
    if sender is None:
        session[SESSION_KEY] = "demo"
        return redirect(url_for("accounts.recover_password"), code=303)
    if not _consume_attempts(((f"recovery:ip:{request.remote_addr}", 30), (f"recovery:user:{username}", 4))):
        return _screen(error="Слишком много запросов. Попробуйте позже.", status=429)

    request_id = secrets.token_urlsafe(24)
    session[SESSION_KEY] = request_id
    connection = _db()
    account = connection.execute("""SELECT users.id, users.password_hash, account_details.email
        FROM users JOIN account_details ON account_details.user_id = users.id
        WHERE users.username = ? AND users.password_hash IS NOT NULL AND account_details.email = ? COLLATE NOCASE""",
        (username, email)).fetchone()
    if account:
        code = str(100_000 + secrets.randbelow(900_000))
        now = int(time())
        with connection:
            connection.execute("DELETE FROM password_recovery_codes WHERE expires <= ? OR user_id = ?", (now, account["id"]))
            connection.execute("INSERT INTO password_recovery_codes VALUES (?, ?, ?, ?, ?, ?)",
                               (request_id, account["id"], account["email"], now + PASSWORD_CODE_LIFETIME,
                                _digest(request_id, code), account["password_hash"]))
        try:
            sender(account["email"], code)
        except Exception:
            current_app.logger.warning("Password recovery email delivery failed")
            with connection:
                connection.execute("DELETE FROM password_recovery_codes WHERE request_id = ?", (request_id,))
    return redirect(url_for("accounts.recover_password"), code=303)


@auth.route("/reset-password", methods=["GET", "POST"])
def recover_password():
    request_id = session.get(SESSION_KEY)
    demo = current_app.config["PASSWORD_CODE_SENDER"] is None
    if not isinstance(request_id, str) or (request_id == "demo") != demo:
        return redirect(url_for("accounts.forgot_password"), code=303)
    if request.method == "GET":
        return _screen(confirm=True)
    if not _password_access_allowed():
        abort(403)
    check_csrf()
    code = request.form.get("email_code", "")
    password = request.form.get("new_password", "")
    if not PASSWORD_CODE_PATTERN.fullmatch(code) or not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH or (
        password != request.form.get("confirm_password")
    ):
        return _screen(confirm=True, error="Проверьте код и новый пароль.", status=400)
    code = code.replace(" ", "")
    if demo:
        if not compare_digest(code, DEMO_PASSWORD_CODE):
            return _screen(confirm=True, error="Для демо введите код 000 000.", status=400)
        session.pop(SESSION_KEY, None)
        flash("Демо восстановления завершено. Для входа используйте прежний пароль.", "success")
        return redirect(url_for("accounts.login"), code=303)
    if not _consume_attempts(((f"recovery:code:{request_id}", 5),)):
        return _screen(confirm=True, error="Слишком много попыток. Запросите новый код.", status=429)

    connection = _db()
    invalid = "Код неверен или истёк. Проверьте письмо или запросите новый код."
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute("""SELECT r.* FROM password_recovery_codes AS r
            JOIN users ON users.id = r.user_id JOIN account_details ON account_details.user_id = r.user_id
            WHERE r.request_id = ? AND r.expires > ? AND users.password_hash = r.old_password_hash
              AND account_details.email = r.email""", (request_id, int(time()))).fetchone()
        if not pending or not compare_digest(pending["code_digest"], _digest(request_id, code)):
            return _screen(confirm=True, error=invalid, status=400)
        user_id = pending["user_id"]
        password_hash = generate_password_hash(password, method=PASSWORD_METHOD)
        connection.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        connection.execute("DELETE FROM password_recovery_codes WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM password_change_codes WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM account_sessions WHERE user_id = ?", (user_id,))
    session.clear()
    flash("Пароль обновлён. Войдите с новым паролем.", "success")
    return redirect(url_for("accounts.login"), code=303)
