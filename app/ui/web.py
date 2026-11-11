"""Веб-демо подбора материалов."""

import json
import math
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, Response, abort, redirect, render_template, request, url_for

from app.catalog import DEMO_CATALOG_PATH, catalog_source, load_catalog, load_history
from app.catalog import material_format as _format_key
from app.catalog.lessons import read_programs
from app.catalog.publications import load_publications
from app.catalog.subscriptions import ACCESS_LABELS, access_category, available_access
from app.contracts import Material, Profile, RecommendationResult, Recommender
from app.ctr_report import ORDER_LABELS
from app.live_data import LiveDataProvider
from app.database import account_database_path
from app.ui.accounts import (
    DEMO_PASSWORD_CODE, DEMO_USERNAME, PROFILE_FIELDS, _db as get_accounts_db, account_avatar, check_csrf,
    configure_accounts, csrf_token, current_account, password_code_pending, profile_values, recent_interactions,
    recommendation_mode, record_interaction, saved_profile, update_profile,
)
from app.ui.admin import admin
from app.ui.chat import chat_bp
from app.ui.avatars import AVATAR_MAX_BYTES, AVATAR_MAX_MB, prepare_avatar
from app.ui.descriptions import clean_plain_text, format_description_html
from app.ui.experiments import experiment_variant, track_experiment_activity
from app.ui.impressions import issue_impression_tickets, record_impressions
from app.recommender import recommend
from app.recommender.baseline import recommend_popular_courses
from app.recommender.daily import daily_picks
from app.recommender.hybrid import cached_bm25, configured_bm25_weight
from app.recommender.runtime import rerank_next_page
from evaluation.catalog_quality import REPORT_NAME, catalog_fingerprint
from evaluation.learning_report import REPORT_NAME as LEARNING_REPORT_NAME
from evaluation.stress_report import REPORT_NAME as STRESS_REPORT_NAME

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
QUICK_GOALS = ("Найти первых клиентов", "Увеличить продажи", "Разобраться в учёте")
GOAL_MAX_LENGTH = 200
RESULTS_PER_PAGE = 6
PAGE_SIZES = (6, 12, 24)
COVER_ORIGIN = "https://s19060.cdn.ngenix.net"
COVER_METADATA = Path(__file__).with_name("public-covers.json")
CHAT_PAGES = frozenset({"index", "material_page", "profile_page", "profile_settings", "accounts.login", "accounts.register"})
_MAX_RECOMMEND_CACHE = 128

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


def _normalize_phone(value: str) -> str:
    if not value:
        return ""
    digits = re.sub(r"[^0-9]", "", value)
    if not re.fullmatch(r"\+?[0-9() .-]+", value):
        raise ValueError("Проверьте формат телефона.")
    if value.startswith("+") and not value.startswith("+7"):
        if 7 <= len(digits) <= 15:
            return "+" + digits
    elif not value.startswith("+") and len(digits) == 10:
        return "+7" + digits
    elif len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    raise ValueError("Введите 10 цифр российского номера после +7 или укажите номер с международным кодом.")


def _display_phone(value: str) -> str:
    if re.fullmatch(r"\+7[0-9]{10}", value):
        return f"+7 ({value[2:5]}) {value[5:8]}-{value[8:10]}-{value[10:12]}"
    return value


def format_lessons(count: int | None) -> str:
    if not count or count <= 0:
        return ""
    rem10 = count % 10
    rem100 = count % 100
    if 11 <= rem100 <= 19:
        word = "уроков"
    elif rem10 == 1:
        word = "урок"
    elif 2 <= rem10 <= 4:
        word = "урока"
    else:
        word = "уроков"
    return f"{count} {word}"


def _pagination_items(current_page: int, total_pages: int) -> list[int | None]:
    if total_pages <= 1:
        return []
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    pages = sorted({1, total_pages, *range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1)})
    items: list[int | None] = []
    for p in pages:
        if items and items[-1] is not None and p - items[-1] > 1:
            items.append(None)
        items.append(p)
    return items


