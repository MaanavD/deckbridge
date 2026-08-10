#!/usr/bin/env python3
"""In-process integration test for the Hermes deckbridge connector.

The test uses deckd.Hub plus a fake renderer and a temporary JSON state file;
no Discord credentials, bot, or browser are required.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import websockets

import connector_hermes
import deckd
import hermes_discord_watcher

HOST, PORT = "127.0.0.1", 8897
RESULTS: list[tuple[str, bool]] = []


class FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def info(self, message: str, *args: object) -> None:
        self.events.append(("INFO", message % args))

    def debug(self, message: str, *args: object) -> None:
        self.events.append(("DEBUG", message % args))


def check(name: str, condition: bool) -> None:
    RESULTS.append((name, bool(condition)))
    print(("PASS" if condition else "FAIL"), name)


async def recv_json(ws, timeout: float = 2.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def recv_state(ws, predicate, timeout: float = 3.0) -> dict:
    """Drain renderer frames until a state frame satisfies predicate."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("timed out waiting for matching state frame")
        msg = await recv_json(ws, remaining)
        if msg.get("type") == "state" and predicate(msg):
            return msg


def write_state(path: Path, pending: list[dict]) -> None:
    # A single write is enough for this test; the production watcher uses an
    # atomic replace so the connector never observes a partially-written file.
    path.write_text(json.dumps({"pending": pending}), encoding="utf-8")


def write_agents(path: Path, agents: list[dict]) -> None:
    path.write_text(json.dumps({"agents": agents}), encoding="utf-8")


