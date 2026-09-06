# Technical.md — how the tree is put together

This is the orientation document. It describes the tree **as it is now**, after the
0.3 subtraction: one hit-test, one number parser, one layout decision, one detach
protocol, one transform table, a shared `fwcommon/` package, and four display
backends that are one shape. Read it if you are about to change something and want to
know where that something lives.

The user-facing documents are elsewhere and this file does not repeat them: the
[README](../README.md) is the install and the per-desktop support, and each tool has a
contract of its own — [WDOTOOL.md](WDOTOOL.md), [WWMCTL.md](WWMCTL.md),
[WXPROP.md](WXPROP.md), [WXRANDR.md](WXRANDR.md), [WARANDR.md](WARANDR.md),
[WMIRROR.md](WMIRROR.md), plus [gnome/README.md](../gnome/README.md) for the bridge
extension and [vm/README.md](../vm/README.md) for the rig.

## 1. The six tools

Six commands, seven Python packages, no third-party dependency anywhere except the
system GTK 3 bindings that `warandr` imports at run time.

| package | command | clones | talks to |
|---|---|---|---|
| `wdotool/` | `wdotool` | xdotool 4.20260303.1 | `/dev/uinput`, `zwp_virtual_keyboard_v1`, `zwlr_virtual_pointer_v1`, and one window backend |
| `wwmctl/` | `wwmctl` | wmctrl 1.07 | one window backend, plus the X plane through `x11_mini` |
| `wxprop/` | `wxprop` | xprop 1.2.8 | the X plane through `x11_mini`, plus one window backend for native windows |
| `wxrandr/` | `wxrandr` | xrandr 1.5.4 | sway IPC, `zwlr_output_management_v1`, Mutter's DisplayConfig, KWin's output protocol |
| `warandr/` | `warandr` | arandr | `wxrandr` or the real `xrandr`, as a child process |
| `wmirror/` | `wmirror` | nothing — there is no X11 original | the external `wl-mirror`, whose lifetime it owns |
| `fwcommon/` | — | — | shared by all six |

`fwcommon/` holds what more than one tool needs and nothing else does, in seven
modules: `session.py` (which session is this, and where are its sockets), `passthrough.py`
(the X11 handover), `dbus_mini.py` and `wayland_mini.py` (the two wire clients),
`errors.py` (`CmdError`, the exception every command in the tree raises and catches),
`stdio.py` (the exit-status rule for an output that never reached its reader) and
`procs.py` (detached children). It is a package rather than a corner of `wdotool`
because that list is exactly what the *display* tools use of it: they find a session,
they talk D-Bus and Wayland, and they never type a key, never open a window backend
and never start the input daemon. It imports nothing outside the standard library, and
nothing in it imports anything of `wdotool`: the package is closed, which is what lets
a zipapp of a display tool carry it and nothing else.

