#!/usr/bin/env python3
"""Tests for connector_agents.py, the unified keys 0-9 agent pool.

Run directly::

    python3 test_connector_agents.py

No pytest, no network, no hub. Writes only inside a temporary directory.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import connector_agents as connector_module  # noqa: E402

from connector_agents import (  # noqa: E402
    AgentConnector, LocalLivenessProbe, SlotMap, agent_key, dedupe_labels, decay_stale,
    drop_uninteresting, face_for, normalize_status, read_agents,
    read_launchers, read_shortcuts, launcher_face, DEFAULT_LAUNCHERS,
    DEFAULT_SHORTCUTS, LAUNCHER_COLOR,
    SOURCE_BADGE, STALE_WORKING_S, STATUS_FACE, STATUS_ORDER, slot_priority,
)
from connection_runtime import HealthReporter  # noqa: E402

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


def write(path: Path, agents: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": agents}), encoding="utf-8")


def test_normalize_status() -> None:
    cases = {
        "working": "working", "running": "working", "BUSY": "working",
        "blocked": "blocked", "needs_input": "blocked", "approval": "blocked",
        "done": "done", "completed": "done",
        "idle": "idle", "": "idle", "nonsense": "idle",
    }
    bad = [f"{k}->{normalize_status(k)}" for k, v in cases.items()
           if normalize_status(k) != v]
    check("producer statuses normalize to the four deck statuses", not bad, "; ".join(bad))


def test_label_cleaning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "local.json"
        write(p, [
            {"name": "cc-sample-api", "status": "working", "cwd": "/x"},
            {"name": "cx-sample_api", "status": "blocked", "cwd": "/x"},
        ])
        agents = read_agents(p, source_default="cmux")
        names = [a["name"] for a in agents]
        check("cc-/cx- prefixes are stripped from labels", names == ["sample api", "sample api"],
              str(names))
        deduped = [a["name"] for a in dedupe_labels(agents)]
        check("identical labels get a numeric suffix", deduped == ["sample api", "sample api 2"],
              str(deduped))


def test_badges_identify_the_tool() -> None:
    check("hermes badge", SOURCE_BADGE["hermes-discord"] == "H")
    check("hermes-ssh badge", SOURCE_BADGE["hermes-ssh"] == "S")
    check("claude badge", SOURCE_BADGE["claude-code"] == "C")
    check("codex badge", SOURCE_BADGE["codex-cli"] == "X")
    check("cursor badge is distinct", SOURCE_BADGE["cursor-agent"] == "R")
    base_sources = ("hermes-discord", "hermes-ssh", "hermes-health",
                    "claude-code", "codex-cli", "cursor-agent", "herdr", "cmux")
    check("primary source badges are unique",
          len({SOURCE_BADGE[s] for s in base_sources}) == len(base_sources))
    face = face_for({"name": "proj", "status": "working", "source": "codex-cli"})
    check("face carries the source badge", face["badge"] == "X")
    check("working agents shimmer", face["effect"] == "shimmer")
    check("face label has no tool prefix", face["label"] == "proj")


def test_ssh_hosted_hermes_agents() -> None:
    """Hermes agents started via `cmux ssh hermes` have no thread, only a session id."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        hermes = Path(tmp) / "hermes.json"
        write(hermes, [
            {"name": "Provider Au", "status": "working", "thread_id": "",
             "session_id": "20260805_a1", "url": "", "cwd": "/home/hermes",
             "source": "hermes-ssh", "last_activity_at": now},
            {"name": "Other task", "status": "blocked", "thread_id": "",
             "session_id": "20260805_b2", "url": "", "cwd": "/home/hermes",
             "source": "hermes-ssh", "last_activity_at": now},
            {"name": "Audio Audit", "status": "working", "thread_id": "999",
             "session_id": "20260805_c3",
             "url": "https://discord.com/channels/1/999",
             "source": "hermes-discord", "last_activity_at": now},
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=hermes,
                           local_state=Path(tmp) / "none.json")
        agents = c.collect(now)
        check("ssh-hosted and discord Hermes agents both survive", len(agents) == 3,
              str(len(agents)))
        # The critical bug this guards: two ssh sessions share a blank
        # thread_id, so identity must fall back to the session id, not the name.
        keys = {agent_key(a) for a in agents}
        check("two ssh sessions are two distinct agents", len(keys) == 3, str(keys))

        faces = c.build_faces(agents)
        agent_faces = {i: faces[i] for i in c._agent_keys}
        check("all three get their own key", len(agent_faces) == 3)
        badges = sorted(f["badge"] for f in agent_faces.values())
        check("ssh agents show the S badge next to the H thread",
              badges == ["H", "S", "S"], str(badges))

        # A press on an ssh agent must pass the session id, since there is no
        # URL to open and its cwd is a path on the remote host.
        ssh_agent = next(a for a in agents if a["source"] == "hermes-ssh")
        check("an ssh agent carries its session id", bool(ssh_agent["session_id"]))
        check("an ssh agent has no jump URL", ssh_agent["url"] == "")
        out = Path(tmp) / "pressed.txt"
        c.focus_cmd = f"printf '%s|%s\\n' {{source}} {{session_id}} >> {out}"
        c.focus(ssh_agent)
        text = out.read_text(encoding="utf-8").strip()
        check("focus receives the source and session id",
              text == f"hermes-ssh|{ssh_agent['session_id']}", text)


def test_remote_hermes_maps_to_a_unique_herdr_ssh_pane() -> None:
    """A remote Hermes DB id needs a local visible-pane identity to be focusable."""
    resolver_type = getattr(connector_module, "HerdrSshPaneResolver", None)
    if resolver_type is None:
        check("remote Hermes has a HerdR SSH-pane resolver", False,
              "HerdrSshPaneResolver is missing")
        return

    pane_list = {
        "result": {"panes": [
            {"pane_id": "w4:p1", "tab_id": "w4:t1", "workspace_id": "w4",
             "agent_status": "unknown"},
            {"pane_id": "w2:p1", "tab_id": "w2:t1", "workspace_id": "w2",
             "agent": "claude", "agent_status": "working"},
        ]}
    }

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ["pane", "list"]:
            payload = pane_list
        elif argv[1:3] == ["pane", "process-info"] and argv[-1] == "w4:p1":
            payload = {"result": {"process_info": {
                "pane_id": "w4:p1",
                "foreground_processes": [{"argv": ["ssh", "hermes"]}],
            }}}
        else:
            payload = {"result": {"process_info": {
                "pane_id": argv[-1],
                "foreground_processes": [{"argv": ["claude"]}],
            }}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    resolver = resolver_type(runner=fake_run, cache_seconds=0)
    remote = [{
        "name": "Review updates", "status": "working",
        "source": "hermes-ssh", "session_id": "remote-1",
        "ssh_host": "hermes", "herdr_pane": "",
    }]
    mapped = resolver.enrich(remote)
    check("unique Hermes SSH viewer gains its exact HerdR pane",
          mapped[0].get("herdr_pane") == "w4:p1", str(mapped))
    check("resolver carries the enclosing tab and workspace",
          mapped[0].get("herdr_tab") == "w4:t1"
          and mapped[0].get("herdr_workspace") == "w4", str(mapped))

    ambiguous = resolver.enrich([
        dict(remote[0], session_id="remote-1"),
        dict(remote[0], session_id="remote-2"),
    ])
    check("two live remote agents never guess the same visible pane",
          not any(item.get("herdr_pane") for item in ambiguous), str(ambiguous))


def test_agent_identity_survives_status_change() -> None:
    """Slot pinning depends on identity being stable across status changes."""
    a = {"name": "proj", "status": "working", "source": "claude-code", "thread_id": ""}
    b = dict(a, status="blocked")
    check("identity ignores status", agent_key(a) == agent_key(b))
    hermes1 = {"name": "Audio Audit", "thread_id": "123", "source": "hermes-discord"}
    hermes2 = {"name": "Audio Audit renamed", "thread_id": "123", "source": "hermes-discord"}
    check("hermes identity is the thread id, not the title",
          agent_key(hermes1) == agent_key(hermes2))
    check("different tools in one directory are different agents",
          agent_key({"name": "proj", "source": "claude-code"})
          != agent_key({"name": "proj", "source": "codex-cli"}))
    check("session ids are scoped by source",
          agent_key({"name": "proj", "source": "claude-code", "session_id": "s1"})
          != agent_key({"name": "proj", "source": "codex-cli", "session_id": "s1"}))


def test_slots_are_pinned() -> None:
    """The core UX guarantee: a key does not change meaning under your finger."""
    slots = SlotMap(10)
    a = {"name": "alpha", "status": "idle", "source": "cmux", "updated_at": 1.0}
    b = {"name": "beta", "status": "blocked", "source": "cmux", "updated_at": 2.0}
    first = slots.assign([a, b])
    pos_a = [s for s, ag in first.items() if ag["name"] == "alpha"][0]
    pos_b = [s for s, ag in first.items() if ag["name"] == "beta"][0]

    # alpha becomes the most urgent; beta calms down. Slots must NOT swap.
    a2 = dict(a, status="blocked", updated_at=9.0)
    b2 = dict(b, status="done")
    second = slots.assign([a2, b2])
    check("an agent keeps its slot when its status changes",
          [s for s, ag in second.items() if ag["name"] == "alpha"][0] == pos_a)
    check("the other agent also keeps its slot",
          [s for s, ag in second.items() if ag["name"] == "beta"][0] == pos_b)

    # A newcomer must not displace anyone.
    c = {"name": "gamma", "status": "blocked", "source": "cmux", "updated_at": 5.0}
    third = slots.assign([a2, b2, c])
    check("a newcomer takes a free slot without moving anyone",
          [s for s, ag in third.items() if ag["name"] == "alpha"][0] == pos_a
          and [s for s, ag in third.items() if ag["name"] == "beta"][0] == pos_b)
    check("newcomer got its own slot", len(third) == 3)

    # Departure frees the slot for reuse.
    fourth = slots.assign([a2, c])
    check("a departed agent releases its slot", len(fourth) == 2)
    check("survivors still hold their slots",
          [s for s, ag in fourth.items() if ag["name"] == "alpha"][0] == pos_a)


def test_full_board_prefers_urgent_newcomers() -> None:
    slots = SlotMap(3)
    base = [
        {"name": f"old{i}", "status": "done", "source": "cmux", "updated_at": 1.0}
        for i in range(3)
    ]
    slots.assign(base)
    urgent = {"name": "urgent", "status": "blocked", "source": "cmux", "updated_at": 9.0}
    placed = slots.assign(base + [urgent])
    check("a full board does not evict pinned agents for a newcomer",
          len(placed) == 3 and "urgent" not in [a["name"] for a in placed.values()])
    # With one slot free, the most urgent newcomer wins it.
    slots2 = SlotMap(3)
    slots2.assign(base[:2])
    calm = {"name": "calm", "status": "done", "source": "cmux", "updated_at": 8.0}
    placed2 = slots2.assign(base[:2] + [calm, urgent])
    names = [a["name"] for a in placed2.values()]
    check("the last free slot goes to the most urgent newcomer",
          "urgent" in names and "calm" not in names, str(names))


def test_stale_working_decays() -> None:
    now = time.time()
    agents = [
        {"name": "live", "status": "working", "updated_at": now - 5, "source": "cmux"},
        {"name": "zombie", "status": "working", "updated_at": now - STALE_WORKING_S - 60,
         "source": "cmux"},
        {"name": "nostamp", "status": "working", "updated_at": 0.0, "source": "cmux"},
        {"name": "long-t3", "status": "working",
         "updated_at": now - STALE_WORKING_S - 600, "source": "t3code-claude"},
    ]
    out = {a["name"]: a["status"] for a in decay_stale(agents, now)}
    check("a fresh working agent stays working", out["live"] == "working")
    check("a working agent with a dead heartbeat decays", out["zombie"] == "done")
    check("an agent with no timestamp is left alone", out["nostamp"] == "working")
    check("authoritative T3 work does not decay on a long turn",
          out["long-t3"] == "working")


def test_exact_liveness_drops_dead_and_preserves_live_sessions() -> None:
    """Fresh timestamps cannot resurrect a process the OS says is gone."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": "cx-dead", "status": "working", "session_id": "dead",
             "source": "codex-cli", "updated_at": now},
            {"name": "cx-live", "status": "working", "session_id": "live",
             "source": "codex-cli", "updated_at": now - STALE_WORKING_S - 60},
            {"name": "cx-unknown", "status": "done", "session_id": "unknown",
             "source": "codex-cli", "updated_at": now},
            {"name": "cx-idle", "status": "idle", "session_id": "idle",
             "source": "codex-cli", "updated_at": now},
            {"name": "cx-ancient", "status": "done", "session_id": "ancient",
             "source": "codex-cli", "updated_at": now - 48 * 3600},
        ])
        c = AgentConnector(hermes_state=Path(tmp) / "none.json", local_state=local)
        verdicts = {"dead": False, "live": True, "unknown": None,
                    "idle": True, "ancient": True}
        c.liveness_probe = lambda a: verdicts[a["session_id"]]
        found = {a["session_id"]: a for a in c.collect(now)}
        check("an exactly dead local session disappears immediately",
              "dead" not in found, str(found))
        check("an exactly live session survives an old hook heartbeat",
              found.get("live", {}).get("status") == "working", str(found))
        check("an unprovable session is not falsely evicted",
              "unknown" in found, str(found))
        check("a verified-live idle session remains visible",
              "idle" in found, str(found))
        check("a verified-live session outranks the wall-clock age cutoff",
              "ancient" in found, str(found))


def test_pid_liveness_checks_birth_marker_and_is_cached() -> None:
    agent = {
        "name": "api", "source": "codex-cli", "session_id": "s",
        "agent_pid": 123, "agent_started_at": "Fri Aug  7 10:00:00 2026",
        "updated_at": time.time(),
    }
    probe = LocalLivenessProbe(cache_seconds=30)
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        output = ("Fri Aug  7 10:00:00 2026\n" if "lstart=" in argv
                  else "/opt/homebrew/bin/codex resume s\n")
        return subprocess.CompletedProcess(argv, 0, output, "")

    probe._run = fake_run  # type: ignore[method-assign]
    check("a matching pid, command, and birth marker proves liveness",
          probe(agent) is True)
    first_calls = len(calls)
    check("liveness probes are cached across the 0.5s poll loop",
          probe(agent) is True and len(calls) == first_calls, str(calls))

    reused = LocalLivenessProbe(cache_seconds=0)

    def reused_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        output = ("Fri Aug  7 12:00:00 2026\n" if "lstart=" in argv
                  else "/opt/homebrew/bin/codex resume other\n")
        return subprocess.CompletedProcess(argv, 0, output, "")

    reused._run = reused_run  # type: ignore[method-assign]
    check("a reused pid with a different birth marker is dead",
          reused(agent) is False)


def test_legacy_liveness_uses_all_available_exact_handles() -> None:
    """A stale inherited tty must not overrule a live exact Herdr pane."""
    probe = LocalLivenessProbe(cache_seconds=0)
    agent = {
        "name": "api", "source": "codex-cli", "session_id": "s",
        "tty": "ttys-old", "herdr_pane": "w2:p3",
    }
    probe._tty_liveness = lambda _agent, _tty: False  # type: ignore[method-assign]

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        payload = json.dumps({
            "result": {"pane": {"agent_status": "working"}},
        })
        return subprocess.CompletedProcess(argv, 0, payload, "")

    probe._run = fake_run  # type: ignore[method-assign]
    check("one live exact handle outranks a stale inherited handle",
          probe._legacy_handle_liveness(agent, time.monotonic()) is True)


def test_remote_hermes_never_depends_on_local_liveness() -> None:
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        hermes = Path(tmp) / "hermes.json"
        write(hermes, [{
            "name": "Background", "status": "working", "thread_id": "42",
            "source": "hermes-discord", "last_activity_at": now,
        }])
        c = AgentConnector(hermes_state=hermes,
                           local_state=Path(tmp) / "none.json")
        c.liveness_probe = lambda agent: (_ for _ in ()).throw(
            AssertionError("remote agent was probed locally"))
        found = c.collect(now)
        check("remote Hermes survives without any local handle probe",
              len(found) == 1 and found[0]["thread_id"] == "42", str(found))


def test_idle_and_old_are_dropped() -> None:
    now = time.time()
    agents = [
        {"name": "busy", "status": "working", "updated_at": now, "source": "cmux"},
        {"name": "sleepy", "status": "idle", "updated_at": now, "source": "cmux"},
        {"name": "ancient", "status": "done", "updated_at": now - 48 * 3600, "source": "cmux"},
        {"name": "recent", "status": "done", "updated_at": now - 60, "source": "cmux"},
    ]
    kept = {a["name"] for a in drop_uninteresting(agents, now, 24.0)}
    check("idle agents are dropped", "sleepy" not in kept)
    check("agents past the age cutoff are dropped", "ancient" not in kept)
    check("live agents are kept", "busy" in kept)
    check("recently finished agents are kept", "recent" in kept)


def test_merges_both_feeds() -> None:
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        hermes = Path(tmp) / "hermes.json"
        local = Path(tmp) / "local.json"
        write(hermes, [{
            "name": "Audio Audit", "status": "working", "thread_id": "123",
            "url": "https://discord.com/channels/1/123",
            "source": "hermes-discord", "last_activity_at": now,
        }])
        write(local, [
            {"name": "cc-sample-api", "status": "blocked", "cwd": "/w/sample-api",
             "source": "claude-code", "updated_at": now},
            {"name": "cx-mirror", "status": "working", "cwd": "/w/mirror",
             "source": "codex-cli", "updated_at": now},
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=hermes, local_state=local)
        agents = c.collect(now)
        check("both feeds merge into one pool", len(agents) == 3, str(len(agents)))
        by_source = {a["source"] for a in agents}
        check("all three sources present",
              by_source == {"hermes-discord", "claude-code", "codex-cli"}, str(by_source))
        faces = c.build_faces(agents)
        check("the claim covers ten keys", len(faces) == 10)
        agent_faces = {i: faces[i] for i in c._agent_keys}
        check("three agent keys are lit", len(agent_faces) == 3,
              str(sorted(agent_faces)))
        badges = {f["badge"] for f in agent_faces.values()}
        check("a Hermes and both local tools are distinguishable by badge",
              badges == {"H", "C", "X"}, str(badges))
        check("presses are mapped for every lit key",
              set(c._agent_keys) == set(agent_faces))


def test_merges_native_desktop_surfaces() -> None:
    """Desktop watcher records are live without pretending they have a CLI PID."""
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        desktop = Path(tmp) / "desktop.json"
        write(local, [])
        write(desktop, [{
            "name": "Plan launch", "status": "working",
            "source": "claude-desktop", "session_id": "chat-1",
            "app": "Claude", "updated_at": time.time(),
            "desktop_surface": True,
        }])
        c = AgentConnector(local_state=local, desktop_state=desktop,
                           hermes_state=Path(tmp) / "remote.json")
        found = c.collect()
        check("native Claude desktop surface reaches the agent board",
              len(found) == 1 and found[0]["source"] == "claude-desktop", str(found))
        c.focus_cmd = f"printf '%s|%s\\n' {{app}} {{session_id}} > {Path(tmp) / 'focus'}"
        c.focus(found[0])
        check("native desktop surface keeps its exact focus identity",
              (Path(tmp) / "focus").read_text(encoding="utf-8").strip() == "Claude|chat-1")


def test_t3_managed_provider_children_are_not_separate_buttons() -> None:
    """T3's Claude/Codex subprocess hooks are implementation detail, not tabs."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        local, t3 = root / "local.json", root / "t3.json"
        write(local, [
            {"name": "cx-Test2", "status": "done", "source": "codex-cli",
             "session_id": "provider-session", "app": "T3 Code (Alpha)",
             "updated_at": now},
            {"name": "cx-Generate", "status": "done", "source": "codex-cli",
             "session_id": "title-worker", "app": "T3 Code (Alpha)",
             "updated_at": now},
        ])
        write(t3, [{
            "name": "Test2", "status": "done", "source": "t3code-codex",
            "session_id": "thread-1", "thread_id": "thread-1",
            "app": "T3 Code (Alpha)", "updated_at": now,
        }])
        connector = AgentConnector(
            hermes_state=root / "none.json", local_state=local,
            desktop_state=root / "desktop.json", t3code_state=t3,
        )
        found = connector.collect(now)
        check("T3 provider subprocesses collapse into their authoritative thread",
              [(a["source"], a["name"]) for a in found]
              == [("t3code-codex", "Test2")], str(found))

async def _noop_send(message: dict[str, Any]) -> None:
    """Swallow publishes in tests that only care about side effects."""
    return None


def test_pager_replaces_the_dead_overflow_key() -> None:
    """The old key said "+2 MORE / NOT SHOWN" and did nothing when pressed.

    Naming hidden work without offering a route to it is worse than hiding it
    silently: it tells the operator two agents want attention and then refuses
    to produce them.
    """
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": f"proj{i}", "status": "working", "cwd": f"/w/{i}",
             "source": "codex-cli", "updated_at": now}
            for i in range(14)
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        faces = c.build_faces(c.collect(now))
        check("the last key is a pager, not a dead counter",
              faces[9]["label"] == "PAGE 1/2", faces[9]["label"])
        check("the pager still reports what is hidden",
              faces[9]["sublabel"] == "+5 more", faces[9]["sublabel"])
        check("the pager is not mistaken for an agent", 9 not in c._agent_keys)
        check("the pager is pressable", c._page_key == 9)
        check("nine agents share the page with the pager",
              len(c._agent_keys) == 9, str(len(c._agent_keys)))

        # Under capacity no key is sacrificed: a pager with one page is a key
        # spent to say nothing.
        write(local, [
            {"name": "solo", "status": "working", "cwd": "/w", "source": "codex-cli",
             "updated_at": now},
        ])
        c2 = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                            local_state=local)
        faces2 = c2.build_faces(c2.collect(now))
        check("no pager when everything fits",
              faces2[9].get("layout") == "logo-only"
              and faces2[9]["source"] == "codex-cli")
        check("no pager key registered", c2._page_key is None)


