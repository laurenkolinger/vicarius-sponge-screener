/**
 * Pure layout and hit testing for the species wheel.
 *
 * The wheel opens centered on a mark. Its hub is a see-through hole over the mark,
 * the inner ring holds the pinned species in key order, and the outer ring shows a
 * window of 16 of the other species that spins. Angles run clockwise from 12
 * o'clock, and screen y grows downward. Nothing here touches the DOM, so every
 * function runs under `node --test` (see tests/test_wheel.mjs); wheel_view.js draws
 * what these functions compute.
 */

import { abbreviatedName, binomialParts, filterSpecies, labelStatus } from './geometry.js';

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

/** The code of the unknown sponge, which leads the outer ring. */
export const UNKNOWN_CODE = 'UNKN';

/** A center that moved this many pixels or less counts as not shifted. */
const SHIFT_TOLERANCE_PX = 0.5;

/** The ellipsis that ends a shortened label. */
const ELLIPSIS = '…';

/** One full turn in radians. */
const TURN = 2 * Math.PI;

/**
 * Tell whether a value is a finite number (not a numeric string, not NaN).
 * @param {*} value Anything.
 * @returns {boolean} True for a finite number.
 */
function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

/**
 * Check that an argument is a finite number.
 * @param {*} value The argument.
 * @param {string} where The function and argument name, such as "zoneAt: dx".
 * @throws {TypeError} When the value is not a finite number.
 */
function requireNumber(value, where) {
  if (!isFiniteNumber(value)) {
    throw new TypeError(`${where} must be a finite number, got ${JSON.stringify(value)}`);
  }
}

/**
 * Check that an argument is an integer at or above a minimum.
 * @param {*} value The argument.
 * @param {number} minimum The smallest allowed value.
 * @param {string} where The function and argument name.
 * @throws {TypeError} When the value is not an integer at or above the minimum.
 */
function requireInteger(value, minimum, where) {
  if (!Number.isInteger(value) || value < minimum) {
    throw new TypeError(`${where} must be an integer of at least ${minimum}, got ${JSON.stringify(value)}`);
  }
}

/**
 * Check that an argument is an array.
 * @param {*} value The argument.
 * @param {string} where The function and argument name.
 * @throws {TypeError} When the value is not an array.
 */
function requireArray(value, where) {
  if (!Array.isArray(value)) {
    throw new TypeError(`${where} must be an array, got ${typeof value}`);
  }
}

/**
 * Limit a number to a closed interval.
 * @param {number} value The number to limit.
 * @param {number} low The smallest allowed result.
 * @param {number} high The largest allowed result.
 * @returns {number} The limited number.
 */
function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

/**
 * Take the remainder that is never negative.
 * @param {number} value The dividend.
 * @param {number} divisor A positive divisor.
 * @returns {number} A value from 0 up to, not including, the divisor.
 */
function mod(value, divisor) {
  return ((value % divisor) + divisor) % divisor;
}

/**
 * Choose the wheel scale for a view, so the whole wheel fits the smaller side.
 * @param {number} viewW View width in CSS pixels.
 * @param {number} viewH View height in CSS pixels.
 * @returns {number} min(viewW, viewH) * fitFraction / (2 * outerRadius), held between
 *   minScale and maxScale.
 * @throws {TypeError} When a size is not a finite number.
 */
export function wheelScale(viewW, viewH) {
  requireNumber(viewW, 'wheelScale: viewW');
  requireNumber(viewH, 'wheelScale: viewH');
  const fit = (Math.min(viewW, viewH) * WHEEL.fitFraction) / (2 * WHEEL.outerRadius);
  return clamp(fit, WHEEL.minScale, WHEEL.maxScale);
}

/**
 * Center the wheel on a mark and keep it inside the view.
 * @param {{x: number, y: number}} mark The mark in view pixels.
 * @param {{w: number, h: number}} view The view size: the viewport, or the full-screen element.
 * @param {number} radius The wheel radius in view pixels, scale included.
 * @returns {{x: number, y: number, shifted: boolean}} The wheel center. On each axis where the
 *   view is wider than the wheel, the center is held between radius and size - radius;
 *   otherwise it sits in the middle of that axis. `shifted` is true when the center moved
 *   more than half a pixel away from the mark.
 * @throws {TypeError} When the mark or the view lacks finite numbers, or the radius is negative.
 */
