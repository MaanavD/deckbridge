#!/usr/bin/env python3
"""Tests for connector_mic.py, the single mic key.

Run directly::

    python3 test_connector_mic.py

No pytest, no hub, no macOS. The script invocation is redirected to a harmless
shell command inside a temporary directory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from connector_mic import (  # noqa: E402
    ACCESS_FACE, ActionResult, DEFAULT_KEY, ERROR_FACE, FIRED_FACE, HOLD_FACE,
    IDLE_FACE, LOCKED_FACE, MicConnector,
)

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))


class FakeWS:
    """Collect what the connector would have sent to the hub."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def test_defaults() -> None:
    check("default key is the bottom-right corner of a 15-key deck",
          DEFAULT_KEY == 14)
    check("idle face is a large mic action",
          IDLE_FACE["source"] == "mic"
          and IDLE_FACE["layout"] == "icon-action"
          and IDLE_FACE["icon"] is None)
    check("idle and fired faces are visually distinct without status prose",
          IDLE_FACE["color"] != FIRED_FACE["color"]
          and IDLE_FACE["sublabel"] == FIRED_FACE["sublabel"] == "hold to talk")
    check("the fired face animates", FIRED_FACE["effect"] == "breathe")


def test_rejects_bad_key() -> None:
    try:
        MicConnector(key=-5)
        check("a negative key index is rejected", False)
    except ValueError:
        check("a negative key index is rejected", True)
    try:
        MicConnector(check_interval=0)
        check("a non-positive preflight interval is rejected", False)
    except ValueError:
        check("a non-positive preflight interval is rejected", True)


def test_claims_exactly_one_key() -> None:
    c = MicConnector(key=14)
    ws = FakeWS()
    c.ws = ws

    async def go() -> None:
        await c._send({
            "type": "hello", "role": "connector",
            "name": c.name, "claim": [c.key, c.key],
        })
        await c.paint(IDLE_FACE)

    asyncio.run(go())
    hello = ws.sent[0]
    check("claim covers exactly one key", hello["claim"] == [14, 14])
    face_msg = ws.sent[1]
    check("paints its own index", face_msg["index"] == 14)
    check("paints the idle mic face first",
          face_msg["face"]["sublabel"] == IDLE_FACE["sublabel"])


def test_press_runs_the_script_and_flashes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "fired.txt"
        c = MicConnector(key=14, command=f"echo fired >> {marker}",
                         flash_seconds=0.05)
        ws = FakeWS()
        c.ws = ws

        async def go() -> None:
            await c._handle({"type": "press", "index": 14})
            if c._flash_task:
                await c._flash_task

        asyncio.run(go())
        check("a press runs the configured command", marker.exists())
        painted = [m["face"] for m in ws.sent if m.get("type") == "face"]
        check("the key flashes then returns to idle",
              painted == [FIRED_FACE, IDLE_FACE], str(painted))


def test_ignores_other_keys_and_events() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "fired.txt"
        c = MicConnector(key=14, command=f"echo fired >> {marker}",
                         flash_seconds=0.01)
        c.ws = FakeWS()

        async def go() -> None:
            await c._handle({"type": "press", "index": 3})
            await c._handle({"type": "release", "index": 14})
            await c._handle({"type": "hello"})
            await c._handle("not a dict")
            await c._handle({"type": "press"})

        asyncio.run(go())
        check("a press on another key does not fire the mic", not marker.exists())
        check("releases, unknown types, and junk are ignored", not marker.exists())


def test_failing_script_is_survivable() -> None:
    """A mic key that raises would take the corner key down with it."""
    c = MicConnector(key=14, command="exit 3", flash_seconds=0.01)
    c.ws = FakeWS()
    c.run_script()
    check("a nonzero exit does not raise", True)
    c.command = "this-command-does-not-exist-xyz"
    c.run_script()
    check("a missing command does not raise", True)


def test_repeat_presses_do_not_stack_flashes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "fired.txt"
        c = MicConnector(key=14, command=f"echo x >> {marker}",
                         flash_seconds=0.01, retrigger_seconds=0.0)
        c.ws = FakeWS()

        async def go() -> None:
            await c._handle({"type": "press", "index": 14})
            first = c._flash_task
            if first:
                await first
            await c._handle({"type": "press", "index": 14})
            check("a later deliberate press gets a fresh feedback task",
                  first is not None and first is not c._flash_task)
            if c._flash_task:
                await c._flash_task

        asyncio.run(go())
        fired = marker.read_text(encoding="utf-8").count("x")
        check("both presses ran the script", fired == 2, str(fired))


