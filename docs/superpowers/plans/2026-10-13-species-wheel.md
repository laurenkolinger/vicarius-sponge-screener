# Species Wheel, Later Points, Full Screen: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task by task. Each implementer sees the whole plan and owns only the files listed under their task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A click on a sponge opens a two-ring species wheel at the click; one pick saves the sighting and playback resumes at once. A Later choice saves the point unlabeled for later. Labels can be changed or cleared. The whole flow works in full screen, and playback starts at 1.5x.

**Architecture:** The Python standard-library server gains an unlabeled state for observation rows (a new `LabelStatus` column), a `PATCH /api/observations/<ID>` route that relabels or unlabels a row and renames its images, and filters that keep Later points out of the tally, the Done status, and the export. The page gains a pure, node-tested wheel module (`static/wheel.js`), a DOM wheel view, a fast save that resumes playback before the images upload, a To label list, and a full-screen theater. A node Chrome DevTools driver tests the whole flow in headless Chrome against a fake bucket.

**Tech Stack:** Python 3.9 standard library (the code must also run on 3.14), pytest 8, node 20.16 (`node --test`, `--experimental-websocket`), ffmpeg at `/opt/homebrew/bin/ffmpeg`, Google Chrome at `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.

**Spec:** `docs/superpowers/specs/2026-10-13-species-wheel-design.md` (read it in full before starting). The original design is `docs/superpowers/specs/2026-09-21-sponge-screener-design.md`.

## Global Constraints

- Project root: `/Users/laurenkay/SpongeScreener`. Use absolute paths in every command.
- Python standard library only. No pip installs, no npm installs. Python code runs on 3.9: no `match`, no `X | Y` type unions, no `list[str]` in annotations evaluated at runtime.
- Every function has a docstring (JSDoc for JavaScript): purpose, parameters, returns, raises. Inputs are validated at the entry point. Errors say what failed, where, and why, and start with the field name (`species: unknown species code 'XXXX'`). Named constants, no magic numbers. No dead code, no placeholders, no commented-out code.
- Tests come first: write the failing test, run it, watch it fail, then write the code.
- Tests never touch the real bucket or the real `data/` folder. The running app on port 8770 belongs to Lauren: never stop it, never write to `/Users/laurenkay/SpongeScreener/data/`.
- No git commits, no git stash, no branch changes. Leave all changes in the working tree.
- User-facing text: American spelling, no em dashes, plain words. Every new control carries a one or two sentence `title` tooltip that names who does what ("You ... The server ..."), matching the existing tooltips in `static/index.html`. Server text reaches the DOM through `textContent` only.
- Each implementer edits only the files listed under their task. A needed change in another task's file goes in the report, not in the file.
- Suites: `cd /Users/laurenkay/SpongeScreener && python3 -m pytest tests -q -m "not live and not e2e"` (903 passing before this work) and `node --test /Users/laurenkay/SpongeScreener/tests/test_geometry.mjs` (67 passing before this work). Both stay green.

## The HTTP contract (Task 1 builds it, Tasks 2 and 3 rely on it)

| Request | Answer |
|---|---|
| `POST /api/observations` with `{key, time, point, box, species, note, frame_jpeg, crop_png}`; `species` is a code or `null` | 201 `{row}`. A `null` species saves a Later point. |
| `PATCH /api/observations/<ID>` with `{species: <code or null>}` and header `X-Screener: 1` | 200 `{row}` after relabeling (code) or unlabeling (`null`). 400 for a bad ID, a missing `species` field, a non-string non-null species, or an unknown code. 404 for an unknown ID. 403 without the header. |
| `GET /api/observations?key=<key>` | `{rows}`: every row of that video, both statuses, file order. |
| `GET /api/observations?status=unlabeled` | `{rows}`: Later points across all videos, file order. `status=labeled` works too; `key` and `status` combine. A missing `key` without `status` stays 400 `key: missing`. A `status` outside the two values is 400. |
| `GET /api/tally` | `{tally, total, unlabeled}`. `tally` and `total` count labeled rows only; `unlabeled` counts Later points. |
| `GET /api/catalog?prefix=...` | Each video entry gains `unlabeled` (int). `sightings` counts labeled rows only. |
| `POST /api/videos/done` with `{key, done: true}` | 400 `done: this video has N unlabeled point(s). Label or delete them first, because a done video tells OnDeck that missing pinned species are absent.` when the video holds Later points. `done: false` always works. |
| `POST /api/export` | `{path, observations, unlabeled_left_out}`. |

Every row the server returns has the 23 columns of `OBSERVATION_COLUMNS`, the last one `LabelStatus` (`labeled` or `unlabeled`). A Later row has `SpeciesCode` and `Sponge Type` blank and `LATER` in place of the code in `FrameFileName` and `CropFileName` (`ID004_LATER_NE_BIT_T1.jpg`).

## The page hooks (Task 2 builds them, Task 3 relies on them)

- `window.__screener` keeps `state` and `pictureRect`, and gains `wheel()`, which returns `null` when the wheel is closed, else `{mode: 'new' | 'edit', rowId: string | null, hubLabel: string, center: {x, y}, hub: {x, y, r}, inner: [{code, slot, x, y}], outer: [{code, slot, x, y}], highlighted: string | null, query: string}`, all coordinates in viewport CSS pixels (the center of each slice's label).
- DOM: the wheel root `#wheel` (`role="menu"`), slices `.wheel-slice[data-ring="inner"|"outer"][data-code]` (`role="menuitem"`), the hub `.wheel-hub`, the readout `.wheel-readout`. The theater wrapper `#theater` holds the stage, the transport row, the wheel, and the toasts. The full-screen button is `#fullscreen`. The To label section is `#to-label` with items `.to-label-item[data-id]` and a count `#to-label-count`. Sightings rows keep `.sighting[data-id]` and gain `data-status="labeled"|"unlabeled"`.
- `state.toLabel` holds the Later rows across all videos. `state.rows` keeps the rows of the open video.

