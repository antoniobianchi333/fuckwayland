"""Discovery of the graphical session's sockets, tolerant of running under sudo
(where XDG_RUNTIME_DIR etc. point at root's empty runtime dir or are unset).
FROZEN — edit only if broken.

Additive fixes (gnome-bridge), each broken on a stock GNOME box:

* `ssh root@box` (no sudo): pam_systemd gives root its own
  XDG_RUNTIME_DIR=/run/user/0 with a `bus` in it, so the old "trust
  $XDG_RUNTIME_DIR first" order found root's empty bus and no compositor.
  Candidate runtime dirs are now anchored on the graphical session: a dir
  that holds a `wayland-*` socket sorts before one that does not, then the
  sudo/pkexec invoking user, then real users, then the rest. $XDG_RUNTIME_DIR
  still wins when it has a Wayland socket (the normal in-session case, and
  the test rigs' private runtime dirs).
* Likewise `find_user_bus()`: root's own `DBUS_SESSION_BUS_ADDRESS`
  (pam_systemd exports `/run/user/0/bus` to an `ssh root@` login) only wins
  when that bus lives next to a Wayland socket; otherwise the scanned bus of
  the graphical session is used.
* `PKEXEC_UID` is honoured next to `SUDO_UID`.
* The X plane of a GNOME session (Xwayland) is found with `find_x_display()`
  (DISPLAY: $DISPLAY, gnome-shell's own environment via /proc, or the
  session user's /tmp/.X11-unix/X* socket) and `find_xauthority()`
  ($XAUTHORITY, Mutter's $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.* cookie,
  GDM's xauth_*, ~/.Xauthority). The GNOME backend asks the bridge first
  (XInfo = gnome-shell's DISPLAY/XAUTHORITY) and uses these as fallbacks.
  `xwayland_running()` tells whether Xwayland is actually up without
  connecting to its socket (Mutter spawns it on demand).
* `find_x_display()` prefers a socket owned by the session user over a
  root-owned one instead of taking the lowest number of either: SDDM leaves
  its greeter's root-owned Xorg on `:0` while the KDE session's Xwayland is
  `:1`, so under `sudo`/`ssh root@` the old order answered with a display the
  session's cookie cannot open and every X-side answer (real `WM_CLASS`,
  client geometry, `wxprop -id`) silently degraded to the synthesized one."""

import glob
import os
import pwd
import stat

from wdotool.ctx import CmdError

X11_SOCKET_DIR = "/tmp/.X11-unix"
#: test seam (production value): the per-user runtime directories
RUN_USER_DIR = "/run/user"
#: Stand-in for $XDG_RUNTIME_DIR when the session did not give us one.
FALLBACK_RUNTIME_DIR = "/tmp/wdotool-%d"


def runtime_dir() -> str:
    """The directory these tools keep session-lifetime files in -- the input
    daemon's socket, the KWin script lock, the per-compositor state files:
    $XDG_RUNTIME_DIR when the session gave us one, else a private
    `/tmp/wdotool-<uid>`, created 0700 and then verified.

    The fallback is not exotic: `sudo` drops XDG_RUNTIME_DIR, and so do
    `su -`, cron and a bare container -- all documented ways to run these
    tools. /tmp is world-writable, so any file we would put there under a
    guessable name can be created by another local user first. For the
    socket that means every request -- the text of `type` included -- is
    delivered to them, and they can reply {"ok":true} so the caller sees a
    success; for a state file it means they choose the answers it holds,
    including the pid a root process signals. A directory nobody else may
    enter closes that for everything inside it. It is verified after
    creation because an attacker may have created it first.

    Raises CmdError when that directory cannot be made, or is not ours.
    Callers for which a runtime path is a convenience rather than a
    contract catch it and fall back to their own name under /tmp."""
    rd = os.environ.get("XDG_RUNTIME_DIR")
    if rd and os.path.isdir(rd):
        return rd
    d = FALLBACK_RUNTIME_DIR % os.getuid()
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    except OSError as e:
        raise CmdError(f"cannot create {d}: {e}") from None
    try:
        st = os.lstat(d)
    except OSError as e:
        raise CmdError(f"cannot stat {d}: {e}") from None
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or st.st_mode & 0o077):
        raise CmdError(
            f"{d} is not a private directory owned by uid {os.getuid()}; "
            "refusing to put the wdotool socket there")
    return d


