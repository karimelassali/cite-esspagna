import sys
import unittest
from unittest.mock import patch

import scraper


class Notifications(unittest.TestCase):
    def test_three_distinct_outcomes(self):
        for outcome, code, expected in [(True, 0, 'disponible'),
                                        (False, 0, 'No hay'),
                                        (RuntimeError('blocked'), 1, 'desconocido')]:
            with self.subTest(outcome=outcome), \
                 patch.object(sys, 'argv', ['scraper.py']), \
                 patch.multiple(scraper, TELEGRAM_TOKEN='test', CHAT_ID='test', ALWAYS_NOTIFY=True), \
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
