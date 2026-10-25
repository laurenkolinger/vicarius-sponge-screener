"""Tests for screener.server and the screener.py entry point.

Every test starts a real ThreadingHTTPServer on a free port of 127.0.0.1 with
temporary data and cache folders, a tiny static folder built here (so the
front end's files are never needed), the project's config folder, and a FakeS3
as the bucket. Requests go through http.client or a raw socket. The entry point
tests run screener.py as a subprocess, the way the end-to-end test does.
"""

import base64
import csv
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
            method: GET, POST, PATCH, DELETE, or another method.
            path: The path and query string.
            body: None, a dict (sent as JSON), or bytes (sent as is).
            headers: Extra headers.
            mutation_header: False leaves out ``X-Screener: 1`` on POST, PATCH, and DELETE.
            host: "" sends the normal Host header, None sends no Host header,
                and other text is sent as the Host header.
        """
        sent = dict(headers or {})
        if method in ("POST", "PATCH", "DELETE") and mutation_header:
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

    def patch(self, path: str, body: Any = None, **kwargs: Any) -> Answer:
        """Send a PATCH with a JSON body (an empty object by default)."""
        return self.request("PATCH", path, body if body is not None else {}, **kwargs)

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
    """Build running apps on the fake bucket, and stop them all afterward.

    Tracking is off unless a test passes a Python and a worker: the default
    Python path does not exist, so no background job reads the fake videos.
    """
    made: List[Running] = []

    def build(
        settings: Optional[Dict[str, Any]] = None,
        bucket_url: Optional[str] = None,
        track_python: Optional[Path] = None,
        track_worker: Path = config.TRACK_WORKER,
    ) -> Running:
        """Start one app on fresh data and cache folders, with optional settings.json contents."""
        data_dir = tmp_path / f"data{len(made)}"
        cache_dir = tmp_path / f"cache{len(made)}"
        if settings is not None:
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        python = tmp_path / "no-venv" / "bin" / "python" if track_python is None else track_python
        app = ScreenerApp(
            data_dir, cache_dir, CONFIG_DIR, static_root, bucket_url or fake.url, 0, track_python=python, track_worker=track_worker
        )
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
        "unlabeled": 0,
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
    assert row["LabelStatus"] == "labeled"
    assert answer.json()["reopened"] is False

    csv_text = (running.data_dir / "observations.csv").read_text(encoding="utf-8")
    assert csv_text.startswith("Site,Transect,Sponge Type,Timestamp,Notes,ID,FileName,FrameFileName,AbbreviatedNote,")
    assert csv_text.splitlines()[0].endswith(",LoggedAt,LabelStatus")
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
    assert empty.json() == {"tally": [], "total": 0, "unlabeled": 0}
    save_one(running)
    save_one(running, key=KEY_2016)
    save_one(running, species="AFUL")
    answer = running.client.get("/api/tally").json()
    assert answer["total"] == 3
    assert answer["unlabeled"] == 0
    assert answer["tally"] == [
        {"code": "ACAU", "name": "Aplysina cauliformis", "sightings": 2, "videos": 2, "earliest_year": 2016},
        {"code": "AFUL", "name": "Aplysina fulva", "sightings": 1, "videos": 1, "earliest_year": 2024},
    ]


def save_later(running: Running, **overrides: Any) -> Dict[str, str]:
    """Save one Later point (a null species) through the API and return its row."""
    return save_one(running, species=None, **overrides)


def test_post_observation_null_species_saves_later_point(running: Running):
    """A null species saves a Later point: blank species, LATER file names, LabelStatus unlabeled."""
    answer = running.client.post("/api/observations", observation(species=None))
    assert answer.status == 201, answer.body
    row = answer.json()["row"]
    assert list(row) == [
        "Site", "Transect", "Sponge Type", "Timestamp", "Notes", "ID", "FileName", "FrameFileName", "AbbreviatedNote",
        "SpeciesCode", "TimestampSeconds", "Quadrant", "PointX", "PointY", "BoxX", "BoxY", "BoxW", "BoxH",
        "CropFileName", "S3Key", "Annotator", "LoggedAt", "LabelStatus",
    ]  # fmt: skip
    assert answer.json()["reopened"] is False
    assert set(answer.json()) == {"row", "reopened"}
    assert row["ID"] == "ID001"
    assert (row["SpeciesCode"], row["Sponge Type"], row["LabelStatus"]) == ("", "", "unlabeled")
    assert row["FrameFileName"] == "ID001_LATER_BOTTOMLEFT_FLC_T1.jpg"
    assert row["CropFileName"] == "ID001_LATER_BOTTOMLEFT_FLC_T1.png"
    assert (running.data_dir / "frames" / row["FrameFileName"]).read_bytes() == JPEG
    assert (running.data_dir / "crops" / row["CropFileName"]).read_bytes() == PNG
    assert running.client.get("/media/crops/" + row["CropFileName"]).body == PNG
    listed = running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json()["rows"]
    assert listed == [row]
    missing_images = running.client.post("/api/observations", observation(species=None, crop_png=None))
    assert missing_images.status == 400 and missing_images.error.startswith("crop_png:")


def test_patch_relabels_row(running: Running):
    """PATCH with a code relabels the row, renames both images, and answers with the new row."""
    first = save_one(running)
    answer = running.client.patch("/api/observations/ID001", {"species": "CDEL"})
    assert answer.status == 200, answer.body
    row = answer.json()["row"]
    assert row["SpeciesCode"] == "CDEL"
    assert row["Sponge Type"] == "Cliona delitrix"
    assert row["LabelStatus"] == "labeled"
    assert row["FrameFileName"] == "ID001_CDEL_BOTTOMLEFT_FLC_T1.jpg"
    assert row["CropFileName"] == "ID001_CDEL_BOTTOMLEFT_FLC_T1.png"
    for column in ("ID", "LoggedAt", "Notes", "TimestampSeconds", "PointX", "PointY", "S3Key", "Annotator"):
        assert row[column] == first[column], column
    assert running.client.get("/media/crops/" + row["CropFileName"]).body == PNG
    assert running.client.get("/media/frames/" + row["FrameFileName"]).body == JPEG
    assert running.client.get("/media/crops/" + first["CropFileName"]).status == 404
    assert running.client.get("/media/frames/" + first["FrameFileName"]).status == 404
    assert running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json() == {"rows": [row]}
    assert answer.json() == {"row": row, "reopened": False}
    assert running.client.patch("/api/observations/ID001", {"species": "CDEL"}).json() == {"row": row, "reopened": False}


def test_patch_null_unlabels_row(running: Running):
    """PATCH with null makes the row a Later point, and a later PATCH with a code labels it again."""
    save_one(running)
    answer = running.client.patch("/api/observations/ID001", {"species": None})
    assert answer.status == 200, answer.body
    row = answer.json()["row"]
    assert (row["SpeciesCode"], row["Sponge Type"], row["LabelStatus"]) == ("", "", "unlabeled")
    assert row["FrameFileName"] == "ID001_LATER_BOTTOMLEFT_FLC_T1.jpg"
    assert row["CropFileName"] == "ID001_LATER_BOTTOMLEFT_FLC_T1.png"
    assert running.client.get("/media/crops/" + row["CropFileName"]).body == PNG
    assert running.client.get("/api/tally").json() == {"tally": [], "total": 0, "unlabeled": 1}
    again = running.client.patch("/api/observations/ID001", {"species": "AFUL"}).json()["row"]
    assert (again["SpeciesCode"], again["LabelStatus"]) == ("AFUL", "labeled")
    assert again["CropFileName"] == "ID001_AFUL_BOTTOMLEFT_FLC_T1.png"


def test_patch_unknown_id_404(running: Running):
    """An ID that no row has is 404 and names the ID, whatever the label."""
    save_one(running)
    for species in ("ACAU", "CDEL", None):
        answer = running.client.patch("/api/observations/ID009", {"species": species})
        assert answer.status == 404, species
        assert "ID009" in answer.error
    assert running.client.patch("/api/observations/ID001/extra", {"species": None}).status == 404
    assert running.client.patch("/api/observations/", {"species": None}).status == 404
    assert running.client.patch("/api/observations", {"species": None}).status == 405


def test_patch_bad_id_400(running: Running):
    """An ID that is not ID plus digits is 400 and starts with obs_id."""
    save_one(running)
    for bad in ("bogus", "id001", "ID", "ID-1", "ID%201", "001"):
        answer = running.client.patch("/api/observations/" + bad, {"species": None})
        assert answer.status == 400, bad
        assert answer.error.startswith("obs_id:"), answer.error
    assert running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json()["rows"][0]["SpeciesCode"] == "ACAU"


def test_patch_missing_species_field_400(running: Running):
    """A body without species is 400 'species: missing', even when it carries other fields."""
    save_one(running)
    for body in ({}, {"label": "CDEL"}, {"Species": "CDEL"}):
        answer = running.client.patch("/api/observations/ID001", body)
        assert answer.status == 400, body
        assert answer.error == "species: missing"
    assert running.client.patch("/api/observations/ID009", {}).status == 400


@pytest.mark.parametrize("species", [7, 1.5, True, ["CDEL"], {"code": "CDEL"}])
def test_patch_number_species_400(running: Running, species: Any):
    """A species that is neither text nor null is 400 and starts with species."""
    row = save_one(running)
    answer = running.client.patch("/api/observations/ID001", {"species": species})
    assert answer.status == 400, answer.body
    assert answer.error.startswith("species:"), answer.error
    assert running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json() == {"rows": [row]}


@pytest.mark.parametrize("species", ["ZZZZ", "acau", "", "../ACAU", "ACAU/../ACAU", "CDEL "])
def test_patch_unknown_code_400(running: Running, species: str):
    """An unknown code is 400 and starts with species, and nothing is renamed."""
    row = save_one(running)
    answer = running.client.patch("/api/observations/ID001", {"species": species})
    assert answer.status == 400, answer.body
    assert answer.error.startswith("species:"), answer.error
    assert running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json() == {"rows": [row]}
    assert (running.data_dir / "frames" / row["FrameFileName"]).read_bytes() == JPEG
    assert list((running.data_dir / "trash").iterdir()) == []


def test_patch_without_mutation_header_403(running: Running):
    """PATCH needs X-Screener: 1 like every write, and a refused PATCH changes nothing."""
    row = save_one(running)
    refused = running.client.patch("/api/observations/ID001", {"species": None}, mutation_header=False)
    assert refused.status == 403
    assert refused.error.startswith("X-Screener:")
    wrong = running.client.patch(
        "/api/observations/ID001", {"species": None}, headers={"X-Screener": "2"}, mutation_header=False
    )
    assert wrong.status == 403
    assert running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json() == {"rows": [row]}


def test_patch_body_not_object_400(running: Running):
    """A body that is not a JSON object is 400 'body:', and a PATCH without a body is 411."""
    row = save_one(running)
    for body in (b'"CDEL"', b"[1, 2]", b"null", b"5", b"{not json", b"\xff\xfe\x00"):
        answer = running.client.patch("/api/observations/ID001", body, headers={"Content-Type": "application/json"})
        assert answer.status == 400, body
        assert answer.error.startswith("body:"), answer.error
    assert running.client.request("PATCH", "/api/observations/ID001").status == 411
    assert running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json() == {"rows": [row]}


def video_state(running: Running, key: str = KEY_T1) -> Dict[str, Any]:
    """Read the catalog entry of one video in the 2024 folder."""
    listed = running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024)).json()["videos"]
    return next(video for video in listed if video["key"] == key)


def mark_done(running: Running, key: str = KEY_T1) -> None:
    """Mark a video done through the API and check that it took."""
    answer = running.client.post("/api/videos/done", {"key": key, "done": True})
    assert answer.status == 200, answer.body
    assert video_state(running, key)["status"] == "done"


def test_post_later_point_on_done_video_reports_reopened(running: Running):
    """A Later point saved into a done video moves it back to in progress and says so; a second one does not."""
    save_one(running)
    save_one(running, key=KEY_T2)
    mark_done(running)
    mark_done(running, KEY_T2)
    first = running.client.post("/api/observations", observation(species=None))
    assert first.status == 201, first.body
    assert first.json()["reopened"] is True
    assert set(first.json()) == {"row", "reopened"}
    assert video_state(running)["status"] == "in progress"
    assert video_state(running, KEY_T2)["status"] == "done"
    assert running.client.post("/api/observations", observation(species=None)).json()["reopened"] is False
    refused = running.client.post("/api/videos/done", {"key": KEY_T1, "done": True})
    assert refused.status == 400 and "2 unlabeled point(s)" in refused.error


def test_post_labeled_sighting_on_done_video_reports_not_reopened(running: Running):
    """A labeled save leaves a done video done, and a Later point on an open video reopens nothing."""
    save_one(running)
    mark_done(running)
    labeled = running.client.post("/api/observations", observation(species="CDEL"))
    assert labeled.status == 201
    assert labeled.json()["reopened"] is False
    assert video_state(running)["status"] == "done"
    open_video = running.client.post("/api/observations", observation(species=None, key=KEY_T2))
    assert open_video.json()["reopened"] is False


def test_patch_unlabel_on_done_video_reports_reopened(running: Running):
    """Unlabeling a sighting of a done video moves the video back to in progress and says so once."""
    save_one(running)
    mark_done(running)
    first = running.client.patch("/api/observations/ID001", {"species": None})
    assert first.status == 200, first.body
    assert first.json()["reopened"] is True
    assert first.json()["row"]["LabelStatus"] == "unlabeled"
    assert set(first.json()) == {"row", "reopened"}
    state = video_state(running)
    assert (state["status"], state["sightings"], state["unlabeled"]) == ("in progress", 0, 1)
    assert running.client.patch("/api/observations/ID001", {"species": None}).json()["reopened"] is False


def test_patch_relabel_to_code_keeps_done_video_done(running: Running):
    """Relabeling to a code never reopens a done video, and unlabeling in an open video reopens nothing."""
    save_one(running)
    mark_done(running)
    answer = running.client.patch("/api/observations/ID001", {"species": "CDEL"})
    assert answer.status == 200
    assert answer.json()["reopened"] is False
    assert video_state(running)["status"] == "done"
    save_one(running, key=KEY_T2)
    unlabeled = running.client.patch("/api/observations/ID002", {"species": None})
    assert unlabeled.json()["reopened"] is False
    assert video_state(running, KEY_T2)["status"] == "in progress"


def test_tally_total_and_unlabeled_come_from_one_read(running: Running, monkeypatch):
    """A sighting saved by another tab between two reads cannot split total and unlabeled."""
    save_one(running)
    save_later(running)
    store = running.app.store
    real_read = store._read_observations
    state = {"saved_elsewhere": False}

    def read_then_other_tab_saves():
        """Read the file, then let another tab save a Later point once."""
        rows = real_read()
        if not state["saved_elsewhere"]:
            state["saved_elsewhere"] = True
            save_later(running)
        return rows

    monkeypatch.setattr(store, "_read_observations", read_then_other_tab_saves)
    answer = running.client.get("/api/tally").json()
    monkeypatch.undo()
    assert (answer["total"], answer["unlabeled"]) == (1, 1)
    assert running.client.get("/api/tally").json()["unlabeled"] == 2


def test_get_observations_status_unlabeled_across_videos(running: Running):
    """status=unlabeled lists Later points of every video in file order; key and status combine."""
    save_one(running)
    save_later(running, key=KEY_T2)
    save_later(running)
    save_one(running, key=KEY_2016, species="AFUL")
    save_later(running, key=KEY_2016)
    everywhere = running.client.get("/api/observations?status=unlabeled")
    assert everywhere.status == 200
    rows = everywhere.json()["rows"]
    assert [row["ID"] for row in rows] == ["ID002", "ID003", "ID005"]
    assert {row["LabelStatus"] for row in rows} == {"unlabeled"}
    assert all(len(row) == 23 for row in rows)
    labeled = running.client.get("/api/observations?status=labeled").json()["rows"]
    assert [row["ID"] for row in labeled] == ["ID001", "ID004"]
    one_video = running.client.get("/api/observations?key=" + quote_key(KEY_T1) + "&status=unlabeled").json()["rows"]
    assert [row["ID"] for row in one_video] == ["ID003"]
    both = running.client.get("/api/observations?key=" + quote_key(KEY_T1)).json()["rows"]
    assert [(row["ID"], row["LabelStatus"]) for row in both] == [("ID001", "labeled"), ("ID003", "unlabeled")]
    assert running.client.get("/api/observations?key=" + quote_key(KEY_MTS) + "&status=unlabeled").json() == {"rows": []}


def test_get_observations_bad_status_400(running: Running):
    """A status outside the two values, empty, or repeated is 400 and starts with status."""
    queries = (
        "status=later",
        "status=",
        "status=Labeled",
        "status=labeled&status=unlabeled",
        "key=" + quote_key(KEY_T1) + "&status=all",
    )
    for query in queries:
        answer = running.client.get("/api/observations?" + query)
        assert answer.status == 400, query
        assert answer.error.startswith("status:"), answer.error


def test_get_observations_without_key_or_status_400(running: Running):
    """Neither key nor status is 400 'key: missing'; a bad key still wins its own error."""
    answer = running.client.get("/api/observations")
    assert answer.status == 400
    assert answer.error == "key: missing"
    assert running.client.get("/api/observations?foo=bar").error == "key: missing"
    bad = running.client.get("/api/observations?status=unlabeled&key=other/x.MP4")
    assert bad.status == 400 and bad.error.startswith("key:")


def test_tally_reports_unlabeled(running: Running):
    """The tally and its total count labeled rows only; unlabeled counts the Later points."""
    assert running.client.get("/api/tally").json() == {"tally": [], "total": 0, "unlabeled": 0}
    save_one(running)
    save_later(running)
    save_later(running, key=KEY_2016)
    answer = running.client.get("/api/tally").json()
    assert answer == {
        "tally": [{"code": "ACAU", "name": "Aplysina cauliformis", "sightings": 1, "videos": 1, "earliest_year": 2024}],
        "total": 1,
        "unlabeled": 2,
    }


def test_catalog_video_has_unlabeled_count(running: Running):
    """Each catalog video carries unlabeled; sightings counts labeled rows only."""
    save_one(running)
    save_later(running)
    save_later(running)
    save_later(running, key=KEY_T2)
    merged = {
        video["key"]: video
        for video in running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2024)).json()["videos"]
    }
    assert (merged[KEY_T1]["sightings"], merged[KEY_T1]["unlabeled"]) == (1, 2)
    assert (merged[KEY_T2]["sightings"], merged[KEY_T2]["unlabeled"]) == (0, 1)
    assert merged[KEY_T1]["status"] == merged[KEY_T2]["status"] == "in progress"
    fresh = running.client.get("/api/catalog?prefix=" + quote_key(FOLDER_2023)).json()["videos"][0]
    assert (fresh["status"], fresh["sightings"], fresh["unlabeled"]) == ("new", 0, 0)


def test_done_refused_with_later_points_400(running: Running):
    """Done is refused while the video holds Later points, un-done always works, and labeling clears the way."""
    save_one(running)
    save_later(running)
    refused = running.client.post("/api/videos/done", {"key": KEY_T1, "done": True})
    assert refused.status == 400
    assert refused.error == (
        "done: this video has 1 unlabeled point(s). Label or delete them first, because a done video "
        "tells OnDeck that missing pinned species are absent."
    )
    assert running.client.post("/api/videos/done", {"key": KEY_T2, "done": True}).status == 200
    assert running.client.post("/api/videos/done", {"key": KEY_T1, "done": False}).status == 200
    assert running.client.patch("/api/observations/ID002", {"species": "AFUL"}).status == 200
    done = running.client.post("/api/videos/done", {"key": KEY_T1, "done": True})
    assert done.status == 200
    assert done.json()["video"]["Status"] == "done"
    assert done.json()["video"]["Sightings"] == "2"


def test_wrong_method_on_observation_route_405_lists_delete_and_patch(running: Running):
    """GET and POST on one observation are 405 with Allow: DELETE, PATCH; PUT and OPTIONS stay 501 as everywhere."""
    for method in ("GET", "POST"):
        answer = running.client.request(method, "/api/observations/ID001", {} if method == "POST" else None)
        assert answer.status == 405, method
        assert answer.headers["Allow"] == "DELETE, PATCH"
        assert answer.error.startswith("method:")
    for method in ("PUT", "OPTIONS"):
        answer = running.client.request(method, "/api/observations/ID001", b"x", headers={"X-Screener": "1"})
        assert answer.status == 501, method
        assert answer.headers["Content-Type"].startswith("application/json")
    assert running.client.get("/api/health").status == 200


def test_delete_later_point_moves_later_files_to_trash(running: Running):
    """Deleting a Later point works like any delete and trashes the LATER files."""
    row = save_later(running)
    deleted = running.client.delete("/api/observations/ID001")
    assert deleted.status == 200
    assert deleted.json()["deleted"] == row
    assert sorted(path.name[-len(row["FrameFileName"]):] for path in (running.data_dir / "trash").iterdir()) == sorted(
        [row["FrameFileName"], row["CropFileName"]]
    )
    assert running.client.get("/api/observations?status=unlabeled").json() == {"rows": []}


# ----- tracks -----

TRACK_HEADER = ["ID", "SpeciesCode", "TimeSeconds", "X", "Y", "BoxX", "BoxY", "BoxW", "BoxH", "Score"]


def save_track_in_process(running: Running, row: Dict[str, str], **changes: Any) -> Dict[str, Any]:
    """Save a track the way the background job does: through the store, inside the server's process."""
    track: Dict[str, Any] = {"points": [[11.0, 0.40, 0.50, 1.0], [12.345, 0.41, 0.50, 0.9], [13.0, 0.43, 0.52, 0.8]], "box": None}
    track.update(changes)
    point = (float(row["PointX"]), float(row["PointY"]))
    return running.app.store.tracks.save(row["ID"], row["S3Key"], float(row["TimestampSeconds"]), point, track)


