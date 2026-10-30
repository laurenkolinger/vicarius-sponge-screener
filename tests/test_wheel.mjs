// Tests for static/wheel.js, the pure layout and hit testing behind the species wheel.
//
// Run with: node --test tests/test_wheel.mjs
//
// The project has no package.json, so node would read static/*.js as CommonJS and
// refuse their `export` statements. The test loads geometry.js through a data: URL,
// points the import line of wheel.js at that URL, and loads wheel.js the same way.

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
const wheelSource = readFileSync(path.join(staticDir, 'wheel.js'), 'utf8');
assert.ok(wheelSource.includes("from './geometry.js'"), 'wheel.js imports ./geometry.js');
const w = await import(moduleUrl(wheelSource.split("from './geometry.js'").join(`from '${geometryUrl}'`)));
const g = await import(geometryUrl);

const EPSILON = 1e-9;
const NUDGE_DEG = 0.01;

/** The species list and default pins of config/species.csv. */
const SPECIES = [];
const DEFAULT_PINS = { 1: null, 2: null, 3: null, 4: null, 5: null, 6: null, 7: null, 8: null, 9: null, 0: null };
for (const line of readFileSync(path.join(here, '..', 'config', 'species.csv'), 'utf8').trim().split('\n').slice(1)) {
  const [code, name, , pin] = line.trim().split(',');
  SPECIES.push({ code, name });
  if (pin) {
    DEFAULT_PINS[pin] = code;
  }
}
const PINNED = g.PIN_KEYS.map((key) => DEFAULT_PINS[key]).filter(Boolean);

/**
 * Give the offset from the wheel center of a point at a clock angle.
 * @param {number} degrees Angle clockwise from 12 o'clock.
 * @param {number} radius Distance from the center.
 * @returns {{dx: number, dy: number}} Screen offsets: y grows downward.
 */
function polar(degrees, radius) {
  const angle = (degrees * Math.PI) / 180;
  return { dx: radius * Math.sin(angle), dy: -radius * Math.cos(angle) };
}

/**
 * Assert that two numbers agree within a tiny tolerance.
 * @param {number} actual The value under test.
 * @param {number} expected The value it should equal.
 * @param {string} label What the number means, for the failure message.
 */
function assertClose(actual, expected, label) {
  assert.ok(Math.abs(actual - expected) < EPSILON, `${label}: expected ${expected}, got ${actual}`);
}

/**
 * Assert that a call throws a TypeError whose message names an argument.
 * @param {Function} call The call that should throw.
 * @param {string} name The argument name the message must hold.
 */
function assertTypeError(call, name) {
  assert.throws(call, (error) => error instanceof TypeError && error.message.includes(name),
    `expected a TypeError naming ${name}`);
}

/**
 * Build a list of placeholder codes c0, c1, ...
 * @param {number} count How many codes.
 * @returns {Array<string>} The codes.
 */
function codes(count) {
  return Array.from({ length: count }, (_, index) => `c${index}`);
}

// ------------------------------------------------------------------ constants

test('WHEEL holds the ring sizes and UNKNOWN_CODE names the unknown sponge', () => {
  assert.deepEqual({ ...w.WHEEL }, {
    hubRadius: 34, innerRadius: 118, outerRadius: 200, outerSlots: 16, minScale: 0.6, maxScale: 1, fitFraction: 0.94,
  });
  assert.ok(Object.isFrozen(w.WHEEL));
  assert.equal(w.UNKNOWN_CODE, 'UNKN');
});

// ----------------------------------------------------------------- wheelScale

test('wheelScale keeps full size in the two window sizes Lauren uses', () => {
  assert.equal(w.wheelScale(1280, 800), 1);
  assert.equal(w.wheelScale(946, 700), 1);
});

test('wheelScale shrinks the wheel to fit a small window, down to the minimum', () => {
  assertClose(w.wheelScale(400, 300), (300 * 0.94) / 400, '400 x 300');
  assert.equal(w.wheelScale(200, 120), 0.6);
});

test('wheelScale refuses sizes that are not finite numbers', () => {
  assertTypeError(() => w.wheelScale('1280', 800), 'viewW');
  assertTypeError(() => w.wheelScale(1280, NaN), 'viewH');
});

// ----------------------------------------------------------------- placeWheel

const VIEW = { w: 1280, h: 800 };
const R = 200;

test('placeWheel centers on a mark in the middle of the view without a shift', () => {
  assert.deepEqual(w.placeWheel({ x: 640, y: 400 }, VIEW, R), { x: 640, y: 400, shifted: false });
  assert.deepEqual(w.placeWheel({ x: 200, y: 600 }, VIEW, R), { x: 200, y: 600, shifted: false });
});

