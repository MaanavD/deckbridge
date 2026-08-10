#!/usr/bin/env bash
# Linux/macOS smoke tests for mic_key.sh. The fake frontmost override keeps
# detection deterministic and prevents any GUI automation in the test suite.

set -u
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SCRIPT="$SCRIPT_DIR/mic_key.sh"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/deckbridge-mic-test.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM
export DECKBRIDGE_MIC_GESTURE_STATE="$TMP_DIR/mic-gesture"
# The developer machine may itself be at loginwindow while this runs. Keep all
# ordinary fixtures explicitly unlocked; the dedicated lock case overrides it.
export DECKBRIDGE_FAKE_SCREEN_LOCKED=0

passed=0
total=0

check() {
    total=$((total + 1))
    if "$@"; then
        passed=$((passed + 1))
    else
        printf 'FAIL:' >&2
        printf ' %s' "$@" >&2
        printf '\n' >&2
    fi
}

contains() {
    case "$1" in
        *"$2"*) return 0 ;;
        *) return 1 ;;
    esac
}

excludes() {
    ! contains "$1" "$2"
}

status_for() {
    DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST="$1" \
    DECKBRIDGE_FAKE_TERMINAL_PROCESS="${2:-}" \
    bash "$SCRIPT" --status
}

out="$(status_for 'Discord|com.hnc.Discord')"
check contains "$out" 'classification=discord'

out="$(status_for 'Ghostty|com.mitchellh.ghostty' claude)"
check contains "$out" 'classification=claude-code'

out="$(status_for 'iTerm2|com.googlecode.iterm2' codex)"
check contains "$out" 'classification=codex-cli'

# A different agent elsewhere on the machine is not evidence about the
# focused pane. Without an exact focused TTY/process, a terminal must use the
# universal dictation fallback rather than guessing Claude or Codex globally.
out="$(status_for 'cmux|com.cmuxterm.app')"
check contains "$out" 'classification=terminal-unknown'

out="$(DECKBRIDGE_FAKE_HERDR_AGENT=claude \
    status_for 'Terminal|com.apple.Terminal')"
check contains "$out" 'classification=herdr'

out="$(DECKBRIDGE_FAKE_HERDR_AGENT=codex \
    status_for 'Terminal|com.apple.Terminal')"
check contains "$out" 'classification=herdr'

out="$(DECKBRIDGE_FAKE_HERDR_AGENT=claude \
    DECKBRIDGE_FAKE_FRONTMOST='Terminal|com.apple.Terminal' \
    DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    bash "$SCRIPT" --dry-run)"
check contains "$out" 'classification=herdr'
check contains "$out" 'Start Dictation'

out="$(status_for 'Claude|com.anthropic.claude')"
check contains "$out" 'classification=claude-desktop'

out="$(status_for 'Cursor|com.todesktop.230313mzl4w4u92')"
check contains "$out" 'classification=cursor'

# Production calls must cross LaunchServices so TCC evaluates the granted app
# identity rather than an interactive terminal or the launchd parent chain.
# A fake `open` replays the app request/result protocol without any GUI or key.
ls_app="$TMP_DIR/LS Deckbridge Mic.app"
ls_helper="$ls_app/Contents/MacOS/deckbridge-mic"
mkdir -p "$(dirname "$ls_helper")"
printf '%s\n' \
    '#!/bin/sh' \
    'case "$1" in' \
    '  frontmost) printf "Cursor|com.todesktop.230313mzl4w4u92|321\n" ;;' \
    '  check) printf "ready=yes\n" ;;' \
    '  request-access) printf "grant requested\n"; exit 4 ;;' \
    '  *) exit 2 ;;' \
    'esac' > "$ls_helper"
