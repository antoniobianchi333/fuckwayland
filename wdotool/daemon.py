"""Input-injection daemon + client.

The daemon owns the uinput devices (device creation costs ~500ms of compositor
hotplug latency — paid once), tracks the injected pointer position, and serves
one JSON object per line on a unix socket. `{"ok":true,...}` or
`{"ok":false,"error":"..."}`; a response may carry `"warnings":[...]` which the
client prints to its stderr.
"""

import fcntl
import json
import os
import socket
import sys
import threading
import time
import traceback

from wdotool import keymap, uinput
from wdotool.ctx import CmdError

# Per-euid log path: /tmp is shared, and a root-owned log must not break (or
# leak into) another user's daemon spawn.
LOG_PATH = ("/tmp/wdotool-daemon.log" if os.geteuid() == 0
            else f"/tmp/wdotool-daemon-{os.geteuid()}.log")
FALLBACK_GEOMETRY = (1920, 1080)


def socket_path() -> str:
    if os.geteuid() == 0:
        return "/run/wdotool.sock"
    rd = os.environ.get("XDG_RUNTIME_DIR")
    if rd:
        return os.path.join(rd, "wdotool.sock")
    return f"/tmp/wdotool-{os.getuid()}.sock"


def _wayland_bbox() -> tuple[int, int]:
    """Bounding box (w, h) of all outputs, queried over the Wayland wire.
    Prefers zxdg_output_manager_v1 logical size/position."""
    from wdotool import session
    from wdotool.wayland_mini import WlConn

    hit = session.find_wayland_socket()
    if hit is None:
        raise RuntimeError("no wayland socket found")
    conn = WlConn(hit[2])
    try:
        outs = []
        for name, (iface, ver) in sorted(conn.get_registry().items()):
            if iface != "wl_output":
                continue
            o = {"x": 0, "y": 0, "w": 0, "h": 0, "scale": 1, "transform": 0,
                 "lx": None, "ly": None, "lw": None, "lh": None}
            o["oid"] = conn.bind(name, "wl_output", min(ver, 2))

            def handler(op, cur, fds, o=o):
                if op == 0:  # geometry(x, y, phys_w, phys_h, subpixel, make, model, transform)
                    o["x"], o["y"] = cur.i32(), cur.i32()
                    cur.i32(), cur.i32(), cur.i32()
                    cur.string(), cur.string()
                    o["transform"] = cur.i32()
                elif op == 1:  # mode(flags, width, height, refresh)
                    flags, w, h = cur.u32(), cur.i32(), cur.i32()
                    if flags & 1:  # current
                        o["w"], o["h"] = w, h
                elif op == 3:  # scale(factor)
                    o["scale"] = cur.i32()

            conn.on(o["oid"], handler)
            outs.append(o)
        conn.roundtrip()

        mgr = conn.find_global("zxdg_output_manager_v1")
        if mgr:
            mid = conn.bind(mgr[0], "zxdg_output_manager_v1", min(mgr[1], 3))
            for o in outs:
                xid = conn.alloc()
                conn.send(mid, 1, [("u", xid), ("u", o["oid"])])  # get_xdg_output

                def xdg_handler(op, cur, fds, o=o):
                    if op == 0:  # logical_position
                        o["lx"], o["ly"] = cur.i32(), cur.i32()
                    elif op == 1:  # logical_size
                        o["lw"], o["lh"] = cur.i32(), cur.i32()

                conn.on(xid, xdg_handler)
            conn.roundtrip()

        boxes = []
        for o in outs:
            if o["lw"] and o["lh"]:
                boxes.append((o["lx"] or 0, o["ly"] or 0, o["lw"], o["lh"]))
            elif o["w"] and o["h"]:
                w, h = o["w"], o["h"]
                if o["transform"] % 2:  # 90/270 (+flipped) swap
                    w, h = h, w
                s = max(o["scale"], 1)
                boxes.append((o["x"], o["y"], w // s, h // s))
        if not boxes:
            raise RuntimeError("no wl_output geometry advertised")
        return (max(x + w for x, y, w, h in boxes), max(y + h for x, y, w, h in boxes))
    finally:
        conn.close()


_SHIFTS = (keymap.KEY_LEFTSHIFT, keymap.KEY_RIGHTSHIFT)


class _Daemon:
    # Injection rate cap. Keystrokes injected faster than the compositor
    # drains its per-open evdev buffer are lost wholesale to the kernel's
    # SYN_DROPPED — a zero-delay `type` of a few thousand characters silently
    # loses keys. Empirically ~600 keystrokes/s is drop-free on a headless
    # wlroots compositor, so floor every inter-keystroke gap at _MIN_GAP.
    # Steady (deadline-scheduled) pacing survives where bursts do not: a burst
    # fills the buffer instantly. Explicit --delay values above the floor are
    # honored unchanged.
    _MIN_GAP = 0.0018  # ~555 keystrokes/s

    def __init__(self):
        self.lock = threading.Lock()
        self.px = self.py = 0
        self.geom = None
        self.geom_warned = False
        self.kb = self.mouse = self.tablet = None
        self.dev_error = "uinput devices not initialized"
        self.down: set[int] = set()  # keycodes we injected as down
        self._next_ok = 0.0  # monotonic deadline for the next keystroke

    def _key_gap(self, delay: float):
        """Inter-keystroke pause. Sleeps `delay` seconds but never lets the
        keystroke rate exceed 1/_MIN_GAP, using an absolute deadline so the
        floor neither drifts nor accumulates syscall overhead."""
        now = time.monotonic()
        target = max(now + delay, self._next_ok)
        if target > now:
            time.sleep(target - now)
        self._next_ok = max(target, now) + self._MIN_GAP

    def create_devices(self):
        try:
            self.kb = uinput.keyboard()
            self.mouse = uinput.rel_mouse()
            self.tablet = uinput.abs_pointer()
            self.dev_error = None
            if os.environ.get("WDOTOOL_FAKE_UINPUT") != "1":
                time.sleep(0.6)  # compositor hotplug settle
        except OSError as e:
            import errno

            hint = ""
            if e.errno in (errno.EACCES, errno.EPERM):
                hint = " (wdotool injects input via /dev/uinput; run it as root)"
            elif e.errno == errno.ENOENT:
                hint = " (/dev/uinput missing; is the uinput kernel module loaded?)"
            self.dev_error = f"cannot create uinput devices: {e}{hint}"
            print(self.dev_error, file=sys.stderr, flush=True)

    def _need_devices(self):
        if self.dev_error:
            raise RuntimeError(self.dev_error)

    # -- geometry / pointer ------------------------------------------------

    def geometry(self, warnings=None):
        if self.geom:
            return self.geom
        try:
            self.geom = _wayland_bbox()
            return self.geom
        except Exception as e:
            if not self.geom_warned:
                self.geom_warned = True
                msg = (f"wdotool: cannot query Wayland output geometry ({e}); "
                       f"assuming {FALLBACK_GEOMETRY[0]}x{FALLBACK_GEOMETRY[1]}")
                print(msg, file=sys.stderr, flush=True)
                if warnings is not None:
                    warnings.append(msg)
            return FALLBACK_GEOMETRY

    def op_mousemove_abs(self, x, y, warnings):
        self._need_devices()
        w, h = self.geometry(warnings)
        x = min(max(x, 0), w - 1)
        y = min(max(y, 0), h - 1)
        self.tablet.emit(uinput.EV_ABS, uinput.ABS_X, x * 32767 // max(w - 1, 1))
        self.tablet.emit(uinput.EV_ABS, uinput.ABS_Y, y * 32767 // max(h - 1, 1))
        self.tablet.syn()
        self.px, self.py = x, y

    def op_mousemove_rel(self, dx, dy, warnings):
        self._need_devices()
        w, h = self.geometry(warnings)
        self.px = min(max(self.px + dx, 0), w - 1)
        self.py = min(max(self.py + dy, 0), h - 1)
        if dx:
            self.mouse.emit(uinput.EV_REL, uinput.REL_X, dx)
        if dy:
            self.mouse.emit(uinput.EV_REL, uinput.REL_Y, dy)
        self.mouse.syn()

    # -- buttons -----------------------------------------------------------

    # X11 button numbering: libinput/XWayland map BTN_SIDE..BTN_TASK to 8..12.
    _BTN = {1: uinput.BTN_LEFT, 2: uinput.BTN_MIDDLE, 3: uinput.BTN_RIGHT,
            8: uinput.BTN_SIDE, 9: uinput.BTN_EXTRA, 10: uinput.BTN_FORWARD,
            11: uinput.BTN_BACK, 12: uinput.BTN_TASK}
    _WHEEL = {4: (uinput.REL_WHEEL, 1), 5: (uinput.REL_WHEEL, -1),
              6: (uinput.REL_HWHEEL, -1), 7: (uinput.REL_HWHEEL, 1)}

    def op_button(self, btn, down):
        self._need_devices()
        if btn in self._BTN:
            self.mouse.key(self._BTN[btn], down)
        elif btn in self._WHEEL:
            if down:  # wheel "buttons" are one detent per press; release is a no-op
                rel, value = self._WHEEL[btn]
                self.mouse.emit(uinput.EV_REL, rel, value)
                self.mouse.syn()
        else:
            raise RuntimeError(f"invalid mouse button {btn}")

    def op_click(self, btn, repeat, delay_ms):
        # xdo_click_window_multiple: 12ms between down/up, then `delay` after
        # every click (including the last one).
        for _ in range(repeat):
            self.op_button(btn, True)
            time.sleep(0.012)
            self.op_button(btn, False)
            time.sleep(delay_ms / 1000)

    # -- keyboard ----------------------------------------------------------

    def _press(self, keys, delay):
        for code, shifted in keys:
            if shifted and not any(s in self.down for s in _SHIFTS):
                self.kb.key(keymap.KEY_LEFTSHIFT, True)
                self.down.add(keymap.KEY_LEFTSHIFT)
            self.kb.key(code, True)
            self.down.add(code)
            self._key_gap(delay)

    def _release(self, keys, delay):
        for code, shifted in keys:
            if shifted and keymap.KEY_LEFTSHIFT in self.down:
                self.kb.key(keymap.KEY_LEFTSHIFT, False)
                self.down.discard(keymap.KEY_LEFTSHIFT)
            self.kb.key(code, False)
            self.down.discard(code)
            self._key_gap(delay)

    def op_key(self, spec, direction, delay_ms, clearmods):
        self._need_devices()
        if clearmods:
            for code in keymap.MODIFIER_KEYCODES:
                self.kb.key(code, False)
                self.down.discard(code)
        keys, warnings = keymap.parse_keyseq(spec)  # ValueError on bad sequence
        d = delay_ms / 1000
        if direction == "press":
            self._press(keys, d / 2)
            self._release(keys, d / 2)
        elif direction == "down":
            self._press(keys, d)
        elif direction == "up":
            self._release(keys, d)
        else:
            raise RuntimeError(f"invalid key direction {direction!r}")
        return warnings

    def op_type(self, text, delay_ms, clearmods):
        self._need_devices()
        warnings = []
        if clearmods:
            for code in keymap.MODIFIER_KEYCODES:
                self.kb.key(code, False)
                self.down.discard(code)
        # xdo_enter_text_window: delay split between down and up, down capped at 50ms
        down_d = min(delay_ms / 2, 50) / 1000
        up_d = delay_ms / 1000 - down_d
        for ch in text:
            hit = keymap.char_to_key(ch)
            if hit is None:
                warnings.append(f"Can't type character '{ch}' (not on the US layout). Skipping.")
                continue
            code, shifted = hit
            synth_shift = shifted and not any(s in self.down for s in _SHIFTS)
            if synth_shift:
                self.kb.key(keymap.KEY_LEFTSHIFT, True)
            self.kb.key(code, True)
            if down_d > 0:
                time.sleep(down_d)
            self.kb.key(code, False)
            if synth_shift:
                self.kb.key(keymap.KEY_LEFTSHIFT, False)
            self._key_gap(up_d)
        return warnings

    # -- protocol ----------------------------------------------------------

    def handle(self, req: dict) -> dict:
        op = req.get("op")
        warnings: list[str] = []
        with self.lock:
            if op == "type":
                warnings = self.op_type(req["text"], req.get("delay_ms", 12),
                                        req.get("clearmods", False))
            elif op == "key":
                warnings = self.op_key(req["spec"], req.get("direction", "press"),
                                       req.get("delay_ms", 12), req.get("clearmods", False))
            elif op == "mousemove_abs":
                self.op_mousemove_abs(int(req["x"]), int(req["y"]), warnings)
            elif op == "mousemove_rel":
                self.op_mousemove_rel(int(req["dx"]), int(req["dy"]), warnings)
            elif op == "button":
                self.op_button(int(req["btn"]), bool(req["down"]))
            elif op == "click":
                self.op_click(int(req["btn"]), int(req.get("repeat", 1)),
                              int(req.get("delay_ms", 100)))
            elif op == "pointer":
                return {"ok": True, "x": self.px, "y": self.py}
            elif op == "geometry":
                w, h = self.geometry(warnings)
                return {"ok": True, "w": w, "h": h, "warnings": warnings}
            elif op == "ping":
                return {"ok": True, "pid": os.getpid()}
            else:
                return {"ok": False, "error": f"unknown op {op!r}"}
        return {"ok": True, "warnings": warnings}

    def serve_client(self, conn: socket.socket):
        rfile = conn.makefile("r", encoding="utf-8")
        try:
            for line in rfile:
                if not line.strip():
                    continue
                try:
                    resp = self.handle(json.loads(line))
                except (ValueError, RuntimeError, OSError, KeyError) as e:
                    resp = {"ok": False, "error": str(e) or repr(e)}
                conn.sendall((json.dumps(resp) + "\n").encode())
        except OSError:
            pass
        finally:
            try:
                rfile.close()
                conn.close()
            except OSError:
                pass


def daemon_main() -> int:
    path = socket_path()
    # Startup lock, held for the daemon's lifetime: losers of a concurrent
    # spawn race must never unlink the winner's freshly-bound socket.
    lock_fd = os.open(path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        print(f"wdotool daemon already running/starting on {path}",
              file=sys.stderr, flush=True)
        return 0

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
        probe.close()
        os.close(lock_fd)
        print(f"wdotool daemon already running on {path}", file=sys.stderr, flush=True)
        return 0
    except OSError:
        probe.close()
        try:
            os.unlink(path)  # stale socket from a killed daemon
        except OSError:
            pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(16)
    print(f"wdotool daemon (pid {os.getpid()}) listening on {path}", flush=True)

    d = _Daemon()
    d.create_devices()
    try:
        while True:
            conn, _addr = srv.accept()
            threading.Thread(target=d.serve_client, args=(conn,), daemon=True).start()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        for dev in (d.kb, d.mouse, d.tablet):
            if dev is not None:
                dev.close()
    return 0


class DaemonClient:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._rfile = sock.makefile("r", encoding="utf-8")

    @classmethod
    def connect_or_spawn(cls) -> "DaemonClient":
        path = socket_path()
        sock = cls._try_connect(path)
        if sock is None:
            # A stale socket file is handled by the daemon itself (it unlinks
            # under the startup lock); unlinking here would race a daemon that
            # just bound the path.
            cls._spawn()
            deadline = time.monotonic() + 2.0
            while sock is None and time.monotonic() < deadline:
                time.sleep(0.05)
                sock = cls._try_connect(path)
            if sock is None:
                raise CmdError(f"cannot start wdotool daemon (see {LOG_PATH})")
        return cls(sock)

    @staticmethod
    def _try_connect(path):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except OSError:
            sock.close()
            return None

    @staticmethod
    def _spawn():
        """Daemonize: fork, setsid, fork; grandchild redirects stdio to the
        daemon log, then re-execs as `<wdotool> __daemon` when argv[0] is the
        wdotool/xdotool executable (so ps shows a predictable name and
        `pkill -f __daemon` works), else runs daemon_main() in-process."""
        pid = os.fork()
        if pid:
            os.waitpid(pid, 0)
            return
        code = 1
        try:
            os.setsid()
            if os.fork():
                os._exit(0)  # session leader exits; grandchild is the daemon
            sys.stdout.flush()
            sys.stderr.flush()
            null = os.open(os.devnull, os.O_RDONLY)
            try:
                # O_NOFOLLOW: never append through a planted symlink in /tmp.
                log = os.open(LOG_PATH,
                              os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                              0o644)
            except OSError:
                log = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 0)
            os.dup2(log, 1)
            os.dup2(log, 2)
            os.close(null)
            os.close(log)
            exe = sys.argv[0] if sys.argv else ""
            if (os.path.basename(exe) in ("wdotool", "xdotool")
                    and os.path.isfile(exe) and os.access(exe, os.X_OK)):
                try:
                    os.execv(exe, [exe, "__daemon"])
                except OSError:
                    pass  # fall back to running the daemon in this process
            code = daemon_main()
        except BaseException:
            traceback.print_exc()
        finally:
            os._exit(code)

    def close(self):
        try:
            self._rfile.close()
            self._sock.close()
        except OSError:
            pass

    def _rpc(self, **req):
        try:
            self._sock.sendall((json.dumps(req) + "\n").encode())
            line = self._rfile.readline()
        except OSError as e:
            raise CmdError(f"wdotool daemon connection lost: {e}") from None
        if not line:
            raise CmdError("wdotool daemon connection lost")
        resp = json.loads(line)
        for warning in resp.get("warnings") or []:
            print(warning, file=sys.stderr)
        if not resp.get("ok"):
            raise CmdError(resp.get("error", "wdotool daemon error"))
        return resp

    def type_text(self, text: str, delay_ms: int):
        self._rpc(op="type", text=text, delay_ms=delay_ms)

    def key(self, spec: str, direction: str, delay_ms: int, clearmods: bool):
        self._rpc(op="key", spec=spec, direction=direction, delay_ms=delay_ms,
                  clearmods=clearmods)

    def mousemove_abs(self, x: int, y: int):
        self._rpc(op="mousemove_abs", x=x, y=y)

    def mousemove_rel(self, dx: int, dy: int):
        self._rpc(op="mousemove_rel", dx=dx, dy=dy)

    def button(self, btn: int, down: bool):
        self._rpc(op="button", btn=btn, down=down)

    def click(self, btn: int, repeat: int, delay_ms: int):
        self._rpc(op="click", btn=btn, repeat=repeat, delay_ms=delay_ms)

    def pointer(self) -> tuple[int, int]:
        resp = self._rpc(op="pointer")
        return (resp["x"], resp["y"])

    def geometry(self) -> tuple[int, int]:
        resp = self._rpc(op="geometry")
        return (resp["w"], resp["h"])
