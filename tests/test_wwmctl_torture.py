#!/usr/bin/env python3
"""Regression tests from the wwmctl torture pass (oracle-diffed against the
real wmctrl 1.07 binary on a live sway+XWayland sandbox).

Bugs fixed and pinned here:
- `python -m wwmctl -q` printed "__main__.py: invalid option" (prog name),
- unknown long options printed "unrecognized option '--x'"; plain getopt in
  the oracle prints "invalid option -- '-'",
- -V/--version printed an identity string instead of the oracle's "1.07",
- a broken/closed stdout tracebacked ("Exception ignored ... BrokenPipeError"
  or AttributeError on sys.stdout=None) instead of a quiet exit,
- -o/-g/-n rejected negative integers that sscanf("%lu") accepts (the oracle
  exits 0; we warn+succeed like the other desktop no-ops),
- -s with a negative desktop and -t to a negative desktop leaked sway's
  off-by-one "Invalid workspace number '-4'" error (now: the -s message is
  wmctrl's own "Invalid desktop ID.", -t warns and succeeds),
- -R / -t -1 while a *named* (numberless) sway workspace was focused
  mis-filed the window onto a workspace literally called "0" instead of the
  current workspace,
- the -lx class / machine columns padded by characters, not bytes (printf
  %-20s counts bytes; only visible with non-ASCII classes/hostnames).
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_wwmctl_cli import SPECS, FakeSwayBackend, FakeX11, run
from wdotool.ctx import CmdError
from wwmctl import cli


class ProgNameTest(unittest.TestCase):
    def test_python_dash_m_does_not_leak_main_py(self):
        rc, _o, err, _b = run(["-q"], argv0="/x/wwmctl/__main__.py")
        self.assertEqual((rc, err), (1, "wwmctl: invalid option -- 'q'\n"))

    def test_symlink_name_is_used(self):
        rc, _o, err, _b = run(["-q"], argv0="/usr/local/bin/wmctrl")
        self.assertEqual((rc, err), (1, "wmctrl: invalid option -- 'q'\n"))


class LongOptionParityTest(unittest.TestCase):
    """The oracle uses plain getopt: any unknown --long option is the
    unknown short option '-' (verified byte-for-byte against wmctrl 1.07)."""

    def test_unknown_long_option(self):
        for argv in (["--frob"], ["--frob", "x"], ["-l", "--help"],
                     ["--help", "-l"], ["--version", "x"]):
            rc, _o, err, _b = run(argv)
            self.assertEqual((rc, err),
                             (1, "wmctrl: invalid option -- '-'\n"), argv)

    def test_double_dash_alone_is_end_of_options(self):
        rc, _o, err, _b = run(["--"])
        self.assertEqual((rc, err), (1, cli.HELP))


class BrokenStdoutTest(unittest.TestCase):
    class _BrokenPipe(io.StringIO):
        def write(self, s):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    def test_broken_pipe_exits_1_quietly(self):
        from wwmctl import core
        old_detect = core._detect_backend
        core._detect_backend = lambda: FakeSwayBackend(
            [dict(s) for s in SPECS])
        old_x11, core._x11_connect = core._x11_connect, lambda: None
        old_out, sys.stdout = sys.stdout, self._BrokenPipe()
        err = io.StringIO()
        old_err, sys.stderr = sys.stderr, err
        try:
            rc = cli.main(["-l"])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            core._detect_backend, core._x11_connect = old_detect, old_x11
        self.assertEqual((rc, err.getvalue()), (1, ""))

    def test_stdout_none_help_exits_0(self):
        # fd 1 closed before Python starts -> sys.stdout is None
        old_out, sys.stdout = sys.stdout, None
        try:
            rc = cli.main(["-h"])
            opened = sys.stdout
        finally:
            sys.stdout = old_out
        self.assertEqual(rc, 0)
        if opened is not None and opened is not old_out:
            opened.close()

    def test_stdout_none_list_exits_0(self):
        from wwmctl import core
        old_detect = core._detect_backend
        core._detect_backend = lambda: FakeSwayBackend(
            [dict(s) for s in SPECS])
        old_x11, core._x11_connect = core._x11_connect, lambda: None
        old_out, sys.stdout = sys.stdout, None
        try:
            rc = cli.main(["-l"])
            opened = sys.stdout
        finally:
            sys.stdout = old_out
            core._detect_backend, core._x11_connect = old_detect, old_x11
        self.assertEqual(rc, 0)
        if opened is not None and opened is not old_out:
            opened.close()


class NegativeIntParityTest(unittest.TestCase):
    """sscanf("%lu") accepts a sign (strtoul wraps): the oracle exits 0 on
    negative -o/-g/-n arguments. We accept them and warn+succeed."""

    def test_o_g_negative(self):
        for flag in ("-o", "-g"):
            rc, _o, err, _b = run([flag, "-1,-1"])
            self.assertEqual(rc, 0, flag)
            self.assertIn("ignoring", err)

    def test_n_negative(self):
        rc, _o, err, _b = run(["-n", "-3"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)

    def test_garbage_still_rejected(self):
        rc, _o, err, _b = run(["-o", "-,3"])
        self.assertEqual((rc, err), (1, "The -o option expects two integers "
                                        "separated with a comma.\n"))


class NegativeDesktopTest(unittest.TestCase):
    def test_s_any_negative_is_invalid_desktop(self):
        # never leak sway's off-by-one "Invalid workspace number '-4'"
        for arg in ("-1", "-5", "-2147483648"):
            rc, _o, err, b = run(["-s", arg])
            self.assertEqual((rc, err), (1, "Invalid desktop ID.\n"), arg)
            self.assertEqual(b.calls, [])

    def test_t_negative_warns_and_succeeds(self):
        # the oracle fires _NET_WM_DESKTOP=huge into the void and exits 0
        rc, _o, err, b = run(["-r", "FootWin", "-t", "-5"])
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", err)
        self.assertEqual(b.calls, [])


class RunCapableFake(FakeSwayBackend):
    """Sway-shaped fake that, like the real SwayBackend, offers run()."""

    def run(self, command):
        self.calls.append(("run", command))

    def window_desktop(self, wid):
        return self._spec(wid).get("desktop", 0)


class CurrentDesktopViaRunTest(unittest.TestCase):
    """-R / -t -1 on sway go through `move container to workspace current`,
    which is correct even when the focused workspace is named (no number).
    The numeric route used to send the window to a workspace called "0"."""

    def _backend(self):
        return RunCapableFake([dict(s) for s in SPECS])

    def test_t_minus_one_uses_workspace_current(self):
        b = self._backend()
        b._cur = -1  # focused workspace is named: get_desktop() == -1
        rc, _o, err, b = run(["-r", "FootWin", "-t", "-1"], backend=b)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            b.calls, [("run", "[con_id=6] move container to workspace "
                              "current")])

    def test_R_uses_workspace_current_then_activates(self):
        b = self._backend()
        b._cur = -1
        rc, _o, err, b = run(["-R", "FootWin"], backend=b)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            b.calls, [("run", "[con_id=6] move container to workspace "
                              "current"),
                      ("activate", 6)])

    def test_vanished_window_is_clean_error(self):
        b = self._backend()

        def gone(wid):
            raise CmdError("window %d not found" % wid)
        b.window_desktop = gone
        rc, _o, err, b = run(["-r", "FootWin", "-t", "-1"], backend=b)
        self.assertEqual((rc, err), (1, "window 6 not found\n"))

    def test_generic_backend_still_uses_numbers(self):
        # no run(): fall back to get_desktop() + set_window_desktop()
        b = FakeSwayBackend([dict(s) for s in SPECS])
        b._cur = 3
        rc, _o, _e, b = run(["-r", "FootWin", "-t", "-1"], backend=b)
        self.assertEqual((rc, b.calls), (0, [("set_window_desktop", 6, 3)]))


class BytePaddingTest(unittest.TestCase):
    """printf %-20s / %*s count bytes; ours must too (visible only with
    non-ASCII WM_CLASS or hostnames)."""

    def test_lx_class_column_pads_bytes(self):
        specs = [dict(SPECS[1])]
        specs[0]["app_id"] = "föö"  # föö: 3 chars, 5 bytes
        rc, out, _e, _b = run(["-lx"], backend=FakeSwayBackend(specs))
        line = out.splitlines()[0]
        head = "0x00000006  0 "
        cls = "föö.föö"  # 7 chars, 11 bytes
        pad = " " * (20 - 11)
        self.assertEqual(line, head + cls + pad + "  testhost FootWin")

    def test_machine_column_pads_bytes(self):
        x11 = FakeX11(machines={0x40000C: "hôst"})  # 4 chars, 5 bytes
        specs = [dict(SPECS[0]), dict(SPECS[1])]
        rc, out, _e, _b = run(["-l"], backend=FakeSwayBackend(specs),
                              x11=x11)
        lines = out.splitlines()
        # width = byte length of the last machine ("testhost", 8): the höst
        # row right-pads to 8 BYTES = 3 spaces before the 5-byte name
        self.assertEqual(lines[0], "0x0040000c  0    hôst Mail inbox")
        self.assertEqual(lines[1], "0x00000006  0 testhost FootWin")


class EnrichmentRaceTest(unittest.TestCase):
    """An XWayland window can die between the tree read and the X property
    reads: every enrichment failure must degrade to compositor data."""

    class _DyingX11:
        def _boom(self, *a, **k):
            raise ConnectionResetError("X connection lost")
        get_wm_class = get_client_machine = get_geometry = get_pid = _boom
        root = get_prop_ints = get_prop_string = set_name = _boom

    def test_listing_survives_x_failures(self):
        rc, out, _e, _b = run(["-lGpx"], x11=self._DyingX11())
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.splitlines()[0],
            "0x0040000c  0 111    0    0    640  720  "
            "xterm.XTerm           testhost Mail inbox")

    def test_set_title_survives_x_failure(self):
        rc, _o, err, _b = run(["-r", "Mail", "-N", "x"],
                              x11=self._DyingX11())
        self.assertEqual(rc, 0)
        self.assertIn("; ignoring", err)


class SelectionLiteralTest(unittest.TestCase):
    """<WIN> is a literal casefolded substring, never a regex."""

    def _backend(self):
        specs = [dict(s) for s in SPECS]
        specs[0]["title"] = "a.*b [1] (main)"
        return FakeSwayBackend(specs)

    def test_metachars_match_literally(self):
        rc, _o, _e, b = run(["-a", "a.*b [1]"], backend=self._backend())
        self.assertEqual((rc, b.calls), (0, [("activate", 5)]))

    def test_metachars_do_not_glob(self):
        rc, _o, _e, b = run(["-a", "a.b"], backend=self._backend())
        self.assertEqual((rc, b.calls), (1, []))

    def test_empty_needle_matches_first_window(self):
        # strstr(title, "") matches: the oracle picks the first listed window
        rc, _o, _e, b = run(["-a", ""])
        self.assertEqual((rc, b.calls), (0, [("activate", 5)]))


class SscanfEdgeTest(unittest.TestCase):
    def test_e_space_before_comma_rejected(self):
        # sscanf: a literal ',' does not skip whitespace
        rc, _o, err, _b = run(["-r", "Mail", "-e", "0 ,10,20,300,200"])
        self.assertEqual(rc, 1)
        self.assertIn("The -e option expects", err)

    def test_e_space_after_comma_ok(self):
        # %ld skips leading whitespace
        specs = [dict(s) for s in SPECS]
        specs[0]["floating"] = True
        rc, _o, _e, b = run(["-r", "Mail", "-e", "0, 10, 20, 300, 200"],
                            backend=FakeSwayBackend(specs))
        self.assertEqual(rc, 0)
        self.assertEqual(b.calls, [("resize", 5, 300, 200),
                                   ("move", 5, 10, 20)])

    def test_e_trailing_junk_after_five_ints_ok(self):
        specs = [dict(s) for s in SPECS]
        specs[0]["floating"] = True
        rc, _o, _e, _b = run(["-r", "Mail", "-e", "0,-1,-1,300,200junk"],
                             backend=FakeSwayBackend(specs))
        self.assertEqual(rc, 0)

    def test_b_third_comma_joins_prop2(self):
        # strchr splitting: "add,a,b,c" -> PROP2 is "b,c" (oracle-verified)
        rc, _o, err, _b = run(["-v", "-r", "Mail", "-b", "add,a,b,c"])
        self.assertEqual(rc, 0)
        self.assertIn("State 2: _NET_WM_STATE_B,C\n", err)
        self.assertIn("State 1: _NET_WM_STATE_A\n", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
