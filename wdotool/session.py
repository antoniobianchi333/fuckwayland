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
  connecting to its socket (Mutter spawns it on demand)."""

import glob
import os
import pwd


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
        for name in os.listdir("/run/user"):
            if name.isdigit():
                dirs.append((int(name), os.path.join("/run/user", name)))
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
    """(uid, runtime_dir, socket_path) of the graphical session, or None."""
    rd = os.environ.get("XDG_RUNTIME_DIR")
    wd = os.environ.get("WAYLAND_DISPLAY")
    if rd and wd:
        sock = wd if wd.startswith("/") else os.path.join(rd, wd)
        if os.path.exists(sock):
            return os.stat(sock).st_uid, rd, sock
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


def _shell_environ(uid: int | None) -> dict[str, str]:
    """Environment of the session's gnome-shell (readable as root or as that
    user), {} when not found/readable."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return {}
    for p in pids:
        try:
            with open(f"/proc/{p}/comm") as f:
                if f.read().strip() != "gnome-shell":
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
            return env
    return {}


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
    lowest-numbered /tmp/.X11-unix/X* socket owned by the session user."""
    d = os.environ.get("DISPLAY", "")
    if d.startswith(":"):
        num = d[1:].split(".")[0]
        if num.isdigit() and os.path.exists(f"/tmp/.X11-unix/X{num}"):
            return d
    if uid is None:
        uid = session_uid()
    env_d = _shell_environ(uid).get("DISPLAY", "")
    if env_d.startswith(":"):
        return env_d
    found = []
    for path in glob.glob("/tmp/.X11-unix/X*"):
        num = os.path.basename(path)[1:]
        if not num.isdigit():
            continue
        try:
            owner = os.stat(path).st_uid
        except OSError:
            continue
        if uid is None or owner == uid or owner == 0:
            found.append(int(num))
    if found:
        return ":%d" % min(found)
    return None


def find_xauthority(uid: int | None = None) -> str | None:
    """Cookie file for the session's X server, or None: $XAUTHORITY when it
    exists; gnome-shell's own XAUTHORITY; the newest
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
