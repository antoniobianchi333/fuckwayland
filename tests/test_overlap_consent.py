"""The agreement to `--unsafe-gnome-overlap`: where it lives, what it records,
what it silences, and -- at length -- everything it cannot do.

The feature is a paragraph that stops being printed.  That is all it is, and
most of this file exists to hold that down, because the tempting version of this
feature (a remembered yes that lets the tool skip ahead) is the one that would
eventually cost somebody their session.  So:

* an agreement is recorded only against a build the six checks have just passed
  on, and it records what they measured -- the Shell version, libmutter's
  generation and the size this build's `MetaMonitorsConfig` turned out to be;
* it stops applying the moment any of that changes, which is what a distribution
  upgrade does;
* it is never consulted for whether to check anything.  Every refusal in
  tests/test_gnome_overlap.py is re-run here *with* an agreement recorded, and
  every one of them still refuses;
* and it is not the flag: an overlapping layout with no `--unsafe-gnome-overlap`
  is still GNOME's refusal, agreement or no agreement.

The harness is tests/test_gnome_overlap.py's -- a mock DisplayConfig, a mock
org.gnome.Shell and a mock extension on a bus in this process, with
XDG_CONFIG_HOME pointed at a temporary directory, which is where the agreement
then lands.
"""

import io
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gnome_overlap import WARNING, Case, _redirect
from warandr import randr as wrandr
from wxrandr import cli, gnome_overlap
from wxrandr.core import Fatal

os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

FLAG = gnome_overlap.FLAG
ALLOW = gnome_overlap.ALLOW_FLAG
FORGET = gnome_overlap.FORGET_FLAG
STATUS = gnome_overlap.STATUS_FLAG

#: the overlapping move every test in here applies: Virtual-2 from +1920+0 to
#: +960+0, half on top of Virtual-1
MOVE = ("--output", "Virtual-2", "--pos", "960x0")


class ConsentCase(Case):
    """A `Case` with the agreement file in reach."""

    def path(self):
        return gnome_overlap.consent_path()

    def record(self, **over):
        """Write an agreement by hand -- for the builds `--gnome-overlap-allow`
        would refuse to record one for, which is where the interesting questions
        are."""
        rec = {"format": gnome_overlap.CONSENT_FORMAT, "shell": "50.1",
               "libmutter": 18, "struct_size": 80,
               "agreed": "2026-01-02T03:04:05Z", "how": "by hand"}
        rec.update(over)
        os.makedirs(os.path.dirname(self.path()), exist_ok=True)
        with open(self.path(), "w") as fh:
            fh.write(json.dumps(rec))
        return rec

    def agree(self):
        """The supported way: run the command and check it worked."""
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 0, err)
        return out

    def stderr_lines(self, err):
        return [ln for ln in err.splitlines() if ln.strip()]

    def run_cli_no_session(self, *argv, how=None):
        """`cli.main` with a Session that cannot be built at all: a text console,
        or a session that will not start.

        `how` is which of the two ways that happens: `Fatal` for a backend that
        was named and is not there, and by default the real `_cant_open()`, which
        is what a machine with no compositor does -- it writes xrandr's own line
        to stderr and raises SystemExit, and answering anyway is the whole point
        of this command."""
        def boom(_sess, forced=None):
            if how is Fatal:
                raise Fatal("Can't open display\n")
            cli.Session._cant_open()
        orig = cli.Session.__init__
        cli.Session.__init__ = boom
        out, err = io.StringIO(), io.StringIO()
        try:
            with _redirect(out, err):
                code = cli.main(list(argv))
        finally:
            cli.Session.__init__ = orig
        return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------- the recording