def _find_next_materials(
    current_material: Material,
    all_materials: tuple[Material, ...] | list[Material],
    allowed_access: set[str] | tuple[str, ...] | None = None,
    seen_ids: set[str] | None = None,
    limit: int = 3,
) -> tuple[list[dict], bool]:
    """Находит наиболее подходящие материалы для продолжения траектории обучения."""
    curr_topics = set(current_material.topics or ())
    curr_stages = set(current_material.stages or ())
    curr_industries = set(current_material.industries or ())
    curr_format = (current_material.format or "").lower()
    seen = set(seen_ids or ())

    def _collect(exclude_seen: bool) -> tuple[list[tuple[float, str, Material, bool]], list[tuple[float, str, Material, bool]]]:
        candidates = []
        paid_followups = []
        for m in all_materials:
            if m.id == current_material.id:
                continue
            is_seen = m.id in seen
            if exclude_seen and is_seen:
                continue
            category = access_category(m)
            needs_access = allowed_access is not None and category not in allowed_access
            paid_followup = (
                needs_access and category in {"paid", "subscription"} and access_category(current_material) == "free"
                and (m.entity_type == "course" or _format_key(m) == "курс")
                and bool(curr_topics.intersection(m.topics))
                and (not curr_stages or not m.stages or bool(curr_stages.intersection(m.stages)))
                and (not curr_industries or not m.industries or bool(curr_industries.intersection(m.industries)))
            )
            if needs_access and not paid_followup:
                continue

            score = 0.0
            m_topics = set(m.topics or ())
            common_topics = curr_topics & m_topics
            if common_topics:
                score += len(common_topics) * 20.0

            common_industries = curr_industries & set(m.industries or ())
            if common_industries:
                score += len(common_industries) * 6.0

            common_stages = curr_stages & set(m.stages or ())
            if common_stages:
                score += len(common_stages) * 4.0

            m_format = (m.format or "").lower()
            if curr_format in ("статья", "видео", "лонгрид", "подкаст"):
                if "курс" in m_format:
                    score += 10.0
                elif m_format in ("инструмент", "шаблон", "чек-лист", "таблица", "калькулятор"):
                    score += 8.0
            elif "курс" in curr_format:
                if m_format in ("инструмент", "шаблон", "чек-лист", "таблица", "калькулятор"):
                    score += 10.0
                elif "курс" in m_format:
                    score += 4.0
            elif m_format != curr_format:
                score += 3.0

            if m.rating:
                score += float(m.rating) * 1.0
            if m.popularity:
                score += math.log1p(float(m.popularity)) * 1.5

            if m_format in ("инструмент", "шаблон", "чек-лист", "таблица", "калькулятор"):
                step_label = "Практический инструмент"
            elif "курс" in m_format and curr_format not in ("курс",):
                step_label = "Углубленный курс"
            elif m_format in ("видео",):
                step_label = "Видеоразбор"
            elif common_topics:
                step_label = "Следующий шаг"
            else:
                step_label = "Рекомендуем изучить"

            if paid_followup:
                topic = min(common_topics)
                paid_followups.append((score, f"Углубление по теме «{topic}»", m, is_seen))
            else:
                candidates.append((score, step_label, m, is_seen))
        return candidates, paid_followups

    candidates, paid_followups = _collect(exclude_seen=True)
    candidates.sort(key=lambda item: item[0], reverse=True)
    next_items = candidates[:limit]
    if limit > 1 and paid_followups and all(access_category(row[2]) == "free" for row in next_items):
        next_items = next_items[:limit - 1] + [max(paid_followups, key=lambda row: row[0])]

    all_seen = False
    if not next_items and seen:
        all_seen = True
        candidates, paid_followups = _collect(exclude_seen=False)
        candidates.sort(key=lambda item: item[0], reverse=True)
        next_items = candidates[:limit]
        if limit > 1 and paid_followups and all(access_category(row[2]) == "free" for row in next_items):
            next_items = next_items[:limit - 1] + [max(paid_followups, key=lambda row: row[0])]

    return (
        [
            {"material": m, "step_label": label, "viewed": is_seen}
            for _, label, m, is_seen in next_items
        ],
        all_seen,
    )


