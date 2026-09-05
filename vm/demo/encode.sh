#!/bin/bash
# encode.sh -- ppm frames from record.sh into one README GIF.
#
#     vm/demo/encode.sh <framedir> <head> <fps> <width> <out.gif> [first] [last]
#
# Two pass palette (palettegen stats_mode=diff, then paletteuse with a bayer
# dither), then gifsicle -O3 --lossy to squeeze the result.  [first] and [last]
# trim frames off the ends, which is how the shell's own `exit` at the end of a
# take is kept out of the picture.  Needs ffmpeg, gifsicle and ImageMagick.
set -eu
DIR=${1:?usage: encode.sh <framedir> <head> <fps> <width> <out.gif> [first] [last]}
HEAD=$2; FPS=$3; W=$4; OUT=$5; FIRST=${6:-0}; LAST=${7:-99999}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

n=0
for f in $(ls "$DIR"/f*-"$HEAD".ppm | sort); do
    i=$(basename "$f" | sed "s/^f0*//; s/-.*//")
    i=${i:-0}
    [ "$i" -lt "$FIRST" ] && continue
    [ "$i" -gt "$LAST" ] && continue
    ln -s "$f" "$WORK/$(printf 'g%05d.ppm' "$n")"
    n=$((n + 1))
done
echo "encode.sh: $n frames -> $OUT"

ffmpeg -hide_banner -loglevel error -y -framerate "$FPS" -i "$WORK/g%05d.ppm" \
    -vf "scale=$W:-1:flags=lanczos,palettegen=stats_mode=diff" -update 1 "$WORK/pal.png"
ffmpeg -hide_banner -loglevel error -y -framerate "$FPS" -i "$WORK/g%05d.ppm" -i "$WORK/pal.png" \
    -lavfi "scale=$W:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
    -loop 0 "$OUT"
gifsicle -O3 --lossy=40 "$OUT" -o "$OUT.tmp" && mv "$OUT.tmp" "$OUT"

identify -format "%wx%h, %n stored frames, " "$OUT" | head -1
echo "$(stat -c %s "$OUT") bytes"
