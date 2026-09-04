"""wmirror: the lifetime of the helper, driven against a stub wl-mirror.

wl-mirror is a process that has to outlive the command that started it, so
wmirror follows wxrandr/gamma.py's holder: double-fork, (pid, starttime) in
a state file, uid check before any signal, bounded SIGTERM then SIGKILL.
What is new here is that the process is not ours -- so the record carries
two (pid, starttime) pairs, and every transition below has to leave nothing
running that `wmirror --list` cannot find and `wmirror --stop` cannot end.

The stub stands in for wl-mirror: it prints the libEGL chatter the real one
prints while it is working perfectly (a launcher must never read stderr as
failure), and it can be told to fail at startup or to die later.
"""

import contextlib
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wmirror import cli, core, supervise                         # noqa: E402

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["FUCKWAYLAND_PASSTHROUGH"] = "never"

STUB = """#!/bin/sh
# stand-in for wl-mirror
if [ -n "$WMIRROR_STUB_LOG" ]; then
    printf '%s wayland=%s\\n' "$*" "$WAYLAND_DISPLAY" >> "$WMIRROR_STUB_LOG"
fi
echo "libEGL warning: DRI2: failed to authenticate" >&2
if [ -n "$WMIRROR_STUB_FAIL" ]; then
    echo "error: options::find_output(): output NOPE not found" >&2
    exit 1
fi
exec sleep "${WMIRROR_STUB_LIFE:-30}"
"""


