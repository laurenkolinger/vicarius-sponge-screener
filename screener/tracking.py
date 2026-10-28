"""Background tracking: each sighting followed through its video by the OpenCV worker.

The server hands this module two kinds of work. A new sighting goes ahead of
everything else that waits. When the page asks for the tracks of a video, the
rows of that video without a track wait behind the new sightings and ahead of
the rows of videos asked for earlier. One worker thread runs one job at a
time: it starts screener/track_worker.py under the Python of .venv-track (the
only Python with OpenCV), sends the job as JSON on stdin, waits for the answer
with a timeout, checks it with tracks.validate_track, and saves it with the
save function the server passes (TrackStore.save). A sighting that was deleted
while it was tracked, or whose ID went to the next sighting (the save checks
the ID, the video, the time, and the point), is dropped with one log line and
never tried again. A failed job waits TRACK_RETRY_SECONDS
before a request queues it again.

Without that Python, or without the worker script, tracking is unavailable:
nothing is queued, and the page draws its marks where they were clicked, as
before. One line on stderr per finished job, and one at startup when tracking
is off, say what happened.
"""

import collections
import itertools
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Tuple

from screener.config import TRACK_RETRY_SECONDS, TRACK_TIMEOUT_SECONDS
from screener.store import StoreError, UnknownObservation
from screener.tracks import TrackError, validate_track

STATE_NONE = "none"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_UNAVAILABLE = "unavailable"

# How track_worker.py says each direction of a track ended.
WORKER_ENDS = ("exit", "frames", "limit", "lost")
DIRECTIONS = ("back", "forward")
BOX_COLUMNS = ("BoxX", "BoxY", "BoxW", "BoxH")
BOX_FIELDS = ("x", "y", "w", "h")
STDERR_TAIL_CHARACTERS = 400
LOG_PREFIX = "tracks:"
CLOSED_REASON = "tracking stopped because the server is closing"

SaveTrack = Callable[[str, str, float, Tuple[float, float], Dict[str, Any]], Dict[str, Any]]


class TrackingError(Exception):
    """A job that failed, with the reason: the worker failed, timed out, or answered something unusable."""


class _Stopped(Exception):
    """The running job was canceled or the tracker closed while the worker ran."""


@dataclass(frozen=True)
class TrackJob:
    """One sighting to follow through its video.

    Attributes:
        obs_id: The sighting ID, such as "ID007".
        key: The S3 key of its video.
        time: Its TimestampSeconds.
        point: ``{"x", "y"}`` in frame fractions.
        box: ``{"x", "y", "w", "h"}`` in frame fractions, or None for a point sighting.
    """

    obs_id: str
    key: str
    time: float
    point: Dict[str, float]
    box: Optional[Dict[str, float]]

    def same_sighting(self, other: Optional["TrackJob"]) -> bool:
        """Tell whether another job follows this very sighting: same ID, video, time, and point.

        An ID goes to the next sighting after the highest one is deleted, and
        a sponge marked again on the same paused frame keeps the time, so only
        all four together name a sighting.
        """
        return other is not None and (other.obs_id, other.key, other.time, other.point) == (
            self.obs_id, self.key, self.time, self.point
        )


@dataclass(frozen=True)
class TrackStatus:
    """Where one sighting's tracking stands.

    Attributes:
        state: "none", "queued", "running", "done", "failed", or "unavailable".
        message: Why the job failed or why tracking is unavailable, else "".
    """

    state: str
    message: str


def _cell_number(row: Mapping[str, str], column: str) -> float:
    """Read one number cell of an observation row.

    Raises:
        ValueError: Starting with the column name when the cell is missing,
            not a number, or not finite.
    """
    text = row.get(column)
    try:
        value = float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{column}: expected a number, got {text!r}") from error
    if not math.isfinite(value):
        raise ValueError(f"{column}: expected a finite number, got {text!r}")
    return value


