# wdotool — design contract

Drop-in `xdotool` clone for Wayland. Pure-stdlib **Python 3.10+**, packaged by
`flake.nix` (nix is the only toolchain — never install compilers/toolchains into the
home directory). Ships two ways: `nix build` (bin/wdotool + bin/xdotool symlink) and a
single-file zipapp (`python3 -m zipapp` with a `/usr/bin/env python3` shebang) that runs
on stock Ubuntu. Root is acceptable and expected (for `/dev/uinput`). No kernel modules.

## Architecture

- **Input injection**: virtual evdev devices via `/dev/uinput` (keyboard, relative
  mouse, absolute pointer mimicking a QEMU USB tablet). Compositor-agnostic — injection
  happens at the kernel input layer, so it works on GNOME, KDE, wlroots, everything.
- **Daemon**: first invocation auto-spawns itself as a daemon (`argv[1] == "__daemon"`,
  double-fork; see `__main__.py`). The daemon owns the uinput devices (device creation
  costs ~500ms of compositor hotplug latency — pay it once), tracks the injected cursor
  position, and serves JSON-lines on a unix socket: `/run/wdotool.sock` when euid==0,
  else `$XDG_RUNTIME_DIR/wdotool.sock`. Client waits for readiness on first spawn.
- **Window management**: per-compositor backends behind `backend.WindowBackend`.
  Detection order: sway/i3 IPC socket → KWin D-Bus → GNOME Shell → wlr
  foreign-toplevel. Window IDs are backend-native numeric ids (sway: node id), printed
  in decimal like xdotool.
- **Running under sudo**: session sockets (`$XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`,
  `SWAYSOCK`, user D-Bus) are discovered by scanning `/run/user/*`, preferring
  `SUDO_UID` — implemented in `wdotool/session.py`.

## Parity references (read these!)

- `SCRATCH/xdotool-src/` — real xdotool source. `xdotool.pod` is the manpage (exact
  flags, output formats, chaining semantics); `cmd_*.c` settle anything ambiguous
  (exact output strings, exit codes, arg consumption).
- `SCRATCH/reference/xdotool-help.txt` — the 48-command list (parity surface).
- In the dev shell (`nix develop`), the real `xdotool` binary and its manpage are
  installed for reference.

SCRATCH = `/tmp/claude-1000/-home-yans-code-wdotool/7fb9b947-9d44-4921-a0b5-d12440271911/scratchpad`

## Command dispatch contract

Chained commands parse exactly like xdotool: each command consumes its own flags and
positionals from the remaining argv and returns how many tokens it consumed:

```python
def cmd_foo(ctx: Context, args: list[str]) -> int   # tokens consumed, excluding command name
```

Failure: `raise CmdError(msg)` — driver prints to stderr, aborts the chain, exits 1.
Non-fatal failure (`search` with no matches): set `ctx.exit_code = 1`, consume args
normally. `Context` (frozen, `ctx.py`) provides `stack`, `resolve_window(arg|None)`,
`resolve_windows` (`%@` → whole stack), lazy `backend()` and `daemon()`.

## File ownership (do not edit files you don't own)

- **Agent A (cli core)**: `cli.py`, `commands.py`, `misc_cmds.py`
- **Agent B (input)**: `daemon.py`, `uinput.py`, `keymap.py`, `keysyms.py`, `input_cmds.py`
- **Agent C (windows)**: `backend_detect.py`, `backend_sway.py`, `backend_wlr.py`,
  `backend_kwin.py`, `backend_gnome.py`, `window_cmds.py`, `desktop_cmds.py`
- **Frozen (implemented; edit only if broken)**: `__init__.py`, `__main__.py`, `ctx.py`,
  `backend.py`, `session.py`, `wayland_mini.py`, `flake.nix`, `pyproject.toml`

`wayland_mini.py` is a working pure-stdlib Wayland wire client shared by B (output
geometry) and C (foreign-toplevel) — wire-level bugfixes allowed, API changes need a
note in your report.

## Command → owner → function (function name = `cmd_<name>`)

- A `misc_cmds`: `exec`, `sleep`, `getdisplaygeometry` (via `ctx.daemon().geometry()`).
  `cli.py` additionally handles: `help`, `version`, `-h/--help`, `-v/--version`, usage
  text, script mode (`wdotool <file> args...` when the first arg is a readable file, and
  `wdotool -` from stdin) per the manpage SCRIPTS section, and the driver loop over
  `commands.py`'s `{name: fn}` registry.
- B `input_cmds`: `key`, `keydown`, `keyup`, `type`, `click`, `mousedown`, `mouseup`,
  `mousemove`, `mousemove_relative`, `getmouselocation`, `behave_screen_edge`
  (unsupported: CmdError).
- C `window_cmds`: `search`, `selectwindow`, `getactivewindow`, `getwindowfocus`,
  `getwindowname`, `getwindowclassname`, `getwindowpid`, `getwindowgeometry`,
  `windowactivate`, `windowfocus`, `windowraise`, `windowlower`, `windowmap`,
  `windowunmap`, `windowminimize`, `windowmove`, `windowsize`, `windowclose`,
  `windowquit`, `windowkill`, `windowreparent` (warn+succeed), `windowstate`,
  `set_window` (warn+succeed), `behave` (unsupported: CmdError).
