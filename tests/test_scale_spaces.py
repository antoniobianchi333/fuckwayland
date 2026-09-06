"""The layout box under fractional and HiDPI scaling, against what the
desktops really do.

Every absolute pointer command is mapped across the layout bounding box
`_wayland_bbox()` reads off the wire, so which pixel space that box is in
decides where `mousemove` lands.  Each case below is one state measured on a
real desktop -- the wire values on the left, the pointer space the hardware
cursor was actually found in on the right -- so the fake compositor here is
a replay of a measurement, not an invention.  What was measured, and how:
`repro/README.md`, "the scaling pass"; the raw readings are in
`tests/fixtures/scaling/`.

The oracle in the guest was the KMS cursor plane (`/sys/kernel/debug/dri/
*/state`, device pixels on the scanout, which nothing in fuckwayland
produces) and, where the compositor draws its own cursor instead of using
that plane, a QMP screendump differenced against a parked one.

One measured state is a compositor bug rather than a pixel space:
StaleXdgOutput, where GNOME 46 keeps advertising a layout it has stopped
drawing -- and keeps mapping absolute pointer motion across it, which is why
the wire's box is still the right one for the pointer there.  What is wrong
in that state is what the user is *told*, and DisplayConfigSource below is
the second source that notices.
"""

import io
import json
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

import test_wxrandr_mutter as twm                         # noqa: E402
import wl_fake                                            # noqa: E402
from wdotool import daemon, layoutbox                     # noqa: E402
from wxrandr import mutter                                # noqa: E402


# A head, as the wire carries it: the wl_output mode (real pixels), the
# integer wl_output.scale -- which cannot carry 1.5 and lies with 2 -- and
# the zxdg_output_v1 logical position and size, or None for a compositor
# that does not advertise the xdg_output manager at all.
def head(mode_w, mode_h, wl_scale, lx, ly, lw, lh):
    return dict(mw=mode_w, mh=mode_h, scale=wl_scale, lx=lx, ly=ly, lw=lw, lh=lh)


class OutputCompositor(wl_fake.Server):
    """wl_output + zxdg_output_v1 for a given head table, nothing else.

    `_wayland_bbox()` binds every wl_output, then every xdg_output, and
    takes the logical geometry when it is advertised.  This serves exactly
    those two interfaces so a state measured on GNOME or KWin can be
    replayed byte for byte.
    """

    PREFIX = "wdotool-scale-"

    def __init__(self, heads, with_xdg_output=True):
        self.heads = heads
        self.with_xdg_output = with_xdg_output
        super().__init__(None)

    def advertise(self):
        out = [("wl_output", 4)] * len(self.heads)
        if self.with_xdg_output:
            out.append(("zxdg_output_manager_v1", 3))
        return out

    def new_state(self):
        return {"outputs": {}, "xdg": None}

    def on_bind(self, conn, state, name, iface, version, new_id):
        if iface == "wl_output":
            h = self.heads[self.names[name][1]]
            state["outputs"][new_id] = h
            body = (struct.pack("<iiiii", h["lx"], h["ly"], 480, 270, 0)
                    + wl_fake.wstr("fuckwayland") + wl_fake.wstr("Virtual")
                    + struct.pack("<i", 0))
            self._send(conn, new_id, 0, body)                        # geometry
            self._send(conn, new_id, 1,
                       struct.pack("<Iiii", 1, h["mw"], h["mh"], 75000))  # mode
            self._send(conn, new_id, 3, struct.pack("<i", h["scale"]))    # scale
        elif iface == "zxdg_output_manager_v1":
            state["xdg"] = new_id

    def on_request(self, conn, state, oid, opcode, body, fds):
        if oid == state["xdg"] and opcode == 1:      # get_xdg_output(id, output)
            new_id, out = struct.unpack_from("<II", body)
            h = state["outputs"].get(out)
            if h is not None and h["lw"] is not None:
                self._send(conn, new_id, 0, struct.pack("<ii", h["lx"], h["ly"]))
                self._send(conn, new_id, 1, struct.pack("<ii", h["lw"], h["lh"]))


