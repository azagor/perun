/* Pure session-choice decisions for "Run" and the Agent Console.
 *
 * Kept out of perun.js so `node --test` can cover it (no DOM). The browser gets
 * it as `window.SessionPick`; Node gets `module.exports`.
 *
 * The rule this guards: a session created by a headless job (a coroutine, a
 * one-off Perun run) is not a chat. It stays explicitly selectable, but the
 * card's fast Run must not silently reopen it - the user would land on a
 * machine prompt instead of their conversation.
 *
 * ``tui`` comes from GET /api/projects/{slug}/sessions:
 *   true  -> the session's first run was a TUI launch
 *   false -> the first run was a headless job
 *   null/undefined -> no run evidence (manual session / older data); resumable
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.SessionPick = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    /**
     * Whether the fast Run may auto-resume this session.
     *
     * Unknown provenance is treated as resumable: a manual or legacy session
     * must not disappear from the fast path just because Perun has no run row
     * for it.
     *
     * @param {object|null|undefined} session
     * @returns {boolean}
     */
    function isResumeCandidate(session) {
        return !!session && session.tui !== false;
    }

    /**
     * The newest resumable session, or null. ``sessions`` is already ordered
     * newest-first by the server.
     *
     * @param {Array<object>|null|undefined} sessions
     * @returns {object|null}
     */
    function latestResumeCandidate(sessions) {
        return (sessions || []).find(isResumeCandidate) || null;
    }

    /**
     * A short label for pickers: "TUI chat", "headless run", or "" when the
     * origin is unknown.
     *
     * @param {object|null|undefined} session
     * @returns {string}
     */
    function originLabel(session) {
        if (!session) return '';
        if (session.tui === false) return 'headless run';
        if (session.tui === true) return 'TUI chat';
        return '';
    }

    return { isResumeCandidate, latestResumeCandidate, originLabel };
});
