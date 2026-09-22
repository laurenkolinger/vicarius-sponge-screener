"""Tests for screener.server and the screener.py entry point.

Every test starts a real ThreadingHTTPServer on a free port of 127.0.0.1 with
temporary data and cache folders, a tiny static folder built here (so the
front end's files are never needed), the project's config folder, and a FakeS3
as the bucket. Requests go through http.client or a raw socket. The entry point
tests run screener.py as a subprocess, the way the end-to-end test does.
"""

import base64
import http.client
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from screener import config
from screener import relay as relay_module
from screener.keys import cache_id, quote_key
from screener.server import ScreenerApp, make_server
from tests.fakes3 import FakeS3

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
ENTRY_POINT = PROJECT_ROOT / "screener.py"
HOMEBREW_BIN = Path("/opt/homebrew/bin")

ROOT = "TCRMP_video_ondeck/"
FOLDER_2024 = ROOT + "2024Annual/"
FOLDER_2023 = ROOT + "2023Annual/"
KEY_T1 = FOLDER_2024 + "TCRMP20241022_video_FLC_T1.MP4"
KEY_T2 = FOLDER_2024 + "TCRMP20241022_video_FLC_T2.MP4"
KEY_2016 = ROOT + "2016Annual/TCRMP20160901_video_FLC_T2.MP4"
KEY_MTS = FOLDER_2023 + "TCRMP20230801_video_BIT_T1.MTS"
KEY_PLUS = ROOT + "2005 Annual/TCRMP20050712_video_FLC_T1+T3-6.MP4"
KEY_TS = ROOT + "2012Annual/TCRMP20120801_video_FLC_T1.MTS"
KEY_MISSING = FOLDER_2024 + "TCRMP20241022_video_FLC_T9.MP4"

# Bigger than one relay chunk, so a range can cross a chunk edge and a stream
# outruns the socket buffers.
BIG_SIZE = 2 * config.CHUNK_SIZE + 777
SMALL_SIZE = 5000

JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
WAIT_SECONDS = 60.0
POLL_SECONDS = 0.02


def b64(data: bytes) -> str:
    """Encode bytes the way the page does, as plain base64 text."""
    return base64.b64encode(data).decode("ascii")


def observation(**overrides: Any) -> Dict[str, Any]:
    """Build a valid POST /api/observations body, with some fields replaced."""
    body: Dict[str, Any] = {
        "key": KEY_T1,
        "time": 12.345,
        "point": {"x": 0.25, "y": 0.75},
        "box": None,
        "species": "ACAU",
        "note": "big one",
        "frame_jpeg": b64(JPEG),
        "crop_png": "data:image/png;base64," + b64(PNG),
    }
    body.update(overrides)
    return body


