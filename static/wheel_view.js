/**
 * The species wheel on screen: an SVG ring menu that floats over the video.
 *
 * The hub is a see-through hole over the mark, the inner ring holds the pinned
 * species in key order, and the outer ring shows 16 of the other species and spins
 * with the scroll wheel. A readout chip above the wheel names the species under the
 * pointer, because hover tooltips would cover the sponges. All layout and hit testing
 * come from wheel.js; this file builds the DOM, follows the pointer, and calls the
 * handlers that app.js passes in. Species names reach the DOM through textContent only.
 */

import { PHASE_LEGEND, PIN_KEYS, speciesTier } from './geometry.js';
import {
  cycleHighlight,
  WHEEL,
  deleteText,
  hoverHighlight,
  hubText,
  innerItems,
  litTarget,
  outerWindow,
  restingText,
  sliceName,
  sliceCenter,
  spinDelta,
  spinOffset,
  wheelMatches,
  zoneAt,
} from './wheel.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Room around the outer ring for the seam tick and the outlines, in wheel units. */
const RING_PAD = 12;
/** The square the SVG draws in, centered on the wheel center, in wheel units. */
const HALF_BOX = WHEEL.outerRadius + RING_PAD;
/**
 * Distance from the center to the labels: the middle of the outer ring, and a little
 * outside the middle of the inner ring, where its wedges are wider.
 */
const INNER_LABEL_OUTWARD = 4;
const INNER_LABEL_RADIUS = (WHEEL.hubRadius + WHEEL.innerRadius) / 2 + INNER_LABEL_OUTWARD;
const OUTER_LABEL_RADIUS = (WHEEL.innerRadius + WHEEL.outerRadius) / 2;
/** Vertical offsets of the label lines from the label center, in wheel units. */
const INNER_KEY_DY = -13;
const INNER_CODE_DY = 1;
const INNER_NAME_DY = 13;
const OUTER_CODE_DY = -3;
const OUTER_NAME_DY = 9;
/** The hub label baseline, just under the ring of the mark. */
const HUB_LABEL_Y = 29;
/** The longest epithet on an inner slice: eight pins leave room for eleven letters, more pins for eight. */
const INNER_NAME_CHARS = 14;
const INNER_NAME_CHARS_CROWDED = 11;
const CROWDED_INNER_COUNT = 8;
/** The longest epithet on an outer slot. */
const OUTER_NAME_CHARS = 12;
/** The seam tick runs from just inside the outer edge to just outside it. */
const SEAM_INNER = WHEEL.outerRadius - 6;
const SEAM_OUTER = WHEEL.outerRadius + 9;
/** The leader line stops this many screen pixels short of the mark, at the mark's ring. */
const LEADER_GAP_PX = 13;
/** The readout chip sits this far above the wheel, and flips below when the top has no room. */
const READOUT_GAP_PX = 8;
const READOUT_HEIGHT_PX = 30;
/** Scroll handling: one slot per 40 pixels, at most three slots per event. */
const SCROLL_STEP_PX = 40;
const MAX_STEPS_PER_EVENT = 3;
const LINE_HEIGHT_PX = 16;
const DOM_DELTA_LINE = 1;
const DOM_DELTA_PAGE = 2;
/** The outer ring turns into place over this many milliseconds after a spin. */
const SPIN_MS = 120;
/** A spin animation turns at most this many slots, so a long jump stays quick to read. */
const MAX_ANIMATED_SLOTS = 3;
const MOUSE_PRIMARY = 0;
/** One full turn in degrees. */
const TURN_DEG = 360;

/** The hub actions: Review for a new mark, Unlabel for a labeled row, Keep for a review point. */
const HUB_LABELS = Object.freeze(['Review', 'Unlabel', 'Keep']);

/**
 * Build an SVG element.
 * @param {string} tag The tag name.
 * @param {Object<string, string|number>} [attrs] Attributes to set.
 * @returns {SVGElement} The new element.
 */
function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) {
    node.setAttribute(name, String(value));
  }
  return node;
}

/**
 * Build an HTML span with text.
 * @param {string} className The class name.
 * @param {string} text The text, set through textContent.
 * @returns {HTMLSpanElement} The span.
 */
function span(className, text) {
  const node = document.createElement('span');
  node.className = className;
  node.textContent = text;
  return node;
}

/**
 * Find a point on a circle by clock angle.
 * @param {number} radius The circle radius in wheel units.
 * @param {number} degrees The angle clockwise from 12 o'clock.
 * @returns {string} "x y" for an SVG path.
 */