class BboxCase(unittest.TestCase):
    """Point `_wayland_bbox()` at one replayed compositor and read the box."""

    def bbox(self, heads, with_xdg_output=True, detail=False):
        comp = OutputCompositor(heads, with_xdg_output=with_xdg_output)
        self.addCleanup(comp.close)
        old = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY")}

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(restore)
        os.environ["XDG_RUNTIME_DIR"] = comp.dir
        os.environ["WAYLAND_DISPLAY"] = os.path.basename(comp.path)
        return daemon._wayland_bbox(detail=detail)

    def detail(self, heads, with_xdg_output=True):
        """(box, outs): the box and the per-head wire state behind it."""
        return self.bbox(heads, with_xdg_output, detail=True)


class MeasuredStates(BboxCase):
    """One test per state measured in a guest, named for the desktop.

    In each of these the box we compute is the space the cursor was found
    in, so `mousemove x y` lands on the pixel the caller meant.
    """

    def test_gnome50_scale1(self):
        """Ubuntu 26.04, one 1920x1080 head at 100%.  Nothing to scale, and
        all four readings agreed on every one of 12 targets."""
        self.assertEqual(self.bbox([head(1920, 1080, 1, 0, 0, 1920, 1080)]),
                         (0, 0, 1920, 1080))

    def test_gnome50_scale2_logical(self):
        """Ubuntu 26.04 at 200%.  Mutter is in logical layout mode, so the
        desktop is 960x540 and the cursor plane sat at exactly 2x every
        target: `mousemove 300 200` put it at device 600,400."""
        self.assertEqual(self.bbox([head(1920, 1080, 2, 0, 0, 960, 540)]),
                         (0, 0, 960, 540))

    def test_gnome50_scale150_logical(self):
        """Ubuntu 26.04 at 150%.  `wl_output.scale` cannot carry 1.5 and
        says 2; xdg_output's 1280x720 is the truth, and the wl_output-only
        fallback would put the right edge 320px out.  Measured landings were
        1.5x the target, within the half pixel a 1.5 factor leaves."""
        self.assertEqual(self.bbox([head(1920, 1080, 2, 0, 0, 1280, 720)]),
                         (0, 0, 1280, 720))

    def test_gnome46_scale2_physical(self):
        """Ubuntu 24.04's default: no "Fractional Scaling", so Mutter is in
        PHYSICAL layout mode and its layout coordinates are raw pixels --
        xdg_output says 1920x1080 for a head at 200%.  The cursor plane
        agreed: `mousemove 400 100` put it at device 400,100, 1:1.  Raw
        pixels here are not a bug; they are the space this desktop uses."""
        self.assertEqual(self.bbox([head(1920, 1080, 2, 0, 0, 1920, 1080)]),
                         (0, 0, 1920, 1080))

    def test_gnome50_two_heads_mixed_scales(self):
        """26.04, 1920x1080 at 100% with a second head at 200% beside it.
        Mutter lays the second one out at logical x=1920 and the box is the
        union in logical pixels; 16 targets, both heads, 0px error."""
        heads = [head(1920, 1080, 1, 0, 0, 1920, 1080),
                 head(1920, 1080, 2, 1920, 0, 960, 540)]
        self.assertEqual(self.bbox(heads), (0, 0, 2880, 1080))

    def test_gnome46_two_heads_mixed_scales_physical(self):
        """The same pair on 24.04 with fractional scaling off: both heads
        are raw, the second sits at x=1920 and the box is 3840 wide.  Also
        0px error on 12 targets."""
        heads = [head(1920, 1080, 1, 0, 0, 1920, 1080),
                 head(1920, 1080, 2, 1920, 0, 1920, 1080)]
        self.assertEqual(self.bbox(heads), (0, 0, 3840, 1080))

    def test_kwin_scale2(self):
        """Plasma 6.6 at 200%: logical throughout, like GNOME 50.  KWin
        composites its own cursor at this scale instead of using the 64x64
        virtio-gpu cursor plane, so that state was read off a screendump:
        the sprite's top-left was 2x the target every time."""
        self.assertEqual(self.bbox([head(1920, 1080, 2, 0, 0, 960, 540)]),
                         (0, 0, 960, 540))

    def test_kwin_two_heads_150_and_100(self):
        """Plasma 6.6, one head at 150% and one at 100% right of it."""
        heads = [head(1920, 1080, 2, 0, 0, 1280, 720),
                 head(1920, 1080, 1, 1280, 0, 1920, 1080)]
        self.assertEqual(self.bbox(heads), (0, 0, 3200, 1080))