def wait_for(predicate, what: str, timeout: float = WAIT_SECONDS) -> None:
    """Poll until a condition holds, or fail with a message."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"timed out after {timeout:g} seconds waiting for {what}")


def find_tool(name: str) -> str:
    """Locate ffmpeg or ffprobe on PATH or in the Homebrew folder, or fail the test."""
    found = shutil.which(name)
    if found:
        return found
    candidate = HOMEBREW_BIN / name
    if candidate.is_file():
        return str(candidate)
    pytest.fail(f"{name} is not installed; the conversion route test needs it (brew install ffmpeg)")


class Answer:
    """One HTTP answer: status, headers, body, and the body as JSON."""

    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes) -> None:
        """Keep the parts of one answer."""
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        """Parse the body as JSON."""
        return json.loads(self.body.decode("utf-8"))

    @property
    def error(self) -> str:
        """Return the error text of a JSON error answer."""
        return self.json()["error"]


class Client:
    """A small http.client wrapper that opens a fresh connection per request."""

    def __init__(self, port: int) -> None:
        """Remember the server port."""
        self.port = port

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        mutation_header: bool = True,
        host: Optional[str] = "",
    ) -> Answer:
        """Send one request and read the whole answer.

        Args:
            method: GET, POST, DELETE, or another method.
            path: The path and query string.
            body: None, a dict (sent as JSON), or bytes (sent as is).
            headers: Extra headers.
            mutation_header: False leaves out ``X-Screener: 1`` on POST and DELETE.
            host: "" sends the normal Host header, None sends no Host header,
                and other text is sent as the Host header.
        """
        sent = dict(headers or {})
        if method in ("POST", "DELETE") and mutation_header:
            sent.setdefault("X-Screener", "1")
        payload: Optional[bytes] = None
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        elif body is not None:
            payload = body
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=WAIT_SECONDS)
        try:
            connection.putrequest(method, path, skip_host=host != "")
            if host is not None and host != "":
                connection.putheader("Host", host)
            for name, value in sent.items():
                connection.putheader(name, value)
            if payload is not None:
                connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders(payload)
            response = connection.getresponse()
            return Answer(response.status, response.headers, response.read())
        finally:
            connection.close()

    def get(self, path: str, **kwargs: Any) -> Answer:
        """Send a GET."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Answer:
        """Send a POST with a JSON body (an empty object by default)."""
        return self.request("POST", path, body if body is not None else {}, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Answer:
        """Send a DELETE."""
        return self.request("DELETE", path, **kwargs)


class Running:
    """A started app: the ScreenerApp, its server, and the folders it uses."""

    def __init__(self, app: ScreenerApp, server: Any, data_dir: Path, cache_dir: Path) -> None:
        """Keep the app, its server, and its folders together."""
        self.app = app
        self.server = server
        self.port = server.server_address[1]
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.client = Client(self.port)


@pytest.fixture
def fake():
    """A FakeS3 with a few videos in two folders."""
    server = FakeS3()
    server.start()
    server.put(KEY_T1, os.urandom(BIG_SIZE))
    server.put(KEY_T2, os.urandom(SMALL_SIZE))
    server.put(KEY_MTS, os.urandom(SMALL_SIZE))
    server.put(KEY_PLUS, os.urandom(SMALL_SIZE))
    server.put(FOLDER_2024 + "notes.txt", b"not a video")
    server.put(FOLDER_2024 + "empty.MP4", b"")
    yield server
    server.stop()


@pytest.fixture
def static_root(tmp_path: Path) -> Path:
    """A tiny static folder, so the tests never depend on the front end's files."""
    folder = tmp_path / "static"
    (folder / "sub").mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html><title>Test page</title><p>hello", encoding="utf-8")
    (folder / "app.js").write_text("export const answer = 42;\n", encoding="utf-8")
    (folder / "style.css").write_text("body { color: magenta; }\n", encoding="utf-8")
    (folder / "sub" / "note.txt").write_text("nested\n", encoding="utf-8")
    return folder


@pytest.fixture
def make_app(tmp_path: Path, fake: FakeS3, static_root: Path):
    """Build running apps on the fake bucket, and stop them all afterward."""
    made: List[Running] = []

    def build(settings: Optional[Dict[str, Any]] = None, bucket_url: Optional[str] = None) -> Running:
        """Start one app on fresh data and cache folders, with optional settings.json contents."""
        data_dir = tmp_path / f"data{len(made)}"
        cache_dir = tmp_path / f"cache{len(made)}"
        if settings is not None:
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        app = ScreenerApp(data_dir, cache_dir, CONFIG_DIR, static_root, bucket_url or fake.url, 0)
        server = make_server(app, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        running = Running(app, server, data_dir, cache_dir)
        made.append(running)
        return running

    yield build
    for running in made:
        running.server.shutdown()
        running.server.server_close()
        running.app.close()


@pytest.fixture
def running(make_app) -> Running:
    """One running app."""
    return make_app()


@pytest.fixture
def fast_backoff(monkeypatch):
    """Shrink the relay's retry pauses so failure tests finish in milliseconds."""
    monkeypatch.setattr(relay_module, "RETRY_BACKOFF_SECONDS", (0.01, 0.01, 0.01))


def save_one(running: Running, **overrides: Any) -> Dict[str, str]:
    """Save one sighting through the API and return its row."""
    answer = running.client.post("/api/observations", observation(**overrides))
    assert answer.status == 201, answer.body
    return answer.json()["row"]


# ----- health, page, static files -----


def test_health(running: Running, capsys):
    """The health route answers JSON with the version, an exact Content-Length, and one log line."""
    answer = running.client.get("/api/health")
    assert answer.status == 200
    assert answer.headers["Content-Type"].startswith("application/json")
    assert int(answer.headers["Content-Length"]) == len(answer.body)
    assert answer.json() == {"ok": True, "version": config.VERSION}
    wait_for(lambda: "GET /api/health 200 " in capsys.readouterr().err, "the request log line")


def test_serves_page_and_static_with_types(running: Running, static_root: Path):
    """The page and its files come with the right content types, and other paths are 404."""
    page = running.client.get("/")
    assert page.status == 200
    assert page.headers["Content-Type"] == "text/html; charset=utf-8"
    assert page.body == (static_root / "index.html").read_bytes()
    assert int(page.headers["Content-Length"]) == len(page.body)
    assert page.headers["Cache-Control"] == "no-cache"

    script = running.client.get("/static/app.js")
    assert script.status == 200
    assert script.headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert script.body == (static_root / "app.js").read_bytes()

    sheet = running.client.get("/static/style.css")
    assert sheet.status == 200
    assert sheet.headers["Content-Type"] == "text/css; charset=utf-8"

    nested = running.client.get("/static/sub/note.txt")
    assert nested.status == 200
    assert nested.headers["Content-Type"] == "text/plain; charset=utf-8"

    missing = running.client.get("/static/missing.js")
    assert missing.status == 404
    assert "missing.js" in missing.error

    folder = running.client.get("/static/sub")
    assert folder.status == 404

    assert running.client.get("/index.html").status == 404


def test_static_traversal_blocked(running: Running, static_root: Path, tmp_path: Path):
    """Every way of naming a file outside the static folder is refused with a JSON error."""
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    (static_root / "escape.txt").symlink_to(secret)
    for path in (
        "/static/../secret.txt",
        "/static/%2e%2e/secret.txt",
        "/static/..%2Fsecret.txt",
        "/static//" + str(secret).lstrip("/"),
        "/static/" + str(secret),
        "/static/sub/../../secret.txt",
        "/static/escape.txt",
        "/static/app.js%00.txt",
        "/static/.",
    ):
        answer = running.client.get(path)
        assert answer.status in (400, 404), path
        assert answer.body != b"secret", path
        assert answer.headers["Content-Type"].startswith("application/json"), path
    assert running.client.get("/static/app.js").status == 200


# ----- catalog -----


def test_catalog_shape_and_status_merge(running: Running, fake: FakeS3):
    """The catalog answer has the plan's shape, merges the store's status, and honors refresh."""
    root = running.client.get("/api/catalog?prefix=" + quote_key(ROOT))
    assert root.status == 200
    page = root.json()
    assert page["prefix"] == ROOT
    assert page["parent"] is None
    assert page["stale"] is False
    assert [folder["name"] for folder in page["folders"]] == ["2005 Annual", "2023Annual", "2024Annual"]
    assert page["folders"][1] == {"prefix": FOLDER_2023, "name": "2023Annual"}
    assert page["videos"] == []
    assert (running.cache_dir / "catalog.json").is_file()

    listed = running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024)).json()
    assert listed["parent"] == ROOT
    assert listed["folders"] == []
    assert [video["key"] for video in listed["videos"]] == [KEY_T1, KEY_T2]
    first = listed["videos"][0]
    assert first == {
        "key": KEY_T1,
        "name": "TCRMP20241022_video_FLC_T1.MP4",
        "size": BIG_SIZE,
        "ext": "mp4",
        "playable": True,
        "status": "new",
        "sightings": 0,
        "converted": "none",
    }

    mts = running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2023)).json()["videos"][0]
    assert mts["ext"] == "mts"
    assert mts["playable"] is False

    assert running.client.post("/api/videos/open", {"key": KEY_T1}).status == 200
    save_one(running)
    save_one(running, key=KEY_T2)
    assert running.client.post("/api/videos/done", {"key": KEY_T2, "done": True}).status == 200
    merged = {video["key"]: video for video in running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024)).json()["videos"]}
    assert (merged[KEY_T1]["status"], merged[KEY_T1]["sightings"]) == ("in progress", 1)
    assert (merged[KEY_T2]["status"], merged[KEY_T2]["sightings"]) == ("done", 1)

    listings_before = fake.count("GET", "")
    assert running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024)).status == 200
    assert fake.count("GET", "") == listings_before
    assert running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024) + "&refresh=1").status == 200
    assert fake.count("GET", "") == listings_before + 1

    assert running.client.get("/api/catalog").json()["prefix"] == ROOT


