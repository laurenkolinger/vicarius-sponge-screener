"""Species and genera the annotator adds while screening.

They live in ``data/custom_species.csv``, so they survive an upgrade of the app, and
they join the species list with no guide part, which colors them as Phase 3.
"""

import csv
import json
import os
import re
import shutil
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

from screener.species import CODE_PATTERN, Species

CUSTOM_FILE = "custom_species.csv"
COLUMNS = ("Code", "ScientificName")
MAX_NAME_LENGTH = 80
# A genus, or a genus and one or two more words, such as "Cinachyrella sp." or "Cinachyrella apion".
NAME_PATTERN = re.compile(r"[A-Z][a-z-]{2,}(?: (?:spp?\.|[a-z][a-z-]+)){0,2}")
CODE_LENGTH = 4


def clean_name(value: Any) -> str:
    """Tidy and check a typed name.

    Args:
        value: The candidate.

    Returns:
        The name with single spaces and a capital first letter only.

    Raises:
        ValueError: Starting with ``name:`` for anything but a genus or species name.
    """
    if not isinstance(value, str):
        raise ValueError(f"name: expected text, got {type(value).__name__}")
    words = value.split()
    name = " ".join(words).capitalize()
    if len(name) > MAX_NAME_LENGTH or not NAME_PATTERN.fullmatch(name):
        raise ValueError(f"name: expected a genus or species name such as Cinachyrella, got {value!r}")
    return name


def new_code(name: str, taken: Sequence[str]) -> str:
    """Make a four-letter code that no other species has, in the style of the species list.

    Args:
        name: The cleaned name.
        taken: The codes in use.

    Returns:
        The first four letters of a genus (CINA), or the genus initial and three epithet
        letters of a species (CAPI). The last letter changes when the code is taken.

    Raises:
        ValueError: Starting with ``name:`` when every variation is taken.
    """
    words = [re.sub(r"[^A-Za-z]", "", word).upper() for word in name.split()]
    binomial = len(words) > 1 and words[1] not in ("SP", "SPP")
    stem = (words[0][0] + words[1] if binomial else words[0]).ljust(CODE_LENGTH, "X")
    for candidate in [stem[:CODE_LENGTH]] + [stem[: CODE_LENGTH - 1] + letter for letter in string.ascii_uppercase]:
        if candidate not in taken:
            return candidate
    raise ValueError(f"name: every code for {name} is taken")


def load_custom_species(data_dir: Path, taken: Sequence[str]) -> List[Species]:
    """Read the added species. A row that cannot be used is reported and skipped.

    A file that cannot be read at all moves aside, so the app still starts and the
    next added species cannot overwrite what the file held.

    Args:
        data_dir: The data folder.
        taken: The codes in the built-in species list.

    Returns:
        The added species, in file order.
    """
    path = Path(data_dir) / CUSTOM_FILE
    if not path.exists():
        return []
    seen = set(taken)
    loaded: List[Species] = []
    skipped = False
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not set(COLUMNS) <= set(reader.fieldnames or []):
                raise ValueError(f"expected the columns {COLUMNS}")
            for row in reader:
                code, name = row[COLUMNS[0]] or "", row[COLUMNS[1]] or ""
                if code in seen or not CODE_PATTERN.fullmatch(code) or not name:
                    sys.stderr.write(f"{path}: skipped the row {code!r}, {name!r}\n")
                    skipped = True
                    continue
                seen.add(code)
                loaded.append(Species(code=code, name=name, part="", default_pin=""))
    except (OSError, ValueError, csv.Error) as error:
        aside = path.with_name(f"{path.name}.damaged-{int(time.time())}")
        try:
            os.replace(path, aside)
        except OSError as move_error:
            sys.stderr.write(f"{path}: could not be moved aside: {move_error}\n")
        sys.stderr.write(f"{path}: could not be read, moved to {aside.name}: {error}\n")
        return []
    if skipped:
        # The next added species rewrites the file without those rows, so keep a copy.
        try:
            shutil.copy2(path, path.with_name(f"{path.name}.before-skip-{int(time.time())}"))
        except OSError as copy_error:
            sys.stderr.write(f"{path}: could not keep a copy: {copy_error}\n")
    return loaded


def replace_file(path: Path, write: Any) -> None:
    """Write a file through a temporary file, so a reader always sees a whole one.

    Args:
        path: The file.
        write: Called with the open text file.
    """
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
            write(out)
        os.replace(temp_name, path)
    except OSError:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise


def save_custom_species(data_dir: Path, added: Sequence[Species]) -> None:
    """Save every added species.

    Args:
        data_dir: The data folder.
        added: The added species.

    Raises:
        OSError: When the file cannot be written.
    """

    def write(out: Any) -> None:
        """Write the table."""
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows((item.code, item.name) for item in added)

    replace_file(Path(data_dir) / CUSTOM_FILE, write)


def add_to_guide(data_dir: Path, species: Species) -> None:
    """Give an added species its entry in the guide, with no description or photos yet.

    Args:
        data_dir: The data folder.
        species: The added species.

    Raises:
        OSError: When the guide cannot be written.
        ValueError: When the guide file holds no valid JSON.
    """
    path = Path(data_dir) / "guide" / "guide.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    guide: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"species": []}
    if not isinstance(guide, dict) or not isinstance(guide.get("species"), list):
        raise ValueError(f"guide: {path} holds no species list")
    entries = [item for item in guide["species"] if isinstance(item, dict) and item.get("code") == species.code]
    if entries and entries[0].get("name") == species.name:
        return
    if entries:
        # A save that failed earlier left this code on another name.
        entries[0]["name"] = species.name
    else:
        guide["species"].append(
            {"code": species.code, "name": species.name, "part": "", "description": "", "source": "", "viewer_name": "", "images": []}
        )
    replace_file(path, lambda out: out.write(json.dumps(guide, indent=1, ensure_ascii=False) + "\n"))
