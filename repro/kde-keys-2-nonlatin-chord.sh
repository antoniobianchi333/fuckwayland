#!/bin/sh
# The same measurement on a layout with no Latin letters on it at all (Greek),
# and the one thing that came out of it: `type` refuses a character the layout
# cannot make and says so, while `key` silently falls back to the built-in US
# table's *position* for the same character.  On a Greek-only session
# `wdotool key ctrl+s` therefore presses <AC02> and Kate receives Ctrl+sigma,
# which is not Save -- with no warning, although `keys explain ctrl+s` calls
# that same 's' unreachable.  Add `us` as a second layout (what a Greek user
# really configures) and the fallback is the right key after all.
#
#   vmctl user <vm> -- sh /tmp/kde-keys-2-nonlatin-chord.sh
KW=$(command -v kwriteconfig6 || command -v kwriteconfig5)
QD=$(command -v qdbus6 || command -v qdbus)
WD="sudo -n /usr/local/bin/wdotool"

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

reset_daemon() {
  sudo -n sh -c 'ps -eo pid,args | awk "/__da[e]mon/ {print \$1}" | xargs -r kill; rm -f /run/wdotool.sock /run/wdotool.sock.lock'
  sleep 2
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
# reading the buffer back must not depend on the chord under test, so the save
# goes through Kate's own action instead
dbus_save() {
  $QD "org.kde.kate-$(pgrep -x kate | head -1)" \
      /kate/MainWindow_1/actions/file_save_all \
      org.qtproject.Qt.QAction.trigger >/dev/null 2>&1
  sleep 2
}

for LIST in gr "gr,us"; do
  echo "== LayoutList=$LIST"
  set_layouts "$LIST"
  reset_daemon
  $WD __keymap --info 2>&1 | sed 's/^/  /'
  F=$HOME/kde-keys-gr.txt
  kate_on "$F"
  echo '  -- typing Greek, an AltGr character and two dead-key ones'
  $WD type -- 'τιμη 5 € και έξι ϊ' 2>&1 | sed 's/^/  stderr : /'
  sleep 1
  dbus_save
  printf '  arrived: %s\n' "$(cat "$F")"
  echo '  -- and the Latin the layout has not got'
  $WD type -- 'αβ ab ä γ' 2>&1 | sed 's/^/  stderr : /'
  echo '  -- the chord: what does `key ctrl+s` press, and does Kate save?'
  F2=$HOME/kde-keys-gr-chord.txt
  kate_on "$F2"
  $WD type -- 'δοκιμη' >/dev/null 2>&1
  sleep 1
  out=$($WD key ctrl+s 2>&1); printf '  stderr : %s\n' "${out:-(silent)}"
  sleep 2
  printf '  after ctrl+s: %s bytes\n' "$(wc -c < "$F2")"
  echo
done
