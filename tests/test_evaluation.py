import unittest
from functools import partial
from math import log2
from unittest.mock import patch

from app.contracts import Material, Profile, RecommendationResult
from app.recommender import recommend
from app.recommender.hybrid import cached_bm25, make_bm25, merge_orders
from evaluation.runner import FIXTURES_DIR, EvaluationCase, _ranking_scores, load_evaluation_inputs, run_evaluation
from evaluation.tune_weights import QUALITY_LIMITS, measure, quality_regressions, split_by_topic


class EvaluationTest(unittest.TestCase):
    def test_bm25_uses_field_weights_and_length_penalty_in_its_index(self):
        catalog = (
            Material("a-long", "Налоги", "https://example.com/long", "Налоги и отчётность предпринимателей"),
            Material("z-short", "Вводный обзор", "https://example.com/short", "Налоги"),
        )
        title_only = make_bm25(catalog, field_weights=(1, 0, 0))
        self.assertEqual(title_only("налоги"), ("a-long",))
        self.assertEqual(cached_bm25(catalog, field_weights=(0, 1, 0), length_penalty=0)("налоги"),
                         ("a-long", "z-short"))
        self.assertEqual(cached_bm25(catalog, field_weights=(0, 1, 0), length_penalty=1)("налоги"),
                         ("z-short", "a-long"))

        repeats = (
            Material("a-single", "Налоги и документы", "https://example.com/single"),
            Material("z-repeat", "Налоги налоги документы", "https://example.com/repeat"),
        )
        self.assertEqual(cached_bm25(repeats, field_weights=(1, 0, 0), saturation=0)("налоги"),
                         ("a-single", "z-repeat"))
        self.assertEqual(cached_bm25(repeats, field_weights=(1, 0, 0), saturation=2)("налоги"),
                         ("z-repeat", "a-single"))

    def test_weight_tuning_keeps_topics_together(self):
        cases = tuple(EvaluationCase(f"{topic}-{index}", "", Profile(), (), (f"{topic}-{index}",))
                      for topic in ("sales", "finance", "tax") for index in range(3))
        tuning, validation, train_topics, test_topics = split_by_topic(cases, .3, "fixed")
        self.assertEqual(len(validation), 3)
        self.assertEqual(set(tuning + validation), set(cases))
        self.assertFalse(set(train_topics) & set(test_topics))
        self.assertEqual(split_by_topic(cases, .3, "fixed")[2:], (train_topics, test_topics))

    def test_weight_tuning_matches_standard_evaluation(self):
        catalog, cases = load_evaluation_inputs("demo")
        offline = partial(recommend, explain_with_llm=False, balance_access=True, links_directory=FIXTURES_DIR)
        row = run_evaluation(offline, partial(offline, bm25_weight=.25)).summaries[-1]
        tuning = measure(.25, catalog, cases, FIXTURES_DIR)
        self.assertEqual((tuning["cases"], tuning["eligible"], tuning["hits"]), (50, row.eligible, row.hits))
        self.assertAlmostEqual(tuning["ndcg_at_3"], row.ndcg)
        self.assertAlmostEqual(tuning["mrr_at_3"], row.mrr)

    def test_weight_tuning_checks_specialized_metrics_in_their_own_directions(self):
        suites = {}
        for name, metric, _ in QUALITY_LIMITS:
            suites.setdefault(name, {})[metric] = .5

        def report(suite_scores):
            return {"specialized": {"suites": [{"title": title, "metrics": scores}
                                              for title, scores in suite_scores.items()]}}

        reference = report(suites)
        candidate = report({**suites, "Педагогическая прогрессия": {"2+ базовых/средних ↑": .6},
                            "Семантические дубли": {"Списки с дублем ↓": .6}})
        issues = quality_regressions(candidate, reference)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["suite"], "Семантические дубли")

    def test_rank_fusion_supports_iterators_and_can_disable_a_signal(self):
        orders = (("core", "both"), ("text", "both"))
        self.assertEqual(merge_orders(iter(orders))[0], "both")
        self.assertEqual(merge_orders(orders, weights=(1, 0)), ["core", "both"])
        self.assertEqual(merge_orders(orders, weights=(0, 1)), ["text", "both"])

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
            empty = lambda *args, **kwargs: RecommendationResult((), "")
            report = run_evaluation(empty, empty, suite="real", real_catalog=catalog)
        baseline = report.summaries[0]
        self.assertEqual((baseline.eligible, baseline.hits, baseline.mrr, baseline.map_at_k), (2, 1, 1 / 4, 1 / 4))
        self.assertTrue(all(row.material_ids == ("top", "lower") for row in report.rows[::3]))
