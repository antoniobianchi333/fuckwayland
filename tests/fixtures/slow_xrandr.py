#!/usr/bin/env python3
"""fake_xrandr with a delay on *queries* (SLOW_QUERY seconds): a compositor
that answers slowly.  Applies stay instant, so a test can start one and know
it will land while it is doing something else."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fake_xrandr

QUERY = ([], ["-q"], ["--query"], ["--current"], ["--verbose"],
         ["--query", "--verbose"], ["--verbose", "--query"])

if __name__ == "__main__":
    argv = sys.argv[1:]
    _backend, rest = fake_xrandr.take_backend(list(argv))
    if rest in QUERY:
        time.sleep(float(os.environ.get("SLOW_QUERY", "0")))
    sys.exit(fake_xrandr.main(argv))
