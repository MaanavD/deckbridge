#!/usr/bin/env python3
"""End-to-end test for the cmux connector and deckd hub.

The test uses only an in-process deckd Hub and a temporary JSON state file; no
cmux installation or Stream Deck hardware is required.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
from pathlib import Path

import websockets

import connector_cmux
import deckd

HOST, PORT = "127.0.0.1", 8798
CLAIM = [5, 9]
results: list[tuple[str, bool]] = []


def check(name: str, condition: bool) -> None:
    ok = bool(condition)
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}")


async def next_state(renderer, timeout: float = 2.0) -> dict:
    """Read through renderer frames until a composited state arrives."""
    while True:
        message = json.loads(await asyncio.wait_for(renderer.recv(), timeout))
        if message.get("type") == "state":
            return message


async def wait_for_faces(renderer, predicate, timeout: float = 3.0) -> dict:
    """Return the first renderer state satisfying predicate(faces)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for expected faces")
        state = await next_state(renderer, remaining)
        if predicate(state["faces"]):
            return state


async def run() -> None:
    hub = deckd.Hub(15)
    server = await websockets.serve(lambda ws: deckd.handle(ws, hub), HOST, PORT)
    renderer = None
    connector_task = None

    with tempfile.TemporaryDirectory(prefix="deckbridge-cmux-") as tmp:
        state_path = Path(tmp) / "cmux_state.json"
        state_path.write_text(
            json.dumps(
                {
                    "agents": [
                        {"name": "codex", "status": "blocked", "cwd": "/tmp/codex"},
                        {"name": "claude", "status": "working", "cwd": "/tmp/claude"},
                        {"name": "hermes", "status": "idle", "cwd": "/tmp/hermes"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        focus_calls: list[str] = []
        focus_seen = threading.Event()
        original_run = connector_cmux.subprocess.run

        def fake_run(command, *args, **kwargs):
            focus_calls.append(str(command))
            focus_seen.set()
            return type("Completed", (), {"returncode": 0})()

        connector_cmux.subprocess.run = fake_run
        try:
            url = f"ws://{HOST}:{PORT}"
            renderer = await websockets.connect(url)
            await renderer.send(
                json.dumps({"type": "hello", "role": "renderer", "name": "test-renderer"})
            )
            renderer_welcome = json.loads(await renderer.recv())
            check(
                "renderer welcome",
                renderer_welcome.get("type") == "welcome"
                and renderer_welcome.get("keys") == 15,
            )
            initial = json.loads(await renderer.recv())
            check("initial composited state", initial.get("type") == "state")

            client = connector_cmux.CmuxConnector(
                url=url,
                state_path=state_path,
                claim=tuple(CLAIM),
                focus_cmd="cmux focus {name}",
                poll_interval=0.05,
            )
            connector_task = asyncio.create_task(client.run())

            # The connector's first response is consumed internally, but the
            # client exposes readiness after receiving deckd's welcome.
            await asyncio.wait_for(client.ready.wait(), timeout=2.0)
            check("connector connected with claimed range", client.claim == tuple(CLAIM))

            state = await wait_for_faces(
                renderer,
                lambda faces: (
                    faces[5]["color"] == "#c0392b"
                    and faces[6]["color"] == "#c0392b"
                    and faces[7]["color"] == "#d9822b"
                    and faces[8]["color"] == "#1f8a4c"
                ),
            )
            faces = state["faces"]
            check(
                "summary shows blocked state",
                faces[5]["color"] == "#c0392b" and faces[5]["effect"] == "breathe",
            )
            check(
                "blocked agent painted red",
                faces[6]["color"] == "#c0392b" and faces[6]["effect"] == "breathe",
            )
            check(
                "working agent painted amber",
                faces[7]["color"] == "#d9822b" and faces[7]["effect"] == "solid",
            )
            check(
                "idle agent painted green",
                faces[8]["color"] == "#1f8a4c" and faces[8]["effect"] == "solid",
            )
            check(
                "unused claimed agent slot is empty",
                faces[9]["color"] == "#111111" and faces[9]["effect"] == "off",
            )
            check(
                "painted keys stay within claim",
                all(
                    CLAIM[0] <= index <= CLAIM[1]
                    for index, face in enumerate(faces)
                    if face.get("effect") != "off"
                ),
            )

            await renderer.send(json.dumps({"type": "press", "index": 7}))
            focus_attempted = await asyncio.to_thread(focus_seen.wait, 2.0)
            check(
                "agent press attempts focus command",
                focus_attempted
                and focus_calls
                and focus_calls[-1] == "cmux focus claude",
            )

            no_summary_args = connector_cmux.build_parser().parse_args(["--no-summary"])
            no_summary = connector_cmux.CmuxConnector(
                url=url,
                state_path=state_path,
                claim=tuple(CLAIM),
                summary=not no_summary_args.no_summary,
                poll_interval=0.05,
            )
            no_summary_faces = no_summary._build_faces(client._agents)
            check(
                "--no-summary puts agent zero on first claimed key",
                no_summary_args.no_summary
                and no_summary_faces[CLAIM[0]]["label"] == "codex"
                and no_summary._agent_keys[CLAIM[0]]["name"] == "codex",
            )
        finally:
            connector_cmux.subprocess.run = original_run
            if connector_task is not None:
                connector_task.cancel()
                try:
                    await connector_task
                except (asyncio.CancelledError, websockets.ConnectionClosed):
                    pass
            if renderer is not None:
                await renderer.close()
            server.close()
            await server.wait_closed()


async def main() -> int:
    try:
        await run()
    except Exception as exc:
        check(f"test completed without exception ({exc})", False)
    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
