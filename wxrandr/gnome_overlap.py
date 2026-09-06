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
* **the warning is printed before the call, in full,** on every invocation until it
  is agreed to -- once, deliberately, and only for the build the checks passed on;
  see "the agreement" at the foot of this file for why that changes what is
  printed and nothing else;
* **it can never write ~/.config/monitors.xml.**  `--persistent` and this flag
  are mutually exclusive here; the extension's type description does not declare
  Mutter's writer at all, and it reports the file's digest from before and after
  its work so the tool can say so rather than assume it.

There is no confirmation prompt on the command line, deliberately.  See
`warning()`; warandr's dialog is the other half, and it too can only ever record
the agreement below.
"""

import json
import os
import re
import time

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
    invocation until `--gnome-overlap-allow` has recorded an agreement for this
    build -- after which it is one line, and the checks are unchanged.

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


def notes_text(reply):
    """Anything the extension noticed that decides nothing.

    There is exactly one today and it is worth its own line: libmutter replaced
    on disk under a live session, which `apt upgrade` does routinely and which
    nothing in a session can otherwise see.  Notes are printed whether or not an
    agreement is recorded, because unlike the paragraph they are news rather
    than reassurance."""
    return "".join("note: %s\n" % n for n in (reply.get("notes") or []) if n)


def applied_text(reply, quiet=False):
    """What to print after a successful apply: what Mutter's validator said (a
    positive control that the write landed on the field it reads), and the proof
    that the saved configuration file was not touched.

    `quiet` (an agreement covers this build) drops both -- they are reassurance,
    and reassurance is exactly what somebody who has agreed does not need every
    time.  What survives `quiet` is the one line that is not reassurance: a
    saved configuration file that moved when it cannot have."""
    out = []
    verify = reply.get("verify")
    if verify and not quiet:
        out.append("mutter's own validator on the result: %s\n" % verify)
    saved = reply.get("saved_config") or {}
    if saved:
        if saved.get("unchanged"):
            if not quiet:
                out.append("%s: unchanged (%s)\n"
                           % (saved.get("path", "monitors.xml"),
                              saved.get("before", "?")))
        else:
            out.append("%s CHANGED across this call (%s -> %s); that should be "
                       "impossible, please report it\n"
                       % (saved.get("path", "monitors.xml"),
                          saved.get("before"), saved.get("after")))
    return "".join(out)


# -- the agreement -----------------------------------------------------------
#
# The paragraph above is right the first time somebody does this and wrong the
# fiftieth, and a warning nobody reads is not a warning.  So it can be agreed to
# once -- but the agreement is *scoped to the build the checks were run on*,
# because agreeing is agreeing to a measured risk, and a compositor nobody has
# measured is not that risk.
#
# What the agreement does and does not do:
#
# * it silences the paragraph, and nothing else.  Every check in the list still
#   runs on every call, inside gnome-shell, whether or not anything is recorded
#   here: the applying path in wxrandr/mutter.py asks this module only what to
#   *print*, and the reply's `ok` is what decides whether anything is written.
#   There is no code path in which a recorded yes reaches a check.
# * it never turns the feature on.  `--unsafe-gnome-overlap` is still the only
#   thing that does, and it is still typed on every invocation.
#
# It is a file rather than GSettings or dconf: wxrandr must be able to read it
# with no GLib bindings and no schema installed, a user has to be able to see
# what they agreed to, and withdrawing it has to be possible with `rm` from a
# console when the session will not start.

CONSENT_DIR = "fuckwayland"
CONSENT_NAME = "overlap-consent.json"
CONSENT_FORMAT = 1
ALLOW_FLAG = "--gnome-overlap-allow"
FORGET_FLAG = "--gnome-overlap-forget"
STATUS_FLAG = "--gnome-overlap-status"


def consent_path(env=None):
    """`$XDG_CONFIG_HOME/fuckwayland/overlap-consent.json`, else
    `~/.config/fuckwayland/overlap-consent.json`.

    Per user, never per system: it is the user's own session that a wrong offset
    ends, so root's answer must not stand in for anybody else's.  The XDG rule is
    the spec's, relative `XDG_CONFIG_HOME` ignored, the same one
    `monitors_xml.default_path()` follows."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or ""
    if not base.startswith("/"):
        base = os.path.join(env.get("HOME", ""), ".config")
    return os.path.join(base, CONSENT_DIR, CONSENT_NAME)


