"""Watched time per video, and the sighting counts of an earlier package.

A zero in the species count means two different things: the species is absent, or
nobody looked. This module keeps the evidence that separates them. ``WatchStore``
saves which stretches of each video played in ``data/watched.json``, and
``load_reference_counts`` reads an earlier ``observations.csv`` to compare against.
"""

import csv
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from screener.config import MAX_TIME_SECONDS
from screener.species import Species
from screener.store import StoreError

WATCHED_FILE = "watched.json"
REFERENCE_FILE = "reference_observations.csv"
# A gap this short counts as watched: clock readings arrive a moment apart.
GAP_TOLERANCE_SECONDS = 1.0
MAX_RANGES_PER_REPORT = 500
REFERENCE_SPECIES_COLUMN = "Sponge Type"
REFERENCE_FILE_COLUMN = "FileName"

Range = Tuple[float, float]


def merge_ranges(ranges: Sequence[Range]) -> List[Range]:
    """Join ranges that overlap or sit within the gap tolerance.

    Args:
        ranges: ``(start, end)`` pairs in any order.

    Returns:
        The joined pairs, sorted by start.
    """
    merged: List[Range] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + GAP_TOLERANCE_SECONDS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def summarize(duration: Optional[float], ranges: Sequence[Range]) -> Dict[str, Any]:
    """Describe how much of a video was watched.

    Args:
        duration: The video's length in seconds, or None when it was never measured.
        ranges: The watched ``(start, end)`` pairs.

    Returns:
        ``duration``, ``watched_seconds``, ``percent`` (a whole number rounded down
        that reaches 100 only with no gap left, or None with no duration), and
        ``gaps`` (the ``[start, end]`` stretches not watched).
    """
    merged = merge_ranges(ranges)
    if duration is None:
        return {
            "duration": None,
            "watched_seconds": sum(end - start for start, end in merged),
            "percent": None,
            "gaps": [],
        }
    clipped = [(start, min(end, duration)) for start, end in merged if start < duration]
    watched = sum(end - start for start, end in clipped)
    gaps = []
    cursor = 0.0
    for start, end in clipped:
        if start - cursor > GAP_TOLERANCE_SECONDS:
            gaps.append([cursor, start])
        cursor = end
    if duration - cursor > GAP_TOLERANCE_SECONDS:
        gaps.append([cursor, duration])
    if not clipped:
        gaps = [[0.0, duration]]
    percent = 100 if not gaps else min(99, math.floor(watched / duration * 100))
    return {"duration": duration, "watched_seconds": watched, "percent": percent, "gaps": gaps}