test('placeWheel moves the wheel inward at each edge', () => {
  assert.deepEqual(w.placeWheel({ x: 10, y: 400 }, VIEW, R), { x: 200, y: 400, shifted: true });
  assert.deepEqual(w.placeWheel({ x: 1275, y: 400 }, VIEW, R), { x: 1080, y: 400, shifted: true });
  assert.deepEqual(w.placeWheel({ x: 640, y: 5 }, VIEW, R), { x: 640, y: 200, shifted: true });
  assert.deepEqual(w.placeWheel({ x: 640, y: 795 }, VIEW, R), { x: 640, y: 600, shifted: true });
});

test('placeWheel moves the wheel inward at each corner', () => {
  assert.deepEqual(w.placeWheel({ x: 0, y: 0 }, VIEW, R), { x: 200, y: 200, shifted: true });
  assert.deepEqual(w.placeWheel({ x: 1280, y: 0 }, VIEW, R), { x: 1080, y: 200, shifted: true });
  assert.deepEqual(w.placeWheel({ x: 0, y: 800 }, VIEW, R), { x: 200, y: 600, shifted: true });
  assert.deepEqual(w.placeWheel({ x: 1280, y: 800 }, VIEW, R), { x: 1080, y: 600, shifted: true });
});

test('placeWheel calls a move of half a pixel or less no shift', () => {
  assert.deepEqual(w.placeWheel({ x: 199.6, y: 400 }, VIEW, R), { x: 200, y: 400, shifted: false });
  assert.deepEqual(w.placeWheel({ x: 199.4, y: 400 }, VIEW, R), { x: 200, y: 400, shifted: true });
});

test('placeWheel centers the wheel on an axis where the view is smaller than the wheel', () => {
  assert.deepEqual(w.placeWheel({ x: 10, y: 10 }, { w: 300, h: 380 }, R), { x: 150, y: 190, shifted: true });
  assert.deepEqual(w.placeWheel({ x: 10, y: 10 }, { w: 1280, h: 350 }, R), { x: 200, y: 175, shifted: true });
  assert.deepEqual(w.placeWheel({ x: 150, y: 190 }, { w: 300, h: 380 }, R), { x: 150, y: 190, shifted: false });
});

test('placeWheel refuses a missing mark, view, or radius', () => {
  assertTypeError(() => w.placeWheel(null, VIEW, R), 'mark');
  assertTypeError(() => w.placeWheel({ x: 1 }, VIEW, R), 'mark');
  assertTypeError(() => w.placeWheel({ x: 1, y: 1 }, { w: 10 }, R), 'view');
  assertTypeError(() => w.placeWheel({ x: 1, y: 1 }, VIEW, -5), 'radius');
});

// ----------------------------------------------------------------- sliceIndex

test('sliceIndex counts clockwise from 12 o\'clock with slice 0 centered there', () => {
  const up = polar(0, 100);
  const right = polar(90, 100);
  const down = polar(180, 100);
  const left = polar(270, 100);
  assert.equal(w.sliceIndex(up.dx, up.dy, 8), 0);
  assert.equal(w.sliceIndex(right.dx, right.dy, 8), 2);
  assert.equal(w.sliceIndex(down.dx, down.dy, 8), 4);
  assert.equal(w.sliceIndex(left.dx, left.dy, 8), 6);
  assert.equal(w.sliceIndex(right.dx, right.dy, 16), 4);
  const lowerLeft = polar(250, 100);
  assert.equal(w.sliceIndex(lowerLeft.dx, lowerLeft.dy, 10), 7);
});

for (const count of [8, 10, 16]) {
  test(`sliceIndex puts each side of every boundary of ${count} slices in the right slice`, () => {
    const width = 360 / count;
    for (let k = 0; k < count; k += 1) {
      const boundary = (k + 0.5) * width;
      const before = polar(boundary - NUDGE_DEG, 150);
      const after = polar(boundary + NUDGE_DEG, 150);
      assert.equal(w.sliceIndex(before.dx, before.dy, count), k, `just before boundary ${k}`);
      assert.equal(w.sliceIndex(after.dx, after.dy, count), (k + 1) % count, `just after boundary ${k}`);
    }
  });
}

test('sliceIndex wraps at 12 o\'clock: just left of 12 is still slice 0', () => {
  const leftOfNoon = polar(-1, 100);
  const rightOfNoon = polar(1, 100);
  assert.equal(w.sliceIndex(leftOfNoon.dx, leftOfNoon.dy, 16), 0);
  assert.equal(w.sliceIndex(rightOfNoon.dx, rightOfNoon.dy, 16), 0);
  const lastSlice = polar(360 - 11.25 - NUDGE_DEG, 100);
  assert.equal(w.sliceIndex(lastSlice.dx, lastSlice.dy, 16), 15);
});

