/**
 * Sponge Screener page logic.
 *
 * The page has two keyboard modes. In screening mode the keys drive the video. A
 * click or a drag on the picture pauses the video, starts a mark, and switches to
 * marking mode, where the number keys choose a pinned species, letters filter the
 * full list, Enter saves, and Escape cancels.
 *
 * Every calculation lives in geometry.js, which has its own node tests. This file
 * wires those calculations to the DOM, the video element, and the HTTP contract of
 * screener/server.py. Server text reaches the DOM through textContent only.
 */

import {
  CROP_SIZE,
  PIN_KEYS,
  QUADRANT_PHRASES,
  SPEEDS,
  anchorPoint,
  binomialParts,
  breadcrumbs,
  clampTime,
  contentRect,
  cropRect,
  endSentence,
  filterByName,
  filterSpecies,
  formatBytes,
  formatClock,
  formatLabel,
  isDrag,
  isUsableBox,
  keyFromEvent,
  lastSighting,
  marksNear,
  mergeTally,
  nextVideo,
  normalizeBox,
  percentText,
  pinKeyFromEvent,
  quadrantOf,
  quadrantRect,
  queueProgress,
  resumePosition,
  sortRowsByTime,
  splitVideoName,
  statusLabel,
  stepSpeed,
  stripDataUrl,
  swapPin,
  tagPosition,
  toNormalized,
  toNormalizedClamped,
  toPixels,
} from './geometry.js';

// ------------------------------------------------------------------ constants

const ROOT_PREFIX = 'TCRMP_video_ondeck/';
const MUTATION_HEADER = 'X-Screener';
const JPEG_QUALITY = 0.95;
const JUMP_SECONDS = 2;
const FRAME_STEP_SECONDS = 1 / 30;
const POLL_MS = 2000;
const NETWORK_RETRY_MS = 3000;
const POSITION_SAVE_MS = 1000;
const SAVED_MARK_WINDOW_SECONDS = 0.6;
const TOAST_MS = 2600;
const ERROR_TOAST_MS = 7000;
const LONG_TOAST_MS = 12000;
const MAX_TOASTS = 4;
const DELETE_ARM_MS = 3000;
const FRESH_ROW_MS = 1600;
const HAVE_CURRENT_DATA = 2;
const MARK_COLOR = '#ff2bd6';
const MARK_INK = '#1f0019';
const QUADRANT_FILL = 'rgba(255, 255, 255, 0.12)';
const CANVAS_FONT = '700 13px ui-monospace, "SF Mono", Menlo, monospace';
const TAG_HEIGHT = 20;
const TICK_REACH = 10;
const SVG_NS = 'http://www.w3.org/2000/svg';
const STORAGE = {
  position: 'screener.position.',
  prefix: 'screener.prefix',
  grid: 'screener.grid',
  speed: 'screener.speed',
};
const IDLE_TEXT = 'Pick a video from the list on the left. Then click a sponge, press its number key, and press Enter.';

// ---------------------------------------------------------------------- state

/**
 * Everything the page knows, as plain data. The end-to-end driver reads this
 * object through window.__screener.state, so it is changed in place and never
 * replaced, and it holds no DOM nodes or timers.
 */
const state = {
  mode: 'screening',
  key: null,
  name: null,
  source: null,
  videoStatus: 'new',
  pending: null,
  saving: false,
  rows: [],
  species: [],
  pins: {},
  annotator: '',
  tallyTarget: 3,
  tally: [],
  tallyTotal: 0,
  tallyError: null,
  speciesError: null,
  prefix: ROOT_PREFIX,
  parent: null,
  stale: false,
  folders: [],
  videos: [],
  catalogLoading: false,
  catalogError: null,
  search: '',
  filterQuery: '',
  filterOpen: false,
  matches: [],
  matchIndex: 0,
  pinCandidate: null,
  grid: true,
  speed: 1,
  conversion: null,
  queue: {},
  status: { kind: 'idle', text: IDLE_TEXT },
  lastToast: null,
};

/** Timers, the live drag, and other values that are not plain page data. */
const runtime = {
  loadToken: 0,
  drag: null,
  convertTimer: null,
  retryTimer: null,
  queueTimer: null,
  positionSavedAt: 0,
  armedDelete: null,
  armedTimer: null,
  freshId: null,
  freshTimer: null,
  scrubbing: false,
  playFailures: 0,
  statusAction: null,
  failedPrefix: ROOT_PREFIX,
  failedRefresh: false,
};

// ------------------------------------------------------------------------ DOM

/**
 * Find a required element of index.html.
 * @param {string} id The element id.
 * @returns {HTMLElement} The element.
 * @throws {Error} When index.html has no element with that id.
 */
function byId(id) {
  const node = document.getElementById(id);
  if (!node) {
    throw new Error(`app.js: index.html has no element with id "${id}", and the page needs it.`);
  }
  return node;
}

const dom = {
  currentVideo: byId('current-video'),
  annotator: byId('annotator'),
  exportButton: byId('export'),
  helpToggle: byId('help-toggle'),
  help: byId('help'),
  helpClose: byId('help-close'),
  staleBadge: byId('stale-badge'),
  breadcrumb: byId('breadcrumb'),
  search: byId('search'),
  refresh: byId('refresh'),
  catalogError: byId('catalog-error'),
  catalogErrorText: byId('catalog-error-text'),
  catalogRetry: byId('catalog-retry'),
  folders: byId('folders'),
  videos: byId('videos'),
  browserEmpty: byId('browser-empty'),
  stage: byId('stage'),
  video: byId('video'),
  overlay: byId('overlay'),
  stageStatus: byId('stage-status'),
  stageStatusText: byId('stage-status-text'),
  stageAction: byId('stage-action'),
  scrub: byId('scrub'),
  scrubMarks: byId('scrub-marks'),
  play: byId('play'),
  playIcon: byId('play-icon'),
  time: byId('time'),
  slower: byId('slower'),
  faster: byId('faster'),
  speed: byId('speed'),
  grid: byId('grid'),
  done: byId('done'),
  doneLabel: byId('done-label'),
  next: byId('next'),
  speciesError: byId('species-error'),
  speciesErrorText: byId('species-error-text'),
  speciesRetry: byId('species-retry'),
  pins: byId('pins'),
  modePill: byId('mode-pill'),
  markSummary: byId('mark-summary'),
  markError: byId('mark-error'),
  filter: byId('filter'),
  matches: byId('matches'),
  note: byId('note'),
  save: byId('save'),
  saveStay: byId('save-stay'),
  cancel: byId('cancel'),
  sightings: byId('sightings'),
  sightingsCount: byId('sightings-count'),
  sightingsEmpty: byId('sightings-empty'),
  tallyTarget: byId('tally-target'),
  tallyRows: byId('tally-rows'),
  tallyEmpty: byId('tally-empty'),
  tallyTotal: byId('tally-total'),
  toasts: byId('toasts'),
};

/**
 * Build an element. Text goes in through textContent, so server text cannot
 * become markup.
 * @param {string} tag The tag name.
 * @param {object} [options] Optional `class`, `text`, `title`, `attrs` (attribute map),
 *   and `data` (dataset map).
 * @param {Array<Node|string>} [children] Nodes or strings to append.
 * @returns {HTMLElement} The new element.
 */
function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.class) {
    node.className = options.class;
  }
  if (options.text !== undefined) {
    node.textContent = options.text;
  }
  if (options.title) {
    node.title = options.title;
  }
  for (const [name, value] of Object.entries(options.attrs || {})) {
    node.setAttribute(name, value);
  }
  for (const [name, value] of Object.entries(options.data || {})) {
    node.dataset[name] = value;
  }
  node.append(...children);
  return node;
}

/**
 * Build an icon that points at a symbol of the sprite in index.html.
 * @param {string} name The symbol name without the "i-" lead, such as "pin".
 * @returns {SVGElement} The icon.
 */
function icon(name) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

/**
 * Read a value from localStorage.
 * @param {string} name The storage name.
 * @returns {?string} The stored text, or null when it is missing or storage is blocked.
 */
function storageGet(name) {
  try {
    return window.localStorage.getItem(name);
  } catch (error) {
    return null;
  }
}

/**
 * Write a value to localStorage. A full or blocked storage is ignored, because the
 * stored values are conveniences and the sightings live on the server.
 * @param {string} name The storage name.
 * @param {string} value The text to store.
 */
function storageSet(name, value) {
  try {
    window.localStorage.setItem(name, value);
  } catch (error) {
    // Nothing to do: the page works without remembered positions.
  }
}

/**
 * Look up a species by code.
 * @param {?string} code A four-letter species code.
 * @returns {?{code: string, name: string, part: string}} The species, or null.
 */
function speciesByCode(code) {
  return state.species.find((item) => item.code === code) || null;
}

// ------------------------------------------------------------ server requests

/** An answer from the server that carries an error status. */
class ApiError extends Error {
  /**
   * @param {string} message The server's error text, or a description of the failure.
   * @param {number} status The HTTP status, or 0 when the server did not answer.
   */
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * Send one request to the local server and read the JSON answer.
 * @param {string} method "GET", "POST", or "DELETE".
 * @param {string} path The path and query string.
 * @param {object} [body] A JSON body for POST.
 * @returns {Promise<object>} The parsed JSON answer.
 * @throws {ApiError} With the server's `error` text when the status is not 2xx, or with a
 *   plain description when the server did not answer or sent something other than JSON.
 */
async function request(method, path, body) {
  const options = { method, headers: {} };
  if (method !== 'GET') {
    options.headers[MUTATION_HEADER] = '1';
  }
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new ApiError(
      `The server did not answer ${method} ${path.split('?')[0]}. Check that screener.py is still running.`,
      0,
    );
  }
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const reason = payload && typeof payload.error === 'string'
      ? payload.error
      : `${method} ${path.split('?')[0]} failed with status ${response.status}.`;
    throw new ApiError(reason, response.status);
  }
  if (payload === null || typeof payload !== 'object') {
    throw new ApiError(`${method} ${path.split('?')[0]} answered with something other than JSON.`, response.status);
  }
  return payload;
}

// --------------------------------------------------------------------- toasts

/**
 * Show a short message above the video.
 * @param {string} message The text to show.
 * @param {string} [kind="ok"] "ok", "info", or "error".
 * @param {number} [duration] Milliseconds on screen. Errors stay longer by default.
 */
function toast(message, kind = 'ok', duration) {
  const text = String(message);
  for (const old of Array.from(dom.toasts.children)) {
    if (old.textContent === text) {
      old.remove();
    }
  }
  while (dom.toasts.children.length >= MAX_TOASTS) {
    dom.toasts.firstElementChild.remove();
  }
  const node = el('div', { class: 'toast', text, data: { kind } });
  dom.toasts.append(node);
  state.lastToast = { message: text, kind };
  const life = duration || (kind === 'error' ? ERROR_TOAST_MS : TOAST_MS);
  window.setTimeout(() => node.remove(), life);
}

// -------------------------------------------------------------- browser panel

/**
 * Load one folder of the catalog and show it.
 * @param {string} prefix The folder prefix to list.
 * @param {boolean} [refresh=false] True asks the server to list the bucket again.
 * @returns {Promise<void>} Settles after the panel shows the folder or the error.
 */
