#!/usr/bin/env bash
# deckbridge microphone key: start voice input for the currently focused app.
#
# Permissions on macOS:
#   * Privacy & Security > Accessibility: allow the stable `Deckbridge Mic.app`
#     helper installed by `./mic_key.sh --install-helper`. The native helper
#     posts keyboard events directly; Deckbridge does not need System Events or
#     Automation permission.
#   * Privacy & Security > Microphone: allow Dictation and the target app to use
#     the microphone. Without it, macOS shows no usable recording/input state or
#     the target app reports a microphone permission error.
#   * Secure Input (password fields, some terminals, and other protected input)
#     can prevent synthetic key events from reaching the focused app. In that
#     case --status still detects the app, but the action may be ignored.
#
# RESEARCH STATUS (checked 2026-08-05):
# VERIFIED: Claude Code voice dictation is documented as hold/tap Space,
# https://docs.anthropic.com/en/docs/claude-code/interactive-mode
# VERIFIED: Claude Desktop/web voice mode has an in-app sound-wave button, but
# the official help page does not document a desktop keyboard shortcut; this
# script therefore uses macOS Dictation for claude-desktop by default.
# https://support.anthropic.com/en/articles/11101966-using-voice-mode-on-claude-mobile-apps
# Codex desktop uses Command+Shift+D for dictation on macOS. This is verified
# against the installed app and kept separate from Codex CLI, which exposes no
# native voice shortcut in OpenAI's CLI documentation.
# https://developers.openai.com/codex/cli
# UNVERIFIED/NOT DOCUMENTED: Codex CLI has no official native voice/dictation
# feature, slash command, or hotkey in the CLI documentation. It falls back to
# macOS Dictation rather than fabricating a Codex CLI shortcut.
# https://developers.openai.com/codex/cli
# VERIFIED: macOS Dictation starts from Edit > Start Dictation; Apple also
# documents the Keyboard > Dictation shortcut and the Microphone key.
# This script prefers the Edit menu and falls back to Left Command twice.
# https://support.apple.com/guide/mac-help/use-dictation-mh40584/mac
# VERIFIED: cmux is a native macOS terminal with scriptable panes and supports
# terminal agents; process inspection below is deliberately best-effort.
# https://github.com/manaflow-ai/cmux
#
# VERIFIED LOCALLY: the fake-frontmost path, config table, dry-run, status, and
# native-helper no-event preflight are exercised by test_mic_key.sh.
# Native frontmost-app lookup uses NSWorkspace, documented here:
# https://developer.apple.com/documentation/appkit/nsworkspace/frontmostapplication
# Focused terminal classification is accepted only from an exact selected TTY
# (currently cmux's active surface) plus that TTY's process list. A machine-wide
# pgrep is deliberately forbidden: a Codex process in tab
# A says nothing about the foreground tab B and used to select the wrong action.
# cmux documents its scriptable panes here:
# https://github.com/manaflow-ai/cmux
# Key codes 49/59/63/96 and modifier chords are posted by Deckbridge Mic with
# CoreGraphics. Secure Input and Accessibility permissions can still block them.
#
# Config: ~/.deckbridge/mic_targets.conf (or DECKBRIDGE_MIC_CONFIG), one
# target=command line per entry. Built-in action names are `dictation`,
# `keycode:N`, and `keycode:N+modifier+modifier`; any other value is executed
# as an explicit /bin/sh command. Example:
#   other=logger -t deckbridge "mic key"
#   dictation_hotkey=cmd,cmd
# Hotkey names supported: cmd, ctrl, fn, mic, f5, or a numeric key code.
# DECKBRIDGE_DICTATION_HOTKEY overrides dictation_hotkey.

set -u

CONFIG_FILE="${DECKBRIDGE_MIC_CONFIG:-$HOME/.deckbridge/mic_targets.conf}"
FRONT_APP=""
FRONT_BUNDLE=""
FRONT_PID=""
TARGET=""
IS_CODEX_DESKTOP=0
SESSION_LOCKED=0

ACTION_DISCORD="dictation"
# Claude Code hold mode maps directly to the Stream Deck's physical edges:
# key-down on press, key-up on release. Leave the prompt input empty so Claude
# interprets held Space as voice rather than text input.
# Needs a Claude.ai login; unavailable with a raw API key, Bedrock, or Vertex.
ACTION_CLAUDE_CODE="key-hold:49"
# Codex CLI has NO documented native voice/dictation feature, so it falls back
# to macOS system dictation like any other terminal app.
ACTION_CODEX_CLI="dictation"
ACTION_CLAUDE_DESKTOP="dictation"
ACTION_CURSOR="cursor-hold"
ACTION_TERMINAL_UNKNOWN="dictation"
ACTION_OTHER="dictation"
# Prefer Edit > Start Dictation. The Keyboard shortcut is only a fallback, and
# this repo defaults that fallback to Left Command twice. Command/Control/Fn
# twice still decode from enabled symbolic hotkey 164. Disabled or missing 164
# is not the hardware microphone key. Override with mic_targets.conf or
# DECKBRIDGE_DICTATION_HOTKEY.
CONFIG_DICTATION_HOTKEY=""
DEFAULT_DICTATION_HOTKEY="cmd,cmd"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
MIC_HELPER_APP="${DECKBRIDGE_MIC_APP:-$HOME/Applications/Deckbridge Mic.app}"
MIC_HELPER="${DECKBRIDGE_MIC_HELPER:-$MIC_HELPER_APP/Contents/MacOS/deckbridge-mic}"
MIC_HELPER_INSTALLER="${DECKBRIDGE_MIC_INSTALLER:-$SCRIPT_DIR/install_mic_helper.sh}"
MIC_OPEN_WAS_SET="${DECKBRIDGE_MIC_OPEN+x}"
MIC_OPEN="${DECKBRIDGE_MIC_OPEN:-/usr/bin/open}"
MIC_GESTURE_STATE="${DECKBRIDGE_MIC_GESTURE_STATE:-$HOME/.deckbridge/mic_gesture}"
DICTATION_RESULT_DIR="${DECKBRIDGE_DICTATION_RESULT_DIR:-$HOME/.deckbridge/dictation-results}"
MIC_HS_SEQ=0

