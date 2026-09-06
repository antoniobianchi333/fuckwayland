"""`wxrandr --unsafe-gnome-overlap`: the one route to a GNOME layout in which two
monitors share screen area.

GNOME refuses such a layout, at one door and for no reason the rest of the
compositor needs -- `meta_verify_logical_monitor_config_list()` requires every
logical monitor to share an exact integer edge with another, and nothing else in
Mutter cares (docs/Technical.md section 6).  Every supported route in is closed:
`ApplyMonitorsConfig` validates on every method, and `~/.config/monitors.xml` is
worse than closed, because its reader runs the same validator and one failure
discards the whole file at every boot for ever.

What is left is a GNOME Shell extension that loads a type description of its own
for the symbols libmutter exports but does not introspect, and writes the new
positions into the configuration object before asking Mutter to apply it.  That
extension is `gnome/fuckwayland-overlap@fuckwayland`; this module is its client
and its gatekeeper.  The dangerous half is 8 bytes per monitor written inside
gnome-shell, and on Wayland a dead compositor is the whole session, so:

* **the flag is off, and its name is the warning.**  Nothing else in the CLI
  begins `--unsafe-`, and no xrandr manual page contains this string;
* **the ordinary layout never reaches any of it.**  The flag changes nothing
  unless the layout being applied is one Mutter's own validator refuses.  A
  request GNOME would take goes down the same DisplayConfig path it always did,
  and the extension is not even asked whether it is there;
* **it refuses on a compositor it does not recognise**, before it asks the
  extension anything, and the extension refuses again on its own account;
* **the warning is printed before the call, in full, every time,** and cannot be
  switched off;
* **it can never write ~/.config/monitors.xml.**  `--persistent` and this flag
  are mutually exclusive here; the extension's type description does not declare
  Mutter's writer at all, and it reports the file's digest from before and after
  its work so the tool can say so rather than assume it.

There is no confirmation prompt, deliberately.  See `warning()`.
"""

import json

from fwcommon.dbus_mini import Bus, DBusError

FLAG = "--unsafe-gnome-overlap"
UUID = "fuckwayland-overlap@fuckwayland"
BUS_NAME = "org.fuckwayland.Overlap"
OBJECT_PATH = "/org/fuckwayland/Overlap"
IFACE = "org.fuckwayland.Overlap1"
SHELL_NAME = "org.gnome.Shell"
SHELL_PATH = "/org/gnome/Shell"
CALL_TIMEOUT = 30.0

#: GNOME Shell releases whose private `MetaMonitorsConfig` layout has been
#: measured (docs/Technical.md section 6).  Everything else is refused before a
#: single byte is read: the extension's own guards would very probably catch a
#: stranger, but "very probably" is not a thing to spend somebody's session on.
SUPPORTED_MAJORS = (46, 50)

INSTALL_HINT = (
    "the overlap extension is not running: install it with\n"
    "    sh gnome/install-overlap.sh\n"
    "and log out and back in once (gnome-shell reads extension directories "
    "only at login)\n")


# -- what the flag is allowed to do ------------------------------------------

def shell_major(version):
    """The major number of a GNOME Shell version string, or None."""
    try:
        return int(str(version).split(".")[0])
    except (ValueError, AttributeError, IndexError):
        return None


def unsupported_reason(version):
    """Why this compositor is not one to write into, or None if it is one.

    Deliberately a version *allowlist* rather than a blocklist.  The offsets
    this depends on are private, they already differ between the two releases
    that are supported, and a wrong one does not raise an error -- it writes
    into the compositor's heap.
    """
    major = shell_major(version)
    if major is None:
        return ("cannot tell which GNOME Shell this is (%r)" % (version,))
    if major not in SUPPORTED_MAJORS:
        return ("GNOME Shell %s is not a build this has been measured on "
                "(%s are)" % (version, " and ".join(str(m) for m in SUPPORTED_MAJORS)))
    return None


def groups(plan):
    """A mutter plan (wxrandr.mutter.MutterOutputs.plan) as the extension's
    connector groups: one entry per logical monitor, mirrored outputs together."""
    out = []
    for p in plan:
        out.append({"connectors": sorted(c for c, _mid, _us in p["members"]),
                    "x": int(p["x"]), "y": int(p["y"])})
    return sorted(out, key=lambda g: (g["x"], g["y"], g["connectors"]))


