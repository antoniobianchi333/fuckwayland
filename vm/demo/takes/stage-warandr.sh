#!/bin/sh
# stage the warandr take: the two monitors side by side again, no leftover
# layout editor, no saved script from the last run, pointer in the canvas area.
pkill -x warandr
wxrandr --output Virtual-2 --right-of Virtual-1
rm -f "$HOME/.screenlayout/stacked.sh"
sleep 1
wdotool mousemove 760 470