export function placeWheel(mark, view, radius) {
  if (!mark || !isFiniteNumber(mark.x) || !isFiniteNumber(mark.y)) {
    throw new TypeError(`placeWheel: mark must be an object with finite x and y, got ${JSON.stringify(mark)}`);
  }
  if (!view || !isFiniteNumber(view.w) || !isFiniteNumber(view.h)) {
    throw new TypeError(`placeWheel: view must be an object with finite w and h, got ${JSON.stringify(view)}`);
  }
  if (!isFiniteNumber(radius) || radius < 0) {
    throw new TypeError(`placeWheel: radius must be a finite number of at least 0, got ${JSON.stringify(radius)}`);
  }
  const x = view.w > 2 * radius ? clamp(mark.x, radius, view.w - radius) : view.w / 2;
  const y = view.h > 2 * radius ? clamp(mark.y, radius, view.h - radius) : view.h / 2;
  return { x, y, shifted: Math.hypot(x - mark.x, y - mark.y) > SHIFT_TOLERANCE_PX };
}

/**
 * Find the slice under an offset from the wheel center.
 * @param {number} dx Horizontal offset from the center in pixels.
 * @param {number} dy Vertical offset from the center in pixels, growing downward.
 * @param {number} count The number of equal slices in the ring.
 * @returns {number} The slice index, counted clockwise with slice 0 centered at 12 o'clock;
 *   a point on a boundary belongs to the slice clockwise of it. -1 when count is 0.
 * @throws {TypeError} When dx or dy is not a finite number, or count is not an integer of 0 or more.
 */
export function sliceIndex(dx, dy, count) {
  requireNumber(dx, 'sliceIndex: dx');
  requireNumber(dy, 'sliceIndex: dy');
  requireInteger(count, 0, 'sliceIndex: count');
  if (count === 0) {
    return -1;
  }
  const width = TURN / count;
  const angle = mod(Math.atan2(dx, -dy), TURN);
  return Math.floor((angle + width / 2) / width) % count;
}

/**
 * Find the zone of the wheel under an offset from its center.
 * @param {number} dx Horizontal offset from the center in view pixels.
 * @param {number} dy Vertical offset from the center in view pixels.
 * @param {number} scale The wheel scale from wheelScale.
 * @param {number} innerCount The number of inner slices (the pinned species).
 * @returns {{zone: string, index: number}} zone is "hub", "inner", "outer", or "outside", by
 *   the distance divided by the scale against hubRadius, innerRadius, and outerRadius. A
 *   point exactly on an edge belongs to the outer zone of the two. index is the slice from
 *   sliceIndex for a ring, and -1 for the hub and the outside.
 * @throws {TypeError} When a number is not finite, the scale is not positive, or innerCount
 *   is not an integer of 0 or more.
 */
export function zoneAt(dx, dy, scale, innerCount) {
  requireNumber(dx, 'zoneAt: dx');
  requireNumber(dy, 'zoneAt: dy');
  if (!isFiniteNumber(scale) || scale <= 0) {
    throw new TypeError(`zoneAt: scale must be a positive finite number, got ${JSON.stringify(scale)}`);
  }
  requireInteger(innerCount, 0, 'zoneAt: innerCount');
  const distance = Math.hypot(dx, dy) / scale;
  if (distance < WHEEL.hubRadius) {
    return { zone: 'hub', index: -1 };
  }
  if (distance < WHEEL.innerRadius) {
    return { zone: 'inner', index: sliceIndex(dx, dy, innerCount) };
  }
  if (distance < WHEEL.outerRadius) {
    return { zone: 'outer', index: sliceIndex(dx, dy, WHEEL.outerSlots) };
  }
  return { zone: 'outside', index: -1 };
}

