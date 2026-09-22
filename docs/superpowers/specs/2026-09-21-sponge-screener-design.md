# Sponge Screener: design

Date: 2026-09-21. Requested by Lauren Olinger. Status: awaiting approval.

## Purpose

Lauren screens TCRMP transect videos for sponge species and sends OnDeck AI a ground-truth
table. In January 2026 she logged 246 sightings of 5 species by watching each video and
speaking position notes, then a script added IDs and pulled frames. The June 17, 2026 meeting
raised the bar to the 10 most important species, each with at least 2 to 3 sightings from
earlier years. Sponge Screener makes each sighting a click, a key press, and Enter, and it
saves a full frame and a sponge crop with every sighting for other training work.

## Measured constraints

- Every object under `s3://uviai/TCRMP_video_ondeck/` is publicly listable and readable.
- A 2024 video is H.264, 1920 x 1080, 59.94 fps, about 4 minutes, 52 Mbps, 1.0 to 1.6 GB.
- One connection from Lauren's Mac to S3 delivered 11 Mbps. Eight parallel range requests
  delivered 162 Mbps on a 713 Mbps line. A browser video element uses one connection, so
  the app relays the video through parallel range requests and playback keeps up.
- The archive holds 2,887 mp4, 1,134 mts, 196 m2t, 75 avi, 27 mov, 6 m4v, 4 mxf, and 1 wmv.
  Chrome plays mp4, m4v, and most mov. The app converts the rest with ffmpeg before playback.
- Older file names cover several transects (`T1-6`, `T1+T3-6`, `T5.2-6`) or follow no
  pattern (`MVI_0203.MOV`), and some sit in nested folders. The S3 key identifies a video.
- The Mac has Python 3.9, pytest, node 20, ffmpeg, Chrome, and 331 GB free.

## Architecture

A local app. `python3 screener.py` starts a server on `127.0.0.1:8770` and opens Chrome.
Python standard library only. ffmpeg runs only for formats Chrome cannot play.

| Unit | One job | Depends on |
|---|---|---|
| `screener/s3catalog.py` | List bucket folders and videos through the public S3 listing API, with pagination and a disk copy for fast starts | urllib |
| `screener/relay.py` | Serve any byte range of a video from an on-disk chunk cache, fetching missing 8 MB chunks over 12 parallel connections with read-ahead and retry; cap the cache at 60 GB, oldest first | urllib, threading |
| `screener/convert.py` | For formats Chrome cannot play: pull the whole file through the relay, then make an MP4 at the original resolution (stream copy for H.264 sources, hardware H.264 encode for MPEG-2, DV, WMV); report job progress | relay, ffmpeg |
| `screener/names.py` | Parse `TCRMP<date>_video_<site>_<transect>` into date, site code, site name, transect label; return blanks for names outside the pattern | sites.csv |
| `screener/positions.py` | Turn a normalized point into a quadrant, and a point or box into a crop rectangle clamped to the frame | none |
| `screener/store.py` | Append and delete sightings with atomic CSV writes under a file lock, assign IDs, write the frame and crop files, keep the screened-video log, build the export package | names, positions |
| `screener/server.py` | HTTP routes, input validation, error messages that name the field and the reason | all above |
| `static/geometry.js` | Pure functions: click to normalized point, quadrant, crop rectangle | none |
| `static/app.js`, `index.html`, `style.css` | Player, keyboard modes, panels | geometry.js |

Data flow for one sighting: the browser plays `/video?key=...` from the relay. A click pauses
the video. A species key and Enter follow. The browser draws the paused frame to a canvas
(allowed because the relay is same-origin), makes the full-frame JPEG and the crop PNG at the
video's native resolution, and posts them with the key, time, point, box, species, and note.
The server validates, writes both images, appends the CSV row, and answers with the row. The
page adds the row to the list and resumes playback.

## Interaction

Screening mode (no mark pending):

