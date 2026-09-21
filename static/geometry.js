/**
 * Pure calculations for the Sponge Screener page.
 *
 * Nothing in this file touches the DOM, the network, or the clock, so every
 * function runs under `node --test` (see tests/test_geometry.mjs). Points and
 * boxes are fractions of the video frame: x and y run from 0 to 1, with 0, 0 at
 * the top left of the picture.
 */

/** The ten pinned-species keys in keyboard order. */
export const PIN_KEYS = Object.freeze(['1', '2', '3', '4', '5', '6', '7', '8', '9', '0']);

/** Playback speeds that the [ and ] keys step through. */
export const SPEEDS = Object.freeze([0.5, 0.75, 1, 1.5, 2, 3]);

/** Plain words for each quadrant code, the same phrases the server writes to Notes. */
export const QUADRANT_PHRASES = Object.freeze({
  TOPLEFT: 'top left',
  TOPRIGHT: 'top right',
  BOTTOMLEFT: 'bottom left',
  BOTTOMRIGHT: 'bottom right',
});

/** A drag must travel farther than this many pixels before it draws a box. */
export const DRAG_THRESHOLD_PX = 6;

/** Side of the square crop around a clicked point, in video pixels. */
export const CROP_SIZE = 512;

/** A box thinner than this fraction of the frame counts as a slip of the hand. */
export const MIN_BOX_FRACTION = 0.004;

/** Seconds to rewind when a video reopens at its saved position. */
export const RESUME_REWIND_SECONDS = 2;

/** A saved position this close to the end restarts the video from the top. */
export const RESUME_END_GUARD_SECONDS = 3;

/** Name of the first breadcrumb, which stands for the root prefix. */
export const ROOT_CRUMB_NAME = 'All videos';

/**
 * Tell whether a value is a finite number (not a numeric string, not NaN).
 * @param {*} value Anything.
 * @returns {boolean} True for a finite number.
 */
function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
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
 * Check that an object carries finite `x` and `y` numbers.
 * @param {*} point The object to check.
 * @param {string} where The caller and argument name, for the error message.
 * @throws {TypeError} When the object or either field is missing or not finite.
 */
function requirePoint(point, where) {
  if (!point || !isFiniteNumber(point.x) || !isFiniteNumber(point.y)) {
    throw new TypeError(`${where} must be an object with finite x and y, got ${JSON.stringify(point)}`);
  }
}

/**
 * Read the number that follows "ID" in a sighting ID such as "ID007".
 * @param {*} id The ID cell of a row.
 * @returns {number} The number, or NaN when the cell has another shape.
 */
function idNumber(id) {
  const match = /^ID(\d+)$/.exec(String(id));
  return match ? Number(match[1]) : NaN;
}

/**
 * Find the letterboxed picture inside a video element that uses object-fit: contain.
 * @param {number} elemW Element width in CSS pixels.
 * @param {number} elemH Element height in CSS pixels.
 * @param {number} videoW Native video width in pixels.
 * @param {number} videoH Native video height in pixels.
 * @returns {{x: number, y: number, w: number, h: number}} The picture rectangle in
 *   element pixels. Every field is 0 while any size is unknown, zero, or negative.
 */
export function contentRect(elemW, elemH, videoW, videoH) {
  const sizes = [elemW, elemH, videoW, videoH];
  if (!sizes.every((size) => isFiniteNumber(size) && size > 0)) {
    return { x: 0, y: 0, w: 0, h: 0 };
  }
  const scale = Math.min(elemW / videoW, elemH / videoH);
  const w = videoW * scale;
  const h = videoH * scale;
  return { x: (elemW - w) / 2, y: (elemH - h) / 2, w, h };
}

/**
 * Turn an element pixel position into frame fractions.
 * @param {number} px Horizontal position in element pixels.
 * @param {number} py Vertical position in element pixels.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture rectangle from contentRect.
 * @returns {?{x: number, y: number}} Fractions from 0 to 1, or null when the position
 *   falls outside the picture, the rectangle is empty, or a number is not finite.
 */
export function toNormalized(px, py, rect) {
  if (!rect || !(rect.w > 0) || !(rect.h > 0) || !isFiniteNumber(px) || !isFiniteNumber(py)) {
    return null;
  }
  const x = (px - rect.x) / rect.w;
  const y = (py - rect.y) / rect.h;
  if (x < 0 || x > 1 || y < 0 || y > 1) {
    return null;
  }
  return { x, y };
}