## Review Focus

1. Two picks less than a second apart at 1.5x: both saves are in flight at once. Both rows must save with distinct IDs and both must show in the list in time order. Owner: Task 3 scenario `rapid_picks`.
2. Relabeling a row renames its crop: the thumbnail in the sightings list and the To label list must switch to the new file name, never show a broken image. Owner: Task 2 (replace the row object from the PATCH answer and re-render); Task 3 scenario `relabel_from_list` checks the `src`.
3. A To label point from a video in another folder (absent from the catalog listing on screen): the page must open that video, seek, and open the wheel. Owner: Task 3 scenario `relabel_from_list` puts the Later point in a second folder.
4. A hand-edited `observations.csv` with a blank or misspelled `LabelStatus`, or a Later row that still carries a SpeciesCode: the store must refuse to read it and name the line. Owner: Task 1 tests `test_label_status_blank_fails_loudly`, `test_unlabeled_row_with_code_fails_loudly`.
5. Full screen toggled, or the window resized, while the wheel is open: the wheel re-centers on the mark and stays inside the viewport. Owner: Task 2 `placeWheel` unit tests; Task 3 scenario `fullscreen`.
6. A frame step (`,` or `.`) with the wheel open, then a pick: the saved TimestampSeconds equals the stepped time. Owner: Task 3 scenario `step_then_pick`.

## File ownership ledger

| Task | Owner | Files |
|---|---|---|
| 1 Server and store | agent BE | `screener/store.py`, `screener/server.py`, `screener/export.py`, `tests/test_store.py`, `tests/test_server.py`, `tests/test_export.py` |
| 2 Page | agent FE | `static/wheel.js` (new), `static/wheel_view.js` (new), `static/app.js`, `static/index.html`, `static/style.css`, `static/geometry.js`, `tests/test_wheel.mjs` (new), `tests/test_geometry.mjs` |
| 3 End to end | agent E2E | `tests/e2e/cdp_driver.mjs` (new), `tests/test_e2e.py` (new), `README.md` |
| 4 Review | agent R | reads everything, edits nothing |

Tasks 1 and 2 run in parallel. Task 3 follows both. Task 4 follows Task 3. The lead (the session that wrote this plan) keeps the ledger, closes findings, and checks the live app.

---

## Task 1: Server and store (agent BE)

**Files:**
- Modify: `screener/store.py`, `screener/server.py`, `screener/export.py`
- Test: `tests/test_store.py`, `tests/test_server.py`, `tests/test_export.py`

