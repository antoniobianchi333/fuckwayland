"""wxrandr KWin backend tests: a wire-level fake KWin — a real unix-socket
wl_display speaking kde_output_device_v2 + kde_output_management_v2 over the
actual Wayland wire format — in the style of tests/test_wxrandr_mutter.py.

The fake serves both discovery shapes (per-output `kde_output_device_v2`
globals as on 5.27..6.6, and `kde_output_device_registry_v2` handing devices
out as new_ids as on 6.7+), advertises a configurable version pair (5.27's
2/3 and Plasma 6.6's 20/19 are both exercised), allocates a fresh mode object
per mode event like a server does, and validates `apply` the way KWin does:
one apply per configuration object (a second one is the fatal
`already_applied` protocol error), no enabled output at a negative
coordinate, never all outputs disabled, `failure_reason` only when the bound
management version is >= 12, and the 1/120 scale quantisation with a
non-positive scale silently dropped. No KDE needed."""

import contextlib
import io
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wdotool import session as wsession                        # noqa: E402
from wdotool.wayland_mini import Cursor                        # noqa: E402
from wxrandr import cli, core, kwin, mutter                    # noqa: E402
from wxrandr.core import Mode, State                           # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest). This line covers `python3 tests/<file>.py`.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

DEV, REG, MGMT, ORDER = kwin.DEV, kwin.REG, kwin.MGMT, kwin.ORDER
GAMMA = kwin.GAMMA

# ---------------------------------------------------------------- fixtures

EDP_MODES = [(1920, 1080, 60020, True), (1920, 1080, 48000, False),
             (1680, 1050, 59954, False), (1280, 720, 59860, False)]
DP_MODES = [(2560, 1600, 59972, True), (1920, 1200, 59950, False),
            (1920, 1080, 60000, False)]
HDMI_MODES = [(1280, 1024, 60020, True), (1024, 768, 60004, False)]


def head(name, make, model, serial, uuid, mm, modes, enabled=True, x=0, y=0,
         scale=1.0, transform=0, subpixel=1, current=0, caps=0, edid="",
         eisa=""):
    return {"name": name, "make": make, "model": model, "serial": serial,
            "uuid": uuid, "mm": mm, "modes": list(modes), "enabled": enabled,
            "x": x, "y": y, "scale": scale, "transform": transform,
            "subpixel": subpixel, "current": current, "caps": caps,
            "edid": edid, "eisa": eisa, "priority": None, "gname": 0}


def EDP(**kw):
    return head("eDP-1", "BOE", "0x0a1b", "0x00000000",
                "d0a1b-eDP-1-uuid", (344, 194), EDP_MODES,
                edid="AP///////wAGrxUKAAAAAAAcAQOAIhN4", **kw)


def DP(**kw):
    kw.setdefault("x", 1920)
    return head("DP-1", "DEL", "DELL U2723QE", "7PJ2XM3", "del-dp1-uuid",
                (597, 373), DP_MODES, subpixel=2, **kw)


def HDMI(**kw):
    return head("HDMI-1", "SAM", "SyncMaster", "H1AK500000", "sam-hdmi-uuid",
                (376, 301), HDMI_MODES, **kw)


# ---------------------------------------------------------------- wire helpers

def _s(v: str) -> bytes:
    b = v.encode() + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)


def _msg(obj: int, op: int, payload: bytes = b"") -> bytes:
    return struct.pack("<II", obj, ((8 + len(payload)) << 16) | op) + payload


MGMT_GNAME, REG_GNAME, GAMMA_GNAME, ORDER_GNAME = 100, 99, 98, 97


