"""Tests for VICARIUS run folders: screener.runs, screener.runstate, and the run routing in the server.

Every run folder is made here under a temporary runs folder with a
project.json in VICARIUS's shape, so no test touches the real
``inprocess/`` folder. Server tests start a real server on a free port with a
FakeS3.
"""

import json
import os
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path

import pytest

from screener import runstate
from screener.runs import NoRun, RunFolders, UnknownRun
from tests.app_helpers import KEY_T1, KEY_T2, PNG, observation, start_app
from tests.fakes3 import FakeS3

RUN_A = "run_26-10-27_LO"
RUN_B = "run_26-10-27_AB"
STAMP = "2026-10-27T20:00:00-04:00"
# How long the lock test watches a blocked write before it releases the lock.
LOCK_WAIT_SECONDS = 1.0
# A second process that takes the run's file lock the way VICARIUS does, says so, and
# holds it until it reads a line.
LOCK_HOLDER = (
    "import fcntl, sys\n"
    "handle = open(sys.argv[1], 'a+')\n"
    "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "sys.stdin.readline()\n"
)


def make_run(root: Path, name: str, initials: str = "LO") -> Path:
    """Create a run folder with a project.json the way VICARIUS mints one.

    Args:
        root: The runs folder.
        name: The run folder name.
        initials: The creator's initials in the first access_log row.

    Returns:
        The run folder.
    """
    folder = root / name
    folder.mkdir(parents=True)
    stub = {
        "id": name[len("run_"):],
        "name": f"{initials}_26-10-27",
        "created_at": "2026-10-27T19:55:00",
        "updated_at": "2026-10-27T19:55:00",
        "project_dir": str(folder),
        "repo_dir": "/nowhere/github_repo",
        "current_step": 1,
        "steps": {},
        "access_log": [{"ts": STAMP, "initials": initials, "action": "create", "purpose": "test"}],
        "initials": initials,
        "name_auto": True,
    }
    (folder / "project.json").write_text(json.dumps(stub, indent=2), encoding="utf-8")
    return folder


def project(folder: Path) -> dict:
    """Read a run's project.json."""
    return json.loads((folder / "project.json").read_text(encoding="utf-8"))


@pytest.fixture
def runs_root(tmp_path) -> Path:
    """A runs folder holding RUN_A and RUN_B."""
    root = tmp_path / "inprocess"
    make_run(root, RUN_A, "LO")
    make_run(root, RUN_B, "AB")
    return root


@pytest.fixture
def fake():
    """A FakeS3 with two small videos."""
    server = FakeS3()
    server.start()
    server.put(KEY_T1, b"\0" * 5000)
    server.put(KEY_T2, b"\0" * 5000)
    yield server
    server.stop()


@pytest.fixture
def vicarius(tmp_path, fake, runs_root, monkeypatch):
    """A running app in VICARIUS mode on runs_root, unlocked."""
    monkeypatch.setenv("VICARIUS_LOCKS_PATH", str(tmp_path / "no-locks.json"))
    app = start_app(tmp_path, fake.url, runs_root=runs_root, guide_dir=tmp_path / "guide")
    yield app
    app.stop()


def in_run(name: str) -> dict:
    """Return the header that names a run."""
    return {"X-Screener-Run": name}


def set_initials(running, run: str, initials: str = "LO") -> None:
    """Store a run's annotator through the API."""
    answer = running.client.post("/api/settings", {"annotator": initials}, headers=in_run(run))
    assert answer.status == 200, answer.body


# ----- RunFolders -----


@pytest.mark.parametrize("root, fragment", [(5, "expected a folder path"), ("relative/inprocess", "absolute"), ("/a\x00b", "NUL")])
def test_runs_root_is_checked(root, fragment):
    with pytest.raises(ValueError, match=fragment):
        RunFolders(root)


def test_folder_returns_a_real_run_folder(runs_root):
    assert RunFolders(runs_root).folder(RUN_A) == runs_root / RUN_A


@pytest.mark.parametrize(
    "name",
    ["../run_x", "run_../x", "run_", "run_a/b", "run_a\x00", "RUN_x", "run_" + "a" * 161, "run_.hidden", "", "inprocess", "run_a b"],
)
def test_folder_refuses_names_that_are_not_run_folders(runs_root, name):
    with pytest.raises(ValueError, match="^run:"):
        RunFolders(runs_root).folder(name)


