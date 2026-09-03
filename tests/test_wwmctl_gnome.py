#!/usr/bin/env python3
"""wwmctl on GNOME: the views()/workspaces()/x_info() route over the mock
fuckwayland bridge (tests/test_backend_gnome.py's MockBridge on the
in-process mock bus), with the X plane faked the way test_wwmctl_cli does.

Covers: -l/-lp/-lG/-lx column parity with X ids for XWayland windows and
bridge ids for native ones, X-plane enrichment through the bridge's
DISPLAY/XAUTHORITY, -d from ListWorkspaces, -m with and without Xwayland,
every action's bridge call (-a/-c/-R/-t/-e/-b/-s/-k/-n/-o/-g), -N/-I/-T on
X vs native windows, -i with either id, :SELECT:/:ACTIVE:, the exit codes
and error strings of the bridge-less / bridge-gone paths, and the end-to-end
detection wiring (backend_detect over the mock bus)."""

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_backend_gnome import (CALC, DESKTOP, EDITOR, XTERM, XTERM_XID,  # noqa: E402
                                MockBridge, _Base)
from wdotool import backend_detect, session  # noqa: E402
from wdotool.backend_gnome import IFACE, OBJECT_PATH, GnomeBackend  # noqa: E402
from wwmctl import cli, core  # noqa: E402

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

WM_CHECK = 0x200001
XAUTH = "/run/user/1000/.mutter-Xwaylandauth.AB12CD"


class FakeX11:
    """The x11_mini surface wwmctl.core uses, shaped like Mutter's
    Xwayland: a check window named "GNOME Shell" without WM_CLASS/_NET_WM_PID
    (real wmctrl -m prints N/A for both there), _NET_SHOWING_DESKTOP on the
    root, and the xterm's client rectangle one titlebar below its frame."""

    TITLEBAR = 37  # Mutter's SSD bar: the client sits one bar below the frame

    def __init__(self, showing=0):
        self.calls = []
        self.showing = showing
        self.bridge = None  # the harness' bridge: frames to derive from

    def root(self):
        return 0x1C5

    def get_wm_class(self, win):
        return ("xterm", "XTerm") if win == XTERM_XID else ("", "")

    def get_client_machine(self, win):
        return "vmhost" if win == XTERM_XID else ""

    def get_geometry(self, win):
        # the xterm's client rectangle: the bridge's frame minus the SSD bar
        # (100,117 640x443 for the fixture's frame at 100,80 640x480)
        if self.bridge is None or win != XTERM_XID:
            return (100, 117, 640, 443)
        d = self.bridge.find(XTERM)
        return (d["x"], d["y"] + self.TITLEBAR, d["width"],
                d["height"] - self.TITLEBAR)

    def get_pid(self, win):
        return 1201 if win == XTERM_XID else 0

    def get_prop_ints(self, win, name):
        if (win, name) == (0x1C5, "_NET_SUPPORTING_WM_CHECK"):
            return [WM_CHECK]
        if (win, name) == (0x1C5, "_NET_SHOWING_DESKTOP"):
            return [self.showing]
        return []

    def get_prop_string(self, win, name):
        if (win, name) == (WM_CHECK, "_NET_WM_NAME"):
            return "GNOME Shell"
        return ""

    def set_name(self, win, name, icon, long_):
        self.calls.append(("set_name", win, name, icon, long_))

    def close(self):
        pass


