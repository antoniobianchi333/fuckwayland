"""zwlr_virtual_pointer_v1: the wire, the coordinates, and the policy.

Everything here runs against a **real Wayland socket** served by a fake
compositor that speaks the wire format (`FakeCompositor` below): the client is
`wdotool/wayland_mini.py` unmodified, and every assertion is about the bytes
the compositor received. A mock of our own client would have proved only that
we can call our own methods.

The compositor serves three heads, and they are the rig's own: one at a
negative origin, one at the origin, and one at scale 1.5 whose `wl_output`
scale is a *lie* (2) that only `zxdg_output_v1` corrects. That layout is what
makes the coordinate assertions worth anything -- absolute motion here is a
ratio over the whole layout bounding box in logical pixels, so a wrong origin,
a wrong extent or a per-output map would all land somewhere plausible on a
single 1920x1080 screen and nowhere near the target on this one.

The selection policy is tested in both directions, like the keyboard's: with
kernel devices available nothing may reach the protocol even where the
compositor offers it, and with none available the protocol is what moves.
"""

import os
import socket
import struct
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# every test file carries this itself: the suite is run file by file, where
# conftest.py never loads, and a tool that hands itself over would not be
# the code under test
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"
os.environ.setdefault("WDOTOOL_LAYOUT", "us")

from wdotool import daemon, uinput, vptr  # noqa: E402

# What a daemon that could not open /dev/uinput says today, and must keep
# saying when there is no protocol to fall back to either.
UINPUT_ERROR = ("cannot create uinput devices: [Errno 13] Permission denied: "
                "'/dev/uinput' (wdotool injects input via /dev/uinput; run it "
                "as root)")

# The rig, as three (logical x, y, w, h, wl_output scale, mode w, mode h).
# Virtual-3 is the scale-1.5 panel: 1920x1080 of pixels, 1280x720 logical,
# and a `wl_output.scale` of 2 because that event cannot carry 1.5.
HEADS = (
    ("Virtual-1", -1920, -540, 1920, 1080, 1, 1920, 1080),
    ("Virtual-2", 0, 0, 1920, 1080, 1, 1920, 1080),
    ("Virtual-3", 1920, 0, 1280, 720, 2, 1920, 1080),
)
BBOX = (-1920, -540, 5120, 1620)


# ---------------------------------------------------------------------------
# a compositor, on the wire


def _pad(n):
    return -n % 4


def _unpack_bind(body):
    name = struct.unpack_from("<I", body)[0]
    n = struct.unpack_from("<I", body, 4)[0]
    iface = body[8:8 + n - 1].decode()
    off = 8 + n + _pad(n)
    ver, new_id = struct.unpack_from("<II", body, off)
    return name, iface, ver, new_id