function polarText(radius, degrees) {
  const angle = (degrees * Math.PI) / 180;
  return `${(radius * Math.sin(angle)).toFixed(3)} ${(-radius * Math.cos(angle)).toFixed(3)}`;
}

/**
 * Trace one wedge of a ring, or the whole ring when the ring has a single slice.
 * @param {number} index The slice index, 0 centered at 12 o'clock.
 * @param {number} count The number of slices in the ring.
 * @param {number} inner The inner radius in wheel units.
 * @param {number} outer The outer radius in wheel units.
 * @returns {string} The SVG path data.
 */
function wedgePath(index, count, inner, outer) {
  if (count <= 1) {
    return `M ${polarText(outer, 0)} A ${outer} ${outer} 0 1 1 ${polarText(outer, 180)} `
      + `A ${outer} ${outer} 0 1 1 ${polarText(outer, 0)} Z `
      + `M ${polarText(inner, 0)} A ${inner} ${inner} 0 1 0 ${polarText(inner, 180)} `
      + `A ${inner} ${inner} 0 1 0 ${polarText(inner, 0)} Z`;
  }
  const width = TURN_DEG / count;
  const start = (index - 0.5) * width;
  const end = (index + 0.5) * width;
  const large = width > TURN_DEG / 2 ? 1 : 0;
  return `M ${polarText(outer, start)} A ${outer} ${outer} 0 ${large} 1 ${polarText(outer, end)} `
    + `L ${polarText(inner, end)} A ${inner} ${inner} 0 ${large} 0 ${polarText(inner, start)} Z`;
}

/**
 * Normalize a wheel event to pixels along the axis the hand moved most.
 * @param {WheelEvent} event The wheel event.
 * @param {number} pageHeight The height of one page in pixels.
 * @returns {number} The distance in pixels; positive scrolls down or right.
 */
function wheelPixels(event, pageHeight) {
  const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
  if (event.deltaMode === DOM_DELTA_LINE) {
    return delta * LINE_HEIGHT_PX;
  }
  if (event.deltaMode === DOM_DELTA_PAGE) {
    return delta * pageHeight;
  }
  return delta;
}

/**
 * Tell whether the person asked the system for less motion.
 * @returns {boolean} True when prefers-reduced-motion is set.
 */
function prefersLessMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * Build the species wheel inside a layer and return its controls.
 * @param {HTMLElement} layer A viewport-sized layer (position fixed, inset 0, no pointer
 *   events) inside the full-screen element; only the wheel itself takes pointer events.
 * @param {{onPick: Function, onHubPick: Function, onCancel: Function, onHighlight: ?Function}} handlers
 *   onPick(code, {shift}) runs when a slice is clicked, onHubPick({shift}) when the hub is
 *   clicked, onCancel() on a right-click. shift is true when Shift was held. The optional
 *   onHighlight(code) runs whenever the lit species changes (pointer or keys), with null when
 *   no species is lit.
 * @returns {{open: Function, close: Function, isOpen: Function, relayout: Function,
 *   spin: Function, setQuery: Function, moveHighlight: Function, pickHighlighted: Function,
 *   armDelete: Function, disarmDelete: Function, deleteArmed: Function, snapshot: Function}}
 *   The wheel controls.
 * @throws {TypeError} When the layer is not an element or a handler is missing.
 */
