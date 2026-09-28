#!/usr/bin/env python3
"""Read-only probe for live Hermes Discord agent threads.

The probe runs on the Hermes host and prints exactly one JSON document to stdout
using this stable contract::

    {"agents": [{"name": "<short label>", "title": "<full title>",
                  "status": "working|done|idle", "thread_id": "...",
                  "url": "https://discord.com/channels/<guild>/<thread_id>",
                  "last_activity": "<description or ''>",
                  "last_activity_at": 0.0, "cwd": "...",
                  "profile": "default|work|..."}]}

It never writes to the database.  Discord sessions are filtered to the recent
window, deduplicated by thread_id (keeping the row with the greatest activity
time), and ranked with actively-working sessions first.  The status is only a
best-effort activity status: approval/blocked state is intentionally owned by
the separate ``hermes_discord_watcher.py``.

Hermes multiplexes profiles. The default profile keeps its database at the
given path; every extra profile keeps its own under ``profiles/<name>/state.db``
beside it. Reading only the default database silently hid every work-profile
thread from the deck, so the probe merges all discoverable profile databases.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB = "/home/hermes/.hermes/state.db"
DEFAULT_LIMIT = 5
DEFAULT_GUILD_ID = ""
#: Sources worth a deck key.  ``discord`` sessions are threads you can jump to
#: in the Discord app.  ``cli`` and ``tui`` sessions are Hermes agents running
#: in a terminal on the Hermes host, which is what you get after
#: ``cmux ssh hermes`` followed by starting an agent: there is no Discord
#: thread to open, so the deck key focuses the ssh pane instead.
#: ``subagent`` rows are excluded: they are children of another session, carry
#: no title or cwd, and would crowd the board with anonymous keys.
DEFAULT_SOURCES = ("discord", "cli", "tui")
DEFAULT_SOURCE = "discord"
DEFAULT_MAX_AGE_HOURS = 24.0
BUSY_TIMEOUT_MS = 250

#: Directory, beside the default database, holding one state database per extra
#: Hermes profile (``profiles/<name>/state.db``).
PROFILES_DIRNAME = "profiles"
PROFILE_DB_FILENAME = "state.db"
DEFAULT_PROFILE = "default"

#: A session whose activity description is nonblank is mid-turn.  Hermes stamps
#: that description on a heartbeat and clears it when the turn ends, so a stale
#: nonblank description means the process died mid-turn.  Treat a "working"
#: session whose heartbeat stopped this long ago as no longer live.
WORKING_HEARTBEAT_GRACE_S = 180.0

#: Window in which a finished turn still counts as "done" (fresh, unseen)
#: rather than decaying to plain idle.
DONE_WINDOW_S = 1800.0

#: Activity descriptions that mean the agent is waiting on the user rather than
#: working.  Matched case-insensitively as substrings.
BLOCKED_HINTS = (
    "approval",
    "waiting for user",
    "awaiting user",
    "permission",
)

#: Words dropped from a thread title when building a tiny key label.
LABEL_STOPWORDS = frozenset({
    "a", "an", "and", "the", "for", "of", "to", "in", "on", "with", "how",
    "is", "are", "my", "our",
})
LABEL_CHARS = 11

#: Urgency order for ranking, highest first.  Shared with the connector so the
#: deck and the probe agree on what "most important" means.
STATUS_RANK = {"blocked": 3, "working": 2, "done": 1, "idle": 0}

#: Deck source tag per Hermes session source.  The connector uses this for the
#: corner badge and to decide how a press should focus the session: a Discord
#: thread opens in Discord, an ssh-hosted agent focuses its terminal pane.
SOURCE_TAGS = {
    "discord": "hermes-discord",
    "cli": "hermes-ssh",
    "tui": "hermes-ssh",
}

# Hermes (and some gateways) start a throwaway CLI session whose entire prompt
# is "return PONG" / "ping" to prove the binary is alive. Those sessions have
# no thread, live in /tmp, and otherwise look like finished work, so they steal
# a deck key until they age out. Match only a title that *is* the probe, not a
# real task that happens to mention ping-pong.
_LIVENESS_TITLE = re.compile(
    r"^(?:return\s+)?pong$|^ping$",
    re.IGNORECASE,
)


def _activity_value(value: Any) -> float:
    """Convert a database activity timestamp to a sortable number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def short_label(title: Any, thread_id: str) -> str:
    """Return a punctuation-free label short enough for a Stream Deck key.

    Thread titles carry a trailing ``#N`` counter and leading filler words that
    waste the ~8 characters a key can actually show, so both are stripped.  A
    session with no title at all gets a human-readable fallback rather than an
    opaque id fragment: an operator can act on "agent 8668", but "thread8668"
    only ever looked like a bug.
    """
    if title is not None and str(title).strip():
        text = re.sub(r"#\d+\s*$", "", str(title))
        cleaned = re.sub(r"[^\w\s]|_", " ", text, flags=re.UNICODE)
        words = cleaned.split()
        keep = [w for w in words if w.lower() not in LABEL_STOPWORDS] or words
        if keep:
            label = " ".join(keep[:2])[:LABEL_CHARS].rstrip()
            if label:
                return label
    tail = str(thread_id)[-4:] if thread_id else ""
    return f"agent {tail}".rstrip() if tail else "agent"


