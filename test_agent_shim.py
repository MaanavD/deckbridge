#!/usr/bin/env python3
"""Tests for agent_shim.py plus its per-agent wrappers.

Run directly::

    python3 test_agent_shim.py

No pytest, no network, no real agent session, and no writes outside a temporary
directory.  Subprocess cases invoke the shims the way the agents do: JSON on
stdin, exit code and stdout checked.
"""
from __future__ import annotations

import json
import os
import pty
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent_shim  # noqa: E402

CLAUDE = os.path.join(HERE, "claude_shim.py")
CODEX = os.path.join(HERE, "codex_shim.py")
CURSOR = os.path.join(HERE, "cursor_shim.py")

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


def run(shim: str, state: Path, payload: dict,
        extra: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, shim, "--state", str(state)] + (extra or []),
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )


def read(state: Path) -> dict:
    with state.open(encoding="utf-8") as handle:
        return json.load(handle)


def agents(state: Path) -> dict[str, dict]:
    return {a["name"]: a for a in read(state).get("agents", [])}


# --------------------------------------------------------------------------
# shared engine
# --------------------------------------------------------------------------

def test_event_mapping() -> None:
    cases = {
        "UserPromptSubmit": "working",
        "PreToolUse": "working",
        "PostToolUse": "working",
        "PreCompact": "working",
        "PostCompact": "working",
        "SubagentStart": "working",
        "SubagentStop": "working",
        "PermissionRequest": "blocked",
        "Notification": "blocked",
        "StopFailure": "blocked",
        "Stop": "done",
        "SessionStart": "idle",
        "SessionEnd": agent_shim.EVICT,
        # Cursor's IDE/CLI hook vocabulary is lower camel case.  It must map
        # through the same public state seam rather than leaving an amber key
        # stuck forever after a turn completes.
        "beforeSubmitPrompt": "working",
        "afterAgentResponse": "working",
        "sessionStart": "idle",
        "sessionEnd": agent_shim.EVICT,
    }
    bad = [f"{e}->{agent_shim.status_for_event(e)} (want {w})"
           for e, w in cases.items() if agent_shim.status_for_event(e) != w]
    check("hook events map to deck statuses", not bad, "; ".join(bad))
    check("unknown event falls back to working",
          agent_shim.status_for_event("SomeFutureEvent") == "working")
    check("lowercase event names map too", agent_shim.status_for_event("stop") == "done")
    check("every mapped status is valid or evict",
          all(v in agent_shim.VALID_STATUSES or v == agent_shim.EVICT
              for v in agent_shim.EVENT_STATUS.values()))


def test_short_label() -> None:
    check("label comes from cwd basename",
          agent_shim.short_label("/Users/m/Documents/deckbridge", "abc") == "deckbridge")
    check("long basename is truncated for tiny keys",
          len(agent_shim.short_label("/tmp/an-extremely-long-name", "abc")) <= 11)
    check("missing cwd falls back to a claude session fragment",
          agent_shim.short_label("", "sess-9182", "claude") == "cc9182")
    check("missing cwd falls back to a codex session fragment",
          agent_shim.short_label("", "sess-9182", "codex") == "cx9182")
    check("missing cwd falls back to a cursor conversation fragment",
          agent_shim.short_label("", "conv-9182", "cursor") == "cu9182")
    check("no cwd and no session still yields a label",
          agent_shim.short_label("", "", "codex") == "cx")
    check("trailing slash does not produce an empty label",
          agent_shim.short_label("/tmp/proj/", "abc") == "proj")
    check("short_label does not apply the prefix itself",
          not agent_shim.short_label("/tmp/proj", "a", "codex").startswith("cx-"))


def test_prompt_titles() -> None:
    check("session naming request becomes a useful compact title",
          agent_shim.smart_title(
              "And then can we do better with session title naming?"
          ) == "Sess titles")
    check("a broken voice report keeps both action and subject",
          agent_shim.smart_title(
              "None of the voice ones work at all - no idea how to fix that"
          ) == "Fix voice")
    check("test-suite request does not waste space on pleasantries",
          agent_shim.smart_title(
              "Please can you run the full test suite to make sure it works?"
          ) == "Run tests")
    check("long prompts never exceed the producer label budget",
          len(agent_shim.smart_title("Investigate accessibility permissions"))
          <= agent_shim.DEFAULT_LABEL_CHARS)
    check("Claude prompt payload is accepted only for prompt events",
          agent_shim.payload_title(
              {"prompt": "Fix Discord reconnects"}, "UserPromptSubmit"
          ) == "Fix Discord")
    check("tool payload text cannot rename a session",
          agent_shim.payload_title(
              {"message": "Permission required"}, "PreToolUse"
          ) == "")


