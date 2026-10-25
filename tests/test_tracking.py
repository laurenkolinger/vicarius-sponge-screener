"""Tests for screener.tracking: the background queue that runs the tracking worker.

A fake worker script stands in for OpenCV. It runs under the Python that runs
these tests, reads the request like track_worker.py, and follows instructions
that each test puts in the request's ``source`` field (the Tracker asks
``source_for`` for it): answer a small track, fail, print other text, sleep,
wait for a gate file, or start a grandchild process. It logs when it starts and
ends, so the tests can see the queue order and that one job runs at a time. A
fake save function stands in for TrackStore.save.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from screener import tracking
from screener.store import StoreError, UnknownObservation
from screener.tracking import TrackJob, Tracker, job_from_row
from screener.tracks import TrackError

KEY_A = "TCRMP_video_ondeck/2024Annual/TCRMP20241018_video_BIT_T1.MP4"
KEY_B = "TCRMP_video_ondeck/2024Annual/TCRMP20241018_video_BIT_T2.MP4"
WAIT_SECONDS = 30.0
POLL_SECONDS = 0.02
GONE_SECONDS = 10.0

FAKE_WORKER = r'''
"""A stand-in for screener/track_worker.py; its source field says what to do."""
import json
import os
import subprocess
import sys
import time

request = json.loads(sys.stdin.read())
plan = json.loads(request["source"])


def note(event):
    """Append one line about this run to the log file, when the plan names one."""
    if plan.get("log"):
        with open(plan["log"], "a") as handle:
            handle.write(json.dumps({"event": event, "time": request["time"]}) + "\n")


note("start")
if plan.get("pids"):
    child = subprocess.Popen(["sleep", "60"])
    with open(plan["pids"], "w") as handle:
        handle.write("%d %d" % (os.getpid(), child.pid))
if plan.get("gate"):
    while not os.path.exists(plan["gate"]):
        time.sleep(0.01)
time.sleep(plan.get("sleep", 0))
note("end")
if plan.get("mode") == "fail":
    sys.stderr.write("source: the fake could not read the video\n")
    sys.exit(1)
if "text" in plan:
    sys.stdout.write(plan["text"])
    sys.exit(0)
t, point, box = request["time"], request["point"], request["box"]
answer = {
    "points": [[t - 0.1, point["x"], point["y"], 0.9], [t, point["x"], point["y"], 1.0], [t + 0.1, point["x"], point["y"], 0.8]],
    "box": None if box is None else {"w": box["w"], "h": box["h"]},
    "ends": {"back": "exit", "forward": "lost"},
}
answer.update(plan.get("answer", {}))
sys.stdout.write(json.dumps(answer) + "\n")
'''


def make_row(obs_id: str, key: str = KEY_A, seconds: str = "12.345", box: bool = False) -> Dict[str, str]:
    """Build an observation row with the cells a job reads, as the store returns them (text)."""
    row = {
        "ID": obs_id, "S3Key": key, "TimestampSeconds": seconds, "PointX": "0.2500", "PointY": "0.7500",
        "BoxX": "", "BoxY": "", "BoxW": "", "BoxH": "",
    }
    if box:
        row.update({"BoxX": "0.2000", "BoxY": "0.7000", "BoxW": "0.1000", "BoxH": "0.1000"})
    return row


def wait_for(predicate: Callable[[], bool], what: str, timeout: float = WAIT_SECONDS) -> None:
    """Poll until a condition holds, or fail with a message."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_SECONDS)
    raise AssertionError("timed out after %g seconds waiting for %s" % (timeout, what))


