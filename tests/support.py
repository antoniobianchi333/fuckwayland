"""Doubles the test files share, in one place.

Not named `test_*.py` on purpose: the escape-hatch guard in
`tests/test_passthrough.py` walks the test files and would count this one,
and no runner should try to collect it. It is imported bare (`import
support`) rather than as `tests.support`, because every way the suite is
run -- `python3 -m unittest discover -s tests`, `python3 -m pytest
tests/x.py`, `python3 tests/x.py` -- puts the tests directory itself on
sys.path, and eight of the files that need it carry no repo-root bootstrap.

What lives here is only what was written more than once and byte-identical
in behaviour: the recorder uinput device and its tablet reader, the
environment context manager, the faked evdev layer, the headless sway
rig the four XWayland live files boot, and stopping a spawned input
daemon -- which three files do, all three differently and none of them
reliably (see the daemon section below). Doubles that differ between their
callers stay where they are -- `make_daemon` (three shapes of `geom`),
`FakeDaemon` (two protocols), `FakeBackend`, the compositor fakes.
"""

import contextlib
import errno
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from wdotool import keystate, uinput


# -- environment --------------------------------------------------------------

@contextlib.contextmanager
def env(**kw):
    """Set (a string) / unset (None) environment variables for the block,
    restoring exactly what was there -- including "was not set"."""
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


# -- the uinput devices, recording --------------------------------------------

