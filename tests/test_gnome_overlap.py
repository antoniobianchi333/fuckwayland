"""`--unsafe-gnome-overlap`: every refusal, the version gate, the exact warning,
and the proof that no other code path can reach the applying one.

The dangerous half of this feature is eight bytes written inside gnome-shell,
and it needs a GNOME session to run at all; everything *around* it does not, and
that is what is here.  The mock org.gnome.Mutter.DisplayConfig from
tests/test_wxrandr_mutter.py is extended with a mock org.gnome.Shell (for the
ShellVersion property the version gate reads) and a mock
org.fuckwayland.Overlap, so a whole `wxrandr --unsafe-gnome-overlap ...`
invocation runs end to end on a bus in this process, and every refusal can be
demanded rather than argued.

The rules the extension applies are in gnome/fuckwayland-overlap@fuckwayland/
rules.js, deliberately in a file with no `gi` imports so that plain node can run
it: `RulesJS` checks it against wxrandr/monitors_xml.py, which is the same rule
written twice, and skips itself where there is no node.

Four things this file is here to hold still:

* the extension is never contacted for a layout GNOME would accept, whatever
  flags were typed;
* nothing but the flag reaches it -- no environment variable, no default, no
  other backend, and not `--persistent`;
* the warning is printed in full, before the call, every time -- these tests
  record no agreement, and tests/test_overlap_consent.py is what happens when
  one is recorded; and
* the shipped type description cannot name Mutter's monitors.xml writer, and
  the apply method is a constant.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from fwcommon import dbus_mini
from fwcommon.dbus_mini import Bus, Message, Variant
import test_wxrandr_mutter as twm
from wxrandr import cli, gnome_overlap, monitors_xml, mutter

os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ERR = dbus_mini.ERR
EXT_DIR = os.path.join(ROOT, "gnome", "fuckwayland-overlap@fuckwayland")
GIR_DIR = os.path.join(ROOT, "gnome", "overlap-typelib")
FLAG = gnome_overlap.FLAG

# The whole of it, byte for byte.  This is the one thing a user gets before the
# session is put at risk, so it is a golden and not a set of `assertIn`s: a
# sentence that quietly stops being printed is a regression, and so is one that
# stops saying how to get back.
WARNING = """\
xrandr: --unsafe-gnome-overlap: GNOME will not place these monitors, so they are going to be
  placed by writing into the running gnome-shell instead of asking it.
  What it does:         move Virtual-2 from +1920+0 to +960+0
                        by writing 8 bytes per monitor inside gnome-shell
                        (GNOME Shell 50.1), through the fuckwayland-overlap@fuckwayland
                        extension, and then asking Mutter to apply the result.
  What it risks:        those 8 bytes go where this build of libmutter keeps a
                        logical monitor's position.  The extension re-checks
                        that this really is such a build before every write --
                        struct size against the GType registry, a sentinel
                        through Mutter's own setter, and every value it reads
                        against what Mutter reports publicly -- and refuses if
                        anything disagrees.  If all of that is wrong anyway,
                        gnome-shell crashes, and on Wayland gnome-shell is the
                        session: every program running in it goes with it.
  What it saves:        nothing.  ~/.config/monitors.xml is never written on
                        this path, so this layout does not survive a logout,
                        and --persistent is refused together with this flag.
  To undo:              wxrandr --output Virtual-1 --pos 0x0 --output Virtual-2 --pos 1920x0
                        or log out and back in.
  If the session dies:  gnome-shell is the session on Wayland, so you land at
                        the login screen.  Log in again -- nothing was saved,
                        so the layout is the one you started with.
                        If a session will not start at all, switch to a text
                        console with Ctrl+Alt+F3, log in and run
                            gnome-extensions disable fuckwayland-overlap@fuckwayland
                        (or delete
                            ~/.local/share/gnome-shell/extensions/fuckwayland-overlap@fuckwayland ),
                        then Ctrl+Alt+F1 back to the login screen.
