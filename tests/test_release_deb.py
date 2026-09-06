#!/usr/bin/env python3
"""What actually ships: the package committed in `release/`, and what the documents say about it.

Every other file in this suite runs the *tree*.  What a user runs is
`sudo apt install ./release/fuckwayland_<version>_all.deb`, which is a binary in the
repository -- and nothing in the tree makes that binary agree with the tree.  At 0.4
HEAD it did not agree: the committed file was the build of the v0.3 tag, put there by
"Release version 0.3" and never rebuilt, so a user who followed the README got none of
the release's changes.  Measured on both default GNOME images, from the shipped
package: the input daemon still outlived a removed socket and then held the lock that
stops the next one starting, a chord the layout cannot produce was still pressed at its
US position in silence, the layout notice still reached only the first command, and the
overlap extension was not in the package at all -- while README.md said "the package
carries a second, separate extension".

So: the payload is compared with the tree, file by file.  It is the only test here that
can fail for something nobody typed.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fwcommon import VERSION

RELEASE = os.path.join(ROOT, "release")
DIST = "usr/lib/python3/dist-packages"
OVERLAP_UUID = "fuckwayland-overlap@fuckwayland"
BRIDGE_UUID = "fuckwayland-bridge@fuckwayland"


def documents():
    """Every markdown file in the repository, as path -> text."""
    out = {}
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", "node_modules")]
        for n in names:
            if n.endswith(".md"):
                path = os.path.join(base, n)
                with open(path, encoding="utf-8", errors="replace") as f:
                    out[os.path.relpath(path, ROOT)] = f.read()
    return out


class ThePackageInTheTree(unittest.TestCase):
    """release/ holds exactly one .deb, it is this version, and its payload is
    this tree."""

    @classmethod
    def setUpClass(cls):
        cls.debs = sorted(n for n in os.listdir(RELEASE) if n.endswith(".deb"))

    def deb(self):
        self.assertEqual(len(self.debs), 1, self.debs)
        return os.path.join(RELEASE, self.debs[0])

    def unpacked(self):
        """The package's payload, extracted once per test that wants it."""
        if shutil.which("dpkg-deb") is None:
            self.skipTest("no dpkg-deb")     # the data member is .zst here
        tmp = tempfile.mkdtemp(prefix="fw-deb-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        subprocess.run(["dpkg-deb", "-x", self.deb(), tmp], check=True)
        return tmp

    def test_exactly_one_package_named_for_this_version(self):
        self.assertEqual(self.debs, ["fuckwayland_%s_all.deb" % VERSION])

    def test_its_own_control_says_the_same_version(self):
        if shutil.which("dpkg-deb") is None:
            self.skipTest("no dpkg-deb")
        out = subprocess.run(["dpkg-deb", "-f", self.deb(), "Version"],
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), VERSION)

    def test_every_module_in_it_is_the_one_in_the_tree(self):
        """The finding itself.  A stale binary is invisible from inside the
        tree: every test passes, every document is right, and the thing people
        install is a previous release."""
        tmp = self.unpacked()
        dist = os.path.join(tmp, DIST)
        self.assertTrue(os.path.isdir(dist), dist)
        seen = 0
        for base, dirs, names in os.walk(dist):
            dirs[:] = [d for d in dirs
                       if d != "__pycache__" and not d.endswith(".dist-info")]
            for n in names:
                if not n.endswith(".py"):
                    continue
                packaged = os.path.join(base, n)
                rel = os.path.relpath(packaged, dist)
                mine = os.path.join(ROOT, rel)
                self.assertTrue(os.path.exists(mine),
                                "%s is in the package and not in the tree" % rel)
                with open(packaged, "rb") as f:
                    a = f.read()
                with open(mine, "rb") as f:
                    b = f.read()
                self.assertEqual(a, b, "%s in the package is not the one in "
                                       "the tree: rebuild it with "
                                       "`sh scripts/build-deb.sh`" % rel)
                seen += 1
        self.assertGreater(seen, 50, seen)

    def test_it_carries_both_extensions_the_udev_rule_and_the_menu_entry(self):
        tmp = self.unpacked()
        for rel in (
                "usr/lib/udev/rules.d/60-fuckwayland-uinput.rules",
                "usr/share/applications/warandr.desktop",
                "usr/share/gnome-shell/extensions/%s/extension.js" % BRIDGE_UUID,
                "usr/share/gnome-shell/extensions/%s/extension.js" % OVERLAP_UUID,
                "usr/share/gnome-shell/extensions/%s/typelib/"
                "FwOverlap18-1.0.typelib" % OVERLAP_UUID):
            self.assertTrue(os.path.exists(os.path.join(tmp, rel)), rel)

    def test_the_shipped_extensions_are_the_ones_in_gnome(self):
        tmp = self.unpacked()
        for uuid in (BRIDGE_UUID, OVERLAP_UUID):
            src = os.path.join(ROOT, "gnome", uuid)
            packaged = os.path.join(tmp, "usr/share/gnome-shell/extensions", uuid)
            for base, _dirs, names in os.walk(packaged):
                for n in names:
                    mine = os.path.join(src, os.path.relpath(
                        os.path.join(base, n), packaged))
                    self.assertTrue(os.path.exists(mine), mine)
                    with open(os.path.join(base, n), "rb") as f:
                        a = f.read()
                    with open(mine, "rb") as f:
                        b = f.read()
                    self.assertEqual(a, b, mine)

    def test_the_readme_installs_the_file_that_is_there(self):
        text = documents()["README.md"]
        named = set(re.findall(r"release/(fuckwayland_[0-9.]+_all\.deb)", text))
        self.assertEqual(named, set(self.debs), "README.md names a package "
                                                "that is not in release/")


