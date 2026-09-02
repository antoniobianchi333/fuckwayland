"""KWin window backend: org.kde.KWin scripting over the session D-Bus, shelling
out to gdbus. Best-effort — KWin cannot run in this sandbox, so this is
untested against a live KWin; it targets both KWin 5 (workspace.clientList)
and KWin 6 (workspace.windowList).

Data flows back from KWin scripts via callDBus() to a marker interface that we
capture with dbus-monitor (KWin scripts have no other stdout)."""

import json
import os
import select
import subprocess
import tempfile
import time

from wdotool.backend import Window, WindowBackend
from wdotool.ctx import CmdError

_IFACE = "org.wdotool.kwin"

# Shared JS prolog: _l = all windows, _wid(w) = our numeric window id
# (first 48 bits of KWin's internalId uuid), _ret(x) = send result back.
_PROLOG = """\
function _wid(w) {
  return parseInt(String(w.internalId).replace(/[^0-9a-fA-F]/g, "").slice(0, 12), 16);
}
function _ret(x) {
  callDBus("org.wdotool", "/", "%s", "result", JSON.stringify(x));
}
var _l = (typeof workspace.windowList === "function")
         ? workspace.windowList() : workspace.clientList();
""" % _IFACE

_LIST_JS = """\
var out = [];
for (var i = 0; i < _l.length; i++) {
  var w = _l[i];
  var g = w.frameGeometry || w.geometry || {x: 0, y: 0, width: 0, height: 0};
  var d = -1;
  if (typeof w.desktop === "number") { d = w.desktop > 0 ? w.desktop - 1 : -1; }
  else if (w.desktops && w.desktops.length === 1
           && typeof w.desktops[0].x11DesktopNumber === "number") {
    d = w.desktops[0].x11DesktopNumber - 1;
  }
  out.push({id: _wid(w), title: String(w.caption || ""),
            cls: String(w.resourceClass || ""), pid: w.pid || 0,
            x: g.x | 0, y: g.y | 0, w: g.width | 0, h: g.height | 0,
            focused: !!w.active, visible: !w.minimized, desktop: d});
}
_ret(out);
"""


