#!/usr/bin/env python3
"""Optional Discord REST watcher for ``connector_hermes.py``.

This module is intentionally separate from the connector and has no import-time
network or file side effects.  Run it only when a real Discord bot token is
available::

    DISCORD_BOT_TOKEN="$DISCORD_BOT_TOKEN" python3 hermes_discord_watcher.py --channel-id 123

It polls ``GET /channels/{channel_id}/messages`` and atomically writes the
connector state contract to ``~/.deckbridge/hermes_approvals.json`` by default.
The optional ``--guild-id`` (or ``DISCORD_GUILD_ID``) makes generated jump URLs
point at a guild; without it, Discord's ``@me`` form is used as a best-effort
fallback because REST message objects do not reliably include guild_id.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from connection_runtime import (
    HealthReporter,
    NONINTERACTIVE_SSH_OPTIONS,
    RetryPolicy,
    retry_delay_for_error,
)

LOG = logging.getLogger("hermes_discord_watcher")
DISCORD_API = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://example.com, 1.0)"
DEFAULT_STATE_PATH = Path("~/.deckbridge/hermes_approvals.json").expanduser()
APPROVAL_TITLE = "Command Approval Required"
EXPIRED_MARKER = "Approval expired"
REMOTE_ENV_KEYS = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_HOME_CHANNEL",
    "DISCORD_GUILD_ID",
)


def _unquote_env(value: str) -> str:
    value = value.strip().rstrip("\r")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def fetch_remote_discord_config(
    host: str, *, timeout: float = 10.0
) -> dict[str, str]:
    """Read only allowlisted Discord settings over bounded, promptless SSH."""
    command = [
        "ssh", *NONINTERACTIVE_SSH_OPTIONS, str(host),
        "cat", "~/.hermes/.env",
    ]
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True,
            timeout=max(0.1, float(timeout)),
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stderr or exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        detail = " ".join(str(output).split())
        message = f"remote Discord configuration timed out after {float(timeout):g}s"
        if detail:
            message += f": {detail}"
        raise RuntimeError(message) from exc
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or "").split())
        raise RuntimeError(detail or f"ssh exited {completed.returncode}")

    accepted: dict[str, str] = {}
    allowed = set(REMOTE_ENV_KEYS)
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        key = key.strip()
        if separator and key in allowed:
            accepted[key] = _unquote_env(value)
    return {key: value for key, value in accepted.items() if value}


def build_messages_request(token: str, channel_id: str, *, limit: int = 50) -> Request:
    """Create the Discord request with the User-Agent required by the API edge."""
    safe_limit = max(1, min(int(limit), 100))
    channel = quote(str(channel_id), safe="")
    url = f"{DISCORD_API}/channels/{channel}/messages?limit={safe_limit}"
    auth = token if token.lower().startswith("bot ") else f"Bot {token}"
    return Request(
        url,
        headers={
            "Authorization": auth,
            "User-Agent": DISCORD_USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )


def build_channel_request(token: str, channel_id: str) -> Request:
    """Create the read-only request used to resolve a channel's guild.

    Discord message objects do not reliably carry ``guild_id``.  Without an
    explicit guild configuration, using ``@me`` for a server channel creates a
    deep link that opens Discord but cannot land on the approval.  The channel
    endpoint provides the missing guild id and is fetched once by the watcher.
    """
    channel = quote(str(channel_id), safe="")
    auth = token if token.lower().startswith("bot ") else f"Bot {token}"
    return Request(
        f"{DISCORD_API}/channels/{channel}",
        headers={
            "Authorization": auth,
            "User-Agent": DISCORD_USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )


def fetch_channel_guild_id(
    token: str, channel_id: str, *, timeout: float = 20.0
) -> str:
    """Return the channel guild id, or ``@me`` for a direct-message channel."""
    request = build_channel_request(token, channel_id)
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Discord channel response was not an object")
    guild_id = str(payload.get("guild_id") or "").strip()
    return guild_id or "@me"


def fetch_messages(token: str, channel_id: str, *, limit: int = 50, timeout: float = 20.0) -> list[dict[str, Any]]:
    request = build_messages_request(token, channel_id, limit=limit)
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Discord messages response was not a list")
    return [item for item in payload if isinstance(item, dict)]


def _embed_text(embed: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "description"):
        value = embed.get(key)
        if value:
            parts.append(str(value))
    for field in embed.get("fields", []) or []:
        if isinstance(field, dict):
            for key in ("name", "value"):
                value = field.get(key)
                if value:
                    parts.append(str(value))
    return "\n".join(parts)


def _field_value(embed: dict[str, Any], wanted: str) -> str:
    wanted = wanted.rstrip(":").strip().lower()
    for field in embed.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name", "")).rstrip(":").strip().lower()
        if name == wanted:
            return str(field.get("value", "")).strip()
    return ""


def _without_code_fence(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"```[^\n]*\n?(.*?)```", value, flags=re.DOTALL)
    return (match.group(1) if match else value).strip()


def _description_value(description: str, label: str, next_label: str | None = None) -> str:
    boundary = rf"\n\s*{re.escape(next_label)}\s*:" if next_label else r"\Z"
    match = re.search(
        rf"{re.escape(label)}\s*:\s*(.*?)(?={boundary})",
        description,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _without_code_fence(match.group(1)) if match else ""


def _created_ts(message: dict[str, Any]) -> float:
    timestamp = message.get("timestamp")
    if timestamp:
        try:
            normalized = str(timestamp).replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            pass
    return time.time()


def approval_from_message(
    message: dict[str, Any], *, channel_id: str, guild_id: str | None = None
) -> dict[str, Any] | None:
    """Convert one Discord message to the local approval record, if active."""
    channel_id = str(channel_id)
    configured_guild = str(guild_id or os.environ.get("DISCORD_GUILD_ID", ""))
    message_id = str(message.get("id", ""))
    if not message_id:
        return None
    for embed in message.get("embeds", []) or []:
        if not isinstance(embed, dict) or embed.get("title") != APPROVAL_TITLE:
            continue
        body = _embed_text(embed)
        if EXPIRED_MARKER in body:
            continue

        description = str(embed.get("description", ""))
        command = _field_value(embed, "Requested command")
        if not command:
            command = _description_value(description, "Requested command", "Reason")
        reason = _field_value(embed, "Reason")
        if not reason:
            reason = _description_value(description, "Reason")

        guild = str(message.get("guild_id") or configured_guild)
        url_guild = guild or "@me"
        return {
            "message_id": message_id,
            "channel_id": channel_id,
            "guild_id": guild,
            "command": command,
            "reason": reason,
            "created_ts": _created_ts(message),
            "url": f"https://discord.com/channels/{url_guild}/{channel_id}/{message_id}",
        }
    return None


def collect_pending(
    messages: Iterable[dict[str, Any]], *, channel_id: str, guild_id: str | None = None
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        record = approval_from_message(message, channel_id=channel_id, guild_id=guild_id)
        if record is None or record["message_id"] in seen:
            continue
        seen.add(record["message_id"])
        records.append(record)
    records.sort(key=lambda item: float(item.get("created_ts", 0)))
    return records


def write_state(path: str | Path, pending: list[dict[str, Any]]) -> None:
    """Atomically publish the state so the connector cannot read a partial JSON."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pending": pending}, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def poll_once(
    token: str,
    channel_id: str,
    *,
    state_path: str | Path = DEFAULT_STATE_PATH,
    guild_id: str | None = None,
    limit: int = 50,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    resolved_guild_id = str(guild_id or "").strip()
    if not resolved_guild_id:
        resolved_guild_id = fetch_channel_guild_id(
            token, channel_id, timeout=timeout
        )
    messages = fetch_messages(token, channel_id, limit=limit, timeout=timeout)
    pending = collect_pending(
        messages, channel_id=channel_id, guild_id=resolved_guild_id
    )
    write_state(state_path, pending)
    return pending


def _log_poll_result(
    count: int, previous_count: int | None, state_path: str | Path
) -> int:
    """Log startup and count transitions without flooding always-on logs.

    The state file is intentionally refreshed on every successful poll so its
    mtime remains a useful liveness signal.  That refresh is routine, though,
    and logging it at INFO every two seconds obscures the warnings that matter.
    """
    if previous_count is None:
        LOG.info(
            "Discord approval watcher ready; wrote %d pending approval(s) to %s",
            count,
            state_path,
        )
    elif count != previous_count:
        LOG.info(
            "Discord pending approval count changed %d -> %d; wrote state to %s",
            previous_count,
            count,
            state_path,
        )
    else:
        LOG.debug(
            "Discord pending approvals unchanged at %d; refreshed %s",
            count,
            state_path,
        )
    return count


def run_watcher(
    token: str | None,
    channel_id: str | None,
    *,
    state_path: str | Path = DEFAULT_STATE_PATH,
    guild_id: str | None = None,
    interval: float = 2.0,
    limit: int = 50,
    timeout: float = 20.0,
    once: bool = False,
    reporter: HealthReporter | None = None,
    credential_loader: Callable[[], dict[str, str]] | None = None,
) -> None:
    # Credentials may come from the local environment or a retryable remote
    # adapter.  The latter keeps the token in memory only while allowing an
    # offline boot to recover without restarting Deckbridge.
    token_value = str(token or "").strip()
    channel_value = str(channel_id or "").strip()
    configured_guild_id = str(guild_id or "").strip()
    resolved_guild_id = configured_guild_id
    static_token = bool(token_value)
    previous_count: int | None = None
    reporter = reporter or HealthReporter(
        "discord_watcher",
        stale_after=max(30.0, float(timeout) + 3 * float(interval)),
    )
    retry = RetryPolicy(initial=max(0.1, float(interval)), maximum=60.0)
    failures = 0
    while True:
        if (not token_value or not channel_value) and credential_loader is not None:
            try:
                remote = credential_loader()
                token_value = token_value or remote.get("DISCORD_BOT_TOKEN", "")
                channel_value = channel_value or remote.get("DISCORD_HOME_CHANNEL", "")
                configured_guild_id = (
                    configured_guild_id or remote.get("DISCORD_GUILD_ID", "")
                )
                resolved_guild_id = configured_guild_id
                if not token_value or not channel_value:
                    raise ValueError("remote Discord configuration lacks token or channel")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                failures += 1
                delay = retry_delay_for_error(retry, failures, exc)
                reporter.degraded(
                    exc, transport="ssh-config", retry_in_seconds=round(delay, 3)
                )
                LOG.warning("Discord configuration unavailable: %s", exc)
                if once:
                    return
                time.sleep(delay)
                continue
        try:
            if not resolved_guild_id:
                resolved_guild_id = fetch_channel_guild_id(
                    token_value, channel_value, timeout=timeout
                )
            pending = poll_once(
                token_value,
                channel_value,
                state_path=state_path,
                guild_id=resolved_guild_id,
                limit=limit,
                timeout=timeout,
            )
            previous_count = _log_poll_result(
                len(pending), previous_count, state_path
            )
            failures = 0
            reporter.ready(
                transport="discord-rest",
                pending_count=len(pending),
                channel_id=channel_value,
            )
            delay = max(0.1, float(interval))
        except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            failures += 1
            delay = retry.delay(failures)
            reporter.degraded(
                exc, transport="discord-rest", retry_in_seconds=round(delay, 3)
            )
            # A remotely sourced token can rotate. Invalidate only the
            # in-memory copy; the next loop reloads it without writing it here.
            if (
                credential_loader is not None
                and not static_token
                and isinstance(exc, HTTPError)
                and exc.code in (401, 403)
            ):
                token_value = ""
            LOG.warning("Discord approval poll failed: %s", exc)
        if once:
            return
        time.sleep(delay)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel-id", default=os.environ.get("DISCORD_HOME_CHANNEL")
    )
    parser.add_argument("--guild-id", default=os.environ.get("DISCORD_GUILD_ID"))
    parser.add_argument(
        "--ssh-env", metavar="HOST",
        help="retry loading missing Discord settings from HOST's ~/.hermes/.env",
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--health-file", type=Path, default=None,
        help="connection health output (default: $DECKBRIDGE_HEALTH_DIR/discord_watcher.json)",
    )
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if (not token or not args.channel_id) and not args.ssh_env:
        raise SystemExit(
            "DISCORD_BOT_TOKEN and --channel-id are required unless --ssh-env is set"
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    reporter = HealthReporter(
        "discord_watcher",
        path=args.health_file,
        stale_after=max(30.0, float(args.timeout) + 3 * float(args.interval)),
    )
    loader = None
    if args.ssh_env:
        loader = lambda: fetch_remote_discord_config(  # noqa: E731
            args.ssh_env, timeout=args.timeout
        )
    run_watcher(
        token,
        args.channel_id,
        state_path=args.state_file,
        guild_id=args.guild_id,
        interval=args.interval,
        limit=args.limit,
        timeout=args.timeout,
        once=args.once,
        reporter=reporter,
        credential_loader=loader,
    )


if __name__ == "__main__":
    main()
