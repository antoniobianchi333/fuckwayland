"""Window-management backend interface. FROZEN — edit only if broken.

Additive extension (gnome-bridge): the View/Workspace dataclasses and the
optional hooks at the end of WindowBackend (views, workspaces, x_info,
window_at, events). They let a backend that knows more than Window carries
(X ids of XWayland windows, WM_CLASS instance/class, workspace names) hand it
to wwmctl/wxprop/getmouselocation without those tools reaching into backend
privates (sway's `_nodes()` tuple). Every hook defaults to "not available";
callers fall back to list()/find()."""

import dataclasses
import os
import signal

from wdotool.ctx import CmdError


@dataclasses.dataclass
class Window:
    id: int = 0
    title: str = ""
    class_: str = ""  # app_id on Wayland; the WM_CLASS *class* for X clients
    # WM_CLASS *instance* of an X/XWayland client ("" when the backend cannot
    # tell it apart from class_); `search --classname` matches this, falling
    # back to class_ so native Wayland toplevels still match their app_id.
    instance: str = ""
    pid: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    focused: bool = False
    visible: bool = True
    desktop: int = -1  # 0-based workspace index, -1 unknown/sticky


@dataclasses.dataclass
class View:
    """One toplevel with everything a wmctrl/xprop clone wants to print.
    `window` is the plain Window; the rest is extra. `xid` is 0 for native
    Wayland toplevels; `instance`/`cls` are the WM_CLASS pair (app_id twice
    when the compositor has no WM_CLASS); `app_id` is the Wayland app id /
    GTK application id ("" for pure X11 clients)."""

    window: Window
    xid: int = 0
    instance: str = ""
    cls: str = ""
    app_id: str = ""
    fullscreen: bool = False
    maximized_h: bool = False
    maximized_v: bool = False
    above: bool = False
    sticky: bool = False
    urgent: bool = False
    minimized: bool = False
    hidden: bool = False
    skip_taskbar: bool = False
    floating: bool = True  # tiling compositors only; GNOME windows all float
    ws_name: str = ""
    window_type: str = "NORMAL"
    client_type: str = "wayland"  # "wayland" | "x11"
    role: str = ""
    desktop_id: str = ""  # .desktop file id, "" when unknown
    monitor: int = -1
    transient_for: int = 0
    decorated: bool = True


@dataclasses.dataclass
class Workspace:
    index: int
    name: str = ""
    active: bool = False
    work_area: tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h


class WindowBackend:
    name = "none"

    def _unsupported(self, op: str):
        raise CmdError(f"{op} is not supported by the {self.name} backend")

    # required
    def list(self) -> list[Window]:
        raise NotImplementedError

    def activate(self, wid: int):
        raise NotImplementedError

    def close(self, wid: int):
        raise NotImplementedError

    def get_desktop(self) -> int:
        raise NotImplementedError

    def set_desktop(self, n: int):
        raise NotImplementedError

    def num_desktops(self) -> int:
        raise NotImplementedError

    # optional, with defaults
    def focus(self, wid: int):
        self.activate(wid)

    def find(self, wid: int) -> Window:
        for w in self.list():
            if w.id == wid:
                return w
        raise CmdError(f"window {wid} not found")

    def kill(self, wid: int):
        pid = self.find(wid).pid
        if pid <= 0:
            raise CmdError(f"no pid for window {wid}")
        os.kill(pid, signal.SIGKILL)

    def move_window(self, wid: int, x: int, y: int):
        self._unsupported("windowmove")

    def resize(self, wid: int, w: int, h: int):
        self._unsupported("windowsize")

    def minimize(self, wid: int):
        self._unsupported("windowminimize")

    def map(self, wid: int):
        self._unsupported("windowmap")

    def unmap(self, wid: int):
        self._unsupported("windowunmap")

    def raise_(self, wid: int):
        self._unsupported("windowraise")

    def lower(self, wid: int):
        self._unsupported("windowlower")

    def set_state(self, wid: int, state: str, action: int):
        """state: uppercase _NET_WM_STATE suffix (e.g. "FULLSCREEN");
        action: 0=remove 1=add 2=toggle"""
        self._unsupported("windowstate")

    def window_desktop(self, wid: int) -> int:
        return self.find(wid).desktop

    def set_window_desktop(self, wid: int, n: int):
        self._unsupported("set_desktop_for_window")

    def select_window(self) -> int:
        """Block until the user focuses a window; return it (selectwindow)."""
        self._unsupported("selectwindow")

    # optional richer views (additive, see the module docstring)
    def views(self) -> "list[View] | None":
        """list() with the View extras, or None when the backend has no
        richer view than Window (callers then synthesize from list())."""
        return None

    def workspaces(self) -> "list[Workspace] | None":
        """Named workspaces with work areas, or None (callers synthesize from
        get_desktop()/num_desktops())."""
        return None

    def x_info(self) -> tuple[str, str] | None:
        """(DISPLAY, XAUTHORITY) of the session's Xwayland, or None when the
        backend cannot tell (callers fall back to session.find_x_display /
        find_xauthority)."""
        return None

    def pointer(self) -> tuple[int, int] | None:
        """The compositor's real pointer position in global layout
        coordinates, or None when the compositor offers no pointer query
        (sway's IPC does not). Callers fall back to the input daemon's
        model of the last position it injected."""
        return None

    def window_at(self, x: int, y: int) -> int | None:
        """Backend-native pointer hit-test: the topmost window under the
        point, skipping desktop/dock layers, 0 for none; None means "use the
        generic hit-test over list()"."""
        return None

    def events(self, timeout: float | None = None):
        """Iterator of (window_id, change) with sway's vocabulary (new, close,
        focus, title, fullscreen_mode, move, urgent, workspace); stops after
        `timeout` seconds of silence (None = never)."""
        self._unsupported("window events")
