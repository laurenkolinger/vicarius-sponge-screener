"""Folder and video listings of the public TCRMP bucket.

S3Catalog asks the bucket's public ListObjectsV2 endpoint for one folder at a
time, follows continuation tokens to the last page, keeps the video files, and
saves every listing to one JSON file so the next start shows folders at once
and a lost network still shows the last known listing.
"""

import http.client
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from screener.config import CATALOG_MAX_AGE_SECONDS, PLAYABLE_EXTENSIONS, VIDEO_EXTENSIONS
from screener.keys import InvalidKey, extension_of, validate_key, validate_prefix

S3_NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
DELIMITER = "/"
CACHE_FORMAT_VERSION = 1
# One page holds up to 1,000 entries, so this allows a million entries in one
# folder, far above the archive's 4,330 videos. The cap stops a bucket that
# keeps answering "truncated" from looping forever.
MAX_LISTING_PAGES = 1000
# A full page of 1,000 long keys is under 1 MiB. The cap bounds memory when
# something other than S3 answers.
MAX_PAGE_BYTES = 16 * 1024 * 1024

_DIGIT_RUNS = re.compile(r"([0-9]+)")


class CatalogError(Exception):
    """Raised when a folder cannot be listed and no saved listing exists."""


@dataclass(frozen=True)
class CatalogEntry:
    """One video file in the bucket.

    Attributes:
        key: The full object key.
        name: The file name (the key's last path segment).
        size: The object length in bytes, always above zero.
        ext: The lowercase extension without a dot.
        playable: True when Chrome plays this format without conversion.
    """

    key: str
    name: str
    size: int
    ext: str
    playable: bool


@dataclass(frozen=True)
class CatalogPage:
    """The contents of one bucket folder.

    Attributes:
        prefix: The folder that was listed, ending with ``/``.
        folders: Full prefixes of the subfolders, each ending with ``/``, in
            natural order.
        videos: The video files directly inside the folder, in natural order
            of their names.
        stale: True when the bucket could not be reached and this is the last
            saved listing.
    """

    prefix: str
    folders: List[str]
    videos: List[CatalogEntry]
    stale: bool


@dataclass
class _SavedPage:
    """A listing held in memory and in the JSON file.

    Attributes:
        fetched_at: The clock reading when the bucket answered.
        folders: Subfolder prefixes in natural order.
        videos: Video entries in natural order.
    """

    fetched_at: float
    folders: List[str]
    videos: List[CatalogEntry]


def natural_key(text: str) -> Tuple[Any, ...]:
    """Return a sort key that orders digit runs by value and ignores case.

    Args:
        text: A file name or folder prefix.

    Returns:
        A tuple usable as a ``sorted`` key. ``T2`` sorts before ``T10``, and
        the original text breaks ties so the order is always the same.

    Raises:
        TypeError: When text is not a string.
    """
    if not isinstance(text, str):
        raise TypeError(f"natural_key: expected text, got {type(text).__name__}")
    parts = []
    for part in _DIGIT_RUNS.split(text):
        if part == "":
            continue
        if part[0] in "0123456789":
            parts.append((0, int(part), ""))
        else:
            parts.append((1, 0, part.lower()))
    return (tuple(parts), text)


def _video_sort_key(entry: "CatalogEntry") -> Tuple[Any, ...]:
    """Return the natural sort key of a video, comparing the stem before the extension.

    Sorting on the stem puts ``x_T1.mp4`` ahead of ``x_T1-6.mp4``; a sort on
    the whole name would compare ``.`` with ``-`` and reverse them.

    Args:
        entry: The catalog entry to place.

    Returns:
        A tuple usable as a ``sorted`` key.
    """
    stem = entry.name[: -(len(entry.ext) + 1)] if entry.ext else entry.name
    return (natural_key(stem), natural_key(entry.ext), entry.name)


def _folder_sort_key(prefix: str) -> Tuple[Any, ...]:
    """Return the natural sort key of a folder prefix, ignoring its final slash.

    Args:
        prefix: A folder prefix that ends with ``/``.

    Returns:
        A tuple usable as a ``sorted`` key, so ``Set1/`` sorts ahead of
        ``Set1-old/``.
    """
    return natural_key(prefix.rstrip(DELIMITER))


def _entry_for(key: str, size: int) -> CatalogEntry:
    """Build the catalog entry for a video key.

    Args:
        key: A validated key with a video extension.
        size: The object length in bytes.

    Returns:
        The entry, with the name, extension, and playable flag filled in.
    """
    ext = extension_of(key)
    return CatalogEntry(key=key, name=key.rsplit("/", 1)[-1], size=size, ext=ext, playable=ext in PLAYABLE_EXTENSIONS)


