#!/usr/bin/env python3
"""test_logos.py - the corner mark on every key.

The mark used to be a letter (H/C/X/M/S), which said nothing to anyone who had
not memorised the mapping. These tests cover the replacement: a real product
logo, with the letter kept only as the fallback for a machine that cannot
rasterise SVG.

Two failure modes matter more than the happy path:

* A missing or unrasterisable logo must degrade to the letter, never raise.
  A decorative mark must not be able to take a key down.
* The three places that name a source -- logos/ filenames, SOURCE_LOGO, and
  connector_agents.SOURCE_BADGE -- must agree, because a silent mismatch shows
  up as one key mysteriously wearing a letter while its neighbours wear logos.
"""
import asyncio
import json
import math
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logos  # noqa: E402
import connector_agents  # noqa: E402


class TestLogoFiles(unittest.TestCase):
    def test_every_agent_source_has_a_logo_file(self):
        """Every source the connector can badge must have a logo on disk."""
        for source in connector_agents.SOURCE_BADGE:
            with self.subTest(source=source):
                self.assertIsNotNone(
                    logos.logo_path(source),
                    f"no logo file for {source}; the key would fall back to a letter",
                )

    def test_mic_has_a_logo(self):
        self.assertIsNotNone(logos.logo_path("mic"))

    def test_every_source_has_the_mapped_logo_file(self):
        """The connector sends this mapping to the emulator; every file exists."""
        for source in list(connector_agents.SOURCE_BADGE) + ["mic"]:
            with self.subTest(source=source):
                path = logos.logo_path(source)
                self.assertIsNotNone(path)
                self.assertEqual(os.path.basename(path), logos.SOURCE_LOGO[source])

    def test_shared_mark_sources_resolve_to_one_file(self):
        """Every source wearing the shared mark must resolve to the same file."""
        paths = {logos.logo_path(s) for s in logos.SHARED_MARK_SOURCES}
        self.assertEqual(len(paths), 1, f"shared-mark sources disagree: {paths}")
        self.assertIsNotNone(paths.pop())

    def test_letters_match_the_connector(self):
        """logos.BADGE_LETTER is the fallback for connector_agents.SOURCE_BADGE."""
        for source, letter in connector_agents.SOURCE_BADGE.items():
            self.assertEqual(logos.badge_letter(source), letter, source)

    def test_svgs_carry_an_explicit_white_fill(self):
        """Simple Icons ship fill-less and inherit currentColor.

        In an <img> that resolves to black, which is invisible against every
        key colour we use. The fill has to be in the file.

        Raster marks are skipped: a PNG carries its own pixels and has no fill
        to inherit.
        """
        for source in list(connector_agents.SOURCE_BADGE) + ["mic"]:
            path = logos.logo_path(source)
            if not path.lower().endswith(".svg"):
                continue
            with self.subTest(source=source):
                with open(path) as handle:
                    head = handle.read().split(">")[0]
                self.assertIn("fill=", head)

    def test_unknown_source_has_no_path_and_no_letter(self):
        self.assertIsNone(logos.logo_path("not-a-real-source"))
        self.assertEqual(logos.badge_letter("not-a-real-source"), "")
        self.assertIsNone(logos.load("not-a-real-source"))

    def test_empty_source_is_safe(self):
        self.assertIsNone(logos.logo_path(""))
        self.assertIsNone(logos.load(""))


