"""OWNER: Agent X. Minimal pure-stdlib X11 wire client for wwmctl.

Talks straight to the XWayland server over its unix socket — enough of the
core protocol for wmctrl-style identity/property work and nothing more:
InternAtom, GetProperty (long properties via the offset loop), ChangeProperty,
SendEvent (ClientMessage to root), GetGeometry + TranslateCoordinates,
QueryTree, GetInputFocus (as the post-void-request sync). No resource ids are
ever allocated, no extensions, no big-requests; byte order 'l' only.

Error model: XUnavailable for anything connection-level (no server, bad
DISPLAY, auth rejected, connection lost), X11Error for errors the server
reports (BadWindow and friends). Callers (wwmctl.core) treat both as "degrade
gracefully".

Conventions: property values of format 32 are returned as unsigned 32-bit
ints (EWMH's -1 reads as 0xFFFFFFFF); get_prop_string() truncates at the
first NUL exactly like wmctrl's printf("%s") does.
"""

import os
import re
import socket
import struct

# Test seam: unit tests point this at a directory with a fake server socket.
_SOCK_DIR = "/tmp/.X11-unix"

_TIMEOUT = 5.0

# X protocol constants used below
_OP_INTERN_ATOM = 16
_OP_CHANGE_PROPERTY = 18
_OP_GET_PROPERTY = 20
_OP_SEND_EVENT = 25
_OP_GET_GEOMETRY = 14
_OP_QUERY_TREE = 15
_OP_TRANSLATE_COORDS = 40
_OP_GET_INPUT_FOCUS = 43
_CLIENT_MESSAGE = 33
_EVENT_MASK_SUBSTRUCTURE = 0x180000  # SubstructureNotify|SubstructureRedirect

_ERROR_NAMES = {
    1: "BadRequest", 2: "BadValue", 3: "BadWindow", 4: "BadPixmap",
    5: "BadAtom", 6: "BadCursor", 7: "BadFont", 8: "BadMatch",
    9: "BadDrawable", 10: "BadAccess", 11: "BadAlloc", 12: "BadColor",
    13: "BadGC", 14: "BadIDChoice", 15: "BadName", 16: "BadLength",
    17: "BadImplementation",
}

_FAMILY_LOCAL = 256
_FAMILY_WILD = 0xFFFF


class XUnavailable(Exception):
    """No usable X server (no socket, handshake refused, connection lost)."""


class X11Error(Exception):
    """An error packet from the X server, mapped from the wire."""

    def __init__(self, code: int, major: int, minor: int, bad_value: int):
        self.code = code
        self.major = major
        self.minor = minor
        self.bad_value = bad_value
        self.name = _ERROR_NAMES.get(code, "XError%d" % code)
        super().__init__("%s (major %d, bad value 0x%x)"
                         % (self.name, major, bad_value))


def _pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def _parse_display(d: str) -> tuple[int, int]:
    """'[host]:num[.screen]' -> (num, screen). Local displays only."""
    m = re.fullmatch(r"(.*?):(\d+)(?:\.(\d+))?", d.strip())
    if not m:
        raise XUnavailable('cannot parse DISPLAY "%s"' % d)
    host = m.group(1)
    if host not in ("", "unix", "localhost"):
        raise XUnavailable('non-local DISPLAY "%s" is not supported' % d)
    return int(m.group(2)), int(m.group(3) or 0)


def _read_xauth(path: str):
    """Parse the binary .Xauthority format: a list of
    (family, address, number, name, data) tuples, all lengths big-endian."""
    with open(path, "rb") as f:
        buf = f.read()
    entries = []
    off = 0
    try:
        while off < len(buf):
            (family,) = struct.unpack_from(">H", buf, off)
            off += 2
            fields = []
            for _ in range(4):
                (n,) = struct.unpack_from(">H", buf, off)
                off += 2
                fields.append(buf[off:off + n])
                off += n
            entries.append((family, *fields))
    except struct.error:
        pass  # truncated file: keep what parsed cleanly
    return entries


