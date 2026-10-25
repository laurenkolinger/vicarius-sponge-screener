# Species wheel build report

Date: 2026-10-13. Spec: `docs/superpowers/specs/2026-10-13-species-wheel-design.md`. Plan: `docs/superpowers/plans/2026-10-13-species-wheel.md`.

## What changed

- A click on a sponge pauses the video and opens a two-ring wheel at the click. The inner ring holds the eight pinned species (keys 1 to 8). The outer ring holds the other 30 species, starting with Unknown sponge at 12 o'clock, and spins with the scroll wheel or trackpad.
- One pick saves the sighting and the video plays on at once. The species label stays at the mark for 1.5 seconds. Images upload in the background; a failed save turns its label red and offers Retry.
- Typing letters filters the wheel; Enter picks what is lit. Shift with any pick saves and stays paused.
- Space or the center of the wheel saves a Later point with no species. The To label list in the right panel holds every Later point across all videos; clicking one opens that video at the point with the wheel ready.
- Clicking a saved mark on a paused frame reopens the wheel to change the species or clear it. The readout says "Changing ID001 ACAU".
- Later points stay out of the tally and the export. Done is refused while a video holds Later points, and a done video that gains one returns to in progress.
- F or the button at the right end of the transport row opens full screen. The wheel, marks, and toasts work there.
- Playback starts at 1.5x. The app remembers a speed you pick after that.
- Z undoes the last pick, including a pick that failed to save, and waits for a save still in flight.
- The Sponge Screener app in the Dock (version 1.1.0) restarts the server whenever the code on disk is newer than the running server, then opens Chrome. A server that fails to start shows an alert that points to `data/screener.log`.

## Tests

| Suite | Result |
|---|---|
| Python unit, adversarial, server (`python3 -m pytest tests -q -m "not live and not e2e"`) | 1005 passed (903 before) |
| Page logic (`node --test tests/test_wheel.mjs tests/test_geometry.mjs`) | 140 passed (67 before) |
| End to end in headless Chrome against a fake bucket (`python3 -m pytest tests -q -m e2e`) | 20 passed, three runs |

Each task had an independent reviewer, plus a whole-branch review. Every defect they found was fixed and has a test that failed before the fix.

## Decisions made on your behalf

| Decision | Why | Cost if wrong |
|---|---|---|
| A done video that gains a Later point returns to in progress, with a notice | A done video tells OnDeck that missing pinned species are absent | You press D again after labeling |
| The wheel stays 400 px across in narrow windows | Smaller labels are harder to read at 1.5x; full screen is the main view | One constant to lower |
| Space types a space while you are typing a name, and means Later otherwise | Names such as "aplysina c" need the space | Escape, then Space, for Later mid-name |
| Clicking a saved mark opens the edit wheel only when the video was already paused | During playback every click is a new sighting | None |
| The wheel floats over the controls around the video while open | The video area alone is too small for a readable wheel | None |
| Z discards an unsaved pick before it touches a saved sighting | Z means "undo what I just did" | None |
| The work ran in a separate copy and was installed in one step | Your running app never served half-built files | None |
| No git commits were made | Commits wait for your say-so | None |

## Open items

- A crash in the instant between renaming a relabeled point's images and writing the CSV would leave one row pointing at renamed files. Delete still recovers it.
- Wheel slices cannot take keyboard focus with Tab; the number keys and typing cover keyboard use.
- The "Sightings in this video" heading wraps to two lines when the count reads "1 · 1 later".
- When two overlapping saves land out of order, Z removes the higher ID.
- Headless Chrome cannot test real full screen: the Escape key lock (hold Escape to leave), the true screen-size layout, and trackpad spin. The README lists these as manual checks.

## Rollback

Your data before the change is in `data/backup-2026-10-13-before-wheel/`. The new server adds a `LabelStatus` column to `observations.csv` on its first save; the old code cannot read that file, so a rollback restores the backup.

## Changes after first use (2026-10-13, afternoon)

- A pick saves and leaves the video paused; Space plays on, and Shift with a pick plays on in one step.
- The panel under the video is hidden; P or the panel button in the transport row shows it, and the app remembers the choice.
- The guide photo tool now matches the viewer's morphotype and alternate names (ACAU, CCAR, OBAR). A fresh run downloaded 260 photos, including the nine species from NERE to VRIG that the September run never fetched. ACOM has no entry in the viewer and stays without photos.
- Tests: end to end 21 passed (twice), Python 1005 passed, page logic 140 passed. These changes had tests but no separate reviewer pass.
- Open: no end-to-end test picks a species from the panel's pin keys or filter list.

## Tracking, guide, and editing (2026-10-13, evening)

- Each sighting gets a track: the server runs OpenCV's CSRT tracker (opencv-contrib-python-headless 5.0.0.93 in `.venv-track/`) on the stretch around it, backward to where the sponge entered the picture and forward until it leaves, and saves the track in `data/tracks/`. Opening a video queues its untracked sightings; a job takes 2 to 3.5 s. The export adds `tracks.csv`.
- Marks ride along their tracks on every frame. Clicking any part of a mark (ring, tag, or box), paused or playing, opens its edit wheel. In that wheel, the delete key pressed twice (at least 0.3 s apart, and not within 1 s of clearing a filter) deletes the sighting, its images, and its track.
- The species guide docks beside the video, follows the species under the pointer or the Tab highlight, and species logged in the open video lead the outer ring with a count.
- Hold Left or Right to rewind or fast-forward at 4x.
- Real footage (BIT T1 2024, five points): tracks covered 1.7 to 7.7 s back and 1.8 to 6.5 s forward; forward-backward error 2.1 to 14.2 px at 1920 wide. A large orange mound held the whole way; a point on bare sand drifted for 1 to 2 s before the tracker stopped it.
- Tests: Python 1429 passed, page logic 182 passed, end to end 27 passed. Independent reviews of storage and of tracking; every defect they found is fixed.
- Open (deferred at Lauren's request for speed): a just-finished track can miss one poll; the page does not check a track's anchor time; a mark can snap back at the end of its track; a scroll does not disarm a pending delete; the page does not say when tracking is off; backlog tracking of old sightings reads up to 27 s of video each and can compete with playback on a slow link; the tracking end-to-end tests fail rather than skip without `.venv-track`; small dead code and README wording.
- Data backup before this install: `data/backup-2026-10-13-before-tracking/`.
