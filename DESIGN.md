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
  Detection order (`backend_detect.py`): `WDOTOOL_BACKEND` → sway/i3 IPC socket →
  KWin (`org.kde.KWin` owned) → GNOME (`org.gnome.Shell` owned) → wlr
  foreign-toplevel → error; the two D-Bus checks are one `ListNames` over
  `dbus_mini`, and the connection is reused by the GNOME backend. A GNOME session
  without the bridge extension fails with the install hint instead of falling
  through (Mutter has no foreign-toplevel protocol). Window IDs are backend-native
  numeric ids (sway: node id; GNOME: `Meta.Window.get_id()`), printed in decimal
  like xdotool.
- **Running under sudo / as root over ssh**: session sockets (`$XDG_RUNTIME_DIR`,
  `WAYLAND_DISPLAY`, `SWAYSOCK`, user D-Bus) are discovered by scanning `/run/user/*`
  — implemented in `wdotool/session.py`. Candidate runtime dirs are anchored on the
  graphical session: a dir holding a `wayland-*` socket sorts first (so `ssh root@`
  with its own empty `/run/user/0` still finds the user's bus), then `SUDO_UID` /
  `PKEXEC_UID`, then real users. The X plane (Xwayland) is found by
  `session.find_x_display()` / `find_xauthority()` (`$DISPLAY`/`$XAUTHORITY`,
  gnome-shell's own environment via `/proc`, Mutter's
  `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` cookie, `/tmp/.X11-unix/X*`).

## X11 passthrough (`passthrough.py`)

We are installed **over** the originals, so on a plain X11 session (Xfce, i3,
GNOME-on-Xorg, KDE-on-Xorg) the right thing to do is get out of the way: the X
server is authoritative there, `xdotool` has XTEST and `--sync` on real X
events, `xprop` has the real property store, `xrandr` has the real RandR, and
we cannot beat any of it from outside. Worse, backend detection would *half*
succeed — GNOME-on-Xorg owns `org.gnome.Shell`, KWin-on-X11 owns
`org.kde.KWin` — so the check has to run **before** it.

`wdotool/passthrough.py` is shared and **frozen after landing** (like
`session.py` and `dbus_mini.py`): wire-level fixes allowed, API changes need a
note. Pure stdlib, no imports from the rest of the tree except `session.py`.

**`session_kind() -> "wayland" | "x11" | None`**, ordered, memoised, and
reading nothing but the environment it is handed plus three seam directories
(`_X11_SOCK_DIR`, `_LOGIND_DIR`, `_RUN_USER_DIR` — which is what makes the
tests hermetic):

1. `FUCKWAYLAND_PASSTHROUGH` / `WDOTOOL_PASSTHROUGH` & co: `never` -> wayland
   (run our own code whatever the session), `always` -> x11. Those variables
   are about the *handover*, so a caller that never hands over passes
   `respect_override=False` and skips this step — see `warandr` below;
   `passthrough_mode()` is the way to ask about the variables themselves.
2. `$WAYLAND_DISPLAY` **and** its socket exists -> wayland. Wayland is tested
   first because `$DISPLAY` is set on a Wayland session too (Xwayland) and is
   therefore never evidence of an X11 session — while a live compositor
   socket is conclusive.
3. `$XDG_SESSION_TYPE`, ignored when `SUDO_UID`/`PKEXEC_UID` is set (`sudo`
   keeps root's `XDG_SESSION_TYPE=tty` from an `ssh root@` login).
4. logind's own record, `/run/systemd/sessions/*` (world-readable key=value,
   read-only best-effort; `<id>.ref` is a FIFO and is never opened): the
   active, local, non-greeter session of the target user, and its `TYPE=`.
5. Socket scan: a `wayland-*` socket **owned by the target user** — a display
   manager's Wayland greeter under another uid must not turn an Xfce box into
   a Wayland session, which is the one real trap in this design — else an X
   socket (or a `host:0` display, which the original handles and we do not).
6. Nothing -> `None`, and the tool prints its own "no session" error.

**`real_tool(name)`** walks `$PATH` for the first executable of that name that
is not us. Four independent "not us" guards, because each alone has a hole:
`samestat` against our own entry points; `basename(realpath(cand))` in
`{wdotool, wwmctl, wxprop, wxrandr, warandr}` (the normal install, where
`xdotool` is a *symlink* to our `wdotool`); a 4 KiB head sniff for the
`fuckwayland-clone:` stamp `scripts/build-pyz.sh` writes into every zipapp
(the build fails without it) or for an import of one of our packages, which
is what a `pip`-generated console script looks like — never an ELF, and
never a bare `fuckwayland`/`wmctrl` *substring*, or a third-party wrapper
that merely mentions the project would be skipped and the user told to
install what is already installed; and `_FUCKWAYLAND_PASSTHROUGH`, which
carries the realpaths already
handed over to — a process that finds *itself* in that list was exec'd as
somebody's "real tool" and refuses to go round again (plus a depth backstop).
`WDOTOOL_REAL_XDOTOOL` / `WWMCTL_REAL_WMCTRL` / `WXPROP_REAL_XPROP` /
`WXRANDR_REAL_XRANDR` skip the walk; set-but-unusable is an error naming the
variable, never a silent fallback.

**`maybe_exec_real(tool, args, ...)`** is the hook at the top of each
`main()`. It returns `None` (keep running) or an exit status; usually it does
not return at all, because the handover is `os.execve`, not `subprocess`:
exit status, death by signal, stop/cont, the controlling terminal and the
process group all survive for free, stdio stays the real fds (`xprop -root |
head -1`, `xdotool selectwindow`), and nothing extra shows up in `ps`. Two
things must happen first or parity silently breaks: `SIGPIPE` and `SIGXFSZ`
back to `SIG_DFL` (Python ignores both, and an *ignored* disposition survives
`execve`, so `| head -1` would print an EPIPE error instead of dying), and a
stdio flush. argv[0] is the original's own name, so its usage text is
internally consistent. No original installed: **127** (never confusable with
a tool failure) and one line naming the package to install and the override
variable — except for a `--help`/`--version`/bare invocation, which falls
back to our own output, and except for `wxprop` (below). Help is recognised
by each original's *exact* spellings (`-h -V --help --version` for wmctrl,
`-help -version -grammar` for xprop, `-h -v --help --version help version`
and `-hv`-style clusters for xdotool, `-help --help -v --version` for
xrandr): `-v` is `--verbose` in wmctrl and unknown to xprop, and a looser
rule would read `wmctrl -v -l` as a help request and answer it with a
Wayland error where `wmctrl -l` correctly says which package to install.

