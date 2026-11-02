"""Where a sighting sits at a moment of its video, for drawing it on the picture."""

from bisect import bisect_right
from typing import Any, Mapping, Optional, Sequence, Tuple

# A sighting with no track at this moment shows at its clicked point this close to its own time.
CLICK_WINDOW_SECONDS = 0.6


def position_at(mark: Mapping[str, Any], time: float) -> Optional[Tuple[float, float]]:
    """Find a sighting's position in the frame at a moment.

    Args:
        mark: The sighting: ``time``, ``point`` ({x, y} fractions of the frame), and
            ``points``, its track as ``[t, x, y, score]`` in time order.
        time: The video time in seconds.

    Returns:
        ``(x, y)`` as fractions of the frame, along the track while the track covers the
        moment, else the clicked point near the sighting's time, else None.
    """
    points: Sequence[Sequence[float]] = mark.get("points") or []
    if points and points[0][0] <= time <= points[-1][0]:
        after = bisect_right([point[0] for point in points], time)
        if after >= len(points):
            return points[-1][1], points[-1][2]
        before = points[after - 1]
        following = points[after]
        share = (time - before[0]) / (following[0] - before[0])
        return before[1] + share * (following[1] - before[1]), before[2] + share * (following[2] - before[2])
    if abs(time - mark["time"]) <= CLICK_WINDOW_SECONDS:
        return mark["point"]["x"], mark["point"]["y"]
    return None
