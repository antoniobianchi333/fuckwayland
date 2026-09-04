"""warandr model tests: snapping, overlap rejection (exact mirrors excepted),
origin normalisation, the xrandr command line (arandr's shape, --off,
--same-as, --rate, --reflect, --scale), layout-script save/load round trips,
a genuine arandr-saved script, backend selection and the --save/--command
CLI against the fake xrandr.  No display needed."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)
sys.path.insert(0, FIXTURES)

import fake_xrandr                                                # noqa: E402
from warandr import cli, model, randr, xrandr_parse              # noqa: E402
from warandr.model import Layout, LayoutError, Mode, Output      # noqa: E402

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ARANDR_SAVED = os.path.join(FIXTURES, "arandr-saved.sh")
ARANDR_LINE = ("xrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 "
               "--rotate normal --output HDMI-1 --mode 1280x1024 --pos 1920x0 "
               "--rotate normal --output DP-2 --mode 1280x720 --pos 3200x0 "
               "--rotate normal --output HDMI-2 --off")


def fixture_layout(hidpi=False, verbose=True):
    """The fake xrandr's 3-output row (+ a disconnected HDMI-2), parsed."""
    text = fake_xrandr.render(fake_xrandr.DEFAULT, verbose)
    return Layout.from_screen(xrandr_parse.parse(text), hidpi=hidpi,
                              command_word="wxrandr" if hidpi else "xrandr")


def synthetic(hidpi=False):
    """A hand-built two-output layout for geometry tests."""
    lay = Layout(hidpi=hidpi, screen_max=(8192, 8192))
    a = lay.add(Output("A", modes=[Mode("1920x1080", 1920, 1080,
                                        [60.0, 50.0], 60.0)]))
    b = lay.add(Output("B", modes=[Mode("1280x1024", 1280, 1024,
                                        [60.02, 75.03], 60.02),
                                   Mode("1024x768", 1024, 768, [60.0])]))
    for o in (a, b):
        o.active = True
        o.mode = o.modes[0]
        o.rate = o.mode.default_rate()
    a.primary = True
    b.x = 1920
    return lay


class Geometry(unittest.TestCase):
    def test_size_rotation_swap(self):
        lay = synthetic()
        b = lay.get("B")
        self.assertEqual(b.size(), (1280, 1024))
        b.rotation = "left"
        self.assertEqual(b.size(), (1024, 1280))
        b.rotation = "inverted"
        self.assertEqual(b.size(), (1280, 1024))

    def test_size_scale_semantics(self):
        a = synthetic(hidpi=True).get("A")
        a.scale = 1.5
        self.assertEqual(a.size(), (1280, 720))      # compositor: px / scale
        a.scale = 1.75
        self.assertEqual(a.size(), (1097, 617))      # truncated like sway
        x = synthetic(hidpi=False).get("A")
        x.scale = 1.25
        self.assertEqual(x.size(), (2400, 1350))     # X11: fb * scale
        x.mode = Mode("1366x768", 1366, 768, [60.0])
        self.assertEqual(x.size(), (1708, 960))      # rounded outwards

    def test_bounding_box_and_normalize(self):
        lay = synthetic()
        self.assertEqual(lay.bounding_box(), (0, 0, 3200, 1080))
        lay.get("A").x, lay.get("A").y = -500, -20
        lay.get("B").x, lay.get("B").y = 1420, 300
        lay.normalize()
        self.assertEqual((lay.get("A").x, lay.get("A").y), (0, 0))
        self.assertEqual((lay.get("B").x, lay.get("B").y), (1920, 320))

    def test_normalize_ignores_inactive(self):
        lay = synthetic()
        lay.get("B").active = False
        lay.get("B").x = -999
        lay.get("A").x = 10
        lay.normalize()
        self.assertEqual(lay.get("A").x, 0)
        self.assertEqual(lay.get("B").x, -999)


class Snapping(unittest.TestCase):
    def test_snaps_to_right_edge(self):
        lay = synthetic()
        # B dragged near A's right edge (1920): within 40 -> 1920
        self.assertEqual(lay.snap("B", 1950, 12, 40), (1920, 0))
        self.assertEqual(lay.snap("B", 1885, -30, 40), (1920, 0))

    def test_no_snap_outside_tolerance(self):
        lay = synthetic()
        self.assertEqual(lay.snap("B", 2000, 100, 40), (2000, 100))

    def test_snaps_to_left_of_other(self):
        lay = synthetic()
        # A (1920 wide) dropped so its right edge nears B's left edge (1920)
        self.assertEqual(lay.snap("A", 10, 0, 40), (0, 0))
        lay.get("B").x = 3000
        self.assertEqual(lay.snap("A", 1070, 5, 40), (1080, 0))

    def test_snaps_bottom_and_centre(self):
        lay = synthetic()
        # B below A: top edge to A's bottom (1080), left edge to A's left
        self.assertEqual(lay.snap("B", 20, 1100, 40), (0, 1080))
        # centred under A: A centre 960 - B width/2 640 = 320
        self.assertEqual(lay.snap("B", 330, 1095, 40), (320, 1080))

    def test_nearest_candidate_wins(self):
        lay = synthetic()
        # candidates near x=1900: 1920 (A right edge) and 1920-1280=640 no;
        # add an output whose edge is 1890
        c = lay.add(Output("C", modes=[Mode("800x600", 800, 600, [60.0])]))
        c.active, c.mode, c.x, c.y = True, c.modes[0], 1090, 2000
        # C right edge = 1890; B dragged to 1902: 1890 is nearer than 1920
        self.assertEqual(lay.snap("B", 1902, 0, 40)[0], 1890)
        self.assertEqual(lay.snap("B", 1911, 0, 40)[0], 1920)

    def test_virtual_screen_origin_snaps(self):
        lay = synthetic()
        self.assertEqual(lay.snap("B", -30, 10, 40), (0, 0))
        # y=25: A's centre line (540 - 512 = 28) is nearer than the top (0)
        self.assertEqual(lay.snap("B", -30, 25, 40), (0, 28))

    def test_mirror_excluded_from_targets(self):
        lay = synthetic()
        lay.set_mirror("B", "A")
        # B mirrors A; dragging A must not snap against B's (identical) rect
        # in a way that differs from the screen origin
        self.assertEqual(lay.snap("A", 30, 30, 40), (0, 0))


