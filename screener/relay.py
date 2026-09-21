"""Relay of bucket videos through an on-disk chunk cache.

One connection from the lab Mac to the bucket carries 11 to 20 Mbps and the
videos run at 52 Mbps, so a browser that streams straight from S3 stalls.
ChunkRelay cuts every object into fixed-size chunks, fetches missing chunks
over many persistent connections at once, keeps them on disk, and hands any
byte range back in order through a RangeReader. A reader's next chunk jumps
the work queue, the chunks after it are fetched ahead of need, and whole
videos can be queued in the background.

Thread safety: one lock guards the queue and every per-video record. No
network request and no disk read or write happens while that lock is held.

The bucket closes a kept-alive connection after about five idle seconds
(measured), and a new HTTPS connection costs most of a second. The queue
therefore hands each new task to the worker that went idle last, so steady
playback keeps a few connections warm and leaves the rest of the pool for
bursts.
"""

import heapq
import http.client
import itertools
import json
import math
import os
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
import urllib.parse
import weakref
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from screener.config import (
    CACHE_CAP_BYTES,
    CACHE_KEEP_SECONDS,
    CHUNK_RETRIES,
    CHUNK_SIZE,
    READ_AHEAD_CHUNKS,
    RELAY_WORKERS,
)
from screener.keys import cache_id, quote_key, validate_key

# Pause before the second, third, and fourth try of one chunk. Later tries
# reuse the last value.
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)

CHUNKS_FOLDER = "chunks"
META_NAME = "meta.json"
CHUNK_SUFFIX = ".bin"
PART_SUFFIX = ".part"
CHUNK_NAME_DIGITS = 6

# Work queue priorities. A lower number is served first.
PRIORITY_DEMAND = 0
PRIORITY_READ_AHEAD = 1
PRIORITY_PREFETCH = 2

# A waiting reader wakes this often to recheck its chunk, so one lost wake-up
# costs a second instead of a hang.
READER_WAKE_SECONDS = 1.0
# close() waits this long for each worker thread to end.
WORKER_JOIN_SECONDS = 5.0
# How often open_reader and prefetch start over when eviction or an object
# change retires the cache entry under them.
ENTRY_ATTEMPTS = 3
# The most times one reader refetches a chunk whose cached file has vanished.
VANISHED_FILE_ATTEMPTS = 3
# Byte positions in a Range header longer than this are treated as garbage.
# S3 objects top out at 5 TB, which is 13 digits.
MAX_RANGE_DIGITS = 20

HTTP_OK = 200
HTTP_PARTIAL = 206
HTTP_NOT_FOUND = 404
HTTP_REQUEST_TIMEOUT = 408
HTTP_RANGE_NOT_SATISFIABLE = 416
HTTP_TOO_MANY_REQUESTS = 429

_RANGE_HEADER = re.compile(
    r"^\s*bytes\s*=\s*([0-9]{0,%d})\s*-\s*([0-9]{0,%d})\s*$" % (MAX_RANGE_DIGITS, MAX_RANGE_DIGITS),
    re.IGNORECASE | re.ASCII,
)
_CONTENT_RANGE = re.compile(r"^\s*bytes\s+([0-9]+)-([0-9]+)/([0-9]+)\s*$", re.IGNORECASE | re.ASCII)
_CHUNK_NAME = re.compile(r"^([0-9]{%d,})%s$" % (CHUNK_NAME_DIGITS, re.escape(CHUNK_SUFFIX)), re.ASCII)


class RelayError(Exception):
    """Raised when the relay cannot size, fetch, or deliver part of an object."""


class RangeNotSatisfiable(RelayError):
    """Raised when a requested byte range is malformed or outside the object."""


class ObjectNotFound(RelayError):
    """Raised when the bucket answers 404 for a key, so a server can answer 404 too."""


class _AttemptFailed(Exception):
    """One try at a chunk failed in a way that another try may fix."""


class _ChunkFailed(Exception):
    """A chunk is given up. The message is the text the waiting reader raises."""


class _ObjectChanged(_ChunkFailed):
    """The bucket's object no longer matches the cached size, so the cache entry is void."""


