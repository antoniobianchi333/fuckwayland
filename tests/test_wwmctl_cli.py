#!/usr/bin/env python3
"""Agent W: wwmctl CLI + core unit tests (no compositor needed).

Byte-parity checks follow wmctrl 1.07 main.c and the reference dumps; when
the real wmctrl binary is on PATH (nix develop) the help text is compared
byte-for-byte against `wmctrl --help`.

Documented deviations from wmctrl 1.07 (deliberate, see wwmctl/cli.py):
- -h/-V/--help/--version need no session (wmctrl needs an X display),
- backend-detection failure prints the detector's error, not
  "Cannot open display.",
- acting on a nonexistent window id exits 1 silently (wmctrl fires the
  ClientMessage into the void and exits 0)."""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wdotool.backend import Window  # noqa: E402
from wdotool.ctx import CmdError  # noqa: E402
from wwmctl import cli, core  # noqa: E402
from wwmctl.cli import WMCTRL_VERSION  # noqa: E402


class FakeSwayBackend:
    """Sway-shaped backend: offers _nodes() (raw tree view) and _msg()."""

    name = "sway"

    def __init__(self, specs, workspaces=None, current=0):
        self.specs = specs
        self.calls = []
        self.workspaces = workspaces or [
            {"num": 1, "name": "1", "focused": True,
             "rect": {"x": 0, "y": 0, "width": 1280, "height": 720}},
        ]
        self._cur = current
        self._select = None

    def _nodes(self):
        out = []
        for s in self.specs:
            node = {
                "id": s["node"],
                "name": s.get("title"),
                "app_id": s.get("app_id"),
                "window": s.get("xid"),
                "window_properties": s.get("wp"),
                "rect": {"x": s.get("x", 0), "y": s.get("y", 0),
                         "width": s.get("w", 0), "height": s.get("h", 0)},
            }
            win = Window(
                id=s["node"], title=s.get("title") or "",
                class_=s.get("app_id") or (s.get("wp") or {}).get("class", ""),
                pid=s.get("pid", 0), x=s.get("x", 0), y=s.get("y", 0),
                w=s.get("w", 0), h=s.get("h", 0),
                focused=s.get("focused", False),
                visible=s.get("visible", True),
                desktop=s.get("desktop", 0),
            )
            out.append((node, win, s.get("floating", False),
                        s.get("ws", "1")))
        return out

    def _msg(self, mtype):
        assert mtype == 1  # GET_WORKSPACES
        return self.workspaces

    def display_size(self):
        return (1280, 720)

    def _spec(self, wid):
        for s in self.specs:
            if s["node"] == wid:
                return s
        raise CmdError("window %d not found" % wid)

    def activate(self, wid):
        self._spec(wid)
        self.calls.append(("activate", wid))

    def close(self, wid):
        self._spec(wid)
        self.calls.append(("close", wid))

    def get_desktop(self):
        return self._cur

    def set_desktop(self, n):
        self.calls.append(("set_desktop", n))

    def num_desktops(self):
        return len(self.workspaces)

    def set_window_desktop(self, wid, n):
        self._spec(wid)
        self.calls.append(("set_window_desktop", wid, n))

    def move_window(self, wid, x, y):
        if not self._spec(wid).get("floating"):
            raise CmdError(
                "sway: cannot move a tiled window to an absolute position "
                "(floating enable it first)")
        self.calls.append(("move", wid, x, y))

    def resize(self, wid, w, h):
        self._spec(wid)
        self.calls.append(("resize", wid, w, h))

    def set_state(self, wid, state, action):
        self._spec(wid)
        if state not in ("FULLSCREEN", "STICKY", "DEMANDS_ATTENTION",
                         "HIDDEN"):
            raise CmdError(
                "windowstate %s is not supported by the sway backend" % state)
        self.calls.append(("set_state", wid, state, action))

    def select_window(self):
        return self._select


