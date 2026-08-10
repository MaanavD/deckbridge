#!/usr/bin/env python3
"""Poll the Hermes Discord session probe and publish connector state.

This watcher is intentionally stdlib-only and does not contain a Discord token.
Use ``--local`` when the state DB is on this machine, or ``--ssh USER@HOST`` to
execute the read-only probe on a remote Hermes host.  The published contract is
the probe's ``{"agents": [...]}`` JSON document, written with ``os.replace`` so
``connector_hermes.py`` never sees a half-written file.

A failed poll is non-destructive: the last good output remains in place and the
watcher logs a warning before trying again.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from connection_runtime import (
    HealthReporter,
    NONINTERACTIVE_SSH_OPTIONS,
    RetryPolicy,
)

LOG = logging.getLogger("hermes_agents_watcher")
DEFAULT_DB = "/home/hermes/.hermes/state.db"
DEFAULT_OUT = Path("~/.deckbridge/hermes_agents.json").expanduser()
DEFAULT_REMOTE_PROBE = "/home/hermes/deckbridge/hermes_agents_probe.py"
DEFAULT_INTERVAL = 5.0
DEFAULT_TIMEOUT = 10.0
DEFAULT_GUILD_ID = ""
DEFAULT_MAX_AGE_HOURS = 24.0
# The unified board has ten agent slots, so the probe must be allowed to fill
# all of them rather than the five the old split-zone layout could show.
DEFAULT_LIMIT = 10
# None means "do not pass --source at all", which lets the probe apply its own
# default set (discord + cli + tui). Hardcoding a single source here used to
# silently hide every ssh-hosted Hermes agent from the deck.
DEFAULT_SOURCES: list[str] | None = None


def _probe_args(
    *,
    db: str,
    limit: int,
    guild_id: str,
    sources: Sequence[str] | None,
    max_age_hours: float,
    active_only: bool = True,
) -> list[str]:
    args = [
        "--db", str(db),
        "--limit", str(limit),
        "--max-age-hours", str(max_age_hours),
    ]
    if guild_id:
        args += ["--guild-id", str(guild_id)]
    for source in sources or ():
        args += ["--source", str(source)]
    if not active_only:
        args.append("--all")
    return args


def build_command(args: argparse.Namespace) -> list[str]:
    """Build a shell-free local or SSH argv for one probe invocation."""
    probe_args = _probe_args(
        db=args.db,
        limit=args.limit,
        guild_id=args.guild_id,
        sources=args.source,
        max_age_hours=args.max_age_hours,
        active_only=args.active_only,
    )
    if args.ssh:
        # A launchd watcher can never answer a password, host-key, or Tailscale
        # check prompt.  Make every transport failure bounded so the retry
        # policy can report it and move on instead of accumulating hung ssh
        # children behind an apparently healthy watcher process.
        ssh_options = list(dict.fromkeys([*NONINTERACTIVE_SSH_OPTIONS, *(args.ssh_opt or [])]))
        command = ["ssh", *ssh_options, args.ssh, "python3", args.remote_probe]
        return command + probe_args
    return [
        sys.executable,
        str(Path(__file__).with_name("hermes_agents_probe.py").resolve()),
        *probe_args,
    ]


def annotate_ssh_host(document: dict[str, Any], ssh_host: str | None) -> dict[str, Any]:
    """Attach the Mac-side SSH alias needed to find a visible local viewer.

    The remote database can identify a Hermes session, but it cannot know
    which local Herdr pane is displaying the SSH connection.  Preserve the
    exact alias used by this watcher so the local connector can make that
    mapping without guessing a host from titles or cwd values.
    """
    host = str(ssh_host or "").strip()
    if not host:
        return document
    for item in document.get("agents", []):
        if isinstance(item, dict) and item.get("source") == "hermes-ssh":
            item["ssh_host"] = host
    return document


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    """Execute one probe and reject non-contract output or process failures."""
    command = build_command(args)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(0.1, float(args.timeout)),
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stderr or exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        detail = " ".join(str(output).split())
        message = f"probe timed out after {float(args.timeout):g}s"
        if detail:
            message += f": {detail}"
        raise RuntimeError(message) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
        raise RuntimeError(detail)
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"probe returned invalid JSON: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("agents"), list):
        raise ValueError("probe JSON must be an object with an agents list")
    return annotate_ssh_host(document, args.ssh)


def write_atomic(path: str | Path, document: dict[str, Any]) -> None:
    """Atomically write one validated probe document to the connector path."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=str(target.parent), text=True
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def poll_once(
    args: argparse.Namespace, *, reporter: HealthReporter | None = None
) -> dict[str, Any] | None:
    """Publish one successful poll; return None while preserving old state on failure."""
    try:
        document = run_probe(args)
        write_atomic(args.out, document)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        if reporter is not None:
            reporter.degraded(exc, transport="ssh" if args.ssh else "local")
        LOG.warning("Hermes agent probe failed; keeping last good state: %s", exc)
        return None
    if reporter is not None:
        reporter.ready(
            transport="ssh" if args.ssh else "local",
            agent_count=len(document["agents"]),
        )
    return document


