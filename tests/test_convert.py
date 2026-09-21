"""Tests for screener.convert: probing, ffmpeg arguments, and the conversion worker.

The sample clips come from ffmpeg's lavfi ``testsrc`` source and are made once
per test session, so no test reads the network or another screener module.
Conversions run the real ffmpeg. A scripted stand-in replaces ffmpeg only where
a test needs exact control of timing, exit codes, or stderr. Two tests serve a
clip from a loopback HTTP server, because the running app hands ffmpeg a local
URL instead of a file path.
"""

import dataclasses
import http.server
import json
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from screener import convert
from screener.convert import (
    ConvertError,
    Converter,
    ConvertStatus,
    build_ffmpeg_args,
    pick_encoder,
    probe,
)
from screener.keys import InvalidKey, cache_id, quote_key

WAIT_SECONDS = 60.0
POLL_SECONDS = 0.01
DURATION_TOLERANCE = 0.2
HOMEBREW_BIN = Path("/opt/homebrew/bin")

KEY_H264 = "TCRMP_video_ondeck/2012Annual/TCRMP20120801_video_FLC_T1.MTS"
KEY_MPEG2 = "TCRMP_video_ondeck/2008Annual/TCRMP20080715_video_BKP_T2.m2t"
KEY_DV = "TCRMP_video_ondeck/2003Annual/TCRMP20030610_video_SCP_T1-6.avi"
KEY_ODD = "TCRMP_video_ondeck/2009Annual/TCRMP20090701_video_FLC_T3.m2t"
KEY_LONG = "TCRMP_video_ondeck/2010Annual/TCRMP20100812_video_GRB_T4.m2t"
KEY_OTHER = "TCRMP_video_ondeck/2010Annual/TCRMP20100812_video_GRB_T5.m2t"
KEY_THIRD = "TCRMP_video_ondeck/2010Annual/TCRMP20100812_video_GRB_T6.m2t"
KEY_INTERLACED_H264 = "TCRMP_video_ondeck/2012Annual/TCRMP20120818_video_CSE_T2.MTS"
KEY_MXF = "TCRMP_video_ondeck/main/TCRMP2024_video/Annual/TCRMP20241029_video_SHR_with3DCamera/TCRMP20241029_SHR_T6.MXF"
KEY_MOV = "TCRMP_video_ondeck/main/TCRMP2016_video/OtherVideo/MVI_0203.MOV"
KEY_SHELL = "TCRMP_video_ondeck/2005 Annual/a\u00f1o b+c;$(touch pwned)'\"&.mts"

HOSTILE_KEYS = [
    "../etc/passwd",
    "TCRMP_video_ondeck/../secret.mts",
    "other_bucket/video.mts",
    "TCRMP_video_ondeck/a\\b.mts",
    "TCRMP_video_ondeck/a\nb.mts",
    "TCRMP_video_ondeck/folder/",
    "TCRMP_video_ondeck//double.mts",
    "TCRMP_video_ondeck/" + "x" * 2000 + ".mts",
    "TCRMP_video_ondeck/lone\udc80surrogate.mts",
    "",
    None,
    123,
]

ENCODERS_WITH_HARDWARE = (
    "Encoders:\n"
    " V..... = Video\n"
    " ------\n"
    " V....D libx264              libx264 H.264 / AVC (codec h264)\n"
    " V....D h264_videotoolbox    VideoToolbox H.264 Encoder (codec h264)\n"
)
ENCODERS_WITHOUT_HARDWARE = (
    "Encoders:\n"
    " V..... = Video\n"
    " ------\n"
    " V....D libx264              libx264 H.264 / AVC (codec h264)\n"
    " V....D mpeg2video           MPEG-2 video, not h264_videotoolbox (codec mpeg2video)\n"
)

FAKE_TOOL_SOURCE = '''\
"""Stand-in for ffmpeg, driven by the JSON control file beside this script."""
import json
import os
import signal
import sys
import time

with open(os.path.splitext(os.path.abspath(__file__))[0] + ".json") as handle:
    control = json.load(handle)
if "-encoders" in sys.argv:
    sys.stdout.write(control.get("encoders", ""))
    sys.exit(0)
out_path = sys.argv[-1]
if control.get("ignore_terminate"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def log(word):
    """Append one event line (word, output path, clock time) to the shared log."""
    if control.get("log"):
        with open(control["log"], "a") as log_handle:
            log_handle.write("%s %s %.6f\\n" % (word, out_path, time.time()))


log("start")
with open(out_path, "wb") as out_handle:
    out_handle.write(b"fake mp4 bytes")
for line in control.get("progress", []):
    sys.stdout.write(line + "\\n")
    sys.stdout.flush()
    time.sleep(control.get("progress_pause", 0.0))
sys.stderr.write(control.get("stderr", ""))
sys.stderr.flush()
time.sleep(control.get("hold", 0.0))
gate = control.get("wait_for")
deadline = time.time() + control.get("max_wait", 30.0)
while gate and not os.path.exists(gate) and time.time() < deadline:
    time.sleep(0.02)
log("end")
sys.exit(control.get("exit_code", 0))
'''


