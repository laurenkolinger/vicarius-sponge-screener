/**
 * Sponge Screener page logic.
 *
 * The page has two keyboard modes. In screening mode the keys drive the video. A
 * click or a drag on the picture pauses the video, starts a mark, and opens the
 * species wheel on it (marking mode). One pick saves the sighting and the video stays
 * paused on that frame while the images upload (Shift with the pick plays on); the hub
 * saves a Later point without a species. The server follows every sighting through its
 * video, and a saved mark rides along its track on every frame the video shows. A click
 * on a drawn mark, paused or playing, opens the same wheel to change that row's species.
 *
 * Every calculation lives in geometry.js, wheel.js, and track_math.js, which have their
 * own node tests; wheel_view.js draws the wheel. This file wires those pieces to the DOM,
 * the video element, and the HTTP contract of screener/server.py. Server text reaches the
 * DOM through textContent only.
 *
 * Under VICARIUS (the server says it keeps runs), the page reads its run folder from
 * ?project_dir=, asks the server for the run's name, and names that run on every call;
 * vicarius_run.js holds those rules and the signed-in initials rule.
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
  discardedText,
  cropRect,
  endSentence,
  exportSummary,
  filterByName,
  filterSpecies,
  formatBytes,
  formatClock,
  flashView,
  formatLabel,
  isDrag,
  isUsableBox,
  keyFromEvent,
  labelStatus,
  lastSighting,
  latestUnsaved,
  leaveBlocked,
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
  sightingsCount,
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
  unsavedSummary,
} from './geometry.js';
import {
  WHEEL,
  deleteKeyAction,
  deleteKeyTiming,
  placeWheel,
  rowLabel,
  seenCounts,
  shortLabel,
  spaceAction,
  wheelScale,
} from './wheel.js';
import { createWheel } from './wheel_view.js';
import { createScrubber } from './scrub.js';
import { markAt, marksAt, readTrackAnswer } from './track_math.js';
import {
  RUN_HEADER,
  initialsFromSearch,
  projectDirFromSearch,
  startingInitials,
  upperCaseAsTyped,
  withRun,
} from './vicarius_run.js';

// ------------------------------------------------------------------ constants

const ROOT_PREFIX = 'TCRMP_video_ondeck/';
const MUTATION_HEADER = 'X-Screener';
const JPEG_QUALITY = 0.95;
const FRAME_STEP_SECONDS = 1 / 30;
const POLL_MS = 2000;
const NETWORK_RETRY_MS = 3000;
const POSITION_SAVE_MS = 1000;
/** A saved mark without a track that covers the time on screen shows this close to its time. */
const SAVED_MARK_WINDOW_SECONDS = 0.6;
/** While a track of the open video is still being made, the page asks for the tracks this often. */
const TRACK_POLL_MS = 3000;
const TOAST_MS = 2600;
const ERROR_TOAST_MS = 7000;
const LONG_TOAST_MS = 12000;
const MAX_TOASTS = 4;
const DELETE_ARM_MS = 3000;
const FRESH_ROW_MS = 1600;
/** An open guide follows the lit wheel species after this pause, so a sweep across slices does not redraw it at every one. */
const GUIDE_FOLLOW_MS = 120;
/** Z waits for the saves in flight; after this long it says so. */
const UNDO_NOTICE_MS = 300;
/** Z stops waiting for a save that has not answered after this long, and does nothing. */
const UNDO_WAIT_LIMIT_MS = 10000;
const HAVE_CURRENT_DATA = 2;
const DEFAULT_SPEED = 1.5;
const MARK_COLOR = '#ff2bd6';
const MARK_INK = '#1f0019';
const LATER_COLOR = '#ffb020';
const LATER_INK = '#2a1a00';
const LATER_DASH = [5, 4];
const FAILED_COLOR = '#e5484d';
const FAILED_INK = '#ffffff';
const QUADRANT_FILL = 'rgba(255, 255, 255, 0.12)';
const CANVAS_FONT = '700 13px ui-monospace, "SF Mono", Menlo, monospace';
const TAG_HEIGHT = 20;
const TICK_REACH = 10;
/** A click this close to a drawn saved mark, paused or playing, opens the wheel for that row. */
/** A click this close to a mark's center edits it: the ring and its ticks reach 18 px, and the
    rest leaves room for a click that comes a moment after a moving mark has moved on. */
const EDIT_REACH_PX = 26;
/** The wheel keeps this much room between its outer ring and the edge of the view. */
const WHEEL_MARGIN_PX = 12;
/** A flash label stays on the picture this long after a pick, fading over the last part. */
const FLASH_MS = 1500;
const FLASH_FADE_MS = 400;
const FLASH_NAME_CHARS = 12;
const FLASH_RING = { width: 2, radius: 9 };
/** File extensions Chrome plays directly, for a video opened from the To label list. */
const PLAYABLE_EXTENSIONS = Object.freeze(['mp4', 'm4v', 'mov']);
/** Elements a mousedown may reach while the wheel is open without cancelling the mark. */
const MARKING_ZONES = '#wheel, #overlay, #pins, .combo, #note, #cancel, #fullscreen, #help, #guide, #guide-large, #toasts';
const SVG_NS = 'http://www.w3.org/2000/svg';
const STORAGE = {
  position: 'screener.position.',
  prefix: 'screener.prefix',
  grid: 'screener.grid',
  deck: 'screener.deck',
  guideDock: 'screener.guideDock',
  speed: 'screener.speed.v2',
};
const IDLE_TEXT = 'Pick a video from the list on the left. Then click a sponge and pick its species in the wheel.';
const NO_RUN_TEXT = 'Open a run from the Sponge Screener card on the VICARIUS desktop.';
const NO_INITIALS_TEXT = 'Type your initials in Annotator before you open a video.';
/** The keys that still work while the page has no run: the shortcut card and closing it. */
const NO_RUN_KEYS = new Set(['?', 'Escape']);
/** The controls that call the server, disabled while the page has no run to work in. */
const NO_RUN_DISABLED = [
  'annotator', 'exportButton', 'guideToggle', 'search', 'refresh', 'slower', 'faster', 'grid', 'deckToggle', 'done', 'next',
  'fullscreen',
];

// ---------------------------------------------------------------------- state

/**
 * Everything the page knows, as plain data. The end-to-end driver reads this
 * object through window.__screener.state, so it is changed in place and never
 * replaced, and it holds no DOM nodes or timers.
 */
const state = {
  mode: 'screening',
  run: '',
  key: null,
  name: null,
  source: null,
  videoStatus: 'new',
  pending: null,
  rows: [],
  tracks: {},
  trackPending: [],
  toLabel: [],
  toLabelError: null,
  species: [],
  pins: {},
  annotator: '',
  tallyTarget: 3,
  tally: [],
  tallyTotal: 0,
  tallyUnlabeled: 0,
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
  deckShown: false,
  guideDocked: true,
  speed: DEFAULT_SPEED,
  fullscreen: false,
  conversion: null,
  queue: {},
  status: { kind: 'idle', text: IDLE_TEXT },
  lastToast: null,
};

