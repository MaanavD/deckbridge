#!/usr/bin/env python3
"""Claude Code hook shim for deckbridge (thin wrapper over ``agent_shim.py``).

Point every Claude Code lifecycle hook at this file.  It forwards to the shared
engine with ``--agent claude``, which prefixes deck labels with ``cc-`` and
tags records ``source: claude-code``.

Install by merging the ``hooks`` block from ``claude_settings.example.json``
into ``~/.claude/settings.json`` (or a project ``.claude/settings.json``), with
``REPLACE_ME`` swapped for your checkout path::

    {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "/path/to/deckbridge/claude_shim.py"}
    ]}]}}

Extra flags pass straight through, so ``claude_shim.py --ttl 300`` works.
See ``agent_shim.py`` for the full event mapping and safety properties.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_shim import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["--agent", "claude", *sys.argv[1:]]))
