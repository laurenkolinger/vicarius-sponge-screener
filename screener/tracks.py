"""Tracks: where a logged sponge sits in the picture as the camera moves.

A track follows one sighting through its video. It is a list of points
``[t, x, y, score]`` sorted by time (t in video seconds, x and y as fractions
of the frame, score from 0 to 1) and, for a sighting drawn as a box, the size
of that box as fractions of the frame. A track reaches TrackStore.save from
inside the server's process, never from a request: no route accepts one.
GET /api/tracks reads tracks back through TrackStore.for_ids, and the export
package lists them in tracks.csv.

A track is one file, ``data/tracks/<ID>.json``:
``{id, key, anchor_time, points, box, updated_at}``. ``key`` is the S3 key of
the video and ``anchor_time`` is the TimestampSeconds of the sighting, both
copied from its row. The file is written under the write lock of the
ObservationStore, which hands this module the lock through ``locked_sighting``
and the trash through ``move_to_trash``. This module never imports the store.

A track belongs to a mark: the ID, the video, the time, and the point of a
sighting. The caller of TrackStore.save passes the four it read from the row
it worked on, and the store confirms under the lock that the row still has
them. A sighting deleted and marked again on the same paused frame gets the
same ID, video, and time but another point, so the result for the old mark is
refused. Marked again at the same point it is the same mark, and the old
result stays right for it.

Bad input raises ValueError with a message that starts with the field name.
Trouble with a track file raises TrackError with the path and the reason.
"""

import contextlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union

from screener.config import MAX_TIME_SECONDS

TRACK_EXTENSION = ".json"
MAX_TRACK_POINTS = 3000
POINT_FIELDS = ("t", "x", "y", "score")
BOX_FIELDS = ("w", "h")
# The rule for a sighting ID is defined here, once, because the store imports this
# module: only an ID the store handed out can name a track file.
ID_PREFIX = "ID"
ID_PATTERN = re.compile(rf"{ID_PREFIX}([0-9]+)")

FRACTION_MIN = 0
FRACTION_MAX = 1
TRACK_FILE_MODE = 0o644
TEMP_SUFFIX = ".tmp"
JSON_SEPARATORS = (",", ":")
WARNING_PREFIX = "tracks: skipped"


class TrackError(Exception):
    """Raised when a track file cannot be read, parsed, checked, or written."""


class TrackAnchor(NamedTuple):
    """What the store confirms about a sighting while it holds the write lock.

    Attributes:
        anchor_time: The TimestampSeconds of the sighting, as a float.
        updated_at: The UTC stamp of this write, such as ``2026-09-21T15:30:00Z``.
    """

    anchor_time: float
    updated_at: str


LockedSighting = Callable[[str, str, float, Tuple[float, float]], ContextManager[TrackAnchor]]
TrashMover = Callable[[Path, datetime], None]


