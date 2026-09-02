# fuckwayland

The X11 power tools — `xdotool`, `wmctrl`, `xprop`, `xrandr` — reborn as no-bullshit
drop-in clones that work on Wayland. Same commands, same flags, same output bytes,
same scripts, bugs faithfully included. Symlink them over the originals and your
muscle memory never finds out the compositor changed underneath it.

![reject modernity, embrace tradition](meme.svg)

(Yes, we see the irony: these tools embrace tradition *on top of* modernity. That's
the point — the tradition was better, so it came along.)

In the box:

- **wdotool** — xdotool, all 48 commands, byte-parity
- **wwmctl** — wmctrl, for native Wayland *and* legacy X apps in one list
- **wxprop** — xprop, real X properties for XWayland windows and synthesized ones for native windows
- **wxrandr** — xrandr, with first-class multimonitor: reshape crazy layouts in one atomic call

## wdotool

xdotool, but it works on Wayland. Drop-in: same commands, same flags, same output
bytes, same chaining, same scripts. Symlink it as `xdotool` and your scripts don't
know the difference.

![wdotool driving a real Wayland desktop](demo.gif)

*(that's wdotool driving a live sway session: typing, chaining, window search,
floating-window moves, fullscreen, mouse, close — recorded in the Ubuntu 26.04 VM
this repo tests in)*

```console
# wdotool search --class foot windowactivate --sync type 'echo hello from wayland'
# wdotool key Return
# wdotool mousemove 640 360 click 1 getmouselocation
x:640 y:360 screen:0 window:5
```

## How

There is no X server to lie to, so wdotool goes underneath instead:

- **Input** is injected as kernel-level virtual devices via `/dev/uinput` — a
  keyboard, a relative mouse, and an absolute tablet (the same shape QEMU uses, which
  every compositor maps across the whole output layout). The compositor can't tell it
  from real hardware, so this works on GNOME, KDE, sway, anything. That's also why it
  needs root — or, if you'd rather not: one udev rule
  (`KERNEL=="uinput", GROUP="input", MODE="0660"` in
  `/etc/udev/rules.d/60-wdotool.rules`) plus membership in the `input` group and it
  runs as a plain user. Media keys work too (`key XF86AudioMute` and friends map
  straight to their evdev codes).
- The first invocation forks a small daemon that owns the devices (creating them
  costs ~600ms of hotplug; you pay it once) and tracks the injected pointer.
- **Window management** talks to the compositor: sway/i3 IPC (complete), KWin
  scripting and GNOME Shell (best-effort), and the wlr-foreign-toplevel protocol as
  the generic fallback. Window ids are real, stable, decimal — like X window ids,
  scripts pipe them around unchanged.
- Runs fine under `sudo`: the graphical session's sockets are found by scanning
  `/run/user/*`.

## Install

Nix: `nix build` → `result/bin/wdotool` (with an `xdotool` symlink next to it).

No nix: `scripts/build-pyz.sh` → `dist/wdotool`, a single self-contained file.
Python ≥ 3.10 stdlib only, no dependencies. Copy it to `/usr/local/bin/wdotool`,
`ln -s wdotool /usr/local/bin/xdotool`, done.

## Compatibility

