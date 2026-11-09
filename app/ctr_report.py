"""Сводка по показам и переходам рекомендаций из SQLite."""

import sqlite3
from argparse import ArgumentParser
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from app.database import account_database_path

ORDER_LABELS = {"recommended": "По подбору", "popularity": "По популярности", "rating": "По оценке"}
LEGACY_ALGORITHMS = ("app.ui.stub.recommend", "app.ai.stub.recomend")
REPORT_AUDIENCE = "audience IN ('guest', 'account')"
CURRENT_ALGORITHMS = "algorithm NOT IN (?, ?)"


@dataclass(frozen=True)
class Metrics:
    impressions: int
    clicks: int

    @property
    def ctr(self) -> float:
        return 100 * self.clicks / self.impressions if self.impressions else 0.0


def read_report(database: Path, *, include_legacy: bool = False) -> tuple[dict[str, Metrics], list[tuple[str, str, int, Metrics]]]:
    filters = REPORT_AUDIENCE if include_legacy else f"{REPORT_AUDIENCE} AND {CURRENT_ALGORITHMS}"
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            f"""WITH positions AS (
                   SELECT algorithm, result_order, position, COUNT(*) AS impressions, COUNT(clicked_at) AS clicks
                   FROM recommendation_impressions
                   WHERE {filters}
                   GROUP BY algorithm, result_order, position
               )
               SELECT algorithm, NULL AS result_order, NULL AS position, SUM(impressions), SUM(clicks)
               FROM positions GROUP BY algorithm
               UNION ALL
               SELECT algorithm, result_order, position, impressions, clicks FROM positions
               ORDER BY algorithm, position, result_order""",
            () if include_legacy else LEGACY_ALGORITHMS,
        ).fetchall()
    totals = {algorithm: Metrics(shown, clicked) for algorithm, _, pos, shown, clicked in rows if pos is None}
    detail = [(algorithm, order, pos, Metrics(shown, clicked)) for algorithm, order, pos, shown, clicked in rows
              if pos is not None]
    return totals, detail


def read_top_cards(database: Path, limit: int = 20) -> list[tuple[str, Metrics]]:
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            f"""SELECT material_id, COUNT(*) AS impressions, COUNT(clicked_at) AS clicks
               FROM recommendation_impressions
               WHERE {REPORT_AUDIENCE} AND {CURRENT_ALGORITHMS}
               GROUP BY material_id
               ORDER BY 1.0 * clicks / impressions DESC, clicks DESC, impressions DESC, material_id
               LIMIT ?""",
            (*LEGACY_ALGORITHMS, limit),
        ).fetchall()
    return [(material_id, Metrics(shown, clicked)) for material_id, shown, clicked in rows]


def read_daily_activity(database: Path, today: date | None = None) -> list[tuple[str, Metrics]]:
    today = today or datetime.now(UTC).date()
    first_day = today - timedelta(days=29)
    start = int(datetime.combine(first_day, time(), UTC).timestamp())
    end = int(datetime.combine(today + timedelta(days=1), time(), UTC).timestamp())
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            f"""WITH daily AS (
                   SELECT date(shown_at, 'unixepoch') AS day, COUNT(*) AS impressions, 0 AS clicks
                   FROM recommendation_impressions
                   WHERE {REPORT_AUDIENCE} AND {CURRENT_ALGORITHMS} AND shown_at >= ? AND shown_at < ?
                   GROUP BY day
                   UNION ALL
                   SELECT date(clicked_at, 'unixepoch'), 0, COUNT(*)
                   FROM recommendation_impressions
                   WHERE {REPORT_AUDIENCE} AND {CURRENT_ALGORITHMS} AND clicked_at >= ? AND clicked_at < ?
                   GROUP BY date(clicked_at, 'unixepoch')
               )
               SELECT day, SUM(impressions), SUM(clicks) FROM daily GROUP BY day""",
            (*LEGACY_ALGORITHMS, start, end, *LEGACY_ALGORITHMS, start, end),
        ).fetchall()
    metrics = {day: Metrics(shown, clicked) for day, shown, clicked in rows}
    days = [(first_day + timedelta(days=offset)).isoformat() for offset in range(30)]
    return [(day, metrics.get(day, Metrics(0, 0))) for day in days]


def main() -> None:
    parser = ArgumentParser(description="Показы, переходы и CTR рекомендаций без тестового аккаунта.")
    parser.add_argument("--database", type=Path, default=account_database_path(), help="Путь к SQLite")
    database = parser.parse_args().database
    try:
        totals, detail = read_report(database)
    except sqlite3.Error as error:
        parser.exit(1, f"Не удалось прочитать отчёт из {database}: {error}\n")

    if not totals:
        print("Показов гостей и личных аккаунтов пока нет.")
        return
    print("| Алгоритм | Показы | Переходы | CTR |")
    print("| --- | ---: | ---: | ---: |")
    for algorithm, metrics in totals.items():
        print(f"| {algorithm} | {metrics.impressions} | {metrics.clicks} | {metrics.ctr:.2f}% |")

    print("\n| Алгоритм | Позиция | Сортировка | Показы | Переходы | CTR |")
    print("| --- | ---: | --- | ---: | ---: | ---: |")
    for algorithm, order, position, metrics in detail:
        label = ORDER_LABELS.get(order, order)
        print(f"| {algorithm} | {position} | {label} | {metrics.impressions} | {metrics.clicks} | {metrics.ctr:.2f}% |")


if __name__ == "__main__":
    main()