class Edits(unittest.TestCase):
    def test_move_and_overlap_rejected(self):
        lay = synthetic()
        lay.move("B", 1950, 12)
        self.assertEqual((lay.get("B").x, lay.get("B").y), (1950, 12))
        with self.assertRaises(LayoutError) as cm:
            lay.move("B", 100, 100)
        self.assertIn("overlaps", str(cm.exception))
        # reverted
        self.assertEqual((lay.get("B").x, lay.get("B").y), (1950, 12))

    def test_move_normalizes(self):
        lay = synthetic()
        lay.move("B", -1280, 0)
        self.assertEqual((lay.get("B").x, lay.get("A").x), (0, 1280))

    def test_same_origin_is_a_clone(self):
        lay = synthetic()
        lay.get("B").mode = Mode("1920x1080", 1920, 1080, [60.0])
        lay.move("B", 0, 0)        # identical rect: a clone, legal
        lay.get("B").mode = lay.get("B").modes[0]
        lay.check()                # same origin, different size: still a
        lay.set_primary("B", True)  # clone (xrandr --same-as), editable
        with self.assertRaises(LayoutError):
            lay.move("B", 100, 0)  # any other intersection is an overlap

    def test_mirror_of(self):
        lay = synthetic()
        lay.set_mirror("B", "A")
        b = lay.get("B")
        self.assertEqual((b.x, b.y), (0, 0))
        self.assertIn("--same-as", lay.args())
        self.assertNotIn("1920x0", lay.args())
        with self.assertRaises(LayoutError):
            lay.move("B", 100, 0)  # mirrors are not moved directly
        lay.move("A", 300, 200)    # target moves, mirror follows, normalised
        self.assertEqual((b.x, b.y), (0, 0))
        lay.set_mirror("B", None)
        self.assertEqual((b.x, b.y), (1920, 0))   # parked right of A
        with self.assertRaises(LayoutError):
            lay.set_mirror("A", "A")

    def test_from_screen_marks_clones(self):
        # the screen already runs `DP-2 --same-as DP-1` (a projector), which
        # xrandr reports as two outputs at 0,0 of different sizes: warandr
        # reads that back as Mirror of, so the layout stays editable and the
        # relation round-trips as --same-as (arandr writes two --pos 0x0 and
        # allows the overlap)
        st = fake_xrandr.load_default()
        st["outputs"][2]["x"] = 0
        text = fake_xrandr.render(st, True)
        lay = Layout.from_screen(xrandr_parse.parse(text))
        dp2 = lay.get("DP-2")
        self.assertEqual(dp2.mirror_of, "DP-1")
        self.assertEqual((dp2.x, dp2.y), (0, 0))
        lay.set_primary("HDMI-1", True)            # was: "DP-1 overlaps DP-2"
        lay.set_rotation("DP-1", "left")           # the mirror follows
        self.assertEqual(dp2.mirror_of, "DP-1")
        args = lay.args()
        self.assertEqual(args[args.index("DP-2"):args.index("DP-2") + 5],
                         ["DP-2", "--mode", "1280x720", "--same-as", "DP-1"])
        fresh = Layout.from_screen(xrandr_parse.parse(text))
        fresh.load_script(lay.to_script())
        self.assertEqual(fresh.args(), lay.args())
        # three at one origin: both later ones mirror the first, no chain
        st["outputs"][1]["x"] = 0
        lay = Layout.from_screen(xrandr_parse.parse(
            fake_xrandr.render(st, True)))
        self.assertEqual([o.mirror_of for o in lay.outputs],
                         [None, "DP-1", "DP-1", None])
        lay.set_active("DP-1", False)
        self.assertEqual([o.mirror_of for o in lay.outputs],
                         [None, None, "HDMI-1", None])

    def test_chained_same_as_flattened(self):
        lay = synthetic()
        c = lay.add(Output("C", modes=[Mode("800x600", 800, 600, [60.0])]))
        c.active, c.mode, c.x = True, c.modes[0], 3200
        lay.load_script("#!/bin/sh\nxrandr --output A --mode 1920x1080 "
                        "--same-as B --output B --mode 1280x1024 --same-as C "
                        "--output C --mode 800x600 --pos 500x500\n")
        self.assertEqual([(o.name, o.mirror_of, o.x, o.y)
                          for o in lay.outputs],
                         [("A", "C", 0, 0), ("B", "C", 0, 0),
                          ("C", None, 0, 0)])
        self.assertEqual(lay.args().count("--same-as"), 2)
        with self.assertRaises(LayoutError) as cm:
            lay.load_script("#!/bin/sh\nxrandr --output A --same-as B "
                            "--output B --same-as A\n")
        self.assertIn("mirrors itself", str(cm.exception))
        with self.assertRaises(LayoutError):
            lay.load_script("#!/bin/sh\nxrandr --output A --same-as A\n")

    def test_mirror_chain_rejected_and_rebased(self):
        lay = synthetic()
        c = lay.add(Output("C", modes=[Mode("800x600", 800, 600, [60.0])]))
        c.active, c.mode, c.x = True, c.modes[0], 3200
        lay.set_mirror("B", "A")
        with self.assertRaises(LayoutError):
            lay.set_mirror("C", "B")          # B is itself a mirror
        lay.set_mirror("C", "A")
        lay.set_active("A", False)   # target off: the clone group survives
        self.assertIsNone(lay.get("B").mirror_of)
        self.assertEqual(lay.get("C").mirror_of, "B")
        self.assertEqual((lay.get("C").x, lay.get("C").y), (0, 0))

    def test_rotation_overlap_rejected(self):
        lay = synthetic()
        lay.move("B", 0, 1080)                # B below A, A 1920x1080
        lay.get("A").x = 0
        # rotating A to portrait (1080x1920) would run into B
        with self.assertRaises(LayoutError):
            lay.set_rotation("A", "left")
        self.assertEqual(lay.get("A").rotation, "normal")
        lay.move("B", 1920, 0)
        lay.set_rotation("A", "left")
        self.assertEqual(lay.get("A").size(), (1080, 1920))
        with self.assertRaises(LayoutError):
            lay.set_rotation("A", "sideways")

    def test_active_toggle(self):
        lay = synthetic()
        lay.set_active("B", False)
        self.assertEqual(lay.args()[-3:], ["--output", "B", "--off"])
        self.assertFalse(lay.get("B").primary)
        lay.set_active("B", True)
        b = lay.get("B")
        self.assertTrue(b.active)
        self.assertEqual((b.x, b.y), (1920, 0))   # right of the layout
        self.assertEqual(b.mode.name, "1280x1024")
        lay.set_active("A", False)
        lay.set_primary("B", True)
        self.assertEqual(lay.args()[:6],
                         ["--output", "A", "--off", "--output", "B",
                          "--primary"])

    def test_primary_exclusive(self):
        lay = synthetic()
        lay.set_primary("B", True)
        self.assertFalse(lay.get("A").primary)
        self.assertTrue(lay.get("B").primary)
        lay.set_primary("B", False)
        self.assertEqual([o.primary for o in lay.outputs], [False, False])

    def test_mode_and_rate(self):
        lay = synthetic()
        lay.set_mode("B", "1024x768")
        self.assertEqual(lay.get("B").size(), (1024, 768))
        self.assertEqual(lay.get("B").rate, 60.0)
        with self.assertRaises(LayoutError):
            lay.set_mode("B", "640x480")
        lay.set_mode("B", "1280x1024")
        lay.set_rate("B", 75.0)
        self.assertEqual(lay.get("B").rate, 75.03)
        self.assertIn("--rate", lay.args())
        self.assertEqual(lay.args()[lay.args().index("--rate") + 1], "75.03")
        lay.set_rate("B", 60.02)
        self.assertNotIn("--rate", lay.args())     # default rate: omitted

    def test_scale_and_reflection(self):
        lay = synthetic(hidpi=True)
        lay.set_scale("A", 2.0)
        self.assertEqual(lay.get("A").size(), (960, 540))
        self.assertIn("2x2", lay.args())
        with self.assertRaises(LayoutError):
            lay.set_scale("A", 0)
        lay.set_reflection("A", "xy")
        self.assertIn("xy", lay.args())
        with self.assertRaises(LayoutError):
            lay.set_reflection("A", "z")
        x11 = synthetic(hidpi=False)
        x11.get("B").x = 2880                      # room for 1920 * 1.5
        x11.set_scale("A", 1.5)
        self.assertEqual(x11.get("A").size(), (2880, 1620))
        # X11 too: what is drawn is what xrandr is told (it accepts --scale)
        self.assertIn("1.5x1.5", x11.args())
        self.assertEqual(x11.args()[x11.args().index("--scale") - 2:]
                         [:2], ["--rotate", "normal"])

    def test_scale_back_to_one_is_explicit(self):
        # xrandr and wxrandr keep an existing scale when --scale is absent:
        # an output read at scale 2 and set to 1 must say --scale 1x1
        lay = synthetic(hidpi=True)
        a = lay.get("A")
        a.scale = a.screen_scale = 2.0
        self.assertIn("2x2", lay.args())
        lay.set_scale("A", 1.0)
        self.assertIn("1x1", lay.args())
        lay.get("B").screen_scale = 1.0
        self.assertEqual(lay.args().count("--scale"), 1)
        # an untouched, unscaled screen still says nothing (arandr's line)
        self.assertNotIn("--scale", fixture_layout().args())

    def test_x11_scale_script_round_trip(self):
        lay = fixture_layout(hidpi=False)
        lay.load_script("#!/bin/sh\nxrandr --output DP-1 --primary --mode "
                        "1920x1080 --pos 0x0 --rotate normal --scale 2x2 "
                        "--output HDMI-1 --mode 1280x1024 --pos 3840x0 "
                        "--rotate normal --output DP-2 --off\n")
        self.assertEqual(lay.get("DP-1").size(), (3840, 2160))
        self.assertEqual(lay.get("HDMI-1").x, 3840)
        self.assertIn("--scale", lay.args())      # the gap is real on screen
        self.assertEqual(lay.args()[lay.args().index("--scale") + 1], "2x2")

    def test_screen_bound(self):
        lay = synthetic()
        lay.screen_max = (4000, 4000)
        with self.assertRaises(LayoutError) as cm:
            lay.move("B", 3000, 0)
        self.assertIn("outside the virtual screen", str(cm.exception))


