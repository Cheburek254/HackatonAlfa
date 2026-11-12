"""Связи материалов с пакетами доступа."""

import json
from collections import defaultdict
from pathlib import Path

COURSE_PLUS = "Курс+"
ACCESS_LABELS = {"free": "Бесплатные", "paid": "Платные", "subscription": "По подписке"}


def access_category(material):
    if material.subscriptions:
        return "subscription"
    return material.access


def available_access(course_plus):
    return ("free", "subscription") if course_plus else ("free",)


def can_open_material(material, *, signed_in, course_plus=False, course_access=None):
    if material.entity_type == "course" and course_access is not None:
        return signed_in and course_access in (1, 2)
    if material.requires_login and not signed_in:
        return False
    if access_category(material) == "subscription":
        return signed_in and course_plus and COURSE_PLUS in material.subscriptions
    return material.access == "free"


def entitled_catalog(catalog, course_plus, *, signed_in=None):
    if signed_in is None:
        signed_in = course_plus
    return tuple(m for m in catalog if can_open_material(m, signed_in=signed_in, course_plus=course_plus))


def access_label(material):
    category = access_category(material)
    if category == "subscription":
        return f"По подписке {', '.join(material.subscriptions)}" if material.subscriptions else "По подписке"
    if category == "paid":
        return "Нужна покупка"
    if category == "free":
        return "Нужен вход на Альфа-Курсе" if material.requires_login else "Бесплатно"
    return "Условия уточняются"


def subscription_memberships(reader) -> dict[str, list[str]]:
    subscriptions = {
        row["Id"]: row["Title"].strip() for row in reader.load_rows("Subscription")
        if row.get("Title") and row.get("IsDeleted") != "t"
    }
    memberships = defaultdict(set)
    for table, key in (("CourseSubscriptions", "CourseId"), ("PodcastSubscriptions", "PodcastId")):
        for row in reader.load_rows(table):
            if (name := subscriptions.get(row.get("SubscriptionId"))) and (mid := row.get(key)):
                memberships[mid].add(name)
    return {mid: sorted(names) for mid, names in sorted(memberships.items())}


def load_memberships(directory: Path) -> dict[str, tuple[str, ...]]:
    export = directory / "subscriptions.json"
    source = export if export.is_file() else Path(__file__).with_name("course-plus.json")
    records = json.loads(source.read_text(encoding="utf-8"))
    return {mid: tuple(names) for mid, names in records.items()}
