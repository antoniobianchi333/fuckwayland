"""wxrandr unit tests: option-parse byte parity, the pending-layout resolver
(which has no overlap rule: overlapping layouts are the compositor's call,
not ours), transform mapping (against the XWayland-verified table), modeline
math, and query rendering against oracle capture bytes. No compositor
needed."""

import contextlib
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wxrandr import cli, core                                   # noqa: E402
from wxrandr.core import (Mode, OutputState, Stanza, State,     # noqa: E402
                          build_targets, resolve_positions)

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
    return code, out.getvalue(), err.getvalue()


def mk_state():
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    f.close()
    os.unlink(f.name)
    return State("test", path=f.name)


def mk_output(name, w, h, x=0, y=0, active=True, transform="normal",
              scale=1.0, refresh=59860, modes=None):
    o = OutputState(name=name, active=active, x=x, y=y, scale=scale,
                    transform=transform)
    if active:
        lw, lh = core.logical_size(w, h, transform, scale)
        o.w, o.h = lw, lh
    if modes is None:
        modes = [(w, h, refresh)]
    o.modes = [Mode(w=mw, h=mh, refresh_mhz=mr) for mw, mh, mr in modes]
    o.virtual_modes = False
    if o.modes:
        o.modes[0].preferred = True
        if active:
            o.current = o.modes[0]
    return o


class ParseErrors(unittest.TestCase):
    """stderr bytes straight from the oracle error captures."""

    CASES = [
        (["--zorp"], "xrandr: unrecognized option '--zorp'\n"),
        (["--mode", "800x600"],
         "xrandr: --mode must be used after --output\n"),
        (["--left-of", "HEADLESS-1"],
         "xrandr: --left-of must be used after --output\n"),
        (["--output", "X", "--pos", "nope"],
         "xrandr: failed to parse 'nope' as a position\n"),
        (["--output", "X", "--gamma", "banana"],
         "xrandr: --gamma: invalid argument 'banana'\n"),
        (["--output", "X", "--gamma", "0:1:1"],
         "xrandr: gamma correction factors must be positive\n"),
        (["--output", "X", "--brightness", "dim"],
         "xrandr: --brightness: invalid argument 'dim'\n"),
        (["--output", "X", "--rotate", "sideways"],
         "xrandr: --rotate: invalid argument 'sideways'\n"),
        (["--output", "X", "--reflect", "z"],
         "xrandr: --reflect: invalid argument 'z'\n"),
        (["--output", "X", "--scale", "big"],
         "xrandr: failed to parse 'big' as a scaling factor\n"),
        (["--output", "X", "--scale", "0"],
         "xrandr: scaling factors must be positive\n"),
        (["--output", "X", "--scale-from", "wide"],
         "xrandr: failed to parse 'wide' as a scale-from size\n"),
        (["--fb", "big"],
         "xrandr: failed to parse 'big' as a framebuffer size\n"),
        (["--fbmm", "big"],
         "xrandr: failed to parse 'big' as a physical size\n"),
        (["--output"], "xrandr: --output requires an argument\n"),
        (["--addmode", "X"], "xrandr: --addmode requires two arguments\n"),
        (["--delmode", "X"], "xrandr: --delmode requires two arguments\n"),
        (["--setmonitor", "a", "b"],
         "xrandr: --setmonitor requires three argument\n"),  # sic (oracle)
        (["--screen", "-2"],
         "xrandr: --screen argument must be nonnegative\n"),
        (["-s", "-4"], "xrandr: --size argument must be nonnegative\n"),
        (["-o", "diagonal"], "xrandr: -o: invalid argument 'diagonal'\n"),
        (["--rate", "fast"],
         "xrandr: failed to parse 'fast' as a number\n"),
    ]

    def test_argerr_bytes(self):
        tail = "Try 'xrandr --help' for more information.\n"
        for argv, want in self.CASES:
            code, out, err = run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(err, want + tail, argv)
            self.assertEqual(out, "", argv)

    def test_help_stdout_exit0(self):
        code, out, err = run_cli("--help")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("usage: xrandr [options]\n"))
        self.assertIn("--setmonitor <name> {auto|<w>/<mmw>x<h>/<mmh>", out)
        self.assertEqual(out.count("\n"), 62)
        code2, out2, _ = run_cli("-help")
        self.assertEqual((code2, out2), (0, out))