def process_gone(pid: int) -> bool:
    """Tell whether a process no longer exists. The tracker reaps its own worker; the test never does."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at 1000 seconds."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


class Harness:
    """A tracker on the fake worker, the plans per video, the saves, and the log."""

    def __init__(self, folder: Path) -> None:
        """Write the fake worker and prepare the log and the plans."""
        self.folder = folder
        self.worker = folder / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.log = folder / "runs.log"
        self.plans: Dict[str, Dict[str, Any]] = {KEY_A: {}, KEY_B: {}}
        self.saves: List[Dict[str, Any]] = []
        self.save_error: Optional[Exception] = None
        self.sources: List[str] = []
        self.trackers: List[Tracker] = []
        self.clock = Clock()

    def source_for(self, key: str) -> str:
        """Hand the fake worker its plan for a video, with the shared log."""
        source = json.dumps(dict(self.plans[key], log=str(self.log)))
        self.sources.append(source)
        return source

    def save(self, obs_id: str, key: str, anchor_time: float, point: Any, track: Dict[str, Any]) -> Dict[str, Any]:
        """Record a save the way TrackStore.save is called, or raise the error the test set."""
        if self.save_error is not None:
            raise self.save_error
        self.saves.append({"id": obs_id, "key": key, "anchor_time": anchor_time, "point": point, "track": track})
        return dict(track, anchor_time=anchor_time, updated_at="2026-10-13T12:00:00Z")

    def tracker(self, python: Optional[Path] = None, **options: Any) -> Tracker:
        """Start a tracker on the fake worker; the harness closes it at the end."""
        made = Tracker(
            Path(sys.executable) if python is None else python,
            self.worker,
            source_for=self.source_for,
            save_track=self.save,
            clock=self.clock,
            **options,
        )
        self.trackers.append(made)
        return made

    def runs(self) -> List[Dict[str, Any]]:
        """Read the fake worker's log lines."""
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def started_times(self) -> List[float]:
        """Return the sighting times of the runs in the order they started."""
        return [run["time"] for run in self.runs() if run["event"] == "start"]

    def gate(self) -> Path:
        """Return the gate file a waiting fake worker looks for."""
        return self.folder / "gate"


@pytest.fixture
def harness(tmp_path: Path):
    """A harness whose trackers are closed after the test."""
    made = Harness(tmp_path)
    yield made
    for tracker in made.trackers:
        tracker.close()


def wait_state(tracker: Tracker, obs_id: str, state: str) -> tracking.TrackStatus:
    """Wait until a job reaches a state, and return its status."""
    wait_for(lambda: tracker.status(obs_id).state == state, "%s to be %s (it is %s)" % (obs_id, state, tracker.status(obs_id)))
    return tracker.status(obs_id)


# ----- reading a row -----


def test_job_from_row_reads_the_point_and_the_box():
    assert job_from_row(make_row("ID001")) == TrackJob("ID001", KEY_A, 12.345, {"x": 0.25, "y": 0.75}, None)
    boxed = job_from_row(make_row("ID002", box=True))
    assert boxed.box == {"x": 0.2, "y": 0.7, "w": 0.1, "h": 0.1}


@pytest.mark.parametrize(
    "column, value",
    [("ID", ""), ("S3Key", ""), ("TimestampSeconds", "soon"), ("TimestampSeconds", ""), ("PointX", "oops"), ("PointY", "nan"), ("BoxW", "wide")],
)
def test_job_from_row_names_the_unreadable_column(column: str, value: str):
    row = make_row("ID001", box=column.startswith("Box"))
    row[column] = value
    with pytest.raises(ValueError, match="^%s:" % column):
        job_from_row(row)


# ----- one job -----


def test_a_new_sighting_is_tracked_and_saved(harness: Harness, capsys):
    tracker = harness.tracker()
    assert tracker.available and tracker.reason == ""
    assert tracker.add(make_row("ID001", box=True)).state in ("queued", "running")
    assert wait_state(tracker, "ID001", "done").message == ""
    [save] = harness.saves
    assert save["id"] == "ID001" and save["key"] == KEY_A and save["anchor_time"] == 12.345
    assert save["point"] == (0.25, 0.75)
    # The fake worker writes its times as 12.345 - 0.1 and 12.345 + 0.1, in floats.
    assert save["track"] == {
        "points": [[12.345 - 0.1, 0.25, 0.75, 0.9], [12.345, 0.25, 0.75, 1.0], [12.345 + 0.1, 0.25, 0.75, 0.8]],
        "box": {"w": 0.1, "h": 0.1},
    }
    request = json.loads(harness.sources[0])
    assert request["log"] == str(harness.log)
    err = capsys.readouterr().err
    assert "tracks: ID001 saved 3 points" in err and "back exit, forward lost" in err


