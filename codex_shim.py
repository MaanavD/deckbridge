#!/usr/bin/env python3
"""Codex CLI hook shim for deckbridge (thin wrapper over ``agent_shim.py``).

Point every Codex lifecycle hook at this file.  It forwards to the shared
engine with ``--agent codex``, which prefixes deck labels with ``cx-`` and tags
records ``source: codex-cli``.

Install by merging ``codex_hooks.example.json`` into ``~/.codex/hooks.json``,
or the inline TOML block from ``codex_config.example.toml`` into
``~/.codex/config.toml``.  Then run ``/hooks`` inside Codex and TRUST the new
hooks: Codex records trust against a hook's hash and silently skips untrusted
hooks, so an untrusted shim looks exactly like a broken one.

Codex differences worth knowing versus Claude Code:

* There is **no ``SessionEnd`` event**, so a finished session is aged off the
  deck by the ``--ttl`` timer (default 900s) rather than evicted on exit.
* There is no ``StopFailure`` event either; a failed turn surfaces as ``Stop``.
* Hook stdout is parsed by Codex, and ``Stop``/``SubagentStop`` expect JSON.
  This shim writes nothing to stdout, which is the documented success case.
  Never add ``--print`` to a real hook config.
* Codex runs matching hooks from every config layer concurrently.  Writes here
  are atomic per invocation, so concurrent hooks cannot corrupt the state file,
  though the last writer wins for one agent's status.

See ``agent_shim.py`` for the full event mapping and safety properties.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_shim import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["--agent", "codex", *sys.argv[1:]]))