def read_tracks(running: Running, key: str = KEY_T1) -> Dict[str, Any]:
    """Ask for the tracks of one video and return the answer's tracks."""
    answer = running.client.get("/api/tracks?key=" + quote_key(key))
    assert answer.status == 200, answer.body
    return answer.json()["tracks"]


def test_get_tracks_lists_the_tracks_of_one_video(running: Running):
    """The answer holds the tracks of the rows of that video only, Later points included, in the planned shape."""
    first = save_one(running)
    save_one(running, species="AFUL")
    later = save_later(running)
    other = save_one(running, key=KEY_T2)
    saved_first = save_track_in_process(running, first)
    saved_later = save_track_in_process(running, later, box={"w": 0.2, "h": 0.1})
    saved_other = save_track_in_process(running, other)
    answer = running.client.get("/api/tracks?key=" + quote_key(KEY_T1))
    assert answer.status == 200
    assert answer.headers["Content-Type"].startswith("application/json")
    assert answer.json() == {"tracks": {"ID001": saved_first, "ID003": saved_later}, "pending": []}
    assert set(answer.json()["tracks"]["ID001"]) == {"points", "box", "anchor_time", "updated_at"}
    assert answer.json()["tracks"]["ID003"]["box"] == {"w": 0.2, "h": 0.1}
    assert answer.json()["tracks"]["ID001"]["anchor_time"] == 12.345
    assert read_tracks(running, KEY_T2) == {"ID004": saved_other}