def test_the_worker_gets_the_sighting_and_the_source(harness: Harness):
    received: List[str] = []
    original = harness.source_for

    def remember(key: str) -> str:
        """Record which key the tracker asked a source for."""
        received.append(key)
        return original(key)

    harness.source_for = remember  # type: ignore[assignment]
    tracker = harness.tracker()
    tracker.add(make_row("ID001", seconds="5.000"))
    wait_state(tracker, "ID001", "done")
    assert received == [KEY_A]
    assert harness.started_times() == [5.0]


# ----- the queue -----


def test_jobs_run_one_at_a_time_in_queue_order(harness: Harness):
    harness.plans[KEY_A] = {"gate": str(harness.gate())}
    tracker = harness.tracker()
    for number in range(1, 5):
        tracker.add(make_row("ID%03d" % number, seconds="%d.000" % number))
    wait_for(lambda: tracker.status("ID001").state == "running", "the first job to run")
    assert [tracker.status("ID%03d" % number).state for number in range(2, 5)] == ["queued"] * 3
    assert tracker.pending(KEY_A) == ["ID001", "ID002", "ID003", "ID004"]
    harness.gate().write_text("go", encoding="utf-8")
    for number in range(1, 5):
        wait_state(tracker, "ID%03d" % number, "done")
    # Every run appends to one log, so two runs at once would show two starts in a row.
    events = [(run["event"], run["time"]) for run in harness.runs()]
    assert events == [(event, float(number)) for number in range(1, 5) for event in ("start", "end")], events
    assert tracker.pending(KEY_A) == []


def test_new_sightings_run_first_and_the_video_asked_for_last_leads_the_backlog(harness: Harness):
    harness.plans[KEY_A] = {"gate": str(harness.gate())}
    harness.plans[KEY_B] = {"gate": str(harness.gate())}
    tracker = harness.tracker()
    tracker.add(make_row("ID001", seconds="1.000"))
    wait_for(lambda: tracker.status("ID001").state == "running", "the first job to run")
    assert tracker.request(KEY_A, [make_row("ID002", seconds="2.000"), make_row("ID003", seconds="3.000")]) == ["ID001", "ID002", "ID003"]
    assert tracker.request(KEY_B, [make_row("ID004", key=KEY_B, seconds="4.000")]) == ["ID004"]
    tracker.add(make_row("ID005", seconds="5.000"))
    tracker.add(make_row("ID006", key=KEY_B, seconds="6.000"))
    assert tracker.pending(KEY_B) == ["ID006", "ID004"]
    harness.gate().write_text("go", encoding="utf-8")
    for number in range(1, 7):
        wait_state(tracker, "ID%03d" % number, "done")
    assert harness.started_times() == [1.0, 5.0, 6.0, 4.0, 2.0, 3.0]


def test_asking_again_for_a_video_moves_its_rows_back_to_the_front(harness: Harness):
    harness.plans[KEY_A] = {"gate": str(harness.gate())}
    harness.plans[KEY_B] = {"gate": str(harness.gate())}
    tracker = harness.tracker()
    tracker.add(make_row("ID001", seconds="1.000"))
    wait_for(lambda: tracker.status("ID001").state == "running", "the first job to run")
    rows_a = [make_row("ID002", seconds="2.000")]
    tracker.request(KEY_A, rows_a)
    tracker.request(KEY_B, [make_row("ID003", key=KEY_B, seconds="3.000")])
    assert tracker.request(KEY_A, rows_a) == ["ID001", "ID002"]
    harness.gate().write_text("go", encoding="utf-8")
    for number in range(1, 4):
        wait_state(tracker, "ID%03d" % number, "done")
    assert harness.started_times() == [1.0, 2.0, 3.0]


def test_a_queued_or_running_row_is_not_queued_twice(harness: Harness):
    harness.plans[KEY_A] = {"gate": str(harness.gate())}
    tracker = harness.tracker()
    tracker.add(make_row("ID001", seconds="1.000"))
    wait_for(lambda: tracker.status("ID001").state == "running", "the first job to run")
    tracker.add(make_row("ID002", seconds="2.000"))
    assert tracker.add(make_row("ID001", seconds="1.000")).state == "running"
    assert tracker.request(KEY_A, [make_row("ID001", seconds="1.000"), make_row("ID002", seconds="2.000")]) == ["ID001", "ID002"]
    harness.gate().write_text("go", encoding="utf-8")
    wait_state(tracker, "ID002", "done")
    assert harness.started_times() == [1.0, 2.0]


