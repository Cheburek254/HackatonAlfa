"""Загрузка каталога в общий формат."""

import json
from datetime import datetime
from pathlib import Path

from app.contracts import InteractionEvent, Material


def load_catalog(path: Path) -> tuple[Material, ...]:
    """Загрузить нормализованный JSON-каталог, включая стартовую фикстуру."""
    records = json.loads(path.read_text(encoding="utf-8"))
    materials = tuple(
        Material(
            id=record["id"],
            title=record["title"],
            url=record["url"],
            description=record.get("description", ""),
            topics=tuple(record.get("topics", [])),
            format=record.get("format"),
            access=record.get("access", "unknown"),
            stages=tuple(record.get("stages", [])),
            industries=tuple(record.get("industries", [])),
            experience_levels=tuple(record.get("experience_levels", [])),
        )
        for record in records
    )
    if len({material.id for material in materials}) != len(materials):
        raise ValueError(f"Каталог содержит повторяющиеся ID: {path}")
    return materials


def load_history(path: Path) -> tuple[InteractionEvent, ...]:
    """Загрузить нормализованный JSON-журнал одного пользователя."""
    records = json.loads(path.read_text(encoding="utf-8"))
    events = []
    for record in records:
        occurred_at = record.get("occurred_at")
        events.append(
            InteractionEvent(
                material_id=record["material_id"],
                kind=record.get("kind", "view"),
                occurred_at=datetime.fromisoformat(occurred_at) if occurred_at else None,
            )
        )
    return tuple(events)
