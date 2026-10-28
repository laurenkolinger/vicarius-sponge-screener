/**
 * How the page joins VICARIUS: the run it was opened on and the initials the person signed in with.
 *
 * The VICARIUS desktop opens the screener at
 * `http://127.0.0.1:5098/?project_dir=<run folder>&home=<launcher>&initials=<XX>`. The page
 * asks the server for the run's name from project_dir and names that run on every call (the
 * RUN_HEADER header, or `run=` on an image address). The annotator field starts on a valid
 * `initials` parameter, then on what the run remembered, never on a default.
 *
 * Pure functions only, so node --test can run them (tests/test_vicarius_run.mjs).
 */

/** Initials VICARIUS signs a person in with: two to four letters. */
export const INITIALS_PATTERN = /^[A-Za-z]{2,4}$/;

/** The header that names the run on every API call, percent-encoded (a header carries only Latin-1). */
export const RUN_HEADER = 'X-Screener-Run';

/** The query parameter that names the run on an image address. */
export const RUN_PARAM = 'run';

/**
 * Read the signed-in initials from the page address.
 * @param {string} search The address's query string, such as `location.search`.
 * @returns {string} The initials in upper case when `initials` appears exactly once and holds
 *   two to four letters after trimming; otherwise "".
 */
export function initialsFromSearch(search) {
  const all = new URLSearchParams(search || '').getAll('initials');
  const raw = all.length === 1 ? all[0].trim() : '';
  return INITIALS_PATTERN.test(raw) ? raw.toUpperCase() : '';
}

/**
 * Read the run folder VICARIUS opened the page with.
 * @param {string} search The address's query string.
 * @returns {string} The trimmed `project_dir` when it appears exactly once and is not blank;
 *   otherwise "".
 */
export function projectDirFromSearch(search) {
  const all = new URLSearchParams(search || '').getAll('project_dir');
  return all.length === 1 ? all[0].trim() : '';
}

/**
 * Choose the annotator field's starting value.
 * @param {string} fromUrl The initials from initialsFromSearch, or "".
 * @param {string} remembered The annotator the server remembers for this run, or "".
 * @returns {string} fromUrl when set, else remembered, else "" (never a default such as LO).
 */
export function startingInitials(fromUrl, remembered) {
  if (typeof fromUrl === 'string' && fromUrl) {
    return fromUrl;
  }
  return typeof remembered === 'string' ? remembered : '';
}

/**
 * Add the run to an address that cannot carry a header (an image source).
 * @param {string} path The address, with or without a query string.
 * @param {string} run The run folder name, or "" for none.
 * @returns {string} The address with `run=<run>` added, or unchanged when run is "".
 */
export function withRun(path, run) {
  if (!run) {
    return path;
  }
  const joiner = path.includes('?') ? '&' : '?';
  return `${path}${joiner}${RUN_PARAM}=${encodeURIComponent(run)}`;
}

/**
 * Upper-case a text field's value as it is typed, keeping the caret where it was.
 * @param {{value: string, selectionStart: ?number, selectionEnd: ?number,
 *   setSelectionRange: function(number, number): void}} input The text field.
 * @returns {boolean} True when the value changed.
 */
export function upperCaseAsTyped(input) {
  const upper = String(input.value || '').toUpperCase();
  if (upper === input.value) {
    return false;
  }
  const start = input.selectionStart;
  const end = input.selectionEnd;
  input.value = upper;
  if (typeof start === 'number' && typeof end === 'number') {
    input.setSelectionRange(start, end);
  }
  return true;
}