def wait_for(predicate: Callable[[], bool], what: str, timeout: float = WAIT_SECONDS) -> None:
    """Poll until a condition holds.

    Args:
        predicate: Returns True once the awaited condition holds.
        what: Words for the failure message, such as "the job to finish".
        timeout: Seconds to wait before failing the test.

    Raises:
        AssertionError: When the condition does not hold within the timeout.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:g} s waiting for {what}")
        time.sleep(POLL_SECONDS)


def finish(converter: Converter, key: str) -> ConvertStatus:
    """Start a conversion and wait for it to end.

    Args:
        converter: The converter under test.
        key: The key to convert.

    Returns:
        The final status, whose state is "done" or "failed".
    """
    converter.start(key)
    wait_for(lambda: converter.status(key).state in ("done", "failed"), f"{key} to finish")
    return converter.status(key)


def stream_facts(ffprobe: str, path: Path, as_mp4: bool = True) -> Dict[str, Any]:
    """Probe a file with a plain ffprobe call that shares no code with the module under test.

    Args:
        ffprobe: Path of the ffprobe program.
        path: The media file to read.
        as_mp4: True reads the file as MP4 only, so any other container raises,
            and turns duration into a float and nb_frames into an int. False
            lets ffprobe detect the container and returns its text values.

    Returns:
        The first video stream's codec_name, width, height, pix_fmt,
        r_frame_rate, field_order (absent when ffprobe names none), duration,
        and nb_frames.
    """
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=codec_name,width,height,pix_fmt,r_frame_rate,field_order,duration,nb_frames", "-of", "json"]
    if as_mp4:
        command += ["-f", "mp4"]
    result = subprocess.run(command + ["-i", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    stream = json.loads(result.stdout)["streams"][0]
    if as_mp4:
        stream["duration"] = float(stream["duration"])
        stream["nb_frames"] = int(stream["nb_frames"])
    return stream


def decodes_cleanly(ffmpeg: str, path: Path) -> bool:
    """Decode every frame of a file, as a player would.

    Args:
        ffmpeg: Path of the ffmpeg program.
        path: The media file to decode.

    Returns:
        True when ffmpeg exits with code 0 and reports no error.
    """
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.returncode == 0 and result.stderr == b""


def top_level_atoms(path: Path) -> List[str]:
    """List the top-level MP4 atoms of a file in file order.

    Args:
        path: An MP4 file.

    Returns:
        Atom names such as ["ftyp", "moov", "free", "mdat"].
    """
    atoms = []
    size_on_disk = path.stat().st_size
    with open(path, "rb") as handle:
        offset = 0
        while offset + 8 <= size_on_disk:
            handle.seek(offset)
            length, name = struct.unpack(">I4s", handle.read(8))
            if length == 1:
                length = struct.unpack(">Q", handle.read(8))[0]
            elif length == 0:
                length = size_on_disk - offset
            atoms.append(name.decode("latin-1"))
            if length < 8:
                break
            offset += length
    return atoms


def processes_mentioning(text: str) -> List[str]:
    """Return the process IDs whose command line contains a piece of text.

    Args:
        text: Text to look for, such as the path of a ``.part`` file.

    Returns:
        A list of process ID strings, empty when no process matches.
    """
    result = subprocess.run(["pgrep", "-f", re.escape(text)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.decode().split()


def part_files(cache_root: Path) -> List[Path]:
    """Return every unfinished ``.part`` file under a cache root."""
    return sorted((cache_root / "converted").glob("*.part"))


def converted_folder_names(cache_root: Path) -> List[str]:
    """Return the sorted file names inside ``<cache_root>/converted``."""
    return sorted(path.name for path in (cache_root / "converted").iterdir())


def make_clip(ffmpeg: str, out_path: Path, source: str, codec_args: List[str]) -> Path:
    """Render one lavfi test clip.

    Args:
        ffmpeg: Path of the ffmpeg program.
        out_path: Where to write the clip.
        source: The lavfi source text, such as "testsrc=size=320x240:rate=30:duration=2".
        codec_args: Encoder and container arguments.

    Returns:
        out_path.

    Raises:
        AssertionError: When ffmpeg fails, with its stderr.
    """
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source] + codec_args + [str(out_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, f"could not make {out_path.name}: {result.stderr.decode()}"
    return out_path


def find_tool(name: str) -> str:
    """Locate ffmpeg or ffprobe on PATH or in the Homebrew folder.

    Args:
        name: "ffmpeg" or "ffprobe".

    Returns:
        The full path of the program. The test session fails when it is missing.
    """
    found = shutil.which(name)
    if found:
        return found
    candidate = HOMEBREW_BIN / name
    if candidate.is_file():
        return str(candidate)
    pytest.fail(f"{name} is not installed; the conversion tests need it (brew install ffmpeg)")


@pytest.fixture(scope="session")
def tools() -> Dict[str, str]:
    """Return the paths of the real ffmpeg and ffprobe programs."""
    return {"ffmpeg": find_tool("ffmpeg"), "ffprobe": find_tool("ffprobe")}


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory, tools: Dict[str, str]) -> Dict[str, Path]:
    """Make the sample clips once per session.

    Returns:
        Paths keyed by name: "h264_ts" (progressive H.264 in MPEG-TS),
        "mpeg2_ts" (interlaced MPEG-2 in MPEG-TS), "dv_avi" (DV in AVI at
        720x480, 30000/1001 fps, yuv411p), "h264i_ts" (interlaced H.264 in
        MPEG-TS, the usual AVCHD camcorder mode), "odd_ts" (MPEG-2 at 321x241,
        a size libx264 refuses), "long_ts" (40 seconds of interlaced 1280x720
        MPEG-2, long enough to watch a job run), "audio_ts" (sound only),
        "mkv" (MPEG-2 in Matroska, whose stream carries no duration, like the
        archive's DV files), "raw_h264" (a bare H.264 stream with no duration
        anywhere), "h264_422_10_ts" (progressive H.264 in 4:2:2 10-bit, the
        format of the 2024 MXF cameras, which Chrome cannot decode), and
        "h264_j420_ts" (full-range 4:2:0 H.264, what small cameras write).
    """
    folder = tmp_path_factory.mktemp("clips")
    ffmpeg = tools["ffmpeg"]
    interlaced = ["-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-top", "1", "-pix_fmt", "yuv420p"]
    clips = {
        "h264_ts": make_clip(ffmpeg, folder / "h264.ts", "testsrc=size=320x240:rate=30:duration=2",
                             ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "mpegts"]),
        "mpeg2_ts": make_clip(ffmpeg, folder / "mpeg2.ts", "testsrc=size=320x240:rate=30:duration=2",
                              interlaced + ["-b:v", "2M", "-f", "mpegts"]),
        "dv_avi": make_clip(ffmpeg, folder / "dv.avi", "testsrc=size=720x480:rate=30000/1001:duration=2",
                            ["-c:v", "dvvideo", "-pix_fmt", "yuv411p", "-f", "avi"]),
        "h264i_ts": make_clip(ffmpeg, folder / "h264i.ts", "testsrc=size=320x240:rate=30:duration=2",
                              ["-c:v", "libx264", "-preset", "ultrafast", "-flags", "+ildct+ilme",
                               "-x264-params", "tff=1", "-pix_fmt", "yuv420p", "-f", "mpegts"]),
        "odd_ts": make_clip(ffmpeg, folder / "odd.ts", "testsrc=size=321x241:rate=30:duration=2",
                            ["-c:v", "mpeg2video", "-pix_fmt", "yuv420p", "-b:v", "2M", "-f", "mpegts"]),
        "long_ts": make_clip(ffmpeg, folder / "long.ts", "testsrc=size=1280x720:rate=30:duration=40",
                             interlaced + ["-b:v", "6M", "-f", "mpegts"]),
        "audio_ts": make_clip(ffmpeg, folder / "audio.ts", "sine=frequency=440:duration=1",
                              ["-c:a", "mp2", "-f", "mpegts"]),
        "mkv": make_clip(ffmpeg, folder / "stream_without_duration.mkv", "testsrc=size=320x240:rate=30:duration=2",
                         ["-c:v", "mpeg2video", "-pix_fmt", "yuv420p", "-b:v", "2M", "-f", "matroska"]),
        "raw_h264": make_clip(ffmpeg, folder / "raw.h264", "testsrc=size=320x240:rate=30:duration=2",
                              ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "h264"]),
        "h264_422_10_ts": make_clip(ffmpeg, folder / "h264_422_10.ts", "testsrc=size=320x240:rate=30:duration=2",
                                    ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv422p10le",
                                     "-profile:v", "high422", "-f", "mpegts"]),
        "h264_j420_ts": make_clip(ffmpeg, folder / "h264_j420.ts", "testsrc=size=320x240:rate=30:duration=2",
                                  ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuvj420p", "-f", "mpegts"]),
    }
    return clips


@pytest.fixture
def make_converter(tmp_path: Path, tools: Dict[str, str]):
    """Build converters that read sample files, and close them after the test.

    Returns:
        A function ``build(sources, cache_root=None, ffmpeg=None, ffprobe=None)``
        where ``sources`` maps a key to the path or URL ffmpeg should read.
    """
    made: List[Converter] = []

    def build(sources: Dict[str, Any], cache_root: Optional[Path] = None,
              ffmpeg: Optional[str] = None, ffprobe: Optional[str] = None) -> Converter:
        """Create one converter over a key-to-source mapping."""
        converter = Converter(
            cache_root if cache_root is not None else tmp_path / "cache",
            lambda key: str(sources[key]),
            ffmpeg=ffmpeg or tools["ffmpeg"],
            ffprobe=ffprobe or tools["ffprobe"],
        )
        made.append(converter)
        return converter

    yield build
    for converter in made:
        converter.close()


@pytest.fixture
def fake_ffmpeg(tmp_path: Path):
    """Write a scripted stand-in for ffmpeg.

    Returns:
        A function ``build(**control)`` that returns the stand-in's path. The
        control values are: encoders (text for ``-encoders``), progress (lines
        to print), progress_pause (seconds after each line), stderr (text),
        hold (seconds to sleep), wait_for (a path to wait for), max_wait,
        ignore_terminate, exit_code, and log (a path for start and end events).
    """
    counter = [0]

    def build(**control: Any) -> str:
        """Create one stand-in with its own control file."""
        counter[0] += 1
        stem = tmp_path / f"fake_ffmpeg_{counter[0]}"
        stem.with_suffix(".py").write_text(FAKE_TOOL_SOURCE)
        stem.with_suffix(".json").write_text(json.dumps(control))
        launcher = stem.with_suffix(".sh")
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stem.with_suffix(".py")}" "$@"\n')
        launcher.chmod(0o755)
        return str(launcher)

    return build


class RangeServer:
    """Loopback HTTP server that serves one file with Range support, like the app's /video route."""

    def __init__(self, path: Path, cut_at: Optional[int] = None) -> None:
        """Prepare the server.

        Args:
            path: The file to serve at every URL.
            cut_at: When set, every answer stops at this file offset and the
                connection closes, as it would after a lost upstream connection.
        """
        body = path.read_bytes()
        ranges: List[Optional[str]] = []
        paths: List[str] = []
        self.ranges = ranges
        self.paths = paths

        class Handler(http.server.BaseHTTPRequestHandler):
            """Answers GET with the whole file or the requested byte range."""

            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                """Keep the test output quiet."""

            def do_GET(self) -> None:
                """Send the file or a byte range of it, stopping early at cut_at."""
                header = self.headers.get("Range")
                ranges.append(header)
                paths.append(self.path)
                start, end, status = 0, len(body) - 1, 200
                if header:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", header.strip())
                    start = int(match.group(1))
                    end = min(int(match.group(2)), end) if match.group(2) else end
                    status = 206
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(status)
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                stop = end + 1 if cut_at is None else min(end + 1, max(cut_at, start))
                try:
                    self.wfile.write(body[start:stop])
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                    return
                if stop < end + 1:
                    self.close_connection = True

        class QuietServer(http.server.ThreadingHTTPServer):
            """Threading server that does not print a traceback when ffmpeg hangs up."""

            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                """Ignore errors from clients that closed the connection."""

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> str:
        """Start serving and return the base URL, such as ``http://127.0.0.1:50123``."""
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        """Stop serving and free the port."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


@pytest.fixture
def range_server():
    """Start loopback file servers and stop them after the test.

    Returns:
        A function ``serve(path, cut_at=None)`` that returns ``(base_url, server)``.
    """
    servers: List[RangeServer] = []

    def serve(path: Path, cut_at: Optional[int] = None):
        """Serve one file and return its base URL with the server object."""
        server = RangeServer(path, cut_at)
        servers.append(server)
        return server.start(), server

    yield serve
    for server in servers:
        server.stop()


def value_after(args: List[str], flag: str) -> str:
    """Return the argument that follows a flag in an ffmpeg command line."""
    return args[args.index(flag) + 1]


def info(codec: str, height: int, field_order: str, width: int = 1920, pix_fmt: str = "yuv420p") -> Dict[str, Any]:
    """Build a probe result for the argument tests."""
    return {"codec": codec, "width": width, "height": height, "field_order": field_order,
            "pix_fmt": pix_fmt, "duration": 240.0}


# ---------------------------------------------------------------- samples


def test_samples_have_the_properties_the_tests_rely_on(tools, samples):
    """A plain ffprobe call confirms the samples: the MPEG-2 clip really is interlaced, the DV clip really is NTSC DV."""
    mpeg2 = stream_facts(tools["ffprobe"], samples["mpeg2_ts"], as_mp4=False)
    assert (mpeg2["codec_name"], mpeg2["field_order"]) == ("mpeg2video", "tt")
    long_clip = stream_facts(tools["ffprobe"], samples["long_ts"], as_mp4=False)
    assert (long_clip["codec_name"], long_clip["field_order"], long_clip["height"]) == ("mpeg2video", "tt", 720)
    h264 = stream_facts(tools["ffprobe"], samples["h264_ts"], as_mp4=False)
    assert (h264["codec_name"], h264["field_order"]) == ("h264", "progressive")
    interlaced_h264 = stream_facts(tools["ffprobe"], samples["h264i_ts"], as_mp4=False)
    assert (interlaced_h264["codec_name"], interlaced_h264["field_order"]) == ("h264", "tt")
    dv = stream_facts(tools["ffprobe"], samples["dv_avi"], as_mp4=False)
    assert (dv["codec_name"], dv["width"], dv["height"]) == ("dvvideo", 720, 480)
    assert (dv["pix_fmt"], dv["r_frame_rate"]) == ("yuv411p", "30000/1001")
    ten_bit = stream_facts(tools["ffprobe"], samples["h264_422_10_ts"], as_mp4=False)
    assert (ten_bit["codec_name"], ten_bit["pix_fmt"]) == ("h264", "yuv422p10le")
    assert ten_bit["field_order"] == "progressive"
    full_range = stream_facts(tools["ffprobe"], samples["h264_j420_ts"], as_mp4=False)
    assert (full_range["codec_name"], full_range["pix_fmt"]) == ("h264", "yuvj420p")
    assert full_range["field_order"] == "progressive"


def test_dv_sample_needs_the_frame_flag_fallback(tools, samples):
    """ffprobe names no field order for DV streams, in this sample as in the archive's DV files."""
    dv = stream_facts(tools["ffprobe"], samples["dv_avi"], as_mp4=False)
    assert "field_order" not in dv, (
        "ffprobe now names a field order for DV, so this sample no longer exercises "
        "the frame-flag fallback in screener.convert.probe"
    )


