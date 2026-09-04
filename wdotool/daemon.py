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

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import sys
import threading
import time
import traceback

from wdotool import keymap, keystate, uinput, xkbmap
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

# Env the daemon keeps when it is spawned from a client (B10). Everything
# else -- the launcher's D-Bus address, DESKTOP_*, anything derived from the
# client's argv -- is dropped: an input daemon that outlives the command that
# started it must not pin that command's session state. WDOTOOL_* is kept as
# a prefix (uinput path, fake-uinput mode, WDOTOOL_REL_MODE).
_KEEP_ENV = ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "HOME", "PATH", "USER",
             "LOGNAME", "LANG", "LC_ALL", "SUDO_UID", "PKEXEC_UID")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


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


def _mods(val, what: str) -> list:
    """Validate a request's modifier-keycode list: nothing but the eight
    modifier keycodes may ever be pressed on a client's say-so."""
    if val is None:
        return []
    if not isinstance(val, list):
        raise RuntimeError(f"invalid {what}: {val!r} (expected a list)")
    out = []
    for v in val:
        if isinstance(v, bool) or not isinstance(v, int):
            raise RuntimeError(f"invalid {what} entry: {v!r} (expected a keycode)")
        if v not in keymap.MODIFIER_KEYCODES:
            raise RuntimeError(f"{what}: {v} is not a modifier keycode")
        out.append(v)
    return out


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

# A modifier held on a keyboard that is not ours cannot be cleared through
# uinput at all (see "modifiers around an injection" below), so the flag says
# so rather than look like it worked. Emitted once per client connection and
# only when the key state could be read -- without that access there is
# nothing to report and the behaviour is the same either way.
FOREIGN_MODS_WARNING = (
    "wdotool: --clearmodifiers cannot clear %s: held on a keyboard that is "
    "not ours, and a uinput device cannot release a key it does not hold -- "
    "the kernel drops that key-up, so nothing reaches the compositor. The "
    "injection goes ahead with the modifier still down; let go of it, or "
    "spell it out in the key sequence instead. Modifiers wdotool itself "
    "holds (keydown) are cleared and put back as usual.")

# Names for that warning, one per MODIFIER_KEYCODES entry.
_MOD_LABELS = {
    keymap.KEY_LEFTSHIFT: "shift", keymap.KEY_RIGHTSHIFT: "right shift",
    keymap.KEY_LEFTCTRL: "ctrl", keymap.KEY_RIGHTCTRL: "right ctrl",
    keymap.KEY_LEFTALT: "alt", keymap.KEY_RIGHTALT: "altgr",
    keymap.KEY_LEFTMETA: "super", keymap.KEY_RIGHTMETA: "right super",
}

# Skip the key-state read (the diagnostic) entirely; for testing.
NO_KEYSTATE_ENV = "WDOTOOL_NO_KEYSTATE"

_UNSET = object()

CGROUP_ROOT = "/sys/fs/cgroup"
# Name of the cgroup the daemon moves itself into; a plain directory under
# the user manager's delegated subtree, not a systemd unit.
_ESCAPE_CGROUP = "wdotool-daemon"


def transient_scope_target(cgroup_line: str, euid: int) -> str | None:
    """Cgroup directory a freshly spawned daemon should move itself into, or
    None when it should stay where it is (B11).

    GNOME runs a custom keyboard shortcut inside a transient systemd scope
    (`app-gnome-<name>-<pid>.scope`). A scope stays *active* while any process
    remains in its cgroup, and neither fork nor setsid changes a cgroup -- so
    the double-forked daemon kept the launcher's scope (and the shell script
    that started it, as far as systemd was concerned) alive for as long as the
    daemon ran; observed at 10+ minutes on 24.04 / systemd 255. `systemd-run
    --user --scope` is the textbook migration but re-runs the command in an
    environment of systemd's choosing, which is exactly what B10 is trying to
    control. Writing our own pid into a sibling cgroup under the user
    manager's *delegated* subtree does the same job with one write, no new
    process and no environment surprises.

    Only transient app scopes are escaped. A `session-N.scope` (a normal
    login) or a system service is left alone: a daemon started there should
    still die with its session. Root is never in a user scope, so the root
    daemon keeps its cgroup too."""
    if euid == 0:
        return None
    path = (cgroup_line or "").strip()
    if path.startswith("0::"):
        path = path[3:]
    if not path.startswith("/"):
        return None
    leaf = path.rsplit("/", 1)[-1]
    if not (leaf.startswith("app-") and leaf.endswith(".scope")):
        return None
    marker = "/user@%d.service" % euid
    idx = path.find(marker + "/")
    if idx < 0:
        return None
    return CGROUP_ROOT + path[:idx + len(marker)] + "/" + _ESCAPE_CGROUP


