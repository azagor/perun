/* Pure decision for a project card's run control.
 *
 * Kept out of perun.js so `node --test` can cover it (no DOM). The browser gets
 * it as `window.CardControls`; Node gets `module.exports`.
 *
 * The bug this guards against: a card could show **Run** while its project was
 * visibly active, because a standalone TUI run (owned by the user's terminal)
 * had its Stop button suppressed while the Run button was always rendered.
 * Starting a second session next to a working one is never right, so a working
 * card always yields the run's own control:
 *   - idle              -> Run   (launch OpenCode in the project directory)
 *   - headless run      -> Stop  (Perun owns the process and may cancel it)
 *   - standalone TUI run -> Finish (end tracking + capture usage; never kill it)
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.CardControls = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    /**
     * Describe the single control a card offers.
     *
     * @param {object|null|undefined} run the project's active run, or nothing
     * @returns {{kind: string, uid: string, cls: string, attr: string,
     *            label: string, title: string}}
     */
    function runControlSpec(run) {
        if (!run) {
            return {
                kind: 'run', uid: '', cls: 'btn sm primary', attr: 'data-tui',
                label: '\u25b6 Run',
                title: "Run OpenCode in this project's directory",
            };
        }
        const uid = run.run_uid || '';
        if (run.standalone) {
            return {
                kind: 'finish', uid, cls: 'btn sm', attr: 'data-finish',
                label: '\u2713 Finish',
                title: 'Mark this terminal session finished - the terminal keeps running',
            };
        }
        return {
            kind: 'stop', uid, cls: 'btn sm danger', attr: 'data-stop',
            label: '\u25a0 Stop', title: 'Cancel this run',
        };
    }

    /**
     * The attribute a card's run control carries, so a renderer can find and
     * sync it. Never returns a Run attribute for an active run.
     */
    function runControlAttr(run) {
        return runControlSpec(run).attr;
    }

    return { runControlSpec, runControlAttr };
});