/**
 * Find where the label of a slice goes.
 * @param {number} index The slice index, 0 at 12 o'clock.
 * @param {number} count The number of slices in the ring.
 * @param {number} ringMid The distance from the center to the middle of the ring.
 * @returns {{x: number, y: number}} The offset of the label center from the wheel center.
 * @throws {TypeError} When the index is outside the ring, the count is below 1, or ringMid
 *   is not a finite number.
 */
export function sliceCenter(index, count, ringMid) {
  requireInteger(count, 1, 'sliceCenter: count');
  if (!Number.isInteger(index) || index < 0 || index >= count) {
    throw new TypeError(`sliceCenter: index must be an integer from 0 to ${count - 1}, got ${JSON.stringify(index)}`);
  }
  requireNumber(ringMid, 'sliceCenter: ringMid');
  const angle = (index * TURN) / count;
  return { x: ringMid * Math.sin(angle), y: -ringMid * Math.cos(angle) };
}

/**
 * List the pinned species of the inner ring.
 * @param {Object<string, ?string>} pins Pinned species code per key.
 * @param {Array<string>} pinKeys The keys in keyboard order: "1" to "9", then "0".
 * @returns {Array<{slot: string, code: string}>} One entry per pinned key in key order;
 *   empty keys are skipped.
 * @throws {TypeError} When pins is not an object or pinKeys is not an array.
 */
export function innerItems(pins, pinKeys) {
  if (!pins || typeof pins !== 'object') {
    throw new TypeError(`innerItems: pins must be an object, got ${JSON.stringify(pins)}`);
  }
  requireArray(pinKeys, 'innerItems: pinKeys');
  return pinKeys.filter((slot) => pins[slot]).map((slot) => ({ slot, code: pins[slot] }));
}

/**
 * Order the outer ring: species already logged on this transect first (most logged first,
 * then by name), then the unknown sponge, then every other unpinned species by name.
 * @param {Array<{code: string, name: string}>} species Every species.
 * @param {Array<string>} pinnedCodes The codes on the inner ring.
 * @param {Map<string, number>} [seen] Labeled sightings per species code in the open video.
 *   Counts of zero and codes outside the species list are ignored.
 * @returns {Array<string>} The unpinned codes, each once.
 * @throws {TypeError} When species or pinnedCodes is not an array, or seen is not a Map.
 */
export function outerOrder(species, pinnedCodes, seen = new Map()) {
  requireArray(species, 'outerOrder: species');
  requireArray(pinnedCodes, 'outerOrder: pinnedCodes');
  if (!(seen instanceof Map)) {
    throw new TypeError(`outerOrder: seen must be a Map of species codes to counts, got ${JSON.stringify(seen)}`);
  }
  const pinned = new Set(pinnedCodes);
  const byName = (left, right) => String(left.name).localeCompare(String(right.name), undefined, { sensitivity: 'base' });
  const unpinned = species.filter((item) => !pinned.has(item.code));
  const countOf = (item) => Number(seen.get(item.code)) || 0;
  const logged = unpinned
    .filter((item) => countOf(item) > 0)
    .sort((left, right) => countOf(right) - countOf(left) || byName(left, right))
    .map((item) => item.code);
  const loggedSet = new Set(logged);
  const rest = unpinned
    .filter((item) => !loggedSet.has(item.code) && item.code !== UNKNOWN_CODE)
    .sort(byName)
    .map((item) => item.code);
  const unknownFree = unpinned.some((item) => item.code === UNKNOWN_CODE) && !loggedSet.has(UNKNOWN_CODE);
  return [...logged, ...(unknownFree ? [UNKNOWN_CODE] : []), ...rest];
}

/**
 * Count the labeled sightings of each species among some observation rows.
 * @param {Array<Object<string, string>>} rows Observation rows, such as those of the open video.
 * @returns {Map<string, number>} Count per species code, in first-seen order. Later points
 *   (unlabeled rows) are left out.
 * @throws {TypeError} When rows is not an array.
 */
export function seenCounts(rows) {
  requireArray(rows, 'seenCounts: rows');
  const counts = new Map();
  for (const row of rows) {
    if (row && labelStatus(row) === 'labeled' && row.SpeciesCode) {
      counts.set(row.SpeciesCode, (counts.get(row.SpeciesCode) || 0) + 1);
    }
  }
  return counts;
}

