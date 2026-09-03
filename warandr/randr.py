"""Backend runner: picks the RandR command (xrandr on X11, wxrandr on
Wayland), snapshots the screen, applies a layout.

Selection (first match wins):

1. ``$WARANDR_XRANDR`` — a command line (shlex-split) to run instead;
   its kind is Wayland when it mentions ``wxrandr``, X11 otherwise, unless
   ``$WARANDR_BACKEND`` (``x11`` / ``wayland``) says so explicitly.
2. a Wayland session (``passthrough.session_kind()``: ``$WAYLAND_DISPLAY``
   with a live socket, logind, the runtime dirs -- a stale ``WAYLAND_DISPLAY``
   with no compositor behind it is *not* one) and the ``wxrandr`` package
   importable
   (repo checkout or the pyz that bundles it): the *same* interpreter with
   ``-m wxrandr``, PYTHONPATH pointing at wherever the package was found.
3. a Wayland session and ``wxrandr`` on PATH.
4. ``xrandr``.
"""

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys

from wdotool import passthrough

from . import xrandr_parse
from .model import Layout


class RandrError(Exception):
    pass


class Backend:
    def __init__(self, argv, wayland, env=None, source="auto"):
        self.argv = list(argv)
        self.wayland = wayland
        self.env = dict(env if env is not None else os.environ)
        self.source = source

    @property
    def word(self):
        """The command word written into layout scripts."""
        return "wxrandr" if self.wayland else "xrandr"

    def describe(self):
        return "%s (%s)" % (" ".join(shlex.quote(a) for a in self.argv),
                            "Wayland" if self.wayland else "X11")

    def set_display(self, display):
        """arandr's --randr-display: talk to another display while the GUI
        stays on the one from the environment."""
        if display:
            self.env["WAYLAND_DISPLAY" if self.wayland else "DISPLAY"] = \
                display

    def run(self, args, timeout=30):
        cmd = self.argv + list(args)
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=self.env,
                               timeout=timeout)
        except OSError as e:
            raise RandrError("cannot run %s: %s" % (cmd[0], e))
        except subprocess.TimeoutExpired:
            raise RandrError("%s did not finish within %ds"
                             % (cmd[0], timeout))
        return (p.returncode, p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))

    def query(self, verbose=True):
        rc, out, err = self.run(["--verbose"] if verbose else ["--query"])
        if rc != 0:
            raise RandrError("%s failed (%d): %s"
                             % (self.word, rc, err.strip() or out.strip()))
        return out

    def snapshot(self):
        """The current screen as a Layout."""
        text = self.query(verbose=True)
        try:
            screen = xrandr_parse.parse(text)
        except xrandr_parse.ParseError as e:
            raise RandrError("cannot parse %s output: %s" % (self.word, e))
        if not screen.outputs and "Screen" not in text:
            raise RandrError("no RandR output from %s" % self.word)
        return Layout.from_screen(screen, hidpi=self.wayland,
                                  command_word=self.word)

    def apply(self, layout):
        """Run the layout's command line; (rc, stdout, stderr)."""
        return self.run(layout.args())


def _package_root(name):
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        return None
    pkgdir = list(spec.submodule_search_locations)[0]
    return os.path.dirname(pkgdir.rstrip(os.sep))


def choose(env=None):
    env = dict(os.environ if env is None else env)
    kind = env.get("WARANDR_BACKEND", "").strip().lower()
    override = env.get("WARANDR_XRANDR", "").strip()
    if override:
        argv = shlex.split(override)
        if not argv:
            raise RandrError("WARANDR_XRANDR is empty")
        wayland = any("wxrandr" in os.path.basename(a) for a in argv)
        source = "WARANDR_XRANDR"
    # respect_override=False: $FUCKWAYLAND_PASSTHROUGH says what to do about
    # *handing over to the original*, and warandr never hands over -- it only
    # picks a command word. Honouring `never` here would answer "wayland" on
    # an X11 box and select wxrandr, i.e. break warandr for exactly the
    # developers the variable is documented for.
    elif passthrough.session_kind(env=env, respect_override=False) == "wayland":
        root = _package_root("wxrandr")
        if root is not None:
            argv = [sys.executable, "-m", "wxrandr"]
            old = env.get("PYTHONPATH")
            env["PYTHONPATH"] = root + (os.pathsep + old if old else "")
            source = "wxrandr package at %s" % root
        elif shutil.which("wxrandr"):
            argv = ["wxrandr"]
            source = "wxrandr on PATH"
        else:
            argv = ["xrandr"]
            source = "xrandr (no wxrandr found; XWayland view)"
        wayland = argv[-1] == "wxrandr"
    else:
        argv = ["xrandr"]
        wayland = False
        source = "xrandr (X11 session)"
    if kind in ("x11", "wayland"):
        wayland = kind == "wayland"
    return Backend(argv, wayland, env=env, source=source)