def test_every_agent_is_reachable_by_paging() -> None:
    """The point of the feature: no session may be permanently unpressable."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        names = [f"proj{i}" for i in range(14)]
        write(local, [
            {"name": n, "status": "working", "cwd": f"/w/{n}",
             "source": "codex-cli", "updated_at": now} for n in names
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        seen: set[str] = set()
        for _ in range(2):
            c.build_faces(c.collect(now))
            seen |= {a["name"] for a in c._agent_keys.values()}
            c.page += 1
        check("every agent appears on some page", seen == set(names),
              str(sorted(set(names) - seen)))


def test_paging_wraps_and_keeps_slots_still() -> None:
    """Cycling must return home, and must not reshuffle the board.

    A pinned slot is the whole promise of this deck.  If paging re-sorted the
    agents, page 1 would look different every time you came back to it and the
    muscle memory would be worthless.
    """
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": f"proj{i}", "status": "working", "cwd": f"/w/{i}",
             "source": "codex-cli", "updated_at": now}
            for i in range(14)
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        first = c.build_faces(c.collect(now))
        c.page += 1
        second = c.build_faces(c.collect(now))
        check("page two is a different set of agents", first != second)
        check("page two names itself", second[9]["label"] == "PAGE 2/2")
        c.page += 1
        third = c.build_faces(c.collect(now))
        check("the pager wraps back to page one", third == first)


def test_a_shrinking_board_cannot_strand_the_operator() -> None:
    """Agents exit while you are on page 2; the board must stay coherent."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": f"proj{i}", "status": "working", "cwd": f"/w/{i}",
             "source": "codex-cli", "updated_at": now}
            for i in range(14)
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        c.build_faces(c.collect(now))
        c.page = 1
        write(local, [
            {"name": "proj0", "status": "working", "cwd": "/w/0",
             "source": "codex-cli", "updated_at": now},
        ])
        faces = c.build_faces(c.collect(now))
        check("the stale page falls back to a real one", c.page == 0)
        check("the pager disappears with the overflow",
              faces[9].get("layout") == "logo-only"
              and faces[9]["source"] == "codex-cli")
        check("the surviving agent is still pressable",
              len(c._agent_keys) == 1, str(len(c._agent_keys)))


