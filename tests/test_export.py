"""Tests for screener.export: the dated package, its files, its README, and its failure paths."""

import csv
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from screener import export as export_module
from screener.export import ExportError, export_package
from screener.species import Species
from screener.store import OBSERVATION_COLUMNS, SCREENED_COLUMNS, ObservationStore, StoreError

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
    package = export_package(filled, export_root, TODAY)
    assert package == export_root / "spongeGroundTruth_20260921"
    assert names_in(package) == ["README.md", "crops", "frames", "observations.csv", "videos_screened.csv"]
    rows = filled.rows()
    assert names_in(package / "frames") == sorted(row["FrameFileName"] for row in rows)
    assert names_in(package / "crops") == sorted(row["CropFileName"] for row in rows)
    for row in rows:
        source = filled.frames_dir / row["FrameFileName"]
        assert (package / "frames" / row["FrameFileName"]).read_bytes() == source.read_bytes()
        source = filled.crops_dir / row["CropFileName"]
        assert (package / "crops" / row["CropFileName"]).read_bytes() == source.read_bytes()
    assert (package / "observations.csv").read_bytes() == filled.observations_path.read_bytes()
    assert (package / "videos_screened.csv").read_bytes() == filled.screened_path.read_bytes()
    exported = read_csv(package / "observations.csv")
    january = ["Site", "Transect", "Sponge Type", "Timestamp", "Notes", "ID", "FileName", "FrameFileName", "AbbreviatedNote"]
    assert exported[0][:9] == january
    assert len(exported) == 6
    assert exported[2][OBSERVATION_COLUMNS.index("Notes")] == 'bottom left, under a ledge, "big" one'


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
    first = export_package(filled, export_root, TODAY)
    second = export_package(filled, export_root, TODAY)
    third = export_package(filled, export_root, TODAY)
    assert [first.name, second.name, third.name] == [
        "spongeGroundTruth_20260921",
        "spongeGroundTruth_20260921_2",
        "spongeGroundTruth_20260921_3",
    ]
    assert names_in(third) == names_in(first)
    other_day = export_package(filled, export_root, date(2026, 9, 22))
    assert other_day.name == "spongeGroundTruth_20260922"


def test_a_plain_file_with_the_package_name_is_stepped_over(filled, export_root):
    blocker = export_root / "spongeGroundTruth_20260921"
    blocker.write_text("not a folder", encoding="utf-8")
    package = export_package(filled, export_root, TODAY)
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
    def refuse(rows, screened, today):
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
    package = export_package(filled, export_root, TODAY)
    exported_frames = names_in(package / "frames")
    assert "ID999_orphan.jpg" not in exported_frames
    assert removed["FrameFileName"] not in exported_frames
    assert len(exported_frames) == 4
    assert not (package / "trash").exists()
    assert len(read_csv(package / "observations.csv")) == 5


def test_screened_file_is_written_with_fresh_counts_even_without_a_log(store, export_root):
    add(store)
    package = export_package(store, export_root, TODAY)
    assert read_csv(package / "videos_screened.csv") == [SCREENED_COLUMNS]
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    stale = store.screened_path.read_text(encoding="utf-8").replace(",1,LO,", ",77,LO,")
    store.screened_path.write_text(stale, encoding="utf-8")
    second = export_package(store, export_root, TODAY)
    records = read_csv(second / "videos_screened.csv")
    assert records[1][SCREENED_COLUMNS.index("Sightings")] == "1"


def test_readme_lists_counts_and_every_column(filled, export_root):
    package = export_package(filled, export_root, TODAY)
    readme = (package / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# ")
    assert "2026-09-21" in readme
    assert "| ACAU | Aplysina cauliformis | 3 | 2 | 2016 |" in readme
    assert "| CDEL | Cliona delitrix | 1 | 1 | 2024 |" in readme
    assert "| XMUT | Xestospongia muta | 1 | 1 | 2009 |" in readme
    assert "Sightings: 5" in readme
    assert "Videos with at least one sighting: 3" in readme
    assert "Videos in the screening log: 2 (1 done, 1 in progress)" in readme
    for column in OBSERVATION_COLUMNS + SCREENED_COLUMNS:
        assert f"| `{column}` |" in readme, column
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
    readme = (export_package(store, export_root, TODAY) / "README.md").read_text(encoding="utf-8")
    assert "| ODDS | Odd \\| name | 1 | 1 | none |" in readme
    assert "0 sightings from videos in those formats" in readme


def test_datetime_is_accepted_as_today(filled, export_root):
    package = export_package(filled, str(export_root), datetime(2026, 1, 5, 23, 59))
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


def test_module_exposes_the_planned_interface():
    assert {"ExportError", "export_package"} <= set(dir(export_module))
    assert issubclass(ExportError, Exception)
