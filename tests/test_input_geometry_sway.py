"""Integration test: output geometry against a real headless sway.

Skipped when sway is not on PATH (run inside `nix develop`). Starts its own
sway on a private XDG_RUNTIME_DIR so concurrent test runs don't collide.
"""

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(shutil.which("sway"), "sway not on PATH (run in nix develop)")
class TestSwayGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rtdir = tempfile.mkdtemp(prefix="wdotool-sway-")
        os.chmod(cls.rtdir, 0o700)
        conf = os.path.join(cls.rtdir, "sway.conf")
        with open(conf, "w") as f:
            f.write("output HEADLESS-1 mode 1280x720\n")
        env = dict(
            os.environ,
            XDG_RUNTIME_DIR=cls.rtdir,
            WLR_BACKENDS="headless",
            WLR_LIBINPUT_NO_DEVICES="1",
            # dodge the nixpkgs sway wrapper's dbus-run-session fallback
            DBUS_SESSION_BUS_ADDRESS=f"unix:path={cls.rtdir}/no-bus",
        )
        cls.sway = subprocess.Popen(
            ["sway", "-c", conf],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(n.startswith("wayland-") and not n.endswith(".lock")
                   for n in os.listdir(cls.rtdir)):
                break
            if cls.sway.poll() is not None:
                raise unittest.SkipTest("sway exited at startup")
            time.sleep(0.1)
        else:
            raise unittest.SkipTest("sway did not create a wayland socket")
        time.sleep(0.3)  # let the headless output appear

        cls.env_backup = {
            k: os.environ.get(k)
            for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "WDOTOOL_UINPUT_PATH",
                      "WDOTOOL_FAKE_UINPUT")
        }
        os.environ["XDG_RUNTIME_DIR"] = cls.rtdir
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ["WDOTOOL_UINPUT_PATH"] = "/dev/null"
        os.environ["WDOTOOL_FAKE_UINPUT"] = "1"

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.sway.send_signal(signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            cls.sway.wait(timeout=5)
        shutil.rmtree(cls.rtdir, ignore_errors=True)

    def test_wayland_bbox(self):
        from wdotool.daemon import _wayland_bbox

        self.assertEqual(_wayland_bbox(), (1280, 720))

    def test_daemon_geometry_and_clamping(self):
        from wdotool.daemon import DaemonClient

        client = DaemonClient.connect_or_spawn()
        self.addCleanup(client.close)
        pid = client._rpc(op="ping")["pid"]
        self.addCleanup(lambda: os.kill(pid, signal.SIGTERM))
        self.assertEqual(client.geometry(), (1280, 720))
        client.mousemove_abs(99999, 99999)
        self.assertEqual(client.pointer(), (1279, 719))
        client.mousemove_abs(0, 0)
        self.assertEqual(client.pointer(), (0, 0))


if __name__ == "__main__":
    unittest.main()