def test_catalog_rejects_bad_prefix(running: Running):
    """Prefixes outside the TCRMP folder, repeated parameters, and odd flags are 400."""
    for prefix in ("other/", "TCRMP_video_ondeck/2024Annual", "TCRMP_video_ondeck/../x/", "", "TCRMP_video_ondeck//x/"):
        answer = running.client.get("/api/catalog?prefix=" + quote_key(prefix))
        assert answer.status == 400, prefix
        assert answer.error.startswith("prefix:"), answer.error
    twice = running.client.get("/api/catalog?prefix=" + quote_key(ROOT) + "&prefix=" + quote_key(ROOT))
    assert twice.status == 400
    assert twice.error.startswith("prefix:")
    flag = running.client.get("/api/catalog?prefix=" + quote_key(ROOT) + "&refresh=maybe")
    assert flag.status == 400
    assert flag.error.startswith("refresh:")


def test_catalog_unreachable_bucket_gives_stale_page_or_502(running: Running, fake: FakeS3):
    """A lost bucket serves the saved listing as stale and 502 for a folder never listed."""
    assert running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024)).status == 200
    fake.stop()
    stale = running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024) + "&refresh=1")
    assert stale.status == 200
    assert stale.json()["stale"] is True
    assert [video["key"] for video in stale.json()["videos"]] == [KEY_T1, KEY_T2]
    unseen = running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2023))
    assert unseen.status == 502
    assert "listing" in unseen.error


# ----- species and settings -----


def test_species_and_settings_round_trip(running: Running):
    """Settings changes come back from the API, land in settings.json, and stamp new rows."""
    answer = running.client.get("/api/species")
    assert answer.status == 200
    data = answer.json()
    assert data["annotator"] == config.DEFAULT_ANNOTATOR
    assert data["tally_target"] == config.TALLY_TARGET
    assert data["species"][0] == {"code": "ACAU", "name": "Aplysina cauliformis", "part": "1"}
    assert len(data["species"]) == 38
    assert data["pins"] == {
        "1": "ACAU", "2": "AFUL", "3": "CDEL", "4": "MLAE", "5": "CPLI",
        "6": "ACRA", "7": "ACOM", "8": "XMUT", "9": None, "0": None,
    }

    changed = running.client.post("/api/settings", {"annotator": "AB"})
    assert changed.status == 200
    assert changed.json()["annotator"] == "AB"
    assert changed.json()["pins"] == data["pins"]

    pins = dict(data["pins"])
    pins["1"], pins["9"] = "ACLA", "ACAU"
    repinned = running.client.post("/api/settings", {"pins": pins})
    assert repinned.status == 200
    assert repinned.json()["pins"] == pins
    assert repinned.json()["annotator"] == "AB"
    assert set(repinned.json()) == {"species", "pins", "annotator", "tally_target"}

    again = running.client.get("/api/species").json()
    assert again["pins"] == pins
    assert again["annotator"] == "AB"
    stored = json.loads((running.data_dir / "settings.json").read_text(encoding="utf-8"))
    assert stored["annotator"] == "AB"
    assert stored["pins"] == pins
    assert stored["export_root"] == str(config.DEFAULT_EXPORT_ROOT)

    row = save_one(running)
    assert row["Annotator"] == "AB"


def test_settings_rejects_unknown_pin_code(running: Running):
    """A pin with an unknown species code is refused and the old pins stay."""
    pins = running.client.get("/api/species").json()["pins"]
    pins["1"] = "ZZZZ"
    answer = running.client.post("/api/settings", {"pins": pins})
    assert answer.status == 400
    assert answer.error.startswith("pins.1: unknown species code")
    assert running.client.get("/api/species").json()["pins"]["1"] == "ACAU"


@pytest.mark.parametrize(
    "body, prefix",
    [
        ({"annotator": ""}, "annotator:"),
        ({"annotator": "a b"}, "annotator:"),
        ({"annotator": 5}, "annotator:"),
        ({"pins": {"1": "ACAU"}}, "pins:"),
        ({"pins": []}, "pins:"),
        ({"annotater": "AB"}, "settings:"),
        ({}, "settings:"),
        ([], "body:"),
    ],
)
def test_settings_rejects_bad_fields(running: Running, body: Any, prefix: str):
    """Bad annotators, incomplete pins, unknown fields, and empty objects are 400 and save nothing."""
    answer = running.client.post("/api/settings", body)
    assert answer.status == 400
    assert answer.error.startswith(prefix), answer.error
    assert not (running.data_dir / "settings.json").exists()


# ----- video relay -----


def test_video_range_bytes_match_source(running: Running, fake: FakeS3):
    """Every Range form returns exactly the bytes of the object, then comes from disk."""
    body = fake._objects[KEY_T1]
    edge = config.CHUNK_SIZE
    for header, start, end in (
        ("bytes=0-0", 0, 0),
        (f"bytes={edge - 1}-{edge}", edge - 1, edge),
        ("bytes=100-200000", 100, 200000),
        ("bytes=-1000", BIG_SIZE - 1000, BIG_SIZE - 1),
        (f"bytes={2 * edge + 5}-", 2 * edge + 5, BIG_SIZE - 1),
        (f"bytes=10-{BIG_SIZE + 5000}", 10, BIG_SIZE - 1),
    ):
        answer = running.client.get("/video?key=" + quote_key(KEY_T1), headers={"Range": header})
        assert answer.status == 206, header
        assert answer.headers["Content-Range"] == f"bytes {start}-{end}/{BIG_SIZE}", header
        assert answer.headers["Content-Length"] == str(end - start + 1), header
        assert answer.headers["Accept-Ranges"] == "bytes"
        assert answer.headers["Content-Type"] == "video/mp4"
        assert answer.body == body[start:end + 1], header

    whole = running.client.get("/video?key=" + quote_key(KEY_T1), headers={"Range": "bytes=0-"})
    assert whole.body == body
    fetched = fake.count("GET", KEY_T1)
    again = running.client.get("/video?key=" + quote_key(KEY_T1), headers={"Range": "bytes=0-"})
    assert again.body == body
    assert fake.count("GET", KEY_T1) == fetched

    plus = running.client.get("/video?key=" + quote_key(KEY_PLUS), headers={"Range": "bytes=0-9"})
    assert plus.status == 206
    assert plus.body == fake._objects[KEY_PLUS][:10]


