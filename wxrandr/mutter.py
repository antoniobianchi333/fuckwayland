"""wxrandr GNOME backend: org.gnome.Mutter.DisplayConfig over wdotool.dbus_mini.

Mutter (gnome-shell's compositor) has no zwlr_output_management; its display
API is the session-bus object /org/gnome/Mutter/DisplayConfig, callable by
any client on the user bus — no extension, no root:

- GetCurrentState() -> (serial, monitors, logical_monitors, properties). One
  monitor per connected connector (== the xrandr output; disabled ones
  included) with vendor/product/serial, mm sizes and its mode list — opaque
  mode ids ("1920x1080@60.000", "1920x1080i@59.940"), refresh as a double,
  is-current/is-preferred/is-interlaced flags and the per-mode list of
  supported scales. One logical monitor per enabled screen region: (x, y,
  scale, transform 0..7 (wl_output numbering; see MUTTER_RANDR_VIEW), primary,
  member monitors — several members == mirror). Sizes are never sent: the
  logical size is the mode size, swapped for 90/270, then in layout-mode 1
  (logical) roundf(px / scale) and in layout-mode 2 (physical — the GNOME 46
  default without "Fractional Scaling") the raw pixels. Read every time.
- ApplyMonitorsConfig(serial, method, logical_monitors, properties): the
  WHOLE layout in one call — atomic, exactly xrandr's model. method 0
  verifies only, 1 applies temporarily (xrandr semantics, no dialog), 2 also
  writes ~/.config/monitors.xml, after which gnome-shell asks "Keep changes?"
  for 20 s. Every connected monitor not listed is disabled. Mutter validates:
  exactly one primary, no overlap, every logical monitor edge-adjacent to
  another (a hole is "not adjacent"), min x = min y = 0, scale exactly one of
  the mode's supported scales, mirror members with identical modes, only mode
  ids it handed out (no custom modes). Rejections come back as D-Bus errors
  whose text is relayed verbatim after `xrandr: GNOME's Mutter refused this
  layout: ` -- Mutter's words, said in Mutter's name, because we never refuse
  a layout ourselves. Measured: with two monitors an overlap never produces
  "Logical monitors overlap"; adjacency is checked first and every layout
  that is not exactly edge-adjacent, overlap and gap alike, comes back
  "Logical monitors not adjacent". Nothing is half-applied.
- MonitorsChanged fires after a successful apply; the serial bumps on every
  change and a stale serial is AccessDenied: the state is re-read and the
  same plan re-sent once, but only when the monitors and layout it was built
  from are still what Mutter has (a serial bump alone); a hotplug or a
  concurrent re-layout in that window is "cancelled by a concurrent change".
- Holes: X tolerates a gap, Mutter does not, so an output that touched a
  neighbour's right/bottom edge keeps touching it when that edge moves
  (`--output A --rotate left` / `--mode SMALLER` / `-s` / `-o` in the middle of
  a row shift the outputs to its right along, one warning each) unless it
  was positioned explicitly in the same invocation (keep_adjacent). An
  output whose neighbour went `--off` is not moved: Mutter's own
  "not adjacent" is the answer, re-place it in the same call.
- GNOME 50 quirk (verified): after a temporary re-primary the old logical
  monitor keeps `primary=true` in GetCurrentState until it is rebuilt, so
  several may be flagged; the legacy GetResources output property
  `primary` tracks the real one (what XWayland and the shell use) and
  breaks the tie. The serial does not bump on temporary applies there.

Mapping to the wxrandr model (core.OutputState / core.Target):
    output name          connector          enabled  in some logical monitor
    x, y                 logical monitor x/y (its coordinate space)
    w, h                 derived (logical_size)   scale/transform  per region
    --same-as            one logical monitor with several members
    --primary            the primary flag (real; state.primary is synced)
    --brightness/--gamma no LUT API: warn + succeed
    --newmode/--addmode  state file as elsewhere; applying one needs a real
                         mode with the same size (and rate) -> `cannot find mode`
"""

import math
import struct

from wdotool import session as wsession
from wdotool.dbus_mini import Bus, DBusError, Variant
from wxrandr import core
from wxrandr.core import (Fatal, Mode, OutputState,   # noqa: F401
                          round_half_away, warn)

