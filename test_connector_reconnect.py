#!/usr/bin/env python3
"""Kill/restart deckd and prove every active connector repairs itself."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import websockets

import deckd
from connection_runtime import ConnectionHealth, HealthReporter
from connector_agents import AgentConnector
from connector_cmux import CmuxConnector
from connector_mic import MicConnector


HOST, PORT = "127.0.0.1", 8996
RESULTS: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    ok = bool(condition)
    RESULTS.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}{'' if ok or not detail else ': ' + detail}")


async def start_hub():
    hub = deckd.Hub(15)
    return await websockets.serve(lambda ws: deckd.handle(ws, hub), HOST, PORT)


async def wait_health(path: Path, state: str, timeout: float = 5.0) -> ConnectionHealth:
    deadline = asyncio.get_running_loop().time() + timeout
    last = ConnectionHealth.from_path(path)
    while asyncio.get_running_loop().time() < deadline:
        last = ConnectionHealth.from_path(path)
        if last.state == state:
            return last
        await asyncio.sleep(0.02)
    raise TimeoutError(f"{path.name} never became {state}: {last.message}")


async def scenario() -> None:
    with tempfile.TemporaryDirectory(prefix="deckbridge-reconnect-e2e-") as tmp:
        root = Path(tmp)
        local_state = root / "cmux_state.json"
        hermes_state = root / "hermes_agents.json"
        local_state.write_text('{"agents":[]}', encoding="utf-8")
        hermes_state.write_text('{"agents":[]}', encoding="utf-8")
        url = f"ws://{HOST}:{PORT}"

        health_paths = {
            name: root / "health" / f"{name}.json"
            for name in ("connector_agents", "connector_mic", "connector_cmux")
        }
        agents = AgentConnector(
            url=url, claim=(0, 9), hermes_state=hermes_state,
            local_state=local_state, poll_interval=0.02,
            health=HealthReporter(
                "connector_agents", path=health_paths["connector_agents"],
                stale_after=2,
            ),
        )
        mic = MicConnector(
            url=url, key=14, command="true", release_command="true",
            check_command="true", check_interval=0.05,
            health=HealthReporter(
                "connector_mic", path=health_paths["connector_mic"],
                stale_after=2,
            ),
        )
        cmux = CmuxConnector(
            url=url, state_path=local_state, claim=(10, 13),
            poll_interval=0.02, summary=False,
            health=HealthReporter(
                "connector_cmux", path=health_paths["connector_cmux"],
                stale_after=2,
            ),
        )

        server = await start_hub()
        tasks = [
            asyncio.create_task(agents.run()),
            asyncio.create_task(mic.run()),
            asyncio.create_task(cmux.run()),
        ]
        try:
            for name, path in health_paths.items():
                health = await wait_health(path, "ready")
                check(f"{name} initially connects", health.ok, health.message)

            server.close()
            await server.wait_closed()
            for name, path in health_paths.items():
                health = await wait_health(path, "degraded")
                check(f"{name} exposes hub outage", not health.ok, health.message)

            server = await start_hub()
            for name, path in health_paths.items():
                health = await wait_health(path, "ready")
                check(f"{name} reconnects and clears failure",
                      health.ok
                      and health.document.get("consecutive_failures") == 0,
                      json.dumps(health.document, sort_keys=True))
            check("no connector process exits during hub restart",
                  all(not task.done() for task in tasks))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            server.close()
            await server.wait_closed()


def main() -> int:
    try:
        asyncio.run(scenario())
    except Exception as exc:
        check("reconnect scenario completes", False, str(exc))
    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if RESULTS and passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
