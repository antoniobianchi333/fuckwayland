"""OWNER: wxprop builder. Plane resolution, property assembly, -spy loops.

Two planes, per WXPROP.md:

- X windows (XWayland): everything through wwmctl.x11_mini against the real
  X server — GetProperty/ListProperties for reads, ChangeProperty/
  DeleteProperty for -set/-remove, PropertyNotify events for -spy. Real
  xprop is the byte oracle.
- native Wayland windows (compositor node ids): a synthesized property set
  built from compositor data, printed through the exact same fmt.py
  machinery so `wxprop -id N WM_CLASS`-style script parsing just works.
  -set/-remove on a native window is a clean one-line error (there is no
  property store to write to); -spy subscribes to sway window IPC events
  and reprints the synthesized property that changed.

Window ids: an id that matches a compositor node resolves through the
node (an XWayland node redirects to its real X window id); anything else
goes to the X server when one is reachable, exactly like xprop would.
"""

import os
import socket
import struct
import sys

from wxprop.fmt import FatalError

# PropertyChangeMask | StructureNotifyMask
SPY_EVENT_MASK = 0x420000

_I3_MAGIC = b"i3-ipc"
_I3_SUBSCRIBE = 2
_I3_EVENT_BIT = 0x80000000
_I3_EVENT_WINDOW = _I3_EVENT_BIT | 3
_I3_EVENT_WORKSPACE = _I3_EVENT_BIT | 0

# -- Xlib's default error report (what xprop shows on e.g. BadWindow) -------

_X_ERROR_TEXT = {
    1: "BadRequest (invalid request code or no such operation)",
    2: "BadValue (integer parameter out of range for operation)",
    3: "BadWindow (invalid Window parameter)",
    4: "BadPixmap (invalid Pixmap parameter)",
    5: "BadAtom (invalid Atom parameter)",
    6: "BadCursor (invalid Cursor parameter)",
    7: "BadFont (invalid Font parameter)",
    8: "BadMatch (invalid parameter attributes)",
    9: "BadDrawable (invalid Pixmap or Window parameter)",
    10: "BadAccess (attempt to access private resource denied)",
    11: "BadAlloc (insufficient resources for operation)",
    12: "BadColor (invalid Colormap parameter)",
    13: "BadGC (invalid GC parameter)",
    14: "BadIDChoice (invalid resource ID chosen for this connection)",
    15: "BadName (named color or font does not exist)",
    16: "BadLength (poly request too large or internal Xlib length error)",
    17: "BadImplementation (server does not implement operation)",
}

_X_OPCODE_NAMES = {
    2: "X_ChangeWindowAttributes", 14: "X_GetGeometry", 15: "X_QueryTree",
    16: "X_InternAtom", 17: "X_GetAtomName", 18: "X_ChangeProperty",
    19: "X_DeleteProperty", 20: "X_GetProperty", 21: "X_ListProperties",
    25: "X_SendEvent", 40: "X_TranslateCoords", 43: "X_GetInputFocus",
}

# resource-id errors get the "Resource id in failed request" line
_X_RESOURCE_ERRORS = {3, 4, 6, 7, 9, 12, 13, 14}


def x_error_report(err) -> str:
    text = _X_ERROR_TEXT.get(err.code, "%s (unknown error)" % err.name)
    seq = getattr(err, "sequence", 0)
    lines = ["X Error of failed request:  %s" % text,
             "  Major opcode of failed request:  %d (%s)"
             % (err.major, _X_OPCODE_NAMES.get(err.major,
                                               "X_Unknown%d" % err.major))]
    if err.code == 2:
        lines.append("  Value in failed request:  0x%x" % err.bad_value)
    elif err.code == 5:
        lines.append("  AtomID (in failed request):  0x%x" % err.bad_value)
    elif err.code in _X_RESOURCE_ERRORS:
        lines.append("  Resource id in failed request:  0x%x"
                     % err.bad_value)
    lines.append("  Serial number of failed request:  %d" % seq)
    lines.append("  Current serial number in output stream:  %d" % seq)
    return "\n".join(lines) + "\n"


# -- injection seams (unit tests monkeypatch these) --------------------------


def _x11_connect(display):
    """An x11_mini.X11Conn, or None. Never raises."""
    if os.environ.get("WXPROP_NO_X"):
        return None
    try:
        from wwmctl import x11_mini
        return x11_mini.X11Conn(display)
    except Exception:
        return None