def _auth_candidates(display_num: int):
    """(name, data) pairs to try during setup, best match first, always
    ending with the cookie-less attempt (XWayland usually allows same-uid)."""
    path = os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority")
    try:
        entries = _read_xauth(path)
    except OSError:
        entries = []
    dnum = str(display_num).encode()
    try:
        host = socket.gethostname().encode()
    except OSError:
        host = b""
    exact, wild, other = [], [], []
    for family, addr, num, name, data in entries:
        if name != b"MIT-MAGIC-COOKIE-1" or num not in (b"", dnum):
            continue
        if family == _FAMILY_LOCAL and addr == host:
            exact.append((name, data))
        elif family == _FAMILY_WILD:
            wild.append((name, data))
        else:
            other.append((name, data))
    out = []
    for cand in exact + wild + other:
        if cand not in out:
            out.append(cand)
    out.append((b"", b""))
    return out


class X11Conn:
    """Synchronous X11 connection. One request in flight at a time."""

    def __init__(self, display: str | None = None):
        self._sock = None
        self._seq = 0
        self._atoms: dict[str, int] = {}
        if display is None:
            display = os.environ.get("DISPLAY")
        if display:
            candidates = [_parse_display(display)]
        else:
            try:
                nums = sorted(
                    int(m.group(1)) for n in os.listdir(_SOCK_DIR)
                    if (m := re.fullmatch(r"X(\d+)", n)))
            except OSError:
                nums = []
            if not nums:
                raise XUnavailable(
                    "no X display (DISPLAY unset, no sockets in %s)"
                    % _SOCK_DIR)
            candidates = [(n, 0) for n in nums]
        err = None
        for num, screen in candidates:
            try:
                self._connect(num, screen)
                return
            except XUnavailable as e:
                err = e
        raise err

    # -- connection setup ----------------------------------------------------

    def _open_socket(self, num: int):
        path = "%s/X%d" % (_SOCK_DIR, num)
        for target in (path, "\0" + path):  # filesystem, then abstract
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(_TIMEOUT)
            try:
                s.connect(target)
                return s
            except OSError:
                s.close()
        raise XUnavailable("cannot connect to X socket %s" % path)

    def _connect(self, num: int, screen: int):
        # A refused setup closes the connection, so each auth candidate gets
        # a fresh socket. Transport hiccups just advance to the next one.
        reason = "no authorization accepted"
        for name, data in _auth_candidates(num):
            sock = self._open_socket(num)
            try:
                ok, why = self._handshake(sock, name, data)
            except (OSError, XUnavailable) as e:
                sock.close()
                reason = str(e)
                continue
            if ok:
                self._sock = sock
                self._root = self._roots[min(screen, len(self._roots) - 1)]
                return
            sock.close()
            reason = why
        raise XUnavailable("X server :%d refused the connection: %s"
                           % (num, reason.strip()))

    def _handshake(self, sock, auth_name: bytes, auth_data: bytes):
        """One setup attempt. Returns (True, "") and fills in the roots on
        success; (False, reason) when the server refuses this auth."""
        sock.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0,
                                 len(auth_name), len(auth_data))
                     + _pad4(auth_name) + _pad4(auth_data))
        head = self._recv_exact(8, sock)
        status = head[0]
        (extra,) = struct.unpack_from("<H", head, 6)
        body = self._recv_exact(extra * 4, sock)
        if status == 0:  # Failed: byte 1 of the head is the reason length
            return False, body[:head[1]].decode("latin-1", "replace")
        if status != 1:  # Authenticate: reason is the whole (padded) body
            return False, body.rstrip(b"\0").decode("latin-1", "replace")
        (self._rid_base, self._rid_mask) = struct.unpack_from("<II", body, 4)
        (vlen,) = struct.unpack_from("<H", body, 16)
        nscreens, nformats = body[20], body[21]
        p = 32 + len(_pad4(b"\0" * vlen)) + 8 * nformats
        self._roots = []
        for _ in range(nscreens):
            (root,) = struct.unpack_from("<I", body, p)
            ndepths = body[p + 39]
            p += 40
            for _ in range(ndepths):
                (nvis,) = struct.unpack_from("<H", body, p + 2)
                p += 8 + 24 * nvis
            self._roots.append(root)
        if not self._roots:
            return False, "setup reply carries no screens"
        return True, ""

    # -- wire plumbing -------------------------------------------------------

    def _recv_exact(self, n: int, sock=None) -> bytes:
        sock = sock or self._sock
        chunks = []
        while n:
            try:
                b = sock.recv(n)
            except socket.timeout:
                raise XUnavailable("X server timed out") from None
            except OSError as e:
                raise XUnavailable("X connection lost: %s" % e) from None
            if not b:
                raise XUnavailable("X connection closed by server")
            chunks.append(b)
            n -= len(b)
        return b"".join(chunks)

    def _send(self, opcode: int, data_byte: int, payload: bytes = b"") -> int:
        """Send one request (payload already padded); returns its sequence."""
        if self._sock is None:
            raise XUnavailable("X connection is closed")
        try:
            self._sock.sendall(struct.pack("<BBH", opcode, data_byte,
                                           1 + len(payload) // 4) + payload)
        except OSError as e:
            raise XUnavailable("X connection lost: %s" % e) from None
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def _wait_reply(self, seq: int):
        """Read packets until the reply for `seq`. Any error packet raises
        X11Error (with the void-request sync below, an error always belongs
        to the request just issued). Events are skipped."""
        while True:
            pkt = self._recv_exact(32)
            kind = pkt[0] & 0x7F
            if kind == 0:
                bad, minor, major = struct.unpack_from("<IHB", pkt, 4)
                raise X11Error(pkt[1], major, minor, bad)
            if kind == 1:
                (pseq,) = struct.unpack_from("<H", pkt, 2)
                (extra,) = struct.unpack_from("<I", pkt, 4)
                body = self._recv_exact(extra * 4) if extra else b""
                if pseq == seq:
                    return pkt, body
                continue  # stale reply: drop
            if kind == 35:  # GenericEvent carries extra length
                (extra,) = struct.unpack_from("<I", pkt, 4)
                if extra:
                    self._recv_exact(extra * 4)
            # other events: nothing selected, but be tolerant and skip

    def _void(self, opcode: int, data_byte: int, payload: bytes):
        """A request with no reply, followed by a GetInputFocus roundtrip so
        the server has processed it and any error surfaces here."""
        self._send(opcode, data_byte, payload)
        self._wait_reply(self._send(_OP_GET_INPUT_FOCUS, 0))

    # -- public API (frozen per WWMCTL.md) -----------------------------------

    def root(self) -> int:
        return self._root

    def atom(self, name: str, only_if_exists: bool = False) -> int:
        a = self._atoms.get(name)
        if a is not None:
            return a
        nb = name.encode("latin-1")
        seq = self._send(_OP_INTERN_ATOM, 1 if only_if_exists else 0,
                         struct.pack("<H2x", len(nb)) + _pad4(nb))
        pkt, _ = self._wait_reply(seq)
        (a,) = struct.unpack_from("<I", pkt, 8)
        if a:
            self._atoms[name] = a
        return a

    def client_list(self) -> list[int]:
        """_NET_CLIENT_LIST on the root; QueryTree fallback when absent."""
        r = self._read_property(self._root, "_NET_CLIENT_LIST")
        if r is not None and r[1] == 32:
            return list(struct.unpack("<%dI" % (len(r[2]) // 4), r[2]))
        seq = self._send(_OP_QUERY_TREE, 0, struct.pack("<I", self._root))
        pkt, body = self._wait_reply(seq)
        (n,) = struct.unpack_from("<H", pkt, 16)
        return list(struct.unpack_from("<%dI" % n, body, 0))

    def get_prop_ints(self, win: int, name: str) -> list[int]:
        r = self._read_property(win, name)
        if r is None:
            return []
        _t, fmt, data = r
        if fmt == 32:
            return list(struct.unpack("<%dI" % (len(data) // 4), data))
        if fmt == 16:
            return list(struct.unpack("<%dH" % (len(data) // 2), data))
        return list(data)

    def get_prop_string(self, win: int, name: str) -> str:
        r = self._read_property(win, name)
        if r is None or r[1] != 8:
            return ""
        type_a, _fmt, data = r
        if type_a == self.atom("UTF8_STRING"):
            s = data.decode("utf-8", "replace")
        else:  # STRING and friends: latin-1 covers every byte
            s = data.decode("latin-1")
        return s.split("\0", 1)[0]

    def get_wm_class(self, win: int) -> tuple[str, str]:
        r = self._read_property(win, "WM_CLASS")
        if r is None or r[1] != 8:
            return "", ""
        parts = r[2].split(b"\0")
        instance = parts[0].decode("latin-1")
        class_ = parts[1].decode("latin-1") if len(parts) > 1 else ""
        return instance, class_

    def get_client_machine(self, win: int) -> str:
        return self.get_prop_string(win, "WM_CLIENT_MACHINE")

    def get_pid(self, win: int) -> int:
        ints = self.get_prop_ints(win, "_NET_WM_PID")
        return ints[0] if ints else 0

    def get_geometry(self, win: int) -> tuple[int, int, int, int]:
        """Root-relative (x, y, w, h): size from GetGeometry, position by
        translating the window origin to root coordinates (matches xwininfo
        and the compositor's idea of the rect — NOT wmctrl's doubled values
        under non-reparenting WMs)."""
        seq = self._send(_OP_GET_GEOMETRY, 0, struct.pack("<I", win))
        pkt, _ = self._wait_reply(seq)
        _x, _y, w, h = struct.unpack_from("<hhHH", pkt, 12)
        seq = self._send(_OP_TRANSLATE_COORDS, 0,
                         struct.pack("<IIhh", win, self._root, 0, 0))
        pkt, _ = self._wait_reply(seq)
        x, y = struct.unpack_from("<hh", pkt, 12)
        return x, y, w, h

    def set_name(self, win: int, name: str, icon: bool, long_: bool) -> None:
        """wmctrl -N/-I/-T semantics: long_ sets WM_NAME/_NET_WM_NAME, icon
        sets WM_ICON_NAME/_NET_WM_ICON_NAME; the legacy property as STRING
        (latin-1), the EWMH one as UTF8_STRING."""
        utf8 = name.encode("utf-8")
        latin = name.encode("latin-1", "replace")
        pairs = []
        if long_:
            pairs.append(("WM_NAME", "_NET_WM_NAME"))
        if icon:
            pairs.append(("WM_ICON_NAME", "_NET_WM_ICON_NAME"))
        for legacy, net in pairs:
            self._change_property(win, self.atom(legacy),
                                  self.atom("STRING"), latin)
            self._change_property(win, self.atom(net),
                                  self.atom("UTF8_STRING"), utf8)

    def send_root_message(self, win: int, type_name: str,
                          data: list[int]) -> None:
        """ClientMessage (format 32) about `win`, sent to the root window
        with SubstructureNotify|SubstructureRedirect — how every EWMH client
        request travels."""
        vals = (list(data) + [0] * 5)[:5]
        event = struct.pack("<BBHII", _CLIENT_MESSAGE, 32, 0,
                            win & 0xFFFFFFFF, self.atom(type_name))
        event += struct.pack("<5I", *((v & 0xFFFFFFFF) for v in vals))
        self._void(_OP_SEND_EVENT, 0,
                   struct.pack("<II", self._root,
                               _EVENT_MASK_SUBSTRUCTURE) + event)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- property machinery --------------------------------------------------

    def _get_property_chunk(self, win: int, prop: int, offset: int,
                            length: int):
        payload = struct.pack("<IIIII", win, prop, 0, offset, length)
        seq = self._send(_OP_GET_PROPERTY, 0, payload)
        pkt, body = self._wait_reply(seq)
        fmt = pkt[1]
        type_a, bytes_after, nitems = struct.unpack_from("<III", pkt, 8)
        return type_a, fmt, body[:nitems * (fmt // 8)], bytes_after

    def _read_property(self, win: int, name: str):
        """Full value of a property: (type_atom, format, bytes), or None if
        the property does not exist. Long values are fetched via the
        long-offset loop (offsets counted in 32-bit units)."""
        prop = self.atom(name, only_if_exists=True)
        if not prop:
            return None
        data = b""
        offset = 0
        while True:
            type_a, fmt, chunk, after = self._get_property_chunk(
                win, prop, offset, 0x40000)
            if type_a == 0:
                return None
            data += chunk
            if after == 0 or not chunk:  # not chunk: server misbehaving
                return type_a, fmt, data
            offset += len(chunk) // 4

    def _change_property(self, win: int, prop: int, type_a: int,
                         data: bytes, fmt: int = 8):
        payload = struct.pack("<IIIB3xI", win, prop, type_a, fmt,
                              len(data) // (fmt // 8)) + _pad4(data)
        self._void(_OP_CHANGE_PROPERTY, 0, payload)  # PropModeReplace