def test_get_tracks_for_a_video_without_tracks_is_empty(running: Running):
    """A video with sightings and no tracks, and a video with no sightings, both answer an empty object.

    Tracking is off in this app, so nothing waits either.
    """
    save_one(running)
    save_later(running)
    assert running.client.get("/api/tracks?key=" + quote_key(KEY_T1)).json() == {"tracks": {}, "pending": []}
    assert running.client.get("/api/tracks?key=" + quote_key(KEY_MTS)).json() == {"tracks": {}, "pending": []}
    assert not (running.data_dir / "tracks").exists()
    assert running.app.tracker.available is False


def test_get_tracks_validates_the_key_like_the_other_routes(running: Running):
    """A missing, empty, repeated, or malformed key is 400 and starts with key."""
    missing = running.client.get("/api/tracks")
    assert missing.status == 400 and missing.error == "key: missing"
    for query in (
        "key=",
        "key=other/x.MP4",
        "key=" + quote_key(ROOT) + "..%2Fsecret.mp4",
        "key=" + quote_key(FOLDER_2024),
        "key=a&key=b",
    ):
        answer = running.client.get("/api/tracks?" + query)
        assert answer.status == 400, query
        assert answer.error.startswith("key:"), answer.error
    assert running.client.get("/api/tracks?foo=bar").error == "key: missing"