**Interfaces:**
- Consumes: the existing store, server, and export modules.
- Produces: the HTTP contract above, and in `screener/store.py`:
  - `LABEL_STATUS_COLUMN = "LabelStatus"`, `LABELED = "labeled"`, `UNLABELED = "unlabeled"`, `LATER_FILE_CODE = "LATER"`.
  - `OBSERVATION_COLUMNS`: the 22 current columns plus `"LabelStatus"` last. `LEGACY_OBSERVATION_COLUMNS`: the 22 current columns.
  - `ObservationStore.add(..., species_code: Optional[str], ...)`: `None` saves a Later point.
  - `ObservationStore.set_label(obs_id: str, species_code: Optional[str]) -> Dict[str, str]`.
  - `ObservationStore.rows(key: Optional[str] = None, status: Optional[str] = None)`.
  - `ObservationStore.video_status()` entries gain `"unlabeled": int`.
  - `ObservationStore.mark_done(key, True, ...)` raises `ValueError` starting with `done:` when the video holds Later points.
  - `export_package(...)` exports labeled rows only, with `EXPORT_COLUMNS` (OBSERVATION_COLUMNS without LabelStatus), and its README states the count left out.

### Rules

- Reading `observations.csv`: accept either the 23-column header or the legacy 22-column header. Legacy rows read with `LabelStatus = "labeled"`. Any other header fails as today. The next write uses the 23-column header. A row with `LabelStatus` outside the two values, a `labeled` row with a blank SpeciesCode, or an `unlabeled` row with a non-blank SpeciesCode or Sponge Type raises StoreError naming the file, the line, and the column, and the store leaves the file alone.
- `add` with `species_code=None`: SpeciesCode and Sponge Type blank, LabelStatus `unlabeled`, file stem `ID###_LATER_<quadrant>_<site>_<transect>`. Any species code that is not `None` must be in the species list (an empty string is an unknown code). Images are required for Later points exactly as for labeled ones.
- `set_label(obs_id, species_code)` under the lock: unknown ID raises UnknownObservation; an unknown code raises ValueError `species_code: ...`. Same label as now: return the row unchanged, rename nothing, rewrite nothing. Otherwise: build new file names by swapping the second `_` part of the stem (`stem.split("_", 2)`: ID, code part, rest) for the new code or `LATER`; move each image with `os.replace` (a file already at the target belongs to no row, so `_move_to_trash` it first); then write the CSV with the new SpeciesCode, Sponge Type, LabelStatus, FrameFileName, CropFileName. ID, LoggedAt, Annotator, Notes, times, points, and boxes stay. If an image is missing, raise StoreError naming the path and change nothing. If the CSV write fails, move the images back before raising. Then sync the Sightings counts.
- Counts: `_apply_counts`, `tally`, `video_status()["sightings"]`, and the Sightings cell of `videos_screened.csv` count labeled rows only.
- `mark_done(done=True)` counts Later points of that key inside the lock and refuses with the message in the contract. `done=False` works always.
- `delete` and `rows(key)` treat Later rows like any other row.
- Server: add `"PATCH"` to `MUTATING_METHODS`, `do_PATCH`, `(OBSERVATION_ROUTE, {"DELETE": "_delete_observation", "PATCH": "_patch_observation"})`. `ScreenerApp.relabel_observation(obs_id: str, fields: Any) -> {"row": ...}` validates the ID pattern, a JSON object body, the presence of `species`, and `species` being a string or `None`. `save_observation` passes `fields["species"]` through, so `null` becomes `None`. `observation_rows(key: Optional[str], status: Optional[str])`. `tally_payload` adds `unlabeled`. `catalog_page` adds `unlabeled` to each video. `export` adds `unlabeled_left_out`.
- Export: `rows = store.rows(status=LABELED)`; Later points never enter the package, and their images are not copied. A store with Later points and no labeled rows raises ExportError `no sightings to export: ... has N unlabeled point(s) and no labeled sightings yet`. README gains the line `- Unlabeled points left out: N` when N > 0.

### Steps

- [ ] **Step 1: Write the failing store tests** in `tests/test_store.py`, following the fixtures already in that file:
  - `test_add_later_point_writes_blank_species_and_later_files`
  - `test_add_rejects_empty_string_species`
  - `test_set_label_relabels_and_renames_both_images` (old paths gone, new paths hold the same bytes)
  - `test_set_label_none_unlabels_and_renames_to_later`
  - `test_set_label_same_code_changes_nothing` (file mtime and CSV bytes unchanged)
  - `test_set_label_unknown_id_raises_unknown_observation`
  - `test_set_label_unknown_code_raises_value_error` (includes `"../ACAU"` and `"acau"`)
  - `test_set_label_missing_image_changes_nothing`
  - `test_set_label_csv_write_failure_moves_images_back` (patch `write_csv_atomic` to raise)
  - `test_set_label_waits_for_lock_held_by_another_writer` (hold the lock in a thread for 0.2 s; the call finishes after it)
  - `test_legacy_header_reads_as_labeled_and_upgrades_on_next_write`
  - `test_label_status_blank_fails_loudly` (message holds the line number and `LabelStatus`)
  - `test_unlabeled_row_with_code_fails_loudly`
  - `test_labeled_row_without_code_fails_loudly`
  - `test_tally_and_counts_skip_later_points` (videos_screened Sightings and video_status both)
  - `test_video_status_reports_unlabeled_count`
  - `test_rows_filters_by_status_and_key`
  - `test_mark_done_refused_with_later_points`, `test_mark_done_false_allowed_with_later_points`
  - `test_delete_later_point_moves_later_files_to_trash`
