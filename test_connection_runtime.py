#!/usr/bin/env python3
"""Interface tests for Deckbridge's shared connection runtime.

These tests deliberately avoid real sockets.  The production adapters are SSH,
Discord REST, WebSockets, and Stream Deck HID; the reliability contract is the
same for all of them and belongs behind one seam.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from connection_runtime import (
    ConnectionHealth,
    HealthReporter,
    RetryPolicy,
    SSH_AUTH_RETRY_SECONDS,
    retry_delay_for_error,
    reconnect_forever,
)


RESULTS: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    ok = bool(condition)
    RESULTS.append((name, ok))
    suffix = "" if ok or not detail else f": {detail}"
    print(f"{'PASS' if ok else 'FAIL'} {name}{suffix}")


def test_retry_policy() -> None:
    policy = RetryPolicy(initial=1, maximum=5, multiplier=2, jitter=0)
    delays = [policy.delay(failures) for failures in range(1, 7)]
    check("retry delay grows and caps", delays == [1, 2, 4, 5, 5, 5], str(delays))
    check("success reset uses the initial delay", policy.delay(1) == 1)
    check("Tailscale auth challenges cool down instead of flashing",
          retry_delay_for_error(
              policy, 1,
              "To authenticate, visit: https://login.tailscale.com/a/example",
          ) == SSH_AUTH_RETRY_SECONDS)
    check("ordinary failures keep normal exponential backoff",
          retry_delay_for_error(policy, 2, "connection reset") == 2)


def test_health_reporter() -> None:
    with tempfile.TemporaryDirectory(prefix="deckbridge-health-") as tmp:
        path = Path(tmp) / "nested" / "hermes_agents.json"
        clock_value = [1000.0]
        reporter = HealthReporter(
            "hermes_agents", path=path, stale_after=20,
            clock=lambda: clock_value[0],
        )
        reporter.ready(agent_count=2, transport="ssh")
        ready = json.loads(path.read_text(encoding="utf-8"))
        check("ready snapshot is atomic and complete",
              ready["status"] == "ready"
              and ready["last_success_at"] == 1000.0
              and ready["consecutive_failures"] == 0
              and ready["agent_count"] == 2)
        check("atomic writer leaves no temp files",
              [item.name for item in path.parent.iterdir()] == [path.name])

        clock_value[0] = 1005.0
        reporter.degraded("Tailscale SSH requires an additional check")
        degraded = json.loads(path.read_text(encoding="utf-8"))
        check("failure preserves last success and becomes visible",
              degraded["status"] == "degraded"
              and degraded["last_success_at"] == 1000.0
              and degraded["consecutive_failures"] == 1
              and "additional check" in degraded["error"])
        health = ConnectionHealth.from_path(path, now=1006.0)
        check("degraded snapshot fails health", not health.ok and health.state == "degraded")

        clock_value[0] = 1010.0
        reporter.ready(agent_count=3)
        health = ConnectionHealth.from_path(path, now=1015.0)
        check("fresh success passes health", health.ok and health.state == "ready")
        stale = ConnectionHealth.from_path(path, now=1031.0)
        check("old success becomes stale", not stale.ok and stale.state == "stale")
        missing = ConnectionHealth.from_path(path.with_name("missing.json"), now=1015.0)
        check("missing snapshot fails health", not missing.ok and missing.state == "missing")


def test_reconnect_loop() -> None:
    async def scenario() -> tuple[int, list[float], dict]:
        with tempfile.TemporaryDirectory(prefix="deckbridge-reconnect-") as tmp:
            path = Path(tmp) / "connector.json"
            reporter = HealthReporter("connector", path=path, stale_after=30)
            attempts = 0
            sleeps: list[float] = []
            stop = asyncio.Event()

            async def connect_once() -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise ConnectionError(f"offline-{attempts}")
                reporter.ready(peer="deckd")
                stop.set()

            async def fake_sleep(delay: float) -> None:
                sleeps.append(delay)

            await reconnect_forever(
                connect_once,
                name="connector",
                reporter=reporter,
                policy=RetryPolicy(initial=0.25, maximum=2, jitter=0),
                stop_event=stop,
                sleep=fake_sleep,
            )
            return attempts, sleeps, json.loads(path.read_text(encoding="utf-8"))

    attempts, sleeps, snapshot = asyncio.run(scenario())
    check("reconnect loop survives failures and recovers", attempts == 3, str(attempts))
    check("reconnect loop applies bounded backoff", sleeps == [0.25, 0.5], str(sleeps))
    check("recovery clears degraded health",
          snapshot["status"] == "ready" and snapshot["consecutive_failures"] == 0)


def main() -> int:
    test_retry_policy()
    test_health_reporter()
    test_reconnect_loop()
    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if RESULTS and passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
