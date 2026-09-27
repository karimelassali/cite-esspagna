import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import scraper


class MultiChatParsingTests(unittest.TestCase):
    def test_parse_multiple_chat_ids_comma_space_semicolon_dedup(self):
        with patch.dict(os.environ, {"CHAT_IDS": "123, 456; 789   123, -100111222"}, clear=True):
            self.assertEqual(scraper.get_chat_ids(), ["123", "456", "789", "-100111222"])

    def test_fallback_to_single_chat_id(self):
        with patch.dict(os.environ, {"CHAT_ID": " 987654321 "}, clear=True):
            self.assertEqual(scraper.get_chat_ids(), ["987654321"])

    def test_chat_ids_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(scraper.get_chat_ids(), [])


class SendTelegramMultiChatTests(unittest.TestCase):
    @patch("scraper.requests.post")
    def test_send_telegram_sends_to_all_chats(self, mock_post):
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {"ok": True}
        mock_post.return_value = mock_response

        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "test_token", "CHAT_IDS": "chat1, chat2"}, clear=True):
            scraper.send_telegram("Hello test")

        self.assertEqual(mock_post.call_count, 2)
        chats_called = [call.kwargs["json"]["chat_id"] for call in mock_post.call_args_list]
        self.assertEqual(chats_called, ["chat1", "chat2"])

    @patch("scraper.requests.post")
    def test_send_telegram_partial_failure_continues(self, mock_post):
        res_fail = MagicMock()
        res_fail.ok = False
        res_fail.json.return_value = {"ok": False, "description": "Forbidden"}

        res_ok = MagicMock()
        res_ok.ok = True
        res_ok.json.return_value = {"ok": True}

        mock_post.side_effect = [res_fail, res_ok]

        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "test_token", "CHAT_IDS": "chat1, chat2"}, clear=True):
            # Should not raise because chat2 succeeded
            scraper.send_telegram("Hello partial test")

        self.assertEqual(mock_post.call_count, 2)

    @patch("scraper.requests.post")
    def test_send_telegram_all_failed_raises_error(self, mock_post):
        res_fail = MagicMock()
        res_fail.ok = False
        res_fail.json.return_value = {"ok": False, "description": "Forbidden"}
        mock_post.return_value = res_fail

        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "test_token", "CHAT_IDS": "chat1, chat2"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                scraper.send_telegram("Hello failure test")
            self.assertIn("failed for all configured chats", str(ctx.exception))


class Notifications(unittest.TestCase):
    def test_three_distinct_outcomes(self):
        for outcome, code, expected in [(True, 0, 'disponible'),
                                        (False, 0, 'No hay'),
                                        (RuntimeError('blocked'), 1, 'desconocido')]:
            with self.subTest(outcome=outcome), \
                 patch.object(sys, 'argv', ['scraper.py']), \
                 patch.dict(os.environ, {'TELEGRAM_TOKEN': 'test', 'CHAT_IDS': 'test', 'ALWAYS_NOTIFY': 'true'}, clear=True), \
                 patch.object(scraper, 'validate_configuration'), \
                 patch.object(scraper, 'send_telegram') as send, \
                 patch.object(scraper, 'check_appointments', return_value=outcome,
                              side_effect=outcome if isinstance(outcome, Exception) else None):
                self.assertEqual(scraper.main(), code)
                send.assert_called_once()
                self.assertIn(expected, send.call_args.args[0])


if __name__ == '__main__':
    if '--live' in sys.argv:
        sys.argv.remove('--live')
        print("Sending live test notification to Telegram...")
        scraper.test_telegram_notification()
    else:
        unittest.main()
