"""Tests for screener/coverage.py: watched time and the January reference counts."""

import json
import math
from pathlib import Path

import pytest

from screener.coverage import (
    MAX_RANGES_PER_REPORT,
    WatchStore,
    load_reference_counts,
    merge_ranges,
    summarize,
    validate_duration,
    validate_ranges,
)
from screener.species import Species
from screener.store import StoreError

KEY = "TCRMP_video_ondeck/2024Annual/TCRMP20241018_video_BIT_T1.MP4"
SPECIES = [
    Species(code="ACAU", name="Aplysina cauliformis", part="1", default_pin="1"),
    Species(code="AFUL", name="Aplysina fulva", part="2", default_pin="2"),
    Species(code="SAUR", name="Smenospongia aurea", part="3", default_pin=""),
]


# merge_ranges


def test_merge_joins_overlapping_and_touching_ranges():
    assert merge_ranges([(5, 10), (0, 5), (8, 12)]) == [(0, 12)]


def test_merge_bridges_a_short_gap_and_keeps_a_long_one():
    assert merge_ranges([(0, 10), (10.5, 20), (21.5, 40)]) == [(0, 20), (21.5, 40)]


def test_merge_of_nothing_is_nothing():
    assert merge_ranges([]) == []


# summarize


def test_summary_of_a_fully_watched_video():
    summary = summarize(100.0, [(0, 100)])
    assert summary["percent"] == 100 and summary["gaps"] == [] and summary["watched_seconds"] == 100


def test_summary_lists_the_stretches_not_watched():
    summary = summarize(100.0, [(10, 40), (60, 90)])
    assert summary["gaps"] == [[0, 10], [40, 60], [90, 100]]
    assert summary["percent"] == 60


def test_summary_ignores_short_gaps_at_both_ends():
    summary = summarize(100.0, [(0.5, 99.5)])
    assert summary["gaps"] == [] and summary["percent"] == 100


def test_summary_never_rounds_up_to_complete():
    summary = summarize(1000.0, [(0, 995)])
    assert summary["percent"] == 99 and summary["gaps"] == [[995, 1000]]


def test_summary_clips_ranges_to_the_duration():
    summary = summarize(50.0, [(0, 500)])
    assert summary["percent"] == 100 and summary["watched_seconds"] == 50


def test_summary_without_a_duration_has_no_percent():
    summary = summarize(None, [(0, 10)])
    assert summary["percent"] is None and summary["gaps"] == []


def test_summary_of_a_very_short_unwatched_video_is_not_complete():
    summary = summarize(0.8, [])
    assert summary["percent"] == 0 and summary["gaps"] == [[0, 0.8]]


def test_summary_arrow_taps_cannot_add_up_to_complete():
    summary = summarize(100.0, [(0, 10), (12, 20), (22, 30), (32, 40), (42, 100)])
    assert summary["percent"] == 92 and len(summary["gaps"]) == 4


def test_summary_of_an_unwatched_video():
    summary = summarize(100.0, [])
    assert summary["percent"] == 0 and summary["gaps"] == [[0, 100]]


# validation


@pytest.mark.parametrize(
    "bad",
    [
        "0-5",
        None,
        [[0]],
        [[0, 1, 2]],
        [["0", "1"]],
        [[True, 5]],
        [[5, 1]],
        [[-1, 5]],
        [[0, math.inf]],
        [[math.nan, 1]],
        [[0, 10 ** 9]],
        [[0, 1]] * (MAX_RANGES_PER_REPORT + 1),
    ],
)
def test_validate_ranges_refuses_bad_input(bad):
    with pytest.raises(ValueError, match="^ranges"):
        validate_ranges(bad)


def test_validate_ranges_accepts_numbers_and_an_empty_list():
    assert validate_ranges([[0, 1.5], [2, 3]]) == [(0.0, 1.5), (2.0, 3.0)]
    assert validate_ranges([]) == []


@pytest.mark.parametrize("bad", ["10", True, -1, 0, math.inf, math.nan, 10 ** 9])
def test_validate_duration_refuses_bad_input(bad):
    with pytest.raises(ValueError, match="^duration"):
        validate_duration(bad)


def test_validate_duration_allows_a_video_not_yet_measured():
    assert validate_duration(None) is None
    assert validate_duration(118.5) == 118.5


# WatchStore


def test_store_merges_reports_and_survives_a_reload(tmp_path: Path):
    path = tmp_path / "watched.json"
    store = WatchStore(path)
    store.report(KEY, [(0, 10)], 100.0)
    summary = store.report(KEY, [(10, 30)], 100.0)
    assert summary["percent"] == 30
    assert WatchStore(path).summary(KEY)["watched_seconds"] == 30


