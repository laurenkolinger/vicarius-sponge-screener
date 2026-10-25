"""End-to-end tests: the whole screening flow in headless Chrome against a fake bucket.

Each test starts a real ScreenerApp on a free port with fresh data and cache
folders, the real ``config/`` and ``static/`` folders, and a FakeS3 that holds
two short clips made with ffmpeg. One headless Chrome serves the whole module.
A node script (``tests/e2e/cdp_driver.mjs``) drives one scenario per test with
real mouse and key events and prints one JSON line of what the page showed. The
test asserts on that line and then checks observations.csv, the image folders,
and the HTTP routes itself, so the page and the files are compared as two
separate witnesses. Screenshots land in ``tests/e2e/out/``.

Run with ``python3 -m pytest tests -q -m e2e``. The default suite
(``-m "not live and not e2e"``) skips this module. Set ``E2E_VERBOSE=1`` and add
``-s`` to print the driver's result line of every scenario.
"""

import atexit
import csv
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest

from screener.server import ScreenerApp, make_server
from screener.species import default_pins, load_species
from tests.fakes3 import FakeS3
from tests.test_track_worker import PAN_FILTER, expected_x, make_clip

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
STATIC_DIR = PROJECT_ROOT / "static"
DRIVER = PROJECT_ROOT / "tests" / "e2e" / "cdp_driver.mjs"
OUT_DIR = PROJECT_ROOT / "tests" / "e2e" / "out"
CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
HOMEBREW_BIN = Path("/opt/homebrew/bin")

ROOT = "TCRMP_video_ondeck/"
FOLDER_2024 = ROOT + "2024Annual/"
FOLDER_2023 = ROOT + "2023Annual/"
KEY_2024 = FOLDER_2024 + "TCRMP20241018_video_BIT_T1.mp4"
KEY_2023 = FOLDER_2023 + "TCRMP20231005_video_FLC_T2.mp4"
# A clip whose picture pans left at a known speed (tests/test_track_worker.py makes it).
KEY_PAN = FOLDER_2024 + "TCRMP20241018_video_BIT_T2.mp4"
SUFFIX_2024 = "_BIT_T1"
SUFFIX_2023 = "_FLC_T2"
MUTATION_HEADER = {"X-Screener": "1"}

CLIP_SECONDS = 12
CHROME_START_SECONDS = 30.0
SCENARIO_TIMEOUT_SECONDS = 150
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIN_IMAGE_BYTES = 200
POINT_TOLERANCE = 0.02
TIME_TOLERANCE = 0.001
PICK_CLOCK_TOLERANCE = 0.05
VIEW_TOLERANCE_PX = 1.0
MIN_PLAYING_DELTA = 0.2
MAX_PICK_GAP_MS = 1000
MIN_SCREENSHOT_BYTES = 20000
MIN_INK_PIXELS = 10
VERBOSE_ENV = "E2E_VERBOSE"
LEADER_MARGIN_PX = 30.0
# The pan scenario picks the middle of the picture at about 5 s and reads the mark 2 s later.
PAN_PICK_SECONDS = 5.0
PAN_LATER_SECONDS = 2.0
PICK_SLACK_SECONDS = 0.5
# A track covers at least this much time on each side of its pick.
TRACK_SIDE_SECONDS = 2.0
# The drawn mark sits on the interpolated track to within float rounding.
DRAWN_TOLERANCE = 1e-6

PINS = {key: code for key, code in default_pins(load_species(CONFIG_DIR / "species.csv")).items() if code}


# -------------------------------------------------------------------- fixtures


def _find_tool(name: str) -> Optional[str]:
    """Locate a command line tool on PATH or in the Homebrew folder.

    Args:
        name: The tool name, such as ``ffmpeg``.

    Returns:
        The path as text, or None when the tool is not installed.
    """
    found = shutil.which(name)
    if found:
        return found
    candidate = HOMEBREW_BIN / name
    return str(candidate) if candidate.is_file() else None


@pytest.fixture(scope="module")
def tools() -> Dict[str, str]:
    """Find ffmpeg, node, and Chrome, or skip the module with the reason.

    Returns:
        ``{"ffmpeg": path, "node": path}``.
    """
    ffmpeg, node = _find_tool("ffmpeg"), _find_tool("node")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed (brew install ffmpeg); the end-to-end tests make their test clips with it")
    if node is None:
        pytest.skip("node is not installed; the end-to-end driver is a node script")
    if not CHROME_BINARY.is_file():
        pytest.skip("Google Chrome is not installed at %s" % CHROME_BINARY)
    return {"ffmpeg": ffmpeg, "node": node}


