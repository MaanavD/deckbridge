#!/usr/bin/env python3
"""Fixture-only tests for the read-only Hermes Discord session probe."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import hermes_agents_probe


RESULTS: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    ok = bool(condition)
    RESULTS.append((name, ok))
    suffix = "" if ok or not detail else f": {detail}"
    print(f"{'PASS' if ok else 'FAIL'} {name}{suffix}")


def make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE sessions (
            id TEXT,
            source TEXT,
            archived INTEGER,
            thread_id TEXT,
            title TEXT,
            last_activity_at REAL,
            last_activity_description TEXT,
            cwd TEXT,
            ended_at REAL,
            chat_id TEXT
        )
        """
    )
    con.commit()
    con.close()


def insert(path: Path, **values: object) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    con = sqlite3.connect(path)
    con.execute(
        f"INSERT INTO sessions ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )
    con.commit()
    con.close()


def run_probe(path: Path, *extra: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(Path(hermes_agents_probe.__file__)), "--db", str(path),
         "--guild-id", "111111111111111111", *extra],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def main() -> int:
    now = time.time()
    with tempfile.TemporaryDirectory(prefix="deckbridge-probe-test-") as tmp:
        db = Path(tmp) / "fixture.sqlite"
        make_db(db)
        insert(
            db,
            id="sess_1111",
            source="discord",
            archived=0,
            thread_id="1111111111111111111",
            title="Old duplicate title",
            last_activity_at=now - 10,
            last_activity_description="",
            cwd="/old",
        )
        insert(
            db,
            id="sess_9999",
            source="discord",
            archived=1,
            thread_id="9999999999999999999",
            title="Archived",
            last_activity_at=now,
            last_activity_description="working",
            cwd="/archived",
        )
        insert(
            db,
            id="sess_1111",
            source="discord",
            archived=0,
            thread_id="1111111111111111111",
            title="Newest duplicate title",
            last_activity_at=now - 5,
            last_activity_description="",
            cwd="/new",
        )
        insert(
            db,
            id="sess_2222",
            source="discord",
            archived=0,
            thread_id="2222222222222222222",
            title="Working API stream",
            last_activity_at=now - 10,
            last_activity_description="receiving stream response",
            cwd="/working",
        )
        insert(
            db,
            id="sess_3333",
            source="discord",
            archived=0,
            thread_id="3333333333333333333",
            title=None,
            last_activity_at=now - 3600,
            last_activity_description="",
            cwd=None,
        )
        insert(
            db,
            id="sess_4444",
            source="cli",
            archived=0,
            thread_id="4444444444444444444",
            title="Ssh hosted agent",
            last_activity_at=now,
            last_activity_description="working",
            cwd="/cli",
        )
        # A source Hermes never puts on the deck: subagents are children of
        # another session with no title or cwd of their own.
        insert(
            db,
            id="sess_sub",
            source="subagent",
            archived=0,
            thread_id="8888888888888888888",
            title=None,
            last_activity_at=now,
            last_activity_description="working",
            cwd=None,
        )
        # A session whose heartbeat died mid-turn: it still carries a "working"
        # description, but nothing has touched it for an hour.  This is the bug
        # that showed a permanently amber key for a dead agent.
        insert(
            db,
            id="sess_5555",
            source="discord",
            archived=0,
            thread_id="5555555555555555555",
            title="Zombie turn",
            last_activity_at=now - 3600,
            last_activity_description="receiving stream response",
            cwd="/zombie",
        )
        # A session waiting on the user rather than working.
        insert(
            db,
            id="sess_6666",
            source="discord",
            archived=0,
            thread_id="6666666666666666666",
            title="Needs approval now",
            last_activity_at=now - 20,
            last_activity_description="waiting for user approval",
            cwd="/blocked",
        )
        # An ancient thread with NO activity timestamp at all.  The old query
        # let these through with `last_activity_at IS NULL OR ...`, which is
        # why the board filled with dead threads.
        insert(
            db,
            id="sess_7777",
            source="discord",
            archived=0,
            thread_id="7777777777777777777",
            title="Ancient no timestamp",
            last_activity_at=None,
            last_activity_description="",
            cwd="/ancient",
        )

        # --all keeps idle sessions, which is what the older assertions assume.
        document = run_probe(db, "--all", "--limit", "50")
        agents = document["agents"]
        by_thread = {agent["thread_id"]: agent for agent in agents}
        check("dedupe keeps newest row", by_thread["1111111111111111111"]["title"] == "Newest duplicate title")
        check("blocked ranks above working",
              agents[0]["thread_id"] == "6666666666666666666", agents[0]["thread_id"])
        check("working ranks above idle",
              agents[1]["status"] == "working" and agents[-1]["status"] == "idle",
              f"{agents[1]['status']}/{agents[-1]['status']}")
        check("null title falls back to a readable label",
              by_thread["3333333333333333333"]["name"] == "agent 3333",
              by_thread["3333333333333333333"]["name"])
        check("label shortening is deck friendly", len(agents[0]["name"]) <= 11 and agents[0]["name"])
        check("cwd null becomes empty string", by_thread["3333333333333333333"]["cwd"] == "")
        check("discord threads are tagged hermes-discord",
              by_thread["2222222222222222222"]["source"] == "hermes-discord")
        check("a cli session is tagged hermes-ssh",
              by_thread["4444444444444444444"]["source"] == "hermes-ssh",
              by_thread["4444444444444444444"]["source"])
        check("subagent rows never reach the deck",
              "8888888888888888888" not in by_thread)
        check("a cli session carries its session id",
              by_thread["4444444444444444444"]["session_id"] == "sess_4444",
              by_thread["4444444444444444444"]["session_id"])
        check("a cli session has no Discord jump URL",
              by_thread["4444444444444444444"]["url"] == "")
        check("a discord thread does have a jump URL",
              by_thread["2222222222222222222"]["url"].startswith("https://discord.com/"))

        # Regression: the two bugs that put dead agents on the board.
        check("a session with no activity timestamp is excluded",
              "7777777777777777777" not in by_thread)
        check("a stale 'working' description does not stay working",
              by_thread["5555555555555555555"]["status"] != "working",
              by_thread["5555555555555555555"]["status"])
        check("an approval wait is reported as blocked",
              by_thread["6666666666666666666"]["status"] == "blocked")
        check("a fresh heartbeat is reported as working",
              by_thread["2222222222222222222"]["status"] == "working")
        check("a trailing #N counter is stripped from labels",
              by_thread["6666666666666666666"]["name"] == "Needs appro",
              by_thread["6666666666666666666"]["name"])

        # Default mode keeps only sessions worth a key.
        active = run_probe(db, "--limit", "50")["agents"]
        statuses = {a["status"] for a in active}
        check("active-only mode drops idle sessions", "idle" not in statuses,
              str(statuses))
        check("active-only mode keeps the blocked and working sessions",
              {"6666666666666666666", "2222222222222222222"}
              <= {a["thread_id"] for a in active})
        check("active-only mode returns fewer agents than --all",
              len(active) < len(agents))

        empty = Path(tmp) / "empty.sqlite"
        make_db(empty)
        check("empty DB yields no agents", run_probe(empty) == {"agents": []})

        # Sessions Hermes has closed must not hold a working/blocked key, and a
        # closed compression duplicate must not outrank the row still live on
        # the thread even when it carries a newer heartbeat.
        ended_db = Path(tmp) / "ended.sqlite"
        make_db(ended_db)
        insert(
            ended_db,
            id="sess_ended", source="cli", archived=0, thread_id="",
            title="One shot job", last_activity_at=now - 60,
            last_activity_description="running a tool", cwd="/home/hermes",
            ended_at=now - 55,
        )
        insert(
            ended_db,
            id="sess_live", source="discord", archived=0,
            thread_id="9999999999999999999", title="Live thread",
            last_activity_at=now - 120,
            last_activity_description="thinking", cwd="/home/hermes",
        )
        insert(
            ended_db,
            id="sess_compressed", source="discord", archived=0,
            thread_id="9999999999999999999", title="Live thread",
            last_activity_at=now - 30, last_activity_description="",
            cwd="/home/hermes", ended_at=now - 25,
        )
        ended_agents = {
            a["session_id"]: a for a in run_probe(ended_db, "--all", "--limit", "50")["agents"]
        }
        check("closed session is never working",
              ended_agents.get("sess_ended", {}).get("status") != "working",
              str(ended_agents.get("sess_ended", {}).get("status")))
        check("live row wins over newer closed compression duplicate",
              "sess_live" in ended_agents and "sess_compressed" not in ended_agents,
              str(sorted(ended_agents)))
        check("live row keeps its working status",
              ended_agents.get("sess_live", {}).get("status") == "working",
              str(ended_agents.get("sess_live", {}).get("status")))

        # infer_status must degrade a closed session directly, independent of DB
        # plumbing, so the rule holds for any caller.
        check("infer_status ignores description when ended",
              hermes_agents_probe.infer_status("running a tool", now, now, ended=True)
              == "done")
        check("infer_status still reports working when not ended",
              hermes_agents_probe.infer_status("running a tool", now, now) == "working")

        # A Discord agent working directly in a channel has no thread_id.  Its
        # key was dead before, because only thread_id produced a jump URL.
        chan_db = Path(tmp) / "channel.sqlite"
        make_db(chan_db)
        insert(
            chan_db,
            id="sess_channel", source="discord", archived=0, thread_id=None,
            title="Channel level work", last_activity_at=now - 10,
            last_activity_description="thinking", cwd="/home/hermes",
            chat_id="222222222222222222",
        )
        insert(
            chan_db,
            id="sess_thread", source="discord", archived=0,
            thread_id="333333333333333333", title="Thread level work",
            last_activity_at=now - 10, last_activity_description="thinking",
            cwd="/home/hermes", chat_id="222222222222222222",
        )
        insert(
            chan_db,
            id="sess_term", source="cli", archived=0, thread_id=None,
            title="Terminal work", last_activity_at=now - 10,
            last_activity_description="thinking", cwd="/home/hermes",
            chat_id="222222222222222222",
        )
        chan = {
            a["session_id"]: a
            for a in run_probe(chan_db, "--all", "--limit", "50")["agents"]
        }
        check("channel-level Discord agent falls back to a channel URL",
              chan.get("sess_channel", {}).get("url", "").endswith(
                  "/222222222222222222"),
              str(chan.get("sess_channel", {}).get("url")))
        check("thread URL still wins over channel id when a thread exists",
              chan.get("sess_thread", {}).get("url", "").endswith(
                  "/333333333333333333"),
              str(chan.get("sess_thread", {}).get("url")))
        check("ssh agent gets no URL even when a chat_id exists",
              chan.get("sess_term", {}).get("url") == "",
              str(chan.get("sess_term", {}).get("url")))

    missing = Path(tmp) / "missing.sqlite"
    failed = subprocess.run(
        [sys.executable, str(Path(hermes_agents_probe.__file__)), "--db", str(missing)],
        check=True,
        capture_output=True,
        text=True,
    )
    check("missing DB is graceful", json.loads(failed.stdout) == {"agents": []})

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
