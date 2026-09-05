#!/usr/bin/env python3
"""In-process checks of the warandr editor under an X display (the GUI test
runs this under its Xvfb): Apply must not block the main loop, a failed
Apply keeps the edits, a layout dump waits for GTK's allocation, popup menus
are released, zoom keeps the View radios in step, the per-output menu has
arandr's order and Scale only on Wayland, Save As warns when the script's
command word is not on PATH.  Prints one JSON object; a stub backend stands
in for xrandr."""

import gc
import json
import os
import stat
import sys
import tempfile
import time
import weakref

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from warandr import gui, randr, xrandr_parse
from warandr.model import Layout

ROTS = "(normal left inverted right x axis y axis)"
TEXT = ("Screen 0: minimum 8 x 8, current 3200 x 1080, maximum 32767 x 32767\n"
        "DP-1 connected primary 1920x1080+0+0 %s 598mm x 336mm\n"
        "   1920x1080     60.00*+  50.00  \n"
        "HDMI-1 connected 1280x1024+1920+0 %s 376mm x 301mm\n"
        "   1280x1024     60.02*+\n" % (ROTS, ROTS))
SLEEP = 1.5


class Stub(randr.Backend):
    def __init__(self, wayland=False, rc=0, read_delay=0.0, read_error=None,
                 apply_error=None):
        super().__init__(["stub"], wayland, env={}, source="stub")
        self.rc = rc
        self.apply_error = apply_error   # ...or fails some other way
        self.read_delay = read_delay     # a compositor that answers slowly
        self.read_error = read_error     # ...or not at all
        self.snapshots = 0
        self.applied = []

    def snapshot(self):
        self.snapshots += 1
        if self.read_delay:
            time.sleep(self.read_delay)
        if self.read_error:
            raise randr.RandrError(self.read_error)
        return Layout.from_screen(xrandr_parse.parse(TEXT),
                                  hidpi=self.wayland, command_word=self.word)

    def apply(self, layout):
        if self.apply_error:
            raise self.apply_error
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

    # -- a layout dump waits until GTK allocated the boxes --------------------
    # (HDMI-1 goes back to landscape: its box turns from 128x160 into
    # 160x128 only in the frame clock's layout phase, after the redraw; no
    # dump may report the old size)
    tmp = tempfile.mkdtemp(prefix="warandr-probe-")
    gui.DUMP = os.path.join(tmp, "dump.jsonl")

    def layout_dumps():
        try:
            with open(gui.DUMP) as f:
                return [d for d in (json.loads(ln) for ln in f if ln.strip())
                        if d["kind"] == "layout"]
        except OSError:
            return []
    before = len(layout_dumps())
    app.layout.set_rotation("HDMI-1", "normal")
    app.redraw()
    res["unsettled_after_redraw"] = not app._layout_settled()
    pump(lambda: len(layout_dumps()) > before, timeout=5)
    res["dumps_after_redraw"] = [[d["boxes"]["HDMI-1"][2:], d["settled"]]
                                 for d in layout_dumps()[before:]]
    res["settled_now"] = app._layout_settled()
    alloc = app.boxes["HDMI-1"].get_allocation()
    res["hdmi_alloc"] = [alloc.width, alloc.height]
    gui.DUMP = None

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

    # -- a redraw must not destroy the Outputs drop-down while it is open ----
    # (set_submenu() destroys the menu it replaces; destroying a mapped menu
    # destroys the window holding the pointer grab, and X keeps that grab)
    sub = app.outputs_menu_item.get_submenu()
    sub.popup_at_widget(app.outputs_menu_item, Gdk.Gravity.SOUTH_WEST,
                        Gdk.Gravity.NORTH_WEST, None)
    pump(lambda: sub.get_mapped(), timeout=5)
    res["outputs_menu_mapped"] = sub.get_mapped()
    app.layout.set_rotation("HDMI-1", "left")
    app.redraw()
    res["menu_kept_while_open"] = app.outputs_menu_item.get_submenu() is sub
    res["menu_still_mapped"] = sub.get_mapped()
    sub.deactivate()
    pump(lambda: app.outputs_menu_item.get_submenu() is not sub, timeout=5)
    fresh = app.outputs_menu_item.get_submenu()
    res["menu_rebuilt_on_close"] = fresh is not sub
    res["menu_rebuilt_items"] = menu_labels(fresh)
    app.layout.set_rotation("HDMI-1", "normal")
    app.redraw()

    # -- reads run off the main loop too, not only Apply ---------------------
    slow = Stub(read_delay=SLEEP)
    app.backend = slow
    ticks.clear()
    t0 = time.monotonic()
    app.do_new()                        # Ctrl+N: re-read the screen
    res["reload_returned_s"] = round(time.monotonic() - t0, 3)
    res["reload_busy"] = app._busy
    res["reload_status"] = app.status_text()
    start = len(ticks)
    res["reload_finished"] = pump(lambda: not app._busy, timeout=SLEEP + 10)
    window = ticks[max(start - 1, 0):]
    gaps = [b - a for a, b in zip(window, window[1:])]
    res["reload_longest_gap_s"] = round(max(gaps), 3) if gaps else None
    res["reload_snapshots"] = slow.snapshots

    # a read that fails keeps the layout that is on screen, and says so
    dialogs.clear()
    app.backend = Stub(read_error="stub: cannot open display")
    app.do_new()
    pump(lambda: not app._busy, timeout=10)
    pump(lambda: bool(dialogs), timeout=5)
    res["reload_fail_dialog"] = dialogs[-1] if dialogs else None
    res["reload_fail_keeps_layout"] = app.layout is not None
    app.backend = backend

    # -- a layout script that is not UTF-8 ----------------------------------
    # the reader thread caught three exception types and a
    # UnicodeDecodeError was none of them, so it died before finish()
    # could clear the busy flag: no dialog, and Apply/Open/New dead for
    # the rest of the session
    dialogs.clear()
    badtmp = tempfile.mkdtemp(prefix="warandr-latin1-")
    bad = os.path.join(badtmp, "latin1.sh")
    with open(bad, "wb") as f:
        f.write(b"#!/bin/sh\n# caf\xe9\nxrandr --output DP-1 "
                b"--mode 1920x1080 --pos 0x0 --rotate normal\n")
    app.load_file(bad)
    res["latin1_finished"] = pump(lambda: not app._busy, timeout=10)
    pump(lambda: bool(dialogs), timeout=5)
    res["latin1_dialog"] = dialogs[-1] if dialogs else None
    res["latin1_apply_live"] = app.toolbuttons["apply"].get_sensitive()
    res["latin1_keeps_layout"] = app.layout is not None
    app.do_new()                       # the window still works after it
    res["latin1_reload_after"] = pump(lambda: not app._busy, timeout=10)

    # -- an Apply that fails with anything but a RandrError -----------------
    dialogs.clear()
    app.backend = Stub(apply_error=OSError(13, "Permission denied"))
    app.do_apply()
    res["apply_boom_finished"] = pump(lambda: not app._busy, timeout=10)
    pump(lambda: bool(dialogs), timeout=5)
    res["apply_boom_dialog"] = dialogs[-1] if dialogs else None
    res["apply_boom_apply_live"] = \
        app.toolbuttons["apply"].get_sensitive()
    app.backend = backend

    # -- a menu built before an Apply edits the layout that is live now -----
    # (an Apply's re-read replaces app.layout wholesale; a menu still holding
    # the old object edited a layout nothing draws and nothing will apply)
    menu = app.output_menu("HDMI-1")
    orientation = [it for it in menu.get_children()
                   if (it.get_label() or "").replace("_", "") ==
                   "Orientation"][0]
    stale = app.layout
    app.set_layout(backend.snapshot())        # what _applied installs
    left = [r for r in orientation.get_submenu().get_children()
            if (r.get_label() or "") == "left"][0]
    left.set_active(True)                     # what clicking it does
    pump(lambda: False, timeout=0.3)
    res["stale_menu_edits_live"] = app.layout.get("HDMI-1").rotation
    res["stale_layout_untouched"] = stale.get("HDMI-1").rotation
    res["stale_menu_is_not_live"] = stale is not app.layout
    app.set_layout(backend.snapshot())

    # -- per-output menu: arandr's order; Scale only on Wayland ---------------
    res["menu_x11"] = menu_labels(app.output_menu("DP-1"))
    app.window.destroy()
    wapp = gui.Application(Stub(wayland=True))
    pump(lambda: wapp.layout is not None)
    res["menu_wayland"] = menu_labels(wapp.output_menu("DP-1"))
    res["wayland_word"] = wapp.status_text().split()[0]

    # -- Save As says so when the script's command word is not on PATH -------
    # (a layout script calls bare `wxrandr`; a stock desktop has none)
    os.environ["WARANDR_TEST_SAVE_AS"] = os.path.join(tmp, "layout")
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = tmp
    wapp.do_save_as()
    res["save_hint"] = wapp.status_text()
    exe = os.path.join(tmp, "wxrandr")
    with open(exe, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(exe, stat.S_IRWXU)
    wapp.do_save_as()
    res["save_nohint"] = wapp.status_text()
    res["saved_path"] = os.path.join(tmp, "layout.sh")
    os.environ["PATH"] = old_path
    # -- Save As asks about the `.sh` the chooser never saw ------------------
    with open(os.path.join(tmp, "desk.sh"), "w") as f:
        f.write("#!/bin/sh\n# an earlier layout\n")
    dialogs.clear()
    # the stub answers anything but YES, so a prompt means "do not save"
    res["sh_overwrite_asks"] = not wapp._confirm_sh_overwrite(
        os.path.join(tmp, "desk"))
    res["sh_overwrite_prompt"] = dialogs[-1] if dialogs else None
    res["sh_overwrite_quiet_when_new"] = wapp._confirm_sh_overwrite(
        os.path.join(tmp, "brandnew"))
    res["sh_overwrite_quiet_when_typed"] = wapp._confirm_sh_overwrite(
        os.path.join(tmp, "desk.sh"))
    with open(os.path.join(tmp, "desk.sh")) as f:
        res["sh_overwrite_kept"] = f.read()

    wapp.window.destroy()
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