def gone(pid, tries=100):
    for _ in range(tries):
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.02)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wmirror-life-")
        self.addCleanup(shutil.rmtree, self.tmp,
                        ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.stub = os.path.join(self.bin, core.HELPER)
        with open(self.stub, "w") as f:
            f.write(STUB)
        os.chmod(self.stub, 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        env = {"XDG_RUNTIME_DIR": self.tmp, "WMIRROR_STUB_LOG": self.log,
               "PATH": self.bin + os.pathsep + os.environ.get("PATH", "")}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.addCleanup(self.env.stop)
        # a start blocks for this long watching the helper stay up; the
        # forked supervisor inherits the shortened value
        self.window = mock.patch.object(supervise, "STARTUP_SECONDS", 0.2)
        self.window.start()
        self.addCleanup(self.window.stop)
        self.started = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for rec in self.started:
            for key in ("pid", "helper_pid"):
                pid = rec.get(key)
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass

    def start(self, recs, source="A", target="B", region=None, **kw):
        argv = core.build_argv(source, target, region=region,
                               helper=self.stub)
        err = supervise.start(recs, source, target, argv, region=region,
                              **kw)
        if target in recs:
            self.started.append(recs[target])
        return err


class Starting(Base):
    def test_the_record_carries_both_processes_and_both_run(self):
        recs = {}
        self.assertIsNone(self.start(recs))
        rec = recs["B"]
        self.assertNotEqual(rec["pid"], rec["helper_pid"])
        self.assertNotEqual(rec["start"], "?")
        self.assertEqual(supervise.liveness(rec), (True, True))
        self.assertEqual(rec["source"], "A")
        with open(self.log) as f:
            self.assertEqual(f.read().split(" wayland=")[0],
                             "--fullscreen-output B --scaling fit A")

    def test_the_supervisor_is_detached(self):
        """setsid + double fork: it is not our child and not in our process
        group, so the shell that started the mirror can go away."""
        recs = {}
        self.start(recs)
        pid = recs["B"]["pid"]
        self.assertNotEqual(os.getpgid(pid), os.getpgid(0))
        self.assertNotEqual(os.getsid(pid), os.getsid(0))  # own session
        with open("/proc/%d/stat" % pid, "rb") as f:
            after = f.read().rsplit(b")", 1)[1].split()
        self.assertNotEqual(int(after[1]), os.getpid())   # reparented

    def test_stderr_chatter_is_not_failure(self):
        """The real wl-mirror prints libEGL warnings while working; the stub
        prints one too. A start that read stderr as failure would refuse
        every real mirror."""
        recs = {}
        self.assertIsNone(self.start(recs))
        self.assertEqual(supervise.liveness(recs["B"])[1], True)

    def test_a_helper_that_fails_at_startup_is_reported_by_its_own_words(self):
        recs = {}
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_FAIL": "1"}):
            err = self.start(recs)
        self.assertTrue(err)
        self.assertIn("output NOPE not found", err[0])
        self.assertEqual(recs, {})               # no record left behind

    def test_a_helper_that_cannot_be_executed_at_all(self):
        recs = {}
        argv = core.build_argv("A", "B", helper=os.path.join(self.tmp, "no"))
        err = supervise.start(recs, "A", "B", argv)
        self.assertTrue(err)
        self.assertIn("cannot run", err[0])
        self.assertEqual(recs, {})


class Stopping(Base):
    def test_stop_ends_both_processes(self):
        recs = {}
        self.start(recs)
        rec = recs["B"]
        self.assertTrue(supervise.stop_record(rec))
        self.assertTrue(gone(rec["helper_pid"]))
        self.assertTrue(gone(rec["pid"]))
        self.assertEqual(supervise.liveness(rec), (False, False))

    def test_stopping_something_already_gone_is_not_an_error(self):
        recs = {}
        self.start(recs)
        rec = recs["B"]
        supervise.stop_record(rec)
        self.assertFalse(supervise.stop_record(rec))

    def test_killing_the_supervisor_leaves_a_findable_mirror(self):
        """Our own process being killed must not strand wl-mirror painting
        somebody's screen with no way to reach it: the record carries the
        helper's own (pid, starttime) for exactly this."""
        recs = {}
        self.start(recs)
        rec = recs["B"]
        os.kill(rec["pid"], signal.SIGKILL)
        self.assertTrue(gone(rec["pid"]))
        self.assertEqual(supervise.liveness(rec), (False, True))
        supervise.reap(recs)
        self.assertIn("B", recs)
        self.assertTrue(recs["B"]["orphan"])
        self.assertIn("(supervisor gone)", core.fmt_record("B", recs["B"]))
        self.assertTrue(supervise.stop_record(rec))
        self.assertTrue(gone(rec["helper_pid"]))

    def test_a_helper_that_dies_on_its_own_takes_the_supervisor_with_it(self):
        recs = {}
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_LIFE": "0.6"}):
            self.assertIsNone(self.start(recs))
        rec = recs["B"]
        self.assertTrue(gone(rec["helper_pid"]))
        self.assertTrue(gone(rec["pid"]))
        supervise.reap(recs)
        self.assertEqual(recs, {})               # reaped, silently

    def test_a_stubborn_helper_gets_sigkill(self):
        proc = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 3"],
                                stderr=subprocess.PIPE)
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.wait)
        with mock.patch.object(supervise, "STOP_SECONDS", 0.3):
            supervise._stop_child(proc)
        self.assertIsNotNone(proc.poll())


class Reaping(Base):
    def test_a_recycled_pid_is_never_mistaken_for_ours(self):
        """The (pid, starttime) guard: a pid whose process started at a
        different moment is a different process, and never signalled."""
        recs = {}
        self.start(recs)
        rec = recs["B"]
        stale = {"helper_pid": rec["helper_pid"],
                 "helper_start": str(int(rec["helper_start"]) + 5)}
        self.assertEqual(supervise.liveness(stale), (False, False))
        self.assertFalse(supervise.stop_record(stale))
        self.assertFalse(gone(rec["helper_pid"], tries=5))

    def test_garbage_records_are_dropped(self):
        recs = {"B": "not a dict", "C": {}, "D": {"pid": 0}}
        supervise.reap(recs)
        self.assertEqual(recs, {})

    @unittest.skipIf(os.geteuid() == 0, "runs as root: everything is ours")
    def test_a_process_that_is_not_ours_is_never_signalled(self):
        """The uid guard from gamma.stop_holder: a state file is a cache,
        and a pid in it that belongs to somebody else is not our helper,
        whatever the record claims."""
        self.assertFalse(supervise.alive(1, "?", core.HELPER))
        self.assertFalse(supervise.stop_record(
            {"helper_pid": 1, "helper_start": "?"}))