"""


# ---------------------------------------------------------------- the mocks

class FakeOverlap:
    """The extension, as a bus name that answers Probe and ApplyOverlap.

    `present` False means the name is not on the bus at all -- the common case
    of "installed but the user has not logged out yet", and the one the tool
    has to explain rather than crash on.
    """

    def __init__(self, shell="50.1", present=True):
        self.shell = shell
        self.present = present
        self.calls = []                  # [(member, request dict)]
        self.reply = None                # override the canned answer
        self.error = None                # answer with a D-Bus error instead
        # the build the answer claims to have measured -- what an agreement is
        # recorded against (tests/test_overlap_consent.py)
        self.libmutter = 18
        self.instance_size = 80          # the field; None: an extension too old to say
        self.declared_size = 80          # what the typelib check's own detail reports
        self.strip_typelib_check = False  # ...an extension too old for either
        # the build of libmutter the checks ran against, and anything the
        # extension noticed that decides nothing (see gnome_overlap.notes_text)
        self.build_id = "0f3a1b2c3d4e5f60718293a4b5c6d7e8f9012345"
        self.notes = []

    def answer(self, member, req):
        self.calls.append((member, req))
        if self.reply is not None:
            return self.reply
        monitors = [dict(g, w=1920, h=1080, scale=1, transform=0,
                         primary=(g["x"] == 0))
                    for g in (req.get("want") or req.get("expect") or [])]
        out = {
            "ok": True, "version": 1, "shell": self.shell,
            "libmutter": self.libmutter, "instance_size": self.instance_size,
            "checks": [{"name": "shell-version", "ok": True,
                        "detail": "GNOME Shell %s, libmutter-%s"
                                  % (self.shell, self.libmutter)},
                       {"name": "typelib", "ok": True,
                        "detail": "FwOverlap%s, MetaMonitorsConfig %s bytes as declared"
                                  % (self.libmutter, self.declared_size)},
                       {"name": "sentinel", "ok": True,
                        "detail": "switch_config round-tripped at the declared offset"},
                       {"name": "pending-dialog", "ok": True,
                        "detail": 'nothing holds a modal grab, so GNOME is not '
                                  'asking "Keep changes?"'},
                       {"name": "bounded-read", "ok": True,
                        "detail": "2 logical monitors, every address range-checked"},
                       {"name": "public-view", "ok": True,
                        "detail": "identical to Mutter's public view"}],
            "monitors": monitors,
            "libmutter_build": self.build_id,
            "notes": list(self.notes),
            "wrote": member == "ApplyOverlap",
        }
        if member == "ApplyOverlap":
            out.update({
                "applied": True, "wrote_words": 2,
                "fault": "logical monitors overlap",
                "verify": "refused: Logical monitors not adjacent",
                "saved_config": {"path": "/home/u/.config/monitors.xml",
                                 "before": "absent", "after": "absent",
                                 "unchanged": True},
            })
        if self.instance_size is None:
            out.pop("instance_size")
        if self.strip_typelib_check:
            out["checks"] = [c for c in out["checks"] if c["name"] != "typelib"]
        return out


class _Conn(twm._MutterConn):
    """The Mutter mock's connection, plus org.gnome.Shell's ShellVersion
    property and the overlap extension."""

    SERVED = (gnome_overlap.SHELL_NAME, gnome_overlap.BUS_NAME)

    def dispatch(self, m):
        # the base mock routes by a fixed list of destinations; ours are two more
        if (m.type == dbus_mini.METHOD_CALL and m.destination in self.SERVED
                and m.interface != "org.freedesktop.DBus.Peer"):
            self._test_method(m)
            return
        super().dispatch(m)

    def _bus_method(self, m):
        ov = getattr(self.bus, "overlap", None)
        if m.member == "NameHasOwner":
            name = m.args()[0]
            if name == gnome_overlap.BUS_NAME:
                self.send(Message.method_return(
                    m, "b", (ov is not None and ov.present,)))
                return
            if name == gnome_overlap.SHELL_NAME:
                self.send(Message.method_return(m, "b", (ov is not None,)))
                return
        super()._bus_method(m)

    def _test_method(self, m):
        ov = getattr(self.bus, "overlap", None)
        if m.destination == gnome_overlap.SHELL_NAME:
            if ov is None:
                self.send(Message.error(m, ERR + "ServiceUnknown", "no shell"))
            elif m.interface == dbus_mini.PROPS_IFACE and m.member == "Get":
                _iface, name = m.args()
                if name == "ShellVersion" and ov.shell is not None:
                    self.send(Message.method_return(
                        m, "v", (Variant("s", ov.shell),)))
                else:
                    self.send(Message.error(m, ERR + "InvalidArgs", "no such property"))
            else:
                self.send(Message.error(m, ERR + "UnknownMethod", "no %s" % m.member))
            return
        if m.destination == gnome_overlap.BUS_NAME:
            if ov is None or not ov.present:
                self.send(Message.error(m, ERR + "ServiceUnknown", "not running"))
                return
            req = json.loads(m.args()[0])
            if ov.error is not None:
                ov.calls.append((m.member, req))
                self.send(Message.error(m, ERR + "Failed", ov.error))
                return
            reply = ov.answer(m.member, req)
            # a str is sent as-is, so a test can hand the client something that
            # is not JSON at all
            self.send(Message.method_return(
                m, "s", (reply if isinstance(reply, str) else json.dumps(reply),)))
            return
        super()._test_method(m)


class _MockBus(twm.MutterMockBus):
    def __init__(self, **kw):
        self.overlap = None
        super().__init__(**kw)

    def _accept(self):
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            c = _Conn(self, s)
            with self.lock:
                self.conns.append(c)
            threading.Thread(target=c.serve, daemon=True).start()


VIRTUALS = {
    "Virtual-1": (("Virtual-1", "QEMU", "Virtual", "0"),
                  [twm.M("1920x1080@60.000", 1920, 1080, 60.0, [1.0], preferred=True)],
                  {"width-mm": 508, "height-mm": 286, "is-builtin": False,
                   "display-name": "Virtual 1"}),
    "Virtual-2": (("Virtual-2", "QEMU", "Virtual", "0"),
                  [twm.M("1920x1080@60.000", 1920, 1080, 60.0, [1.0], preferred=True),
                   twm.M("1280x720@60.000", 1280, 720, 60.0, [1.0])],
                  {"width-mm": 508, "height-mm": 286, "is-builtin": False,
                   "display-name": "Virtual 2"}),
}


# FakeMutter looks its connectors up by name in the shared table
twm.MONITORS.update(VIRTUALS)


def two_virtuals():
    return twm.FakeMutter(
        ["Virtual-1", "Virtual-2"],
        [(0, 0, 1.0, 0, True, [("Virtual-1", "1920x1080@60.000")]),
         (1920, 0, 1.0, 0, False, [("Virtual-2", "1920x1080@60.000")])],
        layout_mode=1)


class Case(unittest.TestCase):
    """A GNOME session with two 1920x1080 heads side by side, an overlap
    extension on the bus, and a shell that says it is 50.1."""

    @classmethod
    def setUpClass(cls):
        cls.mock = _MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-overlap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.opened = []
        self.mock.mutter = two_virtuals()
        self.mock.overlap = FakeOverlap()
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg

    def tearDown(self):
        for mo in self.opened:
            mo.close()
        self.mock.mutter = None
        self.mock.overlap = None

    def outputs(self, backend="mutter"):
        mo = mutter.MutterOutputs(bus=Bus(self.mock.address), wl_socket=False)
        self.opened.append(mo)
        return mo

    def run_cli(self, *argv, backend="mutter", env=None):
        tc = self

        def fake_init(sess, forced=None):
            sess.backend = cli.canonical_backend(forced) or backend
            sess.impl = tc.outputs()
            sess.persistent = os.environ.get("WXRANDR_PERSIST", "") not in ("", "0")
            sess.state = core_state(tc.state_path)
        env = env or {}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        orig = cli.Session.__init__
        cli.Session.__init__ = fake_init
        out, err = io.StringIO(), io.StringIO()
        try:
            with _redirect(out, err):
                try:
                    code = cli.main(list(argv))
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 0
        finally:
            cli.Session.__init__ = orig
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, out.getvalue(), err.getvalue()

    # the two questions every test in here asks about a run
    def ext_calls(self):
        return [c[0] for c in self.mock.overlap.calls]

    def applied(self):
        return [c for c in self.mock.mutter.calls if c[1] != 0]


def core_state(path):
    from wxrandr.core import State
    return State("overlap-test", path=path)


class _redirect:
    def __init__(self, out, err):
        self.out, self.err = out, err

    def __enter__(self):
        self._o, self._e = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err

    def __exit__(self, *a):
        sys.stdout, sys.stderr = self._o, self._e
        return False


# ---------------------------------------------------------------- pure logic

class VersionGate(unittest.TestCase):
    def test_the_two_measured_releases_are_the_only_ones(self):
        for v in ("46.0", "46.2", "46", "50.1", "50"):
            self.assertIsNone(gnome_overlap.unsupported_reason(v), v)

    def test_everything_else_is_refused_by_name(self):
        for v in ("45.9", "47.0", "48.4", "49.1", "51.0", "3.38.5"):
            why = gnome_overlap.unsupported_reason(v)
            self.assertIsNotNone(why, v)
            self.assertIn(v, why)
            self.assertIn("46 and 50", why)

    def test_an_unreadable_version_is_refused_not_guessed(self):
        for v in (None, "", "banana", "  ", object()):
            self.assertIn("cannot tell", gnome_overlap.unsupported_reason(v))

    def test_the_gate_matches_what_the_extension_declares(self):
        meta = json.load(open(os.path.join(EXT_DIR, "metadata.json")))
        self.assertEqual(sorted(int(v) for v in meta["shell-version"]),
                         sorted(gnome_overlap.SUPPORTED_MAJORS))


class Shapes(unittest.TestCase):
    PLAN = [{"x": 0, "y": 0, "scale": 1.0, "transform": 0, "primary": True,
             "members": [("Virtual-1", "m1", False)]},
            {"x": 960, "y": 0, "scale": 1.0, "transform": 0, "primary": False,
             "members": [("Virtual-3", "m3", False), ("Virtual-2", "m2", False)]}]

    def test_groups_are_sorted_and_mirrors_stay_together(self):
        self.assertEqual(gnome_overlap.groups(self.PLAN),
                         [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                          {"connectors": ["Virtual-2", "Virtual-3"], "x": 960, "y": 0}])

    def test_rects_need_a_size_for_every_group(self):
        dims = {"Virtual-1": (1920, 1080), "Virtual-3": (1920, 1080)}
        self.assertEqual(gnome_overlap.rects(self.PLAN, dims),
                         [(0, 0, 1920, 1080), (960, 0, 1920, 1080)])
        self.assertIsNone(gnome_overlap.rects(self.PLAN, {"Virtual-1": (1, 1)}))

    def test_moves_text_names_only_what_moves(self):
        before = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                  {"connectors": ["Virtual-2"], "x": 1920, "y": 0}]
        after = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                 {"connectors": ["Virtual-2"], "x": 960, "y": 0}]
        self.assertEqual(gnome_overlap.moves_text(before, after),
                         ["Virtual-2 from +1920+0 to +960+0"])
        self.assertEqual(gnome_overlap.moves_text(before, before), [])

    def test_the_undo_command_is_the_layout_that_is_running_now(self):
        self.assertEqual(
            gnome_overlap.undo_command(
                [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                 {"connectors": ["Virtual-2", "Virtual-3"], "x": 1920, "y": 0}]),
            "wxrandr --output Virtual-1 --pos 0x0 --output Virtual-2 --pos 1920x0")

    def test_only_positions_differ(self):
        current = mutter._canon(self.PLAN)
        moved = [dict(self.PLAN[0]), dict(self.PLAN[1], x=480)]
        self.assertTrue(gnome_overlap.only_positions_differ(moved, current))
        for change in ({"scale": 2.0}, {"transform": 1}, {"primary": True},
                       {"members": [("Virtual-2", "other", False)]}):
            other = [dict(self.PLAN[0]), dict(self.PLAN[1], **change)]
            self.assertFalse(gnome_overlap.only_positions_differ(other, current),
                             change)

    def test_a_dropped_monitor_is_not_only_a_position(self):
        self.assertFalse(gnome_overlap.only_positions_differ(
            [self.PLAN[0]], mutter._canon(self.PLAN)))


class Messages(unittest.TestCase):
    def test_the_warning_is_this(self):
        expect = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                  {"connectors": ["Virtual-2"], "x": 1920, "y": 0}]
        want = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                {"connectors": ["Virtual-2"], "x": 960, "y": 0}]
        self.assertEqual(
            gnome_overlap.warning("50.1", gnome_overlap.moves_text(expect, want),
                                  gnome_overlap.undo_command(expect)),
            WARNING)

    def test_the_warning_survives_a_layout_where_nothing_moves(self):
        # it is never printed in that case (the backend says "nothing to do"
        # first), but a message that can raise IndexError is not a message
        text = gnome_overlap.warning("50.1", [], "wxrandr ...")
        self.assertIn("move nothing (no monitor changes position)", text)

    def test_the_warning_names_every_move(self):
        text = gnome_overlap.warning(
            "46.0", ["A from +0+0 to +10+0", "B from +100+0 to +5+0"], "wxrandr ...")
        self.assertIn("move A from +0+0 to +10+0", text)
        self.assertIn("move B from +100+0 to +5+0", text)

    def test_a_refusal_names_the_check_that_refused(self):
        self.assertEqual(
            gnome_overlap.refusal_text({"ok": False, "check": "struct-size",
                                        "reason": "80 != 72"}),
            "the overlap extension refused (struct-size): 80 != 72\n")

    def test_an_answer_with_no_reason_still_reads(self):
        self.assertIn("no reason given", gnome_overlap.refusal_text({"ok": False}))

    def test_the_saved_file_is_reported_not_assumed(self):
        text = gnome_overlap.applied_text(
            {"verify": "refused: Logical monitors not adjacent",
             "saved_config": {"path": "/p/monitors.xml", "before": "abc",
                              "after": "abc", "unchanged": True}})
        self.assertIn("mutter's own validator on the result: refused: "
                      "Logical monitors not adjacent\n", text)
        self.assertIn("/p/monitors.xml: unchanged (abc)\n", text)

    def test_a_changed_saved_file_is_shouted_about(self):
        text = gnome_overlap.applied_text(
            {"saved_config": {"path": "/p/monitors.xml", "before": "abc",
                              "after": "def", "unchanged": False}})
        self.assertIn("CHANGED across this call (abc -> def)", text)
        self.assertIn("should be impossible", text)

    def test_a_note_is_relayed_and_an_empty_one_is_nothing(self):
        """Notes decide nothing -- they are what the extension noticed and the
        user could not.  Today there is one: libmutter replaced on disk under a
        live session, which `apt upgrade` does and which the session cannot
        otherwise see."""
        self.assertEqual(gnome_overlap.notes_text({"notes": ["libmutter has been replaced"]}),
                         "note: libmutter has been replaced\n")
        self.assertEqual(gnome_overlap.notes_text({}), "")
        self.assertEqual(gnome_overlap.notes_text({"notes": []}), "")
        self.assertEqual(gnome_overlap.notes_text({"notes": [None, ""]}), "")


# ---------------------------------------------------------------- the CLI

class Refusals(Case):
    def test_persistent_and_the_flag_cannot_be_combined(self):
        for argv in ((FLAG, "--persistent"), ("--persistent", FLAG)):
            code, out, err = self.run_cli(*argv, "--output", "Virtual-2",
                                          "--pos", "960x0")
            self.assertEqual(code, 1, argv)
            self.assertIn("--persistent and --unsafe-gnome-overlap cannot be "
                          "used together", err)
            self.assertIn("discards the whole file", err)
            self.assertEqual(self.ext_calls(), [])
            self.assertEqual(self.applied(), [])

    def test_the_flag_means_nothing_off_gnome(self):
        for backend in ("kwin", "sway", "wlr"):
            code, out, err = self.run_cli(FLAG, "--output", "Virtual-2",
                                          "--pos", "960x0", backend=backend)
            self.assertEqual(code, 1, backend)
            self.assertIn("only means anything on GNOME", err)
            self.assertIn(backend, err)
            self.assertEqual(self.ext_calls(), [])

    def test_an_unmeasured_shell_is_refused_before_the_extension_is_asked(self):
        self.mock.overlap.shell = "48.3"
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("GNOME Shell 48.3 is not a build this has been measured on", err)
        self.assertIn("Nothing was changed", err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])

    def test_a_shell_that_will_not_say_its_version_is_refused(self):
        self.mock.overlap.shell = None
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("cannot tell which GNOME Shell this is", err)
        self.assertEqual(self.ext_calls(), [])

    def test_an_extension_that_is_not_running_says_how_to_install_it(self):
        self.mock.overlap.present = False
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("sh gnome/install-overlap.sh", err)
        self.assertIn("log out and back in once", err)
        self.assertEqual(self.applied(), [])

    def test_more_than_a_position_has_to_go_the_ordinary_way_first(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0", "--mode", "1280x720")
        self.assertEqual(code, 1)
        self.assertIn("changes more than where the monitors are", err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])

    def test_a_refusal_from_the_extension_is_relayed_with_its_check(self):
        self.mock.overlap.reply = {"ok": False, "check": "struct-size",
                                   "reason": "this build's MetaMonitorsConfig "
                                             "is 88 bytes"}
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (struct-size)", err)
        self.assertIn("88 bytes", err)
        self.assertEqual(self.applied(), [])

    def test_an_exception_inside_the_shell_is_a_message_not_a_traceback(self):
        self.mock.overlap.error = "TypeError: this.lib.dup_cfg is not a function"
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension failed", err)
        self.assertIn("dup_cfg is not a function", err)
        self.assertEqual(self.applied(), [])

    def test_an_answer_that_is_not_json_is_a_message_too(self):
        ov = self.mock.overlap

        def not_json(member, req):
            ov.calls.append((member, req))
            return "<html>"
        ov.answer = not_json
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("answered something that is not JSON", err)


class TheOrdinaryPathIsUntouched(Case):
    def test_an_adjacent_layout_never_reaches_the_extension(self):
        # the flag is typed, and does nothing whatever: Mutter takes this
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "1920x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertNotIn(FLAG, err)

    def test_a_move_that_stays_adjacent_goes_through_displayconfig(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-1", "--pos", "1920x0",
                                      "--output", "Virtual-2", "--pos", "0x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(len(self.applied()), 1)
        self.assertEqual(self.applied()[0][1], mutter.TEMPORARY)

    def test_without_the_flag_an_overlap_is_still_mutters_refusal(self):
        code, out, err = self.run_cli("--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("GNOME's Mutter refused this layout", err)
        self.assertEqual(self.ext_calls(), [])

    def test_no_environment_variable_turns_it_on(self):
        env = {"WXRANDR_UNSAFE_GNOME_OVERLAP": "1", "WXRANDR_OVERLAP": "1",
               "WXRANDR_UNSAFE": "1", "WXRANDR_GNOME_OVERLAP": "yes"}
        code, out, err = self.run_cli("--output", "Virtual-2", "--pos", "960x0",
                                      env=env)
        self.assertEqual(code, 1)
        self.assertIn("GNOME's Mutter refused this layout", err)
        self.assertEqual(self.ext_calls(), [])

    def test_a_session_built_without_the_flag_has_it_off(self):
        self.assertIs(cli.Session.overlap, False)


class Applying(Case):
    def test_the_whole_run(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        # the warning, in full, before anything happened
        self.assertTrue(err.startswith(WARNING), err[:400])
        # Mutter tests adjacency first, so an overlap and a gap get one sentence
        self.assertIn("GNOME's rule this breaks: logical monitors not adjacent", err)
        self.assertIn("mutter's own validator on the result: refused: "
                      "Logical monitors not adjacent", err)
        self.assertIn("monitors.xml: unchanged", err)
        # DisplayConfig was never asked to apply anything
        self.assertEqual(self.applied(), [])

    def test_what_the_extension_is_told(self):
        self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        member, req = self.mock.overlap.calls[0]
        self.assertEqual(member, "ApplyOverlap")
        self.assertEqual(req["layout_mode"], 1)
        self.assertEqual(req["expect"],
                         [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                          {"connectors": ["Virtual-2"], "x": 1920, "y": 0}])
        self.assertEqual(req["want"],
                         [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                          {"connectors": ["Virtual-2"], "x": 960, "y": 0}])

    def test_a_gap_is_the_same_route_as_an_overlap(self):
        # Mutter refuses both with one sentence; the flag covers both, because
        # what it means is "a layout GNOME will not place"
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "3000x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.assertIn("logical monitors not adjacent", err)

    def test_asking_for_the_layout_that_is_running_writes_nothing(self):
        # the overlap is applied ...
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 0, err)
        # ... and asking for it again does not go near the extension a second
        # time: there is nothing to write, so nothing is written
        self.mock.mutter.logical[1] = (960, 0, 1.0, 0, False,
                                       [("Virtual-2", "1920x1080@60.000")])
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.assertIn("already where this asks for them; nothing was written", err)
        self.assertNotIn("What it risks", err)

    def test_a_note_reaches_the_user_on_an_apply_and_on_a_dryrun(self):
        """The one thing a session cannot see for itself: libmutter replaced
        on disk under it, which every `apt upgrade` of GNOME does.  It changes
        no decision -- the checks ran against the library in memory, which is
        the one being written to -- so it is a note, and a note is printed."""
        self.mock.overlap.notes = ["libmutter has been replaced on disk since "
                                   "this session started"]
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertIn("note: libmutter has been replaced on disk", err)
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertIn("note: libmutter has been replaced on disk", err)

    def test_a_saved_file_that_moved_is_shouted_about(self):
        self.mock.overlap.reply = {
            "ok": True, "applied": True, "monitors": [],
            "verify": "refused: Logical monitors not adjacent",
            "saved_config": {"path": "/p/monitors.xml", "before": "a",
                             "after": "b", "unchanged": False}}
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertIn("CHANGED across this call", err)


class DryRun(Case):
    def test_dryrun_probes_and_writes_nothing(self):
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertTrue(err.startswith(WARNING), err[:200])
        self.assertIn("overlap check struct-size", err.replace("typelib", "struct-size"))
        self.assertIn("dryrun: nothing was written", err)
        self.assertEqual(self.applied(), [])

    def test_dryrun_reports_the_checks_the_extension_ran(self):
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0")
        for name in ("shell-version", "typelib", "sentinel", "pending-dialog",
                     "bounded-read", "public-view"):
            self.assertIn("overlap check %s:" % name, err)

    def test_a_dryrun_of_an_adjacent_layout_is_the_ordinary_one(self):
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "1920x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertIn("mutter verify: ok", err)

    def test_a_dryrun_refusal_still_writes_nothing(self):
        self.mock.overlap.reply = {"ok": False, "check": "sentinel",
                                   "reason": "the tail moved"}
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("(sentinel)", err)
        self.assertEqual(self.applied(), [])


# ------------------------------------------------- nothing else gets in here

class NoOtherWayIn(unittest.TestCase):
    """The applying path has exactly one caller, and it is behind the flag."""

    def _sources(self):
        out = {}
        for sub in ("wxrandr", "warandr", "wdotool", "wwmctl", "wxprop",
                    "wmirror", "fwcommon"):
            d = os.path.join(ROOT, sub)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.endswith(".py"):
                    p = os.path.join(d, name)
                    out["%s/%s" % (sub, name)] = open(p, encoding="utf-8").read()
        return out

    def test_only_two_modules_know_the_client_exists(self):
        importers = [p for p, s in self._sources().items()
                     if re.search(r"\bgnome_overlap\b", s)
                     and p != "wxrandr/gnome_overlap.py"]
        self.assertEqual(sorted(importers), ["wxrandr/cli.py", "wxrandr/mutter.py"])

    def test_apply_overlap_has_one_caller(self):
        callers = [(p, n) for p, s in self._sources().items()
                   for n, line in enumerate(s.splitlines(), 1)
                   if "apply_overlap(" in line and "def apply_overlap" not in line]
        self.assertEqual([p for p, _n in callers], ["wxrandr/cli.py"])

    def test_that_caller_is_behind_the_flag(self):
        src = open(os.path.join(ROOT, "wxrandr", "cli.py"), encoding="utf-8").read()
        body = src[src.index("    def apply(self, targets):"):]
        body = body[:body.index("\n    @property")]
        guard = body.index("if self.overlap and self.backend == \"mutter\":")
        self.assertLess(guard, body.index("apply_overlap("))
        self.assertIn("return self.impl.apply(self.state, targets, self.persistent)", body)

    def test_the_flag_is_the_only_thing_that_sets_it(self):
        src = open(os.path.join(ROOT, "wxrandr", "cli.py"), encoding="utf-8").read()
        setters = [line.strip() for line in src.splitlines()
                   if re.search(r"\.overlap\s*=|^\s*o\.overlap\s*=|overlap = ", line)]
        for line in setters:
            self.assertFalse("environ" in line, line)

    def test_the_applying_method_never_runs_for_a_layout_gnome_accepts(self):
        src = open(os.path.join(ROOT, "wxrandr", "mutter.py"), encoding="utf-8").read()
        for name in ("apply_overlap", "overlap_dryrun"):
            body = src[src.index("    def %s(self" % name):]
            body = body[:body.index("\n    def ", 10)]
            self.assertIn("route = self.overlap_route(state, targets)", body)
            self.assertIn("if route is None:", body)


# ------------------------------------------------- the shipped extension

class ShippedExtension(unittest.TestCase):
    def setUp(self):
        self.js = open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read()

    def test_enable_touches_nothing_but_dbus(self):
        """The whole design rests on this: an extension that is enabled and
        never called must not be able to hurt anybody, including at login."""
        body = self.js[self.js.index("    enable() {"):]
        body = body[:body.index("\n    disable()")]
        for forbidden in ("typelib", "imports.gi", "libmutter", "get_config_manager",
                          "maps", "Guarded", "wr(", "apply(", "Probe", "memdup"):
            self.assertNotIn(forbidden, body, forbidden)
        self.assertIn("Gio.DBusExportedObject.wrapJSObject", body)

    def test_the_pending_dialog_check_reads_a_field_that_exists(self):
        """The guard between this feature and the only lasting damage it can do.

        It used to read `Main.wm._displayChangeDialog`, which exists on neither
        supported release -- measured on 46.0 and 50.1, with the dialog on
        screen and without it -- so it passed always, and a "Keep changes?"
        confirmed while an overlap was applied wrote that overlap into
        ~/.config/monitors.xml, on both.  A check that cannot fire is worse than
        no check, because it is believed.  What it reads now is the modal count,
        which goes 0 -> 1 while the dialog is up.
        """
        body = self.js[self.js.index("    noPendingDialog() {"):]
        body = body[:body.index("\n    }\n") + 6]
        self.assertIn("Main.modalCount", body)
        self.assertIn("modalVerdict(modal)", body)
        self.assertIn("refuse('pending-dialog'", body)
        # the field it cannot see: nowhere in the file, under any spelling
        self.assertNotIn("_displayChangeDialog", self.js)
        # and the pass line is reached only when the verdict was nothing
        self.assertLess(body.index("refuse('pending-dialog'"),
                        body.index("this._pass('pending-dialog'"))

    def test_the_write_passes_no_length_of_its_own(self):
        """The typelib declares the write's length parameter as the length of
        the byte array, so gjs takes it from there and drops a third argument
        with a `JS WARNING: Too many arguments` in the journal.  le32() is four
        bytes by construction."""
        calls = re.findall(r"lib\.wr\((.*?)\);", self.js)
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call.count(","), 1, call)

    def test_the_apply_method_is_a_constant(self):
        self.assertIn("const METHOD_TEMPORARY = 1;", self.js)
        calls = re.findall(r"lib\.apply\(([^)]*)\)", self.js)
        self.assertEqual(calls, ["g.mm, g.cfg, METHOD_TEMPORARY"])
        # 2 (PERSISTENT) is what makes Mutter write monitors.xml: it must not
        # appear anywhere near the apply, under any spelling
        self.assertNotIn("METHOD_PERSISTENT", self.js)
        self.assertEqual(re.findall(r"METHOD_TEMPORARY = (\d+)", self.js), ["1"])

    def test_nothing_can_write_the_saved_configuration(self):
        for name in sorted(os.listdir(GIR_DIR)):
            if not name.endswith(".gir"):
                continue
            text = open(os.path.join(GIR_DIR, name), encoding="utf-8").read()
            syms = re.findall(r'c:identifier="([^"]+)"', text)
            self.assertTrue(syms)
            for sym in syms:
                for bad in ("save", "write", "store", "persist"):
                    self.assertNotIn(bad, sym.lower(), "%s: %s" % (name, sym))
        for name in sorted(os.listdir(os.path.join(EXT_DIR, "typelib"))):
            raw = open(os.path.join(EXT_DIR, "typelib", name), "rb").read()
            self.assertNotIn(b"save_current", raw, name)
            self.assertIn(b"meta_monitor_manager_apply_monitors_config", raw, name)

    def test_one_description_per_measured_generation(self):
        got = sorted(os.listdir(os.path.join(EXT_DIR, "typelib")))
        self.assertEqual(got, ["FwOverlap14-1.0.typelib", "FwOverlap18-1.0.typelib"])

    def test_every_pointer_is_declared_as_a_number(self):
        """The one property that turns a wrong offset from a SIGSEGV into a
        refusal: gjs must never be able to follow a pointer for us."""
        for name in sorted(os.listdir(GIR_DIR)):
            if not name.endswith(".gir"):
                continue
            text = open(os.path.join(GIR_DIR, name), encoding="utf-8").read()
            for record in re.findall(r"<record .*?</record>", text, re.S):
                for field in re.findall(r"<field .*?</field>", record, re.S):
                    if 'c:type="gpointer"' in field or "*" in field:
                        self.fail("%s declares a pointer field: %s" % (name, field))

    def test_the_metadata_is_narrow_and_says_what_it_is(self):
        meta = json.load(open(os.path.join(EXT_DIR, "metadata.json")))
        self.assertEqual(meta["uuid"], gnome_overlap.UUID)
        self.assertEqual(meta["shell-version"], ["46", "50"])
        self.assertIn("unsupported", meta["name"])
        self.assertIn("nothing at all until it is called", meta["description"])

    def test_the_installer_is_its_own(self):
        sh = open(os.path.join(ROOT, "gnome", "install-overlap.sh"),
                  encoding="utf-8").read()
        self.assertIn(gnome_overlap.UUID, sh)
        self.assertNotIn("fuckwayland-bridge@fuckwayland", sh)
        subprocess.run(["sh", "-n", os.path.join(ROOT, "gnome", "install-overlap.sh")],
                       check=True)
        bridge = open(os.path.join(ROOT, "gnome", "install-bridge.sh"),
                      encoding="utf-8").read()
        self.assertNotIn("overlap", bridge.lower())

    def test_the_typelibs_match_the_descriptions_beside_them(self):
        if shutil.which("g-ir-compiler") is None:
            self.skipTest("no g-ir-compiler")
        rc = subprocess.run([sys.executable, os.path.join(GIR_DIR, "gen-gir.py"),
                             "--check"], capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)

    def test_the_declared_sizes_are_the_measured_ones(self):
        try:
            import gi
            gi.require_version("GIRepository", "2.0")
            from gi.repository import GIRepository as G
        except (ImportError, ValueError):
            self.skipTest("no GIRepository")
        repo = G.Repository.get_default()
        repo.prepend_search_path(os.path.join(EXT_DIR, "typelib"))
        for ns, size, list_at in (("FwOverlap14", 72, 40), ("FwOverlap18", 80, 40)):
            repo.require(ns, "1.0", 0)
            info = repo.find_by_name(ns, "ConfigN")
            self.assertEqual(G.struct_info_get_size(info), size, ns)
            fields = {G.struct_info_get_field(info, i).get_name():
                      G.field_info_get_offset(G.struct_info_get_field(info, i))
                      for i in range(G.struct_info_get_n_fields(info))}
            self.assertEqual(fields["logical_monitor_configs"], list_at, ns)
            lmc = repo.find_by_name(ns, "LMCN")
            self.assertEqual(G.field_info_get_offset(G.struct_info_get_field(lmc, 0)), 0)
            self.assertEqual(G.field_info_get_offset(G.struct_info_get_field(lmc, 1)), 4)


class TheLibraryIdentity(unittest.TestCase):
    """Naming the build of libmutter this session runs is bookkeeping, not a
    guard, and the source has to keep it that way: it reads two files and
    decides nothing, so nothing in it may refuse."""

    def setUp(self):
        self.js = open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read()

    def body(self, head):
        body = self.js[self.js.index(head):]
        return body[:body.index("\n}\n") + 3]

    def test_reading_the_build_id_can_never_refuse(self):
        for head in ("function elfBuildId(path) {", "function libmutterIdentity(mapping) {"):
            body = self.body(head)
            self.assertNotIn("refuse(", body, head)
            self.assertIn("return null", body, head)

    def test_it_is_wrapped_where_it_is_called(self):
        """A guard that cannot fire is worse than no guard; a *note* that can
        fire is worse than no note, because it would stop an apply that every
        check had passed."""
        body = self.js[self.js.index("    version() {"):]
        body = body[:body.index("\n    }\n") + 6]
        at = body.index("libmutterIdentity(")
        self.assertIn("try {", body[:at])
        self.assertIn("catch", body[at:])

    def test_a_replaced_library_is_a_note_and_not_a_check(self):
        # the notes go in their own field, and nothing in the checks list
        self.assertIn("notes: [libmutterNote(this.libmutter)]", self.js)
        self.assertNotIn("refuse('libmutter-build'", self.js)
        self.assertNotIn("_pass('libmutter-build'", self.js)


class HeaderDerivation(unittest.TestCase):
    """`gen-gir.py --from-header` against the two releases' own headers.

    This is the answer to a `typelib` or `sentinel` refusal after an upgrade,
    and it is checked here because a deriver nobody has run on a known answer is
    not a deriver.  Both fixtures are excerpts of mutter's own
    src/backends/meta-monitor-config-manager.h, and what comes out of them has
    to be exactly what is shipped -- which is also an independent confirmation
    of the shipped offsets: they were measured on a live compositor, and this
    arrives at the same numbers from upstream source, by arithmetic."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir", os.path.join(GIR_DIR, "gen-gir.py"))
        cls.gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gen)

    def header(self, name):
        return open(os.path.join(ROOT, "tests", "fixtures", "mutter", name),
                    encoding="utf-8").read()

    def test_the_two_headers_derive_the_two_shipped_descriptions(self):
        for name, gen, size in (("meta-monitors-config-46.h", 14, 72),
                                ("meta-monitors-config-50.h", 18, 80)):
            ints, rows, got = self.gen.build_from_header(self.header(name))
            self.assertEqual(got, size, name)
            self.assertEqual(ints, self.gen.BUILDS[gen][1], name)
            at = {f: off for off, _sz, _t, f in rows}
            self.assertEqual(at["logical_monitor_configs"], 40, name)
            self.assertEqual(at["disabled_monitor_specs"], 48, name)
            # the tail is what differs between the two, and it is what the
            # sentinel pins on a live compositor
            self.assertEqual(at["switch_config"], 64 if gen == 14 else 72, name)

    def test_the_generations_really_do_differ(self):
        a = self.gen.build_from_header(self.header("meta-monitors-config-46.h"))
        b = self.gen.build_from_header(self.header("meta-monitors-config-50.h"))
        self.assertNotEqual(a[0], b[0])
        self.assertNotEqual(a[2], b[2])

    def test_a_type_it_does_not_know_is_an_error_and_not_a_guess(self):
        bad = ("struct _MetaMonitorsConfig {\n  GObject parent;\n"
               "  MetaSomethingNew thing;\n};\n")
        with self.assertRaises(SystemExit) as e:
            self.gen.build_from_header(bad)
        self.assertIn("MetaSomethingNew", str(e.exception))

    def test_a_moved_head_is_refused_rather_than_renumbered(self):
        """A release that inserts a field before logical_monitor_configs has
        broken the description's *shape*, and the honest answer is a human
        reading the new struct, not a new number from this script."""
        moved = self.header("meta-monitors-config-46.h").replace(
            "  GObject parent;", "  GObject parent;\n  gint whatever;")
        with self.assertRaises(SystemExit) as e:
            self.gen.build_from_header(moved)
        self.assertIn("written by hand", str(e.exception))

    def test_it_reads_no_running_compositor(self):
        """The independence of the description from the build it is checked
        against is the whole value of the struct-size and sentinel checks.  A
        deriver that asked the compositor would spend it."""
        src = open(os.path.join(GIR_DIR, "gen-gir.py"), encoding="utf-8").read()
        body = src[src.index("def build_from_header"):]
        for forbidden in ("gi.repository", "type_query", "dbus", "gdbus",
                          "/proc/", "libmutter-"):
            self.assertNotIn(forbidden, body[:body.index("def report_header")],
                             forbidden)