class FakeX11:
    """Implements the parts of the frozen x11_mini API that core uses."""

    def __init__(self, machines=None, wm_name="wlroots wm"):
        self.machines = machines or {}
        self.wm_name = wm_name
        self.calls = []

    def root(self):
        return 1

    def get_wm_class(self, win):
        return ("xterm", "XTerm")

    def get_client_machine(self, win):
        return self.machines.get(win, "xhost")

    def get_geometry(self, win):
        return (7, 8, 111, 222)

    def get_pid(self, win):
        return 999 if win != 99 else 0

    def get_prop_ints(self, win, name):
        if name == "_NET_SUPPORTING_WM_CHECK":
            return [99]
        return []

    def get_prop_string(self, win, name):
        if (win, name) == (99, "_NET_WM_NAME"):
            return self.wm_name
        return ""

    def set_name(self, win, name, icon, long_):
        self.calls.append(("set_name", win, name, icon, long_))


# standard fixture: one XWayland window, one native, one bare/N-A-ish
SPECS = [
    dict(node=5, xid=0x40000C, title="Mail inbox", pid=111,
         wp={"class": "XTerm", "instance": "xterm"},
         x=0, y=0, w=640, h=720, desktop=0),
    dict(node=6, app_id="footw", title="FootWin", pid=222,
         x=640, y=0, w=640, h=720, desktop=0, focused=True),
    dict(node=7, title=None, pid=0, x=-5, y=2, w=10, h=20, desktop=-1,
         ws="__i3_scratch"),
]


def run(argv, backend=None, x11=None, argv0="wmctrl", env=None):
    backend = backend if backend is not None else FakeSwayBackend(
        [dict(s) for s in SPECS])
    old_detect, old_x11 = core._detect_backend, core._x11_connect
    old_host, old_argv = core._hostname, sys.argv
    old_env = {}
    core._detect_backend = lambda: backend
    core._x11_connect = lambda: x11
    core._hostname = lambda: "testhost"
    sys.argv = [argv0]
    for k, v in (env or {}).items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
    finally:
        core._detect_backend, core._x11_connect = old_detect, old_x11
        core._hostname, sys.argv = old_host, old_argv
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return rc, out.getvalue(), err.getvalue(), backend


class UsageTest(unittest.TestCase):
    def test_help_structure(self):
        self.assertEqual(len(cli.HELP.encode()), 6801)
        self.assertTrue(cli.HELP.startswith(
            "wmctrl 1.07\nUsage: wmctrl [OPTION]...\nActions:\n"))
        self.assertTrue(cli.HELP.endswith("Copyright (C) 2003\n"))

    @unittest.skipUnless(shutil.which("wmctrl"), "real wmctrl not on PATH")
    def test_help_byte_parity_with_oracle(self):
        p = subprocess.run(["wmctrl", "--help"], capture_output=True,
                           text=True, timeout=10)
        self.assertEqual(cli.HELP, p.stdout)

    def test_no_args_help_to_stderr(self):
        rc, out, err, _b = run([])
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, cli.HELP)

    def test_positional_only_is_missing_option(self):
        rc, out, err, _b = run(["hello"])
        self.assertEqual((rc, out, err), (1, "", cli.HELP))

    def test_h_and_long_help(self):
        for argv in (["-h"], ["--help"]):
            rc, out, err, _b = run(argv)
            self.assertEqual((rc, out, err), (0, cli.HELP, ""))

    def test_version(self):
        # byte parity with the oracle: exactly "1.07", not an identity string
        for argv in (["-V"], ["--version"]):
            rc, out, _e, _b = run(argv)
            self.assertEqual((rc, out), (0, WMCTRL_VERSION + "\n"))
            self.assertEqual(out, "1.07\n")

    def test_invalid_option(self):
        rc, out, err, _b = run(["-q"])
        self.assertEqual((rc, out, err),
                         (1, "", "wmctrl: invalid option -- 'q'\n"))

    def test_missing_argument(self):
        rc, _o, err, _b = run(["-a"])
        self.assertEqual((rc, err),
                         (1, "wmctrl: option requires an argument -- 'a'\n"))

    def test_unknown_long_option(self):
        # plain getopt in the oracle: "--anything" is unknown option '-'
        rc, _o, err, _b = run(["--frob", "x"])
        self.assertEqual((rc, err),
                         (1, "wmctrl: invalid option -- '-'\n"))

    def test_unknown_workaround(self):
        rc, _o, err, _b = run(["-w", "NOPE"])
        self.assertEqual((rc, err), (1, "Unknown workaround: NOPE\n"))
        rc, _o, err, b = run(["-w", "DESKTOP_TITLES_INVALID_UTF8"])
        self.assertEqual((rc, err), (0, ""))  # accepted, nothing to do

    def test_options_but_no_action(self):
        rc, out, err, b = run(["-p"])
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(b.calls, [])

    def test_envir_utf8(self):
        rc, _o, err, _b = run(["-v", "-p"], env={"LC_ALL": "C.UTF-8"})
        self.assertIn("envir_utf8: 1\n", err)
        rc, _o, err, _b = run(["-v", "-p"], env={"LC_ALL": "C",
                                                 "LC_CTYPE": "C",
                                                 "LANG": "C"})
        self.assertIn("envir_utf8: 0\n", err)
        rc, _o, err, _b = run(["-v", "-u", "-p"], env={"LC_ALL": "C",
                                                       "LC_CTYPE": "C",
                                                       "LANG": "C"})
        self.assertIn("envir_utf8: 1\n", err)


