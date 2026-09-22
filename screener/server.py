"""The HTTP server of Sponge Screener: routes, request rules, and the app behind them.

ScreenerApp wires the data layer, the catalog, the relay, and the converter
together and offers one method per route. The request handler turns HTTP into
those method calls: it checks the Host header, the mutation header, and the
body, maps every module error to a status code, streams video ranges, and
logs one line per request to stderr. make_server binds the ThreadingHTTPServer
that runs the handler.

Every JSON error has the shape ``{"error": "<field>: <reason>"}``.
"""

import base64
import csv
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from screener.config import KEY_PREFIX, MAX_BODY_BYTES, PLAYABLE_EXTENSIONS, TALLY_TARGET, VERSION
from screener.convert import Converter, ConvertError, ConvertStatus
from screener.export import ExportError, export_package
from screener.keys import extension_of, quote_key
from screener.names import load_site_names
from screener.relay import ChunkRelay, ObjectNotFound, RangeNotSatisfiable, RelayError, parse_range_header
from screener.s3catalog import CatalogError, S3Catalog
from screener.settings import Settings, load_settings, save_settings, validate_annotator
from screener.species import PIN_KEYS, Species, load_species, validate_pins
from screener.store import ID_PATTERN, ObservationStore, StoreError, UnknownObservation

# Files inside the config, data, and cache folders.
SPECIES_FILE = "species.csv"
SITES_FILE = "sites.csv"
SETTINGS_FILE = "settings.json"
CATALOG_FILE = "catalog.json"
INDEX_FILE = "index.html"

# Route prefixes.
STATIC_ROUTE = "/static/"
MEDIA_ROUTE = "/media/"
OBSERVATION_ROUTE = "/api/observations/"
MEDIA_FOLDERS = ("frames", "crops")

# Request rules.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
MUTATION_HEADER = "X-Screener"
MUTATION_VALUE = "1"
MUTATING_METHODS = ("POST", "DELETE")
MEDIA_NAME = re.compile(r"[A-Za-z0-9_.-]+")
FORBIDDEN_SEGMENTS = ("", ".", "..")
FORBIDDEN_PATH_CHARACTERS = ("\\", "\x00")
# A socket that sends nothing, or a client that stops reading a stream, holds
# a handler thread this long before the server lets go of it.
SOCKET_TIMEOUT_SECONDS = 600.0
LISTEN_BACKLOG = 64
FILE_BLOCK_BYTES = 1024 * 1024
LOG_PATH_LIMIT = 300
DATA_URL_PREFIX = re.compile(r"^data:[^,]*;base64,", re.IGNORECASE)

# Answer shapes.
JSON_TYPE = "application/json; charset=utf-8"
VIDEO_TYPE = "video/mp4"
BINARY_TYPE = "application/octet-stream"
NO_CACHE = "no-cache"
CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "map": "application/json; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "ico": "image/x-icon",
    "webp": "image/webp",
    "woff": "font/woff",
    "woff2": "font/woff2",
}

HTTP_OK = 200
HTTP_CREATED = 201
HTTP_PARTIAL = 206
HTTP_BAD_REQUEST = 400
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_METHOD_NOT_ALLOWED = 405
HTTP_LENGTH_REQUIRED = 411
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_RANGE_NOT_SATISFIABLE = 416
HTTP_INTERNAL_ERROR = 500
HTTP_BAD_GATEWAY = 502
HTTP_UNAVAILABLE = 503
BODYLESS_STATUSES = (204, 304)

# The store names two arguments differently from the JSON the page posts.
FIELD_NAMES = {"time_seconds": "time", "species_code": "species"}
OBSERVATION_FIELDS = ("key", "time", "point", "species", "frame_jpeg", "crop_png")
SETTINGS_FIELDS = ("annotator", "pins")
QUIET_DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)


class HttpError(Exception):
    """An answer the handler sends as a JSON error.

    Attributes:
        status: The HTTP status code.
        message: The ``error`` text, ``<field>: <reason>``.
        headers: Extra headers for the answer, such as Content-Range on 416.
    """

    def __init__(self, status: int, message: str, headers: Optional[Mapping[str, str]] = None) -> None:
        """Create the error.

        Args:
            status: The HTTP status code.
            message: The ``error`` text.
            headers: Extra headers for the answer.
        """
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = dict(headers or {})


def _rename_field(message: str) -> str:
    """Make a store error name the JSON field the page posted.

    Args:
        message: A ValueError message that starts with the store's argument
            name, such as ``time_seconds: expected a number``.

    Returns:
        The message with ``time_seconds`` and ``species_code`` replaced by
        ``time`` and ``species`` at its start, and any other message unchanged.
    """
    for store_name, json_name in FIELD_NAMES.items():
        if message.startswith(store_name + ":") or message.startswith(store_name + "."):
            return json_name + message[len(store_name):]
    return message


