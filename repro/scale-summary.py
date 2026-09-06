#!/usr/bin/env python3
"""One line per config: the worst error each oracle showed.

  summary.py <dir> [<dir> ...]

in-range   targets that land inside the layout the compositor reports
clamped    targets outside it (asked past the right/bottom edge on purpose)
d_comp     max |compositor pointer - asked| over the in-range targets
d_query    max |getmouselocation - asked|
d_daemon   max |daemon's model - asked|
d_hw       max |cursor plane - asked mapped through the compositor's own
           monitor origin and scale|, after the constant hotspot is removed
"""
import json
import os
import sys


def mon_of(mons, x, y):
    if not isinstance(mons, list):
        return None
    for m in mons:
        mx, my = int(m.get("x", 0)), int(m.get("y", 0))
        if mx <= x < mx + int(m.get("width", 0)) and my <= y < my + int(m.get("height", 0)):
            return m
    return None


def worst(vals):
    return max(vals) if vals else None


def main():
    print(f"{'config':<26}{'layout':>11} {'in':>3}{'clm':>4} "
          f"{'d_comp':>7}{'d_query':>8}{'d_daemon':>9}{'d_hw':>6}  note")
    for d in sys.argv[1:]:
        print(f"--- {d}")
        for n in sorted(os.listdir(d)):
            if not n.endswith(".json"):
                continue
            try:
                r = json.load(open(os.path.join(d, n)))
            except ValueError:
                print(f"{n[:-5]:<26} PROBE FAILED")
                continue
            mons = r.get("monitors")
            g = r.get("getdisplaygeometry", {})
            dc = dq = dd = []
            dc, dq, dd, dark = [], [], [], 0
            hwres, clamped = {}, 0
            for t in r.get("targets", []):
                a = t["asked"]
                m = mon_of(mons, *a)
                if m is None:
                    clamped += 1
                    continue
                for key, acc in (("comp", dc), ("query", dq), ("daemon", dd)):
                    v = t.get(key)
                    if isinstance(v, list):
                        acc.append(max(abs(v[0] - a[0]), abs(v[1] - a[1])))
                hw = t.get("hw") or {}
                if len(hw) != 1:
                    dark += 1
                    continue
                crtc, pos = next(iter(hw.items()))
                s = float(m.get("scale", 1))
                exp = ((a[0] - int(m.get("x", 0))) * s, (a[1] - int(m.get("y", 0))) * s)
                hwres.setdefault(crtc, []).append((pos[0] - exp[0], pos[1] - exp[1]))
            dh = []
            for crtc, res in hwres.items():
                cx = sorted(x for x, _ in res)[len(res) // 2]
                cy = sorted(y for _, y in res)[len(res) // 2]
                dh += [max(abs(x - cx), abs(y - cy)) for x, y in res]
            note = f"{dark} cursor-plane dark" if dark else ""
            print(f"{n[:-5]:<26}{str(g.get('W'))+'x'+str(g.get('H')):>11} "
                  f"{len(dc):>3}{clamped:>4} "
                  f"{str(worst(dc)):>7}{str(worst(dq)):>8}{str(worst(dd)):>9}"
                  f"{('%g' % worst(dh)) if dh else '-':>6}  {note}")


if __name__ == "__main__":
    main()