class StaleXdgOutput(BboxCase):
    """GNOME 46 after the layout mode changes under a scaled monitor.

    Turn "Fractional Scaling" on while a monitor is already at 200% -- what
    a HiDPI laptop owner does -- and Mutter 46 moves to logical layout mode
    without re-sending `zxdg_output_v1.logical_size`.  Measured on
    `noble-gnome-iso`, four times out of four, persisting past a 90s wait
    and past re-applying the same scale:

      org.gnome.Mutter.DisplayConfig   960x540   (the desktop being drawn)
      zxdg_output_v1.logical_size    1920x1080   (stale)
      wl_output mode/scale           1920x1080 @2

    So we advertise a 1920x1080 layout for a 960x540 desktop: every target
    lands at twice its intended fraction of the screen, and everything past
    the middle lands off it -- `mousemove 1600 1000` moved the cursor
    nowhere visible while `getmouselocation` reported 1600,1000.

    AND THE STALE NUMBER IS THE ONE THE POINTER NEEDS.  Mutter maps an
    absolute device across the same rectangle it is still advertising, so with
    this box every target lands exactly where it was asked for -- measured in
    that state against the KMS cursor plane, all eight targets, 1:1 in the
    desktop's own logical coordinates:

      box 1920x1080 (the wire)       asked 100,100 -> logical 100,100
                                     asked 959,539 -> logical 959,539
      box  960x540  (DisplayConfig)  asked 100,100 -> logical 200,200
                                     asked 479,269 -> the bottom-right corner

    So believing DisplayConfig here -- the obvious fix, and the one this file
    first asserted -- doubles every coordinate.  What IS wrong in that state
    is what the user is told: `getdisplaygeometry` reports 1920x1080 for a
    desktop that ends at 959,539, so a coordinate worked out from it misses.
    That is a diagnostic (DisplayConfigSource), not a different box.

    THE WIRE CANNOT TELL THIS APART from test_gnome46_scale2_physical either:
    the three values above are byte for byte what a legitimate physical-layout
    session sends.
    """

    WIRE = [head(1920, 1080, 2, 0, 0, 1920, 1080)]

    def test_the_wire_is_the_box_here_too(self):
        """Asserted as it behaves *and* as it should: identical to
        test_gnome46_scale2_physical, which is both the problem and the
        reason this is where the pointer's coordinates come from."""
        self.assertEqual(self.bbox(self.WIRE), (0, 0, 1920, 1080))

    def test_this_state_is_the_one_that_asks_a_second_source(self):
        """...and the gate is what separates the two: this wire is ambiguous,
        so it is worth a D-Bus call, and the states above are not."""
        _box, outs = self.detail(self.WIRE)
        self.assertTrue(layoutbox.wire_is_ambiguous(outs))


class FallbackWithoutXdgOutput(BboxCase):
    """No zxdg_output_manager_v1 advertised: the box falls back to the
    wl_output mode divided by the integer wl_output.scale.

    A 200% head and a 150% head put the SAME bytes on wl_output -- mode
    1920x1080, scale 2, because that event has no room for 1.5 -- so the
    fallback cannot tell them apart and is right for one and 320px short
    for the other.  That is why xdg_output is preferred, and it is the same
    shape of problem as StaleXdgOutput one interface up.
    """

    WIRE = [head(1920, 1080, 2, 0, 0, None, None)]

    def test_fallback_is_right_at_an_integer_scale(self):
        """A head at 200% really is 960x540 logical."""
        self.assertEqual(self.bbox(self.WIRE, with_xdg_output=False),
                         (0, 0, 960, 540))

    def test_fallback_is_320_short_at_150_percent(self):
        """The identical wire, from a head at 150%, whose desktop is
        1280x720: the fallback still says 960x540, 320px narrow.  Asserted
        as it behaves, not as it should -- with xdg_output present the
        150% case comes out right (test_gnome50_scale150_logical)."""
        self.assertEqual(self.bbox(self.WIRE, with_xdg_output=False),
                         (0, 0, 960, 540))


