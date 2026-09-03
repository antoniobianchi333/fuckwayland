#!/usr/bin/env python3
"""A stand-in xrandr for the warandr GUI test: a tiny RandR simulator.

Query forms (``--query``, ``-q``, ``--verbose``, ``--current``, bare) render
the simulated screen in xrandr 1.5.4's text format through wxrandr.core's
byte-parity renderers, plus one hand-written ``disconnected`` output (a
compositor never has those, real X servers do).  ``--version`` answers like
xrandr 1.5.4.

Any other argv is an *apply*: it is appended as one JSON line to
``$FAKE_XRANDR_LOG`` and folded into the simulated state kept in
``$FAKE_XRANDR_STATE`` (default: next to the log), so a reload after Apply
sees the new layout exactly like the real tools would show it.
``FAKE_XRANDR_FAIL=<message>`` makes the next apply print that message to
stderr and exit 1 without touching the state.
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from wxrandr import core  # noqa: E402

DISCONNECTED = ("HDMI-2 disconnected "
                "(normal left inverted right x axis y axis)")

DEFAULT = {
    "primary": "DP-1",
    "outputs": [
        {"name": "DP-1", "active": True, "x": 0, "y": 0, "transform": "normal",
         "scale": 1.0, "current": 0, "ident": 0x42, "mm": [598, 336],
         "modes": [[1920, 1080, 60000, True], [1920, 1080, 50000, False],
                   [1280, 720, 60000, False]]},
        {"name": "HDMI-1", "active": True, "x": 1920, "y": 0,
         "transform": "normal", "scale": 1.0, "current": 0, "ident": 0x43,
         "mm": [376, 301],
         "modes": [[1280, 1024, 60020, True], [1280, 1024, 75025, False],
                   [1024, 768, 60004, False]]},
        {"name": "DP-2", "active": True, "x": 3200, "y": 0,
         "transform": "normal", "scale": 1.0, "current": 0, "ident": 0x44,
         "mm": [344, 194],
         "modes": [[1280, 720, 60000, True], [800, 600, 60317, False]]},
    ],
}


def state_path():
    p = os.environ.get("FAKE_XRANDR_STATE")
    if p:
        return p
    log = os.environ.get("FAKE_XRANDR_LOG")
    if log:
        return log + ".state.json"
    return os.path.join(os.environ.get("TMPDIR", "/tmp"),
                        "fake_xrandr_%d.json" % os.getuid())


def load():
    try:
        with open(state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT))


def save(st):
    with open(state_path(), "w") as f:
        json.dump(st, f)


def to_outputs(st):
    outs = []
    for d in st["outputs"]:
        o = core.OutputState(name=d["name"], active=d["active"],
                             x=d["x"], y=d["y"], scale=d["scale"],
                             transform=d["transform"], ident=d["ident"],
                             mm_w=d["mm"][0], mm_h=d["mm"][1])
        o.modes = [core.Mode(w=w, h=h, refresh_mhz=r, preferred=p)
                   for w, h, r, p in d["modes"]]
        if d["active"]:
            o.current = o.modes[d["current"]]
            o.w, o.h = core.logical_size(o.current.w, o.current.h,
                                         o.transform, o.scale)
        outs.append(o)
    return outs


def render(st, verbose):
    wx = core.State("fake", path=state_path() + ".wx")
    wx.primary = st.get("primary")
    lines = core.render_query(to_outputs(st), wx, verbose=verbose)
    lines.append(DISCONNECTED)
    return "\n".join(lines) + "\n"


def apply(st, argv):
    """Fold an xrandr command line into the simulated state (the subset
    warandr emits; anything else is an 'unrecognized option' like xrandr)."""
    stanzas = []
    cur = None
    i = 0

    def need():
        nonlocal i
        i += 1
        if i >= len(argv):
            sys.stderr.write("xrandr: %s requires an argument\n" % argv[i - 1])
            sys.exit(1)
        return argv[i]

    while i < len(argv):
        a = argv[i]
        if a == "--output":
            cur = {"name": need()}
            stanzas.append(cur)
        elif cur is None:
            sys.stderr.write("xrandr: unrecognized option '%s'\n" % a)
            sys.exit(1)
        elif a in ("--mode", "--rate", "--pos", "--rotate", "--reflect",
                   "--scale", "--same-as", "--left-of", "--right-of",
                   "--above", "--below"):
            cur[a[2:]] = need()
        elif a in ("--off", "--primary", "--auto", "--preferred"):
            cur[a[2:]] = True
        else:
            sys.stderr.write("xrandr: unrecognized option '%s'\n" % a)
            sys.exit(1)
        i += 1

    by_name = {d["name"]: d for d in st["outputs"]}
    for s in stanzas:
        d = by_name.get(s["name"])
        if d is None:
            if s["name"] != DISCONNECTED.split()[0]:
                sys.stderr.write("warning: output %s not found; ignoring\n"
                                 % s["name"])
            continue
        if s.get("off"):
            d["active"] = False
            if st.get("primary") == d["name"]:
                st["primary"] = None
            continue
        d["active"] = True
        if s.get("primary"):
            st["primary"] = d["name"]
        if "mode" in s:
            m = re.fullmatch(r"(\d+)x(\d+)", s["mode"])
            if not m:
                sys.stderr.write("xrandr: cannot find mode %s\n" % s["mode"])
                sys.exit(1)
            w, h = int(m.group(1)), int(m.group(2))
            cands = [k for k, md in enumerate(d["modes"])
                     if md[0] == w and md[1] == h]
            if not cands:
                sys.stderr.write("xrandr: cannot find mode %s\n" % s["mode"])
                sys.exit(1)
            pick = cands[0]
            for k in cands:
                if d["modes"][k][3]:
                    pick = k
                    break
            if "rate" in s:
                want = float(s["rate"])
                pick = min(cands, key=lambda k: abs(d["modes"][k][2] / 1000.0
                                                    - want))
            d["current"] = pick
        if "pos" in s:
            m = re.fullmatch(r"(-?\d+)x(-?\d+)", s["pos"])
            d["x"], d["y"] = int(m.group(1)), int(m.group(2))
        rot = s.get("rotate", core.RANDR_VIEW[d["transform"]][0])
        refl = s.get("reflect", core.RANDR_VIEW[d["transform"]][1])
        d["transform"] = core.sway_transform(rot, refl)
        if "scale" in s:
            d["scale"] = float(s["scale"].split("x")[0])
    for s in stanzas:  # relations resolve against the new geometry
        d = by_name.get(s["name"])
        if d is None or s.get("off"):
            continue
        for rel in ("same-as", "left-of", "right-of", "above", "below"):
            if rel in s and s[rel] in by_name:
                t = by_name[s[rel]]
                tw, th = size_of(t)
                dw, dh = size_of(d)
                if rel == "same-as":
                    d["x"], d["y"] = t["x"], t["y"]
                elif rel == "left-of":
                    d["x"], d["y"] = t["x"] - dw, t["y"]
                elif rel == "right-of":
                    d["x"], d["y"] = t["x"] + tw, t["y"]
                elif rel == "above":
                    d["x"], d["y"] = t["x"], t["y"] - dh
                else:
                    d["x"], d["y"] = t["x"], t["y"] + th
    # normalise like an X server would not, but wxrandr does: keep as given
    return st


def size_of(d):
    w, h, _r, _p = d["modes"][d["current"]]
    return core.logical_size(w, h, d["transform"], d["scale"])


def main(argv):
    if argv in ([], ["-q"], ["--query"], ["--current"], ["--verbose"],
                ["--current", "--verbose"], ["--verbose", "--current"]):
        sys.stdout.write(render(load(), "--verbose" in argv))
        return 0
    if argv in (["--version"], ["-v"]):
        sys.stdout.write("xrandr program version       1.5.4\n"
                         "Server reports RandR version 1.6\n")
        return 0
    log = os.environ.get("FAKE_XRANDR_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(argv) + "\n")
    fail = os.environ.get("FAKE_XRANDR_FAIL")
    if fail:
        sys.stderr.write(fail if fail.endswith("\n") else fail + "\n")
        return 1
    save(apply(load(), argv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
