#!/bin/sh
# Plasma 5.27 checks: bugs 7, 11, 12, plus the -l -G and -e claims.
export PATH=/usr/local/bin:$PATH
r() { printf '### %s\n' "$*"; "$@" 2>&1; printf 'rc=%s\n' "$?"; }
K=$(wdotool search --class konsole | head -1)
XT=$(wdotool search --class xterm | head -1)
XTX=$(wwmctl -lx | awk 'tolower($3) ~ /xterm/ {print $1; exit}')
echo "K=$K XT=$XT XTX=$XTX"
echo "=== 12 XWayland WM_CLASS case (kde-7) ==="
echo "  ours  getwindowclassname: [$(wdotool getwindowclassname $XT)]"
echo "  oracle xdotool          : [$(xdotool getwindowclassname $XTX 2>&1)]"
echo "  ours   wwmctl -lx row   : $(wwmctl -lx | grep -i xterm | head -1)"
echo "  oracle wmctrl -lx row   : $(wmctrl -lx | grep -i xterm | head -1)"
echo "=== 11 SHADED on a native window vs an X11 one (kde-9) ==="
r wdotool windowstate --add SHADED $K
r wdotool windowstate --add SHADED $XT
sleep 1; echo "  xprop on the xterm: $(xprop -id $XTX _NET_WM_STATE)"
wdotool windowstate --remove SHADED $XT >/dev/null 2>&1
echo "=== 7 a state KWin accepts and ignores, on an XWayland window (kde-1) ==="
r wwmctl -i -r $XTX -b add,fullscreen
sleep 1; echo "  xprop: $(xprop -id $XTX _NET_WM_STATE)"
r wwmctl -i -r $XTX -b remove,fullscreen
sleep 1; echo "  xprop: $(xprop -id $XTX _NET_WM_STATE)"
echo "  oracle for comparison:"
wmctrl -i -r $XTX -b add,fullscreen; sleep 1; echo "  xprop: $(xprop -id $XTX _NET_WM_STATE)"
wmctrl -i -r $XTX -b remove,fullscreen; sleep 1
echo "=== -l -G on 5.27: do we agree with wmctrl? ==="
echo "  ours  : $(wwmctl -lGx | grep -i xterm | head -1)"
echo "  oracle: $(wmctrl -lGx | grep -i xterm | head -1)"
echo "=== 3 script-id race on 5.27 ==="
sh /tmp/kde3_repro.sh 2>&1 | grep -E "failures|loop"
