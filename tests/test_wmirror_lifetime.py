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
import fcntl
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

from wdotool import procs
from wmirror import cli, core, supervise
from wxrandr import core as wxcore

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

    def test_an_interrupt_mid_start_leaves_a_stoppable_record(self):
        """Ctrl-C while the start is still reading the status pipe.

        The supervisor names itself before it can fail, and that line is
        acted on the moment it arrives rather than when the start finishes
        -- so the record naming it is already there when the interrupt
        lands. A start that buffered its lines would leave a mirror running
        with nothing able to find it."""
        recs = {}
        real_select = procs.select.select

        class Shim:                # only the second call is interrupted
            @staticmethod
            def select(*a, **kw):
                if recs.get("B", {}).get("pid"):
                    raise KeyboardInterrupt
                return real_select(*a, **kw)

        # the verdict is a second away, so there is a second select call
        with mock.patch.object(supervise, "STARTUP_SECONDS", 1.0), \
                mock.patch.object(procs, "select", Shim):
            with self.assertRaises(KeyboardInterrupt):
                self.start(recs)
        rec = recs["B"]
        self.assertTrue(procs.alive(rec["pid"], rec["start"],
                                    supervise.SUPERVISOR_COMM))
        self.assertTrue(supervise.stop_record(rec))
        self.assertEqual(supervise.liveness(rec), (False, False))

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

    def test_a_zombie_helper_is_not_a_running_mirror(self):
        """A process that has exited but has not been waited for keeps its
        /proc directory, its owner and its start time, so every other test
        here says it is alive. `--list` would print a mirror that had
        stopped painting, and `--stop` would report stopping it."""
        pid = os.fork()
        if pid == 0:                                   # the "helper"
            os._exit(0)
        self.addCleanup(self._reap, pid)
        for _ in range(200):
            if procs.zombie(pid):
                break
            time.sleep(0.01)
        start = supervise.proc_starttime(pid)
        self.assertTrue(procs.zombie(pid), "no zombie to test with")
        self.assertIsNotNone(start)                    # /proc still has it
        self.assertEqual(os.stat("/proc/%d" % pid).st_uid, os.geteuid())
        self.assertFalse(supervise.alive(pid, start, core.HELPER))

    def test_a_record_whose_helper_is_a_zombie_is_reaped(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        self.addCleanup(self._reap, pid)
        for _ in range(200):
            if procs.zombie(pid):
                break
            time.sleep(0.01)
        recs = {"B": {"source": "A", "helper_pid": pid,
                      "helper_start": supervise.proc_starttime(pid)}}
        self.assertEqual(supervise.liveness(recs["B"]), (False, False))
        self.assertTrue(supervise.reap(recs))
        self.assertEqual(recs, {})

    @staticmethod
    def _reap(pid):
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass

    @unittest.skipIf(os.geteuid() == 0, "runs as root: everything is ours")
    def test_a_process_that_is_not_ours_is_never_signalled(self):
        """The uid guard from gamma.stop_holder: a state file is a cache,
        and a pid in it that belongs to somebody else is not our helper,
        whatever the record claims."""
        self.assertFalse(supervise.alive(1, "?", core.HELPER))
        self.assertFalse(supervise.stop_record(
            {"helper_pid": 1, "helper_start": "?"}))


class Diagnosis(unittest.TestCase):
    """What a start says when the helper is gone before the window is out.

    wl-mirror prints `error:` lines while it works -- measured on the rig, a
    mirror that ran for minutes and matched its source pixel for pixel
    printed `error: mirror-screencopy::on_dmabuf_allocated(): failed to
    allocate dmabuf` and fell back to shm. Reporting a helper's death in
    the words of an error it survived is a confident wrong answer."""

    CHATTER = ["libEGL warning: DRI2: failed to authenticate",
               "error: mirror-screencopy::on_dmabuf_allocated(): "
               "failed to allocate dmabuf",
               "warning: falling back to shm capture"]

    def test_its_own_fatal_line_is_the_verdict(self):
        self.assertEqual(
            supervise._diagnosis(
                self.CHATTER
                + ["error: options::find_output(): output NOPE not found"],
                1),
            "error: options::find_output(): output NOPE not found")

    def test_the_chatter_it_survives_is_never_the_verdict(self):
        said = supervise._diagnosis(self.CHATTER, 1)
        self.assertNotIn("dmabuf", said)
        self.assertIn("exited with status 1", said)

    def test_a_signal_is_named_rather_than_a_negative_number(self):
        said = supervise._diagnosis(self.CHATTER, -11)
        self.assertIn("SIGSEGV", said)
        self.assertNotIn("dmabuf", said)

    def test_a_helper_that_said_nothing_at_all(self):
        self.assertIn("exited with status 3", supervise._diagnosis([], 3))


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
        """One zwlr head as WlrOutputs.live_heads() hands it over: every
        field the wire sets, because the snapshot reads every field."""
        return {"id": 1, "name": name, "description": name,
                "enabled": enabled, "x": x, "y": 0, "transform": 0,
                "scale": 1.0, "current": 1, "gone": False,
                "mm_w": 0, "mm_h": 0, "make": "Unknown", "model": "Unknown",
                "serial": "Unknown",
                "modes": [{"id": 1, "w": w, "h": 1080, "refresh": 60000,
                           "preferred": True}]}

    def supervise_in_thread(self, watch, life="30", **kw):
        with mock.patch.dict(os.environ, {"WMIRROR_STUB_LIFE": life}):
            proc = subprocess.Popen([self.stub, "x"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
        done = threading.Event()
        self.addCleanup(watch.close)
        self.addCleanup(proc.stderr.close)
        t = threading.Thread(
            target=lambda: (supervise._supervise(
                proc, None, "A", "B", "sock", **kw), done.set()),
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

    def test_the_source_moving_ends_a_region_mirror(self):
        """wl-mirror resolves a layout rectangle against the source once,
        when it starts, and then clamps in silence. A start refuses a
        region that does not fit its source; the layout must not be able to
        arrange that afterwards behind the mirror's back."""
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(
            watch, region=(100, 100, 500, 300), src_rect=(0, 0, 1920, 1080))
        watch.change([self.head("A", x=3840), self.head("B", x=1920)])
        self.assertTrue(done.wait(3))

    def test_the_same_move_leaves_a_whole_output_mirror_alone(self):
        watch = self.FakeWatch([self.head("A"), self.head("B", x=1920)])
        proc, done, t = self.supervise_in_thread(watch)
        watch.change([self.head("A", x=3840), self.head("B", x=1920)])
        self.assertFalse(done.wait(1.5))


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


class MirrorCli:
    """cli.main() with the compositor faked out and real processes
    underneath. Not a TestCase: mixed into the ones below."""

    def outputs(self):
        return [wxcore.OutputState("A", True, 0, 0, 1920, 1080),
                wxcore.OutputState("B", True, 1920, 0, 1280, 1024)]

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

class Commands(MirrorCli, Base):
    """The transitions through the command line."""

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


class Serialising(MirrorCli, Base):
    """Two wmirrors at once.

    The state file is the only trace a mirror leaves -- wl-mirror is
    invisible to output management -- so a write that loses a record leaves
    a helper fullscreen on somebody's screen that `--list` cannot see and
    `--stop` cannot end. Measured before the lock existed: two starts on
    one target, two helpers, one record, one orphan."""

    def _record_of_something_running(self):
        """A record that survives a reap: our own pid as the supervisor,
        a real stub as the helper."""
        helper = subprocess.Popen([self.stub, "x"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.addCleanup(helper.wait)
        self.addCleanup(helper.kill)
        return {"source": "A", "target": "B", "scaling": "fit",
                "region": None,
                "pid": os.getpid(),
                "start": supervise.proc_starttime(os.getpid()),
                "helper_pid": helper.pid,
                "helper_start": supervise.proc_starttime(helper.pid)}

    def test_a_start_waits_for_the_one_in_flight_and_then_sees_it(self):
        """Without the lock both starts read an empty file, both spawn a
        helper, and the second write drops the first record."""
        rec = self._record_of_something_running()
        pid = os.fork()
        if pid == 0:                       # the wmirror already in flight
            try:
                with core.state_lock():
                    time.sleep(0.5)        # ...still starting its helper
                    state = core.load_state()
                    core.records(state)["B"] = rec
                    state.save()
            finally:
                os._exit(0)
        self.addCleanup(self._reap, pid)
        time.sleep(0.1)
        began = time.monotonic()
        with mock.patch.object(supervise, "start") as start:
            rc, o, e = self.invoke(["A", "--to", "B"])
        waited = time.monotonic() - began
        self.assertEqual(rc, 1)
        self.assertIn("already mirroring", e)
        start.assert_not_called()
        self.assertGreater(waited, 0.3, "did not wait for the lock")

    def test_a_killed_start_does_not_leave_the_lock_to_the_mirror(self):
        """The lock is a POSIX record lock, which belongs to the process,
        so the supervisor we fork does not carry it. With flock the lock
        lives in the open file description the supervisor inherits: kill a
        start before it can unlock and every later wmirror would wait out
        the whole timeout (both measured)."""
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:                       # a start that holds the lock
            os.close(r)
            try:
                with core.state_lock():
                    grand = os.fork()      # ...and forks its supervisor
                    if grand == 0:
                        os.close(w)
                        time.sleep(5)
                        os._exit(0)
                    os.write(w, str(grand).encode() + b"\n")
                    time.sleep(5)
            finally:
                os._exit(0)
        os.close(w)
        supervisor = int(os.read(r, 32))   # named once the lock is held
        os.close(r)
        self.addCleanup(self._kill, supervisor)
        os.kill(pid, signal.SIGKILL)       # killed before it can unlock
        self._reap(pid)
        began = time.monotonic()
        with core.state_lock():
            waited = time.monotonic() - began
        self.assertLess(waited, 1.0,
                        "the lock outlived the command that took it")

    def _lock_is_free(self):
        """Can another process take the start lock right now?"""
        pid = os.fork()
        if pid == 0:
            code = 2
            try:
                fd = os.open(core.lock_path(),
                             os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    code = 0
                except OSError:
                    code = 1
            except OSError:
                pass
            os._exit(code)
        return os.waitpid(pid, 0)[1] == 0

    def test_saving_the_state_does_not_drop_the_start_lock(self):
        """Closing any fd to a file drops that process's POSIX locks on it,
        and State.save opens and closes its own lock file on every write --
        so the two locks must not live on the same file."""
        self.assertNotEqual(core.lock_path(), core.state_path() + ".lock")
        with core.state_lock():
            self.assertFalse(self._lock_is_free())
            state = core.load_state()
            core.records(state)["B"] = {"source": "A"}
            state.save()                   # opens and closes ITS lock file
            self.assertFalse(self._lock_is_free(),
                             "State.save's lock file dropped ours")
        self.assertTrue(self._lock_is_free())

    def test_a_start_interrupted_mid_flight_is_still_stoppable(self):
        """Ctrl-C in the second a start blocks for. The supervisor names
        itself before it can fail, so the record exists -- it just has to
        reach the file, or the mirror is one nobody can end."""
        real = supervise.start

        def interrupted(*a, **kw):
            real(*a, **kw)
            raise KeyboardInterrupt

        with mock.patch.object(supervise, "start", interrupted):
            rc, o, e = self.invoke(["A", "--to", "B"])
        self.assertEqual(rc, 130)
        recs = self.live()
        self.assertIn("B", recs)
        helper = recs["B"]["helper_pid"]
        self.assertEqual(self.invoke(["--stop", "B"])[0], 0)
        self.assertTrue(gone(helper))

    def test_a_start_that_cannot_be_written_down_is_stopped_again(self):
        """An unwritable state file is not a reason to leave a mirror
        painting with nothing able to find it."""
        seen = []

        def unwritable(target, rec):
            seen.append(dict(rec))
            return False

        with mock.patch.object(core, "recorded", unwritable):
            rc, o, e = self.invoke(["A", "--to", "B"])
        self.assertEqual(rc, 1)
        self.assertIn("could not write it down", e)
        self.assertTrue(gone(seen[0]["helper_pid"]))
        self.assertTrue(gone(seen[0]["pid"]))
        self.assertEqual(self.invoke(["--list"])[1], "")

    @staticmethod
    def _reap(pid):
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass

    @staticmethod
    def _kill(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