@pytest.mark.parametrize("name", [5, None, ["run_a"], b"run_a"])
def test_folder_refuses_names_of_the_wrong_type(runs_root, name):
    with pytest.raises(ValueError, match="^run: expected a run folder name"):
        RunFolders(runs_root).folder(name)


def test_folder_missing_is_unknown(runs_root):
    with pytest.raises(UnknownRun, match="no run folder"):
        RunFolders(runs_root).folder("run_26-10-27_ZZ")


def test_folder_without_project_json_is_unknown(runs_root):
    (runs_root / "run_bare").mkdir()
    with pytest.raises(UnknownRun, match="holds no project.json"):
        RunFolders(runs_root).folder("run_bare")


def test_project_json_that_is_a_folder_is_unknown(runs_root):
    (runs_root / "run_odd" / "project.json").mkdir(parents=True)
    with pytest.raises(UnknownRun):
        RunFolders(runs_root).folder("run_odd")


def test_link_to_a_folder_outside_is_refused(runs_root, tmp_path):
    outside = make_run(tmp_path / "elsewhere", "run_outside")
    os.symlink(outside, runs_root / "run_link")
    with pytest.raises(ValueError, match="is a link"):
        RunFolders(runs_root).folder("run_link")


def test_name_for_project_dir_accepts_the_folder_vicarius_hands_over(runs_root):
    folders = RunFolders(runs_root)
    assert folders.name_for_project_dir(str(runs_root / RUN_A)) == RUN_A
    assert folders.name_for_project_dir(str(runs_root / RUN_A) + "/") == RUN_A
    assert folders.name_for_project_dir("  " + str(runs_root / RUN_A) + "  ") == RUN_A


@pytest.mark.parametrize("relative", ["", "x/" + RUN_A, "../" + RUN_A, RUN_A + "/frames"])
def test_name_for_project_dir_refuses_other_folders(runs_root, relative):
    target = str(runs_root / relative) if relative else "   "
    with pytest.raises(ValueError, match="^(project_dir|run):"):
        RunFolders(runs_root).name_for_project_dir(target)


@pytest.mark.parametrize("value", [None, 7, {"a": 1}, "/x\x00y"])
def test_name_for_project_dir_refuses_bad_values(runs_root, value):
    with pytest.raises(ValueError, match="^project_dir:"):
        RunFolders(runs_root).name_for_project_dir(value)


# ----- runstate -----


def test_mark_step_writes_the_step_and_keeps_every_other_field(runs_root):
    folder = runs_root / RUN_A
    before = project(folder)
    assert runstate.mark_step(folder / "project.json", 1, "Screen videos", "running", now=STAMP) is True
    after = project(folder)
    assert after["steps"] == {"1": {"name": "Screen videos", "status": "running"}}
    assert after["updated_at"] == STAMP
    for field in ("id", "name", "created_at", "project_dir", "repo_dir", "current_step", "access_log", "initials", "name_auto"):
        assert after[field] == before[field], field
    assert sorted(path.name for path in folder.iterdir()) == [".project.json.lock", "project.json"]


def test_mark_step_twice_writes_once(runs_root):
    path = runs_root / RUN_A / "project.json"
    assert runstate.mark_step(path, 1, "Screen videos", "running", now=STAMP) is True
    text = path.read_text(encoding="utf-8")
    assert runstate.mark_step(path, 1, "Screen videos", "running", now="2026-10-28T00:00:00-04:00") is False
    assert path.read_text(encoding="utf-8") == text


def test_record_export_marks_the_step_and_logs_the_export(runs_root):
    path = runs_root / RUN_A / "project.json"
    assert runstate.record_export(path, "LO", "spongeGroundTruth_20261027", 1, now=STAMP) is True
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["steps"]["2"] == {"name": "Export the package", "status": "completed"}
    assert after["access_log"][-1] == {
        "ts": STAMP, "initials": "LO", "action": "export", "purpose": "Exported spongeGroundTruth_20261027 with 1 sighting"
    }
    assert len(after["access_log"]) == 2


def test_record_export_without_initials_logs_no_row(runs_root):
    path = runs_root / RUN_A / "project.json"
    runstate.record_export(path, "", "spongeGroundTruth_20261027", 4, now=STAMP)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["steps"]["2"]["status"] == "completed"
    assert len(after["access_log"]) == 1