# ---------------------------------------------------------------- probe


@pytest.mark.parametrize(
    "name, codec, width, height, field_order, pix_fmt, duration",
    [
        ("h264_ts", "h264", 320, 240, "progressive", "yuv420p", 2.0),
        ("mpeg2_ts", "mpeg2video", 320, 240, "tt", "yuv420p", 2.0),
        ("dv_avi", "dvvideo", 720, 480, "bb", "yuv411p", 2.002),
        ("h264i_ts", "h264", 320, 240, "tt", "yuv420p", 2.0),
        ("h264_422_10_ts", "h264", 320, 240, "progressive", "yuv422p10le", 2.0),
        ("h264_j420_ts", "h264", 320, 240, "progressive", "yuvj420p", 2.0),
    ],
)
def test_probe_reports_codec_size_field_order_duration(
    tools, samples, name, codec, width, height, field_order, pix_fmt, duration
):
    """probe returns exactly the six planned fields with the right values for each sample format."""
    result = probe(tools["ffprobe"], str(samples[name]))
    assert set(result) == {"codec", "width", "height", "field_order", "pix_fmt", "duration"}
    assert result["codec"] == codec
    assert (result["width"], result["height"]) == (width, height)
    assert isinstance(result["width"], int) and isinstance(result["height"], int)
    assert result["field_order"] == field_order
    assert result["pix_fmt"] == pix_fmt
    assert isinstance(result["pix_fmt"], str)
    assert isinstance(result["duration"], float)
    assert result["duration"] == pytest.approx(duration, abs=0.05)


def test_probe_uses_container_duration_when_the_stream_has_none(tools, samples):
    """The archive's DV files give a duration only for the container, so probe falls back to it."""
    result = probe(tools["ffprobe"], str(samples["mkv"]))
    assert result["codec"] == "mpeg2video"
    assert result["duration"] == pytest.approx(2.0, abs=0.05)


def test_probe_reports_zero_duration_when_the_file_does_not_say(tools, samples):
    """A source with no duration anywhere probes as 0.0 instead of failing."""
    result = probe(tools["ffprobe"], str(samples["raw_h264"]))
    assert result["codec"] == "h264"
    assert result["duration"] == 0.0


def test_probe_missing_file_raises(tools, tmp_path):
    """A missing source raises ConvertError that names the source and carries ffprobe's reason."""
    missing = tmp_path / "nowhere" / "gone.mts"
    with pytest.raises(ConvertError) as caught:
        probe(tools["ffprobe"], str(missing))
    assert str(missing) in str(caught.value)
    assert "No such file" in str(caught.value)


def test_probe_rejects_file_without_video(tools, samples):
    """A file that holds only sound raises ConvertError instead of returning blanks."""
    with pytest.raises(ConvertError, match="no video stream"):
        probe(tools["ffprobe"], str(samples["audio_ts"]))


def test_probe_rejects_garbage_file(tools, tmp_path):
    """A text file with a video extension raises ConvertError."""
    garbage = tmp_path / "notes.mts"
    garbage.write_text("this is not a video\n" * 500)
    with pytest.raises(ConvertError) as caught:
        probe(tools["ffprobe"], str(garbage))
    assert str(garbage) in str(caught.value)


def test_probe_source_starting_with_dash_is_read_as_a_name_not_an_option(tools):
    """A source that looks like an option cannot change what ffprobe does."""
    with pytest.raises(ConvertError, match="No such file"):
        probe(tools["ffprobe"], "-version")


@pytest.mark.parametrize("source", ["", None, 17])
def test_probe_rejects_empty_or_non_text_source(tools, source):
    """The source must be non-empty text."""
    with pytest.raises(ConvertError, match="source"):
        probe(tools["ffprobe"], source)