class FakeSource:
    """A stand-in for layoutbox.MutterSource that counts being asked."""

    def __init__(self, box=None):
        self.box, self.asked = box, 0

    def bbox(self):
        self.asked += 1
        return self.box


class DisplayConfigSource(BboxCase):
    """The second source: org.gnome.Mutter.DisplayConfig, asked only when the
    wire is ambiguous (wdotool/layoutbox.py).

    The service here is the wire-level mock from tests/test_wxrandr_mutter.py
    -- the same GetCurrentState the wxrandr backend is tested against, with
    the layout mode and scale of the state being replayed -- so what is
    exercised is a real D-Bus call over a real socket, not a stubbed number.
    """

    #: what a HiDPI laptop advertises in both the right and the wrong state
    WIRE = [head(1920, 1080, 2, 0, 0, 1920, 1080)]

    def source_on(self, mock):
        """A MutterSource whose bus is that mock -- the one line of session discovery
        a test cannot do for itself, since the mock bus is not in a runtime dir."""
        src = layoutbox.MutterSource()
        self.addCleanup(src.close)
        src._connect = lambda: mutter.Bus(mock.address)
        return src

    def gnome(self, layout_mode, scale=2.0):
        """A GNOME on the bus with one 1920x1080 head at `scale`."""
        mock = twm.MutterMockBus()
        self.addCleanup(mock.close)
        mock.mutter = twm.FakeMutter(
            ["eDP-1"],
            [(0, 0, scale, 0, True, [("eDP-1", "1920x1080@60.020")])],
            layout_mode=layout_mode)
        return self.source_on(mock)

    def checked(self, heads, src, warn=lambda t, m: None):
        box, outs = self.detail(heads)
        return layoutbox.check(box, outs, source=src, warn=warn)

    def test_the_box_never_moves_however_loudly_they_disagree(self):
        """The pointer's box is the wire's, in every state: measured in the
        stale state itself, the wire's 1920x1080 lands all eight targets on
        the coordinate asked for and DisplayConfig's 960x540 doubles them.
        This is the assertion that keeps the obvious fix from being made
        again."""
        self.assertEqual(self.checked(self.WIRE, self.gnome(1)),
                         (0, 0, 1920, 1080))

    def test_the_disagreement_is_said_once_with_both_numbers_in_it(self):
        """...and it is said, because `getdisplaygeometry` is reporting the
        stale one and a script that works a coordinate out from it misses."""
        said = []
        src = self.gnome(1)
        for _ in range(3):
            self.checked(self.WIRE, src, warn=lambda t, m: said.append((t, m)))
        self.assertEqual(len(set(t for t, _m in said)), 1)   # one tag: the daemon says it once
        self.assertIn("1920x1080+0+0", said[0][1])
        self.assertIn("960x540+0+0", said[0][1])
        self.assertIn("getdisplaygeometry", said[0][1])
        self.assertIn("still lands", said[0][1])             # the pointer is not the problem

    def test_a_real_physical_mode_session_says_nothing(self):
        """The identical wire from 24.04's default (Fractional Scaling off).
        DisplayConfig is in physical layout mode and computes the same
        1920x1080, so the two agree and there is nothing to report -- raw
        pixels are that desktop's space, not a bug."""
        said = []
        self.assertEqual(
            self.checked(self.WIRE, self.gnome(2), warn=lambda t, m: said.append(m)),
            (0, 0, 1920, 1080))
        self.assertEqual(said, [])

    def test_no_gnome_on_the_bus_says_nothing(self):
        """KDE, sway and a GNOME that is not answering: the wire's box, and
        silence. There is no second opinion to have."""
        mock = twm.MutterMockBus()          # mock.mutter stays None: no DisplayConfig
        self.addCleanup(mock.close)
        src = self.source_on(mock)
        said = []
        self.assertEqual(
            self.checked(self.WIRE, src, warn=lambda t, m: said.append(m)),
            (0, 0, 1920, 1080))
        self.assertEqual(said, [])

    def test_a_source_that_throws_cannot_break_the_pointer(self):
        class Exploding:
            def bbox(self):
                raise RuntimeError("boom")

        said = []
        self.assertEqual(
            self.checked(self.WIRE, Exploding(), warn=lambda t, m: said.append(m)),
            (0, 0, 1920, 1080))
        self.assertEqual(said, [])

    def test_an_unambiguous_wire_is_never_asked_at_all(self):
        """THE GATE.  Every state in MeasuredStates but the stale one is
        unambiguous, so the common session -- scale 1, logical mode at any
        scale, KDE, sway -- never opens a bus and pays nothing for any of
        this."""
        counter = FakeSource((0, 0, 1, 1))
        for heads in ([head(1920, 1080, 1, 0, 0, 1920, 1080)],           # scale 1
                      [head(1920, 1080, 2, 0, 0, 960, 540)],             # 200% logical
                      [head(1920, 1080, 2, 0, 0, 1280, 720)],            # 150%
                      [head(1920, 1080, 1, 0, 0, 1920, 1080),
                       head(1920, 1080, 2, 1920, 0, 960, 540)]):         # mixed
            box, outs = self.detail(heads)
            self.assertFalse(layoutbox.wire_is_ambiguous(outs), heads)
            self.assertEqual(
                layoutbox.check(box, outs, source=counter, warn=lambda t, m: None), box)
        self.assertEqual(counter.asked, 0)

    def test_nothing_is_asked_when_nobody_is_listening(self):
        """No warn callback, no D-Bus call: this exists only to say something."""
        counter = FakeSource((0, 0, 960, 540))
        box, outs = self.detail(self.WIRE)
        self.assertEqual(layoutbox.check(box, outs, source=counter), box)
        self.assertEqual(counter.asked, 0)

    def test_the_rounding_rule_is_the_backends_own(self):
        """layoutbox does not import wxrandr -- the single-file wdotool zipapp
        bundles fwcommon and wdotool only, so a fix that needed wxrandr would
        work from the .deb and quietly not from the zipapp.  The price is a
        second copy of Mutter's logical-size rule, and this is what keeps the
        two from drifting: every size, scale, transform and layout mode, both
        implementations, same answer."""
        for px_w, px_h in ((1920, 1080), (2560, 1600), (1280, 1024), (3840, 2160)):
            for scale in (1.0, 1.25, 1.5, 1.75, 2.0, 3.0):
                for tf in range(8):
                    for mode in (layoutbox.LAYOUT_LOGICAL, layoutbox.LAYOUT_PHYSICAL):
                        self.assertEqual(
                            layoutbox._logical_size(px_w, px_h, tf, scale, mode),
                            mutter.logical_size(px_w, px_h,
                                                mutter.from_transform(tf), scale, mode),
                            (px_w, px_h, scale, tf, mode))

    def test_no_wxrandr_import_creeps_back_in(self):
        """The zipapp guard above, as a check rather than a comment."""
        with io.open(os.path.join(ROOT, "wdotool", "layoutbox.py"),
                     encoding="utf-8") as f:
            src = f.read()
        code = "\n".join(ln for ln in src.splitlines()
                          if not ln.lstrip().startswith("#"))
        self.assertNotIn("import wxrandr", code)
        self.assertNotIn("from wxrandr", code)

    def test_a_rotated_head_is_compared_in_the_orientation_it_is_drawn(self):
        """The signature is "logical size == raw mode size", and a head at
        90 degrees is drawn 1080x1920.  Comparing against the unrotated mode
        would call every rotated head ambiguous and ask on every command."""
        # the daemon's own per-head shape: mode in w/h, xdg logical in lw/lh
        rot = {"w": 1920, "h": 1080, "scale": 2, "transform": 1,
               "lw": 1080, "lh": 1920}
        self.assertTrue(layoutbox.wire_is_ambiguous([rot]))
        self.assertFalse(layoutbox.wire_is_ambiguous([dict(rot, lw=540, lh=960)]))
        # and unrotated, the same head is 1920x1080 drawn
        self.assertFalse(layoutbox.wire_is_ambiguous([dict(rot, transform=0)]))


