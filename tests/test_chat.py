"""Тесты для ИИ-помощника Альфа-Курс."""

import unittest
from unittest.mock import patch

from app.ui.chat import _fallback_reply
from app.ui.web import create_app


class ChatApiTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_chat_widget_rendered_on_index(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="alfa-chat-widget"', html)
        self.assertIn('id="alfa-chat-trigger"', html)
        self.assertIn('id="alfa-chat-window"', html)
        self.assertIn('chat-widget.js', html)

    def test_empty_message_rejected(self):
        response = self.client.post("/api/chat", json={"message": "   "})
        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertFalse(data.get("ok"))

    def test_fallback_replies_cover_core_business_domains(self):
        start_reply = _fallback_reply("С чего начать бизнес и как открыть ИП?")
        self.assertIn("ИП", start_reply)
        self.assertIn("шаг", start_reply.lower())

        sales_reply = _fallback_reply("Как поднять продажи на Wildberries и Ozon?")
        self.assertIn("продаж", sales_reply.lower())

        tax_reply = _fallback_reply("Какой налог выбрать для УСН?")
        self.assertIn("УСН", tax_reply)

    def test_chat_endpoint_returns_json_reply(self):
        with patch("app.ui.chat._call_gemini", return_value="Привет! Я помогу с курсами."):
            response = self.client.post("/api/chat", json={"message": "Привет"})
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("reply"), "Привет! Я помогу с курсами.")


if __name__ == "__main__":
    unittest.main()