def test_failed_action_never_claims_listening() -> None:
    """A rejected Accessibility event must not look like an active mic."""
    c = MicConnector(key=14, command="exit 3", flash_seconds=0.01)
    ws = FakeWS()
    c.ws = ws

    async def go() -> None:
        await c._handle({"type": "press", "index": 14})
        task = getattr(c, "_trigger_task", None) or c._flash_task
        if task:
            await task

    asyncio.run(go())
    painted = [m["face"] for m in ws.sent if m.get("type") == "face"]
    check("a failed mic action never paints the active face",
          FIRED_FACE not in painted, str(painted))
    check("a failed mic action keeps the simple purple face",
          painted and all(face["sublabel"] == "hold to talk" for face in painted),
          str(painted))
    check("failure detail stays available outside the tiny key face",
          painted[-1].get("_diagnostic") == "error", str(painted))


def test_accessibility_failure_is_actionable() -> None:
    c = MicConnector(key=14)
    face = c._face_for_result(ActionResult(False, 4, "not trusted"))
    check("Accessibility failure is retained for diagnostics, not key prose",
          face == ACCESS_FACE and face["sublabel"] == "hold to talk"
          and face.get("_diagnostic") == "accessibility", str(face))


def test_cursor_hold_tracks_physical_press_and_release() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        released = Path(tmp) / "released.txt"
        c = MicConnector(
            key=14,
            command="printf 'gesture=hold\\n'",
            release_command=f"echo released >> {released}",
            flash_seconds=0,
            retrigger_seconds=0,
        )
        ws = FakeWS()
        c.ws = ws

        async def go() -> None:
            await c._handle({"type": "press", "index": 14})
            if c._trigger_task:
                await c._trigger_task
            await c._handle({"type": "release", "index": 14})
            if c._release_task:
                await c._release_task

        asyncio.run(go())
        subs = [m["face"]["sublabel"] for m in ws.sent
                if m.get("type") == "face"]
        check("Cursor hold paints a truthful held face",
              HOLD_FACE["sublabel"] in subs, str(subs))
        check("Stream Deck release emits the matching key-up action",
              released.exists())
        check("Cursor hold returns to idle after release",
              subs[-1] == IDLE_FACE["sublabel"], str(subs))


def test_cursor_hold_coalesces_duplicates_and_times_out_safe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pressed = Path(tmp) / "pressed.txt"
        released = Path(tmp) / "released.txt"
        press_script = Path(tmp) / "press.sh"
        press_script.write_text(
            f"#!/bin/sh\necho x >> '{pressed}'\nprintf 'gesture=hold\\n'\n",
            encoding="utf-8",
        )
        press_script.chmod(0o755)
        c = MicConnector(
            key=14,
            command=str(press_script),
            release_command=f"echo x >> {released}",
            flash_seconds=0,
            retrigger_seconds=0,
            hold_timeout=0.03,
        )
        c.ws = FakeWS()

        async def go() -> None:
            await c._handle({"type": "press", "index": 14})
            await c._handle({"type": "press", "index": 14})
            if c._trigger_task:
                await c._trigger_task
            await c._handle({"type": "press", "index": 14})
            for _ in range(100):
                if released.exists():
                    break
                await asyncio.sleep(0.005)
            await c._handle({"type": "release", "index": 14})
            await c._handle({"type": "release", "index": 14})

        asyncio.run(go())
        check("duplicate hold presses emit exactly one key-down gesture",
              pressed.read_text(encoding="utf-8").count("x") == 1)
        check("hold timeout emits exactly one safety release",
              released.read_text(encoding="utf-8").count("x") == 1)


def test_disconnect_releases_held_cursor_modifier() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        released = Path(tmp) / "released.txt"
        c = MicConnector(
            key=14,
            command="printf 'gesture=hold\\n'",
            release_command=f"echo released >> {released}",
            flash_seconds=0,
            hold_timeout=30,
        )
        c.ws = FakeWS()

        async def go() -> None:
            await c._handle({"type": "press", "index": 14})
            if c._trigger_task:
                await c._trigger_task
            c.ws = None
            await c._cancel_background_tasks()

        asyncio.run(go())
        check("disconnect cleanup releases Cursor's held modifier",
              released.exists())