def test_get_tracks_skips_a_damaged_file_and_logs_a_warning(running: Running, capsys):
    """A track file that cannot be read is left out of the answer, named on stderr, and left on disk."""
    first = save_one(running)
    second = save_one(running)
    save_track_in_process(running, first)
    saved_second = save_track_in_process(running, second)
    damaged = running.data_dir / "tracks" / "ID001.json"
    damaged.write_bytes(b'{"id": "ID001", "points": [[')
    capsys.readouterr()
    answer = running.client.get("/api/tracks?key=" + quote_key(KEY_T1))
    assert answer.status == 200
    assert answer.json() == {"tracks": {"ID002": saved_second}, "pending": []}
    err = capsys.readouterr().err
    assert "tracks: skipped " in err and str(damaged) in err
    assert "Traceback" not in err
    assert damaged.read_bytes() == b'{"id": "ID001", "points": [['


def test_delete_observation_moves_its_track_to_the_trash(running: Running):
    """Deleting a sighting over HTTP trashes its track with its images; the other tracks stay."""
    first = save_one(running)
    second = save_one(running)
    save_track_in_process(running, first)
    saved_second = save_track_in_process(running, second)
    kept = (running.data_dir / "tracks" / "ID001.json").read_bytes()
    assert running.client.delete("/api/observations/ID001").status == 200
    assert sorted(path.name for path in (running.data_dir / "tracks").iterdir()) == ["ID002.json"]
    trashed = [path for path in (running.data_dir / "trash").iterdir() if path.name.endswith("_ID001.json")]
    assert len(trashed) == 1 and trashed[0].read_bytes() == kept
    assert read_tracks(running) == {"ID002": saved_second}