chmod +x "$ls_helper"
fake_open="$TMP_DIR/open"
open_log="$TMP_DIR/open.log"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'while [ "$#" -gt 0 ] && [ "$1" != --args ]; do shift; done' \
    '[ "${1:-}" = --args ] || exit 2' \
    'shift' \
    '[ "${1:-}" = --result ] || exit 2' \
    'result="$2"; shift 2' \
    'printf "%s|%s\\n" "$result" "$*" >> "$DECKBRIDGE_FAKE_OPEN_LOG"' \
    'set +e' \
    'output="$("$DECKBRIDGE_FAKE_LS_HELPER" "$@" 2>&1)"' \
    'rc=$?' \
    'set -e' \
    'tmp="${result}.tmp.$$"' \
    'printf "%s\\n%s\\n" "$rc" "$output" > "$tmp"' \
    'mv "$tmp" "$result"' > "$fake_open"
chmod +x "$fake_open"
out="$(DECKBRIDGE_MIC_APP="$ls_app" \
    DECKBRIDGE_MIC_OPEN="$fake_open" \
    DECKBRIDGE_FAKE_OPEN_LOG="$open_log" \
    DECKBRIDGE_FAKE_LS_HELPER="$ls_helper" \
    bash "$SCRIPT" --status)"
check contains "$out" 'classification=cursor'
check contains "$(cat "$open_log" 2>/dev/null || true)" 'frontmost'
out="$(DECKBRIDGE_MIC_APP="$ls_app" \
    DECKBRIDGE_MIC_OPEN="$fake_open" \
    DECKBRIDGE_FAKE_OPEN_LOG="$open_log" \
    DECKBRIDGE_FAKE_LS_HELPER="$ls_helper" \
    DECKBRIDGE_FAKE_FRONTMOST='Cursor|com.todesktop.230313mzl4w4u92' \
    bash "$SCRIPT" --check)"
check contains "$out" 'ready=yes'
check contains "$(cat "$open_log" 2>/dev/null || true)" 'check'

# Two simultaneous checks get independent result paths and cannot consume or
# overwrite one another's status.
: > "$open_log"
DECKBRIDGE_MIC_APP="$ls_app" \
DECKBRIDGE_MIC_OPEN="$fake_open" \
DECKBRIDGE_FAKE_OPEN_LOG="$open_log" \
DECKBRIDGE_FAKE_LS_HELPER="$ls_helper" \
bash "$SCRIPT" --helper-check > "$TMP_DIR/concurrent-1" &
concurrent_pid_1=$!
DECKBRIDGE_MIC_APP="$ls_app" \
DECKBRIDGE_MIC_OPEN="$fake_open" \
DECKBRIDGE_FAKE_OPEN_LOG="$open_log" \
DECKBRIDGE_FAKE_LS_HELPER="$ls_helper" \
bash "$SCRIPT" --helper-check > "$TMP_DIR/concurrent-2" &
concurrent_pid_2=$!
wait "$concurrent_pid_1"; concurrent_rc_1=$?
wait "$concurrent_pid_2"; concurrent_rc_2=$?
check test "$concurrent_rc_1" -eq 0
check test "$concurrent_rc_2" -eq 0
check contains "$(cat "$TMP_DIR/concurrent-1")" 'ready=yes'
check contains "$(cat "$TMP_DIR/concurrent-2")" 'ready=yes'
unique_results="$(sed -n 's/|.*//p' "$open_log" | sort -u | wc -l | tr -d ' ')"
check test "$unique_results" -eq 2

# A LaunchServices success without an app result is not readiness. Bound the
# wait tightly in this fixture and surface an infrastructure error.
no_result_open="$TMP_DIR/open-no-result"
printf '%s\n' '#!/bin/sh' 'exit 0' > "$no_result_open"
chmod +x "$no_result_open"
no_result_out="$(DECKBRIDGE_MIC_APP="$ls_app" \
    DECKBRIDGE_MIC_OPEN="$no_result_open" \
    DECKBRIDGE_MIC_RESULT_TIMEOUT_TICKS=1 \
    bash "$SCRIPT" --helper-check 2>&1)"
no_result_rc=$?
check test "$no_result_rc" -eq 7
check contains "$no_result_out" 'did not return a result'