**Backend precedence and the argv look-ahead (wxrandr, warandr).** One
rule, everywhere: **`--backend NAME` beats `$WXRANDR_BACKEND` beats
auto-detection**, `auto` is the default, and detection is unchanged. `NAME`
is `auto`, `x11` or one of wxrandr's own backends (`sway`, `wlr`,
`mutter`/`gnome`, `kwin`/`kde`); `x11` *is* this handover. Which means the
hook above has to know about the flag before anything is parsed:
`wxrandr.cli.scan_backend_argv()` walks argv with xrandr's own option
arities and reports `--backend NAME` / `--backend=NAME` and whether
`--print-backend`/`--backends` are present — so `--backend sway` on an X11
session runs our own code, `--backend x11` on a Wayland session hands over
(`maybe_exec_real(..., force=True)`; API note, this file being frozen: the
keyword was added for exactly that, and it does not override `entry`), the
two informational options never hand over (the original would only answer
`unrecognized option`), and an *output* named `--backend` (`--output
--backend --off`) is a value, not a flag. The flag itself is stripped from
the argv the original is exec'd with: real xrandr has no such option. Forcing
a backend that is not available here is one line naming what was missing and
exit 1, never a silent fallback; `$WXRANDR_BACKEND` keeps its older
behaviour (no pre-check), because those bytes are pinned. warandr sits on top
of all of it and never hands over at all: it *chooses* which tool to run and
runs it as a child, which is what lets the window switch backends while it
is open.

