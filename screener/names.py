"""Video file names, site names, and file-safe labels.

A TCRMP video is named ``TCRMP<date>_video_<site>_<transect>.<ext>``. Some
uploads from 2023 and 2024 leave out ``_video``, put a space after it, or spell
it ``vido``, and NAME_PATTERN accepts those forms too. This module reads the
date, the site, and the transect label out of such a name, returns blanks for
every other name, loads the site names from ``config/sites.csv``, and turns any
label into text that is safe inside a file name. It also holds the reader for
the small configuration tables (``sites.csv`` here, ``species.csv`` in
screener.species), so both files follow one set of rules.
"""

import csv
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

MIN_YEAR = 1990
MAX_YEAR = 2100

# The groups are the date, the site code, the transect label, and the
# extension. Every form below comes from a real file name in the bucket.
NAME_PATTERN = re.compile(
    r"^TCRMP(\d{8})"
    # The video token, which one 2024 upload spells "vido", then any mix of
    # spaces and underscores ("_video_FLC", "_video SHR").
    r"(?:_(?:video|vido)[ _]+"
    # Or no token at all ("_BID", "__CSE"). Nothing anchors the site code
    # here, so it must be 3 to 5 characters and start with a letter, which
    # keeps "3D" and "T1" from posing as sites.
    r"|_+(?=[A-Za-z][A-Za-z0-9]{2,4}_))"
    # The token itself is never a site code, in either form.
    r"(?!(?:video|vido)_)"
    r"([A-Za-z0-9]{2,5})_(.+)\.([A-Za-z0-9]+)$",
    re.IGNORECASE | re.ASCII,
)
YEAR_FOLDER_PATTERN = re.compile(r"TCRMP(\d{4})_video", re.IGNORECASE | re.ASCII)
SITE_CODE_PATTERN = re.compile(r"[A-Za-z0-9]{2,5}", re.ASCII)
UNSAFE_FILE_RUN = re.compile(r"[^A-Za-z0-9-]+")

FILE_PART_SEPARATOR = "-"
EMPTY_FILE_PART = "NA"
FORMULA_LEADERS = ("=", "+", "-", "@")
CONTROL_CATEGORY = "Cc"

SITE_CODE_COLUMN = "SiteCode"
SITE_NAME_COLUMN = "SiteName"
SITES_LABEL = "sites"


@dataclass(frozen=True)
class VideoName:
    """What a video's file name says about the video.

    Attributes:
        file_name: The basename of the key.
        date: The eight digit date such as "20241022", or "" when the name is
            outside the TCRMP pattern.
        year: The year from the date; otherwise the year from a
            ``TCRMP2016_video/`` folder in the key; otherwise None.
        site_code: The upper-case site code such as "FLC", or "".
        site_name: The site name such as "Flat Cay"; the site code when
            sites.csv lists no name; "" when the name is outside the pattern.
        transect: The transect label exactly as written, such as "T1",
            "T1-6", "T1+T3-6", "T5.2-6", "BL", or "Other8"; or "".
        standard: True when the name matched the TCRMP pattern.
    """

    file_name: str
    date: str
    year: Optional[int]
    site_code: str
    site_name: str
    transect: str
    standard: bool


