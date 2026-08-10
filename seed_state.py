#!/usr/bin/env python3
"""seed_state.py - write fake state files for the deckbridge emulator.

The connectors read small JSON files.  This writes believable approval, live
Hermes-agent, and local cmux snapshots, and can animate them so the deck
visibly changes while you watch.

    python3 seed_state.py                 # write one realistic snapshot
    python3 seed_state.py --animate       # cycle states every 3s
    python3 seed_state.py --clear         # blank the whole board
    python3 seed_state.py --reset         # documented synonym for --clear

Files written (override with --dir):
    ~/.deckbridge/cmux_state.json
    ~/.deckbridge/hermes_approvals.json
    ~/.deckbridge/hermes_agents.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from pathlib import Path


def write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def approvals(n: int) -> dict[str, list[dict[str, object]]]:
    base = int(time.time())
    items = [
        {
            "message_id": "111", "channel_id": "222", "guild_id": "333",
            "command": "cp config.yaml config.yaml.bak",
            "reason": "overwrite project config file", "created_ts": base - 90,
            "url": "https://discord.com/channels/333/222/111",
        },
        {
            "message_id": "444", "channel_id": "222", "guild_id": "333",
            "command": "rm -rf build/", "reason": "recursive delete",
            "created_ts": base - 20,
            "url": "https://discord.com/channels/333/222/444",
        },
    ]
    return {"pending": items[:n]}


SCENES = [
    ([("codex", "working"), ("claude", "blocked"), ("opencode", "done"), ("gemini", "idle")], 2),
    ([("codex", "working"), ("claude", "working"), ("opencode", "idle"), ("gemini", "idle")], 1),
    ([("codex", "done"), ("claude", "idle"), ("opencode", "idle"), ("gemini", "idle")], 0),
    ([("codex", "blocked"), ("claude", "working"), ("opencode", "working"), ("gemini", "blocked")], 3),
]


def hermes_agents(agents: list[tuple[str, str]]) -> dict[str, list[dict[str, object]]]:
    """Build the live-agent contract used by Hermes keys 1--4 in the demo."""
    now = time.time()
    result = []
    for offset, (name, scene_status) in enumerate(agents, start=1):
        # The probe deliberately has no blocked state.  Keep the demo's
        # attention-grabbing blocked scene visible as a working thread.
        status = "working" if scene_status == "blocked" else scene_status
        result.append({
            "name": name,
            "title": f"{name} Discord thread",
            "status": status,
            "thread_id": str(900000000000000000 + offset),
            "url": f"https://discord.com/channels/111111111111111111/{900000000000000000 + offset}",
            "last_activity": "demo activity" if status == "working" else "",
            "last_activity_at": now,
            "cwd": f"~/proj/{name}",
        })
    return {"agents": result}


def scene(directory: Path, agents: list[tuple[str, str]], approval_count: int) -> None:
    write(
        directory / "cmux_state.json",
        {"agents": [{"name": n, "status": s, "cwd": f"~/proj/{n}"} for n, s in agents]},
    )
    write(directory / "hermes_approvals.json", approvals(min(approval_count, 2)))
    write(directory / "hermes_agents.json", hermes_agents(agents))
    summary = ", ".join(f"{n}={s}" for n, s in agents)
    print(f"agents: {summary} | approvals: {min(approval_count, 2)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=str(Path.home() / ".deckbridge"))
    parser.add_argument("--animate", action="store_true")
    parser.add_argument("--clear", action="store_true", help="blank every connector state file")
    parser.add_argument("--reset", action="store_true", help="synonym for --clear")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()
    directory = Path(args.dir).expanduser()

    if args.clear or args.reset:
        write(directory / "cmux_state.json", {"agents": []})
        write(directory / "hermes_approvals.json", {"pending": []})
        write(directory / "hermes_agents.json", {"agents": []})
        print("cleared all state files")
        return
    if not args.animate:
        scene(directory, *SCENES[0])
        return
    print("animating; ctrl-c to stop")
    try:
        for agents, count in itertools.cycle(SCENES):
            scene(directory, agents, count)
            time.sleep(max(0.05, args.interval))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
