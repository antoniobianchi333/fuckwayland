"""warandr command line — arandr's (``warandr [savedfile]``, --version,
--randr-display, --force-version) plus non-GUI conveniences for scripts:
``--save FILE`` writes the current layout as a layout script, ``--command``
prints the command Apply would run."""

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
    return p


def load_layout(backend, savedfile):
    layout = backend.snapshot()
    if savedfile:
        with open(savedfile) as f:
            layout.load_script(f.read())
    return layout


def write_script(layout, path, word=None):
    if not path.endswith(".sh"):
        path += ".sh"
    with open(path, "w") as f:
        f.write(layout.to_script(word))
    os.chmod(path, stat.S_IRWXU)
    return path


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    args = _parser().parse_args(argv)
    try:
        backend = randr.choose()
        backend.set_display(args.randr_display)
        if args.save or args.command:
            layout = load_layout(backend, args.savedfile)
            if args.command:
                print(layout.command_line())
            if args.save:
                write_script(layout, args.save)
            return 0
    except (randr.RandrError, LayoutError, OSError) as e:
        sys.stderr.write("warandr: %s\n" % e)
        return 1
    try:
        from . import gui
    except (ImportError, ValueError, AttributeError) as e:
        sys.stderr.write(GTK_HINT % e)
        return 1
    return gui.run(backend, args.savedfile)