DEST = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"
IFACE = "org.gnome.Mutter.DisplayConfig"
APPLY_SIG = "uua(iiduba(ssa{sv}))a{sv}"
VERIFY, TEMPORARY, PERSISTENT = 0, 1, 2
LAYOUT_LOGICAL, LAYOUT_PHYSICAL = 1, 2
MONITORS_CHANGED_TIMEOUT = 5.0
CANCELLED = ("output configuration cancelled by a concurrent change; "
             "try again\n")
_MATCH = "type='signal',interface='%s',member='MonitorsChanged'" % IFACE
PERSIST_WARNING = ('GNOME will ask "Keep changes?" for 20 s; confirm the '
                   "dialog or the layout reverts\n")


# -- pure helpers (unit-tested) ----------------------------------------------

def logical_size(px_w: int, px_h: int, sway_tf: str, scale: float,
                 layout_mode: int = LAYOUT_LOGICAL) -> tuple[int, int]:
    """Mutter's logical monitor size: transform swap, then roundf(px/scale)
    in layout-mode 1; raw pixels in layout-mode 2 (scale is a pure UI
    factor there). Differs from core.logical_size (wlroots truncates)."""
    if core.transform_swaps(sway_tf):
        px_w, px_h = px_h, px_w
    if layout_mode == LAYOUT_PHYSICAL or not scale:
        return (px_w, px_h)
    return (round_half_away(px_w / scale), round_half_away(px_h / scale))


# What real xrandr prints through Mutter's XWayland for each Mutter transform
# (measured on GNOME 50, all eight): Xwayland's wl_transform_to_xrandr maps
# n -> RR_Rotate_(90*n) [| RR_Reflect_X], and the spec's 90 is
# counter-clockwise, i.e. xrandr `left`. sway's verified table
# (core.RANDR_VIEW) has "90" == `right`, so the two numberings differ by a
# 90<->270 swap (1<->3, 5<->7); the renderer keeps speaking sway names.
MUTTER_RANDR_VIEW = {0: ("normal", "normal"), 1: ("left", "normal"),
                     2: ("inverted", "normal"), 3: ("right", "normal"),
                     4: ("normal", "x"), 5: ("left", "x"),
                     6: ("inverted", "x"), 7: ("right", "x")}
SWAY_FROM_MUTTER = {n: next(tf for tf, v in core.RANDR_VIEW.items() if v == view)
                    for n, view in MUTTER_RANDR_VIEW.items()}
MUTTER_FROM_SWAY = {tf: n for n, tf in SWAY_FROM_MUTTER.items()}


def to_transform(sway_tf: str) -> int:
    """sway transform name (what core/RANDR_VIEW use) -> Mutter transform."""
    return MUTTER_FROM_SWAY.get(sway_tf, 0)


def from_transform(n: int) -> str:
    return SWAY_FROM_MUTTER.get(n, "normal")


def snap_scale(scale: float, supported) -> float:
    """Nearest of the mode's supported scales (ties -> the smaller one);
    Mutter accepts nothing else. Empty list -> 1.0."""
    supported = [float(s) for s in supported or ()]
    if not supported:
        return 1.0
    return min(supported, key=lambda s: (abs(s - scale), s))


def _interlaced(m: Mode) -> bool:
    return any(f.lower() == "interlace" for f in m.flags)


def match_mode(modes, w: int, h: int, rate_hz: float | None = None,
               tolerance: float | None = None,
               interlaced: bool = False) -> Mode | None:
    """The real (mode-id bearing) mode of size w x h: nearest refresh when a
    rate is given (within `tolerance` Hz if set), else the first listed."""
    cands = [m for m in modes if m.mode_id and (m.w, m.h) == (w, h)
             and _interlaced(m) == interlaced]
    if not cands:
        return None
    if rate_hz:
        best = min(cands, key=lambda m: abs(m.refresh_hz - rate_hz))
        if tolerance is not None and abs(best.refresh_hz - rate_hz) > tolerance:
            return None
        return best
    return cands[0]


def _mode_from_wire(mid: str, w: int, h: int, rate: float, mp: dict) -> Mode:
    interlaced = bool(mp.get("is-interlaced", False))
    return Mode(w=w, h=h, refresh_mhz=int(round(rate * 1000)),
                preferred=bool(mp.get("is-preferred", False)),
                name=("%dx%di" % (w, h)) if interlaced else None,
                flags=("interlace",) if interlaced else (),
                mode_id=mid)


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _text(e: DBusError) -> str:
    return (e.message or e.name) + "\n"


