'use strict';

const test = require('node:test');
const assert = require('node:assert');

const {
  isResumeCandidate,
  latestResumeCandidate,
  originLabel,
} = require('../src/session_pick');

test('a headless session is not a resume candidate', () => {
  assert.strictEqual(isResumeCandidate({ tui: false }), false);
  assert.strictEqual(isResumeCandidate({ tui: true }), true);
  assert.strictEqual(isResumeCandidate({}), true);
  assert.strictEqual(isResumeCandidate(null), false);
});

test('latest resume candidate skips leading headless sessions', () => {
  const sessions = [
    { id: 1, tui: false, title: 'coroutine' },
    { id: 2, tui: false, title: 'one-off' },
    { id: 3, tui: true, title: 'chat' },
  ];
  assert.strictEqual(latestResumeCandidate(sessions).id, 3);
});

test('latest resume candidate is null when every session is headless', () => {
  assert.strictEqual(latestResumeCandidate([{ tui: false }, { tui: false }]), null);
  assert.strictEqual(latestResumeCandidate([]), null);
});

test('origin labels distinguish chats from machine sessions', () => {
  assert.strictEqual(originLabel({ tui: true }), 'TUI chat');
  assert.strictEqual(originLabel({ tui: false }), 'headless run');
  assert.strictEqual(originLabel({}), '');
  assert.strictEqual(originLabel(null), '');
});