class _Client(threading.Thread):
    """One connected wxrandr. Owns the per-connection object ids: server-side
    new_ids (mode objects, and the device objects of the registry path) come
    out of the 0xff000000 range a real server uses."""

    def __init__(self, svc, sock):
        super().__init__(daemon=True)
        self.svc = svc
        self.sock = sock
        self.next_id = kwin.SERVER_ID_BASE
        self.registry = None
        self.mgmt = None
        self.mgmt_bound = 0
        self.dev_bound = 0
        self.reg_obj = None
        self.order_obj = None
        self.devs = {}          # device object id -> head
        self.dev_of = {}        # head gname -> device object id
        self.modes = {}         # mode object id -> (head gname, index)
        self.mode_of = {}       # (head gname, index) -> mode object id
        self.configs = {}
        self.dead = False

    # -- plumbing

    def alloc(self):
        self.next_id += 1
        return self.next_id - 1

    def send(self, data):
        try:
            self.sock.sendall(data)
        except OSError:
            self.dead = True

    def run(self):
        buf = b""
        try:
            while not self.dead:
                data = self.sock.recv(65536)
                if not data:
                    break
                buf += data
                while len(buf) >= 8 and not self.dead:
                    obj, sizeop = struct.unpack_from("<II", buf)
                    size, op = sizeop >> 16, sizeop & 0xFFFF
                    if len(buf) < size:
                        break
                    payload, buf = buf[8:size], buf[size:]
                    self._request(obj, op, Cursor(payload))
        except OSError:
            pass
        finally:
            with self.svc.lock:
                if self in self.svc.clients:
                    self.svc.clients.remove(self)
            try:
                self.sock.close()
            except OSError:
                pass

    # -- requests

    def _request(self, obj, op, cur):
        if obj == 1 and op == 0:                     # wl_display.sync
            self.send(_msg(cur.u32(), 0, struct.pack("<I", 0)))
        elif obj == 1 and op == 1:                   # wl_display.get_registry
            self.registry = cur.u32()
            for gname, iface, ver in self.svc.globals():
                self._global(gname, iface, ver)
        elif obj == self.registry and op == 0:       # wl_registry.bind
            gname, iface, ver, nid = (cur.u32(), cur.string(), cur.u32(),
                                      cur.u32())
            self._bind(gname, iface, ver, nid)
        elif obj == self.mgmt and op == 0:           # create_configuration
            cid = cur.u32()
            self.configs[cid] = {"applied": False, "requests": []}
            with self.svc.lock:
                self.svc.created += 1
        elif obj in self.configs:
            self._config(obj, op, cur)

    def _global(self, gname, iface, ver):
        self.send(_msg(self.registry, 0,
                       struct.pack("<I", gname) + _s(iface)
                       + struct.pack("<I", ver)))

    def _bind(self, gname, iface, ver, nid):
        if iface == MGMT:
            self.mgmt, self.mgmt_bound = nid, ver
        elif iface == REG:
            if ver < kwin.REG_MIN:
                self.send(_msg(1, 0, struct.pack("<II", nid, 1)
                               + _s("unsupported version")))
                self.dead = True
                return
            self.reg_obj, self.dev_bound = nid, ver
            for h in list(self.svc.heads):
                self._offer(h)
        elif iface == ORDER:
            self.order_obj = nid
            self.send_order()
        elif iface == DEV:
            self.dev_bound = ver
            h = self.svc.by_gname(gname)
            if h is not None:
                self.devs[nid] = h
                self.dev_of[h["gname"]] = nid
                self._burst(nid, h, full=True)

    def send_order(self):
        """kde_output_order_v1: the connector names in KWin's own order,
        then `done`; resent whenever the order changes."""
        if self.order_obj is None:
            return
        out = b""
        for name in self.svc.output_order():
            out += _msg(self.order_obj, 0, _s(name))
        self.send(out + _msg(self.order_obj, 1))

    def _offer(self, h):
        """kde_output_device_registry_v2's `output` event: a new_id of
        kde_output_device_v2 the server allocates for this client."""
        oid = self.alloc()
        self.devs[oid] = h
        self.dev_of[h["gname"]] = oid
        self.send(_msg(self.reg_obj, self.svc.registry_opcode,
                       struct.pack("<I", oid)))
        self._burst(oid, h, full=True)

    # -- device events

    def _burst(self, oid, h, full):
        ver = self.dev_bound
        out = _msg(oid, 0, struct.pack("<iiiii", h["x"], h["y"], h["mm"][0],
                                       h["mm"][1], h["subpixel"])
                   + _s(h["make"]) + _s(h["model"])
                   + struct.pack("<i", h["transform"]))
        if full:
            for j, m in enumerate(h["modes"]):
                mid = self.alloc()
                self.modes[mid] = (h["gname"], j)
                self.mode_of[(h["gname"], j)] = mid
                out += _msg(oid, 2, struct.pack("<I", mid))
                out += _msg(mid, 0, struct.pack("<ii", m[0], m[1]))
                if m[2]:
                    out += _msg(mid, 1, struct.pack("<i", m[2]))
                if m[3]:
                    out += _msg(mid, 2)
        if h["enabled"]:
            mid = self.mode_of.get((h["gname"], h["current"]))
            if mid is not None:
                out += _msg(oid, 1, struct.pack("<I", mid))
        out += _msg(oid, 4, struct.pack("<i", kwin.to_fixed(h["scale"])))
        if full:
            out += _msg(oid, 5, _s(h["edid"]))
        out += _msg(oid, 6, struct.pack("<i", 1 if h["enabled"] else 0))
        if full:
            out += _msg(oid, 7, _s(h["uuid"]))
            out += _msg(oid, 8, _s(h["serial"]))
            out += _msg(oid, 9, _s(h["eisa"]))
            out += _msg(oid, 10, struct.pack("<I", h["caps"]))
            if ver >= 2:
                out += _msg(oid, 14, _s(h["name"]))
            if ver >= 18 and h["priority"] is not None:
                out += _msg(oid, 34, struct.pack("<I", h["priority"]))
        if h["name"] != self.svc.withhold_done:
            out += _msg(oid, 3)                       # done
        self.send(out)

    def refresh(self):
        for h in list(self.svc.heads):
            oid = self.dev_of.get(h["gname"])
            if oid is not None:
                self._burst(oid, h, full=False)

    # -- configuration

    def _config(self, cid, op, cur):
        cfg = self.configs[cid]
        if op == kwin.REQ_DESTROY:
            return
        if op == kwin.REQ_APPLY:
            self._apply(cid, cfg)
            return
        if op == kwin.REQ_SET_PRIMARY:
            h = self.devs.get(cur.u32())
            cfg["requests"].append(("primary", h["name"] if h else "?"))
            return
        if op == kwin.REQ_SET_PRIORITY:
            h = self.devs.get(cur.u32())
            cfg["requests"].append(("priority", h["name"] if h else "?",
                                    cur.u32()))
            return
        dev = self.devs.get(cur.u32())
        name = dev["name"] if dev else "?"
        if op == kwin.REQ_ENABLE:
            cfg["requests"].append(("enable", name, bool(cur.i32())))
        elif op == kwin.REQ_MODE:
            gname, idx = self.modes.get(cur.u32(), (None, None))
            cfg["requests"].append(("mode", name, idx))
        elif op == kwin.REQ_TRANSFORM:
            cfg["requests"].append(("transform", name, cur.i32()))
        elif op == kwin.REQ_POSITION:
            cfg["requests"].append(("position", name, (cur.i32(), cur.i32())))
        elif op == kwin.REQ_SCALE:
            cfg["requests"].append(("scale", name, cur.i32()))

    def _fail(self, cid, reason):
        if reason and self.mgmt_bound >= kwin.REASON_MGMT:
            self.send(_msg(cid, 2, _s(reason)))       # failure_reason
        self.send(_msg(cid, 1))                       # failed

    def _apply(self, cid, cfg):
        svc = self.svc
        if cfg["applied"]:
            # already_applied (code 0) is a fatal protocol error: the whole
            # connection goes down, exactly as KWin does it.
            self.send(_msg(1, 0, struct.pack("<II", cid, 0)
                           + _s("output configuration already applied")))
            self.dead = True
            return
        cfg["applied"] = True
        with svc.lock:
            svc.applied.append(list(cfg["requests"]))
            if svc.invalidate_once:
                svc.invalidate_once = False
                self._fail(cid, "One of the relevant outputs is no longer "
                                "available")
                return
            if svc.fail_next is not None:
                reason, svc.fail_next = svc.fail_next, None
                self._fail(cid, reason)
                return
            pend = {h["gname"]: dict(h) for h in svc.heads}
            by_name = {h["name"]: h for h in pend.values()}
            for req in cfg["requests"]:
                h = by_name.get(req[1])
                if h is None:
                    continue
                if req[0] == "enable":
                    h["enabled"] = req[2]
                elif req[0] == "mode" and req[2] is not None:
                    h["current"] = req[2]
                elif req[0] == "transform":
                    h["transform"] = req[2]
                elif req[0] == "position":
                    h["x"], h["y"] = req[2]
                elif req[0] == "scale":
                    # KWin: std::round(s * 120) / 120, and <= 0 is dropped
                    # silently (the request simply has no effect)
                    s = kwin.quantize_scale(req[2] / 256.0)
                    if s > 0:
                        h["scale"] = s
                elif req[0] == "primary":
                    # KWin takes the request and does nothing with it, on
                    # 5.27 as on 6.6: the output order does not move
                    svc.primary_requested = h["name"]
                elif req[0] == "priority":
                    h["priority"] = req[2]
            if not any(h["enabled"] for h in pend.values()):
                self._fail(cid, "Disabling all outputs through configuration "
                                "changes is not allowed")
                return
            for h in pend.values():
                if h["enabled"] and (h["x"] < 0 or h["y"] < 0):
                    self._fail(cid, "Position of enabled output %s is negative"
                               % h["name"])
                    return
            svc.heads = [pend[h["gname"]] for h in svc.heads]
        self.send(_msg(cid, 0))                       # applied
        self.refresh()
        for c in list(svc.clients):
            c.send_order()


class FakeKWin:
    """KWin's two output protocols on a real unix socket."""

    def __init__(self, heads, dev_version=20, mgmt_version=19,
                 registry_path=False, registry_opcode=1, gamma=False,
                 order=True, registry_version=None):
        self.heads = list(heads)
        for i, h in enumerate(self.heads):
            h["gname"] = i + 1
            if h["priority"] is None:
                h["priority"] = i + 1
        self.order = order
        self.dev_version = dev_version
        self.mgmt_version = mgmt_version
        self.registry_path = registry_path
        self.registry_version = registry_version
        self.registry_opcode = registry_opcode
        self.gamma = gamma
        self.created = 0          # configuration objects created
        self.applied = []         # request lists that reached `apply`
        self.primary_requested = None
        self.fail_next = None     # reason string for the next apply
        self.invalidate_once = False
        self.withhold_done = None  # output name whose `done` never comes
        self.clients = []
        self.lock = threading.Lock()
        self.dir = tempfile.mkdtemp(prefix="wxrandr-kwin-wl-")
        self.path = os.path.join(self.dir, "wayland-8")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            c = _Client(self, s)
            with self.lock:
                self.clients.append(c)
            c.start()

    def close(self):
        self.srv.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- state

    def globals(self):
        out = []
        if self.registry_path:
            out.append((REG_GNAME, REG, self.registry_version
                        or max(self.dev_version, kwin.REG_MIN)))
        else:
            for h in self.heads:
                out.append((h["gname"], DEV, self.dev_version))
        out.append((MGMT_GNAME, MGMT, self.mgmt_version))
        if self.order:
            out.append((ORDER_GNAME, ORDER, 1))
        if self.gamma:
            out.append((GAMMA_GNAME, GAMMA, 1))
        return out

    def output_order(self):
        """KWin's output order: the enabled outputs by priority. Its first
        entry is the primary plasmashell and XWayland follow."""
        return [n for _p, _i, n in
                sorted((h["priority"], i, h["name"])
                       for i, h in enumerate(self.heads) if h["enabled"])]

    @property
    def primary(self):
        order = self.output_order()
        return order[0] if order else None

    def hangup(self):
        """The compositor goes away mid-session (a KWin restart)."""
        with self.lock:
            for c in list(self.clients):
                c.dead = True
                try:
                    c.sock.shutdown(socket.SHUT_RDWR)
                    c.sock.close()
                except OSError:
                    pass

    def by_gname(self, gname):
        for h in self.heads:
            if h["gname"] == gname:
                return h
        return None

    def by_name(self, name):
        for h in self.heads:
            if h["name"] == name:
                return h
        return None

    def layout(self):
        return [(h["name"], h["enabled"], h["x"], h["y"], h["scale"],
                 h["transform"], h["current"]) for h in self.heads]

    def unplug(self, name):
        """Hotplug-out: the global goes away (5.27..6.6) or the device is
        marked `removed` (6.7+)."""
        with self.lock:
            h = self.by_name(name)
            if h is None:
                return
            self.heads.remove(h)
            for c in self.clients:
                oid = c.dev_of.pop(h["gname"], None)
                if self.registry_path:
                    if oid is not None:
                        c.send(_msg(oid, 36))          # removed (since v21)
                elif c.registry is not None:
                    c.send(_msg(c.registry, 1, struct.pack("<I", h["gname"])))
                c.send_order()


