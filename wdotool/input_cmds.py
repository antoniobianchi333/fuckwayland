"""Input-injection commands: key/keydown/keyup/type, mouse, getmouselocation.

Command functions follow the ctx.py contract: consume their own flags and
positionals from `args`, return the token count consumed (excluding the
command name), raise CmdError to abort the chain.
"""

import math
import sys
import time

from wdotool import commands
from wdotool.cli import ChainAbort
from wdotool.ctx import CmdError

# ---------------------------------------------------------------------------
# option parsing, via cli.py's glibc getopt_long_only clone


def _parse(cmdname, args, usage, shortopts, longopts, shortmap=None):
    """Parse a command's leading options with cli.py's getopt_long_only clone.

    longopts: [(name, takes_arg), ...]; shortmap maps short chars to the long
    name used in the returned dict. Returns (opts dict, tokens consumed,
    help_requested). Mirrors the C loops: a --help seen before any bad option
    wins; a bad option raises CmdError carrying getopt's message + usage."""
    from wdotool import cli

    shortmap = shortmap or {}

    def convert(pairs):
        opts = {}
        for name, val in pairs:
            name = shortmap.get(name, name)
            if name == "help":
                return opts, True
            opts[name] = True if val is None else val
        return opts, False

    try:
        pairs, i = cli.getopt_long_only(cmdname, args, shortopts, longopts)
    except cli.GetoptError as e:
        _opts, want_help = convert(e.opts)
        if want_help:
            return {}, len(args), True
        raise CmdError(f"{e}\n" + usage.rstrip("\n")) from None
    opts, want_help = convert(pairs)
    return opts, i, want_help


def _atoi(s) -> int:
    """C atoi: leading whitespace, optional sign, leading digits, else 0."""
    s = str(s).strip()
    n = ""
    for idx, ch in enumerate(s):
        if ch in "+-" and idx == 0:
            n += ch
        elif ch.isdigit():
            n += ch
        else:
            break
    try:
        return int(n)
    except ValueError:
        return 0


def _strtonum(s) -> int:
    """C strtoul(s, NULL, 0): 0x/0-prefixed bases accepted."""
    try:
        return int(str(s).strip(), 0)
    except ValueError:
        return _atoi(s)


def _activate_settle(ctx, wid):
    """Approximation of send-to-window: activate the target, give the
    compositor a moment to move focus, then inject normally."""
    ctx.backend().activate(wid)
    time.sleep(0.05)


def _target_windows(ctx, window_arg):
    """Resolved window list for a --window value, or [None] for 'current'."""
    if window_arg is None:
        return [None]
    return ctx.resolve_windows(window_arg)


_ALL_MOD_KEYSYMS = "Shift_L+Shift_R+Control_L+Control_R+Alt_L+Alt_R+Super_L+Super_R"


def _clear_modifiers(ctx):
    ctx.daemon().key(_ALL_MOD_KEYSYMS, "up", 0, False)


# ---------------------------------------------------------------------------
# keyboard

_USAGE_KEY = """Usage: %s [options] <keysequence> [keysequence ...]
--clearmodifiers     - clear active keyboard modifiers during keystrokes
--delay DELAY        - Use DELAY milliseconds between keystrokes
--repeat TIMES       - How many times to repeat the key sequence
--repeat-delay DELAY - DELAY milliseconds between repetitions
--window WINDOW      - send keystrokes to a specific window
Each keysequence can be any number of modifiers and keys, separated by plus (+)
  For example: alt+r

Any letter or key symbol such as Shift_L, Return, Dollar, a, space are valid,
including those not currently available on your keyboard.

If no window is given, and there are windows in the stack, %%1 is used. Otherwise
the currently-focused window is used
"""


def _key_common(ctx, args, default_name, direction):
    cmdname = getattr(ctx, "cmd_name", default_name)
    usage = _USAGE_KEY % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "d:hcw:",
        [("clearmodifiers", False), ("delay", True), ("repeat-delay", True),
         ("help", False), ("window", True), ("repeat", True)],
        {"c": "clearmodifiers", "d": "delay", "h": "help", "w": "window"},
    )
    if want_help:
        print(usage, end="")
        return len(args)
    delay = _strtonum(opts.get("delay", 12))
    repeat = _atoi(opts.get("repeat", 1))
    if "repeat" in opts and repeat < 1:
        raise CmdError(f"Invalid '--repeat' value given: {opts['repeat']}")
    repeat_delay = _strtonum(opts.get("repeat-delay", 0))
    window_arg = opts.get("window")
    if window_arg is None and ctx.stack:
        window_arg = "%1"

    if i == len(args):
        raise CmdError("You specified the wrong number of args.\n" + usage.rstrip("\n"))

    seqs = []
    j = i
    while j < len(args) and not commands.is_command(args[j]):
        seqs.append(args[j])
        j += 1

    daemon = ctx.daemon()
    failed = 0
    for wid in _target_windows(ctx, window_arg):
        if wid is not None:
            _activate_settle(ctx, wid)
        clearmods = bool(opts.get("clearmodifiers"))
        for r in range(repeat):
            for seq in seqs:
                try:
                    daemon.key(seq, direction, delay, clearmods)
                    clearmods = False
                except CmdError as e:
                    if not str(e).startswith("Error: Invalid key sequence"):
                        raise
                    print(e, file=sys.stderr)
                    print(f"xdo_send_keysequence_window reported an error for string '{seq}'",
                          file=sys.stderr)
                    failed += 1
            if repeat_delay > 0 and r < repeat - 1:
                time.sleep(repeat_delay / 1000)
    if failed:
        # real xdotool aborts the chain, exiting with the failure count (the C
        # code sums the per-sequence keyfunc results and returns it)
        raise ChainAbort(failed)
    return j


