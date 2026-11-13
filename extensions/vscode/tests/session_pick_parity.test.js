'use strict';

/**
 * The companion and the dashboard must apply the same session-choice rule.
 *
 * `web/js/session_pick.js` is the browser module (UMD, so it also works under
 * `require`); `src/session_pick.js` mirrors it for the extension host. This test
 * fails the moment the two drift, which would make "Resume latest" in VS Code
 * pick a different session than the dashboard's Run button.
 */

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');
const fs = require('fs');

const webPath = path.join(__dirname, '..', '..', '..', 'web', 'js', 'session_pick.js');
const dashboard = fs.existsSync(webPath) ? require(webPath) : null;
const companion = require('../src/session_pick');

const SAMPLES = [
  { id: 1, tui: true },
  { id: 2, tui: false },
  { id: 3 },
  { id: 4, tui: null },
];

test('the companion and dashboard session rules agree', { skip: !dashboard }, () => {
  for (const value of [null, undefined, ...SAMPLES]) {
    assert.strictEqual(companion.isResumeCandidate(value),
      dashboard.isResumeCandidate(value));
    assert.strictEqual(companion.originLabel(value), dashboard.originLabel(value));
  }
  const lists = [[], SAMPLES, SAMPLES.slice().reverse(), [{ tui: false }]];
  for (const list of lists) {
    const a = companion.latestResumeCandidate(list);
    const b = dashboard.latestResumeCandidate(list);
    assert.strictEqual(a && a.id, b && b.id);
  }
});
