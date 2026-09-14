#!/usr/bin/env bash
#
# Regenerate docs/demo.gif (and a full-length MP4) from the real overlay.
#
# Like docs/capture_screenshot.sh, nothing here is a mockup: this runs the
# actual daemon on a virtual display under a compositing manager, drives it
# through a scripted journey over UDP, and screen-records the result.
#
# Requires: xvfb, xcompmgr, feh, ffmpeg, and a motionless that can import GTK.
#
#   ./docs/capture_demo.sh [path/to/motionless]
#
# Outputs: docs/demo.gif and, alongside it, motionless-demo.mp4 (not committed).

set -euo pipefail

DOCS="$(cd "$(dirname "$0")" && pwd)"
MOTIONLESS="${1:-motionless}"
DISPLAY_NUM="${MOTIONLESS_DISPLAY:-:97}"
GEOMETRY="1520x950"
SECONDS_TO_RECORD=31          # must match TOTAL in demo_journey.py
GIF_START=2                   # skip the settling second or two
GIF_LENGTH=11

for tool in Xvfb xcompmgr feh ffmpeg python3; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

WORK="$(mktemp -d)"
RUNTIME="$WORK/run"
mkdir -p "$RUNTIME"
chmod 700 "$RUNTIME"

cleanup() {
  "$MOTIONLESS" --config "$WORK/config.toml" stop >/dev/null 2>&1 || true
  if [ -n "${XVFB_PID:-}" ]; then
    kill "$XVFB_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

python3 "$DOCS/make_backdrop.py" "$WORK/backdrop.png"

# +extension Composite matters: without a compositor the overlay has nowhere
# to composite its alpha and records as a black rectangle.
Xvfb "$DISPLAY_NUM" -screen 0 "${GEOMETRY}x24" +extension Composite -nolisten tcp &
XVFB_PID=$!
export DISPLAY="$DISPLAY_NUM"
export XDG_SESSION_TYPE=x11
export XDG_RUNTIME_DIR="$RUNTIME"
sleep 2

xcompmgr -c &
sleep 1
feh --bg-fill "$WORK/backdrop.png"
sleep 1

# A throwaway config, so the clip shows the defaults rather than whatever the
# person running this happens to have configured.
"$MOTIONLESS" --config "$WORK/config.toml" config init >/dev/null
"$MOTIONLESS" --config "$WORK/config.toml" config set motion.source udp >/dev/null
"$MOTIONLESS" --config "$WORK/config.toml" start >/dev/null
sleep 2

ffmpeg -hide_banner -loglevel error -y \
  -f x11grab -framerate 30 -video_size "$GEOMETRY" -i "$DISPLAY_NUM" \
  -t "$SECONDS_TO_RECORD" -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p \
  "$WORK/raw.mp4" &
FFMPEG_PID=$!
sleep 0.3
python3 "$DOCS/demo_journey.py" "$MOTIONLESS"
wait "$FFMPEG_PID"

python3 "$DOCS/demo_captions.py" > "$WORK/filters.txt"
ffmpeg -hide_banner -loglevel error -y -i "$WORK/raw.mp4" \
  -filter_script:v "$WORK/filters.txt" \
  -c:v libx264 -preset slow -crf 21 -pix_fmt yuv420p -movflags +faststart \
  "$DOCS/motionless-demo.mp4"

# Two passes: a per-clip palette keeps the dots from banding into the backdrop.
ffmpeg -hide_banner -loglevel error -y -ss "$GIF_START" -t "$GIF_LENGTH" \
  -i "$DOCS/motionless-demo.mp4" \
  -vf "fps=12,scale=640:-1:flags=lanczos,palettegen=max_colors=64:stats_mode=diff" \
  "$WORK/palette.png"
ffmpeg -hide_banner -loglevel error -y -ss "$GIF_START" -t "$GIF_LENGTH" \
  -i "$DOCS/motionless-demo.mp4" -i "$WORK/palette.png" \
  -lavfi "fps=12,scale=640:-1:flags=lanczos[v];[v][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  -loop 0 "$DOCS/demo.gif"

echo "wrote $DOCS/demo.gif and $DOCS/motionless-demo.mp4"
