# Sponge Tracking: Spec and Plan

Date: 2026-10-13. Requested by Lauren Olinger, who chose "both ways, saved": each saved point
follows its sponge backward to where it entered the frame and forward until it leaves, and the
track is saved with the sighting so it shows next time.

## Design

Tracking rides along with normal viewing, so it needs no second decoder and no extra download.

- **Frame ring.** While the open video plays, the page copies presented frames
  (`requestVideoFrameCallback`) into a ring of grayscale frames 320 px wide, at most 12 per video
  second, holding the last 12 video seconds. A seek, a new video, or a gap of more than 0.5 s
  between kept frames clears the ring (the frames are no longer contiguous).
- **Backward.** When a sighting saves (a new ID), or contiguous playback reaches a row's time
  while its backward part is missing, the page traces the point backward through the ring from
  the frame nearest the row's time.
- **Forward.** From the row's time on, every new kept frame extends each live forward track,
  until the point leaves the picture, tracking is lost, or 15 video seconds pass. Paused
  playback simply waits; a seek ends live tracking and saves what exists.
- **Matcher.** Zero-mean normalized cross-correlation of a square template (21 px at 320 px
  width; a box uses its own size, clamped to 13 to 41 px) around the predicted position
  (constant velocity), search radius 12 px. The template blends 10 percent toward each match
  scoring at least 0.8. Tracking is lost after 3 kept frames in a row score under 0.5. A point
  within 2 percent of the picture edge has left the frame.
- **Saved.** The page posts the merged track (both directions, sorted by time) to the server
  when a direction ends and on seek, pause, video change, and page hide. Partial tracks extend
  on a later contiguous pass.
- **Drawing.** A row whose track covers the current time is drawn at the interpolated track
  position, redrawn on every presented frame while playing. Rows without coverage keep today's
  rule (fixed at the click, within 0.6 s). Clicking a saved mark on the paused picture hit-tests
  the drawn positions.
- **Export.** The package gains `tracks.csv` for labeled rows with tracks.

## HTTP contract

| Request | Answer |
|---|---|
| `POST /api/tracks/<ID>` with header `X-Screener: 1` and body `{points: [[t, x, y, score], ...], box: {w, h} or null}` | 200 `{track}`. Replaces the stored track. 400 when: the body is not an object; `points` is not a list of 1 to 3000 four-number lists; any value is not finite; t is outside 0 to 86400 or not strictly increasing; x, y, or score is outside 0 to 1; `box` is not null or `{w, h}` with each in (0, 1]. 404 for an unknown ID. 403 without the header. |
| `GET /api/tracks?key=<key>` | `{tracks: {<ID>: {points, box, anchor_time, updated_at}}}` for the rows of that video that have a track. |
| `DELETE /api/observations/<ID>` | also moves `data/tracks/<ID>.json` to the trash. |
| `POST /api/export` | the package gains `tracks.csv` (columns `ID, SpeciesCode, TimeSeconds, X, Y, BoxX, BoxY, BoxW, BoxH, Score`; box columns blank for point sightings, else the box centered on X, Y), and the README describes it. |

Storage: `data/tracks/<ID>.json` = `{id, key, anchor_time, points, box, updated_at}`, written
atomically (temp file plus `os.replace`) under the store's existing lock. `anchor_time` is the
row's TimestampSeconds. A relabel keeps the file (the ID does not change).

## Global constraints

- Same as `docs/superpowers/plans/2026-10-13-species-wheel.md`: Python standard library only,
  Python 3.9 compatible, docstrings and JSDoc on every function, inputs validated at the entry
  point, errors that start with the field name, named constants, no dead code, American
  spelling, no em dashes, `title` tooltips in the "You ... The server ..." voice.
- Never stop or restart the server on port 8770, and never write to `data/` of the live checkout;
  tests use temporary folders. Port 8791 belongs to another process.
- No git commits.
- Suites: `python3 -m pytest tests -q -m "not live and not e2e"` (1005 before),
  `node --test tests/test_wheel.mjs tests/test_geometry.mjs` (140 before), and
  `python3 -m pytest tests -q -m e2e` (21 before).

## Task S: server, storage, export (agent S)

Files: `screener/tracks.py` (new), `screener/store.py`, `screener/server.py`, `screener/export.py`,
`tests/test_tracks.py` (new), `tests/test_store.py`, `tests/test_server.py`, `tests/test_export.py`.

- `screener/tracks.py`: `validate_track(track) -> dict`, `TrackStore(tracks_dir, locked_sighting, move_to_trash)` with
  `save(obs_id, key, anchor_time, point, track) -> dict` (refuses a row whose video, time, or point changed), `for_ids(ids) -> dict`, `has(obs_id)`, `trash(obs_id, moment)`;
  reuses the store's lock and trash conventions (read store.py first).