async def run() -> None:
    with tempfile.TemporaryDirectory(prefix="deckbridge-hermes-test-") as tmp:
        state_path = Path(tmp) / "hermes_approvals.json"
        agents_path = Path(tmp) / "hermes_agents.json"
        write_state(state_path, [])
        discord_url = "https://discord.com/channels/1/2/agent"
        write_agents(
            agents_path,
            [{"name": "work", "status": "working", "url": discord_url}],
        )

        hub = deckd.Hub(15)
        server = await websockets.serve(
            lambda ws: deckd.handle(ws, hub), HOST, PORT
        )
        renderer = await websockets.connect(f"ws://{HOST}:{PORT}")
        connector_task: asyncio.Task | None = None
        try:
            await renderer.send(json.dumps({
                "type": "hello", "role": "renderer", "name": "fake-renderer"
            }))
            welcome = await recv_json(renderer)
            initial = await recv_json(renderer)
            check(
                "renderer welcome",
                welcome.get("type") == "welcome" and welcome.get("keys") == 15,
            )
            check(
                "renderer initial state",
                initial.get("type") == "state" and len(initial.get("faces", [])) == 15,
            )

            output = io.StringIO()
            connector = connector_hermes.HermesConnector(
                host=HOST,
                port=PORT,
                claim=(0, 4),
                state_path=state_path,
                agents_state=agents_path,
                poll_interval=0.05,
                reconnect_delay=0.05,
                open_cmd="open-test {url}",
                output=output,
            )
            stop = asyncio.Event()
            connector_task = asyncio.create_task(connector.run(stop_event=stop))

            idle = await recv_state(
                renderer,
                lambda st: st["faces"][0]["color"] == "#1f8a4c"
                and st["faces"][0]["effect"] == "solid",
            )
            check(
                "connector claims Hermes zone",
                idle["faces"][0]["label"] == "Hermes",
            )
            check(
                "live Discord agent paints label and color",
                idle["faces"][1]["label"] == "work"
                and idle["faces"][1]["sublabel"] == "working"
                and idle["faces"][1]["color"] == "#d9822b"
                and idle["faces"][1]["icon"] == "discord",
            )
            check(
                "absent agent key is empty",
                idle["faces"][4]["effect"] == "off"
                and idle["faces"][4]["color"] == "#111111",
            )

            old_url = "https://discord.com/channels/1/2/100"
            newest_url = "https://discord.com/channels/1/2/200"
            pending = [
                {
                    "message_id": "100",
                    "channel_id": "2",
                    "guild_id": "1",
                    "command": "echo old",
                    "reason": "old",
                    "created_ts": 100.0,
                    "url": old_url,
                },
                {
                    "message_id": "200",
                    "channel_id": "2",
                    "guild_id": "1",
                    "command": "echo newest",
                    "reason": "new",
                    "created_ts": 200.0,
                    "url": newest_url,
                },
            ]
            write_state(state_path, pending)
            alert = await recv_state(
                renderer,
                lambda st: st["faces"][0]["color"] == "#c0392b"
                and st["faces"][0]["effect"] == "breathe",
            )
            check(
                "pending approvals are red and breathe",
                alert["faces"][0]["color"] == "#c0392b"
                and alert["faces"][0]["effect"] == "breathe"
                and "2 pending" in alert["faces"][0]["sublabel"],
            )

            write_state(state_path, [])
            cleared = await recv_state(
                renderer,
                lambda st: st["faces"][0]["color"] == "#1f8a4c"
                and st["faces"][0]["effect"] == "solid",
            )
            check(
                "resolved approvals clear to idle green",
                cleared["faces"][0]["color"] == "#1f8a4c"
                and cleared["faces"][0]["effect"] == "solid"
                and cleared["faces"][0]["sublabel"] == "idle",
            )

            # Re-arm one approval for the press test.  A cleared key has no
            # newest pending URL, so press-to-jump is exercised while pending.
            write_state(state_path, pending)
            await recv_state(
                renderer,
                lambda st: st["faces"][0]["color"] == "#c0392b"
                and st["faces"][0]["effect"] == "breathe",
            )

            opened: list[tuple[list[str], dict]] = []
            original_run = connector_hermes.subprocess.run

            def fake_run(argv, **kwargs):
                opened.append((list(argv), dict(kwargs)))
                return subprocess.CompletedProcess(argv, 0)

            connector_hermes.subprocess.run = fake_run
            try:
                await renderer.send(json.dumps({"type": "press", "index": 1}))
                await asyncio.wait_for(connector.press_event.wait(), timeout=2.0)
                connector.press_event.clear()
                await renderer.send(json.dumps({"type": "press", "index": 0}))
                await asyncio.wait_for(connector.press_event.wait(), timeout=2.0)
            finally:
                connector_hermes.subprocess.run = original_run

            check(
                "live Discord press emits its URL",
                discord_url in output.getvalue()
                and opened
                and discord_url in opened[0][0],
            )
            check(
                "press emits newest approval URL",
                newest_url in output.getvalue()
                and opened
                and any(newest_url in argv for argv, _ in opened),
            )

            # Small offline checks for the optional watcher contract.
            request = hermes_discord_watcher.build_messages_request(
                "token", "123", limit=10
            )
            check(
                "watcher sends Discord User-Agent",
                request.get_header("User-agent")
                == hermes_discord_watcher.DISCORD_USER_AGENT,
            )
            channel_request = hermes_discord_watcher.build_channel_request(
                "token", "123"
            )
            check(
                "watcher can resolve a channel guild read-only",
                channel_request.full_url.endswith("/channels/123")
                and channel_request.method == "GET"
                and channel_request.get_header("Authorization") == "Bot token",
            )
            record = hermes_discord_watcher.approval_from_message(
                {
                    "id": "300",
                    "timestamp": "2026-08-05T12:00:00.000Z",
                    "embeds": [{
                        "title": "Command Approval Required",
                        "description": (
                            "Do you want Hermes to run this command?\n"
                            "Requested command:\n```sh\necho hi\n```\n"
                            "Reason: test"
                        ),
                    }],
                },
                channel_id="123",
                guild_id="456",
            )
            expired = hermes_discord_watcher.approval_from_message(
                {
                    "id": "301",
                    "embeds": [{
                        "title": "Command Approval Required",
                        "description": "Approval expired — command was not run",
                    }],
                },
                channel_id="123",
                guild_id="456",
            )
            check(
                "watcher detects active and ignores expired approval",
                record is not None
                and record["url"].endswith("/456/123/300")
                and record["command"] == "echo hi"
                and expired is None,
            )

            # No guild id is configured on the target host.  A server-channel
            # approval must therefore resolve its guild via GET /channels once;
            # falling back to @me opens Discord but cannot select the message.
            resolved_path = Path(tmp) / "resolved_approvals.json"
            original_fetch_messages = hermes_discord_watcher.fetch_messages
            original_fetch_guild = hermes_discord_watcher.fetch_channel_guild_id
            hermes_discord_watcher.fetch_messages = lambda *_args, **_kwargs: [{
                "id": "302",
                "embeds": [{"title": "Command Approval Required"}],
            }]
            hermes_discord_watcher.fetch_channel_guild_id = (
                lambda *_args, **_kwargs: "resolved-guild"
            )
            try:
                resolved = hermes_discord_watcher.poll_once(
                    "token", "123", state_path=resolved_path
                )
            finally:
                hermes_discord_watcher.fetch_messages = original_fetch_messages
                hermes_discord_watcher.fetch_channel_guild_id = original_fetch_guild
            check(
                "watcher resolves missing guild before writing jump URL",
                len(resolved) == 1
                and resolved[0]["url"].endswith("/resolved-guild/123/302")
                and json.loads(resolved_path.read_text(encoding="utf-8"))[
                    "pending"
                ][0]["guild_id"] == "resolved-guild",
            )

            fake_log = FakeLog()
            original_log = hermes_discord_watcher.LOG
            hermes_discord_watcher.LOG = fake_log  # type: ignore[assignment]
            try:
                previous = hermes_discord_watcher._log_poll_result(
                    0, None, "/tmp/approvals.json"
                )
                previous = hermes_discord_watcher._log_poll_result(
                    0, previous, "/tmp/approvals.json"
                )
                hermes_discord_watcher._log_poll_result(
                    1, previous, "/tmp/approvals.json"
                )
            finally:
                hermes_discord_watcher.LOG = original_log
            check(
                "steady Discord polls are debug-only; startup and changes are info",
                [level for level, _ in fake_log.events]
                == ["INFO", "DEBUG", "INFO"],
            )
            check(
                "approval count transition is named in the info log",
                "changed 0 -> 1" in fake_log.events[-1][1],
            )
        finally:
            if connector_task is not None:
                stop.set()
                connector_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await connector_task
            await renderer.close()
            server.close()
            await server.wait_closed()

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(run())
