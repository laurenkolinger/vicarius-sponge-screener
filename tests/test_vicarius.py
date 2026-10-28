"""Tests for VICARIUS mode: config/vicarius.ini, the --vicarius options of screener.py, and the launch script.

Every test writes its own ini file under a temporary folder, so no test
creates anything under /mnt/tear or the real inprocess folder. The end-to-end
test runs screener.py --vicarius as a subprocess on port 0 against a FakeS3.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from screener import config
from screener.vicarius import VicariusConfigError, load_vicarius_paths
from tests.app_helpers import Client
from tests.fakes3 import FakeS3

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENTRY_POINT = PROJECT_ROOT / "screener.py"
LAUNCH_SCRIPT = PROJECT_ROOT / "scripts" / "launch_sponge_screener.sh"
SHIPPED_INI = PROJECT_ROOT / "config" / "vicarius.ini"
WAIT_SECONDS = 60.0


def write_ini(folder: Path, body: str) -> Path:
    """Write an ini file and return its path."""
    path = folder / "vicarius.ini"
    path.write_text(body, encoding="utf-8")
    return path


def load_entry_point():
    """Import screener.py (the script, not the package) as a module."""
    spec = importlib.util.spec_from_file_location("screener_entry_point_vicarius", ENTRY_POINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ----- load_vicarius_paths -----


def test_work_root_gives_the_cache_and_the_default_runs_folder(tmp_path):
    module = tmp_path / "modules" / "sponge_screener"
    module.mkdir(parents=True)
    ini = write_ini(tmp_path, f"[vicarius]\nwork_root = {tmp_path / 'work'}\n")
    paths = load_vicarius_paths(ini, module)
    assert paths.cache_dir == tmp_path / "work" / "cache"
    assert paths.runs_root == module / "inprocess"
    assert not (tmp_path / "work").exists()


def test_runs_root_setting_wins(tmp_path):
    ini = write_ini(tmp_path, f"[vicarius]\nwork_root = {tmp_path / 'work'}\nruns_root = {tmp_path / 'runs'}\n")
    assert load_vicarius_paths(ini, tmp_path / "module").runs_root == tmp_path / "runs"


def test_paths_are_normalized_and_spaces_trimmed(tmp_path):
    ini = write_ini(tmp_path, f"[vicarius]\nwork_root =   {tmp_path}/a/../work/   \n")
    assert load_vicarius_paths(ini, tmp_path / "module").cache_dir == tmp_path / "work" / "cache"


def test_shipped_ini_names_a_work_folder_off_the_synced_drive():
    """The committed ini points at /mnt/tear, outside the module folder (the parent may be absent on another machine)."""
    text = SHIPPED_INI.read_text(encoding="utf-8")
    assert "work_root = /mnt/tear/sponge_screener" in text
    assert "/mnt/rip/vicarius_drive" not in text.split("[vicarius]", 1)[1]


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("", "no \\[vicarius\\] section"),
        ("[other]\nwork_root = /tmp/x\n", "no \\[vicarius\\] section"),
        ("[vicarius]\n", "work_root: missing"),
        ("[vicarius]\nwork_root =\n", "work_root: blank"),
        ("[vicarius]\nwork_root = relative/folder\n", "not an absolute path"),
        ("[vicarius]\nwork_root = /tmp/a\x07b\n", "control character"),
        ("[vicarius]\nwork_root = /no-such-mount-xyz/sponge\n", "is that disk mounted"),
        ("[vicarius]\nwork_root = /tmp/x\nruns_root = runs\n", "runs_root"),
        ("not an ini file at all [[[\n", "unreadable"),
        ("[vicarius]\nwork_root = /tmp/a\nwork_root = /tmp/b\n", "unreadable"),
    ],
)
def test_bad_ini_files_are_refused_with_a_reason(tmp_path, body, fragment):
    ini = write_ini(tmp_path, body)
    with pytest.raises(VicariusConfigError, match=fragment):
        load_vicarius_paths(ini, tmp_path / "module")


def test_missing_ini_is_refused(tmp_path):
    with pytest.raises(VicariusConfigError, match="missing"):
        load_vicarius_paths(tmp_path / "absent.ini", tmp_path)


def test_binary_ini_is_refused(tmp_path):
    ini = tmp_path / "vicarius.ini"
    ini.write_bytes(b"\xff\xfe\x00[vicarius]")
    with pytest.raises(VicariusConfigError, match="unreadable"):
        load_vicarius_paths(ini, tmp_path)


@pytest.mark.parametrize("inside", ["", "github_repo/cache", "inprocess/cache"])
def test_work_root_inside_the_module_folder_is_refused(tmp_path, inside):
    module = tmp_path / "sponge_screener"
    (module / "github_repo").mkdir(parents=True)
    target = module / inside if inside else module
    ini = write_ini(tmp_path, f"[vicarius]\nwork_root = {target}\n")
    with pytest.raises(VicariusConfigError, match="drive sync copies"):
        load_vicarius_paths(ini, module)


def test_work_root_that_only_shares_a_prefix_is_allowed(tmp_path):
    module = tmp_path / "sponge"
    module.mkdir()
    ini = write_ini(tmp_path, f"[vicarius]\nwork_root = {tmp_path / 'sponge_work'}\n")
    assert load_vicarius_paths(ini, module).cache_dir == tmp_path / "sponge_work" / "cache"


# ----- parse_args -----


def test_standalone_defaults_are_unchanged():
    args = load_entry_point().parse_args([])
    assert args.port == config.PORT == 8770
    assert args.data_dir == config.data_dir()
    assert args.cache_dir == config.cache_dir()
    assert args.vicarius is False and args.no_browser is False


def test_vicarius_defaults_to_port_5098_without_a_browser():
    args = load_entry_point().parse_args(["--vicarius"])
    assert args.port == config.VICARIUS_PORT == 5098
    assert args.no_browser is True
    assert args.data_dir is None and args.cache_dir is None


def test_vicarius_keeps_an_explicit_port():
    assert load_entry_point().parse_args(["--vicarius", "--port", "5099"]).port == 5099


def test_vicarius_with_a_data_folder_is_an_error(capsys):
    with pytest.raises(SystemExit) as stopped:
        load_entry_point().parse_args(["--vicarius", "--data-dir", "/tmp/x"])
    assert stopped.value.code == 2
    assert "leave --data-dir out" in capsys.readouterr().err


def test_main_reports_a_bad_ini_and_exits_1(tmp_path, capsys):
    ini = write_ini(tmp_path, "[vicarius]\nwork_root = relative\n")
    code = load_entry_point().main(["--vicarius", "--vicarius-ini", str(ini), "--port", "0"])
    assert code == 1
    assert "could not start: work_root" in capsys.readouterr().err


# ----- the whole entry point -----


def test_vicarius_mode_serves_runs_end_to_end(tmp_path):
    """screener.py --vicarius on port 0: health says runs, project/open finds a run, the run saves settings."""
    fake = FakeS3()
    fake.start()
    runs = tmp_path / "inprocess"
    run = runs / "run_26-10-27_LO"
    run.mkdir(parents=True)
    (run / "project.json").write_text(json.dumps({"id": "26-10-27_LO", "steps": {}, "access_log": []}), encoding="utf-8")
    ini = write_ini(tmp_path, f"[vicarius]\nwork_root = {tmp_path / 'work'}\nruns_root = {runs}\n")
    env = dict(os.environ, VICARIUS_LOCKS_PATH=str(tmp_path / "no-locks.json"))
    process = subprocess.Popen(
        [sys.executable, str(ENTRY_POINT), "--vicarius", "--vicarius-ini", str(ini), "--port", "0", "--bucket-url", fake.url],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    try:
        line = process.stdout.readline()
        assert line.startswith("Sponge Screener listening on http://127.0.0.1:"), line
        port = int(line.rsplit(":", 1)[1].rstrip("/\n"))
        assert f"run folders under {runs}" in process.stdout.readline()
        client = Client(port)
        assert client.get("/api/health").json()["runs"] is True
        opened = client.post("/api/project/open", {"project_dir": str(run)}, mutation_header=False)
        assert opened.json() == {"run": run.name, "project_dir": str(run)}
        assert client.get("/api/species").status == 409
        saved = client.post("/api/settings", {"annotator": "LO"}, headers={"X-Screener-Run": run.name})
        assert saved.status == 200
        process.terminate()
        code = process.wait(timeout=WAIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=WAIT_SECONDS)
        fake.stop()
    assert code == 0
    assert json.loads((run / "settings.json").read_text())["annotator"] == "LO"
    assert (tmp_path / "work" / "cache").is_dir()
    assert "Traceback" not in process.stderr.read()


# ----- the launch script -----


def test_launch_script_is_executable_and_needs_a_port():
    assert os.access(LAUNCH_SCRIPT, os.X_OK)
    result = subprocess.run(["bash", str(LAUNCH_SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "port" in result.stderr


@pytest.mark.parametrize("port", ["abc", "70000", "-1", "5098;rm"])
def test_launch_script_refuses_a_bad_port(port):
    result = subprocess.run(["bash", str(LAUNCH_SCRIPT), port], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "port" in result.stderr


def test_launch_script_dry_run_prints_the_command():
    env = dict(os.environ, SPONGE_SCREENER_DRY_RUN="1")
    result = subprocess.run(["bash", str(LAUNCH_SCRIPT), "5098"], capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"/usr/bin/python3 {ENTRY_POINT} --vicarius --port 5098"