def test_prompt_title_survives_later_status_hooks() -> None:
    first = agent_shim.upsert(
        [], name="cx-Fix voice", status="working", cwd="/work/deckbridge",
        now=1.0, source="codex-cli", session_id="session-1",
        display_title="Fix voice",
    )
    later = agent_shim.upsert(
        first, name="cx-deckbridge", status="blocked", cwd="/work/deckbridge",
        now=2.0, source="codex-cli", session_id="session-1",
    )[0]
    check("later non-prompt hooks keep the useful title",
          later["name"] == "cx-Fix voice", str(later))
    check("only the compact title is retained, not its source prompt",
          later.get("display_title") == "Fix voice", str(later))


def test_agent_profiles() -> None:
    check("claude prefix is cc-", agent_shim.agent_profile("claude")["prefix"] == "cc-")
    check("codex prefix is cx-", agent_shim.agent_profile("codex")["prefix"] == "cx-")
    check("cursor prefix is cu-", agent_shim.agent_profile("cursor")["prefix"] == "cu-")
    check("cursor has its own source", agent_shim.agent_profile("cursor")["source"] == "cursor-agent")
    check("unknown agent falls back to generic",
          agent_shim.agent_profile("nope")["source"] == "agent-hook")
    check("prefixes are distinct",
          agent_shim.agent_profile("claude")["prefix"]
          != agent_shim.agent_profile("codex")["prefix"])


# --------------------------------------------------------------------------
# per-tool wrappers
# --------------------------------------------------------------------------

def test_claude_wrapper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "nested" / "cmux_state.json"
        proc = run(CLAUDE, state, {
            "session_id": "s1", "cwd": "/tmp/alpha",
            "hook_event_name": "UserPromptSubmit",
        })
        check("claude shim exits 0 and creates parent dirs",
              proc.returncode == 0 and state.exists(), proc.stderr)
        rec = agents(state).get("cc-alpha", {})
        check("claude record is prefixed cc-", bool(rec))
        check("claude record is working", rec.get("status") == "working")
        check("claude record is tagged claude-code", rec.get("source") == "claude-code")


def test_codex_wrapper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        proc = run(CODEX, state, {
            "session_id": "s1", "cwd": "/tmp/alpha",
            "hook_event_name": "PermissionRequest",
        })
        check("codex shim exits 0", proc.returncode == 0, proc.stderr)
        rec = agents(state).get("cx-alpha", {})
        check("codex record is prefixed cx-", bool(rec))
        check("codex PermissionRequest turns the key blocked",
              rec.get("status") == "blocked")
        check("codex record is tagged codex-cli", rec.get("source") == "codex-cli")

        run(CODEX, state, {
            "session_id": "s1", "cwd": "/tmp/alpha",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "None of the voice ones work at all",
        })
        titled = agents(state).get("cx-Fix voice", {})
        check("Codex prompt replaces the repo basename with a task title",
              bool(titled), str(agents(state)))
        run(CODEX, state, {
            "session_id": "s1", "cwd": "/tmp/alpha",
            "hook_event_name": "PreToolUse",
        })
        check("Codex tool hooks do not revert the task title",
              bool(agents(state).get("cx-Fix voice")), str(agents(state)))


def test_cursor_wrapper_uses_native_hook_fields() -> None:
    """Cursor names identity/workspace differently from Claude and Codex.

    The wrapper's public input is Cursor's real hook JSON: conversation_id and
    workspace_roots.  Losing either makes same-project sessions collide or
    leaves focus_agent with no exact workspace evidence.
    """
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        proc = run(CURSOR, state, {
            "conversation_id": "cursor-conversation-1",
            "workspace_roots": ["/work/alpha"],
            "hook_event_name": "beforeSubmitPrompt",
            "cursor_version": "3.14.7",
        }, ["--app", "Cursor"])
        check("cursor shim exits 0", proc.returncode == 0, proc.stderr)
        rec = agents(state).get("cu-alpha", {})
        check("cursor session is labelled distinctly", bool(rec), str(agents(state)))
        check("cursor prompt turns the key working", rec.get("status") == "working")
        check("cursor record has its own source", rec.get("source") == "cursor-agent")
        check("cursor conversation id is preserved",
              rec.get("session_id") == "cursor-conversation-1", str(rec))
        check("cursor workspace root becomes cwd", rec.get("cwd") == "/work/alpha", str(rec))
        check("cursor IDE host is recorded", rec.get("app") == "Cursor", str(rec))


