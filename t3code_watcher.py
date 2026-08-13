#!/usr/bin/env python3
"""Publish authoritative T3 Code thread state for Deckbridge.

The official desktop app exposes a loopback, bearer-authenticated shell API.
This watcher deliberately rereads its runtime descriptor on every poll: T3
chooses a fresh port after restarts, so caching the first URL makes a boot-time
integration look healthy until the first app update or crash.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from connection_runtime import HealthReporter

log = logging.getLogger("t3code_watcher")

DEFAULT_RUNTIME = "~/.t3/userdata/server-runtime.json"
DEFAULT_TOKEN = "~/.deckbridge/t3code_token"
DEFAULT_STATE = "~/.deckbridge/t3code_agents.json"
DEFAULT_INTERVAL = 0.75

PROVIDER_SOURCE = {
    "claudeagent": "t3code-claude",
    "codex": "t3code-codex",
    "cursor": "t3code-cursor",
    "grok": "t3code-grok",
    "opencode": "t3code-opencode",
}


def iso_epoch(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def provider_source(thread: dict[str, Any]) -> str:
    selection = thread.get("modelSelection") or {}
    session = thread.get("session") or {}
    raw = (selection.get("instanceId") or session.get("providerInstanceId")
           or session.get("providerName") or "")
    key = str(raw).replace("-", "").replace("_", "").lower()
    for prefix, source in PROVIDER_SOURCE.items():
        if key.startswith(prefix):
            return source
    return "t3code"


def thread_status(thread: dict[str, Any]) -> str:
    """Map T3's explicit interaction/lifecycle fields to Deckbridge state."""
    if (thread.get("hasPendingApprovals") or thread.get("hasPendingUserInput")
            or thread.get("hasActionableProposedPlan")):
        return "blocked"
    session = thread.get("session") or {}
    latest = thread.get("latestTurn") or {}
    liveness = thread.get("backgroundLiveness")
    if session.get("status") in ("starting", "running"):
        return "working"
    if latest.get("state") == "running" or liveness in ("working", "monitoring"):
        return "working"
    if session.get("status") == "error" or latest.get("state") == "error":
        return "blocked"
    if latest.get("state") in ("completed", "interrupted"):
        return "done"
    return "idle"


def snapshot_agents(payload: dict[str, Any], origin: str, environment_id: str) -> list[dict[str, Any]]:
    projects = {
        str(project.get("id")): project
        for project in payload.get("projects", []) if isinstance(project, dict)
    }
    agents: list[dict[str, Any]] = []
    for thread in payload.get("threads", []):
        if not isinstance(thread, dict) or thread.get("archivedAt"):
            continue
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            continue
        project = projects.get(str(thread.get("projectId")), {})
        latest = thread.get("latestTurn") or {}
        session = thread.get("session") or {}
        updated = (latest.get("completedAt") or latest.get("startedAt")
                   or session.get("updatedAt") or thread.get("updatedAt"))
        route = f"/{environment_id}/{thread_id}"
        agents.append({
            "name": str(thread.get("title") or "New thread"),
            "status": thread_status(thread),
            "source": provider_source(thread),
            "session_id": thread_id,
            "thread_id": thread_id,
            "cwd": str(project.get("workspaceRoot") or thread.get("worktreePath") or ""),
            "app": "T3 Code (Alpha)",
            "url": f"t3code://app/#{route}",
            "web_url": origin.rstrip("/") + route,
            "updated_at": iso_epoch(updated),
            "activity": str(latest.get("state") or session.get("status") or ""),
        })
    return agents


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class T3CodeWatcher:
    def __init__(self, runtime: Path, token: Path, state: Path,
                 interval: float = DEFAULT_INTERVAL,
                 health: HealthReporter | None = None) -> None:
        self.runtime = runtime.expanduser()
        self.token = token.expanduser()
        self.state = state.expanduser()
        self.interval = interval
        self.health = health

    def endpoint(self) -> tuple[str, str]:
        doc = json.loads(self.runtime.read_text(encoding="utf-8"))
        origin = str(doc.get("origin") or "")
        parsed = urllib.parse.urlparse(origin)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("T3 runtime origin is not a local HTTP endpoint")
        environment = str(doc.get("environmentId") or doc.get("environment_id") or "")
        return origin.rstrip("/") + "/api/orchestration/shell", environment

    def credential(self) -> str:
        mode = stat.S_IMODE(self.token.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(f"T3 token must not be group/world-readable (mode {mode:o})")
        token = self.token.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("T3 token is empty")
        return token

    def poll_once(self) -> list[dict[str, Any]]:
        endpoint, environment = self.endpoint()
        origin = endpoint.removesuffix("/api/orchestration/shell")
        if not environment:
            with urllib.request.urlopen(
                    origin + "/.well-known/t3/environment", timeout=5) as response:
                descriptor = json.load(response)
            environment = str(descriptor.get("environmentId") or "")
        if not environment:
            raise ValueError("T3 environment descriptor has no environmentId")
        request = urllib.request.Request(
            endpoint, headers={"Authorization": "Bearer " + self.credential()},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        agents = snapshot_agents(payload, origin, environment)
        atomic_write(self.state, {"agents": agents, "updated_at": time.time()})
        if self.health:
            self.health.ready(detail=f"{len(agents)} T3 thread(s)")
        return agents

    def run(self) -> None:
        failures = 0
        while True:
            try:
                self.poll_once()
                failures = 0
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
                failures += 1
                if self.health:
                    self.health.degraded(str(exc))
                if failures == 1 or failures % 10 == 0:
                    subprocess.run(
                        ["/usr/bin/open", "-gj", "-a", "T3 Code (Alpha)"],
                        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                log.warning("T3 Code unavailable: %s", exc)
            time.sleep(self.interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    watcher = T3CodeWatcher(
        Path(args.runtime), Path(args.token), Path(args.state), args.interval,
        HealthReporter("t3code_watcher", stale_after=20.0),
    )
    if args.once:
        print(json.dumps(watcher.poll_once(), indent=2))
        return 0
    watcher.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
