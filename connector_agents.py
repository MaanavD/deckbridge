#!/usr/bin/env python3
"""Unified agent connector: sessions on 0-9, fixed shortcuts on 10-13.

This replaces the split "Hermes owns 0-4, local agents own 5-9" layout.  Both
feeds are merged into a single pool of up to ten slots, so a mix of Hermes
Discord threads, Claude Code sessions, and Codex CLI sessions can fill the
board in whatever proportion actually exists right now.

Two input files, both polled, neither required:

* ``~/.deckbridge/hermes_agents.json``  written by ``hermes_agents_watcher.py``
* ``~/.deckbridge/cmux_state.json``     written by the agent hook shims and
  ``cmux_shim.sh``

Design decisions that matter, and why:

**Slots are pinned, not sorted.**  The first time an agent is seen it claims
the lowest free slot and keeps it until it disappears.  Sorting the board by
status every poll looks tidier but makes the deck unusable: a key can change
meaning between deciding to press it and pressing it.  Priority order only
decides *who gets a slot* when more agents exist than slots, never where a
slot-holder sits.

**No tool prefix in the label.**  The label is the project or thread name; the
tool identity is a corner badge glyph instead.  Two agents in the same
directory are disambiguated with a numeric suffix rather than a ``cc-``/``cx-``
prefix eating the tiny label.

**Idle agents are dropped.**  Only agents that need attention or are alive stay
on the board; anything untouched past the cutoff ages out.  A board full of
finished work hides the one thing that is blocked.

Pressing a key runs the focus command with the agent's fields substituted,
which is how a Hermes key opens its Discord thread and a local key raises its
terminal pane.  See ``focus_agent.sh``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import websockets

from connection_runtime import (
    ConnectionHealth,
    HealthReporter,
    RetryPolicy,
    default_health_path,
    reconnect_forever,
)

# The logo/icon filename mapping lives in one module so the hardware renderer
# and the browser emulator cannot disagree about what a key looks like.  The
# emulator used to build `logos/<source>.svg` itself, which silently broke the
# moment a source's mark became a PNG.
import logos
from app_badges import AppBadgeProvider

log = logging.getLogger("connector_agents")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8777
DEFAULT_CLAIM = (0, 13)
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_HERMES_STATE = "~/.deckbridge/hermes_agents.json"
DEFAULT_LOCAL_STATE = "~/.deckbridge/cmux_state.json"
DEFAULT_DESKTOP_STATE = "~/.deckbridge/desktop_agents.json"
DEFAULT_T3CODE_STATE = "~/.deckbridge/t3code_agents.json"
DEFAULT_APPROVALS_STATE = "~/.deckbridge/hermes_approvals.json"
TAILSCALE_AUTH_URL = re.compile(
    r"https://login\.tailscale\.com/a/[A-Za-z0-9]+"
)
DEFAULT_FOCUS_CMD = (
    "./focus_agent.sh --source {source} --name {name} --cwd {cwd} "
    "--url {url} --session {session_id} --tty {tty} --app {app} "
    "--surface {surface} --herdr-pane {herdr_pane} --web-url {web_url}"
)

#: Agents untouched for longer than this drop off the board entirely.
DEFAULT_MAX_AGE_HOURS = 24.0

#: An agent left in a live status with no update for this long is stale: the
#: process died without a closing event.  Shown as done rather than working.
STALE_WORKING_S = 300.0
LIVENESS_CACHE_S = 5.0
LOCAL_SESSION_SOURCES = frozenset({"claude-code", "codex-cli", "cursor-agent"})

VALID_STATUSES = ("blocked", "working", "done", "idle")
STATUS_ORDER = {"idle": 0, "done": 1, "working": 2, "blocked": 3}

STATUS_FACE = {
    "blocked": {"color": "#c0392b", "effect": "breathe", "icon": "alert"},
    "working": {"color": "#d9822b", "effect": "shimmer", "icon": "working"},
    "done": {"color": "#2e6fdb", "effect": "solid", "icon": "check"},
    "idle": {"color": "#1f8a4c", "effect": "solid", "icon": "idle"},
}


def slot_priority(agent: dict[str, Any], seen: bool = False) -> int:
    """Rank a newcomer by whether the operator still owes it attention.

    A completed result is actionable until it has been opened. Keep its real
    ``done`` status in the feed, but let an unseen completion compete for a
    scarce slot at the same priority as an explicit blocked/approval state.
    """
    status = agent.get("status", "idle")
    if status == "done" and not seen:
        return STATUS_ORDER["blocked"]
    return STATUS_ORDER.get(status, 0)

#: How far a seen key's colour is pulled down.  Enough to read as answered at a
#: glance, not so far that it looks disabled or off.
SEEN_DIM = 0.45

#: The seen counterpart of each status icon.  A filled check becomes a hollow
#: one: the same silhouette, so it is obviously the same thing, but visibly
#: acknowledged.  Icons with no distinct seen form keep their own.
SEEN_ICON = {"check": "check-outline"}

#: Hold this long to dismiss a key instead of following it.  Long enough that a
#: firm tap cannot trigger it by accident, short enough not to feel like a
#: hang.  The Stream Deck reports press and release as separate events, so the
#: hold is measured rather than guessed.
LONG_PRESS_S = 0.6


def dim_hex(colour: str, factor: float) -> str:
    """Scale a #rrggbb colour toward black.

    Kept here rather than in a renderer because the face is the contract: both
    renderers must agree on what a seen key looks like, and computing it twice
    is how they would drift apart.
    """
    text = colour.lstrip("#")
    if len(text) != 6:
        return colour
    try:
        parts = [int(text[i:i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return colour
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(p * factor))) for p in parts)


OFF_FACE = {
    "label": "",
    "sublabel": "",
    "badge": "",
    "source": "",
    "color": "#111111",
    "icon": None,
    "effect": "off",
}

#: New-session launchers occupy the lower four keys of the session area while
#: fewer than six sessions are live. At six sessions the complete 0-9 area is
#: returned to agents. Utility shortcuts on 10-13 never become agent slots.
#:
#: ``bundle`` is the macOS application to open.  Editable at
#: ``~/.deckbridge/apps.json`` as a list of the same three fields, because the
#: right set is a matter of taste, not of correctness.
#:
#: Codex's bundle is ``ChatGPT``: VERIFIED on the target Mac, where
#: ``ls /Applications`` lists ChatGPT.app and no Codex.app.  The CLI is called
#: codex; the desktop app that hosts it is not.
#:
#: The first launcher is Hermes, not Discord. Discord is the transport Hermes
#: speaks through, so labelling the key "Discord" named the pipe rather than
#: the thing being launched -- and it sits beside two keys named for agents.
#: The bundle stays Discord.app because that is still what opens.
DEFAULT_LAUNCHERS = [
    {
        "label": "Hermes", "source": "hermes-discord", "bundle": "Discord",
        "sublabel": "new session",
    },
    {
        "label": "T3 Code", "source": "t3code", "bundle": "T3 Code (Alpha)",
        "sublabel": "new session",
    },
    {
        "label": "Claude", "source": "claude-code", "bundle": "Claude",
        "sublabel": "new session",
    },
    {
        "label": "GPT", "source": "codex-cli", "bundle": "ChatGPT",
        "sublabel": "new session",
    },
]
DEFAULT_SHORTCUTS = [
    {"label": "Slack", "source": "slack", "bundle": "Slack"},
    {
        "label": "Gmail", "source": "gmail", "bundle": "Google Chrome",
        "url": "https://mail.google.com/mail/u/0/", "profile": "Default",
    },
    {"label": "Discord", "source": "discord", "bundle": "Discord"},
    {
        "label": "Calendar", "source": "notion-calendar",
        "bundle": "Notion Calendar",
    },
]
SESSION_LAUNCHER_KEYS = (6, 7, 8, 9)
UTILITY_KEYS = (10, 11, 12, 13)
LAUNCHERS_HIDE_AT = 6
DEFAULT_APPS_CONFIG = "~/.deckbridge/apps.json"
DEFAULT_LAUNCH_CMD = "./focus_agent.sh --launch {bundle}"

#: Launcher keys are dim: they are an offer, not a notification.  Nothing on
#: this deck may compete for attention with a red "needs you" key.
LAUNCHER_COLOR = "#2a2f3a"
# The pager is a control, not a session.  It is distinct from both the status
# palette and the launcher grey so a glance never reads it as an agent.
PAGE_COLOR = "#3b3350"

#: Corner glyph per source, replacing the old cc-/cx- label prefixes.
SOURCE_BADGE = {
    "hermes-discord": "H",
    # A Hermes agent running in a terminal on the Hermes host, reached with
    # `cmux ssh hermes`.  Distinct badge because pressing it focuses an ssh
    # pane rather than opening a Discord thread.
    "hermes-ssh": "S",
    "hermes-health": "!",
    "claude-code": "C",
    "claude-desktop": "C",
    "codex-cli": "X",
    "codex-desktop": "X",
    "cursor-agent": "R",
    "cursor-desktop": "R",
    "t3code": "T",
    "t3code-claude": "C",
    "t3code-codex": "X",
    "t3code-cursor": "R",
    "t3code-grok": "G",
    "t3code-opencode": "O",
    "herdr": "E",
    "cmux": "M",
    "slack": "L",
    "gmail": "G",
    "google-chrome": "P",
    "discord": "D",
    "notion-calendar": "N",
}

#: Human-readable status text for the key's second line.
STATUS_TEXT = {
    "blocked": "NEEDS YOU",
    "working": "working",
    "done": "done",
    "idle": "idle",
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
    return label.replace("_", " ").replace("-", " ").strip()


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
        name = _clean_label(item.get("name") or item.get("title") or "")
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
            "status": normalize_status(item.get("status")),
            "source": source,
            "cwd": str(item.get("cwd") or ""),
            "url": str(item.get("url") or ""),
            "web_url": str(item.get("web_url") or ""),
            "thread_id": str(item.get("thread_id") or ""),
            "session_id": str(item.get("session_id") or ""),
            # For a remote terminal session this is the exact SSH alias used
            # by the local watcher. It lets the Mac map the remote DB record to
            # a Herdr pane whose foreground process is `ssh <alias>`.
            "ssh_host": str(item.get("ssh_host") or ""),
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
            "updated_at": updated_at,
        })
    return agents


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


class LocalLivenessProbe:
    """Bounded, cached proof of whether a local Claude/Codex process exists.

    PID is checked immediately because the hook recorded the owning process
    itself. Older pre-upgrade records have no PID; only once their heartbeat is
    stale do we consult exact tty/surface/Herdr handles. Unknown is different
    from dead: missing tools or unrecognised host metadata return ``None`` and
    preserve timestamp fallback rather than falsely evicting a session.
    """

    def __init__(self, cache_seconds: float = LIVENESS_CACHE_S) -> None:
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._cache: dict[str, tuple[float, tuple[object, ...], bool | None]] = {}
        self._cmux_cache: tuple[float, dict[str, str] | None] = (0.0, None)

    @staticmethod
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=1.5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _expected_process(command: str, source: str) -> bool:
        text = command.lower()
        if any(name in text for name in (
                "agent_shim.py", "claude_shim.py", "codex_shim.py",
                "cursor_shim.py")):
            return False
        needles = {
            "claude-code": ("claude",),
            "codex-cli": ("codex",),
            # Current Cursor CLI uses `agent`; cursor-agent remains its
            # backwards-compatible name. IDE sessions usually have no
            # per-conversation PID and stay timestamp driven instead.
            "cursor-agent": ("agent", "cursor-agent", "cursor"),
        }.get(source, ())
        return any(os.path.basename(token.rstrip("/")) in needles
                   for token in text.replace("=", " ").split()) \
            or any(f"/{needle} " in text or f"/{needle}" == text.rstrip()
                   for needle in needles)

    def _pid_liveness(self, agent: dict[str, Any]) -> bool | None:
        raw = agent.get("agent_pid")
        if raw in (None, ""):
            return None
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            return False
        if pid <= 1:
            return False
        result = self._run(["ps", "-p", str(pid), "-o", "command="])
        if result is None:
            return None
        command = result.stdout.strip()
        if result.returncode != 0 or not command:
            return False
        if not self._expected_process(command, str(agent.get("source") or "")):
            return False
        expected_start = str(agent.get("agent_started_at") or "")
        if expected_start:
            started = self._run(["ps", "-p", str(pid), "-o", "lstart="])
            if started is None:
                return None
            if started.returncode != 0 or started.stdout.strip() != expected_start:
                return False
        return True

    def _tty_liveness(self, agent: dict[str, Any], tty: str) -> bool | None:
        tty = str(tty or "").strip().removeprefix("/dev/")
        if not tty:
            return None
        result = self._run(["ps", "-t", tty, "-o", "command="])
        if result is None:
            return None
        commands = result.stdout.strip()
        if result.returncode != 0 or not commands:
            return False
        return self._expected_process(commands, str(agent.get("source") or ""))

    def _cmux_surfaces(self, now: float) -> dict[str, str] | None:
        expires, cached = self._cmux_cache
        if now < expires:
            return cached
        result = self._run(["cmux", "--id-format", "both", "tree", "--all", "--json"])
        surfaces: dict[str, str] | None = None
        if result is not None and result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                surfaces = {}

                def walk(value: object) -> None:
                    if isinstance(value, dict):
                        surface_id = str(value.get("id") or "")
                        if surface_id and value.get("tty"):
                            surfaces[surface_id] = str(value["tty"])
                        for child in value.values():
                            walk(child)
                    elif isinstance(value, list):
                        for child in value:
                            walk(child)

                walk(data)
            except (TypeError, ValueError):
                surfaces = None
        self._cmux_cache = (now + self.cache_seconds, surfaces)
        return surfaces

    def _legacy_handle_liveness(
        self, agent: dict[str, Any], now: float,
    ) -> bool | None:
        verdicts: list[bool | None] = []
        tty = str(agent.get("tty") or "")
        if tty:
            verdicts.append(self._tty_liveness(agent, tty))

        surface = str(agent.get("surface") or "")
        if surface:
            surfaces = self._cmux_surfaces(now)
            if surfaces is None:
                verdicts.append(None)
            elif surface not in surfaces:
                verdicts.append(False)
            else:
                verdicts.append(self._tty_liveness(agent, surfaces[surface]))

        pane = str(agent.get("herdr_pane") or "")
        if pane:
            result = self._run(["herdr", "pane", "get", pane])
            if result is None:
                verdicts.append(None)
            else:
                try:
                    data = json.loads(result.stdout)
                except ValueError:
                    verdicts.append(None if result.returncode == 0 else False)
                else:
                    if isinstance(data, dict) and data.get("error"):
                        error = data.get("error")
                        if (isinstance(error, dict)
                                and error.get("code") == "pane_not_found"):
                            verdicts.append(False)
                        else:
                            verdicts.append(None)
                    else:
                        pane_data = data.get("result", {}).get("pane", {}) \
                            if isinstance(data, dict) else {}
                        status = str(pane_data.get("agent_status") or "").lower()
                        verdicts.append(
                            True if status and status != "unknown" else None)

        # Handle metadata is sticky across hooks because a detached hook may be
        # unable to rediscover it.  Consequently one old handle can coexist
        # with a newer exact one.  A single proven-live route wins; a session is
        # dead only when every available probe conclusively says so.
        if True in verdicts:
            return True
        if verdicts and all(verdict is False for verdict in verdicts):
            return False
        return None

    def __call__(self, agent: dict[str, Any]) -> bool | None:
        if agent.get("source") not in LOCAL_SESSION_SOURCES:
            return None
        now = time.monotonic()
        token = tuple(agent.get(field) for field in (
            "agent_pid", "agent_started_at", "tty", "surface", "herdr_pane",
            "updated_at",
        ))
        key = agent_key(agent)
        cached = self._cache.get(key)
        if cached and now < cached[0] and token == cached[1]:
            return cached[2]

        verdict = self._pid_liveness(agent)
        if verdict is None:
            stamp = float(agent.get("updated_at") or 0.0)
            # Fresh legacy hooks remain timestamp-driven. This avoids turning
            # a transient CLI/permission failure into a false-dead key.
            if stamp and time.time() - stamp <= STALE_WORKING_S:
                verdict = None
            else:
                verdict = self._legacy_handle_liveness(agent, now)
        self._cache[key] = (now + self.cache_seconds, token, verdict)
        return verdict


class HerdrSshPaneResolver:
    """Conservatively map a remote Hermes record to its local Herdr SSH pane.

    A remote Hermes session id names a database row on the SSH host; a Herdr
    pane id names the visible terminal on this Mac.  They are intentionally
    different namespaces.  The bridge is safe only when one single-pane Herdr
    tab is running ``ssh <the watcher alias>`` and exactly one relevant remote
    agent can own it. Ambiguity yields no route rather than the wrong tab.
    """

    SSH_OPTIONS_WITH_VALUE = frozenset({
        "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i",
        "-J", "-L", "-l", "-m", "-O", "-o", "-P", "-p", "-Q",
        "-R", "-S", "-W", "-w",
    })

    def __init__(
        self, *, runner: Any = subprocess.run,
        herdr_bin: str = "herdr", cache_seconds: float = LIVENESS_CACHE_S,
    ) -> None:
        self.runner = runner
        self.herdr_bin = herdr_bin
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._cache: tuple[float, list[dict[str, str]]] = (0.0, [])

    def _run_json(self, argv: list[str]) -> dict[str, Any] | None:
        try:
            result = self.runner(
                argv, capture_output=True, text=True, timeout=1.5, check=False,
            )
            if result.returncode != 0:
                return None
            value = json.loads(result.stdout)
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _normal_host(value: object) -> str:
        host = str(value or "").strip().casefold()
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        return host.strip("[]")

    @classmethod
    def _ssh_target(cls, argv: object) -> str:
        if not isinstance(argv, list) or not argv:
            return ""
        if os.path.basename(str(argv[0])) != "ssh":
            return ""
        index = 1
        while index < len(argv):
            token = str(argv[index])
            if token == "--":
                index += 1
                break
            if not token.startswith("-") or token == "-":
                break
            if token in cls.SSH_OPTIONS_WITH_VALUE:
                index += 2
            else:
                index += 1
        if index >= len(argv):
            return ""
        return cls._normal_host(argv[index])

    def _discover(self) -> list[dict[str, str]]:
        now = time.monotonic()
        expires, cached = self._cache
        if now < expires:
            return cached
        document = self._run_json([self.herdr_bin, "pane", "list"])
        raw = ((document or {}).get("result") or {}).get("panes") or []
        panes = [item for item in raw if isinstance(item, dict)]
        per_tab: dict[str, int] = {}
        for pane in panes:
            tab = str(pane.get("tab_id") or "")
            if tab:
                per_tab[tab] = per_tab.get(tab, 0) + 1

        routes: list[dict[str, str]] = []
        for pane in panes:
            # A pane already owned by a local Herdr agent cannot simultaneously
            # be the raw SSH viewer for a remote Hermes session.
            if pane.get("agent"):
                continue
            pane_id = str(pane.get("pane_id") or "")
            tab_id = str(pane.get("tab_id") or "")
            workspace_id = str(pane.get("workspace_id") or "")
            # Workspace + tab focus can select the exact pane only when that
            # tab contains one pane. Split-pane ambiguity must remain unfocused.
            if not pane_id or not tab_id or not workspace_id or per_tab.get(tab_id) != 1:
                continue
            info = self._run_json([
                self.herdr_bin, "pane", "process-info", "--pane", pane_id,
            ])
            process_info = ((info or {}).get("result") or {}).get("process_info") or {}
            processes = process_info.get("foreground_processes") or []
            hosts = {
                self._ssh_target(process.get("argv"))
                for process in processes if isinstance(process, dict)
            }
            hosts.discard("")
            if len(hosts) == 1:
                routes.append({
                    "ssh_host": next(iter(hosts)),
                    "herdr_pane": pane_id,
                    "herdr_tab": tab_id,
                    "herdr_workspace": workspace_id,
                })
        self._cache = (now + self.cache_seconds, routes)
        return routes

    def enrich(self, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = [dict(agent) for agent in agents]
        eligible = [
            agent for agent in out
            if agent.get("source") == "hermes-ssh"
            and agent.get("ssh_host") and not agent.get("herdr_pane")
        ]
        if not eligible:
            return out
        routes = self._discover()
        hosts = {self._normal_host(agent.get("ssh_host")) for agent in eligible}
        for host in hosts:
            host_agents = [
                agent for agent in eligible
                if self._normal_host(agent.get("ssh_host")) == host
            ]
            candidates = [route for route in routes if route["ssh_host"] == host]
            if len(candidates) != 1:
                continue
            if len(host_agents) == 1:
                owner = host_agents[0]
            else:
                active = [
                    agent for agent in host_agents
                    if agent.get("status") in {"working", "blocked"}
                ]
                if len(active) != 1:
                    continue
                owner = active[0]
            owner.update(candidates[0])
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


def drop_uninteresting(
    agents: list[dict[str, Any]], now: float, max_age_hours: float,
) -> list[dict[str, Any]]:
    """Keep live agents; drop idle ones and anything past the age cutoff."""
    cutoff = now - max(0.0, max_age_hours) * 3600.0
    out = []
    for agent in agents:
        if agent["status"] == "idle" and not agent.get("_verified_live"):
            continue
        stamp = agent.get("updated_at") or 0.0
        if stamp and stamp < cutoff and not agent.get("_verified_live"):
            continue
        out.append(agent)
    return out


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


class SlotMap:
    """Assign each agent a slot index and keep it for the agent's lifetime.

    A pinned slot is the whole point: the operator builds muscle memory for
    "the sample-api key is second on the row", and re-sorting on every status
    change would destroy that. Slots are only reclaimed when an agent leaves.
    """

    def __init__(self, size: int) -> None:
        self.size = max(0, int(size))
        self._slots: dict[str, int] = {}

    def resize(self, size: int) -> None:
        """Grow or shrink the slot space, keeping every pin that still fits.

        Paging means the number of slots is no longer the number of keys: it
        follows the agent count, so a board of twelve agents holds twelve pins
        across two pages.  Growing must never disturb an existing pin, or the
        page an agent lives on would shift under the operator's hand every time
        an unrelated agent appeared.
        """
        self.size = max(0, int(size))
        for key, slot in list(self._slots.items()):
            if slot >= self.size:
                del self._slots[key]

    def remove_and_compact(self, key: str) -> None:
        """Remove one deliberate dismissal and pack survivors leftward.

        Ordinary feed churn keeps pins stable: a session disappearing on its
        own must not reshuffle keys under the operator's hand. A long-hold is
        different—the operator explicitly asked to tidy the board. Keeping a
        middle hole after that action strands the surviving sessions around
        stale positions just as the launcher row returns. Preserve their
        relative order, but close every gap in one deterministic pass.
        """
        self._slots.pop(key, None)
        ordered = sorted(self._slots, key=self._slots.get)
        self._slots = {agent_id: slot for slot, agent_id in enumerate(ordered)}

    def assign(
        self,
        agents: list[dict[str, Any]],
        priority: Callable[[dict[str, Any]], int] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Return {slot_index: agent} honouring existing pins."""
        priority = priority or (lambda agent: slot_priority(agent, seen=True))
        present = {agent_key(a): a for a in agents}

        # Release slots whose agent is gone.
        for key in list(self._slots):
            if key not in present:
                del self._slots[key]

        placed: dict[int, dict[str, Any]] = {}
        for key, slot in list(self._slots.items()):
            agent = present.get(key)
            if agent is not None and slot < self.size:
                placed[slot] = agent
            else:
                del self._slots[key]

        # Newcomers take the lowest free slot, most urgent first so that when
        # the board is full the agents that matter win the remaining space.
        newcomers = [
            a for key, a in present.items() if key not in self._slots
        ]
        newcomers.sort(
            key=lambda a: (priority(a), a.get("updated_at") or 0.0),
            reverse=True,
        )
        free = [i for i in range(self.size) if i not in placed]
        for agent, slot in zip(newcomers, free):
            self._slots[agent_key(agent)] = slot
            placed[slot] = agent
        return placed