def test_a_done_row_is_not_queued_again(harness: Harness):
    tracker = harness.tracker()
    tracker.add(make_row("ID001"))
    wait_state(tracker, "ID001", "done")
    # GET /api/tracks can read the folder just before the save and ask after it.
    assert tracker.request(KEY_A, [make_row("ID001")]) == []
    time.sleep(0.3)
    assert len(harness.saves) == 1 and harness.started_times() == [12.345]


def test_a_reused_id_is_a_new_sighting(harness: Harness):
    tracker = harness.tracker()
    tracker.add(make_row("ID001", seconds="1.000"))
    wait_state(tracker, "ID001", "done")
    assert tracker.request(KEY_A, [make_row("ID001", seconds="9.000")]) == ["ID001"]
    wait_for(lambda: len(harness.saves) == 2, "the second sighting with ID001 to be saved")
    assert [save["anchor_time"] for save in harness.saves] == [1.0, 9.0]


def test_a_sighting_marked_again_on_the_same_frame_is_a_new_sighting(harness: Harness):
    tracker = harness.tracker()
    tracker.add(make_row("ID001", seconds="1.000"))
    wait_state(tracker, "ID001", "done")
    moved = make_row("ID001", seconds="1.000")
    moved["PointX"] = "0.6000"
    assert tracker.request(KEY_A, [moved]) == ["ID001"]
    wait_for(lambda: len(harness.saves) == 2, "the sighting marked again to be saved")
    assert [save["point"] for save in harness.saves] == [(0.25, 0.75), (0.6, 0.75)]


def test_rows_of_another_video_and_unreadable_rows_are_left_out(harness: Harness):
    harness.plans[KEY_A] = {"gate": str(harness.gate())}
    tracker = harness.tracker()
    unreadable = make_row("ID003")
    unreadable["PointX"] = "oops"
    assert tracker.request(KEY_A, [make_row("ID001"), make_row("ID002", key=KEY_B), unreadable]) == ["ID001"]
    assert tracker.status("ID002").state == "none" and tracker.status("ID003").state == "none"
    status = tracker.add(unreadable)
    assert status.state == "failed" and status.message.startswith("PointX:")


# ----- cancel, timeout, close -----


def test_cancel_drops_a_queued_job(harness: Harness):
    harness.plans[KEY_A] = {"gate": str(harness.gate())}
    tracker = harness.tracker()
    tracker.add(make_row("ID001", seconds="1.000"))
    wait_for(lambda: tracker.status("ID001").state == "running", "the first job to run")
    tracker.add(make_row("ID002", seconds="2.000"))
    tracker.request(KEY_A, [make_row("ID003", seconds="3.000")])
    tracker.cancel("ID002")
    tracker.cancel("ID003")
    tracker.cancel("ID099")
    assert tracker.status("ID002").state == "none" and tracker.pending(KEY_A) == ["ID001"]
    harness.gate().write_text("go", encoding="utf-8")
    wait_state(tracker, "ID001", "done")
    time.sleep(0.3)
    assert harness.started_times() == [1.0]
    assert [save["id"] for save in harness.saves] == ["ID001"]


def test_cancel_stops_a_running_job_with_its_children(harness: Harness):
    pids_file = harness.folder / "pids"
    harness.plans[KEY_A] = {"pids": str(pids_file), "sleep": 60}
    tracker = harness.tracker()
    tracker.add(make_row("ID001"))
    wait_for(lambda: pids_file.exists() and pids_file.read_text().strip() != "", "the fake worker to start its child")
    worker_pid, child_pid = (int(value) for value in pids_file.read_text().split())
    started = time.monotonic()
    tracker.cancel("ID001")
    wait_for(lambda: process_gone(worker_pid) and process_gone(child_pid), "the worker and its child to stop", GONE_SECONDS)
    assert time.monotonic() - started < GONE_SECONDS
    wait_for(lambda: tracker.status("ID001").state == "none", "the canceled job to be forgotten")
    assert harness.saves == []
    assert tracker.pending(KEY_A) == []