async function loadCatalog(prefix, refresh = false) {
  state.catalogLoading = true;
  state.catalogError = null;
  renderBrowser();
  try {
    const query = `prefix=${encodeURIComponent(prefix)}${refresh ? '&refresh=1' : ''}`;
    const page = await request('GET', `/api/catalog?${query}`);
    state.prefix = typeof page.prefix === 'string' ? page.prefix : prefix;
    state.parent = page.parent || null;
    state.stale = Boolean(page.stale);
    state.folders = Array.isArray(page.folders) ? page.folders : [];
    state.videos = Array.isArray(page.videos) ? page.videos : [];
    storageSet(STORAGE.prefix, state.prefix);
  } catch (error) {
    state.catalogError = `The folder did not load. ${endSentence(error.message)}`;
    runtime.failedPrefix = prefix;
    runtime.failedRefresh = refresh;
  } finally {
    state.catalogLoading = false;
    renderBrowser();
  }
}

/**
 * Open another folder and clear the search box.
 * @param {string} prefix The folder prefix to open.
 */
function navigate(prefix) {
  state.search = '';
  dom.search.value = '';
  loadCatalog(prefix);
}

/**
 * List the videos that pass the search box, in display order.
 * @returns {Array<object>} Catalog video entries.
 */
function visibleVideos() {
  return filterByName(state.videos, state.search);
}

/**
 * Find a catalog video entry by key.
 * @param {?string} key The S3 key.
 * @returns {?object} The entry of the folder on screen, or null.
 */
function videoEntry(key) {
  return state.videos.find((video) => video.key === key) || null;
}

/** Draw the breadcrumb, the notices, the folder rows, and the video rows. */
function renderBrowser() {
  renderBreadcrumb();
  dom.staleBadge.hidden = !state.stale;
  dom.catalogError.hidden = !state.catalogError;
  dom.catalogErrorText.textContent = state.catalogError || '';

  const folders = filterByName(state.folders, state.search);
  const videos = visibleVideos();
  const folderNodes = folders.map(folderNode);
  if (state.parent) {
    folderNodes.unshift(upNode(state.parent));
  }
  dom.folders.replaceChildren(...folderNodes);
  dom.videos.replaceChildren(...videos.map(videoNode));

  let emptyText = '';
  if (state.catalogLoading) {
    emptyText = 'Loading this folder.';
  } else if (folders.length === 0 && videos.length === 0 && !state.catalogError) {
    emptyText = state.search.trim() === ''
      ? 'This folder holds no videos.'
      : `Nothing in this folder matches "${state.search.trim()}".`;
  }
  dom.browserEmpty.textContent = emptyText;
  dom.browserEmpty.hidden = emptyText === '';
}

/** Draw one button per folder of the current prefix. */
function renderBreadcrumb() {
  const crumbs = breadcrumbs(state.prefix, ROOT_PREFIX);
  const nodes = [];
  crumbs.forEach((crumb, index) => {
    const last = index === crumbs.length - 1;
    const button = el('button', {
      class: 'crumb',
      text: crumb.name,
      title: `You open the folder ${crumb.name}.`,
      attrs: { type: 'button' },
      data: { prefix: crumb.prefix },
    });
    if (last) {
      button.setAttribute('aria-current', 'page');
    }
    nodes.push(button);
    if (!last) {
      nodes.push(el('span', { class: 'crumb-sep', text: '/', attrs: { 'aria-hidden': 'true' } }));
    }
  });
  dom.breadcrumb.replaceChildren(...nodes);
}

/**
 * Build the row that goes up one folder.
 * @param {string} parent The parent prefix.
 * @returns {HTMLElement} The list item.
 */
function upNode(parent) {
  const crumbs = breadcrumbs(parent, ROOT_PREFIX);
  const name = crumbs[crumbs.length - 1].name;
  const button = el('button', {
    class: 'folder-row folder-up',
    title: `You go up one folder, to ${name}.`,
    attrs: { type: 'button' },
    data: { prefix: parent },
  }, [icon('up'), el('span', { text: `Up to ${name}` })]);
  return el('li', {}, [button]);
}

/**
 * Build one folder row.
 * @param {{prefix: string, name: string}} folder A catalog folder.
 * @returns {HTMLElement} The list item.
 */
function folderNode(folder) {
  const button = el('button', {
    class: 'folder-row',
    title: `You open the folder ${folder.name}.`,
    attrs: { type: 'button' },
    data: { prefix: folder.prefix },
  }, [icon('folder'), el('span', { text: folder.name })]);
  return el('li', {}, [button]);
}

/**
 * Build one video row: the name, the status and format badges, and the queue button.
 * @param {object} video A catalog video entry.
 * @returns {HTMLElement} The list item.
 */
function videoNode(video) {
  const parts = splitVideoName(video.name);
  const nameNodes = [];
  if (parts.lead) {
    nameNodes.push(el('span', { class: 'video-name-dim', text: parts.lead }));
  }
  nameNodes.push(el('span', { class: 'video-name-main', text: parts.main }));

  const format = formatLabel(Boolean(video.playable), video.converted);
  const status = video.status === 'in progress' || video.status === 'done' ? video.status : 'new';
  const meta = el('span', { class: 'video-meta' }, [
    el('span', {
      class: 'badge',
      text: statusLabel(status, video.sightings),
      title: 'new: nobody has opened this video. in progress: it was opened, and the number counts its sightings. done: you marked it fully screened.',
      data: { status },
    }),
    el('span', {
      class: 'format',
      text: format.text,
      title: 'plays now: Chrome plays this format directly. needs conversion: the server converts the file first, which takes a few minutes.',
      data: { format: format.kind },
    }),
  ]);

  const size = formatBytes(video.size);
  const open = el('button', {
    class: 'video-open',
    title: `You open ${video.name}${size ? ` (${size})` : ''}. It plays from where you left it, minus 2 seconds.`,
    attrs: { type: 'button' },
    data: { key: video.key },
  }, [el('span', { class: 'video-name' }, nameNodes), meta]);

  const item = el('li', {
    class: 'video-row',
    data: { key: video.key, status, playable: String(Boolean(video.playable)), lead: String(parts.lead !== '') },
  }, [open, queueNode(video)]);
  if (video.key === state.key) {
    item.setAttribute('aria-current', 'true');
  }
  return item;
}

/**
 * Build the queue button of a video row, with its progress when the video is queued.
 * @param {object} video A catalog video entry.
 * @returns {HTMLElement} The button.
 */
function queueNode(video) {
  const job = state.queue[video.key];
  const verb = video.playable ? 'download' : 'conversion';
  let title = video.playable
    ? 'You queue this video for background download, so it plays later without waiting.'
    : 'You queue this video for background conversion. The server downloads it and converts it so Chrome can play it.';
  let content = icon('queue');
  let label = `Queue ${video.name} for background ${verb}`;
  if (job && job.state === 'working') {
    content = percentText(job.fraction);
    label = `Background ${verb} of ${video.name}: ${percentText(job.fraction)}`;
    title = `The server is working on this video in the background: ${percentText(job.fraction)}.`;
  } else if (job && job.state === 'ready') {
    content = icon('check');
    label = `${video.name} is ready on this Mac`;
    title = 'This video is ready on this Mac. It plays without waiting.';
  } else if (job && job.state === 'failed') {
    content = '!';
    label = `Background ${verb} of ${video.name} failed`;
    title = `The background ${verb} failed. ${endSentence(job.message)} You can click to try again.`;
  }
  return el('button', {
    class: 'button button-icon video-queue',
    title,
    attrs: { type: 'button', 'aria-label': label },
    data: { key: video.key, state: job ? job.state : 'idle' },
  }, [content]);
}

/**
 * React to a click in the video list: open a video or queue it.
 * @param {MouseEvent} event The click.
 */
function onVideoListClick(event) {
  const queueButton = event.target.closest('.video-queue');
  if (queueButton) {
    queueVideo(queueButton.dataset.key);
    return;
  }
  const openButton = event.target.closest('.video-open');
  if (openButton) {
    const entry = videoEntry(openButton.dataset.key);
    if (entry) {
      openVideo(entry);
    }
  }
}

/**
 * React to a click on a folder row, the up row, or a breadcrumb.
 * @param {MouseEvent} event The click.
 */
function onFolderClick(event) {
  const row = event.target.closest('[data-prefix]');
  if (row) {
    navigate(row.dataset.prefix);
  }
}

/** Open the first folder or video that passes the search box. Enter in the search box runs this. */
function openFirstSearchResult() {
  const folders = filterByName(state.folders, state.search);
  if (folders.length > 0) {
    navigate(folders[0].prefix);
    return;
  }
  const videos = visibleVideos();
  if (videos.length > 0) {
    dom.search.blur();
    openVideo(videos[0]);
    return;
  }
  toast('Nothing in this folder matches the search.', 'info');
}

// ----------------------------------------------------------- background queue

/**
 * Queue a video for background download (playable formats) or conversion (the rest).
 * @param {string} key The S3 key of the video.
 * @returns {Promise<void>} Settles after the server accepted or refused the job.
 */
async function queueVideo(key) {
  const entry = videoEntry(key);
  if (!entry) {
    return;
  }
  const current = state.queue[key];
  if (current && current.state === 'working') {
    toast('This video is already in the background queue.', 'info');
    return;
  }
  if (current && current.state === 'ready') {
    toast('This video is already on this Mac.', 'info');
    return;
  }
  const kind = entry.playable ? 'prefetch' : 'convert';
  state.queue[key] = { kind, state: 'working', fraction: 0, message: '' };
  renderBrowser();
  try {
    const answer = await request('POST', kind === 'prefetch' ? '/api/prefetch' : '/api/convert', { key });
    applyQueueAnswer(key, answer);
    toast(kind === 'prefetch' ? `Queued ${entry.name} for download` : `Queued ${entry.name} for conversion`);
  } catch (error) {
    state.queue[key] = { kind, state: 'failed', fraction: 0, message: error.message };
    toast(`The queue refused ${entry.name}. ${endSentence(error.message)}`, 'error');
  }
  renderBrowser();
  scheduleQueuePoll();
}

/**
 * Store the progress the server reported for a queued video.
 * @param {string} key The S3 key of the video.
 * @param {object} answer The JSON answer of /api/prefetch or /api/convert.
 */
function applyQueueAnswer(key, answer) {
  const job = state.queue[key];
  if (!job) {
    return;
  }
  const before = job.state;
  Object.assign(job, queueProgress(job.kind, answer));
  const entry = videoEntry(key);
  if (entry && job.kind === 'convert' && answer && typeof answer.state === 'string') {
    entry.converted = answer.state;
  }
  if (job.state === 'failed' && before !== 'failed') {
    toast(`Background conversion failed. ${endSentence(job.message)}`, 'error');
  }
}

/** Start the queue poll timer when a queued video is still in progress. */
function scheduleQueuePoll() {
  const working = Object.values(state.queue).some((job) => job.state === 'working');
  if (working && runtime.queueTimer === null) {
    runtime.queueTimer = window.setTimeout(pollQueue, POLL_MS);
  }
}

