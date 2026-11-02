/* Pure trigger helpers for the Coroutines dashboard.
 *
 * Kept out of perun.js so `node --test` can cover the human-readable schedule
 * text and wizard validation without a DOM. The browser gets it as
 * `window.CoroutineSchedule`; Node gets `module.exports`.
 */
(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    if (root) root.CoroutineSchedule = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
    const DAY_INDEX = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };
    const SOURCE_LABELS = {
        'room.message': 'a PerunChat room message',
        webhook: 'a webhook',
        'run.finished': 'a finished run',
        'proposal.approved': 'an approved proposal',
        'proposal.executed': 'an executed proposal',
        'attention.created': 'an operator-inbox item',
        'git.commit': 'a git commit',
        'git.push': 'a git push',
    };

    const pad = (n) => String(n).padStart(2, '0');
    const num = (v) => {
        const n = Number(v);
        return Number.isFinite(n) ? n : 0;
    };

    function fmtDate(epoch, tz) {
        const n = num(epoch);
        if (!n) return '—';
        const options = {
            weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit',
        };
        if (tz) options.timeZone = String(tz);
        try {
            return new Date(n * 1000).toLocaleString(undefined, options);
        } catch (_) {
            // An invalid IANA name falls back to the browser's local zone.
            delete options.timeZone;
            try { return new Date(n * 1000).toLocaleString(undefined, options); }
            catch (__) { return new Date(n * 1000).toString(); }
        }
    }

    function countdown(epoch) {
        const n = num(epoch);
        if (!n) return '';
        const delta = n - Date.now() / 1000;
        if (delta <= 0) return 'due now';
        const s = Math.round(delta);
        if (s < 60) return `in ${s}s`;
        const m = Math.floor(s / 60);
        if (m < 60) return `in ${m}m`;
        const h = Math.floor(m / 60);
        if (h < 48) return `in ${h}h ${m % 60}m`;
        return `in ${Math.floor(h / 24)}d ${h % 24}h`;
    }

    function intervalHuman(seconds) {
        const s = num(seconds);
        if (s % 86400 === 0 && s >= 86400) return `Every ${s / 86400} day${s === 86400 ? '' : 's'}`;
        if (s % 3600 === 0 && s >= 3600) return `Every ${s / 3600} hour${s === 3600 ? '' : 's'}`;
        if (s % 60 === 0 && s >= 60) return `Every ${s / 60} minute${s === 60 ? '' : 's'}`;
        if (s >= 60) return `Every ${Math.round(s / 60)} minutes`;
        return `Every ${s} seconds`;
    }

    function cronHuman(expr) {
        const parts = String(expr || '').trim().split(/\s+/);
        if (parts.length !== 5) return `Cron ${expr || '—'}`;
        const [min, hour, dom, mon, dow] = parts;
        const at = (m, h) => `${pad(num(h))}:${pad(num(m))}`;
        if (/^\d+$/.test(min) && /^\d+$/.test(hour) && mon === '*' && dom === '*' && dow === '*') {
            return `Every day at ${at(min, hour)}`;
        }
        if (/^\d+$/.test(min) && /^\d+$/.test(hour) && mon === '*' && dom === '*' && dow === '1-5') {
            return `Every weekday at ${at(min, hour)}`;
        }
        if (/^\d+$/.test(min) && /^\d+$/.test(hour) && mon === '*' && dom === '*' && /^\d+$/.test(dow)) {
            return `Every ${DAYS[num(dow) % 7]} at ${at(min, hour)}`;
        }
        if (/^\d+$/.test(min) && /^\d+$/.test(hour) && mon === '*' && /^\d+$/.test(dom) && dow === '*') {
            return `Monthly on day ${num(dom)} at ${at(min, hour)}`;
        }
        const every = String(min).match(/^\*\/(\d+)$/);
        if (every && hour === '*' && dom === '*' && mon === '*' && dow === '*') {
            return `Every ${every[1]} minutes`;
        }
        const everyHour = String(hour).match(/^\*\/(\d+)$/);
        if (everyHour && /^\d+$/.test(min) && dom === '*' && mon === '*' && dow === '*') {
            return `Every ${everyHour[1]} hours at minute ${pad(num(min))}`;
        }
        return `Cron ${expr}`;
    }

    function humanTrigger(job) {
        const j = job || {};
        switch (j.trigger_type) {
            case 'interval':
                return intervalHuman(j.trigger_expr);
            case 'cron':
                return cronHuman(j.trigger_expr);
            case 'once':
                return j.run_at_epoch ? `Once on ${fmtDate(j.run_at_epoch, j.timezone)}` : 'Once';
            case 'manual':
                return 'Manual — run on demand';
            case 'event': {
                const label = SOURCE_LABELS[j.event_source] || j.event_source || 'an event';
                if (j.event_source === 'room.message' && j.event_room_id) {
                    return `When ${label} arrives in ${j.event_room_id}${j.event_mention_only ? ' (mention only)' : ''}`;
                }
                return `When ${label} arrives`;
            }
            default:
                return 'Unknown trigger';
        }
    }

    function modeHuman(job) {
        const j = job || {};
        if (j.execution_mode !== 'loop') return 'Single run';
        const max = num(j.max_iterations);
        if (max === 0) return 'Continuous worker';
        return `Loop · up to ${max} iteration${max === 1 ? '' : 's'}`;
    }

    function triggerSentence(state) {
        const s = state || {};
        const mode = s.execution_mode === 'loop'
            ? (num(s.max_iterations) === 0
                ? 'as a continuous worker'
                : `as a loop of up to ${num(s.max_iterations)} iterations`)
            : 'as a single run';
        const when = humanTrigger(s);
        const where = s.scope === 'global' ? 'globally' : 'in the project';
        return `${when}, run ${mode} ${where}.`;
    }

    function presetCron(preset, time, day) {
        const [h, m] = String(time || '12:00').split(':').map(num);
        const hh = Math.max(0, Math.min(23, h || 0));
        const mm = Math.max(0, Math.min(59, m || 0));
        switch (preset) {
            case 'daily':
                return `${mm} ${hh} * * *`;
            case 'weekdays':
                return `${mm} ${hh} * * 1-5`;
            case 'weekly':
                return `${mm} ${hh} * * ${Math.max(0, Math.min(6, num(day)))}`;
            case 'monthly':
                return `${mm} ${hh} ${Math.max(1, Math.min(28, num(day) || 1))} * *`;
            default:
                return '';
        }
    }

    /**
     * Build the trigger fields for the quick-create dialog.
     * Returns null for an invalid/one-shot time in the past.
     */
    function quickTrigger(when, time, onceAtValue, nowMs) {
        const now = Number.isFinite(nowMs) ? nowMs : Date.now();
        if (when === 'once') {
            const ms = new Date(onceAtValue).getTime();
            if (!Number.isFinite(ms) || ms <= now) return null;
            return {
                trigger_type: 'once',
                trigger_expr: '',
                run_at_epoch: Math.floor(ms / 1000),
            };
        }
        const [h, m] = String(time || '12:00').split(':').map(num);
        return {
            trigger_type: 'cron',
            trigger_expr: `${m || 0} ${h || 0} * * *`,
        };
    }

    function validateStep(step, s) {
        const state = s || {};
        if (step === 0) {
            if (!String(state.name || '').trim()) return 'Give the coroutine a name.';
            if (state.scope !== 'global' && !state.execution_project_id) {
                return 'Choose the project the agent runs in.';
            }
        }
        if (step === 1 && !String(state.prompt || '').trim()) {
            return 'Write the prompt the agent should run.';
        }
        if (step === 2) {
            const trigger = state.trigger_type;
            if (state.scope === 'global' && trigger === 'event') {
                return 'Event triggers are project-scoped. Choose a scheduled, one-shot, or manual global job.';
            }
            if (trigger === 'interval') {
                const seconds = num(state.trigger_expr);
                if (seconds < 60) return 'The interval must be at least one minute.';
            } else if (trigger === 'cron') {
                if (String(state.trigger_expr || '').trim().split(/\s+/).length !== 5) {
                    return 'Use a five-field cron expression.';
                }
            } else if (trigger === 'once') {
                if (!num(state.run_at_epoch)) return 'Choose a date and time.';
                if (num(state.run_at_epoch) <= Date.now() / 1000) return 'Choose a time in the future.';
            } else if (trigger === 'event') {
                if (!state.event_source) return 'Choose an event source.';
                if (state.event_source === 'room.message' && !String(state.event_room_id || '').trim()) {
                    return 'Choose a PerunChat room.';
                }
            }
        }
        if (step === 3 && state.execution_mode === 'loop' && num(state.max_iterations) === 0) {
            if (num(state.max_runs_per_day) <= 0) return 'An unlimited loop needs a daily run cap.';
            if (num(state.cycle_max_seconds) <= 0) return 'An unlimited loop needs a cycle wall-clock cap.';
        }
        return null;
    }

    return {
        DAYS, SOURCE_LABELS, countdown, cronHuman, fmtDate, humanTrigger,
        intervalHuman, modeHuman, presetCron, quickTrigger, triggerSentence,
        validateStep,
    };
});
