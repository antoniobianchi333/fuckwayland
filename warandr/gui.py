"""The arandr-shaped GTK 3 editor.  Widgets only (no cairo — stock Ubuntu
ships python3-gi + gir1.2-gtk-3.0 but not python3-gi-cairo): the canvas is a
Gtk.Fixed inside a scrolled window, every output is a CSS-coloured
Gtk.EventBox with a label, dragged with plain button/motion events and
snapped on drop.

Apply runs the backend on a worker thread (a slow or hung compositor must
not freeze the window); the result comes back through GLib.idle_add.  A
failed Apply keeps the edited layout (arandr raises before re-reading).

Test hooks (env): ``WARANDR_TEST_LAYOUT_DUMP=FILE`` appends one JSON line
per redraw / menu popup / status-bar change with the coordinates of the
boxes, toolbar buttons and menu items, so xdotool/wdotool can drive the
editor deterministically — root-window pixels on X11 (``"coords": "root"``),
toplevel-relative pixels on Wayland (``"coords": "window"``; the compositor
tells nobody where a toplevel is).  A layout dump waits until GTK has
allocated the boxes at the size and place the redraw asked for (the frame
clock lags an idle callback while the compositor reconfigures outputs);
popup-menu items are, on Wayland, *modelled* from what GTK asked the
compositor for (at the pointer, right of the parent item, below the menubar
item) because GDK cannot read a popup's position back there.
``WARANDR_TEST_SAVE_AS=FILE`` makes Save As write there without the file
chooser.
"""

import json
import os
import shutil
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

from . import VERSION, cli, randr  # noqa: E402
from .model import (REFLECTIONS, ROTATIONS, SCALES, LayoutError,  # noqa: E402
                    fmt_rate)

TITLE = "Screen Layout Editor"
# Layout ▸ Backend, in wxrandr's own order (the label, then the token that
# --backend takes)
BACKEND_ITEMS = (("Automatic", "auto"), ("X11 (xrandr)", "x11"),
                 ("sway", "sway"), ("wlroots (wlr)", "wlr"),
                 ("GNOME (mutter)", "mutter"), ("KDE (kwin)", "kwin"))
ZOOMS = (4, 8, 16)     # arandr's View menu; Zoom In/Out walk this list
MARGIN = 12
SNAP_PX = 5            # arandr: tolerance = factor * 5 layout pixels
PALETTE = ("#9ad0f5", "#f5b99a", "#b8e2a0", "#f0d98a", "#d5b3f0", "#f5a3c8",
           "#a3e6dc", "#d0d0d0")

CSS = """
.warandr-canvas { background-color: #404040; }
.warandr-output { border: 1px solid #000000; }
.warandr-output label { color: #000000; }
.warandr-output.selected { border: 3px solid #ffffff; }
.warandr-output.inactive { opacity: 0.35; }
.warandr-output.mirror { border-style: dashed; }
"""
for _i, _c in enumerate(PALETTE):
    CSS += ".warandr-output.c%d { background-color: %s; }\n" % (_i, _c)

DUMP = os.environ.get("WARANDR_TEST_LAYOUT_DUMP")


def _dump(kind, payload):
    if not DUMP:
        return
    payload = dict(payload)
    payload["kind"] = kind
    try:
        with open(DUMP, "a") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError:
        pass


def _is_wayland():
    d = Gdk.Display.get_default()
    return bool(d) and "Wayland" in d.__gtype__.name


def _root_origin(widget):
    """[x, y, w, h] of a widget: root-window pixels on X11; on Wayland the
    toplevel's origin reads as 0,0, so toplevel-relative pixels (the
    toplevel surface includes the CSD shadow when it is not maximized)."""
    top = widget.get_toplevel()
    win = top.get_window() if top else None
    if win is None:
        return None
    res = win.get_origin()
    ox, oy = res[-2], res[-1]
    rel = widget.translate_coordinates(top, 0, 0)
    if rel is None:
        return None
    a = widget.get_allocation()
    return [ox + rel[0], oy + rel[1], a.width, a.height]


def _style_int(widget, name):
    v = GObject.Value(GObject.TYPE_INT)
    widget.style_get_property(name, v)
    return v.get_int()


def _msg(parent, kind, text, buttons=Gtk.ButtonsType.CLOSE):
    d = Gtk.MessageDialog(transient_for=parent, modal=True,
                          message_type=kind, buttons=buttons, text=text)
    r = d.run()
    d.destroy()
    return r