class DivergedLandings(unittest.TestCase):
    """The raw readings from the stale state, re-read from the fixture.

    `tests/fixtures/scaling/noble-gnome-iso-DIVERGED.json` is one run of
    `repro/scale-probe.py` in that state: for each target, where `wdotool`
    was asked to put the cursor and where the KMS cursor plane then had it,
    in device pixels on the scanout.  This is the evidence that the wire's
    box is the right one for the pointer, kept as an assertion rather than a
    sentence, because the sentence is what the first pass got wrong: read as
    a fraction of the screen these landings look doubled, and read as a
    coordinate they are exact.
    """

    @classmethod
    def setUpClass(cls):
        with io.open(os.path.join(ROOT, "tests", "fixtures", "scaling",
                                  "noble-gnome-iso-DIVERGED.json"),
                     encoding="utf-8") as f:
            cls.readings = json.load(f)

    def plane(self, target):
        hw = list(target["hw"].values())
        return hw[0][:2] if hw else None

    def test_the_state_is_the_two_answers_disagreeing(self):
        """1920x1080 advertised, 960x540 drawn -- and `getdisplaygeometry`
        printing the one that is not there, which is the whole defect."""
        self.assertEqual(self.readings["daemon_bbox"], [0, 0, 1920, 1080])
        self.assertIn("current 960 x 540", self.readings["wxrandr_query"])
        self.assertEqual((self.readings["getdisplaygeometry"]["W"],
                          self.readings["getdisplaygeometry"]["H"]), ("1920", "1080"))

    def test_every_landing_is_the_coordinate_that_was_asked_for(self):
        """scale 2, so a target that lands on the desktop's own logical pixel
        is at device 2x -- and the residual is the cursor hotspot, constant
        across every target, which is what says the space is right rather
        than the fit being lucky."""
        res = set()
        for t in self.readings["targets"]:
            pos = self.plane(t)
            if pos is None or t["asked"][0] >= 960:     # off-screen, or the corner clamp
                continue
            res.add((2 * t["asked"][0] - pos[0], 2 * t["asked"][1] - pos[1]))
        self.assertGreaterEqual(len(self.readings["targets"]), 4)
        self.assertEqual(len(res), 1, res)              # one hotspot, every target exact
        self.assertLess(max(abs(v) for v in res.pop()), 64)   # ...and it is a cursor, 64x64

    def test_the_coordinates_that_look_legal_are_off_the_screen(self):
        """1200,700 and 1600,1000 are inside what `getdisplaygeometry`
        reports and outside the desktop that is being drawn: the cursor plane
        goes dark.  A script that works its target out from that number is
        what this costs."""
        for t in self.readings["targets"]:
            if t["asked"][0] > 1000:
                self.assertIsNone(self.plane(t), t["asked"])

    def test_the_compositor_is_consistent_with_the_layout_it_advertises(self):
        """Mutter's own pointer reading is in the advertised space too, so
        the stale rectangle is not just what it says -- it is what it uses."""
        for t in self.readings["targets"]:
            self.assertEqual(t["comp"], t["asked"])
            self.assertEqual(t["query"], t["asked"])