/**
 * Turn an element pixel position into frame fractions, pulling outside positions
 * onto the nearest picture edge. A drag that leaves the picture ends here.
 * @param {number} px Horizontal position in element pixels.
 * @param {number} py Vertical position in element pixels.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture rectangle from contentRect.
 * @returns {?{x: number, y: number}} Fractions from 0 to 1, or null when the rectangle
 *   is empty or a number is not finite.
 */
export function toNormalizedClamped(px, py, rect) {
  if (!rect || !(rect.w > 0) || !(rect.h > 0) || !isFiniteNumber(px) || !isFiniteNumber(py)) {
    return null;
  }
  return {
    x: clamp((px - rect.x) / rect.w, 0, 1),
    y: clamp((py - rect.y) / rect.h, 0, 1),
  };
}

/**
 * Turn frame fractions back into element pixels.
 * @param {{x: number, y: number}} point Fractions from 0 to 1.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture rectangle from contentRect.
 * @returns {{x: number, y: number}} The position in element pixels.
 */
export function toPixels(point, rect) {
  requirePoint(point, 'toPixels: point');
  return { x: rect.x + point.x * rect.w, y: rect.y + point.y * rect.h };
}

/**
 * Name the quadrant that holds a point. The rule matches screener/positions.py:
 * x below 0.5 is LEFT and y below 0.5 is TOP.
 * @param {number} x Horizontal fraction.
 * @param {number} y Vertical fraction.
 * @returns {string} "TOPLEFT", "TOPRIGHT", "BOTTOMLEFT", or "BOTTOMRIGHT".
 * @throws {TypeError} When x or y is not a finite number.
 */
export function quadrantOf(x, y) {
  if (!isFiniteNumber(x) || !isFiniteNumber(y)) {
    throw new TypeError(`quadrantOf: x and y must be finite numbers, got x=${x} y=${y}`);
  }
  return (y < 0.5 ? 'TOP' : 'BOTTOM') + (x < 0.5 ? 'LEFT' : 'RIGHT');
}

/**
 * Find the part of the picture that a quadrant covers.
 * @param {string} quadrant A quadrant code from quadrantOf.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture rectangle from contentRect.
 * @returns {{x: number, y: number, w: number, h: number}} The quadrant in element pixels.
 * @throws {RangeError} When the quadrant code is unknown.
 */
export function quadrantRect(quadrant, rect) {
  if (!Object.prototype.hasOwnProperty.call(QUADRANT_PHRASES, quadrant)) {
    throw new RangeError(`quadrantRect: unknown quadrant ${JSON.stringify(quadrant)}`);
  }
  const halfW = rect.w / 2;
  const halfH = rect.h / 2;
  return {
    x: rect.x + (quadrant.endsWith('RIGHT') ? halfW : 0),
    y: rect.y + (quadrant.startsWith('BOTTOM') ? halfH : 0),
    w: halfW,
    h: halfH,
  };
}

/**
 * Place the small text tag of a mark so it never covers the mark and never leaves the picture.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture rectangle in element pixels.
 * @param {{x: number, y: number}} center The mark center in element pixels.
 * @param {?{x: number, y: number, w: number, h: number}} boxPx The box in element pixels, or null for a point.
 * @param {number} tagW Tag width in pixels.
 * @param {number} tagH Tag height in pixels.
 * @param {number} radius Ring radius of a point mark in pixels.
 * @returns {{x: number, y: number}} The top left corner of the tag. A point tag sits up and to
 *   the right of the ring and flips left or down near an edge. A box tag sits above the
 *   box, or just inside its top left corner when the box touches the top of the picture.
 */
export function tagPosition(rect, center, boxPx, tagW, tagH, radius) {
  const gap = 12;
  const boxGap = 6;
  let x;
  let y;
  if (boxPx) {
    x = boxPx.x;
    y = boxPx.y - tagH - boxGap;
    if (y < rect.y) {
      x = boxPx.x + boxGap;
      y = boxPx.y + boxGap;
    }
  } else {
    x = center.x + radius + gap;
    y = center.y - radius - gap - tagH;
    if (x + tagW > rect.x + rect.w) {
      x = center.x - radius - gap - tagW;
    }
    if (y < rect.y) {
      y = center.y + radius + gap;
    }
  }
  return {
    x: clamp(x, rect.x, Math.max(rect.x, rect.x + rect.w - tagW)),
    y: clamp(y, rect.y, Math.max(rect.y, rect.y + rect.h - tagH)),
  };
}

