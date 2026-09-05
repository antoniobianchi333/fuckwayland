"""Suite-wide guard: no test may leave an input daemon running.

This is the leak itself, not a hypothetical one. The suite spawned daemons
and did not stop them, which is how the test rig came to have 161 of them
alive at once -- ~3GB of resident memory, each listening on a socket in a
per-test runtime directory that had since been deleted, so they were
unreachable as well as immortal; the oldest had been running eighteen
hours. Stopping belongs to the tests that spawn (support.stop_daemons_under
registered before the spawn, so it runs however the test ends); this makes
sure the next test to spawn one cannot quietly forget.

`zz`, because both runners that collect a directory sort the files: this
one goes last. The daemons alive at *import* are the baseline, and import
is before any test has run under both of them -- `unittest discover` loads
every module while building the suite, and pytest collects before it runs.
A daemon that was already there is somebody's real one, or another suite
running beside this one, and is not this suite's business.
"""

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from support import daemon_pids, daemon_runtime_dir, stop_daemon

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

_BEFORE = set(daemon_pids())

# A daemon signalled a moment ago is allowed to finish dying: a test's own
# cleanup waits for its daemons, but a daemon that is on its way out for a
# reason of its own (an idle timer, a socket that has just gone) is racing
# this file, and it is the leftovers that matter, not the microseconds.
GRACE = 5.0


class NoDaemonIsLeftBehind(unittest.TestCase):
    def test_the_suite_stopped_every_daemon_it_started(self):
        deadline = time.monotonic() + GRACE
        while True:
            left = sorted(set(daemon_pids()) - _BEFORE)
            if not left or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        if not left:
            return

        # Say which, before killing them: the pid on its own tells nobody
        # which test is at fault and the runtime directory usually does.
        # And kill them, so that a suite which fails this does not hand the
        # next run a machine with even more of them on it.
        detail = ", ".join(
            "pid %d (%s)" % (pid, daemon_runtime_dir(pid) or "unknown dir")
            for pid in left)
        stuck = [pid for pid in left if not stop_daemon(pid)]
        self.fail("%d wdotool daemon(s) still running at the end of the "
                  "suite: %s%s -- whatever spawned them must stop them "
                  "(tests/support.py: stop_daemons_under)"
                  % (len(left), detail,
                     "; could not stop %s" % stuck if stuck else ""))


if __name__ == "__main__":
    unittest.main()