def create_app(
    recommender: Recommender = recommend, *, database_path: Path | None = None, algorithm_name: str | None = None,
    password_code_sender: Callable[[str, str], None] | None = None,
    course_plus_activator: Callable[[int], bool] | None = None,
    live_data_provider: LiveDataProvider | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024
    app.config["PASSWORD_CODE_SENDER"] = password_code_sender
    app.config["COURSE_PLUS_ACTIVATOR"] = course_plus_activator
    app.config["LIVE_DATA_PROVIDER"] = live_data_provider
    bm25_weight = configured_bm25_weight()
    app.config["BM25_WEIGHT"] = bm25_weight
    source = catalog_source()
    is_demo_catalog = source == DEMO_CATALOG_PATH
    catalog = load_catalog(source)
    if not is_demo_catalog and not catalog:
        raise ValueError(f"Каталог пуст: {source}")
    materials_by_id = {material.id: material for material in catalog}
    publication_dates = load_publications(source.parent)
    app.config["CHAT_CATALOG"] = catalog
    programs = read_programs(source, {material.id for material in catalog if material.entity_type == "course"})
    covers = {} if is_demo_catalog else json.loads(COVER_METADATA.read_text(encoding="utf-8"))
    app.config["CATALOG_TITLES"] = {material.id: material.title for material in catalog}
    app.config["EVALUATION_SUMMARY_DIR"] = None if is_demo_catalog else source.parent
    app.config["CATALOG_QUALITY_PATH"] = None if is_demo_catalog else source.parent / REPORT_NAME
    app.config["STRESS_REPORT_PATH"] = None if is_demo_catalog else source.parent / STRESS_REPORT_NAME
    app.config["LEARNING_REPORT_PATH"] = None if is_demo_catalog else source.parent / LEARNING_REPORT_NAME
    app.config["CATALOG_QUALITY_FINGERPRINT"] = None if is_demo_catalog else catalog_fingerprint(catalog)
    app.config["CATALOG_FORMATS"] = {} if is_demo_catalog else {material.id: _format_key(material) for material in catalog}
    app.config["CATALOG_FORMAT_LABELS"] = FORMAT_LABELS
    if not is_demo_catalog and catalog:
        try:
            cached_bm25(catalog)
        except Exception:
            pass
    sample_history = load_history(FIXTURES / "history.demo.json") if is_demo_catalog else ()
    recommend_cache: dict[tuple, RecommendationResult] = {}
    configure_accounts(app, database_path or account_database_path())
    app.register_blueprint(admin)
    app.register_blueprint(chat_bp)
    app.after_request(track_experiment_activity)
    recommender_type = type(recommender)
    algorithm = algorithm_name or (
        f"{getattr(recommender, '__module__', recommender_type.__module__)}."
        f"{getattr(recommender, '__qualname__', recommender_type.__qualname__)}"
    )
    if algorithm_name is None and recommender is recommend:
        algorithm += ":access-pacing-v1"
    app.jinja_env.globals["signed_in"] = lambda: current_account() is not None
    app.jinja_env.globals["current_account"] = current_account
    app.jinja_env.globals["chat_pages"] = CHAT_PAGES
    app.jinja_env.globals["material_format"] = _format_key
    app.jinja_env.globals["material_access_category"] = access_category
    app.jinja_env.filters["clean_text"] = clean_plain_text
    app.jinja_env.filters["format_lessons"] = format_lessons

    @app.get("/health")
    def health():
        return {"status": "ok", "catalog": "demo" if is_demo_catalog else "real", "materials": len(catalog)}

    @app.after_request
    def protect_pages(response):
        if request.endpoint not in {"static", "health"}:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            cover_source = f" {COVER_ORIGIN}" if request.endpoint == "index" and covers else ""
            response.headers["Content-Security-Policy"] = (
                f"default-src 'none'; img-src 'self'{cover_source}; style-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
            if request.endpoint in CHAT_PAGES:
                response.headers["Content-Security-Policy"] += "; script-src 'self'; connect-src 'self'"
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
            formatted_phone=_display_phone(account["phone"] or ""),
            identity_values=account if details is None else details,
            avatar_max_mb=AVATAR_MAX_MB,
            error=error,
            saved=request.args.get("saved") == "1" and not settings,
            password_code_pending=settings and not is_demo and password_code_pending(account),
            demo_password_code=DEMO_PASSWORD_CODE if app.config["PASSWORD_CODE_SENDER"] is None else None,
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
            if details["phone"] != (account["phone"] or ""):
                details["phone"] = _normalize_phone(details["phone"])
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
        account = current_account()
        impression = request.args.get("impression")
        if request.method == "GET" and not any(
            "prefetch" in request.headers.get(header, "").lower() for header in ("Sec-Purpose", "Purpose")
        ):
            record_interaction(account, material_id, "view")
            if impression:
                record_impressions((impression,), account, previewed_material=material_id)
        open_url = url_for("open_material", material_id=material_id, impression=impression)
        description_html = format_description_html(material.description)
        lessons = programs.get(material_id, ())
        allowed_access = available_access(account and account["has_course_plus"]) if not is_demo_catalog else None
        category = access_category(material)
        access_required = allowed_access is not None and category in {"paid", "subscription"} and category not in allowed_access
        seen_ids = {event.material_id for event in recent_interactions(account)}
        for seen_param in request.args.getlist("seen"):
            seen_ids.update(part.strip() for part in seen_param.split(",") if part.strip())
        seen_ids.discard(material_id)
        next_materials, next_all_seen = _find_next_materials(
            material, catalog, allowed_access=allowed_access, seen_ids=seen_ids, limit=3,
        )
        if request.accept_mimetypes.best == "application/json":
            return {
                "id": material.id,
                "title": material.title,
                "format": material.format,
                "description": material.description,
                "description_html": description_html,
                "subscriptions": material.subscriptions,
                "access_required": access_required,
                "lessons": lessons,
                "authors": material.authors,
                "duration": material.duration,
                "lesson_count": material.lesson_count,
                "url": _material_destination(material.url),
                "open_url": open_url,
                "csrf_token": csrf_token(),
                "next_all_seen": next_all_seen,
                "next_materials": [
                    {
                        "id": item["material"].id,
                        "url": url_for("material_page", material_id=item["material"].id),
                        "title": item["material"].title,
                        "format": item["material"].format,
                        "duration": item["material"].duration,
                        "description": clean_plain_text(item["material"].description or "")[:110],
                        "access": item["material"].access,
                        "cost": item["material"].cost,
                        "subscriptions": item["material"].subscriptions,
                        "step_label": item["step_label"],
                        "viewed": item["viewed"],
                    }
                    for item in next_materials
                ],
            }
        return render_template(
            "material.html",
            material=material,
            description_html=description_html,
            open_url=open_url,
            lessons=lessons,
            next_materials=next_materials,
            next_all_seen=next_all_seen,
            access_required=access_required,
        )

    @app.post("/materials/<material_id>/open")
    def open_material(material_id):
        material = materials_by_id.get(material_id)
        if material is None:
            abort(404)
        account = current_account()
        target = _material_destination(material.url)
        check_csrf()
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
        now = datetime.now(UTC)
        stored_profile = saved_profile(account)
        values = profile_values({} if skip_profile else {**stored_profile, **form})
        profile = Profile(goal=goal.strip()[:GOAL_MAX_LENGTH] or None, **values)
        allow_sample_history = is_demo_catalog and (account is None or account["username"] == DEMO_USERNAME)
        use_sample_history = allow_sample_history and form.get("history") == "sample" and not skip_profile
        history = sample_history if use_sample_history else ()
        if not skip_profile and not allow_sample_history:
            # В движке события в конце последовательности имеют больший вес.
            history = recent_interactions(account)[::-1]
        has_course_plus = bool(account and account["has_course_plus"])
        available = available_access(has_course_plus)
        all_access_filters = tuple(ACCESS_LABELS)
        access_filters = all_access_filters
        candidates = catalog
        if not is_demo_catalog:
            if form.get("access_filters_set") == "1":
                requested = set(form.getlist("access_filter"))
                access_filters = tuple(category for category in all_access_filters if category in requested)
            if access_filters != all_access_filters:
                candidates = tuple(m for m in catalog if access_category(m) in access_filters)
        result_order = form.get("result_order", form.get("current_order", "recommended"))
        if result_order not in ORDER_LABELS:
            result_order = "recommended"
        format_filter = form.get("selected_format") or form.get("format_filter", request.args.get("format", "all"))
        if format_filter not in FORMAT_LABELS:
            format_filter = "all"
        variant = experiment_variant(account) if recommender is recommend else None
        mode = variant.mode if variant else recommendation_mode() if recommender is recommend else "recommend"
        effective_algorithm = algorithm
        balance_access = (access_filters == all_access_filters and format_filter == "all" and result_order == "recommended")
        active_weight = (variant.bm25_weight if variant else bm25_weight) if mode == "hybrid" else 0.0

        if mode == "popular":
            effective_algorithm = "app.recommender.baseline.recommend_popular_courses"
            result = recommend_popular_courses(candidates, len(candidates))
        elif recommender is recommend and candidates:
            if mode == "hybrid":
                effective_algorithm += f":bm25-{active_weight:.12g}"
            cache_key = (
                profile.goal or "",
                tuple(sorted(values.items())),
                len(candidates),
                access_filters,
                balance_access,
                mode,
                active_weight,
                tuple((h.material_id, h.kind) for h in history),
            )
            if cache_key in recommend_cache:
                result = recommend_cache[cache_key]
            else:
                result = recommend(
                    candidates, profile, history, len(candidates),
                    links_directory=source.parent,
                    history_as_of=None if is_demo_catalog else now,
                    balance_access=balance_access,
                    bm25_weight=active_weight,
                )
                if len(recommend_cache) >= _MAX_RECOMMEND_CACHE:
                    recommend_cache.clear()
                recommend_cache[cache_key] = result
        else:
            result = recommender(candidates, profile, history, len(candidates)) if candidates else RecommendationResult((), "")

        if algorithm_name is None and recommender is recommend and is_demo_catalog:
            effective_algorithm += ":demo-catalog"

        candidate_ids = {material.id for material in candidates}
        all_candidate_cards = [(materials_by_id[item.material_id], item) for item in result.items if item.material_id in candidate_ids]
        counts = Counter(_format_key(card) for card, _ in all_candidate_cards)
        format_counts = {fmt: counts.get(fmt, 0) for fmt in FORMAT_LABELS if fmt != "all"}
        format_counts["all"] = len(all_candidate_cards)

        cards = all_candidate_cards
        if format_filter != "all":
            cards = [c for c in cards if _format_key(c[0]) == format_filter]

        if result_order != "recommended":
            cards = [card for card in cards if getattr(card[0], result_order) is not None]
            cards.sort(key=lambda card: getattr(card[0], result_order), reverse=True)
        page_size = form.get("selected_page_size", type=int) or form.get("page_size", type=int)
        if page_size not in PAGE_SIZES:
            page_size = RESULTS_PER_PAGE
        total_pages = max(1, math.ceil(len(cards) / page_size)) if cards else 1
        page = min(max(1, form.get("show_page", default=1, type=int)), total_pages)
        shown_ids = [
            part.strip()
            for raw in form.getlist("shown_ids")
            for part in raw.split(",")
            if part.strip()
        ]
        if page > 1 and result_order == "recommended" and recommender is recommend:
            session_seen_ids = {
                part.strip()
                for raw in form.getlist("seen_ids")
                for part in raw.split(",")
                if part.strip()
            }
            if shown_ids:
                cards = rerank_next_page(
                    cards,
                    shown_ids=shown_ids[:(page - 1) * page_size],
                    session_seen_ids=session_seen_ids,
                    history=history,
                    profile=profile,
                    links_directory=source.parent,
                    db_connection=get_accounts_db(),
                )
        page_start = (page - 1) * page_size
        visible_cards = cards[page_start:page * page_size]
        cumulative_shown_ids = [material.id for material, _ in cards[:page * page_size]]
        first_cards = cards[:RESULTS_PER_PAGE]
        access_counts = Counter(access_category(material) for material, _ in first_cards)
        impression_batch, impression_batch_id, impression_tickets = issue_impression_tickets(
            [material.id for material, _ in visible_cards],
            effective_algorithm, result_order,
            f"{format_filter}:{page_size}:{','.join(access_filters)}",
            form.get("impression_batch") if page > 1 else None,
            variant=variant,
            start_position=page_start + 1,
        )
        is_fragment = request.headers.get("X-Recommendation-Fragment") == "1"
        daily = daily_picks(materials_by_id, profile, history, publication_dates, available,
                            now=now, links_directory=source.parent) if request.method == "GET" and format_filter == "all" and not is_fragment else {}
        return render_template(
            "recommendations_results.html" if is_fragment else "recommendations.html",
            profile=profile,
            profile_values=values,
            profile_fields=PROFILE_FIELDS,
            show_profile_details=use_sample_history or (not skip_profile and values != stored_profile),
            quick_goals=QUICK_GOALS,
            daily=daily,
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
            access_labels=ACCESS_LABELS,
            access_counts=access_counts,
            access_sample_size=len(first_cards),
            access_filters=access_filters,
            available_access=available,
            page_size=page_size,
            page_sizes=PAGE_SIZES,
            result=result,
            cards=visible_cards,
            cumulative_shown_ids=cumulative_shown_ids,
            covers=covers,
            impression_batch=impression_batch,
            impression_batch_id=impression_batch_id,
            impression_tickets=impression_tickets,
            total_cards=len(cards),
            current_page=page,
            total_pages=total_pages,
            pagination_items=_pagination_items(page, total_pages),
            page_start=page_start + 1 if visible_cards else 0,
            page_end=page_start + len(visible_cards),
            prev_page=page - 1 if page > 1 else None,
            next_page=page + 1 if page < total_pages else None,
        )

    return app
