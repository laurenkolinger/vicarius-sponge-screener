"""The dated export package for OnDeck AI.

One call writes ``<export root>/spongeGroundTruth_<YYYYMMDD>`` with
observations.csv, tracks.csv, videos_screened.csv, the frame and the crop of
every labeled sighting, and a README that explains the columns and the
conventions.

Later points (sightings with no species yet) stay out of the package, and the
README says how many stayed out. The observations.csv of a package has no
LabelStatus column, because every row in it is labeled.

tracks.csv holds one row per track point of each labeled sighting that has a
track, and only the header when no sighting has one. A track file that cannot
be read stops the export before anything is written. A track saved for another
moment than its row's TimestampSeconds belongs to another mark, so the export
skips it and writes one line about it to stderr.

The observation rows, the left-out count, the images, and the README counts
come from one read of observations.csv, so they agree even when another tab
saves a sighting during the export. tracks.csv comes from the track files of
those rows, each read once, right after: a track that the background job
saves during the export lands in this package or waits for the next one. The
export only reads the store.
"""

import os
import shutil
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Sequence, Tuple

from screener.config import CROP_SIZE, PLAYABLE_EXTENSIONS, VERSION, VIDEO_EXTENSIONS
from screener.keys import extension_of
from screener.store import (
    CROPS_FOLDER,
    FRACTION_DECIMALS,
    FRAMES_FOLDER,
    LABEL_STATUS_COLUMN,
    LABELED,
    OBSERVATION_COLUMNS,
    OBSERVATIONS_FILE,
    SCREENED_COLUMNS,
    SCREENED_FILE,
    SECONDS_DECIMALS,
    STATUS_DONE,
    TIME_MATCH_SECONDS,
    ObservationStore,
    StoreError,
    cell_matches,
    tally_rows,
    write_csv_atomic,
)
from screener.tracks import TrackError

# The columns of the exported observations.csv: every row there is labeled, so
# the LabelStatus column carries no information.
EXPORT_COLUMNS = [column for column in OBSERVATION_COLUMNS if column != LABEL_STATUS_COLUMN]

# One row per track point. The box columns hold the box centered on X and Y and
# stay empty for a sighting that was marked with a point.
TRACKS_FILE = "tracks.csv"
TRACK_COLUMNS = ["ID", "SpeciesCode", "TimeSeconds", "X", "Y", "BoxX", "BoxY", "BoxW", "BoxH", "Score"]

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
TRACK_COLUMN_NOTES: Dict[str, str] = {
    "ID": "Sighting number, the same ID as in observations.csv.",
    "SpeciesCode": "Four letter species code of the sighting, the same as in observations.csv.",
    "TimeSeconds": "Video time of the track point in seconds, with three decimals.",
    "X": "Horizontal position of the sponge at that time, as a fraction of the frame width from the left edge.",
    "Y": "Vertical position of the sponge at that time, as a fraction of the frame height from the top edge.",
    "BoxX": (
        "Left edge of the box centered on X and Y, as a fraction of the frame width. Empty when the annotator "
        "marked the sponge with a point."
    ),
    "BoxY": (
        "Top edge of the box centered on X and Y, as a fraction of the frame height. Empty when the annotator "
        "marked the sponge with a point."
    ),
    "BoxW": (
        "Width of the box, as a fraction of the frame width. It is the same on every row of a sighting. Empty "
        "when the annotator marked the sponge with a point."
    ),
    "BoxH": (
        "Height of the box, as a fraction of the frame height. It is the same on every row of a sighting. Empty "
        "when the annotator marked the sponge with a point."
    ),
    "Score": "How closely the picture at that time matched the sponge, from 0 to 1. A higher score is a closer match.",
}
TRACKS_INTRO = (
    "Sponge Lab follows each labeled sighting through its video as the camera moves. `tracks.csv` lists the "
    "position of the sponge at each moment, one row per track point, in sighting order and then in time order. A "
    "sighting without a track has no rows. A track covers the stretch of video in which the sponge could be "
    "followed, so it can begin after the sponge entered the frame and end before the sponge left it. The "
    "positions are estimates from a visual tracker and can drift; `Score` shows how closely each point matched."
)
TRACKS_BOX_NOTE = (
    "A box centered near the edge of the frame reaches past it, so `BoxX` and `BoxY` can be below 0 and `BoxX` plus "
    "`BoxW` can be above 1."
)
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


