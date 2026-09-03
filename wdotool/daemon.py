"""Input-injection daemon + client.

The daemon owns the uinput devices (device creation costs ~500ms of compositor
hotplug latency — paid once), tracks the injected pointer position, and serves
one JSON object per line on a unix socket. `{"ok":true,...}` or
`{"ok":false,"error":"..."}`; a response may carry `"warnings":[...]` which the
client prints to its stderr.

The daemon owns the pointer *model* (px, py): the position it last injected.
It is only a model -- REL events, a physical mouse, or another daemon move
the compositor's pointer behind its back -- so clients that can ask the
compositor for the real position (the GNOME bridge's GetPointer) push it
back with the `seed_pointer` op before a relative move (see B1/B6 in
DESIGN.md).
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
FALLBACK_GEOMETRY = (0, 0, 1920, 1080)  # (min_x, min_y, width, height)

# Per-request sanity bounds: one malicious/buggy request must not hold the
# global injection lock for hours or overflow C int fields downstream.
MAX_REPEAT = 1_000_000
MAX_DELAY_MS = 300_000
_I32_MIN, _I32_MAX = -(2**31), 2**31 - 1
_MAX_REQUEST = 16 << 20  # bytes per request line


def _num(val, what: str, lo: int, hi: int) -> int:
    """Validate one numeric request field: JSON numbers only (bool excluded),
    truncated to int, bounds-checked. Raises RuntimeError -> {"ok":false}."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise RuntimeError(f"invalid {what}: {val!r} (expected a number)")
    v = int(val)
    if not lo <= v <= hi:
        raise RuntimeError(f"{what} {v} out of range [{lo}, {hi}]")
    return v


def _text(val, what: str) -> str:
    if not isinstance(val, str):
        raise RuntimeError(f"invalid {what}: {val!r} (expected a string)")
    return val


def socket_path() -> str:
    if os.geteuid() == 0:
        return "/run/wdotool.sock"
    rd = os.environ.get("XDG_RUNTIME_DIR")
    if rd:
        return os.path.join(rd, "wdotool.sock")
    return f"/tmp/wdotool-{os.getuid()}.sock"


def _bbox_of(boxes) -> tuple[int, int, int, int]:
    """(min_x, min_y, width, height) of a list of (x, y, w, h) output boxes.
    Multi-output layouts can have non-zero or negative origins."""
    minx = min(x for x, _y, _w, _h in boxes)
    miny = min(y for _x, y, _w, _h in boxes)
    maxx = max(x + w for x, _y, w, _h in boxes)
    maxy = max(y + h for _x, y, _w, h in boxes)
    return (minx, miny, maxx - minx, maxy - miny)


