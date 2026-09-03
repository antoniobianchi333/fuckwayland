#!/usr/bin/env python3
"""wxrandr's backend selection: `--backend NAME`, `--print-backend`,
`--backends`.

Everything here is hermetic — the probes are replaced by a table, so no
compositor, bus or socket is touched.  The one thing that cannot be faked
in-process is the handover itself (`execve`), which lives with the rest of
it in tests/test_passthrough_exec.py against the fake install tree.
"""

import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

from wdotool import passthrough                                 # noqa: E402
from wxrandr import cli                                         # noqa: E402

#: a GNOME session, as the probes would find it
GNOME = {
    "sway": (False, "no sway or i3 IPC socket ($SWAYSOCK)"),
    "kwin": (False, "the compositor does not advertise "
                    "kde_output_management_v2"),
    "mutter": (True, "org.gnome.Mutter.DisplayConfig on the session bus"),
    "wlr": (False, "the compositor does not advertise "
                   "zwlr_output_manager_v1"),
    "x11": (True, "/usr/bin/xrandr"),
}
COMPOSITOR = {"sway": "sway 1.9", "kwin": "KWin", "mutter": "Mutter",
              "wlr": "wlroots", "x11": "X server (RandR)"}
PROTOCOL = {"sway": "sway IPC (i3-ipc)",
            "kwin": "kde_output_management_v2 version 12",
            "mutter": "org.gnome.Mutter.DisplayConfig (D-Bus)",
            "wlr": "zwlr_output_manager_v1 version 4"}


class ProbeStub:
    """Stands in for cli.probe_backend and records the order it was asked
    in — which is how "the detection order is unchanged" is checked."""

    def __init__(self, table):
        self.table = dict(table)
        self.calls = []

    def __call__(self, name, env=None, verbose=False):
        self.calls.append(name)
        ok, why = self.table.get(name, (False, "unknown backend"))
        return cli.Probe(name, ok, reason="" if ok else why,
                         detail=why if ok else "",
                         compositor=COMPOSITOR.get(name) if ok else None,
                         protocol=PROTOCOL.get(name) if ok else None)


class Stubbed(unittest.TestCase):
    """A test case whose probes are a table, not a session."""

    TABLE = GNOME

    def setUp(self):
        self.probe = ProbeStub(self.TABLE)
        self.orig = cli.probe_backend
        cli.probe_backend = self.probe
        self.addCleanup(setattr, cli, "probe_backend", self.orig)
        self.saved = os.environ.get("WXRANDR_BACKEND")
        os.environ.pop("WXRANDR_BACKEND", None)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        if self.saved is None:
            os.environ.pop("WXRANDR_BACKEND", None)
        else:
            os.environ["WXRANDR_BACKEND"] = self.saved

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue()


class Names(unittest.TestCase):
    def test_canonical_and_aliases(self):
        for spelling, want in (("auto", "auto"), ("x11", "x11"),
                               ("sway", "sway"), ("WLR", "wlr"),
                               (" mutter ", "mutter"), ("gnome", "mutter"),
                               ("GNOME", "mutter"), ("kde", "kwin"),
                               ("kwin", "kwin")):
            self.assertEqual(cli.canonical_backend(spelling), want, spelling)
        for bad in ("", None, "wayland", "xrandr", "wlroots", "sway2"):
            self.assertIsNone(cli.canonical_backend(bad), bad)

    def test_the_options_are_not_in_xrandrs_usage(self):
        """A byte-parity clone may only add options the real one has none
        of, and must not mention them where it prints the original's text."""
        for opt in ("--backend", "--print-backend", "--backends"):
            self.assertNotIn(opt, cli.USAGE, opt)


