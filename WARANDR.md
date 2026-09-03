# warandr — design contract

Drop-in `arandr` clone that works on **Wayland** (through `wxrandr`) and on
**X11** (through `xrandr`). Same window, same menus, same `~/.screenlayout/*.sh`
files — arandr's layout scripts load here and ours load in arandr. House rules
per DESIGN.md, with one exception spelled out below.

## Toolkit rule (the exception)

Everything in this repo is pure-stdlib Python, except the warandr GUI, which
uses **GTK 3 through PyGObject** — stock Ubuntu desktops (24.04 and 26.04) ship
`python3-gi` and `gir1.2-gtk-3.0` as dependencies of update-manager /
software-properties-gtk / apport-gtk. They do **not** ship `python3-gi-cairo`,
so there is no cairo drawing: the canvas is built from widgets (a `Gtk.Fixed`
inside a scrolled window; one CSS-coloured `Gtk.EventBox` + label per output,
dragged with button/motion events). Everything that is not the window
(`xrandr_parse`, `model`, `randr`, `cli --save/--command`) stays stdlib-only
and is what the tests exercise without a display. A missing GTK is one line:
`warandr: GTK 3 for Python is not available (...) - on Ubuntu/Debian: sudo apt
install python3-gi gir1.2-gtk-3.0`.

## Backend selection (`warandr/randr.py`)

First match wins:

0. an explicit choice — `warandr --backend NAME`, or the GUI's
   **Layout ▸ Backend**. `NAME` is `auto` (the default: everything below),
   `x11`, or one of wxrandr's own backends `sway|wlr|mutter|kwin` (aliases
   `gnome`, `kde`), spelled exactly as wxrandr's own flag. `x11` runs the
   real `xrandr`; a Wayland backend runs wxrandr with `--backend NAME`,
   which inside wxrandr beats `$WXRANDR_BACKEND` and its detection — so the
   documented rule holds end to end: **flag > environment > detection**.
   There is no silent fallback: a Wayland backend with no wxrandr to run it
   is an error (`the mutter backend needs wxrandr, which is not here ...`),
   not plain xrandr.
1. `$WARANDR_XRANDR` — a command line to run instead (shlex-split). Its kind
   is Wayland when it mentions `wxrandr`, X11 otherwise; `$WARANDR_BACKEND`
   (`x11`/`wayland`) overrides the kind. Tests point this at the fake. A
   forced Wayland backend appends `--backend NAME` to it (it is still "the
   command to run instead"), a forced `x11` does not.
2. `$WAYLAND_DISPLAY` set and the `wxrandr` package importable (the repo
   checkout, or the pyz that bundles it): run the **same interpreter** with
   `-m wxrandr`, `PYTHONPATH` pointing at wherever the package was found (a
   zipapp path works — zipimport). No second copy of wxrandr is needed.
3. `$WAYLAND_DISPLAY` set and `wxrandr` on `PATH`.
4. `xrandr`.

warandr **never hands its own process over** to the real tool (no `execve`,
unlike the four clones): it chooses which one to *run*, as a child — which is
what makes the choice switchable while the window is open.

Which backend it turned out to be is asked, once, off the main loop:
`wxrandr --print-backend --verbose` gives the token (`mutter`, `sway`, ...)
and the fuller explanation, `wxrandr --backends` the availability table the
Backend menu greys itself with (both are layout-free and answer on X11 too).
The X11 runner is the real xrandr, which has no such options, so its answer
is composed locally; a wxrandr too old to know them leaves the coarse name
`wxrandr (Wayland)` and greys nothing.

The kind decides two things: the **command word** written into layout scripts
(`wxrandr` on Wayland, `xrandr` on X11; both are accepted when loading) and the
**scale semantics** (below). `--randr-display D` sets `WAYLAND_DISPLAY` /
`DISPLAY` for the backend only, like arandr.

## Parsing (`warandr/xrandr_parse.py`)

One parser for `--query` and `--verbose` (xrandr 1.5.x bytes; wxrandr renders
the same). Screen line (min/current/max); output header `NAME connected|
disconnected|unknown connection [primary] [WxH+X+Y [(0xID)] [rotation
[reflection phrase]]] [(allowed rotations/reflections)] [MMmm x MMmm]`; query
mode rows grouped by name with ` %6.2f` rate columns and `*`/`+` flags; verbose
modelines (`  NAME (0xID) CLOCKMHz flags *current +preferred`) with the rate
taken from the `v:` clock, the width/height from `h:`/`v:` (custom mode names
carry no size); the tab block yields Identifier, CRTC and the **Transform**
matrix. Xvfb's minimal server (`screen connected 1280x1024+0+0 0mm x 0mm`, a
`0.00*` rate, no rotations list, `normal (normal)` in verbose) parses too.
warandr runs `--verbose` once per snapshot (arandr does the same).

