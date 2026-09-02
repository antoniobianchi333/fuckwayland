"""Minimal pure-stdlib Wayland wire-protocol client. Shared by daemon.py (output
geometry) and backend_wlr.py (foreign-toplevel). Owner: shared — coordinate via
DESIGN.md before changing the API; fixes for wire-level bugs are fair game.

Usage sketch:

    c = WlConn(socket_path)
    reg = c.get_registry()                # {name: (interface, version)} after roundtrip
    oid = c.bind(name, "wl_output", min(version, 4))
    c.on(oid, handler)                    # handler(opcode, Cursor, fds)
    c.roundtrip()                         # dispatch until sync callback fires
"""

import os
import socket
import struct


class Cursor:
    """Reads wire-format arguments out of one event's payload."""

    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def u32(self) -> int:
        (v,) = struct.unpack_from("<I", self.d, self.i)
        self.i += 4
        return v

    def i32(self) -> int:
        (v,) = struct.unpack_from("<i", self.d, self.i)
        self.i += 4
        return v

    def fixed(self) -> float:
        return self.i32() / 256.0

    def string(self) -> str:
        n = self.u32()  # length including NUL; 0 means null string
        s = self.d[self.i : self.i + max(n - 1, 0)].decode("utf-8", "replace")
        self.i += (n + 3) & ~3
        return s

    def array(self) -> bytes:
        n = self.u32()
        a = self.d[self.i : self.i + n]
        self.i += (n + 3) & ~3
        return a


def _marshal(args) -> bytes:
    """args: list of (type, value); type in {"u","i","f","s","a"} — new_id is "u"."""
    out = b""
    for t, v in args:
        if t == "u":
            out += struct.pack("<I", v & 0xFFFFFFFF)
        elif t == "i":
            out += struct.pack("<i", v)
        elif t == "f":
            out += struct.pack("<i", int(v * 256))
        elif t == "s":
            b = v.encode() + b"\0"
            out += struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)
        elif t == "a":
            out += struct.pack("<I", len(v)) + v + b"\0" * (-len(v) % 4)
        else:
            raise ValueError(f"bad arg type {t}")
    return out


class WlConn:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.next_id = 2  # 1 is wl_display
        self.buf = b""
        self.fds: list[int] = []
        self.handlers = {}  # obj_id -> callable(opcode, Cursor, fds:list)
        self.registry: dict[int, tuple[str, int]] = {}
        self.registry_id = None
        self.dead = None

        def display_handler(op, cur, fds):
            if op == 0:  # error(object_id, code, message)
                obj, code, msg = cur.u32(), cur.u32(), cur.string()
                self.dead = f"wayland protocol error on object {obj} code {code}: {msg}"
            # op 1 = delete_id; ids are never reused here, ignore

        self.handlers[1] = display_handler

    def alloc(self) -> int:
        i = self.next_id
        self.next_id += 1
        return i

    def on(self, obj_id: int, handler):
        self.handlers[obj_id] = handler

    def send(self, obj_id: int, opcode: int, args=()):
        body = _marshal(args)
        msg = struct.pack("<II", obj_id, ((8 + len(body)) << 16) | opcode) + body
        self.sock.sendall(msg)

    def get_registry(self) -> dict[int, tuple[str, int]]:
        """Bind the registry (once), roundtrip, return {name: (interface, version)}."""
        if self.registry_id is None:
            self.registry_id = self.alloc()
            self.send(1, 1, [("u", self.registry_id)])  # wl_display.get_registry

            def reg_handler(op, cur, fds):
                if op == 0:  # global(name, interface, version)
                    name, iface, ver = cur.u32(), cur.string(), cur.u32()
                    self.registry[name] = (iface, ver)
                elif op == 1:  # global_remove(name)
                    self.registry.pop(cur.u32(), None)

            self.on(self.registry_id, reg_handler)
            self.roundtrip()
        return self.registry

    def bind(self, name: int, interface: str, version: int) -> int:
        oid = self.alloc()
        self.send(
            self.registry_id, 0,
            [("u", name), ("s", interface), ("u", version), ("u", oid)],
        )
        return oid

    def find_global(self, interface: str) -> tuple[int, int] | None:
        """(name, version) of a global by interface, or None."""
        for name, (iface, ver) in self.get_registry().items():
            if iface == interface:
                return name, ver
        return None

    def roundtrip(self):
        """wl_display.sync, then dispatch events until the callback fires."""
        cb = self.alloc()
        done = []
        self.on(cb, lambda op, cur, fds: done.append(1))
        self.send(1, 0, [("u", cb)])  # wl_display.sync
        while not done:
            self._dispatch_some()
        del self.handlers[cb]

    def dispatch(self, timeout: float | None = None) -> bool:
        """Dispatch pending events; False on timeout without data."""
        self.sock.settimeout(timeout)
        try:
            self._dispatch_some()
        except TimeoutError:
            return False
        finally:
            self.sock.settimeout(None)
        return True

    def _dispatch_some(self):
        if self.dead:
            raise RuntimeError(self.dead)
        data, anc, _flags, _addr = self.sock.recvmsg(65536, 4096)
        if not data:
            raise RuntimeError("wayland connection closed")
        for level, typ, fddata in anc:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                n = len(fddata) // 4
                self.fds.extend(struct.unpack(f"{n}i", fddata[: n * 4]))
        self.buf += data
        while len(self.buf) >= 8:
            obj_id, sizeop = struct.unpack_from("<II", self.buf)
            size, opcode = sizeop >> 16, sizeop & 0xFFFF
            if size < 8 or len(self.buf) < size:
                break
            payload = self.buf[8:size]
            self.buf = self.buf[size:]
            h = self.handlers.get(obj_id)
            if h:
                h(opcode, Cursor(payload), self.fds)
        if self.dead:
            raise RuntimeError(self.dead)

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.sock.close()