def _decode_image(value: Any, field: str) -> bytes:
    """Turn a posted image into bytes.

    Args:
        value: Base64 text, with or without a ``data:<type>;base64,`` prefix.
        field: ``frame_jpeg`` or ``crop_png``, used in the error message.

    Returns:
        The decoded bytes. The store checks the signature and the size.

    Raises:
        ValueError: Starting with the field name when the value is not text,
            is empty, is a data URL without base64 encoding, or is not base64.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected base64 text, got {type(value).__name__}")
    text = value
    if text.lower().startswith("data:"):
        match = DATA_URL_PREFIX.match(text)
        if match is None:
            raise ValueError(f"{field}: a data URL must be base64 encoded (data:<type>;base64,...)")
        text = text[match.end():]
    text = "".join(text.split())
    if not text:
        raise ValueError(f"{field}: empty")
    try:
        return base64.b64decode(text, validate=True)
    except ValueError as error:
        raise ValueError(f"{field}: not valid base64 text ({error})") from error


def _parent_prefix(prefix: str) -> Optional[str]:
    """Name the folder above a bucket folder.

    Args:
        prefix: A validated prefix that ends with ``/``.

    Returns:
        The parent prefix, or None at the top folder KEY_PREFIX.
    """
    if prefix == KEY_PREFIX:
        return None
    return prefix.rstrip("/").rsplit("/", 1)[0] + "/"


def _folder_name(prefix: str) -> str:
    """Return the last segment of a folder prefix, such as ``2024Annual``."""
    return prefix.rstrip("/").rsplit("/", 1)[-1]


def _status_json(status: ConvertStatus) -> Dict[str, Any]:
    """Lay out a ConvertStatus for the page."""
    return {"state": status.state, "progress": status.progress, "message": status.message}


def _count_csv_rows(path: Path) -> int:
    """Count the data rows of a CSV file.

    Args:
        path: The CSV file, with one header line.

    Returns:
        The number of non-blank rows after the header.
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return sum(1 for cells in csv.reader(handle) if cells) - 1