def _log_poll_result(
    count: int, previous_count: int | None, state_path: str | Path
) -> int:
    """Log startup and count transitions; keep steady refreshes at DEBUG.

    Successful probes still atomically refresh the file every cycle, preserving
    its mtime as a liveness signal without adding an INFO line every five
    seconds for an unchanged board.
    """
    if previous_count is None:
        LOG.info(
            "Hermes agent watcher ready; wrote %d agent(s) to %s",
            count,
            state_path,
        )
    elif count != previous_count:
        LOG.info(
            "Hermes agent count changed %d -> %d; wrote state to %s",
            previous_count,
            count,
            state_path,
        )
    else:
        LOG.debug(
            "Hermes agent count unchanged at %d; refreshed %s",
            count,
            state_path,
        )
    return count


def run_watcher(
    args: argparse.Namespace,
    *,
    reporter: HealthReporter | None = None,
) -> None:
    """Poll until interrupted, never terminating for an individual probe failure."""
    previous_count: int | None = None
    reporter = reporter or HealthReporter(
        "hermes_agents",
        path=args.health_file,
        stale_after=max(30.0, float(args.timeout) + 3 * float(args.interval)),
    )
    retry = RetryPolicy(
        initial=max(0.05, float(args.interval)), maximum=60.0,
    )
    failures = 0
    while True:
        document = poll_once(args, reporter=reporter)
        if document is not None:
            failures = 0
            previous_count = _log_poll_result(
                len(document["agents"]), previous_count, args.out
            )
            delay = max(0.05, float(args.interval))
        else:
            failures += 1
            delay = retry.delay(failures)
            LOG.debug("Hermes agent retry %d in %.1fs", failures, delay)
        if args.once:
            return
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local", action="store_true", help="run the probe on this machine")
    mode.add_argument("--ssh", metavar="USER@HOST", help="run the probe through ssh")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path (local or remote)")
    parser.add_argument("--remote-probe", default=DEFAULT_REMOTE_PROBE)
    parser.add_argument(
        "--ssh-opt", action="append", default=[],
        help="extra ssh option; may be repeated (for example --ssh-opt=-oBatchMode=yes)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--health-file", type=Path, default=None,
        help="connection health output (default: $DECKBRIDGE_HEALTH_DIR/hermes_agents.json)",
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--guild-id", default=DEFAULT_GUILD_ID)
    parser.add_argument(
        "--source", action="append", default=DEFAULT_SOURCES, metavar="SOURCE",
        help="session source to include; repeatable. Omit to use the probe's "
             "own default set (discord + cli + tui).",
    )
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument(
        "--all", dest="active_only", action="store_false", default=True,
        help="include idle/stale sessions too (default: active sessions only)",
    )
    parser.add_argument("--once", action="store_true", help="poll once and exit (useful for checks)")
    args = parser.parse_args(argv)
    args.out = Path(args.out).expanduser()
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        run_watcher(args)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