class CommandLine(unittest.TestCase):
    def test_matches_arandr_exactly(self):
        # the genuine arandr 0.1.11 save of the same fake screen
        self.assertEqual(fixture_layout().command_line(), ARANDR_LINE)
        self.assertEqual(fixture_layout(verbose=False).command_line(),
                         ARANDR_LINE)

    def test_wayland_word(self):
        lay = fixture_layout(hidpi=True)
        self.assertTrue(lay.command_line().startswith("wxrandr --output"))
        self.assertEqual(lay.command_line("xrandr"), ARANDR_LINE)

    def test_all_features(self):
        lay = fixture_layout(hidpi=True)
        lay.set_rotation("HDMI-1", "left")
        lay.set_rate("DP-1", 50)
        lay.set_scale("DP-2", 1.5)
        lay.set_reflection("DP-2", "y")
        lay.set_mirror("DP-2", "DP-1")
        self.assertEqual(lay.args(), [
            "--output", "DP-1", "--primary", "--mode", "1920x1080",
            "--rate", "50", "--pos", "0x0", "--rotate", "normal",
            "--output", "HDMI-1", "--mode", "1280x1024", "--pos", "1920x0",
            "--rotate", "left",
            "--output", "DP-2", "--mode", "1280x720", "--same-as", "DP-1",
            "--rotate", "normal", "--reflect", "y", "--scale", "1.5x1.5",
            "--output", "HDMI-2", "--off"])

    def test_quoting(self):
        lay = synthetic()
        lay.get("A").modes.append(Mode("odd name", 640, 480, [60.0]))
        lay.set_mode("A", "odd name")
        self.assertIn("--mode 'odd name'", lay.command_line())


