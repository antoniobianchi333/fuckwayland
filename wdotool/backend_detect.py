"""Backend detection: WDOTOOL_BACKEND override, then sway IPC socket, KWin
D-Bus, GNOME Shell D-Bus, wlr foreign-toplevel probe."""

import os
import subprocess

from wdotool import session
from wdotool.ctx import CmdError


def _sway():
    from wdotool.backend_sway import SwayBackend
    return SwayBackend()


def _wlr():
    from wdotool.backend_wlr import WlrBackend
    return WlrBackend()


def _kwin():
    from wdotool.backend_kwin import KwinBackend
    return KwinBackend()


def _gnome():
    from wdotool.backend_gnome import GnomeBackend
    return GnomeBackend()


_MAKERS = {"sway": _sway, "i3": _sway, "wlr": _wlr, "kwin": _kwin,
           "gnome": _gnome}


def dbus_env() -> dict | None:
    """Process env pointing at the graphical session's D-Bus, or None."""
    hit = session.find_user_bus()
    if not hit:
        return None
    _uid, addr = hit
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=addr)
    if addr.startswith("unix:path="):
        path = addr[len("unix:path="):].split(",")[0]
        env["XDG_RUNTIME_DIR"] = os.path.dirname(path)
    return env


def dbus_name_has_owner(name: str) -> bool:
    env = dbus_env()
    if env is None:
        return False
    probes = (
        ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
         "--object-path", "/org/freedesktop/DBus",
         "--method", "org.freedesktop.DBus.NameHasOwner", name],
        ["busctl", "--user", "call", "org.freedesktop.DBus",
         "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner",
         "s", name],
    )
    for argv in probes:
        try:
            r = subprocess.run(argv, env=env, capture_output=True, text=True,
                               timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return "true" in r.stdout
    return False


def detect():
    forced = os.environ.get("WDOTOOL_BACKEND")
    if forced:
        maker = _MAKERS.get(forced.strip().lower())
        if maker is None:
            raise CmdError(
                "WDOTOOL_BACKEND=%s is not one of: sway, wlr, kwin, gnome"
                % forced
            )
        return maker()
    if session.find_sway_socket():
        try:
            return _sway()
        except CmdError:
            pass
    if dbus_name_has_owner("org.kde.KWin"):
        try:
            return _kwin()
        except CmdError:
            pass
    if dbus_name_has_owner("org.gnome.Shell"):
        try:
            return _gnome()
        except CmdError:
            pass
    try:
        return _wlr()
    except CmdError:
        pass
    raise CmdError(
        "Cannot find a Wayland window-management backend: no sway/i3 IPC "
        "socket, no KWin or GNOME Shell on the session D-Bus, and the "
        "compositor does not offer wlr-foreign-toplevel. Set "
        "WDOTOOL_BACKEND=sway|wlr|kwin|gnome to force one."
    )
