#!/usr/bin/env python3
"""Tests for the Mac-side Hermes agent watcher.

The watcher is the only thing standing between the probe on the Hetzner box and
the connector on the Mac, so a mistake here silently empties keys rather than
raising.  These tests pin the argv it builds, the state it publishes, and the
last-good-state behaviour on failure.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import hermes_agents_watcher as watcher
from connection_runtime import HealthReporter


RESULTS: list[tuple[str, bool]] = []


class FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def info(self, message: str, *args: object) -> None:
        self.events.append(("INFO", message % args))

    def debug(self, message: str, *args: object) -> None:
        self.events.append(("DEBUG", message % args))


def check(name: str, condition: bool, detail: str = "") -> None:
    ok = bool(condition)
    RESULTS.append((name, ok))
    suffix = "" if ok or not detail else f": {detail}"
    print(f"{'PASS' if ok else 'FAIL'} {name}{suffix}")


def main() -> int:
    local = watcher.parse_args(["--local"])
    local_cmd = watcher.build_command(local)

    # A forced default source is the specific regression that hid every
    # ssh-hosted Hermes agent: the probe defaults to discord+cli+tui, so the
    # watcher must stay silent about --source unless asked.
    check("default local argv passes no --source", "--source" not in local_cmd,
          " ".join(local_cmd))
    check("default local argv targets the probe",
          local_cmd[1].endswith("hermes_agents_probe.py"), " ".join(local_cmd))
    check("default local argv uses this interpreter", local_cmd[0] == sys.executable)
    check("default limit fills all ten agent slots",
          "--limit" in local_cmd and local_cmd[local_cmd.index("--limit") + 1] == "10",
          " ".join(local_cmd))
    check("default argv is active-only", "--all" not in local_cmd)

    ssh = watcher.parse_args(
        ["--ssh", "hetzner", "--source", "cli", "--source", "tui", "--all",
         "--ssh-opt=-oBatchMode=yes"]
    )
    ssh_cmd = watcher.build_command(ssh)
    check("ssh argv starts with ssh and the host",
          ssh_cmd[0] == "ssh" and "hetzner" in ssh_cmd, " ".join(ssh_cmd))
    check("ssh polls are noninteractive and fail transport quickly",
          "-oBatchMode=yes" in ssh_cmd
          and any(part.startswith("-oConnectTimeout=") for part in ssh_cmd)
          and "-oConnectionAttempts=1" in ssh_cmd,
          " ".join(ssh_cmd))
    check("ssh polls reuse one durable transport instead of retriggering auth",
          "-oControlMaster=auto" in ssh_cmd
          and any(part.startswith("-oControlPersist=") for part in ssh_cmd)
          and any(part.startswith("-oControlPath=") for part in ssh_cmd),
          " ".join(ssh_cmd))
    check("ssh argv keeps extra ssh options", "-oBatchMode=yes" in ssh_cmd)
    check("repeated --source is forwarded once per value",
          [ssh_cmd[i + 1] for i, a in enumerate(ssh_cmd) if a == "--source"]
          == ["cli", "tui"], " ".join(ssh_cmd))
    check("--all is forwarded to the probe", "--all" in ssh_cmd)
    check("ssh argv runs the remote probe path",
          watcher.DEFAULT_REMOTE_PROBE in ssh_cmd, " ".join(ssh_cmd))
    check("ssh argv is shell-free",
          not any(any(c in part for c in ";|&") for part in ssh_cmd), " ".join(ssh_cmd))

    annotator = getattr(watcher, "annotate_ssh_host", None)
    routed = ({"agents": []} if annotator is None else annotator(
        {"agents": [
            {"source": "hermes-ssh", "name": "remote"},
            {"source": "hermes-discord", "name": "thread"},
        ]},
        "hetzner",
    ))
    check("SSH watcher records the local host alias on terminal sessions",
          routed.get("agents", [{}])[0].get("ssh_host") == "hetzner"
          if routed.get("agents") else False,
          str(routed))
    check("Discord sessions do not inherit an SSH-pane route",
          len(routed.get("agents", [])) == 2
          and not routed["agents"][1].get("ssh_host"), str(routed))

    # The probe will not invent a Discord URL without a guild id. When the
    # Mac-side watcher is started without --guild-id (the common case: the
    # token and guild live only on the Hermes host), it must recover the
    # guild from the same remote .env the approval watcher already reads.
    ssh_no_guild = watcher.parse_args(["--ssh", "hermes"])
    check("an unconfigured SSH watcher does not yet invent a guild flag",
          "--guild-id" not in watcher.build_command(ssh_no_guild),
          " ".join(watcher.build_command(ssh_no_guild)))
    resolve = getattr(watcher, "resolve_guild_id", None)
    fill = getattr(watcher, "fill_discord_urls", None)
    original_fetch = getattr(watcher, "fetch_remote_discord_config", None)
    if resolve is not None:
        watcher.fetch_remote_discord_config = (  # type: ignore[assignment]
            lambda _host, **_kwargs: {"DISCORD_GUILD_ID": "1507988913527062618"}
        )
        ssh_no_guild.apps_config = Path("/no/such/apps.json")
        try:
            guild = resolve(ssh_no_guild)
            ssh_no_guild.guild_id = guild
            resolved_cmd = watcher.build_command(ssh_no_guild)
        finally:
            if original_fetch is None:
                delattr(watcher, "fetch_remote_discord_config")
            else:
                watcher.fetch_remote_discord_config = original_fetch
        check("SSH watcher discovers DISCORD_GUILD_ID from the remote Hermes env",
              guild == "1507988913527062618", str(guild))
        check("discovered guild is forwarded to the remote probe",
              "--guild-id" in resolved_cmd
              and resolved_cmd[resolved_cmd.index("--guild-id") + 1]
              == "1507988913527062618",
              " ".join(resolved_cmd))
    else:
        check("SSH watcher discovers DISCORD_GUILD_ID from the remote Hermes env",
              False, "resolve_guild_id is missing")
        check("discovered guild is forwarded to the remote probe", False)

    apps_guild = getattr(watcher, "guild_id_from_apps_config", None)
    if apps_guild is None:
        check("watcher reads a guild from the Hermes launcher URL", False)
    else:
        with tempfile.TemporaryDirectory(prefix="deckbridge-guild-apps-") as tmp:
            apps = Path(tmp) / "apps.json"
            apps.write_text(
                '{"launchers":[{"label":"Hermes","source":"hermes-discord",'
                '"bundle":"Discord",'
                '"url":"discord://-/channels/1507988913527062618/1"}]}',
                encoding="utf-8",
            )
            check("watcher reads a guild from the Hermes launcher URL",
                  apps_guild(apps) == "1507988913527062618",
                  apps_guild(apps))

    if fill is None:
        check("watcher fills a missing Discord jump URL after a guild-less probe",
              False, "fill_discord_urls is missing")
        check("watcher never invents a Discord URL for an ssh-hosted agent", False)
        check("watcher leaves an already-correct Discord URL alone", False)
    else:
        filled = fill(
            {"agents": [
                {"source": "hermes-discord", "thread_id": "1545901043475419207",
                 "url": ""},
                {"source": "hermes-ssh", "thread_id": "", "url": ""},
                {"source": "hermes-discord", "thread_id": "1",
                 "url": "https://discord.com/channels/already/1"},
            ]},
            "1507988913527062618",
        )
        check("watcher fills a missing Discord jump URL after a guild-less probe",
              filled.get("agents", [{}])[0].get("url")
              == "https://discord.com/channels/1507988913527062618/1545901043475419207",
              str(filled))
        check("watcher never invents a Discord URL for an ssh-hosted agent",
              filled.get("agents", [{}, {}])[1].get("url") == "", str(filled))
        check("watcher leaves an already-correct Discord URL alone",
              filled.get("agents", [{}, {}, {}])[2].get("url")
              == "https://discord.com/channels/already/1", str(filled))

    fake_log = FakeLog()
    original_log = watcher.LOG
    watcher.LOG = fake_log  # type: ignore[assignment]
    try:
        previous = watcher._log_poll_result(0, None, "/tmp/state.json")
        previous = watcher._log_poll_result(0, previous, "/tmp/state.json")
        watcher._log_poll_result(2, previous, "/tmp/state.json")
    finally:
        watcher.LOG = original_log
    check(
        "steady watcher polls are debug-only while startup and changes stay visible",
        [level for level, _ in fake_log.events] == ["INFO", "DEBUG", "INFO"],
        str(fake_log.events),
    )
    check(
        "agent count transition is named in the info log",
        "changed 0 -> 2" in fake_log.events[-1][1],
        str(fake_log.events),
    )

    with tempfile.TemporaryDirectory(prefix="deckbridge-watcher-test-") as tmp:
        out = Path(tmp) / "nested" / "hermes_agents.json"
        document = {"agents": [{"name": "sample api", "status": "working",
                                "source": "hermes-ssh"}]}
        watcher.write_atomic(out, document)
        check("write_atomic creates missing parent directories", out.exists())
        check("write_atomic round-trips the document",
              json.loads(out.read_text(encoding="utf-8")) == document)
        check("write_atomic leaves no temporary files behind",
              [p.name for p in out.parent.iterdir()] == [out.name],
              str([p.name for p in out.parent.iterdir()]))

        # A failing probe must leave the previous board intact: blanking every
        # key because one poll failed is worse than showing slightly old state.
        # Forced through a nonexistent command so the test touches no network.
        broken = watcher.parse_args(["--local", "--out", str(out)])
        health_path = Path(tmp) / "health" / "hermes_agents.json"
        reporter = HealthReporter(
            "hermes_agents", path=health_path, stale_after=20,
        )
        original = watcher.build_command
        watcher.build_command = lambda _args: [  # type: ignore[assignment]
            str(Path(tmp) / "definitely_not_a_command")
        ]
        try:
            result = watcher.poll_once(broken, reporter=reporter)
        finally:
            watcher.build_command = original  # type: ignore[assignment]
        check("failed poll reports no document", result is None)
        check("failed poll preserves the last good state",
              json.loads(out.read_text(encoding="utf-8")) == document)
        failed_health = json.loads(health_path.read_text(encoding="utf-8"))
        check("failed poll publishes degraded transport health",
              failed_health["status"] == "degraded"
              and failed_health["consecutive_failures"] == 1
              and failed_health["error"])

        original_run_probe = watcher.run_probe
        watcher.run_probe = lambda _args: document  # type: ignore[assignment]
        try:
            recovered = watcher.poll_once(broken, reporter=reporter)
        finally:
            watcher.run_probe = original_run_probe
        recovered_health = json.loads(health_path.read_text(encoding="utf-8"))
        check("successful poll clears degraded health",
              recovered == document
              and recovered_health["status"] == "ready"
              and recovered_health["consecutive_failures"] == 0
              and recovered_health["agent_count"] == 1)

        noisy = {
            "agents": [
                {"name": "Return PONG", "title": "Return PONG",
                 "status": "done", "source": "hermes-ssh", "cwd": "/tmp"},
                {"name": "real work", "title": "Fix the board",
                 "status": "working", "source": "hermes-discord"},
            ]
        }
        watcher.run_probe = lambda _args: noisy  # type: ignore[assignment]
        try:
            cleaned = watcher.poll_once(broken, reporter=reporter)
        finally:
            watcher.run_probe = original_run_probe
        names = [a.get("name") for a in (cleaned or {}).get("agents", [])]
        check("watcher drops Return PONG even if the remote probe still emits it",
              names == ["real work"], str(names))

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