def test_patch_keeps_the_track(running: Running):
    """Relabeling and unlabeling leave the track where it is, and the answer for the video still holds it."""
    row = save_one(running)
    saved = save_track_in_process(running, row)
    before = (running.data_dir / "tracks" / "ID001.json").read_bytes()
    for species in ("CDEL", None, "AFUL"):
        assert running.client.patch("/api/observations/ID001", {"species": species}).status == 200
        assert read_tracks(running) == {"ID001": saved}
    assert (running.data_dir / "tracks" / "ID001.json").read_bytes() == before


def test_tracks_route_answers_get_only(running: Running):
    """No request saves a track: POST and DELETE are 405, a per-ID path is 404, PUT stays 501."""
    row = save_one(running)
    posted = running.client.post("/api/tracks", {"points": [[1.0, 0.5, 0.5, 1.0]], "box": None})
    assert posted.status == 405 and posted.headers["Allow"] == "GET"
    assert running.client.delete("/api/tracks").status == 405
    for method in ("POST", "DELETE", "GET"):
        body = {"points": [[1.0, 0.5, 0.5, 1.0]], "box": None} if method == "POST" else None
        answer = running.client.request(method, "/api/tracks/" + row["ID"], body)
        assert answer.status == 404, method
        assert "/api/tracks/ID001" in answer.error
    assert running.client.post("/api/tracks/ID001", {}, mutation_header=False).status == 403
    for path in ("/api/tracks", "/api/tracks/ID001"):
        unsupported = running.client.request("PUT", path, b"{}", headers={"X-Screener": "1"})
        assert unsupported.status == 501, path
        assert unsupported.headers["Content-Type"].startswith("application/json")
    assert not (running.data_dir / "tracks").exists()
    assert running.client.get("/api/health").status == 200


