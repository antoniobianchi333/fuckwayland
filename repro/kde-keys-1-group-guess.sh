#!/bin/sh
# KDE keyboard claim, measured in a real Kate window rather than at the socket:
# type a string, save it with a chord, read the file back.  Two configurations,
# both set the way a KDE user sets them (System Settings writes kxkbrc; the
# layout switcher is org.kde.KeyboardLayouts on the session bus).
#
#   1. one German layout  -> everything arrives, byte for byte
#   2. "us, de" switched to German with the switcher -> group 1 is assumed,
#      so y/z come out swapped, the umlauts are skipped and @ becomes "
#
# Run as the desktop user on `resolute-kde` (Plasma 6.6) or `noble-kde` (5.27):
#   vmctl user <vm> -- sh /tmp/kde-keys-1-group-guess.sh
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

set_layouts() {   # kxkbrc is what System Settings writes.  KWin re-reads it on
  # KConfig's change notification, so the write needs --notify -- and one
  # notification on its own is not enough: a write of the value already there
  # notifies nobody, and KWin was seen to drop a single change as well, so
  # bounce through another value first and let the second one land.  Plasma
  # 5.27's kwriteconfig5 has no --notify at all and neither kded5's keyboard
  # module nor org.kde.KWin.reconfigure makes KWin re-read the file: there,
  # restart the session after this.
  $KW --notify --file kxkbrc --group Layout --key Use true 2>/dev/null
  $KW --notify --file kxkbrc --group Layout --key LayoutList "us,gb" 2>/dev/null
  sleep 3
  $KW --notify --file kxkbrc --group Layout --key LayoutList "$1" 2>/dev/null
  sleep 4
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

echo '== one German layout'
set_layouts de
reset_daemon
$WD __keymap --info 2>&1 | sed 's/^/  /'
run "$HOME/kde-keys-de.txt" 'Grüße, ça va? — @ € 100% yz'

echo
echo '== "us, de", switched to German the way a KDE user switches'
set_layouts "us,de"
reset_daemon
$QD org.kde.keyboard /Layouts org.kde.KeyboardLayouts.setLayout 1 >/dev/null 2>&1 \
  || $QD org.kde.kglobalaccel /component/KDE_Keyboard_Layout_Switcher \
       org.kde.kglobalaccel.Component.invokeShortcut \
       "Switch to Next Keyboard Layout" >/dev/null 2>&1
sleep 2
$WD __keymap --info 2>&1 | sed 's/^/  /'
run "$HOME/kde-keys-usde.txt" 'Grüße yz @'
echo '  ... and with the group pinned, which is the documented way out:'
reset_daemon
kate_on "$HOME/kde-keys-usde-pinned.txt"
sudo -n WDOTOOL_XKB_GROUP=2 /usr/local/bin/wdotool type -- 'Grüße yz @' 2>&1 | sed 's/^/  stderr : /'
sleep 1
sudo -n WDOTOOL_XKB_GROUP=2 /usr/local/bin/wdotool key ctrl+s >/dev/null 2>&1
sleep 2
printf '  arrived: %s\n' "$(cat "$HOME/kde-keys-usde-pinned.txt")"
