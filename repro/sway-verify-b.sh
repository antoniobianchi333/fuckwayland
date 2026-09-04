#!/bin/sh
export PATH=/usr/local/bin:$PATH
r() { printf '### %s\n' "$*"; "$@" 2>&1; printf 'rc=%s\n' "$?"; }
echo "=== 14 wxprop -root: current desktop vs the count (sway-3) ==="
swaymsg workspace 7 >/dev/null 2>&1; sleep 1
echo "  workspaces: $(swaymsg -t get_workspaces | grep -o '\"num\": *[0-9-]*' | tr '\n' ' ')"
wxprop -root 2>/dev/null | grep -E "_NET_(CURRENT_DESKTOP|NUMBER_OF_DESKTOPS)" | sed 's/^/  /'
echo "  wwmctl -d:"; wwmctl -d | sed 's/^/    /'
swaymsg workspace 1 >/dev/null 2>&1
echo "=== 16 wwmctl -i with an unknown id ==="
r wwmctl -i -a 0x00ffffff
echo "=== 13 argv[0] ==="
/usr/local/bin/wwmctl -q 2>&1 | head -1
/usr/local/bin/wxprop -badflag 2>&1 | head -1
echo "=== 15 unknown state name ==="
FT=$(wdotool search --class foot | head -1)
r wdotool windowstate --add MAXIMISED $FT
echo "=== tiled raise/lower (footnote c) ==="
r wdotool windowraise $FT
r wdotool windowlower $FT
