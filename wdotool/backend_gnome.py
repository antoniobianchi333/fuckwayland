"""GNOME Shell window backend via gdbus. Primary path: org.gnome.Shell.Eval
(works only in unsafe mode / older shells). Fallback: the Window Calls
extension's org.gnome.Shell.Extensions.Windows interface (reduced feature
set). Best-effort — GNOME cannot run in this sandbox, so untested live.

Window ids are mutter's stable sequence numbers (Window Calls uses the same),
stable for the life of the shell."""

import json
import re
import subprocess

from wdotool.backend import Window, WindowBackend
from wdotool.ctx import CmdError

_HINT = ("gnome backend: org.gnome.Shell.Eval is blocked (unsafe mode "
         "disabled) and the Window Calls extension is not installed; "
         "install one of them")

_EXT_PATH = "/org/gnome/Shell/Extensions/Windows"
_EXT_IFACE = "org.gnome.Shell.Extensions.Windows"

# JS: `_w(ID)` finds a meta window by stable sequence.
_FIND = ("const _w = id => global.get_window_actors()"
         ".map(a => a.meta_window).find(w => w.get_stable_sequence() === id);")

_LIST_JS = """\
JSON.stringify(global.get_window_actors().map(a => {
  const w = a.meta_window;
  const r = w.get_frame_rect();
  const ws = w.get_workspace();
  return {id: w.get_stable_sequence(), title: w.get_title() || "",
          cls: w.get_wm_class() || "", pid: w.get_pid(),
          x: r.x, y: r.y, w: r.width, h: r.height,
          focused: w.has_focus(), visible: !w.minimized,
          desktop: w.is_on_all_workspaces() ? -1 : (ws ? ws.index() : -1)};
}))
"""


class _EvalBlocked(Exception):
    pass


def _unquote(s: str) -> str:
    """Undo GVariant single-quoted string escaping (the common cases)."""
    return (s.replace("\\'", "'").replace('\\"', '"')
             .replace("\\n", "\n").replace("\\t", "\t")
             .replace("\\\\", "\\"))


