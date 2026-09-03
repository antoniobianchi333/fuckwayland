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

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"


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

    def minimize(self, wid):
        self._spec(wid)
        self.calls.append(("minimize", wid))

    def lower(self, wid):
        self._spec(wid)
        self.calls.append(("lower", wid))


class FakeX11:
    """Implements the parts of the frozen x11_mini API that core uses."""

    def __init__(self, machines=None, wm_name="wlroots wm"):
        self.machines = machines or {}
        self.wm_name = wm_name
        self.calls = []
        self.atoms = {}

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

    def atom(self, name, only_if_exists=False):
        return self.atoms.setdefault(name, 0x180 + len(self.atoms))

    def send_root_message(self, win, type_name, data):
        self.calls.append(("client_message", win, type_name, tuple(data)))


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

    def test_help_generation_follows_the_oracle(self):
        """wwmctl-5: two oracle generations are in the field and both say
        "1.07" for -V. The newer one (1.07+git20240228, Ubuntu 25.04+)
        documents six more options and `-k toggle`; --help follows whichever
        wmctrl is installed here."""
        self.assertTrue(cli.HELP_GIT.startswith(
            "wmctrl 1.07\nUsage: wmctrl [OPTION]...\nActions:\n"))
        self.assertTrue(cli.HELP_GIT.endswith("Copyright (C) 2003\n"))
        self.assertEqual(len(cli.HELP_GIT.encode()), 7037)
        for opt in ("  -j  ", "  -S  ", "  -Y <WIN>", "  -r <WIN> -y <MVARG>",
                    "  -k (on|off|toggle)"):
            self.assertIn(opt, cli.HELP_GIT)
            self.assertNotIn(opt, cli.HELP)
        for argv in (["-h"], ["--help"]):
            rc, out, err, _b = run(argv, env={"WWMCTL_WMCTRL_GENERATION":
                                              "git"})
            self.assertEqual((rc, out, err), (0, cli.HELP_GIT, ""))
            rc, out, err, _b = run(argv, env={"WWMCTL_WMCTRL_GENERATION":
                                              "1.07"})
            self.assertEqual((rc, out, err), (0, cli.HELP, ""))

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

    def test_l_x_enrichment_and_machine_column_width(self):
        # X plane fills machine/class/geometry for the XWayland row; the
        # machine column is right-aligned to the LONGEST hostname in the
        # list. wmctrl 1.07 uses the last row's width instead -- a main.c
        # bug that is invisible on its creation-ordered list and would
        # re-flow ours (stacking order) on every raise.
        x11 = FakeX11(machines={0x40000C: "longmachine.example"})
        rc, out, _e, _b = run(["-lG"], x11=x11)
        self.assertEqual(out.splitlines(), [
            "0x0040000c  0 7    8    111  222  longmachine.example "
            "Mail inbox",
            "0x00000006  0 640  0    640  720             testhost FootWin",
            "0x00000007 -1 -5   2    10   20              testhost N/A",
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


class PlaceAxisTest(unittest.TestCase):
    """core._place_axis, the -e gravity arithmetic, as a table. Frame edge
    100, frame size 640, asked for a 300-wide client in a 300-wide frame
    (zero extents) unless the row says otherwise."""

    F_OLD, FS_OLD, CS_NEW, FS_NEW = 100, 640, 300, 300

    def place(self, pos, req, keep_anchor=True, static=False, lead=0,
              fs_new=None):
        return core._place_axis(pos, static, req, lead, self.F_OLD,
                                self.FS_OLD, self.CS_NEW,
                                self.FS_NEW if fs_new is None else fs_new,
                                keep_anchor)

    def test_explicit_coordinate_places_the_gravity_point(self):
        for pos, want in ((0, 500), (1, 500), (2, 500)):
            self.assertEqual(self.place(pos, 500), want, pos)
        # a frame wider than the client shifts the leading edge back
        self.assertEqual(self.place(2, 500, fs_new=320), 480)
        self.assertEqual(self.place(1, 500, fs_new=320), 490)
        self.assertEqual(self.place(0, 500, fs_new=320), 500)

    def test_bare_resize_keeps_the_gravity_point(self):
        # both coordinates -1: the reference point does not move
        self.assertEqual(self.place(0, -1), 100)          # left edge
        self.assertEqual(self.place(1, -1), 270)          # centre 420
        self.assertEqual(self.place(2, -1), 440)          # right edge 740

    def test_a_kept_axis_next_to_a_given_one_keeps_its_edge(self):
        # wwmctl-4: Mutter's rule when the request carries a coordinate
        for pos in (0, 1, 2):
            self.assertEqual(self.place(pos, -1, keep_anchor=False), 100,
                             pos)

    def test_static_gravity_addresses_the_client(self):
        self.assertEqual(self.place(0, 500, static=True, lead=37), 463)
        self.assertEqual(self.place(2, 500, static=True, lead=37), 463)
        for keep in (True, False):
            self.assertEqual(
                self.place(2, -1, static=True, lead=37, keep_anchor=keep),
                100, keep)


class GitGenerationOptionsTest(unittest.TestCase):
    """wwmctl-5: the options wmctrl 1.07+git20240228 added. We accept them
    on every flavor -- rejecting `wmctrl -j` on 24.04 would only make a
    26.04 script fail later and more confusingly."""

    def test_j_prints_the_current_desktop(self):
        rc, out, err, _b = run(["-j"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "0 \n")   # printf("%-2d\n")
        b = FakeSwayBackend([dict(s) for s in SPECS], current=12)
        rc, out, _e, _b = run(["-j"], backend=b)
        self.assertEqual(out, "12\n")

    def test_S_is_accepted_and_our_list_is_already_stacking(self):
        rc, plain, err, _b = run(["-l"])
        self.assertEqual((rc, err), (0, ""))
        rc, stacked, err, _b = run(["-lS"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(plain, stacked)

    def test_Y_iconifies(self):
        rc, _o, err, b = run(["-Y", "Mail"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("minimize", 5)])

    def test_z_lowers(self):
        rc, _o, err, b = run(["-z", "Mail"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("lower", 5)])

    def test_E_prints_the_title(self):
        rc, out, err, _b = run(["-E", "Mail"])
        self.assertEqual((rc, out, err), (0, "Mail inbox\n", ""))
        # a titleless window prints an empty line, like the oracle's
        # get_window_title fallback
        rc, out, _e, _b = run(["-i", "-E", "7"])
        self.assertEqual((rc, out), (0, "\n"))

    def test_y_is_e_then_activate(self):
        specs = [dict(s) for s in SPECS]
        specs[0]["floating"] = True
        b = FakeSwayBackend(specs)
        rc, _o, err, b = run(["-r", "Mail", "-y", "0,10,20,300,200"],
                             backend=b)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(b.calls, [("resize", 5, 300, 200),
                                   ("move", 5, 10, 20),
                                   ("activate", 5)])

    def test_unknown_ids_still_exit_1_silently(self):
        for opt in ("-Y", "-z", "-E"):
            rc, out, err, b = run(["-i", opt, "0x999999"])
            self.assertEqual((rc, out, err), (1, "", ""))
            self.assertEqual(b.calls, [])


class XStateFallbackTest(unittest.TestCase):
    """wwmctl-6: a state the compositor backend refuses is retried on the
    X plane for an XWayland window, which is where real wmctrl sends it."""

    def test_refused_state_is_retried_on_the_x_plane(self):
        x = FakeX11()
        rc, _o, err, b = run(["-r", "Mail", "-b", "add,below"], x11=x)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([c for c in x.calls if c[0] == "client_message"],
                         [("client_message", 0x40000C, "_NET_WM_STATE",
                           (1, x.atom("_NET_WM_STATE_BELOW"), 0, 0, 0))])
        self.assertEqual(b.calls, [])   # the backend refused it

    def test_without_an_x_plane_the_refusal_still_warns(self):
        rc, _o, err, _b = run(["-r", "Mail", "-b", "add,below"], x11=None)
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_a_native_window_never_gets_a_client_message(self):
        x = FakeX11()
        rc, _o, err, _b = run(["-r", "FootWin", "-b", "add,below"], x11=x)
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)
        self.assertEqual([c for c in x.calls if c[0] == "client_message"], [])


class WarnAndSucceedTest(unittest.TestCase):
    def test_k(self):
        rc, _o, err, _b = run(["-k", "maybe"])
        self.assertEqual((rc, err), (1, 'The argument to the -k option must '
                                        'be either "on" or "off" or '
                                        '"toggle"\n'))
        for arg in ("on", "off", "toggle"):
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
            for d in ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr",
                      "scripts"):
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