/**
 * Ask the server for the progress of every queued video, then plan the next poll.
 * @returns {Promise<void>} Settles after one round of questions.
 */
async function pollQueue() {
  runtime.queueTimer = null;
  for (const [key, job] of Object.entries(state.queue)) {
    if (job.state !== 'working') {
      continue;
    }
    const route = job.kind === 'prefetch' ? '/api/prefetch' : '/api/convert';
    try {
      applyQueueAnswer(key, await request('GET', `${route}?key=${encodeURIComponent(key)}`));
    } catch (error) {
      job.state = 'failed';
      job.message = error.message;
      toast(`The background queue lost track of a video. ${endSentence(error.message)}`, 'error');
    }
  }
  renderBrowser();
  scheduleQueuePoll();
}

// ------------------------------------------------- species, pins, filter list

/**
 * Load the species list, the pins, the annotator, and the tally target.
 * @returns {Promise<void>} Settles after the strip shows the pins or the error.
 */
async function loadSpecies() {
  try {
    applySettings(await request('GET', '/api/species'));
    state.speciesError = null;
  } catch (error) {
    state.speciesError = `The species list did not load. ${endSentence(error.message)}`;
  }
  dom.speciesError.hidden = !state.speciesError;
  dom.speciesErrorText.textContent = state.speciesError || '';
}

/**
 * Take over the answer of GET /api/species or POST /api/settings.
 * @param {object} data {species, pins, annotator, tally_target}.
 */
function applySettings(data) {
  state.species = Array.isArray(data.species) ? data.species : [];
  state.pins = data.pins && typeof data.pins === 'object' ? data.pins : {};
  state.annotator = typeof data.annotator === 'string' ? data.annotator : '';
  state.tallyTarget = Number.isFinite(data.tally_target) ? data.tally_target : state.tallyTarget;
  if (document.activeElement !== dom.annotator) {
    dom.annotator.value = state.annotator;
  }
  renderPins();
  renderTally();
  updateMatches();
}

/** Draw the ten pinned-species keycaps. */
function renderPins() {
  const chosen = state.pending ? state.pending.species : null;
  const nodes = PIN_KEYS.map((slot) => {
    const code = state.pins[slot] || '';
    const species = speciesByCode(code);
    const name = species ? species.name : '';
    const parts = binomialParts(name);
    const nameNodes = parts
      ? [el('span', { class: 'pin-genus', text: `${parts.initial} ` }), parts.epithet]
      : [name || 'empty'];
    const choose = el('button', {
      class: 'pin-choose',
      title: code
        ? `You choose ${name || code} for the mark on screen. The ${slot} key does the same.`
        : `Key ${slot} has no species yet. You highlight a species in the filter list, then click the pin in this corner.`,
      attrs: {
        type: 'button',
        'aria-label': code ? `Key ${slot}: ${code}, ${name}` : `Key ${slot}: empty`,
      },
      data: { slot },
    }, [
      el('span', { class: 'pin-key', text: slot, attrs: { 'aria-hidden': 'true' } }),
      el('span', { class: 'pin-code', text: code }),
      el('span', { class: 'pin-name' }, nameNodes),
    ]);
    const assign = el('button', {
      class: 'pin-assign',
      title: `You pin the species highlighted in the filter list to key ${slot}. The server remembers your pins.`,
      attrs: { type: 'button', 'aria-label': `Pin the highlighted species to key ${slot}` },
      data: { slot },
    }, [icon('pin')]);
    return el('div', {
      class: 'pin-slot',
      attrs: { role: 'group' },
      data: { slot, code, chosen: String(Boolean(code) && code === chosen) },
    }, [choose, assign]);
  });
  dom.pins.replaceChildren(...nodes);
}

/**
 * React to a click in the species strip: choose a species or change a pin.
 * @param {MouseEvent} event The click.
 */
function onPinsClick(event) {
  const assign = event.target.closest('.pin-assign');
  if (assign) {
    assignPin(assign.dataset.slot);
    return;
  }
  const choose = event.target.closest('.pin-choose');
  if (choose) {
    choosePinned(choose.dataset.slot);
  }
}

/**
 * Choose the species pinned to a key for the mark on screen.
 * @param {string} slot The key, "1" to "9" or "0".
 */
function choosePinned(slot) {
  if (state.mode !== 'marking') {
    toast('Click a sponge in the video first. Then press its species key.', 'info');
    return;
  }
  const code = state.pins[slot];
  if (!code) {
    toast(`Slot ${slot} is empty.`, 'info');
    return;
  }
  chooseSpecies(code);
}

/**
 * Set the species of the pending mark and return the keyboard to the mark.
 * @param {string} code The species code.
 */
function chooseSpecies(code) {
  if (!state.pending) {
    return;
  }
  state.pending.species = code;
  state.pending.error = null;
  clearFilter();
  if (document.activeElement === dom.filter) {
    dom.filter.blur();
  }
  renderPins();
  renderMarkBar();
  drawOverlay();
}

/**
 * Pin the species highlighted in the filter list to a key and save the pins.
 * @param {string} slot The key, "1" to "9" or "0".
 * @returns {Promise<void>} Settles after the server stored or refused the pins.
 */
async function assignPin(slot) {
  const code = pinSource();
  if (!code) {
    toast('Highlight a species in the filter list first. Then click the pin on a key.', 'info');
    dom.filter.focus();
    return;
  }
  try {
    const answer = await request('POST', '/api/settings', { pins: swapPin(state.pins, slot, code) });
    state.pinCandidate = null;
    clearFilter();
    if (document.activeElement === dom.filter) {
      dom.filter.blur();
    }
    applySettings(answer);
    renderMarkBar();
    toast(`Pinned ${code} to key ${slot}`);
  } catch (error) {
    toast(`The pins were not saved. ${endSentence(error.message)}`, 'error');
  }
}

/**
 * Find the species a pin control would assign right now.
 * @returns {?string} The species armed for pinning, else the match highlighted under a
 *   typed query, else null. An open list without a query does not count, so a stray
 *   click on a pin control cannot move the first species of the list.
 */
function pinSource() {
  if (state.pinCandidate) {
    return state.pinCandidate;
  }
  if (state.filterOpen && state.filterQuery.trim() !== '') {
    return state.matches[state.matchIndex] || null;
  }
  return null;
}

/**
 * Hold a species ready for pinning and close the filter list, so every pin control is
 * in view. Screening mode runs this when a match is chosen with Enter or a click.
 * @param {string} code The species code.
 */
function armPin(code) {
  state.pinCandidate = code;
  clearFilter();
  if (document.activeElement === dom.filter) {
    dom.filter.blur();
  }
  renderMatches();
  renderMarkBar();
}

/** Forget the species that was held ready for pinning. */
function disarmPin() {
  if (state.pinCandidate) {
    state.pinCandidate = null;
    renderMatches();
    renderMarkBar();
  }
}

/** Recompute the filter matches from the text in the filter box and draw them. */
function updateMatches() {
  state.filterQuery = dom.filter.value;
  state.matches = filterSpecies(state.species, state.filterQuery).map((item) => item.code);
  state.matchIndex = Math.min(state.matchIndex, Math.max(0, state.matches.length - 1));
  renderMatches();
}

/** Draw the list of matching species above the filter box. It shows while the box has focus. */
function renderMatches() {
  dom.matches.hidden = !state.filterOpen;
  dom.filter.setAttribute('aria-expanded', String(state.filterOpen));
  const highlighted = state.filterOpen ? state.matches[state.matchIndex] : null;
  document.body.dataset.pinning = String(Boolean(pinSource()));
  if (!state.filterOpen) {
    dom.matches.replaceChildren();
    dom.filter.removeAttribute('aria-activedescendant');
    return;
  }
  if (state.matches.length === 0) {
    dom.filter.removeAttribute('aria-activedescendant');
    dom.matches.replaceChildren(el('li', {
      class: 'match-empty',
      text: `No species matches "${state.filterQuery.trim()}". Backspace to edit.`,
    }));
    return;
  }
  const pinOf = new Map(PIN_KEYS.filter((slot) => state.pins[slot]).map((slot) => [state.pins[slot], slot]));
  const nodes = state.matches.map((code, index) => {
    const species = speciesByCode(code);
    const selected = index === state.matchIndex;
    const children = [
      el('span', { class: 'match-code', text: code }),
      el('span', { class: 'match-name', text: species ? species.name : '' }),
    ];
    if (pinOf.has(code)) {
      children.push(el('kbd', { text: pinOf.get(code), title: `This species sits on key ${pinOf.get(code)}.` }));
    }
    return el('li', {
      class: 'match',
      title: state.mode === 'marking'
        ? `You choose ${species ? species.name : code} for the mark on screen.`
        : `You hold ${species ? species.name : code} ready for pinning. Then you click the pin in the corner of a number key.`,
      attrs: { role: 'option', id: `match-${code}`, 'aria-selected': String(selected) },
      data: { code },
    }, children);
  });
  dom.matches.replaceChildren(...nodes);
  dom.filter.setAttribute('aria-activedescendant', `match-${highlighted}`);
  const active = dom.matches.querySelector('[aria-selected="true"]');
  if (active) {
    active.scrollIntoView({ block: 'nearest' });
  }
}

/**
 * Move the highlight in the filter list.
 * @param {number} step 1 for down, -1 for up.
 */
function moveMatch(step) {
  if (state.matches.length === 0) {
    return;
  }
  state.matchIndex = (state.matchIndex + step + state.matches.length) % state.matches.length;
  renderMatches();
}

/** Empty the filter box and reset the highlight. */
function clearFilter() {
  dom.filter.value = '';
  state.matchIndex = 0;
  updateMatches();
}

/**
 * Move the keyboard to the filter box and type one letter there. Marking mode sends
 * letter keys here, so typing a name needs no click.
 * @param {string} letter The typed letter.
 */
function typeIntoFilter(letter) {
  dom.filter.focus();
  dom.filter.value += letter;
  state.matchIndex = 0;
  updateMatches();
}

/**
 * React to a click on a filter match: highlight it, and in marking mode choose it.
 * @param {MouseEvent} event The click.
 */
function onMatchClick(event) {
  const row = event.target.closest('.match');
  if (!row) {
    return;
  }
  state.matchIndex = Math.max(0, state.matches.indexOf(row.dataset.code));
  if (state.mode === 'marking') {
    chooseSpecies(row.dataset.code);
  } else {
    armPin(row.dataset.code);
  }
}

// --------------------------------------------------------------------- player

/**
 * Show or clear the message in the middle of the stage.
 * @param {?string} kind "idle", "loading", "buffering", "converting", "network", "failed", or
 *   null to clear the message.
 * @param {string} [text] The message.
 * @param {{label: string, title: string, run: Function}} [action] A button under the message.
 */
function setStatus(kind, text = '', action = null) {
  state.status = kind ? { kind, text } : null;
  runtime.statusAction = action;
  dom.stageStatus.hidden = !kind;
  dom.stageStatusText.textContent = text;
  dom.stageAction.hidden = !action;
  if (action) {
    dom.stageAction.textContent = action.label;
    dom.stageAction.title = action.title;
  }
}

