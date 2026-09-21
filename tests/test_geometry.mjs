// Tests for static/geometry.js, the pure calculations behind the Sponge Screener page.
//
// Run with: node --test /Users/laurenkay/SpongeScreener/tests/test_geometry.mjs
//
// The project has no package.json, so node would read static/geometry.js as
// CommonJS and refuse its `export` statements. The test loads the file text and
// imports it through a data: URL, which node always treats as an ES module.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(path.join(here, 'fixtures', 'geometry_cases.json'), 'utf8'),
);
const source = readFileSync(path.join(here, '..', 'static', 'geometry.js'), 'utf8');
const g = await import(
  'data:text/javascript;base64,' + Buffer.from(source, 'utf8').toString('base64')
);

const EPSILON = 1e-9;

/**
 * Assert that two numbers agree within a tiny tolerance.
 * @param {number} actual The value under test.
 * @param {number} expected The value it should equal.
 * @param {string} label What the number means, for the failure message.
 */
function assertClose(actual, expected, label) {
  assert.ok(
    Math.abs(actual - expected) < EPSILON,
    `${label}: expected ${expected}, got ${actual}`,
  );
}

/**
 * Assert that a rectangle-like object matches the expected fields within tolerance.
 * @param {object} actual The object under test.
 * @param {object} expected Field names and expected numbers.
 */
function assertFieldsClose(actual, expected) {
  assert.deepEqual(Object.keys(actual).sort(), Object.keys(expected).sort());
  for (const field of Object.keys(expected)) {
    assertClose(actual[field], expected[field], field);
  }
}

const SPECIES = [
  { code: 'ACAU', name: 'Aplysina cauliformis', part: '1' },
  { code: 'CDEL', name: 'Cliona delitrix', part: '1' },
  { code: 'MLAE', name: 'Mycale laevis', part: '1' },
  { code: 'CPLI', name: 'Callyspongia plicifera', part: '1' },
  { code: 'XMUT', name: 'Xestospongia muta', part: '2' },
  { code: 'DANC', name: 'Desmapsamma anchorata', part: '3' },
  { code: 'UNKN', name: 'Unknown sponge', part: '' },
];

const PINS = {
  1: 'ACAU', 2: 'CDEL', 3: 'MLAE', 4: null, 5: null,
  6: null, 7: null, 8: 'XMUT', 9: null, 0: null,
};

/**
 * Build an observation row the way the server returns it (every cell a string).
 * @param {string} id The ID cell, such as "ID007".
 * @param {string} seconds The TimestampSeconds cell.
 * @param {object} [extra] Cells that replace the defaults.
 * @returns {object} The row.
 */
function makeRow(id, seconds, extra = {}) {
  return {
    ID: id, SpeciesCode: 'ACAU', TimestampSeconds: seconds,
    PointX: '0.2500', PointY: '0.7500', BoxX: '', BoxY: '', BoxW: '', BoxH: '',
    ...extra,
  };
}

// ---------------------------------------------------------------- quadrantOf

test('quadrantOf matches every shared fixture case', () => {
  assert.ok(fixture.quadrant.length >= 10);
  for (const item of fixture.quadrant) {
    assert.equal(g.quadrantOf(item.x, item.y), item.expect, `x=${item.x} y=${item.y}`);
  }
});

test('quadrantOf rejects values that are not finite numbers', () => {
  assert.throws(() => g.quadrantOf(NaN, 0.5), TypeError);
  assert.throws(() => g.quadrantOf(0.5, Infinity), TypeError);
  assert.throws(() => g.quadrantOf('0.5', 0.5), TypeError);
});

test('QUADRANT_PHRASES names the four quadrants in plain words', () => {
  assert.deepEqual(g.QUADRANT_PHRASES, {
    TOPLEFT: 'top left', TOPRIGHT: 'top right',
    BOTTOMLEFT: 'bottom left', BOTTOMRIGHT: 'bottom right',
  });
});

// --------------------------------------------------------------- formatClock

test('formatClock matches every shared fixture case', () => {
  assert.ok(fixture.clock.length >= 9);
  for (const item of fixture.clock) {
    assert.equal(g.formatClock(item.seconds), item.expect, `seconds=${item.seconds}`);
  }
});

test('formatClock shows a blank clock for unknown, negative, or infinite time', () => {
  assert.equal(g.formatClock(NaN), '--:--');
  assert.equal(g.formatClock(-1), '--:--');
  assert.equal(g.formatClock(Infinity), '--:--');
  assert.equal(g.formatClock(undefined), '--:--');
  assert.equal(g.formatClock('12'), '--:--');
});

// --------------------------------------------------------------- contentRect

test('contentRect letterboxes a video that is wider than the element', () => {
  assertFieldsClose(g.contentRect(800, 600, 1920, 1080), { x: 0, y: 75, w: 800, h: 450 });
});

test('contentRect pillarboxes a video that is taller than the element', () => {
  assertFieldsClose(g.contentRect(800, 600, 720, 960), { x: 175, y: 0, w: 450, h: 600 });
});