def _detect_backend():
    """A wdotool window backend, or None. Never raises."""
    try:
        from wdotool import backend_detect
        return backend_detect.detect()
    except Exception:
        return None


def _hostname() -> str:
    try:
        return socket.gethostname() or ""
    except OSError:
        return ""


class Session:
    """Lazy handles on the two planes."""

    def __init__(self, display=None):
        self.display = display
        self._x = "unset"
        self._backend = "unset"

    def x11(self):
        if self._x == "unset":
            self._x = _x11_connect(self.display)
        return self._x

    def backend(self):
        if self._backend == "unset":
            self._backend = _detect_backend()
        return self._backend

    def nodes(self):
        """[(node, win)] from the compositor, [] when unavailable. `node`
        is the raw tree dict for sway (carries "window" for XWayland
        views), or a minimal synthesized dict for generic backends."""
        b = self.backend()
        if b is None:
            return []
        nodes_fn = getattr(b, "_nodes", None)
        if nodes_fn is not None:
            try:
                return [(node, win) for node, win, _f, _ws in nodes_fn()]
            except Exception:
                pass
        try:
            wins = b.list()
        except Exception:
            return []
        out = []
        for w in wins:
            out.append(({"app_id": w.class_ or None, "name": w.title,
                         "pid": w.pid, "fullscreen_mode": 0,
                         "visible": w.visible, "sticky": False}, w))
        return out


# -- the synthesized native atom table --------------------------------------

_PREDEFINED_ATOMS = (
    "PRIMARY", "SECONDARY", "ARC", "ATOM", "BITMAP", "CARDINAL", "COLORMAP",
    "CURSOR", "CUT_BUFFER0", "CUT_BUFFER1", "CUT_BUFFER2", "CUT_BUFFER3",
    "CUT_BUFFER4", "CUT_BUFFER5", "CUT_BUFFER6", "CUT_BUFFER7", "DRAWABLE",
    "FONT", "INTEGER", "PIXMAP", "POINT", "RECTANGLE", "RESOURCE_MANAGER",
    "RGB_COLOR_MAP", "RGB_BEST_MAP", "RGB_BLUE_MAP", "RGB_DEFAULT_MAP",
    "RGB_GRAY_MAP", "RGB_GREEN_MAP", "RGB_RED_MAP", "STRING", "VISUALID",
    "WINDOW", "WM_COMMAND", "WM_HINTS", "WM_CLIENT_MACHINE", "WM_ICON_NAME",
    "WM_ICON_SIZE", "WM_NAME", "WM_NORMAL_HINTS", "WM_SIZE_HINTS",
    "WM_ZOOM_HINTS", "MIN_SPACE", "NORM_SPACE", "MAX_SPACE", "END_SPACE",
    "SUPERSCRIPT_X", "SUPERSCRIPT_Y", "SUBSCRIPT_X", "SUBSCRIPT_Y",
    "UNDERLINE_POSITION", "UNDERLINE_THICKNESS", "STRIKEOUT_ASCENT",
    "STRIKEOUT_DESCENT", "ITALIC_ANGLE", "X_HEIGHT", "QUAD_WIDTH", "WEIGHT",
    "POINT_SIZE", "RESOLUTION", "COPYRIGHT", "NOTICE", "FONT_NAME",
    "FAMILY_NAME", "FULL_NAME", "CAP_HEIGHT", "WM_CLASS", "WM_TRANSIENT_FOR",
)

_EXTENDED_ATOMS = (
    "UTF8_STRING", "WM_STATE", "WM_PROTOCOLS", "WM_DELETE_WINDOW",
    "WM_TAKE_FOCUS", "_NET_WM_NAME", "_NET_WM_ICON_NAME", "_NET_WM_PID",
    "_NET_WM_DESKTOP", "_NET_WM_STATE", "_NET_WM_STATE_FULLSCREEN",
    "_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_STICKY", "_NET_WM_STATE_FOCUSED",
    "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_ICON",
    "_NET_SUPPORTED", "_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING",
    "_NET_ACTIVE_WINDOW", "_NET_SUPPORTING_WM_CHECK", "_NET_CURRENT_DESKTOP",
    "_NET_NUMBER_OF_DESKTOPS", "_NET_DESKTOP_NAMES",
)