def test_no_collision_same_directory() -> None:
    """The whole point of the prefixes: two tools, one folder, two keys."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        run(CLAUDE, state, {"session_id": "a", "cwd": "/work/deckbridge",
                            "hook_event_name": "UserPromptSubmit"})
        run(CODEX, state, {"session_id": "b", "cwd": "/work/deckbridge",
                           "hook_event_name": "PermissionRequest"})
        names = agents(state)
        check("claude and codex in one folder occupy two records", len(names) == 2,
              str(sorted(names)))
        check("claude key kept its own status",
              names.get("cc-deckbridge", {}).get("status") == "working")
        check("codex key kept its own status",
              names.get("cx-deckbridge", {}).get("status") == "blocked")
        check("the two keys stay distinguishable at the deck's 8-char label",
              len({n[:8] for n in names}) == 2, str(sorted(n[:8] for n in names)))


def test_no_collision_same_tool_same_directory() -> None:
    """A cwd is a label, not a session identity."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        run(CODEX, state, {"session_id": "session-a", "cwd": "/work/deckbridge",
                           "hook_event_name": "UserPromptSubmit"})
        run(CODEX, state, {"session_id": "session-b", "cwd": "/work/deckbridge",
                           "hook_event_name": "PermissionRequest"})
        records = read(state)["agents"]
        ids = {item.get("session_id") for item in records}
        check("two Codex sessions in one cwd occupy two records",
              ids == {"session-a", "session-b"}, str(records))

        ambiguous = agent_shim.apply_event(
            {"agents": records}, event="SessionEnd", name="cx-deckbridge",
            cwd="/work/deckbridge", now=time.time(), ttl=0,
            source="codex-cli", session_id="",
        )
        check("an id-less SessionEnd never evicts two same-label sessions",
              len(ambiguous["agents"]) == 2, str(ambiguous["agents"]))

        # Closing one same-label session must not evict its sibling.
        updated = agent_shim.apply_event(
            {"agents": records}, event="SessionEnd", name="cx-deckbridge",
            cwd="/work/deckbridge", now=time.time(), ttl=0,
            source="codex-cli", session_id="session-a",
        )
        remaining = {item.get("session_id") for item in updated["agents"]}
        check("SessionEnd evicts only the matching same-label session",
              remaining == {"session-b"}, str(updated["agents"]))


def test_agent_process_identity_is_sticky_and_replaced_together() -> None:
    first = agent_shim.upsert(
        [], name="cx-api", status="working", cwd="/x", now=1.0,
        source="codex-cli", session_id="s", agent_pid=101,
        agent_started_at="Fri Aug  7 10:00:00 2026",
    )
    quiet = agent_shim.upsert(
        first, name="cx-api", status="done", cwd="/x", now=2.0,
        source="codex-cli", session_id="s",
    )[0]
    check("agent pid survives a hook that cannot rediscover it",
          quiet.get("agent_pid") == 101)
    check("agent birth marker survives with its pid",
          quiet.get("agent_started_at") == "Fri Aug  7 10:00:00 2026")
    moved = agent_shim.upsert(
        [quiet], name="cx-api", status="working", cwd="/x", now=3.0,
        source="codex-cli", session_id="s", agent_pid=202,
        agent_started_at="Fri Aug  7 11:00:00 2026",
    )[0]
    check("a resumed session replaces pid and birth marker atomically",
          (moved.get("agent_pid"), moved.get("agent_started_at"))
          == (202, "Fri Aug  7 11:00:00 2026"), str(moved))


def test_stdout_is_empty_for_hooks() -> None:
    """Codex parses hook stdout; noise there causes warnings."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        for shim, label in ((CODEX, "codex"), (CLAUDE, "claude")):
            proc = run(shim, state, {"session_id": "s", "cwd": "/tmp/q",
                                     "hook_event_name": "Stop"})
            check(f"{label} shim writes nothing to stdout", proc.stdout == "",
                  repr(proc.stdout))
        proc = run(CODEX, state, {"session_id": "s", "cwd": "/tmp/q",
                                  "hook_event_name": "Stop"}, extra=["--print"])
        check("--print still emits valid JSON for debugging",
              json.loads(proc.stdout).get("agents") is not None)


def test_upsert_and_multiple_agents() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        run(CODEX, state, {"session_id": "s1", "cwd": "/tmp/alpha",
                           "hook_event_name": "UserPromptSubmit"})
        run(CODEX, state, {"session_id": "s1", "cwd": "/tmp/alpha",
                           "hook_event_name": "PermissionRequest"})
        check("repeat events update in place, no duplicate",
              len(read(state)["agents"]) == 1)
        check("latest event wins",
              agents(state)["cx-alpha"]["status"] == "blocked")
        run(CODEX, state, {"session_id": "s2", "cwd": "/tmp/beta",
                           "hook_event_name": "Stop"})
        names = agents(state)
        check("a second directory adds a second agent", len(names) == 2)
        check("Stop marks the second agent done", names["cx-beta"]["status"] == "done")
        check("first agent is untouched by the second",
              names["cx-alpha"]["status"] == "blocked")


def test_session_end_evicts_claude_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        run(CLAUDE, state, {"session_id": "s", "cwd": "/tmp/gamma",
                            "hook_event_name": "Stop"})
        check("agent present before SessionEnd", "cc-gamma" in agents(state))
        run(CLAUDE, state, {"session_id": "s", "cwd": "/tmp/gamma",
                            "hook_event_name": "SessionEnd"})
        check("SessionEnd removes the record", "cc-gamma" not in agents(state))
        check("state file remains valid JSON after eviction",
              read(state).get("agents") == [])


def test_ttl_prunes_dead_agents() -> None:
    """Codex has no SessionEnd, so the ttl is its only eviction path."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        state.write_text(json.dumps({"agents": [
            {"name": "cx-zombie", "status": "working", "cwd": "/x",
             "updated_at": time.time() - 5000},
            {"name": "cx-fresh", "status": "working", "cwd": "/y",
             "updated_at": time.time()},
            {"name": "from-cmux", "status": "idle", "cwd": "/z"},
        ]}), encoding="utf-8")
        run(CODEX, state, {"session_id": "s", "cwd": "/tmp/delta",
                           "hook_event_name": "Stop"})
        names = set(agents(state))
        check("stale agent is evicted by ttl", "cx-zombie" not in names)
        check("recent agent survives", "cx-fresh" in names)
        check("records without updated_at are preserved (cmux_shim.sh owns them)",
              "from-cmux" in names)
        check("new agent was added", "cx-delta" in names)