def cmd_key(ctx, args):
    return _key_common(ctx, args, "key", "press")


def cmd_keydown(ctx, args):
    return _key_common(ctx, args, "keydown", "down")


def cmd_keyup(ctx, args):
    return _key_common(ctx, args, "keyup", "up")


_USAGE_TYPE = """Usage: %s [--window windowid] [--delay milliseconds] <things to type>
--window <windowid>    - specify a window to send keys to
--delay <milliseconds> - delay between keystrokes
--clearmodifiers       - reset active modifiers (alt, etc) while typing
--args N  - how many arguments to expect in the exec command. This is
            useful for ending an exec and continuing with more xdotool
            commands
--terminator TERM - similar to --args, specifies a terminator that
                    marks the end of 'exec' arguments. This is useful
                    for continuing with more xdotool commands.
--file <filepath> - specify a file, the contents of which will be
                    be typed as if passed as an argument. The filepath
                    may also be '-' to read from stdin.
-h, --help             - show this help output
If no window is given, %%1 is used. See WINDOW STACK in xdotool(1)
"""


def cmd_type(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "type")
    usage = _USAGE_TYPE % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "w:d:ch",
        [("clearmodifiers", False), ("delay", True), ("help", False),
         ("window", True), ("args", True), ("terminator", True), ("file", True)],
        {"c": "clearmodifiers", "d": "delay", "h": "help", "w": "window"},
    )
    if want_help:
        print(usage, end="")
        return len(args)
    delay = _strtonum(opts.get("delay", 12))
    window_arg = opts.get("window")
    arity = _atoi(opts["args"]) if "args" in opts else -1
    terminator = opts.get("terminator")
    file = opts.get("file")
    remaining = args[i:]

    if not remaining and file is None:
        raise CmdError("You specified the wrong number of args.\n" + usage.rstrip("\n"))
    if arity > 0 and terminator is not None:
        raise CmdError("Don't use both --terminator and --args.")
    if len(remaining) < arity:
        raise CmdError(f"You said '--args {arity}' but only gave {len(remaining)} arguments.")

    data = []
    if file is not None:
        try:
            if file == "-":
                data.append(sys.stdin.read())
            else:
                with open(file, "r", encoding="utf-8", errors="replace") as f:
                    data.append(f.read())
        except OSError as e:
            raise CmdError(f"Failure opening '{file}': {e.strerror}") from None

    consumed = 0
    for idx, arg in enumerate(remaining):
        if 0 < arity == idx:
            break
        if terminator is not None and arg == terminator:
            consumed += 1  # consume the terminator, too
            break
        data.append(arg)
        consumed += 1

    daemon = ctx.daemon()
    for wid in _target_windows(ctx, window_arg):
        if wid is not None:
            _activate_settle(ctx, wid)
        if opts.get("clearmodifiers"):
            _clear_modifiers(ctx)
        for piece in data:
            daemon.type_text(piece, delay)
    return i + consumed


# ---------------------------------------------------------------------------
# mouse

_USAGE_CLICK = """Usage: %s [options] <button>
--clearmodifiers       - reset active modifiers (alt, etc) while typing
--window WINDOW        - specify a window to send click to
--repeat REPEATS       - number of times to click. Default is 1
--delay MILLISECONDS   - delay in milliseconds between clicks.
    This has no effect if you do not use --repeat.
    Default is 100ms

Button is a button number. Generally, left = 1, middle = 2, 
right = 3, wheel up = 4, wheel down = 5
"""