/**
 * Tell a drag from a click by the distance between press and release.
 * @param {number} ax Press position, horizontal pixels.
 * @param {number} ay Press position, vertical pixels.
 * @param {number} bx Current position, horizontal pixels.
 * @param {number} by Current position, vertical pixels.
 * @param {number} [thresholdPx=6] Distance a drag must pass.
 * @returns {boolean} True when the distance is greater than the threshold.
 */
export function isDrag(ax, ay, bx, by, thresholdPx = DRAG_THRESHOLD_PX) {
  return Math.hypot(bx - ax, by - ay) > thresholdPx;
}

/**
 * Build a box from two opposite corners in any order.
 * @param {{x: number, y: number}} a One corner, in frame fractions (may lie outside 0 to 1).
 * @param {{x: number, y: number}} b The opposite corner.
 * @returns {{x: number, y: number, w: number, h: number}} The box, clamped to the frame.
 * @throws {TypeError} When a corner lacks finite x and y.
 */
export function normalizeBox(a, b) {
  requirePoint(a, 'normalizeBox: corner a');
  requirePoint(b, 'normalizeBox: corner b');
  const left = clamp(Math.min(a.x, b.x), 0, 1);
  const right = clamp(Math.max(a.x, b.x), 0, 1);
  const top = clamp(Math.min(a.y, b.y), 0, 1);
  const bottom = clamp(Math.max(a.y, b.y), 0, 1);
  return { x: left, y: top, w: right - left, h: bottom - top };
}

/**
 * Tell whether a dragged box is big enough to keep. The server refuses a box with
 * zero width or height, and a sliver is a slip of the hand, so the page turns an
 * unusable box into a point mark.
 * @param {?{x: number, y: number, w: number, h: number}} box The box, or null.
 * @param {number} [minFraction=0.004] Smallest width and height, as a frame fraction.
 * @returns {boolean} True when the box exists and both sides reach the minimum.
 */
export function isUsableBox(box, minFraction = MIN_BOX_FRACTION) {
  return Boolean(box) && box.w >= minFraction && box.h >= minFraction;
}

/**
 * Find the position that decides the quadrant. It matches anchor_point in
 * screener/positions.py: the box center when a box exists, otherwise the point.
 * @param {{x: number, y: number}} point The clicked point.
 * @param {?{x: number, y: number, w: number, h: number}} box The dragged box, or null.
 * @returns {{x: number, y: number}} The anchor in frame fractions.
 */
export function anchorPoint(point, box) {
  if (box) {
    return { x: box.x + box.w / 2, y: box.y + box.h / 2 };
  }
  requirePoint(point, 'anchorPoint: point');
  return { x: point.x, y: point.y };
}

/**
 * Work out which source pixels the crop image copies.
 * @param {{x: number, y: number}} point The clicked point, in frame fractions.
 * @param {?{x: number, y: number, w: number, h: number}} box The dragged box, or null.
 * @param {number} videoW Native frame width in pixels.
 * @param {number} videoH Native frame height in pixels.
 * @param {number} [size=512] Side of the square crop around a point.
 * @returns {{sx: number, sy: number, sw: number, sh: number}} Whole source pixels. A box
 *   crops the box. A point crops a size x size square, shifted to stay inside the
 *   frame and shrunk to the frame when the frame is smaller than the square.
 * @throws {RangeError} When the frame size or the crop size is not a positive number.
 * @throws {TypeError} When the point or the box holds a value that is not a finite number.
 */
export function cropRect(point, box, videoW, videoH, size = CROP_SIZE) {
  if (!isFiniteNumber(videoW) || !isFiniteNumber(videoH) || videoW < 1 || videoH < 1) {
    throw new RangeError(`cropRect: the frame size must be positive, got ${videoW} x ${videoH}`);
  }
  if (!isFiniteNumber(size) || size < 1) {
    throw new RangeError(`cropRect: the crop size must be positive, got ${size}`);
  }
  const frameW = Math.floor(videoW);
  const frameH = Math.floor(videoH);
  if (box) {
    const fields = [box.x, box.y, box.w, box.h];
    if (!fields.every(isFiniteNumber)) {
      throw new TypeError(`cropRect: box must hold finite x, y, w, h, got ${JSON.stringify(box)}`);
    }
    const sx = clamp(Math.round(box.x * frameW), 0, frameW - 1);
    const sy = clamp(Math.round(box.y * frameH), 0, frameH - 1);
    const right = clamp(Math.round((box.x + box.w) * frameW), sx + 1, frameW);
    const bottom = clamp(Math.round((box.y + box.h) * frameH), sy + 1, frameH);
    return { sx, sy, sw: right - sx, sh: bottom - sy };
  }
  requirePoint(point, 'cropRect: point');
  const sw = Math.min(Math.floor(size), frameW);
  const sh = Math.min(Math.floor(size), frameH);
  const sx = clamp(Math.round(clamp(point.x, 0, 1) * frameW - sw / 2), 0, frameW - sw);
  const sy = clamp(Math.round(clamp(point.y, 0, 1) * frameH - sh / 2), 0, frameH - sh);
  return { sx, sy, sw, sh };
}

