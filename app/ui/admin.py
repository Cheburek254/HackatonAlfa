"""Закрытая сводка по рекомендациям и управление доступом к ней."""

from datetime import UTC, datetime, timedelta, timezone

import click
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from app.ctr_report import LEGACY_ALGORITHMS, ORDER_LABELS, Metrics, read_daily_activity, read_report, read_top_cards
from app.live_data import live_payload
from app.recommender import RANKING_MODES
from app.ui.accounts import (
    DEMO_USERNAME, LOGIN_PATTERN, PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH, admin_usernames, check_csrf,
    clear_interactions, create_admin_account, current_account, grant_admin, recommendation_mode, reset_admin_password,
    set_course_plus, set_recommendation_mode,
)
from app.ui.admin_summary import build_assessments
from app.ui.experiments import end_experiment, experiments, group_metrics, running_experiment, start_experiment
from evaluation.catalog_quality import read_report as read_catalog_quality
from evaluation.learning_report import read_report as read_learning_report
from evaluation.runner import SUITE_LABELS, read_summary
from evaluation.stress_report import read_report as read_stress_report

admin = Blueprint("admin", __name__, url_prefix="/admin")
PASSWORD_HINT = f"Пароль должен содержать от {PASSWORD_MIN_LENGTH} до {PASSWORD_MAX_LENGTH} символов."
MSK = timezone(timedelta(hours=3))


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
    weight = config["BM25_WEIGHT"]
    return {
        "catalog_quality": read_catalog_quality(config["CATALOG_QUALITY_PATH"], fingerprint, weight),
        "stress_report": read_stress_report(config["STRESS_REPORT_PATH"], fingerprint, weight),
        "learning_report": read_learning_report(config["LEARNING_REPORT_PATH"], fingerprint, weight),
    }


def _ranking_summary(suite):
    directory = current_app.config["EVALUATION_SUMMARY_DIR"]
    if directory is None:
        return None
    fingerprint = current_app.config["CATALOG_QUALITY_FINGERPRINT"] if suite == "real" else None
    return read_summary(directory / f"ranking-{suite}.json", suite, current_app.config["BM25_WEIGHT"], fingerprint)


def _report_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _latest_report_time(reports):
    return max((value for report in reports if report and (value := _report_datetime(report.get("generated_at")))),
               default=None)


def _msk(value: datetime) -> str:
    return value.astimezone(MSK).strftime("%d.%m.%Y %H:%M МСК")


@admin.app_template_filter("report_msk")
def report_msk(value):
    instant = _report_datetime(value)
    return _msk(instant) if instant else None


@admin.get("")
def dashboard():
    if "category" in request.args:
        return redirect(url_for("admin.analytics", category=request.args["category"], _anchor="admin-cards"), code=303)
    totals, _ = read_report(current_app.config["ACCOUNT_DATABASE"])
    overview = Metrics(sum(row.impressions for row in totals.values()), sum(row.clicks for row in totals.values()))
    reports = _test_reports()
    real_summary = _ranking_summary("real")
    latest = _latest_report_time((*reports.values(), real_summary, _ranking_summary("demo")))
    return render_template(
        "admin.html", heading="Сводка рекомендаций", intro="Основные показатели и оценка последних офлайн-прогонов.",
        overview=overview,
        experiment_running=running_experiment() is not None,
        ranking_mode=recommendation_mode(), ranking_modes=RANKING_MODES,
        assessments=build_assessments(
            reports["catalog_quality"], reports["stress_report"], reports["learning_report"], real_summary,
        ),
        report_date=_msk(latest) if latest else None,
    )


@admin.post("/recommendation-mode")
def change_recommendation_mode():
    check_csrf()
    mode = request.form.get("mode")
    if mode not in RANKING_MODES:
        abort(400)
    set_recommendation_mode(mode)
    flash(f"Общий режим «{RANKING_MODES[mode]}» сохранён. Участники действующего A/B-теста остаются в своих группах.",
          "ranking_success")
    return redirect(url_for("admin.dashboard", _anchor="admin-ranking-mode"), code=303)


@admin.route("/experiments", methods=["GET", "POST"])
def experiment_dashboard():
    if request.method == "POST":
        check_csrf()
        action = request.form.get("action")
        if action == "start":
            mode_a, mode_b = request.form.get("mode_a"), request.form.get("mode_b")
            share_a = request.form.get("share_a", type=int)
            traffic = request.form.get("traffic", type=int)
            if (mode_a not in RANKING_MODES or mode_b not in RANKING_MODES or mode_a == mode_b
                    or share_a not in range(1, 100) or traffic not in range(1, 101)):
                flash("Выберите два разных алгоритма и проверьте проценты трафика.", "experiment_error")
            elif start_experiment(mode_a, mode_b, share_a, traffic, current_app.config["BM25_WEIGHT"]):
                flash("A/B-тест запущен. Новые запросы распределяются по группам.", "experiment_success")
            else:
                flash("Завершите текущий запуск перед созданием нового.", "experiment_error")
            return redirect(url_for("admin.experiment_dashboard"), code=303)
        if action == "stop":
            experiment_id = request.form.get("experiment_id", type=int)
            if experiment_id is None or not 0 < experiment_id < 2**63:
                abort(400)
            if end_experiment(experiment_id):
                flash("Эксперимент завершён. Выдача использует общий алгоритм, отчёт сохранён.", "experiment_success")
            else:
                flash("Этот эксперимент уже завершён или отсутствует.", "experiment_error")
            return redirect(url_for("admin.experiment_dashboard"), code=303)
        abort(400)
    runs = experiments()
    active = next((run for run in runs if run["ended_at"] is None), None)
    selected_id = request.args.get("id", type=int)
    selected = next((run for run in runs if run["id"] == selected_id), None) if selected_id else active or (runs[0] if runs else None)
    if selected_id and selected is None:
        abort(404)
    return render_template(
        "admin_experiments.html", heading="A/B-тестирование",
        intro="Распределение живого трафика между алгоритмами и сравнение результатов.",
        active=active, selected=selected, last_run=runs[0] if runs else None, runs=runs, modes=RANKING_MODES,
        metrics=group_metrics(selected["id"]) if selected else None,
        format_time=lambda value: _msk(datetime.fromtimestamp(value, UTC)),
    )


@admin.get("/tests")
def tests():
    summaries = {suite: snapshot for suite in SUITE_LABELS if (snapshot := _ranking_summary(suite))}
    reports = _test_reports()
    latest = _latest_report_time((*reports.values(), *summaries.values()))
    detailed_suite = request.args.get("details")
    return render_template(
        "admin_tests.html", heading="Тесты рекомендаций",
        intro="Сохранённые результаты последних серверных прогонов.",
        report_date=_msk(latest) if latest else None, titles=current_app.config["CATALOG_TITLES"],
        evaluation_suites=SUITE_LABELS, detailed_suite=detailed_suite,
        bm25_weight=current_app.config["BM25_WEIGHT"], comparison_summaries=summaries,
        subscription_check=(reports["catalog_quality"] or {}).get("subscription_examples"), **reports,
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
        real_catalog_available=current_app.config["EVALUATION_SUMMARY_DIR"] is not None,
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