trim() {
    printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

# Invoke the signed app binary directly in production. This is verified from
# the target user's launchd context and preserves the helper's Accessibility
# grant. LaunchServices `open -n` caused macOS to evaluate a fresh application
# instance and falsely report setup-required even while the same signed binary
# returned ready=yes. The older result-file route remains as an explicit
# DECKBRIDGE_MIC_OPEN compatibility/test seam.
# HID posts still fall back to Hammerspoon when launchd is the responsible
# parent: macOS can deny the same trusted helper in that chain, which is why
# T3 focus already uses hammerspoon:// URL events.
run_helper() {
    local result_dir result_file ticks max_ticks helper_rc helper_detail
    if [ -n "${DECKBRIDGE_MIC_HELPER:-}" ] || [ -z "$MIC_OPEN_WAS_SET" ]; then
        "$MIC_HELPER" "$@"
        return $?
    fi
    [ -x "$MIC_HELPER" ] || return 6
    [ -x "$MIC_OPEN" ] || return 7
    result_dir="$(mktemp -d "${TMPDIR:-/tmp}/deckbridge-mic-call.XXXXXX")" || return 7
    chmod 700 "$result_dir" 2>/dev/null || true
    result_file="$result_dir/result"
    if ! "$MIC_OPEN" -g -n "$MIC_HELPER_APP" --args \
            --result "$result_file" "$@" >/dev/null 2>&1; then
        rm -rf "$result_dir"
        printf 'Deckbridge Mic could not be launched through LaunchServices.\n' >&2
        return 7
    fi
    ticks=0
    max_ticks="${DECKBRIDGE_MIC_RESULT_TIMEOUT_TICKS:-60}"
    while [ ! -s "$result_file" ] && [ "$ticks" -lt "$max_ticks" ]; do
        sleep 0.05
        ticks=$((ticks + 1))
    done
    if [ ! -s "$result_file" ]; then
        rm -rf "$result_dir"
        printf 'Deckbridge Mic did not return a result within 3 seconds.\n' >&2
        return 7
    fi
    helper_rc="$(sed -n '1p' "$result_file")"
    case "$helper_rc" in
        ''|*[!0-9]*)
            rm -rf "$result_dir"
            printf 'Deckbridge Mic returned an invalid result.\n' >&2
            return 7
            ;;
    esac
    helper_detail="$(sed '1d' "$result_file")"
    rm -rf "$result_dir"
    if [ -n "$helper_detail" ]; then
        if [ "$helper_rc" -eq 0 ]; then
            printf '%s\n' "$helper_detail"
        else
            printf '%s\n' "$helper_detail" >&2
        fi
    fi
    if [ "$helper_rc" -gt 255 ]; then
        printf 'Deckbridge Mic returned an invalid exit status.\n' >&2
        return 7
    fi
    return "$helper_rc"
}

hammerspoon_gui_ready() {
    fake="${DECKBRIDGE_FAKE_HAMMERSPOON:-}"
    if [ -n "$fake" ]; then
        [ "$fake" = "1" ]
        return $?
    fi
    [ "${DECKBRIDGE_DISABLE_HAMMERSPOON:-0}" != 1 ] || return 1
    pgrep -x Hammerspoon >/dev/null 2>&1
}

hammerspoon_invoke() {
    local op="$1" code="${2:-}" flags="${3:-none}" bundle="${4:-}"
    local request result_file polls selected url hotkey
    hammerspoon_gui_ready || return 1
    [ -x "$MIC_OPEN" ] || return 1
    MIC_HS_SEQ=$((MIC_HS_SEQ + 1))
    request="$$-$(date +%s)-$MIC_HS_SEQ"
    mkdir -p "$DICTATION_RESULT_DIR" || return 1
    result_file="$DICTATION_RESULT_DIR/$request"
    rm -f "$result_file"
    url="hammerspoon://deckbridge-dictation?op=${op}&request=${request}"
    case "$op" in
        tap|key-down|key-up)
            url="${url}&code=${code}&flags=${flags}"
            ;;
        tap-mic)
            ;;
        start-dictation|stop-dictation|toggle-dictation)
            hotkey="$(dictation_hotkey)"
            url="${url}&hotkey=${hotkey}"
            ;;
        focus-text-entry)
            url="${url}&bundle=${bundle}"
            ;;
        *) return 1 ;;
    esac
    "$MIC_OPEN" -g -a Hammerspoon "$url" >/dev/null 2>&1 || return 1
    polls=0
    while [ "$polls" -lt "${DECKBRIDGE_MIC_RESULT_TIMEOUT_TICKS:-60}" ]; do
        if [ -f "$result_file" ]; then
            selected="$(cat "$result_file" 2>/dev/null || true)"
            rm -f "$result_file"
            case "$selected" in
                ok) return 0 ;;
            esac
            return 1
        fi
        polls=$((polls + 1))
        sleep 0.05
    done
    return 1
}