def test_ttl_zero_disables_pruning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        state.write_text(json.dumps({"agents": [
            {"name": "ancient", "status": "idle", "cwd": "/x", "updated_at": 1.0},
        ]}), encoding="utf-8")
        run(CODEX, state, {"session_id": "s", "cwd": "/tmp/eps",
                           "hook_event_name": "Stop"}, extra=["--ttl", "0"])
        check("--ttl 0 keeps everything", "ancient" in agents(state))


def test_prune_keeps_an_exactly_live_quiet_session() -> None:
    """Wall-clock TTL must not erase a session the OS proves is live."""
    marker = agent_shim.process_started_at(os.getpid())
    record = {
        "name": "cx-quiet", "source": "codex-cli", "status": "done",
        "updated_at": 1.0, "agent_pid": os.getpid(),
        "agent_started_at": marker,
    }
    kept = agent_shim.prune([record], now=10_000.0, ttl=10.0)
    check("an exactly live quiet session survives hook-side TTL pruning",
          bool(marker) and kept == [record], str(kept))
    dead = dict(record, agent_pid=999999)
    check("a provably dead identified session is still pruned",
          agent_shim.prune([dead], now=10_000.0, ttl=10.0) == [])


def test_corrupt_and_missing_input() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        state.write_text("{ this is not json", encoding="utf-8")
        proc = run(CODEX, state, {"session_id": "s", "cwd": "/tmp/zeta",
                                  "hook_event_name": "Stop"})
        check("corrupt existing state is replaced, not fatal", proc.returncode == 0)
        check("agent still recorded after corrupt state", "cx-zeta" in agents(state))
        for shim, label in ((CODEX, "codex"), (CLAUDE, "claude")):
            proc = subprocess.run(
                [sys.executable, shim, "--state", str(state)],
                input="", capture_output=True, text=True, timeout=30,
            )
            check(f"{label} empty stdin exits 0 (never breaks the session)",
                  proc.returncode == 0)


def test_flags_pass_through_wrapper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        run(CODEX, state, {"session_id": "s", "cwd": "/tmp/theta",
                           "hook_event_name": "Stop"}, extra=["--prefix", ""])
        check("--prefix '' drops the tool prefix through the wrapper",
              "theta" in agents(state))
        run(CODEX, state, {"session_id": "s", "cwd": "/tmp/iota",
                           "hook_event_name": "Stop"}, extra=["--name", "custom"])
        check("--name overrides the label entirely", "custom" in agents(state))


def test_atomic_no_temp_left_behind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        for i in range(5):
            run(CODEX, state, {"session_id": f"s{i}", "cwd": f"/tmp/p{i}",
                               "hook_event_name": "UserPromptSubmit"})
        leftovers = [p.name for p in Path(tmp).iterdir() if ".tmp" in p.name]
        check("no temp files left behind", not leftovers, str(leftovers))
        check("all five agents recorded", len(agents(state)) == 5)


def test_concurrent_hooks_do_not_corrupt() -> None:
    """Codex launches matching hooks concurrently; no update may be lost.

    Atomic replacement alone only protects readers from partial JSON.  Without
    a lock around the whole read/modify/write transaction, every process can
    read the same empty document and the last rename silently discards all the
    other sessions.  Supplying the process identity avoids unrelated ``ps``
    timing and makes that race deterministic.
    """
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        count = 24
        procs = [
            subprocess.Popen(
                [sys.executable, CODEX, "--state", str(state),
                 "--agent-pid", "999999", "--agent-started-at", "fixture"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=True,
            )
            for _ in range(count)
        ]
        # Feed every process before waiting for any one of them.  Calling
        # communicate() in this loop used to serialize the supposedly
        # concurrent test and let the lost-update bug pass indefinitely.
        for i, proc in enumerate(procs):
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({
                "session_id": f"s{i}", "cwd": f"/tmp/c{i}",
                "hook_event_name": "PreToolUse",
            }))
            proc.stdin.close()
        for proc in procs:
            proc.wait(timeout=30)
        check("all concurrent hooks exit 0", all(p.returncode == 0 for p in procs))
        try:
            data = read(state)
            valid = isinstance(data.get("agents"), list)
        except ValueError:
            valid = False
        check("state file is still valid JSON after concurrent writes", valid)
        recorded = data.get("agents", []) if valid else []
        check("every concurrent session survives the read-modify-write race",
              len(recorded) == count,
              f"recorded {len(recorded)} of {count}")


