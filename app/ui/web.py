"""Веб-демо подбора материалов."""

import re
from pathlib import Path
from functools import partial
from urllib.parse import urlsplit

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from app.catalog import DEMO_CATALOG_PATH, catalog_source, load_catalog, load_history
from app.contracts import Material, Profile, Recommender
from app.ctr_report import ORDER_LABELS
from app.database import account_database_path
from app.ui.accounts import (
    DEMO_USERNAME, PROFILE_FIELDS, account_avatar, check_csrf, configure_accounts, csrf_token, current_account,
    profile_values, recent_interactions, record_interaction, saved_profile, update_profile,
)
from app.ui.admin import admin
from app.ui.avatars import AVATAR_MAX_BYTES, AVATAR_MAX_MB, prepare_avatar
from app.ui.descriptions import format_description_html
from app.ui.impressions import issue_impression_tickets, record_impressions
from app.recommender import recommend

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
QUICK_GOALS = ("Найти первых клиентов", "Увеличить продажи", "Разобраться в учёте")
GOAL_MAX_LENGTH = 200
RESULTS_PER_PAGE = 6
PAGE_SIZES = (6, 12, 24)

FORMAT_LABELS = {
    "all": "Все форматы",
    "курс": "Курсы",
    "статья": "Статьи",
    "шаблон": "Шаблоны",
    "чек-лист": "Чек-листы",
    "вебинар": "Вебинары",
    "подкаст": "Подкасты",
    "калькулятор": "Калькуляторы",
}
IDENTITY_FIELDS = {
    "first_name": ("Имя", 60, "given-name"),
    "last_name": ("Фамилия", 60, "family-name"),
    "company": ("Название компании", 120, "organization"),
}
CONTACT_FIELDS = {
    "email": ("Электронная почта", 254, "email"),
    "phone": ("Телефон", 40, "tel"),
}


def _material_destination(url: str) -> str:
    try:
        target = urlsplit(url)
    except ValueError:
        abort(404)
    if target.scheme not in {"http", "https"} or not target.hostname:
        abort(404)
    return url


def _format_key(material: Material) -> str | None:
    if material.entity_type == "calculator":
        return "калькулятор"
    try:
        if urlsplit(material.url).path.startswith("/calculators/"):
            return "калькулятор"
    except ValueError:
        pass
    return material.format