# Post a key through Deckbridge Mic when launchd still inherits that grant.
# Exit 4 means macOS denied the helper for this parent; Hammerspoon's GUI
# identity can still synthesize the same HID events.
run_hid() {
    local command="$1" code="$2" flags="$3" err rc
    err="$(mktemp "${TMPDIR:-/tmp}/deckbridge-mic-hid.XXXXXX")" || return 7
    run_helper "$command" "$code" "$flags" >"$err" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        [ -s "$err" ] && cat "$err"
        rm -f "$err"
        return 0
    fi
    if [ "$rc" -eq 4 ] && hammerspoon_invoke "$command" "$code" "$flags"; then
        rm -f "$err"
        return 0
    fi
    cat "$err" >&2
    rm -f "$err"
    return "$rc"
}

run_hid_cmd() {
    local command="$1" err rc
    err="$(mktemp "${TMPDIR:-/tmp}/deckbridge-mic-hid.XXXXXX")" || return 7
    run_helper "$command" >"$err" 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        [ -s "$err" ] && cat "$err"
        rm -f "$err"
        return 0
    fi
    if [ "$rc" -eq 4 ] && hammerspoon_invoke "$command"; then
        rm -f "$err"
        return 0
    fi
    cat "$err" >&2
    rm -f "$err"
    return "$rc"
}

focus_t3_prompt() {
    [ "$TARGET" = "t3code" ] || return 0
    if run_helper focus-text-entry com.t3tools.t3code >/dev/null 2>&1; then
        return 0
    fi
    hammerspoon_invoke focus-text-entry "" none com.t3tools.t3code || true
    return 0
}

load_config() {
    [ -f "$CONFIG_FILE" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="$(trim "$line")"
        case "$line" in
            ''|'#'*) continue ;;
        esac
        key="$(trim "${line%%=*}")"
        value="${line#*=}"
        case "$key" in
            discord) ACTION_DISCORD="$value" ;;
            claude-code) ACTION_CLAUDE_CODE="$value" ;;
            codex-cli) ACTION_CODEX_CLI="$value" ;;
            claude-desktop) ACTION_CLAUDE_DESKTOP="$value" ;;
            cursor) ACTION_CURSOR="$value" ;;
            terminal-unknown) ACTION_TERMINAL_UNKNOWN="$value" ;;
            other) ACTION_OTHER="$value" ;;
            dictation_hotkey) CONFIG_DICTATION_HOTKEY="$value" ;;
        esac
    done < "$CONFIG_FILE"
}

# Read the console-session lock bit without asking System Events. GUI scripting
# is expected to fail behind loginwindow, and treating that expected failure as
# a permanent Accessibility problem leaves the deck falsely red after unlock.
# ioreg is read-only and available before any user GUI session is interactive.
screen_locked() {
    fake="${DECKBRIDGE_FAKE_SCREEN_LOCKED:-}"
    if [ -n "$fake" ]; then
        [ "$fake" = "1" ]
        return $?
    fi
    ioreg_bin=""
    if [ -x /usr/sbin/ioreg ]; then
        ioreg_bin=/usr/sbin/ioreg
    elif command -v ioreg >/dev/null 2>&1; then
        ioreg_bin="$(command -v ioreg)"
    else
        return 1
    fi
    session_info="$($ioreg_bin -n Root -d1 2>/dev/null || true)"
    case "$session_info" in
        *'"CGSSessionScreenIsLocked"=Yes'*) return 0 ;;
        *) return 1 ;;
    esac
}

# Frontmost detection is delegated to the stable native helper's NSWorkspace
# query. This needs neither Accessibility nor System Events/Automation.
detect_frontmost() {
    fake="${DECKBRIDGE_FAKE_FRONTMOST:-}"
    if [ -n "$fake" ]; then
        FRONT_APP="${fake%%|*}"
        FRONT_BUNDLE="${fake#*|}"
        return 0
    fi

    FRONT_APP="Unknown"
    FRONT_BUNDLE="unknown"
    if [ -x "$MIC_HELPER" ]; then
        front="$(run_helper frontmost 2>/dev/null || true)"
        if [ -n "$front" ] && [ "${front#*|}" != "$front" ]; then
            FRONT_APP="${front%%|*}"
            front_rest="${front#*|}"
            FRONT_BUNDLE="${front_rest%%|*}"
            FRONT_PID="${front_rest#*|}"
        fi
    fi
}

