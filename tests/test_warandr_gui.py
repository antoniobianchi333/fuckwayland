"""warandr GUI test: the real GTK 3 editor under Xvfb, driven with xdotool.

Skipped unless GTK 3 is importable and Xvfb + xdotool are on PATH.  The
editor talks to tests/fixtures/fake_xrandr.py (WARANDR_XRANDR), a 3-output
RandR simulator that records every apply.  The editor's test hook
WARANDR_TEST_LAYOUT_DUMP writes root-window coordinates of the output boxes,
toolbar buttons and popup-menu items (and every status-bar change), so the
test clicks real pixels:

  hover HDMI-1 -> its description in the status bar; leave -> the command
  right-click HDMI-1 -> arandr's menu order, no Scale on X11
                     -> Orientation -> left
  drag DP-2 onto DP-1 -> refused, snapped back, "not moved" in the status bar
  drag DP-2 next to the (now portrait) HDMI-1; it snaps to its right edge
  Apply -> the recorded argv is exactly the arandr-shaped command
  Save As (WARANDR_TEST_SAVE_AS) -> arandr's two-line layout script
  a failing apply shows the error dialog, keeps the edits, editor survives
  the status bar's backend indicator names the live backend at all times
  Layout ▸ Backend -> radios, the unreachable ones insensitive with the
                      reason in their tooltip
                   -> GNOME (mutter): the layout is re-read *through* it,
                      the canvas redrawn, and Apply, the command in the
                      status bar and a saved script are that backend's
  a backend that cannot be reached -> the dialog, and the previous one back

Every popup dump also carries the *modelled* item positions — what the
editor computes from where GTK asked for the popup (at the pointer, right
of the parent item, below the menubar item), which is all a Wayland driver
gets — and X11, where GDK knows the truth, checks the model against it for
the context menu, its Orientation submenu, the menubar's Outputs drop-down
and a per-output submenu of that.

tests/fixtures/gui_probe.py runs in-process checks under the same Xvfb
(Apply on a worker thread, popup release, zoom radios, menu shapes, layout
dumps that wait for GTK's allocation, the Save As PATH hint), and a run
without any display must end in one line, not a traceback.

Set WARANDR_TEST_SHOTS=DIR to keep `import -window root` screenshots."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)


def _gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        return True
    except Exception:
        return False


HAVE_GTK = _gtk_available()
HAVE_X = bool(shutil.which("Xvfb") and shutil.which("xdotool"))
SHOTS = os.environ.get("WARANDR_TEST_SHOTS")

EXPECTED = ["--output", "DP-1", "--primary", "--mode", "1920x1080",
            "--pos", "0x0", "--rotate", "normal",
            "--output", "HDMI-1", "--mode", "1280x1024", "--pos", "1920x0",
            "--rotate", "left",
            "--output", "DP-2", "--mode", "1280x720", "--pos", "2944x0",
            "--rotate", "normal",
            "--output", "HDMI-2", "--off"]
ARANDR_MENU = ["Active", "Primary", "Resolution", "Orientation",
               "Refresh rate", "Reflection", "Mirror of"]
BACKEND_MENU = ["Automatic", "X11 (xrandr)", "sway", "wlroots (wlr)",
                "GNOME (mutter)", "KDE (kwin)"]
# what tests/fixtures/fake_xrandr.py simulates for `--backends`
FAKE_AVAILABLE = {"sway": False, "kwin": False, "mutter": True,
                  "wlr": False, "x11": True}


def _base_env(tmp, display):
    env = dict(os.environ)
    env.update({
        "DISPLAY": display, "GDK_BACKEND": "x11",
        "NO_AT_BRIDGE": "1", "GSETTINGS_BACKEND": "memory",
        "HOME": tmp, "PYTHONPATH": ROOT,
        "WARANDR_XRANDR": "%s %s" % (sys.executable,
                                     os.path.join(FIXTURES, "fake_xrandr.py")),
        "FAKE_XRANDR_STATE": os.path.join(tmp, "state.json"),
    })
    env.pop("WAYLAND_DISPLAY", None)
    return env


@unittest.skipUnless(HAVE_GTK, "GTK 3 not importable (python3-gi + "
                     "gir1.2-gtk-3.0)")
@unittest.skipUnless(HAVE_X, "Xvfb/xdotool not on PATH")
class XvfbCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r, w = os.pipe()
        cls.xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(w), "-screen", "0", "1280x1024x24",
             "-nolisten", "tcp"], pass_fds=[w],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(w)
        buf = b""
        deadline = time.time() + 15
        while b"\n" not in buf and time.time() < deadline:
            chunk = os.read(r, 16)
            if not chunk:
                break
            buf += chunk
        os.close(r)
        if not buf.strip():
            cls.xvfb.kill()
            raise unittest.SkipTest("Xvfb did not start")
        cls.display = ":" + buf.decode().strip()

    @classmethod
    def tearDownClass(cls):
        cls.xvfb.terminate()
        try:
            cls.xvfb.wait(5)
        except subprocess.TimeoutExpired:
            cls.xvfb.kill()


class GuiDrive(XvfbCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="warandr-gui-")
        self.dump = os.path.join(self.tmp, "dump.jsonl")
        self.log = os.path.join(self.tmp, "apply.log")
        self.qlog = os.path.join(self.tmp, "query.log")
        self.saved = os.path.join(self.tmp, "saved")
        env = _base_env(self.tmp, self.display)
        env.update({
            "FAKE_XRANDR_LOG": self.log,
            "FAKE_XRANDR_QUERY_LOG": self.qlog,
            "WARANDR_TEST_LAYOUT_DUMP": self.dump,
            "WARANDR_TEST_SAVE_AS": self.saved,
        })
        self.env = env
        self.app_log = open(os.path.join(self.tmp, "app.log"), "w")
        self.launch()

    def launch(self):
        after = len(self.dumps())      # a relaunch must not match old dumps
        self.app = subprocess.Popen([sys.executable, "-m", "warandr"],
                                    env=self.env, stdout=self.app_log,
                                    stderr=subprocess.STDOUT)
        return self.wait_dump("layout", lambda d: len(d["boxes"]) == 3,
                              after=after)

    def tearDown(self):
        if self.app.poll() is None:
            self.app.terminate()
            try:
                self.app.wait(5)
            except subprocess.TimeoutExpired:
                self.app.kill()
        self.app_log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def dumps(self):
        try:
            with open(self.dump) as f:
                return [json.loads(ln) for ln in f if ln.strip()]
        except OSError:
            return []

    def wait_dump(self, kind, pred=lambda d: True, timeout=15, after=0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            ds = self.dumps()
            for i in range(len(ds) - 1, after - 1, -1):
                d = ds[i]
                if d["kind"] == kind and pred(d):
                    return d, i + 1      # the next wait starts after it
            if self.app.poll() is not None:
                self.fail("warandr exited with %d:\n%s" % (
                    self.app.returncode, open(self.app_log.name).read()))
            time.sleep(0.05)
        self.fail("no %r dump within %ds; dumps=%r\napp log:\n%s" % (
            kind, timeout, self.dumps()[-3:], open(self.app_log.name).read()))

    def xdo(self, *args):
        subprocess.run(["xdotool"] + [str(a) for a in args], env=self.env,
                       check=True, timeout=30)

    def centre(self, rect):
        x, y, w, h = rect
        return x + w // 2, y + h // 2

    def click(self, rect, button=1):
        x, y = self.centre(rect)
        self.xdo("mousemove", x, y, "click", button)

    def drag(self, rect, tx, ty):
        """Press in the middle of `rect`, move to (tx, ty) in steps, drop."""
        sx, sy = self.centre(rect)
        self.xdo("mousemove", sx, sy, "mousedown", 1)
        for step in (0.25, 0.5, 0.75, 1.0):
            self.xdo("mousemove", int(sx + (tx - sx) * step),
                     int(sy + (ty - sy) * step))
            time.sleep(0.1)
        self.xdo("mouseup", 1)

    def menu_click(self, name, label, after):
        d, n = self.wait_dump("menu", lambda d: d["name"] == name
                              and label in d["items"], after=after)
        self.click(d["items"][label])
        return n

    def assert_modelled(self, menu, tol=2):
        """The popup-position model (all a Wayland driver gets) agrees, to
        `tol` px, with where X11 really put every item."""
        self.assertEqual(menu["coords"], "root")
        self.assertEqual(set(menu["modelled"]), set(menu["items"]), menu)
        for label, r in menu["items"].items():
            m = menu["modelled"][label]
            self.assertEqual(m[2:], r[2:], (label, r, m))
            self.assertLessEqual(max(abs(m[0] - r[0]), abs(m[1] - r[1])),
                                 tol, (label, r, m))

    def shot(self, name):
        if SHOTS and shutil.which("import"):
            os.makedirs(SHOTS, exist_ok=True)
            subprocess.run(["import", "-window", "root",
                            os.path.join(SHOTS, name + ".png")], env=self.env,
                           timeout=30)

    def layout(self):
        return self.wait_dump("layout")[0]

    def calls(self):
        try:
            with open(self.log) as fh:
                return [json.loads(ln) for ln in fh if ln.strip()]
        except OSError:
            return []

    def queries(self):
        """One entry per query the fake answered: which backend it was
        asked as."""
        try:
            with open(self.qlog) as fh:
                return [json.loads(ln) for ln in fh if ln.strip()]
        except OSError:
            return []

    def backend_dump(self, pred=lambda d: True, after=0):
        return self.wait_dump("backend", pred, after=after)

    def open_backend_menu(self, after):
        """Layout ▸ Backend, and the submenu's dump."""
        lay = self.layout()
        self.click(lay["menubar"]["Layout"])
        menu, n = self.wait_dump("menu", lambda d: d["name"] == "layout"
                                 and "Backend" in d["items"], after=after)
        self.click(menu["items"]["Backend"])
        return self.wait_dump("menu", lambda d: d["name"] == "backend"
                              and "Automatic" in d["items"], after=n)

    # -- the drive ----------------------------------------------------------

    def test_menu_drag_apply_save(self):
        lay = self.layout()
        self.assertEqual(set(lay["boxes"]), {"DP-1", "HDMI-1", "DP-2"})
        self.assertEqual(lay["coords"], "root")
        self.assertTrue(lay["settled"], lay)
        self.assertEqual(len(lay["window"]), 2)
        self.assertEqual(sorted(lay["menubar"]),
                         ["Help", "Layout", "Outputs", "View"])
        f = lay["factor"]
        self.assertEqual(f, 8)
        # 1:8 boxes: 1920x1080 -> 240x135, side by side without gaps
        dp1, hdmi, dp2 = (lay["boxes"][k] for k in ("DP-1", "HDMI-1", "DP-2"))
        self.assertEqual((dp1[2], dp1[3]), (240, 135))
        self.assertEqual((hdmi[2], hdmi[3]), (160, 128))
        self.assertEqual(hdmi[0], dp1[0] + 240)
        self.assertEqual(dp2[0], dp1[0] + 400)
        command_line = "xrandr " + " ".join(lay["command"])
        self.assertEqual(lay["status"], command_line)
        self.shot("warandr-1-start")

        # hovering a box describes it in the status bar (no tooltip that
        # could cover a menu); leaving brings the command back
        n = len(self.dumps())
        self.xdo("mousemove", *self.centre(hdmi))
        hover = "HDMI-1: 1280x1024 @ 60.02 Hz, normal, at 1920,0"
        _, n = self.wait_dump("status", lambda d: d["text"] == hover,
                              after=n)
        self.xdo("mousemove", dp2[0] + dp2[2] + 40, dp2[1] + dp2[3] + 60)
        _, n = self.wait_dump("status", lambda d: d["text"] == command_line,
                              after=n)

        # right-click HDMI-1: arandr's order (Refresh rate and the rest after
        # a separator), no Scale item on the X11 backend
        self.click(hdmi, button=3)
        menu, n = self.wait_dump("menu", lambda d: d["name"] == "output:HDMI-1"
                                 and "Orientation" in d["items"], after=n)
        order = sorted(menu["items"], key=lambda k: menu["items"][k][1])
        self.assertEqual(order, ARANDR_MENU)
        self.assert_modelled(menu)
        self.click(menu["items"]["Orientation"])
        self.shot("warandr-2-menu")
        sub, n = self.wait_dump("menu", lambda d: d["name"] == "Orientation"
                                and "left" in d["items"], after=n)
        self.assert_modelled(sub)
        self.click(sub["items"]["left"])
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["HDMI-1"][2:] == [128, 160],
            after=n)
        self.assertTrue(lay["settled"], lay)
        i = lay["command"].index("HDMI-1")
        self.assertEqual(lay["command"][i:i + 7],
                         ["HDMI-1", "--mode", "1280x1024", "--pos", "1920x0",
                          "--rotate", "left"])
        self.shot("warandr-3-rotated")

        # dropping DP-2 onto DP-1 is refused: it snaps back and the status
        # bar says why — even though the pointer now rests inside DP-1 (the
        # message outranks the hover text; the command line comes back with
        # the next redraw)
        dp2 = lay["boxes"]["DP-2"]
        self.drag(dp2, *self.centre(lay["boxes"]["DP-1"]))
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["DP-2"][:2] == dp2[:2]
            and d["status"].startswith("DP-2 not moved"), after=n)
        self.assertEqual(lay["status"],
                         "DP-2 not moved: DP-1 overlaps DP-2")
        self.assertEqual(lay["command"][lay["command"].index("DP-2") + 4],
                         "3200x0")
        self.shot("warandr-3b-rejected")

        # drag DP-2 leftwards so it lands right of the portrait HDMI-1
        # (its right edge is now at layout x 2944 = screen 12 + 368);
        # dropping 3px short and 2px low snaps to (2944, 0)
        hdmi = lay["boxes"]["HDMI-1"]
        sx, sy = self.centre(dp2)
        dx = (hdmi[0] + hdmi[2]) - dp2[0] + 3
        self.drag(dp2, sx + dx, sy + 2)
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["DP-2"][0] == hdmi[0] + hdmi[2]
            and d["boxes"]["DP-2"][1] == hdmi[1], after=n)
        self.assertEqual(lay["command"], EXPECTED)
        self.shot("warandr-4-dragged")

        # the menubar's Outputs drop-down and DP-1's submenu of it: the
        # model covers those placements too; Escape twice closes them
        self.click(lay["menubar"]["Outputs"])
        outputs, n = self.wait_dump("menu", lambda d: d["name"] == "outputs"
                                    and "DP-1" in d["items"], after=n)
        self.assertEqual(sorted(outputs["items"]),
                         ["DP-1", "DP-2", "HDMI-1", "HDMI-2"])
        self.assert_modelled(outputs)
        self.click(outputs["items"]["DP-1"])
        sub, n = self.wait_dump("menu", lambda d: d["name"] == "output:DP-1"
                                and "Primary" in d["items"], after=n)
        self.assert_modelled(sub)
        self.shot("warandr-4b-outputs-menu")
        self.xdo("key", "Escape", "key", "Escape")
        time.sleep(0.3)

        # Apply: the fake records exactly one command, arandr-shaped
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 0, applied)
        self.assertEqual(applied["stderr"], "")
        self.assertEqual(self.calls(), [EXPECTED])
        # and the reload after Apply shows the same layout (the fake kept it)
        lay, n = self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.assertEqual(lay["command"], EXPECTED)
        self.shot("warandr-5-applied")

        # Save As (dialog bypassed by WARANDR_TEST_SAVE_AS): arandr's file,
        # shebang and the one line
        self.click(lay["buttons"]["save_as"])
        saved, n = self.wait_dump("saved", after=n)
        path = saved["path"]
        self.assertEqual(path, self.saved + ".sh")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        with open(path) as fh:
            text = fh.read()
        self.assertEqual(text, "#!/bin/sh\nxrandr %s\n" % " ".join(EXPECTED))

    def test_backend_indicator_menu_and_switch(self):
        # the indicator is up from the first frame and names the live
        # backend; the availability table arrives from `wxrandr --backends`
        d, n = self.backend_dump(lambda d: d["available"])
        self.assertEqual(d["indicator"], "backend: xrandr (X11)")
        self.assertEqual(d["name"], "x11")
        self.assertIsNone(d["forced"])
        self.assertEqual(d["available"], FAKE_AVAILABLE)
        lay = self.layout()
        self.assertEqual(lay["backend"], "x11")
        self.assertEqual(lay["backend_label"], "xrandr (X11)")
        self.assertTrue(lay["status"].startswith("xrandr --output "), lay)

        # Layout ▸ Backend: arandr's grammar, radios, and the backends this
        # session cannot reach are insensitive with the reason in a tooltip
        menu, n = self.open_backend_menu(n)
        order = sorted(menu["items"], key=lambda k: menu["items"][k][1])
        self.assertEqual(order, BACKEND_MENU)
        self.assert_modelled(menu)
        self.assertEqual(menu["active"],
                         {lbl: lbl == "Automatic" for lbl in BACKEND_MENU})
        self.assertEqual(
            {lbl: menu["sensitive"][lbl] for lbl in BACKEND_MENU},
            {"Automatic": True, "X11 (xrandr)": True, "sway": False,
             "wlroots (wlr)": False, "GNOME (mutter)": True,
             "KDE (kwin)": False})
        self.assertEqual(menu["tooltips"]["sway"],
                         "not available in this session: no sway or i3 IPC "
                         "socket ($SWAYSOCK)")
        self.assertEqual(menu["tooltips"]["GNOME (mutter)"], "")
        self.shot("warandr-7-backend-menu")

        # choosing one re-reads the layout *through* it and redraws
        before, mark = len(self.queries()), len(self.dumps())
        self.click(menu["items"]["GNOME (mutter)"])
        d, n = self.backend_dump(lambda d: d["forced"] == "mutter", after=mark)
        self.assertEqual(d["indicator"], "backend: mutter (Wayland)")
        self.assertEqual(d["word"], "wxrandr --backend mutter")
        self.assertEqual([q["backend"] for q in self.queries()[before:]],
                         ["mutter"])
        lay, _i = self.wait_dump("layout", lambda d: d["backend"] == "mutter",
                                 after=mark)
        n = len(self.dumps())
        self.assertEqual(set(lay["boxes"]), {"DP-1", "HDMI-1", "DP-2"})
        self.assertTrue(lay["settled"], lay)
        # (the pointer is left where the menu item was; park it on the
        # menubar so the status line is the command, not a hover)
        self.xdo("mousemove", 600, 5)
        _d, _i = self.wait_dump(
            "status", lambda d: d["text"] == "wxrandr --backend mutter "
            + " ".join(lay["command"]), after=mark)
        self.shot("warandr-8-backend-switched")

        # ...and everything after it is that backend's: the per-output menu
        # grows the Wayland-only Scale, Apply runs it, the saved script says
        # wxrandr and records the forced choice as a comment
        self.click(lay["boxes"]["DP-1"], button=3)
        out, n = self.wait_dump("menu", lambda d: d["name"] == "output:DP-1"
                                and "Scale" in d["items"], after=n)
        self.xdo("key", "Escape")
        time.sleep(0.3)
        lay = self.layout()
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 0, applied)
        self.assertEqual(self.calls()[-1][:2], ["--backend", "mutter"])
        self.assertEqual(self.calls()[-1][2:], lay["command"])
        lay, n = self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.click(lay["buttons"]["save_as"])
        saved, n = self.wait_dump("saved", after=n)
        with open(saved["path"]) as fh:
            text = fh.read()
        self.assertEqual(text.split("\n")[:2],
                         ["#!/bin/sh",
                          "# warandr: backend mutter forced "
                          "(wxrandr --backend mutter)"])
        self.assertTrue(text.split("\n")[2].startswith("wxrandr --output "),
                        text)
        self.assertNotIn("--backend", text.split("\n")[2])

        # back to Automatic: the X11 backend and arandr's own script again
        menu, n = self.open_backend_menu(n)
        self.assertEqual(menu["active"]["GNOME (mutter)"], True)
        mark = len(self.dumps())
        self.click(menu["items"]["Automatic"])
        d, n = self.backend_dump(lambda d: d["forced"] is None, after=mark)
        self.assertEqual(d["indicator"], "backend: xrandr (X11)")
        lay, n = self.wait_dump("layout", lambda d: d["backend"] == "x11",
                                after=mark)
        self.xdo("mousemove", 600, 5)
        self.wait_dump("status", lambda d: d["text"] == "xrandr "
                       + " ".join(lay["command"]), after=mark)

    def test_unreachable_backend_shows_the_dialog_and_reverts(self):
        self.env["FAKE_XRANDR_BACKEND_FAIL"] = "mutter"
        self.app.terminate()
        self.app.wait(5)
        lay, n = self.launch()
        d, n = self.backend_dump(lambda d: d["available"], after=n)
        self.assertEqual(d["indicator"], "backend: xrandr (X11)")
        menu, n = self.open_backend_menu(n)
        self.click(menu["items"]["GNOME (mutter)"])
        d, n = self.backend_dump(lambda d: not d["ok"], after=n)
        self.assertEqual(d["wanted"], "mutter")
        self.assertIn("the fake says so", d["error"])
        self.assertIsNone(d["forced"])
        time.sleep(0.5)
        self.shot("warandr-9-backend-refused")
        self.xdo("key", "Return")               # the modal dialog
        time.sleep(0.5)
        # the window is neither empty nor switched: the layout, the
        # indicator and the radio are the previous, working choice
        lay = self.layout()
        self.assertEqual(set(lay["boxes"]), {"DP-1", "HDMI-1", "DP-2"})
        self.assertEqual(lay["backend"], "x11")
        self.assertTrue(lay["status"].startswith("xrandr --output "), lay)
        self.assertIsNone(self.app.poll())
        menu, n = self.open_backend_menu(len(self.dumps()))
        self.assertEqual(menu["active"]["Automatic"], True)
        self.assertEqual(menu["active"]["GNOME (mutter)"], False)
        self.xdo("key", "Escape", "key", "Escape")

    def test_apply_failure_keeps_edits(self):
        self.env["FAKE_XRANDR_FAIL"] = "xrandr: Configure crtc 1 failed"
        # the editor inherits env at launch: relaunch with the failure set
        self.app.terminate()
        self.app.wait(5)
        lay, n = self.launch()
        # an edit first: rotate HDMI-1
        self.click(lay["boxes"]["HDMI-1"], button=3)
        n = self.menu_click("output:HDMI-1", "Orientation", n)
        n = self.menu_click("Orientation", "left", n)
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["HDMI-1"][2:] == [128, 160],
            after=n)
        edited = lay["command"]
        self.assertIn("left", edited)
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 1)
        self.assertIn("Configure crtc 1 failed", applied["stderr"])
        time.sleep(0.5)
        self.shot("warandr-6-error")
        # the modal error dialog has the focus: Return closes it; the editor
        # is still alive and still shows the *edited* layout (arandr raises
        # before re-reading the screen; a reload would throw the edit away)
        self.xdo("key", "Return")
        lay, n = self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.assertEqual(lay["command"], edited)
        self.assertEqual(lay["boxes"]["HDMI-1"][2:], [128, 160])
        self.assertEqual(len(self.calls()), 1)
        self.assertIsNone(self.app.poll())
        # and Apply is usable again (still failing, same dialog)
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 1)
        self.xdo("key", "Return")
        self.wait_dump("layout", lambda d: not d["busy"], after=n)
        self.assertEqual(len(self.calls()), 2)
        self.assertIsNone(self.app.poll())


