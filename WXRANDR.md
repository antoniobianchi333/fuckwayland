# wxrandr — design contract

Drop-in `xrandr` clone for Wayland with **first-class multimonitor**: query and
reshape real multi-output layouts — relative positioning, mirroring, rotation,
reflection, per-output scale, custom modes, monitors — the crazy configurations are
the point, not an afterthought. House rules per DESIGN.md/WWMCTL.md.

## Planes / backends

- **sway/i3-compatible (flagship)**: query from `GET_OUTPUTS` (+ `wdotool.wayland_mini`
  wl_output for physical mm sizes); mutate via `output ...` IPC commands
  (mode/--custom, position, transform, scale, enable/disable, dpms).
- **Generic wlroots**: `zwlr_output_management_unstable_v1` over `wayland_mini`
  (ADDITIVE-ONLY changes there; wdotool's 257 input tests must stay green) — atomic
  apply of whole-layout configurations, which is exactly xrandr's model. This is the
  backend that makes crazy configs atomic: build the full config, apply once, handle
  `succeeded/failed/cancelled` events.
- **--brightness**: gamma via `zwlr_gamma_control_manager_v1` (ramps computed like
  xrandr's gamma math, passed over an fd). The control dies with its client, so a
  non-1.0 brightness forks a tiny detached holder process per output (pattern: the
  wdotool daemon fork, simplified); brightness 1.0 kills the holder. Insane, works.
  Headless outputs (WLR_BACKENDS=headless sway) have no gamma LUT, so the
  compositor refuses the control immediately — `--brightness`/`--gamma` there exit
  1 with `xrandr: Gamma size is 0.` (verified live). The holder lifecycle is
  therefore proven against a wire-level mock (tests/test_wxrandr_gamma.py), not a
  headless session. A typo'd `--output NAME --brightness` prints only the bare
  not-found warning and exits 0, like real xrandr (no holder is spawned).
- KWin/GNOME: out of scope for this pass (CmdError with a one-line hint).

## Command surface (byte-parity target: xrandr 1.5.x)

- Query: bare `wxrandr` / `-q` / `--query` (Screen line with minimum/current/maximum,
  per-output `NAME connected/disconnected [primary] WxH+X+Y (normal left inverted
  right x axis y axis) MMmm x MMmm` + mode table with per-rate columns, `*` current
  `+` preferred), `--verbose` (adds transform matrix, gamma/brightness, properties-ish
  block — match what's derivable, omit EDID), `--current`, `--listmonitors` /
  `--listactivemonitors` (RandR 1.5 monitor format from the output layout),
  `--listproviders` (one synthesized provider per GPU? print a single provider line
  for the compositor — document).
- Mutation (the fun): `--output NAME` with `--mode WxH`, `--rate R`, `--auto`,
  `--preferred`, `--off`, `--pos XxY`, **`--left-of/--right-of/--above/--below/--same-as
  OTHER`** (relative placement incl. mirroring via same-as; resolve against the
  target's *pending* geometry so chains like `--output B --right-of A --output C
  --right-of B` in ONE invocation work), `--rotate normal|left|right|inverted`
  (geometry WxH swap semantics identical to xrandr), `--reflect normal|x|y|xy`
  (sway transform "flipped-*" mapping), `--scale SxS`/`--scale-from WxH` (sway takes
  one float; use the x factor, warn if x≠y), `--primary` (persisted in a small state
  file keyed by compositor socket so listings show it consistently — no Wayland
  concept exists; document), `--brightness B`, `--gamma R:G:B`, `--dpi`, `--fb`,
  `--dryrun` (parse+resolve+print what would change, mutate nothing), `--newmode
  <name> <modeline>` / `--addmode OUT NAME` / `--delmode/--rmmode` (modeline store in
  the state file; pixel-clock+timings → WxH@refresh; applied via sway
  `mode --custom`), `--setmonitor` (warn+succeed; no virtual-monitor regions on sway).
- Multiple `--output` stanzas in one invocation = one atomic layout change (compute
  the whole target layout first, then apply; on the wlr backend literally atomic).
- Errors byte-styled like xrandr ("cannot find output", "cannot find mode") with its
  exit codes.

## Crazy-config requirements (torture will check these)

Headless sway grows outputs on demand (`swaymsg create_output`, `output X unplug`) —
build and verify for real: 3–4 output layouts; L-shaped and staircase arrangements;
negative origins; mixed scales (1 + 1.5 + 2); portrait (left/right) mixed with
landscape; mirrored pairs via --same-as; a custom --newmode applied to a headless
output; disabling the middle output of a row (holes are legal); repositioning in one
atomic call. Cross-tool invariant: after every layout change, `wdotool
getdisplaygeometry` and absolute `mousemove` must stay correct (the daemon re-reads
geometry per request — verify, and flag if caching breaks this), and `wwmctl -d`'s
WA geometry must track.

## Files

- `wxrandr/__init__.py`, `__main__.py` — skeleton (done).
- `wxrandr/cli.py` — xrandr's option parser (long-only options with one dash
  accepted? xrandr uses `--opt` strictly; check source), usage/--help byte-parity.
- `wxrandr/core.py` — layout model (Output: name, connected, enabled, mode list,
  current/preferred, pos, transform, reflect, scale, phys mm), pending-layout
  resolver for relative placement, sway apply + wlr atomic apply, state file
  (primary + custom modes) at `$XDG_RUNTIME_DIR/wxrandr-state.json` or /tmp per-uid
  fallback.
- `wxrandr/gamma.py` — brightness/gamma ramps + holder process.
- `tests/test_wxrandr*.py` — unit (format table, relative-placement resolver,
  modeline math) + live multimonitor scenarios per above.

## Parity references

Prep drops in SCRATCH/reference/: xrandr manpage, cloned source
(gitlab.freedesktop.org/xorg/app/xrandr), oracle dumps of real xrandr under rootless
XWayland (limited but format-true), error-path transcripts. Devshell has real xrandr.