def _number(value: Any, field: str) -> Union[int, float]:
    """Check that a value is a finite real number.

    Args:
        value: The candidate, of any type.
        field: The field name for the error message.

    Returns:
        The value unchanged (an int or a float).

    Raises:
        ValueError: Starting with the field name when the value is a bool,
            is not an int or a float, or is NaN or infinite.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}: expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field}: expected a finite number, got {value}")
    return value


def _seconds(value: Any, field: str) -> float:
    """Check a video time.

    Args:
        value: The candidate, of any type.
        field: The field name for the error message.

    Returns:
        The time as a float. Negative zero comes back as zero.

    Raises:
        ValueError: Starting with the field name when the value is not a
            finite number from 0 to MAX_TIME_SECONDS.
    """
    number = _number(value, field)
    if not 0 <= number <= MAX_TIME_SECONDS:
        raise ValueError(f"{field}: expected 0 to {MAX_TIME_SECONDS} seconds, got {number}")
    return float(number) + 0.0


def _fraction(value: Any, field: str) -> float:
    """Check a fraction of the frame, or a score.

    Args:
        value: The candidate, of any type.
        field: The field name for the error message.

    Returns:
        The value as a float. Negative zero comes back as zero.

    Raises:
        ValueError: Starting with the field name when the value is not a
            finite number from 0 to 1.
    """
    number = _number(value, field)
    if not FRACTION_MIN <= number <= FRACTION_MAX:
        raise ValueError(f"{field}: expected a fraction from {FRACTION_MIN} to {FRACTION_MAX}, got {number}")
    return float(number) + 0.0


def _box_size(value: Any, field: str) -> float:
    """Check the width or height of a box.

    Args:
        value: The candidate, of any type.
        field: The field name for the error message.

    Returns:
        The size as a float.

    Raises:
        ValueError: Starting with the field name when the value is not a
            finite number above 0 and at most 1.
    """
    number = _number(value, field)
    if not FRACTION_MIN < number <= FRACTION_MAX:
        raise ValueError(
            f"{field}: expected a fraction above {FRACTION_MIN} and up to {FRACTION_MAX}, got {number}"
        )
    return float(number)


def _describe(value: Any) -> str:
    """Name what a value is, for an error message.

    Args:
        value: Any value.

    Returns:
        The type name, with the length for a list or tuple (``list of 3``).
    """
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__} of {len(value)}"
    return type(value).__name__


def _checked_id(value: Any, field: str) -> str:
    """Check a sighting ID.

    Args:
        value: The candidate, of any type.
        field: The field name for the error message, such as ``obs_id`` or ``ids[2]``.

    Returns:
        The ID unchanged.

    Raises:
        ValueError: Starting with the field name when the value is not text
            or is not ID_PREFIX followed by digits, so no ID can name a file
            outside the track folder.
    """
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected text, got {type(value).__name__}")
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field}: expected {ID_PREFIX} followed by digits, such as {ID_PREFIX}007, got {value!r}")
    return value


def _checked_point(point: Any) -> Tuple[float, float]:
    """Check the point of the mark a track was computed for.

    Args:
        point: ``(x, y)`` as fractions of the frame, in a list or a tuple.

    Returns:
        ``(x, y)`` as floats.

    Raises:
        ValueError: Starting with ``point``, ``point.x``, or ``point.y`` when
            the value is not a pair or a coordinate is not a finite number
            from 0 to 1.
    """
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ValueError(f"point: expected (x, y), got {_describe(point)}")
    return (_fraction(point[0], "point.x"), _fraction(point[1], "point.y"))


def _checked_points(points: Any) -> List[List[float]]:
    """Check the points of a track.

    Args:
        points: A list of 1 to MAX_TRACK_POINTS lists ``[t, x, y, score]``.

    Returns:
        New lists of four floats each, in the given order.

    Raises:
        ValueError: Starting with ``points`` or ``points[<index>]`` and the
            reason: not a list, too few or too many points, a point that is
            not four numbers, a value that is not a finite number in its
            range, or a time that does not come after the previous one.
    """
    if not isinstance(points, (list, tuple)):
        raise ValueError(f"points: expected a list of [t, x, y, score] lists, got {type(points).__name__}")
    if not 1 <= len(points) <= MAX_TRACK_POINTS:
        raise ValueError(f"points: expected 1 to {MAX_TRACK_POINTS} points, got {len(points)}")
    checked: List[List[float]] = []
    previous: Optional[float] = None
    for index, point in enumerate(points):
        where = f"points[{index}]"
        if not isinstance(point, (list, tuple)) or len(point) != len(POINT_FIELDS):
            raise ValueError(
                f"{where}: expected a list of {len(POINT_FIELDS)} numbers [t, x, y, score], got {_describe(point)}"
            )
        seconds = _seconds(point[0], f"{where}.t")
        if previous is not None and seconds <= previous:
            raise ValueError(f"{where}.t: expected a time after {previous}, got {seconds}")
        checked.append(
            [
                seconds,
                _fraction(point[1], f"{where}.x"),
                _fraction(point[2], f"{where}.y"),
                _fraction(point[3], f"{where}.score"),
            ]
        )
        previous = seconds
    return checked


def _checked_box(box: Any) -> Optional[Dict[str, float]]:
    """Check the box size of a track.

    Args:
        box: None for a sighting marked with a point, or an object with
            ``w`` and ``h``, each a fraction above 0 and at most 1.

    Returns:
        None, or a new ``{"w": float, "h": float}``. Other fields are ignored.

    Raises:
        ValueError: Starting with ``box`` or ``box.<field>`` and the reason.
    """
    if box is None:
        return None
    if not isinstance(box, Mapping):
        raise ValueError(f"box: expected null or an object with w and h, got {type(box).__name__}")
    size: Dict[str, float] = {}
    for field in BOX_FIELDS:
        if field not in box:
            raise ValueError(f"box.{field}: missing")
        size[field] = _box_size(box[field], f"box.{field}")
    return size


def validate_track(track: Any) -> Dict[str, Any]:
    """Check a track before it is written, or after it is read back.

    Args:
        track: An object ``{"points": [[t, x, y, score], ...], "box": null or
            {"w": number, "h": number}}``. Both fields are required. Other
            fields are ignored.

    Returns:
        A new ``{"points": [[float, float, float, float], ...], "box": None
        or {"w": float, "h": float}}``. The input is not changed, and no
        number is a negative zero.

    Raises:
        ValueError: Starting with the field name (``track``, ``points``,
            ``points[<index>].<t, x, y, or score>``, ``box``, or
            ``box.<w or h>``) when the track is not an object, a field is
            missing, there are fewer than 1 or more than MAX_TRACK_POINTS
            points, a point is not four numbers, a value is not a finite
            number (text, bool, null, a list, NaN, infinity), t is outside 0
            to MAX_TIME_SECONDS or does not increase strictly, x, y, or score
            is outside 0 to 1, or a box side is outside the range above 0 to 1.
    """
    if not isinstance(track, Mapping):
        raise ValueError(f"track: expected an object with points and box, got {type(track).__name__}")
    if "points" not in track:
        raise ValueError("points: missing")
    points = _checked_points(track["points"])
    if "box" not in track:
        raise ValueError("box: missing")
    return {"points": points, "box": _checked_box(track["box"])}


def _public(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Pick the fields of a track file that the page and the export use.

    Args:
        record: A checked track file record.

    Returns:
        ``{"points", "box", "anchor_time", "updated_at"}``.
    """
    return {
        "points": record["points"],
        "box": record["box"],
        "anchor_time": record["anchor_time"],
        "updated_at": record["updated_at"],
    }


