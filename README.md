# Sponge Screener

A local tool for building sponge ground truth from TCRMP transect video. It
streams videos from the public `uviai` S3 bucket. Each sighting is one click on
the sponge and one pick of its species in a wheel; the video then waits,
paused, until you press Space. Every sighting saves a row in `observations.csv`, the full frame as JPEG,
and a crop of the sponge as PNG, in the layout of the January 2026 package sent
to OnDeck AI.

## Requirements

- macOS with Python 3.9 or newer (the system Python works; no packages to install)
- Google Chrome
- ffmpeg and ffprobe (`brew install ffmpeg`), used for archive formats Chrome
  cannot play (AVCHD `.mts`, HDV `.m2t`, DV `.avi`, `.mxf`, `.wmv`) and by the
  end-to-end tests
- node 20.10 or newer, for the node and end-to-end tests only
- For sponge tracking (optional): a Python environment in `.venv-track/` inside
  the project folder with OpenCV and numpy. It was built with
  `/opt/homebrew/bin/python3 -m venv .venv-track` and
  `.venv-track/bin/pip install opencv-contrib-python-headless numpy`
  (OpenCV 5.0.0, numpy 2.5.4). Without it the app works as before, and marks
  stay where they were clicked.

## Run

```bash
python3 screener.py
```

The server binds to `127.0.0.1:8770` and opens Chrome. Options: `--port`,
`--no-browser`, `--data-dir`, `--cache-dir`, `--bucket-url`.

`tools/install_dock_app.sh` builds a Dock app that does the same with one click.
The app asks launchd to run the server as the service
`edu.uvi.vicar.sponge-screener.server`, because macOS stops any process an app
leaves behind when the app exits. The service starts only when you click the app
and keeps running after Chrome closes. Stop it with:

```bash
launchctl kill SIGTERM gui/$(id -u)/edu.uvi.vicar.sponge-screener.server
```

## Screening

1. Pick a folder and a video in the left panel. Videos marked "converts first"
   are archive formats; the app converts them in the background and plays the
   copy when it is ready. Playback starts at 1.5x, and the app remembers the
   speed you pick after that.
2. Watch. When a sponge sits near the center of the frame, click it. The video
   pauses, the quadrant lights up, and the species wheel opens on the mark.
   Drag instead of clicking to draw a box; the wheel opens on the box center.
3. Pick the species with a click on its slice or with its key.
   - The inner ring holds the pinned species in key order, clockwise from 12
     o'clock (keys `1` to `9` and `0`).
   - The outer ring holds Unknown sponge first, then every other species by
     scientific name, 16 at a time. Scroll or swipe to spin it. Type letters to
     filter by code or name, and press Enter to pick the highlighted match.
   - The hub in the middle is Later (see below).
4. The sighting saves and the video stays paused on that frame; press Space to
   play on. The species label shows at the mark while the images upload in the
   background. A save that fails turns the label into a red `not saved` tag and
   shows one error toast that counts every unsaved pick, with one Retry button.
   The page keeps the frames and crops until the saves go through, and it asks
   before the tab closes or reloads while a save is unsaved or still on its way.

Hold Shift while you pick (click or key) to save and play on in one step.
The panel under the video (pinned species keys, pin controls, note box) is
hidden; press P or the panel button in the transport row to show it. The species
guide (I) docks as a column beside the video, in the window and in full screen,
so it never covers the picture; its dock button lets it float instead. While the
guide is open, the species under the pointer or the Tab highlight in the wheel
shows in it. Species already logged in the open video lead the outer ring, most
logged first, and carry their count beside the code. Near the edge of the window the wheel shifts
inward, and a short leader line points back at the mark.

| Key | Screening mode | Marking mode (wheel open) |
|---|---|---|
| Click, drag | mark a point or draw a box, and open the wheel; a click on a saved mark, paused or playing, opens its wheel | move the mark there |
| Space | play or pause | Later: save without a species; while a filter is typed, a space |
| Left, Right | hold to rewind or fast forward at 4x; let go to stop | step one frame |
| `,` `.` | step one frame | step one frame; the mark and the wheel stay |
| `[` `]` | slower, faster (0.5x to 3x) | |
| Home | back to the start | |
| F | full screen on or off | types into the filter |
| P | show or hide the species keys panel | types into the filter |
| G | quadrant grid and center line | |
| Z | undo the last pick: an unsaved pick is discarded first, else the last saved sighting is removed (it waits up to 10 s for a save still on its way) | |
| D | mark the video fully screened | |
| N | next video | |
| I | species guide | |
| ? | shortcut card | shortcut card |
| `1` to `9`, `0` | | pick the pinned species; the video stays paused |
| letters, Backspace | | filter the wheel |
| Backspace or Delete, twice | | on a saved mark with no filter typed: delete the sighting with its images and its track (the first press turns the hub ring red; any other key cancels) |
| Up, Down, scroll | | spin the outer ring, or move through the matches |
| Enter | | pick the highlighted match; the video stays paused |
| Shift + a pick | | save and play on |
| Escape, right-click (wheel or picture) | | cancel the mark and play on |
| Click anywhere else | | cancel the mark; the video stays paused |
| Tab, Shift+Tab | | walk the highlight around the wheel; an open guide follows it (click the note box to type a note) |