test('contentRect fills the element when the aspect ratios are equal', () => {
  assertFieldsClose(g.contentRect(640, 360, 1280, 720), { x: 0, y: 0, w: 640, h: 360 });
});

test('contentRect returns an empty rectangle before the video has a size', () => {
  const empty = { x: 0, y: 0, w: 0, h: 0 };
  assert.deepEqual(g.contentRect(800, 600, 0, 0), empty);
  assert.deepEqual(g.contentRect(0, 600, 1920, 1080), empty);
  assert.deepEqual(g.contentRect(800, 600, NaN, 1080), empty);
  assert.deepEqual(g.contentRect(800, -5, 1920, 1080), empty);
  assert.deepEqual(g.contentRect(800, 600, '1920', 1080), empty);
});

// -------------------------------------------------------------- toNormalized

test('toNormalized maps a point inside the picture to fractions', () => {
  const rect = { x: 0, y: 75, w: 800, h: 450 };
  assertFieldsClose(g.toNormalized(400, 300, rect), { x: 0.5, y: 0.5 });
  assertFieldsClose(g.toNormalized(200, 412.5, rect), { x: 0.25, y: 0.75 });
});

test('toNormalized accepts points exactly on the picture edge', () => {
  const rect = { x: 0, y: 75, w: 800, h: 450 };
  assertFieldsClose(g.toNormalized(0, 75, rect), { x: 0, y: 0 });
  assertFieldsClose(g.toNormalized(800, 525, rect), { x: 1, y: 1 });
});

test('toNormalized returns null in the letterbox bars and for an empty rectangle', () => {
  const rect = { x: 0, y: 75, w: 800, h: 450 };
  assert.equal(g.toNormalized(400, 10, rect), null);
  assert.equal(g.toNormalized(400, 590, rect), null);
  assert.equal(g.toNormalized(-1, 300, rect), null);
  assert.equal(g.toNormalized(801, 300, rect), null);
  assert.equal(g.toNormalized(10, 10, { x: 0, y: 0, w: 0, h: 0 }), null);
  assert.equal(g.toNormalized(NaN, 10, rect), null);
});

test('toNormalizedClamped pulls an outside point onto the picture edge', () => {
  const rect = { x: 100, y: 50, w: 400, h: 200 };
  assertFieldsClose(g.toNormalizedClamped(-40, 900, rect), { x: 0, y: 1 });
  assertFieldsClose(g.toNormalizedClamped(900, -3, rect), { x: 1, y: 0 });
  assertFieldsClose(g.toNormalizedClamped(300, 150, rect), { x: 0.5, y: 0.5 });
  assert.equal(g.toNormalizedClamped(10, 10, { x: 0, y: 0, w: 0, h: 0 }), null);
  assert.equal(g.toNormalizedClamped(NaN, 10, rect), null);
});

test('toPixels is the inverse of toNormalized', () => {
  const rect = { x: 100, y: 50, w: 400, h: 200 };
  assertFieldsClose(g.toPixels({ x: 0.5, y: 0.25 }, rect), { x: 300, y: 100 });
  const back = g.toNormalized(300, 100, rect);
  assertFieldsClose(back, { x: 0.5, y: 0.25 });
});

// -------------------------------------------------------------------- isDrag

test('isDrag is false at or below the threshold and true past it', () => {
  assert.equal(g.isDrag(10, 10, 13, 14), false); // 5 px
  assert.equal(g.isDrag(0, 0, 6, 0), false); // exactly 6 px is still a click
  assert.equal(g.isDrag(0, 0, 7, 0), true);
  assert.equal(g.isDrag(0, 0, 5, 5), true); // 7.07 px along the diagonal
  assert.equal(g.isDrag(50, 50, 50, 50), false);
});

test('isDrag honors a custom threshold', () => {
  assert.equal(g.isDrag(0, 0, 10, 0, 12), false);
  assert.equal(g.isDrag(0, 0, 13, 0, 12), true);
});

// -------------------------------------------------------------- normalizeBox

test('normalizeBox orders reversed corners', () => {
  assertFieldsClose(
    g.normalizeBox({ x: 0.8, y: 0.7 }, { x: 0.2, y: 0.1 }),
    { x: 0.2, y: 0.1, w: 0.6, h: 0.6 },
  );
});

test('normalizeBox clamps corners that fall outside the picture', () => {
  assertFieldsClose(
    g.normalizeBox({ x: -0.2, y: 0.5 }, { x: 0.5, y: 1.4 }),
    { x: 0, y: 0.5, w: 0.5, h: 0.5 },
  );
  assertFieldsClose(
    g.normalizeBox({ x: 1.7, y: -3 }, { x: 0.9, y: 0.25 }),
    { x: 0.9, y: 0, w: 0.1, h: 0.25 },
  );
});

test('normalizeBox rejects a corner without finite x and y', () => {
  assert.throws(() => g.normalizeBox({ x: 0.1 }, { x: 0.2, y: 0.3 }), TypeError);
  assert.throws(() => g.normalizeBox(null, { x: 0.2, y: 0.3 }), TypeError);
  assert.throws(() => g.normalizeBox({ x: 0.1, y: NaN }, { x: 0.2, y: 0.3 }), TypeError);
});

