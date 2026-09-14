#!/usr/bin/env python3
"""Emit the ffmpeg filter chain that captions the demo recording.

A separate file because drawtext quoting is unforgiving: the filter string
uses single quotes as its own delimiter, so a caption containing an
apostrophe silently unbalances the whole chain and ffmpeg reports a confusing
"No such filter" from somewhere further along. Generating it here lets us
assert the constraint instead of discovering it in the output.

Usage: python3 docs/demo_captions.py > filters.txt
"""

from __future__ import annotations

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

#: Output size. The recording is larger; scaling first keeps the caption size
#: consistent regardless of the capture geometry.
WIDTH, HEIGHT = 1280, 800

#: (start, end, text, font, is_command). Times are in seconds and must line up
#: with the phases in demo_journey.py.
CAPTIONS = (
    (0.0, 4.0, "Stationary - no cues", FONT, False),
    (4.0, 15.0, "Driving - dots drift against the acceleration you feel", FONT, False),
    (15.0, 16.0, "$ motionless toggle", MONO, True),
    (16.0, 20.0, "Cues off - one command, bind it to a hotkey", FONT, False),
    (20.0, 21.0, "$ motionless toggle", MONO, True),
    (21.0, 27.0, "Cues back on - still driving", FONT, False),
    (27.0, 31.0, "Stopped - cues fade out on their own", FONT, False),
)

BOX = "box=1:boxcolor=0x0B0D12CC:boxborderw=16"
COMMAND_COLOUR = "0xFFD479"


def build() -> str:
    parts = [f"scale={WIDTH}:{HEIGHT}"]
    for start, end, text, font, is_command in CAPTIONS:
        # drawtext delimits its own arguments with ':' and quotes with "'".
        if "'" in text or ":" in text:
            raise ValueError(f"caption must not contain an apostrophe or colon: {text!r}")
        colour = COMMAND_COLOUR if is_command else "white"
        parts.append(
            f"drawtext=fontfile={font}:text='{text}':fontcolor={colour}"
            f":fontsize=25:x=44:y=h-86:{BOX}"
            f":enable='between(t,{start},{end})'"
        )
    parts.append(
        f"drawtext=fontfile={FONT}:text='motionless':fontcolor=0xB9C0CC"
        f":fontsize=19:x=w-tw-44:y=h-80"
        f":box=1:boxcolor=0x0B0D12AA:boxborderw=12"
    )
    return ",\n".join(parts)


if __name__ == "__main__":
    print(build())