# ---------------------------------------------------------------- fixtures

def one_head(**kw):
    return FakeKWin([EDP()], **kw)


def two_heads(**kw):
    return FakeKWin([EDP(), DP()], **kw)


def three_heads(hdmi_on=False, **kw):
    return FakeKWin([EDP(), DP(), HDMI(enabled=hdmi_on, x=4480)], **kw)


# ---------------------------------------------------------------- harness

class KwinCase(unittest.TestCase):
    """Each test gets a fresh fake KWin and state file; the CLI runs with
    Session replaced by a KWin session on the fake (backend selection is
    tested separately)."""

    def fixture(self):
        return two_heads()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-kwin-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.svc = self.fixture()
        self.addCleanup(self.svc.close)
        self.opened = []

    def tearDown(self):
        for ko in self.opened:
            ko.close()

    def outputs(self):
        ko = kwin.KwinOutputs(socket_path=self.svc.path)
        self.opened.append(ko)
        return ko

    def state(self):
        return State("kwin-test", path=self.state_path)

    def run_cli(self, *argv, env=None):
        tc = self

        def fake_init(sess, forced=None):
            sess.backend = cli.canonical_backend(forced) or "kwin"
            sess.ipc = sess.wlr = sess.mutter = None
            sess.kwin = tc.outputs()
            sess.persistent = os.environ.get("WXRANDR_PERSIST", "") not in (
                "", "0")
            sess.state = tc.state()
        env = env or {}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        orig = cli.Session.__init__
        cli.Session.__init__ = fake_init
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(list(argv))
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 0
        finally:
            cli.Session.__init__ = orig
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, out.getvalue(), err.getvalue()

    def strip_save(self, err):
        """stderr with the two unconditional apply warnings removed."""
        keep = [ln for ln in err.splitlines(True)
                if not ln.startswith("xrandr: KWin applies and saves")
                and not ln.startswith("xrandr: to restore the previous")]
        return "".join(keep)

    def restore_line(self, err):
        for ln in err.splitlines():
            if ln.startswith("xrandr: to restore the previous layout: "):
                return ln.split(": ", 2)[2]
        return None


# ---------------------------------------------------------------- helpers

class Helpers(unittest.TestCase):
    def test_scale_quantisation_round_trip(self):
        # KWin: std::round(s * 120) / 120, matching fractional-scale-v1
        for s in (1.0, 1.25, 1.5, 1.75, 2.0, 3.0):
            self.assertEqual(kwin.quantize_scale(s), s)
        self.assertEqual(kwin.quantize_scale(1.3), 156 / 120)
        self.assertEqual(kwin.quantize_scale(1.3), 1.3)
        self.assertEqual(kwin.quantize_scale(1.0001), 1.0)
        self.assertEqual(kwin.quantize_scale(1.2345), round(1.2345 * 120) / 120)
        # std::round, not Python's banker's round: 1.4375 * 120 == 172.5
        self.assertEqual(kwin.quantize_scale(1.4375), 173 / 120)
        self.assertEqual(kwin.quantize_scale(1.3 + 1 / 240), 157 / 120)
        # the wl_fixed we marshal ourselves rounds; wayland_mini's "f" (shared
        # with the wlroots path, and deliberately left alone) truncates
        self.assertEqual(kwin.to_fixed(1.5), 384)
        self.assertEqual(kwin.to_fixed(1.25), 320)
        self.assertEqual(kwin.to_fixed(1.3), 333)
        self.assertEqual(int(1.3 * 256), 332)
        # and the value KWin computes from what we send comes back as 1.3
        self.assertEqual(kwin.from_fixed(333 / 256.0), 1.3)
        self.assertEqual(kwin.from_fixed(332 / 256.0), 1.3)

    def test_untouched_shared_fixed_point_marshalling(self):
        from wdotool import wayland_mini
        self.assertEqual(wayland_mini._marshal([("f", 1.3)]),
                         struct.pack("<i", 332))

    def test_logical_size_is_kwins_rule(self):
        L = kwin.logical_size
        self.assertEqual(L(2560, 1600, "normal", 1.25), (2048, 1280))
        self.assertEqual(L(2560, 1600, "normal", 1.5), (1707, 1067))
        self.assertEqual(L(1920, 1080, "270", 1.0), (1080, 1920))
        self.assertEqual(L(1920, 1080, "flipped-90", 2.0), (540, 960))
        # wlroots truncates where KWin does not
        self.assertEqual(core.logical_size(1111, 666, "normal", 1.5), (740, 444))

    def test_logical_size_splits_at_plasma_6(self):
        """Measured against the compositor: 6.x takes the enclosing integer,
        5.27 rounds. One pixel short means the neighbour overlaps."""
        L = kwin.logical_size
        for px, scale, six, five in ((1920, 1.4, 1372, 1371),
                                     (1080, 1.4, 772, 771),
                                     (1280, 1.5, 854, 853),
                                     (1280, 157 / 120, 979, 978),
                                     (1111, 1.5, 741, 741),
                                     (1920, 1.3, 1477, 1477),
                                     (2560, 1.25, 2048, 2048)):
            self.assertEqual(L(px, px, "normal", scale, True), (six, six),
                             (px, scale))
            self.assertEqual(L(px, px, "normal", scale, False), (five, five),
                             (px, scale))
        # 5.27 is exactly Mutter's layout-mode-1 rule
        self.assertEqual(L(1920, 1080, "90", 1.4, False),
                         mutter.logical_size(1920, 1080, "90", 1.4))

    def test_transform_mapping(self):
        # libkscreen's toKScreenRotation: 1 -> left, 3 -> right, 4..7 reflect
        words = {0: "normal", 1: "left", 2: "inverted", 3: "right",
                 4: "normal X axis", 5: "left X axis", 6: "inverted X axis",
                 7: "right X axis"}
        for n, want in words.items():
            rot, refl = core.RANDR_VIEW[kwin.from_transform(n)]
            self.assertEqual(rot + core.REFLECTION_SUFFIX.get(refl, ""), want, n)
            self.assertEqual(kwin.to_transform(kwin.from_transform(n)), n)
        table = {"normal": 0, "90": 3, "180": 2, "270": 1, "flipped": 4,
                 "flipped-90": 7, "flipped-180": 6, "flipped-270": 5}
        for name, n in table.items():
            self.assertEqual(kwin.to_transform(name), n, name)
            self.assertEqual(kwin.from_transform(n), name, n)
        self.assertEqual(kwin.from_transform(99), "normal")
        self.assertEqual(sorted(kwin.KWIN_FROM_SWAY.values()), list(range(8)))

    def test_normalise(self):
        self.assertEqual(kwin.normalise({"a": (-100, 0), "b": (1820, -50)}),
                         {"a": (0, 50), "b": (1920, 0)})
        self.assertEqual(kwin.normalise({"a": (0, 0), "b": (1920, 0)}),
                         {"a": (0, 0), "b": (1920, 0)})
        # a layout already clear of the origin is not re-anchored
        self.assertEqual(kwin.normalise({"a": (100, 100)}), {"a": (100, 100)})
        self.assertEqual(kwin.normalise({}), {})

    def test_match_mode(self):
        modes = [Mode(w=1920, h=1080, refresh_mhz=60020, mode_id="4278190081"),
                 Mode(w=1920, h=1080, refresh_mhz=48000, mode_id="4278190082"),
                 Mode(w=1280, h=720, refresh_mhz=59860, custom=True,
                      name="fancy")]
        self.assertEqual(kwin.match_mode(modes, 1920, 1080).mode_id,
                         "4278190081")
        self.assertEqual(kwin.match_mode(modes, 1920, 1080, 50.0).mode_id,
                         "4278190082")
        self.assertIsNone(kwin.match_mode(modes, 1920, 1080, 30.0,
                                          tolerance=1.0))
        self.assertIsNone(kwin.match_mode(modes, 1280, 720))   # no object
        self.assertIsNone(kwin.match_mode(modes, 800, 600))

    def test_mode_object_matching(self):
        pub = {"modes": [{"id": 11, "w": 1920, "h": 1080, "refresh": 60020},
                         {"id": 12, "w": 1920, "h": 1080, "refresh": 48000},
                         {"id": 13, "w": 1280, "h": 720, "refresh": 0}]}
        M = kwin.KwinOutputs._mode_object
        self.assertEqual(M(pub, Mode(w=1920, h=1080, refresh_mhz=48000)), 12)
        self.assertEqual(M(pub, Mode(w=1920, h=1080, refresh_mhz=59000)), 11)
        self.assertEqual(M(pub, Mode(w=1920, h=1080)), 11)  # no rate: first
        self.assertEqual(M(pub, Mode(w=1280, h=720)), 13)
        self.assertIsNone(M(pub, Mode(w=800, h=600)))

    def test_non_positive_scale_is_kept_out_of_the_wire(self):
        """KWin drops a scale <= 0 silently (no error, no failure), so it
        never reaches the wire."""
        out = core.OutputState(name="eDP-1", active=True, scale=1.5)
        t = core.Target(output=out, stanza=None, scale=0.0)
        ko = kwin.KwinOutputs.__new__(kwin.KwinOutputs)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(ko._scale_for(t), 1.5)
        self.assertEqual(err.getvalue(), "xrandr: scale 0 is not usable on "
                                         "KWin; keeping 1.5 for eDP-1\n")
        t.scale = 1.3
        self.assertEqual(ko._scale_for(t), 1.3)

    def test_restore_command(self):
        a = core.OutputState(name="eDP-1", active=True, x=0, y=0,
                             current=Mode(w=1920, h=1080, refresh_mhz=60020))
        b = core.OutputState(name="DP-1", active=True, x=1920, y=0, scale=1.5,
                             transform="270",
                             current=Mode(w=2560, h=1600, refresh_mhz=59972))
        c = core.OutputState(name="HDMI-1", active=False)
        # every property is spelled out, defaults included: this line is the
        # only undo KWin leaves, and a --rotate/--scale omitted because the
        # old value was the default one would not undo the new one
        self.assertEqual(
            kwin.restore_command([a, b, c], primary="DP-1"),
            "xrandr --output eDP-1 --mode 1920x1080 --rate 60.02 --pos 0x0"
            " --rotate normal --reflect normal --scale 1"
            " --output DP-1 --mode 2560x1600 --rate 59.97 --pos 1920x0"
            " --rotate left --reflect normal --scale 1.5 --primary"
            " --output HDMI-1 --off")
        self.assertNotIn("--primary", kwin.restore_command([a, b, c]))
        self.assertEqual(kwin.restore_command([]), "")

    def test_wire_errors_are_one_line(self):
        with self.assertRaises(core.Fatal) as cm:
            with kwin.wire("sending the output configuration"):
                raise BrokenPipeError(32, "Broken pipe")
        msg = cm.exception.args[0]
        self.assertEqual(msg.count("\n"), 1)
        self.assertTrue(msg.startswith("lost the connection to the "
                                       "compositor while sending"), msg)
        with self.assertRaises(core.Fatal):
            with kwin.wire("x"):
                raise RuntimeError("wayland connection closed")


