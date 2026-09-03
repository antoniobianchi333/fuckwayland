"""OWNER: Agent W. Unified window model + wmctrl semantics over the wdotool
compositor backend, with X11 enrichment for XWayland windows when available.

Dual-plane design per WWMCTL.md:
- the window LIST and all ACTIONS come from the compositor backend
  (wdotool.backend_detect.detect(); backend-native ids address windows),
- XWayland windows are printed with their REAL X11 window id (the backend's
  views() carry it -- GNOME bridge `xid`; sway's raw tree exposes it as the
  node's "window" field) so xprop/real-wmctrl interoperate, and X-only
  data (WM_CLASS, WM_CLIENT_MACHINE, geometry) is read from the XWayland
  server via wwmctl.x11_mini when it is reachable -- with the DISPLAY and
  cookie file the backend reports (x_info(): gnome-shell's own on GNOME,
  where Xwayland runs with -auth), else the session scan,
- with no X server everything still works compositor-only (class from
  the compositor's WM_CLASS, machine falls back to the local hostname —
  XWayland clients are local by construction).

Listing sources, in order: backend.views() (typed View records: GNOME),
the sway-private _nodes() tree, the generic backend.list(). Desktops come
from backend.workspaces() (GNOME: names + work areas) or the sway workspace
list; -k/-n reach the backend's show_desktop()/set_num_desktops() when it
has them (GNOME) and warn otherwise.

Output strings below are byte-parity copies of wmctrl 1.07 (main.c)."""

import dataclasses
import os
import re
import socket
import sys
import time

from wdotool import backend_detect, session
from wdotool.ctx import CmdError
from wwmctl.x11_mini import XUnavailable

# _NET_WM_STATE actions (EWMH)
STATE_REMOVE = 0
STATE_ADD = 1
STATE_TOGGLE = 2

SELECT_WINDOW_MAGIC = ":SELECT:"
ACTIVE_WINDOW_MAGIC = ":ACTIVE:"


def _warn(msg: str):
    sys.stderr.write("wwmctl: %s\n" % msg)


# -- injection seams (unit tests monkeypatch these) --------------------------

def _detect_backend():
    return backend_detect.detect()


def _x11_connect(display=None, xauthority=None):
    """An x11_mini.X11Conn to the XWayland server, or None. Never raises.
    `display`/`xauthority` are what the backend knows about its Xwayland
    (GNOME: gnome-shell's own DISPLAY and Mutter's cookie file); without
    them x11_mini falls back to the environment and the session scan."""
    if os.environ.get("WWMCTL_NO_X"):
        return None
    try:
        from wwmctl import x11_mini
        if display is None and xauthority is None:
            return x11_mini.X11Conn()
        return x11_mini.X11Conn(display, xauthority=xauthority)
    except Exception:
        # covers XUnavailable, the not-yet-implemented stub (AttributeError),
        # and any wire-level failure: degrade to compositor-only silently.
        return None


@dataclasses.dataclass
class UWindow:
    id: int              # printed id: real X11 id for XWayland, else node id
    node_id: int         # compositor node id — actions always go through this
    is_x: bool = False
    title: str | None = None
    class_: str | None = None   # "instance.class" / "app_id.app_id"
    machine: str | None = None
    pid: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    desktop: int = 0     # 0-based workspace index, -1 = all/hidden
    focused: bool = False
    # the compositor's own rectangle (frame rect on GNOME, content rect on
    # sway) -- what move/resize address; x/y/w/h above may be overwritten
    # by the X plane's client rectangle for -lG
    fx: int = 0
    fy: int = 0
    fw: int = 0
    fh: int = 0


