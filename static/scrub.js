/**
 * Hold an arrow key to rewind or fast-forward the video.
 *
 * A held Right arrow plays the video at SCRUB_RATE. Chrome cannot play backward, so a held
 * Left arrow pauses the video and seeks back with the clock, issuing the next seek only after
 * the last one landed, so every step shows a frame. Releasing the key puts the video back the
 * way it was: the same speed, and paused or playing as before.
 */

/** Video seconds per second of holding, both directions. */
export const SCRUB_RATE = 4;

const MS_PER_SECOND = 1000;

/**
 * Throw a TypeError unless a value is a finite number.
 * @param {*} value The value to check.
 * @param {string} name The argument name for the message.
 * @throws {TypeError} When the value is not a finite number.
 */
function requireFinite(value, name) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new TypeError(`${name}: expected a finite number, got ${String(value)}`);
  }
}

/**
 * Work out where a rewind should be after the key was held for a while.
 * @param {number} startTime Video time when the key went down, in seconds.
 * @param {number} heldMs Milliseconds the key has been held, 0 or more.
 * @param {number} rate Video seconds per second of holding, above 0.
 * @returns {number} The target time in seconds, never below 0.
 * @throws {TypeError} When an argument is not a finite number, heldMs is negative, or rate
 *   is not above 0.
 */
export function rewindTarget(startTime, heldMs, rate) {
  requireFinite(startTime, 'startTime');
  requireFinite(heldMs, 'heldMs');
  requireFinite(rate, 'rate');
  if (heldMs < 0) {
    throw new TypeError(`heldMs: expected 0 or more, got ${heldMs}`);
  }
  if (rate <= 0) {
    throw new TypeError(`rate: expected a number above 0, got ${rate}`);
  }
  return Math.max(0, startTime - (heldMs / MS_PER_SECOND) * rate);
}

/**
 * Create the scrubber for one video element.
 * @param {HTMLVideoElement} video The video to move.
 * @param {{now?: Function, schedule?: Function, cancel?: Function}} [hooks] The clock and the
 *   frame scheduler. Tests pass manual ones; the page uses performance.now and
 *   requestAnimationFrame.
 * @returns {{start: Function, stop: Function, active: Function}} start(direction) begins a
 *   hold (1 forward, -1 back) and ignores a second start while one runs; stop() ends it and
 *   restores the video; active() tells whether a hold runs.
 */
export function createScrubber(video, hooks = {}) {
  const now = hooks.now || (() => performance.now());
  const schedule = hooks.schedule || ((callback) => window.requestAnimationFrame(callback));
  const cancel = hooks.cancel || ((id) => window.cancelAnimationFrame(id));
  let run = null;

  /** Take one rewind step: seek back with the clock unless the last seek is still running. */
  function rewindStep() {
    if (!run) {
      return;
    }
    if (!video.seeking) {
      const target = rewindTarget(run.startTime, now() - run.startedAt, SCRUB_RATE);
      if (target < video.currentTime) {
        video.currentTime = target;
      }
    }
    run.frame = schedule(rewindStep);
  }

  return {
    /**
     * Begin holding: fast-forward (1) or rewind (-1). A start while a hold runs is ignored,
     * so key repeats and a second arrow change nothing.
     * @param {number} direction 1 for forward, -1 for back.
     * @throws {TypeError} When direction is not 1 or -1.
     */
    start(direction) {
      if (direction !== 1 && direction !== -1) {
        throw new TypeError(`direction: expected 1 or -1, got ${String(direction)}`);
      }
      if (run) {
        return;
      }
      run = {
        direction,
        wasPaused: video.paused,
        rate: video.playbackRate,
        startTime: video.currentTime,
        startedAt: now(),
        frame: null,
      };
      if (direction === 1) {
        video.playbackRate = SCRUB_RATE;
        const attempt = video.play();
        if (attempt && typeof attempt.catch === 'function') {
          attempt.catch(() => {
            // A blocked or interrupted play leaves the video paused; release restores it.
          });
        }
        return;
      }
      video.pause();
      rewindStep();
    },

    /** End the hold and put the video back: the old speed, and paused or playing as before. */
    stop() {
      if (!run) {
        return;
      }
      const ended = run;
      run = null;
      if (ended.frame !== null) {
        cancel(ended.frame);
      }
      video.playbackRate = ended.rate;
      if (ended.wasPaused) {
        video.pause();
      } else if (video.paused && !video.ended) {
        const attempt = video.play();
        if (attempt && typeof attempt.catch === 'function') {
          attempt.catch(() => {
            // An interrupted play leaves the video paused. Space starts it.
          });
        }
      }
    },

    /**
     * Tell whether a hold runs.
     * @returns {boolean} True between start and stop.
     */
    active() {
      return run !== null;
    },
  };
}
