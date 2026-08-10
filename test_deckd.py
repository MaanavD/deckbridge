#!/usr/bin/env python3
"""End-to-end smoke test for the deckd hub: no hardware, no browser.

Starts deckd in-process, connects a fake renderer and a fake connector, then:
  1. connector claims [6,14], paints key 6
  2. asserts renderer received a state frame with that face
  3. renderer sends press index=6
  4. asserts connector received the routed press
  5. tests overlap rejection and out-of-claim face warning
Exit 0 = all good.
"""
import asyncio, json, sys
import websockets
import deckd

HOST, PORT = "127.0.0.1", 8799
results = []
def check(name, cond):
    results.append((name, cond))
    print(("PASS" if cond else "FAIL"), name)

def test_clean_face():
    """The hub allowlists face fields, so a field it forgets is dropped in
    transit even when both the connector and the renderer handle it. That is
    exactly how the source logo silently failed to reach the deck."""
    out = deckd.clean_face({"label": "x", "source": "claude-code"})
    check("source survives clean_face", out["source"] == "claude-code")

    off = deckd.clean_face({})
    check("missing source defaults to empty", off["source"] == "")

    # A renderer turns source into logos/<source>.svg, so a connector must not
    # be able to point it anywhere it likes.
    for bad in ("../../etc/passwd", "a/b", "UPPER", "with space", "-lead"):
        got = deckd.clean_face({"source": bad})["source"]
        check(f"rejects source {bad!r}", got == "")

    check("accepts hyphenated id", deckd.clean_face({"source": "hermes-ssh"})["source"] == "hermes-ssh")
    check("working shimmer survives clean_face",
          deckd.clean_face({"effect": "shimmer"})["effect"] == "shimmer")
    check("icon-only layout survives clean_face",
          deckd.clean_face({"layout": "logo-only"})["layout"] == "logo-only")
    check("icon-action layout survives clean_face",
          deckd.clean_face({"layout": "icon-action"})["layout"] == "icon-action")
    check("unknown layout falls back to status",
          deckd.clean_face({"layout": "giant-text"})["layout"] == "status")
    check("notification count survives clean_face",
          deckd.clean_face({"notification_count": 12})["notification_count"] == 12)
    check("bad notification counts are safe",
          deckd.clean_face({"notification_count": "nope"})["notification_count"] == 0)
    check("notification counts are bounded",
          deckd.clean_face({"notification_count": 5000})["notification_count"] == 999)
    check("truncated long source is rejected or bounded",
          len(deckd.clean_face({"source": "a" * 99})["source"]) <= 32)


async def run():
    test_clean_face()
    hub = deckd.Hub(15)
    server = await websockets.serve(lambda ws: deckd.handle(ws, hub), HOST, PORT)
    url = f"ws://{HOST}:{PORT}"
    try:
        # renderer
        rend = await websockets.connect(url)
        await rend.send(json.dumps({"type":"hello","role":"renderer","name":"test-rend"}))
        w = json.loads(await rend.recv()); check("renderer welcome", w["type"]=="welcome" and w["keys"]==15)
        st = json.loads(await rend.recv()); check("initial state frame", st["type"]=="state" and len(st["faces"])==15)

        # connector claims [6,14]
        con = await websockets.connect(url)
        await con.send(json.dumps({"type":"hello","role":"connector","name":"cmux","claim":[6,14]}))
        cw = json.loads(await con.recv()); check("connector welcome+claim", cw["type"]=="welcome" and cw["claim"]==[6,14])
        st2 = json.loads(await rend.recv()); check("state after claim", st2["type"]=="state")

        # paint key 6
        await con.send(json.dumps({"type":"face","index":6,
            "face":{"label":"codex","sublabel":"working","color":"#d9822b","icon":"codex","effect":"solid"}}))
        st3 = json.loads(await rend.recv())
        f6 = st3["faces"][6]
        check("face painted to renderer", f6["label"]=="codex" and f6["color"]=="#d9822b" and f6["effect"]=="solid")

        # out-of-claim face -> warn
        await con.send(json.dumps({"type":"face","index":2,"face":{"label":"nope"}}))
        wn = json.loads(await con.recv()); check("out-of-claim warn", wn["type"]=="warn")

        # renderer press index 6 -> routed to connector
        await rend.send(json.dumps({"type":"press","index":6}))
        pr = json.loads(await con.recv()); check("press routed to owner", pr["type"]=="press" and pr["index"]==6)

        # press on unowned key 0 -> nothing routed (use ping to prove channel alive & empty)
        await rend.send(json.dumps({"type":"press","index":0}))
        await con.send(json.dumps({"type":"ping"}))
        pong = json.loads(await con.recv()); check("unowned press dropped (got pong not press)", pong["type"]=="pong")

        # overlap rejection: second connector claims [10,12]
        con2 = await websockets.connect(url)
        await con2.send(json.dumps({"type":"hello","role":"connector","name":"clash","claim":[10,12]}))
        er = json.loads(await con2.recv()); check("overlap rejected", er["type"]=="error" and er["reason"]=="range_taken")
        await con2.close()

        # disconnect connector -> keys freed, renderer sees key6 reset to off.
        # Drain frames (con2's rejected-close also emits a state) until key6 is off.
        await con.close()
        freed = False
        for _ in range(5):
            try:
                stx = json.loads(await asyncio.wait_for(rend.recv(), timeout=1.0))
            except asyncio.TimeoutError:
                break
            if stx["type"] == "state" and stx["faces"][6]["effect"] == "off":
                freed = True
                break
        check("key freed on disconnect", freed)

        await rend.close()
    finally:
        server.close(); await server.wait_closed()

    ok = all(c for _, c in results)
    print(f"\n{sum(c for _,c in results)}/{len(results)} passed")
    sys.exit(0 if ok else 1)

asyncio.run(run())