class ScreenerApp:
    """Everything behind the routes: the store, the settings, the catalog, the relay, the converter.

    One app serves every handler thread. The store, the catalog, the relay,
    and the converter guard their own state; the settings are guarded here.

    Attributes:
        data_dir: The folder with observations.csv, videos_screened.csv,
            frames/, crops/, trash/, .lock, and settings.json.
        cache_dir: The folder with chunks/, converted/, and catalog.json.
        static_dir: The folder with the page and its files.
        port: The port the converter's source URLs use. make_server replaces
            it with the port it bound, so 0 works.
        store, catalog, relay, converter: The modules behind the routes.
    """

    def __init__(
        self,
        data_dir: Path,
        cache_dir: Path,
        config_dir: Path,
        static_dir: Path,
        bucket_url: str,
        port: int,
    ) -> None:
        """Load the configuration and the settings, and start the relay and the converter.

        Args:
            data_dir: The data folder. It is created when missing.
            cache_dir: The cache folder. It is created when missing.
            config_dir: The folder with species.csv and sites.csv.
            static_dir: The existing folder with index.html and its files.
            bucket_url: The bucket's base URL, http or https.
            port: The server port, 0 to 65535. 0 means make_server chooses.

        Raises:
            ValueError: Starting with the argument name when an argument has
                the wrong type or value, or a config file breaks its rules.
            FileNotFoundError: When species.csv or sites.csv is missing.
            StoreError, RelayError, ConvertError: When a folder cannot be
                created.
        """
        for name, value in (("data_dir", data_dir), ("cache_dir", cache_dir), ("config_dir", config_dir), ("static_dir", static_dir)):
            if not isinstance(value, (str, os.PathLike)):
                raise ValueError(f"{name}: expected a folder path, got {type(value).__name__}")
        if not Path(static_dir).is_dir():
            raise ValueError(f"static_dir: {static_dir} is not a folder")
        if not isinstance(bucket_url, str) or urllib.parse.urlsplit(bucket_url).scheme not in ("http", "https"):
            raise ValueError(f"bucket_url: expected an http or https URL, got {bucket_url!r}")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError(f"port: expected a whole number from 0 to 65535, got {port!r}")
        self.data_dir = Path(data_dir)
        self.cache_dir = Path(cache_dir)
        self.static_dir = Path(static_dir).resolve()
        self.port = port
        self._species: List[Species] = load_species(Path(config_dir) / SPECIES_FILE)
        site_names = load_site_names(Path(config_dir) / SITES_FILE)
        self.store = ObservationStore(self.data_dir, self._species, site_names)
        self._settings_path = self.data_dir / SETTINGS_FILE
        self._settings_lock = threading.Lock()
        self._settings = load_settings(self._settings_path, self._species)
        self.catalog = S3Catalog(bucket_url, cache_path=self.cache_dir / CATALOG_FILE)
        self.relay = ChunkRelay(bucket_url, self.cache_dir)
        try:
            self.converter = Converter(cache_root=self.cache_dir, source_for=self._source_for)
        except BaseException:
            self.relay.close()
            raise
        self._closed = False

    def _source_for(self, key: str) -> str:
        """Return the local relay URL ffmpeg reads a key from.

        Args:
            key: A validated key.

        Returns:
            ``http://127.0.0.1:<port>/video?key=<quoted key>``.
        """
        return f"http://{LOOPBACK_HOSTS[0]}:{self.port}/video?key={quote_key(key)}"

    def close(self) -> None:
        """Stop the converter and the relay. A second call does nothing."""
        if self._closed:
            return
        self._closed = True
        self.converter.close()
        self.relay.close()

    # ----- settings -----

    def settings(self) -> Settings:
        """Return a copy of the current settings."""
        with self._settings_lock:
            return Settings(self._settings.annotator, dict(self._settings.pins), self._settings.export_root)

    def pinned_codes(self) -> List[str]:
        """Return the species codes on the number keys, in keyboard order."""
        pins = self.settings().pins
        return [pins[key] for key in PIN_KEYS if pins[key] is not None]

    def species_payload(self) -> Dict[str, Any]:
        """Lay out the species list, the pins, the annotator, and the tally target for the page."""
        current = self.settings()
        return {
            "species": [{"code": item.code, "name": item.name, "part": item.part} for item in self._species],
            "pins": current.pins,
            "annotator": current.annotator,
            "tally_target": TALLY_TARGET,
        }

    def update_settings(self, fields: Any) -> Dict[str, Any]:
        """Change the annotator or the pins and save settings.json.

        Args:
            fields: A JSON object with ``annotator`` and/or ``pins``.

        Returns:
            The species_payload after the change.

        Raises:
            ValueError: Starting with ``settings``, ``annotator``, ``pins``,
                or ``pins.<key>`` when the object has other fields, no field,
                or a field that fails its check. Nothing is saved.
            OSError: When settings.json cannot be written.
        """
        if not isinstance(fields, Mapping):
            raise ValueError(f"settings: expected a JSON object, got {type(fields).__name__}")
        unknown = [name for name in fields if name not in SETTINGS_FIELDS]
        if unknown:
            raise ValueError(f"settings: unknown field {unknown[0]!r}; expected {' or '.join(SETTINGS_FIELDS)}")
        if not fields:
            raise ValueError(f"settings: nothing to change; send {' or '.join(SETTINGS_FIELDS)}")
        with self._settings_lock:
            annotator = self._settings.annotator
            pins = dict(self._settings.pins)
            if "annotator" in fields:
                annotator = validate_annotator(fields["annotator"])
            if "pins" in fields:
                pins = validate_pins(fields["pins"], self._species)
            changed = Settings(annotator, pins, self._settings.export_root)
            save_settings(self._settings_path, changed)
            self._settings = changed
        return self.species_payload()

    # ----- catalog -----

    def catalog_page(self, prefix: str, refresh: bool) -> Dict[str, Any]:
        """List one bucket folder with the screening status of every video.

        Args:
            prefix: The folder to list.
            refresh: True asks the bucket again even when a young listing is saved.

        Returns:
            The ``/api/catalog`` answer.

        Raises:
            InvalidKey: When the prefix breaks the key rules.
            CatalogError: When the bucket cannot be listed and no listing is saved.
            StoreError: When a CSV file cannot be parsed.
        """
        page = self.catalog.list(prefix, refresh=refresh)
        status = self.store.video_status()
        videos = []
        for entry in page.videos:
            known = status.get(entry.key, {"status": "new", "sightings": 0})
            videos.append(
                {
                    "key": entry.key,
                    "name": entry.name,
                    "size": entry.size,
                    "ext": entry.ext,
                    "playable": entry.playable,
                    "status": known["status"],
                    "sightings": known["sightings"],
                    "converted": self.converter.status(entry.key).state,
                }
            )
        return {
            "prefix": page.prefix,
            "parent": _parent_prefix(page.prefix),
            "stale": page.stale,
            "folders": [{"prefix": folder, "name": _folder_name(folder)} for folder in page.folders],
            "videos": videos,
        }

    # ----- observations -----

    def observation_rows(self, key: str) -> Dict[str, Any]:
        """Return the sightings of one video.

        Args:
            key: The video's key.

        Returns:
            ``{"rows": [...]}``.

        Raises:
            InvalidKey: When the key breaks the key rules.
            StoreError: When observations.csv cannot be parsed.
        """
        return {"rows": self.store.rows(key)}

    def save_observation(self, fields: Any) -> Dict[str, Any]:
        """Save one sighting posted by the page.

        Args:
            fields: A JSON object with key, time, point, box (null or an
                object; absent counts as null), species, note (absent counts
                as empty), frame_jpeg, and crop_png (base64 text, a data URL
                prefix is accepted).

        Returns:
            ``{"row": <the saved row>}``.

        Raises:
            ValueError: Starting with the name of the bad field. Nothing is written.
            StoreError: When the store cannot write the sighting.
        """
        if not isinstance(fields, Mapping):
            raise ValueError(f"body: expected a JSON object, got {type(fields).__name__}")
        for name in OBSERVATION_FIELDS:
            if name not in fields:
                raise ValueError(f"{name}: missing")
        frame = _decode_image(fields["frame_jpeg"], "frame_jpeg")
        crop = _decode_image(fields["crop_png"], "crop_png")
        try:
            row = self.store.add(
                key=fields["key"],
                time_seconds=fields["time"],
                point=fields["point"],
                box=fields.get("box"),
                species_code=fields["species"],
                note=fields.get("note", ""),
                annotator=self.settings().annotator,
                frame_jpeg=frame,
                crop_png=crop,
            )
        except ValueError as error:
            raise ValueError(_rename_field(str(error))) from error
        return {"row": row}

    def delete_observation(self, obs_id: str) -> Dict[str, Any]:
        """Remove one sighting and move its images to the trash.

        Args:
            obs_id: The ID from the route, such as ``ID007``.

        Returns:
            ``{"deleted": <the removed row>}``.

        Raises:
            ValueError: Starting with ``obs_id`` when the ID is not ``ID`` plus digits.
            UnknownObservation: When no sighting has the ID.
            StoreError: When the store cannot remove the sighting.
        """
        if not ID_PATTERN.fullmatch(obs_id):
            raise ValueError(f"obs_id: expected ID followed by digits, such as ID007, got {obs_id!r}")
        return {"deleted": self.store.delete(obs_id)}

    def tally_payload(self) -> Dict[str, Any]:
        """Count the sightings per species across all videos.

        Returns:
            ``{"tally": [...], "total": <sightings in all rows>}``.

        Raises:
            StoreError: When observations.csv cannot be parsed.
        """
        tally = self.store.tally()
        return {"tally": tally, "total": sum(item["sightings"] for item in tally)}

    # ----- screened videos -----

    def open_video(self, key: Any) -> Dict[str, Any]:
        """Record that the annotator opened a video, then trim the chunk cache.

        Args:
            key: The video's key.

        Returns:
            ``{"video": <the videos_screened.csv row>}``.

        Raises:
            ValueError: Starting with the name of the bad argument (InvalidKey for the key).
            StoreError: When a CSV file cannot be parsed or written.
        """
        current = self.settings()
        row = self.store.mark_opened(key, current.annotator, self.pinned_codes())
        self.relay.evict()
        return {"video": row}

    def finish_video(self, key: Any, done: Any) -> Dict[str, Any]:
        """Mark a video done, or return it to in progress.

        Args:
            key: The video's key.
            done: True or False.

        Returns:
            ``{"video": <the videos_screened.csv row>}``.

        Raises:
            ValueError: Starting with the name of the bad argument.
            StoreError: When a CSV file cannot be parsed or written.
        """
        current = self.settings()
        return {"video": self.store.mark_done(key, done, current.annotator, self.pinned_codes())}

    # ----- conversion and prefetch -----

    def start_conversion(self, key: Any) -> Dict[str, Any]:
        """Queue a video for conversion and report its status.

        Raises:
            InvalidKey: When the key breaks the key rules.
            ConvertError: When the converter is closed.
        """
        return _status_json(self.converter.start(key))

    def conversion_status(self, key: Any) -> Dict[str, Any]:
        """Report where a video's conversion stands.

        Raises:
            InvalidKey: When the key breaks the key rules.
        """
        return _status_json(self.converter.status(key))

    def start_prefetch(self, key: Any) -> Dict[str, Any]:
        """Queue every missing chunk of a video and report how much is cached.

        Raises:
            InvalidKey: When the key breaks the key rules.
            ObjectNotFound: When the bucket has no such object.
            RelayError: When the object cannot be sized or the relay is closed.
        """
        self.relay.prefetch(key)
        return {"cached": self.relay.cached_fraction(key)}

    def prefetch_status(self, key: Any) -> Dict[str, Any]:
        """Report how much of a video is on disk, without touching the network.

        Raises:
            InvalidKey: When the key breaks the key rules.
            RelayError: When the relay is closed.
        """
        return {"cached": self.relay.cached_fraction(key)}

    # ----- export -----

    def export(self) -> Dict[str, Any]:
        """Write the dated export package into the export folder from the settings.

        Returns:
            ``{"path": <package folder>, "observations": <rows exported>}``.

        Raises:
            ExportError: When the export folder is missing, the store is
                empty, an image is missing, or a file cannot be written.
        """
        package = export_package(self.store, Path(self.settings().export_root), date.today())
        return {"path": str(package), "observations": _count_csv_rows(package / self.store.observations_path.name)}