class NativeAtoms:
    """Fake atom table for the native plane: predefined X atoms keep their
    real ids (1..68) and EWMH names get stable synthesized ids; -f/-set
    intern new names process-locally, mirroring XInternAtom(create)."""

    def __init__(self):
        self.by_name = {}
        self.by_id = {}
        for i, name in enumerate(_PREDEFINED_ATOMS, start=1):
            self.by_name[name] = i
            self.by_id[i] = name
        for i, name in enumerate(_EXTENDED_ATOMS, start=0x100):
            self.by_name[name] = i
            self.by_id[i] = name
        self._next = 0x100 + len(_EXTENDED_ATOMS)

    def intern(self, name: str, create: bool) -> int:
        a = self.by_name.get(name)
        if a:
            return a
        if not create:
            return 0
        a = self._next
        self._next += 1
        self.by_name[name] = a
        self.by_id[a] = name
        return a

    def name(self, a: int):
        return self.by_id.get(a)


# -- property wire builders (native plane) -----------------------------------


def _p_string(s: str):
    try:
        data = s.encode("latin-1")
    except UnicodeEncodeError:
        data = s.encode("utf-8")  # legible under C-locale octal escapes
    return ("STRING", 8, data)


def _p_utf8(s: str):
    return ("UTF8_STRING", 8, s.encode("utf-8"))


def _p_cardinal(vals):
    return ("CARDINAL", 32,
            struct.pack("<%dI" % len(vals),
                        *[v & 0xFFFFFFFF for v in vals]))


def _p_window(vals):
    return ("WINDOW", 32,
            struct.pack("<%dI" % len(vals),
                        *[v & 0xFFFFFFFF for v in vals]))


def _p_atoms(atoms: NativeAtoms, names):
    ids = [atoms.intern(n, True) for n in names]
    return ("ATOM", 32, struct.pack("<%dI" % len(ids), *ids))


# -- targets -----------------------------------------------------------------


class XTarget:
    plane = "x"

    def __init__(self, conn, win: int):
        self.conn = conn
        self.win = win

    def intern(self, name: bytes, create: bool) -> bool:
        return bool(self.conn.atom(name.decode("latin-1"),
                                   only_if_exists=not create))

    def fetch(self, name: bytes):
        r = self.conn.read_property(self.win, name.decode("latin-1"))
        if r is None:
            return None
        tname, size, data = r
        return tname, size, data

    def list_names(self):
        out = []
        for a in self.conn.list_properties(self.win):
            name = self.conn.get_atom_name(a)
            if name is None:
                name = "undefined atom # 0x%x" % a
            out.append(name.encode("latin-1"))
        return out

    def atom_name(self, a: int):
        return self.conn.get_atom_name(a)

    def remove_prop(self, name: bytes) -> bool:
        return self.conn.delete_property(self.win, name.decode("latin-1"))

    def set_prop(self, name: bytes, type_name: str, size: int, data: bytes):
        self.conn.change_property(self.win, name.decode("latin-1"),
                                  type_name, size, data)


class NativeTarget:
    """Shared shape for native views and the native root: a synthesized,
    lazily rebuilt property dict rendered through the same formatter."""

    plane = "native"

    def __init__(self, sess: Session, atoms: NativeAtoms):
        self.sess = sess
        self.atoms = atoms

    def _props(self):  # {name_bytes: (type_name, size, wire)}
        raise NotImplementedError

    def intern(self, name: bytes, create: bool) -> bool:
        s = name.decode("latin-1")
        if s in self.atoms.by_name or name in self._props():
            return True
        if create:
            self.atoms.intern(s, True)
            return True
        return False

    def fetch(self, name: bytes):
        return self._props().get(name)

    def list_names(self):
        return list(self._props().keys())

    def atom_name(self, a: int):
        return self.atoms.name(a)