def test_probe_missing_program_raises(samples):
    """A wrong ffprobe path raises ConvertError that names the program."""
    with pytest.raises(ConvertError, match="/nonexistent/ffprobe"):
        probe("/nonexistent/ffprobe", str(samples["h264_ts"]))


def test_probe_timeout_raises(tmp_path, samples):
    """An ffprobe that never answers is stopped at the timeout, even when it leaves a child holding its output."""
    slow = tmp_path / "slow_ffprobe.sh"
    slow.write_text("#!/bin/sh\nsleep 15\n")
    slow.chmod(0o755)
    began = time.monotonic()
    with pytest.raises(ConvertError, match="0.3 seconds"):
        probe(str(slow), str(samples["h264_ts"]), timeout=0.3)
    assert time.monotonic() - began < 10


# ---------------------------------------------------------------- pick_encoder


def test_pick_encoder_prefers_hardware_and_falls_back(fake_ffmpeg, tools):
    """The hardware encoder wins when ffmpeg lists it as an encoder name, and libx264 is the fallback."""
    assert pick_encoder(fake_ffmpeg(encoders=ENCODERS_WITH_HARDWARE)) == "h264_videotoolbox"
    assert pick_encoder(fake_ffmpeg(encoders=ENCODERS_WITHOUT_HARDWARE)) == "libx264"
    assert pick_encoder(tools["ffmpeg"]) in ("h264_videotoolbox", "libx264")


def test_pick_encoder_missing_program_raises():
    """A wrong ffmpeg path raises ConvertError that names the program."""
    with pytest.raises(ConvertError, match="/nonexistent/ffmpeg"):
        pick_encoder("/nonexistent/ffmpeg")


# ---------------------------------------------------------------- build_ffmpeg_args


def assert_common_shape(args: List[str], source: str, out_path: Path) -> None:
    """Check the parts every conversion command shares.

    Args:
        args: The command line under test.
        source: The expected input.
        out_path: The expected output path.
    """
    assert args[0] == "ffmpeg"
    assert value_after(args, "-i") == source
    assert args.count("-i") == 1
    for flag in ("-an", "-nostats", "-y"):
        assert flag in args
    assert value_after(args, "-movflags") == "+faststart"
    assert value_after(args, "-progress") == "pipe:1"
    assert value_after(args, "-f") == "mp4"
    assert args[-1] == str(out_path)
    assert all(isinstance(item, str) for item in args)
    assert "-s" not in args and not any("scale" in item for item in args)


@pytest.mark.parametrize("field_order", ["progressive", "unknown"])
def test_args_copy_for_progressive_h264(field_order):
    """H.264 that is progressive or of unknown field order is copied, not re-encoded."""
    out_path = Path("/tmp/cache/converted/abc.mp4.part")
    args = build_ffmpeg_args("ffmpeg", "/clips/a.mts", info("h264", 1080, field_order), out_path, "h264_videotoolbox")
    assert_common_shape(args, "/clips/a.mts", out_path)
    assert value_after(args, "-c:v") == "copy"
    for flag in ("-vf", "-b:v", "-pix_fmt", "-crf", "-preset"):
        assert flag not in args
    assert "h264_videotoolbox" not in args


@pytest.mark.parametrize("field_order", ["tt", "bb", "tb", "bt"])
def test_args_transcode_and_deinterlace_for_interlaced_mpeg2(field_order):
    """Interlaced MPEG-2 is re-encoded to yuv420p H.264 with yadif, and so is interlaced H.264."""
    out_path = Path("/tmp/cache/converted/abc.mp4.part")
    source = "http://127.0.0.1:8765/video?key=TCRMP_video_ondeck/2008/a%2Bb.m2t"
    for codec in ("mpeg2video", "h264"):
        args = build_ffmpeg_args("ffmpeg", source, info(codec, 1080, field_order), out_path, "h264_videotoolbox")
        assert_common_shape(args, source, out_path)
        assert value_after(args, "-c:v") == "h264_videotoolbox"
        assert value_after(args, "-pix_fmt") == "yuv420p"
        assert value_after(args, "-vf") == "yadif"
        assert value_after(args, "-b:v") == "25M"


@pytest.mark.parametrize("field_order", ["progressive", "unknown"])
def test_args_transcode_for_422_10bit_h264(field_order):
    """H.264 in 4:2:2 10-bit (the 2024 MXF cameras) is re-encoded to yuv420p, without yadif, because Chrome cannot decode it."""
    out_path = Path("/tmp/cache/converted/abc.mp4.part")
    source = "http://127.0.0.1:8765/video?key=TCRMP_video_ondeck/2024/a.MXF"
    ten_bit = info("h264", 2160, field_order, pix_fmt="yuv422p10le")
    args = build_ffmpeg_args("ffmpeg", source, ten_bit, out_path, "h264_videotoolbox")
    assert_common_shape(args, source, out_path)
    assert value_after(args, "-c:v") == "h264_videotoolbox"
    assert value_after(args, "-pix_fmt") == "yuv420p"
    assert value_after(args, "-b:v") == "25M"
    assert "-vf" not in args
    assert "copy" not in args


def test_args_copy_for_yuvj420p():
    """Full-range 8-bit 4:2:0 H.264 (yuvj420p, what small cameras write) plays in Chrome, so it is copied."""
    out_path = Path("/tmp/cache/converted/abc.mp4.part")
    full_range = info("h264", 720, "progressive", pix_fmt="yuvj420p")
    args = build_ffmpeg_args("ffmpeg", "/clips/MVI_0203.MOV", full_range, out_path, "h264_videotoolbox")
    assert_common_shape(args, "/clips/MVI_0203.MOV", out_path)
    assert value_after(args, "-c:v") == "copy"
    for flag in ("-vf", "-b:v", "-pix_fmt", "-crf", "-preset"):
        assert flag not in args


def test_args_transcode_for_unknown_pix_fmt():
    """H.264 whose pixel format ffprobe could not name is re-encoded instead of trusted to play."""
    out_path = Path("/tmp/cache/converted/abc.mp4.part")
    args = build_ffmpeg_args("ffmpeg", "/clips/a.mts", info("h264", 1080, "progressive", pix_fmt=""), out_path, "libx264")
    assert_common_shape(args, "/clips/a.mts", out_path)
    assert value_after(args, "-c:v") == "libx264"
    assert value_after(args, "-pix_fmt") == "yuv420p"
    assert value_after(args, "-crf") == "18"
    assert "-vf" not in args


@pytest.mark.parametrize("pix_fmt", ["yuv422p", "yuv422p10le", "yuv444p", "yuv420p10le", "gray"])
def test_args_transcode_for_every_pixel_format_outside_8bit_420(pix_fmt):
    """Every H.264 pixel format outside 8-bit 4:2:0 is re-encoded, and deinterlaced only when the field order says so."""
    out_path = Path("/tmp/o.mp4.part")
    progressive_info = info("h264", 1080, "progressive", pix_fmt=pix_fmt)
    progressive = build_ffmpeg_args("ffmpeg", "/clips/a.mts", progressive_info, out_path, "h264_videotoolbox")
    assert value_after(progressive, "-c:v") == "h264_videotoolbox"
    assert value_after(progressive, "-pix_fmt") == "yuv420p"
    assert value_after(progressive, "-b:v") == "25M"
    assert "-vf" not in progressive
    interlaced_info = info("h264", 1080, "bt", pix_fmt=pix_fmt)
    interlaced = build_ffmpeg_args("ffmpeg", "/clips/a.mts", interlaced_info, out_path, "h264_videotoolbox")
    assert value_after(interlaced, "-c:v") == "h264_videotoolbox"
    assert value_after(interlaced, "-pix_fmt") == "yuv420p"
    assert value_after(interlaced, "-vf") == "yadif"


def test_args_progressive_non_h264_is_transcoded_without_yadif():
    """A progressive source in another codec is re-encoded and left without a deinterlace filter."""
    out_path = Path("/tmp/cache/converted/abc.mp4.part")
    for field_order in ("progressive", "unknown"):
        args = build_ffmpeg_args("ffmpeg", "/clips/a.wmv", info("wmv3", 480, field_order), out_path, "h264_videotoolbox")
        assert value_after(args, "-c:v") == "h264_videotoolbox"
        assert value_after(args, "-pix_fmt") == "yuv420p"
        assert "-vf" not in args