/**
 * Format a video time the way the January table does: whole seconds, floored.
 * @param {number} seconds Time in seconds.
 * @returns {string} "MM:SS" (minutes may pass 99), or "--:--" when the time is
 *   unknown, negative, or infinite.
 */
export function formatClock(seconds) {
  if (!isFiniteNumber(seconds) || seconds < 0) {
    return '--:--';
  }
  const whole = Math.floor(seconds);
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
}

/**
 * Rank the species that match a typed query.
 * @param {Array<{code: string, name: string}>} list Every species, in display order.
 * @param {string} query What the annotator typed. Case and outer spaces are ignored.
 * @returns {Array<{code: string, name: string}>} A new array: species whose code starts
 *   with the query, then species with a word in the name that starts with the query,
 *   then species whose code or name holds the query anywhere. Each group keeps the
 *   list order. An empty query returns a copy of the whole list.
 * @throws {TypeError} When the list is not an array or the query is not a string.
 */
export function filterSpecies(list, query) {
  if (!Array.isArray(list)) {
    throw new TypeError(`filterSpecies: list must be an array, got ${typeof list}`);
  }
  if (typeof query !== 'string') {
    throw new TypeError(`filterSpecies: query must be a string, got ${typeof query}`);
  }
  const needle = query.trim().toLowerCase();
  if (needle === '') {
    return list.slice();
  }
  const codePrefix = [];
  const wordPrefix = [];
  const substring = [];
  for (const item of list) {
    const code = String(item.code).toLowerCase();
    const name = String(item.name).toLowerCase();
    if (code.startsWith(needle)) {
      codePrefix.push(item);
    } else if (startsAtWord(name, needle)) {
      wordPrefix.push(item);
    } else if (code.includes(needle) || name.includes(needle)) {
      substring.push(item);
    }
  }
  return [...codePrefix, ...wordPrefix, ...substring];
}

/**
 * Tell whether a phrase occurs in a text at the start of a word.
 * @param {string} text Lowercase text to search.
 * @param {string} phrase Lowercase phrase to find.
 * @returns {boolean} True when an occurrence starts the text or follows a character
 *   that is not a letter or a digit.
 */
function startsAtWord(text, phrase) {
  let index = text.indexOf(phrase);
  while (index !== -1) {
    if (index === 0 || !/[a-z0-9]/.test(text[index - 1])) {
      return true;
    }
    index = text.indexOf(phrase, index + 1);
  }
  return false;
}

/**
 * Split a binomial so a narrow slot can show the epithet, which is the part that
 * tells two species of one genus apart.
 * @param {string} name A scientific name such as "Aplysina cauliformis".
 * @returns {?{initial: string, epithet: string}} {initial: "A.", epithet: "cauliformis"}
 *   for a two-word binomial. Any other name, including "Unknown sponge", gives null.
 */
export function binomialParts(name) {
  if (typeof name !== 'string' || name.startsWith('Unknown ')) {
    return null;
  }
  const match = /^([A-Z])[a-z]+ ([a-z-]+)$/.exec(name);
  return match ? { initial: `${match[1]}.`, epithet: match[2] } : null;
}

/**
 * Build the tally rows the right panel shows.
 * @param {Array<{code: string, name: string, sightings: number, videos: number, earliest_year: ?number}>} tally
 *   The server tally: one entry per species with at least one sighting.
 * @param {Object<string, ?string>} pins Pinned species code per key ("1" to "9", then "0").
 * @param {Array<{code: string, name: string}>} species Every species, for the names of pinned species.
 * @returns {Array<{code: string, name: string, sightings: number, videos: number, earliest_year: ?number, pin: ?string}>}
 *   One row per pinned species in key order (zeros when it has no sightings), then every
 *   other species with sightings, sorted by code. `pin` holds the key, or null.
 * @throws {TypeError} When the tally or the species list is not an array.
 */
