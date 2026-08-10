#!/usr/bin/env python3
import unittest
from unittest.mock import patch

from app_badges import AppBadgeProvider, parse_badges


class BadgeParsingTests(unittest.TestCase):
    def test_layered_badges(self):
        raw = "\n".join([
            "DOCK\tDiscord\t1",
            "DOCK\tSlack\t",
            "DOCK\tNotion Calendar\t3",
            "WIN\tSlack\tteam - 2 new items - Slack",
            "WIN\tDiscord\t(4) Discord",
            "TAB\tInbox (7) - work@example.com - Gmail\thttps://mail.google.com/mail/u/0/#inbox",
        ])
        self.assertEqual(parse_badges(raw), {
            "slack": 2, "gmail": 7, "discord": 4, "notion-calendar": 3,
        })

    def test_gmail_ignores_non_work_account_route(self):
        raw = "TAB\tInbox (8) - personal - Gmail\thttps://mail.google.com/mail/u/1/#inbox"
        self.assertEqual(parse_badges(raw)["gmail"], 0)

    def test_missing_indicators_are_zero(self):
        self.assertEqual(parse_badges("WIN\tNotion Calendar\tAug 9–15"), {
            "slack": 0, "gmail": 0, "discord": 0, "notion-calendar": 0,
        })

    @patch("app_badges.sys.platform", "darwin")
    def test_transient_probe_failure_preserves_last_good_counts(self):
        class Result:
            returncode = 1
            stdout = ""

        provider = AppBadgeProvider(runner=lambda *args, **kwargs: Result())
        provider._counts = {"slack": 2}
        self.assertEqual(provider.refresh(), {"slack": 2})


if __name__ == "__main__":
    unittest.main()