def _owner(path: str) -> int | None:
    """uid owning `path`, or None when it cannot be stat()ed."""
    try:
        return os.stat(path).st_uid
    except OSError:
        return None


def _sudo_uid():
    for var in ("SUDO_UID", "PKEXEC_UID"):
        try:
            return int(os.environ[var])
        except (KeyError, ValueError):
            continue
    return None


def _has_wayland_socket(d: str) -> bool:
    try:
        return any(n.startswith("wayland-") and not n.endswith(".lock")
                   for n in os.listdir(d))
    except OSError:
        return False


def runtime_dir_candidates() -> list[tuple[int, str]]:
    """(uid, dir) candidates, best first: dirs holding a wayland-* socket
    (the graphical session) before the rest; within each group
    $XDG_RUNTIME_DIR, then the sudo/pkexec-invoking user, then real users
    (uid>=1000), then the others."""
    out = []
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d):
        out.append((os.stat(d).st_uid, d))
    target = _sudo_uid()
    dirs = []
    try:
        for name in os.listdir(RUN_USER_DIR):
            if name.isdigit():
                dirs.append((int(name), os.path.join(RUN_USER_DIR, name)))
    except OSError:
        pass
    dirs.sort(key=lambda t: (t[0] != target, t[0] < 1000, t[0]))
    seen = {p for _u, p in out}
    out.extend(t for t in dirs if t[1] not in seen)
    # stable: keeps the order above inside each group
    out.sort(key=lambda t: not _has_wayland_socket(t[1]))
    return out


def _scan(match) -> tuple[int, str] | None:
    for uid, d in runtime_dir_candidates():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if match(n):
                return uid, os.path.join(d, n)
    return None


def find_wayland_socket() -> tuple[int, str, str] | None:
    """(uid, runtime_dir, socket_path) of the graphical session, or None.

    $WAYLAND_DISPLAY alone names the socket. It used to be honoured only
    together with $XDG_RUNTIME_DIR, and the two do not always travel
    together: under `sudo` XDG_RUNTIME_DIR is root's own (or unset) while
    WAYLAND_DISPLAY survives, and a display named on the command line
    (`wxrandr -d wayland-1`, which sets WAYLAND_DISPLAY and nothing else)
    sets only the one variable. Requiring both dropped the named display on
    the floor and scanned up whichever socket sorted first instead -- so
    `sudo wxrandr -d wayland-1` answered about wayland-0. The name is
    therefore looked for in $XDG_RUNTIME_DIR first (the in-session case,
    unchanged) and then in the candidate runtime dirs; only a name that
    exists nowhere falls through to the scan."""
    rd = os.environ.get("XDG_RUNTIME_DIR")
    wd = os.environ.get("WAYLAND_DISPLAY")
    if wd and wd.startswith("/"):
        uid = _owner(wd)
        if uid is not None:
            return uid, rd or os.path.dirname(wd), wd
    elif wd:
        dirs = ([rd] if rd else []) + [d for _u, d in runtime_dir_candidates()]
        for d in dirs:
            sock = os.path.join(d, wd)
            uid = _owner(sock)
            if uid is not None:
                return uid, d, sock
    hit = _scan(lambda n: n.startswith("wayland-") and not n.endswith(".lock"))
    if hit:
        uid, sock = hit
        return uid, os.path.dirname(sock), sock
    return None


def find_sway_socket() -> str | None:
    for var in ("SWAYSOCK", "I3SOCK"):
        p = os.environ.get(var)
        if p and os.path.exists(p):
            return p
    hit = _scan(
        lambda n: (n.startswith("sway-ipc.") or n.startswith("i3-ipc.")) and n.endswith(".sock")
    )
    return hit[1] if hit else None


