#!/usr/bin/env bash
# Public-interface tests for the per-user macOS startup module.
set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
TMP_DIR=$(mktemp -d)
HELPER_PID=""
ADOPT_RENDERER_PID=""
ADOPT_EMULATOR_PID=""
LOCK_RUNNER_PID=""
cleanup() {
  [ -z "$LOCK_RUNNER_PID" ] || kill "$LOCK_RUNNER_PID" 2>/dev/null || true
  [ -z "$HELPER_PID" ] || kill "$HELPER_PID" 2>/dev/null || true
  [ -z "$ADOPT_RENDERER_PID" ] || kill "$ADOPT_RENDERER_PID" 2>/dev/null || true
  [ -z "$ADOPT_EMULATOR_PID" ] || kill "$ADOPT_EMULATOR_PID" 2>/dev/null || true
  [ -z "$HELPER_PID" ] || wait "$HELPER_PID" 2>/dev/null || true
  [ -z "$ADOPT_RENDERER_PID" ] || wait "$ADOPT_RENDERER_PID" 2>/dev/null || true
  [ -z "$ADOPT_EMULATOR_PID" ] || wait "$ADOPT_EMULATOR_PID" 2>/dev/null || true
  [ -z "$LOCK_RUNNER_PID" ] || wait "$LOCK_RUNNER_PID" 2>/dev/null || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT
mkdir -p "$TMP_DIR/home/Library/LaunchAgents" "$TMP_DIR/bin"

PASS=0
FAIL=0
check() {
  local name="$1"; shift
  if "$@"; then
    printf 'PASS %s\n' "$name"
    PASS=$((PASS + 1))
  else
    printf 'FAIL %s\n' "$name"
    FAIL=$((FAIL + 1))
  fi
}

cat >"$TMP_DIR/bin/launchctl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_LAUNCHCTL_LOG"
case "${1:-}" in
  print) [ "${FAKE_LAUNCHD_LOADED:-1}" = 1 ] ;;
  bootstrap)
    if [ "${FAKE_BOOTSTRAP_FAIL_ONCE:-0}" = 1 ] && \
       [ ! -e "$FAKE_LAUNCHCTL_LOG.bootstrap-failed" ]; then
      touch "$FAKE_LAUNCHCTL_LOG.bootstrap-failed"
      printf 'Bootstrap failed: 5: Input/output error\n' >&2
      exit 5
    fi
    exit 0
    ;;
  bootout) [ "${FAKE_BOOTOUT_FAIL:-0}" != 1 ] ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$TMP_DIR/bin/launchctl"

cat >"$TMP_DIR/bin/deckbridge" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = health ]; then
  printf 'run-dir=%s\n' "${DECKBRIDGE_RUN_DIR:-unset}"
  printf '%s\n' "${FAKE_HEALTH_MESSAGE:-deckbridge healthy}"
  exit "${FAKE_HEALTH_EXIT:-0}"
fi
exit 2
EOF
chmod +x "$TMP_DIR/bin/deckbridge"

export HOME="$TMP_DIR/home"
export DECKBRIDGE_UID=501
export DECKBRIDGE_LAUNCHCTL="$TMP_DIR/bin/launchctl"
export DECKBRIDGE_COMMAND="$TMP_DIR/bin/deckbridge"
export DECKBRIDGE_SKIP_SOURCE_STOP=1
export FAKE_LAUNCHCTL_LOG="$TMP_DIR/launchctl.log"
export DECKBRIDGE_MIC_HELPER="$HOME/Applications/Deckbridge Mic.app/Contents/MacOS/deckbridge-mic"

INSTALLER="$ROOT/install_startup.sh"
PLIST="$HOME/Library/LaunchAgents/com.deckbridge.agent.plist"
RUNTIME="$HOME/Library/Application Support/Deckbridge"
PERSISTENT_RUN="$HOME/Library/Caches/Deckbridge/run"
PERSISTENT_LOGS="$HOME/Library/Logs/Deckbridge"

# Slice 1: install is one idempotent command that renders a complete LaunchAgent.
install_out=$("$INSTALLER" install 2>&1)
install_rc=$?
check "install succeeds" test "$install_rc" -eq 0
check "install writes the per-user plist" test -f "$PLIST"
check "install cuts over only after rendering by bootout/bootstrap" \
  grep -q "bootout gui/501/com.deckbridge.agent" "$FAKE_LAUNCHCTL_LOG"