/**
 * Clear the stage message when it is one of the given kinds.
 * @param {Array<string>} kinds The kinds that may be cleared.
 */
function clearStatus(kinds) {
  if (state.status && kinds.includes(state.status.kind)) {
    setStatus(null);
  }
}

/** Stop the timers that belong to the video on screen. */
function stopVideoTimers() {
  window.clearTimeout(runtime.convertTimer);
  window.clearTimeout(runtime.retryTimer);
  runtime.convertTimer = null;
  runtime.retryTimer = null;
}

/**
 * Open a video: tell the server, load its sightings, and start playback, through
 * conversion when Chrome cannot play the format.
 * @param {object} entry A catalog video entry ({key, name, playable, converted, status}).
 * @returns {Promise<void>} Settles after the open call and the sightings call returned.
 */
async function openVideo(entry) {
  if (state.pending) {
    endMark();
  }
  savePosition();
  stopVideoTimers();
  runtime.loadToken += 1;
  runtime.playFailures = 0;
  const token = runtime.loadToken;
  state.key = entry.key;
  state.name = entry.name;
  state.rows = [];
  state.conversion = null;
  state.videoStatus = entry.status === 'done' ? 'done' : 'in progress';
  dom.currentVideo.textContent = entry.name;
  dom.currentVideo.title = `The video that is open now: ${entry.key}`;
  renderBrowser();
  renderSightings();
  renderDone();
  renderMarkBar();

  if (entry.converted === 'done') {
    playSource('converted');
  } else if (entry.playable) {
    playSource('original');
  } else {
    startConversion(token, 'Chrome cannot play this format directly.');
  }

  const [opened, listed] = await Promise.allSettled([
    request('POST', '/api/videos/open', { key: entry.key }),
    request('GET', `/api/observations?key=${encodeURIComponent(entry.key)}`),
  ]);
  if (token !== runtime.loadToken) {
    return;
  }
  if (opened.status === 'fulfilled' && opened.value.video) {
    state.videoStatus = opened.value.video.Status === 'done' ? 'done' : 'in progress';
    const row = videoEntry(entry.key);
    if (row && row.status === 'new') {
      row.status = 'in progress';
    }
  } else if (opened.status === 'rejected') {
    toast(`The server did not record that this video was opened. ${endSentence(opened.reason.message)}`, 'error');
  }
  if (listed.status === 'fulfilled' && Array.isArray(listed.value.rows)) {
    // A sighting saved while the list was on its way stays in the list.
    const listedIds = new Set(listed.value.rows.map((row) => row.ID));
    state.rows = listed.value.rows.concat(state.rows.filter((row) => !listedIds.has(row.ID)));
  } else if (listed.status === 'rejected') {
    toast(`The sightings of this video did not load. ${endSentence(listed.reason.message)}`, 'error');
  }
  renderBrowser();
  renderSightings();
  renderDone();
  drawOverlay();
}

/**
 * Build the address of the open video.
 * @param {string} kind "original" for the relayed file, "converted" for the converted copy.
 * @returns {string} The /video address with the key in the query string.
 */
function videoUrl(kind) {
  return `/video?key=${encodeURIComponent(state.key)}${kind === 'converted' ? '&converted=1' : ''}`;
}

/**
 * Ask the server for the first byte of the open video. A video error alone does not say
 * whether Chrome disliked the format or the bytes never arrived, and only the first case
 * is a reason to convert.
 * @param {string} kind "original" or "converted".
 * @returns {Promise<{reachable: boolean, status: number, message: string}>} reachable is true
 *   when the server delivered the byte. Otherwise message holds the server's error text.
 */
async function probeVideo(kind) {
  let response;
  try {
    response = await fetch(videoUrl(kind), { headers: { Range: 'bytes=0-0' } });
  } catch (error) {
    return { reachable: false, status: 0, message: 'The local server did not answer. Check that screener.py is still running.' };
  }
  if (response.ok) {
    if (response.body) {
      response.body.cancel();
    }
    return { reachable: true, status: response.status, message: '' };
  }
  let message = `The server answered with status ${response.status}.`;
  try {
    const payload = await response.json();
    if (payload && typeof payload.error === 'string') {
      message = payload.error;
    }
  } catch (error) {
    // The plain status message stands when the body is not JSON.
  }
  return { reachable: false, status: response.status, message };
}

/**
 * Point the video element at the relay or at the converted copy, and start muted playback.
 * @param {string} kind "original" or "converted".
 */
function playSource(kind) {
  state.source = kind;
  setStatus('loading', 'Loading the video.');
  dom.video.muted = true;
  dom.video.src = videoUrl(kind);
  dom.video.defaultPlaybackRate = state.speed;
  dom.video.playbackRate = state.speed;
  resumePlayback();
}

/**
 * Start playback and ignore the rejection Chrome sends when another load interrupts it.
 * A video that reached its end stays there, because play() would restart it from the top.
 */
function resumePlayback() {
  if (dom.video.ended) {
    return;
  }
  const attempt = dom.video.play();
  if (attempt && typeof attempt.catch === 'function') {
    attempt.catch(() => {
      // An interrupted or blocked play() leaves the video paused. Space starts it.
    });
  }
}

/**
 * Ask the server to convert the open video, then follow the job.
 * @param {number} token The load token of the video this conversion belongs to.
 * @param {string} reason A sentence that says why the conversion starts.
 * @returns {Promise<void>} Settles after the server accepted or refused the job.
 */
async function startConversion(token, reason) {
  state.source = 'converting';
  dom.video.removeAttribute('src');
  dom.video.load();
  renderTransport();
  setStatus('converting', `${reason} The server is converting it for playback.`);
  try {
    followConversion(token, await request('POST', '/api/convert', { key: state.key }));
  } catch (error) {
    if (token === runtime.loadToken) {
      showConversionFailure(token, error.message);
    }
  }
}

/**
 * Show the progress of a conversion, play the result, or report the failure.
 * @param {number} token The load token of the video this conversion belongs to.
 * @param {{state: string, progress: number, message: string}} status The server's answer.
 */
function followConversion(token, status) {
  if (token !== runtime.loadToken) {
    return;
  }
  state.conversion = status;
  const entry = videoEntry(state.key);
  if (entry && typeof status.state === 'string') {
    entry.converted = status.state;
    renderBrowser();
  }
  if (status.state === 'done') {
    playSource('converted');
    return;
  }
  if (status.state === 'failed') {
    showConversionFailure(token, status.message || 'ffmpeg gave no message.');
    return;
  }
  const waiting = status.state === 'queued' ? ' It is waiting in the conversion queue.' : '';
  setStatus('converting', `Converting this video for playback: ${percentText(status.progress)}.${waiting}`);
  runtime.convertTimer = window.setTimeout(async () => {
    try {
      followConversion(token, await request('GET', `/api/convert?key=${encodeURIComponent(state.key)}`));
    } catch (error) {
      if (token === runtime.loadToken) {
        showConversionFailure(token, error.message);
      }
    }
  }, POLL_MS);
}

/**
 * Report a failed conversion on the stage, with a button that starts it again.
 * @param {number} token The load token of the video this conversion belongs to.
 * @param {string} message The server's failure text.
 */
function showConversionFailure(token, message) {
  setStatus('failed', `The conversion failed, so this video cannot play yet. ${endSentence(message)}`, {
    label: 'Convert again',
    title: 'You ask the server to convert this video again.',
    run: () => startConversion(token, 'You asked for another try.'),
  });
}

/**
 * Report a lost connection on the stage and load the same source again in a few seconds.
 * The retry repeats for as long as the video stays open.
 * @param {number} token The load token of the video on screen.
 * @param {string} source "original" or "converted".
 * @param {string} reason A sentence that says what went wrong.
 */
function retrySoon(token, source, reason) {
  setStatus('network', `${reason} The player tries again every few seconds. Saved sightings are safe.`);
  window.clearTimeout(runtime.retryTimer);
  runtime.retryTimer = window.setTimeout(() => {
    if (token === runtime.loadToken) {
      playSource(source);
    }
  }, NETWORK_RETRY_MS);
}

/**
 * React to an error of the video element. A lost connection is retried. A file that
 * arrives but does not play falls back to conversion, and a converted copy that does
 * not play is reported.
 * @returns {Promise<void>} Settles after the page chose between retry, conversion, and report.
 */
async function onVideoError() {
  if (!state.key || !dom.video.getAttribute('src')) {
    return;
  }
  const failure = dom.video.error;
  const code = failure ? failure.code : 0;
  if (code === MediaError.MEDIA_ERR_ABORTED) {
    return;
  }
  const token = runtime.loadToken;
  const source = state.source;
  if (source !== 'original' && source !== 'converted') {
    return;
  }
  if (code === MediaError.MEDIA_ERR_NETWORK) {
    retrySoon(token, source, 'The connection to the video was lost.');
    return;
  }
  const detail = failure && failure.message ? ` Chrome says: ${endSentence(failure.message)}` : '';
  const probe = await probeVideo(source);
  if (token !== runtime.loadToken) {
    return;
  }
  if (!probe.reachable && source === 'converted' && probe.status === 404) {
    startConversion(token, 'The converted copy is missing.');
  } else if (!probe.reachable) {
    retrySoon(token, source, `The video did not load. ${endSentence(probe.message)}`);
  } else if (source === 'original' && runtime.playFailures < 1) {
    // The bytes arrive now, so the failure may have been a passing one. One more try
    // costs a moment. A needless conversion of a large video costs minutes.
    runtime.playFailures += 1;
    playSource('original');
  } else if (source === 'original') {
    startConversion(token, 'Chrome could not play this file.');
  } else {
    setStatus('failed', `This video did not play, even as a converted copy.${detail}`, {
      label: 'Try again',
      title: 'You load this video again from the server.',
      run: () => playSource('converted'),
    });
  }
}

/** Take over the length of a freshly loaded video and jump to the remembered position. */
function onLoadedMetadata() {
  dom.scrub.max = String(Number.isFinite(dom.video.duration) ? dom.video.duration : 0);
  const start = resumePosition(storageGet(STORAGE.position + state.key), dom.video.duration);
  if (start > 0) {
    dom.video.currentTime = start;
  }
  clearStatus(['loading', 'network']);
  renderTransport();
  renderScrubMarks();
  renderMarkBar();
  drawOverlay();
}

/** Remember the position of the open video, so it reopens there. */
function savePosition() {
  if (state.key && dom.video.getAttribute('src') && Number.isFinite(dom.video.currentTime)) {
    storageSet(STORAGE.position + state.key, String(dom.video.currentTime));
    runtime.positionSavedAt = Date.now();
  }
}

/** Follow the playhead: the clock, the scrub bar, the saved position, the pending mark. */
function onTimeUpdate() {
  if (Date.now() - runtime.positionSavedAt > POSITION_SAVE_MS) {
    savePosition();
  }
  if (state.pending) {
    state.pending.time = dom.video.currentTime;
    renderMarkBar();
  }
  renderTransport();
  drawOverlay();
}