class Recording(ConsentCase):
    def test_it_lands_where_xdg_says_and_nowhere_else(self):
        self.assertEqual(self.path(),
                         os.path.join(self.tmp, "fuckwayland", "overlap-consent.json"))
        self.agree()
        self.assertTrue(os.path.exists(self.path()))

    def test_the_default_is_under_dot_config(self):
        self.assertEqual(gnome_overlap.consent_path({"HOME": "/home/u"}),
                         "/home/u/.config/fuckwayland/overlap-consent.json")
        # the spec's rule, the same one monitors_xml.default_path follows
        self.assertEqual(gnome_overlap.consent_path({"XDG_CONFIG_HOME": "rel",
                                                     "HOME": "/home/u"}),
                         "/home/u/.config/fuckwayland/overlap-consent.json")

    def test_what_it_records_is_what_the_checks_measured(self):
        """Not a constant in this tree: the three numbers come out of the
        extension's answer, which got them out of the running compositor."""
        self.mock.overlap.shell = "46.0"
        self.mock.overlap.libmutter = 14
        self.mock.overlap.instance_size = 72
        self.mock.overlap.declared_size = 72
        self.agree()
        with open(self.path()) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["shell"], "46.0")
        self.assertEqual(rec["libmutter"], 14)
        self.assertEqual(rec["struct_size"], 72)
        self.assertEqual(rec["format"], gnome_overlap.CONSENT_FORMAT)
        self.assertEqual(rec["how"], "wxrandr " + ALLOW)
        self.assertRegex(rec["agreed"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_the_library_it_agrees_to_is_the_one_the_checks_ran_against(self):
        """Four facts, not three: the version string cannot see a libmutter
        swapped under it, and Ubuntu swaps one inside a stable release."""
        self.mock.overlap.build_id = "c0ffee" + "0" * 34
        out = self.agree()
        with open(self.path()) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["libmutter_build"], "c0ffee" + "0" * 34)
        # and it is in the words the user is shown before it is written
        self.assertIn("build c0ffee000000", out)

    def test_a_library_that_will_not_say_its_build_is_still_agreeable(self):
        """The build id is read out of an ELF note.  A library without one is
        odd, not dangerous, and this feature does not refuse on odd."""
        self.mock.overlap.build_id = None
        out = self.agree()
        with open(self.path()) as fh:
            rec = json.load(fh)
        self.assertIsNone(rec["libmutter_build"])
        self.assertIn("libmutter-18,", out)

    def test_the_size_is_read_back_from_the_check_when_the_field_is_missing(self):
        """An extension from before the field existed still reports the size in
        the check that measured it, and that is a number this may record."""
        self.mock.overlap.instance_size = None      # drop the field
        self.agree()
        with open(self.path()) as fh:
            self.assertEqual(json.load(fh)["struct_size"], 80)

    def test_allow_runs_every_check_and_writes_nothing_to_the_compositor(self):
        self.agree()
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertEqual(self.applied(), [])

    def test_allow_says_what_is_being_agreed_to(self):
        out = self.agree()
        # the six checks, named
        for name in ("shell-version", "typelib", "sentinel", "pending-dialog",
                     "bounded-read", "public-view"):
            self.assertIn("check %s: " % name, out)
        self.assertIn("Agreeing to --unsafe-gnome-overlap on GNOME Shell 50.1 "
                      "(libmutter-18 build 0f3a1b2c3d4e, MetaMonitorsConfig 80 "
                      "bytes).", out)
        self.assertIn("What it risks:", out)
        self.assertIn("gnome-shell is the\n                        session", out)
        self.assertIn("What is agreed:", out)
        self.assertIn("this build and no other", out)
        self.assertIn("What is not agreed:", out)
        self.assertIn("Every check above runs again on", out)
        self.assertIn("To withdraw:          wxrandr --gnome-overlap-forget", out)
        self.assertIn("If the session dies:", out)
        self.assertIn("recorded in %s" % self.path(), out)

    def test_nothing_is_recorded_on_an_unmeasured_shell(self):
        self.mock.overlap.shell = "48.3"
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("not a build this has been measured on", err)
        self.assertFalse(os.path.exists(self.path()))
        self.assertEqual(self.ext_calls(), [])

    def test_nothing_is_recorded_when_the_extension_is_not_running(self):
        self.mock.overlap.present = False
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("sh gnome/install-overlap.sh", err)
        self.assertFalse(os.path.exists(self.path()))

    def test_nothing_is_recorded_when_a_check_refuses(self):
        self.mock.overlap.reply = {"ok": False, "check": "struct-size",
                                   "reason": "this build's MetaMonitorsConfig is 88 bytes"}
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (struct-size)", err)
        self.assertFalse(os.path.exists(self.path()))

    def test_nothing_is_recorded_without_a_size_to_record(self):
        """A reply with no measurable struct size is not something to agree to:
        there would be nothing for a later run to compare against."""
        self.mock.overlap.instance_size = None
        self.mock.overlap.strip_typelib_check = True
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("did not say which build it verified", err)
        self.assertFalse(os.path.exists(self.path()))

    def test_there_is_nothing_to_agree_to_off_gnome(self):
        for backend in ("kwin", "sway", "wlr"):
            code, out, err = self.run_cli(ALLOW, backend=backend)
            self.assertEqual(code, 1, backend)
            self.assertIn("there is nothing to agree to", err)
            self.assertFalse(os.path.exists(self.path()))

    def test_a_file_that_is_not_an_agreement_is_not_one(self):
        for bad in ("", "{", "[]", '{"shell": "50.1"}',
                    '{"format": 99, "shell": "50.1"}',
                    '{"format": 1}'):
            os.makedirs(os.path.dirname(self.path()), exist_ok=True)
            with open(self.path(), "w") as fh:
                fh.write(bad)
            self.assertIsNone(gnome_overlap.load_consent(), bad)
            # ...and the tool is therefore loud, which is the safe direction
            code, out, err = self.run_cli(FLAG, *MOVE)
            self.assertEqual(code, 0, err)
            self.assertTrue(err.startswith(WARNING), bad)


# --------------------------------------------------------------- withdrawing

class Withdrawing(ConsentCase):
    def test_forget_removes_it_and_says_so(self):
        self.agree()
        code, out, err = self.run_cli(FORGET)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "withdrawn: %s removed\n" % self.path())
        self.assertFalse(os.path.exists(self.path()))

    def test_forget_with_nothing_recorded_is_not_an_error(self):
        code, out, err = self.run_cli(FORGET)
        self.assertEqual(code, 0, err)
        self.assertIn("nothing to withdraw", out)

    def test_forget_opens_no_session_at_all(self):
        """The moment somebody most needs this is from a text console, with a
        session that will not start.  So it must not need one: Session is made
        to explode, and the withdrawal still happens."""
        self.agree()

        def boom(sess, forced=None):
            raise AssertionError("--gnome-overlap-forget built a Session")
        orig = cli.Session.__init__
        cli.Session.__init__ = boom
        try:
            code, out, err = self.run_cli(FORGET)
        finally:
            cli.Session.__init__ = orig
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.path()))

    def test_the_warning_is_back_after_a_withdrawal(self):
        self.agree()
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(len(self.stderr_lines(err)), 1, err)
        self.run_cli(FORGET)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertTrue(err.startswith(WARNING), err[:300])


