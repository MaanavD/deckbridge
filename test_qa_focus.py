#!/usr/bin/env python3
"""Tests for qa_focus.py, the QA harness itself.

A harness that cannot fail is worse than no harness: it prints PASS forever
and launders bugs into confidence. These tests drive qa_focus against a FAKE
macOS -- a stub cmux, a stub osascript, a stub focus script -- and check two
things for every scenario:

  * it PASSES when the world behaves correctly
  * it FAILS when the world behaves like the actual reported bug

The second half is the point. Every scenario here is derived from a real
failure, so each one is replayed to prove the harness would have caught it.

Runs anywhere: nothing real is launched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
QA = HERE / "qa_focus.py"

PASSED = FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f": {detail}" if detail else ""))


TREE = {
    "active": {"surface_ref": "surface:1"},
    "windows": [{"ref": "window:1", "workspaces": [
        {"ref": "workspace:1", "panes": [{"ref": "pane:1", "surfaces": [
            {"ref": "surface:1", "tty": "ttys001", "title": "~/repo",
             "workspace_ref": "workspace:1"},
            {"ref": "surface:2", "tty": "ttys002", "title": "~/repo",
             "workspace_ref": "workspace:1"},
        ]}]},
        {"ref": "workspace:2", "panes": [{"ref": "pane:2", "surfaces": [
            # No inline workspace_ref: this matches real cmux output and pins
            # the harness's ancestry propagation rather than relying on a fake.
            {"ref": "surface:3", "tty": "ttys003", "title": "~/other"},
        ]}]},
    ]}],
}


def make_world(tmp: Path, *, focus_moves_to: str | None,
               focus_exit: int = 0, focus_says: str = "",
               frontmost: str = "Finder",
               allow_current_spawn: bool = False) -> dict:
    """Build a fake macOS in tmp and return the env to run qa_focus with.

    `focus_moves_to` is what the stub focus script pretends to focus. None
    means "the press changed nothing", which is how a silently-wrong press
    behaves.
    """
    bindir = tmp / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    active = tmp / "active"
    active.write_text("surface:1", encoding="utf-8")
    front = tmp / "front"
    front.write_text(frontmost, encoding="utf-8")
    spawned = tmp / "spawned-cwds"

    tree_json = json.dumps(TREE)
    (bindir / "cmux").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = tree ]; then\n'
        f"  python3 - <<'PY'\n"
        "import json\n"
        f"tree = json.loads(r'''{tree_json}''')\n"
        f"tree['active'] = {{'surface_ref': open(r'{active}').read().strip()}}\n"
        f"spawned = r'{spawned}'\n"
        "if __import__('os').path.exists(spawned):\n"
        "  for i, cwd in enumerate(open(spawned).read().splitlines(), 10):\n"
        "    tree['windows'][0]['workspaces'].append({'ref': f'workspace:{i}', 'panes': [{'ref': f'pane:{i}', 'surfaces': [{'ref': f'surface:{i}', 'tty': f'ttys{i:03d}', 'title': cwd}]}]})\n"
        "print(json.dumps(tree))\n"
        "PY\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = focus-panel ]; then\n'
        f'  printf "%s" "$3" > {active}\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = new-tab ]; then exit 1; fi\n'
        + ("" if not allow_current_spawn else
           'if [ "$1" = new-workspace ]; then\n'
           '  while [ "$#" -gt 1 ]; do\n'
           '    if [ "$1" = --cwd ]; then printf "%s\\n" "$2" >> ' + str(spawned) + '; exit 0; fi\n'
           '    shift\n'
           '  done\n'
           '  exit 1\n'
           'fi\n') +
        "exit 0\n", encoding="utf-8")

    (bindir / "osascript").write_text(
        "#!/bin/sh\n"
        f'case "$*" in *frontmost*) cat {front};; '
        f'*Finder*) printf Finder > {front};; esac\n'
        "exit 0\n", encoding="utf-8")

    (bindir / "pgrep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    move = ""
    if focus_moves_to:
        move = f'printf "%s" "{focus_moves_to}" > {active}\n'
    front_move = ""
    if frontmost != "Finder" or focus_moves_to is None:
        pass
    (tmp / "focus_agent.sh").write_text(
        "#!/bin/sh\n"
        f"{move}"
        f'printf "%s\\n" "{focus_says}"\n'
        f"exit {focus_exit}\n", encoding="utf-8")
    os.chmod(tmp / "focus_agent.sh", 0o755)
    for f in bindir.iterdir():
        os.chmod(f, 0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["QA_STATE_DIR"] = str(tmp / "state")
    env["QA_FAKE_MACOS"] = "1"
    env["QA_SETTLE_S"] = "0"
    return env


def run_qa(tmp: Path, env: dict, scenario: str) -> dict:
    """Run qa_focus from tmp, where our stub focus_agent.sh lives."""
    import shutil
    shutil.copy(QA, tmp / "qa_focus.py")
    proc = subprocess.run(
        [sys.executable, str(tmp / "qa_focus.py"), "--json", "--only", scenario],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp),
    )
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"error": "unparseable", "stdout": proc.stdout,
                "stderr": proc.stderr}


def status_of(payload: dict) -> str:
    results = payload.get("results") or []
    return results[0]["status"] if results else f"<no result: {payload}>"


# ------------------------------------------------------------------ tests ---
def test_harness_refuses_off_macos() -> None:
    """It must not print a green verdict on a machine that cannot host a window.

    A harness that "passes" on Linux would certify focus behaviour it never
    exercised, which is the most expensive possible false positive here.
    """
    if sys.platform == "darwin":
        # This suite is normally developed on Linux, but QA_FOR_CODEX is meant
        # to be run on the target Mac. On macOS, preflight accepting the real
        # host is the corresponding assertion; do not drive the whole live QA
        # suite from this unit test.
        import qa_focus
        problems = qa_focus.preflight()
        check("macOS is accepted as the real focus-test host",
              not any(problem.startswith("not macOS") for problem in problems),
              str(problems))
        check("the off-macOS refusal is not applicable on macOS", True)
        return
    clean = {k: v for k, v in os.environ.items() if k != "QA_FAKE_MACOS"}
    proc = subprocess.run(
        [sys.executable, str(QA), "--json"],
        capture_output=True, text=True, timeout=30, env=clean,
    )
    payload = json.loads(proc.stdout)
    check("off-macOS runs refuse rather than report success",
          payload.get("error") == "preflight failed", proc.stdout[:120])
    check("the refusal exits non-zero", proc.returncode == 2,
          str(proc.returncode))


def test_recorded_tty_passes_when_focus_lands() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = make_world(tmp, focus_moves_to="surface:1")
        got = status_of(run_qa(tmp, env, "recorded_tty"))
        check("a press that lands on the right tab passes", got == "PASS", got)


def test_recorded_tty_fails_when_focus_does_not_move() -> None:
    """The silent no-op: exit 0, nothing happened. Must NOT pass."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = make_world(tmp, focus_moves_to=None, focus_exit=0)
        got = status_of(run_qa(tmp, env, "recorded_tty"))
        check("a press that exits 0 but moves nothing fails", got == "FAIL", got)