def test_shares_contract_with_cmux() -> None:
    """The connector reads name/status/cwd; extra keys must not break it."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        run(CODEX, state, {"session_id": "s", "cwd": "/tmp/kappa",
                           "hook_event_name": "PreToolUse"})
        record = agents(state)["cx-kappa"]
        check("record carries the required contract keys",
              {"name", "status", "cwd"}.issubset(record))
        check("status is one of the four valid values",
              record["status"] in agent_shim.VALID_STATUSES)


def test_tty_is_recorded_and_sticky() -> None:
    """The hook runs INSIDE the agent's terminal, so it can read the tty rather
    than have focus_agent.sh guess it later from titles that agents rewrite."""
    agents = agent_shim.upsert(
        [], name="cc-api", status="working", cwd="/x", now=1.0,
        source="claude-code", tty="ttys013",
    )
    check("upsert records the tty it was given", agents[0].get("tty") == "ttys013")

    # A later hook may have no controlling terminal (piped stdin, background
    # wrapper). Losing the tty then would break focus for the rest of the
    # session, so a blank tty must not erase a known one.
    agents = agent_shim.upsert(
        agents, name="cc-api", status="idle", cwd="/x", now=2.0,
        source="claude-code", tty="",
    )
    check("a blank tty does not erase a previously recorded one",
          agents[0].get("tty") == "ttys013")

    # But the agent can genuinely move to a new surface.
    agents = agent_shim.upsert(
        agents, name="cc-api", status="working", cwd="/x", now=3.0,
        source="claude-code", tty="ttys020",
    )
    check("a new tty replaces the old one", agents[0].get("tty") == "ttys020")

    # Omitted entirely: the key must be absent, not present-and-empty, so
    # downstream consumers can tell "unknown" from "none".
    plain = agent_shim.upsert(
        [], name="cx-x", status="idle", cwd="/y", now=1.0, source="codex-cli",
    )
    check("an unknown tty is omitted rather than stored blank",
          "tty" not in plain[0])


def test_surface_is_read_from_the_agent_environment() -> None:
    """A surface id the agent already knows beats anything derived later.

    A tty must be matched against the cmux tree; a cwd is not an identity at
    all, because every tab open in a repo reports the same one. That is the
    bug this exists to kill: with no tty recorded, a press fell back to a cwd
    match and focused an arbitrary tab out of eight.
    """
    check("a cmux surface variable is picked up",
          agent_shim.surface_from_env({"CMUX_SURFACE": "surface:29"}) == "surface:29")
    check("the documented cmux surface UUID variable is picked up",
          agent_shim.surface_from_env({
              "CMUX_SURFACE_ID": "B854DA82-6647-4ED2-AC3E-0F679082354D",
          }) == "B854DA82-6647-4ED2-AC3E-0F679082354D")
    check("an alternative variable name also works",
          agent_shim.surface_from_env({"CMUX_PANE_ID": "surface:31"}) == "surface:31")
    check("a blank environment yields nothing rather than a guess",
          agent_shim.surface_from_env({}) == "")
    check("whitespace-only values are treated as absent",
          agent_shim.surface_from_env({"CMUX_SURFACE": "   "}) == "")
    check("the exact Herdr pane is read from the agent environment",
          agent_shim.herdr_pane_from_env({"HERDR_PANE_ID": "w2:p7"}) == "w2:p7")


def test_surface_is_recorded_and_sticky() -> None:
    agents = agent_shim.upsert(
        [], name="cc-api", status="working", cwd="/x", now=1.0,
        source="claude-code", surface="surface:29",
    )
    check("upsert records the surface", agents[0].get("surface") == "surface:29")
    agents = agent_shim.upsert(
        agents, name="cc-api", status="idle", cwd="/x", now=2.0,
        source="claude-code", surface="",
    )
    check("a blank surface does not erase a known one",
          agents[0].get("surface") == "surface:29")
    plain = agent_shim.upsert(
        [], name="cx-x", status="idle", cwd="/y", now=1.0, source="codex-cli",
    )
    check("an unknown surface is omitted rather than stored blank",
          "surface" not in plain[0])

    herdr = agent_shim.upsert(
        [], name="cx-h", status="working", cwd="/h", now=1.0,
        source="codex-cli", herdr_pane="w1:p4",
    )
    check("upsert records the exact Herdr pane",
          herdr[0].get("herdr_pane") == "w1:p4")
    herdr = agent_shim.upsert(
        herdr, name="cx-h", status="idle", cwd="/h", now=2.0,
        source="codex-cli", herdr_pane="",
    )
    check("a blank Herdr pane does not erase a known one",
          herdr[0].get("herdr_pane") == "w1:p4")


def test_surface_survives_a_real_hook_invocation() -> None:
    """End to end, with the variable set the way a cmux shell would set it."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        # Clear the documented UUID variable inherited when this suite itself
        # runs inside cmux, so this case isolates the legacy alias it names.
        env = dict(os.environ, CMUX_SURFACE_ID="", CMUX_SURFACE="surface:29")
        subprocess.run(
            [sys.executable, CLAUDE, "--state", str(state)],
            input=json.dumps({"session_id": "s", "cwd": "/w/proj",
                              "hook_event_name": "UserPromptSubmit"}),
            capture_output=True, text=True, timeout=30, env=env,
        )
        rec = read(state)["agents"][0]
        check("the surface reached the state file",
              rec.get("surface") == "surface:29", str(rec.get("surface")))


