#!/bin/bash
# record.sh — steady-cadence grim frame recorder + gif assembly for the wdotool demo.
#
#   record.sh start    capture frames to /root/frames at INTERVAL_MS (default 83ms
#                      ~= 12fps) until /root/frames/.stop appears or MAX_FRAMES
#   record.sh encode   assemble /root/frames into /root/demo.gif (two-pass palette)
set -u
. /root/env.sh
FRAMES=/root/frames
STOP=$FRAMES/.stop
INTERVAL_MS=${INTERVAL_MS:-83}
MAX_FRAMES=${MAX_FRAMES:-400}

case "${1:-start}" in
start)
  rm -rf "$FRAMES"
  mkdir -p "$FRAMES"
  start_ns=$(date +%s%N)
  i=0
  while [ ! -e "$STOP" ] && [ "$i" -lt "$MAX_FRAMES" ]; do
    target=$((start_ns + i * INTERVAL_MS * 1000000))
    now=$(date +%s%N)
    if [ "$now" -lt "$target" ]; then
      d=$(((target - now) / 1000000))
      [ "$d" -gt 0 ] && sleep "$(printf '0.%03d' "$d")"
    fi
    grim -c "$FRAMES/$(printf 'f%04d' "$i").png"
    i=$((i + 1))
  done
  ;;
encode)
  cd "$FRAMES" || exit 1
  ffmpeg -hide_banner -loglevel warning -y -framerate 12 -i f%04d.png \
    -vf "scale=960:-1:flags=lanczos,palettegen=stats_mode=diff" -update 1 /root/palette.png
  ffmpeg -hide_banner -loglevel warning -y -framerate 12 -i f%04d.png -i /root/palette.png \
    -lavfi "scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
    -loop 0 /root/demo.gif
  ls -la /root/demo.gif
  ;;
*)
  echo "usage: $0 start|encode" >&2
  exit 2
  ;;
esac
