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

    with tempfile.TemporaryDirectory(prefix="deckbridge-discord-threads-") as tmp:
        agents_path = Path(tmp) / "hermes_agents.json"
        agents_path.write_text(json.dumps({
            "agents": [
                {"thread_id": "1549244444111405178", "source": "hermes-discord"},
                {"thread_id": "1549244444111405178", "source": "hermes-discord"},
                {"thread_id": "", "source": "hermes-ssh"},
                {"name": "no thread"},
            ],
        }), encoding="utf-8")
        check("agent feed yields unique Discord thread ids",
              watcher.thread_ids_from_agents(agents_path)
              == ["1549244444111405178"])
        check("a missing agent feed yields no thread ids",
              watcher.thread_ids_from_agents(Path(tmp) / "missing.json") == [])

    def card(disabled: bool, footer: str = "") -> dict:
        embed = {"type": "rich",
                 "title": "\u26a0\ufe0f Hermes wants to run a command that needs your OK"}
        if footer:
            embed["footer"] = {"text": footer}
        return {
            "id": "1553000000000000001", "timestamp": "2026-09-24T23:19:10+00:00",
            "content": ("\u26a0\ufe0f **Hermes wants to run a command that needs your OK**\n\n"
                        "**Requested command:**\n```bash\ncurl -s x | python3\n```\n"
                        "**Why it was flagged:** Pipe to interpreter\n"),
            "embeds": [embed],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 3, "label": "Allow Once", "disabled": disabled},
                {"type": 2, "style": 4, "label": "Deny", "disabled": disabled},
            ]}],
        }
    record = watcher.approval_from_message(card(False), channel_id="42", guild_id="1")
    check("the renamed 'needs your OK' card is a pending approval",
          record is not None and record["command"] == "curl -s x | python3"
          and record["reason"] == "Pipe to interpreter", str(record))
    check("an answered card is not pending",
          watcher.approval_from_message(card(True), channel_id="42") is None)
    check("an expired card is not pending",
          watcher.approval_from_message(
              card(False, "\u23f1 Prompt expired \u2014 no action taken"),
              channel_id="42") is None)

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if RESULTS and passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