## Model (`warandr/model.py`)

- `Output.size()` is the drawn (logical) size: scale, then the rotation swap.
  **Wayland**: scale is the compositor's HiDPI factor, logical = `px / scale`
  truncated (sway's rule; wxrandr's `--scale` sets exactly that), recovered
  from `mode / geometry` since compositors report an identity transform.
  **X11**: `--scale` is xrandr's framebuffer factor, logical = `ceil(px *
  scale)`, read from the verbose transform matrix. The Scale submenu (1, 1.25,
  1.5, 1.75, 2, 3) exists on the Wayland path only (arandr has no scale and
  X11 has no per-output HiDPI factor); an existing X11 transform is drawn
  correctly and written back as `--scale`, so what is drawn is what xrandr is
  told.
- **Snapping** (arandr's `Snap`): while dragging, within `factor * 5` layout
  pixels of another active output's (or the virtual screen's) left/right/top/
  bottom edge, the dragged output's own opposite edge, or a centre line, the
  coordinate snaps there. Deviation: the *nearest* candidate wins (arandr
  takes an arbitrary set member).
- **Overlap rejection** (deviation: arandr allows overlaps): two active
  outputs may intersect only as clones — the same origin, whatever their
  sizes, which is what xrandr's `--same-as` produces. Every edit is validated
  as a whole and reverted on failure; the GUI reports "<edit> is not possible
  here: A overlaps B" like arandr's messages, a refused drop snaps back with
  "<output> not moved: A overlaps B" in the status bar.
- **Clones read back as mirrors**: an active output sharing its origin with
  an earlier one (a screen already running `DP-2 --same-as DP-1`, or our own
  Mirror-of after Apply → New) loads as *Mirror of* that output — drawn
  dashed, follows its target, saves as `--same-as` — so such screens stay
  editable (arandr writes two `--pos 0x0`). Chained `--same-as` in a script
  (`A --same-as B`, `B --same-as C`) is flattened onto the root like xrandr's
  fixpoint; a cycle is an error.
- **Normalisation** (deviation: arandr refuses negative positions): after each
  edit the active outputs shift so the layout's top-left is (0, 0). Dragging
  past the left/top edge therefore moves everything else. Inactive outputs
  keep their last position and are parked (dimmed) right of the layout.
- Activating an output places it right of the layout with its preferred
  mode, normal orientation, scale 1 (arandr puts it at 0,0 on top of whatever
  is there). Deactivating drops primary and detaches mirrors of it.
- Screen bound: a layout wider/taller than the server's maximum is refused
  with arandr's "A part of an output is outside the virtual screen."

## Command line (`Layout.args()`)

One stanza per output in server order, arandr's shape and order, extras only
when they carry information:

```
--output N --off                               (inactive or disconnected)
--output N [--primary] --mode M [--rate R] (--pos XxY | --same-as O)
           --rotate R [--reflect x|y|xy] [--scale SxS]
```

`--rate` appears only when the chosen rate differs from what `--mode` alone
would pick (the `+` preferred rate, else the first listed), so an untouched
layout produces byte-for-byte what arandr 0.1.11 saves (tested against a
genuine arandr save of the same screen). `--scale SxS` appears when the
output is scaled *or was scaled when the screen was read* (`1x1` then) —
xrandr and wxrandr both keep an existing scale when it is not mentioned.
Apply runs the backend once with the whole line **on a worker thread** (the
window keeps repainting while a slow or hung compositor takes its 30 s
timeout; Apply is greyed meanwhile); a non-zero exit shows stderr in an
"XRandR failed" dialog and **keeps the edited layout** (arandr raises before
re-reading the screen); on success the screen is re-read. The status bar
shows the command Apply would run.

## Layout scripts

```
#!/bin/sh
xrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal --output HDMI-2 --off
```

arandr's format and arandr's default template: `#!/bin/sh` first line,
exactly one command line — a fresh save of an untouched screen is
byte-identical to arandr's — everything else is a template that survives a
reload (arandr's `%(xrandr)s` mechanism — a file loaded from arandr saves
back byte-identical, extra script lines included). Loading accepts `xrandr` and `wxrandr` command words, `--off`,
`--primary`, `--mode`, `--rate`, `--pos`, `--rotate`, `--reflect`, `--scale`,
`--same-as`, `--left-of/--right-of/--above/--below` (resolved against the
new geometry), and re-bases on the *current* outputs like arandr (unknown
output/mode → error). A stanza with `--primary` moves the primary (the
previous one is cleared even when the script does not mention it, as
xrandr does); a mentioned output without `--primary` loses it (arandr). Saving appends `.sh`, chmods 0700, defaults to
`~/.screenlayout/` — all arandr habits. The command word on save is the
current backend's, so an X11 arandr file re-saved on Wayland says `wxrandr`.
arandr itself can load our files when they use only arandr's vocabulary
(`--rate/--reflect/--scale/--same-as` make its parser bail — documented gap).