class GnomeCliBase(_Base):
    """A mock bridge per test and a GnomeBackend on it; `run` drives the
    CLI with the X plane faked and records what _x11_connect was asked."""

    def setUp(self):
        self.bridge = MockBridge(self.mock.address, select_id=EDITOR,
                                 select_delay=0.05)
        self.backend = GnomeBackend(settle=0.05)
        self.x_calls = []

    def tearDown(self):
        self.backend.bus.close()
        self.bridge.close()

    def calls(self, member):
        return [a for m, a in self.bridge.calls if m == member]

    def wm(self, argv, x11="auto", xwayland=None, detect=None):
        """x11: FakeX11 to hand out, None for "unreachable", "auto" for a
        FakeX11 when an X window is listed; xwayland: what
        session.xwayland_running() reports (None: leave it to the box)."""
        if x11 == "auto":
            x11 = FakeX11() if any(d["xid"] for d in self.bridge.windows) \
                else None
        if x11 is not None:
            x11.bridge = self.bridge
        self.x11 = x11

        def connect(display=None, xauthority=None):
            self.x_calls.append((display, xauthority))
            return x11

        patches = [
            mock.patch.object(core, "_detect_backend",
                              detect or (lambda: self.backend)),
            mock.patch.object(core, "_x11_connect", connect),
            mock.patch.object(core, "_hostname", lambda: "testhost"),
            mock.patch.object(sys, "argv", ["wmctrl"]),
        ]
        if xwayland is not None:
            patches.append(mock.patch.object(session, "xwayland_running",
                                             lambda uid=None: xwayland))
        out, err = io.StringIO(), io.StringIO()
        for p in patches:
            p.start()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(list(argv))
        finally:
            for p in patches:
                p.stop()
        return rc, out.getvalue(), err.getvalue()


