"""Helpers shared by the tests that build an ObservationStore on a temp data folder.

The store tests and the track tests bring their own species and sites, so
edits to the shipped config files cannot change what these tests expect.
"""

import fcntl
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from screener.species import Species
from screener.store import ObservationStore

SPECIES = [
    Species(code="ACAU", name="Aplysina cauliformis", part="1", default_pin="1"),
    Species(code="AFUL", name="Aplysina fulva", part="2", default_pin="2"),
    Species(code="CDEL", name="Cliona delitrix", part="1", default_pin="3"),
    Species(code="XMUT", name="Xestospongia muta", part="2", default_pin="8"),
    Species(code="UNKN", name="Unknown sponge", part="", default_pin=""),
]
SITES = {"FLC": "Flat Cay", "BIT": "Buck Island"}

KEY_2024 = "TCRMP_video_ondeck/2024Annual/TCRMP20241022_video_FLC_T1.MP4"
KEY_2024_T2 = "TCRMP_video_ondeck/2024Annual/TCRMP20241022_video_FLC_T2.MP4"

JPEG = b"\xff\xd8\xff\xe0" + b"full frame bytes"
PNG = b"\x89PNG\r\n\x1a\n" + b"crop bytes"
START = datetime(2026, 9, 21, 15, 30, 0, tzinfo=timezone.utc)


class FixedClock:
    """A clock the test moves by hand."""

    def __init__(self, moment=START):
        self.moment = moment

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        """Move the clock forward and return the new stamp text."""
        self.moment = self.moment + timedelta(seconds=seconds)
        return self.moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_store(tmp_path: Path, clock=None, species=SPECIES, sites=SITES) -> ObservationStore:
    """Build a store on a temp data folder."""
    if clock is None:
        return ObservationStore(tmp_path / "data", species, sites)
    return ObservationStore(tmp_path / "data", species, sites, clock)


def sighting(**changes: Any) -> Dict[str, Any]:
    """Return valid keyword arguments for ObservationStore.add, with changes applied."""
    fields = {
        "key": KEY_2024,
        "time_seconds": 83.72,
        "point": {"x": 0.25, "y": 0.75},
        "box": None,
        "species_code": "ACAU",
        "note": "",
        "annotator": "LO",
        "frame_jpeg": JPEG,
        "crop_png": PNG,
    }
    fields.update(changes)
    return fields


def add_sighting(store: ObservationStore, **changes: Any) -> Dict[str, str]:
    """Add one sighting with the keyword arguments of sighting() and return its row."""
    return store.add(**sighting(**changes))


def identity_of(row: Mapping[str, str]) -> Tuple[str, str, float, Tuple[float, float]]:
    """Return the first four arguments of TrackStore.save for a row, as the background job passes them.

    Args:
        row: A sighting row.

    Returns:
        ``(ID, S3Key, TimestampSeconds, (PointX, PointY))`` with the numbers as floats.
    """
    return (
        row["ID"],
        row["S3Key"],
        float(row["TimestampSeconds"]),
        (float(row["PointX"]), float(row["PointY"])),
    )


def files_in(folder: Path) -> List[str]:
    """Return the sorted file names in a folder, or an empty list when it is missing."""
    return sorted(item.name for item in folder.iterdir()) if folder.exists() else []


def hold_lock(data_dir: Path) -> int:
    """Take the store's write lock from outside the store and return the descriptor."""
    descriptor = os.open(str(data_dir / ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def release_lock(descriptor: int) -> None:
    """Release a lock taken by hold_lock."""
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)