class FakeCompositor:
    """Enough of a wlroots compositor to be a virtual pointer's peer.

    Serves wl_display.sync/get_registry, wl_registry.bind, wl_seat, three
    wl_outputs with their zxdg_output_v1 logical geometry, and
    zwlr_virtual_pointer_manager_v1 + zwlr_virtual_pointer_v1, recording every
    request made on the pointer. Knobs:

      manager_version  None -> the global is not advertised at all (Mutter and
                       KWin: measured, they implement neither protocol)
      refuse_create    answer create_virtual_pointer with a protocol error
      with_seat        advertise a wl_seat at all (the seat argument is
                       allow-null in this protocol)
      with_xdg_output  advertise zxdg_output_manager_v1 (without it the
                       geometry falls back to wl_output, whose scale lies)
    """

    def __init__(self, manager_version=2, refuse_create=False, with_seat=True,
                 with_xdg_output=True):
        self.manager_version = manager_version
        self.refuse_create = refuse_create
        self.with_seat = with_seat
        self.with_xdg_output = with_xdg_output
        self.dir = tempfile.mkdtemp(prefix="wdotool-vp-")
        self.path = os.path.join(self.dir, "wayland-fake")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        # A blocking accept() is not woken by close() on Linux, so poll
        # instead: the suite creates one of these per test and must not pay a
        # join timeout for each.
        self.srv.settimeout(0.05)
        self.connections = 0
        self.mgr_name = None     # registry name the manager was advertised as
        self._registries = []    # (conn, registry id) per live client
        self.binds = []          # (interface, version)
        self.created = []        # (seat_id, pointer object id)
        self.with_output = []    # create_virtual_pointer_with_output (never)
        self.events = []         # every pointer request, in order
        self.destroyed = 0
        self._clients = []
        self._stop = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- lifecycle
    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass
        self._thread.join(timeout=5)
        self.drop_clients()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    def withdraw_manager(self):
        """wl_registry.global_remove for the manager, on every live
        connection -- a compositor withdrawing the protocol mid-session."""
        with self._lock:
            regs = list(self._registries)
        for conn, reg in regs:
            self._send(conn, reg, 1, struct.pack("<I", self.mgr_name or 0))

    def drop_clients(self):
        """Hang up on everyone -- what a compositor restart looks like to a
        client that was holding a virtual pointer."""
        with self._lock:
            socks = list(self._clients)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    # -- the server

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self._clients.append(conn)
            self.connections += 1
            threading.Thread(target=self._client, args=(conn,),
                             daemon=True).start()

    def _client(self, conn):
        buf = b""
        state = {"registry": None, "seat": None, "mgr": None, "vp": None,
                 "outputs": {}, "xdg": None}
        try:
            while not self._stop:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
                while len(buf) >= 8:
                    oid, sizeop = struct.unpack_from("<II", buf)
                    size, opcode = sizeop >> 16, sizeop & 0xFFFF
                    if size < 8 or len(buf) < size:
                        break
                    body, buf = buf[8:size], buf[size:]
                    self._request(conn, state, oid, opcode, body)
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # -- one request
    def _request(self, conn, state, oid, opcode, body):
        if oid == 1 and opcode == 0:            # wl_display.sync(callback)
            (cb,) = struct.unpack_from("<I", body)
            self._send(conn, cb, 0, struct.pack("<I", 0))   # callback.done
            self._send(conn, 1, 1, struct.pack("<I", cb))   # delete_id
            return
        if oid == 1 and opcode == 1:            # wl_display.get_registry
            (reg,) = struct.unpack_from("<I", body)
            state["registry"] = reg
            name = 1
            if self.with_seat:
                self._global(conn, reg, name, "wl_seat", 7)
                name += 1
            for _head in HEADS:
                self._global(conn, reg, name, "wl_output", 4)
                name += 1
            if self.with_xdg_output:
                self._global(conn, reg, name, "zxdg_output_manager_v1", 3)
                name += 1
            if self.manager_version is not None:
                self.mgr_name = name
                self._global(conn, reg, name, vptr.MANAGER,
                             self.manager_version)
            with self._lock:
                self._registries.append((conn, reg))
            return
        if oid == state["registry"] and opcode == 0:   # wl_registry.bind
            name, iface, ver, new_id = _unpack_bind(body)
            self.binds.append((iface, ver))
            if iface == "wl_seat":
                state["seat"] = new_id
                self._send(conn, new_id, 0, struct.pack("<I", 3))  # caps
            elif iface == "wl_output":
                head = HEADS[name - (2 if self.with_seat else 1)]
                state["outputs"][new_id] = head
                self._output_events(conn, new_id, head)
            elif iface == "zxdg_output_manager_v1":
                state["xdg"] = new_id
            elif iface == vptr.MANAGER:
                state["mgr"] = new_id
            return
        if oid == state["xdg"] and opcode == 1:  # get_xdg_output(id, output)
            new_id, out = struct.unpack_from("<II", body)
            head = state["outputs"].get(out)
            if head is not None:
                _n, lx, ly, lw, lh, _s, _mw, _mh = head
                self._send(conn, new_id, 0, struct.pack("<ii", lx, ly))
                self._send(conn, new_id, 1, struct.pack("<ii", lw, lh))
            return
        if oid == state["mgr"] and opcode in (0, 2):
            if opcode == 0:                     # create_virtual_pointer
                seat, new_id = struct.unpack_from("<II", body)
            else:                               # ...with_output (v2)
                seat, _out, new_id = struct.unpack_from("<III", body)
                self.with_output.append(new_id)
            if self.refuse_create:
                self._error(conn, oid, 0, "no virtual pointer for you")
                return
            state["vp"] = new_id
            self.created.append((seat, new_id))
            self.events.append(("create", new_id))
            return
        if state["vp"] is not None and oid == state["vp"]:
            self._pointer_request(opcode, body)

    def _pointer_request(self, opcode, body):
        if opcode == vptr._VP_MOTION:
            t, dx, dy = struct.unpack_from("<Iii", body)
            self.events.append(("motion", dx / 256.0, dy / 256.0))
        elif opcode == vptr._VP_MOTION_ABSOLUTE:
            _t, x, y, xe, ye = struct.unpack_from("<IIIII", body)
            self.events.append(("motion_absolute", x, y, xe, ye))
        elif opcode == vptr._VP_BUTTON:
            _t, btn, st = struct.unpack_from("<III", body)
            self.events.append(("button", btn, st))
        elif opcode == vptr._VP_AXIS:
            _t, ax, val = struct.unpack_from("<IIi", body)
            self.events.append(("axis", ax, val / 256.0))
        elif opcode == vptr._VP_FRAME:
            self.events.append(("frame",))
        elif opcode == vptr._VP_AXIS_SOURCE:
            (src,) = struct.unpack_from("<I", body)
            self.events.append(("axis_source", src))
        elif opcode == vptr._VP_AXIS_STOP:
            _t, ax = struct.unpack_from("<II", body)
            self.events.append(("axis_stop", ax))
        elif opcode == vptr._VP_AXIS_DISCRETE:
            _t, ax, val, disc = struct.unpack_from("<IIii", body)
            self.events.append(("axis_discrete", ax, val / 256.0, disc))
        elif opcode == vptr._VP_DESTROY:
            self.destroyed += 1
            self.events.append(("destroy",))

    # -- wire helpers
    def _send(self, conn, oid, opcode, body=b""):
        msg = struct.pack("<II", oid, ((8 + len(body)) << 16) | opcode) + body
        try:
            conn.sendall(msg)
        except OSError:
            pass

    def _global(self, conn, reg, name, iface, version):
        b = iface.encode() + b"\0"
        body = (struct.pack("<I", name) + struct.pack("<I", len(b)) + b
                + b"\0" * _pad(len(b)) + struct.pack("<I", version))
        self._send(conn, reg, 0, body)

    def _output_events(self, conn, oid, head):
        _name, lx, ly, _lw, _lh, scale, mw, mh = head
        make = b"wdotool\0"
        model = b"fake\0"
        body = (struct.pack("<iiiii", lx, ly, 300, 200, 0)
                + struct.pack("<I", len(make)) + make + b"\0" * _pad(len(make))
                + struct.pack("<I", len(model)) + model
                + b"\0" * _pad(len(model)) + struct.pack("<i", 0))
        self._send(conn, oid, 0, body)                       # geometry
        # mode(flags=current, width, height, refresh)
        self._send(conn, oid, 1, struct.pack("<Iiii", 1, mw, mh, 60000))
        self._send(conn, oid, 3, struct.pack("<i", scale))   # scale

    def _error(self, conn, oid, code, msg):
        b = msg.encode() + b"\0"
        body = (struct.pack("<II", oid, code) + struct.pack("<I", len(b)) + b
                + b"\0" * _pad(len(b)))
        self._send(conn, 1, 0, body)

    # -- readers used by the tests
    def of(self, *kinds):
        return [e for e in self.events if e[0] in kinds]

    def last(self, kind):
        for e in reversed(self.events):
            if e[0] == kind:
                return e
        raise AssertionError(f"no {kind} request was made: {self.events}")