/**
 * Step a highlight through a list of codes, wrapping at both ends (Tab and Shift+Tab).
 * @param {Array<string>} list The codes in wheel order.
 * @param {?string} current The highlighted code, or null.
 * @param {number} step 1 for the next code, -1 for the one before.
 * @returns {?string} The new highlight; the first code (or the last, stepping back) when
 *   nothing listed is highlighted; null for an empty list.
 * @throws {TypeError} When list is not an array or step is not 1 or -1.
 */
export function cycleHighlight(list, current, step) {
  requireArray(list, 'cycleHighlight: list');
  if (step !== 1 && step !== -1) {
    throw new TypeError(`cycleHighlight: step must be 1 or -1, got ${JSON.stringify(step)}`);
  }
  if (list.length === 0) {
    return null;
  }
  const index = list.indexOf(current);
  if (index === -1) {
    return step > 0 ? list[0] : list[list.length - 1];
  }
  return list[(index + step + list.length) % list.length];
}

/**
 * Choose the codes the outer ring shows for a spin offset.
 * @param {Array<string>} codes The outer ring list, in order.
 * @param {number} offset The spin offset; the code at this index sits at 12 o'clock.
 * @param {number} slots The number of outer slots.
 * @returns {Array<{slot: number, code: string}>} When the list fits, slot i holds codes[i]
 *   and the offset is ignored. Otherwise every slot s holds codes[mod(offset + rel(s), n)],
 *   where rel(s) is s for the slots from 12 o'clock clockwise to 6 o'clock and s - slots
 *   for the rest, so the list runs clockwise from 12 o'clock and its end comes back
 *   counterclockwise from 12 o'clock, with the seam just past 6 o'clock.
 * @throws {TypeError} When codes is not an array, the offset is not an integer, or slots is below 1.
 */
export function outerWindow(codes, offset, slots) {
  requireArray(codes, 'outerWindow: codes');
  if (!Number.isInteger(offset)) {
    throw new TypeError(`outerWindow: offset must be an integer, got ${JSON.stringify(offset)}`);
  }
  requireInteger(slots, 1, 'outerWindow: slots');
  if (codes.length <= slots) {
    return codes.map((code, slot) => ({ slot, code }));
  }
  const window = [];
  for (let slot = 0; slot < slots; slot += 1) {
    const rel = slot <= slots / 2 ? slot : slot - slots;
    window.push({ slot, code: codes[mod(offset + rel, codes.length)] });
  }
  return window;
}

/**
 * Spin the outer ring by whole slots.
 * @param {number} offset The current offset.
 * @param {number} steps Slots to move: positive brings later codes to 12 o'clock.
 * @param {number} length The length of the outer ring list.
 * @returns {number} The new offset from 0 up to length - 1, wrapping both ways; 0 when
 *   the list is empty.
 * @throws {TypeError} When offset or steps is not an integer, or length is not an integer of 0 or more.
 */
export function spinOffset(offset, steps, length) {
  if (!Number.isInteger(offset)) {
    throw new TypeError(`spinOffset: offset must be an integer, got ${JSON.stringify(offset)}`);
  }
  if (!Number.isInteger(steps)) {
    throw new TypeError(`spinOffset: steps must be an integer, got ${JSON.stringify(steps)}`);
  }
  requireInteger(length, 0, 'spinOffset: length');
  return length === 0 ? 0 : mod(offset + steps, length);
}

/**
 * Find the shortest turn of the outer ring between two offsets.
 * @param {number} from The offset now.
 * @param {number} to The offset wanted.
 * @param {number} length The length of the outer ring list.
 * @returns {number} Slots to turn: positive forward, negative back, never more than half
 *   the list either way (a tie turns forward); 0 when the list is empty.
 * @throws {TypeError} When from or to is not an integer, or length is not an integer of 0 or more.
 */
