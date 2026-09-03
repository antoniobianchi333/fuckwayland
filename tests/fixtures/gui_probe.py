#!/usr/bin/env python3
"""In-process checks of the warandr editor under an X display (the GUI test
runs this under its Xvfb): Apply must not block the main loop, a failed
Apply keeps the edits, popup menus are released, zoom keeps the View radios
in step, the per-output menu has arandr's order and Scale only on Wayland.
Prints one JSON object; a stub backend stands in for xrandr."""

import gc
import json
import os
import sys
import time
import weakref

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import gi                                                        # noqa: E402
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk                              # noqa: E402

from warandr import gui, randr, xrandr_parse                     # noqa: E402
from warandr.model import Layout                                 # noqa: E402

ROTS = "(normal left inverted right x axis y axis)"
TEXT = ("Screen 0: minimum 8 x 8, current 3200 x 1080, maximum 32767 x 32767\n"
        "DP-1 connected primary 1920x1080+0+0 %s 598mm x 336mm\n"
        "   1920x1080     60.00*+  50.00  \n"
        "HDMI-1 connected 1280x1024+1920+0 %s 376mm x 301mm\n"
        "   1280x1024     60.02*+\n" % (ROTS, ROTS))
SLEEP = 1.5


class Stub(randr.Backend):
    def __init__(self, wayland=False, rc=0):
        super().__init__(["stub"], wayland, env={}, source="stub")
        self.rc = rc
        self.snapshots = 0
        self.applied = []

    def snapshot(self):
        self.snapshots += 1
        return Layout.from_screen(xrandr_parse.parse(TEXT),
                                  hidpi=self.wayland, command_word=self.word)

    def apply(self, layout):
        self.applied.append(layout.args())
        time.sleep(SLEEP)
        return self.rc, "", "stub: configure crtc failed" if self.rc else ""


def pump(until, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not until():
        Gtk.main_iteration_do(True)
    return until()


def menu_labels(menu):
    out = []
    for it in menu.get_children():
        if isinstance(it, Gtk.SeparatorMenuItem):
            out.append("-")
        elif isinstance(it, Gtk.MenuItem):
            out.append((it.get_label() or "").replace("_", ""))
    return out


def main():
    res = {}
    dialogs = []
    gui._msg = lambda parent, kind, text, buttons=None: dialogs.append(text) \
        or Gtk.ResponseType.CLOSE

    # -- Apply on a worker thread: the main loop keeps ticking ---------------
    ticks = []
    GLib.timeout_add(50, lambda: ticks.append(time.monotonic()) or True)
    backend = Stub()
    app = gui.Application(backend)
    pump(lambda: len(ticks) > 3)
    app.layout.template = ["#!/bin/sh", "# mine", "%(xrandr)s"]
    t0 = time.monotonic()
    app.do_apply()
    res["apply_returned_s"] = round(time.monotonic() - t0, 3)
    res["busy_during"] = app._busy and \
        not app.toolbuttons["apply"].get_sensitive() and \
        not app.apply_item.get_sensitive()
    res["status_during"] = app.status_text()
    app.do_apply()                      # a second click while busy: ignored
    start = len(ticks)
    ok = pump(lambda: not app._busy, timeout=SLEEP + 10)
    res["apply_finished"] = ok
    window = ticks[max(start - 1, 0):]
    gaps = [b - a for a, b in zip(window, window[1:])]
    res["longest_gap_s"] = round(max(gaps), 3) if gaps else None
    res["ticks_during_apply"] = len(window)
    res["applied_calls"] = len(backend.applied)
    res["snapshots_after_ok"] = backend.snapshots        # 1 startup + 1
    res["template_kept"] = app.layout.template == \
        ["#!/bin/sh", "# mine", "%(xrandr)s"]
    res["apply_button_back"] = app.toolbuttons["apply"].get_sensitive()

    # -- a failed Apply keeps the edited layout ------------------------------
    backend.rc = 1
    app.layout.set_rotation("HDMI-1", "left")
    app.redraw()
    before = app.layout.args()
    app.do_apply()
    pump(lambda: not app._busy, timeout=SLEEP + 10)
    pump(lambda: bool(dialogs), timeout=5)
    res["fail_dialog"] = dialogs[-1] if dialogs else None
    res["fail_keeps_edits"] = app.layout.args() == before and \
        app.layout.get("HDMI-1").rotation == "left"
    res["snapshots_after_fail"] = backend.snapshots       # unchanged
    backend.rc = 0

    # -- popup menus are released ---------------------------------------------
    # (a programmatic popup gets no pointer grab under Xvfb, so the menu is
    # closed through deactivate() — what a click or Escape does)
    refs = []
    for _ in range(5):
        app.popup_output_menu("DP-1", None)
        refs.append(weakref.ref(app._popup))
        pump(lambda: False, timeout=0.2)
        app._popup.deactivate()
        pump(lambda: app._popup is None, timeout=3)
    gc.collect()
    res["popup_released"] = app._popup is None
    res["popups_alive"] = sum(1 for r in refs if r() is not None)

    # -- zoom radios follow Zoom In/Out ---------------------------------------
    res["zooms"] = list(gui.ZOOMS)
    app.zoom_in()
    app.zoom_in()
    app.zoom_in()
    res["zoom_in_factor"] = app.factor
    res["zoom_in_radio"] = [f for f, r in app._zoom_items.items()
                            if r.get_active()]
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    app.zoom_out()
    res["zoom_out_factor"] = app.factor
    res["zoom_out_radio"] = [f for f, r in app._zoom_items.items()
                             if r.get_active()]

    # -- per-output menu: arandr's order; Scale only on Wayland ---------------
    res["menu_x11"] = menu_labels(app.output_menu("DP-1"))
    app.window.destroy()
    wapp = gui.Application(Stub(wayland=True))
    pump(lambda: wapp.layout is not None)
    res["menu_wayland"] = menu_labels(wapp.output_menu("DP-1"))
    res["wayland_word"] = wapp.status_text().split()[0]
    wapp.window.destroy()
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