/** Timers, the live drag, and other values that are not plain page data. */
const runtime = {
  loadToken: 0,
  annotatorSave: null,
  annotatorSaveValue: '',
  queryEditedAt: -Infinity,
  deleteArmedAt: null,
  guideFollowTimer: null,
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
  flashes: [],
  flashFrame: 0,
  unsaved: [],
  unsavedToast: null,
  inFlight: new Set(),
  settleWaiters: [],
  undoing: false,
  pickCount: 0,
  jump: null,
  toLabelToken: 0,
  tallyToken: 0,
  trackTimer: null,
  trackRequest: 0,
  frameCallback: 0,
  /** The media time of the frame on screen, from requestVideoFrameCallback; null during a seek. */
  presentedTime: null,
  /** The saved marks as last drawn, with their centers in stage pixels, for hit testing. */
  drawn: { time: NaN, marks: [] },
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
  annotator: byId('annotator-initials'),
  runName: byId('run-name'),
  exportButton: byId('export'),
  helpToggle: byId('help-toggle'),
  help: byId('help'),
  helpClose: byId('help-close'),
  guideToggle: byId('guide-toggle'),
  guide: byId('guide'),
  guideClose: byId('guide-close'),
  guideDock: byId('guide-dock'),
  guideSpecies: byId('guide-species'),
  guideCount: byId('guide-count'),
  guideText: byId('guide-text'),
  guideImages: byId('guide-images'),
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
  theater: byId('theater'),
  wheelLayer: byId('wheel-layer'),
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
  deckToggle: byId('deck-toggle'),
  done: byId('done'),
  doneLabel: byId('done-label'),
  next: byId('next'),
  fullscreen: byId('fullscreen'),
  fullscreenIcon: byId('fullscreen-icon'),
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
  cancel: byId('cancel'),
  sightings: byId('sightings'),
  sightingsCount: byId('sightings-count'),
  sightingsEmpty: byId('sightings-empty'),
  toLabel: byId('to-label'),
  toLabelCount: byId('to-label-count'),
  toLabelEmpty: byId('to-label-empty'),
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
  if (state.run) {
    options.headers[RUN_HEADER] = encodeURIComponent(state.run);
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
 * @param {number} [duration] Milliseconds on screen. Errors stay longer by default, and
 *   Infinity keeps the toast until the code removes it.
 * @param {?{label: string, title: string, run: Function}} [action] A button inside the toast.
 * @returns {HTMLElement} The toast, so a caller can change or remove it later.
 */
function toast(message, kind = 'ok', duration, action = null) {
  const text = String(message);
  for (const old of Array.from(dom.toasts.children)) {
    if (old.dataset.text === text && old.dataset.sticky !== 'true') {
      old.remove();
    }
  }
  const removable = () => Array.from(dom.toasts.children).find((node) => node.dataset.sticky !== 'true');
  while (dom.toasts.children.length >= MAX_TOASTS && removable()) {
    removable().remove();
  }
  const life = duration || (kind === 'error' ? ERROR_TOAST_MS : TOAST_MS);
  const node = el('div', {
    class: 'toast',
    data: { kind, text, sticky: String(life === Infinity) },
  }, [el('span', { class: 'toast-text', text })]);
  if (action) {
    const button = el('button', {
      class: 'button toast-action',
      text: action.label,
      title: action.title,
      attrs: { type: 'button' },
    });
    button.addEventListener('click', action.run);
    node.append(button);
  }
  dom.toasts.append(node);
  state.lastToast = { message: text, kind };
  if (life !== Infinity) {
    window.setTimeout(() => node.remove(), life);
  }
  return node;
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
      title: `Open the folder ${crumb.name} in the bucket.`,
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
    title: `Go up one folder, to ${name}.`,
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
    title: `Open the folder ${folder.name} in the bucket.`,
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
  const later = Number(video.unlabeled) || 0;
  const meta = el('span', { class: 'video-meta' }, [
    el('span', {
      class: 'badge',
      text: statusLabel(status, video.sightings),
      title: 'New means nobody has opened the video, and in progress means it was opened, with the number counting its sightings. Done means a person marked it fully screened.',
      data: { status },
    }),
    later > 0 ? el('span', {
      class: 'video-later',
      text: `· ${later} later`,
      title: 'Points saved for later in the video, each still waiting for a species. Give them species from the To label list.',
    }) : '',
    el('span', {
      class: 'format',
      text: format.text,
      title: 'Plays now means Chrome plays the format directly. Needs conversion means Sponge Screener converts the file with FFmpeg first, which takes a few minutes.',
      data: { format: format.kind },
    }),
  ]);

  const size = formatBytes(video.size);
  const open = el('button', {
    class: 'video-open',
    title: `Open ${video.name}${size ? ` (${size})` : ''}. It plays from where it was left, minus 2 seconds.`,
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
    ? 'Queue the video for background download into the cache folder, so it plays later without waiting.'
    : 'Queue the video for background conversion. Sponge Screener downloads it and converts it with FFmpeg so Chrome can play it.';
  let content = icon('queue');
  let label = `Queue ${video.name} for background ${verb}`;
  if (job && job.state === 'working') {
    content = percentText(job.fraction);
    label = `Background ${verb} of ${video.name}: ${percentText(job.fraction)}`;
    title = `Sponge Screener is working on the video in the background: ${percentText(job.fraction)}.`;
  } else if (job && job.state === 'ready') {
    content = icon('check');
    label = `${video.name} is ready in the cache folder`;
    title = 'The video is ready in the cache folder and plays without waiting.';
  } else if (job && job.state === 'failed') {
    content = '!';
    label = `Background ${verb} of ${video.name} failed`;
    title = `The background ${verb} failed, so click to try again. ${endSentence(job.message)}`;
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
    toast('This video is already in the cache folder.', 'info');
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
        ? `Save the open mark as ${name || code}; the video stays paused. The ${slot} key does the same, and Shift with it plays on.`
        : `Key ${slot} holds no species yet. Highlight a species in the filter list, then click the pin in the corner of the key.`,
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
      title: `Pin the species highlighted in the filter list to key ${slot}. Sponge Screener keeps the pins in settings.json in the data folder.`,
      attrs: { type: 'button', 'aria-label': `Pin the highlighted species to key ${slot}` },
      data: { slot },
    }, [icon('pin')]);
    return el('div', {
      class: 'pin-slot',
      attrs: { role: 'group' },
      data: { slot, code },
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
    choosePinned(choose.dataset.slot, event.shiftKey);
  }
}

/**
 * Save the mark on screen as the species pinned to a key.
 * @param {string} slot The key, "1" to "9" or "0".
 * @param {boolean} shift True when Shift was held, which plays on after the save.
 */
function choosePinned(slot, shift) {
  if (state.mode !== 'marking') {
    toast('Click a sponge in the video first. Then press its species key.', 'info');
    return;
  }
  const code = state.pins[slot];
  if (!code) {
    toast(`Slot ${slot} is empty.`, 'info');
    return;
  }
  pickSpecies(code, shift);
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
        ? `Save the open mark as ${species ? species.name : code}; the video stays paused.`
        : `Hold ${species ? species.name : code} ready for pinning, then click the pin in the corner of a number key to put it there.`,
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
 * React to a click on a filter match: in marking mode save the mark as that species,
 * otherwise hold it ready for pinning.
 * @param {MouseEvent} event The click.
 */
function onMatchClick(event) {
  const row = event.target.closest('.match');
  if (!row) {
    return;
  }
  state.matchIndex = Math.max(0, state.matches.indexOf(row.dataset.code));
  if (state.mode === 'marking') {
    pickSpecies(row.dataset.code, event.shiftKey);
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
  window.clearTimeout(runtime.trackTimer);
  runtime.convertTimer = null;
  runtime.retryTimer = null;
  runtime.trackTimer = null;
}

/**
 * Open a video: tell the server, load its sightings and their tracks, and start playback,
 * through conversion when Chrome cannot play the format. Without initials in Annotator the
 * video stays closed and the field is marked, because the server records who opened it.
 * @param {object} entry A catalog video entry ({key, name, playable, converted, status}).
 * @param {?{key: string, time: number, row: object}} [jump] A point to land on once the video
 *   loads: the video seeks there instead of the remembered position, pauses, and opens the
 *   wheel on the row. The To label list passes this.
 * @returns {Promise<void>} Settles after the open call and the sightings call returned.
 */
async function openVideo(entry, jump = null) {
  if (!state.annotator && dom.annotator.value.trim()) {
    // Initials typed and not saved yet: a click on a video blurs the field, and its save
    // is still on the way when the click arrives.
    await saveAnnotator();
  }
  if (!state.annotator) {
    // The server records who opened a video and refuses without initials, so a
    // video opened now would never get its screening row.
    askForInitials();
    dom.annotator.focus();
    return;
  }
  scrubber.stop();
  if (state.pending) {
    endMark();
  }
  savePosition();
  stopVideoTimers();
  runtime.jump = jump;
  runtime.loadToken += 1;
  runtime.playFailures = 0;
  const token = runtime.loadToken;
  state.key = entry.key;
  state.name = entry.name;
  state.rows = [];
  state.tracks = {};
  state.trackPending = [];
  state.conversion = null;
  state.videoStatus = entry.status === 'done' ? 'done' : 'in progress';
  dom.currentVideo.textContent = entry.name;
  dom.currentVideo.title = `The open video's full bucket key is ${entry.key}.`;
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
  loadTracks();

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
    title: 'Convert the open video again. Sponge Screener queues it behind any conversion already running.',
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
      title: 'Load the converted copy of the open video again from the server.',
      run: () => playSource('converted'),
    });
  }
}

/**
 * Take over the length of a freshly loaded video and jump to the remembered position, or
 * to the point the To label list asked for, which takes precedence.
 */
function onLoadedMetadata() {
  dom.scrub.max = String(Number.isFinite(dom.video.duration) ? dom.video.duration : 0);
  const jump = runtime.jump && runtime.jump.key === state.key ? runtime.jump : null;
  runtime.jump = null;
  const start = jump
    ? clampTime(jump.time, dom.video.duration)
    : resumePosition(storageGet(STORAGE.position + state.key), dom.video.duration);
  if (jump) {
    dom.video.pause();
  }
  if (start > 0) {
    dom.video.currentTime = start;
  }
  clearStatus(['loading', 'network']);
  renderTransport();
  renderScrubMarks();
  renderMarkBar();
  drawOverlay();
  if (jump) {
    landJump(jump);
  }
}

/**
 * Open the wheel on a row once the seek to its time has landed.
 * @param {{key: string, time: number, row: object}} jump The point to land on.
 * @returns {Promise<void>} Settles after the wheel opened, or at once when the video changed.
 */
async function landJump(jump) {
  await waitForSeek();
  if (state.key !== jump.key || !isVideoReady()) {
    return;
  }
  if (state.pending) {
    endMark();
  }
  dom.video.pause();
  openEdit(jump.row);
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
 * Show or hide the panel under the video (the pinned species keys and the note box), and
 * remember the choice. A species list that failed to load shows the panel anyway, by CSS.
 * @param {boolean} shown True to show the panel.
 */
function setDeck(shown) {
  state.deckShown = shown;
  document.body.dataset.deck = shown ? 'shown' : 'hidden';
  dom.deckToggle.setAttribute('aria-pressed', String(shown));
  storageSet(STORAGE.deck, shown ? '1' : '0');
}

/**
 * Dock the guide as a column beside the video, or let it float over the page, and remember
 * the choice. The overlay and an open wheel follow the stage's new size on their own.
 * @param {boolean} docked True to dock the guide.
 */
function setGuideDock(docked) {
  state.guideDocked = docked;
  document.body.dataset.guideDock = docked ? 'docked' : 'floating';
  dom.guideDock.setAttribute('aria-pressed', String(docked));
  dom.guideDock.setAttribute('aria-label', docked ? 'Let the guide float over the page' : 'Dock the guide beside the video');
  storageSet(STORAGE.guideDock, docked ? '1' : '0');
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

/** Draw one tick on the scrub bar per row of this video: pink for a sighting, amber for a Later point. */
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
      const tick = el('span', { class: 'scrub-mark', data: { id: row.ID, status: labelStatus(row) } });
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
  const later = state.rows.filter((row) => labelStatus(row) === 'unlabeled').length;
  if (done && later > 0) {
    // The server refuses too; the page answers first so the refusal needs no round trip.
    toast(`The done status was not saved. done: this video has ${later} unlabeled point(s). Label or delete them first, because a done video tells OnDeck that missing pinned species are absent.`, 'error');
    return;
  }
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

// --------------------------------------------------------------------- tracks

/**
 * Ask the server for the tracks of the open video, and ask again every TRACK_POLL_MS while
 * some are still being made. The server queues the sightings that have no track yet when
 * it answers, so the first answer for a video starts its tracking. Only the answer to the
 * newest request counts, and an answer for a video that is no longer open is dropped.
 * @returns {Promise<void>} Settles after the answer was taken in or dropped.
 */
async function loadTracks() {
  window.clearTimeout(runtime.trackTimer);
  runtime.trackTimer = null;
  if (!state.key) {
    return;
  }
  runtime.trackRequest += 1;
  const ask = runtime.trackRequest;
  const key = state.key;
  let answer = null;
  try {
    answer = await request('GET', `/api/tracks?key=${encodeURIComponent(key)}`);
  } catch (error) {
    // The marks keep their tracks; a poll, a save, or the next open asks again.
  }
  if (ask !== runtime.trackRequest || key !== state.key) {
    return;
  }
  if (answer) {
    const read = readTrackAnswer(answer);
    state.tracks = read.tracks;
    state.trackPending = read.pending;
    drawOverlay();
  }
  if (state.trackPending.length > 0) {
    runtime.trackTimer = window.setTimeout(loadTracks, TRACK_POLL_MS);
  }
}

/**
 * Name the video time the overlay draws for: the frame on screen when Chrome reported it,
 * otherwise the playhead (during a seek, and before the first frame).
 * @returns {number} The time in seconds.
 */
function overlayTime() {
  return runtime.presentedTime === null ? dom.video.currentTime : runtime.presentedTime;
}

/** Ask Chrome to call onPresentedFrame for the next frame it shows, once. */
function watchFrames() {
  if (!runtime.frameCallback) {
    runtime.frameCallback = dom.video.requestVideoFrameCallback(onPresentedFrame);
  }
}

/**
 * Redraw the overlay for every frame Chrome shows, so a tracked mark moves with its sponge
 * while the video plays, and keep watching. A paused video shows no new frames, so this
 * costs nothing then.
 * @param {DOMHighResTimeStamp} now The time the frame was shown, unused.
 * @param {{mediaTime: number}} metadata The frame's details; mediaTime is its video time.
 */
function onPresentedFrame(now, metadata) {
  runtime.frameCallback = 0;
  runtime.presentedTime = metadata.mediaTime;
  drawOverlay();
  watchFrames();
}

/** Forget the frame on screen when a seek starts, so the overlay follows the playhead until the next frame shows. */
function forgetPresentedFrame() {
  runtime.presentedTime = null;
}

/** Stop watching frames when the source goes away; the next loadedmetadata starts again. */
function stopWatchingFrames() {
  if (runtime.frameCallback) {
    dom.video.cancelVideoFrameCallback(runtime.frameCallback);
    runtime.frameCallback = 0;
  }
  runtime.presentedTime = null;
}

/**
 * Report the saved marks as last drawn, in viewport pixels. The end-to-end driver aims its
 * clicks on moving marks with this.
 * @returns {{time: number, marks: Array<{id: string, x: number, y: number, point: object,
 *   tracked: boolean}>}} The video time the marks were drawn for, and each mark's center.
 */
function drawnInViewport() {
  const bounds = dom.overlay.getBoundingClientRect();
  return {
    time: runtime.drawn.time,
    marks: runtime.drawn.marks.map((mark) => ({ ...mark, x: bounds.left + mark.x, y: bounds.top + mark.y })),
  };
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
 * @param {Array<number>} [dash] A dash pattern for both strokes; empty draws solid lines.
 */
function strokeOutlined(ctx, trace, color, width, dash = []) {
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.setLineDash(dash);
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
 * @returns {{x: number, y: number, w: number, h: number}} The tag rectangle in stage pixels.
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
  return { x: left, y: top, w: width, h: height };
}

/**
 * Draw a mark: a ring with four ticks for a point, a rectangle for a box.
 * @param {CanvasRenderingContext2D} ctx The overlay context.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture in stage pixels.
 * @param {{point: {x: number, y: number}, box: ?object}} mark The mark in frame fractions.
 * @param {{color: string, width: number, radius: number, dash: ?Array<number>}} style Stroke
 *   color, stroke width, ring radius, and an optional dash pattern (Later points are dashed).
 * @returns {{center: {x: number, y: number}, boxPx: ?object, radius: number}} The drawn shape in
 *   stage pixels, which drawTag needs to place the tag.
 */
function drawMark(ctx, rect, mark, style) {
  const center = toPixels(mark.point, rect);
  const dash = style.dash || [];
  if (mark.box) {
    const corner = toPixels({ x: mark.box.x, y: mark.box.y }, rect);
    const boxPx = { x: corner.x, y: corner.y, w: mark.box.w * rect.w, h: mark.box.h * rect.h };
    strokeOutlined(ctx, () => {
      ctx.beginPath();
      ctx.rect(boxPx.x, boxPx.y, boxPx.w, boxPx.h);
    }, style.color, style.width, dash);
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
  }, style.color, style.width, dash);
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

/**
 * Redraw the overlay for the frame on screen: quadrant highlight, grid, saved and Later
 * marks (along their tracks, or near their own time), the flash labels of recent picks,
 * and the live mark. The saved marks' centers are kept in runtime.drawn for hit testing.
 */
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
  const time = overlayTime();
  runtime.drawn = { time, marks: [] };
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
  // A tracked sighting rides along its track; one without a track covering this time
  // shows where it was clicked, near its own time.
  for (const saved of marksAt(state.rows, state.tracks, time, SAVED_MARK_WINDOW_SECONDS)) {
    let shape;
    let tag;
    if (saved.status === 'unlabeled') {
      shape = drawMark(ctx, rect, saved, { color: LATER_COLOR, width: 2, radius: 8, dash: LATER_DASH });
      tag = drawTag(ctx, rect, '?', shape, { fill: LATER_COLOR, ink: LATER_INK, outline: '#ffffff' });
    } else {
      shape = drawMark(ctx, rect, saved, { color: '#2a2a2a', width: 1.5, radius: 8 });
      tag = drawTag(ctx, rect, `${saved.code} saved`, shape, {
        fill: 'rgba(20, 20, 20, 0.82)', ink: '#ffffff', outline: 'rgba(255, 255, 255, 0.7)',
      });
    }
    // The tag and the box take clicks too, so a click on any part of a mark edits it.
    const areas = shape.boxPx ? [tag, shape.boxPx] : [tag];
    runtime.drawn.marks.push({ id: saved.id, x: shape.center.x, y: shape.center.y, point: saved.point, tracked: saved.tracked, areas });
  }
  drawFlashes(ctx, rect);

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
    // No tag: the wheel sits over the mark and names the species.
    drawMark(ctx, rect, state.pending, { color: MARK_COLOR, width: 2.5, radius: 11 });
  }
}

/**
 * Draw the flash labels of this video: a fading label at the mark for a moment after each
 * pick, and a red "not saved" tag for a save that failed, near its time, until Retry works.
 * @param {CanvasRenderingContext2D} ctx The overlay context.
 * @param {{x: number, y: number, w: number, h: number}} rect The picture in stage pixels.
 */
function drawFlashes(ctx, rect) {
  const now = performance.now();
  for (const flash of runtime.flashes) {
    const look = flashView(flash, now, dom.video.currentTime, SAVED_MARK_WINDOW_SECONDS, FLASH_FADE_MS);
    if (flash.key !== state.key || !look.draw) {
      continue;
    }
    let colors = flash.later
      ? { fill: LATER_COLOR, ink: LATER_INK, outline: '#ffffff' }
      : { fill: MARK_COLOR, ink: MARK_INK, outline: '#ffffff' };
    let text = flash.text;
    if (flash.failed) {
      colors = { fill: FAILED_COLOR, ink: FAILED_INK, outline: '#ffffff' };
      text = 'not saved';
    }
    ctx.save();
    ctx.globalAlpha = look.alpha;
    const shape = drawMark(ctx, rect, flash, {
      color: colors.fill, width: FLASH_RING.width, radius: FLASH_RING.radius, dash: flash.later ? LATER_DASH : [],
    });
    drawTag(ctx, rect, text, shape, colors);
    ctx.restore();
  }
}

/**
 * Add a flash label for a pick and keep the overlay redrawing while it fades. The flash
 * stays in the list while its save is unsettled, however long the server takes.
 * @param {string} key The S3 key of the video.
 * @param {number} time The video time of the pick in seconds.
 * @param {{point: object, box: ?object}} mark The mark in frame fractions.
 * @param {?string} code The species code, or null for a Later point.
 * @returns {{key: string, time: number, point: object, box: ?object, text: string, later: boolean,
 *   until: number, failed: boolean, pending: boolean}} The flash; the save settles it.
 */
function addFlash(key, time, mark, code) {
  const species = code ? speciesByCode(code) : null;
  const flash = {
    key,
    time,
    point: mark.point,
    box: mark.box,
    text: code ? `${code} ${shortLabel(species ? species.name : '', FLASH_NAME_CHARS)}`.trim() : 'later',
    later: !code,
    until: performance.now() + FLASH_MS,
    failed: false,
    pending: true,
  };
  runtime.flashes.push(flash);
  if (!runtime.flashFrame) {
    runtime.flashFrame = window.requestAnimationFrame(flashTick);
  }
  return flash;
}

/**
 * Redraw the overlay once per frame while a flash label fades, and drop the flashes that
 * faded with their save settled.
 */
function flashTick() {
  const now = performance.now();
  runtime.flashes = runtime.flashes.filter((flash) => (
    flashView(flash, now, dom.video.currentTime, SAVED_MARK_WINDOW_SECONDS, FLASH_FADE_MS).keep
  ));
  drawOverlay();
  const fading = runtime.flashes.some((flash) => flash.until > now);
  runtime.flashFrame = fading ? window.requestAnimationFrame(flashTick) : 0;
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
  dom.note.disabled = !(marking && state.pending && state.pending.mode === 'new');
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
    const where = `${QUADRANT_PHRASES[pending.quadrant]} · ${formatClock(pending.time)} · ${pending.box ? 'box' : 'point'}`;
    let action = 'pick a species in the wheel, or Space for later';
    if (pending.mode === 'edit' && pending.status === 'unlabeled') {
      action = `${pending.rowId} Later: pick a species, or Space to keep it for later`;
    } else if (pending.mode === 'edit') {
      action = `${pending.rowId} ${pending.code}: pick a new species, or Space to unlabel it`;
    }
    summary = `${where} · ${action}`;
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
 * Start a click or a drag on the picture. The video pauses at once, so the frame under
 * the pointer is the frame that gets saved. A press on a saved mark is noted against the
 * marks as drawn when the button went down, before the pause, so a mark that moves with
 * the video is hit where it was seen. An open wheel closes until the button comes up, and
 * a press on the black bars beside the picture drops the mark.
 * @param {MouseEvent} event The mousedown on the overlay.
 */
function onStageMouseDown(event) {
  if (event.button !== 0 || !isVideoReady()) {
    return;
  }
  const startPx = stagePoint(event);
  const start = toNormalized(startPx.x, startPx.y, pictureRectLocal());
  if (!start) {
    dropMark();
    return;
  }
  event.preventDefault();
  if (document.activeElement instanceof HTMLElement && document.activeElement !== document.body) {
    document.activeElement.blur();
  }
  const hit = state.pending ? null : rowNear(startPx);
  dom.video.pause();
  wheel.close();
  disarmPin();
  runtime.drag = { startPx, nowPx: startPx, start, hit, marking: Boolean(state.pending) };
  window.addEventListener('mousemove', onStageMouseMove);
  window.addEventListener('mouseup', onStageMouseUp);
}

/**
 * Cancel the pending mark and play on when the picture is right-clicked, as Escape does,
 * instead of opening the browser's menu. Without a mark the menu opens as usual.
 * @param {MouseEvent} event The contextmenu event on the overlay.
 */
function onStageContextMenu(event) {
  if (state.pending) {
    event.preventDefault();
    cancelMark();
  }
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
 * Finish a click or a drag: a drag past 6 px makes a box, anything else makes a point. A
 * click on a drawn saved mark, with no mark on screen, opens the wheel for that row
 * instead, whether the video was paused or playing.
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
  if (!box && drag.hit) {
    openEdit(drag.hit);
    return;
  }
  beginMark(box ? anchorPoint(drag.start, box) : drag.start, box);
}

/**
 * Find the row whose saved mark, as last drawn, sits nearest a stage position.
 * @param {{x: number, y: number}} px The position in stage pixels.
 * @returns {?Object<string, string>} The row whose tag or box holds the position, else the nearest
 *   row within EDIT_REACH_PX, or null.
 */
function rowNear(px) {
  const id = markAt(runtime.drawn.marks, px, EDIT_REACH_PX);
  return id === null ? null : state.rows.find((row) => row.ID === id) || null;
}

/** Drop a drag that lost its mouseup, which happens when the window loses focus mid-drag. */
function abandonDrag() {
  if (runtime.drag) {
    window.removeEventListener('mousemove', onStageMouseMove);
    window.removeEventListener('mouseup', onStageMouseUp);
    runtime.drag = null;
    dropMark();
    drawOverlay();
  }
}

/**
 * Start a new mark, or move the one on screen, and open the wheel on it.
 * @param {{x: number, y: number}} point The mark position in frame fractions (the box center for a box).
 * @param {?{x: number, y: number, w: number, h: number}} box The dragged box, or null for a click.
 */
function beginMark(point, box) {
  const anchor = anchorPoint(point, box);
  state.pending = {
    mode: 'new',
    rowId: null,
    code: null,
    status: null,
    key: state.key,
    time: dom.video.currentTime,
    point,
    box,
    quadrant: quadrantOf(anchor.x, anchor.y),
    picked: false,
    error: null,
  };
  setMode('marking');
  renderMarkBar();
  drawOverlay();
  openWheel();
}

/**
 * Open the wheel on a saved row of the open video, at its mark as drawn now, so a pick
 * changes its species. The video pauses.
 * @param {Object<string, string>} row An observation row of the open video.
 */
function openEdit(row) {
  const [mark] = marksNear([row], Number(row.TimestampSeconds), 0);
  if (!mark) {
    toast(`${row.ID} has no readable time or point, so the wheel cannot open on it.`, 'error');
    return;
  }
  dom.video.pause();
  // The wheel opens on the mark where it is drawn now, along its track when one covers
  // this frame; the quadrant stays the one the row records.
  const [drawn] = marksAt([row], state.tracks, overlayTime(), Infinity);
  const anchor = anchorPoint(mark.point, mark.box);
  state.pending = {
    mode: 'edit',
    rowId: row.ID,
    code: row.SpeciesCode || null,
    status: mark.status,
    key: state.key,
    time: dom.video.currentTime,
    point: drawn.point,
    box: drawn.box,
    quadrant: quadrantOf(anchor.x, anchor.y),
    picked: false,
    error: null,
  };
  setMode('marking');
  renderMarkBar();
  drawOverlay();
  openWheel();
}

/**
 * Name what the hub does for a pending mark.
 * @param {{mode: string, status: ?string}} pending The pending mark.
 * @returns {string} "Later" for a new mark, "Unlabel" for a labeled row, "Keep" for a Later point.
 */
function hubLabelOf(pending) {
  if (pending.mode === 'new') {
    return 'Later';
  }
  return pending.status === 'unlabeled' ? 'Keep' : 'Unlabel';
}

/**
 * Work out where the wheel goes for the pending mark: on the mark, scaled to the view,
 * and kept inside it. The view is the wheel layer, which covers the viewport, or the
 * screen in full screen.
 * @returns {?{center: {x: number, y: number}, scale: number, markPx: {x: number, y: number}}}
 *   The placement in viewport pixels, or null without a mark or a picture.
 */
function wheelPlacement() {
  const pending = state.pending;
  const picture = pictureRectInViewport();
  if (!pending || picture.w === 0) {
    return null;
  }
  const anchor = anchorPoint(pending.point, pending.box);
  const markPx = { x: picture.x + anchor.x * picture.w, y: picture.y + anchor.y * picture.h };
  const view = { w: dom.wheelLayer.clientWidth, h: dom.wheelLayer.clientHeight };
  const scale = wheelScale(view.w, view.h);
  const center = placeWheel(markPx, view, WHEEL.outerRadius * scale + WHEEL_MARGIN_PX);
  return { center: { x: center.x, y: center.y }, scale, markPx };
}

/** Open the wheel on the pending mark. */
function openWheel() {
  const pending = state.pending;
  const place = wheelPlacement();
  if (!pending || !place) {
    return;
  }
  wheel.open({
    mode: pending.mode,
    rowId: pending.rowId,
    hubLabel: hubLabelOf(pending),
    species: state.species,
    pins: state.pins,
    current: pending.mode === 'edit' ? pending.code : null,
    seen: seenCounts(state.rows),
    center: place.center,
    scale: place.scale,
    markPx: place.markPx,
  });
}

/** Put an open wheel back on its mark after the window, the stage, or full screen changed size. */
function relayoutWheel() {
  if (!wheel.isOpen()) {
    return;
  }
  const place = wheelPlacement();
  if (place) {
    wheel.relayout(place.center, place.scale, place.markPx);
  }
}

/** Forget the pending mark, close the wheel, and return to screening mode, without touching playback. */
function endMark() {
  state.pending = null;
  wheel.close();
  dom.note.value = '';
  clearFilter();
  if (document.activeElement === dom.filter || document.activeElement === dom.note) {
    document.activeElement.blur();
  }
  setMode('screening');
  renderMarkBar();
  drawOverlay();
}

/**
 * Cancel the pending mark and play on. Escape, a right-click on the wheel or on the picture,
 * and the Cancel button run this.
 */
function cancelMark() {
  if (!state.pending) {
    return;
  }
  endMark();
  resumePlayback();
}

/** Drop the pending mark and leave the video paused. A click outside the wheel and the picture runs this. */
function dropMark() {
  if (state.pending) {
    endMark();
  }
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
 * Choose a species for the pending mark: save a new mark, or relabel the edited row.
 * @param {string} code The species code.
 * @param {boolean} shift True when Shift was held. A plain pick leaves the video paused;
 *   Shift plays on.
 */
function pickSpecies(code, shift) {
  const stay = !shift;
  const pending = state.pending;
  if (!pending || pending.picked) {
    return;
  }
  showGuide(code, false);
  if (pending.mode === 'edit') {
    relabelPending(pending, code, stay);
    return;
  }
  saveNew(pending, code, stay);
}

/**
 * Choose the hub: save a new mark as a Later point, unlabel an edited labeled row, or
 * keep an edited Later point as it is.
 * @param {boolean} shift True when Shift was held. A plain choice leaves the video paused;
 *   Shift plays on.
 */
function pickHub(shift) {
  const stay = !shift;
  const pending = state.pending;
  if (!pending || pending.picked) {
    return;
  }
  if (pending.mode === 'new') {
    saveNew(pending, null, stay);
  } else if (pending.status === 'labeled') {
    relabelPending(pending, null, stay);
  } else {
    endMark();
    if (!stay) {
      resumePlayback();
    }
  }
}

/**
 * Copy the paused frame at the video's native size. The copy is synchronous, so playback
 * may resume right after it.
 * @returns {HTMLCanvasElement} A canvas that holds the frame.
 * @throws {Error} When the video has no frame to copy yet.
 */
function copyFrame() {
  const width = dom.video.videoWidth;
  const height = dom.video.videoHeight;
  if (!width || !height || dom.video.readyState < HAVE_CURRENT_DATA) {
    throw new Error('frame_jpeg: the video has not shown this frame yet. Wait a moment, then pick again.');
  }
  const frame = document.createElement('canvas');
  frame.width = width;
  frame.height = height;
  frame.getContext('2d').drawImage(dom.video, 0, 0, width, height);
  return frame;
}

/**
 * Encode a canvas as base64 text without blocking the page.
 * @param {HTMLCanvasElement} canvas The canvas.
 * @param {string} type "image/jpeg" or "image/png".
 * @param {string} field The request field the image goes to, for the error message.
 * @param {number} [quality] The JPEG quality from 0 to 1.
 * @returns {Promise<string>} The base64 text of the image.
 * @throws {Error} When the browser cannot encode the canvas or read the result.
 */
function canvasBase64(canvas, type, field, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error(`${field}: the browser could not encode the image, because the canvas has no pixels.`));
        return;
      }
      const reader = new FileReader();
      reader.addEventListener('load', () => resolve(stripDataUrl(reader.result)));
      reader.addEventListener('error', () => reject(new Error(`${field}: the browser could not read the encoded image.`)));
      reader.readAsDataURL(blob);
    }, type, quality);
  });
}

