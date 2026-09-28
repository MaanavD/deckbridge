"""Read, merge, and prune the agent state files the deck consumes.

Each producer (hooks, T3, Claude Desktop, Hermes) writes its own JSON feed.
This module turns them into one list of agent records: normalised status,
duplicate representations of one session collapsed, dead sessions removed.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from hermes_agents_probe import discord_jump_url, discord_route_ids, short_label

log = logging.getLogger("connector_agents")

VALID_STATUSES = ("blocked", "working", "done", "idle")
STATUS_ORDER = {"idle": 0, "done": 1, "working": 2, "blocked": 3}

#: An agent left in a live status with no update for this long is stale: the
#: process died without a closing event.  Shown as done rather than working.
STALE_WORKING_S = 300.0
#: Hook/desktop/Hermes-CLI records that are the same work as a T3 thread.
T3_SHADOW_SOURCES = {
    "t3code-cursor": frozenset({"cursor-agent", "cursor-desktop"}),
    "t3code-claude": frozenset({"claude-code", "claude-desktop"}),
    "t3code-codex": frozenset({"codex-cli", "codex-desktop"}),
}


def normalize_status(status: object) -> str:
    """Coerce any producer's status string into one of the four deck statuses."""
    value = str(status).strip().lower().replace("-", " ").replace("_", " ")
    if value in {"blocked", "waiting", "needs input", "needs you", "error", "approval"}:
        return "blocked"
    if value in {"working", "running", "busy"}:
        return "working"
    if value in {"done", "complete", "completed", "finished"}:
        return "done"
    return "idle"


def agent_key(agent: dict[str, Any]) -> str:
    """Return a stable identity for slot pinning.

    Identity must survive status changes, so it is built from the fields that
    do not change over a session's life.  A Hermes thread is identified by its
    thread id, an ssh-hosted Hermes agent by its session id, and a local agent
    by its source and name.  Falling back to the name for a session that has a
    real id would merge every untitled agent into one key.
    """
    thread = str(agent.get("thread_id") or "").strip()
    if thread:
        return f"hermes:{thread}"
    session = str(agent.get("session_id") or "").strip()
    source = str(agent.get("source") or "local").strip()
    if session:
        # Session ids are only scoped by their producer. Test fixtures often
        # use small ids like "s1", and two real tools are not required to
        # coordinate UUID namespaces, so source is part of identity too.
        return f"{source}:session:{session}"
    return f"{source}:{agent.get('name') or agent.get('cwd') or '?'}"


def _clean_label(text: str) -> str:
    """Strip the tool prefixes and separators that make a tiny label unreadable."""
    label = str(text or "").strip()
    for prefix in ("cc-", "cx-", "cu-", "cm-"):
        if label.lower().startswith(prefix):
            label = label[len(prefix):]
            break
    if " " in label:
        return label
    return label.replace("_", " ").replace("-", " ").strip()


def ensure_hermes_discord_url(
    agent: dict[str, Any], guild_id: str
) -> dict[str, Any]:
    """Fill a missing Discord jump URL from thread/channel id plus guild."""
    if str(agent.get("source") or "") != "hermes-discord":
        return agent
    if str(agent.get("url") or "").strip():
        return agent
    url = discord_jump_url(
        str(guild_id or ""),
        str(agent.get("thread_id") or ""),
        str(agent.get("chat_id") or ""),
    )
    if url:
        agent["url"] = url
    return agent


