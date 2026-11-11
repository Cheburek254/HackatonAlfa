"""Тесты для ИИ-помощника Альфа-Курс."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.contracts import InteractionEvent, Material
from app.recommender.hybrid import merge_orders
from app.ui.chat import _fallback_reply, _linkify_materials
from app.ui.web import create_app


class ChatApiTest(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"ALFA_CATALOG_DIR": ""}))
        self.enterContext(patch("app.catalog.DATA_DIR", Path(temporary)))
        self.credentials = self.enterContext(patch("app.ui.chat._load_env_credentials", return_value=("", "")))
        self.app = create_app(database_path=Path(temporary) / "accounts.sqlite3")
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_chat_widget_rendered_on_index_and_clean_ui(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="alfa-chat-widget"', html)
        self.assertIn('id="alfa-chat-trigger"', html)
        self.assertIn('id="alfa-chat-window"', html)
        self.assertIn('id="alfa-chat-reset-btn"', html)
        self.assertIn('data-user-id=', html)
        self.assertIn('chat-widget.js', html)

        # Проверка удаления лишних плашек
        self.assertNotIn("На базе Gemini AI", html)
        self.assertNotIn("Нейросеть онлайн", html)

    def test_empty_message_rejected(self):
        response = self.client.post("/api/chat", json={"message": "   "})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data.get("ok"))

    def test_fallback_replies_cover_core_business_domains_and_contain_links(self):
        start_reply = _fallback_reply("С чего начать бизнес и как открыть ИП?")
        self.assertIn("ИП", start_reply)
        self.assertIn("шаг", start_reply.lower())
        self.assertIn("/materials/", start_reply)

        sales_reply = _fallback_reply("Как поднять продажи на Wildberries и Ozon?")
        self.assertIn("продаж", sales_reply.lower())
        self.assertIn("/materials/", sales_reply)

        tax_reply = _fallback_reply("Какой налог выбрать для УСН?")
        self.assertIn("УСН", tax_reply)
        self.assertIn("/materials/", tax_reply)

    def test_linkify_materials_converts_known_titles(self):
        text = "Советую курс «Маркетплейсы: с чего начать и как преуспеть» для старта."
        material = Material("course", "Маркетплейсы: с чего начать и как преуспеть", "https://example.com/course")
        converted = _linkify_materials(text, [material])
        self.assertIn("[Маркетплейсы: с чего начать и как преуспеть](/materials/course)", converted)

    def test_fallback_reply_personalization(self):
        profile = {"industry": "общепит", "stage": "запуск"}
        reply = _fallback_reply("Как поднять продажи?", profile=profile)
        self.assertIn("общепит", reply.lower())

    def test_chat_endpoint_returns_json_reply(self):
        response = self.client.post("/api/chat", json={"message": "Привет"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json().get("ok"))
        self.assertEqual(self.client.post("/api/chat", json={"message": 123}).status_code, 400)

    def test_chat_uses_adaptive_weight_and_reading_history(self):
        self.app.config["CHAT_CATALOG"] = (
            Material("template", "Акт сверки взаиморасчётов", "https://example.com/template", access="free",
                     description="Помогает сверить задолженность контрагента."),
            Material("first", "Договор с поставщиком", "https://example.com/first", access="free", entity_type="article"),
            Material("second", "Работа с контрагентами", "https://example.com/second", access="free", entity_type="article"),
        )
        history = (InteractionEvent("first", "click"), InteractionEvent("second", "click"))
        for events, expected in (((), 0.749), (history, 0.25)):
            with self.subTest(events=events), patch("app.ui.chat.recent_interactions", return_value=events), \
                 patch("app.recommender.engine.merge_orders", wraps=merge_orders) as fuse:
                response = self.client.post("/api/chat", json={"message": "Хочу сверить задолженность контрагента."})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(fuse.call_args.kwargs["weights"][1], expected)

    def test_chat_uses_entitled_catalog_and_filters_model_links(self):
        self.app.config["CHAT_CATALOG"] = (
            Material("free", "Бесплатный курс по продажам", "https://example.com/free", access="free"),
            Material("plus", "Подписочный курс по продажам", "https://example.com/plus", access="paid", subscriptions=("Курс+",)),
            Material("paid", "Платный курс по продажам", "https://example.com/paid", access="paid"),
        )
        self.credentials.return_value = ("mock", "gemini-3.5-flash-lite")
        reply = "• [Бесплатный](/materials/free)\n• [Подписочный](/materials/plus)\n• [Платный](/materials/paid)"
        with patch("app.ui.chat._call_gemini", return_value=reply) as call:
            response = self.client.post("/api/chat", json={"message": "курс по продажам", "history": [None, {"role": "user", "text": 7}]})
            self.assertEqual(response.status_code, 200)
            self.assertIn("/materials/free", response.json["reply"])
            self.assertNotIn("/materials/plus", response.json["reply"])
            self.assertNotIn("/materials/paid", response.json["reply"])
            self.assertEqual(call.call_args.args[3], [])
            self.assertNotIn("/materials/plus", call.call_args.kwargs["system_prompt"])
            self.assertEqual(self.app.test_cli_runner().invoke(args=["admin", "course-plus", "demo"]).exit_code, 0)
            self.client.get("/login")
            with self.client.session_transaction() as session:
                token = session["csrf"]
            self.assertEqual(self.client.post("/login/demo", data={"csrf_token": token}).status_code, 303)
            result = self.client.post("/api/chat", json={"message": "курс по продажам"}).json["reply"]
            self.assertIn("/materials/plus", result)
            self.assertNotIn("/materials/paid", result)

    def test_material_page_allows_chat_script_and_fetch(self):
        result = self.client.get("/materials/start-checklist")
        self.assertEqual(result.status_code, 200)
        self.assertIn("chat-widget.js", result.get_data(as_text=True))
        self.assertIn("connect-src 'self'", result.headers["Content-Security-Policy"])


    def test_hybrid_ranking_selects_relevant_materials(self):
        from app.catalog import DEMO_CATALOG_PATH, load_catalog
        from app.ui.chat import _find_relevant_materials

        catalog = load_catalog(DEMO_CATALOG_PATH)
        results = _find_relevant_materials(
            "продажи для магазина",
            {"industry": "розница", "stage": "старт"},
            catalog,
            viewed_titles=[],
            limit=3,
        )
        self.assertTrue(len(results) > 0)
        matched_ids = [m.id for m in results]
        self.assertIn("marketing-first-sales", matched_ids)


if __name__ == "__main__":
    unittest.main()