/**
 * Encode the frame as JPEG and cut the crop around the mark as PNG.
 * @param {HTMLCanvasElement} frame The copied frame at native size.
 * @param {{point: {x: number, y: number}, box: ?object}} mark The mark in frame fractions.
 * @returns {Promise<{frame: string, crop: string}>} Base64 text of the JPEG frame and the PNG crop.
 * @throws {Error} When the browser cannot encode either image.
 */
async function encodeImages(frame, mark) {
  const area = cropRect(mark.point, mark.box, frame.width, frame.height, CROP_SIZE);
  const crop = document.createElement('canvas');
  crop.width = area.sw;
  crop.height = area.sh;
  crop.getContext('2d').drawImage(frame, area.sx, area.sy, area.sw, area.sh, 0, 0, area.sw, area.sh);
  const [jpeg, png] = await Promise.all([
    canvasBase64(frame, 'image/jpeg', 'frame_jpeg', JPEG_QUALITY),
    canvasBase64(crop, 'image/png', 'crop_png'),
  ]);
  return { frame: jpeg, crop: png };
}

/**
 * Save a new mark the fast way: copy the paused frame, close the wheel, show the flash
 * label, and play on only when asked (Shift); then encode the images and post them in the
 * background.
 * @param {object} pending The pending mark.
 * @param {?string} code The species code, or null for a Later point.
 * @param {boolean} stay True keeps the video paused afterwards.
 * @returns {Promise<void>} Settles after the server stored or refused the sighting.
 */
