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


def make_daemon():
    d = daemon._Daemon()
    d.kb, d.mouse, d.tablet = RecorderDev(), RecorderDev(), RecorderDev()
    d.dev_error = None
    d.geom = (1920, 1080)
    return d


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
        d = make_daemon()
        d.op_mousemove_abs(100, 200, [])
        self.assertEqual((d.px, d.py), (100, 200))
        self.assertEqual(d.tablet.events, [
            (uinput.EV_ABS, uinput.ABS_X, 100 * 32767 // 1919),
            (uinput.EV_ABS, uinput.ABS_Y, 200 * 32767 // 1079),
            ("SYN",),
        ])

    def test_mousemove_abs_clamps(self):
        d = make_daemon()
        d.op_mousemove_abs(99999, -5, [])
        self.assertEqual((d.px, d.py), (1919, 0))
        self.assertEqual(d.tablet.events[0], (uinput.EV_ABS, uinput.ABS_X, 32767))
        self.assertEqual(d.tablet.events[1], (uinput.EV_ABS, uinput.ABS_Y, 0))

    def test_mousemove_rel_tracks_and_clamps(self):
        d = make_daemon()
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
        d = daemon._Daemon()
        d.geom = (1920, 1080)
        with self.assertRaises(RuntimeError):
            d.op_button(1, True)


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

        cls.client = daemon.DaemonClient.connect_or_spawn()
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