def job_from_row(row: Mapping[str, str]) -> TrackJob:
    """Read the job of an observation row, as the store returns it (every cell text).

    Args:
        row: A row with ID, S3Key, TimestampSeconds, PointX, PointY, and the
            box columns (blank for a point sighting).

    Returns:
        The job.

    Raises:
        ValueError: Starting with the column name when ID or S3Key is blank or
            a number cell cannot be read. A box with any cell filled must have
            all four.
    """
    for column in ("ID", "S3Key"):
        if not row.get(column):
            raise ValueError(f"{column}: empty")
    box = None
    if any(row.get(column) for column in BOX_COLUMNS):
        box = {field: _cell_number(row, column) for field, column in zip(BOX_FIELDS, BOX_COLUMNS)}
    return TrackJob(
        obs_id=row["ID"],
        key=row["S3Key"],
        time=_cell_number(row, "TimestampSeconds"),
        point={"x": _cell_number(row, "PointX"), "y": _cell_number(row, "PointY")},
        box=box,
    )


def _tail(text: str) -> str:
    """Return the last 400 characters of a program's error text, or a note when it wrote none."""
    stripped = text.strip()
    return stripped[-STDERR_TAIL_CHARACTERS:] if stripped else "the program wrote no error text"


def _signal_group(process: "subprocess.Popen") -> None:
    """Kill a worker and every process it started (ffprobe, ffmpeg); its pid names its process group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def checked_answer(text: str) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Check what the worker printed.

    Args:
        text: The worker's stdout.

    Returns:
        The track as validate_track returns it (``{"points", "box"}``), and the
        ends ``{"back", "forward"}``.

    Raises:
        TrackingError: Starting with ``answer``, the validate_track field name
            (``points[3].t: ...``, ``box: missing``), or ``ends`` when the text
            is not a JSON object, the track breaks the track rules, or the ends
            are not two of exit, frames, limit, and lost.
    """
    try:
        answer = json.loads(text)
    except ValueError as error:
        raise TrackingError(f"answer: not JSON ({error})") from error
    if not isinstance(answer, dict):
        raise TrackingError(f"answer: expected a JSON object, got {type(answer).__name__}")
    try:
        track = validate_track(answer)
    except ValueError as error:
        raise TrackingError(str(error)) from error
    ends = answer.get("ends")
    if not isinstance(ends, dict):
        raise TrackingError(f"ends: expected an object with back and forward, got {type(ends).__name__}")
    for direction in DIRECTIONS:
        if ends.get(direction) not in WORKER_ENDS:
            raise TrackingError(f"ends.{direction}: expected one of {', '.join(WORKER_ENDS)}, got {ends.get(direction)!r}")
    return track, {direction: ends[direction] for direction in DIRECTIONS}


def _log(obs_id: str, text: str) -> None:
    """Write one line about a job to stderr, where the server logs its requests."""
    sys.stderr.write(f"{LOG_PREFIX} {obs_id} {text}\n")