def read_agents(path: Path, *, source_default: str) -> list[dict[str, Any]]:
    """Read one state file, returning [] for anything missing or malformed."""
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("agents")
    if not isinstance(raw, list):
        return []

    agents: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_title = str(item.get("name") or item.get("title") or "").strip()
        # Hermes and hook producers shorten ``name`` for old 11-character
        # keys; the renderer now fits longer text, so prefer the full title.
        name = _clean_label(str(item.get("title") or item.get("display_title")
                                or raw_title))
        if not name:
            continue
        source = str(item.get("source") or source_default)
        stamp = item.get("updated_at")
        if stamp is None:
            stamp = item.get("last_activity_at")
        try:
            updated_at = float(stamp) if stamp is not None else 0.0
        except (TypeError, ValueError):
            updated_at = 0.0
        agents.append({
            "name": name,
            # Deck keys strip hyphens for the 12-char face. T3's sidebar still
            # shows the original title, and that is the string the click must
            # search for.
            "title": raw_title,
            "status": normalize_status(item.get("status")),
            "source": source,
            "cwd": str(item.get("cwd") or ""),
            "url": str(item.get("url") or ""),
            "web_url": str(item.get("web_url") or ""),
            "thread_id": str(item.get("thread_id") or ""),
            "chat_id": str(item.get("chat_id") or ""),
            "session_id": str(item.get("session_id") or ""),
            "cli_session_id": str(item.get("cli_session_id") or ""),
            "environment_id": str(item.get("environment_id") or ""),
            # For a remote terminal session this is the exact SSH alias used
            # by the local watcher. It lets the Mac map the remote DB record to
            # a Herdr pane whose foreground process is `ssh <alias>`.
            "ssh_host": str(item.get("ssh_host") or ""),
            "environment_label": str(item.get("environment_label") or ""),
            # A surface id the agent named itself. Strongest signal there is:
            # unlike a tty it needs no lookup, and unlike a cwd it identifies
            # ONE tab rather than every tab open in the same directory.
            "surface": str(item.get("surface") or ""),
            # Herdr gives every pane a stable ID directly in the environment.
            # It is exact and can be read back after `agent focus`.
            "herdr_pane": str(item.get("herdr_pane") or ""),
            "herdr_tab": str(item.get("herdr_tab") or ""),
            "herdr_workspace": str(item.get("herdr_workspace") or ""),
            # Recorded by the hook from inside the agent's own terminal. This is
            # the only identifier that maps an agent to a cmux surface without
            # guessing: titles are rewritten by whatever is running, and an
            # agent's cwd need not appear in any title.
            "tty": str(item.get("tty") or ""),
            # The macOS application bundle the agent actually runs inside,
            # recorded by the hook from its own process ancestry.  Claude Code
            # and Codex also run in their DESKTOP apps, which have no tty and
            # therefore no cmux surface: every terminal resolver misses them and
            # the key silently does nothing.  This field is what makes those
            # sessions reachable, and unlike a pgrep guess it names the host
            # this specific agent belongs to.
            "app": str(item.get("app") or ""),
            # PID of the actual Claude/Codex ancestor, not the short-lived hook
            # process. It lets the connector distinguish a quiet live session
            # from a fresh-looking state record whose process has exited.
            "agent_pid": item.get("agent_pid"),
            "agent_started_at": str(item.get("agent_started_at") or ""),
            "activity": str(item.get("last_activity") or ""),
            "model": str(item.get("model") or ""),
            "updated_at": updated_at,
        })
    return agents


def workspace_identity(cwd: Any) -> str:
    """Return a comparable workspace key, or empty when the path is too generic.

    Cursor IDE hooks record ``~/.cursor/projects/<slash-path-with-hyphens>``
    while T3 records the real repo path. Those must collapse to one identity.
    Home directories and other two-component paths stay unmatched so a T3
    thread at ``/home/hermes`` cannot swallow every Hermes CLI session.
    """
    raw = os.path.expanduser(str(cwd or "")).replace("\\", "/").rstrip("/")
    if not raw:
        return ""
    lowered = raw.lower()
    marker = "/.cursor/projects/"
    if marker in lowered:
        slug = lowered.split(marker, 1)[1].split("/", 1)[0]
    else:
        slug = lowered.lstrip("/").replace("/", "-")
    if slug.count("-") < 2:
        return ""
    return slug


