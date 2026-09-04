#!/usr/bin/env python3
"""`wdotool keys watch|explain`.

Watch mode is tested over *recorded* evdev streams -- the same
(timestamp, keycode, value) triples the kernel hands out -- because the two
things it exists to get right, chord-versus-sequence and which physical key
carried level three, are pure ordering and pure layout, and both are invisible
in a live session that only has one keyboard on one layout. Explain mode is
tested over the recorded keymaps in tests/fixtures/keymaps, which is where the
layout truth already lives.

The last class is the point of the whole thing: every reproduction the tool
prints is fed back through the injection path and has to produce the keystrokes
it claimed.
"""

import contextlib
import errno
import io
import os
import shlex
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Never hand this process over to the real X11 tools (see tests/conftest.py).
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

from wdotool import cli, commands, daemon, keys_cmds, keystate  # noqa: E402

KEYMAPS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "keymaps")


def km(name):
    return os.path.join(KEYMAPS, name + ".xkb")


@contextlib.contextmanager
def env(**kw):
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


# ---------------------------------------------------------------------------
# the device layer, faked


def caps_bitmap(codes):
    buf = bytearray(keystate._KEY_BYTES)
    for c in codes:
        buf[c >> 3] |= 1 << (c & 7)
    return bytes(buf)


KEYBOARD_CAPS = caps_bitmap([1, 30, 42, 100])     # Esc, a, shift, right alt
MOUSE_CAPS = caps_bitmap([0x110, 0x111])          # BTN_LEFT/RIGHT only


class FakeEvdev:
    """keystate.Evdev's five calls plus read(), over a dict of fake nodes."""

    def __init__(self, nodes):
        self.nodes = dict(nodes)   # path -> dict(name=, caps=, data=, denied=)
        self.open_fds = {}
        self._next = 10

    def paths(self):
        return sorted(self.nodes)

    def open(self, path):
        node = self.nodes.get(path)
        if node is None:
            raise FileNotFoundError(errno.ENOENT, "no such device")
        if node.get("denied"):
            raise PermissionError(errno.EACCES, "Permission denied")
        fd = self._next
        self._next += 1
        self.open_fds[fd] = path
        return fd

    def close(self, fd):
        self.open_fds.pop(fd, None)

    def _node(self, fd):
        return self.nodes[self.open_fds[fd]]

    def name(self, fd):
        return self._node(fd)["name"]

    def key_caps(self, fd):
        return self._node(fd).get("caps", KEYBOARD_CAPS)

    def key_state(self, fd):
        return caps_bitmap(())

    def read(self, fd, n):
        node = self._node(fd)
        if node.get("gone"):
            raise OSError(errno.ENODEV, "No such device")
        data, node["data"] = node.get("data", b"")[:n], node.get("data", b"")[n:]
        return data


class PollableDevices(keys_cmds.Devices):
    """The real Devices with the one thing a fake fd cannot do replaced."""

    def _wait(self, timeout):
        return list(self.fds)


class RecordedDevices:
    """The whole device layer replaced by a recorded stream, so the rendering
    tests are about rendering. Running dry raises KeyboardInterrupt, which is
    also how a real watch session ends."""

    def __init__(self, events, path="/dev/input/event3"):
        self.queue = list(events)
        self.path = path
        self.fds = {3: (path, "Recorded keyboard")}
        self.notices = []
        self.denied = self.ours = 0
        self.closed = False

    def scan(self):
        return []

    def poll(self, timeout=0.5):
        if not self.queue:
            raise KeyboardInterrupt
        out, self.queue = self.queue, []
        return [(self.path, t, ty, c, v) for t, ty, c, v in out]

    def close_all(self):
        self.closed = True


def keys(*evs):
    """(time, keycode, down) triples -> the tuples the device layer yields."""
    return [(t, keys_cmds.EV_KEY, c, v) for t, c, v in evs]


