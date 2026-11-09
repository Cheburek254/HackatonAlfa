"""Сводка по показам и переходам рекомендаций из SQLite."""

import sqlite3
from argparse import ArgumentParser
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.database import account_database_path

ORDER_LABELS = {"recommended": "По подбору", "popularity": "По популярности", "rating": "По оценке"}


@dataclass(frozen=True)
class Metrics:
    impressions: int
    clicks: int

    @property
    def ctr(self) -> float:
        return 100 * self.clicks / self.impressions if self.impressions else 0.0


def read_report(database: Path) -> tuple[dict[str, Metrics], list[tuple[str, str, int, Metrics]]]:
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            """WITH positions AS (
                   SELECT algorithm, result_order, position, COUNT(*) AS impressions, COUNT(clicked_at) AS clicks
                   FROM recommendation_impressions
                   WHERE audience IN ('guest', 'account')
                   GROUP BY algorithm, result_order, position
               )
               SELECT algorithm, NULL AS result_order, NULL AS position, SUM(impressions), SUM(clicks)
               FROM positions GROUP BY algorithm
               UNION ALL
               SELECT algorithm, result_order, position, impressions, clicks FROM positions
               ORDER BY algorithm, position, result_order"""
        ).fetchall()
    totals = {algorithm: Metrics(shown, clicked) for algorithm, _, pos, shown, clicked in rows if pos is None}
    detail = [(algorithm, order, pos, Metrics(shown, clicked)) for algorithm, order, pos, shown, clicked in rows
              if pos is not None]
    return totals, detail


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