# ---------------------------------------------------------------- discovery

class Discovery(KwinCase):
    def test_globals_path_527(self):
        """Plasma 5.27: device 2 / management 3, one global per output."""
        self.svc = two_heads(dev_version=2, mgmt_version=3)
        ko = self.outputs()
        self.assertEqual((ko.dev_version, ko.mgmt_version,
                          ko.mgmt_advertised), (2, 3, 3))
        self.assertIsNone(ko.registry)
        outs = ko.snapshot(self.state())
        self.assertEqual([o.name for o in outs], ["eDP-1", "DP-1"])

    def test_globals_path_66(self):
        """Plasma 6.6: device 20 / management 19 — we still bind 2 / 12."""
        ko = self.outputs()
        self.assertEqual((ko.dev_version, ko.mgmt_version,
                          ko.mgmt_advertised), (2, 12, 19))
        outs = ko.snapshot(self.state())
        self.assertEqual([o.name for o in outs], ["eDP-1", "DP-1"])

    def test_registry_path(self):
        """Plasma 6.7+: no device global at all, devices arrive as new_ids."""
        for opcode in (0, 1):
            self.svc.close()
            self.svc = two_heads(dev_version=25, mgmt_version=22,
                                 registry_path=True, registry_opcode=opcode)
            ko = self.outputs()
            self.assertIsNotNone(ko.registry)
            self.assertEqual(ko.dev_version, 25)
            outs = ko.snapshot(self.state())
            self.assertEqual([(o.name, o.active, o.x, o.w) for o in outs],
                             [("eDP-1", True, 0, 1920),
                              ("DP-1", True, 1920, 2560)])

    def test_snapshot_fields(self):
        self.svc = three_heads()
        outs = self.outputs().snapshot(self.state())
        e, d, h = outs
        self.assertEqual((e.active, e.x, e.y, e.w, e.h, e.scale, e.transform),
                         (True, 0, 0, 1920, 1080, 1.0, "normal"))
        self.assertEqual((e.make, e.model, e.serial, e.mm_w, e.mm_h, e.ident),
                         ("BOE", "0x0a1b", "0x00000000", 344, 194, 1))
        self.assertEqual(e.subpixel, "none")
        self.assertEqual(d.subpixel, "horizontal rgb")
        self.assertIs(e.current, e.modes[0])
        self.assertEqual([(m.w, m.h, m.refresh_mhz) for m in e.modes],
                         [(m[0], m[1], m[2]) for m in EDP_MODES])
        self.assertTrue(e.modes[0].preferred)
        self.assertFalse(e.modes[1].preferred)
        self.assertEqual((d.x, d.y, d.w, d.h), (1920, 0, 2560, 1600))
        self.assertEqual((h.active, h.x, h.w, h.current), (False, 0, 0, None))
        self.assertFalse(any(o.virtual_modes for o in outs))

    def test_uuid_and_edid_collected(self):
        ko = self.outputs()
        ko.snapshot(self.state())
        self.assertEqual(ko.uuid["eDP-1"], "d0a1b-eDP-1-uuid")
        self.assertEqual(ko.edid["eDP-1"],
                         "AP///////wAGrxUKAAAAAAAcAQOAIhN4")
        self.assertEqual(ko.edid["DP-1"], "")

    def test_name_falls_back_to_uuid_below_device_2(self):
        self.svc = two_heads(dev_version=1, mgmt_version=1)
        outs = self.outputs().snapshot(self.state())
        self.assertEqual([o.name for o in outs],
                         ["d0a1b-eDP-1-uuid", "del-dp1-uuid"])

    def test_done_is_the_publish_barrier(self):
        self.svc = two_heads()
        self.svc.withhold_done = "DP-1"
        outs = self.outputs().snapshot(self.state())
        self.assertEqual([o.name for o in outs], ["eDP-1"])

    def test_scale_round_trip(self):
        self.svc = FakeKWin([EDP(scale=1.3), DP(scale=1.5)])
        outs = self.outputs().snapshot(self.state())
        # the wl_fixed alone would read back 1.30078125 and make every run
        # look like a change; quantising to 1/120 recovers KWin's own value
        self.assertEqual(outs[0].scale, 1.3)
        self.assertEqual(outs[1].scale, 1.5)
        self.assertEqual((outs[0].w, outs[0].h), (1477, 831))
        self.assertEqual((outs[1].w, outs[1].h), (1707, 1067))

    def test_gamma_manager_is_probed(self):
        self.assertFalse(self.outputs().has_gamma)
        self.svc.close()
        self.svc = two_heads(gamma=True)
        self.assertTrue(self.outputs().has_gamma)

    def test_no_management_global_is_one_line(self):
        self.svc.close()
        self.svc = FakeKWin([EDP()])
        self.svc.globals = lambda: [(1, DEV, 20)]
        with self.assertRaises(core.Fatal) as cm:
            self.outputs()
        self.assertEqual(cm.exception.args[0],
                         "compositor does not advertise kde_output_management_v2"
                         " (not a KDE Plasma session?)\n")


