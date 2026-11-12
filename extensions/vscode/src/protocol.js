'use strict';

/**
 * Pure helpers for the Perun Overseer extension.
 *
 * These are deliberately free of `vscode` imports so they can be tested with
 * plain `node --test` (no extension host).
 */

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);

/** Normalise a Perun endpoint and refuse anything that is not loopback. */
function normalizeEndpoint(value, fallback = 'http://127.0.0.1:8760') {
  const raw = String(value || '').trim() || fallback;
  let url;
  try {
    url = new URL(raw);
  } catch (_) {
    return fallback;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return fallback;
  if (!LOOPBACK.has(url.hostname)) return fallback;
  const path = url.pathname.replace(/\/+$/, '');
  return `${url.protocol}//${url.host}${path}`;
}

function toWsUrl(endpoint, ticket) {
  const base = normalizeEndpoint(endpoint);
  const url = new URL(base);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = `${url.pathname.replace(/\/+$/, '')}/api/vscode/ws`;
  url.search = `?ticket=${encodeURIComponent(String(ticket || ''))}`;
  return url.toString();
}

/** Project-relative POSIX path, or null when the file escapes the root. */
function relativePath(root, file) {
  const path = require('path');
  const rel = path.relative(path.resolve(root), path.resolve(file));
  if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) return null;
  return rel.split(path.sep).join('/');
}

function selectionRef(rel, startLine, endLine) {
  const start = Math.max(1, Number(startLine) || 1);
  const end = Math.max(start, Number(endLine) || start);
  const clean = String(rel || '').replace(/\\/g, '/').replace(/^\/+/, '');
  return start === end ? `@${clean}#${start}` : `@${clean}#${start}-${end}`;
}

function backoffDelay(attempt) {
  const table = [1000, 2000, 5000, 10000, 30000];
  return table[Math.min(Math.max(0, attempt), table.length - 1)];
}

/** Quote one argument for the integrated terminal (pwsh/cmd/sh). */
function shellArg(value) {
  const text = String(value || '');
  if (/^[A-Za-z0-9_./:@+-]+$/.test(text)) return text;
  return '"' + text.replace(/"/g, '\\"') + '"';
}

module.exports = {
  LOOPBACK,
  normalizeEndpoint,
  toWsUrl,
  relativePath,
  selectionRef,
  backoffDelay,
  shellArg,
};
