#!/usr/bin/env python3
"""Unified agent connector: sessions on 0-9, fixed shortcuts on 10-13.

Every agent feed (hooks, T3 Code, Claude Desktop, Hermes) is merged by
``agent_feeds`` into one pool of up to ten slots.

**Slots are pinned, not sorted.**  The first time an agent is seen it claims
the lowest free slot and keeps it until it disappears.  Sorting the board by
status every poll makes a key change meaning between deciding to press it and
pressing it.  Priority only decides *who gets a slot* when the board is full.

**Every key has a lifetime.**  See ``RETENTION`` below; ``--once`` prints how
long each visible key has left.

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
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

import websockets

from connection_runtime import (
    ConnectionHealth,
    HealthReporter,
    RetryPolicy,
    default_health_path,
    reconnect_forever,
)

import logos
import models
from agent_feeds import (  # noqa: F401  (re-exported for tests and tools)
    STALE_WORKING_S, STATUS_ORDER, T3_SHADOW_SOURCES, VALID_STATUSES,
    agent_key, apply_discord_approvals, collapse_claude_desktop_shadows,
    collapse_t3_shadows, decay_stale, dedupe_labels, drop_unverified_idle,
    expires_at,
    ensure_hermes_discord_url, manual_view_matches, normalize_status,
    read_agents, read_pending_approvals, read_viewed,
    reconcile_local_liveness, workspace_identity,
)
from app_badges import AppBadgeProvider
from chrome_focus import (  # noqa: F401
    CHROME_BIN, _CHROME_RAISE_SCRIPT, _CHROME_TAB_SCRIPT,
    chrome_tab_match_token, chrome_tab_matches_url,
    chrome_titles_refer_to_same_window, chrome_window_belongs_to_profile,
    lookup_chrome_profile_name, open_or_focus_chrome_tab,
    raise_chrome_profile_window,
)
from hermes_agents_probe import guild_id_from_discord_url
from local_liveness import (  # noqa: F401
    LIVENESS_CACHE_S, LOCAL_SESSION_SOURCES, HerdrSshPaneResolver,
    LocalLivenessProbe,
)

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
DEFAULT_ACK_STATE = "~/.deckbridge/agent_acks.json"
TAILSCALE_AUTH_URL = re.compile(
    r"https://login\.tailscale\.com/a/[A-Za-z0-9]+"
)
DEFAULT_FOCUS_CMD = (
    "./focus_agent.sh --source {source} --name {title} --cwd {cwd} "
    "--url {url} --session {session_id} --tty {tty} --app {app} "
    "--surface {surface} --herdr-pane {herdr_pane} --web-url {web_url} "
    "--environment {environment_id} --ssh-host {ssh_host} "
    "--environment-label {environment_label}"
)

#: RETENTION.  How long a key stays on the board without new activity:
#:
#: * working / blocked   while the session is alive.  A hook feed that goes
#:   silent for ``STALE_WORKING_S`` is shown as done instead.
#: * done, not yet seen  ``DEFAULT_MAX_AGE_HOURS`` after the result arrived.
#: * done and seen, or idle  ``DEFAULT_REST_HOURS`` after you last looked
#:   (or after the last activity, if later).
#: * long-pressed        gone until the session does something new.
DEFAULT_MAX_AGE_HOURS = 24.0
DEFAULT_REST_HOURS = 2.0

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
#: The work key opens the Notion command board in the work Chrome profile.
#: It replaced Gmail: the inbox is where work arrives, the board is where work
#: is decided, and only one of those is worth a dedicated key.
COMMAND_BOARD_URL = (
    "https://app.notion.com/p/8bf47822e6014b40a9e7a081e741f321"
    "?v=39ac370222d581018fb0000c4b1137d3&pvs=32"
)
DEFAULT_SHORTCUTS = [
    {"label": "Slack", "source": "slack", "bundle": "Slack"},
    {
        "label": "Command Board", "source": "command-board",
        "bundle": "Google Chrome",
        "url": COMMAND_BOARD_URL, "profile": "Default",
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
    "gmail": "W",
    "google-chrome": "P",
    "discord": "D",
    "notion-calendar": "N",
    "command-board": "B",
}

#: The corner mark shows where a tap takes you. T3 threads open in T3 and
#: Hermes threads in Discord; the lab mark beside the model already says
#: whose model is answering.
HOME_SOURCE = {
    "t3code-claude": "t3code", "t3code-codex": "t3code",
    "t3code-cursor": "t3code", "t3code-grok": "t3code",
    "t3code-opencode": "t3code", "hermes-discord": "discord",
}

#: Human-readable status text for the key's second line.
STATUS_TEXT = {
    "blocked": "NEEDS YOU",
    "working": "working",
    "done": "done",
    "idle": "idle",
}


def _ack_stamp(value: Any) -> float:
    """Round a heartbeat so JSON and SQLite floats compare as the same event."""
    try:
        return round(float(value or 0.0), 3)
    except (TypeError, ValueError):
        return 0.0


class Ack(NamedTuple):
    """An acknowledged event: the status and heartbeat it covered, and when."""
    status: str
    stamp: float
    at: float


def _parse_ack_map(raw: Any) -> dict[str, Ack]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Ack] = {}
    for key, token in raw.items():
        if not isinstance(key, str) or not key:
            continue
        if isinstance(token, (list, tuple)) and len(token) in (2, 3):
            stamp = _ack_stamp(token[1])
            at = _ack_stamp(token[2]) if len(token) == 3 else stamp
            out[key] = Ack(str(token[0]), stamp, at)
    return out


def load_acks(path: Path) -> tuple[dict[str, Ack], dict[str, Ack]]:
    """Read persisted seen/dismissed tokens, or empty maps on any failure."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    return _parse_ack_map(data.get("seen")), _parse_ack_map(data.get("dismissed"))


