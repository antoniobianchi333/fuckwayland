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

### X11

The tools are meant to be installed **over** the originals, so they also have
to behave when the session is a plain X11 one (Xfce, i3, GNOME-on-Xorg,
KDE-on-Xorg): there they detect the session and hand over to the real
`xdotool`/`wmctrl`/`xprop`/`xrandr` with `execve`, argv untouched — same exit
status, same signals, same stdio, no extra process. One script then runs on
both session types, and `xdotool --version` on X11 answers with the version
that is actually installed there.

```console
$ FUCKWAYLAND_PASSTHROUGH=never xdotool key a   # our own code, whatever the session
$ FUCKWAYLAND_PASSTHROUGH=always ...            # hand over, whatever the session
$ WDOTOOL_REAL_XDOTOOL=/opt/bin/xdotool ...     # where the original is
```

`WDOTOOL_PASSTHROUGH`, `WWMCTL_PASSTHROUGH`, `WXPROP_PASSTHROUGH` and
`WXRANDR_PASSTHROUGH` do the same per tool (`warandr` ignores all of them: it
never hands over, it only picks between the `xrandr` and `wxrandr` command
words, and it keeps doing that by session); the `*_REAL_*` variables are
`WDOTOOL_REAL_XDOTOOL`, `WWMCTL_REAL_WMCTRL`, `WXPROP_REAL_XPROP`,
`WXRANDR_REAL_XRANDR`. With no original installed you get exit **127** and a
line saying which package to install — except for `--help`/`--version`, which
still answer, and `wxprop`, which has an X11 client of its own and simply
keeps working. Detection is Wayland-first: `$DISPLAY` is set on a Wayland
session too (Xwayland), so only a live compositor socket — or `loginctl`'s
own record of your session — counts.

Bonus, on X11 as on Wayland: run under `sudo`, over `ssh root@box` or from
cron and we find the session's `DISPLAY` and `XAUTHORITY` and hand them to
the original, so `sudo xdotool key a` works *through* us where
`sudo /usr/bin/xdotool key a` says `Can't open display`.

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
| `getmouselocation` | asks the compositor where the pointer is (GNOME); falls back to the injected position where it cannot (sway) |
| `--clearmodifiers` | releases all modifier keys; can't read or restore prior state |
| `type` non-US chars | US layout table; unreachable characters warn and skip |
| `search --role` | roles don't exist on Wayland; matches against empty string |
| `windowraise`/`lower` | floating windows only (tiling has no z-order) |
| `set_window`, `windowreparent`, viewport/desktop-count setters | warn and succeed (cosmetic on Wayland; scripts keep running) |
| `behave`, `behave_screen_edge`, `windowmap --sync` waits on X events | unsupported, fail cleanly |

Desktops map to workspaces (0-based). `windowunmap`/`windowminimize` use the
scratchpad on sway.

GNOME has a longer list of honest differences (keyboard layouts, shell grabs,
the lock screen, `selectwindow`): see **Known limitations on GNOME** in
[gnome/README.md](gnome/README.md).

### Session readiness and exit codes

`wdotool` separates "there is no session to talk to" from "the session is
fine and nothing matched", so a cron job or a boot script can poll for a
desktop without guessing:

| rc | meaning |
|---|---|
| 0 | the command did what it says |
| 1 | the session is up and the command failed — no matching window, no active window, a wait that timed out |
| 2 | **no Wayland session found**: no compositor, no session bus, GNOME Shell absent, the screen locked, the greeter, or the bridge extension not running |

```sh
# wait for a usable desktop, then act
until wdotool getdisplaygeometry >/dev/null 2>&1; do sleep 2; done
```

`getdisplaygeometry` is the cheapest probe: it needs no window and no
`/dev/uinput`. It never invents a size — with no compositor reachable it
warns and exits 2 rather than printing a made-up `1920 1080`.

### `--sync` waits are bounded

Every `--sync` wait (`windowactivate`, `windowfocus`, `windowmap`,
`windowunmap`, `windowminimize`, `windowmove`, `windowsize`) gives up after
10 seconds with `wdotool: gave up waiting for …` and rc 1. Set
`WDOTOOL_SYNC_TIMEOUT` (seconds; `0` waits for ever) to change it. The one
exception is `search --sync`, which blocks until there are results — that is
what its manpage entry promises, and it is how scripts wait for an
application they have just launched.

### Pointer accuracy

`mousemove` and `mousemove_relative` are pixel-exact: the target is emitted
as an absolute position on a virtual tablet mapped across the whole output
layout, so neither pointer acceleration nor an already-identical coordinate
can lose the move. On sway/i3, relative moves keep using relative events
(that rig runs `pointer_accel 0`); `WDOTOOL_REL_MODE=abs|rel` forces either
mode anywhere.

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

On GNOME (with the bridge extension, see [GNOME](#gnome)) the same list mixes
XWayland windows under their real X ids with native windows under Mutter's ids,
`-d` prints GNOME's workspace names and work areas, `-m` says `GNOME Shell`,
`-k` and `-n` reach the shell, and every action goes through Mutter — including
`-b add,maximized_vert` as a real per-axis maximize. The X plane is reached with
Mutter's own Xwayland cookie, so it works from a custom shortcut, under `sudo`
and from `ssh root@` alike; Xwayland (which Mutter starts on demand) is never
spawned just to be listed. Details in `WWMCTL.md` § GNOME.

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

On GNOME (see [GNOME](#gnome)) native windows get their synthesized set from the
bridge — states, window types, `WM_CLASS` from the app id — and `-spy` follows the
shell's window events; `-root` is Mutter's real X root with `_NET_CLIENT_LIST`,
`_NET_ACTIVE_WINDOW` and the desktop properties re-synthesized so they cover native
windows too. Details in `WXPROP.md` § GNOME.

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
