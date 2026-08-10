#!/usr/bin/env python3
"""deckd - the deckbridge hub.

Single owner of the Stream Deck surface. Connectors (WebSocket clients) claim
key ranges and paint faces; a renderer (hardware bridge or HTML emulator)
receives the composited state and sends back physical presses. deckd routes
each press to the connector that owns that key.

See PROTOCOL.md for the wire format. This process talks to NO hardware itself;
the hardware renderer (renderer_hw.py) is a separate client, so the emulator and
the real deck are interchangeable and deckd never blocks on USB.

Run:
    python3 deckd.py --keys 15 --host 127.0.0.1 --port 8777
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

import websockets

log = logging.getLogger("deckd")

OFF_FACE = {
    "label": "", "sublabel": "", "badge": "", "source": "", "logo": "",
    "color": "#111111", "icon": None, "effect": "off", "seen": False,
    "layout": "status", "notification_count": 0,
}

#: Face fields forwarded to renderers.  This is an allowlist, so a field a
#: connector sets but this tuple omits is silently dropped -- which is exactly
#: how the source logo went missing after being wired up at both ends.
FACE_KEYS = ("label", "sublabel", "badge", "source", "logo", "color", "icon",
             "effect", "seen", "layout", "notification_count")


def clean_face(face: dict) -> dict:
    """Coerce a connector-supplied face into the canonical shape."""
    out = dict(OFF_FACE)
    for k in FACE_KEYS:
        if k in face and face[k] is not None:
            out[k] = face[k]
    # sanity: label/sublabel to str, truncate defensively
    out["label"] = str(out["label"])[:16]
    out["sublabel"] = str(out["sublabel"])[:16]
    # The badge is a corner glyph, so only a couple of characters can ever fit.
    out["badge"] = str(out["badge"])[:2]
    # A renderer turns the source into a filename (logos/<source>.svg), so it
    # is constrained here rather than trusted: a connector is not allowed to
    # steer a renderer at an arbitrary path.
    source = str(out["source"])[:32]
    out["source"] = source if re.fullmatch(r"[a-z0-9][a-z0-9-]*", source) else ""
    if out["effect"] not in ("solid", "breathe", "blink", "shimmer", "off"):
        out["effect"] = "solid"
    if out["layout"] not in ("status", "logo-only", "icon-action"):
        out["layout"] = "status"
    try:
        out["notification_count"] = max(0, min(999, int(out["notification_count"])))
    except (TypeError, ValueError):
        out["notification_count"] = 0
    return out


@dataclass(eq=False)
class Client:
    ws: object
    role: str = "connector"          # "connector" | "renderer"
    name: str = "?"
    claim: Optional[tuple] = None    # (first, last) inclusive, connectors only


class Hub:
    def __init__(self, keys: int):
        self.keys = keys
        self.grid = self._grid_for(keys)
        self.faces = [dict(OFF_FACE) for _ in range(keys)]
        self.owner = [None] * keys          # index -> Client (connector) or None
        self.clients: set[Client] = set()
        self.renderers: set[Client] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def _grid_for(keys: int) -> list:
        return {6: [3, 2], 8: [4, 2], 15: [5, 3], 32: [8, 4]}.get(keys, [keys, 1])

    # ---- state broadcast -------------------------------------------------
    def state_frame(self) -> str:
        return json.dumps({
            "type": "state",
            "keys": self.keys,
            "grid": self.grid,
            "faces": [dict(f, index=i) for i, f in enumerate(self.faces)],
        })

    async def push_state(self):
        if not self.renderers:
            return
        frame = self.state_frame()
        dead = []
        for r in self.renderers:
            try:
                await r.ws.send(frame)
            except Exception:
                dead.append(r)
        for r in dead:
            self.renderers.discard(r)

    # ---- claim management ------------------------------------------------
    def range_free(self, first: int, last: int, me: Client) -> Optional[str]:
        if first < 0 or last >= self.keys or first > last:
            return f"range {first}..{last} outside 0..{self.keys-1}"
        for i in range(first, last + 1):
            o = self.owner[i]
            if o is not None and o is not me:
                return f"{first}..{last} overlaps {o.name}"
        return None

    async def set_claim(self, client: Client, first: int, last: int):
        client.claim = (first, last)
        for i in range(first, last + 1):
            self.owner[i] = client

    async def free_client(self, client: Client):
        if client.claim:
            f, l = client.claim
            for i in range(f, l + 1):
                if self.owner[i] is client:
                    self.owner[i] = None
                    self.faces[i] = dict(OFF_FACE)
        self.clients.discard(client)
        self.renderers.discard(client)
        await self.push_state()

    # ---- face updates ----------------------------------------------------
    async def apply_face(self, client: Client, index: int, face: dict) -> Optional[str]:
        if not client.claim:
            return "no claim"
        f, l = client.claim
        if index < f or index > l:
            return f"face index {index} outside claim {f}..{l}"
        self.faces[index] = clean_face(face)
        return None

    # ---- press routing ---------------------------------------------------
    async def route_press(self, index: int, kind: str):
        if index < 0 or index >= self.keys:
            return
        o = self.owner[index]
        if o is None:
            return
        try:
            await o.ws.send(json.dumps({"type": kind, "index": index}))
        except Exception:
            pass


async def handle(ws, hub: Hub):
    client = Client(ws=ws)
    hub.clients.add(client)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")

            if t == "hello":
                client.role = msg.get("role", "connector")
                client.name = str(msg.get("name", "?"))[:32]
                if client.role == "renderer":
                    hub.renderers.add(client)
                    await ws.send(json.dumps({
                        "type": "welcome", "keys": hub.keys, "grid": hub.grid}))
                    await ws.send(hub.state_frame())
                    log.info("renderer connected: %s", client.name)
                else:
                    claim = msg.get("claim")
                    if claim:
                        first, last = int(claim[0]), int(claim[1])
                        err = hub.range_free(first, last, client)
                        if err:
                            await ws.send(json.dumps({"type": "error", "reason": "range_taken", "detail": err}))
                            continue
                        await hub.set_claim(client, first, last)
                    await ws.send(json.dumps({
                        "type": "welcome", "keys": hub.keys,
                        "claim": list(client.claim) if client.claim else None,
                        "grid": hub.grid}))
                    log.info("connector connected: %s claim=%s", client.name, client.claim)
                    await hub.push_state()

            elif t == "face":
                err = await hub.apply_face(client, int(msg["index"]), msg.get("face", {}))
                if err:
                    await ws.send(json.dumps({"type": "warn", "detail": err}))
                else:
                    await hub.push_state()

            elif t == "faces":
                changed = False
                for item in msg.get("faces", []):
                    idx = int(item.get("index"))
                    err = await hub.apply_face(client, idx, item)
                    changed = changed or (err is None)
                if changed:
                    await hub.push_state()

            elif t in ("press", "release") and client.role == "renderer":
                await hub.route_press(int(msg["index"]), t)

            elif t == "release_claim":
                await hub.free_client(client)
                client.claim = None

            elif t == "ping":
                await ws.send(json.dumps({"type": "pong"}))

    except websockets.ConnectionClosed:
        pass
    finally:
        await hub.free_client(client)
        log.info("disconnected: %s (%s)", client.name, client.role)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", type=int, default=15)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    hub = Hub(args.keys)
    log.info("deckd up on ws://%s:%d  keys=%d grid=%s", args.host, args.port, hub.keys, hub.grid)
    async with websockets.serve(lambda ws: handle(ws, hub), args.host, args.port):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
