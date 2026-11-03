/* Pure decision for a project card's run control.
 *
 * Kept out of perun.js so `node --test` can cover it (no DOM). The browser gets
 * it as `window.CardControls`; Node gets `module.exports`.
 *
 * The regression this guards against: `projectCard()` used to render **Run**
 * unconditionally and only *add* a Stop button when the active run was not
 * standalone. A dashboard-launched TUI run is always `standalone=1`, so after a
 * Perun restart the card kept its "working" pip and banner (the run row survives
 * and its pid is alive) but its button reverted to Run — the intended flow is
 * Run -> Stop, where Stop closes the terminal Perun opened.
 *
 * So a working card always yields the run's own control:
 *   - idle         -> Run   (open OpenCode in the project directory)
 *   - active run   -> Stop  (cancels a headless run / closes a TUI terminal;
 *                            POST /api/runs/{uid}/cancel, which only ever kills a
 *                            process carrying that run's PERUN_RUN_UID marker)
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
        return {
            kind: 'stop', uid: run.run_uid || '', cls: 'btn sm danger', attr: 'data-stop',
            label: '\u25a0 Stop',
            // A standalone TUI run is one Perun launched in a terminal: Stop is
            // what closes that terminal. Never claim it for a run without one.
            title: run.standalone ? 'Stop and close this TUI terminal' : 'Cancel this run',
        };
    }

    /**
     * The attribute a card's run control carries, so a renderer can find and
     * sync it. Never returns the Run attribute for an active run.
     */
    function runControlAttr(run) {
        return runControlSpec(run).attr;
    }

    /**
     * Describe the optional Focus control for a card's active run.
     *
     * Only a tracked TUI run has a window to raise: a headless run has none, and
     * a TUI run whose launch never produced a pid has nothing to attribute. The
     * server also reports whether window activation is supported at all (it is
     * Windows-only today), so other platforms never show a dead button.
     *
     * @param {object|null|undefined} run the project's active run
     * @param {{focusSupported?: boolean}} [opts]
     * @returns {null|{kind: string, uid: string, cls: string, attr: string,
     *                label: string, title: string}}
     */
    function focusControlSpec(run, opts) {
        const supported = !opts || opts.focusSupported !== false;
        if (!run || !run.standalone || !run.pid || !supported) return null;
        return {
            kind: 'focus', uid: run.run_uid || '', cls: 'btn sm ghost', attr: 'data-focus',
            label: '\u2922 Focus',
            title: 'Bring this TUI terminal to the front',
        };
    }

    return { runControlSpec, runControlAttr, focusControlSpec };
});
