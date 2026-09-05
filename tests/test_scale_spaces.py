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

The last case is the one that does not work, and it is left failing on
purpose: see StaleXdgOutput.
"""

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

import wl_fake                                            # noqa: E402
from wdotool import daemon                                # noqa: E402


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

    def bbox(self, heads, with_xdg_output=True):
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
        return daemon._wayland_bbox()


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

    THE WIRE CANNOT TELL THIS APART from test_gnome46_scale2_physical: the
    three values above are byte for byte what a legitimate physical-layout
    session sends, and there the same box is correct.  So this is not a
    one-line fix to _wayland_bbox() -- distinguishing them needs a second
    source, and the only one that was right in every state measured is
    Mutter's own DisplayConfig, which `wxrandr/mutter.py` already speaks.

    Left as an expected failure: whoever fixes it should delete the
    decorator, and until then the suite records that we know.
    """

    @unittest.expectedFailure
    def test_diverged_layout_mode_is_not_believed(self):
        self.assertEqual(self.bbox([head(1920, 1080, 2, 0, 0, 1920, 1080)]),
                         (0, 0, 960, 540))


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


if __name__ == "__main__":
    unittest.main()
