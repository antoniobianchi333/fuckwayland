#!/usr/bin/env python3
"""Which pixel space is each desktop using, and do we agree with it?

  spaces.py <dir> [<dir> ...]

Nothing here assumes an answer.  For every config, each target that lit a
cursor plane is scored against BOTH candidate maps from the layout
coordinate wdotool was handed to the device pixel the plane sits on:

  logical   dev = (asked - monitor.origin) * monitor.scale
  physical  dev = (asked - monitor.origin)

A map is the one in force when its residual is *constant* over the whole
config -- that constant is the cursor hotspot, which no map can know.  The
spread of the residual is the real error in device pixels; the winning
model's spread is what gets reported, and if neither model is constant,
both spreads are.

The raw size of each head comes from the same DRM dump (the primary plane's
crtc-pos), so "the compositor says 1920x1080 at scale 2" is checked against
the framebuffer actually being scanned out rather than against a guess.
"""
import json
import os
import sys


def raw_sizes(planes):
    """{crtc: (w, h)} from the biggest plane on each crtc -- the scanout."""
    got = {}
    for p in planes or []:
        pos, crtc = p.get("crtc_pos"), p.get("crtc")
        if not isinstance(pos, list) or not crtc or crtc == "(null)":
            continue
        w, h = pos[0], pos[1]
        if w > got.get(crtc, (0, 0))[0]:
            got[crtc] = (w, h)
    return got


def mon_of(mons, x, y):
    for m in mons or []:
        mx, my = int(m.get("x", 0)), int(m.get("y", 0))
        if mx <= x < mx + int(m.get("width", 0)) and my <= y < my + int(m.get("height", 0)):
            return m
    return None


def spread(vals):
    return 0.0 if not vals else max(vals) - min(vals)


def report(path):
    r = json.load(open(path))
    mons = r.get("monitors")
    g = r.get("getdisplaygeometry", {})
    raws = raw_sizes(r.get("planes_first"))
    name = os.path.basename(path)[:-5]

    # what the compositor claims, against the framebuffer being scanned out
    claims = []
    for i, m in enumerate(mons or []):
        raw = raws.get(f"crtc-{i}")
        s = float(m.get("scale", 1))
        w = int(m.get("width", 0))
        space = "?"
        if raw:
            if abs(w * s - raw[0]) < 2:
                space = "logical"
            elif abs(w - raw[0]) < 2:
                space = "physical" if s != 1 else "logical=physical"
        claims.append(f"{m.get('connector') or i}:{w}x{m.get('height')}+{m.get('x')}"
                      f"@{s:g} raw={raw[0] if raw else '?'} -> {space}")

    res = {"logical": {}, "physical": {}}
    per, seam = [], []
    for t in r.get("targets", []):
        a = t["asked"]
        hw = t.get("hw") or {}
        if len(hw) != 1:
            continue
        crtc, pos = next(iter(hw.items()))
        # score against the monitor whose head is actually drawing the sprite.
        # Mutter keeps the plane on the left head for the few pixels either
        # side of a seam (virtio-gpu cannot place a cursor plane at a negative
        # crtc-pos), so a target that lit the "wrong" head is the oracle going
        # blind, not a coordinate that went wrong: it is counted and excluded.
        try:
            m = (mons or [])[int(crtc.rsplit("-", 1)[1])]
        except (ValueError, IndexError, AttributeError):
            m = None
        if m is None:
            continue
        if mon_of([m], *a) is None:
            seam.append((a, crtc))
            continue
        ox, oy, s = int(m.get("x", 0)), int(m.get("y", 0)), float(m.get("scale", 1))
        for model, f in (("logical", s), ("physical", 1.0)):
            rx = pos[0] - (a[0] - ox) * f
            ry = pos[1] - (a[1] - oy) * f
            res[model].setdefault(crtc, {"x": [], "y": []})
            res[model][crtc]["x"].append(rx)
            res[model][crtc]["y"].append(ry)
        per.append((a, crtc, pos, m))

    verdict = {}
    for model, byc in res.items():
        sp = max([max(spread(v["x"]), spread(v["y"])) for v in byc.values()] or [None])
        verdict[model] = sp
    lo, ph = verdict["logical"], verdict["physical"]
    if lo is None:
        win, err = "no data", None
    elif lo <= ph:
        win, err = "logical", lo
    else:
        win, err = "physical", ph

    hot = {}
    for crtc, v in res[win].items() if win in res else []:
        hot[crtc] = (min(v["x"]), min(v["y"]))

    print(f"{name:<24} ours={g.get('W')}x{g.get('H')}  {'; '.join(claims)}")
    print(f"{'':<24} pointer space = {win.upper():<9} "
          f"max error {err:g} device px  (other model would be off by up to "
          f"{(ph if win=='logical' else lo):g})"
          if err is not None else f"{'':<24} pointer space = {win}")
    print(f"{'':<24} hotspot per crtc {hot}  targets scored {len(per)}"
          + (f"  seam/blind {len(seam)}: {[x[0] for x in seam]}" if seam else ""))
    return name, win, err


def main():
    for d in sys.argv[1:]:
        print(f"### {d}")
        for n in sorted(os.listdir(d)):
            if n.endswith(".json"):
                try:
                    report(os.path.join(d, n))
                except (ValueError, KeyError) as e:
                    print(f"{n[:-5]:<24} unreadable: {e}")


if __name__ == "__main__":
    main()
