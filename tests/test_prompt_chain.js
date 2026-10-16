const assert = require('assert');
const { buildPromptChain } = require('../web/js/prompt_chain.js');

// User prompt only
assert.strictEqual(
    buildPromptChain([], 'Hello'),
    'Hello'
);

// Skill task emits slash command first
const skillTask = { kind: 'skill', name: 'Audit', prompt: 'check security', skills: ['code-audit'], tools: [] };
assert.strictEqual(
    buildPromptChain([skillTask], ''),
    '/code-audit\ncheck security'
);

// Skill task + user prompt: slash command comes first
assert.strictEqual(
    buildPromptChain([skillTask], 'Please run this'),
    '/code-audit\ncheck security\n\n---\n\nPlease run this'
);

// Non-skill task uses structured prompt and lists tools/skills
const toolTask = {
    kind: 'prompt',
    name: 'Fix',
    prompt: 'fix the bug',
    tools: ['edit'],
    skills: ['find-skills'],
};
const expected = `[Task: Fix]\nfix the bug\n\nAllowed tools: edit\n\nSkills to use: find-skills`;
assert.strictEqual(buildPromptChain([toolTask], ''), expected);

// Mixed queue: skill slash command first, then user prompt, then structured task
const mixed = buildPromptChain([skillTask, toolTask], 'user input');
assert.ok(mixed.startsWith('/code-audit\ncheck security'), 'skill slash command should come first');
assert.ok(mixed.includes('user input'), 'user prompt should be present');
assert.ok(mixed.includes('[Task: Fix]'), 'structured task should be present');

// Skill task without selected skills falls back to structured task text
const skillNoSkills = { kind: 'skill', name: 'Fallback', prompt: 'do something', skills: [], tools: [] };
assert.strictEqual(
    buildPromptChain([skillNoSkills], ''),
    '[Task: Fallback]\ndo something'
);

console.log('prompt_chain tests passed');