def test_wrong_shaped_steps_and_access_log_are_replaced(runs_root):
    path = runs_root / RUN_A / "project.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["steps"], document["access_log"] = ["nonsense"], "nonsense"
    path.write_text(json.dumps(document), encoding="utf-8")
    runstate.record_export(path, "LO", "spongeGroundTruth_20261027", 2, now=STAMP)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert list(after["steps"]) == ["2"]
    assert len(after["access_log"]) == 1


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", "", "\xff\xfe binary"])
def test_unreadable_project_json_is_reported_and_left_alone(runs_root, capsys, content):
    path = runs_root / RUN_A / "project.json"
    path.write_bytes(content.encode("latin-1"))
    assert runstate.mark_step(path, 1, "Screen videos", "running") is False
    assert path.read_bytes() == content.encode("latin-1")
    assert "was not updated" in capsys.readouterr().err


def test_missing_project_json_is_reported(tmp_path, capsys):
    assert runstate.record_export(tmp_path / "project.json", "LO", "p", 1) is False
    assert "missing" in capsys.readouterr().err
    assert not (tmp_path / "project.json").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only folder")
def test_unwritable_run_folder_keeps_the_old_file(runs_root, capsys):
    folder = runs_root / RUN_A
    text = (folder / "project.json").read_text(encoding="utf-8")
    folder.chmod(0o555)
    try:
        assert runstate.mark_step(folder / "project.json", 1, "Screen videos", "running") is False
    finally:
        folder.chmod(0o755)
    assert (folder / "project.json").read_text(encoding="utf-8") == text
    assert "was not updated" in capsys.readouterr().err


@pytest.mark.parametrize("number, name, status", [(0, "a", "b"), (True, "a", "b"), ("1", "a", "b"), (1, "", "b"), (1, "a", None)])
def test_mark_step_refuses_bad_arguments(runs_root, number, name, status):
    with pytest.raises(ValueError):
        runstate.mark_step(runs_root / RUN_A / "project.json", number, name, status)


