import json
import sqlite3
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from app.contracts import Profile, Recommendation, RecommendationResult
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
