'use strict';

const test = require('node:test');
const assert = require('node:assert');

const {
  normalizeEndpoint,
  toWsUrl,
  relativePath,
  selectionRef,
  backoffDelay,
  shellArg,
} = require('../src/protocol');

test('endpoints are normalised and restricted to loopback', () => {
  assert.equal(normalizeEndpoint('http://127.0.0.1:8760/'), 'http://127.0.0.1:8760');
  assert.equal(normalizeEndpoint('http://localhost:9000'), 'http://localhost:9000');
  assert.equal(normalizeEndpoint('http://evil.example.com'), 'http://127.0.0.1:8760');
  assert.equal(normalizeEndpoint('file:///etc/passwd'), 'http://127.0.0.1:8760');
});

test('the websocket URL keeps only the ticket in the query', () => {
  const url = toWsUrl('http://127.0.0.1:8760', 'abc123');
  assert.equal(url, 'ws://127.0.0.1:8760/api/vscode/ws?ticket=abc123');
});

test('relative paths refuse to escape the workspace', () => {
  assert.equal(relativePath('/repo', '/repo/src/app.py'), 'src/app.py');
  assert.equal(relativePath('/repo', '/other/app.py'), null);
});

test('selection refs are 1-based and compact', () => {
  assert.equal(selectionRef('src/app.py', 3, 3), '@src/app.py#3');
  assert.equal(selectionRef('src/app.py', 3, 9), '@src/app.py#3-9');
});

test('reconnect backoff is bounded', () => {
  assert.equal(backoffDelay(0), 1000);
  assert.equal(backoffDelay(2), 5000);
  assert.equal(backoffDelay(99), 30000);
});

test('shell arguments are quoted only when needed', () => {
  assert.equal(shellArg('ses_abc123'), 'ses_abc123');
  assert.equal(shellArg('a b'), '"a b"');
});