class GuiProbe(XvfbCase):
    """tests/fixtures/gui_probe.py: the editor in-process with a stub
    backend whose apply() sleeps 1.5 s."""

    def test_probe(self):
        tmp = tempfile.mkdtemp(prefix="warandr-probe-")
        try:
            env = _base_env(tmp, self.display)
            p = subprocess.run([sys.executable,
                                os.path.join(FIXTURES, "gui_probe.py")],
                               env=env, capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(p.returncode, 0, p.stderr)
            res = json.loads(p.stdout.strip().split("\n")[-1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # Apply returns at once and the main loop keeps ticking while the
        # backend runs; Apply is greyed meanwhile and a second click is a no-op
        self.assertLess(res["apply_returned_s"], 0.5, res)
        self.assertTrue(res["busy_during"], res)
        self.assertTrue(res["status_during"].startswith("running: xrandr "))
        self.assertTrue(res["apply_finished"], res)
        self.assertLess(res["longest_gap_s"], 0.5, res)
        self.assertGreater(res["ticks_during_apply"], 10, res)
        self.assertEqual(res["applied_calls"], 1, res)
        self.assertEqual(res["snapshots_after_ok"], 2, res)
        self.assertTrue(res["template_kept"], res)
        self.assertTrue(res["apply_button_back"], res)
        # a failed Apply: dialog, no reload, the edit stays
        self.assertTrue(res["fail_dialog"].startswith("XRandR failed:"), res)
        self.assertIn("configure crtc failed", res["fail_dialog"])
        self.assertTrue(res["fail_keeps_edits"], res)
        self.assertEqual(res["snapshots_after_fail"], 2, res)
        # a layout dump waits for the frame clock's allocation: none reports
        # the box at its old size
        self.assertTrue(res["unsettled_after_redraw"], res)
        self.assertTrue(res["dumps_after_redraw"], res)
        self.assertEqual(res["dumps_after_redraw"],
                         [[[160, 128], True]] * len(res["dumps_after_redraw"]),
                         res)
        self.assertTrue(res["settled_now"], res)
        self.assertEqual(res["hdmi_alloc"], [160, 128], res)
        # Save As: the hint when wxrandr is missing from PATH, none when it
        # is there
        self.assertEqual(res["save_hint"], "saved %s - note: wxrandr is not "
                         "on PATH, the script needs it" % res["saved_path"])
        self.assertEqual(res["save_nohint"], "saved " + res["saved_path"])
        # popup menus do not accumulate
        self.assertTrue(res["popup_released"], res)
        self.assertLessEqual(res["popups_alive"], 1, res)
        # zoom: arandr's three levels, radios in step with Ctrl+/-
        self.assertEqual(res["zooms"], [4, 8, 16])
        self.assertEqual((res["zoom_in_factor"], res["zoom_in_radio"]),
                         (4, [4]), res)
        self.assertEqual((res["zoom_out_factor"], res["zoom_out_radio"]),
                         (16, [16]), res)
        # per-output menu shape
        self.assertEqual(res["menu_x11"],
                         ["Active", "Primary", "Resolution", "Orientation",
                          "-", "Refresh rate", "Reflection", "Mirror of"])
        self.assertEqual(res["menu_wayland"], res["menu_x11"] + ["Scale"])
        self.assertEqual(res["wayland_word"], "wxrandr")


class BackendCli(unittest.TestCase):
    """warandr's own `--backend NAME` / `--print-backend`, spelled like
    wxrandr's so a hotkey can pin one.  No display needed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="warandr-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = _base_env(self.tmp, "")
        self.env.pop("DISPLAY")
        self.env["FAKE_XRANDR_STATE"] = os.path.join(self.tmp, "state.json")

    def warandr(self, *args):
        return subprocess.run([sys.executable, "-m", "warandr"] + list(args),
                              env=self.env, capture_output=True, text=True,
                              timeout=60)

    def test_print_backend_is_one_token(self):
        p = self.warandr("--print-backend")
        self.assertEqual((p.returncode, p.stdout), (0, "x11\n"))
        p = self.warandr("--backend", "mutter", "--print-backend")
        self.assertEqual((p.returncode, p.stdout), (0, "mutter\n"))
        p = self.warandr("--backend", "gnome", "--print-backend")
        self.assertEqual(p.stdout, "mutter\n")
        p = self.warandr("--backend", "x11", "--print-backend")
        self.assertEqual(p.stdout, "x11\n")

    def test_a_forced_backend_reaches_the_command_and_the_script(self):
        p = self.warandr("--backend", "mutter", "--command")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.startswith("wxrandr --backend mutter "
                                            "--output DP-1 "), p.stdout)
        out = os.path.join(self.tmp, "layout.sh")
        p = self.warandr("--backend", "mutter", "--save", out)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(out) as fh:
            lines = fh.read().split("\n")
        self.assertEqual(lines[:2], ["#!/bin/sh",
                                     "# warandr: backend mutter forced "
                                     "(wxrandr --backend mutter)"])
        self.assertTrue(lines[2].startswith("wxrandr --output DP-1 "))
        # ...and without one, arandr's own two lines
        p = self.warandr("--save", out)
        with open(out) as fh:
            self.assertEqual(len(fh.read().rstrip("\n").split("\n")), 2)

    def test_an_unknown_name_is_one_line(self):
        p = self.warandr("--backend", "banana", "--print-backend")
        self.assertEqual((p.returncode, p.stdout), (1, ""))
        self.assertEqual(p.stderr, "warandr: unknown backend 'banana' "
                         "(valid: auto, x11, sway, wlr, mutter, kwin)\n")
        self.assertNotIn("Traceback", p.stderr)

    def test_help_lists_the_new_options(self):
        p = self.warandr("--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("--backend NAME", p.stdout)
        self.assertIn("--print-backend", p.stdout)


@unittest.skipUnless(HAVE_GTK, "GTK 3 not importable")
class NoDisplay(unittest.TestCase):
    def test_no_display_is_one_line(self):
        tmp = tempfile.mkdtemp(prefix="warandr-nodisplay-")
        try:
            env = _base_env(tmp, "")
            env.pop("DISPLAY")
            p = subprocess.run([sys.executable, "-m", "warandr"], env=env,
                               capture_output=True, text=True, timeout=60)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr.strip().split("\n")[-1],
                         "warandr: cannot open display (DISPLAY is not set)")
        self.assertNotIn("Traceback", p.stderr)


if __name__ == "__main__":
    unittest.main()
