"""Wayland wire helpers for the compositor fakes, and the server base the
two that boot alike share.

Deliberately NOT built on `fwcommon/wayland_mini.py`. These fakes are the
oracle for that client: a fake that marshalled with the code under test
would agree with it by construction, and the one thing these tests exist to
prove -- that the bytes on the wire are the bytes the protocol asks for --
would prove nothing. Everything here packs by hand.

`Server` is the accept loop, the message framing and the two wl_display
requests, for the fakes whose `wl_display.sync` reply is the plain
callback.done + delete_id pair (test_vkbd, test_vptr). The other five
compositor fakes answer sync differently -- some never, one only after a
mode event, one out of a subprocess -- so they take the marshallers only
and keep their own loops.
"""

import os
import socket
import struct
import tempfile
import threading


# -- marshalling --------------------------------------------------------------

def pad(n: int) -> int:
    """Zero bytes of padding after an `n`-byte string or array payload."""
    return -n % 4


def wstr(s) -> bytes:
    """A wire string: length including the NUL, the bytes, then padding."""
    b = (s.encode() if isinstance(s, str) else s) + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * pad(len(b))


def msg(oid: int, op: int, body: bytes = b"") -> bytes:
    """One wire message: object id, (size << 16) | opcode, body."""
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | op) + body


def marshal(args) -> bytes:
    """Typed arguments to a message body, over wayland_mini's own type set:
    u (uint), i (int), f (wl_fixed), s (string)."""
    out = b""
    for kind, v in args:
        if kind == "u":
            out += struct.pack("<I", v & 0xFFFFFFFF)
        elif kind == "i":
            out += struct.pack("<i", v)
        elif kind == "f":
            out += struct.pack("<i", int(v * 256))
        elif kind == "s":
            out += wstr(v)
        else:
            raise ValueError("bad arg type %r" % (kind,))
    return out


def read_str(payload: bytes, off: int) -> tuple[str, int]:
    """The string at `off`, and the offset just past its padding."""
    (n,) = struct.unpack_from("<I", payload, off)
    return payload[off + 4:off + 4 + n - 1].decode(), off + 4 + n + pad(n)


def unpack_bind(body: bytes):
    """wl_registry.bind: (name, interface, version, new_id)."""
    (name,) = struct.unpack_from("<I", body)
    iface, off = read_str(body, 4)
    ver, new_id = struct.unpack_from("<II", body, off)
    return name, iface, ver, new_id


# -- the server ---------------------------------------------------------------

