"""Веб-демо подбора материалов."""

import os
from pathlib import Path

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from app.catalog import load_catalog, load_history
from app.contracts import Profile, Recommender
from app.ui.accounts import (
    DEMO_USERNAME, PROFILE_FIELDS, account_avatar, check_csrf, configure_accounts, current_account,
    profile_values, saved_profile, update_profile,
)
from app.ui.avatars import AVATAR_MAX_BYTES, AVATAR_MAX_MB, prepare_avatar
from app.ui.stub import recommend

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
QUICK_GOALS = ("Найти первых клиентов", "Увеличить продажи", "Разобраться в учёте")
GOAL_MAX_LENGTH = 200
IDENTITY_FIELDS = {
    "first_name": ("Имя", 60, "given-name"),
    "last_name": ("Фамилия", 60, "family-name"),
    "company": ("Название компании", 120, "organization"),
}


def create_app(recommender: Recommender = recommend, *, database_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024
    database_path = database_path or Path(
        os.environ.get("ALFA_DATABASE_PATH", FIXTURES.parent / "data" / "accounts" / "users.sqlite3")
    )
    configure_accounts(app, database_path)
    catalog = load_catalog(FIXTURES / "catalog.demo.json")
    materials_by_id = {material.id: material for material in catalog}
    sample_history = load_history(FIXTURES / "history.demo.json")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.after_request
    def protect_pages(response):
        if request.endpoint not in {"static", "health"}:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self'; style-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def render_profile(account, *, settings=False, values=None, details=None, error=""):
        is_demo = account["username"] == DEMO_USERNAME
        names = [account[field] for field in ("first_name", "last_name") if account[field]]
        initials = "".join(name[0] for name in names) or account["username"][:2]
        return render_template(
            "profile.html",
            account=account,
            display_name="Демо Альфа-Курс" if is_demo else " ".join(names) or account["username"],
            initials="ДА" if is_demo else initials.upper()[:2],
            is_demo=is_demo,
            settings=settings,
            profile_fields=PROFILE_FIELDS,
            profile_values=saved_profile(account) if values is None else values,
            identity_fields=IDENTITY_FIELDS,
            identity_values=account if details is None else details,
            avatar_max_mb=AVATAR_MAX_MB,
            error=error,
            saved=request.args.get("saved") == "1" and not settings,
        )

    @app.get("/profile")
    def profile_page():
        account = current_account()
        if account is None:
            return redirect(url_for("accounts.login"), code=303)
        return render_profile(account)

    @app.post("/profile", endpoint="save_legacy_profile")
    @app.route("/profile/settings", methods=["GET", "POST"])
    def profile_settings():
        account = current_account()
        if account is None:
            return redirect(url_for("accounts.login"), code=303)
        if account["username"] == DEMO_USERNAME:
            if request.method == "POST":
                abort(403)
            return redirect(url_for("profile_page"), code=303)
        if request.method == "GET":
            return render_profile(account, settings=True)
        if request.endpoint == "save_legacy_profile":
            check_csrf()
            update_profile(account, profile_values(request.form))
            return redirect(url_for("profile_page", saved=1), code=303)

        request.max_content_length = AVATAR_MAX_BYTES + 16 * 1024
        request.max_form_parts = 16
        check_csrf()
        values = profile_values(request.form)
        details = {field: request.form.get(field, "").strip() for field in IDENTITY_FIELDS}
        upload = request.files.get("avatar")
        remove_avatar = request.form.get("remove_avatar") == "1"
        try:
            for field, (label, maximum, _) in IDENTITY_FIELDS.items():
                value = details[field]
                if value and (len(value) > maximum or not value.isprintable()):
                    raise ValueError(f"Проверьте поле «{label}». Максимум {maximum} символов.")
            if upload and remove_avatar:
                raise ValueError("Выберите одно действие: загрузить новое фото или удалить текущее.")
            avatar = prepare_avatar(upload) if upload else None
        except ValueError as error:
            return render_profile(account, settings=True, values=values, details=details, error=str(error)), 400
        update_profile(account, values, details=details, avatar=avatar, remove_avatar=remove_avatar)
        return redirect(url_for("profile_page", saved=1), code=303)

    @app.get("/profile/avatar")
    def profile_avatar():
        account = current_account()
        avatar = account_avatar(account) if account is not None and account["has_avatar"] else None
        if avatar is None:
            abort(404)
        return Response(avatar, mimetype="image/jpeg")

    @app.route("/", methods=["GET", "POST"])
    def index():
        form = request.form
        quick_goal = form.get("quick_goal")
        goal = quick_goal if quick_goal in QUICK_GOALS else form.get("goal", "")
        skip_profile = form.get("action") == "skip_profile"
        stored_profile = saved_profile(current_account())
        values = profile_values({} if skip_profile else {**stored_profile, **form})
        profile = Profile(goal=goal.strip()[:GOAL_MAX_LENGTH] or None, **values)
        use_history = form.get("history") == "sample" and not skip_profile
        result = recommender(catalog, profile, sample_history if use_history else (), 3)
        cards = [(materials_by_id[item.material_id], item) for item in result.items]
        return render_template(
            "recommendations.html",
            profile=profile,
            profile_values=values,
            profile_fields=PROFILE_FIELDS,
            show_profile_details=use_history or (not skip_profile and values != stored_profile),
            quick_goals=QUICK_GOALS,
            goal_max_length=GOAL_MAX_LENGTH,
            use_history=use_history,
            result=result,
            cards=cards,
        )

    return app