/** Tell the annotator what to do when the video reaches its end. */
function onEnded() {
  toast('End of the video. Press D when it is fully screened, then N for the next one.', 'info', ERROR_TOAST_MS);
}

/** Play when paused, pause when playing. At the end of the video, play starts over from the top. */
function togglePlay() {
  if (!isVideoReady()) {
    return;
  }
  if (!dom.video.paused) {
    dom.video.pause();
    return;
  }
  if (dom.video.ended) {
    dom.video.currentTime = 0;
  }
  resumePlayback();
}

/**
 * Tell whether a video is loaded far enough to seek and play.
 * @returns {boolean} True once the video element knows its size and length.
 */
function isVideoReady() {
  return Boolean(state.key) && dom.video.readyState >= 1 && dom.video.videoWidth > 0;
}

/**
 * Seek to a time, kept inside the video.
 * @param {number} time The wanted time in seconds.
 */
function seekTo(time) {
  if (isVideoReady()) {
    dom.video.currentTime = clampTime(time, dom.video.duration);
  }
}

/**
 * Pause and step by one thirtieth of a second.
 * @param {number} direction 1 for forward, -1 for back.
 */
function stepFrame(direction) {
  if (isVideoReady()) {
    dom.video.pause();
    seekTo(dom.video.currentTime + direction * FRAME_STEP_SECONDS);
  }
}

/**
 * Set the playback speed and remember it.
 * @param {number} speed A speed from the SPEEDS list.
 */
function setSpeed(speed) {
  state.speed = speed;
  dom.video.defaultPlaybackRate = speed;
  dom.video.playbackRate = speed;
  dom.speed.textContent = `${speed}x`;
  storageSet(STORAGE.speed, String(speed));
}

/**
 * Show or hide the quadrant lines and the center line, and remember the choice.
 * @param {boolean} on True to show the lines.
 */
function setGrid(on) {
  state.grid = on;
  dom.grid.setAttribute('aria-pressed', String(on));
  storageSet(STORAGE.grid, on ? '1' : '0');
  drawOverlay();
}

/** Draw the play button, the clock, and the scrub bar from the video element. */
function renderTransport() {
  const ready = isVideoReady();
  const playing = ready && !dom.video.paused && !dom.video.ended;
  dom.play.disabled = !ready;
  dom.scrub.disabled = !ready;
  dom.playIcon.setAttribute('href', playing ? '#i-pause' : '#i-play');
  dom.play.setAttribute('aria-label', playing ? 'Pause' : 'Play');
  const now = ready ? dom.video.currentTime : NaN;
  const length = ready ? dom.video.duration : NaN;
  dom.time.textContent = `${formatClock(now)} / ${formatClock(length)}`;
  if (ready && !runtime.scrubbing) {
    dom.scrub.value = String(now);
  }
}

/** Draw one pink tick on the scrub bar per sighting of this video. */
function renderScrubMarks() {
  const length = dom.video.duration;
  if (!isVideoReady() || !Number.isFinite(length) || length <= 0) {
    dom.scrubMarks.replaceChildren();
    return;
  }
  const nodes = [];
  for (const row of state.rows) {
    const seconds = Number(row.TimestampSeconds);
    if (row.TimestampSeconds !== '' && Number.isFinite(seconds)) {
      const tick = el('span', { class: 'scrub-mark', data: { id: row.ID } });
      tick.style.left = `${clampTime(seconds, length) / length * 100}%`;
      nodes.push(tick);
    }
  }
  dom.scrubMarks.replaceChildren(...nodes);
}

/** Draw the done button from the status of the open video. */
function renderDone() {
  const done = state.videoStatus === 'done';
  dom.done.disabled = !state.key;
  dom.done.setAttribute('aria-pressed', String(done));
  dom.doneLabel.textContent = done ? 'Done' : 'Mark done';
}

/**
 * Mark the open video as fully screened, or reopen it.
 * @returns {Promise<void>} Settles after the server stored or refused the change.
 */
async function toggleDone() {
  if (!state.key) {
    toast('Open a video first.', 'info');
    return;
  }
  const key = state.key;
  const done = state.videoStatus !== 'done';
  try {
    const answer = await request('POST', '/api/videos/done', { key, done });
    const status = answer.video && answer.video.Status === 'done' ? 'done' : 'in progress';
    const entry = videoEntry(key);
    if (entry) {
      entry.status = status;
    }
    if (key === state.key) {
      state.videoStatus = status;
    }
    toast(status === 'done' ? 'Marked done. Press N for the next video.' : 'Reopened. The video is in progress again.');
  } catch (error) {
    toast(`The done status was not saved. ${endSentence(error.message)}`, 'error');
  }
  renderDone();
  renderBrowser();
}

/** Open the video that follows the open one in the list on screen. */
function openNextVideo() {
  const videos = visibleVideos();
  if (videos.length === 0) {
    toast('This folder shows no videos. Open a folder with videos first.', 'info');
    return;
  }
  const next = nextVideo(videos, state.key);
  if (next) {
    openVideo(next);
  } else {
    toast('This is the last video in the list.', 'info');
  }
}

// -------------------------------------------------------------------- overlay

/**
 * Find the picture inside the stage, without the letterbox bars.
 * @returns {{x: number, y: number, w: number, h: number}} The picture in stage pixels.
 */
function pictureRectLocal() {
  return contentRect(dom.stage.clientWidth, dom.stage.clientHeight, dom.video.videoWidth, dom.video.videoHeight);
}

/**
 * Find the picture in viewport pixels. The end-to-end driver uses this to aim its clicks.
 * @returns {{x: number, y: number, w: number, h: number}} The picture in viewport pixels.
 */
function pictureRectInViewport() {
  const bounds = dom.overlay.getBoundingClientRect();
  const rect = pictureRectLocal();
  return { x: bounds.left + rect.x, y: bounds.top + rect.y, w: rect.w, h: rect.h };
}

/**
 * Stroke a path twice: a wide white line, then the colored line on top, so the shape
 * reads against blue-green water and against bright sand.
 * @param {CanvasRenderingContext2D} ctx The overlay context.
 * @param {Function} trace Builds the path (beginPath and the shapes).
 * @param {string} color The inner stroke color.
 * @param {number} width The inner stroke width in pixels.
 */
function strokeOutlined(ctx, trace, color, width) {
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.setLineDash([]);
  ctx.strokeStyle = '#ffffff';
  ctx.lineWidth = width + 3;
  trace();
  ctx.stroke();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  trace();
  ctx.stroke();
}

/**
 * Draw the quadrant lines and the center line: thin, quiet, with a dark twin so they
 * stay visible on bright frames.
 * @param {CanvasRenderingContext2D} ctx The overlay context.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture in stage pixels.
 */
function drawGrid(ctx, rect) {
  const cx = Math.round(rect.x + rect.w / 2) + 0.5;
  const cy = Math.round(rect.y + rect.h / 2) + 0.5;
  const left = rect.x;
  const right = rect.x + rect.w;
  const top = rect.y;
  const bottom = rect.y + rect.h;
  ctx.setLineDash([]);
  ctx.lineWidth = 1;
  ctx.lineCap = 'butt';

  ctx.strokeStyle = 'rgba(0, 0, 0, 0.35)';
  ctx.beginPath();
  ctx.moveTo(cx + 1, top);
  ctx.lineTo(cx + 1, bottom);
  ctx.moveTo(left, cy + 1);
  ctx.lineTo(right, cy + 1);
  ctx.stroke();

  ctx.strokeStyle = 'rgba(255, 255, 255, 0.34)';
  ctx.beginPath();
  ctx.moveTo(cx, top);
  ctx.lineTo(cx, bottom);
  ctx.stroke();

  // The horizontal line doubles as the center line that times a sighting, so it is a
  // little brighter and carries a small tick at each end.
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.58)';
  ctx.beginPath();
  ctx.moveTo(left, cy);
  ctx.lineTo(right, cy);
  ctx.moveTo(left + 0.5, cy - 6);
  ctx.lineTo(left + 0.5, cy + 6);
  ctx.moveTo(right - 0.5, cy - 6);
  ctx.lineTo(right - 0.5, cy + 6);
  ctx.stroke();
}

/**
 * Draw the small text tag of a mark, beside the mark and inside the picture.
 * @param {CanvasRenderingContext2D} ctx The overlay context.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture in stage pixels.
 * @param {string} text The tag text.
 * @param {{center: {x: number, y: number}, boxPx: ?object, radius: number}} shape Where drawMark drew the mark.
 * @param {{fill: string, ink: string, outline: string}} colors Tag colors.
 */
function drawTag(ctx, rect, text, shape, colors) {
  ctx.font = CANVAS_FONT;
  const width = Math.ceil(ctx.measureText(text).width) + 12;
  const height = TAG_HEIGHT;
  const spot = tagPosition(rect, shape.center, shape.boxPx, width, height, shape.radius);
  const left = spot.x;
  const top = spot.y;
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.roundRect(left, top, width, height, 5);
  ctx.fillStyle = colors.fill;
  ctx.fill();
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = colors.outline;
  ctx.stroke();
  ctx.fillStyle = colors.ink;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, left + 6, top + height / 2 + 1);
}

/**
 * Draw a mark: a ring with four ticks for a point, a rectangle for a box.
 * @param {CanvasRenderingContext2D} ctx The overlay context.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture in stage pixels.
 * @param {{point: {x: number, y: number}, box: ?object}} mark The mark in frame fractions.
 * @param {{color: string, width: number, radius: number}} style Stroke color, stroke width, ring radius.
 * @returns {{center: {x: number, y: number}, boxPx: ?object, radius: number}} The drawn shape in
 *   stage pixels, which drawTag needs to place the tag.
 */
function drawMark(ctx, rect, mark, style) {
  const center = toPixels(mark.point, rect);
  if (mark.box) {
    const corner = toPixels({ x: mark.box.x, y: mark.box.y }, rect);
    const boxPx = { x: corner.x, y: corner.y, w: mark.box.w * rect.w, h: mark.box.h * rect.h };
    strokeOutlined(ctx, () => {
      ctx.beginPath();
      ctx.rect(boxPx.x, boxPx.y, boxPx.w, boxPx.h);
    }, style.color, style.width);
    return { center, boxPx, radius: style.radius };
  }
  const r = style.radius;
  strokeOutlined(ctx, () => {
    ctx.beginPath();
    ctx.arc(center.x, center.y, r, 0, Math.PI * 2);
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      ctx.moveTo(center.x + dx * (r + 4), center.y + dy * (r + 4));
      ctx.lineTo(center.x + dx * (r + TICK_REACH), center.y + dy * (r + TICK_REACH));
    }
  }, style.color, style.width);
  ctx.beginPath();
  ctx.arc(center.x, center.y, 2.6, 0, Math.PI * 2);
  ctx.fillStyle = '#ffffff';
  ctx.fill();
  ctx.beginPath();
  ctx.arc(center.x, center.y, 1.5, 0, Math.PI * 2);
  ctx.fillStyle = style.color;
  ctx.fill();
  return { center, boxPx: null, radius: r };
}

