#!/usr/bin/env python3
"""GNOME backend tests against a mock fuckwayland bridge served on
dbus_mini's in-process mock bus (tests/test_dbus_mini.py MockBus): every
GnomeBackend method, the Window/View/Workspace mapping, error mapping, the
pointer hit-test, the Eval auto-load path, and backend_detect's order for
each ListNames outcome. No GNOME, no real bus needed."""

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
from wdotool.backend_gnome import (BUS_NAME, IFACE, OBJECT_PATH,      # noqa: E402
                                   SHELL_NAME, GnomeBackend)
from wdotool.ctx import CmdError                                       # noqa: E402
from wdotool.dbus_mini import Bus, DBusError, Message, Variant         # noqa: E402

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

    def __init__(self, address, own_shell=True, own_bridge=True,
                 eval_unsafe=False, select_delay=0.2, select_id=EDITOR):
        self.bus = Bus(address)
        self.bus.serve_calls = True
        self.windows = fixture_windows()
        self.calls = []
        self.active_ws = 0
        self.n_ws = 3
        self.eval_unsafe = eval_unsafe
        self.select_delay = select_delay
        self.select_id = select_id
        self.xinfo = (":0", "/run/user/1000/.mutter-Xwaylandauth.AB12CD")
        self.pointer = (640, 400, 0)
        if own_shell:
            assert self.bus.request_name(SHELL_NAME) == 1
        if own_bridge:
            assert self.bus.request_name(BUS_NAME) == 1
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        if self.bus.sock is None:
            return
        try:
            self.bus.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(3)
        self.bus.close()

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
                return "v", (Variant("u", 1),)
            raise DBusError(dbus_mini.ERR + "InvalidArgs", "no such property")
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

    def m_SelectWindow(self, m, timeout_ms):
        time.sleep(self.select_delay)
        return "t", (self.select_id,)

    def m_WindowAt(self, m, x, y):
        for d in reversed(self.windows):
            if d["window_type"] == "DESKTOP" or d["hidden"]:
                continue
            if d["x"] <= x < d["x"] + d["width"] and d["y"] <= y < d["y"] + d["height"]:
                return "t", (d["id"],)
        return "t", (0,)

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
        return "u", (1,)


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
        self.bridge = MockBridge(self.mock.address)
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
                                       pid=1201, x=100, y=80, w=640, h=480,
                                       focused=True, visible=True, desktop=0))
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
        for state in ("SKIP_TASKBAR", "SKIP_PAGER", "SHADED", "MODAL"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.b.set_state(XTERM, state, 1)
            self.assertIn("windowstate %s" % state, err.getvalue())
            self.assertIn("ignoring", err.getvalue())
        # but the window must exist
        with self.assertRaises(CmdError):
            self.b.set_state(999, "SHADED", 1)

    def test_set_state_capability_gaps_raise(self):
        for state in ("BELOW", "FOO"):
            with self.assertRaises(CmdError) as cm:
                self.b.set_state(XTERM, state, 1)
            self.assertIn("windowstate %s is not supported" % state, str(cm.exception))
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

    def test_display_size_and_extras(self):
        self.assertEqual(self.b.display_size(), (1920, 1080))
        self.assertEqual(self.b.real_pointer(), (640, 400))
        self.assertEqual(self.b.bridge_version(), 1)
        mons = self.b.monitors()
        self.assertEqual((mons[0]["connector"], mons[0]["primary"]), ("Virtual-1", True))

    def test_select_window_blocks_for_the_next_focus(self):
        t0 = time.monotonic()
        self.assertEqual(self.b.select_window(), EDITOR)
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)
        self.assertEqual(self.calls("SelectWindow"), [(0,)])
        self.bridge.select_id = 0
        with self.assertRaises(CmdError):
            self.b.select_window()

    # -- pointer hit-test

    def test_window_at_skips_desktop_hidden_and_other_workspaces(self):
        self.assertEqual(self.b.window_at(150, 100), XTERM)
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


class ConstructorTests(_Base):
    def test_no_bridge_no_unsafe_mode_gives_the_install_hint(self):
        bridge = MockBridge(self.mock.address, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            msg = str(cm.exception)
            self.assertIn("gnome/install-bridge.sh", msg)
            self.assertIn("restart the session", msg)
            self.assertEqual([m for m, _ in bridge.calls], ["Eval"])
        finally:
            bridge.close()

    def test_no_shell_at_all(self):
        bridge = MockBridge(self.mock.address, own_shell=False, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
            self.assertIn("org.gnome.Shell is not on the session bus", str(cm.exception))
        finally:
            bridge.close()

    def test_eval_autoload_in_unsafe_mode(self):
        bridge = MockBridge(self.mock.address, own_bridge=False, eval_unsafe=True)
        try:
            b = GnomeBackend()
            self.assertEqual(b.num_desktops(), 3)
            self.assertEqual([m for m, _ in bridge.calls][:2], ["Eval", "GetNWorkspaces"])
            self.assertIn("fuckwayland-bridge@fuckwayland", bridge.calls[0][1][0])
            b.bus.close()
        finally:
            bridge.close()

    def test_no_session_bus(self):
        with no_bus():
            with self.assertRaises(CmdError) as cm:
                GnomeBackend()
        self.assertIn("no session D-Bus found", str(cm.exception))

    def test_reuses_detects_bus_and_names(self):
        bridge = MockBridge(self.mock.address)
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
        bridge = MockBridge(self.mock.address)
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
        bridge = MockBridge(self.mock.address, own_bridge=False)
        try:
            with self.assertRaises(CmdError) as cm:
                backend_detect.detect()
            self.assertIn("gnome/install-bridge.sh", str(cm.exception))
        finally:
            bridge.close()
        self.assertEqual(self._wlr_calls, [])

    def test_kwin_name_wins_over_gnome(self):
        bridge = MockBridge(self.mock.address)
        kwin = Bus(self.mock.address)
        try:
            self.assertEqual(kwin.request_name(backend_detect.KWIN_NAME), 1)
            b = backend_detect.detect()
            self.assertEqual(b.name, "kwin")
        finally:
            kwin.close()
            bridge.close()

    def test_sway_socket_wins_over_dbus(self):
        bridge = MockBridge(self.mock.address)
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
        bridge = MockBridge(self.mock.address)
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
