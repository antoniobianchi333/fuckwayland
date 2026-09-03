"""The arandr-shaped GTK 3 editor.  Widgets only (no cairo — stock Ubuntu
ships python3-gi + gir1.2-gtk-3.0 but not python3-gi-cairo): the canvas is a
Gtk.Fixed inside a scrolled window, every output is a CSS-coloured
Gtk.EventBox with a label, dragged with plain button/motion events and
snapped on drop.

Test hooks (env): ``WARANDR_TEST_LAYOUT_DUMP=FILE`` appends one JSON line
per redraw / menu popup with root-window coordinates of the boxes, toolbar
buttons and menu items, so xdotool can drive the editor deterministically;
``WARANDR_TEST_SAVE_AS=FILE`` makes Save As write there without the file
chooser.
"""

import json
import os
import shlex
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import VERSION, cli, randr  # noqa: E402
from .model import (REFLECTIONS, ROTATIONS, SCALES, LayoutError,  # noqa: E402
                    fmt_rate)

TITLE = "Screen Layout Editor"
ZOOMS = (2, 4, 8, 16, 32)
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


def _root_origin(widget):
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
                        | Gdk.EventMask.BUTTON_MOTION_MASK)
        self.connect("button-press-event", self._press)
        self.connect("motion-notify-event", self._motion)
        self.connect("button-release-event", self._release)
        self._drag = None

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
        tip = self.name
        if output.active and output.mode:
            tip += ": %s" % output.mode.label
            if output.rate:
                tip += " @ %s Hz" % fmt_rate(output.rate)
            tip += ", %s" % output.rotation
            tip += ", at %d,%d" % (output.x, output.y)
        else:
            tip += ": inactive" if output.connected else ": disconnected"
        self.set_tooltip_text(tip)

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
        if pos is not None and pos != (out.x, out.y):
            try:
                self.app.layout.move(self.name, *pos)
            except LayoutError as e:
                self.app.show_status("not moved: %s" % e)
        self.app.redraw()
        return True


