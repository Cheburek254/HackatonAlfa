"""Связи материалов с пакетами доступа."""

import json
from collections import defaultdict
from pathlib import Path

COURSE_PLUS = "Курс+"
ACCESS_LABELS = {"free": "Бесплатные", "paid": "Платные", "subscription": "По подписке Курс+"}


def access_category(material):
    if COURSE_PLUS in material.subscriptions:
        return "subscription"
    return material.access


def available_access(course_plus):
    return ("free", "subscription") if course_plus else ("free",)


def entitled_catalog(catalog, course_plus):
    allowed = available_access(course_plus)
    return tuple(m for m in catalog if access_category(m) in allowed)


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