def landed(ev, bbox=BBOX):
    """Where the compositor puts the cursor for one `motion_absolute`, in
    layout coordinates.

    The request carries a RATIO -- x/x_extent of the layout bounding box, in
    logical pixels -- so this is the compositor's own arithmetic, and it is
    the thing that has to come back exactly. (A live compositor additionally
    clamps a coordinate that falls in a hole between outputs to the nearest
    edge; that is its business, not the map's.)"""
    _kind, x, y, xe, ye = ev
    gx, gy, w, h = bbox
    return (gx + x * w / xe, gy + y * h / ye)


def compositor_pixel(axis_value, span):
    """The kernel path's arithmetic, for the two-path comparison: libinput's
    scale_axis() (value * span / (max - min + 1)) truncated to a pixel."""
    return axis_value * span // 32768


# ---------------------------------------------------------------------------
# daemons


class RecorderDev:
    """A uinput device, recording (the same double as test_input_daemon)."""

    def __init__(self):
        self.events = []

    def key(self, code, down):
        self.events.append(("KEY", code, 1 if down else 0))

    def emit(self, *a):
        self.events.append(("EMIT",) + a)

    def syn(self):
        self.events.append(("SYN",))

    def close(self):
        pass


def abs_report(dev):
    """(ABS_X, ABS_Y) of the last report a recorder tablet emitted."""
    vals = {}
    for ev in dev.events:
        if ev[0] == "EMIT" and ev[1] == uinput.EV_ABS:
            vals[ev[2]] = ev[3]
    return vals[uinput.ABS_X], vals[uinput.ABS_Y]


