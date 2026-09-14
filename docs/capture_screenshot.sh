#!/usr/bin/env bash
#
# Regenerate docs/screenshot.png from the real overlay.
#
# The dots in that image are drawn by motionless itself, not by a mockup: this
# runs the actual daemon on a virtual display, over a synthetic desktop
# backdrop, under a compositing manager (without one, X has nowhere to
# composite the overlay's alpha and it would come out black).
#
# Requires: xvfb, xcompmgr, feh, imagemagick, and a motionless that can import
# GTK — see CONTRIBUTING.md.
#
#   ./docs/capture_screenshot.sh [path/to/motionless]

set -euo pipefail

DOCS="$(cd "$(dirname "$0")" && pwd)"
MOTIONLESS="${1:-motionless}"
GEOMETRY="${MOTIONLESS_GEOMETRY:-1520x950}"

# Second pass: we are inside xvfb-run, on the virtual display.
if [ -n "${MOTIONLESS_CAPTURE_BACKDROP:-}" ]; then
  export XDG_SESSION_TYPE=x11
  xcompmgr -c &
  sleep 1
  feh --bg-fill "$MOTIONLESS_CAPTURE_BACKDROP"
  sleep 1
  # --always-on so the cue is up wherever the demo drive is in its loop.
  "$MOTIONLESS" start --source demo --always-on
  sleep 4
  "$MOTIONLESS" status
  import -window root "$DOCS/screenshot.png"
  "$MOTIONLESS" stop
  exit 0
fi

# First pass: check tooling, build the backdrop, then re-enter under Xvfb.
for tool in xvfb-run xcompmgr feh import python3; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

BACKDROP="$(mktemp -t motionless-backdrop-XXXXXX.png)"
RUNTIME="$(mktemp -d)"
chmod 700 "$RUNTIME"
trap 'rm -rf "$BACKDROP" "$RUNTIME"' EXIT

python3 "$DOCS/make_backdrop.py" "$BACKDROP"

MOTIONLESS_CAPTURE_BACKDROP="$BACKDROP" XDG_RUNTIME_DIR="$RUNTIME" \
  xvfb-run -a --server-args="-screen 0 ${GEOMETRY}x24 +extension Composite" \
  bash "$0" "$MOTIONLESS"

echo "wrote $DOCS/screenshot.png"