class Lookahead(unittest.TestCase):
    """The argv walk that happens before any parsing, so that main() knows
    whether an X11 session hands over."""

    def scan(self, *argv):
        return cli.scan_backend_argv(list(argv))

    def test_both_spellings(self):
        self.assertEqual(self.scan("--backend", "sway", "--query"),
                         ("sway", False, ["--query"]))
        self.assertEqual(self.scan("--backend=x11", "--query"),
                         ("x11", False, ["--query"]))
        self.assertEqual(self.scan("--query"), (None, False, ["--query"]))

    def test_an_output_named_like_the_flag_is_a_value(self):
        """`--output --backend` names an output `--backend`; the scan must
        read it as parse() does, or it would suppress a handover nobody
        asked to suppress."""
        for argv in (["--output", "--backend", "--off"],
                     ["--output", "--backend=x11", "--auto"],
                     ["--output", "DP-1", "--mode", "--backend"],
                     ["--set", "--backend", "sway"],
                     ["--addmode", "--backend", "sway"],
                     ["--setmonitor", "--backend", "sway", "none"],
                     ["--newmode", "--backend", "1", "2", "3", "4", "5",
                      "6", "7", "8", "9"],
                     ["-d", "--backend"],
                     ["--display", "--backends"]):
            self.assertEqual(cli.scan_backend_argv(list(argv)),
                             (None, False, list(argv)), argv)

    def test_after_a_stanza_it_is_still_the_flag(self):
        self.assertEqual(
            self.scan("--output", "DP-1", "--off", "--backend", "mutter"),
            ("mutter", False, ["--output", "DP-1", "--off"]))
        self.assertEqual(
            self.scan("--newmode", "m", "1", "2", "3", "4", "5", "6", "7",
                      "8", "9", "+hsync", "--backend", "kwin")[0], "kwin")

    def test_informational_options(self):
        self.assertEqual(self.scan("--print-backend"),
                         (None, True, ["--print-backend"]))
        self.assertEqual(self.scan("--backends"), (None, True, ["--backends"]))
        self.assertEqual(self.scan("--backend", "kde", "--print-backend"),
                         ("kde", True, ["--print-backend"]))

    def test_last_one_wins_like_parse(self):
        self.assertEqual(self.scan("--backend", "sway", "--backend=kwin")[0],
                         "kwin")

    def test_a_flag_with_no_value_is_still_the_flag(self):
        """It names nothing, but it *is* there.  Lose that and an X11
        session hands `--backend` to the original, and the user gets its
        `unrecognized option` where ours says what is missing."""
        self.assertEqual(self.scan("--backend"), ("", False, []))
        self.assertEqual(self.scan("--query", "--backend"),
                         ("", False, ["--query"]))
        self.assertIsNone(cli.canonical_backend(""))

    def test_the_walk_agrees_with_the_parser(self):
        """The look-ahead's whole job is to read argv exactly as parse()
        will, one step earlier -- for the flag, for the two informational
        options, and for the argv left over once the flag is removed."""
        cases = [
            ["--query"],
            ["--backend", "sway", "--query"],
            ["--backend=kde", "--print-backend"],
            ["--backends", "--verbose"],
            ["-d", ":0", "--backend", "wlr", "--dryrun"],
            ["--fb", "1920x1080", "--backend=auto", "--dpi", "96"],
            ["--output", "--backend", "--off"],
            ["--output", "DP-1", "--mode", "--backend", "--pos", "0x0"],
            ["--output", "DP-1", "--set", "--backend", "--print-backend"],
            ["--output", "DP-1", "--gamma", "1:1:1", "--backend", "mutter"],
            ["--addmode", "--backend", "--backends"],
            ["--rmmode", "--print-backend"],
            ["--newmode", "--backend", "1", "2", "3", "4", "5", "6", "7",
             "8", "9", "+hsync", "--backend", "kwin"],
            ["--setmonitor", "--backend", "x", "--backends", "--query"],
            ["--output", "DP-1", "--auto", "--backend", "sway",
             "--output", "HDMI-1", "--off"],
        ]
        for argv in cases:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):     # "not supported" warns
                opts = cli.parse(list(argv))
                flag, info, rest = cli.scan_backend_argv(list(argv))
                stripped = cli.parse(list(rest))
            self.assertEqual(cli.canonical_backend(flag), opts.backend, argv)
            self.assertEqual(info, opts.print_backend or opts.list_backends,
                             argv)
            self.assertIsNone(stripped.backend, argv)
            self.assertEqual([s.name for s in stripped.stanzas],
                             [s.name for s in opts.stanzas], argv)
            self.assertEqual(stripped.mode_ops, opts.mode_ops, argv)
            self.assertEqual(stripped.monitor_op, opts.monitor_op, argv)

    def test_an_unparseable_argv_does_not_raise(self):
        for argv in (["--backend"], ["--output"], ["--newmode", "m"],
                     ["--set"], ["--zorp", "--backend", "sway"]):
            cli.scan_backend_argv(list(argv))     # must simply not raise
        self.assertEqual(self.scan("--zorp", "--backend", "sway")[0], "sway")

    def test_an_ordinary_argv_is_handed_on_untouched(self):
        argv = ["--output", "DP-1", "--primary", "--mode", "1920x1080",
                "--pos", "0x0", "--rotate", "normal", "--output", "HDMI-2",
                "--off"]
        self.assertEqual(cli.scan_backend_argv(list(argv)),
                         (None, False, argv))