terminal_name() {
    name_lc="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    case "$name_lc" in
        ghostty|iterm2|terminal|terminal.app|wezterm|kitty|alacritty|"visual studio code"|code|cmux|warp)
            return 0 ;;
        *) return 1 ;;
    esac
}

# Return the selected terminal's exact TTY, or nothing when the terminal cannot
# identify it. An empty answer is intentional: macOS Dictation is a safe
# universal fallback, while guessing another tab's Claude/Codex process is not.
focused_terminal_tty() {
    fake_tty="${DECKBRIDGE_FAKE_TERMINAL_TTY:-}"
    if [ -n "$fake_tty" ]; then
        printf '%s\n' "$fake_tty"
        return 0
    fi

    app_lc="$(printf '%s' "$FRONT_APP" | tr '[:upper:]' '[:lower:]')"
    case "$app_lc" in
        cmux)
            if command -v cmux >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
                tree="$(cmux --id-format both tree --all --json 2>/dev/null || true)"
                if [ -n "$tree" ]; then
                    printf '%s' "$tree" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    target = str(data.get("active", {}).get("surface_id") or "")
    def walk(value):
        if isinstance(value, dict):
            if str(value.get("id") or "") == target and value.get("tty"):
                return str(value["tty"])
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return ""
    print(walk(data))
except Exception:
    pass
' 2>/dev/null
                fi
            fi
            return 0
            ;;
    esac

    # Some terminal apps attach fd 0 to their selected pty. Only a real /dev
    # tty is accepted; /dev/null and pipes convey no focused-pane identity.
    if [ -n "$FRONT_PID" ] && command -v lsof >/dev/null 2>&1; then
        lsof -a -p "$FRONT_PID" -d 0 -Fn 2>/dev/null \
            | sed -n 's#^n/dev/\(tty[^ ]*\)$#\1#p' | head -n 1
    fi
}

# Herdr's visible TUI is the terminal process, while Claude/Codex run behind
# its persistent server and therefore do not appear in the selected tty's local
# process list. Herdr exposes the selected pane and its authoritative `agent`
# field; use that only for the default Terminal viewer and only while an
# interactive (non-server) Herdr client is actually attached.
herdr_selected_agent() {
    fake_agent="${DECKBRIDGE_FAKE_HERDR_AGENT:-}"
    if [ -n "$fake_agent" ]; then
        case "$fake_agent" in claude|codex) printf '%s\n' "$fake_agent" ;; esac
        return 0
    fi

    app_lc="$(printf '%s' "$FRONT_APP" | tr '[:upper:]' '[:lower:]')"
    case "$app_lc" in terminal|terminal.app) ;; *) return 1 ;; esac
    command -v herdr >/dev/null 2>&1 || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    command -v ps >/dev/null 2>&1 || return 1
    if ! ps -axo tty=,command= 2>/dev/null | awk '
        $1 !~ /^\?/ {
            $1 = ""
            if ($0 ~ /(^|[\/:[:space:]])herdr([[:space:]]|$)/) found = 1
        }
        END { exit(found ? 0 : 1) }
    '; then
        return 1
    fi

    current="$(herdr pane current 2>/dev/null || true)"
    [ -n "$current" ] || return 1
    printf '%s' "$current" | python3 -c '
import json, sys
try:
    pane = (json.load(sys.stdin).get("result") or {}).get("pane") or {}
    agent = str(pane.get("agent") or "").lower()
    if agent in ("claude", "codex"):
        print(agent)
except Exception:
    pass
' 2>/dev/null
}

# Return claude or codex only when the focused terminal's exact TTY process
# list says so. No machine-wide fallback is allowed.
detect_terminal_cli() {
    fake_process="${DECKBRIDGE_FAKE_TERMINAL_PROCESS:-}"
    if [ -n "$fake_process" ]; then
        printf '%s\n' "$fake_process"
        return 0
    fi

    herdr_agent="$(herdr_selected_agent 2>/dev/null || true)"
    case "$herdr_agent" in
        claude|codex) printf '%s\n' "$herdr_agent"; return 0 ;;
    esac

    # Fake app detection is a deterministic test seam; never let it leak into
    # the user's real cmux/Terminal state when no fake process was supplied.
    if [ -n "${DECKBRIDGE_FAKE_FRONTMOST:-}" ]; then
        printf '%s\n' ""
        return 0
    fi

    tty_name="$(focused_terminal_tty)"
    tty_name="${tty_name#/dev/}"
    if [ -n "$tty_name" ] && command -v ps >/dev/null 2>&1; then
        terminal_processes="$(ps -t "$tty_name" -o command= 2>/dev/null || true)"
        case "$terminal_processes" in
            *[cC][lL][aA][uU][dD][e]*) printf 'claude\n'; return 0 ;;
            *[cC][oO][dD][eE][xX]*) printf 'codex\n'; return 0 ;;
        esac
    fi

    printf '%s\n' ""
}