class DaemonGeometry(BboxCase):
    """The same thing through the daemon's own geometry(), which is what every
    absolute pointer command actually calls."""

    def daemon_box(self, heads, layout_mode):
        mock = twm.MutterMockBus()
        self.addCleanup(mock.close)
        mock.mutter = twm.FakeMutter(
            ["eDP-1"], [(0, 0, 2.0, 0, True, [("eDP-1", "1920x1080@60.020")])],
            layout_mode=layout_mode)
        old_source = layoutbox._SOURCE
        layoutbox._SOURCE = layoutbox.MutterSource()
        layoutbox._SOURCE._connect = lambda: mutter.Bus(mock.address)
        self.addCleanup(layoutbox._SOURCE.close)
        self.addCleanup(setattr, layoutbox, "_SOURCE", old_source)
        self.bbox(heads)                      # points the environment at the fake compositor
        d = daemon._Daemon()
        warnings = []
        return d.geometry(warnings), warnings

    def test_the_stale_state_reaches_the_pointer_untouched_but_reported(self):
        box, warnings = self.daemon_box(StaleXdgOutput.WIRE, 1)
        self.assertEqual(box, (0, 0, 1920, 1080))     # the pointer's space, measured
        self.assertEqual(len(warnings), 1)
        self.assertIn("DisplayConfig", warnings[0])

    def test_physical_mode_reaches_the_pointer_untouched_and_silent(self):
        box, warnings = self.daemon_box(StaleXdgOutput.WIRE, 2)
        self.assertEqual(box, (0, 0, 1920, 1080))
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
