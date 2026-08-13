# Deckbridge

Deckbridge turns an Elgato Stream Deck into a live control surface for coding
agents, communication apps, and push-to-talk dictation on macOS. Active Claude
Code, Codex, Cursor, Hermes, T3 Code, and cmux sessions can appear as keys; a tap
returns to the exact session and a long press dismisses stale work.

The project includes a browser emulator, so most of the experience can be
tested without Stream Deck hardware.

## What it does

- Shows up to ten live agent sessions with working, waiting, done, and idle
  states.
- Treats a completed result as “needs you” until its session has been opened;
- Discovers open native Claude, Codex, and Cursor desktop conversations through
  the optional Deckbridge Mic Accessibility helper, then returns to the exact
  conversation with its app deep link. Native apps do not expose reliable
  per-tab generation status, so an open desktop conversation is shown as live.
  viewed completions settle into the quiet done state.
- Reads T3 Code's authenticated local API for exact thread titles, lifecycle,
  approvals, and pending user input. T3 threads use their native provider logo.
- Focuses the exact application tab or terminal pane instead of merely raising
  an app.
- Reflows keys after sessions end or are dismissed.
- Provides persistent launchers and app shortcuts when capacity allows.
- Supports unread badges for Slack, Gmail, Discord, and Notion Calendar.
- Uses the final key as press-and-hold dictation for the frontmost supported
  app.
- Blanks the physical deck and sets its brightness to zero while macOS is
  locked, then restores the latest frame immediately on unlock.
- Reconnects feeds independently so a slow focus operation cannot block later
  button presses.
- Runs at login through a per-user macOS LaunchAgent.

## Layout

The reference layout targets the classic 15-key Stream Deck:

```text
0–9    live agent sessions
6–9    launchers while fewer than six sessions are visible
10–13  fixed app shortcuts
14     hold-to-talk microphone
```

Launchers and shortcuts are icon-only while inactive. At six or more live
sessions, keys 6–9 become session keys; the launcher row returns and the board
re-sorts when capacity is available again.

## Requirements

- macOS
- Python 3.9 or newer
- Optional: an Elgato Stream Deck and the `hidapi` library
- Optional: Claude Code, Codex, Cursor, T3 Code, cmux, Discord, or the work apps
  you want to control

Only `websockets` is required for the browser emulator. Physical rendering also
uses Pillow and StreamDeck; SVG logos use CairoSVG.

## Install

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/deckbridge.git
cd deckbridge

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Optional local settings. Both files are ignored by Git.
cp deckbridge.conf.example deckbridge.conf
mkdir -p ~/.deckbridge
cp apps.example.json ~/.deckbridge/apps.json

# Add hooks for the locally installed agent tools.
.venv/bin/python install_hooks.py --apply

# Optional: install and authenticate T3 Code for reliable managed threads.
./install_t3code.sh

# Start with the browser emulator.
./deckbridge.sh start
```

Run `/hooks` once inside Codex and trust the installed hooks. Restart Cursor
after installing its global hooks.

Useful lifecycle commands:

```bash
./deckbridge.sh doctor
./deckbridge.sh start --hw
./deckbridge.sh status
./deckbridge.sh connections
./deckbridge.sh logs
./deckbridge.sh stop
```

Missing optional integrations are skipped with a reason; they do not prevent
local agent sessions and the emulator from working.

T3 Code keys always target the installed desktop app. If a browser page asks
for a pairing or bootstrap credential, close that page and use the native T3
Code key; Deckbridge never exposes its private API credential to a browser.

### Start at login

```bash
./install_startup.sh install
./install_startup.sh status
```

The installer copies a launchd-readable runtime to
`~/Library/Application Support/Deckbridge`, installs
`~/Library/LaunchAgents/com.deckbridge.agent.plist`, and starts the physical
renderer. Run `install` again after updating the source checkout. To remove the
service:

```bash
./install_startup.sh uninstall
```

## Configuration

Machine-specific values do not belong in the repository:

- `deckbridge.conf` contains optional shell settings such as ports, an SSH host
  alias, and Discord IDs. Copy `deckbridge.conf.example` to begin.
- `~/.deckbridge/apps.json` defines launcher URLs, Chrome profiles, and fixed
  shortcuts. Copy `apps.example.json` and replace its placeholders.
- Discord tokens are read from `DISCORD_BOT_TOKEN` or a local Hermes `.env`.
  Never put a token in either repository file.
- The T3 bearer session lives at `~/.deckbridge/t3code_token` with mode 600.
  It is never copied into the repository or generated launchd runtime.

For a Hermes Discord launcher, set the URL in your private `apps.json`:

```json
{
  "label": "Hermes",
  "source": "hermes-discord",
  "bundle": "Discord",
  "url": "discord://-/channels/YOUR_GUILD_ID/YOUR_CHANNEL_ID"
}
```

Any launcher or shortcut can be replaced without changing the source. Gmail
can target an existing Chrome profile and opens a new tab in that profile's
window.

## Hold-to-talk dictation

Key 14 sends the appropriate native shortcut to whichever supported app is
frontmost. Hold the key while speaking and release it to stop/dictate.

The default routes include:

- Codex desktop: Command-Shift-D
- Claude Code: Space while held
- Cursor: Control-M
- T3 Code and ordinary text fields: macOS Dictation

Install the signed local helper and grant it Accessibility access when macOS
prompts:

```bash
./install_mic_helper.sh install
./qa_mic_live.sh status
```

In **System Settings → Privacy & Security → Accessibility**, enable
`Deckbridge Mic`. Microphone permission still belongs to the destination app.
The helper does not record or transmit audio; it sends keyboard events to the
frontmost app.

## Architecture

```text
agent and app connectors ──► deckd.py WebSocket hub ──► Stream Deck renderer
                                      │
                                      └───────────────► browser emulator
```

`deckd.py` owns the surface and routes presses to the connector that painted a
key. `connector_agents.py` merges agent feeds, assigns stable slots, and handles
focus/launch actions. `t3code_watcher.py` consumes T3's loopback API and rereads
its runtime descriptor after every app restart. `connector_mic.py` owns hold-to-talk. External feeds use
bounded retries and atomic last-good state so a temporary outage does not erase
known sessions. See [PROTOCOL.md](PROTOCOL.md) for the wire protocol.

## Development and tests

```bash
./run_tests.sh
```

The suite exercises the hub, reconnect behavior, session selection, verified
focus, dynamic layout, Discord/Hermes parsing, startup lifecycle, rendering,
and microphone press/release behavior. Some macOS integration checks require
Accessibility permission and are intentionally separate from unit tests. The
hosted CI runner skips only `test_startup.sh`, whose process-adoption checks
need a real per-user launchd/HID session; the local command runs it by default.

## Privacy and security

Deckbridge is local-first. The hub listens on loopback by default, configuration
files containing machine-specific values are ignored, and the public examples
contain placeholders only. Before sharing diagnostics, inspect local logs under
`~/Library/Logs/Deckbridge`; application titles and agent task names may appear
there.

Product names and logos are trademarks of their respective owners. Their use
identifies compatible integrations and does not imply endorsement.

## License

Deckbridge is available under the [MIT License](LICENSE).