class Application:
    def __init__(self, backend, filename=None):
        self.backend = backend
        self.factor = 8
        self.selected = None
        self.layout = None
        self._menus = []
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

        self.status = Gtk.Statusbar()
        self.status_ctx = self.status.get_context_id("command")
        vbox.pack_start(self.status, False, False, 0)

        self.boxes = {}
        self.window.show_all()
        self.window.connect("configure-event", lambda *_: self._dump_layout())

        if filename:
            self.load_file(filename)
        else:
            self.do_new()

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
        layout.append(self._item("_New", self.do_new, "<Control>n"))
        layout.append(self._item("_Open...", self.do_open, "<Control>o"))
        layout.append(self._item("Save _As...", self.do_save_as,
                                 "<Control>s"))
        layout.append(Gtk.SeparatorMenuItem())
        layout.append(self._item("_Apply", self.do_apply, "<Control>Return"))
        layout.append(self._item("Script _Properties", self.do_properties,
                                 "<Alt>Return"))
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
        for f in (4, 8, 16):
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
        return bar

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

    # -- state ----------------------------------------------------------------

    def show_status(self, text):
        self.status.pop(self.status_ctx)
        self.status.push(self.status_ctx, text)
        self.status.set_tooltip_text(text)

    def command_text(self):
        return self.layout.command_line(self.backend.word)

    def set_layout(self, layout):
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
        self.canvas.move(box, MARGIN + max(0, x) // self.factor,
                         MARGIN + max(0, y) // self.factor)

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
        GLib.idle_add(self._dump_layout, priority=GLib.PRIORITY_LOW)

    def _dump_layout(self):
        if not DUMP:
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
        win = self.window.get_window()
        xid = None
        if win is not None and hasattr(win, "get_xid"):
            try:
                xid = win.get_xid()
            except Exception:
                xid = None
        _dump("layout", {"boxes": boxes, "buttons": buttons, "xid": xid,
                         "factor": self.factor,
                         "command": self.layout.args() if self.layout
                         else None})
        return False

    def _dump_menu(self, menu, name):
        if not DUMP:
            return False
        items = {}
        for it in menu.get_children():
            if not isinstance(it, Gtk.MenuItem) or \
                    isinstance(it, Gtk.SeparatorMenuItem):
                continue
            r = _root_origin(it)
            if r:
                items[(it.get_label() or "").replace("_", "")] = r
        _dump("menu", {"name": name, "items": items})
        return False

    def _track_menu(self, menu, name):
        menu.connect("map", lambda m: GLib.timeout_add(
            150, self._dump_menu, m, name))

    # -- zoom -----------------------------------------------------------------

    def set_factor(self, f):
        f = max(ZOOMS[0], min(ZOOMS[-1], f))
        if f == self.factor:
            return
        self.factor = f
        if f in self._zoom_items and not self._zoom_items[f].get_active():
            self._zoom_items[f].set_active(True)
        self.redraw()

    def _zoom_radio(self, item, f):
        if item.get_active():
            self.set_factor(f)

    def zoom_in(self):
        self.set_factor(self.factor // 2)

    def zoom_out(self):
        self.set_factor(self.factor * 2)

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
            menu.show_all()
            self._track_menu(menu, "outputs")
            self._menus.append(menu)
            menu.popup_at_pointer(ev)
            return True
        return False

    def popup_output_menu(self, name, ev):
        menu = self.output_menu(name)
        menu.show_all()
        self._menus.append(menu)
        menu.popup_at_pointer(ev)

    def _edit(self, what, fn):
        try:
            fn()
        except LayoutError as e:
            _msg(self.window, Gtk.MessageType.ERROR,
                 "%s is not possible here: %s" % (what, e))
        self.redraw()

    def output_menu(self, name):
        """arandr's per-output menu (Active, Primary, Resolution,
        Orientation) plus Refresh rate, Reflection, Scale and Mirror of."""
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
        rates = o.mode.rates if o.mode else []
        radio_submenu("Refresh ra_te",
                      [("%s Hz" % fmt_rate(r), r) for r in rates],
                      o.mode.nearest_rate(o.rate) if o.mode else None,
                      lambda v: lay.set_rate(name, v), bool(rates))
        radio_submenu("_Orientation",
                      [(r, r) for r in ROTATIONS if r in o.rotations
                       or r == o.rotation],
                      o.rotation, lambda v: lay.set_rotation(name, v))
        radio_submenu("Re_flection",
                      [({"normal": "none", "x": "X axis", "y": "Y axis",
                         "xy": "X and Y axis"}[r], r) for r in REFLECTIONS],
                      o.reflection, lambda v: lay.set_reflection(name, v))
        scales = list(SCALES)
        if o.scale not in scales:
            scales.append(o.scale)
            scales.sort()
        radio_submenu("_Scale", [("%g" % s, s) for s in scales], o.scale,
                      lambda v: lay.set_scale(name, v),
                      sensitive=self.backend.wayland)
        others = [p for p in lay.active_outputs()
                  if p is not o and not p.mirror_of]
        radio_submenu("_Mirror of",
                      [("none", None)] + [(p.name, p.name) for p in others],
                      o.mirror_of, lambda v: lay.set_mirror(name, v),
                      sensitive=bool(others) or bool(o.mirror_of))
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
            path = cli.write_script(self.layout, fn, self.backend.word)
        except OSError as e:
            _msg(self.window, Gtk.MessageType.ERROR, "Cannot save:\n%s" % e)
            return
        self.show_status("saved %s" % path)
        _dump("saved", {"path": path})

    def do_apply(self):
        if not self.layout.active_outputs():
            r = _msg(self.window, Gtk.MessageType.WARNING,
                     "Your configuration does not include an active monitor. "
                     "Do you want to apply the configuration?",
                     Gtk.ButtonsType.YES_NO)
            if r != Gtk.ResponseType.YES:
                return
        cmd = self.command_text()
        self.show_status("running: " + cmd)
        try:
            rc, out, err = self.backend.apply(self.layout)
        except randr.RandrError as e:
            rc, out, err = 1, "", str(e)
        _dump("applied", {"rc": rc, "stderr": err})
        if rc != 0:
            _msg(self.window, Gtk.MessageType.ERROR,
                 "XRandR failed:\n%s" % (err.strip() or out.strip()
                                         or "exit status %d" % rc))
        template = self.layout.template
        self.reload()
        self.layout.template = template

    def do_properties(self):
        d = Gtk.Dialog(title="Script Properties", transient_for=self.window,
                       modal=True)
        d.add_button("_Close", Gtk.ResponseType.CLOSE)
        d.set_default_size(560, 300)
        tv = Gtk.TextView()
        tv.set_editable(False)
        tv.set_monospace(True)
        tv.get_buffer().set_text(self.layout.to_script(self.backend.word))
        sw = Gtk.ScrolledWindow()
        sw.add(tv)
        nb = Gtk.Notebook()
        nb.append_page(sw, Gtk.Label(label="Script"))
        info = Gtk.Label(label="backend: %s\nsource: %s" % (
            self.backend.describe(), self.backend.source))
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
                       "scripts are interchangeable.")
        d.set_website("https://github.com/zardus/fuckwayland")
        d.set_logo_icon_name("video-display")
        d.run()
        d.destroy()

    def run(self):
        Gtk.main()
        return 0


def run(backend, filename=None):
    try:
        app = Application(backend, filename)
    except randr.RandrError as e:
        sys.stderr.write("warandr: %s\n" % e)
        return 1
    return app.run()
