#!/usr/bin/env python3
"""Mic connector: hold one key to talk in the focused app.

Claims a single key (default 14, the bottom-right corner of a classic deck) and
runs ``mic_key.sh`` when pressed.  That script detects the frontmost macOS app
and picks the right action for it:

* Discord            -> hold-to-talk macOS system dictation
* Claude Code        -> hold its native voice key (``/voice hold`` enabled)
* Codex CLI          -> hold-to-talk macOS system dictation
* anything else      -> hold-to-talk macOS system dictation

The connector cannot prove that dictation is currently recording, because
neither macOS nor the target apps expose that state. It *can* prove whether the
trigger command succeeded. The face therefore says ``triggered`` only after
exit 0, says ``grant access`` for the named native helper's macOS permission,
and says ``setup needed`` after another rejected setup/action; it never claims
to be listening based on elapsed time alone. A locked login session is a
separate transient state (``unlock Mac``), and unavailable states are checked
again at a bounded interval so the key recovers without another press.

Run it anywhere; on a non-macOS host the script no-ops safely, which keeps the
emulator usable during development.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import websockets

from connection_runtime import HealthReporter, RetryPolicy, reconnect_forever

log = logging.getLogger("connector_mic")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8777
DEFAULT_KEY = 14
DEFAULT_CMD = "./mic_key.sh --press"
DEFAULT_RELEASE_CMD = "./mic_key.sh --release"
DEFAULT_CHECK_CMD = "./mic_key.sh --check"
LOCKED_EXIT_CODE = 5
ACCESSIBILITY_EXIT_CODE = 4

# The mic key is a muscle-memory action, not a status dashboard. Setup details
# belong in `qa_mic_live.sh` and the connector log; on the deck every state
# keeps the same purple, large-icon face so error prose never crowds the key.
MIC_FACE = {
    "label": "",
    "sublabel": "hold to talk",
    "badge": "",
    "source": "mic",
    "color": "#5b3fa8",
    "icon": None,
    "layout": "icon-action",
    "effect": "solid",
}
IDLE_FACE = dict(MIC_FACE)
FIRED_FACE = dict(MIC_FACE, effect="breathe", color="#704ac0")
ERROR_FACE = dict(MIC_FACE, _diagnostic="error")
ACCESS_FACE = dict(MIC_FACE, _diagnostic="accessibility")
LOCKED_FACE = dict(MIC_FACE, _diagnostic="locked")
HOLD_FACE = dict(MIC_FACE, effect="breathe", color="#704ac0")

#: How long the key shows its fired face before returning to idle.  This is
#: feedback for the press, not a claim about how long dictation runs.
FLASH_SECONDS = 2.0
DEFAULT_RETRIGGER_SECONDS = 0.75
DEFAULT_CHECK_INTERVAL = 5.0
DEFAULT_HOLD_TIMEOUT = 60.0


@dataclass(frozen=True)
class ActionResult:
    """A bounded command result that can drive truthful key feedback."""

    ok: bool
    returncode: int
    detail: str = ""


class MicConnector:
    """Own one key and trigger the mic script on press."""

    def __init__(
        self,
        url: str = f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}",
        key: int = DEFAULT_KEY,
        command: str = DEFAULT_CMD,
        release_command: str = DEFAULT_RELEASE_CMD,
        check_command: str = DEFAULT_CHECK_CMD,
        name: str = "mic",
        flash_seconds: float = FLASH_SECONDS,
        retrigger_seconds: float = DEFAULT_RETRIGGER_SECONDS,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        hold_timeout: float = DEFAULT_HOLD_TIMEOUT,
        health: HealthReporter | None = None,
    ) -> None:
        if key < 0:
            raise ValueError("key index must be >= 0")
        self.url = url
        self.key = int(key)
        self.command = command
        self.release_command = release_command
        self.check_command = check_command
        self.name = name
        self.flash_seconds = max(0.0, float(flash_seconds))
        self.retrigger_seconds = max(0.0, float(retrigger_seconds))
        if check_interval <= 0:
            raise ValueError("check interval must be positive")
        self.check_interval = float(check_interval)
        if hold_timeout <= 0:
            raise ValueError("hold timeout must be positive")
        self.hold_timeout = float(hold_timeout)
        self.health = health
        self.ws: Any = None
        # MicConnector is also instantiated outside async contexts. Defer loop-
        # bound state until the first send so Python 3.9 does not require a
        # default event loop merely to load connector configuration.
        self._send_lock: asyncio.Lock | None = None
        self._command_lock: asyncio.Lock | None = None
        self._flash_task: asyncio.Task[None] | None = None
        self._trigger_task: asyncio.Task[None] | None = None
        self._release_task: asyncio.Task[None] | None = None
        self._hold_timeout_task: asyncio.Task[None] | None = None
        self._recheck_task: asyncio.Task[None] | None = None
        self._gesture_held = False
        self._release_pending = False
        self._last_trigger_at = float("-inf")
        self._base_face = dict(IDLE_FACE)

    async def _send(self, message: dict[str, Any]) -> None:
        if self.ws is None:
            return
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            await self.ws.send(json.dumps(message))

    async def paint(self, face: dict[str, Any]) -> None:
        await self._send({"type": "face", "index": self.key, "face": face})

    def _run_command(
        self, command: str, *, purpose: str, quiet: bool = False,
    ) -> ActionResult:
        """Run one bounded command and preserve its exit status and message."""
        if not command.strip():
            return ActionResult(True, 0)
        if not quiet:
            log.info("mic %s: %s", purpose, command)
        try:
            result = subprocess.run(
                command, shell=True, check=False,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=20,
            )
            detail = (result.stdout or "").strip()[:500]
            if result.returncode != 0 and not quiet:
                log.warning("mic %s exit %s: %s",
                            purpose, result.returncode, detail)
            return ActionResult(result.returncode == 0, result.returncode, detail)
        except (OSError, subprocess.SubprocessError) as exc:
            if not quiet:
                log.warning("mic %s failed: %s", purpose, exc)
            return ActionResult(False, -1, str(exc))

    def run_script(self) -> ActionResult:
        """Invoke the mic script, swallowing every failure.

        A mic key that raises kills the connector and takes the whole board's
        bottom-right key with it, which is a much worse outcome than a press
        that silently does nothing.
        """
        return self._run_command(self.command, purpose="trigger")

    def run_release(self) -> ActionResult:
        """Release a stateful native gesture; ordinary actions make this a no-op."""
        return self._run_command(self.release_command, purpose="release")

    def run_check(self, *, quiet: bool = False) -> ActionResult:
        """Run the read-only setup preflight shown by the idle face."""
        return self._run_command(
            self.check_command, purpose="preflight", quiet=quiet)

    @staticmethod
    def _face_for_result(result: ActionResult) -> dict[str, Any]:
        if result.ok:
            return dict(IDLE_FACE)
        if result.returncode == LOCKED_EXIT_CODE:
            return dict(LOCKED_FACE)
        if result.returncode == ACCESSIBILITY_EXIT_CODE:
            return dict(ACCESS_FACE)
        return dict(ERROR_FACE)

    async def _run_serialized(self, callback: Any) -> ActionResult:
        """Run exactly one mic shell command at a time, off the event loop."""
        if self._command_lock is None:
            self._command_lock = asyncio.Lock()
        async with self._command_lock:
            future = asyncio.get_running_loop().run_in_executor(None, callback)
            try:
                # Shield prevents task cancellation from marking the executor
                # future canceled while its subprocess/thread is still running.
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # The shell command is bounded globally at 20 seconds.
                # Drain it before releasing the shared lock so shutdown or a
                # reconnect can never overlap an abandoned preflight with a
                # trigger command.
                try:
                    await future
                except Exception:
                    pass
                raise

    async def _cancel_background_tasks(self) -> None:
        """Cancel and await every connector-owned task before disconnecting."""
        press_was_active = bool(
            self._trigger_task is not None and not self._trigger_task.done()
        )
        tasks = [
            task for task in (
                self._trigger_task, self._release_task,
                self._hold_timeout_task, self._recheck_task,
            )
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A disconnect can race the press subprocess after it has posted the
        # key-down events but before its `gesture=hold` marker is consumed.
        # Releasing in either case is safer than leaving Control/M stuck.
        if self._gesture_held or press_was_active:
            self._gesture_held = False
            try:
                await self._run_serialized(self.run_release)
            except Exception:
                log.exception("mic disconnect release failed")

    async def _run_preflight(
        self, *, paint: bool = True, quiet: bool = False,
    ) -> ActionResult:
        """Refresh the persistent face from one bounded, read-only check."""
        callback = (lambda: self.run_check(quiet=True)) if quiet else self.run_check
        result = await self._run_serialized(callback)
        face = self._face_for_result(result)
        changed = face != self._base_face
        self._base_face = face
        if quiet and changed:
            detail = " ".join(result.detail.split())[:200]
            suffix = f": {detail}" if detail else ""
            log.info(
                "mic preflight state changed to %s (exit %s)%s",
                face["sublabel"], result.returncode, suffix,
            )
        if paint and changed:
            await self.paint(self._base_face)
        return result

    async def _preflight_loop(self) -> None:
        """Retry only while unavailable, recovering automatically after unlock."""
        while True:
            await asyncio.sleep(self.check_interval)
            if self.health is not None:
                self.health.heartbeat(5.0, transport="websocket", peer=self.url)
            if self._base_face == IDLE_FACE:
                continue
            try:
                await self._run_preflight(quiet=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mic periodic preflight failed")

    @staticmethod
    def _is_hold_result(result: ActionResult) -> bool:
        return result.ok and any(
            line.strip() == "gesture=hold" for line in result.detail.splitlines()
        )

    async def _release_gesture(self) -> None:
        if not self._gesture_held:
            return
        self._gesture_held = False
        timeout_task = self._hold_timeout_task
        current = asyncio.current_task()
        if timeout_task is not None and timeout_task is not current:
            timeout_task.cancel()
        result = await self._run_serialized(self.run_release)
        self._base_face = self._face_for_result(result)
        await self.paint(self._base_face)

    async def _release_after_timeout(self) -> None:
        try:
            await asyncio.sleep(self.hold_timeout)
            await self._release_gesture()
        except asyncio.CancelledError:
            raise

    def _start_release(self) -> None:
        if self._release_task is not None and not self._release_task.done():
            return
        self._release_task = asyncio.create_task(self._release_gesture())

    async def _trigger(self) -> None:
        try:
            result = await self._run_serialized(self.run_script)
            # The startup preflight describes whichever app happened to be in
            # front when the connector connected.  The focused app and its
            # requirements can change before every press, so the action result
            # is the new source of truth for the persistent face.
            self._base_face = self._face_for_result(result)
            if self._is_hold_result(result):
                self._gesture_held = True
                self._base_face = dict(IDLE_FACE)
                await self.paint(HOLD_FACE)
                if self._release_pending:
                    self._release_pending = False
                    self._start_release()
                else:
                    self._hold_timeout_task = asyncio.create_task(
                        self._release_after_timeout()
                    )
                return
            # Physical decks emit a release for every tap. If that release
            # arrived while a non-hold command was running, it must not carry
            # forward and immediately cancel some later Cursor hold.
            self._release_pending = False
            await self.paint(FIRED_FACE if result.ok else self._base_face)
            await asyncio.sleep(self.flash_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            if not self._gesture_held:
                self._release_pending = False
                try:
                    await self.paint(self._base_face)
                except Exception:
                    pass

    async def _handle(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        if message.get("index") != self.key:
            return
        if message.get("type") == "release":
            if self._trigger_task is not None and not self._trigger_task.done():
                self._release_pending = True
            elif self._gesture_held:
                self._start_release()
            return
        if message.get("type") != "press":
            return
        now = time.monotonic()
        if self._gesture_held:
            return
        if self._trigger_task and not self._trigger_task.done():
            return
        if now - self._last_trigger_at < self.retrigger_seconds:
            return
        self._last_trigger_at = now
        self._trigger_task = asyncio.create_task(self._trigger())
        # Compatibility alias for embedders that awaited the old flash task.
        # It now spans command execution and result-driven feedback.
        self._flash_task = self._trigger_task

    async def _run_connection(self) -> None:
        async with websockets.connect(self.url) as ws:
            self.ws = ws
            await self._send({
                "type": "hello", "role": "connector",
                "name": self.name, "claim": [self.key, self.key],
            })
            welcome = json.loads(await ws.recv())
            if welcome.get("type") == "error":
                raise RuntimeError(welcome.get("detail") or welcome.get("reason") or "deckd rejected connector")
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"unexpected deckd response: {welcome!r}")
            if self.health is not None:
                self.health.ready(transport="websocket", peer=self.url)
            await self._run_preflight(paint=False)
            await self.paint(self._base_face)
            self._recheck_task = asyncio.create_task(self._preflight_loop())
            try:
                async for raw in ws:
                    try:
                        await self._handle(json.loads(raw))
                    except ValueError:
                        continue
            finally:
                # Make trigger-finally paints no-ops once the connection is
                # closing, then fully drain tasks and any bounded executor work.
                self.ws = None
                await self._cancel_background_tasks()

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        await reconnect_forever(
            self._run_connection,
            name=self.name,
            reporter=self.health,
            policy=RetryPolicy(initial=0.5, maximum=30.0),
            stop_event=stop_event,
            on_error=lambda exc, delay: log.warning(
                "mic connector disconnected: %s; retrying in %.1fs", exc, delay
            ),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--key", type=int, default=DEFAULT_KEY,
                        help="key index to claim (default: 14)")
    parser.add_argument("--command", default=DEFAULT_CMD,
                        help="script to run on press (default: ./mic_key.sh)")
    parser.add_argument("--release-command", default=DEFAULT_RELEASE_CMD,
                        help="script to run when a stateful gesture is released")
    parser.add_argument("--check-command", default=DEFAULT_CHECK_CMD,
                        help="read-only setup check run on connect")
    parser.add_argument("--name", default="mic")
    parser.add_argument("--flash-seconds", type=float, default=FLASH_SECONDS)
    parser.add_argument("--retrigger-seconds", type=float,
                        default=DEFAULT_RETRIGGER_SECONDS)
    parser.add_argument("--check-interval", type=float,
                        default=DEFAULT_CHECK_INTERVAL,
                        help="seconds between unavailable-state preflight retries")
    parser.add_argument("--hold-timeout", type=float,
                        default=DEFAULT_HOLD_TIMEOUT,
                        help="maximum seconds before a held gesture is released")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    connector = MicConnector(
        url=f"ws://{args.host}:{args.port}",
        key=args.key, command=args.command,
        release_command=args.release_command,
        check_command=args.check_command,
        name=args.name,
        flash_seconds=args.flash_seconds,
        retrigger_seconds=args.retrigger_seconds,
        check_interval=args.check_interval, hold_timeout=args.hold_timeout,
        health=HealthReporter("connector_mic", stale_after=20.0),
    )
    try:
        asyncio.run(connector.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
