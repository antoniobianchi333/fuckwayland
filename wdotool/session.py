"""Discovery of the graphical session's sockets, tolerant of running under sudo
(where XDG_RUNTIME_DIR etc. point at root's empty runtime dir or are unset).
FROZEN — edit only if broken."""

import os


def _sudo_uid():
    try:
        return int(os.environ["SUDO_UID"])
    except (KeyError, ValueError):
        return None


def runtime_dir_candidates() -> list[tuple[int, str]]:
    """(uid, dir) candidates, best first: $XDG_RUNTIME_DIR, then /run/user/* with
    the sudo-invoking user preferred, then real users (uid>=1000), then the rest."""
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
    out.extend(dirs)
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
    """(uid, DBUS_SESSION_BUS_ADDRESS) of the graphical session, or None."""
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if addr.startswith("unix:path="):
        path = addr[len("unix:path=") :].split(",")[0]
        if os.path.exists(path):
            return os.stat(path).st_uid, addr
    hit = _scan(lambda n: n == "bus")
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