def test_normal_tap_release_cannot_poison_a_later_cursor_hold() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        released = Path(tmp) / "released.txt"
        c = MicConnector(
            key=14,
            command="sleep 0.04",
            release_command=f"echo released >> {released}",
            flash_seconds=0,
            retrigger_seconds=0,
            hold_timeout=2,
        )
        c.ws = FakeWS()

        async def go() -> tuple[bool, bool]:
            await c._handle({"type": "press", "index": 14})
            await asyncio.sleep(0.005)
            await c._handle({"type": "release", "index": 14})
            if c._trigger_task:
                await c._trigger_task
            c.command = "printf 'gesture=hold\\n'"
            await c._handle({"type": "press", "index": 14})
            if c._trigger_task:
                await c._trigger_task
            await asyncio.sleep(0.02)
            held_before_own_release = c._gesture_held and not released.exists()
            await c._handle({"type": "release", "index": 14})
            if c._release_task:
                await c._release_task
            return held_before_own_release, released.exists()

        held, did_release = asyncio.run(go())
        check("ordinary tap release state does not poison a later hold", held)
        check("later hold still releases on its own physical release", did_release)


def test_success_repairs_a_stale_startup_preflight() -> None:
    """The app in front can change after the connector's startup check.

    A connector launched while Dictation is unavailable may later trigger
    Claude Code's native voice key successfully.  The successful action must
    return to idle rather than repainting the stale startup error forever.
    """
    c = MicConnector(key=14, command="exit 0", flash_seconds=0.01)
    c._base_face = dict(ERROR_FACE)
    ws = FakeWS()
    c.ws = ws

    async def go() -> None:
        await c._handle({"type": "press", "index": 14})
        if c._trigger_task:
            await c._trigger_task

    asyncio.run(go())
    painted = [m["face"] for m in ws.sent if m.get("type") == "face"]
    check("a successful action clears a stale setup error",
          painted == [FIRED_FACE, IDLE_FACE], str(painted))


def test_locked_action_has_distinct_persistent_feedback() -> None:
    """Exit 5 means unlock, not a permanent permissions/setup failure."""
    c = MicConnector(key=14, command="exit 5", flash_seconds=0.01)
    ws = FakeWS()
    c.ws = ws

    async def go() -> None:
        await c._handle({"type": "press", "index": 14})
        if c._trigger_task:
            await c._trigger_task

    asyncio.run(go())
    painted = [m["face"] for m in ws.sent if m.get("type") == "face"]
    check("a locked action stays visually simple but remains diagnosable",
          painted and all(face["sublabel"] == "hold to talk" for face in painted)
          and painted[-1].get("_diagnostic") == "locked", str(painted))


def test_unavailable_preflight_recovers_without_a_press() -> None:
    """Unlock recovery is periodic and does not require another key press."""
    c = MicConnector(key=14, check_interval=0.01)
    c._base_face = dict(LOCKED_FACE)
    ws = FakeWS()
    c.ws = ws
    calls = 0

    def ready_after_unlock(**_kwargs):
        nonlocal calls
        calls += 1
        return ActionResult(True, 0, "ready=yes")

    c.run_check = ready_after_unlock  # type: ignore[method-assign]

    async def go() -> None:
        task = asyncio.create_task(c._preflight_loop())
        for _ in range(100):
            if c._base_face == IDLE_FACE:
                break
            await asyncio.sleep(0.005)
        # Once ready, the loop stays passive. It must not keep launching a
        # health-check subprocess forever on a healthy machine.
        await asyncio.sleep(0.04)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())
    check("locked mic automatically recovers to idle after unlock",
          calls == 1 and c._base_face == IDLE_FACE,
          f"calls={calls} face={c._base_face}")


def test_preflight_and_trigger_subprocesses_never_overlap() -> None:
    """A retry racing a physical press must serialize both shell commands."""
    c = MicConnector(key=14, flash_seconds=0, check_interval=0.01)
    c.ws = FakeWS()
    guard = threading.Lock()
    active = 0
    maximum = 0

    def bounded_result():
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with guard:
            active -= 1
        return ActionResult(True, 0)

    c.run_check = bounded_result  # type: ignore[method-assign]
    c.run_script = bounded_result  # type: ignore[method-assign]

    async def go() -> None:
        checking = asyncio.create_task(c._run_preflight())
        await asyncio.sleep(0.005)
        await c._handle({"type": "press", "index": 14})
        if c._trigger_task:
            await c._trigger_task
        await checking

    asyncio.run(go())
    check("preflight and trigger subprocesses are serialized", maximum == 1,
          f"maximum concurrent commands={maximum}")