/** Redraw the overlay: quadrant highlight, grid, saved marks near this frame, and the live mark. */
function drawOverlay() {
  const width = dom.stage.clientWidth;
  const height = dom.stage.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  const pixelW = Math.max(1, Math.round(width * ratio));
  const pixelH = Math.max(1, Math.round(height * ratio));
  if (dom.overlay.width !== pixelW || dom.overlay.height !== pixelH) {
    dom.overlay.width = pixelW;
    dom.overlay.height = pixelH;
  }
  const ctx = dom.overlay.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const rect = pictureRectLocal();
  if (rect.w === 0 || !state.key) {
    return;
  }

  if (state.pending) {
    const quadrant = quadrantRect(state.pending.quadrant, rect);
    ctx.fillStyle = QUADRANT_FILL;
    ctx.fillRect(quadrant.x, quadrant.y, quadrant.w, quadrant.h);
  }
  if (state.grid) {
    drawGrid(ctx, rect);
  }
  for (const saved of marksNear(state.rows, dom.video.currentTime, SAVED_MARK_WINDOW_SECONDS)) {
    const shape = drawMark(ctx, rect, saved, { color: '#2a2a2a', width: 1.5, radius: 8 });
    drawTag(ctx, rect, `${saved.code} saved`, shape, {
      fill: 'rgba(20, 20, 20, 0.82)', ink: '#ffffff', outline: 'rgba(255, 255, 255, 0.7)',
    });
  }

  const drag = runtime.drag;
  if (drag && isDrag(drag.startPx.x, drag.startPx.y, drag.nowPx.x, drag.nowPx.y)) {
    const end = toNormalizedClamped(drag.nowPx.x, drag.nowPx.y, rect);
    if (end) {
      const box = normalizeBox(drag.start, end);
      drawMark(ctx, rect, { point: anchorPoint(drag.start, box), box }, { color: MARK_COLOR, width: 2.5, radius: 11 });
    }
    return;
  }
  if (state.pending) {
    const shape = drawMark(ctx, rect, state.pending, { color: MARK_COLOR, width: 2.5, radius: 11 });
    drawTag(ctx, rect, state.pending.species || '?', shape, {
      fill: MARK_COLOR, ink: MARK_INK, outline: '#ffffff',
    });
  }
}

// -------------------------------------------------------------------- marking

/**
 * Switch the keyboard mode and the controls that belong to it.
 * @param {string} mode "screening" or "marking".
 */
function setMode(mode) {
  state.mode = mode;
  document.body.dataset.mode = mode;
  const marking = mode === 'marking';
  dom.note.disabled = !marking;
  dom.save.disabled = !marking;
  dom.saveStay.disabled = !marking;
  dom.cancel.disabled = !marking;
  dom.modePill.textContent = marking ? 'Marking' : 'Screening';
}

/** Draw the line under the species strip: what the mark holds, or what to do next. */
function renderMarkBar() {
  const pending = state.pending;
  let summary = 'Open a video to start.';
  if (state.key) {
    summary = isVideoReady()
      ? 'Click a sponge in the video to mark it. Drag to draw a box.'
      : 'The video is not ready for marking yet.';
  }
  if (state.pinCandidate && !pending) {
    summary = `Pinning ${state.pinCandidate}: click the pin on a number key. Esc lets go.`;
  }
  if (pending) {
    const species = speciesByCode(pending.species);
    const chosen = pending.species
      ? `${pending.species} ${species ? species.name : ''}`.trim()
      : 'press 1 to 0, or type a name';
    const shape = pending.box ? 'box' : 'point';
    summary = `${QUADRANT_PHRASES[pending.quadrant]} · ${formatClock(pending.time)} · ${shape} · ${chosen}`;
    if (state.saving) {
      summary = 'Saving the sighting.';
    }
  }
  dom.markSummary.textContent = summary;
  dom.markSummary.title = summary;
  const problem = pending && pending.error ? pending.error : '';
  dom.markError.textContent = problem;
  dom.markError.hidden = problem === '';
}

/**
 * Turn a mouse event into a position inside the stage.
 * @param {MouseEvent} event The mouse event.
 * @returns {{x: number, y: number}} The position in stage pixels.
 */
function stagePoint(event) {
  const bounds = dom.overlay.getBoundingClientRect();
  return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
}

/**
 * Start a click or a drag on the picture. The video pauses at once, so the frame
 * under the pointer is the frame that gets saved.
 * @param {MouseEvent} event The mousedown on the overlay.
 */
function onStageMouseDown(event) {
  if (event.button !== 0 || !isVideoReady() || state.saving) {
    return;
  }
  const startPx = stagePoint(event);
  const start = toNormalized(startPx.x, startPx.y, pictureRectLocal());
  if (!start) {
    return;
  }
  event.preventDefault();
  if (document.activeElement instanceof HTMLElement && document.activeElement !== document.body) {
    document.activeElement.blur();
  }
  dom.video.pause();
  disarmPin();
  runtime.drag = { startPx, nowPx: startPx, start };
  window.addEventListener('mousemove', onStageMouseMove);
  window.addEventListener('mouseup', onStageMouseUp);
}

/**
 * Follow the pointer while the mouse button is down, and draw the box once it is a drag.
 * @param {MouseEvent} event The mousemove.
 */
function onStageMouseMove(event) {
  if (runtime.drag) {
    runtime.drag.nowPx = stagePoint(event);
    drawOverlay();
  }
}

/**
 * Finish a click or a drag: a drag past 6 px makes a box, anything else makes a point.
 * @param {MouseEvent} event The mouseup.
 */
function onStageMouseUp(event) {
  window.removeEventListener('mousemove', onStageMouseMove);
  window.removeEventListener('mouseup', onStageMouseUp);
  const drag = runtime.drag;
  runtime.drag = null;
  if (!drag) {
    return;
  }
  const endPx = stagePoint(event);
  let box = null;
  if (isDrag(drag.startPx.x, drag.startPx.y, endPx.x, endPx.y)) {
    const end = toNormalizedClamped(endPx.x, endPx.y, pictureRectLocal());
    const dragged = end ? normalizeBox(drag.start, end) : null;
    box = isUsableBox(dragged) ? dragged : null;
  }
  beginMark(box ? anchorPoint(drag.start, box) : drag.start, box);
}

/** Drop a drag that lost its mouseup, which happens when the window loses focus mid-drag. */
function abandonDrag() {
  if (runtime.drag) {
    window.removeEventListener('mousemove', onStageMouseMove);
    window.removeEventListener('mouseup', onStageMouseUp);
    runtime.drag = null;
    drawOverlay();
  }
}

/**
 * Start a mark, or replace the one on screen. A species that was already chosen stays,
 * so a second click only corrects the position.
 * @param {{x: number, y: number}} point The mark position in frame fractions (the box center for a box).
 * @param {?{x: number, y: number, w: number, h: number}} box The dragged box, or null for a click.
 */
function beginMark(point, box) {
  const anchor = anchorPoint(point, box);
  state.pending = {
    time: dom.video.currentTime,
    point,
    box,
    quadrant: quadrantOf(anchor.x, anchor.y),
    species: state.pending ? state.pending.species : null,
    error: null,
  };
  setMode('marking');
  renderPins();
  renderMarkBar();
  drawOverlay();
}

/** Forget the pending mark and return to screening mode, without touching playback. */
function endMark() {
  state.pending = null;
  dom.note.value = '';
  clearFilter();
  if (document.activeElement === dom.filter || document.activeElement === dom.note) {
    document.activeElement.blur();
  }
  setMode('screening');
  renderPins();
  renderMarkBar();
  drawOverlay();
}

/** Cancel the pending mark and play on. Escape and the Cancel button run this. */
function cancelMark() {
  if (!state.pending || state.saving) {
    return;
  }
  endMark();
  resumePlayback();
}

/**
 * Wait until the video element finished a seek, so the canvas copies the frame on screen.
 * @returns {Promise<void>} Settles at once when no seek is under way.
 */
function waitForSeek() {
  if (!dom.video.seeking) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    dom.video.addEventListener('seeked', () => resolve(), { once: true });
  });
}

/**
 * Copy the paused frame at the video's native size and cut the crop out of it.
 * @param {{point: object, box: ?object}} mark The pending mark.
 * @returns {{frame: string, crop: string}} Base64 text of the JPEG frame and the PNG crop.
 * @throws {Error} When the video has no frame to copy yet.
 */
function captureImages(mark) {
  const width = dom.video.videoWidth;
  const height = dom.video.videoHeight;
  if (!width || !height || dom.video.readyState < HAVE_CURRENT_DATA) {
    throw new Error('frame_jpeg: the video has not shown this frame yet. Wait a moment, then press Enter again.');
  }
  const frame = document.createElement('canvas');
  frame.width = width;
  frame.height = height;
  frame.getContext('2d').drawImage(dom.video, 0, 0, width, height);

  const area = cropRect(mark.point, mark.box, width, height, CROP_SIZE);
  const crop = document.createElement('canvas');
  crop.width = area.sw;
  crop.height = area.sh;
  crop.getContext('2d').drawImage(frame, area.sx, area.sy, area.sw, area.sh, 0, 0, area.sw, area.sh);
  return {
    frame: stripDataUrl(frame.toDataURL('image/jpeg', JPEG_QUALITY)),
    crop: stripDataUrl(crop.toDataURL('image/png')),
  };
}

/**
 * Save the pending mark as a sighting.
 * @param {boolean} stay True keeps the video paused after the save (Shift+Enter), false plays on.
 * @returns {Promise<void>} Settles after the server stored or refused the sighting.
 */
async function savePending(stay) {
  const pending = state.pending;
  if (!pending || state.saving) {
    return;
  }
  if (!pending.species) {
    pending.error = null;
    toast('Pick a species first.', 'error', TOAST_MS);
    renderMarkBar();
    return;
  }
  state.saving = true;
  pending.error = null;
  renderMarkBar();
  const key = state.key;
  let row;
  try {
    await waitForSeek();
    const images = captureImages(pending);
    const answer = await request('POST', '/api/observations', {
      key,
      time: Math.round(dom.video.currentTime * 1000) / 1000,
      point: { x: pending.point.x, y: pending.point.y },
      box: pending.box ? { x: pending.box.x, y: pending.box.y, w: pending.box.w, h: pending.box.h } : null,
      species: pending.species,
      note: dom.note.value,
      frame_jpeg: images.frame,
      crop_png: images.crop,
    });
    if (!answer.row || typeof answer.row.ID !== 'string') {
      throw new ApiError('row: the server answered without the saved row. Check observations.csv before you mark this sponge again.', 0);
    }
    row = answer.row;
  } catch (error) {
    state.saving = false;
    pending.error = `Not saved. ${endSentence(error.message)}`;
    toast(pending.error, 'error');
    renderMarkBar();
    return;
  }
  state.saving = false;
  if (key === state.key) {
    state.rows.push(row);
    markFresh(row.ID);
  }
  adjustVideoEntry(key, 1);
  endMark();
  renderSightings();
  renderBrowser();
  refreshTally();
  toast(`Saved ${row.ID} ${row.SpeciesCode}`);
  if (!stay) {
    resumePlayback();
  }
}

// ------------------------------------------------------- sightings and tally

