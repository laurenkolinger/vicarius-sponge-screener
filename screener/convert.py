"""Conversion of videos Chrome cannot play into MP4 files it can.

About a third of the TCRMP archive is AVCHD (.mts), HDV (.m2t), DV (.avi), MXF,
or WMV. The Converter reads such a video from the source the app supplies (the
local relay URL), makes an MP4 at the original resolution with ffmpeg, and
reports progress. Progressive 8-bit 4:2:0 H.264, the one kind Chrome decodes,
is copied into the MP4 without re-encoding. Everything else is re-encoded to
H.264, and interlaced video is deinterlaced on the way. One ffmpeg job runs at
a time in one worker thread.
"""

import collections
import dataclasses
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

from screener.keys import cache_id, validate_key

# Where finished and unfinished files live under the cache root.
CONVERTED_FOLDER = "converted"
VIDEO_SUFFIX = ".mp4"
RECORD_SUFFIX = ".json"
PART_SUFFIX = ".part"

# Encoding choices.
HARDWARE_ENCODER = "h264_videotoolbox"
SOFTWARE_ENCODER = "libx264"
COPY_CODEC = "h264"
COPY_FIELD_ORDERS = frozenset({"progressive", "unknown"})
# Chrome decodes only 8-bit 4:2:0 H.264 (limited or full range). 4:2:2, 4:4:4,
# 10-bit, and gray streams are legal H.264 but play as a black picture.
PLAYABLE_PIXEL_FORMATS = frozenset({"yuv420p", "yuvj420p"})
INTERLACED_FIELD_ORDERS = frozenset({"tt", "bb", "tb", "bt"})
KNOWN_FIELD_ORDERS = INTERLACED_FIELD_ORDERS | {"progressive"}
HD_MIN_HEIGHT = 720
HD_BITRATE = "25M"
SD_BITRATE = "8M"
SOFTWARE_PRESET = "veryfast"
SOFTWARE_CRF = "18"
OUTPUT_PIXEL_FORMAT = "yuv420p"
DEINTERLACE_FILTER = "yadif"
# "V" selects video streams that are not cover art, so the probe and the
# conversion always look at the same, real video stream.
VIDEO_STREAM = "V:0"

# Probing.
PROBE_TIMEOUT_SECONDS = 180.0
PROBE_FRAME_COUNT = 30
PROBE_ENTRIES = (
    "stream=codec_name,width,height,field_order,pix_fmt,duration"
    ":format=duration"
    ":frame=interlaced_frame,top_field_first"
)
ENCODER_LIST_TIMEOUT_SECONDS = 30.0

# Running and stopping ffmpeg.
STOP_GRACE_SECONDS = 5.0
STDERR_TAIL_CHARACTERS = 400
STDERR_TAIL_BYTES = 4 * STDERR_TAIL_CHARACTERS
LOG_BLOCK_BYTES = 65536
MICROSECONDS_PER_SECOND = 1000000
# ffmpeg names both fields differently but fills both with microseconds.
PROGRESS_FIELDS = ("out_time_us", "out_time_ms")
# ffmpeg exits with code 0 when its input stops partway (a lost relay
# connection), and leaves a short video. These stderr lines give that away.
READ_FAILURE_MARKERS = (b"Error during demuxing", b"Stream ends prematurely")
FALLBACK_TOOL_FOLDERS = ("/opt/homebrew/bin", "/usr/local/bin")

STATE_NONE = "none"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

QUEUED_MESSAGE = "waiting for the converter"
PROBING_MESSAGE = "reading the source"
CLOSED_MESSAGE = "conversion stopped because the converter closed"


class ConvertError(Exception):
    """Raised when probing or converting a video fails, with the reason."""


@dataclass
class ConvertStatus:
    """The state of one key's conversion.

    Attributes:
        state: "none", "queued", "running", "done", or "failed".
        progress: Fraction of the video converted, from 0.0 to 1.0.
        message: What the converter is doing, or why the conversion failed.
    """

    state: str
    progress: float
    message: str


