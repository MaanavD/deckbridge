#!/usr/bin/env python3
"""Contract tests for the merge-safe Claude/Codex/Cursor hook installer."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import install_hooks


passed = 0
total = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    if condition:
        passed += 1
        print(f"ok - {name}")
    else:
        print(f"not ok - {name}" + (f": {detail}" if detail else ""))


def test_cursor_tool_contract() -> None:
    spec = install_hooks.TOOLS.get("cursor", {})
    check("Cursor is a first-class installer target", bool(spec))
    check("Cursor uses the official global config path",
          str(spec.get("path", "")).endswith("/.cursor/hooks.json"), str(spec))
    check("Cursor uses its flat version-1 hook schema",
          spec.get("schema") == "flat-v1", str(spec))
    check("Cursor installs the lifecycle events deckbridge consumes",
          {"sessionStart", "beforeSubmitPrompt", "afterAgentThought",
           "afterAgentResponse", "stop", "sessionEnd"}.issubset(
               set(spec.get("events", []))), str(spec))


def test_cursor_command_records_native_host() -> None:
    command = install_hooks.command_for(
        Path("/Applications/My Tools/cursor_shim.py"), 300,
        extra_args=("--app", "Cursor"),
    )
    check("Cursor hook command quotes the shim path",
          command.startswith('"/Applications/My Tools/cursor_shim.py"'), command)
    check("Cursor hook command records Cursor as the host app",
          "--app Cursor" in command, command)
    check("Cursor hook command retains the optional ttl",
          command.endswith("--ttl 300"), command)
    check("Cursor shim commands are recognised as deckbridge-owned",
          install_hooks.is_ours(command))


def test_flat_install_merges_and_is_idempotent() -> None:
    original = {
        "version": 1,
        "theme": "keep-me",
        "hooks": {
            "beforeSubmitPrompt": [{"command": "/usr/local/bin/existing"}],
        },
    }
    command = "/repo/cursor_shim.py --app Cursor"
    merged, added, skipped = install_hooks.plan_install_flat(
        original, ["beforeSubmitPrompt", "stop"], command,
    )
    check("flat install preserves unrelated top-level settings",
          merged.get("theme") == "keep-me")
    check("flat install preserves an existing hook",
          merged["hooks"]["beforeSubmitPrompt"][0]["command"]
          == "/usr/local/bin/existing", str(merged))
    check("flat install appends deckbridge in official Cursor shape",
          merged["hooks"]["beforeSubmitPrompt"][1] == {"command": command},
          str(merged))
    check("flat install creates missing event lists",
          merged["hooks"]["stop"] == [{"command": command}], str(merged))
    check("flat install reports added events",
          added == ["beforeSubmitPrompt", "stop"] and not skipped,
          f"added={added}, skipped={skipped}")
    check("flat install does not mutate its input", len(
        original["hooks"]["beforeSubmitPrompt"]) == 1, str(original))

    again, added2, skipped2 = install_hooks.plan_install_flat(
        merged, ["beforeSubmitPrompt", "stop"], command,
    )
    check("Cursor install is idempotent",
          again == merged and not added2
          and skipped2 == ["beforeSubmitPrompt", "stop"],
          f"added={added2}, skipped={skipped2}")


def test_flat_install_updates_an_old_checkout() -> None:
    config = {
        "version": 1,
        "hooks": {"stop": [
            {"command": "/old/deckbridge/cursor_shim.py --app Cursor"},
            {"command": "/usr/local/bin/keep"},
        ]},
    }
    command = "/new/deckbridge/cursor_shim.py --app Cursor"
    merged, added, _ = install_hooks.plan_install_flat(config, ["stop"], command)
    check("flat install updates a stale checkout in place",
          merged["hooks"]["stop"] == [
              {"command": command}, {"command": "/usr/local/bin/keep"}],
          str(merged))
    check("updating a stale flat hook counts as a change", added == ["stop"])


def test_flat_uninstall_preserves_everything_else() -> None:
    config = {
        "version": 1,
        "theme": "keep-me",
        "hooks": {
            "stop": [
                {"command": "/repo/cursor_shim.py --app Cursor"},
                {"command": "/usr/local/bin/keep"},
            ],
            "sessionEnd": [{"command": "/repo/cursor_shim.py --app Cursor"}],
        },
    }
    clean, removed = install_hooks.plan_uninstall_flat(config)
    check("flat uninstall removes every Cursor shim event",
          removed == ["stop", "sessionEnd"], str(removed))
    check("flat uninstall preserves non-deckbridge hooks",
          clean["hooks"]["stop"] == [{"command": "/usr/local/bin/keep"}],
          str(clean))
    check("flat uninstall removes an empty event list",
          "sessionEnd" not in clean["hooks"], str(clean))
    check("flat uninstall preserves unrelated settings and version",
          clean.get("theme") == "keep-me" and clean.get("version") == 1,
          str(clean))


def test_cursor_apply_writes_backup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config_path = root / ".cursor" / "hooks.json"
        config_path.parent.mkdir()
        config_path.write_text(json.dumps({
            "version": 1,
            "hooks": {"stop": [{"command": "/usr/local/bin/keep"}]},
        }), encoding="utf-8")
        shim = root / "cursor_shim.py"
        shim.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        old = install_hooks.TOOLS["cursor"]
        install_hooks.TOOLS["cursor"] = {
            **old, "path": config_path, "shim": shim,
        }
        args = argparse.Namespace(uninstall=False, apply=True, ttl=None)
        try:
            ok = install_hooks.process("cursor", args)
        finally:
            install_hooks.TOOLS["cursor"] = old
        written = json.loads(config_path.read_text(encoding="utf-8"))
        backups = list(config_path.parent.glob("hooks.json.bak-*"))
        check("Cursor apply succeeds", ok)
        check("Cursor apply writes all configured events",
              all(any(install_hooks.is_ours(item.get("command")) for item in
                      written["hooks"][event])
                  for event in install_hooks.CURSOR_EVENTS), str(written))
        check("Cursor apply backs up a pre-existing config", len(backups) == 1,
              str(backups))


def test_nested_schema_still_works() -> None:
    command = "/repo/claude_shim.py"
    merged, added, skipped = install_hooks.plan_install(
        {}, ["SessionStart"], command,
    )
    check("Claude/Codex nested schema remains unchanged",
          merged == {"hooks": {"SessionStart": [{"hooks": [{
              "type": "command", "command": command,
          }]}]}} and added == ["SessionStart"] and not skipped,
          str(merged))


def test_codex_attention_state_has_a_completion_edge() -> None:
    """PermissionRequest must not be the last hook for an approved tool call."""
    events = set(install_hooks.CODEX_EVENTS)
    check("Codex installs PostToolUse to clear an approved permission request",
          "PostToolUse" in events, str(sorted(events)))

    example = json.loads((Path(__file__).parent / "codex_hooks.example.json")
                         .read_text(encoding="utf-8"))
    check("Codex JSON example matches every installed lifecycle event",
          set(example.get("hooks", {})) == events,
          str(sorted(example.get("hooks", {}))))

    toml = (Path(__file__).parent / "codex_config.example.toml").read_text(
        encoding="utf-8")
    check("Codex TOML example includes the post-tool completion hook",
          "[[hooks.PostToolUse]]" in toml)


def test_cursor_example_matches_installer() -> None:
    example = json.loads((Path(__file__).parent / "cursor_hooks.example.json")
                         .read_text(encoding="utf-8"))
    hooks = example.get("hooks", {})
    check("Cursor example uses version 1", example.get("version") == 1)
    check("Cursor example covers every installed event",
          set(hooks) == set(install_hooks.CURSOR_EVENTS), str(hooks))
    check("Cursor example uses the flat command-list schema",
          all(isinstance(items, list)
              and all(set(item) == {"command"} for item in items)
              for items in hooks.values()), str(hooks))


def main() -> int:
    test_cursor_tool_contract()
    test_cursor_command_records_native_host()
    test_flat_install_merges_and_is_idempotent()
    test_flat_install_updates_an_old_checkout()
    test_flat_uninstall_preserves_everything_else()
    test_cursor_apply_writes_backup()
    test_nested_schema_still_works()
    test_codex_attention_state_has_a_completion_edge()
    test_cursor_example_matches_installer()
    print(f"{passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