# ----------------------------------------------------- the build it is scoped to

class ADifferentBuild(ConsentCase):
    def test_a_changed_shell_version_asks_again(self):
        self.record(shell="50.1")
        self.mock.overlap.shell = "50.2"
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        # the whole paragraph, for the build that was not agreed to
        self.assertTrue(err.startswith(WARNING.split("What it does:")[0]), err[:300])
        self.assertIn("(GNOME Shell 50.2)", err)
        for part in ("What it risks:", "What it saves:", "To undo:",
                     "If the session dies:"):
            self.assertIn(part, err)
        self.assertNotIn("as agreed on", err)

    def test_consent_covers_names_both_builds(self):
        ok, why = gnome_overlap.consent_covers({"shell": "46.0"}, "50.1")
        self.assertFalse(ok)
        self.assertIn("given on GNOME Shell 46.0", why)
        self.assertIn("this session is GNOME Shell 50.1", why)
        self.assertEqual(gnome_overlap.consent_covers({"shell": "50.1"}, "50.1"),
                         (True, None))
        self.assertFalse(gnome_overlap.consent_covers(None, "50.1")[0])

    def test_a_moved_struct_withdraws_the_agreement_after_the_apply(self):
        """The version string alone cannot see this: a build that kept its name
        and moved its private layout.  The extension's own struct-size check
        makes it a refusal rather than a bad write, so what is left to do here is
        bookkeeping -- and the bookkeeping is to stop being quiet."""
        self.record(shell="50.1", struct_size=80)
        self.mock.overlap.instance_size = self.mock.overlap.declared_size = 96
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("applying a layout GNOME refuses", err)
        self.assertIn("not the one that was agreed to", err)
        self.assertIn("MetaMonitorsConfig size 96, not 80", err)
        self.assertIn("the next run will ask again", err)
        self.assertFalse(os.path.exists(self.path()))
        # ...and it does
        self.mock.overlap.instance_size = self.mock.overlap.declared_size = 96
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertTrue(err.startswith(WARNING), err[:300])

    def test_a_new_libmutter_under_the_same_shell_withdraws_it(self):
        """The update a user actually receives.

        `ShellVersion` cannot see this: Ubuntu 24.04 ships mutter 46.2 under
        GNOME Shell 46.0, and 46.0 -> 46.2 under one unchanged shell version was
        measured applying with all six checks green.  Nothing about the layout
        moved -- four libmutter builds on each release, one layout -- so this is
        not a danger signal and does not refuse anything.  What it is is the end
        of what was agreed to, and the agreement text promises exactly this:
        "stops applying the moment any of that changes"."""
        self.record(shell="50.1", libmutter=18, libmutter_build="a" * 40)
        self.mock.overlap.build_id = "b" * 40
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("libmutter build bbbbbbbbbbbb, not aaaaaaaaaaaa", err)
        self.assertIn("the next run will ask again", err)
        self.assertFalse(os.path.exists(self.path()))
        # and the next run does ask, in full
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertTrue(err.startswith(WARNING), err[:300])

    def test_the_same_library_is_not_a_difference(self):
        self.record(shell="50.1", libmutter=18,
                    libmutter_build=self.mock.overlap.build_id)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertNotIn("not the one that was agreed to", err)
        self.assertTrue(os.path.exists(self.path()))

    def test_an_agreement_from_before_the_build_was_recorded_still_stands(self):
        """A file written by an older wxrandr names no build.  That is not a
        difference, because it is not a disagreement: only two things that both
        say something can differ."""
        self.record(shell="50.1", libmutter=18, struct_size=80)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertNotIn("not the one that was agreed to", err)
        self.assertTrue(os.path.exists(self.path()))

    def test_a_moved_generation_withdraws_it_too(self):
        self.record(shell="50.1", libmutter=18)
        self.mock.overlap.libmutter = 19
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("libmutter generation 19, not 18", err)
        self.assertFalse(os.path.exists(self.path()))

    def test_drift_is_silent_when_the_build_is_the_one_agreed_to(self):
        f = {"shell": "50.1", "libmutter": 18, "struct_size": 80}
        self.assertIsNone(gnome_overlap.consent_drift(dict(f), f))
        self.assertIsNone(gnome_overlap.consent_drift(None, f))
        # a fact the reply did not carry is not a difference
        self.assertIsNone(gnome_overlap.consent_drift(
            dict(f), {"shell": "50.1", "libmutter": None, "struct_size": None}))