# ----- background tracking -----

# Stands in for screener/track_worker.py: it logs each request next to itself,
# waits while a file named hold sits there, and answers a two-point track.
FAKE_TRACK_WORKER = r'''
import json
import sys
import time
from pathlib import Path

here = Path(__file__).parent
request = json.loads(sys.stdin.read())
with open(here / "requests.log", "a") as handle:
    handle.write(json.dumps(request) + "\n")
while (here / "hold").exists():
    time.sleep(0.01)
t, point, box = request["time"], request["point"], request["box"]
answer = {
    "points": [[t, point["x"], point["y"], 1.0], [t + 0.5, max(0.0, point["x"] - 0.05), point["y"], 0.9]],
    "box": None if box is None else {"w": box["w"], "h": box["h"]},
    "ends": {"back": "exit", "forward": "lost"},
}
sys.stdout.write(json.dumps(answer) + "\n")
'''


@pytest.fixture
def worker_dir(tmp_path: Path) -> Path:
    """The folder of the fake tracking worker, its request log, and its hold file."""
    folder = tmp_path / "fake-worker"
    folder.mkdir()
    (folder / "track_worker.py").write_text(FAKE_TRACK_WORKER, encoding="utf-8")
    return folder


@pytest.fixture
def tracked(make_app, worker_dir: Path) -> Running:
    """A running app whose tracker runs the fake worker under the Python running the tests."""
    return make_app(track_python=Path(sys.executable), track_worker=worker_dir / "track_worker.py")


def worker_requests(worker_dir: Path) -> List[Dict[str, Any]]:
    """Read the requests the fake worker received, in order."""
    log = worker_dir / "requests.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def tracks_answer(running: Running, key: str = KEY_T1) -> Dict[str, Any]:
    """Ask for the tracks of one video and return the whole answer."""
    answer = running.client.get("/api/tracks?key=" + quote_key(key))
    assert answer.status == 200, answer.body
    return answer.json()


def wait_tracked(running: Running, ids: List[str], key: str = KEY_T1) -> Dict[str, Any]:
    """Poll the tracks route the way the page does until the IDs have tracks and nothing waits."""
    found: Dict[str, Any] = {}

    def settled() -> bool:
        """Read the answer and tell whether every ID is tracked and nothing is pending."""
        found.update(tracks_answer(running, key))
        return sorted(found["tracks"]) == sorted(ids) and found["pending"] == []

    wait_for(settled, f"tracks for {ids}")
    return found


def test_a_saved_sighting_is_tracked_in_the_background(tracked: Running, worker_dir: Path):
    """A save queues a job; the worker reads the video through the relay and the track lands in the answer."""
    row = save_one(tracked, box={"x": 0.2, "y": 0.7, "w": 0.1, "h": 0.1})
    answer = wait_tracked(tracked, [row["ID"]])
    track = answer["tracks"]["ID001"]
    assert track["points"] == [[12.345, 0.25, 0.75, 1.0], [12.845, 0.2, 0.75, 0.9]]
    assert track["box"] == {"w": 0.1, "h": 0.1} and track["anchor_time"] == 12.345
    [request] = worker_requests(worker_dir)
    assert request == {
        "source": f"http://127.0.0.1:{tracked.port}/video?key={quote_key(KEY_T1)}",
        "time": 12.345,
        "point": {"x": 0.25, "y": 0.75},
        "box": {"x": 0.2, "y": 0.7, "w": 0.1, "h": 0.1},
    }
    assert (tracked.data_dir / "tracks" / "ID001.json").is_file()


