'use strict';

/**
 * The dashboard's Mode B decision, shared with `web/js/vscode_launch.js`.
 *
 * A machine-wide client count is not enough: a companion for another project
 * must not make this project attempt a VS Code terminal, and a Restricted
 * window must fall back without a red error.
 */

const test = require('node:test');
const assert = require('node:assert');

const {
  shouldAttemptVscode,
  fallbackReason,
} = require('../../../web/js/vscode_launch');

test('an unknown status is attempted and left to the request fallback', () => {
  assert.strictEqual(shouldAttemptVscode(null, 'demo'), true);
  assert.strictEqual(fallbackReason(null, 'demo'), 'unknown');
});

test('another project companion does not count for this one', () => {
  const status = {
    connected_clients: 1,
    project_clients: 0,
    extension_project: 'other',
    extension_trusted: true,
  };
  assert.strictEqual(shouldAttemptVscode(status, 'demo'), false);
  assert.strictEqual(fallbackReason(status, 'demo'), 'disconnected');
});

test('a Restricted window for this project falls back as untrusted', () => {
  const status = {
    project_clients: 0,
    extension_project: 'demo',
    extension_trusted: false,
  };
  assert.strictEqual(shouldAttemptVscode(status, 'demo'), false);
  assert.strictEqual(fallbackReason(status, 'demo'), 'untrusted');
});

test('a connected window for this project is attempted', () => {
  const status = {
    project_clients: 1,
    extension_project: 'demo',
    extension_trusted: true,
  };
  assert.strictEqual(shouldAttemptVscode(status, 'demo'), true);
  assert.strictEqual(fallbackReason(status, 'demo'), '');
});

test('a trusted window overrides an earlier untrusted heartbeat', () => {
  const status = {
    project_clients: 1,
    extension_project: 'demo',
    extension_trusted: false,   // stale write from another window
  };
  assert.strictEqual(shouldAttemptVscode(status, 'demo'), true);
});

test('an older server without project_clients keeps the global behaviour', () => {
  const status = { connected_clients: 1 };
  assert.strictEqual(shouldAttemptVscode(status, 'demo'), true);
});