@pytest.mark.parametrize(
    "height, bitrate",
    [(2160, "25M"), (1080, "25M"), (720, "25M"), (719, "8M"), (480, "8M"), (240, "8M")],
)
def test_args_bitrate_by_height(height, bitrate):
    """720 rows and above get 25M, anything smaller gets 8M."""
    out_path = Path("/tmp/o.mp4.part")
    args = build_ffmpeg_args("ffmpeg", "/clips/a.m2t", info("mpeg2video", height, "tt"), out_path, "h264_videotoolbox")
    assert value_after(args, "-b:v") == bitrate


@pytest.mark.parametrize("height", [1080, 480])
def test_args_libx264_fallback(height):
    """libx264 uses preset veryfast and crf 18 in place of a bitrate, at every height."""
    out_path = Path("/tmp/o.mp4.part")
    args = build_ffmpeg_args("ffmpeg", "/clips/a.avi", info("dvvideo", height, "bb"), out_path, "libx264")
    assert_common_shape(args, "/clips/a.avi", out_path)
    assert value_after(args, "-c:v") == "libx264"
    assert value_after(args, "-preset") == "veryfast"
    assert value_after(args, "-crf") == "18"
    assert value_after(args, "-pix_fmt") == "yuv420p"
    assert value_after(args, "-vf") == "yadif"
    assert "-b:v" not in args


def test_args_map_only_the_probed_video_stream():
    """The command maps the first real video stream, so sound, subtitles, and data never reach the MP4."""
    args = build_ffmpeg_args("ffmpeg", "/clips/a.mts", info("h264", 1080, "tt"), Path("/tmp/o.mp4.part"), "libx264")
    assert value_after(args, "-map") == "0:V:0"
    assert args.count("-map") == 1


@pytest.mark.parametrize(
    "bad_info, word",
    [
        ({"width": 1920, "height": 1080, "field_order": "tt"}, "codec"),
        ({"codec": "h264", "width": 1920, "field_order": "tt"}, "height"),
        ({"codec": "h264", "width": 1920, "height": "tall", "field_order": "tt"}, "height"),
        ({"codec": "h264", "width": 1920, "height": 0, "field_order": "tt"}, "height"),
        ({"codec": "h264", "width": 1920, "height": 1080}, "field_order"),
        ({"codec": "h264", "width": 1920, "height": 1080, "field_order": "tt"}, "pix_fmt"),
        ({"codec": "h264", "width": 1920, "height": 1080, "field_order": "tt", "pix_fmt": None}, "pix_fmt"),
    ],
)
def test_args_reject_incomplete_probe_result(bad_info, word):
    """A probe result missing the codec, a usable height, the field order, or the pixel format raises ConvertError naming it."""
    with pytest.raises(ConvertError, match=word):
        build_ffmpeg_args("ffmpeg", "/clips/a.mts", bad_info, Path("/tmp/o.mp4.part"), "libx264")


# ---------------------------------------------------------------- real conversions


def test_convert_h264_ts_to_playable_mp4(make_converter, samples, tools, tmp_path):
    """Progressive H.264 in MPEG-TS is stream-copied into a faststart MP4 with a record beside it."""
    converter = make_converter({KEY_H264: samples["h264_ts"]})
    assert converter.converted_path(KEY_H264) is None
    status = finish(converter, KEY_H264)
    assert status == ConvertStatus("done", 1.0, "")

    out_path = converter.converted_path(KEY_H264)
    assert out_path == tmp_path / "cache" / "converted" / (cache_id(KEY_H264) + ".mp4")
    facts = stream_facts(tools["ffprobe"], out_path)
    assert facts["codec_name"] == "h264"
    assert facts["pix_fmt"] == "yuv420p"
    assert (facts["width"], facts["height"]) == (320, 240)
    assert abs(facts["duration"] - 2.0) <= DURATION_TOLERANCE
    assert facts["nb_frames"] == 60
    assert decodes_cleanly(tools["ffmpeg"], out_path)
    atoms = top_level_atoms(out_path)
    assert atoms[0] == "ftyp" and atoms.index("moov") < atoms.index("mdat")

    record = json.loads((out_path.parent / (cache_id(KEY_H264) + ".json")).read_text())
    assert set(record) == {"key", "probe", "args"}
    assert record["key"] == KEY_H264
    assert record["probe"] == probe(tools["ffprobe"], str(samples["h264_ts"]))
    assert value_after(record["args"], "-c:v") == "copy"
    assert value_after(record["args"], "-i") == str(samples["h264_ts"])
    assert converted_folder_names(tmp_path / "cache") == [cache_id(KEY_H264) + ".json", cache_id(KEY_H264) + ".mp4"]


def test_convert_mpeg2_interlaced(make_converter, samples, tools, tmp_path):
    """Interlaced MPEG-2 is re-encoded to progressive yuv420p H.264 at the same size and length."""
    converter = make_converter({KEY_MPEG2: samples["mpeg2_ts"]})
    status = finish(converter, KEY_MPEG2)
    assert status.state == "done", status.message

    out_path = converter.converted_path(KEY_MPEG2)
    facts = stream_facts(tools["ffprobe"], out_path)
    assert facts["codec_name"] == "h264"
    assert facts["pix_fmt"] == "yuv420p"
    assert facts["field_order"] == "progressive"
    assert (facts["width"], facts["height"]) == (320, 240)
    assert abs(facts["duration"] - 2.0) <= DURATION_TOLERANCE
    assert facts["nb_frames"] == 60
    assert decodes_cleanly(tools["ffmpeg"], out_path)
    assert top_level_atoms(out_path).index("moov") < top_level_atoms(out_path).index("mdat")

    record = json.loads(out_path.with_suffix(".json").read_text())
    assert record["probe"]["field_order"] == "tt"
    assert value_after(record["args"], "-c:v") == pick_encoder(tools["ffmpeg"])
    assert value_after(record["args"], "-vf") == "yadif"
    assert value_after(record["args"], "-b:v") == "8M"
    assert part_files(tmp_path / "cache") == []


def test_convert_dv_avi(make_converter, samples, tools, tmp_path):
    """DV in AVI (720x480, 29.97 fps, yuv411p) becomes yuv420p H.264 at 720x480, deinterlaced."""
    converter = make_converter({KEY_DV: samples["dv_avi"]})
    status = finish(converter, KEY_DV)
    assert status.state == "done", status.message

    out_path = converter.converted_path(KEY_DV)
    facts = stream_facts(tools["ffprobe"], out_path)
    assert facts["codec_name"] == "h264"
    assert facts["pix_fmt"] == "yuv420p"
    assert (facts["width"], facts["height"]) == (720, 480)
    assert abs(facts["duration"] - 2.002) <= DURATION_TOLERANCE
    assert facts["nb_frames"] == 60
    assert decodes_cleanly(tools["ffmpeg"], out_path)
    assert top_level_atoms(out_path).index("moov") < top_level_atoms(out_path).index("mdat")

    record = json.loads(out_path.with_suffix(".json").read_text())
    assert record["probe"]["codec"] == "dvvideo"
    assert record["probe"]["field_order"] == "bb"
    assert value_after(record["args"], "-c:v") == pick_encoder(tools["ffmpeg"])
    assert value_after(record["args"], "-vf") == "yadif"
    assert part_files(tmp_path / "cache") == []


def test_convert_interlaced_h264_is_re_encoded_not_copied(make_converter, samples, tools):
    """Interlaced H.264, the usual AVCHD mode, is deinterlaced and re-encoded instead of copied."""
    converter = make_converter({KEY_INTERLACED_H264: samples["h264i_ts"]})
    status = finish(converter, KEY_INTERLACED_H264)
    assert status.state == "done", status.message

    out_path = converter.converted_path(KEY_INTERLACED_H264)
    facts = stream_facts(tools["ffprobe"], out_path)
    assert facts["codec_name"] == "h264"
    assert facts["field_order"] == "progressive"
    assert (facts["width"], facts["height"]) == (320, 240)
    assert abs(facts["duration"] - 2.0) <= DURATION_TOLERANCE
    assert facts["nb_frames"] == 60
    record = json.loads(out_path.with_suffix(".json").read_text())
    assert record["probe"]["field_order"] == "tt"
    assert value_after(record["args"], "-c:v") == pick_encoder(tools["ffmpeg"])
    assert value_after(record["args"], "-vf") == "yadif"