class TransformMapping(unittest.TestCase):
    """The sway<->RandR table was verified against XWayland's own RandR
    translation (sway 90 shows as `right`, flipped as `normal X axis`,
    flipped-90 as `right X axis`, ...)."""

    VERIFIED = {  # sway transform -> what real xrandr printed for it
        "normal": ("normal", "normal"), "90": ("right", "normal"),
        "180": ("inverted", "normal"), "270": ("left", "normal"),
        "flipped": ("normal", "x"), "flipped-90": ("right", "x"),
        "flipped-180": ("inverted", "x"), "flipped-270": ("left", "x"),
    }

    def test_randr_view(self):
        self.assertEqual(core.RANDR_VIEW, self.VERIFIED)

    def test_sway_transform_roundtrip(self):
        for tf, (rot, refl) in self.VERIFIED.items():
            self.assertEqual(core.sway_transform(rot, refl), tf)

    def test_all_16_combos_map_to_equivalent_transform(self):
        # reflect y == reflect x + rotate 180; reflect xy == rotate 180
        self.assertEqual(core.sway_transform("normal", "y"), "flipped-180")
        self.assertEqual(core.sway_transform("right", "y"), "flipped-270")
        self.assertEqual(core.sway_transform("inverted", "y"), "flipped")
        self.assertEqual(core.sway_transform("left", "y"), "flipped-90")
        self.assertEqual(core.sway_transform("normal", "xy"), "180")
        self.assertEqual(core.sway_transform("right", "xy"), "270")
        self.assertEqual(core.sway_transform("inverted", "xy"), "normal")
        self.assertEqual(core.sway_transform("left", "xy"), "90")

    def test_logical_size(self):
        # observed sway/wlroots rounding: truncation after transform swap
        self.assertEqual(core.logical_size(1280, 720, "normal", 1.5),
                         (853, 480))
        self.assertEqual(core.logical_size(1111, 666, "normal", 1.5),
                         (740, 444))
        self.assertEqual(core.logical_size(1281, 721, "normal", 2), (640, 360))
        self.assertEqual(core.logical_size(1280, 720, "270", 2), (360, 640))
        self.assertEqual(core.logical_size(1280, 720, "flipped-90", 1),
                         (720, 1280))


class ModelineMath(unittest.TestCase):
    def test_refresh_formula(self):
        # the classic CVT 1280x720 modeline: 74.50MHz 1664x748 -> 59.86Hz
        hz = core.mode_refresh_hz(74.50, 1664, 748)
        self.assertEqual("%6.2f" % hz, " 59.86")

    def test_doublescan_and_interlace(self):
        base = core.mode_refresh_hz(74.50, 1664, 748)
        self.assertAlmostEqual(
            core.mode_refresh_hz(74.50, 1664, 748, ["doublescan"]),
            base / 2)
        self.assertAlmostEqual(
            core.mode_refresh_hz(74.50, 1664, 748, ["interlace"]), base * 2)

    def test_custom_mode_through_state(self):
        st = mk_state()
        st.modes()["fancy"] = {"clock": 74.50,
                               "h": [1280, 1344, 1472, 1664],
                               "v": [720, 723, 728, 748],
                               "flags": ["-hsync", "+vsync"]}
        st.addmodes()["OUT"] = ["fancy"]
        (m,) = st.modes_for_output("OUT")
        self.assertEqual((m.w, m.h, m.name, m.custom),
                         (1280, 720, "fancy", True))
        self.assertEqual("%6.2f" % m.refresh_hz, " 59.86")

    def test_mm_synthesis_matches_xwayland(self):
        # oracle listmonitors: 1280->339 720->190 1024->271 768->203
        #                      800->212 600->159 1920->508 1080->286
        for px, mm in ((1280, 339), (720, 190), (1024, 271), (768, 203),
                       (800, 212), (600, 159), (1920, 508), (1080, 286)):
            self.assertEqual(core.synth_mm(px), mm, px)
        # X screen mm truncate: 1280->338, 720->190 (oracle -s table)
        self.assertEqual(core.screen_mm(1280), 338)
        self.assertEqual(core.screen_mm(720), 190)