def _is_time(value: Any) -> bool:
    """Return whether a value is a finite number from zero to the longest allowed time."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0 <= value <= MAX_TIME_SECONDS


def validate_ranges(value: Any) -> List[Range]:
    """Check the ranges of a watched report.

    Args:
        value: The parsed JSON value.

    Returns:
        The ranges as ``(start, end)`` floats.

    Raises:
        ValueError: Starting with ``ranges``, for anything but a list of at most
            MAX_RANGES_PER_REPORT ``[start, end]`` pairs of times with start <= end.
    """
    if not isinstance(value, list):
        raise ValueError(f"ranges: expected a list, got {type(value).__name__}")
    if len(value) > MAX_RANGES_PER_REPORT:
        raise ValueError(f"ranges: expected at most {MAX_RANGES_PER_REPORT} ranges, got {len(value)}")
    ranges = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not all(_is_time(part) for part in item):
            raise ValueError(f"ranges: expected [start, end] in seconds, got {item!r}")
        if item[0] > item[1]:
            raise ValueError(f"ranges: the start comes after the end in {item!r}")
        ranges.append((float(item[0]), float(item[1])))
    return ranges


def validate_duration(value: Any) -> Optional[float]:
    """Check the duration of a watched report.

    Args:
        value: The parsed JSON value. None means the video was not measured yet.

    Returns:
        The duration in seconds, or None.

    Raises:
        ValueError: Starting with ``duration``, for anything but None or a time above zero.
    """
    if value is None:
        return None
    if not _is_time(value) or value <= 0:
        raise ValueError(f"duration: expected a length in seconds, got {value!r}")
    return float(value)


class WatchStore:
    """The watched stretches of every video, saved in one JSON file."""

    def __init__(self, path: Path) -> None:
        """Remember where the file lives. The file is read on first use.

        Args:
            path: The path of watched.json.
        """
        self._path = Path(path)
        self._lock = threading.Lock()
        self._videos: Optional[Dict[str, Dict[str, Any]]] = None

    def _load(self) -> Dict[str, Dict[str, Any]]:
        """Return the saved videos, reading the file on first use.

        Raises:
            StoreError: When the file cannot be read or holds anything unexpected.
                The file stays as it is.
        """
        if self._videos is not None:
            return self._videos
        try:
            parsed = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._videos = {}
            return self._videos
        except (OSError, ValueError) as error:
            raise StoreError(f"{self._path}: watched.json cannot be read: {error}") from error
        try:
            if not isinstance(parsed, dict):
                raise ValueError("expected an object")
            videos = {}
            for key, entry in parsed.items():
                videos[key] = {
                    "duration": validate_duration(entry["duration"]),
                    "ranges": validate_ranges(entry["ranges"]),
                }
        except (KeyError, TypeError, ValueError) as error:
            raise StoreError(f"{self._path}: watched.json holds bad data: {error}") from error
        self._videos = videos
        return self._videos

    def _save(self) -> None:
        """Write the file through a temporary file, so a reader always sees a whole one.

        Raises:
            StoreError: When the file cannot be written.
        """
        payload = {
            key: {"duration": entry["duration"], "ranges": [list(item) for item in entry["ranges"]]}
            for key, entry in (self._videos or {}).items()
        }
        temp_name = None
        try:
            handle, temp_name = tempfile.mkstemp(prefix=f".{self._path.name}.", dir=self._path.parent)
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(payload, out, indent=1)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_name, self._path)
        except OSError as error:
            if temp_name is not None and os.path.exists(temp_name):
                os.unlink(temp_name)
            raise StoreError(f"{self._path}: watched.json cannot be written: {error}") from error

    def report(self, key: str, ranges: Sequence[Range], duration: Optional[float]) -> Dict[str, Any]:
        """Add watched stretches to a video, and return its summary.

        A report with no ranges and no duration only reads. Reports add up, so
        sending one twice changes nothing.

        Args:
            key: The video's key.
            ranges: Validated watched ``(start, end)`` pairs.
            duration: The validated length in seconds, or None to keep the known one.

        Returns:
            The summary from ``summarize``.

        Raises:
            StoreError: When the file cannot be read or written.
        """
        with self._lock:
            videos = self._load()
            entry = videos.get(key, {"duration": None, "ranges": []})
            if not ranges and duration is None:
                return summarize(entry["duration"], entry["ranges"])
            # The longest length wins: a shorter copy of a video must not hide a gap.
            lengths = [length for length in (entry["duration"], duration) if length is not None]
            updated = {
                "duration": max(lengths) if lengths else None,
                "ranges": merge_ranges([*entry["ranges"], *ranges]),
            }
            if updated != entry:
                had_entry = key in videos
                videos[key] = updated
                try:
                    self._save()
                except StoreError:
                    if had_entry:
                        videos[key] = entry
                    else:
                        del videos[key]
                    raise
            return summarize(updated["duration"], updated["ranges"])

    def summary(self, key: str) -> Dict[str, Any]:
        """Return the summary of one video.

        Args:
            key: The video's key.

        Returns:
            The summary from ``summarize``.

        Raises:
            StoreError: When the file cannot be read.
        """
        return self.report(key, [], None)


def load_reference_counts(path: Path, species: Sequence[Species]) -> Optional[Dict[str, Dict[str, int]]]:
    """Count the sightings per video and species in an earlier observations.csv.

    Args:
        path: The path of the earlier file.
        species: The species list, which turns the scientific names into codes.

    Returns:
        ``{lower-case file name: {code: count}}``, or None when the file is missing.
        A name outside the species list is skipped.

    Raises:
        StoreError: When the file cannot be read or lacks a needed column.
    """
    codes = {item.name.casefold(): item.code for item in species}
    counts: Dict[str, Dict[str, int]] = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for column in (REFERENCE_SPECIES_COLUMN, REFERENCE_FILE_COLUMN):
                if column not in (reader.fieldnames or []):
                    raise StoreError(f"{path}: the reference file has no {column} column")
            for row in reader:
                code = codes.get((row[REFERENCE_SPECIES_COLUMN] or "").strip().casefold())
                name = (row[REFERENCE_FILE_COLUMN] or "").strip().casefold()
                if code and name:
                    video = counts.setdefault(name, {})
                    video[code] = video.get(code, 0) + 1
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise StoreError(f"{path}: the reference file cannot be read: {error}") from error
    return counts
