"""An in-process stand-in for the public S3 bucket, for tests only.

FakeS3 answers the three request shapes the app sends to the real bucket:

* ``GET /?list-type=2&prefix=&delimiter=/&continuation-token=`` with S3
  ListObjectsV2 XML,
* ``HEAD /<percent-encoded key>`` with the object's length,
* ``GET /<percent-encoded key>`` with ``Range`` support (206, 416, 404).

It speaks HTTP/1.1 with keep-alive, so a client can hold one connection open
across many requests the way the relay's workers do. Tests steer it through
``put``, ``fail_next``, ``truncate_next``, ``delay_seconds``, and
``page_size``, and read what arrived from ``requests``.
"""

import hashlib
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
S3_MAX_KEYS = 1000
LISTEN_BACKLOG = 128
BUCKET_NAME = "fake-bucket"
FIXED_LAST_MODIFIED = "Tue, 22 Oct 2024 12:00:00 GMT"

# The name fail_next uses for the listing route, which has no object key.
LISTING = ""


def _parse_range(header: Optional[str], size: int) -> Tuple[int, int, int]:
    """Work out the status and byte span for a Range header, the way S3 does.

    Args:
        header: The Range header value, or None when the request had none.
        size: The object length in bytes.

    Returns:
        ``(status, start, end)`` with an inclusive span. The status is 200
        when there is no usable Range header (S3 ignores a malformed one and
        sends the whole object), 206 for a span inside the object, and 416
        when the span starts at or past the end.
    """
    whole = (200, 0, size - 1)
    if header is None:
        return whole
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return whole
    first, dash, last = spec.strip().partition("-")
    if not dash:
        return whole
    first, last = first.strip(), last.strip()
    if first == "":
        if not last.isdigit():
            return whole
        suffix = int(last)
        if suffix == 0 or size == 0:
            return (416, 0, 0)
        return (206, max(0, size - suffix), size - 1)
    if not first.isdigit() or (last != "" and not last.isdigit()):
        return whole
    start = int(first)
    end = size - 1 if last == "" else min(int(last), size - 1)
    if last != "" and int(last) < start:
        return whole
    if start >= size:
        return (416, 0, 0)
    return (206, start, end)


class _QuietServer(ThreadingHTTPServer):
    """A threading HTTP server that stays silent when a client hangs up."""

    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Swallow connection errors and report everything else on stderr.

        Args:
            request: The client socket.
            client_address: The client's address pair.
        """
        error = sys.exc_info()[1]
        if isinstance(error, OSError):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    """Answers one connection's requests from the FakeS3 that owns the server."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Drop the default per-request log line so test output stays clean."""

    def setup(self) -> None:
        """Register the client socket so FakeS3 can cut it on demand."""
        super().setup()
        self.server.fake._track(self.connection)

    def finish(self) -> None:
        """Forget the client socket, even when the final flush fails."""
        try:
            super().finish()
        except OSError:
            pass
        finally:
            self.server.fake._untrack(self.connection)

    def do_GET(self) -> None:  # noqa: N802
        """Answer a GET for the listing or for one object."""
        self.server.fake._answer(self, "GET")

    def do_HEAD(self) -> None:  # noqa: N802
        """Answer a HEAD for the bucket or for one object."""
        self.server.fake._answer(self, "HEAD")


