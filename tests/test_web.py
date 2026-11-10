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
from app.ui.stub import recommend as stub_recommend
from app.ui.web import create_app
from evaluation.runner import EVALUATION_DIR


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
        self.enterContext(patch.dict("os.environ", {"ALFA_CATALOG_DIR": "", "ALFA_LLM_API_KEY": ""}))
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
        comparison = self.post_form("/admin/tests", {"evaluation_suite": "demo"}, client=new_admin).get_data(as_text=True)
        self.assertIn("Проверено сценариев: 50", comparison)
        self.assertIn("Синтетический каталог · результаты", comparison)
        self.assertIn("Hit@3", comparison)
        self.assertIn("MRR@3", comparison)
        self.assertIn("outside-catalog", comparison)
        self.assertIn("CTR по дате показа", page)

        unavailable = self.post_form("/admin/tests", {"evaluation_suite": "real"}, client=new_admin).get_data(as_text=True)
        self.assertIn("подключите экспорт каталога", unavailable)
        self.assertNotIn("Проверено сценариев", unavailable)

        real_cases = json.loads((EVALUATION_DIR / "profiles.real.json").read_text(encoding="utf-8"))
        self.assertEqual(len(real_cases), 350)
        self.assertEqual(len({case["id"] for case in real_cases}), 350)
        self.assertTrue(all(case["relevant_ids"] for case in real_cases))
        referenced_ids = {mid for case in real_cases for mid in case["relevant_ids"]}
        referenced_ids.update(event["material_id"] for case in real_cases for event in case.get("history", ()))
        material = Material("test-card", "Курс из реального каталога", "https://example.com/real", format="курс")
        self.app.config["EVALUATION_REAL_CATALOG"] = (material,) + tuple(
            Material(mid, f"Материал {mid}", f"https://example.com/{mid}") for mid in sorted(referenced_ids)
        )
        self.app.config["CATALOG_FORMATS"] = {material.id: material.format}
        self.app.config["CATALOG_TITLES"] = {material.id: material.title}
        real_report = self.post_form("/admin/tests", {"evaluation_suite": "real"}, client=new_admin).get_data(as_text=True)
        self.assertIn("Реальный каталог · результаты", real_report)
        self.assertIn("350 размеченных сценариев", real_report)
        self.assertIn("Проверено сценариев: 350", real_report)
        self.assertNotIn("outside-catalog", real_report)
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
        self.assertIn("Показать больше", initial)
        expanded = client.post("/", data={"show_page": 2}).get_data(as_text=True)
        self.assertEqual(card_ids(expanded), [*default, "operations-week-plan"])
        self.assertNotIn("Показать больше", expanded)

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
            self.assertEqual(re.findall(r'href="/materials/([^"/?]+)\?', page), ["free-tax"])
            self.assertIn('value="subscription" disabled', page)
            self.assertIn('value="paid" disabled', page)
            self.assertEqual(client.get("/materials/paid-low").status_code, 404)
            self.assertEqual(client.post("/", data={"access_filters_set": "1", "access_filter": "subscription"}).status_code, 200)
            forged = client.post("/", data={"access_filters_set": "1", "access_filter": "subscription"}).get_data(as_text=True)
            self.assertNotIn('href="/materials/paid-low', forged)

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
            self.assertEqual(runner.invoke(args=["admin", "course-plus", "demo", "--revoke"]).exit_code, 0)
            self.assertEqual(client.get("/materials/paid-low").status_code, 404)

    def test_show_more_appends_six_sorted_cards_until_the_selection_ends(self):
        catalog = tuple(
            Material(f"item-{index}", f"Материал {index}", f"https://example.com/{index}", popularity=index)
            for index in range(1, 15)
        )
        with patch("app.ui.web.load_catalog", return_value=catalog):
            client = create_app(database_path=self.database_path).test_client()

        first_page = client.post("/", data={"result_order": "popularity"}).get_data(as_text=True)
        self.assertIn('formaction="/#result-card-7"', first_page)
        self.assertIn('name="current_order" value="popularity"', first_page)
        for page, count in ((1, 6), (2, 12), (3, 14)):
            html = first_page if page == 1 else client.post("/", data={
                "current_order": "popularity", "show_page": page,
            }).get_data(as_text=True)
            ids = re.findall(r'class="material-link" href="/materials/item-(\d+)\?', html)
            self.assertEqual([int(item) for item in ids], list(range(14, 14 - count, -1)))
            self.assertEqual("Показать больше" in html, count < 14)

    def test_impressions_preserve_positions_deduplicate_pages_and_attribute_clicks(self):
        app = create_app(stub_recommend, database_path=self.database_path, algorithm_name="demo:v1")
        client = app.test_client()
        endpoint = "/recommendations/impressions"

        def cards(html):
            return re.findall(r'class="material-link" href="([^"]+)"', html)

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
        self.assertIn("Показано 14 из 14", expanded)
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
        self.assertIn("<strong>000000</strong>", self.client.get(settings).get_data(as_text=True))
        self.assertEqual(self.post_form(path, {"email_code": "000000"}, client=other, page=settings).location,
                         "/profile/settings#password-settings")
        self.assertEqual(self.post_form(path, {"email_code": "999999"}, page=settings).location,
                         "/profile/settings#password-settings")
        self.assertEqual(self.post_form(path, {"email_code": "000000"}, page=settings).location, "/login")
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