def _is_direct_child(path: str, prefix: str, folder: bool) -> bool:
    """Tell whether a key or subfolder sits directly inside a folder.

    Args:
        path: A validated key or prefix.
        prefix: The folder being listed.
        folder: True when path is a subfolder prefix (which ends with ``/``).

    Returns:
        True when path starts with prefix and adds exactly one path segment.
    """
    if not path.startswith(prefix):
        return False
    rest = path[len(prefix):]
    if folder:
        rest = rest[:-1]
    return rest != "" and DELIMITER not in rest


def _warn(message: str) -> None:
    """Write one line about a cache-file problem to stderr.

    Args:
        message: What failed, where, and why.
    """
    sys.stderr.write(f"catalog: {message}\n")


class S3Catalog:
    """Lists bucket folders, with a saved copy for fast starts and lost networks.

    One object serves every server thread. A lock guards the pages held in
    memory and is never held during a network request or a disk write.
    """

    def __init__(
        self,
        bucket_url: str,
        cache_path: Optional[Path] = None,
        timeout: float = 30.0,
        max_age_seconds: int = CATALOG_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Set up the catalog and load the saved listings when a file exists.

        Args:
            bucket_url: The bucket's base URL, ``http://`` or ``https://``,
                such as ``https://uviai.s3.us-west-2.amazonaws.com``.
            cache_path: The JSON file that keeps listings between runs. None
                keeps listings in memory only. A corrupt file, or a file
                saved for another bucket URL, is ignored and later replaced.
            timeout: Seconds to wait for the bucket on each request.
            max_age_seconds: A saved listing younger than this is served
                without asking the bucket.
            clock: Returns the current time in seconds. Tests pass a fake.

        Raises:
            ValueError: When an argument has the wrong type or range. The
                message names the argument.
        """
        if not isinstance(bucket_url, str):
            raise ValueError(f"bucket_url: expected text, got {type(bucket_url).__name__}")
        parts = urllib.parse.urlsplit(bucket_url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
            raise ValueError(f"bucket_url: expected an http or https URL with a host and no query, got {bucket_url!r}")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"timeout: expected a number of seconds above zero, got {timeout!r}")
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds)
            or max_age_seconds < 0
        ):
            raise ValueError(f"max_age_seconds: expected a number of seconds of zero or more, got {max_age_seconds!r}")
        if not callable(clock):
            raise ValueError(f"clock: expected a callable that returns seconds, got {type(clock).__name__}")
        if cache_path is not None and not isinstance(cache_path, (str, os.PathLike)):
            raise ValueError(f"cache_path: expected a path or None, got {type(cache_path).__name__}")

        self._bucket_url = bucket_url.rstrip("/")
        self._cache_path = None if cache_path is None else Path(cache_path)
        self._timeout = float(timeout)
        self._max_age = float(max_age_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._pages: Dict[str, _SavedPage] = self._load()

    def list(self, prefix: str, refresh: bool = False) -> CatalogPage:
        """Return the folders and videos directly inside one bucket folder.

        Args:
            prefix: The folder to list. It must start with
                ``TCRMP_video_ondeck/`` and end with ``/``.
            refresh: True asks the bucket even when a young saved listing
                exists.

        Returns:
            The listing. ``stale`` is True when the bucket could not be
            reached, or sent an answer that does not parse, and the last
            saved listing is returned in its place.

        Raises:
            InvalidKey: When the prefix breaks the key rules. No request is
                sent.
            ValueError: When refresh is not a bool.
            CatalogError: When the bucket cannot be listed and no saved
                listing exists. The message carries the URL and the reason.
        """
        prefix = validate_prefix(prefix)
        if not isinstance(refresh, bool):
            raise ValueError(f"refresh: expected True or False, got {refresh!r}")

        with self._lock:
            saved = self._pages.get(prefix)
        if saved is not None and not refresh:
            age = self._clock() - saved.fetched_at
            if 0 <= age < self._max_age:
                return self._as_page(prefix, saved, stale=False)

        try:
            folders, videos = self._fetch(prefix)
        except CatalogError:
            if saved is None:
                raise
            return self._as_page(prefix, saved, stale=True)

        fresh = _SavedPage(fetched_at=float(self._clock()), folders=folders, videos=videos)
        with self._lock:
            self._pages[prefix] = fresh
        self._save()
        return self._as_page(prefix, fresh, stale=False)

    @staticmethod
    def _as_page(prefix: str, saved: _SavedPage, stale: bool) -> CatalogPage:
        """Copy a saved listing into the page handed to callers.

        Args:
            prefix: The folder that was listed.
            saved: The listing held in memory.
            stale: The flag to set on the page.

        Returns:
            A page with its own lists, so a caller that edits them leaves the
            saved listing alone.
        """
        return CatalogPage(prefix=prefix, folders=[*saved.folders], videos=[*saved.videos], stale=stale)

    def _page_url(self, prefix: str, token: Optional[str]) -> str:
        """Build the ListObjectsV2 URL for one page.

        Args:
            prefix: The validated folder prefix.
            token: The continuation token from the previous page, or None for
                the first page.

        Returns:
            The URL, with every parameter percent-encoded (a space becomes
            ``%20`` and a plus sign ``%2B``, because S3 reads ``+`` in a
            query as a space).
        """
        params = [("list-type", "2"), ("prefix", prefix), ("delimiter", DELIMITER)]
        if token is not None:
            params.append(("continuation-token", token))
        return self._bucket_url + "/?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="/")

    def _read_xml(self, url: str) -> ET.Element:
        """Fetch one listing page and parse it.

        Args:
            url: The page URL from _page_url.

        Returns:
            The ``ListBucketResult`` root element.

        Raises:
            CatalogError: On a network error, an HTTP error status, an
                oversized answer, XML that does not parse, or a root element
                other than ``ListBucketResult``.
        """
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as response:
                body = response.read(MAX_PAGE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise CatalogError(f"listing {url} failed: the bucket answered HTTP {error.code} {error.reason}") from error
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            reason = getattr(error, "reason", None) or error
            raise CatalogError(f"listing {url} failed: {reason}") from error
        if len(body) > MAX_PAGE_BYTES:
            raise CatalogError(f"listing {url} failed: the answer is larger than {MAX_PAGE_BYTES} bytes")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as error:
            raise CatalogError(f"listing {url} failed: the answer is not valid XML ({error})") from error
        expected = "{%s}ListBucketResult" % S3_NAMESPACE
        if root.tag != expected:
            raise CatalogError(f"listing {url} failed: expected a ListBucketResult document, got <{root.tag}>")
        return root

    def _fetch(self, prefix: str) -> Tuple[List[str], List[CatalogEntry]]:
        """List one folder from the bucket, following every continuation token.

        Args:
            prefix: The validated folder prefix.

        Returns:
            ``(folders, videos)``, both in natural order. Keys without a
            video extension, empty objects, keys that break the key rules,
            and anything outside the folder are left out.

        Raises:
            CatalogError: When any page fails, a truncated page carries no
                new token, a size is not a number, or the listing runs past
                MAX_LISTING_PAGES pages. A partial listing is never returned.
        """
        tag = "{%s}" % S3_NAMESPACE
        folders: Dict[str, None] = {}
        sizes: Dict[str, int] = {}
        token: Optional[str] = None
        seen_tokens = set()
        for _ in range(MAX_LISTING_PAGES):
            url = self._page_url(prefix, token)
            root = self._read_xml(url)
            for item in root.findall(tag + "Contents"):
                key = item.findtext(tag + "Key")
                size_text = item.findtext(tag + "Size")
                if key is None:
                    raise CatalogError(f"listing {url} failed: a Contents element has no Key")
                try:
                    size = int((size_text or "").strip())
                except ValueError:
                    raise CatalogError(f"listing {url} failed: Size of {key!r} is {size_text!r}, not a number") from None
                if size <= 0 or extension_of(key) not in VIDEO_EXTENSIONS:
                    continue
                try:
                    validate_key(key)
                except InvalidKey:
                    continue
                if _is_direct_child(key, prefix, folder=False):
                    sizes[key] = size
            for item in root.findall(tag + "CommonPrefixes"):
                folder = item.findtext(tag + "Prefix")
                if folder is None:
                    raise CatalogError(f"listing {url} failed: a CommonPrefixes element has no Prefix")
                try:
                    validate_prefix(folder)
                except InvalidKey:
                    continue
                if _is_direct_child(folder, prefix, folder=True):
                    folders[folder] = None
            if (root.findtext(tag + "IsTruncated") or "").strip().lower() != "true":
                videos = [_entry_for(key, size) for key, size in sizes.items()]
                videos.sort(key=_video_sort_key)
                return sorted(folders, key=_folder_sort_key), videos
            token = root.findtext(tag + "NextContinuationToken")
            if not token:
                raise CatalogError(f"listing {url} failed: the page is truncated but has no NextContinuationToken")
            if token in seen_tokens:
                raise CatalogError(f"listing {url} failed: the bucket sent the same continuation token twice")
            seen_tokens.add(token)
        raise CatalogError(f"listing {prefix} from {self._bucket_url} failed: more than {MAX_LISTING_PAGES} pages")

    def _load(self) -> Dict[str, _SavedPage]:
        """Read the saved listings from the JSON file.

        Returns:
            The pages by prefix. Empty when there is no cache path, no file,
            a file saved for another bucket URL, or a file that fails any
            check (which is reported on stderr and later replaced).
        """
        if self._cache_path is None:
            return {}
        try:
            text = self._cache_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as error:
            _warn(f"ignoring {self._cache_path}: could not read it ({error})")
            return {}
        try:
            document = json.loads(text)
            if not isinstance(document, dict) or document.get("version") != CACHE_FORMAT_VERSION:
                raise ValueError("not a version %d catalog file" % CACHE_FORMAT_VERSION)
            if document.get("bucket_url") != self._bucket_url:
                return {}
            pages = document.get("pages")
            if not isinstance(pages, dict):
                raise ValueError("'pages' is not an object")
            return {prefix: self._parse_saved(prefix, record) for prefix, record in pages.items()}
        except (ValueError, TypeError, KeyError) as error:
            _warn(f"ignoring {self._cache_path}: {error}")
            return {}

    @staticmethod
    def _parse_saved(prefix: Any, record: Any) -> _SavedPage:
        """Check one saved listing and rebuild its entries.

        Args:
            prefix: The page's prefix from the file.
            record: The page's JSON object.

        Returns:
            The listing, with names, extensions, and playable flags derived
            again from the keys.

        Raises:
            ValueError: When the prefix, the fetch time, a folder, a key, or
                a size fails a check. InvalidKey is a ValueError.
            TypeError, KeyError: When the record has the wrong shape.
        """
        validate_prefix(prefix)
        fetched_at = record["fetched_at"]
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)) or not math.isfinite(fetched_at):
            raise ValueError(f"fetched_at of {prefix} is {fetched_at!r}, not a time")
        folders = []
        for folder in record["folders"]:
            validate_prefix(folder)
            if not _is_direct_child(folder, prefix, folder=True):
                raise ValueError(f"folder {folder!r} is outside {prefix}")
            folders.append(folder)
        videos = []
        for video in record["videos"]:
            key, size = validate_key(video["key"]), video["size"]
            if not _is_direct_child(key, prefix, folder=False) or extension_of(key) not in VIDEO_EXTENSIONS:
                raise ValueError(f"video {key!r} does not belong in {prefix}")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ValueError(f"size of {key!r} is {size!r}, not a byte count above zero")
            videos.append(_entry_for(key, size))
        folders.sort(key=_folder_sort_key)
        videos.sort(key=_video_sort_key)
        return _SavedPage(fetched_at=float(fetched_at), folders=folders, videos=videos)

    def _save(self) -> None:
        """Write every listing held in memory to the JSON file.

        The write goes to a temp file in the same folder and is moved into
        place, so a reader never sees half a file. A failure is reported on
        stderr and the listing still reaches the caller, because a folder the
        bucket just listed should show even when the disk copy cannot be
        kept.
        """
        if self._cache_path is None:
            return
        # The save lock orders writers. The snapshot is taken inside it, so
        # the last file written always holds the newest pages.
        with self._save_lock:
            with self._lock:
                pages = {
                    prefix: {
                        "fetched_at": saved.fetched_at,
                        "folders": [*saved.folders],
                        "videos": [{"key": video.key, "size": video.size} for video in saved.videos],
                    }
                    for prefix, saved in self._pages.items()
                }
            document = {"version": CACHE_FORMAT_VERSION, "bucket_url": self._bucket_url, "pages": pages}
            temp_name = None
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                handle, temp_name = tempfile.mkstemp(
                    prefix=self._cache_path.name + ".", suffix=".tmp", dir=str(self._cache_path.parent)
                )
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(document, stream, indent=1, sort_keys=True)
                os.replace(temp_name, self._cache_path)
            except OSError as error:
                _warn(f"could not save {self._cache_path}: {error}")
                if temp_name is not None:
                    try:
                        os.remove(temp_name)
                    except OSError:
                        pass
