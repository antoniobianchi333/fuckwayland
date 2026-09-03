"""wxrandr Mutter backend tests: a wire-level mock org.gnome.Mutter.DisplayConfig
service on dbus_mini's mock bus (tests/test_dbus_mini.py) that serves
GetCurrentState from fixtures (1/2/3 monitors, a 2560x1600 panel with
supported scales [1, 1.25, 1.5, 2], an interlaced mode, layout modes 1 and 2)
and validates ApplyMonitorsConfig exactly like mutter does (serial, connector
and mode ids, scale in supported_scales, mirror members' modes equal, no
overlap, edge adjacency, exactly one primary, positions anchored at 0,0,
ApplyMonitorsConfigAllowed) and emits MonitorsChanged. No GNOME needed."""

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
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_dbus_mini as tdm                                    # noqa: E402
from wdotool import dbus_mini, session as wsession              # noqa: E402
from wdotool.dbus_mini import Bus, Message, Variant             # noqa: E402
from wxrandr import cli, core, mutter                           # noqa: E402
from wxrandr.core import Mode, Stanza, State                    # noqa: E402

ERR = dbus_mini.ERR
DEST, PATH, IFACE = mutter.DEST, mutter.PATH, mutter.IFACE

# ---------------------------------------------------------------- fixtures

EDP = ("eDP-1", "BOE", "0x0a1b", "0x00000000")
DP = ("DP-1", "DEL", "DELL U2723QE", "7PJ2XM3")
HDMI = ("HDMI-1", "SAM", "SyncMaster", "H1AK500000")
S4 = [1.0, 1.25, 1.5, 2.0]


def M(mid, w, h, rate, scales, preferred=False, interlaced=False):
    return {"id": mid, "w": w, "h": h, "rate": rate, "scales": list(scales),
            "preferred": preferred, "interlaced": interlaced}


MONITORS = {
    "eDP-1": (EDP, [M("1920x1080@60.020", 1920, 1080, 60.02, S4, preferred=True),
                    M("1920x1080@48.000", 1920, 1080, 48.0, S4),
                    M("1680x1050@59.954", 1680, 1050, 59.954, [1.0, 1.25, 1.5]),
                    M("1280x720@59.860", 1280, 720, 59.86, [1.0])],
              {"width-mm": 344, "height-mm": 194, "is-builtin": True,
               "display-name": "Built-in display"}),
    "DP-1": (DP, [M("2560x1600@59.972", 2560, 1600, 59.972, S4, preferred=True),
                  M("1920x1200@59.950", 1920, 1200, 59.95, S4),
                  M("1920x1080@60.000", 1920, 1080, 60.0, S4)],
             {"width-mm": 597, "height-mm": 373, "is-builtin": False,
              "display-name": 'Dell Inc. 27"', "is-underscanning": False}),
    "HDMI-1": (HDMI, [M("1280x1024@60.020", 1280, 1024, 60.02, [1.0], preferred=True),
                      M("1920x1080i@60.000", 1920, 1080, 60.0, [1.0, 1.5],
                        interlaced=True),
                      M("1024x768@60.004", 1024, 768, 60.004, [1.0])],
               {"width-mm": 376, "height-mm": 301, "is-builtin": False,
                "display-name": 'Samsung 19"', "is-underscanning": True}),
}


class MutterError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.name, self.message = name, message


