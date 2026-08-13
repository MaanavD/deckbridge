#!/usr/bin/env python3
"""Fast regression tests for desktop-session route discovery."""
from __future__ import annotations

import desktop_sessions_watcher as watcher


def test_routes() -> None:
    assert watcher.route_id("claude://claude.ai/chat/a1", watcher.SURFACES[0][3]) == "a1"
    assert watcher.route_id("claude://claude.ai/epitaxy/local_a2", watcher.SURFACES[0][3]) == "local_a2"
    assert watcher.route_id("codex://threads/thread_3", watcher.SURFACES[1][3]) == "thread_3"
    assert watcher.route_id("cursor://anysphere.cursor-deeplink/background-agent?bcId=c4", watcher.SURFACES[2][3]) == "c4"
    assert watcher.route_id("https://example.com/not-a-session", watcher.SURFACES[0][3]) == ""
    assert watcher.t3code_thread_id("t3code://app/#/env-1/thread-7") == "thread-7"
    assert watcher.t3code_thread_id("http://127.0.0.1:3773/env-1/thread-7") == ""


def test_records() -> None:
    rows = watcher.parse_helper_lines(
        "1\tPlan release\tclaude://claude.ai/chat/a1\n2\tClaude\tclaude://claude.ai/chat/a1\n",
        "claude-desktop", "Claude", watcher.SURFACES[0][3], 12.5,
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "Plan release"
    assert rows[0]["session_id"] == "a1"
    assert rows[0]["desktop_surface"] is True

    fallback = watcher.parse_helper_lines(
        "0\tClaude\t\n", "claude-desktop", "Claude",
        watcher.SURFACES[0][3], 12.5,
    )
    assert fallback == []


def test_hammerspoon_snapshot() -> None:
    rows = watcher.parse_hammerspoon_snapshot(
        '{"sessions":[{"title":"Release plan - Claude",'
        '"url":"claude.ai/chat/a1","status":"done"}]}', 20.0,
    )
    assert rows == [{
        "name": "Release plan", "display_title": "Release plan",
        "status": "done", "source": "claude-desktop",
        "session_id": "a1", "app": "Claude", "window": "",
        "url": "claude.ai/chat/a1", "updated_at": 20.0,
        "desktop_surface": True, "exact_route": True, "focused": False,
    }]

    assert watcher.parse_hammerspoon_snapshot(
        '{"sessions":[{"title":"Claude","url":"",'
        '"status":"working"}]}', 20.0,
    ) == []


def test_stable_event_timestamps() -> None:
    previous = {"agents": [{
        "source": "claude-desktop", "session_id": "a1",
        "status": "done", "updated_at": 20.0,
    }]}
    unchanged = [{
        "source": "claude-desktop", "session_id": "a1",
        "status": "done", "updated_at": 30.0,
    }]
    assert watcher.stabilize_updates(previous, unchanged, 30.0)[0]["updated_at"] == 20.0

    changed = [{
        "source": "claude-desktop", "session_id": "a1",
        "status": "working", "updated_at": 30.0,
    }]
    assert watcher.stabilize_updates(previous, changed, 30.0)[0]["updated_at"] == 30.0


def test_selected_surface_parsers() -> None:
    assert watcher._active_cmux_surface(
        '{"active":{"surface_id":"surface:7"}}'
    ) == "surface:7"
    assert watcher._current_herdr_pane(
        '{"result":{"pane":{"pane_id":"w2:p4"}}}'
    ) == "w2:p4"
    assert watcher._active_cmux_surface("not json") == ""


def test_manual_view_discovery() -> None:
    original = watcher._run
    try:
        watcher._run = lambda argv, **_kwargs: (  # type: ignore[assignment]
            "Discord|com.hnc.Discord|42"
            if "--helper-frontmost" in argv else
            "https://discord.com/channels/guild/thread/message"
            if "--helper-web-url" in argv else ""
        )
        assert watcher.scan_viewed("helper", []) == [{
            "source": "hermes-discord",
            "url": "https://discord.com/channels/guild/thread/message",
        }]

        watcher._run = lambda argv, **_kwargs: (  # type: ignore[assignment]
            "ChatGPT|com.openai.codex|43"
            if "--helper-frontmost" in argv else ""
        )
        assert watcher.scan_viewed("helper", []) == [{
            "app": "ChatGPT", "unique_app": "1",
        }]

        watcher._run = lambda argv, **_kwargs: (  # type: ignore[assignment]
            "T3 Code (Alpha)|com.t3tools.t3code|44"
            if "--helper-frontmost" in argv else
            "t3code://app/#/env-1/thread-7"
            if "--helper-web-url" in argv else ""
        )
        assert watcher.scan_viewed("helper", []) == [{"session_id": "thread-7"}]
    finally:
        watcher._run = original


if __name__ == "__main__":
    test_routes()
    test_records()
    test_hammerspoon_snapshot()
    test_stable_event_timestamps()
    test_selected_surface_parsers()
    test_manual_view_discovery()
    print("PASS desktop session routes and records")
