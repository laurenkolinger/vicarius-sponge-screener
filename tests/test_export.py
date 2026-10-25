"""Tests for screener.export: the dated package, its files, its README, and its failure paths."""

import csv
import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from screener import export as export_module
from screener.export import ExportError, export_package
from screener.species import Species
from screener.store import OBSERVATION_COLUMNS, SCREENED_COLUMNS, ObservationStore, StoreError
from tests.store_helpers import identity_of

# The export tests bring their own species and sites, so edits to the shipped
# config files cannot change what these tests expect.
SPECIES = [
    Species(code="ACAU", name="Aplysina cauliformis", part="1", default_pin="1"),
    Species(code="AFUL", name="Aplysina fulva", part="2", default_pin="2"),
    Species(code="CDEL", name="Cliona delitrix", part="1", default_pin="3"),
    Species(code="XMUT", name="Xestospongia muta", part="2", default_pin="8"),
    Species(code="UNKN", name="Unknown sponge", part="", default_pin=""),
]
SITES = {"FLC": "Flat Cay", "BIT": "Buck Island"}

KEY_2024 = "TCRMP_video_ondeck/2024Annual/TCRMP20241022_video_FLC_T1.MP4"
KEY_2016_ODD = "TCRMP_video_ondeck/main/TCRMP2016_video/PeakBL/MVI_0203.MOV"
KEY_2009_MTS = "TCRMP_video_ondeck/main/TCRMP2009_video/TCRMP20090611_video_BIT_T1-6.MTS"
JPEG = b"\xff\xd8\xff\xe0" + b"full frame bytes"
PNG = b"\x89PNG\r\n\x1a\n" + b"crop bytes"
TODAY = date(2026, 9, 21)
FIXED_NOW = datetime(2026, 9, 21, 15, 30, 0, tzinfo=timezone.utc)


def add(store, key=KEY_2024, species_code="ACAU", note="", frame=JPEG, crop=PNG):
    """Add one sighting with fixed geometry and return its row."""
    return store.add(
        key=key,
        time_seconds=83.72,
        point={"x": 0.25, "y": 0.75},
        box=None,
        species_code=species_code,
        note=note,
        annotator="LO",
        frame_jpeg=frame,
        crop_png=crop,
    )


@pytest.fixture
def store(tmp_path):
    """A store on a temp data folder with a fixed clock."""
    return ObservationStore(tmp_path / "data", SPECIES, SITES, lambda: FIXED_NOW)


@pytest.fixture
def export_root(tmp_path):
    """An existing folder that stands in for the Drive folder."""
    root = tmp_path / "drive folder"
    root.mkdir()
    return root


@pytest.fixture
def filled(store):
    """A store with five sightings across three videos and a screening log."""
    store.mark_opened(KEY_2024, "LO", ["ACAU", "CDEL"])
    store.mark_opened(KEY_2009_MTS, "LO", ["ACAU", "CDEL"])
    add(store, frame=JPEG + b"1", crop=PNG + b"1")
    add(store, note='under a ledge, "big" one', frame=JPEG + b"2", crop=PNG + b"2")
    add(store, species_code="CDEL", frame=JPEG + b"3", crop=PNG + b"3")
    add(store, key=KEY_2016_ODD, frame=JPEG + b"4", crop=PNG + b"4")
    add(store, key=KEY_2009_MTS, species_code="XMUT", frame=JPEG + b"5", crop=PNG + b"5")
    store.mark_done(KEY_2024, True, "LO", ["ACAU", "CDEL"])
    return store


def names_in(folder):
    """Return the sorted names inside a folder."""
    return sorted(item.name for item in folder.iterdir())