export function mergeTally(tally, pins, species) {
  if (!Array.isArray(tally)) {
    throw new TypeError(`mergeTally: tally must be an array, got ${typeof tally}`);
  }
  if (!Array.isArray(species)) {
    throw new TypeError(`mergeTally: species must be an array, got ${typeof species}`);
  }
  const counted = new Map(tally.map((entry) => [entry.code, entry]));
  const names = new Map(species.map((item) => [item.code, item.name]));
  const pinned = new Set();
  const rows = [];
  for (const key of PIN_KEYS) {
    const code = pins ? pins[key] : null;
    if (!code || pinned.has(code)) {
      continue;
    }
    pinned.add(code);
    const entry = counted.get(code);
    rows.push({
      code,
      name: names.get(code) || (entry && entry.name) || code,
      sightings: entry ? entry.sightings : 0,
      videos: entry ? entry.videos : 0,
      earliest_year: entry && entry.earliest_year != null ? entry.earliest_year : null,
      pin: key,
    });
  }
  const others = tally
    .filter((entry) => !pinned.has(entry.code))
    .sort((left, right) => (left.code < right.code ? -1 : left.code > right.code ? 1 : 0));
  for (const entry of others) {
    rows.push({
      code: entry.code,
      name: entry.name || names.get(entry.code) || entry.code,
      sightings: entry.sightings,
      videos: entry.videos,
      earliest_year: entry.earliest_year != null ? entry.earliest_year : null,
      pin: null,
    });
  }
  return rows;
}

/**
 * Put a species on a pinned key without letting a code appear twice.
 * @param {Object<string, ?string>} pins The current pins.
 * @param {string} slot The key that receives the species ("1" to "9", or "0").
 * @param {string} code The species code to pin.
 * @returns {Object<string, ?string>} A new object with all ten keys. When the species
 *   already sat on another key, that key receives what the target key held (a swap).
 * @throws {RangeError} When the slot is not one of the ten keys.
 * @throws {TypeError} When the code is not a non-empty string.
 */
export function swapPin(pins, slot, code) {
  if (!PIN_KEYS.includes(slot)) {
    throw new RangeError(`swapPin: slot must be one of ${PIN_KEYS.join(' ')}, got ${JSON.stringify(slot)}`);
  }
  if (typeof code !== 'string' || code === '') {
    throw new TypeError(`swapPin: code must be a non-empty string, got ${JSON.stringify(code)}`);
  }
  const next = {};
  for (const key of PIN_KEYS) {
    next[key] = pins && pins[key] ? pins[key] : null;
  }
  const displaced = next[slot];
  const previousSlot = PIN_KEYS.find((key) => next[key] === code && key !== slot);
  if (previousSlot !== undefined) {
    next[previousSlot] = displaced;
  }
  next[slot] = code;
  return next;
}

/** Physical key codes that stand for one fixed key value. */
const KEY_OF_CODE = Object.freeze({
  Enter: 'Enter',
  NumpadEnter: 'Enter',
  Escape: 'Escape',
  Tab: 'Tab',
  Space: ' ',
  Comma: ',',
  Period: '.',
  BracketLeft: '[',
  BracketRight: ']',
  Home: 'Home',
  ArrowLeft: 'ArrowLeft',
  ArrowRight: 'ArrowRight',
  ArrowUp: 'ArrowUp',
  ArrowDown: 'ArrowDown',
});

/**
 * Read the key of a keyboard event, falling back to the physical key code. Real typing
 * always fills `key`. Test drivers that send raw key events sometimes fill `code` only.
 * @param {string} key The event's `key` value.
 * @param {string} code The event's `code` value.
 * @param {boolean} shift True when Shift is down.
 * @returns {string} The key value, or "" when neither field names a key the page uses.
 */
export function keyFromEvent(key, code, shift) {
  if (typeof key === 'string' && key !== '' && key !== 'Unidentified') {
    return key;
  }
  const name = typeof code === 'string' ? code : '';
  const digit = /^(?:Digit|Numpad)(\d)$/.exec(name);
  if (digit) {
    return shift ? '' : digit[1];
  }
  const letter = /^Key([A-Z])$/.exec(name);
  if (letter) {
    return letter[1].toLowerCase();
  }
  if (name === 'Slash') {
    return shift ? '?' : '';
  }
  return Object.prototype.hasOwnProperty.call(KEY_OF_CODE, name) ? KEY_OF_CODE[name] : '';
}

