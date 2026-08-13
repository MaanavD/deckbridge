#!/usr/bin/env bash
# deckbridge.sh - run the whole deckbridge stack with one command.
#
#   ./deckbridge.sh start          # hub + connectors + watchers + emulator
#   ./deckbridge.sh start --hw     # same, plus the physical Stream Deck renderer
#   ./deckbridge.sh start --fg     # run in the foreground; ctrl-c stops all
#   ./deckbridge.sh status         # what is alive, what claimed which keys
#   ./deckbridge.sh health         # machine-checkable required-child health
#   ./deckbridge.sh stop
#   ./deckbridge.sh restart
#   ./deckbridge.sh logs [name]    # tail a component log (default: all)
#   ./deckbridge.sh doctor         # check prerequisites without starting
#
# Everything is optional except the hub. Components whose prerequisites are
# missing are skipped with a reason rather than failing the whole start, so a
# Mac with no Stream Deck, no SSH alias, and no Discord token still gets a
# working emulator board fed by local agent hooks.
#
# Config lives in ./deckbridge.conf (see deckbridge.conf.example). Environment
# variables of the same name win over the file, so one-off overrides work:
#   HERMES_SSH=hetzner ./deckbridge.sh start
set -uo pipefail
cd "$(dirname "$0")"

RUN_DIR="${DECKBRIDGE_RUN_DIR:-.run}"
LOG_DIR="${DECKBRIDGE_LOG_DIR:-logs}"

# ---- defaults, overridable by deckbridge.conf or the environment ------------
WS_PORT="${WS_PORT:-8777}"
HTTP_PORT="${HTTP_PORT:-8080}"
KEYS="${KEYS:-15}"
AGENT_CLAIM="${AGENT_CLAIM:-0 13}"    # sessions 0-9 + fixed shortcuts 10-13
MIC_KEY="${MIC_KEY:--1}"              # -1 disables; 14 = bottom-right corner
MAX_AGE_HOURS="${MAX_AGE_HOURS:-24}"  # agents untouched this long drop off
HERMES_SSH="${HERMES_SSH:-}"          # ssh alias for the remote Hermes probe
DISCORD_CHANNEL_ID="${DISCORD_CHANNEL_ID:-}"
DISCORD_GUILD_ID="${DISCORD_GUILD_ID:-}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"

# The mic key only makes sense on macOS, where the dictation hotkey and
# frontmost-app detection exist.  Default it on there and off elsewhere so the
# emulator does not show a key that cannot work.
if [ "$MIC_KEY" = "-1" ]; then
  if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then MIC_KEY=14; else MIC_KEY=""; fi
fi

[ -f deckbridge.conf ] && . ./deckbridge.conf

# Every transport publishes the same tiny health contract here.  Keep this
# outside .run: temporary runtime directories are replaced during upgrades,
# while last-success timestamps must survive long enough to reveal a stale
# external feed after a restart.
HEALTH_DIR="${DECKBRIDGE_HEALTH_DIR:-$HOME/.deckbridge/health}"
export DECKBRIDGE_HEALTH_DIR="$HEALTH_DIR"

PY=python3
[ -x .venv/bin/python3 ] && PY=.venv/bin/python3

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
ok()   { printf '  %s+%s %s\n' "$c_grn" "$c_off" "$*"; }
skip() { printf '  %s~%s %s\n' "$c_yel" "$c_off" "$*"; }
bad()  { printf '  %s!%s %s\n' "$c_red" "$c_off" "$*"; }
dim()  { printf '%s%s%s\n' "$c_dim" "$*" "$c_off"; }

pidfile() { echo "$RUN_DIR/$1.pid"; }

alive() {  # alive NAME
  local f; f=$(pidfile "$1")
  [ -f "$f" ] || return 1
  local p; p=$(cat "$f" 2>/dev/null) || return 1
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null
}

process_cwd() {  # process_cwd PID
  local lsof_bin
  lsof_bin=$(command -v lsof 2>/dev/null) || return 1
  "$lsof_bin" -a -p "$1" -d cwd -Fn 2>/dev/null \
    | sed -n 's/^n//p' | head -n 1
}