def test_shutdown_drains_background_tasks_and_bounded_executor() -> None:
    """Disconnect must not leak a retry task or abandon its executor command."""
    c = MicConnector(key=14, check_interval=0.005, release_command="true")
    c.ws = FakeWS()
    c._base_face = {**ERROR_FACE}
    started = threading.Event()
    finished = threading.Event()

    def slow_check(**_kwargs):
        started.set()
        time.sleep(0.06)
        finished.set()
        return ActionResult(False, 5)

    c.run_check = slow_check  # type: ignore[method-assign]

    async def go() -> tuple[bool, bool]:
        c._recheck_task = asyncio.create_task(c._preflight_loop())
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.002)
        c._trigger_task = asyncio.create_task(asyncio.sleep(30))
        c.ws = None
        await c._cancel_background_tasks()
        return c._recheck_task.done(), c._trigger_task.done()

    retry_done, trigger_done = asyncio.run(go())
    check("shutdown awaits both mic background tasks",
          retry_done and trigger_done)
    check("shutdown drains the in-flight bounded preflight executor",
          finished.is_set())


def test_periodic_preflight_logging_is_transition_driven() -> None:
    """An unchanged locked retry must not add two log records every five seconds."""
    class Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    logger = logging.getLogger("connector_mic")
    old_level, old_propagate = logger.level, logger.propagate
    capture = Capture()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(capture)
    try:
        c = MicConnector(
            key=14, check_command="exit 5", command="exit 3",
            check_interval=0.01,
        )
        c.ws = FakeWS()
        c._base_face = dict(LOCKED_FACE)

        async def retry_locked() -> None:
            task = asyncio.create_task(c._preflight_loop())
            await asyncio.sleep(0.045)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(retry_locked())
        retry_logs = list(capture.records)
        capture.records.clear()
        c.check_command = "exit 0"
        asyncio.run(c._run_preflight(quiet=True))
        asyncio.run(c._run_preflight(quiet=True))
        transition_logs = list(capture.records)
        capture.records.clear()
        c.run_script()
        trigger_logs = list(capture.records)
    finally:
        logger.removeHandler(capture)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    check("unchanged periodic preflight retries stay quiet",
          not retry_logs,
          str([r.getMessage() for r in retry_logs]))
    check("a preflight face transition logs exactly once",
          len([r for r in transition_logs
               if "preflight state changed" in r.getMessage()]) == 1,
          str([r.getMessage() for r in transition_logs]))
    check("real trigger failures still emit a warning",
          any(r.levelno >= logging.WARNING and "trigger exit 3" in r.getMessage()
              for r in trigger_logs),
          str([r.getMessage() for r in trigger_logs]))


def test_press_burst_is_coalesced_while_action_runs() -> None:
    """One physical press can repeat; it must not toggle dictation twice."""
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "burst.txt"
        command = f"echo x >> {marker}; sleep 0.15"
        c = MicConnector(key=14, command=command, flash_seconds=0.01)
        c.ws = FakeWS()

        async def go() -> None:
            await c._handle({"type": "press", "index": 14})
            await c._handle({"type": "press", "index": 14})
            task = getattr(c, "_trigger_task", None) or c._flash_task
            if task:
                await task

        asyncio.run(go())
        fired = marker.read_text(encoding="utf-8").count("x")
        check("repeat events coalesce into one mic trigger", fired == 1, str(fired))


def main() -> int:
    test_defaults()
    test_rejects_bad_key()
    test_claims_exactly_one_key()
    test_press_runs_the_script_and_flashes()
    test_ignores_other_keys_and_events()
    test_failing_script_is_survivable()
    test_repeat_presses_do_not_stack_flashes()
    test_failed_action_never_claims_listening()
    test_accessibility_failure_is_actionable()
    test_cursor_hold_tracks_physical_press_and_release()
    test_cursor_hold_coalesces_duplicates_and_times_out_safe()
    test_disconnect_releases_held_cursor_modifier()
    test_normal_tap_release_cannot_poison_a_later_cursor_hold()
    test_success_repairs_a_stale_startup_preflight()
    test_locked_action_has_distinct_persistent_feedback()
    test_unavailable_preflight_recovers_without_a_press()
    test_preflight_and_trigger_subprocesses_never_overlap()
    test_shutdown_drains_background_tasks_and_bounded_executor()
    test_periodic_preflight_logging_is_transition_driven()
    test_press_burst_is_coalesced_while_action_runs()
    total = PASSED + FAILED
    print(f"\n{PASSED}/{total} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
