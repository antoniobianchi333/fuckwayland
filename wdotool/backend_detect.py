"""Backend detection.

Order: WDOTOOL_BACKEND override -> sway/i3 IPC socket -> KWin (org.kde.KWin owned on the session bus) -> GNOME
(org.gnome.Shell owned) -> wlr foreign-toplevel probe -> clear error. The two D-Bus checks are ONE ListNames
call over dbus_mini (no gdbus/busctl spawns); the connection is kept for the process so the GNOME backend reuses
it.

Neither D-Bus branch is ever swallowed: a GNOME session without the bridge extension is reported as such (the
install hint), and so is a KWin failure. Mutter offers no foreign-toplevel protocol, and KWin implements neither
zwlr_foreign_toplevel_manager_v1 nor ext_foreign_toplevel_list_v1 (checked against KWin 5.27 through 6.6 and
master), so the generic probe below could never succeed on either -- falling through would only replace a
precise message with "the compositor does not offer wlr-foreign-toplevel"."""

import os

from fwcommon import session
from fwcommon.errors import CmdError
from wdotool.backend import program
from wdotool.ctx import NoSessionError

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
    return KwinBackend(bus=session_bus(), names=session_names())


def _gnome():
    from wdotool.backend_gnome import GnomeBackend
    return GnomeBackend(bus=session_bus(), names=session_names())


_MAKERS = {"sway": _sway, "i3": _sway, "wlr": _wlr, "kwin": _kwin, "gnome": _gnome}


def reset():
    """Forget the cached bus/names (tests re-detect against a fresh bus)."""
    global _bus, _names, _probed
    if _bus is not None:
        try:
            _bus.close()
        except Exception:  # best effort on teardown
            pass
    _bus, _names, _probed = None, None, False


def session_bus():
    """This process's connection to the graphical session's bus, or None
    when no bus can be found/joined. Connected once, cached."""
    global _bus, _probed, _names
    if _probed:
        return _bus
    _probed = True
    from fwcommon.dbus_mini import Bus, DBusError
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


def detect():
    forced = os.environ.get("WDOTOOL_BACKEND")
    if forced:
        maker = _MAKERS.get(forced.strip().lower())
        if maker is None:
            raise CmdError("WDOTOOL_BACKEND=%s is not one of: sway, wlr, kwin, gnome" % forced)
        return maker()
    if session.find_sway_socket():
        try:
            return _sway()
        except CmdError:
            pass
    names = session_names() or []
    if KWIN_NAME in names:
        # Not swallowed either (see the module docstring): KWin offers no
        # foreign-toplevel protocol, so nothing below this could work here.
        return _kwin()
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
    # rc 2, not 1: "there is no session to talk to" is a different answer to
    # a script than "the session is up and nothing matched" (B5).
    raise NoSessionError(
        "%s: no Wayland session found: no sway/i3 IPC socket, %s, and "
        "the compositor does not offer wlr-foreign-toplevel. "
        "Set WDOTOOL_BACKEND=sway|wlr|kwin|gnome to force one."
        % (program(), bus_note)
    )
