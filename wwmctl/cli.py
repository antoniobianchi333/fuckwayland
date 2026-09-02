"""OWNER: Agent W. wmctrl-compatible option parsing and dispatch.

Byte-parity target: wmctrl 1.07 (the binary in the devshell is the oracle;
its --help output is embedded verbatim below, and error strings/exit codes
follow main.c). Deliberate deviations, all documented in the tests:
- -h / -V / --help / --version work without a session (real wmctrl needs an
  open X display for -h and -V),
- "Cannot open display." becomes the backend detector's actual error text,
- acting on a window id that does not exist exits 1 silently (real wmctrl
  fires the ClientMessage into the void and exits 0).
"""

import getopt
import os
import sys

from wdotool.ctx import CmdError

from wwmctl import VERSION, core

# vanilla wmctrl 1.07 optstring
OPTSTRING = "FGVvhlupidmxa:r:s:c:t:w:k:o:n:g:e:b:N:I:T:R:"

HELP = '''wmctrl 1.07
Usage: wmctrl [OPTION]...
Actions:
  -m                   Show information about the window manager and
                       about the environment.
  -l                   List windows managed by the window manager.
  -d                   List desktops. The current desktop is marked
                       with an asterisk.
  -s <DESK>            Switch to the specified desktop.
  -a <WIN>             Activate the window by switching to its desktop and
                       raising it.
  -c <WIN>             Close the window gracefully.
  -R <WIN>             Move the window to the current desktop and
                       activate it.
  -r <WIN> -t <DESK>   Move the window to the specified desktop.
  -r <WIN> -e <MVARG>  Resize and move the window around the desktop.
                       The format of the <MVARG> argument is described below.
  -r <WIN> -b <STARG>  Change the state of the window. Using this option it's
                       possible for example to make the window maximized,
                       minimized or fullscreen. The format of the <STARG>
                       argument and list of possible states is given below.
  -r <WIN> -N <STR>    Set the name (long title) of the window.
  -r <WIN> -I <STR>    Set the icon name (short title) of the window.
  -r <WIN> -T <STR>    Set both the name and the icon name of the window.
  -k (on|off)          Activate or deactivate window manager's
                       "showing the desktop" mode. Many window managers
                       do not implement this mode.
  -o <X>,<Y>           Change the viewport for the current desktop.
                       The X and Y values are separated with a comma.
                       They define the top left corner of the viewport.
                       The window manager may ignore the request.
  -n <NUM>             Change number of desktops.
                       The window manager may ignore the request.
  -g <W>,<H>           Change geometry (common size) of all desktops.
                       The window manager may ignore the request.
  -h                   Print help.

Options:
  -i                   Interpret <WIN> as a numerical window ID.
  -p                   Include PIDs in the window list. Very few
                       X applications support this feature.
  -G                   Include geometry in the window list.
  -x                   Include WM_CLASS in the window list or
                       interpret <WIN> as the WM_CLASS name.
  -u                   Override auto-detection and force UTF-8 mode.
  -F                   Modifies the behavior of the window title matching
                       algorithm. It will match only the full window title
                       instead of a substring, when this option is used.
                       Furthermore it makes the matching case sensitive.
  -v                   Be verbose. Useful for debugging.
  -w <WA>              Use a workaround. The option may appear multiple
                       times. List of available workarounds is given below.

Arguments:
  <WIN>                This argument specifies the window. By default it's
                       interpreted as a string. The string is matched
                       against the window titles and the first matching
                       window is used. The matching isn't case sensitive
                       and the string may appear in any position
                       of the title.

                       The -i option may be used to interpret the argument
                       as a numerical window ID represented as a decimal
                       number. If it starts with "0x", then
                       it will be interpreted as a hexadecimal number.

                       The -x option may be used to interpret the argument
                       as a string, which is matched against the window's
                       class name (WM_CLASS property). Th first matching
                       window is used. The matching isn't case sensitive
                       and the string may appear in any position
                       of the class name. So it's recommended to  always use
                       the -F option in conjunction with the -x option.

                       The special string ":SELECT:" (without the quotes)
                       may be used to instruct wmctrl to let you select the
                       window by clicking on it.

                       The special string ":ACTIVE:" (without the quotes)
                       may be used to instruct wmctrl to use the currently
                       active window for the action.

  <DESK>               A desktop number. Desktops are counted from zero.

  <MVARG>              Specifies a change to the position and size
                       of the window. The format of the argument is:

                       <G>,<X>,<Y>,<W>,<H>

                       <G>: Gravity specified as a number. The numbers are
                          defined in the EWMH specification. The value of
                          zero is particularly useful, it means "use the
                          default gravity of the window".
                       <X>,<Y>: Coordinates of new position of the window.
                       <W>,<H>: New width and height of the window.

                       The value of -1 may appear in place of
                       any of the <X>, <Y>, <W> and <H> properties
                       to left the property unchanged.

  <STARG>              Specifies a change to the state of the window
                       by the means of _NET_WM_STATE request.
                       This option allows two properties to be changed
                       simultaneously, specifically to allow both
                       horizontal and vertical maximization to be
                       altered together.

                       The format of the argument is:

                       (remove|add|toggle),<PROP1>[,<PROP2>]

                       The EWMH specification defines the
                       following properties:

                           modal, sticky, maximized_vert, maximized_horz,
                           shaded, skip_taskbar, skip_pager, hidden,
                           fullscreen, above, below

Workarounds:

  DESKTOP_TITLES_INVALID_UTF8      Print non-ASCII desktop titles correctly
                                   when using Window Maker.

The format of the window list:

  <window ID> <desktop ID> <client machine> <window title>

The format of the desktop list:

  <desktop ID> [-*] <geometry> <viewport> <workarea> <title>


Author, current maintainer: Tomas Styblo <tripie@cpan.org>
Released under the GNU General Public License.
Copyright (C) 2003
'''


