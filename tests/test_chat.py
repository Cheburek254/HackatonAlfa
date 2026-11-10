"""Тесты для ИИ-помощника Альфа-Курс."""

import unittest
from unittest.mock import patch

from app.ui.chat import _fallback_reply, _linkify_materials
from app.ui.web import create_app


class ChatApiTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
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
        converted = _linkify_materials(text, [])
        self.assertIn("[Маркетплейсы: с чего начать и как преуспеть](/materials/d0aa4598-3da2-478b-bbf8-bda81b231b58)", converted)

    def test_fallback_reply_personalization(self):
        profile = {"industry": "общепит", "stage": "запуск"}
        reply = _fallback_reply("Как поднять продажи?", profile=profile)
        self.assertIn("общепит", reply.lower())

    def test_chat_endpoint_returns_json_reply(self):
        with patch("app.ui.chat._call_gemini", return_value="Привет! Посмотри курс [Старт](/materials/123)."):
            response = self.client.post("/api/chat", json={"message": "Привет"})
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data.get("ok"))
            self.assertIn("/materials/123", data.get("reply", ""))


if __name__ == "__main__":
    unittest.main()
