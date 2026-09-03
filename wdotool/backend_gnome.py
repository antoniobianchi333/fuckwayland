"""GNOME Shell (Mutter) window backend over the fuckwayland bridge extension.

GNOME has no window-management protocol and gnome-shell's own D-Bus surface
is either read-only and sender-allowlisted (org.gnome.Shell.Introspect) or
off by default (org.gnome.Shell.Eval outside "unsafe mode"). The only
supported way in is code running inside the shell: gnome/fuckwayland-bridge@
fuckwayland (see gnome/README.md) exports Mutter's window/workspace/monitor
facts and actions on the session bus as

    name  org.fuckwayland.Bridge   path /org/fuckwayland/Bridge
    iface org.fuckwayland.Bridge1  (JSON strings for structured results)

and this module is a thin client for it over dbus_mini (pure stdlib, one
connection per process, no gdbus spawns). Window ids are Meta.Window.get_id()
(64-bit, stable for the shell's lifetime -- the same numbers
org.gnome.Shell.Introspect uses), printed in decimal like every backend.

Field mapping (bridge object -> Window):
  class_   wm_class, else gtk_app_id, else sandboxed_app_id (Mutter reports
           the Wayland app_id as wm_class for native clients)
  instance wm_class_instance -- the WM_CLASS *instance* of an XWayland client
           (`xterm -name myinst` -> "myinst"), "" for native toplevels, where
           `search --classname` then falls back to class_ = the app_id
  x,y,w,h  get_frame_rect(): logical pixels, SSD frame included, no CSD
           shadow -- the same space the input daemon's pointer lives in
  focused  has_focus()
  visible  not hidden (minimized / show-desktop) AND on the active workspace
           -- X11's IsViewable; `is_mapped` is the looser "not minimized" so
           windowmap --sync does not hang on windows parked elsewhere
  desktop  workspace index, -1 when on all workspaces (sticky)

list() order is Mutter's stacking order bottom->top, so the generic hit-test
picks hits[-1] = topmost; window_at() additionally skips DESKTOP/DOCK layers
(desktop icons, docks) like a click-through X11 root window would.

When the bridge name is missing but org.gnome.Shell is on the bus, the
constructor fails with a diagnosis: screen locked, extension disabled or
broken, or the one-line install hint (_HINT). Only with WDOTOOL_GNOME_AUTOLOAD
set (to anything but 0) does it first try to load the installed extension
through org.gnome.Shell.Eval, which works only while the shell is in unsafe
mode; the default path -- the common "installed, needs a re-login" case --
never probes that privileged interface."""

import json
import os
import sys
import time

from wdotool import session
from wdotool.backend import View, Window, WindowBackend, Workspace
from wdotool.ctx import CmdError
from wdotool.dbus_mini import ERR, Bus, DBusError

BUS_NAME = "org.fuckwayland.Bridge"
OBJECT_PATH = "/org/fuckwayland/Bridge"
IFACE = "org.fuckwayland.Bridge1"
SHELL_NAME = "org.gnome.Shell"
SCREENSAVER_NAME = "org.gnome.ScreenSaver"
EXT_UUID = "fuckwayland-bridge@fuckwayland"

_HINT = ("gnome backend: the fuckwayland bridge extension is not running in "
         "GNOME Shell; run gnome/install-bridge.sh and restart the session "
         "(log out and back in)")
_GONE = ("gnome backend: the fuckwayland bridge vanished from the session bus "
         "(extension disabled, screen locked, or shell restarting); run "
         "gnome/install-bridge.sh --check")

CALL_TIMEOUT = 10.0     # every bridge call answers in milliseconds
AUTOLOAD_WAIT = 3.0     # after a successful Eval(loadExtension)

# xdotool action -> bridge SetState action word
_ACTIONS = {0: "remove", 1: "add", 2: "toggle"}
# _NET_WM_STATE atoms Mutter has no setter for and that change nothing a
# script can observe through these tools: warn + succeed (DESIGN.md cosmetic
# rule). SHADED is not one of them: shading is a visible operation a script
# may rely on, and Mutter does not implement it at all -- a real capability
# gap, like BELOW. Everything else the bridge cannot apply is a gap too.
_COSMETIC_STATES = {"SKIP_TASKBAR", "SKIP_PAGER", "MODAL"}
_GAP_REASONS = {"SHADED": "Mutter does not implement window shading",
                "BELOW": "Mutter has no API for it"}
