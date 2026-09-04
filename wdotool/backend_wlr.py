"""wlr-foreign-toplevel window backend (zwlr_foreign_toplevel_management_v1)
via wayland_mini. Capability floor for wlroots compositors without i3-ipc:
list/activate/close/minimize/fullscreen only; no geometry, no pids, no
desktops. Window ids are 1000000 + arrival order and are only stable within
one wdotool process."""

import struct

from wdotool import session
from wdotool.backend import Window, WindowBackend
from wdotool.ctx import CmdError
from wdotool.wayland_mini import WlConn

BASE_ID = 1000000

# zwlr_foreign_toplevel_handle_v1 state enum
_ST_MAXIMIZED = 0
_ST_MINIMIZED = 1
_ST_ACTIVATED = 2
_ST_FULLSCREEN = 3

# handle requests
_REQ_SET_MAXIMIZED = 0
_REQ_UNSET_MAXIMIZED = 1
_REQ_SET_MINIMIZED = 2
_REQ_UNSET_MINIMIZED = 3
_REQ_ACTIVATE = 4
_REQ_CLOSE = 5


class _Toplevel:
    __slots__ = ("oid", "title", "app_id", "states", "closed")

    def __init__(self, oid):
        self.oid = oid
        self.title = ""
        self.app_id = ""
        self.states: set[int] = set()
        self.closed = False


