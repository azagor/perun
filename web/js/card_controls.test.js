// Run with: node --test web/js/card_controls.test.js
//
// The regression these guard against: a dashboard-launched TUI run stays
// `standalone=1` across a restart, and the card used to suppress Stop for it
// while Run stayed hardcoded - so an active card offered a second launch.
const test = require('node:test');
const assert = require('node:assert');
const controls = require('./card_controls.js');

const headless = { run_uid: 'abc123', project_slug: 'perun', standalone: 0 };
const tui = { run_uid: 'def456', project_slug: 'perun', standalone: 1 };

test('an idle card offers Run', () => {
    const spec = controls.runControlSpec(null);
    assert.equal(spec.kind, 'run');
    assert.equal(spec.attr, 'data-tui');
    assert.equal(spec.uid, '');
});

test('a headless run can be stopped', () => {
    const spec = controls.runControlSpec(headless);
    assert.equal(spec.kind, 'stop');
    assert.equal(spec.attr, 'data-stop');
    assert.equal(spec.uid, 'abc123');
});

test('a standalone TUI run can be stopped too (closes its terminal)', () => {
    const spec = controls.runControlSpec(tui);
    assert.equal(spec.kind, 'stop');
    assert.equal(spec.attr, 'data-stop');
    assert.equal(spec.uid, 'def456');
    assert.match(spec.title, /close this TUI terminal/i);
});

test('no active run ever yields the Run control', () => {
    for (const run of [headless, tui, { standalone: true }, { standalone: false }]) {
        assert.notEqual(controls.runControlSpec(run).attr, 'data-tui');
        assert.notEqual(controls.runControlAttr(run), 'data-tui');
    }
});

test('standalone is truthy as a number or a boolean', () => {
    for (const run of [tui, { run_uid: 'x', standalone: true }]) {
        assert.match(controls.runControlSpec(run).title, /TUI terminal/);
    }
    for (const run of [headless, { run_uid: 'x', standalone: false }]) {
        assert.equal(controls.runControlSpec(run).title, 'Cancel this run');
    }
});