def _wayland_bbox() -> tuple[int, int, int, int]:
    """Bounding box (min_x, min_y, w, h) of all outputs, queried over the
    Wayland wire. Prefers zxdg_output_manager_v1 logical size/position."""
    from wdotool import session
    from wdotool.wayland_mini import WlConn

    hit = session.find_wayland_socket()
    if hit is None:
        raise RuntimeError("no wayland socket found")
    conn = WlConn(hit[2])
    # A wedged compositor must fall back to the warned default, not hang the
    # daemon forever while it holds the global lock.
    conn.sock.settimeout(3.0)
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
        return _bbox_of(boxes)
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
        # Last (ABS_X, ABS_Y) written to the tablet; a fresh uinput device
        # starts at 0,0, which is what the kernel will compare against.
        self._last_abs = (0, 0)
        self._rel_abs = None      # relative moves as absolute warps? (B1)
        self.pos_known = False    # has px/py ever been established? (B6)
        self.geom_fallback = False  # last geometry() used FALLBACK_GEOMETRY

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
            self._last_abs = (0, 0)  # fresh device: kernel axis state is 0,0
            self.dev_error = None
            if os.environ.get("WDOTOOL_FAKE_UINPUT") != "1":
                time.sleep(0.6)  # compositor hotplug settle
        except OSError as e:
            import errno

            # A partial set (e.g. keyboard created, mouse raced EPERM) must not
            # leak devices: close what exists so a later retry starts clean.
            for dev in (self.kb, self.mouse, self.tablet):
                if dev is not None:
                    dev.close()
            self.kb = self.mouse = self.tablet = None
            hint = ""
            if e.errno in (errno.EACCES, errno.EPERM):
                hint = " (wdotool injects input via /dev/uinput; run it as root)"
            elif e.errno == errno.ENOENT:
                hint = " (/dev/uinput missing; is the uinput kernel module loaded?)"
            err = f"cannot create uinput devices: {e}{hint}"
            if err != self.dev_error:  # don't spam the log on every retry
                print(err, file=sys.stderr, flush=True)
            self.dev_error = err

    def _need_devices(self):
        if self.dev_error:
            # Retry: the failure may be transient (uinput module loaded after
            # boot, devices raced). Cheap when it fails again — the 600ms
            # hotplug settle is only paid on success.
            self.create_devices()
        if self.dev_error:
            raise RuntimeError(self.dev_error)

    # -- geometry / pointer ------------------------------------------------

    def geometry(self, warnings=None) -> tuple[int, int, int, int]:
        """Layout bounding box (min_x, min_y, w, h), re-read from the
        compositor on every call so output-layout changes (wxrandr, hotplug)
        are visible immediately; the last good reading serves as the fallback
        when the compositor can't be queried. The origin can be
        non-zero/negative on multi-output layouts; pointer coordinates are
        tracked in these global layout coordinates."""
        try:
            self.geom = _wayland_bbox()
            self.geom_fallback = False
            return self.geom
        except Exception as e:
            if self.geom:
                self.geom_fallback = False
                return self.geom
            self.geom_fallback = True
            if not self.geom_warned:
                self.geom_warned = True
                msg = (f"wdotool: cannot query Wayland output geometry ({e}); "
                       f"assuming {FALLBACK_GEOMETRY[2]}x{FALLBACK_GEOMETRY[3]}")
                print(msg, file=sys.stderr, flush=True)
                if warnings is not None:
                    warnings.append(msg)
            return FALLBACK_GEOMETRY

    @staticmethod
    def _axis(delta: int, span: int) -> int:
        """Layout offset -> tablet axis value (B7).

        libinput maps an absolute axis with scale_axis(): the pointer lands at
        `value * span / (max - min + 1)` = `v * span / 32768`, which the
        compositor then floors (or rounds) to a pixel. The forward map that
        round-trips through that inverse exactly is the CEILING of
        `delta * 32768 / span`, not a floor: floor(ceil(d*32768/S) * S/32768)
        == d for every d in [0, S) while S <= 32768. The old
        `d * 32767 // (S - 1)` floor map landed one pixel short wherever the
        division was inexact -- 257 of 301 x values near the layout origin on
        a 5760px-wide three-head rig."""
        span = max(span, 1)
        return min(max(-((-delta * 32768) // span), 0), 32767)

    def _warp(self, x, y, gx, gy, w, h):
        """Put the pointer at the global layout coordinate (x, y) with the
        absolute tablet. The compositor maps the tablet's axes across the FULL
        output layout, so scale the offset from the layout origin, not the raw
        (possibly negative) global coordinate."""
        ax = self._axis(x - gx, w)
        ay = self._axis(y - gy, h)
        # B2: the kernel drops an EV_ABS whose value equals the axis' current
        # value, so re-sending the coordinates the tablet reported last time
        # is a silent no-op -- and the pointer may well have moved since, via
        # REL events, a physical mouse, or another daemon. Nudge one axis by a
        # single unit first (1/32768 of the layout: sub-pixel on any real
        # screen) so the second report is always a change and always lands.
        if (ax, ay) == self._last_abs:
            nudge = ax + 1 if ax < 32767 else ax - 1
            self.tablet.emit(uinput.EV_ABS, uinput.ABS_X, nudge)
            self.tablet.syn()
        self.tablet.emit(uinput.EV_ABS, uinput.ABS_X, ax)
        self.tablet.emit(uinput.EV_ABS, uinput.ABS_Y, ay)
        self.tablet.syn()
        self._last_abs = (ax, ay)
        self.px, self.py = x, y
        self.pos_known = True

    def _rel_absolute(self) -> bool:
        """Should a relative move be emitted as an absolute warp? (B1)

        REL_X/REL_Y go through the compositor's pointer-acceleration curve, so
        `mousemove_relative 500 0` lands wherever libinput's profile puts it:
        on a stock GNOME (adaptive profile) 500 requested pixels moved the
        pointer 462 on 24.04 and 267 on 26.04, and only `accel-profile flat`
        was exact. xdotool's XWarpPointer is pixel-exact, so everywhere but
        sway/i3 the relative move is emitted as an absolute warp to
        (px+dx, py+dy) instead.

        sway/i3 keep the REL path: this repo's sway rig runs `pointer_accel 0`
        (REL is already exact there) and the sway/daemon tests pin the EV_REL
        contract. WDOTOOL_REL_MODE=rel|abs forces either, on any compositor."""
        if self._rel_abs is None:
            mode = os.environ.get("WDOTOOL_REL_MODE", "").strip().lower()
            if mode in ("abs", "absolute", "warp"):
                self._rel_abs = True
            elif mode in ("rel", "relative"):
                self._rel_abs = False
            else:
                from wdotool import session
                self._rel_abs = not bool(session.find_sway_socket())
        return self._rel_abs

    def op_mousemove_abs(self, x, y, warnings):
        self._need_devices()
        gx, gy, w, h = self.geometry(warnings)
        x = min(max(x, gx), gx + w - 1)
        y = min(max(y, gy), gy + h - 1)
        self._warp(x, y, gx, gy, w, h)

    def op_mousemove_rel(self, dx, dy, warnings):
        self._need_devices()
        gx, gy, w, h = self.geometry(warnings)
        tx = min(max(self.px + dx, gx), gx + w - 1)
        ty = min(max(self.py + dy, gy), gy + h - 1)
        if self._rel_absolute():
            self._warp(tx, ty, gx, gy, w, h)
            return
        self.px, self.py = tx, ty
        self.pos_known = True
        if dx:
            self.mouse.emit(uinput.EV_REL, uinput.REL_X, dx)
        if dy:
            self.mouse.emit(uinput.EV_REL, uinput.REL_Y, dy)
        self.mouse.syn()

    def op_seed_pointer(self, x, y, warnings):
        """Adopt the compositor's real pointer position (B6). No injection:
        the client has just asked the compositor where the pointer is and is
        correcting the model before a relative move or a getmouselocation."""
        gx, gy, w, h = self.geometry(warnings)
        self.px = min(max(x, gx), gx + w - 1)
        self.py = min(max(y, gy), gy + h - 1)
        self.pos_known = True

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

    def handle(self, req) -> dict:
        if not isinstance(req, dict):
            return {"ok": False, "error": f"invalid request: {req!r} (expected an object)"}
        op = req.get("op")
        warnings: list[str] = []
        with self.lock:
            if op == "type":
                warnings = self.op_type(
                    _text(req.get("text"), "text"),
                    _num(req.get("delay_ms", 12), "delay_ms", 0, MAX_DELAY_MS),
                    req.get("clearmods", False))
            elif op == "key":
                warnings = self.op_key(
                    _text(req.get("spec"), "spec"),
                    req.get("direction", "press"),
                    _num(req.get("delay_ms", 12), "delay_ms", 0, MAX_DELAY_MS),
                    req.get("clearmods", False))
            elif op == "mousemove_abs":
                self.op_mousemove_abs(_num(req.get("x"), "x", _I32_MIN, _I32_MAX),
                                      _num(req.get("y"), "y", _I32_MIN, _I32_MAX),
                                      warnings)
            elif op == "mousemove_rel":
                self.op_mousemove_rel(_num(req.get("dx"), "dx", _I32_MIN, _I32_MAX),
                                      _num(req.get("dy"), "dy", _I32_MIN, _I32_MAX),
                                      warnings)
            elif op == "button":
                self.op_button(_num(req.get("btn"), "button", 0, 255),
                               bool(req.get("down")))
            elif op == "click":
                self.op_click(_num(req.get("btn"), "button", 0, 255),
                              _num(req.get("repeat", 1), "repeat", 0, MAX_REPEAT),
                              _num(req.get("delay_ms", 100), "delay_ms", 0, MAX_DELAY_MS))
            elif op == "seed_pointer":
                self.op_seed_pointer(_num(req.get("x"), "x", _I32_MIN, _I32_MAX),
                                     _num(req.get("y"), "y", _I32_MIN, _I32_MAX),
                                     warnings)
            elif op == "pointer":
                # B6: never answer "0,0" for a daemon that has no pointer.
                # A daemon that could not open /dev/uinput has injected
                # nothing and knows nothing; it must fail with that reason
                # instead of reporting the origin with rc 0.
                if not self.pos_known:
                    self._need_devices()
                return {"ok": True, "x": self.px, "y": self.py,
                        "known": self.pos_known}
            elif op == "geometry":
                gx, gy, w, h = self.geometry(warnings)
                return {"ok": True, "x": gx, "y": gy, "w": w, "h": h,
                        "fallback": self.geom_fallback, "warnings": warnings}
            elif op == "ping":
                return {"ok": True, "pid": os.getpid()}
            else:
                return {"ok": False, "error": f"unknown op {op!r}"}
        return {"ok": True, "warnings": warnings}

    def serve_client(self, conn: socket.socket):
        rfile = conn.makefile("r", encoding="utf-8")
        try:
            while True:
                line = rfile.readline(_MAX_REQUEST + 1)
                if not line:
                    break
                if len(line) > _MAX_REQUEST and not line.endswith("\n"):
                    # Oversized request: drain the rest of the line so framing
                    # survives, then answer with an error.
                    while True:
                        chunk = rfile.readline(_MAX_REQUEST)
                        if not chunk or chunk.endswith("\n"):
                            break
                    resp = {"ok": False, "error": "request too large"}
                elif not line.strip():
                    continue
                else:
                    # Catch-all per-request boundary: a malformed request (bad
                    # JSON, wrong types, bare non-object) must produce an
                    # {"ok":false} reply, never kill the connection thread.
                    try:
                        resp = self.handle(json.loads(line))
                    except Exception as e:
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
    # Owner-only socket regardless of the spawning client's umask: the root
    # daemon serves root clients only (a world-connectable root socket would be
    # an input-injection privilege escalation). Non-root users get their own
    # per-user daemon, which fails with the clean "/dev/uinput ... run it as
    # root" error. chmod after bind closes the umask race belt-and-braces.
    old_umask = os.umask(0o177)
    try:
        srv.bind(path)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
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
        try:
            resp = json.loads(line)
        except ValueError:
            raise CmdError("wdotool daemon sent an invalid reply") from None
        if not isinstance(resp, dict):
            raise CmdError("wdotool daemon sent an invalid reply")
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

    def seed_pointer(self, x: int, y: int):
        """Tell the daemon where the compositor's pointer really is (B6)."""
        self._rpc(op="seed_pointer", x=x, y=y)

    def geometry(self) -> tuple[int, int]:
        resp = self._rpc(op="geometry")
        return (resp["w"], resp["h"])

    def geometry_status(self) -> tuple[int, int, bool]:
        """(w, h, fallback): `fallback` is True when the compositor could not
        be asked and the numbers are the built-in guess (B5)."""
        resp = self._rpc(op="geometry")
        return (resp["w"], resp["h"], bool(resp.get("fallback")))

    def geometry_full(self) -> tuple[int, int, int, int]:
        """(min_x, min_y, w, h) of the output layout — the origin matters on
        multi-output layouts with non-zero/negative origins."""
        resp = self._rpc(op="geometry")
        return (resp.get("x", 0), resp.get("y", 0), resp["w"], resp["h"])