test('sliceIndex returns -1 when there are no slices, and refuses bad numbers', () => {
  assert.equal(w.sliceIndex(10, 10, 0), -1);
  assertTypeError(() => w.sliceIndex(NaN, 1, 8), 'dx');
  assertTypeError(() => w.sliceIndex(1, 'a', 8), 'dy');
  assertTypeError(() => w.sliceIndex(1, 1, 2.5), 'count');
  assertTypeError(() => w.sliceIndex(1, 1, -1), 'count');
});

// --------------------------------------------------------------------- zoneAt

test('zoneAt names the hub, both rings, and the outside by distance', () => {
  assert.deepEqual(w.zoneAt(0, 0, 1, 8), { zone: 'hub', index: -1 });
  assert.equal(w.zoneAt(0, -33.9, 1, 8).zone, 'hub');
  assert.equal(w.zoneAt(0, -117.9, 1, 8).zone, 'inner');
  assert.equal(w.zoneAt(0, -199.9, 1, 8).zone, 'outer');
  assert.deepEqual(w.zoneAt(0, -200.1, 1, 8), { zone: 'outside', index: -1 });
  assert.deepEqual(w.zoneAt(300, 0, 1, 8), { zone: 'outside', index: -1 });
});

test('zoneAt gives a point exactly on a ring edge to the outer zone of the two', () => {
  assert.deepEqual(w.zoneAt(0, -34, 1, 8), { zone: 'inner', index: 0 });
  assert.deepEqual(w.zoneAt(0, -118, 1, 8), { zone: 'outer', index: 0 });
  assert.deepEqual(w.zoneAt(0, -200, 1, 8), { zone: 'outside', index: -1 });
  assert.deepEqual(w.zoneAt(34, 0, 1, 8), { zone: 'inner', index: 2 });
  assert.deepEqual(w.zoneAt(-118, 0, 1, 8), { zone: 'outer', index: 12 });
});

test('zoneAt divides distances by the scale', () => {
  assert.equal(w.zoneAt(0, -16.9, 0.5, 8).zone, 'hub');
  assert.equal(w.zoneAt(0, -17, 0.5, 8).zone, 'inner');
  assert.equal(w.zoneAt(0, -59, 0.5, 8).zone, 'outer');
  assert.equal(w.zoneAt(0, -100, 0.5, 8).zone, 'outside');
});

test('zoneAt indexes eight and ten inner slices and sixteen outer slots', () => {
  const at40 = polar(40, 80);
  assert.deepEqual(w.zoneAt(at40.dx, at40.dy, 1, 8), { zone: 'inner', index: 1 });
  assert.deepEqual(w.zoneAt(at40.dx, at40.dy, 1, 10), { zone: 'inner', index: 1 });
  const at100 = polar(100, 80);
  assert.deepEqual(w.zoneAt(at100.dx, at100.dy, 1, 8), { zone: 'inner', index: 2 });
  assert.deepEqual(w.zoneAt(at100.dx, at100.dy, 1, 10), { zone: 'inner', index: 3 });
  const outer = polar(100, 160);
  assert.deepEqual(w.zoneAt(outer.dx, outer.dy, 1, 8), { zone: 'outer', index: 4 });
});

for (const [ring, count, radius] of [['inner', 8, 80], ['inner', 10, 80], ['outer', 16, 160]]) {
  test(`zoneAt splits every ${ring} boundary of ${count} slices, including the wrap at 12 o'clock`, () => {
    const width = 360 / count;
    for (let k = 0; k < count; k += 1) {
      const boundary = (k + 0.5) * width;
      const before = polar(boundary - NUDGE_DEG, radius);
      const after = polar(boundary + NUDGE_DEG, radius);
      const innerCount = ring === 'inner' ? count : 8;
      assert.deepEqual(w.zoneAt(before.dx, before.dy, 1, innerCount), { zone: ring, index: k });
      assert.deepEqual(w.zoneAt(after.dx, after.dy, 1, innerCount), { zone: ring, index: (k + 1) % count });
    }
  });
}

test('zoneAt refuses a scale that is not positive and coordinates that are not numbers', () => {
  assertTypeError(() => w.zoneAt(1, 1, 0, 8), 'scale');
  assertTypeError(() => w.zoneAt(1, 1, -1, 8), 'scale');
  assertTypeError(() => w.zoneAt(undefined, 1, 1, 8), 'dx');
  assertTypeError(() => w.zoneAt(1, 1, 1, 'eight'), 'innerCount');
});

// ---------------------------------------------------------------- sliceCenter

