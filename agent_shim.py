#!/usr/bin/env python3
"""Agent hook shim for deckbridge: Claude, Codex, and Cursor -> deck keys.

Both Claude Code and Codex CLI fire lifecycle hooks that pass a JSON document
on stdin with the same shape (``session_id``, ``cwd``, ``hook_event_name``), so
one engine serves both.  Use the thin wrappers rather than calling this
directly:

* ``claude_shim.py``  ->  ``agent_shim.py --agent claude``
* ``codex_shim.py``   ->  ``agent_shim.py --agent codex``
* ``cursor_shim.py``  ->  ``agent_shim.py --agent cursor``

Each invocation translates one hook event into a deckbridge agent record and
upserts it into the local agent state file consumed by ``connector_cmux.py``::

    ~/.deckbridge/cmux_state.json

That is deliberately the SAME file ``cmux_shim.sh`` writes.  Keys 5-9 are the
"local agents" zone, and a Claude session, a Codex session, and a cmux pane are
all equally local agents, so they share one contract and compete for the same
slots.

Event to deck status mapping (shared where the two tools agree):

===================  ========  =============================================
hook_event_name      status    meaning on the deck
===================  ========  =============================================
SessionStart         idle      session registered, nothing running
UserPromptSubmit     working   turn started
PreToolUse           working   still working
PostToolUse          working   still working
PreCompact           working   compacting, still alive
PostCompact          working   compacting, still alive
SubagentStart        working   a child started, parent still going
SubagentStop         working   a child finished, parent still going
PermissionRequest    blocked   RED: the agent needs your approval
Notification         blocked   RED: the agent is waiting on you
StopFailure          blocked   RED: turn died on an error (Claude only)
Stop                 done      turn finished, probably unseen
SessionEnd           (evict)   record removed entirely (Claude only)
===================  ========  =============================================

Anything unrecognised is treated as ``working``, because an unknown event still
means the session is alive.

Three properties matter more than features here:

* **Distinct names per tool.**  The record name is prefixed per agent kind
  (``cc-`` for Claude, ``cx-`` for Codex) so a Claude session and a Codex
  session in the SAME directory occupy two keys instead of overwriting one
  record.  Deck labels render at 8 characters, so ``cc-deckb`` still tells you
  both the tool and the project.
* **Locked, atomic writes.**  A sibling advisory lock serializes the complete
  read/modify/write transaction, then a same-directory temp file is moved with
  ``os.replace``.  Concurrent hooks therefore cannot lose sibling sessions and
  the connector never observes a half-written document.
* **Stale eviction.**  Hooks only fire on events.  Codex has no ``SessionEnd``
  event at all, so a finished or killed session would otherwise leave a
  permanently lit key.  Every invocation prunes legacy records whose
  ``updated_at`` is older than ``--ttl`` seconds (default 900). Records with an
  exact PID and birth marker survive while that process is provably alive;
  connector-side liveness removes them immediately when it exits.

A hook must never break the user's session, so this script exits 0 on every
failure path and writes nothing rather than writing garbage.

It also keeps **stdout empty** unless ``--print`` is passed.  That is required,
not cosmetic: Codex expects JSON on stdout from ``Stop``/``SubagentStop`` hooks
and warns on invalid output, and an empty stdout with exit 0 is the documented
success case for both tools.  Never put ``--print`` in a real hook config.

Manual smoke test::

    echo '{"session_id":"abc","cwd":"/tmp/proj","hook_event_name":"UserPromptSubmit"}' \\
        | ./codex_shim.py --state /tmp/state.json --print
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_STATE = Path("~/.deckbridge/cmux_state.json").expanduser()
DEFAULT_TTL = 900.0
DEFAULT_LABEL_CHARS = 11

WORKING = "working"
BLOCKED = "blocked"
DONE = "done"
IDLE = "idle"
EVICT = "__evict__"

VALID_STATUSES = frozenset({WORKING, BLOCKED, DONE, IDLE})

#: Events both tools share.  Keys are lowercased ``hook_event_name`` values.
EVENT_STATUS = {
    "sessionstart": IDLE,
    "userpromptsubmit": WORKING,
    "userpromptexpansion": WORKING,
    # Cursor IDE / CLI lifecycle vocabulary. Cursor sends these in lower camel
    # case, but status_for_event normalises case so one table covers both.
    "beforesubmitprompt": WORKING,
    "afteragentresponse": WORKING,
    "afteragentthought": WORKING,
    "pretooluse": WORKING,
    "posttooluse": WORKING,
    "posttoolusefailure": WORKING,
    "posttoolbatch": WORKING,
    "subagentstart": WORKING,
    "subagentstop": WORKING,
    "precompact": WORKING,
    "postcompact": WORKING,
    "messagedisplay": WORKING,
    "permissionrequest": BLOCKED,
    "permissiondenied": BLOCKED,
    "elicitation": BLOCKED,
    "notification": BLOCKED,
    "stopfailure": BLOCKED,
    "teammateidle": IDLE,
    "stop": DONE,
    "sessionend": EVICT,
}

#: Per-tool identity.  ``prefix`` disambiguates two tools in one directory;
#: ``fallback`` labels a session with no usable cwd.
AGENTS: dict[str, dict[str, str]] = {
    "claude": {"prefix": "cc-", "source": "claude-code", "fallback": "cc"},
    "codex": {"prefix": "cx-", "source": "codex-cli", "fallback": "cx"},
    "cursor": {"prefix": "cu-", "source": "cursor-agent", "fallback": "cu"},
    "generic": {"prefix": "", "source": "agent-hook", "fallback": "agent"},
}


def agent_profile(agent: str) -> dict[str, str]:
    """Return the identity block for an agent kind, defaulting to generic."""
    return AGENTS.get((agent or "").strip().lower(), AGENTS["generic"])


def status_for_event(event: str) -> str:
    """Map a ``hook_event_name`` to a deckbridge status."""
    return EVENT_STATUS.get((event or "").strip().lower(), WORKING)


def status_for_payload(event: str, payload: dict[str, Any]) -> str:
    """Refine lifecycle status when an event has meaningful subtypes."""
    if str(event or "").strip().lower() != "notification":
        return status_for_event(event)
    kind = str(payload.get("notification_type") or "").strip().lower()
    if kind == "idle_prompt":
        # Claude emits this after Stop when it has finished and is merely
        # waiting for another prompt. It is not an outstanding decision.
        return DONE
    if kind in {"auth_success", "elicitation_complete", "elicitation_response"}:
        return WORKING
    if kind in {"permission_prompt", "elicitation_dialog"}:
        return BLOCKED
    # Preserve the old conservative behavior for future/unknown subtypes.
    return status_for_event(event)


def short_label(cwd: str, session_id: str, agent: str = "generic",
                limit: int = DEFAULT_LABEL_CHARS) -> str:
    """Build a short, deck-friendly agent label from the working directory.

    Stream Deck keys are tiny, so prefer the directory basename and fall back
    to a session-id fragment.  This does NOT apply the per-tool prefix; the
    caller adds that, so an explicit ``--prefix ''`` can suppress it.
    """
    profile = agent_profile(agent)
    fallback = profile["fallback"]
    base = ""
    if cwd:
        base = os.path.basename(str(cwd).rstrip("/")) or ""
    if not base:
        frag = re.sub(r"[^A-Za-z0-9]", "", str(session_id or ""))[-4:]
        base = f"{fallback}{frag}" if frag else fallback
    base = re.sub(r"[^A-Za-z0-9._-]+", "", base) or fallback
    return base[:limit]


_TITLE_STOPWORDS = {
    "a", "about", "actually", "all", "also", "and", "at", "be", "better",
    "btw", "can", "could", "do", "for", "full", "have", "how", "i", "idea",
    "if", "in", "into", "is", "it", "just", "make", "much", "none", "of",
    "on", "ones", "please", "really", "so", "some", "sure", "than", "that",
    "the", "then", "this", "to", "up", "we", "with", "work", "working",
    "worthwhile", "you",
}


def smart_title(text: str, limit: int = DEFAULT_LABEL_CHARS) -> str:
    """Compress one user task into a useful tiny-key label.

    This is intentionally deterministic and local: hooks must finish quickly,
    and the full prompt must never be persisted merely to name a key.  Common
    product phrases get human abbreviations; the generic path keeps the first
    semantic words and fits them without cutting through a word when possible.
    """
    clean = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    clean = re.sub(r"<[^>]+>|https?://\S+", " ", clean)
    clean = re.sub(r"[^A-Za-z0-9+'-]+", " ", clean).strip()
    if not clean or limit <= 0:
        return ""
    lower = clean.lower()

    broken = bool(re.search(
        r"\b(?:broken|fails?|failing|doesn'?t work|isn'?t working|not working)\b",
        lower,
    )) or bool(re.search(r"\bnone\b.*\bwork", lower))
    action = ""
    if broken or re.search(r"\bfix(?:e[ds])?\b", lower):
        action = "Fix"
    elif re.search(r"\b(?:run|test|verify|check)\b", lower):
        action = "Run"
    elif re.search(r"\b(?:add|create|install|build)\b", lower):
        action = "Add"

    # Phrases whose literal spelling wastes most of an 11-character key.
    if "session" in lower and re.search(r"\b(?:title|titles|naming|name)\b", lower):
        return "Sess titles"[:limit]
    if re.search(r"\b(?:voice|microphone|mic)\b", lower):
        topic = "voice"
    elif "test suite" in lower or "tests" in lower:
        topic = "tests"
    elif "accessibility" in lower:
        topic = "access"
    elif re.search(r"\b(?:latency|performance|perf|slow)\b", lower):
        topic = "speed"
    else:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", clean)
                 if w.lower() not in _TITLE_STOPWORDS
                 and w.lower() not in {"fix", "run", "test", "verify", "check",
                                       "add", "create", "install", "build",
                                       "improve", "figure", "out"}]
        if not words:
            return clean[:limit].strip()
        topic = words[0]
        if len(words) > 1 and len(topic) + 1 + len(words[1]) <= limit:
            topic = f"{topic} {words[1]}"

    candidate = f"{action} {topic}".strip()
    if len(candidate) <= limit:
        return candidate
    if len(topic) <= limit:
        return topic
    return topic[:limit].rstrip(" -_")


def payload_title(payload: dict[str, Any], event: str) -> str:
    """Return a compact task title only from title/user-prompt hook fields."""
    for key in ("task_title", "thread_title", "conversation_title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return smart_title(value)

    normalized = re.sub(r"[^a-z]", "", str(event or "").lower())
    if normalized not in {"userpromptsubmit", "beforesubmitprompt"}:
        return ""
    for key in ("title", "prompt", "user_prompt", "input", "message"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("content") or value.get("text")
        if isinstance(value, list):
            parts = [str(item.get("text") or "") for item in value
                     if isinstance(item, dict) and item.get("type") in ("text", "input_text")]
            value = " ".join(parts)
        if isinstance(value, str) and value.strip():
            return smart_title(value)
    return ""


def load_state(path: Path) -> dict[str, Any]:
    """Read the existing state document, tolerating anything malformed."""
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"agents": []}
    if not isinstance(data, dict):
        return {"agents": []}
    agents = data.get("agents")
    if not isinstance(agents, list):
        data["agents"] = []
    else:
        data["agents"] = [a for a in agents if isinstance(a, dict)]
    return data


def prune(agents: list[dict[str, Any]], now: float, ttl: float) -> list[dict[str, Any]]:
    """Drop old legacy/dead records while preserving exactly live sessions.

    Records with no ``updated_at`` are kept: they come from ``cmux_shim.sh``,
    which does not stamp a time, and this shim must not evict another
    producer's agents just because it cannot age them. An old record with a PID
    and birth marker is also kept while that exact process is alive or the
    bounded OS query is inconclusive.
    """
    if ttl <= 0:
        return list(agents)
    kept = []
    for agent in agents:
        stamp = agent.get("updated_at")
        if stamp is None:
            kept.append(agent)
            continue
        try:
            age = now - float(stamp)
        except (TypeError, ValueError):
            kept.append(agent)
            continue
        if age <= ttl:
            kept.append(agent)
            continue
        # A quiet interactive session can legitimately emit no hook for longer
        # than the legacy TTL.  Once the shim has an exact per-session PID and
        # birth marker, wall-clock silence is weaker evidence than the OS.  Do
        # not delete that record before the connector gets to verify it.
        if agent.get("agent_pid") and agent.get("agent_started_at"):
            live = recorded_process_liveness(agent)
            if live is not False:
                # Unknown (for example a transient ps failure) is deliberately
                # preserved: false-dead is worse than a stale record, and the
                # connector will retry its own bounded probe shortly.
                kept.append(agent)
    return kept


#: Environment variables that name the agent's surface DIRECTLY.
#:
#: This is stronger evidence than anything the resolver can reconstruct later.
#: A tty must be matched against a tree, and a cwd is not an identity at all --
#: eight tabs open in one repo report the same directory, which is exactly the
#: case that sent a press to an unrelated tab. A surface id needs no matching.
#:
#: The agent's own environment is where that id lives, because a hook is a child
#: of the agent and inherits it. Several names are tried rather than one: the
#: exact variable is a property of whichever terminal happens to be hosting the
#: session, and guessing wrong costs nothing while guessing narrowly loses the
#: only reliable identifier available.
SURFACE_ENV_VARS = (
    # Documented by `cmux --help` on 0.64.19. It contains the stable UUID, not
    # the display-only `surface:N` short ref printed by a default tree.
    "CMUX_SURFACE_ID",
    "CMUX_SURFACE", "CMUX_SURFACE_REF", "CMUX_PANE", "CMUX_PANE_ID",
    "CMUX_SESSION", "CMUX_SESSION_ID", "CMUX_ID",
)


def surface_from_env(env: dict[str, str] | None = None) -> str:
    """A surface id the agent already knows, or "" when it names none.

    Returns the value verbatim. It is not parsed or validated here: callers
    check whether it is shaped like a cmux ref, and a value this function does
    not recognise is better recorded than discarded.
    """
    source = os.environ if env is None else env
    for name in SURFACE_ENV_VARS:
        value = str(source.get(name) or "").strip()
        if value:
            return value
    return ""


def herdr_pane_from_env(env: dict[str, str] | None = None) -> str:
    """Return the exact Herdr pane inherited by an agent hook."""
    source = os.environ if env is None else env
    return str(source.get("HERDR_PANE_ID") or "").strip()


def current_tty() -> str:
    """The controlling terminal of this hook process, e.g. ``ttys013``.

    A hook runs as a child of the agent, inside the agent's terminal surface, so
    it can simply READ the tty that later has to be searched for. Recording it
    here is the difference between an identification and a guess: cmux exposes a
    per-surface tty, but its ``title`` is whatever the running program last set,
    and an agent's cwd may not appear in any title at all.

    Three sources are tried in order, because the obvious one is not enough:

    1. ``os.ttyname`` on fds 0-2. Free when any stream is still attached.
    2. ``ps`` on this process. Claude Code hands a hook its JSON on stdin and
       captures stdout and stderr, so all three fds are pipes and step 1 finds
       nothing -- observed on the test Mac, where the state file had no tty at
       all. But fd redirection does not detach the *controlling terminal*,
       which is a property of the session, so ps still reports it. (Opening
       /dev/tty is the tempting alternative and is wrong: os.ttyname on that fd
       returns the generic name "tty", not "ttys013", which matches no surface.)
    3. The process ancestry. Covers a hook started in its own session (setsid),
       which has no controlling terminal of its own to report.
    """
    for fd in (0, 1, 2):
        try:
            return _short_tty(os.ttyname(fd))
        except OSError:
            continue
    found = _tty_from_ancestors(os.getpid())
    if found:
        return found
    # 4. The agent's own process. A hook can be spawned detached from the
    #    session that owns the surface -- observed on the test Mac, where a live
    #    cmux Claude Code tab recorded no tty at all and the press then had
    #    nothing but a cwd to go on. Several tabs share a cwd, so that is not
    #    enough to identify a surface, and the press landed on the wrong tab.
    #
    #    The parent agent process is still attached to the terminal even when
    #    this child is not, so ask it directly. Restricted to an ancestor whose
    #    command names the agent, to avoid adopting the tty of some unrelated
    #    process that happens to be running.
    return _tty_from_agent_ancestor(os.getpid())


#: Ancestor commands that count as "the agent itself" when hunting for a tty.
#: Matched as substrings of the full command, because the binary may be invoked
#: via a wrapper, a version manager shim, or an absolute path.
_AGENT_CMD_HINTS = ("claude", "codex", "cursor", "cursor-agent")


def agent_process_pid(agent: str, pid: int | None = None, hops: int = 16) -> int | None:
    """Return the owning Claude/Codex process id from the hook ancestry.

    A PID gives the polling connector a clock-independent liveness fact. The
    hook wrapper itself contains ``codex``/``claude`` in its filename, so it is
    explicitly skipped; recording that short-lived Python process would make
    every session look dead as soon as its hook returned.
    """
    wanted = (agent or "").strip().lower()
    if wanted == "cursor":
        hints = ("cursor", "cursor-agent", "agent")
    elif wanted in {"claude", "codex"}:
        hints = (wanted,)
    else:
        hints = _AGENT_CMD_HINTS
    pid = os.getppid() if pid is None else pid
    for _ in range(hops):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(
                ["ps", "-o", "pid=", "-o", "ppid=", "-o", "command=",
                 "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        parts = out.split(None, 2)
        if len(parts) < 3:
            return None
        current, parent, command = parts
        lowered = command.lower()
        is_wrapper = any(name in lowered for name in (
            "agent_shim.py", "claude_shim.py", "codex_shim.py",
            "cursor_shim.py",
        ))
        tokens = [os.path.basename(token.strip("'\"").rstrip("/")).lower()
                  for token in command.split()]
        is_agent = any(hint in tokens for hint in hints)
        # App-bundle helpers and Codex's shared app-server outlive an
        # individual conversation. Their PID proves the application is open,
        # not that this session exists, so recording one creates immortal
        # false-live keys. CLI binaries are per-session and are safe evidence.
        is_shared_host = (".app/contents/" in lowered
                          or "codex app-server" in lowered)
        if not is_wrapper and is_agent and not is_shared_host:
            try:
                return int(current)
            except ValueError:
                return None
        try:
            pid = int(parent)
        except ValueError:
            return None
    return None


def process_started_at(pid: int | None) -> str:
    """Stable birth marker for a PID, preventing PID-reuse false liveness."""
    if pid is None or pid <= 1:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def recorded_process_liveness(agent: dict[str, Any]) -> bool | None:
    """Check a state record's PID + birth marker without guessing.

    ``True`` proves the same process is still present, ``False`` proves it is
    gone or the PID was reused, and ``None`` means the OS query itself failed.
    The distinction matters during hook-side pruning: an unavailable ``ps``
    must not turn a healthy quiet session into a false-dead key.
    """
    try:
        pid = int(agent.get("agent_pid"))
    except (TypeError, ValueError):
        return False
    expected = str(agent.get("agent_started_at") or "").strip()
    if pid <= 1 or not expected:
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return False
    actual = result.stdout.strip()
    if not actual:
        return None
    return actual == expected


def _tty_from_agent_ancestor(pid: int | None = None, hops: int = 12) -> str:
    """Walk further up looking specifically for the AGENT's terminal.

    ``_tty_from_ancestors`` stops at the first ancestor reporting any terminal
    and gives up after a few hops. This goes further and is choosier: it only
    accepts a tty from a process whose command names claude or codex, so a hook
    that was spawned detached still recovers the surface its agent lives in
    rather than adopting an unrelated process's terminal.
    """
    pid = os.getppid() if pid is None else pid
    for _ in range(hops):
        if pid <= 1:
            return ""
        try:
            out = subprocess.run(
                ["ps", "-o", "tty=", "-o", "ppid=", "-o", "command=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
        if not out:
            return ""
        parts = out.split(None, 2)
        if len(parts) < 3:
            return ""
        tty, parent, command = parts[0], parts[1], parts[2]
        lowered = command.lower()
        if any(hint in lowered for hint in _AGENT_CMD_HINTS):
            if tty not in ("??", "?", "-"):
                if tty.startswith("s") and tty[1:].isdigit():
                    tty = "tty" + tty
                return _short_tty(tty)
        try:
            pid = int(parent)
        except ValueError:
            return ""
    return ""


def _short_tty(name: str) -> str:
    """``/dev/ttys013`` -> ``ttys013``, ``/dev/pts/3`` -> ``pts/3``.

    Strip only the /dev/ prefix. Splitting on the last slash would turn Linux's
    /dev/pts/3 into "3"; macOS is unaffected either way, and cmux reports the
    same short form ("ttys013").
    """
    name = name.strip()
    return name[5:] if name.startswith("/dev/") else name


def _tty_from_ancestors(pid: int | None = None, hops: int = 6) -> str:
    """Walk up from ``pid`` until a process reports a terminal.

    Starts at the process itself: fd redirection hides the tty from ttyname but
    not from ps. Several hops are then allowed because the hook may be a
    grandchild of the agent that owns the surface (wrapper script, shell,
    python). ``ps`` prints ``??`` for a process with no terminal, and macOS
    abbreviates ``ttys013`` to ``s013`` in this column, which is restored here.
    """
    pid = os.getppid() if pid is None else pid
    for _ in range(hops):
        if pid <= 1:
            break
        try:
            out = subprocess.run(
                ["ps", "-o", "tty=", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            ).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return ""
        if len(out) < 2:
            break
        tty, parent = out[0], out[1]
        if tty not in ("??", "?", "-"):
            # macOS `ps` abbreviates the ttys prefix away; Linux prints pts/3.
            if tty.startswith("s") and tty[1:].isdigit():
                tty = "tty" + tty
            return _short_tty(tty)
        try:
            pid = int(parent)
        except ValueError:
            break
    return ""


#: Bundles that are the agent's terminal host rather than the agent's own app.
#: Recorded all the same: knowing the host is what lets a press activate the
#: right window instead of guessing from a list of running processes.
_APP_RE = re.compile(r"/([^/]+)\.app/Contents/")


def host_app(pid: int | None = None, hops: int = 8) -> str:
    """The macOS application bundle that this hook is running inside.

    Claude Code and Codex run in two very different places: a terminal surface
    (cmux, iTerm2) and the desktop applications. The surface case is solved by
    the tty; the desktop case has NO tty at all, which is why those keys did
    nothing -- every resolver downstream is a terminal resolver.

    A hook is a descendant of whatever launched the agent, so the bundle name
    can simply be READ off the ancestry instead of inferred later from a list
    of running processes. That inference is what once activated Terminal.app
    and opened a blank zsh window: `pgrep` can say an app is running but never
    which app owns this agent. Recording it here replaces the guess with a
    fact, and an empty string honestly means "not known" rather than a default.

    Returns e.g. ``Claude``, ``cmux``, ``iTerm``; ``""`` off macOS or when no
    ancestor lives in a bundle.
    """
    pid = os.getppid() if pid is None else pid
    for _ in range(hops):
        if pid <= 1:
            return ""
        try:
            out = subprocess.run(
                # BSD ps sizes every non-final output column narrowly. Putting
                # comm first truncates `/Applications/cmux.app/...` to
                # `/Applications/cm`, destroying the only `.app` evidence.
                # Keep the unbounded command column last and split the numeric
                # parent from the left so bundle paths containing spaces remain
                # intact. GNU ps accepts the same portable field ordering.
                ["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError):
            return ""
        if not out or not out[0].strip():
            return ""
        # `comm` is a full path on macOS and may itself contain spaces, so only
        # the leading ppid is split off.
        parts = out[-1].strip().split(None, 1)
        if len(parts) < 2:
            return ""
        parent, comm = parts[0], parts[1]
        match = _APP_RE.search(comm)
        if match:
            return match.group(1)
        try:
            pid = int(parent)
        except ValueError:
            return ""
    return ""


def upsert(
    agents: list[dict[str, Any]],
    name: str,
    status: str,
    cwd: str,
    now: float,
    source: str,
    tty: str = "",
    app: str = "",
    session_id: str = "",
    surface: str = "",
    herdr_pane: str = "",
    agent_pid: int | str | None = None,
    agent_started_at: str = "",
    display_title: str = "",
) -> list[dict[str, Any]]:
    """Replace the same session, else append a distinct session record.

    The display name is deliberately *not* identity. Two Codex tabs in the
    same repository both render as ``cx-deckbridge``; keying on that label made
    their hooks overwrite one another and SessionEnd for one removed both.
    ``session_id`` is exact when present, with the old name match retained only
    for legacy producers that do not provide one.
    """
    record = {
        "name": name,
        "status": status,
        "cwd": cwd,
        "updated_at": now,
        "source": source,
    }
    # Only recorded when known; an empty value must not overwrite a good one.
    # Both fields carry forward from the previous record for the same name,
    # because a later hook may fire from a context that cannot see them (a
    # setsid'd child has no tty; a detached helper has no bundle ancestor) and
    # forgetting a known-good identifier is worse than having none.
    #
    # session_id joined them because it was previously read from the hook
    # payload, used to build the display name, and then DROPPED. Nothing wrote
    # it to state, so `--session` reached focus_agent.sh empty on every press
    # and no exact-tab route could ever fire. It is the only id the agent and
    # its host app agree on, so losing it is what limits a press to raising an
    # application instead of opening the conversation.
    same_source = lambda a: not source or not a.get("source") or a.get("source") == source
    prior = None
    if session_id:
        prior = next((a for a in agents
                      if str(a.get("session_id") or "") == session_id
                      and same_source(a)), None)
        if prior is None:
            # Upgrade one pre-session-id legacy record in place. Once a record
            # has an id, a different id is always a different session.
            legacy = [a for a in agents if a.get("name") == name
                      and not a.get("session_id") and same_source(a)]
            if len(legacy) == 1:
                prior = legacy[0]
    else:
        prior = next((a for a in agents if a.get("name") == name
                      and same_source(a)), None)
    if display_title:
        record["display_title"] = display_title
    elif prior and prior.get("display_title"):
        # Tool/permission/stop hooks usually carry no prompt. Once a session
        # has earned a useful task label, those lifecycle events must not
        # revert it to the repository basename a few milliseconds later.
        record["name"] = prior["name"]
        record["display_title"] = prior["display_title"]
    for field, value in (("tty", tty), ("app", app),
                         ("session_id", session_id), ("surface", surface),
                         ("herdr_pane", herdr_pane),
                         ("agent_pid", agent_pid),
                         ("agent_started_at", agent_started_at)):
        if value:
            record[field] = value
        elif (prior and prior.get(field)
              and not (field == "agent_started_at" and agent_pid
                       and str(prior.get("agent_pid") or "") != str(agent_pid))):
            record[field] = prior[field]
    out = [a for a in agents if a is not prior]
    out.append(record)
    return out


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via a same-directory temp file plus ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent),
        prefix=path.name + ".tmp", delete=False,
    )
    tmp_name = handle.name
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def state_lock(path: Path):
    """Serialize one state file's complete read/modify/write transaction.

    ``os.replace`` makes a write atomic for readers, but it does not make a
    read followed by a write atomic against another hook.  Codex deliberately
    launches matching hooks concurrently, so without this lock every process
    can read the same old document and the last rename silently discards all
    sibling updates.  ``flock`` is released by the kernel if a hook exits or is
    killed, so there is no stale-lock cleanup path to get wrong.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def apply_event(
    state: dict[str, Any],
    event: str,
    name: str,
    cwd: str,
    now: float,
    ttl: float,
    source: str = "agent-hook",
    tty: str = "",
    app: str = "",
    session_id: str = "",
    surface: str = "",
    herdr_pane: str = "",
    agent_pid: int | str | None = None,
    agent_started_at: str = "",
    display_title: str = "",
    status_override: str | None = None,
) -> dict[str, Any]:
    """Return the new state document for one hook event."""
    agents = prune(state.get("agents", []), now=now, ttl=ttl)
    status = (status_override if status_override in VALID_STATUSES | {EVICT}
              else status_for_event(event))
    if status == EVICT:
        if session_id:
            agents = [a for a in agents
                      if not (str(a.get("session_id") or "") == session_id
                              and (not source or not a.get("source")
                                   or a.get("source") == source))]
        else:
            matches = [a for a in agents if a.get("name") == name
                       and (not source or not a.get("source")
                            or a.get("source") == source)]
            # With two same-label sessions and no id there is no honest way to
            # know which ended. Preserve both; evicting both would manufacture
            # a false-empty board. Legacy singletons still evict as before.
            if len(matches) == 1:
                victim = matches[0]
                agents = [a for a in agents if a is not victim]
    else:
        agents = upsert(
            agents, name=name, status=status, cwd=cwd, now=now, source=source,
            tty=tty, app=app, session_id=session_id, surface=surface,
            herdr_pane=herdr_pane, agent_pid=agent_pid,
            agent_started_at=agent_started_at, display_title=display_title,
        )
    out = dict(state)
    out["agents"] = agents
    return out


