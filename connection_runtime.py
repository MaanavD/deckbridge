#!/usr/bin/env python3
"""Shared retry and truthful-health runtime for Deckbridge connections.

The transports vary (SSH subprocesses, Discord REST, WebSockets, HID), but the
operator contract must not: retry forever, cap the retry rate, preserve the
last good data, and say when that data is no longer live.  Keeping that policy
behind this small interface prevents a connector from being "healthy" merely
because its process still exists.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable


DEFAULT_HEALTH_DIR = "~/.deckbridge/health"
NONINTERACTIVE_SSH_OPTIONS = (
    "-oBatchMode=yes",
    "-oConnectTimeout=6",
    "-oConnectionAttempts=1",
    "-oServerAliveInterval=5",
    "-oServerAliveCountMax=1",
)


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with optional proportional jitter."""

    initial: float = 1.0
    maximum: float = 60.0
    multiplier: float = 2.0
    jitter: float = 0.15

    def __post_init__(self) -> None:
        if self.initial < 0 or self.maximum < 0:
            raise ValueError("retry delays must be non-negative")
        if self.maximum < self.initial:
            raise ValueError("maximum retry delay must be at least initial")
        if self.multiplier < 1:
            raise ValueError("retry multiplier must be at least one")
        if not 0 <= self.jitter <= 1:
            raise ValueError("retry jitter must be between zero and one")

    def delay(self, failures: int) -> float:
        exponent = max(0, int(failures) - 1)
        base = float(self.initial)
        if base == 0:
            return 0.0
        for _ in range(exponent):
            if base >= self.maximum:
                break
            base = min(self.maximum, base * self.multiplier)
        if not self.jitter or not base:
            return float(base)
        spread = base * self.jitter
        return max(0.0, random.uniform(base - spread, base + spread))


def default_health_path(name: str) -> Path:
    root = os.environ.get("DECKBRIDGE_HEALTH_DIR", DEFAULT_HEALTH_DIR)
    return Path(root).expanduser() / f"{name}.json"


class HealthReporter:
    """Atomically publish one connection's last-success/error contract."""

    def __init__(
        self,
        name: str,
        *,
        path: str | Path | None = None,
        stale_after: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if stale_after <= 0:
            raise ValueError("stale_after must be positive")
        self.name = str(name)
        self.path = Path(path).expanduser() if path else default_health_path(self.name)
        self.stale_after = float(stale_after)
        self.clock = clock
        self._last_success_at: float | None = None
        self._checked_at: float | None = None
        self._failures = 0
        self._restore()

    def _restore(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            value = document.get("last_success_at")
            if isinstance(value, (int, float)):
                self._last_success_at = float(value)
            checked = document.get("checked_at")
            if isinstance(checked, (int, float)):
                self._checked_at = float(checked)
            failures = document.get("consecutive_failures", 0)
            if isinstance(failures, int) and failures >= 0:
                self._failures = failures
        except (OSError, ValueError, TypeError):
            return

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = ""
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=str(self.path.parent), text=True
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = ""
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _document(self, status: str, error: str, details: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": status,
            "checked_at": float(self.clock()),
            "last_success_at": self._last_success_at,
            "stale_after_seconds": self.stale_after,
            "consecutive_failures": self._failures,
            "error": error[:1000],
            **details,
        }

    def ready(self, **details: Any) -> None:
        self._last_success_at = float(self.clock())
        self._checked_at = self._last_success_at
        self._failures = 0
        self._write(self._document("ready", "", details))

    def heartbeat(self, interval: float = 5.0, **details: Any) -> bool:
        """Refresh ready health at most once per interval; return if written."""
        now = float(self.clock())
        if self._checked_at is not None and now - self._checked_at < max(0.0, interval):
            return False
        self.ready(**details)
        return True

    def degraded(self, error: object, **details: Any) -> None:
        self._failures += 1
        self._checked_at = float(self.clock())
        message = " ".join(str(error).split()) or "connection failed"
        self._write(self._document("degraded", message, details))


@dataclass(frozen=True)
class ConnectionHealth:
    """Evaluated health snapshot returned to status/health callers."""

    ok: bool
    state: str
    message: str
    document: dict[str, Any]

    @classmethod
    def from_path(
        cls, path: str | Path, *, now: float | None = None
    ) -> "ConnectionHealth":
        target = Path(path).expanduser()
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(False, "missing", f"{target.name} has no health snapshot", {})
        except (OSError, ValueError, TypeError) as exc:
            return cls(False, "invalid", f"{target.name} health is unreadable: {exc}", {})
        if not isinstance(document, dict):
            return cls(False, "invalid", f"{target.name} health is not an object", {})

        name = str(document.get("name") or target.stem)
        status = str(document.get("status") or "invalid")
        error = str(document.get("error") or "")
        if status != "ready":
            message = f"{name} feed is {status}"
            if error:
                message += f": {error}"
            return cls(False, status, message, document)

        checked = document.get("checked_at")
        stale_after = document.get("stale_after_seconds")
        if not isinstance(checked, (int, float)) or not isinstance(stale_after, (int, float)):
            return cls(False, "invalid", f"{name} health lacks freshness metadata", document)
        age = max(0.0, float(now if now is not None else time.time()) - float(checked))
        if age > float(stale_after):
            return cls(
                False,
                "stale",
                f"{name} feed is stale ({age:.0f}s old; limit {float(stale_after):.0f}s)",
                document,
            )
        return cls(True, "ready", f"{name} feed is ready ({age:.0f}s old)", document)


async def reconnect_forever(
    connect_once: Callable[[], Awaitable[None]],
    *,
    name: str,
    reporter: HealthReporter | None = None,
    policy: RetryPolicy | None = None,
    stop_event: asyncio.Event | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_error: Callable[[Exception, float], None] | None = None,
) -> None:
    """Run one connection adapter until stopped, retrying every failure."""

    retry = policy or RetryPolicy()
    failures = 0
    while stop_event is None or not stop_event.is_set():
        try:
            await connect_once()
            # A connection that completed its adapter's normal serving path
            # was genuinely established; a later close starts a new failure
            # streak rather than inheriting yesterday's startup attempts.
            failures = 0
            if stop_event is not None and stop_event.is_set():
                return
            raise ConnectionError(f"{name} connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            delay = retry.delay(failures)
            if reporter is not None:
                reporter.degraded(exc, retry_in_seconds=round(delay, 3))
            if on_error is not None:
                on_error(exc, delay)
            await sleep(delay)
    return
