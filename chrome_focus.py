"""Focus or open a URL in a specific Google Chrome profile."""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("connector_agents")


CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_LOCAL_STATE = Path.home() / "Library/Application Support/Google/Chrome/Local State"
# Chrome's OS window title is "<page> - Google Chrome - <profile>". A work
# profile that is also a person is titled "Maanav (blackforestlabs.ai)", so
# matching the configured profile name as an exact suffix misses every window
# and the Gmail key opens another Gmail tab on every press.
_CHROME_PROFILE_MARKER = " - Google Chrome - "


def chrome_window_belongs_to_profile(window_name: str, profile_name: str) -> bool:
    """True when a Chrome OS window title belongs to ``profile_name``."""
    title = str(window_name or "")
    profile = str(profile_name or "").strip()
    if not title or not profile:
        return False
    marker = _CHROME_PROFILE_MARKER
    if marker not in title:
        return False
    suffix = title.rsplit(marker, 1)[-1]
    return suffix == profile or suffix.endswith(" (" + profile + ")")


def chrome_titles_refer_to_same_window(chrome_title: str, os_title: str) -> bool:
    """Pair a Chrome AppleScript window name with its System Events title.

    Chrome truncates the tab title with an ellipsis; System Events keeps the
    full OS title including the profile suffix. Index-zipping those two lists
    is what made an already-open Gmail tab look missing.
    """
    chrome = str(chrome_title or "")
    os_name = str(os_title or "")
    if not chrome or not os_name:
        return False
    if os_name.startswith(chrome):
        return True
    if "…" in chrome:
        head, tail = chrome.split("…", 1)
        return os_name.startswith(head) and tail in os_name
    return False


#: A Notion page keeps one 32-hex id and rewrites everything around it: the
#: same board is app.notion.com/p/<id>, www.notion.so/<slug>-<id>, and carries
#: whichever ?v= and ?pvs= the last visit left behind. Matching the URL as a
#: prefix therefore fails on the second press and opens the board again.
_PAGE_ID_RE = re.compile(r"[0-9a-f]{32}")


def chrome_tab_match_token(url: str) -> str:
    """Return the substring that still identifies ``url`` after a rewrite."""
    # Query string excluded: ?v= is a view id of the same shape, and matching
    # that would call any view of the database the same page.
    path = str(url or "").lower().split("?", 1)[0]
    found = _PAGE_ID_RE.findall(path)
    return found[-1] if found else ""


def chrome_tab_matches_url(tab_url: str, target: str) -> bool:
    """True when an open tab is the destination, including Gmail hash routes."""
    tab = str(tab_url or "").strip()
    want = str(target or "").strip()
    if not tab or not want:
        return False
    if tab.startswith(want):
        return True
    tab_base = tab.split("#", 1)[0].rstrip("/")
    want_base = want.split("#", 1)[0].rstrip("/")
    if tab_base == want_base or tab_base.startswith(want_base + "/"):
        return True
    if "mail.google.com/mail" in want and "mail.google.com/mail" in tab:
        return True
    token = chrome_tab_match_token(want)
    if token and token in tab.lower():
        return True
    return False


