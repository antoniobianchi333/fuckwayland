"""wxrandr KDE backend: kde_output_device_v2 + kde_output_management_v2 over
wdotool.wayland_mini.

KWin has no zwlr_output_management and no D-Bus display API (org.kde.KWin
exposes activeOutputName() and nothing else; Plasma 5.27's org.kde.KScreen can
only replay the Meta+P presets). The one usable mechanism is the Wayland
protocol pair from plasma-wayland-protocols -- NOT from the kwin repo --
`kde_output_device_v2` (read) and `kde_output_management_v2` (write). It is
what kscreen-doctor and the System Settings KCM drive through libkscreen, and
it is unauthenticated: no portal, no polkit, no security-context check, so any
client holding the Wayland socket can use it (KWinDisplay::allowInterface
blacklists plasma_window_management, fake_input, screencast, ... and never
kde_output_*).

- Discovery has two shapes. Through Plasma 6.6 every output is a
  `kde_output_device_v2` wl_registry global (find them all -- find_global()
  returns only the first). From Plasma 6.7 / device v21 KWin stopped exporting
  that global (wayland_server.cpp constructs only OutputDeviceRegistryV2 and
  hands resources out with wl_resource_create) and the devices arrive as
  new_ids on `kde_output_device_registry_v2`, which must be bound at v21 or
  higher. Both paths are implemented; the device object behaves identically
  either way.
- Versions move fast: 5.27 advertises device 2 / management 3, 6.0 6/7, 6.3
  11/12, 6.4-6.5 16/16, 6.6 20/19, master 25/22. We bind LOW -- device 2 (the
  `name` event, i.e. the connector name, is since 2) and management
  min(advertised, 12) (`set_primary_output` is since 2, `failure_reason` since
  12) -- because a server only sends events whose `since` is <= the bound
  version, so binding low is the compatible choice and every field xrandr
  needs exists at device 2. Optional features are gated on the *bound*
  version, never assumed: on 5.27 a rejection carries no reason string at all.
- Modes are objects, like wlroots: `mode` is a server-allocated new_id whose
  own events carry size (hardware pixels, pre-transform, pre-scale) and
  refresh (mHz). We read the id off the wire and register a handler for it,
  and remember the object per Mode so apply can name it. There is no
  width/height/refresh form of the request.
- `done` is the atomic publish barrier: a device's fields are only visible to
  the snapshot once its `done` arrives.
- Coordinates are LOGICAL and the position is NOT scaled (output.cpp:637:
  `RectF(logicalPosition, transform.map(QSizeF(modeSize)) / scale)`, rounded
  only in geometry()). So the logical size is the transform-swapped mode size
  divided by the scale, kept as a float and rounded half away from zero at the
  very end -- exactly Mutter's layout-mode-1 rule, whose tested helper we
  reuse, and NOT core.logical_size's wlroots truncation.
- Scale is quantised server-side to 1/120 (`std::round(s * 120) / 120`) and a
  scale <= 0 is dropped silently. We quantise on both sides so the round trip
  is stable, and we marshal the wl_fixed ourselves as a plain int: wayland_mini
  `_marshal`'s "f" truncates, and it is shared with the wlroots path where
  changing it would change live sway behaviour (1.3 -> 1.296875).
- Apply is atomic and STRICTLY one-shot: a second `apply` on the same
  configuration object is a fatal `already_applied` protocol error that kills
  the connection, so every attempt builds a fresh configuration. A hotplug
  between create_configuration and apply silently invalidates the object (all
  setters become no-ops and apply answers "One of the relevant outputs is no
  longer available"); that one message is retried once from a fresh snapshot.
- Only deltas are sent. libkscreen compares every field first and skips the
  call entirely when nothing differs, and so do we: a no-op changeset still
  goes through Workspace::applyOutputConfiguration and still causes a modeset.
- KWin validates at apply: every ENABLED output needs x >= 0 and y >= 0
  ("Position of enabled output %1 is negative"), you may not disable every
  output, and an output may not mirror itself. The XML's "no gaps or overlaps"
  sentence is not enforced by the code, so xrandr's gaps and `--same-as`
  overlaps survive. We normalise the layout to the origin before sending and
  refuse the last-output disable client-side, with our own xrandr-shaped
  message.
- There is no temporary mode and no confirmation dialog. KWin persists every
  applied configuration itself (Plasma 6: OutputConfigurationStore::storeConfig
  -> ~/.config/kwinoutputconfig.json, marked Source::User; 5.27: kded5 kscreen
  watches the same libkscreen ConfigMonitor and writes
  ~/.local/share/kscreen/<hash>). The 15-second "Keep these settings?"
  countdown lives in the System Settings KCM, not in KWin. So --persistent is
  the only mode there is: we say so once per apply and print the command that
  restores the pre-apply snapshot.

Mapping to the wxrandr model (core.OutputState / core.Target):
    output name          `name` (device v2), else `uuid`
    x, y                 `geometry` x/y == request `position` (logical)
    w, h                 derived: transform-swap(mode) / scale, round-half-away
    make/model/serial    `geometry` make/model + `serial_number`
    mm, subpixel, EDID   `geometry` mm + subpixel, `edid` (base64)
    --primary            set_primary_output (mgmt v2); not readable back at
                         device v2, so the state file remembers it
    --same-as            the same position (set_replication_source is a
                         different, mgmt-v13 concept and is not used)
    --brightness/--gamma no LUT here: zwlr_gamma_control_manager_v1 is probed
                         and, when absent as under KWin, warn + succeed
    --newmode/--addmode  applying one needs a real mode of the same size (and
                         rate) -> `cannot find mode`, as on Mutter; custom
                         modes proper need management v18 + capability 0x2000
"""

