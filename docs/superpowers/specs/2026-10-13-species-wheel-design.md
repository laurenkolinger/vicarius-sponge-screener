# Sponge Screener: species wheel, Later points, full screen

Date: 2026-10-13. Requested by Lauren Olinger. Status: awaiting approval.
Builds on `2026-09-21-sponge-screener-design.md`.

## Purpose

Lauren screens each video at about 1.5x and wants a sighting to cost one click on the sponge and
one click on its species, with playback resuming by itself. She logs every species she can
identify, and the eight pinned species stay the important ones. A point she cannot name yet
saves as a Later point that she can return to. She can do all of this in full screen.

## Decisions taken in conversation

- Picker: a two-ring wheel centered on the click. Lauren chose it over a drum list and over an
  eight-slice wheel with a More slice.
- Later: the point saves at once with its frame and crop and no species. Lauren chose it over a
  bookmark without images and over no record.

## The wheel

A click on the picture pauses the video, drops the mark, and opens the wheel centered on the
mark. A drag still draws a box, and the wheel opens on the box center.

| Zone | Holds | Choosing it |
|---|---|---|
| Hub, the see-through hole over the mark | Later | Saves the point unlabeled and plays on |
| Inner ring | The pinned species in key order, clockwise from 12 o'clock (eight today; up to ten when keys 9 and 0 are pinned) | Saves the sighting and plays on |
| Outer ring | Every other species: Unknown sponge first, then the rest by scientific name | Saves the sighting and plays on |

- The outer ring shows 16 species at a time. The scroll wheel or a two-finger trackpad swipe spins
  it one slot per step, and it wraps around. A small seam marker at 6 o'clock shows where the
  list starts and ends. The ring starts at the same rotation every time the wheel opens, so
  positions stay learnable.
- Each slice shows its key (inner ring), the code, and a shortened epithet. A readout above the
  wheel shows the full scientific name of the slice under the pointer.
- Typing letters filters by code or name, as the current filter box does. Matching pinned
  species light up in the inner ring, other matches fill the outer ring from 12 o'clock, and the
  first match is highlighted. Up and Down move the highlight; Enter picks it; Backspace edits.
- The wheel floats above the page and may cover the controls around the video while it is open.
  It scales with the window and stays inside it; near an edge it shifts inward and keeps
  pointing at the mark with a short leader line.
- Holding Shift while choosing (click or key) saves and keeps the video paused, for a second
  sponge in the same frame.

Keys while the wheel is open:

| Key | Action |
|---|---|
| `1` to `9`, `0` | Pick the pinned species and play on |
| letters, Backspace | Filter the wheel |
| Up, Down, scroll | Move the highlight, spin the outer ring |
| Enter | Pick the highlighted match and play on |
| Space | Later: save unlabeled and play on |
| `,` `.` | Step one frame; the mark and the wheel stay |
| Esc, right-click | Cancel the mark and play on |
| Click outside the wheel on the picture | Move the mark there |
| Click anywhere else outside the wheel | Cancel the mark; the video stays paused |
| Tab | Go to the note box under the video (windowed view only); Enter returns to the wheel |

## Saving fast

A pick copies the paused frame into a canvas, resumes playback at once, and then encodes the
JPEG frame and PNG crop and posts them in the background. The species label stays on the
picture at the mark for 1.5 seconds while the video plays, then fades. A failed save keeps the
captured images in memory and shows an error toast with a Retry button, so nothing is lost while
the video moves on. Choosing a species anywhere (wheel, number key, the pin strip, the filter
list under the video) now saves at once; the Save and Save and stay buttons leave the bar, and
Cancel stays.

## Later points and relabeling

- A Later point is a row in `observations.csv` with a blank SpeciesCode and Sponge Type and a
  new last column, `LabelStatus`, set to `unlabeled`. Labeled rows read `labeled`. Files of a
  Later point use `LATER` in place of the code: `ID004_LATER_NE_BIT_T1.jpg`.
- Clicking a saved mark on the paused picture opens the same wheel for that row. Picking a
  species relabels it; the hub reads Unlabel for a labeled row and Keep for a Later row, and
  either choice plays on. A click outside this wheel drops the edit and starts a new mark there.
  Relabeling renames the frame and crop to the new code and rewrites the row; the ID and
  LoggedAt stay.
- A new To label list in the right panel shows every Later point across all videos, newest
  first, with the video, the time, and the crop. Clicking one opens that video, seeks to the
  point, and opens the wheel on it.
- Later marks draw as an amber dashed ring with a `?` tag; labeled marks keep today's style.
- The tally and the export count labeled rows only. The export leaves Later points out and its
  README states how many it left out. The Done button refuses while the video holds Later
  points, because a done video tells OnDeck that missing pinned species are absent.
- An observations.csv with the current 22-column header still loads; its rows read as labeled,
  and the next write adds the `LabelStatus` column.

## Full screen and speed

- A full-screen button in the transport row and the F key put the stage and the transport row
  (scrub bar, play, time, speed, grid, Done, Next) in full screen. The wheel, the marks, and the
  toasts work there. The pin strip, the note box, and the side panels stay in the windowed view.
- In full screen the page asks Chrome for the Escape key, so Esc cancels the wheel; holding Esc
  leaves full screen, and F leaves it at once.
- New installs and the first start after this change play at 1.5x. The speed steps stay 0.5x to
  3x, and the app remembers the speed Lauren picks after that.

## Server and data changes

- `POST /api/observations` accepts a null species and saves a Later point.
- `PATCH /api/observations/<ID>` takes `{species: <code or null>}`, relabels or unlabels the row,
  renames its files under the file lock, and answers with the row. Unknown IDs, unknown codes,
  and a missing mutation header are refused with a message that names the field.
- `GET /api/observations?status=unlabeled` lists Later points across all videos.
- `GET /api/tally` adds the count of Later points.

## Testing and review

- Unit (node): wheel layout for 8 and 10 inner slices, pointer-to-zone hit testing at every
  boundary, the 16-slot outer window with wrap and spin in both directions, filter placement,
  wheel placement near each edge and corner, outer ring sort order.
- Unit and adversarial (pytest): save a Later point; relabel; unlabel; relabel to the same code;
  unknown ID; unknown code; a code with `../`; relabel while another writer holds the lock;
  renamed files exist and old ones are gone; tally and export skip Later points; Done refused
  with Later points; the old 22-column header loads and upgrades; a hand-edited LabelStatus value
  outside the two allowed values fails loudly with the line number.
- Server: the PATCH route, its validation, and the mutation header check.
- End to end in headless Chrome against a local test video: click, pick from each ring, confirm
  the row and playback resumed; Later then relabel from the To label list; full screen on and off.
- Pixel: screenshots at 946 and 1280 px and in full screen, with the wheel open, read against
  the seven questions.
- One independent reviewer agent per pass, two passes at most.

## Outside this round

Notes typed inside the wheel, sorting the outer ring by how often a species is used, and a
Delete slice in the wheel (rows keep their delete control, and Z still removes the last
sighting).

## Revision after first use (2026-10-13)

Lauren asked for two changes after trying the wheel:

- A pick saves and leaves the video paused on that frame; Space plays on. Holding Shift with a
  pick saves and plays on in one step. Escape and right-click still cancel and play on.
- The panel under the video (pinned species keys, pin controls, note box) is hidden. P or the
  panel button in the transport row shows it again, and the app remembers the choice.