class Precedence(Stubbed):
    def test_flag_beats_environment_beats_detection(self):
        os.environ["WXRANDR_BACKEND"] = "kwin"
        self.assertEqual(cli.resolve_backend("sway"),
                         ("sway", "flag", "--backend sway"))
        self.assertEqual(cli.resolve_backend(None),
                         ("kwin", "environment", "WXRANDR_BACKEND=kwin"))
        self.assertEqual(cli.resolve_backend("gnome")[0], "mutter")
        os.environ.pop("WXRANDR_BACKEND")
        self.assertEqual(cli.resolve_backend(None), (None, "detection", None))

    def test_auto_is_not_a_forcing(self):
        os.environ["WXRANDR_BACKEND"] = "auto"
        self.assertEqual(cli.resolve_backend("auto"), (None, "detection", None))
        self.assertEqual(cli.resolve_backend(None), (None, "detection", None))

    def test_x11_is_a_value_the_environment_may_hold(self):
        """It was meaningless before the flag existed; now it means what the
        flag means, and main() reads it there (see test_passthrough_exec)."""
        os.environ["WXRANDR_BACKEND"] = "x11"
        self.assertEqual(cli.resolve_backend(None),
                         ("x11", "environment", "WXRANDR_BACKEND=x11"))
        self.assertEqual(cli.resolve_backend("mutter")[0], "mutter")
        self.assertEqual(cli.resolve_backend("auto")[0], "x11")

    def test_detection_order_is_unchanged(self):
        name, _p = cli.detect_wayland()
        self.assertEqual(name, "mutter")
        self.assertEqual(self.probe.calls, ["sway", "kwin", "mutter"])
        self.probe.calls = []
        self.probe.table["sway"] = (True, "IPC socket /run/sway.sock")
        self.assertEqual(cli.detect_wayland()[0], "sway")
        self.assertEqual(self.probe.calls, ["sway"])
        self.probe.calls = []
        self.probe.table["kwin"] = (True, "kde_output_management_v2 version 12")
        self.probe.table["sway"] = (False, "no sway or i3 IPC socket")
        self.assertEqual(cli.detect_wayland()[0], "kwin")
        self.assertEqual(self.probe.calls, ["sway", "kwin"])

    def test_wlr_is_the_fallback_and_is_not_probed_for_it(self):
        self.probe.table["mutter"] = (False, "no session bus")
        self.assertEqual(cli.detect_wayland()[0], "wlr")
        self.assertNotIn("wlr", self.probe.calls)

    def test_chosen_backend_on_an_x11_session(self):
        """Detection asks the session kind first: X11 is where main() hands
        over, so that is the honest answer."""
        env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "PATH": "/nonexist"}
        self.assertEqual(cli.chosen_backend(None, env)[0], "x11")
        self.assertEqual(cli.chosen_backend("mutter", env)[0], "mutter")
        self.assertEqual(cli.chosen_backend("auto", env)[0], "x11")

    def test_the_flag_reaches_the_session(self):
        seen = []

        class FakeSession:
            def __init__(self, forced=None):
                seen.append(forced)
                raise cli.Fatal("stop here\n")
        orig, cli.Session = cli.Session, FakeSession
        try:
            self.assertEqual(self.run_cli("--query")[0], 1)
            self.assertEqual(self.run_cli("--backend", "kde", "--query")[0], 1)
        finally:
            cli.Session = orig
        self.assertEqual(seen, [None, "kwin"])


