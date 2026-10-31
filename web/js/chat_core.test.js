// Run with: node --test web/js/chat_core.test.js
//
// These are the three decisions that produced real duplicate messages in the
// dashboard chat. The DOM/WebSocket plumbing is not covered here on purpose.
const test = require('node:test');
const assert = require('node:assert');
const core = require('./chat_core.js');

const row = (id, overrides = {}) => ({
    id,
    room_id: 'grp:perun:ai-playground:chat',
    sender: { agent_id: 'peruntheoverseer', runtime: 'peruntheoverseer-operator' },
    timestamp: 1793450143349,
    payload: 'hello',
    ...overrides,
});

test('a frame without an id is never rendered', () => {
    assert.equal(core.shouldRender([], { payload: 'x' }, 'grp:a'), false);
});

test('a fresh message in the joined room is rendered', () => {
    assert.equal(core.shouldRender([], row('msg_1'), 'grp:perun:ai-playground:chat'), true);
});

test('a frame for another room is ignored', () => {
    assert.equal(core.shouldRender([], row('msg_1', { room_id: 'grp:other' }), 'grp:perun:ai-playground:chat'), false);
});

test('a frame with no room_id is still rendered (control frames)', () => {
    assert.equal(core.shouldRender([], row('msg_1', { room_id: undefined }), 'grp:perun:ai-playground:chat'), true);
});

test('the same id is rendered once', () => {
    const msg = row('msg_1');
    assert.equal(core.shouldRender([msg], msg, 'grp:perun:ai-playground:chat'), false);
});

test('a replay with a fresh id but the same identity is collapsed', () => {
    const first = row('msg_1');
    const replay = row('msg_2');
    assert.equal(core.shouldRender([first], replay, 'grp:perun:ai-playground:chat'), false);
});

test('the same text at a different time is a new message', () => {
    const first = row('msg_1');
    const later = row('msg_2', { timestamp: 1793450149999 });
    assert.equal(core.shouldRender([first], later, 'grp:perun:ai-playground:chat'), true);
});

test('the same text from another sender is a new message', () => {
    const first = row('msg_1');
    const other = row('msg_2', { sender: { agent_id: 'frambo', runtime: 'frambo' } });
    assert.equal(core.shouldRender([first], other, 'grp:perun:ai-playground:chat'), true);
});

test('empty or whitespace text is never sent', () => {
    assert.equal(core.shouldSend('', 0, '   ', 1000), false);
    assert.equal(core.shouldSend('', 0, '', 1000), false);
});

test('the first send goes out', () => {
    assert.equal(core.shouldSend('', 0, 'hello', 1000), true);
});

test('an identical send inside the window is a double Enter/click', () => {
    assert.equal(core.shouldSend('hello', 1000, 'hello', 1500), false);
});

test('an identical send after the window is a deliberate repeat', () => {
    assert.equal(core.shouldSend('hello', 1000, 'hello', 2500), true);
});

test('a different text inside the window still goes out', () => {
    assert.equal(core.shouldSend('hello', 1000, 'world', 1100), true);
});

test('surrounding whitespace does not defeat the guard', () => {
    assert.equal(core.shouldSend('hello', 1000, '  hello  ', 1100), false);
});
