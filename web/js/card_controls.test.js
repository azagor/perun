// Run with: node --test web/js/card_controls.test.js
//
// The decision that produced the "active card shows Run" bug: a standalone TUI
// run had its Stop button suppressed while the Run button was always rendered.
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

test('a headless run is ours to stop', () => {
    const spec = controls.runControlSpec(headless);
    assert.equal(spec.kind, 'stop');
    assert.equal(spec.attr, 'data-stop');
    assert.equal(spec.uid, 'abc123');
});

test('a standalone TUI run can be finished, never stopped', () => {
    const spec = controls.runControlSpec(tui);
    assert.equal(spec.kind, 'finish');
    assert.equal(spec.attr, 'data-finish');
    assert.equal(spec.uid, 'def456');
    assert.match(spec.title, /terminal keeps running/);
});

test('no active run ever yields the Run control', () => {
    for (const run of [headless, tui, { standalone: true }, { standalone: false }]) {
        assert.notEqual(controls.runControlSpec(run).attr, 'data-tui');
        assert.notEqual(controls.runControlAttr(run), 'data-tui');
    }
});

test('standalone is truthy as a number or a boolean', () => {
    for (const run of [tui, { run_uid: 'x', standalone: true }]) {
        assert.equal(controls.runControlSpec(run).kind, 'finish');
    }
    for (const run of [headless, { run_uid: 'x', standalone: false }]) {
        assert.equal(controls.runControlSpec(run).kind, 'stop');
    }
});
