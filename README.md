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
- **warandr** — arandr, the drag-your-monitors GUI, on Wayland (via wxrandr) and X11 (via xrandr)

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
  needs root — or, if you'd rather not: one udev rule (`sudo sh
  gnome/install-bridge.sh --udev` installs `gnome/60-fuckwayland-uinput.rules`,
  which tags `/dev/uinput` for the logged-in user's ACL — no group, nobody
  else) and it runs as a plain user, no relogin needed. Media keys work too
  (`key XF86AudioMute` and friends map straight to their evdev codes).
- The first invocation forks a small daemon that owns the devices (creating them
  costs ~600ms of hotplug; you pay it once) and tracks the injected pointer.
- **Window management** talks to the compositor: sway/i3 IPC (complete), GNOME
  Shell through the bundled bridge extension (complete, see [GNOME](#gnome)), KWin
  scripting (best-effort), and the wlr-foreign-toplevel protocol as the generic
  fallback. Window ids are real, stable, decimal — like X window ids,
  scripts pipe them around unchanged.
- Runs fine under `sudo`: the graphical session's sockets are found by scanning
  `/run/user/*`.

## Install

Nix: `nix build` → `result/bin/wdotool` (with an `xdotool` symlink next to it).

No nix: `scripts/build-pyz.sh` → `dist/wdotool`, a single self-contained file.
Python ≥ 3.10 stdlib only, no dependencies. Copy it to `/usr/local/bin/wdotool`,
`ln -s wdotool /usr/local/bin/xdotool`, done.

### GNOME

Stock GNOME Wayland sessions — Ubuntu 24.04 (GNOME 46) and 26.04 (GNOME 50) as
installed — are supported, with one extra step: GNOME has no window-management
protocol, so the window side needs a small GNOME Shell extension that exports
Mutter over the session bus. Input injection needs nothing extra beyond
`/dev/uinput` access.

```sh
sh gnome/install-bridge.sh          # copies the extension, enables it; log out/in once
sh gnome/install-bridge.sh --check  # is it loaded? is org.fuckwayland.Bridge owned?
sudo sh gnome/install-bridge.sh --udev   # optional: /dev/uinput for the logged-in user, no relogin
```

* **The extension** (`gnome/fuckwayland-bridge@fuckwayland`, ~1100 lines of
  JavaScript, see `gnome/README.md`) is installed per user by default
  (`--system` for `/usr/share/gnome-shell/extensions`). gnome-shell only scans
  extension directories at login, so the first install needs a logout/login
  (or `--try-unsafe`, which drives Looking Glass through `wdotool` to load it
  in place); after that the installer can enable and disable it live. Everything
  `wdotool`/`wwmctl`/`wxprop` do on GNOME goes through it: `search`,
  `windowactivate`, `windowmove`, `windowstate`, desktops/workspaces,
  `selectwindow`, `getmouselocation`'s window, X ids of XWayland windows.
* **The udev rule** (`gnome/60-fuckwayland-uinput.rules` + a `modules-load.d`
  file) tags `/dev/uinput` `uaccess`, so systemd-logind hands the user of the
  active seat an ACL on it — applied immediately by the installer, and again at
  every login. Nothing else: the node stays `root:root 0600`, no `input`-group
  route (`--udev --uninstall` restores exactly that). Without it, run the tools
  as root (`sudo`), which also works: the session is found by scanning
  `/run/user/*`.
* **Hotkeys**: bind scripts to anything but `Ctrl+Alt+F1`…`F12` — Mutter owns
  those as VT switches on Wayland, so gsd cannot grab them and injecting them
  switches the console. `<Ctrl><Super>F7` works.
* **Security note.** Any process on your session bus can then list, move,
  close and kill your windows through the bridge, and anyone who can open
  `/dev/uinput` can type as you. That is exactly what every X11 client could
  always do, and it is the point of these tools; but it is a deliberate
  widening of GNOME's default. The bridge never evaluates code and never
  injects input; Flatpak/Snap apps without session-bus access cannot reach it.
  Do not install either piece on a machine where that trade is wrong.

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

![wwmctl listing native and X windows in one list, then acting on them](wwmctl-demo.gif)

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

![wxprop rendering a _NET_WM_ICON as ASCII art, byte-identical to real xprop](wxprop-demo.gif)

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

![wxrandr reshaping a multi-output layout live: panels sliding, rotating, scaling](wxrandr-demo.gif)

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

It also works on a stock GNOME desktop — Ubuntu 24.04 (GNOME 46) and 26.04 (GNOME 50)
as installed, no shell extension, no root: wxrandr talks to
`org.gnome.Mutter.DisplayConfig` on the session bus through the toolbox's own
stdlib D-Bus client and submits the whole layout as one `ApplyMonitorsConfig` call.
Relative placement, rotation, mirroring (`--same-as` becomes one logical monitor),
scales snapped to what Mutter offers, `--primary`, `--off` and `--dryrun` (Mutter
verifies the configuration without applying it) all map; Mutter's own validation
errors ("Logical monitors not adjacent", "Logical monitors overlap") come back as
one-line `xrandr:` failures — and since Mutter, unlike X, allows no gaps, an output
that changes size (`--rotate`, `--mode`, `--scale`, `-s`, `-o`) keeps its neighbours
touching it, with a warning. Changes are temporary like xrandr's; `--persistent`
writes `monitors.xml` (GNOME then asks "Keep changes?"). It finds the session from a
custom keyboard shortcut, under `sudo`, or from `ssh root@` with no environment.

## warandr

`arandr` — the little GTK window where you drag your monitors around — reborn for
Wayland. Same window, same menus, same `~/.screenlayout/*.sh` scripts: warandr
loads arandr's saved layouts and arandr loads warandr's. Under Wayland it talks to
`wxrandr` (one atomic apply per click), under X11 to plain `xrandr`, and it tells
you in the status bar exactly which command Apply is about to run:

```console
$ warandr                      # the GUI: drag, snap, right-click, Apply
$ warandr --command            # what Apply would run, no GUI
wxrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal --output HDMI-A-1 --mode 1280x1024 --pos 1920x0 --rotate left
$ warandr --save ~/.screenlayout/desk.sh   # an arandr-compatible layout script
$ cat ~/.screenlayout/desk.sh              # bind this to a hotkey, as with arandr
#!/bin/sh
wxrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal --output HDMI-A-1 --mode 1280x1024 --pos 1920x0 --rotate left
```

On top of arandr's menu (Active, Primary, Resolution, Orientation) every output also
gets Refresh rate, Reflection, Mirror of, and — Wayland only — Scale (1 … 3, the
compositor's HiDPI factor). Overlaps are refused unless they are clones (same
origin, like `--same-as`), the layout is kept anchored at 0,0, Apply runs off the
main loop and a failed Apply keeps your edits. It needs the GTK 3 bindings every stock Ubuntu
desktop already has (`python3-gi`, `gir1.2-gtk-3.0`) and nothing else — no cairo:
the canvas is plain widgets. `warandr.desktop` puts it in the Settings menu.
Contract: `WARANDR.md`.

On a stock GNOME desktop (verified on Ubuntu 24.04 and 26.04, Wayland session):
`scripts/build-pyz.sh`, copy `dist/warandr` and `dist/wxrandr` to `/usr/local/bin/`
— warandr itself carries wxrandr inside, but the layout scripts it saves call bare
`wxrandr`, exactly like arandr's call bare `xrandr`, so a script bound to a hotkey
needs it on `PATH` (Save As says so in the status bar when it is missing). Bind
`warandr` and `~/.screenlayout/desk.sh` to GNOME custom shortcuts on `<Super>F`-keys
(`<Ctrl><Alt>F1`–`F12` are Mutter's VT switches); the script restores a three-head
layout in about a second. Apply is temporary, like xrandr: Mutter drops it at the
next hotplug or login, so the shortcut script *is* the way to keep a layout. A
monitor plugged in while the window is open shows up after New (Ctrl+N), as in
arandr; a layout with a gap between monitors gets Mutter's own "Logical monitors not
adjacent" in the error dialog and stays on the canvas to be fixed. One GNOME habit
to know: an Apply that turns a monitor off or on makes Mutter move the keyboard
focus off the window, so click it before the next Ctrl+S.

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