class NativeViewTarget(NativeTarget):
    def __init__(self, sess, atoms, node, win):
        super().__init__(sess, atoms)
        self.node = node
        self.win = win
        self.node_id = win.id

    def refresh(self):
        for node, win in self.sess.nodes():
            if win.id == self.node_id:
                self.node, self.win = node, win
                return True
        return False

    def _props(self):
        node, win = self.node, self.win
        props = {}
        states = []
        if node.get("fullscreen_mode"):
            states.append("_NET_WM_STATE_FULLSCREEN")
        if not node.get("visible", getattr(win, "visible", True)):
            states.append("_NET_WM_STATE_HIDDEN")
        if node.get("sticky"):
            states.append("_NET_WM_STATE_STICKY")
        props[b"_NET_WM_STATE"] = _p_atoms(self.atoms, states)
        props[b"_NET_WM_WINDOW_TYPE"] = _p_atoms(
            self.atoms, ["_NET_WM_WINDOW_TYPE_NORMAL"])
        desktop = getattr(win, "desktop", -1)
        props[b"_NET_WM_DESKTOP"] = _p_cardinal(
            [desktop if desktop >= 0 else 0xFFFFFFFF])
        pid = node.get("pid") or getattr(win, "pid", 0)
        if pid:
            props[b"_NET_WM_PID"] = _p_cardinal([pid])
        host = _hostname()
        if host:
            props[b"WM_CLIENT_MACHINE"] = _p_string(host)
        wp = node.get("window_properties") or {}
        instance = node.get("app_id") or wp.get("instance")
        cls = node.get("app_id") or wp.get("class")
        if instance or cls:
            tname, size, _ = _p_string("")
            data = (_p_string(instance or "")[2] + b"\0" +
                    _p_string(cls or "")[2] + b"\0")
            props[b"WM_CLASS"] = (tname, size, data)
        title = node.get("name")
        if title is None:
            title = getattr(win, "title", None)
        if title is not None:
            props[b"_NET_WM_NAME"] = _p_utf8(title)
            props[b"WM_NAME"] = _p_string(title)
        return props


class NativeRootTarget(NativeTarget):
    """-root without an X server: a minimal EWMH-ish root property set
    synthesized from the compositor (documented in WXPROP.md's terms: the
    _NET_SUPPORTING_WM_CHECK-ish set). _NET_SUPPORTING_WM_CHECK is 0x0 —
    there is no WM check window to point at."""

    node_id = None

    def refresh(self):
        return True

    def _props(self):
        sess = self.sess
        props = {}
        props[b"_NET_SUPPORTED"] = _p_atoms(self.atoms, [
            "_NET_SUPPORTED", "_NET_CLIENT_LIST", "_NET_ACTIVE_WINDOW",
            "_NET_SUPPORTING_WM_CHECK", "_NET_CURRENT_DESKTOP",
            "_NET_NUMBER_OF_DESKTOPS", "_NET_WM_NAME", "_NET_WM_PID",
            "_NET_WM_DESKTOP", "_NET_WM_STATE", "_NET_WM_STATE_FULLSCREEN",
            "_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_STICKY",
            "_NET_WM_WINDOW_TYPE",
        ])
        nodes = sess.nodes()
        props[b"_NET_CLIENT_LIST"] = _p_window([w.id for _n, w in nodes])
        active = 0
        for _n, w in nodes:
            if w.focused:
                active = w.id
                break
        props[b"_NET_ACTIVE_WINDOW"] = _p_window([active])
        b = sess.backend()
        num, cur = 1, 0
        try:
            num = b.num_desktops()
        except Exception:
            pass
        try:
            cur = max(b.get_desktop(), 0)
        except Exception:
            pass
        props[b"_NET_NUMBER_OF_DESKTOPS"] = _p_cardinal([num])
        props[b"_NET_CURRENT_DESKTOP"] = _p_cardinal([cur])
        props[b"_NET_SUPPORTING_WM_CHECK"] = _p_window([0])
        return props


# -- window selection --------------------------------------------------------


class MissingWindowTarget:
    """-id N with no X server and no matching compositor node. The error
    is deferred to the first window operation, so pure parse errors
    ("format specified without atom") still fire first, like xprop, whose
    window ids are only ever touched at property-access time."""

    plane = "missing"

    def __init__(self, wid: int):
        self.wid = wid

    def _fatal(self):
        raise FatalError("window id # 0x%x does not exists!" % self.wid)

    def intern(self, name, create=False):
        self._fatal()

    def fetch(self, name):
        self._fatal()

    def list_names(self):
        self._fatal()

    def atom_name(self, a):
        return None


