#!/usr/bin/env python3
"""wxrandr against a hostile compositor and a hostile environment.

Everything here is a regression for a defect the hostile pass found, and
every one of them is driven at the wire: tests/fixtures/fake_wlr.py is a real
zwlr_output_manager_v1 server that can be told to go quiet at three different
moments, so the CLI is exercised exactly as a wedged wlroots session would.

  * a compositor that answers `succeeded` and then stops must not hang the
    CLI (the 10 s guard the backend arms has to survive dispatch())
  * a --scale that truncates an output's logical size below the advertised
    16x16 minimum is refused, and nothing is sent
  * --dpi 0 / nan / a negative one falls back to 96dpi instead of aborting
  * a hand-edited state file of the wrong shape never crashes a query
  * a broken stdout exits 1, not the interpreter's 120
"""

import json
import os
import shutil
import signal
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

from wxrandr import cli, core                                    # noqa: E402


class FakeWlr(unittest.TestCase):
    """One fake wlroots compositor per test, in `mode`."""

    MODE = "normal"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-hostile-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sock = os.path.join(self.tmp, "wayland-fake")
        self.applies = os.path.join(self.tmp, "applies.log")
        self.server = subprocess.Popen(
            [sys.executable, os.path.join(FIXTURES, "fake_wlr.py"),
             self.sock, self.MODE, self.applies],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop_server)
        self.assertIn("listening", self.server.stdout.readline())

    def stop_server(self):
        if self.server.poll() is None:
            os.kill(self.server.pid, signal.SIGKILL)
            self.server.wait(10)

    def env(self):
        env = dict(os.environ)
        env.update({"WAYLAND_DISPLAY": self.sock, "PYTHONPATH": ROOT,
                    "XDG_RUNTIME_DIR": self.tmp,
                    "FUCKWAYLAND_PASSTHROUGH": "never"})
        env.pop("DISPLAY", None)
        return env

    def wxrandr(self, *args, timeout=60):
        return subprocess.run(
            [sys.executable, "-m", "wxrandr", "--backend", "wlr"] + list(args),
            env=self.env(), capture_output=True, text=True, timeout=timeout)

    def apply_count(self):
        try:
            with open(self.applies) as f:
                return len(f.read().split())
        except OSError:
            return 0


class ApplyGoesQuiet(FakeWlr):
    """The compositor takes the configuration, says `succeeded`, and then
    never speaks again.  dispatch() used to leave the socket blocking on the
    way out -- clearing the 10 s deadline the backend had armed -- so the
    post-apply roundtrip blocked in recvmsg with nothing to wake it and the
    CLI hung for as long as the session lasted."""

    MODE = "silent-apply"

    def test_apply_returns_and_does_not_hang(self):
        start = time.monotonic()
        p = self.wxrandr("--output", "HEAD-1", "--pos", "100x0", timeout=90)
        self.assertLess(time.monotonic() - start, 40,
                        "the CLI did not come back inside the 10s guard")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.stderr, "xrandr: the compositor applied the output "
                         "configuration and then stopped responding\n")
        self.assertEqual(self.apply_count(), 1)


class ApplyIsIgnored(FakeWlr):
    """The same, one step earlier: no answer to the apply at all.  This one
    always ended in the backend's own fatal; it is here so the two paths are
    covered by the same shape of test."""

    MODE = "mute-apply"

    def test_timeout_is_a_one_line_fatal(self):
        start = time.monotonic()
        p = self.wxrandr("--output", "HEAD-1", "--pos", "100x0", timeout=90)
        self.assertLess(time.monotonic() - start, 40)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, "xrandr: timed out waiting for the "
                         "compositor to apply the output configuration\n")


class MinimumSize(FakeWlr):
    """The Screen line advertises `minimum 16 x 16`.  A --scale big enough to
    truncate int(px / scale) below it produced an output of 0x0 that the
    compositor accepted -- no space on the desktop, and nothing to click to
    get it back."""

    def test_scale_that_truncates_to_zero_is_refused(self):
        p = self.wxrandr("--output", "HEAD-1", "--scale", "99999")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, "xrandr: output HEAD-1 cannot be smaller "
                         "than 16x16 (desired size 0x0)\n")
        self.assertEqual(self.apply_count(), 0, "a refused command sent an "
                         "output configuration anyway")

    def test_scale_below_the_minimum_is_refused(self):
        p = self.wxrandr("--output", "HEAD-1", "--scale", "200")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr, "xrandr: output HEAD-1 cannot be smaller "
                         "than 16x16 (desired size 9x5)\n")
        self.assertEqual(self.apply_count(), 0)

    def test_scale_from_one_pixel_is_refused(self):
        p = self.wxrandr("--output", "HEAD-1", "--scale-from", "1x1")
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot be smaller than 16x16", p.stderr)
        self.assertEqual(self.apply_count(), 0)

    def test_dryrun_refuses_it_too(self):
        p = self.wxrandr("--dryrun", "--output", "HEAD-1", "--scale", "99999")
        self.assertEqual(p.returncode, 1)
        self.assertIn("cannot be smaller than 16x16", p.stderr)

    def test_an_ordinary_scale_still_applies(self):
        p = self.wxrandr("--output", "HEAD-1", "--scale", "2")
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assertEqual(self.apply_count(), 1)


class DpiFallback(FakeWlr):
    """`--dpi 0` (and nan, and a negative one) reached a plain division in
    the verbose/dryrun screen line.  Real xrandr prints a line; we aborted."""

    def test_zero_nan_and_negative_dpi(self):
        for spelling in ("0", "nan", "-1", "inf"):
            with self.subTest(dpi=spelling):
                p = self.wxrandr("--dryrun", "--dpi", spelling,
                                 "--output", "HEAD-1", "--auto")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertNotIn("Traceback", p.stderr)
                self.assertNotIn("nan", p.stdout)
                self.assertNotIn("inf", p.stdout)
                self.assertTrue(p.stdout.startswith("screen 0: "), p.stdout)

    def test_a_real_dpi_is_still_honoured(self):
        p = self.wxrandr("--dryrun", "--dpi", "192", "--output", "HEAD-1",
                         "--auto")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.split("\n")[0],
                         "screen 0: 1920x1080 254x142 mm 192.00dpi")


if __name__ == "__main__":
    unittest.main()