class VptrTest(unittest.TestCase):
    """Base: a fake compositor, and the environment pointed at it."""

    manager_version = 2
    comp_kw: dict = {}

    def setUp(self):
        self.comp = FakeCompositor(manager_version=self.manager_version,
                                   **self.comp_kw)
        self.addCleanup(self.comp.close)
        self._env = {k: os.environ.get(k) for k in
                     ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "WDOTOOL_VKBD",
                      "WDOTOOL_REL_MODE", "SWAYSOCK")}
        self.addCleanup(self._restore)
        os.environ["XDG_RUNTIME_DIR"] = self.comp.dir
        os.environ["WAYLAND_DISPLAY"] = os.path.basename(self.comp.path)
        os.environ.pop("WDOTOOL_VKBD", None)
        os.environ.pop("WDOTOOL_REL_MODE", None)
        # No sway socket: the kernel path's relative moves are warps here, so
        # a test that sees `motion` on the wire saw it because the *protocol*
        # path chose it and not because sway happened to be running.
        os.environ["SWAYSOCK"] = "/nonexistent/wdotool-no-sway"

    def _restore(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def daemon(self, uinput=True):
        d = daemon._Daemon()
        d._reader = None                 # no key-state reads in a test
        if uinput:
            d.kb = RecorderDev()
            d.mouse = RecorderDev()
            d.tablet = RecorderDev()
            d.dev_error = None
        else:
            d.kb = d.mouse = d.tablet = None
            d.dev_error = UINPUT_ERROR
            d.create_devices = lambda: None    # the retry keeps failing
        self.addCleanup(d._drop_vptr)
        self.addCleanup(d._drop_vkbd)
        return d


# ---------------------------------------------------------------------------
# the wire


class Creation(VptrTest):
    def test_it_creates_a_pointer_and_binds_the_manager_at_version_one(self):
        """v2's only addition is `create_virtual_pointer_with_output`, which
        maps into one output's box and CONFINES the cursor to it -- measured,
        with a relative motion of +4000 stopping at that output's own edge.
        Every wdotool coordinate is a layout coordinate, so we bind v1 and
        say so on the wire."""
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.flush()
        self.assertEqual(vp.version, 1)
        self.assertIn((vptr.MANAGER, 1), self.comp.binds)
        self.assertEqual(len(self.comp.created), 1)
        self.assertEqual(self.comp.with_output, [],
                         "the per-output constructor confines the cursor")

    def test_it_passes_the_seat_it_bound(self):
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.flush()
        seat, _oid = self.comp.created[0]
        self.assertNotEqual(seat, 0)
        self.assertIn(("wl_seat", 7), self.comp.binds)

    def test_destroy_on_close(self):
        vp = vptr.VirtualPointer.open()
        vp.flush()
        vp.close()
        self.assertTrue(_eventually(lambda: self.comp.destroyed == 1))

    def test_a_second_close_is_harmless(self):
        vp = vptr.VirtualPointer.open()
        vp.close()
        vp.close()
        self.assertTrue(_eventually(lambda: self.comp.destroyed == 1))


class ACompositorWithNoSeat(VptrTest):
    comp_kw = {"with_seat": False}

    def test_the_seat_argument_is_null(self):
        """`seat` is allow-null in this protocol, and a NULL was accepted and
        still moved the cursor. A seat with no pointer capability is exactly
        where a virtual pointer is worth creating, so nothing here requires
        one."""
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.flush()
        self.assertEqual(self.comp.created[0][0], 0)


class ACompositorWithoutTheProtocol(VptrTest):
    manager_version = None      # Mutter; KWin (both measured)

    def test_open_says_so_and_does_not_raise_anything_else(self):
        with self.assertRaises(vptr.VptrError) as cm:
            vptr.VirtualPointer.open()
        self.assertIn("does not implement", str(cm.exception))
        self.assertIn(vptr.MANAGER, str(cm.exception))
        self.assertEqual(self.comp.created, [])


class ACompositorThatRefuses(VptrTest):
    comp_kw = {"refuse_create": True}

    def test_a_protocol_error_becomes_a_vptrerror(self):
        with self.assertRaises(vptr.VptrError) as cm:
            vptr.VirtualPointer.open()
        self.assertIn("no virtual pointer for you", str(cm.exception))


# ---------------------------------------------------------------------------
# coordinates


class TheLayout(VptrTest):
    def test_the_bbox_is_the_one_the_daemon_computes(self):
        self.assertEqual(daemon._wayland_bbox(), BBOX)

    def test_the_scaled_head_is_read_from_xdg_output_not_wl_output(self):
        """`wl_output.scale` reported 2 for the 1.5-scaled head, where
        `zxdg_output_v1.logical_size` gave the correct 1280x720. The bbox
        prefers xdg_output and is right; this pins that it does, by showing
        what the wl_output-only fallback would have said."""
        self.assertEqual(daemon._wayland_bbox(), BBOX)
        comp = FakeCompositor(with_xdg_output=False)
        self.addCleanup(comp.close)
        os.environ["XDG_RUNTIME_DIR"] = comp.dir
        os.environ["WAYLAND_DISPLAY"] = os.path.basename(comp.path)
        self.assertEqual(daemon._wayland_bbox(), (-1920, -540, 4800, 1620),
                         "wl_output's scale of 2 shrinks the panel to 960x540 "
                         "and moves the layout's right edge 320px")


TARGETS = ((-1920, -540), (3199, 1079), (0, 0), (-1, -1), (1920, 0),
           (3199, 0), (2560, 360), (960, 540), (-960, 0), (0, -540),
           (3199, 719), (1919, 1079), (-1920, 539), (1000, 1000))


class AbsoluteMotion(VptrTest):
    """`motion_absolute(time, x, y, x_extent, y_extent)` is a ratio over the
    whole output layout in logical pixels -- not pixels, and not one output.
    So the forward map is the identity, and the round trip has to be exact:
    no quantisation means the kernel path's ceiling map (B7) has no
    counterpart here and no way to reintroduce the off-by-one it fixed."""

    def test_every_target_lands_with_no_error_at_all(self):
        d = self.daemon(uinput=False)
        for tx, ty in TARGETS:
            self.comp.events.clear()
            d.op_mousemove_abs(tx, ty, [])
            ev = self.comp.last("motion_absolute")
            self.assertEqual(ev[3:], (BBOX[2], BBOX[3]),
                             "the extents ARE the layout size")
            self.assertEqual(landed(ev), (float(tx), float(ty)),
                             f"target {tx},{ty}")

    def test_the_daemon_model_follows(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(2560, 360, [])
        self.assertEqual((d.px, d.py), (2560, 360))
        self.assertTrue(d.pos_known)

    def test_it_clamps_to_the_layout(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(99999, 99999, [])
        self.assertEqual(landed(self.comp.last("motion_absolute")),
                         (3199.0, 1079.0))
        d.op_mousemove_abs(-99999, -99999, [])
        self.assertEqual(landed(self.comp.last("motion_absolute")),
                         (-1920.0, -540.0))

    def test_a_frame_follows_the_motion(self):
        """Motion applies without one, but the receiving client then never
        sees `wl_pointer.frame`."""
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        kinds = [e[0] for e in self.comp.events if e[0] != "create"]
        self.assertEqual(kinds, ["motion_absolute", "frame"])

    def test_the_same_coordinate_twice_is_sent_twice(self):
        """The kernel path has to nudge an axis here (B2: the kernel drops an
        EV_ABS whose value has not changed). This one has nothing to work
        around -- and must not, because a physical mouse may have moved the
        cursor in between."""
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(100, 100, [])
        d.op_mousemove_abs(100, 100, [])
        moves = self.comp.of("motion_absolute")
        self.assertEqual(len(moves), 2)
        self.assertEqual(moves[0], moves[1])

    def test_a_zero_extent_is_floored_to_one(self):
        """x_extent == 0 is a silent no-op on the wire -- no motion event at
        all -- so a degenerate layout must not swallow the move."""
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.warp(0, 0, 0, 0, 0, 0)
        vp.flush()
        self.assertEqual(self.comp.last("motion_absolute")[3:], (1, 1))


class TheTwoPathsLandTogether(VptrTest):
    """The point of the branch: `wdotool mousemove X Y` puts the pointer at
    X,Y whichever device carries it. The kernel arithmetic is the tablet's
    (ceil to an axis value, libinput scales it back, the compositor truncates
    to a pixel); the protocol's is a ratio. Both have to end at the target."""

    def test_the_same_command_reaches_the_same_pixel(self):
        virt = self.daemon(uinput=False)
        kern = self.daemon(uinput=True)
        gx, gy, w, h = BBOX
        for tx, ty in TARGETS:
            self.comp.events.clear()
            kern.tablet.events.clear()
            virt.op_mousemove_abs(tx, ty, [])
            kern.op_mousemove_abs(tx, ty, [])
            ax, ay = abs_report(kern.tablet)
            self.assertEqual(
                (gx + compositor_pixel(ax, w), gy + compositor_pixel(ay, h)),
                (tx, ty), f"kernel path, target {tx},{ty}")
            self.assertEqual(landed(self.comp.last("motion_absolute")),
                             (float(tx), float(ty)),
                             f"protocol path, target {tx},{ty}")
        self.assertEqual((virt.px, virt.py), (kern.px, kern.py))


class RelativeMotion(VptrTest):
    """`motion(time, dx, dy)` in wl_fixed, and it is exact: a virtual pointer
    is not a libinput device, so no acceleration profile can apply to it. B1
    -- 500 requested pixels moving 858 through /dev/uinput -- cannot come
    back here."""

    def test_it_is_sent_as_pixels(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        self.comp.events.clear()
        d.op_mousemove_rel(-50, 5, [])
        self.assertEqual(self.comp.of("motion", "motion_absolute"),
                         [("motion", -50.0, 5.0)])
        self.assertEqual((d.px, d.py), (-50, 5))

    def test_a_frame_follows_it(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_rel(1, 0, [])
        self.assertEqual([e[0] for e in self.comp.events if e[0] != "create"],
                         ["motion", "frame"])

    def test_it_does_not_warp_where_the_kernel_path_would(self):
        """There is no sway socket in this environment, so the kernel path
        emits the target as an absolute warp (B1). The protocol path must not
        copy that: it has nothing to work around, and a warp would need a
        position model it may not have."""
        self.assertIs(self.daemon()._rel_absolute(), True)
        self.assertIs(self.daemon()._rel_absolute(virtual=True), False)

    def test_it_is_right_before_the_daemon_knows_where_the_pointer_is(self):
        d = self.daemon(uinput=False)
        self.assertFalse(d.pos_known)
        d.op_mousemove_rel(37, -11, [])
        self.assertEqual(self.comp.of("motion"), [("motion", 37.0, -11.0)],
                         "the motion asked for, not one computed from 0,0")

    def test_rel_mode_abs_still_forces_a_warp(self):
        os.environ["WDOTOOL_REL_MODE"] = "abs"
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(100, 100, [])
        self.comp.events.clear()
        d.op_mousemove_rel(10, 10, [])
        self.assertEqual(landed(self.comp.last("motion_absolute")),
                         (110.0, 110.0))

    def test_sub_pixel_deltas_survive_the_fixed_encoding(self):
        vp = vptr.VirtualPointer.open()
        self.addCleanup(vp.close)
        vp.move(1, 0)
        vp.flush()
        self.assertEqual(self.comp.of("motion"), [("motion", 1.0, 0.0)])


# ---------------------------------------------------------------------------
# buttons and scroll


class Buttons(VptrTest):
    """`button()` takes raw evdev codes and does not validate them; all eight
    wdotool buttons arrive verbatim, and they are the numbers daemon._BTN
    already uses for the kernel device."""

    ALL = ((1, uinput.BTN_LEFT), (2, uinput.BTN_MIDDLE),
           (3, uinput.BTN_RIGHT), (8, uinput.BTN_SIDE),
           (9, uinput.BTN_EXTRA), (10, uinput.BTN_FORWARD),
           (11, uinput.BTN_BACK), (12, uinput.BTN_TASK))

    def test_every_button_arrives_as_its_evdev_code(self):
        d = self.daemon(uinput=False)
        for btn, code in self.ALL:
            self.comp.events.clear()
            d.op_button(btn, True)
            d.op_button(btn, False)
            self.assertEqual(self.comp.of("button", "frame"),
                             [("button", code, 1), ("frame",),
                              ("button", code, 0), ("frame",)], btn)

    def test_the_codes_are_the_kernel_paths_codes(self):
        self.assertEqual(dict(self.ALL), daemon._Daemon._BTN)

    def test_an_invalid_button_is_refused_on_this_path_too(self):
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.op_button(42, True)
        self.assertIn("invalid mouse button", str(cm.exception))

    def test_a_second_press_is_not_sent(self):
        """Button state is refcounted per seat: press, press then release
        would leave the button DOWN, where the kernel drops the duplicate and
        the release lands. Both paths drop it, so both behave the same."""
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        d.op_button(1, True)
        d.op_button(1, False)
        self.assertEqual(self.comp.of("button"),
                         [("button", uinput.BTN_LEFT, 1),
                          ("button", uinput.BTN_LEFT, 0)])
        self.assertEqual(d.btns, set())

    def test_a_release_we_do_not_hold_is_not_sent(self):
        d = self.daemon(uinput=False)
        d.op_button(1, False)
        self.assertEqual(self.comp.of("button"), [])

    def test_the_kernel_path_drops_the_same_duplicates(self):
        d = self.daemon(uinput=True)
        d.op_button(1, True)
        d.op_button(1, True)
        d.op_button(1, False)
        d.op_button(1, False)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1),
                                          ("KEY", uinput.BTN_LEFT, 0)])

    def test_click_is_a_press_and_a_release(self):
        d = self.daemon(uinput=False)
        d.op_click(1, 2, 0)
        self.assertEqual(self.comp.of("button"),
                         [("button", uinput.BTN_LEFT, 1),
                          ("button", uinput.BTN_LEFT, 0)] * 2)
        self.assertEqual(len(self.comp.created), 1,
                         "the sink is chosen once for the whole run")


class Scroll(VptrTest):
    """The sign is Wayland's, not evdev's: positive vertical is scroll DOWN.
    So wdotool's wheel buttons map 4 -> axis 0 negative, 5 -> axis 0 positive,
    6 -> axis 1 negative, 7 -> axis 1 positive."""

    def test_the_four_wheel_buttons(self):
        want = {4: (vptr.AXIS_VERTICAL, -1), 5: (vptr.AXIS_VERTICAL, 1),
                6: (vptr.AXIS_HORIZONTAL, -1), 7: (vptr.AXIS_HORIZONTAL, 1)}
        d = self.daemon(uinput=False)
        for btn, (axis, direction) in want.items():
            self.comp.events.clear()
            d.op_button(btn, True)
            self.assertEqual(
                self.comp.of("axis_source", "axis_discrete", "frame"),
                [("axis_source", vptr.AXIS_SOURCE_WHEEL),
                 ("axis_discrete", axis, 15.0 * direction, direction),
                 ("frame",)], btn)

    def test_a_wheel_release_is_a_no_op(self):
        d = self.daemon(uinput=False)
        d.op_button(4, False)
        self.assertEqual(self.comp.of("axis_discrete", "axis"), [])

    def test_a_detent_is_a_notch_not_a_smooth_scroll(self):
        """`axis_discrete` is what carries `axis_value120` (120 per detent) to
        a wl_pointer >= 8 client; plain `axis` would arrive as a touchpad-ish
        smooth scroll of 15 units."""
        d = self.daemon(uinput=False)
        d.op_button(5, True)
        self.assertEqual(self.comp.of("axis"), [])
        self.assertEqual(self.comp.last("axis_discrete")[1:],
                         (vptr.AXIS_VERTICAL, 15.0, 1))

    def test_the_wheel_tables_are_the_same_four_gestures(self):
        self.assertEqual(sorted(vptr.WHEEL), sorted(daemon._Daemon._WHEEL))

    def test_they_are_opposite_in_the_vertical(self):
        """REL_WHEEL positive is up; Wayland's axis 0 positive is down. A
        table copied across without the flip would scroll backwards."""
        for btn in (4, 5):
            rel, value = daemon._Daemon._WHEEL[btn]
            axis, direction = vptr.WHEEL[btn]
            self.assertEqual(rel, uinput.REL_WHEEL)
            self.assertEqual(axis, vptr.AXIS_VERTICAL)
            self.assertEqual(value, -direction)

    def test_and_the_same_in_the_horizontal(self):
        for btn in (6, 7):
            rel, value = daemon._Daemon._WHEEL[btn]
            axis, direction = vptr.WHEEL[btn]
            self.assertEqual(rel, uinput.REL_HWHEEL)
            self.assertEqual(axis, vptr.AXIS_HORIZONTAL)
            self.assertEqual(value, direction)


# ---------------------------------------------------------------------------
# the policy


class ThePolicy(VptrTest):
    """One sentence, and it is the keyboard's: the protocol moves when the
    kernel pointer cannot be opened and the compositor implements it,
    /dev/uinput in every other case; --vkbd on|off forces either."""

    def test_with_uinput_the_kernel_devices_move_even_here(self):
        d = self.daemon(uinput=True)
        d.op_mousemove_abs(100, 200, [])
        d.op_button(1, True)
        self.assertTrue(d.tablet.events)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1)])
        self.assertEqual(self.comp.created, [],
                         "no virtual pointer may be created at all")

    def test_without_uinput_the_protocol_moves(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(100, 200, [])
        d.op_button(1, True)
        self.assertEqual(len(self.comp.created), 1)
        self.assertEqual(landed(self.comp.of("motion_absolute")[0]),
                         (100.0, 200.0))
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 1))

    def test_the_notice_names_the_protocol_and_is_said_once(self):
        d = self.daemon(uinput=False)
        resp = d.handle({"op": "mousemove_abs", "x": 0, "y": 0})
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(any("zwlr_virtual_pointer_v1" in w
                            for w in resp["warnings"]), resp)
        self.assertTrue(any("no root and no device rule" in w
                            for w in resp["warnings"]), resp)
        resp = d.handle({"op": "mousemove_abs", "x": 1, "y": 1})
        self.assertEqual(resp["warnings"], [], "and only once")

    def test_without_uinput_and_without_the_protocol_nothing_changes(self):
        self.comp.manager_version = None
        d = self.daemon(uinput=False)
        for req in ({"op": "mousemove_abs", "x": 3, "y": 4},
                    {"op": "mousemove_rel", "dx": 1, "dy": 1},
                    {"op": "button", "btn": 1, "down": True},
                    {"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0}):
            with self.assertRaises(RuntimeError) as cm:
                d.handle(req)
            self.assertEqual(str(cm.exception), UINPUT_ERROR, req["op"])

    def test_vkbd_off_keeps_the_kernel_path_and_its_error(self):
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": True,
                      "vkbd_mode": "off"})
        self.assertEqual(str(cm.exception), UINPUT_ERROR)
        self.assertEqual(self.comp.created, [])

    def test_vkbd_on_uses_the_protocol_although_uinput_works(self):
        d = self.daemon(uinput=True)
        d.handle({"op": "mousemove_abs", "x": 5, "y": 6, "vkbd_mode": "on"})
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        self.assertEqual(landed(self.comp.last("motion_absolute")), (5.0, 6.0))
        self.assertEqual(d.mouse.events, [])
        self.assertEqual(d.tablet.events, [],
                         "nothing may reach the kernel devices")

    def test_vkbd_on_without_the_protocol_is_a_clean_refusal(self):
        self.comp.manager_version = None
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0,
                      "vkbd_mode": "on"})
        self.assertIn("--vkbd on", str(cm.exception))
        self.assertIn("zwlr_virtual_pointer_v1", str(cm.exception))
        self.assertEqual(d.mouse.events, [],
                         "it must not silently use uinput")

    def test_the_environment_selects_it_too(self):
        os.environ["WDOTOOL_VKBD"] = "on"
        d = self.daemon(uinput=True)
        d.op_button(1, True)
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 1))

    def test_the_flag_beats_the_environment(self):
        os.environ["WDOTOOL_VKBD"] = "on"
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "off"})
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1)])
        self.assertEqual(self.comp.created, [])

    def test_a_nonsense_environment_value_is_auto_not_a_failure(self):
        os.environ["WDOTOOL_VKBD"] = "yes-please"
        d = self.daemon(uinput=True)
        d.op_button(1, True)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 1)])

    def test_an_unknown_mode_on_the_wire_is_a_rejected_request(self):
        d = self.daemon(uinput=True)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": True,
                      "vkbd_mode": "maybe"})
        self.assertIn("invalid vkbd_mode", str(cm.exception))

    def test_the_two_halves_are_chosen_independently(self):
        """This compositor implements the pointer protocol and not the
        keyboard one, which is the case that proves the choice is made twice:
        clicking works with no privilege while typing still reports the
        kernel keyboard's error."""
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 1))
        with self.assertRaises(RuntimeError) as cm:
            d.op_type("a", 0, False)
        self.assertEqual(str(cm.exception), UINPUT_ERROR)