def test_recorded_tty_fails_when_focus_lands_on_the_wrong_tab() -> None:
    """The reported bug: a real tab, just not the agent's. Must NOT pass."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = make_world(tmp, focus_moves_to="surface:3", focus_exit=0)
        got = status_of(run_qa(tmp, env, "recorded_tty"))
        check("a press that lands on the WRONG tab fails", got == "FAIL", got)


def test_ambiguous_tabs_passes_only_on_a_refusal() -> None:
    """Two tabs, one directory, no tty. Guessing is the bug; refusing is right.

    The stub tree puts surface:1 and surface:2 both in ~/repo, so the harness
    runs from a directory with that name and sees the ambiguity for real.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t) / "repo"
        tmp.mkdir()
        tmp = tmp.resolve()
        env = make_world(tmp, focus_moves_to=None, focus_exit=1,
                         focus_says="resolver: 2 surfaces match")
        env["HOME"] = str(tmp.parent)
        got = status_of(run_qa(tmp, env, "ambiguous_tabs"))
        check("refusing an unidentifiable tab passes", got == "PASS", got)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t) / "repo"
        tmp.mkdir()
        tmp = tmp.resolve()
        env = make_world(tmp, focus_moves_to="surface:2", focus_exit=0)
        env["HOME"] = str(tmp.parent)
        got = status_of(run_qa(tmp, env, "ambiguous_tabs"))
        check("guessing an arbitrary tab FAILS (the reported regression)",
              got == "FAIL", got)


def test_ambiguous_tabs_can_build_world_with_current_cmux_command() -> None:
    with tempfile.TemporaryDirectory() as t:
        tmp = (Path(t) / "current-cmux-project").resolve()
        tmp.mkdir()
        env = make_world(tmp, focus_moves_to=None, focus_exit=1,
                         focus_says="resolver: ambiguous",
                         allow_current_spawn=True)
        got = status_of(run_qa(tmp, env, "ambiguous_tabs"))
        check("current cmux new-workspace builds a non-skipped ambiguous world",
              got == "PASS", got)


def test_quit_app_refuses_is_checked_by_message_not_just_code() -> None:
    """An exit code alone would accept a crash as correct behaviour."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = make_world(tmp, focus_moves_to=None, focus_exit=1,
                         focus_says="not focusing X: it is not running")
        got = status_of(run_qa(tmp, env, "quit_app_refuses"))
        check("a stated refusal passes", got == "PASS", got)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = make_world(tmp, focus_moves_to=None, focus_exit=0)
        got = status_of(run_qa(tmp, env, "quit_app_refuses"))
        check("silently succeeding on a dead app fails", got == "FAIL", got)


def test_workspace_switch_fails_when_it_lands_on_the_old_workspace() -> None:
    """The "cmux opens on its default tab" symptom, replayed."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = make_world(tmp, focus_moves_to="surface:1", focus_exit=0)
        got = status_of(run_qa(tmp, env, "workspace_switch"))
        check("landing on the wrong workspace fails", got == "FAIL", got)


def test_json_output_is_machine_readable() -> None:
    """An agent loop parses this; it must never be prose."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = make_world(tmp, focus_moves_to="surface:1")
        payload = run_qa(tmp, env, "recorded_tty")
        check("json has a results array", isinstance(payload.get("results"), list))
        check("json has counts",
              all(k in payload for k in ("passed", "failed", "skipped")),
              str(payload.keys()))
        r = (payload.get("results") or [{}])[0]
        check("each result carries expected and actual",
              "expected" in r and "actual" in r, str(r))


if __name__ == "__main__":
    test_harness_refuses_off_macos()
    test_recorded_tty_passes_when_focus_lands()
    test_recorded_tty_fails_when_focus_does_not_move()
    test_recorded_tty_fails_when_focus_lands_on_the_wrong_tab()
    test_ambiguous_tabs_passes_only_on_a_refusal()
    test_ambiguous_tabs_can_build_world_with_current_cmux_command()
    test_quit_app_refuses_is_checked_by_message_not_just_code()
    test_workspace_switch_fails_when_it_lands_on_the_old_workspace()
    test_json_output_is_machine_readable()
    print(f"\n{PASSED}/{PASSED + FAILED} passed")
    raise SystemExit(1 if FAILED else 0)