test('sliceCenter places slice labels around the ring, clockwise from 12 o\'clock', () => {
  const top = w.sliceCenter(0, 8, 76);
  assertClose(top.x, 0, 'slice 0 x');
  assertClose(top.y, -76, 'slice 0 y');
  const right = w.sliceCenter(2, 8, 76);
  assertClose(right.x, 76, 'slice 2 x');
  assertClose(right.y, 0, 'slice 2 y');
  const bottom = w.sliceCenter(4, 8, 76);
  assertClose(bottom.x, 0, 'slice 4 x');
  assertClose(bottom.y, 76, 'slice 4 y');
  const second = w.sliceCenter(1, 16, 159);
  assertClose(second.x, 159 * Math.sin(Math.PI / 8), 'outer slot 1 x');
  assertClose(second.y, -159 * Math.cos(Math.PI / 8), 'outer slot 1 y');
});

test('sliceCenter and zoneAt agree for every slice of 8 and 10 inner and 16 outer', () => {
  const innerMid = (w.WHEEL.hubRadius + w.WHEEL.innerRadius) / 2;
  const outerMid = (w.WHEEL.innerRadius + w.WHEEL.outerRadius) / 2;
  for (const count of [8, 10]) {
    for (let index = 0; index < count; index += 1) {
      const spot = w.sliceCenter(index, count, innerMid);
      assert.deepEqual(w.zoneAt(spot.x, spot.y, 1, count), { zone: 'inner', index });
    }
  }
  for (let index = 0; index < w.WHEEL.outerSlots; index += 1) {
    const spot = w.sliceCenter(index, w.WHEEL.outerSlots, outerMid);
    assert.deepEqual(w.zoneAt(spot.x, spot.y, 1, 8), { zone: 'outer', index });
  }
});

test('sliceCenter refuses a bad index, count, or radius', () => {
  assertTypeError(() => w.sliceCenter(8, 8, 76), 'index');
  assertTypeError(() => w.sliceCenter(-1, 8, 76), 'index');
  assertTypeError(() => w.sliceCenter(0, 0, 76), 'count');
  assertTypeError(() => w.sliceCenter(0, 8, NaN), 'ringMid');
});

// ----------------------------------------------------------------- innerItems

test('innerItems lists the eight default pins in key order', () => {
  assert.deepEqual(w.innerItems(DEFAULT_PINS, g.PIN_KEYS), [
    { slot: '1', code: 'ACAU' }, { slot: '2', code: 'AFUL' }, { slot: '3', code: 'CDEL' },
    { slot: '4', code: 'MLAE' }, { slot: '5', code: 'CPLI' }, { slot: '6', code: 'ACRA' },
    { slot: '7', code: 'ACOM' }, { slot: '8', code: 'XMUT' },
  ]);
});

test('innerItems puts key 9 before key 0 and skips empty keys', () => {
  const ten = { ...DEFAULT_PINS, 9: 'SAUR', 0: 'UNKN' };
  const items = w.innerItems(ten, g.PIN_KEYS);
  assert.equal(items.length, 10);
  assert.deepEqual(items.slice(-2), [{ slot: '9', code: 'SAUR' }, { slot: '0', code: 'UNKN' }]);
  assert.deepEqual(w.innerItems({ 1: 'ACAU', 2: '', 3: 'CDEL', 0: 'UNKN' }, g.PIN_KEYS), [
    { slot: '1', code: 'ACAU' }, { slot: '3', code: 'CDEL' }, { slot: '0', code: 'UNKN' },
  ]);
});

test('innerItems refuses pins that are not an object and keys that are not a list', () => {
  assertTypeError(() => w.innerItems(null, g.PIN_KEYS), 'pins');
  assertTypeError(() => w.innerItems(DEFAULT_PINS, '1234'), 'pinKeys');
});

// ----------------------------------------------------------------- outerOrder

test('outerOrder puts the unknown sponge first, then the unpinned species by name', () => {
  const order = w.outerOrder(SPECIES, PINNED);
  assert.equal(order.length, SPECIES.length - PINNED.length);
  assert.equal(order[0], 'UNKN');
  for (const code of PINNED) {
    assert.ok(!order.includes(code), `${code} is pinned and stays out of the outer ring`);
  }
  const names = order.slice(1).map((code) => SPECIES.find((item) => item.code === code).name);
  const sorted = names.slice().sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
  assert.deepEqual(names, sorted);
  assert.deepEqual(order.slice(1, 4), ['ACLA', 'ACON', 'AARC']);
});

test('outerOrder leaves the unknown sponge out when it is pinned', () => {
  const order = w.outerOrder(SPECIES, [...PINNED, 'UNKN']);
  assert.ok(!order.includes('UNKN'));
  assert.equal(order[0], 'ACLA');
});

