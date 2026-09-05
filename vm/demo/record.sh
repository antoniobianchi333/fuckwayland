#!/bin/bash
# record.sh -- record one take of a README GIF against a running vmctl instance.
#
#     vm/demo/record.sh <take> [head] [interval_ms] [max_frames]
#
#   <take>          a take script, vm/demo/takes/<take>.tk
#   head            which head to capture (default 0)
#   interval_ms     capture period (default 83, so 12 fps)
#   max_frames      hard stop (default 600)
#
# Environment: VM (instance name, default `demo`), VMDATA (default ~/vm-data),
# CPS (typing speed in characters per second, default 22), OUTDIR (frame
# directory, default $VMDATA/frames/<take>).
#
# What it does: kills the last terminal, opens a new one running typer.py on the
# take but parked on /tmp/go, runs takes/stage-<take>.sh in the guest if there is
# one, starts qmprec.py, then releases the take.  When the take reports done the
# recorder is stopped and the frame count printed.  Frames are ppm; feed them to
# encode.sh.
set -u
TAKE=${1:?usage: record.sh <take> [head] [interval_ms] [max_frames]}
HEAD=${2:-0}; IVAL=${3:-83}; MAXF=${4:-600}
HERE=$(cd "$(dirname "$0")" && pwd)
VM=${VM:-demo}
VMDATA=${VMDATA:-$HOME/vm-data}
VMCTL=$HERE/../vmctl
OUT=${OUTDIR:-$VMDATA/frames/$TAKE}
rm -rf "$OUT"; mkdir -p "$OUT"

# the guest side of the take: typer.py plus whatever helper scripts it calls
"$VMCTL" scp "$VM" "$HERE/typer.py" "$VM:/home/test/typer.py" >/dev/null
"$VMCTL" scp "$VM" "$HERE/takes/$TAKE.tk" "$VM:/home/test/$TAKE.tk" >/dev/null
for h in "$HERE"/takes/wa-*.sh; do
    [ -e "$h" ] && "$VMCTL" scp "$VM" "$h" "$VM:/home/test/$(basename "$h")" >/dev/null
done

"$VMCTL" user "$VM" -- pkill -x ptyxis >/dev/null 2>&1
"$VMCTL" user "$VM" -- sudo -k >/dev/null 2>&1
"$VMCTL" user "$VM" -- rm -f /tmp/go /tmp/take-done >/dev/null 2>&1
sleep 1.5

# The terminal waits on /tmp/go so that the recorder starts on an already
# painted window instead of on the window opening animation.
"$VMCTL" user "$VM" -- sh -c "setsid ptyxis --new-window -x \"bash -c 'while [ ! -e /tmp/go ]; do sleep 0.15; done; TYPE_CPS=${CPS:-22} python3 /home/test/typer.py /home/test/$TAKE.tk; touch /tmp/take-done; sleep 2'\" >/dev/null 2>&1 </dev/null &"
sleep 5

if [ -f "$HERE/takes/stage-$TAKE.sh" ]; then
    "$VMCTL" scp "$VM" "$HERE/takes/stage-$TAKE.sh" "$VM:/tmp/stage.sh" >/dev/null
    "$VMCTL" user "$VM" -- sh /tmp/stage.sh
    sleep 2
fi

python3 "$HERE/qmprec.py" "$VMDATA/instances/$VM/qmp.sock" "$OUT" "$HEAD" "$IVAL" "$MAXF" \
    >"$OUT/.frames" 2>"$OUT/.err" &
REC=$!
sleep 0.4
"$VMCTL" user "$VM" -- touch /tmp/go

for _ in $(seq 1 400); do
    "$VMCTL" ssh "$VM" -- test -e /tmp/take-done >/dev/null 2>&1 && break
    sleep 1
done
sleep 0.3
touch "$OUT/.stop"
wait $REC
echo "$OUT: $(ls "$OUT"/*-"$HEAD".ppm 2>/dev/null | wc -l) frames"
