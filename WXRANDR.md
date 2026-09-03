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
- **KDE Plasma / KWin**: the plasma-wayland-protocols pair `kde_output_device_v2`
  (read) + `kde_output_management_v2` (write) over `wayland_mini` — see "KWin
  backend" below. Unauthenticated (no portal, no polkit): the same path
  kscreen-doctor and the System Settings KCM take. Plasma 5.27 (Ubuntu 24.04)
  through 6.7+, no extension, no root.

## Backend selection

Precedence, one rule: **`--backend NAME` beats `WXRANDR_BACKEND=NAME` beats
auto-detection.** `NAME` is `auto` (the default), `x11`, or one of
`sway|wlr|kwin|mutter` (aliases `kde` and `gnome`). Detection is unchanged: a
sway/i3 IPC socket wins, then a compositor advertising
`kde_output_management_v2`, then a session bus owning
`org.gnome.Mutter.DisplayConfig`, then wlr — which is the fallback and is
therefore never probed for the decision. The KWin probe *is* the Wayland
connection the backend then keeps, so a KDE session still opens exactly one,
and a backend forced with the flag is probed the same way.
`--listproviders` names the chosen one (`name:sway`, `name:wlroots`,
`name:kwin`, `name:mutter`).

`x11` means *hand over to the real xrandr* — what happens by itself on an X11
session. That handover is an `execve` at the top of `main()`, **before any
option is parsed**, so the hook looks ahead in argv for `--backend NAME` and
`--backend=NAME` (and for the two informational options below) and honours
it: `--backend sway` on an X11 session runs our own code, `--backend x11` on
a Wayland session hands over, and the flag is stripped from the argv the
original is exec'd with (real xrandr has no such option). The look-ahead
walks argv with xrandr's own option arities, so an output literally named
`--backend` (`--output --backend --off`) is a value there too and is handed
over untouched.

Three options real xrandr does not have, and — like `--persistent` — not in
its usage text, so `--help` and every other byte stay xrandr's:

* **`--backend NAME`**. An unknown name lists the valid ones
  (`xrandr: --backend: invalid argument 'banana'; valid: auto, x11, sway,
  wlr, mutter, kwin` + xrandr's `Try 'xrandr --help' for more information.`,
  exit 1). A backend that is not available *in this session* is one clear
  line naming what was missing and exit 1, never a silent fallback:
  `xrandr: --backend sway is not available in this session: no sway or i3 IPC
  socket ($SWAYSOCK)`. `$WXRANDR_BACKEND` deliberately keeps its older
  behaviour — no pre-check, so it still fails the way the backend itself
  fails (`Can't open display`, `org.gnome.Mutter.DisplayConfig is not on the
  session bus`) and no existing byte moves.
* **`--print-backend`** prints the chosen backend and exits 0 without
  touching the layout (and without handing over, so it answers on X11 too).
  First line: the bare token, for scripts. `--verbose` adds the rest.

  ```console
  $ wxrandr --print-backend
  mutter
  $ wxrandr --print-backend --verbose
  mutter
  session: wayland
  chosen by: detection
  compositor: Mutter
  protocol: org.gnome.Mutter.DisplayConfig (D-Bus)
  available: yes
  ```

  `chosen by:` is `detection`, `flag (--backend mutter)` or
  `environment (WXRANDR_BACKEND=mutter)`; `protocol:` carries the version
  where the protocol has one (`kde_output_management_v2 version 12`,
  `zwlr_output_manager_v1 version 4`), `compositor:` sway's own
  `GET_VERSION` string where it has one; and for `x11` there is a
  `real xrandr: /usr/bin/xrandr` line naming what would be exec'd.
* **`--backends`** — one line per backend, its availability in this session,
  a short true reason when it has none, and `*` on the one *auto* would
  choose (not on a forced one — `--print-backend` answers that):

  ```console
  $ wxrandr --backends
    sway    unavailable  no sway or i3 IPC socket ($SWAYSOCK)
    kwin    unavailable  the compositor does not advertise kde_output_management_v2
  * mutter  available    org.gnome.Mutter.DisplayConfig on the session bus
    wlr     unavailable  the compositor does not advertise zwlr_output_manager_v1
    x11     available    /usr/bin/xrandr
  ```

  This is what warandr greys its Backend menu with.