test('outerOrder sorts names without regard to case', () => {
  const list = [{ code: 'BBBB', name: 'beta one' }, { code: 'AAAA', name: 'Alpha two' }, { code: 'UNKN', name: 'Unknown sponge' }];
  assert.deepEqual(w.outerOrder(list, []), ['UNKN', 'AAAA', 'BBBB']);
});

test('outerOrder refuses a species list or pinned codes that are not arrays', () => {
  assertTypeError(() => w.outerOrder('ACAU', []), 'species');
  assertTypeError(() => w.outerOrder(SPECIES, null), 'pinnedCodes');
});

test('outerOrder puts species already logged on this transect first, most logged first', () => {
  const seen = new Map([['SAUR', 1], ['NERE', 3], ['ACAU', 5], ['VRIG', 1]]);
  const order = w.outerOrder(SPECIES, PINNED, seen);
  assert.deepEqual(order.slice(0, 4), ['NERE', 'SAUR', 'VRIG', 'UNKN'], 'counts first, then names, then the unknown sponge');
  assert.ok(!order.includes('ACAU'), 'a pinned species stays on the inner ring even when logged');
  assert.equal(order.length, SPECIES.length - PINNED.length, 'every unpinned species still appears once');
  assert.equal(new Set(order).size, order.length);
});

test('outerOrder ignores counts of zero and codes outside the species list', () => {
  const seen = new Map([['SAUR', 0], ['ZZZZ', 4]]);
  assert.deepEqual(w.outerOrder(SPECIES, PINNED, seen), w.outerOrder(SPECIES, PINNED));
});

test('outerOrder refuses seen counts that are not a Map', () => {
  assertTypeError(() => w.outerOrder(SPECIES, PINNED, { SAUR: 1 }), 'seen');
});

test('wheelMatches orders the outer ring by the seen counts only while no query is typed', () => {
  const seen = new Map([['SAUR', 2]]);
  assert.equal(w.wheelMatches(SPECIES, PINNED, '', seen).outer[0], 'SAUR');
  assert.deepEqual(w.wheelMatches(SPECIES, PINNED, 'ver', seen).outer, w.wheelMatches(SPECIES, PINNED, 'ver').outer);
});

test('seenCounts counts the labeled rows of each species and skips review points', () => {
  const rows = [
    { SpeciesCode: 'SAUR', LabelStatus: 'labeled' },
    { SpeciesCode: 'SAUR', LabelStatus: 'labeled' },
    { SpeciesCode: 'NERE' },
    { SpeciesCode: '', LabelStatus: 'unlabeled' },
  ];
  assert.deepEqual([...w.seenCounts(rows)], [['SAUR', 2], ['NERE', 1]]);
  assert.equal(w.seenCounts([]).size, 0);
  assertTypeError(() => w.seenCounts(null), 'rows');
});

test('cycleHighlight steps through a list both ways and wraps', () => {
  const list = ['ACAU', 'AFUL', 'SAUR'];
  assert.equal(w.cycleHighlight(list, null, 1), 'ACAU');
  assert.equal(w.cycleHighlight(list, null, -1), 'SAUR');
  assert.equal(w.cycleHighlight(list, 'AFUL', 1), 'SAUR');
  assert.equal(w.cycleHighlight(list, 'SAUR', 1), 'ACAU');
  assert.equal(w.cycleHighlight(list, 'ACAU', -1), 'SAUR');
  assert.equal(w.cycleHighlight(list, 'GONE', 1), 'ACAU', 'a highlight no longer listed starts over');
  assert.equal(w.cycleHighlight([], 'ACAU', 1), null);
  assertTypeError(() => w.cycleHighlight(list, null, 0), 'step');
});

// ---------------------------------------------------------------- outerWindow

test('outerWindow at offset 0 runs the list clockwise to 6 o\'clock and its end back from 12', () => {
  const window = w.outerWindow(codes(30), 0, 16);
  assert.equal(window.length, 16);
  assert.deepEqual(window[0], { slot: 0, code: 'c0' });
  assert.deepEqual(window[8], { slot: 8, code: 'c8' });
  assert.deepEqual(window[9], { slot: 9, code: 'c23' });
  assert.deepEqual(window[15], { slot: 15, code: 'c29' });
});

test('outerWindow follows the offset in both directions and wraps at the list length', () => {
  const plus = w.outerWindow(codes(30), 1, 16);
  assert.deepEqual([plus[0].code, plus[8].code, plus[9].code, plus[15].code], ['c1', 'c9', 'c24', 'c0']);
  const minus = w.outerWindow(codes(30), -1, 16);
  assert.deepEqual([minus[0].code, minus[8].code, minus[9].code, minus[15].code], ['c29', 'c7', 'c22', 'c28']);
  assert.deepEqual(w.outerWindow(codes(30), 30, 16), w.outerWindow(codes(30), 0, 16));
  assert.deepEqual(w.outerWindow(codes(30), -31, 16), minus);
});