- C `desktop_cmds`: `set_num_desktops` (warn+succeed), `get_num_desktops`,
  `set_desktop`, `get_desktop`, `set_desktop_for_window`, `get_desktop_for_window`,
  `get_desktop_viewport` (print `0 0`), `set_desktop_viewport` (warn+succeed).

Impossible-on-Wayland *cosmetic* ops (marked warn+succeed): one warning line to
**stderr**, consume args correctly, succeed — scripts must not break on them. Real
capability gaps in a detected backend: raise CmdError.

## DaemonClient API (daemon.py — B implements; signatures frozen)

```python
class DaemonClient:
    @classmethod
    def connect_or_spawn(cls) -> "DaemonClient": ...
    def type_text(self, text: str, delay_ms: int): ...
    def key(self, spec: str, direction: str, delay_ms: int, clearmods: bool): ...
        # spec "ctrl+shift+t"; direction in {"press","down","up"}
    def mousemove_abs(self, x: int, y: int): ...
    def mousemove_rel(self, dx: int, dy: int): ...
    def button(self, btn: int, down: bool): ...   # 1=L 2=M 3=R 4..7=wheel 8..12=side/extra/fwd/back/task
    def click(self, btn: int, repeat: int, delay_ms: int): ...
    def pointer(self) -> tuple[int, int]: ...
    def geometry(self) -> tuple[int, int]: ...    # bounding box of all outputs
def daemon_main() -> int: ...
```

Daemon notes (B):
- uinput via pure stdlib (`fcntl.ioctl` + `struct`; the legacy `uinput_user_dev`
  write-based setup is the portable path). Devices: keyboard (KEY_ 1..=248), relative
  mouse (REL_X/Y/WHEEL/HWHEEL + BTN_LEFT/MIDDLE/RIGHT/SIDE/EXTRA/FORWARD/BACK/TASK,
  the last three giving X buttons 10..12), absolute pointer
  (ABS_X/ABS_Y 0..=32767 + buttons + wheel — the QEMU usb-tablet shape every compositor
  maps across the whole output layout). Sleep ~600ms once after creation for hotplug.
- `key` spec: modifier aliases ctrl/control/alt/meta/super/shift + any keysym name;
  keysym names via `keysyms.py` — GENERATE a full name→keysym→unicode table from
  xorgproto `keysymdef.h` (curl once, commit the generated file; no network at build).
- char → keycode: static US-QWERTY table (char → keycode, shifted?). Unreachable chars:
  stderr warning, skip. `clearmods`: inject key-up for all 8 modifier keys first.
- geometry: Wayland client via `wayland_mini` (wl_output geometry+mode; prefer
  zxdg_output logical size/position when advertised). Cache. Fallback 1920x1080 + warn.
  Abs scaling: `x * 32767 // max(w - 1, 1)`.
- pointer position: track injected position (abs sets, rel adds, clamp to geometry).
  Authoritative for scripting; physical-mouse drift is out of scope.
- Protocol: one JSON object per line each way; `{"ok":true,...}` /
  `{"ok":false,"error":"..."}`. Ops mirror the client API 1:1.

## Backend notes (C)

- **sway**: raw i3-ipc over `SWAYSOCK` ("i3-ipc" magic + u32 len + u32 type + JSON).
  Window id = node id. map/unmap = scratchpad show / move-to-scratchpad; minimize =
  move-to-scratchpad; raise/lower: warn no-op for tiled, focus for floating.
  Desktops = workspaces, 0-based for xdotool (workspace `num` - 1). `selectwindow` =
  subscribe to window focus events, return the next focused window.
  `getmouselocation`'s window field: hit-test the daemon-tracked pointer against
  `list()` geometries (topmost/focused first).
- **kwin**: `org.kde.KWin` scripting over the session bus; shelling out to
  `busctl --user` / `gdbus call --session` (with the env from `session.find_user_bus`)
  is ACCEPTABLE here.
- **gnome**: try `org.gnome.Shell.Eval` (unsafe mode); else a Window-Calls-style
  extension interface if present; else CmdError with a one-line hint. `gdbus` fine.
- **wlr**: `zwlr_foreign_toplevel_management_unstable_v1` via `wayland_mini`. IDs:
  1000000 + enumeration order. list/activate/close/fullscreen/minimize only; geometry
  unknown (0,0 + output size); move/resize → CmdError.
- `search`: `re.search` on title (`--name`), app_id (`--class`, `--classname`);
  `--pid`, `--all/--any`, `--limit N`, `--onlyvisible`, `--sync`. Writes `ctx.stack`.
  Exact output formats for search/getwindowgeometry/getmouselocation (+ `--shell`
  variants): copy from the manpage and `cmd_*.c`.

## Testing

- In-sandbox (no uinput here — container blocks it): `nix develop`, then
  `WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 sway` gives a real Wayland
  compositor for backend/protocol testing (swaymsg, foot, grim available).
- Full-stack: Ubuntu 26.04 QEMU/KVM VM (user networking, ssh localhost:2222), sway with
  `WLR_BACKENDS=headless,libinput` — libinput MUST be listed so sway picks up uinput
  devices. Screenshots `grim -c` (cursor included). Demo gif: frames → ffmpeg → gif.