def test_session_id_is_recorded_and_sticky() -> None:
    """The session id must be STORED, not just used to build a display name.

    It was read from the hook payload, spliced into the label, and then
    dropped. Nothing wrote it to state, so the connector substituted an empty
    ``--session`` on every press and no exact-tab route could fire -- which is
    why a Claude or Codex key raised the app on its default tab instead of the
    conversation. It is the only id the agent and its host app both know.
    """
    agents = agent_shim.upsert(
        [], name="cc-api", status="working", cwd="/x", now=1.0,
        source="claude-code", session_id="550e8400-e29b-41d4-a716-446655440000",
    )
    check("upsert records the session id it was given",
          agents[0].get("session_id") == "550e8400-e29b-41d4-a716-446655440000")

    # Same stickiness rule as tty and app: a later hook may fire from a context
    # with no payload, and forgetting a good id is worse than never having one.
    agents = agent_shim.upsert(
        agents, name="cc-api", status="idle", cwd="/x", now=2.0,
        source="claude-code", session_id="",
    )
    check("a blank session id does not erase a known one",
          agents[0].get("session_id") == "550e8400-e29b-41d4-a716-446655440000")

    plain = agent_shim.upsert(
        [], name="cx-x", status="idle", cwd="/y", now=1.0, source="codex-cli",
    )
    check("an unknown session id is omitted rather than stored blank",
          "session_id" not in plain[0])


def test_session_id_survives_a_real_hook_invocation() -> None:
    """End to end: payload in, session id in the state file the deck reads."""
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state.json"
        run(CLAUDE, state, {"session_id": "550e8400-e29b-41d4-a716-446655440000",
                            "cwd": "/w/proj", "hook_event_name": "UserPromptSubmit"})
        agents = read(state)["agents"]
        check("one agent recorded", len(agents) == 1, str(agents))
        check("the session id reached the state file",
              agents[0].get("session_id") == "550e8400-e29b-41d4-a716-446655440000",
              str(agents[0].get("session_id")))


def test_tty_flows_through_apply_event() -> None:
    state = agent_shim.apply_event(
        {}, event="UserPromptSubmit", name="cc-api", cwd="/x", now=1.0,
        ttl=0, source="claude-code", tty="ttys007",
    )
    check("apply_event plumbs the tty into the record",
          state["agents"][0].get("tty") == "ttys007")


def test_host_app_is_recorded_and_sticky() -> None:
    """A Claude Code or Codex session running in the DESKTOP app has no tty and
    no cmux surface, so the tty path cannot reach it at all. The hook can read
    its own bundle ancestry, which turns that guess into a fact."""
    agents = agent_shim.upsert(
        [], name="cc-api", status="working", cwd="/x", now=1.0,
        source="claude-code", app="Claude",
    )
    check("upsert records the host app it was given",
          agents[0].get("app") == "Claude")

    agents = agent_shim.upsert(
        agents, name="cc-api", status="idle", cwd="/x", now=2.0,
        source="claude-code", app="",
    )
    check("a blank host app does not erase a previously recorded one",
          agents[0].get("app") == "Claude")

    agents = agent_shim.upsert(
        agents, name="cc-api", status="working", cwd="/x", now=3.0,
        source="claude-code", app="cmux",
    )
    check("a moved session records its new host app",
          agents[0].get("app") == "cmux")

    plain = agent_shim.upsert(
        [], name="cx-x", status="idle", cwd="/y", now=1.0, source="codex-cli",
    )
    check("an unknown host app is omitted rather than stored blank",
          "app" not in plain[0])

    # tty and app are independent: recording one must not drop the other.
    both = agent_shim.upsert(
        [], name="cc-b", status="working", cwd="/x", now=1.0,
        source="claude-code", tty="ttys013", app="cmux",
    )
    both = agent_shim.upsert(
        both, name="cc-b", status="idle", cwd="/x", now=2.0,
        source="claude-code", tty="", app="",
    )
    check("tty and host app both carry forward together",
          both[0].get("tty") == "ttys013" and both[0].get("app") == "cmux")


