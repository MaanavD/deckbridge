#!/usr/bin/env bash
# run_demo.sh - start the whole deckbridge stack for emulator testing, one command.
#
#   ./run_demo.sh              # hub + both connectors + web server, emulator in browser
#   ./run_demo.sh --hardware   # also start the physical-deck renderer
#
# Ctrl-C stops everything (all children are killed via the trap).
set -uo pipefail
cd "$(dirname "$0")"

PY=python3
if [ -x .venv/bin/python3 ]; then PY=.venv/bin/python3; fi

# only websockets is required for the emulator path
if ! $PY -c "import websockets" 2>/dev/null; then
  echo "!! missing dependency 'websockets'."
  echo "   python3 -m venv .venv && . .venv/bin/activate && pip install websockets"
  echo "   (add 'pillow' too if you want the physical deck renderer)"
  exit 1
fi

PIDS=()
cleanup() {
  echo ""
  echo "stopping..."
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

mkdir -p logs

echo "-> deckd (hub) on ws://127.0.0.1:8777"
$PY deckd.py --keys 15 --port 8777 > logs/deckd.log 2>&1 & PIDS+=($!)
sleep 1.2

# One pool owns every agent key, so a Hermes thread and a local Claude session
# compete for the same ten slots instead of each wasting a reserved zone.
echo "-> connector_agents (keys 0-9: H Hermes / S ssh / C Claude / X Codex / M cmux)"
$PY connector_agents.py --claim 0 9 > logs/agents.log 2>&1 & PIDS+=($!)

echo "-> connector_mic (key 14)"
$PY connector_mic.py --key 14 > logs/mic.log 2>&1 & PIDS+=($!)

if [ "${1:-}" = "--hardware" ]; then
  echo "-> renderer_hw (physical Stream Deck)"
  $PY renderer_hw.py > logs/renderer_hw.log 2>&1 & PIDS+=($!)
fi

echo "-> web server for the emulator on http://127.0.0.1:8080"
$PY -m http.server 8080 > logs/http.log 2>&1 & PIDS+=($!)
sleep 1

URL="http://127.0.0.1:8080/emulator.html?ws=ws://127.0.0.1:8777"
echo ""
echo "  open:  $URL"
command -v open >/dev/null && open "$URL" 2>/dev/null || true
echo ""
echo "  seed some state in another terminal:"
echo "     $PY seed_state.py --animate"
echo ""
echo "  logs are in ./logs/ ; ctrl-c here stops everything"
wait