def test_source_without_a_duration_still_converts(make_converter, samples, tools):
    """With no duration to measure against, progress stays at 0 while ffmpeg prints N/A, then the job ends done at 1."""
    converter = make_converter({KEY_THIRD: samples["raw_h264"]})
    seen = [converter.start(KEY_THIRD)]
    deadline = time.monotonic() + WAIT_SECONDS
    while seen[-1].state not in ("done", "failed") and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        seen.append(converter.status(KEY_THIRD))
    assert seen[-1] == ConvertStatus("done", 1.0, ""), seen[-1].message
    assert all(status.progress == 0.0 for status in seen[:-1])
    assert stream_facts(tools["ffprobe"], converter.converted_path(KEY_THIRD))["nb_frames"] == 60


def test_convert_422_10bit_h264_is_transcoded_to_playable_420(make_converter, samples, tools):
    """Progressive H.264 in 4:2:2 10-bit becomes 8-bit 4:2:0 H.264 at the source size, with every frame kept."""
    converter = make_converter({KEY_MXF: samples["h264_422_10_ts"]})
    status = finish(converter, KEY_MXF)
    assert status.state == "done", status.message

    out_path = converter.converted_path(KEY_MXF)
    facts = stream_facts(tools["ffprobe"], out_path)
    assert facts["codec_name"] == "h264"
    assert facts["pix_fmt"] == "yuv420p"
    assert facts["field_order"] == "progressive"
    assert (facts["width"], facts["height"]) == (320, 240)
    assert abs(facts["duration"] - 2.0) <= DURATION_TOLERANCE
    assert facts["nb_frames"] == 60
    assert decodes_cleanly(tools["ffmpeg"], out_path)
    record = json.loads(out_path.with_suffix(".json").read_text())
    assert record["probe"]["pix_fmt"] == "yuv422p10le"
    assert value_after(record["args"], "-c:v") == pick_encoder(tools["ffmpeg"])
    assert value_after(record["args"], "-pix_fmt") == "yuv420p"
    assert "-vf" not in record["args"]


def test_convert_yuvj420p_h264_is_copied(make_converter, samples, tools):
    """Full-range 4:2:0 H.264 is copied, so the output keeps yuvj420p and every frame."""
    converter = make_converter({KEY_MOV: samples["h264_j420_ts"]})
    status = finish(converter, KEY_MOV)
    assert status == ConvertStatus("done", 1.0, "")

    out_path = converter.converted_path(KEY_MOV)
    facts = stream_facts(tools["ffprobe"], out_path)
    assert (facts["codec_name"], facts["pix_fmt"]) == ("h264", "yuvj420p")
    assert (facts["width"], facts["height"]) == (320, 240)
    assert facts["nb_frames"] == 60
    record = json.loads(out_path.with_suffix(".json").read_text())
    assert record["probe"]["pix_fmt"] == "yuvj420p"
    assert value_after(record["args"], "-c:v") == "copy"


def test_convert_over_http_source(make_converter, samples, tools, range_server):
    """A local URL with a quoted key, as the app supplies, converts like a file and reaches the server unchanged."""
    base_url, server = range_server(samples["mpeg2_ts"])
    url = f"{base_url}/video?key={quote_key(KEY_SHELL)}"
    converter = make_converter({KEY_SHELL: url})
    status = finish(converter, KEY_SHELL)
    assert status.state == "done", status.message
    facts = stream_facts(tools["ffprobe"], converter.converted_path(KEY_SHELL))
    assert facts["codec_name"] == "h264"
    assert abs(facts["duration"] - 2.0) <= DURATION_TOLERANCE
    assert facts["nb_frames"] == 60
    assert len(server.paths) >= 2
    assert set(server.paths) == {"/video?key=" + quote_key(KEY_SHELL)}
    query = urllib.parse.urlsplit(server.paths[0]).query
    assert urllib.parse.parse_qs(query, keep_blank_values=True)["key"] == [KEY_SHELL]