class Watching(Base):
    """The supervisor's own reasons to end a mirror. wl-mirror exits by
    itself when its SOURCE disappears; everything else here is ours."""

    class FakeWatch:
        def __init__(self, heads):
            self.a, self.b = socket.socketpair()
            self.serial = 1
            self.heads = list(heads)
            self.raise_on_dispatch = None
            self.conn = mock.Mock()
            self.conn.sock = self.b
            self.conn.dispatch.side_effect = self._dispatch

        def _dispatch(self, timeout=None):
            self.b.recv(4096)
            if self.raise_on_dispatch:
                raise self.raise_on_dispatch
            self.serial += 1
            return True

        def live_heads(self):
            return self.heads

        def close(self):
            self.a.close()
            self.b.close()

        def change(self, heads):
            self.heads = list(heads)
            self.a.sendall(b"x")

    @staticmethod
    def head(name, x=0, w=1920, enabled=True):
        return {"name": name, "enabled": enabled, "x": x, "y": 0,
                "transform": 0, "scale": 1.0, "current": 1,
                "modes": [{"id": 1, "w": w, "h": 1080}]}

    def supervise_in_thread(self, watch, life="30"):
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_LIFE": life}):
            proc = subprocess.Popen([self.stub, "x"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        done = threading.Event()
        self.addCleanup(watch.close)
        self.addCleanup(proc.stderr.close)
        t = threading.Thread(
            target=lambda: (supervise._supervise(
                proc, None, "A", "B", "sock"), done.set()),
            daemon=True)
        # cleanup is LIFO: end the helper first, which ends the loop, then
        # join the thread
        self.addCleanup(t.join, 5)
        self.addCleanup(self._end, proc)
        with mock.patch.object(supervise, "_open_watch", return_value=watch):
            t.start()
            time.sleep(0.1)          # the patch only has to cover the open
        return proc, done, t

    @staticmethod
    def _end(proc):
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()

    def test_the_target_disappearing_ends_the_mirror(self):
        """wl-mirror survives this one on its own -- and sway then moves the
        mirror window onto another output, which can be its own source."""
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        self.assertFalse(done.wait(0.3))
        watch.change([self.head("A")])
        self.assertTrue(done.wait(3), "the supervisor kept going")

    def test_the_target_being_switched_off_ends_it(self):
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A"),
                      self.head("B", x=1920, enabled=False)])
        self.assertTrue(done.wait(3))

    def test_a_layout_change_onto_the_source_ends_it(self):
        """`wxrandr --output B --same-as A` under a running mirror: the two
        now share pixels, so the helper would capture its own window."""
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A"), self.head("B", x=0)])
        self.assertTrue(done.wait(3))

    def test_a_harmless_change_does_not(self):
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A"), self.head("B", x=2000, w=1280)])
        self.assertFalse(done.wait(1))

    def test_the_compositor_going_away_ends_it(self):
        """A compositor restart: wl-mirror dies of a broken pipe and so do
        we, leaving no orphan and no record."""
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        watch.raise_on_dispatch = RuntimeError("wayland connection closed")
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A")])
        self.assertTrue(done.wait(3))

    def test_the_helper_dying_ends_it(self):
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch, life="0.4")
        self.assertTrue(done.wait(3))


class HelperEnvironment(Base):
    """wmirror runs from a hotkey, from `sudo` and from `ssh root@box` with
    an empty environment, like every other tool here -- so the helper is
    told which compositor to talk to instead of reading a WAYLAND_DISPLAY
    that may not be set at all."""

    def test_the_socket_is_handed_to_the_helper(self):
        env = supervise.helper_env("/run/user/1000/wayland-1")
        self.assertEqual(env["WAYLAND_DISPLAY"], "/run/user/1000/wayland-1")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")

    def test_without_one_the_environment_is_left_alone(self):
        self.assertIsNone(supervise.helper_env(None))

    def test_the_helper_really_gets_it(self):
        recs = {}
        argv = [self.stub, "--show-env"]
        supervise.start(recs, "A", "B", argv,
                        wayland_socket="/run/user/4242/wayland-9")
        if "B" in recs:
            self.started.append(recs["B"])
        with open(self.log) as f:
            self.assertIn("wayland=/run/user/4242/wayland-9", f.read())