# ---------------------------------------------------------------- query

class Query(KwinCase):
    def fixture(self):
        return three_heads()

    def test_query_bytes(self):
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        # `primary` comes from kde_output_order_v1, with no state file and
        # nothing ever set: real xrandr always names one too
        self.assertEqual(out, (
            "Screen 0: minimum 16 x 16, current 4480 x 1600, maximum 32767 x 32767\n"
            "eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis "
            "y axis) 344mm x 194mm\n"
            "   1920x1080     60.02*+  48.00  \n"
            "   1680x1050     59.95  \n"
            "   1280x720      59.86  \n"
            "DP-1 connected 2560x1600+1920+0 (normal left inverted right x axis "
            "y axis) 597mm x 373mm\n"
            "   2560x1600     59.97*+\n"
            "   1920x1200     59.95  \n"
            "   1920x1080     60.00  \n"
            "HDMI-1 connected (normal left inverted right x axis y axis)\n"
            "   1280x1024     60.02 +\n"
            "   1024x768      60.00  \n"))

    def test_query_shows_rotation_and_scale(self):
        self.svc = FakeKWin([EDP(transform=1, scale=1.5)])
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        # transform 1 is xrandr `left`, and the logical size is the swapped
        # mode size divided by the scale
        self.assertIn("eDP-1 connected primary 720x1280+0+0 left ", out)

    def test_listmonitors_and_providers(self):
        code, out, err = self.run_cli("--listmonitors")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, ("Monitors: 2\n"
                               " 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1\n"
                               " 1: +DP-1 2560/597x1600/373+1920+0  DP-1\n"))
        # and the primary monitor is listed first, as the X server does and
        # as KWin's own XWayland does (measured)
        self.run_cli("--output", "DP-1", "--primary")
        code, out, err = self.run_cli("--listmonitors")
        self.assertEqual(out, ("Monitors: 2\n"
                               " 0: +*DP-1 2560/597x1600/373+1920+0  DP-1\n"
                               " 1: +eDP-1 1920/344x1080/194+0+0  eDP-1\n"))
        code, out, err = self.run_cli("--listproviders")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("name:kwin", out)

    def test_verbose_block(self):
        code, out, err = self.run_cli("--verbose")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("\tSubpixel:   none", out)
        self.assertIn("\tIdentifier: 0x1", out)


# ---------------------------------------------------------------- apply

