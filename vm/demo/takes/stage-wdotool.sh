#!/bin/sh
# stage the wdotool take: no leftover editor and no restored draft (it would
# open with a "Document Restored" banner), monitors side by side, pointer parked.
pkill -f gnome-text-editor
pkill -x warandr
pkill -x nautilus
sleep 1
rm -rf "$HOME/.local/share/org.gnome.TextEditor" "$HOME/.cache/org.gnome.TextEditor" "$HOME/.local/share/gnome-text-editor"
wxrandr --output Virtual-2 --right-of Virtual-1
sleep 1.5
wwmctl -l
wdotool mousemove 1120 560