# ------------------------------------------------- the rules, run for real

NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(NODE is None, "no node to run rules.js")
class RulesJS(unittest.TestCase):
    """`rules.js` is the extension's half of the decisions, and it is checked
    here against the Python that says the same thing (monitors_xml.fault) --
    which is the only way to test it without a GNOME session."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="rules-js-")
        shutil.copy(os.path.join(EXT_DIR, "rules.js"),
                    os.path.join(cls.dir, "rules.mjs"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def run_js(self, body):
        src = ("import * as R from '%s/rules.mjs';\n"
               "const input = JSON.parse(process.argv[2] || '{}');\n"
               % self.dir) + body
        path = os.path.join(self.dir, "case.mjs")
        with open(path, "w") as fh:
            fh.write(src)
        return path

    def call(self, script, arg):
        rc = subprocess.run([NODE, script, json.dumps(arg)],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        return json.loads(rc.stdout)

    LAYOUTS = [
        [(0, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (960, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (3000, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (3840, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (2880, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (0, 1080, 1920, 1080)],
        [(0, 0, 1920, 1080), (0, 1000, 1920, 1080)],
        [(100, 0, 1920, 1080), (2020, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (1920, 0, 1280, 720)],
        [],
    ]

    def test_it_says_what_mutter_says_and_what_python_says(self):
        script = self.run_js(
            "const out = input.map(r => R.mutterFault("
            "r.map(([x, y, w, h]) => ({x, y, w, h}))));\n"
            "console.log(JSON.stringify(out));\n")
        got = self.call(script, self.LAYOUTS)
        want = [monitors_xml.fault(rects) for rects in self.LAYOUTS]
        # monitors_xml adds a parenthetical to the adjacency sentence; the rest
        # has to agree word for word, and the verdict always has to agree
        for js, py, rects in zip(got, want, self.LAYOUTS):
            if py is None:
                self.assertIsNone(js, rects)
            else:
                self.assertIsNotNone(js, rects)
                self.assertTrue(py.startswith(js), (js, py, rects))

    def test_the_version_gate_is_the_same_on_both_sides(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(v => R.generationFor(v))));\n")
        versions = ["46.0", "46", "50.1", "50", "47.1", "45.4", "51.0", "banana", ""]
        got = self.call(script, versions)
        for v, gen in zip(versions, got):
            self.assertEqual(gen is not None,
                             gnome_overlap.unsupported_reason(v) is None, v)

    def test_the_comparison_rejects_every_single_field(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(c => R.compare(c[0], c[1]))));\n")
        base = {"x": 0, "y": 0, "w": 1920, "h": 1080, "scale": 1,
                "primary": True, "connectors": ["Virtual-1"]}
        cases = [[[base], [base]]]
        for field, bad in (("x", 1), ("y", 1), ("w", 1919), ("h", 1), ("scale", 2),
                           ("primary", False), ("connectors", ["Virtual-2"])):
            cases.append([[dict(base, **{field: bad})], [base]])
        cases.append([[], [base]])                     # a NULL list read
        cases.append([[base, base], [base]])           # garbage that walked too far
        got = self.call(script, cases)
        self.assertEqual(got[0], [], "an identical read must produce no diff")
        for diffs, case in zip(got[1:], cases[1:]):
            self.assertTrue(diffs, case)

    def test_a_missing_public_connector_name_is_not_a_difference(self):
        # GNOME 46 exposes no connector enumeration; the geometry half of the
        # comparison still has to hold, and the names must not fail it
        script = self.run_js(
            "console.log(JSON.stringify(R.compare(input[0], input[1])));\n")
        priv = [{"x": 0, "y": 0, "w": 1920, "h": 1080, "scale": 1,
                 "primary": True, "connectors": ["Virtual-1"]}]
        pub = [dict(priv[0], connectors=[])]
        self.assertEqual(self.call(script, [priv, pub]), [])

    def test_drift_after_the_write_catches_anything_but_the_two_words(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(c => R.drift(c[0], c[1]))));\n")
        want = {"x": 960, "y": 0, "w": 1920, "h": 1080, "scale": 1, "transform": 0,
                "primary": False, "connectors": ["Virtual-2"]}
        cases = [[[want], [want]]]
        for field, bad in (("w", 1280), ("h", 720), ("scale", 2), ("transform", 1),
                           ("primary", True), ("connectors", ["Virtual-9"]),
                           ("x", 0), ("y", 8)):
            cases.append([[dict(want, **{field: bad})], [want]])
        got = self.call(script, cases)
        self.assertEqual(got[0], [])
        for diffs, case in zip(got[1:], cases[1:]):
            self.assertTrue(diffs, case)

    def test_the_pending_dialog_verdict_can_actually_refuse(self):
        """Demanded case by case, because the failure this replaces was a guard
        that returned "fine" for every input there is."""
        script = self.run_js(
            "const cases = {zero: 0, one: 1, two: 2, negative: -1,\n"
            "               missing: undefined, none: null, string: '1',\n"
            "               nan: NaN, fraction: 0.5, object: {}};\n"
            "const out = {};\n"
            "for (const k of Object.keys(cases)) out[k] = R.modalVerdict(cases[k]);\n"
            "console.log(JSON.stringify(out));\n")
        got = self.call(script, None)
        self.assertIsNone(got["zero"], got)
        for name in ("one", "two", "negative", "missing", "none", "string",
                     "nan", "fraction", "object"):
            self.assertIsInstance(got[name], str, name)
            self.assertTrue(got[name].strip(), name)
        # the refusal a user will actually see says what is at stake
        self.assertIn("monitors.xml", got["one"])
        self.assertIn("Keep changes?", got["missing"])
        # ...and what to do about a grab that is not on screen.  Measured once
        # on a real update: the first call after a post-update login refused
        # with nothing visible anywhere, and the next call seconds later
        # applied.  The check was left exactly as strict; the words that sent
        # the user looking for a dialog that was not there were not.
        self.assertIn("Main.modalCount is 1", got["one"])
        self.assertIn("Main.modalCount is 2", got["two"])
        self.assertIn("nothing on screen", got["one"])
        self.assertIn("run the same command again", got["one"])
        self.assertIn("Nothing was read and nothing was written", got["one"])

    def test_the_maps_lines_a_real_release_actually_writes(self):
        """Real lines, because the first cut of this matched none of them: a
        stable Ubuntu maps `libmutter-14.so.0.0.0`, and a pattern anchored on
        the soname reported "no build id" everywhere and looked like a library
        that would not say."""
        maps = "\n".join([
            "7bacc8200000-7bacc8250000 r--p 00000000 fd:02 534233   "
            "/usr/lib/x86_64-linux-gnu/libmutter-14.so.0.0.0",
            "7bacc8250000-7bacc8400000 r-xp 00050000 fd:02 534233   "
            "/usr/lib/x86_64-linux-gnu/libmutter-14.so.0.0.0",
            "7bacc745d000-7bacc7460000 r--p 00000000 fd:02 536272   "
            "/usr/lib/x86_64-linux-gnu/mutter-14/libmutter-cogl-pango-14.so.0.0.0",
            "7bacc7000000-7bacc7100000 r--p 00000000 fd:02 111111   "
            "/usr/lib/x86_64-linux-gnu/mutter-18/libmutter-18.so.0 (deleted)",
            "7fff00000000-7fff00021000 rw-p 00000000 00:00 0        [stack]",
            "",
        ])
        script = self.run_js(
            "console.log(JSON.stringify(R.parseMutterMappings(input)));\n")
        got = self.call(script, maps)
        self.assertEqual(got, [
            {"gen": 14, "path": "/usr/lib/x86_64-linux-gnu/libmutter-14.so.0.0.0",
             "inode": 534233, "deleted": False},
            {"gen": 18, "path": "/usr/lib/x86_64-linux-gnu/mutter-18/libmutter-18.so.0",
             "inode": 111111, "deleted": True},
        ])
        # cogl, clutter and the rest of the family are not libmutter
        self.assertEqual(self.call(script, "\n".join(maps.splitlines()[2:3])), [])

    def test_a_replaced_library_is_a_note_with_no_verdict_in_it(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(i => R.libmutterNote(i))));\n")
        got = self.call(script, [None, {}, {"replaced": False, "path": "/x"},
                                 {"replaced": True, "path": "/lib/libmutter-14.so.0"}])
        self.assertEqual(got[:3], [None, None, None])
        self.assertIn("/lib/libmutter-14.so.0", got[3])
        self.assertIn("this changes nothing now", got[3])
        # it is news, not a refusal: it asks nothing of anybody now
        for imperative in ("Answer it", "run this again", "run the same command"):
            self.assertNotIn(imperative, got[3])

    def test_le32_is_little_endian_and_takes_a_negative(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(v => R.le32(v))));\n")
        self.assertEqual(self.call(script, [0, 1, 960, 3840, -1, -960]),
                         [[0, 0, 0, 0], [1, 0, 0, 0], [192, 3, 0, 0], [0, 15, 0, 0],
                          [255, 255, 255, 255], [64, 252, 255, 255]])

    def test_the_extension_itself_is_a_module(self):
        """It cannot be run here -- it needs gi and a compositor -- but a syntax
        error in it would be discovered by a user's session refusing to start,
        which is exactly the moment this feature must not surprise anybody.

        Every import it makes is stubbed, and the rewrite fails loudly on an
        import that is not gi, not a gnome-shell resource and not the rules file
        beside it: this extension has no business importing anything else.
        """
        src = open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read()
        out, stubs = [], 0
        for line in src.splitlines(True):
            m = re.match(r"import (.+?) from '(.+?)';\s*$", line)
            if m is None:
                out.append(line)
                continue
            what, where = m.groups()
            if where == "./rules.js":
                out.append(line.replace("./rules.js", "./rules.mjs"))
                continue
            self.assertTrue(where.startswith("gi://")
                            or where.startswith("resource:///org/gnome/shell/"),
                            where)
            stubs += 1
            names = ([what[5:]] if what.startswith("* as ")
                     else re.findall(r"\w+", what) if what.startswith("{")
                     else [what])
            for name in names:
                # a class for what is extended, an object for what is called on
                out.append("class %s {}; %s.PACKAGE_VERSION = '50.1'; %s.wm = {};\n"
                           % (name, name, name))
        self.assertGreater(stubs, 3)
        stub = os.path.join(self.dir, "ext.mjs")
        with open(stub, "w") as fh:
            fh.write("".join(out))
        rc = subprocess.run(
            [NODE, "--input-type=module", "-e",
             "import('%s').then(m => console.log(typeof m.default))" % stub],
            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(rc.stdout.strip(), "function")


if __name__ == "__main__":
    unittest.main()
