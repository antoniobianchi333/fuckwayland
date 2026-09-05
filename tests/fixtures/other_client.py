#!/usr/bin/env python3
"""A second GTK client on the same display, used by the GUI tests to tell an
app-local grab from a session-wide one: it prints CLICKED whenever its button
is pressed, so a click that never arrives means somebody else owns the X
pointer grab."""
import sys

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

w = Gtk.Window(title="OtherClient")
w.set_default_size(200, 80)
b = Gtk.Button(label="press me")
b.connect("clicked", lambda *_: (sys.stdout.write("CLICKED\n"),
                                 sys.stdout.flush()))
w.add(b)
w.connect("destroy", Gtk.main_quit)
w.move(900, 700)
w.show_all()
sys.stdout.write("READY\n")
sys.stdout.flush()
Gtk.main()