def test_a_real_press_on_the_pager_turns_the_page() -> None:
    """Cover the press path, not just build_faces.

    The previous key was wired to nothing; asserting the face changed would not
    have caught that. This drives the actual press handler and checks it both
    advances the page and repaints without waiting for the poll, because a key
    that lags a second reads as broken and gets pressed twice.
    """
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": f"proj{i}", "status": "working", "cwd": f"/w/{i}",
             "source": "codex-cli", "updated_at": now}
            for i in range(14)
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        sent: list[dict[str, Any]] = []

        async def capture(message: dict[str, Any]) -> None:
            sent.append(message)

        c._send = capture  # type: ignore[method-assign]
        asyncio.run(c.publish(force=True))
        before = {f["index"]: f["label"] for f in sent[-1]["faces"]}

        async def tap(index: int) -> None:
            """A real tap: down then straight back up."""
            await c._handle({"type": "press", "index": index})
            await c._handle({"type": "release", "index": index})

        asyncio.run(tap(9))
        check("the press advanced the page", c.page == 1)
        check("the press repainted immediately", len(sent) == 2)
        after = {f["index"]: f["label"] for f in sent[-1]["faces"]}
        check("the pager renamed itself", after[9] == "PAGE 2/2", after[9])
        check("the agent keys actually changed",
              before[0] != after[0], f"{before[0]} -> {after[0]}")

        # A press on an agent key must never be mistaken for a page turn.
        c.focus = lambda agent: None  # type: ignore[method-assign]
        asyncio.run(tap(0))
        check("focusing an agent leaves the page alone", c.page == 1)