`wdotool/` is a second shared layer, but only for the two window tools: `wwmctl` and
`wxprop` drive its window backends and its X11 wire client (`wdotool/x11_mini.py`).
The three display tools do not touch it at all, which is what their bundles show: they
carry `fwcommon` and their own packages, and nothing of `wdotool` (see § [The
single-file builds](#the-single-file-builds)). It was not always so. Until this release
three small files sat in `wdotool/` and were imported by the display tools, and because
a bundle copies whole directories rather than the modules actually reached, those three
dragged the keysym table, the daemon and every backend into each of them.

Each package's `VERSION` is derived from one constant, `fwcommon.VERSION`.
`pyproject.toml`, `debian/changelog` and `flake.nix` state the same number for their
own build systems, and `scripts/build-deb.sh` refuses to build when the first two
disagree. `wwmctl`'s and `wxprop`'s user-visible version strings are the *oracle's*
numbers (`1.07`, `xprop 1.2.8`) and deliberately not ours.

### The single-file builds

`scripts/build-pyz.sh` writes one zipapp per tool into `dist/`. Each is a plain
`python3 -m zipapp` with a `/usr/bin/env python3` shebang and a
`fuckwayland-clone:` stamp in its first few hundred bytes, which is the head sniff
`fwcommon.passthrough.is_us()` uses to recognise a copy of ourselves installed under
an original's name. What each bundle contains:

| bundle | packages inside | why |
|---|---|---|
| `dist/wdotool` | `fwcommon`, `wdotool` | the tool itself |
| `dist/wwmctl` | `fwcommon`, `wdotool`, `wwmctl` | the window backends and `x11_mini` live in `wdotool` |
| `dist/wxprop` | `fwcommon`, `wdotool`, `wxprop` | same |
| `dist/wxrandr` | `fwcommon`, `wxrandr` | the tool itself: nothing of `wdotool` is reached any more |
| `dist/warandr` | `fwcommon`, `wxrandr`, `warandr` | on Wayland it runs the same interpreter with `-m wxrandr`, `PYTHONPATH` pointing at the zipapp itself |
| `dist/wmirror` | `fwcommon`, `wxrandr`, `wmirror` | it reads the layout through wxrandr's own wlr client, and the detached supervisor is this same zipapp re-entered by fork |

zipapp copies whole package directories, so a bundle that needs three files of
`wdotool` carries all of it. Do not state a byte size for any of these here: the
script's contents are the statement that keeps.

## 2. Session discovery, and the X11 handover

Two modules answer two questions that look like one, and keeping them apart is the
whole design.

* **`fwcommon/session.py` answers "where is the session?"** — which runtime
  directory, which Wayland socket, which session bus, which `DISPLAY`, which X
  cookie, for a caller that may be root with an empty environment. It is what makes
  `sudo wdotool key a`, `ssh root@box wwmctl -l` and a `@reboot` cron job work.
* **`fwcommon/passthrough.py` answers "is this session ours to serve?"** — and, when
  it is not, `execve`s the real `xdotool`/`wmctrl`/`xprop`/`xrandr` with argv
  untouched. It has to decide **before** any backend is detected, because backend
  detection would half succeed on an X11 session: GNOME-on-Xorg owns
  `org.gnome.Shell`, KWin-on-X11 owns `org.kde.KWin`.

The first is a search. The second is a policy, and it is the policy that runs first.

Session sockets (`$XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`, `SWAYSOCK`, the user D-Bus)
are discovered by scanning `/run/user/*`. Candidate runtime dirs are anchored on the
graphical session: a dir holding a `wayland-*` socket sorts first (so `ssh root@`
with its own empty `/run/user/0` still finds the user's bus), then `SUDO_UID` /
`PKEXEC_UID`, then real users. The X plane (Xwayland) is found by
`session.find_x_display()` / `find_xauthority()`: `$DISPLAY`/`$XAUTHORITY`, the
session leader's own environment via `/proc` — gnome-shell, `startplasma-x11`,
`kwin_x11`, `plasmashell`, `xfce4-session`, `sway`, uid-qualified, which is the only
route to SDDM's `/tmp/xauth_<random>` — Mutter's
`$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` cookie, then `/tmp/.X11-unix/X*`.

The **cookie order matters and was measured**: SDDM 0.20 keeps its cookie in
`/tmp/xauth_<random>`, which is in none of the other three places, so the session
leader's `/proc/<pid>/environ` is the only route to it. A system account's runtime
directory is skipped, because the lowest-numbered one on a box with a display manager
is the *greeter's* and its cookie authorises nothing on the user's X server. uid 0 is
never an answer from either source: `sudo -i` run *by* root leaves `SUDO_UID=0`
behind, and believing it sends the search into `/root`.

### The X11 handover (`fwcommon/passthrough.py`)

We are installed **over** the originals, so on a plain X11 session (Xfce, i3,
GNOME-on-Xorg, KDE-on-Xorg) the right thing to do is get out of the way: the X
server is authoritative there, `xdotool` has XTEST and `--sync` on real X
events, `xprop` has the real property store, `xrandr` has the real RandR, and
we cannot beat any of it from outside. Worse, backend detection would *half*
succeed — GNOME-on-Xorg owns `org.gnome.Shell`, KWin-on-X11 owns
`org.kde.KWin` — so the check has to run **before** it. Measured on the
`noble-kde-x11` flavor (Plasma 5.27 on Xorg): `backend_detect.detect()` there
does answer `KwinBackend`, the script backend does load into `kwin_x11` and
list its windows — and on the same session our own `getdisplaygeometry` has
nothing to ask, because a Plasma X11 session has no compositor socket at all.
`wxprop` is the one tool with a native X11 path of its own, so it is also the
one that could reach that backend *after* the handover declined (no real
`xprop` installed): `wxprop.core._detect_backend()` therefore answers `None`
outright on an X11 session, and `-root` is the X root, as the original's is.
That last one is hardening rather than a fix — measured on both Plasma X11
images the merged root was byte-identical to the real `xprop`, because every
window on an X11 session is an X window; what it removes is the synthesized
root the same code produces when the compositor's view carries no X id.

`fwcommon/passthrough.py` is pure stdlib and imports nothing from the rest of the
tree except `session.py`. Change its API deliberately: five `main()`s and the whole
of `tests/test_passthrough_exec.py` are written against the shape below.

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
(`maybe_exec_real(..., force=True)`; the keyword was added for exactly that,
and it does not override `entry`), the
two informational options never hand over (the original would only answer
`unrecognized option`), and an *output* named `--backend` (`--output
--backend --off`) is a value, not a flag. The flag itself is stripped from
the argv the original is exec'd with: real xrandr has no such option. Forcing
a backend that is not available here is one line naming what was missing and
exit 1, never a silent fallback; a `--backend` with no value is still
the flag (the scan returns `""`), so its error is ours on both kinds of
session. `$WXRANDR_BACKEND` keeps its older behaviour (no pre-check), because
those bytes are pinned — except for the single value `x11`, which the hook
does read: the handover is settled before parsing, so a variable that only
reached `Session` could ask this process to be something it can no longer
become, and would have to answer with a fatal about a flag nobody typed.
warandr sits on top of all of it and never hands over at all: it *chooses*
which tool to run and runs it as a child, which is what lets the window
switch backends while it is open.

**Environment repair.** On the X11 path a missing or dead `$DISPLAY` /
`$XAUTHORITY` is replaced with the session's own (logind's `DISPLAY=`, the
socket scan, the display manager's cookie), so `sudo xdotool key a`,
`ssh root@box xprop -root` and cron jobs work *through* us where the original
alone fails — the Wayland trick of `session.py`, applied to X. Values that
already work are never touched, and a `$XAUTHORITY` that points at nothing is
*removed* rather than forwarded (left in place it suppresses the original's
own `~/.Xauthority` default). The repair is `repair_x_env()` and the handover
is only its first caller: warandr's X11 runner is a *child*, so it never
reaches `child_env()`, and until it took the repair for itself it was the one
tool in the repo that still answered `Can't open display` from a root shell
while the other four worked in the same one. A repair is not a guarantee:
where the X server has no cookie file at all — wlroots starts Xwayland with no
`-auth`, so only the session user's own processes may open it — there is
nothing to find, and the real `xprop` fails from that shell too (`wxprop` falls
back to the compositor's synthesized properties; see the repo README, *Desktop
support*).

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
tracked pointer and `--clearmodifiers` could clear only what uinput
itself holds, missing anything the X server or XTEST put there — the
documented Wayland approximations on a platform that has none). `wxrandr`:
the X server's RandR is the truth, and our Mutter backend on GNOME-on-Xorg is
at best a second opinion. `wwmctl` *does* carry an X11 wire client
(`wdotool/x11_mini.py`), and it is still not enough: `-m`, `-d` viewport and
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

## 3. The wire clients, and their error models

Four modules speak a protocol on a socket, and there is exactly one of each. None of
them imports anything outside the standard library, and none of them spawns a helper
binary (`gdbus`, `busctl`, `swaymsg`, `xprop`) to do its talking.

| module | speaks | error model |
|---|---|---|
| `fwcommon/dbus_mini.py` | D-Bus, session bus or any `unix:` address | `DBusError(name, message)` for ERROR replies **and** for local failures, under `org.freedesktop.DBus.Error.` + `NoServer`/`AuthFailed`/`NoReply`/`Disconnected`. Nothing socket-level escapes: a peer that closes mid-SASL comes back as `Disconnected`, not as a bare `ConnectionResetError` |
| `fwcommon/wayland_mini.py` | the Wayland wire protocol | exceptions from the socket, with a deadline on every roundtrip. A wedged compositor times out and the caller degrades, rather than hanging the daemon |
| `wdotool/x11_mini.py` | the X11 core protocol against Xwayland or Xorg | two classes, and every caller treats both as "degrade gracefully": `XUnavailable` for anything connection-level (no server, bad `DISPLAY`, auth rejected, connection lost) and `X11Error` for errors the server reports (BadWindow and friends) |
| `wdotool/backend_detect.py` | nothing itself — it decides which window backend to build | one `ListNames` over `dbus_mini` answers both the KWin and the GNOME question, and the connection is handed to the GNOME backend rather than opened twice |

`x11_mini.py` lives under `wdotool/` and not under `fwcommon/` on purpose: it already
imports `fwcommon.session`, and moving it into `fwcommon` would make that a cycle.
Its three callers are `wwmctl.core` (the X plane of XWayland windows: `WM_CLASS`,
`WM_CLIENT_MACHINE`, geometry, EWMH ClientMessages), `wxprop.core` (all of its
X-window work) and `wdotool.backend_kwin` (the XWayland ids KWin 6 does not export).
It carries enough of the core protocol for that and nothing more: InternAtom,
GetProperty with the long-property offset loop, ChangeProperty, SendEvent, GetGeometry
plus TranslateCoordinates, QueryTree, GetInputFocus as the post-void-request sync, and
OpenFont/QueryFont/CloseFont for `wxprop -font` — the one place it allocates a
resource id. No extensions, no big-requests, byte order `l` only. Property values of
format 32 come back as unsigned 32-bit ints (EWMH's `-1` reads as `0xFFFFFFFF`), and
`get_prop_string()` truncates at the first NUL exactly like wmctrl's `printf("%s")`.

`wayland_mini.py` is the smallest of the four: a registry, `bind`, per-object
handlers, `roundtrip`, and file-descriptor passing. Its callers are the daemon
(`wl_output` geometry, preferring `zxdg_output` logical size and position when
advertised), `backend_wlr` (foreign-toplevel), `vkbd`/`vptr` (the two virtual-device
protocols), `wxrandr`'s wlr and KWin backends and `wxrandr/gamma.py`.

```
c = WlConn(socket_path)
reg = c.get_registry()                # {name: (interface, version)} after roundtrip
oid = c.bind(name, "wl_output", min(version, 4))
c.on(oid, handler)                    # handler(opcode, Cursor, fds)
c.roundtrip()                         # dispatch until the sync callback fires
```

The rest of this section is `dbus_mini` in full, because it is the one whose wire
details a change is most likely to trip over.
### `dbus_mini` in full

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
- **CLI**: `python3 -m fwcommon.dbus_mini [--address A] [--as-uid N|owner] --names |
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

## 4. Window backends

Four backends implement one interface, `wdotool/backend.py:WindowBackend`, and three
tools drive them: `wdotool`'s window commands, all of `wwmctl`, and `wxprop` for
native windows. A backend is an object with these methods, and nothing above it
reaches into a backend's privates any more.

**The core**, which every backend must answer: `list()`, `find(wid)`,
`activate(wid)`, `focus(wid)`, `close(wid)`, `kill(wid)`, `move_window(wid, x, y)`,
`resize(wid, w, h)`, `minimize(wid)`, `map(wid)`, `unmap(wid)`, `raise_(wid)`,
`lower(wid)`, `set_state(wid, state, action)`, `maximize_pair_state()`,
`is_mapped(wid)`, `display_size()`, `window_desktop(wid)`,
`set_window_desktop(wid, n)`, `get_desktop()`, `set_desktop(n)`, `num_desktops()`,
`set_num_desktops(n)`, `select_window()` with its `select_window_hint`.

**The optional hooks**, which default to "not available" so a caller falls back to
`list()`/`find()`: `views()` (typed `View` records — X ids of XWayland windows,
`WM_CLASS` instance and class, app id, states), `workspaces()` (names and work
areas), `move_to_current_desktop(wid)`, `x_info()` (the compositor's own `DISPLAY`
and cookie path), `pointer()` and `events(timeout)`. These exist so that a backend
which knows more than a `Window` carries can hand it to `wwmctl`/`wxprop` without
those tools reaching into `SwayBackend._nodes()`.

**One hit-test, not three.** `Window.window_type` is filled in by
`GnomeBackend._win` and `KwinBackend._win`, and `backend.hit_test(wins, x, y)` is the
single implementation with the single `_LAYER_TYPES` table: it looks through DESKTOP
and DOCK layers for `getmouselocation`'s window field. There is no per-backend
`window_at` override and no `getattr` probe in `_window_under_pointer` any more. It
runs over `list()` and deliberately not over `views()`: `views()` is an extra round
trip and carries no workspace bit.

**One place decides how many round trips a listing costs**, and that is asserted:
`tests/test_backend_gnome.py` pins the bridge call list for a hit-test at exactly
`["ListWindows"]`.

### Window ids, per compositor

The id is what a script pipes around, so its semantics are per compositor and worth
knowing before writing one.

| backend | id | stable? | accepts an X id? |
|---|---|---|---|
| **sway / i3** (`backend_sway.py`) | the sway node id | for the life of the window | no — an X id is not a node id |
| **GNOME** (`backend_gnome.py`) | `Meta.Window.get_id()`, through the bridge | for the life of the window | XWayland windows also carry their real X id in `views()`, which is what `wwmctl -l` prints |
| **KDE** (`backend_kwin.py`) | minted: `0x40000000 \| 30 bits of internalId`, because the scripting API has no numeric window id at all | while the window lives | no. The range is deliberately outside the one Xwayland hands its clients, so a native id is never mistaken for an X id in the same listing |
| **wlr** (`backend_wlr.py`) | `1000000 + enumeration order` | within one run | no |

KWin's minting is 32-bit clean because every X-shaped consumer truncates there
(`wxprop -id` parses into an XID, the synthesized `_NET_CLIENT_LIST`, wmctrl's
`0x%08lx`), and two uuids colliding in those 30 bits re-mint the second window rather
than dropping it out of the listing.

The **XWayland id of a KWin 6 window** is the hardest case in the tree and is worth
reading before touching it: `x11window.h` lost every scriptable property in Plasma 6,
so `View.xid` is matched against the X server's own `_NET_CLIENT_LIST` — a pid and
`WM_CLASS` filter, then a title and geometry distance score, greedy best first. A
pair must *agree* on pid or class: an X client that publishes neither contradicts
nothing, and matching it on geometry alone would hand its id to a native window,
which would then claim to be an X11 client. Where nothing separates two candidates
they keep id 0 rather than being handed one of two ids. It runs only when an Xwayland
process already exists, because connecting to the X plane must not *start* one.

`sway`'s desktop mapping is the other one to know: wmctrl and xdotool want a dense
0-based desktop list and sway has sparse, named workspaces. `SwayBackend.workspaces()`
sorts on the raw `num` and computes `index = num - 1 if num > 0 else -1`, so a named
workspace and the scratchpad both land on `-1`, which collides with wmctrl's own
`-1` for "sticky". That is inherent to the mapping, and `-R` / `-t -1` sidestep it by
using sway's own "workspace current".

The per-backend measured detail — what each compositor does with maximize, shading,
raise, lower, ids, and every quirk that has a test pinning it — is
[WDOTOOL.md § Backend notes](WDOTOOL.md#backend-notes).

## 5. Input: the daemon, two injection paths, one layout decision

`wdotool/daemon.py` owns everything that injects. The first wdotool command of a
session double-forks it (`argv[1] == "__daemon"`), it creates the uinput devices once
(~600 ms of compositor hotplug latency, paid once), and it serves JSON lines on a
unix socket under `session.runtime_dir()`. Every later command is a client.

Three facts about it decide most of its code:

1. **A held key belongs to a connection.** The compositor releases whatever a client
   holds the instant that client disconnects, so `keydown ctrl` from a process that
   exits is not a hold at all. That is why there is a daemon and not a library.
2. **There are two sinks, and a hold cannot move between them.** `/dev/uinput` and
   the Wayland protocols are different devices, and only the device that pressed a
   key can release it. `_own_sink()` and `_own_pointer()` exist for exactly that.
3. **One policy picks the sink, for both halves**: `key`/`keydown`/`keyup`/`type` go
   through `zwp_virtual_keyboard_v1`, and
   `click`/`mousedown`/`mouseup`/`mousemove`/`mousemove_relative` through
   `zwlr_virtual_pointer_v1`, **when the matching kernel device cannot be opened and
   the compositor implements that protocol** — through `/dev/uinput` in every other
   case, with `--vkbd on|off` (`WDOTOOL_VKBD`) forcing either.

**And it does not live for ever.** A daemon that can no longer be reached — its
socket file deleted, which is what logging out does to `$XDG_RUNTIME_DIR`, or
replaced by a second daemon's — exits, and so does one nobody has used for fifteen
minutes; neither can touch a daemon with a client connected or a key held down. The
period, the check interval and the two off switches are in
[WDOTOOL.md § The input daemon](WDOTOOL.md#the-input-daemon).

**One layout box, from one source — except where one source cannot answer.** Every
absolute pointer coordinate is mapped across the layout bounding box the daemon reads
off the Wayland wire, so which *pixel space* that box is in decides where a move
lands. `zxdg_output_v1`'s logical geometry is that source, and it stays that
source in every state measured — including the one GNOME 46 state where it is stale
(Fractional Scaling switched on under an already-scaled monitor, after which Mutter
stops re-sending `logical_size`), because Mutter maps absolute pointer motion across
the same stale rectangle it is advertising. Reading the box from DisplayConfig there
instead, which is the obvious repair and was implemented and measured, lands every
target at twice the coordinate asked for. What that state really breaks is
`getdisplaygeometry`, which then describes a desktop that is not being drawn, and on
the wire it is byte for byte a legitimate physical-mode session where the same numbers
are right. So `wdotool/layoutbox.py` gates on the signature the two share (a head whose
logical size is its raw mode size while it claims `wl_output.scale` >= 2), asks
`org.gnome.Mutter.DisplayConfig` only then, and turns a disagreement into one
diagnostic rather than a different box. The gate is the point: the common session never
opens a bus, so the check cannot cost anything anywhere else.

**One layout decision, in one function.** `xkbmap.decide(text, group, mode)` is the
single answer to "which character table does this keystroke use?", and
`xkbmap.layout_mode(forced)` is the single answer to "which mode are we in?". Three
callers share them: `_Daemon._layout`, `keys_cmds.Layout.load` and
`xkbmap.diagnostic_main` (`wdotool __keymap`). `xkbmap.fetch(keymap=, group=)` takes
its overrides as keyword arguments rather than writing `os.environ`, which is what
made `WDOTOOL_LAYOUT=us wdotool __keymap --chars z` agree with `type` instead of
disagreeing with it.

The measured behaviour of all of this — the reverse map, the US bypass, the group
guess, the ceiling map for the absolute axis, the unchanged-`EV_ABS` nudge, the
`--clearmodifiers` kernel facts, and every defect the live sessions found — is
[WDOTOOL.md § The input daemon](WDOTOOL.md#the-input-daemon) and
[§ Typing and clicking with no privilege](WDOTOOL.md#typing-and-clicking-with-no-privilege---vkbd).
This file does not repeat it.

**One number parser.** `wdotool/cnum.py` is C's `atoi`/`atof`/`strtol` with C's
semantics (leading space, optional sign, stop at the first character that is not a
digit, and `[0-9]` rather than `\d`, so a Unicode digit gives 0 exactly as C does).
`wwmctl/core.py` and `wxprop/cli.py` import it aliased as `_atoi`. There is one copy,
and the parity tests are what keep it honest.

**One getopt wrapper.** `cli._opts` takes the command name and parses one command's
own flags out of the remaining argv, and every chainable command uses it.

## 6. Display: four backends, one shape

`wxrandr` has four Wayland backends and one handover:

| backend | protocol or interface | picked when |
|---|---|---|
| `sway` | sway/i3 IPC (`GET_OUTPUTS`, batched `output ...` commands in one `RUN_COMMAND`) | a sway or i3 IPC socket exists |
| `wlr` | `zwlr_output_management_unstable_v1`, one atomic configuration apply | the compositor advertises it |
| `mutter` | `org.gnome.Mutter.DisplayConfig` on the session bus, one `ApplyMonitorsConfig` | GNOME |
| `kwin` | `kde_output_management_v2`, with device objects found two ways | Plasma |
| `x11` | the real `xrandr`, by `execve` | an X11 session, or `--backend x11` |

**They are one shape.** Each implements `snapshot(state)`, `predicted_dims(t, state)`,
`verify(...)`, `apply(state, targets, persistent)`, `close()` and `name`, and
`cli.Session` holds exactly one of them in `self.impl`. There used to be four handles
and six name tests scattered through `Session`; there is now one attribute, and
"a backend is an object" is a true sentence about this code rather than an aspiration.

**One transform table.** `core.WL_SPEC_RANDR_VIEW` plus `to_wl_spec_transform` /
`from_wl_spec_transform` replace the twin tables that used to sit in `mutter.py` and
`kwin.py`. `core.RANDR_VIEW` remains as the alias the tests and
[WXRANDR.md](WXRANDR.md) already use.

**One mode resolver.** `core.match_mode` and
`core.resolve_real_mode(t, state, interlace_known)` are shared, and the
`interlace_known` flag is load-bearing rather than decorative: KWin's modes are
flagless and Mutter's are not, so a resolver that assumed either would pick the wrong
mode on one of them. There is a test for that divergence.

**Layout representations went from four to three.** What is on the screen is
`core.OutputState`; what was asked for is `core.Target`; what the user sees is the
rendered `--query` text. `wmirror` used to carry a fourth — its own `Output` class
and `outputs_from_heads()` — and now calls `snapshot_wlr(wlr, state=None)` instead,
which is what makes "wmirror can never disagree with `wxrandr --query`" a literal
statement about one code path rather than a claim about two.

**warandr keeps two of its own, and should.** `warandr/model.py:Layout` is the
canvas: a layout that has *not* been applied, with snapping, normalisation, clone
detection and an `overlap_refusal` sentence supplied by whichever backend is live.
`warandr/xrandr_parse.py` is the other: the text an `xrandr` or `wxrandr` invocation
renders, which is also the format of `~/.screenlayout/*.sh` and therefore has to
round-trip byte-identically, arandr's own files included. They cannot be one, because
one is an editable model of a screen that does not exist yet and the other is a
serialisation contract with a program from 2010.

### GNOME's saved display configuration is all or nothing

`~/.config/monitors.xml` holds one `<configuration>` per monitor set the user has ever
kept — the laptop alone, the laptop plus the desk monitor, the three heads at work —
and GNOME applies the entry whose monitors are plugged in. Mutter's **reader** verifies
every entry in the file with the same `meta_verify_logical_monitor_config_list()` that
`ApplyMonitorsConfig` uses, and **one failure discards the whole file**. Measured on
the 26.04 default install (GNOME 50.1, `resolute-gnome-iso`, three heads), on a file
holding a three-head layout and a two-head one, with one `<x>` in the two-head entry
edited so that its monitors overlap:

```console
$ journalctl -b | grep 'monitors config'
Failed to read monitors config file '/home/test/.config/monitors.xml': Logical monitors not adjacent
$ wxrandr --query | grep '^Virtual'      # at the next login
Virtual-1 connected primary 1920x1080+0+0      # Mutter's default row -- not the saved
Virtual-2 connected 1920x1080+1920+0           # three-head layout, which is still in
Virtual-3 connected 1920x1080+3840+0           # the file, untouched and perfectly valid
```

Nothing says so: no window, no notification, nothing on any screen. The file stays on
disk exactly as it was and every layout in it is inactive, at every login, until
somebody edits it back by hand. Mutter's **writer** verifies nothing —
`meta_monitor_config_manager_save_current()` serialises whatever configuration is
current — so a file in that state can be written by anything that reaches libmutter
without going through DisplayConfig (GNOME on Xorg derives its logical monitors from
the X layout with no verification at all; a Shell extension can ship its own typelib
and call the symbols DisplayConfig does not export). And the next confirmed save
rewrites the file **whole**, from what Mutter holds in memory, which after a discarded
read is only the layout being saved: that is the moment the other monitor sets stop
being recoverable. Measured, same rig: our confirmed `--persistent` on top of a
discarded file left one `<configuration>` where there had been three.

**No path through our own tools can put a bad entry in that file**, and that is
measured rather than argued — every route tried on both default installs (GNOME 50.1
on 26.04 and GNOME 46.0 on 24.04, three virtio heads, the "Keep changes?" dialog
confirmed with `wdotool key Return`, `sha256sum` on the file after every step):

| what was tried | what happened | the file afterwards |
|---|---|---|
| overlapping `--pos`, `--persistent` | refused: `Logical monitors not adjacent` | unchanged, byte for byte (absent on a fresh account, and still absent) |
| a gap in the row, `--persistent` | refused, same sentence | unchanged |
| a vertical overlap, `--persistent` | refused, same sentence | unchanged |
| `--same-as` between two different modes, `--persistent` | refused by us, before the bus | unchanged |
| a valid layout, `--persistent`, dialog left alone | applied, reverted after 20 s | **never written**: Mutter writes only on the confirmation |
| a valid layout, `--persistent`, the session killed while the dialog was up | the old layout at the next login | never written |
| a valid layout, `--persistent`, confirmed | applied | Mutter writes it: the entry for *this* monitor set is replaced, every other entry survives byte for byte |
| a second monitor set (one head unplugged), confirmed | applied | a second `<configuration>` appended; both verify, and plugging the head back in restores the first |
| `warandr`: Apply, from the GUI, of a layout loaded from a saved script | applied through `wxrandr`, no `--persistent` | unchanged |
| `warandr`: Save As / `--save` | writes a layout **script** (`~/.screenlayout/*.sh` shape) | unchanged; warandr never writes this file |
| any apply without `--persistent` | applied | unchanged, and the file is not even opened |

The asymmetry that makes this safe is Mutter's own: `ApplyMonitorsConfig` validates on
*every* method, method 0 included, so the only layout that can reach the writer is one
the validator has already accepted, and the writer runs only after the user confirms.
A refused `--persistent` reaches neither.

**One thing can still rot a file we caused to be written, and it is not a layout
error.** With Fractional Scaling off — GNOME 46's default — the session is in physical
layout mode, where a scaled monitor keeps its pixel width, and Mutter writes the file
with no `<layoutmode>` element at all. Turn the setting on and the same numbers are
read as logical pixels, where a `--scale 2` head is half as wide as the gap its
neighbour was saved at. Measured on 24.04, a three-head row with the scaled head first
and a two-head set saved beside it:

```console
$ gsettings set org.gnome.mutter experimental-features "['scale-monitor-framebuffer']"
$ # ... reboot ...
$ journalctl -b | grep 'monitors config'
Failed to read monitors config file '/home/test/.config/monitors.xml': Logical monitors not adjacent
```

Both entries gone. The layout was valid, Mutter validated it, Mutter wrote it; what
changed is what the numbers mean. GNOME Settings' own 200% scaling writes exactly the
same file, so this is not a wxrandr defect — but `--persistent` is the moment the user
chooses to save, and it is the moment to say so.

**What `wxrandr --persistent` therefore does** (`wxrandr/monitors_xml.py`, about 200
lines, none of it reached by a temporary apply):

1. reads the file before the apply and prints one line when Mutter has already
   discarded it — with Mutter's own verifier, in Mutter's order (adjacency first, so
   the sentence matches the one in the journal), judging an entry that names its layout
   mode in that one and an entry that does not in the session's;
2. warns, before the dialog, when the layout being saved is one that a later
   Fractional Scaling change would break — only in physical layout mode, only when
   something is scaled, and only when the row really does come apart in the other mode;
3. copies the previous bytes to `monitors.xml.wxrandr-backup` once Mutter has accepted
   the layout, so that a rewrite that drops the other monitor sets is recoverable. A
   refused apply copies nothing. GNOME keeps one generation of its own in
   `monitors.xml~` (glib writes it when Mutter replaces the file), but every save
   overwrites that one, including the save that does the damage.

It never writes `monitors.xml` itself. `tests/test_monitors_xml.py` holds the verifier
and the copy against real files from both releases, and
`tests/test_wxrandr_mutter.py:SavedConfigurationFile` holds the invariant end to end:
every refusal leaves the file byte-identical and writes no copy, an accepted persistent
apply writes the copy and still does not touch the file, and a temporary apply does not
open it — that last one enforced by making the reader explode if it is called.
### Why Mutter refuses monitors that share area

The one geometry the four backends do not agree on, and the long form the README and
the two contracts point at. Read and measured against **stock GNOME 46.0 and 50.1**,
three virtual heads, nothing patched.

**One validator, on the way in.** `meta_verify_logical_monitor_config_list()`, in
`src/backends/meta-monitor-config-utils.c`, walks the logical monitors a client
submits and requires each one to share an edge with another by *exact integer
equality*. Adjacency is tested before anything else, which is why one sentence,
`Logical monitors not adjacent`, comes back for a gap and for an overlap alike, and
why `Logical monitors overlap` needs a layout in which adjacency already holds. It is
not a permission check: gnome-control-center's Displays panel is a D-Bus client like
wxrandr and reads the same refusal.

**Nothing else in the compositor needs the invariant.** Read at both versions:

* monitor lookup by point returns the **first match**, not a unique one;
* lookup by rectangle **falls back to the primary** when nothing wins;
* pointer constraints clamp only when the pointer is in **no view at all**, not when
  it is in two;
* the screen size is a **bounding box**, computed the same way either way;
* the renderer already builds **several stage views over identical rectangles**,
  because that is exactly how mirroring is drawn.

**And Mutter already holds overlapping logical monitors in practice.** GNOME on Xorg
derives them from the X layout with no verification at all, so a plain `xrandr
--output B --pos 960x0` on a GNOME/X11 session puts an overlapping set inside the
very same data structures. The invariant is enforced at one door, not required by the
building, which is what makes this a limitation rather than a law of nature, and why
the identical layout is taken as drawn by KWin, by wlroots and by X.

**Every supported route in is closed, and one is worse than closed.**

| route | what happens |
|---|---|
| `ApplyMonitorsConfig` (D-Bus) | validates **before** it applies, on every method: 0 verify, 1 temporary, 2 persistent. `--dryrun` therefore gets exactly the answer an apply would |
| `~/.config/monitors.xml` | the parser calls the **same verifier**, and a failure discards the **entire file** — see the warning below |
| a GNOME Shell extension | can reach the non-introspected libmutter symbol by shipping a typelib of its own. Measured working on **both** versions, shared region byte-identical. It also encodes a private struct offset and the library SONAME, and a wrong offset **writes into the compositor's heap** rather than raising an error, which on Wayland means the user loses the session at login, repeatedly, with the extension already enabled. Not shipped, not going to be |

> **The one warning worth its own line: never hand-edit `monitors.xml` to force an
> overlap.** Mutter discards the whole file on any error, so one bad entry silently
> destroys every other monitor arrangement the user had saved, on every boot, and the
> only trace is a line in the system journal. Nothing warns at the time: the session
> comes up with a layout Mutter has built from scratch, and every arrangement that
> user had saved is gone.

**The safety fact behind that warning: Mutter's own writer does not validate.**
`meta_monitor_config_manager_save_current()` will happily write an overlapping layout
into the file that the reader then rejects in full, for ever. Reader and writer
disagree, and the disagreement is silent and permanent, which is the real reason
nothing in this tree writes that file itself: `--persistent` asks *gnome-shell* for
its "Keep changes?" dialog and lets Mutter write, and that is the only route we take
([WXRANDR.md](WXRANDR.md#mutter-backend-wxrandrmutterpy)).

**So what the tools say instead.** wxrandr keeps passing the layout on unchanged and
attributing the refusal to Mutter by name; nothing here pretends to a workaround. The
substitute the documents offer is a **mirrored region**, and it is never called an
overlap: GNOME will not place two monitors so that they share area, and the closest
thing available is a region whose pixels match exactly, where the copy takes the
clicks that land on it rather than passing them to the window they came from, and
where a copy made by capture rather than by the layout lives only as long as that
capture session, which a screen lock ends. Whole-monitor mirroring is in the layout
on GNOME (`--same-as`: one logical monitor, several members, identical mode, rotation
and scale); a region is `wmirror` on wlroots ([WMIRROR.md](WMIRROR.md)), and on GNOME
and KDE only through the desktop portal, which prompts once per session and is
therefore useless from a hotkey.

## 7. Detached children, runtime paths and stdio

**One detach protocol.** `fwcommon/procs.py` is the whole of it, and both callers use
it: `wxrandr/gamma.py`'s holder, which keeps a `zwlr_gamma_control` alive for as long
as a brightness is set, and `wmirror/supervise.py`, which owns one `wl-mirror`.

    proc_starttime  zombie  owned_by_us  alive  wait_gone  kill_bounded  emit
    spawn_detached(child_main, deadline, on_line)

What it promises, and why each promise has code:

* **double-fork plus `setsid`**, so nothing is left in our process group and the
  child survives the shell, and the terminal, that started it;
* the child writes `pid <pid> <starttime>` up a status pipe **before it can fail**,
  and the parent acts on that line the moment it arrives. `on_line` fires **as lines
  arrive**, not when the start finishes, because a start that hangs or that the user
  interrupts halfway through must still leave a record naming a process something
  later can stop. An orphan nobody can end is the one outcome this protocol exists to
  prevent, and buffering the lines would produce it. There is a test that interrupts
  a start with a KeyboardInterrupt and then finds and stops the child.
* **liveness is `(pid, starttime)` read out of `/proc`**, so a recycled pid is never
  mistaken for the process we started, and the euid check means nothing is signalled
  that is not ours;
* **every kill is bounded** — SIGTERM, wait, SIGKILL, confirm — and never
  fire-and-forget.

`daemon._spawn` deliberately does *not* use it: the input daemon has its own
readiness handshake, its own cgroup escape and its own re-exec, and folding those in
would make one function serve two contracts.

**One runtime directory.** `session.runtime_dir()` returns `$XDG_RUNTIME_DIR` when
there is one, else the verified-0700 `/tmp/wdotool-<uid>`. Four things live there and
all four went through it in 0.3: the daemon socket, `backend_kwin`'s script lock,
`wxrandr`'s state file and `wmirror`'s state file (the last two degrading to today's
`/tmp` name on a `CmdError`). The consequence is the point: under `sudo` or from
cron, those files are private to their owner rather than world-readable in `/tmp`.
The directory *name* is pinned by `tests/test_hardening.py`.

**One stdio rule, for all six tools.** `fwcommon/stdio.py:flush_stdout(prog)` is the
last thing every `main()` does: flush, **close**, and one `prog: message` line on
stderr if the flush failed. Output that never reached its reader makes the exit
status 1, whatever the command itself decided. A reader that closed a pipe is silent,
because the originals die of SIGPIPE without a word. `repair_std()` at the top of
each `main()` is the other half: an fd 1 or 2 closed before the interpreter started
(`>&-`) leaves `sys.stdout` as `None`, and the work still gets done. The result is
that no tool here prints a traceback and none exits 120, which is the interpreter's
own "the exit-time flush of stdout failed". `stdio.warn()` is the same trick for the
other stream: every line a `main()` writes as its last word goes through it, because
`tool >/dev/full 2>&1` is a real case (a cron job whose log filled the disk) and the
diagnostic about the lost output cannot land there either. It swallows the failure and
**closes** stderr, since unwritten bytes left in *that* buffer are flushed again on the
way out and make the status 120 exactly as stdout's do.

## 8. The environment

Eighteen rows below, and rather more names than rows, because some of them group.
All but seven are also written down in the README, in a tool contract or in
`gnome/README.md`; the seven in **bold** are written down only here.

| variable | read by | effect |
|---|---|---|
| `FUCKWAYLAND_PASSTHROUGH` | all six | `never` runs our own code whatever the session, `always` hands over whatever the session. `WDOTOOL_PASSTHROUGH`, `WWMCTL_PASSTHROUGH`, `WXPROP_PASSTHROUGH` and `WXRANDR_PASSTHROUGH` do the same per tool |
| `WDOTOOL_REAL_XDOTOOL` and friends | `passthrough` | where the original is. Also `WWMCTL_REAL_WMCTRL`, `WXPROP_REAL_XPROP`, `WXRANDR_REAL_XRANDR`. Set but unusable is an error naming the variable, never a silent fallback |
| `WDOTOOL_LAYOUT`, `WDOTOOL_XKB_GROUP`, `WDOTOOL_XKB_KEYMAP` | the daemon | the character table, the layout group, a keymap from a file |
| `WDOTOOL_VKBD` | the daemon | `auto` / `on` / `off`, for both injection halves |
| `WDOTOOL_SYNC_TIMEOUT`, `WDOTOOL_REL_MODE`, `WDOTOOL_SELECT_TIMEOUT` | `wdotool` | the `--sync` deadline (`0` waits for ever), `abs`/`rel` for relative pointer moves, and how long KWin's window picker waits (2 minutes by default; GNOME's picker is the bridge's own, capped at 30 s inside the extension, and reads no variable) |
| `WDOTOOL_BACKEND` | `backend_detect` | force a window backend, ahead of detection |
| `WXRANDR_BACKEND`, `WXRANDR_PERSIST` | `wxrandr` | force a display backend (`--backend` beats it); make `--persistent` the default |
| `WWMCTL_WMCTRL_GENERATION` | `wwmctl` | `1.07` or `git`: which upstream `--help` text to print, instead of consulting the installed oracle |
| **`WWMCTL_NO_X`** | `wwmctl.core` | do not open the X plane at all. The listing then carries compositor ids and no X enrichment — how the "no X server" path is exercised without taking one away |
| **`WXPROP_NO_X`** | `wxprop.core` | the same for wxprop: never resolve an X plane, so `-root` answers from the compositor's synthesized set |
| **`WXPROP_ARGV0`** | `wxprop` | the program name in usage and error lines, overriding `argv[0]`. Real xprop prints the name it was invoked under, and `python -m wxprop` has none to print |
| **`WDOTOOL_UINPUT_PATH`** | `wdotool.uinput` | the device node to open, default `/dev/uinput` |
| **`WDOTOOL_FAKE_UINPUT=1`** | `wdotool.uinput` | skip the ioctls, so a regular file can stand in for the device. This is what lets the daemon's event stream be asserted byte for byte in a container that has no `/dev/uinput` |
| `WDOTOOL_DAEMON_IDLE`, `WDOTOOL_DAEMON_CHECK` | the daemon | seconds with no client before it exits (900), and how often it looks (15, and the same tick checks that its socket is still there). `0` is never; `CHECK=0` turns both checks off |
| `WDOTOOL_NO_KEYSTATE=1` | the daemon | force the `keystate.py` path (the foreign-modifier diagnostic) even where the `/dev/input` read would be skipped |
| `WDOTOOL_GNOME_AUTOLOAD=1` | `backend_gnome` | opt in to one `org.gnome.Shell.Eval` that tries to load the installed extension. Eval is a privileged interface and this is off by default |
| **`XPROPFORMATS`** | `wxprop.cli` | a format file, exactly as real xprop's `-fs` and `$XPROPFORMATS` do |
| **`DEBUG`** | `wdotool` | set to anything: print the traceback instead of the one-line error. Every `main()` catches broadly, which is right for users and wrong for whoever is debugging |

`WARANDR_TEST_*`, `FAKE_XRANDR_*`, `FAKE_REAL_*`, `FW_SHIM_SEAMS`, `WD_TEST_*` and
`SLOW_QUERY` are test seams and are documented where they are used, in
[WARANDR.md § Test hooks](WARANDR.md#test-hooks-env) and in the test files
themselves. They are not part of the interface.

## 9. Module → test file → fake

2371 tests, run as `python3 -m unittest discover -s tests` or file by file. Two rules
hold across all of them and are enforced by tests of their own:

* **every `tests/test_*.py` sets `FUCKWAYLAND_PASSTHROUGH=never`**, or the suite
  would `execve` itself away on an X11 box and the parity oracle would compare the
  real xdotool with itself and pass tautologically. `tests/test_passthrough.py` fails
  when a test file is missing the line.
* **no test leaves a daemon running.** One that spawns a real daemon stops it
  through `support.stop_daemons_under()`, registered before the spawn so it runs
  however the test ends and before the runtime directory goes away;
  `tests/test_zz_daemon_leak.py` runs last and fails the suite over anything still
  alive that was not there when it started. The suite is how the rig came to have
  161 of them.
* **shared helpers live in `tests/support.py`**, deliberately not named `test_*.py`
  so the escape-hatch guard above skips it. It holds `RecorderDev` and `abs_report`
  (the plain 3-tuple shape every uinput assertion uses), `env()`, one merged
  `FakeEvdev`, and `HeadlessSway` for the XWayland live files.
  `tests/wl_fake.py` is the other one: the Wayland marshallers and a `Server` base,
  deliberately **not** built on `wayland_mini`, so a bug in the client cannot hide
  itself in the fake.

| what it covers | test files | what stands in for the world |
|---|---|---|
| `cli.py`, `commands.py`, `misc_cmds.py` | `test_cli_chain`, `test_cli_misc`, `test_cli_script`, `test_cli_parity` | the real `xdotool` binary as the byte oracle (skipped outside `nix develop`) |
| `window_cmds.py`, `desktop_cmds.py` | `test_windows_cmds`, `test_windows_sway` | `FakeBackend` in-memory; a real headless sway |
| `input_cmds.py`, `daemon.py`, `uinput.py` | `test_input_cmds`, `test_input_daemon`, `test_input_uinput`, `test_daemon_lifetime`, `test_torture_regressions`, `test_hardening` | `FakeDaemon`, `RecorderDev`, and `WDOTOOL_FAKE_UINPUT=1` writing into a regular file |
| `vkbd.py`, `vptr.py` | `test_vkbd`, `test_vptr` | `wl_fake.Server`: a real unix socket speaking the Wayland wire format |
| `xkbmap.py`, `keymap.py`, `us_keymap.py` | `test_xkbmap`, `test_keymap`, `test_layout_flag` | `tests/fixtures/keymaps/*.xkb`, each a byte-for-byte capture of what a compositor handed a client, from GNOME, sway and KWin |
| `layoutbox.py` | `test_scale_spaces` | a wl_output/xdg_output fake replaying one measured scaling state per test, and `FakeMutter` on the mock bus as the second source |
| `keys_cmds.py` | `test_keys_cmds` | recorded evdev streams |
| `backend_gnome.py` | `test_backend_gnome`, `test_wwmctl_gnome`, `test_wxprop_gnome` | `MockBridge` on `dbus_mini`'s in-process mock bus |
| `backend_kwin.py`, `kwin_js.py` | `test_backend_kwin` | a fake KWin on the same mock bus, answering `loadScript`/`run`/`unloadScript` |
| `backend_sway.py` | `test_windows_sway`, `test_wire_hardening` | real sway; `FakeSway` for the hostile cases |
| `backend_wlr.py` | `test_backend_wlr` | a wire-level foreign-toplevel fake |
| `x11_mini.py` | `test_wwmctl_x11`, `test_wxprop_x11`, `test_wwmctl_hardening` | `FakeXServer`, and `HostileXServer` subclassing it |
| `fwcommon/session.py` | `test_session`, `test_session_discovery` | a temporary `/run/user` tree |
| `fwcommon/passthrough.py` | `test_passthrough`, `test_passthrough_exec` | a hermetic detection matrix; then a fake install tree with real processes |
| `fwcommon/dbus_mini.py` | `test_dbus_mini` | byte-exact fixtures plus an in-process `MockBus`, and a real `dbus-daemon` when `DBUS_SESSION_BUS_ADDRESS` is set |
| `fwcommon/wayland_mini.py` | exercised by every wire test above | `wl_fake` |
| `wwmctl/` | `test_wwmctl_cli`, `test_wwmctl_live`, `test_wwmctl_hardening`, `test_wwmctl_gnome` | `FakeSwayBackend`, `FakeX11`; real sway with XWayland for the live file |
| `wxprop/` | `test_wxprop_cli`, `test_wxprop_fmt`, `test_wxprop_live`, `test_wxprop_gnome`, `test_wxprop_x11` | captured real-xprop bytes; a live XWayland server as the oracle |
| `wxrandr/` | `test_wxrandr_unit`, `test_wxrandr_backend`, `test_wxrandr_mutter`, `test_wxrandr_kwin`, `test_wxrandr_live`, `test_wxrandr_hostile`, `test_wxrandr_gamma`, `test_monitors_xml` | `FakeMutter` on the mock bus; a wire-level fake KWin; real sway with real `xrandr` through XWayland as the oracle; real `monitors.xml` files from both default installs |
| `warandr/` | `test_warandr_model`, `test_warandr_parse`, `test_warandr_gui` | `tests/fixtures/fake_xrandr.py`, a RandR simulator; Xvfb plus xdotool driving the real editor |
| `wmirror/` | `test_wmirror_cli`, `test_wmirror_lifetime` | a fake `wl-mirror` binary, and the detach protocol driven for real |
| `procs.py`, `stdio.py` | `test_wmirror_lifetime`, `test_stdout_gone` | real forks; `>/dev/full`, `\| head -1`, `>&-` |
| the no-dialog guarantee | `test_no_portal` | nothing — it is a static check that no package here names the portal or PolicyKit |

Two environments run these. **In the development shell** (`nix develop`), a container
with no `/dev/uinput`, `WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 sway` gives a
real Wayland compositor for the backend and protocol tests, with `swaymsg`, `foot`,
`grim`, XWayland and the real `xdotool`, `wmctrl`, `xprop` and `xrandr` alongside it
as the byte oracles. **On the rig** (`vm/vmctl`), the same tools run against real
desktops with real input, which is where `WLR_BACKENDS=headless,libinput` matters:
libinput has to be listed or sway does not pick up the uinput devices at all.

## 10. The VM rig

`vm/` is where every "it works on GNOME" sentence in this repo comes from.
`vm/vmctl` builds and runs **twelve golden images**: ten built from an Ubuntu *cloud*
image plus a desktop metapackage (four desktops over three releases, Plasma twice per
LTS so that Wayland and Xorg are both covered), and two — `resolute-gnome-iso` and
`noble-gnome-iso` — installed from `ubuntu-26.04.1-desktop-amd64.iso` and
`ubuntu-24.04.4-desktop-amd64.iso` **by the Ubuntu installer itself**, unattended,
with every question left alone. The ten exist because one script gets four desktops
out of them. The two exist because "it works out of the box on a default Ubuntu
desktop" is a claim about an *installed* system, and a cloud image plus
`ubuntu-desktop` measurably is not one: 226 packages a real 26.04 desktop install
does not have, 55 it has and the cloud image has not, a different kernel with no
firmware at all, 8 snaps against the default 13.

Each image autologins user `test` on a multi-head virtio-vga whose monitors can be
plugged, unplugged and resized from the host at run time, with host-side screenshots
of every head. That is what makes a multi-monitor claim testable at all.

**`vm/selftest.sh` proves the rig, not the tools.** It asserts that the flavor came
up the way the flavor says it should: the right display manager, the right session
type, autologin landed, the heads are there, the compositor is painting,
`vmctl user` reconstructs an environment in which the desktop's own tools work. It is
the check you run after building an image and before believing anything measured on
it. The tools' own behaviour per flavor is the separate table in
[vm/README.md](../vm/README.md), *What the six tools do on each flavor*, and that is what
the README's support matrix is a summary of.

[vm/SETUP.md](../vm/SETUP.md) is how to stand the rig up on a machine of your own, and
`vm/setup-host.sh` does the mechanical part of it.

### The no-dialog measurement

The README's [no authorization dialog](../README.md#no-authorization-dialog) is a
claim about six of these images: GNOME 46 and GNOME 50 (the 26.04 default install off
the ISO among them) and Plasma 5.27, 6.6 and 6.7, with every command run three ways,
as root, as a plain user with the udev rule, and as a plain user with neither.
Watching throughout: the session bus for portal traffic, the system bus for polkit
`CheckAuthorization` and `BeginAuthentication`, the window list for windows we did not
open, and both screens compared pixel by pixel around every command. On all six, and
for the installer and the udev rule as well as the tools: **no prompt, no window we
did not open, and not one portal call from anything of ours.** The same rig pointed at
a real portal client and at `pkexec` produced both dialogs on every image, so it does
see one when there is one. `tests/test_no_portal.py` is the static half of the same
guarantee, and it runs everywhere.

## 11. Installing: what each route costs

The [README](../README.md#install) is the guide, and this is what stands behind it:
what the .deb does that a pip install does not, why each line of the pip route is the
line it is, and what a venv under `$HOME` costs when somebody else has to run the
tools. All of it was measured on a default Ubuntu 24.04 and a default 26.04 desktop
installed from the release ISOs, `noble-gnome-iso` and `resolute-gnome-iso`.

### The .deb

**One** `Architecture: all` package for **both** Ubuntu 24.04 and 26.04. Every module
here is pure standard library, so it lands in the version independent
`/usr/lib/python3/dist-packages` and your own `python3` byte compiles it at install
time, 3.12 on 24.04 and 3.14 on 26.04, from the same file. In the box: the six tools
in `/usr/bin`, the GNOME bridge extension system wide, the udev rule applied at once
with no reboot, and the `warandr` menu entry.

It does **not** replace the real `xdotool`, `wmctrl`, `xprop` or `xrandr`. Not one
path it ships is owned by their packages, so the X11 handover keeps finding them and
the symlinks over the originals stay the user's choice.

`sudo apt remove fuckwayland` takes the extension, the udev rule and the six commands
with it, `/dev/uinput` goes back to `root:root 0600`, and the session in progress
keeps running. `sudo apt purge fuckwayland` drops the last of its bookkeeping. All of
that paragraph and the one above it was run on a default Ubuntu 26.04 desktop and
written down command by command in
[vm/README.md § The package on a default
install](../vm/README.md#the-package-on-a-default-install).

Alongside a pip install of the same source, the two do not fight. The
`/usr/local/bin` symlinks the pip route makes keep winning for the six names, because
`/usr/local/bin` comes first on the Ubuntu `PATH`, and the package owns nothing under
`/usr/local`. One thing to know if the clone is what you work on: inside a
`--system-site-packages` venv, an editable install loses to the packaged modules, so
`import wdotool` finds the packaged copy. `debian/README.Debian` has the detail and
the one line that gets you back to the clone.

The built package is in the repository, at `release/fuckwayland_<version>_all.deb`,
so a clone is already installable and `git log release/` is the record of every
binary that shipped. `scripts/build-deb.sh` writes it there and replaces the one it
finds, dropping any package left over from an older version so exactly one file is in
the tree and the README can name it. What the build also produces and the repository
does not keep is the `.changes` and the `.buildinfo`: a `.buildinfo` is a description
of the machine that ran the build, down to the version of every package installed on
it, which is a build record rather than something anyone installs. Those go to
`dist/`, which `.gitignore` excludes along with the six zipapps of
`scripts/build-pyz.sh`, the `__pycache__` trees, the nix `result` links and the VM
images. Committing a binary is only tolerable because this one is
reproducible: `dpkg-buildpackage` timestamps every member from `debian/changelog`,
so two builds of the same source give the same bytes (measured: identical SHA-256
over two runs), and `git status` stays clean after a rebuild that changed nothing.

`scripts/build-deb.sh` installs its own build tools from the Ubuntu archive on first
run, nothing from a PPA:

```
dpkg-dev debhelper dh-python pybuild-plugin-pyproject python3-all python3-setuptools
```

Pass `--no-deps` to install them yourself instead. What goes where, and why the
extension and the rule are handled the way they are, is `debian/README.Debian`.

### Why the pip route reads the way it does

* **Why a virtual environment.** Ubuntu 24.04 and newer mark the system Python as
  externally managed, so a plain `pip install -e .`, with or without `--user`,
  refuses with `error: externally-managed-environment` and points at a venv, at pipx,
  or at `--break-system-packages`. Those are the honest options, all of them work,
  and a venv is the one that changes nothing outside its own directory.
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

  The other five tools are stdlib only and never notice.
* **Why `-e`, and keep the clone.** pip installs the six commands and nothing else.
  Not `gnome/install-bridge.sh`, not the udev rule, not `warandr.desktop`. Those are
  used from the clone, so keep it where it is and let the editable install point at
  it.

`pip install -e .` fetches setuptools, so it wants network. **On 26.04** a
`--system-site-packages` venv can use the system copy instead, because a default
install ships `python3-setuptools` 78:
`~/.venvs/fuckwayland/bin/pip install --no-build-isolation -e .` **On 24.04 that
shortcut does not work** and the ordinary line is the one to use: a default 24.04
desktop has no `python3-setuptools` at all (`ModuleNotFoundError: No module named
'setuptools'`), and where it is installed it is setuptools 68, which still needs the
separate `wheel` package (`error: invalid command 'bdist_wheel'`). Both measured on
24.04 default and cloud images, see [vm/README.md](../vm/README.md).

### A venv under `$HOME`, and other accounts

Ubuntu home directories are `0750`, so a venv under one is readable by its owner and
root only. Symlinks in `/usr/local/bin` that point into it break for *other* users,
and the message they get does not say why: `sudo` reports `unable to execute
/usr/local/bin/wdotool: Permission denied` on 24.04 and `sudo:
'/usr/local/bin/wdotool': command not found` on 26.04. For a machine wide drop-in use
one venv under `/opt`, and note the missing `-e` there: an editable install keeps
reading the source tree at run time and hits the same wall, while from a readable
copy it does not. A symlink left behind after the venv is deleted just says `No such
file or directory`, so remove the links when you remove the install.

### What the default installs said about the guide

The install guide was run on both ISO installs *verbatim*, as a reader would:
`sudo apt install git python3-venv`, clone, venv, `pip install -e .`, the
`/usr/local/bin` symlinks, `gnome/install-bridge.sh`, one logout, `--udev`. Every
command worked as written on both, and the stock facts the guide leans on hold on a
real default install of either release (no pip, no venv, no pipx, no `git`, no
`curl`, while `python3-gi`, `gir1.2-gtk-3.0`, `acl`, `x11-utils` and
`x11-xserver-utils` are all present, so `warandr`'s GUI comes up with nothing extra
installed). All six tools then behaved **identically to the matching cloud image
flavor**, as the desktop user and as root over ssh with an empty environment. The
24.04 run corrected three sentences of the guide: the *optional*
`--no-build-isolation` line, the bare `python3 -m venv` error, and the
`wxrandr --print-backend --verbose` block in *Check it worked*, which had been one
line short of what the tool prints since the day it was written.

Two things about a default install are worth knowing before trusting a script on one,
and neither is visible on the cloud image flavors, which switch both off:

* **it locks itself.** `idle-delay 300` and `lock-enabled true` are the defaults on
  24.04 and 26.04 alike, and GNOME Shell disables extensions behind the lock screen,
  so five idle minutes turn every window command into `gnome backend: the fuckwayland
  bridge is unavailable while the screen is locked`, rc 1 (rc 2 for `wdotool`). It
  also switches the outputs off, so a screenshot taken then is black. `wxrandr`,
  `warandr` and input injection keep working, and injecting the password is a way
  back in. Five minutes is less than the guide itself takes: on the 24.04 default
  install `sudo apt install git python3-venv` alone ran for three and a half minutes
  and the session was locked by the time the bridge was installed.
* **it has no `xdotool` and no `wmctrl`**, so the X11 handover has nothing to hand to
  until `sudo apt install xdotool wmctrl`. On a Wayland session nothing hands over,
  so this only bites on an X11 session or under `FUCKWAYLAND_PASSTHROUGH=always`.

## 12. The threat model in full

The [README](../README.md#threat-model) states this in short. Here it is with the
mechanics, because "granted once and standing" is the design and not an accident.

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
  Flatpak and Snap apps without session bus access cannot reach it.
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
  and the lock-screen note applies to these routes as much as to the kernel one.
  Mutter and KWin implement neither, so nothing changes there.
* **KDE needs nothing installed**, which is itself the note: any client of a Plasma
  session bus can already load a script into KWin, with or without us.
* **Running as root** (`sudo wdotool`) is the alternative to the udev rule. Then the
  tools find the graphical session by scanning `/run/user/*` and logind, and talk to
  that user's compositor as root.

**What is never asked at run time.** Nothing here uses the desktop portal, so GNOME's
and KDE's *Remote Desktop* consent dialog never appears, and nothing here uses
PolicyKit, so no polkit agent window does either. That is a deliberate choice, and
this section is its cost: with no per-use prompt, everything is granted once and
standing, by the bullets above, whether that is the udev rule, the bridge extension
or `sudo`. The only prompt any of it can raise is GNOME's *Keep these display
settings?*, on an explicit `wxrandr --persistent`.

**What is deliberately not defended against.** Anyone who can already run code as
you: they can type through the daemon, read the same files and talk to the same
buses, and a same-uid boundary is not one we can enforce, so we do not pretend to. A
hostile compositor (you are already inside it). The lock screen: injected keystrokes
reach it, because the kernel does not know they are injected. Scripts you saved and
run later (`warandr`'s layout scripts are shell scripts, so read one before running
it, as with any script). And nothing here is a sandbox: the tools do not confine what
a command they hand over to, `xdotool` on X11, then does.

**What is defended against**, and stays that way: another local user. The daemon
socket, its lock and the wxrandr state file are private to their owner and validated
before they are believed, the daemon refuses to talk to a socket somebody else is
listening on, a state file that is not ours is ignored rather than obeyed, the
real-tool search never looks in the current directory, and a root run with no session
never hands a planted X server another user's cookie.
