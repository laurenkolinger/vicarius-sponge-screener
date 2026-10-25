"""Tests for screener.tracks: the checks on a track, the track files, and their failure paths.

The TrackStore tests run against a real ObservationStore on a temp data folder,
because a track is saved under that store's write lock and checked against its
rows. Tracks reach TrackStore.save from inside the process (a background job),
so no HTTP route is involved here.
"""

import copy
import csv
import json
import math
import os
import threading
import time
from datetime import date, timedelta

import pytest

from screener import store as store_module
from screener import tracks as tracks_module
from screener.config import MAX_TIME_SECONDS
from screener.store import StoreError, UnknownObservation
from screener.tracks import MAX_TRACK_POINTS, TrackError, TrackStore, validate_track
from tests.store_helpers import (
    KEY_2024,
    KEY_2024_T2,
    START,
    FixedClock,
    add_sighting,
    files_in,
    hold_lock,
    identity_of,
    make_store,
    release_lock,
)

STAMP = "2026-09-21T15:30:00Z"
MARK = (0.25, 0.75)  # the point of every sighting that add_sighting() adds
SIGHTED_AT = 83.72
POINTS = [[83.0, 0.40, 0.50, 0.95], [83.72, 0.41, 0.50, 1.0], [84.5, 0.43, 0.52, 0.88]]
FIELDS = ("t", "x", "y", "score")
NAN = float("nan")
INF = float("inf")


@pytest.fixture
def clock():
    """A clock that stands still until the test moves it."""
    return FixedClock()


@pytest.fixture
def store(tmp_path, clock):
    """A store on a temp data folder with a fixed clock."""
    return make_store(tmp_path, clock)


@pytest.fixture
def tracks_dir(tmp_path):
    """The folder the store keeps its track files in."""
    return tmp_path / "data" / "tracks"


def track(**changes):
    """Return a valid track, with changes applied."""
    fields = {"points": copy.deepcopy(POINTS), "box": None}
    fields.update(changes)
    return fields


def save(store, row, **changes):
    """Save a valid track for a row the way the background job will."""
    return store.tracks.save(*identity_of(row), track(**changes))


