# Sponge Screener

A local tool for building sponge ground truth from TCRMP transect video. It
streams videos from the public `uviai` S3 bucket, and each sighting is one
click on the sponge, one key for the species, and Enter. Every sighting saves a
row in `observations.csv`, the full frame as JPEG, and a crop of the sponge as
PNG, in the layout of the January 2026 package sent to OnDeck AI.

## Requirements

- macOS with Python 3.9 or newer (the system Python works; no packages to install)
- Google Chrome
- ffmpeg and ffprobe (`brew install ffmpeg`), used only for archive formats
  Chrome cannot play (AVCHD `.mts`, HDV `.m2t`, DV `.avi`, `.mxf`, `.wmv`)

## Run

```bash
python3 screener.py
```

The server binds to `127.0.0.1:8770` and opens Chrome. Options: `--port`,
`--no-browser`, `--data-dir`, `--cache-dir`, `--bucket-url`.

## Screening

1. Pick a folder and a video in the left panel. Videos marked "converts first"
   are archive formats; the app converts them in the background and plays the
   copy when it is ready.
2. Watch. When a sponge sits near the center of the frame, click it. The video
   pauses and the quadrant lights up. Drag instead of clicking to draw a box.
3. Press the species key (the ten pinned species sit on `1` to `9` and `0`;
   type letters to search the full list of 37 species plus Unknown).
4. Press Enter. The sighting saves and the video plays on.

| Key | Screening mode | Marking mode |
|---|---|---|
| Space | play or pause | |
| Left, Right | jump 2 s | |
| `,` `.` | step one frame | step one frame |
| `[` `]` | slower, faster (0.5x to 3x) | |
| G | quadrant grid and center line | |
| Z | remove the last sighting | |
| D | mark the video fully screened | |
| N | next video | |
| `1` to `9`, `0` | | pinned species |
| letters | | search the species list |
| Tab | | note field |
| Enter | | save and resume |
| Shift+Enter | | save and stay paused |
| Escape | | cancel the mark |

The timestamp convention matches the January package: the moment the sponge
sits closest to the center of the frame.

## Where things go

- `data/observations.csv`: one row per sighting. The first nine columns are
  the January columns in the same order (Site, Transect, Sponge Type,
  Timestamp, Notes, ID, FileName, FrameFileName, AbbreviatedNote), followed by
  SpeciesCode, TimestampSeconds, Quadrant, PointX, PointY, BoxX, BoxY, BoxW,
  BoxH, CropFileName, S3Key, Annotator, LoggedAt. Points and boxes are
  fractions of frame width and height.
- `data/frames/` and `data/crops/`: `ID###_CODE_QUADRANT_SITE_TRANSECT.jpg`
  and the same stem as `.png`.
- `data/videos_screened.csv`: which videos were opened, which are marked done,
  and the pinned species at the time. A finished video with no rows for a
  pinned species counts as a true absence.
- `data/settings.json`: annotator initials, pinned species, export folder.
- `cache/`: video chunks (capped at 60 GB, oldest removed first), converted
  files, and the bucket listing.
- Export writes `spongeGroundTruth_<date>/` into the export folder with both
  CSV files, every referenced image, and a README with counts and column
  definitions. The export folder defaults to `exports/` here; set
  `export_root` in `data/settings.json` to write straight into a synced Drive
  folder.

## Configuration

- `config/species.csv`: code, scientific name, ID guide part, default pin.
  Codes are four upper-case letters.
- `config/sites.csv`: site code and site name. Blank names fall back to the
  code in the Site column.

## Tests

```bash
python3 -m pytest tests -q -m "not live and not e2e"
node --test tests/test_geometry.mjs
python3 -m pytest tests -q -m live      # read-only checks against the real bucket
```

## Design

The approved design is in `docs/superpowers/specs/` and the build plan in
`docs/superpowers/plans/`.
