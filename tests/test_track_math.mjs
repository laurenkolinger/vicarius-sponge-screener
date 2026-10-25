// Tests for static/track_math.js: where a tracked sponge sits at a video time, which marks
// the overlay draws, which drawn mark a click hits, and how the page reads the tracks answer.
//
// Run with: node --test tests/test_track_math.mjs
//
// The project has no package.json, so node would read static/*.js as CommonJS and refuse
// their `export` statements. The test loads geometry.js through a data: URL, points the
// import line of track_math.js at that URL, and loads track_math.js the same way.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.join(here, '..', 'static');

/**
 * Turn module text into a data: URL that node imports as an ES module.
 * @param {string} text The module source.
 * @returns {string} The data: URL.
 */
function moduleUrl(text) {
  return `data:text/javascript;base64,${Buffer.from(text, 'utf8').toString('base64')}`;
}

const geometryUrl = moduleUrl(readFileSync(path.join(staticDir, 'geometry.js'), 'utf8'));
const trackSource = readFileSync(path.join(staticDir, 'track_math.js'), 'utf8');
assert.ok(trackSource.includes("from './geometry.js'"), 'track_math.js imports ./geometry.js');
const t = await import(moduleUrl(trackSource.split("from './geometry.js'").join(`from '${geometryUrl}'`)));

/** A track that moves right by 0.1 of the width per second from 10 s to 11 s, with a 1 s gap after. */
const TRACK = {
  points: [
    [10.0, 0.2, 0.5, 0.9],
    [10.5, 0.25, 0.55, 1.0],
    [11.0, 0.3, 0.6, 0.8],
    [12.0, 0.4, 0.6, 0.7],
  ],
  box: null,
  anchor_time: 10.5,
  updated_at: '2026-10-13T12:00:00Z',
};

/**
 * Build an observation row the way the server sends it, as text cells.
 * @param {string} id The ID.
 * @param {string} seconds The TimestampSeconds cell.
 * @param {object} [extra] Cells to change.
 * @returns {Object<string, string>} The row.
 */
function makeRow(id, seconds, extra = {}) {
  return {
    ID: id, SpeciesCode: 'ACAU', TimestampSeconds: seconds, LabelStatus: 'labeled',
    PointX: '0.2500', PointY: '0.5500', BoxX: '', BoxY: '', BoxW: '', BoxH: '',
    ...extra,
  };
}

/**
 * Compare two numbers within a millionth.
 * @param {number} actual The value found.
 * @param {number} expected The value wanted.
 * @param {string} what What the value is, for the message.
 */
function near(actual, expected, what) {
  assert.ok(Math.abs(actual - expected) < 1e-9, `${what}: ${actual} is not ${expected}`);
}

// ---------------------------------------------------------------- positionAt

test('positionAt answers the points themselves at their times', () => {
  for (const [time, x, y] of TRACK.points) {
    assert.deepEqual(t.positionAt(TRACK, time), { x, y }, `at ${time} s`);
  }
});

test('positionAt interpolates linearly between two points', () => {
  const at = t.positionAt(TRACK, 10.25);
  near(at.x, 0.225, 'x at 10.25 s');
  near(at.y, 0.525, 'y at 10.25 s');
  const later = t.positionAt(TRACK, 10.9);
  near(later.x, 0.29, 'x at 10.9 s');
  near(later.y, 0.59, 'y at 10.9 s');
});

test('positionAt is null outside the track', () => {
  assert.equal(t.positionAt(TRACK, 9.999), null);
  assert.equal(t.positionAt(TRACK, 12.001), null);
  assert.equal(t.positionAt(TRACK, 0), null);
});

test('positionAt is null across a gap longer than half a second, and joins a gap of exactly that', () => {
  assert.equal(t.TRACK_GAP_SECONDS, 0.5);
  assert.equal(t.positionAt(TRACK, 11.5), null, 'the 1 s gap between 11 s and 12 s is not joined');
  assert.equal(t.positionAt(TRACK, 11.0001), null);
  near(t.positionAt(TRACK, 10.75).x, 0.275, 'x inside a 0.5 s step');
});

test('positionAt covers only the moment of a one-point track', () => {
  const single = { points: [[7.5, 0.4, 0.3, 1]], box: null };
  assert.deepEqual(t.positionAt(single, 7.5), { x: 0.4, y: 0.3 });
  assert.equal(t.positionAt(single, 7.49), null);
  assert.equal(t.positionAt(single, 7.51), null);
});

test('positionAt finds the right step in a long track', () => {
  const points = Array.from({ length: 400 }, (_, index) => [index / 15, index / 400, 0.5, 1]);
  const at = t.positionAt({ points, box: null }, 200.5 / 15);
  near(at.x, 200.5 / 400, 'x halfway between point 200 and point 201');
});

