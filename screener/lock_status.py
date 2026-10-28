"""Module-lock probe for Sponge Screener, read on every request.

Copied from the VICARIUS reference implementation in reef_point_seg
(``scripts/pipeline_orchestrator/lock_status.py``) with MODULE_SLUG set to
``sponge_screener``. VEGA's backend-key pass is left out: no headless caller
uses the screener's routes, so a lock keeps everyone out.

Lock state lives in one platform registry, ``_METADATA/module_locks.json``
under VICARIUS_ROOT, written only by the VICARIUS desktop (vicarius_ui_os);
this module only reads it. Locked blocks everyone, including an admin
session. Fail-open by design: any problem reading the registry means
unlocked, because a bad state file must not brick the module, and each
distinct problem prints one warning line on stderr.

Standard library only.
"""

import json
import os
import sys
from pathlib import Path
from typing import Set

MODULE_SLUG = "sponge_screener"
LOCK_MESSAGE = "Under development. Please come back later. Email Lauren for questions."
LOCK_CONTACT = "lauren.olinger@uvi.edu"

# The registry path derives from VICARIUS_ROOT (the variable every VICARIUS run
# script honors), so an install elsewhere sets one variable; the literal
# fallback is the lab server's platform root. VICARIUS_LOCKS_PATH, read at call
# time, overrides the whole path for tests.
DEFAULT_VICARIUS_ROOT = "/mnt/rip/vicarius_drive/vicarius"
LOCKS_PATH = os.path.join(
    os.environ.get("VICARIUS_ROOT") or DEFAULT_VICARIUS_ROOT, "_METADATA", "module_locks.json"
)
LOCKS_PATH_VARIABLE = "VICARIUS_LOCKS_PATH"
BYPASS_VARIABLE = "VICARIUS_LOCK_BYPASS"
BYPASS_VALUE = "1"

# Each distinct read problem is reported once, so a bad registry does not
# print a line on every request.
_warned: Set[str] = set()


def locks_path() -> Path:
    """Return the registry file this process reads.

    Returns:
        The path in VICARIUS_LOCKS_PATH when set, otherwise LOCKS_PATH.
    """
    return Path(os.environ.get(LOCKS_PATH_VARIABLE) or LOCKS_PATH)


def _warn_once(message: str) -> None:
    """Print one stderr line for a registry problem the first time it is seen.

    Args:
        message: What went wrong, naming the registry file.
    """
    if message not in _warned:
        _warned.add(message)
        sys.stderr.write(f"[lock_status] warning: {message}; treating {MODULE_SLUG} as unlocked\n")


def is_locked() -> bool:
    """Say whether the platform lock registry marks Sponge Screener locked.

    The registry is read fresh on every call, so a toggle on the VICARIUS
    desktop takes effect on the next request. VICARIUS_LOCK_BYPASS=1 forces
    unlocked for a manual development run.

    Returns:
        True only when the registry holds ``{"locks": {"sponge_screener":
        {"locked": true}}}``. A missing, empty or corrupt registry, a locks
        table or entry of the wrong shape, or a ``locked`` value that is not
        a boolean all mean unlocked, with one warning per distinct problem.
    """
    if os.environ.get(BYPASS_VARIABLE) == BYPASS_VALUE:
        return False
    path = locks_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        locks = data.get("locks") if isinstance(data, dict) else None
        if not isinstance(locks, dict):
            raise ValueError("registry has no 'locks' table")
        entry = locks.get(MODULE_SLUG)
        if entry is None:
            return False
        if not isinstance(entry, dict):
            raise ValueError(f"lock entry for {MODULE_SLUG!r} is not an object")
        locked = entry["locked"]
        if not isinstance(locked, bool):
            raise ValueError(f"'locked' for {MODULE_SLUG!r} is not true or false")
        return locked
    except Exception as error:  # noqa: BLE001 - fail-open is the contract
        _warn_once(f"{path}: {error}")
        return False
