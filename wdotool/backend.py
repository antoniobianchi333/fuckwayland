"""Window-management backend interface. FROZEN — edit only if broken."""

import dataclasses
import os
import signal

from wdotool.ctx import CmdError


@dataclasses.dataclass
class Window:
    id: int = 0
    title: str = ""
    class_: str = ""  # app_id on Wayland; doubles as WM_CLASS class and instance
    pid: int = 0
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    focused: bool = False
    visible: bool = True
    desktop: int = -1  # 0-based workspace index, -1 unknown/sticky


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
