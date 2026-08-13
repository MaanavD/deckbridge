#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

from install_hammerspoon_bridge import END, START, install


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "init.lua"
        module = Path(tmp) / "bridge.lua"
        config.write_text("print('existing')\n", encoding="utf-8")
        assert install(config, module)
        first = config.read_text(encoding="utf-8")
        assert "print('existing')" in first
        assert first.count(START) == first.count(END) == 1
        assert "hs.ipc.cliInstall()" in first
        assert not install(config, module)
        assert config.read_text(encoding="utf-8") == first
    print("PASS Hammerspoon bridge install is additive and idempotent")


if __name__ == "__main__":
    main()
