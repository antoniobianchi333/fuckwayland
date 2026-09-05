# fuckwayland

The X11 power tools, `xdotool`, `wmctrl`, `xprop` and `xrandr`, reborn as no-bullshit
drop-in clones that work on Wayland. Same commands, same flags, same output bytes,
same scripts, bugs faithfully included. Symlink them over the originals and your
muscle memory never finds out the compositor changed underneath it.

![reject modernity, embrace tradition](meme.svg)

In the box:

- **wdotool**, xdotool, all 48 commands, byte-parity
- **wwmctl**, wmctrl, for native Wayland *and* legacy X apps in one list
- **wxprop**, xprop, real X properties for XWayland windows and synthesized ones for native windows
- **wxrandr**, xrandr, with first-class multimonitor: reshape crazy layouts in one atomic call
- **warandr**, arandr, the drag-your-monitors GUI, on Wayland (via wxrandr) and X11 (via xrandr)
- **wmirror**, the one that clones nothing: mirror a *region*, or an odd-shaped output, on wlroots

## Install

On a default Ubuntu 24.04 or 26.04 desktop, one file and one command. Take
`fuckwayland_0.3.0_all.deb` from the
[releases page](https://github.com/antoniobianchi333/fuckwayland/releases), or build
it from a clone with `sh scripts/build-deb.sh`.

```sh
sudo apt install ./fuckwayland_0.3.0_all.deb
```

That is the six tools in `/usr/bin`, the GNOME Shell bridge extension where
`gnome-shell` looks for it, the udev rule that opens `/dev/uinput` to whoever is at
the seat, and the `warandr` menu entry. The real `xdotool`, `wmctrl`, `xprop` and
`xrandr` stay exactly as they were, so a script that calls both keeps working.

**On GNOME, log out and back in once.** That is the whole of the manual procedure.
`gnome-shell` reads extension directories only when a session starts, so until you do
that the window commands say the bridge is not running. The package enables the
extension for you inside that first session. Everything else works the moment apt
finishes: the display commands, the GUI, typing and clicking.

Then [check it worked](#check-it-worked).

Three routes if the package is not what you want:
[from a clone with pip](#from-a-clone-with-pip), which is the normal one for
development,
[the single-file builds](#without-installing-the-single-file-builds), which install
nothing at all, and [nix](#nix). All of them are under
[other ways to install](#other-ways-to-install), together with pipx, a user site
install and one venv for the whole machine.

### The .deb

**One** `Architecture: all` package for **both** Ubuntu 24.04 and 26.04. Every module
here is pure standard library, so it lands in the version independent
`/usr/lib/python3/dist-packages` and your own `python3` byte compiles it at install
time, 3.12 on 24.04 and 3.14 on 26.04, from the same file. In the box: the six tools
in `/usr/bin`, the [GNOME bridge extension](#gnome) system wide, the [udev
rule](#input-access) applied at once with no reboot, and the `warandr` menu entry.

It does **not** replace the real `xdotool`, `wmctrl`, `xprop` or `xrandr`. Not one
path it ships is owned by their packages, so the [X11 handover](#x11-sessions) keeps
finding them and the [symlinks over the originals](#installing-over-the-originals)
stay your choice.

To take it away:

```sh
sudo apt remove fuckwayland
```

The extension, the udev rule and the six commands go with the package, `/dev/uinput`
goes back to `root:root 0600`, and the session you are in keeps running. Add
`sudo apt purge fuckwayland` to drop the last of its bookkeeping.

Alongside a pip install of the same source, the two do not fight. The
`/usr/local/bin` symlinks the [pip route](#from-a-clone-with-pip) makes keep winning
for the six names, because `/usr/local/bin` comes first on the Ubuntu `PATH`, and the
package owns nothing under `/usr/local`. One thing to know if the clone is what you
work on: inside a `--system-site-packages` venv, an editable install loses to the
packaged modules, so `import wdotool` finds the packaged copy.
`debian/README.Debian` has the detail and the one line that gets you back to the
clone.

`scripts/build-deb.sh` installs its own build tools from the Ubuntu archive on first
run, nothing from a PPA:

```
dpkg-dev debhelper dh-python pybuild-plugin-pyproject python3-all python3-setuptools
```

Pass `--no-deps` to install them yourself instead. What goes where, and why the extension and the rule are handled the way they
are, is in `debian/README.Debian`.

### From a clone, with pip

```sh
sudo apt install git python3-venv
git clone https://github.com/antoniobianchi333/fuckwayland.git
cd fuckwayland
python3 -m venv --system-site-packages ~/.venvs/fuckwayland
~/.venvs/fuckwayland/bin/pip install -e .
```

That is the whole install: `wdotool`, `wwmctl`, `wxprop`, `wxrandr`, `warandr` and
`wmirror` in `~/.venvs/fuckwayland/bin`. Put them on `PATH`:

```sh
for t in wdotool wwmctl wxprop wxrandr warandr wmirror; do
    sudo ln -sfn ~/.venvs/fuckwayland/bin/$t /usr/local/bin/$t
done
```

Four things in those lines are not obvious:

* **Why a virtual environment.** Ubuntu 24.04 and newer mark the system Python as
  externally managed, so a plain `pip install -e .`, with or without `--user`,
  refuses with `error: externally-managed-environment` and points at a venv, at pipx,
  or at `--break-system-packages`. Those are the honest options,
  [all of them work](#other-ways-to-install), and a venv is the one that changes
  nothing outside its own directory.
* **Why `python3-venv` and not `python3-pip`.** A stock Ubuntu desktop has neither
  pip nor venv nor pipx, checked against both installers' own package sets, so
  `pip install` is `pip: not found` before PEP 668 gets a word in. `python3-venv` is
  all you need, because the venv brings its own pip along. (Ask for a directory
  without it installed and the error names the *versioned* package, `python3.12-venv`
  on 24.04 and `python3.14-venv` on 26.04. `python3-venv` pulls the right one on
  both. Run with no arguments at all it only prints argparse's `the following
  arguments are required: ENV_DIR`.)
* **Why `--system-site-packages`.** `warandr` is the one tool with a dependency: the
  Python GTK 3 bindings, which are the apt packages `python3-gi` and `gir1.2-gtk-3.0`
  rather than something pip should build. Every GNOME, KDE and Xfce install already
  has them, but a venv hides system packages unless it is told not to, and then
  `warandr` exits 1 with this:

  ```
  warandr: GTK 3 for Python is not available (No module named 'gi') - on Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0
  ```

  The other five tools are stdlib only and never notice. This is the only place the
  GTK dependency needs handling: everything else in this file assumes it is met.
* **Why `-e`, and keep the clone.** pip installs the six commands and nothing else.
  Not `gnome/install-bridge.sh`, not the udev rule, not `warandr.desktop`. Those are
  used from the clone, so keep it where it is and let the editable install point at
  it.

`pip install -e .` fetches setuptools, so it wants network. **On 26.04** a
`--system-site-packages` venv can use the system copy instead, because a default
install ships `python3-setuptools` 78:
`~/.venvs/fuckwayland/bin/pip install --no-build-isolation -e .` **On 24.04 that
shortcut does not work** and the ordinary line above is the one to use: a default
24.04 desktop has no `python3-setuptools` at all (`ModuleNotFoundError: No module
named 'setuptools'`), and where it is installed it is setuptools 68, which still
needs the separate `wheel` package (`error: invalid command 'bdist_wheel'`). Both
measured on 24.04 default and cloud images, see `vm/README.md`.

Optional, for the GUI: an application menu entry for `warandr`. Its `Exec=` is the
bare name, so this wants `warandr` on the session's `PATH`, which the
`/usr/local/bin` symlink above gives it.

```sh
mkdir -p ~/.local/share/applications
cp warandr.desktop ~/.local/share/applications/
```

To undo all of it:

```sh
sudo rm -f /usr/local/bin/wdotool /usr/local/bin/wwmctl /usr/local/bin/wxprop \
           /usr/local/bin/wxrandr /usr/local/bin/warandr /usr/local/bin/wmirror
rm -rf ~/.venvs/fuckwayland ~/.local/share/applications/warandr.desktop
```

### Check it worked

They all answer everywhere, whatever install path you took (from a single-file build,
prefix them with `dist/`). Note that `wxprop` takes xprop's single-dash `-version`:

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
warandr 0.3.0
$ wmirror --version
wmirror 0.3.0
```

On a **Wayland** session (GNOME, KDE, sway) the version strings are ours, and the
next three commands are the real check: which backend was picked, whether the
compositor answers, and whether input lands.

```console
$ wxrandr --print-backend --verbose
mutter
session: wayland
chosen by: detection
compositor: Mutter
protocol: org.gnome.Mutter.DisplayConfig (D-Bus)
available: yes
$ wwmctl -l
0x8d58a7dd  0 box Screen Layout Editor
$ wdotool key a          # types an 'a' into the focused window
```

The backend token is `mutter` on GNOME, `kwin` on Plasma and `sway` on wlroots. The
second line of `wxrandr --version` is whatever RandR version your own session
reports. `wwmctl -l` on GNOME is the one that needs the [bridge
extension](#gnome). If it says so instead of listing windows, that is the step still
missing, and `wxrandr` above will have worked anyway.

On an **X11** session that block is not what you get, and that is the handover
working: `wdotool`, `wwmctl`, `wxprop` and `wxrandr` *are* the originals there, so
they print the versions installed on your machine rather than ours. On a stock Ubuntu
26.04 Xfce, for instance, `wdotool --version` says `xdotool version 3.20160805.1`,
`wxprop -version` says `xprop 1.2.7` and `wxrandr --version` says `1.5.3`, where
ours, the same commands on the same box under Wayland, say `4.20260303.1`, `1.2.8`
and `1.5.4`. (`wwmctl --version` says `1.07` either way: that is the wmctrl release
we clone.) `warandr` never hands over. It drives the real `xrandr`, and
`warandr --print-backend` says `x11`. If instead you get exit 127 and a line about no
real xdotool on `PATH`, install them: `sudo apt install xdotool wmctrl`.

If something did not work, the first thing to try:

| what you saw | what to do |
|---|---|
| `wdotool: command not found` | `command -v wdotool`, then the symlinks, or `~/.local/bin` not on `PATH` yet (log out and back in) |
| `gnome backend: the fuckwayland bridge extension is not running in GNOME Shell` | `sh gnome/install-bridge.sh`, then log out and back in. `sh gnome/install-bridge.sh --check` must say `loaded in shell: yes` and `org.fuckwayland.Bridge owned: yes` |
| `cannot create uinput devices: [Errno 13] Permission denied` | `sudo wdotool …`, or install the [udev rule](#input-access). `sudo sh gnome/install-bridge.sh --udev --check` should end `uinput usable by <your user>: yes (logind ACL)` |
| `warandr: GTK 3 for Python is not available` | `sudo apt install python3-gi gir1.2-gtk-3.0`, and the venv must have been made `--system-site-packages` |
| `xdotool: … no real xdotool was found on PATH`, exit 127 | you are on X11: `sudo apt install xdotool wmctrl` |
| the tool does something you did not expect on X11 | it *is* the original there. `FUCKWAYLAND_PASSTHROUGH=never` runs our own code instead |
| `gnome backend: the fuckwayland bridge is unavailable while the screen is locked` | unlock the session. GNOME Shell shuts its extensions down behind the lock screen, so every window command stops until you unlock, and a **default** Ubuntu desktop locks itself after 5 minutes idle (`org.gnome.desktop.session idle-delay 300`, `screensaver lock-enabled true`), which is why an unattended script that worked in the morning can fail in the afternoon. `wxrandr`, `warandr` and `wdotool`'s input commands are unaffected: they do not go through the extension |

## What your desktop needs

Whichever install route you took, your desktop wants a piece of its own, and on three
of the four that piece is nothing.

### GNOME

Stock GNOME Wayland sessions, Ubuntu 24.04 (GNOME 46) and 26.04 (GNOME 50) as
installed, are supported, with one extra step: GNOME has no window management
protocol, so the window side needs a small GNOME Shell extension that exports Mutter
over the session bus. The [.deb](#the-deb) brings it. From a clone, run the installer
yourself, because pip does not install it:

```sh
sh gnome/install-bridge.sh          # copies the extension, enables it; log out/in once
sh gnome/install-bridge.sh --check  # is it loaded? is org.fuckwayland.Bridge owned?
```

Expect one session restart. The first install exits 1 asking you to log out and back
in, because gnome-shell only scans extension directories at login. `wxrandr` and
`warandr` never need it (monitors go through Mutter's own DisplayConfig), so until
you have logged back in those two work and the window commands say:

```
gnome backend: the fuckwayland bridge extension is not running in GNOME Shell; run gnome/install-bridge.sh and restart the session (log out and back in)
```

* **The extension** (`gnome/fuckwayland-bridge@fuckwayland`, see
  [gnome/README.md](gnome/README.md)) is installed per user by default
  (`--system` for `/usr/share/gnome-shell/extensions`). After the first login the
  installer can enable and disable it live. Everything `wdotool`, `wwmctl` and
  `wxprop` do on GNOME goes through it: `search`, `windowactivate`, `windowmove`,
  `windowstate`, desktops and workspaces, `selectwindow`, `getmouselocation`'s
  window, X ids of XWayland windows.
* **Hotkeys**: bind scripts to anything but `Ctrl+Alt+F1` through `Ctrl+Alt+F12`.
  Mutter owns those as VT switches on Wayland, so gsd cannot grab them and injecting
  them switches the console. `<Ctrl><Super>F7` works. This applies to every hotkey
  suggestion anywhere in this repo.
* **Security note.** Any process on your session bus can then list, move, close and
  kill your windows through the bridge, and the user at the active seat can type as
  you through `/dev/uinput`. That is exactly what every X11 client could always do,
  and it is the point of these tools, but it is a deliberate widening of GNOME's
  default. The bridge never evaluates code and never injects input. Flatpak and Snap
  apps without session bus access cannot reach it. Do not install either piece on a
  machine where that trade is wrong, and read [Threat model](#threat-model) for the
  full list.

### KDE Plasma

Stock Plasma Wayland sessions, Plasma 5.27 (Ubuntu 24.04) and Plasma 6.6 (26.04), are
supported with **nothing to install**. `org.kde.kwin.Scripting.loadScript()` is plain
`Q_SCRIPTABLE` with no polkit action and no bus policy on both, so `wdotool` pushes
one small JavaScript file into KWin per command and unloads it again. `wwmctl`,
`wxprop` and `wxrandr` come along with it. (That is also a security note in the GNOME
sense: any client on your session bus can already do this, with or without these
tools.)

**Plasma 6.7 changed how KWin publishes displays, and `wxrandr` follows it.** From
6.7.0 an output is no longer a `kde_output_device_v2` `wl_registry` global. The
compositor hands the device objects out through a `kde_output_device_registry_v2`
object instead (kwin `7e32e00c`, never backported, and 6.6 still publishes the
globals). On a real Plasma 6.7.4 session the old global is simply **absent**, so that
second path is the only way to see an output at all, and `wxrandr` takes it: query,
mode, position, rotation, scale, `--off`, `--primary`, `--same-as` and hotplug all
measured there, on the `stonking-kde` VM image (Ubuntu 26.10), against
`kscreen-doctor`. See
[WXRANDR.md](docs/WXRANDR.md#kwin-backend-wxrandrkwinpy).

That is the *only* thing measured on 6.7 so far. `wdotool`, `wwmctl` and `wxprop`
reach KWin through its scripting interface, which this change does not touch, but
they have not been run on 6.7 here and the support matrix below is still 5.27 and
6.6. The image exists (`vm/vmctl build stonking-kde`) if you want to close that gap.

What differs from X, and why:

| | |
|---|---|
| `windowraise` on 5.27 | KWin 5.27 has no per-window raise. The window is activated instead (which focuses it), and says so on stderr. Plasma 6 raises properly |
| `windowlower` | neither release has a per-window lower: the active window is lowered for real, any other is marked keep-below, with a warning |
| `windowstate SHADED` | works on 5.27, **for X11 windows only**, because KWin shades nothing else, and says so. Plasma 6 removed window shading, so it is a clean "not supported" there |
| `windowstate MAXIMIZED_*` on 5.27 | KWin 5.27 exposes no `maximizeMode` to scripts, so a window is read as maximized when its frame fills the maximize area to within one size increment (at least 32px per axis). A merely large window therefore reads as maximized, and `--remove MAXIMIZED_*` cannot clear that reading |
| `set_num_desktops` | KWin caps virtual desktops (20 on 5.27, 25 on 6) and keeps at least one. Asking for more is capped at that with a warning, not an error |
| window ids | KWin's only window handle is a UUID, so the printed ids are minted from it (30 bits of it, `0x40000000` to `0x7FFFFFFF`, out of the range Xwayland gives its clients). They are stable while the window lives, and an X id is not accepted in their place. On sway either, where the ids are sway node ids |
| `wwmctl -l -G` positions | on Plasma 6.6 (and sway) `wmctrl` doubles the frame offset under a non-reparenting WM and ours are the real ones. KWin 5.27's xwm *does* reparent, so both agree there |
| a state KWin ignores | KWin accepts a state a window rule or the client's size hints forbid and does nothing with it. `wwmctl` then sends the EWMH `_NET_WM_STATE` ClientMessage instead, which reaches an XWayland window through KWin's X-plane window manager, and checks that one landed too. `wdotool` has no second route and says what happened |
| `selectwindow` | KWin has one reply slot for its window picker, so a second picker started while the first is up takes the click. The first call then waits until `WDOTOOL_SELECT_TIMEOUT` (2 minutes) and says so |
| XWayland ids on Plasma 6 | `x11window.h` lost every scriptable property in 6, so `View.xid` is matched through the X server's own client list: pid and `WM_CLASS` filter, title and geometry score. Where those tie, two windows of one application in the same place under the same title, the order of the two lists decides, and that is exact rather than a guess: `_NET_CLIENT_LIST` is KWin's own window list with everything but the managed X11 windows dropped. A client that publishes neither `_NET_WM_PID` nor `WM_CLASS`, and a pair that nothing at all separates, keep id 0 rather than being handed one of two ids |
| `wxprop -root` | `_NET_CLIENT_LIST`, `_NET_ACTIVE_WINDOW` and `_NET_DESKTOP_NAMES` are ours (native windows included), not KWin's stale X copies |
| `getmouselocation` | answered by KWin (`workspace.cursorPos`), like GNOME's: a mouse moved by hand, or by another process, reads correctly, and the query needs no `/dev/uinput` at all |

A window state that a client applies asynchronously (fullscreen and maximize on a
Wayland client, applied when it acks the configure) is waited for before the command
returns, so `windowstate` never reports a state it merely has not seen land yet, and
the next command sees a settled window.

**Plasma on X11** (Kubuntu's X11 session, and 26.04's after `apt install
plasma-session-x11`) is none of the above. It is a plain [X11](#x11-sessions)
session, so all four command-line tools hand over to the real `xdotool`, `wmctrl`,
`xprop` and `xrandr`, and `warandr` drives the real `xrandr`. `kwin_x11` owning
`org.kde.KWin` does not change that, because the session decides and not the session
bus, and there is no compositor socket for the KDE display backend to talk to, so
`wxrandr` goes to the X server (`--print-backend` says `x11`). Everything in the
table above therefore applies to the Wayland session only. On Xorg you get X's own
answers, including `xdotool`'s real X window ids and whatever `xdotool` version is
installed. The KWin backend is still reachable on purpose with
`FUCKWAYLAND_PASSTHROUGH=never`, and there it does work, but it is a downgrade on
that session (no `getdisplaygeometry`, minted ids instead of X ids), which is why it
is not the default.

### sway and other wlroots compositors

Stock sway (1.11 on Ubuntu 26.04) is supported with **nothing to install** for the
four command-line tools: they speak sway's own IPC, and `wxrandr --print-backend`
answers `sway`. It is also the one family where **input needs no privilege at all**,
because the compositor offers both halves as unprivileged Wayland protocols. See
[Input access](#input-access).

The GUI is the exception, and it is the one place a sway install differs from a
GNOME, KDE or Xfce one: a minimal sway install has `python3-gi` but **not** the GTK 3
typelib, so `warandr` exits 1 with

```
warandr: GTK 3 for Python is not available (Namespace Gtk not available) - on Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0
```

`sudo apt install python3-gi gir1.2-gtk-3.0` is the whole fix. The four command-line
tools never notice either way, and with a `--system-site-packages` venv the GUI picks
the bindings up with no further step. Two smaller differences, both cosmetic: a
minimal image has no `acl` package either, so `install-bridge.sh --udev --check` says
`uinput ACL users: yes, not listed here (apt install acl)` where a desktop with
`getfacl` lists the user by name (the answer on the line below it, `uinput usable by
<you>`, is the same either way), and `windowmove` on a *tiled* window warns and does
not move it, so float it first (`swaymsg floating enable`). See the [support
matrix](#desktop-support) for the rest.

### X11 sessions

**What to install:** the real tools, if they are not already there,
`sudo apt install xdotool wmctrl`. (`xprop` and `xrandr` come with every X11 desktop,
in `x11-utils` and `x11-xserver-utils`.) Nothing else: no extension, no udev rule, no
`/dev/uinput`. Without them you get exit **127** and a line naming the package to
install.

The tools are meant to be installed **over** the originals, so they also have to
behave when the session is a plain X11 one (Xfce, i3, GNOME-on-Xorg, KDE-on-Xorg, the
last two measured, see [Desktop support](#desktop-support) note **(j)**). There they
detect the session and hand over to the real `xdotool`, `wmctrl`, `xprop` or `xrandr`
with `execve` and argv untouched: same exit status, same signals, same stdio, no
extra process. One script then runs on both session types, and `xdotool --version` on
X11 answers with the version that is actually installed there.

Four things stay ours on either session type, because the original has no such thing
to hand over to: `wdotool keys` (and the hidden `__keymap`), and the leading
`--layout` and `--vkbd` options, which are stripped before the handover. So on X11
`xdotool keys explain a` answers from our own tables where the real xdotool says
`Unknown command: keys`, and `xdotool --layout us key a` hands `key a` over where the
real one refuses the option. They never fail on an X11 session, they just say what
they could not read (`note: the compositor's keymap could not be read (no wayland
socket found)`).

```console
$ FUCKWAYLAND_PASSTHROUGH=never xdotool key a   # our own code, whatever the session
$ FUCKWAYLAND_PASSTHROUGH=always ...            # hand over, whatever the session
$ WDOTOOL_REAL_XDOTOOL=/opt/bin/xdotool ...     # where the original is
```

`WDOTOOL_PASSTHROUGH`, `WWMCTL_PASSTHROUGH`, `WXPROP_PASSTHROUGH` and
`WXRANDR_PASSTHROUGH` do the same per tool. `warandr` ignores all of them: it never
hands over, it only picks between the `xrandr` and `wxrandr` command words, and it
keeps doing that by session (it does take the `DISPLAY` and `XAUTHORITY` repair below
for the `xrandr` it runs). The `*_REAL_*` variables are `WDOTOOL_REAL_XDOTOOL`,
`WWMCTL_REAL_WMCTRL`, `WXPROP_REAL_XPROP` and `WXRANDR_REAL_XRANDR`. With no original
installed you get exit **127** and a line saying which package to install, except for
`--help` and `--version`, which still answer, and except for `wxprop`, which has an
X11 client of its own and simply keeps working. Detection is Wayland first, because
`$DISPLAY` is set on a Wayland session too (Xwayland), so only a live compositor
socket, or `loginctl`'s own record of your session, counts.

Bonus, on X11 as on Wayland: run under `sudo`, over `ssh root@box` or from cron and
we find the session's `DISPLAY` and `XAUTHORITY` and hand them to the original, so
`sudo xdotool key a` works *through* us where `sudo /usr/bin/xdotool key a` says
`Can't open display`. Where the cookie is looked for, and why the session leader's
own environment is the only route to SDDM 0.20's, is
[Technical.md](docs/Technical.md#2-session-discovery-and-the-x11-handover). `warandr` gets
the same repair for the `xrandr` it runs, so `--command` and `--save` answer from a
root shell too, but a *saved* layout script calls the bare command word, exactly as
arandr's does, so running the script itself still wants a session (on Wayland that
word is `wxrandr`, which finds one for itself).

### Input access

Injecting input goes through the kernel's `/dev/uinput`, which is `root:root 0600` on
a stock Ubuntu, so `wdotool`'s **input** commands (`key`, `type`, `click`,
`mousemove`, `mousedown` and `mouseup`, `behave`, and any chain containing one) need
either root or the rule below. On **sway and the wlroots family** they need neither,
because the compositor offers both halves as unprivileged Wayland protocols and
wdotool uses them exactly where the kernel device is closed. Everywhere else, without
the rule, they stop with

```
cannot create uinput devices: [Errno 13] Permission denied: '/dev/uinput'
(wdotool injects input via /dev/uinput; run it as root)
```

Everything else needs nothing. The window commands (`search`, `windowactivate`,
`windowmove`, `windowstate`, `getactivewindow`, `selectwindow`, the desktop ones),
all of `wwmctl`, `wxprop`, `wxrandr` and `warandr` reach the compositor over your own
session bus and run as you.

Two ways to get it, then. The [.deb](#the-deb) does the second for you.

* **Run as root.** `sudo wdotool key a` works with no rule installed at all. The
  session's sockets are found by scanning `/run/user/*`, which is what makes every
  tool here work under `sudo`, over `ssh root@box` and from cron.
* **Install the udev rule this repo ships**, once, from the clone:

  ```sh
  sudo sh gnome/install-bridge.sh --udev            # install it
  sudo sh gnome/install-bridge.sh --udev --check    # what is the node now?
  sudo sh gnome/install-bridge.sh --udev --uninstall # put it back
  ```

  It tags the node `uaccess`, so systemd-logind gives the user of the *active seat*
  an ACL on it: applied immediately (no relogin needed) and again at every login. The
  node itself stays `root:root 0600`, no `input` group is involved, and
  `--uninstall` restores exactly that. Despite living under `gnome/`, none of this is
  GNOME's business: the same command installs the same rule on a Plasma, sway or Xfce
  session, `--udev` never touches the bridge extension, and the tools there use it
  the same way.

  Read the security note at the end of the [GNOME](#gnome) section first: anyone who
  can open `/dev/uinput` can type as you.

One gotcha while you are experimenting: `wdotool` keeps the virtual devices alive in
a small `__daemon` process, and one started while access existed keeps injecting
after the rule is removed. Log out, or stop it, to see the change.

None of these routes goes through the desktop portal, so none of them raises its
consent dialog. The next section is the measurement behind that.

### No authorization dialog

GNOME and KDE both show a consent dialog to an application that injects input through
the **desktop portal**, the *Remote Desktop* and *Input Capture* prompt every libei
client has to get past, once per session. Nothing here ever raises it, and nothing
here ever raises a **polkit** prompt either: no tool of ours speaks to the portal at
all, and none of them defines, calls or needs a PolicyKit action. There is nothing to
switch off with `sudo`, because there is no consent step on the path to begin with.

What is on the path instead:

* **GNOME and KDE Plasma**: the kernel's `/dev/uinput` for input, opened as root or
  through the [udev rule](#input-access) above, and the compositor's own session bus
  interfaces for windows and displays (the [bridge extension](#gnome) on GNOME,
  [KWin scripting](#kde-plasma) on KDE). Plain method calls with no authorization
  behind them.
* **sway and the wlroots family**: `zwp_virtual_keyboard_v1` and
  `zwlr_virtual_pointer_v1`, which the compositor hands to every client of your
  socket. No root, no rule, nobody asked.
* **X11 sessions**: the real `xdotool`, `wmctrl`, `xprop` and `xrandr`, which predate
  portals entirely.

**Measured, not assumed.** Six images, GNOME 46 and GNOME 50 (the 26.04 default
install off the ISO among them) and Plasma 5.27, 6.6 and 6.7, with every command run
three ways: as root, as a plain user with the udev rule, and as a plain user with
neither. Watching throughout: the session bus for portal traffic, the system bus for
polkit `CheckAuthorization` and `BeginAuthentication`, the window list for windows we
did not open, and both screens compared pixel by pixel around every command. On all
six, and for the installer and the udev rule as well as the tools: **no prompt, no
window we did not open, and not one portal call from anything of ours.** The same rig
pointed at a real portal client and at `pkexec` produced both dialogs on every image,
so it does see one when there is one. `tests/test_no_portal.py` is what keeps it
true.

**The one prompt that does exist** is GNOME's own *Keep these display settings?*, and
only an explicit `wxrandr --persistent` asks for it. That is what the flag is for, it
is the only way a wxrandr layout reaches `monitors.xml`, and wxrandr says on stderr
that it is coming. Leave the flag off and nothing appears. KWin has no equivalent: it
applies and saves at once, and says so.

## Other ways to install

All of these were run on a stock desktop and all of them work. Pick by what you want,
not by what is possible.

* **pipx**: `sudo apt install pipx`, then `pipx install --system-site-packages -e .`
  and `pipx ensurepath` once. Prefer it if pipx is already how you keep your tools.
  `--system-site-packages` is not optional here either: without it `warandr` fails
  exactly as [above](#from-a-clone-with-pip). Lands in `~/.local/bin`. Undo with
  `pipx uninstall fuckwayland`.
* **The user site, overriding the rule**: `sudo apt install python3-pip`, then
  `pip install --user --break-system-packages -e .`. Prefer it when you want no venv
  at all and you accept the risk that flag names. Also `~/.local/bin`, which pip
  warns is not on `PATH`: a *login* shell adds it from `~/.profile`, but only if the
  directory existed at login, so log out and back in once. Undo with
  `pip uninstall --break-system-packages fuckwayland`.
* **One venv for the whole machine**: `sudo python3 -m venv --system-site-packages
  /opt/fuckwayland`, then `sudo /opt/fuckwayland/bin/pip install /path/to/the/clone`,
  and symlink out of `/opt/fuckwayland/bin`. Prefer it when other accounts (or `sudo`
  as another user) must run the tools: Ubuntu home directories are `0750`, so a venv
  under your `$HOME` is unreadable to them. Note the missing `-e`: an editable
  install keeps reading the source tree at run time and hits the same wall, while
  from a readable copy it does not.

### Installing over the originals

These are drop-in clones, so the last step is usually to put them where your scripts
already look. `/usr/local/bin` comes before `/usr/bin` on Ubuntu's default `PATH`, so
symlinking there wins without touching a single file the package manager owns, and
the originals stay exactly where they are, which is what makes the
[X11 handover](#x11-sessions) work at all.

From a venv install:

```sh
sudo ln -sfn ~/.venvs/fuckwayland/bin/wdotool /usr/local/bin/xdotool
sudo ln -sfn ~/.venvs/fuckwayland/bin/wwmctl  /usr/local/bin/wmctrl
sudo ln -sfn ~/.venvs/fuckwayland/bin/wxprop  /usr/local/bin/xprop
sudo ln -sfn ~/.venvs/fuckwayland/bin/wxrandr /usr/local/bin/xrandr
```

From pipx or a `--user` install the source is `~/.local/bin/wdotool` instead. From a
single-file build, copy rather than link:
`sudo install -m 755 dist/wdotool /usr/local/bin/xdotool`. A clone that finds itself
under an original's name recognises itself and skips to the real binary, so none of
these loop.

Undo is `sudo rm` of the four names, because nothing else was touched:

```sh
sudo rm -f /usr/local/bin/xdotool /usr/local/bin/wmctrl \
           /usr/local/bin/xprop /usr/local/bin/xrandr
```

Two cautions. A venv under `$HOME` is only readable by you and root (Ubuntu homes are
`0750`), so symlinks into `/usr/local/bin` that point into it break for *other*
users, and the message they get does not say why: `sudo` reports `unable to execute
/usr/local/bin/wdotool: Permission denied` on 24.04 and `sudo:
'/usr/local/bin/wdotool': command not found` on 26.04. For a machine wide drop-in use
the `/opt` venv above. And a symlink left behind after the venv is deleted just says
`No such file or directory`, so remove the links when you remove the install.

### Without installing: the single-file builds

```sh
sh scripts/build-pyz.sh
```

builds `dist/wdotool`, `dist/wwmctl`, `dist/wxprop`, `dist/wxrandr`, `dist/warandr`
and `dist/wmirror`: six self-contained executables needing nothing but the `python3`
that is already on the machine. No pip, no venv, no apt, not even a package to add.
Every bundle carries `fwcommon` and `wdotool`, because that is where session
discovery, the X11 handover and the wire clients live, plus whatever else that tool
imports. `warandr` carries `wxrandr` inside itself and still imports the system GTK
bindings at run time, so the GUI wants `python3-gi` and `gir1.2-gtk-3.0` like
everywhere else. The other five are stdlib only, and `wmirror` runs `wl-mirror` as a
program rather than importing anything of it.
[Technical.md](docs/Technical.md#the-single-file-builds) has the table of what is in each.

Prefer this when you would rather not touch apt at all, when you want no venv, when
you are on a machine you do not administer, or when you want one file to copy to
another box. They run from `dist/`, from `~/bin`, or from `/usr/local/bin` under an
original's name, see [above](#installing-over-the-originals). What you give up is
`pip uninstall` and any notion of an upgrade: you rebuild and copy again.

### Nix

`nix build` gives you `result/bin/` with all six tools, plus `xdotool`, `wmctrl`,
`xprop`, `xrandr` and `arandr` symlinks next to them (`wmirror` gets none, because
there is no X11 original to shadow). The flake wraps the GTK typelibs into `warandr`,
so the GUI works without a system PyGObject.

## Desktop support

What each tool does on each desktop, measured rather than assumed. The branch is run
on nine golden VM images, GNOME 46 and 50, Plasma 5.27 and 6.6 on Wayland and the
same two again on **Xorg**, Xfce 4.18 and 4.20, sway 1.11 on wlroots, twice per
image, once **inside the session** and once as **root over ssh with an empty
environment**, against real windows on a two-head layout. `vm/README.md` keeps the rig
and the verbatim messages behind these cells. A tenth cloud image, Plasma 6.7 on
26.10, is a probe for one protocol change rather than a support target: what has been
measured on it is `wxrandr` and nothing else, and the cells below do not count it.

Those images are an Ubuntu **cloud** image plus a desktop metapackage, which is close
to a desktop install but measurably not one. The closest of them, `resolute-gnome`,
carries 226 packages a real Ubuntu 26.04 desktop installation does not have and is
missing 55 it does, on a different kernel, from a different install source (8 snaps
against the default install's 13). `noble-gnome` is 240 and 55 away from a real 24.04
one. So the rig also has a default install of **each supported LTS**,
**`resolute-gnome-iso`** and **`noble-gnome-iso`**: Ubuntu 26.04 and 24.04 installed
from `ubuntu-26.04.1-desktop-amd64.iso` and `ubuntu-24.04.4-desktop-amd64.iso` by the
Ubuntu installer, every question left alone, with the first-run experience, screen
lock and automatic updates still switched on. One package added to the default set
(`openssh-server`, the only way into a VM) and nothing removed. Twelve images in all:
ten cloud image flavors and two real desktop installs. The cells below are still the
measurement on the nine. The default installs are what they get re-measured on. How
they are built and every deviation from stock: `vm/README.md`.

**What those images say about this guide.** The install above was then run on both of
them *verbatim*, as a reader would: `sudo apt install git python3-venv`, clone, venv,
`pip install -e .`, the `/usr/local/bin` symlinks, `gnome/install-bridge.sh`, one
logout, `--udev`. Every command worked as written on both, and the stock facts the
guide leans on hold on a real default install of either release (no pip, no venv, no
pipx, no `git`, no `curl`, while `python3-gi`, `gir1.2-gtk-3.0`, `acl`, `x11-utils`
and `x11-xserver-utils` are all present, so `warandr`'s GUI comes up with nothing
extra installed). All six tools then behaved **identically to the matching cloud
image flavor**, as the desktop user and as root over ssh with an empty environment.
The 24.04 run corrected two sentences of this guide, both about the *optional*
`--no-build-isolation` line and the bare `python3 -m venv` error, and they are
corrected above. Two things about a default install are worth knowing before you
trust a script on one, and neither is visible on the cloud image flavors, which
switch both off:

* **it locks itself.** `idle-delay 300` and `lock-enabled true` are the defaults on
  24.04 and 26.04 alike, and GNOME Shell disables extensions behind the lock screen,
  so five idle minutes turn every window command into `gnome backend: the fuckwayland
  bridge is unavailable while the screen is locked`, rc 1 (rc 2 for `wdotool`). It
  also switches the outputs off, so a screenshot taken then is black. `wxrandr`,
  `warandr` and input injection keep working, and injecting the password is a way
  back in. Five minutes is less than the guide above takes: on the 24.04 default
  install `sudo apt install git python3-venv` alone ran for three and a half minutes
  and the session was locked by the time the bridge was installed.
* **it has no `xdotool` and no `wmctrl`**, so the [X11](#x11-sessions) handover has
  nothing to hand to until you `sudo apt install xdotool wmctrl`. On a Wayland
  session nothing hands over, so this only bites on an X11 session or under
  `FUCKWAYLAND_PASSTHROUGH=always`.

The last column is a *session type*, not a desktop: what an X11 session gets is the
real tools, whichever desktop is drawing it.

| | GNOME 46 / 50 | Plasma 5.27 / 6.6 (Wayland) | X11 sessions: Xfce 4.18 / 4.20, Plasma on Xorg **(j)** | sway 1.11 (wlroots) |
|---|---|---|---|---|
| **wdotool** | all 48 commands, and the window ones need the [bridge extension](#gnome) | all 48, nothing to install **(a)** | hands over to the installed `xdotool` **(b)** | all 48, four differences **(c)** |
| **wwmctl** | works, the window list needs the bridge | works **(d)** | hands over to `wmctrl` | works |
| **wxprop** | works, X and native windows | works | hands over to `xprop` | works, and from a root shell `-root` is synthesized **(e)** |
| **wxrandr** | works (mutter) | works (kwin) **(f)** | hands over to `xrandr` **(g)** | works (sway) |
| **warandr** | works (mutter) | works (kwin) **(f)** | works, driving the real `xrandr` **(g)** | works (sway), and the stock image has no GTK 3 bindings **(h)** |
| **wmirror** | no, no capture protocol, the portal prompts **(k)** | no, same **(k)** | no, X11 mirrors outputs with `xrandr --same-as` | region and odd-shape mirroring, via `wl-mirror` **(k)** |
| **`wdotool` without root** | pointer *and* keyboard need the udev rule (or root) | pointer *and* keyboard need the udev rule (or root) | nothing needs it (X11) | **nothing needs it**: keyboard and pointer both **(i)** |

All of it works **as the desktop user and as root** (`sudo`, `ssh root@box`, cron),
because the session's compositor socket, session bus, `DISPLAY` and X cookie are
found for you. **(e)** is the one exception, and it is not one we can fix. And on
none of these desktops does any of it show an authorization dialog or touch the
desktop portal, measured on six of the rig's images: [No authorization
dialog](#no-authorization-dialog).

**(a)** With the differences in the [KDE Plasma](#kde-plasma) table above (raise,
lower, shading, maximize on 5.27, minted window ids).

**(b)** On a plain X11 session the tools *are* the originals, so the command set is
whatever is installed there. Both Ubuntu images this branch tests on carry **xdotool
3.20160805.1**, which has no `windowstate` (`xdotool: Unknown command: windowstate`,
rc 1), while parity is claimed against xdotool 4.20260303.1, whose full command set
is what our own Wayland code implements. `getdisplaygeometry` likewise answers per
screen (`1920 1080`) where the Wayland backends report the whole layout span
(`3840 1080`). That is xdotool's own behaviour, faithfully.

**(c)** Four, all about sway's tiling. `windowmove` and `windowsize` on a *tiled*
window warn and succeed without changing it (float it first, `swaymsg floating
enable`, and then the move and the resize land exactly, because `resize set` on a
tiled container moves the split ratio, which is not the size you asked for).
`windowraise` on a tiled window likewise warns and does nothing, because a tiled
container has no stacking position to change (and `windowlower` warns on every
window, since sway has no lower at all). And `windowstate MAXIMIZED_VERT` or
`_HORZ` has no equivalent in sway and fails cleanly.

**(d)** On Plasma 6.6 plasmashell's own desktop windows carry an empty caption, so
those `wwmctl -l` rows have a blank title where 5.27 prints `Desktop @ QRect(…)`.
KWin's caption is what we print. Ids, pid, class and geometry are right on both.

**(e)** sway's Xwayland runs with no authority file, so **only the session user's own
processes can open it**: the real `xprop` from a root shell gets `Authorization
required, but no authorization protocol specified` there too. Rather than fail,
`wxprop -root` answers from sway's IPC, a synthesized `_NET_CLIENT_LIST` of
compositor ids and `_NET_SUPPORTING_WM_CHECK … 0x0`. Inside the session it is
Xwayland's real root window, and every other desktop gives root the real X root.

**(f)** KWin applies a layout immediately and permanently, with no temporary mode and
no confirmation dialog, and says so on stderr, together with the line that puts the
previous layout back. Where it keeps that layout, and how to clear it, is
[WXRANDR.md](docs/WXRANDR.md#keeping-a-layout). `--same-as` is plainly the same position,
which on KWin already shows identical pixels. It reaches for the compositor's own
`set_replication_source` only when the two outputs' logical rectangles differ and a
shared position would give a crop instead of a copy (and says which KWin version that
would need, when the running one is older). A replicated output has no rectangle of
its own on that desktop, because KWin drops it out of the layout, so `--query`
reports it at its source's geometry, `--right-of` it starts where the source ends,
and it cannot be the primary. Mirroring *onto* one is resolved to the output whose
picture it is really showing: KWin accepts a copy of a copy and then never draws it.
Both cells are measured on Plasma 5.27 and 6.6, and the display path again on
**6.7.4**, where KWin publishes outputs through a registry object instead of as
globals and `wxrandr` switches discovery paths to match, see
[KDE Plasma](#kde-plasma). The other four cells in this column are 5.27 and 6.6 only.

**(g)** X11 answers are the X server's own (`Screen 0: minimum 320 x 200 … maximum
8192 x 8192`), and whether an output is marked `primary` is the desktop's business.

**(h)** `warandr` is the one tool with a dependency (`python3-gi`,
`gir1.2-gtk-3.0`). GNOME, KDE and Xfce installs have them, a minimal sway image may
not, and warandr then names the package and exits 1. The other five are stdlib only.

**(i)** sway and wlroots is the only one of the four families that implements
`zwp_virtual_keyboard_v1` *and* `zwlr_virtual_pointer_v1`, so it is the only one
where every injecting command (`key`, `keydown`, `keyup`, `type`, `click`,
`mousedown`, `mouseup`, `mousemove`, `mousemove_relative`) runs with no root, no
group and no udev rule. Mutter and KWin (6.6 and 5.27, both measured) implement
neither, so on GNOME and KDE every injecting command still goes through
`/dev/uinput`. [WDOTOOL.md](docs/WDOTOOL.md#typing-and-clicking-with-no-privilege---vkbd)
is the measurement.

**(j)** Plasma on Xorg is an X11 session like any other and is handled like one,
measured on both generations: Plasma 5.27 with KWin 5.27 (Kubuntu 24.04, the X11
session it ships) and Plasma 6.6 with KWin 6.6 (26.04, after
`apt install plasma-session-x11`, since `kubuntu-desktop` installs no X11 session
there although the packages are in the archive). Every handover is the same on both,
and so is every byte of the output. Two things are worth naming because they look
like they might change the answer and do not. **KWin owns `org.kde.KWin` on the
session bus exactly as it does on Wayland**, but the handover is decided before any
backend is detected, and by the session, not by the bus, so nothing of ours ever asks
it: `session_kind()` is `x11` for all four tools, a Plasma X11 session has no
compositor socket at all (`find_wayland_socket()` answers `None`),
`wxrandr --print-backend` says `x11` with `compositor: X server (RandR)`, and
`wxrandr --backends` marks `kwin` *unavailable, no wayland socket*. The KWin backend
**would** half work there (`FUCKWAYLAND_PASSTHROUGH=never wwmctl -l` does list KWin's
windows), which is the argument for the handover rather than against it: on the same
session our own `getdisplaygeometry` fails (`no wayland socket found`, rc 2) where
the real `xdotool` answers, and the ids are only right by accident. 5.27 still hands
`windowId` to scripts, so those rows carry the real X ids, while KWin 6.6 has dropped
it and the same command prints ids minted from KWin uuids on a session where every
window has an X id. The recovery that fills those in on a Wayland session cannot help
here: it is gated on an `Xwayland` process being alive, on the sound principle that
connecting to the X plane must not *start* one, and a Plasma X11 session has Xorg,
not Xwayland.

**(k)** `wmirror` drives the external `wl-mirror`, which needs wlroots'
`zwlr_screencopy_manager_v1` or the standard `ext-image-copy-capture-v1`. Neither
KWin nor Mutter implements either (on KWin the latter is an open feature request), so
on GNOME and KDE the only capture route is the desktop portal, which asks the user
for permission once per session. That is useless from a hotkey, and the one thing
these tools will not do, see [No authorization
dialog](#no-authorization-dialog). wmirror says exactly that and exits 1, rather than
half working.

## The tools

Each has a contract of its own, and that is where the measured detail lives. What
follows is what each one is for.

### wdotool

xdotool, but it works on Wayland. Drop-in: same commands, same flags, same output
bytes, same chaining, same scripts. Symlink it as `xdotool` and your scripts don't
know the difference.

![wdotool driving a real Wayland desktop](demo.gif)

*(that's wdotool driving a live sway session: typing, chaining, window search,
floating-window moves, fullscreen, mouse, close, recorded in the Ubuntu 26.04 VM this
repo tests in)*

```console
# wdotool search --class foot windowactivate --sync type 'echo hello from wayland'
# wdotool key Return
# wdotool mousemove 640 360 click 1 getmouselocation
x:640 y:360 screen:0 window:5
```

There is no X server to lie to, so wdotool goes underneath instead. **Input** is
injected as kernel level virtual devices via `/dev/uinput`, a keyboard, a relative
mouse and an absolute tablet (the same shape QEMU uses, which every compositor maps
across the whole output layout), so the compositor cannot tell it from real hardware.
On wlroots every injecting command skips that entirely through
`zwp_virtual_keyboard_v1` and `zwlr_virtual_pointer_v1` and needs no privilege at
all. The first invocation forks a small daemon that owns the devices, because
creating them costs about 600ms of hotplug and you should pay it once. **Window
management** talks to the compositor: sway and i3 IPC, GNOME Shell through the
bundled bridge extension, KDE Plasma through KWin scripting, and the
wlr-foreign-toplevel protocol as the generic fallback. Window ids are real, stable
and decimal, like X window ids, so scripts pipe them around unchanged.

All 48 commands, byte-parity against xdotool 4.20260303.1, verbatim C bugs included.
Non-US keyboard layouts work, by reading the compositor's own keymap and looking the
character up backwards, and on a plain US layout none of that code runs at all.
`wdotool keys` is the layout machinery pointed the other way: what to press for a
character, or what you just pressed.

**Everything about it is in [WDOTOOL.md](docs/WDOTOOL.md)**: the honest approximations
table, keyboard layouts and `--layout`, the two privilege-free injection paths and
`--vkbd`, `wdotool keys`, exit codes, the bounded `--sync` waits, pointer accuracy,
the input daemon and the per-compositor backend notes.

### wwmctl

![wwmctl listing native and X windows in one list, then acting on them](wwmctl-demo.gif)

`wmctrl`, same treatment, and it handles **both** native Wayland apps and legacy X
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
an urgency hint. wwmctl routes every action through the compositor, so `-a` focuses,
`-c` closes and `-e` moves, for X and Wayland windows alike. Symlink it as `wmctrl`
(nix does this for you) and byte-parity covers the rest: help text, list formats,
error strings, exit codes. One deliberate exception: the machine column is sized from
the longest hostname, not, as wmctrl 1.07's `main.c` does, from the last row's, which
our stacking-ordered list would reflow on every raise.

On GNOME (with the [bridge extension](#gnome)) the same list mixes XWayland windows
under their real X ids with native windows under Mutter's ids, `-d` prints GNOME's
workspace names and work areas, `-m` says `GNOME Shell`, `-k` and `-n` reach the
shell, and every action goes through Mutter, including `-b add,maximized_vert` as a
real per-axis maximize. The X plane is reached with Mutter's own Xwayland cookie, so
it works from a custom shortcut, under `sudo` and from `ssh root@` alike, and
Xwayland (which Mutter starts on demand) is never spawned just to be listed.

Contract: [WWMCTL.md](docs/WWMCTL.md).

### wxprop

![wxprop rendering a _NET_WM_ICON as ASCII art, byte-identical to real xprop](wxprop-demo.gif)

`xprop`, dual-plane. XWayland windows report their **real** X properties, byte for
byte identical to xprop 1.2.8. The whole formatting machine is ported, down to the
`WM_HINTS` and `WM_SIZE_HINTS` structured dumps, the dformat mini-language, 32-bit
sign-extension quirks, and yes, the `_NET_WM_ICON` ASCII-art renderer. Native Wayland
windows get a synthesized property set in the same grammar, so `xprop -id N WM_CLASS`
script parsing works on every window:

```console
$ wxprop -id 0x0040000c WM_CLASS       # an XWayland window, real X property
WM_CLASS(STRING) = "xterm", "XTerm"

$ wxprop -id 5 WM_CLASS                # a native Wayland window, synthesized
WM_CLASS(STRING) = "foot", "foot"
```

`-set`, `-remove` and `-spy` work on the X plane. `-f`, `-fs`, dformats, `-len`,
`-root`, `-name` and click-to-select all match the real tool, including which
double-dash forms it rejects. Verified byte-identical against the real xprop on a
live XWayland server. `-font` is real too: XWayland serves the core fonts, so
`wxprop -font fixed` is byte-identical to `xprop -font fixed`.

On GNOME native windows get their synthesized set from the bridge (states, window
types, `WM_CLASS` from the app id) and `-spy` follows the shell's window events.
`-root` is Mutter's real X root with `_NET_CLIENT_LIST`, `_NET_ACTIVE_WINDOW` and the
desktop properties re-synthesized so they cover native windows too.

Two honest limits, both inherent. A native window has no window-type hint to report
(xdg-shell has none), so a GTK dialog prints `_NET_WM_WINDOW_TYPE_NORMAL` where its
XWayland twin prints `DIALOG`. And under `-len` truncation real xprop renders
*uninitialised heap* past the end of the fetched data, which nothing can reproduce,
so we stop at the budget instead.

Contract: [WXPROP.md](docs/WXPROP.md).

### wxrandr

![wxrandr reshaping a multi-output layout live: panels sliding, rotating, scaling](wxrandr-demo.gif)

`xrandr`, with the crazy multimonitor configs as the whole point rather than an
afterthought. A real pending-geometry resolver means relative-placement chains
resolve in **one atomic invocation**:

```console
$ wxrandr --output DP-2 --right-of DP-1 --output HDMI-A-1 --below DP-2 --rotate left
$ wxrandr --output DP-2 --scale 1.5x1.5 --output DP-1 --primary
$ wxrandr --output HDMI-A-1 --same-as DP-1        # mirror
```

Mirroring, rotation, reflection, mixed per-output scales, portrait and landscape
mixes, custom modelines (`--newmode` with real pixel-clock math), holes in a row,
negative origins and `--dryrun` are all first class. `--brightness` and `--gamma` run
over wlr gamma-control via a detached holder process, because the control dies with
its client and we simply refuse to die. Query and `--listmonitors` output is byte
styled after xrandr 1.5.4, and the layout stays consistent with the rest of the
toolbox: after any change, `wdotool getdisplaygeometry` and `wwmctl -d` track the new
world.

It also works on a stock GNOME desktop, Ubuntu 24.04 and 26.04 as installed, with no
shell extension and no root: wxrandr talks to `org.gnome.Mutter.DisplayConfig` on the
session bus through the toolbox's own stdlib D-Bus client and submits the whole
layout as one `ApplyMonitorsConfig` call. Mutter's own validation errors come back as
one-line failures in Mutter's name (`xrandr: GNOME's Mutter refused this layout:
...`), because nothing is refused here, so a "no" is always the compositor's. Since
Mutter, unlike X, allows neither gaps nor overlaps, an output that changes size keeps
its neighbours touching it, with a warning. Changes are temporary like xrandr's and
write nothing. `--persistent` makes GNOME ask *Keep changes?*, and only a confirmed
dialog writes `monitors.xml`.

Which backend it is using is never a guess: `--print-backend` prints the token
(`--verbose` adds the session, why it was chosen, the compositor and the protocol
version), `--backends` lists them all with their availability here and a reason where
there is none, and `--backend NAME` forces one for that invocation, beating
`$WXRANDR_BACKEND`, which beats detection. `--backend x11` means "hand over to the
real xrandr", even on Wayland. A Wayland backend runs our own code even on X11. An
unavailable one is one line saying what was missing, never a silent fallback.

```console
$ wxrandr --backends
  sway    unavailable  no sway or i3 IPC socket ($SWAYSOCK)
  kwin    unavailable  the compositor does not advertise kde_output_management_v2
* mutter  available    org.gnome.Mutter.DisplayConfig on the session bus
  wlr     unavailable  the compositor does not advertise zwlr_output_manager_v1
  x11     available    /usr/bin/xrandr
```

**Nothing here restores a layout on its own.** There is no daemon, no service and no
autostart entry, and what becomes of a layout after you set it is the desktop's
business, which the four desktops do not agree on. The measured table, and the recipe
for putting a layout on a hotkey, are
[WXRANDR.md § Keeping a layout](docs/WXRANDR.md#keeping-a-layout).

Contract: [WXRANDR.md](docs/WXRANDR.md).

### warandr

`arandr`, the little GTK window where you drag your monitors around, reborn for
Wayland. Same window, same menus, same `~/.screenlayout/*.sh` scripts: warandr loads
arandr's saved layouts and arandr loads warandr's. Under Wayland it talks to
`wxrandr` (one atomic apply per click), under X11 to plain `xrandr`, and it tells you
in the status bar exactly which command Apply is about to run:

```console
$ warandr                      # the GUI: drag, snap, right-click, Apply
$ warandr --command            # what Apply would run, no GUI
wxrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal --output HDMI-A-1 --mode 1280x1024 --pos 1920x0 --rotate left
$ warandr --save ~/.screenlayout/desk.sh   # an arandr-compatible layout script
```

On top of arandr's menu (Active, Primary, Resolution, Orientation) every output also
gets Refresh rate, Reflection, Mirror of, and, on Wayland only, Scale (1 to 3, the
compositor's HiDPI factor). **Overlapping outputs are allowed wherever the desktop
allows them**, measured: X11, KWin and sway or wlroots all take the geometry *and*
show the same pixels in the shared region (byte-identical crops on both heads), so a
partial overlap really is a partial mirror there, while GNOME's Mutter refuses any
layout that is not edge adjacent and warandr reports that refusal in Mutter's name,
not its own. Mirror of two outputs whose sizes do not match is where that stops being
enough, because a shared position then crops rather than copies, and on KDE
`--same-as` switches to KWin's own output replication for exactly those and only
those. The status bar says which of the four you are getting at the moment of the
drop, and the saved script keeps it in its comment header.

The layout is kept anchored at 0,0, Apply runs off the main loop, and a failed Apply
keeps your edits. It needs the GTK 3 bindings every stock Ubuntu desktop already has
and nothing else, not even cairo, because the canvas is plain widgets.
`warandr.desktop` puts it in the Settings menu.

Which backend it is talking to is in the window at all times. The status bar's right
hand corner says `backend: mutter (Wayland)` or `backend: xrandr (X11)`, with the
full explanation in its tooltip, and **Layout ▸ Backend** changes it: Automatic, X11
(xrandr), sway, wlroots, GNOME (mutter), KDE (kwin), with the ones this session
cannot reach greyed out and the reason given. Picking one re-reads the screen through
it and redraws. If it cannot be reached you get the dialog and the previous one back,
never an empty window. The same spellings work on the command line, so a hotkey can
pin one.

Contract: [WARANDR.md](docs/WARANDR.md), including where the layout scripts go and how to
bind one to a key on each desktop.

### wmirror

The one tool here that clones nothing, because there is no X11 `wmirror` and no
`xrandr` syntax for what it does. On wlroots it mirrors **a region** of an output, or
a whole output onto a **differently shaped** one, by running the existing
[`wl-mirror`](https://github.com/Ferdi265/wl-mirror) (Ubuntu universe:
`sudo apt install wl-mirror`) and owning its lifetime.

```console
$ wmirror DP-1 --to HDMI-A-1                      # whole output, any shape
$ wmirror DP-1 --to HDMI-A-1 --region 800x600+300+200   # just that rectangle
$ wmirror --list
HDMI-A-1 <- DP-1  region 800x600+300+200  scaling fit  wl-mirror pid 40021
$ wmirror --stop HDMI-A-1
```

**It runs only where the layout cannot do the job.** Two outputs of the same size at
the same position already mirror on wlroots, byte identical, whole frame, measured,
so that stays the answer and wmirror sends you there:

```console
$ wmirror DP-1 --to DP-2
wmirror: DP-1 and DP-2 are both 1920x1080: the layout mirrors them byte for byte, with no helper and no cost
  wxrandr --output DP-2 --same-as DP-1
  --keep-layout runs wl-mirror anyway, so DP-2 keeps its own place in the layout
```

It also refuses, by name, what the measurements showed goes wrong: two outputs that
share pixels (a fullscreen mirror window on the target is drawn on the source too, so
the helper captures itself, and run on purpose both heads went entirely black), a
region that runs off the source (wl-mirror silently clamps it), a target that is
already mirroring, and two mirrors pointing at each other. A running mirror also ends
itself if the layout moves out from under it: either output unplugged or switched
off, the two brought on top of each other, or, for a region, its source moved or
resized, which would leave the same rectangle naming different pixels.

**What it costs**, and it says so up front: a resident process and a frame of latency
(median about 63 ms measured, at the rig's floor). An idle sway desktop repaints only
on damage, and a mirror asks for a frame every frame, so the desktop never idles
again while it lives, which was 88% of a software-rendered core in the test VM.
`wl-mirror` is invisible to output management, so `--query` cannot show it, but
`wmirror --list` verifies every pid it prints and `wmirror --stop` and `--stop-all`
end them. Nothing is left running that you cannot find and stop, including when
wmirror's own supervisor is killed.

**wlroots only.** `wmirror --check` says whether this session qualifies and what is
missing if it does not, and **(k)** in the support matrix is why GNOME and KDE cannot
have it.

Contract: [WMIRROR.md](docs/WMIRROR.md).

## Threat model

These are power tools: they exist to give a script the reach an X11 client always
had. That reach is the product, so the honest thing is to say exactly what it is, who
gets it, and what is not defended against.

**What the tools do by design.** `wdotool` injects keystrokes and pointer events as a
kernel level virtual device, which every application, your terminal, your password
prompt, the lock screen, receives as real hardware. Both halves have a second route
on wlroots, `zwp_virtual_keyboard_v1` for the keys and `zwlr_virtual_pointer_v1` for
the pointer, which are not kernel devices at all and reach the same places: measured,
an unprivileged client typed the account password into `swaylock` through the first
and, through the second, moved the cursor and clicked with no root, no group
membership and no device rule. On that family of compositors **nothing wdotool
injects needs a privilege of any kind**. `wwmctl`, `wxprop` and `wxrandr` read and
change window and display state through the compositor. Anything you can do at the
keyboard, a script running as you can do through these tools. That is the whole
point, and it is not a vulnerability.

**What installing the pieces grants, and to whom.**

* **The GNOME bridge extension** grants **every process that can reach your session
  bus**, including a sandboxed app allowed to talk to `org.gnome.Shell`, because the
  object answers there too, the ability to list every window with its title, class,
  pid, geometry and workspace (stock GNOME withholds that:
  `org.gnome.Shell.Introspect` is sender-allowlisted), to move, resize, restack,
  close and **SIGKILL** any window, to learn `DISPLAY` and the path of Mutter's
  Xwayland cookie, to take the shell's modal input grab for the length of one window
  pick, and to confirm a pending display-configuration change. There is no partial
  mode and no caller check. The bridge never evaluates code and never injects input.
* **The udev rule** grants `/dev/uinput`, which is the ability to type as you, to the
  user of the **active seat session**, through a logind ACL, and to nobody else: no
  group, no standing channel. The grant is checked at `open()`, so the daemon
  re-checks it before every injection and destroys its devices when the seat moves to
  another session, and a user who switches away therefore stops being able to type
  into the session they left. That is about the *kernel* device, which is global to
  the machine. On wlroots the Wayland route still works while the seat is elsewhere,
  because it reaches only the compositor whose socket it connected to, which is your
  own.
* **`zwp_virtual_keyboard_v1` and `zwlr_virtual_pointer_v1` on wlroots grant nothing
  that was not already granted**, and that is the note. sway advertises both
  protocols to **every client of your Wayland socket** and restricts them to none:
  any of them could already upload a keymap and type as you, or move your cursor and
  click, with or without us. wdotool installs nothing to use them and asks nobody for
  permission. It is the compositor's grant, to everything that can open your
  compositor's socket, which is the same-uid boundary below. Two consequences worth
  spelling out: on sway, injecting input needs neither root nor the udev rule at all,
  and the lock-screen note below applies to these routes as much as to the kernel
  one. Mutter and KWin 6.6.6 implement neither, so nothing changes there.
* **KDE needs nothing installed**, which is itself the note: any client of a Plasma
  session bus can already load a script into KWin, with or without us.
* **Running as root** (`sudo wdotool`) is the alternative to the udev rule. Then the
  tools find the graphical session by scanning `/run/user/*` and logind, and talk to
  that user's compositor as root.

**What is never asked at run time.** Nothing here uses the desktop portal, so GNOME's
and KDE's *Remote Desktop* consent dialog never appears, and nothing here uses
PolicyKit, so no polkit agent window does either. Measured on GNOME 46 and 50 and on
Plasma 5.27, 6.6 and 6.7, and held there by `tests/test_no_portal.py`, see
[No authorization dialog](#no-authorization-dialog). That is a deliberate choice, and
this section is its cost: with no per-use prompt, everything is granted once and
standing, by the bullets above, whether that is the udev rule, the bridge extension
or `sudo`. The only prompt any of it can raise is GNOME's *Keep these display
settings?*, on an explicit `wxrandr --persistent`.

**What is deliberately not defended against.** Anyone who can already run code as
you: they can type through the daemon, read the same files and talk to the same
buses, and a same-uid boundary is not one we can enforce, so we do not pretend to. A
hostile compositor (you are already inside it). The lock screen: injected keystrokes
reach it, because the kernel does not know they are injected, so do not install the
udev rule on a machine where someone else has physical access to the keyboard while
you are away. Scripts you saved and run later (`warandr`'s layout scripts are shell
scripts, so read one before running it, as with any script). And nothing here is a
sandbox: the tools do not confine what a command they hand over to, `xdotool` on X11,
then does.

**What is defended against**, and stays that way: another local user. The daemon
socket, its lock and the wxrandr state file are private to their owner and validated
before they are believed, the daemon refuses to talk to a socket somebody else is
listening on, a state file that is not ours is ignored rather than obeyed, the
real-tool search never looks in the current directory, and a root run with no session
never hands a planted X server another user's cookie.

**If you want less exposure:** don't install the bridge extension or the udev rule.
Run the tools under `sudo` when you need them, which grants nothing standing to
anybody.

## Releases

The long form of each release, with the measurements behind it, is
[CHANGELOG.md](CHANGELOG.md).

<!-- release-notes: 0.2 -->
### 0.2

Six tools, and on sway nothing wdotool injects needs a privilege at all. The pointer
joined the keyboard on `zwlr_virtual_pointer_v1`. What each desktop does with a
layout after you set it was measured through a hotplug and a reboot on all four.
Plasma over Xorg is handled as the X11 session it is, on both generations. KWin 6.7
changed how it publishes outputs and `wxrandr` follows it. And **wmirror** arrived:
region and odd-shape mirroring on wlroots, the two pictures output geometry alone
cannot produce.

<!-- release-notes: 0.1 -->
### 0.1

The first tagged release: five tools on GNOME 46 and 50, KDE Plasma 5.27 and 6.6,
sway and the wlroots family, and any X11 session, where they hand over to the
originals rather than pretending. A GNOME Shell bridge extension, KWin scripting,
Mutter and KDE display configuration, non-US keyboard layouts read from the
compositor's own keymap, `wdotool keys`, and an install guide written by doing it and
then re-run verbatim on fresh images of four desktops.

## Testing

Developed against real desktops, not against a model of them. `vm/` is the rig:
`vmctl` builds and runs twelve golden images, GNOME, KDE Plasma, Xfce and sway on
Ubuntu 24.04, 26.04 and 26.10 built from cloud images, plus one Ubuntu 26.04 and one
Ubuntu 24.04 desktop **installed from the release ISOs by the Ubuntu installer**,
each with up to four virtual monitors that can be plugged, resized and unplugged from
outside the guest, and every head screenshotted. `vm/selftest.sh <flavor>` proves the
rig itself rather than the tools. `vm/README.md` documents the whole thing, including
what the tools do on each flavor, and [`vm/SETUP.md`](vm/SETUP.md) is how to set the
rig up on a machine of your own, with `vm/setup-host.sh` doing the mechanical part.

`tests/` holds the suite, 2260 tests: unit tests, wire-level fake compositors and X
servers, live-compositor integration, hostile-input torture, byte-parity oracles
against the real xdotool, wmctrl, xprop and xrandr, and one static check that no
package ever reaches for the desktop portal or PolicyKit.

Every line of this repo was written by AI (Claude): the design contracts, the code,
the torture rigs, the hostile fake X servers, the byte-parity oracles, the VM demo,
this README, and yes, the meme. Also fully awesome: live-compositor integration
suites, byte for byte output parity against the real tools with verbatim bugs
included, and every "it works" claim proven inside a real Ubuntu VM before it
shipped. Vibe-check the code yourself, it can take it.

## The rest of the documents

| file | what it is |
|---|---|
| [WDOTOOL.md](docs/WDOTOOL.md) | wdotool's reference: the 48 commands, layouts, the two injection paths, the daemon, the backends |
| [WWMCTL.md](docs/WWMCTL.md) | wwmctl's contract: the dual-plane trick, the wmctrl surface, GNOME and KDE |
| [WXPROP.md](docs/WXPROP.md) | wxprop's contract: the two planes, the formatting machine, GNOME and KDE |
| [WXRANDR.md](docs/WXRANDR.md) | wxrandr's contract: the backends, overlaps, keeping a layout, the command surface |
| [WARANDR.md](docs/WARANDR.md) | warandr's contract: backend selection, the model, layout scripts, the GUI |
| [WMIRROR.md](docs/WMIRROR.md) | wmirror's contract: why it exists, the policy, lifetime, what it costs |
| [Technical.md](docs/Technical.md) | how the tree is put together, for whoever changes it next |
| [Blogpost.md](docs/Blogpost.md) | the long story: what X11 got right, four compositors with four answers, and what the measurements found |
| [CHANGELOG.md](CHANGELOG.md) | the long form release notes |
| [gnome/README.md](gnome/README.md) | the bridge extension's own interface and its live verification |
| [vm/README.md](vm/README.md) | the rig: twelve flavors, what each one is, and what the six tools do on it |
