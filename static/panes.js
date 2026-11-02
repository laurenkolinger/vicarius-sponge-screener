/**
 * Resizable panes. A handle sits on the edge between two panes; dragging it, or pressing an
 * arrow key on it, sets the size of its pane, and a double-click returns the default.
 */

/** Pixels one arrow key press moves a handle, and with Shift held. */
const ARROW_STEP_PX = 16;
const ARROW_BIG_STEP_PX = 48;

/** The video keeps at least this much width, however the panes around it grow. */
const MIN_CENTER_PX = 260;

/** The sightings list keeps at least this much height beside the Review list and the tally. */
const MIN_SIGHTINGS_PX = 120;

/**
 * Every pane. `edge` is 1 when dragging right or down grows the pane, and -1 when it shrinks
 * it. `styles` turns a size into the CSS variables that hold it.
 */
export const PANES = Object.freeze({
  left: { axis: 'x', edge: 1, min: 160, target: '.browser', styles: (px) => ({ '--left': `${px}px` }) },
  right: { axis: 'x', edge: -1, min: 180, target: '.log', styles: (px) => ({ '--right': `${px}px` }) },
  guide: { axis: 'x', edge: -1, min: 220, target: '#guide', styles: (px) => ({ '--guide-dock-width': `${px}px` }) },
  review: { axis: 'y', edge: -1, min: 70, target: '.log-later', styles: (px) => ({ '--review-flex': `0 0 ${px}px`, '--review-max': 'none' }) },
  tally: { axis: 'y', edge: -1, min: 90, target: '.log-tally', styles: (px) => ({ '--tally-flex': `0 0 ${px}px` }) },
});

/**
 * Find the largest size a pane can take.
 * @param {string} name The pane.
 * @param {{width: number, logHeight: number, sizes: Object<string, number>}} room The window
 *   width, the height of the sightings column, and the current size of every pane.
 * @returns {number} The largest size in pixels, never below the pane's smallest.
 */
export function paneMax(name, room) {
  const { width, logHeight, sizes } = room;
  const others = {
    left: sizes.right + sizes.guide,
    right: sizes.left + sizes.guide,
    guide: sizes.left + sizes.right,
    review: sizes.tally,
    tally: sizes.review,
  };
  const max = (PANES[name].axis === 'x' ? width - MIN_CENTER_PX : logHeight - MIN_SIGHTINGS_PX) - others[name];
  return Math.max(PANES[name].min, Math.floor(max));
}

/**
 * Keep a size inside what a pane allows.
 * @param {string} name The pane.
 * @param {number} size The wanted size in pixels.
 * @param {object} room The room, as in paneMax.
 * @returns {number} The size to use, as a whole number of pixels.
 */
export function clampPane(name, size, room) {
  return Math.round(Math.min(Math.max(size, PANES[name].min), paneMax(name, room)));
}

/**
 * Read saved sizes, keeping only numbers for panes that exist.
 * @param {?string} text The saved JSON.
 * @returns {Object<string, number>} The sizes by pane.
 */
export function readPaneSizes(text) {
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return {};
  }
  const sizes = {};
  for (const name of Object.keys(PANES)) {
    if (parsed && Number.isFinite(parsed[name]) && parsed[name] > 0) {
      sizes[name] = parsed[name];
    }
  }
  return sizes;
}

/**
 * Wire every handle on the page and apply the saved sizes.
 * @param {{load: function(): ?string, save: function(string): void}} storage Where sizes live.
 * @param {function(): void} onReset Called after a double-click returns a pane to its default.
 * @returns {{refit: function(): void, isSized: function(string): boolean, clamp: function(string, number): number}}
 *   Refit keeps saved sizes inside the room there is now, isSized tells whether a pane has a
 *   saved size, and clamp keeps any size inside what a pane allows.
 */
export function createPanes(storage, onReset) {
  const sizes = readPaneSizes(storage.load());
  const root = document.documentElement;

  /** Measure one pane along its axis. */
  const measure = (name) => {
    const node = document.querySelector(PANES[name].target);
    if (!node || node.hidden) {
      return 0;
    }
    const box = node.getBoundingClientRect();
    return PANES[name].axis === 'x' ? box.width : box.height;
  };

  /** Describe the room around the panes right now. */
  const room = () => ({
    width: window.innerWidth,
    logHeight: document.querySelector('.log').getBoundingClientRect().height,
    sizes: {
      left: measure('left'),
      right: measure('right'),
      guide: document.body.dataset.guideDock === 'docked' ? measure('guide') : 0,
      review: measure('review'),
      tally: measure('tally'),
    },
  });

  /** Set the CSS variables of one pane. */
  const apply = (name, size) => {
    for (const [variable, value] of Object.entries(PANES[name].styles(size))) {
      root.style.setProperty(variable, value);
    }
  };

  /** Save a pane's size and show it. */
  const setSize = (name, wanted) => {
    sizes[name] = clampPane(name, wanted, room());
    apply(name, sizes[name]);
    storage.save(JSON.stringify(sizes));
  };

  /** Return one pane to its default. */
  const reset = (name) => {
    delete sizes[name];
    for (const variable of Object.keys(PANES[name].styles(0))) {
      root.style.removeProperty(variable);
    }
    storage.save(JSON.stringify(sizes));
    onReset();
  };

  for (const handle of document.querySelectorAll('.pane-handle')) {
    const name = handle.dataset.pane;
    const pane = PANES[name];
    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || event.ctrlKey) {
        return;
      }
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      const startAt = pane.axis === 'x' ? event.clientX : event.clientY;
      const startSize = measure(name);
      document.body.dataset.resizing = pane.axis;
      let onMove = null;
      const onUp = () => {
        delete document.body.dataset.resizing;
        handle.removeEventListener('pointermove', onMove);
        handle.removeEventListener('pointerup', onUp);
        handle.removeEventListener('pointercancel', onUp);
        handle.removeEventListener('lostpointercapture', onUp);
      };
      onMove = (move) => {
        // A release the window never saw ends the drag at the next movement.
        if (move.buttons === 0) {
          onUp();
          return;
        }
        const position = pane.axis === 'x' ? move.clientX : move.clientY;
        setSize(name, startSize + pane.edge * (position - startAt));
      };
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onUp);
      handle.addEventListener('pointercancel', onUp);
      handle.addEventListener('lostpointercapture', onUp);
    });
    handle.addEventListener('dblclick', () => reset(name));
    handle.addEventListener('keydown', (event) => {
      const forward = pane.axis === 'x' ? 'ArrowRight' : 'ArrowDown';
      const backward = pane.axis === 'x' ? 'ArrowLeft' : 'ArrowUp';
      if (event.key !== forward && event.key !== backward) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      const step = (event.shiftKey ? ARROW_BIG_STEP_PX : ARROW_STEP_PX) * (event.key === forward ? 1 : -1);
      setSize(name, measure(name) + pane.edge * step);
    });
  }

  /** Keep the saved sizes inside the window. Saved values stay, so a window that grows back restores them. */
  const refit = () => {
    for (const [name, size] of Object.entries(sizes)) {
      apply(name, clampPane(name, size, room()));
    }
  };
  for (const [name, size] of Object.entries(sizes)) {
    apply(name, size);
  }
  refit();
  return { refit, isSized: (name) => name in sizes, clamp: (name, size) => clampPane(name, size, room()) };
}
