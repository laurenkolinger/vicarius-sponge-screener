"""Tests for screener/track_worker.py, the program that follows a sighting with OpenCV.

The worker runs under the Python of .venv-track, where OpenCV and numpy are
installed, so every test starts it as a subprocess with that Python, the way
screener.tracking does, and sends one JSON request on stdin. The module makes
two clips with ffmpeg: a colored random texture that pans left at a known
speed, and a flat gray picture. It skips with the reason when the venv or
ffmpeg is missing. test_e2e.py makes its pan clip with make_clip and PAN_FILTER
from here, so both suites check the same known motion.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from screener.config import TRACK_PYTHON, TRACK_WORKER

HOMEBREW_BIN = Path("/opt/homebrew/bin")
CLIP_SECONDS = 12
CLIP_RATE = 30
CLIP_WIDTH = 640
CLIP_HEIGHT = 360
PAN_PIXELS_PER_SECOND = 100
# The picture moves left by this fraction of its width every second (0.156).
PAN_SPEED = PAN_PIXELS_PER_SECOND / CLIP_WIDTH
# The window slides this far over the clip, so the texture is that much wider than the
# picture; a narrower texture would stop the pan when the window reaches its end.
TEXTURE_WIDTH = CLIP_WIDTH + PAN_PIXELS_PER_SECOND * CLIP_SECONDS
BLUR_SIGMA = 2.5
PAN_SEED = 11


def pan_texture(seed: int) -> str:
    """Return a lavfi source: a random texture, blurred into blobs, made contrasty, and
    cropped by a window that slides right at PAN_PIXELS_PER_SECOND.

    Args:
        seed: The noise seed; another seed gives another texture with the same motion.
    """
    return (
        f"color=c=gray:s={TEXTURE_WIDTH}x{CLIP_HEIGHT}:r={CLIP_RATE}:d={CLIP_SECONDS},format=yuv444p,"
        f"noise=alls=100:allf=u:all_seed={seed},gblur=sigma={BLUR_SIGMA},eq=contrast=8:saturation=3,"
        f"crop={CLIP_WIDTH}:{CLIP_HEIGHT}:x='t*{PAN_PIXELS_PER_SECOND}':y=0"
    )


PAN_FILTER = pan_texture(PAN_SEED)
# The pan clip cross-fading into its own negative with the same motion, from 5.5 s to 6.5 s:
# the tracker can follow the motion, but the patch stops looking like the sighting. The
# negative correlates at -1 with the sighting's patch, so the score falls under the floor on
# every FFmpeg build. A second random texture did not: two unrelated textures of blobs this
# size still correlate at about 0.1 on a 43 pixel patch with Ubuntu's FFmpeg 6.1, above the
# 0.05 floor, so the direction ran to its limit there.
FADE_START_SECONDS = 5.5
FADE_SECONDS = 1.0
FADE_FILTER = (
    f"{pan_texture(PAN_SEED)}[a];{pan_texture(PAN_SEED)},negate[b];"
    f"[a][b]xfade=transition=fade:duration={FADE_SECONDS:g}:offset={FADE_START_SECONDS:g}[out0]"
)
# The worker drops a frame whose patch scores under this likeness to the sighting.
SCORE_FLOOR = 0.05
GRAY_FILTER = f"color=c=0x808080:s={CLIP_WIDTH}x{CLIP_HEIGHT}:r={CLIP_RATE}:d={CLIP_SECONDS}"
# The pan clip with two short gray flashes, at 5.5 s and 6.5 s, two source frames each so
# one of them lands among the frames the worker decodes at 15 per second.
FLASH_FRAMES = ((165, 166), (195, 196))
FLASH_SECONDS = (5.5, 6.5)
BLINK_FILTER = PAN_FILTER + ",drawbox=x=0:y=0:w=iw:h=ih:color=gray:t=fill:enable='%s'" % "+".join(
    "between(n,%d,%d)" % pair for pair in FLASH_FRAMES
)
CLIP_TIMEOUT_SECONDS = 120
WORKER_TIMEOUT_SECONDS = 120
JPEG_MAGIC = b"\xff\xd8\xff"

# Every tracked point lies within 2 percent of the width of the known motion.
TOLERANCE = 0.02
# The worker ends a direction when the point comes this close to an edge.
EDGE_MARGIN = 0.02
SIGHTED_AT = 5.0
WORKER_FPS = 15
# The worker rounds times to the microsecond.
TIME_ROUNDING = 1e-6
# At most two failed frames in a row leave a gap; the third ends the direction.
LONGEST_GAP = 3 / WORKER_FPS
EXIT_SLACK_SECONDS = 0.3
DEBUG_EVERY = 5


def find_ffmpeg() -> Optional[str]:
    """Return the ffmpeg program from PATH or the Homebrew folder, or None when it is missing."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidate = HOMEBREW_BIN / "ffmpeg"
    return str(candidate) if candidate.is_file() else None


