#!/usr/bin/env python3
"""A wire-level fake wlroots compositor: wl_display plus
zwlr_output_manager_v1 with one head, which is everything wxrandr's generic
wlroots backend talks to.

    fake_wlr.py <socket> <mode> [<log>]

modes:
  normal          answer everything
  silent-apply    take the apply, answer `succeeded`, then never speak again
  mute-apply      take the apply and never answer at all
  silent-start    complete the registry handshake, then never speak again

`log`, when given, gets one `apply` line per zwlr_output_configuration_v1.apply
it receives, so a test can assert that a refused command sent nothing.
"""
import os
import socket
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wl_fake import marshal, pad

SOCK, MODE = sys.argv[1], sys.argv[2]
LOG = sys.argv[3] if len(sys.argv) > 3 else None

HEAD_ID = 0xFF000001
MODE_ID = 0xFF000002


class Peer:
    def __init__(self, c):
        self.c = c
        self.buf = b""
        self.mute = False

    def send(self, oid, op, args=()):
        if self.mute:
            return
        body = marshal(args)
        self.c.sendall(struct.pack("<II", oid, ((8 + len(body)) << 16) | op)
                       + body)

    def read_msg(self):
        while True:
            if len(self.buf) >= 8:
                oid, so = struct.unpack_from("<II", self.buf)
                size, op = so >> 16, so & 0xFFFF
                if size >= 8 and len(self.buf) >= size:
                    payload = self.buf[8:size]
                    self.buf = self.buf[size:]
                    return oid, op, payload
            data = self.c.recv(65536)
            if not data:
                return None
            self.buf += data


def rd_u32(p, i):
    return struct.unpack_from("<I", p, i)[0], i + 4


def rd_str(p, i):
    n, i = rd_u32(p, i)
    s = p[i:i + n - 1].decode()
    return s, i + n + pad(n)


def note(what):
    if LOG:
        with open(LOG, "a") as f:
            f.write(what + "\n")


srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    os.unlink(SOCK)
except OSError:
    pass
srv.bind(SOCK)
srv.listen(4)
print("listening on %s (%s)" % (SOCK, MODE), flush=True)

while True:
    conn, _ = srv.accept()
    if os.fork():            # one child per client: a holder that keeps its
        conn.close()         # connection open must not block the next client
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except OSError:
            pass
        continue
    srv.close()
    p = Peer(conn)
    registry = None
    mgr = None
    configs = set()
    announced = False
    try:
        while True:
            m = p.read_msg()
            if m is None:
                break
            oid, op, payload = m
            if oid == 1 and op == 0:            # wl_display.sync
                cb, _ = rd_u32(payload, 0)
                p.send(cb, 0, [("u", 1)])       # callback.done
            elif oid == 1 and op == 1:          # wl_display.get_registry
                registry, _ = rd_u32(payload, 0)
                p.send(registry, 0, [("u", 1), ("s", "wl_compositor"),
                                     ("u", 4)])
                p.send(registry, 0, [("u", 2), ("s", "zwlr_output_manager_v1"),
                                     ("u", 4)])
                p.send(registry, 0, [("u", 3), ("s", "wl_output"), ("u", 4)])
            elif oid == registry and op == 0:   # bind
                name, i = rd_u32(payload, 0)
                iface, i = rd_str(payload, i)
                ver, i = rd_u32(payload, i)
                newid, i = rd_u32(payload, i)
                if iface == "zwlr_output_manager_v1":
                    mgr = newid
                    if not announced:
                        announced = True
                        p.send(mgr, 0, [("u", HEAD_ID)])          # head
                        p.send(HEAD_ID, 0, [("s", "HEAD-1")])     # name
                        p.send(HEAD_ID, 1, [("s", "fake head")])  # description
                        p.send(HEAD_ID, 2, [("i", 300), ("i", 200)])
                        p.send(HEAD_ID, 3, [("u", MODE_ID)])      # mode
                        p.send(MODE_ID, 0, [("i", 1920), ("i", 1080)])
                        p.send(MODE_ID, 1, [("i", 60000)])
                        p.send(MODE_ID, 2, [])                    # preferred
                        p.send(HEAD_ID, 4, [("i", 1)])            # enabled
                        p.send(HEAD_ID, 5, [("u", MODE_ID)])      # current_mode
                        p.send(HEAD_ID, 6, [("i", 0), ("i", 0)])  # position
                        p.send(HEAD_ID, 7, [("i", 0)])            # transform
                        p.send(HEAD_ID, 8, [("f", 1.0)])          # scale
                        p.send(HEAD_ID, 10, [("s", "Fake")])
                        p.send(HEAD_ID, 11, [("s", "Head")])
                        p.send(HEAD_ID, 12, [("s", "0001")])
                        p.send(mgr, 1, [("u", 1)])                # done(serial)
                        if MODE == "silent-start":
                            p.mute = True
            elif mgr is not None and oid == mgr and op == 0:
                cfg, _ = rd_u32(payload, 0)     # create_configuration
                configs.add(cfg)
            elif oid in configs and op == 2:     # configuration.apply
                note("apply")
                if MODE == "mute-apply":
                    p.mute = True
                else:
                    p.send(oid, 0, [])          # succeeded
                    if MODE == "silent-apply":
                        p.mute = True
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
        os._exit(0)
