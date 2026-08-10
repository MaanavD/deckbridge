"""Source logos for deck keys.

Every agent key carries a corner mark naming the tool that owns it. That mark
used to be a letter (H/C/X/M/S), which is unreadable at a glance on a 72px key
and meaningless to anyone who has not memorised the mapping. This module
replaces the letters with the real product logos.

Two consumers, one source of truth in ``logos/``:

* ``renderer_hw.py`` needs a Pillow image to composite onto the physical key.
* ``emulator.html`` loads ``logos/<source>.svg`` over the HTTP server that
  already serves it from this directory, so filenames are deliberately the
  source ids verbatim and no mapping is duplicated in JavaScript.

The SVGs are Simple Icons (CC0) plus project-drawn glyphs for integrations
without a published icon. All are single-path 24x24 monochrome, so
recolouring is a fill swap rather than an image operation.

cairosvg is optional on purpose. The Mac may not have a working libcairo, and
a missing logo must not take the whole deck down: ``badge_letter`` still
exists, and callers fall back to it when ``load`` returns None.
"""

from __future__ import annotations

import functools
import os
import re
from typing import Any

LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos")


def _expose_homebrew_cairo() -> None:
    """Make Homebrew's cairo visible from a macOS GUI/launch-agent process.

    Finder and launchd sessions do not inherit a shell's library search path.
    CairoSVG then imports successfully but cannot locate ``libcairo``, so logo
    rendering silently degrades to letters.  Adding an installed Homebrew lib
    directory to the fallback path before CairoSVG's lazy import fixes that
    launch-only failure without changing non-macOS environments.
    """
    candidates = ("/opt/homebrew/lib", "/usr/local/lib")
    installed = [path for path in candidates
                 if os.path.exists(os.path.join(path, "libcairo.2.dylib"))]
    if not installed:
        return
    current = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    entries = [part for part in current.split(os.pathsep) if part]
    missing = [path for path in installed if path not in entries]
    if missing:
        os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(missing + entries)

#: Source id -> logo file. Keys match connector_agents.SOURCE_BADGE exactly so
#: the two cannot drift apart silently.
#:
#: hermes-* both carry the Nous mark, not the Discord one. Discord is the
#: transport a Hermes agent happens to speak through; the agent is Hermes.
#: Marking it with Discord's logo named the pipe instead of the thing, and made
#: an ssh-hosted Hermes agent and a Discord-hosted one look like different
#: products. `discord` remains available for a key that really is the app.
_LOCAL_HERMES_MARK = os.path.join(LOGO_DIR, "nous.png")
HERMES_LOGO = "nous.png" if os.path.exists(_LOCAL_HERMES_MARK) else "hermes.svg"

SOURCE_LOGO = {
    "hermes-discord": HERMES_LOGO,
    "hermes-ssh": HERMES_LOGO,
    "hermes-health": HERMES_LOGO,
    "hermes": HERMES_LOGO,
    "nous": HERMES_LOGO,
    "claude-code": "claude-code.svg",
    "codex-cli": "codex-cli.svg",
    "codex": "codex-cli.svg",
    "cursor-agent": "cursor-agent.svg",
    "herdr": "herdr.svg",
    "cmux": "cmux.svg",
    "mic": "mic.svg",
    "slack": "slack.svg",
    "gmail": "gmail.svg",
    "google-chrome": "google-chrome.svg",
    "discord": "discord.svg",
    "notion-calendar": "notion-calendar.svg",
}

# Fixed app shortcuts use the installed applications' own icon resources on
# hardware. The browser emulator falls back to the same one-letter badges when
# these machine-local files are unavailable.
APP_ICON = {}

#: Sources that deliberately share one mark, so a filename cannot be named
#: after all of them. Exported because the tests enforce filename == source id
#: for everything else, and that rule needs an explicit exception rather than
#: a silently weakened assertion.
SHARED_MARK_SOURCES = (
    "hermes-discord", "hermes-ssh", "hermes-health", "hermes", "nous"
)

