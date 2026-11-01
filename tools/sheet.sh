#!/bin/bash
# sheet.sh VIDEO START_FRAME COUNT OUT.png [COLS] — numbered contact sheet of frames.
set -e
video=$1 start=$2 count=$3 out=$4 cols=${5:-6}
tmp=$(mktemp -d)
ffmpeg -v error -i "$video" -vf "select='between(n\,$start\,$((start + count - 1)))',scale=320:-1" -fps_mode passthrough "$tmp/f%03d.png"
i=$start
for f in "$tmp"/f*.png; do
  magick "$f" -gravity NorthWest -fill white -undercolor '#000a' -pointsize 18 -annotate +4+4 "$i" "$f"
  i=$((i + 1))
done
magick montage "$tmp"/f*.png -tile ${cols}x -geometry +2+2 -background '#222' "$out"
rm -rf "$tmp"
