# Changelog

Every claim in this file was measured on the VM rig, or on a real desktop, before it
was written down. The README keeps one short section per version; this is the long
form.

## Version 0.2

Six tools, and on sway nothing wdotool injects needs a privilege at all.

- **On sway, nothing wdotool injects needs a privilege any more.** The pointer goes
  through `zwlr_virtual_pointer_v1` where the kernel device is closed to us, as the
  keyboard already went through `zwp_virtual_keyboard_v1` — so `click`, `mousemove`
  and the rest join `type` and `key` in needing no root, no group and no udev rule
  there. The two halves are chosen separately and by the same rule, so a compositor
  that implements one and not the other gets the protocol for that one and the kernel
  device, and its error, for the other. Absolute moves on the protocol path land with
  0.000 error, measured over 14 targets on a three-head layout with one head at a
  negative origin and one at scale 1.5, and relative moves cannot be accelerated
  there at all, because a virtual pointer is not a libinput device. See
  [WDOTOOL.md](docs/WDOTOOL.md#typing-and-clicking-with-no-privilege---vkbd).
- **What each desktop does with a layout after you set it**, measured through a
  hotplug and a reboot on all four, with where each one keeps it and what that means
  for a layout script. KDE saves whether you want it to or not: KWin has no temporary
  mode, every apply it takes lands in `~/.config/kwinoutputconfig.json` in the same
  second, and `--persistent` is accepted and means nothing there. GNOME writes
  nothing unless a `--persistent` apply is confirmed in Mutter's own *Keep these
  display settings?* dialog. sway and X11 write nothing either way. The tables are in
  [WXRANDR.md](docs/WXRANDR.md) and [WARANDR.md](docs/WARANDR.md).
- **Plasma over Xorg** is an X11 session like any other and is handled like one, on
  both generations, with the two things that look like they should change the answer
  named and measured: KWin owns `org.kde.KWin` on the session bus there exactly as it
  does on Wayland, and the KWin script backend *would* half work. The handover is
  decided by the session and not by the bus, before any backend is detected, which is
  the whole reason it is right.
- **KWin 6.7 stopped publishing outputs the way `wxrandr` found them.** From Plasma
  6.7.0 an output is no longer a `kde_output_device_v2` `wl_registry` global; the
  compositor hands the device objects out through a `kde_output_device_registry_v2`
  object instead (kwin `7e32e00c`, never backported — 6.6 still publishes the
  globals). On a real Plasma 6.7.4 session the old global is simply absent, so the
  second path is the only way to see an output at all, and `wxrandr` takes it. Query,
  mode, position, rotation, scale, `--off`, `--primary`, `--same-as` and hotplug all
  measured there against `kscreen-doctor`.
- **wmirror**, a sixth tool and the only one here that clones nothing. On wlroots it
  mirrors a **region** of an output, or a whole output onto a **differently shaped**
  one, by running the packaged
  [`wl-mirror`](https://github.com/Ferdi265/wl-mirror) and owning its lifetime — the
  two pictures output geometry alone cannot produce, and nothing else. Two outputs of
  the same size at the same position already mirror byte for byte on wlroots, so
  `wxrandr --output B --same-as A` stays the answer there and wmirror sends you to it
  rather than starting anything. It refuses by name what the measurements showed goes
  wrong, chief among them two outputs that **share pixels**, where a fullscreen mirror
  window is drawn on its own source: run that deliberately and both heads go entirely
  black, every pixel. Nothing it starts is left running that `wmirror --list` cannot
  find and `wmirror --stop` cannot end — not when its own supervisor is killed, not
  when a start is interrupted, not when two of them race — and a mirror ends itself
  when the layout moves out from under it. **GNOME and KDE have no unprivileged
  capture protocol at all**, and `wmirror --check` names what is missing instead of
  half working; on X11 the answer is `xrandr --same-as`, and it says so rather than
  naming a package that does not exist there.

Everything above was measured on the rig 0.1 left behind, the same desktop images
with the same heads plugged, resized and unplugged from outside the guest, and the
suite was 2085 tests.

## Version 0.1

The first tagged release. Everything below was measured on real desktops in the test
rig before it was claimed.

The tools run on **GNOME** 46 and 50, **KDE Plasma** 5.27 and 6.6, **sway** and the
wlroots family, and on any **X11** session, where they hand over to the originals
rather than pretending.

- **GNOME**: a Shell extension carrying the window commands, and monitor
  configuration straight through Mutter with nothing to install.
- **KDE**: window commands through KWin's scripting, monitor configuration through
  the KDE output protocol, including the compositor's own clone when two mirrored
  outputs would otherwise crop rather than copy.
- **X11**: the tools hand over to the real `xdotool`, `wmctrl`, `xprop` and `xrandr`
  with `execve` and argv untouched, so an X11 session behaves exactly as it did.
- **warandr**, an arandr clone for both worlds, which shows the backend in use and
  lets you change it from the window.
- **Keyboard layouts**: typing works under a non-US layout, by reading the
  compositor's own keymap and looking the character up backwards. On a plain US
  layout none of that code runs at all, verified key by key against the built-in
  table. On sway typing goes through the Wayland protocol built for it, so there it
  needs no privilege whatsoever.
- **`wdotool keys`**: watch what your keyboard really sends, or ask how to type a
  character on the layout you have.
- Partial **overlap** of outputs where the compositor allows it, with what that means
  on each one stated plainly.
- An install guide that was written by doing it and then re-run verbatim on fresh
  images of four desktops, and a threat model for a toolbox that deliberately injects
  input.

Behind it: a rig of seven desktop images with monitors that could be plugged, resized
and unplugged from outside the guest, and 1884 tests. The tools were stressed
deliberately on each desktop, and what that found is in the history — roughly fifty
defects, including a few that mattered: typing captured by another user, commands
that reported success while failing, and a monitor placed ten pixels wrong on the
wlroots backend at most fractional scales.
