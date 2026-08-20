#!/usr/bin/env python3
"""Publish authoritative T3 Code thread state for Deckbridge.

The official desktop app exposes a loopback, bearer-authenticated shell API.
This watcher deliberately rereads its runtime descriptor on every poll: T3
chooses a fresh port after restarts, so caching the first URL makes a boot-time
integration look healthy until the first app update or crash.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from connection_runtime import NONINTERACTIVE_SSH_OPTIONS, HealthReporter

log = logging.getLogger("t3code_watcher")

DEFAULT_RUNTIME = "~/.t3/userdata/server-runtime.json"
DEFAULT_TOKEN = "~/.deckbridge/t3code_token"
DEFAULT_STATE = "~/.deckbridge/t3code_agents.json"
DEFAULT_INTERVAL = 0.75
T3_APP = Path("/Applications/T3 Code (Alpha).app")
REMOTE_DUMP = r"""
import json, pathlib, socket, urllib.request
home = pathlib.Path.home()
runtime = json.loads((home / ".t3/userdata/server-runtime.json").read_text())
token_path = home / ".deckbridge/t3code_token"
if not token_path.is_file():
    raise SystemExit("missing-token")
token = token_path.read_text(encoding="utf-8").strip()
if not token:
    raise SystemExit("empty-token")
origin = str(runtime.get("origin") or "").rstrip("/")
environment = str(runtime.get("environmentId") or runtime.get("environment_id") or "")
env_path = home / ".t3/userdata/environment-id"
if not environment and env_path.is_file():
    environment = env_path.read_text(encoding="utf-8").strip()
request = urllib.request.Request(
    origin + "/api/orchestration/shell",
    headers={"Authorization": "Bearer " + token},
)
with urllib.request.urlopen(request, timeout=8) as response:
    payload = json.load(response)
print(json.dumps(
    {"payload": payload, "origin": origin, "environment_id": environment,
     "hostname": socket.gethostname()},
    separators=(",", ":"),
))
"""
REMOTE_ISSUE_TOKEN = r"""
set -eu
mkdir -p "$HOME/.deckbridge"
token_path="$HOME/.deckbridge/t3code_token"
if [ -s "$token_path" ]; then
  exit 0
