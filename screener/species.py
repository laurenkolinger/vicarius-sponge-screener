"""The species list and the ten pinned number keys.

``config/species.csv`` is the single source of species. Swapping that file
changes what the app can log, so every rule the rest of the app leans on
(code shape, unique codes, unique names, one species per default pin) is
checked here when the file loads.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from screener.names import check_config_text, read_config_table

PIN_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")

CODE_COLUMN = "Code"
NAME_COLUMN = "ScientificName"
PART_COLUMN = "GuidePart"
PIN_COLUMN = "DefaultPin"
SPECIES_COLUMNS = (CODE_COLUMN, NAME_COLUMN, PART_COLUMN, PIN_COLUMN)
SPECIES_LABEL = "species"
CODE_PATTERN = re.compile(r"[A-Z]{4}")


@dataclass(frozen=True)
class Species:
    """One row of species.csv.

    Attributes:
        code: Four upper-case letters, such as "ACAU".
        name: The scientific name, such as "Aplysina cauliformis".
        part: The part of the ID guide that shows the species, or "".
        default_pin: The number key the species starts on ("1" to "9" or
            "0"), or "" when it starts unpinned.
    """

    code: str
    name: str
    part: str
    default_pin: str


def load_species(path: Path) -> List[Species]:
    """Load the species list from species.csv.

    Args:
        path: The CSV file with the columns ``Code``, ``ScientificName``,
            ``GuidePart``, and ``DefaultPin``.

    Returns:
        The species in file order.

    Raises:
        FileNotFoundError: When the file does not exist.
        ValueError: When the file breaks the read_config_table rules or has
            no species rows, a code is not four upper-case letters, a code or
            a name appears twice, a name is blank or unsafe for a CSV cell, a
            default pin is not one of PIN_KEYS, or two species share a
            default pin. The message names the file, the line, and the column.
    """
    loaded: List[Species] = []
    code_lines: Dict[str, int] = {}
    name_lines: Dict[str, int] = {}
    pin_lines: Dict[str, int] = {}
    for line, row in read_config_table(path, SPECIES_COLUMNS, SPECIES_LABEL):
        where = f"{SPECIES_LABEL} file {path} line {line}"
        code = row[CODE_COLUMN]
        if not CODE_PATTERN.fullmatch(code):
            raise ValueError(f"{where}: {CODE_COLUMN} {code!r} must be 4 upper-case letters, A to Z")
        if code in code_lines:
            raise ValueError(f"{where}: {CODE_COLUMN} {code} already appears on line {code_lines[code]}")
        name = check_config_text(row[NAME_COLUMN], f"{where}: {NAME_COLUMN}")
        if not name:
            raise ValueError(f"{where}: {NAME_COLUMN} is blank for {code}")
        if name.casefold() in name_lines:
            raise ValueError(f"{where}: {NAME_COLUMN} {name!r} already appears on line {name_lines[name.casefold()]}")
        part = check_config_text(row[PART_COLUMN], f"{where}: {PART_COLUMN}")
        pin = row[PIN_COLUMN]
        if pin and pin not in PIN_KEYS:
            raise ValueError(f"{where}: {PIN_COLUMN} {pin!r} must be blank or one of {' '.join(PIN_KEYS)}")
        if pin in pin_lines:
            raise ValueError(f"{where}: {PIN_COLUMN} {pin} is already taken on line {pin_lines[pin]}")
        code_lines[code] = line
        name_lines[name.casefold()] = line
        if pin:
            pin_lines[pin] = line
        loaded.append(Species(code=code, name=name, part=part, default_pin=pin))
    if not loaded:
        raise ValueError(f"{SPECIES_LABEL} file {path}: the file has a header and no species rows")
    return loaded


def species_by_code(species: Sequence[Species]) -> Dict[str, Species]:
    """Check a species list and index it by code.

    Args:
        species: The list from load_species, or any list or tuple of Species.

    Returns:
        ``{code: Species}`` in list order.

    Raises:
        ValueError: Starting with ``species`` or ``species[<index>]`` when
            the value is not a list or tuple, an item is not a Species, or a
            code appears twice.
    """
    if not isinstance(species, (list, tuple)):
        raise ValueError(f"species: expected a list of Species, got {type(species).__name__}")
    indexed: Dict[str, Species] = {}
    for index, item in enumerate(species):
        if not isinstance(item, Species):
            raise ValueError(f"species[{index}]: expected a Species, got {type(item).__name__}")
        if item.code in indexed:
            raise ValueError(f"species[{index}]: the code {item.code} appears twice in the list")
        indexed[item.code] = item
    return indexed


def default_pins(species: Sequence[Species]) -> Dict[str, Optional[str]]:
    """Build the starting pins from the DefaultPin column.

    Args:
        species: The list from load_species.

    Returns:
        ``{pin key: species code or None}`` with exactly the ten PIN_KEYS, in
        keyboard order.

    Raises:
        ValueError: When species fails species_by_code, a default_pin is not
            blank or one of PIN_KEYS, or two species claim one key.
    """
    pins: Dict[str, Optional[str]] = {key: None for key in PIN_KEYS}
    for item in species_by_code(species).values():
        if not item.default_pin:
            continue
        if item.default_pin not in pins:
            raise ValueError(
                f"species {item.code}: default_pin {item.default_pin!r} is not one of {' '.join(PIN_KEYS)}"
            )
        if pins[item.default_pin] is not None:
            raise ValueError(
                f"species {item.code}: default_pin {item.default_pin} is already taken by {pins[item.default_pin]}"
            )
        pins[item.default_pin] = item.code
    return pins


def validate_pins(pins: Any, species: Sequence[Species]) -> Dict[str, Optional[str]]:
    """Check a full set of pins sent by the page or read from settings.

    Args:
        pins: ``{pin key: species code or None}`` with exactly the ten PIN_KEYS.
        species: The list from load_species.

    Returns:
        A new dict with the same pins in keyboard order.

    Raises:
        ValueError: Starting with ``pins`` or ``pins.<key>`` and the reason:
            not an object, missing or extra keys, a value that is not a known
            species code or None, or a code pinned to two keys.
    """
    known = species_by_code(species)
    if not isinstance(pins, Mapping):
        raise ValueError(f"pins: expected an object with the keys {' '.join(PIN_KEYS)}, got {type(pins).__name__}")
    missing = [key for key in PIN_KEYS if key not in pins]
    extra = [repr(key) for key in pins if key not in PIN_KEYS]
    if missing or extra:
        raise ValueError(
            f"pins: expected exactly the keys {' '.join(PIN_KEYS)}; "
            f"missing: {' '.join(missing) or 'none'}; not allowed: {' '.join(extra) or 'none'}"
        )
    checked: Dict[str, Optional[str]] = {}
    pinned_at: Dict[str, str] = {}
    for key in PIN_KEYS:
        code = pins[key]
        if code is None:
            checked[key] = None
            continue
        if not isinstance(code, str):
            raise ValueError(f"pins.{key}: expected a species code or null, got {type(code).__name__}")
        if code not in known:
            raise ValueError(f"pins.{key}: unknown species code {code!r}")
        if code in pinned_at:
            raise ValueError(f"pins.{key}: {code} is already pinned to key {pinned_at[code]}")
        checked[key] = code
        pinned_at[code] = key
    return checked
