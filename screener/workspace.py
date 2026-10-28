"""One screening dataset: its data folder, its store, its settings, and its tracking queue.

Standalone, the screener has one workspace, the ``--data-dir`` folder. In
VICARIUS mode every run folder is a workspace of its own, opened the first
time a request names that run, so two runs never share a sighting, an ID
counter, a settings file, or a tracking queue.
"""

import threading
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence

from screener import runstate
from screener.settings import Settings, load_settings, save_settings, validate_annotator
from screener.species import PIN_KEYS, Species, validate_pins
from screener.store import ObservationStore
from screener.tracking import Tracker

SETTINGS_FILE = "settings.json"
SETTINGS_FIELDS = ("annotator", "pins")
EXPORTS_FOLDER = "exports"
NO_ANNOTATOR = "annotator: empty; type your initials in the Annotator field first"

TrackerFactory = Callable[[ObservationStore], Tracker]


class Workspace:
    """The store, settings, and tracker of one data folder.

    Attributes:
        name: The VICARIUS run folder name, or None for the standalone data folder.
        data_dir: The folder with observations.csv, videos_screened.csv,
            frames/, crops/, tracks/, trash/, .lock, and settings.json.
        project_file: The run's project.json, or None standalone.
        store: The ObservationStore of data_dir.
        tracker: The tracking queue that writes into this store's tracks/.
    """

    def __init__(
        self,
        data_dir: Path,
        species: Sequence[Species],
        site_names: Mapping[str, str],
        make_tracker: TrackerFactory,
        default_export_root: Optional[Path] = None,
        name: Optional[str] = None,
        project_file: Optional[Path] = None,
    ) -> None:
        """Open the data folder, read its settings, and start its tracker.

        Args:
            data_dir: The data folder. It is created when missing.
            species: The list from load_species.
            site_names: ``{site code: site name}``.
            make_tracker: Builds the tracker for a store.
            default_export_root: The export folder when settings.json names
                none; None keeps the app default.
            name: The run folder name, or None standalone.
            project_file: The run's project.json, or None standalone.

        Raises:
            ValueError, StoreError: From ObservationStore.
            OSError: When settings.json exists and cannot be read.
        """
        self.name = name
        self.data_dir = Path(data_dir)
        self.project_file = project_file
        self._species = list(species)
        self.store = ObservationStore(self.data_dir, self._species, site_names)
        self._settings_path = self.data_dir / SETTINGS_FILE
        self._settings_lock = threading.Lock()
        self._settings = load_settings(self._settings_path, self._species, default_export_root)
        self.tracker = make_tracker(self.store)

    def close(self) -> None:
        """Stop this workspace's tracker."""
        self.tracker.close()

    # ----- settings -----

    def settings(self) -> Settings:
        """Return a copy of the current settings."""
        with self._settings_lock:
            return Settings(self._settings.annotator, dict(self._settings.pins), self._settings.export_root)

    def annotator(self) -> str:
        """Return the annotator initials, refusing when nobody has typed any.

        Raises:
            ValueError: NO_ANNOTATOR when settings.json holds no initials.
        """
        current = self.settings().annotator
        if not current:
            raise ValueError(NO_ANNOTATOR)
        return current

    def pinned_codes(self) -> List[str]:
        """Return the species codes on the number keys, in keyboard order."""
        pins = self.settings().pins
        return [pins[key] for key in PIN_KEYS if pins[key] is not None]

    def update_settings(self, fields: Any) -> None:
        """Change the annotator or the pins and save settings.json.

        Args:
            fields: A JSON object with ``annotator`` and/or ``pins``.

        Raises:
            ValueError: Starting with ``settings``, ``annotator``, ``pins``,
                or ``pins.<key>`` when the object has other fields, no field,
                or a field that fails its check. Nothing is saved.
            OSError: When settings.json cannot be written.
        """
        if not isinstance(fields, Mapping):
            raise ValueError(f"settings: expected a JSON object, got {type(fields).__name__}")
        unknown = [name for name in fields if name not in SETTINGS_FIELDS]
        if unknown:
            raise ValueError(f"settings: unknown field {unknown[0]!r}; expected {' or '.join(SETTINGS_FIELDS)}")
        if not fields:
            raise ValueError(f"settings: nothing to change; send {' or '.join(SETTINGS_FIELDS)}")
        with self._settings_lock:
            annotator = self._settings.annotator
            pins = dict(self._settings.pins)
            if "annotator" in fields:
                annotator = validate_annotator(fields["annotator"])
            if "pins" in fields:
                pins = validate_pins(fields["pins"], self._species)
            changed = Settings(annotator, pins, self._settings.export_root)
            save_settings(self._settings_path, changed)
            self._settings = changed

    # ----- run bookkeeping -----

    def note_screening(self) -> None:
        """Mark the run's Screen videos step running in project.json (standalone: nothing)."""
        if self.project_file is not None:
            runstate.mark_step(self.project_file, runstate.SCREEN_STEP, runstate.SCREEN_STEP_NAME, runstate.RUNNING)

    def note_export(self, package: str, rows: int) -> None:
        """Mark the run's export step done and log the export in project.json (standalone: nothing).

        Args:
            package: The package folder name.
            rows: How many sightings it holds.
        """
        if self.project_file is not None:
            runstate.record_export(self.project_file, self.settings().annotator, package, rows)