def create_app(
    recommender: Recommender = recommend, *, database_path: Path | None = None, algorithm_name: str | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024
    source = catalog_source()
    is_demo_catalog = source == DEMO_CATALOG_PATH
    catalog = load_catalog(source)
    if not is_demo_catalog and not catalog:
        raise ValueError(f"Каталог пуст: {source}")
    materials_by_id = {material.id: material for material in catalog}
    app.config["CATALOG_TITLES"] = {material.id: material.title for material in catalog}
    app.config["EVALUATION_RECOMMENDER"] = (
        partial(recommend, explain_with_llm=False) if recommender is recommend else recommender
    )
    sample_history = load_history(FIXTURES / "history.demo.json") if is_demo_catalog else ()
    configure_accounts(app, database_path or account_database_path())
    app.register_blueprint(admin)
    recommender_type = type(recommender)
    algorithm = algorithm_name or (
        f"{getattr(recommender, '__module__', recommender_type.__module__)}."
        f"{getattr(recommender, '__qualname__', recommender_type.__qualname__)}"
    )
    if algorithm_name is None and recommender is recommend and is_demo_catalog:
        algorithm += ":demo-catalog"
    app.jinja_env.globals["signed_in"] = lambda: current_account() is not None
    app.jinja_env.globals["material_format"] = _format_key

    @app.get("/health")
    def health():
        return {"status": "ok", "catalog": "demo" if is_demo_catalog else "real", "materials": len(catalog)}

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
            if request.endpoint == "index":
                response.headers["Content-Security-Policy"] += "; script-src 'self'; connect-src 'self'"
            elif request.endpoint in {"accounts.login", "accounts.register"}:
                response.headers["Content-Security-Policy"] += "; script-src 'self'"
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
            contact_fields=CONTACT_FIELDS,
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
        details.update({field: request.form.get(field, account[field] or "").strip() for field in CONTACT_FIELDS})
        upload = request.files.get("avatar")
        remove_avatar = request.form.get("remove_avatar") == "1"
        try:
            for field, (label, maximum, _) in (IDENTITY_FIELDS | CONTACT_FIELDS).items():
                value = details[field]
                if value and (len(value) > maximum or not value.isprintable()):
                    raise ValueError(f"Проверьте поле «{label}». Максимум {maximum} символов.")
            if details["email"] and not re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", details["email"]):
                raise ValueError("Проверьте адрес электронной почты.")
            phone = details["phone"]
            if phone and (not re.fullmatch(r"\+?[0-9() .-]+", phone) or not 7 <= len(re.findall(r"[0-9]", phone)) <= 15):
                raise ValueError("Укажите телефон с 7–15 цифрами.")
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

    @app.get("/materials/<material_id>")
    def material_page(material_id):
        material = materials_by_id.get(material_id)
        if material is None:
            abort(404)
        if request.method == "GET" and not any(
            "prefetch" in request.headers.get(header, "").lower() for header in ("Sec-Purpose", "Purpose")
        ):
            record_interaction(current_account(), material_id, "view")
        impression = request.args.get("impression")
        open_url = url_for("open_material", material_id=material_id, impression=impression)
        description_html = format_description_html(material.description)
        if request.accept_mimetypes.best == "application/json":
            return {
                "title": material.title,
                "description": material.description,
                "description_html": description_html,
                "url": _material_destination(material.url),
                "open_url": open_url,
                "csrf_token": csrf_token(),
            }
        return render_template(
            "material.html", material=material, description_html=description_html, open_url=open_url
        )

    @app.post("/materials/<material_id>/open")
    def open_material(material_id):
        material = materials_by_id.get(material_id)
        if material is None:
            abort(404)
        target = _material_destination(material.url)
        check_csrf()
        account = current_account()
        record_interaction(account, material_id, "click")
        record_impressions((request.args.get("impression"),), account, clicked_material=material_id)
        if request.accept_mimetypes.best == "application/json":
            return "", 204
        return redirect(target, code=303)

    @app.post("/recommendations/impressions")
    def recommendation_impressions():
        check_csrf()
        record_impressions(request.form.getlist("impression"), current_account())
        return "", 204

    @app.route("/", methods=["GET", "POST"])
    def index():
        form = request.form
        quick_goal = form.get("quick_goal")
        goal = quick_goal if quick_goal in QUICK_GOALS else form.get("goal", "")
        skip_profile = form.get("action") == "skip_profile"
        account = current_account()
        stored_profile = saved_profile(account)
        values = profile_values({} if skip_profile else {**stored_profile, **form})
        profile = Profile(goal=goal.strip()[:GOAL_MAX_LENGTH] or None, **values)
        allow_sample_history = is_demo_catalog and (account is None or account["username"] == DEMO_USERNAME)
        use_sample_history = allow_sample_history and form.get("history") == "sample" and not skip_profile
        history = sample_history if use_sample_history else ()
        if not skip_profile and not allow_sample_history:
            # В движке события в конце последовательности имеют больший вес.
            history = recent_interactions(account)[::-1]
        result = recommender(catalog, profile, history, len(catalog))
        result_order = form.get("result_order", form.get("current_order", "recommended"))
        if result_order not in ORDER_LABELS:
            result_order = "recommended"
        format_filter = form.get("selected_format") or form.get("format_filter", request.args.get("format", "all"))
        if format_filter not in FORMAT_LABELS:
            format_filter = "all"

        all_candidate_cards = [(materials_by_id[item.material_id], item) for item in result.items]
        format_counts = {
            fmt: sum(1 for card, _ in all_candidate_cards if _format_key(card) == fmt)
            for fmt in FORMAT_LABELS if fmt != "all"
        }
        format_counts["all"] = len(all_candidate_cards)

        cards = all_candidate_cards
        if format_filter != "all":
            cards = [c for c in cards if _format_key(c[0]) == format_filter]

        if result_order != "recommended":
            cards = [card for card in cards if getattr(card[0], result_order) is not None]
            cards.sort(key=lambda card: getattr(card[0], result_order), reverse=True)
        page = max(1, form.get("show_page", default=1, type=int))
        page_size = form.get("selected_page_size", type=int) or form.get("page_size", type=int)
        if page_size not in PAGE_SIZES:
            page_size = RESULTS_PER_PAGE
        visible_cards = cards[:page * page_size]
        impression_batch, impression_batch_id, impression_tickets = issue_impression_tickets(
            [material.id for material, _ in visible_cards], algorithm, result_order, f"{format_filter}:{page_size}",
            form.get("impression_batch") if page > 1 else None,
        )
        return render_template(
            "recommendations_results.html" if request.headers.get("X-Recommendation-Fragment") == "1" else "recommendations.html",
            profile=profile,
            profile_values=values,
            profile_fields=PROFILE_FIELDS,
            show_profile_details=use_sample_history or (not skip_profile and values != stored_profile),
            quick_goals=QUICK_GOALS,
            goal_max_length=GOAL_MAX_LENGTH,
            allow_sample_history=allow_sample_history,
            use_sample_history=use_sample_history,
            is_demo_catalog=is_demo_catalog,
            skip_profile=skip_profile,
            result_order=result_order,
            result_orders=ORDER_LABELS,
            format_filter=format_filter,
            format_filters=FORMAT_LABELS,
            format_counts=format_counts,
            page_size=page_size,
            page_sizes=PAGE_SIZES,
            result=result,
            cards=visible_cards,
            impression_batch=impression_batch,
            impression_batch_id=impression_batch_id,
            impression_tickets=impression_tickets,
            total_cards=len(cards),
            next_page=page + 1 if len(visible_cards) < len(cards) else None,
        )

    return app