classify_target() {
    app_lc="$(printf '%s' "$FRONT_APP" | tr '[:upper:]' '[:lower:]')"
    bundle_lc="$(printf '%s' "$FRONT_BUNDLE" | tr '[:upper:]' '[:lower:]')"

    case "$app_lc|$bundle_lc" in
        *discord*|*com.hnc.discord*) TARGET="discord"; return 0 ;;
    esac

    case "$app_lc|$bundle_lc" in
        *"t3 code"*|*"|"*com.t3tools.t3code*) TARGET="t3code"; return 0 ;;
    esac

    case "$app_lc" in
        *"claude code"*) TARGET="claude-code"; return 0 ;;
    esac
    case "$bundle_lc" in
        *claude.code*) TARGET="claude-code"; return 0 ;;
    esac

    if terminal_name "$FRONT_APP"; then
        # Herdr forwards terminal bytes, not physical key hold/release
        # semantics. Claude's native Space hold therefore degrades to typed
        # spaces inside its TUI. System Dictation operates at the focused
        # Terminal text-input layer and preserves Deckbridge's press/release
        # lifecycle for both Claude and Codex panes.
        herdr_cli="$(herdr_selected_agent 2>/dev/null || true)"
        case "$herdr_cli" in
            claude|codex) TARGET="herdr"; return 0 ;;
        esac
        cli="$(detect_terminal_cli)"
        case "$cli" in
            claude*) TARGET="claude-code" ;;
            codex*) TARGET="codex-cli" ;;
            *) TARGET="terminal-unknown" ;;
        esac
        return 0
    fi

    # Claude Desktop's public app is commonly named Claude and uses an
    # anthropic/claude bundle. No official desktop dictation hotkey is published.
    case "$app_lc|$bundle_lc" in
        claude\|*|*"|"*anthropic*claude*) TARGET="claude-desktop"; return 0 ;;
    esac

    # Codex desktop's Command+Shift+D is verified on macOS. The required target
    # vocabulary has no codex-desktop entry, so keep it under codex-cli for a
    # stable config key but select the desktop-native action below.
    case "$app_lc|$bundle_lc" in
        codex\|*|*"|"*openai*codex*) TARGET="codex-cli"; IS_CODEX_DESKTOP=1; return 0 ;;
    esac

    case "$app_lc|$bundle_lc" in
        cursor\|*|*"|"com.todesktop.230313mzl4w4u92) TARGET="cursor"; return 0 ;;
    esac

    TARGET="other"
}

native_keycode_parts() {
    spec="$1"
    body="${spec#keycode:}"
    code="${body%%+*}"
    modifiers="${body#${code}}"
    if [ "$modifiers" = "$body" ]; then modifiers=""; fi
    flags=""
    old_ifs="$IFS"
    IFS='+'
    for modifier in $modifiers; do
        [ -n "$modifier" ] || continue
        modifier="$(trim "$modifier")"
        case "$modifier" in
            ctrl|control) flags="${flags}${flags:+,}control" ;;
            shift) flags="${flags}${flags:+,}shift" ;;
            alt|option) flags="${flags}${flags:+,}option" ;;
            cmd|command) flags="${flags}${flags:+,}command" ;;
            *) IFS="$old_ifs"; return 2 ;;
        esac
    done
    IFS="$old_ifs"
    printf '%s|%s\n' "$code" "${flags:-none}"
}

action_for_target() {
    case "$TARGET" in
        discord) printf '%s\n' "$ACTION_DISCORD" ;;
        herdr) printf '%s\n' "dictation" ;;
        claude-code) printf '%s\n' "$ACTION_CLAUDE_CODE" ;;
        codex-cli)
            if [ "$IS_CODEX_DESKTOP" -eq 1 ] && [ "${DECKBRIDGE_FORCE_CONFIG_ACTION:-0}" != "1" ]; then
                printf '%s\n' "toggle-hold:2+cmd+shift"
            else
                printf '%s\n' "$ACTION_CODEX_CLI"
            fi
            ;;
        claude-desktop) printf '%s\n' "$ACTION_CLAUDE_DESKTOP" ;;
        cursor) printf '%s\n' "$ACTION_CURSOR" ;;
        t3code) printf '%s\n' "dictation" ;;
        terminal-unknown) printf '%s\n' "$ACTION_TERMINAL_UNKNOWN" ;;
        *) printf '%s\n' "$ACTION_OTHER" ;;
    esac
}

describe_action() {
    action="$1"
    case "$action" in
        dictation)
            printf 'Deckbridge Mic: focused app Edit > Start Dictation (hotkey %s fallback)\n' "$(dictation_hotkey)"
            ;;
        keycode:*)
            parts="$(native_keycode_parts "$action" 2>/dev/null || true)"
            printf 'Deckbridge Mic: tap keycode %s\n' "$parts"
            ;;
        toggle-hold:*)
            parts="$(native_keycode_parts "keycode:${action#toggle-hold:}" 2>/dev/null || true)"
            printf 'Deckbridge Mic: hold to talk with keycode %s\n' "$parts"
            ;;
        key-hold:*)
            parts="$(native_keycode_parts "keycode:${action#key-hold:}" 2>/dev/null || true)"
            printf 'Deckbridge Mic: hold keycode %s while Stream Deck is held\n' "$parts"
            ;;
        cursor-hold) printf 'Deckbridge Mic: hold Control+M until Stream Deck release\n' ;;
        *) printf '/bin/sh -c %q\n' "$action" ;;
    esac
}