def test_a_worker_past_its_timeout_is_stopped_with_its_children_and_the_job_fails(harness: Harness):
    pids_file = harness.folder / "pids"
    harness.plans[KEY_A] = {"pids": str(pids_file), "sleep": 60}
    tracker = harness.tracker(timeout=1.0)
    tracker.add(make_row("ID001"))
    status = wait_state(tracker, "ID001", "failed")
    assert "no answer within 1 seconds" in status.message
    worker_pid, child_pid = (int(value) for value in pids_file.read_text().split())
    wait_for(lambda: process_gone(worker_pid) and process_gone(child_pid), "the worker and its child to stop", GONE_SECONDS)
    assert harness.saves == []


def test_close_stops_the_running_job_and_turns_the_tracker_off(harness: Harness):
    pids_file = harness.folder / "pids"
    harness.plans[KEY_A] = {"pids": str(pids_file), "sleep": 60}
    tracker = harness.tracker()
    tracker.add(make_row("ID001"))
    tracker.add(make_row("ID002"))
    wait_for(lambda: pids_file.exists() and pids_file.read_text().strip() != "", "the fake worker to start its child")
    worker_pid, child_pid = (int(value) for value in pids_file.read_text().split())
    tracker.close()
    assert process_gone(worker_pid), "close returned before its worker was stopped"
    wait_for(lambda: process_gone(child_pid), "the worker's child to stop", GONE_SECONDS)
    assert not tracker.available
    assert tracker.add(make_row("ID003")).state == "unavailable"
    assert tracker.request(KEY_A, [make_row("ID004")]) == []
    assert harness.saves == []
    tracker.close()


# ----- failures -----


def test_a_failing_worker_fails_the_job_with_its_message(harness: Harness, capsys):
    harness.plans[KEY_A] = {"mode": "fail"}
    tracker = harness.tracker()
    tracker.add(make_row("ID001"))
    status = wait_state(tracker, "ID001", "failed")
    assert "exit code 1" in status.message and "the fake could not read the video" in status.message
    assert harness.saves == []
    assert "tracks: ID001 failed: " in capsys.readouterr().err


BAD_ANSWERS = [
    ("nothing", {"text": ""}, "answer: "),
    ("not json", {"text": "tracking went fine"}, "answer: "),
    ("a list", {"text": "[]"}, "answer: "),
    ("no points", {"answer": {"points": None}}, "points: "),
    ("unsorted times", {"answer": {"points": [[2.0, 0.5, 0.5, 1.0], [1.0, 0.5, 0.5, 1.0]]}}, "points[1].t: "),
    ("a point outside", {"answer": {"points": [[1.0, 1.5, 0.5, 1.0]]}}, "points[0].x: "),
    ("a nan", {"text": '{"points": [[1.0, NaN, 0.5, 1.0]], "box": null, "ends": {"back": "exit", "forward": "exit"}}'}, "points[0].x: "),
    ("no box", {"text": '{"points": [[1.0, 0.5, 0.5, 1.0]], "ends": {"back": "exit", "forward": "exit"}}'}, "box: "),
    ("no ends", {"answer": {"ends": None}}, "ends: "),
    ("an unknown end", {"answer": {"ends": {"back": "exit", "forward": "tired"}}}, "ends.forward: "),
]


@pytest.mark.parametrize("label, plan, start", BAD_ANSWERS, ids=[item[0] for item in BAD_ANSWERS])
def test_a_malformed_answer_fails_the_job_and_saves_nothing(harness: Harness, label: str, plan: Dict[str, Any], start: str):
    harness.plans[KEY_A] = plan
    tracker = harness.tracker()
    tracker.add(make_row("ID001"))
    status = wait_state(tracker, "ID001", "failed")
    assert status.message.startswith(start), "%s: %r" % (label, status.message)
    assert harness.saves == []