def _escape_transient_scope():
    """Best effort; a failure is a documented limitation, never fatal."""
    try:
        with open("/proc/self/cgroup") as f:
            line = f.readline()
        target = transient_scope_target(line, os.geteuid())
        if target is None:
            return
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "cgroup.procs"), "w") as f:
            f.write("%d\n" % os.getpid())
    except OSError:
        pass


def clean_env(env=None) -> dict:
    """The environment a spawned daemon keeps (B10)."""
    src = os.environ if env is None else env
    out = {k: v for k, v in src.items()
           if k in _KEEP_ENV or k.startswith("WDOTOOL_")}
    if not out.get("PATH"):
        out["PATH"] = _DEFAULT_PATH
    return out


def _close_inherited_fds():
    """Close everything above stdio (B10): a daemon forked out of a running
    command inherits that command's open files -- most importantly the session
    D-Bus socket, which keeps a bus connection ESTABLISHED for the daemon's
    whole life and made the daemon look like a D-Bus client in `ss`."""
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        limit = 4096
    if not isinstance(limit, int) or limit < 3 or limit > 65536:
        limit = 65536
    os.closerange(3, limit)


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
        self._reader = _UNSET     # evdev key-state reader (None: degraded)
        self._keystate_logged = False
        self.pos_known = False    # has px/py ever been established? (B6)
        self.geom_fallback = False  # last geometry() used FALLBACK_GEOMETRY
        # Active-layout state (B13). `_layout_cache` is ((keymap digest,
        # group), ReverseMap or None); None means "the fixed US table is the
        # answer", which is both the bypass and every failure mode.
        self._layout_cache = None
        self._xkb_backoff = 0.0    # monotonic: don't re-try a failing read
        self._xkb_mods_wait = 0.08  # seconds to wait for a modifiers event
        self._xkb_said: set = set()  # one-shot diagnostics already emitted
        self._xkb_degraded = None  # set while the keymap cannot be used
        self._xkb_group_said = None  # layout state the group notice was for

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
            self._reader = _UNSET    # rebuild it: our event nodes are new
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

    # -- the active layout (B13) ------------------------------------------

    def _xkb_say(self, tag: str, msg: str, warnings=None):
        """One diagnostic per daemon per subject: the daemon log always, the
        answering client's stderr once."""
        if tag in self._xkb_said:
            return
        self._xkb_said.add(tag)
        self._xkb_print(msg, warnings)

    def _xkb_print(self, msg: str, warnings):
        print("wdotool: " + msg, file=sys.stderr, flush=True)
        if warnings is not None:
            warnings.append("wdotool: " + msg)

    def _xkb_say_group(self, state, msg: str, warnings):
        """The "which layout is active?" notice: once per layout state, and
        again whenever the state changes -- a layout switch is exactly when
        the guess is worth repeating."""
        if self._xkb_group_said == state:
            return
        self._xkb_group_said = state
        self._xkb_print(msg, warnings)

    def _xkb_warn_degraded(self, warnings):
        """Tell *this* client that the keymap could not be used, whether or
        not an earlier client was told. A session typing US characters
        because the read failed is typing the wrong thing on every command,
        and a single line to whoever happened to ask first tells nobody."""
        if self._xkb_degraded and warnings is not None:
            warnings.append(self._xkb_degraded)

    def _xkb_fell_back(self, why: str, tag: str, warnings):
        msg = why + "; typing with the built-in US layout table"
        self._xkb_degraded = "wdotool: " + msg
        self._xkb_say(tag, msg)          # the log says it once ...
        self._xkb_warn_degraded(warnings)  # ... the client, every time

    def _layout(self, warnings=None, forced=None):
        """The reverse map for the compositor's ACTIVE layout, or None when
        the fixed US table is the right answer (B13).

        None is returned for the US bypass *and* for every failure: no
        compositor, an unreadable or unparsable keymap, a keymap with no
        typable character. The old path is the floor, never a traceback.
        """
        # `forced` is the client's --layout, which outranks the environment:
        # a command line is the more specific statement of intent, and it is
        # the only one that can reach a daemon spawned with a different one.
        mode = (forced or os.environ.get("WDOTOOL_LAYOUT")
                or "auto").strip().lower()
        if mode in ("us", "fixed"):
            # Nothing is read, nothing is parsed and the bypass check itself
            # is skipped: --layout us is a promise that no layout code runs.
            return None
        force = mode == "xkb"
        now = time.monotonic()
        if now < self._xkb_backoff:
            self._xkb_warn_degraded(warnings)
            return None
        try:
            snap = xkbmap.fetch(mods_wait=self._xkb_mods_wait)
        except xkbmap.XkbError as e:
            self._xkb_backoff = now + 5.0
            self._xkb_fell_back(f"cannot read the compositor's keymap ({e})",
                                "read", warnings)
            return None
        except Exception as e:  # a bug in the new code must not break typing
            self._xkb_backoff = now + 60.0
            self._xkb_fell_back(f"keymap read failed ({e!r})", "read", warnings)
            return None
        if not snap.mods_seen:
            # This compositor does not send wl_keyboard.modifiers to an
            # unfocused client, and never will: stop paying for the wait.
            # (Keying this off `group_known` instead left the wait switched
            # on for ever on the commonest GNOME session there is, whose two
            # groups are the same `us` twice -- +87 ms on every command, B5.)
            self._xkb_mods_wait = 0.0
        key = (hashlib.sha256(snap.text.encode("utf-8", "replace")).digest(),
               snap.group)
        if self._layout_cache is not None and self._layout_cache[0] == key:
            self._xkb_warn_degraded(warnings)
            return self._layout_cache[1]
        self._xkb_degraded = None
        rmap = None
        bypassed = False
        try:
            if not force and xkbmap.active_group_is_plain_us(snap.text, snap.group):
                bypassed = True  # THE BYPASS: nothing below this line runs
            else:
                rmap = xkbmap.build(snap.text, snap.group)
        except xkbmap.XkbError as e:
            rmap = None
            self._xkb_fell_back(f"cannot use the compositor's keymap ({e})",
                                "build", warnings)
        except Exception as e:
            rmap = None
            self._xkb_fell_back(f"keymap conversion failed ({e!r})",
                                "build", warnings)
        try:
            if not snap.group_known and (bypassed or rmap is not None):
                # Which layout is active was a guess. Say so on the bypass
                # path too: a `us,de` session sitting on its German group is
                # precisely the one that types the wrong characters, and it
                # is the bypass that takes it (B1). Once per layout state,
                # so a switch is announced again.
                name = (rmap.name if rmap is not None
                        else xkbmap.group_name(snap.text, snap.group))
                self._xkb_say_group(
                    key,
                    "the compositor does not say which keyboard layout is "
                    "active (it sends that only to the focused window); "
                    f"assuming '{name}'. Set WDOTOOL_XKB_GROUP=<n> to "
                    "pin one.", warnings)
        except Exception:
            pass  # a notice must never be the thing that breaks typing
        self._layout_cache = (key, rmap)
        return rmap

    def _mod_keycodes(self, mask: int, layout) -> list:
        """The modifier keys to hold for one entry's mask."""
        if not mask:
            return []
        if layout is not None:
            return layout.modifier_keycodes(mask)
        return [keymap.KEY_LEFTSHIFT] if mask & xkbmap.MOD_SHIFT else []

    def _press(self, keys, delay, layout=None):
        for code, mask in keys:
            mask = int(mask)
            for mod in self._mod_keycodes(mask, layout):
                if mod in (keymap.KEY_LEFTSHIFT, keymap.KEY_RIGHTSHIFT):
                    if any(s in self.down for s in _SHIFTS):
                        continue
                elif mod in self.down:
                    continue
                self.kb.key(mod, True)
                self.down.add(mod)
            self.kb.key(code, True)
            self.down.add(code)
            self._key_gap(delay)

    def _release(self, keys, delay, layout=None):
        for code, mask in keys:
            mask = int(mask)
            for mod in self._mod_keycodes(mask, layout):
                if mod not in self.down:
                    continue
                self.kb.key(mod, False)
                self.down.discard(mod)
            self.kb.key(code, False)
            self.down.discard(code)
            self._key_gap(delay)

    # -- modifiers around an injection (--clearmodifiers) ------------------
    #
    # xdotool's --clearmodifiers is "clear around the injection", not "clear
    # for good": X11 reads the modifier state, releases it, injects, and puts
    # back what was held. Through uinput that is possible for exactly the
    # modifiers *we* hold, and for no others. Two kernel facts decide it, both
    # measured on live GNOME, KDE and sway sessions:
    #
    #   * `input_handle_event()` drops an EV_KEY release for a code the
    #     emitting device does not have down. A key-up we send for a modifier
    #     the user holds on their own keyboard therefore produces no event at
    #     all -- not "the compositor ignored it": nothing reaches the wire. So
    #     a foreign modifier cannot be cleared from here.
    #   * a key-down we send *does* produce an event, and it stays ours until
    #     we release it. Mutter and KWin reference-count key state across the
    #     seat's devices, so the user letting go of the same modifier takes
    #     the count 2->1 and leaves it active -- for the rest of the session,
    #     because nothing else will ever send our release.
    #
    # Pressing back a modifier we did not hold is thus the worst of both: it
    # restores nothing (nothing was cleared) and it sticks. The restore set is
    # `self.down` -- what this daemon is holding -- sampled with no ioctl and
    # no re-read, so it cannot go stale: clear, inject and restore all happen
    # under one hold of the injection lock.
    #
    # The real keyboards are still read (`keystate.py`, EVIOCGKEY) for the
    # diagnostic: when that is permitted (root; logind's uaccess ACL covers
    # /dev/uinput, not the keyboards) and a foreign keyboard is holding a
    # modifier, say so once instead of letting a flag that cannot help look
    # like it did nothing. Clearing such a modifier for real would need the
    # device grabbed away from the compositor (EVIOCGRAB) -- a different tool.

    def _key_reader(self):
        """The evdev key-state reader used for the diagnostic, or None when
        we must not read: the WDOTOOL_NO_KEYSTATE override. Built once per set
        of uinput devices, so that our own event nodes -- which would answer
        with our own injection -- are known before the first read."""
        if self._reader is not _UNSET:
            return self._reader
        override = os.environ.get(NO_KEYSTATE_ENV, "")
        if override and override != "0":
            self._reader = None
        else:
            self._reader = keystate.Reader(exclude_paths=self._own_event_nodes())
        return self._reader

    def _own_event_nodes(self) -> set:
        """/dev/input paths of our own virtual devices (UI_GET_SYSNAME)."""
        out = set()
        for dev in (self.kb, self.mouse, self.tablet):
            name = ""
            if dev is not None:
                try:
                    name = dev.sysname()
                except AttributeError:   # a test double
                    name = ""
            if name:
                out.add(os.path.join(keystate.INPUT_DIR, name))
        return out

    def _clear_mods(self, warnings=None, session=None) -> set:
        """Release the modifier keys; return the ones to press back afterwards.

        That set is what *this daemon* holds. A modifier on another keyboard
        is neither released by the loop below (the kernel drops the key-up)
        nor safe to press back (it would stick), so it is reported and left
        alone. The loop still sends all eight: a key-up for a code we do not
        hold costs one write and is dropped, and spelling out "let go of
        every modifier" is what the flag means."""
        ours = {c for c in keymap.MODIFIER_KEYCODES if c in self.down}
        self._warn_foreign_mods(warnings, session)
        for code in keymap.MODIFIER_KEYCODES:
            self.kb.key(code, False)
            self.down.discard(code)
        return ours

    def _restore_mods(self, held):
        """Press back what _clear_mods() released -- the modifiers this daemon
        was already holding.

        They stay down afterwards, exactly as they were before the injection:
        `keydown ctrl; type --clearmodifiers x` ends with ctrl down, and the
        user's own `keyup ctrl` (or the daemon exiting, which destroys the
        device and releases its keys) still ends it. Nothing else is ever
        pressed here, which is what makes a stuck modifier impossible."""
        for code in keymap.MODIFIER_KEYCODES:
            if code in held:
                self.kb.key(code, True)
                self.down.add(code)

    def _foreign_mods(self):
        """Modifiers held on a keyboard that is not ours, or None when the key
        state cannot be read at all (no permission -- the normal non-root
        case, in which nothing about the behaviour changes)."""
        reader = self._key_reader()
        if reader is None:
            return None
        try:
            return reader.held(keymap.MODIFIER_KEYCODES)
        except Exception:
            return None   # a diagnostic must never break an injection

    def _warn_foreign_mods(self, warnings, session):
        """One warning per client connection (`session`), so a chained
        command says it once and the next invocation says it again."""
        held = self._foreign_mods()
        if not held:
            return       # nothing held, or nothing readable: nothing to say
        msg = FOREIGN_MODS_WARNING % ", ".join(
            _MOD_LABELS[c] for c in keymap.MODIFIER_KEYCODES if c in held)
        if not self._keystate_logged:
            self._keystate_logged = True
            print(msg, file=sys.stderr, flush=True)
        if session is not None:
            if session.get("keystate_warned"):
                return
            session["keystate_warned"] = True
        if warnings is not None:
            warnings.append(msg)

    @contextlib.contextmanager
    def _mods_cleared(self, on, warnings, session):
        """clear -> inject -> restore without letting go of the injection
        lock. The ops that carry `clearmods` themselves (`type`, `key`) do it
        inline; this is for the ones handle() wraps. Doing it as three
        requests instead would leave two gaps in which another wdotool
        process could inject with the modifiers down, or land its own
        injection between ours and the restore."""
        if not on:
            yield
            return
        self._need_devices()
        held = self._clear_mods(warnings, session)
        try:
            yield
        finally:
            self._restore_mods(held)

    def op_clear_modifiers(self, warnings=None, session=None) -> list:
        """The clear half on its own (DaemonClient.clear_modifiers, kept for
        the frozen API). Every wdotool command uses the `clearmods` flag on
        the injection op instead, which keeps the pair atomic."""
        self._need_devices()
        return sorted(self._clear_mods(warnings, session))

    def op_restore_modifiers(self, held):
        """The restore half; see op_clear_modifiers. `held` is validated to
        modifier keycodes by handle(), and _restore_mods presses nothing
        else, so a client cannot use this to hold down an arbitrary key."""
        self._need_devices()
        self._restore_mods(held)

    def op_key(self, spec, direction, delay_ms, clearmods, session=None,
               layout_mode=None):
        self._need_devices()
        warnings = []
        # Outside the clear/restore window: reading the compositor's keymap
        # is a query, and holding the modifiers released across it buys
        # nothing.
        layout = self._layout(warnings, layout_mode)
        # Restore even when the sequence is rejected or the injection fails:
        # the modifiers are already released by then.
        with self._mods_cleared(clearmods, warnings, session):
            # ValueError on a sequence xdo rejects outright
            keys, warns = keymap.parse_keyseq(spec, layout)
            d = delay_ms / 1000
            if direction == "press":
                # xdo_send_keysequence_window converts the sequence once per pass
                # (press, then release), so every "(symbol) No such key name"
                # diagnostic is printed twice by the real xdotool (B12). Our own
                # one-shot layout notice is not one of xdo's and is not doubled.
                warns = warns * 2
                self._press(keys, d / 2, layout)
                self._release(keys, d / 2, layout)
            elif direction == "down":
                self._press(keys, d, layout)
            elif direction == "up":
                self._release(keys, d, layout)
            else:
                raise RuntimeError(f"invalid key direction {direction!r}")
        return warnings + warns

    def op_type(self, text, delay_ms, clearmods, session=None,
                layout_mode=None):
        self._need_devices()
        warnings = []
        layout = self._layout(warnings, layout_mode)   # a query: see op_key
        lname = "US" if layout is None else layout.name
        with self._mods_cleared(clearmods, warnings, session):
            # xdo_enter_text_window: delay split between down and up, down capped at 50ms
            down_d = min(delay_ms / 2, 50) / 1000
            up_d = delay_ms / 1000 - down_d
            for ch in text:
                if layout is None:
                    hit = keymap.char_to_key(ch)
                    seq = None if hit is None else [(hit[0], int(hit[1]))]
                else:
                    # One character can be two keystrokes: a dead key and then
                    # the base letter, which is how a French keyboard types "ô".
                    seq = layout.lookup_char(ch)
                if not seq:
                    warnings.append(
                        f"Can't type character '{ch}' (not on the {lname} layout). Skipping.")
                    continue
                for code, mask in seq:
                    mods = [m for m in self._mod_keycodes(mask, layout)
                            if not (m in (keymap.KEY_LEFTSHIFT, keymap.KEY_RIGHTSHIFT)
                                    and any(s in self.down for s in _SHIFTS))
                            and m not in self.down]
                    for mod in mods:
                        self.kb.key(mod, True)
                    self.kb.key(code, True)
                    if down_d > 0:
                        time.sleep(down_d)
                    self.kb.key(code, False)
                    for mod in reversed(mods):
                        self.kb.key(mod, False)
                    self._key_gap(up_d)
        return warnings

    # -- protocol ----------------------------------------------------------

    def handle(self, req, session=None) -> dict:
        """One request. `session` is the per-connection scratch dict (a
        warning that must be said once per command belongs there, not in the
        daemon: the daemon outlives every client)."""
        if not isinstance(req, dict):
            return {"ok": False, "error": f"invalid request: {req!r} (expected an object)"}
        if session is None:
            session = {}
        op = req.get("op")
        warnings: list[str] = []
        with self.lock:
            if op == "type":
                warnings = self.op_type(
                    _text(req.get("text"), "text"),
                    _num(req.get("delay_ms", 12), "delay_ms", 0, MAX_DELAY_MS),
                    req.get("clearmods", False), session)
            elif op == "key":
                warnings = self.op_key(
                    _text(req.get("spec"), "spec"),
                    req.get("direction", "press"),
                    _num(req.get("delay_ms", 12), "delay_ms", 0, MAX_DELAY_MS),
                    req.get("clearmods", False), session)
            elif op == "clear_modifiers":
                held = self.op_clear_modifiers(warnings, session)
                return {"ok": True, "held": held, "warnings": warnings}
            elif op == "restore_modifiers":
                self.op_restore_modifiers(_mods(req.get("held"), "held"))
            elif op == "mousemove_abs":
                with self._mods_cleared(req.get("clearmods", False), warnings, session):
                    self.op_mousemove_abs(_num(req.get("x"), "x", _I32_MIN, _I32_MAX),
                                          _num(req.get("y"), "y", _I32_MIN, _I32_MAX),
                                          warnings)
            elif op == "mousemove_rel":
                with self._mods_cleared(req.get("clearmods", False), warnings, session):
                    self.op_mousemove_rel(_num(req.get("dx"), "dx", _I32_MIN, _I32_MAX),
                                          _num(req.get("dy"), "dy", _I32_MIN, _I32_MAX),
                                          warnings)
            elif op == "button":
                with self._mods_cleared(req.get("clearmods", False), warnings, session):
                    self.op_button(_num(req.get("btn"), "button", 0, 255),
                                   bool(req.get("down")))
            elif op == "click":
                with self._mods_cleared(req.get("clearmods", False), warnings, session):
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
        session: dict = {}   # per-connection state (see handle())
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
                        resp = self.handle(json.loads(line), session)
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
    def _exec_plan() -> "list[tuple[str, list[str], dict]]":
        """(path, argv, env) candidates for re-execing as the daemon, best
        first (B10). Re-execing is what gives the daemon a clean argv (`ps`
        showed the *client's* command line before), a fresh address space and
        no inherited state; `wdotool __daemon` / `python -m wdotool __daemon`
        are both predictable and both match `pkill -f __daemon`. Paths are
        resolved before the caller chdir()s to /."""
        env = clean_env()
        plan = []
        exe = sys.argv[0] if sys.argv else ""
        if exe:
            exe = os.path.abspath(exe)
        if (os.path.basename(exe) in ("wdotool", "xdotool")
                and os.path.isfile(exe) and os.access(exe, os.X_OK)):
            plan.append((exe, [exe, "__daemon"], dict(env)))
        if sys.executable:
            menv = dict(env)
            try:
                import wdotool as _pkg
                # The parent of the package directory; for a zipapp this is
                # the .pyz itself, which is a valid PYTHONPATH entry too.
                parent = os.path.dirname(
                    os.path.dirname(os.path.abspath(_pkg.__file__)))
            except Exception:  # noqa: BLE001 -- diagnostics only
                parent = ""
            if parent:
                menv["PYTHONPATH"] = parent
            plan.append((sys.executable,
                         [sys.executable, "-m", "wdotool", "__daemon"], menv))
        return plan

    @staticmethod
    def _spawn():
        """Daemonize: fork, setsid, fork; the grandchild redirects stdio to
        the daemon log, closes every inherited fd, leaves the launcher's
        transient scope, drops the client's environment and cwd, and re-execs
        as `wdotool __daemon` (or `python -m wdotool __daemon`); if no re-exec
        is possible it runs daemon_main() in-process with the same clean
        state."""
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
            plan = DaemonClient._exec_plan()  # resolves paths before chdir
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
            _close_inherited_fds()
            _escape_transient_scope()
            try:
                os.chdir("/")  # never hold the client's cwd busy
            except OSError:
                pass
            for path, argv, env in plan:
                try:
                    os.execve(path, argv, env)
                except OSError:
                    continue  # try the next plan, else run in-process
            os.environ.clear()
            os.environ.update(clean_env(dict(plan[0][2]) if plan else None))
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

    def type_text(self, text: str, delay_ms: int, clearmods: bool = False,
                  layout_mode: str | None = None):
        extra = {"layout_mode": layout_mode} if layout_mode else {}
        self._rpc(op="type", text=text, delay_ms=delay_ms, clearmods=clearmods,
                  **extra)

    def key(self, spec: str, direction: str, delay_ms: int, clearmods: bool,
            layout_mode: str | None = None):
        extra = {"layout_mode": layout_mode} if layout_mode else {}
        self._rpc(op="key", spec=spec, direction=direction, delay_ms=delay_ms,
                  clearmods=clearmods, **extra)

    def clear_modifiers(self) -> list:
        """Release the modifier keys and report which ones wdotool itself was
        holding, to be handed back to restore_modifiers() when the injection
        is done. Kept for the frozen API: every command passes `clearmods` to
        the injection op instead, so that the daemon can do clear, inject and
        restore under one lock -- as two extra round trips this pair leaves
        gaps another process can inject into."""
        return list(self._rpc(op="clear_modifiers").get("held") or [])

    def restore_modifiers(self, held):
        """Press back what clear_modifiers() released. No round trip when
        there is nothing to restore."""
        if not held:
            return
        self._rpc(op="restore_modifiers", held=list(held))

    # `clearmods` on these is the whole --clearmodifiers dance in one
    # request: the daemon releases the modifiers, injects and puts back what
    # it was holding without letting go of the injection lock.
    def mousemove_abs(self, x: int, y: int, clearmods: bool = False):
        self._rpc(op="mousemove_abs", x=x, y=y, clearmods=clearmods)

    def mousemove_rel(self, dx: int, dy: int, clearmods: bool = False):
        self._rpc(op="mousemove_rel", dx=dx, dy=dy, clearmods=clearmods)

    def button(self, btn: int, down: bool, clearmods: bool = False):
        self._rpc(op="button", btn=btn, down=down, clearmods=clearmods)

    def click(self, btn: int, repeat: int, delay_ms: int,
              clearmods: bool = False):
        self._rpc(op="click", btn=btn, repeat=repeat, delay_ms=delay_ms,
                  clearmods=clearmods)

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