find_existing_renderer() {
  # A renderer owns an exclusive USB device. During installation an existing
  # hand-started copy must be adopted, not raced with a second opener. Restrict
  # adoption to this checkout, this hub URL, and exactly one process.
  local p cwd command found="" count=0
  command -v pgrep >/dev/null 2>&1 || return 1
  for p in $(pgrep -f 'renderer_hw\.py' 2>/dev/null); do
    kill -0 "$p" 2>/dev/null || continue
    cwd=$(process_cwd "$p" || true)
    [ "$cwd" = "$PWD" ] || continue
    command=$(ps -p "$p" -o command= 2>/dev/null || true)
    case "$command" in
      *renderer_hw.py*"ws://127.0.0.1:$WS_PORT"*) ;;
      *renderer_hw.py*)
        # No --ws means renderer_hw's documented default.
        [ "$WS_PORT" = 8777 ] && [[ "$command" != *"--ws"* ]] || continue
        ;;
      *) continue ;;
    esac
    found="$p"; count=$((count + 1))
  done
  [ "$count" = 1 ] || return 1
  printf '%s\n' "$found"
}

find_existing_emulator() {
  # Only adopt Python's static server when it is rooted in this checkout. An
  # arbitrary server that happens to answer emulator.html remains external.
  local p cwd command found="" count=0
  command -v pgrep >/dev/null 2>&1 || return 1
  for p in $(pgrep -f 'http\.server' 2>/dev/null); do
    kill -0 "$p" 2>/dev/null || continue
    cwd=$(process_cwd "$p" || true)
    command=$(ps -p "$p" -o command= 2>/dev/null || true)
    grep -Eq -- "-m[[:space:]]+http\.server[[:space:]]+$HTTP_PORT([[:space:]]|$)" \
      <<<"$command" || continue
    if [ "$cwd" != "$PWD" ]; then
      grep -Fq -- "--directory $PWD" <<<"$command" || continue
    fi
    found="$p"; count=$((count + 1))
  done
  [ "$count" = 1 ] || return 1
  printf '%s\n' "$found"
}

adopt() {  # adopt NAME PID
  local name="$1" p="$2"
  kill -0 "$p" 2>/dev/null || return 1
  mkdir -p "$RUN_DIR" "$LOG_DIR"
  printf '%s\n' "$p" >"$(pidfile "$name")"
  ok "adopted existing $name (pid $p)"
}

spawn() {  # spawn NAME COMMAND...
  local name="$1"; shift
  if alive "$name"; then skip "$name already running (pid $(cat "$(pidfile "$name")"))"; return 0; fi
  mkdir -p "$RUN_DIR" "$LOG_DIR"
  "$@" >>"$LOG_DIR/$name.log" 2>&1 &
  local p=$!
  echo "$p" > "$(pidfile "$name")"
  sleep 0.4
  if kill -0 "$p" 2>/dev/null; then
    ok "$name (pid $p)"
  else
    bad "$name died instantly; see $LOG_DIR/$name.log"
    tail -n 5 "$LOG_DIR/$name.log" | sed 's/^/      /'
    rm -f "$(pidfile "$name")"
    return 1
  fi
}

emulator_served_here() {  # emulator_served_here PORT
  # Does the server on PORT actually serve THIS checkout?
  #
  # Asks for a file only this directory has, and checks the body rather than
  # just the status code: a different deckbridge extract would also answer 200
  # for emulator.html while serving stale JavaScript. The build marker below is
  # bumped whenever the emulator's wire contract changes.
  $PY - "$1" <<'EOF' 2>/dev/null
import sys, urllib.request
port = int(sys.argv[1])
try:
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/emulator.html", timeout=1.5) as r:
        if r.status != 200:
            sys.exit(1)
        body = r.read().decode("utf-8", "replace")
except Exception:
    sys.exit(1)
sys.exit(0 if "deckbridge-emulator-build:" in body else 1)
EOF
}

port_open() {  # port_open PORT
  $PY - "$1" <<'EOF' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(0.5)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
EOF
}

wait_port() {  # wait_port PORT SECONDS
  local i=0 max="${2:-10}"
  while [ "$i" -lt "$((max * 4))" ]; do
    port_open "$1" && return 0
    sleep 0.25; i=$((i + 1))
  done
  return 1
}

have_dep() { $PY -c "import $1" 2>/dev/null; }