def set_cell(path, column, value):
    """Rewrite one cell of the only data row of a CSV file."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        records = list(csv.reader(handle))
    records[1][records[0].index(column)] = value
    with open(path, "w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(records)


def one_point(position=None, value=None):
    """Return a track with one point; when a position (0 to 3) is given, put value there."""
    point = [5.0, 0.5, 0.5, 1.0]
    if position is not None:
        point[position] = value
    return {"points": [point], "box": None}


# ----- validate_track: what is accepted -----


def test_validate_track_accepts_a_point_track_and_returns_floats():
    checked = validate_track(track())
    assert checked == {"points": POINTS, "box": None}
    assert all(type(value) is float for point in checked["points"] for value in point)


def test_validate_track_accepts_a_box_and_keeps_only_its_size():
    checked = validate_track(track(box={"w": 1, "h": 0.25, "x": 0.5, "note": "ignored"}))
    assert checked["box"] == {"w": 1.0, "h": 0.25}
    assert all(type(value) is float for value in checked["box"].values())


def test_validate_track_accepts_the_limits_of_every_range():
    edges = {"points": [[0, 0, 0, 0], [MAX_TIME_SECONDS, 1, 1, 1]], "box": {"w": 1, "h": 1}}
    assert validate_track(edges) == {
        "points": [[0.0, 0.0, 0.0, 0.0], [86400.0, 1.0, 1.0, 1.0]],
        "box": {"w": 1.0, "h": 1.0},
    }
    tiny = validate_track({"points": [[0, 0.5, 0.5, 1]], "box": {"w": 1e-9, "h": 1e-9}})
    assert tiny["box"] == {"w": 1e-9, "h": 1e-9}
    longest = {"points": [[index * 0.01, 0.5, 0.5, 1.0] for index in range(MAX_TRACK_POINTS)], "box": None}
    assert len(validate_track(longest)["points"]) == MAX_TRACK_POINTS
    assert MAX_TRACK_POINTS == 3000


def test_validate_track_accepts_tuples_and_ignores_other_fields():
    checked = validate_track({"points": ((1, 0.5, 0.5, 1), (2, 0.5, 0.5, 1)), "box": None, "comment": "kept out"})
    assert checked == {"points": [[1.0, 0.5, 0.5, 1.0], [2.0, 0.5, 0.5, 1.0]], "box": None}


def test_validate_track_never_returns_a_negative_zero():
    checked = validate_track({"points": [[-0.0, -0.0, -0.0, -0.0]], "box": None})
    assert [math.copysign(1.0, value) for value in checked["points"][0]] == [1.0, 1.0, 1.0, 1.0]


def test_validate_track_returns_new_lists_and_leaves_its_input_alone():
    body = track(box={"w": 0.1, "h": 0.2})
    before = copy.deepcopy(body)
    checked = validate_track(body)
    assert body == before
    checked["points"][0][0] = 99.0
    checked["box"]["w"] = 0.9
    assert body == before
    body["points"][1][1] = 0.99
    assert validate_track(body)["points"][1][1] == 0.99
    assert checked["points"][1][1] == POINTS[1][1]


# ----- validate_track: what is refused -----


def refused(body):
    """Return the message validate_track raises for a body, after checking its type."""
    with pytest.raises(ValueError) as caught:
        validate_track(body)
    assert not isinstance(caught.value, TrackError)
    return str(caught.value)


@pytest.mark.parametrize("body", [None, [], [track()], "points", 7, 1.5, True, (1, 2)])
def test_validate_track_refuses_a_track_that_is_not_an_object(body):
    assert refused(body).startswith("track: expected an object")


def test_validate_track_names_a_missing_points_and_box():
    assert refused({}) == "points: missing"
    assert refused({"box": None}) == "points: missing"
    assert refused({"points": POINTS}) == "box: missing"


@pytest.mark.parametrize("points", [None, "abc", "abcd", 5, 1.5, True, {"0": [1, 0.5, 0.5, 1]}, {1, 2}])
def test_validate_track_refuses_points_that_are_not_a_list(points):
    assert refused(track(points=points)).startswith("points: expected a list")


def test_validate_track_refuses_an_empty_track_and_too_many_points():
    assert refused(track(points=[])) == "points: expected 1 to 3000 points, got 0"
    too_many = [[index * 0.01, 0.5, 0.5, 1.0] for index in range(MAX_TRACK_POINTS + 1)]
    assert refused(track(points=too_many)) == "points: expected 1 to 3000 points, got 3001"


@pytest.mark.parametrize(
    "point",
    [
        5, 1.5, None, True, "abcd", "", {"t": 0, "x": 0.5, "y": 0.5, "score": 1},
        [], [1], [1, 2, 3], [1, 2, 3, 4, 5], [[1, 2, 3, 4]],
    ],  # fmt: skip
)
def test_validate_track_refuses_a_point_that_is_not_four_numbers_in_a_list(point):
    message = refused(track(points=[[1.0, 0.5, 0.5, 1.0], point]))
    assert message.startswith("points[1]: expected a list of 4 numbers")


NOT_NUMBERS = [
    ("a string", "0.5"),
    ("an empty string", ""),
    ("true", True),
    ("false", False),
    ("null", None),
    ("a list", [0.5]),
    ("a nested list", [[0.5]]),
    ("an object", {"v": 0.5}),
    ("bytes", b"1"),
]
NOT_FINITE = [("nan", NAN), ("infinity", INF), ("negative infinity", -INF)]


@pytest.mark.parametrize("position", range(4), ids=FIELDS)
@pytest.mark.parametrize("label, value", NOT_NUMBERS + NOT_FINITE, ids=[label for label, _ in NOT_NUMBERS + NOT_FINITE])
def test_validate_track_refuses_a_value_that_is_not_a_finite_number(position, label, value):
    assert refused(one_point(position, value)).startswith(f"points[0].{FIELDS[position]}: expected a ")


OUT_OF_RANGE = [(0, value) for value in (-0.001, -1, MAX_TIME_SECONDS + 0.001, 10 ** 400, -(10 ** 400))] + [
    (position, value)
    for position in (1, 2, 3)
    for value in (-0.001, -1, 1.001, 2, 10 ** 400, -(10 ** 400))
]


@pytest.mark.parametrize("position, value", OUT_OF_RANGE)
def test_validate_track_refuses_a_value_outside_its_range(position, value):
    message = refused(one_point(position, value))
    assert message.startswith(f"points[0].{FIELDS[position]}: expected ")
    assert "got" in message


@pytest.mark.parametrize(
    "times, index",
    [
        ([1.0, 1.0], 1),
        ([2.0, 1.0], 1),
        ([1.0, 2.0, 2.0], 2),
        ([1.0, 3.0, 2.0], 2),
        ([0.0, 0.0], 1),
        ([5.0, 4.999999], 1),
    ],
)
def test_validate_track_refuses_times_that_do_not_strictly_increase(times, index):
    points = [[time_value, 0.5, 0.5, 1.0] for time_value in times]
    assert refused(track(points=points)).startswith(f"points[{index}].t: expected a time after ")


def test_validate_track_names_the_first_bad_point():
    points = [[1.0, 0.5, 0.5, 1.0], [2.0, 0.5, 0.5, 1.0], [3.0, 1.5, 0.5, 1.0], [2.5, 0.5, 0.5, 1.0]]
    assert refused(track(points=points)).startswith("points[2].x:")


BAD_BOXES = [
    ("text", "none", "box: expected null or an object"),
    ("a list", [0.1, 0.1], "box: expected null or an object"),
    ("an empty list", [], "box: expected null or an object"),
    ("a number", 5, "box: expected null or an object"),
    ("true", True, "box: expected null or an object"),
    ("an empty object", {}, "box.w: missing"),
    ("no height", {"w": 0.1}, "box.h: missing"),
    ("no width", {"h": 0.1}, "box.w: missing"),
    ("zero width", {"w": 0, "h": 0.1}, "box.w: expected a fraction above 0"),
    ("zero height", {"w": 0.1, "h": 0.0}, "box.h: expected a fraction above 0"),
    ("negative width", {"w": -0.1, "h": 0.1}, "box.w: expected a fraction above 0"),
    ("width over one", {"w": 1.0001, "h": 0.1}, "box.w: expected a fraction above 0"),
    ("height over one", {"w": 0.1, "h": 1.0001}, "box.h: expected a fraction above 0"),
    ("a huge width", {"w": 10 ** 400, "h": 0.1}, "box.w: expected a fraction above 0"),
    ("nan width", {"w": NAN, "h": 0.1}, "box.w: expected a finite number"),
    ("infinite height", {"w": 0.1, "h": INF}, "box.h: expected a finite number"),
    ("a string width", {"w": "0.1", "h": 0.1}, "box.w: expected a number"),
    ("a bool width", {"w": True, "h": 0.1}, "box.w: expected a number"),
    ("a null height", {"w": 0.1, "h": None}, "box.h: expected a number"),
    ("a list height", {"w": 0.1, "h": [0.1]}, "box.h: expected a number"),
]


@pytest.mark.parametrize("label, box, start", BAD_BOXES, ids=[item[0] for item in BAD_BOXES])
def test_validate_track_refuses_a_box_that_is_not_null_or_a_valid_size(label, box, start):
    assert refused(track(box=box)).startswith(start)


# ----- TrackStore.save -----


def test_save_writes_the_planned_file(store, tracks_dir):
    row = add_sighting(store)
    assert not tracks_dir.exists()
    saved = store.tracks.save(row["ID"], KEY_2024, SIGHTED_AT, MARK, track())
    path = tracks_dir / "ID001.json"
    assert store.tracks_dir == tracks_dir
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "id": "ID001",
        "key": KEY_2024,
        "anchor_time": 83.72,
        "points": POINTS,
        "box": None,
        "updated_at": STAMP,
    }
    assert saved == {"points": POINTS, "box": None, "anchor_time": 83.72, "updated_at": STAMP}
    assert path.stat().st_mode & 0o777 == 0o644
    assert files_in(tracks_dir) == ["ID001.json"]
    assert store.tracks.load("ID001") == saved


def test_save_replaces_the_stored_track(store, tracks_dir, clock):
    row = add_sighting(store)
    save(store, row)
    later_stamp = clock.advance(90)
    second = save(store, row, points=[[83.72, 0.6, 0.7, 1.0]], box={"w": 0.2, "h": 0.1})
    assert second == {
        "points": [[83.72, 0.6, 0.7, 1.0]],
        "box": {"w": 0.2, "h": 0.1},
        "anchor_time": 83.72,
        "updated_at": later_stamp,
    }
    assert json.loads((tracks_dir / "ID001.json").read_text(encoding="utf-8"))["points"] == [[83.72, 0.6, 0.7, 1.0]]
    assert store.tracks.load("ID001") == second
    assert files_in(tracks_dir) == ["ID001.json"]


def test_save_records_the_rows_own_key_and_time(store, tracks_dir):
    row = add_sighting(store, time_seconds=83.72)
    saved = store.tracks.save(row["ID"], KEY_2024, 83.7204, MARK, track())
    assert saved["anchor_time"] == float(row["TimestampSeconds"]) == 83.72
    assert json.loads((tracks_dir / "ID001.json").read_text(encoding="utf-8"))["key"] == row["S3Key"] == KEY_2024


def test_save_keeps_the_tracks_of_other_sightings(store, tracks_dir):
    first, second = add_sighting(store), add_sighting(store, key=KEY_2024_T2, time_seconds=12.0)
    save(store, first)
    other = save(store, second, points=[[12.0, 0.1, 0.2, 1.0]])
    assert files_in(tracks_dir) == ["ID001.json", "ID002.json"]
    assert store.tracks.load("ID001")["points"] == POINTS
    assert store.tracks.load("ID002") == other


def test_save_checks_the_track_before_it_writes(store, tracks_dir):
    row = add_sighting(store)
    good = save(store, row)
    before = (tracks_dir / "ID001.json").read_bytes()
    for bad in (track(points=[]), track(box="x"), {"points": POINTS}, [1, 2], None, track(points=[[1, 2, 3, 4]])):
        with pytest.raises(ValueError):
            store.tracks.save("ID001", KEY_2024, SIGHTED_AT, MARK, bad)
    assert (tracks_dir / "ID001.json").read_bytes() == before
    assert store.tracks.load("ID001") == good
    assert files_in(tracks_dir) == ["ID001.json"]


def test_save_refuses_ids_that_are_not_plain_sighting_ids(store, tmp_path):
    add_sighting(store)
    outside = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    hostile = [
        "../ID001", "ID001/../ID002", "ID001/x", "ID001.json", "ID001\\x", "id001", "ID", "ID-1", "ID1.5", "",
        " ID001", "ID001 ", "ID001\n", "ID\u0661\u0662", "ID001\x00", "/etc/passwd", "..", ".", "~", "%2e%2e",
        7, None, ["ID001"], b"ID001",
    ]  # fmt: skip
    for obs_id in hostile:
        with pytest.raises(ValueError, match="^obs_id:"):
            store.tracks.save(obs_id, KEY_2024, SIGHTED_AT, MARK, track())
        with pytest.raises(ValueError, match="^obs_id:"):
            store.tracks.has(obs_id)
        with pytest.raises(ValueError, match="^obs_id:"):
            store.tracks.load(obs_id)
        with pytest.raises(ValueError, match="^obs_id:"):
            store.tracks.trash(obs_id, START)
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == outside


@pytest.mark.parametrize(
    "key, anchor_time, start",
    [
        ("", SIGHTED_AT, "key:"),
        (None, SIGHTED_AT, "key:"),
        (5, SIGHTED_AT, "key:"),
        (["k"], SIGHTED_AT, "key:"),
        (KEY_2024, None, "anchor_time:"),
        (KEY_2024, "83.72", "anchor_time:"),
        (KEY_2024, True, "anchor_time:"),
        (KEY_2024, NAN, "anchor_time:"),
        (KEY_2024, INF, "anchor_time:"),
        (KEY_2024, -0.001, "anchor_time:"),
        (KEY_2024, MAX_TIME_SECONDS + 0.001, "anchor_time:"),
        (KEY_2024, 10 ** 400, "anchor_time:"),
    ],
)
def test_save_refuses_a_bad_key_or_anchor_time(store, tracks_dir, key, anchor_time, start):
    add_sighting(store)
    with pytest.raises(ValueError) as caught:
        store.tracks.save("ID001", key, anchor_time, MARK, track())
    assert str(caught.value).startswith(start)
    assert not isinstance(caught.value, StoreError)
    assert not tracks_dir.exists()


def test_save_for_an_unknown_sighting_raises_unknown_observation_and_writes_nothing(store, tracks_dir):
    with pytest.raises(UnknownObservation, match="ID001"):
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, MARK, track())
    add_sighting(store)
    with pytest.raises(UnknownObservation, match="ID002"):
        store.tracks.save("ID002", KEY_2024, SIGHTED_AT, MARK, track())
    assert not tracks_dir.exists()
    assert store.tracks.for_ids(["ID001", "ID002"]) == {}


def test_save_after_a_delete_raises_unknown_observation_and_leaves_no_track(store, tracks_dir):
    row = add_sighting(store)
    store.delete(row["ID"])
    with pytest.raises(UnknownObservation):
        store.tracks.save(row["ID"], row["S3Key"], SIGHTED_AT, MARK, track())
    assert not store.tracks.has("ID001")
    assert files_in(tracks_dir) == []


def test_save_refuses_a_track_computed_for_another_video_or_time_at_the_same_id(store, tracks_dir):
    old = add_sighting(store)
    store.delete(old["ID"])
    new = add_sighting(store, key=KEY_2024_T2, time_seconds=12.0)
    assert new["ID"] == old["ID"] == "ID001"
    with pytest.raises(UnknownObservation, match="ID001") as caught:
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, MARK, track())
    assert KEY_2024_T2 in str(caught.value) and "12.000" in str(caught.value)
    with pytest.raises(UnknownObservation):
        store.tracks.save("ID001", KEY_2024_T2, SIGHTED_AT, MARK, track())
    with pytest.raises(UnknownObservation):
        store.tracks.save("ID001", KEY_2024, 12.0, MARK, track())
    assert not store.tracks.has("ID001")
    assert files_in(tracks_dir) == []
    assert store.tracks.save("ID001", KEY_2024_T2, 12.0, MARK, track())["anchor_time"] == 12.0


def test_save_matches_a_time_that_rounds_to_the_rows_cell(store):
    row = add_sighting(store, time_seconds=83.7196)
    assert row["TimestampSeconds"] == "83.720"
    for seconds in (83.72, 83.7196, 83.7204, 83.7199):
        store.tracks.save("ID001", KEY_2024, seconds, MARK, track())
    for seconds in (83.7206, 83.7194, 83.0, 0.0):
        with pytest.raises(UnknownObservation):
            store.tracks.save("ID001", KEY_2024, seconds, MARK, track())


def test_save_refuses_a_track_computed_for_another_mark_on_the_same_frame(store, tracks_dir):
    """Delete a sighting and mark the same paused frame again: the ID comes back with the same video and time."""
    old = add_sighting(store, point={"x": 0.10, "y": 0.10})
    store.delete(old["ID"])
    new = add_sighting(store, point={"x": 0.80, "y": 0.70})
    assert (new["ID"], new["S3Key"], new["TimestampSeconds"]) == (old["ID"], old["S3Key"], old["TimestampSeconds"])
    assert (new["PointX"], new["PointY"]) == ("0.8000", "0.7000")
    with pytest.raises(UnknownObservation, match="ID001") as caught:
        store.tracks.save(*identity_of(old), track())
    assert "(0.1000, 0.1000)" in str(caught.value) and "(0.8000, 0.7000)" in str(caught.value)
    assert not store.tracks.has("ID001")
    assert files_in(tracks_dir) == []
    saved = store.tracks.save(*identity_of(new), track())
    assert saved["points"] == POINTS and store.tracks.has("ID001")


def test_save_accepts_a_track_for_the_same_mark_marked_again(store):
    """A re-mark at the same place is the same mark, so the old result is the right track for it."""
    old = add_sighting(store, point={"x": 0.10, "y": 0.10})
    store.delete(old["ID"])
    new = add_sighting(store, point={"x": 0.10, "y": 0.10})
    assert new["ID"] == old["ID"]
    saved = store.tracks.save(*identity_of(old), track())
    assert store.tracks.load("ID001") == saved
    assert store.tracks.save(*identity_of(new), track())["anchor_time"] == float(new["TimestampSeconds"])


@pytest.mark.parametrize(
    "mark",
    [(0.2501, 0.75), (0.2499, 0.75), (0.25, 0.7501), (0.25, 0.7499), (0.30, 0.70), (0.75, 0.25), (0.0, 0.0)],
)
def test_save_refuses_a_point_that_is_not_the_rows(store, tracks_dir, mark):
    add_sighting(store)
    with pytest.raises(UnknownObservation, match="ID001"):
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, mark, track())
    assert files_in(tracks_dir) == []


def test_save_matches_a_point_that_rounds_to_the_rows_cells(store):
    row = add_sighting(store, point={"x": 0.25004, "y": 0.74996})
    assert (row["PointX"], row["PointY"]) == ("0.2500", "0.7500")
    for mark in ((0.25, 0.75), (0.25004, 0.74996), (0.249951, 0.750049), (0.250049, 0.749951), [0.25, 0.75]):
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, mark, track())
    for mark in ((0.250051, 0.75), (0.25, 0.749949), (0.249949, 0.75), (0.25, 0.750051)):
        with pytest.raises(UnknownObservation):
            store.tracks.save("ID001", KEY_2024, SIGHTED_AT, mark, track())


@pytest.mark.parametrize("column", ["PointX", "PointY"])
@pytest.mark.parametrize("cell", ["left", "", "nan"])
def test_save_reads_a_row_whose_point_cell_is_not_a_number_as_another_mark(store, tracks_dir, column, cell):
    add_sighting(store)
    set_cell(store.observations_path, column, cell)
    with pytest.raises(UnknownObservation, match="ID001"):
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, MARK, track())
    assert not tracks_dir.exists()


@pytest.mark.parametrize(
    "point, start",
    [
        (None, "point:"),
        ("xy", "point:"),
        (5, "point:"),
        (b"ab", "point:"),
        ([0.25], "point:"),
        ([0.25, 0.75, 0.5], "point:"),
        ({"x": 0.25, "y": 0.75}, "point:"),
        ((True, 0.75), "point.x:"),
        (("0.25", 0.75), "point.x:"),
        ((None, 0.75), "point.x:"),
        ((NAN, 0.75), "point.x:"),
        ((INF, 0.75), "point.x:"),
        ((-0.001, 0.75), "point.x:"),
        ((1.001, 0.75), "point.x:"),
        ((10 ** 400, 0.75), "point.x:"),
        ((0.25, True), "point.y:"),
        ((0.25, "0.75"), "point.y:"),
        ((0.25, None), "point.y:"),
        ((0.25, NAN), "point.y:"),
        ((0.25, -INF), "point.y:"),
        ((0.25, 1.001), "point.y:"),
        ((0.25, [0.75]), "point.y:"),
    ],
)
def test_save_refuses_a_point_that_is_not_two_fractions(store, tracks_dir, point, start):
    add_sighting(store)
    with pytest.raises(ValueError) as caught:
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, point, track())
    assert str(caught.value).startswith(start)
    assert not isinstance(caught.value, StoreError)
    assert not tracks_dir.exists()


def test_save_reads_a_row_whose_time_cell_is_not_a_number_as_another_sighting(store, tracks_dir):
    row = add_sighting(store)
    path = store.observations_path
    path.write_text(path.read_text(encoding="utf-8").replace(row["TimestampSeconds"], "soon"), encoding="utf-8")
    with pytest.raises(UnknownObservation, match="soon"):
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, MARK, track())
    path.write_text(path.read_text(encoding="utf-8").replace("soon", "nan"), encoding="utf-8")
    with pytest.raises(UnknownObservation, match="nan"):
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, MARK, track())
    assert not tracks_dir.exists()


def test_save_leaves_a_corrupt_observations_file_alone(store, tracks_dir):
    add_sighting(store)
    store.observations_path.write_text("Site,Transect\nx,y\n", encoding="utf-8")
    before = store.observations_path.read_bytes()
    with pytest.raises(StoreError, match="observations.csv"):
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, MARK, track())
    assert store.observations_path.read_bytes() == before
    assert not tracks_dir.exists()


def test_failed_swap_keeps_the_old_file_and_removes_the_temp_file(store, tracks_dir, monkeypatch):
    row = add_sighting(store)
    save(store, row)
    before = (tracks_dir / "ID001.json").read_bytes()

    def full_disk(source, target):
        """Stand in for os.replace on a full disk."""
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", full_disk)
    with pytest.raises(TrackError) as caught:
        save(store, row, points=[[83.72, 0.9, 0.9, 1.0]])
    monkeypatch.undo()
    assert str(caught.value).startswith(str(tracks_dir / "ID001.json"))
    assert "No space left" in str(caught.value)
    assert not isinstance(caught.value, (StoreError, ValueError))
    assert (tracks_dir / "ID001.json").read_bytes() == before
    assert files_in(tracks_dir) == ["ID001.json"]
    assert save(store, row, points=[[83.72, 0.9, 0.9, 1.0]])["points"] == [[83.72, 0.9, 0.9, 1.0]]


def test_save_into_an_unwritable_folder_raises_track_error_naming_the_file(store, tracks_dir):
    row = add_sighting(store)
    save(store, row)
    tracks_dir.chmod(0o555)
    try:
        with pytest.raises(TrackError) as caught:
            save(store, row, points=[[83.72, 0.9, 0.9, 1.0]])
    finally:
        tracks_dir.chmod(0o755)
    assert str(caught.value).startswith(str(tracks_dir / "ID001.json"))
    assert store.tracks.load("ID001")["points"] == POINTS
    assert files_in(tracks_dir) == ["ID001.json"]


def test_save_cannot_create_its_folder_under_a_plain_file(store, tmp_path):
    add_sighting(store)
    (tmp_path / "data" / "tracks").write_text("in the way", encoding="utf-8")
    with pytest.raises(TrackError, match="ID001.json"):
        store.tracks.save("ID001", KEY_2024, SIGHTED_AT, MARK, track())
    assert (tmp_path / "data" / "tracks").read_text(encoding="utf-8") == "in the way"


def test_save_waits_for_a_writer_that_lets_go_in_time(store, tmp_path):
    row = add_sighting(store)
    descriptor = hold_lock(tmp_path / "data")
    releaser = threading.Timer(0.2, release_lock, args=(descriptor,))
    started = time.monotonic()
    releaser.start()
    try:
        saved = save(store, row)
    finally:
        releaser.join()
    assert time.monotonic() - started >= 0.15
    assert saved["points"] == POINTS


def test_save_gives_up_with_a_clear_error_when_another_writer_keeps_the_lock(store, tmp_path, tracks_dir, monkeypatch):
    row = add_sighting(store)
    monkeypatch.setattr(store_module, "LOCK_WAIT_SECONDS", 0.2)
    descriptor = hold_lock(tmp_path / "data")
    try:
        with pytest.raises(StoreError) as caught:
            save(store, row)
    finally:
        release_lock(descriptor)
    assert ".lock" in str(caught.value) and "0.2 seconds" in str(caught.value)
    assert not tracks_dir.exists()
    assert save(store, row)["points"] == POINTS


def test_a_delete_that_queues_up_during_a_save_cannot_leave_a_track_behind(store, tracks_dir, monkeypatch):
    row = add_sighting(store)
    real_read = store._read_observations
    real_write = store.tracks._write
    state = {"deleter": None}

    def read_then_start_a_delete():
        """Read the rows, then start a delete that has to wait for the lock the save holds."""
        rows = real_read()
        if state["deleter"] is None:
            state["deleter"] = threading.Thread(target=store.delete, args=(row["ID"],))
            state["deleter"].start()
        return rows

    def write_after_the_delete_had_its_chance(path, record):
        """Give the queued delete time to finish before the file is written, as far as the lock lets it."""
        state["deleter"].join(timeout=0.3)
        real_write(path, record)

    monkeypatch.setattr(store, "_read_observations", read_then_start_a_delete)
    monkeypatch.setattr(store.tracks, "_write", write_after_the_delete_had_its_chance)
    save(store, row)
    state["deleter"].join(timeout=30)
    monkeypatch.undo()
    assert not state["deleter"].is_alive()
    assert store.rows() == []
    assert not store.tracks.has("ID001")
    assert files_in(tracks_dir) == []
    assert [name for name in files_in(store.trash_dir) if name.endswith("_ID001.json")] != []


def test_a_save_that_queues_up_during_a_delete_finds_the_sighting_gone(store, tracks_dir, monkeypatch):
    row = add_sighting(store)
    real_read = store._read_observations
    state = {"saver": None, "raised": []}

    def attempt_save():
        """Try to save a track and note how it ended."""
        try:
            save(store, row)
        except UnknownObservation as error:
            state["raised"].append(error)

    def read_then_let_a_save_queue_up():
        """Read the rows, then start a save that has to wait for the lock the delete holds."""
        rows = real_read()
        if state["saver"] is None:
            state["saver"] = threading.Thread(target=attempt_save)
            state["saver"].start()
            time.sleep(0.2)
        return rows

    monkeypatch.setattr(store, "_read_observations", read_then_let_a_save_queue_up)
    store.delete(row["ID"])
    state["saver"].join(timeout=30)
    monkeypatch.undo()
    assert len(state["raised"]) == 1
    assert store.rows() == [] and files_in(tracks_dir) == []


def test_many_saves_and_a_delete_at_once_leave_a_track_only_for_sightings_that_remain(store, tracks_dir):
    rows = [add_sighting(store, time_seconds=float(number)) for number in range(1, 7)]
    failures = []

    def keep_saving(row):
        """Save the track of one sighting over and over."""
        try:
            for _ in range(15):
                save(store, row)
        except UnknownObservation:
            pass
        except Exception as error:  # a failure in a thread must fail the test, not vanish
            failures.append(repr(error))

    def delete_some():
        """Delete every other sighting while the saves run."""
        try:
            for row in rows[::2]:
                time.sleep(0.01)
                store.delete(row["ID"])
        except Exception as error:
            failures.append(repr(error))

    threads = [threading.Thread(target=keep_saving, args=(row,)) for row in rows]
    threads.append(threading.Thread(target=delete_some))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert failures == []
    remaining = {row["ID"] for row in store.rows()}
    assert remaining == {row["ID"] for row in rows[1::2]}
    assert {name[:-5] for name in files_in(tracks_dir)} <= remaining
    for obs_id in remaining:
        assert store.tracks.load(obs_id)["points"] == POINTS


# ----- TrackStore.has, load, for_ids -----


def test_has_tells_whether_a_track_file_exists(store, tracks_dir):
    row = add_sighting(store)
    assert store.tracks.has("ID001") is False
    assert store.tracks.has("ID099") is False
    save(store, row)
    assert store.tracks.has("ID001") is True
    assert store.tracks.has("ID002") is False
    (tracks_dir / "ID002.json").mkdir()
    assert store.tracks.has("ID002") is False


def test_has_counts_a_damaged_file_and_does_not_read_it(store, tracks_dir):
    tracks_dir.mkdir(parents=True)
    (tracks_dir / "ID001.json").write_bytes(b"{nope")
    assert store.tracks.has("ID001") is True
    with pytest.raises(TrackError):
        store.tracks.load("ID001")


def record(**changes):
    """Return a valid track file record, with changes applied."""
    fields = {
        "id": "ID001",
        "key": KEY_2024,
        "anchor_time": SIGHTED_AT,
        "points": copy.deepcopy(POINTS),
        "box": None,
        "updated_at": STAMP,
    }
    fields.update(changes)
    return fields


def raw(**changes):
    """Return the bytes of a track file record, with changes applied."""
    return json.dumps(record(**changes)).encode("utf-8")


def raw_without(name):
    """Return the bytes of a track file record that lacks one field."""
    fields = record()
    del fields[name]
    return json.dumps(fields).encode("utf-8")


DAMAGED_FILES = [
    ("empty", b""),
    ("not json", b"{nope"),
    ("cut off", raw()[:40]),
    ("not utf-8", b"\xff\xfe\x00\x00"),
    ("an array", b"[1, 2]"),
    ("null", b"null"),
    ("a number", b"5"),
    ("a nan literal", raw().replace(b"0.41", b"NaN")),
    ("an infinity literal", raw().replace(b"0.41", b"Infinity")),
    ("another id", raw(id="ID002")),
    ("no id", raw_without("id")),
    ("a blank key", raw(key="")),
    ("a number key", raw(key=5)),
    ("no key", raw_without("key")),
    ("a text anchor", raw(anchor_time="83.72")),
    ("a bool anchor", raw(anchor_time=True)),
    ("a negative anchor", raw(anchor_time=-1)),
    ("an anchor past the limit", raw(anchor_time=MAX_TIME_SECONDS + 1)),
    ("no anchor", raw_without("anchor_time")),
    ("no points", raw_without("points")),
    ("points that are text", raw(points="abc")),
    ("empty points", raw(points=[])),
    ("unsorted points", raw(points=[[2.0, 0.5, 0.5, 1.0], [1.0, 0.5, 0.5, 1.0]])),
    ("a point out of range", raw(points=[[1.0, 2.0, 0.5, 1.0]])),
    ("too many points", raw(points=[[index * 0.01, 0.5, 0.5, 1.0] for index in range(MAX_TRACK_POINTS + 1)])),
    ("a text box", raw(box="big")),
    ("a box without a height", raw(box={"w": 0.1})),
    ("no box", raw_without("box")),
    ("a number stamp", raw(updated_at=5)),
    ("a blank stamp", raw(updated_at="")),
    ("no stamp", raw_without("updated_at")),
]


@pytest.mark.parametrize("label, content", DAMAGED_FILES, ids=[item[0] for item in DAMAGED_FILES])
def test_load_refuses_a_damaged_file_and_names_its_path(store, tracks_dir, label, content):
    tracks_dir.mkdir(parents=True)
    path = tracks_dir / "ID001.json"
    path.write_bytes(content)
    with pytest.raises(TrackError) as caught:
        store.tracks.load("ID001")
    assert str(caught.value).startswith(str(path) + ":")
    assert path.read_bytes() == content


def test_load_refuses_a_folder_where_the_file_should_be(store, tracks_dir):
    (tracks_dir / "ID001.json").mkdir(parents=True)
    with pytest.raises(TrackError, match="ID001.json"):
        store.tracks.load("ID001")


def test_load_returns_none_for_a_sighting_without_a_track(store, tracks_dir):
    assert store.tracks.load("ID001") is None
    tracks_dir.mkdir(parents=True)
    assert store.tracks.load("ID001") is None


def test_load_reads_a_hand_written_file_in_the_planned_format(store, tracks_dir):
    tracks_dir.mkdir(parents=True)
    (tracks_dir / "ID001.json").write_bytes(raw(box={"w": 0.2, "h": 0.1}, comment="extra fields are fine"))
    assert store.tracks.load("ID001") == {
        "points": POINTS,
        "box": {"w": 0.2, "h": 0.1},
        "anchor_time": SIGHTED_AT,
        "updated_at": STAMP,
    }


def test_for_ids_returns_the_public_shape_in_the_order_asked(store):
    first, _untracked, third = (add_sighting(store, time_seconds=float(number)) for number in (10, 20, 30))
    saved_third = save(store, third, points=[[30.0, 0.3, 0.3, 1.0]])
    saved_first = save(store, first, points=[[10.0, 0.1, 0.1, 1.0]], box={"w": 0.1, "h": 0.1})
    assert store.tracks.for_ids(["ID001", "ID002", "ID003"]) == {"ID001": saved_first, "ID003": saved_third}
    assert list(store.tracks.for_ids(["ID003", "ID002", "ID001"])) == ["ID003", "ID001"]
    assert set(saved_first) == {"points", "box", "anchor_time", "updated_at"}
    assert store.tracks.for_ids([]) == {}
    assert store.tracks.for_ids(()) == {}
    assert store.tracks.for_ids(("ID001",)) == {"ID001": saved_first}


def test_for_ids_without_a_tracks_folder_returns_nothing(store, tracks_dir):
    assert not tracks_dir.exists()
    assert store.tracks.for_ids(["ID001", "ID002"]) == {}


def test_for_ids_skips_damaged_files_with_a_warning_that_names_the_path(store, tracks_dir, capsys):
    rows = [add_sighting(store, time_seconds=float(number)) for number in (10, 20, 30)]
    save(store, rows[0])
    (tracks_dir / "ID002.json").write_bytes(b'{"id": "ID002", "points": [[')
    save(store, rows[2])
    (tracks_dir / ".ID001.json-leftover.tmp").write_bytes(b"half written")
    capsys.readouterr()
    found = store.tracks.for_ids(["ID001", "ID002", "ID003"])
    assert list(found) == ["ID001", "ID003"]
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("tracks: skipped ") and str(tracks_dir / "ID002.json") in err
    assert "ID001.json" not in err and "ID003.json" not in err
    assert (tracks_dir / "ID002.json").read_bytes() == b'{"id": "ID002", "points": [['


@pytest.mark.parametrize(
    "ids",
    ["ID001", b"ID001", None, 5, 1.5, True, {"ID001"}, {"ID001": 1}, iter(["ID001"]), (name for name in ["ID001"])],
    ids=["text", "bytes", "null", "int", "float", "bool", "set", "dict", "iterator", "generator"],
)
def test_for_ids_requires_a_list_or_tuple(store, capsys, ids):
    row = add_sighting(store)
    save(store, row)
    capsys.readouterr()
    with pytest.raises(ValueError) as caught:
        store.tracks.for_ids(ids)
    assert str(caught.value).startswith("ids: expected a list of sighting IDs, got ")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "ids, field",
    [
        (["ID001", 7], "ids[1]:"),
        ([None], "ids[0]:"),
        ([["ID001"]], "ids[0]:"),
        ([b"ID001"], "ids[0]:"),
        (["ID001", "../ID001"], "ids[1]:"),
        (("ID001", "ID002", "id003"), "ids[2]:"),
        (["ID001", ""], "ids[1]:"),
        (["ID001\n"], "ids[0]:"),
    ],
)
def test_for_ids_names_the_element_that_is_not_a_sighting_id(store, capsys, ids, field):
    row = add_sighting(store)
    save(store, row)
    capsys.readouterr()
    with pytest.raises(ValueError) as caught:
        store.tracks.for_ids(ids)
    assert str(caught.value).startswith(field)
    assert not isinstance(caught.value, StoreError)
    assert capsys.readouterr().err == ""


# ----- TrackStore.trash -----


def test_trash_moves_the_file_under_a_stamped_name(store, tracks_dir):
    row = add_sighting(store)
    save(store, row)
    before = (tracks_dir / "ID001.json").read_bytes()
    store.tracks.trash("ID001", START + timedelta(seconds=90))
    assert files_in(tracks_dir) == []
    assert files_in(store.trash_dir) == ["20260921T153130Z_ID001.json"]
    assert (store.trash_dir / "20260921T153130Z_ID001.json").read_bytes() == before
    assert store.tracks.load("ID001") is None and not store.tracks.has("ID001")


@pytest.mark.parametrize(
    "moment",
    [None, "2026-09-21T15:30:00Z", 1789000000, 1.5, True, date(2026, 9, 21), object()],
    ids=["null", "text", "int", "float", "bool", "date", "object"],
)
def test_trash_requires_a_datetime_moment(store, tracks_dir, moment):
    row = add_sighting(store)
    save(store, row)
    with pytest.raises(ValueError, match="^moment: expected a datetime, got "):
        store.tracks.trash("ID001", moment)
    assert files_in(tracks_dir) == ["ID001.json"] and files_in(store.trash_dir) == []


def test_trash_checks_the_id_before_the_moment(store):
    with pytest.raises(ValueError, match="^obs_id:"):
        store.tracks.trash("../ID001", None)


def test_trash_with_no_file_does_nothing(store, tracks_dir):
    store.tracks.trash("ID001", START)
    add_sighting(store)
    store.tracks.trash("ID001", START)
    assert files_in(store.trash_dir) == [] and not tracks_dir.exists()


def test_trash_keeps_both_copies_when_the_name_is_taken(store):
    row = add_sighting(store)
    save(store, row, points=[[83.72, 0.1, 0.1, 1.0]])
    store.tracks.trash("ID001", START)
    save(store, row, points=[[83.72, 0.2, 0.2, 1.0]])
    store.tracks.trash("ID001", START)
    assert files_in(store.trash_dir) == ["20260921T153000Z-2_ID001.json", "20260921T153000Z_ID001.json"]
    first = json.loads((store.trash_dir / "20260921T153000Z_ID001.json").read_text(encoding="utf-8"))
    second = json.loads((store.trash_dir / "20260921T153000Z-2_ID001.json").read_text(encoding="utf-8"))
    assert (first["points"], second["points"]) == ([[83.72, 0.1, 0.1, 1.0]], [[83.72, 0.2, 0.2, 1.0]])


def test_trash_into_an_unwritable_trash_folder_raises_store_error_naming_both_paths(store, tracks_dir):
    row = add_sighting(store)
    save(store, row)
    store.trash_dir.chmod(0o555)
    try:
        with pytest.raises(StoreError) as caught:
            store.tracks.trash("ID001", START)
    finally:
        store.trash_dir.chmod(0o755)
    assert str(tracks_dir / "ID001.json") in str(caught.value) and str(store.trash_dir) in str(caught.value)
    assert files_in(tracks_dir) == ["ID001.json"]


# ----- the module -----


def test_track_error_is_not_a_value_error_or_a_store_error():
    assert issubclass(TrackError, Exception)
    assert not issubclass(TrackError, (ValueError, StoreError))


def test_track_store_checks_its_arguments(tmp_path):
    def locked(obs_id, key, anchor_time, point):
        """Stand in for the store's locked check."""

    def mover(path, moment):
        """Stand in for the store's trash mover."""

    for arguments in (
        (None, locked, mover),
        (5, locked, mover),
        (tmp_path, None, mover),
        (tmp_path, "locked", mover),
        (tmp_path, locked, None),
        (tmp_path, locked, "mover"),
    ):
        with pytest.raises(ValueError):
            TrackStore(*arguments)
    assert TrackStore(str(tmp_path / "tracks"), locked, mover).tracks_dir == tmp_path / "tracks"
    assert not (tmp_path / "tracks").exists()


def test_module_exposes_the_planned_interface():
    planned = {"validate_track", "TrackStore", "TrackError", "MAX_TRACK_POINTS", "ID_PREFIX", "ID_PATTERN"}
    planned |= {"TEMP_SUFFIX"}
    assert planned <= set(dir(tracks_module))
    assert {"save", "for_ids", "trash", "has", "load", "path_for"} <= set(dir(TrackStore))
    assert callable(validate_track)
