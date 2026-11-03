/* Pure decisions for the PerunChat dashboard log.
 *
 * Kept out of perun.js so they can be tested with `node --test` (no DOM). The
 * browser gets them as `window.ChatCore`; Node gets `module.exports`.
 *
 * The three bugs these guard against, all seen in production:
 *   - a frame for another room was appended to the joined room's log;
 *   - a replay after a reconnect re-delivered a frame under a fresh id;
 *   - a double Enter/click sent the same text twice (two ids, two rows).
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.ChatCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    /** Whether *msg* should be inserted into the log. */
    function shouldRender(existing, msg, joinedRoom) {
        if (!msg || !msg.id) return false;
        // The gateway broadcasts every room's frames to every socket.
        if (msg.room_id && joinedRoom && msg.room_id !== joinedRoom) return false;
        const rows = Array.isArray(existing) ? existing : [];
        if (rows.some((m) => m && m.id === msg.id)) return false;
        // Same message with a fresh id (reconnect/replay): same identity.
        const agent = msg.sender && msg.sender.agent_id;
        return !rows.some((m) => m
            && (m.sender && m.sender.agent_id) === agent
            && m.timestamp === msg.timestamp
            && String(m.payload) === String(msg.payload));
    }

    /** Whether this send is not a double Enter/click of the previous one. */
    function shouldSend(lastText, lastAt, text, now, windowMs) {
        const value = String(text == null ? '' : text).trim();
        if (!value) return false;
        const window = typeof windowMs === 'number' ? windowMs : 1200;
        return !(value === lastText && now - lastAt < window);
    }

    /** Proposal ids named in a message, unique and in first-seen order. */
    function proposalIds(text) {
        const seen = new Set();
        const out = [];
        const pattern = /\bprop_[0-9a-f]{12}\b/g;
        for (const match of String(text == null ? '' : text).matchAll(pattern)) {
            if (!seen.has(match[0])) {
                seen.add(match[0]);
                out.push(match[0]);
            }
        }
        return out;
    }

    /** The actions the dashboard may offer for a proposal in each state. */
    function proposalActions(status) {
        const value = String(status == null ? '' : status);
        if (value === 'pending') return ['approve-run', 'approve', 'reject', 'delete'];
        if (value === 'approved') return ['execute', 'withdraw', 'delete'];
        if (value === 'claimed') return ['revoke-claim'];
        if (value === 'executing') return ['cancel-run'];
        if (['executed', 'failed', 'rejected', 'cancelled', 'expired'].includes(value)) {
            return ['retry', 'delete'];
        }
        return [];
    }

    return { shouldRender, shouldSend, proposalIds, proposalActions };
});