dictation_enabled() {
    fake="${DECKBRIDGE_FAKE_DICTATION_ENABLED:-}"
    if [ -n "$fake" ]; then
        [ "$fake" = "1" ]
        return $?
    fi
    command -v defaults >/dev/null 2>&1 || return 1
    # Current Keyboard Settings is backed by SODictationPreferences and the
    # explicit assistant-support "Dictation Enabled" flag. The older
    # AppleDictationAutoEnable key can disagree (observed 1 vs 0 on macOS 26)
    # and describes automatic enabling, not the current on/off switch. Prefer
    # the explicit flag and retain the legacy key only for older macOS releases.
    value="$(defaults read com.apple.assistant.support 'Dictation Enabled' 2>/dev/null || true)"
    case "$value" in
        1|true|TRUE|YES|yes) return 0 ;;
        0|false|FALSE|NO|no) return 1 ;;
    esac
    value="$(defaults read com.apple.HIToolbox AppleDictationAutoEnable 2>/dev/null || true)"
    case "$value" in
        1|true|TRUE|YES|yes) return 0 ;;
        *) return 1 ;;
    esac
}

accessibility_enabled() {
    fake="${DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED:-}"
    if [ -n "$fake" ]; then
        [ "$fake" = "1" ]
        return $?
    fi
    if [ -x "$MIC_HELPER" ] && run_helper check >/dev/null 2>&1; then
        return 0
    fi
    hammerspoon_gui_ready
}

preflight_action() {
    action="$1"
    if [ "$SESSION_LOCKED" -eq 1 ]; then
        printf '%s\n' \
            'The macOS session is locked. Unlock the Mac; Deckbridge will retry automatically.' >&2
        return 5
    fi
    preflight_rc=0
    case "$action" in
        dictation)
            if ! dictation_enabled; then
                printf '%s\n' \
                    'Dictation is disabled. Open System Settings > Keyboard > Dictation and turn it on.' >&2
                preflight_rc=3
            fi
            ;;
        keycode:*|toggle-hold:*|key-hold:*|cursor-hold) ;;
        *) return 0 ;;
    esac
    if [ -z "${DECKBRIDGE_FAKE_ACCESSIBILITY_ENABLED:-}" ] && [ ! -x "$MIC_HELPER" ]; then
        printf '%s\n' \
            "Deckbridge Mic is not installed. Run: $MIC_HELPER_INSTALLER install" >&2
        if [ "$preflight_rc" -eq 0 ]; then
            preflight_rc=6
        fi
    elif ! accessibility_enabled; then
        printf '%s\n' \
            "GUI keystrokes are blocked. Open System Settings > Privacy & Security > Accessibility and enable Deckbridge Mic ($MIC_HELPER_APP)." >&2
        # Preserve the first failure code while reporting every actionable
        # blocker in one pass. Users should not have to fix Dictation, retry,
        # and only then discover that Accessibility was also disabled.
        if [ "$preflight_rc" -eq 0 ]; then
            preflight_rc=4
        fi
    fi
    return "$preflight_rc"
}

detect_system_dictation_hotkey() {
    fake_plist="${DECKBRIDGE_FAKE_SYMBOLIC_HOTKEYS_PLIST:-}"
    python_bin=""
    if [ -x /usr/bin/python3 ]; then
        python_bin=/usr/bin/python3
    elif command -v python3 >/dev/null 2>&1; then
        python_bin="$(command -v python3)"
    else
        return 1
    fi
    "$python_bin" - "$fake_plist" <<'PY'
import plistlib, subprocess, sys

def decode_modifier(code):
    code = int(code) & 0xFFFFFFFF
    if code & 0x00100008 == 0x00100008:
        return "cmd,cmd"
    if code & 0x00100010 == 0x00100010:
        return "rcmd,rcmd"
    if code & 0x00800000:
        return "fn,fn"
    if code & 0x00040000:
        return "ctrl,ctrl"
    if code & 0x00100000:
        return "cmd,cmd"
    return ""

fake = sys.argv[1]
try:
    if fake:
        with open(fake, "rb") as handle:
            plist = plistlib.load(handle)
    else:
        raw = subprocess.check_output(
            ["defaults", "export", "com.apple.symbolichotkeys", "-"]
        )
        plist = plistlib.loads(raw)
except Exception:
    sys.exit(1)

keys = plist.get("AppleSymbolicHotKeys") or {}
entry = keys.get(164)
if entry is None:
    entry = keys.get("164")
if not isinstance(entry, dict) or not entry.get("enabled"):
    sys.exit(1)
value = entry.get("value") or {}
params = value.get("parameters") or []
kind = value.get("type") or ""
if kind == "modifier" and params:
    decoded = decode_modifier(params[0])
    if decoded:
        print(decoded)
        sys.exit(0)
sys.exit(1)
PY
}

