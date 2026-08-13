#!/usr/bin/env bash
# Build the stable, user-grantable native Accessibility helper used by MIC.

set -u

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SOURCE="$ROOT/DeckbridgeMic.m"
INFO_PLIST="$ROOT/DeckbridgeMic-Info.plist"
APP_PATH="${DECKBRIDGE_MIC_APP:-$HOME/Applications/Deckbridge Mic.app}"
EXPECTED_VERSION=9

helper_path() {
    printf '%s/Contents/MacOS/deckbridge-mic\n' "$APP_PATH"
}

installed_version() {
    helper="$(helper_path)"
    [ -x "$helper" ] || return 1
    "$helper" version 2>/dev/null
}

print_grant_steps() {
    printf '%s\n' \
        'One-time macOS step:' \
        '  System Settings > Privacy & Security > Accessibility' \
        '  Add or enable Deckbridge Mic from:' \
        "  $APP_PATH" \
        '  (Double-click the app to request the official macOS consent prompt.)' \
        'Deckbridge will notice the grant automatically within five seconds.'
}

status() {
    helper="$(helper_path)"
    if [ ! -x "$helper" ]; then
        printf 'not installed: %s\n' "$APP_PATH"
        return 1
    fi
    version="$(installed_version || true)"
    printf 'installed: %s\n' "$APP_PATH"
    printf 'protocol_version=%s\n' "${version:-unknown}"
    if [ "$version" != "$EXPECTED_VERSION" ]; then
        printf 'helper upgrade required; run: %s install --force\n' "$0" >&2
        return 2
    fi
    if DECKBRIDGE_MIC_APP="$APP_PATH" "$ROOT/mic_key.sh" --helper-check \
            >/dev/null 2>&1; then
        printf 'accessibility=ready\n'
        return 0
    else
        check_rc=$?
    fi
    if [ "$check_rc" -eq 4 ]; then
        printf 'accessibility=grant-required\n'
        print_grant_steps
        return 4
    fi
    printf 'accessibility=check-failed (exit %s)\n' "$check_rc" >&2
    return "$check_rc"
}

install_helper() {
    force="${1:-0}"
    current="$(installed_version || true)"
    if [ "$current" = "$EXPECTED_VERSION" ] && [ "$force" != 1 ]; then
        printf 'Deckbridge Mic is already installed: %s\n' "$APP_PATH"
        status
        return $?
    fi
    if [ -n "$current" ] && [ "$force" != 1 ]; then
        printf 'refusing to replace trusted helper protocol %s with %s automatically\n' \
            "$current" "$EXPECTED_VERSION" >&2
        printf 'run `%s install --force` and re-enable Deckbridge Mic if macOS asks\n' \
            "$0" >&2
        return 2
    fi
    if [ ! -f "$SOURCE" ] || [ ! -f "$INFO_PLIST" ]; then
        printf 'helper source is incomplete in %s\n' "$ROOT" >&2
        return 1
    fi
    if [ ! -x /usr/bin/clang ] || [ ! -x /usr/bin/codesign ]; then
        printf 'Deckbridge Mic requires Apple Command Line Tools (clang and codesign).\n' >&2
        return 1
    fi

    parent="$(dirname -- "$APP_PATH")"
    mkdir -p "$parent"
    stage="$(mktemp -d "$parent/.deckbridge-mic-stage.XXXXXX")" || return 1
    trap 'rm -rf "$stage"' EXIT HUP INT TERM
    mkdir -p "$stage/Contents/MacOS"
    cp "$INFO_PLIST" "$stage/Contents/Info.plist"
    module_cache="${CLANG_MODULE_CACHE_PATH:-${TMPDIR:-/tmp}/deckbridge-clang-module-cache}"
    mkdir -p "$module_cache"
    CLANG_MODULE_CACHE_PATH="$module_cache" \
    /usr/bin/clang -fobjc-arc -O2 -framework AppKit -framework ApplicationServices \
        "$SOURCE" -o "$stage/Contents/MacOS/deckbridge-mic" || return 1
    /usr/bin/codesign --force --deep --sign - \
        --identifier com.deckbridge.mic-helper "$stage" >/dev/null || return 1
    /usr/bin/codesign --verify --deep --strict "$stage" || return 1
    built_version="$($stage/Contents/MacOS/deckbridge-mic version 2>/dev/null || true)"
    if [ "$built_version" != "$EXPECTED_VERSION" ]; then
        printf 'built helper protocol mismatch: %s\n' "${built_version:-unknown}" >&2
        return 1
    fi

    if [ -e "$APP_PATH" ]; then
        backup="${APP_PATH}.previous.$(date +%Y%m%d%H%M%S)"
        mv "$APP_PATH" "$backup" || return 1
        printf 'previous helper retained at: %s\n' "$backup"
    fi
    mv "$stage" "$APP_PATH" || return 1
    trap - EXIT HUP INT TERM
    printf 'installed Deckbridge Mic: %s\n' "$APP_PATH"
    if DECKBRIDGE_MIC_APP="$APP_PATH" "$ROOT/mic_key.sh" --helper-check \
            >/dev/null 2>&1; then
        printf 'Accessibility is already ready.\n'
    else
        print_grant_steps
    fi
    return 0
}

case "${1:-install}" in
    install)
        force=0
        [ "${2:-}" != "--force" ] || force=1
        install_helper "$force"
        ;;
    status) status ;;
    *)
        printf 'Usage: %s [install [--force]|status]\n' "$0" >&2
        exit 2
        ;;
esac