- Click the video: pause, drop a marker, highlight the quadrant. Drag: draw a box.
- Space play or pause. Left and Right jump 2 s. Comma and Period step one frame.
- `[` and `]` change speed from 0.5x to 3x. G toggles the quadrant grid and center line.
- Z removes the last sighting and its images. D marks the video fully screened.

Marking mode (after a click or drag):

- 1 to 9 and 0 pick the ten pinned species. Typing letters filters the full list by name or
  code. Tab moves to an optional note.
- Enter saves and resumes playback. Shift+Enter saves and stays paused for another sponge in
  the same frame. Escape cancels the mark and resumes.

Panels:

- Left: bucket browser by folder with search, a status badge per video (new, in progress with
  a count, done), a format badge (plays now, needs conversion), and a background queue.
- Right: sightings in this video (click to jump, delete), and a tally per species across all
  videos (sightings, videos, earliest year) against the target of 3.

Timestamp convention stays the January one: the moment the sponge sits closest to frame
center. The center line helps, and the frame-step keys adjust the paused frame before Enter.

Species list: the 37 species in the ID guide plus Unknown sponge, with four-letter codes in the
January style (ACAU, AFUL, CDEL, MLAE, CPLI). Pinned by default: those five, plus
*Aiolochroia crassa* (ACRA), *Amphimedon compressa* (ACOM), and *Xestospongia muta* (XMUT).
Keys 9 and 0 stay open until Lauren pins her last two from Part 3. Pins change in the app.

## Outputs

Working data autosaves to `~/SpongeScreener/data/` after every sighting. The Export button
writes a dated package into the OnDeck Drive folder with the January layout:

- `observations.csv`: the nine January columns first, same order and formats (Site,
  Transect, Sponge Type, Timestamp as MM:SS, Notes, ID, FileName, FrameFileName,
  AbbreviatedNote), then SpeciesCode, TimestampSeconds, Quadrant, PointX, PointY, BoxX, BoxY,
  BoxW, BoxH, CropFileName, S3Key, Annotator, LoggedAt. Points and boxes are fractions of
  frame width and height. Notes opens with the quadrant phrase. AbbreviatedNote holds the
  quadrant code.
- `frames/ID###_CODE_QUADRANT_SITE_TRANSECT.jpg` and `crops/` with the same stem as PNG. A
  click crops a 512 px square around the point. A box crops the box.
- `videos_screened.csv`: S3Key, FileName, Status, TargetSpecies, Sightings, Annotator,
  FirstOpened, MarkedDone. A finished video with no rows for a target species counts as a
  true absence of that species.
- `README.md`: counts per species, column definitions, conventions.

## Error handling

- Network drop: each chunk retries three times with backoff, then the player reports the
  lost connection and keeps retrying. Saved sightings stay on disk.
- Unplayable video: the app falls back to conversion. A failed conversion flags the video
  with the ffmpeg message.
- Failed save: the pending mark stays on screen with the error text.
- Bad input (unknown species code, point outside 0 to 1, malformed image data, `../` in a
  key, keys outside the TCRMP prefix): rejected with a message that names the field.
- Two tabs: writes take a file lock, and IDs are assigned inside the lock.

## Testing and review

- Unit: every public function, happy path and one failure path (pytest, node for geometry.js).
- Adversarial: malformed and reversed ranges, hostile keys, empty and oversized images,
  duplicate IDs, concurrent writers, a full disk, truncated S3 listing pages, every name in
  the non-standard file list.
- Integration: list the real bucket, relay a range and compare it byte for byte with a direct
  S3 read, play a video in headless Chrome, save a sighting end to end, and check the export's
  first nine columns against the January header.
- Pixel: headless Chrome screenshots at 946 and 1280 px, reviewed against the seven questions.
- Review: one independent reviewer agent per pass, two passes at most.

## Outside this round

Video clips per sighting, voice notes, loading the 246 January rows to add points, a shared
multi-user server, and a coral species list (the species file is swappable, so corals become a
configuration change).
