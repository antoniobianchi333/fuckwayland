#!/usr/bin/env python3
"""GNOME backend tests against a mock fuckwayland bridge served on
dbus_mini's in-process mock bus (tests/test_dbus_mini.py MockBus): every
GnomeBackend method, the Window/View/Workspace mapping, error mapping, the
pointer hit-test, the (opt-in) Eval auto-load path, backend_detect's order
for each ListNames outcome, and the shipped udev rule / installer /
interface XML. No GNOME, no real bus needed."""

import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_dbus_mini import MockBus                                   # noqa: E402
from wdotool import backend_detect, backend_gnome, dbus_mini, session  # noqa: E402
from wdotool.backend import View, Window, Workspace                    # noqa: E402
from wdotool.backend_gnome import (BUS_NAME, EXT_UUID, IFACE,        # noqa: E402
                                   OBJECT_PATH, SHELL_NAME, GnomeBackend)
from wdotool.ctx import CmdError, NoSessionError                       # noqa: E402
from wdotool.dbus_mini import Bus, DBusError, Message, Variant         # noqa: E402

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

XTERM, EDITOR, CALC, DESKTOP = 4194305, 4194306, 4194307, 4194301
XTERM_XID = 0x400005


def _rect(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


def fixture_windows():
    """Bottom-to-top stacking order, shaped like the bridge's ListWindows:
    DING's DESKTOP window, a minimized native editor, a calculator parked
    on workspace 1, and a focused XWayland xterm on top."""
    base = {
        "wm_class_instance": "", "gtk_app_id": "", "sandboxed_app_id": "",
        "desktop_id": "", "role": "", "client_type": "wayland",
        "window_type": "NORMAL", "focused": False, "minimized": False,
        "hidden": False, "on_all_workspaces": False, "workspace": 0,
        "on_active_workspace": True, "monitor": 0, "fullscreen": False,
        "maximized_h": False, "maximized_v": False, "above": False,
        "urgent": False, "skip_taskbar": False, "transient_for": 0,
        "decorated": True, "xid": 0,
    }

    def win(**kw):
        d = dict(base)
        d.update(kw)
        d.update(_rect(*d.pop("rect")))
        d["buffer_rect"] = _rect(d["x"], d["y"], d["width"], d["height"])
        d["stable_sequence"] = d["id"] & 0xff
        return d

    return [
        win(id=DESKTOP, title="Desktop", wm_class="Gjs", pid=900,
            window_type="DESKTOP", rect=(0, 0, 1920, 1080), skip_taskbar=True),
        win(id=EDITOR, title="Untitled Document 1 - Text Editor", wm_class="",
            gtk_app_id="org.gnome.TextEditor",
            desktop_id="org.gnome.TextEditor.desktop", pid=1300,
            rect=(300, 200, 800, 600), minimized=True, hidden=True),
        win(id=CALC, title="Calculator", wm_class="org.gnome.Calculator",
            gtk_app_id="org.gnome.Calculator", pid=1400, workspace=1,
            on_active_workspace=False, rect=(500, 300, 400, 500)),
        win(id=XTERM, xid=XTERM_XID, title="test@vm: ~", wm_class="XTerm",
            wm_class_instance="xterm", pid=1201, client_type="x11",
            rect=(100, 80, 640, 480), focused=True),
    ]


class MockBridge:
    """The extension's D-Bus surface on a Bus of its own (serve_calls),
    owning org.gnome.Shell and/or org.fuckwayland.Bridge like the real shell
    connection does. State lives in `windows` (bridge JSON shape) and is
    mutated by the actions; `calls` records (member, args)."""

    # The bridge's own hard cap on a window selection (extension.js
    # SELECT_MAX_MS); a test below checks the two have not drifted apart.
    SELECT_MAX_MS = 300000
    VERSION = 2

    def __init__(self, mock, own_shell=True, own_bridge=True,
                 eval_unsafe=False, select_delay=0.2, select_id=EDITOR,
                 shell_mode="user", ext_info=None, screensaver_active=False,
                 shell_version="46.0", version=None):
        #: the MockBus itself, not just its address: close() has to wait for
        #: the bus to let go of the names this connection owns before the
        #: next bridge asks for them (MockBus.wait_dropped)
        self.mock = mock
        self.bus = Bus(mock.address)
        self.bus.serve_calls = True
        self.windows = fixture_windows()
        self.calls = []
        self.active_ws = 0
        self.n_ws = 3
        self.eval_unsafe = eval_unsafe
        self.shell_mode = shell_mode
        self.ext_info = ext_info  # None: uuid unknown to the shell
        self.screensaver_active = screensaver_active
        self.select_delay = select_delay
        # What the user does at the picker: "button" (the default), "escape",
        # "timeout" or "disable"; where the press lands is select_at (a point,
        # hit-tested) or, by default, select_id (the window it resolves to).
        self.select_event = "button"
        self.select_id = select_id
        self.select_at = None
        self.grabs = []            # "take"/"release", in order
        self.version = self.VERSION if version is None else version
        self.xinfo = (":0", "/run/user/1000/.mutter-Xwaylandauth.AB12CD")
        self.pointer = (640, 400, 0)
        self._show_desktop_wins = []   # ShowDesktop(true)'s restore set
        self.shell_version = shell_version
        if own_shell:
            assert self.bus.request_name(SHELL_NAME) == 1
            assert self.bus.request_name("org.gnome.ScreenSaver") == 1
        if own_bridge:
            assert self.bus.request_name(BUS_NAME) == 1
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        if self.bus.sock is None:
            return
        unique = self.bus.unique_name
        try:
            self.bus.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(3)
        self.bus.close()
        # Closing the socket is not the same event as the bus letting go of
        # org.gnome.Shell and org.fuckwayland.Bridge: that happens in the
        # bus's own thread when this connection's recv() returns nothing.
        # Return before it and the next bridge's RequestName is answered 3,
        # not 1 -- which is what made this file fail about one run in ten
        # under load. Wait for the drop instead of hoping to win the race.
        if not self.mock.wait_dropped(unique):
            raise AssertionError(
                "MockBus still holds connection %s five seconds after it "
                "was closed" % unique)

    # -- state helpers

    def find(self, wid):
        for d in self.windows:
            if d["id"] == wid:
                return d
        raise DBusError(IFACE + ".NotFound", "window %d not found" % wid)

    def _focus(self, wid):
        for d in self.windows:
            d["focused"] = d["id"] == wid

    def _refresh_active(self):
        for d in self.windows:
            d["on_active_workspace"] = (d["on_all_workspaces"]
                                        or d["workspace"] == self.active_ws)

    # -- serving

    def _serve(self):
        try:
            for m in self.bus.messages(None):
                if m.type != dbus_mini.METHOD_CALL:
                    continue
                try:
                    sig, out = self._dispatch(m)
                except DBusError as e:
                    self.bus.error_reply(m, e.name, e.message)
                    continue
                if sig is None:
                    continue  # async reply already handled
                self.bus.reply(m, sig, out)
        except Exception:  # noqa: BLE001 -- socket shut down by close()
            pass

    def _dispatch(self, m):
        a = m.args()
        if m.interface == dbus_mini.PROPS_IFACE and m.member == "Get":
            if a == (IFACE, "Version"):
                return "v", (Variant("u", self.version),)
            if a == (SHELL_NAME, "Mode"):
                return "v", (Variant("s", self.shell_mode),)
            if a == (SHELL_NAME, "ShellVersion"):
                if self.shell_version is None:
                    raise DBusError(dbus_mini.ERR + "InvalidArgs",
                                    "no such property")
                return "v", (Variant("s", self.shell_version),)
            raise DBusError(dbus_mini.ERR + "InvalidArgs", "no such property")
        if m.interface == "org.gnome.ScreenSaver" and m.member == "GetActive":
            self.calls.append(("GetActive", a))
            return "b", (bool(self.screensaver_active),)
        if m.interface == SHELL_NAME + ".Extensions" and m.member == "GetExtensionInfo":
            self.calls.append(("GetExtensionInfo", a))
            info = dict(self.ext_info or {}) if a[0] == EXT_UUID else {}
            return "a{sv}", ({k: Variant("d", float(v)) if isinstance(v, (int, float))
                              else Variant("s", str(v)) for k, v in info.items()},)
        if m.interface == SHELL_NAME and m.member == "Eval":
            self.calls.append(("Eval", a))
            if not self.eval_unsafe:
                return "bs", (False, "")
            # unsafe mode: pretend loadExtension ran and the bridge came up
            self.bus.request_name(BUS_NAME)
            return "bs", (True, '"ok"')
        if m.interface != IFACE or m.path != OBJECT_PATH:
            raise DBusError(dbus_mini.ERR + "UnknownMethod",
                            "no %s on %s" % (m.member, m.path))
        self.calls.append((m.member, a))
        h = getattr(self, "m_" + m.member, None)
        if h is None:
            raise DBusError(dbus_mini.ERR + "UnknownMethod", "no %s" % m.member)
        return h(m, *a)

    # -- bridge methods (return (out signature, out values))

    def m_ListWindows(self, m):
        import json
        return "s", (json.dumps(self.windows),)

    def m_GetWindow(self, m, wid):
        import json
        return "s", (json.dumps(self.find(wid)),)

    def m_Activate(self, m, wid):
        d = self.find(wid)
        d["minimized"] = d["hidden"] = False
        if not d["on_all_workspaces"]:
            self.active_ws = d["workspace"]
        self._refresh_active()
        self.windows.remove(d)
        self.windows.append(d)
        self._focus(wid)
        return "", ()

    def m_Focus(self, m, wid):
        self.find(wid)
        self._focus(wid)
        return "", ()

    def m_Close(self, m, wid):
        self.windows.remove(self.find(wid))
        return "", ()

    m_Kill = m_Close

    def m_Minimize(self, m, wid):
        d = self.find(wid)
        d["minimized"] = d["hidden"] = True
        d["focused"] = False
        return "", ()

    def m_Unminimize(self, m, wid):
        d = self.find(wid)
        d["minimized"] = d["hidden"] = False
        return "", ()

    def m_Raise(self, m, wid):
        d = self.find(wid)
        self.windows.remove(d)
        self.windows.append(d)
        return "", ()

    def m_Lower(self, m, wid):
        d = self.find(wid)
        self.windows.remove(d)
        self.windows.insert(0, d)
        return "", ()

    def m_Move(self, m, wid, x, y):
        d = self.find(wid)
        d["x"], d["y"] = x, y
        return "", ()

    def m_Resize(self, m, wid, w, h):
        d = self.find(wid)
        d["width"], d["height"] = w, h
        return "", ()

    def m_MoveResize(self, m, wid, x, y, w, h):
        d = self.find(wid)
        d.update(x=x, y=y, width=w, height=h)
        return "", ()

    def m_SetState(self, m, wid, state, action):
        d = self.find(wid)
        if action not in ("add", "remove", "toggle"):
            raise DBusError(IFACE + ".InvalidArgs",
                            "action must be add|remove|toggle, got %s" % action)
        key = {"FULLSCREEN": "fullscreen", "MAXIMIZED_HORZ": "maximized_h",
               "MAXIMIZED_VERT": "maximized_v", "HIDDEN": "minimized",
               "ABOVE": "above", "STICKY": "on_all_workspaces",
               "DEMANDS_ATTENTION": "urgent"}.get(state)
        if key is None:
            return "b", (False,)
        want = {"add": True, "remove": False}.get(action, not d[key])
        d[key] = want
        if key == "minimized":
            d["hidden"] = want
        if key == "on_all_workspaces":
            d["workspace"] = -1 if want else self.active_ws
        return "b", (True,)

    def m_MoveToWorkspace(self, m, wid, index):
        d = self.find(wid)
        if index < 0:
            d["on_all_workspaces"], d["workspace"] = True, -1
        elif index >= self.n_ws:
            raise DBusError(IFACE + ".NotFound", "workspace %d not found" % index)
        else:
            d["on_all_workspaces"], d["workspace"] = False, index
        self._refresh_active()
        return "", ()

    def window_under_pointer(self, x, y):
        """Port of the extension's _windowUnderPointer, over the same rows it
        uses (ListWindows', bottom-to-top): the topmost window containing the
        point, DESKTOP/DOCK looked through, hidden windows and other
        workspaces skipped. The last hit wins -- a focused window does NOT win
        over one above it, unlike getmouselocation's tie-break. 0 = the press
        landed on no window."""
        hit = 0
        for d in self.windows:
            if d["window_type"] in ("DESKTOP", "DOCK"):
                continue
            if d["minimized"] or d["hidden"]:
                continue
            if not (d["on_active_workspace"] or d["on_all_workspaces"]):
                continue
            if d["width"] <= 0 or d["height"] <= 0:
                continue
            if (d["x"] <= x < d["x"] + d["width"]
                    and d["y"] <= y < d["y"] + d["height"]):
                hit = d["id"]
        return hit

    def m_SelectWindow(self, m, timeout_ms):
        """Port of the extension's SelectWindow (extension.js): grab, answer
        on the NEXT BUTTON PRESS with the window under the pointer, and let
        Escape / the timeout / a shutdown come back as .Cancelled -- with the
        grab released on every one of those paths, which `grabs` records."""
        cap = min(timeout_ms or self.SELECT_MAX_MS, self.SELECT_MAX_MS)
        # Refused before anything is taken: a second concurrent picker (the
        # first one's handler would eat every event, leaving this one to
        # swallow the user's next click), and a shell that is already modal
        # (the overview, a menu -- pushModal refuses and the extension does
        # not reach past it for a plain stage grab, because the windows'
        # frame rects are not what is on screen then).
        if self.select_event == "busy":
            raise DBusError(IFACE + ".Unsupported",
                            "another window selection is already in progress")
        if self.select_event == "modal":
            raise DBusError(IFACE + ".Unsupported",
                            "the shell would not grant an input grab "
                            "(something else is modal: the overview, a menu, "
                            "a dialog)")
        self.grabs.append("take")
        try:
            time.sleep(self.select_delay)
            reasons = {"escape": "cancelled with Escape",
                       "timeout": "no window picked within %d ms" % cap,
                       "disable": "the bridge extension was disabled"}
            if self.select_event in reasons:
                raise DBusError(IFACE + ".Cancelled", reasons[self.select_event])
            if self.select_at is not None:
                return "t", (self.window_under_pointer(*self.select_at),)
            return "t", (self.select_id,)
        finally:
            self.grabs.append("release")

    def m_GetActiveWorkspace(self, m):
        return "i", (self.active_ws,)

    def m_SetActiveWorkspace(self, m, index):
        if not 0 <= index < self.n_ws:
            raise DBusError(IFACE + ".NotFound", "workspace %d not found" % index)
        self.active_ws = index
        self._refresh_active()
        return "", ()

    def m_GetNWorkspaces(self, m):
        return "i", (self.n_ws,)

    def m_SetNWorkspaces(self, m, count):
        raise DBusError(IFACE + ".Unsupported",
                        "dynamic workspaces are enabled (org.gnome.mutter "
                        "dynamic-workspaces); the workspace count is managed by the shell")

    def m_ListWorkspaces(self, m):
        import json
        out = [{"index": i, "name": "Workspace %d" % (i + 1),
                "active": i == self.active_ws,
                "work_area": _rect(0, 32, 1920, 1048), "viewport": {"x": 0, "y": 0}}
               for i in range(self.n_ws)]
        return "s", (json.dumps(out),)

    def m_ShowDesktop(self, m, show):
        """Faithful port of the extension's _showDesktop (extension.js):
        minimize every normal window on the active workspace and remember
        them, restore that set on `off`. `on` is a LATCH -- a second one
        must not rescan, or the restore set would come back empty (the
        scan skips already-minimized windows) and `off` would restore
        nothing, forever."""
        if not show:
            for wid in self._show_desktop_wins:
                d = next((x for x in self.windows if x["id"] == wid), None)
                if d is not None and d["minimized"]:
                    d["minimized"] = d["hidden"] = False
            self._show_desktop_wins = []
            return "", ()
        if self._show_desktop_wins:
            return "", ()
        done = []
        for d in self.windows:
            if d["minimized"] or d["window_type"] in ("DESKTOP", "DOCK"):
                continue
            if not (d["on_active_workspace"] or d["on_all_workspaces"]):
                continue
            d["minimized"] = d["hidden"] = True
            d["focused"] = False
            done.append(d["id"])
        self._show_desktop_wins = done
        return "", ()

    def m_DisplaySize(self, m):
        return "ii", (1920, 1080)

    def m_GetPointer(self, m):
        return "iiu", self.pointer

    def m_ListMonitors(self, m):
        import json
        return "s", (json.dumps([{"index": 0, "x": 0, "y": 0, "width": 1920,
                                  "height": 1080, "scale": 1, "primary": True,
                                  "connector": "Virtual-1"}]),)

    def m_XInfo(self, m):
        return "ss", self.xinfo

    def m_ConfirmDisplayChange(self, m, keep):
        return "b", (False,)

    def m_GetVersion(self, m):
        return "u", (self.version,)


@contextlib.contextmanager
def no_bus():
    """Pretend no session bus exists anywhere (the host running the tests
    usually has one under /run/user/<uid>/bus that the scan would find)."""
    orig = session.find_user_bus
    session.find_user_bus = lambda: None
    try:
        yield
    finally:
        session.find_user_bus = orig


@contextlib.contextmanager
def env(**kw):
    """Temporarily set (value) / unset (None) environment variables."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()
        cls.rtdir = tempfile.mkdtemp(prefix="wdotool-gnome-rt-")
        cls._env = env(DBUS_SESSION_BUS_ADDRESS=cls.mock.address,
                       XDG_RUNTIME_DIR=cls.rtdir, WAYLAND_DISPLAY=None,
                       SWAYSOCK=None, I3SOCK=None, WDOTOOL_BACKEND=None,
                       SUDO_UID=None, PKEXEC_UID=None)
        cls._env.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._env.__exit__(None, None, None)
        cls.mock.close()
        shutil.rmtree(cls.rtdir, ignore_errors=True)


class BackendTests(_Base):
    def setUp(self):
        self.bridge = MockBridge(self.mock)
        self.b = GnomeBackend(settle=0.3)

    def tearDown(self):
        self.b.bus.close()
        self.bridge.close()

    def calls(self, member):
        return [a for m, a in self.bridge.calls if m == member]

    # -- listing / fields

    def test_list_maps_fields(self):
        wins = self.b.list()
        self.assertEqual([w.id for w in wins], [DESKTOP, EDITOR, CALC, XTERM])
        xterm = wins[-1]
        self.assertEqual(xterm, Window(id=XTERM, title="test@vm: ~", class_="XTerm",
                                       instance="xterm", pid=1201,
                                       x=100, y=80, w=640, h=480,
                                       focused=True, visible=True, desktop=0))
        # B4: native toplevels carry no WM_CLASS instance, so --classname
        # falls back to class_ = the app_id for them.
        self.assertEqual(wins[2].instance, "")
        editor = wins[1]
        self.assertEqual(editor.class_, "org.gnome.TextEditor")  # gtk_app_id fallback
        self.assertFalse(editor.visible)  # minimized
        self.assertTrue(editor.pid == 1300)
        calc = wins[2]
        self.assertFalse(calc.visible)  # parked on workspace 1
        self.assertEqual(calc.desktop, 1)
        self.assertTrue(wins[0].visible)  # the desktop window is a window too
        self.assertEqual(sum(w.focused for w in wins), 1)

    def test_find_uses_getwindow_and_notfound(self):
        w = self.b.find(CALC)
        self.assertEqual((w.id, w.title, w.desktop), (CALC, "Calculator", 1))
        self.assertEqual(self.calls("GetWindow"), [(CALC,)])
        with self.assertRaises(CmdError) as cm:
            self.b.find(999)
        self.assertEqual(str(cm.exception), "window 999 not found")

    def test_is_mapped_is_not_minimized(self):
        self.assertTrue(self.b.is_mapped(XTERM))
        self.assertFalse(self.b.is_mapped(EDITOR))
        self.assertTrue(self.b.is_mapped(CALC))  # other workspace, still mapped

    # -- actions

    def test_activate_switches_workspace_unminimizes_and_settles(self):
        self.b.activate(CALC)
        self.assertEqual(self.calls("Activate"), [(CALC,)])
        self.assertGreaterEqual(len(self.calls("GetWindow")), 1)  # settle poll
        w = self.b.find(CALC)
        self.assertTrue(w.focused and w.visible)
        self.assertEqual(self.b.get_desktop(), 1)
        self.assertEqual(self.b.list()[-1].id, CALC)  # raised on top
        self.b.activate(EDITOR)
        self.assertTrue(self.b.find(EDITOR).visible)

    def test_activate_unknown_window(self):
        with self.assertRaises(CmdError) as cm:
            self.b.activate(42)
        self.assertEqual(str(cm.exception), "window 42 not found")

    def test_focus_does_not_raise(self):
        self.b.focus(DESKTOP)
        self.assertEqual(self.calls("Focus"), [(DESKTOP,)])
        wins = self.b.list()
        self.assertTrue(wins[0].focused)
        self.assertEqual(wins[-1].id, XTERM)  # stacking unchanged

    def test_close_and_kill(self):
        self.b.close(EDITOR)
        self.assertEqual(self.calls("Close"), [(EDITOR,)])
        self.assertNotIn(EDITOR, [w.id for w in self.b.list()])
        self.b.kill(XTERM)  # via the bridge, not os.kill
        self.assertEqual(self.calls("Kill"), [(XTERM,)])
        self.assertNotIn(XTERM, [w.id for w in self.b.list()])

    def test_minimize_map_unmap(self):
        self.b.minimize(XTERM)
        self.assertEqual(self.calls("Minimize"), [(XTERM,)])
        self.assertFalse(self.b.find(XTERM).visible)
        self.b.map(XTERM)
        self.assertEqual(self.calls("Unminimize"), [(XTERM,)])
        self.assertTrue(self.b.find(XTERM).visible)
        self.b.unmap(XTERM)
        self.assertEqual(self.calls("Minimize"), [(XTERM,), (XTERM,)])
        self.assertFalse(self.b.is_mapped(XTERM))

    def test_raise_lower(self):
        self.b.lower(XTERM)
        self.assertEqual(self.b.list()[0].id, XTERM)
        self.b.raise_(XTERM)
        self.assertEqual(self.b.list()[-1].id, XTERM)
        self.assertEqual(self.calls("Lower") + self.calls("Raise"), [(XTERM,), (XTERM,)])

    def test_move_resize(self):
        self.b.move_window(XTERM, 10, 20)
        self.b.resize(XTERM, 700, 500)
        self.assertEqual(self.calls("Move"), [(XTERM, 10, 20)])
        self.assertEqual(self.calls("Resize"), [(XTERM, 700, 500)])
        w = self.b.find(XTERM)
        self.assertEqual((w.x, w.y, w.w, w.h), (10, 20, 700, 500))
        with self.assertRaises(CmdError):
            self.b.move_window(7, 0, 0)

    def test_set_state_bridge_states(self):
        for state, key in (("FULLSCREEN", "fullscreen"), ("MAXIMIZED_HORZ", "maximized_h"),
                           ("MAXIMIZED_VERT", "maximized_v"), ("HIDDEN", "minimized"),
                           ("ABOVE", "above"), ("STICKY", "on_all_workspaces"),
                           ("DEMANDS_ATTENTION", "urgent")):
            self.b.set_state(XTERM, state, 1)
            self.assertTrue(self.bridge.find(XTERM)[key], state)
            self.b.set_state(XTERM, state, 0)
            self.assertFalse(self.bridge.find(XTERM)[key], state)
            self.b.set_state(XTERM, state, 2)
            self.assertTrue(self.bridge.find(XTERM)[key], state)
            self.b.set_state(XTERM, state, 2)
            self.assertFalse(self.bridge.find(XTERM)[key], state)
        self.assertEqual(self.calls("SetState")[:3],
                         [(XTERM, "FULLSCREEN", "add"), (XTERM, "FULLSCREEN", "remove"),
                          (XTERM, "FULLSCREEN", "toggle")])
        self.b.set_state(XTERM, "STICKY", 1)
        self.assertEqual(self.b.find(XTERM).desktop, -1)

    def test_set_state_cosmetic_warns_and_succeeds(self):
        for state in ("SKIP_TASKBAR", "SKIP_PAGER", "MODAL"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.b.set_state(XTERM, state, 1)
            self.assertIn("windowstate %s" % state, err.getvalue())
            self.assertIn("ignoring", err.getvalue())
        # but the window must exist
        with self.assertRaises(CmdError):
            self.b.set_state(999, "SKIP_TASKBAR", 1)

    def test_set_state_capability_gaps_raise(self):
        for state in ("BELOW", "SHADED", "FOO"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(CmdError) as cm:
                self.b.set_state(XTERM, state, 1)
            self.assertIn("windowstate %s is not supported" % state, str(cm.exception))
            self.assertEqual(err.getvalue(), "")   # an error, not a warning
        with self.assertRaises(CmdError) as cm:
            self.b.set_state(XTERM, "SHADED", 2)
        # review finding 2: shading is observable, so it is a gap, never rc 0
        self.assertIn("does not implement window shading", str(cm.exception))
        with self.assertRaises(CmdError):
            self.b.set_state(XTERM, "FULLSCREEN", 7)

    # -- desktops

    def test_desktops(self):
        self.assertEqual(self.b.num_desktops(), 3)
        self.assertEqual(self.b.get_desktop(), 0)
        self.b.set_desktop(2)
        self.assertEqual(self.b.get_desktop(), 2)
        self.assertFalse(self.b.find(XTERM).visible)  # now on another workspace
        with self.assertRaises(CmdError) as cm:
            self.b.set_desktop(9)
        self.assertEqual(str(cm.exception), "workspace 9 not found")
        self.assertEqual(self.b.window_desktop(CALC), 1)
        self.b.set_window_desktop(CALC, 2)
        self.assertEqual(self.b.window_desktop(CALC), 2)
        self.assertTrue(self.b.find(CALC).visible)
        self.b.set_window_desktop(CALC, -1)
        self.assertEqual(self.b.window_desktop(CALC), -1)
        with self.assertRaises(CmdError):
            self.b.set_window_desktop(CALC, 5)
        self.assertEqual(self.calls("MoveToWorkspace"), [(CALC, 2), (CALC, -1), (CALC, 5)])

    def test_pointer_reports_the_compositors_own(self):
        # B6: Mutter knows where the pointer is whoever moved it, so this is
        # what getmouselocation reports and what seeds the daemon's model.
        self.assertEqual(self.b.pointer(), (640, 400))
        self.bridge.pointer = (2881, 17, 0)
        self.assertEqual(self.b.pointer(), (2881, 17))
        self.assertEqual(self.b.real_pointer(), (2881, 17))  # historical alias
        self.assertEqual(len(self.calls("GetPointer")), 3)

    def test_set_num_desktops_calls_the_bridge(self):
        # B9: the command used to print "managed by the compositor; ignoring"
        # and never call anything. The mock bridge answers Unsupported (its
        # dynamic-workspaces are on), which must be marked as a capability
        # gap rather than a plain failure.
        with self.assertRaises(CmdError) as cm:
            self.b.set_num_desktops(3)
        self.assertEqual(self.calls("SetNWorkspaces"), [(3,)])
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertIn("dynamic workspaces", str(cm.exception))

    def test_invalid_window_id_is_one_line_not_a_marshal_traceback(self):
        # B8: -5 and 2**64 cannot be carried as a D-Bus 't'; ctx rejects them
        # first, and _call is the belt-and-braces guard behind it.
        for bad in (-5, 2 ** 64):
            with self.assertRaises(CmdError) as cm:
                self.b._call("GetWindow", "t", (bad,))
            self.assertIn("invalid argument", str(cm.exception))
            self.assertEqual(len(str(cm.exception).splitlines()), 1)

    def test_display_size_and_extras(self):
        self.assertEqual(self.b.display_size(), (1920, 1080))
        self.assertEqual(self.b.bridge_version(), MockBridge.VERSION)
        mons = self.b.monitors()
        self.assertEqual((mons[0]["connector"], mons[0]["primary"]), ("Virtual-1", True))

    # -- selectwindow: the window under the pointer at the next press

    def test_select_window_blocks_for_a_button_press(self):
        t0 = time.monotonic()
        self.assertEqual(self.b.select_window(), EDITOR)
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)
        # timeout_ms 0: the wait is as long as the user takes (the bridge
        # caps it), and the grab is released whatever happens
        self.assertEqual(self.calls("SelectWindow"), [(0,)])
        self.assertEqual(self.bridge.grabs, ["take", "release"])

    def test_select_window_returns_the_window_that_already_has_focus(self):
        # The whole point: xdotool answers with the window under the pointer,
        # so clicking the focused window is a selection like any other. The
        # focus-change picker could never return it -- it waited for a focus
        # event that a click on the focused window does not produce.
        self.assertTrue(self.bridge.find(XTERM)["focused"])
        self.bridge.select_at = (150, 100)          # inside the xterm
        self.assertEqual(self.b.select_window(), XTERM)
        self.assertEqual(self.bridge.grabs, ["take", "release"])
        self.assertEqual([m for m, _ in self.bridge.calls if m == "Focus"], [])

    def test_select_window_picks_the_topmost_not_the_focused(self):
        # The one deliberate difference from getmouselocation's window: a
        # click lands on what is on top of it, focus or no focus. Here the
        # editor is focused and the xterm is stacked above it.
        self.b.map(EDITOR)
        self.b.focus(EDITOR)
        self.assertEqual(self.b.window_at(350, 250), EDITOR)   # focused wins
        self.bridge.select_at = (350, 250)
        self.assertEqual(self.b.select_window(), XTERM)        # topmost wins

    def test_select_window_hit_test_matches_the_client_side_rule(self):
        # Same answers as window_at() for the same points: the desktop layer
        # is looked through, other workspaces and hidden windows are not hits.
        for x, y in ((150, 100), (600, 400), (1800, 1000), (0, 0)):
            self.bridge.select_at = (x, y)
            expected = self.b.window_at(x, y)
            if expected:
                self.assertEqual(self.b.select_window(), expected)
            else:
                with self.assertRaises(CmdError) as cm:
                    self.b.select_window()
                self.assertIn("no window under the pointer", str(cm.exception))

    def test_select_window_cancelled_with_escape(self):
        self.bridge.select_event = "escape"
        with self.assertRaises(CmdError) as cm:
            self.b.select_window()
        self.assertEqual(str(cm.exception), "selectwindow: cancelled with Escape")
        self.assertFalse(getattr(cm.exception, "unsupported", False))
        self.assertEqual(self.bridge.grabs, ["take", "release"])

    def test_select_window_timeout_and_shutdown_are_cancellations(self):
        for event, needle in (("timeout", "no window picked within"),
                              ("disable", "extension was disabled")):
            self.bridge.grabs = []
            self.bridge.select_event = event
            with self.assertRaises(CmdError) as cm:
                self.b.select_window()
            self.assertIn(needle, str(cm.exception))
            self.assertTrue(str(cm.exception).startswith("selectwindow: "))
            self.assertEqual(self.bridge.grabs, ["take", "release"])

    def test_select_window_refused_while_something_else_is_modal(self):
        # F4: with the overview up the extension used to grab anyway and
        # hit-test the click against frame rects that were not on screen,
        # answering with the wrong window or none. It refuses now, and the
        # session keeps its own modal.
        self.bridge.select_event = "modal"
        with self.assertRaises(CmdError) as cm:
            self.b.select_window()
        self.assertIn("would not grant an input grab", str(cm.exception))
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(self.bridge.grabs, [])     # nothing taken, nothing held

    def test_a_second_picker_is_refused_rather_than_left_grabbing(self):
        # Two stage grabs coexist, but only the first captured-event handler
        # sees each event, so the second picker would sit there grabbing and
        # then eat the user's next click.
        self.bridge.select_event = "busy"
        with self.assertRaises(CmdError) as cm:
            self.b.select_window()
        self.assertIn("already in progress", str(cm.exception))
        self.assertTrue(getattr(cm.exception, "unsupported", False))
        self.assertEqual(self.bridge.grabs, [])

    def test_the_hint_for_an_interactive_selection_says_click(self):
        # wwmctl -a :SELECT: and wxprop's click-select print this before
        # blocking. On GNOME the picker wants a click; only sway's backend,
        # which can do nothing but wait for a focus change, says "focus".
        from wdotool.backend_sway import SwayBackend
        self.assertEqual(self.b.select_window_hint,
                         "click the target window to select it")
        self.assertEqual(SwayBackend.select_window_hint,
                         "focus the target window to select it")

    def test_select_window_on_an_old_bridge_says_to_reinstall_it(self):
        # A v1 bridge is still installed until the user logs back in; it can
        # only wait for a focus change, so it would hang on the focused
        # window. Say what to do about it instead of hanging.
        self.bridge.version = 1
        with self.assertRaises(CmdError) as cm:
            self.b.select_window()
        self.assertIn("version 1", str(cm.exception))
        self.assertIn("install-bridge.sh", str(cm.exception))
        self.assertEqual(self.calls("SelectWindow"), [])

    # -- pointer hit-test

    def test_window_at_skips_desktop_hidden_and_other_workspaces(self):
        self.assertEqual(self.b.window_at(150, 100), XTERM)
        # review finding 3: one hit-test, client-side; the bridge has none
        self.assertEqual([m for m, _ in self.bridge.calls], ["ListWindows"])
        self.assertEqual(self.b.window_at(1800, 1000), 0)     # only the desktop there
        self.assertEqual(self.b.window_at(600, 400), XTERM)   # calc is on ws 1
        self.assertEqual(self.b.window_at(-1, -1), 0)
        # nothing focused: topmost hit wins; the minimized editor never does
        self.bridge._focus(0)
        self.bridge.find(XTERM)["x"] = 0
        self.bridge.find(DESKTOP)["focused"] = True  # focused desktop is still looked through
        self.assertEqual(self.b.window_at(350, 250), XTERM)
        self.b.minimize(XTERM)
        self.assertEqual(self.b.window_at(350, 250), 0)
        self.b.map(EDITOR)
        self.assertEqual(self.b.window_at(350, 250), EDITOR)

    def test_getmouselocation_uses_the_hook(self):
        from wdotool.input_cmds import _window_under_pointer

        class Ctx:
            def backend(inner):
                return self.b

        self.assertEqual(_window_under_pointer(Ctx(), 150, 100), XTERM)
        self.assertEqual(_window_under_pointer(Ctx(), 1800, 1000), 0)

    # -- richer views

    def test_views(self):
        views = self.b.views()
        self.assertEqual([v.window.id for v in views], [DESKTOP, EDITOR, CALC, XTERM])
        xt = views[-1]
        self.assertIsInstance(xt, View)
        self.assertEqual((xt.xid, xt.instance, xt.cls, xt.app_id, xt.client_type),
                         (XTERM_XID, "xterm", "XTerm", "", "x11"))
        ed = views[1]
        self.assertEqual((ed.xid, ed.instance, ed.cls, ed.app_id, ed.client_type),
                         (0, "org.gnome.TextEditor", "org.gnome.TextEditor",
                          "org.gnome.TextEditor", "wayland"))
        self.assertTrue(ed.minimized and ed.hidden)
        self.assertEqual(ed.desktop_id, "org.gnome.TextEditor.desktop")
        self.assertEqual(views[0].window_type, "DESKTOP")
        self.assertEqual(views[2].ws_name, "Workspace 2")
        self.assertTrue(all(v.floating for v in views))

    def test_workspaces(self):
        ws = self.b.workspaces()
        self.assertEqual(ws[0], Workspace(index=0, name="Workspace 1", active=True,
                                          work_area=(0, 32, 1920, 1048)))
        self.assertEqual([w.active for w in ws], [True, False, False])

    def test_x_info_from_bridge(self):
        self.assertEqual(self.b.x_info(), (":0", "/run/user/1000/.mutter-Xwaylandauth.AB12CD"))
        self.bridge.xinfo = ("", "")
        with env(DISPLAY=None, XAUTHORITY=None):
            # nothing from the bridge, nothing on this box -> None, no crash
            info = self.b.x_info()
        self.assertTrue(info is None or isinstance(info, tuple))

    def test_events_stream(self):
        gen = self.b.events(timeout=3)
        emitter = Bus(self.mock.address)

        def fire():
            time.sleep(0.3)
            emitter.emit_signal(OBJECT_PATH, IFACE, "WindowEvent", "ts", (XTERM, "focus"))
            emitter.emit_signal(OBJECT_PATH, IFACE, "WorkspaceEvent", "s", ("switch",))
            emitter.emit_signal(OBJECT_PATH, IFACE, "WindowEvent", "ts", (EDITOR, "close"))
        threading.Thread(target=fire, daemon=True).start()
        try:
            self.assertEqual(next(gen), (XTERM, "focus"))
            self.assertEqual(next(gen), (EDITOR, "close"))
        finally:
            gen.close()
            emitter.close()

    # -- errors

    def test_bridge_gone_is_a_clear_error(self):
        self.bridge.close()
        with self.assertRaises(CmdError) as cm:
            self.b.list()
        self.assertIn("bridge vanished", str(cm.exception))
        self.assertIn("install-bridge.sh --check", str(cm.exception))

    def test_unsupported_error_passes_the_bridge_message(self):
        with self.assertRaises(CmdError) as cm:
            self.b._call("SetNWorkspaces", "i", (4,))
        self.assertIn("dynamic workspaces are enabled", str(cm.exception))
        with self.assertRaises(CmdError) as cm:
            self.b._call("SetState", "tss", (XTERM, "FULLSCREEN", "flip"))
        self.assertIn("action must be add|remove|toggle", str(cm.exception))


class SessionReadinessTests(_Base):
    """B5: "there is no backend to talk to" is rc 2, distinct from rc 1 for
    "the session is up and nothing matched"."""

    def test_every_constructor_failure_is_a_no_session_error(self):
        cases = [
            dict(own_shell=True, own_bridge=False),                   # no bridge
            dict(own_shell=True, own_bridge=False, shell_mode="gdm"),  # greeter
            dict(own_shell=True, own_bridge=False,
                 shell_mode="unlock-dialog"),                          # locked
            dict(own_shell=False, own_bridge=False),                   # no shell
        ]
        for kw in cases:
            bridge = MockBridge(self.mock, **kw)
            try:
                with self.assertRaises(NoSessionError) as cm:
                    GnomeBackend()
                self.assertEqual(cm.exception.exit_code, 2, kw)
            finally:
                bridge.close()

    def test_a_missing_window_stays_rc_1(self):
        bridge = MockBridge(self.mock)
        try:
            b = GnomeBackend()
            self.addCleanup(b.bus.close)
            with self.assertRaises(CmdError) as cm:
                b.find(999)
            self.assertNotIsInstance(cm.exception, NoSessionError)
            self.assertEqual(getattr(cm.exception, "exit_code", 1), 1)
        finally:
            bridge.close()


class ConstructorTests(_Base):
    def test_no_bridge_gives_the_install_hint_without_touching_eval(self):
        # review finding 4: the common "installed, needs a re-login" path
        # must not probe org.gnome.Shell.Eval
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            msg = str(cm.exception)
            self.assertIn("gnome/install-bridge.sh", msg)
            self.assertIn("restart the session", msg)
            self.assertEqual([m for m, _ in bridge.calls], ["GetActive", "GetExtensionInfo"])
        finally:
            bridge.close()

    def test_eval_autoload_is_opt_in(self):
        # unsafe mode on, but nobody asked: still no Eval, still the hint
        bridge = MockBridge(self.mock, own_bridge=False, eval_unsafe=True)
        try:
            for value in (None, "", "0", "no"):
                with env(WDOTOOL_GNOME_AUTOLOAD=value):
                    with self.assertRaises(CmdError) as cm:
                        GnomeBackend()
                self.assertIn("gnome/install-bridge.sh", str(cm.exception))
            self.assertNotIn("Eval", [m for m, _ in bridge.calls])
        finally:
            bridge.close()

    def test_locked_screen_and_disabled_extension_are_diagnosed(self):
        cases = [
            (dict(shell_mode="unlock-dialog"), "screen locked"),
            (dict(shell_mode="unlock-dialog", ext_info={"uuid": EXT_UUID, "state": 2}), "screen locked"),
            (dict(shell_mode="gdm"), "GDM greeter"),
            (dict(ext_info={"uuid": EXT_UUID, "state": 2}), "installed but not enabled"),
            (dict(ext_info={"uuid": EXT_UUID, "state": 3, "error": "boom"}), "failed to load: boom"),
            (dict(ext_info={"uuid": EXT_UUID, "state": 4, "shell-version": "x"}), "out of date"),
            # the unlocked session's mode is "ubuntu" on Ubuntu, "classic" in
            # GNOME Classic: not a lock screen (observed live on 24.04)
            (dict(shell_mode="ubuntu", ext_info={"uuid": EXT_UUID, "state": 2}), "installed but not enabled"),
            (dict(shell_mode="classic", ext_info={"uuid": EXT_UUID, "state": 3, "error": "x"}), "failed to load"),
            (dict(shell_mode="ubuntu"), "gnome/install-bridge.sh"),
            # GNOME 46 keeps Mode at 'ubuntu' behind the lock screen and the
            # extension shows as merely INACTIVE: org.gnome.ScreenSaver tells
            (dict(shell_mode="ubuntu", screensaver_active=True,
                  ext_info={"uuid": EXT_UUID, "state": 2}), "screen is locked"),
            (dict(shell_mode="user", screensaver_active=True), "screen is locked"),
        ]
        for kw, expect in cases:
            bridge = MockBridge(self.mock, own_bridge=False, **kw)
            try:
                with self.assertRaises(CmdError) as cm:
                    GnomeBackend()
                self.assertIn(expect, str(cm.exception), kw)
                if kw.get("shell_mode") not in ("unlock-dialog", "gdm") \
                        and not kw.get("screensaver_active"):
                    self.assertNotIn("locked", str(cm.exception), kw)
            finally:
                bridge.close()

    def test_no_shell_at_all(self):
        bridge = MockBridge(self.mock, own_shell=False, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            self.assertIn("org.gnome.Shell is not on the session bus", str(cm.exception))
        finally:
            bridge.close()

    def test_eval_autoload_in_unsafe_mode_when_asked(self):
        bridge = MockBridge(self.mock, own_bridge=False, eval_unsafe=True)
        try:
            with env(WDOTOOL_GNOME_AUTOLOAD="1"):
                b = GnomeBackend()
            self.assertEqual(b.num_desktops(), 3)
            self.assertEqual([m for m, _ in bridge.calls][:2], ["Eval", "GetNWorkspaces"])
            self.assertIn("fuckwayland-bridge@fuckwayland", bridge.calls[0][1][0])
            b.bus.close()
        finally:
            bridge.close()

    def test_eval_autoload_asked_but_shell_not_unsafe(self):
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            with env(WDOTOOL_GNOME_AUTOLOAD="1"):
                with self.assertRaises(CmdError) as cm:
                    GnomeBackend()
            self.assertIn("gnome/install-bridge.sh", str(cm.exception))
            self.assertEqual([m for m, _ in bridge.calls],
                             ["Eval", "GetActive", "GetExtensionInfo"])
        finally:
            bridge.close()

    def test_no_session_bus(self):
        with no_bus():
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
        self.assertIn("no session D-Bus found", str(cm.exception))

    def test_reuses_detects_bus_and_names(self):
        bridge = MockBridge(self.mock)
        try:
            bus = Bus(self.mock.address)
            b = GnomeBackend(bus=bus, names=bus.list_names())
            self.assertIs(b.bus, bus)
            self.assertEqual(b.get_desktop(), 0)
            bus.close()
        finally:
            bridge.close()


class DetectTests(_Base):
    """backend_detect order: WDOTOOL_BACKEND -> sway socket -> KWin name ->
    GNOME name (never swallowed) -> wlr -> error, with one ListNames."""

    def setUp(self):
        backend_detect.reset()
        self._wlr = backend_detect._wlr
        self._wlr_calls = []

        def no_wlr():
            self._wlr_calls.append(1)
            raise CmdError("wlr: no foreign-toplevel")
        backend_detect._wlr = no_wlr

    def tearDown(self):
        backend_detect._wlr = self._wlr
        backend_detect.reset()

    def test_gnome_with_bridge(self):
        bridge = MockBridge(self.mock)
        try:
            b = backend_detect.detect()
            self.assertEqual(b.name, "gnome")
            self.assertIs(b.bus, backend_detect.session_bus())
            self.assertEqual(b.list()[-1].id, XTERM)
            # one ListNames for the whole detection (the constructor reused it)
            self.assertEqual(backend_detect.session_names().count(BUS_NAME), 1)
        finally:
            bridge.close()
        self.assertEqual(self._wlr_calls, [])

    def test_gnome_without_bridge_is_not_swallowed(self):
        bridge = MockBridge(self.mock, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                backend_detect.detect()
            self.assertIn("gnome/install-bridge.sh", str(cm.exception))
        finally:
            bridge.close()
        self.assertEqual(self._wlr_calls, [])

    def test_kwin_name_wins_over_gnome(self):
        bridge = MockBridge(self.mock)
        kwin = Bus(self.mock.address)
        try:
            self.assertEqual(kwin.request_name(backend_detect.KWIN_NAME), 1)
            b = backend_detect.detect()
            self.assertEqual(b.name, "kwin")
        finally:
            # the same two-event race MockBridge.close() waits out: the next
            # test asks whether anything at all is on the bus, and org.kde.KWin
            # has to be gone from the table before it does, not merely have had
            # its socket closed
            unique = kwin.unique_name
            kwin.close()
            self.assertTrue(self.mock.wait_dropped(unique))
            bridge.close()

    def test_sway_socket_wins_over_dbus(self):
        bridge = MockBridge(self.mock)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = os.path.join(self.rtdir, "sway-ipc.1000.7.sock")
        srv.bind(path)
        srv.listen(1)
        try:
            b = backend_detect.detect()
            self.assertEqual(b.name, "sway")
            b.sock.close()
        finally:
            srv.close()
            os.unlink(path)
            bridge.close()

    def test_env_override(self):
        bridge = MockBridge(self.mock)
        try:
            with env(WDOTOOL_BACKEND="gnome"):
                self.assertEqual(backend_detect.detect().name, "gnome")
            with env(WDOTOOL_BACKEND="bogus"):
                with self.assertRaises(CmdError) as cm:
                    backend_detect.detect()
            self.assertIn("WDOTOOL_BACKEND=bogus", str(cm.exception))
        finally:
            bridge.close()

    def test_nothing_on_the_bus_falls_to_wlr_then_errors(self):
        with self.assertRaises(CmdError) as cm:
            backend_detect.detect()
        self.assertEqual(self._wlr_calls, [1])
        self.assertIn("no KWin or GNOME Shell on the session D-Bus", str(cm.exception))

    def test_no_bus_reachable(self):
        with no_bus():
            with self.assertRaises(CmdError) as cm:
                backend_detect.detect()
        self.assertEqual(self._wlr_calls, [1])
        self.assertIn("no session D-Bus reachable", str(cm.exception))
        self.assertIsNone(backend_detect.session_names())
        self.assertFalse(backend_detect.dbus_name_has_owner(SHELL_NAME))


class ShippedFilesTests(unittest.TestCase):
    """Regressions on the files gnome/ ships: the udev rule grants nothing
    beyond the seat user's ACL, the installer restores the node, and the
    extension's embedded interface XML matches the .xml file (no WindowAt)."""

    GNOME = os.path.join(ROOT, "gnome")
    EXT = os.path.join(GNOME, "fuckwayland-bridge@fuckwayland")

    def test_udev_rule_is_uaccess_only(self):
        # review finding 1: MODE/GROUP would hand every `input` member a
        # standing injection channel; uaccess alone is what wdotool needs
        with open(os.path.join(self.GNOME, "60-fuckwayland-uinput.rules")) as f:
            rules = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertIn('KERNEL=="uinput"', rule)
        self.assertIn('TAG+="uaccess"', rule)
        self.assertIn('OPTIONS+="static_node=uinput"', rule)
        self.assertNotIn("MODE=", rule)
        self.assertNotIn("GROUP=", rule)
        self.assertNotIn("OWNER=", rule)

    def test_installer_parses_and_restores_the_node(self):
        import subprocess
        path = os.path.join(self.GNOME, "install-bridge.sh")
        self.assertEqual(subprocess.run(["sh", "-n", path]).returncode, 0)
        with open(path) as f:
            src = f.read()
        body = src[src.index("restore_uinput_node() {"):]
        body = body[:body.index("\n}\n")]
        for needle in ("setfacl -b /dev/uinput", "chown root:root /dev/uinput",
                       "chmod 0600 /dev/uinput"):
            self.assertIn(needle, body)
        uninstall = src[src.index('if [ "$MODE" = uninstall ]; then'):]
        uninstall = uninstall[:uninstall.index("return 0")]
        # files first, then udev forgets the node (sticky uaccess tag), then
        # the node's ACL/permissions -- and no trigger that could re-apply it
        self.assertLess(uninstall.index('rm -f "$UDEV_DEST"'), uninstall.index("forget_uinput_tags"))
        self.assertLess(uninstall.index("forget_uinput_tags"), uninstall.index("restore_uinput_node"))
        self.assertNotIn("udevadm trigger", uninstall)
        forget = src[src.index("forget_uinput_tags() {"):]
        forget = forget[:forget.index("\n}\n")]
        for needle in ('"/run/udev/data/c$maj:$min"', '/run/udev/tags/*/"c$maj:$min"',
                       "/run/udev/static_node-tags/uaccess/uinput", "udevadm info -q property"):
            self.assertIn(needle, forget)

    def _extension_js(self):
        with open(os.path.join(self.EXT, "extension.js")) as f:
            return f.read()

    def _select_window_source(self):
        """The SelectWindow region of extension.js (the picker and its
        teardown), so a test can say what is and is not in it."""
        js = self._extension_js()
        start = js.index("    // -- selectwindow ---")
        return js[start:js.index("    // -- window bookkeeping ---", start)]

    def test_select_window_no_longer_waits_for_a_focus_change(self):
        # The defect: waiting on the compositor's focus signal meant clicking
        # the window that already had focus never returned. Nothing in the
        # picker may look at focus again.
        block = self._select_window_source()
        for gone in ("notify::focus-window", "focus_window", "focus-window"):
            self.assertNotIn(gone, block)
        # ...and what replaced it: a grab, resolved by a button press, with
        # Escape and a cap as the ways out
        for needed in ("takeGrab(", "Clutter.EventType", "BUTTON_PRESS",
                       "Clutter.KEY_Escape", "SELECT_MAX_MS",
                       "_windowUnderPointer", "ERR_CANCELLED"):
            self.assertIn(needed, block)
        # the picker may only ever name a window ListWindows reports: Mutter's
        # raw list carries surfaces no other command would resolve
        hit = block[block.index("    _windowUnderPointer(x, y) {"):]
        self.assertIn("for (const d of this._listWindows())", hit)
        self.assertNotIn("_allWindows()", hit)

    def test_the_grab_is_feature_detected_and_always_released(self):
        js = self._extension_js()
        grab = js[js.index("function takeGrab("):js.index("\n}\n", js.index("function takeGrab("))]
        # both spellings, neither assumed to exist (46 vs 50)
        for needed in ("isFn(Main, 'pushModal')", "Main.popModal(grab)",
                       "isFn(global.stage, 'grab')", "grab.dismiss()"):
            self.assertIn(needed, grab)
        # a grab that came back dead is dismissed rather than returned
        self.assertIn("grabIsLive(grab)", grab)
        block = self._select_window_source()
        # the timeout is armed before the grab is taken, so a setup that
        # throws is already bounded
        self.assertLess(block.index("GLib.timeout_add"), block.index("_beginSelect(sel)"))
        # every acquisition has its release in the one teardown
        end = block[block.index("    _endSelect(sel, id, errName, errMsg) {"):]
        for needed in ("obj.disconnect(hid)", "GLib.source_remove(sel.timerId)",
                       "Gio.bus_unwatch_name(sel.watchId)", "sel.grab.release()",
                       "if (sel.done)"):
            self.assertIn(needed, end)
        # a caller that goes away (Ctrl-C) releases it too
        self.assertIn("bus_watch_name_on_connection", block)
        # ...as does disabling the extension
        self.assertIn("safe(() => finish(0, ERR_CANCELLED", js)

    def test_the_picker_refuses_rather_than_grabbing_over_a_modal(self):
        # A picker on top of the overview swallows the click and hit-tests it
        # against frame rects that are not on screen (measured: a window
        # nowhere near the pointer on GNOME 50, none at all on 46). Asking
        # pushModal does not settle it -- it nests on some releases -- so the
        # shell's own modal state is read, every signal feature-detected.
        js = self._extension_js()
        modal = js[js.index("function shellIsModal("):
                   js.index("\n}\n", js.index("function shellIsModal("))]
        for needed in ("Shell.ActionMode.NORMAL", "Main.actionMode",
                       "Main.modalCount", "Main.overview.visible"):
            self.assertIn(needed, modal)
        self.assertEqual(modal.count("safe(() =>"), 4)   # none of them assumed
        # ...and it is checked before anything is taken
        block = self._select_window_source()
        guard = block[:block.index("const asked")]
        self.assertIn("shellIsModal()", guard)
        self.assertIn("ERR_UNSUPPORTED", guard)
        # a pushModal that refuses is still final: no plain-stage-grab retry
        grab = js[js.index("function takeGrab("):js.index("\n}\n", js.index("function takeGrab("))]
        between = grab[grab.index("Main.pushModal("):
                       grab.index("isFn(global.stage, 'grab')")]
        self.assertIn("return null;", between)

    def test_only_one_picker_at_a_time(self):
        block = self._select_window_source()
        guard = block[:block.index("const asked")]
        self.assertIn("this._selects?.size", guard)
        self.assertIn("ERR_UNSUPPORTED", guard)

    def test_the_press_is_not_answered_until_its_release(self):
        # An application must not receive a button-release whose press it
        # never saw. The grab is kept for the matching release, bounded by
        # SELECT_RELEASE_MS, and exactly one timer is armed at a time so the
        # single source_remove in _endSelect stays sufficient.
        js = self._extension_js()
        self.assertIn("const SELECT_RELEASE_MS = ", js)
        block = self._select_window_source()
        for needed in ("BUTTON_RELEASE", "TOUCH_END", "sel.pick",
                       "SELECT_RELEASE_MS", "_pickAt(sel, event)"):
            self.assertIn(needed, block)
        pick = block[block.index("    _pickAt(sel, event) {"):]
        # the old deadline is removed before the new one is armed, and a
        # source that cannot be created answers instead of leaving the grab
        # unbounded
        self.assertLess(pick.index("GLib.source_remove(sel.timerId)"),
                        pick.index("GLib.timeout_add("))
        self.assertIn("sel.finish(sel.pick, null, null);",
                      pick[pick.index("if (id)"):])

    def test_the_picker_swallows_every_discrete_input_event(self):
        # A keystroke, a scroll or a touch aimed at the picker must not land
        # in the window under it; motion is deliberately let through.
        block = self._select_window_source()
        handler = block[block.index("    _onSelectEvent(sel, event) {"):
                        block.index("    _pickAt(sel, event) {")]
        for needed in ("T.KEY_RELEASE", "T.SCROLL", "T.TOUCH_UPDATE",
                       "T.TOUCH_CANCEL", "T.PAD_BUTTON_PRESS",
                       "T.PAD_BUTTON_RELEASE"):
            self.assertIn(needed, handler)
        self.assertNotIn("T.MOTION", handler)

    def test_bridge_version_is_bumped_everywhere(self):
        import json as _json
        js = self._extension_js()
        self.assertIn("const VERSION = %d;" % MockBridge.VERSION, js)
        with open(os.path.join(self.EXT, "metadata.json")) as f:
            meta = _json.load(f)
        self.assertEqual(meta["version"], MockBridge.VERSION)
        # the client refuses to hang on anything older
        self.assertEqual(backend_gnome.GnomeBackend._SELECT_MIN_VERSION,
                         MockBridge.VERSION)
        self.assertIn("const SELECT_MAX_MS = %d;" % MockBridge.SELECT_MAX_MS, js)

    def test_embedded_xml_matches_file_and_has_no_hit_test(self):
        with open(os.path.join(self.EXT, "org.fuckwayland.Bridge1.xml")) as f:
            xml = f.read().strip()
        with open(os.path.join(self.EXT, "extension.js")) as f:
            js = f.read()
        start = js.index("const IFACE_XML = `") + len("const IFACE_XML = `")
        embedded = js[start:js.index("`;", start)].strip()
        self.assertEqual(embedded, xml)
        self.assertNotIn("WindowAt", xml)
        self.assertNotIn("_windowAt", js)
        self.assertIn('<method name="GetPointer">', xml)
        for member in ("ListWindows", "GetWindow", "SetState", "SelectWindow",
                       "DisplaySize", "XInfo", "GetVersion"):
            self.assertIn('<method name="%s">' % member, xml)


class SessionTests(unittest.TestCase):
    """The additive session.py pieces: runtime-dir anchoring on the Wayland
    socket, PKEXEC_UID, X display / Xauthority discovery."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wdotool-sess-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dir_with_wayland_socket_sorts_first(self):
        a = os.path.join(self.tmp, "a")
        os.makedirs(a)
        with env(XDG_RUNTIME_DIR=a, PKEXEC_UID=None, SUDO_UID=None):
            cands = session.runtime_dir_candidates()
            self.assertEqual(cands[0][1], a)  # nothing has a socket: env dir first
            self.assertFalse(session._has_wayland_socket(a))
            # a wayland socket anywhere else beats an empty $XDG_RUNTIME_DIR
            have = [d for _u, d in cands[1:] if session._has_wayland_socket(d)]
            if have:
                with open(os.path.join(a, "wayland-9"), "w"):
                    pass
                self.assertEqual(session.runtime_dir_candidates()[0][1], a)

    def test_find_user_bus_prefers_the_bus_next_to_the_wayland_socket(self):
        """ssh root@box: $DBUS_SESSION_BUS_ADDRESS is root's own empty bus in
        /run/user/0; the desktop user's bus sits next to wayland-0."""
        root_dir = os.path.join(self.tmp, "0")
        user_dir = os.path.join(self.tmp, "1000")
        os.makedirs(root_dir)
        os.makedirs(user_dir)
        for p in (os.path.join(root_dir, "bus"), os.path.join(user_dir, "bus"),
                  os.path.join(user_dir, "wayland-0")):
            with open(p, "w"):
                pass
        root_bus = "unix:path=" + os.path.join(root_dir, "bus")
        user_bus = "unix:path=" + os.path.join(user_dir, "bus")
        # env bus without a compositor next to it, session dir scanned via XDG
        with env(DBUS_SESSION_BUS_ADDRESS=root_bus, XDG_RUNTIME_DIR=user_dir,
                 SUDO_UID=None, PKEXEC_UID=None):
            self.assertEqual(session.find_user_bus()[1], user_bus)
        # env bus next to a wayland socket wins outright
        with env(DBUS_SESSION_BUS_ADDRESS=user_bus, XDG_RUNTIME_DIR=root_dir,
                 SUDO_UID=None, PKEXEC_UID=None):
            self.assertEqual(session.find_user_bus()[1], user_bus)
        # no wayland socket anywhere in play: the env bus is trusted
        os.unlink(os.path.join(user_dir, "wayland-0"))
        with env(DBUS_SESSION_BUS_ADDRESS=root_bus, XDG_RUNTIME_DIR=user_dir,
                 SUDO_UID=None, PKEXEC_UID=None):
            got = session.find_user_bus()[1]
        self.assertIn(got, (root_bus,) + tuple(
            "unix:path=" + d + "/bus" for _u, d in session.runtime_dir_candidates()
            if session._has_wayland_socket(d)))

    def test_pkexec_uid(self):
        with env(SUDO_UID=None, PKEXEC_UID="4242"):
            self.assertEqual(session._sudo_uid(), 4242)
        with env(SUDO_UID="7", PKEXEC_UID="4242"):
            self.assertEqual(session._sudo_uid(), 7)
        with env(SUDO_UID="x", PKEXEC_UID=None):
            self.assertIsNone(session._sudo_uid())

    def test_find_xauthority_prefers_env_then_mutter_cookie(self):
        cookie_old = os.path.join(self.tmp, ".mutter-Xwaylandauth.OLD")
        cookie_new = os.path.join(self.tmp, ".mutter-Xwaylandauth.NEW")
        for p, t in ((cookie_old, 1000), (cookie_new, 2000)):
            with open(p, "w"):
                pass
            os.utime(p, (t, t))
        with env(XAUTHORITY=None, XDG_RUNTIME_DIR=self.tmp, SUDO_UID=None, PKEXEC_UID=None):
            self.assertEqual(session.find_xauthority(os.getuid()), cookie_new)
        with env(XAUTHORITY=cookie_old):
            self.assertEqual(session.find_xauthority(), cookie_old)
        with env(XAUTHORITY="/nonexistent/xauth", XDG_RUNTIME_DIR=self.tmp,
                 SUDO_UID=None, PKEXEC_UID=None):
            self.assertEqual(session.find_xauthority(os.getuid()), cookie_new)

    def test_find_x_display_env(self):
        with env(DISPLAY=":424242"):
            # no socket for it -> not trusted; falls through to the scan
            r = session.find_x_display(uid=0x7fffffff)
            self.assertTrue(r is None or r.startswith(":"))
        with env(DISPLAY="host:0"):
            r = session.find_x_display(uid=0x7fffffff)
            self.assertTrue(r is None or r.startswith(":"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
