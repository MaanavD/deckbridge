#!/usr/bin/env bash
# Native helper QA.  No key event is ever posted: only version/frontmost/check.

set -u
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/deckbridge-mic-helper-test.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM
APP="$TMP_DIR/Deckbridge Mic.app"

passed=0
total=0
check() {
    total=$((total + 1))
    if "$@"; then passed=$((passed + 1)); else
        printf 'FAIL:' >&2; printf ' %s' "$@" >&2; printf '\n' >&2
    fi
}

if ! DECKBRIDGE_MIC_APP="$APP" "$ROOT/install_mic_helper.sh" install >/dev/null; then
    printf 'FAIL: helper installation failed\n' >&2
    exit 1
fi
HELPER="$APP/Contents/MacOS/deckbridge-mic"
check test -x "$HELPER"
check test "$("$HELPER" version)" = 6
check test "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")" = com.deckbridge.mic-helper
check /usr/bin/codesign --verify --deep --strict "$APP"

# Modifier-only macOS shortcuts (including Dictation's double Fn/Globe)
# require flagsChanged events.  The release must also clear the modifier bit;
# an ordinary keyUp retaining Fn is accepted by CoreGraphics but ignored by
# the symbolic-hotkey recognizer.
check test "$("$HELPER" event-shape 63 down function)" = 'flags-changed|8388608'
check test "$("$HELPER" event-shape 63 up function)" = 'flags-changed|0'
check test "$("$HELPER" event-shape 2 down control,shift)" = 'key-down|393216'

set +e
web_url_usage="$("$HELPER" web-url 2>&1)"
web_url_usage_rc=$?
set -e
check test "$web_url_usage_rc" -eq 2
check sh -c 'case "$1" in *"web-url <bundle-id>"*) exit 0;; *) exit 1;; esac' sh "$web_url_usage"

# LaunchServices cannot reliably propagate a short-lived app's exit code. The
# helper therefore writes one atomic result file for its caller. Exercise only
# the read-only check command; no keyboard event is posted.
result_file="$TMP_DIR/check.result"
set +e
"$HELPER" --result "$result_file" check >/dev/null 2>&1
result_process_rc=$?
set -e
check test "$result_process_rc" -eq 0
check test -s "$result_file"
result_code="$(sed -n '1p' "$result_file")"
check sh -c '[ "$1" -eq 0 ] || [ "$1" -eq 4 ]' sh "$result_code"
set +e
front="$("$HELPER" frontmost 2>&1)"
front_rc=$?
set -e
if [ "$front_rc" -eq 0 ]; then
    check test "${front#*|}" != "$front"
else
    # Headless CI/sandbox contexts may have no NSWorkspace frontmost app. The
    # live Aqua LaunchAgent check covers the positive path.
    check sh -c '[ "$1" -eq 1 ] && case "$2" in *"frontmost app unavailable"*) exit 0;; *) exit 1;; esac' sh "$front_rc" "$front"
fi

# An untrusted helper returning 4 is expected in an isolated temporary bundle;
# a previously trusted test identity may return 0. Both prove bounded preflight.
set +e
check_out="$("$HELPER" check 2>&1)"
check_rc=$?
set -e
check sh -c '[ "$1" -eq 0 ] || [ "$1" -eq 4 ]' sh "$check_rc"
if [ "$check_rc" -eq 4 ]; then
    check sh -c 'case "$1" in *"Deckbridge Mic"*"Accessibility"*) exit 0;; *) exit 1;; esac' sh "$check_out"
else
    check test "$check_out" = ready=yes
fi

# Installer status uses the same helper identity and preserves infrastructure
# failures instead of mislabeling every nonzero result as a missing AX grant.
set +e
status_out="$(DECKBRIDGE_MIC_APP="$APP" DECKBRIDGE_MIC_HELPER="$HELPER" \
    "$ROOT/install_mic_helper.sh" status 2>&1)"
status_rc=$?
set -e
check sh -c '[ "$1" -eq 0 ] || [ "$1" -eq 4 ]' sh "$status_rc"
check sh -c 'case "$1" in *"accessibility=ready"*|*"accessibility=grant-required"*) exit 0;; *) exit 1;; esac' sh "$status_out"

no_result_open="$TMP_DIR/open-no-result"
printf '%s\n' '#!/bin/sh' 'exit 0' > "$no_result_open"
chmod +x "$no_result_open"
set +e
infra_out="$(DECKBRIDGE_MIC_APP="$APP" \
    DECKBRIDGE_MIC_OPEN="$no_result_open" \
    DECKBRIDGE_MIC_RESULT_TIMEOUT_TICKS=1 \
    "$ROOT/install_mic_helper.sh" status 2>&1)"
infra_rc=$?
set -e
check test "$infra_rc" -eq 7
check sh -c 'case "$1" in *"accessibility=check-failed (exit 7)"*) exit 0;; *) exit 1;; esac' sh "$infra_out"

printf '%s/%s passed\n' "$passed" "$total"
[ "$passed" -eq "$total" ]
