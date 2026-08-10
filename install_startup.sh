#!/usr/bin/env bash
# Stable public interface for Deckbridge's per-user macOS LaunchAgent.
set -u

ROOT=$(cd "$(dirname "$0")" && pwd)
PYTHON=${DECKBRIDGE_STARTUP_PYTHON:-/usr/bin/python3}
exec "$PYTHON" "$ROOT/startup.py" "$@"
