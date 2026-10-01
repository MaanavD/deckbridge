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

    def test_codex_weekly_window_wins_over_an_idle_five_hour_bucket(self):
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
        self.assertEqual(percent, 41)

    def test_codex_primary_can_itself_be_the_weekly_plan(self):
        percent, _ = usage_limits.codex_session_percent({
            "rate_limit": {
                "primary_window": {
                    "used_percent": 14,
                    "limit_window_seconds": 604800,
                },
            },
        })
        self.assertEqual(percent, 14)

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

    def test_a_failed_read_keeps_an_open_window_and_does_not_look_full(self):
        usage_limits.clear_cache()

        def failed(_source):
            return None, 0.0, 120

        original = usage_limits._probe
        usage_limits._probe = failed
        try:
            self.assertIsNone(usage_limits.session_percent("claude-code", now=1000))
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
    def test_opencode_rolling_window_is_the_five_hour_bucket(self):
        percent, _resets = usage_limits.opencode_session_percent({
            "usage": {
                "rolling": {"percent": 3, "resetsAt": "2026-10-01T20:00:44.640Z"},
                "weekly": {"percent": 19, "resetsAt": "2026-10-05T00:00:00.000Z"},
            },
        })
        self.assertEqual(percent, 3)

    def test_opencode_without_a_rolling_window_is_still_full(self):
        self.assertEqual(usage_limits.opencode_session_percent({"usage": {}}), (0, 0.0))

    def test_cursor_plan_uses_the_combined_percent(self):
        percent, resets = usage_limits.cursor_plan_percent({
            "billingCycleEnd": 1792667469000,
            "planUsage": {"totalPercentUsed": 9.944, "apiPercentUsed": 0},
        })
        self.assertEqual(percent, 10)
        self.assertGreater(resets, 1e9)

    def test_launcher_keys_carry_their_provider_meters(self):
        def reader(source):
            return {
                "claude-code": 0, "codex-cli": 88,
                "hermes-discord": 3, "t3code": 10,
            }.get(source)

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
        self.assertEqual(faces[6]["source"], "hermes-discord")
        self.assertEqual(faces[6]["usage"], 3)
        self.assertEqual(faces[7]["source"], "t3code")
        self.assertEqual(faces[7]["usage"], 10)


if __name__ == "__main__":
    unittest.main()