class OutputBox(Gtk.EventBox):
    def __init__(self, app, output, index):
        super().__init__()
        self.app = app
        self.name = output.name
        self.label = Gtk.Label()
        self.label.set_justify(Gtk.Justification.CENTER)
        self.label.set_line_wrap(False)
        self.add(self.label)
        self._pw, self._ph = 60, 40
        ctx = self.get_style_context()
        ctx.add_class("warandr-output")
        ctx.add_class("c%d" % (index % len(PALETTE)))
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.BUTTON_MOTION_MASK
                        | Gdk.EventMask.ENTER_NOTIFY_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("button-press-event", self._press)
        self.connect("motion-notify-event", self._motion)
        self.connect("button-release-event", self._release)
        # no tooltip (it would pop over the context menu): the description
        # goes to the status bar while the pointer is inside the box
        self.connect("enter-notify-event",
                     lambda *_: self.app.show_hover(self.info) or False)
        self.connect("leave-notify-event",
                     lambda *_: self.app.show_hover(None) or False)
        self._drag = None
        self.info = self.name

    # Gtk.Fixed hands children their *natural* size, which for a label is
    # the full text: report the box size as both minimum and natural so the
    # box is exactly the scaled output and the text is clipped/ellipsized
    # inside it instead of stretching the box.
    def do_get_preferred_width(self):
        return (self._pw, self._pw)

    def do_get_preferred_height(self):
        return (self._ph, self._ph)

    def do_get_preferred_width_for_height(self, _h):
        return (self._pw, self._pw)

    def do_get_preferred_height_for_width(self, _w):
        return (self._ph, self._ph)

    def refresh(self, output, selected):
        w, h = output.size()
        f = self.app.factor
        pw, ph = max(w // f, 24), max(h // f, 18)
        if not output.active:
            pw, ph = max(pw, 60), max(ph, 40)
        self._pw, self._ph = pw, ph
        self.set_size_request(pw, ph)
        self.queue_resize()
        rotated = output.active and output.rotation in ("left", "right")
        aw, ah = (ph, pw) if rotated else (pw, ph)
        n = max(len(self.name), 1)
        # arandr sizes the name to the box width; keep it inside the height
        big_px = min(aw * 0.85 / (n * 0.62), ah * 0.42, 56)
        big = max(6, int(big_px * 0.75))
        small = max(6, min(big // 2, 11))
        name = GLib.markup_escape_text(self.name)
        if output.primary:
            name = "<u>%s</u>" % name
        if output.active and output.mode is not None:
            sub = "%dx%d" % (output.mode.w, output.mode.h)
            if output.mirror_of:
                sub += " = " + output.mirror_of
            elif output.scale != 1.0:
                sub += " @%g" % output.scale
        else:
            sub = "(off)"
        self.label.set_markup(
            '<span size="%d"><b>%s</b></span>\n<span size="%d">%s</span>'
            % (big * Pango.SCALE, name, small * Pango.SCALE,
               GLib.markup_escape_text(sub)))
        # ellipsizing is ignored for rotated labels (GTK); they fit by font
        self.label.set_ellipsize(Pango.EllipsizeMode.NONE if rotated
                                 else Pango.EllipsizeMode.END)
        self.label.set_angle({"left": 90, "right": 270,
                              "inverted": 180}.get(output.rotation, 0)
                             if output.active else 0)
        ctx = self.get_style_context()
        for cls, on in (("inactive", not output.active),
                        ("selected", selected),
                        ("mirror", bool(output.mirror_of))):
            if on:
                ctx.add_class(cls)
            else:
                ctx.remove_class(cls)
        info = self.name
        if output.active and output.mode:
            info += ": %s" % output.mode.label
            if output.rate:
                info += " @ %s Hz" % fmt_rate(output.rate)
            info += ", %s" % output.rotation
            if output.mirror_of:
                info += ", mirror of %s" % output.mirror_of
            else:
                info += ", at %d,%d" % (output.x, output.y)
        else:
            info += ": inactive" if output.connected else ": disconnected"
        self.info = info

    # -- mouse ----------------------------------------------------------------

    def _press(self, _w, ev):
        out = self.app.layout.get(self.name)
        if ev.button == 3:
            self.app.select(self.name)
            self.app.popup_output_menu(self.name, ev)
            return True
        if ev.button == 1:
            self.app.select(self.name)
            if out.active and not out.mirror_of:
                self._drag = (ev.x_root, ev.y_root, out.x, out.y)
            return True
        return False

    def _motion(self, _w, ev):
        if self._drag is None or not (ev.state & Gdk.ModifierType.BUTTON1_MASK):
            return False
        sx, sy, ox, oy = self._drag
        f = self.app.factor
        x = ox + (ev.x_root - sx) * f
        y = oy + (ev.y_root - sy) * f
        x, y = self.app.layout.snap(self.name, x, y, f * SNAP_PX)
        out = self.app.layout.get(self.name)
        out.tentative = (x, y)
        self.app.place_box(self, x, y)
        self.app.show_status("%s -> %d,%d" % (self.name, x, y))
        return True

    def _release(self, _w, ev):
        if self._drag is None or ev.button != 1:
            return False
        self._drag = None
        out = self.app.layout.get(self.name)
        pos = out.tentative
        out.tentative = None
        rejected = note = None
        if pos is not None and pos != (out.x, out.y):
            try:
                self.app.layout.move(self.name, *pos)
            except LayoutError as e:
                rejected = e
            else:
                note = self.app.overlap_message(self.name)
        self.app.redraw()          # redraw resets the status bar first ...
        if rejected is not None:   # ... then the message goes on top
            self.app.show_message("%s not moved: %s" % (self.name, rejected))
        elif note is not None:
            self.app.show_message(note)
        return True


class Application:
    def __init__(self, backend, filename=None, display=None):
        self.backend = backend
        self.display = display   # --randr-display, re-applied on a switch
        self.backends = {}       # wxrandr --backends, once it has answered
        self._syncing = False    # the Backend radios are being set, not used
        self.factor = 8
        self.selected = None
        self.layout = None
        self._popup = None       # the one popup menu that can be open
        self._busy = False       # an Apply is running on the worker thread
        self._dump_serial = 0    # a newer redraw supersedes a pending dump
        self.window = Gtk.Window(title=TITLE)
        self.window.set_default_size(720, 520)
        self.window.set_icon_name("video-display")
        self.window.connect("destroy", Gtk.main_quit)

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.accel = Gtk.AccelGroup()
        self.window.add_accel_group(self.accel)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.add(vbox)
        vbox.pack_start(self._build_menubar(), False, False, 0)
        vbox.pack_start(self._build_toolbar(), False, False, 0)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC,
                                 Gtk.PolicyType.AUTOMATIC)
        self.canvas_bg = Gtk.EventBox()
        self.canvas_bg.get_style_context().add_class("warandr-canvas")
        self.canvas_bg.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.canvas_bg.connect("button-press-event", self._canvas_press)
        self.canvas = Gtk.Fixed()
        self.canvas.set_size_request(400, 240)
        self.canvas_bg.add(self.canvas)
        self.scroller.add(self.canvas_bg)
        vbox.pack_start(self.scroller, True, True, 0)

        # the always-visible backend indicator, right of the one status line
        # (a Gtk.Statusbar is a Gtk.Box): `backend: mutter (Wayland)`, with
        # the whole of `--print-backend --verbose` in its tooltip.  It is
        # also the shortest way to change the backend: an indicator that
        # shows a setting should open it, so the label sits in an event box
        # that pops up the very same Layout ▸ Backend menu.
        self.backend_label = Gtk.Label()
        self.backend_button = Gtk.EventBox()
        self.backend_button.add(self.backend_label)
        self.backend_button.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                                       | Gdk.EventMask.ENTER_NOTIFY_MASK
                                       | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.backend_button.connect("button-press-event", self._indicator_press)
        self.backend_button.connect("enter-notify-event",
                                    self._indicator_cursor, True)
        self.backend_button.connect("leave-notify-event",
                                    self._indicator_cursor, False)
        self.status = Gtk.Statusbar()
        # one line, three sources by priority: a transient message (rejected
        # drop, saved file; gone after a few seconds or at the next redraw),
        # else the hovered output's description, else the command Apply
        # would run
        self.status_ctx = self.status.get_context_id("status")
        self._command = self._message = self._hover = None
        self._message_serial = 0
        # RUN_LAST signal: connect_after, so the label is already updated
        self.status.connect_after("text-pushed", self._status_changed)
        self.status.pack_end(self.backend_button, False, False, 6)
        vbox.pack_start(self.status, False, False, 0)

        self.boxes = {}
        self._refresh_backend()
        self.window.show_all()
        self.window.connect("configure-event",
                            lambda *_: self._schedule_dump())

        if filename:
            self.load_file(filename)
        else:
            self.do_new()
        self._identify_async()

    # -- chrome ---------------------------------------------------------------

    def _item(self, label, cb, accel=None, stock=None):
        it = Gtk.MenuItem.new_with_mnemonic(label)
        it.connect("activate", lambda *_: cb())
        if accel:
            key, mods = Gtk.accelerator_parse(accel)
            it.add_accelerator("activate", self.accel, key, mods,
                               Gtk.AccelFlags.VISIBLE)
        return it

    def _build_menubar(self):
        bar = Gtk.MenuBar()
        layout = Gtk.Menu()
        self._track_menu(layout, "layout")
        layout.append(self._item("_New", self.do_new, "<Control>n"))
        layout.append(self._item("_Open...", self.do_open, "<Control>o"))
        layout.append(self._item("Save _As...", self.do_save_as,
                                 "<Control>s"))
        layout.append(Gtk.SeparatorMenuItem())
        self.apply_item = self._item("_Apply", self.do_apply,
                                     "<Control>Return")
        layout.append(self.apply_item)
        layout.append(self._item("Script _Properties", self.do_properties,
                                 "<Alt>Return"))
        layout.append(self._build_backend_menu())
        layout.append(Gtk.SeparatorMenuItem())
        layout.append(self._item("_Quit", Gtk.main_quit, "<Control>q"))
        m = Gtk.MenuItem.new_with_mnemonic("_Layout")
        m.set_submenu(layout)
        bar.append(m)

        view = Gtk.Menu()
        view.append(self._item("Zoom _In", self.zoom_in, "<Control>plus"))
        view.append(self._item("Zoom _Out", self.zoom_out, "<Control>minus"))
        view.append(self._item("_Fit", self.zoom_fit, "<Control>0"))
        view.append(Gtk.SeparatorMenuItem())
        group = None
        self._zoom_items = {}
        for f in ZOOMS:
            r = Gtk.RadioMenuItem.new_with_label_from_widget(group, "1:%d" % f)
            group = group or r
            r.set_active(f == self.factor)
            r.connect("toggled", self._zoom_radio, f)
            self._zoom_items[f] = r
            view.append(r)
        m = Gtk.MenuItem.new_with_mnemonic("_View")
        m.set_submenu(view)
        bar.append(m)

        self.outputs_menu_item = Gtk.MenuItem.new_with_mnemonic("_Outputs")
        self.outputs_menu_item.set_submenu(Gtk.Menu())
        bar.append(self.outputs_menu_item)

        helpm = Gtk.Menu()
        helpm.append(self._item("_About", self.about))
        m = Gtk.MenuItem.new_with_mnemonic("_Help")
        m.set_submenu(helpm)
        bar.append(m)
        self.menubar = bar
        return bar

    def _indicator_press(self, _widget, event):
        """A click anywhere on the indicator opens Layout ▸ Backend, at the
        pointer.  The menu object is the menubar's own, so the radio state,
        the insensitive entries and their reasons are shared — there is one
        backend menu, reachable from two places."""
        menu = getattr(self, "backend_menu_item", None)
        submenu = menu.get_submenu() if menu is not None else None
        if submenu is None:            # menubar not built yet: nothing to open
            return False
        submenu.show_all()
        submenu.popup_at_pointer(event)
        return True

    def _indicator_cursor(self, widget, _event, entering):
        """Point the cursor at the indicator so it reads as clickable."""
        window = widget.get_window()
        if window is None:
            return False
        cursor = None
        if entering:
            cursor = Gdk.Cursor.new_from_name(widget.get_display(), "pointer")
        window.set_cursor(cursor)
        return False

    def _build_backend_menu(self):
        """Layout ▸ Backend: which tool talks to the screen.  It sits with
        Apply and Script Properties because it governs both — arandr has no
        such menu, and View is about how the canvas is drawn, not about who
        answers.  Radios: Automatic, X11 and each of wxrandr's Wayland
        backends; the ones this session cannot reach are insensitive and
        carry the reason in their tooltip (GTK 3 does not pop a tooltip over
        an insensitive item, so the same table is in Script Properties)."""
        menu = Gtk.Menu()
        self._track_menu(menu, "backend")
        group = None
        self._backend_items = {}
        for label, name in BACKEND_ITEMS:
            r = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = group or r
            self._backend_items[name] = r
            r.connect("toggled", self._backend_radio, name)
            menu.append(r)
        item = Gtk.MenuItem.new_with_mnemonic("_Backend")
        item.set_submenu(menu)
        self.backend_menu_item = item
        return item

    def _build_toolbar(self):
        tb = Gtk.Toolbar()
        tb.set_style(Gtk.ToolbarStyle.BOTH_HORIZ)
        self.toolbuttons = {}

        def add(key, icon, label, cb, important=False):
            b = Gtk.ToolButton()
            b.set_icon_name(icon)
            b.set_label(label)
            b.set_tooltip_text(label)
            b.set_is_important(important)
            b.connect("clicked", lambda *_: cb())
            tb.insert(b, -1)
            self.toolbuttons[key] = b
        add("apply", "emblem-ok", "Apply", self.do_apply, True)
        tb.insert(Gtk.SeparatorToolItem(), -1)
        add("new", "document-new", "New", self.do_new)
        add("open", "document-open", "Open", self.do_open)
        add("save_as", "document-save-as", "Save As", self.do_save_as)
        tb.insert(Gtk.SeparatorToolItem(), -1)
        add("zoom_in", "zoom-in", "Zoom in", self.zoom_in)
        add("zoom_out", "zoom-out", "Zoom out", self.zoom_out)
        add("zoom_fit", "zoom-fit-best", "Fit", self.zoom_fit)
        return tb

    # -- backend --------------------------------------------------------------

    def _sync_backend_menu(self):
        """The radios follow the live backend; availability follows the last
        `wxrandr --backends` (nothing is greyed before it has answered, and
        the current choice is never greyed out from under the user)."""
        want = self.backend.forced or "auto"
        self._syncing = True
        try:
            for name, item in self._backend_items.items():
                info = self.backends.get(name)
                if info is not None:
                    ok = info["available"] or name == want
                    item.set_sensitive(ok)
                    item.set_tooltip_text(
                        "" if info["available"] else
                        "not available in this session: %s" % info["reason"])
            self._backend_items[want].set_active(True)
        finally:
            self._syncing = False

    def _backend_radio(self, item, name):
        if self._syncing or not item.get_active():
            return
        self.set_backend(name)

    def _refresh_backend(self):
        self.backend_label.set_text(self.backend.indicator())
        tip = self.backend.detail() + "\n\nClick to change the backend."
        self.backend_label.set_tooltip_text(tip)
        self.backend_button.set_tooltip_text(tip)
        self._sync_backend_menu()
        _dump("backend", {"name": self.backend.name,
                          "forced": self.backend.forced,
                          "indicator": self.backend.indicator(),
                          "overlap": self.backend.overlap_note(),
                          "word": self.backend.run_word,
                          "available": {k: v["available"]
                                        for k, v in self.backends.items()},
                          "ok": True})

    def _identify_async(self):
        """`--print-backend --verbose` (which backend is this, really?) and
        `--backends` (what else could it be?) run off the main loop: the
        window is already up, and a wedged compositor must not hold it."""
        backend = self.backend

        def work():
            backend.identify()
            info = randr.probe_backends(backend.env)
            GLib.idle_add(self._identified, backend, info)
        threading.Thread(target=work, name="warandr-backend",
                         daemon=True).start()

    def _identified(self, backend, info):
        if info:
            self.backends = info
        if self.backend is backend:
            self._refresh_backend()
            if self.layout is not None:
                # identify() may have turned "wayland" into a real name
                self.layout.overlap_refusal = backend.overlap_refusal()
        return False

    def set_backend(self, name):
        """Switch the tool that talks to the screen: re-read the layout
        through it, redraw, and from then on Apply, the command in the
        status bar and a saved script are that backend's.  One that cannot
        be reached keeps the previous choice — the window is never left
        empty — and says why in an Apply-shaped dialog."""
        previous = self.backend
        try:
            backend = randr.choose(forced=name)
            backend.set_display(self.display)
            layout = backend.snapshot()
        except (randr.RandrError, LayoutError, OSError) as e:
            _dump("backend", {"name": previous.name, "wanted": name,
                              "forced": previous.forced, "ok": False,
                              "error": str(e)})
            _msg(self.window, Gtk.MessageType.ERROR,
                 "Cannot use the %s backend:\n%s" % (name, e))
            self._sync_backend_menu()     # the radio goes back
            return False
        self.backend = backend
        self._refresh_backend()
        self.set_layout(layout)
        self._identify_async()
        return True

    # -- state ----------------------------------------------------------------

    def show_status(self, text):
        """The permanent line (the command); clears a transient message."""
        self._command = text
        self._message = None
        self._refresh_status()

    def show_message(self, text, seconds=6):
        """A transient line, on top of everything until the next redraw or
        for `seconds`."""
        self._message = text
        self._message_serial += 1
        GLib.timeout_add_seconds(seconds, self._expire_message,
                                 self._message_serial)
        self._refresh_status()

    def _expire_message(self, serial):
        if serial == self._message_serial and self._message is not None:
            self._message = None
            self._refresh_status()
        return False

    def show_hover(self, text):
        self._hover = text or None
        self._refresh_status()

    def _refresh_status(self):
        text = self._message or self._hover or self._command or ""
        if text != self.status_text():
            self.status.pop(self.status_ctx)
            self.status.push(self.status_ctx, text)
        self.status.set_tooltip_text(self._command or "")

    def status_text(self):
        """What the status bar shows right now."""
        area = self.status.get_message_area()
        for w in area.get_children():
            if isinstance(w, Gtk.Label):
                return w.get_text()
        return ""

    def _status_changed(self, *_):
        _dump("status", {"text": self.status_text()})

    def command_text(self):
        return self.layout.command_line(self.backend.run_word)

    def overlap_message(self, name):
        """What a drop that landed `name` on top of another output means on
        the backend in use — the status bar's one sentence at the moment of
        the drop.  None when this output overlaps nothing (an exact overlap
        is a clone, not this)."""
        others = [b if a == name else a
                  for a, b in self.layout.overlaps() if name in (a, b)]
        if not others:
            return None
        return "%s overlaps %s. %s" % (name, ", ".join(others),
                                       self.backend.overlap_note())

    def set_layout(self, layout):
        # whether an overlapping layout is refused, and in whose name, is
        # the live backend's answer -- never our own geometry policy
        layout.overlap_refusal = self.backend.overlap_refusal()
        self.layout = layout
        if self.selected and not layout.has(self.selected):
            self.selected = None
        self.redraw()

    def reload(self):
        try:
            self.set_layout(self.backend.snapshot())
        except randr.RandrError as e:
            _msg(self.window, Gtk.MessageType.ERROR,
                 "Cannot read the screen configuration:\n%s" % e)
            if self.layout is None:
                raise

    def select(self, name):
        self.selected = name
        for n, b in self.boxes.items():
            b.refresh(self.layout.get(n), n == self.selected)

    # -- drawing --------------------------------------------------------------

    def place_box(self, box, x, y):
        box._target = (MARGIN + max(0, x) // self.factor,
                       MARGIN + max(0, y) // self.factor)
        self.canvas.move(box, *box._target)

    def redraw(self):
        lay = self.layout
        wanted = [o for o in lay.outputs if o.connected or o.active]
        for n in list(self.boxes):
            if not any(o.name == n for o in wanted):
                self.boxes[n].destroy()
                del self.boxes[n]
        _, _, x1, _ = lay.bounding_box()
        park_x = x1 + 24 * self.factor
        park_y = 0
        for i, o in enumerate(lay.outputs):
            if o not in wanted:
                continue
            box = self.boxes.get(o.name)
            if box is None:
                box = OutputBox(self, o, i)
                self.boxes[o.name] = box
                self.canvas.put(box, 0, 0)
                box.show_all()
            box.refresh(o, o.name == self.selected)
            if o.active:
                self.place_box(box, o.x, o.y)
            else:
                self.place_box(box, park_x, park_y)
                park_y += (o.size()[1] if o.mode else 40 * self.factor) \
                    + 10 * self.factor
        self.show_status(self.command_text())
        self._populate_outputs_menu()
        self._schedule_dump()

    # -- test hook: geometry dumps -------------------------------------------

    def _schedule_dump(self):
        if DUMP:
            self._dump_serial += 1
            GLib.idle_add(self._dump_layout, self._dump_serial, 0,
                          priority=GLib.PRIORITY_LOW)

    def _layout_settled(self):
        """True once GTK has allocated every box at the size and place the
        last redraw asked for.  The frame clock's layout phase can lag an
        idle callback — on Wayland it waits for the compositor's frame
        callback, which stalls while Mutter reconfigures outputs right after
        an Apply — and a dump taken before it would report the old boxes."""
        for b in self.boxes.values():
            a = b.get_allocation()
            rel = b.translate_coordinates(self.canvas, 0, 0)
            if (a.width, a.height) != (b._pw, b._ph) or rel is None or \
                    tuple(rel) != getattr(b, "_target", None):
                return False
        return True

    def _dump_layout(self, serial=None, attempt=0):
        if not DUMP or (serial is not None and serial != self._dump_serial):
            return False
        settled = self._layout_settled()
        if not settled and attempt < 60:            # up to 3 s
            GLib.timeout_add(50, self._dump_layout, serial, attempt + 1)
            return False
        boxes = {}
        for n, b in self.boxes.items():
            r = _root_origin(b)
            if r:
                boxes[n] = r
        buttons = {}
        for k, b in self.toolbuttons.items():
            r = _root_origin(b)
            if r:
                buttons[k] = r
        menubar = {}
        for it in self.menubar.get_children():
            r = _root_origin(it)
            if r:
                menubar[(it.get_label() or "").replace("_", "")] = r
        win = self.window.get_window()
        xid = None
        if win is not None and hasattr(win, "get_xid"):
            try:
                xid = win.get_xid()
            except Exception:
                xid = None
        _dump("layout", {"boxes": boxes, "buttons": buttons,
                         "menubar": menubar, "xid": xid,
                         "coords": "window" if _is_wayland() else "root",
                         "window": [win.get_width(), win.get_height()]
                         if win is not None else None,
                         "settled": settled, "backend": self.backend.name,
                         "backend_label": self.backend.label,
                         "backend_indicator": _root_origin(self.backend_button),
                         "factor": self.factor, "status": self.status_text(),
                         "busy": self._busy,
                         "command": self.layout.args() if self.layout
                         else None})
        return False

    def _menu_origin(self, menu):
        """Top-left of a popup menu's allocation, from what GTK asked the
        compositor for (GDK cannot read a popup's position back on
        Wayland): a menu popped at the pointer sits at pointer + (1, 1)
        (gtk_menu_popup_at_pointer anchors south-east of a 1x1 rect); a
        submenu hangs at its parent item's north-east corner, shifted by the
        menu's horizontal-offset/vertical-offset style and its top padding
        so the first item lines up with the parent item; a menubar item's
        menu drops from the item's south-west corner.  Unconstrained
        placement is assumed (the compositor may flip or slide a popup
        near a screen edge)."""
        anchor = getattr(menu, "_anchor", None)
        if anchor is not None:
            return anchor
        item = menu.get_attach_widget()
        if not isinstance(item, Gtk.MenuItem):
            return None
        pr = self._widget_rect(item)
        if pr is None:
            return None
        if isinstance(item.get_parent(), Gtk.MenuBar):
            return (pr[0], pr[1] + pr[3])
        ctx = menu.get_style_context()
        pad = ctx.get_padding(ctx.get_state())
        return (pr[0] + pr[2] + _style_int(menu, "horizontal-offset")
                + pad.left,
                pr[1] + _style_int(menu, "vertical-offset") - pad.top)

    def _widget_rect(self, w):
        """[x, y, w, h] in the layout dump's coordinates: read from GDK for
        a widget of the main window, modelled for a popup-menu item."""
        parent = w.get_parent()
        if isinstance(parent, Gtk.Menu):
            o = self._menu_origin(parent)
            rel = w.translate_coordinates(parent, 0, 0)
            if o is None or rel is None:
                return None
            a = w.get_allocation()
            return [o[0] + rel[0], o[1] + rel[1], a.width, a.height]
        return _root_origin(w)

    def _dump_menu(self, menu, name):
        """Items of a popup: `items` is what a driver clicks (GDK's root
        coordinates on X11, the model on Wayland), `modelled` always the
        model — the X11 GUI test checks the two agree."""
        if not DUMP:
            return False
        items, modelled, sensitive, tips, active = {}, {}, {}, {}, {}
        for it in menu.get_children():
            if not isinstance(it, Gtk.MenuItem) or \
                    isinstance(it, Gtk.SeparatorMenuItem):
                continue
            label = (it.get_label() or "").replace("_", "")
            m = self._widget_rect(it)
            if m:
                modelled[label] = m
            r = m if _is_wayland() else _root_origin(it)
            if r:
                items[label] = r
            sensitive[label] = it.get_sensitive()
            tips[label] = it.get_tooltip_text() or ""
            if isinstance(it, Gtk.CheckMenuItem):
                active[label] = it.get_active()
        _dump("menu", {"name": name, "items": items, "modelled": modelled,
                       "sensitive": sensitive, "tooltips": tips,
                       "active": active,
                       "coords": "window" if _is_wayland() else "root"})
        return False

    def _track_menu(self, menu, name):
        menu.connect("map", lambda m: GLib.timeout_add(
            150, self._dump_menu, m, name))

    # -- zoom -----------------------------------------------------------------

    def set_factor(self, f):
        if f not in ZOOMS:
            f = min(ZOOMS, key=lambda z: abs(z - f))
        if f == self.factor:
            return
        self.factor = f
        if not self._zoom_items[f].get_active():
            self._zoom_items[f].set_active(True)   # radio follows Ctrl+/-
        self.redraw()

    def _zoom_radio(self, item, f):
        if item.get_active():
            self.set_factor(f)

    def zoom_in(self):
        i = ZOOMS.index(self.factor)
        self.set_factor(ZOOMS[max(i - 1, 0)])

    def zoom_out(self):
        i = ZOOMS.index(self.factor)
        self.set_factor(ZOOMS[min(i + 1, len(ZOOMS) - 1)])

    def zoom_fit(self):
        x0, y0, x1, y1 = self.layout.bounding_box()
        a = self.scroller.get_allocation()
        aw, ah = max(a.width - 2 * MARGIN - 40, 100), \
            max(a.height - 2 * MARGIN - 40, 100)
        for f in ZOOMS:
            if (x1 - x0) // f <= aw and (y1 - y0) // f <= ah:
                self.set_factor(f)
                return
        self.set_factor(ZOOMS[-1])

    # -- menus ----------------------------------------------------------------

    def _populate_outputs_menu(self):
        menu = Gtk.Menu()
        self._track_menu(menu, "outputs")
        for o in self.layout.outputs:
            it = Gtk.MenuItem.new_with_label(o.name)
            it.set_submenu(self.output_menu(o.name))
            if not o.connected and not o.active:
                it.set_sensitive(False)
            menu.append(it)
        menu.show_all()
        self.outputs_menu_item.set_submenu(menu)

    def _canvas_press(self, _w, ev):
        if ev.button == 3:
            menu = Gtk.Menu()
            for o in self.layout.outputs:
                it = Gtk.MenuItem.new_with_label(o.name)
                it.set_submenu(self.output_menu(o.name))
                if not o.connected and not o.active:
                    it.set_sensitive(False)
                menu.append(it)
            self._track_menu(menu, "outputs")
            self._popup_menu(menu, ev)
            return True
        return False

    def popup_output_menu(self, name, ev):
        self._popup_menu(self.output_menu(name), ev)

    def _popup_menu(self, menu, ev):
        """Pop `menu` up and own it only while it is open: the previous
        popup is released here, and this one on its deactivate (from idle,
        after the chosen item's handler ran)."""
        menu.show_all()
        menu.connect("deactivate", lambda m: GLib.idle_add(
            self._release_popup, m))
        self._popup = menu
        if ev is not None:
            menu._anchor = (int(ev.x_root) + 1, int(ev.y_root) + 1)
            menu.popup_at_pointer(ev)
        else:                       # no event (programmatic): at the canvas
            r = _root_origin(self.canvas_bg)
            menu._anchor = (r[0], r[1]) if r else None
            menu.popup_at_widget(self.canvas_bg, Gdk.Gravity.NORTH_WEST,
                                 Gdk.Gravity.NORTH_WEST, None)

    def _release_popup(self, menu):
        if self._popup is menu:
            self._popup = None
        return False

    def _edit(self, what, fn):
        try:
            fn()
        except LayoutError as e:
            _msg(self.window, Gtk.MessageType.ERROR,
                 "%s is not possible here: %s" % (what, e))
        self.redraw()

    def output_menu(self, name):
        """arandr's per-output menu (Active, Primary, Resolution,
        Orientation) followed, after a separator, by Refresh rate,
        Reflection, Mirror of and — Wayland only — Scale."""
        lay = self.layout
        o = lay.get(name)
        menu = Gtk.Menu()
        self._track_menu(menu, "output:" + name)

        active = Gtk.CheckMenuItem.new_with_mnemonic("_Active")
        active.set_active(o.active)
        active.connect("toggled", lambda it: self._edit(
            "Activating %s" % name,
            lambda: lay.set_active(name, it.get_active())))
        menu.append(active)
        if not o.connected and not o.active:
            active.set_sensitive(False)
        if not o.active:
            return menu

        primary = Gtk.CheckMenuItem.new_with_mnemonic("_Primary")
        primary.set_active(o.primary)
        primary.connect("toggled", lambda it: self._edit(
            "Primary", lambda: lay.set_primary(name, it.get_active())))
        menu.append(primary)

        def radio_submenu(label, entries, current, setter, sensitive=True):
            sub = Gtk.Menu()
            self._track_menu(sub, label.replace("_", ""))
            group = None
            for text, value in entries:
                r = Gtk.RadioMenuItem.new_with_label_from_widget(group, text)
                group = group or r
                r.set_active(value == current)
                r.connect("toggled", lambda it, v=value: it.get_active()
                          and self._edit(label, lambda: setter(v)))
                sub.append(r)
            it = Gtk.MenuItem.new_with_mnemonic(label)
            it.set_submenu(sub)
            it.set_sensitive(sensitive)
            menu.append(it)
            return it

        radio_submenu("_Resolution", [(m.label, m.name) for m in o.modes],
                      o.mode.name if o.mode else None,
                      lambda v: lay.set_mode(name, v))
        radio_submenu("_Orientation",
                      [(r, r) for r in ROTATIONS if r in o.rotations
                       or r == o.rotation],
                      o.rotation, lambda v: lay.set_rotation(name, v))
        menu.append(Gtk.SeparatorMenuItem())
        rates = o.mode.rates if o.mode else []
        radio_submenu("Refresh ra_te",
                      [("%s Hz" % fmt_rate(r), r) for r in rates],
                      o.mode.nearest_rate(o.rate) if o.mode else None,
                      lambda v: lay.set_rate(name, v), bool(rates))
        radio_submenu("Re_flection",
                      [({"normal": "none", "x": "X axis", "y": "Y axis",
                         "xy": "X and Y axis"}[r], r) for r in REFLECTIONS],
                      o.reflection, lambda v: lay.set_reflection(name, v))
        others = [p for p in lay.active_outputs()
                  if p is not o and not p.mirror_of]
        radio_submenu("_Mirror of",
                      [("none", None)] + [(p.name, p.name) for p in others],
                      o.mirror_of, lambda v: lay.set_mirror(name, v),
                      sensitive=bool(others) or bool(o.mirror_of))
        if self.backend.wayland:       # X11 has no per-output HiDPI scale
            scales = list(SCALES)
            if o.scale not in scales:
                scales.append(o.scale)
                scales.sort()
            radio_submenu("_Scale", [("%g" % s, s) for s in scales], o.scale,
                          lambda v: lay.set_scale(name, v))
        return menu

    # -- actions --------------------------------------------------------------

    def do_new(self):
        self.reload()

    def load_file(self, filename):
        try:
            self.set_layout(cli.load_layout(self.backend, filename))
        except (LayoutError, OSError, randr.RandrError) as e:
            _msg(self.window, Gtk.MessageType.ERROR,
                 "Cannot load %s:\n%s" % (filename, e))
            if self.layout is None:
                self.reload()

    def _file_dialog(self, title, action, button):
        d = Gtk.FileChooserDialog(title=title, transient_for=self.window,
                                  action=action)
        d.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        d.add_button(button, Gtk.ResponseType.ACCEPT)
        folder = os.path.expanduser("~/.screenlayout/")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            pass
        d.set_current_folder(folder)
        flt = Gtk.FileFilter()
        flt.set_name("Shell script (Layout file)")
        flt.add_pattern("*.sh")
        d.add_filter(flt)
        return d

    def do_open(self):
        d = self._file_dialog("Open Layout", Gtk.FileChooserAction.OPEN,
                              "_Open")
        r = d.run()
        fn = d.get_filename()
        d.destroy()
        if r == Gtk.ResponseType.ACCEPT and fn:
            self.load_file(fn)

    def do_save_as(self):
        fn = os.environ.get("WARANDR_TEST_SAVE_AS")
        if not fn:
            d = self._file_dialog("Save Layout", Gtk.FileChooserAction.SAVE,
                                  "_Save")
            d.set_do_overwrite_confirmation(True)
            r = d.run()
            fn = d.get_filename()
            d.destroy()
            if r != Gtk.ResponseType.ACCEPT or not fn:
                return
        try:
            path = cli.write_script(self.layout, fn, self.backend.word,
                                    cli.script_notes(self.layout,
                                                     self.backend))
        except OSError as e:
            _msg(self.window, Gtk.MessageType.ERROR, "Cannot save:\n%s" % e)
            return
        text = "saved %s" % path
        # the script calls the bare command word (arandr's shape); xrandr is
        # always installed on X11, wxrandr on a stock desktop is not
        if not shutil.which(self.backend.word):
            text += " - note: %s is not on PATH, the script needs it" \
                % self.backend.word
        self.show_status(text)
        _dump("saved", {"path": path})

    def do_apply(self):
        if self._busy:
            return
        if not self.layout.active_outputs():
            r = _msg(self.window, Gtk.MessageType.WARNING,
                     "Your configuration does not include an active monitor. "
                     "Do you want to apply the configuration?",
                     Gtk.ButtonsType.YES_NO)
            if r != Gtk.ResponseType.YES:
                return
        self._set_busy(True)
        self.show_status("running: " + self.command_text())
        layout = self.layout

        def work():
            try:
                rc, out, err = self.backend.apply(layout)
            except randr.RandrError as e:
                rc, out, err = 1, "", str(e)
            fresh = exc = None
            if rc == 0:
                try:
                    fresh = self.backend.snapshot()
                except randr.RandrError as e:
                    exc = e
            GLib.idle_add(self._applied, layout, rc, out, err, fresh, exc)
        threading.Thread(target=work, name="warandr-apply",
                         daemon=True).start()

    def _set_busy(self, busy):
        self._busy = busy
        self.toolbuttons["apply"].set_sensitive(not busy)
        self.apply_item.set_sensitive(not busy)

    def _applied(self, layout, rc, out, err, fresh, exc):
        """Back on the main loop with the worker's result."""
        self._set_busy(False)
        _dump("applied", {"rc": rc, "stderr": err})
        if rc != 0 or fresh is None:
            self.redraw()                # the edited layout stays
            _msg(self.window, Gtk.MessageType.ERROR,
                 "XRandR failed:\n%s" % (err.strip() or out.strip()
                                         or "exit status %d" % rc)
                 if rc != 0 else
                 "Cannot read the screen configuration:\n%s" % exc)
            return False
        fresh.template = layout.template
        if self.layout is layout:        # nothing was loaded meanwhile
            self.set_layout(fresh)
        return False

    def do_properties(self):
        d = Gtk.Dialog(title="Script Properties", transient_for=self.window,
                       modal=True)
        d.add_button("_Close", Gtk.ResponseType.CLOSE)
        d.set_default_size(560, 300)
        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_monospace(True)
        tv.get_buffer().set_text(self.layout.to_script(
            self.backend.word, cli.script_notes(self.layout, self.backend)))
        sw = Gtk.ScrolledWindow()
        sw.add(tv)
        nb = Gtk.Notebook()
        nb.append_page(sw, Gtk.Label(label="Script"))
        text = self.backend.detail()
        if self.backends:
            text += "\n\nbackends in this session:"
            for name, _lbl in ((n, lb) for lb, n in BACKEND_ITEMS
                               if n != "auto"):
                what = self.backends.get(name)
                if what is None:
                    continue
                text += "\n  %-6s %s" % (
                    name, "available" if what["available"]
                    else "unavailable: " + what["reason"])
        info = Gtk.Label(label=text)
        info.set_selectable(True)
        nb.append_page(info, Gtk.Label(label="Backend"))
        d.get_content_area().pack_start(nb, True, True, 0)
        d.show_all()
        d.run()
        d.destroy()

    def about(self):
        d = Gtk.AboutDialog(transient_for=self.window, modal=True)
        d.set_program_name("WARandR")
        d.set_version(VERSION.split()[-1])
        d.set_comments("Another XRandR GUI - on Wayland through wxrandr, on "
                       "X11 through xrandr.\nA drop-in arandr clone; layout "
                       "scripts are interchangeable.\n\n"
                       + self.backend.detail())
        d.set_website("https://github.com/zardus/fuckwayland")
        d.set_logo_icon_name("video-display")
        d.run()
        d.destroy()

    def run(self):
        Gtk.main()
        return 0


def run(backend, filename=None, display=None):
    ok, _argv = Gtk.init_check([])
    if not ok:
        disp = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        sys.stderr.write("warandr: cannot open display%s\n"
                         % (" " + disp if disp else " (DISPLAY is not set)"))
        return 1
    try:
        app = Application(backend, filename, display)
    except randr.RandrError as e:
        sys.stderr.write("warandr: %s\n" % e)
        return 1
    return app.run()
