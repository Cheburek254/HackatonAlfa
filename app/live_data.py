"""Контракт агрегированного источника для будущих live-метрик."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.ctr_report import DailyActivity


@dataclass(frozen=True)
class LiveDataSnapshot:
    source: str
    as_of: datetime
    daily: tuple[tuple[date, DailyActivity], ...]


LiveDataProvider = Callable[[date, date], LiveDataSnapshot]


def live_payload(snapshot: LiveDataSnapshot, start: date, end: date) -> dict:
    if not snapshot.source or snapshot.as_of.utcoffset() is None:
        raise ValueError("Expected source and timezone-aware timestamp")
    seen = set()
    rows = []
    for day, metrics in sorted(snapshot.daily, key=lambda row: row[0]):
        if not start <= day <= end or day in seen:
            raise ValueError("Daily metrics are duplicated or outside the requested range")
        counts = (metrics.impressions, metrics.clicks, metrics.attributed_clicks)
        if any(type(count) is not int or count < 0 for count in counts) or metrics.attributed_clicks > metrics.impressions:
            raise ValueError("Daily metrics contain invalid counts")
        seen.add(day)
        rows.append({
            "date": day.isoformat(),
            "impressions": metrics.impressions,
            "clicks": metrics.clicks,
            "attributed_clicks": metrics.attributed_clicks,
            "ctr_pct": metrics.ctr,
        })
    return {
        "version": 1,
        "status": "ok",
        "source": snapshot.source,
        "as_of": snapshot.as_of.astimezone(UTC).isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC"},
        "daily": rows,
    }