def _refused(e: DBusError) -> str:
    """A rejected ApplyMonitorsConfig, in Mutter's name.  We pass every
    layout on unchanged -- overlaps included, which X11, KWin and wlroots
    all take -- so when one comes back refused the limit is GNOME's, and
    the line has to say so before quoting Mutter's own words (a two-monitor
    overlap gets "Logical monitors not adjacent", the same sentence a gap
    gets)."""
    return "GNOME's Mutter refused this layout: " + _text(e)


def _is_stale(e: DBusError) -> bool:
    return e.name.endswith("AccessDenied") and "stale" in (e.message or "").lower()


def _canon(plan) -> list:
    """Order-free form of a logical-monitor plan, for change detection."""
    return sorted((p["x"], p["y"], float(p["scale"]), int(p["transform"]),
                   bool(p["primary"]),
                   tuple(sorted((c, mid, bool(us)) for c, mid, us in p["members"])))
                  for p in plan)


def _positioned(t: core.Target) -> bool:
    """The invocation says where this output goes (--pos or a relation)."""
    s = t.stanza
    return s is not None and (s.pos is not None or s.relation is not None)


def keep_adjacent(targets: list, dims: dict, pos: dict) -> list:
    """Mutter allows no gaps (X does): an output that shared its left (top)
    edge with a neighbour's right (bottom) edge before this invocation keeps
    touching it when that edge moves because the neighbour changed mode,
    rotation or scale (or followed another one itself). Explicit positions
    are the user's: an output placed here (--pos / --left-of ...) never
    moves, and nothing follows one — its old neighbours may no longer be
    neighbours at all — so Mutter's verdict on such a layout stands.
    Neighbours that went --off pull nothing either (nothing to stay adjacent
    to; Mutter reports the hole). Mutates `pos` (every enabled output,
    min x = min y = 0 as core.resolve_positions leaves it) and returns
    [(name, (x, y), neighbour)] for each output it moved, in move order."""
    old = {t.name: (t.output.x, t.output.y, t.output.w, t.output.h)
           for t in targets if t.output.active}
    fixed = {t.name for t in targets if _positioned(t)}
    movable = [t.name for t in targets
               if t.enabled and t.name in old and t.name in pos
               and t.name not in fixed]
    moves = {}
    for _ in range(len(movable) + 1):
        changed = False
        for n in movable:
            ox, oy, ow, oh = old[n]
            x, y = pos[n]
            # neighbours still enabled whose old right (bottom) edge was n's
            # old left (top) edge with strict overlap; n goes to the
            # farthest of their new edges (touch one, overlap none)
            lefts = [(pos[m][0] + dims[m][0], m)
                     for m, (mx, my, mw, mh) in old.items()
                     if m != n and m in pos and m not in fixed
                     and mx + mw == ox and my < oy + oh and oy < my + mh]
            tops = [(pos[m][1] + dims[m][1], m)
                    for m, (mx, my, mw, mh) in old.items()
                    if m != n and m in pos and m not in fixed
                    and my + mh == oy and mx < ox + ow and ox < mx + mw]
            nx, via_x = max(lefts) if lefts else (x, None)
            ny, via_y = max(tops) if tops else (y, None)
            if (nx, ny) != (x, y):
                pos[n] = (nx, ny)
                moves[n] = (pos[n], via_x if nx != x else via_y)
                changed = True
        if not changed:
            break
    if moves:
        min_x = min(p[0] for p in pos.values())
        min_y = min(p[1] for p in pos.values())
        if min_x or min_y:
            for n, (x, y) in list(pos.items()):
                pos[n] = (x - min_x, y - min_y)
            for n in moves:
                moves[n] = (pos[n], moves[n][1])
    return [(n, p, via) for n, (p, via) in moves.items()]


# -- detection ----------------------------------------------------------------

def probe(addr: str | None = None):
    """A Bus on the graphical session's D-Bus if Mutter's DisplayConfig is
    there, else None. Never raises (backend auto-detection)."""
    try:
        if addr is None:
            hit = wsession.find_session_bus()
            if not hit:
                return None
            addr = hit[1]
        bus = Bus(addr)
    except (DBusError, OSError, ValueError):
        return None
    try:
        if bus.name_has_owner(DEST):
            return bus
    except DBusError:
        pass
    bus.close()
    return None


# -- wl_output enrichment -----------------------------------------------------