class TrackStore:
    """The track files of one data folder.

    Attributes:
        tracks_dir: ``data/tracks``, one ``<ID>.json`` file per sighting that
            has a track. The folder is created by the first save.
    """

    def __init__(self, tracks_dir: Path, locked_sighting: LockedSighting, move_to_trash: TrashMover) -> None:
        """Open the track folder of a store.

        Args:
            tracks_dir: The folder for the track files. It need not exist yet.
            locked_sighting: A function ``(obs_id, key, anchor_time, point)``
                that returns a context manager. Entering it takes the
                store's write lock, confirms inside the lock that the row
                with that ID still is the mark the caller worked on (the
                same video, time, and point; it raises UnknownObservation
                when it is not), and yields a TrackAnchor; leaving it
                releases the lock.
            move_to_trash: A function ``(path, moment)`` that moves a file
                into the store's trash folder under a name stamped with the
                moment, and does nothing for a file that is gone.

        Raises:
            ValueError: Starting with the argument name when an argument has
                the wrong type.
        """
        if not isinstance(tracks_dir, (str, os.PathLike)):
            raise ValueError(f"tracks_dir: expected a folder path, got {type(tracks_dir).__name__}")
        if not callable(locked_sighting):
            raise ValueError(f"locked_sighting: expected a function, got {type(locked_sighting).__name__}")
        if not callable(move_to_trash):
            raise ValueError(f"move_to_trash: expected a function, got {type(move_to_trash).__name__}")
        self.tracks_dir = Path(tracks_dir)
        self._locked_sighting = locked_sighting
        self._move_to_trash = move_to_trash

    def path_for(self, obs_id: Any) -> Path:
        """Name the track file of a sighting.

        Args:
            obs_id: The ID of the sighting, such as "ID007".

        Returns:
            ``<tracks_dir>/<ID>.json``. The file need not exist.

        Raises:
            ValueError: Starting with ``obs_id:`` when the ID is not text or
                is not ``ID`` plus digits, so no ID can name a file outside
                the folder.
        """
        return self.tracks_dir / (_checked_id(obs_id, "obs_id") + TRACK_EXTENSION)

    def has(self, obs_id: str) -> bool:
        """Tell whether a sighting has a track file.

        The file is not read, so a damaged file counts. ``load`` and
        ``for_ids`` report the damage.

        Args:
            obs_id: The ID of the sighting, such as "ID007".

        Returns:
            True when ``<tracks_dir>/<ID>.json`` is a file.

        Raises:
            ValueError: Under the path_for rules.
        """
        return self.path_for(obs_id).is_file()

    def save(self, obs_id: str, key: str, anchor_time: float, point: Sequence[float], track: Any) -> Dict[str, Any]:
        """Save the track of one sighting, replacing any earlier track.

        The first four arguments name the mark the track was computed for:
        the ID, the video, the time, and the point of the row the caller
        worked on. The track is checked first (validate_track). The write
        then happens inside the store's write lock, in one step: the JSON
        goes to a temp file in the folder, reaches the disk, and replaces the
        old file through os.replace. A reader sees the old track or the new
        one. Inside the same lock the store confirms that the row with this ID
        still is that mark. A track computed for a sighting that was deleted
        in the meantime, or for a mark that was deleted and replaced by
        another mark at the same ID (another video, time, or point), is
        refused and nothing is written. A mark made again at the same place
        is the same mark, and the track stays right for it.

        Args:
            obs_id: The ID of the sighting, such as "ID007".
            key: The S3 key of the video the track was computed on.
            anchor_time: The video time of the sighting the track was
                computed for, in seconds. It must equal the TimestampSeconds
                of the row to within half a millisecond; the file records the
                row's own value.
            point: ``(x, y)``, the PointX and PointY of the row as floats, in
                a list or a tuple. They must equal the cells of the row to
                within half a unit of the fourth decimal.
            track: ``{"points": [[t, x, y, score], ...], "box": null or {"w", "h"}}``.

        Returns:
            ``{"points", "box", "anchor_time", "updated_at"}`` as stored.

        Raises:
            ValueError: Starting with ``obs_id``, ``key``, ``anchor_time``,
                ``point``, ``point.x``, ``point.y``, or the validate_track
                field names. Nothing is written.
            UnknownObservation: When no sighting has the ID, or the sighting
                that has it is another mark (another video, time, or point).
                It is a StoreError. Nothing is written.
            StoreError: When the write lock cannot be taken or observations.csv
                cannot be parsed. Nothing is written.
            TrackError: With the path and the reason when the file cannot be
                written. The old file stays as it was and the temp file is removed.
        """
        path = self.path_for(obs_id)
        if not isinstance(key, str):
            raise ValueError(f"key: expected the video key as text, got {type(key).__name__}")
        if not key:
            raise ValueError("key: empty")
        seconds = _seconds(anchor_time, "anchor_time")
        mark = _checked_point(point)
        checked = validate_track(track)
        with self._locked_sighting(obs_id, key, seconds, mark) as anchor:
            record = {
                "id": obs_id,
                "key": key,
                "anchor_time": anchor.anchor_time,
                "points": checked["points"],
                "box": checked["box"],
                "updated_at": anchor.updated_at,
            }
            self._write(path, record)
        return _public(record)

    def _write(self, path: Path, record: Mapping[str, Any]) -> None:
        """Write one track file in one step.

        The caller holds the store's write lock.

        Args:
            path: The track file.
            record: The checked record to store as JSON.

        Raises:
            TrackError: With the path and the reason when the folder or the
                file cannot be written. The target stays as it was and the
                temp file is removed.
        """
        temp_name = ""
        try:
            self.tracks_dir.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                dir=str(self.tracks_dir), prefix=f".{path.name}-", suffix=TEMP_SUFFIX
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, separators=JSON_SEPARATORS, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, TRACK_FILE_MODE)
            os.replace(temp_name, path)
        except BaseException as error:
            if temp_name:
                # A temp file that cannot be removed must not hide the error
                # reported below; every reader ignores it.
                with contextlib.suppress(OSError):
                    os.remove(temp_name)
            if isinstance(error, (OSError, UnicodeError)):
                raise TrackError(f"{path}: could not write the track: {error}") from error
            raise

    def load(self, obs_id: str) -> Optional[Dict[str, Any]]:
        """Read and check the track file of one sighting.

        Args:
            obs_id: The ID of the sighting, such as "ID007".

        Returns:
            ``{"points", "box", "anchor_time", "updated_at"}``, or None when
            the sighting has no track file.

        Raises:
            ValueError: Under the path_for rules.
            TrackError: Starting with the path when the file cannot be read,
                is not valid UTF-8 JSON, or holds anything but an object with
                the planned fields (the ID of the sighting, the video key, an
                anchor time, a track that passes validate_track, and a stamp).
        """
        path = self.path_for(obs_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise TrackError(f"{path}: could not read the file: {error}") from error
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as error:
            raise TrackError(f"{path}: not valid JSON text ({error})") from error
        return self._checked_record(path, obs_id, record)

    @staticmethod
    def _checked_record(path: Path, obs_id: str, record: Any) -> Dict[str, Any]:
        """Check a record read from a track file.

        Args:
            path: The file, for the error message.
            obs_id: The sighting the file belongs to.
            record: The parsed JSON.

        Returns:
            The public fields, with the track checked and normalized.

        Raises:
            TrackError: Starting with the path and naming the first field that fails.
        """
        if not isinstance(record, dict):
            raise TrackError(f"{path}: expected a JSON object, got {type(record).__name__}")
        if record.get("id") != obs_id:
            raise TrackError(f"{path}: id is {record.get('id')!r}, expected {obs_id!r}")
        key, stamp = record.get("key"), record.get("updated_at")
        if not isinstance(key, str) or not key:
            raise TrackError(f"{path}: key must be the video key as text")
        if not isinstance(stamp, str) or not stamp:
            raise TrackError(f"{path}: updated_at must be the stamp of the last save as text")
        try:
            anchor_time = _seconds(record.get("anchor_time"), "anchor_time")
            checked = validate_track(record)
        except ValueError as error:
            raise TrackError(f"{path}: {error}") from error
        return {"points": checked["points"], "box": checked["box"], "anchor_time": anchor_time, "updated_at": stamp}

    def for_ids(self, ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Read the tracks of several sightings for the page.

        A sighting without a track file is left out. A file that cannot be
        read or checked is left out too, with one line on stderr that names
        the path and the reason, and the file stays where it is.

        Args:
            ids: The IDs of the sightings as a list or a tuple of text, such
                as ``["ID001", "ID007"]``.

        Returns:
            ``{ID: {"points", "box", "anchor_time", "updated_at"}}`` in the
            order of ids.

        Raises:
            ValueError: Starting with ``ids:`` when ids is not a list or a
                tuple, and with ``ids[<index>]:`` when an item is not a
                sighting ID. Nothing is read.
        """
        if not isinstance(ids, (list, tuple)):
            raise ValueError(f"ids: expected a list of sighting IDs, got {type(ids).__name__}")
        for index, obs_id in enumerate(ids):
            _checked_id(obs_id, f"ids[{index}]")
        tracks: Dict[str, Dict[str, Any]] = {}
        for obs_id in ids:
            try:
                track = self.load(obs_id)
            except TrackError as error:
                sys.stderr.write(f"{WARNING_PREFIX} {error}\n")
                continue
            if track is not None:
                tracks[obs_id] = track
        return tracks

    def trash(self, obs_id: str, moment: datetime) -> None:
        """Move the track file of a sighting into the store's trash folder.

        The caller holds the store's write lock, as ObservationStore.delete
        does. A sighting without a track file is left alone.

        Args:
            obs_id: The ID of the sighting, such as "ID007".
            moment: The moment that stamps the trash name, a datetime in UTC.

        Raises:
            ValueError: Under the path_for rules, and starting with
                ``moment:`` when moment is not a datetime.
            StoreError: With both paths and the reason when the move fails.
        """
        path = self.path_for(obs_id)
        if not isinstance(moment, datetime):
            raise ValueError(f"moment: expected a datetime, got {type(moment).__name__}")
        self._move_to_trash(path, moment)
