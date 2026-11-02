// Run with: node --test web/js/coroutine_schedule.test.js
const test = require('node:test');
const assert = require('node:assert');
const schedule = require('./coroutine_schedule.js');

test('a daily cron reads as a local time', () => {
    assert.equal(schedule.humanTrigger({ trigger_type: 'cron', trigger_expr: '0 12 * * *' }),
        'Every day at 12:00');
    assert.equal(schedule.humanTrigger({ trigger_type: 'cron', trigger_expr: '30 9 * * 1-5' }),
        'Every weekday at 09:30');
    assert.equal(schedule.humanTrigger({ trigger_type: 'cron', trigger_expr: '15 8 * * 0' }),
        'Every Sunday at 08:15');
    assert.equal(schedule.humanTrigger({ trigger_type: 'cron', trigger_expr: '0 3 1 * *' }),
        'Monthly on day 1 at 03:00');
});

test('intervals read naturally', () => {
    assert.equal(schedule.humanTrigger({ trigger_type: 'interval', trigger_expr: '900' }),
        'Every 15 minutes');
    assert.equal(schedule.humanTrigger({ trigger_type: 'interval', trigger_expr: '7200' }),
        'Every 2 hours');
    assert.equal(schedule.humanTrigger({ trigger_type: 'interval', trigger_expr: '86400' }),
        'Every 1 day');
});

test('a room trigger names the room and mention rule', () => {
    const text = schedule.humanTrigger({
        trigger_type: 'event', event_source: 'room.message',
        event_room_id: 'grp:demo', event_mention_only: 1,
    });
    assert.match(text, /grp:demo/);
    assert.match(text, /mention only/);
});

test('presets generate five-field cron expressions', () => {
    assert.equal(schedule.presetCron('daily', '12:00'), '0 12 * * *');
    assert.equal(schedule.presetCron('weekdays', '09:30'), '30 9 * * 1-5');
    assert.equal(schedule.presetCron('weekly', '08:00', 0), '0 8 * * 0');
    assert.equal(schedule.presetCron('monthly', '03:15', 1), '15 3 1 * *');
});

test('the review sentence combines trigger and loop mode', () => {
    const text = schedule.triggerSentence({
        trigger_type: 'cron', trigger_expr: '0 12 * * *',
        execution_mode: 'loop', max_iterations: 10, scope: 'project',
    });
    assert.match(text, /Every day at 12:00/);
    assert.match(text, /loop of up to 10/);
});

test('wizard validation catches the important mistakes', () => {
    assert.match(schedule.validateStep(0, { name: '', execution_project_id: 1 }) || '', /name/i);
    assert.match(schedule.validateStep(0, { name: 'x', scope: 'project', execution_project_id: 0 }) || '', /project/i);
    assert.equal(schedule.validateStep(0, { name: 'x', scope: 'global', execution_project_id: 0 }), null);
    assert.match(schedule.validateStep(1, { prompt: '' }) || '', /prompt/i);
    assert.match(schedule.validateStep(2, { trigger_type: 'cron', trigger_expr: 'bad' }) || '', /five-field/i);
    assert.match(schedule.validateStep(2, {
        trigger_type: 'event', event_source: 'room.message', event_room_id: '',
    }) || '', /room/i);
    assert.match(schedule.validateStep(2, {
        scope: 'global', trigger_type: 'event', event_source: 'webhook',
    }) || '', /project-scoped/i);
    assert.match(schedule.validateStep(3, {
        execution_mode: 'loop', max_iterations: 0,
        max_runs_per_day: 0, cycle_max_seconds: 0,
    }) || '', /daily run cap/i);
    assert.equal(schedule.validateStep(1, { prompt: 'do it' }), null);
});

test('quick create supports a one-shot reminder', () => {
    const now = Date.parse('2026-11-02T10:00:00');
    // Daily keeps the cron shape used by the reminder template.
    assert.deepEqual(schedule.quickTrigger('daily', '09:30', '', now),
        { trigger_type: 'cron', trigger_expr: '30 9 * * *' });
    // Once produces an epoch and no cron expression.
    const once = schedule.quickTrigger('once', '', '2026-11-02T11:00:00', now);
    assert.equal(once.trigger_type, 'once');
    assert.equal(once.trigger_expr, '');
    assert.equal(once.run_at_epoch, Math.floor(Date.parse('2026-11-02T11:00:00') / 1000));
    // A past one-shot is rejected instead of creating a job that never fires.
    assert.equal(schedule.quickTrigger('once', '', '2026-11-02T09:00:00', now), null);
    assert.equal(schedule.quickTrigger('once', '', 'not-a-date', now), null);
});

test('countdown handles overdue and future timestamps', () => {
    assert.equal(schedule.countdown(Date.now() / 1000 - 5), 'due now');
    assert.match(schedule.countdown(Date.now() / 1000 + 3600), /^in /);
});
