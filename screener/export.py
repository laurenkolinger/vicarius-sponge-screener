"""The dated export package for OnDeck AI.

One call writes ``<export root>/spongeGroundTruth_<YYYYMMDD>`` with
observations.csv, videos_screened.csv, the frame and the crop of every
sighting, and a README that explains the columns and the conventions.

Everything in a package comes from one read of observations.csv, so the CSV,
the images, and the README counts always agree, even when another tab saves a
sighting during the export. The export only reads the store.
"""

import os
import shutil
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from screener.config import CROP_SIZE, PLAYABLE_EXTENSIONS, VERSION, VIDEO_EXTENSIONS
from screener.keys import extension_of
from screener.store import (
    CROPS_FOLDER,
    FRAMES_FOLDER,
    OBSERVATION_COLUMNS,
    OBSERVATIONS_FILE,
    SCREENED_COLUMNS,
    SCREENED_FILE,
    STATUS_DONE,
    ObservationStore,
    StoreError,
    tally_rows,
    write_csv_atomic,
)

PACKAGE_PREFIX = "spongeGroundTruth_"
PACKAGE_DATE_FORMAT = "%Y%m%d"
README_DATE_FORMAT = "%Y-%m-%d"
README_FILE = "README.md"
MAX_PACKAGES_PER_DAY = 999
NO_YEAR = "none"

OBSERVATION_COLUMN_NOTES: Dict[str, str] = {
    "Site": (
        "Site name from config/sites.csv for the site code in the video file name. The site code when that file "
        "lists no name. Empty when the file name is outside the TCRMP pattern."
    ),
    "Transect": "Transect label from the video file name, exactly as written (for example T1, T1-6, T1+T3-6).",
    "Sponge Type": "Scientific name of the species.",
    "Timestamp": "Video time of the sighting as MM:SS, rounded down to the whole second.",
    "Notes": "The quadrant of the frame in words (for example top left), then a comma and the annotator's note.",
    "ID": "Sighting number: ID001, ID002, and so on.",
    "FileName": "File name of the video.",
    "FrameFileName": "File name of the full frame (JPEG) in frames/.",
    "AbbreviatedNote": "Quadrant code: TOPLEFT, TOPRIGHT, BOTTOMLEFT, or BOTTOMRIGHT.",
    "SpeciesCode": "Four letter species code from config/species.csv.",
    "TimestampSeconds": "Video time of the sighting in seconds, with three decimals.",
    "Quadrant": (
        "Quadrant code of the sponge. It comes from the center of the box when the annotator drew a box, and "
        "from the point otherwise."
    ),
    "PointX": "Horizontal position of the marked point, as a fraction of the frame width from the left edge.",
    "PointY": "Vertical position of the marked point, as a fraction of the frame height from the top edge.",
    "BoxX": "Left edge of the box, as a fraction of the frame width. Empty when the annotator drew no box.",
    "BoxY": "Top edge of the box, as a fraction of the frame height. Empty when the annotator drew no box.",
    "BoxW": "Width of the box, as a fraction of the frame width. Empty when the annotator drew no box.",
    "BoxH": "Height of the box, as a fraction of the frame height. Empty when the annotator drew no box.",
    "CropFileName": "File name of the sponge crop (PNG) in crops/.",
    "S3Key": "Full key of the video in the uviai S3 bucket. The key identifies the video.",
    "Annotator": "Initials of the person who logged the sighting.",
    "LoggedAt": "When the sighting was saved, in UTC, as YYYY-MM-DDTHH:MM:SSZ.",
}
SCREENED_COLUMN_NOTES: Dict[str, str] = {
    "S3Key": "Full key of the video in the uviai S3 bucket. The key identifies the video.",
    "FileName": "File name of the video.",
    "Status": "`in progress` after an annotator opened the video, `done` after the annotator marked it fully screened.",
    "TargetSpecies": "Codes of the species the annotator screened the video for, separated by semicolons.",
    "Sightings": "Number of rows in observations.csv for this video.",
    "Annotator": "Initials of the person who opened the video or, for a done video, marked it done.",
    "FirstOpened": "When an annotator first opened the video, in UTC.",
    "MarkedDone": "When the annotator marked the video done, in UTC. Empty while the video is in progress.",
}


class ExportError(Exception):
    """Raised when an export package cannot be written in full."""


def _plural(count: int, word: str) -> str:
    """Join a count and its noun.

    Args:
        count: The number of things.
        word: The singular noun, which takes a plain "s" in the plural.

    Returns:
        Text such as "1 sighting" or "5 sightings".
    """
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _table(headers: Sequence[str], records: Sequence[Sequence[str]]) -> List[str]:
    """Lay out a Markdown table.

    Args:
        headers: The column titles.
        records: One sequence of cell texts per row.

    Returns:
        The lines of the table. A ``|`` inside a cell is escaped so it cannot
        split the cell.
    """
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for record in records:
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in record) + " |")
    return lines


