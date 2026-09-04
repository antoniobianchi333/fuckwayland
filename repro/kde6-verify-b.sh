#!/bin/sh
export PATH=/usr/local/bin:$PATH
r() { printf '### %s\n' "$*"; "$@" 2>&1; printf 'rc=%s\n' "$?"; }
K=$(wdotool search --class konsole | head -1)
KH=$(printf 0x%08x $K)
echo "=== 5 raise clears keep-below (kde-3) ==="
wdotool windowactivate $(wdotool search --class xterm | head -1) >/dev/null 2>&1; sleep 1
wdotool windowlower $K 2>&1 | head -1; sleep 1
echo "  after lower: $(wxprop -id $KH _NET_WM_STATE)"
wdotool windowraise $K 2>&1 | head -1; sleep 1
echo "  after raise: $(wxprop -id $KH _NET_WM_STATE)"
echo "=== 13 argv[0] verbatim ==="
/usr/local/bin/wwmctl -q 2>&1 | head -1
/usr/local/bin/wxprop -badflag 2>&1 | head -1
cd /usr/local/bin && ./wwmctl -q 2>&1 | head -1 && ./wxprop -badflag 2>&1 | head -1
cd /
echo "=== 16 wwmctl -i with an unknown id ==="
r wwmctl -i -a 0x00ffffff
r wwmctl -a "no such window at all"
echo "=== 10 SKIP_PAGER readable ==="
wdotool windowstate --add SKIP_PAGER $K >/dev/null 2>&1; sleep 1
r sh -c "wxprop -id $KH _NET_WM_STATE"
wdotool windowstate --remove SKIP_PAGER $K >/dev/null 2>&1
echo "=== 14 root desktops on this compositor ==="
wxprop -root 2>/dev/null | grep -E "_NET_(CURRENT_DESKTOP|NUMBER_OF_DESKTOPS)"
echo "=== 8 selectwindow deadline exists ==="
WDOTOOL_SELECT_TIMEOUT=2 timeout 20 wdotool selectwindow 2>&1 | tail -2; echo "rc=$?"