class Resolver(unittest.TestCase):
    def L(self):  # noqa: N802 - three outputs, side by side
        return [mk_output("A", 1280, 720),
                mk_output("B", 1024, 768, x=1280),
                mk_output("C", 800, 600, x=2304)]

    def dims(self, targets, state):
        return {t.name: core.predicted_dims(t, state)
                for t in targets if t.enabled}

    def test_chain_right_of_pending(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="B", relation=("right-of", "A")),
                   Stanza(name="C", relation=("right-of", "B"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos, {"A": (0, 0), "B": (1280, 0), "C": (2304, 0)})

    def test_l_shape_and_below(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="B", relation=("right-of", "A")),
                   Stanza(name="C", relation=("below", "A"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["C"], (0, 720))

    def test_left_of_goes_negative_then_normalizes(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="C",
                                         relation=("left-of", "A"))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        # C lands at -800, then the whole layout shifts right by 800
        self.assertEqual(pos, {"C": (0, 0), "A": (800, 0), "B": (2080, 0)})

    def test_relative_uses_rotated_dims(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="B", rotate="left",
                          relation=("right-of", "A")),
                   Stanza(name="C", relation=("right-of", "B"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["C"], (1280 + 768, 0))  # B is portrait now

    def test_same_as_mirrors(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="B",
                                         relation=("same-as", "A"))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["B"], pos["A"])

    def test_relation_loop_is_fatal(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="A", relation=("right-of", "B")),
                   Stanza(name="B", relation=("right-of", "A"))]
        ts = build_targets(outs, stanzas, st)
        with self.assertRaises(core.Fatal) as cm:
            resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(cm.exception.args[0],
                         "loop in relative position specifications\n")

    def test_relative_to_unknown_output_fatal(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="B",
                                         relation=("right-of", "NOPE"))], st)
        with self.assertRaises(core.Fatal) as cm:
            resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(cm.exception.args[0],
                         'cannot find output "NOPE"\n')

    def test_relative_to_disabled_lands_at_origin(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="B", off=True),
                   Stanza(name="C", relation=("right-of", "B"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["C"], (0, 0))
        self.assertNotIn("B", pos)

    def test_explicit_negative_pos_normalizes(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="A", pos=(-200, -100))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["A"], (0, 0))
        self.assertEqual(pos["B"], (1480, 100))

    def test_an_overlap_survives_the_resolver_untouched(self):
        """wxrandr has no geometry policy of its own: an overlapping --pos
        is resolved, normalised and pinned exactly as asked.  On sway and
        wlroots that is genuine partial mirroring -- both outputs are
        viewports onto one scene, and the shared region came back
        byte-identical on both heads (measured, sway 1.11)."""
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="B", pos=(640, 0))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos, {"A": (0, 0), "B": (640, 0), "C": (2304, 0)})
        self.assertEqual(core.position_commands(ts, pos),
                         ["output A position 0 0", "output B position 640 0",
                          "output C position 2304 0"])
        # a full overlap (--same-as) and a rotation into a neighbour are the
        # same story: nothing here objects
        ts = build_targets(outs, [Stanza(name="B", pos=(0, 0)),
                                  Stanza(name="C", pos=(100, 100))], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos, {"A": (0, 0), "B": (0, 0), "C": (100, 100)})

    def test_unknown_output_stanza_warns_not_fatal(self):
        outs = self.L()
        st = mk_state()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ts = build_targets(outs, [Stanza(name="GHOST", off=True)], st)
        self.assertEqual(err.getvalue(),
                         "warning: output GHOST not found; ignoring\n")
        self.assertTrue(all(t.enabled for t in ts))

    def test_off_and_hole_stays(self):
        outs = self.L()
        st = mk_state()
        ts = build_targets(outs, [Stanza(name="B", off=True)], st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos, {"A": (0, 0), "C": (2304, 0)})  # hole kept

    def test_scale_changes_pending_dims(self):
        outs = self.L()
        st = mk_state()
        stanzas = [Stanza(name="A", scale=(2.0, 2.0)),
                   Stanza(name="B", relation=("right-of", "A"))]
        ts = build_targets(outs, stanzas, st)
        pos = resolve_positions(ts, self.dims(ts, st))
        self.assertEqual(pos["B"], (640, 0))

    def test_mode_lookup(self):
        o = mk_output("A", 1280, 720,
                      modes=[(1280, 720, 59860), (1280, 720, 50000),
                             (800, 600, 59860)])
        t = build_targets([o], [Stanza(name="A", mode="800x600")], mk_state())
        self.assertEqual((t[0].mode.w, t[0].mode.h), (800, 600))
        # nearest-rate pick (xrandr find_mode has no threshold)
        t = build_targets([o], [Stanza(name="A", mode="1280x720",
                                       rate=49.0)], mk_state())
        self.assertEqual(t[0].mode.refresh_mhz, 50000)
        with self.assertRaises(core.Fatal) as cm:
            build_targets([o], [Stanza(name="A", mode="9999x9999")],
                          mk_state())
        self.assertEqual(cm.exception.args[0],
                         "cannot find mode 9999x9999\n")

    def test_virtual_output_accepts_any_mode(self):
        o = mk_output("A", 1280, 720)
        o.virtual_modes = True
        t = build_targets([o], [Stanza(name="A", mode="777x555")], mk_state())
        self.assertEqual((t[0].mode.w, t[0].mode.h, t[0].mode.custom),
                         (777, 555, True))


