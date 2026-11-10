"""Даты публикаций для ежедневных подборок."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

FILENAME = "publication-dates.json"
MSK = timezone(timedelta(hours=3))


def write_publications(reader, ids: set[str], output: Path) -> None:
    dates = {}
    for table in ("Articles", "Podcasts"):
        for row in reader.load_rows(table):
            if row.get("Id") not in ids or row.get("IsDeleted") == "t" or not row.get("Published"):
                continue
            try:
                timestamp = datetime.fromisoformat(row["Published"])
            except ValueError:
                continue
            if timestamp.tzinfo is not None and timestamp.year > 1970:
                dates[row["Id"]] = timestamp.astimezone(MSK).date().isoformat()
    output.write_text(json.dumps(dates, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def load_publications(directory: Path) -> dict[str, date]:
    try:
        records = json.loads((directory / FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    dates = {}
    if isinstance(records, dict):
        for mid, value in records.items():
            try:
                dates[mid] = date.fromisoformat(value)
            except (TypeError, ValueError):
                continue
    return dates


def main() -> None:
    from argparse import ArgumentParser

    from app.catalog import load_catalog
    from app.catalog.normalizer import PgDumpReader

    parser = ArgumentParser(description=__doc__)
    for name in ("backup", "catalog", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    write_publications(PgDumpReader(args.backup), {m.id for m in load_catalog(args.catalog)}, args.output)


if __name__ == "__main__":
    main()