def watch(events, *args, devices=None):
    out, err = io.StringIO(), io.StringIO()
    dev = devices if devices is not None else RecordedDevices(events)
    with env(WDOTOOL_LAYOUT=None, WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None):
        rc = keys_cmds.watch_main(list(args), devices=dev, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def table(out):
    """Just the event lines (the `= ...` summaries dropped)."""
    return [ln for ln in out.splitlines() if not ln.startswith("=")]


def summaries(out):
    return [ln for ln in out.splitlines() if ln.startswith("=")]


def explain(*args):
    out, err = io.StringIO(), io.StringIO()
    with env(WDOTOOL_LAYOUT=None, WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None):
        rc = keys_cmds.explain_main(list(args), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------


class TestWatchChordsAndSequences(unittest.TestCase):
    """The whole reason the command exists: several keys held at once."""

    def test_a_chord_is_one_line_per_event_plus_one_summary(self):
        rc, out, err = watch(keys((0.0, 100, 1), (0.1, 16, 1),
                                  (0.16, 16, 0), (0.2, 100, 0)),
                             "--keymap", km("de"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(table(out)), 4)          # press and release, both
        self.assertEqual(summaries(out),
                         ["= chord     | wdotool key 108+24 | wdotool type '@'"])
        # the modifier's own press is visible, with the key that carried it
        self.assertIn("down   100 <RALT>", table(out)[0])
        self.assertIn("level3", table(out)[1])
        self.assertIn("'@'", table(out)[1])

    def test_both_reproductions_are_on_every_line(self):
        _rc, out, _err = watch(keys((0.0, 100, 1), (0.1, 16, 1),
                                    (0.16, 16, 0), (0.2, 100, 0)),
                               "--keymap", km("de"))
        for line in table(out):
            self.assertIn("wdotool ", line)
            self.assertEqual(line.count("wdotool "), 2, line)

    def test_a_dead_key_pair_is_two_runs_not_a_chord(self):
        """Written down, `´ e` and AltGr+q look alike. The press/release
        order is the only thing that says which happened."""
        _rc, out, _err = watch(keys((0.0, 13, 1), (0.08, 13, 0),
                                    (0.22, 18, 1), (0.30, 18, 0)),
                               "--keymap", km("de"))
        self.assertEqual(len(table(out)), 4)
        pair = [s for s in summaries(out) if s.startswith("= dead pair")]
        self.assertEqual(len(pair), 1)
        self.assertIn("wdotool key 21 key 26", pair[0])
        self.assertIn("wdotool type 'é'", pair[0])
        self.assertIn("two presses in order, not a chord", pair[0])
        # and the two presses never overlapped
        self.assertIn("up      13", table(out)[1])

    def test_overlapping_presses_released_in_press_order(self):
        _rc, out, _err = watch(keys((0.0, 30, 1), (0.04, 48, 1),
                                    (0.09, 30, 0), (0.14, 48, 0)),
                               "--keymap", km("us"))
        s = summaries(out)
        self.assertEqual(len(s), 1)
        self.assertTrue(s[0].startswith("= sequence"), s[0])
        self.assertIn("wdotool keydown 38 keydown 56 keyup 38 keyup 56", s[0])
        self.assertIn("wdotool keydown a keydown b keyup a keyup b", s[0])
        self.assertIn("2 keys held at once", s[0])

    def test_overlapping_presses_released_in_the_other_order(self):
        """The same two keys, the other release order: a different sequence,
        and still never a chord."""
        _rc, out, _err = watch(keys((0.0, 30, 1), (0.04, 48, 1),
                                    (0.09, 48, 0), (0.14, 30, 0)),
                               "--keymap", km("us"))
        s = summaries(out)
        self.assertIn("wdotool keydown 38 keydown 56 keyup 56 keyup 38", s[0])
        self.assertNotIn("chord", s[0])

    def test_a_key_released_before_another_is_pressed_is_a_sequence(self):
        _rc, out, _err = watch(keys((0.0, 42, 1), (0.04, 30, 1), (0.06, 42, 0),
                                    (0.09, 48, 1), (0.14, 48, 0), (0.2, 30, 0)),
                               "--keymap", km("us"))
        s = summaries(out)
        self.assertEqual(len(s), 1)
        self.assertIn("released out of order", s[0])

    def test_a_modifier_pressed_after_the_key_is_not_that_chord(self):
        """down a, down shift, up a, up shift types `a`, not `A`; rendering it
        as `key shift+a` would be a different keystroke."""
        _rc, out, _err = watch(keys((0.0, 30, 1), (0.04, 42, 1),
                                    (0.09, 30, 0), (0.14, 42, 0)),
                               "--keymap", km("us"))
        s = summaries(out)
        self.assertIn("a modifier was pressed after the key", s[0])
        self.assertIn("wdotool keydown 38 keydown 50 keyup 38 keyup 50", s[0])

    def test_a_modifier_chord_keeps_the_ctrl_token(self):
        _rc, out, _err = watch(keys((0.0, 29, 1), (0.03, 42, 1), (0.06, 20, 1),
                                    (0.12, 20, 0), (0.15, 42, 0), (0.18, 29, 0)),
                               "--keymap", km("us"))
        self.assertEqual(summaries(out),
                         ["= chord     | wdotool key 37+50+28 | wdotool key ctrl+T"])

    def test_a_run_still_held_when_watching_stops_is_flushed(self):
        _rc, out, _err = watch(keys((0.0, 42, 1), (0.04, 30, 1)),
                               "--keymap", km("us"))
        s = summaries(out)
        self.assertIn("still held when watching stopped", s[0])
        self.assertIn("wdotool keydown 50 keydown 38", s[0])


class TestWatchLayoutFacts(unittest.TestCase):
    def test_the_third_level_key_is_the_one_actually_pressed(self):
        """Neo puts ISO_Level3_Shift on <CAPS> and <BKSL>; right Alt is
        level *five* there. Watching must report key 58, not the key wdotool
        would have chosen (<LVL3>, 84) and not an assumed right Alt (100)."""
        _rc, out, err = watch(keys((0.0, 58, 1), (0.07, 32, 1),
                                   (0.12, 32, 0), (0.18, 58, 0)),
                              "--keymap", km("neo"))
        lines = table(out)
        self.assertIn("down    58 <CAPS>", lines[0])
        self.assertIn("ISO_Level3_Shift", lines[0])
        self.assertIn("level3", lines[1])
        self.assertIn("'{'", lines[1])
        self.assertEqual(summaries(out),
                         ["= chord     | wdotool key 66+40 | wdotool type '{'"])
        # ... while the header still says which key wdotool itself presses
        self.assertIn("level3 = key 84 <LVL3>", err)

    def test_the_same_keycode_reads_differently_per_layout(self):
        for name, produced in (("us", "'q'"), ("de", "'q'"), ("fr", "'a'"),
                               ("dvorak", "'\"'")):
            _rc, out, _err = watch(keys((0.0, 16, 1), (0.05, 16, 0)),
                                   "--keymap", km(name))
            self.assertIn(produced, table(out)[0], name)

    def test_a_named_key_has_no_character(self):
        _rc, out, _err = watch(keys((0.0, 28, 1), (0.05, 28, 0)),
                               "--keymap", km("us"))
        self.assertIn("Return", table(out)[0])
        self.assertIn("wdotool key Return", table(out)[0])

    def test_caps_lock_is_tracked(self):
        _rc, out, _err = watch(keys((0.0, 58, 1), (0.02, 58, 0),
                                    (0.1, 30, 1), (0.15, 30, 0)),
                               "--keymap", km("us"))
        line = table(out)[2]
        self.assertIn("caps", line)
        self.assertIn("'A'", line)

    def test_the_group_can_be_chosen(self):
        _rc, out, _err = watch(keys((0.0, 21, 1), (0.05, 21, 0)),
                               "--keymap", km("us_de"), "--group", "2")
        self.assertIn("'z'", table(out)[0])


class TestWatchOptions(unittest.TestCase):
    def test_count_stops_after_n_events(self):
        rc, out, _err = watch(keys((0.0, 30, 1), (0.05, 30, 0),
                                   (0.1, 48, 1), (0.15, 48, 0)),
                              "--keymap", km("us"), "--count", "2")
        self.assertEqual(rc, 0)
        self.assertEqual(len(table(out)), 2)

    def test_raw_prints_unfiltered_events(self):
        evs = [(0.0, keys_cmds.EV_MSC, 4, 458756),
               (0.0, keys_cmds.EV_KEY, 30, 1),
               (0.0, keys_cmds.EV_SYN, 0, 0),
               (0.05, keys_cmds.EV_KEY, 30, 2)]        # autorepeat
        _rc, out, _err = watch(evs, "--keymap", km("us"), "--raw")
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("EV_MSC", lines[0])
        self.assertIn("EV_SYN", lines[2])
        self.assertIn("EV_KEY", lines[3])
        self.assertTrue(lines[3].endswith(" 30 2"))     # the repeat is visible

    def test_autorepeat_is_not_a_new_event_in_the_table(self):
        evs = (keys((0.0, 30, 1)) + [(0.05, keys_cmds.EV_KEY, 30, 2)]
               + keys((0.1, 30, 0)))
        _rc, out, _err = watch(evs, "--keymap", km("us"))
        self.assertEqual(len(table(out)), 2)

    def test_help_and_bad_options(self):
        rc, out, _err = watch([], "--help")
        self.assertEqual(rc, 0)
        self.assertIn("Usage: wdotool keys watch", out)
        rc, _out, err = watch([], "--bogus")
        self.assertEqual(rc, 1)
        self.assertIn("unknown option", err)
        rc, _out, err = watch([], "--count", "lots")
        self.assertEqual(rc, 1)
        self.assertIn("wants a number", err)

    def test_ctrl_c_leaves_nothing_behind(self):
        dev = RecordedDevices(keys((0.0, 30, 1), (0.05, 30, 0)))
        rc, _out, _err = watch(None, "--keymap", km("us"), devices=dev)
        self.assertEqual(rc, 0)
        self.assertTrue(dev.closed)


class TestWatchDevices(unittest.TestCase):
    def devices(self, nodes):
        return PollableDevices(evdev=FakeEvdev(nodes))

    def test_our_own_virtual_keyboard_is_never_recorded(self):
        dev = self.devices({
            "/dev/input/event0": {"name": "AT Translated Set 2 keyboard"},
            "/dev/input/event1": {"name": "wdotool virtual keyboard"},
        })
        dev.scan()
        self.assertEqual([p for p, _n in dev.fds.values()], ["/dev/input/event0"])
        self.assertEqual(dev.ours, 1)

    def test_a_mouse_is_not_a_keyboard(self):
        dev = self.devices({
            "/dev/input/event0": {"name": "Logitech Mouse", "caps": MOUSE_CAPS},
            "/dev/input/event1": {"name": "Keyboard"},
        })
        dev.scan()
        self.assertEqual([p for p, _n in dev.fds.values()], ["/dev/input/event1"])

    def test_an_unreadable_node_is_counted_not_crashed_on(self):
        dev = self.devices({"/dev/input/event0": {"name": "kbd", "denied": True}})
        dev.scan()
        self.assertEqual(dev.fds, {})
        self.assertEqual(dev.denied, 1)

    def test_a_device_appearing_while_watching_is_picked_up(self):
        fake = FakeEvdev({"/dev/input/event0": {"name": "Keyboard"}})
        dev = PollableDevices(evdev=fake)
        dev.scan()
        self.assertEqual(len(dev.fds), 1)
        fake.nodes["/dev/input/event5"] = {"name": "USB Keyboard"}
        dev._next_scan = 0.0
        dev.poll(0)
        self.assertEqual(len(dev.fds), 2)
        self.assertTrue(any("+ /dev/input/event5" in n for n in dev.notices))

    def test_a_device_disappearing_while_watching_is_dropped(self):
        fake = FakeEvdev({"/dev/input/event0": {"name": "Keyboard"},
                          "/dev/input/event5": {"name": "USB Keyboard"}})
        dev = PollableDevices(evdev=fake)
        dev.scan()
        del fake.nodes["/dev/input/event5"]
        dev._next_scan = 0.0
        dev.poll(0)
        self.assertEqual([p for p, _n in dev.fds.values()], ["/dev/input/event0"])
        self.assertTrue(any("disappeared" in n for n in dev.notices))

    def test_a_node_that_stops_reading_is_dropped(self):
        fake = FakeEvdev({"/dev/input/event0": {"name": "Keyboard", "gone": True}})
        dev = PollableDevices(evdev=fake)
        dev.scan()
        dev.poll(0)
        self.assertEqual(dev.fds, {})

    def test_events_are_decoded_from_the_kernel_struct(self):
        import struct
        data = (struct.pack(keys_cmds._EV_FMT, 5, 500000, keys_cmds.EV_KEY, 30, 1)
                + struct.pack(keys_cmds._EV_FMT, 5, 500000, keys_cmds.EV_SYN, 0, 0))
        fake = FakeEvdev({"/dev/input/event0": {"name": "Keyboard", "data": data}})
        dev = PollableDevices(evdev=fake)
        dev.scan()
        got = dev.poll(0)
        self.assertEqual(got, [("/dev/input/event0", 5.5, keys_cmds.EV_KEY, 30, 1),
                               ("/dev/input/event0", 5.5, keys_cmds.EV_SYN, 0, 0)])

    def test_no_readable_keyboard_says_root_and_exits_cleanly(self):
        dev = self.devices({"/dev/input/event0": {"name": "kbd", "denied": True}})
        out, err = io.StringIO(), io.StringIO()
        rc = keys_cmds.watch_main([], devices=dev, out=out, err=err)
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("no keyboard to read", err.getvalue())
        self.assertIn("does need root", err.getvalue())
        self.assertIn("sudo wdotool keys watch", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class TestExplain(unittest.TestCase):
    def check(self, name, want, *extra):
        rc, out, _err = explain("--keymap", km(name), *extra)
        for needle in want:
            self.assertIn(needle, out, "%s: %r" % (name, needle))
        return rc, out

    def test_it_names_the_layout(self):
        _rc, out = self.check("de", ["layout: German", "group 1 of 2 (assumed)"], "a")
        self.assertIn("level keys: shift = key 42 <LFSH>", out)

    def test_german_altgr(self):
        self.check("de", ["press key 16 <AD01> with level3 (key 100 <RALT>",
                          "wdotool key 108+24", "wdotool type '@'"], "@")

    def test_german_dead_key_pair_is_two_steps(self):
        self.check("de", ["2 presses on German (a dead-key pair",
                          "1. press key 13 <AE12> -> dead_acute",
                          "2. press key 18 <AD03> -> 'e'",
                          "wdotool key 21 key 26",
                          "wdotool type 'é'"], "--chars", "é")

    def test_french_circumflex(self):
        self.check("fr", ["dead-key pair", "dead_circumflex"], "--chars", "ô")

    def test_spanish_and_uk_and_dvorak(self):
        self.check("es", ["press key 39 <AC10>"], "--chars", "ñ")
        self.check("gb", ["wdotool type '£'"], "--chars", "£")
        self.check("dvorak", ["press key 36 <AC07>"], "--chars", "h")

    def test_the_third_level_key_by_its_real_name(self):
        """Neo again: explain must name <LVL3>, which is what wdotool
        presses there, and never assume right Alt."""
        _rc, out = self.check("neo", ["with level3 (key 84 <LVL3>, ISO_Level3_Shift)"],
                              "--chars", "{")
        self.assertNotIn("<RALT>", out)

    def test_unreachable_says_so_and_names_the_layout(self):
        rc, out = self.check("fr", ["'ñ' -- unreachable on French"], "--chars", "ñ")
        self.assertEqual(rc, 1)
        self.assertIn("alone or as a dead-key pair", out)

    def test_a_keysym_name_is_taken_as_one(self):
        self.check("de", ["Return -- 1 press", "wdotool key Return"], "Return")
        self.check("de", ["EuroSign -- 1 press"], "EuroSign")

    def test_a_string_is_taken_character_by_character(self):
        _rc, out = self.check("de", ["'a' --", "'b' --", "'c' --"], "abc")
        self.assertEqual(len([ln for ln in out.splitlines()
                              if ln.startswith("'")]), 3)

    def test_not_a_keysym_name(self):
        rc, out = self.check("de", ["Zzzz -- not a keysym name."], "--keysym", "Zzzz")
        self.assertEqual(rc, 1)

    def test_the_group_can_be_chosen(self):
        self.check("us_de", ["press key 21"], "--group", "2", "--chars", "z")
        self.check("us_de", ["press key 44"], "--group", "1", "--chars", "z")

    def test_help_and_bad_options(self):
        rc, out, _err = explain("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Usage: wdotool keys explain", out)
        rc, _out, err = explain("--bogus")
        self.assertEqual(rc, 1)
        self.assertIn("unknown option", err)
        rc, _out, err = explain()
        self.assertEqual(rc, 1)
        self.assertIn("nothing to explain", err)


class TestExplainFollowsTheTypingPath(unittest.TestCase):
    """Same layout-selection rules as `type`, environment overrides included,
    and no privilege of any kind."""

    def test_wdotool_layout_us_never_reads_the_keymap(self):
        mine = km("de")
        with env(WDOTOOL_LAYOUT="us"):
            out, err = io.StringIO(), io.StringIO()
            with env(WDOTOOL_XKB_KEYMAP=mine):
                rc = keys_cmds.explain_main(["--chars", "z"], out=out, err=err)
        self.assertEqual(rc, 0)
        self.assertIn("US (built-in table)", out.getvalue())
        self.assertIn("WDOTOOL_LAYOUT=us", out.getvalue())
        self.assertIn("press key 44", out.getvalue())     # US z, not German z

    def test_a_plain_us_session_is_answered_from_the_built_in_table(self):
        _rc, out, _err = explain("--keymap", km("us"), "--chars", "(")
        self.assertIn("this layout is plain US", out)
        self.assertIn("press key 10 <AE09> with shift", out)   # never the keypad

    def test_layout_xkb_forces_the_reverse_map(self):
        with env(WDOTOOL_LAYOUT="xkb"):
            out, err = io.StringIO(), io.StringIO()
            with env(WDOTOOL_XKB_KEYMAP=km("us")):
                rc = keys_cmds.explain_main(["--chars", "("], out=out, err=err)
        self.assertEqual(rc, 0)
        self.assertNotIn("plain US", out.getvalue())
        self.assertIn("press key 10 <AE09> with shift", out.getvalue())

    def test_no_compositor_falls_back_and_says_so(self):
        with env(WAYLAND_DISPLAY=None, XDG_RUNTIME_DIR="/nonexistent",
                 WDOTOOL_XKB_KEYMAP=None, WDOTOOL_LAYOUT=None):
            out, err = io.StringIO(), io.StringIO()
            rc = keys_cmds.explain_main(["--chars", "a"], out=out, err=err)
        self.assertEqual(rc, 0)
        self.assertIn("US (built-in table)", out.getvalue())
        self.assertIn("could not be read", out.getvalue())
        self.assertIn("press key 30", out.getvalue())

    def test_it_opens_no_input_device(self):
        opened = []

        class Loud(keystate.Evdev):
            def open(self, path):
                opened.append(path)
                raise AssertionError("explain must not touch /dev/input")

        real = keys_cmds.StreamEvdev
        keys_cmds.StreamEvdev = Loud
        try:
            rc, _out, _err = explain("--keymap", km("de"), "--chars", "é")
        finally:
            keys_cmds.StreamEvdev = real
        self.assertEqual(rc, 0)
        self.assertEqual(opened, [])

    def test_the_environment_override_is_put_back(self):
        with env(WDOTOOL_XKB_KEYMAP="/somewhere/else", WDOTOOL_XKB_GROUP="3"):
            keys_cmds.explain_main(["--keymap", km("de"), "--group", "1", "a"],
                                   out=io.StringIO(), err=io.StringIO())
            self.assertEqual(os.environ["WDOTOOL_XKB_KEYMAP"], "/somewhere/else")
            self.assertEqual(os.environ["WDOTOOL_XKB_GROUP"], "3")


# ---------------------------------------------------------------------------
# the round trip


class Recorder:
    def __init__(self):
        self.events = []

    def emit(self, etype, code, value):
        pass

    def syn(self):
        pass

    def key(self, code, down):
        self.events.append((code, 1 if down else 0))

    def close(self):
        pass


def inject(command, keymap_file, group=None):
    """Run a printed reproduction through the real injection path and return
    the (keycode, down) events it would put on the wire."""
    d = daemon._Daemon()
    d.kb = d.mouse = d.tablet = Recorder()
    d.dev_error = None
    d.geom = (0, 0, 1920, 1080)
    d._rel_abs = False
    toks = shlex.split(command)
    assert toks[0] == "wdotool", command
    toks = toks[1:]
    with env(WDOTOOL_XKB_KEYMAP=keymap_file, WDOTOOL_XKB_GROUP=group,
             WDOTOOL_LAYOUT=None), contextlib.redirect_stderr(io.StringIO()):
        while toks:
            op, arg, toks = toks[0], toks[1], toks[2:]
            if op == "type":
                d.op_type(arg, 0, False)
            else:
                d.op_key(arg, {"key": "press", "keydown": "down",
                               "keyup": "up"}[op], 0, False)
    return d.kb.events


class TestTheReproductionsRoundTrip(unittest.TestCase):
    """Whatever the tool printed, fed back to wdotool, has to produce the
    keystrokes it claimed. Both columns, on every shape."""

    def both(self, line):
        """The two reproductions out of one line. Summary lines are
        pipe-separated; event lines are column-aligned, so two spaces end a
        field there."""
        if "|" in line:
            fields = [f.strip() for f in line.split("|")]
            return [f for f in fields if f.startswith("wdotool ")]
        return ["wdotool " + p.split("  ")[0].strip()
                for p in line.split("wdotool ")[1:]]

    def test_a_chord_replays_to_the_same_keys(self):
        _rc, out, _err = watch(keys((0.0, 100, 1), (0.1, 16, 1),
                                    (0.16, 16, 0), (0.2, 100, 0)),
                               "--keymap", km("de"))
        replay, portable = self.both(summaries(out)[0])
        self.assertEqual(inject(replay, km("de")),
                         [(100, 1), (16, 1), (100, 0), (16, 0)])
        self.assertEqual(inject(portable, km("de")),
                         [(100, 1), (16, 1), (16, 0), (100, 0)])

    def test_a_dead_pair_replays_to_the_same_keys(self):
        _rc, out, _err = watch(keys((0.0, 13, 1), (0.08, 13, 0),
                                    (0.22, 18, 1), (0.30, 18, 0)),
                               "--keymap", km("de"))
        line = [s for s in summaries(out) if s.startswith("= dead pair")][0]
        replay, portable = self.both(line)
        want = [(13, 1), (13, 0), (18, 1), (18, 0)]
        self.assertEqual(inject(replay, km("de")), want)
        self.assertEqual(inject(portable, km("de")), want)

    def test_a_sequence_replays_in_the_recorded_order(self):
        _rc, out, _err = watch(keys((0.0, 30, 1), (0.04, 48, 1),
                                    (0.09, 48, 0), (0.14, 30, 0)),
                               "--keymap", km("us"))
        replay, portable = self.both(summaries(out)[0])
        want = [(30, 1), (48, 1), (48, 0), (30, 0)]
        self.assertEqual(inject(replay, km("us")), want)
        self.assertEqual(inject(portable, km("us")), want)

    def test_a_neo_chord_replays_through_whichever_level3_key(self):
        """The recorded keycodes replay the key the *user* pressed (<CAPS>);
        the character form replays through the key wdotool picks (<LVL3>).
        Both are right, which is why both are printed."""
        _rc, out, _err = watch(keys((0.0, 58, 1), (0.07, 32, 1),
                                    (0.12, 32, 0), (0.18, 58, 0)),
                               "--keymap", km("neo"))
        replay, portable = self.both(summaries(out)[0])
        self.assertEqual(inject(replay, km("neo")),
                         [(58, 1), (32, 1), (58, 0), (32, 0)])
        self.assertEqual(inject(portable, km("neo")),
                         [(84, 1), (32, 1), (32, 0), (84, 0)])

    def test_every_event_line_of_a_chord_replays(self):
        _rc, out, _err = watch(keys((0.0, 100, 1), (0.1, 16, 1),
                                    (0.16, 16, 0), (0.2, 100, 0)),
                               "--keymap", km("de"))
        for line in table(out):
            for cmd in self.both(line):
                events = inject(cmd, km("de"))
                self.assertTrue(events, cmd)
                if "keyup" in cmd:
                    self.assertTrue(all(v == 0 for _c, v in events), cmd)

    def test_what_explain_prints_types_the_character(self):
        for name, ch, want in (
            ("de", "@", [(100, 1), (16, 1), (16, 0), (100, 0)]),
            ("de", "é", [(13, 1), (13, 0), (18, 1), (18, 0)]),
            ("neo", "{", [(84, 1), (32, 1), (32, 0), (84, 0)]),
            ("fr", "ô", [(26, 1), (26, 0), (24, 1), (24, 0)]),
        ):
            _rc, out, _err = explain("--keymap", km(name), "--chars", ch)
            printed = [ln.strip().split("  ")[0].strip() for ln in out.splitlines()
                       if ln.strip().startswith(("wdotool key ", "wdotool type "))]
            replay, portable = printed[0], printed[1]
            self.assertEqual(inject(portable, km(name)), want, (name, ch))
            got = inject(replay, km(name))
            # the keycode form presses the same keys, in `key`'s own order
            self.assertEqual(sorted(got), sorted(want), (name, ch, replay))


class TestTheCommandItself(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["wdotool", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_it_is_not_in_the_registry_and_not_in_help(self):
        """`help` is byte-compatible with the real xdotool's, which has no
        `keys` -- so, like __daemon and __keymap, this one is routed in
        cli.main and never registered."""
        self.assertFalse(commands.is_command("keys"))
        self.assertIsNone(commands.lookup("keys"))
        _rc, out, _err = self.run_cli("help")
        self.assertNotIn("keys", out.splitlines())
        self.assertNotIn("  keys", out)
        self.assertEqual(len(commands.REGISTRY), 48)

    def test_bare_keys_prints_usage(self):
        rc, _out, err = self.run_cli("keys")
        self.assertEqual(rc, 1)
        self.assertIn("Usage: wdotool keys", err)

    def test_help_mode(self):
        rc, out, _err = self.run_cli("keys", "--help")
        self.assertEqual(rc, 0)
        self.assertIn("watch", out)
        self.assertIn("explain", out)

    def test_an_unknown_mode(self):
        rc, _out, err = self.run_cli("keys", "wibble")
        self.assertEqual(rc, 1)
        self.assertIn("unknown mode 'wibble'", err)

    def test_explain_through_the_cli(self):
        with env(WDOTOOL_LAYOUT=None, WDOTOOL_XKB_KEYMAP=None,
                 WDOTOOL_XKB_GROUP=None):
            rc, out, _err = self.run_cli("keys", "explain", "--keymap",
                                         km("de"), "@")
        self.assertEqual(rc, 0)
        self.assertIn("wdotool type '@'", out)

    def test_it_never_raises(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = keys_cmds.keys_main(["explain", "--keymap", "/nonexistent", "a"])
        self.assertIn(rc, (0, 1))

    def test_the_hidden_keymap_diagnostic_still_works(self):
        """__keymap stays: it dumps the raw keymap text, which is a different
        job (the fixtures in tests/fixtures/keymaps were captured with it)."""
        with env(WDOTOOL_XKB_KEYMAP=None, WDOTOOL_XKB_GROUP=None,
                 WDOTOOL_LAYOUT=None):
            rc, out, _err = self.run_cli("__keymap", "--keymap", km("de"),
                                         "--chars", "@")
        self.assertEqual(rc, 0)
        self.assertIn("'@': key 16+level3", out)


if __name__ == "__main__":
    unittest.main(verbosity=0, exit=False)
    print("test_keys_cmds: OK")