import struct
import time

from wdotool import session as wsession
from wxrandr import core
from wxrandr.core import Fatal, Mode, OutputState, warn
# KWin's logical-size rule is Mutter's layout-mode-1 rule (transform swap,
# then C roundf of px/scale); reuse the tested helpers instead of a copy.
from wxrandr.mutter import logical_size, round_half_away  # noqa: F401

DEV = "kde_output_device_v2"
REG = "kde_output_device_registry_v2"
MGMT = "kde_output_management_v2"
GAMMA = "zwlr_gamma_control_manager_v1"

# Bind low: every field xrandr needs exists at device 2, and a server only
# sends events whose `since` is <= the bound version.
DEV_WANT = 2         # `name` (the connector) is since 2
MGMT_WANT = 12       # set_primary_output since 2, failure_reason since 12
REG_WANT = 25
REG_MIN = 21         # binding the registry lower is error_unsupported_version

PRIMARY_MGMT = 2     # set_primary_output
REASON_MGMT = 12     # failure_reason
CUSTOM_MODES_MGMT = 18   # create_mode_list / set_custom_modes
CAP_CUSTOM_MODES = 0x2000

# Wayland reserves 0xff000000.. for server-allocated object ids.
SERVER_ID_BASE = 0xFF000000

SCALE_STEPS = 120    # kde_output_configuration_v2_scale: round(s * 120) / 120
APPLY_TIMEOUT = 10.0
SETTLE_ROUNDS = 4

# kde_output_configuration_v2 requests we use (opcodes are stable 5.27..master)
REQ_ENABLE, REQ_MODE, REQ_TRANSFORM = 0, 1, 2
REQ_POSITION, REQ_SCALE, REQ_APPLY, REQ_DESTROY = 3, 4, 5, 6
REQ_SET_PRIMARY = 10

INVALID_HINT = "no longer available"
SAVE_WARNING = ("KWin applies and saves this layout immediately: there is no "
                "temporary mode and no confirmation dialog\n")

SUBPIXEL = {0: "unknown", 1: "none", 2: "horizontal rgb", 3: "horizontal bgr",
            4: "vertical rgb", 5: "vertical bgr"}


# -- pure helpers (unit-tested) ----------------------------------------------

def quantize_scale(scale: float) -> float:
    """KWin's own quantisation: std::round(scale * 120) / 120, matching
    fractional-scale-v1's 120ths. Applied to what we send AND to what we read
    back, so `--scale 1.3` compares equal to the 1.3 KWin stores (the wl_fixed
    round trip alone would give 1.30078125 and every run would look like a
    change)."""
    return round(scale * SCALE_STEPS) / SCALE_STEPS


