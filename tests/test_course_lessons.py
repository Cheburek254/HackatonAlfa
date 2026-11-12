import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from app.catalog.lessons import can_open_lesson, program_availability, read_programs, store_lessons
from app.contracts import Material


class Reader:
    def load_rows(self, name):
        lessons = [
            {"Id": "module", "Title": "Модуль", "IsGroup": "t"},
            {"Id": "closed-module", "Title": "Закрытый модуль", "IsGroup": "t"},
            {"Id": "first", "Title": "Введение"},
            {"Id": "second", "Title": "Практика"},
            {"Id": "restricted", "Title": "Закрытый урок"},
            {"Id": "single", "Title": "Отдельный урок"},
            {"Id": "draft-lesson", "Title": "Урок черновика"},
            {"Id": "deleted", "Title": "Удалённый", "IsDeleted": "t"},
        ]
        links = [
            {"Id": "b2", "LessonId": "second", "Order": "2", "ParentGroupId": "group-link"},
            {"Id": "b1", "LessonId": "first", "Order": "1", "ParentGroupId": "module", "IsTrial": "t"},
            {"Id": "group-link", "LessonId": "module", "Order": "1"},
            {"Id": "b3", "LessonId": "closed-module", "Order": "2", "IsClosed": "t", "ClosedUntil": "2099-01-02 00:00:00+00"},
            {"Id": "b4", "LessonId": "restricted", "Order": "1", "ParentGroupId": "closed-module"},
            {"Id": "bd", "LessonId": "draft-lesson", "CourseId": "draft", "Order": "1"},
            {"Id": "removed", "LessonId": "deleted", "Order": "4"},
        ]
        return {"Lessons": lessons, "LessonTypes": [],
                "CourseLessonBindings": [{"CourseId": "published", **link} for link in links]}[name]


class CourseLessonsTest(unittest.TestCase):
    def test_import_and_program_with_mixed_group_ids(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.sqlite"
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(store_lessons(connection, Reader()), (7, 6, 1))
                self.assertEqual(store_lessons(connection, Reader()), (7, 6, 1))
                self.assertEqual(connection.execute("SELECT id FROM standalone_lessons").fetchall(), [("single",)])
            programs = read_programs(path, {"published"})
            self.assertEqual(list(programs), ["published"])
            self.assertEqual([(item["section"], item["title"], item["trial"]) for item in programs["published"]],
                             [("Модуль", "Введение", True), ("Модуль", "Практика", False),
                              ("Закрытый модуль", "Закрытый урок", False)])
            first, regular, scheduled = program_availability(programs["published"])
            course = Material("published", "Платный курс", "https://example.com/course", entity_type="course", access="paid")
            self.assertTrue(can_open_lesson(course, first, signed_in=False))
            self.assertFalse(can_open_lesson(course, regular, signed_in=False))
            for status, allowed in ((0, False), (1, True), (2, True), (10, False)):
                self.assertEqual(can_open_lesson(course, regular, signed_in=True, course_access=status), allowed)
            self.assertEqual(scheduled["opens_on"], "02.01.2099")
            self.assertFalse(can_open_lesson(course, scheduled, signed_in=True, course_access=1))
