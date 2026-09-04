#!/bin/sh
export PATH=/usr/local/bin:$PATH
swaymsg() { /usr/bin/swaymsg "$@"; }
wxprop -root 2>/dev/null | grep -E "_NET_(CURRENT_DESKTOP|NUMBER_OF_DESKTOPS)"