The timestamp convention matches the January package: the moment the sponge
sits closest to the center of the frame.

## Later points and the To label list

The hub of the wheel (or Space) saves the point without a species. A Later
point is a full row: its frame and crop save at once, `LabelStatus` reads
`unlabeled`, and `LATER` takes the place of the species code in both file names
(`ID004_LATER_TOPLEFT_BIT_T1.jpg`). Later marks draw as an amber dashed ring
with a `?` tag, and amber ticks mark them on the scrub bar.

The To label list in the right panel shows every Later point across all
videos, newest first. A click on one opens that video, seeks to the point, and
opens the wheel on it, with Keep in the hub. Picking a species gives the point
its label and renames its frame and crop to the new code. The ID and the
`LoggedAt` time stay.

To change a saved sighting, click its mark, paused or playing; the video
pauses and the wheel opens for that row. Its readout starts with `Changing ID001 ACAU`, also
while the pointer rests on the hub (`Changing ID001 ACAU. Space or click:
Unlabel`), and the hub ring turns bold white (amber on a Later point) and keeps
that color when lit, so it never reads like a new mark. Picking another species
relabels it, and the hub (Unlabel) turns it into a Later point.

To delete the sighting, press Backspace (the Mac delete key) or Delete in that
wheel with no filter typed. The readout reads `Press delete again to delete
ID004 ACAU` (or `ID004, Later point`) and the hub ring turns red. A second
press within 3 seconds deletes the row, moves its frame, its crop, and its
track to the trash, closes the wheel, and leaves the video paused; the toast
reads `Deleted ID004 ACAU`. Any other key, a move of the pointer to another
slice, or 3 seconds without the second press keeps the sighting.

Later points stay out of the species tally, the Done status, and the export.
The Done button refuses while a video holds Later points, because a done video
tells OnDeck that missing pinned species are absent. Saving a Later point in a
done video, or unlabeling one of its sightings, moves that video back to in
progress. The export toast says how many Later points the package left out.

## Tracking

The server follows every sighting through its video, so its mark rides along
with the sponge as the camera swims down the transect. While you play on, the
marks show which sponges are logged already, so none is logged twice.

- A saved sighting joins the tracking queue at once. Opening a video queues
  every sighting of that video that has no track yet, behind the new
  sightings. One job runs at a time in the background, and the page asks for
  the finished tracks every 3 seconds while some are on their way.
- A job runs `screener/track_worker.py` under `.venv-track/bin/python`. The
  worker decodes the stretch from 12 seconds before the sighting to 15 seconds
  after it with ffmpeg, at 15 frames per second and 640 px wide, and follows
  the sponge with OpenCV's CSRT tracker forward from the sighting's frame and
  backward from it. It follows a point as a square 12 percent of the frame
  height around it, and a box as its box. A direction ends when the sponge
  comes within 2 percent of the edge of the picture; after 3 frames in a row
  where the tracker loses it, or where the tracked patch no longer looks like
  the sighting at all (a score under 0.05); or at its 12 or 15 second limit.
  A job on a 2024
  transect video (1080p at 60 frames per second) took 2.5 to 4.5 seconds.
- The page draws each mark where its track puts it, on every frame the video
  shows. A mark with no track for that moment shows where it was clicked,
  within 0.6 s of its time, as before.
- A track follows its row through a relabel, and goes to the trash with it
  when the row is deleted. A job that fails, for example because the video
  could not be read, runs again when the video is opened 5 minutes later or
  more. The server prints one line per finished job, such as
  `tracks: ID007 saved 172 points from 62.33 s to 73.73 s (back exit, forward
  exit) in 3.4 s`.

Tracking needs texture. Over bare sand, in murky water, or when the camera
turns fast, a track stops early, and it can slide off its sponge for a second
or two before it stops. Each point of a track carries a score from 0 to 1, the
likeness of the tracked patch to the sighting's patch; it also drops when the
view of a sponge changes as the camera passes over it, so a low score alone
does not mean the track is wrong. A sponge that leaves the picture
and comes back gets a track only for the stretch around its sighting. Without
`.venv-track`, the server prints `tracks: tracking is off` at start and the
marks stay where they were clicked.

## Full screen

The full-screen button in the transport row and the F key fill the screen with
the picture and the transport row. The wheel, the marks, and the toasts work
there. The pin strip, the note box, and the side panels stay in the windowed
view. In full screen the page asks Chrome for the Escape key, so Escape cancels
the wheel. A long press on Escape leaves full screen, and so does F while the
wheel is closed; with the wheel open, F types into the filter like any letter.

## Coverage

