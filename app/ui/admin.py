"""Закрытая сводка по рекомендациям и управление доступом к ней."""

from datetime import UTC, datetime, timedelta

import click
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from app.ctr_report import LEGACY_ALGORITHMS, ORDER_LABELS, Metrics, read_daily_activity, read_report, read_top_cards
from app.live_data import live_payload
from app.ui.accounts import (
    DEMO_USERNAME, LOGIN_PATTERN, PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, admin_usernames, check_csrf,
    clear_interactions, create_admin_account, current_account, grant_admin, reset_admin_password, set_course_plus,
)
from app.ui.admin_summary import build_assessments
from evaluation.catalog_quality import read_report as read_catalog_quality
from evaluation.learning_report import read_report as read_learning_report
from evaluation.runner import EvaluationDataError, SUITE_LABELS, evaluation_scenario_counts, run_evaluation
from evaluation.stress_report import check_subscriptions, read_report as read_stress_report

admin = Blueprint("admin", __name__, url_prefix="/admin")
PASSWORD_HINT = f"Пароль должен содержать от {PASSWORD_MIN_LENGTH} до {PASSWORD_MAX_LENGTH} символов."


@admin.before_request
def require_admin():
    account = current_account()
    if request.endpoint == "admin.live_data":
        if account is None or (account["is_admin"] and not session.get("admin_authenticated")):
            return {"version": 1, "status": "unauthenticated"}, 401
        if not account["is_admin"]:
            return {"version": 1, "status": "forbidden"}, 403
    if account is None:
        return redirect(url_for("accounts.admin_login"), code=303)
    if not account["is_admin"]:
        abort(403)
    if not session.get("admin_authenticated"):
        return redirect(url_for("accounts.admin_login"), code=303)
    g.admin_account = account


def _test_reports():
    config = current_app.config
    fingerprint = config["CATALOG_QUALITY_FINGERPRINT"]
    return {
        "catalog_quality": read_catalog_quality(config["CATALOG_QUALITY_PATH"], fingerprint),
        "stress_report": read_stress_report(config["STRESS_REPORT_PATH"], fingerprint),
        "learning_report": read_learning_report(config["LEARNING_REPORT_PATH"], fingerprint),
    }