def test_concurrent_exports_each_log_one_row(runs_root):
    path = runs_root / RUN_A / "project.json"
    threads = [
        threading.Thread(target=runstate.record_export, args=(path, "LO", f"spongeGroundTruth_2026102{index}", index))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert len(after["access_log"]) == 1 + 8


def test_a_write_waits_for_the_lock_vicarius_holds(runs_root):
    """VICARIUS's writer holds .project.json.lock; the screener's write waits for it, so no row is lost."""
    folder = runs_root / RUN_A
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(folder / ".project.json.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        done = threading.Event()
        worker = threading.Thread(
            target=lambda: (runstate.record_export(folder / "project.json", "LO", "spongeGroundTruth_20261027", 1), done.set())
        )
        worker.start()
        assert not done.wait(LOCK_WAIT_SECONDS), "the screener wrote project.json while another process held the lock"
        assert len(project(folder)["access_log"]) == 1
        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert done.wait(30)
        worker.join(timeout=30)
    finally:
        holder.kill()
        holder.wait(timeout=30)
    assert len(project(folder)["access_log"]) == 2


def test_statuses_are_the_words_vicarius_counts():
    """The run cards count "completed" and "running" (vicarius_ui_os/module_views.py); a third word would count as nothing."""
    assert runstate.RUNNING == "running"
    assert runstate.COMPLETED == "completed"
    launcher = Path("/mnt/rip/vicarius_drive/vicarius_ui_os/module_views.py")
    if not launcher.is_file():
        pytest.skip(f"{launcher} is not on this machine, so the cross-check against the launcher is skipped")
    text = launcher.read_text(encoding="utf-8")
    assert '_COMPLETED_STATUS = "completed"' in text
    assert '_RUNNING_STATUS = "running"' in text


def test_ast_now_carries_the_atlantic_offset():
    assert runstate.ast_now().endswith("-04:00")


# ----- the server in VICARIUS mode -----


def test_health_says_the_app_keeps_runs(vicarius):
    assert vicarius.client.get("/api/health").json()["runs"] is True


def test_project_open_answers_the_run_without_the_page_header(vicarius, runs_root):
    answer = vicarius.client.post("/api/project/open", {"project_dir": str(runs_root / RUN_A)}, mutation_header=False)
    assert answer.status == 200, answer.body
    assert answer.json() == {"run": RUN_A, "project_dir": str(runs_root / RUN_A)}
    assert sorted(path.name for path in (runs_root / RUN_A).iterdir()) == ["project.json"]


@pytest.mark.parametrize(
    "body, status, fragment",
    [
        ({"project_dir": "/etc"}, 400, "project_dir:"),
        ({}, 400, "project_dir: missing"),
        ({"project_dir": 3}, 400, "project_dir:"),
        ([], 400, "body:"),
    ],
)
def test_project_open_refuses_other_folders(vicarius, body, status, fragment):
    answer = vicarius.client.post("/api/project/open", body, mutation_header=False)
    assert answer.status == status
    assert answer.error.startswith(fragment), answer.error


def test_project_open_for_a_missing_run_is_404(vicarius, runs_root):
    answer = vicarius.client.post("/api/project/open", {"project_dir": str(runs_root / "run_26-10-27_ZZ")}, mutation_header=False)
    assert answer.status == 404


def test_data_calls_without_a_run_answer_409(vicarius):
    for path in ("/api/species", "/api/tally", "/api/catalog", "/api/observations?status=unlabeled", "/media/crops/ID001_x.png"):
        answer = vicarius.client.get(path)
        assert answer.status == 409, path
        assert "open it from a run" in answer.error
    assert vicarius.client.post("/api/observations", observation()).status == 409


def test_shared_routes_need_no_run(vicarius):
    assert vicarius.client.get("/", headers={"Accept": "text/html"}).status == 200
    assert vicarius.client.get("/static/app.js").status == 200
    assert vicarius.client.get("/api/health").status == 200
    assert vicarius.client.get("/video?key=" + KEY_T1, headers={"Range": "bytes=0-9"}).status == 206


@pytest.mark.parametrize("run, status", [("run_26-10-27_ZZ", 404), ("../../etc", 400), ("run_a/../../x", 400), ("run_x\tz", 400)])
def test_unknown_or_hostile_run_names(vicarius, run, status):
    answer = vicarius.client.get("/api/species", headers=in_run(run))
    assert answer.status == status
    assert answer.error.startswith("run:")


def test_two_runs_keep_separate_sightings_settings_and_ids(vicarius, runs_root):
    set_initials(vicarius, RUN_A, "LO")
    set_initials(vicarius, RUN_B, "AB")
    first = vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A))
    second = vicarius.client.post("/api/observations", observation(species="XMUT"), headers=in_run(RUN_B))
    assert first.status == 201 and second.status == 201
    assert first.json()["row"]["ID"] == "ID001" and first.json()["row"]["Annotator"] == "LO"
    assert second.json()["row"]["ID"] == "ID001" and second.json()["row"]["Annotator"] == "AB"
    rows_a = vicarius.client.get("/api/observations?key=" + KEY_T1, headers=in_run(RUN_A)).json()["rows"]
    rows_b = vicarius.client.get("/api/observations?key=" + KEY_T1, headers=in_run(RUN_B)).json()["rows"]
    assert [row["SpeciesCode"] for row in rows_a] == ["ACAU"]
    assert [row["SpeciesCode"] for row in rows_b] == ["XMUT"]
    assert (runs_root / RUN_A / "observations.csv").is_file()
    assert json.loads((runs_root / RUN_B / "settings.json").read_text())["annotator"] == "AB"


def test_run_query_parameter_names_the_run_for_images(vicarius, runs_root):
    set_initials(vicarius, RUN_A)
    row = vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A)).json()["row"]
    crop = vicarius.client.get(f"/media/crops/{row['CropFileName']}?run={RUN_A}")
    assert crop.status == 200 and crop.body == PNG
    assert vicarius.client.get(f"/media/crops/{row['CropFileName']}?run={RUN_B}").status == 404


def test_a_run_without_initials_refuses_to_save_or_open(vicarius, runs_root):
    saved = vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A))
    assert saved.status == 400
    assert saved.error.startswith("annotator: empty; type your initials")
    opened = vicarius.client.post("/api/videos/open", {"key": KEY_T1}, headers=in_run(RUN_A))
    assert opened.status == 400
    assert not (runs_root / RUN_A / "observations.csv").exists()
    assert vicarius.client.get("/api/species", headers=in_run(RUN_A)).json()["annotator"] == ""


def test_opening_a_video_marks_the_screening_step(vicarius, runs_root):
    set_initials(vicarius, RUN_A)
    answer = vicarius.client.post("/api/videos/open", {"key": KEY_T1}, headers=in_run(RUN_A))
    assert answer.status == 200, answer.body
    assert project(runs_root / RUN_A)["steps"] == {"1": {"name": "Screen videos", "status": "running"}}
    assert project(runs_root / RUN_B)["steps"] == {}