def _readme_text(rows: Sequence[Mapping[str, str]], screened: Sequence[Mapping[str, str]], today: date) -> str:
    """Write the README of a package.

    Args:
        rows: The observation rows in the package.
        screened: The videos_screened.csv rows in the package.
        today: The export date.

    Returns:
        Markdown text with the title, the date, the counts per species, the
        number of videos, the conventions, and the meaning of every column of
        both CSV files.
    """
    converted_formats = sorted(extension.upper() for extension in VIDEO_EXTENSIONS - PLAYABLE_EXTENSIONS)
    from_converted = sum(1 for row in rows if extension_of(row["S3Key"]) not in PLAYABLE_EXTENSIONS)
    done = sum(1 for row in screened if row["Status"] == STATUS_DONE)
    species_records = [
        [item["code"], item["name"], item["sightings"], item["videos"], item["earliest_year"] or NO_YEAR]
        for item in tally_rows(rows, {})
    ]
    lines = [
        f"# Sponge ground truth, exported {today.strftime(README_DATE_FORMAT)}",
        "",
        f"Sponge Screener {VERSION} wrote this package. An annotator logged each sighting by pausing a TCRMP "
        "transect video, marking the sponge, and choosing the species.",
        "",
        "## Contents",
        "",
        f"- `{OBSERVATIONS_FILE}`: one row per sponge sighting ({_plural(len(rows), 'row')}).",
        f"- `{SCREENED_FILE}`: one row per video that an annotator opened ({_plural(len(screened), 'row')}).",
        f"- `{FRAMES_FOLDER}/`: the full video frame of each sighting, as JPEG, at the resolution of the video.",
        f"- `{CROPS_FOLDER}/`: the sponge crop of each sighting, as PNG. A click crops a {CROP_SIZE} pixel square "
        "around the point. A dragged box crops the box.",
        "",
        "## Counts",
        "",
        f"- Sightings: {len(rows)}",
        f"- Videos with at least one sighting: {len({row['S3Key'] for row in rows})}",
        f"- Videos in the screening log: {len(screened)} ({done} done, {len(screened) - done} in progress)",
        "",
        "Earliest year is the earliest survey year among the videos that show the species. It reads "
        f"`{NO_YEAR}` when no such video carries a year in its name or its folder.",
        "",
    ]
    lines += _table(["Code", "Species", "Sightings", "Videos", "Earliest year"], species_records)
    lines += [
        "",
        "## Conventions",
        "",
        "- Timestamp: the annotator logs each sponge at the moment it sits closest to the center of the frame. "
        "`Timestamp` rounds that moment down to the whole second, and `TimestampSeconds` keeps three decimals.",
        "- True absence: a video with the Status `done` was screened from start to end for every species in its "
        "`TargetSpecies` cell. A done video with no rows for one of those species is a true absence of that "
        "species in that video.",
        "- Positions: points and boxes are fractions of the frame width and height, measured from the top left "
        "corner of the frame.",
        f"- Converted videos: Chrome cannot play {', '.join(converted_formats[:-1])}, or {converted_formats[-1]} "
        "files, and it cannot play some MOV files, so the app converts those videos to MP4 before playback. "
        "Frames and crops from a converted video come from "
        "that re-encoded copy, so their pixels can differ slightly from the original file. This package holds "
        f"{_plural(from_converted, 'sighting')} from videos in those formats.",
        "- Times in `LoggedAt`, `FirstOpened`, and `MarkedDone` are UTC.",
        "",
        f"## Columns of {OBSERVATIONS_FILE}",
        "",
    ]
    observation_notes = [[f"`{name}`", OBSERVATION_COLUMN_NOTES[name]] for name in OBSERVATION_COLUMNS]
    screened_notes = [[f"`{name}`", SCREENED_COLUMN_NOTES[name]] for name in SCREENED_COLUMNS]
    lines += _table(["Column", "Meaning"], observation_notes)
    lines += ["", f"## Columns of {SCREENED_FILE}", ""]
    lines += _table(["Column", "Meaning"], screened_notes)
    return "\n".join(lines) + "\n"


