#!/usr/bin/env python3
"""End-to-end proof against a FAKE cmux that reproduces the wrong-tab bug.

The unit tests assert the resolver reports an ancestry. This asserts the whole
press actually lands on the right tab, against a stub that behaves the way the
real cmux does: ``focus-panel`` alone raises the window but leaves the selected
workspace untouched, so the operator sees the default tab. That is the exact
symptom reported from the Mac, and no test that stops at the resolver can catch
it.

Run directly::

    python3 test_focus_e2e.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"ok - {name}")
    else:
        FAILED += 1
        print(f"not ok - {name}" + (f": {detail}" if detail else ""))


#: Two workspaces in one window. The agent is on workspace:13 / surface:27;
#: workspace:1 is what the window currently shows. Copied from real v3.9.6
#: output, trimmed to the fields the resolver reads.
TREE = {
    "active": {"pane_ref": "pane:1", "surface_ref": "surface:1"},
    "windows": [{
        "ref": "window:1",
        "workspaces": [
            {"ref": "workspace:1", "panes": [{"ref": "pane:1", "surfaces": [
                {"ref": "surface:1", "title": "~/Documents/notes",
                 "tty": "ttys000", "type": "terminal"}]}]},
            {"ref": "workspace:13", "panes": [{"ref": "pane:23", "surfaces": [
                {"ref": "surface:27", "title": "\u2733 sample-api \u2014 claude",
                 "tty": "ttys027", "type": "terminal"}]}]},
        ],
    }],
}

FAKE_CMUX = r'''#!/usr/bin/env python3
"""A cmux stub that behaves like the real one, INCLUDING the bug.

`focus-panel` sets the active surface only when that surface lives in the
window's currently selected workspace. Otherwise the window comes forward and
the operator keeps looking at the old tab -- which is what "it opens cmux but
it's not the right tab" means. `select-workspace` is what changes that.
"""
import json, os, sys

STATE = os.environ["FAKE_CMUX_STATE"]
LOG = os.environ["FAKE_CMUX_LOG"]

with open(STATE) as fh:
    state = json.load(fh)
with open(LOG, "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")

argv = sys.argv[1:]
cmd = argv[0] if argv else ""

def save():
    with open(STATE, "w") as fh:
        json.dump(state, fh)

def workspace_of(surface):
    for w in state["tree"]["windows"]:
        for ws in w["workspaces"]:
            for pane in ws["panes"]:
                for s in pane["surfaces"]:
                    if s["ref"] == surface:
                        return ws["ref"]
    return None

if cmd == "tree":
    tree = dict(state["tree"])
    tree["active"] = {"surface_ref": state["active_surface"]}
    print(json.dumps(tree))
    sys.exit(0)

if cmd == "select-workspace":
    # ONLY the documented flag form is accepted, so the script's fallbacks are
    # exercised for real rather than assumed to work.
    if "--workspace" not in argv:
        sys.exit(2)
    state["selected_workspace"] = argv[argv.index("--workspace") + 1]
    save()
    sys.exit(0)

if cmd == "focus-panel":
    if "--panel" not in argv:
        sys.exit(2)
    target = argv[argv.index("--panel") + 1]
    ws = workspace_of(target)
    if ws is None:
        sys.exit(1)
    # The bug: a panel outside the selected workspace does NOT become active.
    if ws == state["selected_workspace"]:
        state["active_surface"] = target
    save()
    sys.exit(0)

if cmd == "version":
    print("cmux 3.9.6")
    sys.exit(0)
sys.exit(1)
'''


def run_press(tmp: Path, *, selected: str) -> tuple[str, str, list[str]]:
    """Press the agent key once; return (active surface, output, cmux calls)."""
    state = tmp / "state.json"
    log = tmp / "calls.log"
    state.write_text(json.dumps({
        "tree": TREE, "selected_workspace": selected,
        "active_surface": "surface:1",
    }), encoding="utf-8")
    log.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{tmp / 'bin'}:{env['PATH']}"
    env["FAKE_CMUX_STATE"] = str(state)
    env["FAKE_CMUX_LOG"] = str(log)
    env["HOME"] = "/Users/example"
    proc = subprocess.run(
        [str(HERE / "focus_agent.sh"), "--source", "claude-code",
         "--name", "sample api", "--cwd", "/Users/example/code/sample-api",
         "--tty", "ttys027"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    after = json.loads(state.read_text(encoding="utf-8"))
    calls = [l for l in log.read_text(encoding="utf-8").splitlines() if l]
    return after["active_surface"], proc.stdout + proc.stderr, calls


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        (tmp / "bin").mkdir()
        fake = tmp / "bin" / "cmux"
        fake.write_text(FAKE_CMUX, encoding="utf-8")
        fake.chmod(0o755)
        # Nothing here may reach AppleScript; a stub proves it never does.
        osa = tmp / "bin" / "osascript"
        osa.write_text("#!/bin/sh\necho OSASCRIPT_RAN >&2\nexit 0\n", encoding="utf-8")
        osa.chmod(0o755)

        # The reported case: the agent is on a workspace the window is not
        # showing. Before the fix this focused nothing and left the default tab.
        active, out, calls = run_press(tmp, selected="workspace:1")
        check("a surface on an unselected workspace still becomes active",
              active == "surface:27", f"active={active} out={out!r}")
        check("the workspace was selected before the panel was focused",
              any(c.startswith("select-workspace") for c in calls)
              and calls.index(next(c for c in calls if c.startswith("select-workspace")))
              < calls.index(next(c for c in calls if c.startswith("focus-panel"))),
              str(calls))
        check("the press reported success", "focused via cmux" in out, out)
        check("no AppleScript was invoked", "OSASCRIPT_RAN" not in out, out)

        # The already-correct case must not regress or thrash.
        active, out, calls = run_press(tmp, selected="workspace:13")
        check("an already-selected workspace still focuses the surface",
              active == "surface:27", f"active={active} out={out!r}")
        check("focus-panel is not called repeatedly when the first press lands",
              len([c for c in calls if c.startswith("focus-panel")]) == 1,
              str(calls))

    total = PASSED + FAILED
    print(f"\n{PASSED}/{total} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