def is_liveness_probe_session(agent: dict[str, Any]) -> bool:
    """True for throwaway ping/pong health-check sessions, not real work."""
    for key in ("title", "name"):
        text = re.sub(r"\s+", " ", str(agent.get(key) or "")).strip()
        if text and _LIVENESS_TITLE.fullmatch(text):
            return True
    return False


def drop_liveness_probes(document: dict[str, Any]) -> dict[str, Any]:
    """Strip ping/pong health checks from a probe document, in place."""
    agents = document.get("agents")
    if isinstance(agents, list):
        document["agents"] = [
            item for item in agents
            if not (isinstance(item, dict) and is_liveness_probe_session(item))
        ]
    return document


def infer_status(
    description: str, activity_at: float, now: float, *, ended: bool = False
) -> str:
    """Classify one session from its activity description and heartbeat age.

    Hermes writes a description while a turn runs and clears it afterwards, so
    a nonblank description normally means "working".  It is only trusted while
    the heartbeat is fresh: a killed process leaves its last description behind
    forever, and a key stuck on amber is worse than one that decays to idle.

    ``ended`` marks a session Hermes has already closed.  A closed session
    cannot be working or blocked whatever description it left behind, so it
    decays straight to done/idle; without this a short one-shot agent holds an
    amber key long after its process exited.
    """
    text = description.strip()
    age = float("inf") if activity_at == float("-inf") else max(0.0, now - activity_at)
    if text and not ended:
        lowered = text.lower()
        if any(hint in lowered for hint in BLOCKED_HINTS):
            return "blocked"
        if age <= WORKING_HEARTBEAT_GRACE_S:
            return "working"
        # Heartbeat went quiet mid-turn: the session is not live any more.
        return "done" if age <= DONE_WINDOW_S else "idle"
    if age <= DONE_WINDOW_S:
        return "done"
    return "idle"


def _row_value(row: sqlite3.Row, column: str) -> Any:
    """Return a column if the row has it, else None.

    Older callers and test fixtures build rows without the newer columns, so a
    missing column must degrade rather than raise.
    """
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


DISCORD_GUILD_IN_URL = re.compile(
    r"(?:https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/"
    r"|discord://(?:-/)?channels/)(\d+)"
)
DISCORD_ROUTE_IN_URL = re.compile(
    r"(?:https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/"
    r"|discord://(?:-/)?channels/)(\d+|@me)/(\d+)"
)


def guild_id_from_discord_url(url: str) -> str:
    """Extract the guild snowflake from a Discord https or discord:// URL."""
    match = DISCORD_GUILD_IN_URL.search(str(url or ""))
    return match.group(1) if match else ""


def discord_route_ids(url: str) -> tuple[str, str]:
    """Return ``(guild, channel_or_thread)`` snowflakes from a jump URL.

    ``discord://-/channels/...`` and ``https://discord.com/channels/...`` name
    the same place. A trailing message id is ignored so a selected message
    still acknowledges the channel.
    """
    match = DISCORD_ROUTE_IN_URL.search(str(url or ""))
    if not match:
        return "", ""
    guild, target = match.group(1), match.group(2)
    return ("" if guild == "@me" else guild), target


def discord_url_in_text(text: str) -> str:
    """Return the first Discord jump URL in ``text``, or empty."""
    match = DISCORD_ROUTE_IN_URL.search(str(text or ""))
    return match.group(0) if match else ""


def discord_jump_url(
    guild_id: str, thread_id: str = "", chat_id: str = ""
) -> str:
    """Return a Discord https jump URL, or empty when the guild is unknown.

    A thread id is preferred. Channel-level Hermes work has only ``chat_id``.
    The URL is unusable without a real guild: Discord's ``@me`` form opens the
    app but cannot land on a server thread.
    """
    guild = str(guild_id or "").strip()
    target = str(thread_id or chat_id or "").strip()
    if not guild or not target:
        return ""
    return f"https://discord.com/channels/{guild}/{target}"


