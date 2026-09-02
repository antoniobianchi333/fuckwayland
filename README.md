# wdotool

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

## Testing

Developed against a real Ubuntu 26.04 VM driving headless sway through the full
uinput path — see `vm/` for the whole rig (`mkvm.sh`, `run.sh`, `compositor.sh`) and
`tests/` for the suite (unit + live-compositor integration).