test('outerWindow fills slots in order and ignores the offset when the list fits', () => {
  const ten = w.outerWindow(codes(10), 0, 16);
  assert.deepEqual(ten, codes(10).map((code, slot) => ({ slot, code })));
  assert.deepEqual(w.outerWindow(codes(10), 5, 16), ten);
  assert.deepEqual(w.outerWindow(codes(16), 3, 16), codes(16).map((code, slot) => ({ slot, code })));
  assert.deepEqual(w.outerWindow([], 2, 16), []);
});

test('outerWindow with one code more than the slots leaves one code out of view', () => {
  const window = w.outerWindow(codes(17), 0, 16);
  assert.equal(window.length, 16);
  assert.equal(window[9].code, 'c10');
  assert.equal(window[15].code, 'c16');
  assert.ok(!window.some((item) => item.code === 'c9'));
});

test('outerWindow refuses a bad list, offset, or slot count', () => {
  assertTypeError(() => w.outerWindow('abc', 0, 16), 'codes');
  assertTypeError(() => w.outerWindow(codes(3), 0.5, 16), 'offset');
  assertTypeError(() => w.outerWindow(codes(3), 0, 0), 'slots');
});

// ----------------------------------------------------------------- spinOffset

test('spinOffset wraps in both directions', () => {
  assert.equal(w.spinOffset(0, 1, 30), 1);
  assert.equal(w.spinOffset(0, -1, 30), 29);
  assert.equal(w.spinOffset(29, 1, 30), 0);
  assert.equal(w.spinOffset(5, -7, 30), 28);
  assert.equal(w.spinOffset(2, 65, 30), 7);
});

test('spinOffset returns 0 for an empty list and refuses steps that are not integers', () => {
  assert.equal(w.spinOffset(4, 3, 0), 0);
  assertTypeError(() => w.spinOffset(0, NaN, 30), 'steps');
  assertTypeError(() => w.spinOffset('1', 1, 30), 'offset');
  assertTypeError(() => w.spinOffset(0, 1, -2), 'length');
});

// --------------------------------------------------------------- wheelMatches

test('wheelMatches with an empty query keeps the pins inside and the rest in outer order', () => {
  const matches = w.wheelMatches(SPECIES, PINNED, '');
  assert.deepEqual(matches.inner, PINNED);
  assert.deepEqual(matches.outer, w.outerOrder(SPECIES, PINNED));
  assert.deepEqual(matches.order, SPECIES.map((item) => item.code));
});

test('wheelMatches "apl" lights two pins and puts three species in the outer ring', () => {
  const matches = w.wheelMatches(SPECIES, PINNED, 'apl');
  assert.deepEqual(matches.order, ['ACAU', 'AFUL', 'AARC', 'AFIS', 'AINS']);
  assert.deepEqual(matches.inner, ['ACAU', 'AFUL']);
  assert.deepEqual(matches.outer, ['AARC', 'AFIS', 'AINS']);
});

test('wheelMatches "saur" finds Smenospongia aurea by its code', () => {
  assert.deepEqual(w.wheelMatches(SPECIES, PINNED, 'saur'), { order: ['SAUR'], inner: [], outer: ['SAUR'] });
  assert.deepEqual(w.wheelMatches(SPECIES, PINNED, ' SAUR '), { order: ['SAUR'], inner: [], outer: ['SAUR'] });
});

test('wheelMatches "zzz" finds nothing', () => {
  assert.deepEqual(w.wheelMatches(SPECIES, PINNED, 'zzz'), { order: [], inner: [], outer: [] });
});

test('wheelMatches keeps the rank order of filterSpecies in both rings', () => {
  const matches = w.wheelMatches(SPECIES, PINNED, 'a');
  const ranked = g.filterSpecies(SPECIES, 'a').map((item) => item.code);
  assert.deepEqual(matches.order, ranked);
  assert.deepEqual(matches.inner, ranked.filter((code) => PINNED.includes(code)));
  assert.deepEqual(matches.outer, ranked.filter((code) => !PINNED.includes(code)));
});

test('wheelMatches refuses a query that is not a string', () => {
  assertTypeError(() => w.wheelMatches(SPECIES, PINNED, null), 'query');
  assertTypeError(() => w.wheelMatches(SPECIES, 'ACAU', ''), 'pinnedCodes');
});

// ----------------------------------------------------------------- shortLabel

test('sliceName shows the genus initial and the epithet', () => {
  assert.equal(w.sliceName('Aplysina cauliformis', 14), 'A. cauliformis');
  assert.equal(w.sliceName('Plakortis angulospiculatus', 10).length, 10);
  assert.equal(w.sliceName('Unknown sponge', 14), 'unknown');
});

