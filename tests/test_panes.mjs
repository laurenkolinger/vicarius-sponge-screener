// Tests for static/panes.js: the limits on pane sizes and reading saved sizes.
// Run with: node --test tests/test_panes.mjs

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, '..', 'static', 'panes.js'), 'utf8');
const { PANES, clampPane, paneMax, readPaneSizes } = await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
);

const room = { width: 1280, logHeight: 750, sizes: { left: 240, right: 250, guide: 330, review: 100, tally: 200 } };

test('a pane cannot shrink below its smallest size', () => {
  assert.equal(clampPane('left', 10, room), PANES.left.min);
  assert.equal(clampPane('tally', 1, room), PANES.tally.min);
});

test('the video keeps its room when a side pane grows', () => {
  assert.equal(paneMax('left', room), 1280 - 260 - 250 - 330);
  assert.equal(clampPane('left', 5000, room), 440);
});

test('the guide can take the room the video gives up', () => {
  assert.equal(clampPane('guide', 5000, room), 1280 - 260 - 240 - 250);
});

test('a pane stays at its smallest in a window with no room', () => {
  const tight = { width: 700, logHeight: 400, sizes: { left: 300, right: 300, guide: 300, review: 100, tally: 200 } };
  assert.equal(paneMax('left', tight), PANES.left.min);
});

test('the Review list and the tally leave room for the sightings list together', () => {
  assert.equal(clampPane('review', 5000, room), 750 - 120 - 200);
  assert.equal(clampPane('tally', 5000, room), 750 - 120 - 100);
});

test('sizes round to whole pixels', () => {
  assert.equal(clampPane('left', 200.6, room), 201);
});

test('saved sizes keep only numbers for real panes', () => {
  assert.deepEqual(readPaneSizes('{"left": 200, "bogus": 3, "right": "x", "guide": -5, "tally": 150}'), { left: 200, tally: 150 });
});

test('damaged or missing saved sizes read as none', () => {
  assert.deepEqual(readPaneSizes('{broken'), {});
  assert.deepEqual(readPaneSizes(null), {});
  assert.deepEqual(readPaneSizes('[1, 2]'), {});
});

test('each pane names the variables that hold its size', () => {
  assert.deepEqual(PANES.left.styles(200), { '--left': '200px' });
  assert.deepEqual(PANES.review.styles(100), { '--review-flex': '0 0 100px', '--review-max': 'none' });
});