def test_source_that_ends_early_is_failed_not_a_short_video(make_converter, samples, range_server, tmp_path):
    """When the source stream stops partway, ffmpeg still exits 0; the job must fail, not pass off a short file."""
    size = samples["long_ts"].stat().st_size
    base_url, _server = range_server(samples["long_ts"], cut_at=size * 6 // 10)
    converter = make_converter({KEY_LONG: f"{base_url}/video?key={quote_key(KEY_LONG)}"})
    status = finish(converter, KEY_LONG)
    assert status.state == "failed"
    assert "whole source" in status.message
    assert converter.converted_path(KEY_LONG) is None
    assert converted_folder_names(tmp_path / "cache") == []


def test_key_and_path_with_shell_characters_are_never_interpreted(make_converter, samples, tmp_path, monkeypatch):
    """Spaces, quotes, semicolons, and $(...) in a key or a path reach ffmpeg as plain text."""
    monkeypatch.chdir(tmp_path)
    hostile_folder = tmp_path / "clips a\u00f1o $(touch pwned); echo 'x' \"y\" &"
    hostile_folder.mkdir()
    source = hostile_folder / "a b+c;$(touch pwned).ts"
    shutil.copyfile(samples["h264_ts"], source)
    converter = make_converter({KEY_SHELL: source})
    status = finish(converter, KEY_SHELL)
    assert status.state == "done", status.message
    assert converter.converted_path(KEY_SHELL).name == cache_id(KEY_SHELL) + ".mp4"
    assert not (tmp_path / "pwned").exists()
    assert not list(tmp_path.rglob("pwned"))


def test_tools_are_found_in_the_homebrew_folder_when_path_lacks_them(tmp_path, samples, tools, monkeypatch):
    """With the default program names and a PATH without ffmpeg, the converter looks in the fallback folders."""
    monkeypatch.setattr(convert, "FALLBACK_TOOL_FOLDERS", (str(Path(tools["ffmpeg"]).parent),))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    converter = Converter(tmp_path / "cache", lambda key: str(samples["h264_ts"]))
    try:
        status = finish(converter, KEY_H264)
    finally:
        converter.close()
    assert status.state == "done", status.message


# ---------------------------------------------------------------- status and idempotence


def test_status_progresses_to_done(make_converter, samples):
    """Status moves none, queued, running, done; progress never falls, passes through a middle value, and ends at 1."""
    converter = make_converter({KEY_LONG: samples["long_ts"]})
    assert converter.status(KEY_LONG) == ConvertStatus("none", 0.0, "")
    seen = [converter.start(KEY_LONG)]
    assert seen[0].state == "queued" and seen[0].progress == 0.0
    deadline = time.monotonic() + WAIT_SECONDS
    while seen[-1].state not in ("done", "failed") and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        seen.append(converter.status(KEY_LONG))

    assert seen[-1].state == "done", seen[-1].message
    order = {"queued": 0, "running": 1, "done": 2}
    ranks = [order[status.state] for status in seen]
    assert ranks == sorted(ranks)
    progress = [status.progress for status in seen]
    assert progress == sorted(progress)
    assert all(0.0 <= value <= 1.0 for value in progress)
    assert any(status.state == "running" and 0.0 < status.progress < 1.0 for status in seen)
    assert any(status.state == "running" and status.message for status in seen)
    assert seen[-1].progress == 1.0


@pytest.mark.parametrize(
    "sample, message",
    [
        ("h264_ts", "copying the H.264 video into an MP4 file"),
        ("h264_422_10_ts", "re-encoding h264 video (yuv422p10le is not playable in Chrome)"),
        ("h264i_ts", "re-encoding h264 video and deinterlacing"),
        ("mpeg2_ts", "re-encoding mpeg2video video to H.264 and deinterlacing"),
        ("dv_avi", "re-encoding dvvideo video to H.264 and deinterlacing"),
    ],
)
def test_running_message_says_what_ffmpeg_is_doing_and_why(
    make_converter, fake_ffmpeg, samples, tmp_path, sample, message
):
    """While ffmpeg runs, the status message names the branch, and a transcode forced by the pixel format says which one."""
    gate = tmp_path / "release"
    converter = make_converter({KEY_OTHER: samples[sample]}, ffmpeg=fake_ffmpeg(wait_for=str(gate)))
    converter.start(KEY_OTHER)
    wait_for(lambda: converter.status(KEY_OTHER).message == message, f"the message {message!r}")
    assert converter.status(KEY_OTHER).state == "running"
    gate.write_text("go")
    wait_for(lambda: converter.status(KEY_OTHER).state == "done", "the job to finish")
    assert converter.status(KEY_OTHER).message == ""


def test_progress_reads_out_time_ms_as_microseconds(make_converter, fake_ffmpeg, samples, tmp_path):
    """Older ffmpeg prints only out_time_ms, which holds microseconds; half a second of a 2 second clip is 0.25."""
    gate = tmp_path / "release"
    lines = ["frame=15", "out_time_ms=N/A", "out_time_ms=500000", "progress=continue"]
    ffmpeg = fake_ffmpeg(progress=lines, wait_for=str(gate))
    converter = make_converter({KEY_H264: samples["h264_ts"]}, ffmpeg=ffmpeg)
    converter.start(KEY_H264)
    wait_for(lambda: converter.status(KEY_H264).progress > 0.0, "the first progress value")
    status = converter.status(KEY_H264)
    assert status.state == "running"
    assert status.progress == pytest.approx(0.25, abs=0.01)
    gate.write_text("go")
    wait_for(lambda: converter.status(KEY_H264).state == "done", "the job to finish")
    assert converter.status(KEY_H264).progress == 1.0


def test_start_is_idempotent(make_converter, fake_ffmpeg, samples, tmp_path):
    """Repeated start calls never queue a key twice, and a finished key is never converted again."""
    gate = tmp_path / "release"
    log = tmp_path / "runs.log"
    ffmpeg = fake_ffmpeg(wait_for=str(gate), log=str(log))
    converter = make_converter({KEY_H264: samples["h264_ts"], KEY_OTHER: samples["mpeg2_ts"]}, ffmpeg=ffmpeg)

    assert converter.start(KEY_H264).state == "queued"
    wait_for(lambda: converter.status(KEY_H264).state == "running" and log.exists(), "the first job to run")
    assert converter.start(KEY_H264).state == "running"
    assert converter.start(KEY_OTHER).state == "queued"
    assert converter.start(KEY_OTHER).state == "queued"
    assert converter.status(KEY_OTHER).state == "queued"
    assert converter.start(KEY_H264).state == "running"

    gate.write_text("go")
    wait_for(lambda: converter.status(KEY_OTHER).state == "done", "both jobs to finish")
    assert converter.status(KEY_H264).state == "done"
    modified = converter.converted_path(KEY_H264).stat().st_mtime_ns
    assert converter.start(KEY_H264) == ConvertStatus("done", 1.0, "")
    assert converter.start(KEY_OTHER) == ConvertStatus("done", 1.0, "")
    time.sleep(0.2)
    starts = [line for line in log.read_text().splitlines() if line.startswith("start ")]
    assert len(starts) == 2
    assert converter.converted_path(KEY_H264).stat().st_mtime_ns == modified


def test_jobs_run_one_at_a_time_in_the_order_they_were_started(make_converter, fake_ffmpeg, samples, tmp_path):
    """Three queued keys give three ffmpeg runs that never overlap and follow the start order."""
    log = tmp_path / "runs.log"
    ffmpeg = fake_ffmpeg(hold=0.15, log=str(log))
    keys = [KEY_LONG, KEY_OTHER, KEY_THIRD]
    converter = make_converter({key: samples["h264_ts"] for key in keys}, ffmpeg=ffmpeg)
    for key in keys:
        converter.start(key)
    wait_for(lambda: all(converter.status(key).state == "done" for key in keys), "three jobs to finish")

    events = [line.split(" ") for line in log.read_text().splitlines()]
    events.sort(key=lambda event: float(event[-1]))
    assert [event[0] for event in events] == ["start", "end"] * 3
    started = [event[1] for event in events if event[0] == "start"]
    assert [Path(path).name for path in started] == [cache_id(key) + ".mp4.part" for key in keys]


def test_start_and_status_from_many_threads(make_converter, fake_ffmpeg, samples, tmp_path):
    """Sixteen threads calling start and status at once cause no error and exactly one run per key."""
    log = tmp_path / "runs.log"
    ffmpeg = fake_ffmpeg(hold=0.05, log=str(log))
    keys = [KEY_H264, KEY_MPEG2, KEY_DV, KEY_LONG]
    converter = make_converter({key: samples["h264_ts"] for key in keys}, ffmpeg=ffmpeg)
    errors: List[BaseException] = []
    barrier = threading.Barrier(16)

    def hammer(index: int) -> None:
        """Call start and status in a tight loop and record any exception."""
        try:
            barrier.wait()
            for turn in range(60):
                key = keys[(index + turn) % len(keys)]
                assert converter.start(key).state in ("queued", "running", "done")
                status = converter.status(key)
                assert status.state in ("queued", "running", "done")
                assert 0.0 <= status.progress <= 1.0
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=hammer, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    wait_for(lambda: all(converter.status(key).state == "done" for key in keys), "every key to finish")
    starts = [line for line in log.read_text().splitlines() if line.startswith("start ")]
    assert len(starts) == len(keys)


def test_status_returns_a_copy(make_converter, fake_ffmpeg, samples, tmp_path):
    """Changing a returned status cannot change what the converter reports next."""
    gate = tmp_path / "release"
    converter = make_converter({KEY_H264: samples["h264_ts"]}, ffmpeg=fake_ffmpeg(wait_for=str(gate)))
    converter.start(KEY_H264)
    wait_for(lambda: converter.status(KEY_H264).state == "running", "the job to run")
    status = converter.status(KEY_H264)
    status.state = "done"
    status.progress = 1.0
    assert converter.status(KEY_H264).state == "running"
    assert dataclasses.is_dataclass(status)
    gate.write_text("go")


# ---------------------------------------------------------------- failures


def test_failed_conversion_reports_stderr_and_cleans_part(make_converter, samples, monkeypatch, tmp_path):
    """A real ffmpeg failure (libx264 refuses 321x241) reports ffmpeg's own words and leaves no file behind."""
    monkeypatch.setattr(convert, "pick_encoder", lambda ffmpeg: "libx264")
    converter = make_converter({KEY_ODD: samples["odd_ts"]})
    status = finish(converter, KEY_ODD)
    assert status.state == "failed"
    assert "ffmpeg exited with code" in status.message
    assert "[" in status.message and "@" in status.message
    assert status.progress == 0.0
    assert converter.converted_path(KEY_ODD) is None
    assert converted_folder_names(tmp_path / "cache") == []


def test_failure_message_keeps_the_last_400_characters_of_stderr(make_converter, fake_ffmpeg, samples, tmp_path):
    """A long stderr is cut to its last 400 characters, and the stand-in's .part file is removed."""
    noise = "".join(f"line {number:05d} of early noise\n" for number in range(4000))
    ending = "x" * 350 + " the disk is full, says the stand-in"
    converter = make_converter({KEY_H264: samples["h264_ts"]}, ffmpeg=fake_ffmpeg(stderr=noise + ending, exit_code=3))
    status = finish(converter, KEY_H264)
    assert status.state == "failed"
    assert "code 3" in status.message
    assert status.message.endswith((noise + ending)[-400:])
    assert "line 00000" not in status.message
    assert converted_folder_names(tmp_path / "cache") == []


def test_stderr_flood_does_not_deadlock(make_converter, fake_ffmpeg, samples):
    """Two megabytes of stderr from a job that succeeds cannot block the worker."""
    flood = "warning: something noisy happened again and again\n" * 40000
    converter = make_converter({KEY_H264: samples["h264_ts"]}, ffmpeg=fake_ffmpeg(stderr=flood))
    status = finish(converter, KEY_H264)
    assert status.state == "done", status.message


def test_exit_zero_without_an_output_file_is_a_failure(make_converter, samples, tmp_path):
    """A program that exits 0 and writes nothing must not be reported as done."""
    silent = tmp_path / "silent_ffmpeg.sh"
    silent.write_text("#!/bin/sh\nexit 0\n")
    silent.chmod(0o755)
    converter = make_converter({KEY_H264: samples["h264_ts"]}, ffmpeg=str(silent))
    status = finish(converter, KEY_H264)
    assert status.state == "failed"
    assert "no output" in status.message
    assert converter.converted_path(KEY_H264) is None


def test_failed_can_restart(make_converter, samples, tmp_path):
    """A failed key starts again on the next start call and can then succeed."""
    late_source = tmp_path / "arrives_later.ts"
    converter = make_converter({KEY_H264: late_source})
    failed = finish(converter, KEY_H264)
    assert failed.state == "failed"
    assert str(late_source) in failed.message
    assert converter.status(KEY_H264).state == "failed"

    shutil.copyfile(samples["h264_ts"], late_source)
    assert converter.start(KEY_H264).state == "queued"
    wait_for(lambda: converter.status(KEY_H264).state in ("done", "failed"), "the second try to finish")
    assert converter.status(KEY_H264) == ConvertStatus("done", 1.0, "")
    assert converter.converted_path(KEY_H264).is_file()


def test_source_for_exception_becomes_a_failed_status_and_the_worker_survives(tmp_path, samples, tools):
    """An exception from source_for fails that key only; the next key still converts."""

    def source_for(key: str) -> str:
        """Fail for one key and serve a sample for every other key."""
        if key == KEY_OTHER:
            raise RuntimeError("relay is not ready")
        return str(samples["h264_ts"])

    converter = Converter(tmp_path / "cache", source_for, ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"])
    try:
        failed = finish(converter, KEY_OTHER)
        assert failed.state == "failed"
        assert "relay is not ready" in failed.message
        assert KEY_OTHER in failed.message
        assert finish(converter, KEY_H264).state == "done"
    finally:
        converter.close()


def test_missing_ffmpeg_program_is_reported(make_converter, samples):
    """A wrong ffmpeg path gives a failed status that names the program."""
    converter = make_converter({KEY_MPEG2: samples["mpeg2_ts"]}, ffmpeg="/nonexistent/ffmpeg")
    status = finish(converter, KEY_MPEG2)
    assert status.state == "failed"
    assert "/nonexistent/ffmpeg" in status.message


# ---------------------------------------------------------------- restart, keys, close


def test_finished_file_is_recognized_after_restart(make_converter, samples, tmp_path):
    """A new converter over the same cache reports done without any work, and clears stray .part files."""
    cache_root = tmp_path / "cache"
    first = make_converter({KEY_H264: samples["h264_ts"]}, cache_root=cache_root)
    assert finish(first, KEY_H264).state == "done"
    out_path = first.converted_path(KEY_H264)
    modified = out_path.stat().st_mtime_ns
    first.close()

    stray_video = cache_root / "converted" / "0123456789abcdef.mp4.part"
    stray_record = cache_root / "converted" / "0123456789abcdef.json.part"
    stray_video.write_bytes(b"half a video")
    stray_record.write_text("{")
    calls: List[str] = []

    def source_for(key: str) -> str:
        """Record that the converter asked for a source, which this test forbids."""
        calls.append(key)
        return str(samples["h264_ts"])

    second = Converter(cache_root, source_for)
    try:
        assert not stray_video.exists() and not stray_record.exists()
        assert second.status(KEY_H264) == ConvertStatus("done", 1.0, "")
        assert second.converted_path(KEY_H264) == out_path
        assert second.start(KEY_H264) == ConvertStatus("done", 1.0, "")
        assert second.status(KEY_MPEG2) == ConvertStatus("none", 0.0, "")
        assert second.converted_path(KEY_MPEG2) is None
        time.sleep(0.2)
        assert calls == []
        assert out_path.stat().st_mtime_ns == modified
    finally:
        second.close()


@pytest.mark.parametrize("key", HOSTILE_KEYS, ids=[repr(key)[:40] for key in HOSTILE_KEYS])
def test_hostile_key_rejected(key, tmp_path):
    """Every public method refuses a hostile key before any work, and nothing reaches source_for or the disk."""
    calls: List[Any] = []
    converter = Converter(tmp_path / "cache", lambda asked: calls.append(asked) or "/dev/null")
    try:
        for method in (converter.start, converter.status, converter.converted_path):
            with pytest.raises(InvalidKey):
                method(key)
        time.sleep(0.05)
        assert calls == []
        assert converted_folder_names(tmp_path / "cache") == []
    finally:
        converter.close()


def test_close_stops_running_job(make_converter, samples, tmp_path):
    """close ends a real ffmpeg in mid-conversion, removes the .part file, and leaves nothing marked done."""
    cache_root = tmp_path / "cache"
    converter = make_converter({KEY_LONG: samples["long_ts"], KEY_OTHER: samples["mpeg2_ts"]}, cache_root=cache_root)
    converter.start(KEY_LONG)
    converter.start(KEY_OTHER)
    part = cache_root / "converted" / (cache_id(KEY_LONG) + ".mp4.part")
    wait_for(lambda: converter.status(KEY_LONG).state == "running" and part.exists() and part.stat().st_size > 0,
             "ffmpeg to start writing")
    assert processes_mentioning(str(part)) != []

    began = time.monotonic()
    converter.close()
    assert time.monotonic() - began < 15
    assert processes_mentioning(str(part)) == []
    assert converted_folder_names(cache_root) == []
    assert converter.converted_path(KEY_LONG) is None
    stopped = converter.status(KEY_LONG)
    assert stopped.state == "failed"
    assert "closed" in stopped.message
    assert converter.status(KEY_OTHER).state == "none"
    converter.close()


def test_close_kills_a_job_that_ignores_terminate(make_converter, fake_ffmpeg, samples, tmp_path, monkeypatch):
    """A child that ignores the terminate signal is killed after the grace period."""
    monkeypatch.setattr(convert, "STOP_GRACE_SECONDS", 0.5)
    cache_root = tmp_path / "cache"
    ffmpeg = fake_ffmpeg(ignore_terminate=True, wait_for=str(tmp_path / "never"), max_wait=120.0)
    converter = make_converter({KEY_H264: samples["h264_ts"]}, cache_root=cache_root, ffmpeg=ffmpeg)
    converter.start(KEY_H264)
    part = cache_root / "converted" / (cache_id(KEY_H264) + ".mp4.part")
    wait_for(lambda: part.exists() and part.stat().st_size > 0, "the stand-in to start")
    time.sleep(0.2)

    began = time.monotonic()
    converter.close()
    assert time.monotonic() - began < 10
    assert processes_mentioning(str(part)) == []
    assert converted_folder_names(cache_root) == []
    assert converter.status(KEY_H264).state == "failed"


def test_close_during_probe_does_not_wait_for_the_probe_timeout(make_converter, samples, tmp_path):
    """close also stops a slow ffprobe, so shutdown never waits out the 180 second probe limit."""
    slow = tmp_path / "slow_ffprobe.sh"
    marker = tmp_path / "probe_started"
    slow.write_text(f'#!/bin/sh\ntouch "{marker}"\nexec sleep 120\n')
    slow.chmod(0o755)
    converter = make_converter({KEY_H264: samples["h264_ts"]}, ffprobe=str(slow))
    converter.start(KEY_H264)
    wait_for(marker.exists, "the slow probe to start")
    began = time.monotonic()
    converter.close()
    assert time.monotonic() - began < 10
    assert converter.status(KEY_H264).state == "failed"


def test_start_after_close_raises(make_converter, samples):
    """A closed converter refuses new work with ConvertError, and still answers status."""
    converter = make_converter({KEY_H264: samples["h264_ts"]})
    converter.close()
    with pytest.raises(ConvertError, match="closed"):
        converter.start(KEY_H264)
    assert converter.status(KEY_H264) == ConvertStatus("none", 0.0, "")


def test_constructor_rejects_a_source_for_that_cannot_be_called(tmp_path):
    """source_for must be callable."""
    with pytest.raises(TypeError, match="source_for"):
        Converter(tmp_path / "cache", "not a function")