# The Hermes keys (H and S) exist only if the watcher runs, and the watcher
# runs only with an ssh alias. Leaving that to an env var meant a default
# `./deckbridge.sh start` silently produced a deck with no Hermes or Discord
# agents at all, which reads as "Discord is broken" rather than "unconfigured".
# Pick the alias out of ~/.ssh/config instead of requiring it to be typed.
detect_hermes_ssh() {
  [ -f "$HOME/.ssh/config" ] || return 1
  local alias
  for alias in hermes hetzner; do
    # Match the alias as a whole word in a Host line: `Host hermes` and
    # `Host hermes hermes-old` both count, `Host hermesque` does not.
    if grep -qiE "^[[:space:]]*Host([[:space:]]+[^[:space:]]+)*[[:space:]]+${alias}([[:space:]]|$)" \
         "$HOME/.ssh/config" 2>/dev/null; then
      echo "$alias"; return 0
    fi
  done
  return 1
}

read_remote_env() {  # read_remote_env SSH_HOST KEY
  # Hermes already owns the Discord gateway configuration.  Reuse it over the
  # same SSH link as the agent probe instead of copying a bot token onto every
  # Mac that runs deckbridge.  Keep the accepted keys explicit so no caller can
  # turn this into an arbitrary remote-file reader.
  local host="${1:-}" key="${2:-}" value
  [ -n "$host" ] || return 1
  case "$key" in
    DISCORD_BOT_TOKEN|DISCORD_HOME_CHANNEL|DISCORD_GUILD_ID) ;;
    *) return 1 ;;
  esac
  value=$(ssh -o BatchMode=yes -o ConnectTimeout=6 "$host" \
    "sed -n 's/^${key}=//p' ~/.hermes/.env | head -n 1" 2>/dev/null) || return 1
  value=${value%$'\r'}
  case "$value" in
    \"*\") value=${value#\"}; value=${value%\"} ;;
    \'*\') value=${value#\'}; value=${value%\'} ;;
  esac
  [ -n "$value" ] || return 1
  printf '%s\n' "$value"
}

read_token() {  # echo the Discord token from env, local file, or remote Hermes
  local remote_host="${1:-}"
  if [ -n "${DISCORD_BOT_TOKEN:-}" ]; then echo "$DISCORD_BOT_TOKEN"; return 0; fi
  local f="$HOME/.hermes/.env"
  if [ -f "$f" ]; then
    local v
    v=$(grep -m1 '^DISCORD_BOT_TOKEN=' "$f" 2>/dev/null | cut -d= -f2- | tr -d '"'"'"'' | tr -d '\r')
    if [ -n "$v" ]; then echo "$v"; return 0; fi
  fi
  read_remote_env "$remote_host" DISCORD_BOT_TOKEN
}

