/**
 * Pure calculations for sponge tracks on the page.
 *
 * The server follows each sighting through its video and answers GET /api/tracks with one
 * track per sighting: {points: [[t, x, y, score], ...], box, anchor_time, updated_at}, with
 * t in seconds of video, strictly increasing, and x and y fractions of the frame. The page
 * draws a tracked sighting where its track puts it at the time on screen, and a sighting
 * without a track where it was clicked, within 0.6 s of its time. Nothing in this file
 * touches the DOM, the network, or the clock, so every function runs under `node --test`
 * (see tests/test_track_math.mjs).
 */

import { marksNear } from './geometry.js';

/** Points further apart in time than this are not joined, so no mark is drawn between them. */
export const TRACK_GAP_SECONDS = 0.5;

/** Each track point holds the time, x, y, and the score. */
const POINT_FIELDS = 4;

/**
 * Tell whether a value is a finite number (not a numeric string, not NaN).
 * @param {*} value Anything.
 * @returns {boolean} True for a finite number.
 */
function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

/**
 * Tell whether a value has the shape of a track: a points list of [t, x, y, score] with
 * finite numbers and strictly increasing times.
 * @param {*} track Anything.
 * @returns {boolean} True for a usable track.
 */
function isTrack(track) {
  if (!track || typeof track !== 'object' || !Array.isArray(track.points) || track.points.length === 0) {
    return false;
  }
  let previous = -Infinity;
  for (const point of track.points) {
    if (!Array.isArray(point) || point.length !== POINT_FIELDS || !point.every(isFiniteNumber) || point[0] <= previous) {
      return false;
    }
    previous = point[0];
  }
  return true;
}

/**
 * Find where a tracked sponge sits at a video time.
 * @param {{points: Array<Array<number>>}} track A track from the server.
 * @param {number} time The video time in seconds.
 * @returns {?{x: number, y: number}} The position in frame fractions, linearly interpolated
 *   between the two points around the time, or null when the time lies outside the track or
 *   inside a gap longer than TRACK_GAP_SECONDS.
 * @throws {TypeError} When the track has no points list with at least one point, or the
 *   time is not a finite number.
 */
export function positionAt(track, time) {
  if (!track || typeof track !== 'object' || !Array.isArray(track.points) || track.points.length === 0) {
    throw new TypeError(`positionAt: track must be an object with a non-empty points list, got ${JSON.stringify(track)}`);
  }
  if (!isFiniteNumber(time)) {
    throw new TypeError(`positionAt: time must be a finite number, got ${String(time)}`);
  }
  const points = track.points;
  const last = points.length - 1;
  if (time < points[0][0] || time > points[last][0]) {
    return null;
  }
  // The first point at or after the time, by binary search.
  let low = 0;
  let high = last;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (points[middle][0] < time) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  const after = points[low];
  if (after[0] === time) {
    return { x: after[1], y: after[2] };
  }
  const before = points[low - 1];
  const span = after[0] - before[0];
  if (span > TRACK_GAP_SECONDS) {
    return null;
  }
  const share = (time - before[0]) / span;
  return {
    x: before[1] + (after[1] - before[1]) * share,
    y: before[2] + (after[2] - before[2]) * share,
  };
}

/**
 * Collect the marks the overlay draws at a video time. A row whose track covers the time is
 * drawn where the track puts it, with its box moved along and kept at its size. Every other
 * row keeps the old rule: drawn where it was clicked, within windowSeconds of its time.
 * @param {Array<Object<string, string>>} rows Observation rows of the open video.
 * @param {Object<string, object>} tracks Tracks by sighting ID, from readTrackAnswer.
 * @param {number} time The video time on screen in seconds.
 * @param {number} windowSeconds How far from its time a row without a covering track shows.
 * @returns {Array<{id: string, code: string, status: string, point: {x: number, y: number},
 *   box: ?{x: number, y: number, w: number, h: number}, tracked: boolean}>} The marks, in row
 *   order. Rows with unreadable cells are left out, as marksNear leaves them out.
 * @throws {TypeError} When rows is not a list or tracks is not a plain object.
 */
export function marksAt(rows, tracks, time, windowSeconds) {
  if (!Array.isArray(rows)) {
    throw new TypeError(`marksAt: rows must be a list, got ${JSON.stringify(rows)}`);
  }
  if (!tracks || typeof tracks !== 'object' || Array.isArray(tracks)) {
    throw new TypeError(`marksAt: tracks must be an object of tracks by ID, got ${JSON.stringify(tracks)}`);
  }
  if (!isFiniteNumber(time)) {
    return [];
  }
  const marks = [];
  for (const row of rows) {
    const seconds = Number(row.TimestampSeconds);
    // A window of 0 at the row's own time reads the row's cells with the rules of marksNear.
    const [mark] = marksNear([row], seconds, 0);
    if (!mark) {
      continue;
    }
    const track = Object.prototype.hasOwnProperty.call(tracks, row.ID) ? tracks[row.ID] : null;
    const at = track ? positionAt(track, time) : null;
    if (at) {
      const dx = at.x - mark.point.x;
      const dy = at.y - mark.point.y;
      const box = mark.box ? { x: mark.box.x + dx, y: mark.box.y + dy, w: mark.box.w, h: mark.box.h } : null;
      marks.push({ ...mark, point: at, box, tracked: true });
    } else if (Math.abs(seconds - time) <= windowSeconds) {
      marks.push({ ...mark, tracked: false });
    }
  }
  return marks;
}

