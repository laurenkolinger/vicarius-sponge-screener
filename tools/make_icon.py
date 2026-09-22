#!/usr/bin/env python3
"""Draw the Sponge Screener app icon as a PNG, with no image library.

The icon is the app's own marker: a magenta ring with a white outline over a
dark rounded square, with the quadrant crosshair behind it.

Usage: python3 make_icon.py OUT.png [SIZE]
"""

import math
import struct
import sys
import zlib

BACKGROUND = (18, 22, 28)
GRID = (255, 255, 255)
GRID_ALPHA = 0.16
MAGENTA = (255, 47, 168)
WHITE = (255, 255, 255)
CORNER_FRACTION = 0.22
RING_RADIUS_FRACTION = 0.24
RING_WIDTH_FRACTION = 0.055
OUTLINE_FRACTION = 0.014
DOT_FRACTION = 0.045
GRID_FRACTION = 0.008


def blend(base, color, alpha):
    """Mix a color over a base color.

    Args:
        base: The (r, g, b) tuple underneath.
        color: The (r, g, b) tuple on top.
        alpha: Coverage of the top color, 0 to 1.

    Returns:
        The mixed (r, g, b) tuple.
    """
    return tuple(int(round(b * (1 - alpha) + c * alpha)) for b, c in zip(base, color))


def coverage(distance, edge, softness):
    """Anti-aliased coverage of a shape whose edge sits at a signed distance.

    Args:
        distance: Signed distance from the edge; negative is inside.
        edge: Where the edge sits along the distance axis.
        softness: Width of the soft transition in pixels.

    Returns:
        1 fully inside, 0 fully outside, a ramp in between.
    """
    return max(0.0, min(1.0, (edge - distance) / softness + 0.5))


def pixel(x, y, size):
    """Compute one icon pixel.

    Args:
        x: Column, 0 to size - 1.
        y: Row, 0 to size - 1.
        size: The icon's width and height in pixels.

    Returns:
        An (r, g, b, a) tuple.
    """
    soft = max(1.0, size / 512)
    half = size / 2.0
    corner = size * CORNER_FRACTION
    dx = max(abs(x + 0.5 - half) - (half - corner), 0.0)
    dy = max(abs(y + 0.5 - half) - (half - corner), 0.0)
    square = coverage(math.hypot(dx, dy), corner, soft)
    if square <= 0:
        return (0, 0, 0, 0)
    color = BACKGROUND
    grid = size * GRID_FRACTION
    on_grid = abs(x + 0.5 - half) < grid or abs(y + 0.5 - half) < grid
    if on_grid:
        color = blend(color, GRID, GRID_ALPHA)
    radius = math.hypot(x + 0.5 - half, y + 0.5 - half)
    ring_radius = size * RING_RADIUS_FRACTION
    ring_width = size * RING_WIDTH_FRACTION
    outline = size * OUTLINE_FRACTION
    outer = coverage(abs(radius - ring_radius), ring_width / 2 + outline, soft)
    inner = coverage(abs(radius - ring_radius), ring_width / 2, soft)
    color = blend(color, WHITE, outer)
    color = blend(color, MAGENTA, inner)
    dot = coverage(radius, size * DOT_FRACTION + outline, soft)
    dot_core = coverage(radius, size * DOT_FRACTION, soft)
    color = blend(color, WHITE, dot)
    color = blend(color, MAGENTA, dot_core)
    return (color[0], color[1], color[2], int(round(255 * square)))


def write_png(path, size):
    """Render the icon and write it as an RGBA PNG.

    Args:
        path: Output file path.
        size: Width and height in pixels.
    """
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            row.extend(pixel(x, y, size))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n")
        handle.write(chunk(b"IHDR", header))
        handle.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        handle.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: make_icon.py OUT.png [SIZE]")
    write_png(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1024)