def test_seen_marks_a_key_without_changing_its_status() -> None:
    """Pressing a key means "I read this", not "this agent is finished".

    The status must survive untouched: an agent that is blocked is still
    blocked after you have looked at it. Only the shouting stops.
    """
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": "sample api", "status": "blocked", "cwd": "/w",
             "source": "codex-cli", "updated_at": now},
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        fresh = c.build_faces(c.collect(now))[0]
        check("an unseen alert breathes", fresh["effect"] == "breathe")
        check("an unseen key is not marked seen", fresh["seen"] is False)

        c.mark_seen(c._agent_keys[0])
        seen = c.build_faces(c.collect(now))[0]
        check("the sublabel still reports the real status",
              seen["sublabel"] == fresh["sublabel"], seen["sublabel"])
        check("a seen key stops animating", seen["effect"] == "solid")
        check("a seen key is dimmer, not recoloured",
              seen["color"] != fresh["color"])
        check("a seen key is flagged for the renderers", seen["seen"] is True)
        check("a seen key is still pressable", 0 in c._agent_keys)


def test_done_needs_attention_until_viewed() -> None:
    """A completion is unread work, not a quiet historical state."""
    agent = {
        "name": "sample api", "status": "done", "source": "codex-cli",
        "updated_at": 1.0,
    }
    fresh = face_for(agent, seen=False)
    check("an unviewed completion says it needs you",
          fresh["sublabel"] == "NEEDS YOU", fresh["sublabel"])
    check("an unviewed completion uses the alert treatment",
          fresh["effect"] == "breathe" and fresh["icon"] == "alert"
          and fresh["color"] == STATUS_FACE["blocked"]["color"], str(fresh))
    check("an unviewed completion has attention priority",
          slot_priority(agent, seen=False) == STATUS_ORDER["blocked"])

    viewed = face_for(agent, seen=True)
    check("viewing reveals the quiet done state",
          viewed["sublabel"] == "done" and viewed["effect"] == "solid"
          and viewed["icon"] == "check-outline", str(viewed))
    check("a viewed completion returns to done priority",
          slot_priority(agent, seen=True) == STATUS_ORDER["done"])