def to_fixed(value: float) -> int:
    """wl_fixed_from_double, rounding. Sent as a plain int arg: wayland_mini's
    "f" marshaller truncates and is shared with the wlroots path, where
    rounding would change live sway scales -- so it stays untouched."""
    return int(round(value * 256.0))


def from_fixed(raw: float) -> float:
    """A wl_fixed scale as KWin means it: 1/120-quantised."""
    return quantize_scale(raw)


# libkscreen's toKScreenRotation (waylandoutputdevice.cpp) reads the wl_output
# transform enum the way the spec's counter-clockwise 90 implies: 1 -> xrandr
# `left`, 3 -> `right`, 4..7 the same rotations with a reflection. sway's
# verified table (core.RANDR_VIEW) numbers "90" the other way round, so the
# sway transform *names* the renderer speaks differ from the wl numbers by a
# 90<->270 swap (1<->3, 5<->7) -- the same permutation the Mutter backend needs.
KWIN_RANDR_VIEW = {0: ("normal", "normal"), 1: ("left", "normal"),
                   2: ("inverted", "normal"), 3: ("right", "normal"),
                   4: ("normal", "x"), 5: ("left", "x"),
                   6: ("inverted", "x"), 7: ("right", "x")}
SWAY_FROM_KWIN = {n: next(tf for tf, v in core.RANDR_VIEW.items() if v == view)
                  for n, view in KWIN_RANDR_VIEW.items()}
KWIN_FROM_SWAY = {tf: n for n, tf in SWAY_FROM_KWIN.items()}


def to_transform(sway_tf: str) -> int:
    """sway transform name (what core/RANDR_VIEW use) -> wl_output enum."""
    return KWIN_FROM_SWAY.get(sway_tf, 0)


def from_transform(n: int) -> str:
    return SWAY_FROM_KWIN.get(n, "normal")


def normalise(pos: dict) -> dict:
    """Shift a layout so no enabled output sits at a negative coordinate --
    KWin answers `failed` with "Position of enabled output %1 is negative"
    otherwise. core.resolve_positions already anchors at 0,0; this is the
    guard that makes the property hold for every path into apply()."""
    if not pos:
        return pos
    min_x = min(x for x, _y in pos.values())
    min_y = min(y for _x, y in pos.values())
    dx = -min_x if min_x < 0 else 0
    dy = -min_y if min_y < 0 else 0
    if not (dx or dy):
        return pos
    return {n: (x + dx, y + dy) for n, (x, y) in pos.items()}


def match_mode(modes, w: int, h: int, rate_hz: float | None = None,
               tolerance: float | None = None) -> Mode | None:
    """The real (object-bearing) mode of size w x h: nearest refresh when a
    rate is given (within `tolerance` Hz if set), else the first listed."""
    cands = [m for m in modes if m.mode_id and (m.w, m.h) == (w, h)]
    if not cands:
        return None
    if rate_hz:
        best = min(cands, key=lambda m: abs(m.refresh_hz - rate_hz))
        if tolerance is not None and abs(best.refresh_hz - rate_hz) > tolerance:
            return None
        return best
    return cands[0]


def restore_command(outputs) -> str:
    """The xrandr invocation that puts `outputs` back the way they were --
    KWin has already saved the new layout by the time we could offer an undo,
    so the pre-apply snapshot is printed as a command the user can paste."""
    parts = []
    for o in outputs:
        parts += ["--output", o.name]
        if not o.active:
            parts.append("--off")
            continue
        if o.current is not None:
            parts += ["--mode", "%dx%d" % (o.current.w, o.current.h)]
            if o.current.refresh_mhz:
                parts += ["--rate", "%.2f" % o.current.refresh_hz]
        parts += ["--pos", "%dx%d" % (o.x, o.y)]
        rot, refl = core.RANDR_VIEW.get(o.transform, ("normal", "normal"))
        if rot != "normal":
            parts += ["--rotate", rot]
        if refl != "normal":
            parts += ["--reflect", refl]
        if abs(o.scale - 1.0) > 1e-9:
            parts += ["--scale", "%g" % o.scale]
    return ("xrandr " + " ".join(parts)) if parts else ""


