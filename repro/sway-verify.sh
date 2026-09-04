#!/bin/sh
# sway checks: bugs 1 (--layout), 4 (tiled resize + exit codes), 9 (-d order)
export PATH=/usr/local/bin:$PATH
r() { printf '### %s\n' "$*"; "$@" 2>&1; printf 'rc=%s\n' "$?"; }
FT=$(wdotool search --class foot | head -1)
echo "FT=$FT"
echo "=== 9 wwmctl -d ordering (sway-2) ==="
swaymsg workspace 5 >/dev/null 2>&1; sleep 0.4
swaymsg workspace 2 >/dev/null 2>&1; sleep 0.4
swaymsg workspace 3 >/dev/null 2>&1; sleep 0.4
swaymsg workspace 1 >/dev/null 2>&1; sleep 0.6
echo "  sway's own order: $(swaymsg -t get_workspaces | grep -o '\"num\": *[0-9-]*' | tr '\n' ' ')"
echo "  ours:"; wwmctl -d | sed 's/^/    /'
echo "  oracle wmctrl -d:"; wmctrl -d 2>&1 | sed 's/^/    /'
echo "=== 4 tiled windowsize / windowmove (matrix-6) ==="
swaymsg "[con_id=$FT] floating disable" >/dev/null 2>&1; sleep 1
echo "  before: $(wwmctl -lGx | grep -i foot | head -1)"
r wdotool windowsize $FT 700 500
sleep 1; echo "  after : $(wwmctl -lGx | grep -i foot | head -1)"
r wdotool windowmove $FT 100 100
echo "  stale id:"; r sh -c "wdotool windowmove 99999999 5 5 && echo 'exited 0'"
echo "  floating, then the same resize:"
swaymsg "[con_id=$FT] floating enable" >/dev/null 2>&1; sleep 1
r wdotool windowsize $FT 700 500
sleep 1; echo "  after : $(wwmctl -lGx | grep -i foot | head -1)"
swaymsg "[con_id=$FT] floating disable" >/dev/null 2>&1
echo "=== 1 --layout reaches the daemon (sway-1) ==="
r sh -c 'WDOTOOL_LAYOUT=xkb wdotool --layout us type abc'
r sh -c 'wdotool --layout nonsense type hi'