def test_manual_surface_view_acknowledges_the_current_event() -> None:
    """Selecting an app tab is as real a read as pressing its deck key."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hermes = root / "hermes.json"
        desktop = root / "desktop.json"
        write(hermes, [{
            "name": "review draft", "status": "done",
            "source": "hermes-discord", "thread_id": "thread-7",
            "url": "https://discord.com/channels/guild/thread-7",
            "updated_at": now,
        }])
        desktop.write_text(json.dumps({
            "agents": [],
            "viewed": [{
                "source": "hermes-discord",
                "url": "https://discord.com/channels/guild/thread-7/message-9",
            }],
        }), encoding="utf-8")
        c = AgentConnector(
            hermes_state=hermes, local_state=root / "none.json",
            desktop_state=desktop,
        )
        face = c.build_faces(c.collect(now))[0]
        check("a manually selected Discord thread becomes viewed",
              face["seen"] is True and face["sublabel"] == "done", str(face))

        # The acknowledgement covers this completion only. A later result in
        # the same selected thread must become visible if the user has left it.
        desktop.write_text(json.dumps({"agents": [], "viewed": []}), encoding="utf-8")
        write(hermes, [{
            "name": "review draft", "status": "done",
            "source": "hermes-discord", "thread_id": "thread-7",
            "url": "https://discord.com/channels/guild/thread-7",
            "updated_at": now + 1,
        }])
        next_face = c.build_faces(c.collect(now + 1))[0]
        check("a later completion in that thread still needs attention",
              next_face["seen"] is False
              and next_face["sublabel"] == "NEEDS YOU", str(next_face))


def test_seen_expires_when_the_agent_moves_on() -> None:
    """An acknowledgement covers a STATE, not an agent, forever.

    Remembering only "you pressed this once" would silence the next real alert
    from the same session, which is the failure that makes people stop trusting
    a notification light.
    """
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": "sample api", "status": "done", "cwd": "/w",
             "source": "codex-cli", "updated_at": now},
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        c.build_faces(c.collect(now))
        c.mark_seen(c._agent_keys[0])
        check("acknowledged while done", c.build_faces(c.collect(now))[0]["seen"])

        write(local, [
            {"name": "sample api", "status": "blocked", "cwd": "/w",
             "source": "codex-cli", "updated_at": now},
        ])
        face = c.build_faces(c.collect(now))[0]
        check("the same agent turning blocked shouts again",
              face["seen"] is False and face["effect"] == "breathe",
              f"{face['seen']} {face['effect']}")


def test_long_press_dismisses_and_short_press_focuses() -> None:
    """Hold takes the key back; tap follows the session.

    The two must not be confused in either direction: a tap that dismissed
    would lose the session, and a hold that focused would drag a window forward
    every time the operator tidied the board.
    """
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": "sample api", "status": "done", "cwd": "/w",
             "source": "codex-cli", "updated_at": now},
            {"name": "mirror", "status": "working", "cwd": "/m",
             "source": "cmux", "updated_at": now},
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        focused: list[str] = []
        c.focus = lambda agent: focused.append(agent["name"])  # type: ignore[method-assign]
        c._send = _noop_send  # type: ignore[method-assign]
        c.build_faces(c.collect(now))
        # Slots are assigned most-urgent-first, so "which key is sample api" is
        # not a constant. Look it up rather than assuming key 0.
        api_key = next(i for i, a in c._agent_keys.items() if a["name"] == "sample api")

        async def hold(index: int, seconds: float) -> None:
            await c._handle({"type": "press", "index": index})
            c._down[index] = time.monotonic() - seconds
            await c._handle({"type": "release", "index": index})

        asyncio.run(hold(api_key, 0.05))
        check("a tap focuses the session", focused == ["sample api"], str(focused))
        faces = c.build_faces(c.collect(now))
        check("a tap marks the key seen", faces[api_key]["seen"])

        asyncio.run(hold(api_key, 1.0))
        check("a hold does not focus", focused == ["sample api"], str(focused))
        names = {a["name"] for a in c.collect(now)}
        check("a hold removes the agent from the board",
              names == {"mirror"}, str(names))


def test_long_press_reflows_survivors_before_launchers_return() -> None:
    """Deleting from a full session row must not preserve a visible hole.

    Six sessions consume keys 0-5 and hide launchers 6-9. Long-holding a
    middle session brings the launcher row back; the five survivors must be
    packed into 0-4 first instead of retaining stale keys around the hole.
    """
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": f"session-{i}", "status": "working", "cwd": f"/w/{i}",
             "source": "codex-cli", "updated_at": now}
            for i in range(6)
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        c._send = _noop_send  # type: ignore[method-assign]
        c.build_faces(c.collect(now))
        removed_name = c._agent_keys[2]["name"]

        async def delete_middle() -> None:
            await c._handle({"type": "press", "index": 2})
            c._down[2] = time.monotonic() - 1.0
            await c._handle({"type": "release", "index": 2})

        asyncio.run(delete_middle())
        check("long-hold deletion removes the chosen middle session",
              removed_name not in {a["name"] for a in c._agent_keys.values()})
        check("survivors reflow before the launcher row returns",
              sorted(c._agent_keys) == [0, 1, 2, 3, 4]
              and sorted(c._launcher_keys) == [6, 7, 8, 9],
              f"agents={sorted(c._agent_keys)} launchers={sorted(c._launcher_keys)}")

        # A second cleanup must compact the already-compacted board too; the
        # bug was especially visible after several quick long-holds.
        asyncio.run(delete_middle())
        check("repeated long-hold deletion keeps the board compact",
              sorted(c._agent_keys) == [0, 1, 2, 3]
              and sorted(c._launcher_keys) == [6, 7, 8, 9],
              f"agents={sorted(c._agent_keys)} launchers={sorted(c._launcher_keys)}")


def test_a_dismissed_agent_returns_when_it_does_something_new() -> None:
    """Dismissal is not deletion. The session is still live and may need you."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"
        write(local, [
            {"name": "sample api", "status": "done", "cwd": "/w",
             "source": "codex-cli", "updated_at": now},
        ])
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        c.build_faces(c.collect(now))
        c.dismiss(c._agent_keys[0])
        check("the board is empty after dismissing", c.collect(now) == [])

        write(local, [
            {"name": "sample api", "status": "blocked", "cwd": "/w",
             "source": "codex-cli", "updated_at": now},
        ])
        back = c.collect(now)
        check("the agent returns when it needs you again",
              [a["name"] for a in back] == ["sample api"], str(back))


def test_faces_carry_the_logo_filename() -> None:
    """The emulator must not derive the logo path itself.

    It used to build `logos/<source>.svg`, which silently 404'd the moment a
    source's mark became a PNG -- exactly what hid the Nous face and left a
    letter in its place. The filename now travels with the face so the two
    renderers cannot disagree.
    """
    face = face_for({"name": "chan", "status": "done", "source": "hermes-discord"})
    check("the Hermes face names the configured Hermes mark",
          face["logo"] == connector_module.logos.HERMES_LOGO,
          str(face.get("logo")))
    svg = face_for({"name": "x", "status": "done", "source": "claude-code"})
    check("an SVG source names its SVG",
          svg["logo"] == "claude-code.svg", str(svg.get("logo")))
    cursor = face_for({"name": "x", "status": "working", "source": "cursor-agent"})
    check("a Cursor session carries the Cursor logo",
          cursor["logo"] == "cursor-agent.svg", str(cursor.get("logo")))


def test_status_icons_exist_for_every_status() -> None:
    """A status whose icon file is missing renders as a blank key."""
    import logos as logos_mod
    for status, style in STATUS_FACE.items():
        check(f"the {status} icon file exists",
              logos_mod.icon_path(style["icon"]) is not None, style["icon"])
    check("the seen counterpart of a check exists",
          logos_mod.icon_path("check-outline") is not None)
    check("the pager icon exists", logos_mod.icon_path("page") is not None)