export function spinDelta(from, to, length) {
  if (!Number.isInteger(from)) {
    throw new TypeError(`spinDelta: from must be an integer, got ${JSON.stringify(from)}`);
  }
  if (!Number.isInteger(to)) {
    throw new TypeError(`spinDelta: to must be an integer, got ${JSON.stringify(to)}`);
  }
  requireInteger(length, 0, 'spinDelta: length');
  if (length === 0) {
    return 0;
  }
  const forward = mod(to - from, length);
  return forward > length / 2 ? forward - length : forward;
}

/**
 * Move the highlight with the pointer.
 * @param {?string} current The highlighted code now, or null.
 * @param {?string} hovered The code under the pointer, or null over the hub, an empty slot,
 *   or outside the wheel.
 * @param {string} query The typed query.
 * @param {Array<string>} order The codes that match the query, in rank order.
 * @returns {?string} Without a query: the hovered code, or null, so crossing the hub or
 *   leaving the wheel lights nothing. With a query: the hovered code when it is a match,
 *   else the current highlight, so the pointer never lights a dimmed slice and the typed
 *   match stays lit for Enter.
 * @throws {TypeError} When the query is not a string or order is not an array.
 */
export function hoverHighlight(current, hovered, query, order) {
  if (typeof query !== 'string') {
    throw new TypeError(`hoverHighlight: query must be a string, got ${JSON.stringify(query)}`);
  }
  requireArray(order, 'hoverHighlight: order');
  if (query.trim() === '') {
    return hovered || null;
  }
  return hovered && order.includes(hovered) ? hovered : current;
}

/**
 * Word the readout while nothing is lit: what the wheel is for and what Space does. An
 * edit names the row first, so it never reads like a new mark.
 * @param {string} mode "new" or "edit".
 * @param {?string} rowId The edited row, such as "ID001".
 * @param {?string} current The species of the edited row, or null for a Later point.
 * @param {string} hubLabel "Later", "Unlabel", or "Keep".
 * @returns {string} The hint text.
 * @throws {TypeError} When the mode is neither "new" nor "edit".
 */
export function restingText(mode, rowId, current, hubLabel) {
  if (mode === 'new') {
    return `Pick a species, type to filter, or press Space: ${hubLabel}`;
  }
  return `${changingText(mode, rowId, current, 'restingText')} Pick a species, or press Space: ${hubLabel}`;
}

/**
 * Word the readout while the pointer rests on the hub. An edit keeps naming the row, so
 * the hub's action never reads like a new mark's Later.
 * @param {string} mode "new" or "edit".
 * @param {?string} rowId The edited row, such as "ID001".
 * @param {?string} current The species of the edited row, or null for a Later point.
 * @param {string} hubLabel "Later", "Unlabel", or "Keep".
 * @returns {string} What the hub does.
 * @throws {TypeError} When the mode is neither "new" nor "edit".
 */
export function hubText(mode, rowId, current, hubLabel) {
  if (mode === 'new') {
    return 'Save the point without a species, for review';
  }
  return `${changingText(mode, rowId, current, 'hubText')} Space or click: ${hubLabel}`;
}

/**
 * Name the row an edit wheel changes.
 * @param {string} mode Must be "edit".
 * @param {?string} rowId The edited row.
 * @param {?string} current The species of the row, or null for a Later point.
 * @param {string} caller The public function's name, for the error message.
 * @returns {string} "Changing ID001 ACAU." or "Changing ID004, Later point."
 * @throws {TypeError} When the mode is not "edit".
 */
function changingText(mode, rowId, current, caller) {
  if (mode !== 'edit') {
    throw new TypeError(`${caller}: mode must be "new" or "edit", got ${JSON.stringify(mode)}`);
  }
  return `Changing ${rowLabel(rowId, current)}.`;
}

/**
 * Name a saved row the way the edit wheel and its toasts do.
 * @param {string} rowId The row's ID, such as "ID004".
 * @param {?string} current Its species code, or null for a Later point.
 * @returns {string} "ID004 ACAU", or "ID004, Later point".
 * @throws {TypeError} When rowId is not a non-empty string.
 */
export function rowLabel(rowId, current) {
  if (typeof rowId !== 'string' || rowId === '') {
    throw new TypeError(`rowLabel: rowId must be a non-empty string, got ${JSON.stringify(rowId)}`);
  }
  return current ? `${rowId} ${current}` : `${rowId}, review point`;
}

