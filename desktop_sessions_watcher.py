#!/usr/bin/env python3
"""Publish open desktop-agent conversations for Deckbridge.

CLI hooks are precise, but they cannot see a conversation created directly in
Claude, Codex, or Cursor.  The signed Deckbridge Mic helper already has the
one Accessibility grant needed to read an Electron window's exposed URL.  This
watcher turns those live window routes into the same tiny state contract the
agent connector consumes. Claude's trusted Accessibility bridge reports its
live generating/finished state; other surfaces use conservative fallbacks.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable


DEFAULT_STATE = "~/.deckbridge/desktop_agents.json"
# The watcher is copied into the generated launchd runtime beside mic_key.sh;
# resolving from this file also keeps direct source-checkout runs correct.
DEFAULT_HELPER = str(Path(__file__).with_name("mic_key.sh"))
POLL_SECONDS = 2.0
HAMMERSPOON_CLI = "/opt/homebrew/bin/hs"
HAMMERSPOON_EXPR = "return deckbridgeClaudeSnapshot()"
DISCORD_BUNDLE = "com.hnc.Discord"
T3CODE_BUNDLE = "com.t3tools.t3code"
CURSOR_STATE_DB = "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"

# bundle id, Deckbridge source, focus app, accepted deep-link URL prefixes
SURFACES = (
    ("com.anthropic.claudefordesktop", "claude-desktop", "Claude",
     ("claude://claude.ai/chat/", "claude://claude.ai/epitaxy/local_")),
    ("com.openai.codex", "codex-desktop", "ChatGPT",
     ("codex://threads/", "codex://local/")),
    ("com.todesktop.230313mzl4w4u92", "cursor-desktop", "Cursor",
     ("cursor://anysphere.cursor-deeplink/background-agent?bcId=",)),
)


def route_id(url: str, prefixes: Iterable[str]) -> str:
    """Return the conversation identity from an app-owned deep-link route."""
    url = str(url or "").strip()
    # Electron AX omits the scheme on current Claude builds while the helper's
    # deep-link surface includes ``claude://``. They name the same chat route.
    comparable = url if "://" in url else f"claude://{url}"
    clean = comparable.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    for prefix in prefixes:
        if comparable.startswith(prefix):
            # Cursor's id is a query parameter, unlike the other two routes.
            if "bcId=" in comparable:
                return comparable.split("bcId=", 1)[1].split("&", 1)[0]
            return clean.rsplit("/", 1)[-1]
    return ""


def t3code_thread_id(url: str) -> str:
    """Extract the thread segment from T3's Electron hash route."""
    value = str(url or "").strip().rstrip("/")
    if "#/" not in value or not value.startswith("t3code://app/"):
        return ""
    parts = value.split("#/", 1)[1].split("/")
    return parts[1] if len(parts) >= 2 and parts[1] else ""


def compact_title(title: str, fallback: str) -> str:
    value = " ".join(str(title or "").split())
    # Generic product/window titles waste the precious key label.
    if not value or value.casefold() in {"claude", "codex", "chatgpt", "cursor"}:
        return fallback
    return value[:48]


def parse_helper_lines(
    text: str, source: str, app: str, prefixes: Iterable[str], now: float,
) -> list[dict[str, object]]:
    """Translate ``index<TAB>title<TAB>url`` helper output into records."""
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        window, title, url = parts
        session = route_id(url, prefixes)
        if not session:
            # A window without a conversation identity cannot be selected
            # exactly. App-only buttons are indistinguishable from a launcher
            # and routinely raise the wrong tab, so they are not agent sessions.
            continue
        if session in seen:
            continue
        seen.add(session)
        records.append({
            "name": compact_title(title, app),
            "display_title": compact_title(title, app),
            # When an app hides its route and controls, merely being open does
            # not prove that it is generating. ``done`` is the conservative
            # fallback: Deckbridge presents an unseen result as NEEDS YOU and
            # marks it seen when focused. Hammerspoon supplies exact live state
            # for Claude on the target Mac and replaces this fallback below.
            "status": "working",
            "source": source,
            "session_id": session,
            "app": app,
            "window": window,
            "url": url,
            "updated_at": now,
            "desktop_surface": True,
            "exact_route": True,
        })
    return records


def parse_hammerspoon_snapshot(text: str, now: float) -> list[dict[str, object]]:
    """Translate the trusted Hammerspoon AX snapshot into Claude records."""
    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        return []
    raw = document.get("sessions") if isinstance(document, dict) else None
    if not isinstance(raw, list):
        return []
    records: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        session = route_id(url, SURFACES[0][3])
        if not session:
            continue
        title = compact_title(str(item.get("title") or ""), "Claude")
        if title.endswith(" - Claude"):
            title = title[:-9].strip() or "Claude"
        records.append({
            "name": title,
            "display_title": title,
            "status": str(item.get("status") or "done"),
            "source": "claude-desktop",
            "session_id": session,
            "app": "Claude",
            "window": "",
            "url": url,
            "updated_at": now,
            "desktop_surface": True,
            "exact_route": True,
            "focused": bool(item.get("focused")),
        })
    return records