_CHROME_RAISE_SCRIPT = r'''
on run argv
    set profileName to item 1 of argv
    set profileSuffix to " - Google Chrome - " & profileName
    set profileParen to " (" & profileName & ")"
    tell application "System Events"
        if exists process "Google Chrome" then
            tell process "Google Chrome"
                repeat with windowRef in windows
                    try
                        set windowName to name of windowRef as text
                        if windowName ends with profileSuffix or windowName ends with profileParen then
                            perform action "AXRaise" of windowRef
                            set frontmost to true
                            return "focused"
                        end if
                    end try
                end repeat
            end tell
        end if
    end tell
    return "missing"
end run
'''
_CHROME_TAB_SCRIPT = r'''
on run argv
    set profileName to item 1 of argv
    set targetUrl to item 2 of argv
    set matchToken to item 3 of argv
    set profileSuffix to " - Google Chrome - " & profileName
    set profileParen to " (" & profileName & ")"
    tell application "System Events"
        if not (exists process "Google Chrome") then return "missing"
        tell process "Google Chrome"
            set osNames to name of windows
        end tell
    end tell
    tell application "Google Chrome"
        if (count of windows) is 0 then return "missing"
        set profileId to 0
        set fallbackWinId to 0
        set fallbackTabIndex to 0
        repeat with w in windows
            set chromeTitle to name of w as text
            set isProfile to false
            repeat with osNameRef in osNames
                set osName to osNameRef as text
                if osName ends with profileSuffix or osName ends with profileParen then
                    if osName starts with chromeTitle then
                        set isProfile to true
                    else if chromeTitle contains "…" then
                        set AppleScript's text item delimiters to "…"
                        set parts to text items of chromeTitle
                        set AppleScript's text item delimiters to ""
                        if (count of parts) is 2 then
                            if osName starts with (item 1 of parts) and osName contains (item 2 of parts) then
                                set isProfile to true
                            end if
                        end if
                    end if
                end if
                if isProfile then exit repeat
            end repeat
            if isProfile and profileId is 0 then set profileId to id of w
            set tabIndex to 0
            repeat with t in tabs of w
                set tabIndex to tabIndex + 1
                try
                    set tabUrl to URL of t as text
                    set matched to false
                    if tabUrl starts with targetUrl then set matched to true
                    if targetUrl contains "mail.google.com" and tabUrl contains "mail.google.com/mail" then set matched to true
                    if matchToken is not "" and tabUrl contains matchToken then set matched to true
                    if matched then
                        if isProfile then
                            set active tab index of w to tabIndex
                            set index of w to 1
                            activate
                            return "focused-tab"
                        end if
                        if fallbackWinId is 0 then
                            set fallbackWinId to id of w
                            set fallbackTabIndex to tabIndex
                        end if
                    end if
                end try
            end repeat
        end repeat
        if fallbackWinId is not 0 then
            set active tab index of window id fallbackWinId to fallbackTabIndex
            set index of window id fallbackWinId to 1
            activate
            return "focused-tab"
        end if
        if profileId is 0 then return "missing"
        tell window id profileId to make new tab with properties {URL:targetUrl}
        set index of window id profileId to 1
        activate
        return "new-tab"
    end tell
end run
'''


def lookup_chrome_profile_name(profile: str) -> str:
    """Return Chrome's visible profile name for a profile directory.

    Window titles use the display name, not ``Default`` / ``Profile 1``. Reading
    Local State keeps Gmail's work-window match working without duplicating
    that name in config.
    """
    directory = str(profile or "").strip()
    if not directory:
        return ""
    try:
        payload = json.loads(CHROME_LOCAL_STATE.read_text(encoding="utf-8"))
        cache = payload.get("profile", {}).get("info_cache", {})
        info = cache.get(directory) if isinstance(cache, dict) else None
        if isinstance(info, dict):
            return str(info.get("name") or "").strip()
    except (OSError, ValueError, TypeError):
        return ""
    return ""


def raise_chrome_profile_window(profile_name: str) -> bool:
    """Raise an existing Chrome window for ``profile_name``, if one is open."""
    name = str(profile_name or "").strip()
    if not name:
        return False
    try:
        focused = subprocess.run(
            ["/usr/bin/osascript", "-e", _CHROME_RAISE_SCRIPT, name],
            check=False, capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Chrome profile focus failed for %s: %s", name, exc)
        return False
    if focused.returncode == 0 and focused.stdout.strip() == "focused":
        log.info("focused Chrome profile window: %s", name)
        return True
    return False


def open_or_focus_chrome_tab(profile_name: str, url: str) -> bool:
    """Focus a matching tab in this Chrome profile, or open one if none exist.

    Searches every window of the profile. Opening a new Gmail tab is reserved
    for the case where that profile has windows but no Gmail at all.
    """
    name = str(profile_name or "").strip()
    target = str(url or "").strip()
    if not name or not target:
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", _CHROME_TAB_SCRIPT, name, target,
             chrome_tab_match_token(target)],
            check=False, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Chrome tab open failed for %s: %s", target, exc)
        return False
    status = (result.stdout or "").strip()
    if result.returncode == 0 and status in ("focused-tab", "new-tab"):
        log.info("Chrome tab %s for %s", status, target)
        return True
    return False