class Core:
    def __init__(self, backend=None, verbose=False):
        self._backend = backend
        self._x11 = "unset"
        self._views_seen = None  # last views() outcome: True/False/None
        self.verbose = verbose

    def vprint(self, msg: str):
        if self.verbose:
            sys.stderr.write(msg)

    def backend(self):
        if self._backend is None:
            self._backend = _detect_backend()
        return self._backend

    def _x_params(self):
        """(display, xauthority) the backend knows for its X plane, or None
        (sway, generic backends: x11_mini discovers on its own)."""
        try:
            info_fn = getattr(self.backend(), "x_info", None)
            info = info_fn() if callable(info_fn) else None
        except Exception:
            return None
        if not info:
            return None
        display, xauth = info
        return (display or None), (xauth or None)

    def x11(self):
        if self._x11 == "unset":
            params = self._x_params()
            if params is None:
                self._x11 = _x11_connect()
            else:
                self._x11 = _x11_connect(display=params[0],
                                         xauthority=params[1])
        return self._x11

    def _x_is_up(self) -> bool:
        """May the X plane be opened without side effects? Mutter starts
        Xwayland on demand: a connect just to ask spawns a server. With a
        views() backend the answer is "an X window is listed, or an
        Xwayland process exists"; sway and the rest keep the old behavior
        (try to connect)."""
        if self._x11 not in ("unset", None):
            return True
        try:
            wins = self.windows()
        except CmdError:
            return True
        if self._views_seen is not True:
            return True
        if any(w.is_x for w in wins):
            return True
        return session.xwayland_running()

    # -- unified window list ------------------------------------------------

    def _from_views(self, views, host) -> list[UWindow]:
        out = []
        for v in views:
            win = v.window
            xid = int(v.xid or 0)
            if xid:
                cls = _dot_class(v.instance or None, v.cls or None)
            elif v.app_id:
                cls = "%s.%s" % (v.app_id, v.app_id)
            else:
                cls = _dot_class(v.instance or None, v.cls or None)
            out.append(UWindow(
                id=xid if xid else win.id,
                node_id=win.id,
                is_x=bool(xid),
                title=win.title,
                class_=cls,
                machine=host,
                pid=win.pid,
                x=win.x, y=win.y, w=win.w, h=win.h,
                desktop=win.desktop,
                focused=win.focused,
                fx=win.x, fy=win.y, fw=win.w, fh=win.h,
            ))
        return out

    def windows(self) -> list[UWindow]:
        backend = self.backend()
        host = _hostname()
        out = None
        views_fn = getattr(backend, "views", None)
        if callable(views_fn):  # typed View records (GNOME bridge)
            views = views_fn()
            if views is not None:
                out = self._from_views(views, host)
                self._views_seen = True
                self._enrich(out)
                return out
        self._views_seen = False
        nodes_fn = getattr(backend, "_nodes", None)
        if nodes_fn is not None:  # sway/i3: raw tree carries the X plane
            # _nodes() is private to wdotool.backend_sway; its tuple shape
            # is a documented contract, but guard the unpacking so a shape
            # drift degrades to the generic listing, not a traceback.
            try:
                out = []
                for node, win, _floating, _ws in nodes_fn():
                    xid = node.get("window")
                    wp = node.get("window_properties") or {}
                    cls = _dot_class(wp.get("instance"), wp.get("class"))
                    if node.get("app_id"):
                        cls = "%s.%s" % (node["app_id"], node["app_id"])
                    out.append(UWindow(
                        id=xid if xid else win.id,
                        node_id=win.id,
                        is_x=bool(xid),
                        title=node.get("name"),
                        class_=cls,
                        machine=host,
                        pid=win.pid,
                        x=win.x, y=win.y, w=win.w, h=win.h,
                        desktop=win.desktop,
                        focused=win.focused,
                        fx=win.x, fy=win.y, fw=win.w, fh=win.h,
                    ))
            except (TypeError, ValueError, KeyError) as e:
                self.vprint("_nodes() has an unexpected shape (%s: %s); "
                            "using the generic backend listing\n"
                            % (type(e).__name__, e))
                out = None
        if out is None:
            out = []
            for win in backend.list():
                cls = "%s.%s" % (win.class_, win.class_) if win.class_ else None
                out.append(UWindow(
                    id=win.id, node_id=win.id, is_x=False,
                    title=win.title or None, class_=cls, machine=host,
                    pid=win.pid, x=win.x, y=win.y, w=win.w, h=win.h,
                    desktop=win.desktop, focused=win.focused,
                    fx=win.x, fy=win.y, fw=win.w, fh=win.h,
                ))
        self._enrich(out)
        return out

    def _enrich(self, wins: list[UWindow]):
        """Fill X-only fields for XWayland windows from the X server.

        Per-window failures (BadWindow on a window that died mid-listing)
        degrade field by field; a connection-level failure (XUnavailable —
        a hung or vanished XWayland) drops the X plane for the rest of the
        process, so the whole listing pays at most one timeout, not
        4 calls x N windows. Compositor data stands either way."""
        if not any(w.is_x for w in wins):
            return
        x = self.x11()
        if x is None:
            return
        for w in wins:
            if not w.is_x:
                continue
            try:
                self._enrich_one(x, w)
            except XUnavailable:
                try:
                    x.close()
                except Exception:
                    pass
                self._x11 = None
                return

    def _enrich_one(self, x, w: UWindow):
        """One window's X reads. XUnavailable propagates (connection dead);
        anything else degrades to the compositor value, field by field."""
        try:
            inst, cls = x.get_wm_class(w.id)
            if inst or cls:
                w.class_ = _dot_class(inst, cls)
        except XUnavailable:
            raise
        except Exception:
            pass
        try:
            machine = x.get_client_machine(w.id)
            if machine:
                w.machine = machine
        except XUnavailable:
            raise
        except Exception:
            pass
        try:
            w.x, w.y, w.w, w.h = x.get_geometry(w.id)
        except XUnavailable:
            raise
        except Exception:
            pass
        if w.pid <= 0:
            try:
                w.pid = x.get_pid(w.id)
            except XUnavailable:
                raise
            except Exception:
                pass

    # -- target selection (wmctrl <WIN> argument) ---------------------------

    def find_target(self, param_window: str, match_by_id: bool,
                    match_by_cls: bool, full_match: bool) -> UWindow | None:
        """Resolve wmctrl's <WIN>. Returns None for a silent exit-1 (wmctrl
        exits 1 without a message when no window matches). Raises CmdError
        with "Cannot convert argument to number." for a bad -i argument."""
        if match_by_id:
            wid = _parse_win_id(param_window)
            if wid is None:
                raise CmdError("Cannot convert argument to number.")
            wins = self.windows()
            # X-plane ids first (real X ids live at 0x00400000+ resource
            # bases, far above sway node ids — but be explicit anyway).
            for w in wins:
                if w.is_x and w.id == wid:
                    return w
            for w in wins:
                if w.node_id == wid or w.id == wid:
                    return w
            return None
        if param_window == SELECT_WINDOW_MAGIC:
            # real wmctrl shows a crosshair cursor; the closest we can do
            # is say what the blocking wait (next focus change) is for
            _warn("focus the target window to select it")
            node = self.backend().select_window()
            for w in self.windows():
                if w.node_id == node:
                    return w
            return None
        if param_window == ACTIVE_WINDOW_MAGIC:
            for w in self.windows():
                if w.focused:
                    return w
            return None
        needle = param_window if full_match else param_window.casefold()
        for w in self.windows():
            hay = w.class_ if match_by_cls else w.title
            if hay is None:
                continue
            if full_match:
                if hay == needle:
                    return w
            elif needle in hay.casefold():
                return w
        return None

    # -- actions ------------------------------------------------------------

    def activate(self, w: UWindow):
        self.backend().activate(w.node_id)

    def close(self, w: UWindow):
        self.backend().close(w.node_id)

    def to_desktop(self, w: UWindow, desktop: int) -> int:
        backend = self.backend()
        if desktop == -1:
            # -R / -t -1: the current desktop. sway can say this directly,
            # which also works when the focused workspace is named (no
            # number — get_desktop() would return -1 and the numeric route
            # would mis-file the window on a workspace called "0").
            run = getattr(backend, "run", None)
            if run is not None and getattr(backend, "_nodes", None):
                backend.window_desktop(w.node_id)  # gone -> CmdError, exit 1
                run("[con_id=%d] move container to workspace current"
                    % w.node_id)
                return 0
            desktop = backend.get_desktop()
        if desktop < 0:
            # negative desktops cannot exist; wmctrl would fire the request
            # into the void and exit 0 — warn instead of passing sway a
            # confusing off-by-one workspace number
            _warn("desktop %d does not exist; ignoring" % desktop)
            return 0
        backend.set_window_desktop(w.node_id, desktop)
        return 0

    def to_current_and_activate(self, w: UWindow) -> int:  # -R
        self.to_desktop(w, -1)
        if getattr(self.backend(), "_nodes", None) is None:
            # wmctrl sleeps to give an asynchronous WM time to move the
            # window; the sway IPC round-trip above is synchronous, so
            # only non-sway backends need the grace period
            time.sleep(0.1)
        self.activate(w)
        return 0

    def move_resize(self, w: UWindow, arg: str) -> int:  # -e
        argerr = ('The -e option expects a list of comma separated integers: '
                  '"gravity,X,Y,width,height"\n')
        if not arg:
            sys.stderr.write(argerr)
            return 1
        m = re.match(r"\s*([+-]?\d+),\s*([+-]?\d+),\s*([+-]?\d+)"
                     r",\s*([+-]?\d+),\s*([+-]?\d+)", arg)
        if not m:
            sys.stderr.write(argerr)
            return 1
        grav, x, y, ww, hh = (int(g) for g in m.groups())
        if grav < 0:
            sys.stderr.write("Value of gravity mustn't be negative. Use zero"
                             " to use the default gravity of the window.\n")
            return 1
        grflags = grav
        if x != -1:
            grflags |= 1 << 8
        if y != -1:
            grflags |= 1 << 9
        if ww != -1:
            grflags |= 1 << 10
        if hh != -1:
            grflags |= 1 << 11
        self.vprint("grflags: %d\n" % grflags)
        # The compositor is the WM: route the request through it with
        # _NET_MOVERESIZE_WINDOW's meaning. `W,H` are the CLIENT size and
        # the gravity names the point of the frame the request positions:
        # NorthWest its top-left, Center its centre, SouthEast its
        # bottom-right, Static the client's own top-left (0 = "the window's
        # own WM_SIZE_HINTS gravity", taken as NorthWest, the ICCCM default
        # and what toolkits set). A -1 keeps that point where it is, so a
        # bare resize under SouthEast pins the frame's bottom-right corner
        # and grows up and to the left -- Mutter's own behavior, verified
        # against real wmctrl. The frame extents (Mutter's server-side
        # titlebar on an XWayland window) turn the client rectangle into
        # the frame rectangle the bridge's Move/Resize address; where the
        # compositor manages the client rectangle itself (native windows,
        # sway's content rect, X plane not reached) they are zero and every
        # gravity but Static collapses to NorthWest. Requests the
        # compositor cannot honor (moving a tiled window, touching a
        # fullscreen one) are warned about and ignored, matching "the WM
        # may ignore the request".
        backend = self.backend()
        ext = self._measure_extents(w)
        if ext is None:
            _warn("window moved while measuring the frame; ignoring")
            return 0
        left, top, right, bottom = ext
        cw = ww if ww != -1 else w.fw - left - right
        ch = hh if hh != -1 else w.fh - top - bottom
        fw, fh = cw + left + right, ch + top + bottom
        col, row = _GRAVITY_CORNER.get(grav, (0, 0))
        static = grav == _GRAVITY_STATIC
        # A -1 keeps an axis, but what "keep" means to Mutter depends on
        # the request as a whole: with BOTH coordinates -1 (a bare resize)
        # it holds the gravity's reference point, so a SouthEast resize
        # grows up and to the left; with one coordinate given and the other
        # -1 it holds the unchanged frame edge instead. Anchoring both ways
        # put us up to 80 px from where real wmctrl leaves the window
        # (measured on GNOME 46, `9,-1,200,400,300`).
        keep_anchor = x == -1 and y == -1
        fx = _place_axis(col, static, x, left, w.fx, w.fw, cw, fw, keep_anchor)
        fy = _place_axis(row, static, y, top, w.fy, w.fh, ch, fh, keep_anchor)
        if ww != -1 or hh != -1:
            try:
                backend.resize(w.node_id, fw, fh)
            except CmdError as e:
                _warn("%s; ignoring" % e)
        # a move was asked for, or the gravity's anchor requires one
        if x != -1 or y != -1 or (fx, fy) != (w.fx, w.fy):
            try:
                backend.move_window(w.node_id, fx, fy)
            except CmdError as e:
                _warn("%s; ignoring" % e)
        return 0

    def _frame_extents(self, w: UWindow):
        """(left, top, right, bottom) between the compositor's frame rect
        and the X plane's client rect of an XWayland window listed by
        views(): Mutter's server-side titlebar and border. Zero when the
        two are the same rectangle (native windows, the sway tree's content
        rect, an X plane that was not reached); None when the client
        rectangle does not sit inside the frame at all -- either the window
        moved between the two reads, or the X coordinate space is scaled
        differently from the compositor's. _measure_extents tells those two
        apart; do not use this on its own to decide a geometry request."""
        if not (self._views_seen and w.is_x):
            return 0, 0, 0, 0
        return _extents_of((w.fx, w.fy, w.fw, w.fh), (w.x, w.y, w.w, w.h))

    def _sample_rects(self, w: UWindow, x):
        """One (frame rect, client rect) pair, read back to back from the
        compositor and from the X server. None when either read fails."""
        try:
            d = self.backend().find(w.node_id)
        except Exception:
            return None
        try:
            client = tuple(x.get_geometry(w.id))
        except Exception:
            return None
        return (d.x, d.y, d.w, d.h), client

    def _measure_extents(self, w: UWindow):
        """The frame extents a -e request may rely on, or None.

        The frame rectangle and the client rectangle come from two servers
        a round trip apart, so a window that is moving (a drag, an
        animation, another script) makes their difference meaningless --
        and it used to come out silently zero, which collapsed every
        gravity to NorthWest and resized the frame to the client size.
        Sample until two consecutive pairs agree: that means the window
        held still, and a client rectangle that still does not fit inside
        the frame is then a coordinate space we cannot subtract in (a
        scaled X plane), where zero extents are the honest answer. A window
        that never holds still yields None and the caller drops the
        request. w's rectangles are updated to the pair that agreed, so the
        arithmetic that follows describes one single instant."""
        if not (self._views_seen and w.is_x):
            return 0, 0, 0, 0
        x = self.x11()
        if x is None:
            return 0, 0, 0, 0
        prev = ((w.fx, w.fy, w.fw, w.fh), (w.x, w.y, w.w, w.h))
        for _ in range(_EXTENT_SAMPLES):
            cur = self._sample_rects(w, x)
            if cur is None:  # the window or a plane went away: no extents
                return 0, 0, 0, 0
            if cur == prev:
                frame, client = cur
                w.fx, w.fy, w.fw, w.fh = frame
                w.x, w.y, w.w, w.h = client
                return _extents_of(frame, client) or (0, 0, 0, 0)
            prev = cur
        return None

    def window_state(self, w: UWindow, arg: str) -> int:  # -b
        argerr = ('The -b option expects a list of comma separated parameters'
                  ': "(remove|add|toggle),<PROP1>[,<PROP2>]"\n')
        if not arg or "," not in arg:
            sys.stderr.write(argerr)
            return 1
        head, rest = arg.split(",", 1)
        if head == "remove":
            action = STATE_REMOVE
        elif head == "add":
            action = STATE_ADD
        elif head == "toggle":
            action = STATE_TOGGLE
        else:
            sys.stderr.write("Invalid action. Use either remove, add or "
                             "toggle.\n")
            return 1
        if "," in rest:
            p1, p2 = rest.split(",", 1)
            if not p2:
                sys.stderr.write("Invalid zero length property.\n")
                return 1
            self.vprint("State 2: _NET_WM_STATE_%s\n" % p2.upper())
        else:
            p1, p2 = rest, None
        if not p1:
            sys.stderr.write("Invalid zero length property.\n")
            return 1
        self.vprint("State 1: _NET_WM_STATE_%s\n" % p1.upper())
        # An XWayland window has a second route: Mutter is a full EWMH
        # window manager for the X plane and honours the _NET_WM_STATE
        # ClientMessage real wmctrl sends -- including for the states its
        # Wayland API cannot express (below, skip_taskbar, skip_pager).
        # The compositor stays the first choice: `hidden` really minimizes
        # through the bridge, where the X route is a no-op.
        skip = self._compositor_cannot_set() if w.is_x else frozenset()
        for prop in (p1, p2):
            if prop is None:
                continue
            name = prop.upper()
            if name in skip and self._x_set_state(w, name, action):
                continue
            try:
                self.backend().set_state(w.node_id, name, action)
            except CmdError as e:
                if self._x_set_state(w, name, action):
                    continue
                _warn("%s; ignoring" % e)
        return 0

    def _compositor_cannot_set(self):
        """_NET_WM_STATE names the compositor backend answers "not
        applied" to. The backend reports them (GNOME: the bridge's own
        gaps); one that does not say lets the CmdError path decide."""
        fn = self._backend_hook("unsupported_states")
        try:
            return frozenset(fn() or ()) if callable(fn) else frozenset()
        except Exception:
            return frozenset()

    def _x_set_state(self, w: UWindow, name: str, action: int) -> bool:
        """The EWMH _NET_WM_STATE ClientMessage, sent to the X root about
        an XWayland window -- byte for byte what real wmctrl does. False
        when there is no X window or no X plane to send it on."""
        if not w.is_x:
            return False
        x = self.x11() if self._x_is_up() else None
        if x is None:
            return False
        try:
            atom = x.atom("_NET_WM_STATE_%s" % name)
            x.send_root_message(w.id, "_NET_WM_STATE",
                                [action, atom, 0, 0, 0])
        except Exception as e:
            self.vprint("_NET_WM_STATE ClientMessage failed: %s\n" % e)
            return False
        return True

    def set_title(self, w: UWindow, title: str, mode: str) -> int:  # -N/-I/-T
        if not w.is_x:
            _warn("window titles cannot be set from outside on Wayland "
                  "(native window); ignoring")
            return 0
        x = self.x11()
        if x is None:
            _warn("cannot reach the XWayland server to set the window "
                  "title; ignoring")
            return 0
        try:
            x.set_name(w.id, title,
                       icon=mode in ("T", "I"), long_=mode in ("T", "N"))
        except Exception as e:
            _warn("cannot set the window title: %s; ignoring" % e)
        return 0

    # -- listings -----------------------------------------------------------

    def list_windows(self, show_pid: bool, show_geometry: bool,
                     show_class: bool) -> int:
        wins = self.windows()
        # The machine column is right-aligned to the LONGEST
        # WM_CLIENT_MACHINE. wmctrl 1.07 uses the LAST row's instead (a bug
        # in main.c), which is stable there only because its list is
        # _NET_CLIENT_LIST, i.e. creation order; ours is Mutter's stacking
        # order, so copying the quirk would re-flow the whole column every
        # time a window is raised. On any real session every row carries the
        # same hostname and the two rules print the same bytes.
        # Widths count BYTES like printf's %*s, not characters.
        machine_len = max([_blen(w.machine) for w in wins if w.machine] or [0])
        for w in wins:
            line = "0x%08x %2d" % (w.id, w.desktop)
            if show_pid:
                line += " %-6d" % (w.pid if w.pid > 0 else 0)
            if show_geometry:
                line += " %-4d %-4d %-4d %-4d" % (w.x, w.y, w.w, w.h)
            if show_class:
                cls = w.class_ if w.class_ is not None else "N/A"
                line += " %s%s " % (cls, " " * max(0, 20 - _blen(cls)))
            machine = w.machine or "N/A"
            line += " %s%s %s" % (" " * max(0, machine_len - _blen(machine)),
                                  machine,
                                  w.title if w.title is not None else "N/A")
            sys.stdout.write(line + "\n")
        return 0

    def list_current_desktop(self) -> int:  # -j (1.07+git)
        """wmctrl prints _NET_CURRENT_DESKTOP with "%-2d\n"."""
        sys.stdout.write("%-2d\n" % self.backend().get_desktop())
        return 0

    def _desktop_rows(self):
        """[(id, current?, dg, vp, wa, name)]. backend.workspaces() when the
        backend has it (GNOME: index, name, work area -- what wmctrl reads
        from _NET_DESKTOP_NAMES/_NET_WORKAREA); sway: one row per workspace
        (id = num-1, same mapping as wdotool's desktop commands); otherwise
        synthesized from the generic backend API."""
        backend = self.backend()
        size_fn = getattr(backend, "display_size", None)
        try:
            dg = "%dx%d" % size_fn() if size_fn else "N/A"
        except Exception:
            dg = "N/A"
        rows = []
        ws_fn = getattr(backend, "workspaces", None)
        typed = ws_fn() if callable(ws_fn) else None
        if typed is not None:
            # VP: like wmctrl under EWMH — a single _NET_DESKTOP_VIEWPORT
            # pair applies to the current desktop only, the others print
            # N/A. A nameless workspace prints its index.
            for ws in typed:
                wx, wy, ww, wh = ws.work_area
                rows.append((ws.index, ws.active, dg,
                             "0,0" if ws.active else "N/A",
                             "%d,%d %dx%d" % (wx, wy, ww, wh),
                             ws.name or "%d" % ws.index))
            return rows
        workspaces = None
        msg = getattr(backend, "_msg", None)
        if msg is not None:
            try:
                from wdotool.backend_sway import GET_WORKSPACES
                workspaces = msg(GET_WORKSPACES)
            except Exception:
                workspaces = None
        # VP: like wmctrl under EWMH — a single _NET_DESKTOP_VIEWPORT pair
        # applies to the current desktop only, the others print N/A.
        if workspaces is not None:
            for ws in workspaces:
                num = ws.get("num", -1)
                cur = bool(ws.get("focused"))
                rect = ws.get("rect") or {}
                wa = "%d,%d %dx%d" % (rect.get("x", 0), rect.get("y", 0),
                                      rect.get("width", 0),
                                      rect.get("height", 0))
                rows.append((num - 1 if num > 0 else -1,
                             cur, dg, "0,0" if cur else "N/A", wa,
                             ws.get("name") or "N/A"))
        else:
            cur = backend.get_desktop()
            for i in range(backend.num_desktops()):
                rows.append((i, i == cur, dg, "0,0" if i == cur else "N/A",
                             "N/A", "N/A"))
        return rows

    def list_desktops(self) -> int:
        rows = self._desktop_rows()
        dgw = max((len(r[2]) for r in rows), default=0)
        vpw = max((len(r[3]) for r in rows), default=0)
        waw = max((len(r[4]) for r in rows), default=0)
        for did, cur, dg, vp, wa, name in rows:
            sys.stdout.write("%-2d %s DG: %s  VP: %s  WA: %s  %s\n" % (
                did, "*" if cur else "-", dg.ljust(dgw), vp.ljust(vpw),
                wa.ljust(waw), name))
        return 0

    def wm_info(self) -> int:
        name = class_ = None
        pid = 0
        showing = None
        got_x = False
        # On GNOME the X plane is only opened when Xwayland is already
        # running (it is spawned on demand): the answer is the same either
        # way, Mutter's check window says "GNOME Shell" and the bridge's
        # wm_name says the same.
        x = self.x11() if self._x_is_up() else None
        if x is not None:
            try:
                sup = x.get_prop_ints(x.root(), "_NET_SUPPORTING_WM_CHECK")
                if not sup and self._views_seen:
                    # Xwayland just came up and the compositor has not
                    # finished its WM setup on the root yet: give it a
                    # moment rather than misreporting the compositor name
                    deadline = time.monotonic() + 2.0
                    while not sup and time.monotonic() < deadline:
                        time.sleep(0.05)
                        sup = x.get_prop_ints(x.root(),
                                              "_NET_SUPPORTING_WM_CHECK")
                if sup:
                    got_x = True
                    name = _xtry(lambda: x.get_prop_string(
                        sup[0], "_NET_WM_NAME")) or None
                    class_ = _xtry(lambda: x.get_prop_string(
                        sup[0], "WM_CLASS")) or None
                    pid = _xtry(lambda: x.get_pid(sup[0])) or 0
                    ints = _xtry(lambda: x.get_prop_ints(
                        x.root(), "_NET_SHOWING_DESKTOP"))
                    showing = ints[0] if ints else None
            except Exception:
                got_x = False
        if not got_x:
            # pure Wayland: the compositor IS the window manager; a backend
            # that knows what its WM calls itself (GNOME: the same string
            # Mutter puts on the X check window) says so
            backend = self.backend()
            name = getattr(backend, "wm_name", None) \
                or getattr(backend, "name", None)
        if name is None:
            self.vprint("Cannot get name of the window manager "
                        "(_NET_WM_NAME).\n")
        if class_ is None:
            self.vprint("Cannot get class of the window manager "
                        "(WM_CLASS).\n")
        if pid <= 0:
            self.vprint("Cannot get pid of the window manager "
                        "(_NET_WM_PID).\n")
        if showing is None:
            self.vprint("Cannot get the _NET_SHOWING_DESKTOP property.\n")
        sys.stdout.write("Name: %s\n" % (name if name else "N/A"))
        sys.stdout.write("Class: %s\n" % (class_ if class_ else "N/A"))
        sys.stdout.write("PID: %s\n" % (pid if pid > 0 else "N/A"))
        sys.stdout.write('Window manager\'s "showing the desktop" mode: '
                         "%s\n" % ("N/A" if showing is None
                                   else "ON" if showing == 1 else "OFF"))
        return 0

    # -- desktop-level actions ----------------------------------------------

    def switch_desktop(self, param: str) -> int:  # -s
        target = _atoi(param)
        if target < 0:
            # wmctrl only rejects exactly -1 (other negatives go into the
            # void, exit 0); negative desktops cannot exist here, so reject
            # them all with the same message instead of confusing sway
            sys.stderr.write("Invalid desktop ID.\n")
            return 1
        self.backend().set_desktop(target)
        return 0

    def showing_desktop(self, param: str) -> int:  # -k
        if param not in ("on", "off", "toggle"):
            sys.stderr.write('The argument to the -k option must be either '
                             '"on" or "off" or "toggle"\n')
            return 1
        if param == "toggle":
            param = "off" if self._showing_desktop() == 1 else "on"
        # Mutter's own show-desktop mode is reachable from the X plane:
        # _NET_SHOWING_DESKTOP on the X root is what real wmctrl sends, and
        # on GNOME it really works -- every window is hidden, `-k off`
        # brings them all back untouched, and `-m`, which reads the same
        # property, agrees. That is the mode; the bridge's
        # minimize-everything stand-in exists because the shell exports no
        # API for it, and is the fallback for a session with no X plane.
        if self._x_showing_desktop(param == "on"):
            return 0
        show_fn = self._backend_hook("show_desktop")
        if show_fn is None:
            _warn("Wayland compositors have no 'showing the desktop' mode; "
                  "ignoring")
            return 0
        try:
            show_fn(param == "on")
        except CmdError as e:
            _warn("%s; ignoring" % e)
        return 0

    def _x_showing_desktop(self, show: bool) -> bool:
        """_NET_SHOWING_DESKTOP to the X root, real wmctrl's own request.

        False when the X plane is not there to take it, and false when the
        window manager does not answer -- Mutter reports the mode by
        updating the root property, so a missing update means an Xwayland
        whose WM half is not up, and the caller falls back to the
        compositor's own stand-in rather than doing nothing at all."""
        x = self.x11() if self._x_is_up() else None
        if x is None:
            return False
        want = 1 if show else 0
        if self._showing_desktop() == want:
            return True          # already in that mode: nothing to ask for
        try:
            x.send_root_message(x.root(), "_NET_SHOWING_DESKTOP",
                                [want, 0, 0, 0, 0])
        except Exception as e:
            self.vprint("_NET_SHOWING_DESKTOP ClientMessage failed: %s\n" % e)
            return False
        deadline = time.monotonic() + 1.0
        while True:
            if self._showing_desktop() == want:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _showing_desktop(self):
        """The X root's _NET_SHOWING_DESKTOP (Mutter keeps it for the
        XWayland plane, and -k drives it there too), or None when there is
        no X plane to ask -- `-k toggle` then reads as "currently off",
        which is what real wmctrl does with an absent property, minus the
        NULL dereference."""
        x = self.x11() if self._x_is_up() else None
        if x is None:
            return None
        vals = _xtry(lambda: x.get_prop_ints(x.root(), "_NET_SHOWING_DESKTOP"))
        return vals[0] if vals else None

    def _backend_hook(self, name: str):
        """An optional backend method (GNOME: show_desktop,
        set_num_desktops), or None -- also when no backend can be found,
        so the warn-and-succeed fallbacks stay session-less."""
        try:
            return getattr(self.backend(), name, None)
        except CmdError:
            return None

    def change_viewport(self, param: str) -> int:  # -o
        if not _parse_two_uints(param):
            sys.stderr.write("The -o option expects two integers separated "
                             "with a comma.\n")
            return 1
        _warn("desktop viewports do not exist on Wayland; ignoring")
        return 0

    def change_geometry(self, param: str) -> int:  # -g
        if not _parse_two_uints(param):
            sys.stderr.write("The -g option expects two integers separated "
                             "with a comma.\n")
            return 1
        _warn("desktop geometry is managed by the compositor; ignoring")
        return 0

    def change_number_of_desktops(self, param: str) -> int:  # -n
        if re.match(r"\s*[+-]?\d+", param or "") is None:
            sys.stderr.write("The -n option expects an integer.\n")
            return 1
        set_fn = self._backend_hook("set_num_desktops")
        if set_fn is None:
            _warn("Wayland workspaces are managed by the compositor; ignoring")
            return 0
        try:
            set_fn(_atoi(param))
        except CmdError as e:
            # "The window manager may ignore the request" (GNOME's dynamic
            # workspaces do exactly that)
            _warn("%s; ignoring" % e)
        return 0

    # -- driver for window actions ------------------------------------------

    def action_window(self, mode: str, param_window: str, param: str | None,
                      match_by_id: bool, match_by_cls: bool,
                      full_match: bool) -> int:
        w = self.find_target(param_window, match_by_id, match_by_cls,
                             full_match)
        if w is None:
            return 1  # wmctrl exits 1 silently when nothing matches
        self.vprint("Using window: 0x%08x\n" % w.id)
        if mode == "a":
            self.activate(w)
            return 0
        if mode == "c":
            self.close(w)
            return 0
        if mode == "R":
            return self.to_current_and_activate(w)
        if mode == "t":
            return self.to_desktop(w, _atoi(param or ""))
        if mode == "e":
            return self.move_resize(w, param or "")
        if mode == "y":  # 1.07+git: -e, then activate
            rv = self.move_resize(w, param or "")
            self.activate(w)
            return rv
        if mode == "Y":  # 1.07+git: iconify (XIconifyWindow)
            self.backend().minimize(w.node_id)
            return 0
        if mode == "z":  # 1.07+git, undocumented: XLowerWindow
            self.backend().lower(w.node_id)
            return 0
        if mode == "E":  # 1.07+git, undocumented: print the title
            sys.stdout.write("%s\n" % (w.title if w.title is not None
                                        else ""))
            return 0
        if mode == "b":
            return self.window_state(w, param or "")
        if mode in ("N", "I", "T"):
            return self.set_title(w, param or "", mode)
        sys.stderr.write("Unknown action: '%s'\n" % mode)
        return 1


