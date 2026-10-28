// Tests for static/vicarius_run.js: the run the page names and the signed-in initials.
// Run with: node --test tests/test_vicarius_run.mjs
//
// The project has no package.json, so node would read static/*.js as CommonJS and refuse
// their `export` statements. The test imports the file through a data: URL, which node
// always treats as an ES module, as the other page tests do.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, '..', 'static', 'vicarius_run.js'), 'utf8');
const {
  INITIALS_PATTERN, RUN_HEADER, initialsFromSearch, projectDirFromSearch, startingInitials, upperCaseAsTyped, withRun,
} = await import(`data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`);

/**
 * Build a stand-in for a text field with a caret.
 * @param {string} value The current value.
 * @param {number} caret Where the caret sits.
 * @returns {object} The fake field.
 */
function fakeField(value, caret) {
  return {
    value,
    selectionStart: caret,
    selectionEnd: caret,
    setSelectionRange(start, end) {
      this.selectionStart = start;
      this.selectionEnd = end;
    },
  };
}

test('a valid initials parameter is read in upper case', () => {
  assert.equal(initialsFromSearch('?initials=LO'), 'LO');
  assert.equal(initialsFromSearch('?project_dir=%2Fx&initials=ab&home=y'), 'AB');
  assert.equal(initialsFromSearch('?initials=%20mkt%20'), 'MKT');
  assert.equal(initialsFromSearch('initials=abcd'), 'ABCD');
});

test('an invalid, missing, or repeated initials parameter gives nothing', () => {
  for (const search of [
    '', '?', '?initials=', '?initials=L', '?initials=LOLOL', '?initials=L0', '?initials=L.O', '?initials=<b>',
    '?initials=LO&initials=AB', '?initial=LO', '?INITIALS=LO', '?initials=%00LO', undefined, null,
  ]) {
    assert.equal(initialsFromSearch(search), '', String(search));
  }
});

test('the initials pattern is two to four letters', () => {
  assert.ok(INITIALS_PATTERN.test('LO'));
  assert.ok(INITIALS_PATTERN.test('abcd'));
  assert.ok(!INITIALS_PATTERN.test('A'));
  assert.ok(!INITIALS_PATTERN.test('ABCDE'));
  assert.ok(!INITIALS_PATTERN.test('A1'));
});

test('the starting value is the URL, then the remembered value, never a default', () => {
  assert.equal(startingInitials('AB', 'LO'), 'AB');
  assert.equal(startingInitials('', 'LO'), 'LO');
  assert.equal(startingInitials('', ''), '');
  assert.equal(startingInitials(undefined, undefined), '');
  assert.equal(startingInitials(null, 7), '');
});

test('project_dir is read once and trimmed', () => {
  const folder = '/mnt/rip/vicarius_drive/vicarius/modules/sponge_screener/inprocess/run_26-10-27_LO';
  assert.equal(projectDirFromSearch(`?project_dir=${encodeURIComponent(folder)}&initials=LO`), folder);
  assert.equal(projectDirFromSearch(`?project_dir=%20${encodeURIComponent(folder)}%20`), folder);
  assert.equal(projectDirFromSearch('?project_dir=a&project_dir=b'), '');
  assert.equal(projectDirFromSearch('?project_dir=%20%20'), '');
  assert.equal(projectDirFromSearch(''), '');
});

test('withRun adds the run to an image address', () => {
  assert.equal(withRun('/media/crops/ID001_ACAU_TOPLEFT_FLC_T1.png', 'run_26-10-27_LO'),
    '/media/crops/ID001_ACAU_TOPLEFT_FLC_T1.png?run=run_26-10-27_LO');
  assert.equal(withRun('/api/observations?key=a', 'run_x'), '/api/observations?key=a&run=run_x');
  assert.equal(withRun('/media/crops/x.png', ''), '/media/crops/x.png');
  assert.equal(withRun('/media/crops/x.png', 'run_a&b=c'), '/media/crops/x.png?run=run_a%26b%3Dc');
});

test('the run header has the name the server reads', () => {
  assert.equal(RUN_HEADER, 'X-Screener-Run');
});

test('typing is upper-cased and the caret stays where it was', () => {
  const field = fakeField('lO', 1);
  assert.equal(upperCaseAsTyped(field), true);
  assert.equal(field.value, 'LO');
  assert.equal(field.selectionStart, 1);
  assert.equal(field.selectionEnd, 1);
});

test('an upper-case value is left alone', () => {
  const field = fakeField('LO', 2);
  assert.equal(upperCaseAsTyped(field), false);
  assert.equal(field.value, 'LO');
});

test('a field without a caret is still upper-cased', () => {
  const field = { value: 'ab', selectionStart: null, selectionEnd: null, setSelectionRange() { throw new Error('no caret'); } };
  assert.equal(upperCaseAsTyped(field), true);
  assert.equal(field.value, 'AB');
});