class Scripts(unittest.TestCase):
    def test_default_script_is_arandrs(self):
        # a fresh save is byte-identical to what arandr 0.1.11 writes for
        # the same screen: shebang, the one line, nothing else
        with open(ARANDR_SAVED) as f:
            arandr = f.read()
        self.assertEqual(fixture_layout().to_script(), arandr)
        self.assertEqual(fixture_layout().to_script(),
                         "#!/bin/sh\n" + ARANDR_LINE + "\n")
        self.assertEqual(model.DEFAULT_TEMPLATE, ["#!/bin/sh", "%(xrandr)s"])

    def test_round_trip(self):
        lay = fixture_layout(hidpi=True)
        lay.set_rotation("HDMI-1", "left")
        lay.set_rate("DP-1", 50)
        lay.set_scale("DP-2", 1.5)
        lay.move("DP-2", 2944, 0)
        text = lay.to_script()
        fresh = fixture_layout(hidpi=True)
        fresh.load_script(text)
        self.assertEqual(fresh.args(), lay.args())
        self.assertEqual(fresh.get("DP-1").rate, 50.0)
        self.assertEqual(fresh.get("DP-2").scale, 1.5)
        self.assertEqual(fresh.get("HDMI-1").size(), (1024, 1280))
        self.assertEqual(fresh.to_script(), text)

    def test_round_trip_mirror(self):
        lay = fixture_layout()
        lay.set_mirror("DP-2", "DP-1")
        fresh = fixture_layout()
        fresh.load_script(lay.to_script())
        self.assertEqual(fresh.get("DP-2").mirror_of, "DP-1")
        self.assertEqual((fresh.get("DP-2").x, fresh.get("DP-2").y), (0, 0))
        self.assertEqual(fresh.args(), lay.args())

    def test_load_genuine_arandr_script(self):
        with open(ARANDR_SAVED) as f:
            text = f.read()
        self.assertEqual(text, "#!/bin/sh\n" + ARANDR_LINE + "\n")
        lay = fixture_layout()
        lay.set_rotation("HDMI-1", "inverted")   # dirty the layout first
        lay.set_active("DP-2", False)
        template = lay.load_script(text)
        self.assertEqual(template, ["#!/bin/sh", "%(xrandr)s"])
        self.assertEqual(lay.command_line(), ARANDR_LINE)
        # saving back is byte-identical to arandr's file
        self.assertEqual(lay.to_script(), text)

    def test_template_preserved(self):
        text = ("#!/bin/sh\n# my layout\n" + ARANDR_LINE +
                "\nnotify-send done\n")
        lay = fixture_layout()
        lay.load_script(text)
        lay.set_rotation("DP-2", "right")
        out = lay.to_script()
        self.assertTrue(out.startswith("#!/bin/sh\n# my layout\nxrandr "))
        self.assertTrue(out.endswith("--rotate right --output HDMI-2 --off\n"
                                     "notify-send done\n"))

    def test_wxrandr_line_accepted_and_relations(self):
        lay = fixture_layout()
        lay.load_script("#!/bin/sh\nwxrandr --output DP-2 --off --output "
                        "HDMI-1 --right-of DP-1 --rotate left --output DP-1 "
                        "--mode 1280x720 --pos 0x0\n")
        self.assertFalse(lay.get("DP-2").active)
        self.assertEqual(lay.get("DP-1").size(), (1280, 720))
        self.assertEqual((lay.get("HDMI-1").x, lay.get("HDMI-1").y),
                         (1280, 0))
        self.assertEqual(lay.get("HDMI-1").rotation, "left")
        self.assertFalse(lay.get("DP-1").primary)  # arandr: reset unless set

    def test_partial_script_moves_primary(self):
        # `xrandr --output HDMI-1 --primary` on a screen whose primary is
        # DP-1 just moves it; the old flag must not survive as a second one
        lay = fixture_layout()
        self.assertTrue(lay.get("DP-1").primary)
        lay.load_script("#!/bin/sh\nxrandr --output HDMI-1 --primary\n")
        self.assertEqual([o.name for o in lay.outputs if o.primary],
                         ["HDMI-1"])
        # without --primary anywhere, an unmentioned primary stays
        lay = fixture_layout()
        lay.load_script("#!/bin/sh\nxrandr --output DP-2 --off\n")
        self.assertTrue(lay.get("DP-1").primary)

    def test_load_errors(self):
        lay = fixture_layout()
        before = lay.args()
        cases = {
            "echo hi\n": "Not a shell script.",
            "#!/bin/sh\necho hi\n": "No recognized xrandr command",
            "#!/bin/sh\nxrandr --output DP-1 --off\nxrandr --output DP-2 "
            "--off\n": "More than one xrandr line",
            "#!/bin/sh\nxrandr --output NOPE --off\n": "Not a known output",
            "#!/bin/sh\nxrandr --output DP-1 --mode 640x480\n":
                "Not a known mode",
            "#!/bin/sh\nxrandr --output DP-1 --same-as ZZZ\n":
                "Not a known output",
            "#!/bin/sh\nxrandr --output DP-1 --brightness 0.5\n":
                "Unsupported option",
            "#!/bin/sh\nxrandr --output DP-1 --pos 0x0 --output HDMI-1 "
            "--pos 100x0\n": "overlaps",
            "#!/bin/sh\nxrandr --output DP-1 --primary --output HDMI-1 "
            "--primary\n": "More than one primary",
            "#!/bin/sh\nxrandr --pos 0x0\n": "must be used after --output",
        }
        for text, msg in cases.items():
            with self.assertRaises(LayoutError, msg=text) as cm:
                lay.load_script(text)
            self.assertIn(msg, str(cm.exception))
            self.assertEqual(lay.args(), before)   # untouched on error


