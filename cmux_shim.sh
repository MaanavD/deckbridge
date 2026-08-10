#!/bin/sh
# Update the state file consumed by connector_cmux.py.
#
# It is suitable as a cmux notification command/status hook.  cmux can provide
# JSON on stdin (the notification policy shape), or its notification-command
# environment variables can be used directly:
#   CMUX_NOTIFICATION_TITLE, CMUX_NOTIFICATION_SUBTITLE,
#   CMUX_NOTIFICATION_BODY
#
# Optional positional arguments override those values:
#   cmux_shim.sh AGENT STATUS [CWD]
#
# The state file is ~/.deckbridge/cmux_state.json by default.  Set
# DECKBRIDGE_CMUX_STATE to override it.  Status mapping is:
# Running/working -> working, Idle -> idle, Waiting/needs input/Error ->
# blocked, and done/complete -> done.  jq is used when available; Python 3 is
# the dependency-free fallback.  Updates are atomic so the connector never
# observes a partially written JSON document.

set -eu

state_path=${DECKBRIDGE_CMUX_STATE:-"${HOME:-.}/.deckbridge/cmux_state.json"}
json_input=$(cat 2>/dev/null || true)

agent=${1:-${CMUX_AGENT:-${CMUX_NOTIFICATION_TITLE:-}}}
status=${2:-${CMUX_STATUS:-${CMUX_NOTIFICATION_SUBTITLE:-${CMUX_NOTIFICATION_BODY:-}}}}
cwd=${3:-${CMUX_CWD:-${PWD:-}}}

# A notification hook receives the documented policy JSON on stdin.  Prefer jq
# for extraction, but keep the hook usable on a minimal macOS install.
if [ -n "$json_input" ]; then
    if command -v jq >/dev/null 2>&1; then
        json_values=$(printf '%s' "$json_input" | jq -r '[.notification.title // "", (.notification.subtitle // .notification.body // ""), (.context.cwd // "")] | @tsv' 2>/dev/null || true)
    else
        json_values=$(printf '%s' "$json_input" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    n = d.get("notification") or {}
    c = d.get("context") or {}
    print("\t".join(str(x or "") for x in (n.get("title"), n.get("subtitle") or n.get("body"), c.get("cwd"))))
except Exception:
    pass
' 2>/dev/null || true)
    fi
    if [ -n "${json_values:-}" ]; then
        old_ifs=$IFS
        tab=$(printf '\t')
        IFS=$tab
        # read keeps any additional tabs in the final field, which is fine for
        # ordinary agent names and paths.
        read -r json_agent json_status json_cwd <<EOF
$json_values
EOF
        IFS=$old_ifs
        [ -n "$agent" ] || agent=$json_agent
        [ -n "$status" ] || status=$json_status
        [ -n "$cwd" ] || cwd=$json_cwd
    fi
fi

[ -n "$agent" ] || agent="cmux"
[ -n "$status" ] || status="idle"

status_lc=$(printf '%s' "$status" | tr '[:upper:]' '[:lower:]')
case "$status_lc" in
    running|working) mapped=working ;;
    idle|quiet) mapped=idle ;;
    waiting|blocked|error|*needs*) mapped=blocked ;;
    done|complete|completed) mapped=done ;;
    *) mapped=idle ;;
esac

parent=$(dirname "$state_path")
mkdir -p "$parent"
tmp_path="$state_path.tmp.$$"
cleanup() { rm -f "$tmp_path"; }
trap cleanup EXIT HUP INT TERM

if command -v jq >/dev/null 2>&1; then
    if [ -f "$state_path" ]; then
        jq --arg name "$agent" --arg status "$mapped" --arg cwd "$cwd" \
           '(. // {}) | .agents = (((.agents // []) | map(select(.name != $name))) + [{name: $name, status: $status, cwd: $cwd}])' \
           "$state_path" > "$tmp_path" 2>/dev/null || {
            printf '%s\n' '{"agents":[]}' | jq --arg name "$agent" --arg status "$mapped" --arg cwd "$cwd" \
              '.agents = [{name: $name, status: $status, cwd: $cwd}]' > "$tmp_path"
        }
    else
        printf '%s\n' '{"agents":[]}' | jq --arg name "$agent" --arg status "$mapped" --arg cwd "$cwd" \
          '.agents = [{name: $name, status: $status, cwd: $cwd}]' > "$tmp_path"
    fi
else
    python3 - "$state_path" "$tmp_path" "$agent" "$mapped" "$cwd" <<'PY'
import json
import os
import sys

state_path, tmp_path, name, status, cwd = sys.argv[1:]
try:
    with open(state_path, encoding="utf-8") as handle:
        document = json.load(handle)
except (FileNotFoundError, OSError, ValueError):
    document = {}
if not isinstance(document, dict):
    document = {}
raw_agents = document.get("agents", [])
if not isinstance(raw_agents, list):
    raw_agents = []
agents = [item for item in raw_agents if isinstance(item, dict) and str(item.get("name", "")) != name]
agents.append({"name": name, "status": status, "cwd": cwd})
document["agents"] = agents
with open(tmp_path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, separators=(",", ":"))
    handle.write("\n")
os.replace(tmp_path, state_path)
PY
fi

# jq branch needs the same atomic rename as the Python branch.
if [ -e "$tmp_path" ]; then
    mv -f "$tmp_path" "$state_path"
fi
trap - EXIT HUP INT TERM