class ListTest(unittest.TestCase):
    def test_l_plain(self):
        rc, out, _e, _b = run(["-l"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 testhost Mail inbox",
            "0x00000006  0 testhost FootWin",
            "0x00000007 -1 testhost N/A",
        ])

    def test_lp(self):
        rc, out, _e, _b = run(["-lp"])
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 111    testhost Mail inbox",
            "0x00000006  0 222    testhost FootWin",
            "0x00000007 -1 0      testhost N/A",
        ])

    def test_lG(self):
        rc, out, _e, _b = run(["-lG"])
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 0    0    640  720  testhost Mail inbox",
            "0x00000006  0 640  0    640  720  testhost FootWin",
            "0x00000007 -1 -5   2    10   20   testhost N/A",
        ])

    def test_lx(self):
        rc, out, _e, _b = run(["-lx"])
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 xterm.XTerm           testhost Mail inbox",
            "0x00000006  0 footw.footw           testhost FootWin",
            "0x00000007 -1 N/A                   testhost N/A",
        ])

    def test_lGpx_combo(self):
        rc, out, _e, _b = run(["-lGpx"])
        self.assertEqual(out.splitlines()[0],
                         "0x0040000c  0 111    0    0    640  720  "
                         "xterm.XTerm           testhost Mail inbox")

    def test_l_x_enrichment_and_machine_len_quirk(self):
        # X plane fills machine/class/geometry for the XWayland row; the
        # machine column width mimics wmctrl 1.07's "last window wins"
        # (not longest) quirk.
        x11 = FakeX11(machines={0x40000C: "longmachine.example"})
        rc, out, _e, _b = run(["-lG"], x11=x11)
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 7    8    111  222  longmachine.example "
            "Mail inbox",
            "0x00000006  0 640  0    640  720  testhost FootWin",
            "0x00000007 -1 -5   2    10   20   testhost N/A",
        ])

    def test_l_generic_backend(self):
        # non-sway backends have no _nodes(): everything is native-style
        class GenericBackend:
            name = "wlr"

            def list(self):
                return [Window(id=1000001, title="T", class_="app",
                               pid=3, x=1, y=2, w=3, h=4, desktop=-1)]

        rc, out, _e, _b = run(["-lx"], backend=GenericBackend())
        self.assertEqual(out.splitlines(), [
            "0x000f4241 -1 app.app               testhost T",
        ])