class Server:
    """A compositor fake on a real Wayland socket, one thread per client.

    A subclass says which globals it advertises (`advertise`), what a bind
    means (`on_bind`) and what to do with a request on one of its own
    objects (`on_request`); the manager interface named by `MANAGER` is
    advertised last, and only when `manager_version` is not None -- None is
    the compositor that does not implement the protocol at all.

    Subclasses set their own attributes *before* calling super().__init__():
    the accept loop starts inside it.
    """

    #: the interface `manager_version` gates, advertised after `advertise()`
    MANAGER = None
    PREFIX = "wdotool-wl-"
    BACKLOG = 4

    def __init__(self, manager_version=1):
        self.manager_version = manager_version
        self.dir = tempfile.mkdtemp(prefix=self.PREFIX)
        self.path = os.path.join(self.dir, "wayland-fake")
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(self.BACKLOG)
        # A blocking accept() is not woken by close() on Linux, so poll
        # instead: the suite creates one of these per test and must not pay
        # a join timeout for each.
        self.srv.settimeout(0.05)
        self.connections = 0
        self.mgr_name = None     # registry name the manager was advertised as
        self.names = {}          # registry name -> (interface, nth of that)
        self.binds = []          # (interface, version)
        self.events = []         # every request on the object under test
        self.destroyed = 0
        self._registries = []    # (conn, registry id) per live client
        self._clients = []
        self._stop = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- what a subclass fills in
    def advertise(self):
        """[(interface, version)] to advertise before the manager."""
        return []

    def new_state(self) -> dict:
        """Extra per-connection state, on top of the registry id."""
        return {}

    def on_bind(self, conn, state, name, iface, version, new_id):
        """A wl_registry.bind for something other than the manager."""

    def on_request(self, conn, state, oid, opcode, body, fds):
        """A request on an object this server handed out."""

    # -- lifecycle
    def close(self):
        self._stop = True
        try:
            self.srv.close()
        except OSError:
            pass
        self._thread.join(timeout=5)
        self.drop_clients()
        try:
            os.unlink(self.path)
        except OSError:
            pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass

    def withdraw_manager(self):
        """wl_registry.global_remove for the manager, on every live
        connection -- a compositor withdrawing the protocol mid-session."""
        with self._lock:
            regs = list(self._registries)
        for conn, reg in regs:
            self._send(conn, reg, 1, struct.pack("<I", self.mgr_name or 0))

    def drop_clients(self):
        """Hang up on everyone -- what a compositor restart looks like to a
        client that was holding one of our objects."""
        with self._lock:
            socks = list(self._clients)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    # -- the accept loop and the framing
    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self._clients.append(conn)
            self.connections += 1
            threading.Thread(target=self._client, args=(conn,),
                             daemon=True).start()

    def _client(self, conn):
        buf = b""
        fds: list[int] = []
        state = dict(self.new_state(), registry=None)
        try:
            while not self._stop:
                data, anc, _flags, _addr = conn.recvmsg(65536, 4096)
                if not data:
                    return
                for level, typ, fddata in anc:
                    if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                        n = len(fddata) // 4
                        fds.extend(struct.unpack("%di" % n, fddata[:n * 4]))
                buf += data
                while len(buf) >= 8:
                    oid, sizeop = struct.unpack_from("<II", buf)
                    size, opcode = sizeop >> 16, sizeop & 0xFFFF
                    if size < 8 or len(buf) < size:
                        break
                    body, buf = buf[8:size], buf[size:]
                    self._request(conn, state, fds, oid, opcode, body)
        except OSError:
            return
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    def _request(self, conn, state, fds, oid, opcode, body):
        if oid == 1 and opcode == 0:            # wl_display.sync(callback)
            (cb,) = struct.unpack_from("<I", body)
            self._send(conn, cb, 0, struct.pack("<I", 0))   # callback.done
            self._send(conn, 1, 1, struct.pack("<I", cb))   # delete_id
            return
        if oid == 1 and opcode == 1:            # wl_display.get_registry
            (reg,) = struct.unpack_from("<I", body)
            state["registry"] = reg
            self._advertise(conn, reg)
            with self._lock:
                self._registries.append((conn, reg))
            return
        if oid == state["registry"] and opcode == 0:   # wl_registry.bind
            name, iface, ver, new_id = unpack_bind(body)
            self.binds.append((iface, ver))
            self.on_bind(conn, state, name, iface, ver, new_id)
            return
        self.on_request(conn, state, oid, opcode, body, fds)

    def _advertise(self, conn, reg):
        name = 1
        seen: dict[str, int] = {}
        for iface, ver in self.advertise():
            self.names[name] = (iface, seen.get(iface, 0))
            seen[iface] = seen.get(iface, 0) + 1
            self._global(conn, reg, name, iface, ver)
            name += 1
        if self.manager_version is not None:
            self.mgr_name = name
            self.names[name] = (self.MANAGER, 0)
            self._global(conn, reg, name, self.MANAGER, self.manager_version)

    # -- wire helpers
    def _send(self, conn, oid, opcode, body=b""):
        try:
            conn.sendall(msg(oid, opcode, body))
        except OSError:
            pass

    def _global(self, conn, reg, name, iface, version):
        self._send(conn, reg, 0,
                   struct.pack("<I", name) + wstr(iface)
                   + struct.pack("<I", version))

    def _error(self, conn, oid, code, text):
        self._send(conn, 1, 0,
                   struct.pack("<II", oid, code) + wstr(text))
