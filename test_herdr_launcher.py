#!/usr/bin/env python3
"""Regression tests for the standalone Herdr Terminal launcher.

The first implementation activated Terminal before asking it to run Herdr.
When Terminal was not already running, ``activate`` created a login window and
``do script`` created a second window.  The launcher also inherited SF Mono,
which cannot render the Nerd Font glyphs used by the shell prompt.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "herdr_launcher.applescript"
INSTALLER = ROOT / "install_herdr_app.sh"
RESULTS: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    ok = bool(condition)
    RESULTS.append((name, ok))
    suffix = "" if ok or not detail else f": {detail}"
    print(f"{'PASS' if ok else 'FAIL'} {name}{suffix}")


def main() -> int:
    source = SOURCE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("--")
    )
    lowered = code.lower()
    check("launcher starts exactly one Terminal command",
          lowered.count("do script") == 1,
          f"do-script count={lowered.count('do script')}")
    check("Terminal is not activated before the Herdr command",
          not re.search(r'tell application "terminal"\s+activate\s+do script',
                        lowered),
          "found the blank-window-producing activate/do-script sequence")
    check("Herdr replaces the login shell",
          'do script "exec ' in lowered)
    check("reopening the app focuses an existing Herdr tab",
          "processes of" in lowered and '"herdr"' in lowered)
    check("existing Herdr tabs also receive the icon-capable profile",
          "set current settings of terminaltab to herdrprofile" in lowered)
    check("launcher selects a dedicated Herdr profile",
          "current settings of" in lowered and "herdrprofile" in lowered)
    check("missing profile is created through Terminal's supported API",
          "make new settings set" in lowered and "duplicate settings set" not in lowered)
    check("Herdr profile uses the installed Nerd Font",
          "jetbrainsmononfm-regular" in lowered)

    installer = INSTALLER.read_text(encoding="utf-8")
    check("standalone app has a repeatable installer",
          "osacompile" in installer and "/Applications/Herdr.app" in installer)
    check("installer rolls back a failed bundle swap",
          "restored the previous app" in installer and 'mv "$backup" "$TARGET"' in installer)

    compiler = shutil.which("osacompile")
    if compiler:
        with tempfile.TemporaryDirectory(prefix="deckbridge-herdr-launcher-") as tmp:
            result = subprocess.run(
                [compiler, "-o", str(Path(tmp) / "Herdr.app"), str(SOURCE)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        check("launcher compiles as a macOS app",
              result.returncode == 0, result.stderr.strip())

    passed = sum(ok for _, ok in RESULTS)
    print(f"herdr launcher: {passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