def test_host_app_flows_through_apply_event() -> None:
    state = agent_shim.apply_event(
        {}, event="UserPromptSubmit", name="cc-api", cwd="/x", now=1.0,
        ttl=0, source="claude-code", app="Claude",
    )
    check("apply_event plumbs the host app into the record",
          state["agents"][0].get("app") == "Claude")


def test_host_app_never_raises() -> None:
    """Runs on Linux in CI and macOS in production; must return a string on
    both, and never take down the user's agent session."""
    value = agent_shim.host_app()
    check("host_app returns a string", isinstance(value, str))
    check("host_app returns a bare bundle name, never a path",
          "/" not in value)
    check("pid 1 has no bundle ancestor and yields empty",
          agent_shim.host_app(1) == "")


def test_host_app_reads_a_bundle_from_the_ancestry() -> None:
    """The real signal: an ancestor whose executable lives inside a .app.
    Exercised with a fake `ps` so it is testable off macOS."""
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "ps"
        # pid 100 -> node under Claude.app, parent 1. Keep ppid first and comm
        # last, matching the macOS query: when comm precedes another column BSD
        # ps truncates `/Applications/Claude.app/...` to `/Applications/Cl`.
        fake.write_text(
            "#!/bin/sh\n"
            "echo '1 /Applications/Claude.app/Contents/MacOS/node'\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}:{old}"
        try:
            check("a .app ancestor is reported as the host app",
                  agent_shim.host_app(100) == "Claude",
                  agent_shim.host_app(100))
        finally:
            os.environ["PATH"] = old

        fake.write_text(
            "#!/bin/sh\n"
            "echo '1 /Applications/Cursor.app/Contents/MacOS/Cursor'\n",
            encoding="utf-8",
        )
        os.environ["PATH"] = f"{bin_dir}:{old}"
        try:
            check("Cursor bundle ancestry is recorded as Cursor",
                  agent_shim.host_app(100) == "Cursor",
                  agent_shim.host_app(100))
        finally:
            os.environ["PATH"] = old

        # A plain binary with no bundle must yield "" rather than a guess.
        fake.write_text("#!/bin/sh\necho '1 /usr/bin/zsh'\n", encoding="utf-8")
        os.environ["PATH"] = f"{bin_dir}:{old}"
        try:
            check("a non-bundled ancestor yields no host app",
                  agent_shim.host_app(100) == "")
        finally:
            os.environ["PATH"] = old

        # Reproduce BSD ps's column-width trap. The fake returns a truncated
        # path for the old comm-first query and the complete bundle only when
        # comm is the final column. This was observed on the target Mac and is
        # why a live cmux-hosted Codex session recorded no host application.
        fake.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'-o ppid= -o comm='*) "
            "echo '1 /Applications/cmux.app/Contents/MacOS/cmux' ;;\n"
            "  *) echo '/Applications/cm 1' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        os.environ["PATH"] = f"{bin_dir}:{old}"
        try:
            check("macOS full bundle path survives the ps column layout",
                  agent_shim.host_app(100) == "cmux",
                  agent_shim.host_app(100))
        finally:
            os.environ["PATH"] = old


def test_current_tty_never_raises() -> None:
    """os.ttyname raises when a stream is a pipe; a hook must never crash."""
    value = agent_shim.current_tty()
    check("current_tty returns a string", isinstance(value, str))
    check("current_tty returns a bare name, never a /dev path",
          not value.startswith("/dev/"))