test('isUsableBox refuses boxes that are missing or thinner than the minimum', () => {
  assert.equal(g.isUsableBox({ x: 0.1, y: 0.1, w: 0.2, h: 0.2 }), true);
  assert.equal(g.isUsableBox({ x: 0.1, y: 0.1, w: 0, h: 0.2 }), false);
  assert.equal(g.isUsableBox({ x: 0.1, y: 0.1, w: 0.2, h: 0.001 }), false);
  assert.equal(g.isUsableBox(null), false);
  assert.equal(g.isUsableBox({ x: 0.1, y: 0.1, w: 0.03, h: 0.03 }, 0.05), false);
});

test('anchorPoint is the point for a click and the box center for a box', () => {
  assertFieldsClose(g.anchorPoint({ x: 0.2, y: 0.3 }, null), { x: 0.2, y: 0.3 });
  assertFieldsClose(
    g.anchorPoint({ x: 0.2, y: 0.3 }, { x: 0.5, y: 0.5, w: 0.25, h: 0.5 }),
    { x: 0.625, y: 0.75 },
  );
});

test('quadrantRect returns the quarter of the picture that a quadrant covers', () => {
  const rect = { x: 10, y: 20, w: 400, h: 200 };
  assertFieldsClose(g.quadrantRect('TOPLEFT', rect), { x: 10, y: 20, w: 200, h: 100 });
  assertFieldsClose(g.quadrantRect('TOPRIGHT', rect), { x: 210, y: 20, w: 200, h: 100 });
  assertFieldsClose(g.quadrantRect('BOTTOMLEFT', rect), { x: 10, y: 120, w: 200, h: 100 });
  assertFieldsClose(g.quadrantRect('BOTTOMRIGHT', rect), { x: 210, y: 120, w: 200, h: 100 });
  assert.throws(() => g.quadrantRect('MIDDLE', rect), RangeError);
});

test('tagPosition puts the tag up and to the right of a point, clear of the ring', () => {
  const rect = { x: 0, y: 100, w: 800, h: 450 };
  const spot = g.tagPosition(rect, { x: 400, y: 300 }, null, 60, 20, 11);
  assert.deepEqual(spot, { x: 423, y: 257 });
});

test('tagPosition flips to the left and below when the point sits in the top right corner', () => {
  const rect = { x: 0, y: 100, w: 800, h: 450 };
  const spot = g.tagPosition(rect, { x: 790, y: 108 }, null, 60, 20, 11);
  assert.deepEqual(spot, { x: 790 - 11 - 12 - 60, y: 108 + 11 + 12 });
  const ring = { left: 790 - 11, right: 790 + 11, top: 108 - 11, bottom: 108 + 11 };
  const overlaps = spot.x < ring.right && spot.x + 60 > ring.left && spot.y < ring.bottom && spot.y + 20 > ring.top;
  assert.equal(overlaps, false, 'the tag never covers the ring');
});

test('tagPosition keeps the tag inside the picture in every corner', () => {
  const rect = { x: 50, y: 100, w: 800, h: 450 };
  for (const center of [{ x: 50, y: 100 }, { x: 850, y: 100 }, { x: 50, y: 550 }, { x: 850, y: 550 }]) {
    const spot = g.tagPosition(rect, center, null, 90, 20, 11);
    assert.ok(spot.x >= rect.x && spot.x + 90 <= rect.x + rect.w, `x for ${JSON.stringify(center)}`);
    assert.ok(spot.y >= rect.y && spot.y + 20 <= rect.y + rect.h, `y for ${JSON.stringify(center)}`);
  }
});

test('tagPosition sits above a box, or inside its top edge when the box touches the top', () => {
  const rect = { x: 0, y: 100, w: 800, h: 450 };
  const above = g.tagPosition(rect, { x: 300, y: 300 }, { x: 200, y: 250, w: 200, h: 100 }, 60, 20, 11);
  assert.deepEqual(above, { x: 200, y: 250 - 20 - 6 });
  const inside = g.tagPosition(rect, { x: 300, y: 150 }, { x: 200, y: 102, w: 200, h: 100 }, 60, 20, 11);
  assert.deepEqual(inside, { x: 206, y: 108 });
  const farRight = g.tagPosition(rect, { x: 780, y: 300 }, { x: 770, y: 250, w: 30, h: 100 }, 60, 20, 11);
  assert.equal(farRight.x, 800 - 60, 'a tag wider than the room to its right slides left');
});

// ------------------------------------------------------------------ cropRect

test('cropRect centers a 512 px square on a point in the middle of the frame', () => {
  assert.deepEqual(
    g.cropRect({ x: 0.5, y: 0.5 }, null, 1280, 720),
    { sx: 384, sy: 104, sw: 512, sh: 512 },
  );
  assert.deepEqual(
    g.cropRect({ x: 0.5, y: 0.5 }, null, 1920, 1080),
    { sx: 704, sy: 284, sw: 512, sh: 512 },
  );
});