/**
 * Read a pinned-species key from a keyboard event.
 * @param {string} key The event's `key` value.
 * @param {string} code The event's `code` value.
 * @returns {?string} "1" to "9" or "0" for the number row and the number pad, else null.
 */
export function pinKeyFromEvent(key, code) {
  if (typeof key === 'string' && PIN_KEYS.includes(key)) {
    return key;
  }
  const match = /^Numpad(\d)$/.exec(String(code));
  if (match && typeof key === 'string' && key === match[1]) {
    return match[1];
  }
  return null;
}

/**
 * Step the playback speed up or down the speed list.
 * @param {number} current The speed now. A speed outside the list snaps to the nearest
 *   listed speed first, and an unknown speed counts as 1.
 * @param {number} direction Positive for faster, negative for slower.
 * @returns {number} The next speed, held at the ends of the list.
 */
export function stepSpeed(current, direction) {
  const speed = isFiniteNumber(current) ? current : 1;
  let nearest = 0;
  for (let index = 1; index < SPEEDS.length; index += 1) {
    if (Math.abs(SPEEDS[index] - speed) < Math.abs(SPEEDS[nearest] - speed)) {
      nearest = index;
    }
  }
  const step = direction > 0 ? 1 : -1;
  return SPEEDS[clamp(nearest + step, 0, SPEEDS.length - 1)];
}

/**
 * Keep a seek target inside the video.
 * @param {number} time The wanted time in seconds.
 * @param {number} duration The video duration in seconds, or NaN while unknown.
 * @returns {number} The time limited to 0 and the duration. An unknown duration only
 *   blocks negative time, and a time that is not finite gives 0.
 */
export function clampTime(time, duration) {
  if (!isFiniteNumber(time)) {
    return 0;
  }
  const high = isFiniteNumber(duration) && duration > 0 ? duration : Infinity;
  return clamp(time, 0, high);
}

/**
 * Choose where a reopened video starts.
 * @param {?(number|string)} saved The position stored for this video, in seconds.
 *   localStorage returns strings, so a numeric string is accepted.
 * @param {number} duration The video duration in seconds, or NaN while unknown.
 * @returns {number} The saved position minus 2 seconds, never below 0. A missing or
 *   unreadable position, or one within 3 seconds of the end, gives 0.
 */
export function resumePosition(saved, duration) {
  const position = typeof saved === 'string' && saved.trim() !== '' ? Number(saved) : saved;
  if (!isFiniteNumber(position) || position <= 0) {
    return 0;
  }
  if (isFiniteNumber(duration) && position >= duration - RESUME_END_GUARD_SECONDS) {
    return 0;
  }
  return Math.max(0, position - RESUME_REWIND_SECONDS);
}

/**
 * Format a file size for the video list.
 * @param {number} bytes Size in bytes.
 * @returns {string} A size such as "999 B", "1.5 KB", "50 MB", or "1.2 GB". One decimal
 *   shows below 10 units. A value that is not a number from 0 up gives "".
 */