Tests: `tests/test_wxrandr_backend.py` (hermetic — the probes are a table, no
socket or bus is touched: precedence, the detection order, the look-ahead in
both directions, the two outputs byte for byte, every error path) and, for
the handover itself, `BackendFlag` in `tests/test_passthrough_exec.py`
against the fake install tree.

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

## KWin backend (`wxrandr/kwin.py`)

KWin has no `zwlr_output_management` and no D-Bus display API (`org.kde.KWin` only
exposes `activeOutputName()`). Everything goes through the Wayland protocols from
plasma-wayland-protocols — NOT from the kwin repo — which are unauthenticated:
`kde_output_device_v2` per output (read), `kde_output_management_v2`
(`create_configuration` → per-output setters → one `apply`), and
`kde_output_order_v1` (read: which output is primary).

| xrandr | KWin |
|---|---|
| output name, make/model/serial, mm, subpixel, EDID | `name` (device v2; `uuid` is the fallback), `geometry` make/model + `serial_number`, `geometry` mm/subpixel, `edid` (base64, collected for callers, not printed) |
| enabled, `WxH+X+Y` | `enabled` / request `enable`; `geometry` x/y — **logical** coordinates, and the position is not scaled |
| mode list, current, preferred | one `mode` object per mode (server-allocated new_id; `size` in hardware pixels, `refresh` in mHz), `current_mode` (sent only while enabled), the mode's `preferred` marker. `mode` takes an object, never a WxH triple, so a mode is matched by size + nearest refresh |
| `--rotate`/`--reflect` | `transform` (wl_output enum, as libkscreen reads it: 1 → xrandr `left`, 3 → `right`, 4..7 the same rotations reflected — the same 90↔270 permutation of the sway names the Mutter backend needs) |
| `--scale S` | `scale` (wl_fixed), quantised server-side to 1/120 with `std::round` (so `--scale 1.4375` lands on 173/120 = 1.44167, exactly where `kscreen-doctor` puts it); logical size = transform-swapped mode size ÷ scale, and **how that becomes an integer changed with Plasma 6**: 6.x takes the enclosing integer (1920 ÷ 1.4 → 1372, 1080 ÷ 1.4 → 772, measured against `kscreen-doctor` at seven scales), 5.27 rounds (1371). One pixel short is not cosmetic — the neighbour then overlaps by a pixel and KWin keeps it — so the rule is gated on the advertised management version (≥ 7 is Plasma 6). Never `core.logical_size`'s wlroots truncation |
| `--primary` | `set_priority(dev, 1..N)` (management v3) over the whole list, `--primary` first and the rest in the order KWin already has — libkscreen's own `setPrimaryOutput` semantics. `set_primary_output` (v2) is sent alongside but is **accepted and ignored** by KWin on 5.27 and on 6.6 alike (measured: the output order does not move, XWayland's `primary` does not move), so it can never be the mechanism. Read back from `kde_output_order_v1` — its first entry is the primary plasmashell and XWayland follow, and it is advertised on 5.27 too, where the device `priority` event (v18) is out of reach. Below management 3, `--primary` warns and is *not* written to the state file: `--query` never names a primary the compositor was not asked for |
| `--same-as` | plainly the same position: KWin does not enforce the XML's no-gaps/no-overlaps sentence (`set_replication_source` is a different, management-v13 concept and is not used) |

