#!/usr/bin/env python3
"""demo_driver.py - a standalone connector that paints a realistic scenario.

Not part of the real system; it exists to prove the visual stack (deckd +
emulator + protocol) end to end without any dependency on cmux or Discord. It
claims the whole deck and paints a believable board: a Hermes approval key, a
few coding agents in various states, and an ops row. Optionally cycles through a
short story so the emulator visibly animates.

    python3 demo_driver.py --ws ws://127.0.0.1:8777 [--once]
"""
import argparse, asyncio, json
import websockets

RED   = "#c0392b"; AMBER = "#d9822b"; BLUE = "#2e6fdb"; GREEN = "#1f8a4c"

def face(label, sub, color, icon, effect="solid"):
    return {"label": label, "sublabel": sub, "color": color, "icon": icon, "effect": effect}

# index -> face, a full 15-key board
SCENE_A = {
    0:  face("Hermes", "2 pending", RED,   "hermes", "breathe"),
    1:  face("#work",  "post",      BLUE,  "discord"),
    2:  face("#demo",  "post",      BLUE,  "discord"),
    3:  face("hetzner","attach",    GREEN, "terminal"),
    4:  face("brief",  "run",       GREEN, "check"),
    5:  face("",        "",         "#111111", None, "off"),
    6:  face("herd",   "1 blocked", RED,   "alert", "breathe"),
    7:  face("codex",  "working",   AMBER, "codex"),
    8:  face("claude", "blocked",   RED,   "claude", "breathe"),
    9:  face("opencode","done",     BLUE,  "agent"),
    10: face("gemini", "idle",      GREEN, "agent"),
    11: face("git",    "clean",     GREEN, "git"),
    12: face("test",   "pass",      GREEN, "check"),
    13: face("model",  "opus",      BLUE,  "model"),
    14: face("effort", "high",      BLUE,  "dial"),
}
# after user "handles" things: approval cleared, claude unblocked -> working
SCENE_B = dict(SCENE_A)
SCENE_B[0] = face("Hermes", "clear", GREEN, "hermes")
SCENE_B[6] = face("herd", "all busy", AMBER, "robot")
SCENE_B[8] = face("claude", "working", AMBER, "claude")

async def paint(ws, scene):
    faces = [dict(scene[i], index=i) for i in sorted(scene)]
    await ws.send(json.dumps({"type": "faces", "faces": faces}))

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="ws://127.0.0.1:8777")
    ap.add_argument("--once", action="store_true", help="paint SCENE_A and hold")
    args = ap.parse_args()
    async with websockets.connect(args.ws) as ws:
        await ws.send(json.dumps({"type": "hello", "role": "connector",
                                  "name": "demo", "claim": [0, 14]}))
        w = json.loads(await ws.recv())
        print("demo connected:", w)
        await paint(ws, SCENE_A)
        print("painted SCENE_A")
        if args.once:
            # hold, and echo presses so clicking keys in the emulator is visible
            async for raw in ws:
                m = json.loads(raw)
                if m.get("type") == "press":
                    print("press on key", m["index"])
        else:
            while True:
                await asyncio.sleep(4); await paint(ws, SCENE_B); print("-> SCENE_B")
                await asyncio.sleep(4); await paint(ws, SCENE_A); print("-> SCENE_A")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