check "install bootstraps the rendered plist" \
  grep -q "bootstrap gui/501 $PLIST" "$FAKE_LAUNCHCTL_LOG"

plist_result=$(python3 - "$PLIST" "$RUNTIME" "$PERSISTENT_RUN" "$PERSISTENT_LOGS" <<'PY'
import plistlib, sys
path, runtime, run_dir, log_dir = sys.argv[1:]
with open(path, "rb") as f:
    p = plistlib.load(f)
checks = {
    "label": p.get("Label") == "com.deckbridge.agent",
    "runner": p.get("ProgramArguments") == [runtime + "/deckbridge_launchd.sh"],
    # The runtime directory is atomically replaced on every install. launchd
    # must start the next generation from its stable parent or the shell can
    # inherit a deleted cwd and fail before deckbridge_launchd.sh can cd.
    "cwd": p.get("WorkingDirectory") == str(__import__("pathlib").Path(runtime).parent),
    "run": p.get("RunAtLoad") is True,
    "restart": p.get("KeepAlive", {}).get("SuccessfulExit") is False,
    "throttle": p.get("ThrottleInterval") == 10,
    "browser": p.get("EnvironmentVariables", {}).get("OPEN_BROWSER") == "0",
    "path": p.get("EnvironmentVariables", {}).get("PATH", "").startswith("/opt/homebrew/bin:"),
    "run-dir": p.get("EnvironmentVariables", {}).get("DECKBRIDGE_RUN_DIR") == run_dir,
    "log-dir": p.get("EnvironmentVariables", {}).get("DECKBRIDGE_LOG_DIR") == log_dir,
    "stdout": p.get("StandardOutPath") == log_dir + "/launchagent.log",
    "stderr": p.get("StandardErrorPath") == log_dir + "/launchagent-error.log",
}
bad = [name for name, ok in checks.items() if not ok]
print("ok" if not bad else "bad:" + ",".join(bad))
PY
)
check "plist pins runner, cwd, env, logs, and restart policy" test "$plist_result" = ok
check "install creates a boot-readable runtime outside protected Downloads" \
  test -x "$RUNTIME/deckbridge_launchd.sh"
check "installed runtime contains the complete lifecycle command" \
  test -x "$RUNTIME/deckbridge.sh"
check "installed runtime keeps its Python environment" \
  test -x "$RUNTIME/.venv/bin/python3"
check "install creates the stable native mic helper outside generated runtime" \
  test -x "$HOME/Applications/Deckbridge Mic.app/Contents/MacOS/deckbridge-mic"
check "install reports the stable service label" \
  grep -q "com.deckbridge.agent" <<<"$install_out"

# Running install again must remain successful and leave a valid plist.
check "install is idempotent" "$INSTALLER" install
check "idempotent install leaves valid plist" plutil -lint "$PLIST"

rm -f "$FAKE_LAUNCHCTL_LOG.bootstrap-failed"
check "install retries launchctl's transient post-bootout error" \
  env FAKE_BOOTSTRAP_FAIL_ONCE=1 DECKBRIDGE_STARTUP_INTERVAL=0.01 \
  "$INSTALLER" install

# launchctl bootout can return before the old supervisor's EXIT trap finishes.
# Replacing its cwd during that interval leaves getcwd errors, while the old
# cleanup can kill the new generation through their shared pid directory.
mkdir -p "$PERSISTENT_RUN/launchd-supervisor.lock"
printf '%s\n' "$$" >"$PERSISTENT_RUN/launchd-supervisor.lock/pid"
touch "$RUNTIME/old-supervisor-still-cleaning"
overlap_out=$(DECKBRIDGE_UNLOAD_TIMEOUT=0.05 \
  "$INSTALLER" install 2>&1)
overlap_rc=$?
check "install waits for the unloaded supervisor to release its lifecycle lock" \
  test "$overlap_rc" -ne 0
check "install preserves runtime while the old supervisor is still cleaning" \
  test -e "$RUNTIME/old-supervisor-still-cleaning"
rm -f "$PERSISTENT_RUN/launchd-supervisor.lock/pid"
rmdir "$PERSISTENT_RUN/launchd-supervisor.lock"

# A genuine unload failure must not let an update replace the runtime under a
# still-running generation.
touch "$RUNTIME/existing-generation"
unsafe_install_out=$(FAKE_BOOTOUT_FAIL=1 FAKE_LAUNCHD_LOADED=1 \
  "$INSTALLER" install 2>&1)
