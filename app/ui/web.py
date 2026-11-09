"""Веб-демо подбора материалов."""

import json
from collections.abc import Mapping
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

from app.catalog import load_catalog, load_history
from app.contracts import Profile, Recommender
from app.ui.stub import recommend

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
QUICK_GOALS = ("Найти первых клиентов", "Увеличить продажи", "Разобраться в учёте")
PROFILE_FIELDS = {
    "stage": ("Стадия бизнеса", ("запуск", "работающий бизнес", "рост")),
    "industry": ("Сфера", ("розница", "услуги", "другое")),
    "experience": ("Опыт", ("начинающий", "опытный")),
}
GOAL_MAX_LENGTH = 200
PROFILE_COOKIE = "alfahack_profile"
PROFILE_MAX_AGE = 180 * 24 * 60 * 60


def _profile_values(values: Mapping[str, object]) -> dict[str, str | None]:
    profile = {}
    for field, (_, options) in PROFILE_FIELDS.items():
        value = values.get(field)
        profile[field] = value if isinstance(value, str) and value in options else None
    return profile


def _saved_profile() -> dict[str, str | None]:
    try:
        values = json.loads(request.cookies.get(PROFILE_COOKIE, "{}"))
    except ValueError:
        values = {}
    return _profile_values(values if isinstance(values, dict) else {})


def create_app(recommender: Recommender = recommend) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024
    catalog = load_catalog(FIXTURES / "catalog.demo.json")
    materials_by_id = {material.id: material for material in catalog}
    sample_history = load_history(FIXTURES / "history.demo.json")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/profile", methods=["GET", "POST"])
    def profile_page():
        if request.method == "POST":
            profile = _profile_values(request.form)
            response = redirect(url_for("profile_page", saved=1), code=303)
            if any(profile.values()):
                response.set_cookie(
                    PROFILE_COOKIE,
                    json.dumps(profile),
                    max_age=PROFILE_MAX_AGE,
                    httponly=True,
                    secure=request.is_secure,
                    samesite="Lax",
                )
            else:
                response.delete_cookie(PROFILE_COOKIE)
            return response
        return render_template(
            "profile.html",
            profile_fields=PROFILE_FIELDS,
            profile_values=_saved_profile(),
            saved=request.args.get("saved") == "1",
        )

    @app.route("/", methods=["GET", "POST"])
    def index():
        form = request.form
        quick_goal = form.get("quick_goal")
        goal = quick_goal if quick_goal in QUICK_GOALS else form.get("goal", "")
        skip_profile = form.get("action") == "skip_profile"
        saved_profile = _saved_profile()
        values = _profile_values({} if skip_profile else {**saved_profile, **form})
        profile = Profile(goal=goal.strip()[:GOAL_MAX_LENGTH] or None, **values)
        use_history = form.get("history") == "sample" and not skip_profile
        result = recommender(catalog, profile, sample_history if use_history else (), 3)
        cards = [(materials_by_id[item.material_id], item) for item in result.items]
        return render_template(
            "recommendations.html",
            profile=profile,
            profile_values=values,
            profile_fields=PROFILE_FIELDS,
            show_profile_details=use_history or (not skip_profile and values != saved_profile),
            quick_goals=QUICK_GOALS,
            goal_max_length=GOAL_MAX_LENGTH,
            use_history=use_history,
            result=result,
            cards=cards,
        )

    return app
