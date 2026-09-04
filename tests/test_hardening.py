"""Hardening regressions: multi-output geometry origins, daemon request
robustness and DoS bounds, partial device creation retry, XF86 keysyms,
full keycode registration, and backend parsing fixes."""

import contextlib
import errno
import io
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock

from wdotool import daemon, keymap, uinput
from wdotool.keysyms import NAME_TO_KEYSYM

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

# B13: the injection tests pin the *fixed US table* as the source of
# keycodes. Without this a developer running the suite inside a German or
# Dvorak session would have the daemon read that session's real keymap and
# type through it, and every keycode assertion here would be wrong.
os.environ.setdefault("WDOTOOL_LAYOUT", "us")


class RecorderDev:
    def __init__(self):
        self.events = []
        self.closed = False

    def emit(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append(("SYN",))

    def key(self, code, down):
        self.events.append(("KEY", code, 1 if down else 0))

    def close(self):
        self.closed = True


def make_daemon(geom=(0, 0, 1920, 1080), rel_abs=False):
    d = daemon._Daemon()
    d.kb, d.mouse, d.tablet = RecorderDev(), RecorderDev(), RecorderDev()
    d.dev_error = None
    d.geom = geom
    # Pin the relative-move mode so these tests never depend on whether a
    # sway socket happens to exist where they run (see B1 / _rel_absolute).
    d._rel_abs = rel_abs
    return d


# ---------------------------------------------------------------------------
# multi-output bounding boxes with non-zero/negative origins


class TestBBox(unittest.TestCase):
    def test_single_output_at_origin(self):
        self.assertEqual(daemon._bbox_of([(0, 0, 1280, 720)]), (0, 0, 1280, 720))

    def test_negative_x_origin(self):
        boxes = [(0, 0, 1280, 720), (-1920, 0, 1920, 1080)]
        self.assertEqual(daemon._bbox_of(boxes), (-1920, 0, 3200, 1080))

    def test_positive_offset_origin(self):
        boxes = [(100, 50, 800, 600)]
        self.assertEqual(daemon._bbox_of(boxes), (100, 50, 800, 600))

    def test_vertical_stack_negative_y(self):
        boxes = [(0, -1080, 1920, 1080), (0, 0, 1920, 1080)]
        self.assertEqual(daemon._bbox_of(boxes), (0, -1080, 1920, 2160))


def compositor_x(axis_value: int, span: int) -> int:
    """libinput scale_axis() + the compositor's pixel truncation: the pixel a
    tablet axis value lands on is floor(v * span / (max - min + 1))."""
    return axis_value * span // 32768


class TestMultiOutputPointer(unittest.TestCase):
    GEOM = (-1920, 0, 3200, 1080)  # HEADLESS-2 at -1920,0 + 1280x720... etc.

    def _abs_values(self, d):
        """(ABS_X, ABS_Y) of the last report the tablet emitted."""
        vals = {}
        for ev in d.tablet.events:
            if ev[0] == uinput.EV_ABS:
                vals[ev[1]] = ev[2]
        return vals[uinput.ABS_X], vals[uinput.ABS_Y]

    def test_abs_maps_layout_origin_to_zero(self):
        d = make_daemon(self.GEOM)
        d.op_mousemove_abs(-1920, 0, [])
        self.assertEqual((d.px, d.py), (-1920, 0))
        self.assertEqual(self._abs_values(d), (0, 0))

    def test_abs_maps_layout_max_to_last_pixel(self):
        # B7: the far edge need not be exactly 32767 -- what matters is that
        # the compositor's inverse maps it back to the last pixel.
        d = make_daemon(self.GEOM)
        d.op_mousemove_abs(1279, 1079, [])
        self.assertEqual((d.px, d.py), (1279, 1079))
        ax, ay = self._abs_values(d)
        self.assertEqual(compositor_x(ax, 3200), 3199)
        self.assertEqual(compositor_x(ay, 1080), 1079)

    def test_abs_round_trips_on_every_head(self):
        """B7: every global x must survive daemon -> tablet -> compositor.
        The old (x-gx)*32767//(w-1) floor map lost a pixel wherever the
        division was inexact (257 of 301 x values near the origin on the
        3-head rig)."""
        for geom in ((-1920, 0, 3200, 1080),      # two heads, negative origin
                     (0, 0, 5760, 1080),          # the 3x1920 stress rig
                     (0, 0, 1920, 1080),
                     (100, 50, 800, 600)):
            gx, gy, w, h = geom
            d = make_daemon(geom)
            xs = (list(range(gx, gx + 320))
                  + list(range(gx + w - 320, gx + w))
                  + list(range(gx + w // 2 - 40, gx + w // 2 + 40)))
            for x in xs:
                d.tablet.events.clear()
                d.op_mousemove_abs(x, gy, [])
                ax, _ay = self._abs_values(d)
                self.assertEqual(gx + compositor_x(ax, w), x,
                                 "geom %r x=%d axis=%d" % (geom, x, ax))

    def test_abs_zero_is_scaled_from_origin(self):
        d = make_daemon(self.GEOM)
        d.op_mousemove_abs(0, 0, [])
        ax, _ay = self._abs_values(d)
        self.assertEqual(ax, -((-1920 * 32768) // 3200))
        self.assertEqual(compositor_x(ax, 3200), 1920)

    def test_abs_clamps_at_negative_edge(self):
        d = make_daemon(self.GEOM)
        d.op_mousemove_abs(-99999, -99999, [])
        self.assertEqual((d.px, d.py), (-1920, 0))
        d.op_mousemove_abs(99999, 99999, [])
        self.assertEqual((d.px, d.py), (1279, 1079))

    def test_rel_clamps_at_negative_edge(self):
        d = make_daemon(self.GEOM)
        d.px, d.py = -1910, 5
        d.op_mousemove_rel(-100, -100, [])
        self.assertEqual((d.px, d.py), (-1920, 0))


# ---------------------------------------------------------------------------
# request validation / robustness (in-process serve_client over a socketpair)


class TestServeClient(unittest.TestCase):
    def serve(self, d):
        a, b = socket.socketpair()
        threading.Thread(target=d.serve_client, args=(b,), daemon=True).start()
        self.addCleanup(a.close)
        return a, a.makefile("r", encoding="utf-8")

    def test_malformed_requests_keep_serving(self):
        d = make_daemon()
        sock, rfile = self.serve(d)
        battery = [
            b"this is not json\n",
            b"5\n",
            b'"just a string"\n',
            b"[1, 2, 3]\n",
            b'{"op": "mousemove_abs", "x": null, "y": null}\n',
            b'{"op": "mousemove_abs", "x": "40", "y": []}\n',
            b'{"op": "key"}\n',
            b'{"op": "type", "text": 7}\n',
            b'{"op": "button", "btn": true, "down": true}\n',
            b'{"op": "click", "btn": 1, "repeat": "many"}\n',
        ]
        for req in battery:
            sock.sendall(req)
            resp = json.loads(rfile.readline())
            self.assertFalse(resp["ok"], f"{req!r} unexpectedly succeeded")
            self.assertIn("error", resp)
        # the connection (and daemon) must still serve after all of that
        sock.sendall(b'{"op": "ping"}\n')
        self.assertTrue(json.loads(rfile.readline())["ok"])
        sock.sendall(b'{"op": "mousemove_abs", "x": 10, "y": 20}\n')
        self.assertTrue(json.loads(rfile.readline())["ok"])
        self.assertEqual((d.px, d.py), (10, 20))

    def test_dos_bounds(self):
        d = make_daemon()
        sock, rfile = self.serve(d)
        for req, field in [
            (b'{"op": "click", "btn": 1, "repeat": 999999999}\n', "repeat"),
            (b'{"op": "click", "btn": 1, "delay_ms": 999999999}\n', "delay_ms"),
            (b'{"op": "key", "spec": "a", "delay_ms": 400000}\n', "delay_ms"),
            (b'{"op": "type", "text": "a", "delay_ms": -5}\n', "delay_ms"),
            (b'{"op": "mousemove_abs", "x": 4000000000, "y": 0}\n', "x"),
        ]:
            sock.sendall(req)
            resp = json.loads(rfile.readline())
            self.assertFalse(resp["ok"], f"{req!r} unexpectedly succeeded")
            self.assertIn(field, resp["error"])
        # bounds are inclusive: the documented maxima still work
        sock.sendall(b'{"op": "click", "btn": 1, "repeat": 3, "delay_ms": 0}\n')
        self.assertTrue(json.loads(rfile.readline())["ok"])

    def test_oversized_request_line(self):
        d = make_daemon()
        with mock.patch.object(daemon, "_MAX_REQUEST", 64):
            sock, rfile = self.serve(d)
            sock.sendall(b'{"op": "ping", "pad": "' + b"x" * 500 + b'"}\n')
            resp = json.loads(rfile.readline())
            self.assertFalse(resp["ok"])
            self.assertIn("too large", resp["error"])
            sock.sendall(b'{"op": "ping"}\n')
            self.assertTrue(json.loads(rfile.readline())["ok"])

    def test_handle_rejects_non_dict(self):
        d = make_daemon()
        resp = d.handle(5)
        self.assertFalse(resp["ok"])

    def test_geometry_response_carries_origin(self):
        d = make_daemon((-1920, 0, 3200, 1080))
        resp = d.handle({"op": "geometry"})
        self.assertEqual((resp["x"], resp["y"], resp["w"], resp["h"]),
                         (-1920, 0, 3200, 1080))


# ---------------------------------------------------------------------------
# partial device creation: close what was made, retry on the next request


class TestDeviceRetry(unittest.TestCase):
    def test_partial_failure_closes_created_devices(self):
        d = daemon._Daemon()
        kb = RecorderDev()
        with mock.patch.object(uinput, "keyboard", return_value=kb), \
             mock.patch.object(uinput, "rel_mouse",
                               side_effect=OSError(errno.EPERM, "denied")), \
             contextlib.redirect_stderr(io.StringIO()):
            d.create_devices()
        self.assertTrue(kb.closed)
        self.assertIsNone(d.kb)
        self.assertIsNone(d.mouse)
        self.assertIsNone(d.tablet)
        self.assertIn("uinput", d.dev_error)

    def test_broken_daemon_retries_and_recovers(self):
        d = daemon._Daemon()
        with mock.patch.object(uinput, "keyboard",
                               side_effect=OSError(errno.EPERM, "denied")), \
             contextlib.redirect_stderr(io.StringIO()):
            d.create_devices()
        self.assertIsNotNone(d.dev_error)
        # next request retries device creation and succeeds this time
        kb, ms, tb = RecorderDev(), RecorderDev(), RecorderDev()
        with mock.patch.object(uinput, "keyboard", return_value=kb), \
             mock.patch.object(uinput, "rel_mouse", return_value=ms), \
             mock.patch.object(uinput, "abs_pointer", return_value=tb), \
             mock.patch.dict(os.environ, {"WDOTOOL_FAKE_UINPUT": "1"}):
            d._need_devices()
        self.assertIsNone(d.dev_error)
        self.assertIs(d.kb, kb)
        d.op_button(1, True)  # and it injects
        self.assertEqual(ms.events, [("KEY", uinput.BTN_LEFT, 1)])

    def test_still_broken_retry_raises_cleanly(self):
        d = daemon._Daemon()
        os.environ["WDOTOOL_UINPUT_PATH"] = "/nonexistent/wdotool-uinput"
        self.addCleanup(os.environ.pop, "WDOTOOL_UINPUT_PATH", None)
        with contextlib.redirect_stderr(io.StringIO()):
            d.create_devices()
            first_err = d.dev_error
            with self.assertRaises(RuntimeError):
                d._need_devices()
        self.assertEqual(d.dev_error, first_err)


# ---------------------------------------------------------------------------
# XF86 keysyms


class TestXF86Keysyms(unittest.TestCase):
    REQUIRED = {
        "XF86AudioMute": 113, "XF86AudioLowerVolume": 114,
        "XF86AudioRaiseVolume": 115, "XF86AudioMicMute": 248,
        "XF86AudioPlay": 164, "XF86AudioPause": 201, "XF86AudioNext": 163,
        "XF86AudioPrev": 165, "XF86AudioStop": 166,
        "XF86MonBrightnessUp": 225, "XF86MonBrightnessDown": 224,
        "XF86Back": 158, "XF86Forward": 159, "XF86Refresh": 173,
        "XF86Stop": 128, "XF86Search": 217, "XF86HomePage": 172,
        "XF86Reload": 173, "XF86Mail": 155, "XF86Calculator": 140,
        "XF86Explorer": 144, "XF86Tools": 171, "XF86Favorites": 156,
        "XF86MyComputer": 157, "XF86PowerOff": 116, "XF86Sleep": 142,
        "XF86WakeUp": 143, "XF86ScreenSaver": 152, "XF86WWW": 150,
        "XF86Display": 227, "XF86KbdBrightnessUp": 230,
        "XF86KbdBrightnessDown": 229, "XF86Eject": 162,
    }

    def test_generated_table_has_xf86_names(self):
        self.assertEqual(NAME_TO_KEYSYM["XF86AudioMute"], 0x1008FF12)
        self.assertEqual(NAME_TO_KEYSYM["XF86AudioRaiseVolume"], 0x1008FF13)
        self.assertEqual(NAME_TO_KEYSYM["XF86MonBrightnessUp"], 0x1008FF02)
        self.assertGreater(
            sum(1 for n in NAME_TO_KEYSYM if n.startswith("XF86")), 300)

    def test_required_names_resolve_to_evdev_codes(self):
        for name, code in self.REQUIRED.items():
            self.assertEqual(keymap.resolve_token(name), (code, False), name)

    def test_evdevk_range_resolves_automatically(self):
        # XF86MediaPlayPause is _EVDEVK(0x0a4): keysym embeds KEY_PLAYPAUSE
        self.assertEqual(NAME_TO_KEYSYM["XF86MediaPlayPause"], 0x100810A4)
        self.assertEqual(keymap.resolve_token("XF86MediaPlayPause"), (164, False))

    def test_unknown_xf86_name_still_warns(self):
        hit = keymap.resolve_token("XF86NoSuchKeyEver")
        self.assertIsInstance(hit, str)
        self.assertIn("No such key name", hit)

    def test_parse_keyseq_with_xf86(self):
        keys, warnings = keymap.parse_keyseq("XF86AudioMute")
        self.assertEqual(keys, [(113, False)])
        self.assertEqual(warnings, [])


# ---------------------------------------------------------------------------
# keyboard registers every keycode the keymap can emit (1..255)


class TestKeycodeRegistration(unittest.TestCase):
    def test_numeric_keycodes_up_to_255_accepted(self):
        self.assertEqual(keymap.resolve_token("263"), (255, False))
        self.assertEqual(keymap.resolve_token("264"),
                         "key '264' is not reachable on the US layout. Ignoring it.")

    def test_keyboard_registers_1_to_255(self):
        fd, path = tempfile.mkstemp(prefix="wdotool-kbd-")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        recorded = []

        def fake_ioctl(fd_, req, arg=0):
            recorded.append((req, arg))
            return 0

        with mock.patch.dict(os.environ, {"WDOTOOL_UINPUT_PATH": path}), \
             mock.patch.object(uinput.fcntl, "ioctl", side_effect=fake_ioctl):
            os.environ.pop("WDOTOOL_FAKE_UINPUT", None)
            dev = uinput.keyboard()
            dev.close()
        keybits = [arg for req, arg in recorded if req == uinput.UI_SET_KEYBIT]
        self.assertEqual(keybits, list(range(1, 256)))


# ---------------------------------------------------------------------------
# wayland_mini: malformed message must raise, not spin


class TestWaylandMalformed(unittest.TestCase):
    def test_short_size_raises(self):
        from wdotool.wayland_mini import WlConn

        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        conn = WlConn.__new__(WlConn)
        conn.sock, conn.buf, conn.fds = b, b"", []
        conn.handlers, conn.dead = {}, None
        a.sendall(struct.pack("<II", 1, (4 << 16) | 0))  # size=4 < 8
        with self.assertRaises(RuntimeError):
            conn._dispatch_some()


# ---------------------------------------------------------------------------
# sway display_size bounding box


class TestSwayDisplaySize(unittest.TestCase):
    def size_for(self, outputs):
        from wdotool.backend_sway import SwayBackend

        b = SwayBackend.__new__(SwayBackend)
        b._msg = lambda *a, **k: outputs
        return b.display_size()

    def rect(self, x, y, w, h, active=True):
        return {"active": active,
                "rect": {"x": x, "y": y, "width": w, "height": h}}

    def test_single(self):
        self.assertEqual(self.size_for([self.rect(0, 0, 1280, 720)]), (1280, 720))

    def test_negative_origin(self):
        outs = [self.rect(0, 0, 1280, 720), self.rect(-1920, 0, 1920, 1080)]
        self.assertEqual(self.size_for(outs), (3200, 1080))

    def test_inactive_ignored(self):
        outs = [self.rect(0, 0, 1280, 720),
                self.rect(-9999, 0, 1920, 1080, active=False)]
        self.assertEqual(self.size_for(outs), (1280, 720))


if __name__ == "__main__":
    unittest.main()
