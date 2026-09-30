#!/usr/bin/env python3
"""Five-hour subscription usage for the Claude and OpenAI launcher keys.

The number on the key is how much of the current five-hour session window is
already spent, 0–100. Codex reads it from the local Codex login. Claude
reads it from the Claude Code keychain login, refreshing that token when it
has expired.
"""
from __future__ import annotations

import getpass
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("usage_limits")

CLAUDE_SOURCE = "claude-code"
CODEX_SOURCE = "codex-cli"
SESSION_SOURCES = (CLAUDE_SOURCE, CODEX_SOURCE)

CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_BETA = "oauth-2025-04-20"
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
#: Codex names the five-hour bucket ``primary``; 18000s is that window.
CODEX_SESSION_SECONDS = 5 * 60 * 60

CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, Optional[int], float]] = {}


def clamp_percent(value: Any) -> Optional[int]:
    """Coerce a used-percent into 0–100. Fractions in (0, 1) are not percents."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 100:
        return None
    return int(round(number))


def iso_epoch(value: Any) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def claude_session_percent(payload: Any) -> tuple[Optional[int], float]:
    """Parse Claude's oauth usage body. Utilization is already 0–100.

    The five-hour clock starts the first time the account spends against it.
    A missing window is that resting state: nothing has been used, so the
    whole allowance is still left.
    """
    if not isinstance(payload, dict):
        return None, 0.0
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        limits = payload
    if not isinstance(limits, dict) or "five_hour" not in limits:
        return 0, 0.0
    window = limits.get("five_hour")
    if window is None:
        return 0, 0.0
    if not isinstance(window, dict):
        return None, 0.0
    used = clamp_percent(window.get("utilization"))
    if used is None:
        return 0, 0.0
    return used, iso_epoch(window.get("resets_at"))


def codex_session_percent(payload: Any) -> tuple[Optional[int], float]:
    """Parse Codex's wham usage body. ``primary_window`` is the five-hour bucket."""
    if not isinstance(payload, dict):
        return None, 0.0
    rate = payload.get("rate_limit")
    if not isinstance(rate, dict):
        return None, 0.0
    window = rate.get("primary_window")
    if not isinstance(window, dict):
        return None, 0.0
    seconds = window.get("limit_window_seconds")
    if seconds is not None:
        try:
            if int(seconds) != CODEX_SESSION_SECONDS:
                return None, 0.0
        except (TypeError, ValueError):
            return None, 0.0
    reset_at = window.get("reset_at")
    try:
        resets = float(reset_at) if reset_at else 0.0
    except (TypeError, ValueError):
        resets = 0.0
    return clamp_percent(window.get("used_percent")), resets


def fresh_percent(percent: Optional[int], resets_at: float, now: float) -> Optional[int]:
    """A finished window has rolled back to empty until the next probe says otherwise."""
    if percent is None:
        return None
    if resets_at and resets_at <= now:
        return 0
    return percent


def session_percent(source: str, now: Optional[float] = None) -> Optional[int]:
    """Used percent of the five-hour window for a launcher source, or None."""
    if source not in SESSION_SOURCES:
        return None
    current = time.time() if now is None else now
    cached = _cache.get(source)
    if cached:
        fetched, percent, resets = cached[0], cached[1], cached[2]
        hold = cached[3] if len(cached) > 3 else fetched + CACHE_TTL_S
        if current < hold:
            return fresh_percent(percent, resets, current)
    probed = _probe(source)
    percent, resets = probed[0], probed[1]
    hold_for = probed[2] if len(probed) > 2 else CACHE_TTL_S
    if percent is None and cached:
        kept = fresh_percent(cached[1], cached[2], current)
        if kept is not None:
            _cache[source] = (current, cached[1], cached[2], current + hold_for)
            return kept
    # A failed read with no open window is the resting state: the five-hour
    # clock has not started, so the allowance is still full.
    if percent is None and source == CLAUDE_SOURCE:
        percent, resets = 0, 0.0
    _cache[source] = (current, percent, resets, current + hold_for)
    return fresh_percent(percent, resets, current)


def clear_cache() -> None:
    _cache.clear()


def _probe(source: str) -> tuple[Optional[int], float, float]:
    """Return used percent, window reset time, and seconds to wait before retrying."""
    try:
        if source == CLAUDE_SOURCE:
            return _probe_claude()
        percent, resets = _probe_codex()
        return percent, resets, CACHE_TTL_S
    except Exception:
        log.warning("%s usage probe failed", source, exc_info=True)
        return None, 0.0, CACHE_TTL_S