def collapse_t3_shadows(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the T3 thread when Hermes or a hook is watching the same session."""
    t3 = [agent for agent in agents
          if str(agent.get("source") or "").startswith("t3code")]
    if not t3:
        return agents
    sessions = {
        str(agent.get("session_id") or agent.get("thread_id") or "")
        for agent in t3
    }
    sessions.discard("")
    workspaces: dict[str, set[str]] = {}
    for agent in t3:
        identity = workspace_identity(agent.get("cwd"))
        if not identity:
            continue
        workspaces.setdefault(identity, set()).add(str(agent.get("source") or ""))
    out: list[dict[str, Any]] = []
    for agent in agents:
        source = str(agent.get("source") or "")
        if source.startswith("t3code") or source.startswith("hermes-discord") \
                or source.startswith("hermes-health"):
            out.append(agent)
            continue
        session = str(agent.get("session_id") or agent.get("thread_id") or "")
        if session and session in sessions:
            continue
        identity = workspace_identity(agent.get("cwd"))
        owners = workspaces.get(identity, set())
        if identity and owners:
            if source == "hermes-ssh":
                continue
            if any(source in T3_SHADOW_SOURCES.get(owner, ()) for owner in owners):
                continue
        out.append(agent)
    return out


def collapse_claude_desktop_shadows(
    agents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the desktop session when a hook is watching the same CLI id.

    Claude Desktop's Code tab used to report through the CLI hook. Current
    releases often skip that hook, so the desktop watcher reads the session
    file. When an older release still fires the hook too, the two records are
    one conversation: the desktop record owns the deep link, and a blocked
    hook status is the stronger attention signal.
    """
    desktop = {
        str(agent.get("cli_session_id") or ""): agent
        for agent in agents
        if str(agent.get("source") or "") == "claude-desktop"
        and str(agent.get("cli_session_id") or "")
    }
    if not desktop:
        return agents
    out: list[dict[str, Any]] = []
    for agent in agents:
        if str(agent.get("source") or "") != "claude-code":
            out.append(agent)
            continue
        owner = desktop.get(str(agent.get("session_id") or ""))
        if owner is None:
            out.append(agent)
            continue
        if agent.get("status") == "blocked":
            owner["status"] = "blocked"
        elif agent.get("status") == "working" and owner.get("status") != "blocked":
            owner["status"] = "working"
    return out


def read_viewed(path: Path) -> list[dict[str, str]]:
    """Read exact, ephemeral surface identities selected outside the deck."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("viewed") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    fields = ("source", "url", "session_id", "thread_id", "surface",
              "herdr_pane", "tty", "app", "unique_app")
    return [
        {field: str(item.get(field) or "") for field in fields}
        for item in raw if isinstance(item, dict)
    ]


def read_pending_approvals(path: Path) -> list[dict[str, Any]]:
    """Read Tirith prompts published by the Discord approval watcher."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = data.get("pending") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _approval_thread_id(item: dict[str, Any]) -> str:
    channel = str(item.get("channel_id") or "").strip()
    if channel.isdigit():
        return channel
    _, target = discord_route_ids(str(item.get("url") or ""))
    return target


def apply_discord_approvals(
    agents: list[dict[str, Any]], pending: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Promote matching Discord threads to blocked while a Tirith prompt waits.

    Hermes session status is a heartbeat, not an approval feed. A finished
    thread can still be sitting on Allow/Deny; that wait is the thing the
    operator has not handled.
    """
    if not pending:
        return agents
    claimed: set[str] = set()
    for agent in agents:
        thread = str(agent.get("thread_id") or "").strip()
        if not thread:
            _, thread = discord_route_ids(str(agent.get("url") or ""))
        if not thread or str(agent.get("source") or "") != "hermes-discord":
            continue
        matches = [
            item for item in pending if _approval_thread_id(item) == thread
        ]
        if not matches:
            continue
        newest = max(matches, key=lambda item: float(item.get("created_ts") or 0))
        agent["status"] = "blocked"
        url = str(newest.get("url") or "")
        if url:
            agent["url"] = url
        stamp = float(newest.get("created_ts") or 0.0)
        if stamp > float(agent.get("updated_at") or 0.0):
            agent["updated_at"] = stamp
        claimed.add(thread)
    for item in pending:
        thread = _approval_thread_id(item)
        if not thread or thread in claimed:
            continue
        claimed.add(thread)
        command = " ".join(str(item.get("command") or "").split())
        agents.append({
            "name": short_label(command, thread) if command else "Approval",
            "title": command or "Command Approval Required",
            "status": "blocked",
            "source": "hermes-discord",
            "thread_id": thread,
            "session_id": str(item.get("message_id") or ""),
            "url": str(item.get("url") or ""),
            "updated_at": float(item.get("created_ts") or 0.0),
        })
    return agents


def manual_view_matches(
    agent: dict[str, Any], view: dict[str, str], agents: list[dict[str, Any]],
) -> bool:
    """Require one strong selected-surface identity; never infer from a label."""
    source = view.get("source", "")
    if source and source != str(agent.get("source") or ""):
        return False
    for field in ("session_id", "thread_id", "surface", "herdr_pane", "tty"):
        selected = view.get(field, "")
        if selected:
            return selected == str(agent.get(field) or "")
    selected_url = view.get("url", "").rstrip("/")
    agent_url = str(agent.get("url") or "").rstrip("/")
    _, selected_target = discord_route_ids(selected_url)
    if selected_target:
        _, agent_target = discord_route_ids(agent_url)
        if selected_target in {
            agent_target,
            str(agent.get("thread_id") or ""),
            str(agent.get("chat_id") or ""),
        }:
            return True
    if selected_url and agent_url:
        # Discord may append a selected message id after the thread/channel.
        return selected_url == agent_url or selected_url.startswith(agent_url + "/")
    app = view.get("app", "")
    if app and view.get("unique_app") == "1" and app == str(agent.get("app") or ""):
        candidates = [a for a in agents if str(a.get("app") or "") == app]
        precise = [a for a in candidates if not str(a.get("source") or "").endswith("-desktop")]
        if precise:
            # A generic desktop-window record may shadow the one hook-backed
            # session. When exactly one precise session exists, viewing the app
            # acknowledges both representations so the fallback cannot keep a
            # duplicate NEEDS YOU key alive.
            return len(precise) == 1 and agent in (precise[0], *[
                a for a in candidates
                if str(a.get("source") or "").endswith("-desktop")
            ])
        return len(candidates) == 1 and agent is candidates[0]
    return False


def decay_stale(agents: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """Demote agents whose live status is contradicted by a silent heartbeat.

    Hooks and watchers only publish on events, so a process killed mid-turn
    leaves a permanently amber key.  A key stuck claiming work is happening is
    worse than one that admits it does not know.
    """
    out = []
    for agent in agents:
        item = dict(agent)
        stamp = item.get("updated_at") or 0.0
        # T3 is polled continuously and its lifecycle flags are authoritative;
        # a long turn may legitimately have no event timestamp for many
        # minutes. The stale-heartbeat rule exists for event-only hook feeds.
        authoritative = str(item.get("source") or "").startswith("t3code")
        if (item["status"] in {"working", "blocked"} and stamp
                and not authoritative
                and not item.get("_verified_live")):
            if now - stamp > STALE_WORKING_S:
                item["status"] = "done"
                item["activity"] = "stale"
        out.append(item)
    return out


def reconcile_local_liveness(
    agents: list[dict[str, Any]], probe: Any,
) -> list[dict[str, Any]]:
    """Drop proven-dead sessions and tag proven-live sessions for decay."""
    out: list[dict[str, Any]] = []
    for agent in agents:
        try:
            verdict = probe(agent)
        except Exception:
            log.exception("local liveness probe failed for %s", agent.get("name"))
            verdict = None
        if verdict is False:
            continue
        item = dict(agent)
        if verdict is True:
            item["_verified_live"] = True
        out.append(item)
    return out


def drop_unverified_idle(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An idle record is only worth a key while its process is provably open."""
    return [a for a in agents
            if a["status"] != "idle" or a.get("_verified_live")]


def expires_at(
    agent: dict[str, Any], seen_at: float | None,
    max_age_s: float, rest_s: float,
) -> float | None:
    """When this key leaves the board if nothing new happens; None = never."""
    status = agent.get("status")
    if status in ("working", "blocked"):
        return None
    stamp = float(agent.get("updated_at") or 0.0)
    if status == "done" and seen_at is None:
        return stamp + max_age_s if stamp else None
    return max(stamp, seen_at or 0.0) + rest_s


def dedupe_labels(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suffix repeated labels so two agents in one directory stay distinct.

    The tool badge already differs, but a glyph is easy to miss at a glance,
    so identical text gets a numeric suffix as well.
    """
    seen: dict[str, int] = {}
    out = []
    for agent in agents:
        item = dict(agent)
        base = item["name"]
        key = base.lower()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            item["name"] = f"{base} {seen[key]}"
        out.append(item)
    return out
