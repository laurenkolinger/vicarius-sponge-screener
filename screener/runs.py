"""VICARIUS run folders: find the run a request names, and refuse anything else.

The VICARIUS desktop mints every run folder, ``<module>/inprocess/run_<id>/``
with a ``project.json`` inside, and opens the screener's page with
``?project_dir=<that folder>``. The page then names the run (the folder name)
on every call. This module turns a run name or a project_dir into the run
folder, and refuses names that are not a plain run folder directly under the
runs folder: no traversal, no links that leave it, no folder without a
``project.json``. The screener never creates a run folder.
"""

import os
import re
from pathlib import Path
from typing import Any

PROJECT_FILE = "project.json"
RUN_NAME = re.compile(r"run_[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
NUL = "\x00"


class UnknownRun(LookupError):
    """The run name is well formed, but no such run folder with a project.json exists (HTTP 404)."""


class NoRun(Exception):
    """A data call named no run while the screener keeps its data in VICARIUS run folders (HTTP 409)."""


class RunFolders:
    """The runs folder VICARIUS mints run folders in, and the checks on a run name.

    Attributes:
        root: The runs folder, ``<module>/inprocess``. It may not exist yet;
            VICARIUS creates it with the first run.
    """

    def __init__(self, root: Any) -> None:
        """Remember the runs folder.

        Args:
            root: The runs folder, an absolute path.

        Raises:
            ValueError: Starting with ``runs_root:`` when root is not a path,
                is relative, or holds a NUL character.
        """
        if not isinstance(root, (str, os.PathLike)):
            raise ValueError(f"runs_root: expected a folder path, got {type(root).__name__}")
        text = os.fspath(root)
        if NUL in text:
            raise ValueError("runs_root: holds a NUL character")
        if not os.path.isabs(text):
            raise ValueError(f"runs_root: expected an absolute path, got {text!r}")
        self.root = Path(os.path.abspath(text))

    def folder(self, name: Any) -> Path:
        """Return the run folder for a run name, after every check.

        Args:
            name: The run folder name, such as ``run_26-10-27_LO``.

        Returns:
            ``<root>/<name>``, a real folder (not a link) directly under the
            runs folder that holds a ``project.json`` file.

        Raises:
            ValueError: Starting with ``run:`` when the name is not text, does
                not match ``run_`` plus letters, digits, period, underscore and
                hyphen (160 characters at most after ``run_``), or names a link.
            UnknownRun: When no such folder exists, or it holds no project.json.
        """
        if not isinstance(name, str):
            raise ValueError(f"run: expected a run folder name, got {type(name).__name__}")
        if not RUN_NAME.fullmatch(name):
            raise ValueError(
                f"run: {name[:80]!r} is not a run folder name; expected run_ followed by letters, digits, "
                "period, underscore, or hyphen"
            )
        path = self.root / name
        if path.is_symlink():
            raise ValueError(f"run: {path} is a link; VICARIUS run folders are real folders")
        if not path.is_dir():
            raise UnknownRun(f"run: no run folder {path}; open a run from the Sponge Screener card on the VICARIUS desktop")
        if not (path / PROJECT_FILE).is_file():
            raise UnknownRun(f"run: {path} holds no {PROJECT_FILE}, so it is not a VICARIUS run")
        return path

    def name_for_project_dir(self, project_dir: Any) -> str:
        """Turn the project_dir VICARIUS hands the page into a run name.

        Args:
            project_dir: The run folder's absolute path, as text.

        Returns:
            The run folder name.

        Raises:
            ValueError: Starting with ``project_dir:`` when the value is
                missing, not text, holds a NUL, or is not a folder directly under
                the runs folder; or starting with ``run:`` from folder().
            UnknownRun: From folder().
        """
        if not isinstance(project_dir, str) or not project_dir.strip():
            raise ValueError("project_dir: missing; expected the run folder VICARIUS opened the window with")
        if NUL in project_dir:
            raise ValueError("project_dir: holds a NUL character")
        candidate = Path(os.path.abspath(project_dir.strip()))
        if candidate.parent != self.root:
            raise ValueError(f"project_dir: {candidate} is not a run folder directly under {self.root}")
        self.folder(candidate.name)
        return candidate.name
