# fuckwayland bridge — the GNOME Shell extension

`fuckwayland-bridge@fuckwayland` is a small GNOME Shell extension that exports
Mutter's window, workspace and monitor state — and the actions on them — over
the session D-Bus. It is what lets `wdotool`, `wwmctl`, `wxprop` and `wxrandr`
work on a stock GNOME Wayland session (Ubuntu 24.04 / GNOME 46 and Ubuntu 26.04
/ GNOME 50, plus everything in between).

## Why an extension

GNOME has no window-management protocol. Mutter does not implement
`wlr-foreign-toplevel-management`, `ext-foreign-toplevel-list` carries no
geometry and no actions, and everything gnome-shell offers on D-Bus is either
read-only and sender-allowlisted (`org.gnome.Shell.Introspect`) or switched off
by default (`org.gnome.Shell.Eval` returns `(false, '')` unless the shell is in
"unsafe mode", which nothing persists). xdotool's author walked through the
same dead ends in
[Exploring the Fragmentation of Wayland, an xdotool adventure](https://www.semicomplete.com/blog/xdotool-and-exploring-wayland-fragmentation/):
on Wayland the compositor decides what a tool may know and do, and on GNOME the
only supported way to ask is code running inside the shell. So that is what
this is: about 1100 lines of ESM JavaScript that run inside gnome-shell and
answer D-Bus calls with JSON.

The Python tools talk to it with a stdlib D-Bus client; no `gi`, no `gdbus`
spawns on the hot path.

## Files

```
gnome/
  fuckwayland-bridge@fuckwayland/
    metadata.json                 uuid, shell-version ["45".."50"]
    extension.js                  the extension (ESM)
    org.fuckwayland.Bridge1.xml   introspection XML (also embedded in extension.js)
  install-bridge.sh               POSIX sh installer / checker / uninstaller (+ --udev)
  60-fuckwayland-uinput.rules     udev rule: /dev/uinput for the active seat user (uaccess)
  modules-load-uinput.conf        loads uinput at boot (installed with the rule)
  README.md                       this file
```

The Python side is `wdotool/backend_gnome.py` (a `dbus_mini` client of this
interface; every backend method maps to one call) and
`tests/test_backend_gnome.py` (a mock bridge on the in-process mock bus).

## Install

```sh
sh gnome/install-bridge.sh            # ~/.local/share/gnome-shell/extensions/<uuid>
sh gnome/install-bridge.sh --system   # /usr/share/gnome-shell/extensions/<uuid> (sudo)
sh gnome/install-bridge.sh --check    # is it loaded? is org.fuckwayland.Bridge owned? udev rule?
sh gnome/install-bridge.sh --uninstall
sudo sh gnome/install-bridge.sh --udev  # /dev/uinput for the logged-in user (see below)
```

The script copies the files, adds the uuid to `org.gnome.shell
enabled-extensions` (`gnome-extensions enable`, or gsettings by hand), and then
tries to make it live without a logout:

1. If the running shell already knows the uuid (`ListExtensions` lists it —
   true after any login with the files in place) it calls `EnableExtension`
   and the bridge is up immediately.
2. Otherwise you will be told to **log out and back in once**. gnome-shell
   scans extension directories only at login and, on Wayland, cannot be
   restarted in place (Alt+F2 `r` only ever worked on X11 and is gone in 50).
   The extension is already enabled in gsettings, so it comes up by itself
   after the re-login.
3. `--try-unsafe` is the no-logout escape hatch: it drives Looking Glass
   (Alt+F2, `lg`, `global.context.unsafe_mode = true`) through `wdotool`'s
   input injection, loads the extension with `org.gnome.Shell.Eval`, enables
   it, and switches unsafe mode off again. It needs `wdotool` with working
   uinput access (root, or the udev rule from the main README), an unlocked
   session and five seconds of not touching the keyboard. It is best-effort by
   nature; if it fails nothing is left behind except the files and the
   gsettings entry, and the next login finishes the job.

Running the installer through `sudo` is fine: it figures out the desktop user
from `$SUDO_USER`, chowns the files to them and does every shell/gsettings
call as that user on `/run/user/<uid>/bus`.

Ubuntu ships `org.gnome.shell disable-user-extensions = false` and
`allow-extension-installation = true`; `--check` prints both in case a site
policy changed them. Upgrading an already-loaded extension still needs a
re-login (the ESM module cache is per process).

Debugging: `journalctl --user -f -o cat _COMM=gnome-shell | grep -i
fuckwayland`. Enable/disable, bus-name changes and unexpected (`.Failed`)
errors are logged at message level and always show up; expected errors
(`NotFound`, `InvalidArgs`, `Unsupported`) and per-call tracing are at debug
level and need `G_MESSAGES_DEBUG=all` in the shell's environment. `--check`
prints the extension state and any load error.

## Security note

Anything that can connect to your session bus can list, move, close, kill and
focus your windows through this bridge. That is exactly the situation on X11,
where any client of the display can do all of that and more (read keystrokes,
grab the screen), and it is the deliberate trade: the tools exist to give
scripts that power back. Sandboxed applications (Flatpak/Snap) without session
bus access cannot reach it. The bridge never evaluates code and never
injects input; input goes through the kernel (`/dev/uinput`), not the shell.

If you do not want any process on the bus to have this power, do not install
the extension — there is no partial mode.

## The interface

Bus name `org.fuckwayland.Bridge` (the object also answers to
`org.gnome.Shell`, because it lives on gnome-shell's own connection), object
path `/org/fuckwayland/Bridge`, interface `org.fuckwayland.Bridge1`.
Structured results are JSON strings; every method is wrapped so a broken call
returns a D-Bus error instead of taking the shell down.

```sh
gdbus call --session --dest org.fuckwayland.Bridge --object-path /org/fuckwayland/Bridge \
  --method org.fuckwayland.Bridge1.ListWindows | python3 -c 'import ast,json,sys; print(json.dumps(json.loads(ast.literal_eval(sys.stdin.read())[0]), indent=1))'
```

### Windows

| Method | Signature | Does |
|---|---|---|
| `ListWindows` | `() → s` | JSON array, bottom-to-top stacking order, all managed windows except override-redirect ones. Object layout below. |
| `GetWindow` | `(t id) → s` | one such object; `NotFound` for unknown ids |
| `Activate` | `(t)` | `activate(ts)`: switch workspace, unminimize, raise, focus (xdotool `windowactivate`) |
| `Focus` | `(t)` | `focus(ts)` without raising when the window is showing on the active workspace, otherwise `Activate` (xdotool `windowfocus`) |
| `Close` | `(t)` | `delete(ts)` — polite close |
| `Kill` | `(t)` | `kill()` — Mutter kills the client (works for XWayland and native, as any user) |
| `Minimize` / `Unminimize` | `(t)` | |
| `Raise` / `Lower` | `(t)` | |
| `Move` | `(t, i x, i y)` | `move_frame(true, x, y)`; frame coordinates, logical pixels |
| `Resize` | `(t, i w, i h)` | `move_resize_frame` keeping the frame's top-left |
| `MoveResize` | `(t, i, i, i, i)` | |
| `SetState` | `(t, s state, s action) → b` | `state` ∈ `FULLSCREEN MAXIMIZED_HORZ MAXIMIZED_VERT MAXIMIZED HIDDEN ABOVE BELOW STICKY DEMANDS_ATTENTION SHADED SKIP_TASKBAR SKIP_PAGER MODAL`, `action` ∈ `add remove toggle`. `MAXIMIZED_HORZ`/`_VERT` are real per-axis operations on every release. Returns `false` (and does nothing) for what Mutter cannot set: `SHADED`, `SKIP_*`, `MODAL`, `BELOW`. Unknown ids still raise `NotFound`. |
| `MoveToWorkspace` | `(t, i index)` | `change_workspace_by_index`; `index < 0` = stick to all workspaces (EWMH 0xFFFFFFFF); `NotFound` for a missing workspace |
| `SelectWindow` | `(u timeout_ms) → t` | resolves with the next window that gains focus (the user must focus a *different* window, like sway), `0` on timeout; `timeout_ms = 0` waits forever. Set your D-Bus call timeout above `timeout_ms`. |

There is no hit-test method on purpose: `getmouselocation`'s window is
computed client-side from `ListWindows` (`GnomeBackend.window_at()`, looking
through `DESKTOP`/`DOCK` windows) with the focused-first/topmost rule every
backend shares, so the two cannot drift apart.

Window object (`ListWindows`/`GetWindow`):

```
id                 Meta.Window.get_id() (64-bit, stable for the shell's lifetime; the
                   same number org.gnome.Shell.Introspect uses). Every t argument above.
xid                X11 client window id for XWayland windows, 0 for native ones
title, wm_class, wm_class_instance, gtk_app_id, sandboxed_app_id, desktop_id, role
pid                0 when unknown
client_type        "x11" | "wayland"
window_type        Meta.WindowType name: NORMAL DESKTOP DOCK DIALOG MODAL_DIALOG ...
x y width height   get_frame_rect(): logical pixels, no CSD shadows, SSD frame included
buffer_rect        {x,y,width,height} of the full buffer (shadows included)
focused minimized hidden on_all_workspaces on_active_workspace
                   hidden = !showing_on_its_workspace()
workspace          index, -1 when on all workspaces
monitor            index into ListMonitors
fullscreen maximized_h maximized_v above urgent skip_taskbar decorated
transient_for      id or 0
stable_sequence    get_stable_sequence() (creation counter)
```

### Workspaces

| Method | Signature | Does |
|---|---|---|
| `GetActiveWorkspace` | `() → i` | |
| `SetActiveWorkspace` | `(i)` | `activate(ts)`; `NotFound` beyond the last one |
| `GetNWorkspaces` | `() → i` | with dynamic workspaces this counts the trailing empty one |
| `SetNWorkspaces` | `(i)` | append/remove and update `num-workspaces`; error `Unsupported` while `org.gnome.mutter dynamic-workspaces` is on (the default) |
| `ListWorkspaces` | `() → s` | `[{index, name, active, work_area:{x,y,width,height}, viewport:{x:0,y:0}}]` — what `wmctrl -d` prints |
| `ShowDesktop` | `(b)` | `true`: minimize every normal window on the active workspace and remember them; `false`: unminimize those. Mutter's real show-desktop mode has no public API. |

### Screen

| Method | Signature | Does |
|---|---|---|
| `DisplaySize` | `() → (ii)` | `global.display.get_size()` — the whole layout |
| `GetPointer` | `() → (iiu)` | the real pointer and Clutter modifier mask. Diagnostic only (`GnomeBackend.real_pointer()`, no command uses it): `getmouselocation` reports the daemon-tracked injected pointer by design, and this is how the two are checked against each other |
| `ListMonitors` | `() → s` | `[{index, x, y, width, height, scale, primary, connector}]`; `connector` is filled on GNOME 49+ (`MonitorManager.get_monitors()` + `get_monitor_for_connector()`) and empty on 46 (match on x/y against `DisplayConfig.GetCurrentState` there) |
| `XInfo` | `() → (ss)` | gnome-shell's own `DISPLAY` and `XAUTHORITY` — how a root caller finds Xwayland and its cookie file. When the shell has no `XAUTHORITY`, the newest `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` is returned instead; `""` means unknown |
| `ConfirmDisplayChange` | `(b keep) → b` | best-effort: finds the "Keep these display settings?" dialog and presses Keep/Revert; returns `false` when no dialog was found (the verdict is still forwarded to Mutter, which ignores it when nothing is pending) |

### Misc, signals, errors

* `GetVersion() → u` and read-only property `Version` (`u`), both `1`.
  (GJS resolves method and property names on the same object, so the method
  could not also be called `Version`.)
* Signal `WindowEvent(t id, s change)` with sway's vocabulary: `new`, `close`,
  `focus` (id `0` = nothing focused), `title`, `fullscreen_mode`, `move`
  (position or size), `urgent`, `workspace` (also when stickiness changes),
  plus `minimized`.
* Signal `WorkspaceEvent(s change)`: `switch`, `add`, `remove`.
* Errors: `org.fuckwayland.Bridge1.NotFound` (bad window/workspace id),
  `.Unsupported`, `.InvalidArgs`, `.Failed` (anything else, message included).

## Verified live

Ubuntu 24.04.4 / GNOME Shell 46.0 (gnome-shell 46.0-0ubuntu6~24.04.14, gjs
1.80.2, Xwayland 23.2.6), fresh autologin session, xterm + gnome-text-editor +
gnome-calculator: user install → "log out and back in" → after the reboot
`--check` shows state 1 / name owned / version 1 and the journal only has
`enabled`/`acquired` lines (no JS ERROR). Through `wdotool`: `search`
(name/class/classname/pid/desktop/onlyvisible), `getactivewindow`,
`getwindow{name,classname,pid,geometry}`, `windowactivate --sync` (switches
workspace, unminimizes), `windowfocus`, `windowmove/windowsize --sync`
(xterm snaps to its cell grid: 640x480 → 640x470), `windowstate`
FULLSCREEN / MAXIMIZED_VERT (per-axis: 500x768 at y=32) / ABOVE / STICKY /
DEMANDS_ATTENTION (SKIP_TASKBAR warns; SHADED and BELOW error), `windowminimize` /
`windowmap --sync`, `set_desktop[_for_window]`, `get_desktop_for_window` (-1
when sticky), `getmouselocation` window hit-test, `windowraise/lower`,
`windowclose`, `windowkill`, `type`/`key` into xterm, `selectwindow` (returns
on the next focus change). As `root` over ssh with no session environment the
same commands work (bus found next to `wayland-0`, dbus_mini's fork auth,
`x_info()` = `(':0', '/run/user/1000/.mutter-Xwaylandauth.*')`). The
installer's live paths: re-run while loaded → "is live", `--uninstall` →
immediate "bridge not running" error from the tools, re-install → live again
without a logout.

Ubuntu 26.04 / GNOME Shell 50.1 (gnome-shell 50.1-0ubuntu1.2, gjs 1.88.0,
Xwayland 24.1.10, kernel 7.0, sudo-rs 0.2.13): the identical run passes,
including per-axis `MAXIMIZED_VERT` through `set_maximize_flags()` (500x768
at y=32), `ListMonitors.connector = "Virtual-1"` (the `get_monitors()` +
`get_connector()` route), `XInfo` = `(':0', '/run/user/1000/.mutter-Xwaylandauth.*')`,
and the installer under sudo-rs. Extra checks there: 200 `ListWindows` calls
take 0.21 s wall in total (~1 ms each) with gnome-shell at 366 MB RSS;
**lock screen**: `loginctl lock-session` → the journal shows `disabled` and
`org.fuckwayland.Bridge` is gone (the tools say "bridge is unavailable while
GNOME Shell is in 'unlock-dialog' mode"), unlock → `enabled`/`acquired` again
within a second; **`--try-unsafe`**: after `--uninstall` + reboot (shell has
never seen the uuid), `WDOTOOL=... sh gnome/install-bridge.sh --try-unsafe`
run as the plain user (uinput via the udev ACL) drove Looking Glass, loaded
and enabled the extension without a logout, and left `Eval('1+1')` at
`(false, '')` — unsafe mode off again; `--check` state 1 afterwards.
`getmouselocation` after `mousemove 400 300` matches the bridge's
`GetPointer` exactly, i.e. the injected tablet pointer and Mutter's frame
rects share one coordinate space.

X ids (TV-1, on 50.1): the bridge's `xid` for xterm (`0x60000c`) is the
entry in `xprop -root _NET_CLIENT_LIST` and `wmctrl -l`; `xprop -id` on it
gives `WM_CLASS = "xterm", "XTerm"` (= `wm_class_instance`/`wm_class`),
`_NET_WM_PID` = `pid`, `_NET_WM_NAME` = `title`, and `_NET_WM_DESKTOP`
follows `set_desktop_for_window` (1) and `STICKY` (`0xffffffff`,
`_NET_WM_STATE_STICKY`). Note that on 26.04 even the desktop user needs
`XAUTHORITY=$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` to talk to Xwayland
(`XInfo`/`session.find_xauthority()` return exactly that file).

Not exercised live yet: `ConfirmDisplayChange` (no display change was
triggered) and the Looking-Glass probes of §6 of the checklist.

Review fixes re-verified on Ubuntu 24.04 / GNOME Shell 46.0 (fresh instance,
systemd 255): the uaccess-only rule gives `root:root 0600` plus
`user:test:rw-` right after `--udev` and again right after autologin on the
next boot, `wdotool mousemove` works as the plain user; `--udev` over the
earlier `GROUP="input"` rule prints "resetting the node" and ends in the
same state; `--udev --uninstall` leaves `root:root 0600`, no ACL, no udev
tags, and repeated `udevadm trigger --name-match=uinput` keep it that way
(`open('/dev/uinput')` as the user → `EACCES`, `wdotool` prints its
run-as-root hint). `windowstate --add/--toggle/--remove SHADED` → rc 1
"Mutter does not implement window shading", `SKIP_TASKBAR`/`MODAL` still
warn with rc 0, `BELOW` rc 1. `getmouselocation`'s window is unchanged with
`WindowAt` gone (`gdbus call … WindowAt` → `UnknownMethod`), `GetPointer`
answers. With the extension disabled, `dbus-monitor` sees no `Eval` from the
tools by default and exactly one with `WDOTOOL_GNOME_AUTOLOAD=1` (refused
outside unsafe mode, same "installed but not enabled" message either way);
`loginctl lock-session` → "unavailable while the screen is locked" (`Mode`
stays `ubuntu` on 46, `ScreenSaver.GetActive` is `true`), unlock → working
again. Journal clean throughout.

## GNOME 46 vs 50 and other honest limits

* **Partial maximization** works on every release: GNOME 46–48 take the
  directions in `maximize(flags)`/`unmaximize(flags)`, 49+ in
  `set_maximize_flags(flags)`/`set_unmaximize_flags(flags)` (plain
  `maximize()` there is just "both"). Reading goes through the
  `maximized-horizontally/-vertically` properties everywhere.
* **Moving/resizing a maximized, fullscreen or tiled window** is silently
  constrained by Mutter, as it is on X11. Unmaximize first if you mean it.
* **`xid`** is `global.display.get_x11_display().lookup_xwindow(window)`
  (public in 46 and 50; `meta_window_get_xwindow` does not exist) for
  windows whose `get_client_type()` is X11; the leading hex number of
  Mutter's window description (`0x<xid>` on 46, `0x<xid> (title)` on 50) is
  the fallback. Wayland-native windows get `0`.
* **`connector`** in `ListMonitors` needs `MonitorManager.get_monitors()` and
  `Meta.Monitor.get_connector()` (Mutter 49+); on 46 it is `""`.
* **Lock screen**: extensions run only in the `user` session mode by default,
  so the bridge vanishes while the screen is locked (verified: `disabled` on
  lock, `enabled` on unlock; the tools report the mode). Add
  `"session-modes": ["user", "unlock-dialog"]` to `metadata.json` if a test
  rig needs it alive there.
* No `Eval`, no input injection, no screenshots: the bridge is the
  window-management plane only. The Python side never touches
  `org.gnome.Shell.Eval` either, unless `WDOTOOL_GNOME_AUTOLOAD=1` is set —
  then, with the shell already in unsafe mode, the backend loads an installed
  but not-yet-loaded copy of the extension on first use instead of asking for
  a re-login.
* **Hotkeys**: do not bind scripts to `Ctrl+Alt+F1`…`F12` on GNOME Wayland.
  Mutter's native backend owns those chords as VT switches
  (`switch-to-session-N`), gsd-media-keys cannot grab them (`Failed to grab
  accelerator` in the journal) and injecting one switches the console — the
  session keeps running on the VT you just left (`chvt N` brings it back).
  `<Ctrl><Super>F7` and the like work fine (verified: custom keybinding →
  script → `wdotool search … windowactivate type`).

## Known limitations on GNOME

Found by stress-testing the tools against real GNOME 46 (24.04) and GNOME 50
(26.04) sessions on a three-head 5760x1080 rig. These are the things that
behave differently from xdotool on X11 and that no amount of code in this
repo can fix; the bugs that *were* fixable have been.

**Keyboard**

* **L1 — the injected keyboard is a US-QWERTY keyboard.** `key`/`type` send
  raw evdev keycodes, and the compositor interprets them through the
  session's *active* layout. With a `de`, `fr` or Dvorak layout active, even
  plain ASCII comes out wrong: `type y` produces `z` on a German layout,
  `key ctrl+z` reaches the application as `ctrl+y`. There is no protocol to
  ask a Wayland compositor to type a character. Set the layout to `us` for
  the duration of a script (`gsettings set org.gnome.desktop.input-sources
  sources "[('xkb','us')]"`), or drive applications by keysym-independent
  means. This is the single biggest behavioural difference from xdotool.
* **L2 — `type` skips characters that are not on the US layout**, with one
  warning per character ("Can't type character 'é' … Skipping."). Same cause
  as L1.
* **L7 — `--clearmodifiers` releases but does not restore.** X11 lets
  xdotool read the modifier state, clear it and put it back; Wayland has no
  way to read it, so wdotool releases all eight modifier keys and leaves
  them released. A modifier a *user* is physically holding stays logically
  released until they release and press it again.
* **L11 — a held `keydown` autorepeats.** The compositor applies its own
  key-repeat to an injected key held down, exactly as it does for a physical
  key. `keydown a; sleep 2; keyup a` types a row of `a`s. xdotool on X11
  behaves the same way; scripts that want one keystroke should use `key`.
* **L12 — injected hotkey chords race the program they launch.** A chord
  injected with `key super+1` is delivered to the shell, which starts the
  bound application; if the script goes straight on to `type`, the modifier
  release and the new keystrokes can interleave with the launch and the
  shell can see stray `Super+<digit>` presses. Put a `search --sync` on the
  new window between the chord and the typing.

**Pointer**

* **L3 — `getmouselocation`'s `window` field is a Mutter window id**, the
  same 64-bit number every other wdotool command uses — not an X11 window
  id, even for XWayland clients, and `0` when the pointer is over the
  desktop, the top bar or a dock. Piping it into `xprop -id` does not work;
  use `wxprop -id`, which speaks the same ids.
* **L13 — `click --window W` activates W but does not move the pointer.**
  There is no send-event on Wayland: the click is a real button press
  wherever the pointer happens to be. Move the pointer first
  (`mousemove --window W x y click 1`) when the position matters.

**Windows and the session**

* **L5 — shell grabs eat injected input.** While the Activities overview,
  the run dialog (`Alt+F2`) or the print/file dialogs of the shell itself
  have a keyboard grab, injected keys and buttons go to the grab, not to the
  window `getactivewindow` reports — and `windowactivate` does not dismiss
  it. An injected `super` needs *two* Escapes to close the overview
  afterwards. Check for and dismiss the overview before a scripted run.
* **L6 — the lock screen and the greeter.** Behind the lock screen the
  bridge extension is not running, so every window/workspace command fails
  fast with rc 2 ("no Wayland session found"), and on the GDM greeter the
  backend says so explicitly. `type`, `key` and `mousemove` still inject:
  `/dev/uinput` does not care that the screen is locked, and neither does
  the compositor. Treat a machine where anyone can reach `/dev/uinput` as a
  machine where anyone can type into the lock screen.
* **L8 — `selectwindow` returns on the next focus *change*.** Mutter offers
  no click-to-select-a-window primitive, so the bridge waits for the next
  focus change instead of a click. Clicking the window that already has
  focus never returns; click another window first.
* **L9 — `--sync` on a maximized, fullscreen or modal-attached window.**
  Mutter constrains moves and resizes of such windows and silently refuses
  them, exactly as an X11 window manager does, so the size or position never
  changes. Since the fix for B3 every `--sync` wait is bounded (10 s by
  default, `WDOTOOL_SYNC_TIMEOUT` overrides it): the command now prints
  `wdotool: gave up waiting for …` and exits 1 instead of hanging for ever.
  Unmaximize first if you mean it.
* **L10 — `windowsize --usehints` is pixels.** Size hints
  (`WM_NORMAL_HINTS` increments — a terminal's character cell) are not
  exported by Mutter for Wayland clients, so `--usehints` warns once and
  interprets width/height as pixels. Note that Mutter *does* still snap the
  result to the client's grid: asking an xterm for 497x392 leaves it at
  496x392. `windowsize --sync` accepts that snapped size as the answer.
* **L4 — one daemon per uid *and* per runtime dir.** The input daemon's
  socket is `$XDG_RUNTIME_DIR/wdotool.sock`, `/run/wdotool.sock` for root,
  and `/tmp/wdotool-<uid>.sock` when there is no `XDG_RUNTIME_DIR` at all —
  so a cron job without a runtime dir gets a *different* daemon from the one
  the desktop session uses, with its own pointer model. Since the fix for B6
  the models are reconciled from the compositor on GNOME (`getmouselocation`
  and `mousemove_relative` both ask Mutter first), so the split no longer
  moves the pointer to the wrong place; it still means two sets of virtual
  input devices. Export `XDG_RUNTIME_DIR=/run/user/$(id -u)` in cron jobs.

## `/dev/uinput` without root (`--udev`)

Input injection (`wdotool key/type/click/mousemove`) goes through
`/dev/uinput`, which stock Ubuntu ships as `root:root 0600` with no udev rule
of its own. Instead of running everything as root or adding yourself to the
`input` group and logging out, install one udev rule:

```sh
sudo sh gnome/install-bridge.sh --udev             # install rule + modules-load, apply now
sudo sh gnome/install-bridge.sh --udev --uninstall
sh gnome/install-bridge.sh --check                 # ...also prints the rule/driver/ACL state
```

`--udev` copies `gnome/60-fuckwayland-uinput.rules` to `/etc/udev/rules.d/`
and `gnome/modules-load-uinput.conf` to
`/etc/modules-load.d/fuckwayland-uinput.conf`, runs `modprobe uinput`,
`udevadm control --reload` and `udevadm trigger --name-match=uinput`:

```
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"
```

* `TAG+="uaccess"` makes systemd-logind put an ACL for the user of the
  **active** seat session on the node (the mechanism that hands you your
  sound card and webcam). The `udevadm trigger` re-runs the rule on the
  existing node, so the user logged in right now gets the ACL **without
  logging out**; logind re-applies it at every login and VT switch.
* `OPTIONS+="static_node=uinput"` has udev create `/dev/uinput` at boot from
  the module's devname alias, with these permissions and tags, on kernels
  where uinput is a module that is not loaded yet (the first `open()`
  autoloads it). The `modules-load.d` file loads it at boot anyway so nothing
  depends on the autoload path.
* Deliberately **no `MODE`/`GROUP`**: the node stays `root:root 0600` plus
  the ACL, so only the user at the seat can inject input. An earlier
  revision added `MODE="0660", GROUP="input"` as a fallback for sessions
  logind does not manage; that hands every member of `input` (service
  accounts included) a standing keystroke-injection channel into the seat
  user's session whether or not they are logged in there, which is exactly
  what the uaccess tag avoids. Sessions without logind run the tools as root.

Verified on Ubuntu 24.04 (GNOME 46, kernel 6.8):

* uinput is **built into the Ubuntu kernel** (`modinfo -n uinput` →
  `(builtin)`), so `/dev/uinput` exists from boot and the "module must be
  loaded before login" worry (PLAN.md critique 11) does not arise there;
  `systemd-modules-load` simply ignores the builtin. The `static_node` /
  `modules-load.d` pieces are for kernels that build it as a module.
* Installing the rule inside a running session: `getfacl /dev/uinput`
  shows `user:test:rw-` immediately after `--udev` (the trigger did it), and
  `wdotool type` works as the plain user in the same session, no relogin.
* After a reboot the ACL is there right after autologin (`--check`: "uinput
  usable by test: yes (logind ACL)"), i.e. the rule present at boot is
  enough.
* `--udev --uninstall` puts the node back to `root:root 0600` with the ACL
  cleared, and makes that stick. udev tags are sticky in its database: with
  the rule merely deleted, the next `udevadm trigger` of the node still
  matched `73-seat-late.rules`' `TAG=="uaccess"` and brought the ACL back
  (observed on systemd 255, where `TAG-=` only drops the *current* tag and
  `TAG==` matches the sticky set). The uninstall therefore deletes the
  files, then the node's udev database entry and tag index links
  (`/run/udev/data/c10:223`, `/run/udev/tags/*/c10:223`, plus the
  `static_node-tags/uaccess/uinput` link logind would act on at the next
  session activation) so udev starts from scratch at the node's next event
  as after a boot, then `setfacl -b` + `chmod 0600`. Verified: repeated
  `udevadm trigger --name-match=uinput` afterwards leave `root:root 0600`,
  no ACL, no tags. Re-running `--udev` over the earlier `GROUP="input"`
  revision of the rule resets the node the same way before installing the
  new one.
* The tools' "bridge unavailable" diagnosis: `org.gnome.ScreenSaver
  .GetActive()` says whether the screen is locked (GNOME 46 keeps the
  shell's `Mode` property at the session mode while locked; 50 reports
  `unlock-dialog` there — both are handled), `Mode` = `gdm` means you
  reached the greeter's bus (nobody logged in). The unlocked session's mode
  is `ubuntu` on Ubuntu (`classic` in GNOME Classic), which is *not*
  treated as locked — a disabled extension there is reported as disabled.

Check with `getfacl /dev/uinput` — expect a `user:<you>:rw-` line while your
session is active (`ls -l` shows `root root` and `rw-rw----`: the group
column is the ACL mask, nobody is in that group). Security-wise, whoever can
open `/dev/uinput` can type as you; the rule limits that to the physically
logged-in user — not to a group — which is the X11 status quo.

## Uninstall

```sh
sh gnome/install-bridge.sh --uninstall           # or --uninstall --system
```

disables the extension in the running shell, removes it from
`enabled-extensions` and deletes the directory. The loaded copy stays in the
shell process until the next login.