def test_video_without_range_streams_everything(running: Running, fake: FakeS3, capsys):
    """Without a Range header the whole object streams as 200 with the right type."""
    answer = running.client.get("/video?key=" + quote_key(KEY_T1))
    assert answer.status == 200
    assert answer.headers["Content-Length"] == str(BIG_SIZE)
    assert answer.headers["Accept-Ranges"] == "bytes"
    assert "Content-Range" not in answer.headers
    assert answer.body == fake._objects[KEY_T1]
    wait_for(lambda: f"200 " in capsys.readouterr().err, "the stream log line")

    other = running.client.get("/video?key=" + quote_key(KEY_MTS))
    assert other.status == 200
    assert other.headers["Content-Type"] == "application/octet-stream"
    assert other.body == fake._objects[KEY_MTS]


def test_video_bad_range_416(running: Running):
    """Malformed, reversed, multi, and out-of-object ranges are 416 with Content-Range bytes */size."""
    for header in (f"bytes={BIG_SIZE}-", f"bytes={BIG_SIZE + 10}-{BIG_SIZE + 20}", "bytes=5-2", "bytes=abc", "bytes=0-1,5-9", "bytes=-0"):
        answer = running.client.get("/video?key=" + quote_key(KEY_T1), headers={"Range": header})
        assert answer.status == 416, header
        assert answer.headers["Content-Range"] == f"bytes */{BIG_SIZE}", header
        assert answer.error.startswith("Range"), answer.error


def test_video_unknown_key_404(running: Running):
    """A key the bucket lacks is 404 with a JSON error that names it."""
    answer = running.client.get("/video?key=" + quote_key(KEY_MISSING))
    assert answer.status == 404
    assert KEY_MISSING in answer.error
    assert answer.headers["Content-Type"].startswith("application/json")


def test_video_key_outside_prefix_400(running: Running, fake: FakeS3):
    """Hostile or missing keys and bad flags are 400 before any bucket request."""
    requests_before = len(fake.requests)
    for query in (
        "key=other/x.MP4",
        "key=" + quote_key("TCRMP_video_ondeck/../x.MP4"),
        "key=" + quote_key("TCRMP_video_ondeck/folder/"),
        "key=",
        "",
        "key=" + quote_key(KEY_T1) + "&key=" + quote_key(KEY_T2),
        "key=" + quote_key(KEY_T1) + "&converted=yes",
    ):
        answer = running.client.get("/video?" + query)
        assert answer.status == 400, query
        assert answer.error.split(":")[0] in ("key", "converted"), answer.error
    assert len(fake.requests) == requests_before


def test_video_bucket_failure_mid_stream_ends_quietly(running: Running, fake: FakeS3, fast_backoff, capsys):
    """A chunk that fails after its retries ends the stream with one log line, and the next request recovers."""
    fake.fail_next(KEY_T1, 40)
    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=WAIT_SECONDS)
    connection.request("GET", "/video?key=" + quote_key(KEY_T1), headers={"Range": "bytes=0-"})
    response = connection.getresponse()
    assert response.status == 206
    with pytest.raises((http.client.IncompleteRead, ConnectionError)):
        response.read()
    connection.close()
    fake.fail_next(KEY_T1, 0)
    err = ""

    def logged() -> bool:
        """Collect stderr and tell whether the awaited log line has appeared."""
        nonlocal err
        err += capsys.readouterr().err
        return "failed after" in err

    wait_for(logged, "the relay failure log line")
    assert "Traceback" not in err
    assert running.client.get("/api/health").status == 200
    recovered = running.client.get("/video?key=" + quote_key(KEY_T1), headers={"Range": "bytes=0-"})
    assert recovered.status == 206
    assert recovered.body == fake._objects[KEY_T1]


# ----- observations -----


def test_save_observation_end_to_end(running: Running):
    """A posted sighting comes back as a row, reaches the CSV and both image files, and lists by key."""
    answer = running.client.post("/api/observations", observation())
    assert answer.status == 201, answer.body
    row = answer.json()["row"]
    assert row["ID"] == "ID001"
    assert row["SpeciesCode"] == "ACAU"
    assert row["Sponge Type"] == "Aplysina cauliformis"
    assert row["Site"] == "Flat Cay"
    assert row["Transect"] == "T1"
    assert row["Timestamp"] == "00:12"
    assert row["TimestampSeconds"] == "12.345"
    assert row["Quadrant"] == "BOTTOMLEFT"
    assert row["Notes"] == "bottom left, big one"
    assert row["PointX"] == "0.2500"
    assert row["BoxX"] == ""
    assert row["FrameFileName"] == "ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg"
    assert row["CropFileName"] == "ID001_ACAU_BOTTOMLEFT_FLC_T1.png"
    assert row["S3Key"] == KEY_T1
    assert row["Annotator"] == config.DEFAULT_ANNOTATOR

    csv_text = (running.data_dir / "observations.csv").read_text(encoding="utf-8")
    assert csv_text.startswith("Site,Transect,Sponge Type,Timestamp,Notes,ID,FileName,FrameFileName,AbbreviatedNote,")
    assert "ID001" in csv_text
    assert (running.data_dir / "frames" / row["FrameFileName"]).read_bytes() == JPEG
    assert (running.data_dir / "crops" / row["CropFileName"]).read_bytes() == PNG

    boxed = running.client.post(
        "/api/observations",
        observation(box={"x": 0.6, "y": 0.1, "w": 0.2, "h": 0.2}, note="", frame_jpeg="data:image/jpeg;base64," + b64(JPEG)),
    )
    assert boxed.status == 201
    assert boxed.json()["row"]["ID"] == "ID002"
    assert boxed.json()["row"]["Quadrant"] == "TOPRIGHT"
    assert boxed.json()["row"]["BoxW"] == "0.2000"
    assert boxed.json()["row"]["Notes"] == "top right"

    listed = running.client.get("/api/observations?key=" + quote_key(KEY_T1))
    assert listed.status == 200
    assert [item["ID"] for item in listed.json()["rows"]] == ["ID001", "ID002"]
    assert running.client.get("/api/observations?key=" + quote_key(KEY_T2)).json() == {"rows": []}


