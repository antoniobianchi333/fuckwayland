# wwmctl — design contract

Drop-in `wmctrl` clone for Wayland, handling **both** native Wayland apps and legacy X
apps (XWayland) as first-class citizens. Lives in this repo beside wdotool and reuses
its machinery. Pure-stdlib Python, same rules as DESIGN.md (nix-only toolchain, no
home-dir installs, byte-parity output, no commits by agents).

## The dual-plane trick

On wlroots compositors the *compositor itself* is the X window manager for XWayland,
and `swaymsg -t get_tree` exposes XWayland clients with their **real X11 window id**
(node `"window"` field) plus `window_properties`. So:

- The **unified window list** comes from the compositor backend (reuse
  `wdotool.backend_detect/backend_sway` — do not fork them).
- **XWayland windows** are printed with their real X11 window ids (`0x%08x`), so other
  X tools (`xprop -id`, real wmctrl, old scripts) interoperate on the same ids. X-only
  data (WM_CLASS instance.class, WM_CLIENT_MACHINE, _NET_WM_PID fallback) is read
  straight from the XWayland server over a pure-stdlib X11 wire client. Name-setting
  (-N/-I/-T) uses X ChangeProperty for X windows.
- **Native Wayland windows** get their compositor node id as the printed id (collision
  with X ids is practically impossible — X ids live at 0x00400000+ resource bases; we
  also check X-plane matches first on `-i` lookups). class comes from app_id
  (`app_id.app_id`), hostname from uname, pid from the compositor.
- **Actions** (activate, close, move/resize, desktops, states) go through the
  compositor backend for both planes — it is authoritative for XWayland windows too.
  The X plane is for identity/properties, not for actions (wlroots' xwm honors some
  EWMH root messages, but the compositor route is strictly more reliable).
- No X server around (pure Wayland, xwayland disabled)? Everything still works;
  X-enrichment silently degrades (class comes from window_properties).
- **Machine column rule** (both planes): WM_CLIENT_MACHINE when the X plane can
  read it, else the local hostname (XWayland and Wayland clients are local by
  construction); "N/A" only when gethostname() itself fails. This is *more*
  filled-in than real wmctrl, which prints "N/A" whenever the property is
  missing — deliberate, since the hostname is always correct here.

## Files (all new; nothing outside these except pyproject/flake already done)

- `wwmctl/__init__.py`, `wwmctl/__main__.py` — done (skeleton).
- `wwmctl/cli.py` — wmctrl option parsing (plain getopt, combined flags), usage/help
  byte-parity, dispatch. Owner: Agent W.
- `wwmctl/core.py` — UWindow model, unified listing, selection (`-r STR`, `:ACTIVE:`,
  `:SELECT:`, `-i` ids, `-F` exact, `-x` class matching), actions, output formatting.
  Owner: Agent W.
- `wwmctl/x11_mini.py` — pure-stdlib X11 client. Owner: Agent X. API is FROZEN:

```python
class XUnavailable(Exception): ...

class X11Conn:
    def __init__(self, display: str | None = None): ...
        # $DISPLAY, else probe /tmp/.X11-unix; auth via XAUTHORITY/~/.Xauthority
        # (MIT-MAGIC-COOKIE-1) with graceful cookie-less fallback; XUnavailable if no server
    def root(self) -> int: ...
    def atom(self, name: str, only_if_exists: bool = False) -> int: ...
    def client_list(self) -> list[int]: ...          # _NET_CLIENT_LIST on root
    def get_prop_ints(self, win: int, name: str) -> list[int]: ...
    def get_prop_string(self, win: int, name: str) -> str: ...   # UTF8_STRING or latin-1
    def get_wm_class(self, win: int) -> tuple[str, str]: ...     # (instance, class)
    def get_client_machine(self, win: int) -> str: ...
    def get_pid(self, win: int) -> int: ...
    def get_geometry(self, win: int) -> tuple[int, int, int, int]: ...  # root-relative x,y,w,h
    def set_name(self, win: int, name: str, icon: bool, long_: bool) -> None: ...
    def send_root_message(self, win: int, type_name: str, data: list[int]) -> None: ...
        # ClientMessage fmt 32 to root, SubstructureNotify|SubstructureRedirect
    def close(self) -> None: ...
```

X11 wire notes for Agent X: unix socket `/tmp/.X11-unix/X<n>`; connection setup with
auth from XAUTHORITY (parse the binary xauth format; try matching + wildcard cookies,
then empty auth). Requests needed: InternAtom, GetProperty, ChangeProperty, SendEvent,
GetGeometry, TranslateCoordinates, QueryTree (fallback when _NET_CLIENT_LIST is
absent). Handle the 32-byte reply/event/error framing, big-requests not needed,
byte order 'l'. Keep it ~400 tight lines.

