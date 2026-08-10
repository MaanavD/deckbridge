#!/usr/bin/env bash
# Build and atomically install the standalone Herdr launcher.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
SOURCE="$ROOT/herdr_launcher.applescript"
TARGET=${HERDR_APP_TARGET:-/Applications/Herdr.app}
COMPILER=${HERDR_OSACOMPILE:-/usr/bin/osacompile}

case "$TARGET" in
  /*.app) ;;
  *) echo "Herdr app target must be an absolute .app path: $TARGET" >&2; exit 2 ;;
esac

[ -x "$COMPILER" ] || {
  echo "osacompile is unavailable at $COMPILER" >&2
  exit 1
}

target_parent=${TARGET%/*}
mkdir -p "$target_parent"
stage_dir=$(mktemp -d "${TMPDIR:-/tmp}/deckbridge-herdr.XXXXXX")
backup="$stage_dir/Herdr.previous.app"

cleanup() {
  rm -rf "$stage_dir"
}
trap cleanup EXIT

"$COMPILER" -o "$stage_dir/Herdr.app" "$SOURCE"

if [ -e "$TARGET" ]; then
  mv "$TARGET" "$backup"
fi

if mv "$stage_dir/Herdr.app" "$TARGET"; then
  echo "installed: $TARGET"
else
  if [ -e "$backup" ]; then
    mv "$backup" "$TARGET"
  fi
  echo "failed to install Herdr; restored the previous app" >&2
  exit 1
fi
