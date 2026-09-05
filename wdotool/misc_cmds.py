"""Misc commands: exec, sleep, getdisplaygeometry (cmd_exec.c, cmd_sleep.c,
cmd_get_display_geometry.c)."""

import math
import re
import subprocess
import sys
import time

from wdotool.cli import ChainAbort, GetoptError, getopt_long_only
from wdotool.ctx import CmdError, NoSessionError

_ATOI_RE = re.compile(r"[ \t\n\r\f\v]*([+-]?\d+)")
_ATOF_RE = re.compile(
    r"[ \t\n\r\f\v]*[+-]?(?:0[xX][0-9a-fA-F]*(?:\.[0-9a-fA-F]*)?(?:[pP][+-]?\d+)?"
    r"|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    r"|[iI][nN][fF](?:[iI][nN][iI][tT][yY])?|[nN][aA][nN])"
)


def _atoi(s: str) -> int:
    m = _ATOI_RE.match(s or "")
    return int(m.group(1)) if m else 0


def _atof(s: str) -> float:
    """C atof(): parse a leading double (decimal, exponent, or hex float),
    0.0 when nothing parses."""
    m = _ATOF_RE.match(s or "")
    if not m:
        return 0.0
    t = m.group().strip()
    try:
        return float.fromhex(t) if re.match(r"[+-]?0[xX]", t) else float(t)
    except ValueError:
        return 0.0
    except OverflowError:
        # C strtod() saturates to +-HUGE_VAL and sets ERANGE; only the hex
        # spelling gets here, because float("1e400") already answers inf.
        # `sleep 0x1p1024` used to end in "hexadecimal value too large to
        # represent as a float" where the oracle returns 0 at once.
        return -math.inf if t.startswith("-") else math.inf


def _help_requested(opts) -> bool:
    return any(name in ("h", "help") for name, _ in opts)


def cmd_exec(ctx, args):
    cmd = getattr(ctx, "cmd_name", "exec")
    usage = (
        "Usage: %s [options] command [arg1 arg2 ...] [terminator]\n"
        "--sync    - only exit when the command given finishes. The default\n"
        "            is to fork a child process and continue.\n"
        "--args N  - how many arguments to expect in the exec command. This is\n"
        "            useful for ending an exec and continuing with more xdotool\n"
        "            commands\n"
        "--terminator TERM - similar to --args, specifies a terminator that\n"
        "                    marks the end of 'exec' arguments. This is useful\n"
        "                    for continuing with more xdotool commands.\n"
        "\n"
        "Unless --args OR --terminator is specified, the exec command is assumed\n"
        "to be the remainder of the command line." % cmd
    )
    try:
        opts, nopts = getopt_long_only(
            cmd, args, "h",
            [("help", False), ("sync", False), ("args", True), ("terminator", True)],
        )
    except GetoptError as e:
        if _help_requested(e.opts):
            print(usage)
            return len(args)
        raise CmdError("%s\n%s" % (e, usage)) from None

    opsync, arity, terminator = False, -1, None
    for name, val in opts:
        if name in ("h", "help"):
            print(usage)
            return len(args)
        elif name == "sync":
            opsync = True
        elif name == "args":
            arity = _atoi(val)
        elif name == "terminator":
            terminator = val

    rest = args[nopts:]
    if not rest:
        raise CmdError("No arguments given.\n%s" % usage)
    if arity > 0 and terminator is not None:
        raise CmdError("Don't use both --terminator and --args.")
    if len(rest) < arity:
        raise CmdError(
            "You said '--args %d' but only gave %d arguments." % (arity, len(rest))
        )

    command: list[str] = []
    command_count = 0
    for i, tok in enumerate(rest):
        if arity > 0 and i == arity:
            break
        if terminator is not None and tok == terminator:
            command_count += 1  # consume the terminator, too
            break
        command.append(tok)
        command_count = i + 1
    consumed = nopts + command_count

    try:
        if not command:
            raise OSError(14, "Bad address")  # execvp(NULL): EFAULT
        proc = subprocess.Popen(command)
    except OSError as e:
        sys.stderr.write("execvp failed: %s\n" % e.strerror)
        if opsync:
            # xdotool returns a fixed 22 here, not the child's errno.
            raise ChainAbort(22) from None
        return consumed

    if opsync:
        status = proc.wait()
        # C uses WEXITSTATUS: signal deaths read as 0 and do not abort.
        if status > 0:
            raise ChainAbort(status)
    return consumed


def cmd_sleep(ctx, args):
    cmd = getattr(ctx, "cmd_name", "sleep")
    usage = (
        "Usage: %s seconds\n"
        "Sleep a given number of seconds. Fractions of seconds are valid." % cmd
    )
    try:
        opts, nopts = getopt_long_only(cmd, args, "h", [("help", False)])
    except GetoptError as e:
        if _help_requested(e.opts):
            print(usage)
            return len(args)
        raise CmdError("%s\n%s" % (e, usage)) from None
    if _help_requested(opts):
        print(usage)
        return len(args)
    rest = args[nopts:]
    if not rest:
        raise CmdError("No arguments given.\n%s" % usage)
    # NaN passes straight through max() and inf overflows time_t: both used
    # to leave a traceback where the real tool sleeps for no time at all.
    secs = _atof(rest[0])
    if not math.isfinite(secs) or secs <= 0.0:
        secs = 0.0
    try:
        time.sleep(secs)
    except OverflowError:
        # `sleep 1e300`: finite, positive, and still far outside time_t.
        # The C code hands it to usleep(), whose useconds_t cannot hold
        # it either, and the real xdotool returns at once with status 0
        # (measured against 3.20160805.1: 1e300, inf and nan all take
        # 0.00 s).  A value that *does* fit is still slept, which is the
        # deliberate divergence already in this line: `sleep 3600` means
        # an hour here and wraps in usleep's 32-bit argument there.
        pass
    return nopts + 1


def cmd_getdisplaygeometry(ctx, args):
    cmd = getattr(ctx, "cmd_name", "getdisplaygeometry")
    usage = "Usage: %s" % cmd
    try:
        opts, nopts = getopt_long_only(
            cmd, args, "h", [("help", False), ("screen", True), ("shell", False)]
        )
    except GetoptError as e:
        if _help_requested(e.opts):
            print(usage)
            return len(args)
        raise CmdError("%s\n%s" % (e, usage)) from None
    shell = False
    for name, _val in opts:
        if name in ("h", "help"):
            print(usage)
            return len(args)
        elif name == "screen":
            pass  # single logical screen on Wayland; accepted and ignored
        elif name == "shell":
            shell = True
    w, h, guessed = ctx.daemon().geometry_status()
    if guessed:
        # B5: printing a made-up 1920x1080 with rc 0 when no compositor could
        # be reached made `getdisplaygeometry` useless as a session probe --
        # it was the one command that "succeeded" with no session at all.
        raise NoSessionError(
            "wdotool: no Wayland session found: cannot query the output "
            "layout (no compositor reachable); not guessing a display size"
        )
    if shell:
        sys.stdout.write("WIDTH=%d\nHEIGHT=%d\n" % (w, h))
    else:
        sys.stdout.write("%d %d\n" % (w, h))
    sys.stdout.flush()
    return nopts
