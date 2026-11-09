"""Закрытая сводка по рекомендациям и управление доступом к ней."""

import click
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from app.ctr_report import ORDER_LABELS, Metrics, read_report
from app.ui.accounts import (
    DEMO_USERNAME, LOGIN_PATTERN, PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, admin_usernames, check_csrf,
    create_admin_account, current_account, grant_admin, reset_admin_password,
)

admin = Blueprint("admin", __name__, url_prefix="/admin")
PASSWORD_HINT = f"Пароль должен содержать от {PASSWORD_MIN_LENGTH} до {PASSWORD_MAX_LENGTH} символов."


@admin.before_request
def require_admin():
    account = current_account()
    if account is None:
        return redirect(url_for("accounts.admin_login"), code=303)
    if not account["is_admin"]:
        abort(403)
    if not session.get("admin_authenticated"):
        return redirect(url_for("accounts.admin_login"), code=303)


@admin.get("")
def dashboard():
    totals, details = read_report(current_app.config["ACCOUNT_DATABASE"])
    overview = Metrics(sum(row.impressions for row in totals.values()), sum(row.clicks for row in totals.values()))
    return render_template(
        "admin.html", overview=overview, totals=totals, details=details, order_labels=ORDER_LABELS,
        usernames=admin_usernames(), password_hint=PASSWORD_HINT,
        password_min=PASSWORD_MIN_LENGTH, password_max=PASSWORD_MAX_LENGTH,
    )


@admin.post("/accounts")
def manage_accounts():
    check_csrf()
    username = request.form.get("username", "").strip().casefold()
    action = request.form.get("action")
    if action not in {"grant", "create"}:
        abort(400)
    if not LOGIN_PATTERN.fullmatch(username) or username == DEMO_USERNAME:
        flash("Укажите логин личного аккаунта из 3–32 латинских букв, цифр или символов . _ -.", "error")
    elif action == "grant":
        if grant_admin(username):
            flash(f"Админ-роль выдана пользователю {username}.", "success")
        else:
            flash("Пользователь с таким логином не найден.", "error")
    else:
        password = request.form.get("password", "")
        if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
            flash(PASSWORD_HINT, "error")
        elif create_admin_account(username, password):
            flash(f"Админ-аккаунт {username} создан.", "success")
        else:
            flash("Этот логин уже занят. Существующему аккаунту можно выдать роль отдельно.", "error")
    return redirect(url_for("admin.dashboard", _anchor="admin-accounts"), code=303)


@admin.cli.command("grant")
@click.argument("username")
def grant_from_console(username: str) -> None:
    """Выдать роль существующему аккаунту."""
    if not grant_admin(username.strip().casefold()):
        raise click.ClickException("Личный аккаунт с таким логином не найден.")
    click.echo("Админ-роль выдана.")


@admin.cli.command("reset-password")
@click.argument("username")
def reset_password_from_console(username: str) -> None:
    """Задать новый пароль админа и завершить его предыдущие сессии."""
    password = click.prompt("Новый пароль", hide_input=True, confirmation_prompt=True)
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise click.ClickException(PASSWORD_HINT)
    if not reset_admin_password(username.strip().casefold(), password):
        raise click.ClickException("Админ-аккаунт с таким логином не найден.")
    click.echo("Пароль обновлён. Предыдущие сессии завершены.")