class Tracker:
    """The queue of tracking jobs, run one at a time by one worker thread.

    add, request, cancel, pending, and status may be called from many server
    threads at once. A lock guards the shared state, and it is never held
    while the worker runs.
    """

    def __init__(
        self,
        python: Path,
        worker: Path,
        source_for: Callable[[str], str],
        save_track: SaveTrack,
        timeout: float = TRACK_TIMEOUT_SECONDS,
        retry_seconds: float = TRACK_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Check the worker's Python and script, and start the worker thread when both are there.

        Args:
            python: The Python of .venv-track.
            worker: screener/track_worker.py.
            source_for: Given a video key, returns what ffmpeg reads: the
                converted copy or the relay URL in the app, anything in tests.
            save_track: TrackStore.save, or a stand-in in tests, called as
                ``(obs_id, key, anchor_time, (x, y), track)``.
            timeout: Seconds a worker may run before it is stopped.
            retry_seconds: Seconds a failed job waits before a request queues it again.
            clock: The monotonic clock for the retry wait.

        Raises:
            ValueError: Starting with the argument name when an argument has
                the wrong type or value.
        """
        for name, value in (("python", python), ("worker", worker)):
            if not isinstance(value, os.PathLike):
                raise ValueError(f"{name}: expected a path, got {type(value).__name__}")
        for name, value in (("source_for", source_for), ("save_track", save_track), ("clock", clock)):
            if not callable(value):
                raise ValueError(f"{name}: expected a function, got {type(value).__name__}")
        for name, value, lowest in (("timeout", timeout, 0.0), ("retry_seconds", retry_seconds, 0.0)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < lowest:
                raise ValueError(f"{name}: expected a finite number of seconds, got {value!r}")
        if timeout == 0:
            raise ValueError("timeout: expected a number of seconds above 0")
        self._python = Path(python)
        self._worker = Path(worker)
        self._source_for = source_for
        self._save_track = save_track
        self._timeout = float(timeout)
        self._retry_seconds = float(retry_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._queued: Dict[str, TrackJob] = {}
        self._fresh: Deque[str] = collections.deque()
        self._backlog: Deque[str] = collections.deque()
        self._running: Optional[TrackJob] = None
        self._process: Optional["subprocess.Popen"] = None
        self._stopped = False
        self._failed: Dict[str, Tuple[TrackJob, float, str]] = {}
        self._done: Dict[str, TrackJob] = {}
        self._dropped: Dict[str, TrackJob] = {}
        self._closed = False
        self._reason = self._unavailable_reason()
        self._thread: Optional[threading.Thread] = None
        if self._reason:
            sys.stderr.write(f"{LOG_PREFIX} {self._reason}\n")
            return
        self._thread = threading.Thread(target=self._work, name="screener-track", daemon=True)
        self._thread.start()

    def _unavailable_reason(self) -> str:
        """Say why tracking cannot run here, or return "" when it can."""
        if not self._python.is_file() or not os.access(str(self._python), os.X_OK):
            return f"tracking is off: {self._python} is missing or cannot run (the README section Resume from cold sets it up)"
        if not self._worker.is_file():
            return f"tracking is off: the worker {self._worker} is missing"
        return ""

    @property
    def available(self) -> bool:
        """True while jobs can run: the venv and the worker are there and the tracker is open."""
        with self._lock:
            return self._reason == ""

    @property
    def reason(self) -> str:
        """Why tracking is unavailable, or "" while it is available."""
        with self._lock:
            return self._reason

    def add(self, row: Mapping[str, str]) -> TrackStatus:
        """Queue a new sighting ahead of every row waiting from an opened video.

        Args:
            row: The saved observation row.

        Returns:
            The sighting's status after the call: "queued", or "running" or
            "queued" unchanged for a sighting already waiting, "unavailable"
            with the reason, or "failed" with the column when the row cannot
            be read. It never raises for the row, so a save never fails here.
        """
        try:
            job = job_from_row(row)
        except ValueError as error:
            return TrackStatus(STATE_FAILED, str(error))
        with self._wake:
            if self._reason:
                return TrackStatus(STATE_UNAVAILABLE, self._reason)
            if not self._waiting_locked(job):
                self._forget_locked(job.obs_id)
                self._queued[job.obs_id] = job
                self._fresh.append(job.obs_id)
                self._wake.notify()
            return self._status_locked(job.obs_id)

    def request(self, key: str, rows: List[Mapping[str, str]]) -> List[str]:
        """Queue the rows of one video that have no track, ahead of the other videos' rows.

        A row is left out when it waits already, when its track was saved or
        its sighting dropped in this run of the server, when its last try
        failed less than retry_seconds ago, when it belongs to another video,
        or when it cannot be read.

        Args:
            key: The video's key.
            rows: Its rows without a track.

        Returns:
            The IDs of that video's sightings that are running or waiting, the
            running one first: what the page still waits for.
        """
        jobs = []
        for row in rows:
            try:
                job = job_from_row(row)
            except ValueError:
                continue
            if job.key == key:
                jobs.append(job)
        with self._wake:
            if self._reason:
                return []
            now = self._clock()
            for job in jobs:
                if self._waiting_locked(job) or self._settled_locked(job, now):
                    continue
                self._forget_locked(job.obs_id)
                self._queued[job.obs_id] = job
                self._backlog.append(job.obs_id)
            mine = [obs_id for obs_id in self._backlog if self._queued[obs_id].key == key]
            others = [obs_id for obs_id in self._backlog if self._queued[obs_id].key != key]
            self._backlog = collections.deque(mine + others)
            self._wake.notify()
            return self._pending_locked(key)

    def pending(self, key: str) -> List[str]:
        """Return the IDs of one video's sightings that are running or waiting, the running one first."""
        with self._lock:
            return self._pending_locked(key)

    def cancel(self, obs_id: str) -> None:
        """Forget a deleted sighting: drop its waiting job, and stop its worker when it runs.

        Args:
            obs_id: The sighting ID. An ID with no job is fine.
        """
        with self._wake:
            self._forget_locked(obs_id)
            process = None
            if self._running is not None and self._running.obs_id == obs_id:
                self._stopped = True
                process = self._process
        if process is not None:
            _signal_group(process)

    def status(self, obs_id: str) -> TrackStatus:
        """Report where one sighting's tracking stands."""
        with self._lock:
            return self._status_locked(obs_id)

    def close(self) -> None:
        """Stop the running worker, drop the waiting jobs, and end the worker thread. A second call does nothing."""
        with self._wake:
            if self._closed:
                return
            self._closed = True
            if not self._reason:
                self._reason = CLOSED_REASON
            self._queued.clear()
            self._fresh.clear()
            self._backlog.clear()
            self._stopped = self._running is not None
            process = self._process
            self._wake.notify_all()
        if process is not None:
            _signal_group(process)
        if self._thread is not None:
            self._thread.join()

    # ----- state, the caller holds the lock -----

    def _waiting_locked(self, job: TrackJob) -> bool:
        """Tell whether this sighting runs now or waits in the queue."""
        running = self._running if not self._stopped else None
        return job.same_sighting(running) or job.same_sighting(self._queued.get(job.obs_id))

    def _settled_locked(self, job: TrackJob, now: float) -> bool:
        """Tell whether this sighting needs no job now: saved, dropped, or failed too recently."""
        if job.same_sighting(self._done.get(job.obs_id)) or job.same_sighting(self._dropped.get(job.obs_id)):
            return True
        failure = self._failed.get(job.obs_id)
        return failure is not None and job.same_sighting(failure[0]) and now - failure[1] < self._retry_seconds

    def _forget_locked(self, obs_id: str) -> None:
        """Drop the waiting job and the records of an ID."""
        if self._queued.pop(obs_id, None) is not None:
            for line in (self._fresh, self._backlog):
                if obs_id in line:
                    line.remove(obs_id)
        self._failed.pop(obs_id, None)
        self._done.pop(obs_id, None)
        self._dropped.pop(obs_id, None)

    def _pending_locked(self, key: str) -> List[str]:
        """List the running and waiting IDs of one video, the running one first."""
        ids = []
        if self._running is not None and not self._stopped and self._running.key == key:
            ids.append(self._running.obs_id)
        ids.extend(obs_id for obs_id in itertools.chain(self._fresh, self._backlog) if self._queued[obs_id].key == key)
        return ids

    def _status_locked(self, obs_id: str) -> TrackStatus:
        """Compute one sighting's status."""
        if self._reason:
            return TrackStatus(STATE_UNAVAILABLE, self._reason)
        if self._running is not None and self._running.obs_id == obs_id and not self._stopped:
            return TrackStatus(STATE_RUNNING, "")
        if obs_id in self._queued:
            return TrackStatus(STATE_QUEUED, "")
        if obs_id in self._failed:
            return TrackStatus(STATE_FAILED, self._failed[obs_id][2])
        if obs_id in self._done:
            return TrackStatus(STATE_DONE, "")
        return TrackStatus(STATE_NONE, "")

    # ----- the worker thread -----

    def _work(self) -> None:
        """Run waiting jobs one after another until the tracker closes."""
        while True:
            with self._wake:
                while not self._fresh and not self._backlog and not self._closed:
                    self._wake.wait()
                if self._closed:
                    return
                obs_id = (self._fresh if self._fresh else self._backlog).popleft()
                job = self._queued.pop(obs_id)
                self._running = job
                self._stopped = False
            state, detail = self._run_job(job)
            with self._wake:
                stopped = self._stopped
                self._running = None
                self._stopped = False
                if stopped:
                    continue
                if state == STATE_DONE:
                    self._done[job.obs_id] = job
                elif state == STATE_FAILED:
                    self._failed[job.obs_id] = (job, self._clock(), detail)
                else:
                    self._dropped[job.obs_id] = job
            _log(job.obs_id, f"failed: {detail}" if state == STATE_FAILED else detail)

    def _run_job(self, job: TrackJob) -> Tuple[str, str]:
        """Track one sighting and save its track. No exception leaves this method.

        Returns:
            The outcome and its detail: "done" with what was saved, "failed"
            with the reason, or "none" with a note for a sighting deleted or
            reused while it was tracked, or for a job that was stopped.
        """
        started = time.monotonic()
        try:
            track, ends = checked_answer(self._ask_worker(job))
            self._save_track(job.obs_id, job.key, job.time, (job.point["x"], job.point["y"]), track)
        except _Stopped:
            return STATE_NONE, "stopped"
        except UnknownObservation:
            return STATE_NONE, "was deleted or its ID went to another sighting while it was tracked, so its track was dropped"
        except (TrackingError, StoreError, TrackError, ValueError) as error:
            return STATE_FAILED, str(error)
        except Exception as error:  # the worker thread must outlive any one bad job
            return STATE_FAILED, f"unexpected {type(error).__name__}: {error}"
        points = track["points"]
        return STATE_DONE, (
            f"saved {len(points)} points from {points[0][0]:.2f} s to {points[-1][0]:.2f} s "
            f"(back {ends['back']}, forward {ends['forward']}) in {time.monotonic() - started:.1f} s"
        )

    def _ask_worker(self, job: TrackJob) -> str:
        """Run the worker on one job and return what it printed.

        The request goes in through a temporary file and both answers come
        back through temporary files, so no pipe can fill and stall. The
        worker starts a session of its own, so stopping it stops ffprobe and
        ffmpeg with it.

        Raises:
            TrackingError: When the worker cannot start, outlasts the timeout,
                or exits with an error.
            _Stopped: When the job was canceled or the tracker closed meanwhile.
        """
        request = json.dumps({"source": self._source_for(job.key), "time": job.time, "point": job.point, "box": job.box})
        args = [str(self._python), "-I", str(self._worker)]
        with tempfile.TemporaryFile() as stdin_file, tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
            stdin_file.write(request.encode("utf-8"))
            stdin_file.seek(0)
            try:
                process = subprocess.Popen(args, stdin=stdin_file, stdout=out_file, stderr=err_file, start_new_session=True)
            except OSError as error:
                raise TrackingError(f"track_worker: could not start {args[0]}: {error}") from error
            self._adopt(process)
            try:
                code = process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired as error:
                _signal_group(process)
                process.wait()
                raise TrackingError(f"track_worker gave no answer within {self._timeout:g} seconds and was stopped") from error
            finally:
                # A worker that died early can leave ffmpeg behind in its group.
                _signal_group(process)
                self._release()
            if self._is_stopped():
                raise _Stopped()
            out_file.seek(0)
            err_file.seek(0)
            out = out_file.read().decode("utf-8", "replace")
            err = err_file.read().decode("utf-8", "replace")
        if code != 0:
            raise TrackingError(f"track_worker failed (exit code {code}): {_tail(err)}")
        return out

    def _adopt(self, process: "subprocess.Popen") -> None:
        """Record the running worker so cancel and close can stop it, and stop it at once when they came first."""
        with self._lock:
            self._process = process
            stopped = self._stopped
        if stopped:
            _signal_group(process)

    def _release(self) -> None:
        """Forget the worker recorded by _adopt, after it has exited."""
        with self._lock:
            self._process = None

    def _is_stopped(self) -> bool:
        """Return True when the running job was canceled or the tracker closed."""
        with self._lock:
            return self._stopped
