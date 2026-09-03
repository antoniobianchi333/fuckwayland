# wxprop — design contract

Drop-in `xprop` clone for Wayland: works on **XWayland windows** (real X properties,
byte-parity with real xprop) and **native Wayland windows** (a synthesized property
set printed in xprop's exact formats, so `xprop -id N WM_CLASS`-style script parsing
just works). Same house rules as DESIGN.md/WWMCTL.md: pure-stdlib Python, nix-only
toolchain, byte-parity oracles, agents never commit.

## Planes

- **X windows** (id ≥ the X resource base, or found via the compositor tree's
  `"window"` field): everything goes through `wwmctl.x11_mini` against the real
  XWayland server — genuine GetProperty/ListProperties output, -set/-remove via
  ChangeProperty/DeleteProperty, -spy via PropertyNotify events. Real xprop in the
  devshell is the byte oracle for these.
- **Native windows** (compositor node ids): synthesize the property set from
  compositor data and print it byte-compatibly: WM_CLASS(STRING) from app_id,
  WM_NAME/_NET_WM_NAME from title, _NET_WM_PID(CARDINAL), _NET_WM_DESKTOP,
  _NET_WM_STATE (fullscreen/hidden/sticky as applicable), WM_CLIENT_MACHINE,
  _NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL. `-len`/`-notype`/format
  args apply identically. `-set`/`-remove` on a native window: one clear error line,
  exit 1 (can't fake a property store). `-spy` on native: sway IPC window-event
  subscription, reprint a synthesized property when its source changes.
- Window selection: `-id` (0x-hex/decimal), `-name` (exact match on title, then on
  instance/class like dsimple.c's Window_With_Name semantics — check source),
  `-root` (X root when X is up: real root properties; without X: synthesized
  _NET_SUPPORTING_WM_CHECK-ish minimal set — document), no selector = click-to-select
  → compositor next-focus selection with the stderr hint (reuse the wwmctl pattern).
  `-frame` is a no-op flag (no reparenting frames on wlroots) — accept it.

## Files

- `wxprop/__init__.py`, `wxprop/__main__.py` — skeleton (done).
- `wxprop/cli.py` — option parsing exactly per xprop (it has its own hand-rolled
  parser; order matters, `-f name format [dformat]` triples, trailing
  `[format [dformat]] atom` groups), usage/-help/-grammar text byte-parity.
- `wxprop/core.py` — plane resolution, property assembly, -spy loops.
- `wxprop/fmt.py` — THE parity heart: xprop's formatting machinery from xprop.c:
  per-type default formats (0s/8s/32x/32c/32a...), dformats, the built-in fallback
  table (WM_HINTS and WM_SIZE_HINTS structured dumps, ATOM lists, WINDOW
  "window id # 0x%x", quoted strings with escape rules, COMPOUND_TEXT, multi-value
  comma joins, `-len` truncation (Xlib word-cap + an 8-byte-per-32-bit-item byte
  budget — NO "..." ellipsis; it just yields fewer/shorter fields, verified against
  the oracle and xprop.c), _NET_WM_ICON's ASCII-art icon
  renderer — yes, really, xprop draws the icon; copy the algorithm from xprop.c).
- `wwmctl/x11_mini.py` — ADDITIVE-ONLY extensions allowed (it is shared with
  wwmctl; the full suite must stay green): `list_properties(win)`,
  `get_atom_name(atom)`, `read_property(win, name) -> (type_name, format, bytes) | None`,
  `delete_property(win, name)`, `change_property(...)` generalization if needed,
  `select_input(win, event_mask)` + `next_event(timeout) -> parsed PropertyNotify`.
- `tests/test_wxprop*.py` — unit (formatting table against captured oracle bytes),
  live (own headless sway + xwayland, xterm vs foot, real xprop oracle diffs).

## Parity references

Prep drops these in SCRATCH/reference/: xprop manpage, cloned xprop source
(gitlab.freedesktop.org/xorg/app/xprop), oracle dumps (full default dump on xterm,
-notype, -len N, -root, -spy transcript, error paths). The devshell has real xprop.

## Notes

- Property VALUES for X windows are the server's truth — never synthesize for a
  window that has a real X id; parity diffs must be byte-exact vs real xprop for the
  same window (modulo _NET_ properties the compositor updates between calls — pin
  the window state first).
- Exit codes and stderr strings per xprop source (e.g. "No such property" wording,
  usage exit 1).
- `-display`, `-fs`, `-grammar` edge flags: -display honored for the X plane;
  -grammar prints the real grammar text.
- `-font <name>` is real: XWayland serves the core fonts (xfonts-base), so the
  font plane is `OpenFont` + `QueryFont` on the X connection and the FONTPROPs
  print through xprop's *font* format table — which replaces the window one for
  the whole run, chosen by pre-scanning argv the way xprop.c does. Values carry
  no type, so no `(TYPE)` is printed and a property the table does not name
  falls back to xprop's default `0x` (bare hex). `-remove`/`-set` on a font are
  the oracle's own `… works only on windows, not fonts`, and `-spy` dumps and
  exits. Byte-identical to `xprop -font fixed` on GNOME 46.

## GNOME

On GNOME the compositor plane is the fuckwayland bridge
(`wdotool.backend_gnome.GnomeBackend`, `gnome/README.md`); wxprop uses its
typed hooks — `views()`, `workspaces()`, `x_info()`, `events()`,
`select_window()` — ahead of the sway tree path, which is unchanged.

* **Planes.** `views()` says which windows are XWayland (`xid ≠ 0`) and
  which are native. `-id` takes an X id or a bridge id: an XWayland
  window's bridge id (`wdotool search` output) redirects to its X id and
  the real X properties, exactly as a sway node id does; a native window
  gets the synthesized set. An id the bridge does not know is handed to
  the X server like xprop would — but only when Xwayland is actually
  running (a typo must not spawn a server; see below), else `window id #
  0x… does not exists!`.
* **Atom ids.** The native plane has no X server to allocate atoms, so it
  numbers the EWMH names itself, from `0x40000000` — deliberately outside
  the range any X server hands out, so a numeric id copied out of a native
  window's dump and fed to a real X tool fails loudly instead of naming a
  plausible wrong atom.
* **Native windows** synthesize, in this order and in xprop's grammar:
  `_NET_WM_STATE` (Mutter's own atom order: `SKIP_TASKBAR`,
  `MAXIMIZED_HORZ`, `MAXIMIZED_VERT`, `FULLSCREEN`, `HIDDEN` (minimized or
  show-desktop), `ABOVE`, `DEMANDS_ATTENTION`, `STICKY` — sway's
  synthesis keeps its `FULLSCREEN, HIDDEN, STICKY` subset),
  `_NET_WM_WINDOW_TYPE` from `Meta.WindowType` (`DESKTOP`, `DOCK`,
  `DIALOG` (also for `MODAL_DIALOG`), `TOOLBAR`, `MENU`, `UTILITY`,
  `SPLASH`, `DROPDOWN_MENU`, `POPUP_MENU`, `TOOLTIP`, `NOTIFICATION`,
  `COMBO`, `DND`, else `NORMAL`), `_NET_WM_DESKTOP` (`0xFFFFFFFF` when
  sticky), `_NET_WM_PID`, `WM_CLIENT_MACHINE` (hostname), `WM_CLASS`
  (`app_id`, `app_id` — the same pair `wwmctl -lx` prints; a window with a
  `WM_CLASS` pair but no app id uses that pair), `_NET_WM_NAME`, `WM_NAME`,
  `WM_STATE` (`Normal`/`Iconic` from the window's visibility, icon window
  `0x0` — Mutter writes it on every X11 window it manages, so a script
  that asks "is this minimized?" gets the same answer on both planes).
  `WM_TRANSIENT_FOR` appears when the bridge reports a parent, and
  `_NET_WM_STATE_FOCUSED` last in `_NET_WM_STATE`, where Mutter's own
  `set_net_wm_state` puts it. A `WM_NAME` that does not fit latin-1 is
  typed `UTF8_STRING`: type `STRING` *means* ISO 8859-1 and xprop re-encodes
  it for the locale, so UTF-8 bytes typed `STRING` print as mojibake.
  Nothing else is invented (no `_NET_FRAME_EXTENTS`, no `WM_HINTS`).
  `-set`/`-remove` on them fail with the usual one line.
* **The X plane** is opened with the `DISPLAY`/`XAUTHORITY` the bridge
  reports (`x_info()`: gnome-shell's own, else Mutter's
  `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` cookie found by
  `wdotool.session`), which is what makes `ssh root@`, `sudo` and a GNOME
  custom-shortcut process all work; `-display` still wins and then uses
  `$XAUTHORITY`/the session cookie. Mutter spawns Xwayland **on demand**,
  so wxprop connects only when an XWayland window is listed or an
  `Xwayland` process exists (`session.xwayland_running()`): `wxprop -root`
  and `-name` on a purely native desktop never start an X server. If
  Xwayland is up but unreachable, an XWayland window degrades to the
  bridge's view of it (`WM_CLASS` from Mutter's pair).
* **`-root`** — the documented choice: with Xwayland up the target is the
  **real X root** (Mutter is a full EWMH window manager for Xwayland:
  `_NET_SUPPORTING_WM_CHECK` → the `GNOME Shell` check window,
  `_NET_WORKAREA`, `_NET_SHOWING_DESKTOP`, `_NET_SUPPORTED`, … all real,
  `-set`/`-remove` go there) **with six properties re-synthesized from the
  bridge**, because the X root only ever sees X clients:
  `_NET_CLIENT_LIST` and `_NET_CLIENT_LIST_STACKING` (every window, by the
  id the tools print — X id for XWayland, bridge id for native — in
  Mutter's stacking order), `_NET_ACTIVE_WINDOW` (the focus window on
  either plane, `0x0` when none), `_NET_NUMBER_OF_DESKTOPS`,
  `_NET_CURRENT_DESKTOP`, `_NET_DESKTOP_NAMES` (the workspace manager
  directly; Mutter's X root tracks the same values, but only once an X
  client exists). Without Xwayland the root is the synthesized set alone,
  same as on sway, plus `_NET_CLIENT_LIST_STACKING` and
  `_NET_DESKTOP_NAMES`, with `_NET_SUPPORTING_WM_CHECK` = `0x0`. Real
  xprop on the same session would print the X-only list; the merged view
  is the point of the tool.
  `-set`/`-remove` always address the real X root, never the synthesis.
  That gap is where damage used to disappear: `wxprop -root -remove
  _NET_CLIENT_LIST` breaks every EWMH client on the X plane (`wmctrl -l`:
  *Cannot get client list properties*) while `-root` went on printing the
  compositor's healthy-looking list. Writing or removing one of the six
  now prints a line on stderr saying where the write went, and reads of
  that name for the rest of the run come from the X root. It is *not*
  enough to treat a missing override as damage: Mutter writes
  `_NET_CURRENT_DESKTOP` on the X root only once the workspace first
  changes, so a fresh GNOME 46 session legitimately has none while the
  compositor knows the answer.
* **`-spy`** on an XWayland window is the X `PropertyNotify` loop. On a
  native window it follows the bridge's `WindowEvent` signals
  (`backend.events()`): `title` reprints `WM_NAME`/`_NET_WM_NAME`,
  `fullscreen_mode`/`minimized`/`urgent` reprint `_NET_WM_STATE`,
  `workspace` (also on stickiness changes) `_NET_WM_DESKTOP` and
  `_NET_WM_STATE`, `close` ends with exit 0; the window is re-read from
  the bridge before each print. On the root without Xwayland the
  `new`/`close`/`focus` window events and the `WorkspaceEvent`s reprint
  the synthesized set; with Xwayland the two streams are merged — the X
  root's own `PropertyNotify`s for everything Mutter owns, the bridge for
  the six synthesized names (an X-side update of one of those is *not*
  reprinted: it would show the X-only view).
* **Click-to-select** (no `-root`/`-id`/`-name`) is the bridge's
  `SelectWindow` with the stderr hint: focus the target window (a
  different one) and wxprop continues with it on whichever plane it lives.
  **`-name`** keeps xprop's semantics first — pre-order `QueryTree` walk
  from the X root, exact `WM_NAME` match, frames included (Mutter's
  `mutter-x11-frames` windows may carry the client's title, bug-for-bug)
  — when Xwayland is up, then exact title, then exact app id over the
  bridge's windows.
* **Errors** are one line, exit 1: without the bridge, click-to-select
  says `can't select a window: gnome backend: the fuckwayland bridge
  extension is not running in GNOME Shell; run gnome/install-bridge.sh …`,
  `-id N` for a window the X server does not have says `cannot look up
  window id # 0x…: gnome backend: …`, `-root` without Xwayland `cannot
  examine the root window: gnome backend: …`; `-name` keeps `No window with
  name … exists!`. Unit coverage: `tests/test_wxprop_gnome.py` on the
  mock bridge plus an in-memory X server stand-in.

Verified live (same rigs as WWMCTL.md's GNOME section, GNOME 46 and 50):
the full `-id <xterm>` dump is byte-identical to real `xprop` (31 lines);
`-root` differs from real `xprop -root` in exactly the merged names
(`_NET_CLIENT_LIST(_STACKING)` and `_NET_ACTIVE_WINDOW` covering the
native windows — Mutter's X root lists the xterm only and, on 50, names
its own no-focus window `0x200003` as active — plus `_NET_CURRENT_DESKTOP`,
which Mutter's X root does not carry); native windows dump the synthesized
set; a bridge id of the xterm redirects to its X properties; `-spy` on the
calculator reprints `_NET_WM_STATE` through fullscreen/hidden toggles and
exits 0 when it is closed; `-root -spy` prints the focus change and the
workspace switches (a switch can arrive more than once, as an X
`PropertyNotify` storm would); `-name "Both by wwmctl"` finds the
`mutter-x11-frames` frame first, exactly as real `xprop` does;
`-set WM_NAME` / `-remove WM_ICON_NAME` on the xterm are read back by real
`xprop`; click-to-select returns with the window `wdotool windowactivate`
focused; all of it also from `ssh root@` with `env -i`, under `sudo` and
from a custom shortcut.