**Environment repair.** On the X11 path a missing or dead `$DISPLAY` /
`$XAUTHORITY` is replaced with the session's own (logind's `DISPLAY=`, the
socket scan, the display manager's cookie), so `sudo xdotool key a`,
`ssh root@box xprop -root` and cron jobs work *through* us where the original
alone fails — the Wayland trick of `session.py`, applied to X. Values that
already work are never touched, and a `$XAUTHORITY` that points at nothing is
*removed* rather than forwarded (left in place it suppresses the original's
own `~/.Xauthority` default).

Whose session, though: as root with no `SUDO_UID` (`ssh root@box`, root cron)
the uid is in neither the environment nor `getuid()`, and `session_uid()` then
asks logind. Failing that, a system account's runtime directory is skipped —
the lowest-numbered one on a box with a display manager is the *greeter's*,
and its cookie authorises nothing on the user's X server, so forwarding it
would break precisely the case this repair exists for. Same rule as
`find_wayland_socket()`. uid 0 is never an answer either, from either source:
`sudo -i` run *by* root leaves `SUDO_UID=0` behind, and believing it sends the
search into `/root` — measured on a real Xfce box, that is the difference
between `sudo -i xdotool getactivewindow` printing the window name through us
and printing `Authorization required, but no authorization protocol
specified`.

**Per tool.** `wdotool`, `wwmctl` and `wxrandr` exec, always. `wdotool` has no
native X11 option worth having (no XTEST, no `XKeysymToKeycode`, no `--sync`
on X events; uinput would inject, but `getmouselocation` would report our
tracked pointer and `--clearmodifiers` still could not read state — the
documented Wayland approximations on a platform that has none). `wxrandr`:
the X server's RandR is the truth, and our Mutter backend on GNOME-on-Xorg is
at best a second opinion. `wwmctl` *does* carry an X11 wire client
(`wwmctl/x11_mini.py`), and it is still not enough: `-m`, `-d` viewport and
workarea, `-e` gravity math, `-r -b` state toggles, `-x` class matching and
`:SELECT:` (which needs `GrabPointer`/`QueryPointer`, not in `x11_mini`) would
all have to be reimplemented and their byte parity re-proved against the real
`wmctrl` on X11, for the sole benefit of a box with no `wmctrl` installed —
and `x11_mini` is unix-socket only, so it cannot do `ssh -X`'s
`DISPLAY=localhost:10.0` while a handover does that for free. `wxprop` is the
exception: its native X11 path is already complete and proven against a live
X server (`WXPROP.md`), and `core.Session` resolves the X plane from
`$DISPLAY` with no backend at all, so it hands over when a real `xprop`
exists and keeps running when none does (`fallback_native=True`) — no 127
from `wxprop`, ever. From a script's point of view the four behave
identically: on X11 the output is the original's.

`warandr` does **not** exec — it is not a clone of an X11 binary we are
installed over, and it already drives the real `xrandr` on X11. It only swaps
`randr.choose()`'s bare `$WAYLAND_DISPLAY` test for
`passthrough.session_kind(respect_override=False) == "wayland"` (the
`respect_override` is load-bearing: `FUCKWAYLAND_PASSTHROUGH=never` means
"do not hand over", and warandr has nothing to hand over — read as a session
type it would select `wxrandr` on an X11 box and every Apply would say
`Can't open display`, for exactly the developers the variable is documented
for), which fixes a stale
`WAYLAND_DISPLAY` selecting `wxrandr` (the GUI came up and every Apply said
`Can't open display`) and makes a thin `.desktop` environment work. It still
writes the bare word `xrandr` into `~/.screenlayout/*.sh` for arandr
compatibility; if we are installed over `/usr/local/bin/xrandr` that word
resolves to us and passes through — one extra process, correct result, no
recursion.

