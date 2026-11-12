import unittest

from app.catalog.editorial import ENTITY_TYPES, editorial_links
from app.catalog.subscriptions import access_category, access_label, can_open_material
from app.contracts import Material


class CatalogRulesTest(unittest.TestCase):
    def test_editorial_edges_keep_source_type_and_order(self):
        article, document = (next(key for key, value in ENTITY_TYPES.items() if value == kind)
                             for kind in ("article", "document"))
        material_types = {"source": "article", "first": "article", "second": "document", "course": "course"}
        rows = [
            {"Id": "source", "ItemId": "second", "ItemTypeId": document, "Order": "2"},
            {"Id": "source", "ItemId": "first", "ItemTypeId": article, "Order": "1", "CourseId": "course"},
            {"Id": "course", "ItemId": "first", "ItemTypeId": document, "Order": "1"},
            {"Id": "outside", "ItemId": "first", "ItemTypeId": article, "Order": "1"},
        ]
        self.assertEqual(editorial_links(rows, material_types), {"source": ["first", "second"]})

    def test_login_gate_is_separate_from_price(self):
        article = Material("id", "Статья", "https://example.com/article", entity_type="article",
                           access="free", requires_login=True)
        self.assertEqual(access_category(article), "free")
        self.assertEqual(access_label(article), "Нужен вход на Альфа-Курсе")
        self.assertFalse(can_open_material(article, signed_in=False))
        self.assertTrue(can_open_material(article, signed_in=True))