class AutoMode(unittest.TestCase):
    """--auto must switch to the PREFERRED mode, not no-op / remembered mode
    (review finding: build_targets --auto)."""

    def test_auto_switches_active_output_to_preferred(self):
        o = mk_output("A", 800, 600,
                      modes=[(800, 600, 59860), (1280, 720, 59860)])
        o.modes[0].preferred = False   # active at 800x600 ...
        o.modes[1].preferred = True    # ... but preferred is 1280x720
        o.current = o.modes[0]
        t = build_targets([o], [Stanza(name="A", auto=True)], mk_state())
        self.assertTrue(t[0].enabled)
        self.assertEqual((t[0].mode.w, t[0].mode.h), (1280, 720))

    def test_auto_keeps_explicit_mode(self):
        o = mk_output("A", 800, 600,
                      modes=[(800, 600, 59860), (1280, 720, 59860)])
        t = build_targets([o], [Stanza(name="A", auto=True, mode="800x600")],
                          mk_state())
        self.assertEqual((t[0].mode.w, t[0].mode.h), (800, 600))

    def test_global_auto_reenables_disabled_at_preferred(self):
        o = mk_output("A", 1280, 720, active=False,
                      modes=[(1280, 720, 59860)])
        ts = build_targets([o], [], mk_state(), global_auto=True)
        self.assertTrue(ts[0].enabled)
        self.assertTrue(ts[0].changed)
        self.assertEqual((ts[0].mode.w, ts[0].mode.h), (1280, 720))


class ScreenBound(unittest.TestCase):
    """xrandr's `screen cannot be larger than` fatal (set_screen_size)."""

    def _t(self, name):
        return type("T", (), {"name": name, "enabled": True})()

    def test_layout_too_wide_is_fatal(self):
        opts = cli.Opts()
        with self.assertRaises(core.Fatal) as cm:
            cli._check_screen_size(opts, [self._t("A")], {"A": (1280, 720)},
                                   {"A": (100000, 0)})
        self.assertEqual(cm.exception.args[0],
                         "screen cannot be larger than 32767x32767 "
                         "(desired size 101280x720)\n")

    def test_fb_too_large_is_fatal(self):
        opts = cli.Opts()
        opts.fb = (40000, 40000)
        with self.assertRaises(core.Fatal) as cm:
            cli._check_screen_size(opts, [], {}, {})
        self.assertEqual(cm.exception.args[0],
                         "screen cannot be larger than 32767x32767 "
                         "(desired size 40000x40000)\n")

    def test_within_bounds_ok(self):
        opts = cli.Opts()
        cli._check_screen_size(opts, [self._t("A")], {"A": (1280, 720)},
                               {"A": (0, 0)})  # no raise