**Never exec'ing the test runner.** ~17 tests call `cli.main([...])`
in-process and several spawn our tools as subprocesses, so an unguarded hook
would `execve` the suite away (and `tests/test_cli_parity.py`, which shells a
shim named `xdotool` while the real one is on PATH, would compare the real
xdotool with itself and pass tautologically). Three independent belts:
`entry=False` — an explicit argv means we are being used as a library, and a
library never replaces its caller's process; `tests/conftest.py`; and the
`FUCKWAYLAND_PASSTHROUGH=never` line every `tests/test_*.py` carries (the
suite is run file by file, where conftest never loads), which a test in
`tests/test_passthrough.py` enforces for future files.


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
  `backend.py`, `session.py`, `passthrough.py`, `wayland_mini.py`, `flake.nix`,
  `pyproject.toml`

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
    def seed_pointer(self, x: int, y: int): ...   # adopt the compositor's real pointer
    def geometry(self) -> tuple[int, int]: ...    # (w, h) of the full output layout
    def geometry_full(self) -> tuple[int, int, int, int]: ...  # (min_x, min_y, w, h)
    def geometry_status(self) -> tuple[int, int, bool]: ...    # (w, h, is_a_guess)
def daemon_main() -> int: ...
```

Daemon notes (B):
- uinput via pure stdlib (`fcntl.ioctl` + `struct`; the legacy `uinput_user_dev`
  write-based setup is the portable path). Devices: keyboard (KEY_ 1..=255 —
  everything the keymap can emit, numeric X keycodes included), relative
  mouse (REL_X/Y/WHEEL/HWHEEL + BTN_LEFT/MIDDLE/RIGHT/SIDE/EXTRA/FORWARD/BACK/TASK,
  the last three giving X buttons 10..12), absolute pointer
  (ABS_X/ABS_Y 0..=32767 + buttons + wheel — the QEMU usb-tablet shape every compositor
  maps across the whole output layout). Sleep ~600ms once after creation for hotplug.
- `key` spec: modifier aliases ctrl/control/alt/meta/super/shift + any keysym name;
  keysym names via `keysyms.py` — GENERATE a full name→keysym→unicode table from
  xorgproto `keysymdef.h` + `XF86keysym.h` (curl once, commit the generated file;
  no network at build). XF86 keysyms resolve through a hand-curated XF86→evdev
  table in `keymap.py` (they have no unicode); `_EVDEVK`-range keysyms
  (0x10081000+code) carry their own evdev code.
- char → keycode: static US-QWERTY table (char → keycode, shifted?). Unreachable chars:
  stderr warning, skip. `clearmods`: inject key-up for all 8 modifier keys first.
- geometry: Wayland client via `wayland_mini` (wl_output geometry+mode; prefer
  zxdg_output logical size/position when advertised), with a 3s socket timeout so a
  wedged compositor falls back instead of hanging the daemon. Cache is the full
  layout box `(min_x, min_y, w, h)` — multi-output layouts can have non-zero or
  negative origins. Fallback (0, 0, 1920, 1080) + warn, and the `geometry` reply
  says `fallback: true` so `getdisplaygeometry` can refuse to print a guess
  (rc 2) instead of pretending. `DaemonClient.geometry()` keeps returning
  `(w, h)`, `geometry_full()` adds the origin, `geometry_status()` adds the flag.
- **abs scaling (B7)**: `ceil((x - min_x) * 32768 / w)`, clamped to 0..32767.
  libinput maps an absolute axis with `scale_axis()`, `v * w / (max - min + 1)`
  = `v * w / 32768`, and the compositor truncates that to a pixel; the forward
  map that round-trips exactly through that inverse is the *ceiling*, since
  `floor(ceil(x*32768/w) * w/32768) == x` for every `x` in `[0, w)` while
  `w <= 32768`. The old floor map `(x - min_x) * 32767 // (w - 1)` landed one
  pixel short wherever the division was inexact — 257 of 301 x values near the
  origin of a 5760px layout.