def facts(reply):
    """The three things the checks actually verified, out of an extension reply:
    the GNOME Shell version string, libmutter's generation, and the size this
    build's `MetaMonitorsConfig` was found to be.

    They are what an agreement is recorded against.  The size comes from the
    reply rather than from anything here, because the check that matters read it
    out of the running GType registry -- a number written in this tree could go
    stale against the compositor, which is the whole failure being guarded."""
    size = reply.get("instance_size")
    if size is None:
        # an extension from before the field existed: the same number, from the
        # check that reported it
        for check in reply.get("checks") or []:
            if check.get("name") == "typelib":
                m = re.search(r"MetaMonitorsConfig (\d+) bytes", check.get("detail") or "")
                if m:
                    size = int(m.group(1))
    return {"shell": str(reply.get("shell") or ""),
            "libmutter": reply.get("libmutter"),
            "struct_size": int(size) if size else None,
            # The GNU build id of the libmutter this session has mapped, as the
            # extension read it out of the file the mapping came from.  It is
            # here because the GNOME Shell version string demonstrably cannot
            # see a library change: Ubuntu 24.04 carries mutter 46.2 under shell
            # 46.0, and 46.0 -> 46.2 under one unchanged shell version was
            # measured applying with every check green.  An agreement that names
            # only the version string therefore outlives the build it was given
            # for.  None on an extension too old to report it, or on a library
            # that would not say -- and then it is simply not compared.
            "libmutter_build": reply.get("libmutter_build") or None}


def describe_build(f):
    """"GNOME Shell 50.1 (libmutter-18 build 0f3a…, MetaMonitorsConfig 80 bytes)".

    The build id is in there because it is the only one of the four that moves
    when a distribution replaces libmutter without touching the shell, which is
    a thing that happens inside a stable release."""
    build = f.get("libmutter_build")
    return ("GNOME Shell %s (libmutter-%s%s, MetaMonitorsConfig %s bytes)"
            % (f.get("shell") or "?", f.get("libmutter") if f.get("libmutter") is not None else "?",
               (" build %s" % str(build)[:12]) if build else "",
               f.get("struct_size") if f.get("struct_size") is not None else "?"))


def load_consent(env=None):
    """The recorded agreement, or None.  Anything unreadable, malformed or in a
    format this build does not know is *not* an agreement: the file only ever
    makes the tool quieter, so failing to read it can only make it louder."""
    path = consent_path(env)
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.loads(fh.read(1 << 16))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("format") != CONSENT_FORMAT:
        return None
    if not rec.get("shell"):
        return None
    return rec