unsafe_install_rc=$?
check "install fails safely when the old job remains loaded" \
  test "$unsafe_install_rc" -ne 0
check "failed cutover preserves the active runtime" \
  test -e "$RUNTIME/existing-generation"
check "failed cutover explains the safety refusal" \
  grep -q 'refusing to replace a still-loaded' <<<"$unsafe_install_out"

# Slice 2: status is not a plist-exists check; it crosses into child health.
status_out=$(FAKE_LAUNCHD_LOADED=1 FAKE_HEALTH_EXIT=0 "$INSTALLER" status 2>&1)
status_rc=$?
check "status succeeds only when launchd and children are healthy" test "$status_rc" -eq 0
check "status prints child health evidence" grep -q "deckbridge healthy" <<<"$status_out"
check "status checks the installed service's persistent pid directory" \
  grep -q "run-dir=$PERSISTENT_RUN" <<<"$status_out"

connections_out=$(FAKE_LAUNCHD_LOADED=1 FAKE_HEALTH_EXIT=0 \
  "$INSTALLER" connections 2>&1)
connections_rc=$?
check "connections checks the installed service's complete transport health" \
  test "$connections_rc" -eq 0
check "connections reports an all-healthy installed verdict" \
  grep -q 'all installed Deckbridge connections are healthy' <<<"$connections_out"

bad_out=$(FAKE_LAUNCHD_LOADED=1 FAKE_HEALTH_EXIT=1 \
  FAKE_HEALTH_MESSAGE='renderer_hw is not running' "$INSTALLER" status 2>&1)
bad_rc=$?
check "status fails when a required child is unhealthy" test "$bad_rc" -ne 0
check "status explains which child is unhealthy" \
  grep -q "renderer_hw is not running" <<<"$bad_out"

unloaded_out=$(FAKE_LAUNCHD_LOADED=0 "$INSTALLER" status 2>&1)
unloaded_rc=$?
check "status fails when LaunchAgent is not loaded" test "$unloaded_rc" -ne 0
check "unloaded status is actionable" grep -q "not loaded" <<<"$unloaded_out"

# Slice 3: uninstall unloads before removing its file and is idempotent.
check "uninstall succeeds" "$INSTALLER" uninstall
check "uninstall removes plist" test ! -e "$PLIST"
check "uninstall asks launchd to boot out the job" \
  grep -q "bootout gui/501/com.deckbridge.agent" "$FAKE_LAUNCHCTL_LOG"
check "uninstall is idempotent" "$INSTALLER" uninstall

# Likewise, a failed bootout must never delete files beneath a live service.
mkdir -p "$RUNTIME"
touch "$RUNTIME/live-generation"
printf 'placeholder\n' >"$PLIST"
unsafe_uninstall_out=$(FAKE_BOOTOUT_FAIL=1 FAKE_LAUNCHD_LOADED=1 \
  "$INSTALLER" uninstall 2>&1)
unsafe_uninstall_rc=$?
check "uninstall fails safely when the job remains loaded" \
  test "$unsafe_uninstall_rc" -ne 0
check "failed uninstall preserves its plist and runtime" \
  test -e "$PLIST" -a -e "$RUNTIME/live-generation"
check "failed uninstall explains the safety refusal" \
  grep -q 'refusing to remove a still-loaded' <<<"$unsafe_uninstall_out"
rm -rf "$RUNTIME"
rm -f "$PLIST"

# Slice 4: the launchd runner converts a partial child stack into a nonzero
# process exit (launchd KeepAlive's restart signal) and always cleans up.
cat >"$TMP_DIR/bin/partial-deckbridge" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$FAKE_DECKBRIDGE_LOG"
case "${1:-}" in
  restart|stop) exit 0 ;;
  health) printf 'renderer_hw is not running\n'; exit 1 ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$TMP_DIR/bin/partial-deckbridge"
export FAKE_DECKBRIDGE_LOG="$TMP_DIR/deckbridge.log"
mkdir -p "$TMP_DIR/supervisor-logs"
python3 -c 'print("x" * 512)' >"$TMP_DIR/supervisor-logs/growing.log"

