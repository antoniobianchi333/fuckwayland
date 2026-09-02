#!/bin/bash
# demo.sh — the wdotool README-demo beats, driven from inside the VM.
#
#   demo.sh setup   stage the desktop (clean windows, main terminal, parked cursor)
#   demo.sh run     perform the ~23s take (start record.sh first)
#
# Everything the viewer sees moving is wdotool: typing, focus, window motion,
# resize, fullscreen, the mouse sweep, and the windowclose at the end.
set -u
. /root/env.sh
D=45 # typing cadence, ms/char

type_line() { # type one shell line, then press Return
  wdotool type --delay $D -- "$1"
  sleep 0.15
  wdotool key Return
}

setup() {
  cat >/root/.demo_bashrc <<'RC'
# minimal demo shell
PS1='\[\e[1;32m\]root@wdotool-vm\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]# '
HISTFILE=/dev/null
RC
  swaymsg '[app_id=demo2] kill' >/dev/null 2>&1
  swaymsg '[app_id=foot] kill' >/dev/null 2>&1
  sleep 0.5
  swaymsg 'for_window [app_id=demo2] floating enable' >/dev/null
  swaymsg exec 'foot --title="wdotool demo" -o font=monospace:size=14 -o colors.background=1a1b26 -o colors.foreground=c0caf5 -- bash --rcfile /root/.demo_bashrc' >/dev/null
  wdotool search --sync --class foot >/dev/null
  sleep 0.6
  # park the cursor so it exists in every frame; move twice — a repeat of the
  # previous absolute position is deduped by the kernel and would leave the
  # visible cursor wherever the compositor last had it
  wdotool mousemove 640 400 sleep 0.05 mousemove 1150 620
  sleep 0.3
}

run() {
  # ---- beat 1: self-referential typing in the main terminal
  sleep 0.6
  wdotool type --delay $D -- '# this desktop is driven by wdotool'
  sleep 0.1
  wdotool key Return
  sleep 0.35
  type_line 'wdotool search --class foot getactivewindow getwindowname getwindowgeometry'
  sleep 1.3

  # ---- beat 2: a second window appears; wdotool snaps it into place and focuses it
  swaymsg exec 'foot --app-id=demo2 --title="window two" -w 540x330 -o font=monospace:size=12 -o colors.background=faf4ed -o colors.foreground=575279 -- bash --rcfile /root/.demo_bashrc' >/dev/null
  W2=$(wdotool search --sync --class demo2 | head -n1)
  sleep 0.35
  wdotool windowmove "$W2" 660 70 windowsize "$W2" 540 330
  sleep 0.45
  wdotool windowactivate "$W2"
  sleep 0.3
  type_line 'echo focused by wdotool'
  sleep 0.7

  # ---- beat 3: glide the floating window, grow it, flash fullscreen
  local glide=() x=660 y=70 i
  for i in $(seq 1 20); do
    x=$((x - 12)) y=$((y + 9))
    glide+=(windowmove "$W2" "$x" "$y" sleep 0.04)
  done
  wdotool "${glide[@]}"
  sleep 0.3
  wdotool windowsize "$W2" 568 344 sleep 0.05 windowsize "$W2" 596 358 \
    sleep 0.05 windowsize "$W2" 624 372 sleep 0.05 windowsize "$W2" 652 386 \
    sleep 0.05 windowsize "$W2" 680 400
  sleep 0.6
  wdotool windowstate --add FULLSCREEN "$W2"
  sleep 1.0
  wdotool windowstate --remove FULLSCREEN "$W2"
  sleep 0.6

  # ---- beat 4: cursor sweep ending in a click that refocuses the main terminal
  wdotool \
    mousemove 1120 540 sleep 0.05 mousemove 1060 440 sleep 0.05 \
    mousemove 980 340 sleep 0.05 mousemove 880 260 sleep 0.05 \
    mousemove 770 210 sleep 0.05 mousemove 650 190 sleep 0.05 \
    mousemove 540 200 sleep 0.05 mousemove 440 240 sleep 0.05 \
    mousemove 360 300 sleep 0.05 mousemove 300 370 sleep 0.05 \
    mousemove 260 440 sleep 0.05 mousemove 230 490 sleep 0.05 \
    mousemove 210 510 sleep 0.05 mousemove 200 520 sleep 0.1 \
    click 1
  sleep 0.5

  # ---- beat 5: close window two by typing the command that does it
  type_line 'wdotool search --class demo2 windowclose'
  sleep 0.9
  type_line 'echo done'
  sleep 1.4
}

case "${1:-run}" in
setup) setup ;;
run) run ;;
*)
  echo "usage: $0 setup|run" >&2
  exit 2
  ;;
esac
