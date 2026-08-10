#!/usr/bin/env python3
"""Install deckbridge hooks into Claude Code, Codex CLI, and Cursor configs.

This edits real user config, so it behaves conservatively:

* **Dry run by default.**  Nothing is written until you pass ``--apply``.
* **Merge, never clobber.**  Existing hooks for an event are preserved and the
  deckbridge handler is appended.  Unrelated settings are untouched.
* **Idempotent.**  A handler already pointing at this checkout is not added
  twice, so re-running after an upgrade is safe.
* **Backups.**  Every file it modifies is copied to ``<name>.bak-<timestamp>``
  first, and the path is printed.
* **Absolute paths.**  Hook commands are written as absolute paths to this
  checkout, because agents run hooks with the session cwd, not the repo root.

Usage::

    python3 install_hooks.py                 # show the plan, write nothing
    python3 install_hooks.py --apply         # install for all three tools
    python3 install_hooks.py --apply --only claude
    python3 install_hooks.py --uninstall --apply
    python3 install_hooks.py --apply --ttl 300

After installing for Codex, run ``/hooks`` inside Codex and TRUST the new
hooks.  Codex records trust against each hook's hash and silently skips
untrusted hooks, which looks identical to a broken install.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

CLAUDE_SETTINGS = Path("~/.claude/settings.json").expanduser()
CODEX_HOOKS = Path("~/.codex/hooks.json").expanduser()
CURSOR_HOOKS = Path("~/.cursor/hooks.json").expanduser()

#: Events to register per tool.  Claude has SessionEnd/StopFailure; Codex does
#: not, and Codex adds SubagentStart.  Registering an event a tool never emits
#: is harmless, but keeping the lists honest keeps the configs readable.
CLAUDE_EVENTS = [
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
    "Notification", "Stop", "StopFailure", "SessionEnd",
]
CODEX_EVENTS = [
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PermissionRequest", "SubagentStart", "SubagentStop", "Stop",
]
CURSOR_EVENTS = [
    "sessionStart", "beforeSubmitPrompt", "afterAgentThought",
    "afterAgentResponse", "stop", "sessionEnd",
]

TOOLS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude Code",
        "path": CLAUDE_SETTINGS,
        "shim": HERE / "claude_shim.py",
        "events": CLAUDE_EVENTS,
        "schema": "nested",
    },
    "codex": {
        "label": "Codex CLI",
        "path": CODEX_HOOKS,
        "shim": HERE / "codex_shim.py",
        "events": CODEX_EVENTS,
        "schema": "nested",
    },
    "cursor": {
        "label": "Cursor",
        "path": CURSOR_HOOKS,
        "shim": HERE / "cursor_shim.py",
        "events": CURSOR_EVENTS,
        "schema": "flat-v1",
        "extra_args": ("--app", "Cursor"),
    },
}


def command_for(
    shim: Path, ttl: float | None, extra_args: tuple[str, ...] = (),
) -> str:
    """Build the hook command string, quoting only when necessary."""
    text = str(shim)
    if " " in text:
        text = f'"{text}"'
    for value in extra_args:
        part = str(value)
        if not part or any(char.isspace() for char in part):
            part = json.dumps(part)
        text = f"{text} {part}"
    if ttl is not None:
        text = f"{text} --ttl {ttl:g}"
    return text


def is_ours(command: object) -> bool:
    """True when a hook command points at any deckbridge shim checkout."""
    if not isinstance(command, str):
        return False
    return any(name in command for name in (
        "claude_shim.py", "codex_shim.py", "cursor_shim.py",
    ))


def load_json(path: Path) -> tuple[dict[str, Any], str | None]:
    """Load a config file, returning ({}, reason) when unusable."""
    if not path.exists():
        return {}, None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"cannot read: {exc}"
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, f"existing file is not valid JSON ({exc}); refusing to touch it"
    if not isinstance(data, dict):
        return {}, "existing file is not a JSON object; refusing to touch it"
    return data, None


def plan_install(
    config: dict[str, Any], events: list[str], command: str,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return (new_config, added_events, skipped_events) without writing."""
    out = json.loads(json.dumps(config))  # deep copy via round-trip
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' in the existing config is not an object")

    added: list[str] = []
    skipped: list[str] = []
    for event in events:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} is not a list")
        existing = None
        for group in groups:
            if not isinstance(group, dict):
                continue
            for handler in group.get("hooks", []):
                if isinstance(handler, dict) and is_ours(handler.get("command")):
                    existing = handler
                    break
            if existing is not None:
                break
        if existing is not None and existing.get("command") == command:
            skipped.append(event)
            continue
        if existing is not None:
            # Two extracted copies can coexist. Treating a handler from another
            # checkout as current makes every later fix inert while presses
            # keep running stale code. Update it in place so unrelated handlers
            # (including Herdr's integration) retain their ordering and config.
            existing["command"] = command
            added.append(event)
            continue
        groups.append({"hooks": [{"type": "command", "command": command}]})
        added.append(event)
    return out, added, skipped


