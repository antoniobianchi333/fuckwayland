#!/bin/sh
# kde-1 repro: concurrent wdotool commands and KWin's reused script ids.
export PATH=/usr/local/bin:$PATH
K=$(wdotool search --class konsole | head -1)
echo "target konsole=$K"
run() {
  n=$1; shift
  rm -f /tmp/cc.*
  i=0
  while [ $i -lt $n ]; do
    ( "$@" >/tmp/cc.$i 2>&1; echo "rc=$?" >>/tmp/cc.$i ) </dev/null >/dev/null 2>&1 &
    i=$((i+1))
  done
  wait
  fail=$(grep -h '^rc=' /tmp/cc.* | grep -cv 'rc=0')
  echo "n=$n cmd='$*' failures=$fail/$n"
  grep -h -v '^rc=0$' /tmp/cc.* | grep -v '^[0-9]*$' | sort | uniq -c | sed 's/^/    /'
}
run 10 wdotool windowmove $K 200 200
run 10 wdotool getwindowname $K
run 6 wdotool windowstate --toggle ABOVE $K
echo "--- two tight loops, 30 rounds each"
rm -f /tmp/loop.*
loop() { i=0; f=0; while [ $i -lt 30 ]; do wdotool getwindowgeometry $K >/dev/null 2>>/tmp/loop.$1 || f=$((f+1)); i=$((i+1)); done; echo "loop$1 failures=$f"; }
( loop a > /tmp/la ) </dev/null >/dev/null 2>&1 &
( loop b > /tmp/lb ) </dev/null >/dev/null 2>&1 &
wait
cat /tmp/la /tmp/lb
sort /tmp/loop.* 2>/dev/null | uniq -c | head -5