test('cropRect shifts the square to stay inside the frame at each corner', () => {
  assert.deepEqual(g.cropRect({ x: 0, y: 0 }, null, 1280, 720), { sx: 0, sy: 0, sw: 512, sh: 512 });
  assert.deepEqual(g.cropRect({ x: 1, y: 0 }, null, 1280, 720), { sx: 768, sy: 0, sw: 512, sh: 512 });
  assert.deepEqual(g.cropRect({ x: 0, y: 1 }, null, 1280, 720), { sx: 0, sy: 208, sw: 512, sh: 512 });
  assert.deepEqual(g.cropRect({ x: 1, y: 1 }, null, 1280, 720), { sx: 768, sy: 208, sw: 512, sh: 512 });
  assert.deepEqual(
    g.cropRect({ x: 0.1, y: 0.95 }, null, 1280, 720),
    { sx: 0, sy: 208, sw: 512, sh: 512 },
  );
});

test('cropRect shrinks to the frame when the frame is smaller than the crop', () => {
  assert.deepEqual(g.cropRect({ x: 0.5, y: 0.5 }, null, 320, 240), { sx: 0, sy: 0, sw: 320, sh: 240 });
  // DV video: wide enough, but only 480 rows.
  assert.deepEqual(g.cropRect({ x: 0.5, y: 0.5 }, null, 720, 480), { sx: 104, sy: 0, sw: 512, sh: 480 });
});

test('cropRect honors a custom crop size', () => {
  assert.deepEqual(
    g.cropRect({ x: 0.5, y: 0.5 }, null, 1280, 720, 100),
    { sx: 590, sy: 310, sw: 100, sh: 100 },
  );
});

test('cropRect crops the box when a box exists', () => {
  assert.deepEqual(
    g.cropRect({ x: 0.5, y: 0.5 }, { x: 0.25, y: 0.25, w: 0.5, h: 0.5 }, 1280, 720),
    { sx: 320, sy: 180, sw: 640, sh: 360 },
  );
  assert.deepEqual(
    g.cropRect({ x: 0.95, y: 0.95 }, { x: 0.9, y: 0.9, w: 0.1, h: 0.1 }, 1280, 720),
    { sx: 1152, sy: 648, sw: 128, sh: 72 },
  );
});

test('cropRect keeps a box crop inside the frame and at least one pixel wide', () => {
  const overflow = g.cropRect({ x: 0.9, y: 0.9 }, { x: 0.8, y: 0.8, w: 0.20005, h: 0.2001 }, 1280, 720);
  assert.ok(overflow.sx + overflow.sw <= 1280);
  assert.ok(overflow.sy + overflow.sh <= 720);
  const tiny = g.cropRect({ x: 1, y: 1 }, { x: 0.99999, y: 0.99999, w: 0.00001, h: 0.00001 }, 1280, 720);
  assert.deepEqual(tiny, { sx: 1279, sy: 719, sw: 1, sh: 1 });
});

test('cropRect always returns whole pixels', () => {
  const rect = g.cropRect({ x: 0.3333, y: 0.6667 }, null, 1919, 1079);
  for (const field of ['sx', 'sy', 'sw', 'sh']) {
    assert.ok(Number.isInteger(rect[field]), `${field} is ${rect[field]}`);
  }
});

test('cropRect rejects a missing frame size, a bad point, and a bad crop size', () => {
  assert.throws(() => g.cropRect({ x: 0.5, y: 0.5 }, null, 0, 720), RangeError);
  assert.throws(() => g.cropRect({ x: 0.5, y: 0.5 }, null, 1280, NaN), RangeError);
  assert.throws(() => g.cropRect({ x: NaN, y: 0.5 }, null, 1280, 720), TypeError);
  assert.throws(() => g.cropRect(null, null, 1280, 720), TypeError);
  assert.throws(() => g.cropRect({ x: 0.5, y: 0.5 }, null, 1280, 720, 0), RangeError);
  assert.throws(() => g.cropRect({ x: 0.5, y: 0.5 }, { x: 0.1, y: 0.1, w: 'a', h: 0.1 }, 1280, 720), TypeError);
});

// ------------------------------------------------------------- filterSpecies

test('filterSpecies returns the whole list for an empty or blank query', () => {
  assert.deepEqual(g.filterSpecies(SPECIES, ''), SPECIES);
  assert.deepEqual(g.filterSpecies(SPECIES, '   '), SPECIES);
  assert.notEqual(g.filterSpecies(SPECIES, ''), SPECIES, 'a copy, so callers cannot change the source list');
});

test('filterSpecies ranks code prefix, then word prefix, then substring', () => {
  const codes = (query) => g.filterSpecies(SPECIES, query).map((item) => item.code);
  assert.deepEqual(codes('c'), ['CDEL', 'CPLI', 'ACAU', 'MLAE', 'DANC']);
  assert.deepEqual(codes('mu'), ['XMUT']);
  assert.deepEqual(codes('del'), ['CDEL']);
  assert.deepEqual(codes('pl'), ['CPLI', 'ACAU']);
  assert.deepEqual(codes('an'), ['DANC']);
  assert.deepEqual(codes('zzz'), []);
});

