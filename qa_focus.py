#!/usr/bin/env python3
"""Self-verifying QA harness for deckbridge focus.

WHY THIS EXISTS
===============

Every deckbridge focus bug so far has had the same shape: the press "succeeded"
while landing somewhere wrong. A blank Terminal window. cmux's default tab. An
arbitrary one of eight tabs open in the same repo. Each time, the exit code
said 0.

Exit codes cannot catch that class of bug, because the thing that is wrong is
the WORLD, not the return value. So this harness never trusts an exit code. It
asks the machine what is actually focused now, and compares that to what should
be focused.

    ground truth = cmux tree's `active` surface, or the frontmost macOS app
    verdict       = ground truth == the surface/app the agent really lives in

That is the whole idea. Everything else is plumbing.

WHAT IT DOES
============

For each scenario:

  1. builds the world (spawns real cmux tabs, notes which apps are running)
  2. writes the state file deckbridge reads, exactly as a hook would
  3. moves focus somewhere else deliberately, so a no-op cannot pass
  4. runs ./focus_agent.sh the way a key press runs it
  5. reads back what is focused NOW
  6. reports PASS/FAIL with the expected and actual value side by side

Step 3 matters more than it looks. If the right tab is already focused, doing
nothing at all looks like success. Every scenario parks focus on a known-wrong
surface first, so a passing result means the press MOVED focus correctly.

USAGE
=====

    python3 qa_focus.py                 # run everything, report
    python3 qa_focus.py --list          # names only
    python3 qa_focus.py --only ambiguous_tabs
    python3 qa_focus.py --json          # machine-readable, for an agent loop
    python3 qa_focus.py --keep          # leave spawned tabs open to inspect

Read QA_FOR_CODEX.md for the iterate-until-green loop.

REQUIREMENTS
============

macOS with cmux. Some scenarios need Claude.app or ChatGPT.app; those SKIP
rather than fail when the app is absent, because "not installed" is not a bug.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FOCUS = HERE / "focus_agent.sh"
STATE_DIR = Path(os.environ.get("QA_STATE_DIR", "/tmp/deckbridge-qa"))
SETTLE_S = float(os.environ.get("QA_SETTLE_S", "1.2"))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# ---------------------------------------------------------------- machine ---
def sh(*args: str, timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def cmux_tree() -> dict:
    code, out, _ = sh("cmux", "tree", "--all", "--json")
    if code != 0 or not out.strip():
        return {}
    try:
        return json.loads(out)
    except ValueError:
        return {}


def _walk(node, out: list[dict], ancestry: dict[str, str] | None = None) -> None:
    ancestry = ancestry or {}
    if isinstance(node, dict):
        ref = node.get("ref") or node.get("surface_ref")
        here = ancestry
        if isinstance(ref, str):
            for prefix, key in (("window:", "window_ref"),
                                ("workspace:", "workspace_ref"),
                                ("pane:", "pane_ref")):
                if ref.startswith(prefix):
                    here = dict(ancestry)
                    here[key] = ref
                    break
        if isinstance(ref, str) and ref.startswith("surface:"):
            # Real `cmux tree --all` nests a surface under its workspace but
            # does not repeat workspace_ref on the surface. Preserve ancestry
            # so workspace_switch tests the live hierarchy instead of SKIPping.
            record = dict(node)
            for key, value in here.items():
                record.setdefault(key, value)
            out.append(record)
        for v in node.values():
            _walk(v, out, here)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out, ancestry)


def surfaces() -> list[dict]:
    found: list[dict] = []
    _walk(cmux_tree(), found)
    # dedupe by ref, keeping the richest record
    by_ref: dict[str, dict] = {}
    for s in found:
        ref = s.get("ref") or s.get("surface_ref")
        if ref and (ref not in by_ref or len(s) > len(by_ref[ref])):
            by_ref[ref] = s
    return list(by_ref.values())


def active_surface() -> str:
    """The surface cmux says is focused RIGHT NOW. This is the ground truth.

    Read from the tree rather than inferred from a command's exit code,
    because the exit code is exactly what lied in every bug so far.
    """
    tree = cmux_tree()
    act = tree.get("active")
    if isinstance(act, dict):
        for key in ("surface_ref", "surface", "ref"):
            v = act.get(key)
            if isinstance(v, str) and v.startswith("surface:"):
                return v
    for s in surfaces():
        if s.get("active") or s.get("focused") or s.get("is_active"):
            ref = s.get("ref") or s.get("surface_ref")
            if isinstance(ref, str):
                return ref
    return ""


def frontmost_app() -> str:
    """The macOS app currently in front, or "" when it cannot be read."""
    if not have("osascript"):
        return ""
    code, out, _ = sh(
        "osascript", "-e",
        'tell application "System Events" to get name of first application '
        'process whose frontmost is true',
    )
    return out.strip() if code == 0 else ""


def app_running(app: str) -> bool:
    if not have("pgrep"):
        return False
    for pattern in (["-x", app], ["-f", f"{app}.app"]):
        if sh("pgrep", *pattern)[0] == 0:
            return True
    return False


def focus_surface(ref: str) -> None:
    target = next((s for s in surfaces()
                   if (s.get("ref") or s.get("surface_ref")) == ref), {})
    workspace = target.get("workspace_ref") or target.get("workspace")
    window = target.get("window_ref") or target.get("window")
    # Current cmux resolves short refs inside the selected workspace. Selecting
    # the target's ancestor first is required when parking on another tab; a
    # bare focus-panel can exit zero while the visible workspace never moves.
    if isinstance(workspace, str) and workspace:
        args = ["cmux", "select-workspace", "--workspace", workspace]
        if isinstance(window, str) and window:
            args += ["--window", window]
        sh(*args)
    args = ["cmux", "focus-panel", "--panel", ref]
    if isinstance(workspace, str) and workspace:
        args += ["--workspace", workspace]
    if isinstance(window, str) and window:
        args += ["--window", window]
    sh(*args)
    time.sleep(SETTLE_S / 2)


# ------------------------------------------------------------------- world ---
def spawn_tab(cwd: str, title: str | None = None) -> str:
    """Open a cmux tab in cwd and return its surface ref.

    Returns "" when the tab cannot be identified afterwards, which the caller
    treats as an inconclusive scenario rather than a failure: a harness that
    could not build its own world has not tested anything.
    """
    before = {s.get("ref") for s in surfaces()}
    # Current cmux exposes tabs as workspaces and removed the old `new-tab`
    # command. Prefer the current API, while keeping the legacy forms for
    # older installations this harness was originally written against.
    args = ["cmux", "new-workspace", "--cwd", cwd, "--focus", "true"]
    if title:
        args += ["--name", title]
    if sh(*args)[0] != 0:
        args = ["cmux", "new-tab", "--cwd", cwd]
        if title:
            args += ["--title", title]
        if sh(*args)[0] != 0 and sh("cmux", "new-tab", cwd)[0] != 0:
            return ""
    time.sleep(SETTLE_S)
    for s in surfaces():
        ref = s.get("ref")
        if ref and ref not in before:
            return ref
    return ""


def write_state(path: Path, agents: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": agents}, indent=1), encoding="utf-8")


def press(agent: dict) -> tuple[int, str]:
    """Run focus_agent.sh exactly as a key press runs it."""
    args = [
        str(FOCUS),
        "--source", agent.get("source", ""),
        "--name", agent.get("name", ""),
        "--cwd", agent.get("cwd", ""),
        "--url", agent.get("url", ""),
        "--session", agent.get("session_id", ""),
        "--tty", agent.get("tty", ""),
        "--app", agent.get("app", ""),
        "--surface", agent.get("surface", ""),
    ]
    code, out, err = sh(*args, timeout=30)
    time.sleep(SETTLE_S)
    return code, (out + err).strip()


# --------------------------------------------------------------- scenarios ---
class Result:
    def __init__(self, name: str, status: str, expected: str = "",
                 actual: str = "", note: str = "", output: str = "") -> None:
        self.name, self.status = name, status
        self.expected, self.actual = expected, actual
        self.note, self.output = note, output

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "expected": self.expected, "actual": self.actual,
                "note": self.note, "output": self.output}


def scenario_recorded_tty(state: Path) -> Result:
    """The ordinary case: the hook recorded a tty, so the tab is identified.

    This is what every healthy press should look like.
    """
    tabs = [s for s in surfaces() if s.get("tty")]
    if len(tabs) < 2:
        return Result("recorded_tty", SKIP,
                      note="need at least two cmux tabs with ttys")
    target, decoy = tabs[0], tabs[-1]
    focus_surface(decoy["ref"])
    agent = {"name": "qa-tty", "status": "blocked", "source": "claude-code",
             "cwd": str(HERE), "tty": target["tty"]}
    write_state(state, [agent])
    code, out = press(agent)
    got = active_surface()
    ok = got == target["ref"]
    return Result("recorded_tty", PASS if ok else FAIL,
                  expected=target["ref"], actual=got or "<unknown>",
                  note=f"exit={code}", output=out)


def scenario_recorded_surface(state: Path) -> Result:
    """A surface id needs no matching at all, so it must always win."""
    tabs = surfaces()
    if len(tabs) < 2:
        return Result("recorded_surface", SKIP, note="need at least two tabs")
    target, decoy = tabs[0], tabs[-1]
    focus_surface(decoy["ref"])
    agent = {"name": "qa-surface", "status": "done", "source": "codex-cli",
             "cwd": "/nonexistent/on/purpose", "surface": target["ref"]}
    write_state(state, [agent])
    code, out = press(agent)
    got = active_surface()
    ok = got == target["ref"]
    return Result("recorded_surface", PASS if ok else FAIL,
                  expected=target["ref"], actual=got or "<unknown>",
                  note=f"exit={code}; cwd is deliberately bogus so ONLY the "
                       f"surface id can succeed", output=out)


def _tabs_sharing_cwd(cwd: str) -> list[dict]:
    """Surfaces that a cwd-based resolver would consider equally good.

    Matches the resolver's own logic: a ~-abbreviated title, an exact title, or
    an explicit cwd field. Deliberately generous -- this is looking for the
    AMBIGUITY, so over-counting is the safe direction.
    """
    home = str(Path.home())
    target = str(Path(cwd)).rstrip("/").casefold()
    tilde = target.replace(home.casefold(), "~", 1)
    hits = []
    for s in surfaces():
        for key in ("cwd", "workingDirectory", "title", "name"):
            v = s.get(key)
            if not isinstance(v, str) or not v:
                continue
            got = v.strip().rstrip("/").casefold()
            if got == target or got == tilde:
                hits.append(s)
                break
    return hits


def scenario_ambiguous_tabs(state: Path) -> Result:
    """THE REGRESSION. Several tabs in one directory, no tty recorded.

    A directory is not an identity. The only correct behaviour is to refuse:
    focusing an arbitrary one of them and reporting success is the bug, and it
    is the one that produced "claude and codex buttons only open cmux".
    """
    cwd = str(HERE)
    same = _tabs_sharing_cwd(cwd)
    if len(same) < 2:
        made = [spawn_tab(cwd) for _ in range(2)]
        if not all(made):
            return Result(
                "ambiguous_tabs", SKIP,
                note=f"need two cmux tabs in {cwd} and could not spawn them; "
                     f"open two there by hand and re-run")
        same = _tabs_sharing_cwd(cwd)
        if len(same) < 2:
            return Result("ambiguous_tabs", SKIP,
                          note="spawned tabs but cmux does not report them "
                               "as sharing a directory")
    parked = active_surface()
    agent = {"name": "qa-ambig", "status": "blocked", "source": "claude-code",
             "cwd": cwd}
    write_state(state, [agent])
    code, out = press(agent)
    got = active_surface()
    refused = code != 0
    moved = got != parked
    ok = refused and not moved
    return Result(
        "ambiguous_tabs", PASS if ok else FAIL,
        expected=f"refusal, focus stays on {parked or '<unknown>'}",
        actual=f"exit={code}, focus={got or '<unknown>'}",
        note=f"{len(same)} tabs share this directory and no tty was recorded, "
             f"so the right one cannot be known; guessing is the bug",
        output=out)


def scenario_desktop_app(state: Path, app: str, source: str) -> Result:
    """A desktop-hosted agent must bring ITS app forward."""
    name = f"desktop_{source}"
    if not app_running(app):
        return Result(name, SKIP, note=f"{app} is not running")
    # Park focus elsewhere: Finder is always available.
    sh("osascript", "-e", 'tell application "Finder" to activate')
    time.sleep(SETTLE_S)
    agent = {"name": f"qa-{source}", "status": "done", "source": source,
             "cwd": str(HERE), "app": app}
    write_state(state, [agent])
    code, out = press(agent)
    got = frontmost_app()
    ok = got.lower().startswith(app.lower()[:6])
    return Result(name, PASS if ok else FAIL, expected=app,
                  actual=got or "<unknown>", note=f"exit={code}", output=out)


def scenario_quit_app_refuses(state: Path) -> Result:
    """A key for a session whose app is gone must refuse, never launch.

    Launching a blank app answers a question nobody asked, and the original
    bug in this project was exactly that: a fresh empty Terminal window.
    """
    ghost = "ThisAppDoesNotExist"
    agent = {"name": "qa-ghost", "status": "done", "source": "claude-code",
             "cwd": str(HERE), "app": ghost}
    write_state(state, [agent])
    code, out = press(agent)
    ok = code != 0 and ("not running" in out or "no focus method" in out)
    return Result("quit_app_refuses", PASS if ok else FAIL,
                  expected="non-zero exit, and it says the app is not running",
                  actual=f"exit={code}", note="", output=out)


def _workspace_of(ref: str) -> str:
    for s in surfaces():
        if (s.get("ref") or s.get("surface_ref")) == ref:
            w = s.get("workspace_ref") or s.get("workspace")
            return w if isinstance(w, str) else ""
    return ""


def scenario_workspace_switch(state: Path) -> Result:
    """A tab on a NON-selected workspace must actually become visible.

    Focusing a panel can raise the window while leaving the old workspace on
    screen. That is the "cmux opens on the default tab" symptom, and it is
    invisible to an exit code: the panel really was focused, it just was not
    the thing the operator ended up looking at.

    So this checks BOTH: the active surface is the target, and the workspace
    that came forward is the target's workspace.
    """
    tabs = [s for s in surfaces() if s.get("tty")]
    ws: dict[str, list[dict]] = {}
    for s in tabs:
        w = s.get("workspace_ref") or s.get("workspace")
        if isinstance(w, str):
            ws.setdefault(w, []).append(s)
    if len(ws) < 2:
        return Result("workspace_switch", SKIP,
                      note="need tabs on two different workspaces")
    keys = list(ws)
    target = ws[keys[-1]][0]
    decoy = ws[keys[0]][0]
    focus_surface(decoy["ref"])
    if active_surface() != decoy["ref"]:
        return Result("workspace_switch", SKIP,
                      note="could not park focus on the decoy workspace")
    agent = {"name": "qa-ws", "status": "working", "source": "codex-cli",
             "cwd": str(HERE), "tty": target.get("tty", "")}
    write_state(state, [agent])
    code, out = press(agent)
    got = active_surface()
    want_ws = target.get("workspace_ref") or target.get("workspace") or ""
    got_ws = _workspace_of(got)
    ok = got == target["ref"] and (not want_ws or got_ws == want_ws)
    return Result("workspace_switch", PASS if ok else FAIL,
                  expected=f"{target['ref']} on {want_ws or '<unknown ws>'}",
                  actual=f"{got or '<unknown>'} on {got_ws or '<unknown ws>'}",
                  note=f"exit={code}; target is on another workspace", output=out)


SCENARIOS = {
    "recorded_tty": scenario_recorded_tty,
    "recorded_surface": scenario_recorded_surface,
    "ambiguous_tabs": scenario_ambiguous_tabs,
    "workspace_switch": scenario_workspace_switch,
    "desktop_claude": lambda s: scenario_desktop_app(s, "Claude", "claude-code"),
    "desktop_codex": lambda s: scenario_desktop_app(s, "ChatGPT", "codex-cli"),
    "quit_app_refuses": scenario_quit_app_refuses,
}


# ------------------------------------------------------------------- main ---
def preflight() -> list[str]:
    problems = []
    # QA_FAKE_MACOS exists so this harness can be tested against a stub world
    # (see test_qa_focus.py). It is deliberately an explicit opt-in with an
    # unmistakable name: a harness that quietly ran on the wrong platform would
    # certify focus behaviour it never exercised, which is the most expensive
    # false positive available here.
    faking = os.environ.get("QA_FAKE_MACOS") == "1"
    if sys.platform != "darwin" and not faking:
        problems.append(
            "not macOS: this harness drives real windows, so it can only "
            "produce a meaningful verdict on the target machine")
    if not have("cmux"):
        problems.append("cmux is not on PATH")
    if not FOCUS.exists():
        problems.append(f"{FOCUS} not found")
    elif not os.access(FOCUS, os.X_OK):
        problems.append(f"{FOCUS} is not executable (chmod +x it)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="deckbridge focus QA harness")
    ap.add_argument("--only", action="append", default=[],
                    help="run only these scenarios (repeatable)")
    ap.add_argument("--list", action="store_true", help="list scenario names")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--keep", action="store_true",
                    help="leave spawned tabs open for inspection")
    args = ap.parse_args(argv)

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    problems = preflight()
    if problems:
        if args.json:
            print(json.dumps({"error": "preflight failed",
                              "problems": problems}, indent=1))
        else:
            print("cannot run:")
            for p in problems:
                print(f"  - {p}")
        return 2

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = STATE_DIR / "local_agents.json"

    chosen = args.only or list(SCENARIOS)
    unknown = [c for c in chosen if c not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}")
        return 2

    results = []
    for name in chosen:
        try:
            results.append(SCENARIOS[name](state))
        except Exception as exc:  # a broken scenario must not hide the rest
            results.append(Result(name, FAIL, note=f"harness error: {exc}"))

    if args.json:
        print(json.dumps({
            "results": [r.as_dict() for r in results],
            "passed": sum(r.status == PASS for r in results),
            "failed": sum(r.status == FAIL for r in results),
            "skipped": sum(r.status == SKIP for r in results),
        }, indent=1))
    else:
        width = max(len(r.name) for r in results)
        for r in results:
            print(f"{r.status:4}  {r.name:<{width}}  {r.note}")
            if r.status == FAIL:
                print(f"        expected: {r.expected}")
                print(f"        actual:   {r.actual}")
                if r.output:
                    for line in r.output.splitlines()[:8]:
                        print(f"        | {line}")
        p = sum(r.status == PASS for r in results)
        f = sum(r.status == FAIL for r in results)
        s = sum(r.status == SKIP for r in results)
        print(f"\n{p} passed, {f} failed, {s} skipped")

    if not args.keep:
        pass  # tabs are left alone: closing a real tab could kill live work

    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
