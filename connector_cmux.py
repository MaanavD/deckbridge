#!/usr/bin/env python3
"""Connector B: expose cmux agent state on a deckbridge Stream Deck zone.

The real cmux API does not currently provide a documented, stable endpoint for
listing all agents and their live statuses.  A small cmux-side shim therefore
writes a local state file, and this connector polls that file:

    ~/.deckbridge/cmux_state.json

The file contract is::

    {
      "agents": [
        {"name": "codex", "status": "working", "cwd": "/path/to/repo"},
        {"name": "claude", "status": "blocked", "cwd": "/path/to/repo"}
      ]
    }

``status`` is one of ``working``, ``blocked``, ``done``, or ``idle``.  See
``cmux_shim.sh`` for a dependency-free writer suitable for a cmux notification
command/status hook.  The connector has no cmux or macOS dependency and can be
run against a local deckd emulator or test hub.

By default, the first key in the inclusive claimed range is a summary key.  The
remaining keys show agents in file order; if the range cannot show every agent,
its last key is an overflow indicator.  ``--no-summary`` makes every claimed
key an agent slot, which is useful for the five-key default zone.  Pressing an
agent key runs the configurable focus command template with ``{name}`` (and
optionally ``{cwd}``) substituted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Iterable

import websockets

from connection_runtime import HealthReporter, RetryPolicy, reconnect_forever

log = logging.getLogger("connector_cmux")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8777
DEFAULT_CLAIM = (5, 9)
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_STATE_PATH = "~/.deckbridge/cmux_state.json"

STATUS_ORDER = {"idle": 0, "done": 1, "working": 2, "blocked": 3}
STATUS_FACE = {
    "blocked": {"color": "#c0392b", "effect": "breathe", "icon": "alert"},
    "working": {"color": "#d9822b", "effect": "solid", "icon": "agent"},
    "done": {"color": "#2e6fdb", "effect": "solid", "icon": "check"},
    "idle": {"color": "#1f8a4c", "effect": "solid", "icon": "agent"},
}
OFF_FACE = {
    "label": "",
    "sublabel": "",
    "color": "#111111",
    "icon": None,
    "effect": "off",
}


class CmuxConnector:
    """Poll cmux shim state and paint one inclusive deckd key range."""

    def __init__(
        self,
        url: str = f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}",
        state_path: str | os.PathLike[str] = DEFAULT_STATE_PATH,
        claim: tuple[int, int] = DEFAULT_CLAIM,
        focus_cmd: str = "cmux focus {name}",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        summary: bool = True,
        health: HealthReporter | None = None,
    ) -> None:
        first, last = int(claim[0]), int(claim[1])
        if first < 0 or first > last:
            raise ValueError(f"invalid inclusive claim {first}..{last}")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.url = url
        self.state_path = Path(os.path.expanduser(os.fspath(state_path)))
        self.claim = (first, last)
        self.focus_cmd = focus_cmd
        self.poll_interval = poll_interval
        self.summary = bool(summary)
        self.health = health

        self.ws: Any = None
        self.ready = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._watcher: asyncio.Task[None] | None = None
        self._state_stamp: tuple[int, int] | None = None
        self._agents: list[dict[str, str]] = []
        self._agent_keys: dict[int, dict[str, str]] = {}

    # ---- state file and face layout -------------------------------------
    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.state_path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.debug("cannot stat %s: %s", self.state_path, exc)
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _read_agents(self) -> list[dict[str, str]] | None:
        """Read and normalize the shim file; None means retry on next poll."""
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            document = json.loads(raw)
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            # The shim replaces atomically, but keeping the last good state is
            # safer if a user edits the file by hand or a write is interrupted.
            log.warning("cannot read cmux state %s: %s", self.state_path, exc)
            return None

        if not isinstance(document, dict):
            log.warning("cmux state root must be an object")
            return None
        raw_agents = document.get("agents", [])
        if not isinstance(raw_agents, list):
            log.warning("cmux state agents must be a list")
            return None

        agents: list[dict[str, str]] = []
        for item in raw_agents:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            status = self._normalize_status(item.get("status", "idle"))
            cwd = str(item.get("cwd", ""))
            agents.append({"name": name, "status": status, "cwd": cwd})
        return agents

    @staticmethod
    def _normalize_status(status: object) -> str:
        value = str(status).strip().lower().replace("-", " ").replace("_", " ")
        if value in {"blocked", "waiting", "needs input", "needs you", "error"}:
            return "blocked"
        if value in {"working", "running"}:
            return "working"
        if value in {"done", "complete", "completed"}:
            return "done"
        return "idle"

    @staticmethod
    def _face_for_status(status: str, label: str, sublabel: str) -> dict[str, Any]:
        style = STATUS_FACE[status]
        return {
            "label": label[:8],
            "sublabel": sublabel[:16],
            "color": style["color"],
            "icon": style["icon"],
            "effect": style["effect"],
        }

    def _build_faces(self, agents: Iterable[dict[str, str]]) -> dict[int, dict[str, Any]]:
        """Build every face in the claim and map pressable keys to agents."""
        first, last = self.claim
        faces = {index: dict(OFF_FACE) for index in range(first, last + 1)}
        self._agent_keys = {}
        agent_list = list(agents)

        if not agent_list:
            return faces

        if not self.summary:
            for index, agent in zip(range(first, last + 1), agent_list):
                self._agent_keys[index] = agent
                faces[index] = self._face_for_status(
                    agent["status"], agent["name"], agent["status"]
                )
            return faces

        worst = max(agent_list, key=lambda agent: STATUS_ORDER[agent["status"]])
        worst_status = worst["status"]
        summary_label = {
            "blocked": "NEEDS?",
            "working": "WORKING",
            "done": "DONE",
            "idle": "IDLE",
        }[worst_status]
        faces[first] = self._face_for_status(worst_status, summary_label, worst_status)

        # One key is reserved for the summary.  If an overflow indicator is
        # needed, reserve the final key for it before assigning agent keys.
        available = max(0, last - first)
        overflow = len(agent_list) > available and available > 0
        shown_count = available - 1 if overflow else available
        shown_count = max(0, shown_count)

        for offset, agent in enumerate(agent_list[:shown_count], start=1):
            index = first + offset
            self._agent_keys[index] = agent
            faces[index] = self._face_for_status(
                agent["status"], agent["name"], agent["status"]
            )

        if overflow:
            hidden = len(agent_list) - shown_count
            faces[last] = self._face_for_status(
                worst_status, f"+{hidden}", "overflow"
            )
        return faces

    async def refresh(self, force: bool = False) -> bool:
        """Paint current state when the file changed; return whether painted."""
        stamp = self._file_stamp()
        if not force and stamp == self._state_stamp:
            return False
        agents = self._read_agents()
        if agents is None:
            return False
        self._state_stamp = stamp
        self._agents = agents
        faces = self._build_faces(agents)
        payload = {
            "type": "faces",
            "faces": [dict(face, index=index) for index, face in faces.items()],
        }
        await self._send(payload)
        return True

    # ---- websocket protocol ---------------------------------------------
    async def _send(self, message: dict[str, Any]) -> None:
        if self.ws is None:
            return
        async with self._send_lock:
            await self.ws.send(json.dumps(message))

    async def _wait_for_welcome(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = message.get("type")
            if kind == "welcome":
                returned_claim = message.get("claim")
                if isinstance(returned_claim, list) and len(returned_claim) == 2:
                    self.claim = (int(returned_claim[0]), int(returned_claim[1]))
                self.ready.set()
                return
            if kind == "error":
                raise RuntimeError(message.get("detail") or message.get("reason", "deckd error"))

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.refresh()
                if self.health is not None:
                    self.health.heartbeat(5.0, transport="websocket", peer=self.url)
            except (OSError, websockets.ConnectionClosed):
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cmux refresh failed")

    def _focus_agent(self, agent: dict[str, str]) -> None:
        """Run the configured focus template without making it a hard failure."""
        try:
            command = self.focus_cmd.format(
                name=shlex.quote(agent["name"]),
                cwd=shlex.quote(agent.get("cwd", "")),
            )
        except (KeyError, ValueError) as exc:
            log.warning("invalid focus command template %r: %s", self.focus_cmd, exc)
            return
        try:
            subprocess.run(
                command,
                shell=True,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("focus command failed for %s: %s", agent["name"], exc)

    async def _handle_message(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        if message.get("type") != "press":
            # Releases, pongs, warnings, and unknown frames require no reply.
            return
        try:
            index = int(message["index"])
        except (KeyError, TypeError, ValueError):
            return
        agent = self._agent_keys.get(index)
        if agent is not None:
            await asyncio.to_thread(self._focus_agent, agent)

    async def _run_connection(self) -> None:
        """Connect to deckd and run until the websocket closes or is cancelled."""
        async with websockets.connect(self.url) as ws:
            self.ws = ws
            await self._send(
                {
                    "type": "hello",
                    "role": "connector",
                    "name": "cmux-local",
                    "claim": [self.claim[0], self.claim[1]],
                }
            )
            await self._wait_for_welcome()
            if self.health is not None:
                self.health.ready(transport="websocket", peer=self.url)
            await self.refresh(force=True)
            self._watcher = asyncio.create_task(self._poll_loop())
            try:
                async for raw in ws:
                    try:
                        message = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    await self._handle_message(message)
            finally:
                if self._watcher is not None:
                    self._watcher.cancel()
                    try:
                        await self._watcher
                    except asyncio.CancelledError:
                        pass
                    self._watcher = None
                self.ws = None
                self.ready.clear()

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Reconnect forever; one hub restart must not strand this connector."""
        await reconnect_forever(
            self._run_connection,
            name="connector_cmux",
            reporter=self.health,
            policy=RetryPolicy(initial=0.5, maximum=30.0),
            stop_event=stop_event,
            on_error=lambda exc, delay: log.warning(
                "cmux connector disconnected: %s; retrying in %.1fs", exc, delay
            ),
        )


