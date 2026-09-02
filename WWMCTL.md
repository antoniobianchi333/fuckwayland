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