fi
node="${HOME}/.hermes/node/bin/node"
[ -x "$node" ] || node=$(command -v node)
cli=$(ls -1d "$HOME"/.t3/runtime/versions/*/node_modules/t3/dist/bin.mjs 2>/dev/null | tail -n 1)
[ -n "$node" ] && [ -n "$cli" ] || exit 1
token=$("$node" "$cli" auth session issue --ttl 3650d --label Deckbridge --subject deckbridge-remote --token-only)
[ -n "$token" ] || exit 1
umask 077
printf '%s\n' "$token" > "$token_path.tmp"
chmod 600 "$token_path.tmp"
mv "$token_path.tmp" "$token_path"
"""

PROVIDER_SOURCE = {
    "claudeagent": "t3code-claude",
    "codex": "t3code-codex",
    "cursor": "t3code-cursor",
    "grok": "t3code-grok",
    "opencode": "t3code-opencode",
}


def iso_epoch(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def provider_source(thread: dict[str, Any]) -> str:
    selection = thread.get("modelSelection") or {}
    session = thread.get("session") or {}
    raw = (selection.get("instanceId") or session.get("providerInstanceId")
           or session.get("providerName") or "")
    key = str(raw).replace("-", "").replace("_", "").lower()
    for prefix, source in PROVIDER_SOURCE.items():
        if key.startswith(prefix):
            return source
    return "t3code"


def thread_is_settled(thread: dict[str, Any]) -> bool:
    """T3's Settled section is a deliberate put-away, not merely done."""
    override = str(thread.get("settledOverride") or "").strip().lower()
    if override:
        return override == "settled"
    return bool(thread.get("settledAt"))


def thread_status(thread: dict[str, Any]) -> str:
    """Map T3's explicit interaction/lifecycle fields to Deckbridge state."""
    if (thread.get("hasPendingApprovals") or thread.get("hasPendingUserInput")
            or thread.get("hasActionableProposedPlan")):
        return "blocked"
    session = thread.get("session") or {}
    latest = thread.get("latestTurn") or {}
    liveness = thread.get("backgroundLiveness")
    if session.get("status") in ("starting", "running"):
        return "working"
    if latest.get("state") == "running" or liveness in ("working", "monitoring"):
        return "working"
    if session.get("status") == "error" or latest.get("state") == "error":
        return "blocked"
    if latest.get("state") in ("completed", "interrupted"):
        return "done"
    return "idle"


def snapshot_agents(payload: dict[str, Any], origin: str, environment_id: str) -> list[dict[str, Any]]:
    projects = {
        str(project.get("id")): project
        for project in payload.get("projects", []) if isinstance(project, dict)
    }
    agents: list[dict[str, Any]] = []
    for thread in payload.get("threads", []):
        if not isinstance(thread, dict) or thread.get("archivedAt"):
            continue
        if thread_is_settled(thread):
            continue
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            continue
        project = projects.get(str(thread.get("projectId")), {})
        latest = thread.get("latestTurn") or {}
        session = thread.get("session") or {}
        updated = (latest.get("completedAt") or latest.get("startedAt")
                   or session.get("updatedAt") or thread.get("updatedAt"))
        route = f"/{environment_id}/{thread_id}"
        agents.append({
            "name": str(thread.get("title") or "New thread"),
            "status": thread_status(thread),
            "source": provider_source(thread),
            "session_id": thread_id,
            "thread_id": thread_id,
            "environment_id": environment_id,
            "cwd": str(project.get("workspaceRoot") or thread.get("worktreePath") or ""),
            "app": "T3 Code (Alpha)",
            "url": f"t3code://app/#{route}",
            "web_url": origin.rstrip("/") + route,
            "updated_at": iso_epoch(updated),
            "activity": str(latest.get("state") or session.get("status") or ""),
        })
    return agents


def annotate_remote_agents(
    agents: list[dict[str, Any]], ssh_host: str,
    environment_label: str = "",
) -> list[dict[str, Any]]:
    """Mark Hermes-hosted T3 threads so focus can keep using the local app."""
    host = str(ssh_host or "").strip()
    label = str(environment_label or "").strip()
    annotated: list[dict[str, Any]] = []
    for item in agents:
        agent = dict(item)
        if host:
            agent["ssh_host"] = host
            # Remote loopback URLs are not reachable from the Mac. The local
            # desktop app is paired with that environment and uses the hash
            # route, so the t3code:// URL is the focus identity.
            agent["web_url"] = ""
        if label:
            agent["environment_label"] = label
        annotated.append(agent)
    return annotated


def merge_agent_sides(
    previous_local: list[dict[str, Any]],
    previous_remote: list[dict[str, Any]],
    local: list[dict[str, Any]] | None,
    remote: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """Keep the last good side when only one T3 server answers this poll."""
    if local is None and remote is None:
        return None
    return (
        (local if local is not None else list(previous_local))
        + (remote if remote is not None else list(previous_remote))
    )


def split_agent_sides(agents: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    local = [item for item in agents if not item.get("ssh_host")]
    remote = [item for item in agents if item.get("ssh_host")]
    return local, remote


def read_published_agents(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    raw = document.get("agents") if isinstance(document, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(token.strip() + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class T3CodeWatcher:
    def __init__(self, runtime: Path, token: Path, state: Path,
                 interval: float = DEFAULT_INTERVAL,
                 health: HealthReporter | None = None,
                 ssh_host: str | None = None,
                 opener: Callable[..., Any] = subprocess.run) -> None:
        self.runtime = runtime.expanduser()
        self.token = token.expanduser()
        self.state = state.expanduser()
        self.interval = interval
        self.health = health
        self.ssh_host = str(ssh_host or "").strip()
        self.opener = opener
        self._last_local, self._last_remote = split_agent_sides(
            read_published_agents(self.state)
        )
        self._reissued_local = False
        self._issued_remote = False

    def endpoint(self) -> tuple[str, str]:
        doc = json.loads(self.runtime.read_text(encoding="utf-8"))
        origin = str(doc.get("origin") or "")
        parsed = urllib.parse.urlparse(origin)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("T3 runtime origin is not a local HTTP endpoint")
        environment = str(doc.get("environmentId") or doc.get("environment_id") or "")
        return origin.rstrip("/") + "/api/orchestration/shell", environment

    def credential(self) -> str:
        mode = stat.S_IMODE(self.token.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(f"T3 token must not be group/world-readable (mode {mode:o})")
        token = self.token.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("T3 token is empty")
        return token

    def fetch_shell(self, endpoint: str, token: str) -> dict[str, Any]:
        request = urllib.request.Request(
            endpoint, headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("T3 shell payload is not an object")
        return payload

    def resolve_environment(self, origin: str, hinted: str) -> str:
        if hinted:
            return hinted
        with urllib.request.urlopen(
                origin + "/.well-known/t3/environment", timeout=5) as response:
            descriptor = json.load(response)
        environment = str(descriptor.get("environmentId") or "")
        if not environment:
            raise ValueError("T3 environment descriptor has no environmentId")
        return environment

    def poll_local(self) -> list[dict[str, Any]]:
        endpoint, environment = self.endpoint()
        origin = endpoint.removesuffix("/api/orchestration/shell")
        environment = self.resolve_environment(origin, environment)
        try:
            payload = self.fetch_shell(endpoint, self.credential())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self.reissue_local_token():
                payload = self.fetch_shell(endpoint, self.credential())
            else:
                raise
        return snapshot_agents(payload, origin, environment)

    def reissue_local_token(self) -> bool:
        """Mint a new loopback bearer without activating the T3 GUI."""
        if self._reissued_local:
            return False
        self._reissued_local = True
        binary = T3_APP / "Contents/MacOS/T3 Code (Alpha)"
        asar = T3_APP / "Contents/Resources/app.asar"
        cli = asar / "apps/server/dist/bin.mjs"
        # The CLI lives inside the asar archive. Electron can load that
        # virtual path; Path.is_file() cannot see it.
        if not binary.is_file() or not asar.is_file():
            return False
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        try:
            completed = self.opener(
                [str(binary), str(cli), "auth", "session", "issue",
                 "--ttl", "3650d", "--label", "Deckbridge",
                 "--subject", "deckbridge-local", "--token-only"],
                check=False, capture_output=True, text=True, timeout=20, env=env,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        token = (completed.stdout or "").strip().splitlines()
        token = token[-1] if token else ""
        if getattr(completed, "returncode", 1) != 0 or not token:
            return False
        write_token(self.token, token)
        return True

    def ssh_command(self, *remote: str) -> list[str]:
        return ["ssh", *NONINTERACTIVE_SSH_OPTIONS, self.ssh_host, *remote]

    def poll_remote(self) -> list[dict[str, Any]]:
        if not self.ssh_host:
            return []
        document = self._remote_dump()
        if document is None:
            self.ensure_remote_token()
            document = self._remote_dump()
        if document is None:
            raise RuntimeError(f"T3 on {self.ssh_host} did not return a shell snapshot")
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("remote T3 dump is missing a shell payload")
        origin = str(document.get("origin") or "http://127.0.0.1:3773")
        environment = str(document.get("environment_id") or "")
        if not environment:
            raise ValueError("remote T3 dump has no environmentId")
        return annotate_remote_agents(
            snapshot_agents(payload, origin, environment), self.ssh_host,
            str(document.get("hostname") or ""),
        )

    def _remote_dump(self) -> dict[str, Any] | None:
        try:
            completed = self.opener(
                self.ssh_command("python3", "-"),
                check=False, capture_output=True, text=True, timeout=15,
                input=REMOTE_DUMP,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"ssh {self.ssh_host} failed: {exc}") from exc
        stderr = (completed.stderr or "").strip()
        stdout = completed.stdout or ""
        if completed.returncode != 0:
            detail = stderr or stdout.strip() or f"exit {completed.returncode}"
            if "missing-token" in detail or "empty-token" in detail:
                return None
            raise RuntimeError(detail)
        try:
            document = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"remote T3 dump was not JSON: {exc}") from exc
        return document if isinstance(document, dict) else None

    def ensure_remote_token(self) -> None:
        if self._issued_remote or not self.ssh_host:
            return
        self._issued_remote = True
        try:
            completed = self.opener(
                self.ssh_command("bash", "-s"),
                check=False, capture_output=True, text=True, timeout=30,
                input=REMOTE_ISSUE_TOKEN,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"could not issue a T3 token on {self.ssh_host}: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip() or "token issue failed"
            raise RuntimeError(f"could not issue a T3 token on {self.ssh_host}: {detail}")

    def poll_once(self) -> list[dict[str, Any]]:
        errors: list[Exception] = []
        local: list[dict[str, Any]] | None = None
        remote: list[dict[str, Any]] | None = None
        try:
            local = self.poll_local()
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            errors.append(exc)
        if self.ssh_host:
            try:
                remote = self.poll_remote()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                errors.append(exc)
        agents = merge_agent_sides(self._last_local, self._last_remote, local, remote)
        if agents is None:
            raise errors[0] if errors else RuntimeError("T3 poll failed")
        if local is not None:
            self._last_local = local
        if remote is not None:
            self._last_remote = remote
        atomic_write(self.state, {"agents": agents, "updated_at": time.time()})
        if self.health:
            self.health.ready(detail=f"{len(agents)} T3 thread(s)")
        if errors:
            log.warning("T3 Code partial poll: %s", errors[0])
        return agents

    def run(self) -> None:
        while True:
            try:
                self.poll_once()
            except (OSError, ValueError, RuntimeError, urllib.error.URLError,
                    json.JSONDecodeError, subprocess.SubprocessError) as exc:
                if self.health:
                    self.health.degraded(str(exc))
                # Never launch or activate T3 from the poll loop. `open -a`
                # steals focus even with -g/-j when the Electron app is already
                # running, which is how a 401 or missing runtime file kept
                # yanking the user back into T3 every few seconds.
                log.warning("T3 Code unavailable: %s", exc)
            time.sleep(self.interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--ssh", default="", help="SSH alias for a remote T3 server")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    watcher = T3CodeWatcher(
        Path(args.runtime), Path(args.token), Path(args.state), args.interval,
        HealthReporter("t3code_watcher", stale_after=20.0),
        ssh_host=args.ssh,
    )
    if args.once:
        print(json.dumps(watcher.poll_once(), indent=2))
        return 0
    watcher.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
