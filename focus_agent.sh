#!/usr/bin/env bash
#
# Focus one deckbridge agent session on macOS.
#
# Usage:
#   focus_agent.sh --source SOURCE --name NAME [--cwd CWD] [--url URL]\
#                  [--session SESSION_ID] [--tty TTY] [--surface ID]\
#                  [--herdr-pane ID] [--dry-run] [--diagnose]
#
# SOURCE is claude-code, codex-cli, cursor-agent, cmux, or Hermes.
#
# macOS permissions:
#   * Accessibility may be required for terminal window/tab selection.
#   * Automation / Apple Events permission is required for osascript to control
#     iTerm2, Terminal, Claude, or another terminal application. macOS normally
#     prompts on first use; denying it makes that branch fail and the resolver
#     falls through to the next branch (or reports that no focus method worked).
#
# Evidence status:
#   VERIFIED: cmux's documented CLI reference lists list-panels, select-workspace,
#   and focus-panel: https://cmux.com/docs/api
#   VERIFIED: cmux is a macOS terminal with a CLI and socket API:
#   https://github.com/manaflow-ai/cmux
#   VERIFIED: tmux list-panes/select-window/select-pane/switch-client are the
#   documented tmux primitives: https://man7.org/linux/man-pages/man1/tmux.1.html
#   VERIFIED: lsof's -a/-p/-d/-F options are documented:
#   https://man7.org/linux/man-pages/man8/lsof.8.html
#   UNVERIFIED: the exact AppleScript tty properties and app-specific selection
#   semantics below have not been exercised on this Linux development host.
#   UNVERIFIED: discord://-/channels/GUILD/CHANNEL is reported by community
#   reverse-engineering, not Discord's official developer documentation, so this
#   script deliberately falls back to open'ing the supplied https URL.
#   UNVERIFIED: CMUX_FOCUS_CMD is an escape hatch for cmux versions/workflows
#   whose target is not available as a documented surface ID. It accepts
#   {name}, {cwd}, and {session} placeholders and is run by /bin/sh.
#
# This file intentionally stays compatible with the bash 3.2 shipped by macOS.

set -u

SCRIPT_NAME=${0##*/}
# Bumped whenever focus resolution changes, so --diagnose and a failed press
# both say which checkout actually ran. A stale extract alongside a fresh one
# produces symptoms identical to a logic bug.
BUILD_STAMP=2026-08-13.t3code-native-only
DRY_RUN=0
DIAGNOSE=0
TTY_HINT=
# A surface id the agent named itself, passed via --surface. Beats every other
# signal available here: it needs no lookup in the tree, and it identifies ONE
# tab, where a cwd matches every tab open in the same directory.
SURFACE_HINT=
# Stable pane ID inherited from Herdr (for example w1:p3).
HERDR_PANE_HINT=
APP_HINT=
LAUNCH_APP=
LAUNCH_T3CODE=0
HELP=0
SOURCE=
NAME=
CWD=
URL=
WEB_URL=
SESSION=

usage() {
  cat <<'EOF'
Usage: focus_agent.sh --source SOURCE --name NAME [--cwd CWD] [--url URL] [--web-url URL] [--session SESSION_ID] [--tty TTY] [--app APP] [--surface ID] [--herdr-pane ID] [--dry-run] [--diagnose]
       focus_agent.sh --launch APP
       focus_agent.sh --launch-t3code

SOURCE: claude-code | codex-cli | cursor-agent | cmux | hermes-discord | hermes-ssh

--app  the application the agent RUNS IN, recorded by its hook. A desktop
       Claude/Codex session has no tty and no cmux surface; this is the only
       way to reach it. Never launched: an agent key means "go to that
       session", so a quit app means the session is gone.
--launch  open an application, launching it when it is not running. The
       explicit app keys, where launching IS the intent.
EOF
}

error() {
  printf '%s: %s\n' "$SCRIPT_NAME" "$1" >&2
}

shell_quote() {
  # Single-quote one argument for a shell command/template.
  local value=$1
  value=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
  printf "'%s'" "$value"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --source)
        [ "$#" -ge 2 ] || { error "--source requires a value"; return 2; }
        SOURCE=$2
        shift 2
        ;;
      --name)
        [ "$#" -ge 2 ] || { error "--name requires a value"; return 2; }
        NAME=$2
        shift 2
        ;;
      --cwd)
        [ "$#" -ge 2 ] || { error "--cwd requires a value"; return 2; }
        CWD=$2
        shift 2
        ;;
      --url)
        [ "$#" -ge 2 ] || { error "--url requires a value"; return 2; }
        URL=$2
        shift 2
        ;;
      --web-url)
        [ "$#" -ge 2 ] || { error "--web-url requires a value"; return 2; }
        WEB_URL=$2
        shift 2
        ;;
      --session)
        [ "$#" -ge 2 ] || { error "--session requires a value"; return 2; }
        SESSION=$2
        shift 2
        ;;
      --tty)
        [ "$#" -ge 2 ] || { error "--tty requires a value"; return 2; }
        TTY_HINT=$2
        shift 2
        ;;
      --app)
        [ "$#" -ge 2 ] || { error "--app requires a value"; return 2; }
        APP_HINT=$2
        shift 2
        ;;
      --surface)
        [ "$#" -ge 2 ] || { error "--surface requires a value"; return 2; }
        SURFACE_HINT=$2
        shift 2
        ;;
      --herdr-pane)
        [ "$#" -ge 2 ] || { error "--herdr-pane requires a value"; return 2; }
        HERDR_PANE_HINT=$2
        shift 2
        ;;
      --launch)
        [ "$#" -ge 2 ] || { error "--launch requires a value"; return 2; }
        LAUNCH_APP=$2
        shift 2
        ;;
      --launch-t3code)
        LAUNCH_T3CODE=1
        shift
        ;;
      --diagnose)
        DIAGNOSE=1
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      -h|--help)
        usage
        HELP=1
        shift
        ;;
      *)
        error "unknown option: $1"
        usage >&2
        return 2
        ;;
    esac
  done

  [ "$HELP" -eq 1 ] && return 0
  # --launch is a mode of its own: an explicit "open this app" with no agent,
  # no session and no surface, so none of the agent arguments apply to it.
  if [ -n "$LAUNCH_APP" ] || [ "$LAUNCH_T3CODE" -eq 1 ]; then
    return 0
  fi
  [ -n "$SOURCE" ] || { error "--source is required"; return 2; }
  case "$SOURCE" in
    claude-code|claude-desktop|codex-cli|codex-desktop|cursor-agent|cursor-desktop|cmux|hermes-discord|hermes-ssh|t3code|t3code-*) ;;
    *)
      error "unknown --source: $SOURCE (expected claude-code, codex-cli, cursor-agent, cmux, hermes-discord, or hermes-ssh)"
      return 2
      ;;
  esac
  [ -n "$NAME" ] || { error "--name is required"; return 2; }
  if [ "$SOURCE" = "hermes-discord" ] && [ -z "$URL" ]; then
    error "--url is required for hermes-discord"
    return 2
  fi
  # An ssh-hosted Hermes agent's cwd is a path on the REMOTE host, so it must
  # never be defaulted to a local directory: matching a local pane against a
  # remote path would focus the wrong window.
  if [ -z "$CWD" ] && [ "$SOURCE" != "hermes-discord" ] && [ "$SOURCE" != "hermes-ssh" ]; then
    CWD=$(pwd -P)
  fi
  # Hooks observe app names from process ancestry and older shims wrote them
  # with inconsistent case. Canonical names keep routing/bundle lookup stable.
  case "$APP_HINT" in
    Claude|claude) APP_HINT=Claude ;;
    ChatGPT|chatgpt|Codex|codex) APP_HINT=ChatGPT ;;
    Cursor|cursor) APP_HINT=Cursor ;;
    "T3 Code"|"T3 Code (Alpha)"|t3code) APP_HINT="T3 Code (Alpha)" ;;
  esac
  return 0
}

# --- hermes-ssh --------------------------------------------------------------
# A Hermes agent started inside `cmux ssh hermes` lives in a pane whose local
# process is an ssh client, not a claude/codex binary, and whose cwd is remote.
# So it cannot be found by cwd matching. Instead find the pane whose running
# command is an ssh session to the configured host.
#
# HERMES_SSH_HOST is the ssh alias/profile name (default: hermes).
# HERMES_SSH_FOCUS_CMD overrides the whole strategy if your setup differs.
HERMES_SSH_HOST=${HERMES_SSH_HOST:-hermes}

# Pure logic helper, unit-tested: input lines are
#   target command_string
# and the first line whose command looks like an ssh session to $2 is printed.
ssh_pane_for_host() {
  local host=$2
  local line target rest
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    target=${line%% *}
    rest=${line#* }
    case "$rest" in
      *ssh*" $host"|*ssh*" $host "*|*ssh*"@$host"*|*"ssh $host"*)
        printf '%s\n' "$target"
        return 0
        ;;
    esac
  done <<EOF
$1
EOF
  return 1
}

