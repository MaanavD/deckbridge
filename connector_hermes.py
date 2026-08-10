#!/usr/bin/env python3
"""Read-only Hermes approval and live Discord-agent connector for deckbridge.

The first claimed key is the existing Hermes approval summary.  Keys 1--4 are
read-only jump keys populated from ``~/.deckbridge/hermes_agents.json`` (override
with ``--agents-state``).  That file is written by the dependency-free probe
watcher and has the shape ``{"agents": [{"name", "status", "url", ...}]}``.
The connector hot-reloads both JSON files by stat mtime.  Agent keys never
approve commands or mutate Hermes state; pressing one only prints its URL and
best-effort runs ``--open-cmd``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shlex
import subprocess
import sys
from pathlib import Path
from typing import IO, Any, Iterable, Optional

import websockets

from connection_runtime import HealthReporter, RetryPolicy

LOG = logging.getLogger("connector_hermes")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8777
DEFAULT_CLAIM = (0, 4)
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_STATE_PATH = Path("~/.deckbridge/hermes_approvals.json").expanduser()
DEFAULT_AGENTS_STATE = Path("~/.deckbridge/hermes_agents.json").expanduser()
DEFAULT_OPEN_CMD = "open {url}"

RED = "#c0392b"
AMBER = "#d9822b"
BLUE = "#2e6fdb"
GREEN = "#1f8a4c"
EMPTY = "#111111"
AGENT_STATUS_FACE = {
    "working": {"color": AMBER, "effect": "shimmer"},
    "done": {"color": BLUE, "effect": "solid"},
    "idle": {"color": GREEN, "effect": "solid"},
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def approval_url(item: dict[str, Any]) -> Optional[str]:
    """Return the supplied URL, or construct a Discord deep link if possible."""
    supplied = item.get("url")
    if supplied:
        return str(supplied)
    guild = str(item.get("guild_id") or "")
    channel = str(item.get("channel_id") or "")
    message = str(item.get("message_id") or "")
    if channel and message:
        return f"https://discord.com/channels/{guild or '@me'}/{channel}/{message}"
    return None


def newest_pending(items: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Select the newest approval, using created_ts then input order."""
    valid = [item for item in items if isinstance(item, dict)]
    if not valid:
        return None
    return max(enumerate(valid), key=lambda pair: (_as_float(pair[1].get("created_ts")), pair[0]))[1]


