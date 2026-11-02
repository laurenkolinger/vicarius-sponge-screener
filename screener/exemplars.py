"""The photo that stands for each species in the compare view."""

import json
from pathlib import Path
from typing import Any, Dict

from screener.custom_species import replace_file

EXEMPLARS_FILE = "guide_exemplars.json"


def load_exemplars(data_dir: Path) -> Dict[str, str]:
    """Read the chosen photos.

    Args:
        data_dir: The data folder.

    Returns:
        ``{species code: photo file name}``. A damaged file reads as no choices.
    """
    try:
        stored = json.loads((Path(data_dir) / EXEMPLARS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {code: name for code, name in stored.items() if isinstance(code, str) and isinstance(name, str)}


def choose_exemplar(data_dir: Path, code: Any, file_name: Any) -> None:
    """Save the photo that stands for a species.

    Args:
        data_dir: The data folder.
        code: The species code.
        file_name: A photo the guide lists for that species.

    Raises:
        ValueError: Starting with ``file:`` when the guide does not list that photo.
        OSError: When the file cannot be written.
    """
    try:
        guide = json.loads((Path(data_dir) / "guide" / "guide.json").read_text(encoding="utf-8"))
        images = next(item["images"] for item in guide["species"] if isinstance(item, dict) and item.get("code") == code)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        raise ValueError(f"file: the guide has no photos for {code!r}") from error
    if file_name not in images:
        raise ValueError(f"file: the guide lists no photo {file_name!r} for {code}")
    chosen = load_exemplars(data_dir)
    chosen[code] = file_name
    replace_file(Path(data_dir) / EXEMPLARS_FILE, lambda out: json.dump(chosen, out, indent=1))
