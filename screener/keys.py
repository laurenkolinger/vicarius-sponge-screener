"""Validation and encoding of S3 object keys.

Every module that touches the bucket or the disk cache passes keys through
here first, so a hostile key (path tricks, control characters, a key outside
the TCRMP folder) stops at one gate.
"""

import hashlib
import urllib.parse
from typing import Any

from screener.config import KEY_PREFIX, MAX_KEY_LENGTH

CACHE_ID_LENGTH = 16
ASCII_DELETE = 127
FIRST_PRINTABLE = 32


class InvalidKey(ValueError):
    """Raised when a key or prefix breaks the rules in this module."""


def _check_path(value: Any, label: str) -> str:
    """Apply the rules shared by keys and prefixes.

    Args:
        value: The candidate key or prefix, of any type.
        label: "key" or "prefix", used in the error message.

    Returns:
        The value unchanged when it passes.

    Raises:
        InvalidKey: With the label and the rule that failed.
    """
    if not isinstance(value, str):
        raise InvalidKey(f"{label}: expected text, got {type(value).__name__}")
    if not value:
        raise InvalidKey(f"{label}: empty")
    if len(value) > MAX_KEY_LENGTH:
        raise InvalidKey(f"{label}: longer than {MAX_KEY_LENGTH} characters")
    if "\\" in value:
        raise InvalidKey(f"{label}: contains a backslash")
    if any(ord(char) < FIRST_PRINTABLE or ord(char) == ASCII_DELETE for char in value):
        raise InvalidKey(f"{label}: contains a control character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        # A lone surrogate passes every check above and then breaks
        # quote_key and cache_id, so it stops here.
        raise InvalidKey(f"{label}: cannot be encoded as UTF-8 ({error.reason})") from error
    if not value.startswith(KEY_PREFIX):
        raise InvalidKey(f"{label}: must start with {KEY_PREFIX}")
    segments = value.split("/")
    if ".." in segments:
        raise InvalidKey(f"{label}: contains a '..' segment")
    # A trailing slash leaves one empty segment at the end. Any other empty
    # segment means a doubled slash.
    if "" in segments[:-1]:
        raise InvalidKey(f"{label}: contains an empty path segment")
    return value


def validate_key(key: Any) -> str:
    """Check that a value is a safe object key inside the TCRMP folder.

    Args:
        key: The candidate key, of any type.

    Returns:
        The key unchanged.

    Raises:
        InvalidKey: When the key is not text, is empty or too long, sits
            outside ``TCRMP_video_ondeck/``, names a folder, or contains
            ``..``, a backslash, a control character, or an empty segment.
    """
    checked = _check_path(key, "key")
    if checked.endswith("/"):
        raise InvalidKey("key: names a folder, not a file")
    return checked


def validate_prefix(prefix: Any) -> str:
    """Check that a value is a safe folder prefix inside the TCRMP folder.

    Args:
        prefix: The candidate prefix, of any type.

    Returns:
        The prefix unchanged.

    Raises:
        InvalidKey: Under the same rules as validate_key, and when the prefix
            does not end with ``/``.
    """
    checked = _check_path(prefix, "prefix")
    if not checked.endswith("/"):
        raise InvalidKey("prefix: must end with /")
    return checked


def quote_key(key: str) -> str:
    """Percent-encode a key for use in a URL path.

    Args:
        key: A validated key.

    Returns:
        The key with everything outside unreserved characters and ``/``
        encoded. A plus sign becomes ``%2B`` because S3 reads a bare plus in a
        path as a space.
    """
    return urllib.parse.quote(key, safe="/")


def cache_id(key: str) -> str:
    """Return the folder name the disk cache uses for a key.

    Args:
        key: A validated key.

    Returns:
        The first 16 hexadecimal characters of the key's SHA-1 digest.
    """
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:CACHE_ID_LENGTH]


def extension_of(key: str) -> str:
    """Return the file extension of a key.

    Args:
        key: A key or file name.

    Returns:
        The text after the last dot of the basename, in lower case, or an
        empty string when the basename has no dot.
    """
    name = key.rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()
