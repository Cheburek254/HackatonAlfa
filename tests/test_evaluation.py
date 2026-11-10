import unittest
from math import log2
from unittest.mock import patch

from app.contracts import Material, RecommendationResult
from evaluation.runner import _ranking_scores, run_evaluation


class EvaluationTest(unittest.TestCase):
    def test_ranking_scores_discount_later_matches_and_ignore_duplicates(self):
        rr, precision, ndcg, ap = _ranking_scores(("other", "match", "match"), {"match", "another"})
        self.assertEqual(rr, 1 / 2)
        self.assertEqual(precision, 1 / 3)
        self.assertAlmostEqual(ndcg, (1 / log2(3)) / (1 + 1 / log2(3)))
        self.assertEqual(ap, 1 / 4)

    def test_popularity_baseline_only_uses_courses_and_ignores_profile(self):
        catalog = (
            Material("article", "Статья", "https://example.com/article", format="статья", popularity=100),
            Material("lower", "Второй курс", "https://example.com/lower", format="курс", popularity=20),
            Material("top", "Первый курс", "https://example.com/top", entity_type="course", popularity=30),
            Material("unknown", "Курс без просмотров", "https://example.com/unknown", format="курс"),
        )
        cases = [
            {"id": "course", "note": "", "profile": {"goal": "курс"}, "relevant_ids": ["lower"]},
            {"id": "article", "note": "", "profile": {"goal": "статья"}, "relevant_ids": ["article"]},
            {"id": "outside", "note": "", "profile": {}, "relevant_ids": []},
        ]
        with patch("evaluation.runner._load_cases", return_value=cases):
            report = run_evaluation(lambda *args, **kwargs: RecommendationResult((), ""), suite="real", real_catalog=catalog)
        baseline = report.summaries[0]
        self.assertEqual((baseline.eligible, baseline.hits, baseline.mrr, baseline.map_at_k), (2, 1, 1 / 4, 1 / 4))
        self.assertTrue(all(row.material_ids == ("top", "lower") for row in report.rows[::3]))