test('filterSpecies ignores case and accepts a phrase that starts at a word', () => {
  const codes = (query) => g.filterSpecies(SPECIES, query).map((item) => item.code);
  assert.deepEqual(codes('cdel'), ['CDEL']);
  assert.deepEqual(codes('XESTO'), ['XMUT']);
  assert.deepEqual(codes('Aplysina C'), ['ACAU']);
  assert.deepEqual(codes('  mycale '), ['MLAE']);
});

test('filterSpecies leaves the input list unchanged and rejects a list that is not an array', () => {
  const before = JSON.stringify(SPECIES);
  g.filterSpecies(SPECIES, 'a');
  assert.equal(JSON.stringify(SPECIES), before);
  assert.throws(() => g.filterSpecies(null, 'a'), TypeError);
  assert.throws(() => g.filterSpecies(SPECIES, 5), TypeError);
});

test('binomialParts splits a two-word scientific name and refuses other names', () => {
  assert.deepEqual(g.binomialParts('Aplysina cauliformis'), { initial: 'A.', epithet: 'cauliformis' });
  assert.deepEqual(g.binomialParts('Xestospongia muta'), { initial: 'X.', epithet: 'muta' });
  assert.equal(g.binomialParts('Unknown sponge'), null, 'a placeholder name is not a binomial');
  assert.equal(g.binomialParts('Porifera'), null);
  assert.equal(g.binomialParts('Cliona sp. 2'), null);
  assert.equal(g.binomialParts(''), null);
  assert.equal(g.binomialParts(undefined), null);
});

// ---------------------------------------------------------------- mergeTally

test('mergeTally lists every pinned species with zeros, then other species with sightings', () => {
  const tally = [
    { code: 'ACAU', name: 'Aplysina cauliformis', sightings: 5, videos: 2, earliest_year: 2016 },
    { code: 'UNKN', name: 'Unknown sponge', sightings: 2, videos: 1, earliest_year: null },
    { code: 'DANC', name: 'Desmapsamma anchorata', sightings: 1, videos: 1, earliest_year: 2019 },
  ];
  const rows = g.mergeTally(tally, PINS, SPECIES);
  assert.deepEqual(rows.map((row) => row.code), ['ACAU', 'CDEL', 'MLAE', 'XMUT', 'DANC', 'UNKN']);
  assert.deepEqual(rows[0], {
    code: 'ACAU', name: 'Aplysina cauliformis', sightings: 5, videos: 2,
    earliest_year: 2016, pin: '1',
  });
  assert.deepEqual(rows[1], {
    code: 'CDEL', name: 'Cliona delitrix', sightings: 0, videos: 0,
    earliest_year: null, pin: '2',
  });
  assert.deepEqual(rows[3].pin, '8');
  assert.deepEqual(rows[4], {
    code: 'DANC', name: 'Desmapsamma anchorata', sightings: 1, videos: 1,
    earliest_year: 2019, pin: null,
  });
});

test('mergeTally orders pinned rows by key, with 0 after 9', () => {
  const pins = { 1: null, 2: 'XMUT', 3: null, 4: null, 5: null, 6: null, 7: null, 8: null, 9: 'CDEL', 0: 'ACAU' };
  const rows = g.mergeTally([], pins, SPECIES);
  assert.deepEqual(rows.map((row) => `${row.pin}:${row.code}`), ['2:XMUT', '9:CDEL', '0:ACAU']);
});

test('mergeTally copes with an empty tally, empty pins, and a code missing from the species list', () => {
  assert.deepEqual(g.mergeTally([], {}, SPECIES), []);
  const rows = g.mergeTally(
    [{ code: 'ZZZZ', name: 'Retired species', sightings: 1, videos: 1, earliest_year: 2001 }],
    { 1: 'QQQQ' },
    SPECIES,
  );
  assert.deepEqual(rows, [
    { code: 'QQQQ', name: 'QQQQ', sightings: 0, videos: 0, earliest_year: null, pin: '1' },
    { code: 'ZZZZ', name: 'Retired species', sightings: 1, videos: 1, earliest_year: 2001, pin: null },
  ]);
});

test('mergeTally rejects a tally or species list that is not an array', () => {
  assert.throws(() => g.mergeTally(null, PINS, SPECIES), TypeError);
  assert.throws(() => g.mergeTally([], PINS, 'ACAU'), TypeError);
});

// ---------------------------------------------------------------------- pins

test('swapPin assigns a species to an empty or occupied key', () => {
  const next = g.swapPin(PINS, '9', 'DANC');
  assert.equal(next['9'], 'DANC');
  assert.equal(PINS['9'], null, 'the input object stays unchanged');
  assert.equal(g.swapPin(PINS, '2', 'DANC')['2'], 'DANC');
});

test('swapPin swaps when the species already sits on another key, so no code repeats', () => {
  const next = g.swapPin(PINS, '1', 'XMUT');
  assert.equal(next['1'], 'XMUT');
  assert.equal(next['8'], 'ACAU');
  const moved = g.swapPin(PINS, '9', 'ACAU');
  assert.equal(moved['9'], 'ACAU');
  assert.equal(moved['1'], null);
  const values = Object.values(next).filter(Boolean);
  assert.equal(new Set(values).size, values.length);
});