async function saveNew(pending, code, stay) {
  pending.picked = true;
  runtime.pickCount += 1;
  const save = {
    key: state.key,
    order: runtime.pickCount,
    frame: null,
    mark: null,
    time: 0,
    species: code,
    note: '',
    body: null,
    flash: null,
    reason: '',
    sending: false,
  };
  // Z and leaving the page wait for this save from the moment of the pick. Until the save
  // goes to sendObservation, any fault releases it, so nothing waits for a save that never
  // goes out.
  trackSave(save);
  try {
    await waitForSeek();
    if (state.pending !== pending) {
      untrackSave(save);
      return;
    }
    try {
      save.frame = copyFrame();
    } catch (error) {
      untrackSave(save);
      pending.picked = false;
      pending.error = `Not saved. ${endSentence(error.message)}`;
      toast(pending.error, 'error');
      renderMarkBar();
      return;
    }
    save.mark = {
      point: { x: pending.point.x, y: pending.point.y },
      box: pending.box ? { x: pending.box.x, y: pending.box.y, w: pending.box.w, h: pending.box.h } : null,
    };
    save.time = Math.round(dom.video.currentTime * 1000) / 1000;
    save.note = dom.note.value;
    endMark();
    save.flash = addFlash(save.key, save.time, save.mark, code);
    if (!stay) {
      resumePlayback();
    }
  } catch (error) {
    untrackSave(save);
    if (save.flash) {
      save.flash.pending = false;
    }
    if (state.pending === pending) {
      pending.picked = false;
    }
    console.error(`saveNew: the pick was not saved: ${error.message}`);
    toast(`Not saved. ${endSentence(error.message)}`, 'error');
    return;
  }
  await sendObservation(save);
}

/**
 * Note a save the server has not answered yet, so Z and leaving the page wait for it.
 * @param {{key: string}} save The save.
 */
function trackSave(save) {
  runtime.inFlight.add(save);
}

/**
 * Forget a save that has its answer (or never started), and wake every wait whose video
 * has no save in flight any more.
 * @param {{key: string}} save The save.
 */
function untrackSave(save) {
  runtime.inFlight.delete(save);
  runtime.settleWaiters = runtime.settleWaiters.filter((waiter) => {
    if (savesInFlight(waiter.key) > 0) {
      return true;
    }
    waiter.resolve();
    return false;
  });
}

/**
 * Count the saves of a video that wait for the server's answer.
 * @param {?string} key The S3 key of the video.
 * @returns {number} The count.
 */
function savesInFlight(key) {
  return Array.from(runtime.inFlight).filter((save) => save.key === key).length;
}

/**
 * Wait until every save of a video has its answer, for at most a time limit.
 * @param {?string} key The S3 key of the video.
 * @param {number} limitMs The longest wait in milliseconds.
 * @returns {Promise<boolean>} True when the saves settled (at once when nothing of that
 *   video is in flight), false when the limit ran out first.
 */
