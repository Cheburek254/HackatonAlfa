import json
import re
import sqlite3
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from app.contracts import Material, Profile, Recommendation, RecommendationResult
from app.ui.web import create_app


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
        self.assertIn('href="/materials/start-checklist"', page)

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
        client = create_app(database_path=self.database_path).test_client()

        def card_ids(html):
            return re.findall(r'class="material-link" href="/materials/([^"/]+)"', html)

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

    def test_show_more_appends_six_sorted_cards_until_the_selection_ends(self):
        catalog = tuple(
            Material(f"item-{index}", f"Материал {index}", "https://example.com", popularity=index)
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
            ids = re.findall(r'class="material-link" href="/materials/item-(\d+)"', html)
            self.assertEqual([int(item) for item in ids], list(range(14, 14 - count, -1)))
            self.assertEqual("Показать больше" in html, count < 14)

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
        result = self.client.post(preview["open_url"], headers=json_headers, data={"csrf_token": preview["csrf_token"]})
        self.assertEqual(result.status_code, 204)
        self.client.get(material)
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