dictation_hotkey() {
    if [ -n "${DECKBRIDGE_DICTATION_HOTKEY:-}" ]; then
        printf '%s\n' "$DECKBRIDGE_DICTATION_HOTKEY"
        return 0
    fi
    if [ -n "$CONFIG_DICTATION_HOTKEY" ]; then
        printf '%s\n' "$CONFIG_DICTATION_HOTKEY"
        return 0
    fi
    detected="$(detect_system_dictation_hotkey 2>/dev/null || true)"
    if [ -n "$detected" ]; then
        printf '%s\n' "$detected"
        return 0
    fi
    printf '%s\n' "$DEFAULT_DICTATION_HOTKEY"
}

tap_dictation_hotkey() {
    hotkey="$(dictation_hotkey)"
    [ "$hotkey" != "none" ] && [ -n "$hotkey" ] || return 1
    IFS=',' read -r -a hotkey_parts <<EOF
$hotkey
EOF
    for part in "${hotkey_parts[@]}"; do
        part="$(trim "$part")"
        case "$part" in
            ctrl|control) code=59; flags=control ;;
            cmd|command) code=55; flags=command ;;
            rcmd|rightcommand) code=54; flags=command ;;
            fn|globe) code=63; flags=function ;;
            f5) code=96; flags=none ;;
            mic|microphone)
                run_hid_cmd tap-mic || return $?
                continue
                ;;
            '') continue ;;
            *[!0-9]*)
                printf 'Unsupported dictation hotkey component: %s\n' "$part" >&2
                return 2 ;;
            *) code="$part"; flags=none ;;
        esac
        run_hid tap "$code" "$flags" || return $?
        sleep 0.1
    done
}

start_dictation() {
    # Menu first. The Keyboard shortcut is a last resort because a guessed
    # chord (Globe, F5, microphone key) plays the wrong sound.
    printf 'dictation_hotkey=%s\n' "$(dictation_hotkey)"
    if hammerspoon_gui_ready && hammerspoon_invoke start-dictation; then
        return 0
    fi
    if run_helper start-dictation >/dev/null 2>&1; then
        return 0
    fi
    tap_dictation_hotkey
}

stop_dictation() {
    if hammerspoon_gui_ready && hammerspoon_invoke stop-dictation; then
        return 0
    fi
    if run_helper stop-dictation >/dev/null 2>&1; then
        return 0
    fi
    tap_dictation_hotkey
}

record_gesture() {
    local state_dir state_tmp
    state_dir="$(dirname -- "$MIC_GESTURE_STATE")"
    mkdir -p "$state_dir" || return 7
    state_tmp="$MIC_GESTURE_STATE.$$"
    if ! printf '%s\n' "$1" > "$state_tmp"; then
        return 7
    fi
    if ! mv -f "$state_tmp" "$MIC_GESTURE_STATE"; then
        rm -f "$state_tmp"
        return 7
    fi
}

release_active_gesture() {
    local gesture parts code flags rc
    [ -f "$MIC_GESTURE_STATE" ] || return 0
    gesture="$(sed -n '1p' "$MIC_GESTURE_STATE")"
    rc=0
    case "$gesture" in
        dictation)
            # Finger-up must not toggle Dictation a second time.
            rm -f "$MIC_GESTURE_STATE"
            return 0
            ;;
        cursor-hold) release_cursor_hold || rc=$? ;;
        toggle-hold:*)
            parts="$(native_keycode_parts "keycode:${gesture#toggle-hold:}")" || return $?
            code="${parts%%|*}"
            flags="${parts#*|}"
            run_hid tap "$code" "$flags" || rc=$?
            ;;
        key-hold:*)
            parts="$(native_keycode_parts "keycode:${gesture#key-hold:}")" || return $?
            code="${parts%%|*}"
            flags="${parts#*|}"
            run_hid key-up "$code" "$flags" || rc=$?
            ;;
        *)
            printf 'Unknown saved voice gesture: %s\n' "$gesture" >&2
            return 2
            ;;
    esac
    if [ "$rc" -eq 0 ]; then
        rm -f "$MIC_GESTURE_STATE"
    fi
    return "$rc"
}

release_cursor_hold() {
    rc=0
    run_hid key-up 46 control || rc=$?
    run_hid key-up 59 none || rc=$?
    return "$rc"
}

execute_action() {
    action="$1"
    phase="${2:-press}"
    if [ "$phase" = release ]; then
        release_active_gesture
        return $?
    fi
    preflight_action "$action" || return $?
    case "$action" in
        dictation)
            start_dictation || return $?
            ;;
        keycode:*)
            parts="$(native_keycode_parts "$action")" || return $?
            code="${parts%%|*}"
            flags="${parts#*|}"
            run_hid tap "$code" "$flags"
            ;;
        toggle-hold:*)
            parts="$(native_keycode_parts "keycode:${action#toggle-hold:}")" || return $?
            code="${parts%%|*}"
            flags="${parts#*|}"
            run_hid tap "$code" "$flags" || return $?
            record_gesture "$action" || {
                run_hid tap "$code" "$flags" >/dev/null 2>&1 || true
                return 7
            }
            printf 'gesture=hold\n'
            ;;
        key-hold:*)
            parts="$(native_keycode_parts "keycode:${action#key-hold:}")" || return $?
            code="${parts%%|*}"
            flags="${parts#*|}"
            run_hid key-down "$code" "$flags" || return $?
            record_gesture "$action" || {
                run_hid key-up "$code" "$flags" >/dev/null 2>&1 || true
                return 7
            }
            printf 'gesture=hold\n'
            ;;
        cursor-hold)
            run_hid key-down 59 control || return $?
            run_hid key-down 46 control || {
                rc=$?
                run_hid key-up 59 none >/dev/null 2>&1 || true
                return "$rc"
            }
            record_gesture cursor-hold || {
                release_cursor_hold >/dev/null 2>&1 || true
                return 7
            }
            printf 'gesture=hold\n'
            ;;
        *)
            /bin/sh -c "$action"
            ;;
    esac
}