BAD_FIELDS = [
    ("key", "other/x.MP4", "key:"),
    ("key", None, "key:"),
    ("time", "soon", "time:"),
    ("time", -1, "time:"),
    ("time", True, "time:"),
    ("point", {"x": 2, "y": 0.5}, "point.x:"),
    ("point", {"x": 0.5}, "point.y:"),
    ("point", "here", "point:"),
    ("box", {"x": 0.9, "y": 0.1, "w": 0.5, "h": 0.2}, "box.w:"),
    ("box", "none", "box:"),
    ("species", "ZZZZ", "species:"),
    ("species", 7, "species:"),
    ("note", "n" * (config.MAX_NOTE_LENGTH + 1), "note:"),
    ("note", 12, "note:"),
    ("frame_jpeg", b64(b"not a jpeg at all"), "frame_jpeg:"),
    ("frame_jpeg", "@@@ not base64 @@@", "frame_jpeg:"),
    ("frame_jpeg", "data:image/jpeg,plain-text-data-url", "frame_jpeg:"),
    ("frame_jpeg", "", "frame_jpeg:"),
    ("frame_jpeg", 5, "frame_jpeg:"),
    ("crop_png", b64(b"not a png at all"), "crop_png:"),
    ("crop_png", None, "crop_png:"),
]


@pytest.mark.parametrize("field, value, prefix", BAD_FIELDS)
def test_save_rejects_each_bad_field_with_named_error(running: Running, field: str, value: Any, prefix: str):
    """Each bad field is refused with an error that names it, and nothing is written."""
    answer = running.client.post("/api/observations", observation(**{field: value}))
    assert answer.status == 400, answer.body
    assert answer.error.startswith(prefix), answer.error
    assert not (running.data_dir / "observations.csv").exists()
    assert list((running.data_dir / "frames").iterdir()) == []


@pytest.mark.parametrize("field", ["key", "time", "point", "species", "frame_jpeg", "crop_png"])
def test_save_rejects_missing_field(running: Running, field: str):
    """A missing required field is refused as '<field>: missing'."""
    body = observation()
    del body[field]
    answer = running.client.post("/api/observations", body)
    assert answer.status == 400
    assert answer.error == f"{field}: missing"


def test_save_without_box_and_note_fields_uses_defaults(running: Running):
    """A body without box and note saves with no box and an empty note."""
    body = observation()
    del body["box"]
    del body["note"]
    answer = running.client.post("/api/observations", body)
    assert answer.status == 201
    assert answer.json()["row"]["Notes"] == "bottom left"
    assert answer.json()["row"]["BoxX"] == ""


def test_observations_requires_key(running: Running):
    """Listing sightings needs a valid key."""
    answer = running.client.get("/api/observations")
    assert answer.status == 400
    assert answer.error == "key: missing"
    bad = running.client.get("/api/observations?key=other/x.MP4")
    assert bad.status == 400
    assert bad.error.startswith("key:")


def test_delete_observation_and_unknown_id_404(running: Running):
    """Deleting moves the images to the trash, an unknown ID is 404, and a malformed ID is 400."""
    row = save_one(running)
    frames = running.data_dir / "frames"
    trash = running.data_dir / "trash"
    deleted = running.client.delete("/api/observations/ID001")
    assert deleted.status == 200
    assert deleted.json()["deleted"]["ID"] == "ID001"
    assert deleted.json()["deleted"]["FrameFileName"] == row["FrameFileName"]
    assert list(frames.iterdir()) == []
    assert sorted(path.name[-len(row["FrameFileName"]):] for path in trash.iterdir()) == sorted(
        [row["FrameFileName"], row["CropFileName"]]
    )
    assert running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json() == {"rows": []}

    again = running.client.delete("/api/observations/ID001")
    assert again.status == 404
    assert "ID001" in again.error

    malformed = running.client.delete("/api/observations/bogus")
    assert malformed.status == 400
    assert malformed.error.startswith("obs_id:")
    assert running.client.delete("/api/observations/").status == 404
    assert running.client.delete("/api/observations").status == 405


def test_tally(running: Running):
    """The tally counts sightings, videos, and the earliest year per species, plus the total."""
    empty = running.client.get("/api/tally")
    assert empty.status == 200
    assert empty.json() == {"tally": [], "total": 0}
    save_one(running)
    save_one(running, key=KEY_2016)
    save_one(running, species="AFUL")
    answer = running.client.get("/api/tally").json()
    assert answer["total"] == 3
    assert answer["tally"] == [
        {"code": "ACAU", "name": "Aplysina cauliformis", "sightings": 2, "videos": 2, "earliest_year": 2016},
        {"code": "AFUL", "name": "Aplysina fulva", "sightings": 1, "videos": 1, "earliest_year": 2024},
    ]


# ----- screened videos -----


def test_open_and_done(running: Running):
    """Opening and finishing a video keeps the screened row with the pinned target species."""
    opened = running.client.post("/api/videos/open", {"key": KEY_T1})
    assert opened.status == 200
    video = opened.json()["video"]
    assert video["S3Key"] == KEY_T1
    assert video["FileName"] == "TCRMP20241022_video_FLC_T1.MP4"
    assert video["Status"] == "in progress"
    assert video["TargetSpecies"] == "ACAU;AFUL;CDEL;MLAE;CPLI;ACRA;ACOM;XMUT"
    assert video["Annotator"] == config.DEFAULT_ANNOTATOR
    assert video["FirstOpened"].endswith("Z")
    assert video["MarkedDone"] == ""
    assert video["Sightings"] == "0"

    save_one(running)
    pins = running.client.get("/api/species").json()["pins"]
    pins["9"] = "ACLA"
    assert running.client.post("/api/settings", {"pins": pins}).status == 200
    done = running.client.post("/api/videos/done", {"key": KEY_T1, "done": True})
    assert done.status == 200
    assert done.json()["video"]["Status"] == "done"
    assert done.json()["video"]["MarkedDone"].endswith("Z")
    assert done.json()["video"]["TargetSpecies"] == "ACAU;AFUL;CDEL;MLAE;CPLI;ACRA;ACOM;XMUT;ACLA"
    assert done.json()["video"]["Sightings"] == "1"
    assert done.json()["video"]["FirstOpened"] == video["FirstOpened"]

    reopened = running.client.post("/api/videos/done", {"key": KEY_T1, "done": False}).json()["video"]
    assert reopened["Status"] == "in progress"
    assert reopened["MarkedDone"] == ""
    screened = (running.data_dir / "videos_screened.csv").read_text(encoding="utf-8")
    assert screened.startswith("S3Key,FileName,Status,TargetSpecies,Sightings,Annotator,FirstOpened,MarkedDone\n")
    assert KEY_T1 in screened

    for body, prefix in (
        ({"key": KEY_T1}, "done:"),
        ({"key": KEY_T1, "done": "yes"}, "done:"),
        ({"done": True}, "key:"),
        ({"key": "other/x.MP4", "done": True}, "key:"),
    ):
        answer = running.client.post("/api/videos/done", body)
        assert answer.status == 400, body
        assert answer.error.startswith(prefix), answer.error
    assert running.client.post("/api/videos/open", {}).error == "key: missing"


