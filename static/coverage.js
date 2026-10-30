/**
 * Watch tracking and the wording of the coverage grid. This file holds no page state, so
 * the node tests can run it without a browser.
 */

/** A jump larger than this between two clock readings is a seek, not playback. */
export const MAX_STEP_SECONDS = 3;

/** How many unwatched stretches the Done warning names before it shortens the list. */
const MAX_GAPS_NAMED = 3;

/**
 * Join stretches that touch, so a long run of failed saves stays a short list.
 * @param {number[][]} ranges The [start, end] stretches.
 * @returns {number[][]} The joined stretches, sorted by start.
 */
function joinTouching(ranges) {
  const joined = [];
  for (const [start, end] of [...ranges].sort((a, b) => a[0] - b[0])) {
    const previous = joined[joined.length - 1];
    if (previous && start <= previous[1]) {
      previous[1] = Math.max(previous[1], end);
    } else {
      joined.push([start, end]);
    }
  }
  return joined;
}

/**
 * Build the tracker that turns clock readings into watched stretches.
 * @returns {{advance: function(number, boolean): void, breakRun: function(): void,
 *   takeRanges: function(): number[][], restore: function(number[][]): void}} The tracker.
 */
export function createWatchTracker() {
  let finished = [];
  let start = null;
  let last = null;

  /** Move the open stretch into the finished list, when it has any length. */
  const closeRun = () => {
    if (start !== null && last > start) {
      finished.push([start, last]);
    }
    start = null;
    last = null;
  };

  return {
    /**
     * Take one reading of the video clock.
     * @param {number} time The video time in seconds.
     * @param {boolean} counting True when this moment counts as watching.
     */
    advance(time, counting) {
      if (!counting || !Number.isFinite(time)) {
        closeRun();
        return;
      }
      if (last !== null && time >= last && time - last <= MAX_STEP_SECONDS) {
        last = time;
        return;
      }
      closeRun();
      start = time;
      last = time;
    },

    /** End the open stretch: the next reading starts a new one. */
    breakRun() {
      closeRun();
    },

    /**
     * Hand over the stretches so far. The open stretch carries on from the last reading.
     * @returns {number[][]} The [start, end] stretches since the last take.
     */
    takeRanges() {
      const position = last;
      closeRun();
      start = position;
      last = position;
      const taken = joinTouching(finished);
      finished = [];
      return taken;
    },

    /**
     * Put back stretches that could not be saved, for the next take.
     * @param {number[][]} ranges The [start, end] stretches.
     */
    restore(ranges) {
      finished = [...ranges, ...finished];
    },
  };
}

/**
 * Read a time as minutes and seconds, like the clock under the video.
 * @param {number} seconds The time in seconds.
 * @returns {string} The time as MM:SS.
 */
function clock(seconds) {
  const whole = Math.floor(seconds);
  return `${String(Math.floor(whole / 60)).padStart(2, '0')}:${String(whole % 60).padStart(2, '0')}`;
}

/**
 * Word the stretches of a video that were not watched.
 * @param {number[][]} gaps The [start, end] stretches.
 * @returns {string} One sentence with the total and the stretches.
 */
export function gapSentence(gaps) {
  const total = gaps.reduce((sum, [start, end]) => sum + end - start, 0);
  const named = gaps.slice(0, MAX_GAPS_NAMED).map(([start, end]) => `${clock(start)} to ${clock(end)}`);
  const more = gaps.length - named.length;
  const tail = more > 0 ? `, and ${more} more` : '';
  return `You have not watched ${clock(total)} of this video: ${named.join(', ')}${tail}.`;
}

/**
 * Decide what one species cell of the grid says.
 * @param {number} count The labeled sightings of the species in the video.
 * @param {?number} reference The January count, or null without a reference file.
 * @param {{percent: ?number}} watched The video's watched summary.
 * @param {number} review The points in the video still waiting for a species.
 * @returns {{state: string, text: string, reference: string, title: string}} The cell. The
 *   state is tagged, missing, absent, or open.
 */
export function targetCell(count, reference, watched, review) {
  const referenceText = reference === null ? '' : `J${reference}`;
  const january = reference === null ? '' : ` January logged ${reference}.`;
  if (count > 0) {
    return { state: 'tagged', text: String(count), reference: referenceText, title: `You tagged ${count}.${january}` };
  }
  if (reference > 0) {
    return { state: 'missing', text: '0', reference: referenceText, title: `Nothing is tagged yet, and January logged ${reference} here. Check this video for this species.` };
  }
  if (review === 0 && watched.percent === 100) {
    return { state: 'absent', text: 'none', reference: referenceText, title: `Absent: you watched every second and tagged none.${january}` };
  }
  return { state: 'open', text: '0', reference: referenceText, title: `Nothing tagged yet. This video is not fully watched and reviewed.${january}` };
}

/**
 * Shorten a video name to its site and transect.
 * @param {string} name The file name, such as TCRMP20241018_video_BIT_T1.MP4.
 * @returns {string} The label, such as BIT T1, or the name when it has another form.
 */
export function videoLabel(name) {
  const match = /_([A-Za-z0-9]+)_(T\d+)\.[^.]+$/.exec(name);
  return match ? `${match[1]} ${match[2]}` : name;
}

/**
 * Word how much of a video was watched.
 * @param {{percent: ?number}} watched The video's watched summary.
 * @returns {string} A percent, or "no record" when no watched time is saved.
 */
export function watchedText(watched) {
  return watched.percent === null ? 'no record' : `${watched.percent}%`;
}