# -- small parsing/format helpers -------------------------------------------

def _hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def _dot_class(instance: str | None, class_: str | None) -> str | None:
    """WM_CLASS "inst\\0cls\\0" printed the wmctrl way: "inst.cls".

    A class of "" means "absent" (degenerate single-string WM_CLASS, where
    get_wm_class cannot express absence in its str return): print just
    "inst" with no trailing dot, like the wmctrl oracle. An empty INSTANCE
    is kept — b"\\0cls\\0" really prints ".cls"."""
    if class_ == "":
        class_ = None
    if instance is not None and class_ is not None:
        return "%s.%s" % (instance, class_)
    if class_ is not None:
        return class_
    if instance is not None:
        return instance
    return None


# ICCCM win_gravity (wmctrl -e's first field) as (column, row), with
# 0 = the left/top edge, 1 = the middle, 2 = the right/bottom edge: which
# point of the window the request positions. StaticGravity (10) addresses
# the client rather than the frame and is handled apart; 0 (use the
# window's own WM_SIZE_HINTS gravity) and unknown values are NorthWest.
_GRAVITY_CORNER = {1: (0, 0), 2: (1, 0), 3: (2, 0), 4: (0, 1), 5: (1, 1),
                   6: (2, 1), 7: (0, 2), 8: (1, 2), 9: (2, 2)}
