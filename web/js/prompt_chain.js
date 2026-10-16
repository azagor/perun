/* Build the prompt text sent to the TUI from a task queue and an optional
   user prompt. Skill tasks are emitted first as `/<skillname>` slash commands
   so the OpenCode TUI recognises them; other tasks are appended as structured
   instructions.

   Kept in its own file so it can be unit-tested without the rest of the
   dashboard. */
const buildPromptChain = (taskQueue, prompt) => {
    prompt = String(prompt || '').trim();
    const parts = [];
    for (const t of taskQueue || []) {
        if (t.kind === 'skill') {
            const cmds = (t.skills || []).map((s) => `/${s}`).join('\n');
            let p = cmds || `[Task: ${t.name}]\n${t.prompt}`;
            if (t.prompt && cmds) p += `\n${t.prompt}`;
            parts.push(p);
        }
    }
    if (prompt) parts.push(prompt);
    for (const t of taskQueue || []) {
        if (t.kind === 'skill') continue;
        let p = `[Task: ${t.name}]\n${t.prompt}`;
        if (t.tools && t.tools.length) p += `\n\nAllowed tools: ${t.tools.join(', ')}`;
        if (t.skills && t.skills.length) p += `\n\nSkills to use: ${t.skills.join(', ')}`;
        parts.push(p);
    }
    return parts.join('\n\n---\n\n');
};

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { buildPromptChain };
}
