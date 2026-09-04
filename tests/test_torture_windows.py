#!/usr/bin/env python3
"""Regression tests from the window/chain torture pass: window stack
reference edge cases, empty-stack defaults on destructive commands, and
the sway backend's floating-resize position restore."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_windows_cmds import make_backend, run
from wdotool.ctx import CmdError, Context

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"


class StackRefTest(unittest.TestCase):
    """%N resolution matches xdotool's window_list(): negative refs count
    from the end (index = len + N), out-of-range refs are one-line errors,
    never IndexError tracebacks."""

    def _ctx(self, stack):
        ctx = Context()
        ctx.stack = list(stack)
        return ctx

    def test_negative_in_range(self):
        # xdotool: index = nwindows + N, then windows[index - 1].
        ctx = self._ctx([10, 20, 30])
        self.assertEqual(ctx.resolve_window("%-1"), 20)
        self.assertEqual(ctx.resolve_window("%-2"), 10)

    def test_negative_out_of_range_is_cmderror(self):
        # Used to raise IndexError (traceback) instead of a clean CmdError.
        ctx = self._ctx([10, 20])
        with self.assertRaises(CmdError):
            ctx.resolve_window("%-2")
        with self.assertRaises(CmdError):
            ctx.resolve_window("%-5")

    def test_negative_out_of_range_in_chain_is_one_line(self):
        rc, out, err, _ = run(["search", "--class", "beta",
                               "getwindowname", "%-2"])
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("Invalid window stack reference '%-2'", err)

    def test_positive_out_of_range(self):
        ctx = self._ctx([10])
        with self.assertRaises(CmdError):
            ctx.resolve_window("%2")
        with self.assertRaises(CmdError):
            ctx.resolve_window("%0")


class PathologicalRegexTest(unittest.TestCase):
    """re.compile raises RecursionError / OverflowError (not re.error) on
    some patterns; search must fail like a bad regex, not traceback."""

    def test_deeply_nested_groups(self):
        pattern = "(a" * 2000 + ")" * 2000
        rc, out, err, _ = run(["search", "--class", pattern])
        self.assertEqual(rc, 1)
        self.assertIn("Failed to compile regex", err)
        self.assertEqual(out, "")

    def test_huge_repetition_count(self):
        rc, _out, err, _ = run(["search", "--class", "a{99999999999}"])
        self.assertEqual(rc, 1)
        self.assertIn("Failed to compile regex", err)


class EmptyStackDefaultTest(unittest.TestCase):
    """An omitted window argument defaults to %1; with an empty stack that
    is an error, exactly like xdotool (manpage COMMAND CHAINING: 'These
    would error: xdotool windowactivate'). The old focused-window fallback
    made `wdotool windowclose` close the focused window."""

    def test_windowclose_without_stack_errors(self):
        backend = make_backend()
        rc, _out, err, _ = run(["windowclose"], backend=backend)
        self.assertEqual(rc, 1)
        self.assertIn("There are no windows in the stack", err)
        # Nothing was closed.
        self.assertNotIn(("close", 11), backend.calls)

    def test_windowclose_with_stack_uses_percent1(self):
        backend = make_backend()
        rc, _out, _err, _ = run(["search", "--class", "beta", "windowclose"],
                                backend=backend)
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in backend.calls if c[0] == "close"],
                         [("close", 22)])

    def test_windowactivate_without_stack_errors(self):
        rc, _out, err, _ = run(["windowactivate"])
        self.assertEqual(rc, 1)
        self.assertIn("There are no windows in the stack", err)

    def test_resolve_window_explicit_id_still_works(self):
        ctx = Context()
        self.assertEqual(ctx.resolve_window("0x2a"), 42)


class WindowmapSyncTest(unittest.TestCase):
    """windowmap --sync must wait on map state, not visibility: a window on
    an unfocused workspace is mapped but not visible, and the old
    visibility-based predicate hung forever."""

    def test_windowmap_sync_mapped_but_not_visible(self):
        backend = make_backend()
        # Window 33 is visible=False (think: unfocused workspace) but the
        # backend reports it mapped.
        backend.is_mapped = lambda wid: True
        rc, _o, _e, _ = run(["windowmap", "--sync", "33"], backend=backend)
        self.assertEqual(rc, 0)  # returns instead of spinning forever

    def test_windowunmap_sync_uses_map_state(self):
        backend = make_backend()
        mapped = {11: True}
        backend.is_mapped = lambda wid: mapped[wid]
        real_unmap = backend.unmap

        def unmap(wid):
            real_unmap(wid)
            mapped[wid] = False

        backend.unmap = unmap
        rc, _o, _e, _ = run(["windowunmap", "--sync", "11"], backend=backend)
        self.assertEqual(rc, 0)
        self.assertFalse(mapped[11])

    def test_sway_is_mapped(self):
        from wdotool.backend_sway import SCRATCHPAD_WS, SwayBackend

        b = SwayBackend.__new__(SwayBackend)
        b._node = lambda wid: ({}, None, False, SCRATCHPAD_WS)
        self.assertFalse(b.is_mapped(1))
        b._node = lambda wid: ({}, None, False, "2")
        self.assertTrue(b.is_mapped(1))


class SwayResizeRestoreTest(unittest.TestCase):
    """SwayBackend.resize keeps a floating window's top-left corner fixed
    (sway resizes floating containers around their center; X11 xdotool
    keeps the origin)."""

    def _backend(self, floating):
        from wdotool.backend_sway import SwayBackend

        b = SwayBackend.__new__(SwayBackend)
        b.commands = []
        node = {"id": 7, "deco_rect": {"height": 0}}
        from wdotool.backend import Window

        win = Window(id=7, x=100, y=50, w=400, h=300)
        b._node = lambda wid: (node, win, floating, "ws")
        b.run = lambda cmd: b.commands.append(cmd)
        return b

    def test_floating_resize_restores_position(self):
        b = self._backend(floating=True)
        b.resize(7, 500, 400)
        self.assertEqual(b.commands, [
            "[con_id=7] resize set 500 px 400 px",
            "[con_id=7] move absolute position 100 50",
        ])

    def test_tiled_resize_does_not_move(self):
        b = self._backend(floating=False)
        b.resize(7, 500, 400)
        self.assertEqual(b.commands, ["[con_id=7] resize set 500 px 400 px"])


class SwayFullscreenGuardTest(unittest.TestCase):
    """sway ignores move/resize on fullscreen containers, which used to
    make windowsize/windowmove --sync spin forever; the backend now
    refuses with a CmdError instead."""

    def _backend(self):
        from wdotool.backend import Window
        from wdotool.backend_sway import SwayBackend

        b = SwayBackend.__new__(SwayBackend)
        b.commands = []
        node = {"id": 7, "deco_rect": {"height": 0}, "fullscreen_mode": 1}
        win = Window(id=7, x=0, y=0, w=1280, h=720)
        b._node = lambda wid: (node, win, True, "3")
        b.run = lambda cmd: b.commands.append(cmd)
        return b

    def test_resize_fullscreen_refused(self):
        b = self._backend()
        with self.assertRaises(CmdError):
            b.resize(7, 500, 300)
        self.assertEqual(b.commands, [])

    def test_move_fullscreen_refused(self):
        b = self._backend()
        with self.assertRaises(CmdError):
            b.move_window(7, 10, 10)
        self.assertEqual(b.commands, [])


if __name__ == "__main__":
    unittest.main()