def plan_uninstall(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Strip every deckbridge handler, returning (new_config, removed_events)."""
    out = json.loads(json.dumps(config))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out, []
    removed: list[str] = []
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        touched = False
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                kept_groups.append(group)
                continue
            kept = [
                h for h in handlers
                if not (isinstance(h, dict) and is_ours(h.get("command")))
            ]
            if len(kept) != len(handlers):
                touched = True
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                kept_groups.append(new_group)
        if touched:
            removed.append(event)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event)
    if not hooks:
        out.pop("hooks", None)
    return out, removed


def plan_install_flat(
    config: dict[str, Any], events: list[str], command: str,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Install hooks using Cursor's ``hooks.<event> = [{command}]`` schema."""
    out = json.loads(json.dumps(config))
    version = out.setdefault("version", 1)
    if version != 1:
        raise ValueError("Cursor hooks config version is not 1; refusing to rewrite it")
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' in the existing config is not an object")

    added: list[str] = []
    skipped: list[str] = []
    for event in events:
        handlers = hooks.setdefault(event, [])
        if not isinstance(handlers, list):
            raise ValueError(f"hooks.{event} is not a list")
        existing = next((
            handler for handler in handlers
            if isinstance(handler, dict) and is_ours(handler.get("command"))
        ), None)
        if existing is not None and existing.get("command") == command:
            skipped.append(event)
            continue
        if existing is not None:
            existing["command"] = command
        else:
            handlers.append({"command": command})
        added.append(event)
    return out, added, skipped


def plan_uninstall_flat(
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Remove deckbridge handlers from Cursor's flat hook lists."""
    out = json.loads(json.dumps(config))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out, []
    removed: list[str] = []
    for event in list(hooks):
        handlers = hooks.get(event)
        if not isinstance(handlers, list):
            continue
        kept = [
            handler for handler in handlers
            if not (isinstance(handler, dict)
                    and is_ours(handler.get("command")))
        ]
        if len(kept) == len(handlers):
            continue
        removed.append(event)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not hooks:
        out.pop("hooks", None)
    return out, removed


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def ensure_executable(shim: Path) -> None:
    try:
        mode = shim.stat().st_mode
        shim.chmod(mode | 0o111)
    except OSError:
        pass


def process(tool: str, args: argparse.Namespace) -> bool:
    spec = TOOLS[tool]
    path: Path = spec["path"]
    shim: Path = spec["shim"]
    print(f"\n=== {spec['label']} ===")
    print(f"config: {path}")

    if not shim.exists():
        print(f"  !! missing shim {shim}; is this the deckbridge checkout?")
        return False

    config, problem = load_json(path)
    if problem:
        print(f"  !! {problem}")
        return False

    try:
        if args.uninstall:
            if spec.get("schema") == "flat-v1":
                new_config, changed = plan_uninstall_flat(config)
            else:
                new_config, changed = plan_uninstall(config)
            verb, skipped = "remove", []
        else:
            command = command_for(
                shim, args.ttl, tuple(spec.get("extra_args", ())),
            )
            if spec.get("schema") == "flat-v1":
                new_config, changed, skipped = plan_install_flat(
                    config, spec["events"], command,
                )
            else:
                new_config, changed, skipped = plan_install(
                    config, spec["events"], command,
                )
            verb = "add"
    except ValueError as exc:
        print(f"  !! {exc}")
        return False

    if skipped:
        print(f"  already installed for: {', '.join(skipped)}")
    if not changed:
        print("  nothing to do")
        return True
    print(f"  will {verb} deckbridge hooks for: {', '.join(changed)}")

    if not args.apply:
        print("  (dry run; pass --apply to write)")
        return True

    saved = backup(path)
    if saved:
        print(f"  backup: {saved}")
    write_json(path, new_config)
    ensure_executable(shim)
    print(f"  wrote {path}")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Install or remove deckbridge hooks for Claude Code, "
                     "Codex CLI, and Cursor."),
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write the config files (default is a dry run)",
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="remove deckbridge hooks instead of adding them",
    )
    parser.add_argument(
        "--only", choices=sorted(TOOLS), default=None,
        help="limit to one tool (default: all)",
    )
    parser.add_argument(
        "--ttl", type=float, default=None,
        help="pass --ttl SECONDS to the shim in the hook command",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tools = [args.only] if args.only else sorted(TOOLS)
    ok = all(process(tool, args) for tool in tools)

    if not args.apply:
        print("\nDry run only. Re-run with --apply to write.")
    elif not args.uninstall:
        print("\nNext steps:")
        print("  1. Codex only: run /hooks inside Codex and TRUST the new hooks.")
        print("  2. Start the stack:  ./deckbridge.sh start")
        print("  3. Reload Cursor so it reads ~/.cursor/hooks.json.")
        print("  4. Run an agent, then watch keys 0-9.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