export function formatBytes(bytes) {
  if (!isFiniteNumber(bytes) || bytes < 0) {
    return '';
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = unit === 0 || value >= 10 ? String(Math.round(value)) : value.toFixed(1);
  return `${text} ${units[unit]}`;
}

/**
 * Format a fraction as a whole percent.
 * @param {number} fraction A number from 0 to 1.
 * @returns {string} "0%" to "100%". Values outside the range are pulled in, and a value
 *   that is not a number gives "0%".
 */
export function percentText(fraction) {
  const safe = isFiniteNumber(fraction) ? clamp(fraction, 0, 1) : 0;
  return `${Math.round(safe * 100)}%`;
}

/**
 * Word the status badge of a video row.
 * @param {string} status "new", "in progress", or "done" from the catalog.
 * @param {number} sightings How many sightings the video has.
 * @returns {string} "new", or the status followed by the count, such as "in progress · 3".
 */
export function statusLabel(status, sightings) {
  if (status !== 'in progress' && status !== 'done') {
    return 'new';
  }
  const count = isFiniteNumber(sightings) && sightings > 0 ? Math.floor(sightings) : 0;
  return `${status} · ${count}`;
}

/**
 * Split a TCRMP file name so the list can dim the part every video in a folder shares.
 * @param {string} name A file name such as "TCRMP20241022_video_FLC_T1.MP4".
 * @returns {{lead: string, main: string}} lead "TCRMP20241022_video_" and main "FLC_T1.MP4".
 *   A name outside the pattern comes back whole in `main` with an empty `lead`.
 */
export function splitVideoName(name) {
  const text = typeof name === 'string' ? name : '';
  const match = /^(TCRMP\d{8}_video_)(.+)$/i.exec(text);
  return match ? { lead: match[1], main: match[2] } : { lead: '', main: text };
}

/**
 * Word the format badge of a video row.
 * @param {boolean} playable True when Chrome plays the file extension directly.
 * @param {string} converted The conversion state: "none", "queued", "running", "done", or "failed".
 * @returns {{text: string, kind: string}} The badge text, and a kind for styling:
 *   "plays" (ready to play), "convert" (conversion needed or under way), or "failed".
 */
export function formatLabel(playable, converted) {
  if (converted === 'failed') {
    return { text: 'conversion failed', kind: 'failed' };
  }
  if (converted === 'done') {
    return { text: 'converted', kind: 'plays' };
  }
  if (converted === 'running') {
    return { text: 'converting', kind: 'convert' };
  }
  if (converted === 'queued') {
    return { text: 'conversion queued', kind: 'convert' };
  }
  return playable
    ? { text: 'plays now', kind: 'plays' }
    : { text: 'needs conversion', kind: 'convert' };
}

/**
 * Read the progress of a queued background job from a server answer.
 * @param {string} kind "prefetch" (answer {cached}) or "convert" (answer {state, progress, message}).
 * @param {?object} answer The JSON the server sent.
 * @returns {{state: string, fraction: number, message: string}} state is "working", "ready",
 *   or "failed"; fraction runs from 0 to 1; message carries the failure text.
 * @throws {RangeError} When the kind is unknown.
 */
export function queueProgress(kind, answer) {
  const data = answer || {};
  if (kind === 'prefetch') {
    const fraction = isFiniteNumber(data.cached) ? clamp(data.cached, 0, 1) : 0;
    return { state: fraction >= 1 ? 'ready' : 'working', fraction, message: '' };
  }
  if (kind === 'convert') {
    const fraction = isFiniteNumber(data.progress) ? clamp(data.progress, 0, 1) : 0;
    const message = typeof data.message === 'string' ? data.message : '';
    if (data.state === 'done') {
      return { state: 'ready', fraction: 1, message };
    }
    return { state: data.state === 'failed' ? 'failed' : 'working', fraction, message };
  }
  throw new RangeError(`queueProgress: kind must be "prefetch" or "convert", got ${JSON.stringify(kind)}`);
}

/**
 * Split a folder prefix into breadcrumbs.
 * @param {string} prefix The folder prefix, such as "TCRMP_video_ondeck/2024Annual/".
 * @param {string} root The root prefix, such as "TCRMP_video_ondeck/".
 * @returns {Array<{name: string, prefix: string}>} The root crumb first, then one crumb
 *   per folder below it. A prefix outside the root gives the root crumb alone.
 */
export function breadcrumbs(prefix, root) {
  const crumbs = [{ name: ROOT_CRUMB_NAME, prefix: root }];
  if (typeof prefix !== 'string' || !prefix.startsWith(root)) {
    return crumbs;
  }
  let walked = root;
  for (const part of prefix.slice(root.length).split('/')) {
    if (part !== '') {
      walked += `${part}/`;
      crumbs.push({ name: part, prefix: walked });
    }
  }
  return crumbs;
}

/**
 * Keep the folders or videos whose name holds every search word.
 * @param {Array<{name: string}>} items Folder or video entries.
 * @param {string} query Search words separated by spaces. Case is ignored.
 * @returns {Array<{name: string}>} A new array in the same order. An empty query keeps everything.
 * @throws {TypeError} When items is not an array.
 */
export function filterByName(items, query) {
  if (!Array.isArray(items)) {
    throw new TypeError(`filterByName: items must be an array, got ${typeof items}`);
  }
  const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  return items.filter((item) => {
    const name = String(item.name).toLowerCase();
    return words.every((word) => name.includes(word));
  });
}

/**
 * Find the video that follows the current one in a list.
 * @param {Array<{key: string}>} videos The video entries in display order.
 * @param {?string} currentKey The key of the open video, or null.
 * @returns {?{key: string}} The next entry, the first entry when the current key is not
 *   in the list, or null when the list is empty or the current video is the last.
 */
export function nextVideo(videos, currentKey) {
  if (!Array.isArray(videos) || videos.length === 0) {
    return null;
  }
  const index = videos.findIndex((video) => video.key === currentKey);
  if (index === -1) {
    return videos[0];
  }
  return index + 1 < videos.length ? videos[index + 1] : null;
}

/**
 * Find the sighting that was saved last, which is the one the Z key removes.
 * @param {Array<Object<string, string>>} rows Observation rows of one video.
 * @returns {?Object<string, string>} The row with the highest ID number, or null when no
 *   row carries a readable ID.
 */
export function lastSighting(rows) {
  let best = null;
  let bestNumber = -Infinity;
  for (const row of rows || []) {
    const number = idNumber(row.ID);
    if (number > bestNumber) {
      best = row;
      bestNumber = number;
    }
  }
  return best;
}

/**
 * Order sightings along the video timeline.
 * @param {Array<Object<string, string>>} rows Observation rows of one video.
 * @returns {Array<Object<string, string>>} A new array sorted by TimestampSeconds, then by ID number.
 */
export function sortRowsByTime(rows) {
  return rows.slice().sort((left, right) => {
    const byTime = Number(left.TimestampSeconds) - Number(right.TimestampSeconds);
    return byTime !== 0 ? byTime : idNumber(left.ID) - idNumber(right.ID);
  });
}

/**
 * Collect the saved marks near a video time, so the overlay can show what is
 * already logged in the frame on screen.
 * @param {Array<Object<string, string>>} rows Observation rows of one video.
 * @param {number} time The video time in seconds.
 * @param {number} windowSeconds How far from `time` a sighting may sit.
 * @returns {Array<{id: string, code: string, point: {x: number, y: number}, box: ?{x: number, y: number, w: number, h: number}}>}
 *   The marks inside the window. Rows with unreadable numbers are left out.
 */
export function marksNear(rows, time, windowSeconds) {
  if (!isFiniteNumber(time)) {
    return [];
  }
  const marks = [];
  for (const row of rows || []) {
    const seconds = Number(row.TimestampSeconds);
    const point = { x: Number(row.PointX), y: Number(row.PointY) };
    if (row.TimestampSeconds === '' || !Number.isFinite(seconds)
      || Math.abs(seconds - time) > windowSeconds
      || row.PointX === '' || row.PointY === ''
      || !Number.isFinite(point.x) || !Number.isFinite(point.y)) {
      continue;
    }
    const cells = [row.BoxX, row.BoxY, row.BoxW, row.BoxH];
    const numbers = cells.map(Number);
    const hasBox = cells.every((cell) => cell !== '' && cell != null) && numbers.every(Number.isFinite);
    marks.push({
      id: row.ID,
      code: row.SpeciesCode,
      point,
      box: hasBox ? { x: numbers[0], y: numbers[1], w: numbers[2], h: numbers[3] } : null,
    });
  }
  return marks;
}

/**
 * Close a piece of server text with a period, so a sentence that follows it reads cleanly.
 * @param {*} text The server's error text, or any value that prints as text.
 * @returns {string} The trimmed text, with a period added unless it already ends in a
 *   period, a question mark, or an exclamation mark. A missing or empty text gives "".
 */
export function endSentence(text) {
  const trimmed = text === undefined || text === null ? '' : String(text).trim();
  if (trimmed === '' || /[.?!]$/.test(trimmed)) {
    return trimmed;
  }
  return `${trimmed}.`;
}

/**
 * Take the base64 text out of a canvas data URL.
 * @param {string} dataUrl A string such as "data:image/png;base64,AAAA".
 * @returns {string} The base64 part. A string without a data URL header comes back unchanged.
 * @throws {TypeError} When the value is not a string.
 * @throws {RangeError} When the data URL is empty, which a zero-size canvas produces.
 */
export function stripDataUrl(dataUrl) {
  if (typeof dataUrl !== 'string') {
    throw new TypeError(`stripDataUrl: expected a string, got ${typeof dataUrl}`);
  }
  if (!dataUrl.startsWith('data:')) {
    return dataUrl;
  }
  const comma = dataUrl.indexOf(',');
  const body = comma === -1 ? '' : dataUrl.slice(comma + 1);
  if (body === '') {
    throw new RangeError('stripDataUrl: the image is empty, so the canvas had no pixels');
  }
  return body;
}