def read_csv(path):
    """Parse a CSV file into a list of records."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_export_creates_dated_package_with_all_files(filled, export_root):
    package = export_package(filled, export_root, TODAY).path
    assert package == export_root / "spongeGroundTruth_20260921"
    assert names_in(package) == ["README.md", "crops", "frames", "observations.csv", "tracks.csv", "videos_screened.csv"]
    rows = filled.rows()
    assert names_in(package / "frames") == sorted(row["FrameFileName"] for row in rows)
    assert names_in(package / "crops") == sorted(row["CropFileName"] for row in rows)
    for row in rows:
        source = filled.frames_dir / row["FrameFileName"]
        assert (package / "frames" / row["FrameFileName"]).read_bytes() == source.read_bytes()
        source = filled.crops_dir / row["CropFileName"]
        assert (package / "crops" / row["CropFileName"]).read_bytes() == source.read_bytes()
    assert read_csv(package / "observations.csv") == [record[:-1] for record in read_csv(filled.observations_path)]
    assert (package / "videos_screened.csv").read_bytes() == filled.screened_path.read_bytes()
    exported = read_csv(package / "observations.csv")
    january = ["Site", "Transect", "Sponge Type", "Timestamp", "Notes", "ID", "FileName", "FrameFileName", "AbbreviatedNote"]
    assert exported[0][:9] == january
    assert len(exported) == 6
    assert exported[2][OBSERVATION_COLUMNS.index("Notes")] == 'bottom left, under a ledge, "big" one'


def add_later(store, key=KEY_2024, tag=b"L"):
    """Add one Later point (no species) and return its row."""
    return add(store, key=key, species_code=None, frame=JPEG + tag, crop=PNG + tag)


def test_export_leaves_out_later_points_and_their_images(filled, export_root):
    later_one = add_later(filled, tag=b"a")
    later_two = add_later(filled, key=KEY_2016_ODD, tag=b"b")
    only_later_video = add_later(filled, key="TCRMP_video_ondeck/2020Annual/TCRMP20200101_video_BIT_T2.MP4", tag=b"c")
    labeled = [row for row in filled.rows() if row["LabelStatus"] == "labeled"]
    assert len(labeled) == 5 and len(filled.rows()) == 8
    package = export_package(filled, export_root, TODAY).path
    assert names_in(package / "frames") == sorted(row["FrameFileName"] for row in labeled)
    assert names_in(package / "crops") == sorted(row["CropFileName"] for row in labeled)
    exported = read_csv(package / "observations.csv")
    id_at = export_module.EXPORT_COLUMNS.index("ID")
    assert [record[id_at] for record in exported[1:]] == [row["ID"] for row in labeled]
    for row in (later_one, later_two, only_later_video):
        assert row["ID"] not in {record[id_at] for record in exported[1:]}
        assert not (package / "frames" / row["FrameFileName"]).exists()
        assert not (package / "crops" / row["CropFileName"]).exists()
        assert (filled.frames_dir / row["FrameFileName"]).read_bytes().startswith(JPEG)
    screened = read_csv(package / "videos_screened.csv")
    sightings = {record[0]: record[SCREENED_COLUMNS.index("Sightings")] for record in screened[1:]}
    assert sightings == {KEY_2024: "3", KEY_2009_MTS: "1"}


def test_export_readme_counts_left_out_points(filled, export_root):
    add_later(filled, tag=b"a")
    add_later(filled, key=KEY_2016_ODD, tag=b"b")
    readme = (export_package(filled, export_root, TODAY).path / "README.md").read_text(encoding="utf-8")
    assert "- Unlabeled points left out: 2" in readme
    assert "Sightings: 5" in readme
    assert "Videos with at least one sighting: 3" in readme
    assert "one row per sponge sighting (5 rows)" in readme
    assert "| ACAU | Aplysina cauliformis | 3 | 2 | 2016 |" in readme
    assert "\u2014" not in readme and all(ord(char) < 128 for char in readme)
    add_later(filled, tag=b"c")
    again = (export_package(filled, export_root, TODAY).path / "README.md").read_text(encoding="utf-8")
    assert "- Unlabeled points left out: 3" in again
    lines = again.splitlines()
    assert lines.index("- Unlabeled points left out: 3") == lines.index("- Sightings: 5") + 3


def test_export_columns_drop_label_status(filled, export_root):
    expected = [column for column in OBSERVATION_COLUMNS if column != "LabelStatus"]
    assert export_module.EXPORT_COLUMNS == expected
    assert len(expected) == 22 and OBSERVATION_COLUMNS[-1] == "LabelStatus"
    january = ["Site", "Transect", "Sponge Type", "Timestamp", "Notes", "ID", "FileName", "FrameFileName", "AbbreviatedNote"]
    assert export_module.EXPORT_COLUMNS[:9] == january
    add_later(filled)
    package = export_package(filled, export_root, TODAY).path
    exported = read_csv(package / "observations.csv")
    assert exported[0] == expected
    assert all(len(record) == 22 for record in exported)
    assert "LabelStatus" not in (package / "observations.csv").read_text(encoding="utf-8")
    assert "LabelStatus" not in (package / "README.md").read_text(encoding="utf-8")


def test_export_only_later_points_raises(store, export_root):
    add_later(store, tag=b"a")
    add_later(store, tag=b"b")
    with pytest.raises(ExportError) as caught:
        export_package(store, export_root, TODAY)
    message = str(caught.value)
    assert message.startswith("no sightings to export: ")
    assert str(store.observations_path) in message
    assert "has 2 unlabeled point(s) and no labeled sightings yet" in message
    assert names_in(export_root) == []
    store.set_label("ID001", "ACAU")
    package = export_package(store, export_root, TODAY).path
    assert len(read_csv(package / "observations.csv")) == 2


def test_export_includes_a_relabeled_point_under_its_new_file_names(filled, export_root):
    later_row = add_later(filled)
    relabeled = filled.set_label(later_row["ID"], "XMUT")
    package = export_package(filled, export_root, TODAY).path
    assert relabeled["FrameFileName"] in names_in(package / "frames")
    assert later_row["FrameFileName"] not in names_in(package / "frames")
    assert (package / "crops" / relabeled["CropFileName"]).read_bytes() == PNG + b"L"
    readme = (package / "README.md").read_text(encoding="utf-8")
    assert "Sightings: 6" in readme and "Unlabeled points left out" not in readme


def test_export_takes_labeled_and_unlabeled_rows_from_one_read(filled, export_root, monkeypatch):
    original = filled.rows
    calls = []

    def reads_once(*args, **kwargs):
        """Count the reads of observations.csv the export makes."""
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    add_later(filled)
    monkeypatch.setattr(filled, "rows", reads_once)
    export_package(filled, export_root, TODAY)
    assert len(calls) == 1


def test_export_result_reports_unlabeled_left_out(filled, export_root):
    plain = export_package(filled, export_root, TODAY)
    assert isinstance(plain, export_module.ExportResult)
    assert plain.path == export_root / "spongeGroundTruth_20260921"
    assert plain.unlabeled_left_out == 0
    add_later(filled, tag=b"a")
    add_later(filled, key=KEY_2016_ODD, tag=b"b")
    result = export_package(filled, export_root, TODAY)
    path, left_out = result
    assert (path, left_out) == (result.path, result.unlabeled_left_out)
    assert (path, left_out) == (export_root / "spongeGroundTruth_20260921_2", 2)
    assert "- Unlabeled points left out: 2" in (path / "README.md").read_text(encoding="utf-8")


def test_export_leaves_the_store_untouched(filled, export_root):
    def snapshot():
        """Map every file under the data folder to its bytes."""
        return {
            str(path.relative_to(filled.data_dir)): path.read_bytes()
            for path in sorted(filled.data_dir.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    export_package(filled, export_root, TODAY)
    assert snapshot() == before


def test_second_export_same_day_gets_suffix(filled, export_root):
    first = export_package(filled, export_root, TODAY).path
    second = export_package(filled, export_root, TODAY).path
    third = export_package(filled, export_root, TODAY).path
    assert [first.name, second.name, third.name] == [
        "spongeGroundTruth_20260921",
        "spongeGroundTruth_20260921_2",
        "spongeGroundTruth_20260921_3",
    ]
    assert names_in(third) == names_in(first)
    other_day = export_package(filled, export_root, date(2026, 9, 22)).path
    assert other_day.name == "spongeGroundTruth_20260922"


def test_a_plain_file_with_the_package_name_is_stepped_over(filled, export_root):
    blocker = export_root / "spongeGroundTruth_20260921"
    blocker.write_text("not a folder", encoding="utf-8")
    package = export_package(filled, export_root, TODAY).path
    assert package.name == "spongeGroundTruth_20260921_2"
    assert blocker.read_text(encoding="utf-8") == "not a folder"


def test_missing_root_raises_with_path(filled, tmp_path):
    missing = tmp_path / "not mounted" / "Oceankind"
    with pytest.raises(ExportError) as caught:
        export_package(filled, missing, TODAY)
    assert str(missing) in str(caught.value)
    assert not missing.exists()
    assert not (tmp_path / "not mounted").exists()


def test_root_that_is_a_file_raises_with_path(filled, tmp_path):
    not_a_folder = tmp_path / "root.txt"
    not_a_folder.write_text("x", encoding="utf-8")
    with pytest.raises(ExportError) as caught:
        export_package(filled, not_a_folder, TODAY)
    assert str(not_a_folder) in str(caught.value)


def test_empty_store_raises(store, export_root):
    with pytest.raises(ExportError, match="no sightings"):
        export_package(store, export_root, TODAY)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    with pytest.raises(ExportError, match="no sightings"):
        export_package(store, export_root, TODAY)
    row = add(store)
    store.delete(row["ID"])
    with pytest.raises(ExportError, match="no sightings"):
        export_package(store, export_root, TODAY)
    assert names_in(export_root) == []


@pytest.mark.parametrize("folder, column", [("frames", "FrameFileName"), ("crops", "CropFileName")])
def test_missing_image_raises_and_names_file(filled, export_root, folder, column):
    lost = filled.rows()[2][column]
    (filled.data_dir / folder / lost).unlink()
    with pytest.raises(ExportError) as caught:
        export_package(filled, export_root, TODAY)
    assert lost in str(caught.value)
    assert "ID003" in str(caught.value)
    assert names_in(export_root) == []


def test_image_that_does_not_arrive_raises_names_the_file_and_removes_the_package(filled, export_root, monkeypatch):
    victim = filled.rows()[3]["CropFileName"]
    real_copy = shutil.copyfile

    def lossy_copy(source, target):
        """Copy every file except one, the way a flaky sync folder might."""
        if Path(source).name == victim:
            return target
        return real_copy(source, target)

    monkeypatch.setattr(shutil, "copyfile", lossy_copy)
    with pytest.raises(ExportError) as caught:
        export_package(filled, export_root, TODAY)
    assert victim in str(caught.value)
    assert names_in(export_root) == []


def test_short_copy_raises_and_removes_the_package(filled, export_root, monkeypatch):
    victim = filled.rows()[0]["FrameFileName"]
    real_copy = shutil.copyfile

    def short_copy(source, target):
        """Write a cut-off copy of one file."""
        if Path(source).name == victim:
            Path(target).write_bytes(Path(source).read_bytes()[:5])
            return target
        return real_copy(source, target)

    monkeypatch.setattr(shutil, "copyfile", short_copy)
    with pytest.raises(ExportError) as caught:
        export_package(filled, export_root, TODAY)
    assert victim in str(caught.value)
    assert names_in(export_root) == []


def test_copy_error_raises_with_file_and_reason_and_removes_the_package(filled, export_root, monkeypatch):
    def full_disk(source, target):
        """Stand in for shutil.copyfile on a full disk."""
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copyfile", full_disk)
    with pytest.raises(ExportError) as caught:
        export_package(filled, export_root, TODAY)
    assert "No space left" in str(caught.value)
    assert "ID001" in str(caught.value)
    assert names_in(export_root) == []


def test_csv_that_cannot_be_written_raises_and_removes_the_package(filled, export_root, monkeypatch):
    def refuse(path, columns, rows):
        """Stand in for the CSV writer on a folder that stops taking writes."""
        raise StoreError(f"{path}: could not write the file: quota exceeded")

    monkeypatch.setattr(export_module, "write_csv_atomic", refuse)
    with pytest.raises(ExportError) as caught:
        export_package(filled, export_root, TODAY)
    assert "observations.csv" in str(caught.value) and "quota exceeded" in str(caught.value)
    assert names_in(export_root) == []


def test_readme_that_cannot_be_written_raises_and_removes_the_package(filled, export_root, monkeypatch):
    def refuse(rows, screened, today, unlabeled_left_out, track_rows):
        """Stand in for a README write that hits a full disk."""
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(export_module, "_readme_text", refuse)
    with pytest.raises(ExportError) as caught:
        export_package(filled, export_root, TODAY)
    assert "README.md" in str(caught.value) and "No space left" in str(caught.value)
    assert names_in(export_root) == []


def test_too_many_packages_for_one_day_raises(filled, export_root, monkeypatch):
    monkeypatch.setattr(export_module, "MAX_PACKAGES_PER_DAY", 2)
    export_package(filled, export_root, TODAY)
    export_package(filled, export_root, TODAY)
    with pytest.raises(ExportError, match="already exist"):
        export_package(filled, export_root, TODAY)
    assert names_in(export_root) == ["spongeGroundTruth_20260921", "spongeGroundTruth_20260921_2"]


def test_unwritable_root_raises_with_path(filled, export_root):
    export_root.chmod(0o555)
    try:
        with pytest.raises(ExportError) as caught:
            export_package(filled, export_root, TODAY)
    finally:
        export_root.chmod(0o755)
    assert str(export_root) in str(caught.value)
    assert names_in(export_root) == []


def test_store_that_cannot_be_parsed_raises_export_error_naming_the_file(filled, export_root):
    filled.observations_path.write_text("Site,Transect\nx,y\n", encoding="utf-8")
    with pytest.raises(ExportError, match="observations.csv"):
        export_package(filled, export_root, TODAY)
    assert names_in(export_root) == []


def test_unreferenced_images_and_trash_stay_home(filled, export_root):
    (filled.frames_dir / "ID999_orphan.jpg").write_bytes(JPEG)
    removed = filled.delete("ID002")
    package = export_package(filled, export_root, TODAY).path
    exported_frames = names_in(package / "frames")
    assert "ID999_orphan.jpg" not in exported_frames
    assert removed["FrameFileName"] not in exported_frames
    assert len(exported_frames) == 4
    assert not (package / "trash").exists()
    assert len(read_csv(package / "observations.csv")) == 5


def test_screened_file_is_written_with_fresh_counts_even_without_a_log(store, export_root):
    add(store)
    package = export_package(store, export_root, TODAY).path
    assert read_csv(package / "videos_screened.csv") == [SCREENED_COLUMNS]
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    stale = store.screened_path.read_text(encoding="utf-8").replace(",1,LO,", ",77,LO,")
    store.screened_path.write_text(stale, encoding="utf-8")
    second = export_package(store, export_root, TODAY).path
    records = read_csv(second / "videos_screened.csv")
    assert records[1][SCREENED_COLUMNS.index("Sightings")] == "1"


def test_readme_lists_counts_and_every_column(filled, export_root):
    package = export_package(filled, export_root, TODAY).path
    readme = (package / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# ")
    assert "2026-09-21" in readme
    assert "| ACAU | Aplysina cauliformis | 3 | 2 | 2016 |" in readme
    assert "| CDEL | Cliona delitrix | 1 | 1 | 2024 |" in readme
    assert "| XMUT | Xestospongia muta | 1 | 1 | 2009 |" in readme
    assert "Sightings: 5" in readme
    assert "Videos with at least one sighting: 3" in readme
    assert "Videos in the screening log: 2 (1 done, 1 in progress)" in readme
    for column in export_module.EXPORT_COLUMNS + SCREENED_COLUMNS:
        assert f"| `{column}` |" in readme, column
    assert "LabelStatus" not in readme
    assert "Unlabeled points left out" not in readme
    lowered = readme.lower()
    assert "closest to the center of the frame" in lowered
    assert "true absence" in lowered
    assert "re-encoded" in lowered
    assert "1 sighting from videos in those formats" in lowered
    assert "cannot play AVI, M2T, MTS, MXF, or WMV files" in readme
    assert "utc" in lowered
    assert "512" in readme
    assert "\u2014" not in readme and "\u2013" not in readme
    assert all(ord(char) < 128 for char in readme)


def test_readme_reports_no_year_and_escapes_table_breakers(tmp_path, export_root):
    odd = [Species(code="ODDS", name="Odd | name", part="", default_pin="")]
    store = ObservationStore(tmp_path / "data", odd, SITES)
    add(store, key="TCRMP_video_ondeck/misc/MVI_0001.MOV", species_code="ODDS")
    readme = (export_package(store, export_root, TODAY).path / "README.md").read_text(encoding="utf-8")
    assert "| ODDS | Odd \\| name | 1 | 1 | none |" in readme
    assert "0 sightings from videos in those formats" in readme


def test_datetime_is_accepted_as_today(filled, export_root):
    package = export_package(filled, str(export_root), datetime(2026, 1, 5, 23, 59)).path
    assert package.name == "spongeGroundTruth_20260105"
    assert "2026-01-05" in (package / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "arguments, starts_with",
    [
        (("not a store", "ROOT", TODAY), "store:"),
        ((None, "ROOT", TODAY), "store:"),
        (("STORE", None, TODAY), "export_root:"),
        (("STORE", 7, TODAY), "export_root:"),
        (("STORE", "ROOT", "2026-09-21"), "today:"),
        (("STORE", "ROOT", None), "today:"),
        (("STORE", "ROOT", 20260921), "today:"),
    ],
)
def test_wrong_argument_types_raise_value_error(filled, export_root, arguments, starts_with):
    swapped = [filled if item == "STORE" else export_root if item == "ROOT" else item for item in arguments]
    with pytest.raises(ValueError) as caught:
        export_package(*swapped)
    assert str(caught.value).startswith(starts_with)
    assert names_in(export_root) == []


# ----- tracks.csv -----

TRACK_HEADER = ["ID", "SpeciesCode", "TimeSeconds", "X", "Y", "BoxX", "BoxY", "BoxW", "BoxH", "Score"]


def add_track(store, row, points=None, box=None):
    """Save a track for a row, the way the background job does, and return what was stored."""
    if points is None:
        points = [[83.0, 0.25, 0.75, 0.9], [83.72, 0.2512, 0.7488, 1.0]]
    return store.tracks.save(*identity_of(row), {"points": points, "box": box})


def track_records(package):
    """Read tracks.csv of a package as a list of records, the header first."""
    return read_csv(package / "tracks.csv")


def tracked_filled(filled):
    """Give three of the five sightings of the filled store a track and return their rows."""
    rows = filled.rows()
    add_track(filled, rows[0])
    add_track(filled, rows[2], points=[[10.0, 0.3, 0.4, 1.0], [10.5, 0.31, 0.41, 0.8]], box={"w": 0.2, "h": 0.1})
    add_track(filled, rows[4], points=[[5.5, 0.9, 0.1, 0.5]])
    return rows


def test_export_writes_tracks_csv_with_the_planned_columns_and_rows(filled, export_root):
    tracked_filled(filled)
    package = export_package(filled, export_root, TODAY).path
    assert export_module.TRACKS_FILE == "tracks.csv"
    assert export_module.TRACK_COLUMNS == TRACK_HEADER
    records = track_records(package)
    assert records[0] == TRACK_HEADER
    assert records[1:] == [
        ["ID001", "ACAU", "83.000", "0.2500", "0.7500", "", "", "", "", "0.9000"],
        ["ID001", "ACAU", "83.720", "0.2512", "0.7488", "", "", "", "", "1.0000"],
        ["ID003", "CDEL", "10.000", "0.3000", "0.4000", "0.2000", "0.3500", "0.2000", "0.1000", "1.0000"],
        ["ID003", "CDEL", "10.500", "0.3100", "0.4100", "0.2100", "0.3600", "0.2000", "0.1000", "0.8000"],
        ["ID005", "XMUT", "5.500", "0.9000", "0.1000", "", "", "", "", "0.5000"],
    ]
    assert (package / "tracks.csv").read_text(encoding="utf-8").splitlines()[0] == ",".join(TRACK_HEADER)


def test_export_tracks_csv_rounds_times_to_three_decimals_and_fractions_to_four(filled, export_root):
    add_track(filled, filled.rows()[0], points=[[83.1236, 0.12346, 0.98764, 0.99996], [100.0, 0.0, 1.0, 0.0]])
    records = track_records(export_package(filled, export_root, TODAY).path)
    assert records[1] == ["ID001", "ACAU", "83.124", "0.1235", "0.9876", "", "", "", "", "1.0000"]
    assert records[2] == ["ID001", "ACAU", "100.000", "0.0000", "1.0000", "", "", "", "", "0.0000"]


def test_export_tracks_csv_follows_the_order_of_the_sightings_then_time(filled, export_root):
    rows = filled.rows()
    add_track(filled, rows[4], points=[[1.0, 0.1, 0.1, 1.0], [2.0, 0.2, 0.2, 1.0]])
    add_track(filled, rows[1], points=[[9.0, 0.3, 0.3, 1.0], [10.0, 0.4, 0.4, 1.0]])
    records = track_records(export_package(filled, export_root, TODAY).path)
    assert [(record[0], record[2]) for record in records[1:]] == [
        ("ID002", "9.000"), ("ID002", "10.000"), ("ID005", "1.000"), ("ID005", "2.000"),
    ]  # fmt: skip


def test_export_tracks_csv_has_only_the_header_when_nothing_is_tracked(filled, export_root):
    package = export_package(filled, export_root, TODAY).path
    assert track_records(package) == [TRACK_HEADER]
    assert (package / "tracks.csv").read_text(encoding="utf-8") == ",".join(TRACK_HEADER) + "\n"


def test_export_tracks_csv_leaves_out_later_points(filled, export_root):
    later_row = add_later(filled)
    add_track(filled, later_row)
    add_track(filled, filled.rows()[0])
    records = track_records(export_package(filled, export_root, TODAY).path)
    assert {record[0] for record in records[1:]} == {"ID001"}
    assert len(records) == 3


def test_export_ignores_a_damaged_track_of_a_later_point(filled, export_root):
    later_row = add_later(filled)
    add_track(filled, later_row)
    (filled.tracks_dir / (later_row["ID"] + ".json")).write_bytes(b"{nope")
    package = export_package(filled, export_root, TODAY).path
    assert track_records(package) == [TRACK_HEADER]


def test_export_tracks_csv_includes_a_point_labeled_after_it_was_tracked(filled, export_root):
    later_row = add_later(filled)
    add_track(filled, later_row, points=[[3.0, 0.5, 0.5, 1.0]])
    assert track_records(export_package(filled, export_root, TODAY).path) == [TRACK_HEADER]
    filled.set_label(later_row["ID"], "XMUT")
    records = track_records(export_package(filled, export_root, TODAY).path)
    assert records[1:] == [[later_row["ID"], "XMUT", "3.000", "0.5000", "0.5000", "", "", "", "", "1.0000"]]


def test_export_tracks_csv_centers_the_box_on_every_point(filled, export_root):
    points = [[1.0, 0.5, 0.5, 1.0], [2.0, 0.02, 0.04, 0.9], [3.0, 0.99, 0.97, 0.8], [4.0, 0.14999, 0.5, 0.7]]
    add_track(filled, filled.rows()[0], points=points, box={"w": 0.3, "h": 0.1})
    records = track_records(export_package(filled, export_root, TODAY).path)[1:]
    assert [record[5:9] for record in records] == [
        ["0.3500", "0.4500", "0.3000", "0.1000"],
        ["-0.1300", "-0.0100", "0.3000", "0.1000"],
        ["0.8400", "0.9200", "0.3000", "0.1000"],
        ["0.0000", "0.4500", "0.3000", "0.1000"],
    ]
    for record in records:
        centers = (float(record[5]) + float(record[7]) / 2, float(record[6]) + float(record[8]) / 2)
        assert centers == pytest.approx((float(record[3]), float(record[4])), abs=1e-4)


def test_export_tracks_csv_never_writes_a_negative_zero(filled, export_root):
    points = [[index * 0.5, 0.15 - index * 1e-6, 0.05 - index * 1e-7, 0.0] for index in range(1, 40)]
    add_track(filled, filled.rows()[0], points=points, box={"w": 0.3, "h": 0.1})
    text = (export_package(filled, export_root, TODAY).path / "tracks.csv").read_text(encoding="utf-8")
    assert "-0.0000" not in text
    assert "0.0000" in text


def stale_track(filled, row, **changes):
    """Rewrite fields of the track file of a row, as if the file had been saved for another mark."""
    path = filled.tracks_dir / (row["ID"] + ".json")
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_export_skips_a_track_saved_for_another_moment_and_names_its_file(filled, export_root, capsys):
    rows = tracked_filled(filled)
    path = stale_track(filled, rows[2], anchor_time=12.0)
    before = path.read_bytes()
    capsys.readouterr()
    package = export_package(filled, export_root, TODAY).path
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("export: skipped ") and str(path) in lines[0] and "ID003" in lines[0]
    assert "12.000" in lines[0] and "83.720" in lines[0]
    records = track_records(package)
    assert {record[0] for record in records[1:]} == {"ID001", "ID005"}
    assert len(records) == 4
    readme = (package / "README.md").read_text(encoding="utf-8")
    assert "(3 rows for 2 sightings)" in readme
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "anchor, kept",
    [(83.72, True), (83.7204, True), (83.7196, True), (83.7206, False), (83.7194, False), (0.0, False), (84.0, False)],
)
def test_export_keeps_a_track_whose_anchor_rounds_to_the_rows_time(filled, export_root, capsys, anchor, kept):
    row = filled.rows()[0]
    add_track(filled, row)
    stale_track(filled, row, anchor_time=anchor)
    capsys.readouterr()
    records = track_records(export_package(filled, export_root, TODAY).path)
    assert (len(records) > 1) is kept
    assert (capsys.readouterr().err == "") is kept


def test_export_with_only_stale_tracks_writes_the_header_and_counts_none(filled, export_root, capsys):
    rows = tracked_filled(filled)
    for row in (rows[0], rows[2], rows[4]):
        stale_track(filled, row, anchor_time=1.0)
    capsys.readouterr()
    package = export_package(filled, export_root, TODAY).path
    assert len(capsys.readouterr().err.splitlines()) == 3
    assert track_records(package) == [TRACK_HEADER]
    assert "(0 rows for 0 sightings)" in (package / "README.md").read_text(encoding="utf-8")


def test_export_fails_loudly_on_a_damaged_track_file_and_writes_nothing(filled, export_root):
    rows = tracked_filled(filled)
    damaged = filled.tracks_dir / (rows[2]["ID"] + ".json")
    for content in (b"{nope", b"", b'{"id": "ID003", "points": []}', damaged.read_bytes().replace(b"0.31", b"7.31")):
        damaged.write_bytes(content)
        with pytest.raises(ExportError) as caught:
            export_package(filled, export_root, TODAY)
        assert str(damaged) in str(caught.value) and "ID003" in str(caught.value)
        assert "Nothing was exported" in str(caught.value)
        assert names_in(export_root) == []
        assert damaged.read_bytes() == content


def test_export_fails_when_a_track_file_is_a_folder(filled, export_root):
    row = filled.rows()[0]
    (filled.tracks_dir / (row["ID"] + ".json")).mkdir(parents=True)
    with pytest.raises(ExportError, match="ID001.json"):
        export_package(filled, export_root, TODAY)
    assert names_in(export_root) == []


def test_export_removes_the_package_when_tracks_csv_cannot_be_written(filled, export_root, monkeypatch):
    real_write = export_module.write_csv_atomic

    def refuse_tracks(path, columns, rows):
        """Stand in for a folder that stops taking writes once tracks.csv comes up."""
        if Path(path).name == "tracks.csv":
            raise StoreError(f"{path}: could not write the file: quota exceeded")
        return real_write(path, columns, rows)

    monkeypatch.setattr(export_module, "write_csv_atomic", refuse_tracks)
    with pytest.raises(ExportError) as caught:
        export_package(filled, export_root, TODAY)
    assert "tracks.csv" in str(caught.value) and "quota exceeded" in str(caught.value)
    assert names_in(export_root) == []


def test_export_reads_tracks_without_changing_any_file(filled, export_root):
    tracked_filled(filled)

    def snapshot():
        """Map every file under the data folder to its bytes."""
        return {
            str(path.relative_to(filled.data_dir)): path.read_bytes()
            for path in sorted(filled.data_dir.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    assert any(name.startswith("tracks/") for name in before)
    export_package(filled, export_root, TODAY)
    assert snapshot() == before


def test_export_takes_the_rows_from_one_read_even_with_tracks(filled, export_root, monkeypatch):
    tracked_filled(filled)
    original = filled.rows
    calls = []

    def reads_once(*args, **kwargs):
        """Count the reads of observations.csv the export makes."""
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(filled, "rows", reads_once)
    export_package(filled, export_root, TODAY)
    assert len(calls) == 1


def test_readme_describes_tracks_csv(filled, export_root):
    tracked_filled(filled)
    readme = (export_package(filled, export_root, TODAY).path / "README.md").read_text(encoding="utf-8")
    assert "- `tracks.csv`: one row per track point of each sighting that has a track (5 rows for 3 sightings)." in readme
    start = readme.index("## Columns of tracks.csv")
    end = readme.index("## Columns of videos_screened.csv")
    assert readme.index("## Columns of observations.csv") < start < end
    section = readme[start:end]
    for column in TRACK_HEADER:
        assert f"| `{column}` |" in section, column
    lowered = section.lower()
    assert "centered on x and y" in lowered
    assert "empty" in lowered and "point" in lowered
    assert "edge of the frame" in lowered
    assert "labeled sighting" in lowered
    assert "\u2014" not in readme and "\u2013" not in readme and all(ord(char) < 128 for char in readme)


def test_readme_counts_zero_tracks(filled, export_root):
    readme = (export_package(filled, export_root, TODAY).path / "README.md").read_text(encoding="utf-8")
    assert "- `tracks.csv`: one row per track point of each sighting that has a track (0 rows for 0 sightings)." in readme
    assert "## Columns of tracks.csv" in readme


def test_readme_counts_one_track_in_the_singular(filled, export_root):
    add_track(filled, filled.rows()[0], points=[[3.0, 0.5, 0.5, 1.0]])
    readme = (export_package(filled, export_root, TODAY).path / "README.md").read_text(encoding="utf-8")
    assert "(1 row for 1 sighting)." in readme


def test_module_exposes_the_planned_interface():
    assert {"ExportError", "ExportResult", "EXPORT_COLUMNS", "TRACK_COLUMNS", "TRACKS_FILE", "export_package"} <= set(
        dir(export_module)
    )
    assert issubclass(ExportError, Exception)
