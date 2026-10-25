"""Tests for screener.store: sightings, images, the screened-video log, and their failure paths."""

import ast
import csv
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from screener import store as store_module
from screener.config import MAX_IMAGE_BYTES, MAX_NOTE_LENGTH, MAX_TIME_SECONDS
from screener.keys import InvalidKey
from screener.species import Species
from screener.store import OBSERVATION_COLUMNS, SCREENED_COLUMNS, ObservationStore, StoreError, UnknownObservation
from tests.store_helpers import (
    JPEG,
    KEY_2024,
    KEY_2024_T2,
    PNG,
    SITES,
    SPECIES,
    FixedClock,
    files_in,
    hold_lock,
    identity_of,
    make_store,
    release_lock,
    sighting,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KEY_MULTI = "TCRMP_video_ondeck/main/TCRMP2005_video/TCRMP20051013_video_SSJ_T1+T3-6.mp4"
KEY_2016_ODD = "TCRMP_video_ondeck/main/TCRMP2016_video/PeakBL/MVI_0203.MOV"
KEY_NO_YEAR = "TCRMP_video_ondeck/misc/MVI_0001.MOV"

FORMULA_LEADERS = ("=", "+", "-", "@")


def read_csv(path):
    """Parse a CSV file into a list of records."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def stray_files(data_dir):
    """Return names in the data folder that are not part of the planned layout."""
    planned = {"observations.csv", "videos_screened.csv", "frames", "crops", "tracks", "trash", ".lock"}
    return sorted(item.name for item in data_dir.iterdir() if item.name not in planned)


def test_columns_are_the_planned_lists():
    assert OBSERVATION_COLUMNS == [
        "Site", "Transect", "Sponge Type", "Timestamp", "Notes", "ID", "FileName", "FrameFileName", "AbbreviatedNote",
        "SpeciesCode", "TimestampSeconds", "Quadrant", "PointX", "PointY", "BoxX", "BoxY", "BoxW", "BoxH",
        "CropFileName", "S3Key", "Annotator", "LoggedAt", "LabelStatus",
    ]  # fmt: skip
    assert store_module.LEGACY_OBSERVATION_COLUMNS == OBSERVATION_COLUMNS[:-1]
    assert len(store_module.LEGACY_OBSERVATION_COLUMNS) == 22
    assert (store_module.LABEL_STATUS_COLUMN, store_module.LABELED, store_module.UNLABELED) == (
        "LabelStatus", "labeled", "unlabeled",
    )
    assert store_module.LATER_FILE_CODE == "LATER"
    assert SCREENED_COLUMNS == [
        "S3Key", "FileName", "Status", "TargetSpecies", "Sightings", "Annotator", "FirstOpened", "MarkedDone",
    ]  # fmt: skip


def test_add_writes_row_and_images(tmp_path):
    store = make_store(tmp_path, FixedClock())
    row = store.add(**sighting())
    assert row == {
        "Site": "Flat Cay",
        "Transect": "T1",
        "Sponge Type": "Aplysina cauliformis",
        "Timestamp": "01:23",
        "Notes": "bottom left",
        "ID": "ID001",
        "FileName": "TCRMP20241022_video_FLC_T1.MP4",
        "FrameFileName": "ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg",
        "AbbreviatedNote": "BOTTOMLEFT",
        "SpeciesCode": "ACAU",
        "TimestampSeconds": "83.720",
        "Quadrant": "BOTTOMLEFT",
        "PointX": "0.2500",
        "PointY": "0.7500",
        "BoxX": "",
        "BoxY": "",
        "BoxW": "",
        "BoxH": "",
        "CropFileName": "ID001_ACAU_BOTTOMLEFT_FLC_T1.png",
        "S3Key": KEY_2024,
        "Annotator": "LO",
        "LoggedAt": "2026-09-21T15:30:00Z",
        "LabelStatus": "labeled",
    }
    data_dir = tmp_path / "data"
    assert (data_dir / "frames" / "ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg").read_bytes() == JPEG
    assert (data_dir / "crops" / "ID001_ACAU_BOTTOMLEFT_FLC_T1.png").read_bytes() == PNG
    records = read_csv(data_dir / "observations.csv")
    assert records[0] == OBSERVATION_COLUMNS
    assert records[1] == [row[column] for column in OBSERVATION_COLUMNS]
    assert len(records) == 2
    assert store.rows() == [row]
    assert stray_files(data_dir) == []


def test_first_nine_columns_match_january_header(tmp_path):
    january = ["Site", "Transect", "Sponge Type", "Timestamp", "Notes", "ID", "FileName", "FrameFileName", "AbbreviatedNote"]
    assert OBSERVATION_COLUMNS[:9] == january
    store = make_store(tmp_path)
    store.add(**sighting())
    first_line = (tmp_path / "data" / "observations.csv").read_text(encoding="utf-8").splitlines()[0]
    assert first_line.split(",")[:9] == january
    assert not first_line.startswith("\ufeff")


def test_ids_increment_and_survive_reload(tmp_path):
    store = make_store(tmp_path)
    assert [store.add(**sighting())["ID"] for _ in range(3)] == ["ID001", "ID002", "ID003"]
    reopened = make_store(tmp_path)
    assert reopened.add(**sighting())["ID"] == "ID004"
    assert [row["ID"] for row in reopened.rows()] == ["ID001", "ID002", "ID003", "ID004"]


def test_next_id_is_highest_plus_one_and_grows_past_three_digits(tmp_path):
    store = make_store(tmp_path)
    for _ in range(3):
        store.add(**sighting())
    store.delete("ID002")
    assert store.add(**sighting())["ID"] == "ID004"
    store.delete("ID004")
    assert store.add(**sighting())["ID"] == "ID004"
    path = tmp_path / "data" / "observations.csv"
    records = read_csv(path)
    last = list(records[-1])
    id_at, frame_at, crop_at = (OBSERVATION_COLUMNS.index(name) for name in ("ID", "FrameFileName", "CropFileName"))
    last[id_at], last[frame_at], last[crop_at] = "ID999", "ID999_x.jpg", "ID999_x.png"
    with open(path, "a", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerow(last)
    assert store.add(**sighting())["ID"] == "ID1000"
    assert store.add(**sighting())["ID"] == "ID1001"


def test_box_sets_quadrant_from_box_center_and_fills_box_cells(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting(point={"x": 0.1, "y": 0.1}, box={"x": 0.55, "y": 0.6, "w": 0.3, "h": 0.25}))
    assert row["Quadrant"] == "BOTTOMRIGHT"
    assert row["AbbreviatedNote"] == "BOTTOMRIGHT"
    assert row["Notes"] == "bottom right"
    assert row["FrameFileName"] == "ID001_ACAU_BOTTOMRIGHT_FLC_T1.jpg"
    assert (row["PointX"], row["PointY"]) == ("0.1000", "0.1000")
    assert (row["BoxX"], row["BoxY"], row["BoxW"], row["BoxH"]) == ("0.5500", "0.6000", "0.3000", "0.2500")
    straddling = store.add(**sighting(point={"x": 0.9, "y": 0.9}, box={"x": 0.0, "y": 0.0, "w": 0.6, "h": 0.6}))
    assert straddling["Quadrant"] == "TOPLEFT"


def test_note_is_cleaned_and_appended_after_quadrant_phrase(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting(note="  under\tthe   ledge,\r\n next to\x00 fan\u2028coral \x1b[31m "))
    assert row["Notes"] == "bottom left, under the ledge, next to fan coral [31m"
    assert row["AbbreviatedNote"] == "BOTTOMLEFT"
    lone_surrogate = store.add(**sighting(note="half\ud83ean emoji"))
    assert lone_surrogate["Notes"] == "bottom left, half an emoji"
    blank = store.add(**sighting(note=" \n\t "))
    assert blank["Notes"] == "bottom left"
    longest = store.add(**sighting(note="  " + "n" * MAX_NOTE_LENGTH + "\n"))
    assert longest["Notes"] == "bottom left, " + "n" * MAX_NOTE_LENGTH


def test_nonstandard_key_writes_blank_site_and_NA_file_parts(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting(key=KEY_2016_ODD, species_code="CDEL", point={"x": 0.8, "y": 0.2}))
    assert row["Site"] == ""
    assert row["Transect"] == ""
    assert row["FileName"] == "MVI_0203.MOV"
    assert row["FrameFileName"] == "ID001_CDEL_TOPRIGHT_NA_NA.jpg"
    assert row["CropFileName"] == "ID001_CDEL_TOPRIGHT_NA_NA.png"
    assert row["S3Key"] == KEY_2016_ODD
    assert files_in(tmp_path / "data" / "frames") == ["ID001_CDEL_TOPRIGHT_NA_NA.jpg"]


def test_multi_transect_label_is_file_safe_in_file_names_and_verbatim_in_csv(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting(key=KEY_MULTI, point={"x": 0.1, "y": 0.1}))
    assert row["Transect"] == "T1+T3-6"
    assert row["Site"] == "SSJ"
    assert row["FileName"] == "TCRMP20051013_video_SSJ_T1+T3-6.mp4"
    assert row["FrameFileName"] == "ID001_ACAU_TOPLEFT_SSJ_T1-T3-6.jpg"
    assert row["CropFileName"] == "ID001_ACAU_TOPLEFT_SSJ_T1-T3-6.png"
    dotted = store.add(**sighting(key="TCRMP_video_ondeck/main/TCRMP20110419_video_GBF_T5.2-6.avi"))
    assert dotted["Transect"] == "T5.2-6"
    assert dotted["FrameFileName"] == "ID002_ACAU_BOTTOMLEFT_GBF_T5-2-6.jpg"
    assert make_store(tmp_path).rows()[0]["Transect"] == "T1+T3-6"


def test_hostile_transect_label_cannot_leave_the_image_folders(tmp_path):
    store = make_store(tmp_path)
    key = "TCRMP_video_ondeck/x/TCRMP20241022_video_FLC_..%2f..%2fetc passwd<script>.MP4"
    row = store.add(**sighting(key=key))
    assert row["FrameFileName"] == "ID001_ACAU_BOTTOMLEFT_FLC_2f-2fetc-passwd-script.jpg"
    assert files_in(tmp_path / "data" / "frames") == [row["FrameFileName"]]
    assert row["Transect"] == "..%2f..%2fetc passwd<script>"


def test_long_transect_label_is_capped_in_file_names(tmp_path):
    store = make_store(tmp_path)
    label = "T" + "9" * 900
    row = store.add(**sighting(key=f"TCRMP_video_ondeck/x/TCRMP20241022_video_FLC_{label}.MP4"))
    assert row["Transect"] == label
    assert len(row["FrameFileName"]) < 140
    assert (tmp_path / "data" / "frames" / row["FrameFileName"]).exists()
    assert (tmp_path / "data" / "crops" / row["CropFileName"]).exists()


def test_rejects_unknown_species(tmp_path):
    store = make_store(tmp_path)
    for bad in ("ZZZZ", "acau", "", 7, ["ACAU"]):
        with pytest.raises(ValueError, match="^species_code:"):
            store.add(**sighting(species_code=bad))
    assert store.rows() == []
    assert files_in(tmp_path / "data" / "frames") == []


@pytest.mark.parametrize(
    "field, value, starts_with",
    [
        ("time_seconds", -0.001, "time_seconds:"),
        ("time_seconds", MAX_TIME_SECONDS + 0.001, "time_seconds:"),
        ("time_seconds", float("nan"), "time_seconds:"),
        ("time_seconds", float("inf"), "time_seconds:"),
        ("time_seconds", "83.7", "time_seconds:"),
        ("time_seconds", None, "time_seconds:"),
        ("time_seconds", True, "time_seconds:"),
        ("time_seconds", 10 ** 400, "time_seconds:"),
        ("point", None, "point:"),
        ("point", {"x": 0.5}, "point.y:"),
        ("point", {"x": 1.2, "y": 0.5}, "point.x:"),
        ("point", {"x": float("nan"), "y": 0.5}, "point.x:"),
        ("point", [0.5, 0.5], "point:"),
        ("box", {"x": 0.5, "y": 0.5, "w": 0.0, "h": 0.1}, "box.w:"),
        ("box", {"x": 0.9, "y": 0.5, "w": 0.5, "h": 0.1}, "box.w:"),
        ("box", {"x": 0.5, "y": 0.5, "w": 0.1}, "box.h:"),
        ("box", "0,0,1,1", "box:"),
        ("note", "n" * (MAX_NOTE_LENGTH + 1), "note:"),
        ("note", None, "note:"),
        ("note", 12, "note:"),
        ("note", ["a"], "note:"),
        ("annotator", "", "annotator:"),
        ("annotator", "way too long name", "annotator:"),
        ("annotator", "L O", "annotator:"),
        ("annotator", None, "annotator:"),
        ("key", "other/TCRMP20241022_video_FLC_T1.MP4", "key:"),
        ("key", "TCRMP_video_ondeck/../secret.mp4", "key:"),
        ("key", "TCRMP_video_ondeck/a\\b.mp4", "key:"),
        ("key", "TCRMP_video_ondeck/a\nb.mp4", "key:"),
        ("key", "TCRMP_video_ondeck/folder/", "key:"),
        ("key", "TCRMP_video_ondeck/" + "k" * 1024, "key:"),
        ("key", "TCRMP_video_ondeck/a\ud800b.mp4", "key:"),
        ("key", None, "key:"),
        ("key", 5, "key:"),
    ],
)
def test_rejects_bad_time_point_box_note_annotator_key(tmp_path, field, value, starts_with):
    store = make_store(tmp_path)
    with pytest.raises(ValueError) as caught:
        store.add(**sighting(**{field: value}))
    assert str(caught.value).startswith(starts_with)
    assert not isinstance(caught.value, StoreError)
    assert store.rows() == []
    assert files_in(tmp_path / "data" / "frames") == []
    assert files_in(tmp_path / "data" / "crops") == []
    assert not (tmp_path / "data" / "observations.csv").exists()


def test_bad_key_raises_invalid_key(tmp_path):
    with pytest.raises(InvalidKey):
        make_store(tmp_path).add(**sighting(key="elsewhere/x.mp4"))


def test_time_limits_are_inclusive_and_whole_numbers_format(tmp_path):
    store = make_store(tmp_path)
    assert store.add(**sighting(time_seconds=0))["TimestampSeconds"] == "0.000"
    top = store.add(**sighting(time_seconds=MAX_TIME_SECONDS))
    assert (top["Timestamp"], top["TimestampSeconds"]) == ("1440:00", "86400.000")
    fine = store.add(**sighting(time_seconds=245.7461))
    assert (fine["Timestamp"], fine["TimestampSeconds"]) == ("04:05", "245.746")


def test_rejects_images_with_wrong_magic_empty_or_oversized(tmp_path):
    store = make_store(tmp_path)
    oversized_jpeg = JPEG[:3] + bytes(MAX_IMAGE_BYTES - 2)
    oversized_png = PNG[:8] + bytes(MAX_IMAGE_BYTES - 7)
    cases = [
        ("frame_jpeg", b""),
        ("frame_jpeg", PNG),
        ("frame_jpeg", b"\xff\xd8"),
        ("frame_jpeg", b"GIF89a"),
        ("frame_jpeg", oversized_jpeg),
        ("frame_jpeg", "\xff\xd8\xff text"),
        ("frame_jpeg", None),
        ("frame_jpeg", [255, 216, 255]),
        ("crop_png", b""),
        ("crop_png", JPEG),
        ("crop_png", b"\x89PNG\r\n\x1a"),
        ("crop_png", b"\x89PNG\n\n\x1a\n rest"),
        ("crop_png", oversized_png),
        ("crop_png", "PNG"),
        ("crop_png", None),
    ]
    for field, value in cases:
        with pytest.raises(ValueError) as caught:
            store.add(**sighting(**{field: value}))
        assert str(caught.value).startswith(field + ":"), (field, str(caught.value))
    assert store.rows() == []
    assert files_in(tmp_path / "data" / "frames") == [] and files_in(tmp_path / "data" / "crops") == []


def test_accepts_images_at_the_size_limit_and_as_bytearray(tmp_path):
    store = make_store(tmp_path)
    largest = JPEG[:3] + bytes(MAX_IMAGE_BYTES - 3)
    row = store.add(**sighting(frame_jpeg=largest, crop_png=bytearray(PNG)))
    assert (tmp_path / "data" / "frames" / row["FrameFileName"]).stat().st_size == MAX_IMAGE_BYTES
    assert (tmp_path / "data" / "crops" / row["CropFileName"]).read_bytes() == PNG


def test_add_takes_keyword_arguments_only(tmp_path):
    with pytest.raises(TypeError):
        make_store(tmp_path).add(KEY_2024, 1.0, {"x": 0.5, "y": 0.5}, None, "ACAU", "", "LO", JPEG, PNG)


def test_failed_csv_write_removes_new_images(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.add(**sighting())
    before = (tmp_path / "data" / "observations.csv").read_bytes()

    def refuse(source, target):
        """Stand in for os.replace on a full disk."""
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(StoreError) as caught:
        store.add(**sighting(species_code="CDEL"))
    monkeypatch.undo()
    assert "observations.csv" in str(caught.value) and "No space left" in str(caught.value)
    data_dir = tmp_path / "data"
    assert files_in(data_dir / "frames") == ["ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg"]
    assert files_in(data_dir / "crops") == ["ID001_ACAU_BOTTOMLEFT_FLC_T1.png"]
    assert (data_dir / "observations.csv").read_bytes() == before
    assert stray_files(data_dir) == []
    assert store.add(**sighting(species_code="CDEL"))["ID"] == "ID002"


def test_failed_crop_write_removes_the_frame_and_adds_no_row(tmp_path):
    store = make_store(tmp_path)
    crops = tmp_path / "data" / "crops"
    crops.chmod(0o555)
    try:
        with pytest.raises(StoreError) as caught:
            store.add(**sighting())
    finally:
        crops.chmod(0o755)
    assert "crops" in str(caught.value)
    assert files_in(tmp_path / "data" / "frames") == []
    assert store.rows() == []
    assert store.add(**sighting())["ID"] == "ID001"


def test_existing_image_file_is_set_aside_not_overwritten(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    orphan = tmp_path / "data" / "frames" / "ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg"
    orphan.write_bytes(b"left behind by a crash")
    row = store.add(**sighting())
    assert orphan.read_bytes() == JPEG
    assert row["FrameFileName"] == orphan.name
    kept = files_in(tmp_path / "data" / "trash")
    assert kept == ["20260921T153000Z_ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg"]
    assert (tmp_path / "data" / "trash" / kept[0]).read_bytes() == b"left behind by a crash"


def test_delete_moves_images_to_trash_and_removes_row(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    first = store.add(**sighting())
    second = store.add(**sighting(species_code="CDEL"))
    clock.advance(90)
    removed = store.delete("ID001")
    assert removed == first
    assert store.rows() == [second]
    data_dir = tmp_path / "data"
    assert files_in(data_dir / "frames") == [second["FrameFileName"]]
    assert files_in(data_dir / "crops") == [second["CropFileName"]]
    assert files_in(data_dir / "trash") == [
        "20260921T153130Z_ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg",
        "20260921T153130Z_ID001_ACAU_BOTTOMLEFT_FLC_T1.png",
    ]
    assert (data_dir / "trash" / "20260921T153130Z_ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg").read_bytes() == JPEG
    assert [record[5] for record in read_csv(data_dir / "observations.csv")] == ["ID", "ID002"]
    assert stray_files(data_dir) == []


def test_delete_twice_in_one_second_keeps_both_trash_copies(tmp_path):
    store = make_store(tmp_path, FixedClock())
    store.add(**sighting(frame_jpeg=JPEG + b" first"))
    store.delete("ID001")
    store.add(**sighting(frame_jpeg=JPEG + b" second"))
    store.delete("ID001")
    trash = tmp_path / "data" / "trash"
    frames = [name for name in files_in(trash) if name.endswith(".jpg")]
    assert len(frames) == 2
    assert sorted((trash / name).read_bytes() for name in frames) == [JPEG + b" first", JPEG + b" second"]


def test_delete_still_removes_the_row_when_an_image_is_already_gone(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    (tmp_path / "data" / "frames" / row["FrameFileName"]).unlink()
    assert store.delete("ID001") == row
    assert store.rows() == []
    assert len(files_in(tmp_path / "data" / "trash")) == 1


def test_delete_unknown_id_raises(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(StoreError, match="ID001"):
        store.delete("ID001")
    with pytest.raises(UnknownObservation, match="ID001"):
        store.delete("ID001")
    assert issubclass(UnknownObservation, StoreError)
    store.add(**sighting())
    for unknown in ("ID002", "id001", "ID1", "001", "", "../ID001", "ID001 "):
        with pytest.raises(StoreError):
            store.delete(unknown)
    for wrong_type in (None, 1, ["ID001"]):
        with pytest.raises(ValueError, match="^obs_id:"):
            store.delete(wrong_type)
    assert [row["ID"] for row in store.rows()] == ["ID001"]
    assert files_in(tmp_path / "data" / "trash") == []


def test_rows_filter_by_key(tmp_path):
    store = make_store(tmp_path)
    store.add(**sighting())
    store.add(**sighting(key=KEY_2024_T2))
    store.add(**sighting(species_code="CDEL"))
    assert [row["ID"] for row in store.rows()] == ["ID001", "ID002", "ID003"]
    assert [row["ID"] for row in store.rows(KEY_2024)] == ["ID001", "ID003"]
    assert [row["ID"] for row in store.rows(key=KEY_2024_T2)] == ["ID002"]
    assert store.rows(KEY_MULTI) == []
    with pytest.raises(InvalidKey):
        store.rows("../observations.csv")
    with pytest.raises(InvalidKey):
        store.rows(7)


def test_rows_returns_copies(tmp_path):
    store = make_store(tmp_path)
    store.add(**sighting())
    store.rows()[0]["ID"] = "changed"
    assert store.rows()[0]["ID"] == "ID001"


def test_tally_counts_sightings_videos_and_earliest_year(tmp_path):
    store = make_store(tmp_path)
    assert store.tally() == []
    store.add(**sighting())
    store.add(**sighting(time_seconds=120))
    store.add(**sighting(species_code="CDEL"))
    store.add(**sighting(key=KEY_2016_ODD))
    store.add(**sighting(key=KEY_NO_YEAR, species_code="XMUT"))
    assert store.tally() == [
        {"code": "ACAU", "name": "Aplysina cauliformis", "sightings": 3, "videos": 2, "earliest_year": 2016},
        {"code": "CDEL", "name": "Cliona delitrix", "sightings": 1, "videos": 1, "earliest_year": 2024},
        {"code": "XMUT", "name": "Xestospongia muta", "sightings": 1, "videos": 1, "earliest_year": None},
    ]


def test_tally_keeps_a_species_that_left_the_species_list(tmp_path):
    make_store(tmp_path).add(**sighting(species_code="XMUT"))
    fewer = [item for item in SPECIES if item.code != "XMUT"]
    assert make_store(tmp_path, species=fewer).tally() == [
        {"code": "XMUT", "name": "Xestospongia muta", "sightings": 1, "videos": 1, "earliest_year": 2024}
    ]


def test_mark_opened_then_done_then_reopened(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    opened = store.mark_opened(KEY_2024, "LO", ["ACAU", "AFUL"])
    assert opened == {
        "S3Key": KEY_2024,
        "FileName": "TCRMP20241022_video_FLC_T1.MP4",
        "Status": "in progress",
        "TargetSpecies": "ACAU;AFUL",
        "Sightings": "0",
        "Annotator": "LO",
        "FirstOpened": "2026-09-21T15:30:00Z",
        "MarkedDone": "",
    }
    clock.advance(60)
    assert store.mark_opened(KEY_2024, "AB", ["CDEL"]) == opened
    done_at = clock.advance(60)
    done = store.mark_done(KEY_2024, True, "AB", ["CDEL", "XMUT"])
    assert done == {**opened, "Status": "done", "TargetSpecies": "CDEL;XMUT", "Annotator": "AB", "MarkedDone": done_at}
    assert store.video_status() == {KEY_2024: {"status": "done", "sightings": 0, "unlabeled": 0}}
    clock.advance(60)
    assert store.mark_opened(KEY_2024, "LO", ["ACAU"]) == done
    clock.advance(60)
    reopened = store.mark_done(KEY_2024, False, "LO", ["ACAU"])
    assert reopened == {**done, "Status": "in progress", "MarkedDone": ""}
    assert reopened["FirstOpened"] == "2026-09-21T15:30:00Z"
    assert store.screened() == [reopened]
    assert store.video_status() == {KEY_2024: {"status": "in progress", "sightings": 0, "unlabeled": 0}}
    records = read_csv(tmp_path / "data" / "videos_screened.csv")
    assert records[0] == SCREENED_COLUMNS
    assert records[1:] == [[reopened[column] for column in SCREENED_COLUMNS]]
    assert stray_files(tmp_path / "data") == []


def test_mark_done_on_a_video_that_was_never_opened_creates_the_row(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    done = store.mark_done(KEY_2024, True, "LO", [])
    assert done["Status"] == "done"
    assert done["TargetSpecies"] == ""
    assert done["FirstOpened"] == done["MarkedDone"] == "2026-09-21T15:30:00Z"
    fresh = store.mark_done(KEY_2024_T2, False, "LO", ["ACAU"])
    assert (fresh["Status"], fresh["MarkedDone"], fresh["TargetSpecies"]) == ("in progress", "", "ACAU")
    assert [row["S3Key"] for row in store.screened()] == [KEY_2024, KEY_2024_T2]


@pytest.mark.parametrize(
    "arguments, starts_with",
    [
        (("elsewhere/x.mp4", "LO", ["ACAU"]), "key:"),
        ((None, "LO", ["ACAU"]), "key:"),
        ((KEY_2024, "", ["ACAU"]), "annotator:"),
        ((KEY_2024, "L O", ["ACAU"]), "annotator:"),
        ((KEY_2024, "LO", ["ZZZZ"]), "target_species:"),
        ((KEY_2024, "LO", "ACAU"), "target_species:"),
        ((KEY_2024, "LO", None), "target_species:"),
        ((KEY_2024, "LO", [None]), "target_species:"),
        ((KEY_2024, "LO", ["ACAU;CDEL"]), "target_species:"),
    ],
)
def test_mark_opened_and_mark_done_reject_bad_arguments(tmp_path, arguments, starts_with):
    store = make_store(tmp_path)
    key, annotator, targets = arguments
    with pytest.raises(ValueError) as opened:
        store.mark_opened(key, annotator, targets)
    with pytest.raises(ValueError) as done:
        store.mark_done(key, True, annotator, targets)
    assert str(opened.value).startswith(starts_with) and str(done.value).startswith(starts_with)
    assert store.screened() == []
    assert not (tmp_path / "data" / "videos_screened.csv").exists()


@pytest.mark.parametrize("bad", [None, 1, 0, "true", "done"])
def test_mark_done_needs_a_real_boolean(tmp_path, bad):
    with pytest.raises(ValueError, match="^done:"):
        make_store(tmp_path).mark_done(KEY_2024, bad, "LO", [])


def test_repeated_target_codes_are_written_once(tmp_path):
    row = make_store(tmp_path).mark_opened(KEY_2024, "LO", ("ACAU", "CDEL", "ACAU"))
    assert row["TargetSpecies"] == "ACAU;CDEL"


def test_sightings_recount_after_add_and_delete(tmp_path):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    store.mark_opened(KEY_2024_T2, "LO", ["ACAU"])
    screened_path = tmp_path / "data" / "videos_screened.csv"
    sightings_at = SCREENED_COLUMNS.index("Sightings")

    def counts_on_disk():
        """Read the Sightings cell of every row straight from the file."""
        return {record[0]: record[sightings_at] for record in read_csv(screened_path)[1:]}

    assert counts_on_disk() == {KEY_2024: "0", KEY_2024_T2: "0"}
    store.add(**sighting())
    store.add(**sighting())
    store.add(**sighting(key=KEY_2024_T2))
    assert counts_on_disk() == {KEY_2024: "2", KEY_2024_T2: "1"}
    store.delete("ID001")
    assert counts_on_disk() == {KEY_2024: "1", KEY_2024_T2: "1"}
    done = store.mark_done(KEY_2024, True, "LO", ["ACAU"])
    assert done["Sightings"] == "1"
    assert store.video_status() == {
        KEY_2024: {"status": "done", "sightings": 1, "unlabeled": 0},
        KEY_2024_T2: {"status": "in progress", "sightings": 1, "unlabeled": 0},
    }


def test_video_with_sightings_and_no_log_row_reads_as_in_progress(tmp_path):
    store = make_store(tmp_path)
    store.add(**sighting())
    assert store.screened() == []
    assert store.video_status() == {KEY_2024: {"status": "in progress", "sightings": 1, "unlabeled": 0}}


def test_stale_sightings_cell_on_disk_is_recounted_on_read_and_on_the_next_write(tmp_path):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    store.add(**sighting())
    path = tmp_path / "data" / "videos_screened.csv"
    path.write_text(path.read_text(encoding="utf-8").replace(",1,LO,", ",41,LO,"), encoding="utf-8")
    assert store.screened()[0]["Sightings"] == "1"
    store.mark_opened(KEY_2024_T2, "LO", ["ACAU"])
    assert ",41," not in path.read_text(encoding="utf-8")


def test_recount_failure_after_a_save_says_the_sighting_is_saved(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    real_replace = os.replace

    def refuse_screened(source, target):
        """Fail the swap of videos_screened.csv and let every other swap through."""
        if str(target).endswith("videos_screened.csv"):
            raise OSError(13, "Permission denied")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", refuse_screened)
    with pytest.raises(StoreError) as caught:
        store.add(**sighting())
    monkeypatch.undo()
    assert "ID001" in str(caught.value) and "saved" in str(caught.value) and "videos_screened.csv" in str(caught.value)
    assert [row["ID"] for row in store.rows()] == ["ID001"]
    assert files_in(tmp_path / "data" / "frames") == ["ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg"]
    assert store.screened()[0]["Sightings"] == "1"
    assert stray_files(tmp_path / "data") == []


def test_unexpected_header_raises_and_leaves_file_untouched(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    january_only = (
        "Site,Transect,Sponge Type,Timestamp,Notes,ID,FileName,FrameFileName,AbbreviatedNote\n"
        "Flat Cay,T1,x,00:01,top left,ID001,a.mp4,a.jpg,TOPLEFT\n"
    )
    path.write_text(january_only, encoding="utf-8")
    for action in (
        lambda: store.add(**sighting()),
        lambda: store.rows(),
        lambda: store.delete("ID001"),
        lambda: store.tally(),
        lambda: store.video_status(),
        lambda: store.mark_opened(KEY_2024, "LO", []),
    ):
        with pytest.raises(StoreError) as caught:
            action()
        message = str(caught.value)
        assert "observations.csv" in message
        assert "AbbreviatedNote" in message and "LoggedAt" in message
    assert path.read_text(encoding="utf-8") == january_only
    assert files_in(tmp_path / "data" / "frames") == [] and files_in(tmp_path / "data" / "crops") == []
    assert not (tmp_path / "data" / "videos_screened.csv").exists()


def test_unexpected_screened_header_stops_writes_before_anything_changes(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "videos_screened.csv"
    path.write_text("Key,Status\nx,done\n", encoding="utf-8")
    for action in (
        lambda: store.add(**sighting()),
        lambda: store.mark_opened(KEY_2024, "LO", []),
        lambda: store.mark_done(KEY_2024, True, "LO", []),
        lambda: store.screened(),
        lambda: store.video_status(),
    ):
        with pytest.raises(StoreError) as caught:
            action()
        assert "videos_screened.csv" in str(caught.value) and "MarkedDone" in str(caught.value)
    assert path.read_text(encoding="utf-8") == "Key,Status\nx,done\n"
    assert store.rows() == []
    assert files_in(tmp_path / "data" / "frames") == []


def observation_line(**changes):
    """Return one valid observations.csv record as a list, with changes applied."""
    cells = {
        "Site": "Flat Cay", "Transect": "T1", "Sponge Type": "Aplysina cauliformis", "Timestamp": "00:01",
        "Notes": "top left", "ID": "ID001", "FileName": "a.mp4", "FrameFileName": "ID001_a.jpg",
        "AbbreviatedNote": "TOPLEFT", "SpeciesCode": "ACAU", "TimestampSeconds": "1.000", "Quadrant": "TOPLEFT",
        "PointX": "0.1000", "PointY": "0.1000", "BoxX": "", "BoxY": "", "BoxW": "", "BoxH": "",
        "CropFileName": "ID001_a.png", "S3Key": KEY_2024, "Annotator": "LO", "LoggedAt": "2026-01-05T10:00:00Z",
        "LabelStatus": "labeled",
    }  # fmt: skip
    cells.update(changes)
    return [cells[column] for column in OBSERVATION_COLUMNS]


def write_observations(path, records, encoding="utf-8", lineterminator="\n", header=None):
    """Write an observations.csv by hand from a list of records, under the current header unless one is given."""
    with open(path, "w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle, lineterminator=lineterminator)
        writer.writerow(OBSERVATION_COLUMNS if header is None else header)
        writer.writerows(records)


@pytest.mark.parametrize(
    "records, fragment",
    [
        ([observation_line()[:-1]], "line 2"),
        ([observation_line() + ["extra"]], "line 2"),
        ([observation_line(ID="7")], "ID"),
        ([observation_line(ID="ID")], "ID"),
        ([observation_line(ID="ID-1")], "ID"),
        ([observation_line(ID="ID\u0661\u0662\u0663")], "ID"),
        ([observation_line(), observation_line()], "ID001"),
        ([observation_line(FrameFileName="../../outside.jpg")], "FrameFileName"),
        ([observation_line(FrameFileName="/etc/hosts")], "FrameFileName"),
        ([observation_line(FrameFileName="..")], "FrameFileName"),
        ([observation_line(FrameFileName="")], "FrameFileName"),
        ([observation_line(CropFileName="crops/../x.png")], "CropFileName"),
        ([observation_line(CropFileName="a\\b.png")], "CropFileName"),
        ([observation_line(S3Key="")], "S3Key"),
    ],
)
def test_malformed_observation_rows_raise_and_leave_the_file_untouched(tmp_path, records, fragment):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    write_observations(path, records)
    before = path.read_bytes()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not ours")
    for action in (lambda: store.rows(), lambda: store.add(**sighting()), lambda: store.delete("ID001")):
        with pytest.raises(StoreError) as caught:
            action()
        assert "observations.csv" in str(caught.value) and fragment in str(caught.value)
        assert not isinstance(caught.value, UnknownObservation)
    assert path.read_bytes() == before
    assert outside.read_bytes() == b"not ours"
    assert files_in(tmp_path / "data" / "trash") == []
    assert files_in(tmp_path / "data" / "frames") == []


@pytest.mark.parametrize("content", [b"", b"\n", b"\xff\xfe\x00bad bytes", b"Site,Transect\x00,x\n"])
def test_unreadable_observations_file_raises_and_is_left_alone(tmp_path, content):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    path.write_bytes(content)
    with pytest.raises(StoreError, match="observations.csv"):
        store.add(**sighting())
    with pytest.raises(StoreError, match="observations.csv"):
        store.rows()
    assert path.read_bytes() == content
    assert files_in(tmp_path / "data" / "frames") == []


def test_reads_a_file_resaved_with_byte_order_mark_and_windows_line_ends(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    write_observations(path, [observation_line(Notes='top left, "big", one')], encoding="utf-8-sig", lineterminator="\r\n")
    assert store.rows()[0]["Notes"] == 'top left, "big", one'
    assert store.add(**sighting())["ID"] == "ID002"
    assert [row["ID"] for row in store.rows()] == ["ID001", "ID002"]
    assert store.rows()[0]["Notes"] == 'top left, "big", one'


@pytest.mark.parametrize(
    "lines, fragment",
    [
        (["k1,a.mp4,finished,,0,LO,2026-01-05T10:00:00Z,"], "Status"),
        (["k1,a.mp4,done,,0,LO,2026-01-05T10:00:00Z,", "k1,a.mp4,done,,0,LO,2026-01-05T10:00:00Z,"], "k1"),
        (["k1,a.mp4,done,,0,LO"], "line 2"),
        ([",a.mp4,done,,0,LO,2026-01-05T10:00:00Z,"], "S3Key"),
    ],
)
def test_malformed_screened_rows_raise_and_leave_the_file_untouched(tmp_path, lines, fragment):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "videos_screened.csv"
    path.write_text("\n".join([",".join(SCREENED_COLUMNS)] + lines) + "\n", encoding="utf-8")
    before = path.read_bytes()
    for action in (lambda: store.screened(), lambda: store.mark_opened(KEY_2024, "LO", []), lambda: store.add(**sighting())):
        with pytest.raises(StoreError) as caught:
            action()
        assert "videos_screened.csv" in str(caught.value) and fragment in str(caught.value)
    assert path.read_bytes() == before
    assert store.rows() == []


def test_concurrent_adds_from_threads_and_processes_give_unique_ids(tmp_path):
    data_dir = tmp_path / "data"
    helper = tmp_path / "add_sightings.py"
    helper.write_text(
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "project_root, data_dir, species_csv, sites_csv, label, count, signals = sys.argv[1:8]\n"
        "sys.path.insert(0, project_root)\n"
        "from screener.names import load_site_names\n"
        "from screener.species import load_species\n"
        "from screener.store import ObservationStore\n"
        "store = ObservationStore(Path(data_dir), load_species(Path(species_csv)), load_site_names(Path(sites_csv)))\n"
        "Path(signals, label + '.ready').write_text('ready')\n"
        "deadline = time.monotonic() + 60\n"
        "while not Path(signals, 'go').exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        sys.exit('no go signal from the test')\n"
        "    time.sleep(0.002)\n"
        "for index in range(int(count)):\n"
        "    store.add(\n"
        "        key='TCRMP_video_ondeck/2024Annual/TCRMP20241022_video_FLC_T1.MP4',\n"
        "        time_seconds=float(index), point={'x': 0.25, 'y': 0.75}, box=None, species_code='ACAU',\n"
        "        note=label + ' ' + str(index), annotator='LO',\n"
        "        frame_jpeg=b'\\xff\\xd8\\xff' + label.encode(), crop_png=b'\\x89PNG\\r\\n\\x1a\\n' + label.encode(),\n"
        "    )\n",
        encoding="utf-8",
    )
    species_csv = tmp_path / "species.csv"
    species_lines = [f"{item.code},{item.name},{item.part},{item.default_pin}" for item in SPECIES]
    species_csv.write_text("\n".join(["Code,ScientificName,GuidePart,DefaultPin"] + species_lines) + "\n", encoding="utf-8")
    sites_csv = tmp_path / "sites.csv"
    site_lines = [f"{code},{name}" for code, name in SITES.items()]
    sites_csv.write_text("\n".join(["SiteCode,SiteName"] + site_lines) + "\n", encoding="utf-8")
    store = ObservationStore(data_dir, SPECIES, SITES)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    signals = tmp_path / "signals"
    signals.mkdir()
    command = [sys.executable, str(helper), str(PROJECT_ROOT), str(data_dir), str(species_csv), str(sites_csv)]
    processes = [
        subprocess.Popen(command + [f"process{number}", "10", str(signals)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for number in range(2)
    ]
    deadline = time.monotonic() + 60
    while len(list(signals.glob("*.ready"))) < 2:
        assert time.monotonic() < deadline, "the helper processes did not start"
        assert all(process.poll() is None for process in processes), "a helper process exited early"
        time.sleep(0.005)
    failures = []

    def add_ten(number):
        """Add ten sightings from one thread and record any failure."""
        try:
            for index in range(10):
                store.add(**sighting(note=f"thread{number} {index}", time_seconds=float(index)))
        except Exception as error:  # a failure in a thread must fail the test, not vanish
            failures.append(repr(error))

    threads = [threading.Thread(target=add_ten, args=(number,)) for number in range(8)]
    (signals / "go").write_text("go", encoding="utf-8")
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    for process in processes:
        _, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr.decode("utf-8", "replace")
    assert failures == []
    rows = store.rows()
    ids = [row["ID"] for row in rows]
    assert len(rows) == 100
    assert len(set(ids)) == 100
    assert sorted(ids) == sorted(f"ID{number:03d}" for number in range(1, 101))
    assert len({row["Notes"] for row in rows}) == 100
    frames = files_in(data_dir / "frames")
    crops = files_in(data_dir / "crops")
    assert len(frames) == 100 and len(crops) == 100
    assert sorted(frames) == sorted(row["FrameFileName"] for row in rows)
    assert sorted(crops) == sorted(row["CropFileName"] for row in rows)
    assert len(read_csv(data_dir / "observations.csv")) == 101
    assert store.screened()[0]["Sightings"] == "100"
    assert read_csv(data_dir / "videos_screened.csv")[1][SCREENED_COLUMNS.index("Sightings")] == "100"
    assert stray_files(data_dir) == []


def test_write_gives_up_with_a_clear_error_when_another_writer_keeps_the_lock(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    monkeypatch.setattr(store_module, "LOCK_WAIT_SECONDS", 0.2)
    descriptor = hold_lock(tmp_path / "data")
    try:
        started = time.monotonic()
        for action in (
            lambda: store.add(**sighting()),
            lambda: store.delete("ID001"),
            lambda: store.mark_opened(KEY_2024, "LO", []),
            lambda: store.mark_done(KEY_2024, True, "LO", []),
        ):
            with pytest.raises(StoreError) as caught:
                action()
            assert ".lock" in str(caught.value) and "0.2 seconds" in str(caught.value)
        assert time.monotonic() - started < 10
        assert store.rows() == [] and store.screened() == []
    finally:
        release_lock(descriptor)
    assert files_in(tmp_path / "data" / "frames") == []
    assert store.add(**sighting())["ID"] == "ID001"


def test_write_waits_for_a_writer_that_lets_go_in_time(tmp_path):
    store = make_store(tmp_path)
    descriptor = hold_lock(tmp_path / "data")
    releaser = threading.Timer(0.3, release_lock, args=(descriptor,))
    started = time.monotonic()
    releaser.start()
    try:
        row = store.add(**sighting())
    finally:
        releaser.join()
    assert row["ID"] == "ID001"
    assert time.monotonic() - started >= 0.25


def test_csv_survives_commas_quotes_newlines_html_in_note(tmp_path):
    store = make_store(tmp_path)
    note = 'big, "barrel"\r\nnext to <b>fan</b> & \'more\'; =1+1, caf\u00e9 \U0001f9fd'
    row = store.add(**sighting(note=note))
    expected = 'bottom left, big, "barrel" next to <b>fan</b> & \'more\'; =1+1, caf\u00e9 \U0001f9fd'
    assert row["Notes"] == expected
    store.add(**sighting(note="plain"))
    records = read_csv(tmp_path / "data" / "observations.csv")
    assert len(records) == 3
    assert all(len(record) == len(OBSERVATION_COLUMNS) for record in records)
    assert records[1][OBSERVATION_COLUMNS.index("Notes")] == expected
    reloaded = make_store(tmp_path).rows()
    assert reloaded[0]["Notes"] == expected
    assert reloaded[1]["Notes"] == "bottom left, plain"
    assert len((tmp_path / "data" / "observations.csv").read_text(encoding="utf-8").splitlines()) == 3


@pytest.mark.parametrize(
    "note",
    ['=HYPERLINK("http://x","y")', "+1+1", "-2+3", "@SUM(A1:A9)", "\t=1+1", "\r=1+1", "=cmd|' /C calc'!A0"],
)
def test_formula_leading_note_cannot_start_a_cell(tmp_path, note):
    store = make_store(tmp_path)
    row = store.add(**sighting(note=note))
    assert row["Notes"].startswith("bottom left, ")
    assert not any(cell.startswith(FORMULA_LEADERS) for cell in row.values())
    on_disk = read_csv(tmp_path / "data" / "observations.csv")[1]
    assert not any(cell.startswith(FORMULA_LEADERS) for cell in on_disk)


def test_negative_zero_point_cannot_write_a_minus_sign(tmp_path):
    row = make_store(tmp_path).add(**sighting(point={"x": -0.0, "y": -0.0}))
    assert (row["PointX"], row["PointY"]) == ("0.0000", "0.0000")


def test_clock_without_a_time_zone_reads_as_utc_and_other_zones_are_converted(tmp_path):
    naive = make_store(tmp_path / "a", lambda: datetime(2026, 1, 5, 10, 0, 0))
    assert naive.add(**sighting())["LoggedAt"] == "2026-01-05T10:00:00Z"
    atlantic = timezone(timedelta(hours=-4))
    zoned = make_store(tmp_path / "b", lambda: datetime(2026, 1, 5, 10, 0, 0, tzinfo=atlantic))
    assert zoned.add(**sighting())["LoggedAt"] == "2026-01-05T14:00:00Z"
    broken = make_store(tmp_path / "c", lambda: "2026-01-05")
    with pytest.raises(StoreError, match="clock"):
        broken.add(**sighting())
    assert files_in(tmp_path / "c" / "data" / "frames") == []


def test_default_clock_writes_a_utc_stamp(tmp_path):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    logged = make_store(tmp_path).add(**sighting())["LoggedAt"]
    parsed = datetime.strptime(logged, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before <= parsed <= datetime.now(timezone.utc)


def test_constructor_creates_the_folders_and_checks_its_arguments(tmp_path):
    store = ObservationStore(str(tmp_path / "deep" / "data"), SPECIES, SITES)
    data_dir = tmp_path / "deep" / "data"
    assert all((data_dir / name).is_dir() for name in ("frames", "crops", "trash"))
    assert store.data_dir == data_dir
    assert store.frames_dir == data_dir / "frames" and store.crops_dir == data_dir / "crops"
    assert store.observations_path == data_dir / "observations.csv"
    assert store.screened_path == data_dir / "videos_screened.csv"
    twice = [SPECIES[0], SPECIES[0]]
    for arguments in (
        (None, SPECIES, SITES),
        (tmp_path / "x", [], SITES),
        (tmp_path / "x", ["ACAU"], SITES),
        (tmp_path / "x", twice, SITES),
        (tmp_path / "x", SPECIES, ["FLC"]),
        (tmp_path / "x", SPECIES, SITES, "not callable"),
    ):
        with pytest.raises(ValueError):
            ObservationStore(*arguments)
    assert not (tmp_path / "x").exists()


def test_constructor_raises_store_error_when_the_data_folder_cannot_be_created(tmp_path):
    blocker = tmp_path / "plain file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(StoreError) as caught:
        ObservationStore(blocker / "data", SPECIES, SITES)
    assert str(blocker) in str(caught.value) and "could not create" in str(caught.value)


def test_unwritable_data_folder_raises_store_error_naming_the_lock_file(tmp_path):
    store = make_store(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.chmod(0o555)
    try:
        with pytest.raises(StoreError) as caught:
            store.add(**sighting())
    finally:
        data_dir.chmod(0o755)
    assert ".lock" in str(caught.value)
    assert files_in(data_dir / "frames") == []


def test_lock_call_that_fails_raises_store_error(tmp_path, monkeypatch):
    store = make_store(tmp_path)

    def broken_flock(descriptor, operation):
        """Stand in for fcntl.flock on a file system without lock support."""
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(store_module.fcntl, "flock", broken_flock)
    with pytest.raises(StoreError) as caught:
        store.add(**sighting())
    monkeypatch.undo()
    assert ".lock" in str(caught.value) and "Operation not supported" in str(caught.value)
    assert store.rows() == []


def test_delete_says_the_row_is_removed_when_an_image_cannot_move(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    trash = tmp_path / "data" / "trash"
    trash.chmod(0o555)
    try:
        with pytest.raises(StoreError) as caught:
            store.delete("ID001")
    finally:
        trash.chmod(0o755)
    message = str(caught.value)
    assert "ID001 is removed" in message
    assert row["FrameFileName"] in message and row["CropFileName"] in message
    assert store.rows() == []
    assert files_in(tmp_path / "data" / "frames") == [row["FrameFileName"]]


def test_delete_says_the_row_is_removed_when_the_counts_cannot_be_updated(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    store.add(**sighting())
    real_replace = os.replace

    def refuse_screened(source, target):
        """Fail the swap of videos_screened.csv and let every other move through."""
        if str(target).endswith("videos_screened.csv"):
            raise OSError(13, "Permission denied")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", refuse_screened)
    with pytest.raises(StoreError) as caught:
        store.delete("ID001")
    monkeypatch.undo()
    assert "ID001 is removed" in str(caught.value) and "videos_screened.csv" in str(caught.value)
    assert store.rows() == []
    assert len(files_in(tmp_path / "data" / "trash")) == 2
    assert store.screened()[0]["Sightings"] == "0"


def test_failed_save_names_new_images_it_could_not_remove(tmp_path, monkeypatch):
    store = make_store(tmp_path)

    def refuse_replace(source, target):
        """Stand in for os.replace on a full disk."""
        raise OSError(28, "No space left on device")

    def refuse_remove(path):
        """Stand in for os.remove on a folder that turned read-only."""
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse_replace)
    monkeypatch.setattr(os, "remove", refuse_remove)
    with pytest.raises(StoreError) as caught:
        store.add(**sighting())
    monkeypatch.undo()
    message = str(caught.value)
    assert "No space left" in message and "could not be removed" in message
    assert "ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg" in message and "ID001_ACAU_BOTTOMLEFT_FLC_T1.png" in message
    assert store.rows() == []


def test_store_works_with_a_swapped_species_list(tmp_path):
    corals = [Species(code="OANN", name="Orbicella annularis", part="", default_pin="1")]
    store = make_store(tmp_path, species=corals)
    row = store.add(**sighting(species_code="OANN"))
    assert row["Sponge Type"] == "Orbicella annularis"
    with pytest.raises(ValueError, match="^species_code:"):
        store.add(**sighting(species_code="ACAU"))


# ----- Later points and relabeling -----


def later(**changes):
    """Return valid keyword arguments for a Later point (no species), with changes applied."""
    return sighting(**{"species_code": None, **changes})


def snapshot_files(*folders):
    """Map every file in the folders to its name and bytes."""
    return {(folder.name, item.name): item.read_bytes() for folder in folders for item in sorted(folder.iterdir())}


def test_add_later_point_writes_blank_species_and_later_files(tmp_path):
    store = make_store(tmp_path, FixedClock())
    row = store.add(**later())
    assert row["SpeciesCode"] == ""
    assert row["Sponge Type"] == ""
    assert row["LabelStatus"] == "unlabeled"
    assert row["FrameFileName"] == "ID001_LATER_BOTTOMLEFT_FLC_T1.jpg"
    assert row["CropFileName"] == "ID001_LATER_BOTTOMLEFT_FLC_T1.png"
    labeled = make_store(tmp_path / "other", FixedClock()).add(**sighting())
    differing = {column for column in OBSERVATION_COLUMNS if row[column] != labeled[column]}
    assert differing == {"SpeciesCode", "Sponge Type", "LabelStatus", "FrameFileName", "CropFileName"}
    data_dir = tmp_path / "data"
    assert (data_dir / "frames" / row["FrameFileName"]).read_bytes() == JPEG
    assert (data_dir / "crops" / row["CropFileName"]).read_bytes() == PNG
    records = read_csv(data_dir / "observations.csv")
    assert records[1] == [row[column] for column in OBSERVATION_COLUMNS]
    assert store.rows() == [row]
    assert stray_files(data_dir) == []


def test_add_later_point_still_requires_both_images(tmp_path):
    store = make_store(tmp_path)
    bad_images = (("frame_jpeg", b""), ("frame_jpeg", PNG), ("crop_png", b""), ("crop_png", JPEG), ("crop_png", None))
    for field, value in bad_images:
        with pytest.raises(ValueError) as caught:
            store.add(**later(**{field: value}))
        assert str(caught.value).startswith(field + ":")
    assert store.rows() == []
    assert files_in(tmp_path / "data" / "frames") == [] and files_in(tmp_path / "data" / "crops") == []


def test_add_rejects_empty_string_species(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="^species_code: unknown species code ''"):
        store.add(**sighting(species_code=""))
    assert store.rows() == []
    assert files_in(tmp_path / "data" / "frames") == [] and files_in(tmp_path / "data" / "crops") == []
    assert not (tmp_path / "data" / "observations.csv").exists()


def test_set_label_relabels_and_renames_both_images(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    store.add(**sighting(frame_jpeg=JPEG + b" one", crop_png=PNG + b" one", note="under a ledge"))
    neighbor = store.add(**sighting(species_code="AFUL", frame_jpeg=JPEG + b" two", crop_png=PNG + b" two"))
    before = store.rows()[0]
    clock.advance(600)
    changed = store.set_label("ID001", "CDEL")
    assert changed == {
        **before,
        "SpeciesCode": "CDEL",
        "Sponge Type": "Cliona delitrix",
        "LabelStatus": "labeled",
        "FrameFileName": "ID001_CDEL_BOTTOMLEFT_FLC_T1.jpg",
        "CropFileName": "ID001_CDEL_BOTTOMLEFT_FLC_T1.png",
    }
    for column in ("ID", "LoggedAt", "Annotator", "Notes", "TimestampSeconds", "PointX", "PointY", "BoxX", "S3Key"):
        assert changed[column] == before[column]
    assert before["LoggedAt"] == "2026-09-21T15:30:00Z"
    data_dir = tmp_path / "data"
    assert files_in(data_dir / "frames") == sorted([changed["FrameFileName"], neighbor["FrameFileName"]])
    assert files_in(data_dir / "crops") == sorted([changed["CropFileName"], neighbor["CropFileName"]])
    assert not (data_dir / "frames" / before["FrameFileName"]).exists()
    assert not (data_dir / "crops" / before["CropFileName"]).exists()
    assert (data_dir / "frames" / changed["FrameFileName"]).read_bytes() == JPEG + b" one"
    assert (data_dir / "crops" / changed["CropFileName"]).read_bytes() == PNG + b" one"
    assert (data_dir / "frames" / neighbor["FrameFileName"]).read_bytes() == JPEG + b" two"
    assert store.rows() == [changed, neighbor]
    assert read_csv(data_dir / "observations.csv")[1] == [changed[column] for column in OBSERVATION_COLUMNS]
    assert files_in(data_dir / "trash") == []
    assert stray_files(data_dir) == []


def test_set_label_none_unlabels_and_renames_to_later(tmp_path):
    store = make_store(tmp_path)
    before = store.add(**sighting())
    changed = store.set_label("ID001", None)
    assert changed == {
        **before,
        "SpeciesCode": "",
        "Sponge Type": "",
        "LabelStatus": "unlabeled",
        "FrameFileName": "ID001_LATER_BOTTOMLEFT_FLC_T1.jpg",
        "CropFileName": "ID001_LATER_BOTTOMLEFT_FLC_T1.png",
    }
    data_dir = tmp_path / "data"
    assert files_in(data_dir / "frames") == [changed["FrameFileName"]]
    assert files_in(data_dir / "crops") == [changed["CropFileName"]]
    assert (data_dir / "frames" / changed["FrameFileName"]).read_bytes() == JPEG
    assert store.rows() == [changed]
    assert stray_files(data_dir) == []


def test_set_label_labels_a_later_point(tmp_path):
    store = make_store(tmp_path)
    before = store.add(**later(note="check later"))
    changed = store.set_label("ID001", "XMUT")
    assert changed["SpeciesCode"] == "XMUT" and changed["Sponge Type"] == "Xestospongia muta"
    assert changed["LabelStatus"] == "labeled"
    assert changed["FrameFileName"] == "ID001_XMUT_BOTTOMLEFT_FLC_T1.jpg"
    assert changed["Notes"] == before["Notes"] == "bottom left, check later"
    assert files_in(tmp_path / "data" / "frames") == [changed["FrameFileName"]]
    assert files_in(tmp_path / "data" / "crops") == [changed["CropFileName"]]
    assert store.rows() == [changed]


def test_set_label_same_code_changes_nothing(tmp_path):
    store = make_store(tmp_path)
    store.add(**sighting())
    store.add(**later())
    data_dir = tmp_path / "data"
    paths = [item for folder in ("frames", "crops") for item in sorted((data_dir / folder).iterdir())]
    paths.append(data_dir / "observations.csv")
    for path in paths:
        os.utime(path, ns=(10 ** 18, 10 ** 18))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    names_before = snapshot_files(data_dir / "frames", data_dir / "crops")
    assert store.set_label("ID001", "ACAU") == store.rows()[0]
    assert store.set_label("ID002", None) == store.rows()[1]
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before
    assert snapshot_files(data_dir / "frames", data_dir / "crops") == names_before
    assert files_in(data_dir / "trash") == []
    assert not (data_dir / "videos_screened.csv").exists()


def test_set_label_unknown_id_raises_unknown_observation(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(UnknownObservation, match="ID001"):
        store.set_label("ID001", "ACAU")
    store.add(**sighting())
    before = (tmp_path / "data" / "observations.csv").read_bytes()
    for unknown in ("ID002", "id001", "ID1", "001", "", "../ID001", "ID001 "):
        with pytest.raises(UnknownObservation):
            store.set_label(unknown, "CDEL")
        with pytest.raises(StoreError):
            store.set_label(unknown, None)
    for wrong_type in (None, 1, ["ID001"]):
        with pytest.raises(ValueError, match="^obs_id:"):
            store.set_label(wrong_type, "CDEL")
    assert (tmp_path / "data" / "observations.csv").read_bytes() == before
    assert files_in(tmp_path / "data" / "frames") == ["ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg"]


@pytest.mark.parametrize("bad", ["ZZZZ", "acau", "../ACAU", "ACAU/", "ACAU ", "", 7, ["ACAU"], b"ACAU"])
def test_set_label_unknown_code_raises_value_error(tmp_path, bad):
    store = make_store(tmp_path)
    store.add(**sighting())
    data_dir = tmp_path / "data"
    before = (data_dir / "observations.csv").read_bytes()
    names = snapshot_files(data_dir / "frames", data_dir / "crops")
    with pytest.raises(ValueError, match="^species_code:") as caught:
        store.set_label("ID001", bad)
    assert not isinstance(caught.value, StoreError)
    assert (data_dir / "observations.csv").read_bytes() == before
    assert snapshot_files(data_dir / "frames", data_dir / "crops") == names
    assert files_in(data_dir / "trash") == []


@pytest.mark.parametrize("folder, column", [("frames", "FrameFileName"), ("crops", "CropFileName")])
def test_set_label_missing_image_changes_nothing(tmp_path, folder, column):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    data_dir = tmp_path / "data"
    (data_dir / folder / row[column]).unlink()
    before = (data_dir / "observations.csv").read_bytes()
    names = snapshot_files(data_dir / "frames", data_dir / "crops")
    for new_code in ("CDEL", None):
        with pytest.raises(StoreError) as caught:
            store.set_label("ID001", new_code)
        assert not isinstance(caught.value, UnknownObservation)
        assert str(data_dir / folder / row[column]) in str(caught.value)
    assert (data_dir / "observations.csv").read_bytes() == before
    assert snapshot_files(data_dir / "frames", data_dir / "crops") == names
    assert files_in(data_dir / "trash") == []


def test_set_label_csv_write_failure_moves_images_back(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    data_dir = tmp_path / "data"
    before = (data_dir / "observations.csv").read_bytes()

    def refuse(path, columns, rows):
        """Stand in for the CSV writer on a full disk."""
        raise StoreError(f"{path}: could not write the file: No space left on device")

    monkeypatch.setattr(store_module, "write_csv_atomic", refuse)
    with pytest.raises(StoreError, match="No space left"):
        store.set_label("ID001", "CDEL")
    monkeypatch.undo()
    assert files_in(data_dir / "frames") == [row["FrameFileName"]]
    assert files_in(data_dir / "crops") == [row["CropFileName"]]
    assert (data_dir / "frames" / row["FrameFileName"]).read_bytes() == JPEG
    assert (data_dir / "crops" / row["CropFileName"]).read_bytes() == PNG
    assert (data_dir / "observations.csv").read_bytes() == before
    assert store.rows() == [row]
    assert stray_files(data_dir) == []


def test_set_label_names_images_that_could_not_move_back(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.add(**sighting())
    real_replace = os.replace
    calls = []

    def second_move_fails(source, target):
        """Let the two renames forward through and refuse the two that undo them."""
        calls.append((str(source), str(target)))
        if len(calls) > 2:
            raise OSError(13, "Permission denied")
        return real_replace(source, target)

    def refuse(path, columns, rows):
        """Stand in for the CSV writer on a full disk."""
        raise StoreError(f"{path}: could not write the file: No space left on device")

    monkeypatch.setattr(store_module, "write_csv_atomic", refuse)
    monkeypatch.setattr(os, "replace", second_move_fails)
    with pytest.raises(StoreError) as caught:
        store.set_label("ID001", "CDEL")
    monkeypatch.undo()
    message = str(caught.value)
    assert "No space left" in message and "could not be moved back" in message
    assert "ID001_CDEL_BOTTOMLEFT_FLC_T1.jpg" in message and "ID001_CDEL_BOTTOMLEFT_FLC_T1.png" in message


def test_set_label_sets_aside_a_file_already_at_the_new_name(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    store.add(**sighting())
    orphan = tmp_path / "data" / "frames" / "ID001_CDEL_BOTTOMLEFT_FLC_T1.jpg"
    orphan.write_bytes(b"left behind by a crash")
    changed = store.set_label("ID001", "CDEL")
    assert orphan.read_bytes() == JPEG
    assert changed["FrameFileName"] == orphan.name
    kept = files_in(tmp_path / "data" / "trash")
    assert kept == ["20260921T153000Z_ID001_CDEL_BOTTOMLEFT_FLC_T1.jpg"]
    assert (tmp_path / "data" / "trash" / kept[0]).read_bytes() == b"left behind by a crash"


def test_set_label_keeps_images_whose_names_already_carry_the_new_code(tmp_path):
    store = make_store(tmp_path)
    store.add(**sighting())
    data_dir = tmp_path / "data"
    for folder, extension in (("frames", "jpg"), ("crops", "png")):
        (data_dir / folder / f"ID001_ACAU_BOTTOMLEFT_FLC_T1.{extension}").rename(
            data_dir / folder / f"ID001_CDEL_BOTTOMLEFT_FLC_T1.{extension}"
        )
    csv_path = data_dir / "observations.csv"
    csv_path.write_text(csv_path.read_text(encoding="utf-8").replace("ID001_ACAU_", "ID001_CDEL_"), encoding="utf-8")
    changed = store.set_label("ID001", "CDEL")
    assert changed["SpeciesCode"] == "CDEL" and changed["FrameFileName"] == "ID001_CDEL_BOTTOMLEFT_FLC_T1.jpg"
    assert (data_dir / "frames" / changed["FrameFileName"]).read_bytes() == JPEG
    assert (data_dir / "crops" / changed["CropFileName"]).read_bytes() == PNG
    assert files_in(data_dir / "trash") == []


def test_set_label_refuses_an_image_name_that_has_no_code_part(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    write_observations(path, [observation_line()])
    for folder, name in (("frames", "ID001_a.jpg"), ("crops", "ID001_a.png")):
        (tmp_path / "data" / folder / name).write_bytes(b"x")
    before = path.read_bytes()
    with pytest.raises(StoreError, match="ID001_a.jpg"):
        store.set_label("ID001", "CDEL")
    assert path.read_bytes() == before
    assert files_in(tmp_path / "data" / "frames") == ["ID001_a.jpg"]
    assert files_in(tmp_path / "data" / "crops") == ["ID001_a.png"]


def test_set_label_waits_for_lock_held_by_another_writer(tmp_path):
    store = make_store(tmp_path)
    store.add(**sighting())
    descriptor = hold_lock(tmp_path / "data")
    releaser = threading.Timer(0.2, release_lock, args=(descriptor,))
    started = time.monotonic()
    releaser.start()
    try:
        changed = store.set_label("ID001", "CDEL")
    finally:
        releaser.join()
    assert time.monotonic() - started >= 0.15
    assert changed["SpeciesCode"] == "CDEL"
    assert files_in(tmp_path / "data" / "frames") == ["ID001_CDEL_BOTTOMLEFT_FLC_T1.jpg"]


def test_set_label_gives_up_with_a_clear_error_when_another_writer_keeps_the_lock(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    monkeypatch.setattr(store_module, "LOCK_WAIT_SECONDS", 0.2)
    descriptor = hold_lock(tmp_path / "data")
    try:
        with pytest.raises(StoreError) as caught:
            store.set_label("ID001", "CDEL")
    finally:
        release_lock(descriptor)
    assert ".lock" in str(caught.value) and "0.2 seconds" in str(caught.value)
    assert files_in(tmp_path / "data" / "frames") == [row["FrameFileName"]]
    assert store.rows() == [row]


def test_legacy_header_reads_as_labeled_and_upgrades_on_next_write(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    legacy = store_module.LEGACY_OBSERVATION_COLUMNS
    second = observation_line(ID="ID002", FrameFileName="ID002_a.jpg", CropFileName="ID002_a.png", SpeciesCode="CDEL")
    write_observations(path, [observation_line()[:-1], second[:-1]], header=legacy)
    before = path.read_bytes()
    rows = store.rows()
    assert [row["LabelStatus"] for row in rows] == ["labeled", "labeled"]
    assert [row["ID"] for row in rows] == ["ID001", "ID002"]
    assert [item["sightings"] for item in store.tally()] == [1, 1]
    assert store.video_status() == {KEY_2024: {"status": "in progress", "sightings": 2, "unlabeled": 0}}
    assert path.read_bytes() == before
    added = store.add(**sighting())
    assert added["ID"] == "ID003"
    records = read_csv(path)
    assert records[0] == OBSERVATION_COLUMNS
    assert all(len(record) == len(OBSERVATION_COLUMNS) for record in records)
    assert [record[-1] for record in records[1:]] == ["labeled", "labeled", "labeled"]
    assert [record[OBSERVATION_COLUMNS.index("ID")] for record in records[1:]] == ["ID001", "ID002", "ID003"]


def test_legacy_header_with_a_wrong_cell_count_names_the_line(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    write_observations(path, [observation_line()], header=store_module.LEGACY_OBSERVATION_COLUMNS)
    before = path.read_bytes()
    with pytest.raises(StoreError, match="line 2") as caught:
        store.rows()
    assert "22" in str(caught.value) and "23" in str(caught.value)
    assert path.read_bytes() == before


BAD_STATUS_CASES = ["", "pending", "Labeled", "LABELED", " labeled", "unlabeled ", "later"]


@pytest.mark.parametrize("status", BAD_STATUS_CASES)
def test_label_status_blank_fails_loudly(tmp_path, status):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    second = observation_line(ID="ID002", FrameFileName="ID002_a.jpg", CropFileName="ID002_a.png", LabelStatus=status)
    write_observations(path, [observation_line(), second])
    before = path.read_bytes()
    for action in (
        lambda: store.rows(),
        lambda: store.rows(status="unlabeled"),
        lambda: store.add(**sighting()),
        lambda: store.add(**later()),
        lambda: store.delete("ID001"),
        lambda: store.set_label("ID001", "CDEL"),
        lambda: store.tally(),
        lambda: store.video_status(),
        lambda: store.mark_opened(KEY_2024, "LO", []),
    ):
        with pytest.raises(StoreError) as caught:
            action()
        message = str(caught.value)
        assert "observations.csv" in message and "line 3" in message and "LabelStatus" in message
    assert path.read_bytes() == before
    assert files_in(tmp_path / "data" / "frames") == [] and files_in(tmp_path / "data" / "trash") == []


@pytest.mark.parametrize("column, value", [("SpeciesCode", "ACAU"), ("Sponge Type", "Aplysina cauliformis")])
def test_unlabeled_row_with_code_fails_loudly(tmp_path, column, value):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    broken = {"LabelStatus": "unlabeled", "SpeciesCode": "", "Sponge Type": "", column: value}
    second = observation_line(ID="ID002", FrameFileName="ID002_a.jpg", CropFileName="ID002_a.png", **broken)
    write_observations(path, [observation_line(), second])
    before = path.read_bytes()
    for action in (lambda: store.rows(), lambda: store.add(**later()), lambda: store.set_label("ID001", None)):
        with pytest.raises(StoreError) as caught:
            action()
        message = str(caught.value)
        assert "observations.csv" in message and "line 3" in message and column in message
    assert path.read_bytes() == before


def test_labeled_row_without_code_fails_loudly(tmp_path):
    store = make_store(tmp_path)
    path = tmp_path / "data" / "observations.csv"
    second = observation_line(ID="ID002", FrameFileName="ID002_a.jpg", CropFileName="ID002_a.png", SpeciesCode="")
    write_observations(path, [observation_line(), second])
    before = path.read_bytes()
    for action in (lambda: store.rows(), lambda: store.tally(), lambda: store.add(**sighting())):
        with pytest.raises(StoreError) as caught:
            action()
        message = str(caught.value)
        assert "observations.csv" in message and "line 3" in message and "SpeciesCode" in message
    assert path.read_bytes() == before


def test_tally_and_counts_skip_later_points(tmp_path):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    store.mark_opened(KEY_2024_T2, "LO", ["ACAU"])
    store.add(**sighting())
    store.add(**later())
    store.add(**later(time_seconds=90))
    store.add(**later(key=KEY_2024_T2))
    store.add(**sighting(key=KEY_2024_T2, species_code="CDEL"))
    assert store.tally() == [
        {"code": "ACAU", "name": "Aplysina cauliformis", "sightings": 1, "videos": 1, "earliest_year": 2024},
        {"code": "CDEL", "name": "Cliona delitrix", "sightings": 1, "videos": 1, "earliest_year": 2024},
    ]
    assert {row["S3Key"]: row["Sightings"] for row in store.screened()} == {KEY_2024: "1", KEY_2024_T2: "1"}
    sightings_at = SCREENED_COLUMNS.index("Sightings")
    on_disk = {record[0]: record[sightings_at] for record in read_csv(tmp_path / "data" / "videos_screened.csv")[1:]}
    assert on_disk == {KEY_2024: "1", KEY_2024_T2: "1"}
    assert store.video_status() == {
        KEY_2024: {"status": "in progress", "sightings": 1, "unlabeled": 2},
        KEY_2024_T2: {"status": "in progress", "sightings": 1, "unlabeled": 1},
    }


def test_relabeling_moves_the_sightings_count_in_videos_screened(tmp_path):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    store.add(**sighting())
    store.add(**later())
    sightings_at = SCREENED_COLUMNS.index("Sightings")

    def count_on_disk():
        """Read the Sightings cell straight from the file."""
        return read_csv(tmp_path / "data" / "videos_screened.csv")[1][sightings_at]

    assert count_on_disk() == "1"
    store.set_label("ID002", "AFUL")
    assert count_on_disk() == "2"
    store.set_label("ID001", None)
    assert count_on_disk() == "1"
    store.set_label("ID001", "CDEL")
    assert count_on_disk() == "2"
    assert store.video_status()[KEY_2024] == {"status": "in progress", "sightings": 2, "unlabeled": 0}


def test_set_label_says_the_row_is_changed_when_the_counts_cannot_be_updated(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    store.add(**sighting())
    real_replace = os.replace

    def refuse_screened(source, target):
        """Fail the swap of videos_screened.csv and let every other move through."""
        if str(target).endswith("videos_screened.csv"):
            raise OSError(13, "Permission denied")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", refuse_screened)
    with pytest.raises(StoreError) as caught:
        store.set_label("ID001", None)
    monkeypatch.undo()
    assert "ID001" in str(caught.value) and "videos_screened.csv" in str(caught.value) and "saved" in str(caught.value)
    assert store.rows()[0]["LabelStatus"] == "unlabeled"
    assert files_in(tmp_path / "data" / "frames") == ["ID001_LATER_BOTTOMLEFT_FLC_T1.jpg"]
    assert store.screened()[0]["Sightings"] == "0"


def test_video_status_reports_unlabeled_count(tmp_path):
    store = make_store(tmp_path)
    assert store.video_status() == {}
    store.add(**later())
    assert store.video_status() == {KEY_2024: {"status": "in progress", "sightings": 0, "unlabeled": 1}}
    store.add(**sighting())
    store.add(**later(key=KEY_2024_T2))
    store.mark_done(KEY_2024_T2.replace("T2", "T9"), True, "LO", [])
    assert store.video_status() == {
        KEY_2024_T2.replace("T2", "T9"): {"status": "done", "sightings": 0, "unlabeled": 0},
        KEY_2024: {"status": "in progress", "sightings": 1, "unlabeled": 1},
        KEY_2024_T2: {"status": "in progress", "sightings": 0, "unlabeled": 1},
    }


def test_rows_filters_by_status_and_key(tmp_path):
    store = make_store(tmp_path)
    store.add(**sighting())
    store.add(**later())
    store.add(**later(key=KEY_2024_T2))
    store.add(**sighting(key=KEY_2024_T2, species_code="CDEL"))
    assert [row["ID"] for row in store.rows()] == ["ID001", "ID002", "ID003", "ID004"]
    assert [row["ID"] for row in store.rows(status="unlabeled")] == ["ID002", "ID003"]
    assert [row["ID"] for row in store.rows(status="labeled")] == ["ID001", "ID004"]
    assert [row["ID"] for row in store.rows(KEY_2024, status="unlabeled")] == ["ID002"]
    assert [row["ID"] for row in store.rows(key=KEY_2024_T2, status="labeled")] == ["ID004"]
    assert [row["ID"] for row in store.rows(KEY_2024)] == ["ID001", "ID002"]
    assert store.rows(KEY_MULTI, status="unlabeled") == []
    for bad in ("later", "", "Labeled", 1, ["labeled"]):
        with pytest.raises(ValueError, match="^status:"):
            store.rows(status=bad)
    with pytest.raises(InvalidKey):
        store.rows("../observations.csv", status="labeled")


def test_mark_done_refused_with_later_points(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    store.add(**sighting())
    store.add(**later())
    store.add(**later(time_seconds=90))
    clock.advance(60)
    before = (tmp_path / "data" / "videos_screened.csv").read_bytes()
    with pytest.raises(ValueError) as caught:
        store.mark_done(KEY_2024, True, "LO", ["ACAU"])
    assert str(caught.value) == (
        "done: this video has 2 unlabeled point(s). Label or delete them first, because a done video "
        "tells OnDeck that missing pinned species are absent."
    )
    assert not isinstance(caught.value, StoreError)
    assert (tmp_path / "data" / "videos_screened.csv").read_bytes() == before
    assert store.screened()[0]["Status"] == "in progress"
    other = store.mark_done(KEY_2024_T2, True, "LO", ["ACAU"])
    assert other["Status"] == "done"
    store.set_label("ID002", "AFUL")
    with pytest.raises(ValueError, match="1 unlabeled point"):
        store.mark_done(KEY_2024, True, "LO", ["ACAU"])
    store.delete("ID003")
    assert store.mark_done(KEY_2024, True, "LO", ["ACAU"])["Status"] == "done"


def test_mark_done_refused_for_a_video_that_was_never_opened_creates_no_row(tmp_path):
    store = make_store(tmp_path)
    store.add(**later())
    with pytest.raises(ValueError, match="^done: this video has 1 unlabeled point"):
        store.mark_done(KEY_2024, True, "LO", [])
    assert store.screened() == []
    assert not (tmp_path / "data" / "videos_screened.csv").exists()


def test_mark_done_false_allowed_with_later_points(tmp_path):
    store = make_store(tmp_path)
    store.mark_done(KEY_2024, True, "LO", ["ACAU"])
    store.add(**later())
    reopened = store.mark_done(KEY_2024, False, "LO", ["ACAU"])
    assert reopened["Status"] == "in progress" and reopened["MarkedDone"] == ""
    again = store.mark_done(KEY_2024_T2, False, "LO", ["ACAU"])
    assert again["Status"] == "in progress"
    assert store.mark_opened(KEY_2024, "LO", ["ACAU"])["Status"] == "in progress"


def test_delete_later_point_moves_later_files_to_trash(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    store.add(**sighting())
    row = store.add(**later())
    clock.advance(90)
    assert store.delete("ID002") == row
    data_dir = tmp_path / "data"
    assert [item["ID"] for item in store.rows()] == ["ID001"]
    assert files_in(data_dir / "trash") == [
        "20260921T153130Z_ID002_LATER_BOTTOMLEFT_FLC_T1.jpg",
        "20260921T153130Z_ID002_LATER_BOTTOMLEFT_FLC_T1.png",
    ]
    assert files_in(data_dir / "frames") == ["ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg"]
    assert files_in(data_dir / "crops") == ["ID001_ACAU_BOTTOMLEFT_FLC_T1.png"]


def test_later_point_counts_toward_the_next_id(tmp_path):
    store = make_store(tmp_path)
    store.add(**later())
    assert store.add(**sighting())["ID"] == "ID002"
    assert [row["ID"] for row in store.rows(status="unlabeled")] == ["ID001"]


# ----- a done video never holds Later points -----


def done_video_with_a_sighting(tmp_path, clock=None):
    """Return a store whose video KEY_2024 holds one labeled sighting and is marked done."""
    store = make_store(tmp_path, clock or FixedClock())
    store.add(**sighting())
    store.mark_done(KEY_2024, True, "LO", ["ACAU"])
    return store


def test_saved_rows_stay_plain_dicts_with_a_reopened_flag(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    assert isinstance(row, dict) and row.reopened is False
    assert dict(row) == store.rows()[0]
    relabeled = store.set_label("ID001", "CDEL")
    assert isinstance(relabeled, dict) and relabeled.reopened is False
    assert store.set_label("ID001", "CDEL").reopened is False


def test_add_later_point_reopens_done_video(tmp_path):
    clock = FixedClock()
    store = done_video_with_a_sighting(tmp_path, clock)
    other = store.mark_done(KEY_2024_T2, True, "LO", ["ACAU"])
    done_before = next(item for item in store.screened() if item["S3Key"] == KEY_2024)
    assert done_before["Status"] == "done" and done_before["MarkedDone"] != ""
    clock.advance(60)
    row = store.add(**later())
    assert row.reopened is True
    assert row["LabelStatus"] == "unlabeled"
    by_key = {item["S3Key"]: item for item in store.screened()}
    assert by_key[KEY_2024] == {**done_before, "Status": "in progress", "MarkedDone": ""}
    assert by_key[KEY_2024_T2] == other
    status_at = SCREENED_COLUMNS.index("Status")
    on_disk = read_csv(tmp_path / "data" / "videos_screened.csv")[1:]
    assert {record[0]: record[status_at] for record in on_disk} == {KEY_2024: "in progress", KEY_2024_T2: "done"}
    assert store.video_status()[KEY_2024] == {"status": "in progress", "sightings": 1, "unlabeled": 1}
    assert store.add(**later(time_seconds=90)).reopened is False


def test_later_point_on_a_video_that_is_not_done_reopens_nothing(tmp_path):
    store = make_store(tmp_path)
    assert store.add(**later()).reopened is False
    assert not (tmp_path / "data" / "videos_screened.csv").exists()
    store.mark_opened(KEY_2024_T2, "LO", ["ACAU"])
    path = tmp_path / "data" / "videos_screened.csv"
    before = path.read_bytes()
    assert store.add(**later(key=KEY_2024_T2)).reopened is False
    assert path.read_bytes() == before


def test_unlabel_reopens_done_video(tmp_path):
    clock = FixedClock()
    store = done_video_with_a_sighting(tmp_path, clock)
    store.add(**sighting(species_code="AFUL", time_seconds=90))
    done_before = store.screened()[0]
    assert done_before["Sightings"] == "2"
    clock.advance(60)
    changed = store.set_label("ID001", None)
    assert changed.reopened is True
    assert changed["LabelStatus"] == "unlabeled"
    reopened = store.screened()[0]
    assert reopened == {**done_before, "Status": "in progress", "MarkedDone": "", "Sightings": "1"}
    assert read_csv(tmp_path / "data" / "videos_screened.csv")[1][SCREENED_COLUMNS.index("Status")] == "in progress"
    again = store.set_label("ID001", None)
    assert again.reopened is False and again == changed
    with pytest.raises(ValueError, match="^done: this video has 1 unlabeled point"):
        store.mark_done(KEY_2024, True, "LO", ["ACAU"])


def test_unlabel_on_a_video_that_is_not_done_reopens_nothing(tmp_path):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    store.add(**sighting())
    assert store.set_label("ID001", None).reopened is False
    assert store.screened()[0]["Status"] == "in progress"
    bare = make_store(tmp_path / "bare")
    bare.add(**sighting())
    assert bare.set_label("ID001", None).reopened is False
    assert not (tmp_path / "bare" / "data" / "videos_screened.csv").exists()


def test_labeled_add_keeps_done_video_done(tmp_path):
    store = done_video_with_a_sighting(tmp_path)
    done_before = store.screened()[0]
    row = store.add(**sighting(species_code="CDEL", time_seconds=90))
    assert row.reopened is False
    after = store.screened()[0]
    assert after["Status"] == "done"
    assert after == {**done_before, "Sightings": "2"}


def test_relabel_to_code_keeps_done_status(tmp_path):
    store = done_video_with_a_sighting(tmp_path)
    done_before = store.screened()[0]
    assert store.set_label("ID001", "CDEL").reopened is False
    assert store.set_label("ID001", "CDEL").reopened is False
    assert store.screened()[0] == done_before
    assert store.screened()[0]["MarkedDone"] != ""


def test_failed_reopen_saves_no_later_point_in_a_done_video(tmp_path, monkeypatch):
    store = done_video_with_a_sighting(tmp_path)
    data_dir = tmp_path / "data"
    observations_before = (data_dir / "observations.csv").read_bytes()
    screened_before = (data_dir / "videos_screened.csv").read_bytes()
    names = snapshot_files(data_dir / "frames", data_dir / "crops")
    real_write = store_module.write_csv_atomic

    def refuse_screened(path, columns, rows):
        """Stand in for a full disk that refuses videos_screened.csv only."""
        if Path(path).name == "videos_screened.csv":
            raise StoreError(f"{path}: could not write the file: No space left on device")
        return real_write(path, columns, rows)

    monkeypatch.setattr(store_module, "write_csv_atomic", refuse_screened)
    with pytest.raises(StoreError, match="No space left"):
        store.add(**later())
    with pytest.raises(StoreError, match="No space left"):
        store.set_label("ID001", None)
    monkeypatch.undo()
    assert (data_dir / "observations.csv").read_bytes() == observations_before
    assert (data_dir / "videos_screened.csv").read_bytes() == screened_before
    assert snapshot_files(data_dir / "frames", data_dir / "crops") == names
    assert store.screened()[0]["Status"] == "done"


def test_failed_frame_write_on_a_done_video_leaves_it_in_progress_with_no_new_point(tmp_path):
    store = done_video_with_a_sighting(tmp_path)
    data_dir = tmp_path / "data"
    observations_before = (data_dir / "observations.csv").read_bytes()
    names = snapshot_files(data_dir / "frames", data_dir / "crops")
    frames = data_dir / "frames"
    frames.chmod(0o555)
    try:
        with pytest.raises(StoreError, match="frames"):
            store.add(**later())
    finally:
        frames.chmod(0o755)
    assert store.screened()[0]["Status"] == "in progress"
    assert store.screened()[0]["MarkedDone"] == ""
    assert (data_dir / "observations.csv").read_bytes() == observations_before
    assert [row["ID"] for row in store.rows()] == ["ID001"]
    assert snapshot_files(data_dir / "frames", data_dir / "crops") == names
    assert stray_files(data_dir) == []


def test_failed_observations_write_on_a_done_video_leaves_it_in_progress_with_no_new_point(tmp_path, monkeypatch):
    store = done_video_with_a_sighting(tmp_path)
    data_dir = tmp_path / "data"
    observations_before = (data_dir / "observations.csv").read_bytes()
    names = snapshot_files(data_dir / "frames", data_dir / "crops")
    real_write = store_module.write_csv_atomic

    def refuse_observations(path, columns, rows):
        """Stand in for a full disk that refuses observations.csv only."""
        if Path(path).name == "observations.csv":
            raise StoreError(f"{path}: could not write the file: No space left on device")
        return real_write(path, columns, rows)

    monkeypatch.setattr(store_module, "write_csv_atomic", refuse_observations)
    with pytest.raises(StoreError, match="No space left"):
        store.add(**later())
    monkeypatch.undo()
    assert store.screened()[0]["Status"] == "in progress"
    assert (data_dir / "observations.csv").read_bytes() == observations_before
    assert [row["ID"] for row in store.rows()] == ["ID001"]
    assert snapshot_files(data_dir / "frames", data_dir / "crops") == names
    assert stray_files(data_dir) == []
    assert store.add(**later())["ID"] == "ID002"


def test_set_label_second_forward_rename_failure_restores_the_first_image(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    data_dir = tmp_path / "data"
    csv_before = (data_dir / "observations.csv").read_bytes()
    real_replace = os.replace
    calls = []

    def fail_the_second_rename(source, target):
        """Let the first rename and the undo through; refuse the second rename."""
        calls.append((str(source), str(target)))
        if len(calls) == 2:
            raise OSError(13, "Permission denied")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_the_second_rename)
    with pytest.raises(StoreError) as caught:
        store.set_label("ID001", "CDEL")
    monkeypatch.undo()
    assert "could not rename" in str(caught.value) and "Permission denied" in str(caught.value)
    assert str(data_dir / "crops" / row["CropFileName"]) in str(caught.value)
    assert [Path(call[0]).name for call in calls] == [
        row["FrameFileName"],
        row["CropFileName"],
        "ID001_CDEL_BOTTOMLEFT_FLC_T1.jpg",
    ]
    assert files_in(data_dir / "frames") == [row["FrameFileName"]]
    assert files_in(data_dir / "crops") == [row["CropFileName"]]
    assert (data_dir / "frames" / row["FrameFileName"]).read_bytes() == JPEG
    assert (data_dir / "crops" / row["CropFileName"]).read_bytes() == PNG
    assert (data_dir / "observations.csv").read_bytes() == csv_before
    assert files_in(data_dir / "trash") == []
    assert store.rows() == [row]


def test_tally_and_unlabeled_come_from_one_read(tmp_path):
    store = make_store(tmp_path)
    store.add(**sighting())
    store.add(**sighting(species_code="CDEL"))
    store.add(**later())
    reads = []
    real_read = store._read_observations

    def counted():
        """Count the reads of observations.csv."""
        reads.append(1)
        return real_read()

    store._read_observations = counted
    tally, unlabeled = store.tally_and_unlabeled()
    assert len(reads) == 1
    assert unlabeled == 1
    assert [item["code"] for item in tally] == ["ACAU", "CDEL"]
    assert store.tally() == tally


# ----- tracks -----

TRACK = {"points": [[83.0, 0.25, 0.75, 0.9], [83.72, 0.25, 0.75, 1.0], [84.5, 0.27, 0.74, 0.91]], "box": None}


def save_track(store, row, **changes):
    """Save a valid track for a row, the way the background job does, and return what was stored."""
    return store.tracks.save(*identity_of(row), {**TRACK, **changes})


def test_tracks_live_in_a_folder_the_first_save_creates(tmp_path):
    store = make_store(tmp_path)
    data_dir = tmp_path / "data"
    assert store_module.TRACKS_FOLDER == "tracks"
    assert store.tracks_dir == data_dir / "tracks"
    row = store.add(**sighting())
    assert not (data_dir / "tracks").exists()
    saved = save_track(store, row)
    assert files_in(data_dir / "tracks") == ["ID001.json"]
    assert store.tracks.load("ID001") == saved
    assert stray_files(data_dir) == []


def test_saving_a_track_changes_no_csv_file(tmp_path):
    store = make_store(tmp_path)
    store.mark_opened(KEY_2024, "LO", ["ACAU"])
    row = store.add(**sighting())
    data_dir = tmp_path / "data"
    before = {name: (data_dir / name).read_bytes() for name in ("observations.csv", "videos_screened.csv")}
    save_track(store, row)
    assert {name: (data_dir / name).read_bytes() for name in before} == before
    assert store.rows() == [row]


def test_delete_moves_the_track_to_the_trash_with_the_images(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    first = store.add(**sighting())
    second = store.add(**sighting(species_code="CDEL"))
    save_track(store, first)
    save_track(store, second, points=[[10.0, 0.1, 0.2, 1.0]])
    data_dir = tmp_path / "data"
    first_bytes = (data_dir / "tracks" / "ID001.json").read_bytes()
    second_bytes = (data_dir / "tracks" / "ID002.json").read_bytes()
    clock.advance(90)
    assert store.delete("ID001") == first
    assert files_in(data_dir / "tracks") == ["ID002.json"]
    assert (data_dir / "tracks" / "ID002.json").read_bytes() == second_bytes
    assert files_in(data_dir / "trash") == [
        "20260921T153130Z_ID001.json",
        "20260921T153130Z_ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg",
        "20260921T153130Z_ID001_ACAU_BOTTOMLEFT_FLC_T1.png",
    ]
    assert (data_dir / "trash" / "20260921T153130Z_ID001.json").read_bytes() == first_bytes
    assert store.tracks.load("ID001") is None and store.tracks.has("ID001") is False
    assert store.tracks.load("ID002")["points"] == [[10.0, 0.1, 0.2, 1.0]]
    assert stray_files(data_dir) == []


def test_delete_moves_the_track_of_a_later_point_too(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    row = store.add(**later())
    save_track(store, row)
    clock.advance(30)
    store.delete("ID001")
    assert files_in(tmp_path / "data" / "tracks") == []
    assert "20260921T153030Z_ID001.json" in files_in(tmp_path / "data" / "trash")


def test_delete_without_a_track_trashes_only_the_images(tmp_path):
    store = make_store(tmp_path, FixedClock())
    store.add(**sighting())
    store.delete("ID001")
    assert files_in(tmp_path / "data" / "trash") == [
        "20260921T153000Z_ID001_ACAU_BOTTOMLEFT_FLC_T1.jpg",
        "20260921T153000Z_ID001_ACAU_BOTTOMLEFT_FLC_T1.png",
    ]
    assert not (tmp_path / "data" / "tracks").exists()


def test_delete_says_the_row_is_removed_when_the_track_cannot_move(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    save_track(store, row)
    trash = tmp_path / "data" / "trash"
    trash.chmod(0o555)
    try:
        with pytest.raises(StoreError) as caught:
            store.delete("ID001")
    finally:
        trash.chmod(0o755)
    message = str(caught.value)
    assert "ID001 is removed" in message
    assert str(tmp_path / "data" / "tracks" / "ID001.json") in message
    assert row["FrameFileName"] in message and row["CropFileName"] in message
    assert store.rows() == []


def test_relabel_and_unlabel_keep_the_track(tmp_path):
    store = make_store(tmp_path)
    row = store.add(**sighting())
    saved = save_track(store, row, box={"w": 0.2, "h": 0.1})
    path = tmp_path / "data" / "tracks" / "ID001.json"
    before = path.read_bytes()
    os.utime(path, ns=(10 ** 18, 10 ** 18))
    for code in ("CDEL", None, "XMUT", "XMUT", None, "ACAU"):
        store.set_label("ID001", code)
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == 10 ** 18
        assert store.tracks.load("ID001") == saved
    assert files_in(tmp_path / "data" / "tracks") == ["ID001.json"]
    assert [name for name in files_in(tmp_path / "data" / "trash") if name.endswith(".json")] == []


def test_add_sets_aside_a_track_left_at_a_reused_id(tmp_path):
    clock = FixedClock()
    store = make_store(tmp_path, clock)
    kept = store.add(**sighting())
    gone = store.add(**sighting(key=KEY_2024_T2))
    save_track(store, kept)
    save_track(store, gone)
    store.delete("ID002")
    tracks = tmp_path / "data" / "tracks"
    (tracks / "ID002.json").write_bytes(b"left behind by a crash")
    untouched = (tracks / "ID001.json").read_bytes()
    clock.advance(60)
    again = store.add(**sighting(key=KEY_2024, time_seconds=12.0))
    assert again["ID"] == "ID002" and again["TimestampSeconds"] == "12.000"
    assert files_in(tracks) == ["ID001.json"]
    assert (tracks / "ID001.json").read_bytes() == untouched
    assert store.tracks.has("ID002") is False and store.tracks.load("ID002") is None
    trash = tmp_path / "data" / "trash"
    assert (trash / "20260921T153100Z_ID002.json").read_bytes() == b"left behind by a crash"
    assert "20260921T153000Z_ID002.json" in files_in(trash)


def test_add_that_finds_a_track_it_cannot_set_aside_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    tracks = tmp_path / "data" / "tracks"
    tracks.mkdir(parents=True)
    (tracks / "ID001.json").write_bytes(b"left behind by a crash")
    (tmp_path / "data" / "trash").chmod(0o555)
    try:
        with pytest.raises(StoreError) as caught:
            store.add(**sighting())
    finally:
        (tmp_path / "data" / "trash").chmod(0o755)
    assert str(tracks / "ID001.json") in str(caught.value)
    assert (tracks / "ID001.json").read_bytes() == b"left behind by a crash"
    assert store.rows() == []
    assert files_in(tmp_path / "data" / "frames") == [] and files_in(tmp_path / "data" / "crops") == []
    assert store.add(**sighting())["ID"] == "ID001"


@pytest.mark.parametrize(
    "cell, value, expected",
    [
        ("83.720", 83.72, True),
        ("83.720", 83.7204, True),
        ("83.720", 83.7196, True),
        ("83.720", 83.7206, False),
        ("83.720", 83.7194, False),
        ("83.7", 83.7, True),
        ("0", 0.0, True),
        ("soon", 83.72, False),
        ("", 83.72, False),
        ("nan", 83.72, False),
        ("inf", 83.72, False),
        ("-inf", 83.72, False),
        ("1e999", 83.72, False),
        ("83.720", float("nan"), False),
        ("83.720", float("inf"), False),
    ],
)
def test_cell_matches_compares_a_numeric_cell_within_the_time_tolerance(cell, value, expected):
    assert store_module.cell_matches(cell, value, store_module.TIME_MATCH_SECONDS) is expected


def test_cell_matches_takes_its_tolerance_from_the_caller():
    assert store_module.cell_matches("0.2500", 0.25004, store_module.POINT_MATCH_FRACTION) is True
    assert store_module.cell_matches("0.2500", 0.25006, store_module.POINT_MATCH_FRACTION) is False
    assert store_module.cell_matches("0.2500", 0.26, 0.02) is True


def test_match_tolerances_are_half_a_unit_of_the_last_stored_decimal():
    assert store_module.TIME_MATCH_SECONDS == 0.5 * 10 ** -store_module.SECONDS_DECIMALS
    assert store_module.POINT_MATCH_FRACTION == 0.5 * 10 ** -store_module.FRACTION_DECIMALS
    tree = ast.parse(Path(store_module.__file__).read_text(encoding="utf-8"))
    for name, decimals in (("TIME_MATCH_SECONDS", "SECONDS_DECIMALS"), ("POINT_MATCH_FRACTION", "FRACTION_DECIMALS")):
        [value] = [
            node.value for node in tree.body if isinstance(node, ast.Assign) and [t.id for t in node.targets] == [name]
        ]
        assert {leaf.id for leaf in ast.walk(value) if isinstance(leaf, ast.Name)} == {decimals}, name


def test_module_exposes_the_planned_interface():
    planned = {
        "OBSERVATION_COLUMNS", "LEGACY_OBSERVATION_COLUMNS", "SCREENED_COLUMNS", "LABEL_STATUS_COLUMN", "LABELED",
        "UNLABELED", "LATER_FILE_CODE", "TRACKS_FOLDER", "StoreError", "UnknownObservation", "ObservationStore",
        "ID_PREFIX", "ID_PATTERN", "TEMP_SUFFIX", "TIME_MATCH_SECONDS", "POINT_MATCH_FRACTION", "cell_matches",
    }  # fmt: skip
    assert planned <= set(dir(store_module))
    methods = {"add", "delete", "set_label", "rows", "tally", "mark_opened", "mark_done", "screened", "video_status"}
    assert methods <= set(dir(ObservationStore))
    assert issubclass(StoreError, Exception) and not issubclass(StoreError, ValueError)
