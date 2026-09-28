"""Prove whether a local agent process, or its remote SSH pane, still exists."""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from agent_feeds import STALE_WORKING_S, agent_key

LIVENESS_CACHE_S = 5.0
LOCAL_SESSION_SOURCES = frozenset({"claude-code", "codex-cli", "cursor-agent"})


class LocalLivenessProbe:
    """Bounded, cached proof of whether a local Claude/Codex process exists.

    PID is checked immediately because the hook recorded the owning process
    itself. Older pre-upgrade records have no PID; only once their heartbeat is
    stale do we consult exact tty/surface/Herdr handles. Unknown is different
    from dead: missing tools or unrecognised host metadata return ``None`` and
    preserve timestamp fallback rather than falsely evicting a session.
    """

    def __init__(self, cache_seconds: float = LIVENESS_CACHE_S) -> None:
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._cache: dict[str, tuple[float, tuple[object, ...], bool | None]] = {}
        self._cmux_cache: tuple[float, dict[str, str] | None] = (0.0, None)

    @staticmethod
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=1.5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _expected_process(command: str, source: str) -> bool:
        text = command.lower()
        if any(name in text for name in (
                "agent_shim.py", "claude_shim.py", "codex_shim.py",
                "cursor_shim.py")):
            return False
        needles = {
            "claude-code": ("claude",),
            "codex-cli": ("codex",),
            # Current Cursor CLI uses `agent`; cursor-agent remains its
            # backwards-compatible name. IDE sessions usually have no
            # per-conversation PID and stay timestamp driven instead.
            "cursor-agent": ("agent", "cursor-agent", "cursor"),
        }.get(source, ())
        return any(os.path.basename(token.rstrip("/")) in needles
                   for token in text.replace("=", " ").split()) \
            or any(f"/{needle} " in text or f"/{needle}" == text.rstrip()
                   for needle in needles)

    def _pid_liveness(self, agent: dict[str, Any]) -> bool | None:
        raw = agent.get("agent_pid")
        if raw in (None, ""):
            return None
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            return False
        if pid <= 1:
            return False
        result = self._run(["ps", "-p", str(pid), "-o", "command="])
        if result is None:
            return None
        command = result.stdout.strip()
        if result.returncode != 0 or not command:
            return False
        if not self._expected_process(command, str(agent.get("source") or "")):
            return False
        expected_start = str(agent.get("agent_started_at") or "")
        if expected_start:
            started = self._run(["ps", "-p", str(pid), "-o", "lstart="])
            if started is None:
                return None
            if started.returncode != 0 or started.stdout.strip() != expected_start:
                return False
        return True

    def _tty_liveness(self, agent: dict[str, Any], tty: str) -> bool | None:
        tty = str(tty or "").strip().removeprefix("/dev/")
        if not tty:
            return None
        result = self._run(["ps", "-t", tty, "-o", "command="])
        if result is None:
            return None
        commands = result.stdout.strip()
        if result.returncode != 0 or not commands:
            return False
        return self._expected_process(commands, str(agent.get("source") or ""))

    def _cmux_surfaces(self, now: float) -> dict[str, str] | None:
        expires, cached = self._cmux_cache
        if now < expires:
            return cached
        result = self._run(["cmux", "--id-format", "both", "tree", "--all", "--json"])
        surfaces: dict[str, str] | None = None
        if result is not None and result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                surfaces = {}

                def walk(value: object) -> None:
                    if isinstance(value, dict):
                        surface_id = str(value.get("id") or "")
                        if surface_id and value.get("tty"):
                            surfaces[surface_id] = str(value["tty"])
                        for child in value.values():
                            walk(child)
                    elif isinstance(value, list):
                        for child in value:
                            walk(child)

                walk(data)
            except (TypeError, ValueError):
                surfaces = None
        self._cmux_cache = (now + self.cache_seconds, surfaces)
        return surfaces

    def _legacy_handle_liveness(
        self, agent: dict[str, Any], now: float,
    ) -> bool | None:
        verdicts: list[bool | None] = []
        tty = str(agent.get("tty") or "")
        if tty:
            verdicts.append(self._tty_liveness(agent, tty))

        surface = str(agent.get("surface") or "")
        if surface:
            surfaces = self._cmux_surfaces(now)
            if surfaces is None:
                verdicts.append(None)
            elif surface not in surfaces:
                verdicts.append(False)
            else:
                verdicts.append(self._tty_liveness(agent, surfaces[surface]))

        pane = str(agent.get("herdr_pane") or "")
        if pane:
            result = self._run(["herdr", "pane", "get", pane])
            if result is None:
                verdicts.append(None)
            else:
                try:
                    data = json.loads(result.stdout)
                except ValueError:
                    verdicts.append(None if result.returncode == 0 else False)
                else:
                    if isinstance(data, dict) and data.get("error"):
                        error = data.get("error")
                        if (isinstance(error, dict)
                                and error.get("code") == "pane_not_found"):
                            verdicts.append(False)
                        else:
                            verdicts.append(None)
                    else:
                        pane_data = data.get("result", {}).get("pane", {}) \
                            if isinstance(data, dict) else {}
                        status = str(pane_data.get("agent_status") or "").lower()
                        verdicts.append(
                            True if status and status != "unknown" else None)

        # Handle metadata is sticky across hooks because a detached hook may be
        # unable to rediscover it.  Consequently one old handle can coexist
        # with a newer exact one.  A single proven-live route wins; a session is
        # dead only when every available probe conclusively says so.
        if True in verdicts:
            return True
        if verdicts and all(verdict is False for verdict in verdicts):
            return False
        return None

    def __call__(self, agent: dict[str, Any]) -> bool | None:
        if agent.get("source") not in LOCAL_SESSION_SOURCES:
            return None
        now = time.monotonic()
        token = tuple(agent.get(field) for field in (
            "agent_pid", "agent_started_at", "tty", "surface", "herdr_pane",
            "updated_at",
        ))
        key = agent_key(agent)
        cached = self._cache.get(key)
        if cached and now < cached[0] and token == cached[1]:
            return cached[2]

        verdict = self._pid_liveness(agent)
        if verdict is None:
            stamp = float(agent.get("updated_at") or 0.0)
            # Fresh legacy hooks remain timestamp-driven. This avoids turning
            # a transient CLI/permission failure into a false-dead key.
            if stamp and time.time() - stamp <= STALE_WORKING_S:
                verdict = None
            else:
                verdict = self._legacy_handle_liveness(agent, now)
        self._cache[key] = (now + self.cache_seconds, token, verdict)
        return verdict