test('positionAt refuses a track without points and a time that is not a finite number', () => {
  for (const track of [null, undefined, 5, 'track', {}, { points: 'abc' }, { points: [] }]) {
    assert.throws(() => t.positionAt(track, 10), (error) => error instanceof TypeError && /^positionAt: track/.test(error.message), JSON.stringify(track));
  }
  for (const time of [NaN, Infinity, '10', null, undefined]) {
    assert.throws(() => t.positionAt(TRACK, time), (error) => error instanceof TypeError && /^positionAt: time/.test(error.message), String(time));
  }
});

// ------------------------------------------------------------------- marksAt

test('marksAt draws a tracked row at its track position, wherever the sighting time is', () => {
  const rows = [makeRow('ID001', '10.500')];
  const marks = t.marksAt(rows, { ID001: TRACK }, 11.0, 0.6);
  assert.deepEqual(marks, [{ id: 'ID001', code: 'ACAU', status: 'labeled', point: { x: 0.3, y: 0.6 }, box: null, tracked: true }]);
  assert.deepEqual(t.marksAt(rows, { ID001: TRACK }, 12.0, 0.6)[0].point, { x: 0.4, y: 0.6 }, 'far from the sighting time');
});

test('marksAt moves a box with its track and keeps its size', () => {
  const rows = [makeRow('ID002', '10.500', { PointX: '0.2500', PointY: '0.5500', BoxX: '0.2000', BoxY: '0.5000', BoxW: '0.1000', BoxH: '0.1000' })];
  const [mark] = t.marksAt(rows, { ID002: { ...TRACK, box: { w: 0.1, h: 0.1 } } }, 11.0, 0.6);
  assert.deepEqual(mark.point, { x: 0.3, y: 0.6 });
  near(mark.box.x, 0.25, 'box x');
  near(mark.box.y, 0.55, 'box y');
  assert.equal(mark.box.w, 0.1);
  assert.equal(mark.box.h, 0.1);
  assert.equal(mark.tracked, true);
});

test('marksAt keeps the 0.6 s rule for a row without a track or outside its track', () => {
  const rows = [makeRow('ID001', '10.500'), makeRow('ID002', '30.000'), makeRow('ID003', '12.400')];
  const tracks = { ID001: TRACK };
  assert.deepEqual(t.marksAt(rows, tracks, 30.5, 0.6).map((mark) => [mark.id, mark.tracked, mark.point]), [
    ['ID002', false, { x: 0.25, y: 0.55 }],
  ]);
  assert.deepEqual(t.marksAt(rows, tracks, 12.5, 0.6).map((mark) => [mark.id, mark.tracked]), [['ID003', false]],
    'ID001 is past its track and far from its sighting');
  assert.deepEqual(t.marksAt(rows, tracks, 11.5, 0.6).map((mark) => mark.id), [],
    'inside the gap of ID001 nothing is drawn, and ID003 is more than 0.6 s away');
});

test('marksAt passes the label status of a Later point and leaves out rows with unreadable cells', () => {
  const rows = [
    makeRow('ID001', '10.500', { SpeciesCode: '', LabelStatus: 'unlabeled' }),
    makeRow('ID002', '10.500', { PointX: 'oops' }),
    makeRow('ID003', '', {}),
  ];
  const marks = t.marksAt(rows, { ID001: TRACK, ID002: TRACK, ID003: TRACK }, 10.5, 0.6);
  assert.deepEqual(marks.map((mark) => [mark.id, mark.status, mark.tracked]), [['ID001', 'unlabeled', true]]);
});

test('marksAt draws nothing at a time that is not a finite number', () => {
  assert.deepEqual(t.marksAt([makeRow('ID001', '10.500')], { ID001: TRACK }, NaN, 0.6), []);
});

test('marksAt refuses rows that are not a list and tracks that are not an object', () => {
  assert.throws(() => t.marksAt(null, {}, 10, 0.6), (error) => error instanceof TypeError && /^marksAt: rows/.test(error.message));
  assert.throws(() => t.marksAt([], null, 10, 0.6), (error) => error instanceof TypeError && /^marksAt: tracks/.test(error.message));
  assert.throws(() => t.marksAt([], [], 10, 0.6), (error) => error instanceof TypeError && /^marksAt: tracks/.test(error.message));
});

// --------------------------------------------------------------- nearestMark

