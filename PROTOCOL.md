# deckbridge wire protocol v1

A single daemon (`deckd`) owns the Stream Deck (real hardware or the HTML
emulator). Everything else is a **connector**: a WebSocket client that claims a
contiguous range of keys, paints their faces, and receives press events for keys
it owns. This is deliberate: the Work Louder hacking notes and the cmux/Herdr
docs all warn that two processes driving one deck overwrite each other. deckd is
the one owner; connectors never touch the device.

## Transport

WebSocket, JSON text frames, one JSON object per frame. Default endpoint:
`ws://127.0.0.1:8777`. deckd is the server. Connectors and renderers connect in.

Two client roles connect to the same hub:
- **connector** — claims keys, sets faces, gets presses.
- **renderer** — the surface (hardware bridge or HTML emulator). Receives the
  full composited key state, sends raw press/release events back.

## Key model

The classic Stream Deck (MK2) is 15 keys, row-major, indices 0..14 in a 5x3
grid. Neo is 8. deckd is told `--keys N` (default 15). Each key has a *face*:

```json
{
  "index": 3,
  "label": "codex",
  "sublabel": "working",
  "color": "#d9822b",
  "icon": null,
  "badge": "X",
  "effect": "shimmer",
  "layout": "status"
}
```

- `color`  — background, `#RRGGBB`.
- `effect` — one of `solid`, `breathe`, `blink`, `shimmer`, `off`. Renderers animate.
- `layout` — `status` (default) or `logo-only`. The latter centers a large
  product glyph and suppresses all label/status/corner metadata for launchers.
  `icon-action` centers a large glyph with one compact action caption.
- `notification_count` — non-negative unread count for a launcher. Renderers
  draw it as a red top-right bubble and display counts above 99 as `99+`.
- `icon`   — optional name from the shared icon set (agent, discord, check,
  alert, robot, git). Renderer maps name→glyph/image. `null` = text only.
- `badge`  — optional corner tag, max 2 chars, identifying which tool owns the
  key: `H` Hermes Discord thread, `S` Hermes over ssh, `C` Claude Code,
  `X` Codex CLI, `R` Cursor Agent, `M` cmux. This exists so labels can carry
  the agent's own name instead of spending scarce characters on a
  `cc-`/`cx-`/`cu-` prefix. deckd
  whitelists face fields, so a field absent from that whitelist is dropped
  silently — anything new must be added there as well as here.
- `label` / `sublabel` — up to ~8 / ~10 chars; renderer truncates.

Animation remains a renderer concern. A connector describes state, never
choreography: the shared working status ring spins, and a renderer may choose a
restrained product-mark motion from the existing `source` while `effect` is
`shimmer` (Claude rocks, Codex pulses, Hermes floats). No animation-specific
wire fields are required. The HTML renderer disables decorative motion for
`prefers-reduced-motion`; the hardware renderer redraws at roughly 12 fps and
updates only animated or freshly pressed keys. Slower breathe/blink effects
are sampled at 4 fps so they do not pay the working spinner's HID-write rate.
Every received state frame is still drawn once in full, so a transition to
`solid` or `off` is immediate.

## Frames: connector → deckd

```json
{"type": "hello",  "role": "connector", "name": "cmux-local", "claim": [6, 14]}
{"type": "face",   "index": 6, "face": { ...face without index... }}
{"type": "faces",  "faces": [ {"index":6, ...}, {"index":7, ...} ]}
{"type": "release_claim"}
{"type": "ping"}
```

- `claim` is an inclusive `[first, last]` range. deckd rejects overlapping
  claims from two live connectors (first-come wins; the loser gets
  `{"type":"error","reason":"range_taken"}`).
- A connector may only set faces for indices inside its claim; out-of-range
  `face` frames are dropped with a `warn`.

## Frames: deckd → connector

```json
{"type": "welcome", "keys": 15, "claim": [6,14], "grid": [5,3]}
{"type": "press",   "index": 6}
{"type": "release", "index": 6}
{"type": "error",   "reason": "range_taken", "detail": "6..14 overlaps cmux-local"}
{"type": "warn",    "detail": "face index 2 outside claim 6..14"}
{"type": "pong"}
```

## Frames: renderer ↔ deckd

Renderer connects with `{"type":"hello","role":"renderer","name":"emulator"}`.
deckd answers `welcome` then streams the **full composited state** whenever any
key changes:

```json
{"type": "state", "keys": 15, "grid": [5,3], "faces": [ {face0}, {face1}, ... ]}
```

Unclaimed / unset keys are rendered as `{"effect":"off","color":"#111111"}`.

Renderer → deckd on physical input:

```json
{"type": "press",   "index": 6}
{"type": "release", "index": 6}
```

deckd routes the press to whichever connector owns that key. If no connector
owns it, the press is dropped.

## Lifecycle

1. deckd starts, opens the device or waits for an emulator renderer.
2. Connectors connect, `hello` with a claim, get `welcome`.
3. Connectors stream `face`/`faces`. deckd composites and pushes `state` to the
   renderer.
4. Renderer sends `press`; deckd routes to the owning connector.
5. On connector disconnect, its claimed keys reset to `off` and the range frees.

## Standard color semantics (shared by both connectors)

| meaning                    | color     | effect   |
|----------------------------|-----------|----------|
| blocked / needs you        | `#c0392b` | breathe  |
| working                    | `#d9822b` | shimmer  |
| done, unseen               | `#2e6fdb` | solid    |
| idle / seen / quiet        | `#1f8a4c` | solid    |
| empty slot                 | `#111111` | off      |

This is the micro-manager palette (red/amber/blue/green) restated so both the
cmux connector and the Hermes connector speak one visual language.
