"""The annotator, the pinned species, and the export folder, kept in settings.json.

Loading never stops the app: a missing file gives the defaults, a file that is
not a JSON object is set aside under a dated name, and a field that fails its
check falls back to its own default. Saving checks every field and swaps the
file in one step, so a crash leaves either the old file or the new one.
"""

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from screener.config import DEFAULT_ANNOTATOR, DEFAULT_EXPORT_ROOT
from screener.names import CONTROL_CATEGORY
from screener.species import CODE_PATTERN, PIN_KEYS, Species, default_pins

MAX_ANNOTATOR_LENGTH = 12
ANNOTATOR_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
ANNOTATOR_FIELD = "annotator"
PINS_FIELD = "pins"
EXPORT_ROOT_FIELD = "export_root"
CORRUPT_SUFFIX = ".corrupt-"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
TEMP_SUFFIX = ".tmp"
SETTINGS_FILE_MODE = 0o644
JSON_INDENT = 2


@dataclass
class Settings:
    """What the app remembers between runs.

    Attributes:
        annotator: Initials written into every sighting, 1 to 12 characters.
        pins: ``{pin key: species code or None}`` with the ten PIN_KEYS.
        export_root: The folder that receives export packages.
    """

    annotator: str
    pins: Dict[str, Optional[str]]
    export_root: str


def validate_annotator(value: Any) -> str:
    """Check the annotator initials.

    Args:
        value: The candidate, of any type.

    Returns:
        The value unchanged.

    Raises:
        ValueError: Starting with ``annotator:`` when the value is not text,
            is empty, is longer than 12 characters, or holds a character
            outside A to Z, a to z, 0 to 9, period, underscore, and hyphen.
    """
    if not isinstance(value, str):
        raise ValueError(f"{ANNOTATOR_FIELD}: expected text, got {type(value).__name__}")
    if not value:
        raise ValueError(f"{ANNOTATOR_FIELD}: empty; type 1 to {MAX_ANNOTATOR_LENGTH} characters")
    if len(value) > MAX_ANNOTATOR_LENGTH:
        raise ValueError(f"{ANNOTATOR_FIELD}: longer than {MAX_ANNOTATOR_LENGTH} characters")
    if not ANNOTATOR_PATTERN.fullmatch(value):
        raise ValueError(f"{ANNOTATOR_FIELD}: only letters, digits, period, underscore, and hyphen are allowed")
    return value


def _validate_export_root(value: Any) -> str:
    """Check the export folder text.

    Args:
        value: The candidate, of any type.

    Returns:
        The value unchanged. Whether the folder exists is checked at export time.

    Raises:
        ValueError: Starting with ``export_root:`` when the value is not
            text, is blank, or holds a control character.
    """
    if not isinstance(value, str):
        raise ValueError(f"{EXPORT_ROOT_FIELD}: expected text, got {type(value).__name__}")
    if not value.strip():
        raise ValueError(f"{EXPORT_ROOT_FIELD}: blank")
    if any(unicodedata.category(char) == CONTROL_CATEGORY for char in value):
        raise ValueError(f"{EXPORT_ROOT_FIELD}: contains a control character")
    return value


def _utc_stamp() -> str:
    """Stamp a set-aside file name.

    Returns:
        The current UTC time as text, such as "20260921T153000Z".
    """
    return datetime.now(timezone.utc).strftime(STAMP_FORMAT)


def _set_aside(path: Path) -> Path:
    """Rename a settings file the app cannot read, so the evidence survives.

    Args:
        path: The unreadable settings file.

    Returns:
        The new path, ``<name>.corrupt-<UTC stamp>`` in the same folder, with
        ``-2``, ``-3`` and so on added when that name is taken.

    Raises:
        OSError: When the rename fails.
    """
    base = f"{path.name}{CORRUPT_SUFFIX}{_utc_stamp()}"
    target = path.with_name(base)
    attempt = 2
    while target.exists():
        target = path.with_name(f"{base}-{attempt}")
        attempt += 1
    os.replace(path, target)
    return target


def _pins_from_file(stored: Any, species: Sequence[Species]) -> Dict[str, Optional[str]]:
    """Repair the pins read from settings.json against the current species list.

    Args:
        stored: The ``pins`` value from the file, of any type.
        species: The list from load_species.

    Returns:
        The default pins when ``stored`` is not an object. Otherwise the ten
        PIN_KEYS, each holding the stored code when that code is still in the
        species list and no earlier key holds it, and None otherwise.
    """
    if not isinstance(stored, Mapping):
        return default_pins(species)
    known = {item.code for item in species}
    pins: Dict[str, Optional[str]] = {key: None for key in PIN_KEYS}
    used = set()
    for key in PIN_KEYS:
        code = stored.get(key)
        if isinstance(code, str) and code in known and code not in used:
            pins[key] = code
            used.add(code)
    return pins