class _ScreenerServer(ThreadingHTTPServer):
    """The threading server, quiet when a client hangs up.

    Attributes:
        app: The ScreenerApp behind the routes, set by make_server.
    """

    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG
    app: ScreenerApp

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Drop connection errors that escape a handler and report everything else.

        Args:
            request: The client socket.
            client_address: The client's address pair.
        """
        error = sys.exc_info()[1]
        if isinstance(error, OSError):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    """Answers one connection's requests from the ScreenerApp of its server.

    Every request passes the Host check, then (for POST and DELETE) the
    mutation header check, then the route table. Route methods raise
    HttpError for an answer with an error status, and module errors are
    mapped to statuses in one place (_dispatch). One log line per request
    goes to stderr when the request ends.
    """

    protocol_version = "HTTP/1.1"
    server_version = f"SpongeScreener/{VERSION}"
    timeout = SOCKET_TIMEOUT_SECONDS

    _status = 0
    _headers_sent = False
    _body_consumed = False
    _sent_bytes: Optional[int] = None
    _note = ""

    EXACT_ROUTES: Dict[str, Dict[str, str]] = {
        "/": {"GET": "_get_index"},
        "/api/health": {"GET": "_get_health"},
        "/api/catalog": {"GET": "_get_catalog"},
        "/api/species": {"GET": "_get_species"},
        "/api/settings": {"POST": "_post_settings"},
        "/api/observations": {"GET": "_get_observations", "POST": "_post_observation"},
        "/api/tally": {"GET": "_get_tally"},
        "/api/videos/open": {"POST": "_post_open"},
        "/api/videos/done": {"POST": "_post_done"},
        "/api/convert": {"GET": "_get_convert", "POST": "_post_convert"},
        "/api/prefetch": {"GET": "_get_prefetch", "POST": "_post_prefetch"},
        "/api/export": {"POST": "_post_export"},
        "/video": {"GET": "_get_video"},
    }
    PREFIX_ROUTES: Tuple[Tuple[str, Dict[str, str]], ...] = (
        (STATIC_ROUTE, {"GET": "_get_static"}),
        (MEDIA_ROUTE, {"GET": "_get_media"}),
        (OBSERVATION_ROUTE, {"DELETE": "_delete_observation"}),
    )

    # ----- entry points called by the base class -----

    def do_GET(self) -> None:  # noqa: N802
        """Serve a GET request."""
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        """Serve a POST request."""
        self._serve()

    def do_DELETE(self) -> None:  # noqa: N802
        """Serve a DELETE request."""
        self._serve()

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """Drop the base class's log line; _log writes one line per request instead."""

    def send_response(self, code: int, message: Optional[str] = None) -> None:
        """Send the status line and remember the status for the log line."""
        self._status = int(code)
        super().send_response(code, message)

    def end_headers(self) -> None:
        """Finish the headers and remember that the answer has begun."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self._headers_sent = True
        super().end_headers()

    def send_error(self, code: int, message: Optional[str] = None, explain: Optional[str] = None) -> None:
        """Answer an error the base class found (a bad request line, an unknown method) as JSON.

        Args:
            code: The HTTP status.
            message: The base class's reason, or None for the standard one.
            explain: Ignored; the reason is enough.
        """
        reason = message or self.responses.get(code, ("Error", ""))[0]
        body = json.dumps({"error": f"request: {reason}"}).encode("utf-8")
        self.close_connection = True
        self.send_response(code, message)
        self.send_header("Content-Type", JSON_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and int(code) >= HTTP_OK and int(code) not in BODYLESS_STATUSES:
            self.wfile.write(body)
        sys.stderr.write(f"{self.command or '-'} {self._loggable_path()} {int(code)} 0ms ({reason})\n")

    # ----- one request -----

    def _serve(self) -> None:
        """Run one request through the checks and its route, answer every error, and log."""
        started = time.monotonic()
        self._status, self._headers_sent, self._body_consumed, self._sent_bytes, self._note = 0, False, False, None, ""
        try:
            self._dispatch()
        except HttpError as error:
            self._answer_error(error)
        except QUIET_DISCONNECTS as error:
            self._note = f"client disconnected: {type(error).__name__}"
            self.close_connection = True
        except Exception as error:  # noqa: BLE001 - the server outlives any one request
            traceback.print_exc(file=sys.stderr)
            self.close_connection = True
            self._answer_error(HttpError(HTTP_INTERNAL_ERROR, f"internal error: {type(error).__name__}: {error}"))
        finally:
            self._log(started)

    def _dispatch(self) -> None:
        """Check the request, find its route, run it, and map module errors to statuses.

        Raises:
            HttpError: For every refused or failed request.
        """
        self._check_host()
        parts = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parts.path)
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if self.command in MUTATING_METHODS:
            self._check_mutation_header()
        route, rest = self._find_route(path)
        try:
            route(rest, query)
        except HttpError:
            raise
        except UnknownObservation as error:
            raise HttpError(HTTP_NOT_FOUND, str(error)) from error
        except ObjectNotFound as error:
            raise HttpError(HTTP_NOT_FOUND, str(error)) from error
        except RangeNotSatisfiable as error:
            raise HttpError(HTTP_RANGE_NOT_SATISFIABLE, str(error)) from error
        except ValueError as error:
            raise HttpError(HTTP_BAD_REQUEST, str(error)) from error
        except ExportError as error:
            raise HttpError(HTTP_BAD_REQUEST, str(error)) from error
        except CatalogError as error:
            raise HttpError(HTTP_BAD_GATEWAY, str(error)) from error
        except RelayError as error:
            raise HttpError(HTTP_BAD_GATEWAY, str(error)) from error
        except ConvertError as error:
            raise HttpError(HTTP_UNAVAILABLE, str(error)) from error
        except StoreError as error:
            raise HttpError(HTTP_INTERNAL_ERROR, str(error)) from error

    def _check_host(self) -> None:
        """Refuse a request whose Host header names another server.

        Raises:
            HttpError: 403 when the Host header is missing or is not
                ``127.0.0.1:<port>`` or ``localhost:<port>``.
        """
        host = self.headers.get("Host")
        port = self.server.server_address[1]
        allowed = tuple(f"{name}:{port}" for name in LOOPBACK_HOSTS)
        if host is None or host.strip().lower() not in allowed:
            raise HttpError(HTTP_FORBIDDEN, f"Host: {host!r} is not this server; expected {' or '.join(allowed)}")

    def _check_mutation_header(self) -> None:
        """Refuse a POST or DELETE without the page's header.

        Raises:
            HttpError: 403 when ``X-Screener: 1`` is missing.
        """
        if self.headers.get(MUTATION_HEADER) != MUTATION_VALUE:
            raise HttpError(
                HTTP_FORBIDDEN,
                f"{MUTATION_HEADER}: a {self.command} request needs the header {MUTATION_HEADER}: {MUTATION_VALUE}",
            )

    def _find_route(self, path: str) -> Tuple[Callable[[str, Dict[str, List[str]]], None], str]:
        """Pick the route method for a path and the request method.

        Args:
            path: The percent-decoded request path.

        Returns:
            ``(method, rest)`` where rest is the text after a prefix route,
            or "" for an exact route.

        Raises:
            HttpError: 404 when no route matches the path, 405 (with an
                Allow header) when the path exists for other methods.
        """
        methods = self.EXACT_ROUTES.get(path)
        rest = ""
        if methods is None:
            for prefix, candidates in self.PREFIX_ROUTES:
                if path.startswith(prefix):
                    methods, rest = candidates, path[len(prefix):]
                    break
        if methods is None:
            raise HttpError(HTTP_NOT_FOUND, f"route: no route for {self.command} {path}")
        name = methods.get(self.command)
        if name is None:
            allowed = ", ".join(sorted(methods))
            raise HttpError(
                HTTP_METHOD_NOT_ALLOWED, f"method: {self.command} is not allowed for {path}; allowed: {allowed}", {"Allow": allowed}
            )
        return getattr(self, name), rest

    def _answer_error(self, error: HttpError) -> None:
        """Send an HttpError as JSON, or end the request when the answer already began.

        Args:
            error: The error to send.
        """
        if self._headers_sent:
            self._note = error.message
            self.close_connection = True
            return
        has_body = "Content-Length" in self.headers or "Transfer-Encoding" in self.headers
        if has_body and not self._body_consumed:
            # The body is still in the socket, so the connection cannot carry
            # another request.
            self.close_connection = True
        self._send_json(error.status, {"error": error.message}, error.headers)

    def _log(self, started: float) -> None:
        """Write the request's log line to stderr.

        Args:
            started: The monotonic clock reading when the request began.
        """
        milliseconds = int((time.monotonic() - started) * 1000)
        line = f"{self.command} {self._loggable_path()} {self._status} {milliseconds}ms"
        if self._sent_bytes is not None:
            line += f" {self._sent_bytes} bytes"
        if self._note:
            line += f" ({self._note})"
        sys.stderr.write(line + "\n")

    def _loggable_path(self) -> str:
        """Return the request path with unprintable characters replaced, cut for the log."""
        path = getattr(self, "path", "-")
        return "".join(char if char.isprintable() else "?" for char in path[:LOG_PATH_LIMIT])

    # ----- reading the request -----

    def _read_json_body(self) -> Any:
        """Read the request body by its Content-Length and parse it as JSON.

        Returns:
            The parsed JSON value. An empty body reads as ``{}``.

        Raises:
            HttpError: 411 without a Content-Length or with a chunked body,
                400 for a Content-Length that is not a byte count or a body
                that is short, not UTF-8, or not JSON, and 413 for a body
                above MAX_BODY_BYTES (nothing of it is read).
        """
        transfer = self.headers.get("Transfer-Encoding")
        if transfer is not None and transfer.strip().lower() != "identity":
            raise HttpError(HTTP_LENGTH_REQUIRED, "body: chunked bodies are not accepted; send a Content-Length")
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise HttpError(HTTP_LENGTH_REQUIRED, "body: the Content-Length header is missing")
        if not length_text.strip().isdigit():
            raise HttpError(HTTP_BAD_REQUEST, f"body: Content-Length {length_text!r} is not a byte count")
        length = int(length_text.strip())
        if length > MAX_BODY_BYTES:
            raise HttpError(HTTP_PAYLOAD_TOO_LARGE, f"body: {length} bytes is larger than the limit of {MAX_BODY_BYTES} bytes")
        self._body_consumed = True
        data = self.rfile.read(length) if length else b""
        if len(data) != length:
            self.close_connection = True
            raise HttpError(HTTP_BAD_REQUEST, f"body: expected {length} bytes, got {len(data)}")
        if not data:
            return {}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HttpError(HTTP_BAD_REQUEST, f"body: not UTF-8 text ({error.reason})") from error
        try:
            return json.loads(text)
        except (ValueError, RecursionError) as error:
            raise HttpError(HTTP_BAD_REQUEST, f"body: not valid JSON ({error})") from error

    def _object_body(self) -> Dict[str, Any]:
        """Read the body and insist on a JSON object.

        Raises:
            HttpError: Under the _read_json_body rules, and 400 when the
                JSON is not an object.
        """
        body = self._read_json_body()
        if not isinstance(body, dict):
            raise HttpError(HTTP_BAD_REQUEST, f"body: expected a JSON object, got {type(body).__name__}")
        return body

    @staticmethod
    def _body_field(body: Mapping[str, Any], name: str) -> Any:
        """Take a required field out of a JSON object.

        Raises:
            HttpError: 400 ``<name>: missing``.
        """
        if name not in body:
            raise HttpError(HTTP_BAD_REQUEST, f"{name}: missing")
        return body[name]

    @staticmethod
    def _query_value(query: Mapping[str, List[str]], name: str) -> Optional[str]:
        """Read one query parameter.

        Returns:
            The value, or None when the parameter is absent.

        Raises:
            HttpError: 400 when the parameter appears more than once.
        """
        values = query.get(name)
        if values is None:
            return None
        if len(values) != 1:
            raise HttpError(HTTP_BAD_REQUEST, f"{name}: given {len(values)} times, expected once")
        return values[0]

    def _required_query(self, query: Mapping[str, List[str]], name: str) -> str:
        """Read a query parameter that must be present.

        Raises:
            HttpError: 400 ``<name>: missing`` or when it appears more than once.
        """
        value = self._query_value(query, name)
        if value is None:
            raise HttpError(HTTP_BAD_REQUEST, f"{name}: missing")
        return value

    def _query_flag(self, query: Mapping[str, List[str]], name: str) -> bool:
        """Read a ``name=1`` switch from the query.

        Returns:
            True for ``1``, False when the parameter is absent, empty, or ``0``.

        Raises:
            HttpError: 400 for any other value.
        """
        value = self._query_value(query, name)
        if value in (None, "", "0"):
            return False
        if value == "1":
            return True
        raise HttpError(HTTP_BAD_REQUEST, f"{name}: expected 1 or 0, got {value!r}")

    # ----- sending answers -----

    def _send_json(self, status: int, payload: Any, headers: Optional[Mapping[str, str]] = None) -> None:
        """Send a JSON answer with an exact Content-Length.

        Args:
            status: The HTTP status.
            payload: The value to encode.
            headers: Extra headers.
        """
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_body_headers(
        self, range_header: Optional[str], start: int, end: int, size: int, content_type: str, ranges: bool, cache_control: Optional[str]
    ) -> None:
        """Send the headers of a file or stream answer.

        Args:
            range_header: The request's Range header, or None. A header
                makes the answer 206 with a Content-Range.
            start: The first byte sent.
            end: The last byte sent (start - 1 for an empty body).
            size: The whole object's length.
            content_type: The Content-Type value.
            ranges: True advertises ``Accept-Ranges: bytes``.
            cache_control: A Cache-Control value, or None for no header.
        """
        status = HTTP_PARTIAL if range_header is not None else HTTP_OK
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        if status == HTTP_PARTIAL:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if cache_control is not None:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()

    def _parse_range(self, range_header: Optional[str], size: int) -> Tuple[int, int]:
        """Turn the Range header into an inclusive span, or answer 416.

        Raises:
            HttpError: 416 with ``Content-Range: bytes */<size>`` when the
                header is malformed or outside the object.
        """
        try:
            return parse_range_header(range_header, size)
        except RangeNotSatisfiable as error:
            raise HttpError(HTTP_RANGE_NOT_SATISFIABLE, str(error), {"Content-Range": f"bytes */{size}"}) from error

    def _send_file(
        self, path: Path, content_type: str, range_header: Optional[str], ranges: bool, cache_control: Optional[str]
    ) -> None:
        """Send a file from disk, whole or as one byte range.

        Args:
            path: An existing regular file.
            content_type: The Content-Type value.
            range_header: The request's Range header when ranges are
                honored, else None.
            ranges: True honors the Range header and advertises ranges.
            cache_control: A Cache-Control value, or None.

        Raises:
            HttpError: 404 when the file cannot be opened, 416 for a bad range.
        """
        try:
            handle = open(path, "rb")
        except OSError as error:
            raise HttpError(HTTP_NOT_FOUND, f"file: {path.name} could not be opened ({error.strerror})") from error
        with handle:
            size = os.fstat(handle.fileno()).st_size
            start, end = self._parse_range(range_header if ranges else None, size)
            self._send_body_headers(range_header if ranges else None, start, end, size, content_type, ranges, cache_control)
            handle.seek(start)
            remaining = end - start + 1
            self._sent_bytes = 0
            while remaining > 0:
                block = handle.read(min(FILE_BLOCK_BYTES, remaining))
                if not block:
                    self._note = f"{path.name} shrank while it was being sent"
                    self.close_connection = True
                    return
                self.wfile.write(block)
                self._sent_bytes += len(block)
                remaining -= len(block)

    def _safe_path(self, root: Path, rest: str) -> Path:
        """Resolve a request path inside a folder, refusing everything that leaves it.

        Args:
            root: The resolved folder the file must sit in.
            rest: The path text after the route prefix.

        Returns:
            The resolved path of an existing regular file inside root.

        Raises:
            HttpError: 400 for an empty, ``.``, or ``..`` segment, a
                backslash, or a NUL; 404 when the file is missing, is not a
                regular file, or resolves (through a symlink) outside root.
        """
        if any(char in rest for char in FORBIDDEN_PATH_CHARACTERS):
            raise HttpError(HTTP_BAD_REQUEST, f"path: {rest!r} contains a backslash or a NUL character")
        segments = rest.split("/")
        if any(segment in FORBIDDEN_SEGMENTS for segment in segments):
            raise HttpError(HTTP_BAD_REQUEST, f"path: {rest!r} has an empty, '.', or '..' segment")
        try:
            resolved = root.joinpath(*segments).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise HttpError(HTTP_NOT_FOUND, f"file: {rest} not found under {root.name}") from error
        if root not in resolved.parents or not resolved.is_file():
            raise HttpError(HTTP_NOT_FOUND, f"file: {rest} is not a file under {root.name}")
        return resolved

    # ----- routes -----

    def _get_index(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Serve the page."""
        static_dir = self.server.app.static_dir
        self._send_file(self._safe_path(static_dir, INDEX_FILE), CONTENT_TYPES["html"], None, False, NO_CACHE)

    def _get_static(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Serve one file from the static folder."""
        target = self._safe_path(self.server.app.static_dir, rest)
        self._send_file(target, CONTENT_TYPES.get(extension_of(target.name), BINARY_TYPE), None, False, NO_CACHE)

    def _get_media(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Serve a frame or a crop by file name."""
        folder, slash, name = rest.partition("/")
        if folder not in MEDIA_FOLDERS or not slash:
            raise HttpError(HTTP_NOT_FOUND, f"route: no route for GET {MEDIA_ROUTE}{rest}")
        if not MEDIA_NAME.fullmatch(name) or name in FORBIDDEN_SEGMENTS:
            raise HttpError(HTTP_BAD_REQUEST, f"name: {name!r} must match ^[A-Za-z0-9_.-]+$ and name a file")
        store = self.server.app.store
        root = (store.frames_dir if folder == MEDIA_FOLDERS[0] else store.crops_dir).resolve()
        target = self._safe_path(root, name)
        self._send_file(target, CONTENT_TYPES.get(extension_of(name), BINARY_TYPE), None, False, NO_CACHE)

    def _get_health(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Answer the health check."""
        self._send_json(HTTP_OK, {"ok": True, "version": VERSION})

    def _get_catalog(self, rest: str, query: Dict[str, List[str]]) -> None:
        """List a bucket folder. A missing prefix lists the top folder."""
        prefix = self._query_value(query, "prefix")
        refresh = self._query_flag(query, "refresh")
        self._send_json(HTTP_OK, self.server.app.catalog_page(KEY_PREFIX if prefix is None else prefix, refresh))

    def _get_species(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Answer the species list, the pins, the annotator, and the tally target."""
        self._send_json(HTTP_OK, self.server.app.species_payload())

    def _post_settings(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Change the annotator or the pins."""
        app = self.server.app
        body = self._object_body()
        try:
            payload = app.update_settings(body)
        except OSError as error:
            raise HttpError(HTTP_INTERNAL_ERROR, f"settings: could not save {app.data_dir / SETTINGS_FILE}: {error}") from error
        self._send_json(HTTP_OK, payload)

    def _get_observations(self, rest: str, query: Dict[str, List[str]]) -> None:
        """List the sightings of one video."""
        self._send_json(HTTP_OK, self.server.app.observation_rows(self._required_query(query, "key")))

    def _post_observation(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Save one sighting."""
        self._send_json(HTTP_CREATED, self.server.app.save_observation(self._object_body()))

    def _delete_observation(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Remove one sighting by the ID in the path."""
        if not rest or "/" in rest:
            raise HttpError(HTTP_NOT_FOUND, f"route: no route for DELETE {OBSERVATION_ROUTE}{rest}")
        self._send_json(HTTP_OK, self.server.app.delete_observation(rest))

    def _get_tally(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Answer the species tally."""
        self._send_json(HTTP_OK, self.server.app.tally_payload())

    def _post_open(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Record that a video was opened."""
        body = self._object_body()
        self._send_json(HTTP_OK, self.server.app.open_video(self._body_field(body, "key")))

    def _post_done(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Mark a video done or in progress."""
        body = self._object_body()
        key = self._body_field(body, "key")
        done = self._body_field(body, "done")
        self._send_json(HTTP_OK, self.server.app.finish_video(key, done))

    def _get_convert(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Report a conversion's status."""
        self._send_json(HTTP_OK, self.server.app.conversion_status(self._required_query(query, "key")))

    def _post_convert(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Queue a conversion."""
        body = self._object_body()
        self._send_json(HTTP_OK, self.server.app.start_conversion(self._body_field(body, "key")))

    def _get_prefetch(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Report how much of a video is cached."""
        self._send_json(HTTP_OK, self.server.app.prefetch_status(self._required_query(query, "key")))

    def _post_prefetch(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Queue a whole video for the cache."""
        body = self._object_body()
        self._send_json(HTTP_OK, self.server.app.start_prefetch(self._body_field(body, "key")))

    def _post_export(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Write the export package."""
        self._object_body()
        self._send_json(HTTP_OK, self.server.app.export())

    def _get_video(self, rest: str, query: Dict[str, List[str]]) -> None:
        """Stream a video from the relay, or a converted copy from disk."""
        key = self._required_query(query, "key")
        converted = self._query_flag(query, "converted")
        range_header = self.headers.get("Range")
        if converted:
            path = self.server.app.converter.converted_path(key)
            if path is None:
                raise HttpError(HTTP_NOT_FOUND, f"converted: {key} is not converted yet")
            self._send_file(path, VIDEO_TYPE, range_header, True, None)
            return
        self._stream_relay(key, range_header)

    def _stream_relay(self, key: str, range_header: Optional[str]) -> None:
        """Send one byte range of a bucket object through the relay.

        A relay failure after the headers went out (a chunk that failed
        after its retries, an object that changed, a closed relay) ends the
        answer early with one log line, because a status can no longer be
        sent.

        Args:
            key: The object key from the query.
            range_header: The request's Range header, or None for the whole object.
        """
        relay = self.server.app.relay
        size = relay.size(key)
        start, end = self._parse_range(range_header, size)
        content_type = VIDEO_TYPE if extension_of(key) in PLAYABLE_EXTENSIONS else BINARY_TYPE
        with relay.open_reader(key, start, end) as reader:
            self._send_body_headers(range_header, start, end, size, content_type, True, None)
            self._sent_bytes = 0
            try:
                for block in reader:
                    self.wfile.write(block)
                    self._sent_bytes += len(block)
            except RelayError as error:
                self._note = f"relay error: {error}"
                self.close_connection = True


def make_server(app: ScreenerApp, host: str, port: int) -> ThreadingHTTPServer:
    """Bind the server that runs the app's routes.

    The socket is bound here, and the bound port is written to ``app.port``
    so the converter's source URLs reach this server when the port was 0.
    Call ``serve_forever`` on the result to answer requests.

    Args:
        app: The app behind the routes.
        host: ``127.0.0.1`` or ``localhost``; the server never listens elsewhere.
        port: The port, 0 to 65535. 0 picks a free port.

    Returns:
        The bound ThreadingHTTPServer.

    Raises:
        ValueError: Starting with the argument name when app is not a
            ScreenerApp, host is not loopback, or port is out of range.
        OSError: When the port cannot be bound (in use, or not permitted).
    """
    if not isinstance(app, ScreenerApp):
        raise ValueError(f"app: expected a ScreenerApp, got {type(app).__name__}")
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"host: expected {' or '.join(LOOPBACK_HOSTS)}, got {host!r}; the server listens on loopback only")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError(f"port: expected a whole number from 0 to 65535, got {port!r}")
    server = _ScreenerServer((host, port), _Handler)
    server.app = app
    app.port = server.server_address[1]
    return server
