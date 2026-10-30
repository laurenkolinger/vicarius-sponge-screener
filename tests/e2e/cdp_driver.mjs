/**
 * End-to-end driver for Sponge Screener: one scenario per run, in a headless Chrome that
 * tests/test_e2e.py started. The driver speaks the Chrome DevTools Protocol over a
 * WebSocket, sends real mouse and key events, reads the page through window.__screener,
 * and prints one JSON line with what it saw. It asserts nothing about the app. The pytest
 * module reads the line and reads observations.csv and the image folders itself.
 *
 * Usage:
 *   node --experimental-websocket cdp_driver.mjs <browser ws url> <app url> <scenario> <out dir> <config json>
 *
 * The config JSON holds root, folder2024, folder2023, key2024, key2023, keyPan (a clip whose
 * picture pans left at a known speed), and pins ({"1": "ACAU", ...}). The last stdout line is
 * the result:
 *   {scenario, ok, step, error, facts, shots, pageErrors, consoleErrors, elapsedMs}
 */

import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';

// ------------------------------------------------------------------ constants

const [WS_URL, APP_URL, SCENARIO, OUT_DIR, CONFIG_JSON] = process.argv.slice(2);
const SIZE_PRESETS = Object.freeze({ wide: [1280, 800], narrow: [946, 700], small: [700, 520] });
const HELD_SAVE_MS = 1500;
const SLOW_SAVE_MS = 12000;
const SLOW_SAVE_WAIT_MS = 20000;
const NOTICE_LOOK_MS = 700;
const STILL_MS = 400;
const POLL_MS = 50;
const DEFAULT_WAIT_MS = 30000;
const VIDEO_WAIT_MS = 90000;
const WHEEL_SETTLE_MS = 200;
const FLASH_LOOK_MS = 150;
const LAYOUT_SETTLE_MS = 500;
const STABLE_RECT_MS = 4000;
const SCENARIO_LIMIT_MS = 120000;
const GATE_TIMEOUT_MS = 5000;
const SHIFT = 8;
const BUTTON_BITS = Object.freeze({ left: 1, right: 2 });
const START_SECONDS = 1;
const REWIND_AFTER_SECONDS = 7;
const SHIFTED_DIGITS = Object.freeze({ 1: '!', 2: '@', 3: '#', 4: '$', 5: '%', 6: '^', 7: '&', 8: '*', 9: '(', 0: ')' });
const NAMED_KEYS = Object.freeze({
  Enter: { key: 'Enter', code: 'Enter', vk: 13, text: '\r' },
  Escape: { key: 'Escape', code: 'Escape', vk: 27 },
  Space: { key: ' ', code: 'Space', vk: 32, text: ' ' },
  Backspace: { key: 'Backspace', code: 'Backspace', vk: 8 },
  Tab: { key: 'Tab', code: 'Tab', vk: 9 },
  ArrowLeft: { key: 'ArrowLeft', code: 'ArrowLeft', vk: 37 },
  ArrowRight: { key: 'ArrowRight', code: 'ArrowRight', vk: 39 },
  '.': { key: '.', code: 'Period', vk: 190, text: '.' },
  ',': { key: ',', code: 'Comma', vk: 188, text: ',' },
  '[': { key: '[', code: 'BracketLeft', vk: 219, text: '[' },
  ']': { key: ']', code: 'BracketRight', vk: 221, text: ']' },
});
const S = 'window.__screener';
/** Gap between the repeats a held key sends, close to a real keyboard's repeat rate. */
const KEY_REPEAT_MS = 33;
/** How long a closed guide is watched to see that hovering the wheel does not open it. */
const GUIDE_STAYS_CLOSED_MS = 400;
/** How long the arrow-scrub scenario holds each arrow. */
const SCRUB_HOLD_MS = 1000;
/** The pan scenario starts here, picks the middle of the picture once the clip passes PAN_PICK_SECONDS, and reads the mark PAN_LATER_SECONDS later. */
const PAN_START_SECONDS = 3.5;
const PAN_PICK_SECONDS = 5;
const PAN_LATER_SECONDS = 2;
/** The delete scenario picks at 3 s of the pan clip, then replays from 3.5 s to click the moving mark. */
const DELETE_START_SECONDS = 2.5;
const DELETE_PICK_SECONDS = 3;
const DELETE_REPLAY_SECONDS = 3.5;
const DELETE_CLICK_SECONDS = 4;
/** One press of Backspace disarms itself after 3 s; the scenario waits a little longer. */
const DELETE_ARM_WAIT_MS = 3300;
/** The deleting press must come at least 300 ms after the arming press; the scenarios wait a little longer. */
const DELETE_CONFIRM_WAIT_MS = 400;
/** Gap between the quick Backspace taps of the burst scenario, like fast typing. */
const BURST_GAP_MS = 50;
/** Time a burst scenario waits for any deletion it should not have caused. */
const BURST_SETTLE_MS = 600;
const V = "document.getElementById('video')";

/**
 * Installed before the page's own scripts. It records every non-GET answer from /api/ (the
 * driver reads them to see the `reopened` flag and the timing of overlapping saves), records
 * every keydown the page receives (the driver reads them to see what a Shift+digit sends),
 * and can hold saves at the door until a set number of them are waiting, then let them all
 * go at the same moment, so two saves are on the server together. It can also hold the next
 * save for a while (a slow server) or fail the next saves before they reach the server (a
 * lost connection), and it records every toast the page shows.
 */
const PAGE_HELPERS = `(() => {
  const log = [];
  const keys = [];
  const toasts = [];
  const gate = { want: 0, waiting: [], delayMs: 0, fail: 0 };
  window.addEventListener('keydown', (event) => {
    keys.push({ key: event.key, code: event.code, shiftKey: event.shiftKey });
  }, true);
  new MutationObserver((changes) => {
    for (const change of changes) {
      for (const node of change.addedNodes) {
        if (node.classList && node.classList.contains('toast')) {
          toasts.push({ kind: node.dataset.kind, text: node.dataset.text });
        }
      }
    }
  }).observe(document, { childList: true, subtree: true });
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    const method = String((init && init.method) || 'GET').toUpperCase();
    const watched = method !== 'GET' && url.startsWith('/api/');
    const save = watched && method === 'POST' && url === '/api/observations';
    if (save && gate.fail > 0) {
      gate.fail -= 1;
      log.push({ method, url, status: 0, startedAt: performance.now(), endedAt: performance.now(), body: null });
      throw new TypeError('Failed to fetch');
    }
    if (save && gate.delayMs > 0) {
      const wait = gate.delayMs;
      gate.delayMs = 0;
      await new Promise((resume) => setTimeout(resume, wait));
    }
    if (save && gate.want > 0) {
      await new Promise((release) => {
        gate.waiting.push(release);
        if (gate.waiting.length >= gate.want) {
          gate.want = 0;
          gate.waiting.splice(0).forEach((open) => open());
        } else {
          setTimeout(release, ${GATE_TIMEOUT_MS});
        }
      });
    }
    const startedAt = performance.now();
    const response = await original(input, init);
    if (watched) {
      const endedAt = performance.now();
      response.clone().json().then(
        (body) => log.push({ method, url, status: response.status, startedAt, endedAt, body }),
        () => log.push({ method, url, status: response.status, startedAt, endedAt, body: null }),
      );
    }
    return response;
  };
  window.__e2e = {
    log,
    keys,
    toasts,
    holdPosts(count) { gate.want = count; },
    delayNextPost(ms) { gate.delayMs = ms; },
    failPosts(count) { gate.fail = count; },
    leaveBlocked() {
      const event = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    },
  };
})();`;

// ------------------------------------------------------------------------ CDP

/** A minimal Chrome DevTools Protocol client over the WebSocket of the browser endpoint. */
class Cdp {
  /**
   * @param {WebSocket} socket An open WebSocket to the browser endpoint.
   */
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.waiting = new Map();
    this.listeners = [];
    socket.addEventListener('message', (message) => this.onMessage(JSON.parse(message.data)));
    socket.addEventListener('close', () => {
      for (const { reject } of this.waiting.values()) {
        reject(new Error('the DevTools connection closed while a command was waiting'));
      }
      this.waiting.clear();
    });
  }

  /**
   * Open the connection.
   * @param {string} url The ws:// address Chrome printed.
   * @returns {Promise<Cdp>} The connected client.
   * @throws {Error} When the socket cannot open.
   */
  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true });
      socket.addEventListener('error', () => reject(new Error(`cannot open the DevTools socket ${url}`)), { once: true });
    });
    return new Cdp(socket);
  }

  /**
   * Route an incoming message to the command that waits for it, or to the listeners.
   * @param {object} data The parsed message.
   */
  onMessage(data) {
    if (data.id !== undefined && this.waiting.has(data.id)) {
      const { resolve, reject, method } = this.waiting.get(data.id);
      this.waiting.delete(data.id);
      if (data.error) {
        reject(new Error(`${method} failed: ${JSON.stringify(data.error)}`));
      } else {
        resolve(data.result);
      }
    } else if (data.method) {
      for (const listener of this.listeners) {
        listener(data);
      }
    }
  }

  /**
   * Send one command.
   * @param {string} method The protocol method, such as Runtime.evaluate.
   * @param {object} [params] The parameters.
   * @param {string} [sessionId] The target session, for page commands.
   * @returns {Promise<object>} The command result.
   * @throws {Error} When Chrome answers with an error.
   */
  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    this.socket.send(JSON.stringify({ id, method, params, sessionId }));
    return new Promise((resolve, reject) => this.waiting.set(id, { resolve, reject, method }));
  }

  /**
   * Listen to every protocol event.
   * @param {Function} listener Called with `{method, params, sessionId}`.
   */
  onEvent(listener) {
    this.listeners.push(listener);
  }
}

// -------------------------------------------------------------------- session

/** One page of the headless Chrome, driven with real input events. */
class Session {
  /**
   * @param {Cdp} cdp The connected client.
   * @param {string} targetId The page target.
   * @param {string} sessionId The attached session.
   */
  constructor(cdp, targetId, sessionId) {
    this.cdp = cdp;
    this.targetId = targetId;
    this.sessionId = sessionId;
    this.facts = {};
    this.shots = [];
    this.pageErrors = [];
    this.consoleErrors = [];
    this.currentStep = 'start';
    cdp.onEvent((event) => this.onEvent(event));
  }

  /**
   * Create a page, attach to it, and switch on the protocol domains the driver needs.
   * @param {Cdp} cdp The connected client.
   * @returns {Promise<Session>} The ready session.
   */
  static async open(cdp) {
    const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    const session = new Session(cdp, targetId, sessionId);
    await session.call('Page.enable');
    await session.call('Runtime.enable');
    await session.call('Log.enable');
    await session.call('Page.bringToFront');
    await session.call('Emulation.setFocusEmulationEnabled', { enabled: true });
    await session.call('Page.addScriptToEvaluateOnNewDocument', { source: PAGE_HELPERS });
    return session;
  }

  /**
   * Record page exceptions and error-level console messages of this page.
   * @param {{method: string, params: object, sessionId: ?string}} event A protocol event.
   */
  onEvent(event) {
    if (event.sessionId !== this.sessionId) {
      return;
    }
    if (event.method === 'Runtime.exceptionThrown') {
      const details = event.params.exceptionDetails;
      this.pageErrors.push((details.exception && details.exception.description) || details.text);
    } else if (event.method === 'Runtime.consoleAPICalled' && ['error', 'assert'].includes(event.params.type)) {
      this.consoleErrors.push(event.params.args.map((arg) => arg.value || arg.description).join(' '));
    } else if (event.method === 'Log.entryAdded' && event.params.entry.level === 'error') {
      this.consoleErrors.push(`${event.params.entry.text} ${event.params.entry.url || ''}`.trim());
    }
  }