class KwinBackend(WindowBackend):
    name = "kwin"

    def __init__(self):
        from wdotool.backend_detect import dbus_env, dbus_name_has_owner
        self.env = dbus_env()
        if self.env is None:
            raise CmdError("kwin backend: no session D-Bus found")
        if not dbus_name_has_owner("org.kde.KWin"):
            raise CmdError("kwin backend: org.kde.KWin is not on the session bus")

    # -- plumbing -----------------------------------------------------------

    def _gdbus(self, dest, path, method, *args) -> str:
        argv = ["gdbus", "call", "--session", "--dest", dest,
                "--object-path", path, "--method", method, *args]
        try:
            r = subprocess.run(argv, env=self.env, capture_output=True,
                               text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CmdError("kwin backend: gdbus failed: %s" % e) from None
        if r.returncode != 0:
            raise CmdError(
                "kwin backend: %s failed: %s" % (method, r.stderr.strip())
            )
        return r.stdout.strip()

    def _run_script(self, body: str, collect: bool):
        """Load+run a KWin script; if collect, capture its _ret() payload."""
        fd, path = tempfile.mkstemp(prefix="wdotool-kwin-", suffix=".js")
        os.write(fd, (_PROLOG + body).encode())
        os.close(fd)
        mon = None
        script_name = "wdotool%d" % os.getpid()
        try:
            if collect:
                try:
                    mon = subprocess.Popen(
                        ["dbus-monitor", "--session",
                         "type='method_call',interface='%s'" % _IFACE],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        env=self.env, text=True,
                    )
                except OSError:
                    raise CmdError(
                        "kwin backend: dbus-monitor is required to read "
                        "results back from KWin scripts"
                    ) from None
                time.sleep(0.3)  # let the monitor attach before we run
            out = self._gdbus("org.kde.KWin", "/Scripting",
                             "org.kde.kwin.Scripting.loadScript",
                             path, script_name)
            digits = "".join(c for c in out if c.isdigit())
            if not digits:
                raise CmdError("kwin backend: loadScript returned %r" % out)
            sid = int(digits)
            last = None
            for objpath in ("/Scripting/Script%d" % sid, "/%d" % sid):
                try:
                    self._gdbus("org.kde.KWin", objpath,
                                "org.kde.kwin.Script.run")
                    break
                except CmdError as e:
                    last = e
            else:
                raise last
            result = self._read_monitor(mon) if collect else None
            for objpath in ("/Scripting/Script%d" % sid, "/%d" % sid):
                try:
                    self._gdbus("org.kde.KWin", objpath,
                                "org.kde.kwin.Script.stop")
                    break
                except CmdError:
                    pass
            try:
                self._gdbus("org.kde.KWin", "/Scripting",
                            "org.kde.kwin.Scripting.unloadScript", script_name)
            except CmdError:
                pass
            return result
        finally:
            if mon is not None:
                mon.kill()
                mon.wait()
            os.unlink(path)

    @staticmethod
    def _read_monitor(mon, timeout=5.0):
        """Scan dbus-monitor output for our method call's string argument."""
        deadline = time.monotonic() + timeout
        saw_call = False
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise CmdError("kwin backend: no reply from KWin script")
            r, _w, _x = select.select([mon.stdout], [], [], remain)
            if not r:
                continue
            line = mon.stdout.readline()
            if not line:
                raise CmdError("kwin backend: dbus-monitor exited early")
            if "interface=%s" % _IFACE in line and "member=result" in line:
                saw_call = True
            elif saw_call:
                s = line.strip()
                if s.startswith('string "'):
                    payload = s[len('string "'):]
                    if payload.endswith('"'):
                        payload = payload[:-1]
                    return payload.replace('\\"', '"').replace("\\\\", "\\")

    def _act(self, wid: int, js_action: str):
        """Run js_action with `w` bound to the window whose _wid == wid."""
        self._run_script(
            "for (var i = 0; i < _l.length; i++) {\n"
            "  var w = _l[i];\n"
            "  if (_wid(w) === %d) { %s }\n"
            "}\n" % (wid, js_action),
            collect=False,
        )

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        raw = self._run_script(_LIST_JS, collect=True)
        try:
            data = json.loads(raw)
        except ValueError:
            raise CmdError("kwin backend: bad script reply: %r" % raw) from None
        return [
            Window(id=d["id"], title=d["title"], class_=d["cls"],
                   pid=d["pid"], x=d["x"], y=d["y"], w=d["w"], h=d["h"],
                   focused=d["focused"], visible=d["visible"],
                   desktop=d["desktop"])
            for d in data
        ]

    def activate(self, wid: int):
        self._act(wid, 'if ("activeWindow" in workspace) '
                       "workspace.activeWindow = w; "
                       "else workspace.activeClient = w;")

    def close(self, wid: int):
        self._act(wid, "w.closeWindow();")

    def minimize(self, wid: int):
        self._act(wid, "w.minimized = true;")

    def map(self, wid: int):
        self._act(wid, "w.minimized = false;")

    def unmap(self, wid: int):
        self.minimize(wid)

    def move_window(self, wid: int, x: int, y: int):
        self._act(wid, "var g = w.frameGeometry; "
                       "w.frameGeometry = {x: %d, y: %d, "
                       "width: g.width, height: g.height};" % (x, y))

    def resize(self, wid: int, w: int, h: int):
        self._act(wid, "var g = w.frameGeometry; "
                       "w.frameGeometry = {x: g.x, y: g.y, "
                       "width: %d, height: %d};" % (w, h))

    def set_state(self, wid: int, state: str, action: int):
        expr = {0: "false", 1: "true"}.get(action)
        if state == "FULLSCREEN":
            self._act(wid, "w.fullScreen = %s;"
                      % (expr or "!w.fullScreen"))
        elif state == "HIDDEN":
            self._act(wid, "w.minimized = %s;"
                      % (expr or "!w.minimized"))
        elif state == "ABOVE":
            self._act(wid, "w.keepAbove = %s;"
                      % (expr or "!w.keepAbove"))
        elif state == "BELOW":
            self._act(wid, "w.keepBelow = %s;"
                      % (expr or "!w.keepBelow"))
        else:
            self._unsupported("windowstate %s" % state)

    def set_window_desktop(self, wid: int, n: int):
        self._act(wid,
                  'if (typeof w.desktop === "number") { w.desktop = %d; } '
                  "else { w.desktops = [workspace.desktops[%d]]; }"
                  % (n + 1, n))

    def get_desktop(self) -> int:
        raw = self._run_script(
            "var cd = workspace.currentDesktop;\n"
            'var n = (typeof cd === "number") ? cd\n'
            "        : (cd && cd.x11DesktopNumber ? cd.x11DesktopNumber : 1);\n"
            "_ret({d: n});\n",
            collect=True,
        )
        try:
            return int(json.loads(raw)["d"]) - 1
        except (ValueError, KeyError, TypeError):
            raise CmdError("kwin backend: bad script reply: %r" % raw) from None

    def set_desktop(self, n: int):
        self._run_script(
            'if (typeof workspace.currentDesktop === "number") '
            "{ workspace.currentDesktop = %d; }\n"
            "else if (%d < workspace.desktops.length) "
            "{ workspace.currentDesktop = workspace.desktops[%d]; }\n"
            % (n + 1, n, n),
            collect=False,
        )

    def num_desktops(self) -> int:
        raw = self._run_script(
            'var nd = (typeof workspace.desktops === "number")\n'
            "         ? workspace.desktops : workspace.desktops.length;\n"
            "_ret({n: nd});\n",
            collect=True,
        )
        try:
            return int(json.loads(raw)["n"])
        except (ValueError, KeyError, TypeError):
            raise CmdError("kwin backend: bad script reply: %r" % raw) from None

    def display_size(self) -> tuple[int, int]:
        raw = self._run_script(
            "var s = workspace.virtualScreenSize || "
            "{width: 1920, height: 1080};\n"
            "_ret({w: s.width, h: s.height});\n",
            collect=True,
        )
        try:
            d = json.loads(raw)
            return int(d["w"]), int(d["h"])
        except (ValueError, KeyError, TypeError):
            raise CmdError("kwin backend: bad script reply: %r" % raw) from None
