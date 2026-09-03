"""warandr model tests: snapping, overlap rejection (exact mirrors excepted),
origin normalisation, the xrandr command line (arandr's shape, --off,
--same-as, --rate, --reflect, --scale), layout-script save/load round trips,
a genuine arandr-saved script, backend selection and the --save/--command
CLI against the fake xrandr.  No display needed."""

import os
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

    def test_exact_mirror_overlap_allowed(self):
        lay = synthetic()
        lay.get("B").mode = Mode("1920x1080", 1920, 1080, [60.0])
        lay.move("B", 0, 0)        # identical rect: a clone, legal
        lay.get("B").mode = lay.get("B").modes[0]
        with self.assertRaises(LayoutError):
            lay.check()            # same origin, different size: overlap

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
        self.assertNotIn("--scale", x11.args())    # never emitted on X11

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
    def test_default_script(self):
        text = fixture_layout().to_script()
        lines = text.split("\n")
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertTrue(lines[1].startswith("# "))
        self.assertEqual(lines[2], ARANDR_LINE)
        self.assertEqual(lines[3], "")
        self.assertEqual(len(lines), 4)

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
            "--pos 0x0\n": "overlaps",
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

    def test_wayland_same_interpreter(self):
        b = randr.choose({"WAYLAND_DISPLAY": "wayland-1", "PATH": "/nonexist"})
        self.assertEqual(b.argv, [sys.executable, "-m", "wxrandr"])
        self.assertTrue(b.wayland)
        self.assertEqual(b.env["PYTHONPATH"].split(os.pathsep)[0], ROOT)
        self.assertEqual(b.env["WAYLAND_DISPLAY"], "wayland-1")

    def test_x11(self):
        b = randr.choose({"DISPLAY": ":0"})
        self.assertEqual(b.argv, ["xrandr"])
        self.assertFalse(b.wayland)
        b.set_display("localhost:10.0")
        self.assertEqual(b.env["DISPLAY"], "localhost:10.0")
        w = randr.choose({"WAYLAND_DISPLAY": "wayland-0"})
        w.set_display("wayland-9")
        self.assertEqual(w.env["WAYLAND_DISPLAY"], "wayland-9")

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
        self.assertEqual(text.split("\n")[0], "#!/bin/sh")
        self.assertIn(ARANDR_LINE, text)
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
            # without a compositor that is wxrandr's own error, not ours
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