# ---------------------------------------------------------------- doctor -----
doctor() {
  echo "deckbridge doctor"
  echo ""
  echo "python: $($PY --version 2>&1) ($PY)"
  have_dep websockets && ok "websockets (required)" || bad "websockets MISSING: pip install -r requirements.txt"
  have_dep PIL && ok "pillow (physical deck only)" || skip "pillow absent; --hw unavailable"
  have_dep StreamDeck && ok "streamdeck (physical deck only)" || skip "streamdeck absent; --hw unavailable"
  echo ""
  port_open "$WS_PORT"   && skip "port $WS_PORT already in use"   || ok "port $WS_PORT free (hub)"
  port_open "$HTTP_PORT" && skip "port $HTTP_PORT already in use" || ok "port $HTTP_PORT free (emulator)"
  echo ""
  # Same autodetection as start(), so doctor reports what start will do rather
  # than what the environment happens to say.
  local dssh="$HERMES_SSH"
  [ -z "$dssh" ] && dssh=$(detect_hermes_ssh || true)
  if [ -n "$dssh" ]; then
    if ssh -o BatchMode=yes -o ConnectTimeout=6 "$dssh" true 2>/dev/null; then
      ok "ssh $dssh reachable (Hermes Discord + ssh agents)"
    else
      bad "ssh $dssh failed; Hermes Discord and ssh keys will stay dark"
    fi
  else
    bad "no ssh alias for the Hermes host; Hermes Discord and ssh keys stay dark"
    echo "     add a Host block for 'hermes' to ~/.ssh/config, or set HERMES_SSH"
  fi
  local dchannel="$DISCORD_CHANNEL_ID"
  [ -z "$dchannel" ] && dchannel=$(read_remote_env "$dssh" DISCORD_HOME_CHANNEL || true)
  if read_token "$dssh" >/dev/null; then
    [ -n "$dssh" ] && [ -z "${DISCORD_BOT_TOKEN:-}" ] && \
      ! grep -q '^DISCORD_BOT_TOKEN=' "$HOME/.hermes/.env" 2>/dev/null \
      && ok "Discord token found on $dssh" || ok "Discord token found"
  else
    skip "no Discord token; approval key disabled"
  fi
  if [ -n "$dchannel" ]; then
    [ -z "$DISCORD_CHANNEL_ID" ] && ok "Discord channel found on $dssh" || ok "DISCORD_CHANNEL_ID set"
  else
    skip "DISCORD_CHANNEL_ID unset; approval key disabled"
  fi
  echo ""
  for f in ~/.claude/settings.json ~/.codex/hooks.json ~/.codex/config.toml ~/.cursor/hooks.json; do
    if [ -f "$f" ] && grep -q '_shim.py' "$f" 2>/dev/null; then
      ok "hooks installed in $f"
    fi
  done
  grep -lq '_shim.py' ~/.claude/settings.json ~/.codex/hooks.json ~/.codex/config.toml ~/.cursor/hooks.json 2>/dev/null \
    || skip "no agent hooks found; run: $PY install_hooks.py --apply"
}