# Opt-in for the Eval-based auto-load of an installed extension (__init__).
AUTOLOAD_ENV = "WDOTOOL_GNOME_AUTOLOAD"
# org.gnome.Shell Mode values in which no user session runs extensions. The
# unlocked session's mode is NOT always "user": Ubuntu's is "ubuntu" (parent
# mode user), GNOME Classic's "classic" -- so the test is for the known
# no-extension modes, never `!= "user"` (that misreported a disabled
# extension as a locked screen, observed live on 24.04).
_LOCKED_MODES = {"unlock-dialog", "initial-setup"}
_GREETER_MODES = {"gdm"}
# window types the pointer hit-test looks through (DING desktop icons, docks)
_LAYER_TYPES = {"DESKTOP", "DOCK"}

# Best-effort: load an installed-but-not-yet-loaded copy of the extension
# from inside the shell. Runs in shellDBus.js's module scope (Main, Gio, GLib
# imported there on 46 and 50); only reachable in unsafe mode.
_AUTOLOAD_JS = """\
(() => {
  const uuid = '%s';
  const em = Main.extensionManager;
  const G = (typeof GLib !== 'undefined') ? GLib : globalThis.imports.gi.GLib;
  let ext = em.lookup(uuid);
  let p = Promise.resolve();
  if (!ext) {
    const user = G.build_filenamev([G.get_user_data_dir(), 'gnome-shell', 'extensions', uuid]);
    const dirs = [[user, 2]].concat(G.get_system_data_dirs().map(
      d => [G.build_filenamev([d, 'gnome-shell', 'extensions', uuid]), 1]));
    for (const [d, type] of dirs) {
      const f = Gio.File.new_for_path(d);
      if (!f.query_exists(null))
        continue;
      ext = em.createExtensionObject(uuid, f, type);
      p = Promise.resolve(em.loadExtension(ext));
      break;
    }
  }
  if (!ext)
    return 'missing';
  return p.then(() => { em.enableExtension(uuid); return 'ok'; });
})()
""" % EXT_UUID