def find_user_bus() -> tuple[int, str] | None:
    """(uid, DBUS_SESSION_BUS_ADDRESS) of the graphical session, or None.

    $DBUS_SESSION_BUS_ADDRESS wins when its socket sits next to a Wayland
    socket (the in-session case). `ssh root@box` gets its own
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus from pam_systemd --
    a bus with no compositor on it -- so a scanned bus that does live in the
    graphical session's runtime dir beats an environment bus that does not.
    With no Wayland socket anywhere the old order holds (env, then scan)."""
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    env_hit = None
    if addr.startswith("unix:path="):
        path = addr[len("unix:path=") :].split(",")[0]
        if os.path.exists(path):
            env_hit = (os.stat(path).st_uid, addr)
            if _has_wayland_socket(os.path.dirname(path)):
                return env_hit
    hit = _scan(lambda n: n == "bus")
    if hit and _has_wayland_socket(os.path.dirname(hit[1])):
        return hit[0], f"unix:path={hit[1]}"
    if env_hit:
        return env_hit
    if hit:
        return hit[0], f"unix:path={hit[1]}"
    return None


def find_session_bus() -> tuple[int, str] | None:
    """(uid, DBUS_SESSION_BUS_ADDRESS) of the bus the *compositor* lives on.
    Additive (GNOME): under `ssh root@host` pam_systemd hands root its own
    /run/user/0 (with a user bus of its own, where no Mutter is), so
    find_user_bus() lands on the wrong bus. Anchor on the runtime dir that
    owns the Wayland socket instead; $DBUS_SESSION_BUS_ADDRESS still wins
    when it belongs to that same user (the normal session / `sudo -E`)."""
    hit = find_wayland_socket()
    if hit is None:
        return find_user_bus()
    uid, rd, _sock = hit
    env = find_user_bus()
    if env is not None and env[0] == uid:
        return env
    p = os.path.join(rd, "bus")
    if os.path.exists(p):
        return os.stat(p).st_uid, f"unix:path={p}"
    return env


# -- X plane (Xwayland) ------------------------------------------------------

def session_uid() -> int | None:
    """uid of the graphical session we are aimed at (Wayland socket owner,
    else the runtime-dir candidate order), or None."""
    hit = find_wayland_socket()
    if hit:
        return hit[0]
    cands = runtime_dir_candidates()
    return cands[0][0] if cands else None


def _runtime_dir_of(uid: int | None) -> str | None:
    for u, d in runtime_dir_candidates():
        if uid is None or u == uid:
            return d
    return None


#: session processes whose own environment names the session's X plane
#: ($DISPLAY, $XAUTHORITY), best first. Matched against /proc/<pid>/comm, so
#: every name has to fit its 15-character limit -- "startplasma-x11" is
#: exactly 15, and a longer one would silently never match.
_SESSION_LEADERS = ("gnome-shell", "startplasma-x11", "kwin_x11",
                    "kwin_wayland", "plasmashell", "ksmserver",
                    "xfce4-session", "sway")