def _overlaps(a, b):
    return (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
            and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


def _adjacent(a, b):
    """mtk_rectangle_is_adjacent_to: a shared x-edge with strict y overlap or
    a shared y-edge with strict x overlap (corner contact does not count)."""
    ax1, ay1, ax2, ay2 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx1, by1, bx2, by2 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    if (ax1 == bx2 or ax2 == bx1) and not (ay2 <= by1 or by2 <= ay1):
        return True
    if (ay1 == by2 or ay2 == by1) and not (ax2 <= bx1 or bx2 <= ax1):
        return True
    return False


class FakeMutter:
    """State + validation of mutter's DisplayConfig (46/50 rules, 46 message
    strings). `logical` entries: (x, y, scale, transform, primary,
    [(connector, mode_id), ...])."""

    def __init__(self, connectors, logical, layout_mode=1, serial=42):
        self.monitors = [MONITORS[c] for c in connectors]
        self.logical = [tuple(lm) for lm in logical]
        self.layout_mode = layout_mode
        self.serial = serial
        self.allowed = True
        self.global_scale_required = False
        self.supports_changing_layout = True
        self.bump_after_get = False     # simulate a concurrent change (serial only)
        self.plug_after_get = None      # connector to hot-plug after the next GetCurrentState
        self.move_after_get = False     # someone else re-lays out after the next GetCurrentState
        self.always_stale = False
        self.sticky_primary = False     # GNOME 50: old primary flag not cleared in place
        self.calls = []                 # every ApplyMonitorsConfig (serial, method, lms, props)
        self.persisted = False
        self._stale = {}                # (x, y) -> stale primary flag (sticky_primary)
        self.lock = threading.Lock()

    def resources_variant(self):
        """GetResources, only as far as the `primary` output property goes."""
        primary = next((lm[5][0][0] for lm in self.logical if lm[4]), None)
        outputs = []
        for i, (spec, _modes, _props) in enumerate(self.monitors):
            crtc = next((j for j, lm in enumerate(self.logical)
                         if any(c == spec[0] for c, _m in lm[5])), -1)
            outputs.append((i, i, crtc, [], spec[0], [], [],
                            {"primary": Variant("b", spec[0] == primary),
                             "vendor": Variant("s", spec[1])}))
        return (self.serial, [], outputs, [], 65535, 65535)

    # -- lookups

    def monitor(self, connector):
        for m in self.monitors:
            if m[0][0] == connector:
                return m
        return None

    def mode(self, connector, mid):
        mon = self.monitor(connector)
        if mon is None:
            return None
        for m in mon[1]:
            if m["id"] == mid:
                return m
        return None

    def current(self):
        out = {}
        for lm in self.logical:
            for connector, mid in lm[5]:
                out[connector] = mid
        return out

    # -- GetCurrentState

    def state_variant(self):
        cur = self.current()
        monitors = []
        for spec, modes, props in self.monitors:
            wire_modes = []
            for m in modes:
                mp = {}
                if cur.get(spec[0]) == m["id"]:
                    mp["is-current"] = Variant("b", True)
                if m["preferred"]:
                    mp["is-preferred"] = Variant("b", True)
                if m["interlaced"]:
                    mp["is-interlaced"] = Variant("b", True)
                wire_modes.append((m["id"], m["w"], m["h"], m["rate"], 1.0,
                                   m["scales"], mp))
            mprops = {"is-builtin": Variant("b", props["is-builtin"]),
                      "display-name": Variant("s", props["display-name"])}
            for k in ("width-mm", "height-mm"):  # absent on real 46/50 QEMU heads
                if k in props:
                    mprops[k] = Variant("i", props[k])
            if "is-underscanning" in props:
                mprops["is-underscanning"] = Variant("b", props["is-underscanning"])
            monitors.append((spec, wire_modes, mprops))
        logical = [(x, y, float(scale), tf, prim or (self.sticky_primary
                                                     and self._stale.get((x, y), False)),
                    [self.monitor(c)[0] for c, _mid in members], {})
                   for (x, y, scale, tf, prim, members) in self.logical]
        props = {"layout-mode": Variant("u", self.layout_mode)}
        if self.supports_changing_layout:
            props["supports-changing-layout-mode"] = Variant("b", True)
        if self.global_scale_required:
            props["global-scale-required"] = Variant("b", True)
        return (self.serial, monitors, logical, props)

    # -- ApplyMonitorsConfig

    def _size(self, connector, mid, scale, transform):
        m = self.mode(connector, mid)
        w, h = m["w"], m["h"]
        if transform in (1, 3, 5, 7):
            w, h = h, w
        if self.layout_mode == 1:
            return mutter.round_half_away(w / scale), mutter.round_half_away(h / scale)
        return w, h

    def apply(self, serial, method, lms, props) -> bool:
        """Returns True when the configuration was applied (signal due)."""
        inv = ERR + "InvalidArgs"
        if serial != self.serial or self.always_stale:
            raise MutterError(ERR + "AccessDenied",
                              "The requested configuration is based on stale information")
        if not self.allowed:
            raise MutterError(ERR + "AccessDenied",
                              "Monitor configuration via D-Bus is disabled")
        if "layout-mode" in props and not self.supports_changing_layout:
            raise MutterError(inv, "Can't set layout mode")
        rects = []
        for (x, y, scale, transform, primary, monitors) in lms:
            if not monitors:
                raise MutterError(inv, "Empty logical monitor")
            sizes = []
            for (connector, mid, mprops) in monitors:
                mon = self.monitor(connector)
                if mon is None:
                    raise MutterError(inv, "Invalid connector '%s' specified" % connector)
                m = self.mode(connector, mid)
                if m is None:
                    raise MutterError(inv, "Invalid mode '%s' specified" % mid)
                if mprops.get("underscanning") and "is-underscanning" not in mon[2]:
                    raise MutterError(inv, "Underscanning requested but unsupported")
                if not any(s == scale for s in m["scales"]):
                    raise MutterError(inv, "Scale %g not valid for resolution %dx%d"
                                      % (scale, m["w"], m["h"]))
                sizes.append((m["w"], m["h"]))
            if x < 0 or y < 0:
                raise MutterError(inv, "Invalid logical monitor position (%d, %d)" % (x, y))
            if any(s != sizes[0] for s in sizes):
                raise MutterError(inv, "Monitor modes in logical monitor conflict")
            w, h = self._size(monitors[0][0], monitors[0][1], scale, transform)
            rects.append((x, y, w, h, primary, scale))
        if not rects:
            raise MutterError(inv, "Monitors config incomplete")
        has_primary = False
        for i, r in enumerate(rects):
            if self.global_scale_required and r[5] != rects[0][5]:
                raise MutterError(inv, "Logical monitor scales must be identical")
            if any(_overlaps(r, o) for o in rects[:i]):
                raise MutterError(inv, "Logical monitors overlap")
            if r[4] and has_primary:
                raise MutterError(inv, "Config contains multiple primary logical monitors")
            has_primary = has_primary or r[4]
        if len(rects) > 1:
            for r in rects:
                if not any(_adjacent(r, o) for o in rects if o is not r):
                    raise MutterError(inv, "Logical monitors not adjacent")
        if min(r[0] for r in rects) != 0 or min(r[1] for r in rects) != 0:
            raise MutterError(inv, "Logical monitors positions are offset")
        if not has_primary:
            raise MutterError(inv, "Config is missing primary logical")
        if method == 0:
            return False
        old_primary = {(lm[0], lm[1]) for lm in self.logical if lm[4]}
        self._stale = {(x, y): (x, y) in old_primary for (x, y, *_r) in lms}
        self.logical = [(x, y, scale, transform, primary,
                         [(c, mid) for c, mid, _p in monitors])
                        for (x, y, scale, transform, primary, monitors) in lms]
        self.serial += 1
        self.persisted = method == 2
        return True


class _MutterConn(tdm._Conn):
    """The mock bus's per-client loop with org.gnome.Mutter.DisplayConfig
    served from the bus's FakeMutter (None = no GNOME on this bus)."""

    def _bus_method(self, m):
        svc = self.bus.mutter
        if m.member == "NameHasOwner" and m.args()[0] == DEST:
            self.send(Message.method_return(m, "b", (svc is not None,)))
        elif m.member == "ListNames":
            names = self.bus.list_names() + ([DEST] if svc is not None else [])
            self.send(Message.method_return(m, "as", (names,)))
        else:
            super()._bus_method(m)

    def _test_method(self, m):
        if m.destination != DEST:
            return super()._test_method(m)
        svc = self.bus.mutter
        if svc is None:
            self.send(Message.error(m, ERR + "ServiceUnknown",
                                    "The name %s was not provided by any .service files"
                                    % DEST))
        elif m.interface == dbus_mini.PROPS_IFACE and m.member == "Get":
            _iface, name = m.args()
            if name == "ApplyMonitorsConfigAllowed":
                self.send(Message.method_return(m, "v", (Variant("b", svc.allowed),)))
            else:
                self.send(Message.error(m, ERR + "InvalidArgs", "No such property"))
        elif m.member == "GetResources":
            with svc.lock:
                res = svc.resources_variant()
            self.send(Message.method_return(
                m, "ua(uxiiiiiuaua{sv})a(uxiausauaua{sv})a(uxuudu)ii", res))
        elif m.member == "GetCurrentState":
            with svc.lock:
                state = svc.state_variant()
                if svc.bump_after_get:
                    svc.serial += 1
                    svc.bump_after_get = False
                if svc.plug_after_get:
                    svc.monitors.append(MONITORS[svc.plug_after_get])
                    svc.serial += 1
                    svc.plug_after_get = None
                if svc.move_after_get:
                    x, y, s, t, p, mons = svc.logical[-1]
                    svc.logical[-1] = (x, y + 100, s, t, p, mons)
                    svc.serial += 1
                    svc.move_after_get = False
            self.send(Message.method_return(m, tdm.GET_CURRENT_STATE_SIG, state))
        elif m.member == "ApplyMonitorsConfig":
            if m.signature != tdm.APPLY_MONITORS_CONFIG_SIG:
                self.send(Message.error(m, ERR + "InvalidArgs", "bad signature"))
                return
            serial, method, lms, props = m.args()
            svc.calls.append((serial, method, lms, props))
            try:
                with svc.lock:
                    changed = svc.apply(serial, method, lms, props)
            except MutterError as e:
                self.send(Message.error(m, e.name, e.message))
                return
            if changed:  # mutter emits the signal before completing the call
                for c in self.bus.connections():
                    if any("MonitorsChanged" in r or "type='signal'" in r
                           for r in c.matches):
                        c.send(Message.signal(PATH, IFACE, "MonitorsChanged"))
            self.send(Message.method_return(m))
        else:
            self.send(Message.error(m, ERR + "UnknownMethod", "no %s" % m.member))


class MutterMockBus(tdm.MockBus):
    def __init__(self, **kw):
        self.mutter = None
        super().__init__(**kw)

    def _accept(self):
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            c = _MutterConn(self, s)
            with self.lock:
                self.conns.append(c)
            threading.Thread(target=c.serve, daemon=True).start()


# ---------------------------------------------------------------- fake wl_output server

def _wl_string(v):
    b = v.encode() + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)


def _wl_msg(obj, op, payload=b""):
    return struct.pack("<II", obj, ((8 + len(payload)) << 16) | op) + payload


class FakeWayland:
    """A wl_display that advertises one wl_output v4 per entry of `outputs`
    ({connector: (mm_w, mm_h, subpixel, make, model)}) and answers
    get_registry / bind / sync — enough for mutter.wl_output_info()."""

    def __init__(self, outputs, version=4):
        self.outputs = outputs
        self.version = version
        self.dir = tempfile.mkdtemp(prefix="wxrandr-wl-")
        self.path = os.path.join(self.dir, "wayland-9")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.srv.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf, registry, names = b"", None, list(self.outputs)
        try:
            while True:
                data = c.recv(65536)
                if not data:
                    return
                buf += data
                while len(buf) >= 8:
                    obj, sizeop = struct.unpack_from("<II", buf)
                    size, op = sizeop >> 16, sizeop & 0xFFFF
                    if len(buf) < size:
                        break
                    args, buf = buf[8:size], buf[size:]
                    if obj == 1 and op == 1:        # get_registry(new_id)
                        (registry,) = struct.unpack("<I", args)
                        for i, name in enumerate(names):
                            c.sendall(_wl_msg(registry, 0, struct.pack("<I", i + 1)
                                              + _wl_string("wl_output")
                                              + struct.pack("<I", self.version)))
                    elif obj == 1 and op == 0:      # sync(new_id)
                        (cb,) = struct.unpack("<I", args)
                        c.sendall(_wl_msg(cb, 0, struct.pack("<I", 0)))
                    elif obj == registry and op == 0:   # bind(name, iface, ver, id)
                        (gname,) = struct.unpack_from("<I", args)
                        (new_id,) = struct.unpack_from("<I", args, len(args) - 4)
                        connector = names[gname - 1]
                        mm_w, mm_h, sub, make, model = self.outputs[connector]
                        c.sendall(_wl_msg(new_id, 0, struct.pack("<iiiii", 0, 0, mm_w, mm_h, sub)
                                          + _wl_string(make) + _wl_string(model)
                                          + struct.pack("<i", 0)))
                        c.sendall(_wl_msg(new_id, 1, struct.pack("<Iiii", 1, 1920, 1080, 60000)))
                        c.sendall(_wl_msg(new_id, 4, _wl_string(connector)))
                        c.sendall(_wl_msg(new_id, 2))   # done
        except OSError:
            pass
        finally:
            c.close()


