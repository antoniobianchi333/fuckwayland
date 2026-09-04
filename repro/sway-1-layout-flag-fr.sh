#!/bin/sh
# bug 1, observable: with a French keymap, --layout us must refuse a character
# only the French table has -- and name the US table while doing it.
export PATH=/usr/local/bin:$PATH
export WDOTOOL_FAKE_UINPUT=1 WDOTOOL_UINPUT_PATH=/tmp/fake-uinput
: > /tmp/fake-uinput
swaymsg input type:keyboard xkb_layout fr >/dev/null 2>&1
sleep 1
pkill -x wdotool 2>/dev/null; rm -f "$XDG_RUNTIME_DIR"/wdotool*.sock* 2>/dev/null; sleep 1
p() { printf '  %-24s -> ' "$1"; shift; out=$("$@" 2>&1); [ -n "$out" ] && echo "$out" || echo "(silent)"; }
p 'default (auto)'      wdotool type 'é'
p '--layout xkb'        wdotool --layout xkb type 'é'
p '--layout us'         wdotool --layout us type 'é'
p '--layout fixed'      wdotool --layout fixed type 'é'
swaymsg input type:keyboard xkb_layout us >/dev/null 2>&1