class Rendering(unittest.TestCase):
    """Byte checks against the oracle captures (the 3-output L-shape)."""

    def l_shape(self):
        outs = [mk_output("HEADLESS-1", 1280, 720),
                mk_output("HEADLESS-2", 1024, 768, x=1280),
                mk_output("HEADLESS-3", 800, 600, y=720)]
        return outs

    def test_screen_line_l_shape(self):
        # oracle xrandr2-query-3out
        self.assertEqual(
            core.render_screen_line(0, self.l_shape()),
            "Screen 0: minimum 16 x 16, current 2304 x 1320, "
            "maximum 32767 x 32767")

    def test_output_headers_l_shape(self):
        st = mk_state()
        lines = core.render_query(self.l_shape(), st)
        self.assertEqual(lines[1], "HEADLESS-1 connected 1280x720+0+0 "
                         "(normal left inverted right x axis y axis) "
                         "0mm x 0mm")
        self.assertIn("HEADLESS-3 connected 800x600+0+720 "
                      "(normal left inverted right x axis y axis) 0mm x 0mm",
                      lines)

    def test_mode_table_bytes(self):
        o = mk_output("H", 1280, 720,
                      modes=[(1280, 720, 59860), (800, 600, 59860)])
        rows = core.render_mode_table(o)
        # oracle: current+preferred `*+`, others two trailing spaces
        self.assertEqual(rows[0], "   1280x720      59.86*+")
        self.assertEqual(rows[1], "   800x600       59.86  ")

    def test_mode_table_groups_same_name(self):
        o = mk_output("H", 1920, 1080,
                      modes=[(1920, 1080, 60000), (1920, 1080, 50000)])
        rows = core.render_mode_table(o)
        self.assertEqual(rows, ["   1920x1080     60.00*+  50.00  "])

    def test_monitors_l_shape_bytes(self):
        # oracle xrandr2-listmonitors-3out, byte for byte
        st = mk_state()
        self.assertEqual(core.render_monitors(self.l_shape(), st), [
            "Monitors: 3",
            " 0: +HEADLESS-1 1280/339x720/190+0+0  HEADLESS-1",
            " 1: +HEADLESS-2 1024/271x768/203+1280+0  HEADLESS-2",
            " 2: +HEADLESS-3 800/212x600/159+0+720  HEADLESS-3",
        ])

    def test_monitors_primary_star(self):
        st = mk_state()
        st.primary = "HEADLESS-2"
        lines = core.render_monitors(self.l_shape(), st)
        self.assertIn(" 1: +*HEADLESS-2 1024/271x768/203+1280+0  HEADLESS-2",
                      lines)

    def test_primary_and_rotation_header(self):
        st = mk_state()
        st.primary = "HEADLESS-1"
        o = mk_output("HEADLESS-1", 1280, 720, transform="270")
        line = core.render_output_header(o, st.primary)
        self.assertEqual(line, "HEADLESS-1 connected primary 720x1280+0+0 "
                         "left (normal left inverted right x axis y axis) "
                         "0mm x 0mm")

    def test_reflection_header_prints_rotation_word(self):
        o = mk_output("H", 1280, 720, transform="flipped")
        self.assertEqual(
            core.render_output_header(o, None),
            "H connected 1280x720+0+0 normal X axis "
            "(normal left inverted right x axis y axis) 0mm x 0mm")

    def test_disabled_output_header(self):
        o = mk_output("H", 1280, 720, active=False)
        self.assertEqual(core.render_output_header(o, None),
                         "H connected (normal left inverted right x axis "
                         "y axis)")

    def test_disabled_everything_screen_current_zero(self):
        o = mk_output("H", 1280, 720, active=False)
        self.assertEqual(
            core.render_screen_line(0, [o]),
            "Screen 0: minimum 16 x 16, current 0 x 0, "
            "maximum 32767 x 32767")

    def test_verbose_custom_modeline_bytes(self):
        st = mk_state()
        st.modes()["fancy"] = {"clock": 74.50,
                               "h": [1280, 1344, 1472, 1664],
                               "v": [720, 723, 728, 748],
                               "flags": ["-hsync", "+vsync"]}
        st.addmodes()["OUT"] = ["fancy"]
        (m,) = st.modes_for_output("OUT")
        m.preferred = True
        lines = core.render_verbose_mode(m, {}, True)
        # oracle print_verbose_mode bytes (xrandr-verbose.out shape)
        self.assertEqual(lines[0],
                         "  fancy (0x0) 74.500MHz -HSync +VSync *current "
                         "+preferred")
        self.assertEqual(lines[1],
                         "        h: width  1280 start 1344 end 1472 total "
                         "1664 skew    0 clock  44.77KHz")
        self.assertEqual(lines[2],
                         "        v: height  720 start  723 end  728 total "
                         " 748           clock  59.86Hz")

    def test_verbose_gamma_falls_back_when_holder_stale(self):
        # a holder record whose starttime no longer matches (killed/recycled)
        # must not keep reporting the old brightness — the compositor restored
        # the neutral ramp (review finding: render_verbose_block gamma record)
        st = mk_state()
        o = mk_output("H", 1280, 720)
        st.gamma()["H"] = {"pid": os.getpid(), "start": "0",
                           "brightness": 0.5, "gamma": [1.2, 1.0, 0.9]}
        lines = core.render_verbose_block(o, st, 0)
        self.assertIn("\tGamma:      1.0:1.0:1.0", lines)
        self.assertIn("\tBrightness: 1.0", lines)

    def test_verbose_gamma_reports_live_holder(self):
        from wxrandr import gamma as g
        st = mk_state()
        o = mk_output("H", 1280, 720)
        pid = os.getpid()
        st.gamma()["H"] = {"pid": pid, "start": g._proc_starttime(pid),
                           "brightness": 0.5, "gamma": [1.0, 1.0, 1.0]}
        lines = core.render_verbose_block(o, st, 0)
        self.assertIn("\tBrightness: 0.50", lines)

    def test_providers(self):
        lines = core.render_providers(self.l_shape(), "sway")
        self.assertEqual(lines[0], "Providers: number : 1")
        self.assertEqual(lines[1],
                         "Provider 0: id: 0x1 cap: 0xb, Source Output, "
                         "Sink Output, Sink Offload crtcs: 3 outputs: 3 "
                         "associated providers: 0 name:sway")

    def test_q1_table_bytes(self):
        # oracle xrandr2-dryrun-size shape (single row here)
        outs = [mk_output("H", 1280, 720)]
        lines = core.render_q1(outs, mk_state())
        self.assertEqual(lines[0],
                         " SZ:    Pixels          Physical       Refresh")
        self.assertEqual(lines[1],
                         "*0   1280 x 720    ( 338mm x 190mm )  *60  ")
        self.assertEqual(core.render_q1_state(outs[0]), [
            "Current rotation - normal",
            "Current reflection - none",
            "Rotations possible - normal left inverted right ",
            "Reflections possible - X Axis Y Axis",
        ])