def test_tty_survives_fully_piped_fds() -> None:
    """The real Claude Code case: stdin, stdout and stderr are all pipes.

    the test Mac produced a state record with no tty at all, because detection
    only looked at file descriptors. Redirection does not detach the
    controlling terminal, so the tty is still discoverable -- and it must be,
    or focus falls back to matching titles that cannot identify an agent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        state = os.path.join(tmp, "state.json")
        pid, master = pty.fork()
        if pid == 0:
            # Child owns a pty, standing in for a cmux surface. The hook it
            # spawns gets JSON on stdin with stdout and stderr captured, which
            # is exactly how Claude Code invokes one.
            subprocess.run(
                [sys.executable, os.path.join(HERE, "agent_shim.py"),
                 "--agent", "claude", "--state", state, "--event", "PreToolUse"],
                input=json.dumps({"session_id": "u1", "cwd": tmp}),
                capture_output=True, text=True,
            )
            os._exit(0)
        os.waitpid(pid, 0)
        os.close(master)
        try:
            agent = json.loads(Path(state).read_text())["agents"][0]
        except Exception as exc:                       # pragma: no cover
            check("hook wrote state from inside a pty", False, str(exc))
            return
    tty = agent.get("tty", "")
    check("tty is recorded even when every hook fd is a pipe",
          bool(tty), "state written without a tty; focus would fall back to titles")
    check("recorded tty is a real device, not the generic 'tty'",
          tty not in ("", "tty"),
          "/dev/tty reports the generic name, which matches no cmux surface")


def test_codex_permission_completion_clears_attention() -> None:
    """An approved permission must not leave Codex stuck at NEEDS YOU."""
    common = {
        "name": "cx-deckbridge",
        "cwd": "/work/deckbridge",
        "ttl": 0,
        "source": "codex-cli",
        "session_id": "codex-session",
    }
    state = agent_shim.apply_event(
        {}, event="PermissionRequest", now=1.0, **common)
    check("Codex permission requests need attention",
          state["agents"][0]["status"] == "blocked")

    state = agent_shim.apply_event(
        state, event="PostToolUse", now=2.0, **common)
    check("Codex tool completion clears permission attention",
          state["agents"][0]["status"] == "working")


def test_claude_notifications_preserve_real_attention_state() -> None:
    common = {
        "name": "cc-Fix auth", "cwd": "/work/project", "ttl": 0,
        "source": "claude-code", "session_id": "claude-session",
    }
    state = agent_shim.apply_event(
        {}, event="Stop", now=1.0, **common)
    idle_status = agent_shim.status_for_payload(
        "Notification", {"notification_type": "idle_prompt"})
    state = agent_shim.apply_event(
        state, event="Notification", now=2.0,
        status_override=idle_status, **common)
    check("Claude idle notification keeps a finished turn done",
          state["agents"][0]["status"] == "done")

    permission_status = agent_shim.status_for_payload(
        "Notification", {"notification_type": "permission_prompt"})
    state = agent_shim.apply_event(
        state, event="Notification", now=3.0,
        status_override=permission_status, **common)
    check("Claude permission notification still needs attention",
          state["agents"][0]["status"] == "blocked")
    check("notification display title cannot rename the task",
          agent_shim.payload_title({
              "notification_type": "permission_prompt",
              "title": "Permission needed",
          }, "Notification") == "")


def test_tty_column_formats() -> None:
    """A tty name reaches us in several shapes and must normalise to one."""
    cases = [
        ("/dev/ttys013", "ttys013", "macOS device path"),
        ("/dev/pts/3", "pts/3", "Linux pts keeps its subdirectory"),
        ("ttys013", "ttys013", "already short"),
        ("  ttys004  ", "ttys004", "ps columns arrive padded"),
    ]
    for raw, want, why in cases:
        got = agent_shim._short_tty(raw)
        check(f"tty normalising: {why}", got == want, f"{raw!r} -> {got!r}")


def test_tty_ancestry_handles_processes_without_a_terminal() -> None:
    """ps prints ?? for a daemon; the walk must skip it, not return garbage."""
    got = agent_shim._tty_from_ancestors(1)
    check("ancestry lookup on pid 1 yields no terminal", got == "",
          f"got {got!r}")
    got = agent_shim._tty_from_ancestors(os.getpid())
    check("ancestry lookup accepts an explicit pid and returns a string",
          isinstance(got, str))
    check("ancestry result is never a raw ps placeholder",
          got not in ("??", "?", "-"), f"got {got!r}")


def main() -> int:
    test_event_mapping()
    test_codex_permission_completion_clears_attention()
    test_claude_notifications_preserve_real_attention_state()
    test_prompt_titles()
    test_prompt_title_survives_later_status_hooks()
    test_tty_is_recorded_and_sticky()
    test_surface_is_read_from_the_agent_environment()
    test_surface_is_recorded_and_sticky()
    test_surface_survives_a_real_hook_invocation()
    test_session_id_is_recorded_and_sticky()
    test_session_id_survives_a_real_hook_invocation()
    test_tty_flows_through_apply_event()
    test_host_app_is_recorded_and_sticky()
    test_host_app_flows_through_apply_event()
    test_host_app_never_raises()
    test_host_app_reads_a_bundle_from_the_ancestry()
    test_current_tty_never_raises()
    test_tty_survives_fully_piped_fds()
    test_tty_column_formats()
    test_tty_ancestry_handles_processes_without_a_terminal()
    test_short_label()
    test_agent_profiles()
    test_claude_wrapper()
    test_codex_wrapper()
    test_cursor_wrapper_uses_native_hook_fields()
    test_no_collision_same_directory()
    test_no_collision_same_tool_same_directory()
    test_agent_process_identity_is_sticky_and_replaced_together()
    test_stdout_is_empty_for_hooks()
    test_upsert_and_multiple_agents()
    test_session_end_evicts_claude_only()
    test_ttl_prunes_dead_agents()
    test_ttl_zero_disables_pruning()
    test_prune_keeps_an_exactly_live_quiet_session()
    test_corrupt_and_missing_input()
    test_flags_pass_through_wrapper()
    test_atomic_no_temp_left_behind()
    test_concurrent_hooks_do_not_corrupt()
    test_shares_contract_with_cmux()
    total = PASSED + FAILED
    print(f"\n{PASSED}/{total} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