## wmctrl surface (byte-parity against wmctrl 1.07)

`-l` (with `-p` pid, `-G` geometry, `-x` class), `-d`, `-s N`, `-a/-c/-R <STR>`,
`-t N -r <STR>`, `-e G,X,Y,W,H -r <STR>`, `-b add/remove/toggle,P1[,P2] -r <STR>`,
`-N/-I/-T <STR> -r <STR>`, `-i`, `-F`, `-v`, `-m`, `-k on|off`, `-o X,Y`, `-n N`,
`-h`. Selection default: case-insensitive substring on title. Exact printf formats,
column widths, error strings, and exit codes come from the real wmctrl source + the
reference dumps (workflow stage 1 produces both; sandbox devshell has the real
`wmctrl` binary). Desktop semantics map exactly like wdotool's desktop commands
(sway workspaces, 0-based). `-k`/`-o`/`-n`: warn+succeed style where Wayland can't
(match wdotool's philosophy; document).

**Known desktop-id mapping hole**: sway workspace *number* N prints as desktop
N-1, but a workspace literally numbered 0 and *named* (numberless) workspaces
both print as desktop `-1` — colliding with wmctrl's `-1` = sticky/all-desktops
notation, and unreachable via `-s`/`-t` (which count 0-based and so address sway
numbers 1+ only). This is inherent to mapping wmctrl's dense 0-based desktop
list onto sway's sparse/named workspaces; `-R`/`-t -1` sidestep it by using
sway's own "workspace current".

## Testbed

- Sandbox devshell now ships xwayland/xterm/xprop/xwininfo/xeyes and the real wmctrl.
  Headless sway with `xwayland enable` in its config starts XWayland lazily (first X
  client); DISPLAY is announced in `swaymsg -t get_tree`-visible env or sway's log —
  export it and real X apps run. Real wmctrl (an X client) then works against the
  same session: it is the live oracle for list formats AND for which actions work on
  XWayland windows.
- VM: stage 1 of the workflow installs xwayland+wmctrl+xterm there and re-enables
  xwayland in vm/compositor.sh for final validation and, later, the mixed X+Wayland
  demo gif.
- Build: `scripts/build-pyz.sh` also emits `dist/wwmctl` (extend it: same zipapp
  pattern, entry `wwmctl.cli:main`).

## GNOME

On a stock GNOME Wayland session (Ubuntu 24.04 / GNOME 46, 26.04 / GNOME 50)
the compositor plane is the fuckwayland bridge extension
(`gnome/install-bridge.sh`, see `gnome/README.md`) through
`wdotool.backend_gnome.GnomeBackend`. wwmctl never reaches into backend
privates there: it consumes the typed hooks `views()`, `workspaces()`,
`x_info()`, `select_window()`, `show_desktop()`, `set_num_desktops()` of
`wdotool/backend.py`, tried *ahead of* the sway `_nodes()` path (which is
untouched) and the generic `list()` fallback.

* **Ids and the list.** `views()` carries Mutter's X11 client window id for
  every XWayland window (`Meta.Window` → `lookup_xwindow`), so `-l` prints
  the same `0x%08x` that `xprop -root _NET_CLIENT_LIST` and real `wmctrl -l`
  show, and `-i` accepts either that X id or the bridge id
  (`Meta.Window.get_id()`, the decimal id `wdotool search` prints). Native
  windows print the bridge id. Rows are Mutter's stacking order, bottom to
  top (real wmctrl prints `_NET_CLIENT_LIST`, i.e. creation order — the
  ids are the same, the order is not).
* **Columns.** `-x`: `instance.class` from the X plane's `WM_CLASS` for
  XWayland windows (the bridge's `wm_class_instance`/`wm_class` pair stands
  in when Xwayland cannot be reached), `app_id.app_id` for native windows
  (Mutter reports the Wayland app id as `wm_class`; GTK apps without one
  fall back to `gtk_app_id`). `-p`: Mutter's pid, `_NET_WM_PID` only as a
  fallback. `-G`: the X client rectangle (GetGeometry + TranslateCoordinates,
  root coordinates) for XWayland windows — one titlebar below the frame,
  Mutter being a reparenting WM — and the bridge's `get_frame_rect()` for
  native ones (logical pixels, no CSD shadows). Machine column: the
  `WM_CLIENT_MACHINE` of X windows, the local hostname otherwise (the
  machine-column rule above). The desktop column is the workspace index,
  `-1` for a sticky window — Mutter's dense 0-based indices are exactly
  wmctrl's, so GNOME has none of the sway id-mapping hole.
* **The X plane** is opened with the `DISPLAY`/`XAUTHORITY` the bridge
  reports (`XInfo`: gnome-shell's own environment, else Mutter's
  `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` cookie found by
  `wdotool.session`), passed to `x11_mini.X11Conn(display, xauthority=)`.
  Mutter starts Xwayland with `-auth`, so the cookie is mandatory — the
  cookie-less same-uid pass that works on wlroots is refused there — and
  this is what makes `ssh root@box` with an empty environment, `sudo`, and
  a GNOME custom-shortcut process all reach it. Xwayland is spawned **on
  demand** by Mutter (the listening socket exists even when no server
  does), so wwmctl only connects when an XWayland window is listed or an
  `Xwayland` process exists (`session.xwayland_running()`); listing a
  purely native desktop never starts an X server.
* **`-d`** comes from `ListWorkspaces`: `DG` is `global.display.get_size()`,
  `VP` is `0,0` for the current workspace and `N/A` for the rest (one
  EWMH viewport pair), `WA` is the workspace's work area over all monitors
  (what Mutter writes into `_NET_WORKAREA`: `0,32 1920x1048` under the top
  bar), the name is `Meta.prefs_get_workspace_name(i)` (`Workspace 1`, …,
  the strings in `_NET_DESKTOP_NAMES`; a nameless workspace prints its
  index). With dynamic workspaces (GNOME's default) the trailing empty
  workspace is listed too — it is real for `-s`/`-t`.
* **`-m`** reads `_NET_SUPPORTING_WM_CHECK` → `_NET_WM_NAME` (`GNOME
  Shell`), `WM_CLASS`/`_NET_WM_PID` (`N/A`: Mutter's check window has
  neither) and `_NET_SHOWING_DESKTOP` (`ON`/`OFF`) from the X root when
  Xwayland is up — byte-identical to real wmctrl there —, waiting up to 2 s
  for a freshly started Xwayland to get its root properties. Without
  Xwayland the name comes from the backend (`GnomeBackend.wm_name`, the
  same string) and the showing-desktop mode is `N/A` (Mutter's real
  show-desktop state has no public API off the X root).
* **Actions** all go through the bridge, for XWayland and native windows
  alike: `-a` `Activate` (switches workspace, unminimizes, raises,
  focuses), `-c` `Close` (polite delete), `-R` `MoveToWorkspace(current)` +
  `Activate`, `-t N` `MoveToWorkspace(N)` (`-t -1` = current; an index past
  the last workspace is a one-line error, exit 1, where wmctrl would fire
  the request into the void), `-s N` `SetActiveWorkspace` (same for a bad
  index), `-b (add|remove|toggle),P1[,P2]` `SetState` per property —
  `fullscreen`, `maximized_vert`/`maximized_horz` (real per-axis
  maximization on every GNOME release), `hidden` (minimize), `above`,
  `sticky`, `demands_attention` are applied by Mutter; `skip_taskbar`,
  `skip_pager`, `modal` warn and succeed (no Mutter setter, nothing
  observable); `shaded` and `below` warn `…; ignoring` and exit 0 like any
  request "the WM may ignore". `-e G,X,Y,W,H` carries
  `_NET_MOVERESIZE_WINDOW`'s meaning: `W,H` are the **client** size and
  the gravity names the point of the window the request positions —
  `1` NorthWest puts the frame's top-left at `X,Y`, `5` Center puts its
  centre on the requested rectangle's, `9` SouthEast its bottom-right at
  `X+W,Y+H`, `10` Static the client itself at `X,Y`; `0` means "the
  window's own `WM_SIZE_HINTS` gravity" and is taken as NorthWest, the
  ICCCM default. A `-1` keeps that point where it is, so `-e 9,-1,-1,W,H`
  pins the bottom-right corner and grows the window up and to the left,
  while `-e 0,-1,-1,W,H` is a resize alone. The frame extents — Mutter's
  server-side titlebar, i.e. the difference between its frame rect and
  the X client rectangle `-lG` prints — turn that client rectangle into
  the `Resize` (frame size) and `Move` (frame top-left) the bridge takes:
  `-e 0,10,20,300,200` on an xterm under a 37 px bar is a `300x237` frame
  at `10,20` around a `300x200` client at `10,57`, which is what real
  wmctrl gets from Mutter. Native windows (and an XWayland window whose X
  plane could not be reached) have no extents, so there every gravity but
  Static collapses to NorthWest and `X,Y,W,H` are the frame rectangle
  `-lG` prints. A maximized or fullscreen window is silently constrained
  by Mutter, as on X11. Two Mutter quirks are deliberately **not**
  copied, both from its gravity code reading the frame rect *with* the
  invisible resize border (worth `28` px horizontally and `66` px
  vertically on GNOME 46) and placing by the visible one: with an
  explicit `X,Y` under a trailing or centre gravity real wmctrl lands
  1–2 px further out
  (`-e 9,900,700,300,200`: Mutter `902,665`, wwmctl `900,663`), and a
  value left at `-1` is re-read from that inflated rect, so
  `-e 6,-1,-1,300,-1` grows the height it was not asked to touch from
  `400` to `466` and `-e 3,-1,60,-1,-1` slides x by 16 px. wwmctl keeps
  what the `-1` asked it to keep.
  `-k on|off` is the bridge's `ShowDesktop` (minimizes every normal
  window on the active workspace and restores exactly those on `off` —
  Mutter's own mode is not scriptable). `-n N` is `SetNWorkspaces`: works
  with static workspaces, and with GNOME's default dynamic workspaces the
  bridge refuses and wwmctl prints `wwmctl: dynamic workspaces are enabled
  …; ignoring` (exit 0, the request the WM ignored). `-o`/`-g` warn and
  succeed as everywhere.
* **`-N`/`-I`/`-T`** set `WM_NAME`/`_NET_WM_NAME` (and the icon names) on
  XWayland windows over the X plane (Mutter re-reads them at once); on
  native windows they warn `native window; ignoring` and exit 0 — Wayland
  has no way to rename another client's toplevel.
* **`:SELECT:`** is the bridge's `SelectWindow`: wwmctl prints `focus the
  target window to select it` and returns with the next window that gains
  focus (focus a *different* window, as on sway). `:ACTIVE:` is Mutter's
  focus window.
* **Errors.** Every failure is one line on stderr, exit 1: the bridge not
  installed (`gnome backend: the fuckwayland bridge extension is not
  running in GNOME Shell; run gnome/install-bridge.sh and restart the
  session (log out and back in)`), installed but disabled, the screen
  locked (extensions stop behind the lock screen), the bridge gone
  mid-session; `-a`/`-c`/… on a window that vanished exits 1 silently like
  a no-match. Unit coverage: `tests/test_wwmctl_gnome.py` on the mock
  bridge of `tests/test_backend_gnome.py`.

Verified live (branch `gnome-wm-tools`, `vm/vmctl` rigs, xterm + xeyes
+ gnome-text-editor + gnome-calculator): Ubuntu 24.04 / GNOME Shell 46 and
Ubuntu 26.04 / GNOME Shell 50, as the desktop user, from a
`<Ctrl><Super>F7` custom shortcut, from `ssh root@` with `env -i`, and
under `sudo` — `-l/-lpGx` (the xterm under its X id `0x00800020` /
`0x0060001e`, `xterm.XTerm`, `WM_CLIENT_MACHINE`; natives under bridge ids
like `0x14a3062e`), `-d` (`WA: 66,32 3774x1048  Workspace 1` on two
1920x1080 heads with the dock; real `wmctrl -d` fails there with `Cannot
get current desktop properties`: Mutter's X root carries no
`_NET_CURRENT_DESKTOP`), `-m` byte-identical to real `wmctrl -m`, `-a`,
`-c :ACTIVE:`, `-i` with either id, `-e` against real `wmctrl` on the
same window (GNOME 46; `xeyes`, `_NET_FRAME_EXTENTS 0,0,37,0`): identical
rectangles for gravity `0`/`1`/`10` and for every bare resize
(`9,-1,-1,300,250` on a `200,150 500x400` frame → `400,263 300x287`,
the `700,550` corner kept; `2,-1,-1,300,250` → `300,150`), 1–2 px apart
only where Mutter's own gravity arithmetic is (above); xterm snaps
`500x400` to `496x392` on its size increments, `-b add,fullscreen` /
`maximized_vert` seen by real `xprop`, `shaded,below`/`skip_taskbar`
warn+exit 0, `-N/-I/-T` read back by real `xprop`, `-t 1`, `-R`, `-s`,
`-t 7` → `workspace 7 not found`, `-k on/off` (0 then 3 visible windows),
`-n 3` warn+exit 0, `:SELECT:` returning on a `wdotool windowactivate`.
Real `wmctrl -lG` prints the doubled coordinates (`80 118` for our `66
69`) — the non-reparenting-xwm quirk the contract already excludes.