def one_monitor(layout_mode=1):
    return FakeMutter(["eDP-1"], [(0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020")])],
                      layout_mode)


def two_monitors(layout_mode=1):
    return FakeMutter(["eDP-1", "DP-1"],
                      [(0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020")]),
                       (1920, 0, 1.0, 0, False, [("DP-1", "2560x1600@59.972")])],
                      layout_mode)


def three_monitors(hdmi_on=True, layout_mode=1):
    logical = [(0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020")]),
               (1920, 0, 1.0, 0, False, [("DP-1", "2560x1600@59.972")])]
    if hdmi_on:
        logical.append((4480, 0, 1.0, 0, False, [("HDMI-1", "1280x1024@60.020")]))
    return FakeMutter(["eDP-1", "DP-1", "HDMI-1"], logical, layout_mode)


# ---------------------------------------------------------------- harness

class MutterCase(unittest.TestCase):
    """One mock bus per class; each test gets a fresh FakeMutter and state
    file, and runs the CLI with Session replaced by a Mutter session on the
    mock (backend selection is tested separately)."""

    @classmethod
    def setUpClass(cls):
        cls.mock = MutterMockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def fixture(self):
        return two_monitors()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-mutter-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.opened = []
        self.mock.mutter = self.fixture()

    def tearDown(self):
        for mo in self.opened:
            mo.close()
        self.mock.mutter = None

    @property
    def svc(self):
        return self.mock.mutter

    def outputs(self):
        mo = mutter.MutterOutputs(bus=Bus(self.mock.address), wl_socket=False)
        self.opened.append(mo)
        return mo

    def state(self):
        return State("mutter-test", path=self.state_path)

    def run_cli(self, *argv, env=None):
        tc = self

        def fake_init(sess):
            sess.backend = "mutter"
            sess.ipc = sess.wlr = None
            sess.mutter = tc.outputs()
            sess.persistent = os.environ.get("WXRANDR_PERSIST", "") not in ("", "0")
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

    def lms(self):
        """The mock's logical monitors as (x, y, scale, transform, primary,
        [(connector, mode_id)])."""
        return [(x, y, s, t, p, list(m)) for (x, y, s, t, p, m) in self.svc.logical]

    def applied(self):
        return [c for c in self.svc.calls if c[1] != 0]


# ---------------------------------------------------------------- helpers

class Helpers(unittest.TestCase):
    def test_round_half_away(self):
        for x, want in ((0.5, 1), (1.5, 2), (2.5, 3), (2.4, 2), (1066.6667, 1067),
                        (853.333, 853), (0.0, 0), (-0.5, -1), (-1.4, -1)):
            self.assertEqual(mutter.round_half_away(x), want, x)

    def test_logical_size_rounds_and_swaps(self):
        L = mutter.logical_size
        self.assertEqual(L(2560, 1600, "normal", 1.25), (2048, 1280))
        self.assertEqual(L(2560, 1600, "normal", 1.5), (1707, 1067))   # roundf
        self.assertEqual(L(2560, 1600, "270", 2.0), (800, 1280))
        self.assertEqual(L(2560, 1600, "flipped-90", 2.0), (800, 1280))
        self.assertEqual(L(1111, 666, "normal", 1.5), (741, 444))
        self.assertEqual(core.logical_size(1111, 666, "normal", 1.5), (740, 444))
        # layout-mode 2 (physical): scale is a UI factor, sizes stay in pixels
        self.assertEqual(L(2560, 1600, "normal", 2.0, mutter.LAYOUT_PHYSICAL), (2560, 1600))
        self.assertEqual(L(2560, 1600, "90", 1.5, mutter.LAYOUT_PHYSICAL), (1600, 2560))
        self.assertEqual(L(1280, 720, "normal", 0, 1), (1280, 720))

    def test_transform_mapping_matches_xwayland_under_mutter(self):
        # measured: what xrandr prints through Mutter's XWayland per transform
        measured = {0: "normal", 1: "left", 2: "inverted", 3: "right", 4: "normal X axis",
                    5: "left X axis", 6: "inverted X axis", 7: "right X axis"}
        for n, words in measured.items():
            rot, refl = core.RANDR_VIEW[mutter.from_transform(n)]
            self.assertEqual(rot + core.REFLECTION_SUFFIX.get(refl, ""), words, n)
            self.assertEqual(mutter.to_transform(mutter.from_transform(n)), n)
        # sway names: the 90/270 family is numbered the other way round
        table = {"normal": 0, "90": 3, "180": 2, "270": 1, "flipped": 4,
                 "flipped-90": 7, "flipped-180": 6, "flipped-270": 5}
        for name, n in table.items():
            self.assertEqual(mutter.to_transform(name), n, name)
            self.assertEqual(mutter.from_transform(n), name, n)
        # RandR words through core.sway_transform
        self.assertEqual(mutter.to_transform(core.sway_transform("right", "normal")), 3)
        self.assertEqual(mutter.to_transform(core.sway_transform("left", "normal")), 1)
        self.assertEqual(mutter.to_transform(core.sway_transform("inverted", "normal")), 2)
        self.assertEqual(mutter.to_transform(core.sway_transform("normal", "x")), 4)
        self.assertEqual(mutter.to_transform(core.sway_transform("left", "x")), 5)
        self.assertEqual(mutter.to_transform(core.sway_transform("normal", "xy")), 2)
        self.assertEqual(mutter.from_transform(99), "normal")
        self.assertEqual(sorted(mutter.MUTTER_FROM_SWAY.values()), list(range(8)))

    def test_snap_scale(self):
        self.assertEqual(mutter.snap_scale(1.3, S4), 1.25)
        self.assertEqual(mutter.snap_scale(1.375, S4), 1.25)  # tie -> smaller
        self.assertEqual(mutter.snap_scale(3.0, S4), 2.0)
        self.assertEqual(mutter.snap_scale(0.5, S4), 1.0)
        self.assertEqual(mutter.snap_scale(2.0, [1.0]), 1.0)
        self.assertEqual(mutter.snap_scale(1.5, []), 1.0)
        self.assertEqual(mutter.snap_scale(1.5, None), 1.0)

    def test_match_mode(self):
        modes = [Mode(w=1920, h=1080, refresh_mhz=60020, mode_id="a"),
                 Mode(w=1920, h=1080, refresh_mhz=48000, mode_id="b"),
                 Mode(w=1920, h=1080, refresh_mhz=60000, mode_id="c",
                      name="1920x1080i", flags=("interlace",)),
                 Mode(w=1280, h=720, refresh_mhz=59860, custom=True, name="fancy")]
        self.assertEqual(mutter.match_mode(modes, 1920, 1080).mode_id, "a")
        self.assertEqual(mutter.match_mode(modes, 1920, 1080, 50.0).mode_id, "b")
        self.assertEqual(mutter.match_mode(modes, 1920, 1080, 59.9).mode_id, "a")
        self.assertEqual(mutter.match_mode(modes, 1920, 1080, interlaced=True).mode_id, "c")
        self.assertIsNone(mutter.match_mode(modes, 1920, 1080, 30.0, tolerance=1.0))
        self.assertIsNone(mutter.match_mode(modes, 1280, 720))  # custom has no id
        self.assertIsNone(mutter.match_mode(modes, 800, 600))

    def test_mode_from_wire(self):
        m = mutter._mode_from_wire("1920x1080i@59.940", 1920, 1080, 59.94,
                                   {"is-interlaced": True, "is-preferred": True})
        self.assertEqual((m.display_name, m.refresh_mhz, m.flags, m.preferred, m.mode_id),
                         ("1920x1080i", 59940, ("interlace",), True, "1920x1080i@59.940"))
        m = mutter._mode_from_wire("1920x1080@60.000999450683594", 1920, 1080,
                                   60.000999450683594, {})
        self.assertEqual((m.display_name, m.refresh_mhz), ("1920x1080", 60001))
        self.assertEqual("%6.2f" % m.refresh_hz, " 60.00")

    def test_canon_ignores_order(self):
        a = [{"x": 0, "y": 0, "scale": 1, "transform": 0, "primary": True,
              "members": [("b", "m", False), ("a", "m", False)]},
             {"x": 5, "y": 0, "scale": 2, "transform": 1, "primary": False,
              "members": [("c", "n", True)]}]
        b = [a[1], dict(a[0], members=list(reversed(a[0]["members"])))]
        self.assertEqual(mutter._canon(a), mutter._canon(b))

    def test_keep_adjacent(self):
        def out(name, x, y, w, h, active=True):
            o = core.OutputState(name=name, active=active)
            o.x, o.y, o.w, o.h = x, y, w, h
            return o

        def tgt(o, stanza=None, enabled=True):
            return core.Target(output=o, stanza=stanza, enabled=enabled)
        # A | B | C in a row, D below A: B gets narrower, A gets shorter
        a, b, c, d = (out("A", 0, 0, 1920, 1080), out("B", 1920, 0, 2560, 1600),
                      out("C", 4480, 0, 1280, 1024), out("D", 0, 1080, 1920, 1080))
        targets = [tgt(a), tgt(b), tgt(c), tgt(d)]
        dims = {"A": (1920, 720), "B": (1600, 2560), "C": (1280, 1024), "D": (1920, 1080)}
        pos = {"A": (0, 0), "B": (1920, 0), "C": (4480, 0), "D": (0, 1080)}
        moves = mutter.keep_adjacent(targets, dims, pos)
        self.assertEqual(pos, {"A": (0, 0), "B": (1920, 0), "C": (3520, 0), "D": (0, 720)})
        self.assertEqual(sorted(moves), [("C", (3520, 0), "B"), ("D", (0, 720), "A")])
        # explicit positions are the user's: a positioned output stays put...
        pos = {"A": (0, 0), "B": (1920, 0), "C": (4480, 0), "D": (0, 1080)}
        targets[2].stanza = Stanza(name="C", pos=(4480, 0))
        self.assertEqual(mutter.keep_adjacent(targets, dims, pos), [("D", (0, 720), "A")])
        self.assertEqual(pos["C"], (4480, 0))
        # ...and nothing follows a positioned one (it may have left the row)
        targets[2].stanza = None
        targets[1].stanza = Stanza(name="B", relation=("below", "A"))
        pos = {"A": (0, 0), "B": (0, 720), "C": (4480, 0), "D": (0, 1080)}
        self.assertEqual(mutter.keep_adjacent(targets, dims, pos), [("D", (0, 720), "A")])
        self.assertEqual(pos["C"], (4480, 0))
        targets[1].stanza = None
        # a disabled neighbour pulls nothing (Mutter reports the hole)
        targets[1].enabled = False
        pos = {"A": (0, 0), "C": (4480, 0), "D": (0, 1080)}
        self.assertEqual(mutter.keep_adjacent(targets, dims, pos), [("D", (0, 720), "A")])
        self.assertEqual(pos["C"], (4480, 0))
        # a chain follows through: A narrower moves B, and C follows B (D,
        # which B also touched, is off here: B may not overlap it)
        targets[1].enabled = True
        targets[3].enabled = False
        dims = {"A": (1280, 720), "B": (2560, 1600), "C": (1280, 1024)}
        pos = {"A": (0, 0), "B": (1920, 0), "C": (4480, 0)}
        moves = mutter.keep_adjacent(targets, dims, pos)
        self.assertEqual(pos, {"A": (0, 0), "B": (1280, 0), "C": (3840, 0)})
        self.assertEqual(moves, [("B", (1280, 0), "A"), ("C", (3840, 0), "B")])
        # with D still 1920 wide under A, B stays: touch one, overlap none
        targets[3].enabled = True
        dims = {"A": (1280, 720), "B": (2560, 1600), "C": (1280, 1024), "D": (1920, 1080)}
        pos = {"A": (0, 0), "B": (1920, 0), "C": (4480, 0), "D": (0, 1080)}
        self.assertEqual(mutter.keep_adjacent(targets, dims, pos), [("D", (0, 720), "A")])
        self.assertEqual(pos["B"], (1920, 0))
        # a grown neighbour pushes; an unchanged layout moves nothing
        dims = {"A": (1920, 1080), "B": (2560, 1600), "C": (1280, 1024), "D": (1920, 1080)}
        pos = {"A": (0, 0), "B": (1920, 0), "C": (4480, 0), "D": (0, 1080)}
        self.assertEqual(mutter.keep_adjacent(targets, dims, pos), [])
        dims["A"] = (2560, 1080)
        self.assertEqual(mutter.keep_adjacent(targets, dims, pos),
                         [("B", (2560, 0), "A"), ("C", (5120, 0), "B")])

    def test_find_session_bus_anchors_on_wayland_socket(self):
        tmp = tempfile.mkdtemp(prefix="wxrandr-sess-")
        self.addCleanup(shutil.rmtree, tmp, True)
        other = os.path.join(tmp, "other-bus")
        for p in (os.path.join(tmp, "wayland-0"), os.path.join(tmp, "bus"), other):
            open(p, "w").close()
        keys = ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ["XDG_RUNTIME_DIR"] = tmp
            os.environ["WAYLAND_DISPLAY"] = "wayland-0"
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            self.assertEqual(wsession.find_session_bus(),
                             (os.getuid(), "unix:path=" + os.path.join(tmp, "bus")))
            # the env bus wins when it belongs to the same user
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + other
            self.assertEqual(wsession.find_session_bus(), (os.getuid(), "unix:path=" + other))
            # no bus in the wayland dir either: whatever find_user_bus says
            os.unlink(os.path.join(tmp, "bus"))
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            self.assertEqual(wsession.find_session_bus(), wsession.find_user_bus())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# ---------------------------------------------------------------- snapshot / query

class Query(MutterCase):
    def fixture(self):
        return three_monitors(hdmi_on=False)

    def test_snapshot_fields(self):
        mo = self.outputs()
        outs = mo.snapshot(self.state())
        self.assertEqual([o.name for o in outs], ["eDP-1", "DP-1", "HDMI-1"])
        e, d, h = outs
        self.assertEqual((e.active, e.x, e.y, e.w, e.h, e.scale, e.transform, e.primary),
                         (True, 0, 0, 1920, 1080, 1.0, "normal", True))
        self.assertEqual((e.make, e.model, e.serial, e.mm_w, e.mm_h, e.ident),
                         ("BOE", "0x0a1b", "0x00000000", 344, 194, 1))
        self.assertIs(e.current, e.modes[0])
        self.assertEqual(e.current.mode_id, "1920x1080@60.020")
        self.assertEqual([m.mode_id for m in e.modes],
                         ["1920x1080@60.020", "1920x1080@48.000", "1680x1050@59.954",
                          "1280x720@59.860"])
        self.assertEqual((d.x, d.y, d.w, d.h, d.primary), (1920, 0, 2560, 1600, False))
        self.assertEqual((h.active, h.x, h.w, h.current, h.primary), (False, 0, 0, None, False))
        self.assertEqual(h.modes[1].display_name, "1920x1080i")
        self.assertEqual(h.modes[1].flags, ("interlace",))
        self.assertFalse(any(o.virtual_modes for o in outs))
        self.assertEqual((mo.serial, mo.layout_mode, mo.primary), (42, 1, "eDP-1"))
        self.assertEqual(mo.scales[("DP-1", "2560x1600@59.972")], S4)
        self.assertEqual(mo.scales[("HDMI-1", "1280x1024@60.020")], [1.0])
        self.assertTrue(mo.underscan["HDMI-1"])
        self.assertFalse(mo.underscan["eDP-1"])

    def test_wl_output_enrichment(self):
        # Mutter sends no width-mm/height-mm on D-Bus for these monitors; the
        # wl_output geometry (what XWayland's RandR shows) fills them in and
        # the subpixel order, D-Bus mm win when present
        spec, modes, props = self.svc.monitors[1]
        self.svc.monitors[1] = (spec, modes, {k: v for k, v in props.items()
                                              if not k.endswith("-mm")})
        if True:
            wl = FakeWayland({"DP-1": (597, 336, 2, "Dell Inc.", "DELL U2723QE"),
                              "eDP-1": (999, 999, 1, "BOE", "0x0a1b")})
            try:
                self.assertEqual(mutter.wl_output_info(wl.path), {
                    "DP-1": {"name": "DP-1", "mm_w": 597, "mm_h": 336,
                             "subpixel": "horizontal rgb", "make": "Dell Inc.",
                             "model": "DELL U2723QE"},
                    "eDP-1": {"name": "eDP-1", "mm_w": 999, "mm_h": 999, "subpixel": "none",
                              "make": "BOE", "model": "0x0a1b"}})
                mo = mutter.MutterOutputs(bus=Bus(self.mock.address), wl_socket=wl.path)
                self.opened.append(mo)
                e, d, h = mo.snapshot(self.state())
                self.assertEqual((e.mm_w, e.mm_h, e.subpixel), (344, 194, "none"))  # D-Bus wins
                self.assertEqual((d.mm_w, d.mm_h, d.subpixel), (597, 336, "horizontal rgb"))
                self.assertEqual((h.mm_w, h.mm_h, h.subpixel), (376, 301, "unknown"))
                lines = core.render_query([e, d, h], self.state())
                self.assertIn("DP-1 connected 2560x1600+1920+0 (normal left inverted right "
                              "x axis y axis) 597mm x 336mm", lines)
            finally:
                wl.close()
            # older wl_output (no name event) and a missing socket degrade to {}
            wl = FakeWayland({"DP-1": (597, 336, 0, "", "")}, version=3)
            try:
                self.assertEqual(mutter.wl_output_info(wl.path), {})
            finally:
                wl.close()
            self.assertEqual(mutter.wl_output_info(os.path.join(self.tmp, "nope")), {})

    def test_real_primary_overrides_state_file(self):
        st = self.state()
        st.primary = "DP-1"
        self.outputs().snapshot(st)
        self.assertEqual(st.primary, "eDP-1")

    def test_query_bytes(self):
        code, out, err = self.run_cli()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, (
            "Screen 0: minimum 16 x 16, current 4480 x 1600, maximum 32767 x 32767\n"
            "eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis "
            "y axis) 344mm x 194mm\n"
            "   1920x1080     60.02*+  48.00  \n"
            "   1680x1050     59.95  \n"
            "   1280x720      59.86  \n"
            "DP-1 connected 2560x1600+1920+0 (normal left inverted right x axis y axis) "
            "597mm x 373mm\n"
            "   2560x1600     59.97*+\n"
            "   1920x1200     59.95  \n"
            "   1920x1080     60.00  \n"
            "HDMI-1 connected (normal left inverted right x axis y axis)\n"
            "   1280x1024     60.02 +\n"
            "   1920x1080i    60.00  \n"
            "   1024x768      60.00  \n"))

    def test_one_monitor_query(self):
        self.mock.mutter = one_monitor()
        code, out, _ = self.run_cli("-q")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[:2], [
            "Screen 0: minimum 16 x 16, current 1920 x 1080, maximum 32767 x 32767",
            "eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis "
            "y axis) 344mm x 194mm"])

    def test_rotated_scaled_header(self):
        self.svc.logical[1] = (1920, 0, 2.0, 1, False, [("DP-1", "2560x1600@59.972")])
        code, out, _ = self.run_cli()
        self.assertIn("DP-1 connected 800x1280+1920+0 left (normal left inverted right "
                      "x axis y axis) 597mm x 373mm\n", out)
        self.assertIn("current 2720 x 1280", out)

    def test_physical_layout_mode_sizes_in_pixels(self):
        self.svc.layout_mode = 2
        self.svc.logical[1] = (1920, 0, 2.0, 0, False, [("DP-1", "2560x1600@59.972")])
        code, out, _ = self.run_cli()
        self.assertIn("DP-1 connected 2560x1600+1920+0 (normal", out)
        self.assertIn("current 4480 x 1600", out)

    def test_mirror_query(self):
        self.svc.logical = [(0, 0, 1.0, 0, True,
                             [("eDP-1", "1920x1080@60.020"), ("DP-1", "1920x1080@60.000")])]
        code, out, _ = self.run_cli()
        lines = out.splitlines()
        self.assertEqual(lines[0], "Screen 0: minimum 16 x 16, current 1920 x 1080, "
                                   "maximum 32767 x 32767")
        self.assertTrue(lines[1].startswith("eDP-1 connected primary 1920x1080+0+0 "))
        self.assertIn("DP-1 connected 1920x1080+0+0 (normal", out)
        self.assertIn("   1920x1080     60.00* \n", out)

    def test_listmonitors_and_providers(self):
        code, out, _ = self.run_cli("--listmonitors")
        self.assertEqual(out, "Monitors: 2\n"
                              " 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1\n"
                              " 1: +DP-1 2560/597x1600/373+1920+0  DP-1\n")
        code, out, _ = self.run_cli("--listproviders")
        self.assertEqual(out.splitlines()[1],
                         "Provider 0: id: 0x1 cap: 0xb, Source Output, Sink Output, "
                         "Sink Offload crtcs: 3 outputs: 3 associated providers: 0 "
                         "name:mutter")

    def test_verbose_interlaced_flag_and_custom_modes_listed(self):
        code, out, err = self.run_cli("--newmode", "fancy", "74.5", "1280", "1344",
                                      "1472", "1664", "720", "723", "728", "748",
                                      "--addmode", "eDP-1", "fancy")
        self.assertEqual((code, out, err), (0, "", ""))
        code, out, _ = self.run_cli("--verbose")
        self.assertIn("  1920x1080i (0x", out)
        self.assertIn(" Interlace", out)
        self.assertIn("  fancy (0x", out)  # state-file modes still listed


# ---------------------------------------------------------------- apply

class Apply(MutterCase):
    def fixture(self):
        return three_monitors()

    def test_relation_chain_in_one_call(self):
        code, out, err = self.run_cli("--output", "DP-1", "--left-of", "eDP-1",
                                      "--output", "HDMI-1", "--below", "eDP-1")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.lms(), [
            (2560, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020")]),
            (0, 0, 1.0, 0, False, [("DP-1", "2560x1600@59.972")]),
            (2560, 1080, 1.0, 0, False, [("HDMI-1", "1280x1024@60.020")])])
        self.assertEqual(len(self.applied()), 1)   # one atomic call
        self.assertEqual(self.applied()[0][1], 1)  # temporary

    def test_above_and_rotate_left_swaps_logical_size(self):
        code, out, err = self.run_cli("--output", "eDP-1", "--above", "DP-1",
                                      "--output", "HDMI-1", "--rotate", "left",
                                      "--right-of", "DP-1")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.lms(), [
            (0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020")]),
            (0, 1080, 1.0, 0, False, [("DP-1", "2560x1600@59.972")]),
            (2560, 1080, 1.0, 1, False, [("HDMI-1", "1280x1024@60.020")])])
        code, out, _ = self.run_cli()
        self.assertIn("HDMI-1 connected 1024x1280+2560+1080 left (normal", out)
        self.assertIn("current 3584 x 2680", out)

    def test_rotation_and_reflection_numbers(self):
        self.mock.mutter = one_monitor()
        for rot, refl, want in (("right", "normal", 3), ("inverted", "normal", 2),
                                ("left", "normal", 1), ("normal", "x", 4),
                                ("right", "x", 7), ("normal", "y", 6), ("left", "x", 5),
                                ("inverted", "xy", 0), ("normal", "normal", 0)):
            code, _, err = self.run_cli("--output", "eDP-1", "--rotate", rot,
                                        "--reflect", refl)
            self.assertEqual((code, err), (0, ""), (rot, refl))
            self.assertEqual(self.lms()[0][3], want, (rot, refl))
        # the current transform carries over: reflect alone keeps the rotation
        self.run_cli("--output", "eDP-1", "--rotate", "right")
        self.run_cli("--output", "eDP-1", "--reflect", "x")
        self.assertEqual(self.lms()[0][3], 7)

    def test_scale_2_shrinks_logical_size(self):
        code, out, err = self.run_cli("--output", "DP-1", "--scale", "2",
                                      "--output", "HDMI-1", "--right-of", "DP-1")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.lms()[1], (1920, 0, 2.0, 0, False, [("DP-1", "2560x1600@59.972")]))
        self.assertEqual(self.lms()[2][:2], (3200, 0))
        code, out, _ = self.run_cli()
        self.assertIn("DP-1 connected 1280x800+1920+0 (normal", out)

    def test_scale_snaps_to_supported_with_warning(self):
        code, out, err = self.run_cli("--output", "DP-1", "--scale", "1.3",
                                      "--output", "HDMI-1", "--right-of", "DP-1")
        self.assertEqual(code, 0)
        self.assertEqual(err, "xrandr: scale 1.3 is not available for DP-1 at 2560x1600; "
                              "using 1.25\n")
        self.assertEqual(self.lms()[1][2], 1.25)
        self.assertEqual(self.lms()[2][0], 1920 + 2048)
        code, out, err = self.run_cli("--output", "HDMI-1", "--scale", "2")
        self.assertEqual(code, 0)
        self.assertEqual(err, "xrandr: scale 2 is not available for HDMI-1 at 1280x1024; "
                              "using 1\n")
        self.assertEqual(self.lms()[2][2], 1.0)

    def test_untouched_unsupported_scale_is_kept(self):
        # a scale Mutter runs now (say from monitors.xml) but does not list:
        # an untouched output keeps it verbatim, silently
        self.svc.logical[1] = (1920, 0, 1.75, 0, False, [("DP-1", "2560x1600@59.972")])
        mo = self.outputs()
        st = self.state()
        outs = mo.snapshot(st)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            plan = mo.plan(st, core.build_targets(outs, [Stanza(name="HDMI-1", off=True)], st))
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(plan[1]["scale"], 1.75)
        # but a mode change snaps it (that mode may support other scales)
        with contextlib.redirect_stderr(err):
            plan = mo.plan(st, core.build_targets(outs, [Stanza(name="DP-1", mode="1920x1200")],
                                                  st))
        self.assertEqual(plan[1]["scale"], 1.75 - 0.25)
        self.assertIn("scale 1.75 is not available for DP-1 at 1920x1200; using 1.5",
                      err.getvalue())

    def test_scale_in_physical_layout_mode(self):
        self.mock.mutter = three_monitors(layout_mode=2)
        code, out, err = self.run_cli("--output", "DP-1", "--scale", "2",
                                      "--output", "HDMI-1", "--right-of", "DP-1")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.lms()[1][2], 2.0)
        self.assertEqual(self.lms()[2][:2], (4480, 0))  # pixels, not 3200

    def test_same_as_becomes_one_logical_monitor(self):
        self.mock.mutter = two_monitors()
        code, out, err = self.run_cli("--output", "DP-1", "--mode", "1920x1080",
                                      "--same-as", "eDP-1")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.lms(), [(0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020"),
                                                            ("DP-1", "1920x1080@60.000")])])
        code, out, _ = self.run_cli("--listmonitors")
        self.assertEqual(out, "Monitors: 2\n"
                              " 0: +*eDP-1 1920/344x1080/194+0+0  eDP-1\n"
                              " 1: +DP-1 1920/597x1080/373+0+0  DP-1\n")
        # and back apart
        code, out, err = self.run_cli("--output", "DP-1", "--right-of", "eDP-1")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.lms()[1][:2], (1920, 0))

    def test_same_as_with_different_modes_is_fatal(self):
        self.mock.mutter = two_monitors()
        before = self.lms()
        code, out, err = self.run_cli("--output", "DP-1", "--same-as", "eDP-1")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xrandr: cannot mirror DP-1 onto eDP-1: Mutter needs the same "
                              "mode, rotation and scale (2560x1600 normal scale 1 vs "
                              "1920x1080 normal scale 1)\n")
        self.assertEqual(self.lms(), before)
        self.assertEqual(self.svc.calls, [])

    def test_off_in_the_middle_is_a_hole(self):
        before = self.lms()
        code, out, err = self.run_cli("--output", "DP-1", "--off")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: Logical monitors not adjacent\n")
        self.assertEqual(self.lms(), before)

    def test_rotate_in_the_middle_keeps_the_row_adjacent(self):
        # review F1: X leaves a gap right of the now-narrower DP-1; Mutter
        # would refuse it, so HDMI-1 (not positioned here) follows DP-1's edge
        code, out, err = self.run_cli("--output", "DP-1", "--rotate", "left")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "xrandr: output HDMI-1 moved to +3520+0 to stay adjacent "
                              "to DP-1\n")
        self.assertEqual(self.lms(), [
            (0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020")]),
            (1920, 0, 1.0, 1, False, [("DP-1", "2560x1600@59.972")]),
            (3520, 0, 1.0, 0, False, [("HDMI-1", "1280x1024@60.020")])])
        self.assertEqual(len(self.applied()), 1)
        # and back: the neighbour follows the edge growing again
        code, out, err = self.run_cli("--output", "DP-1", "--rotate", "normal")
        self.assertEqual((code, err), (0, "xrandr: output HDMI-1 moved to +4480+0 to "
                                          "stay adjacent to DP-1\n"))
        self.assertEqual([lm[:2] for lm in self.lms()], [(0, 0), (1920, 0), (4480, 0)])

    def test_smaller_mode_in_the_middle_and_a_chain(self):
        code, out, err = self.run_cli("--output", "DP-1", "--mode", "1920x1200")
        self.assertEqual((code, err), (0, "xrandr: output HDMI-1 moved to +3840+0 to "
                                          "stay adjacent to DP-1\n"))
        self.assertEqual([lm[:2] for lm in self.lms()], [(0, 0), (1920, 0), (3840, 0)])
        # the first output shrinks: both neighbours to its right move, in order
        code, out, err = self.run_cli("--output", "eDP-1", "--mode", "1280x720")
        self.assertEqual((code, err), (0, "xrandr: output DP-1 moved to +1280+0 to stay "
                                          "adjacent to eDP-1\nxrandr: output HDMI-1 moved "
                                          "to +3200+0 to stay adjacent to DP-1\n"))
        self.assertEqual([lm[:2] for lm in self.lms()], [(0, 0), (1280, 0), (3200, 0)])
        code, out, _ = self.run_cli()
        self.assertIn("current 4480 x 1200", out)

    def test_scale_change_in_the_middle(self):
        code, out, err = self.run_cli("--output", "DP-1", "--scale", "2x2")
        self.assertEqual((code, err), (0, "xrandr: output HDMI-1 moved to +3200+0 to "
                                          "stay adjacent to DP-1\n"))
        self.assertEqual(self.lms()[1][:3], (1920, 0, 2.0))
        self.assertEqual(self.lms()[2][:2], (3200, 0))

    def test_randr_1_0_size_and_orientation_in_a_row(self):
        # -s / -o act on the first output; its neighbours keep touching it
        code, out, err = self.run_cli("-s", "1280x720")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "xrandr: output DP-1 moved to +1280+0 to stay adjacent to "
                              "eDP-1\nxrandr: output HDMI-1 moved to +3840+0 to stay "
                              "adjacent to DP-1\n")
        self.assertEqual(self.lms()[0][5], [("eDP-1", "1280x720@59.860")])
        self.assertEqual([lm[:2] for lm in self.lms()], [(0, 0), (1280, 0), (3840, 0)])
        code, out, err = self.run_cli("-o", "left")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "xrandr: output DP-1 moved to +720+0 to stay adjacent to "
                              "eDP-1\nxrandr: output HDMI-1 moved to +3280+0 to stay "
                              "adjacent to DP-1\n")
        self.assertEqual(self.lms()[0][3], 1)
        self.assertEqual([lm[:2] for lm in self.lms()], [(0, 0), (720, 0), (3280, 0)])

    def test_below_follows_a_shorter_top_neighbour(self):
        self.mock.mutter = FakeMutter(
            ["eDP-1", "DP-1", "HDMI-1"],
            [(0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020")]),
             (1920, 0, 1.0, 0, False, [("DP-1", "2560x1600@59.972")]),
             (0, 1080, 1.0, 0, False, [("HDMI-1", "1280x1024@60.020")])])
        code, out, err = self.run_cli("--output", "eDP-1", "--mode", "1280x720")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "xrandr: output DP-1 moved to +1280+0 to stay adjacent to "
                              "eDP-1\nxrandr: output HDMI-1 moved to +0+720 to stay "
                              "adjacent to eDP-1\n")
        self.assertEqual([lm[:2] for lm in self.lms()], [(0, 0), (1280, 0), (0, 720)])

    def test_explicit_position_is_not_moved(self):
        before = self.lms()
        code, out, err = self.run_cli("--output", "DP-1", "--rotate", "left",
                                      "--output", "HDMI-1", "--pos", "4480x0")
        self.assertEqual((code, err), (1, "xrandr: Logical monitors not adjacent\n"))
        self.assertEqual(self.lms(), before)
        # ...and the documented workaround needs no help (no warning)
        code, out, err = self.run_cli("--output", "DP-1", "--rotate", "left",
                                      "--output", "HDMI-1", "--right-of", "DP-1")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.lms()[2][:2], (3520, 0))

    def test_dryrun_and_verbose_plan_show_the_followed_layout(self):
        before = self.lms()
        code, out, err = self.run_cli("--dryrun", "--output", "DP-1", "--rotate", "left")
        self.assertEqual(code, 0)
        self.assertIn("screen 0: 4800x2560", out)   # 3520 + 1280, not 4480 + 1280
        self.assertIn('crtc 2:    1280x1024  60.02 +3520+0 "HDMI-1"', out)
        self.assertEqual(err, "xrandr: output HDMI-1 moved to +3520+0 to stay adjacent to "
                              "DP-1\nmutter verify: ok\n")
        self.assertEqual(self.lms(), before)
        # --fb is checked against the followed layout too
        code, out, err = self.run_cli("--dryrun", "--fb", "4800x2560", "--output", "DP-1",
                                      "--rotate", "left")
        self.assertEqual(code, 0)
        self.assertNotIn("not large enough", err)

    def test_off_at_the_end_and_lastmode(self):
        code, out, err = self.run_cli("--output", "HDMI-1", "--off")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual([lm[5][0][0] for lm in self.lms()], ["eDP-1", "DP-1"])
        self.assertEqual(self.state().lastmodes()["HDMI-1"], [1280, 1024, 60020])
        code, out, _ = self.run_cli()
        self.assertIn("HDMI-1 connected (normal", out)
        # re-enabling with --auto lands right of the rightmost with a warning
        code, out, err = self.run_cli("--output", "HDMI-1", "--auto")
        self.assertEqual(code, 0)
        self.assertEqual(err, "xrandr: output HDMI-1 enabled without a position; placing "
                              "it right-of DP-1\n")
        self.assertEqual(self.lms()[2], (4480, 0, 1.0, 0, False,
                                         [("HDMI-1", "1280x1024@60.020")]))

    def test_global_auto_places_and_warns(self):
        self.mock.mutter = three_monitors(hdmi_on=False)
        code, out, err = self.run_cli("--auto")
        self.assertEqual(code, 0)
        self.assertIn("placing it right-of DP-1", err)
        self.assertEqual(self.lms()[2][:2], (4480, 0))

    def test_off_everything(self):
        code, out, err = self.run_cli("--output", "eDP-1", "--off", "--output", "DP-1",
                                      "--off", "--output", "HDMI-1", "--off")
        self.assertEqual((code, err), (1, "xrandr: Monitors config incomplete\n"))

    def test_pos_overlap_is_mutters_error(self):
        code, out, err = self.run_cli("--output", "DP-1", "--pos", "100x0")
        self.assertEqual((code, err), (1, "xrandr: Logical monitors overlap\n"))

    def test_apply_disabled_by_policy(self):
        self.svc.allowed = False
        code, out, err = self.run_cli("--output", "HDMI-1", "--off")
        self.assertEqual((code, err), (1, "xrandr: Monitor configuration via D-Bus is "
                                          "disabled\n"))

    def test_primary_moves_and_query_shows_it(self):
        code, out, err = self.run_cli("--output", "DP-1", "--primary")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual([lm[4] for lm in self.lms()], [False, True, False])
        code, out, _ = self.run_cli()
        self.assertIn("DP-1 connected primary 2560x1600+1920+0", out)
        self.assertIn("eDP-1 connected 1920x1080+0+0", out)
        # XWayland's RandR lists the primary monitor first (verified against
        # real xrandr on GNOME 50), so --listmonitors does too on Mutter
        code, out, _ = self.run_cli("--listmonitors")
        self.assertEqual(out, "Monitors: 3\n"
                              " 0: +*DP-1 2560/597x1600/373+1920+0  DP-1\n"
                              " 1: +eDP-1 1920/344x1080/194+0+0  eDP-1\n"
                              " 2: +HDMI-1 1280/376x1024/301+4480+0  HDMI-1\n")
        code, out, _ = self.run_cli()
        self.assertEqual([l.split()[0] for l in out.splitlines() if "connected" in l],
                         ["eDP-1", "DP-1", "HDMI-1"])  # the -q output order stays

    def test_sticky_primary_flags_resolved_via_get_resources(self):
        # GNOME 50: the old logical monitor keeps primary=true after a
        # temporary re-primary; GetResources knows the real one
        self.svc.sticky_primary = True
        code, out, err = self.run_cli("--output", "DP-1", "--primary")
        self.assertEqual((code, out, err), (0, "", ""))
        state = self.svc.state_variant()
        self.assertEqual([lm[4] for lm in state[2]], [True, True, False])  # the quirk
        mo = self.outputs()
        outs = mo.snapshot(self.state())
        self.assertEqual(mo.primary, "DP-1")
        self.assertEqual([o.primary for o in outs], [False, True, False])
        code, out, _ = self.run_cli()
        self.assertIn("DP-1 connected primary 2560x1600+1920+0", out)
        self.assertIn("eDP-1 connected 1920x1080+0+0 (normal", out)
        code, out, _ = self.run_cli("--listmonitors")
        self.assertTrue(out.splitlines()[1].startswith(" 0: +*DP-1 "))
        # change detection uses the resolved primary: nothing to re-apply
        n = len(self.svc.calls)
        code, out, err = self.run_cli("--output", "DP-1", "--primary")
        self.assertEqual((code, err, len(self.svc.calls)), (0, "", n))
        # and moving the primary back is a real change
        code, out, err = self.run_cli("--output", "eDP-1", "--primary")
        self.assertEqual((code, err, len(self.svc.calls)), (0, "", n + 1))
        outs = self.outputs().snapshot(self.state())
        self.assertEqual([o.primary for o in outs], [True, False, False])

    def test_unchanged_layout_is_not_reapplied(self):
        code, out, err = self.run_cli("--output", "eDP-1", "--primary")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.svc.calls, [])
        code, out, err = self.run_cli("--output", "NOPE", "--off")
        self.assertEqual((code, err), (0, "warning: output NOPE not found; ignoring\n"))
        self.assertEqual(self.svc.calls, [])

    def test_noprimary_warns_and_keeps_primary(self):
        code, out, err = self.run_cli("--noprimary")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "xrandr: GNOME requires a primary output; keeping eDP-1\n")
        self.assertEqual([lm[4] for lm in self.lms()], [True, False, False])

    def test_method_selection(self):
        code, _, err = self.run_cli("--output", "HDMI-1", "--off")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.applied()[-1][1], 1)
        self.assertFalse(self.svc.persisted)
        code, _, err = self.run_cli("--persistent", "--output", "HDMI-1", "--auto")
        self.assertEqual(code, 0)
        self.assertIn('xrandr: GNOME will ask "Keep changes?" for 20 s', err)
        self.assertEqual(self.applied()[-1][1], 2)
        self.assertTrue(self.svc.persisted)
        code, _, err = self.run_cli("--output", "HDMI-1", "--off", env={"WXRANDR_PERSIST": "1"})
        self.assertEqual(code, 0)
        self.assertEqual(self.applied()[-1][1], 2)
        # --persistent re-applies an unchanged layout so monitors.xml gets it
        n = len(self.applied())
        code, _, err = self.run_cli("--persistent", "--output", "eDP-1", "--primary")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.applied()), n + 1)
        self.assertEqual(self.applied()[-1][1], 2)

    def test_dryrun_verifies_with_method_0(self):
        before = self.lms()
        code, out, err = self.run_cli("--dryrun", "--output", "HDMI-1", "--off")
        # the verdict is stderr: stdout stays xrandr's own dryrun lines
        self.assertEqual((code, err), (0, "mutter verify: ok\n"))
        self.assertIn("crtc 2: disable\n", out)
        self.assertNotIn("mutter verify", out)
        self.assertEqual([c[1] for c in self.svc.calls], [0])
        self.assertEqual(self.lms(), before)
        self.assertEqual(self.svc.serial, 42)

    def test_dryrun_reports_mutters_rejection(self):
        code, out, err = self.run_cli("--dryrun", "--output", "DP-1", "--off")
        self.assertEqual((code, err), (1, "xrandr: Logical monitors not adjacent\n"))
        self.assertNotIn("mutter verify", out)
        self.assertEqual([c[1] for c in self.svc.calls], [0])

    def test_dryrun_plan_uses_mutter_dims(self):
        code, out, err = self.run_cli("--dryrun", "--output", "DP-1", "--scale", "1.5",
                                      "--output", "HDMI-1", "--right-of", "DP-1")
        self.assertEqual((code, err), (0, "mutter verify: ok\n"))
        # 2560/1.5 = 1706.67 -> 1707 (roundf), wlroots would say 1706
        self.assertIn('"HDMI-1"', out)
        self.assertIn("+%d+0" % (1920 + 1707), out)
        self.assertIn("screen 0: %dx%d" % (1920 + 1707 + 1280, 1080), out)

    def test_stale_serial_is_retried_once(self):
        self.svc.bump_after_get = True  # someone changes the layout right after we read
        code, out, err = self.run_cli("--output", "HDMI-1", "--off")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual([c[0] for c in self.svc.calls], [42, 43])
        self.assertEqual([lm[5][0][0] for lm in self.lms()], ["eDP-1", "DP-1"])

    def test_stale_serial_after_a_hotplug_is_not_retried(self):
        # review F2: the plan was built without the new monitor; re-sending it
        # would silently leave that monitor disabled
        self.mock.mutter = two_monitors()
        self.svc.plug_after_get = "HDMI-1"
        code, out, err = self.run_cli("--output", "DP-1", "--scale", "2")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: output configuration cancelled by a concurrent "
                              "change; try again\n")
        self.assertEqual(len(self.svc.calls), 1)
        self.assertEqual(self.lms()[1][2], 1.0)
        # the next invocation sees the plugged monitor and works
        code, out, err = self.run_cli("--output", "DP-1", "--scale", "2")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.lms()[1][2], 2.0)

    def test_stale_serial_after_someone_elses_layout_is_not_retried(self):
        self.mock.mutter = two_monitors()
        self.svc.move_after_get = True
        code, out, err = self.run_cli("--output", "DP-1", "--scale", "2")
        self.assertEqual((code, err), (1, "xrandr: output configuration cancelled by a "
                                          "concurrent change; try again\n"))
        self.assertEqual(len(self.svc.calls), 1)

    def test_stale_serial_twice_is_fatal(self):
        self.svc.always_stale = True
        code, out, err = self.run_cli("--output", "HDMI-1", "--off")
        self.assertEqual((code, err), (1, "xrandr: output configuration cancelled by a "
                                          "concurrent change; try again\n"))
        self.assertEqual(len(self.svc.calls), 2)

    def test_monitors_changed_then_fresh_snapshot(self):
        mo = self.outputs()
        st = self.state()
        outs = mo.snapshot(st)
        targets = core.build_targets(outs, [Stanza(name="DP-1", relation=("below", "eDP-1")),
                                            Stanza(name="HDMI-1", off=True)], st)
        fresh = mo.apply(st, targets)
        self.assertEqual(mo.serial, 43)
        self.assertEqual([(o.name, o.active, o.x, o.y) for o in fresh],
                         [("eDP-1", True, 0, 0), ("DP-1", True, 0, 1080),
                          ("HDMI-1", False, 0, 0)])
        # MonitorsChanged was consumed by apply (nothing left to wait for)
        self.assertIsNone(mo.bus.wait_signal(IFACE, "MonitorsChanged", 0))

    def test_mode_rate_and_interlaced(self):
        code, out, err = self.run_cli("--output", "eDP-1", "--mode", "1920x1080",
                                      "--rate", "48")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.lms()[0][5], [("eDP-1", "1920x1080@48.000")])
        code, out, err = self.run_cli("--output", "HDMI-1", "--mode", "1920x1080i")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.lms()[2][5], [("HDMI-1", "1920x1080i@60.000")])
        code, out, _ = self.run_cli()
        self.assertIn("   1920x1080i    60.00* \n", out)
        code, out, err = self.run_cli("--output", "HDMI-1", "--mode", "640x480")
        self.assertEqual((code, err), (1, "xrandr: cannot find mode 640x480\n"))

    def test_custom_modes_need_a_real_twin(self):
        self.mock.mutter = one_monitor()
        self.run_cli("--newmode", "fancy", "74.5", "1280", "1344", "1472", "1664",
                     "720", "723", "728", "748", "--addmode", "eDP-1", "fancy")
        self.run_cli("--newmode", "weird", "50.0", "1000", "1010", "1020", "1100",
                     "500", "503", "508", "520", "--addmode", "eDP-1", "weird")
        code, out, err = self.run_cli("--output", "eDP-1", "--mode", "fancy")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.lms()[0][5], [("eDP-1", "1280x720@59.860")])
        code, out, err = self.run_cli("--output", "eDP-1", "--mode", "weird")
        self.assertEqual((code, err), (1, "xrandr: cannot find mode weird\n"))

    def test_underscanning_is_preserved(self):
        code, _, err = self.run_cli("--output", "HDMI-1", "--right-of", "eDP-1",
                                    "--output", "DP-1", "--off")
        self.assertEqual((code, err), (0, ""))
        _serial, _method, lms, _props = self.applied()[-1]
        members = {c: p for lm in lms for (c, _mid, p) in lm[5]}
        self.assertEqual(members["HDMI-1"], {"underscanning": True})
        self.assertEqual(members["eDP-1"], {})

    def test_brightness_and_gamma_warn_and_succeed(self):
        code, out, err = self.run_cli("--output", "eDP-1", "--brightness", "0.5",
                                      "--output", "DP-1", "--gamma", "1.1:1:0.9")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(err, "xrandr: --brightness/--gamma are not supported on Mutter "
                              "(no gamma LUT API); ignoring for eDP-1\n"
                              "xrandr: --brightness/--gamma are not supported on Mutter "
                              "(no gamma LUT API); ignoring for DP-1\n")
        self.assertEqual(self.state().gamma(), {})

    def test_wire_shape_of_apply(self):
        code, _, err = self.run_cli("--output", "HDMI-1", "--off")
        self.assertEqual((code, err), (0, ""))
        serial, method, lms, props = self.applied()[-1]
        self.assertEqual((serial, method, props), (42, 1, {}))
        self.assertEqual(lms, [(0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020", {})]),
                               (1920, 0, 1.0, 0, False, [("DP-1", "2560x1600@59.972", {})])])


# ---------------------------------------------------------------- backend selection

class Detection(MutterCase):
    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.get(k) for k in ("WXRANDR_BACKEND", "XDG_RUNTIME_DIR",
                                                     "WXRANDR_PERSIST", "WAYLAND_DISPLAY")}
        os.environ["XDG_RUNTIME_DIR"] = self.tmp  # state file lands in the temp dir
        os.environ.pop("WXRANDR_BACKEND", None)
        os.environ.pop("WXRANDR_PERSIST", None)
        self.patched = [(wsession, "find_sway_socket", wsession.find_sway_socket),
                        (wsession, "find_wayland_socket", wsession.find_wayland_socket),
                        (wsession, "find_session_bus", wsession.find_session_bus),
                        (mutter, "probe", mutter.probe)]
        wsession.find_sway_socket = lambda: None
        wsession.find_wayland_socket = lambda: None
        mock = self.mock
        wsession.find_session_bus = lambda: (os.getuid(), mock.address)
        self.orig_probe = mutter.probe
        mutter.probe = lambda addr=None: self.orig_probe(mock.address)
        os.environ.pop("WAYLAND_DISPLAY", None)
        # the real Session, with its bus closed by tearDown
        self.orig_init = cli.Session.__init__
        opened = self.opened

        def recording_init(sess):
            self.orig_init(sess)
            if sess.mutter is not None:
                opened.append(sess.mutter)
        cli.Session.__init__ = recording_init

    def main(self, *argv):
        """cli.main with the SystemExit of "Can't open display" caught."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue()

    def tearDown(self):
        for mod, name, orig in self.patched:
            setattr(mod, name, orig)
        cli.Session.__init__ = self.orig_init
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_probe(self):
        bus = mutter.probe()
        self.assertIsNotNone(bus)
        bus.close()
        self.mock.mutter = None
        self.assertIsNone(mutter.probe())
        self.assertIsNone(self.orig_probe("unix:path=/nonexistent/bus"))

    def test_auto_detect_picks_mutter(self):
        sess = cli.Session()
        self.assertEqual((sess.backend, sess.compositor_name), ("mutter", "mutter"))
        self.assertIsNone(sess.ipc)
        self.assertIsNone(sess.wlr)
        self.assertFalse(sess.persistent)
        self.assertEqual([o.name for o in sess.snapshot()], ["eDP-1", "DP-1"])

    def test_forced_and_alias(self):
        for val in ("mutter", "gnome"):
            os.environ["WXRANDR_BACKEND"] = val
            sess = cli.Session()
            self.assertEqual(sess.backend, "mutter", val)
        os.environ["WXRANDR_PERSIST"] = "1"
        sess = cli.Session()
        self.assertTrue(sess.persistent)

    def test_forced_without_gnome_is_one_line(self):
        self.mock.mutter = None
        os.environ["WXRANDR_BACKEND"] = "mutter"
        code, out, err = self.main("--listproviders")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: org.gnome.Mutter.DisplayConfig is not on "
                              "the session bus (not a GNOME session?)\n")
        wsession.find_session_bus = lambda: None
        code, out, err = self.main("--listproviders")
        self.assertEqual((code, err), (1, "Can't open display \n"))

    def test_auto_detect_falls_back_to_wlr_without_gnome(self):
        self.mock.mutter = None
        orig = core.WlrOutputs

        def no_wlr():
            raise core.Fatal("no wlr\n")
        core.WlrOutputs = no_wlr
        try:
            code, out, err = self.main("--listproviders")
            self.assertEqual((code, err), (1, "Can't open display \n"))
        finally:
            core.WlrOutputs = orig

    def test_end_to_end_through_real_session(self):
        code, out, err = self.main("--output", "DP-1", "--left-of", "eDP-1")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.lms()[1][:2], (0, 0))
        self.assertEqual(self.lms()[0][:2], (2560, 0))
        code, out, err = self.main("--listproviders")
        self.assertEqual(code, 0)
        self.assertIn("name:mutter", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