/**
 * Change the sighting count of a video row after a save or a delete.
 * @param {string} key The S3 key of the video.
 * @param {number} change 1 after a save, -1 after a delete.
 */
function adjustVideoEntry(key, change) {
  const entry = videoEntry(key);
  if (entry) {
    entry.sightings = Math.max(0, (Number(entry.sightings) || 0) + change);
    if (entry.status === 'new') {
      entry.status = 'in progress';
    }
  }
}

/**
 * Light up a freshly saved row for a moment.
 * @param {string} id The sighting ID.
 */
function markFresh(id) {
  runtime.freshId = id;
  window.clearTimeout(runtime.freshTimer);
  runtime.freshTimer = window.setTimeout(() => {
    runtime.freshId = null;
    renderSightings();
  }, FRESH_ROW_MS);
}

/** Draw the sightings of the open video, ordered along the timeline. */
function renderSightings() {
  const rows = sortRowsByTime(state.rows);
  dom.sightingsCount.textContent = String(rows.length);
  dom.sightingsEmpty.hidden = rows.length > 0;
  dom.sightingsEmpty.textContent = state.key
    ? 'No sightings in this video yet.'
    : 'Open a video to see its sightings.';
  dom.sightings.replaceChildren(...rows.map(sightingNode));
  const fresh = dom.sightings.querySelector('[data-fresh="true"]');
  if (fresh) {
    fresh.scrollIntoView({ block: 'nearest' });
  }
  renderScrubMarks();
}

/**
 * Build one sighting row: the crop, the species, the time, the note, and the delete button.
 * @param {Object<string, string>} row An observation row from the server.
 * @returns {HTMLElement} The list item.
 */
function sightingNode(row) {
  const species = speciesByCode(row.SpeciesCode);
  const name = species ? species.name : row['Sponge Type'] || row.SpeciesCode;
  const clock = row.Timestamp || formatClock(Number(row.TimestampSeconds));
  const thumb = el('img', {
    class: 'sighting-thumb',
    attrs: {
      src: `/media/crops/${encodeURIComponent(row.CropFileName || '')}`,
      alt: '',
      width: '44',
      height: '44',
      loading: 'lazy',
    },
  });
  thumb.addEventListener('error', () => {
    thumb.dataset.missing = 'true';
  }, { once: true });

  const jump = el('button', {
    class: 'sighting-jump',
    title: `You jump to ${row.ID} at ${clock} and the video pauses there. ${name}. ${row.Notes || ''}`.trim(),
    attrs: { type: 'button' },
    data: { id: row.ID },
  }, [
    thumb,
    el('span', { class: 'sighting-text' }, [
      el('span', { class: 'sighting-line' }, [
        el('span', { class: 'sighting-code', text: row.SpeciesCode }),
        el('span', { class: 'sighting-time', text: clock }),
      ]),
      el('span', { class: 'sighting-sub', text: `${row.ID} · ${row.Notes || ''}` }),
    ]),
  ]);

  const armed = runtime.armedDelete === row.ID;
  const remove = el('button', {
    class: 'button button-icon sighting-delete',
    title: armed
      ? `You confirm: the server removes ${row.ID} and moves its two images to the trash folder.`
      : `You remove ${row.ID}. The button asks once more before anything is removed.`,
    attrs: { type: 'button', 'aria-label': armed ? `Confirm removing ${row.ID}` : `Remove ${row.ID}` },
    data: { id: row.ID, armed: String(armed) },
  }, [armed ? 'Remove?' : icon('close')]);

  return el('li', {
    class: 'sighting',
    data: {
      id: row.ID,
      time: row.TimestampSeconds,
      code: row.SpeciesCode,
      fresh: String(runtime.freshId === row.ID),
      armed: String(armed),
    },
  }, [jump, remove]);
}

/**
 * React to a click in the sightings list: jump to a sighting, or arm and confirm its removal.
 * @param {MouseEvent} event The click.
 */
function onSightingsClick(event) {
  const remove = event.target.closest('.sighting-delete');
  if (remove) {
    const id = remove.dataset.id;
    if (runtime.armedDelete === id) {
      disarmDelete();
      deleteSighting(id);
    } else {
      runtime.armedDelete = id;
      window.clearTimeout(runtime.armedTimer);
      runtime.armedTimer = window.setTimeout(() => {
        disarmDelete();
        renderSightings();
      }, DELETE_ARM_MS);
      renderSightings();
    }
    return;
  }
  const jump = event.target.closest('.sighting-jump');
  if (jump) {
    const row = state.rows.find((item) => item.ID === jump.dataset.id);
    if (row && isVideoReady()) {
      dom.video.pause();
      seekTo(Number(row.TimestampSeconds));
    }
  }
}

/** Return an armed delete button to its resting state. */
function disarmDelete() {
  window.clearTimeout(runtime.armedTimer);
  runtime.armedDelete = null;
  runtime.armedTimer = null;
}

/**
 * Remove one sighting on the server and from the lists.
 * @param {string} id The sighting ID, such as "ID007".
 * @returns {Promise<void>} Settles after the server removed or refused the row.
 */
async function deleteSighting(id) {
  const key = state.key;
  try {
    const answer = await request('DELETE', `/api/observations/${encodeURIComponent(id)}`);
    const gone = answer.deleted || {};
    if (key === state.key) {
      state.rows = state.rows.filter((row) => row.ID !== id);
    }
    adjustVideoEntry(key, -1);
    toast(`Removed ${id} ${gone.SpeciesCode || ''}`.trim());
  } catch (error) {
    toast(`${id} was not removed. ${endSentence(error.message)}`, 'error');
  }
  renderSightings();
  renderBrowser();
  refreshTally();
  drawOverlay();
}

/** Remove the sighting that was saved last in this video. The Z key runs this. */
function undoLastSighting() {
  const last = lastSighting(state.rows);
  if (!last) {
    toast('This video has no sighting to remove.', 'info');
    return;
  }
  deleteSighting(last.ID);
}

/**
 * Load the tally across all videos.
 * @returns {Promise<void>} Settles after the table shows the tally or the error.
 */
async function refreshTally() {
  try {
    const answer = await request('GET', '/api/tally');
    state.tally = Array.isArray(answer.tally) ? answer.tally : [];
    state.tallyTotal = Number(answer.total) || 0;
    state.tallyError = null;
  } catch (error) {
    state.tallyError = `The tally did not load. ${endSentence(error.message)}`;
  }
  renderTally();
}

/** Draw the tally table: pinned species first, rows under the target highlighted. */
function renderTally() {
  dom.tallyTarget.textContent = `target ${state.tallyTarget}`;
  dom.tallyEmpty.hidden = !state.tallyError;
  dom.tallyEmpty.textContent = state.tallyError || '';
  dom.tallyTotal.textContent = state.tallyTotal === 1
    ? '1 sighting across all videos.'
    : `${state.tallyTotal} sightings across all videos.`;
  const rows = mergeTally(state.tally, state.pins, state.species);
  const nodes = [];
  let dividerPlaced = false;
  for (const row of rows) {
    if (row.pin === null && !dividerPlaced && nodes.length > 0) {
      nodes.push(el('tr', { class: 'tally-divider' }, [
        el('td', { text: 'Other species', attrs: { colspan: '4' } }),
      ]));
    }
    dividerPlaced = dividerPlaced || row.pin === null;
    const under = row.sightings < state.tallyTarget;
    const pinBox = el('span', {
      class: row.pin ? 'tally-pin' : 'tally-pin tally-pin-none',
      text: row.pin || '',
      attrs: { 'aria-hidden': 'true' },
    });
    nodes.push(el('tr', {
      title: `${row.name}: ${row.sightings} sightings in ${row.videos} videos. ${under ? `It needs ${state.tallyTarget - row.sightings} more to reach the target.` : 'It reached the target.'}`,
      data: { code: row.code, under: under ? '1' : '0', pinned: row.pin ? '1' : '0' },
    }, [
      el('td', {}, [el('span', { class: 'tally-code' }, [pinBox, row.code])]),
      el('td', { class: 'num tally-seen', text: under ? `${row.sightings}/${state.tallyTarget}` : String(row.sightings) }),
      el('td', { class: 'num', text: String(row.videos) }),
      el('td', { class: 'num', text: row.earliest_year === null ? '' : String(row.earliest_year) }),
    ]));
  }
  dom.tallyRows.replaceChildren(...nodes);
}

// ------------------------------------------------- settings, export, and help

/**
 * Send the annotator field to the server and show the server's verdict.
 * @returns {Promise<void>} Settles after the server stored or refused the value.
 */
async function saveAnnotator() {
  const value = dom.annotator.value.trim();
  if (value === state.annotator) {
    dom.annotator.value = state.annotator;
    dom.annotator.removeAttribute('aria-invalid');
    return;
  }
  try {
    applySettings(await request('POST', '/api/settings', { annotator: value }));
    dom.annotator.value = state.annotator;
    dom.annotator.removeAttribute('aria-invalid');
    toast(`Annotator set to ${state.annotator}`);
  } catch (error) {
    dom.annotator.setAttribute('aria-invalid', 'true');
    toast(`The annotator was not saved, so sightings still carry ${state.annotator}. ${endSentence(error.message)}`, 'error');
  }
}

/**
 * Ask the server for the export package and show where it went.
 * @returns {Promise<void>} Settles after the server wrote or refused the package.
 */
async function exportPackage() {
  dom.exportButton.disabled = true;
  try {
    const answer = await request('POST', '/api/export', {});
    toast(`Exported ${answer.observations} sightings to ${answer.path}`, 'ok', LONG_TOAST_MS);
  } catch (error) {
    toast(`The export failed. ${endSentence(error.message)}`, 'error', LONG_TOAST_MS);
  } finally {
    dom.exportButton.disabled = false;
  }
}

/**
 * Open or close the shortcuts card.
 * @param {boolean} [open] True opens, false closes, and no value flips the card.
 */
function toggleHelp(open) {
  const show = open === undefined ? dom.help.hidden : open;
  dom.help.hidden = !show;
  dom.helpToggle.setAttribute('aria-expanded', String(show));
}

// ------------------------------------------------------------------- keyboard

/**
 * Tell whether an element is one of the text fields, where keys type normally.
 * @param {EventTarget} target The event target.
 * @returns {boolean} True for the annotator, search, filter, and note fields.
 */
function isTextField(target) {
  return target === dom.annotator || target === dom.search || target === dom.filter || target === dom.note;
}

/**
 * Route a key press. Text fields keep their typing keys. Everything else goes to the
 * handler of the current mode.
 * @param {KeyboardEvent} event The keydown.
 */
function onKeyDown(event) {
  if (event.metaKey || event.ctrlKey || event.altKey || event.isComposing) {
    return;
  }
  const key = keyFromEvent(event.key, event.code, event.shiftKey);
  if (key === 'Escape' && !dom.help.hidden) {
    toggleHelp(false);
    event.preventDefault();
    return;
  }
  if (isTextField(event.target)) {
    onTextFieldKey(event, key);
    return;
  }
  if (event.target instanceof HTMLButtonElement && (key === 'Enter' || key === ' ')) {
    return;
  }
  const handled = state.mode === 'marking' ? onMarkingKey(event, key) : onScreeningKey(event, key);
  if (handled) {
    event.preventDefault();
  }
}