class Commands(Base):
    """The same transitions through the command line, with the compositor
    faked out but real processes underneath."""

    def outputs(self):
        return [core.Output("A", True, 0, 0, 1920, 1080),
                core.Output("B", True, 1920, 0, 1280, 1024)]

    def invoke(self, argv):
        conn = mock.Mock()
        o, e = io.StringIO(), io.StringIO()
        with mock.patch.object(core, "open_conn", return_value=conn), \
                mock.patch.object(core, "require_capture",
                                  return_value=[(core.SCREENCOPY, 3)]), \
                mock.patch.object(core, "read_outputs",
                                  return_value=self.outputs()), \
                contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(argv)
        return rc, o.getvalue(), e.getvalue()

    def live(self):
        state = core.load_state()
        recs = core.records(state)
        for rec in recs.values():
            self.started.append(rec)
        return recs

    def test_start_list_stop(self):
        rc, o, e = self.invoke(["A", "--to", "B"])
        self.assertEqual(rc, 0, e)
        self.assertIn("B <- A", o)
        self.assertIn("wl-mirror pid", o)
        recs = self.live()
        self.assertEqual(supervise.liveness(recs["B"]), (True, True))

        rc, o, e = self.invoke(["--list"])
        self.assertEqual(rc, 0)
        self.assertIn("B <- A", o)

        rc, o, e = self.invoke(["--stop", "B"])
        self.assertEqual(rc, 0, e)
        self.assertIn("stopped", o)
        self.assertTrue(gone(recs["B"]["helper_pid"]))
        rc, o, e = self.invoke(["--list"])
        self.assertEqual(o, "")

    def test_a_second_mirror_on_one_target_is_refused_then_replaced(self):
        """wl-mirror would happily run two on one output, the older one
        invisible behind the newer. The user could see one and stop the
        other."""
        self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        first = dict(self.live()["B"])
        rc, o, e = self.invoke(["A", "--to", "B"])
        self.assertEqual(rc, 1)
        self.assertIn("already mirroring", e)
        self.assertEqual(supervise.liveness(first), (True, True))

        rc, o, e = self.invoke(["A", "--to", "B", "--replace"])
        self.assertEqual(rc, 0, e)
        second = self.live()["B"]
        self.assertTrue(gone(first["helper_pid"]))
        self.assertNotEqual(second["helper_pid"], first["helper_pid"])
        self.assertEqual(supervise.liveness(second), (True, True))

    def test_stop_all_ends_everything_it_started(self):
        self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        recs = dict(self.live())
        rc, o, e = self.invoke(["--stop-all"])
        self.assertEqual(rc, 0)
        self.assertIn("stopped", o)
        self.assertTrue(gone(recs["B"]["helper_pid"]))
        self.assertEqual(self.invoke(["--list"])[1], "")

    def test_a_dead_mirror_is_reaped_by_the_next_query(self):
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_LIFE": "0.6"}):
            self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        recs = dict(self.live())
        self.assertTrue(gone(recs["B"]["helper_pid"]))
        self.assertEqual(self.invoke(["--list"])[1], "")
        # ...and the target is free again
        self.assertEqual(self.invoke(["A", "--to", "B"])[0], 0)
        self.live()

    def test_the_region_reaches_the_helper(self):
        rc, o, e = self.invoke(["A", "--to", "B", "--region", "500x300+100+100",
                             "--scaling", "cover"])
        self.assertEqual(rc, 0, e)
        self.live()
        with open(self.log) as f:
            self.assertEqual(
                f.read().split(" wayland=")[0],
                "--fullscreen-output B --scaling cover "
                "--region 100,100 500x300 A")
        self.assertIn("region 500x300+100+100", self.invoke(["--list"])[1])


if __name__ == "__main__":
    unittest.main()
