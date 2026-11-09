"""Веб-демо подбора материалов."""

from pathlib import Path

from flask import Flask, render_template, request

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


def create_app(recommender: Recommender = recommend) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024
    catalog = load_catalog(FIXTURES / "catalog.demo.json")
    materials_by_id = {material.id: material for material in catalog}
    sample_history = load_history(FIXTURES / "history.demo.json")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/", methods=["GET", "POST"])
    def index():
        form = request.form
        quick_goal = form.get("quick_goal")
        goal = quick_goal if quick_goal in QUICK_GOALS else form.get("goal", "")
        skip_profile = form.get("action") == "skip_profile"
        values = {}
        for field, (_, options) in PROFILE_FIELDS.items():
            value = form.get(field)
            values[field] = value if value in options and not skip_profile else None
        profile = Profile(goal=goal.strip()[:GOAL_MAX_LENGTH] or None, **values)
        use_history = form.get("history") == "sample" and not skip_profile
        result = recommender(catalog, profile, sample_history if use_history else (), 3)
        cards = [(materials_by_id[item.material_id], item) for item in result.items]
        return render_template(
            "recommendations.html",
            profile=profile,
            profile_values=values,
            profile_fields=PROFILE_FIELDS,
            quick_goals=QUICK_GOALS,
            goal_max_length=GOAL_MAX_LENGTH,
            use_history=use_history,
            result=result,
            cards=cards,
        )

    return app
