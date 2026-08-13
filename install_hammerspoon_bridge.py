#!/usr/bin/env python3
"""Idempotently load Deckbridge's status bridge from Hammerspoon."""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path


START = "-- BEGIN DECKBRIDGE CLAUDE STATUS"
END = "-- END DECKBRIDGE CLAUDE STATUS"


def block(module: Path) -> str:
    escaped = str(module).replace("\\", "\\\\").replace('"', '\\"')
    return (
        f"{START}\n"
        "hs.ipc.cliInstall()\n"
        f'dofile("{escaped}")\n'
        f"{END}"
    )


def install(config: Path, module: Path) -> bool:
    original = config.read_text(encoding="utf-8") if config.exists() else ""
    desired = block(module)
    if START in original and END in original:
        before, rest = original.split(START, 1)
        _, after = rest.split(END, 1)
        updated = before.rstrip() + "\n\n" + desired + after
    else:
        updated = original.rstrip() + "\n\n" + desired + "\n"
    if updated == original:
        return False
    config.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".init.lua.", dir=config.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="~/.hammerspoon/init.lua")
    parser.add_argument("--module", default=str(Path(__file__).with_name("hammerspoon_deckbridge.lua")))
    args = parser.parse_args()
    changed = install(Path(args.config).expanduser(), Path(args.module).resolve())
    print("installed" if changed else "already installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