- Routes: `(OBSERVATION... )` style prefix route `/api/tracks/` for POST, exact `/api/tracks` for GET.
- Tests first: validation (every 400 case in the contract, hostile values such as NaN, strings,
  nested lists, 3001 points, unsorted and duplicate times, `../` in the ID), save and replace,
  unknown ID 404, mutation header 403, GET for a key with and without tracks, delete moves the
  track to the trash, relabel keeps it, export writes `tracks.csv` with the right rows and
  columns and leaves out Later points, a corrupted track file is skipped by GET with a logged
  warning and fails the export loudly with the path.

## Task T: tracker and page (agent T)

Files: `static/tracker.js` (new, pure), `static/app.js`, `static/geometry.js` if needed,
`tests/test_tracker.mjs` (new), `tests/e2e/cdp_driver.mjs`, `tests/test_e2e.py`, `README.md`.

- `static/tracker.js` (pure, node-tested, no DOM): `grayFromRgba(rgba, w, h)`,
  `templateAt(gray, w, h, cx, cy, size)`, `matchTemplate(gray, w, h, template, size, px, py,
  radius)` returning `{x, y, score}` (ZNCC), `followTrack(frames, anchorIndex, start, direction,
  options)` returning `{points, end}` with `end` in `exit | lost | limit | frames`, `mergeTracks(a,
  b)`, `positionAt(track, t)` (linear interpolation; null outside the track or across a gap over
  0.5 s). Constants as in the Design section.
- Tests first: a textured synthetic frame shifted by known amounts (sub-template, diagonal,
  steady velocity across 40 frames) is followed within 1 px at 320 px width; a flat frame ends
  `lost`; a point crossing the edge ends `exit`; 15 s ends `limit`; backward on reversed frames
  mirrors forward; `positionAt` interpolation, ends, and gaps; bad inputs throw TypeError naming
  the argument.
- Page wiring as in the Design section: frame ring, backward on save and on contiguous arrival,
  live forward, posting, drawing on every presented frame while playing, hit-testing drawn
  positions, loading `GET /api/tracks?key=` on video open. CPU budget: tracking plus capture
  under 4 ms per kept frame with five live tracks (measure and report).
- End to end: generate a pan clip with ffmpeg (a 3840 x 1080 random texture, blurred, cropped to
  1920 x 1080 with `x='t*300'`, 12 s at 30 fps). Play from 0, pick a point at about 5 s, play on.
  Assert the saved track covers time before and after the pick, its x positions follow the known
  motion (300 px per second leftward in frame terms, 0.156 of the width per second) within 2
  percent of the width, the mark drawn at the pick time + 2 s sits on the tracked position, and
  the track survives a page reload (GET returns it).
- Real footage check (report numbers, not a pass/fail gate): on a cached real TCRMP video served
  by a throwaway server on a free port with a scratch data folder and a copy of the cache, track
  five points forward 3 s and back again; report the forward-backward error in pixels at full
  width and how many points ended `lost`.
- README: what tracking does, its limits (needs contiguous playback; existing sightings fill in
  on a later pass; drift over bare sand or in murky water).

## Review

One independent reviewer reads both tasks against this document, runs the suites, and looks at
a screenshot sequence of a tracked mark. Two passes at most.

## Revision: OpenCV on the server (replaces the page tracker above)

Lauren asked to use an existing tool rather than a hand-written matcher, and approved
installing OpenCV (opencv-contrib-python-headless 5.0.0.93 and numpy 2.5.4) into
`.venv-track/` inside the project. The page no longer tracks; the server does, in the
background, for every sighting, old and new.

- `screener/track_worker.py` runs under `.venv-track/bin/python`. It reads one JSON request on
  stdin (`source`, `time`, `point`, `box`, `back_seconds` 12, `forward_seconds` 15, `fps` 15,
  `scale_width` 640), decodes that stretch with the ffmpeg command line into raw frames, runs
  OpenCV's CSRT tracker forward from the sighting's frame and again backward over the reversed
  frames, and prints `{points, box, ends}`. A point sighting tracks a square box 12 percent of
  the frame height around the point. A direction ends when the box center leaves the picture
  (2 percent margin), when the tracker fails 3 frames in a row, or at the time limit.
- `screener/tracking.py` (standard library) holds one background job at a time: it runs the
  worker with a timeout, checks the answer with `tracks.validate_track`, and saves it with
  `TrackStore.save`. Without `.venv-track` it reports tracking as unavailable and the app works
  as before.
- A new sighting queues its job; `GET /api/tracks?key=` queues the rows of that video without a
  track and answers `{tracks, pending}`; a deleted sighting leaves the queue.
- The page loads the tracks of the open video, polls while `pending` is not empty, and draws
  each mark at its interpolated track position on every presented frame. Clicking a drawn mark
  opens its edit wheel, while paused or playing (Lauren asked for points editable on click).
- `POST /api/tracks/<ID>` is dropped: no client posts tracks.
