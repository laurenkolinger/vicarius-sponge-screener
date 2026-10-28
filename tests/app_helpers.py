"""Helpers for the tests that start a real Sponge Screener server: a small HTTP client and an app builder.

The lock-gate tests and the run tests use these. Every app binds a free port
on 127.0.0.1, reads the project's config folder, and talks to a FakeS3, so no
test reaches the real bucket, port 8770 or port 5098.
"""

import base64
import http.client
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from screener import config
from screener.server import ScreenerApp, make_server

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
STATIC_DIR = PROJECT_ROOT / "static"
MUTATING = ("POST", "PATCH", "DELETE")
TIMEOUT_SECONDS = 30.0

KEY_T1 = "TCRMP_video_ondeck/2024Annual/TCRMP20241022_video_FLC_T1.MP4"
KEY_T2 = "TCRMP_video_ondeck/2024Annual/TCRMP20241022_video_FLC_T2.MP4"
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


class Answer:
    """One HTTP answer: status, headers, body, and the body as JSON or text."""

    def __init__(self, status: int, headers: http.client.HTTPMessage, body: bytes) -> None:
        """Keep the parts of one answer."""
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        """Parse the body as JSON."""
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        """Return the body as UTF-8 text."""
        return self.body.decode("utf-8")

    @property
    def error(self) -> str:
        """Return the ``error`` text of a JSON error answer."""
        return self.json()["error"]


class Client:
    """Send one request per connection to a server on 127.0.0.1."""

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
    ) -> Answer:
        """Send one request and read the whole answer.

        Args:
            method: The HTTP method.
            path: The path and query string.
            body: None, a dict or list (sent as JSON), or bytes (sent as is).
            headers: Extra headers.
            mutation_header: False leaves out ``X-Screener: 1`` on POST, PATCH and DELETE.

        Returns:
            The answer.
        """
        sent = dict(headers or {})
        if method in MUTATING and mutation_header:
            sent.setdefault("X-Screener", "1")
        payload: Optional[bytes] = None
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        elif body is not None:
            payload = body
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=TIMEOUT_SECONDS)
        try:
            connection.request(method, path, body=payload, headers=sent)
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


class Running:
    """A started app with its server thread and a client."""

    def __init__(self, app: ScreenerApp, server: Any) -> None:
        """Start serving in a daemon thread."""
        self.app = app
        self.server = server
        self.port = server.server_address[1]
        self.client = Client(self.port)
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the server and close the app."""
        self.server.shutdown()
        self.server.server_close()
        self.app.close()


def start_app(
    tmp_path: Path,
    bucket_url: str,
    data_dir: Optional[Path] = None,
    runs_root: Optional[Path] = None,
    static_dir: Path = STATIC_DIR,
    guide_dir: Optional[Path] = None,
) -> Running:
    """Build and start one app with tracking off.

    Args:
        tmp_path: The test's temporary folder; the cache goes under it.
        bucket_url: The FakeS3 URL.
        data_dir: The standalone data folder, or None in VICARIUS mode.
        runs_root: The runs folder that turns VICARIUS mode on, or None.
        static_dir: The page folder; the real one by default.
        guide_dir: The species guide folder, or None for the default.

    Returns:
        The running app. Call ``stop()`` when the test ends.
    """
    app = ScreenerApp(
        data_dir,
        tmp_path / "cache",
        CONFIG_DIR,
        static_dir,
        bucket_url,
        0,
        track_python=tmp_path / "no-venv" / "bin" / "python",
        track_worker=config.TRACK_WORKER,
        runs_root=runs_root,
        guide_dir=guide_dir,
    )
    return Running(app, make_server(app, "127.0.0.1", 0))


def observation(key: str = KEY_T1, species: Optional[str] = "ACAU", time_seconds: float = 12.5) -> Dict[str, Any]:
    """Build a valid POST /api/observations body.

    Args:
        key: The video key.
        species: A species code, or None for a Later point.
        time_seconds: The sighting time in the video.

    Returns:
        The JSON body as a dict.
    """
    return {
        "key": key,
        "time": time_seconds,
        "point": {"x": 0.25, "y": 0.75},
        "box": None,
        "species": species,
        "note": "",
        "frame_jpeg": base64.b64encode(JPEG).decode("ascii"),
        "crop_png": base64.b64encode(PNG).decode("ascii"),
    }