def _create_package_folder(root: Path, today: date) -> Path:
    """Create the first free package folder for a date.

    Args:
        root: The existing export folder.
        today: The export date.

    Returns:
        The new folder: ``spongeGroundTruth_<YYYYMMDD>``, or the same name
        with ``_2``, ``_3`` and so on when earlier names are taken.

    Raises:
        ExportError: With the path and the reason when the folder cannot be
            created, or when MAX_PACKAGES_PER_DAY names are taken.
    """
    base = PACKAGE_PREFIX + today.strftime(PACKAGE_DATE_FORMAT)
    for attempt in range(1, MAX_PACKAGES_PER_DAY + 1):
        candidate = root / (base if attempt == 1 else f"{base}_{attempt}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        except OSError as error:
            raise ExportError(f"{candidate}: could not create the package folder: {error}") from error
        return candidate
    raise ExportError(
        f"{root}: {MAX_PACKAGES_PER_DAY} packages named {base} already exist; remove some and export again"
    )


def _image_copies(store: ObservationStore, rows: Sequence[Mapping[str, str]]) -> List[Tuple[str, Path, str]]:
    """List every image the rows reference.

    Args:
        store: The store that holds the images.
        rows: The observation rows to export.

    Returns:
        One ``(sighting ID, source path, path inside the package)`` triple
        per frame and per crop, in row order.
    """
    copies = []
    for row in rows:
        copies.append((row["ID"], store.frames_dir / row["FrameFileName"], f"{FRAMES_FOLDER}/{row['FrameFileName']}"))
        copies.append((row["ID"], store.crops_dir / row["CropFileName"], f"{CROPS_FOLDER}/{row['CropFileName']}"))
    return copies


def _fill_package(
    package: Path,
    rows: Sequence[Mapping[str, str]],
    screened: Sequence[Mapping[str, str]],
    copies: Sequence[Tuple[str, Path, str]],
    today: date,
) -> None:
    """Write the CSV files, the images, and the README into a new package folder.

    Args:
        package: The empty package folder.
        rows: The observation rows.
        screened: The videos_screened.csv rows, with counts that match ``rows``.
        copies: The ``(sighting ID, source, path inside the package)`` triples.
        today: The export date.

    Raises:
        ExportError: Naming the first file that could not be written, or the
            first image that did not arrive whole.
    """
    try:
        write_csv_atomic(package / OBSERVATIONS_FILE, OBSERVATION_COLUMNS, rows)
        write_csv_atomic(package / SCREENED_FILE, SCREENED_COLUMNS, screened)
    except StoreError as error:
        raise ExportError(str(error)) from error
    for obs_id, source, inside in copies:
        target = package / inside
        try:
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
        except OSError as error:
            raise ExportError(f"{target}: could not copy the image of {obs_id} from {source}: {error}") from error
    for obs_id, source, inside in copies:
        target = package / inside
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            raise ExportError(f"{target}: the image of {obs_id} did not arrive whole from {source}")
    readme = package / README_FILE
    try:
        readme.write_text(_readme_text(rows, screened, today), encoding="utf-8")
    except OSError as error:
        raise ExportError(f"{readme}: could not write the README: {error}") from error


def export_package(store: ObservationStore, export_root: Path, today: date) -> Path:
    """Write a dated export package and check that every image arrived.

    Args:
        store: The store to export. The export reads it and changes nothing.
        export_root: An existing folder, such as the OnDeck Drive folder. The
            export never creates it, because a missing folder usually means
            the drive is not connected.
        today: The date for the package name and the README.

    Returns:
        The package folder, ``<export_root>/spongeGroundTruth_<YYYYMMDD>``, or
        that name with ``_2``, ``_3`` and so on when earlier names are taken.

    Raises:
        ValueError: Starting with the argument name when an argument has the
            wrong type.
        ExportError: When export_root is not an existing folder, the store
            cannot be parsed, the store has no sightings, a referenced image
            is missing (the message names the first missing file and nothing
            is written), or a file cannot be written or does not arrive whole
            (the partial package is removed).
    """
    if not isinstance(store, ObservationStore):
        raise ValueError(f"store: expected an ObservationStore, got {type(store).__name__}")
    if not isinstance(export_root, (str, os.PathLike)):
        raise ValueError(f"export_root: expected a folder path, got {type(export_root).__name__}")
    if not isinstance(today, date):
        raise ValueError(f"today: expected a date, got {type(today).__name__}")
    root = Path(export_root)
    if not root.is_dir():
        raise ExportError(
            f"export folder not found: {root}. Connect the drive or choose another folder, then export again."
        )
    try:
        rows = store.rows()
        screened = store.screened()
    except StoreError as error:
        raise ExportError(f"the store could not be read: {error}") from error
    if not rows:
        raise ExportError(f"no sightings to export: {store.observations_path} has no rows yet")
    counts = Counter(row["S3Key"] for row in rows)
    for row in screened:
        row["Sightings"] = str(counts.get(row["S3Key"], 0))
    copies = _image_copies(store, rows)
    for obs_id, source, _inside in copies:
        if not source.is_file():
            raise ExportError(f"missing image: {source} (sighting {obs_id}). Nothing was exported.")
    package = _create_package_folder(root, today)
    try:
        _fill_package(package, rows, screened, copies, today)
    except BaseException:
        shutil.rmtree(package, ignore_errors=True)
        raise
    return package
