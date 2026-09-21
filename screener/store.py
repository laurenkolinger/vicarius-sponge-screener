"""Sightings on disk: observations.csv, videos_screened.csv, and a frame and a crop per sighting.

Every write runs under an exclusive lock on ``data/.lock``. Inside the lock the
store rereads the CSV, changes it in memory, writes a temp file in the same
folder, and swaps it in with os.replace. A reader therefore always sees a whole
file, and two tabs or two processes never hand out the same ID. The store never
rewrites a CSV it cannot parse.

Bad input raises ValueError with a message that starts with the field name.
Trouble with the files raises StoreError with the path and the reason.
"""

import csv
import fcntl
import math
import os
import re
import tempfile
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from screener.config import MAX_IMAGE_BYTES, MAX_NOTE_LENGTH, MAX_TIME_SECONDS
from screener.keys import InvalidKey, validate_key
from screener.names import FILE_PART_SEPARATOR, VideoName, file_safe, parse_video_name
from screener.positions import QUADRANT_PHRASES, anchor_point, format_clock, quadrant_of, validate_box, validate_point
from screener.settings import validate_annotator
from screener.species import Species, species_by_code

OBSERVATION_COLUMNS = [
    "Site",
    "Transect",
    "Sponge Type",
    "Timestamp",
    "Notes",
    "ID",
    "FileName",
    "FrameFileName",
    "AbbreviatedNote",
    "SpeciesCode",
    "TimestampSeconds",
    "Quadrant",
    "PointX",
    "PointY",
    "BoxX",
    "BoxY",
    "BoxW",
    "BoxH",
    "CropFileName",
    "S3Key",
    "Annotator",
    "LoggedAt",
]
SCREENED_COLUMNS = [
    "S3Key",
    "FileName",
    "Status",
    "TargetSpecies",
    "Sightings",
    "Annotator",
    "FirstOpened",
    "MarkedDone",
]

OBSERVATIONS_FILE = "observations.csv"
SCREENED_FILE = "videos_screened.csv"
FRAMES_FOLDER = "frames"
CROPS_FOLDER = "crops"
TRASH_FOLDER = "trash"
LOCK_FILE = ".lock"

STATUS_IN_PROGRESS = "in progress"
STATUS_DONE = "done"
TARGET_SEPARATOR = ";"
NOTE_SEPARATOR = ", "

ID_PREFIX = "ID"
ID_MIN_DIGITS = 3
ID_PATTERN = re.compile(r"ID([0-9]+)")
SAFE_FILE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
MAX_FILE_PART_LENGTH = 80
FRAME_EXTENSION = ".jpg"
CROP_EXTENSION = ".png"
JPEG_SIGNATURE = b"\xff\xd8\xff"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
TRASH_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
SECONDS_DECIMALS = 3
FRACTION_DECIMALS = 4
CSV_LINE_END = "\n"
CSV_FILE_MODE = 0o644
LOCK_FILE_MODE = 0o644
# A healthy writer holds the lock for a few milliseconds. A writer that still
# holds it after this long is stuck, and the waiting write fails with a message
# instead of hanging the page.
LOCK_WAIT_SECONDS = 30.0
LOCK_POLL_SECONDS = 0.005
TEMP_SUFFIX = ".tmp"
REPLACED_BY_SPACE = ("Cc", "Cs")
LEFT_ALONE = "The store leaves the file as it is"


class StoreError(Exception):
    """Raised when the files of the store cannot be read, parsed, or written."""


class UnknownObservation(StoreError):
    """Raised by delete when no sighting has the given ID, so a caller can answer "not found"."""


def utc_now() -> datetime:
    """Read the system clock. This is the store's default clock.

    Returns:
        The current moment as a datetime in UTC.
    """
    return datetime.now(timezone.utc)


def clean_note(note: Any) -> str:
    """Make an annotator's note safe for one CSV cell on one line.

    Args:
        note: The note as typed.

    Returns:
        The note with control characters, line breaks, and every other kind
        of white space turned into spaces, runs of spaces collapsed, and the
        ends stripped. An empty note stays empty.

    Raises:
        ValueError: Starting with ``note:`` when the note is not text or the
            cleaned note is longer than MAX_NOTE_LENGTH characters.
    """
    if not isinstance(note, str):
        raise ValueError(f"note: expected text, got {type(note).__name__}")
    spaced = "".join(
        " " if char.isspace() or unicodedata.category(char) in REPLACED_BY_SPACE else char for char in note
    )
    cleaned = " ".join(spaced.split())
    if len(cleaned) > MAX_NOTE_LENGTH:
        raise ValueError(f"note: {len(cleaned)} characters, and the limit is {MAX_NOTE_LENGTH}")
    return cleaned


