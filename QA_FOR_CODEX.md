# QA brief for Codex: make deckbridge focus land on the right thing

You are fixing a Stream Deck integration on a Mac. The author could not test it
themselves (they work on Linux, this is a macOS window-management problem), so
every bug so far has been found by hand and fixed blind. Your advantage is that
you are *on the machine*. Use it.

Work in `~/Downloads/deckbridge` (or wherever this is extracted).

---

## The one sentence that matters

**A press must focus the specific window the agent is actually in — and the
only way to know it did is to ask the machine afterwards.**

Every bug in this project's history had the same shape: the press returned exit
code 0 while landing somewhere wrong. A blank Terminal window. cmux's default
tab. An arbitrary one of eight tabs open in the same repo. Exit codes cannot
catch that, because what is wrong is the *world*, not the return value.

So: never accept an exit code as evidence. Read back what is focused.

---

## Start here

```bash
chmod +x *.sh qa_focus.py
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install cairo          # only needed for key rendering, not for focus

.venv/bin/python3 qa_focus.py
```

That runs the QA harness. It builds real cmux tabs, writes the state files
deckbridge reads, presses keys the way the connector presses them, and then
asks cmux and macOS what is focused now.

Output looks like:

```
PASS  recorded_tty       exit=0
FAIL  ambiguous_tabs     2 tabs share this directory and no tty was recorded
      expected: refusal, focus stays on surface:12
      actual:   exit=0, focus=surface:36
SKIP  desktop_claude     Claude is not running
```

`--json` gives the same thing machine-readably, which is what you want in a
loop.

---

## The loop

```bash
while :; do
  .venv/bin/python3 qa_focus.py --json > /tmp/qa.json
  # read it, fix one thing, re-run
done
```

Rules for the loop:

1. **One failure at a time.** These bugs interact; fixing two at once makes it
   impossible to tell which change helped.
2. **After every fix, run the unit tests too:** `bash run_tests.sh` (or run each
   `test_*.py` / `test_*.sh` directly). 457 checks. Do not trade a unit test for
   a QA scenario without understanding why.
3. **A SKIP is not a pass.** It means the scenario could not be set up. Read the
   note and fix the setup — an untested path is where the next bug lives.
4. **Prove each fix.** Break it deliberately and confirm the scenario goes red
   again. A test that passes both with and without your fix tested nothing.
   (This is not hypothetical: two tests in this repo passed for the wrong reason
   until exactly this check caught them.)

---

## What each scenario is guarding

| Scenario | The real bug it replays |
|---|---|
| `recorded_tty` | Ordinary case. The hook recorded a tty, so the tab is identified exactly. |
| `recorded_surface` | A surface id needs no matching at all. Its cwd is deliberately bogus, so *only* the surface id can succeed. |
| `ambiguous_tabs` | **The big one.** Several tabs in one directory, no tty. A directory is not an identity — the only correct answer is to refuse. |
| `workspace_switch` | Focusing a panel on an unselected workspace raises the window but leaves the old tab on screen. Checks the workspace, not just the surface. |
| `desktop_claude` / `desktop_codex` | A desktop-hosted session must bring *its* app forward, not a terminal. |
| `quit_app_refuses` | A key whose app has quit must refuse. Launching a blank app is the original sin here. |

Each scenario deliberately parks focus somewhere **wrong** first. That is not
decoration: if the right tab is already focused, doing nothing looks identical
to success.

---

## Known-unresolved, and the highest-value thing you can do

**Find out how a cmux session can name its own surface.**

The resolution signals, in descending order of trustworthiness:

| Signal | Strength |
|---|---|
| A surface id the agent knows | Exact. Nothing to match. |
| The agent's tty | Exact after one lookup in the cmux tree. |
| The agent's working directory | **Not an identity.** Every tab in a repo shares it. |

The third is what caused the reported bug. The first would eliminate the whole
class. Run this **inside a cmux tab**:

```bash
./find_surface_var.sh
```

It cross-checks your environment against `cmux tree` and prints `<-- USE THIS`
if a variable holds a real surface ref. If it finds one, add the name to
`SURFACE_ENV_VARS` in `agent_shim.py`.

If it finds nothing, dig further — `cmux --help`, the docs, the config — for any
way a process can learn its own surface. That is worth more than any other fix
you could make here, because it removes the need to guess at all.

Second unknown: **`cmux select-workspace`'s exact flag.** The code tries four
invocation shapes and verifies the landing against `cmux tree`. If none work,
`workspace_switch` fails. Find the real flag and set `CMUX_SELECT_WORKSPACE_CMD`
or fix `cmux_select_workspace` in `focus_agent.sh`.

---

## Diagnosing a single press

```bash
./focus_agent.sh --diagnose --source claude-code --name probe
```

Prints the build stamp, the cmux binary and version, the recorded tty, the
recorded app, whether a deep link is possible, and which surface (if any)
matches. `FOCUS_DEBUG=1` adds the resolver's own reasoning on stderr.

Two lines to check first:

- `tty resolved: <none found>` — the hook is not recording a tty. Everything
  downstream is then a guess. Restart the agent so a fresh hook runs.
- `app recorded: <none ...>` — same cause, same fix.

---

## Hooks, and why restarting agents matters

Focus quality is decided at *record* time, not at press time. A session whose
hook ran under an older build has no tty, no app, and no session id in state —
and never will, because nothing backfills it.

```bash
python3 install_hooks.py --apply          # writes Claude + Codex hook config
# then restart your agent sessions
```

Verify a hook actually records what it should:

```bash
echo '{"session_id":"test","cwd":"'"$PWD"'","hook_event_name":"UserPromptSubmit"}' \
  | ./claude_shim.py --state /tmp/qa-state.json
cat /tmp/qa-state.json
```

You want to see `tty`, and ideally `surface`. If `tty` is missing when run from
a real cmux tab, that is a bug worth chasing — it is the root of the whole
problem.

---

## The map

| File | What it does |
|---|---|
| `focus_agent.sh` | Where a press is resolved. Most focus bugs live here. |
| `agent_shim.py` | The hook. Records tty / app / session id / surface. Fixing bugs *here* prevents them downstream. |
| `connector_agents.py` | State to key faces; press routing; the seen state and pager. |
| `renderer_hw.py` / `emulator.html` | The two renderers. They must agree. |
| `qa_focus.py` | The harness described above. |
| `test_qa_focus.py` | Tests for the harness. Run these if you change it. |
| `find_surface_var.sh` | The cmux surface-variable probe. |

---

## House style, if you are editing

Comments explain **why**, especially why an obvious simpler approach is wrong.
Most of the comments in this codebase are gravestones for a specific bug. When
you fix something, leave one — the next person to read that line needs to know
what happens if they "simplify" it back.

Do not delete a guard because it looks redundant. Several of them are load
bearing in ways the function signature does not show.