# ----- media -----


def test_media_route_serves_images_and_blocks_traversal(running: Running, tmp_path: Path):
    """Frames and crops are served by name, and every escape from the image folders is refused."""
    row = save_one(running)
    frame = running.client.get("/media/frames/" + row["FrameFileName"])
    assert frame.status == 200
    assert frame.headers["Content-Type"] == "image/jpeg"
    assert frame.headers["Content-Length"] == str(len(JPEG))
    assert frame.body == JPEG
    crop = running.client.get("/media/crops/" + row["CropFileName"])
    assert crop.status == 200
    assert crop.headers["Content-Type"] == "image/png"
    assert crop.body == PNG

    (running.data_dir / "frames" / "escape.jpg").symlink_to(running.data_dir / "observations.csv")
    for path in (
        "/media/frames/../observations.csv",
        "/media/frames/..%2Fobservations.csv",
        "/media/frames/..",
        "/media/frames/.",
        "/media/frames/" + str(running.data_dir / "observations.csv"),
        "/media/frames/a%20b.jpg",
        "/media/frames/escape.jpg",
        "/media/crops/" + row["FrameFileName"],
        "/media/frames/" + row["CropFileName"],
        "/media/other/" + row["FrameFileName"],
        "/media/frames/",
    ):
        answer = running.client.get(path)
        assert answer.status in (400, 404), path
        assert b"Site,Transect" not in answer.body, path


# ----- request rules -----


def test_mutations_require_header(running: Running):
    """POST and DELETE without X-Screener: 1 are 403 and change nothing."""
    for method, path, body in (
        ("POST", "/api/settings", {"annotator": "AB"}),
        ("POST", "/api/videos/open", {"key": KEY_T1}),
        ("POST", "/api/export", {}),
        ("DELETE", "/api/observations/ID001", None),
    ):
        refused = running.client.request(method, path, body, mutation_header=False)
        assert refused.status == 403, (method, path)
        assert refused.error.startswith("X-Screener:"), refused.error
        wrong = running.client.request(method, path, body, headers={"X-Screener": "2"}, mutation_header=False)
        assert wrong.status == 403, (method, path)
    assert running.client.get("/api/species").json()["annotator"] == config.DEFAULT_ANNOTATOR
    assert running.client.get("/api/health", mutation_header=False).status == 200
    assert running.client.post("/api/videos/open", {"key": KEY_T1}).status == 200


def test_foreign_host_header_403(running: Running):
    """Only 127.0.0.1:<port> and localhost:<port> pass the Host check."""
    for host in ("evil.example", f"evil.example:{running.port}", f"127.0.0.1:{running.port + 1}", "127.0.0.1", "localhost", None):
        answer = running.client.get("/api/health", host=host)
        assert answer.status == 403, host
        assert answer.error.startswith("Host:"), answer.error
    for host in (f"127.0.0.1:{running.port}", f"localhost:{running.port}", f"LOCALHOST:{running.port}"):
        assert running.client.get("/api/health", host=host).status == 200, host
    posted = running.client.post("/api/videos/open", {"key": KEY_T1}, host=f"evil.example:{running.port}")
    assert posted.status == 403
    assert not (running.data_dir / "videos_screened.csv").exists()


def test_malformed_json_400(running: Running):
    """Broken JSON, non-objects, non-UTF-8, chunked bodies, and bad lengths are refused with the right status."""
    broken = running.client.post("/api/settings", b"{not json", headers={"Content-Type": "application/json"})
    assert broken.status == 400
    assert broken.error.startswith("body:")
    not_object = running.client.post("/api/settings", b"[1, 2]")
    assert not_object.status == 400
    assert not_object.error.startswith("body:")
    not_text = running.client.post("/api/settings", b"\xff\xfe\x00")
    assert not_text.status == 400
    assert not_text.error.startswith("body:")
    empty = running.client.post("/api/export", b"")
    assert empty.status == 400
    assert not empty.error.startswith("body:")
    assert "export" in empty.error

    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=WAIT_SECONDS)
    connection.putrequest("POST", "/api/settings")
    connection.putheader("X-Screener", "1")
    connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    chunked = connection.getresponse()
    assert chunked.status == 411
    assert json.loads(chunked.read())["error"].startswith("body:")
    connection.close()

    for length in ("abc", "-5"):
        connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=WAIT_SECONDS)
        connection.putrequest("POST", "/api/settings")
        connection.putheader("X-Screener", "1")
        connection.putheader("Content-Length", length)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400, length
        connection.close()

    no_length = running.client.request("POST", "/api/settings")
    assert no_length.status == 411


def test_oversized_body_413(running: Running):
    """A body above the limit is refused before it is read, and the connection closes."""
    connection = http.client.HTTPConnection("127.0.0.1", running.port, timeout=WAIT_SECONDS)
    connection.putrequest("POST", "/api/observations")
    connection.putheader("X-Screener", "1")
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Content-Length", str(config.MAX_BODY_BYTES + 1))
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413
    assert response.headers["Connection"] == "close"
    error = json.loads(response.read())["error"]
    assert error.startswith("body:")
    assert str(config.MAX_BODY_BYTES) in error
    connection.close()
    assert running.client.get("/api/health").status == 200


