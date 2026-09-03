"""Unit tests for wdotool.input_cmds using a recording fake daemon/backend."""

import contextlib
import io
import os
import tempfile
import unittest

from wdotool import cli, input_cmds
from wdotool.backend import Window, WindowBackend
from wdotool.ctx import CmdError, Context


class FakeDaemon:
    def __init__(self):
        self.calls = []
        self.pos = (0, 0)

    def type_text(self, text, delay_ms):
        self.calls.append(("type", text, delay_ms))

    def key(self, spec, direction, delay_ms, clearmods):
        self.calls.append(("key", spec, direction, delay_ms, clearmods))

    def mousemove_abs(self, x, y):
        self.calls.append(("abs", x, y))
        self.pos = (x, y)

    def mousemove_rel(self, dx, dy):
        self.calls.append(("rel", dx, dy))
        self.pos = (self.pos[0] + dx, self.pos[1] + dy)

    def seed_pointer(self, x, y):
        self.calls.append(("seed", x, y))
        self.pos = (x, y)

    def button(self, btn, down):
        self.calls.append(("button", btn, down))

    def click(self, btn, repeat, delay_ms):
        self.calls.append(("click", btn, repeat, delay_ms))

    def pointer(self):
        return self.pos

    def geometry(self):
        return (1920, 1080)

    def geometry_full(self):
        return (0, 0, 1920, 1080)


class FakeBackend(WindowBackend):
    name = "fake"

    def __init__(self, wins=()):
        self.wins = list(wins)
        self.activated = []

    def list(self):
        return self.wins

    def activate(self, wid):
        self.activated.append(wid)


def make_ctx(wins=()):
    ctx = Context()
    ctx._daemon = FakeDaemon()
    ctx._backend = FakeBackend(wins)
    return ctx


class TestParse(unittest.TestCase):
    LONG = [("clearmodifiers", False), ("delay", True), ("help", False)]
    MAP = {"c": "clearmodifiers", "d": "delay", "h": "help"}

    def parse(self, args):
        return input_cmds._parse("key", args, "usage\n", "cd:h", self.LONG, self.MAP)

    def test_prefix_match(self):
        opts, i, h = self.parse(["--clear", "x"])
        self.assertEqual((opts, i, h), ({"clearmodifiers": True}, 1, False))

    def test_inline_value(self):
        opts, i, h = self.parse(["--delay=5", "x"])
        self.assertEqual((opts, i), ({"delay": "5"}, 1))

    def test_separate_value(self):
        opts, i, h = self.parse(["--delay", "5", "x"])
        self.assertEqual((opts, i), ({"delay": "5"}, 2))

    def test_single_dash_long(self):
        opts, i, h = self.parse(["-delay", "5"])
        self.assertEqual((opts, i), ({"delay": "5"}, 2))

    def test_short_options(self):
        opts, i, h = self.parse(["-c", "1"])
        self.assertEqual((opts, i), ({"clearmodifiers": True}, 1))
        opts, i, h = self.parse(["-d5", "x"])
        self.assertEqual((opts, i), ({"delay": "5"}, 1))

    def test_double_dash_terminator(self):
        opts, i, h = self.parse(["--", "-20"])
        self.assertEqual((opts, i), ({}, 1))

    def test_unknown_option(self):
        with self.assertRaises(CmdError) as cm:
            self.parse(["--bogus"])
        self.assertIn("unrecognized option '--bogus'", str(cm.exception))
        self.assertIn("usage", str(cm.exception))

    def test_missing_argument(self):
        with self.assertRaises(CmdError):
            self.parse(["--delay"])

    def test_stops_at_positional(self):
        opts, i, h = self.parse(["5", "--delay", "1"])
        self.assertEqual((opts, i), ({}, 0))

    def test_help_detected(self):
        opts, i, h = self.parse(["--help", "x"])
        self.assertTrue(h)

    def test_help_before_bad_option_wins(self):
        opts, i, h = self.parse(["--help", "--bogus"])
        self.assertTrue(h)

    def test_bad_option_before_help_raises(self):
        with self.assertRaises(CmdError):
            self.parse(["--bogus", "--help"])