  /**
   * Send a page command.
   * @param {string} method The protocol method.
   * @param {object} [params] The parameters.
   * @returns {Promise<object>} The command result.
   */
  call(method, params = {}) {
    return this.cdp.send(method, params, this.sessionId);
  }

  /**
   * Name the step the scenario is on, for the failure report.
   * @param {string} name What the scenario does next.
   */
  step(name) {
    this.currentStep = name;
  }

  /**
   * Keep a value for the result line.
   * @param {string} name The fact name.
   * @param {*} value A JSON-safe value.
   */
  fact(name, value) {
    this.facts[name] = value;
  }

  /**
   * Run JavaScript in the page and return the JSON-safe result.
   * @param {string} expression The code. A promise is awaited.
   * @returns {Promise<*>} The value.
   * @throws {Error} When the code throws.
   */
  async evaluate(expression) {
    const result = await this.call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) {
      const details = result.exceptionDetails;
      throw new Error(`page code failed: ${(details.exception && details.exception.description) || details.text} in: ${expression.slice(0, 200)}`);
    }
    return result.result.value;
  }

  /**
   * Wait for a condition, polling in the page.
   * @param {string} expression Page code that is truthy when the wait is over.
   * @param {string} what What is awaited, for the timeout message.
   * @param {number} [timeoutMs] The deadline.
   * @returns {Promise<void>} Settles when the condition holds.
   * @throws {Error} Naming what was awaited when the deadline passes.
   */
  async waitFor(expression, what, timeoutMs = DEFAULT_WAIT_MS) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      if (await this.evaluate(`Boolean(${expression})`)) {
        return;
      }
      if (Date.now() > deadline) {
        throw new Error(`timed out after ${timeoutMs} ms waiting for ${what}`);
      }
      await sleep(POLL_MS);
    }
  }

  /**
   * Dispatch one mouse event.
   * @param {string} type mouseMoved, mousePressed, or mouseReleased.
   * @param {number} x Viewport x in CSS pixels.
   * @param {number} y Viewport y in CSS pixels.
   * @param {object} [extra] More Input.dispatchMouseEvent parameters.
   */
  async mouse(type, x, y, extra = {}) {
    await this.call('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1, ...extra });
  }

  /**
   * Click like a user: move, press, release.
   * @param {number} x Viewport x in CSS pixels.
   * @param {number} y Viewport y in CSS pixels.
   * @param {{modifiers?: number, button?: string}} [options] Modifier bits (Shift is 8) and the
   *   button, "left" or "right".
   */
  async click(x, y, { modifiers = 0, button = 'left' } = {}) {
    await this.mouse('mouseMoved', x, y, { button: 'none', buttons: 0, modifiers });
    await this.mouse('mousePressed', x, y, { button, buttons: BUTTON_BITS[button], modifiers });
    await this.mouse('mouseReleased', x, y, { button, buttons: 0, modifiers });
  }

  /**
   * Press and release one key as a real keyboard would.
   * @param {string} name A digit, a letter, ".", ",", "[", "]", or Enter, Escape, Space, Backspace.
   * @param {{shift?: boolean}} [options] Shift holds modifier 8; a digit then arrives as its
   *   symbol with the Digit code, the way a real keyboard sends it.
   */
  async key(name, { shift = false } = {}) {
    const info = keyInfo(name, shift);
    const modifiers = shift ? SHIFT : 0;
    await this.call('Input.dispatchKeyEvent', {
      type: info.text ? 'keyDown' : 'rawKeyDown',
      key: info.key,
      code: info.code,
      windowsVirtualKeyCode: info.vk,
      nativeVirtualKeyCode: info.vk,
      text: info.text,
      unmodifiedText: info.unmodified,
      modifiers,
    });
    await this.call('Input.dispatchKeyEvent', {
      type: 'keyUp', key: info.key, code: info.code, windowsVirtualKeyCode: info.vk, nativeVirtualKeyCode: info.vk, modifiers,
    });
  }

  /**
   * Hold one key down for a while, sending repeats as a real keyboard does, then release it.
   * @param {string} name A key name keyInfo knows.
   * @param {number} ms How long to hold, in milliseconds.
   */
  async hold(name, ms) {
    const info = keyInfo(name, false);
    const down = { key: info.key, code: info.code, windowsVirtualKeyCode: info.vk, nativeVirtualKeyCode: info.vk };
    await this.call('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...down });
    const until = Date.now() + ms;
    while (Date.now() < until) {
      await sleep(KEY_REPEAT_MS);
      await this.call('Input.dispatchKeyEvent', { type: 'rawKeyDown', autoRepeat: true, ...down });
    }
    await this.call('Input.dispatchKeyEvent', { type: 'keyUp', ...down });
  }

  /**
   * Save a screenshot of the page.
   * @param {string} name The file name without .png.
   * @returns {Promise<string>} The absolute file path.
   */
  async shot(name) {
    const { data } = await this.call('Page.captureScreenshot', { format: 'png' });
    const file = path.resolve(OUT_DIR, `${name}.png`);
    writeFileSync(file, Buffer.from(data, 'base64'));
    this.shots.push(file);
    return file;
  }
}

/**
 * Wait without blocking the event loop.
 * @param {number} ms Milliseconds.
 * @returns {Promise<void>} Settles after the delay.
 */
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Describe a key for Input.dispatchKeyEvent.
 * @param {string} name The key name used by the scenarios.
 * @param {boolean} shift True when Shift is held.
 * @returns {{key: string, code: string, vk: number, text: (string|undefined), unmodified: (string|undefined)}} The key.
 * @throws {Error} When the name is not one the driver knows.
 */
function keyInfo(name, shift) {
  if (NAMED_KEYS[name]) {
    const named = NAMED_KEYS[name];
    return { ...named, unmodified: named.text };
  }
  if (/^[0-9]$/.test(name)) {
    const symbol = SHIFTED_DIGITS[name];
    return {
      key: shift ? symbol : name,
      code: `Digit${name}`,
      vk: 48 + Number(name),
      text: shift ? symbol : name,
      unmodified: name,
    };
  }
  if (/^[a-z]$/.test(name)) {
    const letter = shift ? name.toUpperCase() : name;
    return { key: letter, code: `Key${name.toUpperCase()}`, vk: name.toUpperCase().charCodeAt(0), text: letter, unmodified: name };
  }
  throw new Error(`the driver has no key named ${JSON.stringify(name)}`);
}

// -------------------------------------------------------------- page helpers

/**
 * Load the app and wait until it listed its species.
 * @param {Session} s The session.
 */
async function loadApp(s) {
  s.step('load the app');
  await s.call('Page.navigate', { url: APP_URL });
  await s.waitFor(`document.readyState === 'complete' && ${S} && ${S}.state.species.length > 0`, 'the page to load its species list');
}

/**
 * Reload the page and wait for the new document. A marker on the old window tells the two
 * documents apart, because the old one can still answer while the reload starts.
 * @param {Session} s The session.
 * @param {string} what What the reload is for, for the timeout message.
 */
async function reloadApp(s, what) {
  await s.evaluate('window.__beforeReload = true');
  await s.call('Page.reload');
  await s.waitFor(`!window.__beforeReload && document.readyState === 'complete' && ${S} && ${S}.state.species.length > 0`, what);
}

/**
 * Open a folder of the video browser through the page's own rows.
 * @param {Session} s The session.
 * @param {object} config The scenario config.
 * @param {string} folder The folder prefix, such as TCRMP_video_ondeck/2024Annual/.
 */
async function openFolder(s, config, folder) {
  s.step(`open the folder ${folder}`);
  const here = await s.evaluate(`${S}.state.prefix`);
  if (here === folder) {
    return;
  }
  if (here !== config.root) {
    await s.evaluate(`document.querySelector('.crumb[data-prefix="${config.root}"]').click()`);
    await s.waitFor(`${S}.state.prefix === '${config.root}' && !${S}.state.catalogLoading`, 'the root folder to list');
  }
  await s.waitFor(`document.querySelector('#folders [data-prefix="${folder}"]')`, `a folder row for ${folder}`);
  await s.evaluate(`document.querySelector('#folders [data-prefix="${folder}"]').click()`);
  await s.waitFor(`${S}.state.prefix === '${folder}' && !${S}.state.catalogLoading`, `${folder} to list`);
}

/**
 * Open a video from its row in the browser, and wait until it has frames.
 * @param {Session} s The session.
 * @param {object} config The scenario config.
 * @param {string} key The S3 key of the video.
 * @param {?number} startAt A time in seconds to seek to, or null to keep where it opened.
 */
async function openVideo(s, config, key, startAt = START_SECONDS) {
  await openFolder(s, config, key.slice(0, key.lastIndexOf('/') + 1));
  s.step(`open the video ${key}`);
  await s.waitFor(`document.querySelector('.video-open[data-key="${key}"]')`, `a row for the video ${key}`);
  await s.evaluate(`document.querySelector('.video-open[data-key="${key}"]').click()`);
  await s.waitFor(`${S}.state.key === '${key}' && ${V}.readyState >= 3 && ${V}.videoWidth > 0`, 'the video to load', VIDEO_WAIT_MS);
  if (startAt !== null) {
    await s.evaluate(`${V}.currentTime = ${startAt}`);
    await s.waitFor(`!${V}.seeking && ${V}.readyState >= 3`, `the seek to ${startAt} s`, VIDEO_WAIT_MS);
  }
  await waitPlaying(s, 'the opened video to play by itself');
}

/**
 * Wait until the video is playing: not paused, not seeking, and the clock advancing.
 * @param {Session} s The session.
 * @param {string} what What is awaited, for the timeout message.
 * @throws {Error} When the video does not play within 15 seconds.
 */
async function waitPlaying(s, what) {
  const playing = await s.evaluate(`new Promise((resolve) => {
    const video = ${V};
    const start = video.currentTime;
    const end = performance.now() + 15000;
    const tick = () => {
      if (!video.paused && !video.seeking && video.currentTime > start + 0.1) resolve(true);
      else if (performance.now() > end) resolve(false);
      else setTimeout(tick, ${POLL_MS});
    };
    tick();
  })`);
  if (!playing) {
    throw new Error(`timed out waiting for ${what}`);
  }
}

/**
 * Press Space and wait until the video plays. A pick leaves the video paused, so a scenario
 * that needs the clock moving again (a second pick on a later frame, a jump back to an earlier
 * one) calls this.
 * @param {Session} s The session.
 * @param {string} what What is awaited, for the timeout message.
 * @throws {Error} When the video plays already (Space would pause it) or does not start.
 */
async function playOn(s, what) {
  if (!(await s.evaluate(`${V}.paused`))) {
    throw new Error(`the video was playing before Space, which would pause it: ${what}`);
  }
  await s.key('Space');
  await waitPlaying(s, what);
}

/**
 * Seek back near the start and play, when the clip is about to end. The clips are short, so
 * a scenario with several picks calls this between them.
 * @param {Session} s The session.
 */
async function rewindIfLate(s) {
  const late = await s.evaluate(`${V}.ended || ${V}.currentTime > ${REWIND_AFTER_SECONDS}`);
  if (late) {
    await s.evaluate(`(() => { ${V}.currentTime = ${START_SECONDS}; return ${V}.play(); })()`);
    await s.waitFor(`!${V}.seeking && !${V}.paused`, 'the rewind to land and play');
    await waitPlaying(s, 'the video to play after the rewind');
  }
}

/**
 * Read how far the clock moved in a stretch of wall time, timed inside the page.
 * @param {Session} s The session.
 * @param {number} ms The wall time to watch.
 * @returns {Promise<{delta: number, wall: number, rate: number, paused: boolean}>} Seconds of video
 *   advanced, seconds of wall time that passed, their ratio, and the paused flag at the end.
 */
function clockDelta(s, ms) {
  return s.evaluate(`new Promise((resolve) => {
    const video = ${V};
    const clock = video.currentTime;
    const wall = performance.now();
    setTimeout(() => {
      const delta = video.currentTime - clock;
      const seconds = (performance.now() - wall) / 1000;
      resolve({ delta, wall: seconds, rate: delta / seconds, paused: video.paused });
    }, ${ms});
  })`);
}

/**
 * Read what a pick leaves behind: whether the video is paused and where its clock stands, how
 * the clock moves over a short stretch of wall time, whether the wheel is open, and the mode.
 * A plain pick leaves the video paused and the clock still; a Shift pick leaves it playing.
 * @param {Session} s The session.
 * @returns {Promise<{paused: boolean, time: number, clock: object, wheel: boolean, mode: string}>} The
 *   paused flag and the time at the start, the clockDelta reading over STILL_MS, the wheel flag, and the mode.
 */
async function readAfterPick(s) {
  const paused = await s.evaluate(`${V}.paused`);
  const time = await s.evaluate(`${V}.currentTime`);
  const clock = await clockDelta(s, STILL_MS);
  return {
    paused,
    time,
    clock,
    wheel: await s.evaluate(`Boolean(${S}.wheel())`),
    mode: await s.evaluate(`${S}.state.mode`),
  };
}

/**
 * Click a spot on the picture, given as fractions of the picture, with real mouse events.
 * @param {Session} s The session.
 * @param {number} fx Fraction of the picture width.
 * @param {number} fy Fraction of the picture height.
 * @param {{modifiers?: number, button?: string}} [options] Modifier bits and the button.
 * @returns {Promise<{x: number, y: number}>} The viewport pixel clicked.
 */
async function clickPicture(s, fx, fy, options = {}) {
  const rect = await s.evaluate(`${S}.pictureRect()`);
  const spot = { x: rect.x + rect.w * fx, y: rect.y + rect.h * fy };
  await s.click(spot.x, spot.y, options);
  return spot;
}

/**
 * Wait for the wheel and return its snapshot.
 * @param {Session} s The session.
 * @param {?string} mode "new" or "edit", or null for either.
 * @returns {Promise<object>} The snapshot of window.__screener.wheel().
 */
async function waitWheel(s, mode = null) {
  await s.waitFor(mode ? `${S}.wheel() && ${S}.wheel().mode === '${mode}'` : `${S}.wheel()`, `the wheel to open${mode ? ` in mode ${mode}` : ''}`);
  return s.evaluate(`${S}.wheel()`);
}

/**
 * Shrink a wheel snapshot to what the result line needs.
 * @param {object} wheel The snapshot.
 * @returns {object} Mode, hub, query, highlight, the inner slots with codes, the outer codes.
 */
function summarizeWheel(wheel) {
  return {
    mode: wheel.mode,
    rowId: wheel.rowId,
    hubLabel: wheel.hubLabel,
    query: wheel.query,
    highlighted: wheel.highlighted,
    inner: wheel.inner.map((slice) => `${slice.slot}:${slice.code}`),
    outer: wheel.outer.map((slice) => slice.code),
  };
}

/**
 * Find the inner slice of a pinned key in a wheel snapshot.
 * @param {object} wheel The snapshot.
 * @param {string} slot The key, "1" to "9" or "0".
 * @returns {{code: string, slot: string, x: number, y: number}} The slice.
 * @throws {Error} When the inner ring has no such slice.
 */
function innerSlice(wheel, slot) {
  const found = wheel.inner.find((slice) => slice.slot === slot);
  if (!found) {
    throw new Error(`the inner ring has no slice for key ${slot}: ${JSON.stringify(summarizeWheel(wheel).inner)}`);
  }
  return found;
}

/**
 * Read the rows of the open video as the page holds them.
 * @param {Session} s The session.
 * @returns {Promise<Array<object>>} Rows with the columns the checks use.
 */
function pageRows(s) {
  return s.evaluate(`${S}.state.rows.map((row) => ({
    ID: row.ID, SpeciesCode: row.SpeciesCode, LabelStatus: row.LabelStatus, TimestampSeconds: row.TimestampSeconds,
    PointX: row.PointX, PointY: row.PointY, FrameFileName: row.FrameFileName, CropFileName: row.CropFileName, S3Key: row.S3Key,
  }))`);
}

/**
 * Click a pinned key's inner slice with the real mouse and wait until the wheel closed.
 * @param {Session} s The session.
 * @param {object} wheel A wheel snapshot.
 * @param {string} slot The key whose slice to click.
 * @param {number} [modifiers] Modifier bits.
 */
async function clickInner(s, wheel, slot, modifiers = 0) {
  const slice = innerSlice(wheel, slot);
  await s.click(slice.x, slice.y, { modifiers });
  await s.waitFor(`!${S}.wheel()`, `the wheel to close after the click on key ${slot}`);
}

/**
 * Wait for the row count of the open video, then return the rows.
 * @param {Session} s The session.
 * @param {number} count The row count to wait for.
 * @returns {Promise<Array<object>>} The rows.
 */
async function waitRows(s, count) {
  await s.waitFor(`${S}.state.rows.length === ${count}`, `${count} row(s) in the sightings list`);
  return pageRows(s);
}

/**
 * Read a rectangle of the page, and wait until it stops changing (a layout that follows
 * full screen or a resize).
 * @param {Session} s The session.
 * @returns {Promise<{x: number, y: number, w: number, h: number}>} The picture rectangle in viewport pixels.
 */
async function stablePicture(s) {
  const deadline = Date.now() + STABLE_RECT_MS;
  let last = JSON.stringify(await s.evaluate(`${S}.pictureRect()`));
  for (;;) {
    await sleep(120);
    const now = JSON.stringify(await s.evaluate(`${S}.pictureRect()`));
    if (now === last) {
      return JSON.parse(now);
    }
    if (Date.now() > deadline) {
      throw new Error(`the picture rectangle kept changing for ${STABLE_RECT_MS} ms: ${last} then ${now}`);
    }
    last = now;
  }
}

/**
 * Click the center of an element with the real mouse after scrolling it into view.
 * @param {Session} s The session.
 * @param {string} selector A CSS selector for one element.
 * @returns {Promise<{x: number, y: number}>} The viewport pixel clicked.
 * @throws {Error} When no element matches.
 */
async function clickElement(s, selector) {
  const spot = await s.evaluate(`(() => {
    const node = document.querySelector(${JSON.stringify(selector)});
    if (!node) return null;
    node.scrollIntoView({ block: 'center', inline: 'center' });
    const box = node.getBoundingClientRect();
    return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
  })()`);
  if (!spot) {
    throw new Error(`no element matches ${selector}`);
  }
  await s.click(spot.x, spot.y);
  return spot;
}

/**
 * Describe where the wheel sits against the viewport and the mark.
 * @param {Session} s The session.
 * @returns {Promise<object>} The wheel box, the viewport, the wheel layer, the center, the mark in
 *   viewport pixels, and whether the leader line shows. Null fields when the wheel is closed.
 */
function wheelPlacement(s) {
  return s.evaluate(`(() => {
    const snapshot = ${S}.wheel();
    const root = document.getElementById('wheel');
    const layer = document.getElementById('wheel-layer').getBoundingClientRect();
    const pending = ${S}.state.pending;
    const picture = ${S}.pictureRect();
    const box = root ? root.getBoundingClientRect() : null;
    const leader = document.querySelector('#wheel .wheel-leader');
    return {
      open: Boolean(snapshot),
      center: snapshot && snapshot.center,
      hubRadius: snapshot && snapshot.hub.r,
      box: box && { left: box.left, top: box.top, right: box.right, bottom: box.bottom },
      view: { w: innerWidth, h: innerHeight },
      layer: { left: layer.left, top: layer.top, right: layer.right, bottom: layer.bottom },
      mark: pending && { x: picture.x + pending.point.x * picture.w, y: picture.y + pending.point.y * picture.h },
      leaderShown: Boolean(leader) && !leader.hasAttribute('hidden'),
      fullscreenElement: document.fullscreenElement && document.fullscreenElement.id,
    };
  })()`);
}

/**
 * Count the pixels the page drew on its overlay canvas near a viewport point.
 * @param {Session} s The session.
 * @param {{x: number, y: number}} spot The viewport point.
 * @param {number} radius The reach in CSS pixels.
 * @returns {Promise<{ink: number, amber: number}>} Pixels with any ink, and pixels near the Later amber.
 */
function overlayInk(s, spot, radius) {
  return s.evaluate(`(() => {
    const canvas = document.getElementById('overlay');
    const box = canvas.getBoundingClientRect();
    const scale = canvas.width / box.width;
    const left = Math.max(0, Math.round((${spot.x} - ${radius} - box.left) * scale));
    const top = Math.max(0, Math.round((${spot.y} - ${radius} - box.top) * scale));
    const size = Math.round(${radius} * 2 * scale);
    const data = canvas.getContext('2d').getImageData(left, top, size, size).data;
    let ink = 0;
    let amber = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i + 3] > 0) ink += 1;
      if (data[i + 3] > 128 && data[i] > 200 && data[i + 1] > 130 && data[i + 1] < 220 && data[i + 2] < 120) amber += 1;
    }
    return { ink, amber };
  })()`);
}

/**
 * Read the panel under the video after the layout settled: how it draws, what the page and the
 * button say, what localStorage holds, and how tall the stage is.
 * @param {Session} s The session.
 * @returns {Promise<{display: string, deck: string, shown: boolean, pressed: ?string, stored: ?string,
 *   stageHeight: number}>} The computed display of `.deck`, body[data-deck], the page state,
 *   aria-pressed of the button, the stored choice, and the height of the stage.
 */
async function readDeck(s) {
  await sleep(LAYOUT_SETTLE_MS);
  return s.evaluate(`({
    display: getComputedStyle(document.querySelector('.deck')).display,
    deck: document.body.dataset.deck,
    shown: ${S}.state.deckShown,
    pressed: document.getElementById('deck-toggle').getAttribute('aria-pressed'),
    stored: localStorage.getItem('screener.deck'),
    stageHeight: document.getElementById('stage').getBoundingClientRect().height,
  })`);
}

// ------------------------------------------------------------------ scenarios

const SCENARIOS = {
  /** Click the picture, click the inner slice of key 1, and see the video stay paused on the picked frame. */
  async pick_inner(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('click the picture at (0.30, 0.40)');
    await clickPicture(s, 0.30, 0.40);
    const wheel = await waitWheel(s, 'new');
    s.fact('wheel', summarizeWheel(wheel));
    s.fact('pausedWithWheelOpen', await s.evaluate(`${V}.paused`));
    s.step('click the inner slice of key 1');
    await clickInner(s, wheel, '1');
    s.step('wait for the saved row');
    s.fact('rows', await waitRows(s, 1));
    s.fact('afterPick', await readAfterPick(s));
    s.fact('lastToast', await s.evaluate(`${S}.state.lastToast`));
  },

  /** Pick by number key 2, then by typing a species code and Enter. */
  async pick_key_and_outer(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('click, then press 2');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('2');
    s.fact('afterKey2', await waitRows(s, 1));
    s.fact('pausedAfterKey2', await readAfterPick(s));
    s.step('press Space to play on');
    await playOn(s, 'the video to play on after key 2');
    await sleep(300);
    s.step('click, type saur, press Enter');
    await clickPicture(s, 0.60, 0.55);
    await waitWheel(s, 'new');
    for (const letter of 'saur') {
      await s.key(letter);
    }
    s.fact('typed', summarizeWheel(await s.evaluate(`${S}.wheel()`)));
    await s.key('Enter');
    s.fact('rows', await waitRows(s, 2));
    s.fact('pausedAfterEnter', await readAfterPick(s));
  },

  /** Two picks less than a second apart, once at natural timing and once with both saves on the server together. */
  async rapid_picks(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('first pair, natural timing');
    await clickPicture(s, 0.25, 0.40);
    await waitWheel(s, 'new');
    await s.key('1');
    const firstAt = Date.now();
    await playOn(s, 'the video to play on after the first pick');
    await clickPicture(s, 0.60, 0.55);
    await waitWheel(s, 'new');
    await s.key('2');
    s.fact('firstPairGapMs', Date.now() - firstAt);
    await waitRows(s, 2);
    s.step('second pair, saves held until both are waiting');
    await playOn(s, 'the video to play on before the second pair');
    await rewindIfLate(s);
    await s.evaluate('window.__e2e.holdPosts(2)');
    await clickPicture(s, 0.30, 0.65);
    await waitWheel(s, 'new');
    await s.key('3');
    const thirdAt = Date.now();
    await playOn(s, 'the video to play on after the third pick');
    await clickPicture(s, 0.70, 0.30);
    await waitWheel(s, 'new');
    await s.key('1');
    s.fact('secondPairGapMs', Date.now() - thirdAt);
    s.fact('rows', await waitRows(s, 4));
    s.fact('listOrder', await s.evaluate(`Array.from(document.querySelectorAll('#sightings .sighting')).map((item) => ({ id: item.dataset.id, time: Number(item.dataset.time) }))`));
    s.fact('posts', await s.evaluate(`window.__e2e.log.filter((entry) => entry.url === '/api/observations').map((entry) => ({ status: entry.status, id: entry.body && entry.body.row && entry.body.row.ID, startedAt: entry.startedAt, endedAt: entry.endedAt }))`));
  },

  /** Step three frames with the wheel open, then pick: the row holds the stepped time. */
  async step_then_pick(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('click the picture');
    await clickPicture(s, 0.40, 0.50);
    await waitWheel(s, 'new');
    s.fact('timeAtClick', await s.evaluate(`${V}.currentTime`));
    s.step('press . three times');
    await s.key('.');
    await s.key('.');
    await s.key('.');
    await s.waitFor(`!${V}.seeking`, 'the three frame steps to land');
    await sleep(150);
    s.fact('stepped', await s.evaluate(`({ video: ${V}.currentTime, pending: ${S}.state.pending && ${S}.state.pending.time, clock: document.getElementById('time').textContent, paused: ${V}.paused, wheelOpen: Boolean(${S}.wheel()) })`));
    s.step('press 1');
    await s.key('1');
    s.fact('rows', await waitRows(s, 1));
  },

  /** A Later point with Space, then a cancel with Escape. */
  async later_and_cancel(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('click, then press Space');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    s.fact('hub', (await s.evaluate(`${S}.wheel()`)).hubLabel);
    await s.key('Space');
    s.fact('rows', await waitRows(s, 1));
    await s.waitFor(`document.getElementById('to-label-count').textContent === '1'`, 'the To label count to read 1');
    s.fact('toLabel', await s.evaluate(`({ count: document.getElementById('to-label-count').textContent, items: document.querySelectorAll('#to-label .to-label-item').length, footer: document.getElementById('tally-total').textContent })`));
    s.fact('afterLater', await readAfterPick(s));
    s.step('click, then press Escape');
    await clickPicture(s, 0.65, 0.30);
    await waitWheel(s, 'new');
    await s.key('Escape');
    await s.waitFor(`!${S}.wheel()`, 'the wheel to close on Escape');
    await sleep(500);
    s.fact('afterEscape', {
      rows: await s.evaluate(`${S}.state.rows.length`),
      mode: await s.evaluate(`${S}.state.mode`),
      clock: await clockDelta(s, 400),
    });
  },

  /** A Later point in a video of another folder, relabeled from the To label list. */
  async relabel_from_list(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2023);
    s.step('save a Later point in the 2023 video');
    await clickPicture(s, 0.35, 0.45);
    await waitWheel(s, 'new');
    await s.key('Space');
    const [later] = await waitRows(s, 1);
    s.fact('laterRow', later);
    await s.waitFor(`document.querySelector('#to-label .to-label-item[data-id="${later.ID}"]')`, 'the Later point in the To label list');
    s.fact('toLabelThumbBefore', await s.evaluate(`(() => { const image = document.querySelector('#to-label .to-label-item[data-id="${later.ID}"] .sighting-thumb'); return { src: image.getAttribute('src'), loaded: image.complete && image.naturalWidth > 0 }; })()`));
    s.step('switch to the 2024 video');
    await openVideo(s, config, config.key2024);
    s.fact('listingHas2023', await s.evaluate(`Boolean(document.querySelector('.video-open[data-key="${config.key2023}"]'))`));
    s.fact('toLabelCountBefore', await s.evaluate(`document.getElementById('to-label-count').textContent`));
    s.step('click the To label item');
    await clickElement(s, `#to-label .to-label-item[data-id="${later.ID}"] .sighting-jump`);
    await s.waitFor(`${S}.state.key === '${config.key2023}' && ${S}.wheel()`, 'the 2023 video to open with the wheel on its point', VIDEO_WAIT_MS);
    await sleep(WHEEL_SETTLE_MS);
    s.fact('jump', await s.evaluate(`({
      key: ${S}.state.key,
      wheel: (() => { const w = ${S}.wheel(); return { mode: w.mode, rowId: w.rowId, hubLabel: w.hubLabel }; })(),
      time: ${V}.currentTime,
      paused: ${V}.paused,
      seeking: ${V}.seeking,
      title: document.getElementById('current-video').textContent,
    })`));
    s.step('press 3');
    await s.key('3');
    await s.waitFor(`(${S}.state.rows.find((row) => row.ID === '${later.ID}') || {}).LabelStatus === 'labeled'`, 'the row to read labeled');
    await s.waitFor(`document.getElementById('to-label-count').textContent === '0'`, 'the To label count to read 0');
    s.fact('rows', await pageRows(s));
    await s.waitFor(`(() => { const image = document.querySelector('#sightings .sighting[data-id="${later.ID}"] .sighting-thumb'); return image && image.complete && image.naturalWidth > 0; })()`, 'the sightings thumbnail to load the new crop');
    s.fact('thumbAfter', await s.evaluate(`(() => { const image = document.querySelector('#sightings .sighting[data-id="${later.ID}"] .sighting-thumb'); return { src: image.getAttribute('src'), loaded: image.complete && image.naturalWidth > 0, missing: image.dataset.missing === 'true' }; })()`));
    s.fact('toLabelAfter', await s.evaluate(`({ count: document.getElementById('to-label-count').textContent, items: document.querySelectorAll('#to-label .to-label-item').length })`));
    s.fact('lastToast', await s.evaluate(`${S}.state.lastToast`));
    s.fact('afterRelabel', await readAfterPick(s));
  },

  /** Pause on a saved mark, click it, and press Space in the Unlabel wheel. */
  async unlabel_saved(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('save a labeled sighting');
    await clickPicture(s, 0.30, 0.40);
    const wheel = await waitWheel(s, 'new');
    await clickInner(s, wheel, '1');
    const [saved] = await waitRows(s, 1);
    s.fact('savedRow', saved);
    s.step('play on, then jump to the saved row and wait for the pause');
    await playOn(s, 'the video to play on after the pick');
    await clickElement(s, `#sightings .sighting[data-id="${saved.ID}"] .sighting-jump`);
    await s.waitFor(`${V}.paused && !${V}.seeking && ${V}.readyState >= 2 && Math.abs(${V}.currentTime - ${saved.TimestampSeconds}) < 0.05`, 'the video to pause on the saved time');
    await sleep(300);
    s.step('click the saved mark');
    const rect = await s.evaluate(`${S}.pictureRect()`);
    await s.click(rect.x + Number(saved.PointX) * rect.w, rect.y + Number(saved.PointY) * rect.h);
    await waitWheel(s, 'edit');
    await sleep(WHEEL_SETTLE_MS);
    s.fact('editWheel', await s.evaluate(`(() => { const w = ${S}.wheel(); const current = document.querySelector('#wheel .wheel-slice[data-current="true"]'); return { mode: w.mode, rowId: w.rowId, hubLabel: w.hubLabel, currentCode: current && current.dataset.code, paused: ${V}.paused }; })()`));
    s.step('press Space');
    await s.key('Space');
    await s.waitFor(`(${S}.state.rows.find((row) => row.ID === '${saved.ID}') || {}).LabelStatus === 'unlabeled'`, 'the row to read unlabeled');
    await s.waitFor(`document.getElementById('to-label-count').textContent === '1'`, 'the To label count to read 1');
    s.fact('rows', await pageRows(s));
    s.fact('lastToast', await s.evaluate(`${S}.state.lastToast`));
    s.fact('afterUnlabel', await readAfterPick(s));
  },

  /** A done video that gets a Later point reopens; a video with a Later point refuses Done. */
  async done_refused(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('save a labeled sighting, then press D');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('1');
    await waitRows(s, 1);
    await s.key('d');
    await s.waitFor(`document.querySelector('.toast-action')`, 'the warning about unwatched time');
    await clickElement(s, '.toast-action');
    await s.waitFor(`${S}.state.videoStatus === 'done'`, 'the video to read done');
    s.fact('afterDone', await s.evaluate(`({ status: ${S}.state.videoStatus, button: document.getElementById('done-label').textContent, pressed: document.getElementById('done').getAttribute('aria-pressed') })`));
    s.step('save a Later point in the done video');
    await rewindIfLate(s);
    await clickPicture(s, 0.60, 0.55);
    await waitWheel(s, 'new');
    await s.key('Space');
    await waitRows(s, 2);
    await s.waitFor(`/back in progress/.test(${S}.state.lastToast ? ${S}.state.lastToast.message : '')`, 'the "back in progress" toast');
    s.fact('afterReopen', await s.evaluate(`({
      status: ${S}.state.videoStatus,
      button: document.getElementById('done-label').textContent,
      pressed: document.getElementById('done').getAttribute('aria-pressed'),
      toast: ${S}.state.lastToast,
      toastsOnScreen: Array.from(document.querySelectorAll('#toasts .toast')).map((node) => ({ kind: node.dataset.kind, text: node.dataset.text })),
      browserBadge: (document.querySelector('.video-row[aria-current="true"] .badge') || {}).textContent,
    })`));
    s.fact('laterPost', await s.evaluate(`window.__e2e.log.filter((entry) => entry.url === '/api/observations').map((entry) => ({ status: entry.status, reopened: entry.body && entry.body.reopened, species: entry.body && entry.body.row && entry.body.row.SpeciesCode }))`));
    s.step('press D with the Later point in the video');
    await s.key('d');
    await s.waitFor(`/unlabeled/.test(${S}.state.lastToast ? ${S}.state.lastToast.message : '')`, 'the refusal toast');
    s.fact('afterRefusal', await s.evaluate(`({
      status: ${S}.state.videoStatus,
      button: document.getElementById('done-label').textContent,
      toast: ${S}.state.lastToast,
      doneRequests: window.__e2e.log.filter((entry) => entry.url === '/api/videos/done').length,
    })`));
  },

  /** F enters full screen, the wheel stays inside it, the button re-centers an open wheel. */
  async fullscreen(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.fact('windowedView', await s.evaluate('[innerWidth, innerHeight]'));
    s.step('press F');
    await s.key('f');
    await s.waitFor(`document.fullscreenElement && document.fullscreenElement.id === 'theater'`, 'the theater to enter full screen');
    await sleep(LAYOUT_SETTLE_MS);
    s.fact('entered', await s.evaluate(`({ element: document.fullscreenElement && document.fullscreenElement.id, flag: document.body.dataset.fullscreen, state: ${S}.state.fullscreen, view: [innerWidth, innerHeight] })`));
    s.step('click near the top-left corner of the picture');
    await rewindIfLate(s);
    const corner = await stablePicture(s);
    await s.click(corner.x + 12, corner.y + 10);
    await waitWheel(s, 'new');
    await sleep(WHEEL_SETTLE_MS);
    s.fact('corner', await wheelPlacement(s));
    s.step('press Escape, then F');
    await s.key('Escape');
    await s.waitFor(`!${S}.wheel()`, 'the wheel to close on Escape in full screen');
    s.fact('afterEscape', await s.evaluate('document.fullscreenElement && document.fullscreenElement.id'));
    if (await s.evaluate('Boolean(document.fullscreenElement)')) {
      await s.key('f');
    }
    await s.waitFor('!document.fullscreenElement', 'full screen to end');
    await sleep(LAYOUT_SETTLE_MS);
    s.fact('left', await s.evaluate(`({ element: document.fullscreenElement && document.fullscreenElement.id, flag: document.body.dataset.fullscreen, state: ${S}.state.fullscreen, view: [innerWidth, innerHeight] })`));
    s.step('open the wheel, then toggle full screen with the button');
    await rewindIfLate(s);
    await stablePicture(s);
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await sleep(WHEEL_SETTLE_MS);
    s.fact('windowedWheel', await wheelPlacement(s));
    await clickElement(s, '#fullscreen');
    await s.waitFor(`document.fullscreenElement && document.fullscreenElement.id === 'theater'`, 'full screen to start from the button');
    await sleep(LAYOUT_SETTLE_MS);
    await stablePicture(s);
    s.fact('fullscreenWheel', await wheelPlacement(s));
    await clickElement(s, '#fullscreen');
    await s.waitFor('!document.fullscreenElement', 'full screen to end from the button');
    await sleep(LAYOUT_SETTLE_MS);
    await stablePicture(s);
    s.fact('leftWheel', await wheelPlacement(s));
    s.step('resize the window with the wheel open');
    const [smallW, smallH] = SIZE_PRESETS.small;
    await s.call('Emulation.setDeviceMetricsOverride', { width: smallW, height: smallH, deviceScaleFactor: 1, mobile: false });
    await sleep(LAYOUT_SETTLE_MS);
    await stablePicture(s);
    s.fact('shrunkWheel', await wheelPlacement(s));
    await s.call('Emulation.clearDeviceMetricsOverride');
    await sleep(LAYOUT_SETTLE_MS);
    await stablePicture(s);
    s.fact('restoredWheel', await wheelPlacement(s));
  },

  /** A fresh profile plays at 1.5x, ignores the old speed key, and remembers a new choice. */
  async speed_default(s, config) {
    await loadApp(s);
    s.fact('labelBeforeVideo', await s.evaluate(`document.getElementById('speed').textContent`));
    await openVideo(s, config, config.key2024);
    s.fact('fresh', await s.evaluate(`({ rate: ${V}.playbackRate, label: document.getElementById('speed').textContent, state: ${S}.state.speed, stored: localStorage.getItem('screener.speed.v2') })`));
    s.fact('clock', await clockDelta(s, 1000));
    s.step('plant the old speed key and reload');
    await s.evaluate(`localStorage.setItem('screener.speed', '1')`);
    await reloadApp(s, 'the reloaded page');
    s.fact('afterOldKey', await s.evaluate(`({ label: document.getElementById('speed').textContent, state: ${S}.state.speed })`));
    s.step('press ] and reload');
    await openVideo(s, config, config.key2024);
    await s.key(']');
    await s.waitFor(`${S}.state.speed === 2`, 'the speed to read 2');
    s.fact('afterFaster', await s.evaluate(`({ rate: ${V}.playbackRate, label: document.getElementById('speed').textContent, stored: localStorage.getItem('screener.speed.v2') })`));
    s.step('reload after the speed change');
    await reloadApp(s, 'the page after the second reload');
    s.fact('afterReload', await s.evaluate(`({ label: document.getElementById('speed').textContent, state: ${S}.state.speed })`));
  },

  /** Holding Right fast-forwards, holding Left rewinds, and with the wheel open an arrow steps one frame. */
  async arrow_scrub(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('pause at 4 s');
    await s.evaluate(`${V}.pause(); ${V}.currentTime = 4;`);
    await s.waitFor(`${V}.paused && !${V}.seeking && ${V}.readyState >= 2`, 'the video to pause at 4 s');
    const read = `({ time: ${V}.currentTime, rate: ${V}.playbackRate, paused: ${V}.paused })`;
    s.step('hold Right for one second');
    const beforeForward = await s.evaluate(read);
    const holdForward = s.hold('ArrowRight', SCRUB_HOLD_MS);
    await sleep(SCRUB_HOLD_MS / 2);
    const during = await s.evaluate(read);
    await holdForward;
    await s.waitFor(`!${V}.seeking`, 'the video to settle after the fast-forward');
    const afterForward = await s.evaluate(read);
    s.fact('fastForward', {
      moved: afterForward.time - beforeForward.time,
      rateDuring: during.rate,
      rateAfter: afterForward.rate,
      pausedAfter: afterForward.paused,
    });
    s.step('hold Left for one second');
    await s.hold('ArrowLeft', SCRUB_HOLD_MS);
    await s.waitFor(`!${V}.seeking`, 'the video to settle after the rewind');
    const afterRewind = await s.evaluate(read);
    s.fact('rewind', { moved: afterRewind.time - afterForward.time, rateAfter: afterRewind.rate, pausedAfter: afterRewind.paused });
    s.step('open the wheel and press Right once');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    const beforeStep = await s.evaluate(`${V}.currentTime`);
    await s.key('ArrowRight');
    await s.waitFor(`!${V}.seeking && ${V}.currentTime > ${beforeStep}`, 'the frame step to land');
    s.fact('wheelStep', await s.evaluate(`({ moved: ${V}.currentTime - ${beforeStep}, wheelOpen: Boolean(${S}.wheel()), paused: ${V}.paused })`));
  },

  /** The guide docks beside the video by default and never covers it; the dock button lets it float. */
  async guide_dock(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    const layout = `(() => {
      const box = (id) => { const b = document.getElementById(id).getBoundingClientRect(); return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width }; };
      const transport = document.querySelector('.transport').getBoundingClientRect();
      return { stage: box('stage'), guide: box('guide'), theater: box('theater'), transportBottom: transport.bottom,
        viewportHeight: window.innerHeight, photos: document.querySelectorAll('#guide-images img').length,
        position: getComputedStyle(document.getElementById('guide')).position,
        dock: document.body.dataset.guideDock, pressed: document.getElementById('guide-dock').getAttribute('aria-pressed') };
    })()`;
    const widthBefore = await s.evaluate(`document.getElementById('stage').getBoundingClientRect().width`);
    s.step('open the guide with I');
    await s.key('i');
    await s.waitFor(`!document.getElementById('guide').hidden && document.querySelectorAll('#guide-images img').length > 0`, 'the guide to open with its photos');
    s.fact('docked', { ...(await s.evaluate(layout)), widthBefore });
    s.step('click the dock button');
    await clickElement(s, '#guide-dock');
    await s.waitFor(`document.body.dataset.guideDock === 'floating'`, 'the guide to float');
    s.fact('floating', await s.evaluate(layout));
    s.step('reload');
    await reloadApp(s, 'the reloaded page');
    s.fact('afterReload', await s.evaluate(`document.body.dataset.guideDock`));
  },

  /** With the guide open, the lit wheel species shows in it; a closed guide stays closed; logged species lead the outer ring. */
  async wheel_follows_guide(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    const guideCode = `document.getElementById('guide-species').value`;
    s.step('open the guide, then the wheel');
    await s.key('i');
    await s.waitFor(`!document.getElementById('guide').hidden`, 'the guide to open');
    await clickPicture(s, 0.30, 0.40);
    const wheel = await waitWheel(s, 'new');
    s.step('point at the slice of key 2');
    const second = wheel.inner.find((item) => item.slot === '2');
    await s.mouse('mouseMoved', second.x, second.y, { buttons: 0 });
    await s.waitFor(`${guideCode} === ${JSON.stringify(second.code)}`, 'the guide to show the species of key 2');
    s.fact('afterHover', { expected: second.code, guide: await s.evaluate(guideCode) });
    s.step('press Tab');
    await s.key('Tab');
    const lit = await s.evaluate(`${S}.wheel().highlighted`);
    await s.waitFor(`${guideCode} === ${JSON.stringify(lit)}`, 'the guide to follow the Tab highlight');
    s.fact('afterTab', { lit, guide: await s.evaluate(guideCode) });
    s.step('type saur, press Enter, close the guide');
    for (const name of ['s', 'a', 'u', 'r']) {
      await s.key(name);
    }
    await s.key('Enter');
    await waitRows(s, 1);
    await clickElement(s, '#guide-close');
    await s.waitFor(`document.getElementById('guide').hidden`, 'the guide to close');
    s.step('open the wheel again');
    await clickPicture(s, 0.65, 0.60);
    const again = await waitWheel(s, 'new');
    const first = again.outer.find((item) => item.slot === 0);
    s.fact('seenOrder', {
      first: first ? first.code : null,
      label: await s.evaluate(`(() => { const g = document.querySelector('#wheel .wheel-slice[data-ring="outer"][data-code="SAUR"]'); return g ? g.textContent : null; })()`),
    });
    s.step('point at key 2 with the guide closed');
    const secondAgain = again.inner.find((item) => item.slot === '2');
    await s.mouse('mouseMoved', secondAgain.x, secondAgain.y, { buttons: 0 });
    await sleep(GUIDE_STAYS_CLOSED_MS);
    s.fact('closedGuide', { hidden: await s.evaluate(`document.getElementById('guide').hidden`) });
  },

  /**
   * A sponge picked at about 5 s of the pan clip gets a track from the server; its mark rides
   * along the track while the clip plays, and a click on the moving mark opens its edit wheel.
   */
  async pan_tracking(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.keyPan, PAN_START_SECONDS);
    s.step('play to the pick time and click the middle of the picture');
    await s.waitFor(`${V}.currentTime >= ${PAN_PICK_SECONDS}`, 'the pan clip to reach the pick time');
    await clickPicture(s, 0.5, 0.5);
    await waitWheel(s, 'new');
    await s.key('1');
    const [row] = await waitRows(s, 1);
    s.fact('row', row);
    s.step('wait for the track of the pick');
    await s.waitFor(`${S}.state.tracks['${row.ID}'] && ${S}.state.trackPending.length === 0`, 'the track to arrive', VIDEO_WAIT_MS);
    s.fact('track', await s.evaluate(`${S}.state.tracks['${row.ID}']`));
    s.step('play on and read the mark as drawn 2 s after the pick');
    await playOn(s, 'the pan clip to play on after the pick');
    await s.waitFor(`${V}.currentTime >= ${Number(row.TimestampSeconds) + PAN_LATER_SECONDS}`, 'the clip to pass 2 s after the pick');
    const readMark = `(() => {
      const drawn = ${S}.drawn();
      const mark = drawn.marks.find((item) => item.id === '${row.ID}');
      return { time: drawn.time, mark: mark || null, playing: !${V}.paused };
    })()`;
    s.fact('drawnLater', await s.evaluate(readMark));
    s.step('click the moving mark');
    const target = await s.evaluate(readMark);
    if (!target.mark || !target.playing) {
      throw new Error(`the tracked mark is not drawn on the playing clip: ${JSON.stringify(target)}`);
    }
    await s.click(target.mark.x, target.mark.y);
    const wheel = await waitWheel(s, 'edit');
    s.fact('clickedMark', {
      target,
      wheel: { mode: wheel.mode, rowId: wheel.rowId },
      paused: await s.evaluate(`${V}.paused`),
    });
    await s.key('Escape');
    await s.waitFor(`!${S}.wheel()`, 'the edit wheel to close');
  },

  /**
   * In the edit wheel, one Backspace arms the deletion; another key, a pointer move to another
   * slice, or 3 s disarm it, and Escape deletes nothing. Two presses delete the row: the first
   * deletion clicks a tracked mark while the pan clip plays, the second a fresh mark on the
   * paused picture.
   */
  /** Quick Backspace taps that clear a filter in a saved mark's wheel never delete the mark. */
  async backspace_burst_keeps_row(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('save a sighting, then open its edit wheel');
    await clickPicture(s, 0.5, 0.5);
    await waitWheel(s, 'new');
    await s.key('1');
    await waitRows(s, 1);
    await clickPicture(s, 0.5, 0.5);
    await waitWheel(s, 'edit');
    s.step('type ac, then tap Backspace four times 50 ms apart');
    await s.key('a');
    await s.key('c');
    for (let tap = 0; tap < 4; tap += 1) {
      await s.key('Backspace');
      await sleep(BURST_GAP_MS);
    }
    await sleep(BURST_SETTLE_MS);
    s.fact('afterBurst', await s.evaluate(`({
      rows: ${S}.state.rows.length,
      wheel: Boolean(${S}.wheel()),
      deletes: window.__e2e.log.filter((entry) => entry.method === 'DELETE').length,
    })`));
  },

  async delete_from_wheel(s, config) {
    const markOf = (id) => `(() => {
      const drawn = ${S}.drawn();
      const mark = drawn.marks.find((item) => item.id === '${id}');
      return { time: drawn.time, mark: mark || null, playing: !${V}.paused };
    })()`;
    const deletes = `window.__e2e.log.filter((entry) => entry.method === 'DELETE').map((entry) => ({ id: entry.body && entry.body.deleted && entry.body.deleted.ID, status: entry.status }))`;
    const after = `({ rows: ${S}.state.rows.map((row) => row.ID), paused: ${V}.paused, toast: ${S}.state.lastToast, deletes: ${deletes} })`;
    const pressTwice = async () => {
      await s.key('Backspace');
      await s.waitFor(`${S}.wheel() && ${S}.wheel().armed`, 'the first press to arm the deletion');
      await sleep(DELETE_CONFIRM_WAIT_MS);
      await s.key('Backspace');
      await s.waitFor(`!${S}.wheel() && ${S}.state.rows.length === 0`, 'the second press to delete the row');
      await s.waitFor(`/^Deleted /.test(${S}.state.lastToast ? ${S}.state.lastToast.message : '')`, 'the deletion toast');
    };
    await loadApp(s);
    await openVideo(s, config, config.keyPan, DELETE_START_SECONDS);
    s.step('pick the middle of the picture at about 3 s');
    await s.waitFor(`${V}.currentTime >= ${DELETE_PICK_SECONDS}`, 'the clip to reach the pick time');
    await clickPicture(s, 0.5, 0.5);
    await waitWheel(s, 'new');
    await s.key('1');
    const [first] = await waitRows(s, 1);
    s.fact('first', first);

    s.step('click the mark, press Backspace once, and see what disarms it');
    await clickPicture(s, 0.5, 0.5);
    const editWheel = await waitWheel(s, 'edit');
    const armedNow = `Boolean(${S}.wheel() && ${S}.wheel().armed)`;
    await s.key('Backspace');
    await s.waitFor(armedNow, 'the first press to arm the deletion');
    s.fact('armed', await s.evaluate(`({
      readout: document.querySelector('#wheel .wheel-readout').textContent,
      hub: document.querySelector('#wheel .wheel-hub').getAttribute('data-armed'),
      stroke: getComputedStyle(document.querySelector('#wheel .wheel-hub-ring')).stroke,
    })`));
    await s.key('Tab');
    const afterKey = await s.evaluate(armedNow);
    await s.key('Backspace');
    await s.waitFor(armedNow, 'a new press to arm the deletion');
    const slice = editWheel.inner.find((item) => item.slot === '2');
    await s.mouse('mouseMoved', slice.x, slice.y, { buttons: 0 });
    const afterPointer = await s.evaluate(armedNow);
    await s.key('Backspace');
    await s.waitFor(armedNow, 'a third press to arm the deletion');
    await sleep(DELETE_ARM_WAIT_MS);
    s.fact('disarmed', { afterKey, afterPointer, afterWait: await s.evaluate(armedNow) });
    s.step('press Backspace once more, then Escape');
    await s.key('Backspace');
    await s.waitFor(armedNow, 'the press before Escape to arm the deletion');
    await s.key('Escape');
    await s.waitFor(`!${S}.wheel()`, 'the wheel to close on Escape');
    await sleep(STILL_MS);
    s.fact('afterEscape', await s.evaluate(`({ rows: ${S}.state.rows.map((row) => row.ID), deletes: ${deletes}.length })`));

    s.step('wait for the track, replay, and delete the moving mark');
    await s.waitFor(`${S}.state.tracks['${first.ID}'] && ${S}.state.trackPending.length === 0`, 'the track to arrive', VIDEO_WAIT_MS);
    await s.evaluate(`(() => { ${V}.currentTime = ${DELETE_REPLAY_SECONDS}; return ${V}.play(); })()`);
    await s.waitFor(`!${V}.seeking && !${V}.paused && ${V}.currentTime >= ${DELETE_CLICK_SECONDS}`, 'the replay to pass the click time');
    const target = await s.evaluate(markOf(first.ID));
    if (!target.mark || !target.playing) {
      throw new Error(`the tracked mark is not drawn on the playing clip: ${JSON.stringify(target)}`);
    }
    await s.click(target.mark.x, target.mark.y);
    await waitWheel(s, 'edit');
    s.fact('playingClick', { target, paused: await s.evaluate(`${V}.paused`) });
    await pressTwice();
    s.fact('afterPlayingDelete', await s.evaluate(after));

    s.step('pick again on the paused picture and delete that mark');
    await clickPicture(s, 0.5, 0.5);
    await waitWheel(s, 'new');
    await s.key('2');
    const [second] = await waitRows(s, 1);
    s.fact('second', second);
    await clickPicture(s, 0.5, 0.5);
    await waitWheel(s, 'edit');
    await pressTwice();
    s.fact('afterPausedDelete', await s.evaluate(after));
  },

  /** Shift with a digit saves and plays on; Shift-click does the same for a second sponge. */
  async shift_digit_plays_on(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('click, then press Shift+1');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('1', { shift: true });
    s.fact('keydownSeen', await s.evaluate('window.__e2e.keys[window.__e2e.keys.length - 1]'));
    s.fact('afterShiftDigit', await waitRows(s, 1));
    await waitPlaying(s, 'the video to play on after Shift+1');
    s.fact('playingAfterDigit', await readAfterPick(s));
    s.step('click a second sponge, then Shift-click the slice of key 2');
    await clickPicture(s, 0.65, 0.60);
    const wheel = await waitWheel(s, 'new');
    await clickInner(s, wheel, '2', SHIFT);
    s.fact('afterShiftClick', await waitRows(s, 2));
    await waitPlaying(s, 'the video to play on after the Shift-click');
    s.fact('playingAfterClick', await readAfterPick(s));
  },

  /** Z pressed while the last pick is still on its way to the server removes that pick, not the one before. */
  async undo_during_save(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('save a first sighting with key 1');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('1');
    s.fact('first', (await waitRows(s, 1))[0]);
    await rewindIfLate(s);
    s.step('hold the next save, pick key 2, and press Z at once');
    await s.evaluate(`window.__e2e.delayNextPost(${HELD_SAVE_MS})`);
    await clickPicture(s, 0.60, 0.55);
    await waitWheel(s, 'new');
    await s.key('2');
    await s.key('z');
    await sleep(NOTICE_LOOK_MS);
    s.fact('whileWaiting', await s.evaluate(`({ rows: ${S}.state.rows.length, deletes: window.__e2e.log.filter((entry) => entry.method === 'DELETE').length })`));
    s.step('wait for the held save and the removal');
    await s.waitFor(`window.__e2e.log.some((entry) => entry.method === 'DELETE')`, 'the removal request', DEFAULT_WAIT_MS);
    await s.waitFor(`${S}.state.rows.length === 1`, 'one row left in the sightings list');
    s.fact('rows', await pageRows(s));
    s.fact('saves', await s.evaluate(`window.__e2e.log.filter((entry) => entry.url === '/api/observations').map((entry) => ({ status: entry.status, id: entry.body && entry.body.row && entry.body.row.ID, code: entry.body && entry.body.row && entry.body.row.SpeciesCode }))`));
    s.fact('deletes', await s.evaluate(`window.__e2e.log.filter((entry) => entry.method === 'DELETE').map((entry) => ({ url: entry.url, status: entry.status, id: entry.body && entry.body.deleted && entry.body.deleted.ID }))`));
    s.fact('toasts', await s.evaluate('window.__e2e.toasts'));
  },

  /** Z right after a pick whose save failed discards that pick and leaves the good sighting before it alone. */
  async undo_failed_pick(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('save a first sighting with key 1');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('1');
    s.fact('first', (await waitRows(s, 1))[0]);
    await rewindIfLate(s);
    s.step('fail the next save, pick key 2, and wait for Retry');
    await s.evaluate('window.__e2e.failPosts(1)');
    await clickPicture(s, 0.60, 0.55);
    await waitWheel(s, 'new');
    await s.key('2');
    await s.waitFor(`document.querySelector('#toasts .toast-action')`, 'the Retry button');
    s.step('press Z');
    await s.key('z');
    await s.waitFor(`window.__e2e.toasts.some((toast) => /^(Discarded|Removed)/.test(toast.text))`, 'Z to discard or remove something');
    await sleep(STILL_MS);
    s.fact('after', await s.evaluate(`({
      rows: ${S}.state.rows.map((row) => row.ID),
      retryButtons: document.querySelectorAll('#toasts .toast-action').length,
      deletes: window.__e2e.log.filter((entry) => entry.method === 'DELETE').length,
      leaveBlocked: window.__e2e.leaveBlocked(),
    })`));
    s.fact('toasts', await s.evaluate('window.__e2e.toasts'));
  },

  /** Z gives up after its wait limit when the last save never answers, and deletes nothing. */
  async undo_wait_limit(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('hold the next save for 12 s, pick key 1, and press Z at once');
    await s.evaluate(`window.__e2e.delayNextPost(${SLOW_SAVE_MS})`);
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('1');
    await s.key('z');
    await s.waitFor(`window.__e2e.toasts.some((toast) => /has not answered yet/.test(toast.text))`, 'Z to give up', SLOW_SAVE_WAIT_MS);
    s.step('wait for the slow save to land');
    s.fact('rows', await s.evaluate(`new Promise((resolve) => { const check = () => (${S}.state.rows.length ? resolve(${S}.state.rows.map((row) => row.ID)) : setTimeout(check, ${POLL_MS})); check(); })`));
    await sleep(STILL_MS);
    s.fact('deletes', await s.evaluate(`window.__e2e.log.filter((entry) => entry.method === 'DELETE').length`));
    s.fact('toasts', await s.evaluate('window.__e2e.toasts'));
  },

  /** A fault inside the pick, before the save goes out, releases the save so Z and leaving never wait for it. */
  async pick_fault_untracks(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('break the note box, then pick key 1');
    await s.evaluate(`Object.defineProperty(document.getElementById('note'), 'value', { configurable: true, get() { throw new Error('the note box could not be read'); }, set() {} })`);
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('1');
    await sleep(STILL_MS);
    s.fact('afterFault', await s.evaluate(`({
      leaveBlocked: window.__e2e.leaveBlocked(),
      wheel: Boolean(${S}.wheel()),
      posts: window.__e2e.log.filter((entry) => entry.url === '/api/observations').length,
      toast: ${S}.state.lastToast,
    })`));
    s.step('mend the note box and pick key 1 again');
    await s.evaluate(`delete document.getElementById('note').value`);
    await s.key('1');
    s.fact('rows', await waitRows(s, 1));
    s.fact('leaveBlockedAfter', await s.evaluate('window.__e2e.leaveBlocked()'));
  },

  /** A save that never reaches the server waits for Retry, blocks leaving the page, and saves once on Retry. */
  async retry_after_failure(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.fact('leaveBlockedAtStart', await s.evaluate('window.__e2e.leaveBlocked()'));
    s.step('fail the next save, then pick key 1');
    await s.evaluate('window.__e2e.failPosts(1)');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('1');
    await s.waitFor(`document.querySelector('#toasts .toast-action')`, 'the Retry button');
    s.fact('failed', await s.evaluate(`({
      rows: ${S}.state.rows.length,
      toast: ${S}.state.lastToast,
      retryButtons: document.querySelectorAll('#toasts .toast-action').length,
      leaveBlocked: window.__e2e.leaveBlocked(),
    })`));
    s.step('click Retry');
    await clickElement(s, '#toasts .toast-action');
    s.fact('rows', await waitRows(s, 1));
    await s.waitFor(`!document.querySelector('#toasts .toast-action')`, 'the Retry toast to leave');
    s.fact('afterRetry', await s.evaluate(`({ leaveBlocked: window.__e2e.leaveBlocked(), toast: ${S}.state.lastToast })`));
    s.fact('saves', await s.evaluate(`window.__e2e.log.filter((entry) => entry.url === '/api/observations').map((entry) => ({ status: entry.status, id: entry.body && entry.body.row && entry.body.row.ID }))`));
  },

  /** A right-click on the picture cancels and plays on; a click outside the wheel and the picture drops the mark and stays paused. */
  async cancel_paths(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('open the wheel, then right-click the picture away from it');
    await clickPicture(s, 0.15, 0.20);
    await waitWheel(s, 'new');
    await clickPicture(s, 0.95, 0.90, { button: 'right' });
    await s.waitFor(`!${S}.wheel()`, 'the wheel to close on the right-click');
    s.fact('afterRightClick', { wheel: await s.evaluate(`Boolean(${S}.wheel())`), pending: await s.evaluate(`Boolean(${S}.state.pending)`), mode: await s.evaluate(`${S}.state.mode`), clock: await clockDelta(s, STILL_MS) });
    await rewindIfLate(s);
    s.step('open the wheel, then click the tally panel');
    await clickPicture(s, 0.15, 0.20);
    await waitWheel(s, 'new');
    await clickElement(s, '#tally-total');
    await s.waitFor(`!${S}.wheel()`, 'the wheel to close on the outside click');
    s.fact('afterOutsideClick', { wheel: await s.evaluate(`Boolean(${S}.wheel())`), pending: await s.evaluate(`Boolean(${S}.state.pending)`), mode: await s.evaluate(`${S}.state.mode`), clock: await clockDelta(s, STILL_MS) });
    s.fact('rows', await pageRows(s));
  },

  /** A display fault after a good save is reported in the console and never offered for Retry. */
  async render_fault_after_save(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('break the list scroll, then pick key 1');
    await s.evaluate(`Element.prototype.scrollIntoView = () => { throw new Error('the list could not scroll'); }`);
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('1');
    await s.waitFor(`window.__e2e.log.some((entry) => entry.url === '/api/observations')`, 'the answer to the save');
    await sleep(NOTICE_LOOK_MS);
    s.fact('after', await s.evaluate(`({
      saves: window.__e2e.log.filter((entry) => entry.url === '/api/observations').map((entry) => entry.status),
      retryButtons: document.querySelectorAll('#toasts .toast-action').length,
      leaveBlocked: window.__e2e.leaveBlocked(),
      rows: ${S}.state.rows.length,
    })`));
  },

  /** Tab does nothing while the panel is hidden; with the panel shown it moves to the note, Enter comes back to the wheel, and the note rides on the pick. */
  async note_rides_on_pick(s, config) {
    await loadApp(s);
    await openVideo(s, config, config.key2024);
    s.step('open the wheel and press Tab with the panel hidden');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await s.key('Tab');
    s.fact('hiddenTab', await s.evaluate(`({ deck: document.body.dataset.deck, focus: document.activeElement && document.activeElement.id, wheel: Boolean(${S}.wheel()), highlighted: ${S}.wheel() && ${S}.wheel().highlighted })`));
    s.step('press Escape, then P to show the panel');
    await s.key('Escape');
    await s.waitFor(`!${S}.wheel()`, 'the wheel to close on Escape');
    await s.key('p');
    await s.waitFor(`document.body.dataset.deck === 'shown'`, 'the panel to show');
    await stablePicture(s);
    s.step('open the wheel and click the note box with the panel shown');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await clickElement(s, '#note');
    s.fact('afterNoteClick', await s.evaluate(`({ focus: document.activeElement && document.activeElement.id, wheel: Boolean(${S}.wheel()) })`));
    s.step('type the note and press Enter');
    for (const name of ['b', 'i', 'g', 'Space', 'o', 'n', 'e']) {
      await s.key(name);
    }
    await s.key('Enter');
    s.fact('afterEnter', await s.evaluate(`({ focus: document.activeElement && document.activeElement.id, wheel: Boolean(${S}.wheel()), note: document.getElementById('note').value, rows: ${S}.state.rows.length, paused: ${V}.paused })`));
    s.step('press 1');
    await s.key('1');
    s.fact('rows', await waitRows(s, 1));
  },

  /** A fresh profile hides the panel under the video; P and the panel button show and hide it, and a reload keeps the choice. */
  async deck_toggle(s) {
    await loadApp(s);
    s.step('read the fresh profile');
    s.fact('fresh', await readDeck(s));
    s.step('press P');
    await s.key('p');
    s.fact('afterP', await readDeck(s));
    s.step('reload the page');
    await reloadApp(s, 'the reloaded page');
    s.fact('afterReload', await readDeck(s));
    s.step('click the panel button');
    await clickElement(s, '#deck-toggle');
    s.fact('afterButton', await readDeck(s));
  },

  /** Play part of a clip, see Done warn about the rest, and read the coverage grid. */
  async coverage_grid(s, config) {
    const [wideW, wideH] = SIZE_PRESETS.wide;
    const [narrowW, narrowH] = SIZE_PRESETS.narrow;
    const readGrid = () => s.evaluate(`[...document.querySelectorAll('#coverage-grid tbody tr')].map((row) => ({
      key: row.dataset.key,
      video: row.querySelector('th').textContent,
      watched: row.querySelector('.coverage-watched').textContent,
      unlabeled: row.querySelector('.coverage-number').textContent,
      status: row.querySelector('.coverage-status').textContent,
      cells: [...row.querySelectorAll('.coverage-cell')].map((cell) => [cell.querySelector('.coverage-count').textContent, cell.dataset.state]),
    }))`);
    await loadApp(s);
    s.step('set the 1280 x 800 viewport');
    await s.call('Emulation.setDeviceMetricsOverride', { width: wideW, height: wideH, deviceScaleFactor: 1, mobile: false });
    await openVideo(s, config, config.key2024, 0);
    await stablePicture(s);
    s.step('play with the coverage grid open over the video for a stretch');
    await s.waitFor(`${V}.currentTime > 2`, 'the clip to play past 2 s');
    await clickElement(s, '#coverage-toggle');
    s.fact('openedAt', await s.evaluate(`${V}.currentTime`));
    await s.waitFor(`${V}.currentTime > 5`, 'the clip to play past 5 s');
    await s.key('Escape');
    s.fact('closedAt', await s.evaluate(`${V}.currentTime`));
    await s.waitFor(`${V}.currentTime > 6.5`, 'the clip to play past 6.5 s');
    await s.evaluate(`${V}.pause()`);
    await s.waitFor(`${V}.paused`, 'the video to pause');
    await sleep(STILL_MS);
    s.fact('watched', await s.evaluate(`fetch('/api/videos/watched', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Screener': '1' },
      body: JSON.stringify({ key: ${JSON.stringify(config.key2024)}, ranges: [], duration: null }) })
      .then((answer) => answer.json())`));
    s.step('press D');
    await s.key('d');
    await s.waitFor(`document.querySelector('.toast-action')`, 'the warning about unwatched time');
    s.fact('warning', await s.evaluate(`${S}.state.lastToast`));
    s.fact('statusAfterWarning', await s.evaluate(`${S}.state.videoStatus`));
    s.step('open the coverage grid');
    await clickElement(s, '#coverage-toggle');
    await s.waitFor(`document.querySelector('#coverage-grid tbody tr')`, 'the coverage grid');
    s.fact('headers', await s.evaluate(`[...document.querySelectorAll('.coverage-species')].map((cell) => [cell.textContent, cell.dataset.tier])`));
    s.fact('grid', await readGrid());
    await s.shot('coverage-1280x800');
    s.step('set the 946 x 700 viewport');
    await s.call('Emulation.setDeviceMetricsOverride', { width: narrowW, height: narrowH, deviceScaleFactor: 1, mobile: false });
    await sleep(LAYOUT_SETTLE_MS);
    s.fact('narrowOverflow', await s.evaluate(`document.getElementById('coverage').scrollWidth > document.getElementById('coverage').clientWidth`));
    await s.shot('coverage-946x700');
    s.step('press Escape');
    await s.key('Escape');
    s.fact('closed', await s.evaluate(`document.getElementById('coverage').hidden`));
    s.step('press D with the grid open');
    await clickElement(s, '#coverage-toggle');
    await s.waitFor(`document.querySelector('#coverage-grid tbody tr') && !document.getElementById('coverage').hidden`, 'the grid to open again');
    await s.key('d');
    await s.waitFor(`document.getElementById('coverage').hidden && document.querySelector('.toast-action')`, 'the grid to close for the warning');
    s.step('mark done anyway');
    await clickElement(s, '.toast-action');
    await s.waitFor(`${S}.state.videoStatus === 'done'`, 'the video to read done');
    s.fact('statusAfterAction', await s.evaluate(`${S}.state.videoStatus`));
  },

  /** Read the key colors with the default pins and with a Part 3 species on key 9, and photograph them. */
  async pin_colors(s, config) {
    const readKeys = () => s.evaluate(`[...document.querySelectorAll('.pin-slot')].map((slot) => ({
      slot: slot.dataset.slot,
      code: slot.dataset.code,
      tier: slot.dataset.tier,
      border: getComputedStyle(slot.querySelector('.pin-choose')).borderTopColor,
      ink: getComputedStyle(slot.querySelector('.pin-code')).color,
      label: slot.querySelector('.pin-choose').getAttribute('aria-label'),
    }))`);
    const [wideW, wideH] = SIZE_PRESETS.wide;
    const [narrowW, narrowH] = SIZE_PRESETS.narrow;
    await loadApp(s);
    s.step('set the 1280 x 800 viewport and show the keys');
    await s.call('Emulation.setDeviceMetricsOverride', { width: wideW, height: wideH, deviceScaleFactor: 1, mobile: false });
    if (!(await readDeck(s)).shown) await s.key('p');
    await openVideo(s, config, config.key2024);
    await stablePicture(s);
    s.fact('defaults', await readKeys());
    s.fact('colors', await s.evaluate(`(() => {
      const probe = document.createElement('i');
      document.body.append(probe);
      const read = (name) => { probe.style.color = 'var(' + name + ')'; return getComputedStyle(probe).color; };
      const colors = { green: read('--green'), pink: read('--mark'), line: read('--line-strong') };
      probe.remove();
      return colors;
    })()`));
    await s.shot('keys-1280x800');
    s.step('hover over key 1');
    const keyOne = await s.evaluate(`(() => { const box = document.querySelector('.pin-slot[data-slot="1"] .pin-choose').getBoundingClientRect(); return { x: box.x + box.width / 2, y: box.y + box.height / 2 }; })()`);
    await s.mouse('mouseMoved', keyOne.x, keyOne.y);
    await sleep(STILL_MS);
    s.fact('hoverFilter', await s.evaluate(`getComputedStyle(document.querySelector('.pin-slot[data-slot="1"] .pin-choose')).filter`));
    await s.mouse('mouseMoved', 1, 1);
    s.step('look while marking');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    s.fact('marking', await readKeys());
    await s.key('Escape');
    s.step('set the 946 x 700 viewport');
    await s.call('Emulation.setDeviceMetricsOverride', { width: narrowW, height: narrowH, deviceScaleFactor: 1, mobile: false });
    await sleep(LAYOUT_SETTLE_MS);
    await s.shot('keys-946x700');
    s.step('pin a Part 3 species to key 9');
    await s.evaluate(`fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Screener': '1' },
      body: JSON.stringify({ pins: { ...${S}.state.pins, 9: 'SAUR' } }) }).then((answer) => answer.status)`);
    await reloadApp(s, 'the page with the new pin');
    s.fact('repinned', await readKeys());
  },

  /** Screenshots: the wheel at two window sizes and in full screen, a Later mark, and a flash label. */
  async screenshots(s, config) {
    const [wideW, wideH] = SIZE_PRESETS.wide;
    const [narrowW, narrowH] = SIZE_PRESETS.narrow;
    await loadApp(s);
    s.step('set the 1280 x 800 viewport');
    await s.call('Emulation.setDeviceMetricsOverride', { width: wideW, height: wideH, deviceScaleFactor: 1, mobile: false });
    await openVideo(s, config, config.key2024);
    await stablePicture(s);
    s.step('wheel at 1280 x 800');
    await clickPicture(s, 0.30, 0.40);
    await waitWheel(s, 'new');
    await sleep(WHEEL_SETTLE_MS);
    s.fact('wide', { view: await s.evaluate('[innerWidth, innerHeight]'), opacity: await s.evaluate(`getComputedStyle(document.getElementById('wheel')).opacity`), placement: await wheelPlacement(s) });
    await s.shot('wheel-1280x800');
    s.step('flash label after a pick');
    await s.key('1');
    const flashSpot = await s.evaluate(`(() => { const p = ${S}.pictureRect(); return { x: p.x + 0.30 * p.w, y: p.y + 0.40 * p.h }; })()`);
    await waitRows(s, 1);
    await sleep(FLASH_LOOK_MS);
    s.fact('flash', { ink: await overlayInk(s, flashSpot, 60), paused: await s.evaluate(`${V}.paused`) });
    await s.shot('flash-label');
    s.step('save a Later point');
    await playOn(s, 'the video to play on after the flash label');
    await sleep(1800);
    await rewindIfLate(s);
    const laterSpot = await clickPicture(s, 0.65, 0.30);
    await waitWheel(s, 'new');
    await s.key('Space');
    await waitRows(s, 2);
    await sleep(FLASH_LOOK_MS);
    s.fact('laterFlash', { ink: await overlayInk(s, laterSpot, 60) });
    await s.shot('later-flash');
    s.step('pause on the Later mark');
    await playOn(s, 'the video to play on after the Later point');
    await sleep(1800);
    const later = (await pageRows(s)).find((row) => row.LabelStatus === 'unlabeled');
    await clickElement(s, `#sightings .sighting[data-id="${later.ID}"] .sighting-jump`);
    await waitWheel(s, 'edit');
    await s.key('Space');
    await s.waitFor(`!${S}.wheel() && ${V}.paused && !${V}.seeking`, 'the video to stay paused on the Later point');
    await sleep(WHEEL_SETTLE_MS);
    const pausedSpot = await s.evaluate(`(() => { const p = ${S}.pictureRect(); return { x: p.x + 0.65 * p.w, y: p.y + 0.30 * p.h }; })()`);
    s.fact('laterMark', { ink: await overlayInk(s, pausedSpot, 30) });
    await s.shot('later-mark');
    s.step('wheel at 946 x 700');
    await s.call('Emulation.setDeviceMetricsOverride', { width: narrowW, height: narrowH, deviceScaleFactor: 1, mobile: false });
    await sleep(LAYOUT_SETTLE_MS);
    await stablePicture(s);
    await clickPicture(s, 0.45, 0.65);
    await waitWheel(s, 'new');
    await sleep(WHEEL_SETTLE_MS);
    s.fact('narrow', { view: await s.evaluate('[innerWidth, innerHeight]'), opacity: await s.evaluate(`getComputedStyle(document.getElementById('wheel')).opacity`), placement: await wheelPlacement(s) });
    await s.shot('wheel-946x700');
    await s.key('Escape');
    await s.waitFor(`!${S}.wheel()`, 'the wheel to close');
    s.step('wheel in full screen');
    await s.call('Emulation.clearDeviceMetricsOverride');
    await sleep(LAYOUT_SETTLE_MS);
    await s.key('f');
    await s.waitFor(`document.fullscreenElement && document.fullscreenElement.id === 'theater'`, 'full screen to start');
    await sleep(LAYOUT_SETTLE_MS);
    await stablePicture(s);
    await clickPicture(s, 0.40, 0.50);
    await waitWheel(s, 'new');
    await sleep(WHEEL_SETTLE_MS);
    s.fact('fullscreenShot', { view: await s.evaluate('[innerWidth, innerHeight]'), opacity: await s.evaluate(`getComputedStyle(document.getElementById('wheel')).opacity`), placement: await wheelPlacement(s) });
    await s.shot('wheel-fullscreen');
    await s.key('Escape');
    await s.waitFor(`!${S}.wheel()`, 'the wheel to close in full screen');
    if (await s.evaluate('Boolean(document.fullscreenElement)')) {
      await s.key('f');
    }
    await s.waitFor('!document.fullscreenElement', 'full screen to end');
  },
};

