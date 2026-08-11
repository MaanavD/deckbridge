#!/usr/bin/env python3
"""renderer_hw.py - deckbridge hardware renderer for a physical Elgato Stream Deck.

Connects to deckd as a *renderer* client (see PROTOCOL.md), receives the full
composited `state` frame whenever any key changes, renders each face to a key
image with Pillow, and pushes it to the device via python-elgato-streamdeck.
Physical key presses are sent back to deckd, which routes them to the owning
connector.

This is the counterpart to emulator.html: the deck and the browser are
interchangeable renderers. deckd never touches USB; only this process does,
which is exactly what keeps the "one owner of the device" rule (the trap the
Work Louder / micro-manager notes warn about) satisfied.

macOS (M5 MacBook) setup:
    brew install hidapi                     # the HID backend
    python3 -m venv .venv && . .venv/bin/activate
    pip install 'streamdeck==0.9.5' pillow websockets
    python3 renderer_hw.py --ws ws://127.0.0.1:8777

Linux setup additionally needs a udev rule so a non-root user can open the deck;
see README. This file imports StreamDeck lazily so the rest of deckbridge (hub,
emulator, connectors, tests) runs on a box with no deck and no hidapi.

Effects (breathe/blink/shimmer), the working ring, and source-shaped logo
motions are animated locally at a low frame rate. Only moving/pressed keys are
rewritten; solid/off are drawn once per state change.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import math
import signal
import subprocess
import sys
import time
from typing import Callable, Optional

import websockets

# Pillow is a hard dep of the render path but not of import; guard for clarity.
from PIL import Image, ImageDraw, ImageFont

import logos
from connection_runtime import HealthReporter

#: Edge length of the corner source logo, in pixels.  A Stream Deck key is
#: 72x72, so 18 reads clearly without crowding the label.
BADGE_PX = 18

#: Some marks need more pixels than a single-path glyph.  The Nous mark is a
#: drawn face rather than a silhouette, and at 18px its features collapse into
#: an unreadable blob, so it gets a larger box.  Kept as a per-source override
#: rather than raising BADGE_PX for everything, which would crowd the label on
#: keys whose logos are already legible small.
BADGE_PX_BY_SOURCE = {
    "hermes-discord": 26,
    "hermes-ssh": 26,
    "hermes": 26,
    "nous": 26,
}


def badge_px(source: str) -> int:
    return BADGE_PX_BY_SOURCE.get(source, BADGE_PX)


#: Status icon box.  Larger than a product logo: the status is the thing you
#: read from across the desk, the logo only answers "which tool" once you are
#: already looking.
ICON_PX = 22
LOGO_ONLY_PX = 46

#: Working motion needs enough steps to read as a spin on a 72px display.
#: Breathe/blink are much slower effects and do not: sending them at this full
#: rate made a single working key plus the mic's ``setup needed`` key cost 24
#: full key images (48 HID reports on an Original deck) every second.
ANIMATION_FPS = 12
SLOW_EFFECT_DIVISOR = 3
DEVICE_RETRY_SECONDS = 2.0
POWER_POLL_SECONDS = 0.5
HARDWARE_LABEL_CHARS = 11


OFF_FACE = {"label": "", "sublabel": "", "color": "#111111", "icon": None, "effect": "off"}

# Text glyphs standing in for icons on the physical keys. The deck renders real
# pixels, so we use short unicode marks that a bundled TTF can draw. Swap for
# real PNG assets later by mapping name -> file in ICON_ASSETS.
ICON_GLYPHS = {
    "agent": "AI", "robot": "AI", "discord": "DC", "check": "OK",
    # The pager cycles forward only; the arrow says which way it goes.
    "page": "↻",
    "alert": "!", "git": "git", "claude": "C", "codex": "cx",
    "hermes": "H", "terminal": ">_", "model": "M", "dial": "()",
    "mic": "MIC",
}


def hex_to_rgb(h: str):
    if not h or h[0] != "#" or len(h) < 7:
        return (17, 17, 17)
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def dim(rgb, mul):
    return tuple(max(0, min(255, int(c * mul))) for c in rgb)


def macos_session_locked() -> bool:
    """Return whether the active macOS console session is screen-locked.

    ``IOConsoleLocked`` is the root registry property macOS itself updates on
    lock/unlock. Restrict the query to Root and match that exact property. A
    failed probe is treated as unlocked: transient inspection failure must not
    strand an awake user's deck in darkness.
    """
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-n", "Root", "-d", "1", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=1.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return b'"IOConsoleLocked" = Yes' in result.stdout


def animate_logo_mark(logo, source: str, phase: float):
    """Return one restrained, source-shaped working frame for a corner mark.

    The wire protocol stays semantic (``effect=shimmer``); renderers choose a
    motion from the existing ``source`` field.  No connector needs to know how
    a Claude spark, Codex knot, or Hermes portrait should move.
    """
    resampling = getattr(getattr(Image, "Resampling", Image), "BICUBIC", 3)
    if source == "claude-code":
        # The Claude mark is radial: a small rocking rotation reads as a spark
        # without turning the corner badge into a distracting propeller.
        return logo.rotate(
            -7.0 * math.sin(phase), resample=resampling, expand=False,
        )
    if source in ("codex-cli", "codex"):
        # The six-fold Codex knot looks unchanged under a simple rotation.
        # A quiet contraction/expansion makes it feel alive and keeps its form.
        scale = 0.90 + 0.10 * (0.5 + 0.5 * math.sin(phase))
        size = max(1, round(logo.width * scale))
        scaled = logo.resize((size, size), resampling)
        frame = Image.new("RGBA", logo.size, (0, 0, 0, 0))
        frame.paste(scaled, ((logo.width - size) // 2, (logo.height - size) // 2))
        return frame
    if source in ("hermes-discord", "hermes-ssh", "hermes", "nous"):
        # A portrait should not spin. Let it float by one physical pixel.
        offset = -round(math.sin(phase))
        frame = Image.new("RGBA", logo.size, (0, 0, 0, 0))
        frame.paste(logo, (0, offset))
        # Translation can move the cleaned raster's first interior row back
        # onto the perimeter. Re-establish the one-pixel transparent guard on
        # every floating frame so motion never resurrects the white matte.
        clean = Image.new("RGBA", logo.size, (0, 0, 0, 0))
        clean.paste(
            frame.crop((1, 1, frame.width - 1, frame.height - 1)),
            (1, 1),
        )
        return clean
    return logo


class HWRenderer:
    def __init__(
        self, ws_url: str, brightness: int,
        health: HealthReporter | None = None,
        session_locked: Callable[[], bool] = macos_session_locked,
    ):
        self.ws_url = ws_url
        self.brightness = brightness
        self.deck = None
        self.font = None
        self.font_small = None
        self.key_size = (72, 72)
        self.faces = []
        self.grid = [5, 3]
        self._ws = None
        self._loop = None
        self._pressed_until = {}
        self._session_locked = session_locked
        self._display_suspended = False
        self._next_power_check = 0.0
        self.health = health

    # ---- device --------------------------------------------------------
    def open_device(self):
        from StreamDeck.DeviceManager import DeviceManager  # lazy import
        decks = DeviceManager().enumerate()
        visual = [d for d in decks if d.is_visual()]
        if not visual:
            raise RuntimeError("no visual Stream Deck found")
        self.deck = visual[0]
        self.deck.open()
        self.deck.reset()
        self._display_suspended = self._session_locked()
        self.deck.set_brightness(0 if self._display_suspended else self.brightness)
        self.key_size = self.deck.key_image_format()["size"]
        self._load_fonts()
        print(f"[hw] opened {self.deck.deck_type()} "
              f"serial={self.deck.get_serial_number()} keys={self.deck.key_count()} "
              f"keysize={self.key_size}")
        return self.deck

    def set_display_suspended(self, suspended: bool) -> bool:
        """Blank/restore the physical deck; return whether state changed."""
        suspended = bool(suspended)
        if suspended == self._display_suspended:
            return False
        self._display_suspended = suspended
        deck = self.deck
        if deck is None:
            return True
        with deck:
            if suspended:
                deck.reset()
                deck.set_brightness(0)
            else:
                deck.set_brightness(self.brightness)
        if not suspended:
            # State may have changed while writes were suppressed. Repaint the
            # complete latest frame immediately instead of waiting for deckd.
            self.push_all(time.time())
        return True

    def refresh_display_power(self) -> bool:
        """Apply the current macOS lock state to the physical display."""
        return self.set_display_suspended(self._session_locked())

    def blank_and_close(self) -> None:
        """Leave no stale illuminated frame when the renderer exits."""
        if self.deck is None:
            return
        with suppress(Exception):
            with self.deck:
                self.deck.reset()
                self.deck.set_brightness(0)
        with suppress(Exception):
            self.deck.close()

    def _load_fonts(self):
        # Try a few common fonts; fall back to PIL default (still renders).
        candidates = [
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        big = small = None
        for c in candidates:
            try:
                big = ImageFont.truetype(c, 15)
                small = ImageFont.truetype(c, 11)
                break
            except Exception:
                continue
        self.font = big or ImageFont.load_default()
        self.font_small = small or ImageFont.load_default()

    # ---- render one key ------------------------------------------------
    def render_face(self, face: dict, phase: float = 1.0, pressed: bool = False):
        w, h = self.key_size
        effect = face.get("effect", "off")
        base = hex_to_rgb(face.get("color", "#111111"))
        if effect == "off":
            base = (13, 15, 18)
        mul = 1.0
        if effect == "breathe":
            mul = 0.55 + 0.55 * (0.5 + 0.5 * math.sin(phase))
        elif effect == "blink":
            mul = 1.0 if (phase % (2 * math.pi)) < math.pi else 0.25
        if pressed:
            mul *= 0.62
        # vertical gradient for a bit of depth
        img = Image.new("RGB", (w, h), dim(base, 0.85 * mul))
        draw = ImageDraw.Draw(img)
        top = dim(base, 1.12 * mul)
        for y in range(h):
            t = y / max(1, h - 1)
            row_mul = 1.0
            if effect == "shimmer":
                progress = (phase % (2 * math.pi)) / (2 * math.pi)
                centre = progress * (h + 32) - 16
                row_mul += max(0.0, 1.0 - abs(y - centre) / 11.0) * 0.38
            row = tuple(min(255, int((top[i] + (dim(base, 0.8 * mul)[i] - top[i]) * t) * row_mul))
                        for i in range(3))
            draw.line([(0, y), (w, y)], fill=row)
        if pressed:
            draw.rectangle((1, 1, w - 2, h - 2), outline=(255, 255, 255), width=2)

        # Inactive launchers are visual muscle-memory targets, not status
        # cards. Give the product mark the whole key and draw no secondary
        # metadata or corner badge.
        if face.get("layout") in ("logo-only", "icon-action"):
            action_layout = face.get("layout") == "icon-action"
            source = face.get("source", "")
            logo_size = LOGO_ONLY_PX - 4 if action_layout else LOGO_ONLY_PX
            logo = logos.load(source, size=logo_size, colour="#ffffff")
            if logo is not None:
                centre_y = (h - 10) // 2 if action_layout else h // 2
                img.paste(
                    logo,
                    ((w - logo.width) // 2, centre_y - logo.height // 2),
                    logo,
                )
            else:
                fallback = logos.badge_letter(source)
                if fallback:
                    draw.text(
                        (w / 2, h / 2), fallback, font=self.font,
                        anchor="mm", fill="white",
                    )
            if action_layout:
                draw.text(
                    (w / 2, h - 7),
                    (face.get("sublabel") or "hold to talk").upper(),
                    font=self.font_small or ImageFont.load_default(),
                    anchor="mm", fill="white",
                )
            count = max(0, int(face.get("notification_count") or 0))
            if count:
                text = "99+" if count > 99 else str(count)
                font = self.font_small or ImageFont.load_default()
                box = draw.textbbox((0, 0), text, font=font)
                tw = box[2] - box[0]
                pill_w = max(18, tw + 10)
                x1, y1 = w - pill_w - 3, 3
                draw.rounded_rectangle(
                    (x1, y1, w - 3, 21), radius=9,
                    fill=(255, 59, 48), outline=(255, 125, 117), width=1,
                )
                draw.text(
                    ((x1 + w - 3) / 2, 12), text, font=font,
                    anchor="mm", fill="white",
                )
            return img

        icon = face.get("icon")
        glyph = ICON_GLYPHS.get(icon) if icon else None
        # Eleven compact characters fit at the small font size on a 72px key.
        # The old unconditional eight-character slice turned useful task names
        # such as "Sess titles" back into cryptic fragments.
        label = (face.get("label") or "")[:HARDWARE_LABEL_CHARS]
        sub = (face.get("sublabel") or "")[:12]
        badge = (face.get("badge") or "")[:2]

        cy = 8
        if glyph or icon:
            # Centre in the width the corner mark leaves free, not in the whole
            # key.  At w/2 the glyph runs under the mark: harmless with an 18px
            # letter badge, but the 26px Nous face overlapped "OK" into an
            # unreadable smear.  Both carry meaning, so neither may be drawn
            # over the other.
            free = w - badge_px(face.get("source", "")) - 6
            art = logos.load_icon(icon, size=ICON_PX, colour="#ffffff") if icon else None
            if art is None and icon in ("working", "agent"):
                # CairoSVG is optional and macOS does not always expose
                # Homebrew's libcairo to launch agents. Keep the most important
                # animated status graphical even on that fallback path.
                art = Image.new("RGBA", (ICON_PX, ICON_PX), (0, 0, 0, 0))
                fallback = ImageDraw.Draw(art)
                fallback.arc(
                    (3, 3, ICON_PX - 4, ICON_PX - 4),
                    start=-75,
                    end=245,
                    fill=(255, 255, 255, 255),
                    width=3,
                )
            if art is not None:
                if icon in ("working", "agent"):
                    # working.svg is an open ring. Rotating the gap makes the
                    # status itself read as active instead of relying on the
                    # background shimmer to carry all of the motion.
                    resampling = getattr(getattr(Image, "Resampling", Image), "BICUBIC", 3)
                    art = art.rotate(
                        -math.degrees((phase * 2) % (2 * math.pi)),
                        resample=resampling,
                        expand=False,
                    )
                img.paste(art, (int(free / 2 - ICON_PX / 2), cy - 2), art)
                cy += ICON_PX
            elif glyph:
                # Text fallback for a machine that cannot rasterise SVG.
                draw.text((free / 2, cy + 8), glyph, font=self.font, anchor="mm",
                          fill="white")
                cy += 22
        if label:
            # With no glyph the label would sit at the very top, inside the band
            # the corner logo occupies, and collide with it (a launcher labelled
            # "Discord" ran straight through the Discord mark).  A glyph-less
            # face centres its label in the space between the logo band and the
            # sublabel instead of hugging the top.
            # A face with no status mark at all would put its label at the very
            # top, inside the band the corner logo occupies, and collide with it
            # (a launcher labelled "Discord" ran straight through the Discord
            # mark).  Such a face centres its label below the logo band instead.
            drew_mark = cy > 8
            label_y = cy + 12 if drew_mark else max(cy + 12, badge_px(face.get("source", "")) + 12)
            label_font = self.font_small if len(label) > 8 else self.font
            draw.text((w / 2, label_y), label, font=label_font,
                      anchor="mm", fill="white")
            cy = label_y + 8
        if sub:
            draw.text((w / 2, h - 10), sub.upper(), font=self.font_small, anchor="mm",
                      fill=(235, 235, 235))
        # Corner mark naming the owning tool.  Drawn last so it sits above the
        # gradient and the icon.  The product logo is preferred: a letter is
        # unreadable at 72px and means nothing without the mapping memorised.
        # The letter remains the fallback for a machine where the SVGs cannot
        # be rasterised (no cairosvg, broken libcairo), because a key that
        # renders without its mark beats a key that fails to render.
        # The logo tracks the key's brightness so a breathing key breathes as a
        # whole; a logo held at full white would detach from the face and read
        # as a separate light.  Colour is baked into the raster, so the dim
        # factor is part of the cache key rather than applied afterwards.
        source = face.get("source", "")
        px = badge_px(source)
        tint = "#%02x%02x%02x" % dim((255, 255, 255), max(0.35, mul))
        logo = logos.load(source, size=px, colour=tint) if source else None
        if logo is not None:
            pad = 3
            if effect == "shimmer":
                logo = animate_logo_mark(logo, source, phase)
            # paste-with-mask, not alpha_composite: the key image is RGB (the
            # deck wants no alpha channel) and alpha_composite demands RGBA on
            # both sides.  The logo's own alpha is the mask.
            img.paste(logo, (w - px - pad, pad), logo)
        elif badge:
            pad = 3
            bx, by = w - pad, pad
            try:
                box = draw.textbbox((0, 0), badge, font=self.font_small)
                bw, bh = box[2] - box[0], box[3] - box[1]
            except AttributeError:  # very old Pillow
                bw, bh = 10, 10
            draw.rectangle(
                [bx - bw - 5, by, bx, by + bh + 5], fill=(0, 0, 0), outline=None,
            )
            draw.text((bx - 3, by + 2), badge, font=self.font_small,
                      anchor="ra", fill="white")
        return img

    def push_all(self, phase: float = 1.0):
        if not self.deck or self._display_suspended:
            return
        self.push_indices(range(min(self.deck.key_count(), len(self.faces))), phase)

    def push_indices(self, indices, phase: float = 1.0):
        """Push only ``indices`` while preserving one-owner device locking.

        A full state transition still calls :meth:`push_all` exactly once, so
        a key that just became solid/off is never stranded on its old frame.
        Animation ticks use this narrower seam to avoid rewriting fourteen
        static keys because one agent is working.
        """
        if not self.deck or self._display_suspended:
            return
        from StreamDeck.ImageHelpers import PILHelper
        n = min(self.deck.key_count(), len(self.faces))
        now = time.monotonic()
        with self.deck:
            for i in sorted({i for i in indices if 0 <= i < n}):
                img = self.render_face(
                    self.faces[i], phase,
                    pressed=self._pressed_until.get(i, 0.0) > now,
                )
                native = PILHelper.to_native_key_format(self.deck, img)
                self.deck.set_key_image(i, native)

    # ---- press callback ------------------------------------------------
    def _on_key(self, deck, key, state):
        if self._display_suspended or self._ws is None or self._loop is None:
            return
        kind = "press" if state else "release"
        self._pressed_until[key] = time.monotonic() + (0.16 if state else 0.08)
        asyncio.run_coroutine_threadsafe(
            self._ws.send(json.dumps({"type": kind, "index": key})), self._loop)

    # ---- main loop -----------------------------------------------------
    async def _wait_for_device(self):
        """Stay alive until a visual deck is attached and openable.

        Launch-at-login commonly beats USB enumeration. Exiting here made
        launchd restart the entire stack—including Discord and agent
        connectors—every ten seconds until the deck was plugged in.
        """
        while True:
            try:
                deck = self.open_device()
                deck.set_key_callback(self._on_key)
                if self.health is not None:
                    self.health.ready(transport="hid", peer=self.ws_url, device="ready")
                return deck
            except Exception as exc:
                # A partially opened backend must not keep the exclusive HID
                # handle while we retry. Backends vary, so cleanup is bounded
                # and best-effort; the next enumeration remains authoritative.
                if self.deck is not None:
                    with suppress(Exception):
                        self.deck.close()
                    self.deck = None
                print(f"[hw] Stream Deck unavailable ({exc}); retrying in "
                      f"{DEVICE_RETRY_SECONDS:g}s")
                if self.health is not None:
                    self.health.degraded(
                        exc, transport="hid", peer=self.ws_url,
                        device="waiting", retry_in_seconds=DEVICE_RETRY_SECONDS,
                    )
                await asyncio.sleep(DEVICE_RETRY_SECONDS)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        while True:
            await self._wait_for_device()
            try:
                await self._serve_connections()
                return
            except Exception as exc:
                # HID backends report an unplug on the next image write. Keep
                # this renderer process (and therefore the supervisor's entire
                # connector stack) alive, release any partial handle, and go
                # back to enumeration until the same or another deck appears.
                self._ws = None
                if self.deck is not None:
                    with suppress(Exception):
                        self.deck.close()
                    self.deck = None
                print(f"[hw] Stream Deck connection lost ({exc}); waiting for device")
                await asyncio.sleep(DEVICE_RETRY_SECONDS)

    async def _serve_connections(self):
        async for ws in self._reconnect():
            self._ws = ws
            animator = None
            try:
                await ws.send(json.dumps({"type": "hello", "role": "renderer", "name": "hw-streamdeck"}))
                animator = asyncio.create_task(self._animate())
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "welcome":
                        self.grid = msg.get("grid", self.grid)
                        if self.health is not None:
                            self.health.ready(
                                transport="websocket+hid", peer=self.ws_url,
                                device="ready",
                            )
                    elif msg.get("type") == "state":
                        self.faces = [dict(OFF_FACE, **f) for f in msg["faces"]]
                        self.push_all(time.time())
            except websockets.ConnectionClosed:
                if self.health is not None:
                    self.health.degraded(
                        "deckd websocket closed", transport="websocket+hid",
                        peer=self.ws_url, device="ready",
                    )
            finally:
                # An error close (for example deckd being killed) raises out of
                # the receive loop before its old tail-position cancel could
                # run.  Reconnecting would then start a second animator while
                # the first kept writing the same device, multiplying both HID
                # traffic and CPU after every outage.
                self._ws = None
                if animator is not None:
                    animator.cancel()
                    with suppress(asyncio.CancelledError):
                        await animator

    async def _reconnect(self):
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    print(f"[hw] connected to deckd at {self.ws_url}")
                    yield ws
            except Exception as e:
                print(f"[hw] deckd unreachable ({e}); retrying in 1.5s")
                if self.health is not None:
                    self.health.degraded(
                        e, transport="websocket+hid", peer=self.ws_url,
                        device="ready", retry_in_seconds=1.5,
                    )
                await asyncio.sleep(1.5)

    async def _animate(self):
        """Redraw animated and freshly pressed keys at a device-safe ~12fps."""
        tick = 0
        while True:
            await asyncio.sleep(1 / ANIMATION_FPS)
            health = getattr(self, "health", None)
            if health is not None:
                health.heartbeat(
                    5.0, transport="websocket+hid", peer=self.ws_url,
                    device="ready",
                )
            tick += 1
            now = time.monotonic()
            if now >= getattr(self, "_next_power_check", float("inf")):
                self._next_power_check = now + POWER_POLL_SECONDS
                await asyncio.to_thread(self.refresh_display_power)
            if getattr(self, "_display_suspended", False):
                # Keep health heartbeats alive while the Mac is locked, but do
                # no HID animation writes until unlock restores one full frame.
                continue
            indices = {
                i for i, face in enumerate(self.faces)
                if face.get("effect") == "shimmer"
                or face.get("icon") in ("working", "agent")
                or (
                    face.get("effect") in ("breathe", "blink")
                    and tick % SLOW_EFFECT_DIVISOR == 0
                )
            }
            indices.update(
                i for i, until in self._pressed_until.items() if until > now
            )
            expired = {
                i: until for i, until in self._pressed_until.items()
                if until <= now
            }
            # One last undimmed frame ends the press flash. Without this write,
            # a solid key can remain visibly pressed forever because it has no
            # other reason to enter the animation loop.
            indices.update(expired)
            if indices:
                self.push_indices(indices, time.time() * 3)
            for i, until in expired.items():
                # The HID callback runs on a device thread. Do not erase a new
                # press that raced with this final-frame write.
                if self._pressed_until.get(i) == until:
                    self._pressed_until.pop(i, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="ws://127.0.0.1:8777")
    ap.add_argument("--brightness", type=int, default=45)
    args = ap.parse_args()
    r = HWRenderer(
        args.ws, args.brightness,
        health=HealthReporter("renderer_hw", stale_after=20.0),
    )
    def stop_cleanly(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_cleanly)
    signal.signal(signal.SIGHUP, stop_cleanly)
    try:
        asyncio.run(r.run())
    except KeyboardInterrupt:
        pass
    finally:
        r.blank_and_close()


if __name__ == "__main__":
    main()