class ThePointerCommandsOverTheWire(VptrTest):
    def test_the_client_sends_the_mode_and_the_daemon_reads_it(self):
        sent = {}

        class FakeClient(daemon.DaemonClient):
            def __init__(self):
                pass

            def _rpc(self, **kw):
                sent.update(kw)
                return {}

        c = FakeClient()
        c.click(1, 1, 0, vkbd_mode="on")
        self.assertEqual(sent["vkbd_mode"], "on")
        c.mousemove_abs(1, 2, vkbd_mode="off")
        self.assertEqual(sent["vkbd_mode"], "off")
        c.mousemove_rel(1, 2, vkbd_mode="on")
        self.assertEqual(sent["vkbd_mode"], "on")
        c.button(1, True, vkbd_mode="off")
        self.assertEqual(sent["vkbd_mode"], "off")

    def test_an_absent_mode_is_not_in_the_request(self):
        sent = {}

        class FakeClient(daemon.DaemonClient):
            def __init__(self):
                pass

            def _rpc(self, **kw):
                sent.update(kw)
                return {}

        FakeClient().click(1, 1, 0)
        self.assertNotIn("vkbd_mode", sent)
        self.assertNotIn("layout_mode", sent)

    def test_the_flag_reaches_the_pointer_commands(self):
        from wdotool import input_cmds

        calls = []

        class Ctx:
            stack: list = []
            vkbd_mode = "on"

            def daemon(self):
                class D:
                    def click(self, *a, **kw):
                        calls.append(("click", kw))

                    def button(self, *a, **kw):
                        calls.append(("button", kw))

                    def mousemove_abs(self, *a, **kw):
                        calls.append(("abs", kw))

                    def mousemove_rel(self, *a, **kw):
                        calls.append(("rel", kw))

                    def seed_pointer(self, *a):
                        pass

                    def pointer(self):
                        return (0, 0)

                return D()

            def resolve_windows(self, arg):
                return [None]

            def backend(self):
                return object()      # no `pointer`: the model stands

        ctx = Ctx()
        input_cmds.cmd_click(ctx, ["1"])
        input_cmds.cmd_mousedown(ctx, ["1"])
        input_cmds.cmd_mousemove(ctx, ["10", "20"])
        input_cmds.cmd_mousemove_relative(ctx, ["1", "2"])
        self.assertEqual([k for _n, k in calls],
                         [{"clearmods": False, "vkbd_mode": "on"}] * 2
                         + [{"clearmods": False, "vkbd_mode": "on"}] * 2)