def test_client_disconnect_mid_stream_leaves_server_healthy(running: Running, fake: FakeS3, capsys):
    """A client that hangs up mid-stream leaves one quiet log line and a working server."""
    for _ in range(3):
        sock = socket.create_connection(("127.0.0.1", running.port), timeout=WAIT_SECONDS)
        sock.sendall(
            f"GET /video?key={quote_key(KEY_T1)} HTTP/1.1\r\nHost: 127.0.0.1:{running.port}\r\nRange: bytes=0-\r\n\r\n".encode("ascii")
        )
        head = sock.recv(4096)
        assert head.startswith(b"HTTP/1.1 206")
        sock.close()
    err = ""

    def logged() -> bool:
        """Collect stderr and tell whether the awaited log line has appeared."""
        nonlocal err
        err += capsys.readouterr().err
        return err.count("client disconnected") >= 3

    wait_for(logged, "three disconnect log lines")
    assert "Traceback" not in err
    assert running.client.get("/api/health").status == 200
    whole = running.client.get("/video?key=" + quote_key(KEY_T1), headers={"Range": "bytes=0-"})
    assert whole.body == fake._objects[KEY_T1]


def test_unexpected_exception_gives_500_and_server_survives(running: Running, monkeypatch, capsys):
    """An unexpected exception answers 500 with JSON, prints a traceback, and the server keeps running."""
    def boom() -> None:
        """Stand in for a store method that fails unexpectedly."""
        raise RuntimeError("the tally exploded")

    monkeypatch.setattr(running.app.store, "tally", boom)
    answer = running.client.get("/api/tally")
    assert answer.status == 500
    assert "the tally exploded" in answer.error
    err = ""

    def traced() -> bool:
        """Collect stderr and tell whether the traceback has appeared."""
        nonlocal err
        err += capsys.readouterr().err
        return "Traceback" in err and "the tally exploded" in err

    wait_for(traced, "the traceback on stderr")
    assert running.client.get("/api/health").status == 200


# ----- conversion and prefetch -----


def test_convert_routes(running: Running, fake: FakeS3, tmp_path: Path):
    """A conversion runs through the relay route, the converted MP4 serves with ranges, and the catalog reports it."""
    ffmpeg = find_tool("ffmpeg")
    clip = tmp_path / "clip.ts"
    made = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-f", "mpegts", str(clip)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert made.returncode == 0, made.stderr.decode()
    fake.put(KEY_TS, clip.read_bytes())

    before = running.client.get("/api/convert?key=" + quote_key(KEY_TS))
    assert before.status == 200
    assert before.json() == {"state": "none", "progress": 0.0, "message": ""}
    not_yet = running.client.get("/video?key=" + quote_key(KEY_TS) + "&converted=1")
    assert not_yet.status == 404
    assert "converted" in not_yet.error

    started = running.client.post("/api/convert", {"key": KEY_TS})
    assert started.status == 200
    assert started.json()["state"] in ("queued", "running", "done")
    assert set(started.json()) == {"state", "progress", "message"}

    def finished() -> bool:
        """Tell whether the conversion reached done, failing fast on a failure."""
        status = running.client.get("/api/convert?key=" + quote_key(KEY_TS)).json()
        assert status["state"] != "failed", status["message"]
        return status["state"] == "done"

    wait_for(finished, "the conversion to finish")
    final = running.client.get("/api/convert?key=" + quote_key(KEY_TS)).json()
    assert final == {"state": "done", "progress": 1.0, "message": ""}
    assert running.client.post("/api/convert", {"key": KEY_TS}).json()["state"] == "done"

    converted = running.cache_dir / "converted" / (cache_id(KEY_TS) + ".mp4")
    assert converted.is_file()
    expected = converted.read_bytes()
    whole = running.client.get("/video?key=" + quote_key(KEY_TS) + "&converted=1")
    assert whole.status == 200
    assert whole.headers["Content-Type"] == "video/mp4"
    assert whole.headers["Accept-Ranges"] == "bytes"
    assert whole.headers["Content-Length"] == str(len(expected))
    assert whole.body == expected
    part = running.client.get("/video?key=" + quote_key(KEY_TS) + "&converted=1", headers={"Range": "bytes=10-99"})
    assert part.status == 206
    assert part.headers["Content-Range"] == f"bytes 10-99/{len(expected)}"
    assert part.body == expected[10:100]
    tail = running.client.get("/video?key=" + quote_key(KEY_TS) + "&converted=1", headers={"Range": "bytes=-7"})
    assert tail.body == expected[-7:]
    bad = running.client.get("/video?key=" + quote_key(KEY_TS) + "&converted=1", headers={"Range": f"bytes={len(expected)}-"})
    assert bad.status == 416
    assert bad.headers["Content-Range"] == f"bytes */{len(expected)}"

    listed = running.client.get("/api/catalog?prefix=" + quote_key(ROOT + "2012Annual/")).json()["videos"][0]
    assert listed["playable"] is False
    assert listed["converted"] == "done"

    for query in ("key=other/x.mts", "key="):
        for method in ("GET", "POST"):
            if method == "GET":
                answer = running.client.get("/api/convert?" + query)
            else:
                answer = running.client.post("/api/convert", {"key": query.split("=", 1)[1]})
            assert answer.status == 400, (method, query)
            assert answer.error.startswith("key:"), answer.error


def test_prefetch_routes(running: Running, fake: FakeS3):
    """Prefetch fills the chunk cache to 1.0 and reports 0.0 without any bucket request for a cold key."""
    heads = fake.count("HEAD")
    cold = running.client.get("/api/prefetch?key=" + quote_key(KEY_T2))
    assert cold.status == 200
    assert cold.json() == {"cached": 0.0}
    assert fake.count("HEAD") == heads

    started = running.client.post("/api/prefetch", {"key": KEY_T2})
    assert started.status == 200
    assert 0.0 <= started.json()["cached"] <= 1.0

    def cached() -> bool:
        """Tell whether the whole object is on disk."""
        return running.client.get("/api/prefetch?key=" + quote_key(KEY_T2)).json()["cached"] == 1.0

    wait_for(cached, "the prefetch to finish")
    chunk = running.cache_dir / "chunks" / cache_id(KEY_T2) / "000000.bin"
    assert chunk.read_bytes() == fake._objects[KEY_T2]

    unknown = running.client.post("/api/prefetch", {"key": KEY_MISSING})
    assert unknown.status == 404
    assert unknown.error.startswith("size of")
    for answer in (
        running.client.get("/api/prefetch?key=other/x.MP4"),
        running.client.post("/api/prefetch", {"key": 5}),
        running.client.post("/api/prefetch", {}),
    ):
        assert answer.status == 400
        assert answer.error.startswith("key:"), answer.error