# ----------------------------------------------------------------- start -----
start() {
  local hw=0 fg=0
  for a in "$@"; do
    case "$a" in
      --hw|--hardware) hw=1 ;;
      --fg|--foreground) fg=1 ;;
      *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
  done

  if ! have_dep websockets; then
    bad "missing 'websockets'. python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt"
    exit 1
  fi
  mkdir -p "$RUN_DIR" "$LOG_DIR"

  echo "starting deckbridge"

  # 1. hub, the only hard requirement
  if port_open "$WS_PORT" && ! alive deckd; then
    bad "port $WS_PORT is in use by something else; set WS_PORT or stop it"
    exit 1
  fi
  spawn deckd "$PY" deckd.py --keys "$KEYS" --port "$WS_PORT" || exit 1
  if ! wait_port "$WS_PORT" 10; then
    bad "hub never opened port $WS_PORT; see $LOG_DIR/deckd.log"
    exit 1
  fi

  # T3 Code owns its thread lifecycle and publishes exact approval/input state.
  # Start the feed before the connector so the first board paint can include it.
  spawn t3code_watcher "$PY" t3code_watcher.py

  # 2. unified agents/launchers own 0-9; fixed app shortcuts own 10-13
  # shellcheck disable=SC2086
  spawn connector_agents "$PY" connector_agents.py --claim $AGENT_CLAIM \
    --port "$WS_PORT" --max-age-hours "$MAX_AGE_HOURS"

  # Native Claude, Codex, and Cursor conversations do not trigger terminal
  # lifecycle hooks. The Accessibility-granted helper exposes their open
  # Electron routes without requiring per-app plugins or network access.
  spawn desktop_sessions "$PY" desktop_sessions_watcher.py

  # 2b. mic key (macOS only by default)
  if [ -n "$MIC_KEY" ]; then
    spawn connector_mic "$PY" connector_mic.py --key "$MIC_KEY" --port "$WS_PORT"
  else
    skip "connector_mic: macOS only (set MIC_KEY=14 to force)"
  fi

  # 3. feeds: remote Hermes agent threads
  if [ -z "$HERMES_SSH" ]; then
    HERMES_SSH=$(detect_hermes_ssh || true)
    [ -n "$HERMES_SSH" ] && ok "hermes_agents: using ssh alias '$HERMES_SSH' from ~/.ssh/config"
  fi
  if [ -n "$HERMES_SSH" ]; then
    # Spawn even while offline.  The watcher owns bounded retries and health;
    # a one-shot startup preflight used to omit it permanently until the whole
    # stack was restarted after Tailscale/SSH recovered.
    local hermes_args=(hermes_agents_watcher.py --ssh "$HERMES_SSH")
    [ -z "$DISCORD_GUILD_ID" ] || hermes_args+=(--guild-id "$DISCORD_GUILD_ID")
    spawn hermes_agents "$PY" "${hermes_args[@]}"
  else
    skip "hermes_agents: no ssh alias -- H and S keys will stay dark."
    skip "  fix: add a Host block for 'hermes' to ~/.ssh/config, or run"
    skip "       HERMES_SSH=<alias> ./deckbridge.sh start"
  fi

  # 4. feeds: Discord approvals
  local token channel token_file
  token="${DISCORD_BOT_TOKEN:-}"
  token_file="$HOME/.hermes/.env"
  if [ -z "$token" ] && [ -f "$token_file" ]; then
    token=$(grep -m1 '^DISCORD_BOT_TOKEN=' "$token_file" 2>/dev/null \
      | cut -d= -f2- | tr -d '"'"'"'' | tr -d '\r')
  fi
  channel="$DISCORD_CHANNEL_ID"
  if [ -n "$HERMES_SSH" ]; then
    local discord_args=(hermes_discord_watcher.py --ssh-env "$HERMES_SSH")
    [ -z "$channel" ] || discord_args+=(--channel-id "$channel")
    [ -z "$DISCORD_GUILD_ID" ] || discord_args+=(--guild-id "$DISCORD_GUILD_ID")
    # The remote credential adapter retries in process and keeps the token in
    # memory only. Local values, when present, remain explicit overrides.
    DISCORD_BOT_TOKEN="$token" spawn discord_watcher "$PY" "${discord_args[@]}"
  elif [ -n "$token" ] && [ -n "$channel" ]; then
    if [ -n "$DISCORD_GUILD_ID" ]; then
      DISCORD_BOT_TOKEN="$token" spawn discord_watcher \
        "$PY" hermes_discord_watcher.py --channel-id "$channel" \
        --guild-id "$DISCORD_GUILD_ID"
    else
      DISCORD_BOT_TOKEN="$token" spawn discord_watcher \
        "$PY" hermes_discord_watcher.py --channel-id "$channel"
    fi
  else
    skip "discord_watcher: need local Discord settings or HERMES_SSH"
  fi

  # 5. renderer: physical deck and/or browser emulator
  if [ "$hw" = 1 ]; then
    if ! alive renderer_hw; then
      local existing_renderer=""
      existing_renderer=$(find_existing_renderer || true)
      [ -z "$existing_renderer" ] || adopt renderer_hw "$existing_renderer"
    fi
    if alive renderer_hw; then
      : # already lifecycle-owned, either from an earlier start or adoption
    elif have_dep PIL && have_dep StreamDeck; then
      # cairocffi does not discover Homebrew's keg path from the system Python
      # on Apple Silicon. Pass it only to the hardware renderer so SVG logos
      # survive a normal `start --hw`, not just a hand-crafted shell session.
      local cairo_lib=""
      if [ "$(uname -s 2>/dev/null)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        cairo_lib="$(brew --prefix cairo 2>/dev/null)/lib"
      fi
      if [ -n "$cairo_lib" ] && [ -d "$cairo_lib" ]; then
        spawn renderer_hw env \
          "DYLD_FALLBACK_LIBRARY_PATH=$cairo_lib:${DYLD_FALLBACK_LIBRARY_PATH:-}" \
          "$PY" renderer_hw.py --ws "ws://127.0.0.1:$WS_PORT" \
          || skip "renderer_hw failed; is the deck plugged in? (brew install hidapi)"
      else
        spawn renderer_hw "$PY" renderer_hw.py --ws "ws://127.0.0.1:$WS_PORT" \
          || skip "renderer_hw failed; is the deck plugged in? (brew install hidapi)"
      fi
    else
      skip "renderer_hw: pip install pillow streamdeck (macOS also: brew install hidapi)"
    fi
  fi

  # A busy port does NOT mean our emulator is already being served. The common
  # case is a python http.server left running by an earlier start, rooted in a
  # DIFFERENT directory -- an old zip extract that has since been replaced or
  # deleted. Skipping the spawn and printing the URL anyway then sends the
  # operator to a stranger's server, which answers with python's own
  #
  #   Error code: 404 / Message: File not found.
  #
  # and looks like deckbridge is broken. So the port is probed for OUR file
  # rather than assumed, and --directory pins the root instead of trusting cwd.
  if port_open "$HTTP_PORT"; then
    if emulator_served_here "$HTTP_PORT"; then
      if ! alive emulator; then
        local existing_emulator=""
        existing_emulator=$(find_existing_emulator || true)
        [ -z "$existing_emulator" ] || adopt emulator "$existing_emulator"
      fi
      skip "emulator web server: already serving this checkout on $HTTP_PORT"
    else
      bad "port $HTTP_PORT is taken by a server that is NOT this checkout"
      bad "that server answers 404 for emulator.html; using $((HTTP_PORT + 1))"
      HTTP_PORT=$((HTTP_PORT + 1))
      if port_open "$HTTP_PORT"; then
        skip "emulator web server: $HTTP_PORT is busy too; set HTTP_PORT to a free port"
      else
        spawn emulator "$PY" -m http.server "$HTTP_PORT" --directory "$PWD"
      fi
    fi
  else
    spawn emulator "$PY" -m http.server "$HTTP_PORT" --directory "$PWD"
  fi

  local url="http://127.0.0.1:$HTTP_PORT/emulator.html?ws=ws://127.0.0.1:$WS_PORT"
  echo ""
  echo "  emulator: $url"
  if [ "$OPEN_BROWSER" = "1" ] && command -v open >/dev/null 2>&1; then
    open "$url" 2>/dev/null || true
  fi
  echo ""
  dim "  keys $AGENT_CLAIM = agents (H Hermes thread / S Hermes ssh / C Claude / X Codex / M cmux)${MIC_KEY:+    key $MIC_KEY = mic}"
  dim "  status: ./deckbridge.sh status    logs: ./deckbridge.sh logs    stop: ./deckbridge.sh stop"

  if [ "$fg" = 1 ]; then
    trap 'echo; stop; exit 0' INT TERM
    echo ""
    dim "  foreground mode; ctrl-c stops everything"
    while :; do sleep 1; done
  fi
}

