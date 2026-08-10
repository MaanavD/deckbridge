# Testing Deckbridge on macOS

This guide checks the browser emulator first, then optional local integrations
and Stream Deck hardware.

## 1. Emulator smoke test

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run_demo.sh
```

Open the printed local URL if the browser does not open automatically. In a
second terminal:

```bash
.venv/bin/python seed_state.py --animate
```

Confirm that all 15 keys render, agent states animate, and clicking a key is
reported by the owning connector. Stop the demo with Control-C.

## 2. Agent hooks

```bash
.venv/bin/python install_hooks.py --dry-run
.venv/bin/python install_hooks.py --apply
```

Restart tools that cache their configuration. In Codex, run `/hooks` and trust
the installed hooks. Start a supported agent, submit a short prompt, and verify
that its compact task title appears. A tap should focus its exact tab or pane;
a long press should dismiss it and immediately reflow the remaining keys.

## 3. Hold-to-talk

```bash
./install_mic_helper.sh install
./qa_mic_live.sh status
```

Enable **Deckbridge Mic** in **System Settings → Privacy & Security →
Accessibility**. Place the cursor in a text field in the app under test. Hold
key 14, speak, then release it. Repeat in Codex desktop, Claude Code, Cursor,
and an ordinary macOS text field. `./qa_mic_live.sh status` reports the detected
frontmost app and selected route when troubleshooting.

## 4. Physical hardware

Install the optional renderer dependencies described in `requirements.txt`,
connect the Stream Deck, and run:

```bash
./deckbridge.sh doctor
./deckbridge.sh start --hw
./deckbridge.sh status
```

The status command should name the hub, connectors, feeds, and hardware
renderer as healthy. Use `./deckbridge.sh logs renderer_hw` if the device is
already open in another process or HID access fails.

## 5. Full test suite

```bash
./run_tests.sh
```

The suite is hardware-independent. A few live focus and Accessibility checks
have dedicated QA commands because they depend on the applications and windows
currently open on the Mac.
