#!/bin/sh
# GNOME's "which layout is active?", measured in a real gnome-text-editor
# window rather than at the socket: type a string whose characters all move
# between the layouts, save it with a chord, read the file back with od.
#
# Each case is typed twice: once with the build that assumed the first group
# and once with the one that asks the desktop, so the before and the after sit
# side by side with their stderr.  Put the old build at /usr/local/bin/wdotool-
# before (`sh scripts/build-pyz.sh` on the older checkout, then scp it); with
# no such file the old half is `WDOTOOL_XKB_GROUP=1`, which types the same
# characters the old build did but does not print its notice.
#
#   1. one German source           0.4 typed this right and said so on every
#                                  command ("assuming 'German'"); now it knows
#                                  and says nothing
#   2. `us, de` switched to German 0.4 typed `zy"` for `yz@`; now `yz@`
#   3. five sources, the fifth     Mutter compiles the keymap in chunks of
#      one picked                  three, so the group is 2 and not 1; 0.4
#                                  assumed Russian and typed nothing at all
#
# Run as the desktop user on `resolute-gnome-iso` (GNOME 50) or
# `noble-gnome-iso` (GNOME 46):
#   vmctl user <vm> -- sh /tmp/gnome-keys-1-group-guess.sh
# wdotool goes through sudo because Mutter implements neither virtual-keyboard
# protocol, so injection needs /dev/uinput.  The panel coordinates below are
# for a 1280x800 head.
S=org.gnome.desktop.input-sources
WD="sudo -n /usr/local/bin/wdotool"
OLD=/usr/local/bin/wdotool-before

# a daemon keeps the environment it was started with, and an old one with
# WDOTOOL_XKB_GROUP set would answer for this one
reset_daemon() {
  sudo -n sh -c 'ps -eo pid,args | awk "/__da[e]mon/ {print \$1}" | xargs -r kill; rm -f /run/wdotool.sock /run/wdotool.sock.lock'
  sleep 2
}

set_sources() {   # Mutter recompiles the keymap when `sources` changes and
  # only then, so bounce through another value: a re-run of this script with
  # the list it already has would otherwise capture the old keymap.
  gsettings set $S per-window false
  gsettings set $S sources "[('xkb','us')]"
  sleep 2
  gsettings set $S sources "$1"
  gsettings reset $S mru-sources
  sleep 4
}

switch_to() {     # pick a layout the way a user does: the panel indicator's
  # menu, whose entries are 36px apart.  (Super+Space is the other user means
  # and moves through the most-recently-used order, so on its own it only ever
  # reaches the top two.)
  $WD mousemove 1151 15 >/dev/null 2>&1; sleep 1
  $WD click 1 >/dev/null 2>&1; sleep 3
  $WD mousemove 1110 $((62 + 36 * $1)) >/dev/null 2>&1; sleep 1
  $WD click 1 >/dev/null 2>&1; sleep 4
}

type_into() {     # $1 label, $2 string, $3 "old" or "new"
  pkill -9 -f gnome-text-editor 2>/dev/null; sleep 2
  F="$HOME/repro.txt"; rm -f "$F"; : > "$F"
  nohup gnome-text-editor "$F" >/tmp/gte.log 2>&1 &
  sleep 6
  if [ "$3" = old ] && [ -x "$OLD" ]; then
    sudo -n "$OLD" type -- "$2" 2>/tmp/err.txt
  elif [ "$3" = old ]; then
    sudo -n WDOTOOL_XKB_GROUP=1 /usr/local/bin/wdotool type -- "$2" 2>/tmp/err.txt
  else
    $WD type -- "$2" 2>/tmp/err.txt
  fi
  sleep 1
  $WD key ctrl+s >>/tmp/err.txt 2>&1
  sleep 3
  printf '    %-24s %s\n' "$1" "$(od -c "$F" | head -1 | sed 's/^0000000 *//')"
  sed 's/^/      stderr: /' /tmp/err.txt
}

run() {           # $1 heading, $2 string
  echo "  mru     : $(gsettings get $S mru-sources)"
  reset_daemon
  [ -x "$OLD" ] && echo "  explain-: $("$OLD" keys explain "$2" 2>&1 | head -1)"
  type_into "before (group assumed) :" "$2" old
  reset_daemon
  echo "  explain+: $(wdotool keys explain "$2" 2>&1 | head -1)"
  type_into "now (desktop asked)    :" "$2" new
}

echo '== 1. one German source'
set_sources "[('xkb','de')]"
run 'de' 'Grueße yz@'

echo '== 2. us, de -- switched to German with the panel menu'
set_sources "[('xkb','us'),('xkb','de')]"
switch_to 1
run 'us,de on German' 'yz@'

echo '== 3. five sources, the fifth picked (Mutter chunks the keymap)'
set_sources "[('xkb','de'),('xkb','fr'),('xkb','gr'),('xkb','ru'),('xkb','es')]"
switch_to 4
run 'es of five' 'yz'

echo '== and nothing appeared on screen: the portal read has no consent step'
gsettings reset $S sources
gsettings reset $S mru-sources
