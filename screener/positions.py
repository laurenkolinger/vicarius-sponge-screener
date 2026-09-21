"""Points, boxes, quadrants, and clock text for a sighting.

Points and boxes are fractions of the frame: x runs from 0 at the left edge to
1 at the right edge, and y runs from 0 at the top edge to 1 at the bottom edge.
The page computes the same quadrants and clock text in static/geometry.js, and
both sides are tested against tests/fixtures/geometry_cases.json.
"""

import math
from typing import Any, Mapping, Optional, Tuple

QUADRANTS = ("TOPLEFT", "TOPRIGHT", "BOTTOMLEFT", "BOTTOMRIGHT")
QUADRANT_PHRASES = {
    "TOPLEFT": "top left",
    "TOPRIGHT": "top right",
    "BOTTOMLEFT": "bottom left",
    "BOTTOMRIGHT": "bottom right",
}

FRAME_START = 0.0
FRAME_END = 1.0
FRAME_MIDDLE = 0.5
# The page clamps a box to the frame, so only floating point noise can carry a
# box edge past 1. This much overshoot is accepted.
BOX_EDGE_TOLERANCE = 0.0001
SECONDS_PER_MINUTE = 60
POINT_FIELDS = ("x", "y")
BOX_FIELDS = ("x", "y", "w", "h")


