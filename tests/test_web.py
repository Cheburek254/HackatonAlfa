import json
import re
import sqlite3
import unittest
from contextlib import closing
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from app.contracts import Material, Profile, Recommendation, RecommendationResult
from app.ctr_report import DailyActivity, Metrics, read_daily_activity, read_report, read_top_cards
from app.live_data import LiveDataSnapshot
from app.recommender.hybrid import merge_orders
from app.ui.accounts import create_admin_account, current_account, record_interaction
from app.ui.experiments import DAY, end_experiment, group_metrics, start_experiment
from app.ui.stub import recommend as stub_recommend
from app.ui.web import create_app
from evaluation.runner import EVALUATION_DIR, EvaluationReport, EvaluationRow, EvaluationSummary, summary_payload


class WebFlowTest(unittest.TestCase):
    business = {
        "stage": "рост", "industry": "производство", "experience": "опытный",
        "team_size": "6–15 человек", "learning_topic": "финансы", "preferred_format": "шаблон",
    }
    password = "A long test passphrase!"

    def setUp(self):
        self.calls = []
        directory = self.enterContext(TemporaryDirectory())
        self.database_path = Path(directory) / "accounts.sqlite3"
        self.enterContext(patch("app.catalog.DATA_DIR", Path(directory)))
        self.enterContext(patch.dict("os.environ", {"ALFA_CATALOG_DIR": "", "ALFA_LLM_API_KEY": "", "ALFA_SMTP_HOST": ""}))
        self.app = create_app(self.recommend, database_path=self.database_path)
        self.client = self.app.test_client()

    def post_form(self, path, data, *, client=None, page=None):
        client = client or self.client
        client.get(page or path)
        with client.session_transaction() as browser_session:
            token = browser_session["csrf"]
        return client.post(path, data={**data, "csrf_token": token})

    def register(self, username="owner", business=None):
        return self.post_form("/register", {
            "username": username, "password": self.password,
            **(self.business if business is None else business),
        })

    def recommend(self, catalog, profile, history, limit):
        self.calls.append((profile, history, limit))
        item = Recommendation(catalog[0].id, "Подходит для старта", "Сверьте план запуска")
        return RecommendationResult((item,), "Сначала вводный материал", "Что уже сделали?")

    def test_cold_start_renders_the_result_from_recommend(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls, [(Profile(), (), 7)])
        page = response.get_data(as_text=True)
        self.assertIn("Сначала вводный материал", page)
        self.assertIn("Подходит для старта", page)
        self.assertIn("Сверьте план запуска", page)
        self.assertIn("Что уже сделали?", page)
        self.assertIn('href="/materials/start-checklist?impression=', page)

    def test_profile_history_shows_only_current_account_activity(self):
        self.assertEqual(self.register("history-owner").status_code, 303)
        with self.client:
            self.client.get("/profile")
            account = current_account()
            record_interaction(account, "start-checklist", "view")
        page = self.client.get("/profile").get_data(as_text=True)
        self.assertIn('id="profile-history"', page)
        self.assertIn('href="/materials/start-checklist"', page)
        self.assertIn("Просмотр карточки", page)
        self.assertIn("МСК", page)
        other = self.app.test_client()
        self.assertEqual(self.post_form("/register", {**self.business, "username": "history-other",
                                                     "password": self.password}, client=other).status_code, 303)
        empty = other.get("/profile").get_data(as_text=True)
        self.assertIn("История появится после первого просмотра", empty)
        self.assertNotIn('href="/materials/start-checklist"', empty)

    def test_forgot_password_uses_saved_email_and_revokes_sessions(self):
        mail = []
        self.app.config["PASSWORD_CODE_SENDER"] = lambda recipient, code: mail.append((recipient, code))
        self.assertEqual(self.register("recovery-owner").status_code, 303)
        email = "account@example.com"
        self.assertEqual(self.post_form("/profile/settings", {**self.business, "email": email}).status_code, 303)
        guest = self.app.test_client()
        other = self.app.test_client()
        for browser, address in ((other, "wrong@example.com"), (guest, email)):
            response = self.post_form("/forgot-password", {"username": "recovery-owner", "email": address}, client=browser)
            self.assertEqual(response.location, "/reset-password")
        self.assertEqual(len(mail), 1)
        self.assertEqual(mail[0][0], email)
        code = mail[0][1]
        self.assertNotEqual(code, "000000")
        form = {"new_password": "An updated passphrase!", "confirm_password": "An updated passphrase!"}
        self.assertEqual(self.post_form("/reset-password", {**form, "email_code": code}, client=other).status_code, 400)
        self.assertEqual(self.post_form("/reset-password", {**form, "email_code": "000000"}, client=guest).status_code, 400)
        result = self.post_form("/reset-password", {**form, "email_code": code}, client=guest)
        self.assertEqual(result.location, "/login")
        self.assertEqual(self.client.get("/profile").location, "/login")
        self.assertEqual(guest.get("/reset-password").location, "/forgot-password")
        self.assertEqual(self.post_form("/login", {"username": "recovery-owner", "password": form["new_password"]},
                                        client=guest).status_code, 303)

    def test_demo_recovery_accepts_grouped_code_without_changing_account_password(self):
        self.assertEqual(self.register("demo-recovery-owner").status_code, 303)
        guest = self.app.test_client()
        response = self.post_form("/forgot-password", {"username": "demo-recovery-owner", "email": "owner@example.com"},
                                  client=guest)
        self.assertEqual(response.location, "/reset-password")
        self.assertIn("000 000", guest.get(response.location).get_data(as_text=True))
        new_password = "Simulated password change!"
        result = self.post_form("/reset-password", {"email_code": "000 000", "new_password": new_password,
                                                    "confirm_password": new_password}, client=guest)
        self.assertEqual(result.location, "/login")
        self.assertIn("Демо восстановления завершено", guest.get(result.location).get_data(as_text=True))
        self.assertEqual(self.post_form("/login", {"username": "demo-recovery-owner", "password": self.password},
                                        client=guest).status_code, 303)

    def test_admin_ranking_mode_applies_to_guests_and_accounts_and_survives_restart(self):
        app = create_app(database_path=self.database_path)
        with app.app_context():
            self.assertTrue(create_admin_account("ranking-admin", self.password))
        admin_client, guest, account = app.test_client(), app.test_client(), app.test_client()
        endpoint = "/admin/recommendation-mode"
        self.assertEqual(guest.post(endpoint, data={"mode": "popular"}).location, "/admin/login")
        self.assertEqual(self.post_form("/login/demo", {}, client=account, page="/login").status_code, 303)
        self.assertEqual(account.post(endpoint, data={"mode": "popular"}).status_code, 403)
        self.assertEqual(self.post_form("/admin/login", {"username": "ranking-admin", "password": self.password},
                                        client=admin_client).status_code, 303)
        self.assertEqual(admin_client.post(endpoint, data={"mode": "popular"}).status_code, 400)
        self.assertEqual(self.post_form(endpoint, {"mode": "unknown"}, client=admin_client, page="/admin").status_code, 400)

        def select(mode):
            response = self.post_form(endpoint, {"mode": mode}, client=admin_client, page="/admin")
            self.assertEqual(response.location, "/admin#admin-ranking-mode")

        def results(browser, goal):
            return browser.post("/", data={"goal": goal}).get_data(as_text=True)

        def cards(page):
            return re.findall(r'data-material-id="([^"]+)"', page)

        select("popular")
        baseline = results(guest, "первые клиенты")
        self.assertIn("Курсы отсортированы по популярности", baseline)
        self.assertEqual(cards(baseline), cards(results(account, "налоги")))
        self.assertNotIn("start-checklist", cards(baseline))
        fresh_app = create_app(database_path=self.database_path)
        self.assertEqual(cards(results(fresh_app.test_client(), "учёт")), cards(baseline))
        select("recommend")
        self.assertNotEqual(cards(results(guest, "финансы")), cards(baseline))
        select("hybrid")
        self.assertIn("Подборка учитывает совпадения", results(guest, "финансы"))

    def test_hybrid_uses_adaptive_weights_and_preserves_older_ab_arm(self):
        catalog = (Material("template", "Акт сверки взаиморасчётов", "https://example.com/template",
                            description="Помогает сверить задолженность контрагента."),)
        with patch("app.ui.web.load_catalog", return_value=catalog):
            app = create_app(database_path=self.database_path)
        goal = {"goal": "Хочу сверить задолженность контрагента."}

        def weight(client, profile):
            with patch("app.recommender.engine.merge_orders", wraps=merge_orders) as fuse:
                self.assertEqual(client.post("/", data={**goal, **profile}).status_code, 200)
            return fuse.call_args.kwargs["weights"][1]

        self.assertEqual(weight(app.test_client(), {}), 0.749)
        self.assertEqual(weight(app.test_client(), {"experience": "начинающий"}), 0.25)
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("ALTER TABLE ab_experiments DROP COLUMN bm25_adaptive")
            connection.execute("""INSERT INTO ab_experiments (mode_a, mode_b, share_a, traffic, bm25_weight, seed, started_at)
                                  VALUES ('hybrid', 'recommend', 50, 100, 0.25, 'old-run', 1)""")
        with patch("app.ui.web.load_catalog", return_value=catalog):
            app = create_app(database_path=self.database_path)
        with patch("app.ui.experiments._arm", return_value="A"):
            self.assertEqual(weight(app.test_client(), {}), 0.25)
            with app.app_context():
                self.assertTrue(end_experiment(1))
                self.assertTrue(start_experiment("hybrid", "recommend", 50, 100, 0.25))
            self.assertEqual(weight(app.test_client(), {}), 0.749)

    def test_ab_enrollment_and_metrics_follow_guests_through_registration_and_stop(self):
        app = create_app(database_path=self.database_path)
        with app.app_context():
            self.assertTrue(create_admin_account("experiment-admin", self.password))
        admin, first, second = app.test_client(), app.test_client(), app.test_client()
        path = "/admin/experiments"
        form = {"action": "start", "mode_a": "popular", "mode_b": "recommend", "share_a": 50, "traffic": 100}
        self.assertEqual(first.post(path, data=form).location, "/admin/login")
        self.assertEqual(self.post_form("/admin/login", {"username": "experiment-admin", "password": self.password},
                                        client=admin).status_code, 303)
        self.assertEqual(admin.post(path, data=form).status_code, 400)
        self.post_form(path, {**form, "mode_b": "popular"}, client=admin)
        self.assertIn("Выберите два разных алгоритма", admin.get(path).get_data(as_text=True))
        self.assertEqual(self.post_form(path, form, client=admin).status_code, 303)
        self.post_form(path, form, client=admin)
        self.assertIn("Завершите текущий запуск", admin.get(path).get_data(as_text=True))

        with patch("app.ui.experiments._arm", side_effect=("A", "B")) as assign:
            control_page = first.get("/").get_data(as_text=True)
            self.assertIn("Курсы отсортированы по популярности", control_page)
            self.assertIn("Курсы отсортированы по популярности", first.get("/").get_data(as_text=True))
            treatment_page = second.get("/").get_data(as_text=True)
            self.assertNotIn("Курсы отсортированы по популярности", treatment_page)
            admin.get("/")
            app.test_client().get("/", headers={"User-Agent": "ExampleBot"})
            app.test_client().get("/", headers={"Sec-Purpose": "prefetch"})
            self.assertEqual(assign.call_count, 2)

        def first_link(html):
            return re.search(r'class="material-link" href="([^"]+\?impression=[^"]+)"', html)[1]

        control = first.get(first_link(control_page), headers={"Accept": "application/json"}).json
        self.assertEqual(first.post(control["open_url"], data={"csrf_token": control["csrf_token"]}).status_code, 303)
        treatment = second.get(first_link(treatment_page), headers={"Accept": "application/json"})
        self.assertEqual(treatment.status_code, 200)
        self.assertEqual(self.post_form("/register", {**self.business, "username": "experiment-user",
                                                     "password": self.password}, client=first).status_code, 303)
        self.assertIn("Курсы отсортированы по популярности", first.get("/").get_data(as_text=True))
        other_device = app.test_client()
        self.post_form("/login", {"username": "experiment-user", "password": self.password}, client=other_device)
        self.assertIn("Курсы отсортированы по популярности", other_device.get("/").get_data(as_text=True))
        self.post_form(path, {"action": "stop", "experiment_id": 1}, client=admin)
        self.assertNotIn("Курсы отсортированы по популярности", first.get("/").get_data(as_text=True))
        self.assertIn("История запусков", admin.get(path).get_data(as_text=True))

        now = int(datetime.now(UTC).timestamp())
        with patch("app.ui.experiments.time", return_value=now + 7 * DAY):
            self.assertEqual(second.get("/login").status_code, 200)
        with patch("app.ui.experiments.time", return_value=now + 8 * DAY), app.app_context():
            stats = group_metrics(1)
            self.assertEqual((stats["A"].assigned, stats["A"].participants, stats["A"].impressions, stats["A"].clicks,
                              stats["A"].previews, stats["A"].d7_ready, stats["A"].d7_returned), (1, 1, 1, 1, 1, 1, 0))
            self.assertEqual((stats["B"].assigned, stats["B"].d7_ready, stats["B"].d7_returned), (1, 1, 1))

    def test_daily_articles_and_podcasts_precede_search_and_use_cross_format_history(self):
        directory = self.database_path.parent / "daily-catalog"
        directory.mkdir()
        records = []
        for mid, kind, fmt, topic in (
            ("fresh", "article", "статья", "финансы"),
            ("article-marketing", "article", "статья", "маркетинг"),
            ("article-team", "article", "статья", "команда"),
            ("article-plan", "article", "статья", "планирование"),
            ("article-sale", "article", "статья", "продажи"),
            ("podcast-money", "podcast", "подкаст", "финансы"),
            ("podcast-marketing", "podcast", "подкаст", "маркетинг"),
            ("podcast-team", "podcast", "подкаст", "команда"),
            ("podcast-plan", "podcast", "подкаст", "планирование"),
            ("podcast-sale", "podcast", "подкаст", "продажи"),
            ("course-one", "course", "курс", "маркетинг"),
            ("course-two", "course", "курс", "маркетинг"),
        ):
            records.append({"id": mid, "title": f"{fmt} про {topic} {mid}", "url": f"https://example.com/{mid}",
                            "entity_type": kind, "format": fmt, "topics": [topic], "access": "free"})
        (directory / "catalog.norm.json").write_text(json.dumps(records), encoding="utf-8")
        (directory / "publication-dates.json").write_text(
            json.dumps({"fresh": "2026-01-08", "article-marketing": "2025-01-08", "podcast-money": "2026-01-06"}),
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"ALFA_CATALOG_DIR": str(directory)}):
            client = create_app(database_path=self.database_path).test_client()

        def shelf(page, kind):
            return [link for entry_kind, link in re.findall(
                r'data-daily-kind="(article|podcast)">\s*<a [^>]+href="([^"]+)"', page
            ) if entry_kind == kind]

        home = client.get("/").get_data(as_text=True)
        self.assertLess(home.index('id="recommendation-query"'), home.index('id="daily-track"'))
        self.assertEqual(re.findall(r'data-daily-kind="([^"]+)"', home),
                         ["article"] * 3 + ["podcast"] * 3 + ["article"] * 2 + ["podcast"] * 2)
        self.assertIn('datetime="2026-01-08"', home)
        self.assertLess(shelf(home, "article").index("/materials/fresh"),
                        shelf(home, "article").index("/materials/article-marketing"))
        for mid in ("course-one", "course-two"):
            preview = client.get(f"/materials/{mid}", headers={"Accept": "application/json"}).json
            self.assertEqual(client.post(preview["open_url"], data={"csrf_token": preview["csrf_token"]}).status_code, 303)
        personalized = client.get("/").get_data(as_text=True)
        self.assertLess(shelf(personalized, "article").index("/materials/article-marketing"),
                        shelf(personalized, "article").index("/materials/fresh"))
        self.assertLess(shelf(personalized, "podcast").index("/materials/podcast-marketing"),
                        shelf(personalized, "podcast").index("/materials/podcast-money"))
        fragment = client.post("/", data={"goal": "маркетинг"}, headers={"X-Recommendation-Fragment": "1"}).get_data(as_text=True)
        self.assertNotIn('id="daily-picks"', fragment)

    def test_admin_login_report_and_role_management(self):
        self.assertEqual(self.client.get("/admin").location, "/admin/login")
        self.assertEqual(self.register().status_code, 303)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        credentials = {"username": "owner", "password": self.password}
        self.assertEqual(self.post_form("/admin/login", credentials).status_code, 401)
        runner = self.app.test_cli_runner()
        self.assertEqual(runner.invoke(args=["admin", "grant", "demo"]).exit_code, 1)
        self.assertEqual(runner.invoke(args=["admin", "grant", "owner"]).exit_code, 0)
        self.assertEqual(self.client.get("/admin").location, "/admin/login")
        self.assertEqual(self.post_form("/admin/login", credentials).location, "/admin")
        self.assertEqual(self.client.post("/admin/accounts", data={"action": "grant", "username": "demo"}).status_code, 400)

        guest = self.app.test_client()
        self.assertEqual(self.post_form("/register", {
            **self.business, "username": "nextadmin", "password": self.password,
        }, client=guest).status_code, 303)
        self.assertEqual(self.post_form("/admin/accounts", {
            "action": "grant", "username": "nextadmin",
        }, page="/profile", client=guest).status_code, 403)
        self.assertEqual(self.post_form("/admin/accounts", {
            "action": "grant", "username": "nextadmin",
        }, page="/admin/settings").status_code, 303)
        self.assertEqual(guest.get("/admin").location, "/admin/login")
        self.assertEqual(self.post_form("/admin/login", {
            "username": "nextadmin", "password": self.password,
        }, client=guest).location, "/admin")

        new_account = {"action": "create", "username": "newadmin", "password": self.password}
        self.assertEqual(self.post_form("/admin/accounts", new_account, page="/admin/settings").status_code, 303)
        new_admin = self.app.test_client()
        self.assertEqual(self.post_form("/admin/login", new_account, client=new_admin).location, "/admin")

        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.executemany(
                """INSERT INTO recommendation_impressions
                   (batch_id, material_id, position, algorithm, result_order, audience, shown_at, clicked_at)
                   VALUES (?, 'test-card', ?, ?, ?, ?, 100, ?)""",
                [("1", 1, "algorithm-a", "recommended", "guest", 101),
                 ("2", 1, "algorithm-a", "recommended", "account", None),
                 ("3", 2, "algorithm-a", "popularity", "account", None),
                 ("4", 1, "algorithm-a", "recommended", "demo", 101),
                 ("5", 1, "algorithm-b", "recommended", "guest", None),
                 ("6", 1, "demo-only", "recommended", "demo", 101),
                 ("7", 1, "app.ai.stub.recomend", "recommended", "guest", 101)],
            )
        totals, detail = read_report(self.database_path)
        self.assertEqual(totals, {"algorithm-a": Metrics(3, 1), "algorithm-b": Metrics(1, 0)})
        self.assertEqual(detail[0], ("algorithm-a", "recommended", 1, Metrics(2, 1)))
        self.assertAlmostEqual(totals["algorithm-a"].ctr, 100 / 3)
        summary = new_admin.get("/admin").get_data(as_text=True)
        self.assertIn("Оценка качества рекомендаций", summary)
        self.assertIn('href="/admin/tests"', summary)
        self.assertNotIn("Результаты по сценариям", summary)
        self.assertNotIn("Сводка по алгоритмам", summary)
        page = new_admin.get("/admin/analytics").get_data(as_text=True)
        self.assertIn("Сводка по алгоритмам", page)
        self.assertIn("50,00%", page)
        self.assertNotIn("demo-only", page)
        self.assertIn("Архив · исходный тестовый сценарий", page)
        self.assertIn("app.ai.stub.recomend", page)
        self.assertIn("Эффективность карточек", page)
        self.assertIn("Динамика за 30 дней", page)
        report_dir = self.database_path.parent
        self.app.config.update(EVALUATION_SUMMARY_DIR=report_dir, CATALOG_QUALITY_FINGERPRINT="test-catalog")
        methods = ("Бейзлайн · популярные курсы", "recommend", "BM25 + recommend")
        for suite, count, fingerprint, generated_at in (
            ("demo", 50, None, "2026-11-10T19:30:10+00:00"),
            ("real", 350, "test-catalog", "2026-11-10T21:57:55+00:00"),
        ):
            rows = (EvaluationRow("outside-catalog", "Нет точного совпадения", None, (), "recommend", (), None,
                                  None, None, None),) if suite == "demo" else ()
            report = EvaluationReport(tuple(EvaluationSummary(method, count, 1, 0.1, 0.1, 0.1, 0.1)
                                            for method in methods), rows, count)
            payload = summary_payload(report, suite, .25, fingerprint, include_cases=True, adaptive=True)
            (report_dir / f"ranking-{suite}.json").write_text(
                json.dumps({**payload, "generated_at": generated_at}), encoding="utf-8",
            )
        comparison = new_admin.get("/admin/tests").get_data(as_text=True)
        self.assertIn("Сценариев в наборе · 50", comparison)
        self.assertIn("Синтетический каталог · сохранённый прогон", comparison)
        self.assertIn("Реальный каталог · сохранённый прогон", comparison)
        self.assertIn("Сценариев в наборе · 350", comparison)
        self.assertIn("Последнее тестирование · <strong>11.11.2026 00:57 МСК</strong>", comparison)
        self.assertIn("Последнее тестирование · <strong>11.11.2026 00:57 МСК</strong>",
                      new_admin.get("/admin").get_data(as_text=True))
        self.assertIn("Hit@3", comparison)
        self.assertIn("MRR@3", comparison)
        for metric in ("Precision@3", "nDCG@3", "MAP@3", "популярные курсы", "BM25 + recommend"):
            self.assertIn(metric, comparison)
        self.assertIn("План A/B-теста", new_admin.get("/admin/roadmap").get_data(as_text=True))
        self.assertIn("outside-catalog", new_admin.get("/admin/tests?details=demo").get_data(as_text=True))
        self.assertNotIn("Запустить синтетические тесты", comparison)
        self.assertEqual(new_admin.post("/admin/tests", data={"evaluation_suite": "real"}).status_code, 405)
        self.assertEqual(new_admin.post("/admin", data={"evaluation_suite": "real"}).status_code, 405)
        self.assertIn("CTR по дате показа", page)

        real_cases = json.loads((EVALUATION_DIR / "profiles.real.json").read_text(encoding="utf-8"))
        self.assertEqual(len(real_cases), 350)
        self.assertEqual(len({case["id"] for case in real_cases}), 350)
        self.assertTrue(all(case["relevant_ids"] for case in real_cases))
        material = Material("test-card", "Курс из реального каталога", "https://example.com/real", format="курс")
        self.app.config["CATALOG_FORMATS"] = {material.id: material.format}
        self.app.config["CATALOG_TITLES"] = {material.id: material.title}
        self.assertIn(material.title, new_admin.get("/admin/analytics?category=курс").get_data(as_text=True))
        self.assertNotIn(material.title, new_admin.get("/admin/analytics?category=статья").get_data(as_text=True))

        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                """INSERT INTO interaction_events (user_id, material_id, kind, occurred_at)
                   SELECT id, 'test-card', 'view', 100 FROM users WHERE username IN ('owner', 'nextadmin')"""
            )
        response = self.post_form("/admin/history/clear", {"confirm": "1"}, page="/admin/settings")
        self.assertEqual(response.location, "/admin/settings#admin-history")
        with closing(sqlite3.connect(self.database_path)) as connection:
            remaining = connection.execute(
                "SELECT username FROM interaction_events JOIN users ON users.id = user_id"
            ).fetchall()
        self.assertEqual(remaining, [("nextadmin",)])
        self.assertEqual(read_report(self.database_path)[0], totals)

        weak = runner.invoke(args=["admin", "reset-password", "owner"], input="12345\n12345\n")
        self.assertEqual(weak.exit_code, 1)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        replacement = "A replacement test passphrase!"
        reset = runner.invoke(args=["admin", "reset-password", "owner"], input=f"{replacement}\n{replacement}\n")
        self.assertEqual(reset.exit_code, 0)
        self.assertEqual(self.client.get("/admin").location, "/admin/login")
        self.assertEqual(self.post_form("/admin/login", credentials).status_code, 401)
        self.assertEqual(self.post_form("/admin/login", {
            **credentials, "password": replacement,
        }).location, "/admin")

    def test_activity_and_card_metrics_exclude_demo_and_legacy(self):
        shown_at = int(datetime(2026, 11, 9, 23, tzinfo=UTC).timestamp())
        clicked_at = int(datetime(2026, 11, 10, 1, tzinfo=UTC).timestamp())
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.executemany(
                """INSERT INTO recommendation_impressions
                   (batch_id, material_id, position, algorithm, result_order, audience, shown_at, clicked_at)
                   VALUES (?, 'test-card', 1, ?, 'recommended', ?, ?, ?)""",
                [("guest-row", "algorithm", "guest", shown_at, clicked_at),
                 ("demo-row", "algorithm", "demo", shown_at, clicked_at),
                 ("legacy-row", "app.ui.stub.recommend", "guest", shown_at, clicked_at)],
            )
        activity = read_daily_activity(self.database_path, datetime(2026, 11, 10, tzinfo=UTC).date())
        self.assertEqual(len(activity), 30)
        self.assertEqual(activity[-2:], [("2026-11-09", DailyActivity(1, 0, 1)), ("2026-11-10", DailyActivity(0, 1, 0))])
        self.assertEqual(activity[-2][1].ctr, 100.0)
        self.assertIsNone(activity[-1][1].ctr)

        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.executemany(
                """INSERT INTO recommendation_impressions
                   (batch_id, material_id, position, algorithm, result_order, audience, shown_at, clicked_at)
                   VALUES (?, ?, 1, ?, 'recommended', 'guest', ?, ?)""",
                [("synthetic-row", "test-card", "app.recommender.engine.recommend:demo-catalog", shown_at, clicked_at),
                 ("not-in-catalog", "synthetic-card", "algorithm", shown_at, clicked_at)],
            )
        self.assertEqual(read_top_cards(self.database_path, {"test-card"}), [("test-card", Metrics(1, 1))])
        self.assertEqual(read_top_cards(self.database_path, {"different"}), [])

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
        self.assertEqual(limit, 7)

    def test_skip_profile_keeps_the_goal_and_clears_history(self):
        response = self.client.post("/", data={
            "goal": "  Найти клиентов  ",
            "stage": "запуск",
            "history": "sample",
            "action": "skip_profile",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[-1], (Profile(goal="Найти клиентов"), (), 7))

    def test_result_order_keeps_the_full_selection_and_sorts_only_measured_cards(self):
        client = create_app(stub_recommend, database_path=self.database_path).test_client()

        def card_ids(html):
            return re.findall(r'class="material-link" href="/materials/([^"/?]+)\?', html)

        initial = client.get("/").get_data(as_text=True)
        default = card_ids(initial)
        self.assertEqual(len(default), 6)
        self.assertIn("Показано 6 из 7", initial)
        self.assertIn("Далее →", initial)
        self.assertNotIn("← Назад", initial)
        self.assertNotIn('pagination-prev', initial)
        expanded = client.post("/", data={"show_page": 2}).get_data(as_text=True)
        self.assertEqual(card_ids(expanded), ["operations-week-plan"])
        self.assertNotIn('pagination-next', expanded)
        self.assertNotIn('Далее →', expanded)
        self.assertIn('pagination-prev', expanded)
        self.assertIn('← Назад', expanded)

        popularity = client.post("/", data={"result_order": "popularity"}).get_data(as_text=True)
        self.assertEqual(card_ids(popularity), [
            "marketing-first-sales", "finance-cashflow", "marketing-service-leads",
            "marketing-repeat-sales", "start-checklist", "finance-profitability",
        ])
        self.assertRegex(popularity, r'value="popularity"\s+aria-pressed="true"')
        self.assertNotIn("Начните с этого", popularity)

        rating = client.post("/", data={"result_order": "rating"}).get_data(as_text=True)
        self.assertEqual(card_ids(rating), [
            "marketing-repeat-sales", "finance-cashflow", "start-checklist",
            "marketing-service-leads", "marketing-first-sales", "finance-profitability",
        ])
        self.assertIn("Оценка · 4,9 из 5", rating)
        self.assertEqual(card_ids(client.post("/", data={
            "result_order": "invalid",
        }).get_data(as_text=True)), default)

        selection = client.post("/", data={
            "goal": "Учёт", "stage": "рост", "history": "sample",
        }).get_data(as_text=True)
        orders = re.search(r'<form class="result-orders".*?</form>', selection, re.DOTALL).group()
        self.assertIn('name="goal" value="Учёт"', orders)
        self.assertIn('name="stage" value="рост"', orders)
        self.assertIn('name="history" value="sample"', orders)

    def test_real_catalog_export_drives_web_rank_filters_and_previews(self):
        catalog_dir = self.database_path.parent / "catalog"
        catalog_dir.mkdir()
        catalog_path = catalog_dir / "catalog_slim.sqlite"
        with closing(sqlite3.connect(catalog_path)) as connection, connection:
            connection.execute("""CREATE TABLE materials (
                id TEXT PRIMARY KEY, title TEXT, url TEXT, description TEXT, format TEXT, access TEXT,
                cost INTEGER, popularity_score REAL, avg_rating REAL, topics_json TEXT,
                stages_json TEXT, industries_json TEXT, experience_levels_json TEXT
            )""")
            connection.executemany(
                "INSERT INTO materials VALUES (?, ?, ?, 'Описание материала', 'курс', ?, ?, ?, ?, ?, '[]', '[]', '[]')",
                [
                    ("paid-low", "Налоги для ИП", "https://example.com/low", "paid", 800, 42, 4.7, '["налоги"]'),
                    ("paid-high", "Налоги для ИП: практика", "https://example.com/high", "paid", 4000, 100, 4.9, '["налоги"]'),
                    ("free-tax", "Памятка по налогам для ИП", "https://example.com/free", "free", None, 15, 4.6, '["налоги"]'),
                ],
            )
        with patch.dict("os.environ", {"ALFA_CATALOG_DIR": str(catalog_dir)}):
            (catalog_dir / "subscriptions.json").write_text('{"paid-low":["Курс+"]}', encoding="utf-8")
            app = create_app(database_path=self.database_path)
            client = app.test_client()
            self.assertEqual(client.get("/health").json, {"status": "ok", "catalog": "real", "materials": 3})
            response = client.post("/", data={"goal": "налоги для ИП до 1000 ₽", "result_order": "rating"})
            page = response.get_data(as_text=True)
            self.assertEqual(re.findall(r'href="/materials/([^"/?]+)\?', page), ["paid-low", "free-tax"])
            self.assertIn("Состав первых 2 карточек · бесплатные 1 · платные 0 · Курс+ 1", page)
            for category, mid in (("free", "free-tax"), ("paid", "paid-high"), ("subscription", "paid-low")):
                self.assertIn(f'name="access_filter" value="{category}" checked>', page)
                filtered = client.post("/", data={"access_filters_set": "1", "access_filter": category}).get_data(as_text=True)
                self.assertEqual(re.findall(r'href="/materials/([^"/?]+)\?', filtered), [mid])
            empty = client.post("/", data={"access_filters_set": "1"}).get_data(as_text=True)
            self.assertIn("Выберите хотя бы один тип материалов.", empty)
            preview = client.get("/materials/paid-low", headers={"Accept": "application/json"}).json
            self.assertTrue(preview["access_required"])
            self.assertEqual(preview["url"], "https://example.com/low")
            transition = client.post(preview["open_url"], data={"csrf_token": preview["csrf_token"]})
            self.assertEqual(transition.location, "https://example.com/low")
            next_page = client.get("/materials/free-tax").get_data(as_text=True)
            self.assertIn("Углубление по теме «налоги»", next_page)
            self.assertIn('href="/materials/paid-high"', next_page)

            runner = app.test_cli_runner()
            self.assertEqual(runner.invoke(args=["admin", "course-plus", "demo"]).exit_code, 0)
            self.assertEqual(self.post_form("/login/demo", {}, client=client, page="/login").status_code, 303)
            page = client.post("/", data={"goal": "налоги для ИП до 1000 ₽", "result_order": "rating"}).get_data(as_text=True)
            self.assertEqual(re.findall(r'href="/materials/([^"/?]+)\?', page), ["paid-low", "free-tax"])
            self.assertIn("По подписке Курс+", page)
            self.assertIn("Оценка · 4,7 из 5", page)
            self.assertIn("Популярность · 42", page)
            self.assertNotIn('name="history"', page)
            self.assertNotIn("приведены для примера", page)
            only_subscription = client.post("/", data={
                "access_filters_set": "1", "access_filter": "subscription", "goal": "налоги",
            }).get_data(as_text=True)
            self.assertEqual(re.findall(r'href="/materials/([^"/?]+)\?', only_subscription), ["paid-low"])
            preview = client.get("/materials/paid-low", headers={"Accept": "application/json"}).json
            self.assertEqual(preview["url"], "https://example.com/low")
            self.assertFalse(preview["access_required"])
            self.assertEqual(runner.invoke(args=["admin", "course-plus", "demo", "--revoke"]).exit_code, 0)
            self.assertTrue(client.get("/materials/paid-low", headers={"Accept": "application/json"}).json["access_required"])

    def test_login_required_article_is_in_the_free_filter_and_retains_its_link(self):
        catalog_dir = self.database_path.parent / "login-catalog"
        catalog_dir.mkdir()
        url = "https://kurs.alfabank.ru/articles/example"
        (catalog_dir / "catalog.norm.json").write_text(json.dumps([{
            "id": "login-article", "title": "Статья с входом", "url": url, "format": "статья",
            "entity_type": "article", "access": "free", "requires_login": True,
        }]), encoding="utf-8")
        with patch.dict("os.environ", {"ALFA_CATALOG_DIR": str(catalog_dir)}):
            client = create_app(database_path=self.database_path).test_client()
        page = client.post("/", data={"access_filters_set": "1", "access_filter": "free"}).get_data(as_text=True)
        self.assertIn("Нужен вход на Альфа-Курсе", page)
        preview = client.get("/materials/login-article", headers={"Accept": "application/json"}).json
        self.assertTrue(preview["access_required"])
        self.assertEqual(preview["access_label"], "Нужен вход на Альфа-Курсе")
        self.assertEqual(client.post(preview["open_url"], data={"csrf_token": preview["csrf_token"]}).location, url)

    def test_show_more_appends_six_sorted_cards_until_the_selection_ends(self):
        catalog = tuple(
            Material(f"item-{index}", f"Материал {index}", f"https://example.com/{index}", popularity=index)
            for index in range(1, 15)
        )
        with patch("app.ui.web.load_catalog", return_value=catalog):
            client = create_app(database_path=self.database_path).test_client()

        first_page = client.post("/", data={"result_order": "popularity"}).get_data(as_text=True)
        self.assertIn('name="current_order" value="popularity"', first_page)
        for page, expected_ids in (
            (1, list(range(14, 8, -1))),
            (2, list(range(8, 2, -1))),
            (3, [2, 1]),
        ):
            html = first_page if page == 1 else client.post("/", data={
                "current_order": "popularity", "show_page": page,
            }).get_data(as_text=True)
            ids = re.findall(r'class="material-link" href="/materials/item-(\d+)\?', html)
            self.assertEqual([int(item) for item in ids], expected_ids)
            has_next = 'Далее →' in html
            self.assertEqual(has_next, page < 3)
            has_prev = '← Назад' in html
            self.assertEqual(has_prev, page > 1)

    def test_late_page_navigation_large_payload_succeeds_without_entity_too_large(self):
        total_items = 1146
        catalog = tuple(
            Material(f"catalog-item-slug-number-{index:04d}", f"Материал {index}", f"https://example.com/{index}", popularity=index)
            for index in range(1, total_items + 1)
        )
        with patch("app.ui.web.load_catalog", return_value=catalog):
            client = create_app(database_path=self.database_path).test_client()

        first = client.get("/").get_data(as_text=True)
        scope = re.search(r'data-page-scope="([^"]+)"', first)[1]
        visited = [f"{page}:" + ",".join(f"catalog-item-slug-number-{i:04d}"
                                        for i in range(page * 6 - 5, page * 6 + 1)) for page in range(1, 191)]
        payload_bytes = sum(len(entry.encode("utf-8")) for entry in visited)
        self.assertGreater(payload_bytes, 16 * 1024)

        # Navigating to the last page (191) with payload exceeding 8KB must succeed (HTTP 200, not 413)
        response = client.post("/", data={
            "show_page": 191,
            "visited_page": visited,
            "page_scope": scope,
            "page_size": 6,
        })
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Страница 191 из 191", html)
        self.assertNotIn("Далее →", html)
        self.assertNotIn('pagination-next', html)
        self.assertIn("← Назад", html)
        self.assertIn('pagination-prev', html)

        # Navigating to an intermediate late page (190) also succeeds with large payload
        back_response = client.post("/", data={
            "show_page": 190,
            "visited_page": visited,
            "page_scope": scope,
            "page_size": 6,
        })
        self.assertEqual(back_response.status_code, 200)
        back_html = back_response.get_data(as_text=True)
        self.assertIn("Страница 190 из 191", back_html)
        self.assertIn("Далее →", back_html)
        self.assertIn("← Назад", back_html)

    def test_catalog_keeps_only_visited_pages_for_each_filter_scope(self):
        catalog = tuple(Material(f"item-{i}", f"Маркетинг {i}", f"https://example.com/{i}",
                                 topics=("маркетинг",), format="статья") for i in range(36))
        with patch("app.ui.web.load_catalog", return_value=catalog):
            client = create_app(database_path=self.database_path).test_client()
        form = {"goal": "Маркетинг", "page_size": 6}

        def request_page(page, **state):
            html = client.post("/", data={**form, "show_page": page, **state},
                               headers={"X-Recommendation-Fragment": "1"}).get_data(as_text=True)
            return re.search(r'data-page-scope="([^"]*)"', html)[1], re.findall(r'data-material-id="([^"]+)"', html)

        scope, first = request_page(1)
        scope_after_jump, fifth = request_page(5, page_scope=scope, visited_page=["1:" + ",".join(first)])
        self.assertEqual(scope, scope_after_jump)
        visited = ["1:" + ",".join(first), "5:" + ",".join(fifth)]
        _, second = request_page(2, page_scope=scope, visited_page=visited)
        self.assertTrue(set(second).isdisjoint(first + fifth))
        self.assertEqual(request_page(5, page_scope=scope, visited_page=visited)[1], fifth)
        self.assertEqual(request_page(1, page_scope=scope, visited_page=visited)[1], first)
        other, _ = request_page(1, page_scope=scope, visited_page=visited, selected_page_size=12)
        self.assertNotEqual(other, scope)

    def test_impressions_preserve_positions_deduplicate_pages_and_attribute_clicks(self):
        app = create_app(stub_recommend, database_path=self.database_path, algorithm_name="demo:v1")
        client = app.test_client()
        endpoint = "/recommendations/impressions"

        def cards(html):
            return re.findall(r'class="material-link" href="([^"]+\?impression=[^"]+)"', html)

        def token(url):
            return parse_qs(urlsplit(url).query)["impression"][0]

        def show(html, *urls, browser=client):
            csrf = re.search(r'data-impression-csrf="([^"]+)"', html).group(1)
            return browser.post(endpoint, data={"csrf_token": csrf, "impression": [token(url) for url in urls]})

        initial = client.get("/").get_data(as_text=True)
        first, second, *_ = cards(initial)
        batch = re.search(r'name="impression_batch" value="([^"]+)"', initial).group(1)
        self.assertEqual(client.post(endpoint, data={"impression": token(first)}).status_code, 400)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_impressions").fetchone()[0], 0)
        self.assertEqual(show(initial, first, second, first).status_code, 204)
        expanded = client.post("/", data={"show_page": 2, "impression_batch": batch}).get_data(as_text=True)
        self.assertEqual(show(expanded, cards(expanded)[0], cards(expanded)[-1]).status_code, 204)

        visitor = app.test_client()
        other = visitor.get("/").get_data(as_text=True)
        self.assertEqual(show(other, cards(initial)[2], browser=visitor).status_code, 204)
        json_headers = {"Accept": "application/json"}
        preview = client.get(first, headers=json_headers).get_json()
        for _ in range(2):
            self.assertEqual(client.post(preview["open_url"], data={
                "csrf_token": preview["csrf_token"],
            }, headers=json_headers).status_code, 204)
        mismatched = preview["open_url"].replace("start-checklist", "marketing-first-sales")
        self.assertEqual(client.post(mismatched, data={
            "csrf_token": preview["csrf_token"],
        }, headers=json_headers).status_code, 204)

        sorted_html = client.post("/", data={"result_order": "popularity"}).get_data(as_text=True)
        sorted_first = cards(sorted_html)[0]
        self.assertEqual(client.get(sorted_first).status_code, 200)
        clicked = self.post_form(
            sorted_first.replace("?", "/open?"), {}, page=sorted_first, client=client,
        )
        self.assertEqual(clicked.status_code, 303)

        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute("""SELECT material_id, position, algorithm, result_order, audience, user_id,
                                                clicked_at IS NOT NULL
                                         FROM recommendation_impressions ORDER BY result_order DESC, position""").fetchall()
            batches = connection.execute("SELECT COUNT(DISTINCT batch_id) FROM recommendation_impressions").fetchone()[0]
        self.assertEqual(rows, [
            ("start-checklist", 1, "demo:v1", "recommended", "guest", None, 1),
            ("marketing-first-sales", 2, "demo:v1", "recommended", "guest", None, 0),
            ("operations-week-plan", 7, "demo:v1", "recommended", "guest", None, 0),
            ("marketing-first-sales", 1, "demo:v1", "popularity", "guest", None, 1),
        ])
        self.assertEqual(batches, 2)

    def test_material_history_is_persistent_and_isolated_by_account(self):
        self.assertEqual(self.register().status_code, 303)
        material = "/materials/start-checklist"
        self.client.get("/")
        self.client.head(material)
        self.client.get(material, headers={"Sec-Purpose": "prefetch"})
        self.assertEqual(self.client.post(f"{material}/open").status_code, 400)
        self.assertEqual(self.client.get("/materials/missing").status_code, 404)
        self.client.get("/")
        self.assertEqual(self.calls[-1][1], ())

        json_headers = {"Accept": "application/json"}
        preview = self.client.get(material, headers=json_headers).get_json()
        self.assertIn("Чек-лист запуска", preview["title"])
        self.assertTrue(preview["description"])
        self.assertEqual(preview["url"], "https://example.com/alfakurs-demo/start-checklist")
        self.assertIn("next_materials", preview)
        self.assertTrue(len(preview["next_materials"]) > 0)
        result = self.client.post(preview["open_url"], headers=json_headers, data={"csrf_token": preview["csrf_token"]})
        self.assertEqual(result.status_code, 204)
        html_page = self.client.get(material).get_data(as_text=True)
        self.assertIn("Траектория обучения", html_page)
        self.assertIn("Рекомендуем изучить следующим шагом", html_page)
        with closing(sqlite3.connect(self.database_path)) as connection:
            events = connection.execute("SELECT kind FROM interaction_events ORDER BY id").fetchall()
        self.assertEqual(events, [("view",), ("click",), ("view",)])

        other = create_app(self.recommend, database_path=self.database_path).test_client()
        self.assertEqual(self.post_form("/register", {
            **self.business, "username": "other", "password": self.password,
        }, client=other).status_code, 303)
        other.get("/")
        self.assertEqual(self.calls[-1][1], ())
        self.assertEqual(self.post_form("/login", {
            "username": "owner", "password": self.password,
        }, client=other).status_code, 303)
        response = other.post("/", data={"history": "sample"})
        history = self.calls[-1][1]
        self.assertEqual({(event.material_id, event.kind) for event in history}, {
            ("start-checklist", "view"), ("start-checklist", "click"),
        })
        self.assertTrue(all(event.occurred_at.tzinfo for event in history))
        self.assertNotIn('name="history"', response.get_data(as_text=True))
        other.post("/", data={"action": "skip_profile"})
        self.assertEqual(self.calls[-1][1], ())
        other.get("/")
        self.assertEqual(self.calls[-1][1], history)

        demo = self.app.test_client()
        self.post_form("/login/demo", {}, page="/login", client=demo)
        self.assertEqual(self.post_form(f"{material}/open", {}, page=material, client=demo).status_code, 303)
        demo.get("/")
        self.assertEqual(self.calls[-1][1], ())
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM interaction_events").fetchone()[0], 3)

    def test_account_profile_survives_restart_and_is_isolated(self):
        self.assertEqual(self.register().status_code, 303)
        updated = {**self.business, "stage": "запуск", "industry": "логистика"}
        self.assertEqual(self.post_form("/profile", updated).status_code, 303)
        restarted = create_app(self.recommend, database_path=self.database_path)
        fresh = restarted.test_client()
        fresh.get("/")
        self.assertEqual(self.calls[-1][0], Profile())

        signed_in = self.post_form("/login", {
            "username": "OWNER", "password": self.password,
        }, client=fresh)
        self.assertEqual(signed_in.status_code, 303)
        fresh.post("/", data={"goal": "Продвижение"})
        self.assertEqual(self.calls[-1][0], Profile(goal="Продвижение", **updated))
        with closing(sqlite3.connect(self.database_path)) as connection:
            stored_hash = connection.execute(
                "SELECT password_hash FROM users WHERE username = ?", ("owner",)
            ).fetchone()[0]
        self.assertTrue(stored_hash.startswith("scrypt:"))
        self.assertNotIn(self.password, stored_hash)

    def test_one_off_selection_and_skip_keep_the_saved_profile(self):
        self.assertEqual(self.register().status_code, 303)

        self.client.post("/", data={"stage": "запуск", "industry": ""})
        temporary = {**self.business, "stage": "запуск", "industry": None}
        self.assertEqual(self.calls[-1][0], Profile(**temporary))
        self.client.post("/", data={"action": "skip_profile"})
        self.assertEqual(self.calls[-1][0], Profile())
        self.client.get("/")
        self.assertEqual(self.calls[-1][0], Profile(**self.business))

        self.assertEqual(self.post_form("/profile", {}).status_code, 303)
        self.client.get("/")
        self.assertEqual(self.calls[-1][0], Profile())

    def test_profile_settings_add_private_photo_to_an_existing_account(self):
        self.assertEqual(self.register().status_code, 303)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("DROP TABLE account_details")
        self.client = create_app(self.recommend, database_path=self.database_path).test_client()
        self.assertEqual(self.post_form("/login", {
            "username": "owner", "password": self.password,
        }).status_code, 303)
        overview = self.client.get("/profile").get_data(as_text=True)
        self.assertIn("@owner", overview)
        self.assertNotIn('name="first_name"', overview)

        image = BytesIO()
        exif = Image.Exif()
        exif[315] = "Private metadata"
        Image.new("RGB", (800, 400), "red").save(image, "PNG", exif=exif)
        image.seek(0)
        details = {"first_name": " Анна ", "last_name": "Иванова", "company": "Альфа & партнёры"}
        updated = {**self.business, "industry": "услуги"}
        settings = "/profile/settings"
        response = self.post_form(settings, {**updated, **details, "avatar": (image, "avatar.png")})
        self.assertEqual(response.location, "/profile?saved=1")
        overview = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("Анна Иванова", overview)
        self.assertIn("Альфа &amp; партнёры", overview)
        self.assertIn('src="/profile/avatar"', overview)
        self.client.get("/")
        self.assertEqual(self.calls[-1][0], Profile(**updated))

        avatar = self.client.get("/profile/avatar")
        self.assertEqual(avatar.mimetype, "image/jpeg")
        self.assertEqual(avatar.headers["Cache-Control"], "no-store")
        with Image.open(BytesIO(avatar.data)) as saved_photo:
            self.assertEqual(saved_photo.size, (384, 192))
            self.assertFalse(saved_photo.getexif())
        visitor = self.app.test_client()
        self.assertEqual(visitor.get("/profile/avatar").status_code, 404)
        self.assertEqual(self.post_form("/login/demo", {}, client=visitor, page="/login").status_code, 303)
        self.assertEqual(visitor.get("/profile/avatar").status_code, 404)
        self.assertEqual(self.post_form(settings, details, client=visitor, page="/profile").status_code, 403)

        self.assertEqual(self.post_form(settings, {**updated, **details}).status_code, 303)
        self.assertEqual(self.client.get("/profile/avatar").data, avatar.data)
        invalid = self.post_form(settings, {
            **updated, "company": "Другая компания", "avatar": (BytesIO(b"<svg/>"), "fake.png"),
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("Альфа &amp; партнёры", self.client.get("/profile").get_data(as_text=True))
        self.assertEqual(self.client.get("/profile/avatar").data, avatar.data)
        self.assertEqual(self.post_form(settings, {**updated, **details, "remove_avatar": "1"}).status_code, 303)
        self.assertEqual(self.client.get("/profile/avatar").status_code, 404)

    def test_shared_demo_can_be_used_but_not_edited(self):
        second_browser = self.app.test_client()
        for client in (self.client, second_browser):
            response = self.post_form("/login/demo", {}, client=client, page="/login")
            self.assertEqual(response.status_code, 303)
            client.get("/")
            self.assertEqual(self.calls[-1][0], Profile(
                stage="рост", industry="розница", experience="опытный", team_size="2–5 человек",
                learning_topic="продажи", preferred_format="курс",
            ))
        self.assertEqual(self.post_form("/profile", self.business).status_code, 403)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT username FROM users").fetchall(), [("demo",)])
        self.assertEqual(self.post_form("/logout", {}, page="/profile").status_code, 303)
        self.assertEqual(self.client.get("/profile").location, "/login")

    def test_contact_fields_are_added_to_existing_profiles_and_stay_private(self):
        public = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="/login">Войти</a>', public)
        self.assertEqual(self.register().status_code, 303)
        settings = "/profile/settings"
        data = {**self.business, "first_name": "Анна"}
        self.assertEqual(self.post_form(settings, data).status_code, 303)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("ALTER TABLE account_details DROP COLUMN email")
            connection.execute("ALTER TABLE account_details DROP COLUMN phone")

        app = create_app(self.recommend, database_path=self.database_path)
        self.client = app.test_client()
        self.assertEqual(self.post_form("/login", {
            "username": "owner", "password": self.password,
        }).status_code, 303)
        existing = self.client.get(settings).get_data(as_text=True)
        self.assertIn('name="first_name" value="Анна"', existing)
        self.assertIn('type="email" id="email"', existing)
        self.assertIn('type="tel" id="phone"', existing)

        contacts = {"email": "demo-owner@example.com", "phone": "+7 (900) 000-00-00"}
        self.assertEqual(self.post_form(settings, {**data, **contacts}).status_code, 303)
        invalid = self.post_form(settings, {**data, "email": "invalid", "phone": "1"})
        self.assertEqual(invalid.status_code, 400)
        overview = self.client.get("/profile").get_data(as_text=True)
        self.assertIn('href="/profile" aria-current="page"', overview)
        for value in contacts.values():
            self.assertIn(value, overview)
            self.assertNotIn(value, self.client.get("/").get_data(as_text=True))

        visitor = app.test_client()
        self.assertEqual(self.post_form("/login/demo", {}, client=visitor, page="/login").status_code, 303)
        other_profile = visitor.get("/profile").get_data(as_text=True)
        for value in contacts.values():
            self.assertNotIn(value, other_profile)

    def test_registration_requires_profile_and_csrf_and_keeps_legacy_values(self):
        self.client.set_cookie("alfahack_profile", json.dumps({"industry": "услуги"}))
        page = self.client.get("/register").get_data(as_text=True)
        self.assertIn('value="услуги" selected', page)
        self.assertEqual(self.client.post("/register", data=self.business).status_code, 400)
        self.assertEqual(self.register(business={"industry": "услуги"}).status_code, 400)
        self.assertEqual(self.register(username="demo").status_code, 409)

    def test_password_form_requires_https_for_a_public_host(self):
        public = "http://demo.example"
        page = self.client.get("/login", base_url=public).get_data(as_text=True)
        self.assertIn("Войти в тестовый аккаунт", page)
        self.assertNotIn('name="password"', page)
        response = self.client.post("/login", base_url=public, data={"password": self.password})
        self.assertEqual(response.status_code, 403)

    def test_sql_like_login_input_cannot_bypass_authentication(self):
        self.assertEqual(self.register().status_code, 303)
        visitor = self.app.test_client()
        for data in (
            {"username": "owner' OR '1'='1", "password": self.password},
            {"username": "owner", "password": "' OR 1=1 --"},
        ):
            with self.subTest(data=data):
                self.assertEqual(self.post_form("/login", data, client=visitor).status_code, 401)
                self.assertEqual(visitor.get("/profile").location, "/login")
        self.assertEqual(self.post_form("/login", {
            "username": "owner", "password": self.password,
        }, client=visitor).status_code, 303)

    def test_logged_out_cookie_cannot_be_replayed(self):
        self.assertEqual(self.register().status_code, 303)
        self.client.get("/profile")
        cookie_name = self.app.config["SESSION_COOKIE_NAME"]
        previous_cookie = self.client.get_cookie(cookie_name).value
        self.assertEqual(self.post_form("/logout", {}, page="/profile").status_code, 303)

        replay = self.app.test_client()
        replay.set_cookie(cookie_name, previous_cookie)
        self.assertEqual(replay.get("/profile").location, "/login")
        replay.get("/")
        self.assertEqual(self.calls[-1][0], Profile())

    def test_query_is_escaped_and_scripts_are_blocked(self):
        response = self.client.post("/", data={"goal": "<script>alert(1)</script>"})
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        page = response.get_data(as_text=True)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_material_description_formats_rich_html_and_plain_text(self):
        from app.ui.web import format_description_html

        raw_html = '<p><span style="background-color: transparent;">Форма утверждена.</span></p><p><br></p>'
        formatted = format_description_html(raw_html)
        self.assertEqual(formatted, "<p>Форма утверждена.</p>")
        self.assertNotIn("style=", formatted)
        self.assertNotIn("span", formatted)

        raw_plain = "Первый абзац.\n\nВторой абзац."
        formatted_plain = format_description_html(raw_plain)
        self.assertEqual(formatted_plain, "<p>Первый абзац.</p><p>Второй абзац.</p>")

        preview = self.client.get("/materials/start-checklist", headers={"Accept": "application/json"}).get_json()
        self.assertIn("description_html", preview)
        self.assertIn("<p>", preview["description_html"])
        attack = '<<x>img src="/materials/start-checklist" onerror="alert(1)">'
        self.assertNotIn("<img", format_description_html(attack))
        self.assertEqual(format_description_html("3 > 2\n\nЦена < 100"), "<p>3 &gt; 2</p><p>Цена &lt; 100</p>")
        self.assertNotIn("javascript:", format_description_html('<a href="javascript:alert(1)">Ссылка</a>'))

    def test_format_filter_narrows_displayed_cards(self):
        response = self.client.post("/", data={"goal": "маркетинг", "format_filter": "статья"})
        page = response.get_data(as_text=True)
        self.assertIn('class="format-chip active"', page)
        self.assertIn('name="format_filter" value="статья"', page)

    def test_fragment_keeps_filters_and_page_size_without_the_page_shell(self):
        catalog = tuple(
            Material(str(i), f"Материал {i}", f"https://example.com/calculators/{i}", format="шаблон")
            for i in range(1, 15)
        )
        with patch("app.ui.web.load_catalog", return_value=catalog):
            client = create_app(stub_recommend, database_path=self.database_path).test_client()
        fragment = client.post("/", data={
            "format_filter": "all", "selected_format": "калькулятор", "selected_page_size": "12",
        }, headers={"X-Recommendation-Fragment": "1"}).get_data(as_text=True)
        self.assertNotIn("<!doctype html>", fragment)
        self.assertIn("Показано 12 из 14", fragment)
        self.assertIn('name="format_filter" value="калькулятор"', fragment)
        self.assertNotIn("Карточки показаны в порядке тестового каталога.", fragment)
        batch = re.search(r'name="impression_batch" value="([^"]+)"', fragment).group(1)
        batch_id = re.search(r'data-impression-batch="([^"]+)"', fragment).group(1)
        expanded = client.post("/", data={
            "format_filter": "калькулятор", "page_size": "12", "show_page": "2", "impression_batch": batch,
        }, headers={"X-Recommendation-Fragment": "1"}).get_data(as_text=True)
        self.assertIn("Показано 13–14 из 14", expanded)
        self.assertIn(f'data-impression-batch="{batch_id}"', expanded)
        empty = client.post("/", data={"selected_format": "подкаст"}).get_data(as_text=True)
        self.assertIn("нет материалов выбранного формата", empty)

    def test_account_can_change_its_own_password_to_eight_characters_with_email_code(self):
        self.assertEqual(self.register().status_code, 303)
        path = "/profile/password"
        send_code = "/profile/password/request-code"
        settings = "/profile/settings"
        other = self.app.test_client()
        credentials = {"username": "owner", "password": self.password}
        self.assertEqual(self.post_form("/login", credentials, client=other).status_code, 303)
        self.assertIn("Сначала укажите", self.client.get(settings).get_data(as_text=True))
        self.assertEqual(self.post_form(settings, {**self.business, "email": "owner@example.com",
                                                   "phone": "8 (900) 123-45-67"}).status_code, 303)
        self.assertIn("+7 (900) 123-45-67", self.client.get("/profile").get_data(as_text=True))
        self.assertIn('minlength="8"', self.client.get(settings).get_data(as_text=True))

        form = {"current_password": "неверный", "new_password": "demo8Key", "confirm_password": "demo8Key"}
        self.assertEqual(self.post_form(send_code, form, page=settings).location, "/profile/settings#password-settings")
        self.assertNotIn('name="email_code"', self.client.get(settings).get_data(as_text=True))
        self.assertEqual(other.get("/profile").status_code, 200)
        form["current_password"] = self.password
        self.assertEqual(self.post_form(send_code, form, page=settings).location, "/profile/settings#password-settings")
        self.assertIn("<strong>000 000</strong>", self.client.get(settings).get_data(as_text=True))
        self.assertEqual(self.post_form(path, {"email_code": "000000"}, client=other, page=settings).location,
                         "/profile/settings#password-settings")
        self.assertEqual(self.post_form(path, {"email_code": "999999"}, page=settings).location,
                         "/profile/settings#password-settings")
        self.assertEqual(self.post_form(path, {"email_code": "000 000"}, page=settings).location, "/login")
        self.assertEqual(other.get("/profile").location, "/login")
        self.assertEqual(self.post_form("/login", credentials).status_code, 401)
        self.assertEqual(self.post_form("/login", {**credentials, "password": form["new_password"]}).status_code, 303)

    def test_live_data_contract_requires_admin_and_validates_provider_output(self):
        endpoint = "/admin/api/v1/live-data"
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        self.assertEqual(self.register().status_code, 303)
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.app.test_cli_runner().invoke(args=["admin", "grant", "owner"]).exit_code, 0)
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        self.assertEqual(self.post_form("/admin/login", {"username": "owner", "password": self.password}).status_code, 303)
        self.assertEqual(self.client.get(endpoint + "?days=91").status_code, 400)
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["status"], "not_configured")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

        now = datetime.now(UTC)
        self.app.config["LIVE_DATA_PROVIDER"] = lambda start, end: LiveDataSnapshot(
            "test", now, ((start, DailyActivity(4, 2, 1)), (end, DailyActivity(0, 3, 0))),
        )
        result = self.client.get(endpoint + "?days=2").json
        self.assertEqual(result["period"]["timezone"], "UTC")
        self.assertEqual(result["daily"][0]["ctr_pct"], 25)
        self.assertIsNone(result["daily"][1]["ctr_pct"])
        self.assertEqual(self.client.get(endpoint + "?days=1").status_code, 502)
        self.assertIn("ClickHouse", self.client.get("/admin/roadmap").get_data(as_text=True))

    def test_course_plus_activation_requires_provider_confirmation(self):
        self.assertEqual(self.register().status_code, 303)
        path = "/profile/course-plus/activate"
        self.assertEqual(self.client.post(path).status_code, 400)
        self.assertIn("Активировать Курс+", self.client.get("/profile").get_data(as_text=True))
        self.assertEqual(self.post_form(path, {}, page="/profile").location, "/profile#course-plus")
        demo = self.client.get("/profile").get_data(as_text=True)
        self.assertIn("Сервис оформления подписки Курс+ пока не подключён", demo)
        self.assertNotIn("Подписка Курс+ активна", demo)

        self.app.config["COURSE_PLUS_ACTIVATOR"] = lambda account_id: False
        self.assertEqual(self.post_form(path, {}, page="/profile").status_code, 303)
        self.assertNotIn("Подписка Курс+ активна", self.client.get("/profile").get_data(as_text=True))
        self.app.config["COURSE_PLUS_ACTIVATOR"] = lambda account_id: True
        self.assertEqual(self.post_form(path, {}, page="/profile").status_code, 303)
        activated = self.client.get("/profile").get_data(as_text=True)
        self.assertIn("Подписка Курс+ активна", activated)
        self.assertNotIn(">Активировать Курс+<", activated)

    def test_next_courses_declines_lesson_counts_without_abbreviation(self):
        directory = self.database_path.parent / "next-courses-catalog"
        directory.mkdir()
        records = [
            {"id": "course-1", "title": "Курс 1", "url": "https://example.com/c1",
             "entity_type": "course", "format": "курс", "access": "free", "lesson_count": 1},
            {"id": "course-4", "title": "Курс 4", "url": "https://example.com/c4",
             "entity_type": "course", "format": "курс", "access": "free", "lesson_count": 4},
            {"id": "course-5", "title": "Курс 5", "url": "https://example.com/c5",
             "entity_type": "course", "format": "курс", "access": "free", "lesson_count": 5},
            {"id": "course-12", "title": "Курс 12", "url": "https://example.com/c12",
             "entity_type": "course", "format": "курс", "access": "free", "lesson_count": 12},
            {"id": "course-duration", "title": "Курс без счётчика", "url": "https://example.com/cd",
             "entity_type": "course", "format": "курс", "access": "free", "duration": "2 часа"},
        ]
        (directory / "catalog.norm.json").write_text(json.dumps(records), encoding="utf-8")
        with patch.dict("os.environ", {"ALFA_CATALOG_DIR": str(directory)}):
            client = create_app(database_path=self.database_path).test_client()

        home = client.get("/").get_data(as_text=True)
        section = home.split('id="next-courses"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Курс · 1 урок", section)
        self.assertIn("Курс · 4 урока", section)
        self.assertIn("Курс · 5 уроков", section)
        self.assertIn("Курс · 12 уроков", section)
        self.assertIn("Курс · 2 часа", section)
        self.assertNotIn("ур.", section)

    def test_hero_profile_expanded_by_default_and_collapsed_on_skip(self):
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('<details class="profile" open>', home)
        self.assertIn('id="jury-panel"', home)
        self.assertLess(home.index('id="recommendation-query"'), home.index('id="recommendations"'))
        self.assertEqual(len(re.findall(r'data-material-id="([^"]+)"', home)), 1)
        self.assertNotIn('data-material-id', home.split('id="jury-panel"', 1)[1])

        skipped = self.client.post("/", data={"goal": "Продажи", "action": "skip_profile"}).get_data(as_text=True)
        self.assertIn('<details class="profile">', skipped)
        self.assertNotIn('<details class="profile" open>', skipped)

    def test_jury_lab_requires_csrf_and_isolates_sessions_from_global_mode(self):
        real_app = create_app(database_path=self.database_path)
        judge = real_app.test_client()
        other = real_app.test_client()

        self.assertEqual(judge.post("/demo-lab", data={"lab_action": "set_mode", "mode": "popular"}).status_code, 400)

        judge.get("/")
        with judge.session_transaction() as browser_session:
            token = browser_session["csrf"]

        redirected = judge.post("/demo-lab", data={
            "csrf_token": token, "lab_action": "set_mode", "mode": "popular",
        })
        self.assertEqual(redirected.status_code, 303)

        popular_html = judge.get("/").get_data(as_text=True)
        self.assertIn('<span class="results-algo-badge">Бейзлайн · популярные курсы</span>', popular_html)
        self.assertIn("Сессия изменена", popular_html)

        other_html = other.get("/").get_data(as_text=True)
        self.assertIn('<span class="results-algo-badge">BM25 + recommend</span>', other_html)
        self.assertIn("Режим по умолчанию", other_html)

        switched = judge.post("/demo-lab", data={
            "csrf_token": token,
            "lab_action": "set_mode",
            "mode": "hybrid",
            "hero_sync": "1",
            "goal": '"><script>alert(1)</script> продажи',
            "stage": "рост",
        }, headers={"X-Jury-Panel": "1"})
        self.assertEqual(switched.status_code, 200)
        switched_html = switched.get_data(as_text=True)
        self.assertIn('<span class="results-algo-badge">BM25 + recommend</span>', switched_html)
        self.assertNotIn('"><script>alert(1)</script>', switched_html)
        self.assertIn("адаптивный", switched_html)

        fixed = judge.post("/demo-lab", data={
            "csrf_token": token,
            "lab_action": "toggle_bm25_adaptive",
            "bm25_adaptive": "off",
            "hero_sync": "1",
            "goal": "продажи",
        }, headers={"X-Jury-Panel": "1"}).get_data(as_text=True)
        self.assertIn("фиксированный", fixed)

    def test_jury_lab_presets_simulated_history_and_course_plus_access(self):
        directory = self.database_path.parent / "jury-lab-catalog"
        directory.mkdir()
        records = [
            {"id": "art-mkt", "title": "Статья: реклама и продвижение", "url": "https://example.com/am",
             "entity_type": "article", "format": "статья", "topics": ["маркетинг", "реклама"], "access": "free", "popularity": 400},
            {"id": "art-fin", "title": "Учёт денег в малом бизнесе", "url": "https://example.com/af",
             "entity_type": "article", "format": "статья", "topics": ["финансы"], "access": "free", "popularity": 300},
            {"id": "pod-mkt", "title": "Подкаст про рекламу и продвижение", "url": "https://example.com/pm",
             "entity_type": "podcast", "format": "подкаст", "topics": ["маркетинг", "реклама"], "access": "free", "popularity": 350},
            {"id": "web-mkt", "title": "Вебинар: каналы привлечения клиентов", "url": "https://example.com/wm",
             "entity_type": "article", "format": "вебинар", "topics": ["маркетинг", "реклама"], "access": "free", "popularity": 320},
            {"id": "crs-sales", "title": "Курс по повторным продажам", "url": "https://example.com/cs",
             "entity_type": "course", "format": "курс", "topics": ["продажи"], "access": "free", "lesson_count": 5, "popularity": 500},
            {"id": "sub-sales", "title": "Подписной курс: реклама и клиентский сервис", "url": "https://example.com/ss",
             "entity_type": "course", "format": "курс", "topics": ["продажи", "маркетинг", "реклама"],
             "access": "paid", "lesson_count": 6, "popularity": 700},
            {"id": "tpl-mkt", "title": "Шаблон рекламы и продвижения", "url": "https://example.com/tm",
             "entity_type": "template", "format": "шаблон", "topics": ["маркетинг", "реклама"], "access": "free", "popularity": 450},
            {"id": "chk-start", "title": "Чек-лист запуска", "url": "https://example.com/cs0",
             "entity_type": "checklist", "format": "чек-лист", "topics": ["запуск бизнеса"], "access": "free", "popularity": 600},
            {"id": "art-ops", "title": "Регламенты и планирование смены", "url": "https://example.com/ao",
             "entity_type": "article", "format": "статья", "topics": ["операционная работа"], "access": "free", "popularity": 250},
            {"id": "art-team", "title": "Как нанять первого сотрудника", "url": "https://example.com/at",
             "entity_type": "article", "format": "статья", "topics": ["управление командой"], "access": "free", "popularity": 260},
        ]
        (directory / "catalog.norm.json").write_text(json.dumps(records), encoding="utf-8")
        (directory / "subscriptions.json").write_text(json.dumps({"sub-sales": ["Курс+"]}), encoding="utf-8")
        (directory / "publication-dates.json").write_text(
            json.dumps({"art-mkt": "2026-01-08", "art-fin": "2026-01-05", "pod-mkt": "2026-01-07"}),
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"ALFA_CATALOG_DIR": str(directory)}):
            client = create_app(database_path=self.database_path).test_client()

        client.get("/")
        with client.session_transaction() as browser_session:
            token = browser_session["csrf"]

        preset_resp = client.post("/demo-lab", data={
            "csrf_token": token,
            "lab_action": "apply_preset",
            "preset_id": "growth_retail_plus",
        }, headers={"X-Jury-Panel": "1"})
        self.assertEqual(preset_resp.status_code, 200)
        preset_html = preset_resp.get_data(as_text=True)
        self.assertIn("Подписка Курс+: Активна", preset_html)
        self.assertIn('id="daily-picks"', preset_html)
        self.assertIn('id="next-courses"', preset_html)
        self.assertIn("Подписной курс: реклама и клиентский сервис", preset_html.split('id="next-courses"', 1)[1].split("</section>", 1)[0])
        self.assertNotIn('name="history"', preset_html)
        self.assertIn('<option value="рост" selected>', preset_html)

        sub_preview = client.get("/materials/sub-sales", headers={"Accept": "application/json"}).json
        self.assertFalse(sub_preview["access_required"])

        no_plus_html = client.post("/demo-lab", data={
            "csrf_token": token, "lab_action": "toggle_course_plus", "course_plus": "off", "hero_sync": "1",
        }, headers={"X-Jury-Panel": "1"}).get_data(as_text=True)
        self.assertNotIn('id="next-courses"', no_plus_html)
        self.assertTrue(client.get("/materials/sub-sales", headers={"Accept": "application/json"}).json["access_required"])

        ctx_html = client.post("/demo-lab", data={
            "csrf_token": token,
            "lab_action": "apply_preset",
            "preset_id": "history_context",
        }, headers={"X-Jury-Panel": "1"}).get_data(as_text=True)
        self.assertIn("<span>2 соб.</span>", ctx_html)
        self.assertIn("Продолжение 2 недавних переходов", ctx_html)

        added_html = client.post("/demo-lab", data={
            "csrf_token": token, "lab_action": "add_event", "sim_event": "click_finance", "hero_sync": "1", "goal": "",
        }, headers={"X-Jury-Panel": "1"}).get_data(as_text=True)
        self.assertIn("<span>3 соб.</span>", added_html)

        removed_html = client.post("/demo-lab", data={
            "csrf_token": token, "lab_action": "remove_event", "event_index": "0", "hero_sync": "1",
        }, headers={"X-Jury-Panel": "1"}).get_data(as_text=True)
        self.assertIn("<span>2 соб.</span>", removed_html)

        cleared_html = client.post("/demo-lab", data={
            "csrf_token": token, "lab_action": "clear_history", "hero_sync": "1",
        }, headers={"X-Jury-Panel": "1"}).get_data(as_text=True)
        self.assertIn("<span>0 соб.</span>", cleared_html)

        reset_html = client.post("/demo-lab", data={
            "csrf_token": token, "lab_action": "reset_all",
        }, headers={"X-Jury-Panel": "1"}).get_data(as_text=True)
        self.assertIn("Режим по умолчанию", reset_html)
        with client.session_transaction() as browser_session:
            self.assertNotIn("jury_lab", browser_session)
