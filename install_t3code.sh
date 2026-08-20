#!/usr/bin/env bash
# Install T3 Code and issue Deckbridge a local read-only-ish bearer session.

set -eu

APP="/Applications/T3 Code (Alpha).app"
TOKEN_PATH="${DECKBRIDGE_T3CODE_TOKEN:-$HOME/.deckbridge/t3code_token}"
TTL="${DECKBRIDGE_T3CODE_TOKEN_TTL:-3650d}"

if [ ! -d "$APP" ]; then
  command -v brew >/dev/null 2>&1 || {
    echo "Homebrew is required to install T3 Code: https://brew.sh" >&2
    exit 1
  }
  brew install --cask t3-code
fi

binary="$APP/Contents/MacOS/T3 Code (Alpha)"
cli="$APP/Contents/Resources/app.asar/apps/server/dist/bin.mjs"
[ -x "$binary" ] && [ -f "$APP/Contents/Resources/app.asar" ] || {
  echo "T3 Code's supported server CLI was not found in $APP" >&2
  exit 1
}

mkdir -p "$(dirname "$TOKEN_PATH")"
token=$(ELECTRON_RUN_AS_NODE=1 "$binary" "$cli" auth session issue \
  --ttl "$TTL" --label Deckbridge --subject deckbridge-local --token-only)
[ -n "$token" ] || { echo "T3 Code returned an empty credential" >&2; exit 1; }
umask 077
temporary="$TOKEN_PATH.tmp.$$"
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf '%s\n' "$token" >"$temporary"
chmod 600 "$temporary"
mv "$temporary" "$TOKEN_PATH"
trap - EXIT HUP INT TERM

echo "T3 Code is installed and Deckbridge credentialed at $TOKEN_PATH (mode 600)."
echo "Add each project once in T3 Code; its threads will then appear automatically."