function savesSettled(key, limitMs) {
  if (savesInFlight(key) === 0) {
    return Promise.resolve(true);
  }
  return new Promise((resolve) => {
    const waiter = {
      key,
      resolve: () => {
        window.clearTimeout(timer);
        resolve(true);
      },
    };
    const timer = window.setTimeout(() => {
      runtime.settleWaiters = runtime.settleWaiters.filter((item) => item !== waiter);
      resolve(false);
    }, limitMs);
    runtime.settleWaiters.push(waiter);
  });
}

/**
 * Encode the images of a save unless that happened before, and post the sighting.
 * @param {object} save The save that saveNew built.
 * @returns {Promise<{row: Object<string, string>, reopened: ?boolean}>} The server's answer.
 * @throws {Error} When an image cannot be encoded, the server refuses the row, or the
 *   answer holds no row.
 */
async function postSave(save) {
  if (!save.body) {
    const images = await encodeImages(save.frame, save.mark);
    save.body = {
      key: save.key,
      time: save.time,
      point: save.mark.point,
      box: save.mark.box,
      species: save.species,
      note: save.note,
      frame_jpeg: images.frame,
      crop_png: images.crop,
    };
    save.frame = null;
  }
  const answer = await request('POST', '/api/observations', save.body);
  if (!answer.row || typeof answer.row.ID !== 'string') {
    throw new ApiError('row: the server answered without the saved row. Check observations.csv before you mark this sponge again.', 0);
  }
  return answer;
}

/**
 * Post a save and report the result. Only the request and the answer check count as a
 * failure; a fault while the page shows a saved row goes to the console and is never
 * retried, because the row is already on the server. The Retry button of a failed save
 * runs this again with the same save.
 * @param {object} save The save that saveNew built.
 * @returns {Promise<void>} Settles after the server stored or refused the sighting.
 */
async function sendObservation(save) {
  if (save.sending) {
    return;
  }
  save.sending = true;
  trackSave(save);
  try {
    let answer;
    try {
      answer = await postSave(save);
    } catch (error) {
      onSaveFailed(save, error);
      return;
    }
    try {
      onSaved(save, answer);
    } catch (error) {
      console.error(`row: ${answer.row.ID} is saved, but the page could not show it: ${error.message}`);
    }
  } finally {
    save.sending = false;
    untrackSave(save);
  }
}

/**
 * Take a stored sighting into the lists, the counts, and the tally.
 * @param {object} save The save that was posted.
 * @param {{row: Object<string, string>, reopened: ?boolean}} answer The server's answer.
 */
function onSaved(save, answer) {
  const row = answer.row;
  save.flash.pending = false;
  if (save.flash.failed) {
    runtime.flashes = runtime.flashes.filter((flash) => flash !== save.flash);
  }
  runtime.unsaved = runtime.unsaved.filter((item) => item !== save);
  renderUnsaved();
  if (save.key === state.key && !state.rows.some((item) => item.ID === row.ID)) {
    state.rows.push(row);
    markFresh(row.ID);
  }
  if (save.key === state.key) {
    // The server queued the new sighting's track; ask until it arrives.
    loadTracks();
  }
  const later = labelStatus(row) === 'unlabeled';
  adjustVideoEntry(save.key, later ? 'unlabeled' : 'sightings', 1);
  renderSightings();
  renderBrowser();
  refreshTally();
  refreshToLabel();
  drawOverlay();
  toast(later ? `Saved ${row.ID} for later` : `Saved ${row.ID} ${row.SpeciesCode}`);
  applyReopened(answer, save.key);
}

/**
 * Report a failed save: its flash label turns into a red "not saved" tag, however late
 * the failure came, and the one error toast of unsaved picks counts it.
 * @param {object} save The save that failed. It keeps its images in memory.
 * @param {Error} error What went wrong.
 */
function onSaveFailed(save, error) {
  save.flash.failed = true;
  save.flash.pending = false;
  save.reason = endSentence(error.message);
  if (!runtime.unsaved.includes(save)) {
    runtime.unsaved.push(save);
  }
  renderUnsaved();
  if (save.species === null) {
    refreshVideoStatus(save.key);
  }
  drawOverlay();
}

/**
 * Show one sticky error toast for every pick whose save failed, with one Retry button that
 * sends them all again, or remove it when none is left.
 */
function renderUnsaved() {
  const saves = runtime.unsaved;
  if (saves.length === 0) {
    if (runtime.unsavedToast) {
      runtime.unsavedToast.remove();
      runtime.unsavedToast = null;
    }
    return;
  }
  const summary = unsavedSummary(saves.map((save) => {
    const species = save.species ? speciesByCode(save.species) : null;
    return { code: save.species, name: species ? species.name : '', time: save.time };
  }));
  const reason = saves[saves.length - 1].reason;
  const tail = `The page kept the images, so Retry sends ${saves.length === 1 ? 'it' : 'them'} again.`;
  const message = [summary, reason, tail].filter(Boolean).join(' ');
  if (runtime.unsavedToast && runtime.unsavedToast.isConnected) {
    runtime.unsavedToast.querySelector('.toast-text').textContent = message;
    state.lastToast = { message, kind: 'error' };
    return;
  }
  runtime.unsavedToast = toast(message, 'error', Infinity, {
    label: 'Retry',
    title: 'Send every unsaved sighting to the server again. The page still holds their frames and crops, so each one saves in full.',
    run: retryUnsaved,
  });
}

/** Send every unsaved pick to the server again. The Retry button of the error toast runs this. */
function retryUnsaved() {
  for (const save of runtime.unsaved.slice()) {
    sendObservation(save);
  }
}

/**
 * Change the species of the edited row, or clear it (code null). The wheel closes at once,
 * the video plays on only when asked (Shift), and the lists follow when the server answers.
 * @param {object} pending The pending edit.
 * @param {?string} code The new species code, or null to unlabel the row.
 * @param {boolean} stay True keeps the video paused afterwards.
 * @returns {Promise<void>} Settles after the server changed or refused the row.
 */
async function relabelPending(pending, code, stay) {
  pending.picked = true;
  const id = pending.rowId;
  endMark();
  if (!stay) {
    resumePlayback();
  }
  if (code !== null && code === pending.code && pending.status === 'labeled') {
    return;
  }
  try {
    const answer = await request('PATCH', `/api/observations/${encodeURIComponent(id)}`, { species: code });
    if (!answer.row || answer.row.ID !== id) {
      throw new ApiError(`row: the server answered without ${id}. Reload the page before you change it again.`, 0);
    }
    applyRelabel(answer.row, pending.status);
    toast(code ? `${id} is now ${code}` : `${id} waits in the To label list`);
    applyReopened(answer, pending.key);
  } catch (error) {
    toast(`${id} was not changed. ${endSentence(error.message)}`, 'error');
    if (code === null) {
      refreshVideoStatus(pending.key);
    }
  }
}

/**
 * Put a relabeled row in place of the old one in both lists, so the thumbnails use its
 * new file names, and update the counts.
 * @param {Object<string, string>} row The row the server answered with.
 * @param {string} before The label status of the row before the change.
 */
function applyRelabel(row, before) {
  const after = labelStatus(row);
  state.rows = state.rows.map((item) => (item.ID === row.ID ? row : item));
  const listed = state.toLabel.some((item) => item.ID === row.ID);
  if (after === 'unlabeled') {
    state.toLabel = listed ? state.toLabel.map((item) => (item.ID === row.ID ? row : item)) : state.toLabel.concat(row);
  } else {
    state.toLabel = state.toLabel.filter((item) => item.ID !== row.ID);
  }
  if (after !== before) {
    adjustVideoEntry(row.S3Key, 'sightings', after === 'labeled' ? 1 : -1);
    adjustVideoEntry(row.S3Key, 'unlabeled', after === 'unlabeled' ? 1 : -1);
  }
  renderSightings();
  renderToLabel();
  renderBrowser();
  refreshTally();
  refreshToLabel();
  drawOverlay();
}

/**
 * Ask the server for the status of a video again after a Later save or an unlabel failed,
 * because the server may have moved a done video back to in progress before the failure.
 * POST /api/videos/open answers with the status and changes nothing else for an open video.
 * @param {string} key The S3 key of the video.
 * @returns {Promise<void>} Settles after the Done button and the browser row show the answer.
 */
async function refreshVideoStatus(key) {
  let answer;
  try {
    answer = await request('POST', '/api/videos/open', { key });
  } catch (error) {
    // The error toast of the failed save already says the server did not answer; the
    // status on screen stays until the next open or save.
    return;
  }
  const status = answer.video && answer.video.Status === 'done' ? 'done' : 'in progress';
  const entry = videoEntry(key);
  if (entry) {
    entry.status = status;
  }
  if (key === state.key) {
    state.videoStatus = status;
  }
  renderDone();
  renderBrowser();
}

/**
 * Follow the server when a save or an unlabel returned a done video to in progress,
 * because a done video never holds Later points.
 * @param {{reopened: ?boolean}} answer The answer of POST or PATCH /api/observations. A
 *   missing field counts as false.
 * @param {string} key The S3 key of the video the row belongs to.
 */
function applyReopened(answer, key) {
  if (answer.reopened !== true) {
    return;
  }
  const entry = videoEntry(key);
  if (entry) {
    entry.status = 'in progress';
  }
  if (key === state.key) {
    state.videoStatus = 'in progress';
  }
  renderDone();
  renderBrowser();
  toast('This video is back in progress because it now has a point to label.', 'info');
}

// ------------------------------------------------------- sightings and tally

/**
 * Change a count of a video row after a save, a relabel, or a delete.
 * @param {string} key The S3 key of the video.
 * @param {string} field "sightings" for labeled rows, "unlabeled" for Later points.
 * @param {number} change 1 for a row added to the count, -1 for a row taken out.
 */
