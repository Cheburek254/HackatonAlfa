"""Веб-демо подбора материалов."""

import os
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, url_for

from app.catalog import load_catalog, load_history
from app.contracts import Profile, Recommender
from app.ui.accounts import (
    DEMO_USERNAME, PROFILE_FIELDS, check_csrf, configure_accounts, current_account,
    profile_values, saved_profile, update_profile,
)
from app.ui.stub import recommend

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
QUICK_GOALS = ("Найти первых клиентов", "Увеличить продажи", "Разобраться в учёте")
GOAL_MAX_LENGTH = 200


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
        return response

    @app.route("/profile", methods=["GET", "POST"])
    def profile_page():
        account = current_account()
        if account is None:
            return redirect(url_for("accounts.login"), code=303)
        is_demo = account["username"] == DEMO_USERNAME
        if request.method == "POST":
            check_csrf()
            if is_demo:
                abort(403)
            update_profile(account, profile_values(request.form))
            return redirect(url_for("profile_page", saved=1), code=303)
        return render_template(
            "profile.html",
            profile_fields=PROFILE_FIELDS,
            profile_values=saved_profile(account),
            account=account,
            is_demo=is_demo,
            saved=request.args.get("saved") == "1",
        )

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