// ----------------------------------------------------------------------- main

/**
 * Collect page state for a failure report.
 * @param {Session} s The session.
 * @returns {Promise<object>} The video, mode, wheel, toast, and row count, or the reason it could not be read.
 */
async function pageDump(s) {
  try {
    return await s.evaluate(`({
      key: ${S}.state.key, mode: ${S}.state.mode, rows: ${S}.state.rows.length, toast: ${S}.state.lastToast,
      wheel: ${S}.wheel() && { mode: ${S}.wheel().mode, query: ${S}.wheel().query },
      video: { paused: ${V}.paused, time: ${V}.currentTime, seeking: ${V}.seeking, ended: ${V}.ended, ready: ${V}.readyState },
      view: [innerWidth, innerHeight], fullscreen: document.fullscreenElement && document.fullscreenElement.id,
    })`);
  } catch (error) {
    return { unreadable: String(error.message) };
  }
}

/**
 * Run the scenario named on the command line and print the result line.
 * @returns {Promise<void>} Settles after the line was written.
 */
async function main() {
  const started = Date.now();
  const config = JSON.parse(CONFIG_JSON);
  const result = { scenario: SCENARIO, ok: false, step: 'connect', error: null, facts: {}, shots: [], pageErrors: [], consoleErrors: [], elapsedMs: 0 };
  let session = null;
  const limit = setTimeout(() => {
    process.stdout.write(`${JSON.stringify({ ...result, error: `the scenario ran past ${SCENARIO_LIMIT_MS} ms`, elapsedMs: Date.now() - started })}\n`);
    process.exit(1);
  }, SCENARIO_LIMIT_MS);
  try {
    if (!SCENARIOS[SCENARIO]) {
      throw new Error(`unknown scenario ${JSON.stringify(SCENARIO)}; known: ${Object.keys(SCENARIOS).join(', ')}`);
    }
    mkdirSync(OUT_DIR, { recursive: true });
    rmSync(path.resolve(OUT_DIR, `${SCENARIO}-error.png`), { force: true });
    const cdp = await Cdp.connect(WS_URL);
    session = await Session.open(cdp);
    await SCENARIOS[SCENARIO](session, config);
    result.ok = true;
  } catch (error) {
    result.error = String((error && error.message) || error);
    if (session) {
      result.facts.dump = await pageDump(session);
      try {
        await session.shot(`${SCENARIO}-error`);
      } catch (shotError) {
        result.facts.shotError = String(shotError.message);
      }
    }
  }
  if (session) {
    result.step = session.currentStep;
    result.facts = { ...session.facts, ...result.facts };
    result.shots = session.shots;
    result.pageErrors = session.pageErrors;
    result.consoleErrors = session.consoleErrors;
    try {
      await session.call('Emulation.clearDeviceMetricsOverride');
      await session.cdp.send('Target.closeTarget', { targetId: session.targetId });
    } catch (closeError) {
      result.facts.closeError = String(closeError.message);
    }
  }
  clearTimeout(limit);
  result.elapsedMs = Date.now() - started;
  process.stdout.write(`${JSON.stringify(result)}\n`);
  process.exit(result.ok ? 0 : 1);
}

main();