@pytest.fixture(scope="module")
def clips(tools: Dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> Dict[str, bytes]:
    """Make the test clips once for the module: two test patterns and the pan clip.

    Args:
        tools: The tool paths.
        tmp_path_factory: Pytest's temporary folder factory.

    Returns:
        ``{key: mp4 bytes}`` for the 2024 and the 2023 clip and the pan clip.
    """
    folder = tmp_path_factory.mktemp("clips")
    made: Dict[str, bytes] = {}
    for key in (KEY_2024, KEY_2023):
        out = folder / (Path(key).stem + ".mp4")
        subprocess.run(
            [
                tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                "-t", str(CLIP_SECONDS), "-pix_fmt", "yuv420p", "-c:v", "libx264", "-movflags", "+faststart", str(out),
            ],
            check=True,
            timeout=120,
        )
        made[key] = out.read_bytes()
    made[KEY_PAN] = make_clip(tools["ffmpeg"], PAN_FILTER, folder / "pan.mp4").read_bytes()
    return made


@pytest.fixture(scope="module")
def bucket(clips: Dict[str, bytes]) -> Iterator[FakeS3]:
    """Run a fake bucket that holds the two clips, for the whole module."""
    fake = FakeS3()
    fake.start()
    try:
        for key, body in clips.items():
            fake.put(key, body)
        yield fake
    finally:
        fake.stop()


class HeadlessChrome:
    """A headless Chrome with its own profile, started for this module only.

    Attributes:
        process: The Chrome process, the leader of its own process group.
        ws_url: The browser DevTools address Chrome printed on stderr.
    """

    def __init__(self, profile: Path) -> None:
        """Start Chrome and wait for its DevTools address.

        Args:
            profile: An empty folder for ``--user-data-dir``.

        Raises:
            RuntimeError: When Chrome prints no DevTools address in time.
        """
        self.stderr_lines: List[str] = []
        self.ws_url: Optional[str] = None
        self._found = threading.Event()
        self.process = subprocess.Popen(
            [
                str(CHROME_BINARY), "--headless=new", "--remote-debugging-port=0", "--user-data-dir=%s" % profile,
                "--window-size=1280,800", "--autoplay-policy=no-user-gesture-required", "--no-first-run",
                "--no-default-browser-check", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "about:blank",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        atexit.register(self.stop)
        threading.Thread(target=self._read_stderr, daemon=True).start()
        if not self._found.wait(CHROME_START_SECONDS) or self.ws_url is None:
            self.stop()
            raise RuntimeError(
                "Chrome printed no 'DevTools listening on' line within %g s. Its stderr: %s"
                % (CHROME_START_SECONDS, " | ".join(self.stderr_lines[-10:]))
            )

    def _read_stderr(self) -> None:
        """Read Chrome's stderr to the end, and take the DevTools address from it."""
        marker = "DevTools listening on "
        assert self.process.stderr is not None
        for line in self.process.stderr:
            text = line.strip()
            self.stderr_lines.append(text)
            if marker in text and self.ws_url is None:
                self.ws_url = text.split(marker, 1)[1].strip()
                self._found.set()

    def stop(self) -> None:
        """Stop Chrome and every helper process in its group. A second call does nothing."""
        if self.process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except ProcessLookupError:
                return
            self.process.wait(timeout=10)


@pytest.fixture(scope="module")
def chrome(tools: Dict[str, str], tmp_path_factory: pytest.TempPathFactory) -> Iterator[HeadlessChrome]:
    """Start one headless Chrome for the module, and stop it at teardown even after a failure."""
    browser = HeadlessChrome(tmp_path_factory.mktemp("chrome-profile"))
    try:
        yield browser
    finally:
        browser.stop()


class RunningApp:
    """A ScreenerApp served on a free port, with its own data and cache folders.

    Attributes:
        app: The ScreenerApp.
        url: The page address, ``http://127.0.0.1:<port>/``.
        data_dir: The folder with observations.csv, frames/, crops/, and videos_screened.csv.
    """

    def __init__(self, bucket_url: str, folder: Path) -> None:
        """Create the app and start serving it on a thread.

        Args:
            bucket_url: The fake bucket's base URL.
            folder: An empty folder; ``data`` and ``cache`` go inside it.
        """
        self.data_dir = folder / "data"
        self.app = ScreenerApp(self.data_dir, folder / "cache", CONFIG_DIR, STATIC_DIR, bucket_url, 0)
        self.server = make_server(self.app, "127.0.0.1", 0)
        self.url = "http://127.0.0.1:%d/" % self.app.port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop serving, join the server thread, and close the relay and the converter."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.app.close()


@pytest.fixture()
def app(bucket: FakeS3, tmp_path: Path) -> Iterator[RunningApp]:
    """Serve a fresh app for one test, so no test sees another test's rows."""
    running = RunningApp(bucket.url, tmp_path)
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture()
def drive(chrome: HeadlessChrome, app: RunningApp, tools: Dict[str, str]) -> Callable[..., Dict[str, Any]]:
    """Return a function that runs one driver scenario against this test's app.

    The function fails the test with the scenario, the step, the error, and the observed
    facts when the driver reports a failure or the page raised an error. A scenario may
    name console messages it expects; those land in ``allowedConsole`` for the test to check.
    """
    config = {
        "root": ROOT, "folder2024": FOLDER_2024, "folder2023": FOLDER_2023,
        "key2024": KEY_2024, "key2023": KEY_2023, "keyPan": KEY_PAN, "pins": PINS,
    }

    def run(scenario: str, expected_console: Tuple[str, ...] = ()) -> Dict[str, Any]:
        """Run the scenario and return its result line.

        Args:
            scenario: A scenario name of cdp_driver.mjs.
            expected_console: Regular expressions for console errors the scenario causes on
                purpose; a matching message is moved to ``allowedConsole`` instead of failing.

        Returns:
            The parsed result: ``facts`` and ``shots`` among other fields.
        """
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [tools["node"], "--experimental-websocket", str(DRIVER), chrome.ws_url, app.url, scenario, str(OUT_DIR), json.dumps(config)],
            capture_output=True,
            text=True,
            timeout=SCENARIO_TIMEOUT_SECONDS,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            pytest.fail("scenario %s: the driver printed no result (exit %s). stderr: %s" % (scenario, completed.returncode, completed.stderr[-2000:]), pytrace=False)
        result = json.loads(lines[-1])
        expected = [re.compile(pattern) for pattern in expected_console]
        result["allowedConsole"] = [line for line in result["consoleErrors"] if any(rule.search(line) for rule in expected)]
        result["consoleErrors"] = [line for line in result["consoleErrors"] if line not in result["allowedConsole"]]
        if os.environ.get(VERBOSE_ENV):
            print(json.dumps(result, indent=1))
        problems = []
        if not result["ok"]:
            problems.append("the driver failed at step %r: %s" % (result["step"], result["error"]))
        if result["pageErrors"]:
            problems.append("page exceptions: %s" % result["pageErrors"])
        if result["consoleErrors"]:
            problems.append("console errors: %s" % result["consoleErrors"])
        if problems:
            pytest.fail(
                "scenario %s\n%s\nobserved: %s\nscreenshots: %s"
                % (scenario, "\n".join(problems), json.dumps(result["facts"], indent=1)[:4000], result["shots"]),
                pytrace=False,
            )
        return result

    return run


# ---------------------------------------------------------------------- helpers


def read_rows(app: RunningApp) -> List[Dict[str, str]]:
    """Read observations.csv from disk.

    Args:
        app: The running app.

    Returns:
        One dict per data row, in file order. An empty list before the first save.
    """
    path = app.data_dir / "observations.csv"
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def read_screened(app: RunningApp) -> Dict[str, Dict[str, str]]:
    """Read videos_screened.csv from disk.

    Args:
        app: The running app.

    Returns:
        ``{S3Key: row}``.
    """
    path = app.data_dir / "videos_screened.csv"
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {row["S3Key"]: row for row in csv.DictReader(handle)}


def get_json(app: RunningApp, path: str) -> Any:
    """GET a JSON route of the app.

    Args:
        app: The running app.
        path: The path and query, such as ``/api/tally``.

    Returns:
        The parsed JSON answer.
    """
    with urllib.request.urlopen(app.url.rstrip("/") + path, timeout=30) as answer:
        return json.loads(answer.read().decode("utf-8"))


def post_json(app: RunningApp, path: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """POST a JSON body to the app with the mutation header.

    Args:
        app: The running app.
        path: The path, such as ``/api/videos/done``.
        body: The JSON body.

    Returns:
        ``(status, parsed JSON answer)``; an error status is returned, not raised.
    """
    request = urllib.request.Request(
        app.url.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers=dict(MUTATION_HEADER, **{"Content-Type": "application/json"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as answer:
            return answer.status, json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def image_path(app: RunningApp, folder: str, name: str) -> Path:
    """Build the path of an image in the data folder.

    Args:
        app: The running app.
        folder: ``frames`` or ``crops``.
        name: The file name from a CSV row.

    Returns:
        The path, which may not exist.
    """
    return app.data_dir / folder / name


def assert_images(app: RunningApp, row: Dict[str, str], label: str, suffix: str) -> None:
    """Check a row's frame and crop: named for the label, present on disk, and real images.

    Args:
        app: The running app.
        row: A row of observations.csv.
        label: The species code, or ``LATER`` for a Later point.
        suffix: The site and transect part of the names, such as ``_BIT_T1``.
    """
    frame, crop = row["FrameFileName"], row["CropFileName"]
    assert frame.startswith("%s_%s_" % (row["ID"], label)) and frame.endswith(suffix + ".jpg"), frame
    assert crop.startswith("%s_%s_" % (row["ID"], label)) and crop.endswith(suffix + ".png"), crop
    frame_bytes = image_path(app, "frames", frame).read_bytes()
    crop_bytes = image_path(app, "crops", crop).read_bytes()
    assert frame_bytes.startswith(JPEG_MAGIC) and len(frame_bytes) > MIN_IMAGE_BYTES, "%s is not a JPEG" % frame
    assert crop_bytes.startswith(PNG_MAGIC) and len(crop_bytes) > MIN_IMAGE_BYTES, "%s is not a PNG" % crop


def png_size(path: Path) -> Tuple[int, int]:
    """Read the width and height from a PNG header.

    Args:
        path: A PNG file.

    Returns:
        ``(width, height)`` in pixels.
    """
    header = path.read_bytes()[:24]
    assert header.startswith(PNG_MAGIC), "%s is not a PNG" % path
    return struct.unpack(">II", header[16:24])


def inside(box: Dict[str, float], width: float, height: float) -> bool:
    """Tell whether a box lies inside a view, within the pixel tolerance.

    Args:
        box: ``{left, top, right, bottom}`` in viewport pixels.
        width: The view width.
        height: The view height.

    Returns:
        True when no side leaves the view by more than VIEW_TOLERANCE_PX.
    """
    return (
        box["left"] >= -VIEW_TOLERANCE_PX and box["top"] >= -VIEW_TOLERANCE_PX
        and box["right"] <= width + VIEW_TOLERANCE_PX and box["bottom"] <= height + VIEW_TOLERANCE_PX
    )


def distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Return the distance between two ``{x, y}`` points."""
    return ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5


def expected_center(placement: Dict[str, Any]) -> Dict[str, float]:
    """Work out where a wheel belongs: on its mark, pushed inward until its box fits the layer.

    Args:
        placement: A ``wheelPlacement`` fact from the driver (box, layer, mark).

    Returns:
        The wheel center as ``{x, y}`` in viewport pixels.
    """
    half_w = (placement["box"]["right"] - placement["box"]["left"]) / 2
    half_h = (placement["box"]["bottom"] - placement["box"]["top"]) / 2
    layer = placement["layer"]
    mark = placement["mark"]
    return {
        "x": min(max(mark["x"], layer["left"] + half_w), layer["right"] - half_w),
        "y": min(max(mark["y"], layer["top"] + half_h), layer["bottom"] - half_h),
    }


def assert_playing(clock: Dict[str, Any], what: str) -> None:
    """Assert that a clock reading shows the video playing on.

    Args:
        clock: ``{delta, paused}`` from the driver.
        what: What the reading follows, for the message.
    """
    assert clock["paused"] is False and clock["delta"] > MIN_PLAYING_DELTA, "%s: the video is not playing: %s" % (what, clock)


def assert_paused(after: Dict[str, Any], what: str) -> None:
    """Assert that a reading shows a pick that left the video paused in screening mode.

    Args:
        after: ``{paused, time, clock, wheel, mode}`` from the driver's ``readAfterPick``.
        what: What the reading follows, for the message.
    """
    clock = after["clock"]
    assert after["paused"] is True and clock["paused"] is True and clock["delta"] == 0, "%s: the video is not paused and still: %s" % (what, after)
    assert after["wheel"] is False and after["mode"] == "screening", "%s: the wheel is open or the mode is not screening: %s" % (what, after)


# ------------------------------------------------------------------- scenarios


def test_pick_inner(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """A click opens the wheel, one click on the key 1 slice saves, and the video stays paused on the picked frame."""
    facts = drive("pick_inner")["facts"]
    assert facts["wheel"]["mode"] == "new" and facts["wheel"]["hubLabel"] == "Later"
    assert facts["wheel"]["inner"][0] == "1:%s" % PINS["1"]
    assert facts["wheel"]["inner"] == ["%s:%s" % (key, code) for key, code in PINS.items()]
    assert facts["pausedWithWheelOpen"] is True
    assert_paused(facts["afterPick"], "after the pick")
    rows = read_rows(app)
    assert len(rows) == 1
    row = rows[0]
    assert row["SpeciesCode"] == PINS["1"] and row["LabelStatus"] == "labeled"
    assert abs(float(row["PointX"]) - 0.30) < POINT_TOLERANCE and abs(float(row["PointY"]) - 0.40) < POINT_TOLERANCE
    assert row["S3Key"] == KEY_2024
    picked_at, clock_at = float(row["TimestampSeconds"]), facts["afterPick"]["time"]
    assert abs(clock_at - picked_at) < PICK_CLOCK_TOLERANCE, "the video stopped at %s, the pick is at %s" % (clock_at, picked_at)
    assert_images(app, row, PINS["1"], SUFFIX_2024)
    assert facts["rows"][0]["ID"] == row["ID"]
    assert get_json(app, "/api/tally")["total"] == 1


def test_pick_key_and_outer(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """Key 2 saves its pinned species; typing saur and pressing Enter saves SAUR from the outer ring. Each pick leaves the video paused."""
    facts = drive("pick_key_and_outer")["facts"]
    assert facts["typed"]["query"] == "saur" and facts["typed"]["highlighted"] == "SAUR"
    assert_paused(facts["pausedAfterKey2"], "after key 2")
    assert_paused(facts["pausedAfterEnter"], "after Enter on the lit match")
    rows = read_rows(app)
    assert [row["SpeciesCode"] for row in rows] == [PINS["2"], "SAUR"]
    assert all(row["LabelStatus"] == "labeled" for row in rows)
    assert PINS["2"] != "SAUR" and "SAUR" not in PINS.values()
    for row in rows:
        assert_images(app, row, row["SpeciesCode"], SUFFIX_2024)
    assert [row["ID"] for row in facts["rows"]] == [row["ID"] for row in rows]


def test_rapid_picks(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """Two picks under a second apart save two rows with distinct IDs, listed in time order, even with both saves in flight."""
    facts = drive("rapid_picks")["facts"]
    assert facts["firstPairGapMs"] < MAX_PICK_GAP_MS and facts["secondPairGapMs"] < MAX_PICK_GAP_MS
    rows = read_rows(app)
    assert len(rows) == 4
    ids = [row["ID"] for row in rows]
    assert len(set(ids)) == 4 and ids == ["ID001", "ID002", "ID003", "ID004"]
    assert [row["SpeciesCode"] for row in rows[:2]] == [PINS["1"], PINS["2"]]
    assert sorted(row["SpeciesCode"] for row in rows[2:]) == sorted([PINS["3"], PINS["1"]])
    for row in rows:
        assert row["LabelStatus"] == "labeled"
        assert_images(app, row, row["SpeciesCode"], SUFFIX_2024)
    assert len({row["FrameFileName"] for row in rows}) == 4 and len({row["CropFileName"] for row in rows}) == 4
    listed = facts["listOrder"]
    assert sorted(item["id"] for item in listed) == sorted(ids)
    times = [item["time"] for item in listed]
    assert times == sorted(times), "the sightings list is not in time order: %s" % listed
    by_id = {row["ID"]: float(row["TimestampSeconds"]) for row in rows}
    assert all(abs(item["time"] - by_id[item["id"]]) < TIME_TOLERANCE for item in listed)
    posts = facts["posts"]
    assert len(posts) == 4 and all(post["status"] == 201 for post in posts)
    first, second = sorted(posts[2:], key=lambda post: post["startedAt"])
    assert second["startedAt"] < first["endedAt"], "the second pair's saves did not overlap: %s" % posts[2:]


def test_step_then_pick(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """Three frame steps with the wheel open, then a pick: the row holds the stepped time."""
    facts = drive("step_then_pick")["facts"]
    stepped = facts["stepped"]
    assert stepped["wheelOpen"] is True and stepped["paused"] is True
    assert 0.06 < stepped["video"] - facts["timeAtClick"] < 0.14, "three steps moved the clock from %s to %s" % (facts["timeAtClick"], stepped["video"])
    assert abs(stepped["pending"] - stepped["video"]) < TIME_TOLERANCE
    rows = read_rows(app)
    assert len(rows) == 1
    assert abs(float(rows[0]["TimestampSeconds"]) - stepped["video"]) <= TIME_TOLERANCE, (rows[0]["TimestampSeconds"], stepped)
    assert rows[0]["SpeciesCode"] == PINS["1"]
    assert_images(app, rows[0], PINS["1"], SUFFIX_2024)


def test_later_and_cancel(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """Space saves a Later point with LATER in both file names and leaves the video paused; Escape adds nothing and plays on."""
    facts = drive("later_and_cancel")["facts"]
    assert facts["hub"] == "Later"
    rows = read_rows(app)
    assert len(rows) == 1
    row = rows[0]
    assert row["LabelStatus"] == "unlabeled" and row["SpeciesCode"] == "" and row["Sponge Type"] == ""
    assert "_LATER_" in row["FrameFileName"] and "_LATER_" in row["CropFileName"]
    assert_images(app, row, "LATER", SUFFIX_2024)
    assert facts["toLabel"]["count"] == "1" and facts["toLabel"]["items"] == 1
    assert_paused(facts["afterLater"], "after the Later point")
    assert facts["afterEscape"]["rows"] == 1 and facts["afterEscape"]["mode"] == "screening"
    assert_playing(facts["afterEscape"]["clock"], "after Escape")
    tally = get_json(app, "/api/tally")
    assert tally["total"] == 0 and tally["unlabeled"] == 1


def test_relabel_from_list(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """A Later point in another folder opens from the To label list, relabels with key 3 and leaves the video paused, and renames its files."""
    facts = drive("relabel_from_list")["facts"]
    later = facts["laterRow"]
    assert later["LabelStatus"] == "unlabeled" and "_LATER_" in later["CropFileName"]
    assert later["S3Key"] == KEY_2023
    assert facts["toLabelThumbBefore"]["loaded"] is True and facts["toLabelThumbBefore"]["src"].endswith(later["CropFileName"])
    assert facts["listingHas2023"] is False, "the 2023 video must be absent from the listing on screen"
    assert facts["toLabelCountBefore"] == "1"
    jump = facts["jump"]
    assert jump["key"] == KEY_2023 and jump["wheel"] == {"mode": "edit", "rowId": later["ID"], "hubLabel": "Keep"}
    assert jump["paused"] is True and jump["seeking"] is False
    assert abs(jump["time"] - float(later["TimestampSeconds"])) < 0.1, "the video sought to %s, the point is at %s" % (jump["time"], later["TimestampSeconds"])
    assert jump["title"] == "TCRMP20231005_video_FLC_T2.mp4"

    rows = read_rows(app)
    assert len(rows) == 1
    row = rows[0]
    assert row["ID"] == later["ID"] and row["TimestampSeconds"] == later["TimestampSeconds"]
    assert row["SpeciesCode"] == PINS["3"] and row["LabelStatus"] == "labeled"
    assert_images(app, row, PINS["3"], SUFFIX_2023)
    assert not image_path(app, "frames", later["FrameFileName"]).exists(), "the old frame name is still on disk"
    assert not image_path(app, "crops", later["CropFileName"]).exists(), "the old crop name is still on disk"
    assert sorted(path.name for path in (app.data_dir / "frames").iterdir()) == [row["FrameFileName"]]
    assert sorted(path.name for path in (app.data_dir / "crops").iterdir()) == [row["CropFileName"]]

    thumb = facts["thumbAfter"]
    assert thumb["src"].endswith(row["CropFileName"]) and thumb["loaded"] is True and thumb["missing"] is False, thumb
    assert facts["toLabelAfter"] == {"count": "0", "items": 0}
    assert_paused(facts["afterRelabel"], "after relabeling from the To label list")
    assert get_json(app, "/api/observations?status=unlabeled")["rows"] == []
    tally = get_json(app, "/api/tally")
    assert tally["total"] == 1 and tally["unlabeled"] == 0


def test_unlabel_saved(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """Clicking a saved mark on the paused picture opens the Unlabel wheel; Space makes the row a Later point and leaves the video paused."""
    facts = drive("unlabel_saved")["facts"]
    saved = facts["savedRow"]
    assert saved["SpeciesCode"] == PINS["1"] and saved["LabelStatus"] == "labeled"
    assert facts["editWheel"] == {
        "mode": "edit", "rowId": saved["ID"], "hubLabel": "Unlabel", "currentCode": PINS["1"], "paused": True,
    }
    rows = read_rows(app)
    assert len(rows) == 1
    row = rows[0]
    assert row["ID"] == saved["ID"] and row["LabelStatus"] == "unlabeled" and row["SpeciesCode"] == "" and row["Sponge Type"] == ""
    assert_images(app, row, "LATER", SUFFIX_2024)
    assert not image_path(app, "frames", saved["FrameFileName"]).exists()
    assert not image_path(app, "crops", saved["CropFileName"]).exists()
    assert saved["ID"] in facts["lastToast"]["message"]
    assert_paused(facts["afterUnlabel"], "after Unlabel")
    tally = get_json(app, "/api/tally")
    assert tally["total"] == 0 and tally["unlabeled"] == 1


def test_done_refused(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """A Later point reopens a done video; Done is refused while the video holds a Later point."""
    facts = drive("done_refused")["facts"]
    assert facts["afterDone"] == {"status": "done", "button": "Done", "pressed": "true"}
    reopened = facts["afterReopen"]
    assert reopened["status"] == "in progress" and reopened["button"] == "Mark done" and reopened["pressed"] == "false"
    assert "back in progress" in reopened["toast"]["message"] and reopened["toast"]["kind"] == "info"
    assert any("back in progress" in toast["text"] for toast in reopened["toastsOnScreen"])
    assert reopened["browserBadge"].startswith("in progress")
    posts = facts["laterPost"]
    assert [post["status"] for post in posts] == [201, 201]
    assert posts[0]["reopened"] is False and posts[1]["reopened"] is True and posts[1]["species"] == ""
    refusal = facts["afterRefusal"]
    assert refusal["status"] == "in progress" and refusal["button"] == "Mark done"
    assert "unlabeled" in refusal["toast"]["message"] and refusal["toast"]["kind"] == "error"
    assert refusal["doneRequests"] == 1, "only the first D press may reach the server"
    assert read_screened(app)[KEY_2024]["Status"] == "in progress"
    status, answer = post_json(app, "/api/videos/done", {"key": KEY_2024, "done": True})
    assert status == 400 and "unlabeled" in answer["error"]
    assert read_screened(app)[KEY_2024]["Status"] == "in progress"
    rows = read_rows(app)
    assert [row["LabelStatus"] for row in rows] == ["labeled", "unlabeled"]


def test_fullscreen(drive: Callable[[str], Dict[str, Any]]) -> None:
    """F puts the theater in full screen; the wheel stays inside the view and re-centers when the button toggles."""
    facts = drive("fullscreen")["facts"]
    assert facts["entered"]["element"] == "theater" and facts["entered"]["flag"] == "true" and facts["entered"]["state"] is True
    corner = facts["corner"]
    view = corner["view"]
    assert corner["open"] is True and corner["fullscreenElement"] == "theater"
    assert inside(corner["box"], view["w"], view["h"]), "the wheel left the view: %s" % corner
    assert corner["leaderShown"] is True, "a corner click needs the leader line: %s" % corner
    assert facts["left"] == {"element": None, "flag": "false", "state": False, "view": facts["windowedView"]}

    windowed, full, back = facts["windowedWheel"], facts["fullscreenWheel"], facts["leftWheel"]
    shrunk, restored = facts["shrunkWheel"], facts["restoredWheel"]
    assert all(placement["open"] for placement in (windowed, full, back, shrunk, restored))
    assert windowed["fullscreenElement"] is None and full["fullscreenElement"] == "theater" and back["fullscreenElement"] is None
    assert shrunk["view"] == {"w": 700, "h": 520}, "the window did not shrink: %s" % shrunk["view"]
    for name, placement in (("windowed", windowed), ("fullscreen", full), ("left", back), ("shrunk", shrunk), ("restored", restored)):
        assert inside(placement["box"], placement["view"]["w"], placement["view"]["h"]), "%s: the wheel left the view: %s" % (name, placement)
        assert inside(placement["box"], placement["layer"]["right"], placement["layer"]["bottom"]), "%s: the wheel left its layer: %s" % (name, placement)
        # The wheel sits on its mark, pushed inward only as far as its box needs to stay inside the view.
        wanted = expected_center(placement)
        assert distance(placement["center"], wanted) <= VIEW_TOLERANCE_PX, "%s: the wheel is at %s, the mark %s calls for %s" % (name, placement["center"], placement["mark"], wanted)
        if distance(placement["center"], placement["mark"]) > placement["hubRadius"] + LEADER_MARGIN_PX:
            assert placement["leaderShown"], "%s: the mark is outside the hub and the leader line is hidden: %s" % (name, placement)
    assert distance(full["center"], windowed["center"]) > 5, "the full-screen wheel did not move with the mark"
    assert distance(back["center"], windowed["center"]) <= 3, "the wheel did not return to its mark after leaving full screen"
    assert distance(shrunk["center"], back["center"]) > 5, "the wheel did not move when the window shrank"
    assert distance(restored["center"], windowed["center"]) <= 3, "the wheel did not return to its mark after the window grew back"


def test_speed_default(drive: Callable[[str], Dict[str, Any]]) -> None:
    """A fresh profile plays at 1.5x, ignores the old speed key, and remembers a new choice."""
    facts = drive("speed_default")["facts"]
    assert facts["labelBeforeVideo"] == "1.5x"
    assert facts["fresh"]["rate"] == 1.5 and facts["fresh"]["label"] == "1.5x" and facts["fresh"]["state"] == 1.5
    assert not facts["clock"]["paused"] and 1.35 < facts["clock"]["rate"] < 1.65, "the video ran at %sx real time" % facts["clock"]["rate"]
    assert facts["afterOldKey"] == {"label": "1.5x", "state": 1.5}
    assert facts["afterFaster"]["rate"] == 2 and facts["afterFaster"]["label"] == "2x" and facts["afterFaster"]["stored"] == "2"
    assert facts["afterReload"] == {"label": "2x", "state": 2}


def test_shift_digit_plays_on(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """Shift with a digit saves and plays on; a Shift-click on a slice does the same for a second sponge."""
    facts = drive("shift_digit_plays_on")["facts"]
    rows = read_rows(app)
    assert [row["SpeciesCode"] for row in rows] == [PINS["1"], PINS["2"]]
    assert all(row["LabelStatus"] == "labeled" for row in rows)
    first_time, second_time = (float(row["TimestampSeconds"]) for row in rows)
    assert second_time - first_time > TIME_TOLERANCE, "the second sponge was saved on the same frame, so the video did not play on"
    assert facts["keydownSeen"] == {"key": "!", "code": "Digit1", "shiftKey": True}, "the page saw %s" % facts["keydownSeen"]
    for name, what in (("playingAfterDigit", "after Shift+1"), ("playingAfterClick", "after the Shift-click")):
        after = facts[name]
        assert after["paused"] is False and after["wheel"] is False and after["mode"] == "screening", "%s: %s" % (what, after)
        assert_playing(after["clock"], what)
    for row in rows:
        assert_images(app, row, row["SpeciesCode"], SUFFIX_2024)


def test_screenshots(drive: Callable[[str], Dict[str, Any]]) -> None:
    """Screenshots of the wheel at 1280 x 800, 946 x 700, and full screen, a Later mark, and a flash label."""
    result = drive("screenshots")
    facts = result["facts"]
    expected = {
        "wheel-1280x800": tuple(facts["wide"]["view"]),
        "wheel-946x700": tuple(facts["narrow"]["view"]),
        "wheel-fullscreen": tuple(facts["fullscreenShot"]["view"]),
        "flash-label": tuple(facts["wide"]["view"]),
        "later-flash": tuple(facts["wide"]["view"]),
        "later-mark": tuple(facts["wide"]["view"]),
    }
    assert facts["wide"]["view"] == [1280, 800] and facts["narrow"]["view"] == [946, 700]
    for name, size in expected.items():
        path = OUT_DIR / ("%s.png" % name)
        assert path.is_file() and path.stat().st_size > MIN_SCREENSHOT_BYTES, "%s is missing or nearly empty" % path
        assert png_size(path) == size, "%s is %s, the view was %s" % (path.name, png_size(path), size)
    for name in ("wide", "narrow", "fullscreenShot"):
        placement = facts[name]["placement"]
        assert facts[name]["opacity"] == "1", "%s: the wheel was still fading in" % name
        assert placement["open"] and inside(placement["box"], placement["view"]["w"], placement["view"]["h"]), (name, placement)
    assert facts["flash"]["ink"]["ink"] >= MIN_INK_PIXELS and facts["flash"]["paused"] is True
    assert facts["laterFlash"]["ink"]["amber"] >= MIN_INK_PIXELS, "no amber Later flash was drawn: %s" % facts["laterFlash"]
    assert facts["laterMark"]["ink"]["amber"] >= MIN_INK_PIXELS, "no amber Later mark was drawn: %s" % facts["laterMark"]


# The removed pick shows in the list for a moment, so its thumbnail request can land after
# its crop moved to the trash. That one 404 is expected; any other console error fails.
REMOVED_CROP_404 = r"status of 404 \(Not Found\) http://127\.0\.0\.1:\d+/media/crops/(ID\d+)_"


def test_undo_during_save(drive: Callable[..., Dict[str, Any]], app: RunningApp) -> None:
    """Z pressed while the last pick is still saving removes that pick, never the sighting before it."""
    result = drive("undo_during_save", expected_console=(REMOVED_CROP_404,))
    facts = result["facts"]
    first = facts["first"]
    assert first["SpeciesCode"] == PINS["1"]
    assert facts["whileWaiting"]["deletes"] == 0, "Z removed something before the held save answered: %s" % facts["whileWaiting"]
    saves = facts["saves"]
    assert [save["status"] for save in saves] == [201, 201] and [save["code"] for save in saves] == [PINS["1"], PINS["2"]]
    picked = saves[1]["id"]
    assert [delete["id"] for delete in facts["deletes"]] == [picked] and facts["deletes"][0]["status"] == 200
    assert any(toast["text"] == "Waiting for the last save" and toast["kind"] == "info" for toast in facts["toasts"]), facts["toasts"]
    rows = read_rows(app)
    assert [row["ID"] for row in rows] == [first["ID"]] and rows[0]["SpeciesCode"] == PINS["1"]
    assert_images(app, rows[0], PINS["1"], SUFFIX_2024)
    assert [row["ID"] for row in facts["rows"]] == [first["ID"]]
    left = sorted(path.name for path in (app.data_dir / "frames").iterdir())
    assert left == [rows[0]["FrameFileName"]], "the removed pick left a frame behind: %s" % left
    for line in result["allowedConsole"]:
        assert re.search(REMOVED_CROP_404, line).group(1) == picked, "a 404 for a crop that was not removed: %s" % line


def test_retry_after_failure(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """A save that never reached the server waits for Retry and blocks leaving; Retry saves it exactly once."""
    facts = drive("retry_after_failure")["facts"]
    assert facts["leaveBlockedAtStart"] is False
    failed = facts["failed"]
    assert failed["rows"] == 0 and failed["retryButtons"] == 1 and failed["leaveBlocked"] is True
    assert failed["toast"]["kind"] == "error" and "not saved" in failed["toast"]["message"] and PINS["1"] in failed["toast"]["message"]
    assert facts["afterRetry"]["leaveBlocked"] is False
    assert [save["status"] for save in facts["saves"]] == [0, 201], "one failed attempt, then one save: %s" % facts["saves"]
    rows = read_rows(app)
    assert len(rows) == 1 and rows[0]["SpeciesCode"] == PINS["1"] and rows[0]["ID"] == facts["saves"][1]["id"]
    assert_images(app, rows[0], PINS["1"], SUFFIX_2024)


def test_arrow_scrub(drive: Callable[[str], Dict[str, Any]]) -> None:
    """Holding Right fast-forwards at 4x, holding Left rewinds, release restores speed and pause, and with the wheel open an arrow steps one frame."""
    facts = drive("arrow_scrub")["facts"]
    forward, rewind, step = facts["fastForward"], facts["rewind"], facts["wheelStep"]
    assert 2.5 < forward["moved"] < 5.5, "one second of Right moved %s s" % forward["moved"]
    assert forward["rateDuring"] == 4 and forward["rateAfter"] == 1.5 and forward["pausedAfter"] is True, forward
    assert -5.5 < rewind["moved"] < -2.0, "one second of Left moved %s s" % rewind["moved"]
    assert rewind["rateAfter"] == 1.5 and rewind["pausedAfter"] is True, rewind
    assert step["wheelOpen"] is True and step["paused"] is True and 0.02 < step["moved"] < 0.05, step


# A guide with this many photos is far taller than the window. Docked, it must still take the
# video's height and scroll inside (on 2026-10-13 the photo list stretched the video area to
# about 3,000 px and pushed the picture and the controls out of view).
GUIDE_FIXTURE_PHOTOS = 40
# The smallest valid PNG: one gray pixel.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010800000000"
    "3a7e9b550000000a49444154789c63680000000201014868a7c50000000049454e44ae426082"
)


def write_guide_fixture(data_dir: Path, codes: Sequence[str]) -> None:
    """Write a species guide into a test data folder: many photos for each listed species.

    Args:
        data_dir: The app's data folder.
        codes: The species codes in the guide; the first is the one the guide opens on (the
            species pinned to key 1).
    """
    entries = []
    names = ["photo%02d.png" % index for index in range(GUIDE_FIXTURE_PHOTOS)]
    for code in codes:
        folder = data_dir / "guide" / code
        folder.mkdir(parents=True)
        for name in names:
            (folder / name).write_bytes(TINY_PNG)
        entries.append({"code": code, "name": code, "part": "", "description": "Test description.",
                        "source": "", "viewer_name": "", "images": names})
    (data_dir / "guide" / "guide.json").write_text(json.dumps({"species": entries}), encoding="utf-8")


def test_guide_dock(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """The guide opens docked beside the video without covering it, keeps the video's height with many photos, floats on request, and remembers the choice."""
    write_guide_fixture(app.data_dir, [PINS["1"]])
    facts = drive("guide_dock")["facts"]
    docked, floating = facts["docked"], facts["floating"]
    assert docked["photos"] == GUIDE_FIXTURE_PHOTOS, docked["photos"]
    assert docked["theater"]["bottom"] <= docked["viewportHeight"] + 1, "the docked guide stretched the video area: %s" % docked
    assert docked["transportBottom"] <= docked["viewportHeight"] + 1, "the controls left the window: %s" % docked
    assert docked["dock"] == "docked" and docked["pressed"] == "true" and docked["position"] == "static", docked
    assert docked["guide"]["left"] >= docked["stage"]["right"] - 1, "the docked guide covers the video: %s" % docked
    assert docked["stage"]["width"] < docked["widthBefore"] - 100, "the video did not make room: %s" % docked
    assert floating["dock"] == "floating" and floating["pressed"] == "false" and floating["position"] == "fixed", floating
    assert abs(floating["stage"]["width"] - docked["widthBefore"]) <= 2, floating
    assert facts["afterReload"] == "floating"


def test_wheel_follows_guide(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """With the guide open, the species under the pointer or the Tab highlight shows in it; a closed guide stays closed; species logged in this video lead the outer ring with their count."""
    write_guide_fixture(app.data_dir, list(PINS.values()) + ["SAUR"])
    facts = drive("wheel_follows_guide")["facts"]
    assert facts["afterHover"]["guide"] == facts["afterHover"]["expected"] == PINS["2"], facts["afterHover"]
    assert facts["afterTab"]["lit"] and facts["afterTab"]["guide"] == facts["afterTab"]["lit"], facts["afterTab"]
    rows = read_rows(app)
    assert len(rows) == 1 and rows[0]["SpeciesCode"] == "SAUR", rows
    assert facts["seenOrder"]["first"] == "SAUR", facts["seenOrder"]
    assert "\u00d71" in (facts["seenOrder"]["label"] or ""), facts["seenOrder"]
    assert facts["closedGuide"]["hidden"] is True, "hovering the wheel opened a closed guide"


def test_cancel_paths(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """A right-click on the picture cancels the mark and plays on; a click outside the wheel and the picture drops it and stays paused."""
    facts = drive("cancel_paths")["facts"]
    right = facts["afterRightClick"]
    assert right["wheel"] is False and right["pending"] is False and right["mode"] == "screening"
    assert_playing(right["clock"], "after the right-click on the picture")
    outside = facts["afterOutsideClick"]
    assert outside["wheel"] is False and outside["pending"] is False and outside["mode"] == "screening"
    assert outside["clock"]["paused"] is True and outside["clock"]["delta"] == 0, "the video moved after the outside click: %s" % outside["clock"]
    assert facts["rows"] == [] and read_rows(app) == []


def test_note_rides_on_pick(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """Tab walks the wheel highlight; with the panel shown a click on the note box takes the note, Enter returns to the wheel without saving, and the note lands in the Notes column."""
    facts = drive("note_rides_on_pick")["facts"]
    hidden = facts["hiddenTab"]
    assert hidden["deck"] == "hidden" and hidden["wheel"] is True and hidden["focus"] != "note", "Tab reached the note box: %s" % hidden
    assert hidden["highlighted"] == PINS["1"], "Tab did not light the first species: %s" % hidden
    assert facts["afterNoteClick"] == {"focus": "note", "wheel": True}
    after_enter = facts["afterEnter"]
    assert after_enter["focus"] != "note" and after_enter["wheel"] is True and after_enter["rows"] == 0
    assert after_enter["note"] == "big one" and after_enter["paused"] is True
    rows = read_rows(app)
    assert len(rows) == 1 and rows[0]["SpeciesCode"] == PINS["1"]
    assert rows[0]["Notes"] == "top left, big one", rows[0]["Notes"]


def test_deck_toggle(drive: Callable[[str], Dict[str, Any]]) -> None:
    """A fresh profile hides the panel under the video; P and the panel button show and hide it, a reload keeps it shown, and the stage grows when it hides."""
    facts = drive("deck_toggle")["facts"]
    fresh, after_p, after_reload, after_button = facts["fresh"], facts["afterP"], facts["afterReload"], facts["afterButton"]
    assert fresh["display"] == "none" and fresh["deck"] == "hidden" and fresh["shown"] is False, fresh
    assert fresh["pressed"] == "false" and fresh["stored"] in (None, "0"), fresh
    for what, shown in (("after P", after_p), ("after the reload", after_reload)):
        assert shown["display"] != "none" and shown["deck"] == "shown" and shown["shown"] is True, "%s: %s" % (what, shown)
        assert shown["pressed"] == "true" and shown["stored"] == "1", "%s: %s" % (what, shown)
    assert after_button["display"] == "none" and after_button["deck"] == "hidden" and after_button["shown"] is False, after_button
    assert after_button["pressed"] == "false" and after_button["stored"] == "0", after_button
    assert fresh["stageHeight"] - after_p["stageHeight"] > VIEW_TOLERANCE_PX, "the stage did not shrink when P showed the panel: %s then %s" % (fresh, after_p)
    assert after_button["stageHeight"] - after_reload["stageHeight"] > VIEW_TOLERANCE_PX, "the stage did not grow when the button hid the panel: %s then %s" % (after_reload, after_button)


SAVED_BUT_NOT_SHOWN = r"is saved, but the page could not show it"


def test_render_fault_after_save(drive: Callable[..., Dict[str, Any]], app: RunningApp) -> None:
    """A fault while the page shows a saved row goes to the console; the row is never retried or posted twice."""
    result = drive("render_fault_after_save", expected_console=(SAVED_BUT_NOT_SHOWN,))
    after = result["facts"]["after"]
    assert after["saves"] == [201] and after["retryButtons"] == 0 and after["leaveBlocked"] is False, after
    assert len(result["allowedConsole"]) == 1, result["allowedConsole"]
    rows = read_rows(app)
    assert len(rows) == 1 and rows[0]["SpeciesCode"] == PINS["1"]
    assert rows[0]["ID"] in result["allowedConsole"][0]


def test_undo_failed_pick(drive: Callable[..., Dict[str, Any]], app: RunningApp) -> None:
    """Z right after a pick whose save failed discards that pick; the good sighting before it stays on disk."""
    facts = drive("undo_failed_pick")["facts"]
    first = facts["first"]
    after = facts["after"]
    assert after["deletes"] == 0, "Z sent a DELETE: %s" % after
    assert after["retryButtons"] == 0 and after["leaveBlocked"] is False, after
    assert after["rows"] == [first["ID"]]
    texts = [toast["text"] for toast in facts["toasts"]]
    assert any(text.startswith("Discarded the unsaved %s pick at " % PINS["2"]) for text in texts), texts
    assert not any(text.startswith("Removed") for text in texts), texts
    rows = read_rows(app)
    assert [row["ID"] for row in rows] == [first["ID"]] and rows[0]["SpeciesCode"] == PINS["1"]
    assert_images(app, rows[0], PINS["1"], SUFFIX_2024)


def test_undo_wait_limit(drive: Callable[..., Dict[str, Any]], app: RunningApp) -> None:
    """Z stops waiting for a save that does not answer, says so, and deletes nothing; the slow save still lands."""
    facts = drive("undo_wait_limit")["facts"]
    texts = [toast["text"] for toast in facts["toasts"]]
    assert "Waiting for the last save" in texts and "The last save has not answered yet. Z did nothing." in texts, texts
    assert facts["deletes"] == 0
    rows = read_rows(app)
    assert len(rows) == 1 and rows[0]["SpeciesCode"] == PINS["1"] and facts["rows"] == [rows[0]["ID"]]


PICK_FAULT = r"the pick was not saved: the note box could not be read"


def test_pick_fault_untracks(drive: Callable[..., Dict[str, Any]], app: RunningApp) -> None:
    """A fault inside a pick before its save goes out leaves nothing in flight, keeps the wheel, and the next pick saves."""
    result = drive("pick_fault_untracks", expected_console=(PICK_FAULT,))
    after = result["facts"]["afterFault"]
    assert after["leaveBlocked"] is False and after["posts"] == 0 and after["wheel"] is True, after
    assert after["toast"]["kind"] == "error" and "note box" in after["toast"]["message"], after["toast"]
    assert len(result["allowedConsole"]) == 1
    assert result["facts"]["leaveBlockedAfter"] is False
    rows = read_rows(app)
    assert len(rows) == 1 and rows[0]["SpeciesCode"] == PINS["1"]



def position_at(points: List[List[float]], time: float) -> Tuple[float, float]:
    """Interpolate a track at a time, as the page's positionAt does inside the track.

    Args:
        points: The track's ``[t, x, y, score]`` points, in time order.
        time: A time between the first and the last point.

    Returns:
        ``(x, y)`` in frame fractions.
    """
    for (t0, x0, y0, _), (t1, x1, y1, _) in zip(points, points[1:]):
        if t0 <= time <= t1:
            share = (time - t0) / (t1 - t0)
            return x0 + (x1 - x0) * share, y0 + (y1 - y0) * share
    raise AssertionError("%.3f s is outside the track (%.3f to %.3f s)" % (time, points[0][0], points[-1][0]))


def test_pan_tracking(drive: Callable[[str], Dict[str, Any]], app: RunningApp) -> None:
    """A point picked at about 5 s of the pan clip gets a track before and after the pick that follows the known motion; 2 s later its mark is drawn on the track, and a click on the moving mark opens its edit wheel with the video paused."""
    facts = drive("pan_tracking")["facts"]
    row = facts["row"]
    picked_at, start_x, start_y = float(row["TimestampSeconds"]), float(row["PointX"]), float(row["PointY"])
    assert abs(picked_at - PAN_PICK_SECONDS) < PICK_SLACK_SECONDS, row
    points = facts["track"]["points"]
    assert points[0][0] <= picked_at - TRACK_SIDE_SECONDS and points[-1][0] >= picked_at + TRACK_SIDE_SECONDS, (points[0], points[-1])
    for time, x, y, _ in points:
        assert abs(x - expected_x(start_x, picked_at, time)) <= POINT_TOLERANCE, "at %.3f s the track holds x %.4f" % (time, x)
        assert abs(y - start_y) <= POINT_TOLERANCE, "at %.3f s the track holds y %.4f" % (time, y)

    later = facts["drawnLater"]
    assert later["playing"] is True and later["mark"]["tracked"] is True, later
    assert later["time"] >= picked_at + PAN_LATER_SECONDS - PICK_SLACK_SECONDS, later
    on_track = position_at(points, later["time"])
    drawn = (later["mark"]["point"]["x"], later["mark"]["point"]["y"])
    assert abs(drawn[0] - on_track[0]) <= DRAWN_TOLERANCE and abs(drawn[1] - on_track[1]) <= DRAWN_TOLERANCE, (drawn, on_track)
    assert abs(drawn[0] - expected_x(start_x, picked_at, later["time"])) <= POINT_TOLERANCE, later

    clicked = facts["clickedMark"]
    assert clicked["target"]["playing"] is True and clicked["target"]["mark"]["tracked"] is True
    assert clicked["wheel"] == {"mode": "edit", "rowId": row["ID"]} and clicked["paused"] is True, clicked
    stored = get_json(app, "/api/tracks?key=" + urllib.parse.quote(KEY_PAN, safe=""))
    assert stored["pending"] == [] and stored["tracks"][row["ID"]]["points"] == points


# The hub ring's color while a deletion waits for its second press (--wheel-delete, #ff4a4a).
ARMED_STROKE = "rgb(255, 74, 74)"


def test_backspace_burst_keeps_row(drive: Callable[..., Dict[str, Any]], app: RunningApp) -> None:
    """Four quick Backspace taps after a two-letter filter in a saved mark's wheel delete nothing (the 2026-10-13 review reproduced a deletion here)."""
    after = drive("backspace_burst_keeps_row")["facts"]["afterBurst"]
    assert after["rows"] == 1 and after["deletes"] == 0, after
    assert len(read_rows(app)) == 1


def test_delete_from_wheel(drive: Callable[..., Dict[str, Any]], app: RunningApp) -> None:
    """In the edit wheel one Backspace turns the hub red and asks again; another key, a pointer move to another slice, or 3 s disarm it, and Escape deletes nothing. Two delete the row with its images and track, on a tracked mark clicked during playback and on a mark of the paused picture, and the video stays paused."""
    result = drive("delete_from_wheel", expected_console=(REMOVED_CROP_404,))
    facts = result["facts"]
    first, second = facts["first"], facts["second"]
    armed = facts["armed"]
    assert armed["readout"] == "Press delete again to delete %s %s" % (first["ID"], PINS["1"]), armed
    assert armed["hub"] == "true" and armed["stroke"] == ARMED_STROKE, armed
    assert facts["disarmed"] == {"afterKey": False, "afterPointer": False, "afterWait": False}, facts["disarmed"]
    assert facts["afterEscape"] == {"rows": [first["ID"]], "deletes": 0}

    playing = facts["playingClick"]
    assert playing["target"]["playing"] is True and playing["target"]["mark"]["tracked"] is True, playing
    assert playing["paused"] is True
    moving = facts["afterPlayingDelete"]
    assert moving["rows"] == [] and moving["paused"] is True, moving
    assert moving["toast"]["message"] == "Deleted %s %s" % (first["ID"], PINS["1"])
    assert moving["deletes"] == [{"id": first["ID"], "status": 200}]

    still = facts["afterPausedDelete"]
    assert still["rows"] == [] and still["paused"] is True, still
    assert still["toast"]["message"] == "Deleted %s %s" % (second["ID"], PINS["2"])
    assert [entry["status"] for entry in still["deletes"]] == [200, 200]

    assert read_rows(app) == []
    for folder in ("frames", "crops"):
        assert list((app.data_dir / folder).iterdir()) == [], "%s kept an image of a deleted row" % folder
    tracks = app.data_dir / "tracks"
    assert not tracks.exists() or list(tracks.iterdir()) == [], list(tracks.iterdir())
    trashed = [path.name for path in (app.data_dir / "trash").iterdir()]
    assert any(name.endswith("_%s.json" % first["ID"]) for name in trashed), "the first track is not in the trash: %s" % trashed
    for line in result["allowedConsole"]:
        assert re.search(REMOVED_CROP_404, line).group(1) in (first["ID"], second["ID"]), line
