"""Загрузка каталога и журнала действий в общий формат контракта."""

import json
from datetime import datetime
from pathlib import Path

from app.contracts import InteractionEvent, Material

VALID_EVENT_KINDS = {"view", "click", "complete"}
DEFAULT_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "catalog.norm.json"
DEMO_CATALOG_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "catalog.demo.json"


def _optional_metric(value: object, *, maximum: float | None = None) -> float | None:
    if type(value) not in (int, float) or not 0 <= value < float("inf"):
        return None
    return value if maximum is None or value <= maximum else None


def load_catalog(path: Path | str | None = None) -> tuple[Material, ...]:
    """Загрузить нормализованный JSON-каталог, включая стартовую фикстуру."""
    if path is None:
        target = DEFAULT_CATALOG_PATH if DEFAULT_CATALOG_PATH.exists() else DEMO_CATALOG_PATH
    else:
        target = Path(path)

    records = json.loads(target.read_text(encoding="utf-8"))
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
        access = access_raw if access_raw in ("free", "paid", "unknown") else "unknown"
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
                popularity=_optional_metric(record.get("popularity")),
                rating=_optional_metric(record.get("rating"), maximum=5),
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
