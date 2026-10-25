"""Follow one sighting through its video with OpenCV's CSRT tracker.

The server (screener/tracking.py) runs this program under the Python of
.venv-track, the only place OpenCV and numpy are installed. It reads one JSON
request on stdin:

    {"source": <file path or URL>, "time": <seconds>, "point": {"x", "y"},
     "box": null or {"x", "y", "w", "h"}, "back_seconds": 12,
     "forward_seconds": 15, "fps": 15, "scale_width": 640}

The last four fields are optional and default to the values shown. Points and
boxes are fractions of the frame, with 0, 0 at the top left. The program
decodes the stretch from back_seconds before the sighting to forward_seconds
after it with the ffmpeg command line, at fps frames per second and
scale_width pixels wide. It starts CSRT on the sighting's frame and follows the
sponge forward, then starts it again on the same frame and follows the sponge
backward over the frames in reverse. A point sighting tracks a square 12
percent of the frame height around the point; a box sighting tracks its box.
A frame is bad when the tracker fails on it, or when the tracked patch has
lost all likeness to the sighting's patch (a score under 0.05). A direction
ends when the point comes within 2 percent of an edge, or the tracker fails
on a box that already reaches past an edge ("exit"); after 3 bad frames in a
row anywhere else ("lost"); at its time limit ("limit"); or when the video has
no more frames that way ("frames").

It prints one JSON answer on stdout:

    {"points": [[t, x, y, score], ...], "box": null or {"w", "h"},
     "ends": {"back": <end>, "forward": <end>}}

The points of both directions come in time order, and the sighting's own
point is among them with score 1. A score says how much the tracked patch
still looks like the sighting's patch: their normalized cross-correlation,
clipped to 0 to 1. A bad frame adds no point.

A bad request or a failed step prints one line to stderr that starts with the
field name (``time: ...``) and exits with code 1. With ``--debug-dir`` the
program also writes every fifth tracked frame as a JPEG with the box drawn.
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import cv2
import numpy as np

# What a request may ask for, and the defaults of its optional fields.
REQUEST_FIELDS = ("source", "time", "point", "box", "back_seconds", "forward_seconds", "fps", "scale_width")
DEFAULT_BACK_SECONDS = 12.0
DEFAULT_FORWARD_SECONDS = 15.0
DEFAULT_FPS = 15.0
DEFAULT_SCALE_WIDTH = 640
MAX_TIME_SECONDS = 86400.0
MAX_WINDOW_SECONDS = 60.0
MAX_FPS = 60.0
MIN_SCALE_WIDTH = 64
MAX_SCALE_WIDTH = 3840
# A box may reach past an edge by this much, the rounding of the page's numbers.
EDGE_SLACK = 1e-9

# How a sighting is followed.
POINT_BOX_FRACTION = 0.12
MIN_BOX_PIXELS = 16
EDGE_MARGIN = 0.02
LOST_AFTER_BAD_FRAMES = 3
# CSRT calls a frame failed when the peak of its correlation response falls
# below this value. OpenCV's default (0.035) never fails, not even on a flat
# gray picture or on unrelated noise; at 0.1 both fail at once while a
# textured target that moves 10 pixels a frame is still followed. On five
# points of a real 2024 transect (BIT T1), 0.1 and the default gave the same
# tracks, because the likeness floor below catches what the default lets
# through; 0.2 ended two of the five tracks on their first frame.
CSRT_PEAK_THRESHOLD = 0.1
SIGHTING_SCORE = 1.0
# A tracked patch whose likeness to the sighting's patch falls under this is a
# bad frame. On the real transect the tracks that stayed on their sponge never
# fell under 0.11, while a track sliding across bare sand sat at 0 for seconds;
# 0.15 already cut a correct track short.
SCORE_FLOOR = 0.05

# The four ways a direction ends.
END_EXIT = "exit"
END_LOST = "lost"
END_LIMIT = "limit"
END_FRAMES = "frames"

# Decoding.
BYTES_PER_PIXEL = 3
FALLBACK_TOOL_FOLDERS = ("/opt/homebrew/bin", "/usr/local/bin")
PROBE_TIMEOUT_SECONDS = 120.0
STOP_GRACE_SECONDS = 5.0
STDERR_TAIL_CHARACTERS = 400
# ffmpeg exits with code 0 when its input stops partway, and these lines give that away.
READ_FAILURE_MARKERS = ("Error during demuxing", "Stream ends prematurely")
# Frame times are fractions of a second; this keeps a time that is a whole
# number of frames from rounding down to the frame before.
FRAME_COUNT_EPSILON = 1e-9

# The answer's precision: a microsecond, a millionth of the frame, and a
# ten-thousandth of the score.
TIME_DECIMALS = 6
FRACTION_DECIMALS = 6
SCORE_DECIMALS = 4

# Debug frames.
DEBUG_EVERY = 5
DEBUG_JPEG_QUALITY = 85
DEBUG_OK_COLOR = (80, 220, 80)
DEBUG_WEAK_COLOR = (40, 160, 255)
DEBUG_FAILED_COLOR = (60, 60, 230)
STATE_OK = "ok"
STATE_WEAK = "weak"
STATE_FAILED = "failed"
DEBUG_COLORS = {STATE_OK: DEBUG_OK_COLOR, STATE_WEAK: DEBUG_WEAK_COLOR, STATE_FAILED: DEBUG_FAILED_COLOR}
DEBUG_TEXT_COLOR = (255, 255, 255)
DEBUG_LINE_WIDTH = 2
DEBUG_DOT_RADIUS = 3
DEBUG_FONT_SCALE = 0.5
DEBUG_TEXT_ORIGIN = (8, 20)


class WorkerError(Exception):
    """A bad request or a failed step, with a message that starts with the field name."""


@dataclass(frozen=True)
class Request:
    """A checked request.

    Attributes:
        source: The file path or URL ffmpeg reads.
        time: The sighting's time in seconds.
        point: The sighting's point as (x, y) fractions.
        box: The sighting's box as (x, y, w, h) fractions, or None for a point.
        back_seconds: How far back to follow the sponge.
        forward_seconds: How far forward to follow the sponge.
        fps: Frames per second to decode and track.
        scale_width: The width in pixels to decode at.
    """

    source: str
    time: float
    point: Tuple[float, float]
    box: Optional[Tuple[float, float, float, float]]
    back_seconds: float
    forward_seconds: float
    fps: float
    scale_width: int


@dataclass(frozen=True)
class Plan:
    """The stretch of video to decode, in frames of the decoded stream.

    Attributes:
        width: The decoded frame width in pixels.
        height: The decoded frame height in pixels.
        back_frames: Frames before the sighting's frame.
        forward_frames: Frames after the sighting's frame.
        back_end: How the backward direction ends when it uses every frame
            before the sighting: "limit", or "frames" when the video starts
            before back_seconds runs out.
    """

    width: int
    height: int
    back_frames: int
    forward_frames: int
    back_end: str


@dataclass(frozen=True)
class Target:
    """What CSRT follows, in pixels of the decoded frames.

    Attributes:
        box: The starting box (x, y, w, h), whole pixels, inside the frame.
        offset: The sighting's point minus the box center, which stays
            nonzero only when the box had to move to fit inside the frame.
        template: The sighting's patch in gray, for the scores.
    """

    box: Tuple[int, int, int, int]
    offset: Tuple[float, float]
    template: np.ndarray


# ----------------------------------------------------------------- the request


def _number(value: Any, field: str, low: float, high: float) -> float:
    """Check one number of the request.

    Args:
        value: The JSON value.
        field: Its name, used to open the error message.
        low: The smallest allowed value.
        high: The largest allowed value.

    Returns:
        The value as a float.

    Raises:
        WorkerError: When the value is not a number (a bool is not), not
            finite, or outside low to high.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerError(f"{field}: expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkerError(f"{field}: expected a finite number, got {value!r}")
    # Compared before the float conversion, so a huge whole number cannot overflow it.
    if not low <= value <= high:
        raise WorkerError(f"{field}: expected a number from {low:g} to {high:g}, got {value!r}")
    return float(value)


def _object(value: Any, field: str, names: Tuple[str, ...]) -> Dict[str, Any]:
    """Check that a request field is a JSON object holding the given names.

    Args:
        value: The JSON value.
        field: Its name, used to open the error message.
        names: The names it must hold.

    Returns:
        The object.

    Raises:
        WorkerError: Starting with the field name when the value is not an
            object, or with ``<field>.<name>`` when a name is missing.
    """
    if not isinstance(value, dict):
        raise WorkerError(f"{field}: expected an object with {', '.join(names)}, got {type(value).__name__}")
    for name in names:
        if name not in value:
            raise WorkerError(f"{field}.{name}: missing")
    return value


def parse_request(text: str) -> Request:
    """Read and check the JSON request.

    Args:
        text: Everything the caller sent on stdin.

    Returns:
        The checked request, with the defaults filled in.

    Raises:
        WorkerError: Starting with the name of the first bad field, or with
            ``request`` when the text is not a JSON object or holds a field
            this program does not know.
    """
    try:
        data = json.loads(text)
    except ValueError as error:
        raise WorkerError(f"request: not valid JSON ({error})") from error
    if not isinstance(data, dict):
        raise WorkerError(f"request: expected a JSON object, got {type(data).__name__}")
    unknown = sorted(name for name in data if name not in REQUEST_FIELDS)
    if unknown:
        raise WorkerError(f"request: unknown field {unknown[0]!r}; expected {', '.join(REQUEST_FIELDS)}")
    for name in ("source", "time", "point", "box"):
        if name not in data:
            raise WorkerError(f"{name}: missing")
    source = data["source"]
    if not isinstance(source, str) or not source:
        raise WorkerError(f"source: expected a file path or URL, got {source!r}")
    time = _number(data["time"], "time", 0.0, MAX_TIME_SECONDS)
    point = _object(data["point"], "point", ("x", "y"))
    xy = (_number(point["x"], "point.x", 0.0, 1.0), _number(point["y"], "point.y", 0.0, 1.0))
    box = None
    if data["box"] is not None:
        fields = _object(data["box"], "box", ("x", "y", "w", "h"))
        left = _number(fields["x"], "box.x", 0.0, 1.0)
        top = _number(fields["y"], "box.y", 0.0, 1.0)
        width = _number(fields["w"], "box.w", 0.0, 1.0)
        height = _number(fields["h"], "box.h", 0.0, 1.0)
        if width == 0 or height == 0:
            raise WorkerError(f"box.{'w' if width == 0 else 'h'}: expected a size above 0")
        if left + width > 1 + EDGE_SLACK or top + height > 1 + EDGE_SLACK:
            raise WorkerError(f"box: reaches past the edge of the frame (x + w = {left + width:g}, y + h = {top + height:g})")
        box = (left, top, width, height)
    fps = _number(data.get("fps", DEFAULT_FPS), "fps", 0.0, MAX_FPS)
    if fps == 0:
        raise WorkerError("fps: expected a number above 0")
    scale_width = data.get("scale_width", DEFAULT_SCALE_WIDTH)
    if isinstance(scale_width, bool) or not isinstance(scale_width, int) or not MIN_SCALE_WIDTH <= scale_width <= MAX_SCALE_WIDTH:
        raise WorkerError(f"scale_width: expected a whole number from {MIN_SCALE_WIDTH} to {MAX_SCALE_WIDTH}, got {scale_width!r}")
    return Request(
        source=source,
        time=time,
        point=xy,
        box=box,
        back_seconds=_number(data.get("back_seconds", DEFAULT_BACK_SECONDS), "back_seconds", 0.0, MAX_WINDOW_SECONDS),
        forward_seconds=_number(data.get("forward_seconds", DEFAULT_FORWARD_SECONDS), "forward_seconds", 0.0, MAX_WINDOW_SECONDS),
        fps=fps,
        scale_width=scale_width,
    )


# ------------------------------------------------------------------- decoding


def find_tool(name: str) -> str:
    """Find ffmpeg or ffprobe when the server's PATH lacks the Homebrew folder.

    Args:
        name: The program name.

    Returns:
        The full path from PATH, else the path inside the first fallback
        folder that holds it, else the name unchanged (starting it then fails
        with a message that names it). screener.convert searches the same folders.
    """
    found = shutil.which(name)
    if found:
        return found
    for folder in FALLBACK_TOOL_FOLDERS:
        candidate = Path(folder) / name
        if candidate.is_file():
            return str(candidate)
    return name


def _tail(text: str) -> str:
    """Return the last 400 characters of a program's error text, or a note when it wrote none."""
    stripped = text.strip()
    return stripped[-STDERR_TAIL_CHARACTERS:] if stripped else "the program wrote no error text"


def probe_size(source: str) -> Tuple[int, int]:
    """Read the frame size of the first video stream.

    Args:
        source: The file path or URL.

    Returns:
        The width and height in pixels.

    Raises:
        WorkerError: Starting with ``source:`` when ffprobe cannot start,
            times out, cannot read the source, or finds no video stream.
    """
    args = [
        find_tool("ffprobe"), "-v", "error", "-select_streams", "V:0",
        "-show_entries", "stream=width,height", "-of", "json", "-i", source,
    ]
    try:
        completed = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS)
    except OSError as error:
        raise WorkerError(f"source: could not start {args[0]}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise WorkerError(f"source: ffprobe gave no answer about {source} within {PROBE_TIMEOUT_SECONDS:g} seconds") from error
    if completed.returncode != 0:
        raise WorkerError(f"source: ffprobe could not read {source} (exit code {completed.returncode}): {_tail(completed.stderr)}")
    try:
        streams = json.loads(completed.stdout).get("streams") or []
    except (ValueError, AttributeError) as error:
        raise WorkerError(f"source: ffprobe gave an unreadable report for {source}") from error
    sizes = [(stream.get("width"), stream.get("height")) for stream in streams]
    if not sizes or not all(isinstance(value, int) and value > 0 for value in sizes[0]):
        raise WorkerError(f"source: {source} has no video stream with a frame size")
    return sizes[0]


def make_plan(request: Request, source_size: Tuple[int, int]) -> Plan:
    """Work out the decoded frame size and how many frames lie on each side of the sighting.

    Args:
        request: The checked request.
        source_size: The source's width and height in pixels.

    Returns:
        The plan. The width is scale_width (never wider than the source) and
        both sides are even, which the pixel formats of ffmpeg need.
    """
    source_width, source_height = source_size
    width = max(2, min(request.scale_width, source_width) // 2 * 2)
    height = max(2, round(width * source_height / source_width / 2) * 2)
    seconds_back = min(request.back_seconds, request.time)
    return Plan(
        width=width,
        height=height,
        back_frames=int(math.floor(seconds_back * request.fps + FRAME_COUNT_EPSILON)),
        forward_frames=int(math.floor(request.forward_seconds * request.fps + FRAME_COUNT_EPSILON)),
        back_end=END_LIMIT if request.back_seconds <= request.time else END_FRAMES,
    )


def decode_args(request: Request, plan: Plan) -> List[str]:
    """Build the ffmpeg command that writes the planned frames as raw BGR to stdout.

    Input seeking (``-ss`` before ``-i``) decodes from the keyframe before the
    start and drops what comes before it, so the first frame out is the frame
    at the start and frame k sits k / fps seconds later.

    Args:
        request: The checked request.
        plan: The plan from make_plan.

    Returns:
        The argument list, starting with the program. The source is one
        argument after ``-i``, so it can never act as an option.
    """
    start = max(0.0, request.time - plan.back_frames / request.fps)
    total = plan.back_frames + 1 + plan.forward_frames
    return [
        find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{start:.6f}", "-i", request.source, "-map", "0:V:0",
        "-vf", f"fps={request.fps:g},scale={plan.width}:{plan.height}:flags=area",
        "-pix_fmt", "bgr24", "-frames:v", str(total), "-f", "rawvideo", "pipe:1",
    ]


class Decoder:
    """An ffmpeg child that writes the planned frames to a pipe, read one frame at a time.

    stderr goes to a temporary file, so a noisy ffmpeg can never fill a pipe
    and stall. Use it as a context manager; leaving the block stops ffmpeg.
    """

    def __init__(self, args: List[str], width: int, height: int) -> None:
        """Start ffmpeg.

        Args:
            args: The command from decode_args.
            width: The decoded frame width in pixels.
            height: The decoded frame height in pixels.

        Raises:
            WorkerError: Starting with ``source:`` when ffmpeg cannot start.
        """
        self._shape = (height, width, BYTES_PER_PIXEL)
        self._frame_bytes = width * height * BYTES_PER_PIXEL
        self._log: IO[bytes] = tempfile.TemporaryFile()
        self.stopped_early = False
        try:
            self._process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self._log)
        except OSError as error:
            self._log.close()
            raise WorkerError(f"source: could not start {args[0]}: {error}") from error

    def __enter__(self) -> "Decoder":
        """Return the decoder for the with block."""
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Stop ffmpeg when it still runs, and close its pipe and its log."""
        self.stop()
        self._process.stdout.close()
        self._log.close()

    def read(self) -> Optional[np.ndarray]:
        """Read the next frame.

        Returns:
            The frame as a height x width x 3 BGR array, or None when ffmpeg
            has no more frames.
        """
        data = self._process.stdout.read(self._frame_bytes)
        if len(data) < self._frame_bytes:
            return None
        return np.frombuffer(data, dtype=np.uint8).reshape(self._shape)

    def stop(self) -> None:
        """Stop ffmpeg before its end, politely first and by force after a grace period.

        The pipe closes first: ffmpeg usually sits blocked writing a frame
        nobody reads, and only a failed write lets it act on the signal.
        """
        if self._process.poll() is not None:
            return
        self.stopped_early = True
        self._process.stdout.close()
        self._process.terminate()
        try:
            self._process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()

    def check(self, source: str) -> None:
        """Make sure ffmpeg read what it was asked to, once it exited on its own.

        Args:
            source: The source, named in the error message.

        Raises:
            WorkerError: Starting with ``source:`` when ffmpeg failed or its
                input stopped partway. Nothing is checked after stop().
        """
        code = self._process.wait()
        if self.stopped_early:
            return
        self._log.seek(0)
        text = self._log.read().decode("utf-8", "replace")
        if code != 0:
            raise WorkerError(f"source: ffmpeg could not decode {source} (exit code {code}): {_tail(text)}")
        if any(marker in text for marker in READ_FAILURE_MARKERS):
            raise WorkerError(f"source: ffmpeg could not read the whole stretch of {source}: {_tail(text)}")


# ------------------------------------------------------------------- tracking


def _gray(frame: np.ndarray) -> np.ndarray:
    """Return a BGR frame in gray."""
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _patch(gray: np.ndarray, box: Tuple[float, float, float, float], size: Tuple[int, int]) -> np.ndarray:
    """Cut a box out of a gray frame and scale it to a size.

    Args:
        gray: The gray frame.
        box: (x, y, w, h) in pixels; parts outside the frame repeat the edge pixels.
        size: The wanted (width, height).

    Returns:
        The patch as float32.
    """
    x, y, w, h = box
    cut = cv2.getRectSubPix(gray, (max(1, int(round(w))), max(1, int(round(h)))), (x + w / 2, y + h / 2))
    return cv2.resize(cut, size, interpolation=cv2.INTER_AREA).astype(np.float32)


def similarity(template: np.ndarray, patch: np.ndarray) -> float:
    """Score how much a patch looks like the sighting's patch.

    Args:
        template: The sighting's patch.
        patch: A patch of the same size.

    Returns:
        Their zero-mean normalized cross-correlation clipped to 0 to 1, and 0
        when either patch is flat, because a flat patch carries no likeness.
    """
    first = template - template.mean()
    second = patch - patch.mean()
    scale = math.sqrt(float((first * first).sum()) * float((second * second).sum()))
    if scale == 0:
        return 0.0
    return min(1.0, max(0.0, float((first * second).sum()) / scale))


def make_target(request: Request, plan: Plan, anchor: np.ndarray) -> Target:
    """Place the box CSRT starts from on the sighting's frame.

    Args:
        request: The checked request.
        plan: The plan, for the frame size.
        anchor: The sighting's frame.

    Returns:
        The target. A point sighting gets a square 12 percent of the frame
        height around the point; a box sighting keeps its box. Either is at
        least MIN_BOX_PIXELS on a side and moves inside the frame when it
        reaches past an edge.
    """
    point_x, point_y = request.point[0] * plan.width, request.point[1] * plan.height
    if request.box is None:
        side = POINT_BOX_FRACTION * plan.height
        center_x, center_y, box_w, box_h = point_x, point_y, side, side
    else:
        left, top, width, height = request.box
        box_w, box_h = width * plan.width, height * plan.height
        center_x, center_y = left * plan.width + box_w / 2, top * plan.height + box_h / 2
    box_w = int(round(min(plan.width, max(MIN_BOX_PIXELS, box_w))))
    box_h = int(round(min(plan.height, max(MIN_BOX_PIXELS, box_h))))
    x = int(round(min(max(center_x - box_w / 2, 0), plan.width - box_w)))
    y = int(round(min(max(center_y - box_h / 2, 0), plan.height - box_h)))
    box = (x, y, box_w, box_h)
    template = _patch(_gray(anchor), box, (box_w, box_h))
    return Target(box=box, offset=(point_x - (x + box_w / 2), point_y - (y + box_h / 2)), template=template)


def _new_tracker(frame: np.ndarray, box: Tuple[int, ...]) -> Any:
    """Start a CSRT tracker on a box of a frame.

    Args:
        frame: The frame the target is known in.
        box: The target's box (x, y, w, h) in whole pixels, inside the frame.

    Returns:
        The tracker, ready for update().
    """
    params = cv2.TrackerCSRT_Params()
    params.psr_threshold = CSRT_PEAK_THRESHOLD
    tracker = cv2.TrackerCSRT_create(params)
    tracker.init(frame, box)
    return tracker


def inside(x: float, y: float) -> bool:
    """Tell whether a point in frame fractions sits at least EDGE_MARGIN inside every edge."""
    return EDGE_MARGIN <= x <= 1 - EDGE_MARGIN and EDGE_MARGIN <= y <= 1 - EDGE_MARGIN


def reaches_past_edge(box: Tuple[float, ...], plan: Plan) -> bool:
    """Tell whether a box in pixels reaches past any edge of the decoded frame."""
    x, y, w, h = box
    return x < 0 or y < 0 or x + w > plan.width or y + h > plan.height


DebugWriter = Callable[[int, float, np.ndarray, Tuple[float, ...], str, str, float], None]


def follow(
    anchor: np.ndarray,
    target: Target,
    plan: Plan,
    frames: Iterable[Tuple[int, float, np.ndarray]],
    end_when_done: str,
    direction: str,
    debug: Optional[DebugWriter],
) -> Tuple[List[List[float]], str]:
    """Follow the target through frames that lead away from the sighting.

    Args:
        anchor: The sighting's frame.
        target: The target from make_target.
        plan: The plan, for the frame size.
        frames: (index, time, frame) of each frame, in the order to track.
        end_when_done: How the direction ends when the frames run out:
            "limit" or "frames".
        direction: "back" or "forward", written on the debug frames.
        debug: Writes a debug frame, or None.

    Returns:
        The tracked points ``[t, x, y, score]`` in the order of the frames,
        and how the direction ended. A target the tracker fails on while its
        last box reaches past an edge has left the picture, so that end is
        "exit", not "lost". This happens to a box that started moved inside
        the frame and crosses the picture: it leaves a little before its point.
    """
    tracker = _new_tracker(anchor, target.box)
    width, height = target.box[2], target.box[3]
    points: List[List[float]] = []
    bad = 0
    last_frame, last_box = anchor, target.box
    for index, time, frame in frames:
        ok, box = tracker.update(frame)
        if not ok:
            bad += 1
            if debug is not None:
                # A failed update answers an empty box, so the last box found is drawn.
                debug(index, time, frame, last_box, STATE_FAILED, direction, 0.0)
            if reaches_past_edge(last_box, plan):
                return points, END_EXIT
            if bad >= LOST_AFTER_BAD_FRAMES:
                return points, END_LOST
            # After one failed frame OpenCV's CSRT searches around an invalid
            # center and never finds the target again, so a new tracker starts
            # from the last frame the target was found in.
            tracker = _new_tracker(last_frame, last_box)
            continue
        x = (box[0] + box[2] / 2 + target.offset[0]) / plan.width
        y = (box[1] + box[3] / 2 + target.offset[1]) / plan.height
        if not inside(x, y):
            return points, END_EXIT
        score = similarity(target.template, _patch(_gray(frame), box, (width, height)))
        weak = score < SCORE_FLOOR
        if debug is not None:
            debug(index, time, frame, tuple(box), STATE_WEAK if weak else STATE_OK, direction, score)
        if weak:
            bad += 1
            if bad >= LOST_AFTER_BAD_FRAMES:
                return points, END_LOST
            continue
        bad = 0
        last_frame, last_box = frame, tuple(int(value) for value in box)
        points.append([round(time, TIME_DECIMALS), round(x, FRACTION_DECIMALS), round(y, FRACTION_DECIMALS), round(score, SCORE_DECIMALS)])
    return points, end_when_done


def debug_writer(folder: Path, sighting_index: int) -> DebugWriter:
    """Make the function that writes every fifth tracked frame with its box.

    Args:
        folder: Where the JPEGs go; it is created when missing.
        sighting_index: The index of the sighting's frame; every frame a
            multiple of DEBUG_EVERY frames away from it is written.

    Returns:
        A function of (index, time, frame, box, state, direction, score) that
        writes ``frame_<index>.jpg``: the box in green when the tracker found
        the target ("ok"), orange when the patch lost its likeness ("weak"),
        and red when the tracker failed ("failed"), a dot at the box center,
        and a line of text with the direction, the time, the state, and the score.
    """
    folder.mkdir(parents=True, exist_ok=True)

    def write(index: int, time: float, frame: np.ndarray, box: Tuple[float, ...], state: str, direction: str, score: float) -> None:
        """Write one frame when it lies a multiple of DEBUG_EVERY frames from the sighting."""
        if (index - sighting_index) % DEBUG_EVERY != 0:
            return
        picture = frame.copy()
        color = DEBUG_COLORS[state]
        x, y, w, h = (int(round(value)) for value in box)
        cv2.rectangle(picture, (x, y), (x + w, y + h), color, DEBUG_LINE_WIDTH)
        cv2.circle(picture, (x + w // 2, y + h // 2), DEBUG_DOT_RADIUS, color, -1)
        label = f"{direction} t={time:.2f}s {state} score={score:.2f}"
        cv2.putText(picture, label, DEBUG_TEXT_ORIGIN, cv2.FONT_HERSHEY_SIMPLEX, DEBUG_FONT_SCALE, DEBUG_TEXT_COLOR, 1, cv2.LINE_AA)
        cv2.imwrite(str(folder / f"frame_{index:04d}.jpg"), picture, [cv2.IMWRITE_JPEG_QUALITY, DEBUG_JPEG_QUALITY])

    return write


def track(request: Request, debug_dir: Optional[Path]) -> Dict[str, Any]:
    """Decode the stretch around the sighting and follow the sponge both ways.

    Args:
        request: The checked request.
        debug_dir: A folder for debug frames, or None.

    Returns:
        The answer: ``{"points", "box", "ends"}``.

    Raises:
        WorkerError: When the source cannot be read, or it has no frame at
            the sighting's time (``time:``).
    """
    plan = make_plan(request, probe_size(request.source))
    step = 1 / request.fps
    with Decoder(decode_args(request, plan), plan.width, plan.height) as decoder:
        before: List[np.ndarray] = []
        while len(before) <= plan.back_frames:
            frame = decoder.read()
            if frame is None:
                break
            before.append(frame)
        if len(before) <= plan.back_frames:
            decoder.check(request.source)
            raise WorkerError(f"time: {request.source} has no frame at {request.time:g} seconds")
        anchor = before[-1]
        target = make_target(request, plan, anchor)
        debug = debug_writer(debug_dir, plan.back_frames) if debug_dir is not None else None
        if debug is not None:
            debug(plan.back_frames, request.time, anchor, target.box, STATE_OK, "sighting", SIGHTING_SCORE)

        decoded = {"after": 0}

        def ahead() -> Iterator[Tuple[int, float, np.ndarray]]:
            """Yield the frames after the sighting as ffmpeg decodes them, counting them."""
            for number in range(1, plan.forward_frames + 1):
                frame = decoder.read()
                if frame is None:
                    return
                decoded["after"] = number
                yield plan.back_frames + number, request.time + number * step, frame

        forward, forward_end = follow(anchor, target, plan, ahead(), END_LIMIT, "forward", debug)
        if forward_end == END_LIMIT and decoded["after"] < plan.forward_frames:
            # The frames ran out before the time limit: the video ended.
            forward_end = END_FRAMES
        if forward_end in (END_EXIT, END_LOST):
            decoder.stop()
        decoder.check(request.source)

    behind = ((index, request.time - (plan.back_frames - index) * step, before[index]) for index in range(plan.back_frames - 1, -1, -1))
    backward, back_end = follow(anchor, target, plan, behind, plan.back_end, "back", debug)
    sighting = [request.time, request.point[0], request.point[1], SIGHTING_SCORE]
    return {
        "points": list(reversed(backward)) + [sighting] + forward,
        "box": None if request.box is None else {"w": request.box[2], "h": request.box[3]},
        "ends": {"back": back_end, "forward": forward_end},
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Read the request on stdin, track, and print the answer.

    Args:
        argv: The command line options; None reads sys.argv.

    Returns:
        0 after the answer went to stdout, 1 after an error line went to stderr.
    """
    parser = argparse.ArgumentParser(description="Follow one sighting through its video with OpenCV's CSRT tracker.")
    parser.add_argument("--debug-dir", type=Path, help="write every fifth tracked frame, with its box, as a JPEG into this folder")
    options = parser.parse_args(argv)
    try:
        answer = track(parse_request(sys.stdin.read()), options.debug_dir)
    except WorkerError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(json.dumps(answer) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