def make_clip(ffmpeg: str, source_filter: str, out: Path) -> Path:
    """Encode a lavfi source as an H.264 MP4 that Chrome and ffmpeg both play.

    Args:
        ffmpeg: The ffmpeg program.
        source_filter: A lavfi filter graph, such as PAN_FILTER.
        out: The file to write.

    Returns:
        The written file.
    """
    subprocess.run(
        [
            ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", source_filter, "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-movflags", "+faststart", str(out),
        ],
        check=True,
        timeout=CLIP_TIMEOUT_SECONDS,
    )
    return out


def expected_x(start_x: float, start_time: float, time: float) -> float:
    """Return where a point of the pan clip sits at a time, as a fraction of the width.

    Args:
        start_x: Where the point sits at start_time.
        start_time: The time of start_x in seconds.
        time: The time asked for in seconds.

    Returns:
        The fraction of the width; the picture moves left at PAN_SPEED.
    """
    return start_x - PAN_SPEED * (time - start_time)


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    """Make the pan clip and the gray clip once, or skip when the venv or ffmpeg is missing.

    Returns:
        ``{"pan": path, "gray": path, "blink": path, "fade": path}``.
    """
    if not TRACK_PYTHON.exists():
        pytest.skip(f"the tracking venv is missing ({TRACK_PYTHON}); the worker needs its OpenCV. See the README section Resume from cold.")
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed (brew install ffmpeg); the worker decodes video with it")
    folder = tmp_path_factory.mktemp("track-clips")
    return {
        "pan": make_clip(ffmpeg, PAN_FILTER, folder / "pan.mp4"),
        "gray": make_clip(ffmpeg, GRAY_FILTER, folder / "gray.mp4"),
        "blink": make_clip(ffmpeg, BLINK_FILTER, folder / "blink.mp4"),
        "fade": make_clip(ffmpeg, FADE_FILTER, folder / "fade.mp4"),
    }


def run_worker(request: Any, *options: str, raw: Optional[str] = None) -> Tuple[int, Optional[Dict[str, Any]], str]:
    """Run the worker on one request, the way screener.tracking runs it.

    Args:
        request: The request, sent as JSON.
        *options: Command line options, such as ``--debug-dir`` and a folder.
        raw: Text to send in place of the JSON of request.

    Returns:
        The exit code, the parsed answer (None unless the exit code is 0), and stderr.
    """
    completed = subprocess.run(
        [str(TRACK_PYTHON), "-I", str(TRACK_WORKER), *options],
        input=json.dumps(request) if raw is None else raw,
        capture_output=True,
        text=True,
        timeout=WORKER_TIMEOUT_SECONDS,
    )
    answer = json.loads(completed.stdout) if completed.returncode == 0 else None
    return completed.returncode, answer, completed.stderr


def pan_request(clips: Dict[str, Path], **changes: Any) -> Dict[str, Any]:
    """Return a request for a point in the middle of the pan clip at 5 s, with changes applied."""
    request = {"source": str(clips["pan"]), "time": SIGHTED_AT, "point": {"x": 0.5, "y": 0.5}, "box": None}
    request.update(changes)
    return request


def times_of(points: List[List[float]]) -> List[float]:
    """Return the times of a track's points."""
    return [point[0] for point in points]