The Coverage button opens a grid of the 8 target species (ID guide Parts 1 and 2)
by every video in the folder on screen, so a zero can be told apart from a video
nobody finished. Each cell shows your count and, when a January file is present,
the January count after a J. An amber cell means January logged the species and
nothing is tagged yet. A cell reads none only when the video is marked done and
every second was watched.

The app records a stretch as watched while the video plays forward, at any speed
on the speed control, with the tab in view and neither the Coverage grid nor the
shortcuts card open. Seeks, arrow-key scrubbing, and frame steps do not count, and
a gap of 1 second or less counts as watched. The app cannot tell whether you were
looking at the screen, so a video left playing counts as watched. The Done
button warns when a stretch was not watched, and its Mark done anyway button goes
ahead. Viewing from before this feature was never recorded, so watched time starts
at zero.

To show the January counts, copy the January `observations.csv` to
`data/reference_observations.csv`.

## Where things go

- `data/observations.csv`: one row per sighting or Later point. The first nine
  columns are the January columns in the same order (Site, Transect, Sponge
  Type, Timestamp, Notes, ID, FileName, FrameFileName, AbbreviatedNote),
  followed by SpeciesCode, TimestampSeconds, Quadrant, PointX, PointY, BoxX,
  BoxY, BoxW, BoxH, CropFileName, S3Key, Annotator, LoggedAt, and LabelStatus
  (`labeled` or `unlabeled`). Points and boxes are fractions of frame width and
  height. A file with the older 22-column header still loads, with every row
  read as labeled, and the next write adds the LabelStatus column.
- `data/frames/` and `data/crops/`: `ID###_CODE_QUADRANT_SITE_TRANSECT.jpg`
  and the same stem as `.png`. A Later point has `LATER` in place of the code.
- `data/videos_screened.csv`: which videos were opened, which are marked done,
  and the pinned species at the time. A finished video with no rows for a
  pinned species counts as a true absence.
- `data/tracks/`: one `ID###.json` per sighting with a track: its points
  `[t, x, y, score]` (t in video seconds, x and y as fractions of the frame,
  score from 0 to 1), the box size of a box sighting, and the sighting's video
  key and time.
- `data/watched.json`: the watched stretches of each video, in seconds. Export
  leaves this file out.
- `data/settings.json`: annotator initials, pinned species, export folder.
- `cache/`: video chunks (capped at 60 GB, oldest removed first), converted
  files, and the bucket listing.
- Export writes `spongeGroundTruth_<date>/` into the export folder with
  `observations.csv`, `videos_screened.csv`, `tracks.csv` (one row per track
  point of each labeled sighting: ID, SpeciesCode, TimeSeconds, X, Y, the box
  centered on X and Y for a box sighting, and Score), every referenced image,
  and a README with counts and column definitions. It leaves Later points out, and its README states how many it
  left out. The export folder defaults to `exports/` here; set `export_root` in
  `data/settings.json` to write straight into a synced Drive folder.

## Configuration

- `config/species.csv`: code, scientific name, ID guide part, default pin.
  Codes are four upper-case letters.
- `config/sites.csv`: site code and site name. Blank names fall back to the
  code in the Site column.

## Tests

```bash
python3 -m pytest tests -q -m "not live and not e2e"
node --test tests/test_track_math.mjs tests/test_scrub.mjs tests/test_wheel.mjs tests/test_geometry.mjs
python3 -m pytest tests -q -m e2e       # headless Chrome against a fake bucket
python3 -m pytest tests -q -m live      # read-only checks against the real bucket
```

`tests/test_track_worker.py` runs the tracking worker under
`.venv-track/bin/python` on two clips it makes with ffmpeg (a texture that pans
at a known speed, and a flat gray picture), and skips with a message when the
venv is missing. `tests/test_tracking.py` drives the tracking queue with a
stand-in worker, so it needs no OpenCV.

The end-to-end tests start the app on a free port with temporary data and cache
folders, serve three short ffmpeg clips from a fake bucket (two test patterns
and the panning texture, for the tracking scenarios), and drive the page in
headless Chrome with real mouse and key events (`tests/e2e/cdp_driver.mjs`).
Each scenario runs against fresh data, then checks `observations.csv` and the
image folders on disk. They never touch `data/`, the real bucket, or port 8770.
A test skips with a message when ffmpeg, node, or Chrome is missing. Screenshots
of the wheel at 1280 x 800, at 946 x 700, and in full screen, a Later mark, and
a flash label land in `tests/e2e/out/`. Set `E2E_VERBOSE=1` and add `-s` to
print what each scenario saw.

Headless Chrome cannot check a few things. Check these by hand in a real Chrome
window after a change to full screen or the wheel:

- In full screen, Escape closes an open wheel and keeps full screen (the
  Escape key lock), and holding Escape for about two seconds leaves full screen.
- Full screen fills the whole display: the picture takes the free height and the
  transport row sits at the bottom edge.
- A two-finger trackpad swipe over the wheel spins the outer ring one slot at a
  time, in both directions, and a mouse scroll wheel does the same.

## Design

The approved designs are in `docs/superpowers/specs/` and the build plans in
`docs/superpowers/plans/`.