def rects(plan, dims):
    """(x, y, w, h) per logical monitor, for Mutter's verifier.  `dims` maps a
    connector to its pending logical size."""
    out = []
    for p in plan:
        first = p["members"][0][0]
        if first not in dims:
            return None
        out.append((int(p["x"]), int(p["y"])) + tuple(dims[first]))
    return out


def only_positions_differ(plan, current):
    """True when `plan` differs from the running configuration `current` (a
    `mutter._canon` list) in positions and nothing else.

    The extension writes two 32-bit words per logical monitor and nothing more:
    it cannot change a mode, a scale, a rotation, the primary or which outputs
    are mirrored together.  A request that changes any of those has to go
    through DisplayConfig first, and this is where that is noticed -- before
    anything is written -- instead of the extension quietly applying half of it.
    """
    from wxrandr import mutter as mutter_mod
    strip = lambda canon: sorted(entry[2:] for entry in canon)      # noqa: E731
    return strip(mutter_mod._canon(plan)) == strip(current or [])


def undo_command(current_groups):
    """The `wxrandr` line that puts the layout back, built from the layout that
    is running *now*, before anything is applied: an undo the user has to
    compose afterwards is not an undo.  It goes through DisplayConfig like any
    other invocation -- the way back does not depend on the dangerous half
    still working, or on the extension still being loaded."""
    parts = []
    for g in current_groups:
        parts.append("--output %s --pos %dx%d" % (g["connectors"][0], g["x"], g["y"]))
    return "wxrandr " + " ".join(parts)


# -- the warning -------------------------------------------------------------

RECOVERY = (
    "  If the session dies:  gnome-shell is the session on Wayland, so you land at\n"
    "                        the login screen.  Log in again -- nothing was saved,\n"
    "                        so the layout is the one you started with.\n"
    "                        If a session will not start at all, switch to a text\n"
    "                        console with Ctrl+Alt+F3, log in and run\n"
    "                            gnome-extensions disable %s\n"
    "                        (or delete\n"
    "                            ~/.local/share/gnome-shell/extensions/%s ),\n"
    "                        then Ctrl+Alt+F1 back to the login screen.\n" % (UUID, UUID))


def warning(shell, moves, undo):
    """Exactly what is about to happen, what it risks and how to get back.

    Printed to stderr, in full, before the extension is called, on every
    invocation, and there is no way to silence it.

    **No confirmation prompt, on purpose.**  The decision is already made, once,
    by a flag that cannot be typed by accident and appears in no xrandr manual;
    a prompt would only ask the user to make it a second time.  Worse, a prompt
    has to be skippable to keep the tool usable from a script and from warandr,
    and a guard with a documented bypass protects nobody -- while the guards
    that do the work (the version gate here, and the struct-size, sentinel,
    bounded-read and public-view checks inside the extension) run whether or not
    a human answered a question.  GNOME's own answer to "are you sure" is the
    "Keep changes?" dialog with its 20-second revert, and this path cannot have
    that: it does not go through the D-Bus method that arms the timer.  So what
    is offered instead of a question is a printed undo command, and a printed
    way back from a session that will not start.
    """
    lines = ["xrandr: %s: GNOME will not place these monitors, so they are going to be\n"
             "  placed by writing into the running gnome-shell instead of asking it.\n"
             % FLAG]
    for i, m in enumerate(moves or ["nothing (no monitor changes position)"]):
        lines.append("  %s move %s\n"
                     % ("What it does:        " if i == 0 else " " * 21, m))
    lines.append(
        "                        by writing 8 bytes per monitor inside gnome-shell\n"
        "                        (GNOME Shell %s), through the %s\n"
        "                        extension, and then asking Mutter to apply the result.\n"
        % (shell, UUID))
    lines.append(
        "  What it risks:        those 8 bytes go where this build of libmutter keeps a\n"
        "                        logical monitor's position.  The extension re-checks\n"
        "                        that this really is such a build before every write --\n"
        "                        struct size against the GType registry, a sentinel\n"
        "                        through Mutter's own setter, and every value it reads\n"
        "                        against what Mutter reports publicly -- and refuses if\n"
        "                        anything disagrees.  If all of that is wrong anyway,\n"
        "                        gnome-shell crashes, and on Wayland gnome-shell is the\n"
        "                        session: every program running in it goes with it.\n")
    lines.append(
        "  What it saves:        nothing.  ~/.config/monitors.xml is never written on\n"
        "                        this path, so this layout does not survive a logout,\n"
        "                        and --persistent is refused together with this flag.\n")
    lines.append("  To undo:              %s\n" % undo)
    lines.append("                        or log out and back in.\n")
    lines.append(RECOVERY)
    return "".join(lines)


