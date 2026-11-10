"""Справочник уроков и порядок занятий в программе курса."""

import argparse
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

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
         row.get("IsTrial") == "t", row.get("IsClosed") == "t")
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
            parent_id TEXT, position INTEGER NOT NULL, display_title TEXT, trial INTEGER NOT NULL, closed INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS course_lessons_course ON course_lessons(course_id, position);
        CREATE INDEX IF NOT EXISTS course_lessons_lesson ON course_lessons(lesson_id);
        CREATE VIEW IF NOT EXISTS standalone_lessons AS
            SELECT * FROM lessons WHERE is_group = 0
            AND NOT EXISTS (SELECT 1 FROM course_lessons WHERE course_lessons.lesson_id = lessons.id);
    """)
    with connection:
        connection.execute("DELETE FROM course_lessons")
        connection.execute("DELETE FROM lessons")
        connection.executemany("INSERT INTO lessons VALUES (?, ?, ?, ?, ?, ?)", lessons.values())
        connection.executemany("INSERT INTO course_lessons VALUES (?, ?, ?, ?, ?, ?, ?, ?)", links)
    return len(lessons), len(links), connection.execute("SELECT COUNT(*) FROM standalone_lessons").fetchone()[0]


def read_programs(source, published_courses):
    if source.suffix != ".sqlite":
        return {}
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'course_lessons'").fetchone()
        if not table:
            return {}
        rows = connection.execute("""
            SELECT course_id, course_lessons.id, lesson_id, parent_id, lessons.is_group,
                   COALESCE(display_title, lessons.title), lessons.lesson_type, trial, closed
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
        for binding, lesson, parent, group, title, kind, trial, closed in entries:
            children[groups.get(parent)].append((lesson, group, title, kind, trial, closed))

        def walk(parent, section=None, visited=frozenset()):
            for lesson, group, title, kind, trial, closed in children[parent]:
                if closed:
                    continue
                if group and lesson not in visited:
                    yield from walk(lesson, f"{section} · {title}" if section else title, visited | {lesson})
                elif not group:
                    yield {"title": title, "type": kind, "trial": bool(trial), "section": section}

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