class Apply(KwinCase):
    def test_right_of_sends_only_the_delta(self):
        code, out, err = self.run_cli("--output", "DP-1", "--right-of",
                                      "eDP-1", "--mode", "1920x1200")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.strip_save(err), "")
        self.assertEqual(len(self.svc.applied), 1)
        # eDP-1 does not move and is not mentioned at all
        self.assertEqual(self.svc.applied[0], [("mode", "DP-1", 1)])
        self.assertEqual(self.svc.by_name("DP-1")["current"], 1)

    def test_no_op_creates_no_configuration(self):
        code, out, err = self.run_cli("--output", "eDP-1", "--mode",
                                      "1920x1080", "--pos", "0x0")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual((self.svc.created, self.svc.applied), (0, []))

    def test_one_configuration_per_apply(self):
        for _ in range(2):
            self.run_cli("--output", "DP-1", "--mode", "1920x1200")
            self.run_cli("--output", "DP-1", "--mode", "2560x1600")
        # four real changes, four configuration objects, four applies: an
        # object is never reused (a second apply on one is fatal)
        self.assertEqual(self.svc.created, 4)
        self.assertEqual(len(self.svc.applied), 4)

    def test_second_apply_on_one_configuration_is_fatal(self):
        """The rule the backend is built around, proven against the fake."""
        ko = self.outputs()
        ko.snapshot(self.state())
        dev = ko.by_name["DP-1"]["id"]
        cfg = ko.conn.alloc()
        ko.conn.on(cfg, lambda op, cur, fds: None)
        ko.conn.send(ko.mgmt, 0, [("u", cfg)])
        ko.conn.send(cfg, kwin.REQ_POSITION, [("u", dev), ("i", 1920), ("i", 8)])
        ko.conn.send(cfg, kwin.REQ_APPLY, [])
        ko.conn.roundtrip()
        ko.conn.send(cfg, kwin.REQ_APPLY, [])
        # the fake closes the socket right after the error, so the write of
        # the next roundtrip can lose the race: what must hold is that the
        # connection is dead and says why (asserting on which of the two
        # exceptions wins makes this flaky under load)
        for _ in range(3):
            try:
                ko.conn.dispatch(timeout=2.0)
            except (RuntimeError, OSError):
                break
        self.assertIn("already applied", ko.conn.dead or "")

    def test_off_and_back_on(self):
        code, _out, err = self.run_cli("--output", "DP-1", "--off")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(self.svc.applied[-1], [("enable", "DP-1", False)])
        self.assertFalse(self.svc.by_name("DP-1")["enabled"])
        code, _out, err = self.run_cli("--output", "DP-1", "--auto",
                                       "--right-of", "eDP-1")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        # coming back from disabled the output is fully described: KWin's
        # stored geometry for a disabled output is not what the query showed
        self.assertEqual(self.svc.applied[-1],
                         [("enable", "DP-1", True), ("mode", "DP-1", 0),
                          ("transform", "DP-1", 0), ("position", "DP-1",
                                                     (1920, 0)),
                          ("scale", "DP-1", 256)])
        self.assertTrue(self.svc.by_name("DP-1")["enabled"])

    def test_last_output_refused_client_side(self):
        self.svc = one_head()
        code, out, err = self.run_cli("--output", "eDP-1", "--off")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: cannot disable all outputs (KWin "
                              "requires at least one enabled output)\n")
        self.assertEqual((self.svc.created, self.svc.applied), (0, []))
        # and with two heads the refusal only fires when both go
        self.svc = two_heads()
        code, _out, err = self.run_cli("--output", "eDP-1", "--off",
                                       "--output", "DP-1", "--off")
        self.assertEqual(code, 1)
        self.assertIn("cannot disable all outputs", err)
        self.assertEqual(self.svc.created, 0)

    def test_dryrun_refuses_without_touching_the_compositor(self):
        self.svc = one_head()
        code, out, err = self.run_cli("--dryrun", "--output", "eDP-1", "--off")
        self.assertEqual(code, 1)
        self.assertIn("cannot disable all outputs", err)
        code, out, err = self.run_cli("--dryrun", "--output", "eDP-1",
                                      "--mode", "1280x720")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((self.svc.created, self.svc.applied), (0, []))

    def test_negative_positions_are_normalised(self):
        code, _out, err = self.run_cli("--output", "eDP-1", "--pos",
                                       "-100x-50")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        # KWin fails a configuration with an enabled output at a negative
        # coordinate; the whole layout slides back to the origin instead —
        # and eDP-1, which lands where it already was, is not mentioned
        self.assertEqual(self.svc.applied[-1],
                         [("position", "DP-1", (2020, 50))])
        self.assertEqual([(h["name"], h["x"], h["y"]) for h in self.svc.heads],
                         [("eDP-1", 0, 0), ("DP-1", 2020, 50)])

    def test_same_as_shares_the_position(self):
        code, _out, err = self.run_cli("--output", "DP-1", "--same-as",
                                       "eDP-1")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(self.svc.applied[-1], [("position", "DP-1", (0, 0))])
        self.assertEqual([(h["x"], h["y"]) for h in self.svc.heads],
                         [(0, 0), (0, 0)])

    def test_rotate_and_scale_on_the_wire(self):
        code, _out, err = self.run_cli("--output", "eDP-1", "--rotate", "left",
                                       "--scale", "1.3")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        reqs = dict((r[0], r[1:]) for r in self.svc.applied[-1])
        self.assertEqual(reqs["transform"], ("eDP-1", 1))   # left == wl 90
        self.assertEqual(reqs["scale"], ("eDP-1", 333))     # rounded wl_fixed
        self.assertEqual(self.svc.by_name("eDP-1")["scale"], 1.3)
        # 1920/1.3 is 1476.9 and 1080/1.3 is 830.8: the enclosing integer
        # KWin does not enforce the XML's no-gaps sentence, so DP-1 stays
        # where xrandr leaves it and is not mentioned at all
        self.assertNotIn("position", reqs)
        self.assertEqual(self.svc.by_name("DP-1")["x"], 1920)
        code, out, _err = self.run_cli()
        self.assertIn("eDP-1 connected primary 831x1477+0+0 left ", out)

    def test_relative_placement_uses_the_logical_size(self):
        code, _out, err = self.run_cli("--output", "eDP-1", "--rotate", "left",
                                       "--scale", "1.3",
                                       "--output", "DP-1", "--right-of",
                                       "eDP-1")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        reqs = dict((r[0], r[1:]) for r in self.svc.applied[-1])
        # the neighbour follows the transform-swapped, scaled width of
        # eDP-1: round(1080 / 1.3) == 831, not the 1080 hardware pixels
        self.assertEqual(reqs["position"], ("DP-1", (831, 0)))

    def test_scale_is_not_resent_when_unchanged(self):
        self.run_cli("--output", "eDP-1", "--scale", "1.3")
        self.svc.applied.clear()
        code, _out, err = self.run_cli("--output", "eDP-1", "--scale", "1.3")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(self.svc.applied, [])

    def test_primary_moves_the_output_order_with_set_priority(self):
        """set_primary_output is accepted and ignored by KWin (measured on
        5.27 and on 6.6): what moves the primary is set_priority over the
        whole list, and what reports it is kde_output_order_v1."""
        self.assertEqual(self.svc.primary, "eDP-1")
        code, _out, err = self.run_cli("--output", "DP-1", "--primary")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual([r for r in self.svc.applied[-1]
                          if r[0] in ("priority", "primary")],
                         [("primary", "DP-1"),
                          ("priority", "DP-1", 1), ("priority", "eDP-1", 2)])
        self.assertEqual(self.svc.primary, "DP-1")
        self.svc.applied.clear()
        code, out, err = self.run_cli("--output", "DP-1", "--primary")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.svc.applied, [])      # already primary
        code, out, _err = self.run_cli()
        self.assertIn("DP-1 connected primary ", out)

    def test_primary_is_read_from_the_compositor(self):
        """No state file needed, and a disabled output is never the primary:
        both come from KWin's own output order."""
        code, out, _err = self.run_cli()
        self.assertIn("eDP-1 connected primary ", out)
        self.assertIsNone(State("kwin-test", path=self.state_path).d.get(
            "primary"))
        code, _out, err = self.run_cli("--output", "eDP-1", "--off")
        self.assertEqual(code, 0)
        code, out, _err = self.run_cli()
        self.assertIn("DP-1 connected primary ", out)
        self.assertNotIn("eDP-1 connected primary", out)

    def test_primary_below_set_priority_is_not_remembered(self):
        """K5: a --primary the backend never sent must not make --query lie."""
        self.svc.close()
        self.svc = two_heads(dev_version=2, mgmt_version=1, order=False)
        code, out, err = self.run_cli("--output", "DP-1", "--primary")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "xrandr: this KWin is too old for --primary "
                              "(kde_output_management_v2 version 1)\n")
        self.assertEqual((self.svc.created, self.svc.applied), (0, []))
        code, out, _err = self.run_cli("--listmonitors")
        self.assertNotIn("*", out)
        self.assertIsNone(State("kwin-test", path=self.state_path).primary)

    def test_primary_falls_back_to_the_state_file(self):
        """A KWin that announces no output order: what we set is remembered,
        and it is still only sent once."""
        self.svc.close()
        self.svc = two_heads(dev_version=2, mgmt_version=3, order=False)
        code, _out, err = self.run_cli("--output", "DP-1", "--primary")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual([r for r in self.svc.applied[-1] if r[0] == "priority"],
                         [("priority", "DP-1", 1), ("priority", "eDP-1", 2)])
        self.assertEqual(State("kwin-test", path=self.state_path).primary,
                         "DP-1")
        self.svc.applied.clear()
        code, _out, err = self.run_cli("--output", "DP-1", "--primary")
        self.assertEqual(self.svc.applied, [])

    def test_dryrun_records_no_primary(self):
        code, out, err = self.run_cli("--dryrun", "--output", "DP-1",
                                      "--primary")
        self.assertEqual(code, 0)
        self.assertEqual((self.svc.created, self.svc.primary), (0, "eDP-1"))
        self.assertEqual(State("kwin-test", path=self.state_path).primary,
                         "eDP-1")
        code, out, _err = self.run_cli()
        self.assertIn("eDP-1 connected primary ", out)

    def test_noprimary_warns(self):
        self.run_cli("--output", "DP-1", "--primary")
        code, _out, err = self.run_cli("--noprimary")
        self.assertEqual(code, 0)
        self.assertEqual(err, "xrandr: KWin keeps a primary output; keeping "
                              "DP-1\n")

    def test_failure_reason_is_surfaced_in_kwins_name(self):
        """KWin's own words, said in KWin's name: we never refuse a layout
        of our own accord, so a refusal has to point at the compositor."""
        self.svc.fail_next = "The driver rejected the output configuration: ENOSPC"
        code, out, err = self.run_cli("--output", "DP-1", "--mode",
                                      "1920x1200")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.strip_save(err),
                         "xrandr: KWin rejected the output configuration: "
                         "The driver rejected the output "
                         "configuration: ENOSPC\n")

    def test_an_overlapping_position_is_sent_and_kept(self):
        """The XML's "no gaps or overlaps" sentence is not enforced by the
        code and we add no rule of our own: an overlapping --pos goes on the
        wire exactly as asked and KWin keeps it.  Measured on Plasma 6 at
        two overlap widths: the shared region comes back byte-identical on
        both heads, KWin rendering each output as a view onto one scene."""
        code, _out, err = self.run_cli("--output", "DP-1", "--pos", "960x0")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(self.svc.applied[-1],
                         [("position", "DP-1", (960, 0))])
        self.assertEqual([(h["name"], h["x"], h["y"])
                          for h in self.svc.heads],
                         [("eDP-1", 0, 0), ("DP-1", 960, 0)])
        # ...and it reads back as the overlap it is
        code, out, _err = self.run_cli("--listmonitors")
        self.assertIn("+960+0", out)

    def test_failure_without_a_reason_on_527(self):
        self.svc = two_heads(dev_version=2, mgmt_version=3)
        self.svc.fail_next = "Position of enabled output DP-1 is negative"
        code, out, err = self.run_cli("--output", "DP-1", "--mode",
                                      "1920x1200")
        self.assertEqual((code, out), (1, ""))
        # management 3 is below failure_reason's `since 12`: no string exists
        self.assertEqual(self.strip_save(err),
                         "xrandr: KWin rejected the output configuration "
                         "(this KWin is too old to report why)\n")

    def test_hotplug_invalidation_is_retried_once(self):
        self.svc.invalidate_once = True
        code, _out, err = self.run_cli("--output", "DP-1", "--mode",
                                       "1920x1200")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(len(self.svc.applied), 2)
        self.assertEqual(self.svc.created, 2)       # a fresh object each time
        self.assertEqual(self.svc.by_name("DP-1")["current"], 1)

    def _hotplug_class(self):
        """A backend that unplugs a head between create_configuration and
        apply, the way a real hotplug lands."""
        tc = self

        class Hotplug(kwin.KwinOutputs):
            def _send(self, records, primary, sig=None):
                if not getattr(self, "_bounced", False):
                    self._bounced = True
                    tc.svc.unplug("HDMI-1")
                super()._send(records, primary, sig)
        return Hotplug

    def test_invalidation_is_retried_without_a_reason_string(self):
        """5.27 binds management 3, where failure_reason does not exist: the
        retry cannot key on the message. The outputs having moved under the
        configuration is the evidence, and it is there on every version."""
        self.svc = three_heads(hdmi_on=True, dev_version=2, mgmt_version=3)
        self.svc.invalidate_once = True
        orig = kwin.KwinOutputs
        kwin.KwinOutputs = self._hotplug_class()
        try:
            code, _out, err = self.run_cli("--output", "DP-1", "--scale",
                                           "1.5")
        finally:
            kwin.KwinOutputs = orig
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(self.svc.by_name("DP-1")["scale"], 1.5)
        self.assertEqual((self.svc.created, len(self.svc.applied)), (2, 2))
        self.assertEqual([h["name"] for h in self.svc.heads],
                         ["eDP-1", "DP-1"])

    def test_a_rejection_that_is_not_a_hotplug_is_not_retried(self):
        """The evidence test must not turn every failure into a second
        configuration: with the outputs unmoved, the message stands."""
        self.svc = two_heads(dev_version=2, mgmt_version=3)
        self.svc.fail_next = "Position of enabled output DP-1 is negative"
        code, _out, err = self.run_cli("--output", "DP-1", "--scale", "1.5")
        self.assertEqual(code, 1)
        self.assertEqual((self.svc.created, len(self.svc.applied)), (1, 1))
        self.assertIn("too old to report why", err)

    def test_invalidation_retry_rebuilds_from_a_fresh_snapshot(self):
        """The retry re-reads: the mode objects of the first attempt are gone
        once the device is re-announced, so matching has to be by value."""
        self.svc = three_heads(hdmi_on=True)
        self.svc.invalidate_once = True
        orig = kwin.KwinOutputs
        kwin.KwinOutputs = self._hotplug_class()
        try:
            code, _out, err = self.run_cli("--output", "DP-1", "--mode",
                                           "1920x1080", "--rate", "60")
        finally:
            kwin.KwinOutputs = orig
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual([h["name"] for h in self.svc.heads],
                         ["eDP-1", "DP-1"])
        self.assertEqual(self.svc.by_name("DP-1")["current"], 2)
        self.assertEqual(len(self.svc.applied), 2)

    def test_save_warning_and_restore_command(self):
        code, _out, err = self.run_cli("--output", "DP-1", "--off")
        self.assertEqual(code, 0)
        lines = err.splitlines()
        self.assertEqual(lines[0], "xrandr: " + kwin.SAVE_WARNING.rstrip())
        self.assertEqual(
            self.restore_line(err),
            "xrandr --output eDP-1 --mode 1920x1080 --rate 60.02 --pos 0x0"
            " --rotate normal --reflect normal --scale 1 --primary"
            " --output DP-1 --mode 2560x1600 --rate 59.97 --pos 1920x0"
            " --rotate normal --reflect normal --scale 1")
        # exactly once per invocation, even when several outputs move
        code, _out, err = self.run_cli("--output", "DP-1", "--auto",
                                       "--right-of", "eDP-1",
                                       "--output", "eDP-1", "--scale", "1.5")
        self.assertEqual(err.count("KWin applies and saves"), 1)
        # and the restore command names the layout as it was before
        self.assertIn("--output DP-1 --off", self.restore_line(err))

    def test_the_restore_command_is_a_real_inverse(self):
        """KWin has no temporary mode, so this line is the only undo there
        is: running it verbatim must put every property back, including the
        ones whose old value happened to be the default."""
        before = (self.svc.layout(), self.svc.primary)
        code, _out, err = self.run_cli("--output", "DP-1", "--rotate", "left",
                                       "--scale", "1.5", "--primary")
        self.assertEqual(code, 0)
        self.assertNotEqual((self.svc.layout(), self.svc.primary), before)
        line = self.restore_line(err).split()
        self.assertEqual(line[0], "xrandr")
        code, _out, err = self.run_cli(*line[1:])
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual((self.svc.layout(), self.svc.primary), before)

    def test_the_restore_command_survives_an_overlapping_layout(self):
        """An overlapping layout is nothing special to the undo line, which
        is worth pinning now that one is a layout a user can really be in:
        KWin takes an overlapping position (measured on Plasma 6, the shared
        region byte-identical on both heads), and because every position in
        the line is spelled absolutely, replaying it puts the overlap back
        rather than re-deriving a side-by-side layout."""
        code, _out, err = self.run_cli("--output", "DP-1", "--pos", "960x0")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        before = (self.svc.layout(), self.svc.primary)
        code, _out, err = self.run_cli("--output", "DP-1", "--right-of",
                                       "eDP-1")
        self.assertEqual(code, 0)
        self.assertNotEqual((self.svc.layout(), self.svc.primary), before)
        line = self.restore_line(err)
        self.assertIn("--output DP-1 --mode 2560x1600", line)
        self.assertIn("--pos 960x0", line)
        code, _out, err = self.run_cli(*line.split()[1:])
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual((self.svc.layout(), self.svc.primary), before)

    def test_a_failed_apply_claims_nothing_was_saved(self):
        """K3: the warning says KWin has already saved the layout and offers
        a command that would change the live one -- neither is true when the
        apply was refused."""
        self.svc.fail_next = "The driver rejected the output configuration"
        code, _out, err = self.run_cli("--output", "DP-1", "--scale", "1.5")
        self.assertEqual(code, 1)
        self.assertNotIn("KWin applies and saves", err)
        self.assertNotIn("to restore the previous layout", err)
        self.assertEqual(self.svc.by_name("DP-1")["scale"], 1.0)
        # and the next, successful, apply does say it
        code, _out, err = self.run_cli("--output", "DP-1", "--scale", "1.5")
        self.assertEqual(code, 0)
        self.assertIn("KWin applies and saves", err)

    def test_a_compositor_that_hangs_up_is_one_xrandr_line(self):
        """K2: the send side of an apply is as guarded as the receive side --
        a KWin that goes away mid-apply is not a bare errno."""
        tc = self

        class Hangup(kwin.KwinOutputs):
            def plan(self, state, targets):
                out = super().plan(state, targets)
                tc.svc.hangup()
                return out
        orig = kwin.KwinOutputs
        kwin.KwinOutputs = Hangup
        try:
            code, out, err = self.run_cli("--output", "DP-1", "--scale", "1.5")
        finally:
            kwin.KwinOutputs = orig
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.count("\n"), 1, err)
        self.assertTrue(err.startswith("xrandr: "), err)
        self.assertNotIn("Traceback", err)

    def test_no_outputs_is_a_refusal_not_an_empty_screen(self):
        """K6: management present, not one device published. Printing an
        empty screen and calling every apply a success hides a real
        failure."""
        for svc in (FakeKWin([]),
                    two_heads(registry_path=True, registry_version=20)):
            self.svc.close()
            self.svc = svc
            code, out, err = self.run_cli()
            self.assertEqual((code, out), (1, ""))
            self.assertIn("xrandr: kde_output_management_v2 is advertised but"
                          " the compositor announced no outputs\n", err)
            code, out, err = self.run_cli("--output", "eDP-1", "--off")
            self.assertEqual((code, self.svc.created), (1, 0))
        # the too-old registry says which version it was
        self.assertIn("kde_output_device_registry_v2 version 20 is older than"
                      " the 21", err)

    def test_logical_size_gates_the_layout_on_the_plasma_version(self):
        """Plasma 6 takes the enclosing integer: a neighbour placed one pixel
        short of it is an overlap KWin silently keeps."""
        for mgmt, dev, w, h in ((19, 20, 1372, 772), (3, 2, 1371, 771)):
            self.svc.close()
            self.svc = FakeKWin([EDP(scale=1.4), DP(x=4000)],
                                dev_version=dev, mgmt_version=mgmt)
            code, out, _err = self.run_cli()
            self.assertIn("eDP-1 connected primary %dx%d+0+0 " % (w, h), out)
            code, _out, err = self.run_cli("--output", "DP-1", "--right-of",
                                           "eDP-1")
            self.assertEqual((code, self.strip_save(err)), (0, ""))
            self.assertEqual(self.svc.applied[-1],
                             [("position", "DP-1", (w, 0))])

    def test_persistent_is_the_only_mode(self):
        code, _out, err = self.run_cli("--persistent", "--output", "DP-1",
                                       "--mode", "1920x1200")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertIn("no temporary mode", err)

    def test_brightness_and_gamma_warn_and_succeed(self):
        code, out, err = self.run_cli("--output", "eDP-1", "--brightness",
                                      "0.5", "--output", "DP-1", "--gamma",
                                      "1.1:1:0.9")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.strip_save(err),
                         "xrandr: --brightness/--gamma are not supported on "
                         "KWin (no gamma LUT API); ignoring for eDP-1\n"
                         "xrandr: --brightness/--gamma are not supported on "
                         "KWin (no gamma LUT API); ignoring for DP-1\n")
        self.assertEqual(self.state().gamma(), {})

    def test_custom_modes_need_a_real_twin(self):
        self.svc = one_head()
        self.run_cli("--newmode", "fancy", "74.5", "1280", "1344", "1472",
                     "1664", "720", "723", "728", "748",
                     "--addmode", "eDP-1", "fancy")
        self.run_cli("--newmode", "weird", "50.0", "1000", "1010", "1020",
                     "1100", "500", "503", "508", "520",
                     "--addmode", "eDP-1", "weird")
        code, _out, err = self.run_cli("--output", "eDP-1", "--mode", "fancy")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(self.svc.by_name("eDP-1")["current"], 3)  # 1280x720
        code, out, err = self.run_cli("--output", "eDP-1", "--mode", "weird")
        self.assertEqual((code, out, err), (1, "",
                                            "xrandr: cannot find mode weird\n"))
        self.assertFalse(self.outputs().supports_custom_modes("eDP-1"))

    def test_mode_and_rate_pick_the_mode_object(self):
        code, _out, err = self.run_cli("--output", "eDP-1", "--mode",
                                       "1920x1080", "--rate", "48")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(self.svc.applied[-1], [("mode", "eDP-1", 1)])
        self.assertEqual(self.svc.by_name("eDP-1")["current"], 1)

    def test_off_records_the_last_mode(self):
        self.run_cli("--output", "DP-1", "--off")
        self.assertEqual(self.state().lastmodes()["DP-1"], [2560, 1600, 59972])

    def test_apply_through_the_registry_path(self):
        self.svc = two_heads(dev_version=25, mgmt_version=22,
                             registry_path=True)
        code, _out, err = self.run_cli("--output", "DP-1", "--above", "eDP-1")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        self.assertEqual(sorted(self.svc.applied[-1]),
                         sorted([("position", "DP-1", (0, 0)),
                                 ("position", "eDP-1", (0, 1600))]))

    def test_randr_1_0_path(self):
        self.svc = one_head()
        code, _out, err = self.run_cli("-s", "1280x720", "-o", "left")
        self.assertEqual((code, self.strip_save(err)), (0, ""))
        h = self.svc.by_name("eDP-1")
        self.assertEqual((h["current"], h["transform"]), (3, 1))


