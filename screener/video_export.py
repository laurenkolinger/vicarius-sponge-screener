"""The background job behind Export: one zip with the package and marked videos.

Rendering takes minutes per video, so it runs apart from the requests. A video whose sightings
have not changed reuses its last render.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

RENDERED_FOLDER = "rendered"
BUILD_FOLDER = "building"
VIDEOS_IN_PACKAGE = "videos_tracked"
ARCHIVE_PREFIX = "spongeGroundTruth_with_videos_"
ERROR_TAIL = 400
HASH_LENGTH = 12


class VideoExporter:
    """Runs one export at a time and reports how far it has come."""

    def __init__(
        self,
        folder: Path,
        python: Path,
        worker: Path,
        collect: Callable[[Optional[List[str]]], List[Dict[str, Any]]],
        build_package: Callable[[Path, Optional[List[str]]], Path],
        zip_package: Callable[[Path], Path],
    ) -> None:
        """Remember the tools. Nothing starts until ``start``.

        Args:
            folder: The folder for renders and the finished zip.
            python: The Python that has OpenCV.
            worker: screener/annotate_worker.py.
            collect: Takes the chosen keys, or None for all, and returns one
                ``{"name", "source", "marks"}`` per chosen video with sightings.
            build_package: Writes the regular export package of the chosen keys into a
                folder and returns it.
            zip_package: Packs a package folder and returns the zip beside it.
        """
        self._folder = Path(folder)
        self._python = Path(python)
        self._worker = Path(worker)
        self._collect = collect
        self._build_package = build_package
        self._zip_package = zip_package
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._stopping = False
        self._status: Dict[str, Any] = {"state": "idle", "run": None, "done": 0, "total": 0, "fraction": 0.0, "message": "", "name": ""}
        self._restore()

    def _restore(self) -> None:
        """Offer a zip left from an earlier run."""
        archives = sorted(self._folder.glob(f"{ARCHIVE_PREFIX}*.zip"), key=lambda item: item.stat().st_mtime) if self._folder.exists() else []
        if archives:
            self._status.update(state="done", name=archives[-1].name)

    def status(self) -> Dict[str, Any]:
        """Return a copy of the job's state: idle, running, done, or failed."""
        with self._lock:
            return dict(self._status)

    def _update(self, **fields: Any) -> None:
        """Change the job's state."""
        with self._lock:
            self._status.update(fields)

    def start(self, keys: Optional[List[str]] = None, label: str = "") -> Dict[str, Any]:
        """Start the job unless it is already running.

        Args:
            keys: The videos to export, by S3 key, or None for every video.
            label: A word for the zip's name, such as a site.

        Returns:
            The state.

        Raises:
            ValueError: Starting with ``videos:`` when the Python with OpenCV is missing.
        """
        with self._lock:
            if self._status["state"] == "running":
                return dict(self._status)
            if not self._python.exists():
                raise ValueError(f"videos: tracking is not set up, so {self._python} is missing")
            self._status = {"state": "running", "run": uuid.uuid4().hex, "done": 0, "total": 0, "fraction": 0.0, "message": "", "name": ""}
        thread = threading.Thread(target=self._run, args=(keys, label), name="video-export", daemon=True)
        thread.start()
        return self.status()

    def archive(self) -> Optional[Path]:
        """Return the finished zip, or None when there is none."""
        with self._lock:
            name = self._status["name"] if self._status["state"] == "done" else ""
        path = self._folder / name if name else None
        return path if path and path.is_file() else None

    def close(self) -> None:
        """Stop a running job."""
        self._stopping = True
        with self._lock:
            process = self._process
        if process is not None:
            process.terminate()

    def _render(self, video: Dict[str, Any], target: Path) -> None:
        """Render one video through the worker.

        Args:
            video: One entry from ``collect``.
            target: The finished file.

        Raises:
            RuntimeError: When the worker fails or the job is stopped.
        """
        partial = target.with_name(f".{target.stem}.part.mp4")
        request = json.dumps({"source": video["source"], "output": str(partial), "marks": video["marks"]})
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                [str(self._python), str(self._worker)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True,
            )
            with self._lock:
                self._process = process
            try:
                process.stdin.write(request)
                process.stdin.close()
            except BrokenPipeError:
                # The worker died before reading; its error output, read below, says why.
                pass
            for line in process.stdout:
                if line.startswith("progress "):
                    self._update(fraction=float(line.split()[1]))
            code = process.wait()
            with self._lock:
                self._process = None
            if self._stopping:
                raise RuntimeError("videos: the app stopped")
            if code != 0:
                errors.seek(0)
                raise RuntimeError(f"{video['name']}: {errors.read().decode('utf-8', 'replace')[-ERROR_TAIL:].strip()}")
        os.replace(partial, target)

    def _run(self, keys: Optional[List[str]], label: str) -> None:
        """Do the whole job, reporting any failure in the state.

        Args:
            keys: The videos to export, or None for every video.
            label: A word for the zip's name, or nothing.
        """
        building = self._folder / BUILD_FOLDER
        rendered = self._folder / RENDERED_FOLDER
        try:
            rendered.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(building, ignore_errors=True)
            building.mkdir(parents=True)
            videos = self._collect(keys)
            self._update(total=len(videos))
            package = self._build_package(building, keys)
            target_folder = package / VIDEOS_IN_PACKAGE
            target_folder.mkdir()
            keep = set()
            stems = {Path(video["name"]).stem for video in videos}
            for index, video in enumerate(videos):
                self._update(done=index, fraction=0.0, message=video["name"])
                # A converted copy has its own frames, so it gets its own render.
                signature = {"marks": video["marks"], "converted": True} if video.get("converted") else video["marks"]
                digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode("utf-8")).hexdigest()[:HASH_LENGTH]
                stem = Path(video["name"]).stem
                render = rendered / f"{stem}_tracked.{digest}.mp4"
                keep.add(render.name)
                if not render.exists():
                    self._render(video, render)
                # Two folders can hold videos with one name, so the number keeps both.
                os.link(render, target_folder / f"{stem}_{index + 1:02d}_tracked.mp4" if (target_folder / f"{stem}_tracked.mp4").exists() else target_folder / f"{stem}_tracked.mp4")
            self._update(done=len(videos), fraction=1.0, message="Zipping")
            built = self._zip_package(package)
            scope = f"{label}_" if label else ""
            archive = self._folder / f"{ARCHIVE_PREFIX}{scope}{built.stem.rsplit('_', 1)[-1]}.zip"
            os.replace(built, archive)
            for older in self._folder.glob(f"{ARCHIVE_PREFIX}*.zip"):
                if older != archive:
                    older.unlink()
            # A part-way export keeps the renders of the videos it left out.
            for stale in rendered.glob("*.mp4"):
                if stale.name not in keep and (keys is None or stale.name.split("_tracked.")[0] in stems):
                    stale.unlink()
            self._update(state="done", name=archive.name, message="")
        except Exception as error:  # noqa: BLE001 - the page shows any failure of the job
            self._update(state="failed", message=str(error))
        finally:
            shutil.rmtree(building, ignore_errors=True)