# Malformed app output also remains unavailable instead of being interpreted
# as an accidental successful check.
invalid_open="$TMP_DIR/open-invalid"
printf '%s\n' \
    '#!/bin/sh' \
    'while [ "$#" -gt 0 ] && [ "$1" != --args ]; do shift; done' \
    'shift' \
    '[ "$1" = --result ] || exit 2' \
    'printf "not-a-status\\n" > "$2"' > "$invalid_open"
chmod +x "$invalid_open"
invalid_out="$(DECKBRIDGE_MIC_APP="$ls_app" \
    DECKBRIDGE_MIC_OPEN="$invalid_open" \
    bash "$SCRIPT" --helper-check 2>&1)"
invalid_rc=$?
check test "$invalid_rc" -eq 7
check contains "$invalid_out" 'invalid result'

# The explicit consent path propagates the helper's not-yet-trusted status and
# is only invoked on demand; ordinary checks above never request a prompt.
request_out="$(DECKBRIDGE_MIC_APP="$ls_app" \
    DECKBRIDGE_MIC_OPEN="$fake_open" \
    DECKBRIDGE_FAKE_OPEN_LOG="$open_log" \
    DECKBRIDGE_FAKE_LS_HELPER="$ls_helper" \
    bash "$SCRIPT" --request-access 2>&1)"
request_rc=$?
check test "$request_rc" -eq 4
check contains "$request_out" 'grant requested'
check contains "$(cat "$open_log")" 'request-access'

out="$(status_for 'Notes|com.apple.Notes')"
check contains "$out" 'classification=other'

out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
    bash "$SCRIPT" --dry-run)"
check contains "$out" 'Start Dictation'
check contains "$out" 'hotkey fn,fn fallback'

# A dry-run must not execute a configured shell command.
marker="$TMP_DIR/dry-run-marker"
printf 'other=touch %s\n' "$marker" > "$TMP_DIR/dry.conf"
out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/dry.conf" \
    DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
    bash "$SCRIPT" --dry-run)"
check contains "$out" 'classification=other'
check contains "$out" 'command_would_run='
check test ! -e "$marker"

# The same override executes only in normal mode, proving the selected action
# came from the target table rather than the built-in default.
printf 'other=touch %s\n' "$marker" > "$TMP_DIR/exec.conf"
DECKBRIDGE_MIC_CONFIG="$TMP_DIR/exec.conf" \
DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
bash "$SCRIPT" >/dev/null 2>&1
check test -e "$marker"

# Preflight is read-only: it reports macOS setup without firing a key event.
out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
    DECKBRIDGE_FAKE_DICTATION_ENABLED=0 \
    DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED=0 \
    bash "$SCRIPT" --check 2>&1 || true)"
check contains "$out" 'ready=no'
check contains "$out" 'Keyboard > Dictation'
check contains "$out" 'Accessibility'

out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
    DECKBRIDGE_FAKE_DICTATION_ENABLED=1 \
    DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED=1 \
    bash "$SCRIPT" --check 2>&1)"
check contains "$out" 'ready=yes'

# Cursor Agents Window voice is a stateful hold gesture, not an instantaneous
# shortcut.  The helper receives explicit down/up events while --dry-run stays
# silent and still exposes the selected target/action.
helper_log="$TMP_DIR/helper.log"
fake_helper="$TMP_DIR/deckbridge-mic"
printf '%s\n' \
    '#!/bin/sh' \
    'if [ "$1" = check ]; then exit 0; fi' \
    'case "$1" in *-dictation) [ "${DECKBRIDGE_FAKE_NO_DICTATION_MENU:-0}" = 1 ] && exit 5 ;; esac' \
    'printf "%s\\n" "$*" >> "$DECKBRIDGE_FAKE_HELPER_LOG"' > "$fake_helper"
chmod +x "$fake_helper"

