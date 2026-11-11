"""Тесты для ИИ-помощника Альфа-Курс."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.contracts import InteractionEvent, Material
from app.recommender.hybrid import merge_orders
from app.ui.accounts import set_recommendation_mode
from app.ui.chat import CHAT_CLARIFICATION, _fallback_reply, _linkify_materials
from app.ui.chat_metrics import chat_statistics
from app.ui.web import create_app


class ChatApiTest(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"ALFA_CATALOG_DIR": ""}))
        self.enterContext(patch("app.catalog.DATA_DIR", Path(temporary)))
        self.credentials = self.enterContext(patch("app.ui.chat._load_env_credentials", return_value=("", "")))
        self.openrouter = self.enterContext(patch("app.ui.chat._load_openrouter_credentials", return_value=("", "")))
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

    def test_fallback_only_uses_selected_catalog_cards(self):
        card = Material("guide", "Учёт на УСН", "https://example.com/guide", description="<p>Проверка расходов.</p>")
        reply = _fallback_reply([card])
        self.assertIn("Проверка расходов.", reply)
        self.assertNotIn("<p>", reply)
        self.assertEqual(reply.count("/materials/"), 1)
        self.assertIn("/materials/guide", reply)
        self.assertEqual(_fallback_reply([]), CHAT_CLARIFICATION)

    def test_linkify_materials_converts_known_titles(self):
        text = "Советую курс «Маркетплейсы: с чего начать и как преуспеть» для старта."
        material = Material("course", "Маркетплейсы: с чего начать и как преуспеть", "https://example.com/course")
        converted = _linkify_materials(text, [material])
        self.assertIn("[Маркетплейсы: с чего начать и как преуспеть](/materials/course)", converted)

    def test_help_and_unclear_messages_skip_search_and_providers(self):
        with patch("app.ui.chat._find_relevant_materials") as search, patch("app.ui.chat._load_env_credentials") as provider:
            for query, intent, snippet in (
                ("У меня не открывается курс по налогам", "support", "Какой материал"),
                ("Что открывает подписка Курс+?", "subscription", "покупаются отдельно"),
                ("Как поменять пароль?", "account", "Код появится в форме демо"),
                ("Как войти в аккаунт?", "account", "/login"),
                ("Россия Путин Победа", "clarification", CHAT_CLARIFICATION),
                ("Какая погода завтра?", "clarification", CHAT_CLARIFICATION),
                ("Привет!", "greeting", "Чем помочь?"),
            ):
                with self.subTest(query=query):
                    response = self.client.post("/api/chat", json={"message": query})
                    self.assertEqual(response.json["intent"], intent)
                    self.assertIn(snippet, response.json["reply"])
                    self.assertEqual(response.json["cards"], [])
            search.assert_not_called()
            provider.assert_not_called()

    def test_card_memory_preserves_display_order_across_help_and_details(self):
        catalog = self.app.config["CHAT_CATALOG"] = tuple(
            Material(mid, title, f"https://example.com/{mid}", access="free", description=f"Описание {title}")
            for mid, title in (("a", "Акт сверки"), ("b", "Шаблон сверки"), ("c", "Разбор сверки"))
        )
        with patch("app.ui.chat._find_relevant_materials", return_value=list(catalog)) as search:
            first = self.client.post("/api/chat", json={"message": "Подбери документы для сверки"}).json
            self.assertEqual([card["id"] for card in first["cards"]], ["a", "b", "c"])
            token = first["card_context"]
            help_turn = self.client.post("/api/chat", json={"message": "Что такое Курс+?", "card_context": token}).json
            self.assertEqual(help_turn["card_context"], token)
            for query, expected in (("Расскажи про второй материал", "b"), ("А про третий", "c")):
                turn = self.client.post("/api/chat", json={"message": query, "card_context": token}).json
                self.assertEqual(turn["intent"], "card_detail")
                self.assertEqual(turn["cards"][0]["id"], expected)
                self.assertEqual(turn["card_context"], token)
            other_guest = self.app.test_client().post("/api/chat", json={
                "message": "Расскажи про второй материал", "card_context": token,
            }).json
            self.assertEqual(other_guest["cards"], [])
            self.assertIn("Сначала попросите", other_guest["reply"])
            search.assert_called_once()

    def test_chat_rate_limit_and_catalog_independence(self):
        with self.app.app_context():
            set_recommendation_mode("popular")
        self.app.config["CHAT_BM25_WEIGHT"] = 0.4
        card = Material("draft", "Акт сверки", "https://example.com/draft", access="free")
        with patch("app.ui.chat.CHAT_REQUESTS_PER_MINUTE", 1), \
             patch("app.ui.chat._find_relevant_materials", return_value=[card]) as search:
            first = self.client.post("/api/chat", json={"message": "Нужен акт сверки"})
            self.assertEqual(first.json["cards"][0]["id"], "draft")
            self.assertEqual(search.call_args.kwargs["bm25_weight"], 0.4)
            limited = self.client.post("/api/chat", json={"message": "Нужен акт сверки"})
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers["Retry-After"], "60")
            search.assert_called_once()

    def test_provider_fallback_budget_and_open_metrics(self):
        self.credentials.return_value = ("mock", "gemini")
        self.openrouter.return_value = ("mock", "router")
        card = Material("draft", "Акт сверки", "https://example.com/draft", access="free")
        with patch("app.ui.chat._find_relevant_materials", return_value=[card]), \
             patch("app.ui.chat._call_gemini", return_value=None) as gemini, \
             patch("app.ui.chat._call_openrouter", return_value=None) as router:
            response = self.client.post("/api/chat", json={"message": "Нужен акт сверки", "history": [
                {"role": "user", "text": "Нужен акт сверки"},
            ]}).json
        self.assertEqual(response["source"], "fallback")
        self.assertEqual(gemini.call_args.args[3], [])
        self.assertEqual(gemini.call_args.kwargs["deadline"], router.call_args.kwargs["deadline"])
        self.assertFalse(gemini.call_args.kwargs["retry_default"])
        for _ in range(2):
            self.assertEqual(self.client.post("/api/chat/card-open", json={
                "material_id": "draft", "click_context": response["click_context"],
            }).status_code, 204)
        self.assertEqual(self.client.post("/api/chat/card-open", json={
            "material_id": "unlisted", "click_context": response["click_context"],
        }).status_code, 403)
        with self.app.app_context():
            stats = chat_statistics()
        self.assertEqual((stats["responses"], stats["fallback"], stats["opened"]), (1, 1, 1))
        self.assertIsNotNone(stats["p95_ms"])

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
            Material("free", "Бесплатный курс по продажам", "https://example.com/free", access="free", format="курс"),
            Material("plus", "Подписочный курс по продажам", "https://example.com/plus", access="paid",
                     format="курс", subscriptions=("Курс+",)),
            Material("paid", "Платный курс по продажам", "https://example.com/paid", access="paid", format="курс"),
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