class StateFile(unittest.TestCase):
    def test_roundtrip_and_isolation(self):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        st = State("sock-a", path=f.name)
        st.primary = "OUT-1"
        st.modes()["m"] = {"clock": 1.0, "h": [1, 2, 3, 4],
                           "v": [5, 6, 7, 8], "flags": []}
        st.save()
        again = State("sock-a", path=f.name)
        self.assertEqual(again.primary, "OUT-1")
        self.assertIn("m", again.modes())
        other = State("sock-b", path=f.name)
        self.assertIsNone(other.primary)

    def test_corrupt_file_ignored(self):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                        mode="w")
        f.write("{not json")
        f.close()
        self.addCleanup(os.unlink, f.name)
        st = State("k", path=f.name)
        self.assertIsNone(st.primary)

    def _tmp(self):
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        f.close()
        os.unlink(f.name)
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        self.addCleanup(lambda: os.path.exists(f.name + ".lock")
                        and os.unlink(f.name + ".lock"))
        return f.name

    def test_concurrent_same_key_writes_merge(self):
        # two "processes" writing different outputs' gamma under one key must
        # not drop each other's record (read-modify-write race fix)
        path = self._tmp()
        a = State("k", path=path)
        b = State("k", path=path)          # both loaded the (empty) file
        a.gamma()["A"] = {"pid": 1, "start": "sa"}
        b.gamma()["B"] = {"pid": 2, "start": "sb"}
        a.save()
        b.save()                           # must merge onto A's write
        c = State("k", path=path)
        self.assertIn("A", c.gamma())
        self.assertIn("B", c.gamma())

    def test_concurrent_delete_survives_merge(self):
        path = self._tmp()
        seed = State("k", path=path)
        seed.gamma()["A"] = {"pid": 1, "start": "sa"}
        seed.save()
        a = State("k", path=path)          # sees A
        b = State("k", path=path)          # sees A
        b.gamma()["B"] = {"pid": 2, "start": "sb"}
        b.save()                           # disk now A + B
        a.gamma().pop("A")                 # a removes A
        a.save()                           # A gone, B kept
        c = State("k", path=path)
        self.assertNotIn("A", c.gamma())
        self.assertIn("B", c.gamma())

    def test_other_key_preserved_across_writers(self):
        path = self._tmp()
        a = State("k1", path=path)
        a.primary = "OUT-1"
        a.save()
        b = State("k2", path=path)
        b.primary = "OUT-2"
        b.save()
        self.assertEqual(State("k1", path=path).primary, "OUT-1")
        self.assertEqual(State("k2", path=path).primary, "OUT-2")

    def test_corrupt_custom_mode_returns_none(self):
        st = mk_state()
        st.modes()["empty"] = {}
        st.modes()["short"] = {"clock": 1.0, "h": [1, 2], "v": [3, 4, 5, 6]}
        st.modes()["wrongtype"] = {"clock": 1.0, "h": 5, "v": 6}
        self.assertIsNone(st.custom_mode("empty"))
        self.assertIsNone(st.custom_mode("short"))
        self.assertIsNone(st.custom_mode("wrongtype"))
        self.assertIsNone(st.custom_mode("missing"))


