"""Keep a VICARIUS run's project.json current as screening proceeds.

VICARIUS mints ``project.json`` in every run folder with ``id``, ``name``,
``created_at``, ``updated_at``, ``project_dir``, ``repo_dir``,
``current_step``, ``steps`` and ``access_log``. The screener fills two steps:
step 1, Screen videos, is ``running`` once a video is opened; step 2, Export
the package, is ``done`` after an export, and the export also appends an
``access_log`` row. Every other field is kept as it is.

A write reads the file, changes the parsed object, writes a temp file in the
same folder and swaps it in with ``os.replace``, so a crash leaves the old
file or the new one. A file that is missing or cannot be parsed is reported
in one stderr line and left alone: run bookkeeping never stops a sighting or
an export.
"""

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

AST = timezone(timedelta(hours=-4))
SCREEN_STEP = 1
SCREEN_STEP_NAME = "Screen videos"
EXPORT_STEP = 2
EXPORT_STEP_NAME = "Export the package"
RUNNING = "running"
DONE = "done"
JSON_INDENT = 2
LOG_PREFIX = "runstate:"
FILE_MODE = 0o644

# One lock per process: the screener's own threads never interleave a
# read-change-write. VICARIUS writes the same file only when a person opens or
# creates the run, before the page starts calling.
_write_lock = threading.Lock()


def ast_now() -> str:
    """Return the current time in Atlantic Standard Time, ISO 8601 with the offset, to the second."""
    return datetime.now(AST).isoformat(timespec="seconds")


def _report(path: Path, problem: str) -> None:
    """Write one stderr line about a project.json the screener could not update.

    Args:
        path: The project.json file.
        problem: What went wrong.
    """
    sys.stderr.write(f"{LOG_PREFIX} {path}: {problem}; the run's progress record was not updated\n")


def _read(path: Path) -> Optional[Dict[str, Any]]:
    """Read project.json as a JSON object.

    Args:
        path: The project.json file.

    Returns:
        The parsed object, or None (after one stderr line) when the file is
        missing, unreadable, not JSON, or not an object.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _report(path, "missing")
        return None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as error:
        _report(path, f"unreadable ({type(error).__name__}: {error})")
        return None
    if not isinstance(document, dict):
        _report(path, f"holds a {type(document).__name__}, not an object")
        return None
    return document


def _write(path: Path, document: Dict[str, Any]) -> bool:
    """Write project.json through a temp file and os.replace.

    Args:
        path: The project.json file.
        document: The object to write.

    Returns:
        True when the new file is in place; False (after one stderr line) when
        the write failed, in which case the old file is untouched.
    """
    try:
        descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp")
    except OSError as error:
        _report(path, f"could not create a temp file ({error})")
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=JSON_INDENT)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, FILE_MODE)
        os.replace(temp_name, path)
        return True
    except OSError as error:
        _report(path, f"could not write ({error})")
        return False
    finally:
        if os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError as error:
                _report(path, f"left the temp file {temp_name} behind ({error})")


def _update(path: Path, change: Callable[[Dict[str, Any]], bool], now: str) -> bool:
    """Apply one change to project.json under the process lock.

    Args:
        path: The project.json file.
        change: Changes the parsed object in place and returns True when it
            changed anything; nothing is written when it returns False.
        now: The AST timestamp written to ``updated_at``.

    Returns:
        True when the file was rewritten.
    """
    with _write_lock:
        document = _read(path)
        if document is None or not change(document):
            return False
        document["updated_at"] = now
        return _write(path, document)


def _steps(document: Dict[str, Any]) -> Dict[str, Any]:
    """Return the ``steps`` object of a project, replacing a value of the wrong type with {}.

    Args:
        document: The parsed project.json.

    Returns:
        The steps object, stored back on the document.
    """
    steps = document.get("steps")
    if not isinstance(steps, dict):
        steps = {}
        document["steps"] = steps
    return steps


def mark_step(path: Path, number: int, name: str, status: str, now: Optional[str] = None) -> bool:
    """Set one step's name and status, writing only when that changes something.

    Args:
        path: The run's project.json.
        number: The step number, 1 or more; stored as a text key as VICARIUS does.
        name: The step name shown by VICARIUS.
        status: ``running`` or ``done``.
        now: The AST timestamp for ``updated_at``; the current time when None.

    Returns:
        True when the file was rewritten.

    Raises:
        ValueError: When number is not a whole number of 1 or more, or name or
            status is blank.
    """
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"number: expected a step number of 1 or more, got {number!r}")
    for label, value in (("name", name), ("status", status)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}: expected text, got {value!r}")
    wanted = {"name": name, "status": status}

    def change(document: Dict[str, Any]) -> bool:
        steps = _steps(document)
        if steps.get(str(number)) == wanted:
            return False
        steps[str(number)] = wanted
        return True

    return _update(Path(path), change, now or ast_now())


def record_export(path: Path, initials: str, package: str, rows: int, now: Optional[str] = None) -> bool:
    """Mark the export step done and add an access_log row for the export.

    Args:
        path: The run's project.json.
        initials: The annotator's initials; "" leaves the access_log row out.
        package: The package folder name, such as ``spongeGroundTruth_20261027``.
        rows: How many sightings the package holds.
        now: The AST timestamp; the current time when None.

    Returns:
        True when the file was rewritten.
    """
    stamp = now or ast_now()
    noun = "sighting" if rows == 1 else "sightings"

    def change(document: Dict[str, Any]) -> bool:
        _steps(document)[str(EXPORT_STEP)] = {"name": EXPORT_STEP_NAME, "status": DONE}
        if initials:
            log = document.get("access_log")
            if not isinstance(log, list):
                log = []
                document["access_log"] = log
            log.append({"ts": stamp, "initials": initials, "action": "export", "purpose": f"Exported {package} with {rows} {noun}"})
        return True

    return _update(Path(path), change, stamp)
