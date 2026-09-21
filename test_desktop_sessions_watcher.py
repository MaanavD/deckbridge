#!/usr/bin/env python3
"""Fast regression tests for desktop-session route discovery."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

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


def test_claude_desktop_session_files() -> None:
    now = 1_790_000_000.0
    recent = {
        "sessionId": "local_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "cliSessionId": "11111111-2222-3333-4444-555555555555",
        "title": "Sf flux events search",
        "cwd": "/Users/maanav/Library/Application Support/Claude/local-agent-mode-sessions/x/outputs",
        "originCwd": "/Users/maanav/Documents/audio_test",
        "lastActivityAt": (now - 60) * 1000,
        "isArchived": False,
    }
    row = watcher.parse_claude_session_document(recent, now, alive=False)
    assert row is not None
    assert row["name"] == "Sf flux events search"
    assert row["source"] == "claude-desktop"
    assert row["status"] == "done"
    assert row["url"] == "claude://claude.ai/epitaxy/local_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    scheduled = dict(recent, sessionType="scheduled", scheduledTaskId="sf-flux-events-search",
                     title="Sf flux events search")
    chat = watcher.parse_claude_session_document(scheduled, now, alive=False)
    assert chat is not None
    assert chat["session_id"] == "sf-flux-events-search"
    assert chat["url"] == "claude://claude.ai/scheduled-task/sf-flux-events-search"
    assert row["cwd"] == "/Users/maanav/Documents/audio_test"
    assert row["cli_session_id"] == "11111111-2222-3333-4444-555555555555"

    working = watcher.parse_claude_session_document(recent, now, alive=True)
    assert working is not None and working["status"] == "working"

    stale = dict(recent, lastActivityAt=(now - 7 * 3600) * 1000)
    assert watcher.parse_claude_session_document(stale, now, alive=False) is None
    assert watcher.parse_claude_session_document(
        dict(recent, isArchived=True), now, alive=True,
    ) is None

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = root / "acct" / "space"
        folder.mkdir(parents=True)
        (folder / "local_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.json").write_text(
            json.dumps(recent), encoding="utf-8",
        )
        (folder / "local_old.json").write_text(json.dumps({
            "sessionId": "local_old", "title": "Old", "isArchived": False,
            "lastActivityAt": (now - 9 * 3600) * 1000,
        }), encoding="utf-8")
        found = watcher.scan_claude_session_files(
            now, roots=[root], command_lines="node claude --session 11111111-2222-3333-4444-555555555555",
        )
        assert [item["session_id"] for item in found] == [
            "local_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ]
        assert found[0]["status"] == "working"


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
    original_hs = watcher._discord_url_from_hammerspoon
    # Live Discord would leak into the ChatGPT/T3 cases through Hammerspoon.
    watcher._discord_url_from_hammerspoon = lambda: ""  # type: ignore[assignment]
    try:
        def discord_url_for(argv: list[str]) -> str:
            if "--helper-web-url" in argv and argv[-1] == watcher.DISCORD_BUNDLE:
                return "https://discord.com/channels/1507988913527062618/1549243762025173053/9"
            return ""

        watcher._run = lambda argv, **_kwargs: (  # type: ignore[assignment]
            "Discord|com.hnc.Discord|42"
            if "--helper-frontmost" in argv else discord_url_for(argv)
        )
        assert watcher.scan_viewed("helper", []) == [{
            "source": "hermes-discord",
            "url": "https://discord.com/channels/1507988913527062618/1549243762025173053/9",
        }]

        # Hermes Discord work is read in Discord, then the operator returns to
        # the editor. The selected channel is still the view of that thread.
        watcher._run = lambda argv, **_kwargs: (  # type: ignore[assignment]
            "Google Chrome|com.google.Chrome|719"
            if "--helper-frontmost" in argv else
            "https://discord.com/channels/1507988913527062618/1549244444111405178"
            if "--helper-web-url" in argv and argv[-1] == watcher.DISCORD_BUNDLE else
            "https://www.facebook.com/marketplace"
            if "--helper-web-url" in argv else ""
        )
        assert watcher.scan_viewed("helper", []) == [{
            "source": "hermes-discord",
            "url": "https://discord.com/channels/1507988913527062618/1549244444111405178",
        }]

        # Launchd cannot use Deckbridge Mic for Discord AX; Hammerspoon can,
        # and its CLI prefixes JSON with an extension-load line.
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "hs"
            cli.write_text(
                "#!/bin/sh\n"
                "echo '-- Loading extension: axuielement'\n"
                "echo '{\"url\":\"https://discord.com/channels/1/2\"}'\n",
                encoding="utf-8",
            )
            cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
            watcher._run = lambda argv, **_kwargs: (  # type: ignore[assignment]
                "Google Chrome|com.google.Chrome|719"
                if "--helper-frontmost" in argv else ""
            )
            watcher._discord_url_from_hammerspoon = original_hs
            previous = os.environ.get("DECKBRIDGE_HS_CLI")
            os.environ["DECKBRIDGE_HS_CLI"] = str(cli)
            try:
                assert watcher.scan_viewed("helper", []) == [{
                    "source": "hermes-discord",
                    "url": "https://discord.com/channels/1/2",
                }]
            finally:
                if previous is None:
                    os.environ.pop("DECKBRIDGE_HS_CLI", None)
                else:
                    os.environ["DECKBRIDGE_HS_CLI"] = previous
                watcher._discord_url_from_hammerspoon = (  # type: ignore[assignment]
                    lambda: ""
                )

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
            if "--helper-web-url" in argv and argv[-1] == watcher.T3CODE_BUNDLE else ""
        )
        assert watcher.scan_viewed("helper", []) == [{"session_id": "thread-7"}]
    finally:
        watcher._run = original
        watcher._discord_url_from_hammerspoon = original_hs


if __name__ == "__main__":
    test_routes()
    test_records()
    test_claude_desktop_session_files()
    test_hammerspoon_snapshot()
    test_stable_event_timestamps()
    test_selected_surface_parsers()
    test_manual_view_discovery()
    print("PASS desktop session routes and records")