/**
 * Find the drawn mark a click hits: the nearest one whose center lies within the reach.
 * @param {Array<{id: string, x: number, y: number}>} drawn The marks as drawn, with their
 *   centers in stage pixels.
 * @param {{x: number, y: number}} spot The click in stage pixels.
 * @param {number} reach The largest distance that still counts, in pixels.
 * @returns {?string} The ID of the hit mark, or null. Of two marks at the same distance the
 *   one drawn later wins, because it sits on top.
 * @throws {TypeError} When drawn is not a list, the spot lacks finite x and y, or the reach is
 *   not a number of 0 or more.
 */
export function nearestMark(drawn, spot, reach) {
  if (!Array.isArray(drawn)) {
    throw new TypeError(`nearestMark: drawn must be a list, got ${JSON.stringify(drawn)}`);
  }
  if (!spot || !isFiniteNumber(spot.x) || !isFiniteNumber(spot.y)) {
    throw new TypeError(`nearestMark: spot must have finite x and y, got ${JSON.stringify(spot)}`);
  }
  if (!isFiniteNumber(reach) || reach < 0) {
    throw new TypeError(`nearestMark: reach must be a number of 0 or more, got ${String(reach)}`);
  }
  let nearest = null;
  let best = reach;
  for (const mark of drawn) {
    const distance = Math.hypot(mark.x - spot.x, mark.y - spot.y);
    if (distance <= best) {
      nearest = mark.id;
      best = distance;
    }
  }
  return nearest;
}

/**
 * Find the drawn mark a click lands on: first any mark whose tag or box holds the click (the
 * nearest such center wins), else the nearest center within the reach.
 * @param {Array<{id: string, x: number, y: number, areas: ?Array<{x: number, y: number, w: number, h: number}>}>} drawn
 *   The marks as last drawn, in stage pixels; areas are the tag and box rectangles.
 * @param {{x: number, y: number}} spot The click in stage pixels.
 * @param {number} reach The largest distance from a center that still counts, in pixels.
 * @returns {?string} The ID of the mark, or null.
 * @throws {TypeError} When drawn is not a list, the spot lacks finite x and y, or reach is negative.
 */
export function markAt(drawn, spot, reach) {
  if (!Array.isArray(drawn)) {
    throw new TypeError(`markAt: drawn must be a list, got ${JSON.stringify(drawn)}`);
  }
  if (!spot || !isFiniteNumber(spot.x) || !isFiniteNumber(spot.y)) {
    throw new TypeError(`markAt: spot must have finite x and y, got ${JSON.stringify(spot)}`);
  }
  if (!isFiniteNumber(reach) || reach < 0) {
    throw new TypeError(`markAt: reach must be a number of 0 or more, got ${String(reach)}`);
  }
  const inside = (area) => spot.x >= area.x && spot.x <= area.x + area.w && spot.y >= area.y && spot.y <= area.y + area.h;
  let hit = null;
  let best = Infinity;
  for (const mark of drawn) {
    if (Array.isArray(mark.areas) && mark.areas.some(inside)) {
      const distance = Math.hypot(mark.x - spot.x, mark.y - spot.y);
      if (distance < best) {
        hit = mark.id;
        best = distance;
      }
    }
  }
  return hit !== null ? hit : nearestMark(drawn, spot, reach);
}

/**
 * Read the answer of GET /api/tracks.
 * @param {*} answer The parsed JSON: {tracks: {ID: track}, pending: [ID, ...]}.
 * @returns {{tracks: Object<string, object>, pending: Array<string>}} The usable tracks, and
 *   the IDs whose tracks are still on their way. A malformed track or ID is left out, so one
 *   damaged file cannot break the overlay; an answer without pending waits for nothing.
 */
export function readTrackAnswer(answer) {
  const tracks = {};
  const given = answer && answer.tracks;
  if (given && typeof given === 'object' && !Array.isArray(given)) {
    for (const [id, track] of Object.entries(given)) {
      if (isTrack(track)) {
        tracks[id] = track;
      }
    }
  }
  const pending = answer && Array.isArray(answer.pending) ? answer.pending.filter((id) => typeof id === 'string') : [];
  return { tracks, pending };
}