def write_acks(
    path: Path, seen: dict[str, Ack], dismissed: dict[str, Ack],
) -> None:
    """Atomically persist acknowledgements so a restart cannot resurrect keys."""
    payload = {
        "seen": {key: list(ack) for key, ack in seen.items()},
        "dismissed": {key: list(ack) for key, ack in dismissed.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent), text=True,
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(payload, out)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def configured_discord_guild_id(apps_config: Path | None = None) -> str:
    """Guild used to rebuild a Hermes jump URL when the probe omitted it."""
    env = str(os.environ.get("DISCORD_GUILD_ID") or "").strip()
    if env:
        return env
    if apps_config is None:
        return ""
    for group in (read_launchers(apps_config), read_shortcuts(apps_config)):
        for item in group:
            guild = guild_id_from_discord_url(item.get("url", ""))
            if guild:
                return guild
    return ""


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
    color, effect = style["color"], style["effect"]
    if seen:
        # Dim rather than recolour, and stop animating: a breathing key you
        # have already answered trains you to ignore one you have not.
        color = dim_hex(color, SEEN_DIM)
        effect = "solid"
    model = str(agent.get("model") or "")
    sublabel = (
        agent.get("notice_label")
        or (STATUS_TEXT["blocked"] if status == "blocked" else "")
        or models.short_name(model)
        or (STATUS_TEXT["blocked"] if needs_attention
            else STATUS_TEXT.get(status, status))
    )
    home = HOME_SOURCE.get(agent.get("source", ""), agent.get("source", ""))
    return {
        "layout": "agent",
        "label": agent["name"][:48],
        "sublabel": str(sublabel)[:16],
        "badge": SOURCE_BADGE.get(home, ""),
        "source": home,
        "logo": logos.SOURCE_LOGO.get(home, ""),
        "model": model,
        "provider": models.provider(model),
        "color": color,
        # Colour and motion already say working or done; only a key that
        # wants you spends pixels on an icon.
        "icon": "alert" if visual_status == "blocked" else None,
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
        rest_hours: float = DEFAULT_REST_HOURS,
        name: str = "agents",
        apps_config: str | os.PathLike[str] = DEFAULT_APPS_CONFIG,
        launch_cmd: str = DEFAULT_LAUNCH_CMD,
        health: HealthReporter | None = None,
        hermes_health: str | os.PathLike[str] | None = None,
        remote_herdr_resolver: Any | None = None,
        badge_provider: Any | None = None,
        ack_state: str | os.PathLike[str] | None = None,
        approvals_state: str | os.PathLike[str] | None = None,
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
        self.approvals_state = (
            Path(os.path.expanduser(os.fspath(approvals_state)))
            if approvals_state is not None else Path(os.devnull)
        )
        self.focus_cmd = focus_cmd
        self.poll_interval = poll_interval
        self.max_age_s = max(0.0, max_age_hours) * 3600.0
        self.rest_s = max(0.0, rest_hours) * 3600.0
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
        # Tests construct connectors against temporary feeds and must not write
        # acknowledgements into the live ~/.deckbridge directory.  The CLI
        # always passes the configured path explicitly.
        self.ack_state = (
            Path(os.path.expanduser(os.fspath(ack_state)))
            if ack_state is not None else None
        )

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
        # agent moves on.  Loaded from disk so a LaunchAgent recycle cannot
        # resurrect a thread the operator already put away.
        if self.ack_state is not None:
            self._seen, self._dismissed = load_acks(self.ack_state)
        else:
            self._seen, self._dismissed = {}, {}
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
        guild = configured_discord_guild_id(self.apps_config)
        for agent in agents:
            ensure_hermes_discord_url(agent, guild)
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
        # T3 owns provider subprocesses, but its thread feed is not guaranteed
        # to contain their parent (for example during an environment switch).
        # Keep the hook record until collapse_t3_shadows can prove an exact
        # same-provider workspace thread is authoritative.
        local = read_agents(self.local_state, source_default="cmux")
        agents += reconcile_local_liveness(local, self.liveness_probe)
        # Native desktop conversations do not have a child CLI PID to probe.
        # Their watcher renews only while an Accessibility-visible app window
        # still exposes the exact deep-link route.
        agents += read_agents(self.desktop_state, source_default="desktop")
        agents += read_agents(self.t3code_state, source_default="t3code")
        agents = collapse_t3_shadows(agents)
        agents = collapse_claude_desktop_shadows(agents)
        agents = decay_stale(agents, current)
        agents = drop_unverified_idle(agents)
        agents = apply_discord_approvals(
            agents, read_pending_approvals(self.approvals_state),
        )
        agents = dedupe_labels(agents)
        for view in read_viewed(self.desktop_state):
            for agent in agents:
                if manual_view_matches(agent, view, agents):
                    self.mark_seen(agent, at=current)
        # Forget old acknowledgements, but not merely because the agent left
        # this poll: Hermes ranking can drop a finished thread for one cycle
        # and put it back with the same heartbeat.
        cutoff = current - self.max_age_s - self.rest_s
        expired = False
        for store in (self._seen, self._dismissed):
            for key in [k for k, ack in store.items()
                        if max(ack.stamp, ack.at) < cutoff]:
                del store[key]
                expired = True
        if expired:
            self._persist_acks()
        return [agent for agent in agents
                if not self._is_dismissed(agent)
                and not self._expired(agent, current)]

    def expires_at(self, agent: dict[str, Any]) -> float | None:
        return expires_at(agent, self._seen_at(agent), self.max_age_s, self.rest_s)

    def _expired(self, agent: dict[str, Any], now: float) -> bool:
        deadline = self.expires_at(agent)
        return deadline is not None and deadline <= now

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
        return (str(agent.get("status", "")), _ack_stamp(agent.get("updated_at")))

    def _ack_covers(self, stored: Ack | None, agent: dict[str, Any]) -> bool:
        """True when ``stored`` still accounts for the agent's current event.

        A later, *less* urgent status on the same heartbeat is the same turn
        decaying (Hermes clearing its in-progress description), not a new
        result.  A more urgent status, or any newer heartbeat, is new work.
        """
        if stored is None:
            return False
        current = self._ack_token(agent)
        if stored[1] != current[1]:
            return False
        return STATUS_ORDER.get(current[0], 0) <= STATUS_ORDER.get(stored[0], 0)

    def _is_seen(self, agent: dict[str, Any]) -> bool:
        """Has this agent been acknowledged as it stands RIGHT NOW?"""
        return self._ack_covers(self._seen.get(agent_key(agent)), agent)

    def _seen_at(self, agent: dict[str, Any]) -> float | None:
        ack = self._seen.get(agent_key(agent))
        return ack.at if self._ack_covers(ack, agent) else None

    def mark_seen(self, agent: dict[str, Any], at: float | None = None) -> None:
        key = agent_key(agent)
        existing = self._seen.get(key)
        if existing is not None and self._ack_covers(existing, agent):
            return
        self._seen[key] = Ack(*self._ack_token(agent), time.time() if at is None else at)
        self._persist_acks()

    def dismiss(self, agent: dict[str, Any]) -> None:
        """Drop an agent from the board until it does something new.

        This is the long-press.  Unlike ``mark_seen`` it takes the key back,
        which is the point: a finished session you have dealt with is clutter,
        and clutter is what pushes live agents onto page 2.
        """
        key = agent_key(agent)
        existing = self._dismissed.get(key)
        if existing is None or not self._ack_covers(existing, agent):
            self._dismissed[key] = Ack(*self._ack_token(agent), time.time())
        self._slots.remove_and_compact(key)
        self._persist_acks()

    def _is_dismissed(self, agent: dict[str, Any]) -> bool:
        return self._ack_covers(self._dismissed.get(agent_key(agent)), agent)

    def _persist_acks(self) -> None:
        if self.ack_state is None:
            return
        try:
            write_acks(self.ack_state, self._seen, self._dismissed)
        except OSError:
            log.warning("could not persist agent acknowledgements", exc_info=True)

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
        # Chrome is one process for every profile. Activating the app or
        # sending the binary a URL often creates another window of that
        # profile. Raise the existing work/personal window first; only spawn
        # Chrome when that profile has no window at all.
        if self.launch_cmd == DEFAULT_LAUNCH_CMD and profile:
            window_name = (
                profile_name or lookup_chrome_profile_name(profile) or profile
            )
            if url:
                if open_or_focus_chrome_tab(window_name, url):
                    return
                command_argv = [
                    CHROME_BIN, f"--profile-directory={profile}", url,
                ]
                log.info("launch URL: %s", command_argv)
                try:
                    subprocess.run(
                        command_argv, check=False, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=15,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    log.warning("launch URL failed for %s: %s", app.get("label"), exc)
                return
            if raise_chrome_profile_window(window_name):
                return
            command_argv = [
                CHROME_BIN, f"--profile-directory={profile}",
                "--new-window", "chrome://newtab/",
            ]
            log.info("launch Chrome profile window: %s", command_argv)
            try:
                subprocess.run(
                    command_argv, check=False, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("Chrome profile launch failed for %s: %s", window_name, exc)
            return
        if self.launch_cmd == DEFAULT_LAUNCH_CMD and url:
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
        ensure_hermes_discord_url(agent, configured_discord_guild_id(self.apps_config))
        try:
            command = self.focus_cmd.format(
                name=shlex.quote(agent.get("name", "")),
                title=shlex.quote(agent.get("title") or agent.get("name", "")),
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
                environment_id=shlex.quote(agent.get("environment_id", "")),
                ssh_host=shlex.quote(agent.get("ssh_host", "")),
                environment_label=shlex.quote(agent.get("environment_label", "")),
            )
        except (KeyError, ValueError, IndexError) as exc:
            log.warning("invalid focus template %r: %s", self.focus_cmd, exc)
            return
        log.info("focus: %s", command)
        try:
            result = subprocess.run(
                command, shell=True, check=False, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15,
            )
            detail = (result.stdout or "").strip()
            if result.returncode != 0:
                log.warning("focus exited %s for %s: %s", result.returncode,
                            agent.get("name"), detail or "no detail")
            elif detail:
                log.info("focus result for %s: %s", agent.get("name"), detail)
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


def lifetime(deadline: float | None, now: float) -> str:
    if deadline is None:
        return "stays while active"
    minutes = max(0, int((deadline - now) // 60))
    return f"leaves in {minutes // 60}h{minutes % 60:02d}m"


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
    parser.add_argument("--approvals-state", default=DEFAULT_APPROVALS_STATE)
    parser.add_argument("--ack-state", default=DEFAULT_ACK_STATE)
    parser.add_argument("--focus-cmd", default=DEFAULT_FOCUS_CMD)
    parser.add_argument("--apps-config", default=DEFAULT_APPS_CONFIG)
    parser.add_argument("--launch-cmd", default=DEFAULT_LAUNCH_CMD)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help="unseen results stay this long")
    parser.add_argument("--rest-hours", type=float, default=DEFAULT_REST_HOURS,
                        help="seen results and idle sessions stay this long after you look")
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
        approvals_state=args.approvals_state,
        ack_state=args.ack_state,
        focus_cmd=args.focus_cmd,
        poll_interval=args.poll_interval,
        max_age_hours=args.max_age_hours,
        rest_hours=args.rest_hours,
        name=args.name,
        apps_config=args.apps_config,
        launch_cmd=args.launch_cmd,
        health=HealthReporter("connector_agents", stale_after=20.0),
        hermes_health=default_health_path("hermes_agents"),
    )
    if args.once:
        now = time.time()
        faces = connector.build_faces(connector.collect(now))
        for index, face in sorted(faces.items()):
            agent = connector._agent_keys.get(index)
            line = f"key {index:>2}  {face.get('label') or '':<13} {face.get('sublabel') or '':<13}"
            if agent is not None:
                line += f" {agent['status']:<8} {agent.get('model') or '-':<22} {lifetime(connector.expires_at(agent), now)}"
            print(line.rstrip())
        return 0
    try:
        asyncio.run(connector.run())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
