"""Загрузка каталога и журнала действий в общий формат контракта."""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.contracts import InteractionEvent, Material
from app.catalog.subscriptions import load_memberships

VALID_EVENT_KINDS = {"view", "click", "complete"}
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEMO_CATALOG_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "catalog.demo.json"
CATALOG_FILES = ("catalog_slim.sqlite", "catalog_slim.json", "catalog.norm.json")


def material_format(material: Material) -> str | None:
    if material.entity_type == "calculator":
        return "калькулятор"
    try:
        if urlsplit(material.url).path.startswith("/calculators/"):
            return "калькулятор"
    except ValueError:
        pass
    return material.format


def catalog_data_dir() -> Path:
    return Path(os.environ.get("ALFA_CATALOG_DIR") or DATA_DIR)


def catalog_source() -> Path:
    directory = catalog_data_dir()
    for filename in CATALOG_FILES:
        target = directory / filename
        if target.is_file():
            return target
    if os.environ.get("ALFA_CATALOG_DIR"):
        raise FileNotFoundError(f"В каталоге {directory} нет ни одного файла: {', '.join(CATALOG_FILES)}")
    return DEMO_CATALOG_PATH


def _read_records(path: Path) -> list[dict]:
    if path.suffix != ".sqlite":
        return json.loads(path.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(materials)")}
        entity_type = "entity_type" if "entity_type" in columns else "NULL AS entity_type"
        authors = "authors_json" if "authors_json" in columns else "'[]' AS authors_json"
        duration = "duration" if "duration" in columns else "NULL AS duration"
        lesson_count = "lesson_count" if "lesson_count" in columns else "NULL AS lesson_count"
        requires_login = "requires_login" if "requires_login" in columns else "0 AS requires_login"
        rows = connection.execute(
            f"""SELECT id, title, url, description, format, access, cost, popularity_score, avg_rating, {entity_type}, {authors}, {duration}, {lesson_count},
                      topics_json, stages_json, industries_json, experience_levels_json, {requires_login}
               FROM materials ORDER BY popularity_score DESC, rowid"""
        ).fetchall()
    records = [dict(row) for row in rows]
    for record in records:
        for field in ("topics", "stages", "industries", "experience_levels", "authors"):
            record[field] = json.loads(record.pop(f"{field}_json") or "[]")
    return records


def _optional_metric(value: object, *, maximum: float | None = None) -> float | None:
    if type(value) not in (int, float) or not 0 <= value < float("inf"):
        return None
    return value if maximum is None or value <= maximum else None


def load_catalog(path: Path | str | None = None) -> tuple[Material, ...]:
    """Загрузить экспорт каталога JSON/SQLite или стартовую фикстуру."""
    target = Path(path) if path is not None else catalog_source()
    records = _read_records(target)
    memberships = load_memberships(target.parent)
    materials = []
    seen_ids: set[str] = set()
    for record in records:
        mid = str(record.get("id", "")).strip()
        if not mid:
            continue
        if mid in seen_ids:
            raise ValueError(f"Каталог содержит повторяющиеся ID: {target}")
        seen_ids.add(mid)
        access_raw = record.get("access", "unknown")
        access = access_raw if access_raw in ("free", "paid", "subscription", "unknown") else "unknown"
        authors = dict.fromkeys(name.strip() for name in (record.get("authors") or ()) if isinstance(name, str) and name.strip())
        raw_duration = record.get("duration")
        duration = str(raw_duration).strip() if raw_duration and str(raw_duration).strip() else None
        raw_lessons = record.get("lesson_count")
        lesson_count = int(raw_lessons) if isinstance(raw_lessons, (int, float)) and raw_lessons > 0 else None
        materials.append(
            Material(
                id=mid,
                title=str(record.get("title", "")).strip(),
                url=str(record.get("url", "")).strip(),
                description=str(record.get("description", "") or "").strip(),
                topics=tuple(str(t).strip() for t in record.get("topics", []) if str(t).strip()),
                format=record.get("format"),
                access=access,
                stages=tuple(str(s).strip() for s in record.get("stages", []) if str(s).strip()),
                industries=tuple(str(i).strip() for i in record.get("industries", []) if str(i).strip()),
                experience_levels=tuple(
                    str(e).strip() for e in record.get("experience_levels", []) if str(e).strip()
                ),
                popularity=_optional_metric(record.get("popularity", record.get("popularity_score"))),
                rating=_optional_metric(record.get("rating", record.get("avg_rating")), maximum=5),
                cost=_optional_metric(record.get("cost")),
                entity_type=record.get("entity_type"),
                subscriptions=memberships.get(mid, ()),
                authors=tuple(authors),
                duration=duration,
                lesson_count=lesson_count,
                requires_login=bool(record.get("requires_login")),
            )
        )
    return tuple(materials)


def load_history(path: Path | str) -> tuple[InteractionEvent, ...]:
    """Загрузить нормализованный JSON-журнал одного пользователя."""
    target = Path(path)
    records = json.loads(target.read_text(encoding="utf-8"))
    events = []
    for record in records:
        mid = str(record.get("material_id", "")).strip()
        if not mid:
            continue
        kind = record.get("kind", "view")
        if kind not in VALID_EVENT_KINDS:
            kind = "view"
        occurred_at_raw = record.get("occurred_at")
        occurred_at = None
        if occurred_at_raw:
            try:
                occurred_at = datetime.fromisoformat(str(occurred_at_raw))
            except ValueError:
                occurred_at = None
        events.append(
            InteractionEvent(
                material_id=mid,
                kind=kind,
                occurred_at=occurred_at,
            )
        )
    return tuple(events)
