"""OWNER: wxrandr builder. --brightness/--gamma via zwlr_gamma_control_manager_v1.

The Wayland gamma control dies with its client connection, so a non-identity
brightness/gamma forks a tiny detached holder process per output (the wdotool
daemon double-fork, simplified) that acquires the gamma control, submits the
ramp, and then just keeps the connection alive. Changing brightness kills and
replaces the holder; identity (brightness 1, gamma 1:1:1) just kills it — the
compositor restores the neutral ramp when the control's client disconnects.

Ramp math is xrandr's set_gamma() (xrandr.c:1419):
    ramp[i] = min((i/(size-1))^(1/gamma) * brightness, 1.0) * 65535
with the linear shortcut when gamma == 1 and brightness == 1.

Holder liveness is tracked in the wxrandr state file as (pid, starttime) —
starttime from /proc/pid/stat guards against pid reuse."""

import os
import signal
import struct
import sys
import time

MANAGER = "zwlr_gamma_control_manager_v1"


def compute_ramp(size: int, brightness: float, gamma_rgb) -> bytes:
    """The three channel ramps, red then green then blue, native-endian u16
    (the zwlr_gamma_control fd format)."""
    out = bytearray()
    for g in gamma_rgb:
        shift = 1.0 / g if g else 1.0
        for i in range(size):
            frac = i / (size - 1) if size > 1 else 0.0
            if g == 1.0 and brightness == 1.0:
                v = frac
            else:
                v = min(pow(frac, shift) * brightness, 1.0)
            out += struct.pack("=H", int(v * 65535.0))
    return bytes(out)


def _proc_starttime(pid: int) -> str | None:
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        # field 22, counting from 1; comm (field 2) may contain spaces —
        # everything after the closing paren is space-separated.
        after = data[data.rindex(b")") + 2:].split()
        return after[19].decode()  # 22 - 2 (pid, comm)
    except (OSError, ValueError, IndexError):
        return None


def stop_holder(state, output: str) -> bool:
    """Kill the recorded holder for `output` (verifying starttime so a
    recycled pid is never killed). Returns True if one was running."""
    rec = state.gamma().pop(output, None)
    if not rec or not rec.get("pid"):
        return False
    pid = rec["pid"]
    if _proc_starttime(pid) != rec.get("start"):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(50):
        if _proc_starttime(pid) != rec.get("start"):
            return True
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True


def set_output_gamma(state, output: str, brightness: float, gamma_rgb,
                     wayland_socket: str | None = None) -> str | None:
    """Kill any existing holder, then (unless identity) spawn a fresh one.
    Returns None on success, else an error string ("refused" when the
    compositor rejected the gamma control — headless outputs have no LUT)."""
    stop_holder(state, output)
    identity = (brightness == 1.0 and tuple(gamma_rgb) == (1.0, 1.0, 1.0))
    if identity:
        return None
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: detach, grandchild holds the gamma control
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
            holder_main(output, brightness, gamma_rgb, status_fd=w,
                        wayland_socket=wayland_socket)
        finally:
            os._exit(0)
    os.close(w)
    os.waitpid(pid, 0)
    status = b""
    deadline = time.monotonic() + 10.0
    import select
    while b"\n" not in status and time.monotonic() < deadline:
        ready, _, _ = select.select([r], [], [], 0.2)
        if not ready:
            continue
        chunk = os.read(r, 4096)
        if not chunk:
            break
        status += chunk
    os.close(r)
    line = status.decode(errors="replace").strip()
    if line.startswith("ok "):
        _, hpid, start = line.split()
        state.gamma()[output] = {
            "pid": int(hpid), "start": start,
            "brightness": brightness, "gamma": list(gamma_rgb)}
        return None
    if line.startswith("failed "):
        return line[len("failed "):]
    return "gamma holder did not report status"


def holder_main(output: str, brightness: float, gamma_rgb,
                status_fd: int | None = None,
                wayland_socket: str | None = None):
    """The detached holder: acquire the output's gamma control, submit the
    ramp, report over status_fd, then keep the connection alive forever.
    Exits when the compositor closes the connection or on SIGTERM."""

    def report(msg: str):
        if status_fd is not None:
            try:
                os.write(status_fd, (msg + "\n").encode())
                os.close(status_fd)
            except OSError:
                pass

    try:
        from wdotool import session
        from wdotool.wayland_mini import WlConn
        if wayland_socket is None:
            hit = session.find_wayland_socket()
            if hit is None:
                report("failed no wayland socket")
                return 1
            wayland_socket = hit[2]
        conn = WlConn(wayland_socket)
        reg = conn.get_registry()
        mgr = conn.find_global(MANAGER)
        if mgr is None:
            report("failed compositor does not advertise %s" % MANAGER)
            return 1
        target = None
        pending = []
        for name, (iface, ver) in sorted(reg.items()):
            if iface != "wl_output":
                continue
            oid = conn.bind(name, "wl_output", min(ver, 4))
            info = {"oid": oid, "name": None}

            def h(op, cur, fds, info=info):
                if op == 4:  # name event (wl_output v4)
                    info["name"] = cur.string()

            conn.on(oid, h)
            pending.append(info)
        conn.roundtrip()
        for info in pending:
            if info["name"] == output:
                target = info["oid"]
        if target is None:
            report("failed no wl_output named %s" % output)
            return 1
        mid = conn.bind(mgr[0], MANAGER, 1)
        gid = conn.alloc()
        st = {"size": None, "failed": False}

        def gh(op, cur, fds):
            if op == 0:
                st["size"] = cur.u32()
            elif op == 1:
                st["failed"] = True

        conn.on(gid, gh)
        conn.send(mid, 0, [("u", gid), ("u", target)])  # get_gamma_control
        conn.roundtrip()
        if st["failed"] or not st["size"]:
            report("failed refused")
            return 1

        def submit():
            ramp = compute_ramp(st["size"], brightness, gamma_rgb)
            fd = os.memfd_create("wxrandr-gamma")
            try:
                os.write(fd, ramp)
                os.lseek(fd, 0, os.SEEK_SET)
                conn.send_fds(gid, 0, [], [fd])  # set_gamma(fd)
            finally:
                os.close(fd)

        submit()
        conn.roundtrip()
        if st["failed"]:
            report("failed refused")
            return 1
        report("ok %d %s" % (os.getpid(), _proc_starttime(os.getpid()) or "?"))
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        while True:  # a compositor may resend gamma_size; resubmit then
            before = st["size"]
            conn.dispatch(timeout=60.0)
            if st["failed"]:
                return 0
            if st["size"] != before:
                submit()
    except Exception as e:
        report("failed %s" % e)
        return 1