# The production helper lives inside "Deckbridge Mic.app".  An unquoted
# command-position expansion splits that path at the space, silently loses the
# NSWorkspace result, and classifies every focused app as `other` even though
# the helper itself is healthy.
spaced_helper_dir="$TMP_DIR/Deckbridge Mic.app/Contents/MacOS"
mkdir -p "$spaced_helper_dir"
spaced_helper="$spaced_helper_dir/deckbridge-mic"
printf '%s\n' \
    '#!/bin/sh' \
    'case "$1" in' \
    '  frontmost) printf "Cursor|com.todesktop.230313mzl4w4u92|123\n" ;;' \
    '  check) printf "ready=yes\n" ;;' \
    '  *) exit 2 ;;' \
    'esac' > "$spaced_helper"
chmod +x "$spaced_helper"
out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_SCREEN_LOCKED=0 \
    DECKBRIDGE_MIC_HELPER="$spaced_helper" \
    bash "$SCRIPT" --status)"
check contains "$out" 'classification=cursor'

out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Cursor|com.todesktop.230313mzl4w4u92' \
    DECKBRIDGE_MIC_HELPER="$fake_helper" \
    DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
    bash "$SCRIPT" --dry-run)"
check contains "$out" 'classification=cursor'
check contains "$out" 'hold Control+M'
check test ! -e "$helper_log"

out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Cursor|com.todesktop.230313mzl4w4u92' \
    DECKBRIDGE_MIC_HELPER="$fake_helper" \
    DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED=0 \
    bash "$SCRIPT" --check 2>&1 || true)"
check contains "$out" 'ready=no'
check contains "$out" 'Accessibility'

out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Cursor|com.todesktop.230313mzl4w4u92' \
    DECKBRIDGE_MIC_HELPER="$fake_helper" \
    DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
    bash "$SCRIPT" --press)"
check contains "$out" 'gesture=hold'
check contains "$(cat "$helper_log")" 'key-down 59 control'
check contains "$(cat "$helper_log")" 'key-down 46 control'

: > "$helper_log"
DECKBRIDGE_MIC_HELPER="$fake_helper" \
DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
bash "$SCRIPT" --release >/dev/null
check contains "$(cat "$helper_log")" 'key-up 46 control'
check contains "$(cat "$helper_log")" 'key-up 59 none'

# Codex desktop uses Command+Shift+D as a press-to-start/press-to-stop toggle.
# This is deliberately distinct from Codex CLI, which has no native mic chord.
: > "$helper_log"
out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Codex|com.openai.codex' \
    DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED=1 \
    DECKBRIDGE_MIC_HELPER="$fake_helper" \
    DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
    bash "$SCRIPT" --press)"
check contains "$out" 'gesture=hold'
check contains "$(cat "$helper_log")" 'tap 2 command,shift'
: > "$helper_log"
DECKBRIDGE_MIC_HELPER="$fake_helper" \
DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
bash "$SCRIPT" --release >/dev/null
check contains "$(cat "$helper_log")" 'tap 2 command,shift'

# Universal voice uses the configured macOS Dictation shortcut. AXPress can
# return success for Start Dictation without DictationIM actually launching.
: > "$helper_log"
out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
    DECKBRIDGE_FAKE_DICTATION_ENABLED=1 \
    DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED=1 \
    DECKBRIDGE_MIC_HELPER="$fake_helper" \
    DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
    bash "$SCRIPT" --press)"
check contains "$out" 'gesture=hold'
check test "$(grep -c '^tap 63 function$' "$helper_log")" -eq 2
check excludes "$(cat "$helper_log")" 'start-dictation'
: > "$helper_log"
DECKBRIDGE_MIC_HELPER="$fake_helper" \
DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
bash "$SCRIPT" --release >/dev/null
check test "$(grep -c '^tap 63 function$' "$helper_log")" -eq 2