class TheDocumentsAboutIt(unittest.TestCase):
    """Three claims the 0.4 retest measured false.  All three are cheap to
    check against a fact rather than against prose."""

    def test_no_document_says_the_package_leaves_the_overlap_extension_out(self):
        """`debian/fuckwayland.install` decides this, and since 0.4 it lists
        the overlap extension -- while gnome/README.md, docs/Technical.md and
        the installer's own header still said "not in the .deb", the last of
        them four lines under "since 0.4 it is packaged"."""
        with open(os.path.join(ROOT, "debian", "fuckwayland.install"),
                  encoding="utf-8") as f:
            shipped = OVERLAP_UUID in f.read()
        self.assertTrue(shipped, "debian/fuckwayland.install no longer ships "
                                 "the overlap extension: fix this test's "
                                 "premise, not the documents")
        claims = ("not in the .deb", "not installed by the .deb",
                  "not part of the .deb", "is not in the package")
        for name, text in documents().items():
            for claim in claims:
                self.assertNotIn(claim, text, "%s: %r" % (name, claim))
        with open(os.path.join(ROOT, "gnome", "install-overlap.sh"),
                  encoding="utf-8") as f:
            self.assertNotIn("not installed by\n# the .deb", f.read())

    def test_the_overlap_sample_reports_the_rigs_own_millimetres(self):
        """README.md's overlap example showed `320mm x 200mm`, which is an
        older rig's head.  Measured on the virtio-vga rig the rest of that
        document describes: a 1920x1080 head reports **480mm x 270mm** through
        the Wayland backends, from the EDID size `wl_output` carries.  (The X
        server's own RandR computes millimetres from 96 dpi instead and says
        487mm x 274mm for the same head, which is why vm/README.md quotes that
        number for the Xfce flavors: two right answers, one per path.)"""
        docs = documents()
        section = docs["README.md"].split(
            "#### Overlapping monitors on GNOME")[1].split("\n### ")[0]
        m = re.search(r"Virtual-2 connected 1920x1080\+960\+0[^\n]*?"
                      r"(\d+mm x \d+mm)", section)
        self.assertTrue(m, "the sample --query line is gone from the section")
        self.assertEqual(m.group(1), "480mm x 270mm")
        for name, text in docs.items():
            self.assertNotIn("320mm x 200mm", text, name)

    def test_the_overlap_installers_exit_status_is_written_down(self):
        """It exits 1 on its ordinary success path, because the extension is
        installed and gnome-shell has not loaded it yet -- the same as the
        bridge installer, whose 1 the README does document.  A `set -e` script
        that follows the three steps stops at the first one."""
        with open(os.path.join(ROOT, "gnome", "install-overlap.sh"),
                  encoding="utf-8") as f:
            sh = f.read()
        # the path that says "log out and back in" really does exit non-zero
        self.assertIn("log out and back in once.  gnome-shell scans extension", sh)
        self.assertRegex(sh, r"comes up idle[^`]*?EOM\n    exit 1")
        self.assertIn("Exit status", sh)
        docs = documents()
        # ...and it is written where somebody is being told to type it
        section = docs["README.md"].split("#### Overlapping monitors on GNOME")[1]
        section = section.split("\n### ")[0]
        self.assertIn("sh gnome/install-overlap.sh", section)
        self.assertIn("exits 1", section)
        line = [ln for ln in docs["gnome/README.md"].splitlines()
                if ln.startswith("sh gnome/install-overlap.sh ")]
        self.assertTrue(line and "exit 1" in line[0], line)


if __name__ == "__main__":
    unittest.main()
