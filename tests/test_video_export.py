"""Tests for the Export videos job, its mark positions, and the real rendering worker."""

import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from screener import config
from screener.mark_path import position_at
from screener.video_export import VideoExporter

MARK = {"label": "ID001 ACAU", "tier": "original", "time": 5.0, "point": {"x": 0.5, "y": 0.5}, "points": [[4.0, 0.1, 0.2, 1.0], [6.0, 0.3, 0.6, 1.0]]}


def test_position_follows_the_track():
    assert position_at(MARK, 5.0) == pytest.approx((0.2, 0.4))
    assert position_at(MARK, 4.0) == pytest.approx((0.1, 0.2))
    assert position_at(MARK, 6.0) == pytest.approx((0.3, 0.6))


def test_position_is_absent_outside_the_track():
    assert position_at(MARK, 3.0) is None
    assert position_at(MARK, 7.0) is None


def test_position_without_a_track_is_the_click_near_its_time():
    mark = {**MARK, "points": []}
    assert position_at(mark, 5.4) == (0.5, 0.5)
    assert position_at(mark, 5.7) is None


def make_worker(folder: Path, body: str) -> Path:
    """Write a stand-in worker that reads the request and runs the body."""
    path = folder / "worker.py"
    path.write_text("import json, sys\nrequest = json.load(sys.stdin)\n" + body, encoding="utf-8")
    return path


GOOD_WORKER = """
print("progress 0.5", flush=True)
open(request["output"], "wb").write(b"video")
"""


def make_job(tmp_path: Path, worker_body: str = GOOD_WORKER, videos=None) -> VideoExporter:
    """Build a job with a stand-in worker, package, and zip."""

    def build_package(folder: Path) -> Path:
        package = folder / "spongeGroundTruth_20261101"
        package.mkdir()
        (package / "observations.csv").write_text("x\n")
        return package

    def zip_package(folder: Path) -> Path:
        archive = folder.with_name(folder.name + ".zip")
        with zipfile.ZipFile(archive, "w") as output:
            for path in folder.rglob("*"):
                if path.is_file():
                    output.write(path, Path(folder.name) / path.relative_to(folder))
        return archive

    listed = [{"name": "a.MP4", "source": "a", "marks": [MARK]}] if videos is None else videos
    return VideoExporter(tmp_path / "out", Path(sys.executable), make_worker(tmp_path, worker_body), lambda: listed, build_package, zip_package)


def wait_until_finished(job: VideoExporter) -> dict:
    """Wait for the job to end and return its state."""
    for _ in range(200):
        status = job.status()
        if status["state"] != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("the job did not finish")


def test_job_puts_the_package_and_videos_in_one_zip(tmp_path: Path):
    job = make_job(tmp_path)
    assert job.archive() is None
    job.start()
    status = wait_until_finished(job)
    assert status["state"] == "done", status
    assert status["name"] == "spongeGroundTruth_with_videos_20261101.zip"
    with zipfile.ZipFile(job.archive()) as opened:
        names = opened.namelist()
    assert "spongeGroundTruth_20261101/observations.csv" in names
    assert "spongeGroundTruth_20261101/videos_tracked/a_tracked.mp4" in names


def test_unchanged_sightings_reuse_the_render(tmp_path: Path):
    job = make_job(tmp_path, GOOD_WORKER + '\nopen(request["output"] + ".ran", "w").write("x")\n')
    job.start()
    wait_until_finished(job)
    runs = list((tmp_path / "out" / "rendered").glob("*.ran"))
    for run in runs:
        run.unlink()
    job.start()
    assert wait_until_finished(job)["state"] == "done"
    assert list((tmp_path / "out" / "rendered").glob("*.ran")) == []


def test_changed_sightings_render_again_and_drop_the_old_file(tmp_path: Path):
    videos = [{"name": "a.MP4", "source": "a", "marks": [MARK]}]
    job = make_job(tmp_path, videos=videos)
    job.start()
    wait_until_finished(job)
    first = {item.name for item in (tmp_path / "out" / "rendered").glob("*.mp4")}
    videos[0]["marks"] = [{**MARK, "label": "ID001 AFUL"}]
    job.start()
    wait_until_finished(job)
    second = {item.name for item in (tmp_path / "out" / "rendered").glob("*.mp4")}
    assert len(second) == 1 and first != second