def scan_hammerspoon(now: float) -> list[dict[str, object]]:
    cli = os.environ.get("DECKBRIDGE_HS_CLI", HAMMERSPOON_CLI)
    if not os.path.isfile(cli):
        return []
    try:
        result = subprocess.run(
            [cli, "-c", HAMMERSPOON_EXPR], capture_output=True, text=True,
            timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_hammerspoon_snapshot(result.stdout, now) if result.returncode == 0 else []


def write_state(
    path: Path, agents: list[dict[str, object]],
    viewed: list[dict[str, object]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"agents": agents, "viewed": viewed or []}, handle,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_state(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def stabilize_updates(
    previous: dict[str, object], agents: list[dict[str, object]], now: float,
) -> list[dict[str, object]]:
    """Preserve the event token until a session's actual status changes."""
    old_agents = previous.get("agents")
    if not isinstance(old_agents, list):
        old_agents = []
    old_by_identity = {
        (item.get("source"), item.get("session_id")): item
        for item in old_agents if isinstance(item, dict)
    }
    for agent in agents:
        prior = old_by_identity.get((agent.get("source"), agent.get("session_id")))
        if prior and prior.get("status") == agent.get("status"):
            agent["updated_at"] = prior.get("updated_at", now)
        else:
            agent["updated_at"] = now
    return agents


def scan(helper: str, now: float | None = None) -> list[dict[str, object]]:
    current = time.time() if now is None else now
    agents: list[dict[str, object]] = []
    for bundle, source, app, prefixes in SURFACES:
        try:
            result = subprocess.run(
                [helper, "--helper-web-windows", bundle], capture_output=True,
                text=True, timeout=3, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            agents.extend(parse_helper_lines(result.stdout, source, app, prefixes, current))
    # Hammerspoon already has a durable Accessibility grant and sees Claude's
    # live status text ("responding" / "finished") plus its chat URL. Prefer
    # that exact snapshot to the helper's route-bearing Claude records without
    # rebuilding the signed helper and invalidating its macOS privacy grant.
    claude = scan_hammerspoon(current)
    if claude:
        agents = [a for a in agents if a.get("source") != "claude-desktop"]
        agents.extend(claude)
    return agents


def _run(
    argv: list[str], *, timeout: float = 2.0,
) -> str:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _active_cmux_surface(text: str) -> str:
    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        return ""
    active = document.get("active") if isinstance(document, dict) else None
    if not isinstance(active, dict):
        return ""
    return str(active.get("surface_id") or active.get("id") or "")


def _current_herdr_pane(text: str) -> str:
    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        return ""
    result = document.get("result") if isinstance(document, dict) else None
    pane = result.get("pane") if isinstance(result, dict) else None
    return str(pane.get("pane_id") or "") if isinstance(pane, dict) else ""


def scan_viewed(
    helper: str, agents: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return exact surfaces the user currently has in front of them."""
    front = _run([helper, "--helper-frontmost"])
    parts = front.split("|", 2)
    if len(parts) < 2:
        return []
    app, bundle = parts[0], parts[1]
    viewed: list[dict[str, object]] = []

    surface = next((item for item in SURFACES if item[0] == bundle), None)
    if bundle == DISCORD_BUNDLE:
        url = _run([helper, "--helper-web-url", bundle], timeout=3.0)
        if url:
            viewed.append({"source": "hermes-discord", "url": url})
    elif bundle == T3CODE_BUNDLE:
        url = _run([helper, "--helper-web-url", bundle], timeout=3.0)
        session = t3code_thread_id(url)
        if session:
            viewed.append({"session_id": session})
    elif surface is not None:
        url = _run([helper, "--helper-web-url", bundle], timeout=3.0)
        session = route_id(url, surface[3])
        if session:
            viewed.append({"session_id": session})
        else:
            # Safe only when the connector sees one non-generic live session
            # hosted by this app. It performs that uniqueness check globally.
            viewed.append({"app": surface[2], "unique_app": "1"})

    if bundle == SURFACES[0][0]:
        for agent in agents:
            if agent.get("source") == "claude-desktop" and agent.get("focused"):
                viewed.append({
                    "source": "claude-desktop",
                    "session_id": str(agent.get("session_id") or ""),
                })

    if bundle == SURFACES[2][0] and not any(v.get("session_id") for v in viewed):
        db = Path(os.environ.get("CURSOR_STATE_DB", CURSOR_STATE_DB)).expanduser()
        selected = _run([
            "sqlite3", "-noheader", str(db),
            "SELECT CAST(value AS TEXT) FROM ItemTable "
            "WHERE key='cursor/glass.selectedAgent';",
        ])
        if selected:
            viewed.append({"session_id": selected})

    if app.casefold() == "cmux":
        tree = _run(["cmux", "--id-format", "both", "tree", "--all", "--json"])
        active = _active_cmux_surface(tree)
        if active:
            viewed.append({"surface": active})

    if app.casefold() in {"terminal", "terminal.app"}:
        pane = _current_herdr_pane(_run(["herdr", "pane", "current"]));
        if pane:
            viewed.append({"herdr_pane": pane})
        tty = _run([helper, "--helper-focused-tty"])
        if tty:
            viewed.append({"tty": tty.removeprefix("/dev/")})
    return [view for view in viewed if any(str(value) for value in view.values())]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--helper", default=DEFAULT_HELPER)
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    state = Path(args.state).expanduser()
    interval = max(0.2, args.poll_seconds)
    while True:
        now = time.time()
        agents = scan(os.path.expanduser(args.helper), now)
        stable = stabilize_updates(read_state(state), agents, now)
        write_state(state, stable, scan_viewed(os.path.expanduser(args.helper), stable))
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