def test_a_second_result_with_the_same_status_still_announces_itself() -> None:
    """done -> working -> done is TWO results, not one.

    Keying the acknowledgement on the status string alone looked correct and
    was wrong: the second "done" is a different piece of work, but the string
    is identical, so the key stayed dimmed and the new result never announced
    itself. The heartbeat distinguishes them -- any event at all moves
    updated_at, and any event is something the operator has not seen.
    """
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"

        def publish(status: str, stamp: float) -> None:
            write(local, [
                {"name": "proj", "status": status, "cwd": "/w",
                 "source": "codex-cli", "updated_at": stamp},
            ])

        publish("done", now)
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        c.build_faces(c.collect(now))
        c.mark_seen(c._agent_keys[0])
        check("acknowledged", c.build_faces(c.collect(now))[0]["seen"])

        publish("working", now + 1)
        check("new work lights the key",
              c.build_faces(c.collect(now + 1))[0]["seen"] is False)

        publish("done", now + 2)
        check("a SECOND done is not covered by the first acknowledgement",
              c.build_faces(c.collect(now + 2))[0]["seen"] is False)

        # ...and acknowledging it must still stick across ordinary polls, or
        # the seen state would flicker off on every refresh.
        c.mark_seen(c._agent_keys[0])
        check("the new acknowledgement holds",
              c.build_faces(c.collect(now + 2))[0]["seen"])
        check("and survives a repoll with no new events",
              c.build_faces(c.collect(now + 3))[0]["seen"])


def test_a_dismissed_agent_returns_on_a_repeated_status() -> None:
    """The long press has the same trap: dismissal must not hide new work."""
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.json"

        def publish(status: str, stamp: float) -> None:
            write(local, [
                {"name": "proj", "status": status, "cwd": "/w",
                 "source": "codex-cli", "updated_at": stamp},
            ])

        publish("done", now)
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "none.json",
                           local_state=local)
        c.build_faces(c.collect(now))
        c.dismiss(c._agent_keys[0])
        check("dismissed", c.collect(now) == [])

        publish("done", now + 5)
        check("a fresh result brings a dismissed agent back",
              [a["name"] for a in c.collect(now + 5)] == ["proj"])


def test_missing_and_corrupt_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.json"
        check("a missing state file yields no agents",
              read_agents(missing, source_default="cmux") == [])
        bad = Path(tmp) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        check("a corrupt state file yields no agents",
              read_agents(bad, source_default="cmux") == [])
        wrong = Path(tmp) / "wrong.json"
        wrong.write_text('{"agents": "not a list"}', encoding="utf-8")
        check("a wrong-typed agents field yields no agents",
              read_agents(wrong, source_default="cmux") == [])
        # An empty board no longer means a dead board.  A deck with nothing on
        # it cannot start anything, so the launchers fill the bottom of the
        # claimed range and the keys above them stay dark.
        apps = Path(tmp) / "apps.json"
        c = AgentConnector(claim=(0, 9), hermes_state=missing, local_state=bad,
                           apps_config=apps)
        faces = c.build_faces(c.collect())
        check("an empty board still claims ten keys", len(faces) == 10)
        lit = [i for i, f in sorted(faces.items()) if f["effect"] != "off"]
        check("an empty board offers the four default launchers",
              lit == [6, 7, 8, 9], repr(lit))
        check("launchers sit at the bottom, agent slots stay dark",
              all(faces[i]["effect"] == "off" for i in range(0, 6)))
        check("a launcher is dim, never attention-grabbing",
              all(faces[i]["color"] == LAUNCHER_COLOR for i in lit))
        # "Hermes", not "Discord": Discord is the transport the agent speaks
        # through, and naming the key after the pipe put a transport beside two
        # keys named for agents.  The bundle it opens is still Discord.app.
        check("launchers are icon-only with no text or badge",
              all(
                  faces[i].get("layout") == "logo-only"
                  and not faces[i]["label"]
                  and not faces[i]["sublabel"]
                  and not faces[i]["badge"]
                  for i in lit
              ))
        # A launcher carries no state, so it gets no status glyph.  Reusing the
        # agent glyph stamped a meaningless "AI" on all four, stealing the top
        # line and duelling with the corner logo that already names the app.
        check("a launcher shows no status glyph",
              all(faces[i]["icon"] is None for i in lit),
              repr([faces[i]["icon"] for i in lit]))
        check("a launcher still carries its logo source",
              [faces[i]["source"] for i in lit]
              == ["hermes-discord", "t3code", "claude-code", "codex-cli"])
        check("the second launcher opens standalone T3 Code",
              c._launcher_keys[7].get("bundle") == "T3 Code (Alpha)")


def test_focus_command_receives_agent_fields() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "pressed.txt"
        c = AgentConnector(
            claim=(0, 9),
            hermes_state=Path(tmp) / "a.json", local_state=Path(tmp) / "b.json",
            focus_cmd=f"printf '%s|%s|%s\\n' {{source}} {{name}} {{url}} >> {out}",
        )
        c.focus({"name": "sample api", "source": "hermes-discord", "cwd": "/w",
                 "url": "https://discord.com/channels/1/2", "thread_id": "2"})
        text = out.read_text(encoding="utf-8").strip()
        check("focus substitutes source, name, and url",
              text == "hermes-discord|sample api|https://discord.com/channels/1/2", text)
        # A template referencing an unknown field must not raise.
        c.focus_cmd = "echo {nope}"
        c.focus({"name": "x", "source": "cmux", "cwd": "", "url": "", "thread_id": ""})
        check("an invalid focus template is survivable", True)


def test_tty_reaches_the_focus_command() -> None:
    """The tty recorded by the hook is the only non-guessing link from an agent
    to a cmux surface, so it must survive the whole state -> focus path."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "pressed.txt"
        state = Path(tmp) / "local.json"
        state.write_text(json.dumps({"agents": [{
            "name": "cc-sample api", "status": "working", "cwd": "/w",
            "tty": "ttys013", "source": "claude-code",
            "updated_at": time.time(),
        }]}), encoding="utf-8")
        c = AgentConnector(
            claim=(0, 9),
            hermes_state=Path(tmp) / "a.json", local_state=state,
            focus_cmd=f"printf '%s\\n' {{tty}} >> {out}",
        )
        agents = c.collect()
        check("the tty survives state loading",
              bool(agents) and agents[0].get("tty") == "ttys013",
              str(agents))
        c.focus(agents[0])
        check("the focus command receives the recorded tty",
              out.read_text(encoding="utf-8").strip() == "ttys013")

        # An agent with no recorded tty must still produce a runnable command.
        out.unlink()
        c.focus({"name": "x", "source": "cmux", "cwd": "/w", "url": "",
                 "thread_id": "", "session_id": ""})
        check("a missing tty degrades to an empty argument, not a crash",
              out.read_text(encoding="utf-8").strip() == "")


def test_herdr_pane_reaches_the_focus_command() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "pressed.txt"
        state = Path(tmp) / "local.json"
        state.write_text(json.dumps({"agents": [{
            "name": "cx-herdr", "status": "working", "cwd": "/w",
            "herdr_pane": "w2:p7", "source": "codex-cli",
            "updated_at": time.time(),
        }]}), encoding="utf-8")
        c = AgentConnector(
            claim=(0, 9), hermes_state=Path(tmp) / "a.json",
            local_state=state,
            focus_cmd=f"printf '%s\\n' {{herdr_pane}} >> {out}",
        )
        agents = c.collect()
        check("the Herdr pane survives state loading",
              bool(agents) and agents[0].get("herdr_pane") == "w2:p7",
              str(agents))
        c.focus(agents[0])
        check("the focus command receives the exact Herdr pane",
              out.read_text(encoding="utf-8").strip() == "w2:p7")


def test_host_app_reaches_the_focus_command() -> None:
    """Claude Code and Codex also run in their DESKTOP apps, where there is no
    tty and no cmux surface at all.  The recorded host app is the only thing
    that can reach such a session, so it must survive state -> focus."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "pressed.txt"
        state = Path(tmp) / "local.json"
        state.write_text(json.dumps({"agents": [{
            "name": "cc-desk", "status": "working", "cwd": "/w",
            "app": "Claude", "source": "claude-code",
            "updated_at": time.time(),
        }]}), encoding="utf-8")
        c = AgentConnector(
            claim=(0, 9),
            hermes_state=Path(tmp) / "a.json", local_state=state,
            focus_cmd=f"printf '%s\\n' {{app}} >> {out}",
        )
        agents = c.collect()
        check("the host app survives state loading",
              bool(agents) and agents[0].get("app") == "Claude", str(agents))
        c.focus(agents[0])
        check("the focus command receives the recorded host app",
              out.read_text(encoding="utf-8").strip() == "Claude")
        out.unlink()
        c.focus({"name": "x", "source": "cmux", "cwd": "/w", "url": "",
                 "thread_id": "", "session_id": "", "tty": ""})
        check("a missing app degrades to an empty argument, not a crash",
              out.read_text(encoding="utf-8").strip() == "")