def test_export_goes_into_the_run_and_logs_it(vicarius, runs_root):
    set_initials(vicarius, RUN_A)
    vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A))
    answer = vicarius.client.post("/api/export", {}, headers=in_run(RUN_A))
    assert answer.status == 200, answer.body
    package = Path(answer.json()["path"])
    assert package == runs_root / RUN_A / "exports" / ("spongeGroundTruth_" + date.today().strftime("%Y%m%d"))
    assert (package / "observations.csv").is_file()
    record = project(runs_root / RUN_A)
    assert record["steps"]["2"] == {"name": "Export the package", "status": "completed"}
    assert record["access_log"][-1]["action"] == "export"
    assert record["access_log"][-1]["initials"] == "LO"
    assert record["access_log"][-1]["purpose"] == f"Exported {package.name} with 1 sighting"


def test_run_settings_keep_a_relative_export_folder_that_follows_the_run(vicarius, runs_root, tmp_path, fake, monkeypatch):
    set_initials(vicarius, RUN_A)
    assert json.loads((runs_root / RUN_A / "settings.json").read_text())["export_root"] == "exports"
    vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A))
    vicarius.stop()
    moved_root = tmp_path / "moved" / "inprocess"
    moved_root.parent.mkdir()
    (runs_root).rename(moved_root)
    monkeypatch.setenv("VICARIUS_LOCKS_PATH", str(tmp_path / "no-locks.json"))
    again = start_app(tmp_path / "second", fake.url, runs_root=moved_root)
    try:
        answer = again.client.post("/api/export", {}, headers=in_run(RUN_A))
        assert answer.status == 200, answer.body
        assert Path(answer.json()["path"]).parent == moved_root / RUN_A / "exports"
    finally:
        again.stop()


def test_a_broken_project_json_never_stops_screening(vicarius, runs_root, capsys):
    set_initials(vicarius, RUN_A)
    (runs_root / RUN_A / "project.json").write_text("{broken", encoding="utf-8")
    assert vicarius.client.post("/api/videos/open", {"key": KEY_T1}, headers=in_run(RUN_A)).status == 200
    assert vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A)).status == 201
    assert (runs_root / RUN_A / "project.json").read_text(encoding="utf-8") == "{broken"


def test_a_run_whose_project_json_disappears_is_refused(vicarius, runs_root):
    (runs_root / RUN_B / "project.json").unlink()
    assert vicarius.client.get("/api/species", headers=in_run(RUN_B)).status == 404


def test_the_same_run_reuses_one_workspace(vicarius):
    assert vicarius.app.workspace(RUN_A) is vicarius.app.workspace(RUN_A)
    assert vicarius.app.workspace(RUN_A) is not vicarius.app.workspace(RUN_B)


def test_no_standalone_workspace_in_vicarius_mode(vicarius):
    with pytest.raises(NoRun):
        vicarius.app.workspace(None)
    with pytest.raises(NoRun):
        _ = vicarius.app.store


def test_concurrent_saves_into_two_runs_never_mix(vicarius, runs_root):
    set_initials(vicarius, RUN_A, "LO")
    set_initials(vicarius, RUN_B, "AB")
    failures = []

    def save(run: str, count: int) -> None:
        for _ in range(count):
            answer = vicarius.client.post("/api/observations", observation(), headers=in_run(run))
            if answer.status != 201:
                failures.append((run, answer.status, answer.body))

    threads = [threading.Thread(target=save, args=(run, 5)) for run in (RUN_A, RUN_B, RUN_A, RUN_B)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert failures == []
    for run, initials in ((RUN_A, "LO"), (RUN_B, "AB")):
        rows = vicarius.client.get("/api/observations?key=" + KEY_T1, headers=in_run(run)).json()["rows"]
        assert len(rows) == 10
        assert {row["Annotator"] for row in rows} == {initials}
        assert sorted(row["ID"] for row in rows) == [f"ID{index:03d}" for index in range(1, 11)]


def test_guide_comes_from_the_guide_folder(vicarius, tmp_path):
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide" / "guide.json").write_text('{"species": {}}', encoding="utf-8")
    answer = vicarius.client.get("/guide/guide.json")
    assert answer.status == 200 and answer.json() == {"species": {}}


# ----- the server standalone -----


def test_standalone_refuses_a_run_header_and_project_open(tmp_path, fake, monkeypatch):
    monkeypatch.setenv("VICARIUS_LOCKS_PATH", str(tmp_path / "no-locks.json"))
    running = start_app(tmp_path, fake.url, data_dir=tmp_path / "data")
    try:
        answer = running.client.get("/api/species", headers=in_run(RUN_A))
        assert answer.status == 400 and "has no runs" in answer.error
        opened = running.client.post("/api/project/open", {"project_dir": "/x/run_a"}, mutation_header=False)
        assert opened.status == 400 and "opens no runs" in opened.error
        assert running.client.get("/api/species").status == 200
        assert running.client.get("/api/health").json()["runs"] is False
    finally:
        running.stop()


def test_app_needs_a_data_folder_or_a_runs_folder(tmp_path, fake):
    with pytest.raises(ValueError, match="got neither"):
        start_app(tmp_path, fake.url)


# ----- findings of the code review -----


def test_a_hand_edit_of_settings_json_takes_effect_and_survives_a_later_save(vicarius, runs_root, tmp_path):
    set_initials(vicarius, RUN_A, "LO")
    vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A))
    synced = tmp_path / "synced_ondeck"
    synced.mkdir()
    path = runs_root / RUN_A / "settings.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["export_root"] = str(synced)
    path.write_text(json.dumps(document), encoding="utf-8")
    exported = vicarius.client.post("/api/export", {}, headers=in_run(RUN_A))
    assert exported.status == 200, exported.body
    assert Path(exported.json()["path"]).parent == synced
    set_initials(vicarius, RUN_A, "MK")
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["export_root"] == str(synced) and after["annotator"] == "MK"