def _is_whole_number(value: Any) -> bool:
    """Tell whether a value is an int and not a bool.

    Args:
        value: Anything.

    Returns:
        True for ints other than True and False.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def parse_range_header(header: Optional[str], size: int) -> Tuple[int, int]:
    """Turn an HTTP Range header into an inclusive byte span inside an object.

    Args:
        header: The Range header value, or None when the request had none.
            ``bytes=a-b``, ``bytes=a-``, and ``bytes=-n`` are understood.
        size: The object length in bytes.

    Returns:
        ``(start, end)``, both inclusive. None gives the whole object, which
        is ``(0, size - 1)`` and therefore ``(0, -1)`` for an empty object.
        An end past the object is pulled back to the last byte.

    Raises:
        ValueError: When size is not a whole number of zero or more.
        RangeNotSatisfiable: For several ranges in one header, text that is
            not a byte range, a start after the end, a start at or past the
            object's length, and a suffix of zero bytes.
    """
    if not _is_whole_number(size) or size < 0:
        raise ValueError(f"size: expected a whole number of bytes of zero or more, got {size!r}")
    if header is None:
        return (0, size - 1)
    if not isinstance(header, str):
        raise RangeNotSatisfiable(f"Range header: expected text, got {type(header).__name__}")
    if "," in header:
        raise RangeNotSatisfiable(f"Range header {header!r}: several ranges in one request are not supported")
    match = _RANGE_HEADER.match(header)
    if match is None or (match.group(1) == "" and match.group(2) == ""):
        raise RangeNotSatisfiable(f"Range header {header!r}: not a byte range such as bytes=0-1023")
    first, last = match.group(1), match.group(2)
    if first == "":
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiable(f"Range header {header!r}: no bytes to send from an object of {size} bytes")
        return (max(0, size - suffix), size - 1)
    start = int(first)
    if start >= size:
        raise RangeNotSatisfiable(f"Range header {header!r}: starts at or past the end of an object of {size} bytes")
    if last == "":
        return (start, size - 1)
    end = int(last)
    if end < start:
        raise RangeNotSatisfiable(f"Range header {header!r}: the end comes before the start")
    return (start, min(end, size - 1))


def chunk_span(start: int, end: int, chunk_size: int) -> range:
    """Return the indexes of the chunks that hold an inclusive byte span.

    Args:
        start: The first byte wanted.
        end: The last byte wanted, at or after start.
        chunk_size: The chunk length in bytes, above zero.

    Returns:
        A range of chunk indexes, first to last.

    Raises:
        ValueError: When a value is not a whole number, start is negative,
            end comes before start, or chunk_size is below one.
    """
    for name, value in (("start", start), ("end", end), ("chunk_size", chunk_size)):
        if not _is_whole_number(value):
            raise ValueError(f"{name}: expected a whole number, got {value!r}")
    if start < 0 or end < start:
        raise ValueError(f"start and end: expected 0 <= start <= end, got {start} and {end}")
    if chunk_size < 1:
        raise ValueError(f"chunk_size: expected one byte or more, got {chunk_size}")
    return range(start // chunk_size, end // chunk_size + 1)


def _tries(count: int) -> str:
    """Return "1 try" or "N tries" for an error message.

    Args:
        count: How many tries were made.

    Returns:
        The count with the right noun.
    """
    return "1 try" if count == 1 else f"{count} tries"


def _describe(error: BaseException) -> str:
    """Put a network or disk error into words for an error message.

    Args:
        error: The exception one try ended with.

    Returns:
        A short reason. The relay's own messages pass through unchanged.
    """
    if isinstance(error, (_AttemptFailed, _ChunkFailed)):
        return str(error)
    if isinstance(error, socket.timeout):
        return "the bucket did not answer in time"
    if isinstance(error, http.client.IncompleteRead):
        return f"the connection closed after {len(error.partial)} bytes of the body"
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


class _Task:
    """One chunk waiting in, or taken from, the work queue.

    Attributes:
        video: The cache entry the chunk belongs to.
        index: The chunk index.
        priority: PRIORITY_DEMAND, PRIORITY_READ_AHEAD, or PRIORITY_PREFETCH.
        prefetch: True when a background prefetch also wants this chunk, so
            a dropped read-ahead falls back to the prefetch queue.
        state: ``queued``, ``running``, ``done``, ``failed``, or ``dropped``.
        error: The reason, once state is ``failed``.
    """

    __slots__ = ("video", "index", "priority", "prefetch", "state", "error")

    def __init__(self, video: "_Video", index: int, priority: int, prefetch: bool) -> None:
        """Create a queued task.

        Args:
            video: The cache entry the chunk belongs to.
            index: The chunk index.
            priority: The queue priority.
            prefetch: True when a background prefetch asked for the chunk.
        """
        self.video = video
        self.index = index
        self.priority = priority
        self.prefetch = prefetch
        self.state = "queued"
        self.error = ""


class _Video:
    """The relay's record of one object: its size, its folder, and its chunks.

    Every attribute except the constants (key, folder, size, chunk_count) is
    read and written only while the relay's lock is held.

    Attributes:
        key: The object key.
        folder: The cache folder for this key.
        size: The object length in bytes.
        chunk_count: How many chunks the object has.
        have: Indexes of the chunks on disk.
        pending: Queued and running tasks by chunk index.
        readers: Open readers on this object.
        valid: False once eviction or an object change retires this record.
        retired_reason: Why the record was retired.
    """

    def __init__(self, key: str, folder: Path, size: int, chunk_size: int, have: Set[int]) -> None:
        """Create the record.

        Args:
            key: The object key.
            folder: The cache folder for this key.
            size: The object length in bytes.
            chunk_size: The relay's chunk length.
            have: Indexes of the chunks already on disk.
        """
        self.key = key
        self.folder = folder
        self.size = size
        self.chunk_count = (size + chunk_size - 1) // chunk_size
        self.have = have
        self.pending: Dict[int, _Task] = {}
        self.readers: "weakref.WeakSet[RangeReader]" = weakref.WeakSet()
        self.valid = True
        self.retired_reason = ""


class ChunkRelay:
    """Serves byte ranges of bucket objects from a chunk cache it fills in parallel.

    One relay serves every server thread. ``open_reader`` hands out a
    RangeReader per request, ``prefetch`` queues a whole video in the
    background, ``evict`` keeps the cache under its cap, and ``close`` stops
    the worker threads.
    """

    def __init__(
        self,
        bucket_url: str,
        cache_root: Path,
        chunk_size: int = CHUNK_SIZE,
        workers: int = RELAY_WORKERS,
        read_ahead: int = READ_AHEAD_CHUNKS,
        retries: int = CHUNK_RETRIES,
        cap_bytes: int = CACHE_CAP_BYTES,
        keep_seconds: int = CACHE_KEEP_SECONDS,
        timeout: float = 30.0,
    ) -> None:
        """Check the settings, clean the cache folder, and start the workers.

        Startup cleaning removes chunk files with the wrong length, leftover
        ``.part`` files, and every video folder whose ``meta.json`` is
        missing, unreadable, or written for another chunk size.

        Args:
            bucket_url: The bucket's base URL, ``http://`` or ``https://``.
            cache_root: The cache folder. Chunks go under ``chunks/`` inside.
            chunk_size: Bytes per chunk.
            workers: How many fetch threads run, each with one persistent
                connection.
            read_ahead: How many chunks past a reader's position are fetched
                before the reader asks.
            retries: How many tries one chunk (or one size request) gets.
            cap_bytes: The cache size evict() works toward.
            keep_seconds: evict() leaves a video alone for this long after a
                reader last opened it.
            timeout: Seconds to wait on the bucket for a connection or a
                read.

        Raises:
            ValueError: When an argument has the wrong type or range. The
                message names the argument.
            RelayError: When the cache folder cannot be created.
        """
        if not isinstance(bucket_url, str):
            raise ValueError(f"bucket_url: expected text, got {type(bucket_url).__name__}")
        parts = urllib.parse.urlsplit(bucket_url)
        try:
            port = parts.port
        except ValueError:
            port = -1
        if parts.scheme not in ("http", "https") or not parts.hostname or port == -1 or parts.query or parts.fragment:
            raise ValueError(f"bucket_url: expected an http or https URL with a host and no query, got {bucket_url!r}")
        if not isinstance(cache_root, (str, os.PathLike)):
            raise ValueError(f"cache_root: expected a folder path, got {type(cache_root).__name__}")
        for name, value, least in (
            ("chunk_size", chunk_size, 1),
            ("workers", workers, 1),
            ("read_ahead", read_ahead, 0),
            ("retries", retries, 1),
            ("cap_bytes", cap_bytes, 0),
        ):
            if not _is_whole_number(value) or value < least:
                raise ValueError(f"{name}: expected a whole number of {least} or more, got {value!r}")
        for name, value, allow_zero in (("keep_seconds", keep_seconds, True), ("timeout", timeout, False)):
            is_number = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            if not is_number or value < 0 or (value == 0 and not allow_zero):
                floor = "zero or more" if allow_zero else "above zero"
                raise ValueError(f"{name}: expected a number of seconds {floor}, got {value!r}")

        self._bucket_url = bucket_url.rstrip("/")
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = port
        self._base_path = parts.path.rstrip("/")
        self._ssl_context = ssl.create_default_context() if parts.scheme == "https" else None
        self._chunks_root = Path(cache_root) / CHUNKS_FOLDER
        self._chunk_size = chunk_size
        self._read_ahead = read_ahead
        self._retries = retries
        self._cap_bytes = cap_bytes
        self._keep_seconds = float(keep_seconds)
        self._timeout = float(timeout)

        # One lock, several conditions on it: readers wait on _cond for their
        # chunk, and each worker waits on its own condition so the queue can
        # choose which worker to wake.
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._worker_conds = [threading.Condition(self._lock) for _ in range(workers)]
        self._idle: List[int] = []
        self._stop = threading.Event()
        self._closed = False
        self._videos: Dict[str, _Video] = {}
        self._loading: Dict[str, threading.Event] = {}
        self._heap: List[Tuple[int, int, _Task]] = []
        self._sequence = itertools.count()
        self._queue_grew = False
        self._connections: Dict[int, http.client.HTTPConnection] = {}

        try:
            self._chunks_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RelayError(f"cache folder {self._chunks_root}: could not create it: {_describe(error)}") from error
        self._clean_cache()
        self._threads = [
            threading.Thread(target=self._work, args=(slot,), name=f"relay-worker-{slot}", daemon=True)
            for slot in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    # ----- public interface -----

    @property
    def chunk_size(self) -> int:
        """Return the chunk length in bytes."""
        return self._chunk_size

    def size(self, key: str) -> int:
        """Return an object's length in bytes.

        The first call for a key sends one HEAD to the bucket and saves
        ``{key, size, chunk_size}`` in the key's ``meta.json``. Later calls,
        in this run and the next, answer from that file.

        Args:
            key: The object key.

        Returns:
            The length in bytes.

        Raises:
            InvalidKey: When the key breaks the key rules. No request is sent.
            ObjectNotFound: When the bucket has no such object. It is a
                RelayError, and the message names the key and the 404.
            RelayError: When the size request fails on every try, or the
                relay is closed.
        """
        return self._video(key).size

    def open_reader(self, key: str, start: int, end: int) -> "RangeReader":
        """Open a reader on an inclusive byte range of an object.

        Opening starts the fetch of the first chunk at once, queues the
        read-ahead behind it, and touches ``meta.json`` so eviction treats
        the video as recently used.

        Args:
            key: The object key.
            start: The first byte wanted.
            end: The last byte wanted. ``(0, -1)`` is accepted for an empty
                object and yields nothing.

        Returns:
            A RangeReader. Use it as a context manager so it is closed when
            the client hangs up.

        Raises:
            InvalidKey: When the key breaks the key rules.
            ValueError: When start or end is not a whole number.
            RangeNotSatisfiable: When the range is reversed or reaches
                outside the object.
            ObjectNotFound: When the bucket has no such object.
            RelayError: When the object cannot be sized or the relay is
                closed.
        """
        for name, value in (("start", start), ("end", end)):
            if not _is_whole_number(value):
                raise ValueError(f"{name}: expected a whole number, got {value!r}")
        for _ in range(ENTRY_ATTEMPTS):
            video = self._video(key)
            whole_empty_object = video.size == 0 and start == 0 and end == -1
            if not whole_empty_object and not 0 <= start <= end < video.size:
                raise RangeNotSatisfiable(f"bytes {start}-{end} of {key}: the object has {video.size} bytes")
            self._touch(video)
            with self._cond:
                if self._closed:
                    raise RelayError("relay closed")
                if not video.valid:
                    continue
                reader = RangeReader(self, video, start, end)
                video.readers.add(reader)
                if reader.length:
                    self._schedule_locked(video, reader, start // self._chunk_size)
                return reader
        raise RelayError(f"open {key}: its cache entry was retired {ENTRY_ATTEMPTS} times in a row, try again")

    def prefetch(self, key: str) -> None:
        """Queue every missing chunk of an object at background priority.

        The call returns at once. Readers' chunks and read-ahead are always
        served first. ``cached_fraction`` reports the progress.

        Args:
            key: The object key.

        Raises:
            InvalidKey: When the key breaks the key rules.
            RelayError: When the object cannot be sized or the relay is
                closed.
        """
        for _ in range(ENTRY_ATTEMPTS):
            video = self._video(key)
            self._touch(video)
            with self._cond:
                if self._closed:
                    raise RelayError("relay closed")
                if not video.valid:
                    continue
                for index in range(video.chunk_count):
                    if index not in video.have:
                        self._want_locked(video, index, PRIORITY_PREFETCH, prefetch=True)
                self._wake_workers_locked()
                return
        raise RelayError(f"prefetch {key}: its cache entry was retired {ENTRY_ATTEMPTS} times in a row, try again")

    def cached_fraction(self, key: str) -> float:
        """Return how much of an object is on disk, by bytes.

        This never contacts the bucket. A key the relay has not sized yet has
        nothing cached and reports 0.0.

        Args:
            key: The object key.

        Returns:
            A number from 0.0 to 1.0. An empty object reports 1.0.

        Raises:
            InvalidKey: When the key breaks the key rules.
            RelayError: When the relay is closed.
        """
        video = self._video(key, allow_head=False)
        if video is None:
            return 0.0
        if video.size == 0:
            return 1.0
        with self._cond:
            cached = len(video.have) * self._chunk_size
            if video.chunk_count - 1 in video.have:
                cached -= video.chunk_count * self._chunk_size - video.size
        return cached / video.size

    def evict(self) -> int:
        """Remove whole video folders, least recently opened first, until the cache fits its cap.

        A folder is left alone when a reader opened it within
        ``keep_seconds`` (by the modification time of its ``meta.json``),
        when a reader is open on it, or when any of its chunks is queued or
        being fetched. The cache can therefore stay above the cap.

        Returns:
            The number of video folders removed.
        """
        now = time.time()
        measured = []
        total = 0
        try:
            entries = [entry for entry in os.scandir(self._chunks_root) if entry.is_dir(follow_symlinks=False)]
        except OSError:
            return 0
        for entry in entries:
            used, stamp = self._measure(Path(entry.path))
            total += used
            measured.append((stamp, entry.path, used))
        removed = 0
        for stamp, path, used in sorted(measured):
            if total <= self._cap_bytes:
                break
            if now - stamp < self._keep_seconds:
                continue
            folder = Path(path)
            meta = self._read_meta(folder)
            key = meta[0] if meta is not None and cache_id(meta[0]) == folder.name else None
            with self._cond:
                if self._closed:
                    break
                guard = self._claim_folder_locked(folder, key)
            if guard is None:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            self._release_guard(key, guard)
            total -= used
            removed += 1
        return removed

    def close(self) -> None:
        """Stop the workers and close their connections.

        Readers that are waiting for a chunk raise ``RelayError("relay
        closed")``. Every later call on the relay raises the same error.
        Calling close() twice is harmless.
        """
        with self._cond:
            if self._closed:
                return
            self._closed = True
            connections = list(self._connections.values())
            self._connections.clear()
            self._cond.notify_all()
            for condition in self._worker_conds:
                condition.notify_all()
        self._stop.set()
        for connection in connections:
            self._hang_up(connection)
        for thread in self._threads:
            thread.join(WORKER_JOIN_SECONDS)

    # ----- cache entries: loading, sizing, and the files on disk -----

    def _video(self, key: Any, allow_head: bool = True) -> Optional[_Video]:
        """Return the record for a key, loading it from disk or the bucket once.

        When several threads ask for a new key together, one of them loads it
        and the rest wait for that result, so the bucket sees one HEAD.

        Args:
            key: The object key, not yet validated.
            allow_head: False forbids the HEAD request. The answer is then
                None when the disk holds no usable ``meta.json``.

        Returns:
            The record, or None when allow_head is False and nothing is
            cached.

        Raises:
            InvalidKey: When the key breaks the key rules.
            RelayError: When the relay is closed or the object cannot be
                sized.
        """
        key = validate_key(key)
        while True:
            with self._cond:
                if self._closed:
                    raise RelayError("relay closed")
                video = self._videos.get(key)
                if video is not None:
                    return video
                guard = self._loading.get(key)
                if guard is None:
                    guard = self._loading[key] = threading.Event()
                    break
            guard.wait()
        loaded = None
        try:
            loaded = self._load_video(key, allow_head)
        finally:
            with self._cond:
                if loaded is not None and not self._closed:
                    self._videos[key] = loaded
            self._release_guard(key, guard)
        return loaded

    def _load_video(self, key: str, allow_head: bool) -> Optional[_Video]:
        """Build the record for a key from its cache folder, sizing the object when needed.

        Args:
            key: A validated key.
            allow_head: False returns None in place of asking the bucket.

        Returns:
            The record, or None when allow_head is False and the folder has
            no usable ``meta.json``.

        Raises:
            RelayError: When the size request or the ``meta.json`` write
                fails.
        """
        folder = self._chunks_root / cache_id(key)
        meta = self._read_meta(folder)
        if meta is not None and meta[0] == key:
            size = meta[1]
        else:
            if not allow_head:
                return None
            size = self._head(key)
            shutil.rmtree(folder, ignore_errors=True)
            self._write_meta(folder, key, size)
        return _Video(key, folder, size, self._chunk_size, self._scan_chunks(folder, size))

    def _head(self, key: str) -> int:
        """Ask the bucket for an object's length with a HEAD request.

        Args:
            key: A validated key.

        Returns:
            The Content-Length the bucket reports.

        Raises:
            ObjectNotFound: At once for a 404.
            RelayError: At once for another definite refusal, and after
                ``retries`` tries for network errors and 5xx answers.
        """
        path = self._object_path(key)
        url = self._bucket_url + "/" + quote_key(key)
        reason = ""
        for attempt in range(self._retries):
            if attempt:
                self._stop.wait(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1])
            if self._stop.is_set():
                raise RelayError("relay closed")
            connection = self._new_connection()
            try:
                connection.request("HEAD", path)
                response = connection.getresponse()
                response.read()
                status, length = response.status, response.getheader("Content-Length")
            except (OSError, http.client.HTTPException) as error:
                reason = _describe(error)
                continue
            finally:
                connection.close()
            if status == HTTP_NOT_FOUND:
                raise ObjectNotFound(f"size of {key}: the bucket has no such object (HTTP 404 from {url})")
            if status == HTTP_OK:
                if length is None or not length.strip().isdigit():
                    raise RelayError(f"size of {key}: HEAD {url} sent Content-Length {length!r}, not a byte count")
                return int(length.strip())
            reason = f"HTTP {status} {response.reason}"
            if 400 <= status < 500 and status not in (HTTP_REQUEST_TIMEOUT, HTTP_TOO_MANY_REQUESTS):
                raise RelayError(f"size of {key}: HEAD {url} was refused: {reason}")
        raise RelayError(f"size of {key}: HEAD {url} failed after {_tries(self._retries)}: {reason}")

    def _read_meta(self, folder: Path) -> Optional[Tuple[str, int]]:
        """Read a cache folder's ``meta.json``.

        Args:
            folder: The cache folder of one key.

        Returns:
            ``(key, size)`` when the file parses, names a valid key, holds a
            size of zero or more, and was written for this relay's chunk
            size. None in every other case, which marks the folder unusable.
        """
        try:
            document = json.loads((folder / META_NAME).read_text(encoding="utf-8"))
            key, size, chunk_size = document["key"], document["size"], document["chunk_size"]
            validate_key(key)
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if not _is_whole_number(size) or size < 0 or chunk_size != self._chunk_size or isinstance(chunk_size, bool):
            return None
        return (key, size)

    def _write_meta(self, folder: Path, key: str, size: int) -> None:
        """Create a cache folder and write its ``meta.json`` through a temp file.

        Args:
            folder: The cache folder of the key.
            key: The object key.
            size: The object length in bytes.

        Raises:
            RelayError: When the folder or the file cannot be written.
        """
        temp_name = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(prefix=META_NAME + ".", suffix=PART_SUFFIX, dir=str(folder))
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"key": key, "size": size, "chunk_size": self._chunk_size}, stream)
            os.replace(temp_name, folder / META_NAME)
        except OSError as error:
            if temp_name is not None:
                self._remove_quietly(temp_name)
            raise RelayError(f"cache entry for {key}: could not write {folder / META_NAME}: {_describe(error)}") from error

    @staticmethod
    def _remove_quietly(path: Any) -> None:
        """Delete a file the cache no longer counts on.

        A failure is ignored on purpose: the file is already outside the set
        of chunks the relay serves, and a later fetch of the same chunk
        replaces it.

        Args:
            path: The file to delete.
        """
        try:
            os.remove(path)
        except OSError:
            pass

    def _chunk_name(self, index: int) -> str:
        """Return the file name of a chunk, such as ``000012.bin``."""
        return "%0*d%s" % (CHUNK_NAME_DIGITS, index, CHUNK_SUFFIX)

    def _object_path(self, key: str) -> str:
        """Return the request path for a key, percent-encoded."""
        return self._base_path + "/" + quote_key(key)

    def _scan_chunks(self, folder: Path, size: int) -> Set[int]:
        """List the good chunks in a cache folder and delete the bad ones.

        A chunk file is good when its name is the canonical name of an index
        inside the object and its length is exactly what that chunk holds.
        Other chunk files and leftover ``.part`` files are deleted.

        Args:
            folder: The cache folder of one key.
            size: The object length in bytes.

        Returns:
            The indexes of the good chunks.
        """
        have: Set[int] = set()
        count = (size + self._chunk_size - 1) // self._chunk_size
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return have
        for entry in entries:
            match = _CHUNK_NAME.match(entry.name)
            if match is not None:
                index = int(match.group(1))
                expected = min(self._chunk_size, size - index * self._chunk_size)
                try:
                    good = (
                        index < count
                        and entry.name == self._chunk_name(index)
                        and entry.is_file(follow_symlinks=False)
                        and entry.stat(follow_symlinks=False).st_size == expected
                    )
                except OSError:
                    good = False
                if good:
                    have.add(index)
                    continue
            if match is not None or entry.name.endswith(PART_SUFFIX):
                self._remove_quietly(entry.path)
        return have

    def _clean_cache(self) -> None:
        """Clean every video folder at startup.

        A folder whose ``meta.json`` is missing, unreadable, written for
        another chunk size, or filed under the wrong name is removed whole.
        In the other folders, wrong-length chunks and ``.part`` files go.
        """
        for entry in list(os.scandir(self._chunks_root)):
            if not entry.is_dir(follow_symlinks=False):
                continue
            folder = Path(entry.path)
            meta = self._read_meta(folder)
            if meta is None or cache_id(meta[0]) != folder.name:
                shutil.rmtree(folder, ignore_errors=True)
            else:
                self._scan_chunks(folder, meta[1])

    def _touch(self, video: _Video) -> None:
        """Mark a video as just used by updating its ``meta.json`` time.

        Args:
            video: The record of the video.

        Raises:
            RelayError: When the file is gone and cannot be written again.
        """
        try:
            os.utime(video.folder / META_NAME, None)
        except OSError:
            self._write_meta(video.folder, video.key, video.size)

    @staticmethod
    def _measure(folder: Path) -> Tuple[int, float]:
        """Add up a video folder's bytes and find when it was last used.

        Args:
            folder: The cache folder of one key.

        Returns:
            ``(bytes, stamp)``. The stamp is the modification time of
            ``meta.json``, or of the folder while ``meta.json`` is not
            written yet, or now when the folder has just vanished.
        """
        used = 0
        stamp = None
        try:
            for entry in os.scandir(folder):
                if entry.is_file(follow_symlinks=False):
                    status = entry.stat(follow_symlinks=False)
                    used += status.st_size
                    if entry.name == META_NAME:
                        stamp = status.st_mtime
            if stamp is None:
                stamp = folder.stat().st_mtime
        except OSError:
            return (used, time.time())
        return (used, stamp)

    def _claim_folder_locked(self, folder: Path, key: Optional[str]) -> Optional[threading.Event]:
        """Reserve an idle video folder for deletion. The lock must be held.

        Args:
            folder: The cache folder eviction wants to remove.
            key: The key from the folder's ``meta.json``, or None when the
                file is unusable.

        Returns:
            A guard event to pass to _release_guard after the deletion, or
            None when the folder is busy (open reader, queued or running
            chunk, or a load in progress).
        """
        video = next((entry for entry in self._videos.values() if entry.folder == folder), None)
        if video is not None:
            if len(video.readers) or video.pending:
                return None
            return self._retire_locked(video, f"{video.key} was evicted from the cache")
        if key is None:
            if any(cache_id(loading) == folder.name for loading in self._loading):
                return None
            return threading.Event()
        if key in self._loading:
            return None
        guard = self._loading[key] = threading.Event()
        return guard

    def _retire_locked(self, video: _Video, reason: str) -> Optional[threading.Event]:
        """Take a record out of service so its folder can be deleted. The lock must be held.

        Queued chunks of the record fail with the reason, waiting readers
        wake and raise it, and loads of the same key wait on the returned
        guard until the folder is gone.

        Args:
            video: The record to retire.
            reason: The text waiting readers raise.

        Returns:
            The guard event, or None when the record was retired before or
            another thread already guards the key.
        """
        if not video.valid:
            return None
        video.valid = False
        video.retired_reason = reason
        if self._videos.get(video.key) is video:
            del self._videos[video.key]
        for task in list(video.pending.values()):
            if task.state == "queued":
                task.state, task.error = "failed", reason
                del video.pending[task.index]
        self._cond.notify_all()
        if video.key in self._loading:
            return None
        guard = self._loading[video.key] = threading.Event()
        return guard

    def _release_guard(self, key: Optional[str], guard: threading.Event) -> None:
        """Let loads of a key go ahead again.

        Args:
            key: The guarded key, or None for a guard that was never
                registered.
            guard: The event from _video, _claim_folder_locked, or
                _retire_locked.
        """
        with self._cond:
            if key is not None and self._loading.get(key) is guard:
                del self._loading[key]
        guard.set()

    # ----- the work queue -----

    def _want_locked(self, video: _Video, index: int, priority: int, prefetch: bool) -> _Task:
        """Queue a missing chunk, or raise the priority of its queued task. The lock must be held.

        Args:
            video: The record the chunk belongs to.
            index: The chunk index, not on disk.
            priority: The priority the caller needs.
            prefetch: True when a background prefetch asks.

        Returns:
            The chunk's task, new or existing.
        """
        task = video.pending.get(index)
        if task is None:
            task = video.pending[index] = _Task(video, index, priority, prefetch)
        elif task.state == "queued" and priority < task.priority:
            task.priority = priority
        else:
            task.prefetch = task.prefetch or prefetch
            return task
        task.prefetch = task.prefetch or prefetch
        # An upgraded task gets a second heap entry. The worker skips the old
        # one because its priority no longer matches the task.
        heapq.heappush(self._heap, (priority, next(self._sequence), task))
        self._queue_grew = True
        return task

    def _wake_workers_locked(self) -> None:
        """Wake idle workers for new queue entries, most recently idle first. The lock must be held.

        The worker that went idle last has the connection most likely to be
        open still, so it is the first to get new work.
        """
        if self._queue_grew:
            self._queue_grew = False
            for _ in range(min(len(self._idle), len(self._heap))):
                self._worker_conds[self._idle.pop()].notify()

    def _schedule_locked(self, video: _Video, reader: "RangeReader", index: int) -> Optional[_Task]:
        """Queue a reader's current chunk and its read-ahead window. The lock must be held.

        The window runs ``read_ahead`` chunks past the current one and stops
        at the last chunk of the reader's range.

        Args:
            video: The record the reader is open on.
            reader: The reader.
            index: The chunk the reader needs now.

        Returns:
            The task of the current chunk, or None when it is on disk.
        """
        task = None
        if index not in video.have:
            task = self._want_locked(video, index, PRIORITY_DEMAND, prefetch=False)
        for ahead in range(index + 1, min(index + self._read_ahead, reader.last_index) + 1):
            if ahead not in video.have:
                self._want_locked(video, ahead, PRIORITY_READ_AHEAD, prefetch=False)
        self._wake_workers_locked()
        return task

    def _reader_near_locked(self, task: _Task) -> bool:
        """Tell whether an open reader sits within read_ahead chunks behind a task. The lock must be held."""
        return any(
            not reader.closed and reader.position <= task.index <= reader.position + self._read_ahead
            for reader in task.video.readers
        )

    def _pop_runnable_locked(self) -> Optional[_Task]:
        """Take the most urgent task that is still worth running. The lock must be held.

        A read-ahead task with no open reader near it is dropped, or put
        back at prefetch priority when a prefetch also asked for it.

        Returns:
            The task, or None when the queue has nothing to run.
        """
        while self._heap:
            priority, _, task = heapq.heappop(self._heap)
            if task.state != "queued" or task.priority != priority:
                continue
            if priority == PRIORITY_READ_AHEAD and not self._reader_near_locked(task):
                if task.prefetch:
                    task.priority = PRIORITY_PREFETCH
                    heapq.heappush(self._heap, (PRIORITY_PREFETCH, next(self._sequence), task))
                else:
                    task.state = "dropped"
                    del task.video.pending[task.index]
                continue
            return task
        return None

    def _take_task(self, slot: int) -> Optional[_Task]:
        """Block until a task is ready or the relay closes.

        Args:
            slot: The worker's number. While the worker waits, the slot sits
                on the idle stack so _wake_workers_locked can pick it.

        Returns:
            The task, marked running, or None when the relay is closed.
        """
        with self._lock:
            while True:
                if self._closed:
                    return None
                task = self._pop_runnable_locked()
                if task is not None:
                    task.state = "running"
                    return task
                self._idle.append(slot)
                self._worker_conds[slot].wait()
                if slot in self._idle:
                    self._idle.remove(slot)

    def _work(self, slot: int) -> None:
        """Run one worker thread until the relay closes.

        Args:
            slot: The worker's number, which also names its connection.
        """
        while True:
            task = self._take_task(slot)
            if task is None:
                break
            self._run_task(slot, task)
        self._drop_connection(slot)

    def _run_task(self, slot: int, task: _Task) -> None:
        """Fetch and store one chunk, then record the outcome and wake the waiters.

        Args:
            slot: The worker's number.
            task: The running task.
        """
        video, index = task.video, task.index
        error = ""
        changed = False
        try:
            self._store(video, index, self._download(slot, video, index))
        except _ObjectChanged as failure:
            error, changed = str(failure), True
        except _ChunkFailed as failure:
            error = str(failure)
        except Exception as failure:  # noqa: BLE001 - a worker must outlive any surprise
            error = f"chunk {index} of {video.key} failed: {_describe(failure)}"
        guard = None
        orphaned = False
        with self._cond:
            if video.pending.get(index) is task:
                del video.pending[index]
            if error:
                task.state, task.error = "failed", error
                if changed:
                    guard = self._retire_locked(video, error)
            elif video.valid:
                video.have.add(index)
                task.state = "done"
            else:
                task.state, task.error = "failed", video.retired_reason
                orphaned = True
            self._cond.notify_all()
        if orphaned:
            # The record was retired while this chunk was on its way. The
            # folder may already belong to a newer record, so the file goes.
            self._remove_quietly(video.folder / self._chunk_name(index))
        if guard is not None:
            shutil.rmtree(video.folder, ignore_errors=True)
            self._release_guard(video.key, guard)

    def _download(self, slot: int, video: _Video, index: int) -> bytes:
        """Fetch one chunk, trying again after a pause when a try fails.

        Args:
            slot: The worker's number.
            video: The record the chunk belongs to.
            index: The chunk index.

        Returns:
            The chunk's bytes, exactly as long as the chunk.

        Raises:
            _ChunkFailed: After the last try, when the object changed, or
                when the relay closes. The message is ready for the reader.
        """
        reason = ""
        for attempt in range(self._retries):
            if attempt:
                self._stop.wait(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1])
            if self._stop.is_set():
                raise _ChunkFailed("relay closed")
            try:
                return self._request_chunk(slot, video, index)
            except _ChunkFailed:
                self._drop_connection(slot)
                raise
            except (_AttemptFailed, OSError, http.client.HTTPException) as error:
                self._drop_connection(slot)
                reason = _describe(error)
        raise _ChunkFailed(f"chunk {index} of {video.key} failed after {_tries(self._retries)}: {reason}")

    def _request_chunk(self, slot: int, video: _Video, index: int) -> bytes:
        """Send one range request on the worker's connection and check the answer.

        A kept-alive connection that the bucket closed while idle fails
        before any answer arrives. That case gets one immediate second send
        on a new connection and does not count as a try.

        Args:
            slot: The worker's number.
            video: The record the chunk belongs to.
            index: The chunk index.

        Returns:
            The chunk's bytes.

        Raises:
            _AttemptFailed: For a status other than 206, a missing or wrong
                Content-Range, a wrong Content-Length, or a short body.
            _ObjectChanged: When the bucket reports another total size, or
                no longer has the object or the range.
            OSError, http.client.HTTPException: For network errors.
        """
        first = index * self._chunk_size
        last = min(first + self._chunk_size, video.size) - 1
        expected = last - first + 1
        path = self._object_path(video.key)
        headers = {"Range": f"bytes={first}-{last}"}
        connection, reused = self._connection(slot)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
        except (ConnectionError, ssl.SSLError):
            if not reused:
                raise
            self._drop_connection(slot)
            connection, _ = self._connection(slot)
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()

        status = response.status
        if status == HTTP_NOT_FOUND:
            raise _ObjectChanged(f"chunk {index} of {video.key} failed: object changed (the bucket no longer has it, HTTP 404)")
        if status == HTTP_RANGE_NOT_SATISFIABLE:
            raise _ObjectChanged(
                f"chunk {index} of {video.key} failed: object changed "
                f"(the cache holds a {video.size}-byte object, the bucket has fewer than {first + 1} bytes, HTTP 416)"
            )
        if status != HTTP_PARTIAL:
            raise _AttemptFailed(f"HTTP {status} {response.reason}, expected 206")
        content_range = response.getheader("Content-Range")
        match = _CONTENT_RANGE.match(content_range or "")
        if match is None:
            raise _AttemptFailed(f"the Content-Range header is {content_range!r}, expected bytes {first}-{last}/{video.size}")
        got_first, got_last, total = (int(group) for group in match.groups())
        if total != video.size:
            raise _ObjectChanged(
                f"chunk {index} of {video.key} failed: object changed "
                f"(the cache holds a {video.size}-byte object, the bucket now reports {total} bytes)"
            )
        if (got_first, got_last) != (first, last):
            raise _AttemptFailed(f"the Content-Range header covers bytes {got_first}-{got_last}, expected {first}-{last}")
        length = response.getheader("Content-Length")
        if length is not None and length.strip() != str(expected):
            raise _AttemptFailed(f"Content-Length is {length.strip()}, expected {expected} bytes")
        body = response.read() if length is not None else response.read(expected + 1)
        if len(body) != expected:
            raise _AttemptFailed(f"the body has {len(body)} bytes, expected {expected}")
        if length is None:
            # Without a Content-Length the end of this answer is unknown, so
            # the connection cannot carry another request.
            self._drop_connection(slot)
        return body

    def _store(self, video: _Video, index: int, data: bytes) -> None:
        """Write a chunk to a ``.part`` file and rename it into place.

        Args:
            video: The record the chunk belongs to.
            index: The chunk index.
            data: The chunk's bytes.

        Raises:
            _ChunkFailed: When the record was retired meanwhile, or the disk
                write fails (the message carries the path and the reason).
        """
        with self._cond:
            if not video.valid:
                raise _ChunkFailed(video.retired_reason)
        final = video.folder / self._chunk_name(index)
        part = final.with_name(final.name + PART_SUFFIX)
        try:
            video.folder.mkdir(parents=True, exist_ok=True)
            with open(part, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(part, final)
        except OSError as error:
            self._remove_quietly(part)
            raise _ChunkFailed(f"chunk {index} of {video.key} failed: could not write {final}: {_describe(error)}") from error

    # ----- connections -----

    def _new_connection(self) -> http.client.HTTPConnection:
        """Create an unconnected HTTP or HTTPS connection to the bucket, by the URL scheme."""
        if self._scheme == "https":
            return http.client.HTTPSConnection(self._host, self._port, timeout=self._timeout, context=self._ssl_context)
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _connection(self, slot: int) -> Tuple[http.client.HTTPConnection, bool]:
        """Return a worker's persistent connection, creating it when needed.

        Args:
            slot: The worker's number.

        Returns:
            ``(connection, reused)``. reused is True when the connection has
            carried a request before, so the bucket may have closed it.

        Raises:
            _ChunkFailed: When the relay is closed.
            OSError: When a new connection cannot be opened.
        """
        with self._cond:
            connection = self._connections.get(slot)
        if connection is not None:
            if connection.sock is not None:
                return connection, True
            # The bucket answered the last request with "Connection: close".
            self._drop_connection(slot)
        connection = self._new_connection()
        connection.connect()
        # close() must be able to cut this connection for good, so it may
        # never reopen itself behind the relay's back.
        connection.auto_open = 0
        with self._cond:
            if not self._closed:
                self._connections[slot] = connection
                return connection, False
        connection.close()
        raise _ChunkFailed("relay closed")

    def _drop_connection(self, slot: int) -> None:
        """Close a worker's connection so its next request opens a new one.

        Args:
            slot: The worker's number.
        """
        with self._cond:
            connection = self._connections.pop(slot, None)
        if connection is not None:
            connection.close()

    @staticmethod
    def _hang_up(connection: http.client.HTTPConnection) -> None:
        """Cut a connection that another thread may be reading from.

        Shutting the socket down first wakes a worker blocked in a read,
        which a plain close does not do on every platform.

        Args:
            connection: The connection to cut.
        """
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    # ----- services for RangeReader -----

    def _await_chunk(self, reader: "RangeReader", index: int) -> None:
        """Block until a reader's chunk is on disk, keeping its read-ahead queued.

        Args:
            reader: The reader that needs the chunk.
            index: The chunk index.

        Raises:
            RelayError: When the relay or the reader is closed, the cache
                entry is retired, or the chunk fails after its last try.
        """
        video = reader.video
        with self._cond:
            reader.position = index
            task = None
            while True:
                if self._closed:
                    raise RelayError("relay closed")
                if reader.closed:
                    raise RelayError(f"reader on {video.key} is closed")
                if task is not None and task.state == "failed":
                    raise RelayError(task.error)
                if not video.valid:
                    raise RelayError(video.retired_reason)
                task = self._schedule_locked(video, reader, index)
                if task is None:
                    return
                self._cond.wait(READER_WAKE_SECONDS)

    def _read_chunk(self, video: _Video, index: int, offset: int, length: int) -> Optional[bytes]:
        """Read part of a cached chunk from disk.

        Args:
            video: The record the chunk belongs to.
            index: The chunk index.
            offset: The first byte wanted, counted from the chunk's start.
            length: How many bytes are wanted.

        Returns:
            The bytes, or None when the file is gone or short. The chunk is
            then marked missing so the caller's next wait fetches it again.
        """
        try:
            with open(video.folder / self._chunk_name(index), "rb") as stream:
                stream.seek(offset)
                data = stream.read(length)
        except OSError:
            data = b""
        if len(data) == length:
            return data
        with self._cond:
            video.have.discard(index)
        return None

    def _forget_reader(self, reader: "RangeReader") -> None:
        """Close a reader: stop counting it for read-ahead and wake its waiting thread.

        Args:
            reader: The reader to close.
        """
        with self._cond:
            reader.closed = True
            reader.video.readers.discard(reader)
            self._cond.notify_all()


class RangeReader:
    """Yields one inclusive byte range of one object, block by block, in order.

    Use it as a context manager::

        with relay.open_reader(key, start, end) as reader:
            for block in reader:
                wfile.write(block)

    Iterating blocks until each chunk is on disk. Blocks are at most one
    chunk long. Leaving the ``with`` block (or calling close) stops the
    relay from scheduling read-ahead for this reader.

    Attributes:
        key: The object key.
        start: The first byte of the range.
        end: The last byte of the range.
        length: How many bytes the reader yields in total.
        video, position, last_index, closed: Bookkeeping shared with the
            relay. position and closed change only under the relay's lock.
    """

    def __init__(self, relay: ChunkRelay, video: _Video, start: int, end: int) -> None:
        """Create a reader. ChunkRelay.open_reader is the only caller.

        Args:
            relay: The relay that owns the cache.
            video: The record of the object.
            start: The first byte of the range, already checked.
            end: The last byte of the range, already checked.
        """
        self.key = video.key
        self.start = start
        self.end = end
        self.length = end - start + 1
        self.video = video
        # An empty object has no chunks, so its reader has an empty span.
        span = chunk_span(start, end, relay.chunk_size) if self.length else range(0)
        self.position = span.start
        self.last_index = span.stop - 1
        self.closed = False
        self._relay = relay
        self._offset = start

    def __enter__(self) -> "RangeReader":
        """Return the reader for a ``with`` block."""
        return self

    def __exit__(self, *exc_info: Any) -> None:
        """Close the reader when the ``with`` block ends, however it ends."""
        self.close()

    def __iter__(self) -> Iterator[bytes]:
        """Yield the range as bytes blocks, in order.

        A second iteration continues after the last block the first one
        yielded.

        Yields:
            Blocks of one chunk or less. Together they are exactly bytes
            start to end of the object.

        Raises:
            RelayError: When a chunk fails after its last try (``chunk N of
                <key> failed after 3 tries: <reason>``), the object changed
                in the bucket, the relay or this reader is closed, or a
                cached file keeps vanishing.
        """
        chunk_size = self._relay.chunk_size
        vanished = 0
        while self._offset <= self.end:
            index = self._offset // chunk_size
            self._relay._await_chunk(self, index)
            within = self._offset - index * chunk_size
            wanted = min(self.end, (index + 1) * chunk_size - 1) - self._offset + 1
            data = self._relay._read_chunk(self.video, index, within, wanted)
            if data is None:
                vanished += 1
                if vanished > VANISHED_FILE_ATTEMPTS:
                    raise RelayError(
                        f"chunk {index} of {self.key} failed: its cached file vanished {vanished} times in a row"
                    )
                continue
            vanished = 0
            self._offset += len(data)
            yield data

    def close(self) -> None:
        """Stop this reader. Read-ahead for it is no longer scheduled.

        A thread blocked in this reader's iteration wakes and raises
        RelayError. Calling close() twice is harmless.
        """
        self._relay._forget_reader(self)