test('swapPin always returns the ten keys and rejects an unknown key or an empty code', () => {
  assert.deepEqual(Object.keys(g.swapPin({}, '5', 'ACAU')).sort(), [...g.PIN_KEYS].sort());
  assert.throws(() => g.swapPin(PINS, '11', 'ACAU'), RangeError);
  assert.throws(() => g.swapPin(PINS, '1', ''), TypeError);
});

test('pinKeyFromEvent reads the number row and the number pad', () => {
  assert.equal(g.pinKeyFromEvent('1', 'Digit1'), '1');
  assert.equal(g.pinKeyFromEvent('0', 'Digit0'), '0');
  assert.equal(g.pinKeyFromEvent('7', 'Numpad7'), '7');
  assert.equal(g.pinKeyFromEvent('!', 'Digit1'), null, 'a shifted digit is not a pin key');
  assert.equal(g.pinKeyFromEvent('a', 'KeyA'), null);
  assert.equal(g.pinKeyFromEvent('12', ''), null);
  assert.equal(g.pinKeyFromEvent(undefined, undefined), null);
});

test('keyFromEvent keeps a real key value and falls back to the physical key code', () => {
  assert.equal(g.keyFromEvent('1', 'Digit1', false), '1');
  assert.equal(g.keyFromEvent('Enter', 'Enter', true), 'Enter');
  assert.equal(g.keyFromEvent('?', 'Slash', true), '?');
  assert.equal(g.keyFromEvent('', 'Digit1', false), '1');
  assert.equal(g.keyFromEvent('', 'Numpad7', false), '7');
  assert.equal(g.keyFromEvent('Unidentified', 'KeyZ', false), 'z');
  assert.equal(g.keyFromEvent('', 'NumpadEnter', false), 'Enter');
  assert.equal(g.keyFromEvent('', 'Space', false), ' ');
  assert.equal(g.keyFromEvent('', 'Comma', false), ',');
  assert.equal(g.keyFromEvent('', 'Period', false), '.');
  assert.equal(g.keyFromEvent('', 'BracketLeft', false), '[');
  assert.equal(g.keyFromEvent('', 'BracketRight', false), ']');
  assert.equal(g.keyFromEvent('', 'Slash', true), '?');
  assert.equal(g.keyFromEvent(undefined, 'Escape', false), 'Escape');
  assert.equal(g.keyFromEvent(undefined, 'ArrowLeft', false), 'ArrowLeft');
  assert.equal(g.keyFromEvent(undefined, 'Home', false), 'Home');
  assert.equal(g.keyFromEvent(undefined, 'Tab', false), 'Tab');
});

test('keyFromEvent gives an empty key for shifted digits and unknown codes', () => {
  assert.equal(g.keyFromEvent('', 'Digit1', true), '', 'Shift+1 is "!", which is not a species key');
  assert.equal(g.keyFromEvent('', 'Slash', false), '');
  assert.equal(g.keyFromEvent('', 'F5', false), '');
  assert.equal(g.keyFromEvent('', '', false), '');
  assert.equal(g.keyFromEvent(undefined, undefined, false), '');
});