#: Retained as the fallback when a logo cannot be rasterised, and for the
#: text-mode dump in connector_agents --print.
BADGE_LETTER = {
    "hermes-discord": "H",
    "hermes-ssh": "S",
    "hermes-health": "!",
    "discord": "D",
    "claude-code": "C",
    "codex-cli": "X",
    "codex": "X",
    "cursor-agent": "R",
    "herdr": "E",
    "cmux": "M",
    "slack": "L",
    "gmail": "G",
    "google-chrome": "P",
    "notion-calendar": "N",
}


def badge_letter(source: str) -> str:
    return BADGE_LETTER.get(source, "")


def logo_path(source: str) -> str | None:
    app_icon = APP_ICON.get(source)
    if app_icon and os.path.exists(app_icon):
        return app_icon
    name = SOURCE_LOGO.get(source)
    if not name:
        return None
    path = os.path.join(LOGO_DIR, name)
    return path if os.path.exists(path) else None


def _recolour(svg: str, colour: str) -> str:
    """Force every path in a Simple Icons SVG to ``colour``.

    Simple Icons ship with no fill attribute at all, inheriting currentColor,
    which resolves to black once cairosvg renders standalone. A black logo on
    the dark blue and red key faces is invisible, so the fill is injected
    rather than left to inheritance.
    """
    if "<svg" not in svg:
        return svg
    svg = re.sub(r'\sfill="[^"]*"', "", svg)
    return svg.replace("<svg", f'<svg fill="{colour}"', 1)


@functools.lru_cache(maxsize=64)
def _svg_text(source: str) -> str | None:
    path = logo_path(source)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


@functools.lru_cache(maxsize=128)
def load(source: str, size: int = 18, colour: str = "#ffffff") -> Any | None:
    """Rasterise a logo to an RGBA Pillow image, or None if unavailable.

    Returns None rather than raising for any failure: a missing cairosvg, a
    broken libcairo, an absent file. The caller draws the letter instead. A
    logo is decoration; refusing to render the key would be worse than the
    thing it fixes.

    PNG sources take a different path from SVG on purpose. The Nous mark is
    line art -- a face, not a single-path glyph -- so the recolour-to-one-fill
    treatment that suits Simple Icons destroys it, flattening the features into
    a blob. It keeps its own pixels and only its alpha is used.
    """
    app_icon = APP_ICON.get(source)
    if app_icon:
        return _load_image_path(app_icon, size)
    name = SOURCE_LOGO.get(source)
    if name and name.lower().endswith(".png"):
        return _load_png(source, size)
    svg = _svg_text(source)
    if not svg:
        return None
    _expose_homebrew_cairo()
    try:
        import cairosvg  # noqa: PLC0415  (optional dependency, imported lazily)
        from PIL import Image  # noqa: PLC0415
    except Exception:
        return None
    import io

    try:
        png = cairosvg.svg2png(
            bytestring=_recolour(svg, colour).encode("utf-8"),
            output_width=size,
            output_height=size,
        )
        return Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception:
        return None


def _load_image_path(path: str, size: int) -> Any | None:
    try:
        from PIL import Image
        with Image.open(path) as source:
            img = source.convert("RGBA")
        lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        img.thumbnail((size, size), lanczos)
        frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        frame.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
        return frame
    except Exception:
        return None