- [ ] **Step 2: Run them and watch them fail.** `cd /Users/laurenkay/SpongeScreener && python3 -m pytest tests/test_store.py -q`
- [ ] **Step 3: Implement the store rules.** Key pieces:

```python
def _stem_with_code(file_name: str, code: str) -> str:
    """Swap the code part of an image file name.

    Args:
        file_name: A name such as ``ID004_ACAU_NE_BIT_T1.jpg``.
        code: A species code or LATER_FILE_CODE.

    Returns:
        The same name with the second part replaced: ``ID004_LATER_NE_BIT_T1.jpg``.

    Raises:
        StoreError: When the name does not have an ID part, a code part, and a rest.
    """
    parts = file_name.split("_", 2)
    if len(parts) != 3 or not ID_PATTERN.fullmatch(parts[0]):
        raise StoreError(f"{file_name}: not an ID_CODE_rest image name")
    return "_".join([parts[0], code, parts[2]])
```

  `_read_table` gains an optional `legacy: Optional[Sequence[str]] = None` parameter: a file whose header equals `legacy` reads with the missing trailing columns filled by the caller. `_read_observations` fills `LabelStatus = LABELED` for legacy rows and runs the three LabelStatus checks.
- [ ] **Step 4: Run the store tests until green**, then the full suite.
- [ ] **Step 5: Write the failing server and export tests**:
  - `tests/test_server.py`: `test_post_observation_null_species_saves_later_point`, `test_patch_relabels_row`, `test_patch_null_unlabels_row`, `test_patch_unknown_id_404`, `test_patch_bad_id_400`, `test_patch_missing_species_field_400`, `test_patch_number_species_400`, `test_patch_without_mutation_header_403`, `test_patch_body_not_object_400`, `test_get_observations_status_unlabeled_across_videos`, `test_get_observations_bad_status_400`, `test_get_observations_without_key_or_status_400`, `test_tally_reports_unlabeled`, `test_catalog_video_has_unlabeled_count`, `test_done_refused_with_later_points_400`, `test_options_or_put_on_observation_route_405` (Allow header lists DELETE, PATCH).
  - `tests/test_export.py`: `test_export_leaves_out_later_points_and_their_images`, `test_export_readme_counts_left_out_points`, `test_export_columns_drop_label_status` (first nine columns still the January header), `test_export_only_later_points_raises`, `test_export_answer_reports_unlabeled_left_out`.
- [ ] **Step 6: Run them and watch them fail; implement; run until green.**
- [ ] **Step 7: Run the full suite** `python3 -m pytest tests -q -m "not live and not e2e"` and paste the count into your report. Also run it with `/opt/homebrew/bin/python3 -m pytest ...` (3.14).

---

## Task 2: Page (agent FE)

**Files:**
- Create: `static/wheel.js` (pure functions, no DOM), `static/wheel_view.js` (the SVG wheel and its events), `tests/test_wheel.mjs`
- Modify: `static/app.js`, `static/index.html`, `static/style.css`, `static/geometry.js` (only `marksNear` gains a `status` field in each mark), `tests/test_geometry.mjs`

**Interfaces:**
- Consumes: the HTTP contract above.
- Produces: the page hooks above.

### `static/wheel.js` (pure, node-tested)

```javascript
import { filterSpecies } from './geometry.js';

/** Sizes in CSS pixels at scale 1, and the ring layout. */
export const WHEEL = Object.freeze({
  hubRadius: 34,
  innerRadius: 118,
  outerRadius: 200,
  outerSlots: 16,
  minScale: 0.6,
  maxScale: 1,
  fitFraction: 0.94,
});
export const UNKNOWN_CODE = 'UNKN';
```