/**
 * Word the readout of an edit wheel after the first press of Backspace or Delete.
 * @param {string} rowId The edited row.
 * @param {?string} current Its species code, or null for a Later point.
 * @returns {string} Such as "Press delete again to delete ID004 ACAU".
 * @throws {TypeError} When rowId is not a non-empty string.
 */
export function deleteText(rowId, current) {
  return `Press delete again to delete ${rowLabel(rowId, current)}`;
}

/** A delete press this soon after a press that edited the query does not arm: quick taps to
 * clear a filter must never run on into deleting the row. */
export const QUERY_EDIT_GUARD_MS = 1000;
/** The deleting press must come at least this long after the arming press, so one burst of taps
 * cannot arm and delete. */
export const DELETE_CONFIRM_MIN_MS = 300;

/**
 * Decide what a press of Backspace or Delete does in the wheel of a saved mark with no query.
 * @param {number} now The time of this press, in milliseconds.
 * @param {number} queryEditedAt The time of the last press that edited the query (-Infinity if none).
 * @param {?number} armedAt The time the deletion was armed, or null when it is not armed.
 * @returns {string} "arm" to arm the deletion, "delete" to delete the row, or "ignore".
 * @throws {TypeError} When now is not a finite number, queryEditedAt is not a number, or armedAt
 *   is neither null nor a finite number.
 */
export function deleteKeyTiming(now, queryEditedAt, armedAt) {
  if (typeof now !== 'number' || !Number.isFinite(now)) {
    throw new TypeError(`deleteKeyTiming: now must be a finite number, got ${JSON.stringify(now)}`);
  }
  if (typeof queryEditedAt !== 'number' || Number.isNaN(queryEditedAt)) {
    throw new TypeError(`deleteKeyTiming: queryEditedAt must be a number, got ${String(queryEditedAt)}`);
  }
  if (armedAt !== null && (typeof armedAt !== 'number' || !Number.isFinite(armedAt))) {
    throw new TypeError(`deleteKeyTiming: armedAt must be null or a finite number, got ${JSON.stringify(armedAt)}`);
  }
  if (armedAt === null) {
    return now - queryEditedAt >= QUERY_EDIT_GUARD_MS ? 'arm' : 'ignore';
  }
  return now - armedAt >= DELETE_CONFIRM_MIN_MS ? 'delete' : 'ignore';
}

/**
 * Tell what Backspace or Delete does in the wheel.
 * @param {string} mode "new" or "edit".
 * @param {string} query The typed query.
 * @returns {string} "query" while a query is typed (the key edits it); "arm" in an edit wheel
 *   without one (the first press arms deleting the row, a second press deletes it); "none" in
 *   the wheel of a new mark without one, which has no row to delete.
 * @throws {TypeError} When the mode is unknown or the query is not a string.
 */
export function deleteKeyAction(mode, query) {
  if (mode !== 'new' && mode !== 'edit') {
    throw new TypeError(`deleteKeyAction: mode must be "new" or "edit", got ${JSON.stringify(mode)}`);
  }
  if (typeof query !== 'string') {
    throw new TypeError(`deleteKeyAction: query must be a string, got ${JSON.stringify(query)}`);
  }
  if (query !== '') {
    return 'query';
  }
  return mode === 'edit' ? 'arm' : 'none';
}

/**
 * Tell what Space does in the wheel.
 * @param {string} query The typed query.
 * @returns {string} "query" while a query is typed (Space types a space, for names of two
 *   words), "hub" otherwise (Later, Unlabel, or Keep).
 * @throws {TypeError} When the query is not a string.
 */
export function spaceAction(query) {
  if (typeof query !== 'string') {
    throw new TypeError(`spaceAction: query must be a string, got ${JSON.stringify(query)}`);
  }
  return query === '' ? 'hub' : 'query';
}