class DesktopTest(unittest.TestCase):
    def _backend(self):
        return FakeSwayBackend(
            [dict(s) for s in SPECS],
            workspaces=[
                {"num": 1, "name": "1", "focused": False,
                 "rect": {"x": 0, "y": 0, "width": 1280, "height": 690}},
                {"num": 2, "name": "web", "focused": True,
                 "rect": {"x": 0, "y": 0, "width": 1280, "height": 720}},
            ])

    def test_d_format(self):
        # VP is per-current-desktop, like wmctrl under EWMH: the single
        # viewport pair applies to the current desktop, the rest print N/A
        rc, out, _e, _b = run(["-d"], backend=self._backend())
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            "0  - DG: 1280x720  VP: N/A  WA: 0,0 1280x690  1",
            "1  * DG: 1280x720  VP: 0,0  WA: 0,0 1280x720  web",
        ])

    def test_s(self):
        rc, _o, _e, b = run(["-s", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(b.calls, [("set_desktop", 1)])

    def test_s_invalid(self):
        rc, _o, err, b = run(["-s", "-1"])
        self.assertEqual((rc, err), (1, "Invalid desktop ID.\n"))
        self.assertEqual(b.calls, [])

    def test_s_atoi_garbage_is_desktop_zero(self):
        rc, _o, _e, b = run(["-s", "junk"])  # C atoi() -> 0
        self.assertEqual((rc, b.calls), (0, [("set_desktop", 0)]))


class WmInfoTest(unittest.TestCase):
    def test_m_with_x_plane(self):
        rc, out, _e, _b = run(["-m"], x11=FakeX11())
        self.assertEqual(out, "Name: wlroots wm\n"
                              "Class: N/A\n"
                              "PID: N/A\n"
                              'Window manager\'s "showing the desktop" '
                              "mode: N/A\n")

    def test_m_compositor_only(self):
        rc, out, _e, _b = run(["-m"])
        self.assertEqual(out.splitlines()[0], "Name: sway")
        self.assertEqual(rc, 0)


class SelectionTest(unittest.TestCase):
    def test_title_substring_case_insensitive(self):
        rc, _o, _e, b = run(["-a", "mail IN"])
        self.assertEqual((rc, b.calls), (0, [("activate", 5)]))

    def test_first_match_wins(self):
        specs = [dict(SPECS[0]), dict(SPECS[1])]
        specs[1]["title"] = "Mail inbox two"
        b = FakeSwayBackend(specs)
        rc, _o, _e, b = run(["-a", "mail"], backend=b)
        self.assertEqual(b.calls, [("activate", 5)])

    def test_F_exact_case_sensitive(self):
        rc, _o, _e, b = run(["-F", "-a", "mail inbox"])
        self.assertEqual((rc, b.calls), (1, []))
        rc, _o, _e, b = run(["-F", "-a", "Mail inbox"])
        self.assertEqual((rc, b.calls), (0, [("activate", 5)]))

    def test_x_matches_class(self):
        rc, _o, _e, b = run(["-x", "-a", "term.XT"])
        self.assertEqual(b.calls, [("activate", 5)])
        rc, _o, _e, b = run(["-x", "-F", "-a", "footw.footw"])
        self.assertEqual(b.calls, [("activate", 6)])

    def test_no_match_silent_exit_1(self):
        rc, out, err, b = run(["-a", "zzznope"])
        self.assertEqual((rc, out, err, b.calls), (1, "", "", []))

    def test_i_decimal_and_hex(self):
        rc, _o, _e, b = run(["-i", "-a", "6"])
        self.assertEqual(b.calls, [("activate", 6)])
        rc, _o, _e, b = run(["-i", "-a", "0x0040000c"])
        self.assertEqual(b.calls, [("activate", 5)])  # X id -> node 5
        rc, _o, _e, b = run(["-i", "-a", "4194316"])  # decimal X id
        self.assertEqual(b.calls, [("activate", 5)])

    def test_i_bad_number(self):
        rc, _o, err, _b = run(["-i", "-a", "nope"])
        self.assertEqual((rc, err), (1, "Cannot convert argument to "
                                        "number.\n"))

    def test_i_unknown_id_exits_1(self):
        rc, out, err, b = run(["-i", "-a", "0xdead"])
        self.assertEqual((rc, out, err, b.calls), (1, "", "", []))

    def test_active_magic(self):
        rc, _o, _e, b = run(["-a", ":ACTIVE:"])
        self.assertEqual(b.calls, [("activate", 6)])  # focused foot

    def test_select_magic(self):
        b = FakeSwayBackend([dict(s) for s in SPECS])
        b._select = 5
        rc, _o, _e, b = run(["-a", ":SELECT:"], backend=b)
        self.assertEqual(b.calls, [("activate", 5)])

    def test_no_window_specified(self):
        rc, _o, err, _b = run(["-t", "1"])
        self.assertEqual((rc, err), (1, "No window was specified.\n"))

    def test_later_window_arg_wins(self):
        rc, _o, _e, b = run(["-r", "junk", "-a", "FootWin"])
        self.assertEqual(b.calls, [("activate", 6)])
        rc, _o, _e, b = run(["-a", "FootWin", "-r", "junk"])
        self.assertEqual((rc, b.calls), (1, []))

    def test_verbose_using_window(self):
        rc, _o, err, _b = run(["-v", "-i", "-a", "6"])
        self.assertIn("Using window: 0x00000006\n", err)


class ActionTest(unittest.TestCase):
    def test_close(self):
        rc, _o, _e, b = run(["-c", "FootWin"])
        self.assertEqual((rc, b.calls), (0, [("close", 6)]))

    def test_t_move_to_desktop(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-t", "3"])
        self.assertEqual((rc, b.calls), (0, [("set_window_desktop", 6, 3)]))

    def test_t_minus_one_is_current(self):
        b = FakeSwayBackend([dict(s) for s in SPECS], current=0)
        b._cur = 4
        rc, _o, _e, b = run(["-r", "FootWin", "-t", "-1"], backend=b)
        self.assertEqual(b.calls, [("set_window_desktop", 6, 4)])

    def test_t_atoi(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-t", "2abc"])
        self.assertEqual(b.calls, [("set_window_desktop", 6, 2)])

    def test_R_current_desktop_then_activate(self):
        rc, _o, _e, b = run(["-R", "FootWin"])
        self.assertEqual((rc, b.calls),
                         (0, [("set_window_desktop", 6, 0),
                              ("activate", 6)]))


class MoveResizeTest(unittest.TestCase):
    def _floating(self):
        specs = [dict(s) for s in SPECS]
        specs[0]["floating"] = True
        return FakeSwayBackend(specs)

    def test_e_full(self):
        rc, _o, err, b = run(["-r", "Mail", "-e", "0,10,20,300,200"],
                             backend=self._floating())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("resize", 5, 300, 200),
                                   ("move", 5, 10, 20)])

    def test_e_resize_only(self):
        rc, _o, _e, b = run(["-r", "Mail", "-e", "0,-1,-1,300,200"],
                            backend=self._floating())
        self.assertEqual(b.calls, [("resize", 5, 300, 200)])

    def test_e_move_only_fills_current(self):
        rc, _o, _e, b = run(["-r", "Mail", "-e", "0,10,-1,-1,-1"],
                            backend=self._floating())
        self.assertEqual(b.calls, [("move", 5, 10, 0)])  # y from rect

    def test_e_tiled_move_warns_but_succeeds(self):
        rc, _o, err, b = run(["-r", "Mail", "-e", "0,10,20,-1,-1"])
        self.assertEqual(rc, 0)
        self.assertIn("; ignoring", err)
        self.assertEqual(b.calls, [])

    def test_e_parse_error(self):
        rc, _o, err, b = run(["-r", "Mail", "-e", "1,2,3"])
        self.assertEqual((rc, err), (1, 'The -e option expects a list of '
                                        'comma separated integers: '
                                        '"gravity,X,Y,width,height"\n'))
        self.assertEqual(b.calls, [])

    def test_e_negative_gravity(self):
        rc, _o, err, _b = run(["-r", "Mail", "-e", "-1,2,3,4,5"])
        self.assertEqual((rc, err),
                         (1, "Value of gravity mustn't be negative. Use zero"
                             " to use the default gravity of the window.\n"))

    def test_e_verbose_grflags(self):
        rc, _o, err, _b = run(["-v", "-r", "Mail", "-e", "5,10,-1,300,-1"],
                              backend=self._floating())
        # grav 5 | x(1<<8) | w(1<<10)
        self.assertIn("grflags: %d\n" % (5 | 256 | 1024), err)


class WindowStateTest(unittest.TestCase):
    def test_b_add_fullscreen(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-b", "add,fullscreen"])
        self.assertEqual((rc, b.calls),
                         (0, [("set_state", 6, "FULLSCREEN", 1)]))

    def test_b_remove_and_toggle(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-b", "remove,hidden"])
        self.assertEqual(b.calls, [("set_state", 6, "HIDDEN", 0)])
        rc, _o, _e, b = run(["-r", "FootWin", "-b", "toggle,sticky"])
        self.assertEqual(b.calls, [("set_state", 6, "STICKY", 2)])

    def test_b_two_props(self):
        rc, _o, _e, b = run(["-r", "FootWin", "-b", "add,sticky,hidden"])
        self.assertEqual(b.calls, [("set_state", 6, "STICKY", 1),
                                   ("set_state", 6, "HIDDEN", 1)])

    def test_b_unsupported_warns_but_succeeds(self):
        rc, _o, err, b = run(
            ["-r", "FootWin", "-b", "add,maximized_vert,maximized_horz"])
        self.assertEqual(rc, 0)
        self.assertEqual(err.count("; ignoring"), 2)
        self.assertIn("MAXIMIZED_VERT", err)
        self.assertEqual(b.calls, [])

    def test_b_errors(self):
        argerr = ('The -b option expects a list of comma separated '
                  'parameters: "(remove|add|toggle),<PROP1>[,<PROP2>]"\n')
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "nocomma"])
        self.assertEqual((rc, err), (1, argerr))
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "frob,fullscreen"])
        self.assertEqual((rc, err),
                         (1, "Invalid action. Use either remove, add or "
                             "toggle.\n"))
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "add,fullscreen,"])
        self.assertEqual((rc, err), (1, "Invalid zero length property.\n"))
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "add,"])
        self.assertEqual((rc, err), (1, "Invalid zero length property.\n"))

    def test_b_verbose_state2_before_state1(self):
        rc, _o, err, _b = run(
            ["-v", "-r", "FootWin", "-b", "toggle,sticky,hidden"])
        lines = [ln for ln in err.splitlines() if ln.startswith("State")]
        self.assertEqual(lines, ["State 2: _NET_WM_STATE_HIDDEN",
                                 "State 1: _NET_WM_STATE_STICKY"])