def resolve_id(sess: Session, wid: int):
    """xprop -id N: a compositor node id resolves through the node (an
    XWayland node redirects to its real X window id); otherwise the id is
    handed to the X server, exactly like xprop. The error wording for a
    hopeless id is xprop's own (grammar and all)."""
    x = sess.x11()
    for node, win in sess.nodes():
        xid = node.get("window")
        if win.id == wid or xid == wid:
            if xid and x is not None:
                return XTarget(x, xid)
            return NativeViewTarget(sess, NativeAtoms(), node, win)
    if x is not None:
        return XTarget(x, wid)
    return MissingWindowTarget(wid)


def resolve_root(sess: Session):
    x = sess.x11()
    if x is not None:
        return XTarget(x, x.root())
    if sess.backend() is None:
        raise FatalError("cannot examine the root window: no X server and "
                         "no compositor backend")
    return NativeRootTarget(sess, NativeAtoms())


def _x_fetch_name(x, win: int):
    """XFetchName: WM_NAME only when its type is STRING (format 8)."""
    try:
        r = x.read_property(win, "WM_NAME")
    except Exception:
        return None
    if r is None or r[0] != "STRING" or r[1] != 8:
        return None
    return r[2].split(b"\0", 1)[0]


def _window_with_name(x, top: int, name: bytes, depth: int = 0):
    """dsimple.c's Window_With_Name: pre-order DFS, exact strcmp on
    WM_NAME, first match wins. Depth-bounded against a lying server."""
    if depth > 64:
        return 0
    if _x_fetch_name(x, top) == name:
        return top
    try:
        children = x.query_tree(top)
    except Exception:
        return 0
    for ch in children:
        w = _window_with_name(x, ch, name, depth + 1)
        if w:
            return w
    return 0


def resolve_name(sess: Session, name: str):
    """-name: real xprop semantics first (exact WM_NAME match, DFS from the
    X root) so X-plane behavior is untouched; the native plane then gets
    the same courtesy (exact title, then exact app_id) for windows real
    xprop could never see."""
    x = sess.x11()
    if x is not None:
        w = _window_with_name(x, x.root(), os.fsencode(name))
        if w:
            return XTarget(x, w)
    nodes = sess.nodes()
    for node, win in nodes:
        if node.get("window") and x is not None:
            continue  # X windows are only ever matched the X way above
        if node.get("name") == name:
            return NativeViewTarget(sess, NativeAtoms(), node, win)
    for node, win in nodes:
        if node.get("window") and x is not None:
            continue
        if node.get("app_id") == name:
            return NativeViewTarget(sess, NativeAtoms(), node, win)
    raise FatalError("No window with name %s exists!" % name)


def select_target(sess: Session, prog: str):
    """No selector: compositor next-focus selection with a stderr hint
    (the wwmctl pattern; there is no X pointer grab to borrow)."""
    b = sess.backend()
    if b is None:
        raise FatalError("can't select a window without a compositor "
                         "backend; use -root, -id or -name")
    sys.stderr.write("%s: focus the target window to select it\n" % prog)
    sys.stderr.flush()
    try:
        node_id = b.select_window()
    except Exception as e:
        raise FatalError("window selection failed: %s" % e) from None
    return resolve_id(sess, node_id)


# -- Show_Prop ---------------------------------------------------------------


def show_prop(formatter, target, out: bytearray, fmt_b, dfmt_b,
              prop_b: bytes):
    out += prop_b
    if not target.intern(prop_b, create=False):
        out += b":  no such atom on any window.\n"
        return
    r = target.fetch(prop_b)
    if r is None:
        out += b":  not found.\n"
        return
    type_name, size, wire = r
    formatter.render_property(out, prop_b, type_name, size, wire,
                              fmt_b, dfmt_b)


def show_all_props(formatter, target, out: bytearray):
    for name in target.list_names():
        show_prop(formatter, target, out, None, None, name)


# -- -spy --------------------------------------------------------------------


