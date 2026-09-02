#!/usr/bin/env python3
"""wxprop builder: offline tests for wxprop.cli + wxprop.core.

Everything here runs without a compositor or X server: the X plane is
disabled via WXPROP_NO_X and the compositor is a fake sway backend injected
through core._detect_backend. Error strings and exit codes were captured
from real xprop 1.2.8 (SCRATCH/reference/xprop-*.err); the deliberate
deviations (native plane, -version identity, -font, click-select) are
covered as their own contracts.
"""

import io
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wxprop import cli, core
from wxprop import fmt as fmtmod

USAGE_FIRST = "usage:  xprop [-options ...] [[format [dformat]] atom] ...\n"


class _CapStdout:
    def __init__(self):
        self.buffer = io.BytesIO()

    def write(self, s):
        self.buffer.write(s.encode("utf-8", "surrogateescape"))
        return len(s)

    def flush(self):
        pass

    def fileno(self):
        raise ValueError("no fileno in capture")

    def isatty(self):
        return False


class _FakeWin:
    def __init__(self, **kw):
        self.id = kw.get("id", 0)
        self.title = kw.get("title", "")
        self.class_ = kw.get("class_", "")
        self.pid = kw.get("pid", 0)
        self.x = self.y = self.w = self.h = 0
        self.focused = kw.get("focused", False)
        self.visible = kw.get("visible", True)
        self.desktop = kw.get("desktop", 0)


class _FakeSway:
    """Just enough of wdotool.backend_sway for the native plane."""

    def __init__(self):
        self.foot = {
            "id": 6, "app_id": "footw", "name": "WL-Foot", "pid": 4242,
            "fullscreen_mode": 0, "visible": True, "sticky": False,
        }
        self.xterm = {
            "id": 5, "app_id": None, "name": "XW-Xterm", "pid": 4243,
            "window": 0x40000C, "fullscreen_mode": 0, "visible": True,
            "sticky": False,
            "window_properties": {"instance": "xterm", "class": "XTerm"},
        }

    def _nodes(self):
        return [
            (self.xterm, _FakeWin(id=5, title="XW-Xterm", pid=4243,
                                  desktop=0), False, "1"),
            (self.foot, _FakeWin(id=6, title="WL-Foot", class_="footw",
                                 pid=4242, desktop=0, focused=True),
             False, "1"),
        ]

    def num_desktops(self):
        return 2

    def get_desktop(self):
        return 0

    def select_window(self):
        return 6


class CliTestBase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {
            "WXPROP_NO_X": "1", "WXPROP_ARGV0": "xprop", "LC_ALL": "C"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("COLORTERM", None)
        os.environ.pop("XPROPFORMATS", None)

    def run_cli(self, *args, backend="none", hostname="testhost"):
        if backend == "fake":
            fake = _FakeSway()
            det = lambda: fake
        else:
            det = lambda: None
        out = _CapStdout()
        err = io.StringIO()
        with mock.patch.object(core, "_detect_backend", det), \
                mock.patch.object(core, "_hostname",
                                  lambda: hostname), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            code = cli.main(list(args))
        return code, out.buffer.getvalue(), err.getvalue()


class VersionHelpGrammarTest(CliTestBase):
    # The 1.2.8 RELEASE binary handles -grammar/-help/-version in the option
    # loop (NOT a pre-scan): single dash only, in argv order, after the
    # display would be opened. All assertions below verified against the
    # oracle (see xprop-notes.md's correction of the master-source notes).
    def test_help_goes_to_stderr_exit_0(self):
        code, out, err = self.run_cli("-help")
        self.assertEqual((code, out), (0, b""))
        self.assertTrue(err.startswith(USAGE_FIRST), err)
        self.assertIn("    -version                       "
                      "print program version\n", err)

    def test_double_dash_help_is_unrecognized(self):
        # oracle: `xprop --help` -> "unrecognized argument --help", exit 1
        code, out, err = self.run_cli("--help")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("xprop: unrecognized argument --help\n\n", err)

    def test_help_wins_over_everything(self):
        # xprop2-spy-usage: `-spy -id W -len 1 -help` -> usage, exit 0
        code, out, err = self.run_cli("-spy", "-id", "0x1", "-len", "1",
                                      "-help")
        self.assertEqual((code, out), (0, b""))
        self.assertTrue(err.startswith(USAGE_FIRST))

    def test_bad_flag_before_version_is_usage_error(self):
        # oracle: `xprop -badflag -version` -> the option loop hits -badflag
        # FIRST (argv order), so it's a usage error, NOT a version print
        code, out, err = self.run_cli("-badflag", "-version")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("xprop: unrecognized argument -badflag\n\n", err)