function adjustVideoEntry(key, field, change) {
  const entry = videoEntry(key);
  if (entry) {
    entry[field] = Math.max(0, (Number(entry[field]) || 0) + change);
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
  dom.sightingsCount.textContent = sightingsCount(rows);
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
 * Build the crop thumbnail of a row. A missing file leaves an empty square, never a broken image.
 * @param {Object<string, string>} row An observation row from the server.
 * @returns {HTMLImageElement} The thumbnail.
 */
function cropThumb(row) {
  const thumb = el('img', {
    class: 'sighting-thumb',
    attrs: {
      src: withRun(`/media/crops/${encodeURIComponent(row.CropFileName || '')}`, state.run),
      alt: '',
      width: '44',
      height: '44',
      loading: 'lazy',
    },
  });
  thumb.addEventListener('error', () => {
    thumb.dataset.missing = 'true';
  }, { once: true });
  return thumb;
}

/**
 * Build one sighting row: the crop, the species (or Later), the time, the note, and the
 * delete button.
 * @param {Object<string, string>} row An observation row from the server.
 * @returns {HTMLElement} The list item.
 */
function sightingNode(row) {
  const status = labelStatus(row);
  const species = speciesByCode(row.SpeciesCode);
  const name = species ? species.name : row['Sponge Type'] || row.SpeciesCode;
  const clock = row.Timestamp || formatClock(Number(row.TimestampSeconds));
  const title = status === 'unlabeled'
    ? `Jump to ${row.ID} at ${clock} and open the wheel on it to give it a species. ${row.Notes || ''}`
    : `Jump to ${row.ID} at ${clock}, marked ${name}; the video pauses there. ${row.Notes || ''}`;

  const jump = el('button', {
    class: 'sighting-jump',
    title: title.trim(),
    attrs: { type: 'button' },
    data: { id: row.ID },
  }, [
    cropThumb(row),
    el('span', { class: 'sighting-text' }, [
      el('span', { class: 'sighting-line' }, [
        el('span', { class: 'sighting-code', text: status === 'unlabeled' ? 'Later' : row.SpeciesCode }),
        el('span', { class: 'sighting-time', text: clock }),
      ]),
      el('span', { class: 'sighting-sub', text: `${row.ID} · ${row.Notes || ''}` }),
    ]),
  ]);

  const armed = runtime.armedDelete === row.ID;
  const remove = el('button', {
    class: 'button button-icon sighting-delete',
    title: armed
      ? `Confirm the removal. Sponge Screener deletes ${row.ID} from the table and moves its frame, crop and track to the trash folder in the data folder.`
      : `Remove ${row.ID}. The button asks once more first, then Sponge Screener moves its images and track to the trash folder.`,
    attrs: { type: 'button', 'aria-label': armed ? `Confirm removing ${row.ID}` : `Remove ${row.ID}` },
    data: { id: row.ID, armed: String(armed) },
  }, [armed ? 'Remove?' : icon('close')]);

  return el('li', {
    class: 'sighting',
    data: {
      id: row.ID,
      time: row.TimestampSeconds,
      code: row.SpeciesCode,
      status,
      fresh: String(runtime.freshId === row.ID),
      armed: String(armed),
    },
  }, [jump, remove]);
}

/**
 * React to a click in the sightings list: jump to a sighting, open the wheel on a Later
 * point, or arm and confirm a removal.
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
    if (row && labelStatus(row) === 'unlabeled') {
      jumpToRow(row);
    } else if (row && isVideoReady()) {
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
 * Remove one sighting on the server and from the lists. The server moves its images and
 * its track to the trash, and the page asks for the video's tracks again, because the next
 * sighting may receive the same ID.
 * @param {string} id The sighting ID, such as "ID007".
 * @param {string} [doneText] The toast after the removal; the default names the row as removed.
 * @returns {Promise<void>} Settles after the server removed or refused the row.
 */
async function deleteSighting(id, doneText = '') {
  const key = state.key;
  try {
    const answer = await request('DELETE', `/api/observations/${encodeURIComponent(id)}`);
    const gone = answer.deleted || {};
    if (key === state.key) {
      state.rows = state.rows.filter((row) => row.ID !== id);
      state.tracks = Object.fromEntries(Object.entries(state.tracks).filter(([trackId]) => trackId !== id));
      state.trackPending = state.trackPending.filter((pendingId) => pendingId !== id);
      loadTracks();
    }
    const later = labelStatus(gone) === 'unlabeled';
    adjustVideoEntry(key, later ? 'unlabeled' : 'sightings', -1);
    toast(doneText || (later ? `Removed ${id}, a point saved for later` : `Removed ${id} ${gone.SpeciesCode || ''}`.trim()));
  } catch (error) {
    toast(`${id} was not removed. ${endSentence(error.message)}`, 'error');
  }
  renderSightings();
  renderBrowser();
  refreshTally();
  refreshToLabel();
  drawOverlay();
}

/**
 * Undo the last pick of this video. The Z key runs this.
 *
 * A save still on its way to the server is the last pick, so Z first waits for this
 * video's saves, at most UNDO_WAIT_LIMIT_MS; when the limit runs out it says so and does
 * nothing. Then, when this video holds an unsaved (failed) pick, Z discards the most recent
 * one and changes nothing on the server. Only a video with no unsaved pick loses its last
 * saved sighting. A second Z while one is waiting or removing is ignored.
 * @returns {Promise<void>} Settles after the discard, the removal, the refusal, or the video change.
 */
async function undoLastSighting() {
  if (runtime.undoing) {
    return;
  }
  runtime.undoing = true;
  try {
    const key = state.key;
    if (savesInFlight(key) > 0) {
      const notice = window.setTimeout(() => toast('Waiting for the last save', 'info'), UNDO_NOTICE_MS);
      const settled = await savesSettled(key, UNDO_WAIT_LIMIT_MS);
      window.clearTimeout(notice);
      if (!settled) {
        toast('The last save has not answered yet. Z did nothing.', 'info');
        return;
      }
      if (key !== state.key) {
        return;
      }
    }
    const unsaved = latestUnsaved(runtime.unsaved, key);
    if (unsaved) {
      discardUnsaved(unsaved);
      return;
    }
    const last = lastSighting(state.rows);
    if (!last) {
      toast('This video has no sighting to remove.', 'info');
      return;
    }
    await deleteSighting(last.ID);
  } finally {
    runtime.undoing = false;
  }
}

/**
 * Drop an unsaved pick: it leaves the Retry list and its red tag leaves the picture. The
 * server never had it, so nothing there changes.
 * @param {object} save The unsaved pick.
 */
function discardUnsaved(save) {
  runtime.unsaved = runtime.unsaved.filter((item) => item !== save);
  runtime.flashes = runtime.flashes.filter((flash) => flash !== save.flash);
  renderUnsaved();
  drawOverlay();
  toast(discardedText(save.species, save.time), 'info');
}

/**
 * Ask before the page closes or reloads while a save is in flight or failed, because the
 * page holds those images and nothing else does.
 * @param {BeforeUnloadEvent} event The beforeunload event.
 */
function onBeforeUnload(event) {
  if (leaveBlocked(runtime.inFlight.size, runtime.unsaved.length)) {
    event.preventDefault();
    event.returnValue = '';
  }
}

/**
 * Load the tally across all videos.
 * @returns {Promise<void>} Settles after the table shows the tally or the error.
 */
async function refreshTally() {
  runtime.tallyToken += 1;
  const token = runtime.tallyToken;
  let answer = null;
  let problem = null;
  try {
    answer = await request('GET', '/api/tally');
  } catch (error) {
    problem = `The tally did not load. ${endSentence(error.message)}`;
  }
  if (token !== runtime.tallyToken) {
    return;
  }
  if (answer) {
    state.tally = Array.isArray(answer.tally) ? answer.tally : [];
    state.tallyTotal = Number(answer.total) || 0;
    state.tallyUnlabeled = Number(answer.unlabeled) || 0;
  }
  state.tallyError = problem;
  renderTally();
}

/** Draw the tally table: pinned species first, rows under the target highlighted. */
function renderTally() {
  dom.tallyTarget.textContent = `target ${state.tallyTarget}`;
  dom.tallyEmpty.hidden = !state.tallyError;
  dom.tallyEmpty.textContent = state.tallyError || '';
  const sightings = state.tallyTotal === 1 ? '1 sighting' : `${state.tallyTotal} sightings`;
  const points = state.tallyUnlabeled === 1 ? '1 point' : `${state.tallyUnlabeled} points`;
  dom.tallyTotal.textContent = `${sightings} across all videos. ${points} to label.`;
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

// ----------------------------------------------------------------- to label

/**
 * Load the Later points of every video. Only the answer to the newest request counts,
 * so quick saves in a row cannot leave an older list on screen.
 * @returns {Promise<void>} Settles after the list shows the points or the error.
 */
async function refreshToLabel() {
  runtime.toLabelToken += 1;
  const token = runtime.toLabelToken;
  let rows = null;
  let problem = null;
  try {
    const answer = await request('GET', '/api/observations?status=unlabeled');
    rows = Array.isArray(answer.rows) ? answer.rows : [];
  } catch (error) {
    problem = `The To label list did not load. ${endSentence(error.message)}`;
  }
  if (token !== runtime.toLabelToken) {
    return;
  }
  if (rows) {
    state.toLabel = rows;
  }
  state.toLabelError = problem;
  renderToLabel();
}

/** Draw the To label list, newest point first. */
function renderToLabel() {
  dom.toLabelCount.textContent = String(state.toLabel.length);
  dom.toLabel.replaceChildren(...state.toLabel.slice().reverse().map(toLabelNode));
  dom.toLabelEmpty.textContent = state.toLabelError || 'No points waiting for a label.';
  dom.toLabelEmpty.hidden = state.toLabel.length > 0 && !state.toLabelError;
}

/**
 * Build one To label row: the crop, the site and transect part of the file name, the time,
 * and the ID with the rest of the file name.
 * @param {Object<string, string>} row A Later row from the server.
 * @returns {HTMLElement} The list item.
 */
function toLabelNode(row) {
  const parts = splitVideoName(row.FileName || row.S3Key);
  const clock = row.Timestamp || formatClock(Number(row.TimestampSeconds));
  const jump = el('button', {
    class: 'sighting-jump',
    title: `Open ${row.FileName} at ${clock} with the wheel on ${row.ID}, ready for a species.`,
    attrs: { type: 'button' },
    data: { id: row.ID },
  }, [
    cropThumb(row),
    el('span', { class: 'sighting-text' }, [
      el('span', { class: 'sighting-line' }, [
        el('span', { class: 'to-label-name', text: parts.main }),
        el('span', { class: 'sighting-time', text: clock }),
      ]),
      el('span', { class: 'sighting-sub', text: parts.lead ? `${row.ID} · ${parts.lead}` : row.ID }),
    ]),
  ]);
  return el('li', {
    class: 'to-label-item',
    data: { id: row.ID, key: row.S3Key },
  }, [jump]);
}

/**
 * React to a click in the To label list: open that point in the wheel.
 * @param {MouseEvent} event The click.
 */
function onToLabelClick(event) {
  const item = event.target.closest('.to-label-item');
  const row = item ? state.toLabel.find((entry) => entry.ID === item.dataset.id) : null;
  if (row) {
    jumpToRow(row);
  }
}

/**
 * Build the entry that opens the video of a row: the catalog entry when the folder on
 * screen holds the video, otherwise one made from the row.
 * @param {Object<string, string>} row An observation row from the server.
 * @returns {{key: string, name: string, playable: boolean, converted: ?string, status: string}} The entry.
 */
function entryForRow(row) {
  const listed = videoEntry(row.S3Key);
  if (listed) {
    return listed;
  }
  const extension = String(row.FileName).split('.').pop().toLowerCase();
  return {
    key: row.S3Key,
    name: row.FileName,
    playable: PLAYABLE_EXTENSIONS.includes(extension),
    converted: null,
    status: 'in progress',
  };
}

/**
 * Go to a Later point and open the wheel on it: seek and pause when its video is open,
 * otherwise open that video, which lands on the point once it loads.
 * @param {Object<string, string>} row A Later row from the server.
 * @returns {Promise<void>} Settles after the wheel opened or the video started loading.
 */
async function jumpToRow(row) {
  const jump = { key: row.S3Key, time: Number(row.TimestampSeconds), row };
  if (row.S3Key !== state.key) {
    openVideo(entryForRow(row), jump);
    return;
  }
  if (!isVideoReady()) {
    runtime.jump = jump;
    return;
  }
  dropMark();
  dom.video.pause();
  seekTo(jump.time);
  await landJump(jump);
}

// ------------------------------------------------- settings, export, and help

/**
 * Send the annotator field to the server, once per value: a second call for the value
 * already on its way (the field's change event and a video click right after it) waits for
 * that save instead of sending another.
 * @returns {Promise<void>} Settles after the server stored or refused the value.
 */
function saveAnnotator() {
  const value = dom.annotator.value.trim();
  if (runtime.annotatorSave && runtime.annotatorSaveValue === value) {
    return runtime.annotatorSave;
  }
  const save = sendAnnotator().finally(() => {
    if (runtime.annotatorSave === save) {
      runtime.annotatorSave = null;
    }
  });
  runtime.annotatorSave = save;
  runtime.annotatorSaveValue = value;
  return save;
}

/**
 * Send the annotator field to the server and show the server's verdict.
 * @returns {Promise<void>} Settles after the server stored or refused the value.
 */
async function sendAnnotator() {
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
    const carried = state.annotator ? `, so sightings still carry ${state.annotator}` : '';
    toast(`The annotator was not saved${carried}. ${endSentence(error.message)}`, 'error');
  }
  if (state.annotator) {
    dismissToast(NO_INITIALS_TEXT);
  }
}

/**
 * Mark the empty annotator field and keep one message on screen until initials are saved.
 * The toast helper never replaces a sticky toast, so the old one is removed first.
 */
function askForInitials() {
  dom.annotator.setAttribute('aria-invalid', 'true');
  dismissToast(NO_INITIALS_TEXT);
  toast(NO_INITIALS_TEXT, 'error', Infinity);
}

/**
 * Remove a toast by its text, sticky or not.
 * @param {string} text The toast's message.
 */
function dismissToast(text) {
  for (const node of Array.from(dom.toasts.children)) {
    if (node.dataset.text === text) {
      node.remove();
    }
  }
}

/**
 * Show why the page cannot work and disable every control that would call the server, so
 * nothing invites a click that can only fail. The right panel stays empty, because nothing
 * was loaded into it.
 * @param {string} text The sentence for the player.
 */
function enterNoRun(text) {
  setStatus('error', text);
  document.body.dataset.run = 'none';
  for (const name of NO_RUN_DISABLED) {
    dom[name].disabled = true;
  }
}

/**
 * Start the annotator field on the signed-in initials: a valid ?initials= first, then what
 * the run remembered, never a default. A URL value that differs from the remembered one is
 * saved, so the sightings carry it. A field that stays empty is marked and explained.
 * @returns {Promise<void>} Settles after the server stored or refused the URL initials.
 */
async function seedInitials() {
  const fromUrl = initialsFromSearch(window.location.search);
  const start = startingInitials(fromUrl, state.annotator);
  if (start && start !== state.annotator) {
    dom.annotator.value = start;
    await saveAnnotator();
  }
  if (!state.annotator) {
    askForInitials();
  }
}

/**
 * Join the VICARIUS run the page was opened on. A standalone server keeps no runs, and the
 * page goes on as before. Under VICARIUS the page needs ?project_dir= and a run the server
 * accepts; without one it says how to open a run and loads nothing else.
 * @returns {Promise<boolean>} True when the page may load its data.
 */
async function joinRun() {
  let health;
  try {
    health = await request('GET', '/api/health');
  } catch (error) {
    enterNoRun(`The server did not answer. ${endSentence(error.message)}`);
    return false;
  }
  if (!health.runs) {
    return true;
  }
  const projectDir = projectDirFromSearch(window.location.search);
  if (!projectDir) {
    enterNoRun(NO_RUN_TEXT);
    return false;
  }
  try {
    const answer = await request('POST', '/api/project/open', { project_dir: projectDir });
    state.run = typeof answer.run === 'string' ? answer.run : '';
  } catch (error) {
    enterNoRun(`${endSentence(error.message)} ${NO_RUN_TEXT}`);
    return false;
  }
  if (!state.run) {
    enterNoRun(NO_RUN_TEXT);
    return false;
  }
  dom.runName.textContent = state.run;
  dom.runName.title = `Sponge Screener saves every sighting, image, track and export package in this VICARIUS run folder, inprocess/${state.run}.`;
  dom.runName.hidden = false;
  return true;
}

/**
 * Ask the server for the export package and show where it went.
 * @returns {Promise<void>} Settles after the server wrote or refused the package.
 */
async function exportPackage() {
  dom.exportButton.disabled = true;
  try {
    const answer = await request('POST', '/api/export', {});
    toast(exportSummary(answer), 'ok', LONG_TOAST_MS);
  } catch (error) {
    toast(`The export failed. ${endSentence(error.message)}`, 'error', LONG_TOAST_MS);
  } finally {
    dom.exportButton.disabled = false;
  }
}

/** The species guide: reference photos and the ID guide text, served from /guide/. */
const guide = { data: null, code: null, error: '', loading: null };

/**
 * Load guide.json once. A missing guide leaves a message instead of photos.
 * @returns {Promise<void>} Settles when the guide is loaded or the error is known.
 */
function loadGuide() {
  if (guide.data || guide.loading) {
    return guide.loading || Promise.resolve();
  }
  guide.loading = request('GET', '/guide/guide.json')
    .then((data) => {
      guide.data = Array.isArray(data.species) ? data.species : [];
      guide.error = '';
      fillGuideSelect();
    })
    .catch((error) => {
      guide.error = endSentence(error.message);
    })
    .then(() => {
      guide.loading = null;
      renderGuide();
    });
  return guide.loading;
}

/** Fill the species picker in the guide from the loaded manifest. */
function fillGuideSelect() {
  dom.guideSpecies.textContent = '';
  for (const item of guide.data) {
    const label = `${item.code}  ${item.name}${item.images.length ? '' : '  (no photos)'}`;
    dom.guideSpecies.append(el('option', { text: label, attrs: { value: item.code } }));
  }
}

/**
 * Show a species in the guide.
 * @param {string} code The species code.
 * @param {boolean} [open=true] Open the guide when it is closed.
 */
function showGuide(code, open = true) {
  guide.code = code;
  if (open) {
    toggleGuide(true);
  } else if (!dom.guide.hidden) {
    renderGuide();
  }
}

/**
 * Show the species the wheel lights in the guide, when the guide is open. The guide never
 * opens by itself; it follows the pointer or Tab only while it is already on screen.
 * @param {?string} code The lit species, or null when nothing is lit.
 */
function followGuide(code) {
  window.clearTimeout(runtime.guideFollowTimer);
  if (!code || dom.guide.hidden) {
    return;
  }
  runtime.guideFollowTimer = window.setTimeout(() => {
    if (!dom.guide.hidden) {
      showGuide(code, false);
    }
  }, GUIDE_FOLLOW_MS);
}

/**
 * Open or close the guide.
 * @param {boolean} [open] The wanted state; omitted means toggle.
 */
function toggleGuide(open) {
  const show = open === undefined ? dom.guide.hidden : open;
  dom.guide.hidden = !show;
  dom.guideToggle.setAttribute('aria-expanded', show ? 'true' : 'false');
  if (show) {
    if (!guide.code) {
      guide.code = state.pins[PIN_KEYS[0]] || (state.species[0] && state.species[0].code) || null;
    }
    loadGuide();
    renderGuide();
  }
}

/** Draw the guide for the chosen species. */
function renderGuide() {
  if (dom.guide.hidden) {
    return;
  }
  dom.guideImages.textContent = '';
  if (guide.error) {
    dom.guideText.textContent = '';
    dom.guideCount.textContent = '';
    dom.guideImages.append(el('p', { class: 'guide-empty', text: `The guide did not load. ${guide.error} Run tools/fetch_guide_images.py, then reopen the guide.` }));
    return;
  }
  if (!guide.data) {
    dom.guideText.textContent = 'Loading the guide.';
    return;
  }
  const item = guide.data.find((entry) => entry.code === guide.code) || guide.data[0];
  if (!item) {
    dom.guideText.textContent = 'The guide holds no species.';
    return;
  }
  guide.code = item.code;
  dom.guideSpecies.value = item.code;
  dom.guideText.textContent = item.description || 'No description in the ID guide yet.';
  dom.guideCount.textContent = item.images.length ? `${item.images.length} photos` : 'no photos';
  if (!item.images.length) {
    dom.guideImages.append(el('p', { class: 'guide-empty', text: `No reference photos for ${item.name} in the species viewer.` }));
    return;
  }
  for (const file of item.images) {
    const src = `/guide/${encodeURIComponent(item.code)}/${encodeURIComponent(file)}`;
    const img = el('img', { attrs: { src, alt: item.name, loading: 'lazy' } });
    // A div, not a button: Chrome gives a button no height from an image
    // child inside a grid, so the tiles would stack over each other.
    const tile = el('div', {
      class: 'guide-image',
      attrs: { role: 'button', tabindex: '0' },
      title: `Enlarge the photo of ${item.name}. Click the large photo or press Escape to shrink it again.`,
    }, [img]);
    tile.addEventListener('click', () => enlargeGuideImage(src, item.name));
    tile.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        enlargeGuideImage(src, item.name);
      }
    });
    dom.guideImages.append(tile);
  }
}

