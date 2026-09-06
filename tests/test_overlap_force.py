"""`--unsafe-gnome-overlap-unmeasured`, and the table it exists to be the escape
hatch from.

Two features, one file, because neither makes sense without the other: the table
is what a GNOME release is added to, and the flag is what somebody does on the
GNOME nobody has added yet.

What is held down here:

* **the flag cannot be reached by accident.**  It is not a default in `Opts`, in
  `Session` or in any method signature; no environment variable sets it; it is a
  usage error without `--unsafe-gnome-overlap`, so it can never be the thing that
  turns the feature on; and its required argument is the GNOME Shell major of
  the machine in front of you, so a command line pasted out of a forum is refused
  by number on anybody else's;
* **it is never remembered.**  A forced run neither reads nor writes the
  agreement, `--gnome-overlap-allow` refuses to be typed with it, and
  `save_consent()` refuses a forced reply even if something ever called it;
* **it forces exactly one refusal.**  Every refusal in this feature is either
  *cautious* -- "this is a build nobody here has measured" -- or *certain* --
  a symbol that is not there, an extension that is not there, a compositor that
  is not GNOME, a struct nothing describes, a read that disagrees with Mutter.
  Only the first kind is forceable, and each of the others is re-run here with
  the flag typed and still refuses;
* **the table is one table.**  The extension's `generations.json` and wxrandr's
  `GENERATIONS` are proved identical, `metadata.json` is proved derived from
  them, and nothing anywhere composes a library name out of a number -- which is
  the thing that stopped being true when mutter 51 renumbered libmutter's API
  version to the GNOME major;
* **what a refusal prints** is a golden, because a message a maintainer cannot
  act on is the same as no message.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gnome_overlap import Case, EXT_DIR, GIR_DIR, FakeOverlap
from wxrandr import cli, gnome_overlap

os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

FLAG = gnome_overlap.FLAG
FORCE = gnome_overlap.FORCE_FLAG
MOVE = ("--output", "Virtual-2", "--pos", "960x0")

TABLE_JSON = os.path.join(EXT_DIR, "generations.json")

#: A GNOME nobody in this tree has measured, standing in for the next Ubuntu.
#: The numbers are real: mutter 51's own meson.build sets
#: `libmutter_api_version = '51'`, the archive's libmutter-51-0 ships
#: /usr/lib/x86_64-linux-gnu/libmutter-51.so.0 and gir1.2-mutter-51 ships
#: Meta-51.typelib, and `gen-gir.py --from-header` on 51's
#: src/backends/meta-monitor-config-manager.h lays the struct out at 80 bytes
#: with three tail slots.  None of that makes 51 supported -- it is not in the
#: table and nothing here adds it -- it makes the *shape* of the next release
#: something these tests can be written against.
NEXT = {"shell_major": 51, "libmutter": "51", "soname": "libmutter-51.so.0",
        "meta_typelib": "51", "namespace": "FwOverlap51",
        "struct_size": 80, "tail_slots": 3,
        "measured_on": "nowhere: this record is a test fixture"}


def unmeasured(mock, shell="51.0"):
    """Point the mock extension at a GNOME nobody has measured, with the
    library and typelib names that release really carries."""
    ov = mock.overlap = FakeOverlap(shell=shell)
    ov.sonames = ["libmutter-%s.so.0" % shell.split(".")[0]]
    ov.meta_typelib = shell.split(".")[0]
    ov.libmutter = shell.split(".")[0]
    ov.instance_size = 80
    return ov


# --------------------------------------------------------------- reachability

class Reachability(Case):
    """Every way in, and the far larger number of ways that are not one."""

    def test_the_flag_alone_does_nothing_and_says_so(self):
        """It is a modifier, never an entry point.  Forgetting the dangerous
        flag and typing this one has to be a usage error, or there would be a
        command line in which this is what turns the feature on."""
        unmeasured(self.mock)
        code, out, err = self.run_cli(FORCE, "51", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("only means something together with %s" % FLAG, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])

    def test_it_needs_a_whole_gnome_major_and_not_a_version_string(self):
        for bad in ("", "51.0", "fifty-one", "-1", "51x", "٥١"):
            code, out, err = self.run_cli(FLAG, FORCE, bad, *MOVE)
            self.assertEqual(code, 1, bad)
            self.assertIn("takes the GNOME Shell major version", err)
            self.assertEqual(self.ext_calls(), [], bad)

    def test_a_missing_argument_is_a_usage_error(self):
        code, out, err = self.run_cli(FLAG, FORCE)
        self.assertEqual(code, 1)
        self.assertEqual(self.ext_calls(), [])

    def test_it_has_to_name_the_gnome_in_front_of_you(self):
        """The property a bare --force cannot have: a line copied from a forum
        names the GNOME that person had, and is refused here by number."""
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "49", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("names GNOME Shell 49; this session is GNOME Shell 51.0", err)
        self.assertIn("copied from somewhere else", err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])

    def test_it_applies_when_it_names_this_gnome(self):
        ov = unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        # the request carries the force, explicitly, on the call itself
        self.assertEqual(ov.calls[0][1]["force"], {"shell_major": 51})

    def test_a_measured_gnome_does_not_need_it_and_is_unchanged_by_it(self):
        """Typing it where it is not needed changes nothing at all: the version
        gate passes on its own, and the extension is asked the same question."""
        code, out, err = self.run_cli(FLAG, FORCE, "50", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])

    def test_a_layout_gnome_accepts_never_reaches_any_of_it(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "51", "--output", "Virtual-2",
                                      "--pos", "1920x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertNotIn(FORCE, err)


class NotADefault(Case):
    def test_nothing_defaults_to_forcing(self):
        self.assertIsNone(cli.Opts().overlap_force)
        self.assertIsNone(cli.Session.overlap_force)

    def test_no_environment_variable_turns_it_on(self):
        unmeasured(self.mock, "51.0")
        env = {"WXRANDR_UNSAFE_GNOME_OVERLAP_UNMEASURED": "51",
               "WXRANDR_OVERLAP_FORCE": "1", "WXRANDR_FORCE": "51",
               "WXRANDR_UNMEASURED": "51"}
        code, out, err = self.run_cli(FLAG, *MOVE, env=env)
        self.assertEqual(code, 1)
        self.assertIn("is not a build this has been measured on", err)
        self.assertEqual(self.applied(), [])

    def test_the_word_force_appears_in_no_environment_lookup(self):
        """Not a behaviour test: a proof from the source that there is no
        variable to find.  Every os.environ read in the two modules is named."""
        for mod in ("wxrandr/cli.py", "wxrandr/mutter.py",
                    "wxrandr/gnome_overlap.py"):
            with open(os.path.join(ROOT, mod), encoding="utf-8") as fh:
                src = fh.read()
            for m in re.finditer(r"environ(?:\.get)?\(?\[?[\"']([A-Z_]+)[\"']", src):
                self.assertNotIn("OVERLAP", m.group(1), mod)
                self.assertNotIn("FORCE", m.group(1), mod)
                self.assertNotIn("UNSAFE", m.group(1), mod)


class NeverRemembered(Case):
    """A forced run leaves nothing behind, and cannot be covered by anything
    left behind earlier."""

    def consent_file(self):
        return gnome_overlap.consent_path()

    def test_a_forced_run_records_nothing(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.consent_file()))
        self.assertIn("Nothing was recorded", err)

    def test_the_agreement_and_the_flag_cannot_be_typed_together(self):
        for argv in ((FLAG, FORCE, "51", gnome_overlap.ALLOW_FLAG),
                     (gnome_overlap.ALLOW_FLAG, FLAG, FORCE, "51")):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("cannot be used together", err)
            self.assertIn("forcing is what is done when they have not", err)

    def test_an_agreement_already_on_disk_does_not_quieten_a_forced_run(self):
        """Written by hand for exactly the build in the room, which is the only
        way such a file could exist at all -- `--gnome-overlap-allow` cannot
        produce one here.  The paragraph is printed anyway."""
        unmeasured(self.mock, "51.0")
        path = self.consent_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"format": gnome_overlap.CONSENT_FORMAT, "shell": "51.0",
                       "libmutter": "51", "struct_size": 80,
                       "agreed": "2026-01-01T00:00:00Z", "how": "by hand"}, fh)
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("forcing past the one check", err)
        self.assertIn("What may happen:", err)
        self.assertNotIn("as agreed on", err)

    def test_save_consent_refuses_a_forced_reply_on_its_own(self):
        """Belt and braces, and from the *reply* rather than the caller's word
        for it: nothing calls this on the forced path, and if anything ever did
        it would raise rather than write."""
        facts = gnome_overlap.facts(
            {"shell": "51.0", "libmutter": "51", "instance_size": 80,
             "forced": {"shell_major": 51, "using": "FwOverlap18"}})
        self.assertTrue(facts["forced"])
        with self.assertRaises(ValueError) as e:
            gnome_overlap.save_consent(facts, "a test that should not have")
        self.assertIn("forced run is the one that did not", str(e.exception))
        self.assertFalse(os.path.exists(self.consent_file()))

    def test_the_applying_path_reads_the_agreement_only_when_not_forcing(self):
        """From the source, because this is a structural claim: there is one
        read of the agreement on the applying path and it is guarded."""
        with open(os.path.join(ROOT, "wxrandr", "mutter.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def apply_overlap"):]
        body = body[:body.index("\n    def ", 1)]
        self.assertIn("rec = None if force else gnome_overlap.load_consent()", body)
        self.assertEqual(body.count("load_consent"), 1)
        self.assertNotIn("save_consent", body)


# --------------------------------------------------- which refusals are which

class WhichRefusalsAreForceable(Case):
    """The classification, run rather than asserted about.

    A *cautious* refusal is one where the tool does not know enough: this is a
    build nobody here has measured.  That is what forcing is for.  A *certain*
    refusal is one where something is missing or wrong -- and forcing it would
    be pretending the missing thing is there.
    """

    def test_the_list_is_one_entry_and_both_halves_agree_on_it(self):
        self.assertEqual(gnome_overlap.FORCEABLE, ("shell-version",))
        self.assertTrue(gnome_overlap.is_forceable("shell-version"))
        for certain in ("symbols", "struct-size", "sentinel", "pending-dialog",
                        "bounded-read", "layout-mode", "public-view", "table",
                        "meta-typelib", "libmutter", "current", "connectors",
                        "maps", "request", "read-back", "positive-control",
                        "apply", "not-an-overlap", "write", "internal", None):
            self.assertFalse(gnome_overlap.is_forceable(certain), certain)

    def test_the_extension_decides_it_and_the_tool_only_reads_the_answer(self):
        """A reply says whether its own refusal is forceable; the tool falls
        back to the same list only for an extension too old to say."""
        self.assertTrue(gnome_overlap.refusal_is_forceable(
            {"check": "sentinel", "forceable": True}))
        self.assertFalse(gnome_overlap.refusal_is_forceable(
            {"check": "shell-version", "forceable": False}))
        self.assertTrue(gnome_overlap.refusal_is_forceable(
            {"check": "shell-version"}))

    # -- certain: forcing does not get past any of these ---------------------

    def test_a_compositor_that_is_not_gnome_is_certain(self):
        for backend in ("kwin", "sway", "wlr"):
            code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE, backend=backend)
            self.assertEqual(code, 1, backend)
            self.assertIn("only means anything on GNOME", err)
            self.assertEqual(self.ext_calls(), [], backend)

    def test_a_shell_that_will_not_say_its_version_is_certain(self):
        """Forcing is somebody vouching for the build in front of them by
        naming it.  A version string nothing can read is not a build anybody
        can name, so there is nothing for the flag to agree with."""
        self.mock.overlap.shell = None
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("does not report a version this can read", err)
        self.assertEqual(self.ext_calls(), [])

    def test_an_extension_that_is_not_there_is_certain(self):
        ov = unmeasured(self.mock, "51.0")
        ov.present = False
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension is not running", err)
        self.assertEqual(self.applied(), [])

    def test_more_than_a_position_is_certain(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE, "--mode", "1280x720")
        self.assertEqual(code, 1)
        self.assertIn("changes more than where the monitors are", err)
        self.assertEqual(self.ext_calls(), [])

    def test_persistent_is_certain(self):
        code, out, err = self.run_cli(FLAG, FORCE, "51", "--persistent", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("cannot be used together", err)
        self.assertEqual(self.ext_calls(), [])

    def test_a_symbol_that_is_absent_is_certain(self):
        ov = unmeasured(self.mock, "51.0")
        ov.reply = {"ok": False, "check": "symbols", "forceable": False,
                    "reason": "FwOverlap18.create_linear is not callable"}
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (symbols)", err)
        self.assertIn("create_linear is not callable", err)
        self.assertNotIn("To add this build", err)
        self.assertEqual(self.applied(), [])

    def test_a_struct_no_description_describes_is_certain(self):
        """Forcing picks a description by size; it cannot write one.  A build
        whose MetaMonitorsConfig is a size nothing here describes has nothing
        to force with, and says so."""
        ov = unmeasured(self.mock, "51.0")
        ov.instance_size = 96
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (struct-size)", err)
        self.assertIn("96 bytes", err)
        self.assertEqual(self.applied(), [])

    def test_every_other_check_stays_refused_with_the_flag_typed(self):
        for check in ("sentinel", "pending-dialog", "bounded-read",
                      "public-view", "layout-mode", "read-back",
                      "positive-control", "table"):
            ov = unmeasured(self.mock, "51.0")
            ov.reply = {"ok": False, "check": check, "forceable": False,
                        "reason": "made to fire for this test"}
            code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
            self.assertEqual(code, 1, check)
            self.assertIn("the overlap extension refused (%s)" % check, err)
            self.assertEqual(self.applied(), [], check)

    # -- cautious: the one that forcing is for -------------------------------

    def test_the_version_gate_is_the_only_one_that_changes_answer(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("is not a build this has been measured on", err)
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 0, err)


# ------------------------------------------------------------ what it prints

class WhatForcingPrints(Case):
    def test_the_paragraph_is_printed_before_the_call_every_time(self):
        unmeasured(self.mock, "51.0")
        for _ in range(3):
            code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
            self.assertEqual(code, 0, err)
            head = err[:err.index("What it does:")]
            self.assertIn("forcing past the one check", head)
            for said in ("What is skipped:", "What is not skipped:",
                         "What may happen:", "What is recorded:",
                         "If it happens:"):
                self.assertIn(said, head)

    def test_it_says_what_is_skipped_and_what_cannot_be(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertIn("one thing: that GNOME Shell 51.0 is a build this project has",
                      err)
        self.assertIn("chosen instead by the size this build's own GType registry", err)
        for kept in ("sentinel", "modal-grab", "bounded read", "public",
                     "read-back", "positive control", "monitors.xml"):
            self.assertIn(kept, err)
        self.assertIn("none of it can be forced", err)

    def test_it_says_what_may_happen_and_what_to_do_if_it_does(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertIn("gnome-shell crashes", err)
        self.assertIn("every program running in it goes", err)
        # the way back, in full, and it is the same one the ordinary warning gives
        self.assertIn("Ctrl+Alt+F3", err)
        self.assertIn("gnome-extensions disable fuckwayland-overlap@fuckwayland", err)
        self.assertIn("~/.local/share/gnome-shell/extensions/", err)

    def test_the_ordinary_warning_is_printed_as_well(self):
        """Forcing adds a paragraph; it never replaces the one that says what
        moves, what it risks, what it saves and how to undo it."""
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        for said in ("What it does:", "What it risks:", "What it saves:",
                     "To undo:"):
            self.assertIn(said, err)
        self.assertIn("move Virtual-2 from +1920+0 to +960+0", err)

    def test_the_apply_says_which_description_it_used(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertIn("applied on an unmeasured GNOME through FwOverlap18", err)
        self.assertIn("MetaMonitorsConfig is 80 bytes here", err)

    def test_a_dryrun_prints_it_and_writes_nothing(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli("--dryrun", FLAG, FORCE, "51", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("forcing past the one check", err)
        self.assertIn("dryrun: nothing was written", err)
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertEqual(self.applied(), [])


class WhatARefusalPrints(Case):
    """The message somebody who has never seen this code has to add a
    generation from.  A golden, because the whole value of it is that every
    part is there."""

    def refusal(self):
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        return err

    def test_it_names_the_versions_found(self):
        err = self.refusal()
        self.assertIn("GNOME Shell 51.0", err)
        self.assertIn("libmutter-51.so.0", err)
        self.assertIn("Meta typelib 51", err)

    def test_it_names_the_structure_size_this_build_reports(self):
        self.assertIn("MetaMonitorsConfig 80 bytes, from this build's GType "
                      "registry", self.refusal())

    def test_it_names_what_was_expected(self):
        err = self.refusal()
        for line in gnome_overlap.describe_table():
            self.assertIn(line, err)

    def test_it_names_where_the_answer_goes_and_what_to_run(self):
        err = self.refusal()
        self.assertIn("gnome/fuckwayland-overlap@fuckwayland/generations.json", err)
        self.assertIn("wxrandr/gnome_overlap.py  (GENERATIONS)", err)
        self.assertIn("gen-gir.py --from-header", err)
        self.assertIn("meta-monitor-config-manager.h", err)
        self.assertIn('docs/Technical.md section 6, "Adding a GNOME generation"',
                      err)

    def test_it_offers_the_flag_with_this_machines_number_in_it(self):
        err = self.refusal()
        self.assertIn("%s %s 51" % (FLAG, FORCE), err)
        self.assertIn("skips this one check and no other", err)
        self.assertIn("may end this session", err)

    def test_a_forced_run_is_not_told_how_to_force(self):
        """It is already forcing.  Repeating the offer would be noise on the one
        message that has to be read."""
        ov = unmeasured(self.mock, "51.0")
        ov.reply = {"ok": False, "check": "sentinel", "forceable": False,
                    "reason": "the tail is not where it was measured"}
        code, out, err = self.run_cli(FLAG, FORCE, "51", *MOVE)
        self.assertNotIn("To try it here now", err)

    def test_with_no_extension_it_says_the_numbers_need_one(self):
        ov = unmeasured(self.mock, "51.0")
        ov.present = False
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the rest needs the extension", err)
        self.assertIn("sh gnome/install-overlap.sh", err)

    def test_the_agreement_option_gives_the_same_message(self):
        """`--gnome-overlap-allow` on a new release is exactly where a
        maintainer lands, so it is the same message and not a shorter one."""
        unmeasured(self.mock, "51.0")
        code, out, err = self.run_cli(gnome_overlap.ALLOW_FLAG)
        self.assertEqual(code, 1)
        self.assertIn("To add this build:", err)
        # ...without the offer to force: that is not what this option does
        self.assertNotIn("To try it here now", err)


# ------------------------------------------------------------------ the table

class TheTable(unittest.TestCase):
    def table(self):
        with open(TABLE_JSON, encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def read(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_two_copies_are_one_table(self):
        """The extension reads generations.json; wxrandr cannot (it is
        installed somewhere else entirely), so it carries the same records.
        They have to be identical, and this names the file to fix."""
        js = self.table()["generations"]
        py = list(gnome_overlap.GENERATIONS)
        self.assertEqual([g["shell_major"] for g in js],
                         [g["shell_major"] for g in py],
                         "generations.json and wxrandr/gnome_overlap.py list "
                         "different GNOME releases")
        for a, b in zip(js, py):
            self.assertEqual(a, b, "the GNOME %s records differ" % a["shell_major"])

    def test_every_record_is_complete(self):
        for g in self.table()["generations"]:
            for field in gnome_overlap.TABLE_FIELDS:
                self.assertIn(field, g)
                self.assertIsNotNone(g[field])

    def test_the_extension_metadata_is_derived_from_it(self):
        meta = json.loads(self.read(os.path.join(EXT_DIR, "metadata.json")))
        self.assertEqual(meta["shell-version"],
                         [str(g["shell_major"]) for g in self.table()["generations"]])

    def test_there_is_one_description_per_record_and_no_others(self):
        got = sorted(os.listdir(os.path.join(EXT_DIR, "typelib")))
        self.assertEqual(got, sorted("%s-1.0.typelib" % g["namespace"]
                                     for g in self.table()["generations"]))
        girs = sorted(n for n in os.listdir(GIR_DIR) if n.endswith(".gir"))
        self.assertEqual(girs, sorted("%s-1.0.gir" % g["namespace"]
                                      for g in self.table()["generations"]))

    def test_the_shipped_gir_names_the_soname_the_record_does(self):
        for g in self.table()["generations"]:
            text = self.read(os.path.join(GIR_DIR, "%s-1.0.gir" % g["namespace"]))
            self.assertIn('shared-library="%s"' % g["soname"], text)

    #: code that builds a *file name* or a *namespace* out of a substituted
    #: value.  Prose about the old scheme is fine and is everywhere; a format
    #: string that ends in `.so`, or a `FwOverlap` with a hole in it, is the
    #: thing mutter 51 made wrong, and there must be none left.
    COMPOSED = (re.compile(r"libmutter-(%[sd]|\$\{|\{|\" *\+|' *\+)[^\n]*\.so"),
                re.compile(r"FwOverlap(%[sd]|\$\{|\{|\" *\+|' *\+)"))

    def test_no_library_name_is_composed_out_of_a_number(self):
        """The rewrite in one assertion.  `libmutter-14` and `libmutter-18`
        appear in prose all over this tree; what must not appear anywhere is
        code that *builds* a soname or a namespace from an integer, because
        mutter 51 stopped following the arithmetic that would make it right."""
        roots = [os.path.join(ROOT, p) for p in
                 ("wxrandr", "warandr", "gnome", "fwcommon")]
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    if not name.endswith((".py", ".js", ".sh")):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    for line in text.splitlines():
                        if line.lstrip().startswith(("#", "//", "*")):
                            continue        # prose about the old scheme is fine
                        for pat in self.COMPOSED:
                            self.assertIsNone(pat.search(line),
                                              "%s composes a name: %s"
                                              % (path, line.strip()))

    def test_it_can_express_the_next_ubuntu(self):
        """GNOME 51 really does break the old scheme: mutter sets
        `libmutter_api_version = '51'`, so the library is libmutter-51.so.0 and
        the typelib is Meta-51 -- 51 rather than the 19 the 46->14, 50->18
        counter would have produced.  Adding it must be one record, with no code
        anywhere having to learn a new rule.

        This does not make GNOME 51 supported.  Nothing here writes the record
        into the shipped table; it proves the table has room for it."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir_next", os.path.join(GIR_DIR, "gen-gir.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        table = self.table()
        table["generations"] = table["generations"] + [dict(NEXT)]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "generations.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(table, fh)
            records = gen.load_table(path)
        self.assertEqual([g["shell_major"] for g in records], [46, 50, 51])
        ns, text = gen.gir(records[-1])
        self.assertEqual(ns, "FwOverlap51")
        self.assertIn('shared-library="libmutter-51.so.0"', text)
        self.assertIn('name="FwOverlap51"', text)
        self.assertEqual(gen.metadata_shell_versions(records), ["46", "50", "51"])

    def test_it_can_express_a_name_that_follows_no_scheme_at_all(self):
        """Not a prediction, a property: the next rename does not have to be one
        anybody guessed."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir_odd", os.path.join(GIR_DIR, "gen-gir.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        odd = dict(NEXT, shell_major=99, libmutter="mainline",
                   soname="libmutter-mainline.so.0", meta_typelib="mainline",
                   namespace="FwOverlapMainline")
        ns, text = gen.gir(odd)
        self.assertEqual(ns, "FwOverlapMainline")
        self.assertIn('shared-library="libmutter-mainline.so.0"', text)

    def test_a_record_missing_a_field_is_an_error_naming_the_field(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir_bad", os.path.join(GIR_DIR, "gen-gir.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        table = self.table()
        broken = dict(NEXT)
        broken.pop("soname")
        table["generations"] = [broken]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "generations.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(table, fh)
            with self.assertRaises(SystemExit) as e:
                gen.load_table(path)
        self.assertIn("soname", str(e.exception))

    def test_the_header_deriver_reads_a_whole_real_header(self):
        """`struct _MetaMonitorsConfig` is a prefix of
        `struct _MetaMonitorsConfigKey`, which mutter's real header declares
        first.  A plain `find` therefore laid out the Key struct and reported
        that the head of the config had moved -- on the very file the
        documented procedure tells a maintainer to point this at."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir_hdr", os.path.join(GIR_DIR, "gen-gir.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        header = """
typedef struct _MetaMonitorsConfigKey
{
  GList *monitor_specs;
  MetaLogicalMonitorLayoutMode layout_mode;
} MetaMonitorsConfigKey;

struct _MetaMonitorsConfig
{
  GObject parent;

  MetaMonitorsConfig *parent_config;
  MetaMonitorsConfigKey *key;
  GList *logical_monitor_configs;

  GList *disabled_monitor_specs;
  GList *for_lease_monitor_specs;

  MetaMonitorsConfigFlag flags;

  MetaLogicalMonitorLayoutMode layout_mode;

  MetaMonitorSwitchConfigType switch_config;
};
"""
        ints, rows, size = gen.build_from_header(header)
        self.assertEqual((ints, size), (3, 80))
        at = {f: off for off, _sz, _t, f in rows}
        self.assertEqual(at["logical_monitor_configs"], 40)
        self.assertEqual(at["switch_config"], 72)


class GenGirIsTheOnlyGenerator(unittest.TestCase):
    def test_the_generated_files_are_in_step_with_the_table(self):
        if shutil.which("g-ir-compiler") is None:
            self.skipTest("no g-ir-compiler")
        rc = subprocess.run([sys.executable, os.path.join(GIR_DIR, "gen-gir.py"),
                             "--check"], capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)

    def test_gen_is_gone_and_says_what_replaced_it(self):
        """`--gen 18` used to mean the libmutter generation.  The table is keyed
        by GNOME major now, and a script that takes one number when it means the
        other invites exactly the wrong answer, so the old spelling is an error
        rather than a silent reinterpretation."""
        rc = subprocess.run([sys.executable, os.path.join(GIR_DIR, "gen-gir.py"),
                             "--from-header", "/dev/null", "--gen", "18"],
                            capture_output=True, text=True)
        self.assertNotEqual(rc.returncode, 0)
        self.assertIn("--shell 50", rc.stdout + rc.stderr)


# -------------------------------------------------------------------- the GUI

class TheGuiCannotForce(unittest.TestCase):
    """warandr has no way to reach this, deliberately, and this is the test that
    keeps it that way.

    The reason is the flag's own argument.  What makes forcing defensible is
    that somebody typed the version of the GNOME in front of them, out of the
    refusal they had just read; a checkbox is a click, and a click carries none
    of that.  warandr's overlap route is also gated on
    `wxrandr --gnome-overlap-status`, which answers `unavailable` on an
    unmeasured build -- so the window reports GNOME's refusal there, as it did
    before any of this existed, and the way to overrule it is a terminal.
    """

    def test_no_spelling_of_the_flag_is_anywhere_in_warandr(self):
        for name in sorted(os.listdir(os.path.join(ROOT, "warandr"))):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(ROOT, "warandr", name),
                      encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn(FORCE, text, name)
            self.assertNotIn("unmeasured", text.lower(), name)

    def test_the_command_line_it_builds_can_never_contain_it(self):
        from warandr import randr as wrandr

        class L:
            def overlaps(self):
                return [("DP-1", "DP-2")]
        b = wrandr.Backend(["wxrandr"], True, name="mutter")
        for state in ("agreed", "available", "unavailable", None):
            b.overlap_info = None if state is None else {"state": state}
            self.assertNotIn(FORCE, b.overlap_flag(L()))
            self.assertNotIn(FORCE, b.run_word_for(L()))

    def test_its_own_unsafe_flag_waives_a_question_and_not_a_check(self):
        """`warandr --unsafe-gnome-overlap` exists and means something much
        smaller: do not put the dialog up.  It is not this flag and does not
        imply it."""
        from warandr import cli as wcli
        args = wcli._parser().parse_args(["--unsafe-gnome-overlap"])
        self.assertTrue(args.unsafe_gnome_overlap)
        self.assertFalse(hasattr(args, "unsafe_gnome_overlap_unmeasured"))


# ---------------------------------------- the same decisions, run under node

NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(NODE is None, "no node to run rules.js")
class RulesJSForce(unittest.TestCase):
    """The extension's half, run for real.

    Everything the force path decides lives in rules.js, which has no `gi`
    imports precisely so that it can be run here: the gate that compares the
    named major with the running one, the selection of a description by struct
    size, and the list of forceable checks.  Checking the Python and leaving the
    JavaScript to a code review would be checking the half that cannot kill a
    session.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="rules-force-")
        shutil.copy(os.path.join(EXT_DIR, "rules.js"),
                    os.path.join(cls.dir, "rules.mjs"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def call(self, body, arg):
        src = ("import * as R from '%s/rules.mjs';\n"
               "const input = JSON.parse(process.argv[2] || '{}');\n"
               % self.dir) + body
        path = os.path.join(self.dir, "case.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        rc = subprocess.run([NODE, path, json.dumps(arg)],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        return json.loads(rc.stdout)

    def table(self):
        with open(TABLE_JSON, encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_gate_refuses_everything_but_this_gnome(self):
        cases = [[None, "51.0"],                       # not forced at all
                 [{}, "51.0"],                         # forced with nothing named
                 [{"shell_major": "51"}, "51.0"],      # a string, not a number
                 [{"shell_major": 51.5}, "51.0"],      # not a whole number
                 [{"shell_major": 49}, "51.0"],        # somebody else's machine
                 [{"shell_major": 51}, "banana"],      # a version nothing can read
                 [{"shell_major": 51}, "51.0"]]        # the one that passes
        got = self.call("console.log(JSON.stringify(input.map("
                        "c => R.forceGate(c[0], c[1]))));\n", cases)
        self.assertEqual(got[0], "not forced")
        for i in range(1, 6):
            self.assertIsNotNone(got[i], cases[i])
        self.assertIn("names GNOME Shell 49", got[4])
        self.assertIsNone(got[6])

    def test_the_same_gate_as_the_python_side(self):
        """Two implementations of one rule, and they answer the same on every
        case: the tool refuses out here, and the extension refuses again on its
        own account, so a disagreement would be a hole."""
        cases = [[{"shell_major": 51}, "51.0"], [{"shell_major": 49}, "51.0"],
                 [{"shell_major": 51}, "51"], [{"shell_major": 51}, ""],
                 [None, "51.0"]]
        got = self.call("console.log(JSON.stringify(input.map("
                        "c => R.forceGate(c[0], c[1]))));\n", cases)
        for js, (force, shell) in zip(got, cases):
            py = gnome_overlap.force_reason(force, shell)
            self.assertEqual(js is None, py is None, (force, shell, js, py))

    def test_a_description_is_selected_by_size_and_never_guessed(self):
        table = self.table()
        got = self.call("console.log(JSON.stringify(input.sizes.map("
                        "s => R.selectByStructSize(input.table, s))));\n",
                        {"table": table, "sizes": [72, 80, 96, 0, None, -8]})
        self.assertEqual(got[0]["generation"]["namespace"], "FwOverlap14")
        self.assertEqual(got[1]["generation"]["namespace"], "FwOverlap18")
        for i in (2, 3, 4, 5):
            self.assertNotIn("generation", got[i])
            self.assertIn("refusal", got[i])
        self.assertIn("no description shipped here describes", got[2]["refusal"])
        self.assertIn("Forcing cannot invent a description", got[2]["refusal"])

    def test_two_descriptions_of_one_size_is_a_refusal_not_a_coin_toss(self):
        table = self.table()
        table["generations"] = [dict(g, struct_size=80)
                                for g in table["generations"]]
        got = self.call("console.log(JSON.stringify("
                        "R.selectByStructSize(input, 80)));\n", table)
        self.assertNotIn("generation", got)
        self.assertIn("cannot say which one", got["refusal"])

    def test_the_forceable_list_is_the_same_on_both_sides(self):
        checks = ["shell-version", "struct-size", "sentinel", "pending-dialog",
                  "bounded-read", "public-view", "symbols", "table", "libmutter"]
        got = self.call("console.log(JSON.stringify(input.map("
                        "c => R.isForceable(c))));\n", checks)
        for check, js in zip(checks, got):
            self.assertEqual(js, gnome_overlap.is_forceable(check), check)

    def test_the_soname_scan_finds_the_library_and_not_its_relatives(self):
        """The next Ubuntu's real mapping lines, and the ones beside them.
        `libmutter-clutter-51.so.0` is mapped into the same process and is not
        the library this writes to; a scan that returned both would refuse every
        session on 51 for the wrong reason."""
        maps = "\n".join([
            "7f0000000000-7f0000200000 r--p 00000000 fd:02 100001   "
            "/usr/lib/x86_64-linux-gnu/libmutter-51.so.0.0.0",
            "7f0000200000-7f0000300000 r--p 00000000 fd:02 100002   "
            "/usr/lib/x86_64-linux-gnu/mutter-51/libmutter-clutter-51.so.0.0.0",
            "7f0000300000-7f0000400000 r--p 00000000 fd:02 100003   "
            "/usr/lib/x86_64-linux-gnu/mutter-51/libmutter-cogl-51.so.0.0.0",
            "7f0000400000-7f0000500000 r--p 00000000 fd:02 100004   "
            "/usr/lib/x86_64-linux-gnu/mutter-51/libmutter-mtk-51.so.0.0.0",
            ""])
        got = self.call("console.log(JSON.stringify(R.mutterSonames(input)));\n",
                        maps)
        self.assertEqual(got, ["libmutter-51.so.0"])

    def test_the_soname_token_is_read_and_not_computed(self):
        got = self.call("console.log(JSON.stringify(input.map("
                        "s => R.sonameToken(s))));\n",
                        ["libmutter-14.so.0", "libmutter-51.so.0",
                         "libmutter-mainline.so.0", "libmutter-clutter-51.so.0",
                         "libfoo.so.0", ""])
        self.assertEqual(got, ["14", "51", "mainline", None, None, None])

    def test_the_table_is_read_and_not_restated(self):
        table = self.table()
        got = self.call("console.log(JSON.stringify({"
                        "majors: R.knownMajors(input),"
                        "lines: R.describeTable(input)}));\n", table)
        self.assertEqual(got["majors"], list(gnome_overlap.SUPPORTED_MAJORS))
        self.assertEqual(got["lines"], gnome_overlap.describe_table())


if __name__ == "__main__":
    unittest.main()