def test_failed_worker_reports_why_and_keeps_the_last_zip(tmp_path: Path):
    job = make_job(tmp_path)
    job.start()
    wait_until_finished(job)
    good = job.archive()
    assert good is not None
    broken = make_job(tmp_path, 'sys.stderr.write("disk full"); sys.exit(1)\n', videos=[{"name": "b.MP4", "source": "b", "marks": [MARK]}])
    broken.start()
    status = wait_until_finished(broken)
    assert status["state"] == "failed" and "disk full" in status["message"] and "b.MP4" in status["message"]
    assert good.exists()
    assert not list((tmp_path / "out").glob("building"))


def test_worker_that_dies_before_reading_still_reports_its_error(tmp_path: Path):
    worker = tmp_path / "dead.py"
    worker.write_text('import sys\nsys.stderr.write("No module named cv2")\nsys.exit(1)\n', encoding="utf-8")
    big = {"name": "a.MP4", "source": "a", "marks": [{**MARK, "points": [[index, 0.5, 0.5, 1.0] for index in range(100000)]}]}
    job = make_job(tmp_path, videos=[big])
    job._worker = worker
    job.start()
    status = wait_until_finished(job)
    assert status["state"] == "failed" and "No module named cv2" in status["message"]


def test_each_run_has_its_own_name_and_a_restart_has_none(tmp_path: Path):
    job = make_job(tmp_path)
    first = job.start()["run"]
    wait_until_finished(job)
    second = job.start()["run"]
    wait_until_finished(job)
    assert first and second and first != second
    assert make_job(tmp_path).status()["run"] is None


def test_a_new_job_offers_the_zip_from_an_earlier_run(tmp_path: Path):
    job = make_job(tmp_path)
    job.start()
    wait_until_finished(job)
    assert make_job(tmp_path).status()["state"] == "done"


def test_missing_python_is_refused(tmp_path: Path):
    job = VideoExporter(tmp_path, tmp_path / "absent", tmp_path / "w.py", list, lambda folder: folder, lambda folder: folder)
    with pytest.raises(ValueError, match="^videos"):
        job.start()


def test_second_start_while_running_keeps_one_job(tmp_path: Path):
    job = make_job(tmp_path, "import time; time.sleep(0.5)\n" + GOOD_WORKER)
    job.start()
    assert job.start()["state"] == "running"
    assert wait_until_finished(job)["state"] == "done"


@pytest.mark.skipif(not config.TRACK_PYTHON.exists() or not shutil.which("ffmpeg") and not Path("/opt/homebrew/bin/ffmpeg").exists(), reason="needs OpenCV and ffmpeg")
def test_real_worker_draws_the_label_on_the_tracked_position(tmp_path: Path):
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    clip = tmp_path / "clip.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    output = tmp_path / "out.mp4"
    mark = {"label": "ID001 ACAU", "tier": "original", "time": 1.0, "point": {"x": 0.25, "y": 0.5}, "points": [[0.0, 0.25, 0.5, 1.0], [2.0, 0.25, 0.5, 1.0]]}
    request = json.dumps({"source": str(clip), "output": str(output), "marks": [mark]})
    result = subprocess.run([str(config.TRACK_PYTHON), str(config.APP_ROOT / "screener" / "annotate_worker.py")], input=request, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert output.stat().st_size > 0
    frame = tmp_path / "frame.png"
    subprocess.run([ffmpeg, "-v", "error", "-ss", "1", "-i", str(output), "-frames:v", "1", str(frame)], check=True)
    probe = subprocess.run(
        [str(config.TRACK_PYTHON), "-c", "import cv2,sys; f=cv2.imread(sys.argv[1]); print(int((f[:, :, 1] > 150).sum()), f.shape[1], f.shape[0])", str(frame)],
        capture_output=True, text=True, check=True,
    )
    bright, width, height = (int(item) for item in probe.stdout.split())
    assert (width, height) == (640, 360)
    assert bright > 200, "no green label on the frame"