def _checked_key(key: Any) -> str:
    """Check a key for the store.

    Args:
        key: The candidate S3 key, of any type.

    Returns:
        The key unchanged.

    Raises:
        InvalidKey: Under the validate_key rules, and when the key holds a
            character that cannot be written to a UTF-8 file (a lone surrogate).
    """
    checked = validate_key(key)
    try:
        checked.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidKey("key: contains a character that cannot be written as UTF-8 text") from error
    return checked


def _validate_time(value: Any) -> float:
    """Check the video time of a sighting.

    Args:
        value: The candidate time in seconds, of any type.

    Returns:
        The time as a float.

    Raises:
        ValueError: Starting with ``time_seconds:`` when the value is not a
            finite number from 0 to MAX_TIME_SECONDS.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"time_seconds: expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"time_seconds: expected a finite number, got {value}")
    if not 0 <= value <= MAX_TIME_SECONDS:
        raise ValueError(f"time_seconds: expected 0 to {MAX_TIME_SECONDS} seconds, got {value}")
    return float(value)


def _validate_image(data: Any, field: str, signature: bytes, kind: str) -> bytes:
    """Check one posted image by its size and its first bytes.

    Args:
        data: The candidate image bytes, of any type.
        field: "frame_jpeg" or "crop_png", used in the error message.
        signature: The bytes every file of this kind starts with.
        kind: "JPEG" or "PNG", used in the error message.

    Returns:
        The image as bytes.

    Raises:
        ValueError: Starting with the field name when the value is not bytes,
            is empty, is larger than MAX_IMAGE_BYTES, or starts with other bytes.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError(f"{field}: expected {kind} bytes, got {type(data).__name__}")
    if not data:
        raise ValueError(f"{field}: empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"{field}: {len(data)} bytes, and the limit is {MAX_IMAGE_BYTES}")
    if not data.startswith(signature):
        raise ValueError(f"{field}: the data does not start with the {kind} signature, so it is not a {kind} image")
    return bytes(data)


def _remove_files(paths: Sequence[Path]) -> List[str]:
    """Remove files the store just wrote, after a later step failed.

    Args:
        paths: The files to remove. Missing files are fine.

    Returns:
        The paths that are still on disk because the removal failed.
    """
    stuck = []
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError:
            stuck.append(str(path))
    return stuck


def write_csv_atomic(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    """Write a CSV file so a reader sees the old file or the new one, never a part.

    The rows go to a temp file in the same folder, reach the disk, and then
    replace the target through os.replace.

    Args:
        path: The CSV file to write.
        columns: The header, which also sets the cell order.
        rows: One mapping per row with a value for every column.

    Raises:
        StoreError: With the path and the reason when the folder or the file
            cannot be written. The target stays as it was and the temp file
            is removed.
    """
    target = Path(path)
    temp_name = ""
    try:
        descriptor, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}-", suffix=TEMP_SUFFIX
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator=CSV_LINE_END)
            writer.writerow(columns)
            writer.writerows([row[column] for column in columns] for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, CSV_FILE_MODE)
        os.replace(temp_name, target)
    except BaseException as error:
        if temp_name:
            _remove_files([Path(temp_name)])
        if isinstance(error, (OSError, csv.Error, UnicodeError)):
            raise StoreError(f"{target}: could not write the file: {error}") from error
        raise


def _read_table(path: Path, columns: Sequence[str]) -> List[Tuple[int, Dict[str, str]]]:
    """Read one of the store's CSV files and insist on the expected header.

    Args:
        path: The CSV file. A missing file reads as no rows.
        columns: The exact header the file must have.

    Returns:
        One ``(line number, {column: cell})`` pair per row. Blank lines are skipped.

    Raises:
        StoreError: Naming the file when it cannot be read as UTF-8 CSV, is
            empty, has another header (the message shows both headers), or
            holds a row with the wrong number of cells.
    """
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            records = [(reader.line_num, cells) for cells in reader if cells]
    except (OSError, UnicodeError, csv.Error) as error:
        raise StoreError(f"{path}: could not read the file: {error}. {LEFT_ALONE}.") from error
    expected = list(columns)
    if not records:
        raise StoreError(f"{path}: the file is empty. Expected the header: {','.join(expected)}. {LEFT_ALONE}.")
    header = records[0][1]
    if header != expected:
        raise StoreError(
            f"{path}: unexpected header. Expected: {','.join(expected)}. Found: {','.join(header)}. {LEFT_ALONE}."
        )
    rows = []
    for line, cells in records[1:]:
        if len(cells) != len(expected):
            raise StoreError(f"{path} line {line}: {len(cells)} cells, expected {len(expected)}. {LEFT_ALONE}.")
        rows.append((line, dict(zip(expected, cells))))
    return rows


