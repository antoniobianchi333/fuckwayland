#!/usr/bin/env python3
"""qmprec.py -- steady cadence QMP screendump recorder, host side.

    qmprec.py <qmp.sock> <outdir> <head[,head...]> <interval_ms> <max_frames>

One QMP connection stays open and `screendump` is issued on a fixed clock, so the
guest needs nothing installed and the compositor is irrelevant: this records what
the virtual GPU scans out, on GNOME, KDE, Xfce and sway alike.  `ppm` is the format
on purpose -- a 1280x720 dump costs about 3ms that way and about 240ms as png, and
the encoder reads ppm anyway.

Stops when <outdir>/.stop appears or after max_frames.  Frames are named
f<NNNN>-<head>.ppm, which is the order `ls | sort` gives back.
"""
import json
import os
import socket
import sys
import time

GPU = "gpu0"          # the -device id vmctl gives the virtio-vga


def main():
    sock_path, outdir, heads, interval_ms, max_frames = sys.argv[1:6]
    heads = [int(h) for h in heads.split(",")]
    interval = int(interval_ms) / 1000.0
    max_frames = int(max_frames)
    os.makedirs(outdir, exist_ok=True)
    stop = os.path.join(outdir, ".stop")

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(sock_path)
    f = s.makefile("rb")

    def send(o):
        s.sendall((json.dumps(o) + "\n").encode())

    def reply():
        while True:
            line = f.readline()
            if not line:
                raise SystemExit("qmprec: QMP connection closed")
            m = json.loads(line)
            if "return" in m or "error" in m:
                return m

    json.loads(f.readline() or b"{}")          # the greeting
    send({"execute": "qmp_capabilities"})
    reply()

    t0 = time.monotonic()
    i = 0
    while i < max_frames and not os.path.exists(stop):
        target = t0 + i * interval
        now = time.monotonic()
        if now < target:
            time.sleep(target - now)
        for h in heads:
            path = os.path.join(outdir, "f%04d-%d.ppm" % (i, h))
            send({"execute": "screendump",
                  "arguments": {"filename": path, "device": GPU,
                                "head": h, "format": "ppm"}})
            r = reply()
            if "error" in r:
                raise SystemExit("qmprec: screendump: %s" % r["error"])
        i += 1
    print(i)


main()