def test_launchers_persist_until_six_live_sessions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "local.json"
        apps = Path(tmp) / "apps.json"
        c = AgentConnector(claim=(0, 9), hermes_state=Path(tmp) / "a.json",
                           local_state=state, apps_config=apps)
        state.write_text(json.dumps({"agents": [{
            "name": "cc-sample", "status": "working", "cwd": "/w",
            "source": "claude-code", "updated_at": time.time(),
        }]}), encoding="utf-8")
        faces = c.build_faces(c.collect())
        lit = [i for i, f in sorted(faces.items()) if f["effect"] != "off"]
        check("one live agent keeps the new-session row",
              lit == [0, 6, 7, 8, 9], repr(lit))
        check("the persistent launcher keys remain pressable",
              sorted(c._launcher_keys) == [6, 7, 8, 9])

        state.write_text(json.dumps({"agents": [{
            "name": f"session-{i}", "status": "working", "cwd": f"/w/{i}",
            "source": "claude-code", "updated_at": time.time(),
        } for i in range(6)]}), encoding="utf-8")
        faces = c.build_faces(c.collect())
        check("six live sessions withdraw the whole launcher row",
              c._launcher_keys == {})
        check("all six session keys stay pressable", len(c._agent_keys) == 6)

        # ...and they come back when it goes away, so the deck is never dead.
        state.write_text(json.dumps({"agents": []}), encoding="utf-8")
        faces = c.build_faces(c.collect())
        lit = [i for i, f in sorted(faces.items()) if f["effect"] != "off"]
        check("launchers return once the board empties again",
              lit == [6, 7, 8, 9], repr(lit))


def test_launcher_config_is_editable_and_forgiving() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "apps.json"
        check("a missing config yields the built-in launchers",
              read_launchers(path) == DEFAULT_LAUNCHERS)
        path.write_text("{not json", encoding="utf-8")
        check("a corrupt config yields the built-in launchers",
              read_launchers(path) == DEFAULT_LAUNCHERS)
        path.write_text(json.dumps({"apps": [{"label": "Zed", "bundle": "Zed"}]}),
                        encoding="utf-8")
        check("a valid config replaces the launchers",
              [a["bundle"] for a in read_launchers(path)] == ["Zed"])
        check("missing shortcut config keeps permanent defaults",
              read_shortcuts(path) == DEFAULT_SHORTCUTS)
        # An entry with no bundle names nothing to open and is dropped; a file
        # of nothing but such entries falls back rather than blanking the deck.
        path.write_text(json.dumps([{"label": "broken"}]), encoding="utf-8")
        check("entries with no bundle fall back to the defaults",
              read_launchers(path) == DEFAULT_LAUNCHERS)


def test_fixed_bottom_row_shortcuts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        c = AgentConnector(
            hermes_state=Path(tmp) / "a.json",
            local_state=Path(tmp) / "b.json",
            apps_config=Path(tmp) / "apps.json",
        )
        faces = c.build_faces([])
        check("default connector claims sessions and shortcut rows",
              sorted(faces) == list(range(14)))
        check("keys 10-13 are the requested permanent apps",
              [faces[i]["source"] for i in range(10, 14)]
              == ["slack", "gmail", "discord", "notion-calendar"])
        check("all four permanent apps are pressable",
              all(i in c._launcher_keys for i in range(10, 14)))
        check("Gmail is pinned to the work Chrome profile",
              c._launcher_keys[11].get("profile") == "Default")
        check("Discord is available without private server configuration",
              c._launcher_keys[12].get("bundle") == "Discord"
              and not c._launcher_keys[12].get("url"))

        class Badges:
            def counts(self):
                return {"slack": 2, "gmail": 7, "discord": 1}

            def refresh(self):
                return self.counts()

        c.badge_provider = Badges()
        faces = c.build_faces([])
        check("utility launchers carry truthful unread counts",
              [faces[i].get("notification_count", 0) for i in range(10, 14)]
              == [2, 7, 1, 0])