: > "$helper_log"
DECKBRIDGE_DICTATION_HOTKEY=fn,fn \
DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
DECKBRIDGE_FAKE_DICTATION_ENABLED=1 \
DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED=1 \
DECKBRIDGE_FAKE_NO_DICTATION_MENU=1 \
DECKBRIDGE_MIC_HELPER="$fake_helper" \
DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
bash "$SCRIPT" --press >/dev/null
check test "$(grep -c '^tap 63 function$' "$helper_log")" -eq 2
: > "$helper_log"
DECKBRIDGE_DICTATION_HOTKEY=fn,fn \
DECKBRIDGE_FAKE_NO_DICTATION_MENU=1 \
DECKBRIDGE_MIC_HELPER="$fake_helper" \
DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
bash "$SCRIPT" --release >/dev/null
check test "$(grep -c '^tap 63 function$' "$helper_log")" -eq 2

# Claude Code's hold mode maps to a real Space key-down/key-up pair.
: > "$helper_log"
DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
DECKBRIDGE_FAKE_FRONTMOST='Ghostty|com.mitchellh.ghostty' \
DECKBRIDGE_FAKE_TERMINAL_PROCESS=claude \
DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED=1 \
DECKBRIDGE_MIC_HELPER="$fake_helper" \
DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
bash "$SCRIPT" --press >/dev/null
DECKBRIDGE_MIC_HELPER="$fake_helper" \
DECKBRIDGE_FAKE_HELPER_LOG="$helper_log" \
bash "$SCRIPT" --release >/dev/null
check contains "$(cat "$helper_log")" 'key-down 49 none'
check contains "$(cat "$helper_log")" 'key-up 49 none'
check excludes "$(cat "$helper_log")" 'tap 49 none'

# A locked login session is not an Accessibility setup failure. The check must
# stop before the native helper, explain the transient fix, and use a distinct
# exit code so the connector can show the right face.
locked_marker="$TMP_DIR/locked-helper-marker"
locked_helper="$TMP_DIR/locked-helper"
printf '%s\n' '#!/bin/sh' "touch '$locked_marker'" 'exit 99' > "$locked_helper"
chmod +x "$locked_helper"
out="$(DECKBRIDGE_MIC_HELPER="$locked_helper" \
    DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_SCREEN_LOCKED=1 \
    bash "$SCRIPT" --check 2>&1)"
locked_rc=$?
check test "$locked_rc" -eq 5
check contains "$out" 'session_locked=yes'
check contains "$out" 'Unlock the Mac'
check contains "$out" 'ready=no'
check test ! -e "$locked_marker"

# --check may inspect setup but must never execute even an explicit custom
# action. This is the hard safety boundary that keeps health checks silent.
check_marker="$TMP_DIR/check-marker"
printf 'other=touch %s\n' "$check_marker" > "$TMP_DIR/check.conf"
out="$(DECKBRIDGE_MIC_CONFIG="$TMP_DIR/check.conf" \
    DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
    bash "$SCRIPT" --check 2>&1)"
check contains "$out" 'ready=yes'
check test ! -e "$check_marker"

# Current macOS Keyboard Settings reads the assistant-support Dictation flag;
# AppleDictationAutoEnable is a legacy/auto-enable preference and can disagree.
# The modern explicit enabled flag must win, with the old key only a fallback.
fake_bin="$TMP_DIR/fake-bin"
mkdir -p "$fake_bin"
printf '%s\n' \
    '#!/bin/sh' \
    'case "$2" in' \
    '  com.apple.assistant.support) printf "1\\n" ;;' \
    '  com.apple.HIToolbox) printf "0\\n" ;;' \
    '  *) exit 1 ;;' \
    'esac' > "$fake_bin/defaults"
chmod +x "$fake_bin/defaults"
out="$(PATH="$fake_bin:$PATH" \
    DECKBRIDGE_MIC_CONFIG="$TMP_DIR/no-config" \
    DECKBRIDGE_FAKE_FRONTMOST='Notes|com.apple.Notes' \
    DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED=1 \
    bash "$SCRIPT" --check 2>&1 || true)"
check contains "$out" 'ready=yes'

printf '%s/%s passed\n' "$passed" "$total"
[ "$passed" -eq "$total" ]