_SUBPIXEL = {0: "unknown", 1: "none", 2: "horizontal rgb", 3: "horizontal bgr",
             4: "vertical rgb", 5: "vertical bgr"}


def wl_output_info(sock_path: str | None = None) -> dict:
    """{connector: {"mm_w", "mm_h", "subpixel", "make", "model"}} from the
    compositor's wl_output globals (v4: the `name` event is the connector).
    Mutter 46 and 50 never put width-mm/height-mm into GetCurrentState (the
    XML documents them, the code does not emit them — verified with gdbus)
    but hands the EDID size to wl_output, which is what XWayland's RandR
    shows; reading it keeps the header/--listmonitors mm byte-identical.
    Never raises: {} when there is no reachable Wayland socket."""
    try:
        from wdotool.wayland_mini import WlConn
        if sock_path is None:
            hit = wsession.find_wayland_socket()
            if hit is None:
                return {}
            sock_path = hit[2]
        conn = WlConn(sock_path)
    except (OSError, RuntimeError, ValueError):
        return {}
    try:
        conn.sock.settimeout(5.0)
        outs = []
        for gname, (iface, ver) in list(conn.get_registry().items()):
            if iface != "wl_output" or ver < 4:
                continue
            o = {"name": "", "mm_w": 0, "mm_h": 0, "subpixel": "unknown",
                 "make": "", "model": ""}

            def handler(op, cur, fds, o=o):
                if op == 0:  # geometry(x, y, mm_w, mm_h, subpixel, make, model, tf)
                    cur.i32()
                    cur.i32()
                    o["mm_w"], o["mm_h"] = cur.i32(), cur.i32()
                    o["subpixel"] = _SUBPIXEL.get(cur.i32(), "unknown")
                    o["make"], o["model"] = cur.string(), cur.string()
                elif op == 4:  # name(connector)
                    o["name"] = cur.string()
            conn.on(conn.bind(gname, "wl_output", 4), handler)
            outs.append(o)
        conn.roundtrip()
        return {o["name"]: o for o in outs if o["name"]}
    except (OSError, RuntimeError, ValueError, struct.error):
        return {}
    finally:
        conn.close()


# -- the backend --------------------------------------------------------------