class TestNousMark(unittest.TestCase):
    """The Hermes keys wear the Nous mark, not Discord's.

    Reported as "the corner thing should be an icon not a letter", which had
    two independent causes: cairosvg was absent (so every SVG fell back to a
    letter) and the Hermes sources pointed at Discord's logo anyway.
    """

    def test_hermes_sources_use_the_nous_mark(self):
        for source in ("hermes-discord", "hermes-ssh"):
            with self.subTest(source=source):
                path = logos.logo_path(source)
                self.assertIsNotNone(path, f"{source} has no mark")
                self.assertEqual(os.path.basename(path), logos.HERMES_LOGO)

    def test_the_discord_logo_is_still_reachable(self):
        """Renaming the mark must not delete the Discord one outright."""
        self.assertIsNotNone(logos.logo_path("discord"))

    def test_the_nous_mark_needs_no_cairosvg(self):
        """A raster mark must render on a Mac with no working libcairo.

        This is the whole reason the mark is a PNG: an SVG-only pipeline shows
        letters on such a machine, which is the bug being fixed.
        """
        if logos.HERMES_LOGO != "nous.png":
            self.skipTest("the optional local raster override is not installed")
        img = logos.load("hermes-discord", size=26)
        self.assertIsNotNone(img, "the Nous mark failed to load")
        self.assertEqual(img.size, (26, 26))

    def test_the_nous_mark_has_real_transparency(self):
        """Alpha is what lets the key colour show through.

        A fully opaque square would paste a white block over the key corner.
        """
        if logos.HERMES_LOGO != "nous.png":
            self.skipTest("the optional local raster override is not installed")
        img = logos.load("hermes-discord", size=26)
        alpha = img.getchannel("A")
        self.assertEqual(alpha.getextrema()[0], 0, "no transparent pixels")
        self.assertGreater(alpha.getextrema()[1], 0, "no opaque pixels")

    def test_the_nous_mark_ignores_the_tint(self):
        """Line art must keep its own pixels.

        The SVG path forces every path to one fill colour, which flattens a
        drawn face into a silhouette. The raster path must ignore the requested
        colour entirely, so two different tints produce identical pixels.
        """
        if logos.HERMES_LOGO != "nous.png":
            self.skipTest("the optional local raster override is not installed")
        red = logos.load("hermes-discord", size=26, colour="#ff0000")
        blue = logos.load("hermes-discord", size=26, colour="#0000ff")
        self.assertEqual(list(red.getdata()), list(blue.getdata()),
                         "the tint reached the raster mark")

    def test_the_nous_mark_keeps_interior_detail(self):
        """The face has to survive being shrunk to a badge.

        A silhouette is all-or-nothing alpha; a face keeps midtones. If the
        partial-alpha pixels vanish, the mark has become a blob and the whole
        point of using the real artwork is lost.
        """
        if logos.HERMES_LOGO != "nous.png":
            self.skipTest("the optional local raster override is not installed")
        img = logos.load("hermes-discord", size=26)
        alpha = list(img.getchannel("A").getdata())
        midtones = [a for a in alpha if 30 < a < 225]
        self.assertGreater(len(midtones), 20,
                           "the mark flattened into a silhouette")

    def test_the_nous_mark_has_no_visible_raster_perimeter(self):
        """The source raster must not paste a one-pixel white box on a key.

        The illustration itself can reach close to an edge, but the outermost
        output pixels are matte, not artwork.  They must be transparent after
        loading so both the physical renderer and its compositing tests see a
        clean badge rather than a pale square around the girl.
        """
        if logos.HERMES_LOGO != "nous.png":
            self.skipTest("the optional local raster override is not installed")
        img = logos.load("hermes-discord", size=26)
        alpha = img.getchannel("A")
        edge = (
            list(alpha.crop((0, 0, 26, 1)).getdata())
            + list(alpha.crop((0, 25, 26, 26)).getdata())
            + list(alpha.crop((0, 1, 1, 25)).getdata())
            + list(alpha.crop((25, 1, 26, 25)).getdata())
        )
        self.assertTrue(all(a == 0 for a in edge),
                        "the Nous raster still has a visible one-pixel halo")


class TestRendererBadgeSize(unittest.TestCase):
    def test_the_nous_mark_gets_more_pixels_than_a_glyph(self):
        """At 18px the drawn face collapses into a blob; it needs a bigger box."""
        try:
            import renderer_hw
        except Exception:  # pragma: no cover - Pillow missing
            self.skipTest("renderer_hw unavailable")
        self.assertGreater(renderer_hw.badge_px("hermes-discord"),
                           renderer_hw.BADGE_PX)

    def test_single_path_logos_keep_the_default_size(self):
        try:
            import renderer_hw
        except Exception:  # pragma: no cover
            self.skipTest("renderer_hw unavailable")
        for source in ("claude-code", "codex-cli", "cursor-agent", "cmux"):
            self.assertEqual(renderer_hw.badge_px(source), renderer_hw.BADGE_PX)