def face_for(agent: dict[str, Any], seen: bool = False) -> dict[str, Any]:
    """Build one key face for an agent.

    ``seen`` marks a key the operator has already pressed. It is not a fourth
    status: the agent is still exactly as done or as blocked as it was. It
    records that the notification has been read, so the board can stop
    shouting about it without pretending the session is gone.
    """
    status = agent["status"]
    needs_attention = status == "done" and not seen
    visual_status = "blocked" if needs_attention else status
    style = STATUS_FACE.get(visual_status, STATUS_FACE["idle"])
    color, effect, icon = style["color"], style["effect"], style["icon"]
    if seen:
        # Dim the colour rather than change it: the status must still be
        # readable at a glance, just quieter. Kill the animation outright,
        # because a breathing key you have already answered is the exact thing
        # that trains you to ignore a breathing key you have not.
        color = dim_hex(color, SEEN_DIM)
        effect = "solid"
        icon = SEEN_ICON.get(icon, icon)
    return {
        "label": agent["name"][:12],
        "sublabel": str(
            agent.get("notice_label")
            or (STATUS_TEXT["blocked"] if needs_attention
                else STATUS_TEXT.get(status, status))
        )[:16],
        "badge": SOURCE_BADGE.get(agent.get("source", ""), ""),
        # Source id travels with the face so renderers can draw the product
        # logo.  The letter badge stays alongside it as the fallback for a
        # machine where the SVGs cannot be rasterised.
        "source": agent.get("source", ""),
        "logo": logos.SOURCE_LOGO.get(agent.get("source", ""), ""),
        "color": color,
        "icon": icon,
        "effect": effect,
        "seen": seen,
    }


