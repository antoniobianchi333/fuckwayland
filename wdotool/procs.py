"""OWNER: wdotool. Detached children, and the /proc facts that outlive them.

Two of these tools start a process that must outlive the command that
started it: the gamma holder that keeps a zwlr_gamma_control alive for as
long as a brightness is set, and the supervisor that owns one screen-copy
helper. What they share is not a spawn but a protocol, because of what it
promises:

  * double-fork + setsid, so nothing is left in our process group and the
    child survives the shell -- and the terminal -- that started it;
  * the child writes `pid <pid> <starttime>` up a status pipe BEFORE it can
    fail, and the parent acts on that line the moment it arrives. Not when
    the start finishes: a start that hangs, or that the user interrupts
    halfway through, must still leave a record naming a process something
    later can stop. An orphan nobody can end is the one outcome this
    protocol exists to prevent, and buffering the lines would produce it;
  * liveness is (pid, starttime) read out of /proc, so a recycled pid is
    never mistaken for the process we started, and nothing is signalled
    that is not ours (the euid check);
  * every kill is bounded -- SIGTERM, wait, SIGKILL, confirm -- and never
    fire-and-forget.

What a status line means is the caller's business: this module knows only
which line ends the start. Standard library only, like everything under
wdotool/.
"""

import os
import select
import signal
import time


# -- identity -----------------------------------------------------------------

def proc_starttime(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat, the half of an identity that a pid
    alone does not give: pids are recycled, (pid, starttime) pairs are not.
    None when the process is gone (or /proc is not there to ask)."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        # field 22, counting from 1; comm (field 2) may contain spaces —
        # everything after the closing paren is space-separated.
        after = data[data.rindex(b")") + 2:].split()
        return after[19].decode()  # 22 - 2 (pid, comm)
    except (OSError, ValueError, IndexError):
        return None


def comm(pid: int) -> str | None:
    """/proc/<pid>/comm, the executable name, or None."""
    try:
        with open("/proc/%d/comm" % pid) as f:
            return f.read().strip()
    except OSError:
        return None


def zombie(pid: int) -> bool:
    """Has that process already exited, with only its exit status left?

    /proc still has the directory, the uid and the start time of a zombie,
    so every other test here says it is alive -- and a query would report a
    running child that had exited, a stop would report stopping it. The
    state letter is the only thing that tells them apart."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            data = f.read()
    except OSError:
        return False
    try:                    # comm is parenthesised and may contain spaces
        return data.rsplit(")", 1)[1].split()[0] == "Z"
    except IndexError:
        return False


def owned_by_us(pid: int) -> bool:
    """A process we started runs as us. Anything else is never signalled,
    whatever a state file claims. (Under sudo "us" is root, and what root
    forked is root too.)"""
    try:
        return os.stat("/proc/%d" % pid).st_uid == os.geteuid()
    except OSError:
        return False


def alive(pid, start, comm_hint=None) -> bool:
    """Is that exact process still running?

    With a starttime the answer is exact. Without one (a '?' record, written
    when /proc could not be read) we fall back to the process name against
    `comm_hint` -- one string or several: never matching would strand a
    child that is holding something, with no way left to stop it."""
    if not pid:
        return False
    if not owned_by_us(pid):
        return False
    cur = proc_starttime(pid)
    if cur is None:
        return False
    if zombie(pid):
        return False
    if start and start != "?":
        return cur == start
    name = comm(pid)
    hints = ((comm_hint,) if isinstance(comm_hint, str)
             else tuple(comm_hint or ()))
    return bool(name and any(h.lower() in name.lower() for h in hints))


# -- ending -------------------------------------------------------------------

def wait_gone(pid: int, start, tries: int = 50) -> bool:
    """Poll (bounded) until `pid` is gone / recycled. With a real starttime
    we detect recycle too; for a '?' record we can only watch for
    disappearance."""
    for _ in range(tries):
        cur = proc_starttime(pid)
        if cur is None or (start != "?" and cur != start):
            return True
        time.sleep(0.02)
    return False


def kill_bounded(pid: int, start=None) -> bool:
    """SIGTERM, bounded wait, SIGKILL, confirm. Never fire-and-forget."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    if wait_gone(pid, start if start else "?"):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return True
    wait_gone(pid, start if start else "?")   # confirm the SIGKILL took
    return True


# -- the status pipe ----------------------------------------------------------

def emit(status_fd, msg: str, close: bool = False):
    """One line from a detached child to whoever started it. Never raises:
    the reader is a command that may have gone away already, and a child
    that died of its own status pipe is exactly the orphan this module is
    here to prevent."""
    if status_fd is None:
        return
    try:
        os.write(status_fd, (msg + "\n").encode())
        if close:
            os.close(status_fd)
    except OSError:
        pass


def spawn_detached(child_main, seconds: float, on_line) -> str | None:
    """Fork a detached grandchild running `child_main(status_fd)`, then read
    its status pipe for up to `seconds` and return the line that ended the
    start (None if none came in time, or the pipe closed first).

    Every line is handed to `on_line` as it arrives, and the first line
    `on_line` does not claim (does not return true for) is that terminal
    line. The caller's `on_line` is therefore where a child's identity is
    written down, while the start is still running -- see the module
    docstring: that timing is the whole point."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:                  # child: detach, the grandchild is the work
        try:
            os.close(r)
            os.setsid()
            if os.fork():
                os._exit(0)
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.close(devnull)
            child_main(w)
        finally:
            os._exit(0)
    os.close(w)
    os.waitpid(pid, 0)            # the middle process, already exiting
    deadline = time.monotonic() + seconds
    buf = b""
    status = None
    try:
        while status is None and time.monotonic() < deadline:
            ready, _, _ = select.select([r], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(r, 4096)
            except OSError:
                break
            if not chunk:         # the child closed it: no more lines coming
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode(errors="replace").strip()
                if not on_line(line):
                    status = line
                    break
    finally:
        os.close(r)               # also on the Ctrl-C that gets us here
    return status