def test_get_tracks_queues_the_rows_without_a_track_and_answers_what_waits(tracked: Running, worker_dir: Path):
    """Rows saved before tracking (straight into the store) are queued when the page asks, behind the new sighting."""
    hold = worker_dir / "hold"
    hold.write_text("wait", encoding="utf-8")
    save_one(tracked)
    for seconds in (20.0, 30.0):
        tracked.app.store.add(
            key=KEY_T1, time_seconds=seconds, point={"x": 0.5, "y": 0.5}, box=None, species_code="ACAU",
            note="", annotator="LO", frame_jpeg=JPEG, crop_png=PNG,
        )
    wait_for(lambda: len(worker_requests(worker_dir)) == 1, "the new sighting's job to start")
    assert tracks_answer(tracked) == {"tracks": {}, "pending": ["ID001", "ID002", "ID003"]}
    assert tracks_answer(tracked, KEY_T2) == {"tracks": {}, "pending": []}
    hold.unlink()
    answer = wait_tracked(tracked, ["ID001", "ID002", "ID003"])
    assert [request["time"] for request in worker_requests(worker_dir)] == [12.345, 20.0, 30.0]
    assert answer["tracks"]["ID003"]["anchor_time"] == 30.0


def test_deleting_a_sighting_drops_its_waiting_job(tracked: Running, worker_dir: Path):
    """A sighting deleted while its job waits is never tracked, and the answer stops waiting for it."""
    hold = worker_dir / "hold"
    hold.write_text("wait", encoding="utf-8")
    save_one(tracked)
    save_one(tracked, time=40.0)
    wait_for(lambda: len(worker_requests(worker_dir)) == 1, "the first job to start")
    assert tracks_answer(tracked)["pending"] == ["ID001", "ID002"]
    assert tracked.client.delete("/api/observations/ID002").status == 200
    assert tracks_answer(tracked)["pending"] == ["ID001"]
    hold.unlink()
    wait_tracked(tracked, ["ID001"])
    time.sleep(0.3)
    assert [request["time"] for request in worker_requests(worker_dir)] == [12.345]
    assert sorted(path.name for path in (tracked.data_dir / "tracks").iterdir()) == ["ID001.json"]


def test_the_worker_reads_the_converted_copy_when_there_is_one(tracked: Running, worker_dir: Path):
    """A video that plays as a converted copy is tracked on that copy, the file whose times the page shows."""
    copy = tracked.cache_dir / "converted" / (cache_id(KEY_MTS) + ".mp4")
    copy.write_bytes(b"converted video")
    row = save_one(tracked, key=KEY_MTS)
    wait_tracked(tracked, [row["ID"]], KEY_MTS)
    assert [request["source"] for request in worker_requests(worker_dir)] == [str(copy)]


def test_tracks_payload_refuses_a_missing_key(running: Running):
    """Called without a key, the app refuses instead of answering, and queueing, the rows of every video."""
    save_one(running)
    with pytest.raises(ValueError, match="^key: missing$"):
        running.app.tracks_payload(None)


