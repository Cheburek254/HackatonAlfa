import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from app.catalog.lessons import read_programs, store_lessons


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
            {"Id": "b3", "LessonId": "closed-module", "Order": "2", "IsClosed": "t"},
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
                             [("Модуль", "Введение", True), ("Модуль", "Практика", False)])