class TestKey(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_key(ctx, ["ctrl+t"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls, [("key", "ctrl+t", "press", 12, False)])

    def test_stops_at_next_command(self):
        ctx = make_ctx()
        n = input_cmds.cmd_key(ctx, ["ctrl+t", "BackSpace", "mousemove", "3", "4"])
        self.assertEqual(n, 2)
        self.assertEqual([c[1] for c in ctx._daemon.calls], ["ctrl+t", "BackSpace"])

    def test_command_detection_case_insensitive(self):
        ctx = make_ctx()
        n = input_cmds.cmd_key(ctx, ["a", "MouseMove", "1", "2"])
        self.assertEqual(n, 1)

    def test_delay_and_clearmodifiers(self):
        ctx = make_ctx()
        input_cmds.cmd_key(ctx, ["--clearmodifiers", "--delay", "5", "a", "b"])
        self.assertEqual(ctx._daemon.calls, [
            ("key", "a", "press", 5, True),
            ("key", "b", "press", 5, False),  # cleared once
        ])

    def test_repeat(self):
        ctx = make_ctx()
        n = input_cmds.cmd_key(ctx, ["--repeat", "3", "a"])
        self.assertEqual(n, 3)
        self.assertEqual(len(ctx._daemon.calls), 3)

    def test_invalid_repeat(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_key(ctx, ["--repeat", "0", "a"])

    def test_no_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_key(ctx, [])

    def test_window_flag_activates(self):
        ctx = make_ctx([Window(id=42)])
        input_cmds.cmd_key(ctx, ["--window", "42", "a"])
        self.assertEqual(ctx._backend.activated, [42])
        self.assertEqual(ctx._daemon.calls, [("key", "a", "press", 12, False)])

    def test_stack_default_window(self):
        ctx = make_ctx([Window(id=7)])
        ctx.stack = [7]
        input_cmds.cmd_key(ctx, ["a"])
        self.assertEqual(ctx._backend.activated, [7])

    def test_keydown_keyup_directions(self):
        ctx = make_ctx()
        input_cmds.cmd_keydown(ctx, ["ctrl"])
        input_cmds.cmd_keyup(ctx, ["ctrl"])
        self.assertEqual([c[2] for c in ctx._daemon.calls], ["down", "up"])

    def test_invalid_sequence_aborts_chain(self):
        ctx = make_ctx()

        def bad_key(spec, direction, delay_ms, clearmods):
            raise CmdError(f"Error: Invalid key sequence '{spec}'")

        ctx._daemon.key = bad_key
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_key(ctx, ["ctrl-x"])
        # Byte-parity with xdotool 4.x: the sequence is converted once per
        # press pass and once per release pass, each failing pass printing
        # BOTH diagnostics and adding 1 to the exit status (B12).
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "Error: Invalid key sequence 'ctrl-x'\n"
            "Failure converting key sequence 'ctrl-x' to keycodes\n"
            "Error: Invalid key sequence 'ctrl-x'\n"
            "Failure converting key sequence 'ctrl-x' to keycodes\n"
            "xdo_send_keysequence_window reported an error for string 'ctrl-x'\n",
        )

    def test_invalid_sequence_keydown_single_pass(self):
        ctx = make_ctx()

        def bad_key(spec, direction, delay_ms, clearmods):
            raise CmdError(f"Error: Invalid key sequence '{spec}'")

        ctx._daemon.key = bad_key
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_keydown(ctx, ["ctrl-x"])
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "Error: Invalid key sequence 'ctrl-x'\n"
            "Failure converting key sequence 'ctrl-x' to keycodes\n"
            "xdo_send_keysequence_window reported an error for string 'ctrl-x'\n",
        )


class TestType(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["hello world"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls, [("type", "hello world", 12)])

    def test_consumes_everything_even_command_names(self):
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["hello", "key", "a"])
        self.assertEqual(n, 3)
        self.assertEqual([c[1] for c in ctx._daemon.calls], ["hello", "key", "a"])

    def test_args_limit(self):
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["--args", "1", "hello", "key", "a"])
        self.assertEqual(n, 3)  # 2 flag tokens + 1 typed arg
        self.assertEqual([c[1] for c in ctx._daemon.calls], ["hello"])

    def test_terminator(self):
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["--terminator", "END", "a", "b", "END", "key", "x"])
        self.assertEqual(n, 5)  # flags + a b + terminator
        self.assertEqual([c[1] for c in ctx._daemon.calls], ["a", "b"])

    def test_args_and_terminator_conflict(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_type(ctx, ["--args", "1", "--terminator", "X", "a"])

    def test_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("line1\nline2\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["--file", path])
        self.assertEqual(n, 2)
        self.assertEqual(ctx._daemon.calls, [("type", "line1\nline2\n", 12)])

    def test_missing_file(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_type(ctx, ["--file", "/nonexistent/x"])

    def test_no_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_type(ctx, [])

    def test_clearmodifiers(self):
        ctx = make_ctx()
        input_cmds.cmd_type(ctx, ["--clearmodifiers", "hi"])
        self.assertEqual(ctx._daemon.calls[0][0], "key")
        self.assertEqual(ctx._daemon.calls[0][2], "up")
        self.assertEqual(ctx._daemon.calls[1], ("type", "hi", 12))

    def test_no_stack_default(self):
        # unlike `key`, `type` ignores the window stack (cmd_type.c)
        ctx = make_ctx([Window(id=7)])
        ctx.stack = [7]
        input_cmds.cmd_type(ctx, ["x"])
        self.assertEqual(ctx._backend.activated, [])


class TestClick(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_click(ctx, ["1"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls, [("click", 1, 1, 100)])

    def test_repeat_delay(self):
        ctx = make_ctx()
        input_cmds.cmd_click(ctx, ["--repeat", "2", "--delay", "50", "3"])
        self.assertEqual(ctx._daemon.calls, [("click", 3, 2, 50)])

    def test_invalid_repeat(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_click(ctx, ["--repeat", "0", "1"])

    def test_no_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_click(ctx, [])

    def test_window_implies_clearmodifiers(self):
        ctx = make_ctx([Window(id=9)])
        input_cmds.cmd_click(ctx, ["--window", "9", "1"])
        self.assertEqual(ctx._backend.activated, [9])
        self.assertEqual(ctx._daemon.calls[0][2], "up")  # modifier clear
        self.assertEqual(ctx._daemon.calls[1], ("click", 1, 1, 100))

    def test_ignores_stack(self):
        ctx = make_ctx([Window(id=9)])
        ctx.stack = [9]
        input_cmds.cmd_click(ctx, ["1"])
        self.assertEqual(ctx._backend.activated, [])


class TestMouseUpDown(unittest.TestCase):
    def test_mousedown(self):
        ctx = make_ctx()
        n = input_cmds.cmd_mousedown(ctx, ["3"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls, [("button", 3, True)])

    def test_mouseup(self):
        ctx = make_ctx()
        input_cmds.cmd_mouseup(ctx, ["1"])
        self.assertEqual(ctx._daemon.calls, [("button", 1, False)])

    def test_no_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_mousedown(ctx, [])


class TestMousemove(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_mousemove(ctx, ["100", "200"])
        self.assertEqual(n, 2)
        self.assertEqual(ctx._daemon.calls, [("abs", 100, 200)])

    def test_restore(self):
        ctx = make_ctx()
        input_cmds.cmd_mousemove(ctx, ["100", "200"])  # saves (0, 0) first
        n = input_cmds.cmd_mousemove(ctx, ["restore"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls[-1], ("abs", 0, 0))

    def test_restore_without_previous(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_mousemove(ctx, ["restore"])

    def test_window_relative(self):
        ctx = make_ctx([Window(id=7, x=50, y=60, w=100, h=100)])
        n = input_cmds.cmd_mousemove(ctx, ["--window", "7", "10", "20"])
        self.assertEqual(n, 4)
        self.assertEqual(ctx._daemon.calls, [("abs", 60, 80)])

    def test_polar_screen_center(self):
        ctx = make_ctx()
        input_cmds.cmd_mousemove(ctx, ["--polar", "90", "100"])
        self.assertEqual(ctx._daemon.calls[-1], ("abs", 1060, 540))

    def test_wrong_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_mousemove(ctx, ["100"])


class TestMousemoveRelative(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_mousemove_relative(ctx, ["--", "-20", "-15"])
        self.assertEqual(n, 3)
        self.assertEqual(ctx._daemon.calls, [("rel", -20, -15)])

    def test_zero_move_is_noop(self):
        ctx = make_ctx()
        n = input_cmds.cmd_mousemove_relative(ctx, ["0", "0"])
        self.assertEqual(n, 2)
        self.assertEqual(ctx._daemon.calls, [])

    def test_polar(self):
        ctx = make_ctx()
        input_cmds.cmd_mousemove_relative(ctx, ["--polar", "90", "100"])
        self.assertEqual(ctx._daemon.calls, [("rel", 100, 0)])

    def test_wrong_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_mousemove_relative(ctx, ["5"])


class TestGetmouselocation(unittest.TestCase):
    def test_output_and_stack(self):
        ctx = make_ctx([
            Window(id=5, x=0, y=0, w=1000, h=1000),
            Window(id=7, x=250, y=350, w=200, h=200),
        ])
        ctx._daemon.pos = (300, 400)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(n, 0)
        self.assertEqual(out.getvalue(), "x:300 y:400 screen:0 window:7\n")
        self.assertEqual(ctx.stack, [7])

    def test_focused_window_wins(self):
        ctx = make_ctx([
            Window(id=5, x=0, y=0, w=1000, h=1000, focused=True),
            Window(id=7, x=250, y=350, w=200, h=200),
        ])
        ctx._daemon.pos = (300, 400)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertIn("window:5", out.getvalue())

    def test_no_window_under_pointer(self):
        ctx = make_ctx()
        ctx._daemon.pos = (10, 10)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:10 y:10 screen:0 window:0\n")

    def test_shell_output(self):
        ctx = make_ctx()
        ctx._daemon.pos = (880, 443)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, ["--shell"])
        self.assertEqual(out.getvalue(), "X=880\nY=443\nSCREEN=0\nWINDOW=0\n")
        self.assertEqual(ctx.stack, [])  # --shell does not update the stack

    def test_shell_prefix(self):
        ctx = make_ctx()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, ["--shell", "--prefix", "M_"])
        self.assertTrue(out.getvalue().startswith("M_X="))

    def test_silent_when_not_last(self):
        ctx = make_ctx()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = input_cmds.cmd_getmouselocation(ctx, ["key", "a"])
        self.assertEqual(n, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(ctx.stack, [0])  # stack still updated


class TestBehaveScreenEdge(unittest.TestCase):
    def test_unsupported(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError) as cm:
            input_cmds.cmd_behave_screen_edge(ctx, ["bottom-left", "key", "a"])
        self.assertIn("not supported", str(cm.exception))

    def test_bad_edge(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError) as cm:
            input_cmds.cmd_behave_screen_edge(ctx, ["middle", "key", "a"])
        self.assertIn("Invalid edge or corner", str(cm.exception))

    def test_too_few_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError) as cm:
            input_cmds.cmd_behave_screen_edge(ctx, ["left"])
        self.assertIn("Invalid number of arguments", str(cm.exception))


class PointerBackend(WindowBackend):
    """A backend that can be asked where the pointer is (GNOME's bridge)."""

    name = "pointer-fake"

    def __init__(self, pos=(2880, 540), wins=(), fail=None):
        self.pos = pos
        self.wins = list(wins)
        self.fail = fail
        self.queries = 0

    def list(self):
        return self.wins

    def activate(self, wid):
        pass

    def pointer(self):
        self.queries += 1
        if self.fail is not None:
            raise self.fail
        return self.pos

class TestRealPointer(unittest.TestCase):
    """B6/B1: the compositor is the source of truth for the pointer, and the
    daemon's model is corrected from it before a relative move."""

    def ctx(self, backend):
        ctx = Context()
        ctx._daemon = FakeDaemon()
        ctx._backend = backend
        return ctx

    def test_getmouselocation_reports_the_compositor(self):
        b = PointerBackend(pos=(2880, 540),
                           wins=[Window(id=9, x=2000, y=500, w=1000, h=200)])
        ctx = self.ctx(b)
        ctx._daemon.pos = (0, 0)          # a daemon that has injected nothing
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:2880 y:540 screen:0 window:9\n")
        self.assertEqual(b.queries, 1)
        # ... and the daemon's model is corrected from it
        self.assertEqual(ctx._daemon.calls, [("seed", 2880, 540)])
        self.assertEqual(ctx._daemon.pos, (2880, 540))

    def test_relative_move_counts_from_the_real_position(self):
        b = PointerBackend(pos=(1662, 601))   # a physical mouse moved it here
        ctx = self.ctx(b)
        ctx._daemon.pos = (1200, 601)         # what this daemon last injected
        input_cmds.cmd_mousemove_relative(ctx, ["500", "0"])
        self.assertEqual(ctx._daemon.calls,
                         [("seed", 1662, 601), ("rel", 500, 0)])
        self.assertEqual(ctx._daemon.pos, (2162, 601))

    def test_restore_captures_the_real_position(self):
        b = PointerBackend(pos=(77, 88))
        ctx = self.ctx(b)
        input_cmds.cmd_mousemove(ctx, ["100", "200"])
        input_cmds.cmd_mousemove(ctx, ["restore"])
        self.assertEqual(ctx._daemon.calls[-1], ("abs", 77, 88))

    def test_a_backend_without_a_pointer_query_keeps_the_model(self):
        ctx = make_ctx()                      # FakeBackend: no pointer()
        ctx._daemon.pos = (300, 400)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:300 y:400 screen:0 window:0\n")
        self.assertNotIn("seed", [c[0] for c in ctx._daemon.calls])

    def test_no_session_falls_back_to_the_model(self):
        b = PointerBackend(fail=CmdError("gnome backend: the bridge vanished"))
        ctx = self.ctx(b)
        ctx._daemon.pos = (5, 6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:5 y:6 screen:0 window:0\n")

    def test_a_daemon_that_cannot_be_seeded_is_not_fatal(self):
        b = PointerBackend(pos=(11, 22))
        ctx = self.ctx(b)

        def boom(x, y):
            raise CmdError("cannot start wdotool daemon")

        ctx._daemon.seed_pointer = boom
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:11 y:22 screen:0 window:0\n")

class TestStrtonum(unittest.TestCase):
    """B14: C strtoul(s, NULL, 0), not Python's int(s, 0)."""

    def test_c_bases(self):
        cases = {
            "0755": 0o755,      # C octal; int(s, 0) rejects it outright
            "0b101": 0,         # strtoul stops at the 'b'; int(s, 0) says 5
            "0x1f": 31, "0X1F": 31, "0": 0, "08": 0, "  12": 12,
            "12ms": 12, "-5": -5, "+7": 7, "": 0, "abc": 0, "0xzz": 0,
        }
        for text, want in cases.items():
            self.assertEqual(input_cmds._strtonum(text), want, text)

    def test_delay_option_uses_it(self):
        ctx = make_ctx()
        input_cmds.cmd_key(ctx, ["--delay", "0755", "a"])
        self.assertEqual(ctx._daemon.calls[-1][3], 0o755)
        ctx = make_ctx()
        input_cmds.cmd_key(ctx, ["--delay", "0b101", "a"])
        self.assertEqual(ctx._daemon.calls[-1][3], 0)

if __name__ == "__main__":
    unittest.main()
