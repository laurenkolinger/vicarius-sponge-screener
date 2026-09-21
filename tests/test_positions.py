"""Tests for screener.positions: point and box validation, quadrants, and clock text."""

import json
import math
from pathlib import Path

import pytest

from screener import positions
from screener.positions import (
    QUADRANT_PHRASES,
    QUADRANTS,
    anchor_point,
    format_clock,
    quadrant_of,
    validate_box,
    validate_point,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "geometry_cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
HUGE_INT = 10 ** 400


def test_quadrant_cases_from_shared_fixture():
    assert len(CASES["quadrant"]) >= 10
    for case in CASES["quadrant"]:
        assert quadrant_of(case["x"], case["y"]) == case["expect"], case


def test_clock_cases_from_shared_fixture():
    assert len(CASES["clock"]) >= 9
    for case in CASES["clock"]:
        assert format_clock(case["seconds"]) == case["expect"], case


def test_quadrant_names_and_phrases_cover_each_other():
    assert QUADRANTS == ("TOPLEFT", "TOPRIGHT", "BOTTOMLEFT", "BOTTOMRIGHT")
    assert QUADRANT_PHRASES == {
        "TOPLEFT": "top left",
        "TOPRIGHT": "top right",
        "BOTTOMLEFT": "bottom left",
        "BOTTOMRIGHT": "bottom right",
    }
    assert set(QUADRANT_PHRASES) == set(QUADRANTS)


def test_every_quadrant_result_is_a_known_quadrant():
    steps = [index / 20 for index in range(21)]
    seen = {quadrant_of(x, y) for x in steps for y in steps}
    assert seen == set(QUADRANTS)


def test_validate_point_accepts_corners_integers_and_extra_fields():
    assert validate_point({"x": 0, "y": 1}) == (0.0, 1.0)
    assert validate_point({"x": 0.25, "y": 0.75, "z": "ignored"}) == (0.25, 0.75)
    checked = validate_point({"x": 1, "y": 0})
    assert all(isinstance(value, float) for value in checked)


def test_validate_point_turns_negative_zero_into_zero():
    x, y = validate_point({"x": -0.0, "y": -0.0})
    assert math.copysign(1.0, x) == 1.0
    assert math.copysign(1.0, y) == 1.0


@pytest.mark.parametrize(
    "bad, field",
    [
        ({"x": float("nan"), "y": 0.5}, "point.x"),
        ({"x": 0.5, "y": float("nan")}, "point.y"),
        ({"x": float("inf"), "y": 0.5}, "point.x"),
        ({"x": 0.5, "y": float("-inf")}, "point.y"),
        ({"x": "0.5", "y": 0.5}, "point.x"),
        ({"x": 0.5, "y": "0.5"}, "point.y"),
        ({"x": True, "y": 0.5}, "point.x"),
        ({"x": 0.5, "y": False}, "point.y"),
        ({"x": None, "y": 0.5}, "point.x"),
        ({"x": [0.5], "y": 0.5}, "point.x"),
        ({"y": 0.5}, "point.x"),
        ({"x": 0.5}, "point.y"),
        ({}, "point.x"),
        ({"x": -0.0001, "y": 0.5}, "point.x"),
        ({"x": 1.0001, "y": 0.5}, "point.x"),
        ({"x": 0.5, "y": -3}, "point.y"),
        ({"x": 0.5, "y": 2}, "point.y"),
        ({"x": HUGE_INT, "y": 0.5}, "point.x"),
        ({"x": 0.5, "y": -HUGE_INT}, "point.y"),
        (None, "point"),
        ([0.5, 0.5], "point"),
        ((0.5, 0.5), "point"),
        ("0.5,0.5", "point"),
        (0.5, "point"),
        (True, "point"),
    ],
)
def test_validate_point_rejects_nan_inf_strings_bools_missing_fields_out_of_range(bad, field):
    with pytest.raises(ValueError) as caught:
        validate_point(bad)
    assert str(caught.value).startswith(field + ":")


def test_validate_box_accepts_none_and_edge_touching_box():
    assert validate_box(None) is None
    assert validate_box({"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}) == (0.5, 0.5, 0.5, 0.5)
    assert validate_box({"x": 0, "y": 0, "w": 1, "h": 1}) == (0.0, 0.0, 1.0, 1.0)
    assert validate_box({"x": 0.7, "y": 0.1, "w": 0.3, "h": 0.2}) == (0.7, 0.1, 0.3, 0.2)


def test_validate_box_allows_float_noise_up_to_the_tolerance_and_no_further():
    assert validate_box({"x": 0.9, "y": 0.9, "w": 0.10005, "h": 0.10005}) == (0.9, 0.9, 0.10005, 0.10005)
    with pytest.raises(ValueError, match=r"^box\.w:"):
        validate_box({"x": 0.9, "y": 0.1, "w": 0.1002, "h": 0.1})
    with pytest.raises(ValueError, match=r"^box\.h:"):
        validate_box({"x": 0.1, "y": 0.9, "w": 0.1, "h": 0.1002})


@pytest.mark.parametrize(
    "bad, field",
    [
        ({"x": 0.1, "y": 0.1, "w": 0, "h": 0.2}, "box.w"),
        ({"x": 0.1, "y": 0.1, "w": 0.2, "h": 0}, "box.h"),
        ({"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.0}, "box.w"),
        ({"x": 0.1, "y": 0.1, "w": -0.2, "h": 0.2}, "box.w"),
        ({"x": 0.1, "y": 0.1, "w": 0.2, "h": -0.2}, "box.h"),
        ({"x": 0.9, "y": 0.1, "w": 0.2, "h": 0.2}, "box.w"),
        ({"x": 0.1, "y": 0.9, "w": 0.2, "h": 0.2}, "box.h"),
        ({"x": -0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "box.x"),
        ({"x": 0.1, "y": 1.5, "w": 0.2, "h": 0.2}, "box.y"),
        ({"x": 0.1, "y": 0.1, "w": 1.5, "h": 0.2}, "box.w"),
        ({"x": 0.1, "y": 0.1, "w": 0.2, "h": HUGE_INT}, "box.h"),
        ({"x": 0.1, "y": 0.1, "w": float("nan"), "h": 0.2}, "box.w"),
        ({"x": 0.1, "y": 0.1, "w": 0.2, "h": float("inf")}, "box.h"),
        ({"x": "0.1", "y": 0.1, "w": 0.2, "h": 0.2}, "box.x"),
        ({"x": 0.1, "y": True, "w": 0.2, "h": 0.2}, "box.y"),
        ({"x": 0.1, "y": 0.1, "w": 0.2}, "box.h"),
        ({"x": 0.1, "y": 0.1, "h": 0.2}, "box.w"),
        ({}, "box.x"),
        ([0.1, 0.1, 0.2, 0.2], "box"),
        ("box", "box"),
        (False, "box"),
        (0, "box"),
    ],
)
def test_validate_box_rejects_zero_negative_overflow(bad, field):
    with pytest.raises(ValueError) as caught:
        validate_box(bad)
    assert str(caught.value).startswith(field + ":")


def test_anchor_point_uses_box_center():
    assert anchor_point((0.1, 0.1), (0.5, 0.5, 0.4, 0.2)) == pytest.approx((0.7, 0.6))
    assert anchor_point((0.9, 0.9), (0.0, 0.0, 0.2, 0.2)) == pytest.approx((0.1, 0.1))


def test_anchor_point_without_box_is_the_point():
    assert anchor_point((0.25, 0.75), None) == (0.25, 0.75)


def test_anchor_point_stays_inside_the_frame_for_a_box_at_the_tolerance_edge():
    box = validate_box({"x": 1.0, "y": 1.0, "w": 0.0001, "h": 0.0001})
    x, y = anchor_point((0.5, 0.5), box)
    assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
    assert quadrant_of(x, y) == "BOTTOMRIGHT"


@pytest.mark.parametrize(
    "point, box, field",
    [
        ((0.5,), None, "point"),
        ((0.5, 0.5, 0.5), None, "point"),
        ("ab", None, "point"),
        (None, None, "point"),
        ((0.5, "0.5"), None, "point"),
        ((0.5, 1.5), None, "point"),
        ((0.5, 0.5), (0.1, 0.1, 0.2), "box"),
        ((0.5, 0.5), {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "box"),
        ((0.5, 0.5), (0.1, 0.1, float("nan"), 0.2), "box"),
    ],
)
def test_anchor_point_rejects_malformed_arguments(point, box, field):
    with pytest.raises(ValueError, match="^" + field):
        anchor_point(point, box)


@pytest.mark.parametrize(
    "x, y, field",
    [
        (float("nan"), 0.5, "x"),
        (0.5, float("nan"), "y"),
        (float("inf"), 0.5, "x"),
        ("0.5", 0.5, "x"),
        (0.5, None, "y"),
        (True, 0.5, "x"),
        (1.5, 0.5, "x"),
        (0.5, -0.5, "y"),
    ],
)
def test_quadrant_of_rejects_values_that_are_not_fractions(x, y, field):
    with pytest.raises(ValueError, match="^" + field + ":"):
        quadrant_of(x, y)


def test_format_clock_rejects_negative_and_nan():
    for bad in (-1, -0.001, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="^seconds:"):
            format_clock(bad)


@pytest.mark.parametrize("bad", ["12", None, True, [12], b"12"])
def test_format_clock_rejects_values_that_are_not_numbers(bad):
    with pytest.raises(ValueError, match="^seconds:"):
        format_clock(bad)


def test_format_clock_handles_whole_numbers_negative_zero_and_long_videos():
    assert format_clock(7) == "00:07"
    assert format_clock(-0.0) == "00:00"
    assert format_clock(86400) == "1440:00"
    assert format_clock(359999.99) == "5999:59"


def test_module_exposes_the_planned_interface():
    planned = {"QUADRANTS", "QUADRANT_PHRASES", "validate_point", "validate_box", "quadrant_of", "anchor_point", "format_clock"}
    assert planned <= set(dir(positions))