test('shortLabel shows the epithet of a binomial', () => {
  assert.equal(w.shortLabel('Aplysina cauliformis', 12), 'cauliformis');
  assert.equal(w.shortLabel('Aplysina cauliformis', 11), 'cauliformis');
  assert.equal(w.shortLabel('Aplysina fulva', 10), 'fulva');
});

test('shortLabel cuts a long epithet with a trailing ellipsis inside the limit', () => {
  const label = w.shortLabel('Plakortis angulospiculatus', 10);
  assert.equal(label, 'angulospi…');
  assert.equal(label.length, 10);
});

test('shortLabel shows "unknown" for the unknown sponge and the whole name otherwise', () => {
  assert.equal(w.shortLabel('Unknown sponge', 10), 'unknown');
  assert.equal(w.shortLabel('Unknown sponge', 5), 'unkn…');
  assert.equal(w.shortLabel('Spongia', 10), 'Spongia');
  assert.equal(w.shortLabel('', 10), '');
});

test('shortLabel refuses a name that is not a string and a limit below one', () => {
  assertTypeError(() => w.shortLabel(null, 10), 'name');
  assertTypeError(() => w.shortLabel('Aplysina fulva', 0), 'maxChars');
  assertTypeError(() => w.shortLabel('Aplysina fulva', 2.5), 'maxChars');
});

// ------------------------------------------------------------- hover and lit

test('hoverHighlight moves the highlight to the slice under the pointer', () => {
  assert.equal(w.hoverHighlight('ACAU', 'AFUL', '', []), 'AFUL');
  assert.equal(w.hoverHighlight(null, 'SAUR', '  ', []), 'SAUR');
});

test('hoverHighlight without a query clears the highlight on the hub and outside the wheel', () => {
  assert.equal(w.hoverHighlight('ACAU', null, '', []), null, 'crossing the hub or leaving the wheel lights nothing');
  assert.equal(w.hoverHighlight('ACAU', null, 'apl', ['ACAU']), 'ACAU', 'a typed match stays lit for Enter');
});

test('hoverHighlight under a query lands only on a match, never on a dimmed slice', () => {
  const order = ['ACAU', 'AFUL', 'AARC'];
  assert.equal(w.hoverHighlight('ACAU', 'AFUL', 'apl', order), 'AFUL');
  assert.equal(w.hoverHighlight('ACAU', 'CDEL', 'apl', order), 'ACAU');
  assert.equal(w.hoverHighlight(null, 'CDEL', 'zzz', []), null);
});

test('hoverHighlight refuses a query that is not a string or matches that are not a list', () => {
  assertTypeError(() => w.hoverHighlight(null, 'ACAU', 3, []), 'query');
  assertTypeError(() => w.hoverHighlight(null, 'ACAU', 'a', null), 'order');
});

test('litTarget lights the hub only without a query, else the highlighted species', () => {
  assert.deepEqual(w.litTarget('ACAU', true, ''), { zone: 'hub', code: null });
  assert.deepEqual(w.litTarget('ACAU', false, ''), { zone: 'species', code: 'ACAU' });
  assert.equal(w.litTarget(null, false, ''), null);
  assert.deepEqual(w.litTarget('SAUR', true, 'saur'), { zone: 'species', code: 'SAUR' },
    'with a query, Enter picks the lit match even while the pointer rests on the hub');
  assert.equal(w.litTarget(null, true, 'zzz'), null);
  assertTypeError(() => w.litTarget('ACAU', 'yes', ''), 'hubHovered');
});

// ------------------------------------------------------------------ spinDelta

test('spinDelta turns the short way around the list', () => {
  assert.equal(w.spinDelta(0, 3, 30), 3);
  assert.equal(w.spinDelta(0, 28, 30), -2);
  assert.equal(w.spinDelta(28, 1, 30), 3);
  assert.equal(w.spinDelta(5, 5, 30), 0);
  assert.equal(w.spinDelta(0, 15, 30), 15, 'a tie turns forward');
  assert.equal(w.spinDelta(0, 0, 0), 0);
  assertTypeError(() => w.spinDelta(0, 1.5, 30), 'to');
  assertTypeError(() => w.spinDelta(0, 1, -1), 'length');
});

// ---------------------------------------------------------------- restingText

test('restingText tells a new mark from an edit at a glance', () => {
  assert.equal(w.restingText('new', null, null, 'Review'), 'Pick a species, type to filter, or press Space: Review');
  assert.equal(w.restingText('edit', 'ID001', 'ACAU', 'Unlabel'), 'Changing ID001 ACAU. Pick a species, or press Space: Unlabel');
  assert.equal(w.restingText('edit', 'ID004', null, 'Keep'), 'Changing ID004, review point. Pick a species, or press Space: Keep');
});