# ------------------------------------- what no agreement can do: skip a check

class NoAgreementSkipsAnything(ConsentCase):
    """Every refusal in tests/test_gnome_overlap.py, re-run with an agreement
    recorded for exactly the build in the room."""

    def setUp(self):
        super().setUp()
        self.record(shell="50.1", libmutter=18, struct_size=80)

    def test_an_unmeasured_shell_is_still_refused(self):
        # the agreement even names that shell: it is still refused
        self.record(shell="48.3", libmutter=18, struct_size=80)
        self.mock.overlap.shell = "48.3"
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("not a build this has been measured on", err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])

    def test_a_shell_that_will_not_say_its_version_is_still_refused(self):
        self.mock.overlap.shell = None
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("cannot tell which GNOME Shell this is", err)
        self.assertEqual(self.ext_calls(), [])

    def test_a_missing_extension_is_still_refused(self):
        self.mock.overlap.present = False
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("sh gnome/install-overlap.sh", err)
        self.assertEqual(self.applied(), [])

    def test_more_than_a_position_is_still_refused(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0", "--mode", "1280x720")
        self.assertEqual(code, 1)
        self.assertIn("changes more than where the monitors are", err)
        self.assertEqual(self.ext_calls(), [])

    def test_a_refusal_from_the_extension_is_still_a_refusal(self):
        self.mock.overlap.reply = {"ok": False, "check": "public-view",
                                   "reason": "monitor 0: x reads 4919, Mutter says 0"}
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (public-view)", err)
        self.assertIn("4919", err)
        self.assertEqual(self.applied(), [])

    def test_the_checks_still_run_inside_the_shell(self):
        """The point of the whole design: an agreed apply is the same call, with
        the same checks behind it, as an unagreed one."""
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.run_cli(FORGET)
        before = len(self.mock.overlap.calls)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual([c[0] for c in self.mock.overlap.calls[before:]],
                         ["ApplyOverlap"])

    def test_persistent_is_still_refused(self):
        code, out, err = self.run_cli(FLAG, "--persistent", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("cannot be used together", err)

    def test_it_is_still_nothing_off_gnome(self):
        code, out, err = self.run_cli(FLAG, *MOVE, backend="kwin")
        self.assertEqual(code, 1)
        self.assertIn("only means anything on GNOME", err)

    def test_an_agreement_is_not_the_flag(self):
        """The loudest one.  An overlapping layout with no --unsafe-gnome-overlap
        is GNOME's refusal, however many times the user has agreed to anything."""
        code, out, err = self.run_cli(*MOVE)
        self.assertEqual(code, 1)
        self.assertIn("GNOME's Mutter refused this layout", err)
        # it went to DisplayConfig, which said no; the extension was never asked
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual([c[1] for c in self.applied()], [1])

    def test_an_agreed_layout_gnome_accepts_never_reaches_the_extension(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "1920x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertNotIn("agreed", err)

    def test_the_agreement_is_read_after_the_last_refusal_and_used_only_to_print(self):
        """A source-level fence.  Everything that can refuse an apply happens
        before the agreement is even read, and the value it produces reaches
        nothing but the two functions that write to stderr."""
        src = open(os.path.join(ROOT, "wxrandr", "mutter.py"), encoding="utf-8").read()
        body = src[src.index("    def apply_overlap(self"):]
        body = body[:body.index("\n    def ", 10)]
        read = body.index("load_consent()")
        # every guard in the apply path is above it
        for guard in ("route = self.overlap_route(state, targets)",
                      "if route is None:", "self._overlap_client(plan)"):
            self.assertLess(body.index(guard), read, guard)
        # ...and the extension's own answer is still what decides, below it
        self.assertLess(read, body.index('if not reply.get("ok"):'))
        # `quiet` reaches warn(), warn_bare() and applied_text(quiet=...), and
        # nothing else at all
        uses = [ln.strip() for ln in body.splitlines()
                if re.search(r"\bquiet\b", ln) and not ln.lstrip().startswith("#")]
        self.assertTrue(uses)
        for line in uses:
            self.assertTrue(line == "if quiet:"
                            or line.startswith("quiet, _why = gnome_overlap.consent_covers(")
                            or "warn(" in line or "warn_bare(" in line
                            or "applied_text(" in line, line)


# ------------------------------------------------------------- the quiet path

class Quiet(ConsentCase):
    def setUp(self):
        super().setUp()
        self.record(shell="50.1", libmutter=18, struct_size=80)

    def test_an_agreed_apply_says_one_line(self):
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            err,
            'xrandr: --unsafe-gnome-overlap: applying a layout GNOME refuses '
            '("logical monitors not adjacent (an overlap counts, and so does a '
            'gap)"), as agreed on 2026-01-02\n')
        # and it really applied, through the extension
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.assertEqual(self.applied(), [])

    def test_none_of_the_paragraph_survives(self):
        code, out, err = self.run_cli(FLAG, *MOVE)
        for gone in ("What it does:", "What it risks:", "To undo:",
                     "If the session dies:", "GNOME's rule this breaks:",
                     "mutter's own validator on the result:",
                     "monitors.xml: unchanged"):
            self.assertNotIn(gone, err)

    def test_a_saved_file_that_moved_is_still_shouted_about(self):
        """What survives `quiet` is the one line that is not reassurance."""
        ov = self.mock.overlap
        base = ov.answer

        def moved(member, req):
            out = base(member, req)
            if member == "ApplyOverlap":
                out["saved_config"] = {"path": "/home/u/.config/monitors.xml",
                                       "before": "absent", "after": "d41d8c",
                                       "unchanged": False}
            return out
        ov.answer = moved
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("CHANGED across this call", err)
        self.assertIn("please report it", err)

    def test_the_dryrun_is_never_quiet(self):
        """--dryrun exists to be read; it is what somebody runs to find out what
        would happen, so it says all of it whatever is recorded."""
        code, out, err = self.run_cli(FLAG, "--dryrun", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertTrue(err.startswith(WARNING), err[:300])
        self.assertIn("dryrun: nothing was written", err)
        self.assertEqual(self.ext_calls(), ["Probe"])

    def test_the_quiet_line_names_the_rule_and_the_day(self):
        line = gnome_overlap.quiet_line({"agreed": "2026-01-02T03:04:05Z"},
                                        "logical monitors overlap")
        self.assertEqual(line, '--unsafe-gnome-overlap: applying a layout GNOME '
                               'refuses ("logical monitors overlap"), as agreed '
                               'on 2026-01-02\n')
        self.assertLess(len(line), 120)


# ------------------------------------------------------------------ the status

class Status(ConsentCase):
    def lines(self, *argv, **kw):
        code, out, err = self.run_cli(STATUS, *argv, **kw)
        self.assertEqual(code, 0, err)
        return out.splitlines()

    def test_available_when_the_route_is_there_and_nothing_is_agreed(self):
        lines = self.lines()
        self.assertEqual(lines[0], "available")
        self.assertIn("shell: 50.1", lines)
        self.assertIn("extension: running", lines)
        self.assertIn("asks first: nothing is recorded", lines)
        self.assertIn("file: %s" % self.path(), lines)

    def test_agreed_once_it_is(self):
        self.agree()
        lines = self.lines()
        self.assertEqual(lines[0], "agreed")
        self.assertIn("agreed for: GNOME Shell 50.1 (libmutter-18 build "
                      "0f3a1b2c3d4e, MetaMonitorsConfig 80 bytes)", lines)
        self.assertIn("agreed by: wxrandr --gnome-overlap-allow", lines)

    def test_available_again_when_the_agreement_is_for_another_build(self):
        self.record(shell="46.0")
        lines = self.lines()
        self.assertEqual(lines[0], "available")
        self.assertTrue(any("asks first:" in ln and "46.0" in ln for ln in lines), lines)

    def test_unavailable_on_an_unmeasured_shell(self):
        self.mock.overlap.shell = "48.3"
        lines = self.lines()
        self.assertEqual(lines[0], "unavailable")
        self.assertTrue(any("not a build this has been measured on" in ln
                            for ln in lines), lines)

    def test_unavailable_with_the_extension_absent(self):
        self.mock.overlap.present = False
        lines = self.lines()
        self.assertEqual(lines[0], "unavailable")
        self.assertTrue(any("install-overlap.sh" in ln for ln in lines), lines)

    def test_unavailable_off_gnome(self):
        for backend in ("kwin", "sway", "wlr"):
            lines = self.lines(backend=backend)
            self.assertEqual(lines[0], "unavailable")
            self.assertTrue(any(backend in ln for ln in lines), lines)

    def test_it_answers_with_no_session_at_all(self):
        """A status query has to be answerable from a text console with a
        session that will not start: the record is a file, and half of what this
        reports is in it.

        Both ways a session fails to build, because they are different
        exceptions: a named backend that is not there is a `Fatal`, and no
        compositor at all goes through `_cant_open()`, which is xrandr's own
        "Can't open display" and a `SystemExit`.  The second one is the text
        console this command is for, and it used to walk straight past the
        answer."""
        self.agree()
        for how in (Fatal, None):
            code, out, err = self.run_cli_no_session(STATUS, how=how)
            self.assertEqual(code, 0, err)
            lines = out.splitlines()
            self.assertEqual(lines[0], "unavailable", how)
            self.assertIn("reason: Can't open display", lines)
            self.assertIn("agreed by: wxrandr --gnome-overlap-allow", lines)
            self.assertIn("file: %s" % self.path(), lines)
            # and the stray line does not land on top of the answer
            self.assertNotIn("Can't open display", err)

    def test_it_reads_nothing_out_of_gnome_shell(self):
        """A GUI runs this at startup.  It must not cost a walk of Mutter's
        private structures to answer 'would this work?'."""
        self.agree()
        before = len(self.mock.overlap.calls)
        self.lines()
        self.assertEqual(self.mock.overlap.calls[before:], [])
        self.assertEqual(self.applied(), [])


# ------------------------------------------------------------------- warandr

class WarandrSide(unittest.TestCase):
    """The GUI's half: it never imports any of the above -- it runs wxrandr and
    reads the first line of `--gnome-overlap-status`."""

    def backend(self, name="mutter", state=None, wayland=True):
        b = wrandr.Backend(["wxrandr"], wayland, env={}, name=name)
        if state is not None:
            b.overlap_info = {"state": state}
        return b

    def layout(self, overlapping):
        class L:
            def overlaps(self):
                return [("DP-1", "DP-2")] if overlapping else []
        return L()

    def test_the_flag_is_added_only_for_an_overlap_on_gnome_with_a_route(self):
        for name, state, over, want in (
                ("mutter", "agreed", True, ["--unsafe-gnome-overlap"]),
                ("mutter", "available", True, ["--unsafe-gnome-overlap"]),
                ("mutter", "unavailable", True, []),
                ("mutter", None, True, []),
                ("mutter", "agreed", False, []),
                ("kwin", "agreed", True, []),
                ("x11", None, True, []),
                ("sway", None, True, [])):
            b = self.backend(name, state, wayland=name != "x11")
            self.assertEqual(b.overlap_flag(self.layout(over)), want,
                             (name, state, over))
        self.assertEqual(self.backend("mutter", "agreed").overlap_flag(None), [])

    def test_asking_stops_the_moment_it_is_agreed(self):
        self.assertTrue(self.backend("mutter", "available")
                        .overlap_needs_asking(self.layout(True)))
        self.assertFalse(self.backend("mutter", "agreed")
                         .overlap_needs_asking(self.layout(True)))
        self.assertFalse(self.backend("mutter", "available")
                         .overlap_needs_asking(self.layout(False)))
        self.assertFalse(self.backend("kwin", "available")
                         .overlap_needs_asking(self.layout(True)))

    def test_the_command_the_window_shows_is_the_command_it_runs(self):
        b = self.backend("mutter", "available")
        b.forced = "mutter"
        self.assertEqual(b.run_word_for(self.layout(True)),
                         "wxrandr --backend mutter --unsafe-gnome-overlap")
        self.assertEqual(b.run_word_for(self.layout(False)),
                         "wxrandr --backend mutter")

    def test_gnome_stops_refusing_an_overlap_once_there_is_a_route(self):
        self.assertIsNotNone(self.backend("mutter").overlap_refusal())
        self.assertIsNotNone(self.backend("mutter", "unavailable").overlap_refusal())
        for state in ("available", "agreed"):
            b = self.backend("mutter", state)
            self.assertIsNone(b.overlap_refusal(), state)
            self.assertIn("fuckwayland-overlap extension", b.overlap_note())
            self.assertIn("gone at the next login", b.overlap_note())

    def test_one_sentence_for_both_states(self):
        """It is also the comment a saved script carries; what a script says
        about a layout must not depend on who saved it."""
        self.assertEqual(self.backend("mutter", "available").overlap_note(),
                         self.backend("mutter", "agreed").overlap_note())

    def test_the_status_is_parsed_into_the_token_and_its_lines(self):
        b = self.backend("mutter")
        b.run = lambda args, timeout=30: (
            0, "agreed\nshell: 50.1\nagreed on: 2026-01-02T03:04:05Z\n", "")
        info = b.read_overlap_status()
        self.assertEqual(info["state"], "agreed")
        self.assertEqual(info["shell"], "50.1")
        self.assertEqual(info["agreed on"], "2026-01-02T03:04:05Z")
        self.assertIn("agreed on 2026-01-02T03:04:05Z", "\n".join(b.info_lines()))

    def test_a_wxrandr_too_old_for_the_option_is_simply_no_route(self):
        b = self.backend("mutter")
        b.run = lambda args, timeout=30: (1, "", "xrandr: unrecognized option\n")
        self.assertEqual(b.read_overlap_status()["state"], "unavailable")
        b.run = lambda args, timeout=30: (0, "some other tool's output\n", "")
        self.assertEqual(b.read_overlap_status()["state"], "unavailable")

    def test_a_backend_that_cannot_be_run_is_no_route_and_no_traceback(self):
        b = self.backend("mutter")

        def boom(args, timeout=30):
            raise wrandr.RandrError("cannot run wxrandr")
        b.run = boom
        self.assertEqual(b.read_overlap_status()["state"], "unavailable")

    def test_nothing_but_gnome_is_even_asked(self):
        for name in ("kwin", "sway", "wlr", "x11"):
            b = self.backend(name, wayland=name != "x11")
            b.run = lambda args, timeout=30: self.fail("%s was asked" % name)
            self.assertIsNone(b.read_overlap_status())

    def test_warandr_does_not_import_any_of_this(self):
        """warandr runs wxrandr; it never reaches into it.  The agreement is
        wxrandr's file, read and written by wxrandr, and warandr only ever sees
        the first line of a status command."""
        for name in sorted(os.listdir(os.path.join(ROOT, "warandr"))):
            if not name.endswith(".py"):
                continue
            src = open(os.path.join(ROOT, "warandr", name), encoding="utf-8").read()
            self.assertNotIn("gnome_overlap", src, name)
            self.assertNotIn("import wxrandr", src, name)


if __name__ == "__main__":
    unittest.main()
