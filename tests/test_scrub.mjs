// Tests for static/scrub.js: hold an arrow key to rewind or fast-forward the video.
// Run with: node --test tests/test_scrub.mjs
//
// The project has no package.json, so node would read static/*.js as CommonJS and refuse
// their `export` statements. The test imports scrub.js through a data: URL, which node
// always treats as an ES module, as the geometry and wheel tests do.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, '..', 'static', 'scrub.js'), 'utf8');
const { SCRUB_RATE, createScrubber, rewindTarget } = await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
);

/**
 * Build a stand-in for a video element with the properties the scrubber touches.
 * @param {{time?: number, paused?: boolean, rate?: number, ended?: boolean}} [start] The starting state.
 * @returns {object} The fake video, which records play and pause calls.
 */
function fakeVideo(start = {}) {
  const video = {
    currentTime: start.time ?? 10,
    paused: start.paused ?? true,
    playbackRate: start.rate ?? 1.5,
    ended: start.ended ?? false,
    seeking: false,
    calls: [],
    play() {
      this.paused = false;
      this.calls.push('play');
      return Promise.resolve();
    },
    pause() {
      this.paused = true;
      this.calls.push('pause');
    },
  };
  return video;
}

/**
 * Build a manual clock and frame scheduler, so a test decides when time passes.
 * @returns {{hooks: object, advance: Function, pending: Function}} The hooks for the
 *   scrubber, a function that moves the clock and runs the scheduled frame, and a count of
 *   scheduled frames.
 */
function manualFrames() {
  let clock = 0;
  let queued = null;
  let nextId = 1;
  const hooks = {
    now: () => clock,
    schedule: (callback) => {
      queued = { id: nextId, callback };
      nextId += 1;
      return queued.id;
    },
    cancel: (id) => {
      if (queued && queued.id === id) {
        queued = null;
      }
    },
  };
  return {
    hooks,
    advance(ms) {
      clock += ms;
      const due = queued;
      queued = null;
      if (due) {
        due.callback();
      }
    },
    pending: () => (queued ? 1 : 0),
  };
}

test('rewindTarget moves back at the rate and stops at the start of the video', () => {
  assert.equal(rewindTarget(10, 0, 4), 10);
  assert.equal(rewindTarget(10, 500, 4), 8);
  assert.equal(rewindTarget(10, 1000, 4), 6);
  assert.equal(rewindTarget(1, 1000, 4), 0);
});

test('rewindTarget rejects values that are not finite or are negative', () => {
  assert.throws(() => rewindTarget('10', 0, 4), /startTime/);
  assert.throws(() => rewindTarget(10, Number.NaN, 4), /heldMs/);
  assert.throws(() => rewindTarget(10, -5, 4), /heldMs/);
  assert.throws(() => rewindTarget(10, 5, 0), /rate/);
});

test('holding Right plays at the scrub rate, and release restores speed and pause', () => {
  const video = fakeVideo({ paused: true, rate: 1.5 });
  const scrubber = createScrubber(video, manualFrames().hooks);
  scrubber.start(1);
  assert.equal(scrubber.active(), true);
  assert.equal(video.playbackRate, SCRUB_RATE);
  assert.equal(video.paused, false);
  scrubber.stop();
  assert.equal(scrubber.active(), false);
  assert.equal(video.playbackRate, 1.5);
  assert.equal(video.paused, true);
});

test('fast forward from playing keeps playing at the old speed after release', () => {
  const video = fakeVideo({ paused: false, rate: 1.5 });
  const scrubber = createScrubber(video, manualFrames().hooks);
  scrubber.start(1);
  scrubber.stop();
  assert.equal(video.playbackRate, 1.5);
  assert.equal(video.paused, false);
  assert.deepEqual(video.calls.filter((call) => call === 'pause'), []);
});

test('holding Left pauses and seeks back with the clock, waiting out each seek', () => {
  const video = fakeVideo({ time: 10, paused: false, rate: 1.5 });
  const frames = manualFrames();
  const scrubber = createScrubber(video, frames.hooks);
  scrubber.start(-1);
  assert.equal(video.paused, true);
  frames.advance(250);
  assert.equal(video.currentTime, 10 - 0.25 * SCRUB_RATE);
  video.seeking = true;
  frames.advance(250);
  assert.equal(video.currentTime, 10 - 0.25 * SCRUB_RATE, 'no new seek while the last one runs');
  video.seeking = false;
  frames.advance(250);
  assert.equal(video.currentTime, 10 - 0.75 * SCRUB_RATE, 'the next seek catches up with the clock');
  scrubber.stop();
  assert.equal(frames.pending(), 0, 'release cancels the scheduled frame');
  assert.equal(video.paused, false, 'the video plays again because it was playing before');
  assert.equal(video.playbackRate, 1.5);
});

test('rewind stops at the start of the video and keeps waiting there', () => {
  const video = fakeVideo({ time: 0.5 });
  const frames = manualFrames();
  const scrubber = createScrubber(video, frames.hooks);
  scrubber.start(-1);
  frames.advance(1000);
  assert.equal(video.currentTime, 0);
  frames.advance(1000);
  assert.equal(video.currentTime, 0);
  scrubber.stop();
});

test('a second start while scrubbing is ignored, and stop when idle does nothing', () => {
  const video = fakeVideo({ time: 10 });
  const frames = manualFrames();
  const scrubber = createScrubber(video, frames.hooks);
  scrubber.stop();
  assert.deepEqual(video.calls, []);
  scrubber.start(-1);
  scrubber.start(1);
  assert.equal(video.playbackRate, 1.5, 'the held Left keeps control');
  scrubber.stop();
});

test('release after fast-forwarding to the end does not restart the video', () => {
  const video = fakeVideo({ paused: false });
  const scrubber = createScrubber(video, manualFrames().hooks);
  scrubber.start(1);
  video.ended = true;
  video.paused = true;
  scrubber.stop();
  assert.equal(video.calls.filter((call) => call === 'play').length, 1, 'only the fast-forward play');
});

test('start rejects a direction other than 1 or -1', () => {
  const scrubber = createScrubber(fakeVideo(), manualFrames().hooks);
  assert.throws(() => scrubber.start(0), /direction/);
  assert.throws(() => scrubber.start('1'), /direction/);
});
