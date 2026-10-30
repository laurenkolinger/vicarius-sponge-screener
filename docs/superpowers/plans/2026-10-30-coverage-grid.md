# Coverage grid and watch tracking

Goal: Lauren can see, for the eight target species and every video in a folder,
what is tagged, what January found, and how much of the video she watched, so a
zero count can be read as absent or as not yet looked at.

## Inputs and outputs

- Input from the page: the stretches of video played while the tab is visible.
- Input from disk: `data/reference_observations.csv`, a copy of the January 2026
  `observations.csv`. Without the file, the grid leaves out the January numbers.
- Output: `data/watched.json`, `GET /api/coverage`, `POST /api/videos/watched`, a
  Coverage panel, and a warning before Done.

## Decisions

- The eight targets are ID guide Parts 1 and 2, read from `species.csv`, so moving
  a pin does not change the grid.
- Watched time means forward playback with the tab visible and no Coverage grid or
  shortcuts card open, at any speed on the speed control (up to 3x). Seeks, arrow
  scrubbing, and frame steps do not count. A video left playing unattended counts.
- A gap of 1 second or less counts as watched.
- The longest length reported for a video wins, so a shorter copy cannot hide a gap.
- Watched time starts at zero. Viewing before this build was never recorded.
- Done warns and lets Lauren go ahead. The refusal for unlabeled points stays.
- A cell reads absent only when the count is zero, the video is done, and every
  second was watched. Zero with a January sighting is flagged.
- Watched time stays out of the OnDeck export.
- A watched-time save that fails keeps the seconds for a retry when the same video
  is open. Any seconds lost show as unwatched, never as watched.
- A corrupt `watched.json` raises an error and is never overwritten.

## Files

- Create `screener/coverage.py`, `static/coverage.js`, `tests/test_coverage.py`,
  `tests/test_coverage.mjs`.
- Change `screener/server.py`, `static/app.js`, `static/index.html`,
  `static/style.css`, `tests/test_server.py`, `tests/test_e2e.py`,
  `tests/e2e/cdp_driver.mjs`, `README.md`.

## Tests

- Unit: merging, gaps, percent, validation, the store, reference counts, the
  tracker, and the cell states.
- Adversarial: NaN and infinite times, reversed ranges, booleans, too many ranges,
  a corrupt file, a reference file with missing columns, stale time jumps.
- Integration: the routes on the fake bucket; a real browser plays a clip, the grid
  shows the percent, and Done warns on a skipped stretch.
- Regression: the full Python, node, and end-to-end suites.
- Pixel: the grid at 1280 and 946 pixels wide.

## Review

One independent reviewer reads the diff against this plan, then one re-review if
the first finds defects.