# ----- export -----


def test_export_route_writes_package(make_app, tmp_path: Path):
    """Export writes the dated package into the folder from settings.json and counts the rows."""
    export_root = tmp_path / "exports"
    export_root.mkdir()
    running = make_app(settings={"export_root": str(export_root)})
    assert running.client.get("/api/species").json()["annotator"] == config.DEFAULT_ANNOTATOR

    empty = running.client.post("/api/export", {})
    assert empty.status == 400
    assert "no sightings" in empty.error

    row = save_one(running)
    answer = running.client.post("/api/export", {})
    assert answer.status == 200, answer.body
    package = Path(answer.json()["path"])
    assert package == export_root / ("spongeGroundTruth_" + date.today().strftime("%Y%m%d"))
    assert answer.json()["observations"] == 1
    assert (package / "observations.csv").is_file()
    assert (package / "videos_screened.csv").is_file()
    assert (package / "README.md").is_file()
    assert (package / "frames" / row["FrameFileName"]).read_bytes() == JPEG
    assert (package / "crops" / row["CropFileName"]).read_bytes() == PNG

    second = running.client.post("/api/export", {})
    assert second.status == 200
    assert Path(second.json()["path"]).name == package.name + "_2"


def test_export_missing_root_400(make_app, tmp_path: Path):
    """A missing export folder is the user's problem and answers 400 with the path."""
    running = make_app(settings={"export_root": str(tmp_path / "not-mounted")})
    save_one(running)
    answer = running.client.post("/api/export", {})
    assert answer.status == 400
    assert "not-mounted" in answer.error
    assert "export folder not found" in answer.error


# ----- routing -----


def test_unknown_route_404(running: Running):
    """Unknown paths are 404, wrong methods are 405 with Allow, and unsupported methods are 501, all as JSON."""
    for method, path in (("GET", "/nope"), ("GET", "/api/nope"), ("POST", "/api/nope"), ("GET", "/video/extra"), ("GET", "/media/frames")):
        answer = running.client.request(method, path, {} if method == "POST" else None)
        assert answer.status == 404, (method, path)
        assert answer.headers["Content-Type"].startswith("application/json")
        assert path in answer.error
    wrong_method = running.client.post("/api/health", {})
    assert wrong_method.status == 405
    assert wrong_method.headers["Allow"] == "GET"
    assert running.client.request("DELETE", "/api/species").status == 405
    only_delete = running.client.get("/api/observations/ID001")
    assert only_delete.status == 405
    assert only_delete.headers["Allow"] == "DELETE"
    unsupported = running.client.request("PUT", "/api/health", b"x", headers={"X-Screener": "1"})
    assert unsupported.status == 501
    assert unsupported.headers["Content-Type"].startswith("application/json")
    assert running.client.get("/api/health").status == 200


def test_app_rejects_bad_arguments(tmp_path: Path, static_root: Path, fake: FakeS3):
    """ScreenerApp and make_server refuse bad ports, folders, URLs, and non-loopback hosts."""
    with pytest.raises(ValueError, match="port"):
        ScreenerApp(tmp_path / "d", tmp_path / "c", CONFIG_DIR, static_root, fake.url, 70000)
    with pytest.raises(ValueError, match="static_dir"):
        ScreenerApp(tmp_path / "d", tmp_path / "c", CONFIG_DIR, tmp_path / "missing", fake.url, 0)
    with pytest.raises(ValueError, match="bucket_url"):
        ScreenerApp(tmp_path / "d", tmp_path / "c", CONFIG_DIR, static_root, "ftp://x", 0)
    with pytest.raises(FileNotFoundError):
        ScreenerApp(tmp_path / "d", tmp_path / "c", tmp_path / "no-config", static_root, fake.url, 0)
    app = ScreenerApp(tmp_path / "d", tmp_path / "c", CONFIG_DIR, static_root, fake.url, 0)
    try:
        with pytest.raises(ValueError, match="host"):
            make_server(app, "0.0.0.0", 0)
    finally:
        app.close()
    app.close()


# ----- entry point -----


def load_entry_point():
    """Import screener.py (the script, not the package) as a module."""
    spec = importlib.util.spec_from_file_location("screener_entry_point", ENTRY_POINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM], ids=["sigint", "sigterm"])
def test_main_prints_bound_port_and_stops_on_sigint(fake: FakeS3, tmp_path: Path, stop_signal: int):
    """screener.py prints the bound port first, serves, and exits 0 on Ctrl+C or a plain kill."""
    process = subprocess.Popen(
        [sys.executable, str(ENTRY_POINT), "--port", "0", "--no-browser",
         "--data-dir", str(tmp_path / "data"), "--cache-dir", str(tmp_path / "cache"), "--bucket-url", fake.url],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = process.stdout.readline()
        assert line.startswith("Sponge Screener listening on http://127.0.0.1:"), line
        assert line.endswith("/\n")
        port = int(line.rsplit(":", 1)[1].rstrip("/\n"))
        assert port > 0
        client = Client(port)
        assert client.get("/api/health").json()["ok"] is True
        assert client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024)).status == 200
        process.send_signal(stop_signal)
        code = process.wait(timeout=WAIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=WAIT_SECONDS)
    assert code == 0
    stderr = process.stderr.read()
    assert "Traceback" not in stderr
    assert "GET /api/health 200 " in stderr
    assert (tmp_path / "data").is_dir()
    assert (tmp_path / "cache" / "chunks").is_dir()


def test_main_rejects_bad_port_and_reports_port_in_use(fake: FakeS3, tmp_path: Path, capsys):
    """A port outside the range exits 2, and a taken port returns 1 with a plain message."""
    entry = load_entry_point()
    with pytest.raises(SystemExit) as stopped:
        entry.main(["--port", "70000", "--no-browser"])
    assert stopped.value.code == 2

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    try:
        code = entry.main([
            "--port", str(port), "--no-browser",
            "--data-dir", str(tmp_path / "data"), "--cache-dir", str(tmp_path / "cache"), "--bucket-url", fake.url,
        ])
    finally:
        taken.close()
    assert code == 1
    err = capsys.readouterr().err
    assert str(port) in err
    assert "Traceback" not in err