class GnomeBackend(WindowBackend):
    name = "gnome"

    def __init__(self):
        from wdotool.backend_detect import dbus_env, dbus_name_has_owner
        self.env = dbus_env()
        if self.env is None:
            raise CmdError("gnome backend: no session D-Bus found")
        if not dbus_name_has_owner("org.gnome.Shell"):
            raise CmdError(
                "gnome backend: org.gnome.Shell is not on the session bus"
            )
        self._eval_ok: bool | None = None  # tri-state: unknown/yes/no

    # -- plumbing -----------------------------------------------------------

    def _gdbus(self, path, method, *args) -> str:
        argv = ["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                "--object-path", path, "--method", method, *args]
        try:
            r = subprocess.run(argv, env=self.env, capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CmdError("gnome backend: gdbus failed: %s" % e) from None
        if r.returncode != 0:
            raise CmdError(
                "gnome backend: %s failed: %s" % (method, r.stderr.strip())
            )
        return r.stdout.strip()

    def _eval(self, js: str) -> str:
        """Run JS in the shell; returns the eval result string (often JSON)."""
        if self._eval_ok is False:
            raise _EvalBlocked()
        out = self._gdbus("/org/gnome/Shell", "org.gnome.Shell.Eval", js)
        m = re.match(r"\((true|false),\s*'(.*)'\)\s*$", out, re.S)
        if not m:
            raise CmdError("gnome backend: unexpected Eval reply: %r" % out)
        if m.group(1) != "true":
            self._eval_ok = False
            raise _EvalBlocked()
        self._eval_ok = True
        return _unquote(m.group(2))

    def _eval_json(self, js: str):
        raw = self._eval(js)
        if not raw:
            return None
        val = json.loads(raw)
        if isinstance(val, str):  # our JS often JSON.stringifys itself
            val = json.loads(val)
        return val

    def _ext(self, method, *args) -> str:
        return self._gdbus(_EXT_PATH, "%s.%s" % (_EXT_IFACE, method), *args)

    def _ext_str(self, method, *args) -> str:
        """Extension methods return ('result',) — extract the string."""
        out = self._ext(method, *args)
        m = re.match(r"\('(.*)',\)\s*$", out, re.S)
        return _unquote(m.group(1)) if m else ""

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        try:
            data = self._eval_json(_LIST_JS)
            return [
                Window(id=d["id"], title=d["title"], class_=d["cls"],
                       pid=d["pid"], x=d["x"], y=d["y"], w=d["w"], h=d["h"],
                       focused=d["focused"], visible=d["visible"],
                       desktop=d["desktop"])
                for d in data or []
            ]
        except _EvalBlocked:
            pass
        try:
            data = json.loads(self._ext_str("List"))
        except (CmdError, ValueError):
            raise CmdError(_HINT) from None
        wins = []
        for d in data:
            wid = d.get("id", 0)
            title = ""
            try:
                title = self._ext_str("GetTitle", str(wid))
            except CmdError:
                pass
            wins.append(Window(
                id=wid, title=title, class_=d.get("wm_class") or "",
                pid=d.get("pid") or 0,
                x=d.get("x", 0), y=d.get("y", 0),
                w=d.get("width", 0), h=d.get("height", 0),
                focused=bool(d.get("focus")),
                visible=bool(d.get("in_current_workspace", True)),
                desktop=-1,
            ))
        return wins

    def _act(self, wid: int, js: str, ext=None):
        """Eval `js` with _w(ID); fall back to a Window Calls method."""
        try:
            self._eval_json("(() => {%s const w = _w(%d); if (w) { %s } })()"
                            % (_FIND, wid, js))
            return
        except _EvalBlocked:
            if ext is None:
                raise CmdError(_HINT) from None
        method, args = ext
        self._ext(method, str(wid), *[str(a) for a in args])

    def activate(self, wid: int):
        self._act(wid, "w.activate(global.get_current_time());",
                  ext=("Activate", ()))

    def close(self, wid: int):
        self._act(wid, "w.delete(global.get_current_time());", ext=("Close", ()))

    def minimize(self, wid: int):
        self._act(wid, "w.minimize();", ext=("Minimize", ()))

    def map(self, wid: int):
        self._act(wid, "w.unminimize();", ext=("Unminimize", ()))

    def unmap(self, wid: int):
        self.minimize(wid)

    def move_window(self, wid: int, x: int, y: int):
        self._act(wid, "w.move_frame(true, %d, %d);" % (x, y),
                  ext=("Move", (x, y)))

    def resize(self, wid: int, w: int, h: int):
        self._act(wid,
                  "const r = w.get_frame_rect(); "
                  "w.move_resize_frame(true, r.x, r.y, %d, %d);" % (w, h),
                  ext=("Resize", (w, h)))

    def set_state(self, wid: int, state: str, action: int):
        if state == "FULLSCREEN":
            js = {0: "w.unmake_fullscreen();", 1: "w.make_fullscreen();",
                  2: "w.is_fullscreen() ? w.unmake_fullscreen() "
                     ": w.make_fullscreen();"}[action]
        elif state in ("MAXIMIZED_VERT", "MAXIMIZED_HORZ"):
            js = {0: "w.unmaximize(3);", 1: "w.maximize(3);",
                  2: "w.get_maximized() ? w.unmaximize(3) : w.maximize(3);"}[action]
        elif state == "HIDDEN":
            js = {0: "w.unminimize();", 1: "w.minimize();",
                  2: "w.minimized ? w.unminimize() : w.minimize();"}[action]
        elif state == "ABOVE":
            js = {0: "w.unmake_above();", 1: "w.make_above();",
                  2: "w.is_above() ? w.unmake_above() : w.make_above();"}[action]
        elif state == "STICKY":
            js = {0: "w.unstick();", 1: "w.stick();",
                  2: "w.on_all_workspaces ? w.unstick() : w.stick();"}[action]
        else:
            self._unsupported("windowstate %s" % state)
            return
        self._act(wid, js)

    def set_window_desktop(self, wid: int, n: int):
        self._act(wid, "w.change_workspace_by_index(%d, false);" % n)

    def get_desktop(self) -> int:
        try:
            return int(self._eval_json(
                "global.workspace_manager.get_active_workspace_index()"))
        except _EvalBlocked:
            raise CmdError(_HINT) from None

    def set_desktop(self, n: int):
        try:
            self._eval_json(
                "global.workspace_manager.get_workspace_by_index(%d)"
                ".activate(global.get_current_time())" % n)
        except _EvalBlocked:
            raise CmdError(_HINT) from None

    def num_desktops(self) -> int:
        try:
            return int(self._eval_json(
                "global.workspace_manager.get_n_workspaces()"))
        except _EvalBlocked:
            raise CmdError(_HINT) from None

    def display_size(self) -> tuple[int, int]:
        try:
            d = self._eval_json(
                "JSON.stringify({s: global.display.get_size()})")
            return int(d["s"][0]), int(d["s"][1])
        except _EvalBlocked:
            raise CmdError(_HINT) from None
