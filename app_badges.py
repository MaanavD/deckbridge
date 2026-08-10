#!/usr/bin/env python3
"""Read truthful macOS notification counts for Deckbridge app shortcuts.

There is no single badge API shared by Electron apps and Chrome.  This probe
therefore combines the Dock's AXStatusLabel with the small amount of public UI
state each app exposes.  It never opens messages or reads page content: only
app names, window titles, Gmail tab titles, and Gmail tab URLs are returned.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Callable


APPLESCRIPT = r'''
set output to ""
tell application "System Events"
    if exists process "Dock" then
        tell process "Dock" to tell list 1
            repeat with itemRef in UI elements
                try
                    set appName to name of itemRef as text
                    set appBadge to value of attribute "AXStatusLabel" of itemRef
                    if appBadge is missing value then set appBadge to ""
                    set output to output & "DOCK" & tab & appName & tab & (appBadge as text) & linefeed
                end try
            end repeat
        end tell
    end if
    repeat with appName in {"Slack", "Discord", "Notion Calendar"}
        if exists process appName then
            tell process appName
                repeat with windowRef in windows
                    try
                        set output to output & "WIN" & tab & (appName as text) & tab & (name of windowRef as text) & linefeed
                    end try
                end repeat
            end tell
        end if
    end repeat
end tell
if application "Google Chrome" is running then
    tell application "Google Chrome"
        repeat with windowRef in windows
            repeat with tabRef in tabs of windowRef
                try
                    set output to output & "TAB" & tab & (title of tabRef as text) & tab & (URL of tabRef as text) & linefeed
                end try
            end repeat
        end repeat
    end tell
end if
return output
'''


def _number(text: str) -> int:
    match = re.search(r"\d+", text or "")
    return int(match.group()) if match else 0


def parse_badges(output: str) -> dict[str, int]:
    """Parse the deliberately narrow line protocol emitted by APPLESCRIPT."""
    counts = {name: 0 for name in ("slack", "gmail", "discord", "notion-calendar")}
    for raw in output.splitlines():
        fields = raw.split("\t", 2)
        if len(fields) != 3:
            continue
        kind, name, value = fields
        if kind == "DOCK":
            source = {
                "Slack": "slack", "Discord": "discord",
                "Notion Calendar": "notion-calendar",
            }.get(name)
            if source:
                counts[source] = max(counts[source], _number(value))
        elif kind == "WIN" and name == "Slack":
            match = re.search(r"\b(\d+)\s+new items?\b", value, re.I)
            if not match:
                match = re.search(r"\b(\d+)\s+unread\b", value, re.I)
            if match:
                counts["slack"] = max(counts["slack"], int(match.group(1)))
        elif kind == "WIN" and name == "Discord":
            match = re.search(r"(?:^\((\d+)\)|\b(\d+)\s+unread\b)", value, re.I)
            if match:
                counts["discord"] = max(
                    counts["discord"], int(match.group(1) or match.group(2)))
        elif kind == "WIN" and name == "Notion Calendar":
            match = re.search(r"(?:^\((\d+)\)|\b(\d+)\s+(?:notification|reminder)s?\b)", value, re.I)
            if match:
                counts["notion-calendar"] = max(
                    counts["notion-calendar"], int(match.group(1) or match.group(2)))
        elif kind == "TAB" and "mail.google.com/mail/u/0" in value:
            # /u/0 is the same account target as the configured work shortcut.
            match = re.search(r"(?:^\((\d+)\)|\bInbox\s*\((\d+)\))", name, re.I)
            if match:
                counts["gmail"] = max(
                    counts["gmail"], int(match.group(1) or match.group(2)))
    return counts


class AppBadgeProvider:
    """Cached badge source; ``refresh`` is intended to run in an executor."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.runner = runner
        self._counts: dict[str, int] = {}

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def refresh(self) -> dict[str, int]:
        if sys.platform != "darwin":
            self._counts = {}
            return {}
        try:
            result = self.runner(
                ["osascript", "-e", APPLESCRIPT], capture_output=True,
                text=True, timeout=2.0, check=False,
            )
            # Keep the last known-good frame across a transient Automation/AX
            # timeout. Clearing it would make every red bubble flicker off for
            # one poll even though no message had actually been read.
            if result.returncode == 0:
                self._counts = parse_badges(result.stdout)
        except (OSError, subprocess.SubprocessError):
            pass
        return self.counts()