/**
 * Show one guide photo at full size over the page.
 * @param {string} src The photo URL.
 * @param {string} name The species name, used as the alt text.
 */
function enlargeGuideImage(src, name) {
  closeGuideLarge();
  const large = el('div', { class: 'guide-large', attrs: { id: 'guide-large', role: 'dialog', 'aria-label': name } }, [
    el('img', { attrs: { src, alt: name } }),
  ]);
  large.addEventListener('click', closeGuideLarge);
  dom.theater.append(large);
}

/**
 * Remove the enlarged guide photo, if one is showing.
 * @returns {boolean} True when a photo was showing and is gone now.
 */
function closeGuideLarge() {
  const large = document.getElementById('guide-large');
  if (large) {
    large.remove();
    return true;
  }
  return false;
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

// ---------------------------------------------------------------- full screen

/**
 * Fill the screen with the theater (the video, the marks, the wheel, and the transport
 * row), or leave full screen.
 */
function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {
      // Chrome refuses only when nothing is in full screen, which is the wanted state.
    });
    return;
  }
  dom.theater.requestFullscreen().catch((error) => {
    toast(`Full screen did not start. ${endSentence(error.message)}`, 'error');
  });
}

/**
 * Follow the full-screen state: the button, the Escape key lock, the overlay, and an open
 * wheel, which moves back onto its mark inside the new view.
 */
