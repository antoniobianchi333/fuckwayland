#!/usr/bin/env python3
"""Wire-level hardening regressions: a compositor that stops answering, goes
away, or sends a short event must reach the user as one clear line and a
bounded wait -- never a hang and never a traceback.

Each test drives the real client against a mock that speaks the actual wire
format, so the guards are exercised, not mocked out."""

import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fwcommon.wayland_mini import Cursor, WlConn
from wl_fake import msg, wstr
from wdotool.ctx import CmdError


def _tmpsock(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    return os.path.join(d, "sock")


class _Server:
    """A listening AF_UNIX socket that runs `handler(conn)` per connection."""

    def __init__(self, handler):
        self.path = _tmpsock("wire-mock-")
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.bind(self.path)
        self.s.listen(4)
        self.handler = handler
        self.conns = []
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        try:
            self.handler(c)
        except OSError:
            pass

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


# -- wayland --------------------------------------------------------------

class BrokenCompositor(_Server):
    """Enough of wl_display/wl_registry for WlrBackend's constructor.

    `mode` picks the failure: "silent" (accept, then never answer), "closes"
    (go away after the globals), "shortmode" (a wl_output.mode event whose
    payload is empty)."""

    def __init__(self, mode):
        self.mode = mode
        super().__init__(self._serve)

    def _serve(self, c):
        buf = b""
        registry = None
        output_oid = None
        announced = False
        if self.mode == "silent":
            while True:
                time.sleep(0.5)
        while True:
            data = c.recv(65536)
            if not data:
                return
            buf += data
            while len(buf) >= 8:
                oid, so = struct.unpack_from("<II", buf)
                size, op = so >> 16, so & 0xFFFF
                if size < 8 or len(buf) < size:
                    break
                pay = buf[8:size]
                buf = buf[size:]
                if registry is not None and oid == registry and op == 0:
                    cur = Cursor(pay)          # bind(name, iface, ver, new_id)
                    cur.u32()
                    iface = cur.string()
                    cur.u32()
                    nid = cur.u32()
                    if iface == "wl_output":
                        output_oid = nid
                elif oid == 1 and op == 1:     # wl_display.get_registry
                    registry = struct.unpack_from("<I", pay)[0]
                    c.sendall(
                        msg(registry, 0, struct.pack("<I", 1)
                             + wstr("wl_output") + struct.pack("<I", 2))
                        + msg(registry, 0, struct.pack("<I", 2)
                               + wstr("zwlr_foreign_toplevel_manager_v1")
                               + struct.pack("<I", 3)))
                    announced = True
                elif oid == 1 and op == 0:     # wl_display.sync
                    cb = struct.unpack_from("<I", pay)[0]
                    if self.mode == "closes" and announced:
                        c.close()
                        return
                    if self.mode == "shortmode" and output_oid:
                        c.sendall(msg(output_oid, 1, b""))
                    c.sendall(msg(cb, 0, struct.pack("<I", 0)))


class WlConnDeadline(unittest.TestCase):
    def test_a_compositor_that_never_answers_does_not_hang(self):
        """WlConn used to leave the socket blocking, so roundtrip() waited
        forever with no message and no exit code."""
        srv = BrokenCompositor("silent")
        self.addCleanup(srv.close)
        c = WlConn(srv.path, timeout=0.4)
        self.addCleanup(c.close)
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            c.get_registry()
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_the_default_is_a_deadline_not_blocking(self):
        srv = BrokenCompositor("silent")
        self.addCleanup(srv.close)
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        self.assertEqual(c.sock.gettimeout(), WlConn.DEFAULT_TIMEOUT)

    def test_dispatch_restores_the_callers_timeout(self):
        """dispatch() used to restore None, leaving the connection blocking
        for every later read -- which kwin.py had to work around by hand."""
        srv = BrokenCompositor("silent")
        self.addCleanup(srv.close)
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        c.sock.settimeout(3.0)
        self.assertFalse(c.dispatch(0.1))
        self.assertEqual(c.sock.gettimeout(), 3.0)


class CursorBounds(unittest.TestCase):
    def test_a_truncated_string_does_not_leak_the_terminator(self):
        """The slice was max(n - 1, 0) bytes with no check that they exist,
        so a short payload returned whatever followed, NUL included."""
        cur = Cursor(struct.pack("<I", 8) + b"abc\0")
        self.assertEqual(cur.string(), "abc")

    def test_a_string_that_runs_off_the_payload_is_clamped(self):
        cur = Cursor(struct.pack("<I", 64) + b"hi\0")
        self.assertEqual(cur.string(), "hi")


class WlrBackendGuards(unittest.TestCase):
    def _backend(self, mode):
        srv = BrokenCompositor(mode)
        self.addCleanup(srv.close)
        old = os.environ.get("WAYLAND_DISPLAY")
        os.environ["WAYLAND_DISPLAY"] = srv.path
        self.addCleanup(lambda: os.environ.__setitem__("WAYLAND_DISPLAY", old)
                        if old is not None
                        else os.environ.pop("WAYLAND_DISPLAY", None))
        # The constructor raises with its connection half-built, so nothing
        # else will close it: an fd left to the collector prints a
        # ResourceWarning into whatever stderr a later test is capturing.
        from wdotool import backend_wlr
        made = []
        real = backend_wlr.WlConn

        def tracking(*a, **kw):
            c = real(*a, **kw)
            made.append(c)
            return c

        backend_wlr.WlConn = tracking

        def restore():
            backend_wlr.WlConn = real
            for c in made:
                try:
                    c.close()
                except OSError:
                    pass

        self.addCleanup(restore)
        return backend_wlr.WlrBackend

    def test_a_compositor_that_goes_away_is_one_line(self):
        """RuntimeError('wayland connection closed') used to escape
        backend_wlr and reach the user as a traceback."""
        WlrBackend = self._backend("closes")
        with self.assertRaises(CmdError) as cm:
            WlrBackend()
        self.assertIn("wlr backend:", str(cm.exception))

    def test_a_short_event_payload_is_one_line(self):
        """A wl_output.mode with an empty payload used to raise struct.error
        out of Cursor.u32()."""
        WlrBackend = self._backend("shortmode")
        with self.assertRaises(CmdError) as cm:
            WlrBackend()
        self.assertIn("wlr backend:", str(cm.exception))


# -- sway ------------------------------------------------------------------

_MAGIC = b"i3-ipc"


def iframe(mtype, payload):
    return _MAGIC + struct.pack("<II", len(payload), mtype) + payload


class FakeSway(_Server):
    """`mode` is "gone" (answer once, then the session ends), "badjson", or
    "wedged" (accept the connection and then never answer -- a compositor
    stuck in its own event loop; the kernel accepts for it)."""

    def __init__(self, mode):
        self.mode = mode
        super().__init__(self._serve)

    def _serve(self, c):
        n, buf = 0, b""
        if self.mode == "wedged":
            while True:
                time.sleep(0.5)
        while True:
            data = c.recv(65536)
            if not data:
                return
            buf += data
            while len(buf) >= 14:
                ln, mt = struct.unpack("<II", buf[6:14])
                if len(buf) < 14 + ln:
                    break
                buf = buf[14 + ln:]
                n += 1
                if self.mode == "badjson":
                    c.sendall(iframe(mt, b"{not json"))
                    continue
                c.sendall(iframe(mt, json.dumps(
                    [{"num": 3, "name": "3", "focused": True}]).encode()))
                if n == 1:
                    time.sleep(0.05)
                    c.close()
                    return


class SwayWireGuards(unittest.TestCase):
    def _backend(self, mode):
        srv = FakeSway(mode)
        self.addCleanup(srv.close)
        from wdotool.backend_sway import SwayBackend
        b = SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        return b

    def test_a_session_that_ends_mid_chain_is_one_line(self):
        """_read_exact guarded only a clean EOF: a peer that has gone gives
        ECONNRESET on the read and EPIPE on the next write, and both used to
        reach the user as a traceback."""
        b = self._backend("gone")
        b.get_desktop()                    # the one answer the mock gives
        with self.assertRaises(CmdError) as cm:
            for _ in range(3):
                b.get_desktop()
        self.assertIn("sway backend: lost the connection", str(cm.exception))

    def test_a_reply_that_is_not_json_is_one_line(self):
        b = self._backend("badjson")
        with self.assertRaises(CmdError) as cm:
            b.get_desktop()
        self.assertIn("sway backend: lost the connection", str(cm.exception))

    def test_a_compositor_that_never_answers_does_not_hang(self):
        """The command socket had no deadline at all, so every tool waited on
        a wedged sway for ever -- no timeout, no message, nothing to Ctrl-C
        out of but the tool itself."""
        srv = FakeSway("wedged")
        self.addCleanup(srv.close)
        from wdotool import backend_sway
        with mock.patch.object(backend_sway, "IPC_TIMEOUT", 0.4):
            b = backend_sway.SwayBackend(sockpath=srv.path)
            self.addCleanup(b.sock.close)
            self.assertEqual(b.sock.gettimeout(), 0.4)
            start = time.monotonic()
            with self.assertRaises(CmdError) as cm:
                b.get_desktop()
            waited = time.monotonic() - start
        self.assertIn("no answer from the compositor", str(cm.exception))
        self.assertIn("not responding", str(cm.exception))
        self.assertLess(waited, 5.0)

    def test_the_deadline_is_the_command_sockets_alone(self):
        """select_window() and wxprop's -spy subscribe on their own socket
        from _connect() and wait there for an event that may be minutes away:
        giving that one a deadline would break both."""
        srv = FakeSway("wedged")
        self.addCleanup(srv.close)
        from wdotool import backend_sway
        b = backend_sway.SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        self.assertEqual(b.sock.gettimeout(), backend_sway.IPC_TIMEOUT)
        s = b._connect()
        self.addCleanup(s.close)
        self.assertIsNone(s.gettimeout())


# -- D-Bus -----------------------------------------------------------------

from fwcommon import dbus_mini as D


def _raw_bus():
    """A Bus over a socketpair, built past authentication: the other end is
    a raw peer that can put any bytes on the wire."""
    ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    b = D.Bus.__new__(D.Bus)
    b.address = "unix:path=/dev/null"
    b.timeout = 5.0
    b.unique_name = ":1.99"
    b.guid = ""
    b.fds_ok = False
    b.auth_path = "direct"
    b.serve_calls = False
    b._serial = 0
    b._buf = bytearray()
    b._pending_fds = []
    b._waiting = set()
    b._replies = {}
    b._queue = __import__("collections").deque()
    b.sock = ours
    return b, theirs


def _reply_frame(to_serial, body_sig, body, endian=b"l"):
    m = D.Message(D.METHOD_RETURN, reply_serial=to_serial,
                  signature=body_sig, body=b"")
    out = bytearray(m.to_bytes(7)) + bytearray(body)
    struct.pack_into("<I", out, 4, len(body))
    if endian != b"l":
        out[0:1] = endian
    return bytes(out)


class DBusMalformedFrames(unittest.TestCase):
    """A peer that puts something impossible on the wire is a broken or
    hostile peer, not a bug in the caller: every consumer catches DBusError
    and nothing else, so a ValueError or a RecursionError out of the
    marshaller reached the user as a traceback."""

    def _peer_answers(self, make_body):
        bus, peer = _raw_bus()
        self.addCleanup(bus.close)
        self.addCleanup(peer.close)

        def serve():
            buf = b""
            while True:
                try:
                    d = peer.recv(65536)
                except OSError:
                    return
                if not d:
                    return
                buf += d
                while True:
                    n = D.Message.frame_length(buf)
                    if n is None or len(buf) < n:
                        break
                    m = D.Message.from_bytes(buf[:n])
                    buf = buf[n:]
                    try:
                        peer.sendall(make_body(m.serial))
                    except OSError:
                        return

        threading.Thread(target=serve, daemon=True).start()
        return bus

    def test_a_body_that_contradicts_its_signature_is_a_dbus_error(self):
        bus = self._peer_answers(
            lambda serial: _reply_frame(serial, "s", b"\x10\x00"))
        with self.assertRaises(D.DBusError) as cm:
            bus.call("org.example", "/", "org.example", "Thing", timeout=5.0)
        self.assertIn("signature", str(cm.exception))

    def test_an_impossible_endianness_byte_closes_the_connection(self):
        """frame_length() raised before the frame was consumed, so the bad
        frame stayed in the buffer and the parse loop spun on it."""
        bus = self._peer_answers(
            lambda serial: _reply_frame(serial, "y", b"\x01", endian=b"Z"))
        with self.assertRaises(D.DBusError) as cm:
            bus.call("org.example", "/", "org.example", "Thing", timeout=5.0)
        self.assertIn("malformed message", str(cm.exception))
        self.assertIsNone(bus.sock, "a stream we cannot resynchronise stays shut")

    def test_absurd_type_nesting_is_refused_not_a_stack_overflow(self):
        """1000 nested variants is 3 KiB on the wire and legal type nesting;
        the specification's limit is 32 arrays and 32 variants deep."""
        body = b"\x01v\x00" * 2000 + b"\x01y\x00" + b"\x2a"
        with self.assertRaises(ValueError) as cm:
            D.unmarshal("v", body, "<", (), False)
        self.assertIn("nesting", str(cm.exception))
        bus = self._peer_answers(lambda serial: _reply_frame(serial, "v", body))
        with self.assertRaises(D.DBusError):
            bus.call("org.example", "/", "org.example", "Thing", timeout=5.0)

    def test_a_body_within_the_limit_still_parses(self):
        body = b"\x01v\x00" * 40 + b"\x01y\x00" + b"\x2a"
        self.assertEqual(D.unmarshal("v", body, "<", (), False), (42,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
