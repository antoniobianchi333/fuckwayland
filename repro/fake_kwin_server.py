#!/usr/bin/env python3
"""Run the repo's wire-level KwinOutputServer as a standalone server, so the real
wxrandr CLI (a separate process, a real Session) can be pointed at it.

    kwin_server.py <tree> [one|two|three]
"""
import os
import sys
import time

ROOT = os.path.abspath(sys.argv[1])
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import test_wxrandr_kwin as T                                    # noqa: E402

kind = sys.argv[2] if len(sys.argv) > 2 else "two"
heads = {"one": [T.EDP()], "two": [T.EDP(), T.DP()],
         "three": [T.EDP(), T.DP(), T.HDMI(x=4480)]}[kind]
svc = T.KwinOutputServer(heads)
print(svc.path, flush=True)
while True:
    time.sleep(3600)