Versions move fast — 5.27 advertises device 2 / management 3, 6.0 6/7, 6.3 11/12,
6.4–6.5 16/16, 6.6 20/19, master 25/22 — so we bind LOW (device 2 for `name`,
management `min(advertised, 12)` for `set_priority` + `failure_reason`) and
gate every optional feature on the *bound* version: on 5.27 a rejection carries no
reason string at all and says so. Discovery has two shapes and both are
implemented: per-output `kde_output_device_v2` globals (5.27..6.6) and, from Plasma
6.7 where that global is gone, `kde_output_device_registry_v2` (bound at ≥ 21)
handing device objects out as new_ids. `done` is the publish barrier: an output
appears in the snapshot only after it arrives — and management advertised with not
one device published (a registry too old to bind, say) is a refusal
(`xrandr: kde_output_management_v2 is advertised but the compositor announced no
outputs`), not an empty screen and an apply that silently succeeds.

Apply is atomic and **one-shot** — a second `apply` on one configuration object is
a fatal `already_applied` protocol error that kills the connection — so every
attempt builds a fresh object, and only deltas are sent (an unchanged invocation
creates no configuration at all: a no-op changeset still costs a modeset). An
output coming back from disabled is described in full. A hotplug between
`create_configuration` and `apply` silently invalidates the object. The retry is
keyed on evidence, not on that message: `failure_reason` is management v12, so on
5.27 (which advertises 3) the string can never arrive, and what is checked instead
is whether the set of published device objects moved while the configuration was
open. Then it is rebuilt once from a fresh snapshot, mode objects and all. A
rejection with the outputs unmoved is reported, not retried. Every socket error on
the send side is one `xrandr:` line too, never a bare `[Errno 32] Broken pipe`.

What we refuse client-side, before KWin's own message: disabling every output
(`xrandr: cannot disable all outputs (KWin requires at least one enabled output)`)
and an apply against a compositor that published no output at all.
What we normalise: the layout slides back to the origin, because KWin rejects any
enabled output at a negative coordinate. What warns and succeeds:
`--brightness`/`--gamma` (probed — no `zwlr_gamma_control_manager_v1` under KWin
and no LUT call in the protocol), `--noprimary` (KWin's output order always has a
first entry, so there is no inverse to set), `--set`/`--transform`/`--panning` as
elsewhere. What fails like Mutter:
`--newmode`/`--addmode` applied without a real mode of the same size and rate
(`cannot find mode NAME`) — protocol custom modes need management v18 plus
`capability_custom_modes`, which nothing we bind offers.

**KWin always saves the layout.** Plasma 6 writes `~/.config/kwinoutputconfig.json`
from `applyOutputConfiguration` itself; on 5.27 `kded5 kscreen` watches the same
libkscreen monitor and writes `~/.local/share/kscreen/<hash>`. There is no
temporary mode and no confirmation dialog — the 15-second countdown belongs to the
System Settings KCM, not the compositor — so `--persistent` is the only mode there
is. Every apply that KWin actually took says so once, and prints the `xrandr …`
command that restores the pre-apply snapshot, which is the only undo there is; an
apply KWin refused says neither (nothing was saved, and that command would *change*
the live layout). Because it is the only undo, it spells every property out —
`--mode/--rate/--pos/--rotate/--reflect/--scale`, plus `--primary`, defaults
included — so replaying it verbatim really is the inverse (verified live: layout,
scale, rotation and primary all come back). The one thing it cannot express is
KDE's full output *order*: xrandr has no syntax for it, and libkscreen permutes the
non-primary ranks the same way. `--dryrun` runs the plan client-side only (mode
resolution, the last-output refusal) and touches nothing — not even the state
file's primary: KWin has no verify request, so nothing is claimed about it.

Tests: `tests/test_wxrandr_kwin.py` runs the whole CLI against a wire-level fake
KWin — a real unix-socket wl_display speaking all three protocols over the actual
Wayland wire format, and modelling KWin's own behaviour including a
`set_primary_output` that does nothing — covering both discovery paths, both
version pairs, the mode-object matching, the 1/120 scale round trip, the Plasma
5.27/6 logical-size split, negative-coordinate normalisation, delta-only
changesets, the one-shot rule, the invalidation retry with and without a reason
string, failure with and without one, the restore command replayed as an inverse,
the last-output refusal, the no-output refusal, a compositor that hangs up
mid-apply and the query bytes.

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