class BackendChoice(unittest.TestCase):
    def test_override(self):
        b = randr.choose({"WARANDR_XRANDR": "python3 /x/fake_xrandr.py"})
        self.assertEqual(b.argv, ["python3", "/x/fake_xrandr.py"])
        self.assertFalse(b.wayland)
        self.assertEqual(b.word, "xrandr")
        b = randr.choose({"WARANDR_XRANDR": "/opt/bin/wxrandr --nograb"})
        self.assertTrue(b.wayland)
        self.assertEqual(b.word, "wxrandr")
        b = randr.choose({"WARANDR_XRANDR": "/x/fake.py",
                          "WARANDR_BACKEND": "wayland"})
        self.assertTrue(b.wayland)

    def wayland_env(self, **extra):
        """A Wayland session as passthrough.session_kind() sees one: the
        variable *and* a live socket (a leftover WAYLAND_DISPLAY with no
        compositor behind it is an X11 session, see test_x11)."""
        rd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, rd, ignore_errors=True)
        open(os.path.join(rd, "wayland-1"), "w").close()
        env = {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": rd,
               "PATH": "/nonexist"}
        env.update(extra)
        return env

    def test_wayland_same_interpreter(self):
        b = randr.choose(self.wayland_env())
        self.assertEqual(b.argv, [sys.executable, "-m", "wxrandr"])
        self.assertTrue(b.wayland)
        self.assertEqual(b.env["PYTHONPATH"].split(os.pathsep)[0], ROOT)
        self.assertEqual(b.env["WAYLAND_DISPLAY"], "wayland-1")

    def test_x11(self):
        b = randr.choose({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"})
        self.assertEqual(b.argv, ["xrandr"])
        self.assertFalse(b.wayland)
        b.set_display("localhost:10.0")
        self.assertEqual(b.env["DISPLAY"], "localhost:10.0")
        w = randr.choose(self.wayland_env())
        self.assertTrue(w.wayland)
        w.set_display("wayland-9")
        self.assertEqual(w.env["WAYLAND_DISPLAY"], "wayland-9")

    def test_stale_wayland_display_is_x11(self):
        """New with the passthrough: WAYLAND_DISPLAY without a socket used to
        select wxrandr, and every Apply then said "Can't open display"."""
        b = randr.choose({"WAYLAND_DISPLAY": "wayland-1", "PATH": "/nonexist",
                          "XDG_SESSION_TYPE": "x11"})
        self.assertFalse(b.wayland)
        self.assertEqual(b.argv, ["xrandr"])
        self.assertEqual(b.word, "xrandr")
        # ...and the explicit overrides still win
        b = randr.choose({"WARANDR_BACKEND": "wayland", "XDG_SESSION_TYPE": "x11",
                          "PATH": "/nonexist"})
        self.assertTrue(b.wayland)

    def test_the_passthrough_escape_hatch_is_not_a_session_type(self):
        """FUCKWAYLAND_PASSTHROUGH=never means "run our own code instead of
        handing over to the original" — and warandr never hands over. Read as
        a session type it would select wxrandr on an X11 box (rc 1, "Can't
        open display" on every Apply) for anyone who exported it, which the
        README suggests and the whole test suite does."""
        for value in ("never", "always"):
            b = randr.choose({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0",
                              "PATH": "/nonexist",
                              "FUCKWAYLAND_PASSTHROUGH": value})
            self.assertEqual(b.argv, ["xrandr"], value)
            self.assertFalse(b.wayland, value)
        w = randr.choose(self.wayland_env(FUCKWAYLAND_PASSTHROUGH="never"))
        self.assertTrue(w.wayland)

    def test_snapshot_through_fake(self):
        env = dict(os.environ)
        env["WARANDR_XRANDR"] = "%s %s" % (
            sys.executable, os.path.join(FIXTURES, "fake_xrandr.py"))
        env["FAKE_XRANDR_STATE"] = os.path.join(tempfile.mkdtemp(), "s.json")
        b = randr.choose(env)
        lay = b.snapshot()
        self.assertEqual(lay.command_line(), ARANDR_LINE)
        self.assertEqual(lay.screen_max, (32767, 32767))
        bad = randr.Backend(["/nonexistent/xrandr"], False)
        with self.assertRaises(randr.RandrError):
            bad.snapshot()


class ForcedBackend(unittest.TestCase):
    """`warandr --backend NAME` / the GUI's Layout ▸ Backend.  It beats
    everything (and, inside wxrandr, $WXRANDR_BACKEND and detection), and it
    never falls back silently to something else."""

    def fake_env(self, **extra):
        env = {"PATH": os.environ.get("PATH", ""),
               "WARANDR_XRANDR": "%s %s" % (sys.executable,
                                            os.path.join(FIXTURES,
                                                         "fake_xrandr.py")),
               "FAKE_XRANDR_STATE": os.path.join(tempfile.mkdtemp(),
                                                 "s.json")}
        env.update(extra)
        return env

    def test_x11_runs_the_real_xrandr(self):
        b = randr.choose({"WAYLAND_DISPLAY": "wayland-1"}, forced="x11")
        self.assertEqual(b.argv, ["xrandr"])
        self.assertFalse(b.wayland)
        self.assertEqual((b.name, b.forced, b.word, b.run_word),
                         ("x11", "x11", "xrandr", "xrandr"))
        self.assertEqual(b.indicator(), "backend: xrandr (X11)")
        self.assertEqual(b.script_note(),
                         "warandr: backend x11 forced (xrandr)")

    def test_a_wayland_backend_runs_wxrandr_with_the_flag(self):
        for spelling, name in (("mutter", "mutter"), ("gnome", "mutter"),
                               ("kde", "kwin"), ("sway", "sway"),
                               ("wlr", "wlr")):
            b = randr.choose({"PATH": "/nonexist"}, forced=spelling)
            self.assertEqual(b.argv, [sys.executable, "-m", "wxrandr",
                                      "--backend", name], spelling)
            self.assertTrue(b.wayland, spelling)
            self.assertEqual((b.name, b.forced, b.word), (name, name,
                                                          "wxrandr"))
            self.assertEqual(b.run_word, "wxrandr --backend " + name)
            self.assertEqual(b.indicator(),
                             "backend: %s (Wayland)" % name)
            self.assertEqual(b.env["PYTHONPATH"].split(os.pathsep)[0], ROOT)

    def test_auto_is_the_default_and_changes_nothing(self):
        env = {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11", "PATH": "/nonexist"}
        plain, auto = randr.choose(env), randr.choose(env, forced="auto")
        self.assertEqual((auto.argv, auto.wayland, auto.forced),
                         (plain.argv, plain.wayland, None))
        self.assertIsNone(auto.script_note())

    def test_forcing_a_wayland_backend_without_wxrandr_is_an_error(self):
        orig = randr._package_root
        randr._package_root = lambda name: None
        try:
            with self.assertRaises(randr.RandrError) as cm:
                randr.choose({"PATH": "/nonexist"}, forced="mutter")
        finally:
            randr._package_root = orig
        self.assertIn("needs wxrandr", str(cm.exception))
        self.assertNotIn("xrandr --", str(cm.exception))   # no silent fallback

    def test_an_unknown_name_lists_the_valid_ones(self):
        with self.assertRaises(randr.RandrError) as cm:
            randr.choose({}, forced="banana")
        self.assertEqual(str(cm.exception), "unknown backend 'banana' "
                         "(valid: auto, x11, sway, wlr, mutter, kwin)")

    def test_the_override_is_still_the_command_that_runs(self):
        env = self.fake_env()
        b = randr.choose(env, forced="mutter")
        self.assertEqual(b.argv[-2:], ["--backend", "mutter"])
        self.assertTrue(b.wayland)
        self.assertEqual(b.snapshot().command_line(), ARANDR_LINE.replace(
            "xrandr ", "wxrandr ", 1))
        b = randr.choose(env, forced="x11")
        self.assertFalse(b.wayland)
        self.assertNotIn("--backend", b.argv)

    def test_identify_asks_the_runner_which_backend_it_is(self):
        b = randr.choose(self.fake_env(), forced="mutter").identify()
        self.assertEqual(b.name, "mutter")
        self.assertEqual(b.info[0], "mutter")
        detail = b.detail()
        self.assertTrue(detail.startswith("backend: mutter (Wayland)\n"),
                        detail)
        self.assertIn("chosen by: --backend mutter", detail)
        self.assertIn("compositor: Mutter (fake)", detail)
        self.assertNotIn("\nmutter\n", detail)   # the token is not repeated
        # the X11 runner is the real xrandr and knows no such option: the
        # answer is composed here, without running anything
        b = randr.choose({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
                          "PATH": "/nonexist"}).identify()
        self.assertEqual(b.name, "x11")
        self.assertIn("backend: xrandr (X11)", b.detail())
        self.assertIn("runs: xrandr", b.detail())

    def test_the_explanation_answers_each_question_once(self):
        """detail() is the indicator's tooltip, the About paragraph and the
        Script Properties page.  warandr says why it picked this backend and
        wxrandr says why *it* did -- but warandr is the one who passed
        `--backend`, so the inner answer only restates the outer one, and
        two `chosen by:` lines in one paragraph contradict each other for a
        living."""
        b = randr.choose(self.fake_env(), forced="mutter").identify()
        lines = b.detail().splitlines()
        self.assertEqual([ln for ln in lines if ln.startswith("chosen by:")],
                         ["chosen by: --backend mutter ($WARANDR_XRANDR)"])
        keys = [ln.split(":", 1)[0] for ln in lines]
        self.assertEqual(sorted(keys), sorted(set(keys)), lines)
        self.assertEqual(keys[0], "backend")
        self.assertIn("compositor: Mutter (fake)", lines)

    def test_report_is_the_token_then_the_explanation(self):
        """`warandr --print-backend --verbose`, spelled like wxrandr's own:
        a bare token first, for scripts."""
        b = randr.choose(self.fake_env(), forced="mutter").identify()
        rep = b.report()
        self.assertEqual(rep[:3], ["mutter", "kind: Wayland",
                                   "runs: " + b.command()])
        self.assertIn("chosen by: --backend mutter ($WARANDR_XRANDR)", rep)
        self.assertIn("available: yes", rep)
        self.assertEqual(len([ln for ln in rep
                              if ln.startswith("chosen by:")]), 1)
        # the X11 runner knows no such option; its answer is composed here
        x = randr.choose({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11",
                          "PATH": "/nonexist"}).identify()
        self.assertEqual(x.report()[:2], ["x11", "kind: X11"])

    def test_identify_survives_a_runner_that_knows_nothing(self):
        b = randr.Backend(["/nonexistent/wxrandr"], True)
        self.assertEqual(b.identify().name, "wayland")
        self.assertEqual(b.indicator(), "backend: wxrandr (Wayland)")

    def test_probe_backends_reads_wxrandrs_table(self):
        info = randr.probe_backends(self.fake_env())
        self.assertEqual({k: v["available"] for k, v in info.items()},
                         {"sway": False, "kwin": False, "mutter": True,
                          "wlr": False, "x11": True})
        self.assertEqual(info["sway"]["reason"],
                         "no sway or i3 IPC socket ($SWAYSOCK)")
        self.assertTrue(info["x11"]["auto"])
        self.assertFalse(info["mutter"]["auto"])

    def test_probe_backends_without_wxrandr_says_so(self):
        orig = randr._package_root
        randr._package_root = lambda name: None
        try:
            info = randr.probe_backends({"PATH": "/nonexist"})
        finally:
            randr._package_root = orig
        self.assertFalse(any(info[n]["available"]
                             for n in randr.WAYLAND_BACKENDS))
        self.assertEqual(info["mutter"]["reason"], "wxrandr is not installed")
        self.assertIn("x11", info)

    def test_warandr_never_hands_its_process_over(self):
        """It *chooses* which tool to run and runs it as a child -- which is
        what makes the choice switchable while the window is open."""
        for name in sorted(os.listdir(os.path.join(ROOT, "warandr"))):
            if not name.endswith(".py"):
                continue
            # explicit encoding: these sources hold non-ASCII, and the
            # default is whatever LC_CTYPE happens to say (LANG=C -> ASCII)
            with open(os.path.join(ROOT, "warandr", name),
                      encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("maybe_exec_real", src, name)
            self.assertNotIn("os.exec", src, name)

    def test_the_saved_script_records_a_forced_backend_as_a_comment(self):
        """A layout script is arandr's and must stay runnable by `sh` on a
        plain X11 box, so the note is a comment -- never an option."""
        lay = fixture_layout(hidpi=True)
        note = "warandr: backend mutter forced (wxrandr --backend mutter)"
        text = lay.to_script("wxrandr", note)
        lines = text.rstrip("\n").split("\n")
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertEqual(lines[1], "# " + note)
        self.assertTrue(lines[2].startswith("wxrandr --output "))
        self.assertNotIn("--backend", lines[2])
        # nothing forced: arandr's own two lines, byte for byte
        self.assertEqual(lay.to_script("wxrandr"),
                         "#!/bin/sh\n" + lines[2] + "\n")
        # it loads back (the comment is not a second command line) and the
        # file's own template wins on the way out: no second note
        again = fixture_layout(hidpi=True)
        again.load_script(text)
        self.assertEqual(again.to_script("wxrandr", note), text)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = dict(os.environ)
        self.env["WARANDR_XRANDR"] = "%s %s" % (
            sys.executable, os.path.join(FIXTURES, "fake_xrandr.py"))
        self.env["FAKE_XRANDR_STATE"] = os.path.join(self.tmp, "state.json")
        self.env["FAKE_XRANDR_LOG"] = os.path.join(self.tmp, "log")
        self.env["PYTHONPATH"] = ROOT

    def run_cli(self, *args):
        p = subprocess.run([sys.executable, "-m", "warandr"] + list(args),
                           env=self.env, capture_output=True, text=True,
                           timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_command(self):
        rc, out, err = self.run_cli("--command")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.strip(), ARANDR_LINE)

    def test_save_and_reopen(self):
        target = os.path.join(self.tmp, "layout")
        rc, out, err = self.run_cli("--save", target)
        self.assertEqual((rc, err), (0, ""))
        path = target + ".sh"                     # arandr appends .sh
        self.assertTrue(os.path.exists(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        with open(path) as f:
            text = f.read()
        self.assertEqual(text, "#!/bin/sh\n" + ARANDR_LINE + "\n")
        # a saved file re-based on the current outputs
        with open(path, "w") as f:
            f.write("#!/bin/sh\nxrandr --output DP-2 --off\n")
        rc, out, err = self.run_cli("--command", path)
        self.assertEqual(rc, 0, err)
        self.assertIn("--output DP-2 --off", out)
        self.assertNotIn("--output DP-2 --mode", out)

    def test_errors(self):
        rc, out, err = self.run_cli("--command", os.path.join(self.tmp, "nope"))
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("warandr: "))
        self.env["WARANDR_XRANDR"] = "/nonexistent/xrandr"
        rc, out, err = self.run_cli("--command")
        self.assertEqual(rc, 1)
        self.assertIn("cannot run", err)

    def test_version_and_help(self):
        rc, out, err = self.run_cli("--version")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("warandr "))
        rc, out, err = self.run_cli("--help")
        self.assertEqual(rc, 0)
        for opt in ("--randr-display", "--force-version", "--save",
                    "[savedfile]"):
            self.assertIn(opt, out)

    def test_gtk_hint_shape(self):
        self.assertIn("apt install python3-gi gir1.2-gtk-3.0", cli.GTK_HINT)


class BuildScript(unittest.TestCase):
    """scripts/build-pyz.sh emits dist/warandr, and the zipapp runs the
    non-GUI paths (a temp copy, so parallel runs cannot race on dist/)."""

    def test_pyz(self):
        import shutil
        with tempfile.TemporaryDirectory(prefix="warandr-build-") as tmp:
            for d in ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr",
                      "scripts"):
                shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d),
                                ignore=shutil.ignore_patterns("__pycache__"))
            p = subprocess.run(["sh", os.path.join(tmp, "scripts",
                                                   "build-pyz.sh")],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            pyz = os.path.join(tmp, "dist", "warandr")
            self.assertTrue(os.path.exists(pyz))
            env = dict(os.environ)
            env["WARANDR_XRANDR"] = "%s %s" % (
                sys.executable, os.path.join(FIXTURES, "fake_xrandr.py"))
            env["FAKE_XRANDR_STATE"] = os.path.join(tmp, "state.json")
            env.pop("PYTHONPATH", None)
            p = subprocess.run([sys.executable, pyz, "--version"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual((p.returncode, p.stdout), (0, "warandr 0.1.0\n"))
            p = subprocess.run([sys.executable, pyz, "--command"], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual((p.returncode, p.stdout.strip()),
                             (0, ARANDR_LINE), p.stderr)
            # the Wayland path resolves `-m wxrandr` from inside the pyz:
            # without a compositor that is wxrandr's own error, not ours.
            # The socket has to exist -- passthrough.session_kind() does not
            # believe a WAYLAND_DISPLAY with nothing behind it.
            open(os.path.join(tmp, "wayland-nope"), "w").close()
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp,
                   "WAYLAND_DISPLAY": "wayland-nope",
                   "XDG_RUNTIME_DIR": tmp}
            p = subprocess.run([sys.executable, pyz, "--command"], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 1)
            self.assertIn("wxrandr failed", p.stderr)
            self.assertIn("wayland-nope", p.stderr)


if __name__ == "__main__":
    unittest.main()