# ---------------------------------------------------------------- selection

class Detection(unittest.TestCase):
    def session(self, *a, **kw):
        """cli.Session, with everything detection opened closed again when
        the test ends.  A CLI just exits; a test process does not, and a
        connection left to the garbage collector surfaces as a
        ResourceWarning in the middle of some later test's captured
        stderr."""
        sess = cli.Session(*a, **kw)
        self.addCleanup(lambda: [pr.close() for pr in sess.probes.values()])
        return sess

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-kwin-det-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.svc = two_heads()
        self.addCleanup(self.svc.close)
        self.saved = {k: os.environ.get(k)
                      for k in ("WXRANDR_BACKEND", "XDG_RUNTIME_DIR",
                                "WXRANDR_PERSIST", "WAYLAND_DISPLAY")}
        os.environ["XDG_RUNTIME_DIR"] = self.tmp
        os.environ.pop("WXRANDR_BACKEND", None)
        os.environ.pop("WXRANDR_PERSIST", None)
        os.environ.pop("WAYLAND_DISPLAY", None)
        self.patched = [(wsession, "find_sway_socket",
                         wsession.find_sway_socket),
                        (wsession, "find_wayland_socket",
                         wsession.find_wayland_socket),
                        (wsession, "find_session_bus",
                         wsession.find_session_bus)]
        path = self.svc.path
        wsession.find_sway_socket = lambda: None
        wsession.find_wayland_socket = lambda: (os.getuid(),
                                                os.path.dirname(path), path)
        wsession.find_session_bus = lambda: None
        self.sessions = []
        orig_init = cli.Session.__init__
        self.orig_init = orig_init
        sessions = self.sessions

        def recording_init(sess, forced=None):
            orig_init(sess, forced)
            sessions.append(sess)
        cli.Session.__init__ = recording_init

    def tearDown(self):
        cli.Session.__init__ = self.orig_init
        for sess in self.sessions:
            if sess.kwin is not None:
                sess.kwin.close()
                # Session adopts the connection _probe_kwin() opened, and
                # KwinOutputs only closes one it opened itself, so the CLI
                # leaves this one to process exit. Nothing exits here: close
                # it, or the collector reports it as unclosed at some
                # unrelated point later in a shared runner.
                try:
                    sess.kwin.conn.close()
                except OSError:
                    pass
        for mod, name, orig in self.patched:
            setattr(mod, name, orig)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue()

    def test_detection_closes_the_connections_it_does_not_keep(self):
        """Detection opens a connection per backend it tries and Session
        reuses exactly one; the others have to be closed there and then.
        Left to the collector they surface as a ResourceWarning on stderr at
        an arbitrary later moment -- in a CLI, in the middle of somebody
        else's output."""
        sess = self.session()
        self.assertEqual(sess.backend, "kwin")
        live = sorted(p.name for p in sess.probes.values()
                      if p.handle is not None)
        self.assertEqual(live, ["kwin"])
        self.assertIs(sess.probes["kwin"].handle, sess.kwin.conn)

    def test_probe(self):
        conn = kwin.probe(self.svc.path)
        self.assertIsNotNone(conn)
        conn.close()
        self.assertIsNone(kwin.probe(os.path.join(self.tmp, "nope")))

    def test_auto_detect_picks_kwin_before_gnome_and_wlr(self):
        sess = self.session()
        self.assertEqual((sess.backend, sess.compositor_name),
                         ("kwin", "kwin"))
        self.assertIsNone(sess.ipc)
        self.assertIsNone(sess.wlr)
        self.assertIsNone(sess.mutter)
        self.assertFalse(sess.persistent)
        self.assertEqual([o.name for o in sess.snapshot()], ["eDP-1", "DP-1"])

    def test_forced_and_alias(self):
        for val in ("kwin", "kde"):
            os.environ["WXRANDR_BACKEND"] = val
            self.assertEqual(self.session().backend, "kwin", val)
        os.environ["WXRANDR_PERSIST"] = "1"
        self.assertTrue(self.session().persistent)

    def test_sway_still_wins(self):
        wsession.find_sway_socket = lambda: "/nonexistent/sway.sock"
        code, out, err = self.main("--listproviders")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "Can't open display \n")

    def test_forced_without_kde_is_one_line(self):
        self.svc.globals = lambda: [(1, DEV, 20)]
        os.environ["WXRANDR_BACKEND"] = "kwin"
        code, out, err = self.main("--listproviders")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: compositor does not advertise "
                              "kde_output_management_v2 (not a KDE Plasma "
                              "session?)\n")
        wsession.find_wayland_socket = lambda: None
        code, out, err = self.main("--listproviders")
        self.assertEqual((code, err), (1, "Can't open display \n"))

    def test_end_to_end_through_the_real_session(self):
        code, out, err = self.main("--output", "DP-1", "--left-of", "eDP-1")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("KWin applies and saves", err)
        self.assertEqual([(h["name"], h["x"]) for h in self.svc.heads],
                         [("eDP-1", 2560), ("DP-1", 0)])
        code, out, err = self.main("--listproviders")
        self.assertEqual(code, 0)
        self.assertIn("name:kwin", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
