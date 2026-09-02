#!/usr/bin/env python3
"""Agent C: integration tests against a real headless sway — sway backend
(search/activate/move/resize/scratchpad/desktops/selectwindow) plus a smoke
pass of the wlr foreign-toplevel backend against the same compositor.

Skipped when sway/foot are not on PATH (run inside `nix develop`). Starts its
own sway on a private XDG_RUNTIME_DIR so concurrent runs don't collide."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@unittest.skipUnless(shutil.which("sway"), "sway not on PATH (run in nix develop)")
@unittest.skipUnless(shutil.which("foot"), "foot not on PATH (run in nix develop)")
class SwayWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rtdir = tempfile.mkdtemp(prefix="wdotool-winc-")
        os.chmod(cls.rtdir, 0o700)
        conf = os.path.join(cls.rtdir, "sway.conf")
        with open(conf, "w") as f:
            f.write(
                "output HEADLESS-1 mode 1280x720\n"
                'exec foot --app-id foota --title "Foot A"\n'
                'exec foot --app-id footb --title "Foot B"\n'
            )
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR=cls.rtdir,
            WLR_BACKENDS="headless",
            WLR_LIBINPUT_NO_DEVICES="1",
            # dodge the nixpkgs sway wrapper's dbus-run-session fallback
            DBUS_SESSION_BUS_ADDRESS=f"unix:path={cls.rtdir}/no-bus",
        )
        cls.sway = subprocess.Popen(
            ["sway", "-c", conf], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cls.sock = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            socks = [n for n in os.listdir(cls.rtdir)
                     if n.startswith("sway-ipc.") and n.endswith(".sock")]
            if socks:
                cls.sock = os.path.join(cls.rtdir, socks[0])
                break
            if cls.sway.poll() is not None:
                raise unittest.SkipTest("sway exited at startup")
            time.sleep(0.2)
        if cls.sock is None:
            cls.sway.kill()
            raise unittest.SkipTest("sway did not create an IPC socket")
        # wait for both foot windows
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            rc, out, _err = cls.wdo("search", "--class", "foot[ab]")
            if rc == 0 and len(out.split()) == 2:
                return
            time.sleep(0.3)
        raise unittest.SkipTest("foot windows never appeared")

    @classmethod
    def tearDownClass(cls):
        cls.sway.send_signal(signal.SIGTERM)
        try:
            cls.sway.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.sway.kill()
        shutil.rmtree(cls.rtdir, ignore_errors=True)

    backend = "sway"

    @classmethod
    def wdo(cls, *args, backend=None):
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR=cls.rtdir,
            WDOTOOL_BACKEND=backend or cls.backend,
            SWAYSOCK=cls.sock,
            WAYLAND_DISPLAY="wayland-1",
        )
        p = subprocess.run(
            [sys.executable, "-m", "wdotool", *args],
            env=env, capture_output=True, text=True, cwd=ROOT, timeout=30,
        )
        return p.returncode, p.stdout, p.stderr

    def swaymsg(self, cmd):
        subprocess.run(
            ["swaymsg", "-s", self.sock, cmd],
            env=dict(os.environ, XDG_RUNTIME_DIR=self.rtdir),
            capture_output=True, timeout=10, check=True,
        )

    # -- sway backend -------------------------------------------------------

    def test_01_search_and_queries(self):
        rc, out, err = self.wdo("search", "--class", "foota")
        self.assertEqual(rc, 0, err)
        wid = int(out)
        rc, out, _e = self.wdo("getwindowclassname", str(wid))
        self.assertEqual(out, "foota\n")
        rc, out, _e = self.wdo("search", "--class", "foota", "getwindowpid")
        self.assertEqual(rc, 0)
        self.assertGreater(int(out), 0)
        rc, out, _e = self.wdo("search", "--class", "foota", "getwindowname")
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith("\n") and out.strip())

    def test_02_search_forms(self):
        rc, out, _e = self.wdo("search", "--shell", "--prefix", "W",
                               "--class", "foot[ab]")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("WWINDOWS=(") and out.endswith(")\n"))
        rc, out, _e = self.wdo("search", "--limit", "1", "--class", "foot[ab]")
        self.assertEqual(len(out.split()), 1)
        rc, out, _e = self.wdo("search", "--class", "nosuchapp")
        self.assertEqual((rc, out), (1, ""))

    def test_03_activate_focus(self):
        for cls_ in ("foota", "footb"):
            rc, _o, err = self.wdo("search", "--class", cls_,
                                   "windowactivate", "--sync")
            self.assertEqual(rc, 0, err)
            rc, out, _e = self.wdo("getactivewindow", "getwindowclassname")
            self.assertEqual(out, cls_ + "\n")
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowfocus", "--sync")
        self.assertEqual(rc, 0, err)
        rc, out, _e = self.wdo("getwindowfocus", "getwindowclassname")
        self.assertEqual(out, "foota\n")

    def test_04_move_resize_floating(self):
        self.swaymsg("[app_id=foota] floating enable")
        try:
            rc, _o, err = self.wdo("search", "--class", "foota",
                                   "windowmove", "--sync", "100", "50")
            self.assertEqual(rc, 0, err)
            rc, out, _e = self.wdo("search", "--class", "foota",
                                   "getwindowgeometry", "--shell")
            geo = dict(line.split("=") for line in out.split())
            self.assertEqual((geo["X"], geo["Y"]), ("100", "50"))

            # x/y literal passthrough
            rc, _o, _e = self.wdo("search", "--class", "foota",
                                  "windowmove", "x", "200")
            rc, out, _e = self.wdo("search", "--class", "foota",
                                   "getwindowgeometry", "--shell")
            geo = dict(line.split("=") for line in out.split())
            self.assertEqual((geo["X"], geo["Y"]), ("100", "200"))

            rc, _o, err = self.wdo("search", "--class", "foota",
                                   "windowsize", "--sync", "400", "300")
            self.assertEqual(rc, 0, err)
            rc, out, _e = self.wdo("search", "--class", "foota",
                                   "getwindowgeometry", "--shell")
            geo = dict(line.split("=") for line in out.split())
            # foot may shave up to one terminal cell to snap to its grid
            self.assertLessEqual(abs(int(geo["WIDTH"]) - 400), 16, out)
            self.assertLessEqual(abs(int(geo["HEIGHT"]) - 300), 16, out)
        finally:
            self.swaymsg("[app_id=foota] floating disable")

    def test_05_move_tiled_warns_but_succeeds(self):
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowmove", "10", "10")
        self.assertEqual(rc, 0)
        self.assertIn("xdo_move_window reported an error", err)

    def test_06_scratchpad_map_unmap(self):
        rc, _o, err = self.wdo("search", "--class", "foota", "windowunmap")
        self.assertEqual(rc, 0, err)
        rc, out, _e = self.wdo("search", "--onlyvisible", "--class", "foota")
        self.assertEqual((rc, out), (1, ""))
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowmap", "--sync")
        self.assertEqual(rc, 0, err)
        rc, _o, _e = self.wdo("search", "--onlyvisible", "--class", "foota")
        self.assertEqual(rc, 0)
        # windowminimize behaves like unmap on sway
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowminimize", "--sync")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "foota", "windowmap")
        self.assertEqual(rc, 0, err)
        self.swaymsg("[app_id=foota] floating disable")

    def test_07_windowstate(self):
        rc, _o, err = self.wdo("search", "--class", "footb",
                               "windowstate", "--add", "FULLSCREEN")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "footb",
                               "windowstate", "--toggle", "FULLSCREEN")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "footb",
                               "windowstate", "--add", "MAXIMIZED_VERT")
        self.assertEqual(rc, 1)
        self.assertIn("xdo_window_property reported an error", err)

    def test_08_desktops(self):
        rc, out, _e = self.wdo("get_desktop")
        self.assertEqual((rc, out), (0, "0\n"))
        rc, out, _e = self.wdo("set_desktop", "1", "get_desktop")
        self.assertEqual(out, "1\n")
        rc, out, _e = self.wdo("set_desktop", "--relative", "--", "-1",
                               "get_desktop")
        self.assertEqual(out, "0\n")
        rc, out, err = self.wdo("search", "--class", "footb",
                                "set_desktop_for_window", "1",
                                "get_desktop_for_window")
        self.assertEqual(out, "1\n", err)
        rc, _o, _e = self.wdo("search", "--class", "footb",
                              "set_desktop_for_window", "0")
        rc, out, _e = self.wdo("get_num_desktops")
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(int(out), 1)
        rc, out, _e = self.wdo("get_desktop_viewport", "--shell")
        self.assertEqual(out, "X=0\nY=0\n")

    def test_09_warn_and_succeed(self):
        rc, out, err = self.wdo("search", "--class", "foot[ab]",
                                "windowreparent", "%1", "%2")
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("windowreparent", err)
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "set_window", "--name", "zzz")
        self.assertEqual(rc, 0)
        self.assertIn("set_window", err)
        rc, _o, err = self.wdo("search", "--class", "foota", "windowraise")
        self.assertEqual(rc, 0)  # tiled: warn + succeed
        rc, _o, err = self.wdo("search", "--class", "foota", "windowlower")
        self.assertEqual(rc, 0)

    def test_10_selectwindow(self):
        t = threading.Timer(
            1.0, lambda: self.swaymsg("[app_id=footb] focus"))
        t.start()
        try:
            rc, out, err = self.wdo("selectwindow", "getwindowclassname")
            self.assertEqual(rc, 0, err)
            self.assertEqual(out, "footb\n")
        finally:
            t.cancel()

    def test_11_close_quit_kill(self):
        self.swaymsg("exec foot --app-id victim1")
        self.swaymsg("exec foot --app-id victim2")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rc, out, _e = self.wdo("search", "--class", "victim[12]")
            if rc == 0 and len(out.split()) == 2:
                break
            time.sleep(0.3)
        else:
            self.fail("victim windows never appeared")
        rc, _o, err = self.wdo("search", "--class", "victim1", "windowclose")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "victim2", "windowkill")
        self.assertEqual(rc, 0, err)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rc, _o, _e = self.wdo("search", "--class", "victim[12]")
            if rc == 1:
                break
            time.sleep(0.3)
        self.assertEqual(rc, 1, "victims did not go away")

    # -- wlr backend smoke --------------------------------------------------

    def test_12_wlr_smoke(self):
        rc, out, err = self.wdo("search", "--class", "foot[ab]",
                                backend="wlr")
        self.assertEqual(rc, 0, err)
        wids = [int(x) for x in out.split()]
        self.assertEqual(len(wids), 2)
        for wid in wids:
            self.assertGreaterEqual(wid, 1000000)
        rc, out, err = self.wdo("search", "--class", "foota",
                                "getwindowclassname", backend="wlr")
        self.assertEqual((rc, out), (0, "foota\n"))
        rc, _o, err = self.wdo("search", "--class", "footb",
                               "windowactivate", backend="wlr")
        self.assertEqual(rc, 0, err)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rc, out, _e = self.wdo("getactivewindow", "getwindowclassname",
                                   backend="wlr")
            if out == "footb\n":
                break
            time.sleep(0.2)
        self.assertEqual(out, "footb\n")
        rc, out, _e = self.wdo("search", "--class", "foota",
                               "getwindowgeometry", backend="wlr")
        self.assertIn("Geometry: 1280x720", out)  # geometry unknown: output size
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowstate", "--add", "FULLSCREEN",
                               backend="wlr")
        self.assertEqual(rc, 0, err)
        rc, _o, err = self.wdo("search", "--class", "foota",
                               "windowstate", "--remove", "FULLSCREEN",
                               backend="wlr")
        self.assertEqual(rc, 0, err)
        # capability gaps error cleanly
        rc, _o, err = self.wdo("get_desktop", backend="wlr")
        self.assertEqual(rc, 1)
        self.assertIn("not supported by the wlr backend", err)
        rc, _o, err = self.wdo("search", "--class", "foota", "getwindowpid",
                               backend="wlr")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