class Info(Stubbed):
    def test_print_backend_is_one_token(self):
        code, out, err = self.run_cli("--print-backend")
        self.assertEqual((code, out, err), (0, "mutter\n", ""))
        code, out, err = self.run_cli("--backend", "sway", "--print-backend")
        self.assertEqual((code, out), (0, "sway\n"))

    def test_print_backend_verbose(self):
        code, out, err = self.run_cli("--print-backend", "--verbose")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "mutter\n"
                              "session: wayland\n"
                              "chosen by: detection\n"
                              "compositor: Mutter\n"
                              "protocol: org.gnome.Mutter.DisplayConfig "
                              "(D-Bus)\n"
                              "available: yes\n")

    def test_print_backend_verbose_says_why_and_what_is_missing(self):
        os.environ["WXRANDR_BACKEND"] = "kwin"
        code, out, _err = self.run_cli("--print-backend", "--verbose")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "kwin", "session: wayland",
            "chosen by: environment (WXRANDR_BACKEND=kwin)",
            "available: no (the compositor does not advertise "
            "kde_output_management_v2)"])
        code, out, _err = self.run_cli("--backend", "sway", "--print-backend",
                                       "--verbose")
        self.assertEqual(out.splitlines()[2],
                         "chosen by: flag (--backend sway)")

    def test_print_backend_for_x11_names_the_real_xrandr(self):
        env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
        lines = cli.print_backend_lines(None, env, verbose=True)
        self.assertEqual(lines, ["x11", "session: x11", "chosen by: detection",
                                 "compositor: X server (RandR)",
                                 "real xrandr: /usr/bin/xrandr",
                                 "available: yes"])

    def test_backends_table(self):
        code, out, err = self.run_cli("--backends")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         "  sway    unavailable  no sway or i3 IPC socket "
                         "($SWAYSOCK)\n"
                         "  kwin    unavailable  the compositor does not "
                         "advertise kde_output_management_v2\n"
                         "* mutter  available    "
                         "org.gnome.Mutter.DisplayConfig on the session bus\n"
                         "  wlr     unavailable  the compositor does not "
                         "advertise zwlr_output_manager_v1\n"
                         "  x11     available    /usr/bin/xrandr\n")

    def test_backends_marks_what_auto_would_choose_not_what_is_forced(self):
        code, out, _err = self.run_cli("--backend", "sway", "--backends")
        self.assertEqual(code, 0)
        marked = [ln[2:8].strip() for ln in out.splitlines()
                  if ln.startswith("*")]
        self.assertEqual(marked, ["mutter"])

    def test_the_informational_options_never_open_a_session(self):
        class NoSession:
            def __init__(self, forced=None):
                raise AssertionError("--print-backend touched the layout")
        orig, cli.Session = cli.Session, NoSession
        try:
            self.assertEqual(self.run_cli("--print-backend")[0], 0)
            self.assertEqual(self.run_cli("--backends")[0], 0)
            self.assertEqual(self.run_cli("--backend", "kwin",
                                          "--print-backend")[0], 0)
        finally:
            cli.Session = orig


