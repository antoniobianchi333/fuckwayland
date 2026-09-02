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
  comma joins, `-len` truncation with trailing "...", _NET_WM_ICON's ASCII-art icon
  renderer — yes, really, xprop draws the icon; copy the algorithm from xprop.c).
- `wwmctl/x11_mini.py` — ADDITIVE-ONLY extensions allowed (it is shared with
  wwmctl; all 429 existing tests must stay green): `list_properties(win)`,
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
- `-display`, `-fs`/`-font`, `-grammar` edge flags: -display honored for the X
  plane; font properties (-fs/-font) print xprop's error/unsupported path (no core
  fonts here) with a clean line; -grammar prints the real grammar text.