@admin.route("", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        return redirect(url_for("admin.tests", _anchor="admin-evaluation"), code=307)
    if "category" in request.args:
        return redirect(url_for("admin.analytics", category=request.args["category"], _anchor="admin-cards"), code=303)
    totals, _ = read_report(current_app.config["ACCOUNT_DATABASE"])
    overview = Metrics(sum(row.impressions for row in totals.values()), sum(row.clicks for row in totals.values()))
    reports = _test_reports()
    return render_template(
        "admin.html", heading="Сводка рекомендаций", intro="Основные показатели и оценка последних офлайн-прогонов.",
        overview=overview,
        assessments=build_assessments(reports["catalog_quality"], reports["stress_report"], reports["learning_report"]),
        report_date=max((report["generated_at"][:10] for report in reports.values() if report), default=None),
    )


@admin.route("/tests", methods=["GET", "POST"])
def tests():
    comparison = evaluation_suite = evaluation_error = None
    real_catalog = current_app.config["EVALUATION_REAL_CATALOG"]
    if request.method == "POST":
        check_csrf()
        evaluation_suite = request.form.get("evaluation_suite", "demo")
        if evaluation_suite not in SUITE_LABELS:
            abort(400)
        try:
            comparison = run_evaluation(
                current_app.config["EVALUATION_RECOMMENDERS"][evaluation_suite],
                suite=evaluation_suite, real_catalog=real_catalog,
            )
        except EvaluationDataError as error:
            evaluation_error = str(error)
    return render_template(
        "admin_tests.html", heading="Тесты рекомендаций",
        intro="Прогоны, метрики по наборам, примеры сценариев и проверка прав доступа.",
        comparison=comparison, titles=current_app.config["CATALOG_TITLES"],
        evaluation_suites=SUITE_LABELS, evaluation_suite=evaluation_suite, evaluation_error=evaluation_error,
        evaluation_counts=evaluation_scenario_counts(),
        subscription_check=check_subscriptions(real_catalog) if real_catalog else None,
        real_catalog_available=bool(real_catalog), **_test_reports(),
    )


@admin.get("/analytics")
def analytics():
    database = current_app.config["ACCOUNT_DATABASE"]
    all_totals, all_details = read_report(database, include_legacy=True)
    legacy_totals = {name: metrics for name, metrics in all_totals.items() if name in LEGACY_ALGORITHMS}
    totals = {name: metrics for name, metrics in all_totals.items() if name not in LEGACY_ALGORITHMS}
    details = [row for row in all_details if row[0] not in LEGACY_ALGORITHMS]
    activity = read_daily_activity(database)
    formats = current_app.config["CATALOG_FORMAT_LABELS"]
    category = request.args.get("category", "all")
    if category not in formats:
        category = "all"
    material_ids = [mid for mid, fmt in current_app.config["CATALOG_FORMATS"].items() if category == "all" or fmt == category]
    return render_template(
        "admin_analytics.html", heading="Статистика выдачи", intro="Показы и переходы гостей и личных аккаунтов.",
        totals=totals, details=details, order_labels=ORDER_LABELS,
        legacy_totals=legacy_totals,
        top_cards=read_top_cards(database, material_ids), titles=current_app.config["CATALOG_TITLES"],
        top_formats=formats, top_category=category,
        real_catalog_available=bool(current_app.config["EVALUATION_REAL_CATALOG"]),
        impressions_by_day=[(day, metrics.impressions) for day, metrics in activity],
        clicks_by_day=[(day, metrics.clicks) for day, metrics in activity],
        ctr_by_day=[(day, metrics.ctr) for day, metrics in activity],
    )


@admin.get("/settings")
def settings():
    return render_template(
        "admin_settings.html", heading="Управление", intro="Административные роли и история текущего аккаунта.",
        usernames=admin_usernames(), password_hint=PASSWORD_HINT,
        password_min=PASSWORD_MIN_LENGTH, password_max=PASSWORD_MAX_LENGTH,
    )


@admin.get("/roadmap")
def roadmap():
    return render_template(
        "admin_roadmap.html", heading="Развитие системы",
        intro="Варианты архитектуры для аналитики, рекомендаций и эксплуатации.",
        live_configured=current_app.config["LIVE_DATA_PROVIDER"] is not None,
    )


@admin.get("/api/v1/live-data")
def live_data():
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        days = 0
    if not 1 <= days <= 90:
        return {"version": 1, "status": "invalid_days", "message": "days должен быть от 1 до 90"}, 400
    provider = current_app.config["LIVE_DATA_PROVIDER"]
    if provider is None:
        return {"version": 1, "status": "not_configured", "message": "Источник live-данных ещё не подключён."}, 503
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days - 1)
    try:
        return live_payload(provider(start, end), start, end)
    except Exception:
        current_app.logger.warning("Live data provider failed")
        return {"version": 1, "status": "source_unavailable", "message": "Источник live-данных недоступен."}, 502


@admin.post("/history/clear")
def clear_own_history():
    check_csrf()
    if request.form.get("confirm") != "1":
        flash("Подтвердите очистку истории.", "history_error")
    else:
        deleted = clear_interactions(g.admin_account["id"])
        flash(f"История очищена. Удалено событий: {deleted}.", "history_success")
    return redirect(url_for("admin.settings", _anchor="admin-history"), code=303)


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
    return redirect(url_for("admin.settings", _anchor="admin-accounts"), code=303)


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


@admin.cli.command("course-plus")
@click.argument("username")
@click.option("--revoke", is_flag=True, help="Отозвать доступ к Курс+.")
def course_plus_from_console(username: str, revoke: bool) -> None:
    """Управлять доступом существующего аккаунта к Курс+."""
    if not set_course_plus(username.strip().casefold(), active=not revoke):
        raise click.ClickException("Аккаунт с таким логином не найден.")
    click.echo("Доступ к Курс+ отозван." if revoke else "Доступ к Курс+ выдан.")