class WlrBackend(WindowBackend):
    name = "wlr"

    def __init__(self):
        hit = session.find_wayland_socket()
        if not hit:
            raise CmdError("wlr backend: no Wayland socket found")
        _uid, _rd, sockpath = hit
        try:
            self.c = WlConn(sockpath)
        except OSError as e:
            raise CmdError(
                "wlr backend: cannot connect to %s: %s" % (sockpath, e)
            ) from None
        try:
            reg = self.c.get_registry()
            g = self.c.find_global("zwlr_foreign_toplevel_manager_v1")
        except (OSError, RuntimeError, struct.error) as e:
            raise CmdError("wlr backend: %s" % e) from None
        if not g:
            self.c.close()
            raise CmdError(
                "wlr backend: compositor does not offer "
                "zwlr_foreign_toplevel_management_unstable_v1"
            )
        self.tops: dict[int, _Toplevel] = {}  # handle oid -> record
        self.order: list[int] = []  # handle oids, arrival order
        self.mgr_ver = min(g[1], 3)
        self.mgr = self.c.bind(g[0], "zwlr_foreign_toplevel_manager_v1",
                               self.mgr_ver)
        self.c.on(self.mgr, self._on_mgr)

        self.seat = None
        sg = self.c.find_global("wl_seat")
        if sg:
            self.seat = self.c.bind(sg[0], "wl_seat", min(sg[1], 2))
            self.c.on(self.seat, lambda op, cur, fds: None)

        self.out_w = self.out_h = 0
        for name, (iface, ver) in list(reg.items()):
            if iface == "wl_output":
                oid = self.c.bind(name, "wl_output", min(ver, 2))
                self.c.on(oid, self._on_output)

        self._pump()  # toplevel announcements
        self._pump()  # each handle's initial title/app_id/state/done

    # -- events -------------------------------------------------------------

    def _on_mgr(self, op, cur, fds):
        if op == 0:  # toplevel(new_id)
            oid = cur.u32()
            t = _Toplevel(oid)
            self.tops[oid] = t
            self.order.append(oid)
            self.c.on(oid, lambda o, c, f, t=t: self._on_top(t, o, c))
        # op 1 = finished

    def _on_top(self, t: _Toplevel, op, cur):
        if op == 0:
            t.title = cur.string()
        elif op == 1:
            t.app_id = cur.string()
        elif op == 4:
            arr = cur.array()
            t.states = set(struct.unpack("<%dI" % (len(arr) // 4), arr))
        elif op == 6:
            t.closed = True
        # 2/3 output enter/leave, 5 done, 7 parent: ignored

    def _on_output(self, op, cur, fds):
        if op == 1:  # mode(flags, width, height, refresh)
            flags, w, h = cur.u32(), cur.i32(), cur.i32()
            if flags & 1:  # current mode
                self.out_w = max(self.out_w, w)
                self.out_h = max(self.out_h, h)

    def _pump(self):
        """One roundtrip, with the wire's failures turned into one clear line.

        A compositor that goes away mid-session (RuntimeError), or answers
        with an event whose payload is shorter than the interface says
        (struct.error), or whose socket errors or times out (OSError), is a
        routine thing for a session that is restarting -- not a traceback."""
        try:
            self.c.roundtrip()
        except CmdError:
            raise
        except (OSError, RuntimeError, struct.error) as e:
            raise CmdError("wlr backend: %s" % e) from None

    def _by_wid(self, wid: int) -> _Toplevel:
        self._pump()
        idx = wid - BASE_ID
        if 0 <= idx < len(self.order):
            t = self.tops[self.order[idx]]
            if not t.closed:
                return t
        raise CmdError("window %d not found" % wid)

    def _request(self, t: _Toplevel, opcode: int, args=()):
        try:
            self.c.send(t.oid, opcode, args)
        except OSError as e:
            raise CmdError("wlr backend: %s" % e) from None
        self._pump()

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        self._pump()
        wins = []
        for i, oid in enumerate(self.order):
            t = self.tops[oid]
            if t.closed:
                continue
            wins.append(Window(
                id=BASE_ID + i,
                title=t.title,
                class_=t.app_id,
                pid=0,
                x=0, y=0, w=self.out_w, h=self.out_h,  # geometry unknown
                focused=_ST_ACTIVATED in t.states,
                visible=_ST_MINIMIZED not in t.states,
                desktop=-1,
            ))
        return wins

    def activate(self, wid: int):
        t = self._by_wid(wid)
        if self.seat is None:
            raise CmdError("wlr backend: compositor offers no wl_seat; "
                           "cannot activate windows")
        self._request(t, _REQ_ACTIVATE, [("u", self.seat)])

    def close(self, wid: int):
        self._request(self._by_wid(wid), _REQ_CLOSE)

    def minimize(self, wid: int):
        self._request(self._by_wid(wid), _REQ_SET_MINIMIZED)

    def map(self, wid: int):
        self._request(self._by_wid(wid), _REQ_UNSET_MINIMIZED)

    def unmap(self, wid: int):
        self._request(self._by_wid(wid), _REQ_SET_MINIMIZED)

    def set_state(self, wid: int, state: str, action: int):
        t = self._by_wid(wid)
        if state == "FULLSCREEN":
            if self.mgr_ver < 2:
                self._unsupported("windowstate FULLSCREEN")
            on = action == 1 or (action == 2 and _ST_FULLSCREEN not in t.states)
            if on:
                self._request(t, 8, [("u", 0)])  # set_fullscreen(null output)
            else:
                self._request(t, 9)  # unset_fullscreen
        elif state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            # the protocol only has all-or-nothing maximize
            on = action == 1 or (action == 2 and _ST_MAXIMIZED not in t.states)
            self._request(t, _REQ_SET_MAXIMIZED if on else _REQ_UNSET_MAXIMIZED)
        elif state == "HIDDEN":
            on = action == 1 or (action == 2 and _ST_MINIMIZED not in t.states)
            self._request(t, _REQ_SET_MINIMIZED if on else _REQ_UNSET_MINIMIZED)
        else:
            self._unsupported("windowstate %s" % state)

    def get_desktop(self) -> int:
        self._unsupported("get_desktop")

    def set_desktop(self, n: int):
        self._unsupported("set_desktop")

    def num_desktops(self) -> int:
        self._unsupported("get_num_desktops")

    def display_size(self) -> tuple[int, int]:
        if not self.out_w or not self.out_h:
            raise CmdError("wlr backend: no wl_output mode seen")
        return self.out_w, self.out_h