def read_config_table(path: Path, required_columns: Sequence[str], label: str) -> List[Tuple[int, Dict[str, str]]]:
    """Read one of the small configuration CSV files into rows.

    The file is UTF-8 text (a byte order mark from Excel is accepted). The
    first non-blank line is the header. Extra columns are allowed, blank lines
    are skipped, every cell is trimmed, and a short row reads as blank cells
    at its end.

    Args:
        path: The CSV file.
        required_columns: Column names the header must contain.
        label: A word for the file in error messages, such as "species".

    Returns:
        One ``(line number, {column: cell})`` pair per data row, in file order.

    Raises:
        FileNotFoundError: When the path is not an existing file.
        ValueError: When the file is not UTF-8 CSV text, is empty, lacks a
            required column, repeats a column, or holds a row with more cells
            than the header. The message names the file and the line.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"{label} file not found: {source}")
    try:
        with open(source, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            records = [(reader.line_num, [cell.strip() for cell in cells]) for cells in reader]
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} file {source}: the file is not UTF-8 text ({error})") from error
    except csv.Error as error:
        raise ValueError(f"{label} file {source}: the file is not readable as CSV ({error})") from error
    records = [(line, cells) for line, cells in records if any(cells)]
    wanted = ",".join(required_columns)
    if not records:
        raise ValueError(f"{label} file {source}: the file is empty; expected the header {wanted}")
    header_line, header = records[0]
    missing = [column for column in required_columns if column not in header]
    if missing:
        raise ValueError(
            f"{label} file {source} line {header_line}: the header lacks {', '.join(missing)}; "
            f"expected {wanted}; found {','.join(header)}"
        )
    repeated = sorted({column for column in header if column and header.count(column) > 1})
    if repeated:
        raise ValueError(
            f"{label} file {source} line {header_line}: the column {', '.join(repeated)} appears twice in the header"
        )
    rows = []
    for line, cells in records[1:]:
        if len(cells) > len(header):
            raise ValueError(
                f"{label} file {source} line {line}: the row has {len(cells)} cells and the header has "
                f"{len(header)}; put quotes around a cell that contains a comma"
            )
        padded = cells + [""] * (len(header) - len(cells))
        rows.append((line, dict(zip(header, padded))))
    return rows


def check_config_text(value: str, where: str) -> str:
    """Check that a name from a configuration file is safe to copy into a CSV cell.

    Args:
        value: The trimmed cell text.
        where: The file, line, and column, used in the error message.

    Returns:
        The value unchanged.

    Raises:
        ValueError: When the text holds a control character or a line break,
            or starts with ``=``, ``+``, ``-``, or ``@`` (a spreadsheet would
            read that cell as a formula).
    """
    if any(unicodedata.category(char) == CONTROL_CATEGORY for char in value):
        raise ValueError(f"{where}: {value!r} contains a control character or a line break")
    if value.startswith(FORMULA_LEADERS):
        raise ValueError(f"{where}: {value!r} starts with {value[0]!r}, which a spreadsheet reads as a formula")
    return value


def load_site_names(path: Path) -> Dict[str, str]:
    """Load the site names from sites.csv.

    Args:
        path: The CSV file with the columns ``SiteCode`` and ``SiteName``.

    Returns:
        ``{site code in upper case: site name}`` for every row that has a
        name. Rows with a blank name are skipped.

    Raises:
        FileNotFoundError: When the file does not exist.
        ValueError: When the file breaks the read_config_table rules, a site
            code is not 2 to 5 letters or digits, a named site code appears
            twice, or a name is unsafe for a CSV cell. The message names the
            file and the line.
    """
    site_names: Dict[str, str] = {}
    first_lines: Dict[str, int] = {}
    for line, row in read_config_table(path, (SITE_CODE_COLUMN, SITE_NAME_COLUMN), SITES_LABEL):
        where = f"{SITES_LABEL} file {path} line {line}"
        code = row[SITE_CODE_COLUMN].upper()
        if not SITE_CODE_PATTERN.fullmatch(code):
            raise ValueError(f"{where}: {SITE_CODE_COLUMN} {row[SITE_CODE_COLUMN]!r} must be 2 to 5 letters or digits")
        name = check_config_text(row[SITE_NAME_COLUMN], f"{where}: {SITE_NAME_COLUMN}")
        if not name:
            continue
        if code in site_names:
            raise ValueError(f"{where}: {SITE_CODE_COLUMN} {code} already has a name on line {first_lines[code]}")
        site_names[code] = name
        first_lines[code] = line
    return site_names


def _real_date(digits: str) -> Optional[date]:
    """Turn eight digits into a calendar date inside the accepted year range.

    Args:
        digits: Eight ASCII digits in the order year, month, day.

    Returns:
        The date, or None when the digits name no real day or the year falls
        outside MIN_YEAR to MAX_YEAR.
    """
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:])
    if not MIN_YEAR <= year <= MAX_YEAR:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year_from_folders(folders: Sequence[str]) -> Optional[int]:
    """Find a survey year in the folder names of a key.

    Args:
        folders: The path segments of the key without the file name.

    Returns:
        The year from the deepest folder named like ``TCRMP2016_video`` whose
        year falls inside MIN_YEAR to MAX_YEAR, or None.
    """
    for folder in reversed(folders):
        match = YEAR_FOLDER_PATTERN.fullmatch(folder)
        if match and MIN_YEAR <= int(match.group(1)) <= MAX_YEAR:
            return int(match.group(1))
    return None


def parse_video_name(key: str, site_names: Mapping[str, str]) -> VideoName:
    """Read the date, site, and transect out of a video's key.

    The file name must match NAME_PATTERN without regard to case, and the date
    must be a real day between MIN_YEAR and MAX_YEAR. The accepted forms are:

    - ``TCRMP<8 digit date>_video_<site>_<transect>.<ext>``, where the site is
      2 to 5 letters or digits. Spaces and underscores in any mix may follow
      ``video``, and ``vido`` is read as ``video``.
    - ``TCRMP<8 digit date>_<site>_<transect>.<ext>`` with no video token,
      where the site is 3 to 5 letters or digits and starts with a letter.

    ``video`` and ``vido`` are never read as a site code. Every other name,
    such as one with a 4 or 6 digit date, counts as outside the pattern and
    gives blanks.

    Args:
        key: The S3 key of the video, or a bare file name. This function
            parses and does not judge safety; screener.keys.validate_key is
            the gate for keys that reach the bucket or the disk.
        site_names: ``{upper-case site code: site name}`` from load_site_names.

    Returns:
        A VideoName. Outer spaces around the transect label are dropped.

    Raises:
        ValueError: When the key is not text or site_names is not a mapping.
    """
    if not isinstance(key, str):
        raise ValueError(f"key: expected text, got {type(key).__name__}")
    if not isinstance(site_names, Mapping):
        raise ValueError(f"site_names: expected a mapping of site code to name, got {type(site_names).__name__}")
    segments = key.split("/")
    file_name = segments[-1]
    match = NAME_PATTERN.fullmatch(file_name)
    day = _real_date(match.group(1)) if match else None
    if match is None or day is None:
        return VideoName(
            file_name=file_name,
            date="",
            year=_year_from_folders(segments[:-1]),
            site_code="",
            site_name="",
            transect="",
            standard=False,
        )
    site_code = match.group(2).upper()
    listed_name = site_names.get(site_code)
    site_name = listed_name.strip() if isinstance(listed_name, str) else ""
    return VideoName(
        file_name=file_name,
        date=match.group(1),
        year=day.year,
        site_code=site_code,
        site_name=site_name or site_code,
        transect=match.group(3).strip(),
        standard=True,
    )


def file_safe(label: str) -> str:
    """Turn a label into text that is safe inside a file name.

    Args:
        label: Any text, such as a transect label or a site code.

    Returns:
        The label with A to Z, a to z, 0 to 9, and ``-`` kept, every other run
        of characters turned into one ``-``, and ``-`` stripped from both
        ends. A label with nothing left gives "NA".

    Raises:
        ValueError: When the label is not text.
    """
    if not isinstance(label, str):
        raise ValueError(f"label: expected text, got {type(label).__name__}")
    cleaned = UNSAFE_FILE_RUN.sub(FILE_PART_SEPARATOR, label).strip(FILE_PART_SEPARATOR)
    return cleaned or EMPTY_FILE_PART