function onFullscreenChange() {
  const on = document.fullscreenElement === dom.theater;
  state.fullscreen = on;
  document.body.dataset.fullscreen = String(on);
  dom.fullscreenIcon.setAttribute('href', on ? '#i-collapse' : '#i-expand');
  dom.fullscreen.setAttribute('aria-label', on ? 'Leave full screen' : 'Full screen');
  const keyboard = navigator.keyboard;
  if (keyboard && typeof keyboard.lock === 'function') {
    if (on) {
      // With Escape locked, Escape reaches the page and cancels the wheel; a long press
      // still leaves full screen.
      keyboard.lock(['Escape']).catch(() => {
        // Without the lock, Escape leaves full screen at once, which still works.
      });
    } else {
      keyboard.unlock();
    }
  }
  drawOverlay();
  relayoutWheel();
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
  if (document.body.dataset.run === 'none' && !NO_RUN_KEYS.has(key)) {
    // The buttons are disabled without a run, so their shortcuts are too.
    return;
  }
  if (key !== 'Backspace' && key !== 'Delete') {
    // Any other key disarms a deletion that waits for its second press.
    wheel.disarmDelete();
  }
  if (key === 'Escape' && closeGuideLarge()) {
    event.preventDefault();
    return;
  }
  if (key === 'Escape' && !dom.guide.hidden) {
    toggleGuide(false);
    event.preventDefault();
    return;
  }
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
    const slot = pinKeyFromEvent(key, event.code, event.shiftKey);
    if (marking && slot) {
      if (!event.repeat) {
        choosePinned(slot, event.shiftKey);
      }
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
 * Handle Enter inside a text field. In the filter box it chooses the highlighted match;
 * in the note box it returns the keyboard to the wheel and never saves.
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
    } else if (marking && !repeat) {
      pickSpecies(code, shift);
    } else if (!marking) {
      armPin(code);
    }
    return;
  }
  if (field === dom.search) {
    openFirstSearchResult();
    return;
  }
  field.blur();
}

/**
 * Handle Escape inside a text field: in marking mode the note box returns the keyboard to
 * the wheel and the filter box cancels the mark; otherwise the field empties or resets and
 * lets go of the keyboard.
 * @param {HTMLInputElement} field The field that holds the keyboard.
 */
function onTextFieldEscape(field) {
  if (state.mode === 'marking') {
    if (field === dom.note) {
      field.blur();
    } else {
      cancelMark();
    }
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
 * Handle a key while the wheel is open, outside the text fields.
 * @param {KeyboardEvent} event The keydown.
 * @param {string} key The key value from keyFromEvent.
 * @returns {boolean} True when the key was used, so the browser default is blocked.
 */
function onMarkingKey(event, key) {
  const slot = pinKeyFromEvent(key, event.code, event.shiftKey);
  if (slot) {
    if (!event.repeat) {
      choosePinned(slot, event.shiftKey);
    }
    return true;
  }
  const open = wheel.snapshot();
  const query = open ? open.query : '';
  if (key === 'Enter') {
    if (!event.repeat) {
      wheel.pickHighlighted(event.shiftKey);
    }
    return true;
  }
  if (key === ' ') {
    if (spaceAction(query) === 'query') {
      wheel.setQuery(`${query} `);
    } else if (!event.repeat) {
      pickHub(event.shiftKey);
    }
    return true;
  }
  if (key === 'Escape') {
    cancelMark();
    return true;
  }
  if (key === 'Tab') {
    // Tab and Shift+Tab walk the highlight around the wheel; an open guide follows it.
    wheel.moveHighlight(event.shiftKey ? -1 : 1);
    return true;
  }
  if (key === ',' || key === '.' || key === 'ArrowLeft' || key === 'ArrowRight') {
    stepFrame(key === '.' || key === 'ArrowRight' ? 1 : -1);
    return true;
  }
  if (key === 'ArrowUp' || key === 'ArrowDown') {
    const step = key === 'ArrowDown' ? 1 : -1;
    if (query.trim() !== '') {
      wheel.moveHighlight(step);
    } else {
      wheel.spin(step);
    }
    return true;
  }
  if (key === 'Backspace' || key === 'Delete') {
    onDeleteKey(open ? open.mode : 'new', query, event.repeat);
    return true;
  }
  if (key === '?') {
    toggleHelp();
    return true;
  }
  if (/^[a-z]$/i.test(key)) {
    wheel.setQuery(query + key.toLowerCase());
    return true;
  }
  return false;
}

/**
 * Handle Backspace or Delete in the wheel. A typed query loses its last letter. In the wheel
 * of a saved mark without a query, the first press arms deleting the row and a second press
 * within DELETE_ARM_MS deletes it; a held key's repeats neither arm nor delete, a press within
 * QUERY_EDIT_GUARD_MS of clearing the query does not arm, and a press within
 * DELETE_CONFIRM_MIN_MS of arming does not delete.
 * @param {string} mode The wheel's mode, "new" or "edit".
 * @param {string} query The typed query.
 * @param {boolean} repeat True when the key repeats because it is held down.
 */
function onDeleteKey(mode, query, repeat) {
  const action = deleteKeyAction(mode, query);
  const now = performance.now();
  if (action === 'query') {
    wheel.setQuery(query.slice(0, -1));
    runtime.queryEditedAt = now;
  } else if (action === 'arm' && !repeat) {
    // Taps that just cleared a filter cannot arm, and a second tap right after arming cannot
    // delete, so a burst of Backspace never removes a sighting.
    const decision = deleteKeyTiming(now, runtime.queryEditedAt, wheel.deleteArmed() ? runtime.deleteArmedAt : null);
    if (decision === 'delete') {
      deleteFromWheel();
    } else if (decision === 'arm') {
      wheel.armDelete(DELETE_ARM_MS);
      runtime.deleteArmedAt = now;
    }
  }
}

/**
 * Delete the row of the open edit wheel, on the second press of Backspace or Delete. The
 * wheel closes, the video stays paused, and the server moves the row's images and its track
 * to the trash.
 * @returns {Promise<void>} Settles after the server removed or refused the row.
 */
async function deleteFromWheel() {
  const pending = state.pending;
  if (!pending || pending.mode !== 'edit' || pending.picked) {
    return;
  }
  pending.picked = true;
  const label = rowLabel(pending.rowId, pending.code);
  endMark();
  await deleteSighting(pending.rowId, `Deleted ${label}`);
}

/**
 * End a held arrow key's rewind or fast-forward when the key comes up.
 * @param {KeyboardEvent} event The keyup.
 */
function onKeyUp(event) {
  if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && scrubber.active()) {
    scrubber.stop();
    event.preventDefault();
  }
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
    // Holding the key rewinds or fast-forwards; onKeyUp ends it. Key repeats are ignored.
    if (!event.repeat && isVideoReady()) {
      scrubber.start(key === 'ArrowRight' ? 1 : -1);
    }
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
  if (pinKeyFromEvent(key, event.code, event.shiftKey)) {
    if (state.key && !event.repeat) {
      toast('Click a sponge in the video first. Then press its species key.', 'info');
    }
    return true;
  }
  const letter = key.length === 1 ? key.toLowerCase() : '';
  if (event.repeat) {
    return ['g', 'z', 'd', 'n', 'i', 'f', 'p'].includes(letter);
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
  if (letter === 'i') {
    toggleGuide();
    return true;
  }
  if (letter === 'f') {
    toggleFullscreen();
    return true;
  }
  if (letter === 'p') {
    setDeck(!state.deckShown);
    return true;
  }
  return false;
}

// -------------------------------------------------------------------- startup

/**
 * Drop the mark when a mousedown lands outside the wheel, the picture, and the controls
 * that work with an open mark. The video stays paused.
 * @param {MouseEvent} event The mousedown, caught on its way down to its target.
 */
function onMouseDownOutside(event) {
  if (state.mode === 'marking' && event.target instanceof Element && !event.target.closest(MARKING_ZONES)) {
    dropMark();
  }
}

/** Attach every event listener of the page. */
function wireEvents() {
  document.addEventListener('keydown', onKeyDown);
  document.addEventListener('keyup', onKeyUp);
  window.addEventListener('blur', () => scrubber.stop());
  document.addEventListener('mousedown', onMouseDownOutside, true);
  document.addEventListener('fullscreenchange', onFullscreenChange);
  window.addEventListener('resize', relayoutWheel);
  document.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button && event.detail > 0) {
      button.blur();
    }
  });
  window.addEventListener('blur', abandonDrag);
  window.addEventListener('pagehide', savePosition);
  window.addEventListener('beforeunload', onBeforeUnload);

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
  dom.overlay.addEventListener('contextmenu', onStageContextMenu);
  dom.stageAction.addEventListener('click', () => {
    if (runtime.statusAction) {
      runtime.statusAction.run();
    }
  });
  new ResizeObserver(() => {
    drawOverlay();
    relayoutWheel();
  }).observe(dom.stage);

  dom.video.addEventListener('loadedmetadata', onLoadedMetadata);
  dom.video.addEventListener('loadedmetadata', watchFrames);
  dom.video.addEventListener('emptied', stopWatchingFrames);
  dom.video.addEventListener('seeking', forgetPresentedFrame);
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
  dom.fullscreen.addEventListener('click', toggleFullscreen);
  dom.deckToggle.addEventListener('click', () => setDeck(!state.deckShown));

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

  dom.cancel.addEventListener('click', cancelMark);

  dom.sightings.addEventListener('click', onSightingsClick);
  dom.toLabel.addEventListener('click', onToLabelClick);
  dom.annotator.addEventListener('change', saveAnnotator);
  dom.annotator.addEventListener('input', () => {
    upperCaseAsTyped(dom.annotator);
    dom.annotator.removeAttribute('aria-invalid');
  });
  dom.exportButton.addEventListener('click', exportPackage);
  dom.helpToggle.addEventListener('click', () => toggleHelp());
  dom.helpClose.addEventListener('click', () => toggleHelp(false));
  dom.guideToggle.addEventListener('click', () => toggleGuide());
  dom.guideClose.addEventListener('click', () => toggleGuide(false));
  dom.guideDock.addEventListener('click', () => setGuideDock(!state.guideDocked));
  dom.guideSpecies.addEventListener('change', () => showGuide(dom.guideSpecies.value));
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
  setSpeed(SPEEDS.includes(speed) ? speed : DEFAULT_SPEED);
  setDeck(storageGet(STORAGE.deck) === '1');
  setGuideDock(storageGet(STORAGE.guideDock) !== '0');
}

/**
 * Start the page: wire the events, join the VICARIUS run when the server keeps runs, then
 * load the species (and start the annotator on the signed-in initials), the tally, the To
 * label list, and the folder that was open last time.
 * @returns {Promise<void>} Settles after the first requests returned, or at once when the
 *   page has no run to work in.
 */
async function start() {
  wireEvents();
  restorePreferences();
  setMode('screening');
  setStatus('idle', IDLE_TEXT);
  renderPins();
  renderMarkBar();
  renderSightings();
  renderToLabel();
  renderDone();
  renderTransport();

  if (!(await joinRun())) {
    return;
  }
  const remembered = storageGet(STORAGE.prefix);
  const first = remembered && remembered.startsWith(ROOT_PREFIX) ? remembered : ROOT_PREFIX;
  await Promise.all([
    loadSpecies().then(() => (state.speciesError ? undefined : seedInitials())),
    refreshTally(),
    refreshToLabel(),
    loadCatalog(first).then(() => (
      state.catalogError && first !== ROOT_PREFIX ? loadCatalog(ROOT_PREFIX) : undefined
    )),
  ]);
}

const scrubber = createScrubber(dom.video);

const wheel = createWheel(dom.wheelLayer, {
  onPick: (code, choice) => pickSpecies(code, choice.shift),
  onHubPick: (choice) => pickHub(choice.shift),
  onCancel: cancelMark,
  onHighlight: followGuide,
});

window.__screener = { state, pictureRect: pictureRectInViewport, wheel: () => wheel.snapshot(), drawn: drawnInViewport };
start();
