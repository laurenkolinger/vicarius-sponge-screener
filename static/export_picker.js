/**
 * The choices of the Export picker. This file holds no page state, so the node tests can run
 * it without a browser.
 */

/**
 * Group the videos on offer by site, keeping their order.
 * @param {Array<{key: string, site: string}>} videos The videos with sightings.
 * @returns {Array<{site: string, videos: Array<object>}>} One entry per site.
 */
export function groupBySite(videos) {
  const groups = new Map();
  for (const video of videos) {
    if (!groups.has(video.site)) {
      groups.set(video.site, []);
    }
    groups.get(video.site).push(video);
  }
  return [...groups].map(([site, members]) => ({ site, videos: members }));
}

/**
 * Say how much of a group is checked.
 * @param {Array<{key: string}>} videos The group.
 * @param {Set<string>} unchecked The keys left out.
 * @returns {'all'|'some'|'none'} The state of the group's checkbox.
 */
export function groupState(videos, unchecked) {
  const left = videos.filter((video) => unchecked.has(video.key)).length;
  if (left === 0) {
    return 'all';
  }
  return left === videos.length ? 'none' : 'some';
}

/**
 * Check or uncheck a whole group.
 * @param {Array<{key: string}>} videos The group.
 * @param {Set<string>} unchecked The keys left out.
 * @param {boolean} checked True checks the group.
 * @returns {Set<string>} The new keys left out.
 */
export function setGroup(videos, unchecked, checked) {
  const next = new Set(unchecked);
  for (const video of videos) {
    if (checked) {
      next.delete(video.key);
    } else {
      next.add(video.key);
    }
  }
  return next;
}

/**
 * Build the request for the checked videos. Every video checked sends no list, so the export
 * covers whatever has sightings when it starts.
 * @param {Array<{key: string}>} videos The videos on offer.
 * @param {Set<string>} unchecked The keys left out.
 * @returns {{keys?: Array<string>}} The body of the request.
 */
export function exportRequest(videos, unchecked) {
  const keys = videos.filter((video) => !unchecked.has(video.key)).map((video) => video.key);
  return keys.length === videos.length ? {} : { keys };
}

/**
 * Name the picker's button.
 * @param {Array<{key: string}>} videos The videos on offer.
 * @param {Set<string>} unchecked The keys left out.
 * @returns {string} The label, such as Export 6 videos.
 */
export function exportLabel(videos, unchecked) {
  const count = videos.filter((video) => !unchecked.has(video.key)).length;
  return `Export ${count} video${count === 1 ? '' : 's'}`;
}

/**
 * Word one video's counts.
 * @param {{sightings: number, review: number}} video The video.
 * @returns {string} The counts, such as 35 sightings, 7 review.
 */
export function videoCounts(video) {
  const sightings = `${video.sightings} sighting${video.sightings === 1 ? '' : 's'}`;
  return video.review ? `${sightings}, ${video.review} review` : sightings;
}
