"""OWNER: Agent W. Unified window model + wmctrl semantics over the wdotool
compositor backend, with X11 enrichment for XWayland windows when available.

Dual-plane design per WWMCTL.md:
- the window LIST and all ACTIONS come from the compositor backend
  (wdotool.backend_detect.detect(); sway node ids address windows),
- XWayland windows are printed with their REAL X11 window id (sway exposes it
  as the node's "window" field) so xprop/real-wmctrl interoperate, and X-only
  data (WM_CLASS, WM_CLIENT_MACHINE, geometry) is read from the XWayland
  server via wwmctl.x11_mini when it is reachable,
- with no X server everything still works compositor-only (class from
  window_properties, machine falls back to the local hostname — XWayland
  clients are local by construction).

Output strings below are byte-parity copies of wmctrl 1.07 (main.c)."""

import dataclasses
import os
import re
import socket
import sys
import time

from wdotool import backend_detect
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


def _x11_connect():
    """An x11_mini.X11Conn to the XWayland server, or None. Never raises."""
    if os.environ.get("WWMCTL_NO_X"):
        return None
    try:
        from wwmctl import x11_mini
        return x11_mini.X11Conn()
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


class Core:
    def __init__(self, backend=None, verbose=False):
        self._backend = backend
        self._x11 = "unset"
        self.verbose = verbose

    def vprint(self, msg: str):
        if self.verbose:
            sys.stderr.write(msg)

    def backend(self):
        if self._backend is None:
            self._backend = _detect_backend()
        return self._backend

    def x11(self):
        if self._x11 == "unset":
            self._x11 = _x11_connect()
        return self._x11

    # -- unified window list ------------------------------------------------

    def windows(self) -> list[UWindow]:
        backend = self.backend()
        host = _hostname()
        out = None
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
        # The compositor is the WM: route the request through it. Gravity has
        # no compositor equivalent — coordinates address the content top-left
        # (gravity 0 behavior). Requests the compositor cannot honor (moving
        # a tiled window, touching a fullscreen one) are warned about and
        # ignored, matching "the WM may ignore the request".
        backend = self.backend()
        if ww != -1 or hh != -1:
            try:
                backend.resize(w.node_id,
                               ww if ww != -1 else w.w,
                               hh if hh != -1 else w.h)
            except CmdError as e:
                _warn("%s; ignoring" % e)
        if x != -1 or y != -1:
            try:
                backend.move_window(w.node_id,
                                    x if x != -1 else w.x,
                                    y if y != -1 else w.y)
            except CmdError as e:
                _warn("%s; ignoring" % e)
        return 0

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
        for prop in (p1, p2):
            if prop is None:
                continue
            try:
                self.backend().set_state(w.node_id, prop.upper(), action)
            except CmdError as e:
                _warn("%s; ignoring" % e)
        return 0

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
        # wmctrl 1.07 quirk kept for byte parity: the machine column width is
        # the length of the LAST window's WM_CLIENT_MACHINE, not the longest
        # (all-local XWayland/Wayland clients share one hostname anyway).
        # Widths count BYTES like printf's %*s, not characters.
        machine_len = 0
        for w in wins:
            if w.machine:
                machine_len = _blen(w.machine)
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

    def _desktop_rows(self):
        """[(id, current?, dg, vp, wa, name)]. sway: one row per workspace
        (id = num-1, same mapping as wdotool's desktop commands); otherwise
        synthesized from the generic backend API."""
        backend = self.backend()
        size_fn = getattr(backend, "display_size", None)
        try:
            dg = "%dx%d" % size_fn() if size_fn else "N/A"
        except Exception:
            dg = "N/A"
        rows = []
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
        x = self.x11()
        if x is not None:
            try:
                sup = x.get_prop_ints(x.root(), "_NET_SUPPORTING_WM_CHECK")
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
            # pure Wayland: the compositor IS the window manager
            name = getattr(self.backend(), "name", None)
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
        if param not in ("on", "off"):
            sys.stderr.write('The argument to the -k option must be either '
                             '"on" or "off"\n')
            return 1
        _warn("Wayland compositors have no 'showing the desktop' mode; "
              "ignoring")
        return 0

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
        _warn("Wayland workspaces are managed by the compositor; ignoring")
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