def _load_png(source: str, size: int) -> Any | None:
    """Load a raster logo, needing Pillow only -- no cairosvg, no libcairo.

    This matters beyond the Nous mark: a Mac without a working libcairo shows
    letters for every SVG source, and a PNG mark still renders there.
    """
    path = logo_path(source)
    if not path:
        return None
    try:
        from PIL import Image  # noqa: PLC0415
    except Exception:
        return None
    try:
        img = Image.open(path).convert("RGBA")
        # Pillow 10 moved the constant to Image.Resampling; the old alias still
        # works but is gone from the type stubs, and older Pillows lack the new
        # home. Ask for whichever this install has.
        lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        img = img.resize((size, size), lanczos)
        if source in SHARED_MARK_SOURCES and size > 2:
            # The supplied Nous illustration has a one-pixel white matte at
            # badge scale.  It is visible against amber/red keys as a pale
            # square even though the interior artwork has useful alpha detail.
            # Clear only the output perimeter: doing this after resampling
            # removes the matte without thresholding (and therefore damaging)
            # the face's soft interior linework.
            clean = Image.new("RGBA", img.size, (0, 0, 0, 0))
            clean.paste(img.crop((1, 1, size - 1, size - 1)), (1, 1))
            img = clean
        return img
    except Exception:
        return None


def available() -> bool:
    """True when logos can actually be rasterised on this machine."""
    return load("claude-code", 8) is not None


# --- status icons ---------------------------------------------------------
#
# The status glyph used to be text: "AI", "OK", "!" in the hardware renderer
# and emoji in the emulator. Both were wrong for the same reason. Text at 26px
# is a shape you decode rather than recognise, and emoji are drawn by whatever
# the system font decides, so the same board looked like a different product in
# each renderer and neither matched the product logos beside them.
#
# These are drawn at the same 24x24 single-path weight as the Simple Icons
# logos, so the whole key reads as one set.

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

#: Status name -> icon file. ``check-outline`` is the SEEN state: same
#: silhouette as ``check`` so it is recognisably the same thing, hollow so a
#: glance can tell an acknowledged key from a fresh one without reading.
ICON_FILE = {
    "alert": "alert.svg",
    "check": "check.svg",
    "check-outline": "check-outline.svg",
    "working": "working.svg",
    "agent": "working.svg",
    "idle": "idle.svg",
    "page": "page.svg",
}


def icon_path(name: str) -> str | None:
    filename = ICON_FILE.get(name)
    if not filename:
        return None
    path = os.path.join(ICON_DIR, filename)
    return path if os.path.exists(path) else None


@functools.lru_cache(maxsize=64)
def _icon_svg(name: str) -> str | None:
    path = icon_path(name)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _recolour_stroke(svg: str, colour: str) -> str:
    """Recolour a STROKE-drawn icon.

    The status icons are strokes, not filled silhouettes, because a filled
    glyph at 22px has to knock its detail out of a solid shape -- a tick
    reversed out of a disc read as a blob with a hole, not as a tick. Strokes
    keep the detail as the drawn thing itself.

    That makes ``_recolour`` actively wrong for them: it strips ``fill`` and
    forces a fill on the root, which would flood every outline solid. Here the
    stroke is swapped and ``fill="none"`` is preserved.
    """
    if "<svg" not in svg:
        return svg
    svg = re.sub(r'\sstroke="[^"]*"', "", svg, count=1)
    return svg.replace("<svg", f'<svg stroke="{colour}"', 1)


@functools.lru_cache(maxsize=128)
def load_icon(name: str, size: int = 22, colour: str = "#ffffff") -> Any | None:
    """Rasterise a status icon, or None when it cannot be drawn.

    Same contract as ``load``: never raises, and a caller that gets None draws
    its text fallback instead. A key that renders without its icon beats a key
    that fails to render.
    """
    svg = _icon_svg(name)
    if not svg:
        return None
    _expose_homebrew_cairo()
    try:
        import cairosvg  # noqa: PLC0415  (optional dependency, imported lazily)
        from PIL import Image  # noqa: PLC0415
    except Exception:
        return None
    import io

    try:
        png = cairosvg.svg2png(
            bytestring=_recolour_stroke(svg, colour).encode("utf-8"),
            output_width=size,
            output_height=size,
        )
        return Image.open(io.BytesIO(png)).convert("RGBA")
    except Exception:
        return None
