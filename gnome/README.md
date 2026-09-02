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
| `WindowAt` | `(i x, i y) → t` | topmost non-desktop window showing on the active workspace whose frame contains the point, `0` if none |

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
| `GetPointer` | `() → (iiu)` | the real pointer and Clutter modifier mask |
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
  so the bridge vanishes while the screen is locked. Add
  `"session-modes": ["user", "unlock-dialog"]` to `metadata.json` if a test
  rig needs it alive there.
* No `Eval`, no input injection, no screenshots: the bridge is the
  window-management plane only.

## `/dev/uinput` without root (`--udev`)

Input injection (`wdotool key/type/click/mousemove`) goes through
`/dev/uinput`, which stock Ubuntu ships as `root:root 0600`. Instead of
running everything as root or adding yourself to the `input` group and
logging out, install one udev rule:

```sh
sudo sh gnome/install-bridge.sh --udev
```

copies `gnome/60-fuckwayland-uinput.rules` to `/etc/udev/rules.d/` and
`gnome/modules-load-uinput.conf` to `/etc/modules-load.d/fuckwayland-uinput.conf`,
loads the module, reloads udev and re-triggers the device:

```
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess", MODE="0660", GROUP="input"
```

* `TAG+="uaccess"` makes systemd-logind put an ACL for the user of the
  **active** seat session on the node (the same mechanism that gives you
  your sound card and webcam). `udevadm trigger --name-match=uinput`
  re-runs the rule immediately, so the currently logged-in user gets the
  ACL **without logging out**; logind re-applies it at every login and VT
  switch.
* `OPTIONS+="static_node=uinput"` has udev create `/dev/uinput` at boot
  from the module's devname alias, with these permissions and tags, even
  when the module is not loaded yet (the first `open()` autoloads it). The
  `modules-load.d` file loads it at boot anyway so nothing depends on the
  autoload path.
* `MODE="0660", GROUP="input"` keeps the classic route (membership in
  `input` + relogin) for sessions logind does not manage (ssh, containers).

Check with `getfacl /dev/uinput` — expect a `user:<you>:rw-` line while your
session is active — and `sh gnome/install-bridge.sh --check`, which reports
the rule, the module and the ACL. Security-wise, whoever can open
`/dev/uinput` can type as you; the rule limits that to the physically
logged-in user, which is the X11 status quo.

## Uninstall

```sh
sh gnome/install-bridge.sh --uninstall           # or --uninstall --system
```

disables the extension in the running shell, removes it from
`enabled-extensions` and deletes the directory. The loaded copy stays in the
shell process until the next login.
