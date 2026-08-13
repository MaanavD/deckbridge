#!/usr/bin/env bash
# launchd-owned supervisor for the complete physical Deckbridge stack.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")" && pwd)
DECKBRIDGE=${DECKBRIDGE_COMMAND:-"$ROOT/deckbridge.sh"}
HEALTH_INTERVAL=${DECKBRIDGE_HEALTH_INTERVAL:-5}
LOG_DIR=${DECKBRIDGE_LOG_DIR:-logs}
LOG_MAX_BYTES=${DECKBRIDGE_LOG_MAX_BYTES:-5242880}
LOG_KEEP_BYTES=${DECKBRIDGE_LOG_KEEP_BYTES:-1048576}
STOPPING=0
RUN_DIR=${DECKBRIDGE_RUN_DIR:-.run}
SUPERVISOR_LOCK="$RUN_DIR/launchd-supervisor.lock"
LOCK_OWNED=0

case "$LOG_MAX_BYTES:$LOG_KEEP_BYTES" in
  *[!0-9:]*)
    printf '[launchd] log limits must be non-negative byte counts\n' >&2
    exit 2
    ;;
esac
if [ "$LOG_KEEP_BYTES" -gt "$LOG_MAX_BYTES" ]; then
  LOG_KEEP_BYTES=$LOG_MAX_BYTES
fi

trim_logs() {
  # Keep long-running component and supervisor logs bounded without renaming
  # their live files: launchd and the Python children retain open descriptors,
  # so rotation-by-move would silently send new output into orphaned inodes.
  local file size tmp
  mkdir -p "$LOG_DIR"
  for file in "$LOG_DIR"/*.log; do
    [ -f "$file" ] || continue
    size=$(wc -c <"$file" | tr -d '[:space:]')
    [ "$size" -le "$LOG_MAX_BYTES" ] && continue
    tmp="$file.trim.$$"
    if tail -c "$LOG_KEEP_BYTES" "$file" >"$tmp"; then
      command cat "$tmp" >"$file"
    fi
    rm -f "$tmp"
  done
}

release_supervisor_lock() {
  [ "$LOCK_OWNED" = 1 ] || return 0
  # Only the process that wrote this PID may release the lock. This protects a
  # newer generation if shutdown and launchd restart cross in flight.
  if [ "$(cat "$SUPERVISOR_LOCK/pid" 2>/dev/null || true)" = "$$" ]; then
    rm -f "$SUPERVISOR_LOCK/pid"
    rmdir "$SUPERVISOR_LOCK" 2>/dev/null || true
  fi
  LOCK_OWNED=0
}

acquire_supervisor_lock() {
  local owner announced=0
  mkdir -p "$RUN_DIR"
  while ! mkdir "$SUPERVISOR_LOCK" 2>/dev/null; do
    owner=$(cat "$SUPERVISOR_LOCK/pid" 2>/dev/null || true)
    if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
      if [ "$announced" = 0 ]; then
        printf '[launchd] waiting for supervisor %s to finish\n' "$owner"
        announced=1
      fi
      sleep 0.2
      continue
    fi
    # A crash can leave the tiny directory behind. Remove only its known file
    # and empty directory, then contend atomically again.
    rm -f "$SUPERVISOR_LOCK/pid"
    rmdir "$SUPERVISOR_LOCK" 2>/dev/null || true
  done
  printf '%s\n' "$$" >"$SUPERVISOR_LOCK/pid"
  LOCK_OWNED=1
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM HUP
  if [ "$LOCK_OWNED" = 1 ]; then
    "$DECKBRIDGE" stop || true
  fi
  release_supervisor_lock
  exit "$rc"
}

terminate() {
  STOPPING=1
  exit 0
}

trap cleanup EXIT
trap terminate INT TERM HUP

cd "$ROOT"
acquire_supervisor_lock
trim_logs
printf '[launchd] starting full hardware stack from %s\n' "$ROOT"
"$DECKBRIDGE" restart --hw || exit 1

# Do not wait for the first interval before detecting a partial startup. Missing
# Discord/Hermes watchers or a renderer makes this process fail, which activates
# launchd's KeepAlive retry rather than blessing a half-running service.
while :; do
  if health_output=$("$DECKBRIDGE" health --full --hw 2>&1); then
    : # successful probes stay quiet; logs record transitions, not polling noise
  else
    printf '%s\n' "$health_output" >&2
    [ "$STOPPING" = 1 ] && exit 0
    printf '[launchd] required child unhealthy; exiting for launchd restart\n' >&2
    exit 1
  fi
  trim_logs
  sleep "$HEALTH_INTERVAL" &
  wait $!
done