class GnomeBackend(WindowBackend):
    name = "gnome"

    def __init__(self, bus: Bus | None = None, names: list[str] | None = None,
                 settle: float = 0.5):
        """`bus`/`names`: reuse backend_detect's connection and its ListNames
        result (one round trip per process). `settle`: how long activate()
        waits for the focus change to land before returning (Mutter animates
        and may defer focus; `key --window` injects 50 ms after activate)."""
        self.settle = settle
        if bus is None:
            try:
                bus = Bus()
            except DBusError as e:
                raise CmdError("gnome backend: %s" % _no_bus_text(e)) from None
        self.bus = bus
        if names is None:
            try:
                names = self.bus.list_names()
            except DBusError as e:
                raise CmdError("gnome backend: ListNames failed: %s" % e) from None
        if BUS_NAME not in names:
            if SHELL_NAME not in names:
                raise CmdError("gnome backend: %s is not on the session bus "
                               "(no GNOME session?)" % SHELL_NAME)
            if not (_autoload_wanted() and self._try_autoload()):
                raise CmdError(self._missing_bridge_text())

    # -- plumbing -----------------------------------------------------------

    def _call(self, member: str, sig: str = "", args=(),
              timeout: float | None = CALL_TIMEOUT) -> tuple:
        try:
            return self.bus.call(BUS_NAME, OBJECT_PATH, IFACE, member, sig, args,
                                 timeout=timeout)
        except DBusError as e:
            raise self._map_error(member, e) from None

    @staticmethod
    def _map_error(member: str, e: DBusError) -> CmdError:
        n = e.name
        if n.startswith(IFACE + "."):
            kind = n[len(IFACE) + 1:]
            if kind in ("NotFound", "Unsupported"):
                return CmdError(e.message or "%s: %s" % (member, kind))
            return CmdError("gnome backend: %s: %s" % (member, e.message or kind))
        if n in (ERR + "ServiceUnknown", ERR + "NameHasNoOwner"):
            return CmdError(_GONE)
        if n == ERR + "NoReply":
            return CmdError("gnome backend: %s: no reply from the bridge within "
                            "the timeout (is gnome-shell hung?)" % member)
        if n == ERR + "Disconnected":
            return CmdError("gnome backend: session bus connection lost (%s)"
                            % e.message)
        return CmdError("gnome backend: %s failed: %s" % (member, e))

    def _missing_bridge_text(self) -> str:
        """Why is the bridge name missing? Extensions only run in the shell's
        `user` session mode (the name goes away behind the lock screen), and
        an installed extension may simply be disabled; both are readable by
        anyone on the bus. Falls back to the generic install hint."""
        try:
            mode = str(self.bus.get_property(SHELL_NAME, "/org/gnome/Shell",
                                             SHELL_NAME, "Mode",
                                             timeout=CALL_TIMEOUT) or "")
        except DBusError:
            mode = ""
        if mode in _LOCKED_MODES:
            return ("gnome backend: the fuckwayland bridge is unavailable while "
                    "GNOME Shell is in '%s' mode (screen locked?); extensions run "
                    "only in the unlocked session" % mode)
        if mode in _GREETER_MODES:
            return ("gnome backend: the GNOME Shell on this session bus is the "
                    "GDM greeter ('%s' mode): nobody is logged in there, and "
                    "extensions do not run in the greeter" % mode)
        if self._screen_locked():
            return ("gnome backend: the fuckwayland bridge is unavailable while "
                    "the screen is locked (GNOME Shell disables extensions "
                    "behind the lock screen); unlock the session")
        try:
            (info,) = self.bus.call(SHELL_NAME, "/org/gnome/Shell",
                                    SHELL_NAME + ".Extensions", "GetExtensionInfo",
                                    "s", (EXT_UUID,), timeout=CALL_TIMEOUT)
        except DBusError:
            info = {}
        if info:
            state = int(info.get("state", 0) or 0)
            if state == 1:
                return ("gnome backend: the fuckwayland bridge extension reports "
                        "active but %s is not owned; gnome/install-bridge.sh --check"
                        % BUS_NAME)
            if state == 3:
                return ("gnome backend: the fuckwayland bridge extension failed "
                        "to load: %s (gnome/install-bridge.sh --check)"
                        % (info.get("error") or "see journalctl --user _COMM=gnome-shell"))
            if state == 4:
                return ("gnome backend: the fuckwayland bridge extension is marked "
                        "out of date for this GNOME Shell (%s); reinstall a "
                        "matching gnome/ from the repo" % info.get("shell-version"))
            return ("gnome backend: the fuckwayland bridge extension is installed "
                    "but not enabled (state %d); run gnome/install-bridge.sh "
                    "(or: gnome-extensions enable %s)" % (state, EXT_UUID))
        return _HINT

    def _screen_locked(self) -> bool:
        """org.gnome.ScreenSaver.GetActive(): public, unprivileged, true
        while the shell's lock screen is up. Needed besides `Mode`: GNOME 46
        keeps the Mode property at the session mode ('ubuntu') while locked
        (observed live), only 50 reports 'unlock-dialog' there."""
        try:
            (active,) = self.bus.call(SCREENSAVER_NAME, "/org/gnome/ScreenSaver",
                                      SCREENSAVER_NAME, "GetActive",
                                      timeout=CALL_TIMEOUT)
        except (DBusError, ValueError):
            return False
        return bool(active)

    def _try_autoload(self) -> bool:
        """Eval-based load of an installed extension; False unless the shell
        is in unsafe mode and the load produced the bridge name. Opt-in only
        (WDOTOOL_GNOME_AUTOLOAD): Eval is a privileged interface and the
        common bridge-less case is "installed, needs a re-login", which
        must not send a round trip to it just to be refused."""
        try:
            ok, result = self.bus.call(SHELL_NAME, "/org/gnome/Shell", SHELL_NAME,
                                       "Eval", "s", (_AUTOLOAD_JS,), timeout=CALL_TIMEOUT)
        except DBusError:
            return False
        if not ok or "ok" not in str(result):
            return False
        deadline = time.monotonic() + AUTOLOAD_WAIT
        while time.monotonic() < deadline:
            try:
                if self.bus.name_has_owner(BUS_NAME):
                    return True
            except DBusError:
                return False
            time.sleep(0.1)
        return False

    def _json(self, member: str, sig: str = "", args=()):
        (raw,) = self._call(member, sig, args)
        try:
            return json.loads(raw)
        except ValueError:
            raise CmdError("gnome backend: %s returned malformed JSON" % member) from None

    def _raw_list(self) -> "list[dict]":
        data = self._json("ListWindows")
        return data if isinstance(data, list) else []

    def _raw_get(self, wid: int) -> dict:
        return self._json("GetWindow", "t", (wid,))

    @staticmethod
    def _win(d: dict) -> Window:
        hidden = bool(d.get("hidden", False))
        on_active = bool(d.get("on_active_workspace", True))
        return Window(
            id=int(d.get("id", 0)),
            title=d.get("title") or "",
            class_=d.get("wm_class") or d.get("gtk_app_id")
            or d.get("sandboxed_app_id") or "",
            instance=d.get("wm_class_instance") or "",
            pid=int(d.get("pid") or 0),
            x=int(d.get("x", 0)), y=int(d.get("y", 0)),
            w=int(d.get("width", 0)), h=int(d.get("height", 0)),
            focused=bool(d.get("focused", False)),
            visible=(not hidden) and on_active,
            desktop=int(d.get("workspace", -1)),
        )

    @classmethod
    def _view(cls, d: dict) -> View:
        win = cls._win(d)
        client = d.get("client_type") or "wayland"
        wm_class = d.get("wm_class") or ""
        app_id = d.get("gtk_app_id") or (wm_class if client == "wayland" else "")
        return View(
            window=win,
            xid=int(d.get("xid") or 0),
            instance=d.get("wm_class_instance") or wm_class or win.class_,
            cls=wm_class or win.class_,
            app_id=app_id,
            fullscreen=bool(d.get("fullscreen")),
            maximized_h=bool(d.get("maximized_h")),
            maximized_v=bool(d.get("maximized_v")),
            above=bool(d.get("above")),
            sticky=bool(d.get("on_all_workspaces")),
            urgent=bool(d.get("urgent")),
            minimized=bool(d.get("minimized")),
            hidden=bool(d.get("hidden")),
            skip_taskbar=bool(d.get("skip_taskbar")),
            floating=True,
            ws_name="",
            window_type=d.get("window_type") or "NORMAL",
            client_type=client,
            role=d.get("role") or "",
            desktop_id=d.get("desktop_id") or "",
            monitor=int(d.get("monitor", -1)),
            transient_for=int(d.get("transient_for") or 0),
            decorated=bool(d.get("decorated", True)),
        )

    def _wait_focused(self, wid: int):
        """activate()/focus() return before Mutter has moved the focus; give
        it `settle` seconds so `key --window` lands in the right window."""
        deadline = time.monotonic() + self.settle
        while time.monotonic() < deadline:
            try:
                if self._raw_get(wid).get("focused"):
                    return
            except CmdError:
                return
            time.sleep(0.03)

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        return [self._win(d) for d in self._raw_list()]

    def find(self, wid: int) -> Window:
        return self._win(self._raw_get(wid))

    def activate(self, wid: int):
        self._call("Activate", "t", (wid,))
        self._wait_focused(wid)

    def focus(self, wid: int):
        self._call("Focus", "t", (wid,))
        self._wait_focused(wid)

    def close(self, wid: int):
        self._call("Close", "t", (wid,))

    def kill(self, wid: int):
        # Mutter kills the client itself (XKillClient for X11, SIGKILL on the
        # pid for Wayland) -- works for any client whatever uid we run as.
        self._call("Kill", "t", (wid,))

    def minimize(self, wid: int):
        self._call("Minimize", "t", (wid,))

    def map(self, wid: int):
        self._call("Unminimize", "t", (wid,))

    def unmap(self, wid: int):
        self._call("Minimize", "t", (wid,))

    def is_mapped(self, wid: int) -> bool:
        return not self._raw_get(wid).get("minimized", False)

    def raise_(self, wid: int):
        self._call("Raise", "t", (wid,))

    def lower(self, wid: int):
        self._call("Lower", "t", (wid,))

    def move_window(self, wid: int, x: int, y: int):
        self._call("Move", "tii", (wid, x, y))

    def resize(self, wid: int, w: int, h: int):
        self._call("Resize", "tii", (wid, w, h))

    def set_state(self, wid: int, state: str, action: int):
        word = _ACTIONS.get(action)
        if word is None:
            raise CmdError("windowstate: bad action %r" % (action,))
        (applied,) = self._call("SetState", "tss", (wid, state, word))
        if applied:
            return
        if state in _COSMETIC_STATES:
            sys.stderr.write("wdotool: windowstate %s: Mutter cannot set it on "
                             "Wayland; ignoring\n" % state)
            return
        raise CmdError("windowstate %s is not supported by the gnome backend (%s)"
                       % (state, _GAP_REASONS.get(state, "Mutter has no API for it")))

    def window_desktop(self, wid: int) -> int:
        return self.find(wid).desktop

    def set_window_desktop(self, wid: int, n: int):
        self._call("MoveToWorkspace", "ti", (wid, n))

    def get_desktop(self) -> int:
        return int(self._call("GetActiveWorkspace")[0])

    def set_desktop(self, n: int):
        self._call("SetActiveWorkspace", "i", (n,))

    def num_desktops(self) -> int:
        return int(self._call("GetNWorkspaces")[0])

    def select_window(self) -> int:
        # SelectWindow(0) waits for the next focus change without a deadline;
        # the reply comes when the user focuses a different window.
        (wid,) = self._call("SelectWindow", "u", (0,), timeout=None)
        if not wid:
            raise CmdError("selectwindow: the bridge stopped waiting "
                           "(extension disabled while waiting?)")
        return int(wid)

    def display_size(self) -> tuple[int, int]:
        w, h = self._call("DisplaySize")
        if w <= 0 or h <= 0:
            raise CmdError("gnome backend: display size unknown")
        return int(w), int(h)

    # -- optional hooks -----------------------------------------------------

    def window_at(self, x: int, y: int) -> int:
        """Topmost window under (x, y) on the active workspace; DESKTOP and
        DOCK layers are looked through, the focused window wins among the
        hits (the generic rule getmouselocation applies). Client-side over
        ListWindows on purpose: the bridge exports no hit-test, so this and
        the rule in input_cmds cannot drift apart."""
        hits = []
        for d in self._raw_list():
            if d.get("window_type") in _LAYER_TYPES:
                continue
            if d.get("hidden") or not d.get("on_active_workspace", True):
                continue
            wx, wy = int(d.get("x", 0)), int(d.get("y", 0))
            ww, wh = int(d.get("width", 0)), int(d.get("height", 0))
            if ww > 0 and wh > 0 and wx <= x < wx + ww and wy <= y < wy + wh:
                hits.append(d)
        if not hits:
            return 0
        for d in hits:
            if d.get("focused"):
                return int(d["id"])
        return int(hits[-1]["id"])

    def views(self) -> "list[View]":
        names = {}
        try:
            names = {w.index: w.name for w in self.workspaces()}
        except CmdError:
            pass
        out = []
        for d in self._raw_list():
            v = self._view(d)
            v.ws_name = names.get(v.window.desktop, "")
            out.append(v)
        return out

    def workspaces(self) -> "list[Workspace]":
        out = []
        for d in self._json("ListWorkspaces") or []:
            wa = d.get("work_area") or {}
            out.append(Workspace(
                index=int(d.get("index", len(out))),
                name=d.get("name") or "",
                active=bool(d.get("active")),
                work_area=(int(wa.get("x", 0)), int(wa.get("y", 0)),
                           int(wa.get("width", 0)), int(wa.get("height", 0))),
            ))
        return out

    def x_info(self) -> tuple[str, str] | None:
        """(DISPLAY, XAUTHORITY) of Xwayland: what gnome-shell itself has in
        its environment (the bridge reads it), each blank filled from the
        session scan (session.find_x_display / find_xauthority)."""
        display, xauth = "", ""
        try:
            display, xauth = self._call("XInfo")
        except CmdError:
            pass
        uid = None
        try:
            uid = self.bus._owner_uid()
        except Exception:  # noqa: BLE001 -- diagnostics only
            uid = None
        display = display or session.find_x_display(uid) or ""
        xauth = xauth or session.find_xauthority(uid) or ""
        if not display and not xauth:
            return None
        return display, xauth

    def events(self, timeout: float | None = None, workspaces: bool = False):
        """(id, change) for every bridge WindowEvent, on a connection of its
        own so queued signals never pile up behind the command connection.
        With `workspaces` the bridge's WorkspaceEvent (switch/add/remove)
        is folded in as (0, "workspace") -- no window has id 0 -- so a
        root-level watcher (wxprop -root -spy) needs one stream only."""
        bus = Bus(self.bus.address)
        try:
            bus.add_match("type='signal',interface='%s',path='%s'"
                          % (IFACE, OBJECT_PATH))
            for m in bus.messages(timeout):
                if m.interface != IFACE:
                    continue
                if m.member == "WindowEvent":
                    wid, change = m.args()
                    yield int(wid), str(change)
                elif workspaces and m.member == "WorkspaceEvent":
                    yield 0, "workspace"
        finally:
            bus.close()

    # -- extras for the other tools ---------------------------------------

    # wmctrl -m: what Mutter writes into _NET_SUPPORTING_WM_CHECK's
    # _NET_WM_NAME on the X root, so the answer is the same with or without
    # Xwayland running (it is spawned on demand and must not be started
    # just to be asked its name).
    wm_name = "GNOME Shell"

    def show_desktop(self, show: bool):
        """wmctrl -k on|off. Mutter's own show-desktop mode has no public
        API; the bridge minimizes every normal window on the active
        workspace and remembers them for `off` (gnome/README.md)."""
        self._call("ShowDesktop", "b", (bool(show),))

    def set_num_desktops(self, n: int):
        """wmctrl -n: only with static workspaces; with dynamic workspaces
        (GNOME's default) the bridge answers Unsupported (a CmdError the
        caller turns into wmctrl's "the WM may ignore the request")."""
        self._call("SetNWorkspaces", "i", (int(n),))

    def monitors(self) -> "list[dict]":
        """[{index, x, y, width, height, scale, primary, connector}] --
        `connector` is "" on GNOME 46 (no JS route), filled on 49+."""
        data = self._json("ListMonitors")
        return data if isinstance(data, list) else []

    def pointer(self) -> tuple[int, int] | None:
        """The compositor's real pointer (B6). Mutter knows where the pointer
        is whoever moved it -- our tablet, a REL event, a physical mouse or
        another wdotool daemon -- so getmouselocation reports this and the
        input daemon's model is corrected from it before a relative move."""
        x, y, _mods = self._call("GetPointer")
        return int(x), int(y)

    # Historical name kept for the diagnostics scripts in vm/ and gnome/.
    real_pointer = pointer

    def bridge_version(self) -> int:
        return int(self._call("GetVersion")[0])


def _autoload_wanted() -> bool:
    v = os.environ.get(AUTOLOAD_ENV, "").strip().lower()
    return bool(v) and v not in ("0", "no", "false", "off")


def _no_bus_text(e: DBusError) -> str:
    if e.name == ERR + "NoServer":
        return "no session D-Bus found (set DBUS_SESSION_BUS_ADDRESS or run " \
               "inside the graphical session / under sudo)"
    return "cannot connect to the session D-Bus: %s" % e