def profile_db_paths(
    db_path: str | Path, *, include_profiles: bool = True
) -> list[tuple[Path, str]]:
    """Return ``(path, profile)`` for the default DB and every sibling profile.

    The default profile keeps its database at ``db_path``. Extra profiles live
    under a ``profiles/`` directory next to it, each with its own ``state.db``.
    A missing or unreadable directory degrades to just the default database, so
    a host without profiles behaves exactly as before.
    """
    main = Path(db_path).expanduser()
    found: list[tuple[Path, str]] = [(main, DEFAULT_PROFILE)]
    if not include_profiles:
        return found
    try:
        children = sorted(
            child for child in (main.parent / PROFILES_DIRNAME).iterdir()
            if child.is_dir()
        )
    except OSError:
        return found
    for child in children:
        candidate = child / PROFILE_DB_FILENAME
        if candidate.is_file():
            found.append((candidate, child.name))
    return found


def finished_sessions(connection: sqlite3.Connection, ids: Iterable[str]) -> set[str]:
    """Sessions whose newest message is a final assistant reply.

    Hermes sometimes leaves "starting new turn" behind after it has answered,
    which would read as working until the heartbeat grace runs out.
    """
    done: set[str] = set()
    for session_id in ids:
        try:
            row = connection.execute(
                "SELECT role, tool_calls FROM messages WHERE session_id = ? "
                "ORDER BY id DESC LIMIT 1", (session_id,),
            ).fetchone()
        except sqlite3.Error:
            return done
        if row and row[0] == "assistant" and not row[1]:
            done.add(session_id)
    return done


def _row_to_agent(
    row: sqlite3.Row, *, guild_id: str, now: float, replied: set[str] = frozenset(),
    profile: str = DEFAULT_PROFILE,
) -> dict[str, Any]:
    raw_source = str(row["source"] or "").strip().lower()
    source_tag = SOURCE_TAGS.get(raw_source, "hermes-ssh")
    thread_id = "" if row["thread_id"] is None else str(row["thread_id"]).strip()
    chat_id = "" if _row_value(row, "chat_id") is None else str(_row_value(row, "chat_id")).strip()
    title = "" if row["title"] is None else str(row["title"])
    description = "" if row["last_activity_description"] is None else str(row["last_activity_description"])
    if str(row["id"]) in replied:
        description = ""
    activity_at = _activity_value(row["last_activity_at"])
    ended = _row_value(row, "ended_at") is not None
    status = infer_status(description, activity_at, now, ended=ended)
    session_id = "" if row["id"] is None else str(row["id"])
    # A Discord agent jumps to its thread when it has one and to its channel
    # otherwise: plenty of Hermes work happens at channel level, and those keys
    # used to be dead because only thread_id was considered.  An ssh-hosted
    # agent has no URL at all, so the connector focuses its terminal pane.
    url = ""
    if source_tag == "hermes-discord":
        url = discord_jump_url(guild_id, thread_id, chat_id)
    return {
        "name": short_label(row["title"], thread_id or session_id),
        "title": title,
        "status": status,
        "thread_id": thread_id,
        "session_id": session_id,
        "url": url,
        "last_activity": description,
        "last_activity_at": 0.0 if activity_at == float("-inf") else activity_at,
        "cwd": "" if row["cwd"] is None else str(row["cwd"]),
        "source": source_tag,
        "model": str(_row_value(row, "model") or ""),
        # The row's own profile wins when present; otherwise it is the profile
        # directory the row was read from.
        "profile": str(_row_value(row, "profile_name") or "").strip() or profile,
    }


def _row_precedence(row: sqlite3.Row) -> tuple[int, float]:
    """Rank duplicate rows for the same thread: live rows beat closed ones.

    Hermes writes an extra row per context compression and stamps the older
    copy with ``end_reason='compression'``.  Those copies can carry a newer
    heartbeat than the row still serving the thread, so ordering on timestamp
    alone would let a closed bookkeeping row decide the key's status.
    """
    live = 0 if _row_value(row, "ended_at") is not None else 1
    return (live, _activity_value(row["last_activity_at"]))


def is_anonymous_discord_bookkeeping_row(row: sqlite3.Row) -> bool:
    """Return whether a Discord row is bookkeeping rather than a session.

    Hermes can emit a short-lived parent-channel record when it creates or
    closes work elsewhere.  It has no thread, title, workspace, or activity,
    and Deckbridge can only render an opaque ``agent abcd`` fallback for it.
    A named or active channel-level session remains eligible for the deck.
    """
    if str(row["source"] or "").strip().lower() != "discord":
        return False
    return not any(str(_row_value(row, field) or "").strip() for field in (
        "thread_id", "title", "cwd", "last_activity_description",
    ))