All 48 xdotool commands are implemented, with output byte-compatible against
xdotool 4.20260303.1 (including `--help` text, error strings, and several verbatim
C bugs, e.g. `windowmove`'s percent-y quirk).

Wayland forces a few honest approximations:

| | |
|---|---|
| `key`/`type` `--window` | activates the target first, then injects (no XSendEvent) |
| `getmouselocation` | reports the injected pointer, not the physical one |
| `--clearmodifiers` | releases all modifier keys; can't read or restore prior state |
| `type` non-US chars | US layout table; unreachable characters warn and skip |
| `search --role` | roles don't exist on Wayland; matches against empty string |
| `windowraise`/`lower` | floating windows only (tiling has no z-order) |
| `set_window`, `windowreparent`, viewport/desktop-count setters | warn and succeed (cosmetic on Wayland; scripts keep running) |
| `behave`, `behave_screen_edge`, `windowmap --sync` waits on X events | unsupported, fail cleanly |

Desktops map to workspaces (0-based). `windowunmap`/`windowminimize` use the
scratchpad on sway.

## wwmctl

`wmctrl`, same treatment — and it handles **both** native Wayland apps and legacy X
apps (XWayland) in one list. The compositor exposes XWayland windows with their real
X11 window ids, so wwmctl prints ids that `xprop` and your old scripts can actually
use, enriched straight from the XWayland server over a built-in X11 wire client.
Native windows ride along with compositor node ids:

```console
$ wwmctl -lGpx
0x00000005  0 31496  0    23   640  697  foot.foot             host yans@host: ~
0x0040000c  0 31526  642  23   636  695  xterm.XTerm           host yans@host: ~

$ wmctrl -lGpx        # the real one, on the same desktop
0x0040000c  0 31526  1284 46   636  695  xterm.XTerm           host yans@host: ~
```

Real wmctrl on Wayland can't see the foot window at all, prints doubled coordinates
(a non-reparenting-xwm quirk), its `-c` silently closes nothing, and `-a` only sets
an urgency hint. wwmctl routes every action through the compositor, so `-a`
focuses, `-c` closes, `-e` moves — for X and Wayland windows alike. Symlink it as
`wmctrl` (nix does this for you) and byte-parity covers the rest: help text, list
formats, error strings, even wmctrl 1.07's machine-column width bug.

## wxprop

`xprop`, dual-plane. XWayland windows report their **real** X properties, byte-for-byte
identical to xprop 1.2.8 — the whole formatting machine is ported, down to the
`WM_HINTS`/`WM_SIZE_HINTS` structured dumps, the dformat mini-language, 32-bit
sign-extension quirks, and yes, the `_NET_WM_ICON` ASCII-art renderer. Native Wayland
windows get a synthesized property set in the same grammar, so `xprop -id N WM_CLASS`
script parsing works on every window:

```console
$ wxprop -id 0x0040000c WM_CLASS       # an XWayland window — real X property
WM_CLASS(STRING) = "xterm", "XTerm"

$ wxprop -id 5 WM_CLASS                # a native Wayland window — synthesized
WM_CLASS(STRING) = "foot", "foot"
```

`-set`/`-remove`/`-spy` work on the X plane; `-f`/`-fs`/dformats, `-len`, `-root`,
`-name`, click-to-select all match the real tool (including which double-dash forms it
rejects). Verified byte-identical against the real xprop on a live XWayland server.

## wxrandr

`xrandr`, with the crazy multimonitor configs as the whole point, not an afterthought.
A real pending-geometry resolver means relative-placement chains resolve in **one
atomic invocation**:

```console
$ wxrandr --output DP-2 --right-of DP-1 --output HDMI-A-1 --below DP-2 --rotate left
$ wxrandr --output DP-2 --scale 1.5x1.5 --output DP-1 --primary
$ wxrandr --output HDMI-A-1 --same-as DP-1        # mirror
```

Mirroring, rotation, reflection, mixed per-output scales, portrait/landscape mixes,
custom modelines (`--newmode` with real pixel-clock math), holes in a row, negative
origins, `--dryrun` — all first-class, applied through sway IPC or an atomic
wlr-output-management backend. `--brightness`/`--gamma` run over wlr gamma-control via
a detached holder process (the control dies with its client, so we simply refuse to
die). Query/`--listmonitors` output is byte-styled after xrandr 1.5.4, and the layout
stays consistent with the rest of the toolbox: after any change, `wdotool
getdisplaygeometry` and `wwmctl -d` track the new world.

## Fully vibed, fully awesome

Every line of this repo was written by AI (Claude): the design contracts, the code,
the torture rigs, the hostile fake X servers, the byte-parity oracles, the VM demo,
this README, and yes, the meme. Fully vibed. Also fully awesome: 646 tests and
counting, live-compositor integration suites, byte-for-byte output parity against
the real tools (verbatim bugs included), and every "it works" claim proven inside a
real Ubuntu 26.04 VM before it shipped. Vibe-check the code yourself — it can take it.

## Testing

Developed against a real Ubuntu 26.04 VM driving headless sway through the full
uinput path — see `vm/` for the whole rig (`mkvm.sh`, `run.sh`, `compositor.sh`) and
`tests/` for the suite: unit, live-compositor integration, hostile fake X servers,
and byte-parity oracles against the real xdotool and wmctrl binaries.