class TestHardwareDisplayPower(unittest.TestCase):
    class Deck:
        def __init__(self):
            self.events = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def reset(self):
            self.events.append("reset")

        def set_brightness(self, value):
            self.events.append(("brightness", value))

        def close(self):
            self.events.append("close")

    def test_ioreg_root_lock_state_is_detected(self):
        import renderer_hw
        completed = mock.Mock(stdout=b'  "IOConsoleLocked" = Yes\n')
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(
            subprocess, "run", return_value=completed
        ):
            self.assertTrue(renderer_hw.macos_session_locked())

    def test_lock_blanks_once_and_unlock_repaints_latest_state(self):
        import renderer_hw
        states = iter((True, True, False))
        r = renderer_hw.HWRenderer(
            "ws://unused", 45, session_locked=lambda: next(states))
        deck = self.Deck()
        r.deck = deck
        repaints = []
        r.push_all = lambda phase: repaints.append(phase)

        self.assertTrue(r.refresh_display_power())
        self.assertEqual(deck.events, ["reset", ("brightness", 0)])
        self.assertFalse(r.refresh_display_power(), "locked polling rewrote HID")
        self.assertEqual(deck.events, ["reset", ("brightness", 0)])

        self.assertTrue(r.refresh_display_power())
        self.assertEqual(deck.events[-1], ("brightness", 45))
        self.assertEqual(len(repaints), 1)

    def test_shutdown_blanks_before_closing(self):
        import renderer_hw
        r = renderer_hw.HWRenderer("ws://unused", 45)
        deck = self.Deck()
        r.deck = deck
        r.blank_and_close()
        self.assertEqual(
            deck.events, ["reset", ("brightness", 0), "close"])


class TestDependencyManifest(unittest.TestCase):
    def test_requirements_names_cairosvg(self):
        """Omitting it degraded silently to letters rather than failing loudly.

        The renderer treats a logo as optional decoration, so a missing
        cairosvg produces no error anywhere -- only lettered keys. The manifest
        is the only place that can prevent it.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "requirements.txt")) as handle:
            text = handle.read().lower()
        self.assertIn("cairosvg", text)
        self.assertIn("pillow", text)

    def test_homebrew_cairo_is_discovered_by_a_gui_launch_environment(self):
        """A launch agent lacks Homebrew's library path unless we expose it.

        That environment is exactly where the hardware renderer runs.  On a
        Homebrew Mac with cairo installed, SVG logos must rasterise rather than
        silently falling back to letters.
        """
        cairo_lib = "/opt/homebrew/lib/libcairo.2.dylib"
        if sys.platform != "darwin" or not os.path.exists(cairo_lib):
            self.skipTest("not a Homebrew cairo Mac")
        old = os.environ.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
        logos.load.cache_clear()
        try:
            self.assertIsNotNone(logos.load("claude-code", size=18),
                                 "Homebrew cairo was invisible to the renderer")
        finally:
            if old is not None:
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = old
            else:
                os.environ.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
            logos.load.cache_clear()


class TestRecolour(unittest.TestCase):
    def test_fill_is_replaced_not_appended(self):
        """A second fill attribute would leave the original colour winning."""
        out = logos._recolour('<svg fill="#000"><path fill="#000" d="M0 0"/></svg>', "#ff0000")
        self.assertEqual(out.count("fill="), 1)
        self.assertIn('fill="#ff0000"', out)

    def test_non_svg_passes_through(self):
        self.assertEqual(logos._recolour("garbage", "#fff"), "garbage")


class TestRasterise(unittest.TestCase):
    def setUp(self):
        if not logos.available():
            self.skipTest("cairosvg unavailable on this machine")

    def test_load_returns_a_square_rgba_image(self):
        img = logos.load("claude-code", size=18)
        self.assertEqual(img.size, (18, 18))
        self.assertEqual(img.mode, "RGBA")

    def test_colour_reaches_the_pixels(self):
        """Recolouring is the whole reason a dim logo can breathe with its key."""
        img = logos.load("claude-code", size=24, colour="#ff0000")
        px = img.load()
        reds = [px[x, y] for y in range(img.height) for x in range(img.width)
                if px[x, y][3] > 200]
        self.assertTrue(reds, "logo rendered fully transparent")
        self.assertTrue(all(p[0] > 200 and p[1] < 60 for p in reds), "fill ignored")

    def test_every_source_actually_rasterises(self):
        """A malformed hand-drawn glyph would return None and silently
        degrade to a letter, so each file is rendered rather than assumed."""
        for source in list(connector_agents.SOURCE_BADGE) + ["mic"]:
            with self.subTest(source=source):
                img = logos.load(source, size=18)
                self.assertIsNotNone(img, f"{source} failed to rasterise")
                px = img.load()
                self.assertTrue(
                    any(px[x, y][3] > 0
                        for y in range(img.height) for x in range(img.width)),
                    f"{source} rendered blank",
                )


class TestGracefulDegradation(unittest.TestCase):
    def test_load_returns_none_without_cairosvg(self):
        """No cairosvg (or a broken libcairo) must not raise into the renderer."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "cairosvg":
                raise ImportError("simulated missing cairosvg")
            return real_import(name, *args, **kwargs)

        logos.load.cache_clear()
        builtins.__import__ = fake_import
        try:
            self.assertIsNone(logos.load("claude-code", size=18))
        finally:
            builtins.__import__ = real_import
            logos.load.cache_clear()

    def test_rasteriser_failure_returns_none(self):
        """A cairosvg that raises mid-render is caught, not propagated."""
        # The import must come AFTER the availability check, not before: on a
        # host without cairosvg this errored instead of skipping, which reads
        # as a broken test suite rather than an optional dependency. cairosvg
        # is optional on purpose -- the Mac may have no working libcairo.
        if not logos.available():
            self.skipTest("cairosvg unavailable")
        import cairosvg  # noqa: PLC0415

        real = cairosvg.svg2png

        def boom(*args, **kwargs):
            raise RuntimeError("simulated cairo failure")

        logos.load.cache_clear()
        cairosvg.svg2png = boom
        try:
            self.assertIsNone(logos.load("claude-code", size=18))
        finally:
            cairosvg.svg2png = real
            logos.load.cache_clear()


