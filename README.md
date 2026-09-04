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

## Version 0.1

The first tagged release. Everything below was measured on real desktops in
the test rig before it was claimed here.

The five tools run on **GNOME** 46 and 50, **KDE Plasma** 5.27 and 6.6,
**sway** and the wlroots family, and on any **X11** session, where they hand
over to the originals rather than pretending. What each one does per desktop,
including where it differs, is the [support matrix](#desktop-support).

What 0.1 contains, beyond the sway-only toolbox it started as:

- **GNOME**: a Shell extension carrying the window commands, and monitor
  configuration straight through Mutter with nothing to install.
- **KDE**: window commands through KWin's scripting, monitor configuration
  through the KDE output protocol, including the compositor's own clone when
  two mirrored outputs would otherwise crop rather than copy.
- **X11**: the tools hand over to the real `xdotool`, `wmctrl`, `xprop` and
  `xrandr`, so an X11 session behaves exactly as it did.
- **warandr**, an arandr clone for both worlds, which shows the backend in use
  and lets you change it from the window.
- **Keyboard layouts**: typing works under a non-US layout, by reading the
  compositor's own keymap; on a plain US layout none of that code runs at all.
  On sway typing goes through the Wayland protocol built for it, so there it
  needs no privilege whatsoever.
- **`wdotool keys`**: watch what your keyboard really sends, or ask how to type
  a character on the layout you have.
- Partial **overlap** of outputs where the compositor allows it, with what that
  means on each one stated plainly.
- An [install guide](#install) that was written by doing it and then re-run
  verbatim on fresh images of four desktops, and a [threat
  model](#threat-model) for a toolbox that deliberately injects input.

Behind it: a rig of seven desktop images with monitors that can be plugged,
resized and unplugged from outside the guest, and 1884 tests. The tools were
stressed deliberately on each desktop, and what that found is in the history —
roughly fifty defects, including a few that mattered: typing captured by
another user, commands that reported success while failing, and a monitor
placed ten pixels wrong on the wlroots backend at most fractional scales.

Known limitations are listed per tool and in the support matrix; the parity
test against the real xdotool needs that binary and an X display, so it skips
outside the nix development shell.

## How

There is no X server to lie to, so wdotool goes underneath instead:

- **Input** is injected as kernel-level virtual devices via `/dev/uinput` — a
  keyboard, a relative mouse, and an absolute tablet (the same shape QEMU uses, which
  every compositor maps across the whole output layout). The compositor can't tell it
  from real hardware, so this works on GNOME, KDE, sway, anything. That's also why it
  needs root — on wlroots, every injecting command skips that entirely through
  `zwp_virtual_keyboard_v1` and `zwlr_virtual_pointer_v1` (see [Typing and
  clicking with no privilege at
  all](#typing-and-clicking-with-no-privilege-at-all---vkbd)) — or,
  if you'd rather not: one udev rule (`sudo sh
  gnome/install-bridge.sh --udev` installs `gnome/60-fuckwayland-uinput.rules`,
  which tags `/dev/uinput` for the logged-in user's ACL — no group, nobody
  else) and it runs as a plain user, no relogin needed. The rule lives under
  `gnome/` for want of a better home and has nothing to do with GNOME: it is
  the same rule on KDE, sway and Xfce, and `--udev` installs it there too
  without touching the bridge extension. Media keys work too
  (`key XF86AudioMute` and friends map straight to their evdev codes).
- The first invocation forks a small daemon that owns the devices (creating them
  costs ~600ms of hotplug; you pay it once) and tracks the injected pointer.
- **Window management** talks to the compositor: sway/i3 IPC (complete), GNOME
  Shell through the bundled bridge extension (complete, see [GNOME](#gnome)), KDE
  Plasma through KWin scripting (complete, and nothing to install — KWin lets any
  session-bus client load a script), and the wlr-foreign-toplevel protocol as the
  generic fallback. Window ids are real, stable, decimal — like X window ids,
  scripts pipe them around unchanged.
- Runs fine under `sudo`: the graphical session's sockets are found by scanning
  `/run/user/*`.

## Install

Three routes:

1. **[pip, from a clone](#from-a-clone-with-pip)** — the normal one: one apt
   package, one venv, one pip line.
2. **[the single-file builds](#without-installing-the-single-file-builds)** — no
   install at all, five self-contained executables.
3. **[nix](#nix)** — `nix build`, if that is your world.

Whichever you pick, your desktop wants a piece of its own — a GNOME Shell
extension on [GNOME](#gnome), the real X11 tools on an [X11](#x11) session,
nothing at all on [KDE Plasma](#kde-plasma), the GTK bindings on a minimal
[sway](#sway-and-other-wlroots-compositors) — and injecting input needs
[access to `/dev/uinput`](#input-access). Then
[check it worked](#check-it-worked).

### From a clone, with pip

```sh
sudo apt install git python3-venv
git clone https://github.com/antoniobianchi333/fuckwayland.git
cd fuckwayland
python3 -m venv --system-site-packages ~/.venvs/fuckwayland
~/.venvs/fuckwayland/bin/pip install -e .
```

That is the whole install: `wdotool`, `wwmctl`, `wxprop`, `wxrandr` and
`warandr` in `~/.venvs/fuckwayland/bin`. Put them on `PATH`:

```sh
for t in wdotool wwmctl wxprop wxrandr warandr; do
    sudo ln -sfn ~/.venvs/fuckwayland/bin/$t /usr/local/bin/$t
done
```

Four things in those lines are not obvious:

* **Why a virtual environment.** Ubuntu 24.04 and newer mark the system Python
  as externally managed, so a plain `pip install -e .` — with or without
  `--user` — refuses with `error: externally-managed-environment` and points at
  a venv, at pipx, or at `--break-system-packages`. Those are the honest
  options; [all of them work](#other-ways-to-install), and a venv is the one
  that changes nothing outside its own directory.
* **Why `python3-venv` and not `python3-pip`.** A stock Ubuntu desktop has
  neither pip nor venv nor pipx, so `pip install` is `pip: not found` before
  PEP 668 gets a word in. `python3-venv` is all you need — the venv brings its
  own pip along. (Run bare, `python3 -m venv` names the *versioned* package in
  its error, `python3.12-venv` on 24.04 and `python3.14-venv` on 26.04;
  `python3-venv` pulls the right one on both.)
* **Why `--system-site-packages`.** `warandr` is the one tool with a
  dependency: the Python GTK 3 bindings, which are the apt packages
  `python3-gi` and `gir1.2-gtk-3.0` rather than something pip should build.
  Every GNOME, KDE and Xfce install already has them — but a venv hides system
  packages unless it is told not to, and then `warandr` exits 1 with
  `warandr: GTK 3 for Python is not available (No module named 'gi') - on
  Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0`. The other four
  tools are stdlib-only and never notice. The same line, with
  `(Namespace Gtk not available)` in the parentheses, is what a machine that
  has `python3-gi` but not the GTK 3 typelib says — see
  [sway](#sway-and-other-wlroots-compositors).
* **Why `-e`, and keep the clone.** pip installs the five commands and nothing
  else — not `gnome/install-bridge.sh`, not the udev rule, not
  `warandr.desktop`. Those are used from the clone, so keep it where it is and
  let the editable install point at it.

`pip install -e .` fetches setuptools, so it wants network. On a desktop image,
which ships `python3-setuptools`, a `--system-site-packages` venv can use that
copy instead: `~/.venvs/fuckwayland/bin/pip install --no-build-isolation -e .`

Optional, for the GUI: an application-menu entry for `warandr`. Its `Exec=` is
the bare name, so this wants `warandr` on the session's `PATH` — which the
`/usr/local/bin` symlink above gives it.

```sh
mkdir -p ~/.local/share/applications
cp warandr.desktop ~/.local/share/applications/
```

To undo all of it:

```sh
sudo rm -f /usr/local/bin/wdotool /usr/local/bin/wwmctl /usr/local/bin/wxprop \
           /usr/local/bin/wxrandr /usr/local/bin/warandr
rm -rf ~/.venvs/fuckwayland ~/.local/share/applications/warandr.desktop
```

### Other ways to install

All of these were run on a stock desktop and all of them work; pick by what
you want, not by what is possible.

* **pipx** — `sudo apt install pipx`, then `pipx install
  --system-site-packages -e .` and `pipx ensurepath` once. Prefer it if pipx is
  already how you keep your tools. `--system-site-packages` is not optional
  here either: without it `warandr` fails exactly as above. Lands in
  `~/.local/bin`; undo with `pipx uninstall fuckwayland`.
* **The user site, overriding the rule** — `sudo apt install python3-pip`, then
  `pip install --user --break-system-packages -e .`. Prefer it when you want no
  venv at all and you accept the risk that flag names. Also `~/.local/bin`,
  which pip warns is not on `PATH`: a *login* shell adds it from `~/.profile`,
  but only if the directory existed at login, so log out and back in once.
  Undo with `pip uninstall --break-system-packages fuckwayland`.
* **One venv for the whole machine** — `sudo python3 -m venv
  --system-site-packages /opt/fuckwayland`, then `sudo /opt/fuckwayland/bin/pip
  install /path/to/the/clone`, and symlink out of `/opt/fuckwayland/bin`.
  Prefer it when other accounts (or `sudo` as another user) must run the tools:
  Ubuntu home directories are `0750`, so a venv under your `$HOME` is
  unreadable to them. Note the missing `-e`: an editable install keeps reading
  the source tree at run time and hits the same wall, from a readable copy it
  does not.
* **No install at all** — [the single-file
  builds](#without-installing-the-single-file-builds), below.

### X11

**What to install:** the real tools, if they are not already there —
`sudo apt install xdotool wmctrl`. (`xprop` and `xrandr` come with every X11
desktop, in `x11-utils` and `x11-xserver-utils`.) Nothing else: no extension,
no udev rule, no `/dev/uinput`. Without them you get exit **127** and a line
naming the package to install.

The tools are meant to be installed **over** the originals, so they also have
to behave when the session is a plain X11 one (Xfce, i3, GNOME-on-Xorg,
KDE-on-Xorg — the last two are measured, see
[Desktop support](#desktop-support) note **(j)**): there they detect the session
and hand over to the real
`xdotool`/`wmctrl`/`xprop`/`xrandr` with `execve`, argv untouched — same exit
status, same signals, same stdio, no extra process. One script then runs on
both session types, and `xdotool --version` on X11 answers with the version
that is actually installed there.

Four things stay ours on either session type, because the original has no
such thing to hand over to: `wdotool keys` (and the hidden `__keymap`), and
the leading `--layout` / `--vkbd` options, which are stripped before the
handover. So on X11 `xdotool keys explain a` answers from our own tables
where the real xdotool says `Unknown command: keys`, and `xdotool --layout
us key a` hands `key a` over where the real one refuses the option. They
never fail on an X11 session, they just say what they could not read
(`note: the compositor's keymap could not be read (no wayland socket
found)`).

```console
$ FUCKWAYLAND_PASSTHROUGH=never xdotool key a   # our own code, whatever the session
$ FUCKWAYLAND_PASSTHROUGH=always ...            # hand over, whatever the session
$ WDOTOOL_REAL_XDOTOOL=/opt/bin/xdotool ...     # where the original is
```

`WDOTOOL_PASSTHROUGH`, `WWMCTL_PASSTHROUGH`, `WXPROP_PASSTHROUGH` and
`WXRANDR_PASSTHROUGH` do the same per tool (`warandr` ignores all of them: it
never hands over, it only picks between the `xrandr` and `wxrandr` command
words, and it keeps doing that by session — it does take the `DISPLAY`/
`XAUTHORITY` repair below for the `xrandr` it runs); the `*_REAL_*` variables are
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
`sudo /usr/bin/xdotool key a` says `Can't open display`. The cookie is looked
for in `$XAUTHORITY`, in the session's own leader (`/proc/<pid>/environ` of
gnome-shell, `startplasma-x11`, `kwin_x11`, `plasmashell`, `xfce4-session`,
`sway` — uid-qualified), in the runtime directory, and in `~/.Xauthority`;
the leader is the one that finds SDDM 0.20's, which lives in
`/tmp/xauth_<random>` and is in none of the other three places. `warandr` gets the
same repair for the `xrandr` it runs, so `--command` and `--save` answer from
a root shell too — but a *saved* layout script calls the bare command word,
exactly as arandr's does, so running the script itself still wants a session
(on Wayland that word is `wxrandr`, which finds one for itself).

### GNOME

Stock GNOME Wayland sessions — Ubuntu 24.04 (GNOME 46) and 26.04 (GNOME 50) as
installed — are supported, with one extra step: GNOME has no window-management
protocol, so the window side needs a small GNOME Shell extension that exports
Mutter over the session bus. Input injection needs nothing extra beyond
[`/dev/uinput` access](#input-access).

Run the installer from the clone — pip does not install it — and expect one
session restart: the first install exits 1 asking you to log out and back in,
because gnome-shell only scans extension directories at login. `wxrandr` and
`warandr` never need it (monitors go through Mutter's own DisplayConfig), so
until you have logged back in those two work and the window commands say
`gnome backend: the fuckwayland bridge extension is not running in GNOME
Shell; run gnome/install-bridge.sh and restart the session (log out and back
in)`.

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
  close and kill your windows through the bridge, and the user at the active
  seat can type as you through `/dev/uinput`. That is exactly what every X11
  client could always do, and it is the point of these tools; but it is a
  deliberate widening of GNOME's default. The bridge never evaluates code and
  never injects input; Flatpak/Snap apps without session-bus access cannot
  reach it. Do not install either piece on a machine where that trade is
  wrong — see [Threat model](#threat-model) for the full list.

### KDE Plasma

Stock Plasma Wayland sessions — Plasma 5.27 (Ubuntu 24.04) and Plasma 6.6
(26.04) — are supported with **nothing to install**: `org.kde.kwin.Scripting.
loadScript()` is plain `Q_SCRIPTABLE` with no polkit action and no bus policy
on both, so `wdotool` pushes one small JavaScript file into KWin per command
and unloads it again. `wwmctl`, `wxprop` and `wxrandr` come along with it.
(That is also a security note in the GNOME sense: any client on your session
bus can already do this, with or without these tools.)

**Plasma 6.7 changed how KWin publishes displays, and `wxrandr` follows it.**
From 6.7.0 an output is no longer a `kde_output_device_v2` wl_registry global;
the compositor hands the device objects out through a
`kde_output_device_registry_v2` object instead (kwin `7e32e00c`, never
backported — 6.6 still publishes the globals). On a real Plasma 6.7.4 session
the old global is simply **absent**, so that second path is the only way to
see an output at all, and `wxrandr` takes it: query, mode, position, rotation,
scale, `--off`, `--primary`, `--same-as` and hot-plug all measured there, on
the `stonking-kde` VM image (Ubuntu 26.10), against `kscreen-doctor` — see
[WXRANDR.md](WXRANDR.md#kwin-backend-wxrandrkwinpy).

That is the *only* thing measured on 6.7 so far. `wdotool`, `wwmctl` and
`wxprop` reach KWin through its scripting interface, which this change does
not touch, but they have not been run on 6.7 here and the support matrix below
is still 5.27 and 6.6. The image exists (`vm/vmctl build stonking-kde`) if you
want to close that gap.

What differs from X, and why:

| | |
|---|---|
| `windowraise` on 5.27 | KWin 5.27 has no per-window raise; the window is activated instead (which focuses it), and says so on stderr. Plasma 6 raises properly |
| `windowlower` | neither release has a per-window lower: the active window is lowered for real, any other is marked keep-below, with a warning |
| `windowstate SHADED` | works on 5.27, **for X11 windows only** — KWin shades nothing else, and says so; Plasma 6 removed window shading, so it is a clean "not supported" there |
| `windowstate MAXIMIZED_*` on 5.27 | KWin 5.27 exposes no `maximizeMode` to scripts, so a window is read as maximized when its frame fills the maximize area to within one size increment (at least 32px per axis). A merely large window therefore reads as maximized, and `--remove MAXIMIZED_*` cannot clear that reading |
| `set_num_desktops` | KWin caps virtual desktops (20 on 5.27, 25 on 6) and keeps at least one; asking for more is capped at that with a warning, not an error |
| window ids | KWin's only window handle is a UUID, so the printed ids are minted from it (30 bits of it: `0x40000000`–`0x7FFFFFFF`, out of the range Xwayland gives its clients). They are stable while the window lives, and an X id is not accepted in their place — on sway either, where the ids are sway node ids |
| `wwmctl -l -G` positions | on Plasma 6.6 (and sway) `wmctrl` doubles the frame offset under a non-reparenting WM and ours are the real ones; KWin 5.27's xwm *does* reparent, so both agree there |
| a state KWin ignores | KWin accepts a state a window rule or the client's size hints forbid and does nothing with it. `wwmctl` then sends the EWMH `_NET_WM_STATE` ClientMessage instead, which reaches an XWayland window through KWin's X-plane window manager, and checks that one landed too; `wdotool` has no second route and says what happened |
| `selectwindow` | KWin has one reply slot for its window picker, so a second picker started while the first is up takes the click. The first call then waits until `WDOTOOL_SELECT_TIMEOUT` (2 minutes) and says so |
| XWayland ids on Plasma 6 | `x11window.h` lost every scriptable property in 6, so `View.xid` is matched through the X server's own client list: pid and `WM_CLASS` filter, title and geometry score. Where those tie -- two windows of one application in the same place under the same title, two maximized editor windows -- the order of the two lists decides, and that is exact rather than a guess: `_NET_CLIENT_LIST` is KWin's own window list with everything but the managed X11 windows dropped. A client that publishes neither `_NET_WM_PID` nor `WM_CLASS`, and a pair that nothing at all separates, keep id 0 rather than being handed one of two ids |
| `wxprop -root` | `_NET_CLIENT_LIST`, `_NET_ACTIVE_WINDOW` and `_NET_DESKTOP_NAMES` are ours (native windows included), not KWin's stale X copies |
| `getmouselocation` | answered by KWin (`workspace.cursorPos`), like GNOME's: a mouse moved by hand, or by another process, reads correctly, and the query needs no `/dev/uinput` at all |

A window state that a client applies asynchronously (fullscreen and maximize
on a Wayland client, applied when it acks the configure) is waited for before
the command returns, so `windowstate` never reports a state it merely has not
seen land yet, and the next command sees a settled window.

**Plasma on X11** (Kubuntu's X11 session, and 26.04's after `apt install
plasma-session-x11`) is none of the above: it is a plain [X11](#x11) session, so
all four command-line tools hand over to the real `xdotool`/`wmctrl`/`xprop`/
`xrandr` and `warandr` drives the real `xrandr`. `kwin_x11` owning `org.kde.KWin`
does not change that — the session decides, not the session bus — and there is no
compositor socket for the KDE display backend to talk to, so `wxrandr` goes to the
X server (`--print-backend` says `x11`). Everything in the table above therefore
applies to the Wayland session only; on Xorg you get X's own answers, including
`xdotool`'s real X window ids and whatever `xdotool` version is installed. The
KWin backend is still reachable on purpose with `FUCKWAYLAND_PASSTHROUGH=never`,
and there it does work — but it is a downgrade on that session (no
`getdisplaygeometry`, minted ids instead of X ids), which is why it is not the
default.

### sway and other wlroots compositors

Stock sway (1.11 on Ubuntu 26.04) is supported with **nothing to install** for
the four command-line tools: they speak sway's own IPC, and `wxrandr
--print-backend` answers `sway`. Input needs
[`/dev/uinput`](#input-access), exactly as on any other Wayland session.

The GUI is the exception, and it is the one place a sway install differs from
a GNOME/KDE/Xfce one: a minimal sway install has `python3-gi` but **not** the GTK 3
typelib, so `warandr` exits 1 with

```
warandr: GTK 3 for Python is not available (Namespace Gtk not available) - on Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0
```

`sudo apt install python3-gi gir1.2-gtk-3.0` is the whole fix — the four
command-line tools never notice either way, and with a
`--system-site-packages` venv the GUI picks the bindings up with no further
step. Two smaller differences, both cosmetic: a minimal image has no `acl`
package either, so `install-bridge.sh --udev --check` says
`uinput ACL users: yes, not listed here (apt install acl)` where a desktop
with `getfacl` lists the user by name (the answer on the line below it,
`uinput usable by <you>`, is the same either way); and `windowmove` on a
*tiled* window warns and does not move it — float it first
(`swaymsg floating enable`). See the [support matrix](#desktop-support) for
the rest.

### Input access

Injecting input goes through the kernel's `/dev/uinput`, which is
`root:root 0600` on a stock Ubuntu — so `wdotool`'s **input** commands (`key`,
`type`, `click`, `mousemove`, `mousedown`/`mouseup`, `behave`, and any chain
containing one) need either root or the rule below. On **sway and the wlroots
family** they need neither, because the compositor offers both halves as
unprivileged Wayland protocols and wdotool uses them exactly where the kernel
device is closed — see [Typing and clicking with no privilege at
all](#typing-and-clicking-with-no-privilege-at-all---vkbd). Everywhere else,
without the rule, they stop with

```
cannot create uinput devices: [Errno 13] Permission denied: '/dev/uinput'
(wdotool injects input via /dev/uinput; run it as root)
```

Everything else needs nothing: the window commands (`search`,
`windowactivate`, `windowmove`, `windowstate`, `getactivewindow`,
`selectwindow`, the desktop ones), all of `wwmctl`, `wxprop`, `wxrandr` and
`warandr` reach the compositor over your own session bus and run as you.

Two ways to get it, then:

* **Run as root.** `sudo wdotool key a` works with no rule installed at all —
  the session's sockets are found by scanning `/run/user/*`.
* **Install the udev rule this repo ships**, once, from the clone:

  ```sh
  sudo sh gnome/install-bridge.sh --udev            # install it
  sudo sh gnome/install-bridge.sh --udev --check    # what is the node now?
  sudo sh gnome/install-bridge.sh --udev --uninstall # put it back
  ```

  It tags the node `uaccess`, so systemd-logind gives the user of the *active
  seat* an ACL on it: applied immediately (no relogin needed) and again at
  every login. The node itself stays `root:root 0600` — no `input` group is
  involved — and `--uninstall` restores exactly that. Despite living under
  `gnome/`, none of this is GNOME's business: the same command installs the
  same rule on a Plasma session, and the tools there use it the same way.

  Read the security note at the end of the [GNOME](#gnome) section first:
  anyone who can open `/dev/uinput` can type as you.

One gotcha while you are experimenting: `wdotool` keeps the virtual devices
alive in a small `__daemon` process, and one started while access existed keeps
injecting after the rule is removed. Log out, or kill it, to see the change.

### Installing over the originals

These are drop-in clones, so the last step is usually to put them where your
scripts already look. `/usr/local/bin` comes before `/usr/bin` on Ubuntu's
default `PATH`, so symlinking there wins without touching a single file the
package manager owns — and the originals stay exactly where they are, which is
what makes the [X11](#x11) handover work at all.

From a venv install:

```sh
sudo ln -sfn ~/.venvs/fuckwayland/bin/wdotool /usr/local/bin/xdotool
sudo ln -sfn ~/.venvs/fuckwayland/bin/wwmctl  /usr/local/bin/wmctrl
sudo ln -sfn ~/.venvs/fuckwayland/bin/wxprop  /usr/local/bin/xprop
sudo ln -sfn ~/.venvs/fuckwayland/bin/wxrandr /usr/local/bin/xrandr
```

From pipx or a `--user` install the source is `~/.local/bin/wdotool` instead;
from a single-file build, copy rather than link:
`sudo install -m 755 dist/wdotool /usr/local/bin/xdotool`. A clone that finds
itself under an original's name recognises itself and skips to the real binary,
so none of these loop.

Undo is `sudo rm` of the four names — nothing else was touched:

```sh
sudo rm -f /usr/local/bin/xdotool /usr/local/bin/wmctrl \
           /usr/local/bin/xprop /usr/local/bin/xrandr
```

Two cautions. A venv under `$HOME` is only readable by you and root (Ubuntu
homes are `0750`), so symlinks into `/usr/local/bin` that point into it break
for *other* users, and the message they get does not say why: `sudo` reports
`unable to execute /usr/local/bin/wdotool: Permission denied` on 24.04 and
`sudo: '/usr/local/bin/wdotool': command not found` on 26.04. For a
machine-wide drop-in use the [`/opt` venv](#other-ways-to-install). And a
symlink left behind after the venv is deleted just says `No such file or
directory` — remove the links when you remove the install.

### Without installing: the single-file builds

```sh
sh scripts/build-pyz.sh
```

builds `dist/wdotool`, `dist/wwmctl`, `dist/wxprop`, `dist/wxrandr` and
`dist/warandr`: five self-contained executables, 0.5–0.9 MB each, needing
nothing but the `python3` that is already on the machine — no pip, no venv, no
apt, not even a package to add. (`warandr` still imports the system GTK
bindings at run time, so the GUI wants `python3-gi` and `gir1.2-gtk-3.0` like
everywhere else; the other four are stdlib-only.)

Prefer this when you would rather not touch apt at all, when you want no venv,
when you are on a machine you do not administer, or when you want one file to
copy to another box. They run from `dist/`, from `~/bin`, or from
`/usr/local/bin` under an original's name — see
[above](#installing-over-the-originals). What you give up is `pip uninstall`
and any notion of an upgrade: you rebuild and copy again.

### Nix

`nix build` → `result/bin/` with all five tools, plus `xdotool`, `wmctrl`,
`xprop`, `xrandr` and `arandr` symlinks next to them. The flake wraps the GTK
typelibs into `warandr`, so the GUI works without a system PyGObject.

### Check it worked

The five answer everywhere, whatever install path you took (from a single-file
build, prefix them with `dist/`). Note that `wxprop` takes xprop's single-dash
`-version`:

```console
$ wdotool --version
xdotool version 4.20260303.1
$ wwmctl --version
1.07
$ wxprop -version
xprop 1.2.8
$ wxrandr --version
xrandr program version       1.5.4
Server reports RandR version 1.6
$ warandr --version
warandr 0.1.0
```

On a **Wayland** session (GNOME, KDE, sway) the version strings are ours, and
the next three commands are the real check — which backend was picked, whether
the compositor answers, and whether input lands:

```console
$ wxrandr --print-backend --verbose
mutter
session: wayland
chosen by: detection
compositor: Mutter
protocol: org.gnome.Mutter.DisplayConfig (D-Bus)
$ wwmctl -l
0x8d58a7dd  0 box Screen Layout Editor
$ wdotool key a          # types an 'a' into the focused window
```

The backend token is `mutter` on GNOME, `kwin` on Plasma and `sway` on
wlroots; the second line of `wxrandr --version` is whatever RandR version your
own session reports. `wwmctl -l` on GNOME is the one that needs the
[bridge extension](#gnome) — if it says so instead of listing windows, that is
the step still missing, and `wxrandr` above will have worked anyway.

On an **X11** session that block is not what you get, and that is the handover
working: `wdotool`, `wwmctl`, `wxprop` and `wxrandr` *are* the originals there,
so they print the versions installed on your machine rather than ours. On a
stock Ubuntu 26.04 Xfce, for instance, `wdotool --version` says `xdotool
version 3.20160805.1`, `wxprop -version` says `xprop 1.2.7` and `wxrandr
--version` says `1.5.3`, where ours — the same commands on the same box under
Wayland — say `4.20260303.1`, `1.2.8` and `1.5.4`. (`wwmctl --version` says
`1.07` either way: that is the wmctrl release we clone.) `warandr` is the
fifth and never hands over; it drives the real `xrandr`, and
`warandr --print-backend` says `x11`. If instead you get exit 127 and a line
about no real xdotool on `PATH`, install them:
`sudo apt install xdotool wmctrl`.

If something did not work, the first thing to try:

| what you saw | what to do |
|---|---|
| `wdotool: command not found` | `command -v wdotool` — the symlinks, or `~/.local/bin` not on `PATH` yet (log out and back in) |
| `gnome backend: the fuckwayland bridge extension is not running in GNOME Shell` | `sh gnome/install-bridge.sh`, then log out and back in; `sh gnome/install-bridge.sh --check` must say `loaded in shell: yes` and `org.fuckwayland.Bridge owned: yes` |
| `cannot create uinput devices: [Errno 13] Permission denied` | `sudo wdotool …`, or install the [udev rule](#input-access); `sudo sh gnome/install-bridge.sh --udev --check` should end `uinput usable by <your user>: yes (logind ACL)` |
| `warandr: GTK 3 for Python is not available` | `sudo apt install python3-gi gir1.2-gtk-3.0` — and the venv must have been made `--system-site-packages` |
| `xdotool: … no real xdotool was found on PATH`, exit 127 | you are on X11: `sudo apt install xdotool wmctrl` |
| the tool does something you did not expect on X11 | it *is* the original there; `FUCKWAYLAND_PASSTHROUGH=never` runs our own code instead |

## Desktop support

What each tool does on each desktop, measured rather than assumed: the branch is run
on eight golden VM images — GNOME 46 and 50, Plasma 5.27 and 6.6 on Wayland, Plasma
5.27 on **Xorg**, Xfce 4.18 and 4.20, sway 1.11 on wlroots — twice per image, once
**inside the session** and once as **root over ssh with an empty environment**,
against real windows on a two-head layout. `vm/README.md` keeps the rig and the
verbatim messages behind these cells.

The last column is a *session type*, not a desktop: what an X11 session gets is the
real tools, whichever desktop is drawing it.

| | GNOME 46 / 50 | Plasma 5.27 / 6.6 (Wayland) | X11 sessions — Xfce 4.18 / 4.20, Plasma on Xorg **(j)** | sway 1.11 (wlroots) |
|---|---|---|---|---|
| **wdotool** | all 48 commands; the window ones need the [bridge extension](#gnome) | all 48, nothing to install **(a)** | hands over to the installed `xdotool` **(b)** | all 48; four differences **(c)** |
| **wwmctl** | works; the window list needs the bridge | works **(d)** | hands over to `wmctrl` | works |
| **wxprop** | works, X and native windows | works | hands over to `xprop` | works; from a root shell `-root` is synthesized **(e)** |
| **wxrandr** | works (mutter) | works (kwin) **(f)** | hands over to `xrandr` **(g)** | works (sway) |
| **warandr** | works (mutter) | works (kwin) **(f)** | works, driving the real `xrandr` **(g)** | works (sway); the stock image has no GTK 3 bindings **(h)** |
| **`wdotool` without root** | pointer *and* keyboard need the udev rule (or root) | pointer *and* keyboard need the udev rule (or root) | nothing needs it (X11) | **nothing needs it**: keyboard and pointer both **(i)** |


All of it works **as the desktop user and as root** — `sudo`, `ssh root@box`, cron —
because the session's compositor socket, session bus, `DISPLAY` and X cookie are
found for you. **(e)** is the one exception, and it is not one we can fix.

**(a)** With the differences in the [KDE Plasma](#kde-plasma) table above (raise,
lower, shading, maximize on 5.27, minted window ids).

**(b)** On a plain X11 session the tools *are* the originals, so the command set is
whatever is installed there. Both Ubuntu images this branch tests on carry **xdotool
3.20160805.1**, which has no `windowstate` — `xdotool: Unknown command: windowstate`,
rc 1 — while parity is claimed against xdotool 4.20260303.1, whose full command set is
what our own Wayland code implements. `getdisplaygeometry` likewise answers per screen
(`1920 1080`) where the Wayland backends report the whole layout span (`3840 1080`):
that is xdotool's own behaviour, faithfully.

**(c)** Four, all about sway's tiling. `windowmove` and `windowsize` on a *tiled*
window warn and succeed without changing it (float it first: `swaymsg floating
enable`, and then the move and the resize land exactly — `resize set` on a tiled
container moves the split ratio, which is not the size you asked for);
`windowraise` on a tiled window likewise warns and does nothing, because a tiled
container has no stacking position to change (and `windowlower` warns on every
window: sway has no lower at all); and `windowstate
MAXIMIZED_VERT`/`_HORZ` has no equivalent in sway and fails cleanly.

**(d)** On Plasma 6.6 plasmashell's own desktop windows carry an empty caption, so
those `wwmctl -l` rows have a blank title where 5.27 prints `Desktop @ QRect(…)`.
KWin's caption is what we print; ids, pid, class and geometry are right on both.

**(e)** sway's Xwayland runs with no authority file, so **only the session user's own
processes can open it**: the real `xprop` from a root shell gets `Authorization
required, but no authorization protocol specified` there too. Rather than fail,
`wxprop -root` answers from sway's IPC — a synthesized `_NET_CLIENT_LIST` of
compositor ids and `_NET_SUPPORTING_WM_CHECK … 0x0`. Inside the session it is
Xwayland's real root window, and every other desktop gives root the real X root.

**(f)** KWin applies a layout immediately and permanently — no temporary mode, no
confirmation dialog — and says so on stderr, together with the line that puts the
previous layout back — where it keeps that layout, and how to clear it, is under
[Keeping a layout](#keeping-a-layout). `--same-as` is plainly the same position,
which on KWin
already shows identical pixels; it reaches for the compositor's own
`set_replication_source` only when the two outputs' logical rectangles differ and
a shared position would give a crop instead of a copy (and says which KWin
version that would need, when the running one is older). A replicated output has
no rectangle of its own on that desktop — KWin drops it out of the layout — so
`--query` reports it at its source's geometry, `--right-of` it starts where the
source ends, and it cannot be the primary. Mirroring *onto* one is resolved to
the output whose picture it is really showing: KWin accepts a copy of a copy and
then never draws it. Both cells are measured on Plasma 5.27 and 6.6, and the
display path again on **6.7.4**, where KWin publishes outputs through a
registry object instead of as globals and `wxrandr` switches discovery paths to
match — see [KDE Plasma](#kde-plasma). The other four cells in this column are
5.27 and 6.6 only.

**(g)** X11 answers are the X server's own (`Screen 0: minimum 320 x 200 … maximum
8192 x 8192`), and whether an output is marked `primary` is the desktop's business.

**(i)** sway/wlroots is the only one of the four that implements
`zwp_virtual_keyboard_v1` *and* `zwlr_virtual_pointer_v1`, so it is the only
one where every injecting command — `key`, `keydown`, `keyup`, `type`,
`click`, `mousedown`, `mouseup`, `mousemove`, `mousemove_relative` — runs
with no root, no group and no udev rule; see [Typing and clicking with no
privilege at all](#typing-and-clicking-with-no-privilege-at-all---vkbd).
Mutter and KWin (6.6 and 5.27, both measured) implement neither, so on GNOME
and KDE every injecting command still goes through `/dev/uinput`.

**(h)** `warandr` is the one tool with a dependency (`python3-gi`, `gir1.2-gtk-3.0`).
GNOME, KDE and Xfce installs have them; a minimal sway image may not, and warandr then
names the package and exits 1. The other four are stdlib-only.

**(j)** Plasma on Xorg is an X11 session like any other and is handled like one,
measured on both generations: Plasma 5.27 / KWin 5.27 (Kubuntu 24.04, the X11 session
it ships) and Plasma 6.6 / KWin 6.6 (26.04, after `apt install plasma-session-x11` —
`kubuntu-desktop` installs no X11 session there, though the packages are in the
archive). Every handover is the same on both, and so is every byte of the output.
Two things are worth naming because
they look like they might change the answer and do not. **KWin owns
`org.kde.KWin` on the session bus exactly as it does on Wayland** — but the
handover is decided before any backend is detected, and by the session, not by the
bus, so nothing of ours ever asks it: `session_kind()` is `x11` for all four tools,
a Plasma X11 session has no compositor socket at all (`find_wayland_socket()` →
`None`), `wxrandr --print-backend` says `x11` / `compositor: X server (RandR)`, and
`wxrandr --backends` marks `kwin` *unavailable — no wayland socket*. And the KWin
script backend **would** half-work there (`FUCKWAYLAND_PASSTHROUGH=never wwmctl -l`
does list KWin's windows), which is the argument for the handover rather than against
it: on the same session our own `getdisplaygeometry` fails (`no wayland socket found`,
rc 2) where the real `xdotool` answers, and the ids are only right by accident — 5.27
still hands `windowId` to scripts, so those rows carry the real X ids, while KWin 6.6
has dropped it and the same command prints ids minted from KWin uuids on a session
where every window has an X id.


## Compatibility

All 48 xdotool commands are implemented, with output byte-compatible against
xdotool 4.20260303.1 (including `--help` text, error strings, and several verbatim
C bugs, e.g. `windowmove`'s percent-y quirk). That is our own code, i.e. every
Wayland session; on an X11 session we hand over, so what you get there is the
command set of the `xdotool` that is installed — see **(b)** under
[Desktop support](#desktop-support).

Wayland forces a few honest approximations:

| | |
|---|---|
| `key`/`type` `--window` | activates the target first, then injects (no XSendEvent) |
| `getmouselocation` | asks the compositor where the pointer is (GNOME, KDE); on sway/wlroots nothing can be asked — the IPC has no pointer query and `zwlr_virtual_pointer_v1` has no events at all — so it reports the position wdotool itself put the pointer at, which is exact, and **refuses with that reason** rather than guessing when wdotool has not moved it |
| `--clearmodifiers` | clears and restores the modifiers **wdotool itself** holds (from `keydown`). One held on a physical keyboard cannot be cleared through uinput at all — the kernel drops a key-up from a device that does not hold the key — and pressing it back afterwards would leave it stuck, so it is left alone; wdotool names it if it may read `/dev/input/event*` (root), and is silent, with identical behaviour, if it may not. On the virtual-keyboard path (see below) there is no such gap: modifier state there is per device, so a modifier on a real keyboard does not reach our keystrokes in the first place. On a *pointer* command the modifier still rides the click whichever device sends it, because modifier state reaches the seat from the seat's keyboards, so that warning stays |
| `type` non-US chars | typed through the session's active layout (see below); characters it cannot produce warn and skip |
| `search --role` | roles don't exist on Wayland; matches against empty string |
| `windowraise`/`lower` | floating windows only (tiling has no z-order) |
| `set_window`, `windowreparent`, viewport/desktop-count setters | warn and succeed (cosmetic on Wayland; scripts keep running) |
| `behave`, `behave_screen_edge`, `windowmap --sync` waits on X events | unsupported, fail cleanly |
| `selectwindow` | click-to-select on GNOME (bridge grab, needs bridge v2) and KDE (KWin's picker); Escape cancels with rc 1, as does a second picker or a shell that is already modal (the GNOME overview, a menu). sway/i3 have no picker in their IPC and still wait for the next focus change |

Desktops map to workspaces (0-based). `windowunmap`/`windowminimize` use the
scratchpad on sway.

GNOME has a longer list of honest differences (shell grabs, the lock screen,
`selectwindow`): see **Known limitations on GNOME** in
[gnome/README.md](gnome/README.md).

### Keyboard layouts

`key` and `type` inject keycodes through a virtual keyboard, and the
compositor reads those keycodes through whatever XKB layout your session has
active — so a fixed US table would type `z` for `y` on a German layout and
skip every accented character. wdotool reads the compositor's *own* keymap
instead (every Wayland client is handed it on `wl_keyboard.keymap`) and looks
the character up backwards: which key, with which modifiers, produces it here.

```console
$ wdotool type 'Grüße, ça va?'      # de, fr, es, dvorak … all fine
```

* AltGr (level three) and level five are pressed when the layout needs them —
  `@` on German is AltGr+Q, and wdotool finds out *which key* is AltGr from
  the keymap, not from a guess.
* A character that needs a **dead key** becomes two keystrokes (`é` on German
  is `´` then `e`) and the application composes them, exactly as it does when
  you type it by hand. Which two keystrokes follows the Compose table every
  toolkit ships: an accent on its own is the dead key *twice* (`´` is
  dead_acute dead_acute), and dead key + space is what that table says it is
  (`'`, not `´`).
* Characters the active layout genuinely cannot produce warn and skip, one
  line each, and the rest of the string is typed — `ñ` is not on a French
  keyboard (`fr(basic)` has no `dead_tilde` and no `ntilde`), so
  `type 'ñ'` says so and types nothing.
* **When the active layout is plain US, none of this runs.** wdotool checks
  the keymap key by key against its built-in US table and, when they agree,
  uses the built-in table — the most common setup keeps the code path it
  always had, byte for byte. Keyboard *options* do not spoil that: swapping
  Caps and Escape, or putting the layout switcher on Super+Space, still
  bypasses, because what is compared is the keys the fixed table actually
  presses. The same fixed table is the fallback whenever the keymap cannot be
  read at all (no compositor, a locked screen, an unparsable keymap): a
  warning on every command that types through it, never a failure.

Two things are still on the honest list:

* **Compose-only characters.** A character the layout reaches only through a
  Compose *sequence* that is not a dead-key pair (`ø` on German, say) is
  skipped with the warning above. wdotool composes nothing itself — it
  presses keys, and the application does the composing.
* **Which of several configured layouts is active.** `wl_keyboard.modifiers`
  carries that and every compositor sends it only to the window with keyboard
  focus, which an injector never is. With one layout configured there is
  nothing to guess. With several, wdotool uses the **first** one and says so
  — including when the first one is `us`, where it is the built-in table that
  gets used. So a `us, de` session that has switched to German types US
  characters until you pin the group:

```console
$ WDOTOOL_XKB_GROUP=2 wdotool type 'Grüße'   # the second configured layout
```

### Forcing the layout

`--layout` says which character table the typing commands use, ahead of
everything else. It goes before the command, and it is ours, not xdotool's:

```console
$ wdotool --layout us type 'hello'    # the built-in US table, no questions asked
$ wdotool --layout xkb type 'Grüße'   # the compositor's keymap, even on US
$ wdotool --layout auto type 'hello'  # the default: decide per session
```

`--layout us` is the one that promises something. It does not read the
compositor's keymap and it does not run the "is this plain US?" check either,
so **no layout code executes at all** — nothing in the keymap reader or the
reverse map can affect that run. Use it when you know your keyboard is US and
you would rather the tool did not look, or to rule the layout machinery out
while diagnosing something else. `--layout fixed` is a synonym.

The option beats the variables below, which matters because those are read by
the daemon and a daemon may already be running with different ones.

| variable | effect |
|---|---|
| `WDOTOOL_LAYOUT=auto` | the default: the compositor's keymap, unless it is plain US |
| `WDOTOOL_LAYOUT=us` | never read the keymap; use the built-in US table |
| `WDOTOOL_LAYOUT=xkb` | use the compositor's keymap even if it looks like US |
| `WDOTOOL_XKB_GROUP=<n>` | pin the active layout group (1 = the first one) |
| `WDOTOOL_XKB_KEYMAP=<file>` | read the keymap from a file instead of the compositor |

These four are read by the *daemon*, which keeps the environment it was
started with, so set them before the first wdotool command of a session — or
stop the running daemon first (`pkill -f 'wdotool __daemon'`), which is what
a script that changes the pin mid-run has to do. Changing your **layout**
needs none of that: the keymap and the active group are re-read on every
single command, so a long-running daemon follows a layout switch by itself.

`wdotool __keymap` is a hidden diagnostic that prints what the compositor
actually sent; `--info` summarises it (groups, active group, whether the US
bypass takes it). For "what do I press for this character?" the documented
command is `wdotool keys explain`, below.

### Typing and clicking with no privilege at all (`--vkbd`)

There is a Wayland protocol built for exactly the problem above:
`zwp_virtual_keyboard_v1` lets a client upload **its own** keymap and send
keycodes against it, so no reverse lookup is needed — the keymap that reads
our keycodes is the one we just uploaded. wdotool uploads a captured plain-US
keymap, which is precisely the one its built-in character table was written
for.

And there is a second one for the other half. `zwp_virtual_keyboard_v1` has
four requests — keymap, key, modifiers, destroy — and not one of them is a
pointer, so wlroots ships `zwlr_virtual_pointer_v1` beside it: absolute and
relative motion, buttons and scroll, equally unprivileged. wdotool uses both,
under one policy and one flag.

**Who has them, and what that means for privileges.** Measured on all four
desktops this branch is tested on, as the session user and as root:

| session | `zwp_virtual_keyboard_v1` | `zwlr_virtual_pointer_v1` | what injects | typing needs | pointer (`click`, `mousemove`, `mousedown/up`) needs |
|---|---|---|---|---|---|
| **sway 1.11 / wlroots** | **yes**, v1, advertised to every client and restricted to none | **yes**, v2, likewise | `/dev/uinput` when it can be opened, the protocol when it cannot | **nothing** — no root, no group, no udev rule | **nothing** either |
| **GNOME 46 / 50 (Mutter)** | no | no | `/dev/uinput`, always | root, or the udev rule | root, or the udev rule |
| **Plasma 5.27 / 6.6 (KWin)** | no — the interface is in no Plasma library, and 5.27 does not advertise it either | no | `/dev/uinput`, always | root, or the udev rule | root, or the udev rule |
| X11 (Xfce, …) | not applicable — wdotool hands over to the real `xdotool` | not applicable | X | nothing | nothing |

So on sway **every injecting command** wdotool has — `key`, `keydown`,
`keyup`, `type`, `click`, `mousedown`, `mouseup`, `mousemove`,
`mousemove_relative` — needs **no privilege whatsoever**, where before it
needed root because `/dev/uinput` there is `crw------- root root` with no
`uaccess` ACL. `search`, the window commands, `getdisplaygeometry` and
`getactivewindow` never needed any. On GNOME and KDE nothing changes at all,
and the reverse map above stays exactly where it is for both.

The two halves are chosen **separately and by the same rule**, so a
compositor that implements one and not the other gets the protocol for that
one and the kernel device (and its error) for the other.

**When they are used.** Only when the matching kernel device cannot be opened
*and* the compositor implements that protocol. Where `/dev/uinput` works — as
root, or with the udev rule, or on GNOME and KDE — nothing changes at all: the
same kernel devices inject the same events they always did. Typing through the
protocol is not free where it exists (the compositor hands the focused
application *our* keymap ahead of our first key and the session's keymap back
afterwards, so each injection makes that application recompile its keymap
twice), so the protocols are used where they turn a hard failure into working
input, and not to replace something that already works.

```console
$ wdotool type 'hello'              # as a plain user on sway: works, no root
$ wdotool click 1                   # ... and so does this now
$ wdotool mousemove 2560 360        # ... exactly, on any head of any layout
$ wdotool --vkbd on click 1         # force the protocols (error if absent)
$ wdotool --vkbd off click 1        # force /dev/uinput, whatever is offered
```

`--vkbd` is one switch for one decision, and it covers both halves: `--vkbd
on type x` and `--vkbd on click 1` each ask for the protocol wdotool would
use for that command.

| | |
|---|---|
| `--vkbd auto` | the default: `/dev/uinput`, and a protocol only where there is no usable kernel device |
| `--vkbd on` | always the protocol; a compositor that does not implement it is an error, never a silent fallback |
| `--vkbd off` | always `/dev/uinput`, including its "run it as root" error |
| `WDOTOOL_VKBD=auto\|on\|off` | the same, for the daemon; the flag beats it |

**What it costs.** The keymap wdotool uploads is a plain US one, so the
characters this path can type are the characters a US keyboard has. On a
German session the kernel path reaches `ü` through the reverse map and this
one does not — it warns and skips, as it does for any character the active
layout cannot produce. Generating a keymap that holds exactly the characters
being typed would fix that and is not possible yet: a keymap wdotool
synthesises itself *compiles* (the compositor hands it back to the focused
client) and then delivers no key events at all — measured twice, unexplained.
Until that is understood the keymap is a captured one, uploaded byte for
byte. Where `/dev/uinput` is open, none of this applies: the kernel path and
its reverse map are still what runs.

On this path `--layout` has nothing to decide: the character table is the
built-in US one by construction, because the keymap being read is the one
wdotool uploaded. `--layout us` therefore describes what already happens, and
`--layout xkb` — "use the *session's* keymap" — says so in one line and is
ignored; use `--vkbd off` if you want the session's keymap and the kernel
device. `--clearmodifiers` is the other way round: it is *more* honest here
than on the kernel path, because the modifier state that applies to our keys
is the mask we send and nothing else. A modifier held on a real keyboard
provably does not reach these keystrokes, so there is nothing wdotool cannot
clear and nothing to warn about.

Keys held across commands still work (`keydown ctrl` then `type c` then
`keyup ctrl`): the daemon holds one connection and one virtual keyboard for
its life, which it has to — a compositor releases whatever a client was
holding the moment that client disconnects. If the compositor restarts, the
keyboard, the uploaded keymap and anything held go with it. The next command
reconnects and re-uploads and *works*; the hold is the part that cannot be
recovered, so that is the only part you are told about:

```console
$ wdotool keyup shift
wdotool: the compositor restarted; the keys wdotool was holding on its virtual keyboard were released with it
```

A hold cannot move between the two paths either — a key can only be released
by the device that pressed it. Forcing a different `--vkbd` while something is
held (`--vkbd on keydown shift` then `--vkbd off type A`) releases it on the
keyboard that has it, says so, and then types what you asked for:

```console
$ wdotool --vkbd off type A
wdotool: the keys wdotool was holding on the virtual keyboard (shift) were released: this command types through the kernel one, and only the device that pressed a key can release it
```

Modifier state is per device in both directions, which is worth knowing before
mixing them: a shift held on the kernel device does not reach keys sent
through the protocol, `--clearmodifiers` on one does not clear the other's,
and a CapsLock locked through the protocol applies to its own keys only.

#### The pointer half (`zwlr_virtual_pointer_v1`)

Same policy, same flag, same lifetime rules — and the coordinates come out
*better* than the kernel path's, not merely as good.

**Absolute moves land exactly.** `motion_absolute` takes a ratio over the
whole output layout in logical pixels, so wdotool sends the offset from the
layout origin with the layout's own size as the extent, and the compositor
puts the cursor precisely there. Measured on a three-head layout — one head at
a negative origin, one at scale 1.5 — 14 of 14 targets landed with **0.000**
error, including both corners of the bounding box. There is no axis
quantisation to correct for at all, which is the off-by-one the kernel tablet
path had to be fixed for, and no way to reintroduce it.

**Relative moves are exact too, for a structural reason.** A virtual pointer
is not a libinput device (sway lists it with an empty libinput configuration),
so `pointer_accel` and `accel_profile` cannot apply to it on any wlroots
compositor: 1, 10, 100, 500 and 1000 pixels each moved exactly that far. On
the same seat, a `/dev/uinput` mouse asked for 500 units of relative motion
moved the cursor 858 — which is the acceleration problem the kernel path
works around by warping, and it cannot come back here.

**Buttons and scroll are the same gestures.** All eight buttons wdotool
supports (`1 2 3 8 9 10 11 12`) arrive as the evdev codes the kernel device
sends. The four wheel "buttons" (`4 5 6 7`) become one notched detent each,
with Wayland's sign convention rather than evdev's — positive vertical is
scroll *down* — and carry `axis_value120`, so a client cannot tell them from
a real wheel.

**`getmouselocation` is the one thing this path cannot do.**
`zwlr_virtual_pointer_v1` sends input and receives nothing: it has no events
at all, and sway's IPC carries no cursor position either (Xwayland only knows
the pointer while it is over an X surface). So wdotool reports the position it
put the pointer at — exact, per the paragraph above, until somebody moves a
real mouse — and, when it has not moved the pointer at all, says so instead of
printing a guess:

```console
$ wdotool getmouselocation
wdotool does not know where the pointer is: it has not moved it, and zwlr_virtual_pointer_v1 cannot be asked -- the protocol delivers no events, and neither sway's IPC nor Xwayland carries the cursor position. Move the pointer once (mousemove) and this answers exactly where it was put; on GNOME and KDE the compositor answers it directly.
$ wdotool mousemove 2560 360 getmouselocation
x:2560 y:360 screen:0 window:...
```

It is an **absolute** move that makes it answerable. `mousemove_relative` is
a delta: applied to a position nobody knows it produces another position
nobody knows, so it moves the cursor and leaves the question refused rather
than answering a number it made up.

**The one place the answer can be wrong without anyone touching a mouse** is
a coordinate inside the layout's bounding box but on **no output** — the gap
above or below a head that does not span the full height, on a layout whose
heads are not flush. wdotool asks for it, the compositor clamps the cursor to
the nearest output, and the model still holds the coordinate that was asked
for. On a rig with heads at `-1920,-540` and `0,0`, `mousemove 1 -539` puts
the cursor at `-0.004,-539` and `getmouselocation` says `1,-539`. Both paths
do this and always have: the kernel tablet is mapped across the same bounding
box.

A held button behaves exactly like a held key. `mousedown 1`, `mousemove`,
`mouseup 1` is one drag through one connection and one pointer object; the
compositor releases a held button the instant its client disconnects, so the
daemon keeps both for its life. A button cannot move between the two paths any
more than a key can, and forcing `--vkbd` differently mid-drag releases it on
the pointer that has it and says so:

```console
$ wdotool --vkbd off mouseup 1
wdotool: the mouse buttons wdotool was holding on the virtual pointer (left) were released: this command injects through the kernel one, and only the device that pressed a button can release it
```

The release happens **even when the sink you named turns out not to exist**,
which on a stock wlroots box is the ordinary case: `--vkbd off` there is
asking for a `/dev/uinput` you cannot open. The command fails with that
error, as a forced mode must — but a button is never left down on a pointer
nothing is going to use again:

```console
$ wdotool --vkbd on mousedown 1
$ wdotool --vkbd off mouseup 1
wdotool: the mouse buttons wdotool was holding on the virtual pointer (left) were released: this command asked for the kernel one, which cannot be used, and a button cannot be left down on a pointer nothing is going to inject through
cannot create uinput devices: [Errno 13] Permission denied: '/dev/uinput' (wdotool injects input via /dev/uinput; run it as root)
$ echo $?
1
```

`--vkbd on keydown shift` followed by `--vkbd off keyup shift` says the same
thing about the key.

One last thing worth knowing: there is one cursor per seat and this *is* that
cursor. A physical mouse moves "our" pointer, and the position wdotool
reports goes stale the moment anyone touches it — the same caveat the kernel
path has always had.

### Which key was that? — `wdotool keys`

`wdotool keys` is the layout machinery pointed the other way: what to press
for a character, or what you just pressed.

```
wdotool keys explain 'ç'   what to press, without touching the keyboard
wdotool keys watch         one line per key event, as you type
```

`keys` is ours, not xdotool's. It is a standalone command rather than a link
in a command chain, and it is not in `help` — that output stays byte-for-byte
the real xdotool's.

#### `explain` — no privilege, no devices

The keymap arrives on `wl_keyboard.keymap`, which every Wayland client is
handed, so `explain` needs no root and opens nothing under `/dev/input`. It
follows the same layout rules as `type` (`WDOTOOL_LAYOUT`, `WDOTOOL_XKB_GROUP`,
the US bypass), so what it prints is what `type` sends.

```console
$ wdotool keys explain 'ç'
layout: German -- group 1 of 2 (assumed), from wayland
level keys: shift = key 42 <LFSH>   level3 = key 100 <RALT>   level5 = key 195 <LVL5>   (what wdotool presses)
'ç' -- 2 presses on German (a dead-key pair: two presses in order, not a chord)
    1. press key 13 <AE12> with level3 (key 100 <RALT>, ISO_Level3_Shift) -> dead_cedilla
    2. press key 46 <AB03> -> 'c'
    wdotool key 108+21 key 54                  (keycodes)
    wdotool type 'ç'                           (characters)
```

That is the awkward case in full: **a dead key that is itself on the third
level**. `ç` on a German keyboard is AltGr held down across the `´` key, both
let go of, and *then* `c` — two presses in order, not one chord, and which of
the two it is changes the events completely. An argument that is a keysym name
(`Return`, `EuroSign`) is taken as one; anything else is taken character by
character, and `--chars`/`--keysym` say which explicitly. A character this
layout cannot produce says so, names the layout, and makes the exit status 1.

#### `watch` — needs root

`watch` is the same question asked by pressing the key. It reads
`/dev/input/event*`, which is `root:input` with no ACL on every desktop
measured, and nothing tags it: the `uaccess` tag is one *this project's* udev
rule puts on `/dev/uinput` (the injecting half, above), and no rule anywhere
grants read on a keyboard. So watch mode needs root. Without it, it says which
case this machine is in — nodes it may not read, no nodes at all, or only
wdotool's own — and exits 1; it never grabs a device (`EVIOCGRAB`), so the
compositor keeps seeing every key and stopping leaves nothing behind.

```console
# wdotool keys watch
layout: German -- group 1 of 2 (assumed), from wayland
level keys: shift = key 42 <LFSH>   level3 = key 100 <RALT>   level5 = key 195 <LVL5>   (what wdotool presses)
watching 2 keyboards, ignoring 1 of our own; codes are evdev keycodes, the replay column uses X keycodes (evdev+8). Ctrl-C to stop.
    TIME EV     CODE KEY      MODIFIERS       PRODUCES         REPLAY (keycodes)          CHARACTER (portable)
   0.000 down   100 <RALT>   -               ISO_Level3_Shift wdotool keydown 108        wdotool keydown ISO_Level3_Shift
   0.090 down    13 <AE12>   level3          dead_cedilla     wdotool key 108+21         wdotool key dead_cedilla
   0.150 up      13 <AE12>   level3          dead_cedilla     wdotool keyup 21           wdotool keyup dead_acute
   0.210 up     100 <RALT>   -               ISO_Level3_Shift wdotool keyup 108          wdotool keyup ISO_Level3_Shift
= chord     | wdotool key 108+21 | wdotool key dead_cedilla
   0.440 down    46 <AB03>   -               'c'              wdotool key 54             wdotool type 'c'
   0.500 up      46 <AB03>   -               'c'              wdotool keyup 54           wdotool keyup c
= chord     | wdotool key 54 | wdotool type 'c'
= dead pair | wdotool key 108+21 key 54 | wdotool type 'ç' | two presses in order, not a chord
```

That is the same `ç`, typed rather than looked up — and the two presses are
plainly two presses, because the `´` key is released before `c` is touched.

* **Every event, press and release.** A chord holds a modifier down *across*
  another key press; a dead-key pair is two presses in turn. Written down as
  "AltGr, `´`, `c`" they look alike, so the ordering is printed rather than
  summarised away.
* **Which key actually carried level three.** `MODIFIERS` says `level3`; the
  `CODE`/`KEY` columns say the key it came from. On German that is `<RALT>`;
  on Neo it is `<CAPS>`, and on a board with a dedicated key it is that key.
  Nothing is assumed — the header line separately reports which key *wdotool*
  would press, which is not always the same one.
* **Both reproductions on every line.** `REPLAY` is keycodes, exact and
  meaningless under a different layout (and it is X keycodes, evdev + 8, which
  is what `wdotool key` takes — zero-padded when the number would also read as
  a keysym name, since `key 9` is the *digit* and `key 09` is Escape). `CHARACTER` is characters and keysym names,
  which travel. A *release* line names its key by what it produces with no
  modifier at all (`keyup dead_acute` for the key that gave `dead_cedilla`
  while AltGr was down), because that is the plain way to say "let go of
  that one key" and nothing else.
* **A `=` line closes each run of held keys.** `= chord` when the run really
  is one chord; `= sequence` with the literal down/up commands and the reason
  when it is not — two keys held at once, released out of order, or a modifier
  pressed after the key, all of which a chord would silently change. `= dead
  pair` when two runs composed into one character.
* **Our own devices are skipped**, by the `wdotool ` device-name prefix, so a
  recording session does not capture a concurrent `wdotool type`.
* **Several keyboards are one timeline.** Every keyboard on the seat is read
  at once and each round is merged by the kernel's timestamps, because the
  seat merges modifier state too: Shift held on the laptop's board really does
  shift the key struck on the external one, and it is printed as the one chord
  it is. A board unplugged while it still holds a key has that key released —
  the kernel does the same for everyone else — so the run closes and nothing
  afterwards is reported under a modifier nobody is holding.
* **A release with no press** (the Enter that started the command is still
  down when the device is opened) is printed as itself and labelled, not
  guessed at.
* **Buttons are not keys.** A combined keyboard+mouse sends both down one
  node; the table is keys, and `--raw` is everything.

Keyboards that appear or disappear while watching are picked up and dropped;
Ctrl-C exits 0. The table goes to **stdout** and everything else to stderr, so
`wdotool keys watch --count 4 > keys.log` is a usable recording.

| option | |
|---|---|
| `--count N` | stop after N key events — for scripting |
| `--raw` | unfiltered evdev event lines instead of the table (autorepeat, `EV_SYN`, every device) |
| `--group N` | read group N of the keymap instead of the active one |
| `--keymap FILE` | read the keymap from a file instead of the compositor |

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

Where the [pointer protocol](#the-pointer-half-zwlr_virtual_pointer_v1) is
used instead of the tablet, it is exact rather than merely pixel-exact: the
two paths were measured against each other on the same three-head rig, one
head at a negative origin and one at scale 1.5, and every target landed on
the same pixel — the protocol path with 0.000 error, the tablet path within
its own 1/32768-of-the-layout axis step.

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
formats, error strings, exit codes. (One deliberate exception: the machine
column is sized from the longest hostname, not — as wmctrl 1.07's `main.c`
does — from the last row's, which our stacking-ordered list would re-flow on
every raise. See [WWMCTL.md](WWMCTL.md).)

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

`-font` is real too: XWayland serves the core fonts, so `wxprop -font fixed` is
byte-identical to `xprop -font fixed`.

On GNOME (see [GNOME](#gnome)) native windows get their synthesized set from the
bridge — states, window types, `WM_CLASS` from the app id — and `-spy` follows the
shell's window events; `-root` is Mutter's real X root with `_NET_CLIENT_LIST`,
`_NET_ACTIVE_WINDOW` and the desktop properties re-synthesized so they cover native
windows too. Details in `WXPROP.md` § GNOME.

Two honest limits, both inherent: a native window has no window-type hint to report
(xdg-shell has none), so a GTK dialog prints `_NET_WM_WINDOW_TYPE_NORMAL` where its
XWayland twin prints `DIALOG`; and under `-len` truncation real xprop renders
*uninitialised heap* past the end of the fetched data, which nothing can reproduce —
we stop at the budget instead. `wwmctl`'s equivalents (`:SELECT:` waits for a focus
change rather than a click; `shaded`/`modal` are no-ops on both sides) are in
`WWMCTL.md`.

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
one-line failures in Mutter's name (`xrandr: GNOME's Mutter refused this layout:
...`) — nothing is refused here, so a "no" is always the compositor's — and since
Mutter, unlike X, allows neither gaps nor overlaps, an output
that changes size (`--rotate`, `--mode`, `--scale`, `-s`, `-o`) keeps its neighbours
touching it, with a warning. Changes are temporary like xrandr's and write nothing;
`--persistent` makes GNOME ask "Keep changes?", and only a confirmed dialog writes
`monitors.xml` — what each desktop then does with the layout is under [Keeping a
layout](#keeping-a-layout). It finds the session from a
custom keyboard shortcut, under `sudo`, or from `ssh root@` with no environment.

Which backend it is using is never a guess: `--print-backend` prints the token
(`--verbose` adds the session, why it was chosen, the compositor and the
protocol version), `--backends` lists them all with their availability here and
a reason where there is none, and `--backend NAME` forces one for that
invocation — beating `$WXRANDR_BACKEND`, which beats detection. `--backend x11`
means "hand over to the real xrandr", even on Wayland; a Wayland backend runs
our own code even on X11; an unavailable one is one line saying what was
missing, never a silent fallback.

```console
$ wxrandr --print-backend --verbose
mutter
session: wayland
chosen by: detection
compositor: Mutter
protocol: org.gnome.Mutter.DisplayConfig (D-Bus)
available: yes
$ wxrandr --backends
  sway    unavailable  no sway or i3 IPC socket ($SWAYSOCK)
  kwin    unavailable  the compositor does not advertise kde_output_management_v2
* mutter  available    org.gnome.Mutter.DisplayConfig on the session bus
  wlr     unavailable  the compositor does not advertise zwlr_output_manager_v1
  x11     available    /usr/bin/xrandr
```

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
compositor's HiDPI factor). **Overlapping outputs are allowed wherever the desktop
allows them** — measured: X11, KWin and sway/wlroots all take the geometry *and*
show the same pixels in the shared region (byte-identical crops on both heads), so
a partial overlap really is a partial mirror there; GNOME's Mutter refuses any
layout that is not edge-adjacent (`Logical monitors not adjacent`) and warandr
reports that refusal in Mutter's name, not its own. Mirror of two outputs whose
sizes do not match is where that stops being enough — a shared position then
crops rather than copies — and on KDE `--same-as` switches to KWin's own
output replication for exactly those, and only those (never onto an output that
is itself replicating, which KWin takes and leaves blank). The status bar says which of
the four you are getting at the moment of the drop, and the saved script keeps it
in its comment header; `WARANDR.md` and `WXRANDR.md` have the table, the evidence,
and why true region mirroring (a resident capture-and-paint helper, `wl-mirror` on
wlroots) is deliberately out of scope. The layout is kept anchored at 0,0, Apply
runs off the main loop and a failed Apply keeps your edits. It needs the GTK 3 bindings every stock Ubuntu
desktop already has (`python3-gi`, `gir1.2-gtk-3.0`) and nothing else — no cairo:
the canvas is plain widgets. `warandr.desktop` puts it in the Settings menu.
Contract: `WARANDR.md`.

Which backend it is talking to is in the window at all times — the status bar's
right-hand corner says `backend: mutter (Wayland)` or `backend: xrandr (X11)`,
with the full explanation in its tooltip — and **Layout ▸ Backend** changes it:
Automatic, X11 (xrandr), sway, wlroots, GNOME (mutter), KDE (kwin), the ones
this session cannot reach greyed out with the reason. Picking one re-reads the
screen through it and redraws; if it cannot be reached you get the dialog and
the previous one back, never an empty window. The same spellings work on the
command line, so a hotkey can pin one:

```console
$ warandr --print-backend            # mutter
$ warandr --print-backend --verbose  # ...and what runs, why, and what it found
$ warandr --backend x11              # the GUI, talking to the real xrandr
$ warandr --backend mutter --command
wxrandr --backend mutter --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal
```

On a stock GNOME desktop (verified on Ubuntu 24.04 and 26.04, Wayland session):
`scripts/build-pyz.sh`, copy `dist/warandr` and `dist/wxrandr` to `/usr/local/bin/`
— warandr itself carries wxrandr inside, but the layout scripts it saves call bare
`wxrandr`, exactly like arandr's call bare `xrandr`, so a script bound to a hotkey
needs it on `PATH` (Save As says so in the status bar when it is missing), and
`~/.screenlayout` has to exist before `--save` will write into it. Bind
`warandr` and `~/.screenlayout/desk.sh` to GNOME custom shortcuts on `<Super>F`-keys
(`<Ctrl><Alt>F1`–`F12` are Mutter's VT switches); the script restores a three-head
layout in about a second — though the first press after a reboot is lost while the
session is still coming up. Apply is temporary, like xrandr: Mutter writes nothing
and lays the monitors out afresh at the next login, while at a hotplug it puts the
layout back once the original monitors return. A saved script on a key is how a
layout comes back, here and on the other three desktops: the commands, and what
each desktop does with a layout of its own accord, are under [Keeping a
layout](#keeping-a-layout). A
monitor plugged in while the window is open shows up after New (Ctrl+N), as in
arandr; a layout with a gap between monitors gets Mutter's own "Logical monitors not
adjacent" in the error dialog and stays on the canvas to be fixed. One GNOME habit
to know: an Apply that turns a monitor off or on makes Mutter move the keyboard
focus off the window, so click it before the next Ctrl+S.

## Keeping a layout

Nothing here restores a layout on its own. There is no daemon, no service and no
autostart entry: `wxrandr` and `warandr` change the screen when you run them and
then exit, and nothing in the toolbox watches for a monitor being plugged in. (The
only resident process it ever leaves behind is `wdotool`'s input daemon, which owns
input devices and has nothing to do with outputs.) What becomes of a layout after
that is the desktop's business, and the four desktops do not agree.

Measured on GNOME 50 (Mutter), Plasma 6 (KWin), sway 1.11 (wlroots) and Xfce 4.20
on X11, on three heads with one of them rotated:

| | a head unplugged and plugged back in | reboot | where the desktop keeps a layout |
|---|---|---|---|
| **GNOME** (Mutter) | comes back in full: Mutter lays the *remaining* monitors out in a row while the set is short, and puts the layout back when the original set returns | lost, unless a `--persistent` apply was confirmed | `~/.config/monitors.xml`, written by GNOME Settings or a confirmed `--persistent` and by nothing else; a fresh install has none |
| **KDE Plasma** (KWin) | comes back in full | **kept** | `~/.config/kwinoutputconfig.json`, written by every apply KWin takes |
| **sway** (wlroots) | comes back in full, every output | lost | nothing on disk; only `~/.config/sway/config` makes a layout stick |
| **Xfce** (X11) | **lost**: the head comes back at the end of a plain row, unrotated, and `primary` is cleared | lost, `primary` with it | nothing; `displays.xml` is byte-identical after an apply |

Restarting the compositor is a third event, and it splits the same way: `swaymsg
reload` puts sway's outputs back in its own enumeration order, while `xfwm4
--replace` changes nothing, because on X11 the layout belongs to the X server and
not to the window manager. Xfce's forgetting at hotplug is `xfsettingsd`'s doing —
it is what re-enables the returning head, in a row, and it clears `primary` when it
starts.

**KDE saves whether you want it to or not.** KWin has no temporary mode: every apply
it takes lands in `~/.config/kwinoutputconfig.json` in the same second, the file is
there before you run anything, and `--persistent` is accepted but means nothing.
Every such apply also prints, once, the command that puts the previous layout back;
replay it with `wxrandr` (`WXRANDR.md`). To clear what KDE remembers, delete that
file with the session stopped — deleting it from inside the session achieves
nothing, because KWin writes it out again on the way out.

**On GNOME an apply is temporary, like xrandr's**, and writes nothing. `wxrandr
--persistent` applies the layout and lets gnome-shell ask *Keep these display
settings?* for 20 seconds: ignored, the layout reverts and nothing is written;
confirmed, `monitors.xml` appears at once and the layout then survives both a
hotplug and a reboot. The dialog and the switch are GNOME's alone: on KDE
`--persistent` is accepted and changes nothing, and on sway and X11 nothing is
written either way.

### A layout script on a key

The way to get a layout back is to save it as a script and put that script on a
key. It is arandr's habit, it reads the same on all four desktops, and it runs when
you press it rather than when something guesses you wanted it.

```console
$ mkdir -p ~/.screenlayout                   # --save fails if it is not there
$ warandr --save ~/.screenlayout/desk.sh     # or Save As, from the window
$ sh ~/.screenlayout/desk.sh                 # run it once before binding it
```

The saved script calls bare `wxrandr` (bare `xrandr` on an X11 session), so that
has to be on `PATH`: install `dist/wxrandr` as `/usr/local/bin/wxrandr`. An output
the script has to switch back on needs `--auto` in its line, not only a position.

Then bind it:

- **GNOME** — Settings ▸ Keyboard ▸ Custom Shortcuts, or from a terminal:

  ```console
  $ K=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/
  $ S=org.gnome.settings-daemon.plugins.media-keys
  $ gsettings set $S custom-keybindings "['$K']"
  $ gsettings set $S.custom-keybinding:$K name 'restore desk layout'
  $ gsettings set $S.custom-keybinding:$K command "$HOME/.screenlayout/desk.sh"
  $ gsettings set $S.custom-keybinding:$K binding '<Super>F8'
  ```

  Bind a chord Mutter does not own: `<Ctrl><Alt>F1`–`F12` are its VT switches.

- **sway** — one line in `~/.config/sway/config`, then a reload:

  ```console
  $ echo 'bindsym $mod+F8 exec ~/.screenlayout/desk.sh' >> ~/.config/sway/config
  $ swaymsg reload
  ```

  The reload itself drops the current layout, so press the key afterwards.

- **Xfce** — Settings ▸ Keyboard ▸ Application Shortcuts, or live, with no restart:

  ```console
  $ xfconf-query -c xfce4-keyboard-shortcuts -p "/commands/custom/<Super>F8" \
      -n -t string -s "$HOME/.screenlayout/desk.sh"
  ```

- **KDE Plasma** — System Settings ▸ Shortcuts, as a custom shortcut. This is the
  one desktop where the shortcut cannot be set up from the command line: an entry
  written into `kglobalshortcutsrc` does register, and running it over D-Bus does
  start the script, but the key never fires it. KDE is also the desktop that needs
  the script least, since KWin already has the layout after a reboot.

Test it by pressing the key and reading the screen back with `wxrandr --query`.
Two things to expect. The shortcut is dead until the session is up: the first press
after a reboot is silently lost on GNOME and on Xfce (about 24 s from `reboot` on
the test rig), while sway ran the script on the first press. And the script itself
is quick — 0.13–0.76 s for three heads across the four desktops, 0.7 s to 2 s
end to end from the key press.

## Threat model

These are power tools: they exist to give a script the reach an X11 client
always had. That reach is the product, so the honest thing is to say exactly
what it is, who gets it, and what is not defended against.

**What the tools do by design.** `wdotool` injects keystrokes and pointer
events as a kernel-level virtual device, which every application — your
terminal, your password prompt, the lock screen — receives as real hardware.
Both halves have a second route on wlroots, `zwp_virtual_keyboard_v1` for the
keys and `zwlr_virtual_pointer_v1` for the pointer, which are not kernel
devices at all and reach the same places: measured, an unprivileged client
typed the account password into `swaylock` through the first and, through the
second, moved the cursor and clicked with no root, no group membership and no
device rule. On that family of compositors **nothing wdotool injects needs a
privilege of any kind**.
`wwmctl`, `wxprop` and `wxrandr` read and change window and display state
through the compositor. Anything you can do at the keyboard, a script running
as you can do through these tools; that is the whole point, and it is not a
vulnerability.

**What installing the pieces grants, and to whom.**

* **The GNOME bridge extension** grants **every process that can reach your
  session bus** — including a sandboxed app allowed to talk to
  `org.gnome.Shell`, because the object answers there too — the ability to
  list every window with its title, class, pid, geometry and workspace (stock
  GNOME withholds that: `org.gnome.Shell.Introspect` is sender-allowlisted);
  to move, resize, restack, close and **SIGKILL** any window; to learn
  `DISPLAY` and the path of Mutter's Xwayland cookie; to take the shell's
  modal input grab for the length of one window pick; and to confirm a
  pending display-configuration change. There is no partial mode and no
  caller check. The bridge never evaluates code and never injects input.
* **The udev rule** grants `/dev/uinput` — i.e. the ability to type as you —
  to the user of the **active seat session**, through a logind ACL, and to
  nobody else: no group, no standing channel. The grant is checked at
  `open()`, so the daemon re-checks it before every injection and destroys
  its devices when the seat moves to another session; a user who switches
  away therefore stops being able to type into the session they left. That is
  about the *kernel* device, which is global to the machine — on wlroots the
  Wayland route still works while the seat is elsewhere, because it reaches
  only the compositor whose socket it connected to, which is your own.
* **`zwp_virtual_keyboard_v1` and `zwlr_virtual_pointer_v1` on wlroots grant
  nothing that was not already granted**, and that is the note. sway
  advertises both protocols to **every client of your Wayland socket** and
  restricts them to none: any of them could already upload a keymap and type
  as you, or move your cursor and click, with or without us. wdotool installs
  nothing to use them and asks nobody for permission — it is the compositor's
  grant, to everything that can open your compositor's socket, which is the
  same-uid boundary below. Two consequences worth spelling out: on sway,
  injecting input needs neither root nor the udev rule at all, and the
  lock-screen note below applies to these routes as much as to the kernel
  one. Mutter and KWin 6.6.6 implement neither, so nothing changes there.
* **KDE needs nothing installed**, which is itself the note: any client of a
  Plasma session bus can already load a script into KWin, with or without us.
* **Running as root** (`sudo wdotool`) is the alternative to the udev rule.
  Then the tools find the graphical session by scanning `/run/user/*` and
  logind, and talk to that user's compositor as root.

**What is deliberately not defended against.** Anyone who can already run
code as you: they can type through the daemon, read the same files and talk
to the same buses — a same-uid boundary is not one we can enforce, and we do
not pretend to. A hostile compositor (you are already inside it). The lock
screen: injected keystrokes reach it, because the kernel does not know they
are injected — do not install the udev rule on a machine where someone else
has physical access to the keyboard while you are away. Scripts you saved and
run later (`warandr`'s layout scripts are shell scripts; read one before
running it, as with any script). And nothing here is a sandbox: the tools do
not confine what a command they hand over to (`xdotool` on X11) then does.

**What is defended against**, and stays that way: another local user. The
daemon socket, its lock and the wxrandr state file are private to their
owner and validated before they are believed; the daemon refuses to talk to a
socket somebody else is listening on; a state file that is not ours is
ignored rather than obeyed; the real-tool search never looks in the current
directory; and a root run with no session never hands a planted X server
another user's cookie.

**If you want less exposure:** don't install the bridge extension or the
udev rule — run the tools under `sudo` when you need them, which grants
nothing standing to anybody.

## Fully vibed, fully awesome

Every line of this repo was written by AI (Claude): the design contracts, the code,
the torture rigs, the hostile fake X servers, the byte-parity oracles, the VM demo,
this README, and yes, the meme. Fully vibed. Also fully awesome: 1884 tests and
counting, live-compositor integration suites, byte-for-byte output parity against
the real tools (verbatim bugs included), and every "it works" claim proven inside a
real Ubuntu 26.04 VM before it shipped. Vibe-check the code yourself — it can take it.

## Testing

Developed against real desktops, not against a model of them. `vm/` is the rig:
`vmctl` builds and runs seven golden images — GNOME, KDE Plasma, Xfce and sway on
Ubuntu 24.04 and 26.04 — each with up to four virtual monitors that can be
plugged, resized and unplugged from outside the guest, and every head
screenshotted. `vm/selftest.sh <flavor>` is its own check; `vm/README.md`
documents the whole thing, including what the four tools do on each flavor.

The original sway rig (`mkvm.sh`, `run.sh`, `compositor.sh`) is still there and
still works. `tests/` holds the suite: unit tests, wire-level fake compositors
and X servers, live-compositor integration, hostile-input torture, and
byte-parity oracles against the real xdotool, wmctrl, xprop and xrandr.