class FakeS3:
    """A small public-bucket imitation that runs on 127.0.0.1.

    Attributes:
        requests: One dict per request received, in arrival order, with the
            keys ``method``, ``path`` (raw, still percent-encoded, no query),
            ``query`` (dict of decoded parameters), ``key`` (decoded object
            key, or ``""`` for the listing), ``range`` (the Range header or
            None), and ``time`` (``time.time()`` at arrival).
        delay_seconds: Every request waits this long before it is answered.
        page_size: The most entries (objects plus folders) one listing page
            holds. A small value forces pagination.
        in_flight: Requests being answered right now.
        max_in_flight: The highest value ``in_flight`` has reached.
    """

    def __init__(self) -> None:
        """Create an empty bucket. Call start() to begin answering."""
        self.requests: List[Dict[str, Any]] = []
        self.delay_seconds: float = 0.0
        self.page_size: int = S3_MAX_KEYS
        self.in_flight: int = 0
        self.max_in_flight: int = 0
        self._objects: Dict[str, bytes] = {}
        self._failures: Dict[Tuple[str, str], List[int]] = {}
        self._truncations: Dict[str, int] = {}
        self._tokens: Dict[str, int] = {}
        self._token_count = 0
        self._sockets: List[socket.socket] = []
        self._lock = threading.Lock()
        self._server: Optional[_QuietServer] = None
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "FakeS3":
        """Start the server and return this object, for ``with`` blocks."""
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Stop the server when the ``with`` block ends."""
        self.stop()

    def start(self) -> str:
        """Start answering on a free port of 127.0.0.1.

        Returns:
            The base URL, such as ``http://127.0.0.1:50123``, with no
            trailing slash.

        Raises:
            RuntimeError: When the server is already running.
        """
        if self._server is not None:
            raise RuntimeError("FakeS3.start: the server is already running")
        server = _QuietServer(("127.0.0.1", 0), _Handler)
        server.fake = self
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self._server, self._thread = server, thread
        return self.url

    @property
    def url(self) -> str:
        """Return the base URL of the running server.

        Raises:
            RuntimeError: When the server is not running.
        """
        if self._server is None:
            raise RuntimeError("FakeS3.url: the server is not running")
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    def stop(self) -> None:
        """Stop listening and cut every open connection.

        After this call a new connection is refused and a kept-alive
        connection is dead, which is what a lost network looks like to the
        app. Calling stop() twice is harmless.
        """
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self.drop_connections()

    def drop_connections(self) -> None:
        """Cut every open client connection while the server keeps running.

        This imitates S3 closing idle kept-alive connections: the client
        finds out only when its next request on that connection fails.
        """
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def put(self, key: str, body: bytes) -> None:
        """Store or replace an object.

        Args:
            key: The object key, unencoded, such as
                ``TCRMP_video_ondeck/2024Annual/x.MP4``.
            body: The object's bytes. Empty bodies are allowed.

        Raises:
            TypeError: When key is not text or body is not bytes.
        """
        if not isinstance(key, str) or not key:
            raise TypeError("FakeS3.put: key must be non-empty text, got %r" % (key,))
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError("FakeS3.put: body must be bytes, got %s" % type(body).__name__)
        with self._lock:
            self._objects[key] = bytes(body)

    def fail_next(self, key: str, times: int, status: int = 500, method: str = "GET") -> None:
        """Make the next requests for one key fail with an HTTP error.

        Args:
            key: The object key, or ``fakes3.LISTING`` for the listing route.
            times: How many requests fail before answers return to normal.
                Zero clears a failure set earlier.
            status: The HTTP status to send.
            method: ``"GET"`` (the default) or ``"HEAD"``. Only requests with
                this method count and fail.

        Raises:
            ValueError: When times is negative or method is not GET or HEAD.
        """
        if not isinstance(times, int) or times < 0:
            raise ValueError("FakeS3.fail_next: times must be a whole number of 0 or more, got %r" % (times,))
        if method not in ("GET", "HEAD"):
            raise ValueError("FakeS3.fail_next: method must be GET or HEAD, got %r" % (method,))
        with self._lock:
            self._failures[(method, key)] = [times, int(status)]

    def truncate_next(self, key: str, times: int) -> None:
        """Make the next GETs for one key send half their body, then hang up.

        The answer promises the full Content-Length, delivers the first half
        of those bytes, and closes the connection.

        Args:
            key: The object key.
            times: How many GETs are cut short. Zero clears the setting.

        Raises:
            ValueError: When times is negative.
        """
        if not isinstance(times, int) or times < 0:
            raise ValueError("FakeS3.truncate_next: times must be a whole number of 0 or more, got %r" % (times,))
        with self._lock:
            self._truncations[key] = times

    def gets(self, key: str) -> List[Dict[str, Any]]:
        """Return the recorded GET requests for one object key, oldest first.

        Args:
            key: The decoded object key.

        Returns:
            A new list of the matching entries of ``requests``.
        """
        with self._lock:
            return [entry for entry in self.requests if entry["method"] == "GET" and entry["key"] == key]

    def count(self, method: str, key: Optional[str] = None) -> int:
        """Count recorded requests.

        Args:
            method: ``"GET"`` or ``"HEAD"``.
            key: When given, count only requests for this decoded key
                (``fakes3.LISTING`` counts listing requests).

        Returns:
            The number of matching requests received so far.
        """
        with self._lock:
            return sum(
                1 for entry in self.requests if entry["method"] == method and (key is None or entry["key"] == key)
            )

    # ----- internals used by the handler -----

    def _track(self, sock: socket.socket) -> None:
        """Remember an open client socket so stop() can cut it."""
        with self._lock:
            self._sockets.append(sock)

    def _untrack(self, sock: socket.socket) -> None:
        """Forget a client socket that has closed."""
        with self._lock:
            if sock in self._sockets:
                self._sockets.remove(sock)

    def _answer(self, handler: _Handler, method: str) -> None:
        """Record one request and send its answer.

        Args:
            handler: The request handler that holds the socket and headers.
            method: ``"GET"`` or ``"HEAD"``.
        """
        parts = urllib.parse.urlsplit(handler.path)
        query = {name: values[0] for name, values in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
        # S3 reads a bare plus sign in a path as a space, so a client that
        # forgets to encode "+" asks for the wrong key here too.
        key = urllib.parse.unquote(parts.path[1:].replace("+", " "))
        with self._lock:
            self.requests.append(
                {
                    "method": method,
                    "path": parts.path,
                    "query": query,
                    "key": key,
                    "range": handler.headers.get("Range"),
                    "time": time.time(),
                }
            )
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            delay = self.delay_seconds
        try:
            if delay > 0:
                time.sleep(delay)
            if parts.path == "/":
                self._answer_listing(handler, method, query)
            else:
                self._answer_object(handler, method, key)
        finally:
            with self._lock:
                self.in_flight -= 1

    def _take_failure(self, method: str, key: str) -> Optional[int]:
        """Use up one injected failure for a method and key.

        Returns:
            The status to send, or None when no failure is pending.
        """
        with self._lock:
            pending = self._failures.get((method, key))
            if not pending or pending[0] <= 0:
                return None
            pending[0] -= 1
            return pending[1]

    def _take_truncation(self, key: str) -> bool:
        """Use up one injected truncation for a key.

        Returns:
            True when this GET must be cut short.
        """
        with self._lock:
            left = self._truncations.get(key, 0)
            if left <= 0:
                return False
            self._truncations[key] = left - 1
            return True

    def _send(self, handler: _Handler, method: str, status: int, headers: Dict[str, str], body: bytes) -> None:
        """Send a complete answer with an exact Content-Length.

        Args:
            handler: The request handler to answer through.
            method: The request method; HEAD answers carry no body.
            status: The HTTP status.
            headers: Extra headers. ``Content-Length`` is filled in from body
                unless the caller set it.
            body: The payload.
        """
        handler.send_response(status)
        headers = dict(headers)
        headers.setdefault("Content-Length", str(len(body)))
        for name, value in headers.items():
            handler.send_header(name, value)
        handler.end_headers()
        if method != "HEAD" and body:
            handler.wfile.write(body)
        handler.wfile.flush()

    def _send_error(self, handler: _Handler, method: str, status: int, code: str, message: str) -> None:
        """Send an S3-style XML error.

        Args:
            handler: The request handler to answer through.
            method: The request method.
            status: The HTTP status.
            code: The S3 error code, such as ``NoSuchKey``.
            message: A sentence for the ``Message`` element.
        """
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>%s</Code><Message>%s</Message></Error>'
            % (escape(code), escape(message))
        ).encode("utf-8")
        self._send(handler, method, status, {"Content-Type": "application/xml"}, body)

    def _answer_listing(self, handler: _Handler, method: str, query: Dict[str, str]) -> None:
        """Answer ``/``: a ListObjectsV2 page for GET, an empty 200 for HEAD."""
        status = self._take_failure(method, LISTING)
        if status is not None:
            self._send_error(handler, method, status, "InternalError", "injected listing failure")
            return
        if method == "HEAD":
            self._send(handler, method, 200, {"Content-Type": "application/xml"}, b"")
            return
        if query.get("list-type") != "2":
            self._send_error(handler, method, 400, "InvalidArgument", "FakeS3 answers only list-type=2")
            return
        prefix = query.get("prefix", "")
        delimiter = query.get("delimiter", "")
        token = query.get("continuation-token")
        try:
            max_keys = int(query.get("max-keys", S3_MAX_KEYS))
        except ValueError:
            self._send_error(handler, method, 400, "InvalidArgument", "max-keys is not a number")
            return
        with self._lock:
            sizes = {name: len(body) for name, body in self._objects.items()}
            page_size = max(1, min(int(self.page_size), max_keys, S3_MAX_KEYS))
            offset = 0 if token is None else self._tokens.get(token, -1)
        if offset < 0:
            self._send_error(handler, method, 400, "InvalidArgument", "The continuation token provided is incorrect")
            return

        entries: List[Tuple[str, bool]] = []
        seen_prefixes = set()
        for name in sorted(sizes):
            if not name.startswith(prefix):
                continue
            rest = name[len(prefix):]
            if delimiter and delimiter in rest:
                common = prefix + rest[: rest.index(delimiter) + len(delimiter)]
                if common not in seen_prefixes:
                    seen_prefixes.add(common)
                    entries.append((common, True))
            else:
                entries.append((name, False))

        page = entries[offset: offset + page_size]
        truncated = offset + page_size < len(entries)
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<ListBucketResult xmlns="%s">' % S3_NAMESPACE,
            "<Name>%s</Name>" % BUCKET_NAME,
            "<Prefix>%s</Prefix>" % escape(prefix),
            "<KeyCount>%d</KeyCount>" % len(page),
            "<MaxKeys>%d</MaxKeys>" % page_size,
        ]
        if delimiter:
            lines.append("<Delimiter>%s</Delimiter>" % escape(delimiter))
        lines.append("<IsTruncated>%s</IsTruncated>" % ("true" if truncated else "false"))
        if token is not None:
            lines.append("<ContinuationToken>%s</ContinuationToken>" % escape(token))
        if truncated:
            lines.append("<NextContinuationToken>%s</NextContinuationToken>" % escape(self._new_token(offset + page_size)))
        for name, is_prefix in page:
            if not is_prefix:
                lines.append(
                    "<Contents><Key>%s</Key><LastModified>2024-10-22T12:00:00.000Z</LastModified>"
                    "<ETag>&quot;%s&quot;</ETag><Size>%d</Size><StorageClass>STANDARD</StorageClass></Contents>"
                    % (escape(name), hashlib.md5(name.encode("utf-8")).hexdigest(), sizes[name])
                )
        for name, is_prefix in page:
            if is_prefix:
                lines.append("<CommonPrefixes><Prefix>%s</Prefix></CommonPrefixes>" % escape(name))
        lines.append("</ListBucketResult>")
        self._send(handler, method, 200, {"Content-Type": "application/xml"}, "\n".join(lines).encode("utf-8"))

    def _new_token(self, offset: int) -> str:
        """Issue a continuation token for a listing offset.

        The token carries ``+``, ``/``, and ``=`` the way real S3 tokens do,
        so a client that forgets to percent-encode it sends back a token this
        server does not know and gets a 400.

        Args:
            offset: The index of the first entry of the next page.

        Returns:
            The opaque token text.
        """
        with self._lock:
            self._token_count += 1
            token = "1+tok/%d/%d==" % (self._token_count, offset)
            self._tokens[token] = offset
        return token

    def _answer_object(self, handler: _Handler, method: str, key: str) -> None:
        """Answer HEAD or GET for one object, with Range support."""
        status = self._take_failure(method, key)
        if status is not None:
            self._send_error(handler, method, status, "InternalError", "injected failure for %s" % key)
            return
        with self._lock:
            body = self._objects.get(key)
        if body is None:
            self._send_error(handler, method, 404, "NoSuchKey", "The specified key does not exist: %s" % key)
            return
        size = len(body)
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": "binary/octet-stream",
            "ETag": '"%s"' % hashlib.md5(body).hexdigest(),
            "Last-Modified": FIXED_LAST_MODIFIED,
        }
        range_status, start, end = _parse_range(handler.headers.get("Range"), size)
        if range_status == 416:
            headers["Content-Range"] = "bytes */%d" % size
            self._send_error_with_headers(handler, method, headers)
            return
        payload = body[start: end + 1]
        if range_status == 206:
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        if method == "HEAD":
            headers["Content-Length"] = str(len(payload))
            self._send(handler, method, range_status, headers, b"")
            return
        if self._take_truncation(key):
            headers["Content-Length"] = str(len(payload))
            handler.close_connection = True
            self._send(handler, method, range_status, headers, payload[: len(payload) // 2])
            return
        self._send(handler, method, range_status, headers, payload)

    def _send_error_with_headers(self, handler: _Handler, method: str, headers: Dict[str, str]) -> None:
        """Send the 416 answer, which carries ``Content-Range: bytes */size``.

        Args:
            handler: The request handler to answer through.
            method: The request method.
            headers: The object headers, including Content-Range.
        """
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>InvalidRange</Code>'
            "<Message>The requested range is not satisfiable</Message></Error>"
        ).encode("utf-8")
        headers = dict(headers)
        headers["Content-Type"] = "application/xml"
        self._send(handler, method, 416, headers, body)