    def test_grammar_single_dash_only(self):
        code, out, err = self.run_cli("-grammar")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(b"Grammar for xprop:\n\n"))
        self.assertTrue(out.endswith(
            b"\tnormal char ::= <any char except a digit, $, ?, \\, or )>"
            b"\n\n"), out)
        # --grammar is NOT accepted (matches the oracle)
        code, _out, err = self.run_cli("--grammar")
        self.assertEqual(code, 1)
        self.assertIn("xprop: unrecognized argument --grammar\n\n", err)

    def test_version(self):
        # byte parity with the oracle: `xprop -version` -> "xprop 1.2.8"
        code, out, err = self.run_cli("-version")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b"xprop 1.2.8\n")

    def test_double_dash_version_is_unrecognized(self):
        code, out, err = self.run_cli("--version")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("xprop: unrecognized argument --version\n\n", err)

    def test_version_after_root(self):
        # -root is a select arg (consumed early); -version still fires in
        # the option loop -> version print, exit 0 (oracle: xprop -root
        # -version -> "xprop 1.2.8")
        code, out, err = self.run_cli("-root", "-version", backend="fake")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, b"xprop 1.2.8\n")


class ArgErrorsTest(CliTestBase):
    def test_unrecognized_argument(self):
        code, _out, err = self.run_cli("-zorp")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(
            "xprop: unrecognized argument -zorp\n\n" + USAGE_FIRST), err)

    def test_missing_arguments(self):
        cases = (
            (("-id",), "-id requires an argument"),
            (("-name",), "-name requires an argument"),
            (("-display",), "-display requires an argument"),
            (("-id", "0x1", "-len"), "-len requires an argument"),
            (("-id", "0x1", "-fs"), "-fs requires an argument"),
            (("-id", "0x1", "-formats"), "-fs requires an argument"),
            (("-id", "0x1", "-font"), "-font requires an argument"),
            (("-id", "0x1", "-remove"), "-remove requires an argument"),
            (("-id", "0x1", "-set", "A"), "insufficient arguments for -set"),
            (("-id", "0x1", "-f"), "insufficient arguments for -format"),
            (("-id", "0x1", "-f", "A"), "insufficient arguments for "
                                        "-format"),
        )
        for args, msg in cases:
            code, _out, err = self.run_cli(*args)
            self.assertEqual(code, 1, args)
            self.assertTrue(err.startswith("xprop: %s\n\n" % msg),
                            (args, err))

    def test_spec_without_atom(self):
        code, _out, err = self.run_cli("-id", "0x1", "8s")
        self.assertEqual(code, 1)
        self.assertIn("xprop: format specified without atom\n\n", err)
        code, _out, err = self.run_cli("-id", "0x1", "8s", "$0\\n")
        self.assertEqual(code, 1)
        self.assertIn("xprop: dformat specified without atom\n\n", err)

    def test_invalid_window_id(self):
        for bad in ("zzz", "0", "0xzz"):
            code, _out, err = self.run_cli("-id", bad)
            self.assertEqual(code, 1, bad)
            self.assertEqual(
                err, "xprop: error: Invalid window id format: %s.\n" % bad)

    def test_partial_id_parses_like_sscanf(self):
        self.assertEqual(cli._parse_window_id("12abc"), 12)
        self.assertEqual(cli._parse_window_id("0x40000czz"), 0x40000C)
        self.assertEqual(cli._parse_window_id(" 7"), 7)

    def test_bad_format_flag(self):
        code, _out, err = self.run_cli("-id", "0x1", "-f", "A", "s8")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: Bad format: s8.\n")

    def test_font_is_a_clean_error(self):
        code, _out, err = self.run_cli("-font", "fixed")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: Unable to open font fixed!\n")

    def test_explicit_display_that_cannot_open(self):
        code, _out, err = self.run_cli("-display", ":9313", "-root")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop:  unable to open display ':9313'\n")

    def test_mixed_set_and_specs(self):
        # -remove keeps its arity, so the trailing spec is diagnosed
        code, _out, err = self.run_cli("-id", "6", "-remove", "A",
                                       "WM_CLASS", backend="fake")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith(
            "xprop: unrecognized argument WM_CLASS\n\n"), err)

    def test_set_swallows_one_extra_token(self):
        # xprop.c:2052 bug, ported: `-set name value` eats the next arg,
        # so no "unrecognized argument" fires — the native -set error does
        code, _out, err = self.run_cli("-id", "6", "-set", "A", "b",
                                       "WM_CLASS", backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(
            err, "xprop: error: -set cannot work on a native Wayland "
                 "window (it has no X property store)\n")

    def test_no_window_with_name(self):
        code, _out, err = self.run_cli("-name", "nosuchwindowname",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(
            err,
            "xprop: error: No window with name nosuchwindowname exists!\n")

    def test_unknown_id_without_x(self):
        code, _out, err = self.run_cli("-id", "0x999999", backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(
            err, "xprop: error: window id # 0x999999 does not exists!\n")


NATIVE_DUMP = (
    b"_NET_WM_STATE(ATOM) = \n"
    b"_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL\n"
    b"_NET_WM_DESKTOP(CARDINAL) = 0\n"
    b"_NET_WM_PID(CARDINAL) = 4242\n"
    b'WM_CLIENT_MACHINE(STRING) = "testhost"\n'
    b'WM_CLASS(STRING) = "footw", "footw"\n'
    b'_NET_WM_NAME(UTF8_STRING) = "WL-Foot"\n'
    b'WM_NAME(STRING) = "WL-Foot"\n')


class NativePlaneTest(CliTestBase):
    def test_full_dump(self):
        code, out, err = self.run_cli("-id", "6", backend="fake")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, NATIVE_DUMP)

    def test_single_property_for_scripts(self):
        code, out, _err = self.run_cli("-id", "6", "WM_CLASS",
                                      backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n')

    def test_notype_and_len_apply(self):
        code, out, _err = self.run_cli("-notype", "-len", "3", "-id", "6",
                                       "WM_CLASS", backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS = "foo"\n')

    def test_dformat_spec(self):
        code, out, _err = self.run_cli(
            "-id", "6", "8s", " instance=$0 class=$1\\n", "WM_CLASS",
            backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) instance="footw" '
                              b'class="footw"\n')

    def test_no_such_atom_and_not_found(self):
        code, out, _err = self.run_cli("-id", "6", "NOSUCHATOM123", "ATOM",
                                       backend="fake")
        self.assertEqual(code, 0)  # exit stays 0, like the oracle
        self.assertEqual(
            out,
            b"NOSUCHATOM123:  no such atom on any window.\n"
            b"ATOM:  not found.\n")

    def test_f_interns_the_atom(self):
        # after -f NAME 8s the atom exists -> "not found" instead
        code, out, _err = self.run_cli("-id", "6", "-f", "NOSUCHATOM123",
                                       "8s", "NOSUCHATOM123",
                                       backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b"NOSUCHATOM123:  not found.\n")

    def test_name_matches_title_then_app_id(self):
        for sel in ("WL-Foot", "footw"):
            code, out, _err = self.run_cli("-name", sel, "WM_CLASS",
                                          backend="fake")
            self.assertEqual(code, 0, sel)
            self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n')

    def test_xwayland_node_without_x_degrades_to_synthesis(self):
        code, out, _err = self.run_cli("-id", "5", "WM_CLASS",
                                      backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n')

    def test_remove_native_fails_cleanly(self):
        code, _out, err = self.run_cli("-id", "6", "-remove", "WM_NAME",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(
            err, "xprop: error: -remove cannot work on a native Wayland "
                 "window (it has no X property store)\n")

    def test_root_without_x(self):
        code, out, err = self.run_cli("-root", backend="fake")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith(b"_NET_SUPPORTED(ATOM) = "))
        self.assertIn(b"_NET_CLIENT_LIST(WINDOW): window id # 0x5, 0x6",
                      lines)
        self.assertIn(b"_NET_ACTIVE_WINDOW(WINDOW): window id # 0x6",
                      lines)
        self.assertIn(b"_NET_NUMBER_OF_DESKTOPS(CARDINAL) = 2", lines)
        self.assertIn(b"_NET_CURRENT_DESKTOP(CARDINAL) = 0", lines)
        self.assertIn(b"_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x0",
                      lines)

    def test_root_without_anything(self):
        code, _out, err = self.run_cli("-root", backend="none")
        self.assertEqual(code, 1)
        self.assertIn("cannot examine the root window", err)

    def test_click_select_uses_next_focus(self):
        code, out, err = self.run_cli("WM_CLASS", backend="fake")
        self.assertEqual(code, 0)
        self.assertIn("xprop: focus the target window to select it\n", err)
        self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n')

    def test_frame_is_accepted(self):
        code, out, _err = self.run_cli("-frame", "-id", "6", "WM_CLASS",
                                       backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n')

    def test_fullscreen_hidden_sticky_states(self):
        fake = _FakeSway()
        fake.foot["fullscreen_mode"] = 1
        fake.foot["visible"] = False
        fake.foot["sticky"] = True
        out = _CapStdout()
        with mock.patch.object(core, "_detect_backend", lambda: fake), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            code = cli.main(["-id", "6", "_NET_WM_STATE"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.buffer.getvalue(),
            b"_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN, "
            b"_NET_WM_STATE_HIDDEN, _NET_WM_STATE_STICKY\n")


class FormatFileTest(CliTestBase):
    def _file(self, content: bytes) -> str:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".fmt")
        self.addCleanup(os.unlink, f.name)
        f.write(content)
        f.close()
        return f.name

    def test_fs_missing_file(self):
        code, _out, err = self.run_cli(
            "-fs", "/nonexistent-format-file", "-id", "6", "WM_CLASS",
            backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: unable to open file "
                              "/nonexistent-format-file for reading.\n")

    def test_fs_mapping_applies(self):
        path = self._file(b"WM_CLASS 8s ' inst=$0\\n'\n")
        code, out, _err = self.run_cli("-fs", path, "-id", "6", "WM_CLASS",
                                       backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) inst="footw"\n')

    def test_fs_default_dformat_and_line_continuation(self):
        # no quoted dformat -> the default " = $0+\n"; backslash-newline
        # inside quotes is a line continuation
        path = self._file(b"WM_CLASS 8s\nWM_NAME 8s ' a\\\nb\\n'\n")
        code, out, _err = self.run_cli("-fs", path, "-id", "6", "WM_CLASS",
                                       "WM_NAME", backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) = "footw", "footw"\n'
                              b'WM_NAME(STRING) ab\n')

    def test_fs_bad_file(self):
        path = self._file(b"JUSTONE")
        code, _out, err = self.run_cli("-fs", path, "-id", "6",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: Bad format file format.\n")

    def test_fs_unterminated_quote(self):
        path = self._file(b"A 8s 'oops")
        code, _out, err = self.run_cli("-fs", path, "-id", "6",
                                      backend="fake")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xprop: error: Bad format file: "
                              "Unexpected EOF.\n")

    def test_xpropformats_env(self):
        path = self._file(b"WM_CLASS 8s ' env=$0\\n'\n")
        with mock.patch.dict(os.environ, {"XPROPFORMATS": path}):
            code, out, _err = self.run_cli("-id", "6", "WM_CLASS",
                                          backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b'WM_CLASS(STRING) env="footw"\n')


class HelperTest(CliTestBase):
    def test_atoi(self):
        self.assertEqual(cli._atoi("12"), 12)
        self.assertEqual(cli._atoi("  -5x"), -5)
        self.assertEqual(cli._atoi("abc"), 0)
        self.assertEqual(cli._atoi(""), 0)

    def test_strtoul(self):
        self.assertEqual(cli._strtoul("42"), 42)
        self.assertEqual(cli._strtoul("0x10"), 16)
        self.assertEqual(cli._strtoul("010"), 8)
        self.assertEqual(cli._strtoul("089"), 0)   # octal stops at 8
        self.assertEqual(cli._strtoul("abc"), 0)
        self.assertEqual(cli._strtoul("-1"), (1 << 64) - 1)

    def test_strtoul_saturates_on_overflow(self):
        # C strtoul returns ULONG_MAX (ERANGE) on overflow, NOT a mod-2^64
        # wrap. Verified: `-set 32c 18446744073709551617` stores 0xffffffff
        # via the oracle (ULONG_MAX low 32 bits), where a wrap would give 1.
        self.assertEqual(cli._strtoul("18446744073709551617"),
                         (1 << 64) - 1)
        self.assertEqual(cli._pack_ints([cli._strtoul(
            "18446744073709551617")], 32), struct.pack("<I", 0xFFFFFFFF))
        # a huge hex magnitude saturates too
        self.assertEqual(cli._strtoul("0x1" + "0" * 20), (1 << 64) - 1)
        # overflowing NEGATIVE wraps small (glibc: -ULONG_MAX == 1)
        self.assertEqual(cli._strtoul("-18446744073709551617"), 1)

    def test_len_atoi_jank(self):
        # -len abc -> atoi 0 -> everything truncates to no fields
        code, out, _err = self.run_cli("-len", "abc", "-id", "6",
                                       "WM_CLASS", backend="fake")
        self.assertEqual(code, 0)
        self.assertEqual(out, b"WM_CLASS(STRING) = \n")

    def test_lone_dash_stops_selection_scan(self):
        # dsimple's scans stop at "-"; the option loop then chokes on -root
        code, _out, err = self.run_cli("-", "-root", backend="fake")
        self.assertEqual(code, 1)
        self.assertIn("xprop: unrecognized argument -root\n\n", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
