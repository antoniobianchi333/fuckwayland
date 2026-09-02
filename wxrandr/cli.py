"""OWNER: wxrandr builder. xrandr 1.5.4 option parsing + dispatch.

Byte-parity notes (SCRATCH/reference/xrandr-notes.md + captures):
- strict `--long` options plus short -d -s -r -v -x -y -o -q; `-help` and
  `--help` both work; no other single-dash long forms.
- parse errors: stderr `xrandr: <msg>` + `Try 'xrandr --help' for more
  information.`, exit 1. Fatals: `xrandr: <msg>`, exit 1. A bad --output NAME
  is only a bare `warning: output NAME not found; ignoring` (exit 0).
- bare invocation / -q / --verbose query; --verbose with a 1.0-only set adds
  the RandR 1.0 table; mode-store operations with no other action exit
  silently like the real thing.
"""

import os
import re
import sys

from wxrandr import core
from wxrandr.core import ArgErr, Fatal, Stanza

USAGE = """usage: xrandr [options]
  where options are:
  --display <display> or -d <display>
  --help
  -o <normal,inverted,left,right,0,1,2,3>
            or --orientation <normal,inverted,left,right,0,1,2,3>
  -q        or --query
  -s <size>/<width>x<height> or --size <size>/<width>x<height>
  -r <rate> or --rate <rate> or --refresh <rate>
  -v        or --version
  -x        (reflect in x)
  -y        (reflect in y)
  --screen <screen>
  --verbose
  --current
  --dryrun
  --nograb
  --prop or --properties
  --fb <width>x<height>
  --fbmm <width>x<height>
  --dpi <dpi>/<output>
  --output <output>
      --auto
      --mode <mode>
      --preferred
      --pos <x>x<y>
      --rate <rate> or --refresh <rate>
      --reflect normal,x,y,xy
      --rotate normal,inverted,left,right
      --left-of <output>
      --right-of <output>
      --above <output>
      --below <output>
      --same-as <output>
      --set <property> <value>
      --scale <x>[x<y>]
      --scale-from <w>x<h>
      --transform <a>,<b>,<c>,<d>,<e>,<f>,<g>,<h>,<i>
      --filter nearest,bilinear
      --off
      --crtc <crtc>
      --panning <w>x<h>[+<x>+<y>[/<track:w>x<h>+<x>+<y>[/<border:l>/<t>/<r>/<b>]]]
      --gamma <r>[:<g>:<b>]
      --brightness <value>
      --primary
  --noprimary
  --newmode <name> <clock MHz>
            <hdisp> <hsync-start> <hsync-end> <htotal>
            <vdisp> <vsync-start> <vsync-end> <vtotal>
            [flags...]
            Valid flags: +HSync -HSync +VSync -VSync
                         +CSync -CSync CSync Interlace DoubleScan
  --rmmode <name>
  --addmode <output> <name>
  --delmode <output> <name>
  --listproviders
  --setprovideroutputsource <prov-xid> <source-xid>
  --setprovideroffloadsink <prov-xid> <sink-xid>
  --listmonitors
  --listactivemonitors
  --setmonitor <name> {auto|<w>/<mmw>x<h>/<mmh>+<x>+<y>} {none|<output>,<output>,...}
  --delmonitor <name>
"""

_DIRECTION = ("normal", "left", "inverted", "right")


class Opts:
    def __init__(self):
        self.query = False
        self.query_1 = False
        self.verbose = False
        self.dryrun = False
        self.version = False
        self.current = False
        self.props = False
        self.screen = -1
        self.fb = None
        self.fbmm = None
        self.dpi = None            # float or output name
        self.noprimary = False
        self.persistent = False    # --persistent (Mutter: write monitors.xml)
        self.global_auto = False
        self.stanzas = []
        self.mode_ops = []         # ("new", name, modeline)/("rm", name)/
        #                            ("add"|"del", output, name)
        self.monitor_op = None     # ("list",)/("listactive",)/("set",...)/
        #                            ("del", name)
        self.providers = False
        # RandR 1.0
        self.size = -1             # index, or (w, h)
        self.rate = -1.0
        self.rot = -1              # index into _DIRECTION
        self.toggle_x = False
        self.toggle_y = False
        # bookkeeping
        self.setit = False
        self.setit_1_2 = False
        self.action = False