focus_hermes_ssh() {
  # 1. explicit override wins
  if [ -n "${HERMES_SSH_FOCUS_CMD:-}" ]; then
    local expanded
    expanded=$(printf '%s' "$HERMES_SSH_FOCUS_CMD" \
      | sed -e "s|{host}|$HERMES_SSH_HOST|g" -e "s|{name}|$NAME|g")
    if [ "$DRY_RUN" -eq 1 ]; then
      printf '1. HERMES_SSH_FOCUS_CMD (UNVERIFIED): %s\n' "$expanded"
      printf 'WOULD RUN: %s\n' "$expanded"
      return 0
    fi
    sh -c "$expanded" >/dev/null 2>&1 && return 0
  fi

  # 2. tmux: find the pane whose command is an ssh session to the host.
  if command -v tmux >/dev/null 2>&1; then
    local listing target
    listing=$(tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_title}' 2>/dev/null || true)
    if [ -n "$listing" ]; then
      target=$(ssh_pane_for_host "$listing" "$HERMES_SSH_HOST" || true)
      if [ -n "$target" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
          printf '2. tmux ssh pane match: %s\n' "$target"
          printf 'WOULD RUN: tmux select-window -t %s ; tmux select-pane -t %s\n' "$target" "$target"
          return 0
        fi
        tmux select-window -t "$target" >/dev/null 2>&1
        tmux select-pane -t "$target" >/dev/null 2>&1 && return 0
      fi
    fi
  fi

  # 3. cmux: find the surface whose command is an ssh session to the host.
  if command -v cmux >/dev/null 2>&1; then
    local cmux_listing cmux_target
    cmux_listing=$(cmux tree --all --json 2>/dev/null || true)
    if [ -n "$cmux_listing" ]; then
      cmux_target=$(CMUX_JSON=$cmux_listing CMUX_HOST=$HERMES_SSH_HOST python3 -c '
import json, os, sys
try:
    doc = json.loads(os.environ.get("CMUX_JSON") or "")
except Exception:
    sys.exit(1)
host = os.environ.get("CMUX_HOST") or ""
# VERIFIED against cmux v3.9.6 `tree --all --json`: a surface exposes ref/title/
# tty/type. There is no `command`/`process` field, so `title` is the only text
# that can carry "ssh hermes". The other names are kept for schema tolerance.
ID_KEYS = ("ref", "surface_ref", "id", "surface", "surfaceRef", "panel", "handle")
TXT_KEYS = ("title", "command", "process", "name", "foregroundProcess", "cmdline")
found = None
def ref_of(n):
    for k in ID_KEYS:
        v = n.get(k)
        if isinstance(v, str) and v.startswith("surface:"):
            return v
    for k in ID_KEYS:
        v = n.get(k)
        if isinstance(v, str) and v:
            return v
    return None
def walk(n):
    global found
    if found:
        return
    if isinstance(n, dict):
        text = " ".join(str(n[k]) for k in TXT_KEYS if isinstance(n.get(k), str))
        ref = ref_of(n)
        if ref and text and "ssh" in text and host in text:
            found = ref
            return
        for v in n.values():
            walk(v)
    elif isinstance(n, list):
        for v in n:
            walk(v)
walk(doc)
if not found:
    sys.exit(1)
print(found)
' 2>/dev/null || true)
      if [ -n "$cmux_target" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
          printf '3. cmux ssh surface match: %s\n' "$cmux_target"
          printf 'WOULD RUN: cmux focus-panel --panel %s\n' "$cmux_target"
          return 0
        fi
        cmux focus-panel --panel "$cmux_target" >/dev/null 2>&1 && return 0
      fi
    fi
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '4. give up rather than raising an unrelated terminal window\n'
    printf 'WOULD RUN: nothing (no ssh pane or surface matched)\n'
    return 0
  fi
  return 1
}

# Pure logic helper: input lines are exactly
#   session:window.pane pane_current_path
# and the first exact cwd match is printed.
tmux_pane_for_cwd() {
  local target=$1
  local line pane_path
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    pane_path=${line#* }
    if [ "$pane_path" = "$target" ]; then
      printf '%s\n' "${line%% *}"
      return 0
    fi
  done
  return 1
}

print_dry_run() {
  local qname qcwd qurl qsession expanded
  qname=$(shell_quote "$NAME")
  qcwd=$(shell_quote "$CWD")
  qurl=$(shell_quote "$URL")
  qsession=$(shell_quote "$SESSION")

  printf 'dry-run: no external command will be executed\n'
  printf 'source=%s name=%s cwd=%s session=%s\n' "$SOURCE" "$NAME" "${CWD:-<none>}" "${SESSION:-<none>}"

  if [ "$SOURCE" = "hermes-discord" ]; then
    printf 'resolution chain: Discord URL\n'
    printf 'WOULD RUN: open %s\n' "$qurl"
    printf 'chosen command: open %s\n' "$qurl"
    return 0
  fi

  if [ -n "$HERDR_PANE_HINT" ]; then
    printf 'resolution chain: recorded Herdr pane %s with focus read-back\n' "$HERDR_PANE_HINT"
    printf 'WOULD RUN: herdr pane get %s\n' "$HERDR_PANE_HINT"
    printf 'WOULD RUN: herdr agent focus %s, then exact workspace/tab fallback if needed\n' "$HERDR_PANE_HINT"
    return 0
  fi

  if [ "$SOURCE" = "hermes-ssh" ]; then
    printf 'resolution chain: ssh pane for host %s\n' "$HERMES_SSH_HOST"
    focus_hermes_ssh
    return 0
  fi

  printf 'resolution chain (first successful branch at runtime):\n'
  if [ -n "$SESSION" ] && is_cmux_ref "$SESSION"; then
    printf '1. documented cmux surface focus: cmux focus-panel --panel %s\n' "$qsession"
    printf 'WOULD RUN: cmux focus-panel --panel %s\n' "$qsession"
  elif [ -n "$CWD" ]; then
    printf '1. cmux surface resolved by cwd (session %s is not a cmux ref)\n' "${SESSION:-<none>}"
    printf 'WOULD RUN: cmux tree --all --json\n'
    printf 'WOULD RUN: cmux focus-panel --panel <resolved-surface-ref>\n'
  elif [ -n "${CMUX_FOCUS_CMD:-}" ]; then
    expanded=${CMUX_FOCUS_CMD//\{name\}/$qname}
    expanded=${expanded//\{cwd\}/$qcwd}
    expanded=${expanded//\{session\}/$qsession}
    printf '1. configured cmux hook (UNVERIFIED): %s\n' "$expanded"
    printf 'WOULD RUN: %s\n' "$expanded"
  else
    printf '1. cmux: no session ID; CMUX_FOCUS_CMD is not configured\n'
    printf 'WOULD RUN: cmux focus-panel --panel <surface-id> (only when --session is supplied)\n'
  fi
  printf '2. tmux pane lookup: tmux list-panes -a -F '\''#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}'\''\n'
  printf 'WOULD RUN: tmux list-panes -a -F '\''#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}'\''\n'
  printf 'WOULD RUN: tmux select-window -t <session>:<window>\n'
  printf 'WOULD RUN: tmux select-pane -t <session>:<window>.<pane>\n'
  printf 'WOULD RUN: tmux switch-client -t <session>:<window>\n'
  printf '3. process lookup: pgrep -f '\''(^|[ /])(claude|codex)([ /]|$)'\''\n'
  printf 'WOULD RUN: lsof -a -p <PID> -d cwd -Fn\n'
  printf 'WOULD RUN: ps -o tty= -p <PID>\n'
  printf '4. terminal/app activation via timed osascript (TTY-aware for iTerm2/Terminal.app)\n'
  printf 'WOULD RUN: osascript -e <AppleScript> (timeout: 5s)\n'
  printf '5. last resort: activate the ALREADY-RUNNING host terminal (cmux preferred, then iTerm2/Ghostty/WezTerm/kitty/Alacritty/Code/Terminal); skipped entirely when it is not running, so no new window is ever launched\n'
  if [ -n "$SESSION" ] && is_cmux_ref "$SESSION"; then
    printf 'chosen command: cmux focus-panel --panel %s\n' "$qsession"
  elif [ -n "$CWD" ]; then
    printf 'chosen command: cmux focus-panel --panel <surface resolved from cwd %s>\n' "$qcwd"
  elif [ -n "${CMUX_FOCUS_CMD:-}" ]; then
    printf 'chosen command: %s\n' "$expanded"
  else
    printf 'chosen command: tmux list-panes -a -F '\''#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}'\''\n'
  fi
}

run_osascript_timeout() {
  # osascript has no portable timeout flag. Kill its child after five seconds.
  local apple_script=$1 child killer rc
  osascript -e "$apple_script" >/dev/null 2>&1 &
  child=$!
  ( sleep 5; kill "$child" 2>/dev/null ) &
  killer=$!
  wait "$child"
  rc=$?
  kill "$killer" 2>/dev/null || true
  wait "$killer" 2>/dev/null || true
  return "$rc"
}

# A cmux surface ref looks like "surface:2", "pane:1", or a bare index ("0").
# A Claude Code / Codex hook session_id is a UUID. Passing the latter to
# `cmux focus-panel --panel` is always wrong: it cannot match a surface, so the
# call fails and the whole chain falls through to "activate Terminal", which on
# macOS LAUNCHES Terminal.app and shows a brand-new zsh window. Gate on shape.
is_cmux_ref() {
  case "$1" in
    surface:*|pane:*|tab:*|workspace:*) return 0 ;;
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

# Stable cmux object IDs are UUIDs. Keep this separate from is_cmux_ref:
# a hook session UUID is NOT a cmux target, but a UUID read specifically from
# CMUX_SURFACE_ID and passed via --surface is authoritative.
is_cmux_uuid() {
  local id=$1 h4 h8 h12
  h4='[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]'
  h8="$h4$h4"
  h12="$h4$h4$h4"
  case "$id" in
    $h8-$h4-$h4-$h4-$h12) return 0 ;;
    *) return 1 ;;
  esac
}

# Resolve a cmux surface ref from the workspace tree.
#
# VERIFIED against real `cmux tree --all --json` output (v3.9.6): a surface is
#   { "ref": "surface:1", "title": "~/Documents/deckbridge", "tty": "ttys000",
#     "type": "terminal", "pane_ref": "pane:1", ... }
# There is NO cwd field anywhere in the tree. An earlier resolver looked for
# "cwd"/"workingDirectory"/"path" keys, found nothing, and therefore could never
# match a surface. The two fields that actually identify a surface are `tty`
# (authoritative) and `title` (a ~-abbreviated path, best effort).
#
# The resolver is written in Python, so a host without an interpreter silently
# resolved NOTHING and fell all the way through to the app fallback. macOS has
# not shipped a /usr/bin/python3 that works without the Command Line Tools, so
# this is a real failure mode on a clean Mac, not a hypothetical one. Find an
# interpreter once and say so out loud when there is none.
python_bin() {
  local candidate
  if [ -n "${FOCUS_PYTHON:-}" ]; then
    printf '%s\n' "$FOCUS_PYTHON"
    return 0
  fi
  for candidate in python3 python /usr/bin/python3 /opt/homebrew/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

debug() {
  [ "${FOCUS_DEBUG:-0}" = "1" ] || return 0
  printf 'focus_agent: debug: %s\n' "$*" >&2
}

herdr_pane_id_from_json() {
  local py
  py=$(python_bin) || return 1
  HERDR_JSON=$1 "$py" -c '
import json, os, sys
try:
    doc = json.loads(os.environ.get("HERDR_JSON") or "")
except Exception:
    sys.exit(1)
pane = (doc.get("result") or {}).get("pane") or {}
value = pane.get("pane_id")
if not isinstance(value, str) or not value:
    sys.exit(1)
print(value)
' 2>/dev/null
}

herdr_pane_field_from_json() {
  local py
  py=$(python_bin) || return 1
  HERDR_JSON=$1 HERDR_FIELD=$2 "$py" -c '
import json, os, sys
try:
    doc = json.loads(os.environ.get("HERDR_JSON") or "")
except Exception:
    sys.exit(1)
pane = (doc.get("result") or {}).get("pane") or {}
value = pane.get(os.environ.get("HERDR_FIELD") or "")
if not isinstance(value, str) or not value:
    sys.exit(1)
print(value)
' 2>/dev/null
}

herdr_client_tty() {
  # The headless server has no tty; the interactive TUI does. Matching the
  # client process by tty lets us raise the exact Terminal tab instead of an
  # arbitrary terminal window (or opening a second Herdr client).
  ps -axo tty=,command= 2>/dev/null | awk '
    $1 !~ /^\?/ {
      tty = $1
      $1 = ""
      if ($0 ~ /(^|[\/:[:space:]])herdr([[:space:]]|$)/) {
        print tty
        exit
      }
    }
  '
}

launch_herdr_terminal_client() {
  # A Herdr server owns the live panes independently of its viewers. If the
  # last Terminal client closed, the pane can still be selected and verified
  # but nothing visible comes forward. Start one viewer only after herdr_focus
  # has proved the requested pane exists; this is an exact-session attach, not
  # the old generic "open a terminal and hope" fallback.
  local herdr_bin
  herdr_bin=$(command -v herdr 2>/dev/null || true)
  [ -n "$herdr_bin" ] || return 1
  command -v osascript >/dev/null 2>&1 || {
    error "Herdr selected its pane, but osascript cannot start its Terminal viewer"
    return 1
  }

  osascript - "$herdr_bin" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  set herdrBin to item 1 of argv
  tell application "Terminal"
    activate
    do script "exec " & quoted form of herdrBin
  end tell
end run
APPLESCRIPT
}

select_herdr_terminal_tty() {
  local tty=$1
  command -v osascript >/dev/null 2>&1 || return 1

  osascript - "$tty" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
  set shortTTY to item 1 of argv
  if shortTTY starts with "/dev/" then
    set wantedTTY to shortTTY
  else
    set wantedTTY to "/dev/" & shortTTY
  end if
  set foundTTY to false
  tell application "Terminal"
    repeat with terminalWindow in windows
      repeat with terminalTab in tabs of terminalWindow
        if tty of terminalTab is wantedTTY then
          set selected tab of terminalWindow to terminalTab
          set index of terminalWindow to 1
          set foundTTY to true
          exit repeat
        end if
      end repeat
      if foundTTY then exit repeat
    end repeat
  end tell
  if not foundTTY then error "Herdr Terminal tty not found"
  tell application "System Events" to set frontmost of process "Terminal" to true
end run
APPLESCRIPT
}

activate_herdr_terminal() {
  local tty polls max_polls
  tty=$(herdr_client_tty || true)
  if [ -z "$tty" ]; then
    launch_herdr_terminal_client || {
      error "Herdr selected its pane, but no interactive Herdr terminal could be started"
      return 1
    }
    polls=0
    max_polls=${HERDR_CLIENT_POLLS:-30}
    case "$max_polls" in ''|*[!0-9]*) max_polls=30 ;; esac
    while [ "$polls" -lt "$max_polls" ]; do
      tty=$(herdr_client_tty || true)
      [ -n "$tty" ] && break
      polls=$((polls + 1))
      [ "$polls" -lt "$max_polls" ] && sleep 0.1
    done
  fi
  [ -n "$tty" ] || {
    error "Herdr selected its pane, but no interactive Herdr terminal was found"
    return 1
  }
  select_herdr_terminal_tty "$tty" || {
    error "Herdr selected its pane, but Terminal did not expose client tty $tty"
    return 1
  }
}

herdr_focus() {
  local target current raw workspace tab
  [ -n "$HERDR_PANE_HINT" ] || return 1
  command -v herdr >/dev/null 2>&1 || {
    error "agent recorded Herdr pane $HERDR_PANE_HINT but herdr is not installed"
    # Exit 2 means the recorded identity cannot exist in this environment.
    # Keep it distinct from a live pane that failed focus/read-back: callers
    # may safely try another exact identity only in the former case.
    return 2
  }

  # Resolve aliases first: Herdr preserves a pane's old ID after moving it to a
  # new workspace, while read-back reports the new canonical ID.
  raw=$(herdr pane get "$HERDR_PANE_HINT" 2>/dev/null || true)
  target=$(herdr_pane_id_from_json "$raw" || true)
  workspace=$(herdr_pane_field_from_json "$raw" workspace_id || true)
  tab=$(herdr_pane_field_from_json "$raw" tab_id || true)
  [ -n "$target" ] || {
    error "recorded Herdr pane $HERDR_PANE_HINT is no longer present"
    return 2
  }

  # Normal local agents are focusable by agent identity. An SSH shell hosting
  # a remote Hermes agent is a real pane but deliberately has no local Herdr
  # agent identity, so agent focus returns agent_not_found. Read back first;
  # if it missed, select the exact enclosing workspace and tab instead.
  herdr agent focus "$HERDR_PANE_HINT" >/dev/null 2>&1 || true
  raw=$(herdr pane current 2>/dev/null || true)
  current=$(herdr_pane_id_from_json "$raw" || true)
  if [ "$current" != "$target" ] && [ -n "$workspace" ] && [ -n "$tab" ]; then
    herdr workspace focus "$workspace" >/dev/null 2>&1 || true
    herdr tab focus "$tab" >/dev/null 2>&1 || true
    raw=$(herdr pane current 2>/dev/null || true)
    current=$(herdr_pane_id_from_json "$raw" || true)
  fi
  if [ "$current" != "$target" ]; then
    error "Herdr focused ${current:-<unknown>} but the agent is in $target"
    return 1
  fi
  activate_herdr_terminal || return 1
  return 0
}

# Inputs: CMUX_JSON, CMUX_TTY and/or CMUX_WANT (a working directory), and an
# optional CMUX_SURFACE UUID/ref recorded directly from the agent environment.
#
# Prints ONE line of five space-separated refs, "-" where unknown:
#   <surface> <pane> <workspace> <window> <tab>
#
# Why the ancestry and not just the surface: a surface is nested
# windows[] -> workspaces[] -> panes[] -> surfaces[], and focusing a panel that
# lives in a workspace which is not the selected one raises the WINDOW while
# leaving the visible workspace alone. That is exactly the "it opens cmux but
# on the default tab" symptom. Selecting the ancestor workspace first is the
# missing step, and it can only be done if the resolver reports the ancestor.
cmux_resolve_full() {
  local py
  py=$(python_bin) || {
    error 'no python3 interpreter found; cannot parse the cmux tree (set FOCUS_PYTHON)'
    return 1
  }
  CMUX_JSON=$1 CMUX_WANT=${2:-} CMUX_TTY=${3:-} CMUX_SURFACE=${4:-} "$py" -c '
import json, os, sys
raw = os.environ.get("CMUX_JSON") or ""
want = (os.environ.get("CMUX_WANT") or "").rstrip("/")
tty = (os.environ.get("CMUX_TTY") or "").strip()
surface_id = (os.environ.get("CMUX_SURFACE") or "").strip().casefold()
try:
    doc = json.loads(raw)
except Exception:
    sys.exit(1)

home = os.path.expanduser("~")

def norm(p):
    # Case-folded on purpose. macOS ships a case-INSENSITIVE filesystem, so a
    # shell reporting /Users/example/downloads/deckbridge and a cmux title
    # reading ~/Downloads/deckbridge are the same directory. Comparing them
    # exactly made a live surface look like no match at all, which then let the
    # desktop-app guess fire and open Claude instead of the cmux tab.
    p = p.strip()
    if p.startswith("~"):
        p = home + p[1:]
    return os.path.normpath(p.rstrip("/")).casefold() if p else ""

want_n = norm(want) if want else ""
# A bare tty name (ttys004) and a device path (/dev/ttys004) must compare equal.
def tty_norm(v):
    v = (v or "").strip()
    return v[5:] if v.startswith("/dev/") else v

tty_n = tty_norm(tty)

# Field names kept broad so a future cmux schema keeps working; the real v3.9.6
# names are listed first in each tuple.
ID_KEYS = ("ref", "surface_ref", "id", "surface", "surfaceRef", "panel", "handle")
TTY_KEYS = ("tty", "tty_name", "device")
CWD_KEYS = ("cwd", "workingDirectory", "working_dir", "dir")
TITLE_KEYS = ("title", "name")

# Each hit is (surface_ref, ancestry_dict).
by_tty = None
by_surface = None
by_cwd = None
by_title = None
# Every surface whose cwd matches, not just the first. A directory is NOT an
# identity: eight tabs open in one repo all report the same cwd, so picking the
# first is a coin flip that lands on the wrong tab seven times out of eight.
# Counted so an ambiguous match can be REFUSED rather than guessed.
cwd_hits = []
title_hits = []
# Claude Code and Codex rewrite the terminal title while they run ("✳ sample-api",
# "sample-api — claude"), so an exact title match is not enough on a live agent.
# Collect basename hits too, but only trust them when exactly one surface
# matches: one candidate is an identification, several is a guess.
basename_hits = []
want_base = os.path.basename(want_n) if want_n else ""

def ref_of(node):
    for k in ID_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v.startswith("surface:"):
            return v
    for k in ID_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v:
            return v
    return None


# A node is classified by the prefix of its own ref, so the ancestry survives a
# schema that renames the containers but keeps the ref namespace.
KIND_OF = {"window:": "window", "workspace:": "workspace",
           "pane:": "pane", "tab:": "tab"}


def classify(ref):
    for prefix, kind in KIND_OF.items():
        if ref.startswith(prefix):
            return kind
    return None


def walk(node, anc):
    global by_surface, by_tty, by_cwd, by_title
    if isinstance(node, dict):
        ref = ref_of(node)
        here = anc
        if ref:
            kind = classify(ref)
            if kind:
                here = dict(anc)
                here[kind] = ref
            # A surface may also name its own containers inline (pane_ref,
            # tab_ref); those beat an inferred ancestor because they are the
            # nodes own statement about where it lives.
            for key, kind2 in (("pane_ref", "pane"), ("tab_ref", "tab"),
                               ("workspace_ref", "workspace"),
                               ("window_ref", "window")):
                v = node.get(key)
                if isinstance(v, str) and v:
                    if here is anc:
                        here = dict(anc)
                    here[kind2] = v
            # CMUX_SURFACE_ID is a stable UUID while the default tree exposes
            # `surface:N`. With `--id-format both`, the surface node carries
            # both, so translate the exact agent-owned UUID to the short ref
            # and its workspace ancestry before focusing. Accepting the short
            # ref here as well gives the stale-identity guard below one parser
            # for both formats.
            if surface_id and ref.startswith("surface:"):
                v = node.get("id") or node.get("surface_id")
                if ref.casefold() == surface_id or (
                        isinstance(v, str) and v.casefold() == surface_id):
                    # The root-level `active` summary appears before the real
                    # nested surface and repeats only surface_ref. Keep looking
                    # for the richer match so workspace/window ancestry is not
                    # lost merely because this surface happens to be active.
                    if by_surface is None or len(here) > len(by_surface[1]):
                        by_surface = (ref, here)
            if tty_n and by_tty is None:
                for k in TTY_KEYS:
                    v = node.get(k)
                    if isinstance(v, str) and tty_norm(v) == tty_n:
                        by_tty = (ref, here)
                        break
            # Deliberately NOT short-circuited on `by_cwd is None`: every
            # matching surface has to be counted, because the count is what
            # decides whether this is an identification or a coin flip.
            if want_n:
                for k in CWD_KEYS:
                    v = node.get(k)
                    if isinstance(v, str) and v and norm(v) == want_n:
                        if by_cwd is None:
                            by_cwd = (ref, here)
                        cwd_hits.append(ref)
                        break
            if want_n:
                for k in TITLE_KEYS:
                    v = node.get(k)
                    if not isinstance(v, str) or not v:
                        continue
                    if norm(v) == want_n:
                        if by_title is None:
                            by_title = (ref, here)
                        title_hits.append(ref)
                        break
                    # v is casefolded here too: want_base comes from the
                    # casefolded path, so comparing it against a raw title
                    # would never match a differently-cased directory.
                    if want_base and want_base in v.casefold() and \
                            ref not in [h[0] for h in basename_hits]:
                        basename_hits.append((ref, here))
        for v in node.values():
            walk(v, here)
    elif isinstance(node, list):
        for v in node:
            walk(v, anc)

walk(doc, {})
# A recorded surface is exact only at the instant it is captured. Environment
# variables can survive a move between terminal hosts, and a short cmux ref can
# later be reused. When the hook also recorded the agent tty, require both facts
# to identify the SAME current surface. This turns contradictory exact-looking
# metadata into a refusal instead of a confident wrong-tab focus.
if surface_id and tty_n:
    if by_surface is None or by_tty is None or by_surface[0] != by_tty[0]:
        sys.stderr.write(
            "resolver: recorded surface %r and tty %r identify different current surfaces\n"
            % (surface_id, tty))
        sys.exit(1)
# tty is a kernel fact: it names exactly one surface and cannot be rewritten by
# whatever is running there. Everything below it is a heuristic.
#
# A cwd match is only an identification when it is UNIQUE. Eight tabs open in
# one repo all report the same directory, so accepting the first hit picks the
# wrong surface seven times out of eight -- and the press lands on an unrelated
# tab while reporting success, which is worse than not moving at all. The same
# rule already applied to basename hits; it should always have applied here.
best = by_surface or by_tty
if not best and len(set(cwd_hits)) == 1:
    best = by_cwd
if not best and len(set(title_hits)) == 1:
    best = by_title
# The basename fallback is the weakest signal of all -- a substring of a title.
# It must not rescue a case the stronger signals just refused: if several
# surfaces share this directory, a looser match cannot possibly tell them apart,
# it can only pick one and look confident about it.
if not best and len(basename_hits) == 1 and len(set(cwd_hits) | set(title_hits)) <= 1:
    best = basename_hits[0]
if not best:
    shared = sorted(set(cwd_hits) | set(title_hits))
    if len(shared) > 1:
        sys.stderr.write(
            "resolver: %d surfaces match %r and no tty was recorded, so the "
            "right one cannot be identified: %r\n"
            % (len(shared), want, shared))
    else:
        sys.stderr.write(
            "resolver: no surface matched tty=%r cwd=%r (ambiguous basename hits: %r)\n"
            % (tty, want, [h[0] for h in basename_hits]))
    sys.exit(1)
ref, anc = best
print(" ".join([ref or "-", anc.get("pane") or "-", anc.get("workspace") or "-",
                anc.get("window") or "-", anc.get("tab") or "-"]))
' 2>"$([ "${FOCUS_DEBUG:-0}" = 1 ] && echo /dev/stderr || echo /dev/null)"
}

# Back-compat surface: the surface ref alone.
cmux_resolve_surface() {
  local line
  line=$(cmux_resolve_full "$@") || return 1
  [ -n "$line" ] || return 1
  printf '%s\n' "${line%% *}"
}

# Prove that a recorded surface still belongs to the agent tty before using it
# as recovery from a stale Herdr pane. Both values may have been inherited from
# an older terminal host, so mere existence of the surface is not sufficient.
# This helper is read-only: it asks cmux for the tree and delegates identity
# comparison to the same resolver that normal focus uses.
cmux_surface_matches_tty() {
  local surface=$1 tty=$2 listing
  [ -n "$surface" ] && [ -n "$tty" ] || return 1
  command -v cmux >/dev/null 2>&1 || return 1
  listing=$(cmux --id-format both tree --all --json 2>/dev/null || true)
  [ -n "$listing" ] || listing=$(cmux tree --all --json 2>/dev/null || true)
  [ -n "$listing" ] || return 1
  cmux_resolve_full "$listing" "" "$tty" "$surface" >/dev/null
}

# Kept as a thin wrapper: cwd-only resolution with no tty hint.
cmux_ref_for_cwd() {
  cmux_resolve_surface "$1" "$2" ""
}

# The tty of a live claude/codex/agent process whose cwd is $1. This is what
# turns an agent into a surface: the process knows its tty, and the cmux tree
# maps a tty to a surface ref.
tty_for_cwd() {
  local target=$1 pattern pids pid cwd_info line cwd_path tty
  [ -n "$target" ] || return 1
  command -v pgrep >/dev/null 2>&1 || return 1
  command -v lsof >/dev/null 2>&1 || return 1
  command -v ps >/dev/null 2>&1 || return 1
  pattern=${AGENT_PROC_PATTERN:-'(^|[ /])(claude|codex|node|python[0-9.]*)([ /]|$)'}
  pids=$(pgrep -f "$pattern" 2>/dev/null) || return 1
  for pid in $pids; do
    cwd_info=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null) || continue
    cwd_path=
    while IFS= read -r line; do
      case "$line" in
        n*) cwd_path=${line#n} ;;
      esac
    done <<EOF
$cwd_info
EOF
    [ "${cwd_path%/}" = "${target%/}" ] || continue
    tty=$(ps -o tty= -p "$pid" 2>/dev/null | sed 's/[[:space:]]//g')
    [ -n "$tty" ] && [ "$tty" != "?" ] || continue
    printf '%s\n' "$tty"
    return 0
  done
  return 1
}

# Like tty_for_cwd, but case-insensitively and WITHOUT lsof.
#
# Two things defeat tty_for_cwd on a real Mac. lsof may be slow or refused, and
# the comparison is exact while the filesystem is case-insensitive, so a shell
# reporting .../downloads/... never matches an agent in .../Downloads/... .
#
# This matters more than it looks: when no tty is found the resolver falls back
# to matching a cwd, and a cwd matches EVERY tab open in that repo. Recovering
# the tty is what turns "one of these eight tabs" into "this tab".
agent_tty_for_cwd() {
  local target=$1 pattern pids pid cwd_path tty
  [ -n "$target" ] || return 1
  command -v pgrep >/dev/null 2>&1 || return 1
  command -v ps >/dev/null 2>&1 || return 1
  pattern=${AGENT_PROC_PATTERN:-'(^|[ /])(claude|codex)([ /]|$)'}
  pids=$(pgrep -f "$pattern" 2>/dev/null) || return 1
  for pid in $pids; do
    cwd_path=""
    if command -v lsof >/dev/null 2>&1; then
      cwd_path=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
    fi
    [ -n "$cwd_path" ] || continue
    # Case-folded compare: macOS treats these as the same directory.
    if [ "$(printf '%s' "${cwd_path%/}" | tr '[:upper:]' '[:lower:]')" \
       != "$(printf '%s' "${target%/}" | tr '[:upper:]' '[:lower:]')" ]; then
      continue
    fi
    tty=$(ps -o tty= -p "$pid" 2>/dev/null | sed 's/[[:space:]]//g')
    [ -n "$tty" ] && [ "$tty" != "?" ] && [ "$tty" != "??" ] || continue
    case "$tty" in
      s[0-9]*) tty="tty$tty" ;;
    esac
    printf '%s\n' "$tty"
    return 0
  done
  return 1
}

# Focus a resolved surface AND the workspace that contains it.
#
# `cmux focus-panel --panel surface:N` focuses the panel, but when that panel
# lives in a workspace which is not the window's selected workspace, the window
# comes forward still showing whatever workspace was already selected. That is
# the reported symptom: cmux opens, on the default tab.
#
# Current cmux help documents `--workspace` and the `--window` context needed
# for short refs. Older invocation shapes remain as compatibility fallbacks.
#
# The result is then VERIFIED rather than assumed: `cmux tree` reports the
# active surface, so the function can check it actually landed and say so.
cmux_select_workspace() {
  local ws=$1 window=${2:-}
  [ -n "$ws" ] && [ "$ws" != "-" ] || return 1
  if [ -n "${CMUX_SELECT_WORKSPACE_CMD:-}" ]; then
    local expanded=${CMUX_SELECT_WORKSPACE_CMD//\{workspace\}/$ws}
    expanded=${expanded//\{window\}/$window}
    sh -c "$expanded" >/dev/null 2>&1 && return 0
  fi
  if [ -n "$window" ] && [ "$window" != "-" ]; then
    cmux select-workspace --workspace "$ws" --window "$window" >/dev/null 2>&1 && return 0
  fi
  cmux select-workspace --workspace "$ws" >/dev/null 2>&1 && return 0
  cmux select-workspace "$ws" >/dev/null 2>&1 && return 0
  cmux select-workspace --id "$ws" >/dev/null 2>&1 && return 0
  cmux select-workspace --ref "$ws" >/dev/null 2>&1 && return 0
  return 1
}

cmux_focus_panel_in_context() {
  local surface=$1 workspace=${2:-} window=${3:-}
  if [ -n "$workspace" ] && [ "$workspace" != "-" ] \
     && [ -n "$window" ] && [ "$window" != "-" ]; then
    cmux focus-panel --panel "$surface" --workspace "$workspace" --window "$window" >/dev/null 2>&1
  elif [ -n "$window" ] && [ "$window" != "-" ]; then
    cmux focus-panel --panel "$surface" --window "$window" >/dev/null 2>&1
  elif [ -n "$workspace" ] && [ "$workspace" != "-" ]; then
    cmux focus-panel --panel "$surface" --workspace "$workspace" >/dev/null 2>&1
  else
    cmux focus-panel --panel "$surface" >/dev/null 2>&1
  fi
}

# The surface cmux currently reports as active, or "" when it cannot be read.
cmux_active_surface() {
  local py listing
  py=$(python_bin) || return 1
  listing=$(cmux tree --all --json 2>/dev/null || true)
  [ -n "$listing" ] || return 1
  CMUX_JSON=$listing "$py" -c '
import json, os, sys
try:
    doc = json.loads(os.environ["CMUX_JSON"])
except Exception:
    sys.exit(1)
act = doc.get("active")
if not isinstance(act, dict):
    sys.exit(1)
ref = act.get("surface_ref")
if not isinstance(ref, str) or not ref:
    sys.exit(1)
print(ref)
' 2>/dev/null
}

cmux_focus_resolved() {
  local surface=$1 workspace=$3 window=$4 active
  [ -n "$surface" ] && [ "$surface" != "-" ] || return 1

  # Workspace first, panel second. The reverse lets a workspace switch move
  # focus off the panel that was just focused.
  cmux_select_workspace "$workspace" "$window" || true
  cmux_focus_panel_in_context "$surface" "$workspace" "$window" || return 1

  # Confirm. A focus-panel that exits 0 while the wrong workspace stays visible
  # is exactly the failure being fixed, so trusting the exit code is not enough.
  active=$(cmux_active_surface 2>/dev/null || true)
  if [ -n "$active" ] && [ "$active" != "$surface" ]; then
    debug "focus landed on $active, wanted $surface; retrying after workspace select"
    cmux_select_workspace "$workspace" "$window" || true
    cmux_focus_panel_in_context "$surface" "$workspace" "$window" || return 1
    active=$(cmux_active_surface 2>/dev/null || true)
    if [ -n "$active" ] && [ "$active" != "$surface" ]; then
      error "cmux focused $active but the agent is on $surface (workspace ${workspace:-unknown})"
      return 1
    fi
  fi
  return 0
}

cmux_has_surfaces() {
  # True when cmux is running and reports at least one surface.
  #
  # Used to refuse a desktop-app guess. An agent whose hook recorded no tty is
  # unidentified, not proven to be desktop-hosted, and a live cmux tree is
  # strong evidence the session is a terminal one this resolver merely failed
  # to pin down. Activating a chat app in that situation shows the wrong window
  # and reports success, which is worse than an honest failure.
  command -v cmux >/dev/null 2>&1 || return 1
  local listing
  listing=$(cmux tree --all --json 2>/dev/null || true)
  [ -n "$listing" ] || return 1
  case "$listing" in
    *'"surface:'*) return 0 ;;
    *) return 1 ;;
  esac
}

cmux_focus() {
  local command qname qcwd qsession listing resolved agent_tty
  command -v cmux >/dev/null 2>&1 || return 1

  # 0. A surface the AGENT named. Nothing here needs matching, so this is tried
  #    before tty/cwd matching. A short ref still needs one tree lookup for its
  #    workspace/window ancestry: current cmux scopes `surface:N` to the
  #    selected workspace and can otherwise reject a globally valid ref.
  if [ -n "$SURFACE_HINT" ] && is_cmux_ref "$SURFACE_HINT"; then
    listing=$(cmux tree --all --json 2>/dev/null || true)
    if [ -n "$listing" ]; then
      resolved=$(cmux_resolve_full "$listing" "" "" "$SURFACE_HINT" || true)
      if [ -n "$resolved" ]; then
        # shellcheck disable=SC2086  # deliberate: five positional refs
        if cmux_focus_resolved $resolved; then
          printf 'focus_agent: focused surface %s (recorded by the agent)\n' "$SURFACE_HINT"
          return 0
        fi
      fi
      error "recorded cmux surface $SURFACE_HINT is no longer present or focusable"
      return 1
    fi
    # Compatibility for older cmux builds without a global tree. Read-back
    # still prevents an exit-zero wrong landing from being accepted.
    if cmux_focus_resolved "$SURFACE_HINT" - - - -; then
      printf 'focus_agent: focused surface %s (recorded by the agent)\n' "$SURFACE_HINT"
      return 0
    fi
  fi
  if [ -n "$SURFACE_HINT" ] && is_cmux_uuid "$SURFACE_HINT"; then
    # cmux exports CMUX_SURFACE_ID as a UUID. Ask for both ID formats so the
    # exact UUID can be translated to the surface ref plus workspace ancestry;
    # this preserves the post-focus verification used for short refs.
    listing=$(cmux --id-format both tree --all --json 2>/dev/null || true)
    resolved=$(cmux_resolve_full "$listing" "" "" "$SURFACE_HINT" || true)
    if [ -n "$resolved" ]; then
      # shellcheck disable=SC2086  # deliberate: five positional refs
      if cmux_focus_resolved $resolved; then
        printf 'focus_agent: focused surface %s (recorded by the agent)\n' "$SURFACE_HINT"
        return 0
      fi
    fi
    error "recorded cmux surface $SURFACE_HINT is no longer present"
    return 1
  fi

  # 1. An explicit, correctly-shaped cmux surface ref can be focused directly.
  if [ -n "$SESSION" ] && is_cmux_ref "$SESSION"; then
    cmux focus-panel --panel "$SESSION" >/dev/null 2>&1 && return 0
  fi

  # 2. Otherwise resolve the surface from the cmux tree. Two signals, strongest
  #    first: the tty of the live agent process (a kernel fact) and the surface
  #    title (a ~-abbreviated path the program may overwrite). Each listing is
  #    fetched lazily so a matched tree costs one socket round-trip per press.
  # The hook recorded the agent's own tty from inside its surface, so prefer it
  # over anything this process can infer. Falling back to tty_for_cwd only helps
  # when the agent process is still alive AND lsof is permitted.
  agent_tty=$TTY_HINT
  if [ -n "$CWD" ] || [ -n "$agent_tty" ]; then
    # No recorded tty is the common case for a session started before the hook
    # learned to record one. Ask the live agent process instead of falling
    # through to a cwd match, which cannot tell four tabs in one repo apart.
    [ -n "$agent_tty" ] || agent_tty=$(tty_for_cwd "$CWD" 2>/dev/null || true)
    [ -n "$agent_tty" ] || agent_tty=$(agent_tty_for_cwd "$CWD" 2>/dev/null || true)
    # `tree --all` is the authority for uniqueness because it sees every
    # workspace. `list-panels` is scoped to the selected workspace on current
    # cmux builds; retrying an ambiguous global result against that partial view
    # makes the current tab look uniquely identified and falsely reports success.
    listing=$(cmux tree --all --json 2>/dev/null || true)
    if [ -n "$listing" ]; then
      resolved=$(cmux_resolve_full "$listing" "$CWD" "$agent_tty" || true)
      if [ -n "$resolved" ]; then
        # shellcheck disable=SC2086  # deliberate: five positional refs
        cmux_focus_resolved $resolved && return 0
      fi
      return 1
    fi

    # Compatibility fallback for cmux versions without `tree --all`. It is
    # safe only when no global tree was available, never as a second opinion on
    # a tree that already refused an ambiguous match.
    listing=$(cmux list-panels --json 2>/dev/null || true)
    if [ -n "$listing" ]; then
      resolved=$(cmux_resolve_full "$listing" "$CWD" "$agent_tty" || true)
      if [ -n "$resolved" ]; then
        # shellcheck disable=SC2086  # deliberate: five positional refs
        cmux_focus_resolved $resolved && return 0
      fi
    fi
  fi

  if [ -n "${CMUX_FOCUS_CMD:-}" ]; then
    qname=$(shell_quote "$NAME")
    qcwd=$(shell_quote "$CWD")
    qsession=$(shell_quote "$SESSION")
    command=${CMUX_FOCUS_CMD//\{name\}/$qname}
    command=${command//\{cwd\}/$qcwd}
    command=${command//\{session\}/$qsession}
    # UNVERIFIED configurable hook; it is intentionally shell-template based.
    sh -c "$command" && return 0
  fi
  return 1
}

# Recover a session that moved from Herdr into cmux while inheriting stale host
# variables. The tty is a live kernel identity, so it may supersede an obsolete
# pane/surface hint—but only through the ordinary global-tree resolver, which
# requires the tty to identify exactly one current surface and verifies the
# post-focus active surface. Clear every older ID during this one attempt so a
# UUID reused by another tab cannot win before the tty is considered.
cmux_focus_recorded_tty() {
  local saved_surface saved_session rc
  [ -n "$TTY_HINT" ] || return 1
  saved_surface=$SURFACE_HINT
  saved_session=$SESSION
  SURFACE_HINT=
  SESSION=
  cmux_focus
  rc=$?
  SURFACE_HINT=$saved_surface
  SESSION=$saved_session
  return "$rc"
}

tmux_focus() {
  local panes pane_ref target rest window pane session
  command -v tmux >/dev/null 2>&1 || return 1
  panes=$(tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_path}' 2>/dev/null) || return 1
  pane_ref=$(printf '%s\n' "$panes" | tmux_pane_for_cwd "$CWD") || return 1
  [ -n "$pane_ref" ] || return 1

  session=${pane_ref%%:*}
  rest=${pane_ref#*:}
  window=${rest%%.*}
  pane=${rest#*.}
  target="$session:$window"
  tmux select-window -t "$target" >/dev/null 2>&1 || return 1
  tmux select-pane -t "$target.$pane" >/dev/null 2>&1 || return 1
  # switch-client fails when called outside an attached tmux client; selection
  # above is still useful in that case, so treat this final operation as best effort.
  tmux switch-client -t "$target" >/dev/null 2>&1 || true
  return 0
}

terminal_app_name() {
  local candidate
  if [ -n "${FOCUS_TERMINAL_APP:-}" ]; then
    printf '%s\n' "$FOCUS_TERMINAL_APP"
    return 0
  fi
  command -v pgrep >/dev/null 2>&1 || { printf 'Terminal\n'; return 0; }
  # cmux is checked FIRST: it is the documented host for these agents, and
  # falling straight to Terminal.app is what opened an unrelated zsh window.
  for candidate in cmux iTerm2 Ghostty WezTerm kitty Alacritty Code Terminal; do
    if pgrep -x "$candidate" >/dev/null 2>&1 || pgrep -f "$candidate.app" >/dev/null 2>&1; then
      case "$candidate" in
        Code) printf 'Visual Studio Code\n' ;;
        *) printf '%s\n' "$candidate" ;;
      esac
      return 0
    fi
  done
  printf 'Terminal\n'
}

focus_terminal_app() {
  local tty=$1 app=$2 tty_path apple_script
  [ -n "$tty" ] || return 1
  [ "$tty" != "?" ] || return 1
  command -v osascript >/dev/null 2>&1 || return 1
  # Every AppleScript below starts with `activate`, which LAUNCHES the app when
  # it is not running. Terminal.app launched this way opens a brand-new login
  # zsh window, which is the exact symptom this script exists to prevent. The
  # same check exists in last_resort_focus; it is repeated here because this
  # function is reachable from process_focus without passing through that one.
  if ! app_is_running "$app"; then
    error "not focusing $app: it is not running (refusing to launch a new window)"
    return 1
  fi
  tty_path="$tty"
  case "$tty_path" in
    /dev/*) ;;
    *) tty_path="/dev/$tty_path" ;;
  esac

  case "$app" in
    iTerm2)
      # UNVERIFIED exact iTerm2 dictionary property names; app activation and
      # session selection are based on iTerm2's public scripting guide.
      apple_script="tell application \"iTerm2\"
activate
repeat with w in windows
  repeat with t in tabs of w
    repeat with s in sessions of t
      if (tty of s as text) is \"$tty_path\" then
        set current session of t to s
        set current tab of w to t
        set index of w to 1
        return
      end if
    end repeat
  end repeat
end repeat
end tell"
      ;;
    Terminal)
      # UNVERIFIED exact Terminal.app tty property behavior on current macOS.
      apple_script="tell application \"Terminal\"
activate
repeat with w in windows
  repeat with t in tabs of w
    if (tty of t as text) is \"$tty_path\" then
      set selected tab of w to t
      set index of w to 1
      return
    end if
  end repeat
end repeat
end tell"
      ;;
    *)
      # Ghostty, WezTerm, kitty, Alacritty, and VS Code have no stable,
      # documented tab-selection AppleScript contract used here: activate only.
      apple_script="tell application \"$app\" to activate"
      ;;
  esac
  run_osascript_timeout "$apple_script"
}

process_focus() {
  local pattern pids pid cwd_info line cwd_path tty app
  # A cwd match tells us WHICH tty the agent owns, but terminal_app_name guesses
  # the host app from a list of running processes and has no idea which app owns
  # that tty. For a cmux-hosted agent that guess lands on Terminal.app whenever
  # cmux is not matched by pgrep, and activating Terminal opens a fresh login
  # zsh. cmux_focus already had its shot; do not guess after it.
  if is_cmux_hosted_source "$SOURCE"; then
    error "$SOURCE is cmux-hosted; not guessing a host terminal from its pid"
    return 1
  fi
  command -v pgrep >/dev/null 2>&1 || return 1
  command -v lsof >/dev/null 2>&1 || return 1
  command -v ps >/dev/null 2>&1 || return 1
  command -v osascript >/dev/null 2>&1 || return 1

  pattern='(^|[ /])(claude|codex)([ /]|$)'
  pids=$(pgrep -f "$pattern" 2>/dev/null) || pids=
  for pid in $pids; do
    cwd_info=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null) || cwd_info=
    cwd_path=
    while IFS= read -r line; do
      case "$line" in
        n*) cwd_path=${line#n} ;;
      esac
    done <<EOF
$cwd_info
EOF
    [ "$cwd_path" = "$CWD" ] || continue
    tty=$(ps -o tty= -p "$pid" 2>/dev/null | sed 's/[[:space:]]//g')
    [ -n "$tty" ] || continue
    [ "$tty" != "?" ] || continue
    app=$(terminal_app_name)
    focus_terminal_app "$tty" "$app" && return 0
  done
  return 1
}

# Raising an app that is NOT running is the bug that produced a brand-new zsh
# window on every press: AppleScript `activate` LAUNCHES a non-running app.
# Never activate anything that is not already running.
app_is_running() {
  local app=$1
  command -v pgrep >/dev/null 2>&1 || return 1
  case "$app" in
    iTerm2) pgrep -x iTerm2 >/dev/null 2>&1 && return 0
            pgrep -f 'iTerm.app' >/dev/null 2>&1 && return 0
            return 1 ;;
    "Visual Studio Code") pgrep -x Code >/dev/null 2>&1 && return 0; return 1 ;;
    Claude) pgrep -x Claude >/dev/null 2>&1 && return 0
            pgrep -f 'Claude.app' >/dev/null 2>&1 && return 0
            return 1 ;;
    *) pgrep -x "$app" >/dev/null 2>&1 && return 0
       pgrep -f "$app.app" >/dev/null 2>&1 && return 0
       return 1 ;;
  esac
}

# AppleScript reports success when `activate` merely delivered an event, even
# if a stale/headless process has no window and another app remains in front.
# Focus is world state, so ask System Events after the action and fail closed
# when the recorded host did not actually become frontmost.
app_is_frontmost() {
  local app=$1 apple_script
  command -v osascript >/dev/null 2>&1 || return 1
  apple_script="tell application \"System Events\"
repeat 10 times
  if exists application process \"$app\" then
    if frontmost of application process \"$app\" then return true
  end if
  delay 0.1
end repeat
error \"$app did not become frontmost\"
end tell"
  run_osascript_timeout "$apple_script"
}

# Agents that live INSIDE a cmux surface. For these there is exactly one correct
# target: the surface. "Raise some terminal app" is never a useful answer for
# them and is what produced a blank zsh window, so it is not offered.
is_cmux_hosted_source() {
  case "$1" in
    claude-code|codex-cli|codex|cursor-agent|cmux|hermes-ssh) return 0 ;;
    *) return 1 ;;
  esac
}

# The desktop Claude app is NOT Claude Code. Claude Code is a CLI running inside
# a terminal surface, so activating "Claude" focuses an unrelated chat app.
# Resolve terminal-hosted agents to their host terminal instead.
last_resort_focus() {
  local app apple_script
  # Checked BEFORE osascript: this is a policy decision about the source, not a
  # capability question about the host.
  if is_cmux_hosted_source "$SOURCE"; then
    error "$SOURCE lives in a cmux surface; refusing to raise a bare terminal instead"
    return 1
  fi
  command -v osascript >/dev/null 2>&1 || return 1
  app=$(terminal_app_name)
  if ! app_is_running "$app"; then
    error "not focusing $app: it is not running (refusing to launch a new window)"
    return 1
  fi
  apple_script="tell application \"$app\" to activate"
  run_osascript_timeout "$apple_script"
}

# Apps that are TERMINALS hosting an agent, not the agent's own application.
# For these the surface is the correct target and raising the app alone is the
# old bug (a window comes forward showing the wrong session, or worse, a new
# one). The distinction matters because `--app` now carries both cases.
is_terminal_host_app() {
  case "$1" in
    cmux|iTerm|iTerm2|Ghostty|WezTerm|kitty|Alacritty|Terminal|Cursor|"Visual Studio Code"|Code)
      return 0 ;;
    *) return 1 ;;
  esac
}

# Focus an agent that lives in a DESKTOP APPLICATION rather than a terminal.
#
# Claude Code and Codex both have desktop apps. A session running there has no
# tty and appears in no cmux tree, so every resolver above misses it and the key
# does nothing at all -- the reported "Claude and Codex apps don't open
# anything". The app name is not guessed here: the hook recorded it from its own
# process ancestry, so this activates the host this specific agent belongs to.
#
# The no-launch guard still applies. Pressing an AGENT key means "take me to
# that session"; if its app has since quit, the session is gone and launching a
# blank app would be a lie. Explicit app keys are a different feature with a
# different rule.
app_focus() {
  local app=$APP_HINT
  [ -n "$app" ] || return 1
  is_terminal_host_app "$app" && return 1
  # The running check comes BEFORE the osascript check: whether to raise a quit
  # app is a policy decision about the agent, and saying "that session is gone"
  # is more useful than saying nothing on a host that has no osascript.
  if ! app_is_running "$app"; then
    error "not focusing $app: it is not running (the session it hosted is gone)"
    return 1
  fi
  command -v osascript >/dev/null 2>&1 || return 1
  run_osascript_timeout "tell application \"$app\" to activate" || return 1
  if ! app_is_frontmost "$app"; then
    error "$app accepted activate but did not become frontmost"
    return 1
  fi
  return 0
}

# The desktop application a source's agent MIGHT be hosted by, when its hook
# never recorded one.
#
# Why this exists: `--app` is written by the hook at session start, so every
# session that began before this build has no app recorded, and a desktop
# Claude/Codex session has no tty and no cmux surface either. Those keys
# resolved to nothing at all and did nothing when pressed. This is the last
# guess before giving up.
#
# It is a GUESS and treated as one. The recorded app is authoritative when
# present; this only runs when it is absent, only after every surface resolver
# has missed, and -- critically -- still refuses to LAUNCH. A running Claude
# means the session plausibly lives there; a quit Claude means it does not.
desktop_app_for_source() {
  case "$1" in
    claude-code) printf 'Claude\n' ;;
    codex-cli|codex) printf 'ChatGPT\n' ;;
    cursor-agent) printf 'Cursor\n' ;;
    *) return 1 ;;
  esac
}


# --- Cursor exact-agent focus -----------------------------------------------
#
# Cursor 3.x's Glass Agents window is not represented in VS Code's ordinary
# ``windowsState.openedWindows`` list, so a workspace path cannot identify the
# chat/tab the user pressed. Cursor's own hook gives us the stronger identity:
# ``conversation_id``. The installed deeplink handler accepts that id and emits
# ``selectAgentRequested`` for the exact agent.
#
# Opening an arbitrary cursor:// URL could launch the app or select nothing, so
# this route fails closed three times: Cursor must already be running, the id
# must name exactly one non-archived local conversation in Cursor's database,
# and Cursor must persist the same id as its selected agent after the request.
CURSOR_CONVERSATION_DB=${CURSOR_CONVERSATION_DB:-$HOME/Library/Application Support/Cursor/User/globalStorage/conversation-search.db}
CURSOR_STATE_DB=${CURSOR_STATE_DB:-$HOME/Library/Application Support/Cursor/User/globalStorage/state.vscdb}

cursor_local_agent_is_live() {
  local id=$1 count
  [ -r "$CURSOR_CONVERSATION_DB" ] || return 1
  command -v sqlite3 >/dev/null 2>&1 || return 1
  # deep_link_for_agent performs the strict UUID check before the id reaches
  # SQL. Besides matching Cursor's real ids, that makes interpolation inert.
  deep_link_for_agent Cursor "$id" >/dev/null 2>&1 || return 1
  count=$(sqlite3 -noheader "$CURSOR_CONVERSATION_DB" \
    "SELECT count(*) FROM conversations WHERE source='local' AND scope='' AND id='$id' AND is_archived=0;" \
    2>/dev/null) || return 1
  [ "$count" = 1 ]
}

cursor_selected_agent_id() {
  [ -r "$CURSOR_STATE_DB" ] || return 1
  command -v sqlite3 >/dev/null 2>&1 || return 1
  sqlite3 -noheader "$CURSOR_STATE_DB" \
    "SELECT CAST(value AS TEXT) FROM ItemTable WHERE key='cursor/glass.selectedAgent';" \
    2>/dev/null
}

cursor_agent_focus() {
  local url selected polls max_polls
  if ! app_is_running Cursor; then
    error "not focusing Cursor: it is not running (the recorded agent is unavailable)"
    return 1
  fi
  url=$(deep_link_for_agent Cursor "$SESSION") || {
    error "not focusing Cursor: hook recorded no valid conversation id"
    return 1
  }
  if ! cursor_local_agent_is_live "$SESSION"; then
    error "not focusing Cursor: local agent $SESSION is missing, archived, or ambiguous"
    return 1
  fi
  if [ "${DIAGNOSE:-0}" = 1 ]; then
    printf 'WOULD RUN: open %s\n' "$url"
    return 0
  fi
  open "$url" >/dev/null 2>&1 || {
    error "Cursor rejected its exact-agent deep link"
    return 1
  }

  polls=0
  max_polls=${CURSOR_FOCUS_POLLS:-40}
  while [ "$polls" -lt "$max_polls" ]; do
    selected=$(cursor_selected_agent_id 2>/dev/null || true)
    if [ "$selected" = "$SESSION" ]; then
      if app_is_frontmost Cursor; then
        return 0
      fi
      error "Cursor selected agent $SESSION but did not become frontmost"
      return 1
    fi
    polls=$((polls + 1))
    [ "$polls" -lt "$max_polls" ] && sleep 0.05
  done
  error "Cursor did not select recorded agent $SESSION; refusing to report success"
  return 1
}


# These desktop apps register a URL scheme that opens a SPECIFIC conversation,
# which is the difference between "the app came forward" and "I am looking at
# the session I pressed":
#
#   Claude:  claude://claude.ai/chat/<uuid>      (support.claude.com, Jun 2026)
#   Codex:   codex://threads/<thread-id>         (learn.chatgpt.com)
#   Cursor:  cursor://anysphere.cursor-deeplink/background-agent?bcId=<uuid>
#            (the installed Cursor 3.x deeplink handler)
#
# The id is only usable when it is the id THAT APP uses. The hook records
# whatever the agent called its session, and for a terminal-hosted CLI that is
# its own session id, which lives in a different namespace from a desktop
# conversation id. Handing the wrong id over is the worst failure available
# here: both apps silently fall back to a recent-conversations list, so the
# press looks like it worked while showing the wrong thing.
#
# So the rule is narrow on purpose: a deep link is attempted ONLY for a session
# the hook positively identified as desktop-hosted (it recorded that app), and
# only when the id looks like that app's own id. Everything else keeps the old
# behaviour of activating the app, which is honest about being approximate.
deep_link_for_agent() {
  local app=$1 id=$2 h4 h8 h12 uuid_id
  [ -n "$id" ] || return 1
  case "$app" in
    Claude)
      # A claude.ai conversation id is a UUID. A Claude Code CLI session id is
      # not, so this check is also what keeps a terminal session from being
      # deep-linked into the desktop chat app.
      #
      # Spelled out rather than using ? wildcards: ? matches ANY character, so
      # a "uuid-shaped" glob would happily accept `xxxxxxxx-xxxx-...`. The
      # whole point of the check is that the id is really hex.
      h4="[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]"
      h8="$h4$h4"
      h12="$h4$h4$h4"
      uuid_id=$id
      case "$id" in
        local_*) uuid_id=${id#local_} ;;
      esac
      case "$uuid_id" in
        $h8-$h4-$h4-$h4-$h12) ;;
        *) return 1 ;;
      esac
      case "$id" in
        local_*) printf 'claude://claude.ai/epitaxy/local_%s\n' "$uuid_id" ;;
        *) printf 'claude://claude.ai/chat/%s\n' "$uuid_id" ;;
      esac
      ;;
    ChatGPT)
      # Codex thread ids are opaque; require something that cannot be confused
      # with a path or a title.
      case "$id" in
        *[!A-Za-z0-9_-]*) return 1 ;;
        "") return 1 ;;
      esac
      printf 'codex://threads/%s\n' "$id"
      ;;
    Cursor)
      # Cursor hook conversation ids and local Glass agent ids are UUIDs. The
      # background-agent route's name is historical: in current Cursor it
      # directly emits selectAgentRequested for this exact local id.
      h4="[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]"
      h8="$h4$h4"
      h12="$h4$h4$h4"
      case "$id" in
        $h8-$h4-$h4-$h4-$h12) ;;
        *) return 1 ;;
      esac
      printf 'cursor://anysphere.cursor-deeplink/background-agent?bcId=%s\n' "$id"
      ;;
    *) return 1 ;;
  esac
}


# Read the selected web route through the same stable, Accessibility-granted
# helper used by the microphone button. Electron apps do not expose a supported
# shell API for their active internal route, but their AX web area does expose
# its URL. Keeping that read-back in the named helper means launchd never needs
# its own broad Accessibility grant.
app_bundle_id() {
  case "$1" in
    Claude) printf 'com.anthropic.claudefordesktop\n' ;;
    ChatGPT) printf 'com.openai.codex\n' ;;
    "T3 Code (Alpha)") printf 'com.t3tools.t3code\n' ;;
    *) return 1 ;;
  esac
}

deckbridge_control() {
  local helper_cli
  helper_cli=${DECKBRIDGE_CONTROL_CLI:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/mic_key.sh}
  [ -x "$helper_cli" ] || return 1
  "$helper_cli" "$@"
}

focus_t3code() {
  local selected polls
  command -v open >/dev/null 2>&1 || return 1
  open -a "T3 Code (Alpha)" >/dev/null 2>&1 || return 1
  if deckbridge_control --helper-press-button com.t3tools.t3code "$NAME" >/dev/null 2>&1; then
    polls=0
    while [ "$polls" -lt 20 ]; do
      selected=$(deckbridge_control --helper-web-url com.t3tools.t3code 2>/dev/null || true)
      case "$selected" in
        *"/$SESSION"|*"/$SESSION/"*)
          printf 'focus_agent: focused exact T3 Code thread %s (verified)\n' "$SESSION"
          return 0
          ;;
      esac
      polls=$((polls + 1))
      sleep 0.05
    done
  fi
  # The local HTTP route is not a safe fallback. Its browser client requires a
  # separate bootstrap pairing credential and cannot inherit the desktop-managed
  # session. Fail closed here instead of stranding the operator on a token prompt.
  error "could not select exact T3 Code thread $SESSION"
  return 1
}

launch_t3code_thread() {
  command -v open >/dev/null 2>&1 || return 1
  open -a "T3 Code (Alpha)" >/dev/null 2>&1 || return 1
  # The app may still be constructing its renderer after a cold launch.
  local polls=0
  while [ "$polls" -lt 30 ]; do
    if deckbridge_control --helper-press-button com.t3tools.t3code "New thread" >/dev/null 2>&1; then
      return 0
    fi
    polls=$((polls + 1))
    sleep 0.1
  done
  error "T3 Code opened but its New thread control was unavailable"
  return 1
}

app_selected_url() {
  local app=$1 bundle helper_cli
  bundle=$(app_bundle_id "$app") || return 1
  helper_cli=${DECKBRIDGE_CONTROL_CLI:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/mic_key.sh}
  [ -x "$helper_cli" ] || return 1
  "$helper_cli" --helper-web-url "$bundle"
}

selected_url_matches_session() {
  local app=$1 id=$2 selected=$3 clean last
  [ -n "$selected" ] || return 1
  clean=${selected%%\?*}
  clean=${clean%%\#*}
  clean=${clean%/}
  last=${clean##*/}
  [ "$last" = "$id" ] || return 1
  case "$app:$id:$clean" in
    Claude:local_*:*/epitaxy/local_*) return 0 ;;
    Claude:*:*/chat/*) return 0 ;;
    ChatGPT:*:*/local/*|ChatGPT:*:*/threads/*) return 0 ;;
    *) return 1 ;;
  esac
}

# Focus the exact conversation and require selected-route read-back. Return 2
# only when this app/session has no supported exact deep link; return 1 after
# any attempted exact focus fails so callers cannot disguise it as success by
# merely raising the app.
app_focus_deep() {
  local url selected polls max_polls blank_polls max_blank_polls route_id
  route_id=$SESSION
  # Claude's desktop Claude Code surface stores these as /epitaxy/local_UUID,
  # while its hook reports the underlying UUID. This conversion is allowed
  # only when that hook positively named Claude as its non-terminal host.
  if [ "$APP_HINT" = Claude ] && [ "$SOURCE" = claude-code ]; then
    case "$route_id" in local_*) ;; *) route_id=local_$route_id ;; esac
  fi
  url=$(deep_link_for_agent "$APP_HINT" "$route_id") || return 2
  if ! app_is_running "$APP_HINT"; then
    error "not focusing $APP_HINT: it is not running (the recorded session is unavailable)"
    return 1
  fi
  if [ "${DIAGNOSE:-0}" = 1 ]; then
    printf 'WOULD RUN: open %s\n' "$url"
    return 0
  fi
  open "$url" >/dev/null 2>&1 || {
    error "deep link failed for $APP_HINT"
    return 1
  }

  polls=0
  blank_polls=0
  max_polls=${APP_FOCUS_POLLS:-40}
  case "$max_polls" in ''|*[!0-9]*) max_polls=40 ;; esac
  # A non-empty but mismatched route may simply be the old task while the deep
  # link loads, so it gets the full verification window. Repeatedly receiving
  # no AX route at all means this app build does not expose read-back; waiting
  # several seconds cannot improve it and only piles up focus workers.
  max_blank_polls=${APP_FOCUS_BLANK_POLLS:-3}
  case "$max_blank_polls" in ''|*[!0-9]*|0) max_blank_polls=3 ;; esac
  while [ "$polls" -lt "$max_polls" ]; do
    selected=$(app_selected_url "$APP_HINT" 2>/dev/null || true)
    if selected_url_matches_session "$APP_HINT" "$route_id" "$selected"; then
      if app_is_frontmost "$APP_HINT"; then
        printf 'focus_agent: focused exact %s session %s (verified)\n' "$APP_HINT" "$SESSION"
        return 0
      fi
      error "$APP_HINT selected session $SESSION but did not become frontmost"
      return 1
    fi
    if [ -z "$selected" ]; then
      blank_polls=$((blank_polls + 1))
      [ "$blank_polls" -lt "$max_blank_polls" ] || break
    else
      blank_polls=0
    fi
    polls=$((polls + 1))
    [ "$polls" -lt "$max_polls" ] && sleep 0.05
  done
  if [ -z "${selected:-}" ]; then
    error "$APP_HINT selected-session read-back is unavailable; refusing to report success"
  else
    error "$APP_HINT selected ${selected##*/}, not recorded session $route_id; refusing to report success"
  fi
  return 1
}


# Current Codex desktop and Codex CLI share the same local rollout/thread UUID
# namespace. If a terminal tab has disappeared while its rollout still exists,
# the desktop app can recover the exact thread. This is deliberately Codex-only
# and existence-gated: Claude's CLI and desktop-local IDs are not interchangeable.
CODEX_SESSION_ROOT=${CODEX_SESSION_ROOT:-$HOME/.codex/sessions}

codex_thread_is_local() {
  local id=$1 found
  [ -d "$CODEX_SESSION_ROOT" ] || return 1
  deep_link_for_agent ChatGPT "$id" >/dev/null 2>&1 || return 1
  found=$(find "$CODEX_SESSION_ROOT" -type f -name "*-$id.jsonl" -print -quit 2>/dev/null || true)
  [ -n "$found" ]
}

codex_app_recovery_focus() {
  local saved_app rc
  [ "$SOURCE" = "codex-cli" ] || return 1
  codex_thread_is_local "$SESSION" || return 1
  saved_app=$APP_HINT
  APP_HINT=ChatGPT
  app_focus_deep
  rc=$?
  APP_HINT=$saved_app
  if [ "$rc" -eq 0 ]; then
    printf 'focus_agent: recovered exact Codex thread %s in desktop app (verified)\n' "$SESSION"
    return 0
  fi
  return 1
}


focus_agent() {
  local herdr_rc deep_rc
  printf 'focus_agent: source=%s name=%s cwd=%s app=%s\n' \
    "$SOURCE" "$NAME" "$CWD" "${APP_HINT:-<none>}"
  if [ -n "$HERDR_PANE_HINT" ]; then
    if herdr_focus; then
      printf 'focus_agent: focused Herdr pane %s (verified)\n' "$HERDR_PANE_HINT"
      return 0
    else
      herdr_rc=$?
    fi
    # A process can inherit HERDR_PANE, later move into cmux, and keep reporting
    # the old pane after Herdr has deleted it.  In that case an exact, live cmux
    # surface is stronger evidence than the stale environment variable.  Do
    # not continue to cwd/process/app guesses: only the recorded cmux identity
    # is allowed to recover this branch, and cmux_focus verifies the landing.
    if [ "$herdr_rc" -eq 2 ]; then
      if { is_cmux_ref "$SURFACE_HINT" || is_cmux_uuid "$SURFACE_HINT"; } && \
         cmux_surface_matches_tty "$SURFACE_HINT" "$TTY_HINT"; then
        error "recorded Herdr pane $HERDR_PANE_HINT is unavailable; trying recorded cmux surface $SURFACE_HINT verified on tty $TTY_HINT"
        if cmux_focus; then
          printf 'focus_agent: focused exact cmux surface after stale Herdr identity\n'
          return 0
        fi
      elif [ -n "$SURFACE_HINT" ]; then
        error "recorded Herdr pane $HERDR_PANE_HINT is unavailable, and cmux surface $SURFACE_HINT no longer belongs to agent tty ${TTY_HINT:-<none recorded>}"
      fi
      if cmux_focus_recorded_tty; then
        printf 'focus_agent: focused exact cmux tty %s after stale Herdr/surface identity\n' "$TTY_HINT"
        return 0
      fi
      if codex_app_recovery_focus; then
        return 0
      fi
      error "recorded Herdr pane $HERDR_PANE_HINT is unavailable and tty ${TTY_HINT:-<none recorded>} identifies no unique cmux surface; refusing wrong-tab focus"
      return 1
    fi
    error "could not focus recorded Herdr pane $HERDR_PANE_HINT; refusing fallbacks"
    return 1
  fi
  # A native Cursor Agent belongs to an exact Glass conversation, not merely
  # to Cursor.app or an editor workspace. Activating the app can expose the
  # last-used agent, so this exact-conversation route is authoritative and
  # weaker fallbacks are forbidden.
  # Cursor CLI running *inside a terminal* records that terminal plus a tty and
  # continues through the terminal resolver below instead.
  if [ "$SOURCE" = "cursor-agent" ] && \
     { [ "$APP_HINT" = "Cursor" ] || { [ -z "$APP_HINT" ] && [ -z "$TTY_HINT" ]; }; }; then
    if cursor_agent_focus; then
      printf 'focus_agent: focused exact Cursor agent %s\n' "$SESSION"
      return 0
    fi
    error "could not focus the exact Cursor agent; refusing app and terminal fallbacks"
    return 1
  fi
  # An app-hosted agent is checked FIRST when the hook named a non-terminal
  # host: for that agent there is no surface to find, and letting cmux_focus run
  # first risks matching some unrelated surface by basename.
  if [ -n "$APP_HINT" ] && ! is_terminal_host_app "$APP_HINT"; then
    # Try for the exact conversation before settling for the app. Once an
    # exact link was attempted, a failed read-back is final: merely activating
    # the app would show an arbitrary last-used tab and lie about success.
    if app_focus_deep; then
      return 0
    else
      deep_rc=$?
    fi
    if [ "$deep_rc" -ne 2 ]; then
      error "recorded $APP_HINT session could not be selected exactly; refusing app-only fallback"
      return 1
    fi
    if app_focus; then
      printf 'focus_agent: activated host app %s\n' "$APP_HINT"
      return 0
    fi
    # The hook's recorded desktop host is authoritative. If it cannot be
    # focused (most importantly because it quit), this session is unavailable;
    # a cwd match in cmux would be an unrelated terminal tab, not a fallback.
    error "recorded host app $APP_HINT could not be focused; refusing terminal fallbacks"
    return 1
  fi
  if cmux_focus; then
    printf 'focus_agent: focused via cmux\n'
    return 0
  fi
  if tmux_focus; then
    printf 'focus_agent: focused via tmux pane\n'
    return 0
  fi
  if process_focus; then
    printf 'focus_agent: focused process terminal\n'
    return 0
  fi
  # A Codex thread can outlive the terminal tab that launched it. Prefer the
  # shared local thread in Codex desktop, but only with exact selected-route
  # read-back; app activation alone is forbidden here.
  if codex_app_recovery_focus; then
    return 0
  fi
  # Nothing matched a terminal surface. Before the generic fallback, try the
  # desktop app this source plausibly runs in.
  #
  # This is the "Claude and ChatGPT keys don't open at all" case: a desktop
  # session has no tty and no cmux surface, and if its hook ran before --app
  # existed there is no recorded app either, so every branch above misses and
  # the key silently does nothing. Guessing the app from the source is weaker
  # evidence than a recorded one, so it goes last and still refuses to launch.
  #
  # Gated on having NO tty, which keeps an older guard intact: the desktop
  # Claude app is not Claude Code. A CLI session in a terminal HAS a tty, so a
  # surface lookup that merely failed must not be rescued by activating an
  # unrelated chat app -- that would show the wrong window and claim success.
  # No tty at all is the actual signature of a desktop-hosted session.
  #
  # ALSO gated on cmux having no surfaces. "No tty recorded" is the absence of
  # evidence, not evidence of absence: a hook written by an older shim records
  # no tty even for a cmux-hosted agent. When cmux is running with live
  # surfaces, the session is far more likely to be one of them than to be a
  # desktop chat window, so guessing is refused and the failure is reported.
  # This is the "cmux Claude/Codex tabs open the native app instead" bug.
  if [ -z "$APP_HINT" ] && [ -z "$TTY_HINT" ]; then
    local guessed
    if guessed=$(desktop_app_for_source "$SOURCE"); then
      if cmux_has_surfaces; then
        # A running desktop app is not evidence that THIS unrecorded session
        # lives there. The live cmux tree is stronger evidence that an old hook
        # omitted the tty, so refuse instead of switching to an unrelated chat.
        error "cmux has live surfaces; cannot identify this session or assume $guessed hosts it"
        error "restart the agent so its hook records a tty or app, then press again"
      elif app_is_running "$guessed"; then
        if APP_HINT=$guessed app_focus; then
          printf 'focus_agent: activated %s (guessed from source; hook recorded no app or tty)\n' "$guessed"
          return 0
        fi
      else
        printf 'focus_agent: %s is not running; not guessing it hosts this session\n' "$guessed"
      fi
    fi
  fi
  if last_resort_focus; then
    printf 'focus_agent: activated fallback application\n'
    return 0
  fi
  error 'no focus method succeeded (this host may not be macOS, or permissions/tools are missing)'
  if is_cmux_hosted_source "$SOURCE"; then
    error "re-run with FOCUS_DEBUG=1 to see which cmux surfaces were considered"
    # Two checkouts of this repo can coexist (a tarball extract and a clone),
    # and a press that runs the older one looks identical to a logic bug.
    error "this script: ${BASH_SOURCE[0]} (build $BUILD_STAMP)"
    error "agent tty was: ${TTY_HINT:-<none recorded>}"
    error "agent app was: ${APP_HINT:-<none recorded>}"
  fi
  return 1
}

# `--diagnose` answers the only question that matters when a press misses:
# what does cmux actually report, and which surface (if any) matches this agent.
diagnose() {
  local py listing agent_tty ref dl raw herdr_target herdr_workspace herdr_tab
  printf 'script: %s (build %s)\n' "${BASH_SOURCE[0]}" "$BUILD_STAMP"
  printf 'source=%s name=%s cwd=%s session=%s\n' "$SOURCE" "$NAME" "$CWD" "$SESSION"
  if py=$(python_bin); then
    printf 'python: %s\n' "$py"
  else
    printf 'python: NONE FOUND -- the resolver cannot run; set FOCUS_PYTHON\n'
  fi
  # The exact-tab question, answered before a key is ever pressed. Printed
  # BEFORE the cmux check, which returns early: a desktop-hosted Claude or
  # Codex session has nothing to do with cmux, so "cmux not on PATH" must not
  # suppress the one line that explains whether its tab can be reached.
  if [ -z "$SESSION" ]; then
    printf 'deep link:      no -- no session id recorded (restart the agent so its hook records one)\n'
  elif dl=$(deep_link_for_agent "${APP_HINT:-}" "$SESSION" 2>/dev/null); then
    printf 'deep link:      %s\n' "$dl"
  elif [ -n "$APP_HINT" ] && is_terminal_host_app "$APP_HINT"; then
    printf 'deep link:      n/a -- terminal-hosted (%s); the surface resolver handles this\n' "$APP_HINT"
  else
    printf 'deep link:      no -- session %s is not a valid id for app %s; will raise the app instead\n' \
      "$SESSION" "${APP_HINT:-<none>}"
  fi
  printf 'app recorded:   %s\n' "${APP_HINT:-<none -- hook predates --app, or a terminal-hosted agent>}"
  if [ -n "$HERDR_PANE_HINT" ]; then
    raw=$(herdr pane get "$HERDR_PANE_HINT" 2>/dev/null || true)
    herdr_target=$(herdr_pane_id_from_json "$raw" || true)
    herdr_workspace=$(herdr_pane_field_from_json "$raw" workspace_id || true)
    herdr_tab=$(herdr_pane_field_from_json "$raw" tab_id || true)
    printf 'Herdr recorded: %s\n' "$HERDR_PANE_HINT"
    printf 'Herdr resolved: pane=%s workspace=%s tab=%s\n' \
      "${herdr_target:-<missing>}" "${herdr_workspace:-<missing>}" "${herdr_tab:-<missing>}"
    [ -n "$herdr_target" ] && return 0
    return 1
  fi
  if command -v cmux >/dev/null 2>&1; then
    printf 'cmux: %s (%s)\n' "$(command -v cmux)" "$(cmux version 2>&1 | head -1)"
  else
    printf 'cmux: NOT ON PATH -- every terminal-hosted press will fall through\n'
    return 1
  fi
  agent_tty=$TTY_HINT
  printf 'tty from --tty: %s\n' "${TTY_HINT:-<not supplied>}"
  if [ -n "$APP_HINT" ]; then
    if app_is_running "$APP_HINT"; then
      printf 'app running:    yes\n'
    else
      printf 'app running:    NO -- an agent key will refuse (that session is gone)\n'
    fi
  else
    local guess
    guess=$(desktop_app_for_source "$SOURCE" || true)
    if [ -n "$guess" ]; then
      if app_is_running "$guess"; then
        printf 'app fallback:   %s (running) -- used when no surface matches\n' "$guess"
      else
        printf 'app fallback:   %s (not running)\n' "$guess"
      fi
    fi
  fi
  [ -n "$agent_tty" ] || agent_tty=$(tty_for_cwd "$CWD" 2>/dev/null || true)
  printf 'tty resolved:   %s\n' "${agent_tty:-<none found>}"
  listing=$(cmux tree --all --json 2>&1 || true)
  if [ -z "$listing" ]; then
    printf 'cmux tree: EMPTY\n'
    return 1
  fi
  printf 'cmux surfaces:\n'
  CMUX_JSON=$listing "${py:-python3}" -c '
import json, os, sys
try:
    doc = json.loads(os.environ["CMUX_JSON"])
except Exception as exc:
    print("  unparseable: %s" % exc); sys.exit(0)
def walk(n):
    if isinstance(n, dict):
        r = n.get("ref")
        if isinstance(r, str) and r.startswith("surface:"):
            print("  %-12s tty=%-9s title=%r" % (r, n.get("tty"), n.get("title")))
        for v in n.values(): walk(v)
    elif isinstance(n, list):
        for v in n: walk(v)
walk(doc)
' 2>&1
  ref=$(cmux_resolve_surface "$listing" "$CWD" "$agent_tty" 2>&1 || true)
  printf 'resolved: %s\n' "${ref:-<no match>}"
}

# Open a Discord link in the DESKTOP APP rather than a browser tab.
#
# `open https://discord.com/channels/...` hands the URL to the default browser,
# which lands on a Chrome window showing the web client -- reported as
# "unusable". The desktop app registers a `discord://` scheme, so the https URL
# is rewritten to it:
#
#   https://discord.com/channels/<guild>/<channel>[/<message>]
#   discord://-/channels/<guild>/<channel>[/<message>]
#
# The `-` is the app's own placeholder host and is required; `discord://channels/...`
# is a different, mostly-broken route.
#
# Evidence status: UNOFFICIAL. Discord publishes no protocol reference. The
# route is community-documented and reported working on macOS:
#   https://gist.github.com/ghostrider-05/8f1a0bfc27c7c4509b4ea4e8ce718af0
# Hence the fallback below rather than trusting it outright.
discord_deep_link() {
  local url=$1
  case "$url" in
    discord://*) printf '%s\n' "$url"; return 0 ;;
  esac
  # Accept every host Discord has shipped over the years.
  case "$url" in
    https://discord.com/channels/*|https://discordapp.com/channels/*|https://ptb.discord.com/channels/*|https://canary.discord.com/channels/*)
      printf 'discord://-/channels/%s\n' "${url#*/channels/}"
      return 0 ;;
  esac
  # Anything else (an invite, a profile, a non-Discord URL) is passed through
  # untouched: guessing a scheme for a route we have not verified would trade a
  # working browser tab for a dead link.
  printf '%s\n' "$url"
  return 0
}

# True when the Discord desktop app is actually installed. Without this a
# machine with no Discord app would get a silent no-op from the deep link,
# which is strictly worse than the browser tab it replaced.
discord_app_present() {
  [ -n "${DISCORD_APP_PATH:-}" ] && [ -e "$DISCORD_APP_PATH" ] && return 0
  [ -d "/Applications/Discord.app" ] && return 0
  [ -d "$HOME/Applications/Discord.app" ] && return 0
  command -v pgrep >/dev/null 2>&1 && pgrep -f 'Discord.app' >/dev/null 2>&1 && return 0
  return 1
}

open_discord_url() {
  local url=$1 deep
  [ -n "$url" ] || { error "no Discord URL to open"; return 2; }
  if ! discord_app_present; then
    # No app: the browser is the only thing that can serve this, so use it
    # rather than firing a scheme nothing handles.
    open "$url"
    return $?
  fi
  deep=$(discord_deep_link "$url")
  if [ "$deep" != "$url" ]; then
    # Name the handler explicitly. `open discord://...` can deliver the route
    # to an already-running Discord process without activating it, which makes
    # a deck press appear to do nothing while the user remains in another app.
    # `-a Discord` both routes the deep link and brings its target app forward.
    open -a Discord "$deep" 2>/dev/null && return 0
    # The scheme is unofficial. If the OS has no handler for it, `open` fails
    # and the browser still gets its chance -- degraded, not broken.
    error "discord:// route failed; falling back to the browser"
  fi
  open "$url"
  return $?
}

# Open an application by name, LAUNCHING it if it is not running.
#
# This is the one place in the script where launching is correct, and the
# distinction is intent, not capability. Pressing an explicit "Claude" key says
# "I want Claude"; there is no session to be wrong about. Pressing an AGENT key
# says "take me to that running session", and launching a blank app there is a
# lie about state -- which is exactly the phantom-window bug. Two code paths,
# opposite rules, on purpose.
#
# `open -a` is used rather than AppleScript `activate` because it reports a
# missing application as a nonzero exit instead of silently doing nothing.
launch_app() {
  local app=$1
  [ -n "$app" ] || { error "--launch requires an application name"; return 2; }
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'WOULD RUN: open -a %s\n' "$app"
    return 0
  fi
  command -v open >/dev/null 2>&1 || { error "open is unavailable (not macOS?)"; return 1; }
  open -a "$app" 2>/dev/null && return 0
  error "could not open $app (no such application?)"
  return 1
}

main() {
  parse_args "$@" || return $?
  [ "$HELP" -eq 1 ] && return 0
  if [ -n "$LAUNCH_APP" ]; then
    launch_app "$LAUNCH_APP"
    return $?
  fi
  if [ "$LAUNCH_T3CODE" -eq 1 ]; then
    launch_t3code_thread
    return $?
  fi
  if [ "${DIAGNOSE:-0}" -eq 1 ]; then
    diagnose
    return $?
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    print_dry_run
    return 0
  fi
  if [ "$SOURCE" = "hermes-discord" ]; then
    command -v open >/dev/null 2>&1 || { error "open is unavailable; cannot open Discord URL"; return 1; }
    open_discord_url "$URL"
    return $?
  fi
  case "$SOURCE" in
    t3code|t3code-*) focus_t3code; return $? ;;
  esac
  if [ -n "$HERDR_PANE_HINT" ]; then
    # An exact pane supplied by the connector is stronger than the legacy
    # Hermes host scan. The latter understands tmux/cmux but not a raw SSH
    # process inside Herdr, which was why a visible tab still looked missing.
    focus_agent
    return $?
  fi
  if [ "$SOURCE" = "hermes-ssh" ]; then
    # Deliberately do NOT fall through to the generic terminal-raising chain:
    # raising an arbitrary terminal (or launching one) is what opened a blank
    # zsh window instead of the agent. Failing loudly is more useful.
    focus_hermes_ssh && return 0
    error "no ssh pane or cmux surface for host $HERMES_SSH_HOST; set HERMES_SSH_FOCUS_CMD to override"
    return 1
  fi
  focus_agent
}

if [ "${FOCUS_AGENT_LIB_ONLY:-0}" -ne 1 ]; then
  main "$@"
  exit $?
fi
