#!/bin/sh
export PATH=/usr/local/bin:$PATH
r() { printf '### %s\n' "$*"; "$@" 2>&1; printf 'rc=%s\n' "$?"; }
K=$(wdotool search --class konsole | head -1); KH=$(printf 0x%08x $K)
echo "== 4 windowmove with a stale id"; r sh -c "wdotool windowmove 99999999 5 5 && echo 'exited 0 (looks like it moved)'"
echo "== 5 lower then raise"
wdotool windowactivate $(wdotool search --class xterm|head -1) >/dev/null 2>&1; sleep 1
wdotool windowlower $K >/dev/null 2>&1; sleep 1
echo "  after lower: $(wxprop -id $KH _NET_WM_STATE)"
wdotool windowraise $K >/dev/null 2>&1; sleep 1
echo "  after raise: $(wxprop -id $KH _NET_WM_STATE)"
echo "== 10 BELOW readable?"; wdotool windowstate --add BELOW $K >/dev/null 2>&1; sleep 1
echo "  $(wxprop -id $KH _NET_WM_STATE)"; wdotool windowstate --remove BELOW $K >/dev/null 2>&1
wdotool windowraise $K >/dev/null 2>&1
echo "== 6 getmouselocation as test (no /dev/uinput)"; r wdotool getmouselocation
echo "== 13 argv[0]"; /usr/local/bin/wwmctl -q 2>&1|head -1; /usr/local/bin/wxprop -badflag 2>&1|head -1
echo "== 15 unknown state"; r wdotool windowstate --add MAXIMISED $K
echo "== 16 unknown id"; r wwmctl -i -a 0x00ffffff
echo "== 8 selectwindow with a 2s budget (it has none)"; timeout 8 wdotool selectwindow 2>&1|tail -1; echo "timeout rc=$?"