def save_consent(f, how, env=None):
    """Write the agreement.  Returns the path.

    `f` is a `facts()` dict that came back from a *successful* probe or apply, so
    nothing can be recorded for a build the six checks have not just passed on;
    `how` is the words for how it was given, kept so that `--gnome-overlap-status`
    can say it back."""
    path = consent_path(env)
    rec = dict(f)
    rec.update({"format": CONSENT_FORMAT,
                "agreed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "how": how})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def forget_consent(env=None):
    """Withdraw it.  `(path, removed)` -- `removed` False when there was
    nothing to remove, which is not an error: `--gnome-overlap-forget` on a
    machine that never agreed has done what it was asked."""
    path = consent_path(env)
    try:
        os.unlink(path)
        return path, True
    except OSError:
        return path, False


def consent_covers(rec, shell):
    """Does the recorded agreement cover the GNOME that is running *now*?

    Only the version string can be compared here, and that is deliberate: this
    question has to be answered before anything is read out of the compositor,
    because its answer decides whether the paragraph is printed *before* the
    write.  The other two recorded facts are audited afterwards, against the
    reply, by `consent_drift()`.

    Returns `(True, None)` or `(False, one sentence saying why not)`."""
    if not rec:
        return False, "nothing is recorded"
    if str(rec.get("shell")) != str(shell):
        return False, ("the agreement was given on GNOME Shell %s; this session "
                       "is GNOME Shell %s" % (rec.get("shell"), shell))
    return True, None


def consent_drift(rec, f):
    """The audit after the reply: everything the agreement recorded, against
    everything the checks just measured.

    Reaching this with a difference needs a build that kept its version string
    and changed its private layout -- which the extension's own struct-size check
    turns into a refusal, not a bad write -- so this is a record-keeping check
    rather than a guard.  When it fires the record is withdrawn, and the next run
    asks in full."""
    if not rec:
        return None
    bad = []
    for k, word in (("libmutter", "libmutter generation"),
                    ("struct_size", "MetaMonitorsConfig size")):
        want, got = rec.get(k), f.get(k)
        if want is not None and got is not None and int(want) != int(got):
            bad.append("%s %s, not %s" % (word, got, want))
    # The one that a routine `apt upgrade` actually moves.  Measured: four
    # libmutter builds on 24.04 and four on 26.04, every one of them a different
    # binary and every one of them the same private layout -- so this is not a
    # danger signal, it is the end of what was agreed to.  The next run asks in
    # full about the build that is there now.
    want, got = rec.get("libmutter_build"), f.get("libmutter_build")
    if want and got and str(want) != str(got):
        bad.append("libmutter build %s, not %s" % (str(got)[:12], str(want)[:12]))
    if not bad:
        return None
    return ("this GNOME is not the one that was agreed to (%s); the agreement "
            "has been withdrawn and the next run will ask again\n" % "; ".join(bad))


def quiet_line(rec, fault):
    """The whole of what an agreed run says.  One line, and the last one: it
    names the rule GNOME would have refused this layout under, because a layout
    no other desktop would blink at is still an unusual thing to be doing, and
    the day it was agreed, because that is where `--gnome-overlap-status` and
    `--gnome-overlap-forget` are found from."""
    return ('%s: applying a layout GNOME refuses ("%s"), as agreed on %s\n'
            % (FLAG, fault, str((rec or {}).get("agreed", "?")).split("T")[0]))


def agreement_text(f):
    """What `--gnome-overlap-allow` prints before it writes anything: the risk
    being agreed to, and the two things the agreement is *not*.

    The checks have already run and passed at this point -- that is where the
    build in the first line comes from -- so this is a description of a measured
    thing rather than a guess about one."""
    return ("".join([
        "Agreeing to %s on %s.\n\n" % (FLAG, describe_build(f)),
        "  What it does:         places monitors GNOME's own validator refuses, by writing\n"
        "                        8 bytes per monitor inside gnome-shell through the\n"
        "                        %s extension, and then asking Mutter\n"
        "                        to apply the result.\n" % UUID,
        "  What it risks:        those 8 bytes go where this build of libmutter keeps a\n"
        "                        logical monitor's position.  If that is ever wrong,\n"
        "                        gnome-shell crashes, and on Wayland gnome-shell is the\n"
        "                        session: every program running in it goes with it.\n",
        "  What it saves:        nothing.  ~/.config/monitors.xml is never written on this\n"
        "                        path, so an overlapping layout is gone at the next login.\n",
        "  What is agreed:       this build and no other.  The agreement records\n"
        "                            %s\n"
        "                        and stops applying the moment any of that changes, which\n"
        "                        is what a distribution upgrade does.\n" % describe_build(f),
        "  What is not agreed:   any of the checking.  Every check above runs again on\n"
        "                        every single apply, agreed or not, and a build this does\n"
        "                        not recognise is refused however old the agreement is.\n",
        "  To withdraw:          wxrandr %s\n" % FORGET_FLAG,
        RECOVERY]))