_GRAVITY_STATIC = 10


# how many (frame rect, client rect) pairs -e samples while waiting for two
# consecutive ones to agree, i.e. for the window to hold still
_EXTENT_SAMPLES = 4


def _extents_of(frame, client):
    """(left, top, right, bottom) of `frame` around `client`, or None when
    the client rectangle does not sit inside the frame."""
    fx, fy, fw, fh = frame
    cx, cy, cw, ch = client
    left, top = cx - fx, cy - fy
    right, bottom = fw - cw - left, fh - ch - top
    if min(left, top, right, bottom) < 0:
        return None
    return left, top, right, bottom


def _anchor(pos: int, size: int) -> int:
    """Where the gravity's reference point sits inside a rectangle of
    `size`: its leading edge, its middle, or its trailing edge (Mutter's
    adjust_for_gravity arithmetic, halves truncated)."""
    if pos == 1:
        return size // 2
    if pos == 2:
        return size
    return 0


def _place_axis(pos: int, static: bool, req: int, lead: int,
                f_old: int, fs_old: int, cs_new: int, fs_new: int,
                keep_anchor: bool = True) -> int:
    """The frame's new leading edge on one axis.

    `pos` is the gravity's reference point (see _GRAVITY_CORNER), `static`
    marks StaticGravity, `req` the requested coordinate (-1 = keep), `lead`
    the leading frame extent (left or top), `f_old`/`fs_old` the frame's
    current edge and size, `cs_new`/`fs_new` the client and frame sizes the
    request asks for. A request positions the frame's reference point on
    the same point of the requested client rectangle.

    `keep_anchor` says what a -1 means, and it is a property of the whole
    request rather than of this axis: for `G,-1,-1,W,H` -- a bare resize --
    Mutter keeps the gravity's reference point, so SouthEast grows up and
    to the left; where the other axis carries a coordinate, it keeps this
    axis's frame edge instead. Both were measured against Mutter on GNOME
    46; the second case is why `9,-1,200,400,300` used to land 80 px from
    where real wmctrl leaves the window."""
    if static:                       # the client itself is addressed
        return f_old if req == -1 else req - lead
    if req == -1:
        if not keep_anchor:
            return f_old
        return f_old + _anchor(pos, fs_old) - _anchor(pos, fs_new)
    return req + _anchor(pos, cs_new) - _anchor(pos, fs_new)


def _parse_win_id(s: str) -> int | None:
    """wmctrl -i: sscanf "0x%lx" / "0X%lx" / "%lu" prefix semantics."""
    m = re.match(r"\s*0[xX]([0-9a-fA-F]+)", s or "")
    if m:
        return int(m.group(1), 16)
    m = re.match(r"\s*\+?(\d+)", s or "")
    if m:
        return int(m.group(1))
    return None


def _atoi(s: str) -> int:
    """C atoi(): leading whitespace + optional sign + digits, else 0."""
    m = re.match(r"[ \t\n\r\f\v]*([+-]?\d+)", s or "")
    return int(m.group(1)) if m else 0


def _parse_two_uints(s: str):
    """sscanf(s, "%lu,%lu") == 2 (%lu accepts a sign — strtoul wraps
    negatives, so "-1,-1" parses; the oracle exits 0 on it)"""
    m = re.match(r"\s*[+-]?(\d+),\s*[+-]?(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _blen(s: str) -> int:
    """printf column widths count bytes, not characters."""
    return len(s.encode("utf-8", "replace"))


def _xtry(fn):
    try:
        return fn()
    except Exception:
        return None
