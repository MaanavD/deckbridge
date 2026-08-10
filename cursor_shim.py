#!/usr/bin/env python3
"""Thin Cursor hook wrapper around :mod:`agent_shim`.

Cursor supplies ``conversation_id`` and ``workspace_roots`` on stdin.  The
shared engine translates those fields into Deckbridge's stable session seam.
"""
import sys

from agent_shim import main


if __name__ == "__main__":
    raise SystemExit(main(["--agent", "cursor", *sys.argv[1:]]))
