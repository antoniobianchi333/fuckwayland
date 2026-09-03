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
- **GNOME / Mutter**: `org.gnome.Mutter.DisplayConfig` on the session bus over the
  pure-stdlib `wdotool.dbus_mini` — see "Mutter backend" below. Stock Ubuntu 24.04
  (GNOME 46) and 26.04 (GNOME 50), no extension, no root.
- KWin: out of scope (CmdError with a one-line hint).

Backend selection: `WXRANDR_BACKEND=sway|wlr|mutter` (alias `gnome`); otherwise a
sway/i3 IPC socket wins, then a session bus owning `org.gnome.Mutter.DisplayConfig`,
then wlr. `--listproviders` names it (`name:sway`, `name:wlroots`, `name:mutter`).

## Mutter backend (`wxrandr/mutter.py`)

Mutter has no `zwlr_output_management`; its display API is the D-Bus object
`/org/gnome/Mutter/DisplayConfig` (`GetCurrentState` + `ApplyMonitorsConfig`), which any
client on the user bus may call. The bus is found like the compositor socket
(`session.find_session_bus`: `$DBUS_SESSION_BUS_ADDRESS` when it belongs to the Wayland
session's user, else the `bus` in the runtime dir owning the Wayland socket), so
wxrandr works from a GNOME custom keyboard shortcut, from `sudo`, and from
`ssh root@host` with an empty environment (root's `/run/user/0` bus is skipped; when
dbus-daemon turns root away, `dbus_mini` reconnects as the socket's owner).

What maps:

| xrandr | Mutter |
|---|---|
| output name, make/model/serial, mm size | monitor connector, spec; mm from `width-mm`/`height-mm` when Mutter sends them, else from `wl_output.geometry` (Mutter 46/50 never emit the D-Bus keys — verified with gdbus — but give the EDID size to `wl_output`, which is what XWayland's RandR prints: 320mm x 200mm on a QEMU head, byte-identical here) |
| mode table (`*` current, `+` preferred, `WxHi` interlaced) | the monitor's mode list; opaque ids kept verbatim (`Mode.mode_id`) |
| enabled, `WxH+X+Y`, rotation/reflection, scale | membership of a logical monitor; its x/y, transform, scale. Mutter numbers transforms like `wl_output` with the spec's counter-clockwise 90 — through Mutter's XWayland real xrandr prints 1 as `left`, 3 as `right`, 5 as `left X axis`, 7 as `right X axis` (all eight measured on GNOME 50), whereas sway's verified table has "90" == `right`; `mutter.MUTTER_RANDR_VIEW` holds the measured words and the 1↔3 / 5↔7 permutation follows from it |
| `--mode/--rate/--auto/--preferred` | mode id chosen by size + nearest rate |
| `--scale S` | snapped to the nearest of the mode's `supported_scales` (warning when it changed); logical size = `roundf(px / scale)` in layout-mode 1, raw pixels in layout-mode 2 (GNOME 46 without "Fractional Scaling": integer scales only) — the dryrun plan uses the same math |
| `--pos`, `--left-of/--right-of/--above/--below` | positions in Mutter's logical space, resolved against pending sizes like everywhere else |
| `--same-as` | one logical monitor with several members (Mutter requires the same mode, rotation and scale: otherwise `xrandr: cannot mirror B onto A: ...`) |
| `--primary` | the real primary flag (exactly one; what Mutter reports overrides the state file). GNOME 50 keeps a stale `primary=true` on the previous logical monitor after a temporary re-primary (until that monitor is rebuilt), so when several are flagged the legacy `GetResources` output property `primary` — which tracks the real one, as XWayland shows — breaks the tie |
| `--off` | the connector is left out of the configuration. A disabled output cannot stay primary on Mutter (X keeps the flag and prints `connected primary` for it): the primary moves to the first enabled output and the query shows it there |
| `--listmonitors` | one RandR monitor per active output, the primary listed first (the X server orders monitors that way; verified against real xrandr on GNOME 50) |
| several `--output` stanzas | one `ApplyMonitorsConfig` call — atomic; after it, wxrandr waits for `MonitorsChanged` (≤ 5 s) and re-reads |
| `--newmode/--addmode/--rmmode/--delmode` | state file as on sway/wlr; *applying* a custom mode works only when a real mode with that size and rate exists, else `cannot find mode NAME` |

What warns and succeeds: `--brightness`/`--gamma` (Mutter exposes no gamma LUT),
`--noprimary` (GNOME requires a primary; the current one is kept), an output enabled
without a position (`--auto` on a disabled output; xrandr would put it at 0,0, which
Mutter rejects as an overlap, so it is placed right of the rightmost output), a scale
Mutter cannot do (snapped), `--filter`/`--set`/`--transform`/`--panning` as elsewhere.

Holes: X tolerates a gap, Mutter does not (every logical monitor must share an edge
with another). So an output that touched a neighbour's right or bottom edge keeps
touching it when that edge moves because the neighbour changed mode, rotation or
scale: `--output A --rotate left`, `--output A --mode SMALLER`, `--scale`, `-s`, `-o`
in the middle of a row shift the outputs right of (below) it along, chains included,
one warning each — `xrandr: output C moved to +3000+0 to stay adjacent to A`. Explicit
positions are the user's: an output given `--pos`/`--right-of`/... in the same call
never moves and nothing follows it (its old neighbours may not be neighbours any
more), and an output whose neighbour went `--off` is not moved either; those layouts
get Mutter's own verdict — re-place the neighbour in the same call
(`--output C --right-of A`). The `--verbose`/`--dryrun` plan and the `--fb` check
show the shifted layout.

What fails, one line and exit 1, with Mutter's own text: a hole (`xrandr: Logical
monitors not adjacent`, see above), overlapping positions
(`Logical monitors overlap`), turning everything off (`Monitors config incomplete`),
`ApplyMonitorsConfigAllowed=false` (`Monitor configuration via D-Bus is disabled`).
A configuration serial that went stale between our read and the apply is re-read:
when the monitors and the layout are still the ones the plan was built from (GNOME
bumps the serial on its own as well) the same call is retried once; otherwise — a
hotplug or someone else's re-layout in that window, which the plan knows nothing
about — it is `output configuration cancelled by a concurrent change; try again`.

Persistence: by default the apply uses method 1 (temporary — xrandr semantics, no
dialog; the layout lasts until the next hotplug/login). `--persistent` (a wxrandr
option, not in xrandr's usage text) or `WXRANDR_PERSIST=1` uses method 2: the layout
is applied and gnome-shell shows its "Keep changes?" dialog — wxrandr prints a
one-line warning; confirming it makes Mutter write `~/.config/monitors.xml`, otherwise
the previous layout comes back after 20 s (verified on GNOME 46: nothing is written
before the confirmation, and there is no D-Bus call to confirm from outside the
shell). `--dryrun` additionally submits the exact configuration with method 0 (verify
only) and prints `mutter verify: ok` on stderr (stdout stays xrandr's own dryrun
lines), or Mutter's rejection as the fatal a real run would give.

Launching it (verified on 24.04 and 26.04): a GNOME custom keyboard shortcut runs the
command with the session's environment, nothing to set up — but bind a chord Mutter
does not own: `<Ctrl><Alt>F1`–`F12` are its VT switches (`switch-to-session-N`),
gsd-media-keys logs `Failed to grab accelerator` and the chord changes the VT instead;
`<Super>F8` works. A `cron @reboot` job, or any script with an empty environment,
needs no exports at all (the session is found from the runtime dir owning the Wayland
socket), only to wait until the session bus is up and owns the name — on the rig
`org.gnome.Mutter.DisplayConfig` appears 3–4 s after the session; loop on
`python3 -m wxrandr --listmonitors` until it exits 0. Toggle scripts: `--output C
--right-of B` on a disabled output only positions it (xrandr semantics); to turn it
on say `--output C --auto --right-of B`.

Coordinates are Mutter's logical ones (what `wl_output`/xdg-output and GNOME
Settings show). Real `xrandr` through XWayland agrees byte for byte on GNOME 46 and,
at scale 1, on GNOME 50; Ubuntu 26.04 ships XWayland native scaling, so with any
output at scale 2 real xrandr there reports every output multiplied by that integer
factor (`Virtual-1 2560x1600+0+0` for a 1280x800 panel next to a scale-2 monitor) —
an X-plane artefact wxrandr does not imitate.

Tests: `tests/test_wxrandr_mutter.py` runs the whole CLI against a wire-level mock
DisplayConfig service on `dbus_mini`'s mock bus that validates like mutter
(serial, ids, scales, adjacency, overlap, primary, offset) and emits `MonitorsChanged`.

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