test('nearestMark finds the drawn mark nearest a click within the reach', () => {
  const drawn = [{ id: 'ID001', x: 100, y: 100 }, { id: 'ID002', x: 110, y: 100 }, { id: 'ID003', x: 300, y: 300 }];
  assert.equal(t.nearestMark(drawn, { x: 104, y: 100 }, 14), 'ID001');
  assert.equal(t.nearestMark(drawn, { x: 107, y: 100 }, 14), 'ID002');
  assert.equal(t.nearestMark(drawn, { x: 300, y: 314 }, 14), 'ID003', 'exactly at the reach counts');
  assert.equal(t.nearestMark(drawn, { x: 300, y: 315 }, 14), null);
  assert.equal(t.nearestMark([], { x: 0, y: 0 }, 14), null);
});

test('nearestMark refuses drawn marks that are not a list, a click without numbers, and a negative reach', () => {
  assert.throws(() => t.nearestMark(null, { x: 0, y: 0 }, 14), (error) => error instanceof TypeError && /^nearestMark: drawn/.test(error.message));
  assert.throws(() => t.nearestMark([], { x: 'a', y: 0 }, 14), (error) => error instanceof TypeError && /^nearestMark: spot/.test(error.message));
  assert.throws(() => t.nearestMark([], { x: 0, y: 0 }, -1), (error) => error instanceof TypeError && /^nearestMark: reach/.test(error.message));
});

// ----------------------------------------------------------- readTrackAnswer

test('readTrackAnswer keeps well-formed tracks and the pending IDs', () => {
  const answer = { tracks: { ID001: TRACK }, pending: ['ID002', 'ID003'] };
  assert.deepEqual(t.readTrackAnswer(answer), { tracks: { ID001: TRACK }, pending: ['ID002', 'ID003'] });
});

test('readTrackAnswer leaves out malformed tracks and pending entries, so one bad file cannot break drawing', () => {
  const answer = {
    tracks: {
      ID001: TRACK,
      ID002: { points: [] },
      ID003: { points: [[2, 0.5, 0.5, 1], [1, 0.5, 0.5, 1]] },
      ID004: { points: [[1, 0.5, 'x', 1]] },
      ID005: null,
      ID006: { points: [[1, 0.5, 0.5]] },
    },
    pending: ['ID007', 7, null],
  };
  assert.deepEqual(t.readTrackAnswer(answer), { tracks: { ID001: TRACK }, pending: ['ID007'] });
});

test('readTrackAnswer reads an answer without pending, or without anything, as nothing to wait for', () => {
  assert.deepEqual(t.readTrackAnswer({ tracks: { ID001: TRACK } }), { tracks: { ID001: TRACK }, pending: [] });
  for (const answer of [null, undefined, {}, { tracks: [] }, { tracks: 'x', pending: 'ID001' }, 5]) {
    assert.deepEqual(t.readTrackAnswer(answer), { tracks: {}, pending: [] }, JSON.stringify(answer));
  }
});

// ------------------------------------------------------------------- markAt

test('markAt takes a click on a mark tag or inside its box, even far from the center', () => {
  const drawn = [
    { id: 'ID001', x: 100, y: 100, areas: [{ x: 112, y: 68, w: 70, h: 20 }] },
    { id: 'ID002', x: 300, y: 300, areas: [{ x: 312, y: 268, w: 70, h: 20 }, { x: 250, y: 250, w: 100, h: 100 }] },
  ];
  assert.equal(t.markAt(drawn, { x: 170, y: 78 }, 26), 'ID001', 'a click on the tag 70 px away');
  assert.equal(t.markAt(drawn, { x: 255, y: 345 }, 26), 'ID002', 'a click in a box corner');
  assert.equal(t.markAt(drawn, { x: 120, y: 100 }, 26), 'ID001', 'within the reach of the ring and ticks');
  assert.equal(t.markAt(drawn, { x: 140, y: 140 }, 26), null);
});

test('markAt prefers a mark whose tag or box holds the click over a nearer bare center', () => {
  const drawn = [
    { id: 'NEAR', x: 210, y: 100, areas: [] },
    { id: 'TAGGED', x: 150, y: 120, areas: [{ x: 190, y: 92, w: 60, h: 20 }] },
  ];
  assert.equal(t.markAt(drawn, { x: 200, y: 100 }, 26), 'TAGGED');
});

test('markAt keeps working for marks without areas, and refuses bad input', () => {
  assert.equal(t.markAt([{ id: 'ID009', x: 10, y: 10 }], { x: 30, y: 10 }, 26), 'ID009');
  assert.throws(() => t.markAt(null, { x: 0, y: 0 }, 26), /drawn/);
  assert.throws(() => t.markAt([], { x: 'a', y: 0 }, 26), /spot/);
  assert.throws(() => t.markAt([], { x: 0, y: 0 }, -1), /reach/);
});
