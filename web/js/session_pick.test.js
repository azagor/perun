const assert = require('assert');
const {
    isResumeCandidate,
    latestResumeCandidate,
    originLabel,
} = require('./session_pick.js');

// Unknown provenance (manual session, older data) stays resumable.
assert.strictEqual(isResumeCandidate(null), false);
assert.strictEqual(isResumeCandidate({}), true);
assert.strictEqual(isResumeCandidate({ tui: null }), true);
assert.strictEqual(isResumeCandidate({ tui: true }), true);
assert.strictEqual(isResumeCandidate({ tui: false }), false);

// The newest TUI/unknown session wins; a newer headless one is skipped.
const sessions = [
    { id: 1, tui: false, title: 'headless job' },
    { id: 2, tui: true, title: 'real chat' },
    { id: 3, title: 'manual chat' },
];
assert.strictEqual(latestResumeCandidate(sessions).id, 2);
assert.strictEqual(latestResumeCandidate([]), null);
assert.strictEqual(latestResumeCandidate(null), null);
assert.strictEqual(latestResumeCandidate([{ id: 9, tui: false }]), null);

// Labels are advisory; unknown stays unlabeled.
assert.strictEqual(originLabel({ tui: false }), 'headless run');
assert.strictEqual(originLabel({ tui: true }), 'TUI chat');
assert.strictEqual(originLabel({}), '');
assert.strictEqual(originLabel(null), '');

console.log('session_pick tests passed');
