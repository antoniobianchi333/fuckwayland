"""warandr command line — arandr's (``warandr [savedfile]``, --version,
--randr-display, --force-version) plus non-GUI conveniences for scripts:
``--save FILE`` writes the current layout as a layout script, ``--command``
prints the command Apply would run, ``--backend NAME`` pins the backend for
this run (the GUI's Layout ▸ Backend, spelled the same as wxrandr's own
flag) and ``--print-backend`` prints the backend token and exits; with
``--verbose`` it adds the whole of what the window's indicator explains,
again spelled like wxrandr's own ``--print-backend --verbose``."""

import argparse
import os
import stat
import sys

from . import VERSION, randr
from .model import LayoutError

GTK_HINT = ("warandr: GTK 3 for Python is not available (%s) - on Ubuntu/"
            "Debian: sudo apt install python3-gi gir1.2-gtk-3.0\n")


def _parser():
    p = argparse.ArgumentParser(
        prog="warandr", usage="%(prog)s [options] [savedfile]",
        description="Another XRandR GUI - on Wayland through wxrandr, on X11 "
                    "through xrandr.")
    p.add_argument("savedfile", nargs="?", help="layout script to open")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--randr-display", metavar="D",
                   help="Use D as display for xrandr/wxrandr (but still show "
                        "the GUI on the display from the environment; e.g. "
                        "`localhost:10.0` or `wayland-1`)")
    p.add_argument("--force-version", action="store_true",
                   help="Even run with untested XRandR versions (accepted for "
                        "arandr compatibility; warandr never refuses one)")
    p.add_argument("--save", metavar="FILE",
                   help="write the current layout (or SAVEDFILE re-based on "
                        "the current outputs) as a layout script and exit; "
                        "no GUI")
    p.add_argument("--command", action="store_true",
                   help="print the command Apply would run and exit; no GUI")
    p.add_argument("--backend", metavar="NAME",
                   help="force the backend for this run: %s (aliases gnome, "
                        "kde); auto is the default and picks the supported "
                        "one. Beats $WXRANDR_BACKEND, which beats detection"
                        % ", ".join(randr.BACKENDS))
    p.add_argument("--print-backend", action="store_true",
                   help="print the backend token (x11, sway, wlr, mutter, "
                        "kwin) and exit; no GUI")
    p.add_argument("--verbose", action="store_true",
                   help="with --print-backend: add what runs, why it was "
                        "picked, and what that tool says about the session")
    return p


def load_layout(backend, savedfile):
    layout = backend.snapshot()
    if savedfile:
        with open(savedfile) as f:
            layout.load_script(f.read())
    return layout


def script_notes(layout, backend):
    """The comment header a saved layout carries: the forced backend, if
    one was forced, and — when the layout has a partial overlap — what that
    overlap means on the backend that wrote it.  Comments only: the script
    still runs anywhere."""
    notes = []
    forced = backend.script_note()
    if forced:
        notes.append(forced)
    pairs = layout.overlaps()
    if pairs:
        # neither output is "over" the other -- on every backend that takes
        # an overlap both draw the shared region, which is the whole point --
        # so the note names the pair symmetrically and says which rectangle
        # they share, in xrandr's own WxH+X+Y spelling
        shared = []
        for a, b in pairs:
            x, y, w, h = layout.shared_region(a, b)
            shared.append("%s and %s share %dx%d at +%d+%d"
                          % (a, b, w, h, x, y))
        notes.append("warandr: partial overlap (%s)" % "; ".join(shared))
        notes.append(backend.overlap_note())
    return notes


def write_script(layout, path, word=None, notes=None):
    if not path.endswith(".sh"):
        path += ".sh"
    with open(path, "w") as f:
        f.write(layout.to_script(word, notes))
    os.chmod(path, stat.S_IRWXU)
    return path


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = _parser().parse_args(argv)
    try:
        backend = randr.choose(forced=args.backend)
        backend.set_display(args.randr_display)
        if args.print_backend:
            backend.identify()
            for line in (backend.report() if args.verbose
                         else [backend.name]):
                print(line)
            return 0
        if args.save or args.command:
            # which backend this really is decides whether an overlapping
            # layout is refused and in whose name, so ask before reading
            # one; `auto` on Wayland is only "wxrandr" until it has.  (The
            # window asks off the main loop instead, and patches the layout
            # when the answer lands.)
            backend.identify()
            layout = load_layout(backend, args.savedfile)
            if args.command:
                print(layout.command_line(backend.run_word))
            if args.save:
                write_script(layout, args.save, backend.word,
                             script_notes(layout, backend))
            return 0
    except (randr.RandrError, LayoutError, OSError) as e:
        sys.stderr.write("warandr: %s\n" % e)
        return 1
    try:
        from . import gui
    except (ImportError, ValueError, AttributeError) as e:
        sys.stderr.write(GTK_HINT % e)
        return 1
    return gui.run(backend, args.savedfile, args.randr_display)