class ListingTests(GnomeCliBase):
    def test_l_ids_planes_and_columns(self):
        rc, out, err = self.wm(["-l"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        # stacking order bottom->top, X ids for XWayland, bridge ids native
        self.assertEqual(out.splitlines(), [
            "0x003ffffd  0 testhost Desktop",
            "0x00400002  0 testhost Untitled Document 1 - Text Editor",
            "0x00400003  1 testhost Calculator",
            "0x00400005  0 testhost test@vm: ~",
        ])
        # one listing round trip, then XInfo for the X plane (an XWayland
        # window is listed) -- which this run leaves unreachable
        self.assertEqual([m for m, _ in self.bridge.calls],
                         ["ListWorkspaces", "ListWindows", "XInfo"])

    def test_lpGx_without_x_plane(self):
        rc, out, _e = self.wm(["-lpGx"], x11=None)
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "0x003ffffd  0 900    0    0    1920 1080 Gjs.Gjs               "
            "testhost Desktop",
            "0x00400002  0 1300   300  200  800  600  "
            "org.gnome.TextEditor.org.gnome.TextEditor  "
            "testhost Untitled Document 1 - Text Editor",
            "0x00400003  1 1400   500  300  400  500  "
            "org.gnome.Calculator.org.gnome.Calculator  testhost Calculator",
            # the bridge's WM_CLASS pair stands in when X is unreachable
            "0x00400005  0 1201   100  80   640  480  xterm.XTerm           "
            "testhost test@vm: ~",
        ])

    def test_l_x_enrichment_uses_the_bridge_display_and_cookie(self):
        rc, out, _e = self.wm(["-lGx"])
        self.assertEqual(rc, 0)
        # one X connection, opened with what XInfo said
        self.assertEqual(self.x_calls, [(":0", XAUTH)])
        lines = out.splitlines()
        # machine column: WM_CLIENT_MACHINE from X for the xterm, hostname
        # for native windows; width = the LAST row's machine (wmctrl quirk)
        self.assertEqual(lines[-1], "0x00400005  0 100  117  640  443  "
                                    "xterm.XTerm           vmhost test@vm: ~")
        self.assertEqual(lines[0], "0x003ffffd  0 0    0    1920 1080 "
                                   "Gjs.Gjs               testhost Desktop")

    def test_no_x_windows_means_no_x_connection(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        rc, out, _e = self.wm(["-lx"], x11=FakeX11())
        self.assertEqual(rc, 0)
        self.assertEqual(self.x_calls, [])  # Xwayland is never spawned
        self.assertEqual(len(out.splitlines()), 3)

    def test_x_info_falls_back_to_the_session_scan(self):
        self.bridge.xinfo = ("", "")
        with mock.patch.object(session, "find_x_display", lambda uid=None: ":7"), \
                mock.patch.object(session, "find_xauthority",
                                  lambda uid=None: "/rt/.mutter-Xwaylandauth.Z"):
            rc, _o, _e = self.wm(["-l"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.x_calls, [(":7", "/rt/.mutter-Xwaylandauth.Z")])

    def test_sticky_window_prints_desktop_minus_one(self):
        self.bridge.find(CALC).update(on_all_workspaces=True, workspace=-1)
        rc, out, _e = self.wm(["-l"], x11=None)
        self.assertIn("0x00400003 -1 testhost Calculator", out.splitlines())


class DesktopTests(GnomeCliBase):
    def test_d_from_list_workspaces(self):
        rc, out, err = self.wm(["-d"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.splitlines(), [
            "0  * DG: 1920x1080  VP: 0,0  WA: 0,32 1920x1048  Workspace 1",
            "1  - DG: 1920x1080  VP: N/A  WA: 0,32 1920x1048  Workspace 2",
            "2  - DG: 1920x1080  VP: N/A  WA: 0,32 1920x1048  Workspace 3",
        ])
        self.assertIn("DisplaySize", [m for m, _ in self.bridge.calls])
        self.assertIn("ListWorkspaces", [m for m, _ in self.bridge.calls])

    def test_d_nameless_workspace_prints_its_index(self):
        orig = self.bridge.m_ListWorkspaces

        def nameless(m):
            import json
            sig, (raw,) = orig(m)
            rows = json.loads(raw)
            rows[1]["name"] = ""
            return sig, (json.dumps(rows),)
        self.bridge.m_ListWorkspaces = nameless
        rc, out, _e = self.wm(["-d"], x11=None)
        self.assertEqual(out.splitlines()[1],
                         "1  - DG: 1920x1080  VP: N/A  WA: 0,32 1920x1048  1")

    def test_s(self):
        rc, _o, err = self.wm(["-s", "2"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("SetActiveWorkspace"), [(2,)])
        rc, _o, err = self.wm(["-s", "9"], x11=None)
        self.assertEqual((rc, err), (1, "workspace 9 not found\n"))
        rc, _o, err = self.wm(["-s", "-1"], x11=None)
        self.assertEqual((rc, err), (1, "Invalid desktop ID.\n"))

    def test_k_goes_through_show_desktop(self):
        rc, _o, err = self.wm(["-k", "on"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, err = self.wm(["-k", "off"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("ShowDesktop"), [(True,), (False,)])
        rc, _o, err = self.wm(["-k", "maybe"], x11=None)
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls("ShowDesktop"), [(True,), (False,)])

    def test_n_dynamic_workspaces_warns_and_succeeds(self):
        rc, _o, err = self.wm(["-n", "5"], x11=None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("SetNWorkspaces"), [(5,)])
        self.assertTrue(err.startswith("wwmctl: dynamic workspaces are enabled"))
        self.assertTrue(err.rstrip().endswith("; ignoring"))
        self.assertEqual(err.count("\n"), 1)

    def test_o_and_g_still_warn(self):
        for argv in (["-o", "0,0"], ["-g", "1920,1080"]):
            rc, _o, err = self.wm(argv, x11=None)
            self.assertEqual(rc, 0)
            self.assertIn("ignoring", err)
        self.assertNotIn("ShowDesktop", [m for m, _ in self.bridge.calls])


class WmInfoTests(GnomeCliBase):
    EXPECT = ("Name: GNOME Shell\nClass: N/A\nPID: N/A\n"
              'Window manager\'s "showing the desktop" mode: %s\n')

    def test_m_from_the_x_plane_when_xwayland_is_up(self):
        rc, out, _e = self.wm(["-m"], x11=FakeX11(showing=1))
        self.assertEqual((rc, out), (0, self.EXPECT % "ON"))
        self.assertEqual(self.x_calls, [(":0", XAUTH)])

    def test_m_from_the_bridge_when_xwayland_is_down(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        rc, out, _e = self.wm(["-m"], x11=FakeX11(), xwayland=False)
        self.assertEqual((rc, out), (0, self.EXPECT % "N/A"))
        self.assertEqual(self.x_calls, [])  # nothing spawned Xwayland

    def test_m_xwayland_up_without_x_windows_still_asks_x(self):
        self.bridge.windows = [d for d in self.bridge.windows if not d["xid"]]
        rc, out, _e = self.wm(["-m"], x11=FakeX11(), xwayland=True)
        self.assertEqual((rc, out), (0, self.EXPECT % "OFF"))
        self.assertEqual(self.x_calls, [(":0", XAUTH)])


class ActionTests(GnomeCliBase):
    def test_a_by_title_and_by_either_id(self):
        rc, _o, err = self.wm(["-a", "calcu"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, _e = self.wm(["-i", "-a", "0x400005"], x11=None)  # X id
        self.assertEqual(rc, 0)
        rc, _o, _e = self.wm(["-i", "-a", "%d" % XTERM], x11=None)  # bridge id
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("Activate"), [(CALC,), (XTERM,), (XTERM,)])
        rc, _o, err = self.wm(["-i", "-a", "0x999"], x11=None)
        self.assertEqual((rc, err), (1, ""))  # silent exit 1, like wmctrl

    def test_x_matches_class_pairs(self):
        rc, _o, _e = self.wm(["-x", "-a", "TextEditor"], x11=None)
        self.assertEqual(self.calls("Activate"), [(EDITOR,)])
        rc, _o, _e = self.wm(["-x", "-F", "-a", "xterm.XTerm"], x11=None)
        self.assertEqual(self.calls("Activate"), [(EDITOR,), (XTERM,)])

    def test_c_and_active_magic(self):
        rc, _o, _e = self.wm(["-c", ":ACTIVE:"], x11=None)
        self.assertEqual((rc, self.calls("Close")), (0, [(XTERM,)]))
        self.assertNotIn(XTERM, [d["id"] for d in self.bridge.windows])

    def test_select_magic_waits_for_the_next_focus(self):
        rc, _o, err = self.wm(["-a", ":SELECT:"], x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("focus the target window to select it", err)
        self.assertEqual(self.calls("SelectWindow"), [(0,)])
        self.assertEqual(self.calls("Activate"), [(EDITOR,)])

    def test_R_moves_to_the_current_desktop_then_activates(self):
        self.bridge.active_ws = 2
        self.bridge._refresh_active()
        with mock.patch.object(core.time, "sleep") as sl:
            rc, _o, _e = self.wm(["-R", "Calculator"], x11=None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls("MoveToWorkspace"), [(CALC, 2)])
        self.assertEqual(self.calls("Activate"), [(CALC,)])
        sl.assert_called()  # the non-sway grace period

    def test_t(self):
        rc, _o, err = self.wm(["-r", "Calculator", "-t", "2"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, err = self.wm(["-r", "Calculator", "-t", "-1"], x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("MoveToWorkspace"), [(CALC, 2), (CALC, 0)])
        rc, _o, err = self.wm(["-r", "Calculator", "-t", "7"], x11=None)
        self.assertEqual((rc, err), (1, "workspace 7 not found\n"))

    def test_e_client_rectangle_with_the_frame_extents(self):
        # gravity 0 (the window's own: NorthWest): the frame's top-left at
        # X,Y; W,H are the CLIENT size, so the frame is one SSD bar taller
        # (the X plane says the client sits 37 px below the frame)
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "0,10,20,300,200"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize"), [(XTERM, 300, 237)])
        self.assertEqual(self.calls("Move"), [(XTERM, 10, 20)])
        # -1 keeps the current value: the frame stays at y=20 and 300 wide
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "0,50,-1,-1,-1"])
        self.assertEqual(self.calls("Move")[-1], (XTERM, 50, 20))
        self.assertEqual(len(self.calls("Resize")), 1)
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "0,-1,-1,-1,700"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 300, 737))
        self.assertEqual(len(self.calls("Move")), 2)
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "1,2"])
        self.assertEqual(rc, 1)
        self.assertIn("gravity,X,Y,width,height", err)

    def test_e_gravity_places_the_named_frame_point(self):
        # frame 100,80 640x480 over client 100,117 640x443: extents
        # 0,37,0,0.  SouthEast: the frame's bottom-right corner lands on
        # the requested client rectangle's, (X+W, Y+H)
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,1000,900,300,200"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 300, 237))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 1000, 863))
        # Center: the frame centred on the requested rectangle's centre
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "5,500,400,300,200"])
        self.assertEqual(self.calls("Move")[-1], (XTERM, 500, 382))
        # Static: the client itself at X,Y, the frame one bar higher; the
        # -1s keep the client size
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "10,200,300,-1,-1"])
        self.assertEqual(self.calls("Move")[-1], (XTERM, 200, 263))
        self.assertEqual(len(self.calls("Resize")), 2)
        # Static with -1 for the position keeps the frame where it is
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "10,-1,-1,400,100"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 400, 137))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 200, 263))

    def test_e_bare_resize_keeps_the_gravity_point(self):
        # `-e 9,-1,-1,W,H` pins the frame's bottom-right corner and grows
        # up and to the left, as Mutter does for real wmctrl (live: a
        # frame 200,150 500x400 asked for a 300x250 client keeps its
        # 700,550 corner and lands at 400,263 with a 300x287 frame)
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,-1,-1,300,250"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize"), [(XTERM, 300, 287)])
        self.assertEqual(self.calls("Move"), [(XTERM, 440, 273)])  # 740,560
        # Center keeps the frame's centre (440+150, 273+143 = 590, 416)
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "5,-1,-1,200,150"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 200, 187))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 490, 323))
        # NorthWest pins the top-left, so a bare resize is a resize alone
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "0,-1,-1,300,200"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 300, 237))
        self.assertEqual(len(self.calls("Move")), 2)
        # ... and a narrower window under East keeps its right edge (790)
        # while the unchanged height leaves the vertical centre alone
        rc, _o, _e = self.wm(["-r", "test@vm", "-e", "6,-1,-1,150,-1"])
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 150, 237))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 640, 323))

    def test_e_without_frame_extents_is_the_frame_rectangle(self):
        # a native window: the frame is the client, so every gravity puts
        # X,Y,W,H on the rectangle -lG prints
        for grav in ("0", "5", "9", "10"):
            rc, _o, err = self.wm(["-r", "Calculator", "-e",
                                   "%s,100,100,320,240" % grav], x11=None)
            self.assertEqual((rc, err), (0, ""), grav)
            self.assertEqual(self.calls("Resize")[-1], (CALC, 320, 240), grav)
            self.assertEqual(self.calls("Move")[-1], (CALC, 100, 100), grav)
        # an XWayland window with the X plane unreachable: the bridge's
        # frame rect is all there is, so it is addressed directly
        rc, _o, err = self.wm(["-r", "test@vm", "-e", "9,10,20,300,200"],
                              x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("Resize")[-1], (XTERM, 300, 200))
        self.assertEqual(self.calls("Move")[-1], (XTERM, 10, 20))

    def test_b_states_through_set_state(self):
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "add,fullscreen"],
                               x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, err = self.wm(
            ["-r", "Calculator", "-b", "toggle,maximized_vert,maximized_horz"],
            x11=None)
        self.assertEqual((rc, err), (0, ""))
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "remove,hidden"],
                               x11=None)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.calls("SetState"), [
            (CALC, "FULLSCREEN", "add"), (CALC, "MAXIMIZED_VERT", "toggle"),
            (CALC, "MAXIMIZED_HORZ", "toggle"), (CALC, "HIDDEN", "remove")])
        d = self.bridge.find(CALC)
        self.assertTrue(d["fullscreen"] and d["maximized_v"] and d["maximized_h"])

    def test_b_gaps_warn_and_succeed(self):
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "add,shaded,below"],
                               x11=None)
        self.assertEqual(rc, 0)
        self.assertEqual(err.count("; ignoring"), 2)
        self.assertIn("SHADED", err)
        self.assertIn("BELOW", err)
        rc, _o, err = self.wm(["-r", "Calculator", "-b", "add,skip_taskbar"],
                               x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_N_I_T_on_an_x_window_go_to_the_x_plane(self):
        for mode, icon, long_ in (("N", False, True), ("I", True, False),
                                  ("T", True, True)):
            x11 = FakeX11()
            rc, _o, err = self.wm(["-r", "test@vm", "-%s" % mode, "New"],
                                   x11=x11)
            self.assertEqual((rc, err), (0, ""), mode)
            self.assertEqual(x11.calls,
                             [("set_name", XTERM_XID, "New", icon, long_)])
        self.assertEqual(self.x_calls, [(":0", XAUTH)] * 3)

    def test_N_on_a_native_window_warns_and_succeeds(self):
        rc, _o, err = self.wm(["-r", "Calculator", "-N", "New"], x11=FakeX11())
        self.assertEqual(rc, 0)
        self.assertIn("native window", err)
        self.assertIn("ignoring", err)
        self.assertEqual(self.x11.calls, [])

    def test_N_on_x_window_with_x_unreachable_warns(self):
        rc, _o, err = self.wm(["-r", "test@vm", "-N", "New"], x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("cannot reach the XWayland server", err)


class ErrorPathTests(_Base):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(core, "_x11_connect",
                               lambda display=None, xauthority=None: None), \
                mock.patch.object(sys, "argv", ["wmctrl"]), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def setUp(self):
        backend_detect.reset()

    def tearDown(self):
        backend_detect.reset()

    def test_real_detection_over_the_mock_bus(self):
        bridge = MockBridge(self.mock.address)
        try:
            rc, out, err = self._run(["-l"])
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(len(out.splitlines()), 4)
            self.assertIn("0x00400005  0 ", out)
        finally:
            bridge.close()

    def test_bridge_not_installed_is_one_clear_line(self):
        bridge = MockBridge(self.mock.address, own_bridge=False)
        try:
            for argv in (["-l"], ["-d"], ["-m"], ["-a", "x"], ["-s", "1"]):
                backend_detect.reset()
                rc, out, err = self._run(argv)
                self.assertEqual((rc, out), (1, ""), argv)
                self.assertEqual(err.count("\n"), 1, err)
                self.assertIn("gnome/install-bridge.sh", err)
        finally:
            bridge.close()

    def test_bridge_gone_mid_session(self):
        bridge = MockBridge(self.mock.address)
        b = GnomeBackend(settle=0.05)
        bridge.close()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(core, "_detect_backend", lambda: b), \
                mock.patch.object(sys, "argv", ["wmctrl"]), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["-l"])
        b.bus.close()
        self.assertEqual(rc, 1)
        self.assertIn("bridge vanished", err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_k_and_n_without_any_session_still_warn_and_succeed(self):
        # no bridge, no shell: the warn-and-succeed fallbacks need no session
        with mock.patch.object(core, "_detect_backend",
                               mock.Mock(side_effect=core.CmdError("nope"))):
            for argv in (["-k", "on"], ["-n", "3"]):
                rc, _o, err = self._run(argv)
                self.assertEqual(rc, 0, argv)
                self.assertIn("ignoring", err)


class EventsHookTests(_Base):
    def test_workspace_events_are_folded_in_on_request(self):
        import threading
        import time
        from wdotool.dbus_mini import Bus
        bridge = MockBridge(self.mock.address)
        b = GnomeBackend(settle=0.05)
        emitter = Bus(self.mock.address)
        try:
            gen = b.events(timeout=3, workspaces=True)

            def fire():
                time.sleep(0.2)
                emitter.emit_signal(OBJECT_PATH, IFACE, "WorkspaceEvent", "s", ("switch",))
                emitter.emit_signal(OBJECT_PATH, IFACE, "WindowEvent", "ts", (CALC, "title"))
            threading.Thread(target=fire, daemon=True).start()
            self.assertEqual(next(gen), (0, "workspace"))
            self.assertEqual(next(gen), (CALC, "title"))
            gen.close()
        finally:
            emitter.close()
            b.bus.close()
            bridge.close()

    def test_show_desktop_and_set_num_desktops(self):
        bridge = MockBridge(self.mock.address)
        b = GnomeBackend(settle=0.05)
        try:
            b.show_desktop(True)
            b.show_desktop(False)
            self.assertEqual([a for m, a in bridge.calls if m == "ShowDesktop"],
                             [(True,), (False,)])
            with self.assertRaises(core.CmdError):
                b.set_num_desktops(4)
            self.assertEqual(b.wm_name, "GNOME Shell")
        finally:
            b.bus.close()
            bridge.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