# A second generation must never run `restart`: that command stops children
# owned by the first generation and was the source of a launchd restart loop.
mkdir -p "$TMP_DIR/locked-run/launchd-supervisor.lock"
printf '%s\n' "$$" >"$TMP_DIR/locked-run/launchd-supervisor.lock/pid"
DECKBRIDGE_RUN_DIR="$TMP_DIR/locked-run" \
  DECKBRIDGE_COMMAND="$TMP_DIR/bin/partial-deckbridge" \
  DECKBRIDGE_LOG_DIR="$TMP_DIR/supervisor-logs" \
  "$ROOT/deckbridge_launchd.sh" >"$TMP_DIR/locked-runner.log" 2>&1 &
LOCK_RUNNER_PID=$!
sleep 0.1
check "a concurrent supervisor waits without restarting shared children" \
  test ! -s "$FAKE_DECKBRIDGE_LOG"
kill "$LOCK_RUNNER_PID" 2>/dev/null || true
wait "$LOCK_RUNNER_PID" 2>/dev/null || true
LOCK_RUNNER_PID=""
rm -f "$TMP_DIR/locked-run/launchd-supervisor.lock/pid"
rmdir "$TMP_DIR/locked-run/launchd-supervisor.lock"

runner_out=$(DECKBRIDGE_COMMAND="$TMP_DIR/bin/partial-deckbridge" \
  DECKBRIDGE_RUN_DIR="$TMP_DIR/runner-run" \
  DECKBRIDGE_HEALTH_INTERVAL=0.01 \
  DECKBRIDGE_LOG_DIR="$TMP_DIR/supervisor-logs" \
  DECKBRIDGE_LOG_MAX_BYTES=128 DECKBRIDGE_LOG_KEEP_BYTES=32 \
  "$ROOT/deckbridge_launchd.sh" 2>&1)
runner_rc=$?
check "runner exits nonzero so launchd restarts a partial stack" test "$runner_rc" -ne 0
check "runner always requests the full hardware stack" \
  grep -q '^restart --hw$' "$FAKE_DECKBRIDGE_LOG"
check "runner health requires feeds and hardware" \
  grep -q '^health --full --hw$' "$FAKE_DECKBRIDGE_LOG"
check "runner stops children before launchd retry" \
  grep -q '^stop$' "$FAKE_DECKBRIDGE_LOG"
check "runner preserves the child failure evidence" \
  grep -q 'renderer_hw is not running' <<<"$runner_out"
trimmed_size=$(wc -c <"$TMP_DIR/supervisor-logs/growing.log" | tr -d '[:space:]')
check "runner bounds growing logs without renaming their live files" \
  test "$trimmed_size" -eq 32

# Slice 5: Deckbridge exposes a real health command for the startup module.
# The fixture owns two loopback listeners and serves this checkout's emulator;
# pidfiles point at that live fixture process, exercising only public inputs.
mkdir -p "$TMP_DIR/run"
python3 - "$ROOT" "$TMP_DIR/ports" >"$TMP_DIR/helper.log" 2>&1 <<'PY' &
import http.server, os, socket, sys, threading, time
root, port_file = sys.argv[1:]
os.chdir(root)
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
ws = socket.socket()
ws.bind(("127.0.0.1", 0))
ws.listen()
with open(port_file, "w") as f:
    f.write(f"{ws.getsockname()[1]} {httpd.server_port}\n")
threading.Thread(target=httpd.serve_forever, daemon=True).start()
while True:
    time.sleep(1)
PY
HELPER_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$TMP_DIR/ports" ] && break
  sleep 0.1
done
read -r TEST_WS_PORT TEST_HTTP_PORT <"$TMP_DIR/ports"
for name in deckd connector_agents desktop_sessions t3code_watcher connector_mic hermes_agents discord_watcher renderer_hw; do
  printf '%s\n' "$HELPER_PID" >"$TMP_DIR/run/$name.pid"
done
health_out=$(DECKBRIDGE_RUN_DIR="$TMP_DIR/run" WS_PORT="$TEST_WS_PORT" \
  HTTP_PORT="$TEST_HTTP_PORT" MIC_KEY=14 OPEN_BROWSER=0 \
  "$ROOT/deckbridge.sh" health --full --hw 2>&1)
health_rc=$?
check "health accepts a complete live full-hardware stack" test "$health_rc" -eq 0
check "healthy output names the complete stack" grep -q 'full hardware stack is healthy' <<<"$health_out"

