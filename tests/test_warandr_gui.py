"""warandr GUI test: the real GTK 3 editor under Xvfb, driven with xdotool.

Skipped unless GTK 3 is importable and Xvfb + xdotool are on PATH.  The
editor talks to tests/fixtures/fake_xrandr.py (WARANDR_XRANDR), a 3-output
RandR simulator that records every apply.  The editor's test hook
WARANDR_TEST_LAYOUT_DUMP writes root-window coordinates of the output boxes,
toolbar buttons and popup-menu items, so the test clicks real pixels:

  right-click HDMI-1 -> Orientation -> left
  drag DP-2 next to the (now portrait) HDMI-1; it snaps to its right edge
  Apply -> the recorded argv is exactly the arandr-shaped command
  Save As (WARANDR_TEST_SAVE_AS) -> the layout script
  a failing apply shows the error dialog and the editor survives

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


@unittest.skipUnless(HAVE_GTK, "GTK 3 not importable (python3-gi + "
                     "gir1.2-gtk-3.0)")
@unittest.skipUnless(HAVE_X, "Xvfb/xdotool not on PATH")
class GuiDrive(unittest.TestCase):
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

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="warandr-gui-")
        self.dump = os.path.join(self.tmp, "dump.jsonl")
        self.log = os.path.join(self.tmp, "apply.log")
        self.saved = os.path.join(self.tmp, "saved")
        env = dict(os.environ)
        env.update({
            "DISPLAY": self.display, "GDK_BACKEND": "x11",
            "NO_AT_BRIDGE": "1", "GSETTINGS_BACKEND": "memory",
            "HOME": self.tmp, "PYTHONPATH": ROOT,
            "WARANDR_XRANDR": "%s %s" % (sys.executable,
                                         os.path.join(FIXTURES,
                                                      "fake_xrandr.py")),
            "FAKE_XRANDR_LOG": self.log,
            "FAKE_XRANDR_STATE": os.path.join(self.tmp, "state.json"),
            "WARANDR_TEST_LAYOUT_DUMP": self.dump,
            "WARANDR_TEST_SAVE_AS": self.saved,
        })
        env.pop("WAYLAND_DISPLAY", None)
        self.env = env
        self.app_log = open(os.path.join(self.tmp, "app.log"), "w")
        self.app = subprocess.Popen([sys.executable, "-m", "warandr"],
                                    env=env, stdout=self.app_log,
                                    stderr=subprocess.STDOUT)
        self.wait_dump("layout", lambda d: len(d["boxes"]) == 3)

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

    def menu_click(self, name, label, after):
        d, n = self.wait_dump("menu", lambda d: d["name"] == name
                              and label in d["items"], after=after)
        self.click(d["items"][label])
        return n

    def shot(self, name):
        if SHOTS and shutil.which("import"):
            os.makedirs(SHOTS, exist_ok=True)
            subprocess.run(["import", "-window", "root",
                            os.path.join(SHOTS, name + ".png")], env=self.env,
                           timeout=30)

    def layout(self):
        return self.wait_dump("layout")[0]

    # -- the drive ----------------------------------------------------------

    def test_menu_drag_apply_save(self):
        lay = self.layout()
        self.assertEqual(set(lay["boxes"]), {"DP-1", "HDMI-1", "DP-2"})
        f = lay["factor"]
        self.assertEqual(f, 8)
        # 1:8 boxes: 1920x1080 -> 240x135, side by side without gaps
        dp1, hdmi, dp2 = (lay["boxes"][k] for k in ("DP-1", "HDMI-1", "DP-2"))
        self.assertEqual((dp1[2], dp1[3]), (240, 135))
        self.assertEqual((hdmi[2], hdmi[3]), (160, 128))
        self.assertEqual(hdmi[0], dp1[0] + 240)
        self.assertEqual(dp2[0], dp1[0] + 400)
        self.shot("warandr-1-start")

        # right-click HDMI-1 -> Orientation -> left
        n = len(self.dumps())
        self.click(hdmi, button=3)
        n = self.menu_click("output:HDMI-1", "Orientation", n)
        self.shot("warandr-2-menu")
        n = self.menu_click("Orientation", "left", n)
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["HDMI-1"][2:] == [128, 160],
            after=n)
        i = lay["command"].index("HDMI-1")
        self.assertEqual(lay["command"][i:i + 7],
                         ["HDMI-1", "--mode", "1280x1024", "--pos", "1920x0",
                          "--rotate", "left"])
        self.shot("warandr-3-rotated")

        # drag DP-2 leftwards so it lands right of the portrait HDMI-1
        # (its right edge is now at layout x 2944 = screen 12 + 368);
        # dropping 3px short and 2px low snaps to (2944, 0)
        dp2 = lay["boxes"]["DP-2"]
        hdmi = lay["boxes"]["HDMI-1"]
        sx, sy = self.centre(dp2)
        dx = (hdmi[0] + hdmi[2]) - dp2[0] + 3
        self.xdo("mousemove", sx, sy, "mousedown", 1)
        for step in (0.25, 0.5, 0.75, 1.0):
            self.xdo("mousemove", int(sx + dx * step), sy + 2)
            time.sleep(0.1)
        self.xdo("mouseup", 1)
        lay, n = self.wait_dump(
            "layout", lambda d: d["boxes"]["DP-2"][0] == hdmi[0] + hdmi[2]
            and d["boxes"]["DP-2"][1] == hdmi[1], after=n)
        self.assertEqual(lay["command"], EXPECTED)
        self.shot("warandr-4-dragged")

        # Apply: the fake records exactly one command, arandr-shaped
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 0, applied)
        self.assertEqual(applied["stderr"], "")
        with open(self.log) as fh:
            calls = [json.loads(ln) for ln in fh if ln.strip()]
        self.assertEqual(calls, [EXPECTED])
        # and the reload after Apply shows the same layout (the fake kept it)
        lay, n = self.wait_dump("layout", after=n)
        self.assertEqual(lay["command"], EXPECTED)
        self.shot("warandr-5-applied")

        # Save As (dialog bypassed by WARANDR_TEST_SAVE_AS)
        self.click(lay["buttons"]["save_as"])
        saved, n = self.wait_dump("saved", after=n)
        path = saved["path"]
        self.assertEqual(path, self.saved + ".sh")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        with open(path) as fh:
            lines = fh.read().split("\n")
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertTrue(lines[1].startswith("# "))
        self.assertEqual(lines[2], "xrandr " + " ".join(EXPECTED))
        self.assertEqual(lines[3:], [""])

    def test_apply_failure_dialog(self):
        lay = self.layout()
        n = len(self.dumps())
        with open(os.path.join(self.tmp, "state.json"), "w") as fh:
            fh.write("")           # keep the default; the fake ignores junk
        self.env["FAKE_XRANDR_FAIL"] = "xrandr: Configure crtc 1 failed"
        # the editor inherits env at launch: relaunch with the failure set
        self.app.terminate()
        self.app.wait(5)
        self.app = subprocess.Popen([sys.executable, "-m", "warandr"],
                                    env=self.env, stdout=self.app_log,
                                    stderr=subprocess.STDOUT)
        lay, n = self.wait_dump("layout", lambda d: len(d["boxes"]) == 3,
                                after=n)
        self.click(lay["buttons"]["apply"])
        applied, n = self.wait_dump("applied", after=n)
        self.assertEqual(applied["rc"], 1)
        self.assertIn("Configure crtc 1 failed", applied["stderr"])
        time.sleep(0.5)
        self.shot("warandr-6-error")
        # the modal error dialog has the focus: Return closes it, the editor
        # reloads and is still alive
        self.xdo("key", "Return")
        self.wait_dump("layout", after=n)
        self.assertIsNone(self.app.poll())


if __name__ == "__main__":
    unittest.main()