def _require_number(value: Any, field: str) -> Any:
    """Check that a value is a finite real number.

    Args:
        value: The candidate, of any type.
        field: The field name for the error message.

    Returns:
        The value unchanged (an int or a float).

    Raises:
        ValueError: When the value is a bool, is not an int or a float, or is
            NaN or infinite.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field}: expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field}: expected a finite number, got {value}")
    return value


def _require_fraction(value: Any, field: str) -> float:
    """Check that a value is a number from 0 to 1.

    Args:
        value: The candidate, of any type.
        field: The field name for the error message.

    Returns:
        The value as a float. Negative zero comes back as zero.

    Raises:
        ValueError: When the value is not a finite number or lies outside 0 to 1.
    """
    number = _require_number(value, field)
    if not FRAME_START <= number <= FRAME_END:
        raise ValueError(f"{field}: expected a fraction of the frame from 0 to 1, got {number}")
    return float(number) + 0.0


def _require_fields(value: Any, fields: Tuple[str, ...], label: str) -> Tuple[float, ...]:
    """Read named fractions out of a JSON object.

    Args:
        value: The candidate object, of any type.
        fields: The field names to read, in order.
        label: "point" or "box", used in the error message.

    Returns:
        The fractions in the order of ``fields``. Other fields are ignored.

    Raises:
        ValueError: When the value is not an object, a field is missing, or a
            field is not a fraction from 0 to 1.
    """
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}: expected an object with {', '.join(fields)}, got {type(value).__name__}")
    fractions = []
    for field in fields:
        if field not in value:
            raise ValueError(f"{label}.{field}: missing")
        fractions.append(_require_fraction(value[field], f"{label}.{field}"))
    return tuple(fractions)


def validate_point(point: Any) -> Tuple[float, float]:
    """Check the clicked point of a sighting.

    Args:
        point: ``{"x": number, "y": number}``, both fractions of the frame.

    Returns:
        ``(x, y)`` as floats.

    Raises:
        ValueError: Starting with ``point``, ``point.x``, or ``point.y`` and
            the reason: not an object, a missing field, a value that is not a
            finite number (text, bool, NaN, infinity), or a value outside 0 to 1.
    """
    x, y = _require_fields(point, POINT_FIELDS, "point")
    return (x, y)


def validate_box(box: Any) -> Optional[Tuple[float, float, float, float]]:
    """Check the dragged box of a sighting.

    Args:
        box: None for a sighting without a box, or
            ``{"x": number, "y": number, "w": number, "h": number}`` with the
            top left corner, the width, and the height as fractions of the frame.

    Returns:
        None, or ``(x, y, w, h)`` as floats.

    Raises:
        ValueError: Starting with ``box`` or ``box.<field>`` and the reason:
            not an object, a missing field, a value that is not a fraction
            from 0 to 1, a width or height of zero, or a box that passes the
            right or bottom edge by more than BOX_EDGE_TOLERANCE.
    """
    if box is None:
        return None
    x, y, w, h = _require_fields(box, BOX_FIELDS, "box")
    for field, size in (("w", w), ("h", h)):
        if size <= FRAME_START:
            raise ValueError(f"box.{field}: expected a size above 0, got {size}")
    limit = FRAME_END + BOX_EDGE_TOLERANCE
    if x + w > limit:
        raise ValueError(f"box.w: x + w is {x + w}, which passes the right edge of the frame at 1")
    if y + h > limit:
        raise ValueError(f"box.h: y + h is {y + h}, which passes the bottom edge of the frame at 1")
    return (x, y, w, h)


def quadrant_of(x: float, y: float) -> str:
    """Name the quadrant of the frame that holds a point.

    Args:
        x: Fraction of the frame width, 0 at the left edge.
        y: Fraction of the frame height, 0 at the top edge.

    Returns:
        One of QUADRANTS. ``x < 0.5`` is LEFT and ``y < 0.5`` is TOP, so the
        center lines belong to the right and bottom halves.

    Raises:
        ValueError: When x or y is not a finite number from 0 to 1.
    """
    checked_x = _require_fraction(x, "x")
    checked_y = _require_fraction(y, "y")
    vertical = "TOP" if checked_y < FRAME_MIDDLE else "BOTTOM"
    horizontal = "LEFT" if checked_x < FRAME_MIDDLE else "RIGHT"
    return vertical + horizontal


def _require_tuple(value: Any, length: int, label: str) -> Tuple[float, ...]:
    """Check a tuple of fractions that came from validate_point or validate_box.

    Args:
        value: The candidate tuple or list.
        length: The number of fractions expected.
        label: "point" or "box", used in the error message.

    Returns:
        The fractions as floats.

    Raises:
        ValueError: When the value is not a tuple or list of that length, or
            an item is not a fraction from 0 to 1.
    """
    if not isinstance(value, (tuple, list)) or len(value) != length:
        raise ValueError(f"{label}: expected {length} numbers from validate_{label}, got {value!r}")
    return tuple(_require_fraction(item, f"{label}[{index}]") for index, item in enumerate(value))


def anchor_point(
    point: Tuple[float, float], box: Optional[Tuple[float, float, float, float]]
) -> Tuple[float, float]:
    """Return the spot that stands for the sponge when naming its quadrant.

    Args:
        point: ``(x, y)`` from validate_point.
        box: None, or ``(x, y, w, h)`` from validate_box.

    Returns:
        The center of the box when a box exists, otherwise the point. The
        center is held inside the frame, because validate_box lets a box edge
        pass 1 by BOX_EDGE_TOLERANCE.

    Raises:
        ValueError: When point is not two fractions or box is not None or
            four fractions.
    """
    x, y = _require_tuple(point, len(POINT_FIELDS), "point")
    if box is None:
        return (x, y)
    left, top, width, height = _require_tuple(box, len(BOX_FIELDS), "box")
    center_x = min(FRAME_END, left + width / 2)
    center_y = min(FRAME_END, top + height / 2)
    return (center_x, center_y)


def format_clock(seconds: float) -> str:
    """Write a video time as the January ``MM:SS`` text.

    Args:
        seconds: Time from the start of the video, zero or more.

    Returns:
        Minutes and seconds with at least two digits each, rounded down to
        the whole second. Minutes keep counting past 99 ("100:03").

    Raises:
        ValueError: When seconds is not a finite number or is below zero.
    """
    number = _require_number(seconds, "seconds")
    if number < 0:
        raise ValueError(f"seconds: expected zero or more, got {number}")
    minutes, rest = divmod(math.floor(number), SECONDS_PER_MINUTE)
    return f"{minutes:02d}:{rest:02d}"
