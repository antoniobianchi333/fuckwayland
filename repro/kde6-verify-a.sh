#!/bin/sh
# Verification on resolute-kde (Plasma 6.6), run as `test` in the session.
export PATH=/usr/local/bin:$PATH
r() { printf '### %s\n' "$*"; "$@" 2>&1; printf 'rc=%s\n' "$?"; }
K=$(wdotool search --class konsole | head -1)
XT=$(wdotool search --class xterm | head -1)
echo "K=$K XT=$XT"

echo "=== 1 --layout reaches the daemon (sway-1, all backends) ==="
wdotool key --clearmodifiers a >/dev/null 2>&1
WDOTOOL_LAYOUT=xkb wdotool --layout us type "x" 2>&1 | head -2
echo "  (no 'built-in US layout' warning above = --layout us bypassed the keymap read)"
r sh -c 'wdotool --layout nonsense type hi'

echo "=== 3 script-id race (kde-1) ==="
sh /tmp/kde3_repro.sh 2>&1 | grep -E "failures|loop"

echo "=== 4 windowmove exit code (kde-2) ==="
r wdotool windowmove 99999999 5 5
r sh -c "wdotool windowmove 99999999 5 5 && echo MOVED || echo 'reported as failed'"

echo "=== 5 windowlower then windowraise clears keep-below (kde-3) ==="
wdotool windowactivate $XT >/dev/null 2>&1; sleep 1
wdotool windowlower $K 2>&1 | head -1
sleep 1; echo "  after lower : $(wxprop -id $(printf 0x%08x $K) _NET_WM_STATE 2>/dev/null || wwmctl -lx | grep -i konsole)"
wdotool windowraise $K 2>&1 | head -1
sleep 1
echo "  stacking now: $(wwmctl -l | awk '{print $NF}' | tr '\n' '|')"

echo "=== 6 getmouselocation from the compositor (kde-5) ==="
r wdotool mousemove 321 234
r wdotool getmouselocation
pkill -x -f 'wdotool.*daemon' 2>/dev/null
r wdotool getmouselocation

echo "=== 10 BELOW / SKIP_PAGER are readable ==="
wdotool windowstate --add BELOW $K 2>&1 | head -1; sleep 1
r sh -c "wxprop -id $(printf 0x%08x $K) _NET_WM_STATE"
wdotool windowstate --remove BELOW $K >/dev/null 2>&1

echo "=== 11 SHADED on a native window (kde-9) ==="
r wdotool windowstate --add SHADED $K

echo "=== 15 an unknown state name (kde-11) ==="
r wdotool windowstate --add MAXIMISED $K

echo "=== 13 argv[0] verbatim (kde-tools kde-3) ==="
/usr/local/bin/wwmctl -q 2>&1 | head -1
/usr/local/bin/wxprop -badflag 2>&1 | head -1

echo "=== 16 wwmctl -i with an id that names nothing (sway-5) ==="
r wwmctl -i -a 0x00ffffff

echo "=== 12 XWayland class case (kde-7) ==="
echo "  ours:   $(wdotool getwindowclassname $XT)"
echo "  oracle: $(xdotool getwindowclassname $(printf 0x%08x $(wwmctl -lx | awk '$3 ~ /XTerm/ {print $1; exit}')) 2>/dev/null)"
echo "  wwmctl -lx xterm row: $(wwmctl -lx | grep -i xterm | head -1)"