/**
 * Handle the keys that keep a meaning inside a text field: Enter, Escape, and Tab, plus
 * Up, Down, and the species keys in the filter box.
 * @param {KeyboardEvent} event The keydown inside a text field.
 * @param {string} key The key value from keyFromEvent.
 */
function onTextFieldKey(event, key) {
  const field = event.target;
  const marking = state.mode === 'marking';
  if (field === dom.filter) {
    if (key === 'ArrowDown' || key === 'ArrowUp') {
      moveMatch(key === 'ArrowDown' ? 1 : -1);
      event.preventDefault();
      return;
    }
    const slot = pinKeyFromEvent(key, event.code);
    if (marking && slot) {
      choosePinned(slot);
      event.preventDefault();
      return;
    }
  }
  if (key === 'Enter') {
    event.preventDefault();
    onTextFieldEnter(field, event.shiftKey, event.repeat);
  } else if (key === 'Escape') {
    event.preventDefault();
    onTextFieldEscape(field);
  } else if (key === 'Tab' && marking && !event.shiftKey && field !== dom.note) {
    event.preventDefault();
    dom.note.focus();
  }
}

/**
 * Handle Enter inside a text field.
 * @param {HTMLInputElement} field The field that holds the keyboard.
 * @param {boolean} shift True when Shift is down.
 * @param {boolean} repeat True when the key repeats because it is held down.
 */
function onTextFieldEnter(field, shift, repeat) {
  const marking = state.mode === 'marking';
  if (field === dom.filter && state.filterQuery.trim() !== '') {
    const code = state.matches[state.matchIndex];
    if (!code) {
      toast(`No species matches "${state.filterQuery.trim()}".`, 'error', TOAST_MS);
    } else if (marking) {
      chooseSpecies(code);
    } else {
      armPin(code);
    }
    return;
  }
  if (field === dom.search) {
    openFirstSearchResult();
    return;
  }
  if (field === dom.annotator) {
    field.blur();
    return;
  }
  if (marking && !repeat) {
    savePending(shift);
  }
}

/**
 * Handle Escape inside a text field: cancel the mark in marking mode, otherwise leave the field.
 * @param {HTMLInputElement} field The field that holds the keyboard.
 */
function onTextFieldEscape(field) {
  if (state.mode === 'marking') {
    cancelMark();
    return;
  }
  if (field === dom.filter) {
    clearFilter();
    disarmPin();
  } else if (field === dom.search) {
    dom.search.value = '';
    state.search = '';
    renderBrowser();
  } else if (field === dom.annotator) {
    dom.annotator.value = state.annotator;
    dom.annotator.removeAttribute('aria-invalid');
  }
  field.blur();
}

/**
 * Handle a key in marking mode, outside the text fields.
 * @param {KeyboardEvent} event The keydown.
 * @param {string} key The key value from keyFromEvent.
 * @returns {boolean} True when the key was used, so the browser default is blocked.
 */
function onMarkingKey(event, key) {
  const slot = pinKeyFromEvent(key, event.code);
  if (slot) {
    choosePinned(slot);
    return true;
  }
  if (key === 'Enter') {
    if (!event.repeat) {
      savePending(event.shiftKey);
    }
    return true;
  }
  if (key === 'Escape') {
    cancelMark();
    return true;
  }
  if (key === 'Tab' && !event.shiftKey) {
    dom.note.focus();
    return true;
  }
  if (key === ',' || key === '.') {
    stepFrame(key === '.' ? 1 : -1);
    return true;
  }
  if (key === ' ' || key === 'ArrowUp' || key === 'ArrowDown') {
    return true;
  }
  if (key === '?') {
    toggleHelp();
    return true;
  }
  if (/^[a-z]$/i.test(key)) {
    typeIntoFilter(key);
    return true;
  }
  return false;
}

/**
 * Handle a key in screening mode, outside the text fields.
 * @param {KeyboardEvent} event The keydown.
 * @param {string} key The key value from keyFromEvent.
 * @returns {boolean} True when the key was used, so the browser default is blocked.
 */
function onScreeningKey(event, key) {
  if (key === 'Escape' && state.pinCandidate) {
    disarmPin();
    return true;
  }
  if (key === ' ') {
    togglePlay();
    return true;
  }
  if (key === 'ArrowLeft' || key === 'ArrowRight') {
    seekTo(dom.video.currentTime + (key === 'ArrowRight' ? JUMP_SECONDS : -JUMP_SECONDS));
    return true;
  }
  if (key === ',' || key === '.') {
    stepFrame(key === '.' ? 1 : -1);
    return true;
  }
  if (key === '[' || key === ']') {
    setSpeed(stepSpeed(state.speed, key === ']' ? 1 : -1));
    return true;
  }
  if (key === 'Home') {
    seekTo(0);
    return true;
  }
  if (key === '?') {
    toggleHelp();
    return true;
  }
  if (pinKeyFromEvent(key, event.code)) {
    if (state.key && !event.repeat) {
      toast('Click a sponge in the video first. Then press its species key.', 'info');
    }
    return true;
  }
  const letter = key.length === 1 ? key.toLowerCase() : '';
  if (event.repeat) {
    return letter === 'g' || letter === 'z' || letter === 'd' || letter === 'n';
  }
  if (letter === 'g') {
    setGrid(!state.grid);
    return true;
  }
  if (letter === 'z') {
    undoLastSighting();
    return true;
  }
  if (letter === 'd') {
    toggleDone();
    return true;
  }
  if (letter === 'n') {
    openNextVideo();
    return true;
  }
  return false;
}

// -------------------------------------------------------------------- startup

/** Attach every event listener of the page. */
function wireEvents() {
  document.addEventListener('keydown', onKeyDown);
  document.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button && event.detail > 0) {
      button.blur();
    }
  });
  window.addEventListener('blur', abandonDrag);
  window.addEventListener('pagehide', savePosition);

  dom.breadcrumb.addEventListener('click', onFolderClick);
  dom.folders.addEventListener('click', onFolderClick);
  dom.videos.addEventListener('click', onVideoListClick);
  dom.search.addEventListener('input', () => {
    state.search = dom.search.value;
    renderBrowser();
  });
  dom.refresh.addEventListener('click', () => loadCatalog(state.prefix, true));
  dom.catalogRetry.addEventListener('click', () => loadCatalog(runtime.failedPrefix, runtime.failedRefresh));

  dom.overlay.addEventListener('mousedown', onStageMouseDown);
  dom.stageAction.addEventListener('click', () => {
    if (runtime.statusAction) {
      runtime.statusAction.run();
    }
  });
  new ResizeObserver(drawOverlay).observe(dom.stage);

  dom.video.addEventListener('loadedmetadata', onLoadedMetadata);
  dom.video.addEventListener('timeupdate', onTimeUpdate);
  dom.video.addEventListener('seeked', onTimeUpdate);
  dom.video.addEventListener('ended', onEnded);
  dom.video.addEventListener('error', onVideoError);
  dom.video.addEventListener('waiting', () => {
    if (state.status === null && !dom.video.paused) {
      setStatus('buffering', 'Buffering the video.');
    }
  });
  for (const name of ['canplay', 'playing']) {
    dom.video.addEventListener(name, () => clearStatus(['loading', 'buffering', 'network']));
  }
  dom.video.addEventListener('playing', () => {
    runtime.playFailures = 0;
  });
  for (const name of ['play', 'pause', 'emptied', 'durationchange']) {
    dom.video.addEventListener(name, renderTransport);
  }
  dom.video.addEventListener('emptied', renderMarkBar);
  dom.video.addEventListener('pause', savePosition);

  dom.play.addEventListener('click', togglePlay);
  dom.scrub.addEventListener('pointerdown', () => {
    runtime.scrubbing = true;
  });
  dom.scrub.addEventListener('input', () => seekTo(Number(dom.scrub.value)));
  dom.scrub.addEventListener('change', () => {
    runtime.scrubbing = false;
    dom.scrub.blur();
  });
  dom.slower.addEventListener('click', () => setSpeed(stepSpeed(state.speed, -1)));
  dom.faster.addEventListener('click', () => setSpeed(stepSpeed(state.speed, 1)));
  dom.grid.addEventListener('click', () => setGrid(!state.grid));
  dom.done.addEventListener('click', toggleDone);
  dom.next.addEventListener('click', openNextVideo);

  dom.pins.addEventListener('click', onPinsClick);
  dom.pins.addEventListener('mousedown', keepFilterFocus);
  dom.matches.addEventListener('mousedown', keepFilterFocus);
  dom.matches.addEventListener('click', onMatchClick);
  dom.filter.addEventListener('input', () => {
    state.matchIndex = 0;
    updateMatches();
  });
  dom.filter.addEventListener('focus', () => {
    state.filterOpen = true;
    updateMatches();
  });
  dom.filter.addEventListener('blur', () => {
    state.filterOpen = false;
    renderMatches();
  });
  dom.speciesRetry.addEventListener('click', loadSpecies);

  dom.save.addEventListener('click', () => savePending(false));
  dom.saveStay.addEventListener('click', () => savePending(true));
  dom.cancel.addEventListener('click', cancelMark);

  dom.sightings.addEventListener('click', onSightingsClick);
  dom.annotator.addEventListener('change', saveAnnotator);
  dom.exportButton.addEventListener('click', exportPackage);
  dom.helpToggle.addEventListener('click', () => toggleHelp());
  dom.helpClose.addEventListener('click', () => toggleHelp(false));
}

/**
 * Keep the keyboard in the filter box when the mouse presses a filter match or a pin
 * control, so the highlighted species survives the click.
 * @param {MouseEvent} event The mousedown.
 */
function keepFilterFocus(event) {
  if (event.target.closest('.match, .pin-assign')) {
    event.preventDefault();
  }
}

/** Read the remembered grid and speed choices from localStorage. */
function restorePreferences() {
  state.grid = storageGet(STORAGE.grid) !== '0';
  dom.grid.setAttribute('aria-pressed', String(state.grid));
  const speed = Number(storageGet(STORAGE.speed));
  setSpeed(SPEEDS.includes(speed) ? speed : 1);
}

/**
 * Start the page: wire the events, then load the species, the tally, and the folder
 * that was open last time.
 * @returns {Promise<void>} Settles after the three first requests returned.
 */
async function start() {
  wireEvents();
  restorePreferences();
  setMode('screening');
  setStatus('idle', IDLE_TEXT);
  renderPins();
  renderMarkBar();
  renderSightings();
  renderDone();
  renderTransport();

  const remembered = storageGet(STORAGE.prefix);
  const first = remembered && remembered.startsWith(ROOT_PREFIX) ? remembered : ROOT_PREFIX;
  await Promise.all([
    loadSpecies(),
    refreshTally(),
    loadCatalog(first).then(() => (
      state.catalogError && first !== ROOT_PREFIX ? loadCatalog(ROOT_PREFIX) : undefined
    )),
  ]);
}

window.__screener = { state, pictureRect: pictureRectInViewport };
start();
