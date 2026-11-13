/* Pure decision for "can this project host the TUI in VS Code?".
 *
 * The dashboard asks `/api/vscode/status?project=<slug>` before requesting a
 * Mode B terminal. A machine-wide client count is not enough: a companion for
 * another project does not mean this one can host anything, and a Restricted
 * window must fall back to an external terminal without a red error.
 *
 * Kept out of perun.js so `node --test` can cover it.
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.VSCodeLaunch = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    function clientsFor(status) {
        if (!status) return 0;
        // Older servers only report the machine-wide count.
        const value = status.project_clients !== undefined
            ? status.project_clients
            : status.connected_clients;
        return Number(value || 0);
    }

    function matchesProject(status, slug) {
        if (!status || !slug) return true;
        return !status.extension_project || status.extension_project === slug;
    }

    function shouldAttemptVscode(status, slug) {
        if (!status) return true;  // unknown: try, the caller has a fallback
        return clientsFor(status) > 0;
    }

    function fallbackReason(status, slug) {
        if (!status) return 'unknown';
        if (status.extension_trusted === false && matchesProject(status, slug)
                && clientsFor(status) === 0) {
            return 'untrusted';
        }
        if (clientsFor(status) === 0) return 'disconnected';
        return '';
    }

    return { shouldAttemptVscode, fallbackReason };
});