class VersionAndMisc(unittest.TestCase):
    def test_version_program_line(self):
        # the full two-line --version needs a compositor; the program line
        # prints before connecting and must match the oracle bytes
        self.assertEqual("xrandr program version       "
                         + core.PROGRAM_VERSION,
                         "xrandr program version       1.5.4")

    def test_gamma_ramp_math(self):
        from wxrandr.gamma import compute_ramp
        import struct as st

        ramp = compute_ramp(4, 1.0, (1.0, 1.0, 1.0))
        vals = st.unpack("=12H", ramp)
        self.assertEqual(vals[:4], (0, 21845, 43690, 65535))  # linear
        self.assertEqual(vals[:4], vals[4:8])  # per-channel identical
        ramp = compute_ramp(4, 0.5, (1.0, 1.0, 1.0))
        vals = st.unpack("=12H", ramp)
        self.assertEqual(vals[3], 32767)  # brightness scales the top
        # gamma exponent: xrandr uses (i/(size-1))^(1/gamma)*brightness
        ramp = compute_ramp(4, 1.0, (2.0, 1.0, 1.0))
        vals = st.unpack("=12H", ramp)
        self.assertEqual(vals[1], int((1 / 3) ** 0.5 * 65535))

    def test_negative_brightness_clamps_to_black(self):
        # xrandr accepts a negative --brightness and applies the (black) ramp,
        # exit 0; without the low clamp struct.pack('=H') would raise
        from wxrandr.gamma import compute_ramp
        import struct as st
        ramp = compute_ramp(4, -0.5, (1.0, 1.0, 1.0))
        self.assertEqual(st.unpack("=12H", ramp)[:4], (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
