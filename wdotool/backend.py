"""Window-management backend interface. FROZEN — edit only if broken.

Additive extension (gnome-bridge): the View/Workspace dataclasses and the
optional hooks at the end of WindowBackend (views, workspaces, x_info,
events). They let a backend that knows more than Window carries
(X ids of XWayland windows, WM_CLASS instance/class, workspace names) hand it
to wwmctl/wxprop/getmouselocation without those tools reaching into backend
privates (sway's `_nodes()` tuple). Every hook defaults to "not available";
callers fall back to list()/find()."""

import dataclasses
import os
import signal
import sys

from wdotool.ctx import CmdError

#: The name the backends put in front of their own warnings. wwmctl and
#: wxprop drive these same backends, and a line reading "wdotool: ..." in
#: the middle of a `wmctrl -b` run names a tool the user did not run. Each
#: CLI's main() sets it -- wxprop's to whatever argv[0] says, like the
#: original it replaces -- so one process running two of them in turn (the
#: in-process `main([...])` callers) still gets one name per run.
_PROGRAM = "wdotool"


def set_program(name) -> None:
    """Name the running tool. Called once, first thing, by each main()."""
    global _PROGRAM
    _PROGRAM = str(name) if name else "wdotool"


def program() -> str:
    return _PROGRAM


def warn(msg: str) -> None:
    """One warning line on stderr, in the running tool's name."""
    sys.stderr.write("%s: %s\n" % (_PROGRAM, msg))


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
    # Mutter window-type name (NORMAL DESKTOP DOCK DIALOG ...); the backends
    # that know it fill it in, the rest leave it NORMAL. hit_test() is the
    # only reader: it is what lets one rule look through the desktop and dock
    # layers on every backend that can tell them apart.
    window_type: str = "NORMAL"


#: Window types the pointer hit-test looks through: the desktop-icon layer
#: and docks/panels, which a click on X11 falls straight through.
_LAYER_TYPES = {"DESKTOP", "DOCK"}


def hit_test(wins: "list[Window]", x: int, y: int) -> int:
    """The window under (x, y) in a list() result, 0 for none.

    One rule for every backend, so getmouselocation and the backends cannot
    drift apart: DESKTOP and DOCK layers are looked through, invisible
    windows (minimized, or on another workspace) are never hits, the focused
    window wins among the rest, and otherwise the topmost -- list() is
    stacking order bottom to top, so that is the last hit.

    Client-side on purpose. Neither the GNOME bridge nor KWin exports a
    hit-test, and KWin 6's workspace.windowAt() would answer for one Plasma
    release only."""
    hits = [w for w in wins
            if w.visible and w.window_type not in _LAYER_TYPES
            and w.w > 0 and w.h > 0
            and w.x <= x < w.x + w.w and w.y <= y < w.y + w.h]
    if not hits:
        return 0
    for w in hits:
        if w.focused:
            return w.id
    return hits[-1].id


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
    below: bool = False
    sticky: bool = False
    urgent: bool = False
    minimized: bool = False
    hidden: bool = False
    skip_taskbar: bool = False
    skip_pager: bool = False
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
        # `unsupported` marks a capability gap (as opposed to a failed
        # operation) so callers can downgrade it to a warning -- see
        # set_num_desktops, which must not fail a chain on a compositor with
        # a fixed workspace count.
        err = CmdError(f"{op} is not supported by the {self.name} backend")
        err.unsupported = True
        raise err

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

    def set_state(self, wid: int, state: str, action: int) -> "str | None":
        """state: uppercase _NET_WM_STATE suffix (e.g. "FULLSCREEN");
        action: 0=remove 1=add 2=toggle.

        Returns None when the state applied, or when the backend cannot
        tell. Returns a one-line reason when the compositor ACCEPTED the
        request and did not apply it -- which KWin does for a window rule,
        for size hints a fullscreen cannot satisfy, and for SHADED on
        anything but an X11 window. Raise a CmdError only for a request the
        backend could not make at all.

        The caller decides what to do with a reason: wdotool prints it and
        succeeds (the X tools cannot tell either), wwmctl first tries the
        EWMH ClientMessage, which reaches an XWayland window through the X
        server the compositor's own API just refused."""
        self._unsupported("windowstate")

    def set_num_desktops(self, n: int):
        """Ask the compositor for exactly n workspaces (set_num_desktops).
        Raises a CmdError with .unsupported set where the count is not the
        caller's to choose (dynamic workspaces)."""
        self._unsupported("set_num_desktops")

    def window_desktop(self, wid: int) -> int:
        return self.find(wid).desktop

    def set_window_desktop(self, wid: int, n: int):
        self._unsupported("set_desktop_for_window")

    # What to tell the user while an interactive selection is pending. The
    # backends that implement select_window() properly want a click (GNOME's
    # bridge grab, KWin's own picker); the sway backend, which can only wait
    # for a focus change, says so instead -- callers print this rather than
    # guess, because the two are opposite instructions.
    select_window_hint = "click the target window to select it"

    def select_window(self) -> int:
        """Interactively pick a window (selectwindow); return its id.

        xdotool grabs the pointer and answers with the window under it at the
        next button press. GNOME (bridge grab) and KDE (KWin's own picker) do
        exactly that; sway/i3 have no picker and no pointer query in their
        IPC, so that backend still waits for the next focus change and says
        so. Cancelling (Escape, or the picker's own timeout) raises
        CmdError -- rc 1, never a made-up window."""
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

    def events(self, timeout: float | None = None):
        """Iterator of (window_id, change) with sway's vocabulary (new, close,
        focus, title, fullscreen_mode, move, urgent, workspace); stops after
        `timeout` seconds of silence (None = never)."""
        self._unsupported("window events")
