"""Constants and folder locations shared by every Sponge Screener module.

Every tunable number in the app lives here under a name, so no module carries
an unexplained literal.
"""

import os
from pathlib import Path

VERSION = "1.0.0"

APP_ROOT = Path(__file__).resolve().parent.parent

# The public UVI bucket that holds the TCRMP videos, and the only part of it
# the app may read.
BUCKET_URL = "https://uviai.s3.us-west-2.amazonaws.com"
KEY_PREFIX = "TCRMP_video_ondeck/"
MAX_KEY_LENGTH = 1024

# Relay tuning. One connection from St. Thomas to us-west-2 delivers about
# 11 to 20 Mbps and the 2024 videos run at 52 Mbps, so the relay fetches many
# chunks at once.
CHUNK_SIZE = 4 * 1024 * 1024
RELAY_WORKERS = 16
READ_AHEAD_CHUNKS = 24
CHUNK_RETRIES = 3
CACHE_CAP_BYTES = 60 * 1024 ** 3
CACHE_KEEP_SECONDS = 600
CATALOG_MAX_AGE_SECONDS = 86400

HOST = "127.0.0.1"
PORT = 8765
MAX_BODY_BYTES = 120 * 1024 * 1024

# Extensions Chrome plays directly, and every extension the catalog lists.
PLAYABLE_EXTENSIONS = frozenset({"mp4", "m4v", "mov"})
VIDEO_EXTENSIONS = frozenset({"mp4", "m4v", "mov", "mts", "m2t", "avi", "mxf", "wmv"})

# Sighting limits.
CROP_SIZE = 512
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_NOTE_LENGTH = 500
MAX_TIME_SECONDS = 86400
TALLY_TARGET = 3

DEFAULT_ANNOTATOR = "LO"

# Export packages land here unless data/settings.json names another folder
# (for example the synced OnDeck folder in Google Drive). The environment
# variable SCREENER_EXPORT_ROOT sets the default for a fresh install.
DEFAULT_EXPORT_ROOT = Path(os.environ.get("SCREENER_EXPORT_ROOT", str(APP_ROOT / "exports")))


def data_dir() -> Path:
    """Return the folder that holds sightings, images, and settings.

    Returns:
        The path in the SCREENER_DATA_DIR environment variable when set,
        otherwise ``<app root>/data``.
    """
    return Path(os.environ.get("SCREENER_DATA_DIR", str(APP_ROOT / "data")))


def cache_dir() -> Path:
    """Return the folder that holds video chunks, converted files, and listings.

    Returns:
        The path in the SCREENER_CACHE_DIR environment variable when set,
        otherwise ``<app root>/cache``.
    """
    return Path(os.environ.get("SCREENER_CACHE_DIR", str(APP_ROOT / "cache")))


def config_dir() -> Path:
    """Return the folder that holds species.csv and sites.csv."""
    return APP_ROOT / "config"


def static_dir() -> Path:
    """Return the folder that holds the page, its script, and its stylesheet."""
    return APP_ROOT / "static"
