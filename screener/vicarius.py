"""Where Sponge Screener keeps things when the VICARIUS desktop runs it (``screener.py --vicarius``).

VICARIUS mints run folders under ``<module folder>/inprocess/`` and every run
folder is a screening data folder. The video chunk cache, converted copies
and the bucket listing go to a work folder off the synced drive, named in
``config/vicarius.ini``:

    [vicarius]
    work_root = /mnt/tear/sponge_screener
    # runs_root = <module folder>/inprocess   (optional; this is the default)

The work folder itself is created when missing, but its parent must exist, so
an unmounted disk is refused rather than filled in on the system disk. A work
folder inside the module folder is refused, because the drive sync copies
everything there.
"""

import configparser
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SECTION = "vicarius"
WORK_ROOT_KEY = "work_root"
RUNS_ROOT_KEY = "runs_root"
CACHE_FOLDER = "cache"
RUNS_FOLDER = "inprocess"
CONTROL_CATEGORY = "Cc"


class VicariusConfigError(ValueError):
    """config/vicarius.ini is missing, unreadable, or names a folder the screener must not use."""


@dataclass(frozen=True)
class VicariusPaths:
    """The folders of VICARIUS mode.

    Attributes:
        runs_root: The folder VICARIUS mints run folders in.
        cache_dir: The video chunk cache, converted copies and bucket listing,
            ``<work_root>/cache``.
    """

    runs_root: Path
    cache_dir: Path


def _absolute_folder(raw: Any, key: str, source: Path) -> Path:
    """Check one folder setting from the ini file.

    Args:
        raw: The text from the file.
        key: The setting name, for messages.
        source: The ini file, for messages.

    Returns:
        The folder as an absolute, normalized path.

    Raises:
        VicariusConfigError: When the value is blank, holds a control
            character, or is not an absolute path.
    """
    text = (raw or "").strip()
    if not text:
        raise VicariusConfigError(f"{key}: blank in {source}; give an absolute folder path")
    if any(unicodedata.category(char) == CONTROL_CATEGORY for char in text):
        raise VicariusConfigError(f"{key}: holds a control character in {source}")
    if not os.path.isabs(text):
        raise VicariusConfigError(f"{key}: {text!r} in {source} is not an absolute path")
    return Path(os.path.normpath(text))


def load_vicarius_paths(ini_path: Path, module_dir: Path) -> VicariusPaths:
    """Read config/vicarius.ini and work out the folders of VICARIUS mode.

    Args:
        ini_path: The ini file, normally ``<clone>/config/vicarius.ini``.
        module_dir: The module folder, the parent of the clone, which the
            drive sync copies.

    Returns:
        The runs folder (``runs_root``, default ``<module_dir>/inprocess``)
        and the cache folder (``<work_root>/cache``).

    Raises:
        VicariusConfigError: When the file is missing or unreadable, has no
            ``[vicarius]`` section or ``work_root``, a folder value is blank,
            relative or holds a control character, the work folder sits
            inside the module folder, or the work folder's parent does not
            exist.
    """
    source = Path(ini_path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(source, encoding="utf-8") as handle:
            parser.read_file(handle)
    except FileNotFoundError as error:
        raise VicariusConfigError(f"{source}: missing; it names the work folder for VICARIUS mode") from error
    except (OSError, UnicodeDecodeError, configparser.Error) as error:
        raise VicariusConfigError(f"{source}: unreadable ({type(error).__name__}: {error})") from error
    if not parser.has_section(SECTION):
        raise VicariusConfigError(f"{source}: no [{SECTION}] section")
    settings = parser[SECTION]
    if WORK_ROOT_KEY not in settings:
        raise VicariusConfigError(f"{WORK_ROOT_KEY}: missing from [{SECTION}] in {source}")
    work_root = _absolute_folder(settings.get(WORK_ROOT_KEY), WORK_ROOT_KEY, source)
    module = Path(os.path.abspath(module_dir))
    if work_root == module or module in work_root.parents:
        raise VicariusConfigError(
            f"{WORK_ROOT_KEY}: {work_root} is inside the module folder {module}, which the drive sync copies; "
            "name a folder off the synced drive, such as /mnt/tear/sponge_screener"
        )
    if not work_root.parent.is_dir():
        raise VicariusConfigError(
            f"{WORK_ROOT_KEY}: the parent folder {work_root.parent} of {work_root} does not exist; is that disk mounted?"
        )
    if RUNS_ROOT_KEY in settings:
        runs_root = _absolute_folder(settings.get(RUNS_ROOT_KEY), RUNS_ROOT_KEY, source)
    else:
        runs_root = module / RUNS_FOLDER
    return VicariusPaths(runs_root=runs_root, cache_dir=work_root / CACHE_FOLDER)