def _shell_environ(uid: int | None) -> dict[str, str]:
    """Environment of the session's own compositor or session leader
    (readable as root or as that user), {} when not found/readable.

    More than gnome-shell, because a display manager may keep the X cookie
    where no search can find it: SDDM writes /tmp/xauth_<random>, which is
    neither ~/.Xauthority nor anything in a runtime directory, so on a
    Plasma X11 session one of the session's own processes is the only place
    a root shell (`ssh root@box`, cron) can learn the cookie path at all --
    without it the original we hand over to dies with `Authorization
    required, but no authorization protocol specified` where it should have
    worked. The scan is uid-qualified, exactly as before, so it never reads
    another user's session; what it trusts is a process of the target user,
    the same trust ~/.Xauthority already gets."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return {}
    best, best_rank = {}, len(_SESSION_LEADERS)
    for p in pids:
        try:
            with open(f"/proc/{p}/comm") as f:
                comm = f.read().strip()
            if comm not in _SESSION_LEADERS:
                continue
            rank = _SESSION_LEADERS.index(comm)
            if rank >= best_rank:
                continue
            if uid is not None and os.stat(f"/proc/{p}").st_uid != uid:
                continue
            with open(f"/proc/{p}/environ", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        env = {}
        for item in raw.split(b"\0"):
            k, sep, v = item.partition(b"=")
            if sep:
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        if env:
            best, best_rank = env, rank
            if rank == 0:
                break
    return best


def xwayland_running(uid: int | None = None) -> bool:
    """Is an Xwayland server process alive (for `uid`'s session, or any)?
    Mutter and KWin spawn Xwayland on demand and keep the listening socket
    themselves, so the socket's existence says nothing -- and connecting to
    it to find out would start the server. The process table answers
    without side effects (comm is world-readable)."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for p in pids:
        try:
            with open(f"/proc/{p}/comm") as f:
                if f.read().strip() != "Xwayland":
                    continue
            if uid is not None and os.stat(f"/proc/{p}").st_uid != uid:
                continue
        except OSError:
            continue
        return True
    return False


def find_x_display(uid: int | None = None) -> str | None:
    """DISPLAY of the session's X server (Xwayland), or None. $DISPLAY when
    its socket exists; else gnome-shell's own DISPLAY (procfs); else the
    lowest-numbered X11_SOCKET_DIR/X* socket owned by the session user
    (a root-owned one only when the user owns none: see below)."""
    d = os.environ.get("DISPLAY", "")
    if d.startswith(":"):
        num = d[1:].split(".")[0]
        if num.isdigit() and os.path.exists(f"{X11_SOCKET_DIR}/X{num}"):
            return d
    if uid is None:
        uid = session_uid()
    env_d = _shell_environ(uid).get("DISPLAY", "")
    if env_d.startswith(":"):
        return env_d
    mine, root = [], []
    for path in glob.glob(X11_SOCKET_DIR + "/X*"):
        num = os.path.basename(path)[1:]
        if not num.isdigit():
            continue
        owner = _owner(path)
        if owner is None:
            continue
        if uid is None or owner == uid:
            mine.append(int(num))
        elif owner == 0:
            root.append(int(num))
    # The session user's own socket first, and only then a root-owned one.
    # A Wayland compositor creates the listening socket for its Xwayland
    # itself, as the session user; a display manager's greeter leaves a
    # root-owned Xorg socket behind on the *lower* number, and taking that
    # one (as "owner == uid or owner == 0, lowest wins" did) hands out a
    # DISPLAY that the session's cookie cannot open -- the whole X plane
    # then silently disappears from a `sudo` or `ssh root@` run, on KDE
    # with SDDM in particular. A plain X11 session's Xorg *is* root-owned,
    # so that stays the fallback.
    found = mine or root
    if found:
        return ":%d" % min(found)
    return None


def find_xauthority(uid: int | None = None) -> str | None:
    """Cookie file for the session's X server, or None: $XAUTHORITY when it
    exists; the session leader's own XAUTHORITY (gnome-shell, Plasma's
    startplasma/kwin/plasmashell, xfce4-session, sway -- the only route to
    SDDM's /tmp/xauth_<random>); the newest
    <runtime dir>/.mutter-Xwaylandauth.* (Mutter) or xauth_* (GDM);
    ~/.Xauthority of the session user."""
    p = os.environ.get("XAUTHORITY", "")
    if p and os.path.exists(p):
        return p
    if uid is None:
        uid = session_uid()
    env_p = _shell_environ(uid).get("XAUTHORITY", "")
    if env_p and os.path.exists(env_p):
        return env_p
    rd = _runtime_dir_of(uid)
    if rd:
        cands = glob.glob(os.path.join(rd, ".mutter-Xwaylandauth.*")) + \
            glob.glob(os.path.join(rd, "xauth_*"))
        best, best_m = None, -1.0
        for c in cands:
            try:
                m = os.stat(c).st_mtime
            except OSError:
                continue
            if m > best_m:
                best, best_m = c, m
        if best:
            return best
    if uid is not None:
        try:
            home = pwd.getpwuid(uid).pw_dir
        except KeyError:
            home = None
        if home and os.path.exists(os.path.join(home, ".Xauthority")):
            return os.path.join(home, ".Xauthority")
    return None