def cmd_click(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "click")
    usage = _USAGE_CLICK % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "cw:h",
        [("clearmodifiers", False), ("help", False), ("window", True),
         ("delay", True), ("repeat", True)],
        {"c": "clearmodifiers", "w": "window", "h": "help"},
    )
    if want_help:
        print(usage, end="")
        return len(args)
    clearmods = bool(opts.get("clearmodifiers"))
    window_arg = opts.get("window")
    if window_arg is not None:
        clearmods = True  # quirk copied from cmd_click.c
    delay = _strtonum(opts.get("delay", 100))
    repeat = _atoi(opts.get("repeat", 1))
    if "repeat" in opts and repeat <= 0:
        raise CmdError(f"Invalid repeat value '{opts['repeat']}' (must be >= 1)\n"
                       + usage.rstrip("\n"))
    if i >= len(args):
        raise CmdError(usage.rstrip("\n") + "\nYou specified the wrong number of args.")
    button = _atoi(args[i])

    daemon = ctx.daemon()
    for wid in _target_windows(ctx, window_arg):
        if wid is not None:
            _activate_settle(ctx, wid)
        if clearmods:
            _clear_modifiers(ctx)
        daemon.click(button, repeat, delay)
    return i + 1


_USAGE_MOUSEBTN = """Usage: %s [--clearmodifiers] [--window WINDOW] <button>
--window <windowid>    - specify a window to send keys to
--clearmodifiers       - reset active modifiers (alt, etc) while typing
"""


def _mouse_updown(ctx, args, default_name, down, noargs_msg):
    cmdname = getattr(ctx, "cmd_name", default_name)
    usage = _USAGE_MOUSEBTN % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "chw:",
        [("clearmodifiers", False), ("help", False), ("window", True)],
        {"c": "clearmodifiers", "h": "help", "w": "window"},
    )
    if want_help:
        print(usage, end="")
        return len(args)
    if i >= len(args):
        raise CmdError(usage.rstrip("\n") + "\n" + noargs_msg)
    button = _atoi(args[i])

    daemon = ctx.daemon()
    for wid in _target_windows(ctx, opts.get("window")):
        if wid is not None:
            _activate_settle(ctx, wid)
        if opts.get("clearmodifiers"):
            _clear_modifiers(ctx)
        daemon.button(button, down)
    return i + 1


def cmd_mousedown(ctx, args):
    return _mouse_updown(ctx, args, "mousedown", True, "What button do you want me to send?")


def cmd_mouseup(ctx, args):
    return _mouse_updown(ctx, args, "mouseup", False, "You specified the wrong number of args.")


_USAGE_MOUSEMOVE = """Usage: %s [options] <x> <y>
-c, --clearmodifiers      - reset active modifiers (alt, etc) while typing
--screen SCREEN           - which screen to move on, default is current screen
--sync                    - only exit once the mouse has moved
-w, --window <windowid>   - specify a window to move relative to.
"""


def _polar_to_xy(angle, distance, origin_x, origin_y):
    radians = ((360 - angle) + 90) * math.pi / 180
    return (int(origin_x + math.cos(radians) * distance),
            int(origin_y + -math.sin(radians) * distance))


