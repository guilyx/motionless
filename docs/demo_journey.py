#!/usr/bin/env python3
"""Drive a running motionless daemon through a scripted journey, over UDP.

Used by ``docs/capture_demo.sh`` to record the demo. The phases exist to show
the three things that matter: the cue is absent when you are still, it tracks
real acceleration when you are moving, and one command turns it off and on.

Usage: python3 docs/demo_journey.py /path/to/motionless
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from motionless.motion import GRAVITY
from motionless.sources.demo import demo_acceleration

#: Seconds of wall clock the journey takes; matches the recording length.
TOTAL = 31.0
#: The engine is running between these times.
DRIVE = (4.0, 27.0)
#: `motionless toggle` fires at each of these.
TOGGLES = (15.0, 20.0)
#: Offset into the synthetic drive, so the clip opens mid-bend rather than
#: on the flat part of the waveform.
DRIVE_OFFSET = 5.0

ADDRESS = ("127.0.0.1", 5577)
RATE_HZ = 60.0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    motionless = sys.argv[1]

    def toggle() -> None:
        subprocess.run(
            [motionless, "toggle"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pending = list(TOGGLES)
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < TOTAL:
        if DRIVE[0] <= elapsed < DRIVE[1]:
            x, y, z = demo_acceleration(elapsed - DRIVE[0] + DRIVE_OFFSET)
        else:
            x, y, z = 0.0, 0.0, GRAVITY
        sock.sendto(f'{{"x":{x:.4f},"y":{y:.4f},"z":{z:.4f}}}'.encode(), ADDRESS)
        if pending and elapsed >= pending[0]:
            pending.pop(0)
            # In a thread: the CLI round trip would otherwise stall the stream
            # long enough for the daemon to treat the sensor as stale.
            threading.Thread(target=toggle, daemon=True).start()
        time.sleep(1.0 / RATE_HZ)
    print("journey complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