class TestFacesCarrySource(unittest.TestCase):
    """The renderers pick a logo from face['source'], so it has to be there."""

    def test_agent_face_carries_source(self):
        face = connector_agents.face_for(
            {"name": "sample api", "status": "working", "source": "claude-code"}
        )
        self.assertEqual(face["source"], "claude-code")
        self.assertEqual(face["badge"], "C")

    def test_hardware_keeps_compact_task_titles(self):
        from renderer_hw import HARDWARE_LABEL_CHARS
        self.assertEqual(HARDWARE_LABEL_CHARS, 11)

    def test_the_status_glyph_clears_the_corner_mark(self):
        """Two marks that both carry meaning may not be drawn on top of each other.

        The status glyph was centred in the whole key. That was survivable
        against an 18px letter badge, but the 26px Nous face ran straight
        through "OK" and left a smear that read as neither. The glyph is now
        centred in the width the mark leaves free, so the right-hand column of
        the key belongs to the logo alone.
        """
        from renderer_hw import HWRenderer, badge_px
        r = HWRenderer.__new__(HWRenderer)
        r.deck = None
        r.key_size = (72, 72)
        r._load_fonts()
        r.faces = {}
        img = r.render_face({
            "label": "chan", "sublabel": "done", "badge": "H",
            "source": "hermes-discord", "color": "#2f6fed",
            "icon": "check", "effect": "solid",
        }, 1.0)
        # The band the logo occupies, below the top padding: any glyph pixel
        # here is an overlap.
        px = badge_px("hermes-discord")
        plain = r.render_face({
            "label": "chan", "sublabel": "done", "badge": "H",
            "source": "hermes-discord", "color": "#2f6fed",
            "icon": None, "effect": "solid",
        }, 1.0)
        # Compare glyph-bearing and glyph-less renders inside the logo band.
        # Identical pixels there prove the glyph added nothing under the mark.
        band = (72 - px - 3, 3, 72 - 3, 3 + px)
        self.assertEqual(
            list(img.crop(band).getdata()), list(plain.crop(band).getdata()),
            "the status glyph bled into the corner mark")

    def test_status_icons_stay_outlines(self):
        """A stroke icon must not be flooded solid by the logo recolour path.

        The product logos are filled single-path silhouettes, so `_recolour`
        strips fill and forces one on the root. The status icons are the
        opposite: strokes over `fill="none"`, because a tick knocked out of a
        solid disc read as a blob with a hole rather than as a tick. Running
        the logo recolour over them would flood every outline solid, which
        renders as a filled shape and looks like a different icon entirely.

        A ring is the sharpest probe: filled, its centre is opaque; stroked,
        its centre is empty.
        """
        # No cairosvg means no rasteriser, which is a MISSING DEPENDENCY, not
        # a failing assertion. Crashing here would mask the real result.
        if not logos.available():
            self.skipTest("cairosvg unavailable on this machine")
        img = logos.load_icon("idle", size=48)
        assert img is not None
        centre = img.getpixel((24, 24))
        self.assertEqual(centre[3], 0,
                         "the idle ring was filled instead of stroked")
        edge = img.getchannel("A").getextrema()[1]
        self.assertGreater(edge, 200, "the ring has no drawn stroke at all")

    def test_status_icons_take_the_requested_colour(self):
        """Recolouring has to reach the stroke, not just the (absent) fill."""
        # No cairosvg means no rasteriser, which is a MISSING DEPENDENCY, not
        # a failing assertion. Crashing here would mask the real result.
        if not logos.available():
            self.skipTest("cairosvg unavailable on this machine")
        img = logos.load_icon("check", size=48, colour="#ff0000")
        assert img is not None
        reds = [px for px in img.convert("RGBA").getdata()
                if px[3] > 200 and px[0] > 200 and px[1] < 60]
        self.assertGreater(len(reds), 20, "the tint never reached the stroke")

    def test_the_seen_icon_differs_from_the_unseen_one(self):
        """The acknowledged state has to be visible, or it says nothing."""
        # No cairosvg means no rasteriser, which is a MISSING DEPENDENCY, not
        # a failing assertion. Crashing here would mask the real result.
        if not logos.available():
            self.skipTest("cairosvg unavailable on this machine")
        a = logos.load_icon("check", size=32)
        b = logos.load_icon("check-outline", size=32)
        assert a is not None and b is not None
        fresh, seen = list(a.getdata()), list(b.getdata())
        self.assertNotEqual(fresh, seen, "seen and unseen render identically")

    def test_off_and_pager_faces_have_an_empty_source(self):
        """A missing key must be absent, not marked; an empty string means
        'draw nothing' in both renderers."""
        self.assertEqual(connector_agents.OFF_FACE["source"], "")
        self.assertEqual(connector_agents.page_face(0, 2, 3)["source"], "")

    def test_mic_faces_carry_the_mic_source(self):
        import connector_mic

        self.assertEqual(connector_mic.IDLE_FACE["source"], "mic")
        self.assertEqual(connector_mic.FIRED_FACE["source"], "mic")
        # No letter: there is nothing to disambiguate the mic key from.
        self.assertEqual(connector_mic.IDLE_FACE["badge"], "")

    def test_unknown_source_yields_an_empty_mark(self):
        face = connector_agents.face_for(
            {"name": "x", "status": "done", "source": "who-knows"}
        )
        self.assertEqual(face["badge"], "")
        self.assertIsNone(logos.load(face["source"]))