def _apply_counts(observations: Sequence[Mapping[str, str]], screened: Sequence[Dict[str, str]]) -> bool:
    """Recount the Sightings cell of every screened row from the observation rows.

    Args:
        observations: The rows of observations.csv.
        screened: The rows of videos_screened.csv, changed in place.

    Returns:
        True when at least one Sightings cell changed.
    """
    counts = Counter(row["S3Key"] for row in observations)
    changed = False
    for row in screened:
        counted = str(counts.get(row["S3Key"], 0))
        if row["Sightings"] != counted:
            row["Sightings"] = counted
            changed = True
    return changed


def tally_rows(rows: Sequence[Mapping[str, str]], species_names: Mapping[str, str]) -> List[Dict[str, Any]]:
    """Count sightings, videos, and the earliest survey year per species.

    Args:
        rows: Rows of observations.csv.
        species_names: ``{species code: scientific name}``. A code outside
            this mapping takes the name from its first row.

    Returns:
        One dict per species that has a sighting, sorted by code, with
        ``code``, ``name``, ``sightings``, ``videos`` (distinct S3 keys), and
        ``earliest_year`` (an int, or None when no video of that species
        carries a year in its name or folder).
    """
    years_by_key: Dict[str, Optional[int]] = {}
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code, key = row["SpeciesCode"], row["S3Key"]
        if key not in years_by_key:
            years_by_key[key] = parse_video_name(key, {}).year
        group = groups.setdefault(
            code, {"name": species_names.get(code) or row["Sponge Type"], "sightings": 0, "keys": set()}
        )
        group["sightings"] += 1
        group["keys"].add(key)
    tally = []
    for code in sorted(groups):
        group = groups[code]
        years = [years_by_key[key] for key in group["keys"] if years_by_key[key] is not None]
        tally.append(
            {
                "code": code,
                "name": group["name"],
                "sightings": group["sightings"],
                "videos": len(group["keys"]),
                "earliest_year": min(years) if years else None,
            }
        )
    return tally


def _file_part(label: str) -> str:
    """Turn a site code or transect label into one part of an image file name.

    Args:
        label: The label as parsed from the video name. It may be empty.

    Returns:
        ``file_safe(label)``, cut to MAX_FILE_PART_LENGTH characters so a
        very long label cannot push the file name past the file system limit.
    """
    return file_safe(label)[:MAX_FILE_PART_LENGTH].rstrip(FILE_PART_SEPARATOR)


def _fraction_text(value: float) -> str:
    """Format a fraction of the frame for a CSV cell.

    Args:
        value: A fraction from 0 to 1.

    Returns:
        The value with FRACTION_DECIMALS decimals, such as "0.2500".
    """
    return f"{value:.{FRACTION_DECIMALS}f}"