test('restingText refuses an unknown mode', () => {
  assertTypeError(() => w.restingText('move', 'ID001', 'ACAU', 'Unlabel'), 'mode');
});

// ---------------------------------------------------------------- spaceAction

test('spaceAction types a space into a typed query and otherwise chooses the hub', () => {
  assert.equal(w.spaceAction(''), 'hub');
  assert.equal(w.spaceAction('apl'), 'query');
  assert.equal(w.spaceAction('aplysina '), 'query');
  assertTypeError(() => w.spaceAction(null), 'query');
});

// -------------------------------------------------------------------- hubText

test('hubText keeps naming the edited row while the pointer rests on the hub', () => {
  assert.equal(w.hubText('new', null, null, 'Review'), 'Save the point without a species, for review');
  assert.equal(w.hubText('edit', 'ID001', 'ACAU', 'Unlabel'), 'Changing ID001 ACAU. Space or click: Unlabel');
  assert.equal(w.hubText('edit', 'ID004', null, 'Keep'), 'Changing ID004, review point. Space or click: Keep');
  assertTypeError(() => w.hubText('move', 'ID001', 'ACAU', 'Unlabel'), 'mode');
});

// ------------------------------------------------------------ deleting a row

test('rowLabel names a row by its ID and species, or as a review point', () => {
  assert.equal(w.rowLabel('ID004', 'ACAU'), 'ID004 ACAU');
  assert.equal(w.rowLabel('ID004', null), 'ID004, review point');
  assertTypeError(() => w.rowLabel('', 'ACAU'), 'rowId');
  assertTypeError(() => w.rowLabel(null, 'ACAU'), 'rowId');
});

test('deleteText asks for the second press and names the row', () => {
  assert.equal(w.deleteText('ID004', 'ACAU'), 'Press delete again to delete ID004 ACAU');
  assert.equal(w.deleteText('ID004', null), 'Press delete again to delete ID004, review point');
});

test('deleteKeyAction arms in an edit wheel with no query, and edits any typed query', () => {
  assert.equal(w.deleteKeyAction('edit', ''), 'arm');
  assert.equal(w.deleteKeyAction('edit', 'sa'), 'query');
  assert.equal(w.deleteKeyAction('edit', ' '), 'query', 'a typed space is part of the query');
  assert.equal(w.deleteKeyAction('new', 'sa'), 'query');
  assert.equal(w.deleteKeyAction('new', ''), 'none', 'a new mark has no row to delete');
  assertTypeError(() => w.deleteKeyAction('move', ''), 'mode');
  assertTypeError(() => w.deleteKeyAction('edit', null), 'query');
});

// ------------------------------------------------------------ deleteKeyTiming

test('deleteKeyTiming arms only well after a query edit and deletes only well after arming', () => {
  const quiet = -Infinity;
  assert.equal(w.deleteKeyTiming(5000, 5000 - w.QUERY_EDIT_GUARD_MS + 1, null), 'ignore', 'a tap that just cleared the filter must not arm');
  assert.equal(w.deleteKeyTiming(5000, 5000 - w.QUERY_EDIT_GUARD_MS, null), 'arm');
  assert.equal(w.deleteKeyTiming(5000, quiet, null), 'arm');
  assert.equal(w.deleteKeyTiming(5000, quiet, 5000 - w.DELETE_CONFIRM_MIN_MS + 1), 'ignore', 'a second tap too soon after arming must not delete');
  assert.equal(w.deleteKeyTiming(5000, quiet, 5000 - w.DELETE_CONFIRM_MIN_MS), 'delete');
});

test('deleteKeyTiming: four quick taps after clearing a two-letter filter never delete', () => {
  // Taps 50 ms apart: the first two edit the query, the next two come within the guard.
  let queryEditedAt = -Infinity;
  let armedAt = null;
  let deleted = false;
  const query = ['a', 'c'];
  for (let tap = 0; tap < 4; tap += 1) {
    const now = 1000 + tap * 50;
    if (query.length > 0) {
      query.pop();
      queryEditedAt = now;
      continue;
    }
    const decision = w.deleteKeyTiming(now, queryEditedAt, armedAt);
    if (decision === 'arm') {
      armedAt = now;
    } else if (decision === 'delete') {
      deleted = true;
    }
  }
  assert.equal(deleted, false);
  assert.equal(armedAt, null, 'no tap within the guard arms the deletion');
});

test('deleteKeyTiming refuses times that are not numbers', () => {
  assertTypeError(() => w.deleteKeyTiming('5000', 0, null), 'now');
  assertTypeError(() => w.deleteKeyTiming(5000, Number.NaN, null), 'queryEditedAt');
  assertTypeError(() => w.deleteKeyTiming(5000, 0, 'x'), 'armedAt');
});
