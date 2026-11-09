import unittest

from app.contracts import Profile, Recommendation, RecommendationResult
from app.ui.web import create_app


class WebFlowTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.client = create_app(self.recommend).test_client()

    def recommend(self, catalog, profile, history, limit):
        self.calls.append((profile, history, limit))
        item = Recommendation(catalog[0].id, "Подходит для старта", "Сверьте план запуска")
        return RecommendationResult((item,), "Сначала вводный материал", "Что уже сделали?")

    def test_cold_start_renders_the_result_from_recommend(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, [(Profile(), (), 3)])
        page = response.get_data(as_text=True)
        self.assertIn("Сначала вводный материал", page)
        self.assertIn("Подходит для старта", page)
        self.assertIn("Сверьте план запуска", page)
        self.assertIn("Что уже сделали?", page)
        self.assertIn("https://example.com/alfakurs-demo/start-checklist", page)

    def test_quick_goal_and_sample_history_reach_recommend(self):
        response = self.client.post("/", data={
            "goal": "Предыдущая задача",
            "quick_goal": "Увеличить продажи",
            "stage": "рост",
            "industry": "розница",
            "experience": "опытный",
            "history": "sample",
        })

        self.assertEqual(response.status_code, 200)
        profile, history, limit = self.calls[-1]
        self.assertEqual(profile, Profile("рост", "розница", "Увеличить продажи", "опытный"))
        self.assertEqual({event.kind for event in history}, {"view", "click", "complete"})
        self.assertEqual(limit, 3)

    def test_skip_profile_keeps_the_goal_and_clears_history(self):
        response = self.client.post("/", data={
            "goal": "  Найти клиентов  ",
            "stage": "запуск",
            "history": "sample",
            "action": "skip_profile",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[-1], (Profile(goal="Найти клиентов"), (), 3))