class MutterOutputs:
    """Snapshot + one-call atomic apply over org.gnome.Mutter.DisplayConfig."""

    def __init__(self, bus: Bus | None = None, addr: str | None = None,
                 wl_socket=None):
        """`wl_socket`: Wayland socket path for the wl_output enrichment
        (None = the session's, False = none)."""
        self.wl_socket = wl_socket
        if bus is None:
            if addr is None:
                hit = wsession.find_session_bus()
                if not hit:
                    raise DBusError("org.freedesktop.DBus.Error.NoServer",
                                    "no session D-Bus found")
                addr = hit[1]
            bus = Bus(addr)
        self.bus = bus
        try:
            owned = self.bus.name_has_owner(DEST)
        except DBusError:
            self.bus.close()
            raise
        if not owned:
            self.bus.close()
            raise Fatal("%s is not on the session bus (not a GNOME "
                        "session?)\n" % DEST)
        self.serial = 0
        self.fingerprint = None      # monitors + layout the serial stood for
        self.props = {}
        self.layout_mode = LAYOUT_LOGICAL
        self.global_scale_required = False
        self.primary = None          # connector of the primary logical monitor
        self.scales = {}             # (connector, mode_id) -> supported scales
        self.underscan = {}          # connector -> currently underscanning
        self.current_config = []     # _canon() of what Mutter shows now
        self.last_method = None      # method of the last ApplyMonitorsConfig
        self._matched = False

    def close(self):
        self.bus.close()

    # -- query ---------------------------------------------------------------

    def get_current_state(self):
        try:
            return self.bus.call(DEST, PATH, IFACE, "GetCurrentState")
        except DBusError as e:
            raise Fatal(_text(e))

    def snapshot(self, state: core.State) -> list:
        """OutputState list in Mutter's monitor order; records serial,
        layout mode, supported scales, the real primary (synced into
        state.primary — the state file never overrides Mutter here)."""
        serial, monitors, logical, props = self.get_current_state()
        wl = {} if self.wl_socket is False else wl_output_info(
            self.wl_socket or None)
        self.serial = serial
        self.fingerprint = self._fingerprint(monitors, logical)
        self.props = props
        self.layout_mode = _int(props.get("layout-mode")) or LAYOUT_LOGICAL
        self.global_scale_required = bool(props.get("global-scale-required",
                                                    False))
        lm_of = {}
        for lm in logical:
            for spec in lm[5]:
                lm_of[spec[0]] = lm
        primary_lm = self._primary_lm(logical, lm_of)
        # Which CONNECTOR is primary, out of the primary logical monitor's
        # members.  A mirror group has several and Mutter names none of them
        # -- the flag is on the group -- so its member order decides, and
        # that order is the order the group was built in, not a choice
        # anybody made.  Mirroring A onto B therefore used to move the
        # primary to whichever came first, silently overwriting a --primary
        # the user had set on the other member.  Keep the user's choice
        # whenever it is still in the group.
        if primary_lm is None:
            self.primary = None
        else:
            members = [spec[0] for spec in primary_lm[5]]
            self.primary = (state.primary if state.primary in members
                            else members[0])
        self.scales = {}
        self.underscan = {}
        current_ids = {}
        outs = []
        for i, (spec, modes, mprops) in enumerate(monitors):
            connector, vendor, product, mserial = spec
            st = OutputState(name=connector, active=connector in lm_of,
                             ident=i + 1,
                             mm_w=_int(mprops.get("width-mm")),
                             mm_h=_int(mprops.get("height-mm")),
                             make=vendor or "Unknown",
                             model=product or "Unknown",
                             serial=mserial or "Unknown")
            self.underscan[connector] = bool(mprops.get("is-underscanning",
                                                        False))
            w_info = wl.get(connector)
            if w_info:
                st.mm_w = st.mm_w or w_info["mm_w"]
                st.mm_h = st.mm_h or w_info["mm_h"]
                st.subpixel = w_info["subpixel"]
            current = None
            for (mid, w, h, rate, _pscale, scales, mp) in modes:
                m = _mode_from_wire(mid, w, h, rate, mp)
                self.scales[(connector, mid)] = ([float(s) for s in scales]
                                                 or [1.0])
                st.modes.append(m)
                if mp.get("is-current"):
                    current = m
            if st.active:
                x, y, scale, transform, primary, _specs, _lp = lm_of[connector]
                st.x, st.y, st.scale = x, y, float(scale)
                st.transform = from_transform(transform)
                st.current = current if current is not None else (
                    st.modes[0] if st.modes else None)
                if st.current is not None:
                    st.w, st.h = logical_size(st.current.w, st.current.h,
                                              st.transform, st.scale,
                                              self.layout_mode)
                    current_ids[connector] = st.current.mode_id
                st.primary = lm_of[connector] is primary_lm
            if not any(m.preferred for m in st.modes) and st.modes:
                st.modes[0].preferred = True
            st.modes.extend(state.modes_for_output(connector))
            outs.append(st)
        self.current_config = _canon([
            {"x": lm[0], "y": lm[1], "scale": lm[2], "transform": lm[3],
             "primary": lm is primary_lm,
             "members": [(s[0], current_ids.get(s[0], ""),
                          self.underscan.get(s[0], False)) for s in lm[5]]}
            for lm in logical])
        state.primary = self.primary
        return outs

    @staticmethod
    def _fingerprint(monitors, logical) -> tuple:
        """What a plan was built from: the connectors with their mode ids
        and current mode, and the logical layout (order-free). Equal
        fingerprints under different serials mean nothing that matters to
        the plan changed (GNOME bumps the serial on its own as well)."""
        mons = tuple((spec[0], tuple(m[0] for m in modes),
                      next((m[0] for m in modes if m[6].get("is-current")),
                           None))
                     for spec, modes, _p in monitors)
        lms = tuple(sorted((lm[0], lm[1], float(lm[2]), int(lm[3]),
                            bool(lm[4]), tuple(sorted(s[0] for s in lm[5])))
                           for lm in logical))
        return mons, lms

    def _primary_lm(self, logical, lm_of):
        """The logical monitor that is really primary. One flagged: that one.
        Several (GNOME 50 keeps stale flags on in-place updates): the one
        holding the connector GetResources marks primary. None: None."""
        flagged = [lm for lm in logical if lm[4]]
        if len(flagged) <= 1:
            return flagged[0] if flagged else None
        try:
            _serial, _crtcs, outputs, _modes, _mw, _mh = self.bus.call(
                DEST, PATH, IFACE, "GetResources")
            for out in outputs:
                if out[7].get("primary") and out[4] in lm_of:
                    return lm_of[out[4]]
        except (DBusError, ValueError, IndexError, TypeError, AttributeError):
            pass
        return flagged[0]

    # -- planning ------------------------------------------------------------

    def resolve_mode(self, t: core.Target, state: core.State) -> Mode:
        """The real mode an enabled target will run: the stanza's, else the
        current one, else the mode wxrandr disabled it at (state file), else
        the preferred one. A custom (--newmode) mode is only applicable when
        a real mode of the same size and rate exists."""
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
            mode = next((m for m in o.modes if m.preferred and m.mode_id),
                        None)
        if mode is None:
            mode = next((m for m in o.modes if m.mode_id), None)
        if mode is None:
            raise Fatal("cannot find preferred mode\n")
        if mode.mode_id:
            return mode
        real = match_mode(o.modes, mode.w, mode.h, mode.refresh_hz or None,
                          tolerance=1.0, interlaced=_interlaced(mode))
        if real is None:
            raise Fatal("cannot find mode %s\n" % mode.display_name)
        return real

    def _scale_for(self, t: core.Target, mode: Mode) -> float:
        """The scale Mutter will accept: an output keeping its mode and scale
        keeps them verbatim (Mutter runs that combination right now, even if
        the scale came from monitors.xml and is not in supported_scales);
        anything else is snapped to the mode's supported list."""
        o = t.output
        if (o.active and o.current is not None
                and o.current.mode_id == mode.mode_id
                and abs(t.scale - o.scale) < 1e-9):
            return t.scale
        return snap_scale(t.scale,
                          self.scales.get((t.name, mode.mode_id)) or [1.0])

    def predicted_dims(self, t: core.Target, state: core.State) -> tuple:
        """Pending logical size of an enabled target in Mutter's space (the
        dryrun/verbose plan and --fb checks use this instead of the wlroots
        prediction)."""
        m = self.resolve_mode(t, state)
        return logical_size(m.w, m.h, t.sway_tf, self._scale_for(t, m),
                            self.layout_mode)

    @staticmethod
    def _floating(t: core.Target) -> bool:
        """Enabled now, was off, and nothing says where it goes: xrandr would
        drop it at 0,0 (an overlap Mutter refuses)."""
        return (t.enabled and not t.output.active
                and (t.stanza is None
                     or (t.stanza.pos is None and t.stanza.relation is None)))

    def _auto_place(self, targets, dims, pos):
        placed = [t.name for t in targets if t.enabled and not self._floating(t)]
        for t in targets:
            if not self._floating(t):
                continue
            if placed:
                right = max(placed, key=lambda n: (pos[n][0] + dims[n][0],
                                                   -pos[n][1]))
                pos[t.name] = (pos[right][0] + dims[right][0], pos[right][1])
                warn("output %s enabled without a position; placing it "
                     "right-of %s\n" % (t.name, right))
            else:
                pos[t.name] = (0, 0)
            placed.append(t.name)
        if pos:
            min_x = min(p[0] for p in pos.values())
            min_y = min(p[1] for p in pos.values())
            if min_x or min_y:
                for n, (x, y) in list(pos.items()):
                    pos[n] = (x - min_x, y - min_y)

    def plan(self, state: core.State, targets: list) -> list:
        """Targets -> logical monitors: modes resolved, scales snapped (warn
        when changed), positions from core.resolve_positions in Mutter's
        logical space, same-position outputs grouped into one (mirror)
        logical monitor, exactly one primary, floating outputs auto-placed."""
        real, scales = {}, {}
        for t in targets:
            if not t.enabled:
                continue
            m = self.resolve_mode(t, state)
            s = self._scale_for(t, m)
            if abs(s - t.scale) > 1e-6:
                warn("scale %g is not available for %s at %dx%d; using %g\n"
                     % (t.scale, t.name, m.w, m.h, s))
            real[t.name], scales[t.name] = m, s
        by_name = {t.name: t for t in targets}
        dims = {n: logical_size(real[n].w, real[n].h, by_name[n].sway_tf,
                                scales[n], self.layout_mode) for n in real}
        pos = core.resolve_positions(targets, dims)
        for n, (x, y), via in keep_adjacent(targets, dims, pos):
            warn("output %s moved to +%d+%d to stay adjacent to %s\n"
                 % (n, x, y, via))
        self._auto_place(targets, dims, pos)
        groups, index = [], {}
        for t in targets:
            if not t.enabled:
                continue
            key = pos[t.name]
            if key in index:
                groups[index[key]][1].append(t.name)
            else:
                index[key] = len(groups)
                groups.append((key, [t.name]))
        enabled = [t.name for t in targets if t.enabled]
        if state.primary in enabled:
            primary = state.primary
        elif self.primary in enabled:
            primary = self.primary
        else:
            primary = enabled[0] if enabled else None
        out = []
        for (x, y), names in groups:
            first = by_name[names[0]]
            for n in names[1:]:
                t = by_name[n]
                if ((real[n].w, real[n].h) != (real[first.name].w,
                                                real[first.name].h)
                        or t.sway_tf != first.sway_tf
                        or scales[n] != scales[first.name]):
                    raise Fatal("cannot mirror %s onto %s: Mutter needs the "
                                "same mode, rotation and scale (%s %s scale "
                                "%g vs %s %s scale %g)\n"
                                % (n, first.name, real[n].display_name,
                                   t.sway_tf, scales[n],
                                   real[first.name].display_name,
                                   first.sway_tf, scales[first.name]))
            out.append({"x": x, "y": y, "scale": scales[first.name],
                        "transform": to_transform(first.sway_tf),
                        "primary": primary in names,
                        "members": [(n, real[n].mode_id,
                                     self.underscan.get(n, False))
                                    for n in names]})
        return out

    @staticmethod
    def to_wire(plan: list) -> list:
        return [(p["x"], p["y"], float(p["scale"]), int(p["transform"]),
                 bool(p["primary"]),
                 [(c, mid, ({"underscanning": Variant("b", True)} if us else {}))
                  for c, mid, us in p["members"]])
                for p in plan]

    # -- apply ---------------------------------------------------------------

    def _call_apply(self, method: int, plan: list):
        self.last_method = method
        self.bus.call(DEST, PATH, IFACE, "ApplyMonitorsConfig", APPLY_SIG,
                      (self.serial, method, self.to_wire(plan), {}))

    def _send(self, method: int, plan: list):
        """ApplyMonitorsConfig with one stale-serial retry, and only when
        the re-read state still has the monitors and layout the plan was
        built from (a plan re-sent after a hotplug would silently leave the
        new monitor out, one re-sent after someone else's re-layout would
        undo it); every other rejection is Mutter's own message as a
        Fatal."""
        try:
            self._call_apply(method, plan)
        except DBusError as e:
            if not _is_stale(e):
                raise Fatal(_refused(e))
            serial, monitors, logical, _props = self.get_current_state()
            if self._fingerprint(monitors, logical) != self.fingerprint:
                raise Fatal(CANCELLED)
            self.serial = serial
            try:
                self._call_apply(method, plan)
            except DBusError as e2:
                if _is_stale(e2):
                    raise Fatal(CANCELLED)
                raise Fatal(_refused(e2))

    def verify(self, state: core.State, targets: list):
        """--dryrun: method 0 — Mutter validates, nothing changes."""
        self._send(VERIFY, self.plan(state, targets))

    def apply(self, state: core.State, targets: list,
              persistent: bool = False) -> list:
        """One ApplyMonitorsConfig for the whole layout, then wait for
        MonitorsChanged (<= 5 s) and return the fresh snapshot. An unchanged
        temporary layout is not re-applied (no modeset for `--primary` on the
        primary); --persistent always writes, so monitors.xml gets it."""
        plan = self.plan(state, targets)
        method = PERSISTENT if persistent else TEMPORARY
        if method == TEMPORARY and _canon(plan) == self.current_config:
            return self.snapshot(state)
        for t in targets:
            if t.changed and not t.enabled and t.output.active:
                cur = t.output.current
                if cur:
                    state.lastmodes()[t.name] = [cur.w, cur.h, cur.refresh_mhz]
        if method == PERSISTENT:
            warn(PERSIST_WARNING)
        if not self._matched:
            try:
                self.bus.add_match(_MATCH)
            except DBusError:
                pass
            self._matched = True
        self._send(method, plan)
        try:
            self.bus.wait_signal(IFACE, "MonitorsChanged",
                                 MONITORS_CHANGED_TIMEOUT)
        except DBusError:
            pass
        return self.snapshot(state)