def assert_follows_the_pan(points: List[List[float]], start_x: float, start_y: float, start_time: float = SIGHTED_AT) -> None:
    """Check a track against the known motion of the pan clip.

    Args:
        points: The track's ``[t, x, y, score]`` points.
        start_x: The sighting's x at start_time.
        start_y: The sighting's y, which the pan never changes.
        start_time: The sighting's time in seconds.
    """
    times = times_of(points)
    assert times == sorted(times) and len(set(times)) == len(times), "the times do not strictly increase: %s" % times
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert max(gaps, default=0) <= LONGEST_GAP + 1e-6, "a gap is longer than two failed frames: %s" % max(gaps)
    for time, x, y, score in points:
        expected = expected_x(start_x, start_time, time)
        assert abs(x - expected) <= TOLERANCE, "at %.3f s x is %.4f, the pan puts it at %.4f" % (time, x, expected)
        assert abs(y - start_y) <= TOLERANCE, "at %.3f s y is %.4f, it stays at %.4f" % (time, y, start_y)
        assert 0.0 <= score <= 1.0, "the score %s is outside 0 to 1" % score


# ----- following the motion -----


def test_a_point_follows_the_pan_backward_and_forward_until_it_leaves_the_picture(clips: Dict[str, Path]) -> None:
    code, answer, err = run_worker(pan_request(clips))
    assert code == 0, err
    assert set(answer) == {"points", "box", "ends"}
    assert answer["box"] is None
    assert answer["ends"] == {"back": "exit", "forward": "exit"}
    points = answer["points"]
    assert [SIGHTED_AT, 0.5, 0.5, 1.0] in points, "the sighting itself is not in the track"
    assert_follows_the_pan(points, 0.5, 0.5)
    # Moving at PAN_SPEED from the middle, the point reaches the 2 percent margin about 3.07 s
    # before and after the sighting.
    to_edge = (0.5 - EDGE_MARGIN) / PAN_SPEED
    assert abs(points[0][0] - (SIGHTED_AT - to_edge)) <= EXIT_SLACK_SECONDS, points[0]
    assert abs(points[-1][0] - (SIGHTED_AT + to_edge)) <= EXIT_SLACK_SECONDS, points[-1]
    assert all(EDGE_MARGIN <= x <= 1 - EDGE_MARGIN for _, x, _, _ in points)