def _tail(text: str) -> str:
    """Return the last 400 characters of a program's error text.

    Args:
        text: Everything the program wrote to stderr.

    Returns:
        The stripped text cut to its last STDERR_TAIL_CHARACTERS characters,
        or a short note when the program wrote nothing.
    """
    stripped = text.strip()
    if not stripped:
        return "the program wrote no error text"
    return stripped[-STDERR_TAIL_CHARACTERS:]


def _stop_process(process: "subprocess.Popen") -> None:
    """Stop a child process, politely first and by force after a grace period.

    Args:
        process: The child to stop. A child that already exited is left alone.
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_tool(
    args: List[str],
    timeout: float,
    label: str,
    on_start: Optional[Callable[["subprocess.Popen"], None]] = None,
) -> Tuple[int, str, str]:
    """Run a short-lived program and collect its output.

    Both output streams go to temporary files instead of pipes. A file never
    blocks the writer, and the wait ends when the program itself exits, even
    when a wrapper script leaves a child behind that still holds the streams.

    Args:
        args: The program and its arguments. No shell is involved.
        timeout: Seconds to wait before the program is killed.
        label: The calling function's name, used to open error messages.
        on_start: Called with the running child, so a caller can stop it early.

    Returns:
        The exit code, stdout, and stderr (both decoded as UTF-8).

    Raises:
        ConvertError: When the program cannot start or outlasts the timeout.
    """
    with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out_file, stderr=err_file)
        except OSError as error:
            raise ConvertError(f"{label}: could not start {args[0]}: {error}") from error
        if on_start is not None:
            on_start(process)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise ConvertError(
                f"{label}: {args[0]} gave no answer within {timeout:g} seconds and was stopped"
            ) from error
        out_file.seek(0)
        err_file.seek(0)
        return code, out_file.read().decode("utf-8", "replace"), err_file.read().decode("utf-8", "replace")


def _field_order(stream: Mapping[str, Any], frames: List[Mapping[str, Any]]) -> str:
    """Work out the field order of a video stream.

    ffprobe names the field order for MPEG-2 and H.264 streams. It names none
    for DV, although DV camcorder footage is interlaced, so the flags on the
    first decoded frames settle the question when the stream gives no answer.

    Args:
        stream: The stream entry of ffprobe's JSON report.
        frames: The frame entries of the same report, possibly empty.

    Returns:
        "progressive", "tt", "bb", "tb", "bt", or "unknown" when the stream
        names no order and no frame could be decoded.
    """
    named = stream.get("field_order")
    if named in KNOWN_FIELD_ORDERS:
        return str(named)
    interlaced = [frame for frame in frames if frame.get("interlaced_frame") == 1]
    if interlaced:
        return "tt" if interlaced[0].get("top_field_first") == 1 else "bb"
    if frames:
        return "progressive"
    return "unknown"


def _duration(stream: Mapping[str, Any], container: Mapping[str, Any]) -> float:
    """Pick the video length from ffprobe's report.

    Args:
        stream: The stream entry of the report.
        container: The format entry of the report.

    Returns:
        The stream's duration in seconds, else the container's, else 0.0 when
        neither holds a positive finite number.
    """
    for candidate in (stream.get("duration"), container.get("duration")):
        try:
            seconds = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    return 0.0


def _probe(
    ffprobe: str,
    source: str,
    timeout: float,
    on_start: Optional[Callable[["subprocess.Popen"], None]],
) -> Dict[str, Any]:
    """Do the work of probe, with a hook that lets the Converter stop ffprobe.

    Args:
        ffprobe: The ffprobe program.
        source: The file path or URL to read.
        timeout: Seconds to wait for ffprobe.
        on_start: Called with the running ffprobe child, or None.

    Returns:
        The same dictionary as probe.

    Raises:
        ConvertError: Under the same conditions as probe.
    """
    if not isinstance(source, str) or not source:
        raise ConvertError(f"probe: source must be non-empty text, got {source!r}")
    args = [
        ffprobe, "-v", "error",
        "-select_streams", VIDEO_STREAM,
        "-show_entries", PROBE_ENTRIES,
        "-read_intervals", f"%+#{PROBE_FRAME_COUNT}",
        "-of", "json",
        "-i", source,
    ]
    code, out, err = _run_tool(args, timeout, "probe", on_start)
    if code != 0:
        raise ConvertError(f"probe: ffprobe could not read {source} (exit code {code}): {_tail(err)}")
    try:
        report = json.loads(out)
    except ValueError as error:
        raise ConvertError(f"probe: ffprobe gave an unreadable report for {source}: {error}") from error
    streams = report.get("streams") or []
    if not streams:
        raise ConvertError(f"probe: no video stream in {source}")
    stream = streams[0]
    codec, width, height = stream.get("codec_name"), stream.get("width"), stream.get("height")
    if not codec or not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ConvertError(f"probe: the video stream in {source} has no codec name or no frame size")
    return {
        "codec": str(codec),
        "width": width,
        "height": height,
        "field_order": _field_order(stream, report.get("frames") or []),
        "pix_fmt": str(stream.get("pix_fmt") or ""),
        "duration": _duration(stream, report.get("format") or {}),
    }


def probe(ffprobe: str, source: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Read the facts about a video that decide how it is converted.

    Args:
        ffprobe: The ffprobe program, as a name on PATH or a full path.
        source: The file path or URL to read. It is passed as one argument
            after ``-i``, so it can never act as an option.
        timeout: Seconds to wait for ffprobe before it is stopped.

    Returns:
        A dictionary with "codec" (ffprobe's codec name, such as "h264"),
        "width" and "height" (whole pixels), "field_order" ("progressive",
        "tt", "bb", "tb", "bt", or "unknown"), "pix_fmt" (ffprobe's pixel
        format name, such as "yuv420p", or "" when it names none), and
        "duration" (seconds as a float, 0.0 when the file does not say).

    Raises:
        ConvertError: When the source is not text, ffprobe cannot start, times
            out, cannot read the source, or finds no video stream. The message
            names the source and carries the end of ffprobe's error text.
    """
    return _probe(ffprobe, source, timeout, None)


