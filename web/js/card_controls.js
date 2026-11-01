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

    return { runControlSpec, runControlAttr };
});
