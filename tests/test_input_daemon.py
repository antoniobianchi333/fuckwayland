"""Daemon tests: injection logic on recorder devices, plus the full
client<->daemon JSON-lines protocol over a real spawned daemon (fake uinput)."""

import contextlib
import io
import json
import os
import signal
import socket
import tempfile
import time
import unittest

from wdotool import daemon, keymap, uinput
from wdotool.ctx import CmdError


class RecorderDev:
    def __init__(self):
        self.events = []

    def emit(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append(("SYN",))

    def key(self, code, down):
        self.events.append(("KEY", code, 1 if down else 0))

    def close(self):
        pass


def make_daemon(geom=(0, 0, 1920, 1080), rel_abs=False):
    """A daemon on recorder devices. `rel_abs` pins the relative-move mode
    (B1) so no test depends on whether a sway socket happens to exist where
    it runs; False is the sway/i3 contract (REL events), True the warp."""
    d = daemon._Daemon()
    d.kb, d.mouse, d.tablet = RecorderDev(), RecorderDev(), RecorderDev()
    d.dev_error = None
    d.geom = geom
    d._rel_abs = rel_abs
    return d


def compositor_pixel(axis_value: int, span: int) -> int:
    """The pixel a tablet axis value lands on: libinput's scale_axis()
    (value * span / (max - min + 1)) truncated to a pixel."""
    return axis_value * span // 32768


def _close_quietly(fd):
    with contextlib.suppress(OSError):
        os.close(fd)


def _kill_quietly(pid):
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def abs_report(dev):
    """(ABS_X, ABS_Y) of the last report a recorder tablet emitted."""
    vals = {}
    for ev in dev.events:
        if ev[0] == uinput.EV_ABS:
            vals[ev[1]] = ev[2]
    return vals[uinput.ABS_X], vals[uinput.ABS_Y]


class TestInjectionLogic(unittest.TestCase):
    def test_key_press_order_and_release(self):
        d = make_daemon()
        d.op_key("ctrl+shift+t", "press", 0, False)
        self.assertEqual(d.kb.events, [
            ("KEY", 29, 1), ("KEY", 42, 1), ("KEY", 20, 1),
            ("KEY", 29, 0), ("KEY", 42, 0), ("KEY", 20, 0),
        ])
        self.assertEqual(d.down, set())

    def test_keydown_keyup_state(self):
        d = make_daemon()
        d.op_key("ctrl", "down", 0, False)
        self.assertEqual(d.down, {29})
        d.op_key("ctrl", "up", 0, False)
        self.assertEqual(d.down, set())

    def test_shifted_keysym_synthesizes_shift(self):
        d = make_daemon()
        d.op_key("A", "press", 0, False)
        self.assertEqual(d.kb.events, [
            ("KEY", 42, 1), ("KEY", 30, 1),
            ("KEY", 42, 0), ("KEY", 30, 0),
        ])

    def test_no_double_shift(self):
        d = make_daemon()
        d.op_key("shift+A", "press", 0, False)
        self.assertEqual(d.kb.events, [
            ("KEY", 42, 1), ("KEY", 30, 1),
            ("KEY", 42, 0), ("KEY", 30, 0),
        ])

    def test_clearmodifiers_releases_eight(self):
        d = make_daemon()
        d.down = {29, 42}
        d.op_key("a", "press", 0, True)
        ups = d.kb.events[:8]
        self.assertEqual({e[1] for e in ups}, set(keymap.MODIFIER_KEYCODES))
        self.assertTrue(all(e[0] == "KEY" and e[2] == 0 for e in ups))
        self.assertEqual(d.kb.events[8:], [("KEY", 30, 1), ("KEY", 30, 0)])

    def test_key_warnings(self):
        d = make_daemon()
        warns = d.op_key("ctrl+bogus+t", "press", 0, False)
        self.assertEqual(warns, ["(symbol) No such key name 'bogus'. Ignoring it."])

    def test_key_invalid_sequence_raises(self):
        d = make_daemon()
        with self.assertRaises(ValueError):
            d.op_key("ctrl-x", "press", 0, False)

    def test_type_shift_wrapping(self):
        d = make_daemon()
        warns = d.op_type("aA", 0, False)
        self.assertEqual(warns, [])
        self.assertEqual(d.kb.events, [
            ("KEY", 30, 1), ("KEY", 30, 0),
            ("KEY", 42, 1), ("KEY", 30, 1), ("KEY", 30, 0), ("KEY", 42, 0),
        ])

    def test_type_newline_tab(self):
        d = make_daemon()
        d.op_type("\n\t", 0, False)
        self.assertEqual(d.kb.events, [
            ("KEY", 28, 1), ("KEY", 28, 0),
            ("KEY", 15, 1), ("KEY", 15, 0),
        ])

    def test_type_unmapped_char_warns_and_skips(self):
        d = make_daemon()
        warns = d.op_type("aéb", 0, False)
        self.assertEqual(len(warns), 1)
        self.assertIn("é", warns[0])
        self.assertEqual([e for e in d.kb.events if e[2] == 1],
                         [("KEY", 30, 1), ("KEY", 48, 1)])

    def test_mousemove_abs_scaling_and_tracking(self):
        # B7: ceil(x * 32768 / span), the exact inverse of the compositor's
        # floor(v * span / 32768).
        d = make_daemon()
        d.op_mousemove_abs(100, 200, [])
        self.assertEqual((d.px, d.py), (100, 200))
        self.assertEqual(d.tablet.events, [
            (uinput.EV_ABS, uinput.ABS_X, -((-100 * 32768) // 1920)),
            (uinput.EV_ABS, uinput.ABS_Y, -((-200 * 32768) // 1080)),
            ("SYN",),
        ])

    def test_mousemove_abs_clamps(self):
        d = make_daemon()
        d.op_mousemove_abs(99999, -5, [])
        self.assertEqual((d.px, d.py), (1919, 0))
        ax, ay = abs_report(d.tablet)
        self.assertEqual(compositor_pixel(ax, 1920), 1919)
        self.assertEqual(ay, 0)

    def test_mousemove_rel_emits_rel_on_sway(self):
        """sway/i3 keep the REL path (the rig runs pointer_accel 0)."""
        d = make_daemon(rel_abs=False)
        d.px, d.py = 10, 10
        d.op_mousemove_rel(-50, 5, [])
        self.assertEqual((d.px, d.py), (0, 15))
        self.assertEqual(d.mouse.events, [
            (uinput.EV_REL, uinput.REL_X, -50),
            (uinput.EV_REL, uinput.REL_Y, 5),
            ("SYN",),
        ])

    def test_buttons(self):
        d = make_daemon()
        d.op_button(1, True)
        d.op_button(1, False)
        d.op_button(3, True)
        self.assertEqual(d.mouse.events, [
            ("KEY", uinput.BTN_LEFT, 1), ("KEY", uinput.BTN_LEFT, 0),
            ("KEY", uinput.BTN_RIGHT, 1),
        ])

    def test_wheel_buttons(self):
        d = make_daemon()
        d.op_button(4, True)
        d.op_button(4, False)  # no-op
        d.op_button(5, True)
        d.op_button(7, True)
        self.assertEqual(d.mouse.events, [
            (uinput.EV_REL, uinput.REL_WHEEL, 1), ("SYN",),
            (uinput.EV_REL, uinput.REL_WHEEL, -1), ("SYN",),
            (uinput.EV_REL, uinput.REL_HWHEEL, 1), ("SYN",),
        ])

    def test_invalid_button(self):
        d = make_daemon()
        with self.assertRaises(RuntimeError):
            d.op_button(42, True)

    def test_click_sequence(self):
        d = make_daemon()
        d.op_click(1, 2, 0)
        self.assertEqual(d.mouse.events, [
            ("KEY", uinput.BTN_LEFT, 1), ("KEY", uinput.BTN_LEFT, 0),
            ("KEY", uinput.BTN_LEFT, 1), ("KEY", uinput.BTN_LEFT, 0),
        ])

    def test_no_devices_error(self):
        # _need_devices retries create_devices; point it at a missing node so
        # the retry deterministically fails no matter where the tests run.
        os.environ["WDOTOOL_UINPUT_PATH"] = "/nonexistent/wdotool-uinput"
        self.addCleanup(os.environ.pop, "WDOTOOL_UINPUT_PATH", None)
        d = daemon._Daemon()
        d.geom = (0, 0, 1920, 1080)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(RuntimeError):
                d.op_button(1, True)


class TestPointerMapping(unittest.TestCase):
    """B1/B2/B7: where an injected move actually lands."""

    def test_abs_round_trips_through_the_compositor_inverse(self):
        """B7: every x must survive daemon -> tablet -> compositor. The old
        (x-gx)*32767//(w-1) map lost a pixel wherever the division was
        inexact: 257 of 301 x values near the origin of the 3x1920 rig."""
        for geom in ((0, 0, 5760, 1080), (0, 0, 1920, 1080),
                     (-1920, 0, 3200, 1080), (100, 50, 800, 600)):
            gx, gy, w, h = geom
            d = make_daemon(geom)
            probes = (list(range(gx, gx + 400))                   # origin head
                      + list(range(gx + w - 400, gx + w))         # far edge
                      + list(range(gx + w // 3 - 30, gx + w // 3 + 30)))
            for x in probes:
                d.tablet.events.clear()
                d.op_mousemove_abs(x, gy, [])
                ax, _ = abs_report(d.tablet)
                self.assertEqual(gx + compositor_pixel(ax, w), x,
                                 "geom=%r x=%d axis=%d" % (geom, x, ax))
            for y in (gy, gy + 1, gy + h // 2, gy + h - 2, gy + h - 1):
                d.tablet.events.clear()
                d.op_mousemove_abs(gx, y, [])
                _, ay = abs_report(d.tablet)
                self.assertEqual(gy + compositor_pixel(ay, h), y,
                                 "geom=%r y=%d axis=%d" % (geom, y, ay))

    def test_axis_values_stay_in_range(self):
        for span in (1, 2, 800, 1920, 5760, 32768, 65536):
            for delta in (0, 1, span // 2, span - 1):
                v = daemon._Daemon._axis(delta, span)
                self.assertTrue(0 <= v <= 32767, (span, delta, v))

    def test_repeated_absolute_move_is_nudged(self):
        """B2: the kernel drops an EV_ABS whose value has not changed, so
        `mousemove X Y` twice used to be a silent no-op the second time."""
        d = make_daemon()
        d.op_mousemove_abs(1500, 700, [])
        first = list(d.tablet.events)
        d.tablet.events.clear()
        d.op_mousemove_abs(1500, 700, [])
        ax, ay = abs_report(d.tablet)
        # a nudged X on its own report, then the real coordinates
        self.assertEqual(d.tablet.events[0][:2], (uinput.EV_ABS, uinput.ABS_X))
        self.assertNotEqual(d.tablet.events[0][2], ax)
        self.assertEqual(abs(d.tablet.events[0][2] - ax), 1)
        self.assertEqual(d.tablet.events[1], ("SYN",))
        self.assertEqual(d.tablet.events[2:], first)
        self.assertEqual((d.px, d.py), (1500, 700))

    def test_absolute_move_after_relative_still_warps(self):
        """B2, the field case: mousemove 1500 700; mousemove_relative 100 0;
        mousemove 1500 700 must put the pointer back."""
        d = make_daemon(rel_abs=False)
        d.op_mousemove_abs(1500, 700, [])
        d.op_mousemove_rel(100, 0, [])          # REL: tablet axes unchanged
        d.tablet.events.clear()
        d.op_mousemove_abs(1500, 700, [])
        self.assertEqual(d.tablet.events[0][:2], (uinput.EV_ABS, uinput.ABS_X))
        self.assertEqual(d.tablet.events[1], ("SYN",))
        ax, ay = abs_report(d.tablet)
        self.assertEqual((compositor_pixel(ax, 1920), compositor_pixel(ay, 1080)),
                         (1500, 700))

    def test_first_move_to_the_origin_is_nudged(self):
        """A fresh uinput device starts at axis 0,0, so a first
        `mousemove 0 0` would be dropped without the nudge."""
        d = make_daemon()
        d.op_mousemove_abs(0, 0, [])
        self.assertEqual(d.tablet.events[0], (uinput.EV_ABS, uinput.ABS_X, 1))
        self.assertEqual(d.tablet.events[1], ("SYN",))
        self.assertEqual(abs_report(d.tablet), (0, 0))
        self.assertEqual((d.px, d.py), (0, 0))

    def test_nudge_at_the_far_edge_goes_down(self):
        d = make_daemon()
        d.op_mousemove_abs(1919, 1079, [])
        top = abs_report(d.tablet)
        d.tablet.events.clear()
        d.op_mousemove_abs(1919, 1079, [])
        self.assertEqual(d.tablet.events[0][2], top[0] + (-1 if top[0] == 32767 else 1))

    def test_relative_move_warps_by_default(self):
        """B1: REL events go through libinput's acceleration curve, so a
        relative move is emitted as an absolute warp to the target."""
        d = make_daemon(rel_abs=True)
        d.op_mousemove_abs(1200, 601, [])
        d.tablet.events.clear()
        d.op_mousemove_rel(500, 0, [])
        self.assertEqual((d.px, d.py), (1700, 601))
        self.assertEqual(d.mouse.events, [])  # no REL_X/REL_Y at all
        ax, ay = abs_report(d.tablet)
        self.assertEqual((compositor_pixel(ax, 1920), compositor_pixel(ay, 1080)),
                         (1700, 601))

    def test_relative_warp_clamps_to_the_layout(self):
        d = make_daemon((0, 0, 5760, 1080), rel_abs=True)
        d.op_mousemove_abs(5000, 500, [])
        d.op_mousemove_rel(9999, -9999, [])
        self.assertEqual((d.px, d.py), (5759, 0))
        ax, ay = abs_report(d.tablet)
        self.assertEqual(compositor_pixel(ax, 5760), 5759)
        self.assertEqual(compositor_pixel(ay, 1080), 0)

    def test_rel_mode_selection(self):
        env = os.environ.get("WDOTOOL_REL_MODE")
        sway = os.environ.get("SWAYSOCK")
        self.addCleanup(lambda: (os.environ.__setitem__("WDOTOOL_REL_MODE", env)
                                 if env is not None else
                                 os.environ.pop("WDOTOOL_REL_MODE", None)))
        self.addCleanup(lambda: (os.environ.__setitem__("SWAYSOCK", sway)
                                 if sway is not None else
                                 os.environ.pop("SWAYSOCK", None)))
        for value, want in (("abs", True), ("warp", True), ("rel", False),
                            ("relative", False)):
            os.environ["WDOTOOL_REL_MODE"] = value
            d = daemon._Daemon()
            self.assertIs(d._rel_absolute(), want, value)
        # no override: sway/i3 keep REL, everything else warps
        os.environ.pop("WDOTOOL_REL_MODE", None)
        with tempfile.NamedTemporaryFile(prefix="wdotool-swaysock-") as sock:
            os.environ["SWAYSOCK"] = sock.name
            self.assertIs(daemon._Daemon()._rel_absolute(), False)
        os.environ["SWAYSOCK"] = "/nonexistent/wdotool-no-sway"
        self.assertIs(daemon._Daemon()._rel_absolute(), True)

class TestProtocol(unittest.TestCase):
    """Full client<->daemon protocol against a really spawned (forked) daemon."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="wdotool-test-")
        cls.env_backup = {
            k: os.environ.get(k)
            for k in ("XDG_RUNTIME_DIR", "WDOTOOL_UINPUT_PATH", "WDOTOOL_FAKE_UINPUT",
                      "WAYLAND_DISPLAY", "SWAYSOCK", "I3SOCK")
        }
        os.environ["XDG_RUNTIME_DIR"] = cls.tmp.name
        os.environ["WDOTOOL_UINPUT_PATH"] = "/dev/null"
        os.environ["WDOTOOL_FAKE_UINPUT"] = "1"
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ.pop("SWAYSOCK", None)
        os.environ.pop("I3SOCK", None)

        # leave a stale socket behind to exercise cleanup
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(daemon.socket_path())
        stale.close()

        # spawn under a fully permissive umask: the daemon must still bind an
        # owner-only (0600) socket
        old_umask = os.umask(0o000)
        try:
            cls.client = daemon.DaemonClient.connect_or_spawn()
        finally:
            os.umask(old_umask)
        cls.pid = cls.client._rpc(op="ping")["pid"]

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        with contextlib.suppress(OSError):
            os.kill(cls.pid, signal.SIGTERM)
        for k, v in cls.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls.tmp.cleanup()

    def test_geometry_fallback(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(self.client.geometry(), (1920, 1080))

    def test_geometry_full_carries_origin(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(self.client.geometry_full(), (0, 0, 1920, 1080))

    def test_socket_mode_is_0600(self):
        # owner-only regardless of the spawning client's umask
        mode = os.stat(daemon.socket_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_malformed_json_values_keep_connection(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(daemon.socket_path())
        self.addCleanup(sock.close)
        rfile = sock.makefile("r")
        for req in (b"5\n",
                    b'{"op": "mousemove_abs", "x": null, "y": null}\n',
                    b'{"op": "click", "btn": 1, "repeat": 99999999999}\n'):
            sock.sendall(req)
            self.assertFalse(json.loads(rfile.readline())["ok"])
        sock.sendall(b'{"op": "ping"}\n')
        self.assertEqual(json.loads(rfile.readline())["pid"], self.pid)

    def test_pointer_tracking_roundtrip(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.client.mousemove_abs(300, 400)
            self.assertEqual(self.client.pointer(), (300, 400))
            self.client.mousemove_rel(-100, 50)
            self.assertEqual(self.client.pointer(), (200, 450))
            self.client.mousemove_abs(99999, 99999)
            self.assertEqual(self.client.pointer(), (1919, 1079))

    def test_key_and_type(self):
        self.client.key("ctrl+shift+t", "press", 0, False)
        self.client.key("shift", "down", 0, False)
        self.client.key("shift", "up", 0, False)
        self.client.type_text("Hello, world!\n", 0)

    def test_key_warning_printed_to_stderr(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.client.key("ctrl+bogus", "press", 0, False)
        self.assertEqual(stderr.getvalue(),
                         "(symbol) No such key name 'bogus'. Ignoring it.\n")

    def test_invalid_key_sequence_is_cmderror(self):
        with self.assertRaises(CmdError) as cm:
            self.client.key("ctrl-x", "press", 0, False)
        self.assertTrue(str(cm.exception).startswith("Error: Invalid key sequence"))

    def test_click_and_buttons(self):
        self.client.click(1, 1, 0)
        self.client.click(3, 2, 0)
        self.client.button(2, True)
        self.client.button(2, False)
        with self.assertRaises(CmdError):
            self.client.button(77, True)

    def test_garbage_line_gets_error_response(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(daemon.socket_path())
        sock.sendall(b"this is not json\n")
        resp = json.loads(sock.makefile("r").readline())
        self.assertFalse(resp["ok"])
        sock.close()

    def test_unknown_op(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(daemon.socket_path())
        sock.sendall(b'{"op": "frobnicate"}\n')
        resp = json.loads(sock.makefile("r").readline())
        self.assertFalse(resp["ok"])
        self.assertIn("frobnicate", resp["error"])
        sock.close()

    def test_second_client_reaches_same_daemon(self):
        c2 = daemon.DaemonClient.connect_or_spawn()
        self.addCleanup(c2.close)
        self.assertEqual(c2._rpc(op="ping")["pid"], self.pid)

    def test_concurrent_clients(self):
        clients = [daemon.DaemonClient.connect_or_spawn() for _ in range(4)]
        for c in clients:
            self.addCleanup(c.close)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for n, c in enumerate(clients):
                c.mousemove_abs(n, n)
            for c in clients:
                self.assertEqual(len(c.pointer()), 2)


if __name__ == "__main__":
    unittest.main()