class ObservationStore:
    """The sightings, their images, and the screened-video log in one data folder.

    Attributes:
        data_dir: The data folder.
        observations_path: ``data/observations.csv``.
        screened_path: ``data/videos_screened.csv``.
        frames_dir: ``data/frames``, one JPEG per sighting.
        crops_dir: ``data/crops``, one PNG per sighting.
        trash_dir: ``data/trash``, where the images of deleted sightings go.
    """

    def __init__(
        self,
        data_dir: Path,
        species: Sequence[Species],
        site_names: Mapping[str, str],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Open a data folder, creating it and its image folders when missing.

        Args:
            data_dir: The folder that holds the CSV files and the images.
            species: The list from load_species. It must hold at least one species.
            site_names: ``{site code: site name}`` from load_site_names.
            clock: Returns the current moment. A datetime without a time zone
                is read as UTC. Tests pass a fixed clock.

        Raises:
            ValueError: Starting with the argument name when an argument has
                the wrong type, the species list is empty, or a code repeats.
            StoreError: When a folder cannot be created.
        """
        if not isinstance(data_dir, (str, os.PathLike)):
            raise ValueError(f"data_dir: expected a folder path, got {type(data_dir).__name__}")
        self._species = species_by_code(species)
        if not self._species:
            raise ValueError("species: the list is empty, so no sighting could be logged")
        if not isinstance(site_names, Mapping):
            raise ValueError(f"site_names: expected a mapping of site code to name, got {type(site_names).__name__}")
        if not callable(clock):
            raise ValueError(f"clock: expected a function that returns a datetime, got {type(clock).__name__}")
        self._site_names = dict(site_names)
        self._clock = clock
        self.data_dir = Path(data_dir)
        self.observations_path = self.data_dir / OBSERVATIONS_FILE
        self.screened_path = self.data_dir / SCREENED_FILE
        self.frames_dir = self.data_dir / FRAMES_FOLDER
        self.crops_dir = self.data_dir / CROPS_FOLDER
        self.trash_dir = self.data_dir / TRASH_FOLDER
        self._lock_path = self.data_dir / LOCK_FILE
        self._ensure_folders()

    def _ensure_folders(self) -> None:
        """Create the data folder and the image folders when they are missing.

        Raises:
            StoreError: With the folder and the reason when one cannot be created.
        """
        for folder in (self.data_dir, self.frames_dir, self.crops_dir, self.trash_dir):
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise StoreError(f"{folder}: could not create the folder: {error}") from error

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the exclusive write lock on ``data/.lock`` for one write.

        Each call opens its own descriptor, so the lock also holds between
        threads of one process. The lock is not reentrant: code inside the
        block must not take it again. The wait for another writer ends after
        LOCK_WAIT_SECONDS.

        Raises:
            StoreError: When the lock file cannot be opened or locked, or
                another writer keeps the lock for LOCK_WAIT_SECONDS.
        """
        self._ensure_folders()
        try:
            descriptor = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, LOCK_FILE_MODE)
        except OSError as error:
            raise StoreError(f"{self._lock_path}: could not open the lock file: {error}") from error
        try:
            self._wait_for_lock(descriptor)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _wait_for_lock(self, descriptor: int) -> None:
        """Take the exclusive lock, asking again every LOCK_POLL_SECONDS.

        Args:
            descriptor: The open lock file.

        Raises:
            StoreError: When another writer keeps the lock for
                LOCK_WAIT_SECONDS, or the lock call fails for another reason.
        """
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as error:
                if time.monotonic() >= deadline:
                    raise StoreError(
                        f"{self._lock_path}: another writer has kept the write lock for "
                        f"{LOCK_WAIT_SECONDS:g} seconds, so nothing was written. Close any other "
                        "Sponge Screener that uses this data folder, then try again."
                    ) from error
                time.sleep(LOCK_POLL_SECONDS)
            except OSError as error:
                raise StoreError(f"{self._lock_path}: could not take the write lock: {error}") from error

    def _now(self) -> datetime:
        """Read the clock.

        Returns:
            The current moment as a UTC datetime.

        Raises:
            StoreError: When the clock returns something other than a datetime.
        """
        moment = self._clock()
        if not isinstance(moment, datetime):
            raise StoreError(f"clock: expected a datetime from the clock, got {type(moment).__name__}")
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _read_observations(self) -> List[Dict[str, str]]:
        """Read and check observations.csv.

        Returns:
            The rows in file order.

        Raises:
            StoreError: Under the _read_table rules, and when an ID is not
                ``ID`` plus digits, an ID appears twice, an image file name is
                not a plain file name, or an S3Key is blank.
        """
        rows = []
        lines_by_id: Dict[str, int] = {}
        for line, row in _read_table(self.observations_path, OBSERVATION_COLUMNS):
            where = f"{self.observations_path} line {line}"
            obs_id = row["ID"]
            if not ID_PATTERN.fullmatch(obs_id):
                raise StoreError(f"{where}: ID {obs_id!r} is not {ID_PREFIX} followed by digits. {LEFT_ALONE}.")
            if obs_id in lines_by_id:
                raise StoreError(f"{where}: {obs_id} already appears on line {lines_by_id[obs_id]}. {LEFT_ALONE}.")
            for column in ("FrameFileName", "CropFileName"):
                if not SAFE_FILE_NAME.fullmatch(row[column]):
                    raise StoreError(f"{where}: {column} {row[column]!r} is not a plain file name. {LEFT_ALONE}.")
            if not row["S3Key"]:
                raise StoreError(f"{where}: S3Key is blank. {LEFT_ALONE}.")
            lines_by_id[obs_id] = line
            rows.append(row)
        return rows

    def _read_screened(self) -> List[Dict[str, str]]:
        """Read and check videos_screened.csv.

        Returns:
            The rows in file order.

        Raises:
            StoreError: Under the _read_table rules, and when an S3Key is
                blank or appears twice, or a Status is not one of the two statuses.
        """
        rows = []
        lines_by_key: Dict[str, int] = {}
        for line, row in _read_table(self.screened_path, SCREENED_COLUMNS):
            where = f"{self.screened_path} line {line}"
            key = row["S3Key"]
            if not key:
                raise StoreError(f"{where}: S3Key is blank. {LEFT_ALONE}.")
            if key in lines_by_key:
                raise StoreError(f"{where}: S3Key {key} already appears on line {lines_by_key[key]}. {LEFT_ALONE}.")
            if row["Status"] not in (STATUS_IN_PROGRESS, STATUS_DONE):
                raise StoreError(
                    f"{where}: Status {row['Status']!r} must be {STATUS_IN_PROGRESS!r} or {STATUS_DONE!r}. "
                    f"{LEFT_ALONE}."
                )
            lines_by_key[key] = line
            rows.append(row)
        return rows

    def _species_for(self, species_code: Any) -> Species:
        """Look up the species of a sighting.

        Args:
            species_code: The candidate code, of any type.

        Returns:
            The Species with that code.

        Raises:
            ValueError: Starting with ``species_code:`` when the code is not
                in the species list.
        """
        if not isinstance(species_code, str) or species_code not in self._species:
            raise ValueError(f"species_code: unknown species code {species_code!r}")
        return self._species[species_code]

    def _validate_targets(self, target_species: Any) -> List[str]:
        """Check the species an annotator screens a video for.

        Args:
            target_species: A list or tuple of species codes. It may be empty.

        Returns:
            The codes in the given order, each one once.

        Raises:
            ValueError: Starting with ``target_species:`` when the value is
                not a list or tuple, or a code is not in the species list.
        """
        if not isinstance(target_species, (list, tuple)):
            raise ValueError(
                f"target_species: expected a list of species codes, got {type(target_species).__name__}"
            )
        for code in target_species:
            if not isinstance(code, str) or code not in self._species:
                raise ValueError(f"target_species: unknown species code {code!r}")
        return list(dict.fromkeys(target_species))

    def _move_to_trash(self, path: Path, moment: datetime) -> None:
        """Move a file into ``data/trash/<UTC stamp>_<file name>``.

        A file that is already gone is left out without an error. A taken
        trash name gets ``-2``, ``-3`` and so on after the stamp.

        Args:
            path: The file to move.
            moment: The moment that stamps the trash name.

        Raises:
            StoreError: With both paths and the reason when the move fails.
        """
        if not os.path.lexists(path):
            return
        stamp = moment.strftime(TRASH_STAMP_FORMAT)
        target = self.trash_dir / f"{stamp}_{path.name}"
        attempt = 2
        while os.path.lexists(target):
            target = self.trash_dir / f"{stamp}-{attempt}_{path.name}"
            attempt += 1
        try:
            os.replace(path, target)
        except OSError as error:
            raise StoreError(f"{path}: could not move the file to {target}: {error}") from error

    def _write_image(self, path: Path, data: bytes, moment: datetime, created: List[Path]) -> None:
        """Write one image, never over an existing file.

        A file that already sits at the path belongs to no row (the new ID is
        unused), so it is moved to the trash first and nothing is destroyed.

        Args:
            path: Where the image goes.
            data: The image bytes.
            moment: The moment that stamps a trash name, when one is needed.
            created: Receives the path before the write starts, so the caller
                can remove the file when a later step fails.

        Raises:
            StoreError: With the path and the reason when the write fails.
        """
        self._move_to_trash(path, moment)
        created.append(path)
        try:
            with open(path, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise StoreError(f"{path}: could not write the image: {error}") from error

    def _sync_counts(self, observations: Sequence[Mapping[str, str]], screened: List[Dict[str, str]]) -> None:
        """Rewrite videos_screened.csv when a Sightings count went stale.

        Args:
            observations: The rows now in observations.csv.
            screened: The rows read from videos_screened.csv under the same lock.

        Raises:
            StoreError: When the file cannot be written.
        """
        if _apply_counts(observations, screened):
            write_csv_atomic(self.screened_path, SCREENED_COLUMNS, screened)

    def _build_row(
        self,
        obs_id: str,
        video: VideoName,
        key: str,
        seconds: float,
        point: Tuple[float, float],
        box: Optional[Tuple[float, float, float, float]],
        species: Species,
        note: str,
        annotator: str,
        moment: datetime,
    ) -> Dict[str, str]:
        """Assemble the CSV row of a sighting from checked values.

        Args:
            obs_id: The new ID.
            video: What the file name of the key says about the video.
            key: The S3 key of the video.
            seconds: The video time of the sighting.
            point: The clicked point as fractions of the frame.
            box: The dragged box as fractions of the frame, or None.
            species: The species of the sighting.
            note: The cleaned note. It may be empty.
            annotator: The annotator initials.
            moment: When the sighting is logged, in UTC.

        Returns:
            ``{column: text}`` for every column of OBSERVATION_COLUMNS.
        """
        quadrant = quadrant_of(*anchor_point(point, box))
        phrase = QUADRANT_PHRASES[quadrant]
        stem = "_".join([obs_id, species.code, quadrant, _file_part(video.site_code), _file_part(video.transect)])
        box_cells = [_fraction_text(value) for value in box] if box is not None else ["", "", "", ""]
        return {
            "Site": video.site_name,
            "Transect": video.transect,
            "Sponge Type": species.name,
            "Timestamp": format_clock(seconds),
            "Notes": phrase + (NOTE_SEPARATOR + note if note else ""),
            "ID": obs_id,
            "FileName": video.file_name,
            "FrameFileName": stem + FRAME_EXTENSION,
            "AbbreviatedNote": quadrant,
            "SpeciesCode": species.code,
            "TimestampSeconds": f"{seconds:.{SECONDS_DECIMALS}f}",
            "Quadrant": quadrant,
            "PointX": _fraction_text(point[0]),
            "PointY": _fraction_text(point[1]),
            "BoxX": box_cells[0],
            "BoxY": box_cells[1],
            "BoxW": box_cells[2],
            "BoxH": box_cells[3],
            "CropFileName": stem + CROP_EXTENSION,
            "S3Key": key,
            "Annotator": annotator,
            "LoggedAt": moment.strftime(STAMP_FORMAT),
        }

    def add(
        self,
        *,
        key: str,
        time_seconds: float,
        point: Any,
        box: Any,
        species_code: str,
        note: str,
        annotator: str,
        frame_jpeg: bytes,
        crop_png: bytes,
    ) -> Dict[str, str]:
        """Save one sighting: both images first, then the CSV row.

        The ID is the highest ID in observations.csv plus one, with at least
        three digits, and it is assigned inside the write lock.

        Args:
            key: The S3 key of the video.
            time_seconds: The video time, 0 to MAX_TIME_SECONDS.
            point: ``{"x", "y"}`` as fractions of the frame.
            box: None, or ``{"x", "y", "w", "h"}`` as fractions of the frame.
            species_code: A code from the species list.
            note: Free text up to MAX_NOTE_LENGTH characters after cleaning.
            annotator: Initials under the validate_annotator rules.
            frame_jpeg: The full frame as JPEG bytes, 1 byte to MAX_IMAGE_BYTES.
            crop_png: The sponge crop as PNG bytes, 1 byte to MAX_IMAGE_BYTES.

        Returns:
            The new row, ``{column: text}`` for every column of OBSERVATION_COLUMNS.

        Raises:
            ValueError: Starting with the name of the bad argument. Nothing is written.
            StoreError: When a CSV file cannot be parsed (nothing is written),
                or an image or observations.csv cannot be written (the new
                images are removed and no row is added), or the row is saved
                and the Sightings counts in videos_screened.csv could not be
                updated (the message says the sighting is saved).
        """
        checked_key = _checked_key(key)
        seconds = _validate_time(time_seconds)
        checked_point = validate_point(point)
        checked_box = validate_box(box)
        species = self._species_for(species_code)
        cleaned_note = clean_note(note)
        checked_annotator = validate_annotator(annotator)
        frame = _validate_image(frame_jpeg, "frame_jpeg", JPEG_SIGNATURE, "JPEG")
        crop = _validate_image(crop_png, "crop_png", PNG_SIGNATURE, "PNG")
        video = parse_video_name(checked_key, self._site_names)
        with self._locked():
            moment = self._now()
            observations = self._read_observations()
            screened = self._read_screened()
            highest = max((int(ID_PATTERN.fullmatch(row["ID"]).group(1)) for row in observations), default=0)
            obs_id = f"{ID_PREFIX}{highest + 1:0{ID_MIN_DIGITS}d}"
            row = self._build_row(
                obs_id,
                video,
                checked_key,
                seconds,
                checked_point,
                checked_box,
                species,
                cleaned_note,
                checked_annotator,
                moment,
            )
            created: List[Path] = []
            try:
                self._write_image(self.frames_dir / row["FrameFileName"], frame, moment, created)
                self._write_image(self.crops_dir / row["CropFileName"], crop, moment, created)
                write_csv_atomic(self.observations_path, OBSERVATION_COLUMNS, observations + [row])
            except Exception as error:
                stuck = _remove_files(created)
                if stuck:
                    raise StoreError(
                        f"{error}. These new image files could not be removed: {', '.join(stuck)}"
                    ) from error
                raise
            try:
                self._sync_counts(observations + [row], screened)
            except StoreError as error:
                raise StoreError(
                    f"{obs_id} is saved in {OBSERVATIONS_FILE}, but the sighting counts were not updated: {error}"
                ) from error
        return dict(row)

    def delete(self, obs_id: str) -> Dict[str, str]:
        """Remove one sighting and move its two images into the trash folder.

        The row leaves observations.csv first. The images then move to
        ``data/trash/<UTC stamp>_<file name>``; an image that is already gone
        is skipped.

        Args:
            obs_id: The ID of the sighting, such as "ID007".

        Returns:
            The removed row.

        Raises:
            ValueError: When obs_id is not text.
            UnknownObservation: When no sighting has that ID. It is a StoreError.
            StoreError: When a CSV file cannot be parsed or written, or the
                row is removed and an image could not be moved or the
                Sightings counts could not be updated (the message says the
                row is removed).
        """
        if not isinstance(obs_id, str):
            raise ValueError(f"obs_id: expected text, got {type(obs_id).__name__}")
        with self._locked():
            moment = self._now()
            observations = self._read_observations()
            screened = self._read_screened()
            removed = next((row for row in observations if row["ID"] == obs_id), None)
            if removed is None:
                raise UnknownObservation(f"obs_id: no sighting has the ID {obs_id!r} in {self.observations_path}")
            remaining = [row for row in observations if row is not removed]
            write_csv_atomic(self.observations_path, OBSERVATION_COLUMNS, remaining)
            problems = []
            for folder, column in ((self.frames_dir, "FrameFileName"), (self.crops_dir, "CropFileName")):
                try:
                    self._move_to_trash(folder / removed[column], moment)
                except StoreError as error:
                    problems.append(str(error))
            try:
                self._sync_counts(remaining, screened)
            except StoreError as error:
                problems.append(str(error))
            if problems:
                raise StoreError(f"{obs_id} is removed from {OBSERVATIONS_FILE}, but: {'; '.join(problems)}")
        return dict(removed)

    def rows(self, key: Optional[str] = None) -> List[Dict[str, str]]:
        """Return the sightings in file order.

        Args:
            key: When given, only the sightings of this video are returned.

        Returns:
            One ``{column: text}`` dict per sighting.

        Raises:
            InvalidKey: When a key is given and fails the key rules.
            StoreError: When observations.csv cannot be parsed.
        """
        if key is None:
            return self._read_observations()
        checked_key = _checked_key(key)
        return [row for row in self._read_observations() if row["S3Key"] == checked_key]

    def tally(self) -> List[Dict[str, Any]]:
        """Count the sightings per species across all videos.

        Returns:
            The tally_rows result for every row of observations.csv, with
            names from the species list.

        Raises:
            StoreError: When observations.csv cannot be parsed.
        """
        names = {code: item.name for code, item in self._species.items()}
        return tally_rows(self._read_observations(), names)

    def _upsert_screened(
        self, key: Any, annotator: Any, target_species: Any, done: Optional[bool]
    ) -> Dict[str, str]:
        """Create or update the videos_screened.csv row of one video.

        Args:
            key: The S3 key of the video.
            annotator: The annotator initials.
            target_species: The species codes the annotator screens for.
            done: None to only make sure the row exists, True to mark the
                video done, False to return it to in progress.

        Returns:
            The row after the change.

        Raises:
            ValueError: Starting with the name of the bad argument.
            StoreError: When a CSV file cannot be parsed or written.
        """
        checked_key = _checked_key(key)
        checked_annotator = validate_annotator(annotator)
        targets = TARGET_SEPARATOR.join(self._validate_targets(target_species))
        with self._locked():
            stamp = self._now().strftime(STAMP_FORMAT)
            observations = self._read_observations()
            screened = self._read_screened()
            before = [dict(row) for row in screened]
            row = next((item for item in screened if item["S3Key"] == checked_key), None)
            if row is None:
                row = {
                    "S3Key": checked_key,
                    "FileName": parse_video_name(checked_key, self._site_names).file_name,
                    "Status": STATUS_IN_PROGRESS,
                    "TargetSpecies": targets,
                    "Sightings": "0",
                    "Annotator": checked_annotator,
                    "FirstOpened": stamp,
                    "MarkedDone": "",
                }
                screened.append(row)
            if done is True:
                row.update(
                    {
                        "Status": STATUS_DONE,
                        "MarkedDone": stamp,
                        "TargetSpecies": targets,
                        "Annotator": checked_annotator,
                    }
                )
            elif done is False:
                row.update({"Status": STATUS_IN_PROGRESS, "MarkedDone": ""})
            _apply_counts(observations, screened)
            if screened != before:
                write_csv_atomic(self.screened_path, SCREENED_COLUMNS, screened)
            return dict(row)

    def mark_opened(self, key: str, annotator: str, target_species: Sequence[str]) -> Dict[str, str]:
        """Record that an annotator opened a video.

        A video without a row gets one with the Status ``in progress``, the
        FirstOpened stamp, the annotator, and the target species. An existing
        row keeps every cell, so its FirstOpened stamp and a ``done`` status
        stay as they are; only its Sightings count is refreshed.

        Args:
            key: The S3 key of the video.
            annotator: The annotator initials.
            target_species: The species codes the annotator screens for.

        Returns:
            The row of the video.

        Raises:
            ValueError: Starting with the name of the bad argument.
            StoreError: When a CSV file cannot be parsed or written.
        """
        return self._upsert_screened(key, annotator, target_species, None)

    def mark_done(self, key: str, done: bool, annotator: str, target_species: Sequence[str]) -> Dict[str, str]:
        """Mark a video fully screened, or return it to in progress.

        ``done=True`` sets the Status ``done``, the MarkedDone stamp, the
        target species (codes joined by ``;``), and the annotator.
        ``done=False`` sets the Status ``in progress`` and clears MarkedDone.
        A video without a row gets one first, as in mark_opened.

        Args:
            key: The S3 key of the video.
            done: True or False.
            annotator: The annotator initials.
            target_species: The species codes the annotator screened for.

        Returns:
            The row of the video after the change.

        Raises:
            ValueError: Starting with the name of the bad argument.
            StoreError: When a CSV file cannot be parsed or written.
        """
        if not isinstance(done, bool):
            raise ValueError(f"done: expected true or false, got {type(done).__name__}")
        return self._upsert_screened(key, annotator, target_species, done)

    def screened(self) -> List[Dict[str, str]]:
        """Return the rows of videos_screened.csv in file order.

        Returns:
            One ``{column: text}`` dict per video. Sightings is recounted from
            observations.csv at read time.

        Raises:
            StoreError: When a CSV file cannot be parsed.
        """
        observations = self._read_observations()
        screened = self._read_screened()
        _apply_counts(observations, screened)
        return screened

    def video_status(self) -> Dict[str, Dict[str, Any]]:
        """Summarize every video the annotators touched.

        Returns:
            ``{S3 key: {"status": "in progress" or "done", "sightings": int}}``
            for every video in videos_screened.csv, plus every video that has
            sightings and no row there (status ``in progress``).

        Raises:
            StoreError: When a CSV file cannot be parsed.
        """
        counts = Counter(row["S3Key"] for row in self._read_observations())
        status = {
            row["S3Key"]: {"status": row["Status"], "sightings": counts.get(row["S3Key"], 0)}
            for row in self._read_screened()
        }
        for key, count in counts.items():
            if key not in status:
                status[key] = {"status": STATUS_IN_PROGRESS, "sightings": count}
        return status