def _read_rows(
    path: Path, sources: list[str], cutoff: float,
) -> tuple[list[sqlite3.Row], set[str]] | None:
    """Read eligible rows and replied session ids from one state database.

    Returns None when the database cannot be opened or queried, so the caller
    can tell an unreadable profile apart from an empty one.
    """
    placeholders = ", ".join("?" for _ in sources)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        rows = connection.execute(
            f"""
            SELECT *
            FROM sessions
            WHERE source IN ({placeholders})
              AND archived = 0
              AND last_activity_at IS NOT NULL
              AND last_activity_at >= ?
            """,
            (*sources, cutoff),
        ).fetchall()
        replied = finished_sessions(connection, [
            str(row["id"]) for row in rows
            if str(row["last_activity_description"] or "").strip()
        ])
        return rows, replied
    except (sqlite3.Error, OSError, ValueError):
        return None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


def probe(
    db_path: str | Path = DEFAULT_DB,
    *,
    limit: int = DEFAULT_LIMIT,
    guild_id: str = DEFAULT_GUILD_ID,
    source: str | Iterable[str] = DEFAULT_SOURCES,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: float | None = None,
    active_only: bool = True,
    include_profiles: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Read and rank sessions across the default and every profile DB.

    Returns an empty contract only when no database could be read at all.  A
    profile that cannot be opened does not hide the sessions in the others.

    ``active_only`` drops sessions that are merely stale rather than live.  The
    deck has ten slots and Hermes accumulates hundreds of old threads, so
    showing every historical thread guarantees the interesting ones are pushed
    off the board.  Blocked and working sessions are always kept.

    ``source`` accepts one source or several.  Discord threads and terminal
    (``cli``/``tui``) sessions are both real Hermes agents worth a key; only the
    press behaviour differs.
    """
    try:
        current = time.time() if now is None else float(now)
        age = max(0.0, float(max_age_hours))
        cutoff = current - age * 3600.0
        safe_limit = max(0, int(limit))
    except (TypeError, ValueError, OverflowError):
        return {"agents": []}
    if safe_limit == 0:
        return {"agents": []}

    sources = [str(source)] if isinstance(source, str) else [str(s) for s in source]
    sources = [s for s in sources if s]
    if not sources:
        return {"agents": []}

    collected: list[tuple[sqlite3.Row, str]] = []
    replied: set[str] = set()
    read_any = False
    for path, profile in profile_db_paths(db_path, include_profiles=include_profiles):
        result = _read_rows(path, sources, cutoff)
        if result is None:
            continue
        read_any = True
        rows, rows_replied = result
        collected.extend((row, profile) for row in rows)
        replied |= rows_replied
    if not read_any:
        return {"agents": []}

    newest: dict[str, tuple[sqlite3.Row, str]] = {}
    for row, profile in collected:
        if is_anonymous_discord_bookkeeping_row(row):
            continue
        raw_thread_id = row["thread_id"]
        thread_id = "" if raw_thread_id is None else str(raw_thread_id).strip()
        # Discord rows collapse per thread (Hermes writes one row per context
        # compression).  A terminal session has no thread, so it is its own key
        # and must not be discarded for lacking one.
        if thread_id:
            key = f"thread:{thread_id}"
        else:
            session_id = row["id"]
            if session_id is None or not str(session_id).strip():
                continue
            key = f"session:{session_id}"
        previous = newest.get(key)
        if previous is None or _row_precedence(row) > _row_precedence(previous[0]):
            newest[key] = (row, profile)

    ranked = list(newest.values())
    agents = [
        _row_to_agent(
            row, guild_id=str(guild_id), now=current, replied=replied,
            profile=profile,
        )
        for row, profile in ranked
    ]
    agents = [agent for agent in agents if not is_liveness_probe_session(agent)]
    if active_only:
        agents = [a for a in agents if a["status"] != "idle"]
    # Most urgent first, then most recent, so a truncating limit keeps the
    # sessions that actually need attention.
    agents.sort(
        key=lambda a: (STATUS_RANK.get(a["status"], 0), a["last_activity_at"]),
        reverse=True,
    )
    return {"agents": agents[:safe_limit]}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="read-only SQLite state DB")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--guild-id", default=DEFAULT_GUILD_ID)
    parser.add_argument(
        "--source", action="append", default=None, metavar="SOURCE",
        help="session source to include; repeatable "
             f"(default: {' '.join(DEFAULT_SOURCES)})",
    )
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument(
        "--all", dest="active_only", action="store_false", default=True,
        help="include idle/stale sessions too (default: active sessions only)",
    )
    parser.add_argument(
        "--no-profiles", dest="include_profiles", action="store_false", default=True,
        help="read only the default DB, ignoring sibling profiles/ databases",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    document = probe(
        args.db,
        limit=args.limit,
        guild_id=args.guild_id,
        source=args.source or DEFAULT_SOURCES,
        max_age_hours=args.max_age_hours,
        active_only=args.active_only,
        include_profiles=getattr(args, "include_profiles", True),
    )
    json.dump(document, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