Functions, each with JSDoc and a TypeError naming the argument on bad input:

- `wheelScale(viewW, viewH) -> number`: `clamp(min(viewW, viewH) * fitFraction / (2 * outerRadius), minScale, maxScale)`.
- `placeWheel(mark, view, radius) -> {x, y, shifted}`: center on the mark; on each axis, when the view is wider than `2 * radius`, clamp the center to `[radius, size - radius]`, else center on that axis. `shifted` is true when the center moved more than half a pixel. The wheel lives in a viewport-sized layer, so `view` is the viewport (or the full-screen element), not the stage.
- `sliceIndex(dx, dy, count) -> number`: angle measured clockwise from 12 o'clock, slice 0 centered at 12 o'clock, each slice `2π / count` wide. Returns -1 when count is 0.
- `zoneAt(dx, dy, scale, innerCount) -> {zone: 'hub'|'inner'|'outer'|'outside', index}`: by distance `hypot(dx, dy) / scale` against hubRadius, innerRadius, outerRadius; index from `sliceIndex` with `innerCount` or `outerSlots`. A point exactly on a boundary belongs to the outer zone of the two.
- `sliceCenter(index, count, ringMid) -> {x, y}`: offset from the wheel center of a slice's label.
- `innerItems(pins, pinKeys) -> [{slot, code}]`: pinned slots in key order (1 to 9, then 0), empty slots skipped.
- `outerOrder(species, pinnedCodes) -> string[]`: every unpinned code; `UNKN` first when unpinned, then the rest by scientific name (`localeCompare`, base sensitivity).
- `outerWindow(codes, offset, slots) -> [{slot, code}]`: when `codes.length <= slots`, slot i holds codes[i] and offset is ignored. Otherwise slot s holds `codes[mod(offset + rel(s), n)]` with `rel(s) = s <= slots / 2 ? s : s - slots`, so offset 0 puts codes[0] at 12 o'clock, the list runs clockwise to 6 o'clock, its end comes back counterclockwise from 12 o'clock, and the seam sits just past 6 o'clock.
- `spinOffset(offset, steps, length) -> number`: wraps in both directions; returns 0 when length is 0.
- `wheelMatches(species, pinnedCodes, query) -> {order: string[], inner: string[], outer: string[]}`: `order` is `filterSpecies(species, query)` codes; `inner` keeps the pinned ones, `outer` the rest, both in rank order. An empty query gives `outer = outerOrder(...)` and `inner = pinnedCodes`.
- `shortLabel(name, maxChars) -> string`: the epithet of a binomial (or the whole name otherwise, lowercased for `Unknown sponge` to `unknown`), cut to `maxChars` with a trailing `…`.