**A forced backend is recorded, as a comment.** A layout script has to stay
arandr's and stay runnable by `sh` on a plain X11 box, and `--backend` is not
an option the real `xrandr` would ignore — it would abort on it — so the note
is a comment and nothing else:

```
#!/bin/sh
# warandr: backend mutter forced (wxrandr --backend mutter)
wxrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal
```

It appears only when a backend was forced (an untouched auto save is
byte-identical to arandr's, as before) and only in the *default* template, so
a file loaded from disk is still written back byte-identically — arandr's
template rule wins. Nothing reads it back: it documents which backend the
window was talking to when the layout was captured, so that a hotkey script
that misbehaves on another machine says why. To pin the backend in a script,
edit the command word's line yourself (`wxrandr --backend mutter --output
...`) — on that machine, where it is true.

## GUI (`warandr/gui.py`)

Window "Screen Layout Editor" (arandr's title). Menus: **Layout** (New, Open,
Save As, Apply ⌃⏎, Script Properties ⌥⏎, Quit), **View** (Zoom In/Out/Fit
stepping through arandr's 1:4 / 1:8 / 1:16 radios, which follow; default
1:8), **Outputs** (one submenu per output, disconnected ones greyed),
**Help** (About). Layout also carries **Backend ▸** (right after Script
Properties, with the two things it governs — arandr has no such menu, and
View is about how the canvas is drawn, not about who answers): radio items
*Automatic*, *X11 (xrandr)*, *sway*, *wlroots (wlr)*, *GNOME (mutter)*,
*KDE (kwin)*. A backend this session cannot reach is **insensitive** with the
reason in its tooltip (GTK 3 pops no tooltip over an insensitive item, so the
same table is spelled out in Script Properties ▸ Backend); the current choice
is never greyed out from under the user. Choosing one re-reads the layout
through that backend and redraws the canvas — a `New`, so unapplied edits go,
like arandr's New — and from then on Apply, the command in the status bar,
Save As and the per-output menu (Scale is Wayland-only) are that backend's.
One that cannot be reached leaves everything as it was: the dialog says
`Cannot use the mutter backend: ...` in the Apply-failure style, the radio
goes back, and the window is never left empty. Toolbar: Apply | New,
Open, Save As | zoom. Canvas: dark grey, one box per connected-or-active
output, a distinct pastel per output (server order), black border, name big
and underlined when primary (arandr), resolution (and `= mirror target` /
`@scale`) below, label rotated with the orientation, dimmed when inactive,
white border when selected, dashed when a mirror. No tooltips (one would pop
over the context menu): hovering a box puts its description in the status
bar. Left-drag moves (snap on motion, validate on drop, revert + status
message on rejection); right-click (or the Outputs menu) opens arandr's
per-output menu — **Active**, **Primary**, **Resolution** ▸, **Orientation** ▸
(normal/right/inverted/left) — then, after a separator, **Refresh rate** ▸,
**Reflection** ▸, **Mirror of** ▸ (none / each other non-mirror active
output) and, on Wayland only, **Scale** ▸. Right-click on empty canvas: the
outputs menu. Empty-layout Apply asks first, like arandr. The status bar
shows, by priority, a transient message (refused drop, saved file; cleared
by the next redraw or after a few seconds), the hovered output's
description, or the command Apply would run — which is the *whole* command,
so a forced backend shows there too (`wxrandr --backend mutter --output
...`); a saved script still gets the bare command word. Right of that line,
always visible, is the backend indicator: `backend: mutter (Wayland)`,
`backend: xrandr (X11)` — the compositor backend on Wayland (once
`--print-backend` has answered, `wxrandr` until then), the tool itself on
X11. Its tooltip is the fuller explanation, `--print-backend --verbose`'s
lines under what warandr runs and why it picked it; the same text is a
paragraph in the About dialog and a page of Script Properties. Save As reports `saved PATH`
and, when the script's command word is not on `PATH` (a stock desktop has
no `wxrandr`), appends `- note: wxrandr is not on PATH, the script needs
it` — arandr's scripts call bare `xrandr`, ours call bare `wxrandr`, and a
hotkey running the script needs it installed. Without a display warandr
exits 1 with one line (`warandr: cannot open display ...`).

## CLI

`warandr [--randr-display D] [--force-version] [savedfile]` (arandr's, the
last one accepted and ignored — warandr never refuses a RandR version), plus
`--save FILE` (write the current layout — or SAVEDFILE re-based on the
current outputs — as a layout script, no GUI), `--command` (print what
Apply would run, the `--backend` flag included when one is forced) and the
two backend spellings, the same as wxrandr's so a hotkey can pin one:
`--backend NAME` (applies to the GUI, `--command` and `--save` alike; an
unknown name is `warandr: unknown backend 'banana' (valid: auto, x11, sway,
wlr, mutter, kwin)`) and `--print-backend`, which prints the token
(`x11`, `mutter`, ...) and exits without a GUI — with `--verbose`, the same
explanation the indicator's tooltip carries, under a first line that is
still the bare token, spelled like wxrandr's own `--print-backend
--verbose`:

```console
$ warandr --print-backend --verbose
mutter
kind: Wayland
runs: /usr/bin/python3 -m wxrandr
chosen by: wxrandr package at /usr/lib/python3/dist-packages
session: wayland
compositor: Mutter
protocol: org.gnome.Mutter.DisplayConfig (D-Bus)
available: yes
```

(One `chosen by:` line, never two: warandr passed the `--backend` flag that
wxrandr would otherwise report back to it, so the inner answer is dropped
where it only restates the outer one.) Exit 1 with `warandr: ...`
on backend/parse/file errors.

## Launching on GNOME (verified live: Ubuntu 24.04 / GNOME 46, 26.04 / GNOME 50)

- **Install**: `scripts/build-pyz.sh`; `dist/warandr` runs on the stock
  desktop (`python3-gi` + `gir1.2-gtk-3.0` are there, GTK 3.24.41 / 3.24.52,
  PyGObject 3.48 / 3.56, Python 3.12 / 3.14) and carries wxrandr inside for
  its own Apply. The scripts it saves call bare `wxrandr` (arandr's shape,
  bare `xrandr`), so install `dist/wxrandr` as `/usr/local/bin/wxrandr` too;
  Save As's status line says when it is missing. `warandr.desktop` goes to
  `~/.local/share/applications/` (or `/usr/share/applications/`).
- **Hotkeys**: GNOME custom shortcuts (Settings ▸ Keyboard, or `gsettings`
  on `org.gnome.settings-daemon.plugins.media-keys custom-keybindings`)
  run their command with the session's environment — `warandr` on one,
  `~/.screenlayout/desk.sh` on another, nothing to export. Bind chords Mutter
  does not own: `<Ctrl><Alt>F1`–`F12` are its VT switches (the chord changes
  the VT and gsd logs `Failed to grab accelerator`); `<Super>F6`/`<Super>F7`
  work. The window is up ~2 s after the chord; a three-head layout script
  restores the screen in about a second (0.65 s / 0.99 s measured).
- **Temporary, like xrandr**: Apply uses Mutter's non-persistent method —
  no "Keep changes?" dialog, nothing written to `monitors.xml` — and Mutter
  drops it at the next hotplug or login (`wxrandr --persistent` is the
  other way). The shortcut script is how a layout comes back, as with arandr.
- **Mutter allows no gaps**: a layout leaving a hole between monitors
  (`--pos 5000x0`) is refused by Mutter itself; the dialog shows its text
  (`XRandR failed: xrandr: Logical monitors not adjacent`), the screen is
  unchanged and the edited layout stays on the canvas. Mutter keeps
  neighbours adjacent for wxrandr's own size changes (WXRANDR.md).
- **Hotplug**: a monitor plugged in while the window is open is not picked
  up until New (Ctrl+N) — arandr's behaviour; the new head appears where
  Mutter put it (right of the row, e.g. `Virtual-4 1280x1024 at 5760x0`).
  Mutter itself discards the temporary layout at hotplug and re-lays every
  monitor out in a row; after the unplug GNOME 46 keeps that row, GNOME 50
  restores the pre-hotplug temporary layout.
- **Keyboard focus after Apply**: when an Apply changes the set of
  monitors (an output turned off or on), Mutter moves the keyboard focus
  off the window — on GNOME 50 the next `Ctrl+S` landed in the desktop
  (Desktop Icons' "Clear Current Selection before New Search" dialog) —
  until it is clicked again. A Wayland client cannot take focus back
  itself, so click the window (a driver: the canvas) before the next
  accelerator; mouse clicks on the boxes and toolbar keep working.
- **Windows on Wayland**: `org.gnome.Shell.Introspect.GetWindows` is
  `AccessDenied` on a stock session and wdotool has no GNOME window
  backend there, so a driver finds the window by screenshot; `super+Up`
  maximizes it so the layout dump's window-relative coordinates become
  absolute after the shell's chrome offset (dock + top bar: (66, 32) on
  24.04, (67, 32) on 26.04 at 1920x1080; `"window"` is then 1853x1048).
  Idle: 0 % CPU, ~70–80 MB RSS, no GTK/GLib warnings on either release.

## Test hooks (env)

- `WARANDR_TEST_LAYOUT_DUMP=FILE`: append one JSON line per redraw
  (`{"kind":"layout","boxes":{name:[x,y,w,h]},"buttons":{...},
  "menubar":{...},"xid":..,"coords":..,"window":[w,h],"settled":..,
  "backend":..,"backend_label":..,
  "factor":..,"status":..,"busy":..,"command":[...]}`), per popup menu
  (`"kind":"menu","name":..,"items":{label:[x,y,w,h]},"modelled":{...},
  "sensitive":{label:bool},"tooltips":{label:text},"active":{label:bool},
  "coords":..`), per status-bar change (`"kind":"status","text":..`), per
  backend indicator refresh (`"kind":"backend","name":..,"forced":..,
  "indicator":..,"word":..,"available":{name:bool},"ok":true`, and
  `"ok":false,"wanted":..,"error":..` for a refused switch), per
  apply (`rc`, `stderr`) and per save (`path`), so xdotool/wdotool can click
  real widgets. Coordinates: `"coords":"root"` — root-window pixels — on X11;
  `"coords":"window"` on Wayland, where a toplevel's position is unknowable
  and everything is relative to the toplevel surface (which includes the
  CSD shadow unless the window is maximized; `"window"` is the surface
  size, `"xid"` null). A layout line is written only once GTK has
  allocated every box at the size and place the redraw asked for
  (`"settled":true`; after 3 s of waiting it is written anyway with
  `false`): the frame clock's layout phase runs after an idle callback, and
  on Wayland it waits for the compositor's frame callback, which stalls
  while Mutter reconfigures outputs right after an Apply — a dump taken
  then would carry the previous boxes. Popup items: GDK cannot read a
  popup's position back on Wayland (`get_origin` is a constant), so
  `"modelled"` holds positions computed from what GTK asked the compositor
  for — a menu popped at the pointer sits at pointer + (1, 1), a submenu at
  its parent item's north-east corner shifted by the menu's
  `horizontal-offset`/`vertical-offset` style and top padding (its first
  item level with the parent item), a menubar drop-down at the item's
  south-west corner; `"items"` is what a driver clicks: GDK's truth on X11,
  the model on Wayland. The X11 GUI test checks the model against GDK for
  all three placements (it is exact under Xvfb); unconstrained placement is
  assumed — near a screen edge the compositor may flip or slide a popup.
- `WARANDR_TEST_SAVE_AS=FILE`: Save As writes there without a file chooser.

## Files

`warandr/{__init__,__main__,cli,randr,xrandr_parse,model,gui}.py`,
`warandr.desktop`, `scripts/build-pyz.sh` (→ `dist/warandr`, bundling wdotool
+ wxrandr so the Wayland backend runs from inside the pyz), `pyproject.toml`
console script. Tests: `tests/test_warandr_parse.py` (Xvfb captures, an
xrandr 1.5.4 laptop capture, wxrandr renders for 1–4 outputs),
`tests/test_warandr_model.py` (geometry, snapping, edits, command line,
scripts incl. `tests/fixtures/arandr-saved.sh` — a genuine arandr 0.1.11
save — backend choice, forcing one and asking which it is, the saved
script's backend comment, CLI), `tests/test_warandr_gui.py` (Xvfb + xdotool
drive of the real editor against `tests/fixtures/fake_xrandr.py`, a RandR
simulator rendering through wxrandr's renderers, including the popup
position model against GDK's X11 truth, plus the backend indicator,
Layout ▸ Backend with its greyed-out entries, a switch that re-reads through
the new backend and one that is refused, and warandr's own
`--backend`/`--print-backend`/`--print-backend --verbose`;
`tests/fixtures/gui_probe.py`, the editor in-process with a stub backend —
Apply off the main loop, failure keeps edits, layout dumps that wait for the
allocation, popup release, zoom radios, menu shapes, the Save As PATH hint;
a no-display run; skipped
without GTK/Xvfb).