def load_settings(path: Path, species: Sequence[Species]) -> Settings:
    """Read settings.json, repairing what it can and never stopping the app.

    Args:
        path: The settings file. It may be missing.
        species: The list from load_species, used for default and stale pins.

    Returns:
        The defaults (DEFAULT_ANNOTATOR, default_pins, DEFAULT_EXPORT_ROOT)
        when the file is missing. The defaults when the file is not a JSON
        object; that file is renamed to ``settings.json.corrupt-<UTC stamp>``.
        Otherwise the stored settings, where a missing or invalid field takes
        its default and a pin whose code left the species list becomes None.

    Raises:
        ValueError: When species is not a list of Species.
        OSError: When the file exists and cannot be read or renamed.
    """
    fallback = Settings(
        annotator=DEFAULT_ANNOTATOR, pins=default_pins(species), export_root=str(DEFAULT_EXPORT_ROOT)
    )
    source = Path(path)
    if not source.exists():
        return fallback
    try:
        stored = json.loads(source.read_bytes().decode("utf-8"))
    except (ValueError, RecursionError):
        stored = None
    if not isinstance(stored, dict):
        _set_aside(source)
        return fallback
    try:
        annotator = validate_annotator(stored.get(ANNOTATOR_FIELD))
    except ValueError:
        annotator = fallback.annotator
    try:
        export_root = _validate_export_root(stored.get(EXPORT_ROOT_FIELD))
    except ValueError:
        export_root = fallback.export_root
    if PINS_FIELD in stored:
        pins = _pins_from_file(stored[PINS_FIELD], species)
    else:
        pins = fallback.pins
    return Settings(annotator=annotator, pins=pins, export_root=export_root)


def _check_pin_shapes(pins: Any) -> Dict[str, Optional[str]]:
    """Check pins before saving, without a species list at hand.

    Args:
        pins: The candidate pins, of any type.

    Returns:
        A new dict with the pins in keyboard order.

    Raises:
        ValueError: Starting with ``pins`` or ``pins.<key>`` when the value
            is not an object with exactly the ten PIN_KEYS, a value is not
            None or four upper-case letters, or a code sits on two keys.
    """
    if not isinstance(pins, Mapping) or set(pins) != set(PIN_KEYS):
        raise ValueError(f"{PINS_FIELD}: expected an object with exactly the keys {' '.join(PIN_KEYS)}")
    checked: Dict[str, Optional[str]] = {}
    pinned_at: Dict[str, str] = {}
    for key in PIN_KEYS:
        code = pins[key]
        if code is not None and not (isinstance(code, str) and CODE_PATTERN.fullmatch(code)):
            raise ValueError(f"{PINS_FIELD}.{key}: expected a 4 letter species code or null, got {code!r}")
        if code is not None and code in pinned_at:
            raise ValueError(f"{PINS_FIELD}.{key}: {code} is already pinned to key {pinned_at[code]}")
        if code is not None:
            pinned_at[code] = key
        checked[key] = code
    return checked


def save_settings(path: Path, settings: Settings) -> None:
    """Write settings.json in one step.

    The JSON goes to a temp file in the same folder, reaches the disk, and
    then replaces the old file through os.replace. The folder is created when
    it is missing.

    Args:
        path: The settings file to write.
        settings: The settings to store.

    Raises:
        ValueError: When settings is not a Settings, or a field fails its
            check (the message starts with the field name). Nothing is written.
        OSError: When the folder or the file cannot be written. The old file
            stays as it was and the temp file is removed.
    """
    if not isinstance(settings, Settings):
        raise ValueError(f"settings: expected a Settings record, got {type(settings).__name__}")
    document = {
        ANNOTATOR_FIELD: validate_annotator(settings.annotator),
        PINS_FIELD: _check_pin_shapes(settings.pins),
        EXPORT_ROOT_FIELD: _validate_export_root(settings.export_root),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}-", suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=JSON_INDENT)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, SETTINGS_FILE_MODE)
        os.replace(temp_name, target)
    except BaseException:
        if os.path.exists(temp_name):
            os.remove(temp_name)
        raise