`tests/test_wheel.mjs` (written first): eight and ten inner slices; `zoneAt` at the hub edge, both ring edges, just outside, every slice boundary of 8 and of 16 slots (including the wrap at 12 o'clock); `outerWindow` at offset 0, 1, -1, n, with 30 codes and with 10 codes; `spinOffset` both directions and length 0; `placeWheel` at each edge, each corner, the center, and a view smaller than the wheel; `wheelScale` at 1280 x 800, 946 x 700, 400 x 300; `outerOrder` puts UNKN first and skips pinned codes; `wheelMatches` for `""`, `"apl"`, `"saur"`, `"zzz"`; `shortLabel` for long, short, and Unknown names; bad-type inputs throw TypeError naming the argument.

### `static/wheel_view.js`

`createWheel(layer, handlers)` builds an SVG wheel inside `layer` (a `position: fixed; inset: 0; pointer-events: none` element inside `#theater`; only the wheel itself takes pointer events) and returns an object with `open(options)`, `close()`, `isOpen()`, `relayout(center, scale, markPx)`, `spin(steps)`, `setQuery(text)`, `moveHighlight(step)`, `pickHighlighted(stay)`, `snapshot()` (the data `window.__screener.wheel()` returns). `handlers`: `onPick(code | null, {stay})`, `onHubPick({stay})`, `onCancel()`. Look and feel:

- Slices are dark translucent wedges (`rgba(16, 18, 22, 0.78)`) with a 1 px light separator, so the paused frame shows dimly behind them. The hub is a see-through hole with a thin ring; its label (`Later`, `Unlabel`, or `Keep`) sits just under the mark ring.
- Inner slice label: the key number small, the code bold (13 px), the short epithet (10 px, italic). Outer slot label: code bold (12 px) and short epithet (9.5 px). Labels stay upright.
- Hover or highlight fills the wedge with the mark pink (`#ff2bd6`) at 85 percent and dark ink. The current species of an edited row carries a white outline.
- A readout chip above the wheel shows the hovered or highlighted species: `ACAU  Aplysina cauliformis  key 1`, and the typed query when filtering.
- Scroll over the outer ring spins it: accumulate `deltaY` (normalize `deltaMode` lines to 16 px) and step one slot per 40 px, at most 3 slots per event, with a 120 ms rotate transition. A seam tick sits just past 6 o'clock. Every `open` resets the outer ring to offset 0, so positions stay learnable. When filtering with no more matches than slots, scroll and Up/Down move the highlight instead.
- When `shifted`, a thin leader line runs from the hub edge to the mark.
- Shift held at the moment of a click passes `{stay: true}`.
- Right-click on the wheel calls `onCancel` and blocks the context menu.
- Opening and closing take no animation longer than 90 ms, and `prefers-reduced-motion` turns the spin transition off.

### `static/app.js` changes

- **Opening:** `beginMark` opens the wheel (mode `new`, hub `Later`) centered on the mark, using `wheelScale` and `placeWheel` against the theater's viewport rectangle. A mouseup on the picture without a drag, within 14 px of a mark drawn on screen (saved or Later), opens the wheel in mode `edit` for that row instead (hub `Unlabel` for a labeled row, `Keep` for a Later row), with the row's species outlined. The video pauses in both cases.
- **Keys while the wheel is open** (route them from `onMarkingKey`): `1` to `9`, `0` pick the pinned species; letters and Backspace go to `wheel.setQuery`; Up and Down call `moveHighlight` when a query is set, else `spin`; Enter picks the highlighted match (Shift+Enter stays paused); Space is the hub (Later, Unlabel, or Keep), Shift+Space stays paused; `,` `.` step a frame and keep the wheel; Escape cancels; Tab moves to the note box and Enter or Escape in the note box returns to the wheel (Enter never saves from the note box now); `?` toggles help. Shift with a digit uses `event.code`, as `pinKeyFromEvent` already does.
- **Clicks outside the wheel:** a mousedown on the picture moves the mark (mode `new`) or drops the edit and starts a new mark; a click on the pin strip or the filter list picks that species; a mousedown anywhere else cancels the mark and leaves the video paused.
- **Fast save (mode `new`):** on a pick, `await waitForSeek()`, copy the frame into a canvas at native size (synchronous `drawImage`), close the wheel, end the mark, push a flash label `{point, box, text, until}` for 1500 ms, and resume playback unless `stay`. Then encode the JPEG frame (`toBlob`, quality 0.95) and the PNG crop (`cropRect` as today) asynchronously, convert to base64, and POST. On success add the row to `state.rows` (or `state.toLabel` for a Later point), re-render the lists, refresh the tally, and show a short toast (`Saved ID007 ACAU` or `Saved ID008 for later`). On failure keep the request body in memory and show an error toast with a Retry button that resends it; the flash label turns into a red `not saved` tag until Retry succeeds. The old `savePending` and `captureImages` give way to this path; keep `waitForSeek`.
- **Edit (mode `edit`):** a pick sends `PATCH /api/observations/<ID>` with the code; the hub sends `null` for `Unlabel` and nothing for `Keep`. Replace the row object in `state.rows` and `state.toLabel` from the answer (so thumbnails use the new file name), re-render, refresh the tally, resume playback unless `stay`.
- **Flash labels:** drawn by `drawOverlay` in the mark pink with dark ink, the species code and short epithet, fading over the last 400 ms. While any flash is live, a `requestAnimationFrame` loop redraws the overlay; it stops when none are left.
- **Later marks:** amber `#ffb020`, dashed ring, tag `?` (`marksNear` passes `status` from `LabelStatus`). Scrub ticks for Later points are amber.
- **Sightings list:** a Later row shows `Later` in amber in place of the code, `data-status="unlabeled"`. Clicking a Later row jumps there and opens the edit wheel on it; clicking a labeled row jumps and pauses as today.
- **To label list:** a new section in the right panel between the sightings and the tally: head `To label` with `#to-label-count`, rows `.to-label-item` with the crop, the video file name (site and transect), and the time, newest first. Empty state: `No points waiting for a label.` Load it from `GET /api/observations?status=unlabeled` at start and after every save, relabel, and delete. Clicking a row: when its video is open, seek, pause, and open the edit wheel; otherwise open the video with an entry built from the row (`{key: S3Key, name: FileName, playable: extension is mp4, m4v, or mov, converted: null, status: 'in progress'}`, or the catalog entry when the folder on screen holds it), then seek to the time once the metadata loads (take precedence over the remembered position), and open the edit wheel once the seek completes.
- **Done:** when `state.rows` holds Later points, toggling to done shows the refusal text from the contract as an error toast without calling the server; the server refusal is the backstop.
- **Video browser:** a video with `unlabeled > 0` shows `· N later` after its sightings count.
- **Tally footer:** `N sightings across all videos. M points to label.`
- **The bar under the video:** remove `#save` and `#save-stay` and their handlers; keep `#cancel`. Choosing a species anywhere saves at once. The summary line in marking mode reads `<quadrant phrase> · <time> · point|box · pick a species in the wheel, or Space for later`.
- **Full screen:** wrap `.stage` and `.transport` in `<div id="theater" class="theater">`, and move `#toasts`, `#help`, and `#guide` inside `#theater` so they show in full screen. Add `#fullscreen` to the transport row (new sprite symbols `i-expand` and `i-collapse`; tooltip: `You fill the screen with the video and its controls. The F key does the same, and F or a long press on Escape leaves.`). The F key toggles full screen in screening mode. `dom.theater.requestFullscreen()`; on `fullscreenchange` set `body[data-fullscreen]`, call `navigator.keyboard.lock(['Escape'])` when it exists (unlock on exit; ignore a rejection), redraw the overlay, and `relayout` an open wheel. In full screen the theater is a column: the stage takes the free height, the transport row stays at the bottom.
- **Speed:** `DEFAULT_SPEED = 1.5`; the stored key becomes `screener.speed.v2`, so the first start after this change plays at 1.5x.
- **Help card:** rewrite the marking column for the wheel keys above and add `F` to the screening column.
- **Hooks:** add `wheel: () => wheel.snapshot()` to `window.__screener` and assign `state.toLabel`.

### Steps

- [ ] **Step 1: Write `tests/test_wheel.mjs` (all cases above), run `node --test /Users/laurenkay/SpongeScreener/tests/test_wheel.mjs`, watch it fail.**
- [ ] **Step 2: Write `static/wheel.js` until green.**
- [ ] **Step 3: Add the `status` field to `marksNear` with a failing test in `tests/test_geometry.mjs` first.**
- [ ] **Step 4: Build `static/wheel_view.js`, then the `app.js`, `index.html`, and `style.css` changes, in the order: open and pick (new), fast save, Later, edit, To label, full screen, speed, help.**
- [ ] **Step 5: Check the page by hand against a throwaway server** (never the one on port 8770): `python3 /Users/laurenkay/SpongeScreener/screener.py --no-browser --port 8791 --data-dir <scratch>/data --cache-dir /Users/laurenkay/SpongeScreener/cache` (the shared cache is read-mostly and safe to reuse), open `http://127.0.0.1:8791/` in headless Chrome or the browser pane, and screenshot at 1280 x 800 and 946 x 700 with the wheel open, and in full screen. Read every screenshot. Stop that server when done.
- [ ] **Step 6: Run both node suites and the Python suite**, paste the counts into your report.

---

## Task 3: End to end (agent E2E)

**Files:**
- Create: `tests/e2e/cdp_driver.mjs`, `tests/test_e2e.py`
- Modify: `README.md` (screening section, key table, Later points, To label list, full screen, 1.5x default, new tests command)

**Interfaces:**
- Consumes: the HTTP contract, the page hooks, `tests/fakes3.py` (`FakeS3().start()`, `.put(key, bytes)`, `.url`), `screener.server.ScreenerApp(data_dir, cache_dir, config_dir, static_dir, bucket_url, port)` and `make_server(app, host, port)`.
- Produces: `python3 -m pytest tests -q -m e2e` runs the scenarios; screenshots are written to `tests/e2e/out/` (already gitignored).

### Setup in `tests/test_e2e.py` (module marked `pytest.mark.e2e`)

- Skip with a clear reason when ffmpeg, Chrome, or node is missing.
- Make two clips once per module with ffmpeg into `tmp_path_factory`: `ffmpeg -v error -f lavfi -i testsrc2=size=1280x720:rate=30 -t 12 -pix_fmt yuv420p -c:v libx264 -movflags +faststart <out>`.
- FakeS3 holds `TCRMP_video_ondeck/2024Annual/TCRMP20241018_video_BIT_T1.mp4` and `TCRMP_video_ondeck/2023Annual/TCRMP20231005_video_FLC_T2.mp4`.
- Start `ScreenerApp` on port 0 with temp data and cache folders, the real `config/` and `static/`, and the fake bucket URL, served by `make_server` on a thread.
- Launch Chrome headless (`--headless=new --remote-debugging-port=0 --user-data-dir=<tmp> --window-size=1280,800 --autoplay-policy=no-user-gesture-required`), read the `DevTools listening on ws://...` line from stderr, and run `node --experimental-websocket tests/e2e/cdp_driver.mjs <ws url> <app url> <scenario> <out dir>` per scenario. The driver prints one JSON line of results; pytest asserts on it and independently on `observations.csv` and the image folders in the temp data folder (a second method).

### Scenarios (one pytest test each)

- `pick_inner`: open the 2024 video, wait until it plays, click the picture at (0.30, 0.40), assert `wheel().mode == 'new'`, click the inner slice of key 1. Within 1 s the video plays again (`paused == false`, `currentTime` advancing); the CSV gains a row with SpeciesCode = the code pinned to key 1 and LabelStatus `labeled`; both images exist.
- `pick_key_and_outer`: click, press `2`; click, type `saur`, press Enter. Rows for key 2's code and SAUR.
- `rapid_picks`: two click-and-key picks less than one second apart. Two rows with distinct IDs; the sightings list shows both in time order.
- `step_then_pick`: click, press `.` three times, press `1`. The row's TimestampSeconds equals the time the page showed after the steps, within 0.001 s.
- `later_and_cancel`: click, press Space: a Later row, `LATER` in both file names, the To label count is 1. Click, press Escape: no new row, video playing.
- `relabel_from_list`: save a Later point in the 2023 video (open it by key through the page's own open path), switch to the 2024 video, click the To label item. The 2023 video opens, seeks to the point, and the wheel opens in mode `edit` with hub `Keep`; press `3`. The row now carries key 3's code, files renamed on disk, the old names gone, the sightings thumbnail `src` ends with the new crop name, the To label count is 0.
- `unlabel_saved`: pause on a saved labeled mark, click it, assert mode `edit` with hub `Unlabel`, press Space. The row reads `unlabeled`.
- `done_refused`: with a Later point in the open video, press D. Status stays `in progress`; the last toast holds `unlabeled`.
- `fullscreen`: press F; `document.fullscreenElement.id == 'theater'`. Click the picture near the top-left corner; the wheel stays inside the viewport and the leader line shows. Press F with the wheel closed to leave; open the wheel, toggle full screen with the button, and assert the wheel re-centers inside the viewport.
- `speed_default`: a fresh profile plays at 1.5x (`playbackRate == 1.5`) and the speed label reads `1.5x`.
- `screenshots`: wheel open at 1280 x 800, at 946 x 700, and in full screen; a Later mark and a flash label on screen. Save PNGs to `tests/e2e/out/`.

### Steps

- [ ] **Step 1: Write the driver** with a small CDP client over `WebSocket` (Target.attachToTarget with `flatten`, Runtime.evaluate with `awaitPromise`, Input.dispatchMouseEvent and Input.dispatchKeyEvent for real user gestures, Page.captureScreenshot, Emulation.setDeviceMetricsOverride for 946 x 700). Every wait polls with a deadline and fails with what it waited for.
- [ ] **Step 2: Write the pytest module, run `python3 -m pytest /Users/laurenkay/SpongeScreener/tests/test_e2e.py -q -m e2e`**, and iterate until every scenario passes three runs in a row. A failure that comes from page code goes into your report with the scenario, the step, and the observed state; do not edit `static/`.
- [ ] **Step 3: Update `README.md`.**
- [ ] **Step 4: Run all suites and paste the counts into your report.**

---

## Task 4: Review (agent R)

Read the spec, this plan, and the diff (`git -C /Users/laurenkay/SpongeScreener diff` plus the new files). Run every suite. Look at the screenshots in `tests/e2e/out/` and take your own with headless Chrome against a throwaway server on a free port (never 8770). Answer the seven questions (spacing, clarity, functionality, layout, accessibility, consistency, ease of use) with a one-line verdict each. Report defects (wrong output, a crash, a silent miscount, a broken control) separately from notes (wording, style), each with file, line, and a reproduction. Edit nothing.