export function createWheel(layer, handlers) {
  if (!(layer instanceof HTMLElement)) {
    throw new TypeError('createWheel: layer must be an HTML element');
  }
  for (const name of ['onPick', 'onHubPick', 'onCancel']) {
    if (!handlers || typeof handlers[name] !== 'function') {
      throw new TypeError(`createWheel: handlers.${name} must be a function`);
    }
  }
  if (handlers.onHighlight !== undefined && typeof handlers.onHighlight !== 'function') {
    throw new TypeError('createWheel: handlers.onHighlight must be a function when given');
  }

  const root = document.createElement('div');
  root.id = 'wheel';
  root.className = 'wheel';
  root.hidden = true;
  root.setAttribute('role', 'menu');
  root.setAttribute('aria-label', 'Species wheel');

  const readout = document.createElement('div');
  readout.className = 'wheel-readout';
  readout.style.minHeight = `${READOUT_HEIGHT_PX}px`;
  // Screen readers hear the readout only after a key changes it, never on every hover.
  const announcer = document.createElement('div');
  announcer.className = 'visually-hidden';
  announcer.setAttribute('aria-live', 'polite');

  const box = HALF_BOX * 2;
  const canvas = svg('svg', {
    class: 'wheel-svg',
    viewBox: `${-HALF_BOX} ${-HALF_BOX} ${box} ${box}`,
    overflow: 'visible',
  });
  const leader = svg('g', { class: 'wheel-leader', hidden: '' });
  const leaderLines = [svg('line', { class: 'wheel-leader-under' }), svg('line', { class: 'wheel-leader-line' })];
  leader.append(...leaderLines);
  const outerRing = svg('g', { class: 'wheel-ring wheel-ring-outer' });
  const seam = svg('path', {
    class: 'wheel-seam',
    d: `M ${polarText(SEAM_INNER, (Math.floor(WHEEL.outerSlots / 2) + 0.5) * (TURN_DEG / WHEEL.outerSlots))} `
      + `L ${polarText(SEAM_OUTER, (Math.floor(WHEEL.outerSlots / 2) + 0.5) * (TURN_DEG / WHEEL.outerSlots))}`,
  });
  const innerRing = svg('g', { class: 'wheel-ring wheel-ring-inner' });
  const hub = svg('g', { class: 'wheel-hub', role: 'menuitem' });
  const hubHit = svg('circle', { class: 'wheel-hub-hit', r: WHEEL.hubRadius });
  const hubRing = svg('circle', { class: 'wheel-hub-ring', r: WHEEL.hubRadius - 1 });
  const hubLabel = svg('text', { class: 'wheel-hub-label', x: 0, y: HUB_LABEL_Y });
  hub.append(hubHit, hubRing, hubLabel);
  canvas.append(outerRing, seam, innerRing, hub, leader);
  const legend = document.createElement('div');
  legend.className = 'phase-legend wheel-legend';
  for (const { tier, label } of PHASE_LEGEND) {
    const chip = document.createElement('span');
    chip.className = 'phase-chip';
    chip.dataset.tier = tier;
    chip.textContent = label;
    legend.append(chip);
  }
  // The corner of the wheel's square lies outside the circle, so the legend covers no slice.
  root.append(readout, announcer, canvas, legend);
  layer.append(root);

  /** Everything the open wheel shows. */
  const view = {
    open: false,
    mode: 'new',
    rowId: null,
    hubLabel: 'Review',
    current: null,
    species: [],
    names: new Map(),
    parts: new Map(),
    inner: [],
    pinOf: new Map(),
    query: '',
    matches: { order: [], inner: [], outer: [] },
    seen: new Map(),
    reported: null,
    offset: 0,
    highlighted: null,
    hoverHub: false,
    center: { x: 0, y: 0 },
    scale: 1,
    pointer: null,
    pressed: false,
    scrollRest: 0,
    spinFrame: 0,
    armed: false,
    armTimer: 0,
  };

  /** Forget an armed deletion and its timer, without drawing. */
  function disarm() {
    window.clearTimeout(view.armTimer);
    view.armTimer = 0;
    view.armed = false;
  }

  /**
   * List the outer ring codes for the current query.
   * @returns {Array<string>} The codes in ring order.
   */
  function outerList() {
    return view.matches.outer;
  }

  /**
   * List the codes the outer slots show now.
   * @returns {Array<{slot: number, code: string}>} One entry per filled slot.
   */
  function outerShown() {
    return outerWindow(outerList(), view.offset, WHEEL.outerSlots);
  }

  /**
   * Tell whether a query filters the wheel.
   * @returns {boolean} True when the query holds more than spaces.
   */
  function filtering() {
    return view.query.trim() !== '';
  }

  /**
   * Tell what the wheel lights now: what Enter picks and the readout names.
   * @returns {?{zone: string, code: ?string}} The lit hub or species, or null.
   */
  function lit() {
    return litTarget(view.highlighted, view.hoverHub, view.query);
  }

  /**
   * Find the species code under a wheel zone.
   * @param {{zone: string, index: number}} spot A zone from zoneAt.
   * @returns {?string} The code, or null for the hub, an empty slot, or the outside.
   */
  function codeAt(spot) {
    if (spot.zone === 'inner') {
      const item = view.inner[spot.index];
      return item ? item.code : null;
    }
    if (spot.zone === 'outer') {
      const item = outerShown().find((entry) => entry.slot === spot.index);
      return item ? item.code : null;
    }
    return null;
  }

  /**
   * Find the wheel zone under a pointer position.
   * @param {{x: number, y: number}} point The pointer in viewport pixels.
   * @returns {{zone: string, index: number}} The zone from zoneAt.
   */
  function zoneOf(point) {
    const bounds = canvas.getBoundingClientRect();
    const cx = bounds.left + bounds.width / 2;
    const cy = bounds.top + bounds.height / 2;
    return zoneAt(point.x - cx, point.y - cy, view.scale, view.inner.length);
  }

  /**
   * Build one slice: the wedge and its label lines.
   * @param {string} ring "inner" or "outer".
   * @param {number} index The slice index in its ring.
   * @param {number} count The number of slices in the ring.
   * @param {?{code: string, slot: string|number}} item The species in this slice, or null.
   * @returns {SVGGElement} The slice group.
   */
  function sliceNode(ring, index, count, item) {
    const inner = ring === 'inner';
    const wedge = svg('path', {
      class: 'wheel-wedge',
      d: wedgePath(index, count, inner ? WHEEL.hubRadius : WHEEL.innerRadius, inner ? WHEEL.innerRadius : WHEEL.outerRadius),
    });
    if (!item) {
      const empty = svg('g', { class: 'wheel-slot-empty', 'data-ring': ring });
      empty.append(wedge);
      return empty;
    }
    const name = view.names.get(item.code) || '';
    const target = lit();
    const group = svg('g', {
      class: 'wheel-slice',
      role: 'menuitem',
      'aria-label': `${item.code} ${name}`.trim(),
      'data-ring': ring,
      'data-code': item.code,
      'data-tier': speciesTier(view.parts.get(item.code)),
      'data-lit': String(Boolean(target) && target.code === item.code),
      'data-current': String(item.code === view.current),
    });
    if (filtering() && inner) {
      group.setAttribute('data-match', String(view.matches.inner.includes(item.code)));
    }
    const spot = sliceCenter(index, count, inner ? INNER_LABEL_RADIUS : OUTER_LABEL_RADIUS);
    const lines = inner
      ? [
        ['wheel-key', item.slot, INNER_KEY_DY],
        ['wheel-code wheel-code-inner', item.code, INNER_CODE_DY],
        ['wheel-name wheel-name-inner', sliceName(name, count > CROWDED_INNER_COUNT ? INNER_NAME_CHARS_CROWDED : INNER_NAME_CHARS), INNER_NAME_DY],
      ]
      : [
        ['wheel-code wheel-code-outer', item.code, OUTER_CODE_DY],
        ['wheel-name wheel-name-outer', sliceName(name, OUTER_NAME_CHARS), OUTER_NAME_DY],
      ];
    group.append(wedge);
    const seen = Number(view.seen.get(item.code)) || 0;
    if (seen > 0) {
      group.setAttribute('data-seen', String(seen));
    }
    for (const [className, text, dy] of lines) {
      const label = svg('text', { class: className, x: spot.x.toFixed(2), y: (spot.y + dy).toFixed(2) });
      label.textContent = text;
      if (seen > 0 && className.startsWith('wheel-code')) {
        // Species already logged on this transect carry their count beside the code.
        const count = svg('tspan', { class: 'wheel-seen' });
        count.textContent = ` \u00d7${seen}`;
        label.append(count);
      }
      group.append(label);
    }
    return group;
  }

  /**
   * Build the white outline that marks the current species of an edited row.
   * @param {string} ring "inner" or "outer".
   * @param {number} index The slice index in its ring.
   * @param {number} count The number of slices in the ring.
   * @returns {SVGPathElement} The outline.
   */
  function currentOutline(ring, index, count) {
    const inner = ring === 'inner';
    return svg('path', {
      class: 'wheel-current',
      d: wedgePath(index, count, inner ? WHEEL.hubRadius : WHEEL.innerRadius, inner ? WHEEL.innerRadius : WHEEL.outerRadius),
    });
  }

  /** Draw both rings, the hub, the seam, and the readout from the view state. */
  function render() {
    const innerNodes = view.inner.map((item, index) => sliceNode('inner', index, view.inner.length, item));
    const currentInner = view.inner.findIndex((item) => item.code === view.current);
    if (currentInner !== -1) {
      innerNodes.push(currentOutline('inner', currentInner, view.inner.length));
    }
    innerRing.replaceChildren(...innerNodes);

    const shown = new Map(outerShown().map((item) => [item.slot, item]));
    const outerNodes = [];
    for (let slot = 0; slot < WHEEL.outerSlots; slot += 1) {
      outerNodes.push(sliceNode('outer', slot, WHEEL.outerSlots, shown.get(slot) || null));
    }
    for (const [slot, item] of shown) {
      if (item.code === view.current) {
        outerNodes.push(currentOutline('outer', slot, WHEEL.outerSlots));
      }
    }
    outerRing.replaceChildren(...outerNodes);

    seam.toggleAttribute('hidden', outerList().length <= WHEEL.outerSlots);
    hubLabel.textContent = view.hubLabel;
    hub.setAttribute('aria-label', view.hubLabel);
    // The hub ring takes a tint per hub action, so an edit never reads like a new mark.
    hub.setAttribute('data-label', view.hubLabel);
    // An armed deletion turns the hub ring red until the second press or the disarm.
    hub.setAttribute('data-armed', String(view.armed));
    const target = lit();
    hub.setAttribute('data-lit', String(Boolean(target) && target.zone === 'hub'));
    renderReadout(target);
    reportHighlight(target);
  }

  /**
   * Tell the page when the lit species changes, so an open guide can follow it.
   * @param {?{zone: string, code: ?string}} target What the wheel lights, from lit().
   */
  function reportHighlight(target) {
    const code = target && target.zone === 'species' ? target.code : null;
    if (code === view.reported) {
      return;
    }
    view.reported = code;
    if (handlers.onHighlight) {
      handlers.onHighlight(code);
    }
  }

  /**
   * Write the readout chip: the query, and what Enter picks (the lit species or the hub).
   * @param {?{zone: string, code: ?string}} target What the wheel lights, from lit().
   */
  function renderReadout(target) {
    if (view.armed) {
      readout.replaceChildren(span('wheel-readout-hint wheel-readout-delete', deleteText(view.rowId, view.current)));
      return;
    }
    const parts = [];
    if (filtering()) {
      parts.push(span('wheel-query', view.query));
    }
    if (target && target.zone === 'hub' && view.mode === 'new') {
      parts.push(span('wheel-readout-code', view.hubLabel));
      parts.push(span('wheel-readout-hint', hubText(view.mode, view.rowId, view.current, view.hubLabel)));
    } else if (target && target.zone === 'hub') {
      parts.push(span('wheel-readout-hint', hubText(view.mode, view.rowId, view.current, view.hubLabel)));
    } else if (target) {
      parts.push(span('wheel-readout-code', target.code));
      parts.push(span('wheel-readout-name', view.names.get(target.code) || ''));
      if (view.pinOf.has(target.code)) {
        parts.push(span('wheel-readout-key', `key ${view.pinOf.get(target.code)}`));
      }
    } else if (filtering()) {
      parts.push(span('wheel-readout-hint', 'No species matches. Backspace edits.'));
    } else {
      parts.push(span('wheel-readout-hint', restingText(view.mode, view.rowId, view.current, view.hubLabel)));
    }
    readout.replaceChildren(...parts);
  }

  /** Tell screen readers what the readout says now. Keyboard changes call this. */
  function announce() {
    announcer.textContent = readout.textContent;
  }

  /**
   * Turn the outer ring from a few slots away back into place.
   * @param {number} steps The slots the list moved; positive moved later codes to 12 o'clock.
   */
  function animateSpin(steps) {
    window.cancelAnimationFrame(view.spinFrame);
    const slots = Math.max(-MAX_ANIMATED_SLOTS, Math.min(MAX_ANIMATED_SLOTS, steps));
    if (slots === 0 || prefersLessMotion()) {
      outerRing.removeAttribute('transform');
      return;
    }
    const from = slots * (TURN_DEG / WHEEL.outerSlots);
    const started = performance.now();
    const frame = (now) => {
      const progress = Math.min(1, (now - started) / SPIN_MS);
      const eased = 1 - (1 - progress) ** 2;
      outerRing.setAttribute('transform', `rotate(${(from * (1 - eased)).toFixed(3)})`);
      if (progress < 1) {
        view.spinFrame = window.requestAnimationFrame(frame);
      } else {
        outerRing.removeAttribute('transform');
      }
    };
    outerRing.setAttribute('transform', `rotate(${from.toFixed(3)})`);
    view.spinFrame = window.requestAnimationFrame(frame);
  }

  /**
   * Move the outer ring to a new offset and redraw.
   * @param {number} offset The new offset.
   * @param {number} steps The slots moved, for the animation.
   */
  function setOffset(offset, steps) {
    view.offset = offset;
    if (view.pointer) {
      followPointer(view.pointer);
    }
    render();
    animateSpin(steps);
  }

  /**
   * Bring a code of the outer ring into view by turning it to 12 o'clock the short way.
   * @param {?string} code A species code.
   */
  function showInOuter(code) {
    const list = outerList();
    const index = list.indexOf(code);
    if (index === -1 || outerShown().some((item) => item.code === code)) {
      return;
    }
    const steps = spinDelta(view.offset, index, list.length);
    view.offset = index;
    animateSpin(steps);
  }

  /**
   * Follow the pointer: note whether it rests on the hub, and move the highlight to the
   * slice under it (under a query, only onto a match), so Enter picks what is lit.
   * @param {{x: number, y: number}} point The pointer in viewport pixels.
   */
  function followPointer(point) {
    const spot = zoneOf(point);
    view.hoverHub = spot.zone === 'hub';
    view.highlighted = hoverHighlight(view.highlighted, codeAt(spot), view.query, view.matches.order);
  }

  /**
   * Move the highlight through the matches of the query, turning the outer ring when the
   * highlighted species is out of view.
   * @param {number} step 1 for the next match, -1 for the one before.
   */
  function moveBy(step) {
    // Under a query the highlight walks the matches; without one it walks the whole wheel,
    // the inner ring in key order and then the outer ring in its order (Tab, Shift+Tab).
    const list = filtering() ? view.matches.order : [...view.inner.map((item) => item.code), ...outerList()];
    if (list.length === 0 || step === 0) {
      return;
    }
    view.highlighted = cycleHighlight(list, view.highlighted, step > 0 ? 1 : -1);
    view.hoverHub = false;
    showInOuter(view.highlighted);
    render();
  }

  /**
   * Spin the outer ring by whole slots. While a query leaves no more matches than slots,
   * the highlight moves instead.
   * @param {number} steps Slots to move; positive brings later species to 12 o'clock.
   */
  function spinBy(steps) {
    if (filtering() && outerList().length <= WHEEL.outerSlots) {
      moveBy(steps);
      return;
    }
    setOffset(spinOffset(view.offset, steps, outerList().length), steps);
  }

  /**
   * Choose what sits at a pointer position.
   * @param {{x: number, y: number}} point The pointer in viewport pixels.
   * @param {boolean} shift True when Shift was held.
   */
  function pickAt(point, shift) {
    const spot = zoneOf(point);
    if (spot.zone === 'hub') {
      handlers.onHubPick({ shift });
      return;
    }
    const code = codeAt(spot);
    if (code) {
      handlers.onPick(code, { shift });
    }
  }

  canvas.addEventListener('mousedown', (event) => {
    event.preventDefault();
    event.stopPropagation();
    view.pressed = event.button === MOUSE_PRIMARY;
  });
  canvas.addEventListener('click', (event) => {
    event.stopPropagation();
    if (!view.open || !view.pressed || event.button !== MOUSE_PRIMARY) {
      return;
    }
    view.pressed = false;
    pickAt({ x: event.clientX, y: event.clientY }, event.shiftKey);
  });
  canvas.addEventListener('mousemove', (event) => {
    if (!view.open) {
      return;
    }
    view.pointer = { x: event.clientX, y: event.clientY };
    const before = JSON.stringify(lit());
    followPointer(view.pointer);
    if (JSON.stringify(lit()) !== before) {
      // A pointer that moves on to another slice disarms a deletion.
      disarm();
      render();
    }
  });
  canvas.addEventListener('mouseleave', () => {
    view.pointer = null;
    const before = JSON.stringify(lit());
    view.hoverHub = false;
    view.highlighted = hoverHighlight(view.highlighted, null, view.query, view.matches.order);
    if (JSON.stringify(lit()) !== before) {
      disarm();
      render();
    }
  });
  canvas.addEventListener('contextmenu', (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (view.open) {
      handlers.onCancel();
    }
  });
  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    if (!view.open) {
      return;
    }
    const pixels = wheelPixels(event, layer.clientHeight);
    if (Math.sign(pixels) !== Math.sign(view.scrollRest)) {
      view.scrollRest = 0;
    }
    view.scrollRest += pixels;
    const whole = Math.trunc(view.scrollRest / SCROLL_STEP_PX);
    if (whole === 0) {
      return;
    }
    const steps = Math.max(-MAX_STEPS_PER_EVENT, Math.min(MAX_STEPS_PER_EVENT, whole));
    view.scrollRest = steps === whole ? view.scrollRest - whole * SCROLL_STEP_PX : 0;
    spinBy(steps);
  }, { passive: false });

  const api = {
    /**
     * Open the wheel on a mark. The outer ring starts at offset 0 every time.
     * @param {{mode: string, rowId: ?string, hubLabel: string, species: Array<{code: string, name: string}>,
     *   pins: Object<string, ?string>, current: ?string, seen: ?Map<string, number>,
     *   center: {x: number, y: number}, scale: number, markPx: {x: number, y: number}}} options
     *   mode "new" or "edit"; rowId the edited row; hubLabel "Later", "Unlabel", or "Keep";
     *   current the species to outline; seen the labeled sightings per species in the open
     *   video, which lead the outer ring and carry a count; center and markPx in viewport
     *   pixels; scale from wheelScale.
     * @throws {TypeError} When the mode or the hub label is unknown.
     */
    open(options) {
      if (options.mode !== 'new' && options.mode !== 'edit') {
        throw new TypeError(`wheel.open: mode must be "new" or "edit", got ${JSON.stringify(options.mode)}`);
      }
      if (!HUB_LABELS.includes(options.hubLabel)) {
        throw new TypeError(`wheel.open: hubLabel must be Review, Unlabel, or Keep, got ${JSON.stringify(options.hubLabel)}`);
      }
      window.cancelAnimationFrame(view.spinFrame);
      outerRing.removeAttribute('transform');
      view.mode = options.mode;
      view.rowId = options.rowId || null;
      view.hubLabel = options.hubLabel;
      view.current = options.current || null;
      view.species = options.species;
      view.names = new Map(options.species.map((item) => [item.code, item.name]));
      view.parts = new Map(options.species.map((item) => [item.code, item.part]));
      view.inner = innerItems(options.pins, PIN_KEYS);
      view.pinOf = new Map(view.inner.map((item) => [item.code, item.slot]));
      view.query = '';
      view.seen = options.seen instanceof Map ? options.seen : new Map();
      view.matches = wheelMatches(options.species, view.inner.map((item) => item.code), '', view.seen);
      view.reported = null;
      view.offset = 0;
      view.highlighted = null;
      view.hoverHub = false;
      view.pointer = null;
      view.pressed = false;
      view.scrollRest = 0;
      disarm();
      view.open = true;
      announcer.textContent = '';
      root.hidden = false;
      api.relayout(options.center, options.scale, options.markPx);
      render();
    },

    /** Hide the wheel, forget the pointer, and disarm a deletion. */
    close() {
      view.open = false;
      view.pressed = false;
      view.pointer = null;
      disarm();
      window.cancelAnimationFrame(view.spinFrame);
      root.hidden = true;
    },

    /**
     * Tell whether the wheel is on screen.
     * @returns {boolean} True while the wheel is open.
     */
    isOpen() {
      return view.open;
    },

    /**
     * Move and size the wheel, point the leader line at the mark, and place the readout.
     * @param {{x: number, y: number}} center The wheel center in viewport pixels.
     * @param {number} scale The wheel scale.
     * @param {{x: number, y: number}} markPx The mark in viewport pixels.
     */
    relayout(center, scale, markPx) {
      view.center = { x: center.x, y: center.y };
      view.scale = scale;
      const size = HALF_BOX * 2 * scale;
      root.style.left = `${center.x - size / 2}px`;
      root.style.top = `${center.y - size / 2}px`;
      root.style.width = `${size}px`;
      root.style.height = `${size}px`;
      const dx = (markPx.x - center.x) / scale;
      const dy = (markPx.y - center.y) / scale;
      const distance = Math.hypot(dx, dy);
      const end = distance - LEADER_GAP_PX / scale;
      if (end > WHEEL.hubRadius) {
        for (const line of leaderLines) {
          line.setAttribute('x1', ((dx / distance) * WHEEL.hubRadius).toFixed(2));
          line.setAttribute('y1', ((dy / distance) * WHEEL.hubRadius).toFixed(2));
          line.setAttribute('x2', ((dx / distance) * end).toFixed(2));
          line.setAttribute('y2', ((dy / distance) * end).toFixed(2));
        }
        leader.removeAttribute('hidden');
      } else {
        leader.setAttribute('hidden', '');
      }
      const room = center.y - size / 2 - READOUT_GAP_PX - READOUT_HEIGHT_PX;
      const offset = `calc(100% + ${READOUT_GAP_PX}px)`;
      readout.style.bottom = room < 0 ? '' : offset;
      readout.style.top = room < 0 ? offset : '';
    },

    /**
     * Spin the outer ring by whole slots from the keyboard. While a query leaves no more
     * matches than slots, the highlight moves instead.
     * @param {number} steps Slots to move; positive brings later species to 12 o'clock.
     */
    spin(steps) {
      if (!view.open || steps === 0) {
        return;
      }
      spinBy(steps);
      announce();
    },

    /**
     * Filter the wheel by a typed query. The first match is highlighted and the outer ring
     * starts from 12 o'clock again.
     * @param {string} text The query.
     */
    setQuery(text) {
      if (!view.open) {
        return;
      }
      view.query = String(text);
      view.matches = wheelMatches(view.species, view.inner.map((item) => item.code), view.query, view.seen);
      view.offset = 0;
      view.highlighted = filtering() ? view.matches.order[0] || null : null;
      outerRing.removeAttribute('transform');
      render();
      announce();
    },

    /**
     * Move the highlight through the matches of the query, turning the outer ring when the
     * highlighted species is out of view.
     * @param {number} step 1 for the next match, -1 for the one before.
     */
    moveHighlight(step) {
      if (!view.open) {
        return;
      }
      moveBy(step);
      announce();
    },

    /**
     * Choose exactly what the wheel lights and the readout names (Enter): the lit species,
     * or the hub when the pointer rests on it and no query is typed.
     * @param {boolean} shift True when Shift was held.
     * @returns {boolean} True when something was lit and chosen.
     */
    pickHighlighted(shift) {
      const target = lit();
      if (!view.open || !target) {
        return false;
      }
      if (target.zone === 'hub') {
        handlers.onHubPick({ shift: Boolean(shift) });
      } else {
        handlers.onPick(target.code, { shift: Boolean(shift) });
      }
      return true;
    },

    /**
     * Arm deleting the edited row: the readout asks for a second press and the hub ring turns
     * red. A pointer that moves on to another slice, closing the wheel, a disarmDelete call,
     * or the time running out disarms it.
     * @param {number} ms How long the deletion stays armed, in milliseconds.
     * @returns {boolean} True when armed; false when the wheel is closed or edits no row.
     * @throws {TypeError} When ms is not a positive finite number.
     */
    armDelete(ms) {
      if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) {
        throw new TypeError(`wheel.armDelete: ms must be a positive number, got ${JSON.stringify(ms)}`);
      }
      if (!view.open || view.mode !== 'edit') {
        return false;
      }
      disarm();
      view.armed = true;
      view.armTimer = window.setTimeout(() => {
        disarm();
        render();
      }, ms);
      render();
      announce();
      return true;
    },

    /** Disarm a deletion and redraw the hub and the readout. Nothing happens when none is armed. */
    disarmDelete() {
      if (view.armed) {
        disarm();
        render();
      }
    },

    /**
     * Tell whether a deletion waits for its second press.
     * @returns {boolean} True while the wheel is open and the deletion armed.
     */
    deleteArmed() {
      return view.open && view.armed;
    },

    /**
     * Describe the open wheel for the end-to-end driver.
     * @returns {?{mode: string, rowId: ?string, hubLabel: string, center: {x: number, y: number},
     *   hub: {x: number, y: number, r: number}, inner: Array<{code: string, slot: string, x: number, y: number}>,
     *   outer: Array<{code: string, slot: number, x: number, y: number}>, highlighted: ?string,
     *   query: string, armed: boolean}} Viewport pixels at the center of each slice label;
     *   highlighted is the lit species, the one Enter picks (null when nothing or the hub is
     *   lit); armed is true while a deletion waits for its second press. Null when closed.
     */
    snapshot() {
      if (!view.open) {
        return null;
      }
      const { center, scale } = view;
      const place = (index, count, radius) => {
        const spot = sliceCenter(index, count, radius);
        return { x: center.x + spot.x * scale, y: center.y + spot.y * scale };
      };
      return {
        mode: view.mode,
        rowId: view.rowId,
        hubLabel: view.hubLabel,
        center: { x: center.x, y: center.y },
        hub: { x: center.x, y: center.y, r: WHEEL.hubRadius * scale },
        inner: view.inner.map((item, index) => ({
          code: item.code, slot: item.slot, ...place(index, view.inner.length, INNER_LABEL_RADIUS),
        })),
        outer: outerShown().map((item) => ({
          code: item.code, slot: item.slot, ...place(item.slot, WHEEL.outerSlots, OUTER_LABEL_RADIUS),
        })),
        highlighted: lit() && lit().zone === 'species' ? lit().code : null,
        query: view.query,
        armed: view.armed,
      };
    },
  };
  return api;
}
