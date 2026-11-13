'use strict';

/**
 * Pure session-choice rules for the companion, mirroring
 * `web/js/session_pick.js` in the dashboard.
 *
 * `tui` comes from `GET /api/projects/{slug}/sessions`:
 *   true       -> the session's first run was a TUI launch
 *   false      -> the first run was a headless job (a coroutine, a one-off)
 *   null/undef -> no run evidence (manual/legacy); resumable
 *
 * A headless session stays explicitly selectable; the quick "Resume latest"
 * must never silently open one, exactly like the dashboard's Run button.
 */

function isResumeCandidate(session) {
  return !!session && session.tui !== false;
}

function latestResumeCandidate(sessions) {
  return (sessions || []).find(isResumeCandidate) || null;
}

function originLabel(session) {
  if (!session) return '';
  if (session.tui === false) return 'headless run';
  if (session.tui === true) return 'TUI chat';
  return '';
}

module.exports = { isResumeCandidate, latestResumeCandidate, originLabel };
