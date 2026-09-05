#!/usr/bin/env python3
"""The no-authorization-dialog guarantee, enforced statically.

GNOME and KDE both show a consent dialog to an application that injects
input through the desktop portal -- xdg-desktop-portal's RemoteDesktop and
InputCapture interfaces, the ones libei speaks -- and a polkit agent window
to anything that asks PolicyKit for an authorization. These tools promise
that neither ever appears: input goes through the kernel's `/dev/uinput` (as
root, or through the udev rule this repo ships), windows and displays go
through the compositor's own session-bus interfaces, and on wlroots input
goes through `zwp_virtual_keyboard_v1` / `zwlr_virtual_pointer_v1`. Not one
of those has a consent step. README.md's "No authorization dialog" section
states that and records the measurement behind it.

A measurement is a snapshot; this test is the ratchet. The day a package
grows a portal proxy, a libei backend or a PolicyKit action, that README
section stops being true and no dbus-monitor is watching -- so the suite
fails here instead, before it ships. A route that genuinely needs one of
these has to change the README section, the support matrix and this list in
the same commit, which is the point: the guarantee cannot be lost by
accident.

Two things the token list deliberately does *not* match, because our own
code says them to claim the opposite and that claim should stay greppable:
the bare word "portal" (`wmirror` names the portal in the message that
explains why it refuses to mirror on GNOME and KDE) and the bare word
"polkit" (`wdotool/backend_kwin.py` and `wxrandr/` note that KWin scripting
and `kde_output_management_v2` have no polkit action behind them).

Not scanned: `tests/` -- this file names every token -- and `vm/`, whose rig
drives a real portal client and `pkexec` on purpose, as the positive
controls that prove a dialog would have been seen if there had been one.
"""

import os
import re
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This file spawns nothing, but the suite-wide guard wants the line in every
# test file and the guard is right: the day this one shells a tool, it is
# already here.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The six tools this repo ships, i.e. everything that runs on a user's
# machine as one of our commands.
PACKAGES = ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr", "wmirror")

# The other half of what a user installs: the GNOME Shell extension and the
# script that installs it and the udev rule. The extension runs inside
# gnome-shell and the installer runs under sudo, so both are places a
# consent prompt could be introduced. `.md` files are left out on purpose --
# documentation is allowed, and required, to name what we do not use.
EXTRA_DIRS = ("gnome",)
DOC_SUFFIXES = (".md",)

FORBIDDEN = (
    "org.freedesktop.portal",       # the portal's public bus name
    "org.freedesktop.impl.portal",  # the desktop's implementation of it
    "RemoteDesktop",                # the portal interface that injects input
    "InputCapture",                 # its newer sibling, same consent dialog
    "libei",                        # the transport both of them speak
    "PolicyKit",                    # a polkit action means an agent window
)

# Case-insensitive: a different spelling is the same call.
_RE = re.compile("|".join(re.escape(t) for t in FORBIDDEN), re.IGNORECASE)

SKIP_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".png", ".gif", ".svg")


def _files(root, skip_docs=False):
    """Every text file under `root`, build and image droppings aside."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(SKIP_SUFFIXES):
                continue
            if skip_docs and name.endswith(DOC_SUFFIXES):
                continue
            out.append(os.path.join(dirpath, name))
    return out


def _hits(paths):
    """(path, lineno, line) for every forbidden token in `paths`."""
    found = []
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if _RE.search(line):
                    found.append((os.path.relpath(path, REPO), n,
                                  line.strip()[:120]))
    return found


def _report(hits):
    return "\n".join("%s:%d: %s" % h for h in hits)


class NoPortalNoPolkit(unittest.TestCase):
    """See the module docstring: this is the README's guarantee as a test."""

    def test_the_six_packages_are_all_there(self):
        """A rename must not turn the scan below into a no-op."""
        for pkg in PACKAGES:
            self.assertTrue(os.path.isdir(os.path.join(REPO, pkg)),
                            "package %s is gone: fix PACKAGES, or the "
                            "guarantee stops being checked for it" % pkg)

    def test_no_package_references_the_portal_or_polkit(self):
        paths = []
        for pkg in PACKAGES:
            paths += _files(os.path.join(REPO, pkg))
        # Guard against a scan that walks nothing and passes vacuously.
        self.assertGreater(len(paths), len(PACKAGES))
        hits = _hits(paths)
        self.assertEqual(hits, [], "the tools must never reach for the "
                         "desktop portal or PolicyKit -- that is what puts "
                         "GNOME's and KDE's consent dialog on screen, and "
                         "README.md promises it never appears:\n"
                         + _report(hits))

    def test_the_extension_and_installer_do_not_either(self):
        """`gnome/` is installed too, and the installer runs as root."""
        paths = []
        for d in EXTRA_DIRS:
            paths += _files(os.path.join(REPO, d), skip_docs=True)
        self.assertGreater(len(paths), 0)
        hits = _hits(paths)
        self.assertEqual(hits, [], "the bridge extension and its installer "
                         "must not introduce a prompt either:\n"
                         + _report(hits))

    def test_the_matcher_would_catch_a_real_one(self):
        """The ratchet is worthless if the pattern does not match."""
        samples = [
            'bus.call("org.freedesktop.portal.Desktop", "/org/freedesktop'
            '/portal/desktop", "org.freedesktop.portal.RemoteDesktop", '
            '"CreateSession", ...)',
            "from gi.repository import Libei",
            "pkaction = 'org.freedesktop.policykit.exec'",
            "org.freedesktop.impl.portal.InputCapture",
        ]
        for s in samples:
            self.assertTrue(_RE.search(s), s)
        # ... and does not fire on our own "we do not use it" comments.
        for s in ["no portal, no polkit, no security-context check",
                  "the desktop portal, which asks the user for permission",
                  "PKEXEC_UID"]:
            self.assertIsNone(_RE.search(s), s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