def test_a_sighting_deleted_while_it_was_tracked_is_dropped_quietly_and_never_retried(harness: Harness, capsys):
    harness.save_error = UnknownObservation("ID001: no sighting has this ID")
    tracker = harness.tracker()
    tracker.add(make_row("ID001"))
    wait_for(lambda: "ID001 was deleted" in capsys.readouterr().err, "the drop to be logged")
    assert tracker.status("ID001").state == "none"
    harness.clock.now += 10 ** 6
    assert tracker.request(KEY_A, [make_row("ID001")]) == []


@pytest.mark.parametrize(
    "error",
    [TrackError("/data/tracks/ID001.json: No space left on device"), StoreError("observations.csv: the write lock was busy")],
    ids=["track file", "store"],
)
def test_a_failed_save_fails_the_job_with_its_message(harness: Harness, error: Exception):
    harness.save_error = error
    tracker = harness.tracker()
    tracker.add(make_row("ID001"))
    assert str(error) in wait_state(tracker, "ID001", "failed").message


def test_a_failed_row_waits_before_another_try(harness: Harness):
    harness.plans[KEY_A] = {"mode": "fail"}
    tracker = harness.tracker(retry_seconds=300.0)
    tracker.add(make_row("ID001"))
    wait_state(tracker, "ID001", "failed")
    harness.plans[KEY_A] = {}
    harness.clock.now += 299.0
    assert tracker.request(KEY_A, [make_row("ID001")]) == []
    assert tracker.status("ID001").state == "failed"
    harness.clock.now += 2.0
    assert tracker.request(KEY_A, [make_row("ID001")]) == ["ID001"]
    wait_state(tracker, "ID001", "done")
    assert len(harness.saves) == 1


def test_the_worker_thread_survives_a_failing_source(harness: Harness):
    calls = {"count": 0}

    def broken_source(key: str) -> str:
        """Fail on the first call, as a converter lookup might."""
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("the converted copy could not be read")
        return json.dumps({})

    harness.source_for = broken_source  # type: ignore[assignment]
    tracker = harness.tracker()
    tracker.add(make_row("ID001", seconds="1.000"))
    assert "the converted copy could not be read" in wait_state(tracker, "ID001", "failed").message
    tracker.add(make_row("ID002", seconds="2.000"))
    wait_state(tracker, "ID002", "done")


# ----- without the venv -----


def test_without_the_venv_tracking_is_unavailable_and_nothing_runs(harness: Harness, tmp_path: Path):
    missing = tmp_path / "no-venv" / "bin" / "python"
    tracker = harness.tracker(python=missing)
    assert tracker.available is False
    assert str(missing) in tracker.reason
    status = tracker.add(make_row("ID001"))
    assert status.state == "unavailable" and str(missing) in status.message
    assert tracker.request(KEY_A, [make_row("ID002")]) == []
    assert tracker.pending(KEY_A) == []
    assert tracker.status("ID002").state == "unavailable"
    time.sleep(0.2)
    assert harness.runs() == [] and harness.saves == []
    tracker.cancel("ID001")
    tracker.close()


def test_without_the_worker_script_tracking_is_unavailable(harness: Harness, tmp_path: Path):
    harness.worker.unlink()
    tracker = harness.tracker()
    assert tracker.available is False and str(harness.worker) in tracker.reason


def test_the_tracker_checks_its_arguments(tmp_path: Path):
    def save(obs_id: str, key: str, anchor_time: float, point: Any, track: Any) -> Dict[str, Any]:
        """Stand in for TrackStore.save."""
        return {}

    def source(key: str) -> str:
        """Stand in for the server's source lookup."""
        return key

    python, worker = Path(sys.executable), tmp_path / "worker.py"
    for arguments, options, name in (
        ((python, worker, "source", save), {}, "source_for"),
        ((python, worker, source, None), {}, "save_track"),
        ((python, worker, source, save), {"timeout": 0}, "timeout"),
        ((python, worker, source, save), {"retry_seconds": -1}, "retry_seconds"),
        ((python, worker, source, save), {"timeout": float("nan")}, "timeout"),
        (("python", worker, source, save), {}, "python"),
    ):
        with pytest.raises(ValueError, match="^%s:" % name):
            Tracker(*arguments, **options)


def test_threads_do_not_pile_up(harness: Harness):
    before = threading.active_count()
    for _ in range(3):
        harness.tracker().close()
    assert threading.active_count() <= before