def _number(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        raise ArgErr("failed to parse '%s' as a number\n" % s)


def parse(argv: list) -> Opts:
    o = Opts()
    cur: Stanza | None = None
    i = 0

    def need(n=1):
        nonlocal i
        if i + n >= len(argv):
            if n == 1:
                raise ArgErr("%s requires an argument\n" % argv[i])
            if n == 3:
                raise ArgErr("%s requires three argument\n" % argv[i])
            raise ArgErr("%s requires two arguments\n" % argv[i])
        i += 1
        return argv[i]

    def per_output(opt):
        if cur is None:
            raise ArgErr("%s must be used after --output\n" % opt)

    while i < len(argv):
        a = argv[i]
        if a in ("-help", "--help"):
            sys.stdout.write(USAGE)
            raise SystemExit(0)
        elif a in ("-d", "--display"):
            v = need()
            # X display syntax (":0") means "the current session" here; a
            # real socket name selects the wayland display to talk to.
            if not v.startswith(":"):
                os.environ["WAYLAND_DISPLAY"] = v
        elif a in ("-v", "--version"):
            o.version = True
            o.action = True
        elif a in ("-q", "--query"):
            o.query = True
        elif a == "--q1":
            o.query_1 = True
        elif a == "--q12":
            pass
        elif a == "--verbose":
            o.verbose = True
        elif a == "--dryrun":
            o.dryrun = True
            o.verbose = True
        elif a == "--current":
            o.current = True
        elif a == "--nograb":
            pass
        elif a in ("--prop", "--properties"):
            o.props = True
        elif a == "--screen":
            v = need()
            o.screen = int(_number(v))
            if o.screen < 0:
                raise ArgErr("--screen argument must be nonnegative\n")
        elif a == "--fb":
            v = need()
            m = re.fullmatch(r"(\d+)x(\d+)", v)
            if not m:
                raise ArgErr("failed to parse '%s' as a framebuffer size\n"
                             % v)
            o.fb = (int(m.group(1)), int(m.group(2)))
            o.setit_1_2 = True
            o.action = True
        elif a == "--fbmm":
            v = need()
            m = re.fullmatch(r"(\d+)x(\d+)", v)
            if not m:
                raise ArgErr("failed to parse '%s' as a physical size\n" % v)
            o.fbmm = (int(m.group(1)), int(m.group(2)))
            o.setit_1_2 = True
            o.action = True
        elif a == "--dpi":
            v = need()
            try:
                o.dpi = float(v)
            except ValueError:
                o.dpi = v  # an output name: dpi from its physical size
            o.setit_1_2 = True
            o.action = True
        elif a in ("-o", "--orientation"):
            v = need()
            if v in ("0", "1", "2", "3"):
                o.rot = int(v)
            elif v in _DIRECTION:
                o.rot = _DIRECTION.index(v)
            else:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            o.setit = True
            o.action = True
        elif a in ("-s", "--size"):
            v = need()
            m = re.fullmatch(r"(\d+)x(\d+)", v)
            if m:
                o.size = (int(m.group(1)), int(m.group(2)))
            else:
                o.size = int(_number(v))
                if o.size < 0:
                    raise ArgErr("--size argument must be nonnegative\n")
            o.setit = True
            o.action = True
        elif a in ("-r", "--rate", "--refresh"):
            v = _number(need())
            o.setit = True
            if cur is not None:
                cur.rate = v
                o.setit_1_2 = True
            else:
                o.rate = v
            o.action = True
        elif a == "-x":
            o.toggle_x = True
            o.setit = True
            o.action = True
        elif a == "-y":
            o.toggle_y = True
            o.setit = True
            o.action = True
        elif a == "--output":
            cur = Stanza(name=need())
            o.stanzas.append(cur)
            o.setit_1_2 = True
            o.action = True
        elif a == "--auto":
            if cur is not None:
                cur.auto = True
            else:
                o.global_auto = True
            o.setit_1_2 = True
            o.action = True
        elif a == "--mode":
            per_output(a)
            cur.mode = need()
        elif a == "--preferred":
            per_output(a)
            cur.preferred = True
        elif a == "--pos":
            per_output(a)
            v = need()
            m = re.fullmatch(r"(-?\d+)x(-?\d+)", v)
            if not m:
                raise ArgErr("failed to parse '%s' as a position\n" % v)
            cur.pos = (int(m.group(1)), int(m.group(2)))
        elif a == "--rotate":
            per_output(a)
            v = need()
            if v not in core.ROTATIONS:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            cur.rotate = v
        elif a == "--reflect":
            per_output(a)
            v = need()
            if v not in core.REFLECTIONS:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            cur.reflect = v
        elif a in ("--left-of", "--right-of", "--above", "--below",
                   "--same-as"):
            per_output(a)
            cur.relation = (a[2:], need())
        elif a == "--set":
            per_output(a)
            if i + 2 >= len(argv):
                raise ArgErr("%s requires two arguments\n" % a)
            cur.props.append((argv[i + 1], argv[i + 2]))
            i += 2
        elif a == "--scale":
            per_output(a)
            v = need()
            m = re.fullmatch(r"([0-9.eE+-]+)x([0-9.eE+-]+)", v)
            try:
                if m:
                    sx, sy = float(m.group(1)), float(m.group(2))
                else:
                    sx = sy = float(v)
            except ValueError:
                raise ArgErr("failed to parse '%s' as a scaling factor\n" % v)
            if sx <= 0 or sy <= 0:
                raise ArgErr("scaling factors must be positive\n")
            cur.scale = (sx, sy)
        elif a == "--scale-from":
            per_output(a)
            v = need()
            m = re.fullmatch(r"(-?\d+)x(-?\d+)", v)
            if not m:
                raise ArgErr("failed to parse '%s' as a scale-from size\n"
                             % v)
            w, h = int(m.group(1)), int(m.group(2))
            if w < 0 or h < 0:
                raise ArgErr("--scale-from dimensions must be nonnegative\n")
            cur.scale_from = (w, h)
        elif a == "--transform":
            per_output(a)
            v = need()
            if v != "none":
                parts = v.split(",")
                if len(parts) != 9:
                    raise ArgErr("failed to parse '%s' as a transformation\n"
                                 % v)
                for p in parts:
                    try:
                        float(p)
                    except ValueError:
                        raise ArgErr("failed to parse '%s' as a "
                                     "transformation\n" % v)
            core.warn("--transform is not supported on Wayland; ignoring\n")
        elif a == "--filter":
            per_output(a)
            v = need()
            if v not in ("nearest", "bilinear"):
                raise ArgErr("Bad argument: %s, for a filter\n" % v)
            cur.props.append(("__filter", v))
        elif a == "--off":
            per_output(a)
            cur.off = True
        elif a == "--crtc":
            per_output(a)
            need()  # crtc assignment is meaningless here; accepted, ignored
        elif a == "--panning":
            per_output(a)
            need()
            core.warn("--panning is not supported on Wayland; ignoring\n")
        elif a == "--gamma":
            per_output(a)
            v = need()
            parts = v.split(":")
            try:
                vals = [float(p) for p in parts]
            except ValueError:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            if len(vals) == 1:
                vals = vals * 3
            if len(vals) != 3:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            if any(g <= 0 for g in vals):
                raise ArgErr("gamma correction factors must be positive\n")
            cur.gamma = tuple(vals)
            o.setit_1_2 = True
        elif a == "--brightness":
            per_output(a)
            v = need()
            try:
                cur.brightness = float(v)
            except ValueError:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            o.setit_1_2 = True
        elif a == "--primary":
            per_output(a)
            cur.primary = True
        elif a == "--noprimary":
            o.noprimary = True
            o.setit_1_2 = True
            o.action = True
        elif a == "--persistent":
            # wxrandr extension (not in xrandr's usage): on Mutter apply with
            # method 2 so the layout lands in ~/.config/monitors.xml
            o.persistent = True
        elif a == "--newmode":
            name = need()
            clock = _number(need())
            nums = [int(_number(need())) for _ in range(8)]
            flags = []
            while i + 1 < len(argv) and argv[i + 1].lower() in core.MODE_FLAGS:
                i += 1
                flags.append(argv[i].lower())
            o.mode_ops.append(("new", name, clock, nums, flags))
            o.action = True
        elif a == "--rmmode":
            o.mode_ops.append(("rm", need()))
            o.action = True
        elif a in ("--addmode", "--delmode"):
            if i + 2 >= len(argv):
                raise ArgErr("%s requires two arguments\n" % a)
            o.mode_ops.append((a[2:5], argv[i + 1], argv[i + 2]))
            i += 2
            o.action = True
        elif a == "--listproviders":
            o.providers = True
            o.action = True
        elif a in ("--setprovideroutputsource", "--setprovideroffloadsink"):
            if i + 2 >= len(argv):
                raise ArgErr("%s requires two arguments\n" % a)
            i += 2
            core.warn("%s is not supported on Wayland; ignoring\n" % a)
            o.action = True
        elif a == "--listmonitors":
            o.monitor_op = ("list",)
            o.action = True
        elif a == "--listactivemonitors":
            o.monitor_op = ("listactive",)
            o.action = True
        elif a == "--setmonitor":
            if i + 3 >= len(argv):
                raise ArgErr("%s requires three argument\n" % a)
            i += 3
            core.warn("--setmonitor is not supported on Wayland; ignoring\n")
            o.action = True
        elif a == "--delmonitor":
            o.monitor_op = ("del", need())
            o.action = True
        else:
            raise ArgErr("unrecognized option '%s'\n" % a)
        i += 1
    return o


# -- backends -----------------------------------------------------------------

class Session:
    """Compositor connection bundle: chosen backend + state file + wlr
    enrichment. Built lazily — --help/parse errors never touch a socket.

    Backends: sway (IPC socket present), mutter (GNOME: the session bus owns
    org.gnome.Mutter.DisplayConfig — no extension, no root), wlr (anything
    with zwlr_output_management). WXRANDR_BACKEND=sway|wlr|mutter (alias
    gnome) forces one."""

    BACKENDS = ("sway", "wlr", "mutter")

    def __init__(self):
        from wdotool import session as wsession
        self.backend = os.environ.get("WXRANDR_BACKEND")
        if self.backend == "gnome":
            self.backend = "mutter"
        sway_sock = wsession.find_sway_socket()
        probe = None
        if self.backend not in self.BACKENDS:
            if sway_sock:
                self.backend = "sway"
            else:
                from wxrandr import mutter as mutter_mod
                probe = mutter_mod.probe()
                self.backend = "mutter" if probe is not None else "wlr"
        self.ipc = None
        self.wlr = None
        self.mutter = None
        self.persistent = os.environ.get("WXRANDR_PERSIST", "") not in ("", "0")
        # OSError as well as Fatal: WlConn's connect() can raise
        # ConnectionRefusedError on a stale-but-present socket, and sway IPC
        # can drop mid-handshake — both must read as "Can't open display",
        # never a traceback.
        if self.backend == "sway":
            try:
                self.ipc = core.SwayIPC(sway_sock)
            except (Fatal, OSError):
                self._cant_open()
            self.wlr = core.wlr_snapshot_safe()
        elif self.backend == "mutter":
            from wxrandr import mutter as mutter_mod
            try:
                # no bus at all -> "Can't open display"; a bus without
                # DisplayConfig raises Fatal with a one-line explanation
                self.mutter = mutter_mod.MutterOutputs(bus=probe)
            except (mutter_mod.DBusError, OSError, ValueError):
                self._cant_open()
        else:
            try:
                self.wlr = core.WlrOutputs()
            except (Fatal, OSError):
                self._cant_open()
        # state is keyed by the compositor's wayland socket so all backends
        # share one primary/custom-mode store per session
        hit = wsession.find_wayland_socket()
        key = hit[2] if hit else (self.ipc.sockpath if self.ipc else "?")
        self.state = core.State(key)

    @staticmethod
    def _cant_open():
        sys.stderr.write("Can't open display %s\n"
                         % os.environ.get("WAYLAND_DISPLAY", ""))
        raise SystemExit(1)

    def snapshot(self):
        if self.backend == "sway":
            return core.snapshot_sway(self.ipc, self.state, self.wlr)
        if self.backend == "mutter":
            return self.mutter.snapshot(self.state)
        return core.snapshot_wlr(self.wlr, self.state)

    def dims(self, t) -> tuple:
        """Pending logical size of an enabled target in the backend's own
        coordinate space (Mutter rounds and may not scale at all; wlroots
        truncates)."""
        if self.backend == "mutter":
            return self.mutter.predicted_dims(t, self.state)
        return core.predicted_dims(t, self.state)

    def apply(self, targets):
        if self.backend == "sway":
            return core.apply_sway(self.ipc, self.state, targets)
        if self.backend == "mutter":
            return self.mutter.apply(self.state, targets, self.persistent)
        core.apply_wlr(self.wlr, self.state, targets)
        # refresh the head snapshot for the post-apply query
        self.wlr.conn.roundtrip()
        return self.snapshot()

    @property
    def compositor_name(self):
        if self.backend == "mutter":
            return "mutter"
        return "sway" if self.backend == "sway" else "wlroots"


# -- action helpers -----------------------------------------------------------

def _do_mode_ops(sess: Session, opts: Opts, outputs):
    st = sess.state
    names = {o.name for o in outputs}
    for op in opts.mode_ops:
        if op[0] == "new":
            _, name, clock, nums, flags = op
            st.modes()[name] = {
                "clock": clock,
                "h": nums[0:4], "v": nums[4:8],
                "flags": flags,
            }
        elif op[0] == "rm":
            name = op[1]
            if name not in st.modes():
                raise Fatal('cannot find mode "%s"\n' % name)
            del st.modes()[name]
            for lst in st.addmodes().values():
                if name in lst:
                    lst.remove(name)
        else:  # add / del
            _, out, name = op
            if out not in names:
                raise Fatal('cannot find output "%s"\n' % out)
            if name not in st.modes():
                raise Fatal('cannot find mode "%s"\n' % name)
            lst = st.addmodes().setdefault(out, [])
            if op[0] == "add":
                if name not in lst:
                    lst.append(name)
            else:
                if name in lst:
                    lst.remove(name)
    st.save()


def _dpi_and_mm(opts: Opts, outputs, new_w, new_h):
    """The verbose/dryrun screen line pieces (xrandr main + set_screen_size):
    dpi from the *current* screen height unless --dpi/--fbmm."""
    if opts.fbmm:
        return (25.4 * new_h / opts.fbmm[1] if opts.fbmm[1] else 96.0,
                opts.fbmm[0], opts.fbmm[1])
    dpi = None
    if isinstance(opts.dpi, float):
        dpi = opts.dpi
    elif isinstance(opts.dpi, str):
        for o in outputs:
            if o.name == opts.dpi and o.active and o.mm_h:
                dpi = 25.4 * o.h / o.mm_h
        if dpi is None:
            core.warn("output %s has no physical size; using 96dpi\n"
                      % opts.dpi)
            dpi = 96.0
    if dpi is None:
        x0, y0, x1, y1 = core.layout_box(outputs)
        cur_h = y1 - y0
        mm_h = core.screen_mm(cur_h)
        dpi = 25.4 * cur_h / mm_h if mm_h else 96.0
    return dpi, int(25.4 * new_w / dpi), int(25.4 * new_h / dpi)


def _print_plan(opts: Opts, outputs, targets, dims, pos):
    """The crtc/screen plan lines xrandr prints under --verbose/--dryrun."""
    crtc_of = {}
    n = 0
    for o in outputs:
        if o.active:
            crtc_of[o.name] = n
            n += 1
    for t in targets:
        if t.name not in crtc_of and t.enabled:
            crtc_of[t.name] = n
            n += 1
    for t in targets:
        if not t.changed:
            continue
        was = t.output
        mode_changed = t.enabled and t.mode is not None and (
            was.current is None
            or (t.mode.w, t.mode.h, t.mode.refresh_mhz)
            != (was.current.w, was.current.h, was.current.refresh_mhz))
        if (not t.enabled and was.active) or mode_changed:
            print("crtc %d: disable" % crtc_of.get(t.name, 0))
    xs = [pos[t.name][0] + dims[t.name][0] for t in targets if t.enabled]
    ys = [pos[t.name][1] + dims[t.name][1] for t in targets if t.enabled]
    new_w = max(xs) if xs else 0
    new_h = max(ys) if ys else 0
    if opts.fb:
        new_w, new_h = opts.fb
    dpi, mm_w, mm_h = _dpi_and_mm(opts, outputs, new_w, new_h)
    print("screen 0: %dx%d %dx%d mm %6.2fdpi" % (new_w, new_h, mm_w, mm_h,
                                                 dpi))
    for t in targets:
        if not t.changed or not t.enabled:
            continue
        mode = t.mode or t.output.current
        if mode is None:
            continue
        x, y = pos.get(t.name, (t.output.x, t.output.y))
        print('crtc %d: %12s %6.2f +%d+%d "%s"'
              % (crtc_of.get(t.name, 0), mode.display_name, mode.refresh_hz,
                 x, y, t.name))


def _check_fb(opts: Opts, targets, dims, pos):
    if not opts.fb:
        return
    fw, fh = opts.fb
    for t in targets:
        if not t.enabled or t.name not in pos:
            continue
        x, y = pos[t.name]
        w, h = dims[t.name]
        if x + w > fw or y + h > fh:
            core.warn("specified screen %dx%d not large enough for output"
                      " %s (%dx%d+%d+%d)\n" % (fw, fh, t.name, w, h, x, y))


def _check_screen_size(opts: Opts, targets, dims, pos):
    """xrandr's set_screen_size bound (xrandr.c:2109): the resolved layout (or
    an explicit --fb) may not exceed maxWidth/maxHeight. Guards against a
    far-flung --pos/--fb being handed to the compositor (and, on the wlr
    backend, against the out-of-range struct pack it would otherwise trip)."""
    if opts.fb:
        desired_w, desired_h = opts.fb
    else:
        xs = [pos[t.name][0] + dims[t.name][0]
              for t in targets if t.enabled and t.name in pos]
        ys = [pos[t.name][1] + dims[t.name][1]
              for t in targets if t.enabled and t.name in pos]
        desired_w = max(xs) if xs else 0
        desired_h = max(ys) if ys else 0
    if desired_w > core.MAX_WIDTH or desired_h > core.MAX_HEIGHT:
        raise Fatal("screen cannot be larger than %dx%d (desired size "
                    "%dx%d)\n" % (core.MAX_WIDTH, core.MAX_HEIGHT,
                                  desired_w, desired_h))


def _apply_gamma(sess: Session, opts: Opts, outputs):
    from wxrandr import gamma as gammamod
    from wdotool import session as wsession
    hit = wsession.find_wayland_socket()
    sock = hit[2] if hit else None
    known = {o.name for o in outputs}
    changed = False
    for s in opts.stanzas:
        if s.brightness is None and s.gamma is None:
            continue
        if s.name not in known:
            # build_targets already printed the bare not-found warning and
            # xrandr keeps exit 0 for a typo'd --output; don't spawn a holder
            # against a name the compositor has never heard of.
            continue
        if sess.backend == "mutter":
            # Mutter has neither zwlr_gamma_control nor a DisplayConfig LUT
            # call: a cosmetic impossibility, so warn and succeed
            core.warn("--brightness/--gamma are not supported on Mutter "
                      "(no gamma LUT API); ignoring for %s\n" % s.name)
            continue
        rec = sess.state.gamma().get(s.name, {})
        brightness = (s.brightness if s.brightness is not None
                      else rec.get("brightness", 1.0))
        gam = (s.gamma if s.gamma is not None
               else tuple(rec.get("gamma", (1.0, 1.0, 1.0))))
        err = gammamod.set_output_gamma(sess.state, s.name, brightness, gam,
                                        wayland_socket=sock)
        changed = True
        if err == "refused":
            sess.state.save()
            raise Fatal("Gamma size is 0.\n")
        if err is not None:
            sess.state.save()
            raise Fatal("cannot set gamma: %s\n" % err)
    if changed:
        sess.state.save()


def _do_setit_1_2(sess: Session, opts: Opts, outputs):
    filter_cmds = []
    for s in opts.stanzas:
        for prop, val in s.props:
            if prop == "__filter":
                if sess.backend == "sway":
                    filter_cmds.append("output %s scale_filter %s" % (
                        s.name, "linear" if val == "bilinear" else val))
                else:
                    core.warn("--filter needs the sway backend; ignoring\n")
            else:
                core.warn("--set %s is not supported on Wayland; ignoring\n"
                          % prop)
    targets = core.build_targets(outputs, opts.stanzas, sess.state,
                                 opts.global_auto)
    dims = {t.name: sess.dims(t) for t in targets if t.enabled}
    pos = core.resolve_positions(targets, dims)
    _check_fb(opts, targets, dims, pos)
    _check_screen_size(opts, targets, dims, pos)
    if opts.verbose:
        _print_plan(opts, outputs, targets, dims, pos)
    if opts.noprimary:
        if (sess.backend == "mutter" and sess.mutter.primary
                and not any(s.primary for s in opts.stanzas)):
            core.warn("GNOME requires a primary output; keeping %s\n"
                      % sess.mutter.primary)
        sess.state.primary = None
    for s in opts.stanzas:
        if s.primary and any(t.name == s.name and t.stanza is s
                             for t in targets):
            sess.state.primary = s.name
    if opts.dryrun:
        if sess.backend == "mutter":
            # method 0: Mutter validates the exact call a real run would
            # make (adjacency, overlap, primary, scales); a rejection is the
            # same one-line `xrandr: <mutter message>` the apply would give
            sess.mutter.verify(sess.state, targets)
            print("mutter verify: ok")
        sess.state.save()
        return outputs
    for cmd in filter_cmds:
        sess.ipc.run(cmd)
    fresh = sess.apply(targets)
    still = {o.name for o in fresh}
    if sess.state.primary and sess.state.primary not in still:
        sess.state.primary = None
    sess.state.save()
    _apply_gamma(sess, opts, outputs)
    return fresh


def _do_1_0(sess: Session, opts: Opts, outputs) -> int:
    """The RandR 1.0 path: -s/-o/-x/-y/global -r against the first output."""
    if not outputs:
        raise Fatal("cannot find preferred mode\n")
    first = outputs[0]
    sizes = core.q1_sizes(outputs)
    if isinstance(opts.size, tuple):
        w, h = opts.size
        idx = next((i for i, (sw, sh, _r) in enumerate(sizes)
                    if (sw, sh) == (w, h)), None)
        if idx is None:
            sys.stderr.write("Size %dx%d not found in available modes\n"
                             % (w, h))
            return 1
    elif opts.size >= 0:
        if opts.size >= len(sizes):
            sys.stderr.write("Size index %d is too large, there are only "
                             "%d sizes\n" % (opts.size, len(sizes)))
            return 1
        idx = opts.size
    else:
        idx = None
        if first.current is not None:
            for i, (sw, sh, _r) in enumerate(sizes):
                if (sw, sh) == (first.current.w, first.current.h):
                    idx = i
        if idx is None:
            idx = 0
    if opts.rate >= 0 and sizes:
        rates = sizes[idx][2]
        if rates and round(opts.rate) not in rates:
            sys.stderr.write("Rate %.2f Hz not available for this size\n"
                             % opts.rate)
            return 1
    cur_rot, cur_refl = core.RANDR_VIEW.get(first.transform,
                                            ("normal", "normal"))
    rot = _DIRECTION[opts.rot] if opts.rot >= 0 else cur_rot
    refl = set()
    if "x" in cur_refl:
        refl.add("x")
    if cur_refl in ("y", "xy"):
        refl.add("y")
    if opts.toggle_x:
        refl.symmetric_difference_update("x")
    if opts.toggle_y:
        refl.symmetric_difference_update("y")
    new_refl = ("xy" if refl == {"x", "y"} else
                "x" if refl == {"x"} else "y" if refl == {"y"} else "normal")
    if opts.query or opts.query_1:
        for line in core.render_q1(outputs, sess.state):
            print(line)
    if opts.query:
        for line in core.render_q1_state(first):
            print(line)
    if opts.verbose and opts.setit:
        print("Setting size to %d, rotation to %s" % (idx, rot))
        refl_word = ("X Axis " if new_refl == "x" else
                     "Y Axis" if new_refl == "y" else
                     "X Axis Y Axis" if new_refl == "xy" else "neither axis")
        print("Setting reflection on %s" % refl_word)
    if not opts.setit or opts.dryrun:
        return 0
    s = Stanza(name=first.name)
    if sizes:
        s.mode = "%dx%d" % (sizes[idx][0], sizes[idx][1])
    if opts.rate >= 0:
        s.rate = opts.rate
    s.rotate = rot
    s.reflect = new_refl
    targets = core.build_targets(outputs, [s], sess.state)
    sess.apply(targets)
    sess.state.save()
    return 0


# -- entry point --------------------------------------------------------------

def _run(argv) -> int:
    opts = parse(argv)
    if not opts.action:
        opts.query = True
    if opts.verbose:
        opts.query = True
        if opts.setit and not opts.setit_1_2:
            opts.query_1 = True
    if opts.version:
        print("xrandr program version       " + core.PROGRAM_VERSION)
    sess = Session()
    if opts.persistent:
        sess.persistent = True
    if opts.screen > 0:
        sys.stderr.write("Invalid screen number %d (display has 1)\n"
                         % opts.screen)
        return 1
    if opts.version:
        print("Server reports RandR version 1.6")
    outputs = sess.snapshot()
    if opts.mode_ops:
        _do_mode_ops(sess, opts, outputs)
        if not (opts.setit_1_2 or opts.monitor_op or opts.props):
            return 0
        outputs = sess.snapshot()
    if opts.providers:
        for line in core.render_providers(outputs, sess.compositor_name):
            print(line)
        return 0
    if opts.monitor_op:
        if opts.monitor_op[0] in ("list", "listactive"):
            for line in core.render_monitors(outputs, sess.state,
                                             sess.backend == "mutter"):
                print(line)
            return 0
        if opts.monitor_op[0] == "del":
            print("No monitor named '%s'" % opts.monitor_op[1])
            return 0
    if opts.setit_1_2:
        _do_setit_1_2(sess, opts, outputs)
        # xrandr exits right after apply() — no query follows a 1.2 set,
        # even under --verbose/--dryrun (xrandr.c:3654, oracle capture
        # xrandr2-dryrun-mode).
        return 0
    if opts.setit and not opts.setit_1_2:
        return _do_1_0(sess, opts, outputs)
    if opts.query_1 and not opts.setit:
        for line in core.render_q1(outputs, sess.state):
            print(line)
        for line in core.render_q1_state(outputs[0] if outputs else
                                         core.OutputState("none", False)):
            print(line)
        return 0
    if opts.query:
        for line in core.render_query(outputs, sess.state,
                                      verbose=opts.verbose, props=opts.props,
                                      fb=opts.fb):
            print(line)
    return 0


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        code = _run(list(argv))
        sys.stdout.flush()
        return code
    except ArgErr as e:
        sys.stderr.write("xrandr: %s" % e.args[0])
        sys.stderr.write("Try 'xrandr --help' for more information.\n")
        return 1
    except Fatal as e:
        sys.stderr.write("xrandr: %s" % e.args[0])
        return 1
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # never a traceback: an out-of-range --pos/--rate/--scale that trips a
        # struct pack, a lost compositor connection mid-apply, malformed IPC —
        # all become one-line xrandr: fatals, like the real thing.
        sys.stderr.write("xrandr: %s\n" % e)
        return 1