# ------------------------------------------------------------------ stop -----
stop() {
  local any=0
  mkdir -p "$RUN_DIR"
  for f in "$RUN_DIR"/*.pid; do
    [ -e "$f" ] || continue
    local name p
    name=$(basename "$f" .pid); p=$(cat "$f" 2>/dev/null)
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
      kill "$p" 2>/dev/null
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$p" 2>/dev/null || break
        sleep 0.2
      done
      kill -9 "$p" 2>/dev/null || true
      ok "stopped $name (pid $p)"
      any=1
    fi
    rm -f "$f"
  done
  [ "$any" = 0 ] && dim "nothing was running"
  return 0
}

# ---------------------------------------------------------------- status -----
status() {
  echo "deckbridge status"
  local any=0
  for f in "$RUN_DIR"/*.pid; do
    [ -e "$f" ] || continue
    local name p
    name=$(basename "$f" .pid); p=$(cat "$f" 2>/dev/null)
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
      ok "$(printf '%-18s pid %s' "$name" "$p")"; any=1
    else
      bad "$(printf '%-18s dead (stale pidfile)' "$name")"
    fi
  done
  [ "$any" = 0 ] && dim "  nothing running; ./deckbridge.sh start"
  echo ""
  port_open "$WS_PORT"   && ok "hub listening on $WS_PORT"        || skip "hub not listening on $WS_PORT"
  port_open "$HTTP_PORT" && ok "emulator served on $HTTP_PORT"    || skip "no web server on $HTTP_PORT"
  echo ""
  echo "state files:"
  for s in ~/.deckbridge/cmux_state.json ~/.deckbridge/desktop_agents.json ~/.deckbridge/t3code_agents.json ~/.deckbridge/hermes_agents.json ~/.deckbridge/hermes_approvals.json; do
    if [ -f "$s" ]; then
      local n
      n=$($PY -c "import json,sys;d=json.load(open(sys.argv[1]));print(len(d.get('agents',d.get('approvals',[]))))" "$s" 2>/dev/null || echo '?')
      printf '  %-42s %s entries\n' "$(basename "$s")" "$n"
    else
      printf '  %-42s absent\n' "$(basename "$s")"
    fi
  done
  echo ""
  echo "connection health:"
  local health_found=0 health_name health_out
  for health_name in t3code_watcher hermes_agents discord_watcher connector_agents connector_mic connector_cmux connector_hermes renderer_hw; do
    [ -f "$HEALTH_DIR/$health_name.json" ] || continue
    health_found=1
    if health_out=$(connection_health "$health_name" 2>&1); then
      ok "$health_out"
    else
      bad "$health_out"
    fi
  done
  [ "$health_found" = 1 ] || dim "  no connection snapshots yet"
}

connection_health() {  # connection_health NAME
  "$PY" - "$HEALTH_DIR/$1.json" <<'PY'
import sys
from connection_runtime import ConnectionHealth

health = ConnectionHealth.from_path(sys.argv[1])
print(health.message)
raise SystemExit(0 if health.ok else 1)
PY
}

# --------------------------------------------------------------- health -----
# Machine-checkable seam used by the launchd supervisor. `status` remains an
# operator report and deliberately exits zero; health names every failed
# invariant and exits nonzero so a process supervisor can repair the stack.
health() {
  local full=0 hw=0 feeds=0 connections=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --full) full=1 ;;
      --hw|--hardware) hw=1 ;;
      --feeds) feeds=1 ;;
      --connections) connections=1; feeds=1 ;;
      *) echo "unknown health option: $1" >&2; return 2 ;;
    esac
    shift
  done

  local failed=0 name
  local required=(deckd connector_agents desktop_sessions t3code_watcher)
  [ -n "$MIC_KEY" ] && required+=(connector_mic)
  if [ "$full" = 1 ]; then
    required+=(hermes_agents discord_watcher)
  fi
  [ "$hw" = 1 ] && required+=(renderer_hw)

  echo "deckbridge health"
  for name in "${required[@]}"; do
    if alive "$name"; then
      ok "$name is running (pid $(cat "$(pidfile "$name")"))"
    else
      bad "$name is not running"
      failed=1
    fi
  done

  if port_open "$WS_PORT"; then
    ok "hub is accepting connections on $WS_PORT"
  else
    bad "hub is not accepting connections on $WS_PORT"
    failed=1
  fi
  if emulator_served_here "$HTTP_PORT"; then
    ok "emulator from this checkout is served on $HTTP_PORT"
  else
    bad "emulator from this checkout is not served on $HTTP_PORT"
    failed=1
  fi

  # Remote outages are deliberately opt-in.  launchd uses ordinary health and
  # must not recycle mic/focus/rendering because Tailscale or Discord is down;
  # operator-facing status uses --feeds/--connections for truthful freshness.
  if [ "$feeds" = 1 ]; then
    for name in hermes_agents discord_watcher; do
      if health_out=$(connection_health "$name" 2>&1); then
        ok "$health_out"
      else
        bad "$health_out"
        failed=1
      fi
    done
  fi
  if [ "$connections" = 1 ]; then
    local connection_names=(connector_agents t3code_watcher)
    [ -z "$MIC_KEY" ] || connection_names+=(connector_mic)
    [ "$hw" = 0 ] || connection_names+=(renderer_hw)
    for name in "${connection_names[@]}"; do
      if health_out=$(connection_health "$name" 2>&1); then
        ok "$health_out"
      else
        bad "$health_out"
        failed=1
      fi
    done
  fi

  if [ "$failed" = 0 ]; then
    if [ "$full" = 1 ] && [ "$hw" = 1 ]; then
      ok "full hardware stack is healthy"
    else
      ok "required stack is healthy"
    fi
    return 0
  fi
  return 1
}

logs() {
  local name="${1:-}"
  if [ -n "$name" ]; then
    tail -n 40 -f "$LOG_DIR/$name.log"
  else
    ls "$LOG_DIR" >/dev/null 2>&1 || { dim "no logs yet"; return 0; }
    tail -n 15 "$LOG_DIR"/*.log
  fi
}

case "${1:-start}" in
  start)   shift || true; start "$@" ;;
  stop)    stop ;;
  restart) shift || true; stop; sleep 0.5; start "$@" ;;
  status)  status ;;
  health)  shift || true; health "$@" ;;
  connections) health --full --hw --connections ;;
  logs)    shift || true; logs "$@" ;;
  doctor)  doctor ;;
  *) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
