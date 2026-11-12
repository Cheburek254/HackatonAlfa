"""Справочник уроков и порядок занятий в программе курса."""

import argparse
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from app.catalog.subscriptions import can_open_material

MSK = timezone(timedelta(hours=3))


def can_open_lesson(material, lesson, *, signed_in, course_plus=False, course_access=None):
    if lesson["closed"] or course_access == 10:
        return False
    return lesson["trial"] or can_open_material(material, signed_in=signed_in, course_plus=course_plus,
                                                 course_access=course_access)


def program_availability(program):
    now = datetime.now(UTC)
    result = []
    for lesson in program:
        display = dict(lesson)
        opens_at = display.pop("opens_at")
        delayed = opens_at is not None and opens_at > now
        display["opens_on"] = opens_at.astimezone(MSK).strftime("%d.%m.%Y") if delayed and not display["closed"] else None
        display["closed"] = display["closed"] or delayed
        result.append(display)
    return tuple(result)


def _opening_time(value):
    try:
        timestamp = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return timestamp.replace(tzinfo=timestamp.tzinfo or UTC) if timestamp else None


def store_lessons(connection, reader):
    types = {row["Id"]: row["Title"] for row in reader.load_rows("LessonTypes") if row.get("Id")}
    lessons = {
        row["Id"]: (row["Id"], row["Title"].strip(), (row.get("Description") or "").strip(),
                    types.get(row.get("TypeId")), row.get("Duration"), row.get("IsGroup") == "t")
        for row in reader.load_rows("Lessons") if row.get("Id") and (row.get("Title") or "").strip()
        and row.get("IsDeleted") != "t"
    }
    links = [
        (row["Id"], row["CourseId"], row["LessonId"], row.get("ParentGroupId"),
         int(row.get("Order") or 0), (row.get("Title") or "").strip() or None,
         row.get("IsTrial") == "t", row.get("IsClosed") == "t", row.get("ClosedUntil"))
        for row in reader.load_rows("CourseLessonBindings")
        if row.get("Id") and row.get("CourseId") and row.get("LessonId") in lessons and row.get("IsDeleted") != "t"
    ]
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS lessons (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
            lesson_type TEXT, duration TEXT, is_group INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS course_lessons (
            id TEXT PRIMARY KEY, course_id TEXT NOT NULL, lesson_id TEXT NOT NULL REFERENCES lessons(id),
            parent_id TEXT, position INTEGER NOT NULL, display_title TEXT, trial INTEGER NOT NULL, closed INTEGER NOT NULL,
            closed_until TEXT
        );
        CREATE INDEX IF NOT EXISTS course_lessons_course ON course_lessons(course_id, position);
        CREATE INDEX IF NOT EXISTS course_lessons_lesson ON course_lessons(lesson_id);
        CREATE VIEW IF NOT EXISTS standalone_lessons AS
            SELECT * FROM lessons WHERE is_group = 0
            AND NOT EXISTS (SELECT 1 FROM course_lessons WHERE course_lessons.lesson_id = lessons.id);
    """)
    with connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(course_lessons)")}
        if "closed_until" not in columns:
            connection.execute("ALTER TABLE course_lessons ADD COLUMN closed_until TEXT")
        connection.execute("DELETE FROM course_lessons")
        connection.execute("DELETE FROM lessons")
        connection.executemany("INSERT INTO lessons VALUES (?, ?, ?, ?, ?, ?)", lessons.values())
        connection.executemany("INSERT INTO course_lessons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", links)
    return len(lessons), len(links), connection.execute("SELECT COUNT(*) FROM standalone_lessons").fetchone()[0]


def read_programs(source, published_courses):
    if source.suffix != ".sqlite":
        return {}
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'course_lessons'").fetchone()
        if not table:
            return {}
        columns = {row[1] for row in connection.execute("PRAGMA table_info(course_lessons)")}
        closing_time = "closed_until" if "closed_until" in columns else "NULL"
        rows = connection.execute(f"""
            SELECT course_id, course_lessons.id, lesson_id, parent_id, lessons.is_group,
                   COALESCE(display_title, lessons.title), lessons.lesson_type, trial, closed, {closing_time}
            FROM course_lessons JOIN lessons ON lessons.id = course_lessons.lesson_id
            ORDER BY position, course_lessons.rowid
        """).fetchall()

    courses = defaultdict(list)
    for course, *row in rows:
        if course in published_courses:
            courses[course].append(row)

    programs = {}
    for course, entries in courses.items():
        children = defaultdict(list)
        groups = {alias: lesson for binding, lesson, _, group, *_ in entries if group for alias in (binding, lesson)}
        for binding, lesson, parent, group, title, kind, trial, closed, until in entries:
            children[groups.get(parent)].append((lesson, group, title, kind, trial, closed, _opening_time(until)))

        def walk(parent, section=None, visited=frozenset(), parent_until=None, parent_closed=False):
            for lesson, group, title, kind, trial, closed, until in children[parent]:
                opens = max((date for date in (parent_until, until) if date), default=None)
                closed = parent_closed or (closed and until is None)
                if group and lesson not in visited:
                    yield from walk(lesson, f"{section} · {title}" if section else title, visited | {lesson}, opens, closed)
                elif not group:
                    yield {"title": title, "type": kind, "trial": bool(trial), "section": section,
                           "closed": closed, "opens_at": opens}

        programs[course] = tuple(walk(None))
    return programs


def main():
    from app.catalog.normalizer import PgDumpReader

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()
    with closing(sqlite3.connect(f"{args.catalog.resolve().as_uri()}?mode=rw", uri=True)) as connection:
        lessons, links, standalone = store_lessons(connection, PgDumpReader(args.backup))
    print(f"Справочник: {lessons} записей, {links} привязок к курсам, {standalone} отдельных уроков.")


if __name__ == "__main__":
    main()