def test_store_repeating_a_report_changes_nothing(tmp_path: Path):
    store = WatchStore(tmp_path / "watched.json")
    store.report(KEY, [(0, 10)], 100.0)
    assert store.report(KEY, [(0, 10)], 100.0)["watched_seconds"] == 10


def test_store_keeps_the_known_duration_when_a_report_has_none(tmp_path: Path):
    store = WatchStore(tmp_path / "watched.json")
    store.report(KEY, [(0, 10)], 100.0)
    assert store.report(KEY, [(10, 20)], None)["duration"] == 100.0


def test_store_a_shorter_copy_cannot_hide_a_gap(tmp_path: Path):
    store = WatchStore(tmp_path / "watched.json")
    store.report(KEY, [(0, 50)], 100.0)
    summary = store.report(KEY, [], 50.0)
    assert summary["duration"] == 100.0 and summary["percent"] == 50


def test_store_failed_write_leaves_memory_matching_the_disk(tmp_path: Path, monkeypatch):
    store = WatchStore(tmp_path / "watched.json")
    store.report(KEY, [(0, 10)], 100.0)

    def refuse(*args):
        raise OSError("disk full")

    monkeypatch.setattr("screener.coverage.os.replace", refuse)
    with pytest.raises(StoreError, match="cannot be written"):
        store.report(KEY, [(10, 20)], 100.0)
    monkeypatch.undo()
    assert store.summary(KEY)["watched_seconds"] == 10
    assert store.report(KEY, [(10, 20)], 100.0)["watched_seconds"] == 20
    assert WatchStore(tmp_path / "watched.json").summary(KEY)["watched_seconds"] == 20
    assert [item.name for item in tmp_path.iterdir()] == ["watched.json"]


def test_store_answers_for_a_video_never_watched(tmp_path: Path):
    summary = WatchStore(tmp_path / "watched.json").summary(KEY)
    assert summary["duration"] is None and summary["percent"] is None and summary["watched_seconds"] == 0


def test_store_empty_report_writes_no_row_for_nothing(tmp_path: Path):
    path = tmp_path / "watched.json"
    WatchStore(path).report(KEY, [], None)
    assert not path.exists()


@pytest.mark.parametrize("text", ["{not json", "[]", '{"k": 3}', '{"k": {"duration": "x", "ranges": []}}'])
def test_store_refuses_a_corrupt_file_and_leaves_it_alone(tmp_path: Path, text: str):
    path = tmp_path / "watched.json"
    path.write_text(text, encoding="utf-8")
    store = WatchStore(path)
    with pytest.raises(StoreError, match="watched.json"):
        store.report(KEY, [(0, 1)], 10.0)
    assert path.read_text(encoding="utf-8") == text


def test_store_leaves_no_temp_file_behind(tmp_path: Path):
    WatchStore(tmp_path / "watched.json").report(KEY, [(0, 1)], 10.0)
    assert [item.name for item in tmp_path.iterdir()] == ["watched.json"]
    assert json.loads((tmp_path / "watched.json").read_text(encoding="utf-8"))[KEY]["ranges"] == [[0, 1]]


# load_reference_counts


def write_reference(path: Path, rows: str, header: str = "Site,Sponge Type,FileName") -> None:
    path.write_text(f"﻿{header}\n{rows}", encoding="utf-8")


def test_reference_counts_by_video_and_species_ignoring_case(tmp_path: Path):
    path = tmp_path / "reference.csv"
    write_reference(
        path,
        "BIT,Aplysina cauliformis,TCRMP20241018_video_BIT_T1.mp4\n"
        "BIT,Aplysina cauliformis,TCRMP20241018_video_BIT_T1.MP4\n"
        "BIT,Aplysina fulva,TCRMP20241018_video_BIT_T1.MP4\n"
        "FLC,Aplysina fulva,TCRMP20241022_video_FLC_T6.MP4\n",
    )
    counts = load_reference_counts(path, SPECIES)
    assert counts["tcrmp20241018_video_bit_t1.mp4"] == {"ACAU": 2, "AFUL": 1}
    assert counts["tcrmp20241022_video_flc_t6.mp4"] == {"AFUL": 1}


def test_reference_file_missing_means_no_counts(tmp_path: Path):
    assert load_reference_counts(tmp_path / "absent.csv", SPECIES) is None


def test_reference_skips_a_name_outside_the_species_list(tmp_path: Path):
    path = tmp_path / "reference.csv"
    write_reference(path, "BIT,Not a sponge,a.MP4\n")
    assert load_reference_counts(path, SPECIES) == {}


def test_reference_without_the_columns_is_an_error(tmp_path: Path):
    path = tmp_path / "reference.csv"
    write_reference(path, "a,b\n", header="Site,Other")
    with pytest.raises(StoreError, match="Sponge Type"):
        load_reference_counts(path, SPECIES)
