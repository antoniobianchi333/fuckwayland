#!/usr/bin/env python3
"""fwcommon/session.py over a temporary /run/user.

Every tool starts by answering "which graphical session am I aimed at?", and
session.py answers it by walking /run/user. That walk was only ever exercised
sideways -- two cases inside test_backend_gnome, the X half in
test_session_discovery, and the rest through whatever happened to be under
the real /run/user on the machine running the suite, which is exactly the
thing a test must not depend on.

So the tree here is built by hand: `session.RUN_USER_DIR` is pointed at a
temporary directory whose subdirectory *names* are the uids, which is where
session.py reads them from, and each case puts sockets in it and asks what
comes back. The four questions are deliberately separate -- the Wayland
socket, the sway IPC socket, the user bus and the compositor's bus are four
different answers on `ssh root@box`, and treating them as one is the bug
that keeps coming back.
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fwcommon import session
from support import env
from wdotool.ctx import CmdError

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"


class Tree(unittest.TestCase):
    """A temporary /run/user, and an environment that says nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fw_session_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.run_user = os.path.join(self.tmp, "run-user")
        os.mkdir(self.run_user)
        old = session.RUN_USER_DIR
        session.RUN_USER_DIR = self.run_user
        self.addCleanup(setattr, session, "RUN_USER_DIR", old)
        # a developer's own session must not answer for any of this
        ctx = env(XDG_RUNTIME_DIR=None, WAYLAND_DISPLAY=None, SWAYSOCK=None,
                  I3SOCK=None, DBUS_SESSION_BUS_ADDRESS=None, SUDO_UID=None,
                  PKEXEC_UID=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    # -- building the tree
    def rtdir(self, uid) -> str:
        d = os.path.join(self.run_user, str(uid))
        os.makedirs(d, exist_ok=True)
        return d

    def sock(self, uid, name) -> str:
        p = os.path.join(self.rtdir(uid), name)
        open(p, "w").close()
        return p

    def dirs(self):
        return [d for _u, d in session.runtime_dir_candidates()]

    def uids(self):
        return [u for u, _d in session.runtime_dir_candidates()]


class Candidates(Tree):
    def test_a_wayland_socket_moves_a_directory_to_the_front(self):
        self.rtdir(0)
        self.rtdir(1000)
        self.sock(1001, "wayland-0")
        self.assertEqual(self.uids(), [1001, 1000, 0])

    def test_a_lock_file_is_not_a_wayland_socket(self):
        self.sock(1000, "wayland-1")
        self.sock(1001, "wayland-1.lock")
        self.assertEqual(self.uids()[0], 1000)

    def test_real_users_before_system_uids_and_then_by_number(self):
        for uid in (0, 1000, 1001, 42):
            self.rtdir(uid)
        self.assertEqual(self.uids(), [1000, 1001, 0, 42])

    def test_the_sudo_invoker_comes_first_of_all(self):
        for uid in (0, 1000, 1001):
            self.rtdir(uid)
        with env(SUDO_UID="1001"):
            self.assertEqual(self.uids(), [1001, 1000, 0])
        # PKEXEC_UID is read the same way, and a junk value is ignored
        with env(PKEXEC_UID="1001"):
            self.assertEqual(self.uids(), [1001, 1000, 0])
        with env(SUDO_UID="not-a-number"):
            self.assertEqual(self.uids(), [1000, 1001, 0])

    def test_xdg_runtime_dir_leads_its_own_group(self):
        mine = self.rtdir(1000)
        self.sock(1001, "wayland-0")
        with env(XDG_RUNTIME_DIR=mine):
            # ...but the group with a Wayland socket still comes first: an
            # XDG_RUNTIME_DIR with no compositor in it is what `sudo` and
            # `ssh root@` both hand us
            self.assertEqual(self.dirs()[0], os.path.join(self.run_user, "1001"))
        self.sock(1000, "wayland-1")
        with env(XDG_RUNTIME_DIR=mine):
            self.assertEqual(self.dirs()[0], mine)

    def test_names_that_are_not_uids_are_skipped(self):
        os.makedirs(os.path.join(self.run_user, "systemd"))
        self.rtdir(1000)
        self.assertEqual(self.uids(), [1000])

    def test_no_run_user_at_all_is_not_an_error(self):
        shutil.rmtree(self.run_user)
        self.assertEqual(session.runtime_dir_candidates(), [])
        self.assertIsNone(session.find_wayland_socket())
        self.assertIsNone(session.session_uid())


class WaylandSocket(Tree):
    def test_the_scan_answers_with_the_directory_and_the_owner(self):
        p = self.sock(1001, "wayland-1")
        self.assertEqual(session.find_wayland_socket(),
                         (1001, os.path.dirname(p), p))

    def test_the_lowest_name_in_a_directory_wins(self):
        self.sock(1000, "wayland-3")
        first = self.sock(1000, "wayland-0")
        self.assertEqual(session.find_wayland_socket()[2], first)

    def test_session_uid_is_the_socket_owner_not_the_first_candidate(self):
        self.rtdir(1000)            # a real user with no compositor
        self.sock(1001, "wayland-0")
        self.assertEqual(session.session_uid(), 1001)

    def test_session_uid_falls_back_to_the_candidate_order(self):
        self.rtdir(0)
        self.rtdir(1000)
        self.assertEqual(session.session_uid(), 1000)


class SwaySocket(Tree):
    def test_the_environment_wins_when_the_socket_is_really_there(self):
        named = os.path.join(self.tmp, "named.sock")
        open(named, "w").close()
        self.sock(1000, "sway-ipc.1000.99.sock")
        with env(SWAYSOCK=named):
            self.assertEqual(session.find_sway_socket(), named)
        with env(I3SOCK=named):
            self.assertEqual(session.find_sway_socket(), named)

    def test_a_stale_name_falls_through_to_the_scan(self):
        p = self.sock(1000, "sway-ipc.1000.99.sock")
        with env(SWAYSOCK=os.path.join(self.tmp, "gone.sock")):
            self.assertEqual(session.find_sway_socket(), p)

    def test_i3_names_are_found_too(self):
        p = self.sock(1000, "i3-ipc.1000.7.sock")
        self.assertEqual(session.find_sway_socket(), p)

    def test_nothing_anywhere(self):
        self.rtdir(1000)
        self.assertIsNone(session.find_sway_socket())


class Buses(Tree):
    def test_a_bus_beside_a_compositor_beats_an_environment_bus_without_one(self):
        """`ssh root@box` gets DBUS_SESSION_BUS_ADDRESS=/run/user/0/bus from
        pam_systemd -- a real bus, with no compositor on it."""
        roots = self.sock(0, "bus")
        self.sock(1000, "wayland-0")
        session_bus = self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path=" + roots):
            self.assertEqual(session.find_user_bus(),
                             (1000, "unix:path=" + session_bus))

    def test_the_environment_bus_wins_when_it_is_in_the_session(self):
        self.sock(1000, "wayland-0")
        mine = self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path=" + mine):
            self.assertEqual(session.find_user_bus()[1], "unix:path=" + mine)

    def test_with_no_compositor_anywhere_the_environment_still_wins(self):
        roots = self.sock(0, "bus")
        self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path=" + roots):
            self.assertEqual(session.find_user_bus()[1], "unix:path=" + roots)

    def test_an_environment_bus_that_does_not_exist_is_ignored(self):
        p = self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path=" + p + "-gone"):
            self.assertEqual(session.find_user_bus(), (1000, "unix:path=" + p))

    def test_the_session_bus_is_anchored_on_the_wayland_socket(self):
        self.sock(0, "bus")
        self.sock(1000, "wayland-0")
        compositor_bus = self.sock(1000, "bus")
        with env(DBUS_SESSION_BUS_ADDRESS="unix:path="
                 + os.path.join(self.run_user, "0", "bus")):
            self.assertEqual(session.find_session_bus(),
                             (1000, "unix:path=" + compositor_bus))

    def test_the_session_bus_degrades_to_the_user_bus_with_no_compositor(self):
        p = self.sock(1000, "bus")
        self.assertEqual(session.find_session_bus(), (1000, "unix:path=" + p))

    def test_no_bus_anywhere(self):
        self.sock(1000, "wayland-0")
        self.assertIsNone(session.find_user_bus())


class RuntimeDir(Tree):
    """runtime_dir(): where a socket, a lock and a state file may live."""

    def setUp(self):
        super().setUp()
        self.fallback = os.path.join(self.tmp, "wdotool-%d")
        old = session.FALLBACK_RUNTIME_DIR
        session.FALLBACK_RUNTIME_DIR = self.fallback
        self.addCleanup(setattr, session, "FALLBACK_RUNTIME_DIR", old)

    def mine(self):
        return self.fallback % os.getuid()

    def test_xdg_runtime_dir_is_taken_as_it_is(self):
        d = self.rtdir(1000)
        with env(XDG_RUNTIME_DIR=d):
            self.assertEqual(session.runtime_dir(), d)

    def test_a_name_that_is_not_a_directory_falls_back(self):
        p = os.path.join(self.tmp, "not-a-dir")
        open(p, "w").close()
        with env(XDG_RUNTIME_DIR=p):
            self.assertEqual(session.runtime_dir(), self.mine())

    def test_the_fallback_is_private_and_ours(self):
        d = session.runtime_dir()
        self.assertEqual(d, self.mine())
        st = os.lstat(d)
        self.assertTrue(stat.S_ISDIR(st.st_mode))
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(st.st_mode & 0o077, 0)

    def test_an_existing_private_directory_is_reused(self):
        os.mkdir(self.mine(), 0o700)
        self.assertEqual(session.runtime_dir(), self.mine())

    def test_a_directory_anyone_could_enter_is_refused(self):
        os.mkdir(self.mine(), 0o755)
        with self.assertRaises(CmdError) as cm:
            session.runtime_dir()
        self.assertIn("not a private directory", str(cm.exception))

    def test_a_symlink_planted_in_its_place_is_refused(self):
        os.symlink(self.tmp, self.mine())
        with self.assertRaises(CmdError) as cm:
            session.runtime_dir()
        self.assertIn("not a private directory", str(cm.exception))

    def test_a_directory_that_cannot_be_made_says_so(self):
        session.FALLBACK_RUNTIME_DIR = os.path.join(self.tmp, "gone", "x-%d")
        with self.assertRaises(CmdError) as cm:
            session.runtime_dir()
        self.assertIn("cannot create", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
