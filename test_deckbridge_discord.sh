#!/usr/bin/env bash
set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT
mkdir -p "$TMP_DIR/bin" "$TMP_DIR/home/.ssh"

cat >"$TMP_DIR/home/.ssh/config" <<'EOF'
Host hermes
  HostName 192.0.2.10
EOF

cat >"$TMP_DIR/bin/ssh" <<'EOF'
#!/usr/bin/env bash
[ "${FAKE_SSH_OFFLINE:-0}" = 0 ] || exit 255
case "$*" in
  *DISCORD_BOT_TOKEN*) printf '%s\n' 'test-token' ;;
  *DISCORD_HOME_CHANNEL*) printf '%s\n' '121212121212121212' ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$TMP_DIR/bin/ssh"

out=$(cd "$ROOT" && HOME="$TMP_DIR/home" PATH="$TMP_DIR/bin:$PATH" \
  HERMES_SSH=hermes ./deckbridge.sh doctor 2>&1)

if grep -q 'Discord token found on hermes' <<<"$out" && \
   grep -q 'Discord channel found on hermes' <<<"$out"; then
  echo 'PASS remote Hermes Discord configuration is discovered'
else
  echo 'FAIL remote Hermes Discord configuration is discovered'
  printf '%s\n' "$out"
  exit 1
fi

# A remote outage at boot must start retrying watcher processes rather than
# permanently omitting them. They publish degraded health and recover in place
# when SSH returns; launchd should not need to recycle the local stack.
run_dir="$TMP_DIR/run"
log_dir="$TMP_DIR/logs"
FAKE_SSH_OFFLINE=1 HOME="$TMP_DIR/home" PATH="$TMP_DIR/bin:$PATH" \
  HERMES_SSH=hermes DECKBRIDGE_RUN_DIR="$run_dir" \
  DECKBRIDGE_LOG_DIR="$log_dir" WS_PORT=8977 HTTP_PORT=8978 \
  OPEN_BROWSER=0 MIC_KEY='' "$ROOT/deckbridge.sh" start >/dev/null 2>&1
start_rc=$?
sleep 0.5
hermes_pid=$(cat "$run_dir/hermes_agents.pid" 2>/dev/null || true)
discord_pid=$(cat "$run_dir/discord_watcher.pid" 2>/dev/null || true)
if [ "$start_rc" -eq 0 ] && [ -n "$hermes_pid" ] && [ -n "$discord_pid" ] && \
   kill -0 "$hermes_pid" 2>/dev/null && kill -0 "$discord_pid" 2>/dev/null; then
  echo 'PASS offline boot keeps both remote watchers alive and retrying'
else
  echo 'FAIL offline boot keeps both remote watchers alive and retrying'
  [ -f "$log_dir/hermes_agents.log" ] && tail -n 10 "$log_dir/hermes_agents.log"
  [ -f "$log_dir/discord_watcher.log" ] && tail -n 10 "$log_dir/discord_watcher.log"
  FAKE_SSH_OFFLINE=1 HOME="$TMP_DIR/home" PATH="$TMP_DIR/bin:$PATH" \
    DECKBRIDGE_RUN_DIR="$run_dir" DECKBRIDGE_LOG_DIR="$log_dir" \
    WS_PORT=8977 HTTP_PORT=8978 OPEN_BROWSER=0 MIC_KEY='' \
    "$ROOT/deckbridge.sh" stop >/dev/null 2>&1 || true
  exit 1
fi
FAKE_SSH_OFFLINE=1 HOME="$TMP_DIR/home" PATH="$TMP_DIR/bin:$PATH" \
  DECKBRIDGE_RUN_DIR="$run_dir" DECKBRIDGE_LOG_DIR="$log_dir" \
  WS_PORT=8977 HTTP_PORT=8978 OPEN_BROWSER=0 MIC_KEY='' \
  "$ROOT/deckbridge.sh" stop >/dev/null 2>&1 || true