class RecorderDev:
    """A uinput device that records instead of writing to the kernel.

    Every event is a plain tuple in the order it was emitted: `(etype,
    code, value)` for emit(), `("KEY", code, 0|1)` for key(), `("SYN",)`
    for syn() -- which is what the assertions in these tests read."""

    def __init__(self):
        self.events = []
        self.closed = False

    def emit(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append(("SYN",))

    def key(self, code, down):
        self.events.append(("KEY", code, 1 if down else 0))

    def close(self):
        self.closed = True


def abs_report(dev):
    """(ABS_X, ABS_Y) of the last report a recorder tablet emitted."""
    vals = {}
    for ev in dev.events:
        if ev[0] == uinput.EV_ABS:
            vals[ev[1]] = ev[2]
    return vals[uinput.ABS_X], vals[uinput.ABS_Y]


# -- the evdev layer, faked ---------------------------------------------------
#
# --clearmodifiers reads the real keyboards' key state (EVIOCGKEY) to know
# what to put back, and `keys watch` reads their event streams. A test must
# never read the *runner's* keyboard for either: it would answer differently
# depending on what the person at the keyboard happens to be holding, and
# there is no keyboard at all in a container. So keystate.Evdev -- the whole
# syscall layer -- is swapped for this.

def key_bitmap(codes) -> bytes:
    """A kernel key bitmap (EVIOCGKEY / EVIOCGBIT shape). Bytes pass
    through, so a node may carry a ready-made bitmap instead of codes."""
    if isinstance(codes, (bytes, bytearray)):
        return bytes(codes)
    buf = bytearray(keystate._KEY_BYTES)
    for c in codes:
        buf[c >> 3] |= 1 << (c & 7)
    return bytes(buf)


KEYBOARD_CAPS = key_bitmap([1, 30, 42, 100])     # Esc, a, shift, right alt
MOUSE_CAPS = key_bitmap([0x110, 0x111])          # BTN_LEFT/RIGHT only


class FakeEvdev:
    """keystate.Evdev's calls, plus read(), over scripted device nodes.

    A node is a dict: `name`, `caps` (key codes or a ready bitmap; a
    keyboard by default), `held` (the codes currently down), `data` (bytes
    read() hands out), `denied` (EACCES on open), `gone` (ENODEV on read).
    `devices` takes either that mapping, or the flat `(path, name, caps,
    held)` tuples the daemon tests write.

    `unreadable` paths are listed by paths() and raise EACCES on open,
    which is what a /dev/input/event* looks like to a uid that may not read
    it (the normal case for a desktop user -- see the module docstring of
    keystate.py). `before_read(self, nth)` runs before every key-state
    read, which is how a test makes the user let go of a key mid-sequence.
    """

    def __init__(self, devices=(), unreadable=(), before_read=None):
        if hasattr(devices, "items"):
            self.nodes = {p: dict(n) for p, n in devices.items()}
        else:
            self.nodes = {p: {"name": name, "caps": set(caps),
                              "held": set(held)}
                          for p, name, caps, held in devices}
        self.unreadable = set(unreadable)
        self.before_read = before_read
        self.reads = []          # paths whose key state was read, in order
        self.opened = []
        self.open_fds = {}
        self._next_fd = 10

    # -- keystate.Evdev interface
    def paths(self):
        return sorted(set(self.nodes) | self.unreadable)

    def open(self, path):
        node = self.nodes.get(path)
        if node is None and path not in self.unreadable:
            raise FileNotFoundError(errno.ENOENT, "no such device", path)
        if node is None or node.get("denied"):
            raise PermissionError(errno.EACCES, "Permission denied", path)
        self.opened.append(path)
        fd = self._next_fd
        self._next_fd += 1
        self.open_fds[fd] = path
        return fd

    def close(self, fd):
        self.open_fds.pop(fd, None)

    def _node(self, fd):
        return self.nodes[self.open_fds[fd]]

    def name(self, fd):
        return self._node(fd)["name"]

    def key_caps(self, fd):
        return key_bitmap(self._node(fd).get("caps", KEYBOARD_CAPS))

    def key_state(self, fd):
        path = self.open_fds[fd]
        self.reads.append(path)
        if self.before_read:
            self.before_read(self, len(self.reads))
        return key_bitmap(self.nodes[path].get("held", ()))

    def read(self, fd, n):
        node = self._node(fd)
        if node.get("gone"):
            raise OSError(errno.ENODEV, "No such device")
        data = node.get("data", b"")
        node["data"] = data[n:]
        return data[:n]

    # -- test helpers
    def press(self, path, *codes):
        self.nodes[path].setdefault("held", set()).update(codes)

    def release(self, path, *codes):
        self.nodes[path].setdefault("held", set()).difference_update(codes)


# -- the input daemon, spawned and stopped ------------------------------------
#
# Every test that spawns a real daemon stops it through these, and no test
# may spawn one any other way. A daemon nobody stops is not one stray
# process for the length of one test: its socket sits in a temporary
# runtime directory the test then deletes, and a daemon whose socket file
# is gone cannot be reached by anybody -- 161 of them were found alive at
# once on the test rig, ~3GB between them, the oldest 18 hours old.
# `wdotool/daemon.py` now notices that and exits by itself; these keep the
# suite from leaning on it.

DAEMON_ARGV = "__daemon"


def daemon_runtime_dir(pid):
    """$XDG_RUNTIME_DIR the process was started with, or None.

    /proc/<pid>/environ is the environment at exec(), which is exactly
    what the daemon computed its socket path from."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for kv in raw.split("\0"):
        if kv.startswith("XDG_RUNTIME_DIR="):
            return kv.split("=", 1)[1]
    return None


def daemon_pids(rtdir=None):
    """pids of this euid's wdotool input daemons; with `rtdir`, only the
    ones spawned into that runtime directory.

    Read out of /proc rather than matched with `pkill -f __daemon`: that
    pattern also matches the shell that runs it and anything else carrying
    the word, and a test that knows which directory it spawned into can
    simply name its own processes."""
    me = os.geteuid()
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if os.stat("/proc/%d" % pid).st_uid != me:
                continue
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue           # it exited while we were reading it
        if DAEMON_ARGV not in argv:
            continue
        if rtdir is not None and daemon_runtime_dir(pid) != rtdir:
            continue
        out.append(pid)
    return sorted(out)


def stop_daemon(pid, timeout=5.0):
    """SIGTERM, then SIGKILL, and do not return until the process is
    really gone. True when it is.

    The waiting is the point. A daemon that has been signalled but has not
    gone yet is still holding its socket, and a test that removes its
    runtime directory in between leaves exactly the unreachable daemon
    this exists to prevent. (A daemon spawned by a client is double-forked
    and reparented, so it is nobody's child and there is no zombie to wait
    for: kill(pid, 0) is the whole answer.)"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False       # not ours to kill; the caller says so
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            time.sleep(0.02)
    return False


def stop_daemons_under(rtdir, timeout=5.0):
    """Stop every daemon spawned into `rtdir`, and wait for them.

    Register it with addCleanup/addClassCleanup *before* whatever removes
    the directory (cleanups run last-in-first-out, and unittest runs
    tearDown before any of them) and before the spawn itself, so that a
    test which dies half way through its own setup still takes its daemon
    with it."""
    left = [pid for pid in daemon_pids(rtdir)
            if not stop_daemon(pid, timeout)]
    if left:
        raise AssertionError(
            "wdotool daemon(s) %s spawned into %s could not be stopped"
            % (left, rtdir))


# -- a headless sway with XWayland --------------------------------------------

class HeadlessSway:
    """A private sway on its own XDG_RUNTIME_DIR, with XWayland on.

    The four live files each booted one by hand, identically down to the
    WLR_* variables and the two 15s/10s waits. Every way it can fail to
    come up is a SkipTest here, exactly as it was there: these tests prove
    the tools against a real compositor, and a box that cannot start one
    has nothing to say about them.

    `extra_conf` is appended to the config before the line that reports
    DISPLAY. `extra_env` is merged over the environment sway is started in
    (and over `.env`, which is what a test hands to its own subprocesses);
    pass a callable to build it from the runtime directory, which only
    exists once the rig does.
    With `need_display=False` an XWayland that never announces itself is
    not fatal and `.display` comes back empty.
    """

    CONF = ("output HEADLESS-1 mode 1280x720\n"
            "xwayland enable\n"
            "default_border none\n")

    def __init__(self, prefix, extra_conf="", extra_env=None,
                 need_display=True):
        self.rtdir = tempfile.mkdtemp(prefix=prefix)
        os.chmod(self.rtdir, 0o700)
        conf = os.path.join(self.rtdir, "sway.conf")
        with open(conf, "w") as f:
            f.write(self.CONF + extra_conf
                    + "exec sh -c 'echo \"$DISPLAY\" > %s/display'\n"
                    % self.rtdir)
        self.env = dict(
            os.environ,
            XDG_RUNTIME_DIR=self.rtdir,
            WLR_BACKENDS="headless",
            WLR_LIBINPUT_NO_DEVICES="1",
            WLR_RENDERER="pixman",
            # dodge the nixpkgs sway wrapper's dbus-run-session fallback
            DBUS_SESSION_BUS_ADDRESS="unix:path=%s/no-bus" % self.rtdir,
        )
        if callable(extra_env):
            extra_env = extra_env(self.rtdir)
        self.env.update(extra_env or {})
        self.proc = subprocess.Popen(
            ["sway", "-c", conf], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sock = self._wait_socket()
        self.env["SWAYSOCK"] = self.sock
        self.display = self._wait_display()
        if self.display:
            self.env["DISPLAY"] = self.display
        elif need_display:
            self.stop()
            raise unittest.SkipTest("sway did not announce an X DISPLAY "
                                    "(xwayland enable missing?)")

    def _wait_socket(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            socks = [n for n in os.listdir(self.rtdir)
                     if n.startswith("sway-ipc.") and n.endswith(".sock")]
            if socks:
                return os.path.join(self.rtdir, socks[0])
            if self.proc.poll() is not None:
                self.stop()
                raise unittest.SkipTest("sway exited at startup")
            time.sleep(0.2)
        self.stop()
        raise unittest.SkipTest("sway did not create an IPC socket")

    def _wait_display(self):
        dfile = os.path.join(self.rtdir, "display")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with open(dfile) as f:
                    got = f.read().strip()
            except OSError:
                got = ""
            if got:
                return got
            time.sleep(0.2)
        return ""

    def wayland_display(self, default="wayland-1"):
        """The name of the Wayland socket this sway is listening on."""
        names = [n for n in os.listdir(self.rtdir)
                 if n.startswith("wayland-") and not n.endswith(".lock")]
        return names[0] if names else default

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.rtdir, ignore_errors=True)


def compositor_pids():
    """Headless compositors this user is running, as (pid, config) pairs.

    A test that boots one names its own configuration file under a temporary
    directory, which is what distinguishes it from the compositor somebody is
    actually sitting in.  Read from /proc rather than by running ps, for the
    same reason daemon_pids does: no subprocess, and nothing to parse."""
    out = []
    uid = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat("/proc/" + entry).st_uid != uid:
                continue
            with open("/proc/%s/cmdline" % entry, "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if not argv or os.path.basename(argv[0].decode("utf-8", "replace")) != "sway":
            continue
        conf = ""
        for i, a in enumerate(argv):
            if a in (b"-c", b"--config") and i + 1 < len(argv):
                conf = argv[i + 1].decode("utf-8", "replace")
        out.append((int(entry), conf))
    return out


def stop_compositor(pid, timeout=5.0):
    """SIGTERM, then SIGKILL, then wait. True when it is gone."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + timeout / 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
    return False
