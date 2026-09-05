#!/usr/bin/env python3
"""A Plasma 6.7 compositor's output protocols, driven off the upstream XML.

Not a hand-written mock: every event this serves, its opcode, its argument
types and its `since` gate are read out of plasma-wayland-protocols'
kde-output-device-v2.xml / kde-output-management-v2.xml at run time, and the
order of the initial burst is KWin's own
OutputDeviceV2InterfacePrivate::kde_output_device_v2_bind_resource().

It exists because no KDE image here is Plasma 6.7, so the registry discovery
path -- `kde_output_device_registry_v2`, how 6.7 and newer publish outputs --
has no real compositor to talk to. Point wxrandr at this and it takes that
path end to end, including the three ways it can go wrong.

    $ curl -sO <plasma-wayland-protocols>/src/protocols/kde-output-device-v2.xml
    $ curl -sO <plasma-wayland-protocols>/src/protocols/kde-output-management-v2.xml
    $ python3 kde-outreg-specfake.py kde-output-device-v2.xml \
          kde-output-management-v2.xml /tmp/sf.sock 23 21 &
    $ WAYLAND_DISPLAY=/tmp/sf.sock python3 dist/wxrandr --query

Usage: <devxml> <mgmtxml> <socketpath> [regversion] [mgmtversion] [mode]

`regversion`/`mgmtversion` are what the compositor advertises: 23/21 is KWin
6.7.0-6.7.4, 25/22 is master, and 20 is a registry too old to bind. `mode` is
how it should misbehave -- see MODE below.
"""
import os
import socket
import struct
import sys
import threading
import xml.etree.ElementTree as ET

SERVER_ID_BASE = 0xFF000000

# How this run should misbehave, to exercise the client's failure paths:
#   normal   -- a faithful KWin 6.7
#   empty    -- registry binds, announces no output at all
#   silent   -- registry binds and then answers nothing, ever (not even sync)
#   badop    -- the `output` event arrives on an opcode nobody documented
MODE = ["normal"]


def load(path):
    root = ET.parse(path).getroot()
    out = {}
    for i in root.findall("interface"):
        ev = []
        for n, e in enumerate(i.findall("event")):
            args = [(a.get("name"), a.get("type")) for a in e.findall("arg")]
            ev.append((n, e.get("name"), int(e.get("since") or 1), args))
        rq = []
        for n, r in enumerate(i.findall("request")):
            args = [(a.get("name"), a.get("type")) for a in r.findall("arg")]
            rq.append((n, r.get("name"), int(r.get("since") or 1), args))
        out[i.get("name")] = {"version": int(i.get("version")),
                              "events": ev, "requests": rq}
    return out


def s(v):
    b = v.encode() + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)


def msg(oid, op, body=b""):
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | op) + body


class Cur:
    def __init__(self, d):
        self.d, self.i = d, 0

    def u32(self):
        (v,) = struct.unpack_from("<I", self.d, self.i)
        self.i += 4
        return v

    def i32(self):
        (v,) = struct.unpack_from("<i", self.d, self.i)
        self.i += 4
        return v

    def string(self):
        n = self.u32()
        raw = self.d[self.i:self.i + max(n - 1, 0)]
        self.i += (n + 3) & ~3
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


# KWin src/wayland/outputdevice_v2.cpp, kde_output_device_v2_bind_resource(): the exact order the compositor
# sends the initial burst in.  "@modes" is where sendNewMode() runs for every mode, "@current" is
# sendCurrentMode().
BURST = [
    "geometry", "scale", "eisa_id", "name", "serial_number",
    "@modes", "@current",
    "uuid", "edid", "enabled", "capabilities", "overscan", "vrr_policy",
    "rgb_range", "high_dynamic_range", "sdr_brightness", "wide_color_gamut",
    "auto_rotate_policy", "icc_profile_path", "brightness_metadata",
    "brightness_overrides", "sdr_gamut_wideness", "color_profile_source",
    "brightness", "color_power_tradeoff", "dimming", "replication_source",
    "ddc_ci_allowed", "max_bits_per_color", "max_bits_per_color_range",
    "automatic_max_bits_per_color_limit", "edr_policy", "sharpness",
    "priority", "auto_brightness", "hdr_icc_profile_path",
    "hdr_color_profile_source", "abm_level",
    "done",
]