class Errors(Stubbed):
    def test_an_unknown_name_lists_the_valid_ones(self):
        code, out, err = self.run_cli("--backend", "banana", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err,
                         "xrandr: --backend: invalid argument 'banana'; "
                         "valid: auto, x11, sway, wlr, mutter, kwin\n"
                         "Try 'xrandr --help' for more information.\n")
        code, _out, err = self.run_cli("--backend=nope")
        self.assertEqual(code, 1)
        self.assertIn("invalid argument 'nope'", err)

    def test_forcing_an_unavailable_backend_names_what_is_missing(self):
        code, out, err = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: --backend sway is not available in "
                              "this session: no sway or i3 IPC socket "
                              "($SWAYSOCK)\n")
        code, _out, err = self.run_cli("--backend", "kde", "--output", "DP-1",
                                       "--off")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xrandr: --backend kwin is not available in "
                              "this session: the compositor does not "
                              "advertise kde_output_management_v2\n")

    def test_no_silent_fallback(self):
        """The forced one fails; the one auto would have picked is not tried
        behind the user's back."""
        code, _out, err = self.run_cli("--backend", "wlr", "--query")
        self.assertEqual(code, 1)
        self.assertIn("--backend wlr is not available", err)
        self.assertNotIn("mutter", err)

    def test_the_environment_variable_keeps_its_own_behaviour(self):
        """WXRANDR_BACKEND is *not* pre-checked (that would change bytes
        every existing test pins): the backend itself says what is wrong."""
        os.environ["WXRANDR_BACKEND"] = "sway"
        code, _out, err = self.run_cli("--query")
        self.assertEqual(code, 1)
        self.assertNotIn("is not available in this session", err)
        self.assertIn("Can't open display", err)

    def test_x11_from_a_library_call_is_one_line(self):
        """At a command line `--backend x11` never reaches here: main() has
        exec'd the real xrandr.  Embedded, where a process may not be
        replaced, it is a fatal, not a traceback."""
        code, out, err = self.run_cli("--backend", "x11", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: --backend x11 hands over to the real "
                              "xrandr, which an embedded call cannot do\n")


class EnvironmentX11(Stubbed):
    """`WXRANDR_BACKEND=x11` is the same request as `--backend x11`.  At a
    command line main() hands over before parsing (proved with a real
    `execve` in tests/test_passthrough_exec.py); embedded, where a process
    may not be replaced, it is one fatal line -- naming the variable that
    asked, not a flag nobody typed."""

    def test_it_is_one_line_naming_the_variable(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, out, err = self.run_cli("--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: WXRANDR_BACKEND=x11 hands over to the "
                              "real xrandr, which an embedded call cannot "
                              "do\n")

    def test_the_flag_still_beats_it(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, _out, err = self.run_cli("--backend", "sway", "--query")
        self.assertEqual(code, 1)
        self.assertIn("--backend sway is not available", err)
        self.assertNotIn("WXRANDR_BACKEND", err)

    def test_the_informational_options_answer_for_it(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, out, _err = self.run_cli("--print-backend")
        self.assertEqual((code, out), (0, "x11\n"))


class X11Probe(unittest.TestCase):
    """The one probe with no compositor in it: which real xrandr `x11` is."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxr-x11-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_finds_the_real_xrandr(self):
        p = cli.probe_backend("x11", {"PATH": self.tmp})
        self.assertFalse(p.available)
        self.assertEqual(p.reason,
                         "no real xrandr on PATH (install x11-xserver-utils)")
        real = os.path.join(self.tmp, "xrandr")
        with open(real, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(real, stat.S_IRWXU)
        passthrough.reset_cache()
        p = cli.probe_backend("x11", {"PATH": self.tmp})
        self.assertTrue(p.available, p.reason)
        self.assertEqual(p.detail, real)

    def test_an_unusable_override_is_the_reason(self):
        p = cli.probe_backend("x11", {"PATH": self.tmp,
                                      "WXRANDR_REAL_XRANDR": "/no/such/tool"})
        self.assertFalse(p.available)
        self.assertIn("WXRANDR_REAL_XRANDR", p.reason)
        self.assertNotIn("\n", p.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
