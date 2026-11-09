import unittest
from pathlib import Path

from app.catalog import load_catalog, load_history
from app.contracts import InteractionEvent, Material, Profile
from app.recommender import recommend
from app.recommender.engine import _expand_synonyms, _normalize_text, _tokenize_stems


class RecommenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        cls.fixtures_dir = root / "fixtures"
        cls.data_dir = root / "data"
        cls.catalog = load_catalog(cls.fixtures_dir / "catalog.demo.json")
        cls.history = load_history(cls.fixtures_dir / "history.demo.json")

    def test_short_synonym_requires_a_complete_word(self):
        certification = "Порядок сертификации продукции"
        self.assertNotIn("chatgpt", _expand_synonyms(_tokenize_stems(certification), _normalize_text(certification)))
        ai = "Как применить ИИ"
        self.assertIn("chatgpt", _expand_synonyms(_tokenize_stems(ai), _normalize_text(ai)))

    def test_empty_catalog_and_invalid_limits(self):
        self.assertEqual(recommend((), Profile(), (), 3).items, ())
        for lim in (0, -1, -5):
            self.assertEqual(recommend(self.catalog, Profile(), (), lim).items, ())

    def test_supports_limit_3_and_6(self):
        res3 = recommend(self.catalog, Profile(goal="маркетинг"), (), 3)
        res6 = recommend(self.catalog, Profile(), (), 6)
        self.assertLessEqual(len(res3.items), 3)
        self.assertEqual(len(res6.items), 6)
        self.assertEqual(len({i.material_id for i in res6.items}), 6)

    def test_scenario_1_new_user_business_promotion(self):
        """Сценарий 1 из Приложения 2: новичок указывает задачу «продвижение бизнеса»."""
        result = recommend(self.catalog, Profile(goal="продвижение бизнеса"), (), 3)
        self.assertTrue(result.items)
        self.assertEqual(result.items[0].material_id, "marketing-first-sales")
        self.assertTrue(result.ordering_reason)
        self.assertTrue(all(item.reason and item.next_step for item in result.items))

    def test_scenario_2_history_changes_ranking(self):
        """Сценарий 2 из Приложения 2: журнал действий меняет приоритет выдачи."""
        profile = Profile(goal="Маркетинг", stage="работающий бизнес")
        without_hist = recommend(self.catalog, profile, (), 3)
        with_hist = recommend(self.catalog, profile, self.history, 3)

        self.assertIn(without_hist.items[0].material_id, {"marketing-first-sales", "marketing-service-leads"})
        self.assertEqual(with_hist.items[0].material_id, "marketing-service-leads")
        # Пройденный start-checklist не должен быть на первом месте
        self.assertNotEqual(with_hist.items[0].material_id, "start-checklist")

    def test_scenario_3_weak_match_provides_follow_up(self):
        """Сценарий 3 из Приложения 2: запрос вне каталога возвращает общую подборку и уточнение."""
        result = recommend(
            self.catalog,
            Profile(goal="Как настроить экспортную сертификацию?"),
            (),
            3,
        )
        self.assertTrue(result.items)
        self.assertIsNotNone(result.follow_up_question)
        self.assertTrue(result.ordering_reason)

    def test_stage_and_experience_differentiate_profiles(self):
        """Пары профилей с разной стадией и опытом получают разные топ-1 рекомендации."""
        launch_res = recommend(
            self.catalog,
            Profile(goal="Продажи в рознице", industry="розница", stage="запуск"),
            (),
            3,
        )
        growth_res = recommend(
            self.catalog,
            Profile(goal="Продажи в рознице", industry="розница", stage="рост"),
            (),
            3,
        )
        self.assertEqual(launch_res.items[0].material_id, "marketing-first-sales")
        self.assertEqual(growth_res.items[0].material_id, "marketing-repeat-sales")

        cashflow_res = recommend(
            self.catalog,
            Profile(goal="УЧЕТ денежных потоков", stage="работающий бизнес", experience="начинающий"),
            (),
            3,
        )
        profit_res = recommend(
            self.catalog,
            Profile(goal="Финансы и учёт. Хочу сравнить прибыльность направлений", stage="рост", experience="опытный"),
            (),
            3,
        )
        self.assertEqual(cashflow_res.items[0].material_id, "finance-cashflow")
        self.assertEqual(profit_res.items[0].material_id, "finance-profitability")

    def test_ignores_unknown_history_events(self):
        unknown_history = (InteractionEvent(material_id="non-existent-id", kind="complete"),)
        result = recommend(self.catalog, Profile(goal="Планирование на рабочую неделю"), unknown_history, 3)
        self.assertEqual(result.items[0].material_id, "operations-week-plan")

    def test_hard_filters_free_format_and_ip_vs_ooo(self):
        custom_catalog = (
            Material(id="ip-tax", title="Налоги для ИП на УСН", url="https://example.com/1", topics=("налоги",), format="статья", access="free"),
            Material(id="ooo-tax", title="Налоги для ООО", url="https://example.com/2", topics=("налоги",), format="статья", access="free"),
            Material(id="paid-course", title="Платный курс по налогам ИП", url="https://example.com/3", topics=("налоги",), format="курс", access="paid"),
        )
        res = recommend(custom_catalog, Profile(goal="налоги для ИП только бесплатно"), (), 3)
        returned_ids = [i.material_id for i in res.items]
        self.assertIn("ip-tax", returned_ids)
        self.assertNotIn("ooo-tax", returned_ids)
        self.assertNotIn("paid-course", returned_ids)

    def test_real_normalized_catalog_if_present(self):
        norm_path = self.data_dir / "catalog.norm.json"
        if not norm_path.exists():
            self.skipTest("Нормализованный каталог ещё не сгенерирован в data/catalog.norm.json")
        real_cat = load_catalog(norm_path)
        self.assertGreater(len(real_cat), 1000)
        res = recommend(
            real_cat,
            Profile(
                goal="Как выбрать систему налогообложения для ИП",
                stage="запуск",
                industry="розница",
                experience="начинающий",
            ),
            (),
            6,
        )
        self.assertEqual(len(res.items), 6)
        self.assertTrue(all(item.reason and item.next_step for item in res.items))


if __name__ == "__main__":
    unittest.main()