/**
 * Tell what the wheel lights, which is what Enter picks and the readout names.
 * @param {?string} highlighted The highlighted species code, or null.
 * @param {boolean} hubHovered True while the pointer rests on the hub.
 * @param {string} query The typed query.
 * @returns {?{zone: string, code: ?string}} {zone: "hub"} while the pointer rests on the
 *   hub and no query is typed; otherwise {zone: "species", code} for the highlight; null
 *   when nothing is lit. With a query the hub never lights, so Enter picks the lit match.
 * @throws {TypeError} When hubHovered is not a boolean or the query is not a string.
 */
export function litTarget(highlighted, hubHovered, query) {
  if (typeof hubHovered !== 'boolean') {
    throw new TypeError(`litTarget: hubHovered must be a boolean, got ${JSON.stringify(hubHovered)}`);
  }
  if (typeof query !== 'string') {
    throw new TypeError(`litTarget: query must be a string, got ${JSON.stringify(query)}`);
  }
  if (hubHovered && query.trim() === '') {
    return { zone: 'hub', code: null };
  }
  return highlighted ? { zone: 'species', code: highlighted } : null;
}

/**
 * Sort the species that match a typed query onto the two rings.
 * @param {Array<{code: string, name: string}>} species Every species, in display order.
 * @param {Array<string>} pinnedCodes The codes on the inner ring, in key order.
 * @param {string} query What the annotator typed.
 * @param {Map<string, number>} [seen] Labeled sightings per species in the open video.
 * @returns {{order: Array<string>, inner: Array<string>, outer: Array<string>}} order holds
 *   the codes from filterSpecies in rank order; inner keeps the pinned ones and outer the
 *   rest, both in rank order. An empty query gives inner = pinnedCodes and outer =
 *   outerOrder(species, pinnedCodes, seen).
 * @throws {TypeError} When species or pinnedCodes is not an array, or the query is not a string.
 */
export function wheelMatches(species, pinnedCodes, query, seen = new Map()) {
  requireArray(species, 'wheelMatches: species');
  requireArray(pinnedCodes, 'wheelMatches: pinnedCodes');
  if (typeof query !== 'string') {
    throw new TypeError(`wheelMatches: query must be a string, got ${JSON.stringify(query)}`);
  }
  const order = filterSpecies(species, query).map((item) => item.code);
  if (query.trim() === '') {
    return { order, inner: pinnedCodes.slice(), outer: outerOrder(species, pinnedCodes, seen) };
  }
  const pinned = new Set(pinnedCodes);
  return {
    order,
    inner: order.filter((code) => pinned.has(code)),
    outer: order.filter((code) => !pinned.has(code)),
  };
}

/**
 * Word a species name for a wheel slice: the genus initial and the epithet.
 * @param {string} name The scientific name, such as "Aplysina cauliformis".
 * @param {number} maxChars The longest label, ellipsis included.
 * @returns {string} Such as "A. cauliformis", cut with an ellipsis when it is longer.
 */
export function sliceName(name, maxChars) {
  if (!binomialParts(name)) {
    return shortLabel(name, maxChars);
  }
  const label = abbreviatedName(name);
  return label.length > maxChars ? `${label.slice(0, maxChars - 1)}${ELLIPSIS}` : label;
}

/**
 * Shorten a scientific name for a slice label.
 * @param {string} name The scientific name, such as "Aplysina cauliformis".
 * @param {number} maxChars The longest label, ellipsis included.
 * @returns {string} The epithet of a binomial ("cauliformis"), "unknown" for a name that
 *   starts with "Unknown", or the whole name otherwise, cut to maxChars with a trailing
 *   ellipsis when it is longer.
 * @throws {TypeError} When the name is not a string or maxChars is not an integer of 1 or more.
 */
export function shortLabel(name, maxChars) {
  if (typeof name !== 'string') {
    throw new TypeError(`shortLabel: name must be a string, got ${JSON.stringify(name)}`);
  }
  requireInteger(maxChars, 1, 'shortLabel: maxChars');
  const parts = binomialParts(name);
  let label = name;
  if (parts) {
    label = parts.epithet;
  } else if (name.startsWith('Unknown')) {
    label = name.split(' ')[0].toLowerCase();
  }
  return label.length > maxChars ? `${label.slice(0, maxChars - 1)}${ELLIPSIS}` : label;
}
