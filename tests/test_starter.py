import unittest
from pathlib import Path

from app.catalog import load_catalog, load_history
from app.contracts import InteractionEvent, Profile, Recommender
from app.ui.stub import recommend


class StarterContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures = Path(__file__).resolve().parents[1] / "fixtures"
        cls.catalog = load_catalog(fixtures / "catalog.demo.json")
        cls.history = load_history(fixtures / "history.demo.json")

    def test_fixture_keeps_optional_metadata_optional(self):
        materials_by_id = {material.id: material for material in self.catalog}
        starter = materials_by_id["marketing-first-sales"]
        sparse = materials_by_id["operations-week-plan"]

        self.assertIn("маркетинг", starter.topics)
        self.assertEqual(starter.access, "free")
        self.assertEqual(sparse.industries, ())
        self.assertEqual(sparse.access, "unknown")

    def test_history_fixture_references_catalog(self):
        catalog_ids = {material.id for material in self.catalog}
        self.assertTrue(all(event.material_id in catalog_ids for event in self.history))
        self.assertEqual({event.kind for event in self.history}, {"view", "click", "complete"})
        self.assertIsNotNone(self.history[0].occurred_at.tzinfo)
        self.assertIsNone(self.history[-1].occurred_at)

    def test_stub_returns_renderable_cards_for_empty_profile(self):
        recommender: Recommender = recommend
        result = recommender(self.catalog, Profile(), (), 3)
        ids = [card.material_id for card in result.items]

        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(set(ids) <= {material.id for material in self.catalog})
        self.assertTrue(result.ordering_reason)
        self.assertTrue(all(card.reason and card.next_step for card in result.items))

    def test_stub_handles_empty_catalog_and_non_positive_limits(self):
        self.assertEqual(recommend((), Profile(), (), 3).items, ())
        for limit in (0, -1):
            with self.subTest(limit=limit):
                self.assertEqual(recommend(self.catalog, Profile(), (), limit).items, ())

    def test_history_promotes_related_materials_without_overriding_the_goal(self):
        history = (InteractionEvent("start-checklist", "view"), InteractionEvent("marketing-first-sales", "click"))
        result = recommend(self.catalog, Profile(), history, 3)
        self.assertEqual(result.items[0].material_id, "marketing-repeat-sales")
        self.assertIn("просмотров", result.ordering_reason)
        result = recommend(self.catalog, Profile(goal="финансы"), history, 3)
        self.assertEqual(result.items[0].material_id, "finance-cashflow")
