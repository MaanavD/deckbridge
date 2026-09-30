#!/usr/bin/env python3
"""Five-hour usage parsing, and that it lands on the Claude and OpenAI keys."""
import unittest

import connector_agents
import usage_limits


class TestUsageParsers(unittest.TestCase):
    def test_claude_five_hour_is_a_percent(self):
        percent, resets = usage_limits.claude_session_percent({
            "rate_limits": {
                "five_hour": {"utilization": 37, "resets_at": "2026-09-29T18:00:00Z"},
                "seven_day": {"utilization": 88, "resets_at": "2026-10-04T18:00:00Z"},
            },
        })
        self.assertEqual(percent, 37)
        self.assertGreater(resets, 0)

    def test_codex_primary_window_is_the_five_hour_bucket(self):
        percent, _ = usage_limits.codex_session_percent({
            "rate_limit": {
                "primary_window": {
                    "used_percent": 0,
                    "limit_window_seconds": 18000,
                    "reset_at": 1790719290,
                },
                "secondary_window": {
                    "used_percent": 41,
                    "limit_window_seconds": 604800,
                },
            },
        })
        self.assertEqual(percent, 0)

    def test_codex_ignores_a_window_that_is_not_five_hours(self):
        percent, _ = usage_limits.codex_session_percent({
            "rate_limit": {
                "primary_window": {
                    "used_percent": 41,
                    "limit_window_seconds": 604800,
                },
            },
        })
        self.assertIsNone(percent)

    def test_an_unstarted_five_hour_window_is_still_full(self):
        """The session clock starts on first spend, so no window means nothing used."""
        self.assertEqual(
            usage_limits.claude_session_percent({"rate_limits": {"five_hour": None}}),
            (0, 0.0),
        )
        self.assertEqual(
            usage_limits.claude_session_percent({"rate_limits": {}}),
            (0, 0.0),
        )

    def test_a_failed_read_keeps_an_open_window_and_otherwise_stays_full(self):
        usage_limits.clear_cache()

        def failed(_source):
            return None, 0.0, 120

        original = usage_limits._probe
        usage_limits._probe = failed
        try:
            self.assertEqual(usage_limits.session_percent("claude-code", now=1000), 0)
            usage_limits._cache["claude-code"] = (1000.0, 40, 5000.0, 1000.0)
            self.assertEqual(usage_limits.session_percent("claude-code", now=2000), 40)
        finally:
            usage_limits._probe = original
            usage_limits.clear_cache()

    def test_a_finished_window_reads_as_empty(self):
        self.assertEqual(usage_limits.fresh_percent(100, 1000, 1001), 0)
        self.assertEqual(usage_limits.fresh_percent(40, 1000, 999), 40)

    def test_session_percent_uses_a_cached_reading(self):
        usage_limits.clear_cache()
        calls = {"n": 0}

        def fake(source):
            calls["n"] += 1
            return 40, 1000 + 3600

        original = usage_limits._probe
        usage_limits._probe = fake
        try:
            self.assertEqual(usage_limits.session_percent("claude-code", now=1000), 40)
            self.assertEqual(usage_limits.session_percent("claude-code", now=1020), 40)
            self.assertEqual(calls["n"], 1)
            self.assertIsNone(usage_limits.session_percent("slack", now=1000))
            self.assertEqual(calls["n"], 1)
        finally:
            usage_limits._probe = original
            usage_limits.clear_cache()

    def test_cached_reading_goes_empty_once_the_window_resets(self):
        usage_limits.clear_cache()
        usage_limits._cache["codex-cli"] = (1590.0, 90, 1500.0)
        try:
            self.assertEqual(usage_limits.session_percent("codex-cli", now=1600), 0)
        finally:
            usage_limits.clear_cache()


class TestLauncherMeters(unittest.TestCase):
    def test_only_claude_and_openai_launchers_carry_the_meter(self):
        def reader(source):
            return {"claude-code": 0, "codex-cli": 88}.get(source)

        connector = connector_agents.AgentConnector(
            claim=(0, 13),
            hermes_state="/tmp/deckbridge-no-hermes.json",
            local_state="/tmp/deckbridge-no-local.json",
            apps_config="/tmp/deckbridge-no-apps.json",
            usage_reader=reader,
        )
        faces = connector.build_faces([])
        self.assertEqual(faces[8]["source"], "claude-code")
        self.assertEqual(faces[8]["usage"], 0)
        self.assertEqual(faces[9]["source"], "codex-cli")
        self.assertEqual(faces[9]["usage"], 88)
        self.assertNotIn("usage", faces[6])
        self.assertNotIn("usage", faces[7])


if __name__ == "__main__":
    unittest.main()
