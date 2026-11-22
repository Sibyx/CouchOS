#!/usr/bin/env python3
"""Cut App Store app previews out of one iPhone screen recording.

    python3 make-previews.py RECORDING.mp4 OUT_DIR START:SECONDS:NAME [...]

Record first (1206 x 2622 on an iPhone 17 Pro):
    xcrun devicectl device capture screen-record --device <udid> \
        --destination /abs/.../marketing/raw/recording.mp4 --duration 120

Each START:SECONDS:NAME clip becomes OUT_DIR/NAME.mp4 in App Store Connect's
app preview format for 6.1"/6.3" iPhones: 886 x 1920 portrait, 30 fps, H.264
High, 15-30 s, with a silent stereo AAC track (uploads without an audio track
can be refused). Needs ffmpeg (Homebrew).
"""

import subprocess
import sys
from pathlib import Path

WIDTH, HEIGHT, FPS = 886, 1920, 30


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__.strip())
        return 2
    source, out = Path(argv[1]), Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for spec in argv[3:]:
        start, seconds, name = spec.split(":", 2)
        if not 15 <= float(seconds) <= 30:
            print(f"✗ {name}: {seconds} s; App Store previews must be 15-30 s", file=sys.stderr)
            return 1
        target = out / f"{name}.mp4"
        # Scale to the preview's width, then crop the few extra rows (the
        # phone is 0.460 wide-to-tall, the preview 0.461).
        video = f"scale={WIDTH}:-2:flags=lanczos,crop={WIDTH}:{HEIGHT},fps={FPS},format=yuv420p"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", start, "-t", seconds, "-i", str(source),
            "-f", "lavfi", "-t", seconds, "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", video,
            "-c:v", "libx264", "-profile:v", "high", "-level", "4.0", "-preset", "slow",
            "-b:v", "10M", "-maxrate", "12M", "-bufsize", "24M",
            "-c:a", "aac", "-b:a", "256k",
            "-shortest", "-movflags", "+faststart", str(target),
        ], check=True)
        print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
