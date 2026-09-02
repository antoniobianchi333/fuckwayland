"""Backend detection.

Order: WDOTOOL_BACKEND override -> sway/i3 IPC socket -> KWin (org.kde.KWin
owned on the session bus) -> GNOME (org.gnome.Shell owned) -> wlr
foreign-toplevel probe -> clear error. The two D-Bus checks are ONE ListNames
call over dbus_mini (no gdbus/busctl spawns); the connection is kept for the
process so the GNOME backend reuses it.

A GNOME session without the bridge extension is reported as such (the
install hint) instead of falling through to wlr: Mutter offers no
foreign-toplevel protocol, so the generic probe could never succeed there.
KWin does offer it, so a KWin backend failure still falls through."""

import os

from wdotool import session
from wdotool.ctx import CmdError

KWIN_NAME = "org.kde.KWin"
GNOME_NAME = "org.gnome.Shell"

_bus = None          # dbus_mini.Bus for this process, once connected
_names = None        # cached ListNames result (None = no bus reachable)
_probed = False


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
    return GnomeBackend(bus=session_bus(), names=session_names())


_MAKERS = {"sway": _sway, "i3": _sway, "wlr": _wlr, "kwin": _kwin,
           "gnome": _gnome}


def reset():
    """Forget the cached bus/names (tests re-detect against a fresh bus)."""
    global _bus, _names, _probed
    if _bus is not None:
        try:
            _bus.close()
        except Exception:  # noqa: BLE001 -- best effort on teardown
            pass
    _bus, _names, _probed = None, None, False


def session_bus():
    """This process's connection to the graphical session's bus, or None
    when no bus can be found/joined. Connected once, cached."""
    global _bus, _probed, _names
    if _probed:
        return _bus
    _probed = True
    from wdotool.dbus_mini import Bus, DBusError
    try:
        _bus = Bus()
        _names = _bus.list_names()
    except DBusError:
        if _bus is not None:
            _bus.close()
        _bus, _names = None, None
    return _bus


def session_names() -> list[str] | None:
    """Names on the session bus (one ListNames per process), or None when
    there is no bus."""
    session_bus()
    return _names


def dbus_env() -> dict | None:
    """Process env pointing at the graphical session's D-Bus, or None (for
    backends that still shell out to gdbus/busctl, e.g. kwin)."""
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
    names = session_names()
    return bool(names) and name in names


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
    names = session_names() or []
    if KWIN_NAME in names:
        try:
            return _kwin()
        except CmdError:
            pass
    if GNOME_NAME in names:
        # Not swallowed: the GNOME error carries the bridge install hint and
        # nothing below it can work under Mutter.
        return _gnome()
    try:
        return _wlr()
    except CmdError:
        pass
    bus_note = ("no session D-Bus reachable" if session_names() is None
                else "no KWin or GNOME Shell on the session D-Bus")
    raise CmdError(
        "Cannot find a Wayland window-management backend: no sway/i3 IPC "
        "socket, %s, and the compositor does not offer wlr-foreign-toplevel. "
        "Set WDOTOOL_BACKEND=sway|wlr|kwin|gnome to force one." % bus_note
    )