- **always land the move (B2)**: the kernel drops an `EV_ABS` whose value equals
  the axis' current value, so re-sending the coordinates the tablet reported
  last time was a silent no-op — and REL events, a physical mouse or another
  daemon may have moved the pointer since. When the computed axis pair equals
  the last one written, one axis is nudged by a single unit (1/32768 of the
  layout: sub-pixel) in its own report first. A fresh uinput device reads 0,0,
  which is why a first `mousemove 0 0` is nudged too.
- **relative moves (B1)**: `REL_X`/`REL_Y` go through the compositor's pointer
  acceleration, so `mousemove_relative 500 0` moved 462px on GNOME 46 and 267px
  on GNOME 50 while xdotool's `XWarpPointer` is exact. Everywhere but sway/i3
  the daemon emits the *target* `(px+dx, py+dy)` as an absolute warp instead.
  sway/i3 keep the REL path (that rig runs `pointer_accel 0`, and the daemon
  tests pin the `EV_REL` contract); `WDOTOOL_REL_MODE=abs|rel` forces either.
  The switch is decided once per daemon by looking for a sway/i3 IPC socket.
- **pointer position (B6)**: the daemon tracks the position it last injected —
  a *model*, not the truth. Clients that can ask the compositor (the GNOME
  bridge's `GetPointer`) do so first and push the answer back with
  `seed_pointer`, so `getmouselocation` reports reality and a following
  relative move counts from it. sway's IPC has no pointer query, so the model
  stands there. A daemon that never opened `/dev/uinput` refuses the `pointer`
  op with that reason rather than answering `0,0` with rc 0.
- **spawn hygiene (B10/B11)**: the double-forked grandchild closes every fd
  above stdio (it used to keep the client's session-D-Bus socket ESTABLISHED
  for its whole life), `chdir("/")`, keeps only the environment it needs
  (`XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`, `HOME`, `PATH`, `USER`, `LOGNAME`,
  `LANG`, `LC_ALL`, `SUDO_UID`, `PKEXEC_UID`, `WDOTOOL_*`), and re-execs as
  `wdotool __daemon` or `python -m wdotool __daemon` so `ps` shows the daemon
  and not the command that happened to spawn it. When it was started from a
  GNOME custom shortcut it also leaves the launcher's transient
  `app-*.scope`: a systemd scope stays active while any process is in its
  cgroup and neither fork nor setsid changes cgroups, so the daemon used to
  hold that scope (and the shortcut's shell script, as far as systemd was
  concerned) open for its whole life. It writes its pid into a sibling cgroup
  under the user manager's delegated subtree — one write, no `systemd-run`
  subprocess and none of its environment surprises. `session-*.scope` and
  service units are left alone: a daemon started from a login session should
  still die with it. A failure at any step is not fatal, just the old
  behaviour.
- hardening: the socket is bound under umask 0o177 and chmod 0600 (root daemon
  serves root only; non-root users spawn their own per-user daemon and hit the
  clean "/dev/uinput ... run it as root" error). Per-request catch-all keeps a
  malformed request from killing the connection; repeat ≤ 1e6, delays ≤ 300s,
  coordinates int32, request lines ≤ 16MB. Partially-created uinput devices are
  closed on failure and creation is retried on the next request.
- Protocol: one JSON object per line each way; `{"ok":true,...}` /
  `{"ok":false,"error":"..."}`. Ops mirror the client API 1:1.

### `--sync` waits (B3)

`window_cmds._wait_until` polls with a deadline: `WDOTOOL_SYNC_TIMEOUT`
seconds, 10 by default, `0` for the old unbounded loop. On expiry it raises
`CmdError("wdotool: gave up waiting for <what> after <n>s")` — one line, rc 1.
xdotool's own waits are bounded too (`xdo.c` loops `MAX_TRIES` = 500 x 30ms =
15s) but then return success silently; we prefer a diagnosis. `search --sync`
stays unbounded: its manpage entry is the one that promises to block until
there are results.

