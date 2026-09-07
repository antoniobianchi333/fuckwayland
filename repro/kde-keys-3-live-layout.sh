#!/bin/sh
# The KDE active-layout read, measured in a real Kate window rather than at the
# socket: configure two layouts, switch to the second the way a user does, type
# a string whose every character moves between them, save it with a chord and
# read the file back.
#
#   wanted 'yz@'  ->  arrived 'zy"'   when group 1 is assumed
#   wanted 'yz@'  ->  arrived 'yz@'   when KWin is asked (which it now is)
#
# The script does not know which wdotool is installed: run it once with each
# and compare.  `__keymap --info` names the active group and its source, so the
# two runs also differ in one printed line ("wayland" vs "wayland + kwin") and
# in whether the "assuming" notice appears on stderr at all.
#
# Run as the desktop user on `resolute-kde` (Plasma 6.6) or `noble-kde` (5.27):
#   vmctl user <vm> -- sh /tmp/kde-keys-3-live-layout.sh
# wdotool goes through sudo because KDE implements neither virtual-keyboard
# protocol, so injection needs /dev/uinput.
KW=$(command -v kwriteconfig6 || command -v kwriteconfig5)
QD=$(command -v qdbus6 || command -v qdbus)
WD="sudo -n /usr/local/bin/wdotool"

# a daemon keeps the environment it was started with, and an old one with
# WDOTOOL_XKB_GROUP set would answer for this one
reset_daemon() {
  sudo -n sh -c 'ps -eo pid,args | awk "/__da[e]mon/ {print \$1}" | xargs -r kill; rm -f /run/wdotool.sock /run/wdotool.sock.lock'
  sleep 2
}

set_layouts() {   # kxkbrc is what System Settings writes; see
  # kde-keys-1-group-guess.sh for why the value is bounced through another one
  # first, and why 5.27 needs a session restart instead.
  $KW --notify --file kxkbrc --group Layout --key Use true 2>/dev/null
  $KW --notify --file kxkbrc --group Layout --key LayoutList "us,gb" 2>/dev/null
  sleep 3
  $KW --notify --file kxkbrc --group Layout --key LayoutList "$1" 2>/dev/null
  sleep 4
}

switch_to_second() {   # the layout switcher, then the global shortcut
  $QD org.kde.KWin /Layouts org.kde.KeyboardLayouts.setLayout 1 >/dev/null 2>&1 \
    || $QD org.kde.keyboard /Layouts org.kde.KeyboardLayouts.setLayout 1 >/dev/null 2>&1 \
    || $QD org.kde.kglobalaccel /component/KDE_Keyboard_Layout_Switcher \
         org.kde.kglobalaccel.Component.invokeShortcut \
         "Switch to Next Keyboard Layout" >/dev/null 2>&1
  sleep 2
  printf '  KWin says the active layout is index %s (0-based)\n' \
    "$($QD org.kde.KWin /Layouts org.kde.KeyboardLayouts.getLayout 2>/dev/null)"
}

kate_on() {
  pkill -9 -x kate 2>/dev/null; sleep 2
  rm -f "$1"; : > "$1"
  nohup kate -n "$1" >/tmp/kate.log 2>&1 &
  i=0; W=""
  while [ $i -lt 40 ]; do W=$($WD search --name Kate 2>/dev/null | head -1); [ -n "$W" ] && break; i=$((i+1)); sleep 1; done
  sleep 2
  [ -n "$W" ] && $WD windowactivate "$W" >/dev/null 2>&1
  sleep 1
}

run() {           # $1 file, $2 string
  kate_on "$1"
  printf '  wanted : %s\n' "$2"
  $WD type -- "$2" 2>&1 | sed 's/^/  stderr : /'
  sleep 1
  $WD key ctrl+s 2>&1 | sed 's/^/  stderr : /'   # the chord: Ctrl+S saves
  sleep 2
  printf '  arrived: %s\n' "$(cat "$1")"
}

echo '== "us, de", switched to German the way a KDE user switches'
set_layouts "us,de"
switch_to_second
reset_daemon
$WD __keymap --info 2>&1 | sed 's/^/  /'
run "$HOME/kde-keys-live.txt" 'yz@'

echo
echo '== the same session with the group pinned by hand (the way out before KWin was asked)'
reset_daemon
kate_on "$HOME/kde-keys-live-pinned.txt"
sudo -n WDOTOOL_XKB_GROUP=2 /usr/local/bin/wdotool type -- 'yz@' 2>&1 | sed 's/^/  stderr : /'
sleep 1
sudo -n WDOTOOL_XKB_GROUP=2 /usr/local/bin/wdotool key ctrl+s >/dev/null 2>&1
sleep 2
printf '  arrived: %s\n' "$(cat "$HOME/kde-keys-live-pinned.txt")"

echo
echo '== switched back and forth under ONE running daemon'
reset_daemon      # the pinned daemon above would answer for this one too
set_layout() {
  $QD org.kde.KWin /Layouts org.kde.KeyboardLayouts.setLayout "$1" >/dev/null 2>&1 \
    || $QD org.kde.keyboard /Layouts org.kde.KeyboardLayouts.setLayout "$1" >/dev/null 2>&1
  sleep 2
  printf '  KWin says the active layout is index %s (0-based)\n' \
    "$($QD org.kde.KWin /Layouts org.kde.KeyboardLayouts.getLayout 2>/dev/null)"
}
set_layout 0
run "$HOME/kde-keys-live-us.txt" 'yz@'
set_layout 1
run "$HOME/kde-keys-live-de.txt" 'yz@'

echo
echo '== what wdotool thinks it is typing through'
$WD keys explain 'y' 2>&1 | sed 's/^/  /'
