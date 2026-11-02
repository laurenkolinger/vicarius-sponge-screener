"""Write a copy of one video with each sighting's ID drawn on its tracked position.

The server runs this program under the Python that has OpenCV. It reads one JSON request on
stdin: {"source": <file or URL>, "output": <file>, "marks": [{"label", "tier", "time", "point",
"points"}, ...]}. It prints "progress <fraction>" lines, and on failure one line on stderr and
exit code 1.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np

from mark_path import position_at

FALLBACK_TOOL_FOLDERS = ("/opt/homebrew/bin", "/usr/local/bin")
BYTES_PER_PIXEL = 3
PROGRESS_EVERY_FRAMES = 120
VIDEO_BITRATE = "10M"
RING_SHARE = 0.018
FONT = cv2.FONT_HERSHEY_SIMPLEX
# Blue, green, red: the colors of the page.
COLORS = {
    "original": (155, 220, 109),
    "added": (214, 43, 255),
    "other": (205, 205, 205),
    "review": (32, 176, 255),
}
INK = (16, 16, 16)
STDERR_TAIL = 400


def find_tool(name: str) -> str:
    """Find an ffmpeg program.

    Args:
        name: The program name.

    Returns:
        The program's path.

    Raises:
        RuntimeError: When the program is not installed.
    """
    found = shutil.which(name)
    if found:
        return found
    for folder in FALLBACK_TOOL_FOLDERS:
        candidate = Path(folder) / name
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"{name}: not installed")


def probe(ffprobe: str, source: str) -> Tuple[int, int, Fraction, float]:
    """Read a video's size, frame rate, and length.

    Args:
        ffprobe: The ffprobe program.
        source: The video.

    Returns:
        ``(width, height, frames per second, seconds)``.

    Raises:
        RuntimeError: When ffprobe cannot read the video.
    """
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate:format=duration", "-of", "json", source],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"source: ffprobe could not read {source}: {result.stderr[-STDERR_TAIL:]}")
    report = json.loads(result.stdout)
    stream = report["streams"][0]
    return stream["width"], stream["height"], Fraction(stream["r_frame_rate"]), float(report["format"]["duration"])


def choose_encoder(ffmpeg: str) -> List[str]:
    """Choose the fastest H.264 encoder this ffmpeg has.

    Args:
        ffmpeg: The ffmpeg program.

    Returns:
        The encoder arguments.
    """
    listing = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "h264_videotoolbox" in listing:
        return ["-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE]
    return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", VIDEO_BITRATE]


def draw_mark(frame: np.ndarray, position: Tuple[float, float], label: str, color: Tuple[int, int, int]) -> None:
    """Draw a ring on a sighting and its label beside it.

    Args:
        frame: The picture, changed in place.
        position: ``(x, y)`` as fractions of the frame.
        label: The text.
        color: The color.
    """
    height, width = frame.shape[:2]
    x, y = int(position[0] * width), int(position[1] * height)
    ring = max(8, int(height * RING_SHARE))
    scale = height / 1400
    thick = max(1, round(height / 540))
    cv2.circle(frame, (x, y), ring, INK, thick + 2, cv2.LINE_AA)
    cv2.circle(frame, (x, y), ring, color, thick, cv2.LINE_AA)
    (text_w, text_h), base = cv2.getTextSize(label, FONT, scale, thick)
    left = min(max(0, x + ring + 4), width - text_w - 8)
    top = min(max(0, y - text_h // 2 - 4), height - text_h - base - 8)
    cv2.rectangle(frame, (left, top), (left + text_w + 8, top + text_h + base + 8), color, -1)
    cv2.putText(frame, label, (left + 4, top + text_h + 4), FONT, scale, INK, thick, cv2.LINE_AA)


def render(request: Dict[str, Any]) -> None:
    """Write the marked copy of one video.

    Args:
        request: The JSON request.

    Raises:
        RuntimeError: When a step fails.
    """
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    source, output, marks = request["source"], request["output"], request["marks"]
    width, height, fps, seconds = probe(ffprobe, source)
    frame_bytes = width * height * BYTES_PER_PIXEL
    total = max(1, int(seconds * fps))
    with tempfile.TemporaryFile() as decode_log, tempfile.TemporaryFile() as encode_log:
        decoder = subprocess.Popen(
            [ffmpeg, "-v", "error", "-i", source, "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=decode_log,
        )
        encoder = subprocess.Popen(
            [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
             *choose_encoder(ffmpeg), "-pix_fmt", "yuv420p", "-an",
             "-movflags", "+faststart", output],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encode_log,
        )
        index = 0
        try:
            while True:
                raw = decoder.stdout.read(frame_bytes)
                if len(raw) < frame_bytes:
                    break
                time = float(index / fps)
                placed = [(mark, position_at(mark, time)) for mark in marks]
                placed = [(mark, spot) for mark, spot in placed if spot is not None]
                if placed:
                    frame = np.frombuffer(raw, np.uint8).reshape((height, width, BYTES_PER_PIXEL)).copy()
                    for mark, spot in placed:
                        draw_mark(frame, spot, mark["label"], COLORS.get(mark["tier"], COLORS["other"]))
                    raw = frame.tobytes()
                try:
                    encoder.stdin.write(raw)
                except BrokenPipeError:
                    # The encoder died; its own log, read below, says why.
                    break
                index += 1
                if index % PROGRESS_EVERY_FRAMES == 0:
                    print(f"progress {min(1.0, index / total):.4f}", flush=True)
        finally:
            try:
                encoder.stdin.close()
            except BrokenPipeError:
                pass
            decoder.stdout.close()
            decoder.wait()
            encoder.wait()
        if encoder.returncode != 0:
            encode_log.seek(0)
            raise RuntimeError(f"output: ffmpeg could not encode {output}: {encode_log.read().decode('utf-8', 'replace')[-STDERR_TAIL:]}")
        if index == 0 or decoder.returncode != 0:
            decode_log.seek(0)
            raise RuntimeError(f"source: ffmpeg could not decode {source}: {decode_log.read().decode('utf-8', 'replace')[-STDERR_TAIL:]}")


def main() -> int:
    """Run one request from stdin and return a process exit code."""
    try:
        render(json.load(sys.stdin))
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
