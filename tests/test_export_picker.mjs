/** Tests for the choices of the Export picker. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(here, '..', 'static', 'export_picker.js'), 'utf8');
const { exportLabel, exportRequest, groupBySite, groupState, setGroup, videoCounts } = await import(
  `data:text/javascript;base64,${Buffer.from(source, 'utf8').toString('base64')}`
);

const VIDEOS = [
  { key: 'a', site: 'Buck Island', transect: 'T1', sightings: 35, review: 7 },
  { key: 'b', site: 'Buck Island', transect: 'T2', sightings: 1, review: 0 },
  { key: 'c', site: 'Flat Cay', transect: 'T1', sightings: 8, review: 0 },
];

test('groupBySite keeps the order and handles no videos', () => {
  assert.deepEqual(groupBySite(VIDEOS).map((group) => [group.site, group.videos.length]), [['Buck Island', 2], ['Flat Cay', 1]]);
  assert.deepEqual(groupBySite([]), []);
});

test('groupState reads all, some, and none', () => {
  assert.equal(groupState(VIDEOS, new Set()), 'all');
  assert.equal(groupState(VIDEOS, new Set(['a'])), 'some');
  assert.equal(groupState(VIDEOS, new Set(['a', 'b', 'c'])), 'none');
});

test('setGroup leaves out and restores one site without touching the other', () => {
  const site = groupBySite(VIDEOS)[1].videos;
  const out = setGroup(site, new Set(['a']), false);
  assert.deepEqual([...out].sort(), ['a', 'c']);
  assert.deepEqual([...setGroup(site, out, true)], ['a']);
});

test('exportRequest names keys only for part of the videos', () => {
  assert.deepEqual(exportRequest(VIDEOS, new Set()), {});
  assert.deepEqual(exportRequest(VIDEOS, new Set(['c'])), { keys: ['a', 'b'] });
});

test('exportRequest ignores a remembered key that no longer has sightings', () => {
  assert.deepEqual(exportRequest(VIDEOS, new Set(['gone'])), {});
});

test('exportLabel and videoCounts count correctly', () => {
  assert.equal(exportLabel(VIDEOS, new Set(['a'])), 'Export 2 videos');
  assert.equal(exportLabel(VIDEOS, new Set(['a', 'b'])), 'Export 1 video');
  assert.equal(videoCounts(VIDEOS[0]), '35 sightings, 7 review');
  assert.equal(videoCounts(VIDEOS[1]), '1 sighting');
});
