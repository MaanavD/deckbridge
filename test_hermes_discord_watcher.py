#!/usr/bin/env python3
"""Reliability-interface tests for the Discord REST watcher."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from urllib.error import URLError

import hermes_discord_watcher as watcher
from connection_runtime import HealthReporter


RESULTS: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}{'' if ok or not detail else ': ' + detail}")


def main() -> int:
    class Completed:
        returncode = 0
        stdout = (
            "DISCORD_BOT_TOKEN=remote-token\n"
            "DISCORD_HOME_CHANNEL=123\n"
            "DISCORD_GUILD_ID=456\n"
            "UNRELATED_SECRET=do-not-read\n"
        )
        stderr = ""

    original_run = watcher.subprocess.run
    commands: list[list[str]] = []
    watcher.subprocess.run = lambda command, **_kwargs: (  # type: ignore[assignment]
        commands.append(command) or Completed()
    )
    try:
        remote = watcher.fetch_remote_discord_config("hermes", timeout=3)
    finally:
        watcher.subprocess.run = original_run
    check("remote credential adapter reads only allowlisted Discord keys",
          remote == {
              "DISCORD_BOT_TOKEN": "remote-token",
              "DISCORD_HOME_CHANNEL": "123",
              "DISCORD_GUILD_ID": "456",
          }, str(remote))
    check("remote credential SSH is noninteractive and bounded",
          commands
          and "-oBatchMode=yes" in commands[0]
          and any(part.startswith("-oConnectTimeout=") for part in commands[0]),
          str(commands))
    parsed = watcher.parse_args(["--ssh-env", "hermes"])
    check("remote credential mode needs no startup token or channel",
          parsed.ssh_env == "hermes" and parsed.channel_id is None)

    with tempfile.TemporaryDirectory(prefix="deckbridge-discord-health-") as tmp:
        health_path = Path(tmp) / "discord_watcher.json"
        reporter = HealthReporter("discord_watcher", path=health_path, stale_after=10)
        original_poll = watcher.poll_once
        try:
            watcher.poll_once = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[assignment]
                URLError("Discord edge offline")
            )
            watcher.run_watcher(
                "secret-token", "channel", state_path=Path(tmp) / "state.json",
                guild_id="guild", interval=0.01, timeout=0.01, once=True,
                reporter=reporter,
            )
            failed = json.loads(health_path.read_text(encoding="utf-8"))
            check("REST failure publishes degraded health",
                  failed["status"] == "degraded"
                  and failed["consecutive_failures"] == 1
                  and "Discord edge offline" in failed["error"])
            check("health output never persists the bot token",
                  "secret-token" not in health_path.read_text(encoding="utf-8"))

            watcher.poll_once = lambda *_a, **_k: [{"message_id": "1"}]  # type: ignore[assignment]
            watcher.run_watcher(
                "secret-token", "channel", state_path=Path(tmp) / "state.json",
                guild_id="guild", interval=0.01, timeout=0.01, once=True,
                reporter=reporter,
            )
            ready = json.loads(health_path.read_text(encoding="utf-8"))
            check("REST recovery clears degraded health",
                  ready["status"] == "ready"
                  and ready["consecutive_failures"] == 0
                  and ready["pending_count"] == 1)
        finally:
            watcher.poll_once = original_poll

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if RESULTS and passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