def cmd_mousemove(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "mousemove")
    usage = _USAGE_MOUSEMOVE % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "chw:pd:",
        [("clearmodifiers", False), ("help", False), ("polar", False),
         ("screen", True), ("sync", False), ("window", True)],
        {"c": "clearmodifiers", "h": "help", "w": "window", "p": "polar",
         "d": "_delay"},  # short -d parses (and is ignored) like the C code
    )
    if want_help:
        print(usage, end="")
        return len(args)

    if i >= len(args) or (args[i] != "restore" and len(args) - i < 2):
        raise CmdError(
            usage.rstrip("\n")
            + "\nYou specified the wrong number of args (expected 2 coordinates or 'restore')."
        )

    if args[i] == "restore":
        last = getattr(ctx, "_last_mouse", None)
        if last is None:
            raise CmdError("Have no previous mouse position. Cannot restore.")
        x, y = last
        consumed = 1
    else:
        x, y = _atoi(args[i]), _atoi(args[i + 1])
        consumed = 2

    window_arg = opts.get("window")
    daemon = ctx.daemon()
    for wid in _target_windows(ctx, window_arg):
        if wid is None:
            ctx._last_mouse = daemon.pointer()  # noqa: SLF001 — restore state
        tx, ty = x, y
        if opts.get("polar"):
            if wid is not None:
                win = ctx.backend().find(wid)
                origin = (win.x + win.w // 2, win.y + win.h // 2)
            else:
                gx, gy, gw, gh = daemon.geometry_full()
                origin = (gx + gw // 2, gy + gh // 2)
            tx, ty = _polar_to_xy(x, y, *origin)
        elif wid is not None:
            win = ctx.backend().find(wid)
            tx, ty = win.x + x, win.y + y
        if opts.get("clearmodifiers"):
            _clear_modifiers(ctx)
        daemon.mousemove_abs(tx, ty)
        # --sync: our injected position is authoritative, nothing to wait for
    return i + consumed


_USAGE_MOUSEMOVE_REL = """Usage: %s [options] <x> <y>
-c, --clearmodifiers      - reset active modifiers (alt, etc) while typing
-p, --polar               - Use polar coordinates. X as an angle, Y as distance
--sync                    - only exit once the mouse has moved

Using polar coordinate mode makes 'x' the angle (in degrees) and
'y' the distance.

If you want to use negative numbers for a coordinate, you'll need to
invoke it this way (with the '--'):
   %s -- -20 -15
otherwise, normal usage looks like this:
   %s 100 140
"""


def cmd_mousemove_relative(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "mousemove_relative")
    usage = _USAGE_MOUSEMOVE_REL % (cmdname, cmdname, cmdname)
    opts, i, want_help = _parse(
        cmdname, args, usage, "cph",
        [("help", False), ("sync", False), ("polar", False),
         ("clearmodifiers", False)],
        {"c": "clearmodifiers", "p": "polar", "h": "help"},
    )
    if want_help:
        print(usage, end="")
        return len(args)
    if len(args) - i < 2:
        raise CmdError(usage.rstrip("\n") + "\nYou specified the wrong number of args (expected 2).")
    x, y = _atoi(args[i]), _atoi(args[i + 1])
    if x == 0 and y == 0:
        return i + 2
    if opts.get("polar"):
        x, y = _polar_to_xy(x, y, 0, 0)
    if opts.get("clearmodifiers"):
        _clear_modifiers(ctx)
    ctx.daemon().mousemove_rel(x, y)
    return i + 2


_USAGE_GETMOUSELOCATION = """Usage: %s [--shell] [--prefix <STR>]
--shell      - output shell variables for use with eval
--prefix STR - use prefix for shell variables names (max 16 chars) 
"""


def _window_under_pointer(ctx, x, y) -> int:
    """Hit-test the daemon-tracked pointer against the backend's window list.
    Focused window wins, else the topmost (last listed) hit; 0 with no backend.
    A backend with a native hit-test (`window_at`, e.g. GNOME looking through
    desktop-icon and dock layers) is asked first; None means "use the generic
    rule"."""
    try:
        backend = ctx.backend()
        native = getattr(backend, "window_at", None)
        if native is not None:
            hit = native(x, y)
            if hit is not None:
                return int(hit)
        wins = backend.list()
    except Exception:
        return 0
    hits = [w for w in wins
            if w.visible and w.w > 0 and w.h > 0
            and w.x <= x < w.x + w.w and w.y <= y < w.y + w.h]
    if not hits:
        return 0
    for w in hits:
        if w.focused:
            return w.id
    return hits[-1].id


def cmd_getmouselocation(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "getmouselocation")
    usage = _USAGE_GETMOUSELOCATION % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "h",
        [("help", False), ("shell", False), ("prefix", True)],
        {"h": "help"},
    )
    if want_help:
        print(usage, end="")
        return len(args)
    x, y = ctx.daemon().pointer()
    window = _window_under_pointer(ctx, x, y)
    if opts.get("shell"):
        prefix = str(opts.get("prefix", ""))[:16]
        print(f"{prefix}X={x}")
        print(f"{prefix}Y={y}")
        print(f"{prefix}SCREEN=0")
        print(f"{prefix}WINDOW={window}")
    else:
        if i == len(args):  # only print if we're the last command
            print(f"x:{x} y:{y} screen:0 window:{window}")
        ctx.stack = [window]
    return i


_USAGE_BEHAVE_SCREEN_EDGE = """Usage: %s [options] edge-or-corner action [args...]
--delay MILLISECONDS     - delay before activating. During this time,
        your mouse must stay in the area selected (corner or edge)
        otherwise this timer will reset. Default is no delay (0).
--quiesce MILLISECONDS   - quiet time period after activating that no
        new activation will occur. This helps prevent accidental
        re-activation immediately after an event. Default is 2000 (2
        seconds).
edge-or-corner can be any of:
  Edges: left, top, right, bottom
  Corners: top-left, top-right, bottom-left, bottom-right
The action is any valid xdotool command (chains OK here)
"""

_EDGES = {"left", "top-left", "top", "top-right", "right",
          "bottom-right", "bottom", "bottom-left"}


def cmd_behave_screen_edge(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "behave_screen_edge")
    usage = _USAGE_BEHAVE_SCREEN_EDGE % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "h",
        [("help", False), ("delay", True), ("quiesce", True)],
        {"h": "help"},
    )
    if want_help:
        print(usage, end="")
        return len(args)
    if len(args) - i < 2:
        raise CmdError("Invalid number of arguments (minimum is 2)\n" + usage.rstrip("\n"))
    if args[i] not in _EDGES:
        raise CmdError(f"Invalid edge or corner, '{args[i]}'\n" + usage.rstrip("\n"))
    raise CmdError(
        "behave_screen_edge is not supported on Wayland: compositors do not "
        "expose global pointer motion"
    )