class _Invalidated(Exception):
    """A hotplug landed between create_configuration and apply."""


# -- detection ----------------------------------------------------------------

def probe(sock_path: str | None = None):
    """A live WlConn on the compositor socket when it advertises
    kde_output_management_v2, else None (the connection is closed again on the
    way out, so a non-KDE session is left with none). Never raises: this runs
    during backend auto-detection."""
    try:
        from wdotool.wayland_mini import WlConn
        if sock_path is None:
            hit = wsession.find_wayland_socket()
            if hit is None:
                return None
            sock_path = hit[2]
        conn = WlConn(sock_path)
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        conn.sock.settimeout(10.0)
        for iface, _ver in conn.get_registry().values():
            if iface == MGMT:
                return conn
    except (OSError, RuntimeError, ValueError, struct.error):
        pass
    conn.close()
    return None


# -- the backend --------------------------------------------------------------

class KwinOutputs:
    """Snapshot + one-shot atomic apply over kde_output_management_v2."""

    def __init__(self, conn=None, socket_path: str | None = None):
        from wdotool.wayland_mini import WlConn
        self._own_conn = conn is None
        if conn is None:
            if socket_path is None:
                hit = wsession.find_wayland_socket()
                if hit is None:
                    raise Fatal("cannot connect to the compositor "
                                "(no wayland socket found)\n")
                socket_path = hit[2]
            conn = WlConn(socket_path)
        self.conn = conn
        try:
            # dispatch() clears the timeout again; _send restores it
            conn.sock.settimeout(10.0)
        except OSError:
            pass
        regs = conn.get_registry()
        mgmt = next(((n, v) for n, (i, v) in sorted(regs.items())
                     if i == MGMT), None)
        if mgmt is None:
            self.close()
            raise Fatal("compositor does not advertise %s (not a KDE Plasma "
                        "session?)\n" % MGMT)
        self.mgmt_advertised = mgmt[1]
        self.mgmt_version = max(1, min(mgmt[1], MGMT_WANT))
        self.mgmt = conn.bind(mgmt[0], MGMT, self.mgmt_version)
        self.has_gamma = any(i == GAMMA for i, _v in regs.values())
        self.dev_version = 0
        self.registry = None          # kde_output_device_registry_v2 (6.7+)
        self.devices = []             # device records, server announce order
        self._by_global = {}
        self._by_id = {}
        self.by_name = {}             # published name -> device record
        self.edid = {}                # name -> base64 EDID
        self.uuid = {}                # name -> KWin's stable uuid
        self.primary = None           # the primary we believe KWin has
        self._primary_seen = False
        self._current = []            # the OutputStates of the last snapshot
        self.previous = []            # pre-apply snapshot, for recovery
        self._warned_save = False
        self._discover()

    def close(self):
        if self._own_conn:
            try:
                self.conn.close()
            except OSError:
                pass

    # -- discovery -----------------------------------------------------------

    def _discover(self):
        """Bind whatever announces outputs, both shapes. Idempotent: a second
        call picks up hotplugged globals and drops departed ones."""
        regs = self.conn.get_registry()
        reg = next(((n, v) for n, (i, v) in sorted(regs.items()) if i == REG),
                   None)
        if reg is not None and self.registry is None:
            name, adv = reg
            ver = min(adv, REG_WANT)
            if ver >= REG_MIN:
                self.dev_version = ver
                self.registry = self.conn.bind(name, REG, ver)
                self.conn.on(self.registry, self._on_registry)
        if self.registry is not None:
            return                      # devices arrive as new_ids, not globals
        live = set()
        for name, (iface, adv) in sorted(regs.items()):
            if iface != DEV:
                continue
            live.add(name)
            if name in self._by_global:
                continue
            ver = min(adv, DEV_WANT)
            self.dev_version = ver
            self._add_device(self.conn.bind(name, DEV, ver), gname=name)
        for name in [n for n in self._by_global if n not in live]:
            self._by_global.pop(name)["gone"] = True

    def _on_registry(self, op, cur, fds):
        """kde_output_device_registry_v2's `output` event: one new_id of
        kde_output_device_v2 per output. The interface lives in
        plasma-wayland-protocols, not in the kwin tree, and the opcode is
        recorded as 1 -- so rather than trust one index we accept any event
        whose whole payload is a single server-range new_id, which no other
        event of this interface can look like."""
        if len(cur.d) != 4:
            return
        oid = cur.u32()
        if oid < SERVER_ID_BASE or oid in self._by_id:
            return
        self._add_device(oid)

    def _add_device(self, oid: int, gname=None):
        d = {"id": oid, "global": gname, "name": "", "make": "Unknown",
             "model": "Unknown", "serial": "Unknown", "eisa": "", "uuid": "",
             "edid": "", "mm_w": 0, "mm_h": 0, "subpixel": 0, "transform": 0,
             "x": 0, "y": 0, "scale": 1.0, "enabled": False, "current": None,
             "modes": [], "caps": 0, "priority": None, "gone": False,
             "pub": None}
        self._by_id[oid] = d
        if gname is not None:
            self._by_global[gname] = d
        self.devices.append(d)
        self.conn.on(oid, lambda op, cur, fds, d=d: self._on_device(d, op, cur))
        return d

    # -- device events -------------------------------------------------------

    def _on_device(self, d, op, cur):
        if op == 0:      # geometry(x, y, mm_w, mm_h, subpixel, make, model, tf)
            d["x"], d["y"] = cur.i32(), cur.i32()
            d["mm_w"], d["mm_h"] = cur.i32(), cur.i32()
            d["subpixel"] = cur.i32()
            d["make"], d["model"] = cur.string(), cur.string()
            d["transform"] = cur.i32()
        elif op == 1:    # current_mode(object) -- only sent while enabled
            d["current"] = cur.u32()
        elif op == 2:    # mode(new_id): a server-allocated mode object
            mid = cur.u32()
            m = {"id": mid, "w": 0, "h": 0, "refresh": 0, "preferred": False,
                 "gone": False}
            d["modes"].append(m)
            self.conn.on(mid, lambda op, cur, fds, m=m: self._on_mode(m, op,
                                                                     cur))
        elif op == 3:    # done: the atomic publish barrier
            self._publish(d)
        elif op == 4:
            d["scale"] = from_fixed(cur.fixed())
        elif op == 5:
            d["edid"] = cur.string()
        elif op == 6:
            d["enabled"] = bool(cur.i32())
        elif op == 7:
            d["uuid"] = cur.string()
        elif op == 8:
            d["serial"] = cur.string() or "Unknown"
        elif op == 9:
            d["eisa"] = cur.string()
        elif op == 10:
            d["caps"] = cur.u32()
        elif op == 14:   # name(connector) -- since device v2
            d["name"] = cur.string()
        elif op == 34:   # priority -- since v18, only on the registry path
            d["priority"] = cur.u32()
        elif op == 36:   # removed -- since v21
            d["gone"] = True

    @staticmethod
    def _on_mode(m, op, cur):
        if op == 0:
            m["w"], m["h"] = cur.i32(), cur.i32()
        elif op == 1:
            m["refresh"] = cur.i32()     # mHz
        elif op == 2:
            m["preferred"] = True
        elif op == 3:
            m["gone"] = True             # the object is destroyed right after

    @staticmethod
    def _publish(d):
        pub = {k: v for k, v in d.items() if k not in ("pub", "modes")}
        pub["modes"] = [dict(m) for m in d["modes"] if not m["gone"]]
        d["pub"] = pub

    def _settle(self):
        for _ in range(SETTLE_ROUNDS):
            self.conn.roundtrip()
            if all(d["pub"] is not None for d in self.devices if not d["gone"]):
                return

    def _refresh(self):
        """Drain pending events (global add/remove included), bind whatever is
        new, then dispatch until every live device has crossed its `done`."""
        self.conn.roundtrip()
        self._discover()
        self._settle()

    def live(self) -> list:
        return [d for d in self.devices if not d["gone"] and d["pub"]]

    # -- query ---------------------------------------------------------------

    def snapshot(self, state: core.State) -> list:
        """OutputState list in KWin's announce order, built from the published
        (post-`done`) device state."""
        self._refresh()
        self.by_name, self.edid, self.uuid = {}, {}, {}
        outs = []
        for i, d in enumerate(self.live()):
            p = d["pub"]
            name = p["name"] or p["uuid"] or "output-%d" % (i + 1)
            st = OutputState(name=name, active=p["enabled"], ident=i + 1,
                             mm_w=p["mm_w"], mm_h=p["mm_h"],
                             make=p["make"] or "Unknown",
                             model=p["model"] or "Unknown",
                             serial=p["serial"] or "Unknown",
                             subpixel=SUBPIXEL.get(p["subpixel"], "unknown"))
            self.by_name[name] = d
            self.edid[name] = p["edid"]
            self.uuid[name] = p["uuid"]
            st.virtual_modes = not p["modes"]
            current = None
            for m in p["modes"]:
                mode = Mode(w=m["w"], h=m["h"], refresh_mhz=m["refresh"],
                            preferred=m["preferred"], mode_id="%d" % m["id"])
                st.modes.append(mode)
                if p["current"] == m["id"]:
                    current = mode
            if st.active:
                st.x, st.y = p["x"], p["y"]
                st.scale = p["scale"] or 1.0
                st.transform = from_transform(p["transform"])
                st.current = current if current is not None else (
                    st.modes[0] if st.modes else None)
                if st.current is not None:
                    st.w, st.h = logical_size(st.current.w, st.current.h,
                                              st.transform, st.scale)
            if not any(m.preferred for m in st.modes) and st.modes:
                st.modes[0].preferred = True
            st.modes.extend(state.modes_for_output(name))
            outs.append(st)
        # KWin does not report its primary at device v2 (`priority` is since
        # v18 and only reachable on the registry path), so the state file is
        # the record of what we set -- read once, then only a successful
        # set_primary_output moves it (a re-snapshot mid-apply must not make
        # the pending change look already done).
        if not self._primary_seen:
            self.primary, self._primary_seen = state.primary, True
        self._current = outs
        return outs

    # -- planning ------------------------------------------------------------

    def resolve_mode(self, t: core.Target, state: core.State) -> Mode:
        """The real mode an enabled target will run: the stanza's, else the
        current one, else the mode wxrandr disabled it at (state file), else
        the preferred one. A custom (--newmode) mode is only applicable when a
        real mode of the same size and rate exists -- KWin needs a mode object,
        and creating one needs management v18 plus capability_custom_modes,
        which nothing we bind offers."""
        o = t.output
        mode = t.mode
        if mode is None:
            mode = o.current
        if mode is None:
            last = state.lastmodes().get(t.name)
            if last:
                mode = match_mode(o.modes, last[0], last[1],
                                  (last[2] or 0) / 1000.0 or None)
        if mode is None:
            mode = next((m for m in o.modes if m.preferred and m.mode_id), None)
        if mode is None:
            mode = next((m for m in o.modes if m.mode_id), None)
        if mode is None:
            raise Fatal("cannot find preferred mode\n")
        if mode.mode_id:
            return mode
        real = match_mode(o.modes, mode.w, mode.h, mode.refresh_hz or None,
                          tolerance=1.0)
        if real is None:
            raise Fatal("cannot find mode %s\n" % mode.display_name)
        return real

    def supports_custom_modes(self, name: str) -> bool:
        """Custom modes need management v18 (create_mode_list) *and* the
        device's capability_custom_modes bit. We bind management at
        min(advertised, 12), so this is False everywhere today -- it is the
        honest form of the check, not a guess."""
        d = self.by_name.get(name)
        caps = d["pub"]["caps"] if d and d["pub"] else 0
        return (self.mgmt_version >= CUSTOM_MODES_MGMT
                and bool(caps & CAP_CUSTOM_MODES))

    def _scale_for(self, t: core.Target) -> float:
        """1/120-quantised, and never <= 0: KWin drops a non-positive scale
        silently, so we keep the current one and say so."""
        if t.scale <= 0:
            cur = t.output.scale if t.output.active else 1.0
            warn("scale %g is not usable on KWin; keeping %g for %s\n"
                 % (t.scale, cur, t.name))
            return quantize_scale(cur)
        return quantize_scale(t.scale)

    def predicted_dims(self, t: core.Target, state: core.State) -> tuple:
        """Pending logical size in KWin's space (the dryrun/verbose plan and
        the --fb checks use this instead of the wlroots prediction)."""
        m = self.resolve_mode(t, state)
        return logical_size(m.w, m.h, t.sway_tf, self._scale_for(t))

    @staticmethod
    def _mode_object(pub, mode: Mode):
        """The device's mode object for `mode`: same size, nearest refresh.
        Matching by value rather than by the remembered object id is what
        survives a re-snapshot -- the server allocates a fresh id for every
        mode event, so ids from the previous snapshot mean nothing."""
        cands = [m for m in pub["modes"] if (m["w"], m["h"]) == (mode.w, mode.h)]
        if not cands:
            return None
        if mode.refresh_mhz:
            return min(cands,
                       key=lambda m: abs(m["refresh"] - mode.refresh_mhz))["id"]
        return cands[0]["id"]

    def plan(self, state: core.State, targets: list) -> tuple:
        """(per-output delta records, primary device id or None).

        Deltas only, against the published device state: unmentioned outputs
        and unmentioned properties are left alone by KWin, and a changeset
        that changes nothing still costs a modeset. An output coming back from
        disabled gets the full set -- KWin's stored geometry for a disabled
        output is not what the query showed."""
        known = [t for t in targets if t.name in self.by_name]
        if known and not any(t.enabled for t in known):
            raise Fatal("cannot disable all outputs (KWin requires at least "
                        "one enabled output)\n")
        modes, scales = {}, {}
        for t in known:
            if not t.enabled:
                continue
            modes[t.name] = self.resolve_mode(t, state)
            scales[t.name] = self._scale_for(t)
        by_target = {t.name: t for t in known}
        dims = {n: logical_size(modes[n].w, modes[n].h, by_target[n].sway_tf,
                                scales[n]) for n in modes}
        pos = normalise(core.resolve_positions(known, dims))
        records = []
        for t in known:
            d = self.by_name[t.name]
            p = d["pub"]
            rec = {"name": t.name, "dev": d["id"], "enable": None,
                   "mode": None, "transform": None, "position": None,
                   "scale": None}
            if not t.enabled:
                if p["enabled"]:
                    rec["enable"] = False
                    records.append(rec)
                continue
            full = not p["enabled"]
            if full:
                rec["enable"] = True
            obj = self._mode_object(p, modes[t.name])
            if obj is None:
                raise Fatal("cannot find mode %s\n"
                            % modes[t.name].display_name)
            if full or obj != p["current"]:
                rec["mode"] = obj
            tf = to_transform(t.sway_tf)
            if full or tf != p["transform"]:
                rec["transform"] = tf
            xy = pos.get(t.name)
            if xy is not None and (full or xy != (p["x"], p["y"])):
                rec["position"] = xy
            if full or abs(scales[t.name] - p["scale"]) > 1e-9:
                rec["scale"] = scales[t.name]
            if any(rec[k] is not None for k in ("enable", "mode", "transform",
                                                "position", "scale")):
                records.append(rec)
        primary = None
        want = state.primary
        if want and want != self.primary:
            t = next((t for t in known if t.name == want and t.enabled), None)
            if t is not None:
                if self.mgmt_version >= PRIMARY_MGMT:
                    primary = (self.by_name[want]["id"], want)
                else:
                    warn("this KWin is too old for --primary (%s version "
                         "%d)\n" % (MGMT, self.mgmt_version))
        return records, primary

    # -- apply ---------------------------------------------------------------

    def _send(self, records: list, primary):
        """One configuration object, the deltas, one apply. The object is
        never reused: a second apply on it is a fatal `already_applied`
        protocol error that takes the whole connection down."""
        cfg = self.conn.alloc()
        result = {}

        def on_config(op, cur, fds):
            if op == 0:
                result["ok"] = True
            elif op == 1:
                result["failed"] = True
            elif op == 2:                      # since management v12
                result["reason"] = cur.string()
        self.conn.on(cfg, on_config)
        self.conn.send(self.mgmt, 0, [("u", cfg)])   # create_configuration
        for rec in records:
            dev = rec["dev"]
            if rec["enable"] is not None:
                self.conn.send(cfg, REQ_ENABLE,
                               [("u", dev), ("i", 1 if rec["enable"] else 0)])
            if rec["mode"] is not None:
                self.conn.send(cfg, REQ_MODE, [("u", dev), ("u", rec["mode"])])
            if rec["transform"] is not None:
                self.conn.send(cfg, REQ_TRANSFORM,
                               [("u", dev), ("i", rec["transform"])])
            if rec["position"] is not None:
                x, y = rec["position"]
                self.conn.send(cfg, REQ_POSITION,
                               [("u", dev), ("i", x), ("i", y)])
            if rec["scale"] is not None:
                # the wl_fixed is marshalled here, not by _marshal's "f"
                self.conn.send(cfg, REQ_SCALE,
                               [("u", dev), ("i", to_fixed(rec["scale"]))])
        if primary is not None:
            self.conn.send(cfg, REQ_SET_PRIMARY, [("u", primary[0])])
        self.conn.send(cfg, REQ_APPLY, [])
        deadline = time.monotonic() + APPLY_TIMEOUT
        try:
            while not ("ok" in result or "failed" in result):
                if time.monotonic() >= deadline:
                    break
                self.conn.dispatch(timeout=1.0)
        except (RuntimeError, OSError) as e:
            raise Fatal("%s\n" % e)
        finally:
            # dispatch() leaves the socket blocking; a compositor that goes
            # quiet must not hang the CLI on the post-apply re-read
            try:
                self.conn.sock.settimeout(10.0)
            except OSError:
                pass
        try:
            self.conn.send(cfg, REQ_DESTROY, [])
        except OSError:
            pass
        self.conn.handlers.pop(cfg, None)
        if "failed" in result:
            reason = result.get("reason")
            if reason and INVALID_HINT in reason:
                raise _Invalidated(reason)
            if reason:
                raise Fatal("%s\n" % reason)
            if self.mgmt_version < REASON_MGMT:
                raise Fatal("KWin rejected the output configuration (this "
                            "KWin is too old to report why)\n")
            raise Fatal("KWin rejected the output configuration\n")
        if "ok" not in result:
            raise Fatal("timed out waiting for the compositor to apply the "
                        "output configuration\n")

    def _warn_saved(self):
        if self._warned_save:
            return
        self._warned_save = True
        warn(SAVE_WARNING)
        cmd = restore_command(self.previous)
        if cmd:
            warn("to restore the previous layout: %s\n" % cmd)

    def _rebind(self, targets: list, outputs: list) -> list:
        """Re-point targets at a fresh snapshot after a hotplug: the device
        and mode objects of the old one are gone."""
        by_name = {o.name: o for o in outputs}
        out = []
        for t in targets:
            fresh = by_name.get(t.name)
            if fresh is None:
                continue        # the output the plan named is no longer there
            t.output = fresh
            if t.mode is not None:
                twin = match_mode(fresh.modes, t.mode.w, t.mode.h,
                                  t.mode.refresh_hz or None)
                if twin is not None:
                    t.mode = twin
            out.append(t)
        return out

    def verify(self, state: core.State, targets: list):
        """--dryrun: KWin has no verify request (and building a configuration
        without applying it changes nothing), so this only re-runs the plan --
        client-side rejections still surface, the compositor is not touched."""
        self.plan(state, targets)

    def apply(self, state: core.State, targets: list,
              persistent: bool = False) -> list:
        """One atomic configuration, applied once. Returns the fresh
        snapshot. `persistent` is accepted for contract parity and ignored:
        KWin persists every applied layout itself."""
        records, primary = self.plan(state, targets)
        if not records and primary is None:
            return self.snapshot(state)
        self.previous = list(self._current)
        self._warn_saved()
        for t in targets:
            if t.changed and not t.enabled and t.output.active:
                cur = t.output.current
                if cur:
                    state.lastmodes()[t.name] = [cur.w, cur.h, cur.refresh_mhz]
        try:
            self._send(records, primary)
        except _Invalidated:
            # a hotplug between create_configuration and apply silently
            # invalidated the object: rebuild from a fresh snapshot, once
            targets = self._rebind(targets, self.snapshot(state))
            records, primary = self.plan(state, targets)
            if records or primary is not None:
                self._send(records, primary)
        if primary is not None:
            self.primary = primary[1]
        return self.snapshot(state)
