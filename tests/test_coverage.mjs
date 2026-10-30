// Tests for static/coverage.js: the watch tracker and the wording of the coverage grid.
// Run with: node --test tests/test_coverage.mjs
//
// The project has no package.json, so the test imports the module through a data: URL,
// as the geometry and wheel tests do.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, '..', 'static', 'coverage.js'), 'utf8');
const { MAX_STEP_SECONDS, createWatchTracker, gapSentence, targetCell, videoLabel, watchedText } = await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
);

test('steady playback becomes one range', () => {
  const tracker = createWatchTracker();
  for (const time of [1, 1.25, 1.5, 1.75]) {
    tracker.advance(time, true);
  }
  assert.deepEqual(tracker.takeRanges(), [[1, 1.75]]);
});

test('taking the ranges empties the tracker and the run carries on from the same spot', () => {
  const tracker = createWatchTracker();
  tracker.advance(1, true);
  tracker.advance(2, true);
  assert.deepEqual(tracker.takeRanges(), [[1, 2]]);
  assert.deepEqual(tracker.takeRanges(), []);
  tracker.advance(3, true);
  assert.deepEqual(tracker.takeRanges(), [[2, 3]]);
});

test('time that does not count breaks the run', () => {
  const tracker = createWatchTracker();
  tracker.advance(1, true);
  tracker.advance(2, true);
  tracker.advance(3, false);
  tracker.advance(4, true);
  tracker.advance(5, true);
  assert.deepEqual(tracker.takeRanges(), [[1, 2], [4, 5]]);
});

test('a seek breaks the run, so the jump is never counted', () => {
  const tracker = createWatchTracker();
  tracker.advance(1, true);
  tracker.advance(2, true);
  tracker.breakRun();
  tracker.advance(50, true);
  tracker.advance(51, true);
  assert.deepEqual(tracker.takeRanges(), [[1, 2], [50, 51]]);
});

test('a jump larger than one step is not counted even with no seek event', () => {
  const tracker = createWatchTracker();
  tracker.advance(1, true);
  tracker.advance(1 + MAX_STEP_SECONDS + 1, true);
  tracker.advance(1 + MAX_STEP_SECONDS + 2, true);
  assert.deepEqual(tracker.takeRanges(), [[1 + MAX_STEP_SECONDS + 1, 1 + MAX_STEP_SECONDS + 2]]);
});

test('moving backward is not counted', () => {
  const tracker = createWatchTracker();
  tracker.advance(10, true);
  tracker.advance(9, true);
  tracker.advance(9.5, true);
  assert.deepEqual(tracker.takeRanges(), [[9, 9.5]]);
});

test('a single reading is not a range', () => {
  const tracker = createWatchTracker();
  tracker.advance(1, true);
  assert.deepEqual(tracker.takeRanges(), []);
});

test('a bad clock reading breaks the run and is not stored', () => {
  const tracker = createWatchTracker();
  tracker.advance(1, true);
  tracker.advance(2, true);
  tracker.advance(Number.NaN, true);
  tracker.advance(3, true);
  assert.deepEqual(tracker.takeRanges(), [[1, 2]]);
});

test('restored ranges go out with the next take', () => {
  const tracker = createWatchTracker();
  tracker.restore([[1, 2]]);
  tracker.advance(5, true);
  tracker.advance(6, true);
  assert.deepEqual(tracker.takeRanges(), [[1, 2], [5, 6]]);
});

test('stretches that touch come out joined after failed saves', () => {
  const tracker = createWatchTracker();
  tracker.restore([[0, 5], [5, 10]]);
  tracker.restore([[10, 15]]);
  assert.deepEqual(tracker.takeRanges(), [[0, 15]]);
});

test('a held arrow key plays faster than any speed on the control, so a scrub never counts', async () => {
  const load = async (name) => import(
    `data:text/javascript;base64,${readFileSync(path.join(here, '..', 'static', name)).toString('base64')}`
  );
  const { SCRUB_RATE } = await load('scrub.js');
  const { SPEEDS } = await load('geometry.js');
  assert.ok(SCRUB_RATE > Math.max(...SPEEDS));
});

test('a tagged species shows its count', () => {
  const cell = targetCell(3, 7, { percent: 40 }, false);
  assert.equal(cell.state, 'tagged');
  assert.equal(cell.text, '3');
  assert.equal(cell.reference, 'J7');
});

test('zero tags with a January sighting is flagged', () => {
  assert.equal(targetCell(0, 2, { percent: 100 }, true).state, 'missing');
});

test('zero tags reads absent only on a done, fully watched video', () => {
  assert.equal(targetCell(0, 0, { percent: 100 }, true).state, 'absent');
  assert.equal(targetCell(0, 0, { percent: 99 }, true).state, 'open');
  assert.equal(targetCell(0, 0, { percent: 100 }, false).state, 'open');
  assert.equal(targetCell(0, 0, { percent: null }, true).state, 'open');
});

test('no reference file leaves the January number out', () => {
  assert.equal(targetCell(3, null, { percent: 40 }, false).reference, '');
});

test('a video label is the site and transect', () => {
  assert.equal(videoLabel('TCRMP20241018_video_BIT_T1.MP4'), 'BIT T1');
  assert.equal(videoLabel('odd name.mp4'), 'odd name.mp4');
});

test('watched text', () => {
  assert.equal(watchedText({ percent: null, watched_seconds: 0 }), 'no record');
  assert.equal(watchedText({ percent: 42, watched_seconds: 10 }), '42%');
});

test('the gap sentence names the time left and each stretch', () => {
  assert.equal(
    gapSentence([[0, 10], [40, 70]]),
    'You have not watched 00:40 of this video: 00:00 to 00:10, 00:40 to 01:10.',
  );
});

test('the gap sentence shortens a long list', () => {
  const gaps = Array.from({ length: 6 }, (_, index) => [index * 100, index * 100 + 10]);
  assert.match(gapSentence(gaps), /and 3 more\.$/);
});