HEADS = [
    {"name": "DP-1", "make": "Dell Inc.", "model": "U2723QE",
     "serial": "H4ZM33", "uuid": "e6f1b2c4-0001", "mm": (600, 340),
     "x": 0, "y": 0, "scale": 1.0, "transform": 0, "enabled": 1,
     "modes": [(3840, 2160, 59997, 1), (2560, 1440, 59951, 0),
               (1920, 1080, 60000, 0)], "current": 0, "priority": 1},
    {"name": "eDP-1", "make": "AU Optronics", "model": "B140HAN",
     "serial": "", "uuid": "e6f1b2c4-0002", "mm": (309, 174),
     "x": 3840, "y": 0, "scale": 1.0, "transform": 0, "enabled": 1,
     "modes": [(1920, 1080, 60020, 1), (1280, 720, 59860, 0)],
     "current": 0, "priority": 2},
]


class Client(threading.Thread):
    def __init__(self, srv, sock):
        super().__init__(daemon=True)
        self.srv, self.sock = srv, sock
        self.buf = b""
        self.registry = None
        self.next_sid = SERVER_ID_BASE
        self.reg_obj = None
        self.reg_ver = 0
        self.mgmt = None
        self.mgmt_ver = 0
        self.order_obj = None
        self.devs = {}          # device oid -> head
        self.configs = {}

    def alloc(self):
        i = self.next_sid
        self.next_sid += 1
        return i

    def send(self, b):
        try:
            self.sock.sendall(b)
        except OSError:
            pass

    def run(self):
        while True:
            try:
                d = self.sock.recv(65536)
            except OSError:
                return
            if not d:
                return
            self.buf += d
            while len(self.buf) >= 8:
                oid, so = struct.unpack_from("<II", self.buf)
                size, op = so >> 16, so & 0xFFFF
                if size < 8 or len(self.buf) < size:
                    break
                body = self.buf[8:size]
                self.buf = self.buf[size:]
                try:
                    self.request(oid, op, Cur(body))
                except Exception as e:            # noqa: BLE001
                    print("specfake: %r" % (e,), file=sys.stderr)

    # -- requests
    def request(self, oid, op, cur):
        if MODE[0] == "silent" and self.reg_obj is not None:
            return                                     # never answer again
        if oid == 1 and op == 0:                       # wl_display.sync
            self.send(msg(cur.u32(), 0, struct.pack("<I", 0)))
        elif oid == 1 and op == 1:                     # get_registry
            self.registry = cur.u32()
            for gname, iface, ver in self.srv.globals:
                self.send(msg(self.registry, 0,
                              struct.pack("<I", gname) + s(iface)
                              + struct.pack("<I", ver)))
        elif oid == self.registry and op == 0:         # bind
            gname, iface, ver, nid = (cur.u32(), cur.string(), cur.u32(),
                                      cur.u32())
            self.bind(iface, ver, nid)
        elif oid == self.mgmt and op == 0:             # create_configuration
            self.configs[cur.u32()] = []
        elif oid in self.configs:
            self.config(oid, op, cur)

    def bind(self, iface, ver, nid):
        if iface == "kde_output_device_registry_v2":
            if ver < 21:
                # kde_output_device_registry_v2::error::unsupported_version = 0
                self.send(msg(1, 0, struct.pack("<II", nid, 0)
                              + s("unsupported version")))
                return
            self.reg_obj, self.reg_ver = nid, ver
            if MODE[0] == "empty":
                return
            for h in HEADS:
                self.offer(h)
        elif iface == "kde_output_management_v2":
            self.mgmt, self.mgmt_ver = nid, ver
        elif iface == "kde_output_order_v1":
            self.order_obj = nid
            out = b""
            for h in sorted((h for h in HEADS if h["enabled"]),
                            key=lambda h: h["priority"]):
                out += msg(nid, 0, s(h["name"]))
            self.send(out + msg(nid, 1))

    # -- the device burst, generated from the XML
    def offer(self, h):
        oid = self.alloc()
        self.devs[oid] = h
        ev = {name: (n, since, args)
              for n, name, since, args in
              self.srv.dev["kde_output_device_v2"]["events"]}
        mev = {name: (n, since, args)
               for n, name, since, args in
               self.srv.dev["kde_output_device_mode_v2"]["events"]}
        # kde_output_device_registry_v2.output(new_id) -- opcode from the XML
        out_op = {name: n for n, name, _s, _a
                  in self.srv.dev["kde_output_device_registry_v2"]["events"]}
        op = 7 if MODE[0] == "badop" else out_op["output"]
        self.send(msg(self.reg_obj, op, struct.pack("<I", oid)))
        ver = self.reg_ver          # the device resource takes the registry's
        buf = b""
        modeids = []
        for step in BURST:
            if step == "@modes":
                for w, ht, r, pref in h["modes"]:
                    mid = self.alloc()
                    modeids.append(mid)
                    n, _si, _a = ev["mode"]
                    buf += msg(oid, n, struct.pack("<I", mid))
                    buf += msg(mid, mev["size"][0], struct.pack("<ii", w, ht))
                    buf += msg(mid, mev["refresh"][0], struct.pack("<i", r))
                    if pref:
                        buf += msg(mid, mev["preferred"][0])
                    if ver >= mev["flags"][1]:
                        buf += msg(mid, mev["flags"][0], struct.pack("<I", 0))
                continue
            if step == "@current":
                if h["enabled"]:
                    buf += msg(oid, ev["current_mode"][0],
                               struct.pack("<I", modeids[h["current"]]))
                continue
            n, since, args = ev[step]
            if ver < since:
                continue
            buf += msg(oid, n, self.payload(step, args, h))
        self.send(buf)

    def payload(self, name, args, h):
        vals = {
            "geometry": lambda: (struct.pack("<iiiii", h["x"], h["y"],
                                             h["mm"][0], h["mm"][1], 1)
                                 + s(h["make"]) + s(h["model"])
                                 + struct.pack("<i", h["transform"])),
            "scale": lambda: struct.pack("<i", int(h["scale"] * 256)),
            "eisa_id": lambda: s(""),
            "name": lambda: s(h["name"]),
            "serial_number": lambda: s(h["serial"]),
            "uuid": lambda: s(h["uuid"]),
            "edid": lambda: s("AP///////wAQrDdBTThaMg=="),
            "enabled": lambda: struct.pack("<i", h["enabled"]),
            "replication_source": lambda: s(""),
            "priority": lambda: struct.pack("<I", h["priority"]),
            "icc_profile_path": lambda: s(""),
            "hdr_icc_profile_path": lambda: s(""),
        }
        if name in vals:
            return vals[name]()
        # everything else: fill each argument by its declared wire type
        out = b""
        for _an, at in args:
            if at in ("uint", "object", "new_id"):
                out += struct.pack("<I", 0)
            elif at in ("int", "fixed"):
                out += struct.pack("<i", 0)
            elif at == "string":
                out += s("")
            elif at == "array":
                out += struct.pack("<I", 0)
            else:
                raise SystemExit("specfake: unhandled arg type %r in %s"
                                 % (at, name))
        return out

    def config(self, cid, op, cur):
        rq = {n: (name, args) for n, name, _s, args
              in self.srv.mgmt["kde_output_configuration_v2"]["requests"]}
        if op not in rq:
            print("specfake: unknown configuration opcode %d" % op,
                  file=sys.stderr)
            return
        name, args = rq[op]
        self.configs[cid].append(name)
        if name == "apply":
            ev = {n: nm for n, nm, _s, _a
                  in self.srv.mgmt["kde_output_configuration_v2"]["events"]}
            applied = [k for k, v in ev.items() if v == "applied"][0]
            print("specfake: apply: %s" % " ".join(self.configs[cid]))
            self.send(msg(cid, applied))


class Server:
    def __init__(self, dev, mgmt, path, regver, mgmtver):
        self.dev, self.mgmt = dev, mgmt
        self.globals = [
            (1, "kde_output_device_registry_v2", regver),
            (2, "kde_output_management_v2", mgmtver),
            (3, "kde_output_order_v1", 1),
        ]
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(path):
            os.unlink(path)
        self.srv.bind(path)
        self.srv.listen(8)

    def serve(self):
        while True:
            c, _ = self.srv.accept()
            Client(self, c).start()


if __name__ == "__main__":
    devxml, mgmtxml, path = sys.argv[1], sys.argv[2], sys.argv[3]
    regv = int(sys.argv[4]) if len(sys.argv) > 4 else 23
    mgmtv = int(sys.argv[5]) if len(sys.argv) > 5 else 21
    MODE[0] = sys.argv[6] if len(sys.argv) > 6 else "normal"
    Server(load(devxml), load(mgmtxml), path, regv, mgmtv).serve()