def _retry_after(headers: Any) -> float:
    try:
        return max(0.0, float(headers.get("Retry-After") or 0))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _http_json(
    url: str,
    headers: dict[str, str],
    body: Optional[bytes] = None,
    timeout: float = 8.0,
) -> tuple[int, Any, float]:
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
            retry = _retry_after(getattr(response, "headers", {}))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        retry = _retry_after(getattr(exc, "headers", {}))
    if not raw:
        return status, None, retry
    try:
        return status, json.loads(raw.decode("utf-8")), retry
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None, retry


def _read_claude_document() -> Optional[dict[str, Any]]:
    try:
        raw = subprocess.check_output(
            [
                "security", "find-generic-password",
                "-a", getpass.getuser(),
                "-s", CLAUDE_KEYCHAIN_SERVICE,
                "-w",
            ],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return None
    oauth = document.get("claudeAiOauth") if isinstance(document, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    return document


def _write_claude_document(document: dict[str, Any]) -> None:
    subprocess.run(
        [
            "security", "add-generic-password", "-U",
            "-a", getpass.getuser(),
            "-s", CLAUDE_KEYCHAIN_SERVICE,
            "-w", json.dumps(document, separators=(",", ":")),
        ],
        check=True, timeout=5,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _refresh_claude(document: dict[str, Any]) -> Optional[dict[str, Any]]:
    oauth = dict(document.get("claudeAiOauth") or {})
    refresh = str(oauth.get("refreshToken") or "")
    if not refresh:
        return None
    payload: dict[str, Any] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLAUDE_CLIENT_ID,
    }
    scopes = oauth.get("scopes")
    if isinstance(scopes, list) and scopes:
        payload["scope"] = " ".join(str(item) for item in scopes)
    status, body, _retry = _http_json(
        CLAUDE_TOKEN_URL,
        {"Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
    )
    if status != 200 or not isinstance(body, dict):
        log.warning("claude usage token refresh failed (HTTP %s)", status)
        return None
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    new_refresh = body.get("refresh_token")
    try:
        expires_in = float(body.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600.0
    oauth["accessToken"] = access
    if isinstance(new_refresh, str) and new_refresh:
        oauth["refreshToken"] = new_refresh
    oauth["expiresAt"] = int((time.time() + expires_in) * 1000)
    updated = dict(document)
    updated["claudeAiOauth"] = oauth
    try:
        _write_claude_document(updated)
    except (OSError, subprocess.SubprocessError):
        log.warning("claude usage token refreshed but could not be saved")
    return updated


def _probe_claude() -> tuple[Optional[int], float, float]:
    document = _read_claude_document()
    if document is None:
        return None, 0.0, CACHE_TTL_S
    # Ask with the saved token first. Refreshing on the clock alone was
    # failing, and a 429 from this endpoint means "ask again later", not
    # "the five-hour allowance is gone".
    token = str((document.get("claudeAiOauth") or {}).get("accessToken") or "")
    if not token:
        return None, 0.0, CACHE_TTL_S
    status, body, retry = _fetch_claude_usage(token)
    if status == 401:
        refreshed = _refresh_claude(document)
        token = str(((refreshed or {}).get("claudeAiOauth") or {}).get("accessToken") or "")
        if not token:
            return None, 0.0, CACHE_TTL_S
        status, body, retry = _fetch_claude_usage(token)
    if status == 429:
        wait = min(max(retry or 300.0, 60.0), 30 * 60)
        log.info("claude usage endpoint asked for a pause of %.0fs", wait)
        return None, 0.0, wait
    if status != 200:
        log.warning("claude usage read failed (HTTP %s)", status)
        return None, 0.0, CACHE_TTL_S
    percent, resets = claude_session_percent(body)
    return percent, resets, CACHE_TTL_S


def _fetch_claude_usage(token: str) -> tuple[int, Any, float]:
    return _http_json(
        CLAUDE_USAGE_URL,
        {
            "Authorization": "Bearer " + token,
            "anthropic-beta": CLAUDE_BETA,
        },
    )


def _probe_codex() -> tuple[Optional[int], float]:
    path = Path.home() / ".codex" / "auth.json"
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, 0.0
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    if not isinstance(tokens, dict):
        return None, 0.0
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return None, 0.0
    headers = {
        "Authorization": "Bearer " + access,
        "Content-Type": "application/json",
        "OpenAI-Beta": "codex-1",
        "Originator": "Codex Desktop",
    }
    account = tokens.get("account_id")
    if isinstance(account, str) and account:
        headers["Chatgpt-Account-Id"] = account
    status, body, _retry = _http_json(CODEX_USAGE_URL, headers)
    if status != 200:
        log.warning("codex usage read failed (HTTP %s)", status)
        return None, 0.0
    return codex_session_percent(body)