# ---------------------------------------------------------------------------
# held buttons: the lifetime, and the sink switch


class HeldButtons(VptrTest):
    def test_a_button_held_across_commands_keeps_one_pointer(self):
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        d.op_mousemove_abs(500, 500, [])
        d.op_button(1, False)
        self.assertEqual(len(self.comp.created), 1,
                         "one connection and one pointer, for the daemon")
        self.assertEqual(self.comp.of("button", "motion_absolute"),
                         [("button", 0x110, 1),
                          self.comp.last("motion_absolute"),
                          ("button", 0x110, 0)])
        self.assertEqual(d.btns, set())

    def test_a_drag_is_one_object_from_press_to_release(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(-1920, -540, [])
        d.op_button(1, True)
        d.op_mousemove_abs(2560, 360, [])
        d.op_mousemove_rel(10, 0, [])
        d.op_button(1, False)
        kinds = [e[0] for e in self.comp.events]
        self.assertEqual(kinds.count("create"), 1)
        self.assertEqual([k for k in kinds if k != "frame" and k != "create"],
                         ["motion_absolute", "button", "motion_absolute",
                          "motion", "button"])

    def test_auto_does_not_change_pointers_under_a_held_button(self):
        """`wdotool --vkbd on mousedown 1` and then a plain `wdotool mouseup
        1`: the second command must not switch to the kernel device, because
        a button held on one cannot be released on the other."""
        d = self.daemon(uinput=True)          # uinput works in this one
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        d.handle({"op": "mousemove_abs", "x": 10, "y": 10})   # auto
        d.handle({"op": "button", "btn": 1, "down": False})   # auto
        self.assertEqual(d.mouse.events, [])
        self.assertEqual(d.tablet.events, [],
                         "nothing may leak to the kernel devices mid-hold")
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 0))
        self.assertEqual(d.btns, set())

    def test_forcing_off_under_a_held_button_releases_it_on_the_holder(self):
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        resp = d.handle({"op": "button", "btn": 3, "down": True,
                         "vkbd_mode": "off"})
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 0),
                         "released on the pointer that was holding it")
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_RIGHT, 1)])
        self.assertTrue(any("were released" in w for w in resp["warnings"]),
                        resp)
        self.assertTrue(any("left" in w for w in resp["warnings"]), resp)
        self.assertEqual(d.btns, {uinput.BTN_RIGHT})

    def test_forcing_on_under_a_kernel_held_button_releases_it_there(self):
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "off"})
        d.mouse.events.clear()
        resp = d.handle({"op": "button", "btn": 1, "down": True,
                         "vkbd_mode": "on"})
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_LEFT, 0)])
        self.assertEqual(self.comp.of("button"), [("button", 0x110, 1)])
        self.assertTrue(any("were released" in w for w in resp["warnings"]),
                        resp)

    def test_nothing_is_said_when_nothing_is_held(self):
        d = self.daemon(uinput=True)
        for mode in ("on", "off", "on"):
            resp = d.handle({"op": "click", "btn": 1, "repeat": 1,
                             "delay_ms": 0, "vkbd_mode": mode})
            self.assertEqual(resp["warnings"], [], mode)

    def test_a_key_and_a_button_are_held_independently(self):
        """Two connections and two objects on purpose: the keyboard's
        troubles must not drop the pointer's held button."""
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        d.op_key("ctrl", "down", 0, False, None, None, "off")   # kernel keys
        self.assertEqual(d.btns, {uinput.BTN_LEFT})
        self.assertTrue(d._btns_virtual)
        self.assertFalse(d._down_virtual)
        d.handle({"op": "button", "btn": 1, "down": False})
        self.assertEqual(self.comp.last("button"), ("button", 0x110, 0))