# External connections need a second, truthful health layer.  They must not be
# folded into launchd's process-liveness check: restarting the entire local
# stack cannot repair Tailscale reauthentication or a Discord outage.
mkdir -p "$TMP_DIR/health"
python3 - "$TMP_DIR/health" <<'PY'
import json, os, sys, time
root = sys.argv[1]
for name in ("hermes_agents", "discord_watcher"):
    with open(os.path.join(root, name + ".json"), "w") as f:
        json.dump({
            "name": name, "status": "ready", "checked_at": time.time(),
            "last_success_at": time.time(), "stale_after_seconds": 30,
            "consecutive_failures": 0, "error": "",
        }, f)
PY
feed_out=$(DECKBRIDGE_RUN_DIR="$TMP_DIR/run" \
  DECKBRIDGE_HEALTH_DIR="$TMP_DIR/health" WS_PORT="$TEST_WS_PORT" \
  HTTP_PORT="$TEST_HTTP_PORT" MIC_KEY=14 OPEN_BROWSER=0 \
  "$ROOT/deckbridge.sh" health --full --hw --feeds 2>&1)
feed_rc=$?
check "fresh external feeds pass explicit feed health" test "$feed_rc" -eq 0
check "feed health names both remote adapters" \
  sh -c 'grep -q "hermes_agents feed is ready" <<<"$1" && grep -q "discord_watcher feed is ready" <<<"$1"' sh "$feed_out"

python3 - "$TMP_DIR/health/hermes_agents.json" <<'PY'
import json, sys, time
path = sys.argv[1]
with open(path) as f:
    state = json.load(f)
state.update({
    "status": "degraded", "checked_at": time.time(),
    "consecutive_failures": 3,
    "error": "Tailscale SSH requires an additional check",
})
with open(path, "w") as f:
    json.dump(state, f)
PY
degraded_out=$(DECKBRIDGE_RUN_DIR="$TMP_DIR/run" \
  DECKBRIDGE_HEALTH_DIR="$TMP_DIR/health" WS_PORT="$TEST_WS_PORT" \
  HTTP_PORT="$TEST_HTTP_PORT" MIC_KEY=14 OPEN_BROWSER=0 \
  "$ROOT/deckbridge.sh" health --full --hw --feeds 2>&1)
degraded_rc=$?
check "degraded external feed fails explicit feed health" test "$degraded_rc" -ne 0
check "feed failure preserves the actionable transport reason" \
  grep -q 'Tailscale SSH requires an additional check' <<<"$degraded_out"

# The supervisor's ordinary health remains green during an external outage, so
# Discord cannot take down mic, focus, hub, or hardware connections with it.
local_health_out=$(DECKBRIDGE_RUN_DIR="$TMP_DIR/run" WS_PORT="$TEST_WS_PORT" \
  HTTP_PORT="$TEST_HTTP_PORT" MIC_KEY=14 OPEN_BROWSER=0 \
  "$ROOT/deckbridge.sh" health --full --hw 2>&1)
local_health_rc=$?
check "external outage does not fail local supervisor health" \
  test "$local_health_rc" -eq 0

# The documented convenience command must remain an actual command, not fall
# into the usage/default branch. T3 is part of the local connection report.
python3 - "$TMP_DIR/health" <<'PY'
import json, os, sys, time
for name in ("t3code_watcher", "connector_agents", "connector_mic", "renderer_hw",
             "hermes_agents", "discord_watcher"):
    with open(os.path.join(sys.argv[1], name + ".json"), "w") as f:
        json.dump({"name": name, "status": "ready",
                   "checked_at": time.time(), "last_success_at": time.time(),
                   "stale_after_seconds": 30, "consecutive_failures": 0,
                   "error": ""}, f)
PY
connections_out=$(DECKBRIDGE_RUN_DIR="$TMP_DIR/run" \
  DECKBRIDGE_HEALTH_DIR="$TMP_DIR/health" WS_PORT="$TEST_WS_PORT" \
  HTTP_PORT="$TEST_HTTP_PORT" MIC_KEY=14 OPEN_BROWSER=0 \
  "$ROOT/deckbridge.sh" connections 2>&1)
check "connections command runs the complete health report" \
  grep -q 'full hardware stack is healthy' <<<"$connections_out"
check "connections command includes the T3 API feed" \
  grep -q 't3code_watcher feed is ready' <<<"$connections_out"