class HerdrSshPaneResolver:
    """Conservatively map a remote Hermes record to its local Herdr SSH pane.

    A remote Hermes session id names a database row on the SSH host; a Herdr
    pane id names the visible terminal on this Mac.  They are intentionally
    different namespaces.  The bridge is safe only when one single-pane Herdr
    tab is running ``ssh <the watcher alias>`` and exactly one relevant remote
    agent can own it. Ambiguity yields no route rather than the wrong tab.
    """

    SSH_OPTIONS_WITH_VALUE = frozenset({
        "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i",
        "-J", "-L", "-l", "-m", "-O", "-o", "-P", "-p", "-Q",
        "-R", "-S", "-W", "-w",
    })

    def __init__(
        self, *, runner: Any = subprocess.run,
        herdr_bin: str = "herdr", cache_seconds: float = LIVENESS_CACHE_S,
    ) -> None:
        self.runner = runner
        self.herdr_bin = herdr_bin
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._cache: tuple[float, list[dict[str, str]]] = (0.0, [])

    def _run_json(self, argv: list[str]) -> dict[str, Any] | None:
        try:
            result = self.runner(
                argv, capture_output=True, text=True, timeout=1.5, check=False,
            )
            if result.returncode != 0:
                return None
            value = json.loads(result.stdout)
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _normal_host(value: object) -> str:
        host = str(value or "").strip().casefold()
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        return host.strip("[]")

    @classmethod
    def _ssh_target(cls, argv: object) -> str:
        if not isinstance(argv, list) or not argv:
            return ""
        if os.path.basename(str(argv[0])) != "ssh":
            return ""
        index = 1
        while index < len(argv):
            token = str(argv[index])
            if token == "--":
                index += 1
                break
            if not token.startswith("-") or token == "-":
                break
            if token in cls.SSH_OPTIONS_WITH_VALUE:
                index += 2
            else:
                index += 1
        if index >= len(argv):
            return ""
        return cls._normal_host(argv[index])

    def _discover(self) -> list[dict[str, str]]:
        now = time.monotonic()
        expires, cached = self._cache
        if now < expires:
            return cached
        document = self._run_json([self.herdr_bin, "pane", "list"])
        raw = ((document or {}).get("result") or {}).get("panes") or []
        panes = [item for item in raw if isinstance(item, dict)]
        per_tab: dict[str, int] = {}
        for pane in panes:
            tab = str(pane.get("tab_id") or "")
            if tab:
                per_tab[tab] = per_tab.get(tab, 0) + 1

        routes: list[dict[str, str]] = []
        for pane in panes:
            # A pane already owned by a local Herdr agent cannot simultaneously
            # be the raw SSH viewer for a remote Hermes session.
            if pane.get("agent"):
                continue
            pane_id = str(pane.get("pane_id") or "")
            tab_id = str(pane.get("tab_id") or "")
            workspace_id = str(pane.get("workspace_id") or "")
            # Workspace + tab focus can select the exact pane only when that
            # tab contains one pane. Split-pane ambiguity must remain unfocused.
            if not pane_id or not tab_id or not workspace_id or per_tab.get(tab_id) != 1:
                continue
            info = self._run_json([
                self.herdr_bin, "pane", "process-info", "--pane", pane_id,
            ])
            process_info = ((info or {}).get("result") or {}).get("process_info") or {}
            processes = process_info.get("foreground_processes") or []
            hosts = {
                self._ssh_target(process.get("argv"))
                for process in processes if isinstance(process, dict)
            }
            hosts.discard("")
            if len(hosts) == 1:
                routes.append({
                    "ssh_host": next(iter(hosts)),
                    "herdr_pane": pane_id,
                    "herdr_tab": tab_id,
                    "herdr_workspace": workspace_id,
                })
        self._cache = (now + self.cache_seconds, routes)
        return routes

    def enrich(self, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = [dict(agent) for agent in agents]
        eligible = [
            agent for agent in out
            if agent.get("source") == "hermes-ssh"
            and agent.get("ssh_host") and not agent.get("herdr_pane")
        ]
        if not eligible:
            return out
        routes = self._discover()
        hosts = {self._normal_host(agent.get("ssh_host")) for agent in eligible}
        for host in hosts:
            host_agents = [
                agent for agent in eligible
                if self._normal_host(agent.get("ssh_host")) == host
            ]
            candidates = [route for route in routes if route["ssh_host"] == host]
            if len(candidates) != 1:
                continue
            if len(host_agents) == 1:
                owner = host_agents[0]
            else:
                active = [
                    agent for agent in host_agents
                    if agent.get("status") in {"working", "blocked"}
                ]
                if len(active) != 1:
                    continue
                owner = active[0]
            owner.update(candidates[0])
        return out