class TestHardwareRenderer(unittest.TestCase):
    def setUp(self):
        try:
            import renderer_hw  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"renderer_hw unimportable: {exc}")
        self.renderer_hw = renderer_hw

    def test_face_with_logo_renders(self):
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.key_size = (72, 72)
        r._load_fonts()
        img = r.render_face(
            {
                "label": "sample api",
                "sublabel": "working",
                "badge": "C",
                "source": "claude-code",
                "color": "#d9822b",
                "icon": "agent",
                "effect": "solid",
            }
        )
        self.assertEqual(img.size, (72, 72))

    def test_face_without_source_still_renders(self):
        """Old connectors emit no source key at all; that must not crash."""
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.key_size = (72, 72)
        r._load_fonts()
        img = r.render_face(
            {"label": "x", "sublabel": "", "badge": "H", "color": "#2e6fdb",
             "icon": "agent", "effect": "solid"}
        )
        self.assertEqual(img.size, (72, 72))

    def test_logo_only_notification_draws_ios_red_bubble(self):
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.key_size = (72, 72)
        r._load_fonts()
        img = r.render_face({
            "source": "discord", "color": "#2a2f3a", "effect": "solid",
            "layout": "logo-only", "notification_count": 4,
        })
        reds = sum(
            1 for red, green, blue in img.crop((45, 0, 72, 27)).getdata()
            if red > 220 and green < 130 and blue < 130
        )
        self.assertGreater(reds, 80)

    def test_mic_action_uses_large_central_glyph_and_caption(self):
        import connector_mic

        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.key_size = (72, 72)
        r._load_fonts()
        img = r.render_face(connector_mic.IDLE_FACE)
        background = img.getpixel((0, 0))
        central_ink = sum(
            1 for pixel in img.crop((12, 8, 60, 53)).getdata()
            if pixel != background
        )
        self.assertGreater(central_ink, 150)

    def test_shimmer_changes_between_animation_frames(self):
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.key_size = (72, 72)
        r._load_fonts()
        face = {
            "label": "build", "sublabel": "working", "badge": "X",
            "source": "codex-cli", "color": "#d9822b",
            "icon": "working", "effect": "shimmer",
        }
        first = r.render_face(face, 0.0)
        second = r.render_face(face, math.pi)
        self.assertNotEqual(list(first.getdata()), list(second.getdata()))

    def test_working_status_ring_spins_even_on_a_solid_face(self):
        """The open status ring itself moves; background shimmer is not enough."""
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.key_size = (72, 72)
        r._load_fonts()
        face = {
            "label": "", "sublabel": "", "badge": "", "source": "",
            "color": "#d9822b", "icon": "working", "effect": "solid",
        }
        first = r.render_face(face, 0.0)
        second = r.render_face(face, math.pi / 2)
        self.assertNotEqual(list(first.getdata()), list(second.getdata()),
                            "working ring stayed static between frames")

    def test_working_product_marks_have_source_specific_motion(self):
        """Claude, Codex, and Hermes marks each animate while their agent works."""
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.key_size = (72, 72)
        r._load_fonts()

        def logo_mask(source, phase):
            face = {
                "label": "", "sublabel": "", "badge": "", "source": source,
                "color": "#d9822b", "icon": None, "effect": "shimmer",
            }
            with_logo = r.render_face(face, phase)
            without = r.render_face(dict(face, source=""), phase)
            px = self.renderer_hw.badge_px(source)
            box = (72 - px - 3, 3, 69, 3 + px)
            return {
                i for i, (marked, plain) in enumerate(zip(
                    with_logo.crop(box).getdata(), without.crop(box).getdata()
                ))
                if marked != plain
            }

        for source in ("claude-code", "codex-cli", "hermes-discord"):
            with self.subTest(source=source):
                first = logo_mask(source, 0.0)
                second = logo_mask(source, math.pi / 2)
                self.assertTrue(first and second, f"{source} mark did not render")
                self.assertNotEqual(first, second,
                                    f"{source} mark stayed static while working")

    def test_floating_hermes_mark_does_not_reintroduce_the_halo(self):
        mark = logos.load("hermes-discord", size=26)
        floated = self.renderer_hw.animate_logo_mark(
            mark, "hermes-discord", math.pi / 2
        )
        alpha = floated.getchannel("A")
        edge = (
            list(alpha.crop((0, 0, 26, 1)).getdata())
            + list(alpha.crop((0, 25, 26, 26)).getdata())
            + list(alpha.crop((0, 1, 1, 25)).getdata())
            + list(alpha.crop((25, 1, 26, 25)).getdata())
        )
        self.assertTrue(all(a == 0 for a in edge),
                        "the floating frame moved raster matte back to an edge")

    def test_animation_tick_writes_only_animated_or_pressed_keys(self):
        """One working key costs ~12 device writes/sec, not 15 times that."""
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.faces = [
            {"effect": "shimmer" if i == 3 else "off", "icon": None}
            for i in range(15)
        ]
        now = time.monotonic()
        r._pressed_until = {5: now + 60}
        writes = []
        r.push_all = lambda phase: writes.append(("all", tuple(range(15))))
        r.push_indices = lambda indices, phase: writes.append(
            ("indices", tuple(sorted(indices)))
        )

        class StopTick(Exception):
            pass

        sleeps = 0

        async def one_tick(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise StopTick

        with mock.patch.object(self.renderer_hw.asyncio, "sleep", one_tick):
            with self.assertRaises(StopTick):
                asyncio.run(r._animate())

        self.assertEqual(writes, [("indices", (3, 5))])

    def test_slow_effects_do_not_pay_the_working_spinner_frame_rate(self):
        """Breathe/blink move slowly; full-rate HID writes only waste power.

        A common live scene has one working agent and the microphone's
        held face breathing.  The working ring must keep all twelve
        frames per second, while each slow effect needs only four.
        """
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.faces = [
            {"effect": "shimmer", "icon": "working"},
            {"effect": "breathe", "icon": "alert"},
            {"effect": "blink", "icon": "alert"},
        ]
        r._pressed_until = {}
        writes = []
        r.push_indices = lambda indices, phase: writes.append(tuple(sorted(indices)))

        class StopTicks(Exception):
            pass

        sleeps = 0

        async def twelve_ticks(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > self.renderer_hw.ANIMATION_FPS:
                raise StopTicks

        with mock.patch.object(self.renderer_hw.asyncio, "sleep", twelve_ticks):
            with self.assertRaises(StopTicks):
                asyncio.run(r._animate())

        self.assertEqual(sum(0 in batch for batch in writes), 12)
        self.assertEqual(sum(1 in batch for batch in writes), 4)
        self.assertEqual(sum(2 in batch for batch in writes), 4)

    def test_error_disconnect_cancels_animator_before_reconnect(self):
        """A dropped hub connection must not leave another 12fps writer alive."""
        r = self.renderer_hw.HWRenderer("ws://unused", 45)

        class Deck:
            def set_key_callback(self, callback):
                self.callback = callback

        deck = Deck()
        r.open_device = lambda: deck
        r.deck = deck
        started = False
        cancelled = False

        async def animate_until_cancelled():
            nonlocal started, cancelled
            started = True
            try:
                await asyncio.Future()
            finally:
                cancelled = True

        r._animate = animate_until_cancelled
        closed_error = self.renderer_hw.websockets.ConnectionClosedError

        class DroppedSocket:
            async def send(self, _message):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                # Give the newly-created animator one turn before simulating
                # an abnormal close, so the test proves its cleanup executes.
                await asyncio.sleep(0)
                raise closed_error(None, None)

        async def one_connection():
            yield DroppedSocket()

        r._reconnect = one_connection
        asyncio.run(r.run())

        self.assertTrue(started)
        self.assertTrue(cancelled)
        self.assertIsNone(r._ws, "a physical press could target the dead socket")

    def test_renderer_waits_for_a_late_stream_deck_instead_of_exiting(self):
        """Login may happen before USB attach; the whole stack must stay up."""
        r = self.renderer_hw.HWRenderer("ws://unused", 45)
        attempts = 0

        class Deck:
            def set_key_callback(self, callback):
                self.callback = callback

        deck = Deck()

        def late_device():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("no visual Stream Deck found")
            r.deck = deck
            return deck

        async def no_connections():
            if False:
                yield None

        r.open_device = late_device
        r._reconnect = no_connections
        with mock.patch.object(self.renderer_hw.asyncio, "sleep",
                               mock.AsyncMock(return_value=None)):
            asyncio.run(r.run())

        self.assertEqual(attempts, 3)
        self.assertIs(deck.callback.__self__, r)

    def test_renderer_reopens_after_the_deck_disconnects(self):
        """A USB unplug must not restart Discord and every other connector."""
        r = self.renderer_hw.HWRenderer("ws://unused", 45)
        opens = 0
        connections = 0

        class Deck:
            def set_key_callback(self, callback):
                self.callback = callback

            def close(self):
                pass

        def open_again():
            nonlocal opens
            opens += 1
            r.deck = Deck()
            return r.deck

        class OneState:
            async def send(self, _message):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if hasattr(self, "sent"):
                    raise StopAsyncIteration
                self.sent = True
                return json.dumps({"type": "state", "faces": []})

        async def connection_then_done():
            nonlocal connections
            connections += 1
            if connections == 1:
                yield OneState()

        writes = 0

        def disconnected_write(_phase):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise OSError("device disconnected")

        r.open_device = open_again
        r._reconnect = connection_then_done
        r.push_all = disconnected_write
        with mock.patch.object(self.renderer_hw.asyncio, "sleep",
                               mock.AsyncMock(return_value=None)):
            asyncio.run(r.run())

        self.assertEqual(opens, 2)
        self.assertEqual(connections, 2)

    def test_animation_tick_raises_when_the_deck_is_gone(self):
        """A static board does no HID writes. The power poll must still notice
        an unplug, or a replugged deck stays dark forever."""
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.faces = [{"effect": "solid", "icon": "idle"}]
        r._pressed_until = {}
        r._display_suspended = False
        r._next_power_check = 0.0
        r.health = None
        r.deck = mock.Mock(connected=mock.Mock(return_value=False))
        r.refresh_display_power = lambda: None
        r.push_indices = lambda indices, phase: None

        sleeps = 0

        async def one_tick(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 2:
                raise AssertionError("disconnected deck did not fail the animator")

        with mock.patch.object(self.renderer_hw.asyncio, "sleep", one_tick):
            with self.assertRaises(OSError):
                asyncio.run(r._animate())

    def test_renderer_reopens_after_an_animation_write_fails(self):
        """An animator HID error must reopen the device, not die on a
        background task while the websocket stays up and the deck stays dark."""
        r = self.renderer_hw.HWRenderer(
            "ws://unused", 45, session_locked=lambda: False)
        opens = 0
        connections = 0

        class Deck:
            def set_key_callback(self, callback):
                self.callback = callback

            def close(self):
                pass

            def connected(self):
                return True

        def open_again():
            nonlocal opens
            opens += 1
            r.deck = Deck()
            r.faces = [{"effect": "shimmer", "icon": "working"}]
            return r.deck

        class HoldOpen:
            async def send(self, _message):
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

        async def connection_then_done():
            nonlocal connections
            connections += 1
            if connections == 1:
                yield HoldOpen()

        def disconnected_indices(_indices, _phase):
            raise OSError("device disconnected")

        r.open_device = open_again
        r._reconnect = connection_then_done
        r.push_indices = disconnected_indices
        r.push_all = lambda phase: None

        async def bounded():
            try:
                await asyncio.wait_for(r.run(), timeout=1.0)
            except asyncio.TimeoutError:
                self.fail("animator HID error never reopened the device")

        with mock.patch.object(self.renderer_hw.asyncio, "sleep",
                               mock.AsyncMock(return_value=None)):
            asyncio.run(bounded())

        self.assertGreaterEqual(opens, 2)
        self.assertGreaterEqual(connections, 2)

    def test_animation_tick_restores_a_key_after_press_flash_expires(self):
        """The final undimmed frame is written once, then the key goes quiet."""
        r = self.renderer_hw.HWRenderer.__new__(self.renderer_hw.HWRenderer)
        r.faces = [{"effect": "solid", "icon": "idle"} for _ in range(15)]
        r._pressed_until = {6: time.monotonic() - 0.01}
        writes = []
        r.push_indices = lambda indices, phase: writes.append(tuple(sorted(indices)))

        class StopTick(Exception):
            pass

        sleeps = 0

        async def one_tick(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise StopTick

        with mock.patch.object(self.renderer_hw.asyncio, "sleep", one_tick):
            with self.assertRaises(StopTick):
                asyncio.run(r._animate())

        self.assertEqual(writes, [(6,)])
        self.assertNotIn(6, r._pressed_until,
                         "expired press would be redrawn forever")


class TestEmulatorAnimations(unittest.TestCase):
    """The browser renderer exposes the same motion language as hardware."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "emulator.html"), encoding="utf-8") as handle:
            cls.html = handle.read()

    def test_working_ring_and_product_marks_have_motion(self):
        self.assertIn("@keyframes status-spin", self.html)
        self.assertIn("status-working", self.html)
        for motion in ("motion-claude", "motion-codex", "motion-hermes"):
            with self.subTest(motion=motion):
                self.assertIn(motion, self.html)

    def test_reduced_motion_preference_stops_decorative_animation(self):
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.html)
        self.assertIn("animation: none !important", self.html)

    def test_emulator_clips_only_the_nous_raster_perimeter(self):
        self.assertIn(".key.mark-lg .logo", self.html)
        self.assertIn("clip-path: inset(1px)", self.html)

    def test_emulator_has_numeric_notification_bubbles(self):
        self.assertIn('class="notification"', self.html)
        self.assertIn("background:#ff3b30", self.html)
        self.assertIn('notificationCount > 99 ? "99+"', self.html)

    def test_emulator_has_large_icon_action_layout(self):
        self.assertIn(".key.icon-action .logo", self.html)
        self.assertIn('(f.layout==="icon-action"?" icon-action":"")', self.html)


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=0).result
    total = result.testsRun
    bad = len(result.failures) + len(result.errors)
    print(f"{total - bad}/{total} passed")
    sys.exit(1 if bad else 0)
