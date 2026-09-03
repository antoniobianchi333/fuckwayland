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

1. `$WARANDR_XRANDR` — a command line to run instead (shlex-split). Its kind
   is Wayland when it mentions `wxrandr`, X11 otherwise; `$WARANDR_BACKEND`
   (`x11`/`wayland`) overrides the kind. Tests point this at the fake.
2. `$WAYLAND_DISPLAY` set and the `wxrandr` package importable (the repo
   checkout, or the pyz that bundles it): run the **same interpreter** with
   `-m wxrandr`, `PYTHONPATH` pointing at wherever the package was found (a
   zipapp path works — zipimport). No second copy of wxrandr is needed.
3. `$WAYLAND_DISPLAY` set and `wxrandr` on `PATH`.
4. `xrandr`.

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

## GUI (`warandr/gui.py`)

Window "Screen Layout Editor" (arandr's title). Menus: **Layout** (New, Open,
Save As, Apply ⌃⏎, Script Properties ⌥⏎, Quit), **View** (Zoom In/Out/Fit
stepping through arandr's 1:4 / 1:8 / 1:16 radios, which follow; default
1:8), **Outputs** (one submenu per output, disconnected ones greyed),
**Help** (About). Toolbar: Apply | New,
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
description, or the command Apply would run. Without a display warandr
exits 1 with one line (`warandr: cannot open display ...`).

## CLI

`warandr [--randr-display D] [--force-version] [savedfile]` (arandr's, the
last one accepted and ignored — warandr never refuses a RandR version), plus
`--save FILE` (write the current layout — or SAVEDFILE re-based on the
current outputs — as a layout script, no GUI) and `--command` (print what
Apply would run). Exit 1 with `warandr: ...` on backend/parse/file errors.

## Test hooks (env)

- `WARANDR_TEST_LAYOUT_DUMP=FILE`: append one JSON line per redraw
  (`{"kind":"layout","boxes":{name:[x,y,w,h]},"buttons":{...},"xid":..,
  "factor":..,"status":..,"busy":..,"command":[...]}`), per popup menu
  (`"kind":"menu","name":..,"items":{label:[x,y,w,h]}`), per status-bar
  change (`"kind":"status","text":..`), per apply (`rc`, `stderr`) and per
  save (`path`) — root-window pixel coordinates so xdotool can click real
  widgets.
- `WARANDR_TEST_SAVE_AS=FILE`: Save As writes there without a file chooser.

## Files

`warandr/{__init__,__main__,cli,randr,xrandr_parse,model,gui}.py`,
`warandr.desktop`, `scripts/build-pyz.sh` (→ `dist/warandr`, bundling wdotool
+ wxrandr so the Wayland backend runs from inside the pyz), `pyproject.toml`
console script. Tests: `tests/test_warandr_parse.py` (Xvfb captures, an
xrandr 1.5.4 laptop capture, wxrandr renders for 1–4 outputs),
`tests/test_warandr_model.py` (geometry, snapping, edits, command line,
scripts incl. `tests/fixtures/arandr-saved.sh` — a genuine arandr 0.1.11
save — backend choice, CLI), `tests/test_warandr_gui.py` (Xvfb + xdotool
drive of the real editor against `tests/fixtures/fake_xrandr.py`, a RandR
simulator rendering through wxrandr's renderers; `tests/fixtures/
gui_probe.py`, the editor in-process with a stub backend — Apply off the
main loop, failure keeps edits, popup release, zoom radios, menu shapes;
a no-display run; skipped without GTK/Xvfb).