def _write_flush(data: bytes):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def spy_x(formatter, target: XTarget, specs):
    """After the initial dump: PropertyNotify -> Show_Prop, DestroyNotify ->
    exit 0, BadWindow/BadMatch -> empty line + exit 0 (xprop.c:2109)."""
    x = target.conn
    try:
        x.select_input(target.win, SPY_EVENT_MASK)
        while True:
            sys.stdout.buffer.flush()
            ev = x.next_event(None)
            if ev is None:
                continue
            if ev["type"] == "DestroyNotify":
                return 0
            if ev["type"] != "PropertyNotify":
                continue
            name = x.get_atom_name(ev["atom"])
            if name is None:
                name = "undefined atom # 0x%x" % ev["atom"]
            name_b = name.encode("latin-1")
            fmt_b = dfmt_b = None
            if specs is not None:
                for sname, sfmt, sdfmt in specs:
                    if sname == name_b:
                        fmt_b, dfmt_b = sfmt, sdfmt
                        break
                else:
                    continue
            out = bytearray()
            show_prop(formatter, target, out, fmt_b, dfmt_b, name_b)
            _write_flush(out)
    except Exception as e:
        code = getattr(e, "code", None)
        if code in (3, 8):  # BadWindow/BadMatch: the window went away
            _write_flush(b"\n")
            return 0
        raise


def _sway_ipc_events(backend, payload: bytes):
    """Subscribe on a fresh IPC socket; yield (event_type, data) forever.
    Reuses the sway backend's framing helpers (the documented-contract
    reuse pattern wwmctl established for _nodes)."""
    connect = getattr(backend, "_connect", None)
    send = getattr(backend, "_send", None)
    recv = getattr(backend, "_recv", None)
    if not (connect and send and recv):
        raise FatalError("-spy on a native window needs the sway backend")
    s = connect()
    try:
        send(s, _I3_SUBSCRIBE, payload)
        t, reply = recv(s)
        if (t & _I3_EVENT_BIT) or not (isinstance(reply, dict)
                                       and reply.get("success")):
            raise FatalError("sway IPC event subscription failed")
        while True:
            t, data = recv(s)
            if t & _I3_EVENT_BIT and isinstance(data, dict):
                yield t, data
    finally:
        try:
            s.close()
        except OSError:
            pass


# which synthesized properties a sway window event touches, in the order
# the X plane would emit them (WM_NAME before _NET_WM_NAME, like xterm)
_NATIVE_EVENT_PROPS = {
    "title": (b"WM_NAME", b"_NET_WM_NAME"),
    "fullscreen_mode": (b"_NET_WM_STATE",),
    "move": (b"_NET_WM_DESKTOP", b"_NET_WM_STATE"),
    "floating": (),
    "focus": (),
    "urgent": (),
}


def spy_native_view(formatter, target: NativeViewTarget, specs):
    backend = target.sess.backend()
    for t, data in _sway_ipc_events(backend, b'["window"]'):
        if t != _I3_EVENT_WINDOW:
            continue
        container = data.get("container") or {}
        if container.get("id") != target.node_id:
            continue
        change = data.get("change")
        if change == "close":
            return 0
        names = _NATIVE_EVENT_PROPS.get(change)
        if names is None or not names:
            continue
        if change == "move":
            target.refresh()  # workspace only lives in the tree
        else:
            target.node = container
        out = bytearray()
        for name_b in names:
            fmt_b = dfmt_b = None
            if specs is not None:
                for sname, sfmt, sdfmt in specs:
                    if sname == name_b:
                        fmt_b, dfmt_b = sfmt, sdfmt
                        break
                else:
                    continue
            show_prop(formatter, target, out, fmt_b, dfmt_b, name_b)
        if out:
            _write_flush(out)


def spy_native_root(formatter, target: NativeRootTarget, specs):
    backend = target.sess.backend()
    for t, data in _sway_ipc_events(backend, b'["window","workspace"]'):
        if t == _I3_EVENT_WINDOW:
            change = data.get("change")
            names = {"new": (b"_NET_CLIENT_LIST",),
                     "close": (b"_NET_CLIENT_LIST", b"_NET_ACTIVE_WINDOW"),
                     "focus": (b"_NET_ACTIVE_WINDOW",)}.get(change, ())
        elif t == _I3_EVENT_WORKSPACE:
            names = (b"_NET_CURRENT_DESKTOP", b"_NET_NUMBER_OF_DESKTOPS")
        else:
            continue
        out = bytearray()
        for name_b in names:
            fmt_b = dfmt_b = None
            if specs is not None:
                for sname, sfmt, sdfmt in specs:
                    if sname == name_b:
                        fmt_b, dfmt_b = sfmt, sdfmt
                        break
                else:
                    continue
            show_prop(formatter, target, out, fmt_b, dfmt_b, name_b)
        if out:
            _write_flush(out)