def test_a_run_made_again_under_the_same_name_starts_fresh(vicarius, runs_root):
    set_initials(vicarius, RUN_A, "AB")
    assert vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A)).status == 201
    old = runs_root / RUN_A
    old.rename(runs_root.parent / "archived_run")
    make_run(runs_root, RUN_A, "LO")
    assert vicarius.client.get("/api/species", headers=in_run(RUN_A)).json()["annotator"] == ""
    saved = vicarius.client.post("/api/observations", observation(), headers=in_run(RUN_A))
    assert saved.status == 400 and saved.error.startswith("annotator: empty")
    assert not (runs_root / RUN_A / "observations.csv").exists()


def test_only_an_image_may_name_its_run_in_the_query(vicarius, runs_root):
    set_initials(vicarius, RUN_A)
    posted = vicarius.client.post(f"/api/observations?run={RUN_A}", observation())
    assert posted.status == 409
    assert vicarius.client.get(f"/api/tracks?key={KEY_T1}&run={RUN_A}").status == 409
    assert vicarius.client.get(f"/api/species?run={RUN_A}").status == 409
    assert not (runs_root / RUN_A / "observations.csv").exists()


def test_a_linked_run_is_refused_over_http(vicarius, runs_root, tmp_path):
    outside = make_run(tmp_path / "elsewhere", "run_outside")
    os.symlink(outside, runs_root / "run_link")
    answer = vicarius.client.get("/api/species", headers=in_run("run_link"))
    assert answer.status == 400 and "is a link" in answer.error
    opened = vicarius.client.post("/api/project/open", {"project_dir": str(runs_root / "run_link")}, mutation_header=False)
    assert opened.status == 400


def test_a_run_name_with_an_accent_works_through_the_encoded_header_and_the_image_query(vicarius, runs_root):
    name = "run_26-10-27_Muñoz"
    make_run(runs_root, name)
    encoded = {"X-Screener-Run": "run_26-10-27_Mu%C3%B1oz"}
    opened = vicarius.client.post("/api/project/open", {"project_dir": str(runs_root / name)}, mutation_header=False)
    assert opened.json()["run"] == name
    assert vicarius.client.post("/api/settings", {"annotator": "LO"}, headers=encoded).status == 200
    row = vicarius.client.post("/api/observations", observation(), headers=encoded).json()["row"]
    assert (runs_root / name / "frames" / row["FrameFileName"]).is_file()
    crop = vicarius.client.get(f"/media/crops/{row['CropFileName']}?run=run_26-10-27_Mu%C3%B1oz")
    assert crop.status == 200 and crop.body == PNG


def test_an_encoded_header_cannot_smuggle_a_path(vicarius):
    answer = vicarius.client.get("/api/species", headers={"X-Screener-Run": "run_a%2F..%2F..%2Fetc"})
    assert answer.status == 400 and answer.error.startswith("run:")


def test_a_relative_project_dir_is_refused(runs_root):
    with pytest.raises(ValueError, match="not an absolute path"):
        RunFolders(runs_root).name_for_project_dir(f"inprocess/{RUN_A}")