def read_stdin_json() -> dict[str, Any]:
    """Read the hook payload, returning an empty dict when absent or invalid."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def payload_cwd(payload: dict[str, Any], agent: str) -> str:
    """Return the workspace path from a tool's native hook payload.

    Claude and Codex send ``cwd``. Cursor sends ``workspace_roots`` because a
    window may be multi-root. Deckbridge currently has one focus path field,
    so a single root is exact and multiple roots are intentionally left blank:
    choosing the first of several would manufacture a window identity.
    """
    cwd = str(payload.get("cwd") or "").strip()
    if cwd or agent != "cursor":
        return cwd
    roots = payload.get("workspace_roots")
    if not isinstance(roots, list):
        return ""
    clean = [str(root).strip() for root in roots if str(root).strip()]
    return clean[0] if len(clean) == 1 else ""


def payload_session_id(payload: dict[str, Any], agent: str) -> str:
    """Return the exact conversation identity in each tool's vocabulary."""
    native = payload.get("session_id")
    if native:
        return str(native)
    if agent == "cursor":
        return str(payload.get("conversation_id") or "")
    return ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate a Claude Code, Codex CLI, or Cursor hook event into "
                    "deckbridge agent state.",
    )
    parser.add_argument(
        "--agent", default="generic", choices=sorted(AGENTS),
        help="which tool is calling; sets the label prefix and source tag",
    )
    parser.add_argument(
        "--state", type=Path, default=DEFAULT_STATE,
        help="agent state file to update (default: ~/.deckbridge/cmux_state.json)",
    )
    parser.add_argument(
        "--event", default=None,
        help="override hook_event_name (normally read from stdin JSON)",
    )
    parser.add_argument(
        "--name", default=None,
        help="override the derived agent label entirely, prefix included",
    )
    parser.add_argument(
        "--cwd", default=None,
        help="override the session working directory",
    )
    parser.add_argument(
        "--tty", default=None,
        help="override the recorded controlling terminal (default: detected)",
    )
    parser.add_argument(
        "--app", default=None,
        help="override the recorded host application bundle (default: detected)",
    )
    parser.add_argument(
        "--surface", default=None,
        help="override the recorded terminal surface id (default: from the "
             "agent's environment). A surface id needs no matching later, so "
             "it beats both the tty and the cwd.",
    )
    parser.add_argument(
        "--herdr-pane", default=None,
        help="override the exact Herdr pane id (default: HERDR_PANE_ID)",
    )
    parser.add_argument(
        "--agent-pid", type=int, default=None,
        help="override the owning agent pid (default: detected from ancestry)",
    )
    parser.add_argument(
        "--agent-started-at", default=None,
        help="override the owning process birth marker (default: ps lstart)",
    )
    parser.add_argument(
        "--ttl", type=float, default=DEFAULT_TTL,
        help="seconds before an un-updated record is evicted (0 disables)",
    )
    parser.add_argument(
        "--prefix", default=None,
        help="override the per-agent label prefix; pass '' for none",
    )
    parser.add_argument(
        "--print", dest="do_print", action="store_true",
        help="print the resulting document to stdout (debugging only, never "
             "in a hook config: Codex parses hook stdout)",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    profile = agent_profile(args.agent)
    payload = read_stdin_json()

    event = args.event or payload.get("hook_event_name") or ""
    cwd = args.cwd if args.cwd is not None else payload_cwd(payload, args.agent)
    session_id = payload_session_id(payload, args.agent)
    prefix = profile["prefix"] if args.prefix is None else args.prefix
    display_title = args.name or payload_title(payload, event)
    name = args.name or (prefix + (
        display_title or short_label(cwd, session_id, args.agent)
    ))
    owner_pid = (args.agent_pid if args.agent_pid is not None
                 else agent_process_pid(args.agent))
    owner_started_at = (args.agent_started_at
                        if args.agent_started_at is not None
                        else process_started_at(owner_pid))

    state_path = Path(args.state).expanduser()
    try:
        with state_lock(state_path):
            # Timestamp after acquiring the transaction lock so the process
            # that commits last also carries the newest ordering marker.
            now = time.time()
            state = load_state(state_path)
            updated = apply_event(
                state, event=event, name=name, cwd=cwd, now=now,
                ttl=args.ttl, source=profile["source"],
                status_override=status_for_payload(event, payload),
                tty=args.tty if args.tty is not None else current_tty(),
                app=args.app if args.app is not None else host_app(),
                session_id=session_id,
                surface=(args.surface if args.surface is not None
                         else surface_from_env()),
                herdr_pane=(args.herdr_pane if args.herdr_pane is not None
                            else herdr_pane_from_env()),
                agent_pid=owner_pid,
                agent_started_at=owner_started_at,
                display_title=display_title,
            )
            write_atomic(state_path, updated)
    except Exception as exc:  # never break the user's agent session
        print(f"agent_shim[{args.agent}]: {exc}", file=sys.stderr)
        return 0
    if args.do_print:
        print(json.dumps(updated, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