def pick_encoder(ffmpeg: str) -> str:
    """Choose the H.264 encoder for videos that need re-encoding.

    Args:
        ffmpeg: The ffmpeg program, as a name on PATH or a full path.

    Returns:
        "h264_videotoolbox" (the Mac hardware encoder) when ``ffmpeg -encoders``
        lists it as an encoder name, otherwise "libx264".

    Raises:
        ConvertError: When ffmpeg cannot start, times out, or exits with an error.
    """
    code, out, err = _run_tool([ffmpeg, "-hide_banner", "-encoders"], ENCODER_LIST_TIMEOUT_SECONDS, "pick_encoder")
    if code != 0:
        raise ConvertError(f"pick_encoder: {ffmpeg} -encoders exited with code {code}: {_tail(err)}")
    for line in out.splitlines():
        columns = line.split()
        if len(columns) >= 2 and columns[1] == HARDWARE_ENCODER:
            return HARDWARE_ENCODER
    return SOFTWARE_ENCODER


def build_ffmpeg_args(ffmpeg: str, source: str, info: Mapping[str, Any], out_path: Path, encoder: str) -> List[str]:
    """Build the ffmpeg command that turns one source into an MP4.

    H.264 video in 8-bit 4:2:0 (yuv420p or yuvj420p) whose field order is
    progressive or unknown is copied. Every other video is re-encoded with the
    given encoder to yuv420p, at 25M for 720 rows and above and 8M below
    (libx264 uses preset veryfast and crf 18 in place of a bitrate), and
    deinterlaced with yadif when its field order is tt, bb, tb, or bt. The
    frame size is never changed. Only the first real video stream is mapped,
    because the sound, subtitle, and data streams of camcorder files cannot go
    into an MP4 and would make ffmpeg fail.

    Args:
        ffmpeg: The ffmpeg program.
        source: The file path or URL ffmpeg reads.
        info: A probe result. "codec", "height", "field_order", and
            "pix_fmt" are used.
        out_path: Where ffmpeg writes. The format is forced to MP4, so the
            path may end in ``.part``.
        encoder: "h264_videotoolbox" or "libx264", from pick_encoder.

    Returns:
        The argument list for subprocess, starting with the program.

    Raises:
        ConvertError: When source or encoder is not non-empty text, or info
            lacks a codec, a positive whole height, a field order, or a
            pix_fmt entry (an empty pix_fmt is allowed and means unknown).
    """
    for name, value in (("source", source), ("encoder", encoder)):
        if not isinstance(value, str) or not value:
            raise ConvertError(f"build_ffmpeg_args: {name} must be non-empty text, got {value!r}")
    codec, height, field_order = info.get("codec"), info.get("height"), info.get("field_order")
    if not isinstance(codec, str) or not codec:
        raise ConvertError(f"build_ffmpeg_args: info has no codec name, got {codec!r}")
    if not isinstance(height, int) or isinstance(height, bool) or height <= 0:
        raise ConvertError(f"build_ffmpeg_args: info height must be a positive whole number, got {height!r}")
    if not isinstance(field_order, str) or not field_order:
        raise ConvertError(f"build_ffmpeg_args: info has no field_order, got {field_order!r}")
    pix_fmt = info.get("pix_fmt")
    if not isinstance(pix_fmt, str):
        raise ConvertError(f"build_ffmpeg_args: info pix_fmt must be text, empty when unknown, got {pix_fmt!r}")

    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", source, "-map", f"0:{VIDEO_STREAM}"]
    if codec == COPY_CODEC and field_order in COPY_FIELD_ORDERS and pix_fmt in PLAYABLE_PIXEL_FORMATS:
        args += ["-c:v", "copy"]
    else:
        args += ["-c:v", encoder, "-pix_fmt", OUTPUT_PIXEL_FORMAT]
        if encoder == SOFTWARE_ENCODER:
            args += ["-preset", SOFTWARE_PRESET, "-crf", SOFTWARE_CRF]
        else:
            args += ["-b:v", HD_BITRATE if height >= HD_MIN_HEIGHT else SD_BITRATE]
        if field_order in INTERLACED_FIELD_ORDERS:
            args += ["-vf", DEINTERLACE_FILTER]
    args += ["-an", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-y", "-f", "mp4", str(out_path)]
    return args


def _describe(info: Mapping[str, Any], args: List[str]) -> str:
    """Say in plain words what a conversion command does, for the status message.

    Args:
        info: The probe result of the source.
        args: The command from build_ffmpeg_args.

    Returns:
        A short sentence fragment such as "re-encoding mpeg2video video to
        H.264 and deinterlacing" or "re-encoding h264 video (yuv422p10le is
        not playable in Chrome)".
    """
    if args[args.index("-c:v") + 1] == "copy":
        return "copying the H.264 video into an MP4 file"
    words = f"re-encoding {info['codec']} video"
    if info["codec"] != COPY_CODEC:
        words += " to H.264"
    elif info["pix_fmt"] not in PLAYABLE_PIXEL_FORMATS:
        reason = f"{info['pix_fmt']} is not playable in Chrome" if info["pix_fmt"] else "the pixel format is unknown"
        words += f" ({reason})"
    if "-vf" in args:
        words += " and deinterlacing"
    return words


def _progress_seconds(line: str) -> Optional[float]:
    """Read the converted length from one line of ffmpeg's ``-progress`` output.

    Args:
        line: One ``name=value`` line.

    Returns:
        Seconds of video written so far, or None when the line is another
        field or holds no number yet (ffmpeg prints N/A before the first frame).
    """
    name, _, value = line.strip().partition("=")
    if name not in PROGRESS_FIELDS:
        return None
    try:
        microseconds = int(value)
    except ValueError:
        return None
    return max(microseconds, 0) / MICROSECONDS_PER_SECOND


def _read_log(log: IO[bytes]) -> Tuple[str, bool]:
    """Read ffmpeg's stderr back from its temporary file.

    Args:
        log: The file ffmpeg wrote its stderr to, open for reading.

    Returns:
        The last 400 characters of the text, and True when any line reports
        that ffmpeg could not read its input to the end.
    """
    log.seek(0)
    overlap = max(len(marker) for marker in READ_FAILURE_MARKERS) - 1
    read_failed = False
    carry = b""
    kept = b""
    while True:
        block = log.read(LOG_BLOCK_BYTES)
        if not block:
            break
        window = carry + block
        if any(marker in window for marker in READ_FAILURE_MARKERS):
            read_failed = True
        carry = window[-overlap:]
        kept = (kept + block)[-STDERR_TAIL_BYTES:]
    return _tail(kept.decode("utf-8", "replace")), read_failed


def _remove_file(path: Path) -> None:
    """Delete a file, and accept that it may already be gone.

    Args:
        path: The file to delete.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _resolve_tool(name: str) -> str:
    """Find ffmpeg or ffprobe when the app's PATH lacks the Homebrew folder.

    Args:
        name: A program name or path, as given to the Converter.

    Returns:
        The name unchanged when it is a path, the full path from PATH when the
        program is there, the full path inside the first fallback folder that
        holds it, and otherwise the name unchanged (starting it then fails with
        a message that names it).
    """
    if os.sep in name:
        return name
    found = shutil.which(name)
    if found:
        return found
    for folder in FALLBACK_TOOL_FOLDERS:
        candidate = Path(folder) / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return name


class Converter:
    """Queue of video conversions, run one at a time by one worker thread.

    start and status may be called from many server threads at once. A lock
    guards the shared state, and it is never held while ffprobe or ffmpeg runs.
    A finished video is recognized by its file on disk, so it survives restarts.
    """

    def __init__(
        self,
        cache_root: Path,
        source_for: Callable[[str], str],
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        """Prepare the output folder, clear unfinished files, and start the worker.

        Args:
            cache_root: The app's cache folder. Output goes to
                ``<cache_root>/converted``.
            source_for: Given a validated key, returns what ffmpeg should read:
                the local relay URL in the app, a file path in tests.
            ffmpeg: The ffmpeg program, as a name or a full path.
            ffprobe: The ffprobe program, as a name or a full path.

        Raises:
            TypeError: When source_for is not callable or cache_root is not a path.
            ConvertError: When the output folder cannot be created.
        """
        if not callable(source_for):
            raise TypeError(f"Converter: source_for must be callable, got {type(source_for).__name__}")
        try:
            self._folder = Path(cache_root) / CONVERTED_FOLDER
        except TypeError as error:
            raise TypeError(f"Converter: cache_root must be a path, got {type(cache_root).__name__}") from error
        self._source_for = source_for
        self._ffmpeg = _resolve_tool(ffmpeg)
        self._ffprobe = _resolve_tool(ffprobe)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._jobs: Dict[str, ConvertStatus] = {}
        self._queue: Deque[str] = collections.deque()
        self._process: Optional["subprocess.Popen"] = None
        self._closed = False
        self._encoder: Optional[str] = None
        try:
            self._folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ConvertError(f"Converter: could not create the folder {self._folder}: {error}") from error
        self._remove_part_files()
        self._worker = threading.Thread(target=self._work, name="screener-convert", daemon=True)
        self._worker.start()

    def start(self, key: str) -> ConvertStatus:
        """Queue a key for conversion unless it is already queued, running, or done.

        Args:
            key: The S3 key of the video.

        Returns:
            The key's status after the call: "done" for a finished video,
            "queued" or "running" unchanged for a job in flight, and "queued"
            for a new key or a key whose last try failed.

        Raises:
            InvalidKey: When the key breaks the rules in screener.keys.
            ConvertError: When the converter is closed and the key is not done.
        """
        validate_key(key)
        with self._wake:
            current = self._status_locked(key)
            if current.state in (STATE_DONE, STATE_QUEUED, STATE_RUNNING):
                return current
            if self._closed:
                raise ConvertError(f"start: the converter is closed, so {key} was not queued")
            self._jobs[key] = ConvertStatus(STATE_QUEUED, 0.0, QUEUED_MESSAGE)
            self._queue.append(key)
            self._wake.notify()
            return dataclasses.replace(self._jobs[key])

    def status(self, key: str) -> ConvertStatus:
        """Report where a key's conversion stands.

        Args:
            key: The S3 key of the video.

        Returns:
            A copy of the status. "none" means no job and no finished file.

        Raises:
            InvalidKey: When the key breaks the rules in screener.keys.
        """
        validate_key(key)
        with self._lock:
            return self._status_locked(key)

    def converted_path(self, key: str) -> Optional[Path]:
        """Return the finished MP4 for a key.

        Args:
            key: The S3 key of the video.

        Returns:
            The path of ``<cache_id>.mp4`` when it exists, else None.

        Raises:
            InvalidKey: When the key breaks the rules in screener.keys.
        """
        validate_key(key)
        path = self._path(key, VIDEO_SUFFIX)
        return path if path.is_file() else None

    def close(self) -> None:
        """Stop the running ffmpeg or ffprobe, end the worker, and remove unfinished files.

        Queued keys return to "none". The stopped key reports "failed". A
        second call does nothing more.
        """
        with self._wake:
            self._closed = True
            process = self._process
            for key in self._queue:
                self._jobs.pop(key, None)
            self._queue.clear()
            self._wake.notify_all()
        if process is not None:
            _stop_process(process)
        self._worker.join()
        self._remove_part_files()

    def _path(self, key: str, suffix: str) -> Path:
        """Return the output path for a key.

        Args:
            key: A validated key.
            suffix: ".mp4", ".json", or either one followed by ".part".

        Returns:
            ``<cache_root>/converted/<cache_id><suffix>``.
        """
        return self._folder / (cache_id(key) + suffix)

    def _status_locked(self, key: str) -> ConvertStatus:
        """Compute a key's status. The caller holds the lock.

        Args:
            key: A validated key.

        Returns:
            A fresh ConvertStatus: the job in flight, else "done" when the
            finished file exists, else the last failure, else "none".
        """
        job = self._jobs.get(key)
        if job is not None and job.state in (STATE_QUEUED, STATE_RUNNING):
            return dataclasses.replace(job)
        if self._path(key, VIDEO_SUFFIX).is_file():
            return ConvertStatus(STATE_DONE, 1.0, "")
        if job is not None:
            return dataclasses.replace(job)
        return ConvertStatus(STATE_NONE, 0.0, "")

    def _remove_part_files(self) -> None:
        """Delete every ``.part`` file in the output folder."""
        for path in self._folder.glob("*" + PART_SUFFIX):
            _remove_file(path)

    def _is_closed(self) -> bool:
        """Return True once close has been called."""
        with self._lock:
            return self._closed

    def _adopt(self, process: "subprocess.Popen") -> None:
        """Record the running child so close can stop it.

        Args:
            process: The ffprobe or ffmpeg child that just started. It is
                stopped at once when the converter closed in the meantime.
        """
        with self._lock:
            self._process = process
            closed = self._closed
        if closed:
            _stop_process(process)

    def _release(self) -> None:
        """Forget the child recorded by _adopt, after it has exited."""
        with self._lock:
            self._process = None

    def _set_running(self, key: str, progress: float, message: str) -> None:
        """Publish a running job's progress, never letting it move backward.

        Args:
            key: The key being converted.
            progress: The newly measured fraction, from 0.0 to 1.0.
            message: What the converter is doing.
        """
        with self._lock:
            previous = self._jobs[key].progress
            self._jobs[key] = ConvertStatus(STATE_RUNNING, max(previous, progress), message)

    def _work(self) -> None:
        """Run queued jobs one after another until the converter closes."""
        while True:
            with self._wake:
                while not self._queue and not self._closed:
                    self._wake.wait()
                if self._closed:
                    return
                key = self._queue.popleft()
                self._jobs[key] = ConvertStatus(STATE_RUNNING, 0.0, PROBING_MESSAGE)
            self._run_job(key)

    def _run_job(self, key: str) -> None:
        """Convert one key and record the outcome. No exception leaves this method.

        Args:
            key: The key taken from the queue, already marked "running".
        """
        video_part = self._path(key, VIDEO_SUFFIX + PART_SUFFIX)
        record_part = self._path(key, RECORD_SUFFIX + PART_SUFFIX)
        try:
            self._convert(key, video_part, record_part)
            return
        except ConvertError as error:
            failure = str(error)
        except Exception as error:  # the worker must outlive any one bad job
            failure = f"convert: unexpected {type(error).__name__} while converting {key}: {error}"
        _remove_file(video_part)
        _remove_file(record_part)
        with self._lock:
            if self._closed:
                failure = CLOSED_MESSAGE
            self._jobs[key] = ConvertStatus(STATE_FAILED, 0.0, failure)

    def _convert(self, key: str, video_part: Path, record_part: Path) -> None:
        """Probe the source, run ffmpeg, and move the finished files into place.

        Args:
            key: The key to convert.
            video_part: The ``.mp4.part`` path ffmpeg writes.
            record_part: The ``.json.part`` path of the record file.

        Raises:
            ConvertError: When the probe fails, ffmpeg fails or cannot read the
                whole source, no output appears, or the converter closes.
            OSError: When the record or the finished files cannot be written.
        """
        source = self._source_for(key)
        try:
            info = _probe(self._ffprobe, source, PROBE_TIMEOUT_SECONDS, self._adopt)
        finally:
            self._release()
        if self._encoder is None:
            self._encoder = pick_encoder(self._ffmpeg)
        args = build_ffmpeg_args(self._ffmpeg, source, info, video_part, self._encoder)
        if self._is_closed():
            raise ConvertError(CLOSED_MESSAGE)
        message = _describe(info, args)
        self._set_running(key, 0.0, message)
        code, error_text, read_failed = self._run_ffmpeg(key, args, info["duration"], message)
        # An ffmpeg that close() stopped exits with 255 or a signal code, never
        # 0, so a zero here always means a complete file worth keeping.
        if code != 0:
            raise ConvertError(f"ffmpeg exited with code {code}: {error_text}")
        if read_failed:
            raise ConvertError(
                f"ffmpeg could not read the whole source, so the partial video was discarded: {error_text}"
            )
        if not video_part.is_file() or video_part.stat().st_size == 0:
            raise ConvertError(f"ffmpeg exited with code 0 but wrote no output to {video_part}")
        record_part.write_text(json.dumps({"key": key, "probe": info, "args": args}, indent=2))
        with self._lock:
            os.replace(str(record_part), str(self._path(key, RECORD_SUFFIX)))
            os.replace(str(video_part), str(self._path(key, VIDEO_SUFFIX)))
            del self._jobs[key]

    def _run_ffmpeg(self, key: str, args: List[str], duration: float, message: str) -> Tuple[int, str, bool]:
        """Run one ffmpeg command to its end while publishing progress.

        stderr goes to a temporary file, so a noisy ffmpeg can never fill a
        pipe and stall. stdout carries only the ``-progress`` lines.

        Args:
            key: The key being converted.
            args: The command from build_ffmpeg_args.
            duration: The probed length in seconds, or 0.0 when unknown.
            message: The status message to keep while progress advances.

        Returns:
            The exit code, the last 400 characters of stderr, and whether
            stderr reports that the input stopped early.

        Raises:
            ConvertError: When ffmpeg cannot start.
        """
        with tempfile.TemporaryFile() as log:
            try:
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log)
            except OSError as error:
                raise ConvertError(f"convert: could not start {args[0]}: {error}") from error
            self._adopt(process)
            try:
                for raw in process.stdout:
                    seconds = _progress_seconds(raw.decode("ascii", "replace"))
                    if seconds is not None and duration > 0:
                        self._set_running(key, min(seconds / duration, 1.0), message)
                code = process.wait()
            finally:
                _stop_process(process)
                process.stdout.close()
                self._release()
            error_text, read_failed = _read_log(log)
        return code, error_text, read_failed