def _read_button_group(
    path: Path, key: str, defaults: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Read the launcher config, falling back to the built-in three.

    A malformed or missing file yields the defaults rather than an empty row:
    the launchers exist precisely for the moment when nothing else is on the
    deck, so a typo in a config file must not leave the operator with a wholly
    dark board and no way to start anything.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return [dict(item) for item in defaults]
    if isinstance(data, dict):
        raw = data.get(key)
        if raw is None and key == "launchers":
            raw = data.get("apps")
    else:
        raw = data if key == "launchers" else None
    if not isinstance(raw, list):
        return [dict(item) for item in defaults]
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        bundle = str(item.get("bundle") or item.get("app") or "").strip()
        if not bundle:
            continue
        out.append({
            "label": str(item.get("label") or bundle)[:12],
            "source": str(item.get("source") or ""),
            "bundle": bundle,
            "url": str(item.get("url") or ""),
            "profile": str(item.get("profile") or ""),
            "profile_name": str(item.get("profile_name") or ""),
            "sublabel": str(item.get("sublabel") or ""),
        })
    return out or [dict(item) for item in defaults]


def read_launchers(path: Path) -> list[dict[str, str]]:
    return _read_button_group(path, "launchers", DEFAULT_LAUNCHERS)


def read_shortcuts(path: Path) -> list[dict[str, str]]:
    return _read_button_group(path, "shortcuts", DEFAULT_SHORTCUTS)


def launcher_face(app: dict[str, str], notification_count: int = 0) -> dict[str, Any]:
    """Build one key face for a launcher.

    Deliberately dim and effect-free.  A launcher is an offer; only an agent
    that needs the operator is allowed to be bright or to animate.
    """
    return {
        "label": "",
        "sublabel": "",
        "badge": "",
        "source": app.get("source", ""),
        "logo": logos.SOURCE_LOGO.get(app.get("source", ""), ""),
        "color": LAUNCHER_COLOR,
        "layout": "logo-only",
        "notification_count": max(0, int(notification_count or 0)),
        # No status glyph.  On a live agent the glyph carries state (! / OK),
        # which is worth the top line.  A launcher has no state, so the same
        # glyph would render a bare "AI" above every one of them: three
        # identical marks that say nothing, crowding the label and competing
        # with the corner logo that already names the app.
        "icon": None,
        "effect": "solid",
    }


def page_face(page: int, pages: int, hidden: int) -> dict[str, Any]:
    """Build the pager key shown when more agents exist than fit on one page.

    The old face said "+2 MORE / NOT SHOWN" and did nothing when pressed: it
    named a problem and offered no way out, so two live agents were simply
    unreachable.  The key now cycles to the next page, and says which page you
    are on, because a button that changes what the board means has to tell you
    what it did.
    """
    return {
        "label": f"PAGE {page + 1}/{pages}",
        "sublabel": f"+{hidden} more",
        "badge": "",
        "source": "",
        "color": PAGE_COLOR,
        "icon": "page",
        "effect": "solid",
    }


class AgentConnector:
    """Poll both agent feeds and paint one inclusive deckd key range."""

    def __init__(
        self,
        url: str = f"ws://{DEFAULT_HOST}:{DEFAULT_PORT}",
        claim: tuple[int, int] = DEFAULT_CLAIM,
        hermes_state: str | os.PathLike[str] = DEFAULT_HERMES_STATE,
        local_state: str | os.PathLike[str] = DEFAULT_LOCAL_STATE,
        desktop_state: str | os.PathLike[str] | None = None,
        t3code_state: str | os.PathLike[str] | None = None,
        focus_cmd: str = DEFAULT_FOCUS_CMD,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
        name: str = "agents",
        apps_config: str | os.PathLike[str] = DEFAULT_APPS_CONFIG,
        launch_cmd: str = DEFAULT_LAUNCH_CMD,
        health: HealthReporter | None = None,
        hermes_health: str | os.PathLike[str] | None = None,
        remote_herdr_resolver: Any | None = None,
        badge_provider: Any | None = None,
    ) -> None:
        first, last = int(claim[0]), int(claim[1])
        if first < 0 or first > last:
            raise ValueError(f"invalid inclusive claim {first}..{last}")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.url = url
        self.claim = (first, last)
        self.hermes_state = Path(os.path.expanduser(os.fspath(hermes_state)))
        self.local_state = Path(os.path.expanduser(os.fspath(local_state)))
        # The CLI always passes the configured desktop feed explicitly. Keeping
        # direct library construction isolated prevents unit tests and embedded
        # consumers with temporary local feeds from accidentally ingesting the
        # real user's ~/.deckbridge desktop state.
        self.desktop_state = (
            Path(os.path.expanduser(os.fspath(desktop_state)))
            if desktop_state is not None else Path(os.devnull)
        )
        self.t3code_state = (
            Path(os.path.expanduser(os.fspath(t3code_state)))
            if t3code_state is not None else Path(os.devnull)
        )
        self.focus_cmd = focus_cmd
        self.poll_interval = poll_interval
        self.max_age_hours = max_age_hours
        self.name = name
        self.apps_config = Path(os.path.expanduser(os.fspath(apps_config)))
        self.launch_cmd = launch_cmd
        self.health = health
        self.hermes_health = (
            Path(os.path.expanduser(os.fspath(hermes_health)))
            if hermes_health is not None else None
        )
        self.remote_herdr_resolver = (
            remote_herdr_resolver
            if remote_herdr_resolver is not None else HerdrSshPaneResolver()
        )
        self.badge_provider = badge_provider if badge_provider is not None else AppBadgeProvider()

        self.ws: Any = None
        # Constructed by tests and config loaders before an event loop exists.
        # Python 3.9 binds Lock() through get_event_loop(), which raises after a
        # previous asyncio.run() closed the thread's loop. Create it lazily in
        # the actual async send path, where a running loop is guaranteed.
        self._send_lock: asyncio.Lock | None = None
        # Focus/read-back may legitimately take seconds when an app is slow to
        # expose its selected route.  Keep those workers alive without making
        # the serial websocket consumer wait; otherwise one slow verification
        # queues every later physical button press behind it.
        self._actions: set[asyncio.Future[Any]] = set()
        session_size = max(0, min(last, 9) - first + 1)
        self._slots = SlotMap(session_size)
        self._agent_keys: dict[int, dict[str, Any]] = {}
        self._launcher_keys: dict[int, dict[str, str]] = {}
        self._page_key: int | None = None
        self.page = 0
        # agent_key -> the status that was acknowledged.  Storing the status,
        # not just a flag, is what makes the acknowledgement expire when the
        # agent moves on.
        self._seen: dict[str, tuple[str, float]] = {}
        self._dismissed: dict[str, tuple[str, float]] = {}
        self._down: dict[int, float] = {}
        self._last_payload: dict[int, dict[str, Any]] | None = None
        self.liveness_probe: Any = LocalLivenessProbe()

    # -- state ------------------------------------------------------------
    def collect(self, now: float | None = None) -> list[dict[str, Any]]:
        """Merge both feeds into one cleaned, deduplicated agent list."""
        current = time.time() if now is None else now
        # Remote Hermes records are controlled by their watcher and must never
        # depend on local process/terminal handles. Only the local feed is
        # reconciled against the local OS.
        agents = read_agents(self.hermes_state, source_default="hermes-discord")
        try:
            agents = self.remote_herdr_resolver.enrich(agents)
        except Exception:
            log.exception("remote Hermes HerdR pane resolution failed")
        if self.hermes_health is not None:
            feed = ConnectionHealth.from_path(self.hermes_health, now=current)
            # Missing is normal for the first poll of a fresh install. Once a
            # watcher has spoken, degraded/stale/invalid must be a visible deck
            # event rather than a silently empty or deceptively cached board.
            if feed.state not in ("ready", "missing"):
                raw_error = str(feed.document.get("error") or feed.message)
                error = raw_error.lower()
                auth_url = TAILSCALE_AUTH_URL.search(raw_error)
                auth = "additional check" in error or auth_url is not None
                agents.append({
                    "name": "Hermes auth" if auth else "Hermes feed",
                    "status": "blocked",
                    "source": "hermes-health",
                    "session_id": "hermes-feed-health",
                    "updated_at": current,
                    "system_notice": True,
                    "notice_label": "SIGN IN" if auth else "OFFLINE",
                    "detail": feed.message,
                    # Only an exact vendor-owned check URL becomes actionable;
                    # arbitrary transport errors can never turn into links.
                    "url": auth_url.group(0) if auth_url is not None else "",
                })
        local = read_agents(self.local_state, source_default="cmux")
        agents += reconcile_local_liveness(local, self.liveness_probe)
        # Native desktop conversations do not have a child CLI PID to probe.
        # Their watcher renews only while an Accessibility-visible app window
        # still exposes the exact deep-link route.
        agents += read_agents(self.desktop_state, source_default="desktop")
        agents += read_agents(self.t3code_state, source_default="t3code")
        agents = decay_stale(agents, current)
        agents = drop_uninteresting(agents, current, self.max_age_hours)
        agents = dedupe_labels(agents)
        for view in read_viewed(self.desktop_state):
            for agent in agents:
                if manual_view_matches(agent, view, agents):
                    self.mark_seen(agent)
        # Acknowledgements are forgotten for agents that have left the feeds
        # entirely, or the dicts grow without bound in a long-running session.
        # This is done BEFORE the dismissed filter, because a dismissed agent is
        # deliberately absent from the returned list but very much still live.
        live = {agent_key(a) for a in agents}
        for store in (self._seen, self._dismissed):
            for key in [k for k in store if k not in live]:
                del store[key]
        # A long-pressed agent leaves the board until it does something new.
        return [a for a in agents if not self._is_dismissed(a)]

    def build_faces(self, agents: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """Map every claimed key to a face, pinning agents to their slots."""
        first, last = self.claim
        session_last = min(last, 9)
        session_size = max(0, session_last - first + 1)
        faces = {index: dict(OFF_FACE) for index in range(first, last + 1)}
        self._agent_keys = {}
        self._launcher_keys = {}
        self._page_key = None

        # Slots must outnumber keys once paging exists, or agents past the last
        # key would never be assigned one and could not be paged to.
        self._slots.resize(max(session_size, len(agents)))

        # Utility buttons are a permanent bottom row. They are outside the
        # session slot map, so no amount of agent churn can repurpose them.
        badge_counts = self.badge_provider.counts()
        for index, app in zip(UTILITY_KEYS, read_shortcuts(self.apps_config)):
            if first <= index <= last:
                faces[index] = launcher_face(
                    app, badge_counts.get(app.get("source", ""), 0))
                self._launcher_keys[index] = app

        placed = self._slots.assign(
            agents, priority=lambda agent: slot_priority(
                agent, seen=self._is_seen(agent)))

        # Paging.  Every agent gets a stable global slot; the board shows one
        # window onto them.  A window is a key shorter than the claim whenever
        # the pager is needed, since the pager itself has to live somewhere.
        total = len(placed)
        if total <= session_size:
            per_page, pages = session_size, 1
        else:
            per_page = session_size - 1
            pages = max(1, -(-total // per_page))  # ceil

        # Pressing the pager on the last page returns to the first: the key is
        # a cycle, not a scroll, so there is never a state the operator can get
        # stuck in.  Wrapping also absorbs a shrinking board, where the page
        # count can drop below where the operator was standing.
        self.page %= pages

        start = self.page * per_page
        window = range(
            start, start + per_page if pages > 1 else start + session_size
        )

        for offset, slot in enumerate(window):
            agent = placed.get(slot)
            index = first + offset
            if index > session_last or agent is None:
                continue
            faces[index] = face_for(agent, seen=self._is_seen(agent))
            self._agent_keys[index] = agent

        # The pager only exists when there is somewhere to go.  Spending a key
        # on it otherwise would cost a slot to say nothing.
        if pages > 1:
            hidden = total - sum(1 for s in window if s in placed)
            faces[session_last] = page_face(self.page, pages, hidden)
            self._agent_keys.pop(session_last, None)
            self._page_key = session_last

        # Keep the new-session row visible during ordinary use. Once the sixth
        # live session arrives, withdraw all four together so the session area
        # has one stable meaning and the new session can take its pinned slot.
        if total < LAUNCHERS_HIDE_AT:
            for index, app in zip(
                SESSION_LAUNCHER_KEYS, read_launchers(self.apps_config)
            ):
                if first <= index <= session_last and index not in self._agent_keys:
                    faces[index] = launcher_face(app)
                    self._launcher_keys[index] = app
        return faces

    # -- seen ---------------------------------------------------------------
    def _ack_token(self, agent: dict[str, Any]) -> tuple[str, float]:
        """What an acknowledgement is actually AGAINST.

        Not the agent, and not its status either. Keying on status alone looked
        right and was wrong: an agent that goes done -> working -> done has done
        a second piece of work, and the status string is identical, so the key
        stayed dimmed and the new result never announced itself.

        The heartbeat is the honest signal. Any event at all -- a status change
        or another update with the same status -- moves ``updated_at``, and any
        event means there is something the operator has not seen.
        """
        return (str(agent.get("status", "")), float(agent.get("updated_at") or 0.0))

    def _is_seen(self, agent: dict[str, Any]) -> bool:
        """Has this agent been acknowledged as it stands RIGHT NOW?"""
        return self._seen.get(agent_key(agent)) == self._ack_token(agent)

    def mark_seen(self, agent: dict[str, Any]) -> None:
        self._seen[agent_key(agent)] = self._ack_token(agent)

    def dismiss(self, agent: dict[str, Any]) -> None:
        """Drop an agent from the board until it does something new.

        This is the long-press.  Unlike ``mark_seen`` it takes the key back,
        which is the point: a finished session you have dealt with is clutter,
        and clutter is what pushes live agents onto page 2.
        """
        key = agent_key(agent)
        self._dismissed[key] = self._ack_token(agent)
        self._slots.remove_and_compact(key)

    def _is_dismissed(self, agent: dict[str, Any]) -> bool:
        return self._dismissed.get(agent_key(agent)) == self._ack_token(agent)

    # -- press ------------------------------------------------------------
    def launch(self, app: dict[str, str]) -> None:
        """Open a launcher's application, never raising.

        Launching is correct HERE and wrong for an agent key.  Pressing
        "Claude" states an intent to have Claude; pressing an agent key asks to
        be taken to a running session, and opening a blank window in that case
        would answer a question nobody asked.
        """
        if (self.launch_cmd == DEFAULT_LAUNCH_CMD
                and app.get("source") == "t3code"):
            command = "./focus_agent.sh --launch-t3code"
            try:
                subprocess.run(
                    command, shell=True, check=False, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("T3 Code new-thread launch failed: %s", exc)
            return
        url = str(app.get("url") or "").strip()
        profile = str(app.get("profile") or "").strip()
        profile_name = str(app.get("profile_name") or "").strip()
        # URL buttons use argv, not a shell template. Gmail's explicit Chrome
        # profile is what prevents a work shortcut from silently landing in
        # the personal inbox. A custom launch_cmd remains an injectable test
        # and operator override for every button.
        if (self.launch_cmd == DEFAULT_LAUNCH_CMD and profile
                and profile_name and not url):
            # Chrome is one process for every profile, so activating the app or
            # sending Cmd+` cannot identify which of several windows is the
            # personal one. Window titles expose the visible profile suffix;
            # raise that exact window without creating a disposable tab.
            focus_script = r'''
on run argv
    set profileName to item 1 of argv
    set profileSuffix to " - Google Chrome - " & profileName
    tell application "System Events"
        if exists process "Google Chrome" then
            tell process "Google Chrome"
                repeat with windowRef in windows
                    try
                        if (name of windowRef as text) ends with profileSuffix then
                            perform action "AXRaise" of windowRef
                            set frontmost to true
                            return "focused"
                        end if
                    end try
                end repeat
            end tell
        end if
    end tell
    return "missing"
end run
'''
            try:
                focused = subprocess.run(
                    ["/usr/bin/osascript", "-e", focus_script, profile_name],
                    check=False, capture_output=True, text=True, timeout=3,
                )
                if focused.returncode == 0 and focused.stdout.strip() == "focused":
                    log.info("focused Chrome profile window: %s", profile_name)
                    return
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("Chrome profile focus failed for %s: %s", profile_name, exc)
            command_argv = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                f"--profile-directory={profile}", "--new-window",
                "chrome://newtab/",
            ]
            log.info("launch Chrome profile window: %s", command_argv)
            try:
                subprocess.run(
                    command_argv, check=False, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("Chrome profile launch failed for %s: %s", profile_name, exc)
            return
        if self.launch_cmd == DEFAULT_LAUNCH_CMD and url:
            if profile:
                command_argv = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    # With no --new-window switch Chrome's singleton forwards
                    # the URL into a new tab of the existing profile window.
                    # If that profile has no window, Chrome naturally creates
                    # one, which is the only useful fallback.
                    f"--profile-directory={profile}", url,
                ]
            else:
                command_argv = ["/usr/bin/open", url]
            log.info("launch URL: %s", command_argv)
            try:
                subprocess.run(
                    command_argv, check=False, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("launch URL failed for %s: %s", app.get("label"), exc)
            return
        try:
            command = self.launch_cmd.format(
                bundle=shlex.quote(app.get("bundle", "")),
                label=shlex.quote(app.get("label", "")),
                source=shlex.quote(app.get("source", "")),
            )
        except (KeyError, ValueError, IndexError) as exc:
            log.warning("invalid launch template %r: %s", self.launch_cmd, exc)
            return
        log.info("launch: %s", command)
        try:
            subprocess.run(
                command, shell=True, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("launch failed for %s: %s", app.get("bundle"), exc)

    def focus(self, agent: dict[str, Any]) -> None:
        """Run the focus command for a pressed agent, never raising."""
        try:
            command = self.focus_cmd.format(
                name=shlex.quote(agent.get("name", "")),
                cwd=shlex.quote(agent.get("cwd", "")),
                url=shlex.quote(agent.get("url", "")),
                web_url=shlex.quote(agent.get("web_url", "")),
                source=shlex.quote(agent.get("source", "")),
                thread_id=shlex.quote(agent.get("thread_id", "")),
                session_id=shlex.quote(agent.get("session_id", "")),
                tty=shlex.quote(agent.get("tty", "")),
                app=shlex.quote(agent.get("app", "")),
                surface=shlex.quote(agent.get("surface", "")),
                herdr_pane=shlex.quote(agent.get("herdr_pane", "")),
            )
        except (KeyError, ValueError, IndexError) as exc:
            log.warning("invalid focus template %r: %s", self.focus_cmd, exc)
            return
        log.info("focus: %s", command)
        try:
            subprocess.run(
                command, shell=True, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("focus failed for %s: %s", agent.get("name"), exc)

    # -- wire -------------------------------------------------------------
    async def _send(self, message: dict[str, Any]) -> None:
        if self.ws is None:
            return
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            await self.ws.send(json.dumps(message))

    async def publish(self, force: bool = False) -> None:
        faces = self.build_faces(self.collect())
        if not force and faces == self._last_payload:
            return
        self._last_payload = faces
        await self._send({
            "type": "faces",
            "faces": [{"index": i, **f} for i, f in sorted(faces.items())],
        })

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.publish()
                if self.health is not None:
                    self.health.heartbeat(5.0, transport="websocket", peer=self.url)
            except Exception:
                log.exception("publish failed")
            await asyncio.sleep(self.poll_interval)

    async def _badge_loop(self) -> None:
        """Refresh OS badge state without ever delaying a deck button event."""
        while True:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self.badge_provider.refresh)
                await self.publish(force=True)
            except Exception:
                log.exception("app badge refresh failed")
            await asyncio.sleep(max(3.0, self.poll_interval))

    def _start_action(self, function: Any, argument: dict[str, Any]) -> None:
        """Dispatch a blocking desktop action without stalling deck input."""
        future = asyncio.get_running_loop().run_in_executor(
            None, function, argument)
        self._actions.add(future)
        future.add_done_callback(self._actions.discard)

    async def _handle(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind not in ("press", "release"):
            return
        raw_index = message.get("index")
        if raw_index is None:
            return
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return

        # The deck reports press and release separately, so a long press is
        # just the gap between them.  Acting on RELEASE rather than press is
        # what makes that possible: a hold cannot be distinguished from a tap
        # until the finger comes up.
        if kind == "press":
            self._down[index] = time.monotonic()
            return
        held = time.monotonic() - self._down.pop(index, time.monotonic())

        agent = self._agent_keys.get(index)
        if agent is not None:
            if agent.get("system_notice"):
                self.mark_seen(agent)
                if agent.get("url"):
                    self._start_action(self.launch, agent)
                await self.publish(force=True)
                return
            if held >= LONG_PRESS_S:
                # Hold means "I am finished with this": take the key back
                # rather than following it. Deliberately does NOT focus, or
                # every dismissal would drag a window to the front.
                self.dismiss(agent)
                await self.publish(force=True)
                return
            self.mark_seen(agent)
            self._start_action(self.focus, agent)
            await self.publish(force=True)
            return
        if index == self._page_key:
            # Repaint at once rather than waiting out the poll interval.  A
            # deck key that takes a second to visibly respond feels broken, and
            # the operator presses it again and lands two pages away.
            self.page += 1
            await self.publish(force=True)
            return
        app = self._launcher_keys.get(index)
        if app is not None:
            self._start_action(self.launch, app)

    async def _run_connection(self) -> None:
        first, last = self.claim
        async with websockets.connect(self.url) as ws:
            self.ws = ws
            await self._send({
                "type": "hello", "role": "connector",
                "name": self.name, "claim": [first, last],
            })
            welcome = json.loads(await ws.recv())
            if welcome.get("type") == "error":
                raise RuntimeError(welcome.get("detail") or welcome.get("reason") or "deckd rejected connector")
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"unexpected deckd response: {welcome!r}")
            if self.health is not None:
                self.health.ready(transport="websocket", peer=self.url)
            # deckd releases claims and blanks their keys on disconnect.  The
            # faces may be byte-for-byte unchanged, but a new connection must
            # still republish them rather than trusting the old payload cache.
            self._last_payload = None
            await self.publish(force=True)
            poller = asyncio.create_task(self._poll_loop())
            badge_poller = asyncio.create_task(self._badge_loop())
            try:
                async for raw in ws:
                    try:
                        await self._handle(json.loads(raw))
                    except ValueError:
                        continue
            finally:
                poller.cancel()
                badge_poller.cancel()
                await asyncio.gather(poller, badge_poller, return_exceptions=True)
                self.ws = None

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        await reconnect_forever(
            self._run_connection,
            name=self.name,
            reporter=self.health,
            policy=RetryPolicy(initial=0.5, maximum=30.0),
            stop_event=stop_event,
            on_error=lambda exc, delay: log.warning(
                "agent connector disconnected: %s; retrying in %.1fs", exc, delay
            ),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--claim", type=int, nargs=2, metavar=("FIRST", "LAST"),
        default=list(DEFAULT_CLAIM), help="inclusive key range (default: 0 13)",
    )
    parser.add_argument("--hermes-state", default=DEFAULT_HERMES_STATE)
    parser.add_argument("--local-state", default=DEFAULT_LOCAL_STATE)
    parser.add_argument("--desktop-state", default=DEFAULT_DESKTOP_STATE)
    parser.add_argument("--t3code-state", default=DEFAULT_T3CODE_STATE)
    parser.add_argument("--focus-cmd", default=DEFAULT_FOCUS_CMD)
    parser.add_argument("--apps-config", default=DEFAULT_APPS_CONFIG)
    parser.add_argument("--launch-cmd", default=DEFAULT_LAUNCH_CMD)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--name", default="agents")
    parser.add_argument(
        "--once", action="store_true",
        help="print the faces that would be sent and exit (no hub needed)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    connector = AgentConnector(
        url=f"ws://{args.host}:{args.port}",
        claim=(args.claim[0], args.claim[1]),
        hermes_state=args.hermes_state,
        local_state=args.local_state,
        desktop_state=args.desktop_state,
        t3code_state=args.t3code_state,
        focus_cmd=args.focus_cmd,
        poll_interval=args.poll_interval,
        max_age_hours=args.max_age_hours,
        name=args.name,
        apps_config=args.apps_config,
        launch_cmd=args.launch_cmd,
        health=HealthReporter("connector_agents", stale_after=20.0),
        hermes_health=default_health_path("hermes_agents"),
    )
    if args.once:
        faces = connector.build_faces(connector.collect())
        for index, face in sorted(faces.items()):
            badge = face.get("badge") or " "
            label = face.get("label") or ""
            sub = face.get("sublabel") or ""
            print(f"key {index:>2}  [{badge}] {label:<13} {sub:<11} {face['color']}  {face['effect']}")
        return 0
    try:
        asyncio.run(connector.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