def _prog() -> str:
    if sys.argv and sys.argv[0]:
        return os.path.basename(sys.argv[0])
    return "wwmctl"


def _envir_utf8(force: bool) -> bool:
    """wmctrl's init_charset(): locale charset, with LC_CTYPE/LANG able to
    force UTF-8 on. Verbose-only cosmetics for us (we always emit UTF-8)."""
    if force:
        return True
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = (os.environ.get(var) or "").upper()
        if "UTF8" in val or "UTF-8" in val:
            return True
    return False


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # wmctrl special-cases exactly `wmctrl --help` / `wmctrl --version`
    if len(argv) == 1:
        if argv[0] == "--help":
            sys.stdout.write(HELP)
            return 0
        if argv[0] == "--version":
            print(VERSION)
            return 0

    try:
        opts, _positional = getopt.gnu_getopt(argv, OPTSTRING)
    except getopt.GetoptError as e:
        opt = getattr(e, "opt", "") or ""
        if len(opt) > 1:  # a --long option we do not know
            sys.stderr.write("%s: unrecognized option '--%s'\n"
                             % (_prog(), opt))
        elif "requires argument" in str(e):
            sys.stderr.write("%s: option requires an argument -- '%s'\n"
                             % (_prog(), opt))
        else:
            sys.stderr.write("%s: invalid option -- '%s'\n" % (_prog(), opt))
        return 1

    if not opts:  # wmctrl: "missing_option" -> full help on stderr
        sys.stderr.write(HELP)
        return 1

    show_pid = show_geometry = show_class = False
    match_by_id = match_by_cls = full_match = False
    verbose = force_utf8 = False
    param_window = None
    param = None
    action = None

    for name, val in opts:
        c = name[1:]
        if c == "F":
            full_match = True
        elif c == "G":
            show_geometry = True
        elif c == "i":
            match_by_id = True
        elif c == "v":
            verbose = True
        elif c == "u":
            force_utf8 = True
        elif c == "x":
            match_by_cls = True
            show_class = True
        elif c == "p":
            show_pid = True
        elif c in ("a", "c", "R"):
            param_window = val
            action = c
        elif c == "r":
            param_window = val
        elif c in ("t", "e", "b", "N", "I", "T", "s", "k", "o", "n", "g"):
            param = val
            action = c
        elif c == "w":
            if val != "DESKTOP_TITLES_INVALID_UTF8":
                sys.stderr.write("Unknown workaround: %s\n" % val)
                return 1
        else:  # V h l d m
            action = c

    if verbose:
        sys.stderr.write("envir_utf8: %d\n" % int(_envir_utf8(force_utf8)))

    if action == "V":
        print(VERSION)
        return 0
    if action == "h":
        sys.stdout.write(HELP)
        return 0
    if action is None:  # e.g. plain `wwmctl -p`: options but nothing to do
        return 0

    ctl = core.Core(verbose=verbose)
    try:
        if action == "l":
            return ctl.list_windows(show_pid, show_geometry, show_class)
        if action == "d":
            return ctl.list_desktops()
        if action == "m":
            return ctl.wm_info()
        if action == "s":
            return ctl.switch_desktop(param or "")
        if action == "k":
            return ctl.showing_desktop(param or "")
        if action == "o":
            return ctl.change_viewport(param or "")
        if action == "n":
            return ctl.change_number_of_desktops(param or "")
        if action == "g":
            return ctl.change_geometry(param or "")
        # window actions
        if param_window is None:
            sys.stderr.write("No window was specified.\n")
            return 1
        return ctl.action_window(action, param_window, param,
                                 match_by_id, match_by_cls, full_match)
    except CmdError as e:
        sys.stderr.write("%s\n" % e)
        return 1
    except (BrokenPipeError, KeyboardInterrupt):
        return 1