print_detection() {
    printf 'frontmost_app=%s\n' "$FRONT_APP"
    printf 'bundle_id=%s\n' "$FRONT_BUNDLE"
    printf 'classification=%s\n' "$TARGET"
    if [ "$SESSION_LOCKED" -eq 1 ]; then
        printf 'session_locked=yes\n'
    else
        printf 'session_locked=no\n'
    fi
}

usage() {
    printf '%s\n' "Usage: $0 [--press|--release|--dry-run|--status|--check|--helper-check|--helper-frontmost|--helper-focused-tty|--helper-web-url BUNDLE|--helper-web-urls BUNDLE|--helper-web-windows BUNDLE|--helper-press-button BUNDLE TITLE|--helper-focus-text-entry BUNDLE|--request-access|--install-helper]"
    printf '%s\n' "  --press    start the selected focused-app voice gesture"
    printf '%s\n' "  --release  stop the active hold-to-talk voice gesture"
    printf '%s\n' "  --dry-run  print detection and the action without executing it"
    printf '%s\n' "  --status   print detection only"
    printf '%s\n' "  --check    read-only Dictation/Accessibility setup check"
    printf '%s\n' "  --helper-check  read-only native helper Accessibility check"
    printf '%s\n' "  --helper-frontmost  print the frontmost app identity"
    printf '%s\n' "  --helper-focused-tty  print the selected terminal TTY when exact"
    printf '%s\n' "  --helper-web-url  read the selected AX web route for a bundle id"
    printf '%s\n' "  --helper-web-urls  read every exposed AX web route for a bundle id"
    printf '%s\n' "  --helper-web-windows  read every AX window title and route, if exposed"
    printf '%s\n' "  --request-access  ask macOS to show Deckbridge Mic consent"
    printf '%s\n' "  --install-helper  install stable native Deckbridge Mic.app"
}

load_config
case "${1:-}" in
    --install-helper)
        exec "$MIC_HELPER_INSTALLER" install
        ;;
    --helper-status)
        exec "$MIC_HELPER_INSTALLER" status
        ;;
    --helper-check)
        run_helper check
        exit $?
        ;;
    --helper-frontmost)
        run_helper frontmost
        exit $?
        ;;
    --helper-focused-tty)
        detect_frontmost
        focused_terminal_tty
        exit $?
        ;;
    --helper-web-url)
        [ "$#" -eq 2 ] || { usage >&2; exit 2; }
        run_helper web-url "$2"
        exit $?
        ;;
    --helper-web-urls)
        [ "$#" -eq 2 ] || { usage >&2; exit 2; }
        run_helper web-urls "$2"
        exit $?
        ;;
    --helper-web-windows)
        [ "$#" -eq 2 ] || { usage >&2; exit 2; }
        run_helper web-windows "$2"
        exit $?
        ;;
    --helper-press-button)
        [ "$#" -eq 3 ] || { usage >&2; exit 2; }
        run_helper press-button "$2" "$3"
        exit $?
        ;;
    --helper-focus-text-entry)
        [ "$#" -eq 2 ] || { usage >&2; exit 2; }
        run_helper focus-text-entry "$2"
        exit $?
        ;;
    --request-access)
        run_helper request-access
        exit $?
        ;;
    --release)
        execute_action '' release
        exit $?
        ;;
esac
if screen_locked; then
    SESSION_LOCKED=1
    FRONT_APP="loginwindow"
    FRONT_BUNDLE="com.apple.loginwindow"
else
    detect_frontmost
fi
classify_target

case "${1:-}" in
    --dry-run)
        print_detection
        printf 'command_would_run='; describe_action "$(action_for_target)"
        exit 0
        ;;
    --status)
        print_detection
        exit 0
        ;;
    --check)
        print_detection
        action="$(action_for_target)"
        printf 'command_would_run='; describe_action "$action"
        if preflight_action "$action"; then
            printf 'ready=yes\n'
            exit 0
        else
            rc=$?
            printf 'ready=no\n'
            exit "$rc"
        fi
        ;;
    --help|-h)
        usage
        exit 0
        ;;
    --press|'')
        # T3 has no native voice action. Put the insertion point in its prompt
        # before invoking macOS Dictation so press/hold/release works even when
        # the user last clicked the sidebar or transcript.
        if [ "$TARGET" = "t3code" ]; then
            focus_t3_prompt
        fi
        execute_action "$(action_for_target)" press
        exit $?
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
