# Sponge Screener (`sponge_screener`)

Sponge Screener lets a person build sponge ground truth from TCRMP transect video, one click and one species pick per sighting.

- Tags: TCRMP, AI (working tags) | Version: 1.2.0 | Status: draft VICARIUS module | Owner: Lauren K Olinger
- Repo: [laurenkolinger/vicarius-sponge-screener](https://github.com/laurenkolinger/vicarius-sponge-screener) (public) | Related studies: none yet

## What it does and why it exists

The lab needs labeled sponge sightings from TCRMP transect video to train and check sponge detectors, first for the January 2026 package sent to OnDeck AI. Sponge Screener streams the videos from the public `uviai` S3 bucket, so nobody downloads a transect before screening it. A person watches at 1.5x, clicks a sponge when it sits near the center of the frame, and picks its species in a wheel. The screener then saves one row in `observations.csv`, the full frame as JPEG, and a crop of the sponge as PNG, in the layout of the January package.

The server also follows every sighting through its video, so its mark rides along with the sponge as the camera swims down the transect and nobody logs the same sponge twice. A video marked done tells OnDeck that every pinned species without a sighting is absent from that video, which turns the screening into presence and absence data.

Under VICARIUS each run folder is one screening dataset. A person opens a run from the Sponge Screener card on the VICARIUS desktop, and every sighting, image, track and export package of that session goes into the run folder. The video cache stays off the synced drive.

## Where it sits in the pipeline

- Upstream: the TCRMP transect videos in the public bucket `https://uviai.s3.us-west-2.amazonaws.com` under `TCRMP_video_ondeck/`, named `TCRMP{YYYYMMDD}_video_{SITECODE}_T{N}.{ext}`. No VICARIUS module writes them; Sponge Screener reads them only.
- Downstream: the export package (`spongeGroundTruth_<YYYYMMDD>/`) goes to OnDeck AI and to sponge detector training. No VICARIUS module reads it yet.
- The screener is not part of the 3D survey chain (3D_phase_1, manual_edit, 3D_phase2, tagfab).

## Inputs

| Input | Type | Formats | Required | What it is |
|-------|------|---------|----------|------------|
| `transect_videos` | file | `.mp4`, `.m4v`, `.mov`, `.mts`, `.m2t`, `.avi`, `.mxf`, `.wmv` | yes | TCRMP transect videos in the public bucket, streamed read only. Chrome plays MP4, M4V and MOV directly; the screener converts the archive formats (AVCHD, HDV, DV, MXF, WMV) with FFmpeg first. |
| `species_list` | file | `.csv` | yes | [config/species.csv](config/species.csv): four-letter code, scientific name, ID guide part, and default pin key for each species a person can pick. |
| `site_names` | file | `.csv` | yes | [config/sites.csv](config/sites.csv): TCRMP site code and site name; a blank name falls back to the code in the Site column. |

## Parameters

Each run keeps these settings. They are set in the window or in the file named, not on the VICARIUS launch form.

| Parameter | Type | Default | What it controls |
|-----------|------|---------|------------------|
| `annotator` | string | none | Type your initials in the Annotator field. VICARIUS fills them in from your sign-in; Sponge Screener writes them to every sighting and blocks saves without initials. |
| `pins` | list | ACAU, AFUL, CDEL, MLAE, CPLI, ACRA, ACOM, XMUT | Pin up to ten species to the number keys 1 to 9 and 0. Sponge Screener counts every pinned species with no sighting as absent when a video is marked done; each run keeps its own pins in `settings.json`. |
| `export_root` | string | `exports` in the run folder | Set `export_root` in the run's `settings.json` to send packages to another folder, such as a synced OnDeck folder. Sponge Screener writes each package as `spongeGroundTruth_<YYYYMMDD>` inside it. |
| `work_root` | string | `/mnt/tear/sponge_screener` | Set `work_root` in [config/vicarius.ini](config/vicarius.ini) to move the video cache, capped at 60 GB. Sponge Screener rejects folders inside the module directory (the drive sync copies those) and folders on unmounted disks. |

## Outputs and data-dictionary entries

Under VICARIUS the run folder `inprocess/run_<YY-MM-DD>_<INITIALS>/` is the data folder, so every path below starts there. Standalone, the same files go to `data/` in the project folder. The five descriptors are drafts staged with the harness branch `vortex-sponge-modules`; the links work once that branch is merged.

| Output dataset | Path template | Descriptor | What it is |
|----------------|---------------|------------|------------|
| Sightings table | `${run_dir}/observations.csv` | [sponge_screener_observations](../../../_METADATA/dictionary/datasets/sponge_screener_observations.yaml) | One row per sighting or Later point, 23 columns. The first nine are the January columns in their order (Site, Transect, Sponge Type, Timestamp, Notes, ID, FileName, FrameFileName, AbbreviatedNote), then SpeciesCode, TimestampSeconds, Quadrant, PointX, PointY, BoxX, BoxY, BoxW, BoxH, CropFileName, S3Key, Annotator, LoggedAt, and LabelStatus (`labeled` or `unlabeled`). Points and boxes are fractions of the frame width and height. A file with the older 22-column header still loads, every row read as labeled. |
| Frames and crops | `${run_dir}/frames/{frame_file_name}`, `${run_dir}/crops/{crop_file_name}` | [sponge_screener_images](../../../_METADATA/dictionary/datasets/sponge_screener_images.yaml) | The full frame as JPEG and a crop of the sponge as PNG, named `ID###_CODE_QUADRANT_SITE_TRANSECT`; a Later point has `LATER` in place of the code. |
| Screened videos | `${run_dir}/videos_screened.csv` | [sponge_screener_videos_screened](../../../_METADATA/dictionary/datasets/sponge_screener_videos_screened.yaml) | One row per video a person opened: status (`in progress` or `done`), the pinned species at the time, the sighting count, the annotator, and when it was first opened and marked done. A done video with no rows for a pinned species counts as a true absence. |
| Tracks | `${run_dir}/tracks/{sighting_id}.json` | [sponge_screener_tracks](../../../_METADATA/dictionary/datasets/sponge_screener_tracks.yaml) | One file per tracked sighting: its points `[t, x, y, score]` (t in video seconds, x and y as fractions of the frame, score from 0 to 1), the box size of a box sighting, and the sighting's video key and time. |
| Export package | `${run_dir}/exports/spongeGroundTruth_{export_date}/` | [sponge_screener_export](../../../_METADATA/dictionary/datasets/sponge_screener_export.yaml) | `observations.csv`, `videos_screened.csv`, `tracks.csv` (one row per track point of each labeled sighting), every referenced frame and crop, and a README with counts and column definitions. Later points are left out, and the README says how many. A second export on the same day adds `_2`, `_3` and so on. |

The run folder also holds `settings.json` (annotator, pins, export folder), `trash/` (the images and tracks of deleted sightings), `.lock` (the write lock), and `project.json`, which VICARIUS mints. Sponge Screener marks step 1, Screen videos, as running in `project.json` once a video is opened, and step 2, Export the package, as done after an export, with an `access_log` row naming the package.

Times in `LoggedAt`, `FirstOpened` and `MarkedDone` are UTC (`YYYY-MM-DDTHH:MM:SSZ`), the convention of the January package.

## How to run it

### From the UI

1. On the VICARIUS desktop (port 5090), open the Sponge Screener card.
2. Pick New run, or open an existing run. VICARIUS asks for a purpose and your initials, mints the run folder `inprocess/run_<YY-MM-DD>_<INITIALS>/` with its `project.json`, starts the screener on port 5098 if it is not running, and opens `http://127.0.0.1:5098/?project_dir=<run folder>&initials=<XX>`.
3. The window shows the run folder name beside the title, and the Annotator field starts on the initials you signed in with. Type over them if someone else is screening.

A window opened on port 5098 without a run says to open it from a run and loads nothing else.

### From the CLI

```bash
# what VICARIUS runs: VICARIUS mode on port 5098
scripts/launch_sponge_screener.sh 5098
# the same, by hand
/usr/bin/python3 screener.py --vicarius
# standalone, as on a laptop: port 8770, data/ and cache/ in the project, opens Chrome
python3 screener.py
```

`screener.py` options: `--port` (8770 standalone, 5098 with `--vicarius`; 0 picks a free port), `--no-browser`, `--data-dir`, `--cache-dir`, `--bucket-url`, `--vicarius`, and `--vicarius-ini` (default [config/vicarius.ini](config/vicarius.ini)). `--vicarius` refuses `--data-dir`, because its data lives in run folders, and never opens a browser. `SPONGE_SCREENER_DRY_RUN=1 scripts/launch_sponge_screener.sh 5098` prints the command without running it.

### Screening

1. Pick a folder and a video in the left panel. Videos marked "converts first" are archive formats; the screener converts them in the background and plays the copy when it is ready. Playback starts at 1.5x, and the page remembers the speed picked after that.
2. When a sponge sits near the center of the frame, click it. The video pauses, the quadrant lights up, and the species wheel opens on the mark. Drag instead of clicking to draw a box; the wheel opens on the box center.
3. Pick the species with a click on its slice or with its key.
   - The inner ring holds the pinned species in key order, clockwise from 12 o'clock (keys `1` to `9` and `0`).
   - The outer ring holds Unknown sponge first, then every other species by scientific name, 16 at a time. Scroll or swipe to spin it. Type letters to filter by code or name, and press Enter to pick the highlighted match.
   - The hub in the middle is Later (see below).
4. The sighting saves and the video stays paused on that frame; press Space to play on. The species label shows at the mark while the images upload in the background. A save that fails turns the label into a red `not saved` tag and shows one error toast that counts every unsaved pick, with one Retry button. The page keeps the frames and crops until the saves go through, and it asks before the tab closes or reloads while a save is unsaved or on its way.

Hold Shift while picking (click or key) to save and play on in one step. The panel under the video (pinned species keys, pin controls, note box) is hidden; press P or the panel button in the transport row to show it. The species guide (I) docks as a column beside the video, in the window and in full screen; its dock button lets it float instead. While the guide is open, the species under the pointer or the Tab highlight in the wheel shows in it. Species already logged in the open video lead the outer ring, most logged first, with their count beside the code. Near the edge of the window the wheel shifts inward, and a short leader line points back at the mark.

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
| Z | undo the last pick: an unsaved pick is discarded first, else the last saved sighting is removed (it waits up to 10 s for a save on its way) | |
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

The timestamp convention matches the January package: the moment the sponge sits closest to the center of the frame.

### Later points and the To label list

The hub of the wheel (or Space) saves the point without a species. A Later point is a full row: its frame and crop save at once, `LabelStatus` reads `unlabeled`, and `LATER` takes the place of the species code in both file names (`ID004_LATER_TOPLEFT_BIT_T1.jpg`). Later marks draw as an amber dashed ring with a `?` tag, and amber ticks mark them on the scrub bar.

The To label list in the right panel shows every Later point across all videos of the run, newest first. A click on one opens that video, seeks to the point, and opens the wheel on it, with Keep in the hub. Picking a species gives the point its label and renames its frame and crop to the new code. The ID and the `LoggedAt` time stay.

To change a saved sighting, click its mark, paused or playing; the video pauses and the wheel opens for that row. Its readout starts with `Changing ID001 ACAU`, also while the pointer rests on the hub (`Changing ID001 ACAU. Space or click: Unlabel`), and the hub ring turns bold white (amber on a Later point). Picking another species relabels it, and the hub (Unlabel) turns it into a Later point.

To delete the sighting, press Backspace (the Mac delete key) or Delete in that wheel with no filter typed. The readout reads `Press delete again to delete ID004 ACAU` (or `ID004, Later point`) and the hub ring turns red. A second press within 3 seconds deletes the row, moves its frame, its crop, and its track to `trash/`, closes the wheel, and leaves the video paused. Any other key, a move of the pointer to another slice, or 3 seconds without the second press keeps the sighting.

Later points stay out of the species tally, the Done status, and the export. The Done button refuses while a video holds Later points, because a done video tells OnDeck that missing pinned species are absent. Saving a Later point in a done video, or unlabeling one of its sightings, moves that video back to in progress.

### Full screen

The full-screen button in the transport row and the F key fill the screen with the picture and the transport row. The wheel, the marks, and the toasts work there; the pin strip, the note box, and the side panels stay in the windowed view. In full screen the page asks Chrome for the Escape key, so Escape cancels the wheel. A long press on Escape leaves full screen, and so does F while the wheel is closed.

## How it works inside

- [screener.py](screener.py) parses the options and starts [screener/server.py](screener/server.py), a standard-library `ThreadingHTTPServer` bound to 127.0.0.1 only. Every request passes the Host check (127.0.0.1 or localhost on the bound port), then the VICARIUS module lock, then, for POST, PATCH and DELETE, the page's `X-Screener: 1` header.
- The lock: [screener/lock_status.py](screener/lock_status.py) reads `_METADATA/module_locks.json` under `VICARIUS_ROOT` fresh on every request (fail-open, one warning per problem); [screener/lockgate.py](screener/lockgate.py) answers the lock page to a person and 423 to every call. Static files and `/api/health` pass.
- Runs: [screener/runs.py](screener/runs.py) accepts only a real `run_*` folder with a `project.json` directly under the runs folder. The page sends `POST /api/project/open {project_dir}` once, then names the run on every call in the `X-Screener-Run` header (`?run=` on crop thumbnails). [screener/workspace.py](screener/workspace.py) keeps one store, settings file and tracking queue per run, opened on first use. [screener/runstate.py](screener/runstate.py) writes the two steps and the export row into `project.json` through a temp file. A data call that names no run under VICARIUS answers 409.
- Video: [screener/relay.py](screener/relay.py) fetches 4 MB chunks from the bucket with 16 workers and 24 chunks of read-ahead (one connection from St. Thomas to us-west-2 gives 11 to 20 Mbps, and the 2024 videos run at 52 Mbps), caches them, and serves byte ranges to the page. [screener/convert.py](screener/convert.py) converts archive formats with FFmpeg (VideoToolbox on a Mac, libx264 elsewhere). [screener/s3catalog.py](screener/s3catalog.py) lists the bucket, reuses a listing for up to a day, and falls back to the saved listing when the bucket cannot be reached.
- Data: [screener/store.py](screener/store.py) writes `observations.csv`, `videos_screened.csv`, frames, crops and tracks under an exclusive lock on `.lock`; [screener/export.py](screener/export.py) writes the package and checks that every image arrived.
- Tracking: [screener/tracking.py](screener/tracking.py) queues one job per sighting and runs [screener/track_worker.py](screener/track_worker.py) under `.venv-track/bin/python`. The worker decodes from 12 s before to 15 s after the sighting with FFmpeg at 15 frames per second and 640 px wide, and follows the sponge with OpenCV's CSRT tracker forward and backward. A point is followed as a square 12 percent of the frame height; a box as its box. A direction ends near the frame edge (2 percent), after 3 lost frames in a row or a likeness score under 0.05, or at its 12 or 15 s limit. One job on a 2024 transect video (1080p at 60 frames per second) took 2.5 to 4.5 s. Without `.venv-track`, tracking is off and marks stay where they were clicked.
- The page: [static/app.js](static/app.js) wires [static/geometry.js](static/geometry.js), [static/wheel.js](static/wheel.js), [static/wheel_view.js](static/wheel_view.js), [static/track_math.js](static/track_math.js), [static/scrub.js](static/scrub.js) and [static/vicarius_run.js](static/vicarius_run.js) (run and signed-in initials rules) to the DOM. No GPU is used anywhere.

## Gotchas and troubleshooting

- The Annotator field is red and saves fail with `annotator: empty`: nobody has typed initials in this run yet, and the window was opened without a valid `initials` parameter. Type two to four letters and press Retry on the toast.
- The window says to open it from a run: it was opened on port 5098 without `?project_dir=`. Open the run from the Sponge Screener card on the VICARIUS desktop.
- `screener: could not start: work_root: ... is that disk mounted?`: the parent of `work_root` in [config/vicarius.ini](config/vicarius.ini) is missing; mount `/mnt/tear` or name another folder off the synced drive.
- `tracks: tracking is off` at start: `.venv-track/` is missing; build it as in Resume from cold, or screen without tracks.
- Tracking needs texture. Over bare sand, in murky water, or when the camera turns fast, a track stops early, and it can slide off its sponge for a second or two before it stops. A low score alone does not mean the track is wrong, because the score also drops when the view of a sponge changes as the camera passes over it. A sponge that leaves the picture and comes back gets a track only for the stretch around its sighting.
- A tracking job that fails runs again when the video is opened 5 minutes later or more.
- The list shows "offline copy": the bucket could not be reached, so the list comes from the saved listing; press Refresh to try again.
- Two windows on two different runs never mix: each names its own run on every call.

## Resume from cold

Everything below assumes the lab server and the clone at `/mnt/rip/vicarius_drive/vicarius/modules/sponge_screener/github_repo`.

1. Interpreter: the app runs on the system Python, `/usr/bin/python3` (3.9 or newer), with no packages. FFmpeg and ffprobe come from `/usr/bin`.
2. Work folder: `config/vicarius.ini` names `work_root` (default `/mnt/tear/sponge_screener`); the screener creates `work_root/cache` on start. The parent folder must exist.
3. Tracking (optional): `/usr/bin/python3 -m venv .venv-track` then `.venv-track/bin/pip install -r requirements-track.txt` (OpenCV contrib build and numpy). On a Python without ensurepip, make it with `--without-pip` and install with another pip: `<other python> -m pip --python .venv-track/bin/python install -r requirements-track.txt`.
4. Test environment: `/usr/bin/python3 -m venv env` and `env/bin/pip install -r requirements.txt` (pytest and PyYAML), or the `--without-pip` route above with `env/bin/python`.
5. Run: open a run from the VICARIUS desktop, or `scripts/launch_sponge_screener.sh 5098`.
6. Check success: `curl -s http://127.0.0.1:5098/api/health` answers `{"ok": true, "version": "1.2.0", "locked": false, "runs": true}`. After the first sighting, the run folder holds `observations.csv` with one row, one file in `frames/` and one in `crops/`, `settings.json` with the annotator, and `project.json` with step 1 running.

Run folder layout:

```
inprocess/run_<YY-MM-DD>_<INITIALS>/
  project.json            minted by VICARIUS; steps 1 and 2 and export rows filled by the screener
  settings.json           annotator, pins, export_root (exports)
  observations.csv        one row per sighting or Later point
  videos_screened.csv     one row per opened video
  frames/  crops/         ID###_CODE_QUADRANT_SITE_TRANSECT.jpg and .png
  tracks/                 ID###.json
  trash/                  images and tracks of deleted sightings
  exports/                spongeGroundTruth_<YYYYMMDD>/
```

## On a Mac (standalone)

Sponge Screener was first built as a Mac tool, and it still runs that way.

- Requirements: macOS with Python 3.9 or newer (the system Python works; no packages), Google Chrome, and FFmpeg with ffprobe (`brew install ffmpeg`). Node 20.10 or newer is needed for the node and end-to-end tests only.
- Run `python3 screener.py`. The server binds to 127.0.0.1:8770 and opens Chrome; data goes to `data/` and the cache to `cache/` in the project folder.
- [tools/install_dock_app.sh](tools/install_dock_app.sh) builds "Sponge Screener.app" in `~/Applications` and puts it in the Dock; the app starts the server on port 8770, restarts it when the code changed, and opens Chrome. Run it again after moving the project folder.
- Tracking: `/opt/homebrew/bin/python3 -m venv .venv-track` and `.venv-track/bin/pip install opencv-contrib-python-headless numpy` (built with OpenCV 5.0.0 and numpy 2.5.4).
- The species guide photos come from the private benthic-species-viewer repository: `python3 tools/fetch_guide_images.py` (needs `gh auth login`) writes `data/guide/`, and [tools/add_guide_descriptions.py](tools/add_guide_descriptions.py) adds the ID guide text. The photos are for internal use and stay out of git. Under VICARIUS the screener reads the same `data/guide/` folder in the clone for every run.
- Set `export_root` in `data/settings.json` to write packages straight into a synced Drive folder; the default is `exports/` in the project folder (or `SCREENER_EXPORT_ROOT`).

## Tests

```bash
env/bin/python -m pytest tests -q -m "not live and not e2e"
node --test tests/test_geometry.mjs tests/test_track_math.mjs tests/test_scrub.mjs tests/test_wheel.mjs tests/test_vicarius_run.mjs
/usr/bin/python3 tests/test_house_rules.py
env/bin/python -m pytest tests -q -m e2e      # headless Chrome against a fake bucket; needs Node 22 or newer
env/bin/python -m pytest tests -q -m live     # read-only checks against the real bucket, by hand only
```

- [tests/test_lock_gate.py](tests/test_lock_gate.py) pins the module lock: fresh reads, fail-open registries, the exact lock page, 423 for every call, and static files and health passing.
- [tests/test_runs.py](tests/test_runs.py) pins the run rules: hostile run names, two runs never mixing, 409 without a run, `project.json` bookkeeping, and concurrent saves.
- [tests/test_vicarius.py](tests/test_vicarius.py) covers `config/vicarius.ini`, the `--vicarius` options, the launch script, and one end-to-end run of `screener.py --vicarius`.
- [tests/test_module_yaml.py](tests/test_module_yaml.py) keeps `module.yaml` in step with the code.
- [tests/test_track_worker.py](tests/test_track_worker.py) runs the tracking worker under `.venv-track/bin/python` on two clips made with FFmpeg and skips when the environment is missing.
- The end-to-end tests start the app on a free port with temporary data, serve three short FFmpeg clips from a fake bucket, and drive the page in headless Chrome with real mouse and key events ([tests/e2e/cdp_driver.mjs](tests/e2e/cdp_driver.mjs)); their screenshots are written to `tests/e2e/out/`. Headless Chrome cannot check the Escape key lock in full screen, the full-screen layout on a real display, or trackpad swipes over the wheel; check those by hand after a change to full screen or the wheel.

No test reaches the real bucket, port 8770 or port 5098.

## Provenance and links

- Repository: [laurenkolinger/vicarius-sponge-screener](https://github.com/laurenkolinger/vicarius-sponge-screener). Agent rules for this clone: [CLAUDE.md](CLAUDE.md); glossary: [CONTEXT.md](CONTEXT.md); decision records: [docs/adr/](docs/adr/).
- Designs and plans: [docs/superpowers/specs/](docs/superpowers/specs/) and [docs/superpowers/plans/](docs/superpowers/plans/).
- Module contract: [module.yaml](module.yaml). Descriptors: `sponge_screener_observations`, `sponge_screener_images`, `sponge_screener_videos_screened`, `sponge_screener_tracks`, `sponge_screener_export` under `vicarius/_METADATA/dictionary/datasets/`.
- Related modules: VISTA (`video_count`) counts animals in video with a detection model; VEGA (`reef_point_seg`) trains the lab's models.