class SetTitleTest(unittest.TestCase):
    def test_N_I_T_on_x_window(self):
        for mode, icon, long_ in (("N", False, True), ("I", True, False),
                                  ("T", True, True)):
            x11 = FakeX11()
            rc, _o, err, _b = run(["-r", "Mail", "-%s" % mode, "New"],
                                  x11=x11)
            self.assertEqual((rc, err), (0, ""), mode)
            self.assertEqual(x11.calls,
                             [("set_name", 0x40000C, "New", icon, long_)])

    def test_title_on_native_warns_but_succeeds(self):
        rc, _o, err, _b = run(["-r", "FootWin", "-N", "New"], x11=FakeX11())
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_title_without_x_plane_warns(self):
        rc, _o, err, _b = run(["-r", "Mail", "-N", "New"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)


class WarnAndSucceedTest(unittest.TestCase):
    def test_k(self):
        rc, _o, err, _b = run(["-k", "maybe"])
        self.assertEqual((rc, err), (1, 'The argument to the -k option must '
                                        'be either "on" or "off"\n'))
        for arg in ("on", "off"):
            rc, _o, err, _b = run(["-k", arg])
            self.assertEqual(rc, 0)
            self.assertIn("ignoring", err)

    def test_o(self):
        rc, _o, err, _b = run(["-o", "12"])
        self.assertEqual((rc, err), (1, "The -o option expects two integers "
                                        "separated with a comma.\n"))
        rc, _o, err, _b = run(["-o", "0,0"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_n(self):
        rc, _o, err, _b = run(["-n", "junk"])
        self.assertEqual((rc, err), (1, "The -n option expects an "
                                        "integer.\n"))
        rc, _o, err, _b = run(["-n", "4"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_g(self):
        rc, _o, err, _b = run(["-g", "1x2"])
        self.assertEqual((rc, err), (1, "The -g option expects two integers "
                                        "separated with a comma.\n"))
        rc, _o, err, _b = run(["-g", "1280,720"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)


class BackendErrorTest(unittest.TestCase):
    def test_backend_failure_prints_message(self):
        def boom():
            raise CmdError("no compositor found")
        old = core._detect_backend
        core._detect_backend = boom
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(["-l"])
        finally:
            core._detect_backend = old
        self.assertEqual((rc, err.getvalue()), (1, "no compositor found\n"))


class HelpersTest(unittest.TestCase):
    def test_parse_win_id(self):
        self.assertEqual(core._parse_win_id("0x40000c"), 0x40000C)
        self.assertEqual(core._parse_win_id("0X40000C"), 0x40000C)
        self.assertEqual(core._parse_win_id("42"), 42)
        self.assertEqual(core._parse_win_id(" 42xyz"), 42)  # sscanf prefix
        self.assertIsNone(core._parse_win_id("xyz"))
        self.assertIsNone(core._parse_win_id(""))

    def test_atoi(self):
        self.assertEqual(core._atoi("12"), 12)
        self.assertEqual(core._atoi("-1"), -1)
        self.assertEqual(core._atoi("  8x"), 8)
        self.assertEqual(core._atoi("x8"), 0)

    def test_dot_class(self):
        self.assertEqual(core._dot_class("a", "B"), "a.B")
        self.assertEqual(core._dot_class(None, "B"), "B")
        self.assertEqual(core._dot_class("a", None), "a")
        self.assertIsNone(core._dot_class(None, None))


class BuildScriptTest(unittest.TestCase):
    """scripts/build-pyz.sh also emits dist/wwmctl (in a temp copy so a
    parallel test run cannot race on the repo's dist/.stage)."""

    def test_build_emits_both_zipapps(self):
        with tempfile.TemporaryDirectory(prefix="wwmctl-build-") as tmp:
            for d in ("wdotool", "wwmctl", "wxprop", "wxrandr", "scripts"):
                shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d),
                                ignore=shutil.ignore_patterns("__pycache__"))
            p = subprocess.run(["sh", os.path.join(tmp, "scripts",
                                                   "build-pyz.sh")],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            for name in ("wdotool", "wwmctl"):
                path = os.path.join(tmp, "dist", name)
                self.assertTrue(os.path.exists(path), name)
            p = subprocess.run([sys.executable,
                                os.path.join(tmp, "dist", "wwmctl"),
                                "--version"],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual((p.returncode, p.stdout),
                             (0, WMCTRL_VERSION + "\n"))
            p = subprocess.run([sys.executable,
                                os.path.join(tmp, "dist", "wwmctl"), "-h"],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(len(p.stdout.encode()), 6801)
            p = subprocess.run([sys.executable,
                                os.path.join(tmp, "dist", "wdotool"),
                                "version"],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