test('PIN_KEYS lists the ten keys in keyboard order', () => {
  assert.deepEqual([...g.PIN_KEYS], ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0']);
});

// ------------------------------------------------------------ playback math

test('stepSpeed walks the speed list and stops at both ends', () => {
  assert.deepEqual([...g.SPEEDS], [0.5, 0.75, 1, 1.5, 2, 3]);
  assert.equal(g.stepSpeed(1, 1), 1.5);
  assert.equal(g.stepSpeed(1, -1), 0.75);
  assert.equal(g.stepSpeed(3, 1), 3);
  assert.equal(g.stepSpeed(0.5, -1), 0.5);
  assert.equal(g.stepSpeed(1.25, 1), 1.5, 'an unlisted speed snaps to the nearest listed speed first');
  assert.equal(g.stepSpeed(NaN, 1), 1.5, 'an unknown speed counts as 1x');
});

test('clampTime keeps a seek inside the video', () => {
  assert.equal(g.clampTime(-3, 240), 0);
  assert.equal(g.clampTime(500, 240), 240);
  assert.equal(g.clampTime(12.5, 240), 12.5);
  assert.equal(g.clampTime(12.5, NaN), 12.5, 'an unknown duration only blocks negative time');
  assert.equal(g.clampTime(NaN, 240), 0);
});

test('resumePosition rewinds two seconds and restarts a video that was left at its end', () => {
  assert.equal(g.resumePosition(100, 240), 98);
  assert.equal(g.resumePosition(1, 240), 0);
  assert.equal(g.resumePosition(239, 240), 0);
  assert.equal(g.resumePosition(900, 240), 0);
  assert.equal(g.resumePosition(null, 240), 0);
  assert.equal(g.resumePosition(NaN, 240), 0);
  assert.equal(g.resumePosition('57.5', 240), 55.5, 'localStorage hands back strings');
  assert.equal(g.resumePosition(100, NaN), 98);
});

// ------------------------------------------------------------ list helpers

test('formatBytes picks a readable unit', () => {
  assert.equal(g.formatBytes(0), '0 B');
  assert.equal(g.formatBytes(999), '999 B');
  assert.equal(g.formatBytes(1536), '1.5 KB');
  assert.equal(g.formatBytes(52428800), '50 MB');
  assert.equal(g.formatBytes(1288490189), '1.2 GB');
  assert.equal(g.formatBytes(-4), '');
  assert.equal(g.formatBytes('big'), '');
});

test('percentText rounds a fraction and stays between 0 and 100', () => {
  assert.equal(g.percentText(0.4237), '42%');
  assert.equal(g.percentText(1), '100%');
  assert.equal(g.percentText(7), '100%');
  assert.equal(g.percentText(-1), '0%');
  assert.equal(g.percentText(NaN), '0%');
});

test('statusLabel adds the sighting count to videos that were opened', () => {
  assert.equal(g.statusLabel('new', 0), 'new');
  assert.equal(g.statusLabel('in progress', 3), 'in progress · 3');
  assert.equal(g.statusLabel('done', 0), 'done · 0');
  assert.equal(g.statusLabel('in progress', undefined), 'in progress · 0');
  assert.equal(g.statusLabel('', 2), 'new');
});

test('splitVideoName separates the shared TCRMP lead from the part that tells videos apart', () => {
  assert.deepEqual(
    g.splitVideoName('TCRMP20241022_video_FLC_T1.MP4'),
    { lead: 'TCRMP20241022_video_', main: 'FLC_T1.MP4' },
  );
  assert.deepEqual(
    g.splitVideoName('tcrmp20160804_VIDEO_BIT_T1+T3-6.mts'),
    { lead: 'tcrmp20160804_VIDEO_', main: 'BIT_T1+T3-6.mts' },
  );
  assert.deepEqual(g.splitVideoName('MVI_0203.MOV'), { lead: '', main: 'MVI_0203.MOV' });
  assert.deepEqual(g.splitVideoName('TCRMP20241022_video_'), { lead: '', main: 'TCRMP20241022_video_' });
  assert.deepEqual(g.splitVideoName(undefined), { lead: '', main: '' });
});

test('formatLabel words the format badge from the playable flag and the conversion state', () => {
  assert.deepEqual(g.formatLabel(true, 'none'), { text: 'plays now', kind: 'plays' });
  assert.deepEqual(g.formatLabel(false, 'none'), { text: 'needs conversion', kind: 'convert' });
  assert.deepEqual(g.formatLabel(false, 'queued'), { text: 'conversion queued', kind: 'convert' });
  assert.deepEqual(g.formatLabel(false, 'running'), { text: 'converting', kind: 'convert' });
  assert.deepEqual(g.formatLabel(false, 'done'), { text: 'converted', kind: 'plays' });
  assert.deepEqual(g.formatLabel(true, 'done'), { text: 'converted', kind: 'plays' });
  assert.deepEqual(g.formatLabel(false, 'failed'), { text: 'conversion failed', kind: 'failed' });
  assert.deepEqual(g.formatLabel(true, 'failed'), { text: 'conversion failed', kind: 'failed' });
  assert.deepEqual(g.formatLabel(true, undefined), { text: 'plays now', kind: 'plays' });
});

test('queueProgress reads a prefetch answer and a conversion answer', () => {
  assert.deepEqual(g.queueProgress('prefetch', { cached: 0.25 }), { state: 'working', fraction: 0.25, message: '' });
  assert.deepEqual(g.queueProgress('prefetch', { cached: 1 }), { state: 'ready', fraction: 1, message: '' });
  assert.deepEqual(
    g.queueProgress('convert', { state: 'running', progress: 0.5, message: '' }),
    { state: 'working', fraction: 0.5, message: '' },
  );
  assert.deepEqual(
    g.queueProgress('convert', { state: 'queued', progress: 0, message: '' }),
    { state: 'working', fraction: 0, message: '' },
  );
  assert.deepEqual(
    g.queueProgress('convert', { state: 'done', progress: 1, message: '' }),
    { state: 'ready', fraction: 1, message: '' },
  );
  assert.deepEqual(
    g.queueProgress('convert', { state: 'failed', progress: 0.3, message: 'moov atom not found' }),
    { state: 'failed', fraction: 0.3, message: 'moov atom not found' },
  );
  assert.deepEqual(g.queueProgress('prefetch', {}), { state: 'working', fraction: 0, message: '' });
  assert.deepEqual(g.queueProgress('prefetch', null), { state: 'working', fraction: 0, message: '' });
  assert.throws(() => g.queueProgress('upload', {}), RangeError);
});

test('breadcrumbs splits a prefix into one crumb per folder', () => {
  const root = 'TCRMP_video_ondeck/';
  assert.deepEqual(g.breadcrumbs(root, root), [{ name: 'All videos', prefix: root }]);
  assert.deepEqual(g.breadcrumbs('TCRMP_video_ondeck/2024Annual/day 1+2/', root), [
    { name: 'All videos', prefix: root },
    { name: '2024Annual', prefix: 'TCRMP_video_ondeck/2024Annual/' },
    { name: 'day 1+2', prefix: 'TCRMP_video_ondeck/2024Annual/day 1+2/' },
  ]);
  assert.deepEqual(g.breadcrumbs('elsewhere/x/', root), [{ name: 'All videos', prefix: root }]);
  assert.deepEqual(g.breadcrumbs(undefined, root), [{ name: 'All videos', prefix: root }]);
});

test('filterByName keeps items whose name holds every search word', () => {
  const items = [
    { name: 'TCRMP20241022_video_FLC_T1.MP4' },
    { name: 'TCRMP20241022_video_FLC_T10.MP4' },
    { name: 'TCRMP20241023_video_BIT_T1.MP4' },
    { name: 'MVI_0203.MOV' },
  ];
  assert.deepEqual(g.filterByName(items, ''), items);
  assert.deepEqual(g.filterByName(items, 'flc t1').map((item) => item.name), [
    'TCRMP20241022_video_FLC_T1.MP4', 'TCRMP20241022_video_FLC_T10.MP4',
  ]);
  assert.deepEqual(g.filterByName(items, 'mov').map((item) => item.name), ['MVI_0203.MOV']);
  assert.deepEqual(g.filterByName(items, 'nothing here'), []);
  assert.throws(() => g.filterByName(null, 'x'), TypeError);
});

test('nextVideo finds the video after the current one', () => {
  const videos = [{ key: 'a' }, { key: 'b' }, { key: 'c' }];
  assert.deepEqual(g.nextVideo(videos, 'a'), { key: 'b' });
  assert.equal(g.nextVideo(videos, 'c'), null);
  assert.deepEqual(g.nextVideo(videos, 'not listed'), { key: 'a' });
  assert.deepEqual(g.nextVideo(videos, null), { key: 'a' });
  assert.equal(g.nextVideo([], 'a'), null);
});

// ------------------------------------------------------------ sighting rows

test('lastSighting picks the highest ID number, not the last in the array', () => {
  const rows = [makeRow('ID009', '5.000'), makeRow('ID1000', '2.000'), makeRow('ID010', '9.000')];
  assert.equal(g.lastSighting(rows).ID, 'ID1000');
  assert.equal(g.lastSighting([]), null);
  assert.equal(g.lastSighting([{ ID: 'garbage' }]), null);
});

test('sortRowsByTime orders by video time, then by ID, and leaves the input alone', () => {
  const rows = [makeRow('ID003', '50.000'), makeRow('ID002', '7.500'), makeRow('ID001', '50.000')];
  const sorted = g.sortRowsByTime(rows);
  assert.deepEqual(sorted.map((row) => row.ID), ['ID002', 'ID001', 'ID003']);
  assert.deepEqual(rows.map((row) => row.ID), ['ID003', 'ID002', 'ID001']);
});

test('marksNear returns the saved marks within the time window', () => {
  const rows = [
    makeRow('ID001', '10.000'),
    makeRow('ID002', '10.400', {
      SpeciesCode: 'CDEL', PointX: '0.5000', PointY: '0.5000',
      BoxX: '0.4000', BoxY: '0.4000', BoxW: '0.2000', BoxH: '0.2000',
    }),
    makeRow('ID003', '30.000'),
    makeRow('ID004', '10.100', { PointX: 'oops' }),
  ];
  const marks = g.marksNear(rows, 10.2, 0.5);
  assert.deepEqual(marks, [
    { id: 'ID001', code: 'ACAU', point: { x: 0.25, y: 0.75 }, box: null },
    { id: 'ID002', code: 'CDEL', point: { x: 0.5, y: 0.5 }, box: { x: 0.4, y: 0.4, w: 0.2, h: 0.2 } },
  ]);
  assert.deepEqual(g.marksNear(rows, NaN, 0.5), []);
  assert.deepEqual(g.marksNear([], 10, 0.5), []);
});

test('endSentence closes server text with a period so the next sentence reads cleanly', () => {
  assert.equal(g.endSentence('species: unknown code ZZZZ'), 'species: unknown code ZZZZ.');
  assert.equal(g.endSentence('The folder did not load.'), 'The folder did not load.');
  assert.equal(g.endSentence('Is the server running?'), 'Is the server running?');
  assert.equal(g.endSentence('  timed out  '), 'timed out.');
  assert.equal(g.endSentence(''), '');
  assert.equal(g.endSentence(undefined), '');
  assert.equal(g.endSentence(404), '404.');
});

test('stripDataUrl returns the base64 part of a data URL', () => {
  assert.equal(g.stripDataUrl('data:image/png;base64,AAAA'), 'AAAA');
  assert.equal(g.stripDataUrl('data:image/jpeg;base64,/9j/4AAQ'), '/9j/4AAQ');
  assert.equal(g.stripDataUrl('AAAA'), 'AAAA');
  assert.throws(() => g.stripDataUrl(null), TypeError);
  assert.throws(() => g.stripDataUrl('data:,'), RangeError, 'an empty canvas gives "data:,"');
});