`windowsize --sync` additionally accepts a size the compositor has *snapped*
to. Mutter honours the client's resize increments, so an xterm asked for
497x392 stays at 496x392 and "the size changed" is never true; the wait ends
when the size changed at all (xdotool's rule) **or** the current size is
within `_SNAP_TOL` (32px, about one character cell) of the request.

### Exit codes (B5)

`CmdError.exit_code` is 1. `NoSessionError` (`ctx.py`) carries 2 and means
"no Wayland session / window backend found at all": no compositor, no session
bus, GNOME Shell absent, the screen locked, the greeter, the bridge extension
not running. `cli.run_chain` returns `exit_code`. That is the whole
difference between "not logged in yet" and "no such window" for a script.

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
- **gnome**: the fuckwayland bridge extension (`gnome/fuckwayland-bridge@fuckwayland`,
  installer `gnome/install-bridge.sh`) exports Mutter over the session bus — name
  `org.fuckwayland.Bridge`, path `/org/fuckwayland/Bridge`, interface
  `org.fuckwayland.Bridge1`, JSON strings for structured results — and
  `backend_gnome.py` is a thin `dbus_mini` client for it (no gdbus, no Eval, no
  Window Calls). Ids = `Meta.Window.get_id()`. `class_` = `wm_class` (Mutter reports
  the Wayland app_id there), else `gtk_app_id`; geometry = `get_frame_rect()` in
  logical pixels, the same space as the daemon's pointer; `visible` = not
  minimized/show-desktop AND on the active workspace (X11 IsViewable), `is_mapped` =
  not minimized. `list()` is stacking order bottom→top; `window_at()` looks through
  DESKTOP/DOCK layers for `getmouselocation`. `activate` = `Meta.Window.activate`
  (switches workspace, unminimizes, raises) and waits ≤ 0.5 s for the focus to land;
  `focus` = `Meta.Window.focus` (no raise); `kill` = `Meta.Window.kill` (Mutter kills
  the client, works as any uid); map/unmap = unminimize/minimize; raise/lower real.
  `windowstate`: FULLSCREEN, MAXIMIZED_HORZ/VERT (per-axis on 46 and 49+), HIDDEN,
  ABOVE, STICKY, DEMANDS_ATTENTION applied by Mutter; SKIP_TASKBAR/SKIP_PAGER/
  MODAL warn+succeed (cosmetic, no Mutter setter); SHADED (observable, Mutter
  cannot shade), BELOW and anything else CmdError. Desktops = workspaces (dynamic
  workspaces count the trailing empty one;
  `set_desktop_for_window -1` sticks). `selectwindow` = bridge `SelectWindow(0)`,
  next focus change. Extras for wwmctl/wxprop: `views()` (xid, WM_CLASS
  instance/class, app_id, states), `workspaces()`, `x_info()` (gnome-shell's own
  DISPLAY/XAUTHORITY via the bridge, else `session.find_x_display/find_xauthority`),
  `events()` (bridge `WindowEvent` signals), `monitors()`, `real_pointer()`
  (diagnostic: the compositor's pointer vs the daemon's). The bridge exports no
  hit-test; `window_at()` is client-side over `ListWindows` so it cannot drift
  from the generic rule. Without the bridge name but with `org.gnome.Shell`
  owned the constructor diagnoses (locked screen, disabled/broken extension, or
  "run `gnome/install-bridge.sh` and restart the session") without touching
  `org.gnome.Shell.Eval`; only `WDOTOOL_GNOME_AUTOLOAD=1` makes it try one Eval
  to load the installed extension first (works in unsafe mode only). Bridge gone
  mid-session (extension disabled, lock screen) → one clear error. The udev rule
  is `uaccess` only (seat user's ACL, node stays `root:root 0600`; no group).
- **wlr**: `zwlr_foreign_toplevel_management_unstable_v1` via `wayland_mini`. IDs:
  1000000 + enumeration order. list/activate/close/fullscreen/minimize only; geometry
  unknown (0,0 + output size); move/resize → CmdError.
- `search`: `re.search` on title (`--name`), app_id (`--class`, `--classname`);
  `--pid`, `--all/--any`, `--limit N`, `--onlyvisible`, `--sync`. Writes `ctx.stack`.
  Exact output formats for search/getwindowgeometry/getmouselocation (+ `--shell`
  variants): copy from the manpage and `cmd_*.c`.

## dbus_mini (shared — `wdotool/dbus_mini.py`)

Pure-stdlib D-Bus client for the session bus and any `unix:` address (QEMU's
`-display dbus` bus). No gdbus/busctl spawns, no glib, signals included. Shared like
`wayland_mini.py`: wire-level fixes are fair game, API changes need a note.

- **Wire**: `unix:path=`/`unix:abstract=`/`unix:runtime=yes` (`;` alternatives, `,`
  key=value, `%XX` unescaped); SASL `\0AUTH EXTERNAL <hex(uid)>` → `OK` →
  `NEGOTIATE_UNIX_FD` (ERROR tolerated) → `BEGIN` → `Hello`. Message = 12-byte fixed
  header + `a(yv)` fields + pad 8 + body; fields written PATH, DESTINATION, INTERFACE,
  MEMBER, ERROR_NAME, REPLY_SERIAL, SENDER, SIGNATURE, UNIX_FDS (byte-identical to
  libdbus's Hello). On read each known field must carry its fixed signature and a
  call/signal/reply/error its required fields (else ValueError); unknown field codes
  and unknown message types (5+) are ignored — such frames are dropped, fds closed. Full
  grammar `ybnqiuxtdhsog a() a{} v`; alignment relative to message start (the pad after
  an array length to the element alignment is not counted and is present for empty
  arrays); reads both endians, writes `l`. struct↔tuple, array↔list (`ay`↔bytes),
  `a{}`↔dict, `v`↔`Variant(sig, value)` on write (plain bool/int/float/str/bytes are
  guessed), plain value on read (`wrap_variants=True` keeps `Variant`). `h` = index into
  the SCM_RIGHTS fds of the same sendmsg (`socket.send_fds/recv_fds`), resolved to real
  fds on read. Max message 2^27 bytes.
- **API**: `Bus(addr=None, as_uid=None)` (addr from `session.find_user_bus()`),
  `call(dest, path, iface, member, sig='', args=(), timeout=25.0) -> tuple`,
  `get_property`/`set_property`/`get_all_properties`, `introspect`, `list_names`,
  `name_has_owner`, `get_name_owner`, `request_name`, `add_match`,
  `wait_signal(iface, member, timeout, path=None, sender=None)` (None on timeout),
  `messages(timeout)` generator of queued signals (and calls with `serve_calls=True`),
  `reply`/`error_reply`/`emit_signal`, `close()`/context manager. `DBusError(name,
  message)` for ERROR replies and local failures (`org.freedesktop.DBus.Error.` +
  `NoServer`/`AuthFailed`/`NoReply`/`Disconnected`). Method calls aimed at us get
  UnknownMethod immediately (Peer.Ping answered) unless `serve_calls`. One `Bus` per
  thread. `Bus(timeout=)` bounds connect (SO_SNDTIMEO covers the kernel wait on a full
  AF_UNIX backlog), auth, Hello and every later send — the socket stays in timeout mode
  and a send that times out closes the connection (Disconnected); `call(timeout=)`
  bounds the reply (NoReply). Fds in a received message belong to whoever takes it and
  are CLOEXEC; fds on frames the client discards (late replies, auto-answered calls,
  unknown types, anything still queued at `close()`) are closed by the client.
- **Root vs the user's bus**: stock session.conf has no `<allow user="*"/>`, so
  dbus-daemon answers root's EXTERNAL auth with `OK` and then closes the socket at the
  policy check (Hello dies with EPIPE). `Bus(as_uid=uid)` — and the automatic retry
  when euid 0 is turned away by a socket owned by another uid — forks a child that
  `setgroups/setgid/setuid`s, connects, authenticates and Hellos, then hands the live
  socket back over a socketpair with SCM_RIGHTS (`connect_as_uid`); SO_PEERCRED is
  fixed at connect, so the bus keeps attributing the connection to that uid.
  `bus.auth_path` is `'direct'` or `'fork'`. A child failure comes back under its own
  error name (NoServer for a missing socket, AuthFailed for REJECTED, AccessDenied "needs
  root" when not root); a child still silent `timeout + 5` s in is SIGKILLed and reaped
  (NoServer). Verified on Ubuntu 24.04 dbus-daemon 1.14.10: user → direct, `sudo` → fork.
- **CLI**: `python3 -m wdotool.dbus_mini [--address A] [--as-uid N|owner] --names |
  --has-owner NAME | --call DEST PATH IFACE MEMBER [SIG JSON-args] | --get DEST PATH
  IFACE PROP | --get-all DEST PATH IFACE | --introspect DEST PATH | --monitor [RULE…]
  [--seconds N]`. Output is JSON (variants unwrapped, `ay` as int lists). Exit 0, 1
  (D-Bus/OS error; Ctrl-C and a closed stdout exit quietly like wwmctl), 2 (usage).
- **Tests**: `tests/test_dbus_mini.py` — byte-exact marshalling facts, the canonical
  128-byte Hello, big-endian parse, DisplayConfig `GetCurrentState`/`ApplyMonitorsConfig`
  fixtures, QEMU `SetUIInfo(qqiiuu)`, an in-process mock bus (auth, names, echo of every
  type, errors, timeouts + late replies, signals, unix fds both ways, client↔client
  calls, fork hand-off, CLOEXEC on received fds, fd release for auto-answered calls /
  unknown types / `close()`, typed header fields, connect and send timeouts against a
  full backlog / a non-reading peer, a stuck child being killed, CLI option errors and
  Ctrl-C); with `DBUS_SESSION_BUS_ADDRESS` set (`dbus-run-session --
  python3 tests/test_dbus_mini.py`) also a real dbus-daemon incl. NameOwnerChanged from
  a second connection. Verified against QEMU 8.2's display bus (`org.qemu`, Console
  properties, `SetUIInfo`).

## Testing

- In-sandbox (no uinput here — container blocks it): `nix develop`, then
  `WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 sway` gives a real Wayland
  compositor for backend/protocol testing (swaymsg, foot, grim available).
- Full-stack: Ubuntu 26.04 QEMU/KVM VM (user networking, ssh localhost:2222), sway with
  `WLR_BACKENDS=headless,libinput` — libinput MUST be listed so sway picks up uinput
  devices. Screenshots `grim -c` (cursor included). Demo gif: frames → ffmpeg → gif.
- X11 passthrough: `tests/test_passthrough.py` (hermetic detection matrix, the
  "not us" guards, each `main()`'s argv convention against a stubbed `execve`,
  help/version, environment repair) and `tests/test_passthrough_exec.py` (real
  processes against a fake install tree: the handover is an `execve` because
  the exec'd tool logs *our* pid, exit codes and signal deaths propagate,
  `| head -1` dies of SIGPIPE, rc 127 and its message, two copies of us on
  PATH stop after exactly two invocations). **Every `tests/test_*.py` sets
  `FUCKWAYLAND_PASSTHROUGH=never`** — the suite would otherwise exec itself
  away on an X11 box, and the parity oracle would go tautological;
  `tests/test_passthrough.py` fails if a test file is missing the line.