def parse_claim(values: list[int]) -> tuple[int, int]:
    first, last = values
    if first < 0 or first > last:
        raise argparse.ArgumentTypeError(f"invalid inclusive claim {first}..{last}")
    return first, last


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST, help="deckd host (default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="deckd port (default: %(default)s)")
    parser.add_argument(
        "--claim",
        nargs=2,
        metavar=("FIRST", "LAST"),
        type=int,
        default=list(DEFAULT_CLAIM),
        help="inclusive key range to claim (default: 5 9)",
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="use every claimed key as an agent slot (no summary key)",
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_PATH,
        help="cmux shim JSON state file (default: %(default)s)",
    )
    parser.add_argument(
        "--focus-cmd",
        default="cmux focus {name}",
        help="shell command template for agent presses (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="state file polling interval in seconds (default: %(default)s)",
    )
    return parser


async def async_main(args: argparse.Namespace) -> None:
    claim = parse_claim(args.claim)
    connector = CmuxConnector(
        url=f"ws://{args.host}:{args.port}",
        state_path=args.state_file,
        claim=claim,
        focus_cmd=args.focus_cmd,
        poll_interval=args.poll_interval,
        summary=not args.no_summary,
        health=HealthReporter("connector_cmux", stale_after=20.0),
    )
    await connector.run()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        log.error("connector stopped: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    raise SystemExit(main())