def test_app_rejects_track_paths_that_are_not_paths(tmp_path: Path, static_root: Path, fake: FakeS3):
    """The tracking Python and worker must be paths, and the message names the argument."""
    for name in ("track_python", "track_worker"):
        with pytest.raises(ValueError, match=f"^{name}:"):
            ScreenerApp(tmp_path / "d", tmp_path / "c", CONFIG_DIR, static_root, fake.url, 0, **{name: "python"})


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
    """POST, PATCH, and DELETE without X-Screener: 1 are 403 and change nothing."""
    for method, path, body in (
        ("POST", "/api/settings", {"annotator": "AB"}),
        ("POST", "/api/videos/open", {"key": KEY_T1}),
        ("POST", "/api/export", {}),
        ("PATCH", "/api/observations/ID001", {"species": None}),
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

    monkeypatch.setattr(running.app.store, "tally_and_unlabeled", boom)
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


def test_export_answer_reports_unlabeled_left_out(make_app, tmp_path: Path):
    """Export leaves Later points and their images out, says how many in the answer, and counts labeled rows."""
    export_root = tmp_path / "exports"
    export_root.mkdir()
    running = make_app(settings={"export_root": str(export_root)})
    labeled = save_one(running)
    later_one = save_later(running)
    later_two = save_later(running, key=KEY_T2)
    answer = running.client.post("/api/export", {})
    assert answer.status == 200, answer.body
    body = answer.json()
    assert set(body) == {"path", "observations", "unlabeled_left_out"}
    assert body["observations"] == 1
    assert body["unlabeled_left_out"] == 2
    package = Path(body["path"])
    assert sorted(path.name for path in (package / "frames").iterdir()) == [labeled["FrameFileName"]]
    assert sorted(path.name for path in (package / "crops").iterdir()) == [labeled["CropFileName"]]
    for row in (later_one, later_two):
        assert not (package / "frames" / row["FrameFileName"]).exists()
    assert "- Unlabeled points left out: 2" in (package / "README.md").read_text(encoding="utf-8")
    assert running.client.patch("/api/observations/ID002", {"species": "AFUL"}).status == 200
    assert running.client.patch("/api/observations/ID003", {"species": "CDEL"}).status == 200
    everything = running.client.post("/api/export", {}).json()
    assert (everything["observations"], everything["unlabeled_left_out"]) == (3, 0)


def test_export_left_out_count_comes_from_the_exported_snapshot(make_app, tmp_path: Path, monkeypatch):
    """A Later point saved by another tab during the export changes neither the answer nor the package."""
    export_root = tmp_path / "exports"
    export_root.mkdir()
    running = make_app(settings={"export_root": str(export_root)})
    save_one(running)
    save_later(running)
    store = running.app.store
    real_rows = store.rows
    state = {"saved_elsewhere": False}

    def rows_then_other_tab_saves(*args: Any, **kwargs: Any):
        """Read the rows for the export, then let another tab save a Later point once."""
        result = real_rows(*args, **kwargs)
        if not state["saved_elsewhere"]:
            state["saved_elsewhere"] = True
            save_later(running, key=KEY_T2)
        return result

    monkeypatch.setattr(store, "rows", rows_then_other_tab_saves)
    body = running.client.post("/api/export", {}).json()
    monkeypatch.undo()
    assert state["saved_elsewhere"] is True
    assert (body["observations"], body["unlabeled_left_out"]) == (1, 1)
    assert "- Unlabeled points left out: 1" in (Path(body["path"]) / "README.md").read_text(encoding="utf-8")
    assert running.client.post("/api/export", {}).json()["unlabeled_left_out"] == 2


def test_export_only_later_points_400(make_app, tmp_path: Path):
    """A store with Later points and no labeled sightings answers 400 and names the count."""
    export_root = tmp_path / "exports"
    export_root.mkdir()
    running = make_app(settings={"export_root": str(export_root)})
    save_later(running)
    answer = running.client.post("/api/export", {})
    assert answer.status == 400
    assert "no sightings to export" in answer.error
    assert "1 unlabeled point(s) and no labeled sightings yet" in answer.error
    assert list(export_root.iterdir()) == []


def test_export_route_writes_tracks_csv_for_labeled_sightings(make_app, tmp_path: Path):
    """The package holds tracks.csv with the planned columns, only labeled sightings, and the answer keeps its shape."""
    export_root = tmp_path / "exports"
    export_root.mkdir()
    running = make_app(settings={"export_root": str(export_root)})
    labeled = save_one(running)
    later = save_later(running)
    save_track_in_process(running, labeled, box={"w": 0.2, "h": 0.1})
    save_track_in_process(running, later)
    answer = running.client.post("/api/export", {})
    assert answer.status == 200, answer.body
    assert set(answer.json()) == {"path", "observations", "unlabeled_left_out"}
    package = Path(answer.json()["path"])
    with open(package / "tracks.csv", "r", encoding="utf-8", newline="") as handle:
        records = list(csv.reader(handle))
    assert records[0] == TRACK_HEADER
    assert records[1:] == [
        ["ID001", "ACAU", "11.000", "0.4000", "0.5000", "0.3000", "0.4500", "0.2000", "0.1000", "1.0000"],
        ["ID001", "ACAU", "12.345", "0.4100", "0.5000", "0.3100", "0.4500", "0.2000", "0.1000", "0.9000"],
        ["ID001", "ACAU", "13.000", "0.4300", "0.5200", "0.3300", "0.4700", "0.2000", "0.1000", "0.8000"],
    ]
    readme = (package / "README.md").read_text(encoding="utf-8")
    assert "tracks.csv" in readme


def test_export_without_tracks_still_writes_tracks_csv_with_its_header(make_app, tmp_path: Path):
    """A package from a store with no tracks holds tracks.csv with the header line only."""
    export_root = tmp_path / "exports"
    export_root.mkdir()
    running = make_app(settings={"export_root": str(export_root)})
    save_one(running)
    package = Path(running.client.post("/api/export", {}).json()["path"])
    assert (package / "tracks.csv").read_text(encoding="utf-8") == ",".join(TRACK_HEADER) + "\n"


def test_export_with_a_damaged_track_answers_400_and_names_the_file(make_app, tmp_path: Path):
    """A track file that cannot be read stops the export before anything is written, and the answer names the file."""
    export_root = tmp_path / "exports"
    export_root.mkdir()
    running = make_app(settings={"export_root": str(export_root)})
    row = save_one(running)
    save_one(running)
    save_track_in_process(running, row)
    damaged = running.data_dir / "tracks" / "ID001.json"
    damaged.write_bytes(b"{nope")
    answer = running.client.post("/api/export", {})
    assert answer.status == 400
    assert str(damaged) in answer.error and "ID001" in answer.error
    assert list(export_root.iterdir()) == []
    assert damaged.read_bytes() == b"{nope"


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
    wrong_for_observation = running.client.get("/api/observations/ID001")
    assert wrong_for_observation.status == 405
    assert wrong_for_observation.headers["Allow"] == "DELETE, PATCH"
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