def moves_text(before, after):
    """"Virtual-2 from +1920+0 to +960+0" per monitor that actually moves."""
    was = {tuple(g["connectors"]): (g["x"], g["y"]) for g in before}
    out = []
    for g in after:
        key = tuple(g["connectors"])
        old = was.get(key)
        if old is None or old == (g["x"], g["y"]):
            continue
        out.append("%s from +%d+%d to +%d+%d"
                   % ("+".join(key), old[0], old[1], g["x"], g["y"]))
    return out


# -- the client --------------------------------------------------------------

class OverlapError(Exception):
    """A refusal, already worded for the user."""


class Overlap:
    """Client of org.fuckwayland.Overlap1.  Every call is a JSON string in and a
    JSON string out; the extension answers with `ok: false` and a named check
    rather than a D-Bus error, so a refusal reads the same wherever it came
    from."""

    def __init__(self, bus: Bus):
        self.bus = bus

    def running(self) -> bool:
        try:
            return self.bus.name_has_owner(BUS_NAME)
        except DBusError:
            return False

    def shell_version(self):
        """The running shell's own version string, from its public property."""
        try:
            (v,) = self.bus.call(SHELL_NAME, SHELL_PATH,
                                 "org.freedesktop.DBus.Properties", "Get", "ss",
                                 (SHELL_NAME, "ShellVersion"))
        except (DBusError, ValueError):
            return None
        return getattr(v, "value", v)

    def _call(self, member, request):
        try:
            (raw,) = self.bus.call(BUS_NAME, OBJECT_PATH, IFACE, member, "s",
                                   (json.dumps(request),), timeout=CALL_TIMEOUT)
        except DBusError as e:
            raise OverlapError("the overlap extension failed: %s\n"
                               % (e.message or e.name))
        try:
            return json.loads(raw)
        except ValueError:
            raise OverlapError("the overlap extension answered something that "
                               "is not JSON\n")

    def probe(self, layout_mode=None, expect=None):
        return self._call("Probe", _request(layout_mode, expect))

    def apply(self, layout_mode, expect, want):
        req = _request(layout_mode, expect)
        req["want"] = want
        return self._call("ApplyOverlap", req)


def _request(layout_mode, expect):
    req = {}
    if layout_mode:
        req["layout_mode"] = int(layout_mode)
    if expect is not None:
        req["expect"] = expect
    return req


def refusal_text(reply):
    """One line for a reply with `ok: false`."""
    check = reply.get("check") or "?"
    return ("the overlap extension refused (%s): %s\n"
            % (check, reply.get("reason") or "no reason given"))


def applied_text(reply):
    """What to print after a successful apply: what Mutter's validator said (a
    positive control that the write landed on the field it reads), and the proof
    that the saved configuration file was not touched."""
    out = []
    verify = reply.get("verify")
    if verify:
        out.append("mutter's own validator on the result: %s\n" % verify)
    saved = reply.get("saved_config") or {}
    if saved:
        if saved.get("unchanged"):
            out.append("%s: unchanged (%s)\n"
                       % (saved.get("path", "monitors.xml"),
                          saved.get("before", "?")))
        else:
            out.append("%s CHANGED across this call (%s -> %s); that should be "
                       "impossible, please report it\n"
                       % (saved.get("path", "monitors.xml"),
                          saved.get("before"), saved.get("after")))
    return "".join(out)