def summary_face(pending: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the canonical face for the first key in the connector claim."""
    if pending:
        return {
            "label": "APPROVE?",
            "sublabel": f"{len(pending)} pending",
            "color": RED,
            "icon": "hermes",
            "effect": "breathe",
        }
    return {
        "label": "Hermes",
        "sublabel": "idle",
        "color": GREEN,
        "icon": "hermes",
        "effect": "solid",
    }


class HermesConnector:
    """WebSocket client that owns one contiguous key range in deckd."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        claim: tuple[int, int] = DEFAULT_CLAIM,
        state_path: str | Path = DEFAULT_STATE_PATH,
        agents_state: str | Path = DEFAULT_AGENTS_STATE,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        reconnect_delay: float = 1.0,
        open_cmd: str = DEFAULT_OPEN_CMD,
        output: IO[str] | None = None,
        name: str = "hermes-discord",
        health: HealthReporter | None = None,
    ) -> None:
        first, last = int(claim[0]), int(claim[1])
        if first < 0 or first > last:
            raise ValueError(f"invalid inclusive claim: {first}..{last}")
        self.host = host
        self.port = int(port)
        self.claim = (first, last)
        self.state_path = Path(state_path).expanduser()
        self.agents_state = Path(agents_state).expanduser()
        self.poll_interval = max(0.01, float(poll_interval))
        self.reconnect_delay = max(0.0, float(reconnect_delay))
        self.open_cmd = open_cmd
        self.output = output if output is not None else sys.stdout
        self.name = name
        self.health = health
        self.pending: list[dict[str, Any]] = []
        self.agents: list[dict[str, Any]] = []
        self.agent_keys: dict[int, dict[str, Any]] = {}
        self.press_event = asyncio.Event()
        self._state_mtime: int | None = None
        self._agents_mtime: int | None = None

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def hello_message(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "role": "connector",
            "name": self.name,
            "claim": [self.claim[0], self.claim[1]],
        }

    def _stat_mtime(self) -> int | None:
        try:
            return self.state_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOG.warning("cannot stat approval state %s: %s", self.state_path, exc)
            return None

    def _stat_agents_mtime(self) -> int | None:
        try:
            return self.agents_state.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        except OSError as exc:
            LOG.warning("cannot stat Hermes agent state %s: %s", self.agents_state, exc)
            return None

    def read_pending(self) -> list[dict[str, Any]]:
        """Read and validate the approval state, retaining old state on bad JSON."""
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            document = json.loads(raw)
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            LOG.warning("cannot read approval state %s: %s", self.state_path, exc)
            return list(self.pending)
        if not isinstance(document, dict) or not isinstance(document.get("pending"), list):
            LOG.warning("approval state must be an object with a pending list")
            return list(self.pending)
        return [item for item in document["pending"] if isinstance(item, dict)]

    def read_agents(self) -> list[dict[str, Any]]:
        """Read live agent state; a missing file means all agent keys are empty."""
        try:
            raw = self.agents_state.read_text(encoding="utf-8")
            document = json.loads(raw)
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            LOG.warning("cannot read Hermes agent state %s: %s", self.agents_state, exc)
            return list(self.agents)
        if not isinstance(document, dict) or not isinstance(document.get("agents"), list):
            LOG.warning("Hermes agent state must be an object with an agents list")
            return list(self.agents)
        agents: list[dict[str, Any]] = []
        for item in document["agents"]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            url = str(item.get("url", "")).strip()
            if not name or not url:
                continue
            status = str(item.get("status", "idle")).strip().lower()
            if status not in AGENT_STATUS_FACE:
                status = "idle"
            agents.append({"name": name, "status": status, "url": url})
        return agents

    def faces_for(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return flattened ``faces`` protocol items for the claimed range."""
        faces = [{"index": self.claim[0], **summary_face(pending)}]
        self.agent_keys = {}
        for offset, index in enumerate(range(self.claim[0] + 1, self.claim[1] + 1)):
            agent = self.agents[offset] if offset < len(self.agents) and offset < 4 else None
            if agent is None:
                face = {
                    "label": "",
                    "sublabel": "",
                    "color": EMPTY,
                    "icon": None,
                    "effect": "off",
                }
            else:
                style = AGENT_STATUS_FACE[agent["status"]]
                self.agent_keys[index] = agent
                face = {
                    "label": agent["name"],
                    "sublabel": agent["status"],
                    "color": style["color"],
                    "icon": "discord",
                    "effect": style["effect"],
                }
            faces.append({"index": index, **face})
        return faces

    async def paint(self, ws, pending: list[dict[str, Any]]) -> None:
        await ws.send(json.dumps({"type": "faces", "faces": self.faces_for(pending)}))

    async def _poll_state(self, ws, stop_event: asyncio.Event) -> None:
        first_poll = True
        while not stop_event.is_set():
            if self.health is not None:
                self.health.heartbeat(5.0, transport="websocket", peer=self.url)
            state_mtime = self._stat_mtime()
            agents_mtime = self._stat_agents_mtime()
            if first_poll or state_mtime != self._state_mtime or agents_mtime != self._agents_mtime:
                self.pending = self.read_pending()
                self.agents = self.read_agents()
                self._state_mtime = state_mtime
                self._agents_mtime = agents_mtime
                await self.paint(ws, self.pending)
                first_poll = False
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _receive(self, ws) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or message.get("type") != "press":
                continue
            try:
                index = int(message.get("index"))
            except (TypeError, ValueError):
                continue
            if index == self.claim[0]:
                self.handle_press()
            elif self.claim[0] <= index <= self.claim[1]:
                self.handle_agent_press(index)

    def _open_url(self, url: str, description: str) -> None:
        """Best-effort run the configured open command for a URL."""
        if not self.open_cmd:
            return
        try:
            command = self.open_cmd.format(url=url)
            argv = shlex.split(command)
            if not argv:
                raise ValueError("open command is empty")
            subprocess.run(
                argv,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (IndexError, KeyError, OSError, ValueError) as exc:
            print(f"{description}: could not run open command: {exc}", file=self.output, flush=True)

    def handle_press(self) -> None:
        """Print and best-effort open the newest currently pending approval."""
        item = newest_pending(self.pending)
        url = approval_url(item) if item else None
        if not url:
            print("Hermes approval key pressed: no pending approval", file=self.output, flush=True)
            self.press_event.set()
            return
        print(f"Hermes approval pending: {url}", file=self.output, flush=True)
        self._open_url(url, "Hermes approval")
        self.press_event.set()

    def handle_agent_press(self, index: int) -> None:
        """Print and best-effort open a live agent URL, or report an empty key."""
        agent = self.agent_keys.get(index)
        url = agent.get("url") if agent is not None else None
        if not url:
            print(f"no agent on key {index}", file=self.output, flush=True)
            self.press_event.set()
            return
        print(f"Hermes agent key {index}: {url}", file=self.output, flush=True)
        self._open_url(url, f"Hermes agent key {index}")
        self.press_event.set()

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Connect, claim, paint, poll, and reconnect until stopped/cancelled."""
        if stop_event is None:
            stop_event = asyncio.Event()
        retry = RetryPolicy(
            initial=self.reconnect_delay,
            maximum=max(30.0, self.reconnect_delay),
        )
        failures = 0
        while not stop_event.is_set():
            try:
                async with websockets.connect(self.url) as ws:
                    await ws.send(json.dumps(self.hello_message()))
                    welcome = json.loads(await ws.recv())
                    if welcome.get("type") == "error":
                        raise RuntimeError(welcome.get("reason", "deckd rejected connector"))
                    if welcome.get("type") != "welcome":
                        raise RuntimeError(f"unexpected deckd response: {welcome!r}")
                    failures = 0
                    if self.health is not None:
                        self.health.ready(transport="websocket", peer=self.url)
                    LOG.info("connected to %s claim=%s", self.url, self.claim)
                    receive_task = asyncio.create_task(self._receive(ws))
                    poll_task = asyncio.create_task(self._poll_state(ws, stop_event))
                    done, pending_tasks = await asyncio.wait(
                        (receive_task, poll_task), return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending_tasks:
                        task.cancel()
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                    for task in done:
                        task.result()
                    if stop_event.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop_event.is_set():
                    return
                failures += 1
                delay = retry.delay(failures)
                if self.health is not None:
                    self.health.degraded(exc, retry_in_seconds=round(delay, 3))
                LOG.warning(
                    "Hermes connector disconnected: %s; retrying in %.1fs",
                    exc, delay,
                )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--claim", nargs=2, type=int, metavar=("FIRST", "LAST"),
        default=list(DEFAULT_CLAIM), help="inclusive key range (default: 0 4)",
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--agents-state", type=Path, default=DEFAULT_AGENTS_STATE,
        help="live Hermes agent state (default: %(default)s)",
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--open-cmd", default=DEFAULT_OPEN_CMD, help="template containing {url}")
    parser.add_argument("--name", default="hermes-discord")
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> None:
    connector = HermesConnector(
        host=args.host,
        port=args.port,
        claim=(args.claim[0], args.claim[1]),
        state_path=args.state_file,
        agents_state=args.agents_state,
        poll_interval=args.poll_interval,
        open_cmd=args.open_cmd,
        name=args.name,
        health=HealthReporter("connector_hermes", stale_after=20.0),
    )
    await connector.run()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
