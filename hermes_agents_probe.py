#!/usr/bin/env python3
"""Read-only probe for live Hermes Discord agent threads.

The probe runs on the Hermes host and prints exactly one JSON document to stdout
using this stable contract::

    {"agents": [{"name": "<short label>", "title": "<full title>",
                  "status": "working|done|idle", "thread_id": "...",
                  "url": "https://discord.com/channels/<guild>/<thread_id>",
                  "last_activity": "<description or ''>",
                  "last_activity_at": 0.0, "cwd": "..."}]}

It never writes to the database.  Discord sessions are filtered to the recent
window, deduplicated by thread_id (keeping the row with the greatest activity
time), and ranked with actively-working sessions first.  The status is only a
best-effort activity status: approval/blocked state is intentionally owned by
the separate ``hermes_discord_watcher.py``.
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


def _row_to_agent(row: sqlite3.Row, *, guild_id: str, now: float) -> dict[str, Any]:
    raw_source = str(row["source"] or "").strip().lower()
    source_tag = SOURCE_TAGS.get(raw_source, "hermes-ssh")
    thread_id = "" if row["thread_id"] is None else str(row["thread_id"]).strip()
    chat_id = "" if _row_value(row, "chat_id") is None else str(_row_value(row, "chat_id")).strip()
    title = "" if row["title"] is None else str(row["title"])
    description = "" if row["last_activity_description"] is None else str(row["last_activity_description"])
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
        target = thread_id or chat_id
        if target and guild_id:
            url = f"https://discord.com/channels/{guild_id}/{target}"
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


def probe(
    db_path: str | Path = DEFAULT_DB,
    *,
    limit: int = DEFAULT_LIMIT,
    guild_id: str = DEFAULT_GUILD_ID,
    source: str | Iterable[str] = DEFAULT_SOURCES,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: float | None = None,
    active_only: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Read and rank sessions, returning an empty contract on any DB failure.

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
    placeholders = ", ".join("?" for _ in sources)

    connection: sqlite3.Connection | None = None
    try:
        path = str(Path(db_path).expanduser())
        connection = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        rows = connection.execute(
            f"""
            SELECT id, source, thread_id, title, last_activity_at,
                   last_activity_description, cwd, ended_at, chat_id
            FROM sessions
            WHERE source IN ({placeholders})
              AND archived = 0
              AND last_activity_at IS NOT NULL
              AND last_activity_at >= ?
            """,
            (*sources, cutoff),
        ).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        return {"agents": []}
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    newest: dict[str, sqlite3.Row] = {}
    for row in rows:
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
        if previous is None or _row_precedence(row) > _row_precedence(previous):
            newest[key] = row

    ranked = list(newest.values())
    agents = [
        _row_to_agent(row, guild_id=str(guild_id), now=current) for row in ranked
    ]
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
    )
    json.dump(document, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