rm "$TMP_DIR/run/renderer_hw.pid"
unhealthy_out=$(DECKBRIDGE_RUN_DIR="$TMP_DIR/run" WS_PORT="$TEST_WS_PORT" \
  HTTP_PORT="$TEST_HTTP_PORT" MIC_KEY=14 OPEN_BROWSER=0 \
  "$ROOT/deckbridge.sh" health --full --hw 2>&1)
unhealthy_rc=$?
check "health fails when required hardware renderer disappears" test "$unhealthy_rc" -ne 0
check "health names the missing hardware renderer" \
  grep -q 'renderer_hw is not running' <<<"$unhealthy_out"

kill "$HELPER_PID"
wait "$HELPER_PID" 2>/dev/null || true
HELPER_PID=""

# Slice 6: cutover from a manually started stack adopts, rather than duplicates,
# the one hardware owner and web server that already belong to this checkout.
read -r ADOPT_WS_PORT ADOPT_HTTP_PORT < <(python3 - <<'PY'
import socket
sockets = []
ports = []
for _ in range(2):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    sockets.append(s)
    ports.append(s.getsockname()[1])
print(*ports)
PY
)
(cd "$ROOT" && exec python3 -c 'import time; time.sleep(120)' \
  renderer_hw.py --ws "ws://127.0.0.1:$ADOPT_WS_PORT") &
ADOPT_RENDERER_PID=$!
(cd "$ROOT" && exec python3 -m http.server "$ADOPT_HTTP_PORT" \
  --bind 127.0.0.1 --directory "$ROOT") >"$TMP_DIR/adopt-emulator.log" 2>&1 &
ADOPT_EMULATOR_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  python3 - "$ADOPT_HTTP_PORT" <<'PY' >/dev/null 2>&1 && break
import socket, sys
s = socket.socket(); s.settimeout(.1)
raise SystemExit(s.connect_ex(("127.0.0.1", int(sys.argv[1]))))
PY
  sleep 0.1
done

mkdir -p "$TMP_DIR/adopt-run" "$TMP_DIR/adopt-logs"
adopt_out=$(DECKBRIDGE_RUN_DIR="$TMP_DIR/adopt-run" \
  DECKBRIDGE_LOG_DIR="$TMP_DIR/adopt-logs" WS_PORT="$ADOPT_WS_PORT" \
  HTTP_PORT="$ADOPT_HTTP_PORT" HOME="$TMP_DIR/home" OPEN_BROWSER=0 \
  "$ROOT/deckbridge.sh" start --hw 2>&1)
adopt_rc=$?
adopted_renderer=$(cat "$TMP_DIR/adopt-run/renderer_hw.pid" 2>/dev/null || true)
adopted_emulator=$(cat "$TMP_DIR/adopt-run/emulator.pid" 2>/dev/null || true)
check "start succeeds beside an old manual stack" test "$adopt_rc" -eq 0
check "start adopts the existing renderer instead of opening the device twice" \
  test "$adopted_renderer" = "$ADOPT_RENDERER_PID"
check "start adopts the existing emulator instead of duplicating its server" \
  test "$adopted_emulator" = "$ADOPT_EMULATOR_PID"
check "adoption is visible in lifecycle output" \
  grep -q 'adopted existing renderer_hw' <<<"$adopt_out"

DECKBRIDGE_RUN_DIR="$TMP_DIR/adopt-run" DECKBRIDGE_LOG_DIR="$TMP_DIR/adopt-logs" \
  WS_PORT="$ADOPT_WS_PORT" HTTP_PORT="$ADOPT_HTTP_PORT" HOME="$TMP_DIR/home" \
  OPEN_BROWSER=0 "$ROOT/deckbridge.sh" stop >/dev/null
wait "$ADOPT_RENDERER_PID" 2>/dev/null || true
wait "$ADOPT_EMULATOR_PID" 2>/dev/null || true
sleep 0.3
check "stop owns and closes the adopted renderer" \
  sh -c '! kill -0 "$1" 2>/dev/null' sh "$ADOPT_RENDERER_PID"
check "stop owns and closes the adopted emulator" \
  sh -c '! kill -0 "$1" 2>/dev/null' sh "$ADOPT_EMULATOR_PID"
ADOPT_RENDERER_PID=""
ADOPT_EMULATOR_PID=""

printf '\nstartup: %d passed, %d failed\n' "$PASS" "$FAIL"
test "$FAIL" -eq 0