class Reconnecting(VptrTest):
    def test_a_compositor_restart_costs_the_hold_and_not_the_command(self):
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        self.assertEqual(d.btns, {uinput.BTN_LEFT})
        self.comp.drop_clients()
        resp = d.handle({"op": "mousemove_abs", "x": 0, "y": 0})
        self.assertTrue(resp.get("ok"), resp)
        self.assertTrue(any("buttons wdotool was holding" in w
                            for w in resp["warnings"]), resp["warnings"])
        self.assertEqual(d.btns, set(),
                         "the compositor released what we held; so must we")
        self.assertEqual(len(self.comp.created), 2)

    def test_a_restart_with_nothing_held_says_nothing(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        self.comp.drop_clients()
        resp = d.handle({"op": "mousemove_abs", "x": 1, "y": 1})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(resp["warnings"], [])

    def test_no_button_survives_a_drop_in_the_daemons_model(self):
        """The worst outcome this path could have: a button the daemon
        believes is held on a pointer that no longer exists. Nothing could
        ever release it, and every later click would be a drag.

        The connection is checked before each command, so the ordinary
        restart is the graceful one above; this is the other half -- the
        pointer dying with an injection already in flight, where the write
        that fails is the release of the very button we hold, so the object's
        own `held` no longer lists it. _drop_vptr() must not trust that."""
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        self.assertEqual(d.btns, {uinput.BTN_LEFT})
        sink, _virtual = d._pointer_sink()   # the object the op would use
        self.comp.drop_clients()
        with self.assertRaises(RuntimeError):
            with d._vp_guard():
                for _ in range(200):    # fill the socket buffer to notice
                    d._button(sink, 1, False)
                    d._button(sink, 1, True)
        self.assertEqual(d.btns, set(), "nothing may stay down after a drop")
        self.assertIsNone(d._vp)

    def test_a_compositor_that_does_not_come_back_is_a_clean_error(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        self.comp.close()                # socket gone, nothing to reconnect to
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": True})
        self.assertEqual(str(cm.exception), UINPUT_ERROR,
                         "the error is the kernel device's, unchanged")

    def test_a_restart_falls_back_to_uinput_when_that_is_what_works(self):
        """uinput available, a button held on the virtual pointer, and the
        compositor gone: the hold cannot be honoured by anyone, so auto is
        free to choose again -- and chooses the kernel devices rather than
        failing the command."""
        d = self.daemon(uinput=True)
        d.handle({"op": "button", "btn": 1, "down": True, "vkbd_mode": "on"})
        self.comp.drop_clients()
        self.comp.close()
        resp = d.handle({"op": "button", "btn": 3, "down": True})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(d.mouse.events, [("KEY", uinput.BTN_RIGHT, 1)])
        self.assertEqual(d.btns, {uinput.BTN_RIGHT})

    def test_the_protocol_withdrawn_mid_session_does_not_break_the_hold(self):
        """global_remove for the manager. A Wayland object outlives the global
        it came from, so the pointer we already have keeps clicking and our
        client must not choke on the event; only the next connection finds the
        protocol gone, and falls back cleanly."""
        d = self.daemon(uinput=False)
        d.op_button(1, True)
        self.comp.withdraw_manager()
        d.op_mousemove_abs(0, 0, [])
        d.op_button(1, False)
        self.assertEqual(len(self.comp.created), 1)
        self.comp.manager_version = None
        self.comp.drop_clients()
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "button", "btn": 1, "down": True})
        self.assertEqual(str(cm.exception), UINPUT_ERROR)