def test_launcher_press_launches_the_app() -> None:
    """Launching is correct HERE and wrong for an agent key.  This asserts the
    launcher path actually runs its command with the configured bundle."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "launched.txt"
        c = AgentConnector(
            claim=(0, 9), hermes_state=Path(tmp) / "a.json",
            local_state=Path(tmp) / "b.json",
            apps_config=Path(tmp) / "apps.json",
            launch_cmd=f"printf '%s\\n' {{bundle}} >> {out}",
        )
        c.build_faces(c.collect())
        check("the empty board registered pressable launchers",
              sorted(c._launcher_keys) == [6, 7, 8, 9])
        check("Hermes launcher contains no private channel by default",
              c._launcher_keys[6].get("bundle") == "Discord"
              and not c._launcher_keys[6].get("url"))
        c.launch(c._launcher_keys[9])
        check("pressing a launcher opens its bundle",
              out.read_text(encoding="utf-8").strip() == "ChatGPT")
        # A bad template must warn, not take the connector down mid-press.
        c.launch_cmd = "echo {nope}"
        c.launch({"bundle": "Discord"})
        check("an invalid launch template does not raise", True)


def test_gmail_reuses_the_work_profile_window() -> None:
    """The fixed Gmail key opens a tab, not another Chrome window."""
    c = AgentConnector()
    calls = []
    original_run = connector_module.subprocess.run
    connector_module.subprocess.run = lambda argv, **kwargs: calls.append(argv)
    try:
        c.launch(DEFAULT_SHORTCUTS[1])
    finally:
        connector_module.subprocess.run = original_run
    argv = calls[0] if calls else []
    check("Gmail targets Chrome's Default work profile",
          "--profile-directory=Default" in argv, repr(argv))
    check("Gmail opens a tab instead of forcing a new window",
          "--new-window" not in argv, repr(argv))


def test_personal_chrome_focuses_or_creates_its_exact_profile() -> None:
    c = AgentConnector()
    original_run = connector_module.subprocess.run
    personal_chrome = {
        "label": "Personal", "source": "google-chrome",
        "bundle": "Google Chrome", "profile": "Profile 1",
        "profile_name": "Example Profile",
    }

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    focused_calls = []
    connector_module.subprocess.run = lambda argv, **kwargs: (
        focused_calls.append(argv) or Result("focused\n"))
    try:
        c.launch(personal_chrome)
    finally:
        connector_module.subprocess.run = original_run
    check("existing personal Chrome window is raised exactly once",
          len(focused_calls) == 1
          and focused_calls[0][0] == "/usr/bin/osascript",
          repr(focused_calls))

    missing_calls = []

    def missing_then_launch(argv, **kwargs):
        missing_calls.append(argv)
        return Result("missing\n")

    connector_module.subprocess.run = missing_then_launch
    try:
        c.launch(personal_chrome)
    finally:
        connector_module.subprocess.run = original_run
    fallback = missing_calls[1] if len(missing_calls) > 1 else []
    check("missing personal window creates only Profile 1",
          "--profile-directory=Profile 1" in fallback
          and "--new-window" in fallback, repr(missing_calls))


def test_claim_validation() -> None:
    try:
        AgentConnector(claim=(5, 2))
        check("an inverted claim is rejected", False)
    except ValueError:
        check("an inverted claim is rejected", True)
    try:
        AgentConnector(poll_interval=0)
        check("a non-positive poll interval is rejected", False)
    except ValueError:
        check("a non-positive poll interval is rejected", True)


def test_remote_feed_failure_is_visible_on_the_deck() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hermes = root / "hermes.json"
        local = root / "local.json"
        health_path = root / "health.json"
        write(hermes, [{
            "name": "cached session", "status": "done",
            "source": "hermes-discord", "thread_id": "thread-1",
            "updated_at": time.time(),
        }])
        write(local, [])
        reporter = HealthReporter(
            "hermes_agents", path=health_path, stale_after=20,
        )
        reporter.ready(agent_count=1)
        c = AgentConnector(
            hermes_state=hermes, local_state=local,
            hermes_health=health_path,
        )
        check("a fresh Hermes feed adds no system notice",
              all(not a.get("system_notice") for a in c.collect()))

        auth_url = "https://login.tailscale.com/a/testauth"
        reporter.degraded(
            f"Tailscale SSH requires an additional check. To authenticate, visit: {auth_url}"
        )
        agents = c.collect()
        notices = [a for a in agents if a.get("system_notice")]
        check("a degraded Hermes feed creates one visible system notice",
              len(notices) == 1 and notices[0]["status"] == "blocked",
              repr(notices))
        check("the notice names authentication rather than hiding sessions",
              notices[0]["name"] == "Hermes auth"
              and any(a.get("thread_id") == "thread-1" for a in agents),
              repr(agents))
        check("the auth notice retains the exact sign-in URL",
              notices[0].get("url") == auth_url, repr(notices[0]))
        faces = c.build_faces(agents)
        notice_key = next(index for index, agent in c._agent_keys.items()
                          if agent.get("system_notice"))
        check("the deck notice is animated and actionable-looking",
              faces[notice_key]["effect"] == "breathe"
              and faces[notice_key]["sublabel"] == "SIGN IN")

        actions = []
        c._start_action = lambda function, argument: actions.append(  # type: ignore[method-assign]
            (function.__name__, argument)
        )

        async def quiet_publish(force: bool = False) -> None:
            del force

        c.publish = quiet_publish  # type: ignore[method-assign]
        c._down[notice_key] = time.monotonic()
        asyncio.run(c._handle({"type": "release", "index": notice_key}))
        check("pressing SIGN IN opens the retained auth URL",
              len(actions) == 1
              and actions[0][0] == "launch"
              and actions[0][1].get("url") == auth_url,
              repr(actions))


def test_slow_focus_does_not_block_the_next_button_event() -> None:
    """Read-back can take seconds, but dispatching the requested window must not.

    The websocket consumes messages serially.  Awaiting the focus worker here
    queues every later Stream Deck press behind app/cmux verification, which is
    the user-visible intermittent multi-second lag.
    """
    async def scenario() -> tuple[float, int]:
        c = AgentConnector()
        agent = {"name": "slow", "source": "codex-cli"}
        c._agent_keys[0] = agent
        calls: list[int] = []

        def slow_focus(_agent: dict[str, object]) -> None:
            calls.append(1)
            time.sleep(0.20)

        async def quiet_publish(force: bool = False) -> None:
            del force

        c.focus = slow_focus  # type: ignore[method-assign]
        c.publish = quiet_publish  # type: ignore[method-assign]
        c._down[0] = time.monotonic()
        started = time.monotonic()
        await c._handle({"type": "release", "index": 0})
        elapsed = time.monotonic() - started
        # Give a detached worker time to finish; before the fix _handle itself
        # consumes this interval, which is precisely the regression signal.
        await asyncio.sleep(0.25)
        return elapsed, len(calls)

    elapsed, calls = asyncio.run(scenario())
    check("slow focus verification does not block button dispatch",
          elapsed < 0.08, f"dispatch took {elapsed:.3f}s")
    check("background focus still runs exactly once", calls == 1, str(calls))


def main() -> int:
    test_normalize_status()
    test_label_cleaning()
    test_badges_identify_the_tool()
    test_ssh_hosted_hermes_agents()
    test_remote_hermes_maps_to_a_unique_herdr_ssh_pane()
    test_agent_identity_survives_status_change()
    test_slots_are_pinned()
    test_full_board_prefers_urgent_newcomers()
    test_stale_working_decays()
    test_exact_liveness_drops_dead_and_preserves_live_sessions()
    test_pid_liveness_checks_birth_marker_and_is_cached()
    test_legacy_liveness_uses_all_available_exact_handles()
    test_remote_hermes_never_depends_on_local_liveness()
    test_idle_and_old_are_dropped()
    test_merges_both_feeds()
    test_merges_native_desktop_surfaces()
    test_t3_managed_provider_children_are_not_separate_buttons()
    test_pager_replaces_the_dead_overflow_key()
    test_every_agent_is_reachable_by_paging()
    test_paging_wraps_and_keeps_slots_still()
    test_a_shrinking_board_cannot_strand_the_operator()
    test_a_real_press_on_the_pager_turns_the_page()
    test_seen_marks_a_key_without_changing_its_status()
    test_done_needs_attention_until_viewed()
    test_manual_surface_view_acknowledges_the_current_event()
    test_seen_expires_when_the_agent_moves_on()
    test_long_press_dismisses_and_short_press_focuses()
    test_long_press_reflows_survivors_before_launchers_return()
    test_a_dismissed_agent_returns_when_it_does_something_new()
    test_faces_carry_the_logo_filename()
    test_status_icons_exist_for_every_status()
    test_a_second_result_with_the_same_status_still_announces_itself()
    test_a_dismissed_agent_returns_on_a_repeated_status()
    test_missing_and_corrupt_files()
    test_focus_command_receives_agent_fields()
    test_tty_reaches_the_focus_command()
    test_herdr_pane_reaches_the_focus_command()
    test_host_app_reaches_the_focus_command()
    test_launchers_persist_until_six_live_sessions()
    test_launcher_config_is_editable_and_forgiving()
    test_fixed_bottom_row_shortcuts()
    test_launcher_press_launches_the_app()
    test_gmail_reuses_the_work_profile_window()
    test_personal_chrome_focuses_or_creates_its_exact_profile()
    test_claim_validation()
    test_remote_feed_failure_is_visible_on_the_deck()
    test_slow_focus_does_not_block_the_next_button_event()
    total = PASSED + FAILED
    print(f"\n{PASSED}/{total} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