def test_a_box_sighting_follows_its_box_and_answers_its_size(clips: Dict[str, Path]) -> None:
    # Decoded at half the clip's width, so the scaling path is covered too.
    box = {"x": 0.55, "y": 0.3, "w": 0.1, "h": 0.2}
    request = pan_request(clips, point={"x": 0.6, "y": 0.4}, box=box, back_seconds=2, forward_seconds=2, scale_width=CLIP_WIDTH // 2)
    code, answer, err = run_worker(request)
    assert code == 0, err
    assert answer["box"] == {"w": 0.1, "h": 0.2}
    assert answer["ends"] == {"back": "limit", "forward": "limit"}
    assert_follows_the_pan(answer["points"], 0.6, 0.4)


def test_the_time_limits_end_both_directions_on_the_frame_grid(clips: Dict[str, Path]) -> None:
    code, answer, err = run_worker(pan_request(clips, back_seconds=1, forward_seconds=1.5, fps=WORKER_FPS))
    assert code == 0, err
    assert answer["ends"] == {"back": "limit", "forward": "limit"}
    times = times_of(answer["points"])
    assert times[0] == pytest.approx(SIGHTED_AT - 1.0)
    assert times[-1] == pytest.approx(SIGHTED_AT + 22 / WORKER_FPS)
    assert len(times) == 15 + 1 + 22
    assert all(later - earlier == pytest.approx(1 / WORKER_FPS, abs=TIME_ROUNDING) for earlier, later in zip(times, times[1:]))
    assert_follows_the_pan(answer["points"], 0.5, 0.5)


def test_single_failed_frames_leave_gaps_and_the_direction_still_reaches_its_limit(clips: Dict[str, Path]) -> None:
    request = pan_request(clips, source=str(clips["blink"]), back_seconds=0, forward_seconds=2, fps=WORKER_FPS)
    code, answer, err = run_worker(request)
    assert code == 0, err
    assert answer["ends"] == {"back": "limit", "forward": "limit"}
    times = times_of(answer["points"])
    assert len(times) == 1 + 2 * WORKER_FPS - len(FLASH_SECONDS), "one point per frame, but none for a gray frame: %s" % times
    # Each gray frame leaves one missing frame: a gap of two frame steps right after its flash.
    gaps = [(earlier + later) / 2 for earlier, later in zip(times, times[1:]) if later - earlier > 1.5 / WORKER_FPS]
    assert len(gaps) == len(FLASH_SECONDS), gaps
    assert all(abs(gap - flash) <= 1 / WORKER_FPS for gap, flash in zip(gaps, FLASH_SECONDS)), gaps
    assert_follows_the_pan(answer["points"], 0.5, 0.5)


def test_a_patch_that_stops_looking_like_the_sighting_ends_lost(clips: Dict[str, Path]) -> None:
    request = pan_request(clips, source=str(clips["fade"]), back_seconds=0, forward_seconds=3)
    code, answer, err = run_worker(request)
    assert code == 0, err
    assert answer["ends"] == {"back": "limit", "forward": "lost"}
    points = answer["points"]
    assert FADE_START_SECONDS <= points[-1][0] <= FADE_START_SECONDS + FADE_SECONDS, points[-1]
    assert all(score >= SCORE_FLOOR for _, _, _, score in points), points
    assert_follows_the_pan(points, 0.5, 0.5)


def test_a_sighting_near_the_end_runs_forward_to_the_last_frame(clips: Dict[str, Path]) -> None:
    code, answer, err = run_worker(pan_request(clips, time=11.0, point={"x": 0.9, "y": 0.5}))
    assert code == 0, err
    assert answer["ends"] == {"back": "exit", "forward": "frames"}
    points = answer["points"]
    assert points[-1][0] >= CLIP_SECONDS - 2 / WORKER_FPS, points[-1]
    assert_follows_the_pan(points, 0.9, 0.5, 11.0)


def test_a_point_near_the_edge_exits_forward_and_runs_back_to_the_first_frame(clips: Dict[str, Path]) -> None:
    code, answer, err = run_worker(pan_request(clips, point={"x": 0.06, "y": 0.7}))
    assert code == 0, err
    assert answer["ends"] == {"back": "frames", "forward": "exit"}
    points = answer["points"]
    assert points[0][0] == pytest.approx(0.0), "the track did not reach the start of the video: %s" % points[0]
    assert points[-1][0] <= SIGHTED_AT + (0.06 - EDGE_MARGIN) / PAN_SPEED + EXIT_SLACK_SECONDS, points[-1]
    assert_follows_the_pan(points, 0.06, 0.7)


def test_a_flat_gray_picture_is_lost_in_both_directions(clips: Dict[str, Path]) -> None:
    code, answer, err = run_worker({"source": str(clips["gray"]), "time": SIGHTED_AT, "point": {"x": 0.5, "y": 0.5}, "box": None})
    assert code == 0, err
    assert answer["ends"] == {"back": "lost", "forward": "lost"}
    assert answer["points"] == [[SIGHTED_AT, 0.5, 0.5, 1.0]]


def test_a_sighting_at_the_right_edge_leaves_at_once_backward_and_crosses_the_picture_forward(clips: Dict[str, Path]) -> None:
    # The square around the point reaches past the edge, so it starts moved inside the
    # frame and the point keeps its place beside the square's center.
    code, answer, err = run_worker(pan_request(clips, point={"x": 0.99, "y": 0.5}))
    assert code == 0, err
    assert answer["ends"] == {"back": "exit", "forward": "exit"}
    points = answer["points"]
    assert points[0] == [SIGHTED_AT, 0.99, 0.5, 1.0]
    assert_follows_the_pan(points, 0.99, 0.5)
    assert abs(points[-1][0] - (SIGHTED_AT + (0.99 - EDGE_MARGIN) / PAN_SPEED)) <= EXIT_SLACK_SECONDS, points[-1]


def test_the_debug_folder_gets_every_fifth_frame_with_the_box(clips: Dict[str, Path], tmp_path: Path) -> None:
    folder = tmp_path / "debug"
    code, answer, err = run_worker(pan_request(clips, back_seconds=1, forward_seconds=1.5, fps=WORKER_FPS), "--debug-dir", str(folder))
    assert code == 0, err
    names = sorted(path.name for path in folder.iterdir())
    offsets = [offset for offset in range(-15, 23) if offset % DEBUG_EVERY == 0]
    assert names == ["frame_%04d.jpg" % (offset + 15) for offset in offsets]
    assert all((folder / name).read_bytes().startswith(JPEG_MAGIC) for name in names)


# ----- refusals -----


def test_a_time_after_the_end_of_the_video_fails_with_a_message(clips: Dict[str, Path]) -> None:
    code, answer, err = run_worker(pan_request(clips, time=CLIP_SECONDS + 5.0))
    assert code != 0 and answer is None
    assert err.strip().splitlines()[-1].startswith("time:"), err


def test_an_unreadable_source_fails_with_a_message_that_names_it(tmp_path: Path, clips: Dict[str, Path]) -> None:
    missing = tmp_path / "missing.mp4"
    code, answer, err = run_worker({"source": str(missing), "time": 1.0, "point": {"x": 0.5, "y": 0.5}, "box": None})
    assert code != 0 and answer is None
    assert str(missing) in err, err


GOOD = {"source": "clip.mp4", "time": 5.0, "point": {"x": 0.5, "y": 0.5}, "box": None}
BAD_REQUESTS = [
    ("not json", None, "{nope", "request"),
    ("a list", [GOOD], None, "request"),
    ("an unknown field", dict(GOOD, speed="fast"), None, "request"),
    ("no source", {key: value for key, value in GOOD.items() if key != "source"}, None, "source"),
    ("an empty source", dict(GOOD, source=""), None, "source"),
    ("a number source", dict(GOOD, source=5), None, "source"),
    ("no time", {key: value for key, value in GOOD.items() if key != "time"}, None, "time"),
    ("a text time", dict(GOOD, time="5"), None, "time"),
    ("a bool time", dict(GOOD, time=True), None, "time"),
    ("a negative time", dict(GOOD, time=-1), None, "time"),
    ("a nan time", None, json.dumps(GOOD).replace("5.0", "NaN"), "time"),
    ("an infinite time", None, json.dumps(GOOD).replace("5.0", "1e400"), "time"),
    ("no point", {key: value for key, value in GOOD.items() if key != "point"}, None, "point"),
    ("a list point", dict(GOOD, point=[0.5, 0.5]), None, "point"),
    ("a point without y", dict(GOOD, point={"x": 0.5}), None, "point.y"),
    ("a point outside", dict(GOOD, point={"x": 1.5, "y": 0.5}), None, "point.x"),
    ("no box", {key: value for key, value in GOOD.items() if key != "box"}, None, "box"),
    ("a text box", dict(GOOD, box="big"), None, "box"),
    ("a box without height", dict(GOOD, box={"x": 0.1, "y": 0.1, "w": 0.2}), None, "box.h"),
    ("a box of zero width", dict(GOOD, box={"x": 0.1, "y": 0.1, "w": 0, "h": 0.2}), None, "box.w"),
    ("a box past the right edge", dict(GOOD, box={"x": 0.9, "y": 0.1, "w": 0.2, "h": 0.2}), None, "box"),
    ("a negative back_seconds", dict(GOOD, back_seconds=-1), None, "back_seconds"),
    ("a huge forward_seconds", dict(GOOD, forward_seconds=1000), None, "forward_seconds"),
    ("a zero fps", dict(GOOD, fps=0), None, "fps"),
    ("a fractional scale_width", dict(GOOD, scale_width=640.5), None, "scale_width"),
    ("a tiny scale_width", dict(GOOD, scale_width=8), None, "scale_width"),
]


@pytest.mark.parametrize("label, request_body, raw, field", BAD_REQUESTS, ids=[item[0] for item in BAD_REQUESTS])
def test_a_bad_request_exits_nonzero_with_a_message_that_starts_with_the_field(
    clips: Dict[str, Path], label: str, request_body: Any, raw: Optional[str], field: str
) -> None:
    code, answer, err = run_worker(request_body, raw=raw)
    assert code != 0 and answer is None, label
    last = err.strip().splitlines()[-1]
    assert last.startswith(field + ":"), "%s: %r does not start with %r" % (label, last, field)