class ExportResult(NamedTuple):
    """What export_package wrote.

    Attributes:
        path: The package folder.
        unlabeled_left_out: How many Later points the package leaves out,
            counted from the same read of observations.csv as the package rows.
    """

    path: Path
    unlabeled_left_out: int


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


def _decimal_text(value: float, decimals: int) -> str:
    """Format a number with a fixed count of decimals, never as a negative zero.

    Args:
        value: The number.
        decimals: How many digits follow the decimal point.

    Returns:
        The number rounded to ``decimals`` places. A value that rounds to zero
        reads ``0.0000`` even when it is a hair below zero.
    """
    return f"{round(value, decimals) + 0.0:.{decimals}f}"


def _track_rows(store: ObservationStore, rows: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    """List the tracks.csv rows of the labeled sightings, one per track point.

    Args:
        store: The store that holds the track files.
        rows: The labeled observation rows in the package, in file order.

    Returns:
        One ``{column: text}`` dict for every column of TRACK_COLUMNS and
        every point of every track, in the order of ``rows`` and then of
        time. Times have SECONDS_DECIMALS decimals and fractions have
        FRACTION_DECIMALS. A track with a box gets the box centered on the
        point, so BoxX and BoxY can fall below 0 near the edge of the frame;
        a track without one leaves the four box columns empty. A sighting
        without a track file adds no rows, and neither does a track saved for
        another moment than the TimestampSeconds of its row (a leftover of
        another mark); that track is skipped with one line on stderr that
        names its file.

    Raises:
        ExportError: Naming the file and the sighting when a track file
            cannot be read or fails its checks. Nothing is written.
    """
    records: List[Dict[str, str]] = []
    for row in rows:
        try:
            track = store.tracks.load(row["ID"])
        except TrackError as error:
            raise ExportError(f"damaged track of {row['ID']}: {error}. Nothing was exported.") from error
        if track is None:
            continue
        if not cell_matches(row["TimestampSeconds"], track["anchor_time"], TIME_MATCH_SECONDS):
            sys.stderr.write(
                f"export: skipped {store.tracks.path_for(row['ID'])}: it was saved for the sighting at "
                f"{_decimal_text(track['anchor_time'], SECONDS_DECIMALS)} seconds, but {row['ID']} is at "
                f"{row['TimestampSeconds']} seconds\n"
            )
            continue
        box = track["box"]
        for seconds, x, y, score in track["points"]:
            records.append(
                {
                    "ID": row["ID"],
                    "SpeciesCode": row["SpeciesCode"],
                    "TimeSeconds": _decimal_text(seconds, SECONDS_DECIMALS),
                    "X": _decimal_text(x, FRACTION_DECIMALS),
                    "Y": _decimal_text(y, FRACTION_DECIMALS),
                    "BoxX": "" if box is None else _decimal_text(x - box["w"] / 2, FRACTION_DECIMALS),
                    "BoxY": "" if box is None else _decimal_text(y - box["h"] / 2, FRACTION_DECIMALS),
                    "BoxW": "" if box is None else _decimal_text(box["w"], FRACTION_DECIMALS),
                    "BoxH": "" if box is None else _decimal_text(box["h"], FRACTION_DECIMALS),
                    "Score": _decimal_text(score, FRACTION_DECIMALS),
                }
            )
    return records


def _readme_text(
    rows: Sequence[Mapping[str, str]],
    screened: Sequence[Mapping[str, str]],
    today: date,
    unlabeled_left_out: int,
    track_rows: Sequence[Mapping[str, str]],
) -> str:
    """Write the README of a package.

    Args:
        rows: The observation rows in the package, all labeled.
        screened: The videos_screened.csv rows in the package.
        today: The export date.
        unlabeled_left_out: How many Later points the package leaves out.
        track_rows: The tracks.csv rows in the package.

    Returns:
        Markdown text with the title, the date, the counts per species, the
        number of videos, the number of Later points left out (when above
        zero), the conventions, and the meaning of every column of the three
        CSV files, with how many track points and tracked sightings the
        package holds.
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
        f"Sponge Lab {VERSION} wrote this package. An annotator logged each sighting by pausing a TCRMP "
        "transect video, marking the sponge, and choosing the species.",
        "",
        "## Contents",
        "",
        f"- `{OBSERVATIONS_FILE}`: one row per sponge sighting ({_plural(len(rows), 'row')}).",
        f"- `{TRACKS_FILE}`: one row per track point of each sighting that has a track "
        f"({_plural(len(track_rows), 'row')} for {_plural(len({row['ID'] for row in track_rows}), 'sighting')}).",
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
    ]
    if unlabeled_left_out:
        lines.append(f"- Unlabeled points left out: {unlabeled_left_out}")
    lines += [
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
    observation_notes = [[f"`{name}`", OBSERVATION_COLUMN_NOTES[name]] for name in EXPORT_COLUMNS]
    track_notes = [[f"`{name}`", TRACK_COLUMN_NOTES[name]] for name in TRACK_COLUMNS]
    screened_notes = [[f"`{name}`", SCREENED_COLUMN_NOTES[name]] for name in SCREENED_COLUMNS]
    lines += _table(["Column", "Meaning"], observation_notes)
    lines += ["", f"## Columns of {TRACKS_FILE}", "", TRACKS_INTRO, ""]
    lines += _table(["Column", "Meaning"], track_notes)
    lines += ["", TRACKS_BOX_NOTE]
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
    unlabeled_left_out: int,
    track_rows: Sequence[Mapping[str, str]],
) -> None:
    """Write the CSV files, the images, and the README into a new package folder.

    Args:
        package: The empty package folder.
        rows: The labeled observation rows.
        screened: The videos_screened.csv rows, with counts that match ``rows``.
        copies: The ``(sighting ID, source, path inside the package)`` triples.
        today: The export date.
        unlabeled_left_out: How many Later points the package leaves out.
        track_rows: The tracks.csv rows, one per track point.

    Raises:
        ExportError: Naming the first file that could not be written, or the
            first image that did not arrive whole.
    """
    try:
        write_csv_atomic(package / OBSERVATIONS_FILE, EXPORT_COLUMNS, rows)
        write_csv_atomic(package / TRACKS_FILE, TRACK_COLUMNS, track_rows)
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
        readme.write_text(_readme_text(rows, screened, today, unlabeled_left_out, track_rows), encoding="utf-8")
    except OSError as error:
        raise ExportError(f"{readme}: could not write the README: {error}") from error


def export_package(store: ObservationStore, export_root: Path, today: date) -> ExportResult:
    """Write a dated export package of the labeled sightings and check that every image arrived.

    Later points are left out, with their images and tracks, and the README
    states how many. The rows, the left-out count, and the images come from
    one read of observations.csv. tracks.csv lists the track points of the
    labeled sightings; it holds only its header when none has a track. A
    track saved for another moment than its row is skipped.

    Args:
        store: The store to export. The export reads it and changes nothing.
        export_root: An existing folder, such as the OnDeck Drive folder. The
            export never creates it, because a missing folder usually means
            the drive is not connected.
        today: The date for the package name and the README.

    Returns:
        An ExportResult. Its ``path`` is the package folder,
        ``<export_root>/spongeGroundTruth_<YYYYMMDD>``, or that name with
        ``_2``, ``_3`` and so on when earlier names are taken. Its
        ``unlabeled_left_out`` is the number of Later points the package leaves out.

    Raises:
        ValueError: Starting with the argument name when an argument has the
            wrong type.
        ExportError: When export_root is not an existing folder, the store
            cannot be parsed, the store has no labeled sightings (the message
            says how many Later points it holds), a referenced image
            is missing or a track file of a labeled sighting cannot be read
            (the message names the first such file and nothing is written),
            or a file cannot be written or does not arrive whole (the partial
            package is removed).
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
        every_row = store.rows()
        screened = store.screened()
    except StoreError as error:
        raise ExportError(f"the store could not be read: {error}") from error
    rows = [row for row in every_row if row[LABEL_STATUS_COLUMN] == LABELED]
    unlabeled_left_out = len(every_row) - len(rows)
    if not rows and unlabeled_left_out:
        raise ExportError(
            f"no sightings to export: {store.observations_path} has {unlabeled_left_out} unlabeled point(s) "
            "and no labeled sightings yet"
        )
    if not rows:
        raise ExportError(f"no sightings to export: {store.observations_path} has no rows yet")
    counts = Counter(row["S3Key"] for row in rows)
    for row in screened:
        row["Sightings"] = str(counts.get(row["S3Key"], 0))
    copies = _image_copies(store, rows)
    for obs_id, source, _inside in copies:
        if not source.is_file():
            raise ExportError(f"missing image: {source} (sighting {obs_id}). Nothing was exported.")
    track_rows = _track_rows(store, rows)
    package = _create_package_folder(root, today)
    try:
        _fill_package(package, rows, screened, copies, today, unlabeled_left_out, track_rows)
    except BaseException:
        shutil.rmtree(package, ignore_errors=True)
        raise
    return ExportResult(package, unlabeled_left_out)