# ---------------------------------------------------------------------------
# getmouselocation


class WhereThePointerIs(VptrTest):
    """The protocol CANNOT say: `zwlr_virtual_pointer_v1` has no events at
    all, and sway's IPC carries no cursor position either. So the answer is
    exactly what wdotool put there -- which on this path is exact, because
    the absolute map has no error -- and a refusal when it put nothing."""

    def test_it_refuses_rather_than_answering_the_origin(self):
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "pointer"})
        self.assertIn("does not know where the pointer is", str(cm.exception))
        self.assertIn("zwlr_virtual_pointer_v1", str(cm.exception))
        self.assertNotIn("run it as root", str(cm.exception),
                         "uinput is not the thing the user would have to fix")

    def test_it_answers_exactly_once_wdotool_has_moved_it(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(2560, 360, [])
        self.assertEqual(d.handle({"op": "pointer"}),
                         {"ok": True, "x": 2560, "y": 360, "known": True})

    def test_a_relative_move_makes_it_answerable_too(self):
        d = self.daemon(uinput=False)
        d.op_mousemove_abs(0, 0, [])
        d.op_mousemove_rel(10, 20, [])
        self.assertEqual(d.handle({"op": "pointer"})["x"], 10)

    def test_seed_pointer_still_works(self):
        d = self.daemon(uinput=False)
        d.op_seed_pointer(1234, 56, [])
        self.assertEqual(d.handle({"op": "pointer"}),
                         {"ok": True, "x": 1234, "y": 56, "known": True})

    def test_without_the_protocol_the_uinput_error_is_still_the_answer(self):
        self.comp.manager_version = None
        d = self.daemon(uinput=False)
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "pointer"})
        self.assertEqual(str(cm.exception), UINPUT_ERROR)


# ---------------------------------------------------------------------------
# --clearmodifiers on a pointer command


class ClearModifiers(VptrTest):
    def test_a_click_with_no_kernel_device_still_clears_modifiers(self):
        """`click --clearmodifiers` used to demand /dev/uinput for the
        keyboard half whatever the pointer did, which would have left it the
        one pointer command that still needed root here. There is no virtual
        keyboard on this compositor, so the clear has nothing to do -- and
        must not be the thing that fails the click."""
        d = self.daemon(uinput=False)
        resp = d.handle({"op": "click", "btn": 1, "repeat": 1, "delay_ms": 0,
                         "clearmods": True})
        self.assertTrue(resp.get("ok"), resp)
        self.assertEqual(self.comp.of("button"),
                         [("button", 0x110, 1), ("button", 0x110, 0)])


def _eventually(pred, tries=100):
    import time

    for _ in range(tries):
        if pred():
            return True
        time.sleep(0.01)
    return pred()


if __name__ == "__main__":
    unittest.main(verbosity=1)
