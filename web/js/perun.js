/* ==========================================================================
   Perun — dashboard SPA
   Hash-routed, dependency-free. Talks to the FastAPI service under /api.
   ========================================================================== */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, attrs = {}, html = '') => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === 'class') n.className = v;
        else if (k === 'dataset') Object.assign(n.dataset, v);
        else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
        else if (v !== null && v !== undefined) n.setAttribute(k, v);
    }
    if (html) n.innerHTML = html;
    return n;
};
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
// Only allow href schemes we trust; anything else (e.g. javascript:) is dropped.
const safeUrl = (u) => {
    const s = String(u ?? '').trim();
    return /^(https?:|mailto:|vscode:\/\/file\/|\/|#|\.)/i.test(s) ? s : '#';
};

// Pure chat-log decisions live in js/chat_core.js (loaded just before this
// file) so they can be unit-tested without a DOM. If that script is missing —
// a stale cache or a truncated deploy — fall back to the permissive behaviour
// so the log still renders, and say so once in the console.
const ChatCore = window.ChatCore || {
    shouldRender: () => true,
    // Keep the "never send empty text" guard even without the module.
    shouldSend: (_lastText, _lastAt, text) => String(text == null ? '' : text).trim() !== '',
    // proposalIds / proposalActions are used by the chat and peek; without them
    // the fallback would throw on exactly the stale-cache path it exists for.
    proposalIds: () => [],
    proposalActions: () => [],
};
if (!window.ChatCore) {
    console.error('perun: chat_core.js did not load; chat dedupe is disabled');
}

const state = {
    projects: [],
    settings: null,
    health: null,
    activeRuns: [],
    // The server says whether it can raise another process's terminal window
    // (Windows-only today); older servers omit it, so default to "show".
    focusSupported: true,
    models: [],
    agents: [],
    skills: [],
    tools: [],
    runSource: null,
    activitySource: null,
    // Orchestrator chat continuity (session per dashboard visit).
    orchestrator: { sessionPk: null, ocSid: '' },
    update: null,
};

/* ---------------------------------------------------------------- api ---- */
async function loadTrustedAgents() {
    try {
        const data = await api('/api/perunchat/trusted');
        return data.trusted || [];
    } catch (e) { return []; }
}

async function api(path, opts = {}) {
    const init = { headers: {} };
    if (opts.method) init.method = opts.method;
    // State-changing API calls require the local token, injected into
    // index.html by the server (window.PERUN_TOKEN).
    if (window.PERUN_TOKEN) init.headers['X-Perun-Token'] = window.PERUN_TOKEN;
    if (opts.body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
        const detail = (data && (data.detail || data.reason)) || res.statusText;
        const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
        err.status = res.status;
        throw err;
    }
    return data;
}

/* ------------------------------------------------------------- toast ---- */
function toast(msg, kind = '') {
    const t = el('div', { class: 'toast ' + kind }, esc(msg));
    $('#toasts').appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; }, 3200);
    setTimeout(() => t.remove(), 3600);
}

/* ------------------------------------------------------------- modal ---- */
function modal(title, bodyNode, actions = []) {
    const root = $('#modal-root');
    root.innerHTML = '';
    const back = el('div', { class: 'modal-back' });
    const box = el('div', { class: 'modal' });
    box.appendChild(el('h3', {}, esc(title)));
    box.appendChild(bodyNode);
    const acts = el('div', { class: 'modal-actions' });
    for (const a of actions) {
        acts.appendChild(el('button', { class: 'btn ' + (a.class || ''), onclick: () => a.onClick(box) }, esc(a.label)));
    }
    box.appendChild(acts);
    back.appendChild(box);
    back.addEventListener('click', (e) => { if (e.target === back) root.innerHTML = ''; });
    root.appendChild(back);
    return box;
}
const closeModal = () => { $('#modal-root').innerHTML = ''; };

function confirmDialog(title, message, onConfirm, confirmLabel = 'Confirm') {
    const body = el('div', {}, `<p class="secondary">${esc(message)}</p>`);
    modal(title, body, [
        { label: 'Cancel', class: 'ghost', onClick: closeModal },
        { label: confirmLabel, class: 'danger', onClick: () => { closeModal(); onConfirm(); } },
    ]);
}

/* --------------------------------------------------- proposal references --- */
// Set before navigating to a project's Shared tab so the card can reveal and
// flash the exact row a chat message referenced.
let pendingProposalFocus = null;
// Chat history is re-rendered on reconnect/restore; one lookup per id is enough
// to colour the chips. The peek itself always fetches fresh state.
const proposalLookupCache = new Map();
const PROPOSAL_CACHE_MAX = 200;
function proposalLookup(proposalId) {
    if (!proposalLookupCache.has(proposalId)) {
        // Oldest-first eviction keeps a long-lived tab bounded even if a room
        // keeps inventing ids. Failures are not cached: a transient outage (or a
        // server still on an older build) must not pin the chip neutral forever.
        if (proposalLookupCache.size >= PROPOSAL_CACHE_MAX) {
            const oldest = proposalLookupCache.keys().next().value;
            proposalLookupCache.delete(oldest);
        }
        const pending = api(`/api/proposals/${encodeURIComponent(proposalId)}`)
            .then((res) => {
                proposalLookupCache.set(proposalId, Promise.resolve(res));
                return res;
            })
            .catch(() => {
                proposalLookupCache.delete(proposalId);
                return null;
            });
        proposalLookupCache.set(proposalId, pending);
    }
    return proposalLookupCache.get(proposalId);
}

// Reveal an already-rendered proposal row (the Shared card may already be on
// screen when another chat chip is clicked). Returns false when it is not there.
function focusProposalInShared(proposalId) {
    const target = document.querySelector(
        `.proposal-list [data-proposal="${CSS.escape(proposalId)}"]`);
    if (!target) return false;
    const completed = target.closest('.completed-proposals');
    if (completed) {
        const list = completed.querySelector('.list');
        if (list) list.classList.remove('hidden');
        const marker = completed.querySelector('.card-head span');
        if (marker) marker.textContent = '▾';
    }
    target.classList.add('proposal-focus');
    target.scrollIntoView({ block: 'center' });
    // Keep the marker: the URL keeps the proposal id, so the row is still the
    // one the operator came for while the card lives. Switching tabs clears it.
    return true;
}

function proposalBadge(status) {
    const name = String(status || 'unknown');
    const cls = name === 'executed' ? 'green' : name === 'failed' ? 'red'
        : ['approved', 'executing', 'claimed'].includes(name) ? 'blue'
        : ['rejected', 'cancelled', 'expired', 'deleted'].includes(name) ? 'muted' : 'amber';
    return `<span class="badge ${cls}">${esc(name)}</span>`;
}

function syncProposalChip(proposalId, status, projectName, expiresAt) {
    const active = ['pending', 'approved', 'claimed', 'executing'].includes(String(status));
    document.querySelectorAll(
        `.proposal-ref[data-proposal="${CSS.escape(proposalId)}"]`,
    ).forEach((chip) => {
        for (const cls of [...chip.classList]) {
            if (cls.startsWith('proposal-') && cls !== 'proposal-ref') {
                chip.classList.remove(cls);
            }
        }
        if (status) chip.classList.add(`proposal-${status}`);
        let title = `Proposal ${proposalId} — ${status || 'unknown'}`
            + (projectName ? ` · ${projectName}` : '');
        if (active && expiresAt) title += ` · ${timeLeft(expiresAt)} left`;
        chip.title = title;
    });
}

const PROPOSAL_ACTION_META = {
    'approve-run': ['Approve & run', 'primary', ''],
    'approve': ['Approve only', 'ghost', ''],
    'reject': ['Reject', 'ghost', ''],
    'delete': ['Delete', 'ghost danger', ''],
    'execute': ['Run now', 'primary', ''],
    'withdraw': ['Withdraw', 'ghost',
                 'Withdraw this approval; the record stays in the audit trail'],
    'revoke-claim': ['Revoke claim', 'ghost', ''],
    'cancel-run': ['Cancel', 'ghost', ''],
    'retry': ['Retry', 'primary', ''],
};

function proposalActionButtons(prop) {
    return ChatCore.proposalActions(prop && prop.status).map((act) => {
        const [label, cls, title] = PROPOSAL_ACTION_META[act] || [act, 'ghost', ''];
        return `<button class="btn sm ${cls}" data-act="${act}" type="button" title="${esc(title)}">${esc(label)}</button>`;
    });
}

async function runProposalAction(loaded, action, refresh, proposalId) {
    const prop = loaded.proposal || {};
    const project = loaded.project || {};
    const id = prop.id || proposalId;
    const slug = project.slug || '';
    const base = `/api/projects/${encodeURIComponent(slug)}/proposals/${encodeURIComponent(id)}`;
    const targets = (prop.targets || []).join(', ') || '(none)';
    const plans = {
        'approve-run': { url: `${base}/approve-and-execute`, method: 'POST',
                         ok: 'Approved — headless run started' },
        'approve': { url: `${base}/approve`, method: 'POST',
                     confirm: `Approve this proposal without starting it?\n\n${prop.intent}\n\nTargets: ${targets}`,
                     ok: 'Approved' },
        'reject': { url: `${base}/reject`, method: 'POST',
                    confirm: `Reject this proposal?\n\n${prop.intent}`,
                    ok: 'Rejected' },
        'withdraw': { url: `${base}/cancel`, method: 'POST',
                      body: { reason: 'withdrawn from chat peek' },
                      confirm: `Withdraw this approval?\n\n${prop.intent}\n\nThe proposal is cancelled for good; it stays in the audit trail.`,
                      ok: 'Withdrawn' },
        'execute': { url: `${base}/execute`, method: 'POST', ok: 'Headless run started' },
        'revoke-claim': { url: `${base}/revoke-claim`, method: 'POST', ok: 'Claim revoked' },
        'cancel-run': { url: `/api/runs/${encodeURIComponent(id)}/cancel`, method: 'POST',
                        confirm: `Cancel the running agent for this proposal?\n\n${prop.intent}`,
                        ok: 'Cancelling…' },
    };
    if (action === 'retry') {
        // Retry creates a *new* proposal; show that one rather than refreshing a
        // terminal card that the operator can no longer advance.
        try {
            const res = await api(`${base}/retry`, { method: 'POST' });
            toast('Retry proposal created', 'ok');
            proposalLookupCache.delete(id);
            const newId = ((res || {}).proposal || {}).id;
            closeModal();
            if (newId) openProposalPeek(newId);
        } catch (e) { toast(e.message, 'err'); }
        return;
    }
    if (action === 'delete') {
        if (!confirm(`Delete this proposal permanently?\n\n${prop.intent}`)) return;
        try {
            await api(base, { method: 'DELETE' });
            toast('Proposal deleted', 'ok');
            proposalLookupCache.delete(id);
            syncProposalChip(id, 'deleted', project.name);
            closeModal();
        } catch (e) { toast(e.message, 'err'); }
        return;
    }
    const plan = plans[action];
    if (!plan) return;
    if (plan.confirm && !confirm(plan.confirm)) return;
    try {
        await api(plan.url, { method: plan.method, body: plan.body });
        toast(plan.ok, 'ok');
        proposalLookupCache.delete(id);
        const fresh = await refresh();
        if (fresh) {
            syncProposalChip(id, (fresh.proposal || {}).status,
                             (fresh.project || {}).name,
                             (fresh.proposal || {}).expires_at);
        }
    } catch (e) { toast(e.message, 'err'); }
}

async function openProposalPeek(proposalId) {
    const body = el('div', {}, '<div class="empty"><span class="spinner"></span> loading proposal…</div>');
    let loaded = null;
    let firstLoad = true;
    const box = modal(`Proposal ${proposalId}`, body, [
        {
            label: 'Open in project',
            class: 'primary',
            onClick: () => {
                if (!loaded) return;
                closeModal();
                const slug = loaded.project.slug;
                const target = `#/project/${slug}/shared/${proposalId}`;
                if (location.hash === target && focusProposalInShared(proposalId)) return;
                pendingProposalFocus = proposalId;
                if (location.hash === target) {
                    // The Shared card is still loading: re-render it; the card's
                    // insertion hook consumes the pending focus.
                    viewProject(slug, 'shared');
                } else {
                    goto(target);
                }
            },
        },
        { label: 'Close', class: 'ghost', onClick: closeModal },
    ]);
    const openButton = box.querySelector('.modal-actions .btn.primary');
    if (openButton) {
        openButton.disabled = true;
        openButton.title = 'Waiting for the proposal…';
    }

    const render = () => {
        const prop = loaded.proposal || {};
        const project = loaded.project || {};
        const targets = prop.targets || [];
        const operations = Array.isArray(prop.operations) ? prop.operations : [];
        const operationHtml = operations.length
            ? operations.map((op) =>
                `<span class="chip"><b>${esc(String(op.op || 'op'))}</b> ${esc(String(op.path || ''))}</span>`
              ).join(' ')
            : '<span class="muted">(none)</span>';
        const targetHtml = targets.length
            ? targets.map((t) => `<span class="chip">${esc(String(t))}</span>`).join(' ')
            : '<span class="muted">(none)</span>';
        const changeHtml = operations.length
            ? `<span class="k">Operations</span><span class="v">${operationHtml}</span>`
            : `<span class="k">Targets</span><span class="v">${targetHtml}</span>`;
        const lines = [
            `<span class="k">Project</span><span class="v">${esc(project.name || project.slug || '?')}</span>`,
            `<span class="k">Status</span><span class="v">${proposalBadge(prop.status)}</span>`,
            `<span class="k">Requested by</span><span class="v">${esc(prop.requester || '?')}</span>`,
            `<span class="k">Intent</span><span class="v">${esc(prop.intent || '')}</span>`,
            changeHtml,
            `<span class="k">Created</span><span class="v">${esc(timeAgo(prop.created_at))}</span>`,
        ];
        if (prop.approved_by) {
            lines.push(`<span class="k">Approved by</span><span class="v">${esc(prop.approved_by)}</span>`);
        }
        if (prop.executed_by || prop.execution_mode) {
            lines.push(`<span class="k">Executed by</span><span class="v">${esc(prop.executed_by || prop.execution_mode)}</span>`);
        }
        if (prop.result) {
            lines.push(`<span class="k">Result</span><span class="v">${esc(String(prop.result).slice(0, 500))}</span>`);
        }
        if (['pending', 'approved', 'claimed', 'executing'].includes(String(prop.status))
            && prop.expires_at) {
            lines.push(`<span class="k">Expires</span><span class="v">${esc(timeLeft(prop.expires_at))} <span class="muted">(${esc(new Date(prop.expires_at * 1000).toLocaleString())})</span></span>`);
        }
        const details = prop.details && Object.keys(prop.details).length
            ? JSON.stringify(prop.details) : '';
        const actionButtons = proposalActionButtons(prop);
        const actionsHtml = actionButtons.length
            ? `<div class="row" style="gap:8px;margin-top:14px;flex-wrap:wrap">${actionButtons.join('')}</div>`
            : '';
        body.innerHTML = `<div class="kv">${lines.join('')}</div>` + actionsHtml
            + (details ? `<div class="meta" style="margin-top:10px;max-height:180px;overflow:auto;white-space:pre-wrap">${esc(details.slice(0, 3000))}</div>` : '');
        body.querySelectorAll('[data-act]').forEach((button) => {
            button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    await runProposalAction(loaded, button.dataset.act, refresh, proposalId);
                } finally {
                    if (button.isConnected) button.disabled = false;
                }
            });
        });
    };

    const refresh = async () => {
        try {
            loaded = await api(`/api/proposals/${encodeURIComponent(proposalId)}`);
        } catch (e) {
            if (!firstLoad && e.status === 404) {
                closeModal();
                syncProposalChip(proposalId, 'deleted');
                return null;
            }
            if (openButton) openButton.disabled = true;
            body.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
            return null;
        }
        firstLoad = false;
        if (openButton) {
            openButton.disabled = false;
            openButton.title = '';
        }
        render();
        return loaded;
    };

    await refresh();
}

/* --------------------------------------------------------- formatting --- */
function timeLeft(epochSeconds) {
    const seconds = Number(epochSeconds) - Date.now() / 1000;
    if (!Number.isFinite(seconds)) return '';
    if (seconds <= 0) return 'expired';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function timeAgo(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return '—';
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 60) return `${Math.floor(s)}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
}
/* Render a safe subset of Markdown (headings, lists, bold/italic, code,
   links). All text is escaped first, so untrusted manifest bodies cannot
   inject markup; only http(s)/mailto/relative link targets are honoured. */
function renderMarkdown(md) {
    if (!md) return '';
    const lines = String(md).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let inList = false, inCode = false;
    let codeBuf = [];
    const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
    const inline = (s) => esc(s)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>')
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, txt, url) =>
            (/^(https?:|mailto:|\/|#|\.)/i.test(url)
                ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`
                : `${txt} (${url})`));
    for (const raw of lines) {
        if (/^\s*```/.test(raw)) {
            if (inCode) { out.push('<pre>' + esc(codeBuf.join('\n')) + '</pre>'); codeBuf = []; inCode = false; }
            else { closeList(); inCode = true; }
            continue;
        }
        if (inCode) { codeBuf.push(raw); continue; }
        const h = raw.match(/^(#{1,6})\s+(.*)$/);
        if (h) { closeList(); const lvl = Math.min(h[1].length, 3); out.push(`<h${lvl}>${inline(h[2])}</h${lvl}>`); continue; }
        const li = raw.match(/^\s*[-*+]\s+(.*)$/);
        if (li) { if (!inList) { out.push('<ul>'); inList = true; } out.push(`<li>${inline(li[1])}</li>`); continue; }
        if (!raw.trim()) { closeList(); continue; }
        closeList(); out.push(`<p>${inline(raw)}</p>`);
    }
    if (inCode) out.push('<pre>' + esc(codeBuf.join('\n')) + '</pre>');
    closeList();
    return out.join('\n');
}

const KIND_BADGE = { opencode: 'gold', git: 'blue', dir: 'muted' };
const STATUS_BADGE = { done: 'green', queued: 'muted', running: 'amber', failed: 'red', cancelled: 'red', timeout: 'red' };

/* --------------------------------------------------------------- data --- */
async function loadProjects() {
    // Ask for the hidden control plane too; the server includes it only when
    // this installation has a configured operator (a name or a paired chat).
    // A fresh install therefore lists just the user's own projects.
    state.projects = (await api('/api/projects?include_hidden=true')).projects || [];
    // Projects drive which cards are awake; recompute so the pips update too.
    if (typeof computePresence === 'function') computePresence();
}
async function loadHealth() { state.health = await api('/api/health'); }
async function loadSettings() { state.settings = (await api('/api/settings')).settings; }

let _globalInstructionsDir = null;
async function loadGlobalInstructionsDir() {
    if (_globalInstructionsDir !== null) return _globalInstructionsDir;
    try {
        const res = await api('/api/instructions/dir');
        _globalInstructionsDir = res.dir || '';
    } catch (e) {
        console.error('loadGlobalInstructionsDir failed', e);
        _globalInstructionsDir = '';
    }
    return _globalInstructionsDir;
}

function instructionFileUrl(filename, isGlobal, projectPath) {
    const base = isGlobal ? (_globalInstructionsDir || '') : (projectPath || '');
    if (!base || !filename) return '#';
    const cleanBase = String(base).replace(/\\/g, '/').replace(/\/+$/, '');
    const cleanFile = String(filename).replace(/\\/g, '/').replace(/^\/+/, '');
    if (isGlobal) {
        return `vscode://file/${cleanBase}/${cleanFile}`;
    }
    return `vscode://file/${cleanBase}/.perun/instructions/${cleanFile}`;
}

function projectBySlug(slug) { return state.projects.find((p) => p.slug === slug); }

async function loadModels(force) {
    if (state.models.length && !force) return;
    try { state.models = (await api('/api/models')).models || []; } catch (_) { state.models = []; }
}
async function loadAgents(force) {
    if (state.agents.length && !force) return;
    try {
        const res = await api('/api/agents');
        state.agents = res.agents || [];
        if (!state.agents.length) state.agents = [
            { id: 'build', name: 'Build', mode: 'primary', description: 'Default coding agent' },
            { id: 'plan', name: 'Plan', mode: 'primary', description: 'Planning agent' },
            { id: 'general', name: 'General', mode: 'subagent', description: 'General-purpose research' },
            { id: 'explore', name: 'Explore', mode: 'subagent', description: 'Read-only exploration' },
        ];
    } catch (e) {
        console.error('loadAgents failed', e);
        state.agents = [
            { id: 'build', name: 'Build', mode: 'primary', description: 'Default coding agent' },
            { id: 'plan', name: 'Plan', mode: 'primary', description: 'Planning agent' },
            { id: 'general', name: 'General', mode: 'subagent', description: 'General-purpose research' },
            { id: 'explore', name: 'Explore', mode: 'subagent', description: 'Read-only exploration' },
        ];
    }
}
// Model entries are structured ({id, provider, name}); tolerate plain strings.
const modelId = (m) => (typeof m === 'string' ? m : (m && (m.id || m.label)) || '');
const nameFromPath = (p) => String(p || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || 'project';

let _cachedTerminals = null;
async function loadTerminals(force) {
    if (_cachedTerminals && !force) return _cachedTerminals;
    try {
        const res = await api('/api/terminals');
        state.terminals = res || { terminals: [], configured: '', configured_source: 'os', effective_path: '' };
        _cachedTerminals = state.terminals;
    } catch (e) {
        console.error('loadTerminals failed', e);
        _cachedTerminals = { terminals: [], configured: '', configured_source: 'os', effective_path: '' };
        state.terminals = _cachedTerminals;
    }
    return _cachedTerminals;
}
async function loadSkills(project) {
    const slug = project || (state.projects[0] && state.projects[0].slug) || '';
    try { state.skills = (await api(`/api/skills?project=${encodeURIComponent(slug)}`)).skills || []; }
    catch (e) { console.error('loadSkills failed', e); state.skills = []; }
}
async function loadTools(project) {
    const slug = project || (state.projects[0] && state.projects[0].slug) || '';
    try { state.tools = (await api(`/api/tools?project=${encodeURIComponent(slug)}`)).tools || []; }
    catch (e) { console.error('loadTools failed', e); state.tools = []; }
}
const terminalId = () => {
    // Console-level override takes precedence.
    const consoleSel = document.getElementById('c-terminal');
    if (consoleSel && consoleSel.value) return consoleSel.value;
    if (state.settings && state.settings.terminal) return state.settings.terminal;
    const t = state.terminals || _cachedTerminals || {};
    if (t.configured) return t.configured;
    const first = (t.terminals || [])[0];
    return first ? first.id : (navigator.platform.toLowerCase().startsWith('win') ? 'cmd' : 'sh');
};
// Commands are assembled server-side from these fields: the server quotes each
// value for the target shell, so a prompt can never be executed as shell code.
const launchInTerminal = async ({ cwd, model, agent, sessionId, prompt, tui, runUid, project }) => {
    try {
        const r = await api('/api/run-in-terminal', {
            method: 'POST',
            body: {
                project,
                cwd,
                prompt: prompt || '',
                model: model || '',
                agent: agent || '',
                session: sessionId || '',
                run_uid: runUid || '',
                tui: !!tui,
                shell: terminalId(),
            },
        });
        toast(tui ? `Opened ${r.shell} (TUI)` : `Opened ${r.shell}`, 'ok');
        return { ok: true, ...r };
    } catch (e) {
        toast(`Could not open terminal: ${e.message || 'unknown error'}`, 'err');
        // A server response (4xx/5xx) means the launch definitely failed. A
        // network error/timeout is uncertain: the terminal may have opened
        // before the response was lost, so the caller must not finalize the run.
        return { ok: false, definitive: !!(e && e.status), error: e };
    }
};
const copyCommandToClipboard = (cmd) => {
    const showModal = () => {
        const body = el('div', {}, `
            <p class="secondary">Paste this into your terminal to run the same session:</p>
            <textarea id="copied-cmd" readonly rows="3" style="margin-top:10px;background:var(--bg-inset);font-family:monospace">${esc(cmd)}</textarea>`);
        modal('Terminal command', body, [{ label: 'Close', class: 'ghost', onClick: closeModal }]);
        const ta = body.querySelector('#copied-cmd');
        ta.focus(); ta.select();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(cmd)
            .then(() => toast('Command copied — paste it into your terminal', 'ok'))
            .catch(showModal);
    } else {
        showModal();
    }
};

/* ============================================================= PERUN FX ==
   The storm layer. A lightweight canvas draws Perun's lightning and the
   escalating "eyes" keep the UI alive:
       calm  → nothing
       awake → a project was active recently (soft glow)
       storm → at least one agent run is executing (lightning)
   Purely cosmetic: it never captures input and is off under reduced motion. */
const AWAKE_WINDOW_MS = 15 * 60 * 1000;

const perunFx = (() => {
    const reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    let canvas = null, ctx = null, dpr = 1, w = 0, h = 0;
    let bolts = [];
    let raf = null;
    let ambientAt = 0;
    let level = 'calm';

    function init() {
        canvas = document.getElementById('perun-fx');
        if (!canvas || reduced) return;
        ctx = canvas.getContext('2d');
        resize();
        window.addEventListener('resize', resize);
    }
    function resize() {
        if (!canvas) return;
        dpr = Math.min(window.devicePixelRatio || 1, 2);
        w = window.innerWidth; h = window.innerHeight;
        canvas.width = Math.floor(w * dpr);
        canvas.height = Math.floor(h * dpr);
        canvas.style.width = w + 'px';
        canvas.style.height = h + 'px';
        if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    // One jagged polyline from (x, y0) down to y1.
    function makeBolt(x, y0, y1, spread, segs) {
        const pts = [{ x, y: y0 }];
        let px = x;
        for (let i = 1; i <= segs; i++) {
            const t = i / segs;
            px += (Math.random() - 0.5) * spread * (1 - t * 0.5);
            pts.push({ x: px, y: y0 + (y1 - y0) * t });
        }
        return pts;
    }
    function strike(x, y0, opts = {}) {
        if (!ctx || reduced) return;
        const y1 = opts.y1 != null ? opts.y1 : Math.min(h, y0 + (opts.len || 220) + Math.random() * 120);
        const main = makeBolt(x, y0, y1, opts.spread || 46, opts.segs || 9);
        const branches = [];
        const nBranch = opts.branches != null ? opts.branches : 2;
        for (let b = 0; b < nBranch; b++) {
            const p = main[2 + Math.floor(Math.random() * Math.max(1, main.length - 3))];
            if (!p) continue;
            const len = 40 + Math.random() * 90;
            const shift = (Math.random() < 0.5 ? -1 : 1) * (0.3 + Math.random() * 0.6);
            branches.push(makeBolt(p.x, p.y, p.y + len, (opts.spread || 46) * 0.6, 5)
                .map((q) => ({ x: q.x + shift * (q.y - p.y), y: q.y })));
        }
        bolts.push({ main, branches, born: performance.now(), life: opts.life || 440 });
        if (!raf) raf = requestAnimationFrame(frame);
    }
    function poly(pts, alpha, width, color) {
        ctx.beginPath();
        ctx.moveTo(pts[0].x, pts[0].y);
        for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
        ctx.lineWidth = width;
        ctx.strokeStyle = color;
        ctx.globalAlpha = alpha;
        ctx.stroke();
    }
    function frame(now) {
        raf = null;
        ctx.clearRect(0, 0, w, h);
        ctx.globalCompositeOperation = 'lighter';
        ctx.lineJoin = 'round'; ctx.lineCap = 'round';
        bolts = bolts.filter((b) => now - b.born < b.life);
        for (const b of bolts) {
            const a = Math.pow(1 - (now - b.born) / b.life, 1.6);
            ctx.shadowColor = 'rgba(228,196,106,.9)'; ctx.shadowBlur = 16;
            poly(b.main, a * 0.9, 3.2, '#fff3c4');
            poly(b.main, a * 0.5, 6.5, 'rgba(201,162,39,.55)');
            ctx.shadowBlur = 10;
            for (const br of b.branches) poly(br, a * 0.7, 1.6, '#e4c46a');
            ctx.shadowBlur = 0;
        }
        ctx.globalAlpha = 1;
        ctx.globalCompositeOperation = 'source-over';
        if (level === 'storm' && now > ambientAt && bolts.length < 4) {
            ambientAt = now + 1300 + Math.random() * 2600;
            strike(w * (0.12 + Math.random() * 0.76), -10,
                { len: h * (0.4 + Math.random() * 0.35), branches: 2 + Math.floor(Math.random() * 2) });
        }
        if (bolts.length || level === 'storm') raf = requestAnimationFrame(frame);
    }
    function setLevel(l) {
        const prev = level;
        level = l;
        if (!ctx || reduced) return;
        if (l === 'storm' && prev !== 'storm') {
            strike(w * 0.5, -10, { len: h * 0.5, branches: 3 });  // a welcoming volley
            ambientAt = performance.now() + 900;
        }
        if (!raf && (bolts.length || l === 'storm')) raf = requestAnimationFrame(frame);
    }
    function flash(originEl) {
        if (reduced) return;
        const node = document.createElement('div');
        node.className = 'perun-flash';
        document.body.appendChild(node);
        requestAnimationFrame(() => node.classList.add('go'));
        setTimeout(() => node.remove(), 620);
        if (originEl && originEl.getBoundingClientRect) {
            const r = originEl.getBoundingClientRect();
            strike(r.left + r.width / 2, r.top, { len: 150 + Math.random() * 140 });
        }
    }
    return { init, strike, setLevel, flash };
})();

/* --------------------------------------------------- storm presence ---- */
const presence = { working: new Set(), awake: new Set(), level: 'calm' };

function computePresence() {
    const prevWorking = presence.working;
    // Only projects the UI is allowed to list may light up. An installed copy
    // never lists the hidden control plane, so its runs must not surface here
    // either (the Orchestrator page still shows its own chat).
    const working = new Set();
    for (const r of CardControls.visibleRuns(state.activeRuns, state.projects)) {
        if (r.project_slug) working.add(r.project_slug);
    }
    const awake = new Set(working);
    const now = Date.now();
    for (const p of (state.projects || [])) {
        const la = p.last_activity && p.last_activity.ts ? Date.parse(p.last_activity.ts) : NaN;
        const seen = p.last_seen_at ? Date.parse(p.last_seen_at) : NaN;
        if ((Number.isFinite(la) && now - la < AWAKE_WINDOW_MS) || (Number.isFinite(seen) && now - seen < AWAKE_WINDOW_MS)) {
            awake.add(p.slug);
        }
    }
    presence.working = working;
    presence.awake = awake;
    presence.level = working.size ? 'storm' : (awake.size ? 'awake' : 'calm');
    document.body.dataset.storm = presence.level;
    perunFx.setLevel(presence.level);
    updateStormMeter();
    applyCardStates();
    renderWorkingNow($('#dash-working-now'));
    const statWorking = $('#dash-stat-working');
    if (statWorking) {
        statWorking.querySelector('.stat-value').textContent = presence.working.size;
        statWorking.querySelector('.stat-foot').textContent = presence.working.size ? 'agent(s) executing' : 'no active runs';
    }
    const statAwake = $('#dash-stat-awake');
    if (statAwake) {
        statAwake.querySelector('.stat-value').textContent = presence.awake.size;
    }
    // Lightning greets a project the moment its agent starts working.
    for (const slug of working) {
        if (prevWorking.has(slug)) continue;
        try {
            const card = document.querySelector(`.proj-card[data-slug="${CSS.escape(slug)}"]`);
            if (card) { const r = card.getBoundingClientRect(); perunFx.strike(r.left + r.width / 2, r.top - 8, { len: 170 }); }
        } catch (_) { /* CSS.escape unavailable — skip the flourish */ }
    }
}

const isWorking = (slug) => presence.working.has(slug);
const isAwake = (slug) => presence.awake.has(slug);

/* The three-level per-project state, in one place, so the card class, the pip
   class and the pip label can never drift apart. */
function pipState(slug) {
    const working = isWorking(slug);
    const awake = !working && isAwake(slug);
    return { working, awake, cls: working ? 'working' : (awake ? 'awake' : ''), label: working ? 'working' : (awake ? 'awake' : 'idle') };
}

/* A card's run control comes from js/card_controls.js (loaded just before this
   file) so the rule — an active card never offers Run — is unit-tested. If that
   script is missing (stale cache), keep the safe half: a working card still
   offers Stop rather than a second launch. */
const CardControls = window.CardControls || {
    runControlSpec: (run) => (run
        ? { kind: 'stop', uid: run.run_uid || '', cls: 'btn sm danger', attr: 'data-stop',
            label: '\u25a0 Stop', title: 'Cancel this run' }
        : { kind: 'run', uid: '', cls: 'btn sm primary', attr: 'data-tui',
            label: '\u25b6 Run', title: "Resume the latest TUI chat (use the caret for a new or other session)" }),
    runMenuSpec: (run) => (run ? null : {
        kind: 'run-menu', uid: '', cls: 'btn sm ghost', attr: 'data-tui-menu',
        label: '\u25be', title: 'Start a new session or choose an existing one',
    }),
    focusControlSpec: () => null,
    // Fallback for a stale cached card_controls.js. The module's gridPlan is
    // unit-tested; this keeps the grid usable if the script failed to load.
    gridPlan: (width, gap, count) => {
        width = Number(width) || 0;
        const rawGap = Number(gap);
        gap = Number.isFinite(rawGap) && rawGap >= 0 ? rawGap : 16;
        count = Number(count) || 0;
        if (width <= 0 || count <= 0) return { columns: 0, card: 0 };
        const maxCols = Math.max(1, Math.floor((width + gap) / (310 + gap)));
        const columns = Math.min(maxCols, count);
        const card = Math.min(width, 520,
            Math.floor((width - (columns - 1) * gap) / columns));
        return { columns, card };
    },
    visibleRuns: (runs, projects) => {
        const listed = new Set((projects || [])
            .map((p) => p && p.slug)
            .filter(Boolean));
        return (runs || []).filter(
            (r) => r && r.project_slug && listed.has(r.project_slug));
    },
};
if (!window.CardControls) {
    console.error('perun: card_controls.js did not load; card actions are degraded');
}

/* Session-choice rules come from js/session_pick.js (loaded just before this
   file). If it is missing, keep the safe rule: unknown/legacy sessions are
   resumable, a headless-only session is not auto-resumed. */
const SessionPick = window.SessionPick || {
    isResumeCandidate: (s) => !!s && s.tui !== false,
    latestResumeCandidate: (sessions) => (sessions || [])
        .find((s) => !!s && s.tui !== false) || null,
    originLabel: (s) => (s && s.tui === false ? 'headless run'
        : (s && s.tui === true ? 'TUI chat' : '')),
};
if (!window.SessionPick) {
    console.error('perun: session_pick.js did not load; session labels are degraded');
}

function runControlButton(spec) {
    const btn = el('button', { class: spec.cls, title: spec.title });
    btn.setAttribute(spec.attr, '');
    if (spec.uid) btn.dataset.runUid = spec.uid;
    btn.textContent = spec.label;
    return btn;
}

function applyCardStates() {
    document.querySelectorAll('.proj-card[data-slug]').forEach((card) => {
        const slug = card.dataset.slug;
        const ps = pipState(slug);
        card.classList.toggle('is-working', ps.working);
        card.classList.toggle('is-awake', ps.awake);
        const pip = card.querySelector('.card-pip');
        if (pip) {
            pip.classList.toggle('working', ps.working);
            pip.classList.toggle('awake', ps.awake);
            const lbl = pip.querySelector('[data-pip-label]');
            if (lbl) lbl.textContent = ps.label;
        }
        // Sync the running banner and the run control with the current active
        // run so cards update live without requiring a full page re-render.
        const run = ps.working ? (state.activeRuns || []).find((r) => r.project_slug === slug) : null;
        let banner = card.querySelector('.pc-running');
        if (run) {
            const promptText = esc((run.prompt || '').slice(0, 140));
            if (!banner) {
                banner = el('div', { class: 'pc-running' }, `⚡ <span class="pr-prompt">${promptText}</span>`);
                const meta = card.querySelector('.pc-meta');
                if (meta) card.insertBefore(banner, meta);
            } else {
                const span = banner.querySelector('.pr-prompt');
                if (span) span.textContent = run.prompt ? run.prompt.slice(0, 140) : '';
            }
        } else if (banner) {
            banner.remove();
        }
        syncRunControl(card, run);
    });
    // A banner or a new run control changes the natural height; equalize again.
    document.querySelectorAll('.proj-grid').forEach(layoutProjectGrid);
}

/* Keep a card's run control in step with *run* (null = idle) without recreating
   the button unless its kind or run actually changed. */
function syncRunControl(card, run) {
    const actions = card.querySelector('.pc-actions');
    if (!actions) return;
    const spec = CardControls.runControlSpec(run);
    const current = actions.querySelector('[data-tui], [data-stop]');
    const matches = current && current.hasAttribute(spec.attr)
        && (current.dataset.runUid || '') === spec.uid;
    if (!matches) {
        const fresh = runControlButton(spec);
        if (current) current.replaceWith(fresh);
        else actions.insertBefore(fresh, actions.firstElementChild || null);
    }
    // Focus is only offered for a tracked TUI run with a window to raise.
    const focusSpec = CardControls.focusControlSpec(run, { focusSupported: state.focusSupported });
    const currentFocus = actions.querySelector('[data-focus]');
    if (!focusSpec) {
        if (currentFocus) currentFocus.remove();
    } else if (!currentFocus || (currentFocus.dataset.runUid || '') !== focusSpec.uid) {
        const fresh = runControlButton(focusSpec);
        if (currentFocus) {
            currentFocus.replaceWith(fresh);
        } else {
            const anchor = actions.querySelector('[data-tui], [data-stop]');
            if (anchor) anchor.insertAdjacentElement('afterend', fresh);
            else actions.insertBefore(fresh, actions.firstElementChild || null);
        }
    }
    // The caret belongs to an idle card only. It is the new/choose menu; the
    // main Run is the resume-latest fast path.
    const menuSpec = CardControls.runMenuSpec(run);
    const currentMenu = actions.querySelector('[data-tui-menu]');
    if (!menuSpec) {
        if (currentMenu) currentMenu.remove();
    } else if (!currentMenu) {
        const fresh = runControlButton(menuSpec);
        const anchor = actions.querySelector('[data-tui]');
        if (anchor) anchor.insertAdjacentElement('afterend', fresh);
        else actions.insertBefore(fresh, actions.firstElementChild || null);
    }
}

function renderWorkingNow(container) {
    if (!container) return;
    container.innerHTML = '';
    // Match the project grid: a run whose project the UI may not list (the
    // hidden control plane on an installed copy) is not advertised here.
    const runs = CardControls.visibleRuns(state.activeRuns, state.projects);
    if (!runs.length) return;
    const refreshBtn = el('button', { class: 'btn sm ghost', type: 'button' }, '⟳ Refresh');
    refreshBtn.addEventListener('click', async () => {
        refreshBtn.disabled = true;
        try { await refreshPresence(); } finally { refreshBtn.disabled = false; }
    });
    container.appendChild(sectionTitle('Working now', refreshBtn));
    const box = el('div', { class: 'card' });
    const list = el('div', { class: 'list' });
    for (const r of runs) {
        const row = el('div', { class: 'list-row' },
            `<span class="badge amber">${esc(r.status)}</span>
             <span class="grow"><b>${esc(r.project_name || r.project_slug)}</b>
             <span class="muted"> — ${esc((r.prompt || '').slice(0, 90))}</span></span>
             <button class="btn sm ghost" data-run="${esc(r.run_uid)}" data-project="${esc(r.project_slug || '')}">View</button>`);
        list.appendChild(row);
    }
    box.appendChild(list);
    container.appendChild(box);
    box.querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', () => {
        const slug = b.dataset.project;
        goto(slug ? '#/console/' + slug : '#/console');
    }));
}

function updateStormMeter() {
    const txt = $('#storm-text');
    if (txt) {
        const n = presence.working.size;
        txt.textContent = n ? `${n} working` : (presence.awake.size ? 'awake' : 'calm');
    }
    const meter = $('#storm-meter');
    if (meter) {
        const n = presence.working.size;
        meter.title = n ? `${n} agent(s) working`
            : (presence.awake.size ? `${presence.awake.size} project(s) active in the last 15 min` : 'All quiet');
    }
}

async function refreshPresence() {
    try {
        const res = await api('/api/runs/active');
        state.activeRuns = res.runs || [];
        state.focusSupported = res.focus_supported !== false;
    } catch (_) {
        state.activeRuns = [];
    }
    computePresence();
}

/* ------------------------------------------------- model picker (combobox) -
   A text input with a search-filtered, grouped-by-provider dropdown. The input
   stays editable, so an arbitrary model id can still be typed by hand. Used for
   the orchestrator model and the default model. */
const _openSelects = new Set();
document.addEventListener('mousedown', (e) => {
    for (const wrap of Array.from(_openSelects)) {
        if (!wrap.contains(e.target)) wrap._close();
    }
});

function modelSelect(opts = {}) {
    const wrap = el('div', { class: 'mselect', id: opts.id || null });
    const input = el('input', {
        type: 'text', class: 'mselect-input', autocomplete: 'new-password', spellcheck: 'false',
        placeholder: opts.placeholder || 'provider/model',
    });
    input.value = opts.value || opts.initial || '';
    const toggle = el('button', { type: 'button', class: 'mselect-toggle', tabindex: '-1', title: 'Choose a model' }, '▾');
    const panel = el('div', { class: 'mselect-panel' });
    wrap.appendChild(input); wrap.appendChild(toggle); wrap.appendChild(panel);

    let isOpen = false;
    const refresh = () => {
        const q = input.value.trim().toLowerCase();
        const groups = new Map();
        for (const m of (state.models || [])) {
            const id = modelId(m);
            if (!id || (q && !id.toLowerCase().includes(q))) continue;
            const provider = (typeof m === 'object' && m.provider) || id.split('/')[0] || 'other';
            if (!groups.has(provider)) groups.set(provider, []);
            groups.get(provider).push(id);
        }
        panel.innerHTML = '';
        if (!groups.size) {
            panel.appendChild(el('div', { class: 'mselect-empty' }, (state.models || []).length ? 'No matching model' : 'No models detected'));
            return;
        }
        const current = input.value.trim();
        for (const [provider, ids] of groups) {
            panel.appendChild(el('div', { class: 'mselect-group' }, esc(provider)));
            for (const id of ids.slice(0, 200)) {
                const item = el('div', { class: 'mselect-item' + (id === current ? ' selected' : '') }, esc(id));
                item.addEventListener('mousedown', (ev) => {
                    ev.preventDefault();
                    input.value = id;
                    close();
                    if (opts.onSelect) opts.onSelect(id);
                });
                panel.appendChild(item);
            }
        }
    };
    const open = () => { if (isOpen) return; isOpen = true; refresh(); panel.style.display = 'block'; _openSelects.add(wrap); };
    const close = () => { isOpen = false; panel.style.display = 'none'; _openSelects.delete(wrap); };
    wrap._close = close;
    wrap.refresh = refresh;

    input.addEventListener('focus', open);
    input.addEventListener('input', () => { open(); refresh(); if (opts.onInput) opts.onInput(input.value.trim()); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    toggle.addEventListener('click', () => { if (isOpen) close(); else { input.focus(); open(); } });

    wrap.getValue = () => input.value.trim();
    wrap.setValue = (v) => { input.value = v || ''; close(); };
    wrap.input = input;
    return wrap;
}

function agentSelect(opts = {}) {
    const wrap = el('div', { class: 'mselect mselect-agent', id: opts.id || null });
    const input = el('input', {
        type: 'text', class: 'mselect-input', autocomplete: 'new-password', spellcheck: 'false',
        placeholder: opts.placeholder || 'agent',
    });
    input.value = opts.value || opts.initial || '';
    const toggle = el('button', { type: 'button', class: 'mselect-toggle', tabindex: '-1', title: 'Choose an agent' }, '▾');
    const panel = el('div', { class: 'mselect-panel' });
    wrap.appendChild(input); wrap.appendChild(toggle); wrap.appendChild(panel);

    let isOpen = false;
    const refresh = () => {
        const q = input.value.trim().toLowerCase();
        const entries = [];
        for (const a of (state.agents || [])) {
            const id = a.id || '';
            if (!id || (q && !id.toLowerCase().includes(q) && !(a.name || '').toLowerCase().includes(q))) continue;
            entries.push(a);
        }
        panel.innerHTML = '';
        if (!entries.length) {
            panel.appendChild(el('div', { class: 'mselect-empty' }, (state.agents || []).length ? 'No matching agent' : 'No agents detected'));
            return;
        }
        const current = input.value.trim();
        for (const a of entries) {
            const label = `${a.name || a.id}${a.mode ? ' · ' + a.mode : ''}`;
            const item = el('div', { class: 'mselect-item' + (a.id === current ? ' selected' : '') }, esc(label));
            item.title = a.description || '';
            item.addEventListener('mousedown', (ev) => {
                ev.preventDefault();
                input.value = a.id;
                close();
                if (opts.onSelect) opts.onSelect(a.id);
            });
            panel.appendChild(item);
        }
    };
    const open = () => { if (isOpen) return; isOpen = true; refresh(); panel.style.display = 'block'; _openSelects.add(wrap); };
    const close = () => { isOpen = false; panel.style.display = 'none'; _openSelects.delete(wrap); };
    wrap._close = close;
    wrap.refresh = refresh;

    input.addEventListener('focus', open);
    input.addEventListener('input', () => { open(); refresh(); if (opts.onInput) opts.onInput(input.value.trim()); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    toggle.addEventListener('click', () => { if (isOpen) close(); else { input.focus(); open(); } });

    wrap.getValue = () => input.value.trim();
    wrap.setValue = (v) => { input.value = v || ''; close(); };
    wrap.input = input;
    return wrap;
}

function multiSelect(opts = {}) {
    // opts.items: array of {value, label, description?}
    // opts.value: array of selected values
    const wrap = el('div', { class: 'mselect mselect-multi', id: opts.id || null });
    const tags = el('div', { class: 'mselect-tags' });
    const toggle = el('button', { type: 'button', class: 'mselect-toggle', tabindex: '-1', title: 'Choose options' }, '▾');
    const panel = el('div', { class: 'mselect-panel' });
    wrap.appendChild(tags); wrap.appendChild(toggle); wrap.appendChild(panel);

    let isOpen = false;
    let selected = new Set((opts.value || []).map(String));
    const _items = () => opts.items || [];

    const renderTags = () => {
        tags.innerHTML = '';
        for (const v of selected) {
            const it = _items().find((x) => String(x.value) === v);
            const label = it ? (it.label || it.value) : v;
            const tag = el('span', { class: 'mselect-tag' }, `${esc(label)} <span class="rm">×</span>`);
            tag.querySelector('.rm').addEventListener('click', (e) => {
                e.stopPropagation();
                selected.delete(v);
                renderTags(); refresh();
                if (opts.onChange) opts.onChange(Array.from(selected));
            });
            tags.appendChild(tag);
        }
        if (!selected.size) {
            tags.appendChild(el('span', { class: 'muted', style: 'font-size:.82rem;padding:3px 0' }, opts.placeholder || 'Select…'));
        }
    };

    let filterText = '';
    const searchInput = el('input', { type: 'text', class: 'mselect-input', placeholder: 'Search…', style: 'margin:4px;width:calc(100% - 16px)' });
    searchInput.addEventListener('input', () => { filterText = searchInput.value; refresh(); });
    searchInput.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    panel.appendChild(searchInput);

    const refresh = () => {
        const q = filterText.trim().toLowerCase();
        const children = Array.from(panel.children).filter((c) => c !== searchInput);
        children.forEach((c) => c.remove());
        const items = _items().filter((it) => !q || (it.label || it.value).toLowerCase().includes(q) || (it.description || '').toLowerCase().includes(q));
        if (!items.length) {
            panel.appendChild(el('div', { class: 'mselect-empty' }, _items().length ? 'No matching options' : 'No options'));
            return;
        }
        for (const it of items) {
            const val = String(it.value);
            const checked = selected.has(val);
            const row = el('label', { class: 'mselect-item' },
                `<input type="checkbox" ${checked ? 'checked' : ''}> <span>${esc(it.label || it.value)}${it.description ? ' · ' + esc(it.description) : ''}</span>`);
            const cb = row.querySelector('input');
            cb.addEventListener('change', () => {
                if (cb.checked) selected.add(val); else selected.delete(val);
                renderTags();
                if (opts.onChange) opts.onChange(Array.from(selected));
            });
            panel.appendChild(row);
        }
    };

    const open = () => {
        if (isOpen) return;
        // Close any other open multi-selects so their panels don't intercept clicks.
        for (const other of Array.from(_openSelects)) {
            if (other !== wrap && other._close) other._close();
        }
        isOpen = true;
        refresh();
        panel.style.display = 'block';
        _openSelects.add(wrap);
    };
    const close = () => { isOpen = false; panel.style.display = 'none'; _openSelects.delete(wrap); };
    wrap._close = close;
    wrap.refresh = () => { refresh(); renderTags(); };

    tags.addEventListener('click', (e) => { e.stopPropagation(); if (!isOpen) { filterText = ''; searchInput.value = ''; open(); } });
    toggle.addEventListener('click', (e) => { e.stopPropagation(); if (isOpen) close(); else { filterText = ''; searchInput.value = ''; open(); } });

    // Escape closes.
    searchInput.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); } });

    wrap.getValue = () => Array.from(selected);
    wrap.setValue = (arr) => { selected = new Set((arr || []).map(String)); renderTags(); refresh(); };
    wrap.setItems = (items) => { opts.items = items; renderTags(); if (isOpen) refresh(); };
    renderTags();
    return wrap;
}

/* ============================================================ ROUTER ==== */
const routes = {
    '#/dashboard': viewDashboard,
    '#/orchestrator': viewOrchestrator,
    '#/projects': viewProjects,
    '#/coroutines': viewCoroutines,
    '#/console': viewConsole,
    '#/memory': viewMemory,
    '#/activity': viewActivity,
    '#/mcp': viewMcp,
    '#/telegram': viewTelegram,
    '#/perunchat': viewPerunChat,
    '#/attention': viewAttention,
    '#/providers': viewProviders,
    '#/settings': viewSettings,
    '#/updates': viewUpdates,
};
let currentRoute = '';

function parseHash() {
    const h = location.hash || '#/dashboard';
    const parts = h.slice(2).split('/');
    return { base: '#/' + parts[0], sub: parts[1] || '', rest: parts.slice(2).join('/') };
}

function closeStreams() {
    if (state.runSource) { state.runSource.close(); state.runSource = null; }
    if (state.activitySource) { state.activitySource.close(); state.activitySource = null; }
    stopUpdatePoll();
    if (typeof stopCoroutinePoll === 'function') stopCoroutinePoll();
}

async function router() {
    const { base, sub, rest } = parseHash();
    closeStreams();
    let fn = routes[base];
    let args = [];
    if (base === '#/project') { fn = viewProject; args = [sub, rest]; }
    else if (base === '#/console' && sub) { args = [sub]; }
    else if (base === '#/coroutines' && sub) { args = [sub]; }
    if (!fn) { location.hash = '#/dashboard'; return; }
    currentRoute = location.hash;
    document.querySelectorAll('.nav-item').forEach((n) => {
        const r = n.dataset.route;
        n.classList.toggle('active', r === base || (base === '#/project' && r === '#/projects'));
    });
    try {
        await fn(...args);
    } catch (err) {
        $('#view').innerHTML = `<div class="empty">Failed to load: ${esc(err.message)}</div>`;
    }
}

function goto(hash) { location.hash = hash; }

/* ========================================================= DASHBOARD ==== */
async function viewDashboard() {
    $('#topbar-title').textContent = 'Dashboard';
    await Promise.all([loadProjects(), loadHealth()]);
    await refreshPresence();
    const v = $('#view');
    v.innerHTML = '';
    const h = state.health;
    const mem = await api('/api/memory/stats');
    const stats = mem.stats || {};

    const head = el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Overseer</div><h1>Command Dashboard</h1>
         <div class="sub">Every project, every agent, every memory — in one place.</div></div>`);
    v.appendChild(head);
    v.appendChild(el('div', { class: 'storm-legend' },
        `<span class="lg working"><i class="lg-eye"></i> Working — an agent is executing right now</span>
         <span class="lg awake"><i class="lg-eye"></i> Awake — active in the last 15 min</span>
         <span class="lg"><i class="lg-eye"></i> Idle — registered, quiet</span>`));
    v.appendChild(updateBanner());

    const grid = el('div', { class: 'grid stats-grid' });
    const stat = (label, value, foot, id) => el('div', { class: 'card stat', ...(id ? { id } : {}) },
        `<div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${esc(foot || '')}</div>`);
    const opencodeCount = state.projects.filter((p) => p.kind === 'opencode').length;
    let providerSummary;
    try {
        const pr = await api('/api/providers');
        const connected = (pr.providers || []).filter((p) => p.connected).length;
        const total = (pr.providers || []).length;
        const prOk = pr.ok && total > 0;
        providerSummary = stat('Providers', prOk ? `${connected}/${total}` : '—',
            prOk ? `${connected} connected, ${total - connected} not connected` : (pr.error || 'check opencode auth'),
            'dash-stat-providers');
    } catch (e) {
        providerSummary = stat('Providers', '—', 'unavailable', 'dash-stat-providers');
    }
    grid.appendChild(stat('Projects', state.projects.length, `${opencodeCount} OpenCode project(s)`, 'dash-stat-projects'));
    grid.appendChild(stat('Working now', presence.working.size, presence.working.size ? 'agent(s) executing' : 'no active runs', 'dash-stat-working'));
    grid.appendChild(stat('Awake', presence.awake.size, 'active in the last 15 min', 'dash-stat-awake'));
    grid.appendChild(stat('Facts', stats.facts_total ?? 0, `${stats.facts_global ?? 0} global`, 'dash-stat-facts'));
    grid.appendChild(stat('Doc chunks', stats.chunks_total ?? 0, 'semantic index', 'dash-stat-chunks'));
    grid.appendChild(providerSummary);
    grid.appendChild(stat('Embeddings', stats.ollama_ok ? 'Online' : 'Offline', stats.ollama_ok ? 'Ollama ready' : 'start ollama serve', 'dash-stat-embeddings'));
    v.appendChild(grid);

    // Usage — tokens and cost in the last 24 h, machine-wide and per project
    const usageCard = el('div', { class: 'card', id: 'dash-usage' },
        `<div class="card-head"><h4>Usage · captured in the last 24h</h4>
            <span class="meta" id="dash-usage-head">loading…</span>
            <button class="btn sm ghost" id="dash-usage-refresh" title="Re-read usage from OpenCode">⟳</button></div>
         <div id="dash-usage-body"><div class="meta">reading…</div></div>`);
    v.appendChild(usageCard);
    usageCard.querySelector('#dash-usage-refresh')?.addEventListener('click', async () => {
        const btn = usageCard.querySelector('#dash-usage-refresh');
        if (btn) btn.disabled = true;
        try {
            const r = await api('/api/usage/refresh', { method: 'POST', body: { hours: 24 * 7 } });
            toast(`Usage refreshed — ${r.updated || 0} session(s)`, 'ok');
        } catch (e) { toast(e.message, 'err'); }
        finally { if (btn) btn.disabled = false; }
        renderDashboardUsage();
    });
    let usageBusy = false;
    const renderDashboardUsage = async () => {
        if (usageBusy || !document.getElementById('dash-usage')) return;
        usageBusy = true;
        try {
            const r = await api('/api/usage?hours=24');
            const t = r.totals || {};
            const cost = Number(t.cost_usd || 0);
            const tok = Number(t.tokens_in || 0) + Number(t.tokens_out || 0);
            const partial = Number(t.incomplete || 0);
            $('#dash-usage-head').innerHTML =
                `<b>$${cost.toFixed(4)}</b> · ${tok.toLocaleString()} tok · ${Number(t.sessions || 0)} session(s)`
                + (partial ? ` · <span class="badge amber" title="Some snapshots did not reach the end of the session">${partial} partial</span>` : '');
            const rows = (r.by_project || []).map((p) =>
                `<div class="list-row"><span class="intent"><div class="msg truncate">${esc(p.slug)}</div>
                 <div class="meta">${Number(p.tokens || 0).toLocaleString()} tok</div></span>
                 <span class="mono">$${Number(p.cost || 0).toFixed(4)}</span></div>`).join('');
            const modelRows = (r.by_model || []).slice(0, 5).map((m) =>
                `<div class="list-row"><span class="intent"><div class="msg truncate">${esc(m.model)}</div>
                 <div class="meta">${Number(m.tokens || 0).toLocaleString()} tok</div></span>
                 <span class="mono">$${Number(m.cost || 0).toFixed(4)}</span></div>`).join('');
            $('#dash-usage-body').innerHTML = (rows
                || '<div class="meta">No usage captured yet — it is recorded when a run finishes (TUI runs when you finish or close them).</div>')
                + (modelRows ? `<div class="meta" style="margin-top:8px">By model</div>${modelRows}` : '');
        } catch (e) {
            // The view may have changed while the request was in flight.
            const body = $('#dash-usage-body');
            if (body) body.innerHTML = `<div class="meta">usage unavailable: ${esc(e.message)}</div>`;
        } finally {
            usageBusy = false;
        }
    };
    renderDashboardUsage();
    if (dashboardUsageTimer) clearInterval(dashboardUsageTimer);
    dashboardUsageTimer = setInterval(() => {
        if (!document.getElementById('dash-usage')) {
            clearInterval(dashboardUsageTimer);
            dashboardUsageTimer = null;
            return;
        }
        renderDashboardUsage();
    }, USAGE_REFRESH_MS);

    // Working now — live runs, each mapped to its project
    const workingNowBox = el('div', { id: 'dash-working-now' });
    renderWorkingNow(workingNowBox);
    v.appendChild(workingNowBox);

    // Projects grid
    const projActions = el('div', { class: 'row', style: 'gap:6px' });
    projActions.appendChild(el('button', { class: 'btn sm ghost', onclick: () => goto('#/projects') }, 'Manage →'));
    const hcBtn = el('button', { class: 'btn sm ghost', id: 'btn-health-check' }, '🩺 Health check & fix');
    hcBtn.addEventListener('click', async () => {
        hcBtn.disabled = true;
        try {
            const r = await api('/api/health/check', { method: 'POST', body: {} });
            if (r.ok) {
                toast('Health check passed — all fixes applied', 'ok');
            } else {
                const issues = (r.issues || []).join(', ');
                toast(`Fixes applied, remaining issues: ${issues || 'see details'}`, issues ? 'warn' : 'ok');
            }
        } catch (e) {
            toast(e.message, 'err');
        } finally {
            hcBtn.disabled = false;
            await refreshHealth();
        }
    });
    projActions.appendChild(hcBtn);
    v.appendChild(sectionTitle('Projects', projActions));
    v.appendChild(projectGrid(state.projects.slice(0, 8)));

    // Recent activity
    const act = await api('/api/activity?limit=12');
    v.appendChild(sectionTitle('Recent activity'));
    v.appendChild(activityCard(act.activity || []));

    v.querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', () => {
        const slug = b.dataset.project;
        goto(slug ? '#/console/' + slug : '#/console');
    }));
}

function sectionTitle(text, extra) {
    const s = el('div', { class: 'section-title' }, `<h3>${esc(text)}</h3><span class="rule"></span>`);
    if (extra) s.appendChild(extra);
    return s;
}

function layoutProjectGrid(grid) {
    // Size every card to one width, using the maximum column count that fits
    // (the previous first-row size). A partly filled last row starts at the
    // first column; no rebalancing.
    if (!grid || !grid.isConnected) return;
    const width = grid.clientWidth;
    if (!width) return;
    const styles = getComputedStyle(grid);
    const gap = parseFloat(styles.columnGap || styles.gap) || 16;
    const count = grid.querySelectorAll(':scope > .proj-card').length;
    if (!count) return;
    // A stale cached card_controls.js may predate gridPlan; keep the grid
    // usable (one column, capped) rather than throwing.
    const plan = (typeof CardControls.gridPlan === 'function')
        ? CardControls.gridPlan(width, gap, count)
        : { columns: 1, card: Math.min(width, 520) };
    if (!plan.columns) return;
    grid.style.setProperty('--proj-card-w', `${plan.card}px`);
    // Flex lines size independently; give every card the tallest card's height
    // so a short row (PerunChat alone) matches the first row.
    grid.style.removeProperty('--proj-card-h');
    let tallest = 0;
    grid.querySelectorAll(':scope > .proj-card').forEach((node) => {
        tallest = Math.max(tallest, node.getBoundingClientRect().height);
    });
    if (tallest) grid.style.setProperty('--proj-card-h', `${Math.ceil(tallest)}px`);
}

function projectGrid(projects) {
    if (!projects.length) return el('div', { class: 'empty' }, 'No projects yet — run discovery to register your projects.');
    const grid = el('div', { class: 'grid proj-grid' });
    for (const p of projects) grid.appendChild(projectCard(p));
    requestAnimationFrame(() => layoutProjectGrid(grid));
    return grid;
}

window.addEventListener('resize', () => {
    document.querySelectorAll('.proj-grid').forEach(layoutProjectGrid);
});

// Fonts can change text height after the first layout; re-measure once loaded.
if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
        document.querySelectorAll('.proj-grid').forEach(layoutProjectGrid);
    });
}

function projectCard(p) {
    const isControl = !!p.is_control;
    const ps = pipState(p.slug);
    const stateCls = ps.working ? ' is-working' : (ps.awake ? ' is-awake' : '');
    const badge = isControl ? 'gold' : (KIND_BADGE[p.kind] || 'muted');
    const badgeText = isControl ? 'orchestrator' : p.kind;
    const fav = p.favorite ? '★ ' : '';
    const delBtn = isControl ? '' :
        `<button class="btn sm danger ghost" data-del title="Remove from Perun">✕</button>`;
    const pip = `<span class="card-pip ${ps.cls}">
            <span class="pip-eyes"><i class="pip-eye"></i><i class="pip-eye"></i></span>
            <span class="pip-bolt">⚡</span>
            <span data-pip-label>${ps.label}</span>
         </span>`;
    const run = ps.working ? (state.activeRuns || []).find((r) => r.project_slug === p.slug) : null;
    const running = run ? `<div class="pc-running">${run.standalone ? '🖥' : '⚡'} <span class="pr-prompt">${esc((run.prompt || '').slice(0, 140))}</span>${run.standalone ? ' <span class="muted">TUI session</span>' : ''}</div>` : '';
    // Never render Run next to a working card: the control belongs to the run.
    const runBtn = runControlButton(CardControls.runControlSpec(run)).outerHTML;
    const runMenuSpec = CardControls.runMenuSpec(run);
    const runMenuBtn = runMenuSpec ? runControlButton(runMenuSpec).outerHTML : '';
    // Focus is only meaningful for a tracked TUI run (it raises its window).
    const focusSpec = CardControls.focusControlSpec(run, { focusSupported: state.focusSupported });
    const focusBtn = focusSpec ? runControlButton(focusSpec).outerHTML : '';
    const card = el('div', { class: 'card clickable proj-card' + stateCls, dataset: { slug: p.slug } },
        `<div class="pc-top">
            <div><div class="pc-name">${fav}${esc(p.name)}</div><div class="pc-path">${esc(p.path)}</div></div>
            <div class="pc-flags">${pip}</div>
         </div>
         <div class="pc-desc">${esc(p.description || 'No description.')}</div>
         ${running}
         <div class="pc-meta">
            <span class="badge ${badge}">${esc(badgeText)}</span>
             ${(p.ide || {}).vscode_linked ? '<span class="badge green">VS Code</span>' : ''}
             ${p.work_paused ? '<span class="badge amber">paused</span>' : ''}
            <span>◧ ${p.session_count || 0} sessions</span>
            <span>◉ ${p.memory_count || 0} facts</span>
            <span>▤ ${p.chunk_count || 0} chunks</span>
         </div>
         <div class="pc-actions">
            ${runBtn}
            ${runMenuBtn}
            ${focusBtn}
            <button class="btn sm" data-open>Open</button>
            ${(p.ide || {}).vscode_linked ? '<button class="btn sm ghost" data-vscode title="Open in VS Code">VS Code</button>' : ''}
            <button class="btn sm ghost" data-index>Index docs</button>
            ${delBtn}
         </div>`);
    card.addEventListener('click', (e) => {
        const stop = e.target.closest('[data-stop]');
        if (stop) { e.stopPropagation(); cancelProjectRun(stop.dataset.runUid); return; }
        const focus = e.target.closest('[data-focus]');
        if (focus) { e.stopPropagation(); focusProjectTerminal(focus.dataset.runUid); return; }
        if (e.target.closest('[data-del]')) { e.stopPropagation(); deleteProject(p); return; }
        if (e.target.closest('[data-index]')) { indexProject(p); return; }
        if (e.target.closest('[data-vscode]')) {
            e.stopPropagation();
            openProjectVsCode(p);
            return;
        }
        if (e.target.closest('[data-tui-menu]')) {
            e.stopPropagation();
            openTuiMenu(p);
            return;
        }
        if (e.target.closest('[data-tui]')) {
            e.stopPropagation();
            const btn = e.target.closest('[data-tui]');
            // The session lookup is a network round-trip; a second click would
            // launch a second terminal. Disable the button until it settles.
            if (btn.disabled) return;
            btn.disabled = true;
            Promise.resolve(resumeLatestProjectTui(p))
                .catch(() => { /* already toasted inside the launch flow */ })
                .finally(() => {
                    // A successful launch re-renders the card; only re-enable if
                    // this element is still on the page.
                    if (btn.isConnected) btn.disabled = false;
                });
            return;
        }
        goto('#/project/' + p.slug);
    });
    return card;
}

async function fetchProjectSessions(p) {
    try {
        const res = await api(`/api/projects/${encodeURIComponent(p.slug)}/sessions`);
        return (res.sessions || []).filter(
            (s) => String(s.opencode_session_id || '').trim());
    } catch (_) {
        return [];
    }
}

/* One terminal launch per project at a time. The session lookup is a network
   round-trip, and the card can be re-rendered (or the caret used) while it is
   in flight; a second click must never open a second terminal. */
const tuiLaunchInFlight = new Set();

function openResumedSession(p, session) {
    return openProjectTui(p, {
        sessionId: session.opencode_session_id,
        sessionPk: session.id,
        sessionTitle: session.title,
    });
}

async function resumeLatestProjectTui(p) {
    // The fast path: the newest **TUI chat**, or a fresh session when the only
    // sessions were created by headless jobs. A machine session stays available
    // through the caret's explicit picker.
    if (!p || tuiLaunchInFlight.has(p.slug)) return;
    const sessions = await fetchProjectSessions(p);
    const latest = SessionPick.latestResumeCandidate(sessions);
    if (latest) return openResumedSession(p, latest);
    return openProjectTui(p, {});
}

function chooseProjectSession(p, sessions) {
    const body = el('div');
    const list = el('div', { class: 'list' });
    for (const session of sessions) {
        const origin = SessionPick.originLabel(session);
        const row = el('div', { class: 'list-row' },
            `<span class="grow"><div class="msg truncate">${esc(session.title || '(untitled)')}</div>
             <div class="meta">${esc(timeAgo(session.updated_at))} · ${esc(session.status || '')}${origin ? ' · ' + esc(origin) : ''} · ${esc(session.opencode_session_id || '')}</div></span>`);
        const resume = el('button', { class: 'btn sm primary' }, 'Resume');
        resume.addEventListener('click', () => {
            closeModal();
            openResumedSession(p, session);
        });
        row.appendChild(resume);
        list.appendChild(row);
    }
    body.appendChild(list);
    modal(`Sessions · ${p.name || p.slug}`, body, [
        { label: 'New session', class: 'primary', onClick: () => { closeModal(); openProjectTui(p, {}); } },
        { label: 'Cancel', class: 'ghost', onClick: closeModal },
    ]);
}

async function openTuiMenu(p) {
    const sessions = await fetchProjectSessions(p);
    const latest = SessionPick.latestResumeCandidate(sessions);
    const body = el('div');
    const add = (label, desc, cls, onClick) => {
        const row = el('button', {
            class: 'btn ' + cls,
            style: 'display:block;width:100%;text-align:left;margin:6px 0;white-space:normal',
        }, `<b>${esc(label)}</b>${desc ? `<div class="meta">${esc(desc)}</div>` : ''}`);
        row.addEventListener('click', () => { closeModal(); onClick(); });
        body.appendChild(row);
    };
    if (latest) {
        add('Resume latest',
            `${latest.title || '(untitled)'} · ${timeAgo(latest.updated_at)}`,
            'primary', () => openResumedSession(p, latest));
    }
    add('Start a new session',
        latest ? 'Open a fresh OpenCode session' : 'No previous session for this project',
        latest ? 'ghost' : 'primary', () => openProjectTui(p, {}));
    if (sessions.length) {
        add('Choose an existing session…', `${sessions.length} session(s)`, 'ghost',
            () => chooseProjectSession(p, sessions));
    }
    add('Run in VS Code terminal', 'TUI inside VS Code — direct selection hand-off', 'ghost',
        () => openProjectTui(p, { launchMode: 'vscode-terminal' }));
    add('Run in external terminal', 'Windows Terminal — VS Code still follows edits', 'ghost',
        () => openProjectTui(p, { launchMode: 'external-terminal' }));
    modal(`Run · ${p.name || p.slug}`, body, [
        { label: 'Cancel', class: 'ghost', onClick: closeModal },
    ]);
}

async function openProjectTui(p, opts = {}) {
    if (!p || !p.path) {
        toast('This project has no folder path. Re-register or rediscover it.', 'err');
        return;
    }
    // One launch per project at a time: the card may be re-rendered (or the
    // caret used) while the session lookup is in flight.
    if (tuiLaunchInFlight.has(p.slug)) return;
    tuiLaunchInFlight.add(p.slug);
    try {
        const sessionId = String(opts.sessionId || '').trim();
        const sessionPk = opts.sessionPk || null;
        const sessionTitle = String(opts.sessionTitle || '').trim();
        // A resumed TUI opens `opencode --session <id>`; a new one behaves like
        // running `opencode` in the directory.
        const res = await api('/api/runs/tui', {
            method: 'POST',
            body: {
                project: p.slug, model: p.model, agent: p.agent,
                opencode_session_id: sessionId,
                session_pk: sessionPk,
                prompt: sessionTitle ? `Resumed: ${sessionTitle}` : '',
            },
        });
        const runUid = res.run && res.run.run_uid;
        if (!runUid) { throw new Error('Perun did not return a run id'); }
        // A "new session" run now carries the OpenCode session id Perun created,
        // so the TUI opens it and usage is attributable from the first message.
        const openedSession = sessionId || String((res.run && res.run.opencode_session_id) || '');
        const ide = p.ide || {};
        const mode = String(opts.launchMode || ide.launch_mode || 'external-terminal');
        if (mode === 'vscode-terminal') {
            try {
                await api(`/api/runs/${runUid}/vscode/open`, { method: 'POST' });
                toast(sessionId ? 'Resumed in VS Code' : 'Started in VS Code', 'ok');
                await refreshPresence();
                return;
            } catch (err) {
                toast('VS Code companion not connected — opening an external terminal', 'err');
            }
        }
        const opened = await launchInTerminal({
            project: p.slug, cwd: p.path, model: p.model, agent: p.agent,
            tui: true, runUid, sessionId: openedSession,
        });
        if (!opened || !opened.ok) {
            // The run row was created before the launch. A definitive failure
            // means no terminal exists, so finish it now. An uncertain failure
            // (network/timeout) may have opened one: leave the run reachable by
            // Stop instead of untracking a live terminal.
            if (opened && opened.definitive) {
                try { await api(`/api/runs/${runUid}/finish`, { method: 'POST' }); }
                catch (_) { /* finalization is best effort */ }
            }
            await refreshPresence();
            return;
        }
        if (ide.vscode_linked) {
            // Linked projects open (or focus) their VS Code workspace alongside
            // the external TUI. Edit-follow and selection are handled by the
            // companion; a missing extension degrades to plain VS Code.
            try {
                await api(`/api/projects/${encodeURIComponent(p.slug)}/vscode/open`, {
                    method: 'POST', body: {},
                });
            } catch (_) { /* VS Code is optional */ }
        }
        toast(sessionId ? 'Resumed session' : 'Started a new session', 'ok');
        await refreshPresence();
        if (runUid) {
            // Optimistically show working state so the card lights up immediately,
            // including the control that belongs to the new run.
            const card = document.querySelector(`.proj-card[data-slug="${CSS.escape(p.slug)}"]`);
            if (card) {
                card.classList.add('is-working');
                card.classList.remove('is-awake');
                const pip = card.querySelector('.card-pip');
                if (pip) { pip.classList.add('working'); pip.classList.remove('awake'); }
                const lbl = pip && pip.querySelector('[data-pip-label]');
                if (lbl) lbl.textContent = 'working';
                syncRunControl(card, res.run || { run_uid: runUid, project_slug: p.slug, standalone: 1 });
            }
        }
    } catch (e) { toast(e.message, 'err'); }
    finally { tuiLaunchInFlight.delete(p.slug); }
}

async function focusProjectTerminal(runUid) {
    if (!runUid) return;
    try {
        const r = await api(`/api/runs/${runUid}/terminal/focus`, { method: 'POST' });
        if (!r.focused) {
            toast(r.reason || 'Could not focus that terminal', 'err');
        } else if (r.ambiguous) {
            toast('Several terminals match - click Focus again for the next one');
        }
    } catch (e) { toast(e.message, 'err'); }
}

async function openProjectVsCode(p) {
    if (!p || !p.slug) return;
    try {
        await api(`/api/projects/${encodeURIComponent(p.slug)}/vscode/open`, {
            method: 'POST', body: {},
        });
        toast('Opening VS Code…', 'ok');
    } catch (e) {
        toast(e.message, 'err');
    }
}

async function refreshVSCodeStatus() {
    const node = document.getElementById('pc-vscode-status');
    if (!node) return;
    try {
        const s = await api('/api/vscode/status');
        const parts = [];
        const untrusted = s.extension_trusted === false;
        if (!s.detected) {
            parts.push('VS Code not detected');
        } else {
            parts.push(`VS Code ${s.code_version || ''}`.trim());
            parts.push(s.extension_installed
                ? `companion ${s.extension_version || 'installed'}`
                : 'companion not installed');
            if (untrusted) parts.push('workspace untrusted (Restricted Mode)');
        }
        if (s.connected_clients) parts.push(`${s.connected_clients} window(s) connected`);
        node.innerHTML = esc(parts.join(' · '));
        if (untrusted) {
            const trustBtn = el('button', { class: 'btn sm ghost', type: 'button', style: 'margin-left:8px' },
                'How to trust');
            trustBtn.addEventListener('click', showVSCodeTrustHelp);
            node.appendChild(trustBtn);
        }
        if (s.detected && !s.extension_installed && s.install_command) {
            const btn = el('button', { class: 'btn sm ghost', type: 'button', style: 'margin-left:8px' },
                'Copy install command');
            btn.addEventListener('click', async () => {
                try {
                    await navigator.clipboard.writeText(s.install_command);
                    toast('Install command copied', 'ok');
                } catch (_) { toast(s.install_command, 'ok'); }
            });
            node.appendChild(btn);
        }
    } catch (_) {
        node.textContent = 'companion status unavailable';
    }
}

function showVSCodeTrustHelp() {
    const body = el('div', {},
        `<p class="secondary">VS Code is running this folder in <b>Restricted Mode</b>.
         Perun cannot grant trust — that is a decision only you can make in VS Code.</p>
         <ol class="secondary" style="margin:10px 0 0 18px;line-height:1.7">
            <li>In the VS Code window, click <b>Yes, I trust the authors</b> in the Restricted Mode banner.</li>
            <li>If the banner is gone, run <b>Workspaces: Manage Workspace Trust</b> from the Command Palette,
                then trust this folder.</li>
            <li>The companion reconnects immediately — no window reload is needed.</li>
         </ol>
         <p class="muted" style="margin-top:10px">Opening the dashboard in VS Code does not change trust.</p>`);
    modal('Trust the VS Code workspace', body, [
        { label: 'Close', class: 'ghost', onClick: closeModal },
    ]);
}

async function cancelProjectRun(runUid) {
    if (!runUid) return;
    // Find the card that owns this run so we update the right one when multiple
    // projects are working.
    let card = null;
    for (const btn of document.querySelectorAll('[data-stop]')) {
        if (btn.dataset.runUid === runUid) {
            card = btn.closest('.proj-card');
            break;
        }
    }
    // Optimistically remove the working state from the affected card so the UI
    // feels instant, then let the next poll confirm it.
    if (card) {
        card.classList.remove('is-working');
        const pip = card.querySelector('.card-pip');
        if (pip) { pip.classList.remove('working'); pip.classList.add('awake'); }
        const banner = card.querySelector('.pc-running');
        if (banner) banner.remove();
        const stopBtn = card.querySelector('[data-stop]');
        if (stopBtn) stopBtn.remove();
        const focusBtn = card.querySelector('[data-focus]');
        if (focusBtn) focusBtn.remove();
    }
    try {
        // /cancel ends both kinds of run: it cancels the orchestrator's run and,
        // for a TUI session Perun launched, closes the terminal it opened.
        await api(`/api/runs/${runUid}/cancel`, { method: 'POST' });
        toast('Stop requested', 'ok');
        await new Promise((r) => setTimeout(r, 500));
        await refreshPresence();
        await loadProjects();
    } catch (e) { toast(e.message, 'err'); }
}

function deleteProject(p) {
    confirmDialog('Remove project',
        `Remove “${p.name}” from Perun? Its memory and indexed docs are deleted. Files on disk are untouched.`,
        async () => {
            try {
                await api(`/api/projects/${p.slug}`, { method: 'DELETE' });
                toast('Removed from Perun', 'ok');
                await loadProjects();
                if (location.hash.startsWith('#/project/')) goto('#/projects'); else router();
            } catch (e) { toast(e.message, 'err'); }
        }, 'Remove');
}

async function indexProject(p) {
    try {
        const res = await api(`/api/projects/${p.slug}/index`, { method: 'POST' });
        toast(`Indexed ${res.chunks || 0} chunk(s) from ${(res.files || []).length} file(s)`, 'ok');
        loadProjects();
    } catch (e) { toast(e.message, 'err'); }
}

function activityCard(items) {
    const box = el('div', { class: 'card' });
    if (!items.length) { box.appendChild(el('div', { class: 'empty' }, 'No activity yet.')); return box; }
    const list = el('div', { class: 'list' });
    for (const a of items) {
        list.appendChild(el('div', { class: 'list-row' },
            `<span class="badge muted">${esc(a.kind)}</span>
             <span class="grow msg truncate">${esc(a.message)}</span>
             <span class="ts">${esc(timeAgo(a.ts))}</span>`));
    }
    box.appendChild(list);
    return box;
}

/* ========================================================== PROJECTS ==== */
async function viewProjects() {
    $('#topbar-title').textContent = 'Projects';
    await loadProjects();
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Registry</div><h1>Projects</h1>
          <div class="sub">${state.projects.length} registered. Perun can start agents and search docs in any of them.</div></div>
         <div class="row wrap">
            <input type="text" id="proj-filter" placeholder="Filter…" style="width:200px">
            <button class="btn ghost" id="btn-discover">⟳ Discover</button>
            <button class="btn primary" id="btn-add">＋ Add project</button>
         </div>`));

    const container = el('div');
    v.appendChild(container);
    const render = (filter = '') => {
        const f = filter.toLowerCase();
        const list = state.projects.filter((p) =>
            !f || p.name.toLowerCase().includes(f) || p.slug.includes(f) || p.path.toLowerCase().includes(f));
        container.innerHTML = '';
        container.appendChild(projectGrid(list));
    };
    render();

    $('#proj-filter').addEventListener('input', (e) => render(e.target.value));
    $('#btn-add').addEventListener('click', projectWizard);
    $('#btn-discover').addEventListener('click', async () => {
        try {
            const res = await api('/api/discover', { method: 'POST', body: {} });
            toast(`Discovered: ${(res.created || []).length} new, ${(res.updated || []).length} updated`, 'ok');
            await loadProjects();
            render($('#proj-filter').value);
        } catch (e) { toast(e.message, 'err'); }
    });
}

/* ---------------------------------------- participant editor (shared) ----
   The trust list is the pool: participants are a subset of it, and every chip
   cycles rw → ro → removed. The add field trusts a brand-new agent on the spot
   (the server rejects participants that are not trusted) and selects it rw, so
   adding someone never means leaving the page. Used by both the project wizard
   and the Shared tab so the two cannot drift. */
function participantEditor(opts = {}) {
    const trusted = ((opts.trusted || []).map((a) => String(a).toLowerCase()).filter(Boolean));
    const roleMap = new Map();
    for (const item of (opts.initial || [])) {
        const id = String(item && item.agent_id ? item.agent_id : item || '').toLowerCase();
        if (!id) continue;
        const roles = (item && item.roles) || (item && item.role ? [item.role] : []);
        roleMap.set(id, roles.includes('rw') ? 'rw' : (roles.includes('ro') ? 'ro' : null));
        if (!trusted.includes(id)) trusted.push(id);
    }

    const wrap = el('div', { class: 'participant-editor' });
    const pool = el('div', { class: 'trust-pool' });
    const row = el('div', { class: 'participant-add' });
    const input = el('input', {
        type: 'text', autocomplete: 'new-password', spellcheck: 'false',
        placeholder: 'agent id, e.g. frambo_malina_omarchy',
    });
    const addBtn = el('button', { type: 'button', class: 'btn sm ghost' }, '＋ Add participant');
    row.appendChild(input);
    row.appendChild(addBtn);
    wrap.appendChild(pool);
    wrap.appendChild(row);
    wrap.appendChild(el('p', { class: 'muted participant-hint' },
        'Adding an agent trusts it on PerunChat first. Click a chip to cycle rw → ro → removed.'));

    const getValue = () => Array.from(roleMap.entries())
        .filter(([, role]) => !!role)
        .map(([agent_id, role]) => ({ agent_id, roles: [role] }));
    const notify = () => { if (opts.onChange) opts.onChange(getValue()); };

    const render = () => {
        pool.innerHTML = '';
        if (!trusted.length) {
            pool.appendChild(el('span', { class: 'muted' }, 'No trusted agents yet — add one below.'));
            return;
        }
        for (const agent of trusted) {
            const role = roleMap.get(agent) || null;
            const chip = el('span', {
                class: 'trust-chip' + (role ? ' role-' + role : ''),
                'data-agent': agent,
                title: role === 'rw' ? 'read-write participant (click for read-only)'
                    : role === 'ro' ? 'read-only participant (click to remove)'
                        : 'not a participant (click to add read-write)',
            }, `${esc(agent)}<span class="role">${role || '—'}</span>`);
            chip.addEventListener('click', () => {
                const current = roleMap.get(agent) || null;
                roleMap.set(agent, current === null ? 'rw' : (current === 'rw' ? 'ro' : null));
                render();
                notify();
            });
            pool.appendChild(chip);
        }
    };

    const adopt = (id) => {
        const clean = String(id || '').trim().toLowerCase();
        if (!clean) return;
        if (!/^[a-z0-9][a-z0-9._-]{1,63}$/.test(clean)) {
            toast('Agent ids use letters, numbers, dot, dash and underscore', 'err');
            return;
        }
        if (!trusted.includes(clean)) trusted.push(clean);
        roleMap.set(clean, 'rw');
    };

    const add = async () => {
        const raw = input.value.trim();
        if (!raw) return;
        const id = raw.toLowerCase();
        if (!/^[a-z0-9][a-z0-9._-]{1,63}$/.test(id)) {
            toast('Agent ids use letters, numbers, dot, dash and underscore', 'err');
            return;
        }
        if (!trusted.includes(id)) {
            addBtn.disabled = true;
            try {
                if (opts.onTrust) await opts.onTrust(id);
                else await api('/api/perunchat/trusted', { method: 'POST', body: { agent_id: id } });
                if (!trusted.includes(id)) trusted.push(id);
            } catch (e) {
                toast(e.message || 'Could not trust agent', 'err');
                addBtn.disabled = false;
                return;
            }
            addBtn.disabled = false;
        }
        adopt(id);
        input.value = '';
        render();
        notify();
    };
    addBtn.addEventListener('click', add);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); add(); }
    });

    render();
    wrap.getValue = getValue;
    wrap.setValue = (list) => {
        roleMap.clear();
        for (const item of (list || [])) {
            const id = String(item && item.agent_id ? item.agent_id : item || '').toLowerCase();
            if (!id) continue;
            const roles = (item && item.roles) || (item && item.role ? [item.role] : []);
            roleMap.set(id, roles.includes('rw') ? 'rw' : (roles.includes('ro') ? 'ro' : null));
            if (!trusted.includes(id)) trusted.push(id);
        }
        render();
    };
    wrap.setTrusted = (list) => {
        for (const id of (list || [])) {
            const clean = String(id || '').toLowerCase();
            if (clean && !trusted.includes(clean)) trusted.push(clean);
        }
        render();
    };
    return wrap;
}

/* ---------------------------------------------------- project wizard ---- */
function projectWizard() {
    loadModels();
    const wiz = {
        step: 0, method: '',
        path: '', clone_url: '',
        name: '', kind: 'opencode', description: '', tags: '',
        model: '', agent: '',
        bootstrap: true, git_init: false, git_remote: '', git_branch: 'main',
        index_docs: false,
        // Opt-in: a generated .code-workspace is a new VS Code trust scope, so
        // creating a project must not link it unless the operator asks.
        vscode_link: false,
        shared: false, participants: [], external_deps: [],
    };
    const STEPS = ['Method', 'Location', 'Details', 'Review'];

    const bind = (pane) => {
        const sync = (e) => {
            const t = e.target;
            const f = t && t.dataset ? t.dataset.field : null;
            if (!f) return;
            wiz[f] = t.type === 'checkbox' ? t.checked : t.value;
        };
        pane.addEventListener('input', sync);
        pane.addEventListener('change', sync);
    };

    const methodStep = () => `
        <p class="secondary">How would you like to add this project?</p>
        <div class="method-cards">
            <button type="button" class="method-card" data-method="existing">
                <div class="mc-icon">◧</div><div class="mc-title">Existing folder</div>
                <div class="mc-sub">Register a project that already lives on disk.</div></button>
            <button type="button" class="method-card" data-method="new">
                <div class="mc-icon">＋</div><div class="mc-title">Create new folder</div>
                <div class="mc-sub">Make a directory and bootstrap a starter project.</div></button>
            <button type="button" class="method-card" data-method="clone">
                <div class="mc-icon">⧉</div><div class="mc-title">Clone repository</div>
                <div class="mc-sub">Clone a remote Git repository into a new folder.</div></button>
        </div>`;

    const locationStep = () => {
        let html = '';
        if (wiz.method === 'clone') {
            html += `<label class="field"><span class="lab">Repository URL</span>
                <input type="text" data-field="clone_url" value="${esc(wiz.clone_url)}" placeholder="https://github.com/you/repo.git"></label>`;
        }
        html += `<label class="field"><span class="lab">${wiz.method === 'new' ? 'New folder path' : 'Folder path'}</span>
            <div class="row" style="gap:8px"><input type="text" data-field="path" value="${esc(wiz.path)}" style="flex:1"
                placeholder="${wiz.method === 'new' ? 'C:\\my_apps\\new_project' : 'C:\\my_apps\\existing'}">
                <button class="btn ghost" type="button" data-browse>Browse…</button></div></label>`;
        if (wiz.method === 'new') {
            html += `<div class="row" style="gap:14px;align-items:center">
                <button class="btn sm ghost" type="button" data-mkdir>＋ Create folder now</button>
                <span class="muted">or Perun creates it when you finish</span></div>`;
        } else if (wiz.method === 'existing') {
            html += `<p class="muted mt">Perun can register a path that does not exist yet — it is created lazily on first use.</p>`;
        }
        return html;
    };

    const detailsStep = () => {
        return `
        <div class="form-row">
            <label class="field"><span class="lab">Name</span><input type="text" data-field="name" value="${esc(wiz.name)}" placeholder="My Project"></label>
            <label class="field"><span class="lab">Kind</span><select data-field="kind">
                <option value="opencode" ${wiz.kind === 'opencode' ? 'selected' : ''}>OpenCode project</option>
                <option value="git" ${wiz.kind === 'git' ? 'selected' : ''}>Git repository</option>
                <option value="dir" ${wiz.kind === 'dir' ? 'selected' : ''}>Plain directory</option>
            </select></label>
        </div>
        <label class="field"><span class="lab">Description</span><input type="text" data-field="description" value="${esc(wiz.description)}" placeholder="optional"></label>
        <div class="form-row">
            <label class="field"><span class="lab">Default model</span><span id="wiz-model-host" class="select-host"></span></label>
            <label class="field"><span class="lab">Agent</span><span id="wiz-agent-host" class="select-host"></span></label>
        </div>
        <label class="field"><span class="lab">Tags</span><input type="text" data-field="tags" value="${esc(wiz.tags)}" placeholder="python, trading (optional)"></label>
        <div class="wiz-options">
            ${wiz.method === 'new' ? `<label class="opt"><input type="checkbox" data-field="bootstrap" ${wiz.bootstrap ? 'checked' : ''}> Bootstrap starter files (README, AGENTS.md, opencode.json)</label>` : ''}
            ${wiz.method !== 'clone' ? `<label class="opt"><input type="checkbox" data-field="git_init" ${wiz.git_init ? 'checked' : ''}> Initialise a Git repository</label>` : ''}
            <label class="opt"><input type="checkbox" data-field="index_docs" ${wiz.index_docs ? 'checked' : ''}> Index documentation now</label>
            <label class="opt"><input type="checkbox" data-field="vscode_link" ${wiz.vscode_link ? 'checked' : ''}> Link with Visual Studio Code (generate a workspace)</label>
            <p class="muted" style="font-size:.75rem;margin:2px 0 0">Off by default: a generated workspace file is a new VS Code trust scope.</p>
        </div>
        ${wiz.method !== 'clone' ? `
        <div class="form-row">
            <label class="field"><span class="lab">Git remote (optional)</span><input type="text" data-field="git_remote" value="${esc(wiz.git_remote)}" placeholder="https://github.com/you/repo.git"></label>
            <label class="field"><span class="lab">Branch</span><input type="text" data-field="git_branch" value="${esc(wiz.git_branch)}" placeholder="main"></label>
        </div>` : ''}
        <hr style="margin:18px 0;border-color:var(--border)">
        <label class="opt"><input type="checkbox" data-field="shared" ${wiz.shared ? 'checked' : ''}> <b>Enable shared project (PerunChat)</b></label>
        <div class="shared-section ${wiz.shared ? '' : 'hidden'}">
            <h4>Participants</h4>
            <p class="muted" style="font-size:.78rem">Only trusted agents can be participants. New agents are trusted automatically when you add them.</p>
            <div id="wiz-participants"></div>
        </div>`;
    };

    const reviewStep = () => {
        const rows = [
            ['Method', { existing: 'Existing folder', new: 'New folder', clone: 'Clone repository' }[wiz.method] || wiz.method],
            ['Path', wiz.path || '—'],
            wiz.method === 'clone' ? ['Repository', wiz.clone_url || '—'] : null,
            ['Name', wiz.name || nameFromPath(wiz.path)],
            ['Kind', wiz.kind],
            ['Model', wiz.model || '(inherit default)'],
            ['Git', wiz.method === 'clone' ? 'cloned from remote'
                : (wiz.git_init ? `init on ${wiz.git_branch || 'main'}` : (wiz.git_remote ? 'remote configured' : 'none'))],
            ['Index docs', wiz.index_docs ? 'yes' : 'no'],
            ['VS Code', wiz.vscode_link ? 'linked (VS Code terminal)' : 'not linked'],
            ['Shared project', wiz.shared ? `yes (${wiz.participants.length} participant(s))` : 'no'],
        ].filter(Boolean);
        return `<p class="secondary">Review and create.</p>
            <div class="kv">${rows.map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${esc(v)}</span>`).join('')}</div>`;
    };

    const validate = () => {
        if (wiz.step === 0) return wiz.method ? null : 'Choose how to add the project';
        if (wiz.step === 1) {
            if (wiz.method === 'clone') {
                if (!wiz.clone_url) return 'Repository URL is required';
                if (!wiz.path) return 'Choose a target folder';
            } else if (!wiz.path) return 'Folder path is required';
        }
        return null;
    };

    let trustedAgents = [];
    const render = async () => {
        const body = el('div', { class: 'wizard' });
        body.appendChild(el('div', { class: 'wiz-steps' }, STEPS.map((s, i) =>
            `<span class="wiz-step ${i === wiz.step ? 'active' : ''} ${i < wiz.step ? 'done' : ''}"><b>${i + 1}</b> ${esc(s)}</span>`).join('')));
        const pane = el('div', { class: 'wiz-pane' });
        if (wiz.step === 2 && !trustedAgents.length) {
            trustedAgents = await loadTrustedAgents();
        }
        if (wiz.step === 2) {
            // Both pickers need their option sources before they can open with
            // a useful list; they are cheap and cached after the first call.
            await Promise.all([loadModels(), loadAgents()]);
        }
        pane.innerHTML = [methodStep, locationStep, detailsStep, reviewStep][wiz.step]();
        body.appendChild(pane);

        if (wiz.step === 2) {
            const modelHost = pane.querySelector('#wiz-model-host');
            if (modelHost) {
                modelHost.appendChild(modelSelect({
                    value: wiz.model,
                    placeholder: 'Inherit global default',
                    onInput: (v) => { wiz.model = v; },
                    onSelect: (v) => { wiz.model = v; },
                }));
            }
            const agentHost = pane.querySelector('#wiz-agent-host');
            if (agentHost) {
                agentHost.appendChild(agentSelect({
                    value: wiz.agent,
                    placeholder: 'Inherit project default',
                    onInput: (v) => { wiz.agent = v; },
                    onSelect: (v) => { wiz.agent = v; },
                }));
            }
            const participantsHost = pane.querySelector('#wiz-participants');
            if (participantsHost) {
                participantsHost.appendChild(participantEditor({
                    initial: wiz.participants,
                    trusted: trustedAgents,
                    onTrust: async (id) => {
                        await api('/api/perunchat/trusted', { method: 'POST', body: { agent_id: id } });
                        if (!trustedAgents.includes(id)) trustedAgents.push(id);
                    },
                    onChange: (list) => { wiz.participants = list; },
                }));
            }
        }

        pane.querySelectorAll('[data-method]').forEach((c) => c.addEventListener('click', () => {
            wiz.method = c.dataset.method; wiz.step = 1; render();
        }));
        pane.querySelectorAll('[data-browse]').forEach((b) => b.addEventListener('click', () => {
            browseFolder(wiz.method === 'clone' ? '' : (wiz.path || ''), (picked) => {
                wiz.path = picked;
                if (!wiz.name) wiz.name = nameFromPath(picked);
                render();
            }, render);
        }));
        const mk = pane.querySelector('[data-mkdir]');
        if (mk) mk.addEventListener('click', async () => {
            if (!wiz.path) { toast('Enter a folder path first', 'err'); return; }
            try { const r = await api('/api/fs/mkdir', { method: 'POST', body: { path: wiz.path } });
                toast(r.created ? 'Folder created' : 'Folder already exists', 'ok'); }
            catch (e) { toast(e.message, 'err'); }
        });
        bind(pane);
        // The participants block is hidden until the shared checkbox is on;
        // toggling it must not require a full re-render (which would drop the
        // user's focus and any half-typed participant id).
        const sharedCb = pane.querySelector('[data-field="shared"]');
        if (sharedCb) sharedCb.addEventListener('change', () => {
            const section = pane.querySelector('.shared-section');
            if (section) section.classList.toggle('hidden', !sharedCb.checked);
        });

        const actions = [];
        if (wiz.step > 0) actions.push({ label: '◀ Back', class: 'ghost', onClick: () => { wiz.step--; render(); } });
        actions.push({ label: 'Cancel', class: 'ghost', onClick: closeModal });
        if (wiz.step < STEPS.length - 1) {
            actions.push({ label: 'Next ▶', class: 'primary', onClick: () => {
                const err = validate();
                if (err) { toast(err, 'err'); return; }
                wiz.step++; render();
            } });
        } else {
            actions.push({ label: 'Create project', class: 'primary', onClick: () => submitWizard(wiz) });
        }
        modal('New project', body, actions);
    };
    render();
}

async function submitWizard(wiz) {
    const payload = {
        method: wiz.method, path: wiz.path, name: wiz.name || nameFromPath(wiz.path),
        kind: wiz.kind, description: wiz.description, tags: wiz.tags,
        model: wiz.model, agent: wiz.agent,
        bootstrap: wiz.bootstrap, git_init: wiz.git_init,
        git_remote: wiz.git_remote, git_branch: wiz.git_branch,
        index_docs: wiz.index_docs, clone_url: wiz.clone_url,
    };
    if (wiz.shared && wiz.participants.length) {
        payload.shared = true;
        payload.participants = wiz.participants;
        payload.external_deps = wiz.external_deps;
    }
    if (!payload.path) { toast('Folder path is required', 'err'); return; }
    try {
        const res = await api('/api/projects', { method: 'POST', body: payload });
        closeModal();
        toast(res.created ? 'Project created' : 'Project registered', 'ok');
        if (wiz.vscode_link) {
            try {
                await api(`/api/projects/${encodeURIComponent(res.project.slug)}/vscode/link`, {
                    method: 'POST',
                    body: { launch_mode: 'vscode-terminal', auto_follow_edits: true },
                });
            } catch (e) { toast('Project created — VS Code link failed: ' + e.message, 'err'); }
        }
        await loadProjects();
        goto('#/project/' + res.project.slug);
    } catch (e) { toast(e.message, 'err'); }
}

async function browseFolder(startPath, onPick, onCancel) {
    const hidden = browseFolder._hidden || false;
    let data;
    try {
        data = await api(`/api/fs/list?path=${encodeURIComponent(startPath || '')}&show_hidden=${hidden}`);
    } catch (e) { toast(e.message, 'err'); if (onCancel) onCancel(); return; }
    const body = el('div', { class: 'fs-browser' });
    body.appendChild(el('div', { class: 'fs-path mono' }, esc(data.path)));

    const rootsRow = el('div', { class: 'fs-roots' });
    for (const r of (data.roots || [])) rootsRow.appendChild(el('button', { class: 'btn sm ghost', type: 'button', onclick: () => browseFolder(r, onPick, onCancel) }, esc(r)));
    if (data.home) rootsRow.appendChild(el('button', { class: 'btn sm ghost', type: 'button', onclick: () => browseFolder(data.home, onPick, onCancel) }, '⌂ Home'));
    body.appendChild(rootsRow);

    const list = el('div', { class: 'fs-list' });
    if (data.parent) list.appendChild(el('div', { class: 'fs-item dir', onclick: () => browseFolder(data.parent, onPick, onCancel) }, '↰ ..'));
    for (const d of (data.dirs || [])) list.appendChild(el('div', { class: 'fs-item dir', onclick: () => browseFolder(`${String(data.path).replace(/[\\/]+$/, '')}/${d}`, onPick, onCancel) }, `▸ ${esc(d)}`));
    for (const f of (data.files || []).slice(0, 80)) list.appendChild(el('div', { class: 'fs-item file' }, `· ${esc(f)}`));
    if (!(data.dirs || []).length && !(data.files || []).length) list.appendChild(el('div', { class: 'empty' }, 'Empty folder'));
    body.appendChild(list);

    const toggle = el('label', { class: 'fs-toggle' });
    const cb = el('input', { type: 'checkbox' });
    if (hidden) cb.checked = true;
    cb.addEventListener('change', () => { browseFolder._hidden = cb.checked; browseFolder(data.path, onPick, onCancel); });
    toggle.appendChild(cb); toggle.appendChild(document.createTextNode(' show hidden folders'));
    body.appendChild(toggle);

    modal('Choose a folder', body, [
        { label: 'Cancel', class: 'ghost', onClick: () => { if (onCancel) onCancel(); else closeModal(); } },
        { label: 'Use this folder', class: 'primary', onClick: () => onPick(data.path) },
    ]);
}

/* ===================================================== PROJECT DETAIL === */
async function viewProject(slug, tab) {
    if (!slug) { goto('#/projects'); return; }
    // `#/project/<slug>/shared/<prop_…>` keeps the focus target in the URL, so a
    // poll or reload re-focuses the same row instead of dropping it.
    let focusId = '';
    if (tab && tab.includes('/')) {
        const [head, tail] = tab.split('/', 2);
        tab = head;
        if (/^prop_[0-9a-f]{12}$/.test(tail || '')) focusId = tail;
    }
    if (!focusId) {
        const escaped = slug.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const m = (location.hash || '').match(
            new RegExp(`^#/project/${escaped}/shared/(prop_[0-9a-f]{12})$`));
        if (m) focusId = m[1];
    }
    if (focusId) pendingProposalFocus = focusId;
    $('#topbar-title').textContent = slug;
    const detail = await api(`/api/projects/${slug}`);
    const p = detail.project;
    const v = $('#view');
    v.innerHTML = '';

    const fav = p.favorite ? '★' : '☆';
    v.appendChild(el('div', { class: 'view-head' },
        `<div>
            <div class="eyebrow">${esc(p.kind)} project</div>
            <h1>${esc(p.name)}</h1>
            <div class="sub mono">${esc(p.path)}</div>
         </div>
         <div class="row wrap">
            <button class="btn ghost sm" id="pj-fav">${fav} Favorite</button>
            <button class="btn ghost sm" id="pj-index">⟳ Index docs</button>
            <button class="btn primary sm" id="pj-ask">▶ Agent Console</button>
         </div>`));

    // Usage for this project (captured in the last 24h) — /api/usage already narrows by slug.
    const usageBox = el('div', { class: 'meta', id: 'pj-usage' }, 'usage…');
    v.appendChild(usageBox);
    let usageBusy = false;
    const renderProjectUsage = async () => {
        if (usageBusy) return;
        usageBusy = true;
        try {
            const r = await api(`/api/usage?hours=24&project=${encodeURIComponent(slug)}`);
            const t = r.totals || {};
            const tok = Number(t.tokens_in || 0) + Number(t.tokens_out || 0);
            const partial = Number(t.incomplete || 0);
            usageBox.innerHTML = `Usage · captured in the last 24h — <b>$${Number(t.cost_usd || 0).toFixed(4)}</b>`
                + ` · ${tok.toLocaleString()} tok · ${Number(t.sessions || 0)} session(s)`
                + (partial ? ` · <span class="badge amber">${partial} partial</span>` : '');
        } catch (e) { usageBox.textContent = 'usage unavailable'; }
        finally { usageBusy = false; }
    };
    renderProjectUsage();
    if (projectUsageTimer) clearInterval(projectUsageTimer);
    projectUsageTimer = setInterval(() => {
        if (!document.getElementById('pj-usage')) {
            clearInterval(projectUsageTimer);
            projectUsageTimer = null;
            return;
        }
        renderProjectUsage();
    }, USAGE_REFRESH_MS);

    const tabDefs = [
        ['overview', 'Overview'], ['shared', 'Shared'], ['checks', 'Checks'], ['tasks', 'Tasks'], ['console', 'Console'], ['sessions', 'Sessions'],
        ['memory', 'Memory'], ['activity', 'Activity'], ['git', 'Git'],
        ['manifest', 'Manifest'], ['instructions', 'Instructions'], ['config', 'Config'],
    ];
    const tabs = el('div', { class: 'tabs' });
    const pane = el('div');
    const knownTabs = new Set(tabDefs.map((d) => d[0]));
    let active = (tab && knownTabs.has(tab)) ? tab : 'overview';
    const setTab = (name) => {
        active = name;
        tabs.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
        pane.innerHTML = '';
        pane.appendChild(renderProjectTab(name, detail));
        if (name === 'console') mountConsole(pane, p);
        if (name === 'git') mountGit(pane, p);
        if (name === 'manifest') mountManifest(pane, p);
        if (name === 'tasks') mountTasks(pane, p);
        // Keep the proposal id in the URL while the Shared tab is rendering from
        // a chat jump; a poll/reload then re-focuses the same row. A deliberate
        // tab click has no pending focus and drops it.
        if (name !== 'shared') pendingProposalFocus = null;
        const focusId = name === 'shared' ? pendingProposalFocus : '';
        const expected = `#/project/${slug}/${name}${focusId ? `/${focusId}` : ''}`;
        if (location.hash !== expected) {
            history.replaceState(null, '', expected);
            currentRoute = expected;
        }
    };
    for (const [name, label] of tabDefs) tabs.appendChild(el('div', { class: 'tab', dataset: { tab: name }, onclick: () => setTab(name) }, label));
    v.appendChild(tabs);
    v.appendChild(pane);
    setTab(active);

    $('#pj-fav').addEventListener('click', async () => {
        await api(`/api/projects/${slug}`, { method: 'PATCH', body: { favorite: p.favorite ? 0 : 1 } });
        toast('Updated', 'ok'); viewProject(slug);
    });
    $('#pj-index').addEventListener('click', () => indexProject(p));
    $('#pj-ask').addEventListener('click', () => goto('#/console/' + slug));
}

/* Checks: the only commands Perun may run without asking the operator again.
   The store lives in Perun, not in the project, so a room participant or an
   approved proposal can never add one — this form is the operator's. */
async function mountChecks(wrap, p) {
    const card = el('div', { class: 'card' },
        `<div class="card-head"><h4>Checks</h4><span class="badge gold">operator-owned</span></div>
         <p class="secondary">A check is a command <b>you</b> allow Perun to run without asking again.
         Declarations live in Perun's own store, never in the repository, so an agent or a proposal
         cannot add one. A pinned check is refused when the pinned file changes.</p>
         <div id="ck-list"><div class="empty"><span class="spinner"></span> loading checks…</div></div>
         <div id="ck-output"></div>
         <hr>
         <h4>Declare a check</h4>
         <div class="form-row"><label>Name</label><input id="ck-name" placeholder="check-manifest"></div>
         <div class="form-row"><label>Command</label><input id="ck-command" placeholder="python tests/check_manifest.py"></div>
         <div class="form-row"><label>Working directory</label><input id="ck-cwd" placeholder="(project root)"></div>
         <div class="form-row"><label>Timeout (s)</label><input id="ck-timeout" type="number" min="1" max="600" value="120"></div>
         <div class="form-row"><label>Pin file</label><input id="ck-pin" placeholder="tests/check_manifest.py (optional)"></div>
         <div class="form-row"><label>Environment</label><textarea id="ck-env" rows="2" placeholder="ONE_KEY=VALUE per line (paths only, never secrets)"></textarea></div>
         <button class="btn primary" id="ck-add">Declare</button>`);
    wrap.appendChild(card);
    const list = card.querySelector('#ck-list');
    const output = card.querySelector('#ck-output');

    const load = async () => {
        list.innerHTML = '<div class="empty"><span class="spinner"></span> loading checks…</div>';
        try {
            const data = await api(`/api/projects/${p.slug}/checks`);
            const rows = data.checks || [];
            if (!rows.length) {
                list.innerHTML = '<div class="empty">No checks declared. Add one below.</div>';
                return;
            }
            list.innerHTML = '';
            for (const c of rows) {
                const badge = c.last_status === 'ok' ? 'green' : (c.last_status ? 'red' : 'muted');
                const row = el('div', { class: 'list-row' },
                    `<span class="badge ${badge}">${esc(c.last_status || 'never')}</span>
                     <span class="grow"><div class="mono">${esc(c.name)}</div>
                     <div class="muted mono truncate">${esc(c.command)}</div>
                     ${c.pin_path ? `<div class="muted" style="font-size:.72rem">pinned ${esc(String(c.pin_sha256 || '').slice(0, 12))} · ${esc(c.pin_path)}</div>` : ''}</span>
                     <span class="ts">${esc(String(c.timeout_seconds || 120))}s</span>`);
                const run = el('button', { class: 'btn sm primary' }, 'Run');
                if (c.last_status === 'running') {
                    // The server refuses a second run; show that instead of
                    // offering a button that can only fail.
                    run.disabled = true;
                    run.textContent = 'Running…';
                }
                run.addEventListener('click', async () => {
                    run.disabled = true;
                    run.textContent = 'Running…';
                    output.innerHTML = '';
                    try {
                        const res = await api(`/api/projects/${p.slug}/checks/${encodeURIComponent(c.name)}/run`, { method: 'POST' });
                        const r = res.result || {};
                        const ok = r.status === 'ok';
                        output.innerHTML = `<div class="card" style="margin-top:10px">
                            <div class="card-head"><h4>${esc(c.name)}</h4>
                            <span class="badge ${ok ? 'green' : 'red'}">${esc(r.status || '?')}</span></div>
                            <div class="muted mono" style="font-size:.78rem">exit ${esc(String(r.exit_code))} · ${(Number(r.duration_ms || 0) / 1000).toFixed(1)}s${r.truncated ? ' · output truncated' : ''}</div>
                            <pre class="mono" style="background:var(--bg-inset);padding:12px;border-radius:8px;margin-top:8px;white-space:pre-wrap">${esc(r.output || '(no output)')}</pre></div>`;
                        toast(ok ? 'Check passed' : 'Check failed', ok ? 'ok' : 'err');
                    } catch (e) {
                        output.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
                    } finally {
                        run.disabled = false;
                        run.textContent = 'Run';
                        load();
                    }
                });
                const del = el('button', { class: 'btn sm ghost danger' }, 'Remove');
                del.addEventListener('click', async () => {
                    if (!confirm(`Remove the check "${c.name}"?`)) return;
                    try {
                        await api(`/api/projects/${p.slug}/checks/${encodeURIComponent(c.name)}`, { method: 'DELETE' });
                        toast('Check removed', 'ok');
                        load();
                    } catch (e) { toast(e.message, 'err'); }
                });
                row.appendChild(run);
                row.appendChild(del);
                list.appendChild(row);
            }
        } catch (e) {
            list.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
        }
    };

    card.querySelector('#ck-add').addEventListener('click', async () => {
        const name = card.querySelector('#ck-name').value.trim();
        const command = card.querySelector('#ck-command').value.trim();
        const cwd = card.querySelector('#ck-cwd').value.trim();
        const pin = card.querySelector('#ck-pin').value.trim();
        const timeout = Number(card.querySelector('#ck-timeout').value || 120);
        if (!name || !command) { toast('Name and command are required', 'err'); return; }
        const env = {};
        for (const line of card.querySelector('#ck-env').value.split('\n')) {
            const text = line.trim();
            if (!text) continue;
            const split = text.indexOf('=');
            if (split < 1) { toast(`Invalid env line: ${text}`, 'err'); return; }
            env[text.slice(0, split).trim()] = text.slice(split + 1);
        }
        try {
            await api(`/api/projects/${p.slug}/checks`, {
                method: 'POST',
                body: { name, command, cwd, pin, env, timeout_seconds: timeout },
            });
            card.querySelector('#ck-name').value = '';
            card.querySelector('#ck-command').value = '';
            card.querySelector('#ck-cwd').value = '';
            card.querySelector('#ck-pin').value = '';
            card.querySelector('#ck-env').value = '';
            toast('Check declared', 'ok');
            load();
        } catch (e) { toast(e.message, 'err'); }
    });

    load();
}

function renderProjectTab(name, detail) {
    const p = detail.project;
    const wrap = el('div');
    if (name === 'overview') {
        const mem = detail.memory_stats || {};
        const kv = el('div', { class: 'card' },
            `<div class="kv">
                <span class="k">Slug</span><span class="v mono">${esc(p.slug)}</span>
                <span class="k">Kind</span><span class="v">${esc(p.kind)}</span>
                <span class="k">Description</span><span class="v">${esc(p.description || '—')}</span>
                <span class="k">Model</span><span class="v mono">${esc(p.model || '(default)')}</span>
                <span class="k">Agent</span><span class="v mono">${esc(p.agent || '(default)')}</span>
                <span class="k">Tags</span><span class="v">${esc(p.tags || '—')}</span>
                <span class="k">Sessions</span><span class="v">${p.session_count || 0}</span>
                <span class="k">Facts</span><span class="v">${mem.facts_project ?? 0}</span>
                <span class="k">Doc chunks</span><span class="v">${mem.chunks ?? 0}</span>
                <span class="k">Last seen</span><span class="v">${esc(timeAgo(p.last_seen_at))}</span>
             </div>`);
        wrap.appendChild(kv);
        // Always render the manifest card. A project can lack a synced
        // manifest, or have a minimal one with no docs/anchors — either way the
        // Overview must say so and link to the Manifest tab instead of
        // silently showing nothing (the ai_playground wizard case).
        wrap.appendChild(manifestOverview(p));
        wrap.appendChild(sectionTitle('Recent runs'));
        const box = el('div', { class: 'card' });
        const list = el('div', { class: 'list' });
        for (const r of (detail.runs || []).slice(0, 8)) {
            list.appendChild(el('div', { class: 'list-row' },
                `<span class="badge ${STATUS_BADGE[r.status] || 'muted'}">${esc(r.status)}</span>
                 ${r.coroutine_id ? '<span class="badge gold" title="Started by a coroutine">coroutine</span>' : ''}
                 <span class="grow msg truncate">${esc(r.prompt)}</span>
                 <span class="ts">${esc(timeAgo(r.started_at))}</span>`));
        }
        if (!(detail.runs || []).length) list.appendChild(el('div', { class: 'empty' }, 'No runs yet.'));
        box.appendChild(list); wrap.appendChild(box);
    } else if (name === 'sessions') {
        const box = el('div', { class: 'card' });
        const list = el('div', { class: 'list' });
        for (const s of detail.sessions || []) {
            list.appendChild(el('div', { class: 'list-row' },
                `<span class="badge ${s.status === 'error' ? 'red' : 'green'}">${esc(s.status)}</span>
                 <span class="grow"><div class="truncate">${esc(s.title || '(untitled)')}</div>
                 <div class="muted mono" style="font-size:.72rem">${esc(s.opencode_session_id || '—')}</div></span>
                 <span class="ts">${esc(timeAgo(s.updated_at))}</span>`));
        }
        if (!(detail.sessions || []).length) list.appendChild(el('div', { class: 'empty' }, 'No sessions yet.'));
        box.appendChild(list); wrap.appendChild(box);
    } else if (name === 'memory') {
        wrap.appendChild(memoryList(detail.memory || []));
        wrap.appendChild(sectionTitle('Add fact'));
        wrap.appendChild(memoryForm(p.slug));
    } else if (name === 'activity') {
        wrap.appendChild(activityCard(detail.activity || []));
    } else if (name === 'git') {
        // mounted separately
    } else if (name === 'manifest') {
        // mounted separately
    } else if (name === 'instructions') {
        wrap.appendChild(projectInstructionsForm(p));
    } else if (name === 'tasks') {
        // mounted separately
    } else if (name === 'console') {
        // mounted separately
    } else if (name === 'config') {
        wrap.appendChild(projectConfigForm(p));
    } else if (name === 'checks') {
        mountChecks(wrap, p);
    } else if (name === 'shared') {
        const sharedPlaceholder = el('div', { class: 'card' }, '<div class="empty"><span class="spinner"></span> loading shared settings…</div>');
        const proposalsPlaceholder = el('div', { class: 'card' }, '<div class="empty"><span class="spinner"></span> loading proposals…</div>');
        wrap.appendChild(sharedPlaceholder);
        wrap.appendChild(proposalsPlaceholder);
        // Wait for both cards before focusing: the settings card above the
        // proposal list changes height after it loads, which used to move the
        // row off-screen right after the scroll.
        const sharedReady = sharedProjectCard(detail).catch((e) => {
            sharedPlaceholder.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
            return null;
        });
        const proposalsReady = proposalsCard(detail).catch((e) => {
            proposalsPlaceholder.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
            return null;
        });
        Promise.all([sharedReady, proposalsReady]).then(([sharedCard, proposalsCardEl]) => {
            if (sharedCard) sharedPlaceholder.replaceWith(sharedCard);
            if (proposalsCardEl) proposalsPlaceholder.replaceWith(proposalsCardEl);
            if (pendingProposalFocus) {
                const id = pendingProposalFocus;
                pendingProposalFocus = null;
                requestAnimationFrame(() => focusProposalInShared(id));
            }
        });
    }
    return wrap;
}

function projectInstructionsForm(p) {
    const wrap = el('div', { class: 'card' },
        `<div class="card-head"><h4>Project instruction files</h4><span class="badge gold">project scoped</span></div>
         <p class="secondary">These apply only to <b>${esc(p.name)}</b>. Perun prepends enabled files to every <b>new</b> agent session, in sorted filename order. Continuations inherit them from the session history.</p>
         <p class="muted" style="font-size:.8rem">Click a filename to open it in your default editor.</p>
         <div id="pi-list"></div>
         <hr>
         <h4 id="pi-edit-title">New instruction file</h4>
         <div class="form-row">
            <label class="field"><span class="lab">Filename</span><input type="text" id="pi-filename" placeholder="guidance.md"></label>
            <label class="field"><span class="lab">Name</span><input type="text" id="pi-name" placeholder="Display name"></label>
         </div>
         <label class="field"><span class="lab">Body</span>
            <textarea id="pi-body" rows="8" placeholder="Markdown instructions..."></textarea></label>
         <div class="form-actions">
            <button class="btn primary" id="pi-save">Save file</button>
            <button class="btn ghost" id="pi-new">New file</button>
         </div>`);

    async function refreshList() {
        const host = wrap.querySelector('#pi-list');
        host.innerHTML = '';
        try {
            const res = await api(`/api/projects/${p.slug}/instruction-files`);
            const files = res.files || [];
            if (!files.length) {
                host.innerHTML = '<p class="secondary">No instruction files yet. Use the editor below to create one.</p>';
                return;
            }
            const ul = el('ul', { class: 'instr-list' });
            for (const f of files) {
                const fileUrl = instructionFileUrl(f.filename, false, p.path);
                const li = el('li', { class: 'row', style: 'gap:12px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border)' },
                    `<span class="row" style="gap:6px;flex:1;align-items:center"><input type="checkbox" ${f.enabled ? 'checked' : ''}> <a class="link instr-link" href="${esc(safeUrl(fileUrl))}" title="Click to open in editor">${esc(f.name)}</a> <span class="secondary">${esc(f.filename)}</span></span>
                     <button class="btn ghost" data-edit="${esc(f.filename)}">Edit</button>
                     <button class="btn ghost danger" data-del="${esc(f.filename)}">Delete</button>`);
                const cb = li.querySelector('input');
                cb.addEventListener('change', async () => {
                    await api(`/api/projects/${p.slug}/instruction-files/${encodeURIComponent(f.filename)}/toggle`, {
                        method: 'PATCH', body: { enabled: cb.checked }
                    });
                    toast(`${f.name} ${cb.checked ? 'enabled' : 'disabled'}`, 'ok');
                });
                li.querySelector('[data-edit]').addEventListener('click', async () => {
                    const d = await api(`/api/projects/${p.slug}/instruction-files/${encodeURIComponent(f.filename)}`);
                    wrap.querySelector('#pi-filename').value = d.filename;
                    wrap.querySelector('#pi-name').value = d.name;
                    wrap.querySelector('#pi-body').value = d.body;
                    wrap.querySelector('#pi-edit-title').textContent = `Edit ${esc(d.filename)}`;
                });
                li.querySelector('[data-del]').addEventListener('click', async () => {
                    if (!confirm(`Delete ${f.name}?`)) return;
                    await api(`/api/projects/${p.slug}/instruction-files/${encodeURIComponent(f.filename)}`, { method: 'DELETE' });
                    refreshList();
                    toast(`${f.name} deleted`, 'ok');
                });
                ul.appendChild(li);
            }
            host.appendChild(ul);
        } catch (e) { toast(e.message, 'err'); }
    }
    refreshList();

    const resetEditor = () => {
        wrap.querySelector('#pi-filename').value = '';
        wrap.querySelector('#pi-name').value = '';
        wrap.querySelector('#pi-body').value = '';
        wrap.querySelector('#pi-edit-title').textContent = 'New instruction file';
    };
    wrap.querySelector('#pi-new').addEventListener('click', resetEditor);
    wrap.querySelector('#pi-save').addEventListener('click', async () => {
        const filename = (wrap.querySelector('#pi-filename').value || '').trim() || 'guidance.md';
        try {
            await api(`/api/projects/${p.slug}/instruction-files/${encodeURIComponent(filename)}`, {
                method: 'POST',
                body: {
                    name: wrap.querySelector('#pi-name').value.trim(),
                    body: wrap.querySelector('#pi-body').value,
                    enabled: true,
                }
            });
            toast('Instruction file saved', 'ok');
            refreshList();
            resetEditor();
        } catch (e) { toast(e.message, 'err'); }
    });

    return wrap;
}

async function sharedProjectCard(detail) {
    const p = detail.project;
    const card = el('div', { class: 'card shared-card' });
    let cfg = {};
    let chat = null;
    let projectChat = null;
    let configStatus = null;
    try {
        const res = await api(`/api/projects/${p.slug}/shared`);
        cfg = res.shared || {};
        chat = res.chat || null;
        projectChat = res.project_chat || null;
        configStatus = res.shared_config || null;
    } catch (e) {
        card.appendChild(el('div', { class: 'card-head' }, '<h4>Shared project</h4>'));
        card.appendChild(el('div', { class: 'empty' }, `Could not load shared settings: ${e.message}`));
        return card;
    }
    if (!projectChat && chat) {
        // Older server payload: synthesise the richer shape from the binding.
        projectChat = {
            room_id: chat.room_id, state: chat.state, managed_by: chat.managed_by,
            manifest_rev: chat.manifest_rev, last_sync_at: chat.last_sync_at,
            expected_members: (cfg.participants || []).map((x) => x.agent_id || x),
            members: null, error: null, label: `${p.name || p.slug} project chat`,
        };
    }
    const statusOf = (info) => {
        const state = (info && info.state) || 'disabled';
        if (state === 'active') return { cls: 'on', text: 'Chat active' };
        if (state === 'pending') return { cls: 'pending', text: 'Chat pending sync' };
        if (state === 'unbound') return { cls: 'pending', text: 'Chat not created' };
        return { cls: '', text: 'Chat disabled' };
    };
    const enabled = !!cfg.shared;
    const paused = !!p.work_paused;
    const initialStatus = statusOf(projectChat);

    card.innerHTML = `
        <div class="card-head"><h4>Shared project</h4>
            <span class="shared-status ${initialStatus.cls}" id="sp-status">${esc(initialStatus.text)}</span>
            ${enabled ? (paused
                ? '<span class="badge amber" id="sp-work-state">Work paused</span>'
                : '<span class="badge green" id="sp-work-state">Work running</span>') : ''}
            ${enabled ? `<button class="btn sm ${paused ? 'primary' : 'ghost'}" id="sp-work-toggle" type="button">${paused ? '▶ Resume work' : '❚❚ Pause work'}</button>` : ''}
        </div>
        ${enabled && paused ? `<div class="cor-warn" id="sp-paused-note">Autonomous work is paused${p.work_paused_by ? ` by ${esc(p.work_paused_by)}` : ''}. New chat messages are kept, but no agent will act until you resume. In-flight work finishes.</div>` : ''}
        <label class="opt"><input type="checkbox" id="sp-enabled" ${enabled ? 'checked' : ''}> <b>Enable shared project (PerunChat)</b></label>
        <p class="muted" style="font-size:.72rem;margin-top:4px">Hiding a project only removes it from lists; it does not disable sharing. Turn this off to tear the room down.</p>
        <div id="sp-config"></div>
        <div id="sp-chat" class="shared-section sp-chat-panel"></div>
        <div id="sp-form" class="mt ${enabled ? '' : 'hidden'}">
            <div class="shared-section">
                <h4>Participants</h4>
                <div id="sp-participants"></div>
            </div>
            <div class="shared-section">
                <h4>External dependencies</h4>
                <div id="sp-deps"></div>
                <button class="btn sm ghost mt" id="sp-add-dep" type="button">＋ Add dependency</button>
            </div>
            <div class="shared-section">
                <label class="opt"><input type="checkbox" id="sp-auto-approve" ${cfg.auto_approve ? 'checked' : ''}> <b>Agent approval in chat</b></label>
                <p class="muted" style="font-size:.72rem;margin-top:4px">When enabled, action proposals created by chat agents must be explicitly approved or rejected by another <code>rw</code> participant agent in the project chat. The proposal is not executed until a peer agent posts an approval.</p>
                <label class="opt"><input type="checkbox" id="sp-policy-auto-approve" ${cfg.policy_auto_approve ? 'checked' : ''}> <b>Policy auto-approve</b></label>
                <p class="muted" style="font-size:.72rem;margin-top:4px">When enabled, the system approves and executes chat-agent proposals immediately without waiting for a peer or human review. This overrides agent approval.</p>
                <p class="warn-text" style="font-size:.72rem;margin-top:4px">Warning: enabling policy auto-approve lets chat agents run code in this project without any review. Only enable for projects whose participants you trust.</p>
            </div>
            <button class="btn primary" id="sp-save" type="button">Save shared settings</button>
            <p class="muted" style="font-size:.72rem;margin-top:10px">A post-run filesystem audit logs any changes outside the allowlist. The optional <code>perun-guard</code> MCP tools enforce the allowlist only if the Project Agent chooses them.</p>
        </div>`;

    const statusBadge = card.querySelector('#sp-status');
    const configHost = card.querySelector('#sp-config');

    const syncShared = async (btn, note) => {
        if (btn) btn.disabled = true;
        if (note) note.textContent = 'Syncing…';
        try {
            const res = await api(`/api/projects/${p.slug}/shared/sync`, { method: 'POST' });
            renderChat(res.project_chat);
            renderConfigWarning(res.shared_config);
            const ok = !!(res.result && res.result.ok);
            toast(ok ? 'Project chat synchronized'
                : `Sync failed: ${(res.result && res.result.error) || 'unknown error'}`,
                ok ? 'ok' : 'err');
        } catch (e) {
            if (note) note.textContent = e.message;
            toast(e.message, 'err');
        } finally {
            if (btn) btn.disabled = false;
        }
    };

    const renderConfigWarning = (status) => {
        if (!configHost) return;
        if (!status || !status.stale) {
            configHost.innerHTML = '';
            return;
        }
        configHost.innerHTML = `
            <p class="warn-text" style="font-size:.78rem">${esc(status.warning || 'Shared MCP config is stale.')}</p>
            <button class="btn sm ghost" id="sp-config-sync" type="button">↻ Sync config</button>`;
        const btn = configHost.querySelector('#sp-config-sync');
        if (btn) btn.addEventListener('click', () => syncShared(btn, null));
    };

    const renderChat = (info) => {
        projectChat = info || null;
        const st = statusOf(info);
        if (statusBadge) {
            statusBadge.className = 'shared-status ' + st.cls;
            statusBadge.textContent = st.text;
        }
        const host = card.querySelector('#sp-chat');
        if (!host) return;
        if (!info) {
            host.innerHTML = '<h4>Project chat</h4><p class="muted">Project chat state is unavailable.</p>';
            return;
        }
        const expected = new Set([
            ...((info.expected_members || []).map((m) => String(m).toLowerCase())),
            String(info.managed_by || '').toLowerCase(),
        ].filter(Boolean));
        const members = info.members;
        const memberHtml = (members === null || members === undefined)
            ? '<span class="muted">Members unavailable (gateway error)</span>'
            : (members.length
                ? members.map((m) => {
                    const wanted = expected.has(String(m).toLowerCase());
                    return `<span class="chat-member ${wanted ? 'ok' : 'extra'}" title="${wanted
                        ? 'declared participant'
                        : 'not on the project participant list — removed by the next sync'}">${esc(m)}${wanted ? '' : ' ⚠'}</span>`;
                }).join('')
                : '<span class="muted">No members yet</span>');
        const extras = (members || []).filter((m) => !expected.has(String(m).toLowerCase()));
        host.innerHTML = `
            <div class="card-head"><h4>Project chat</h4>
                <span class="shared-status ${st.cls}">${esc(st.text)}</span></div>
            <p class="muted" style="font-size:.78rem">The dedicated PerunChat room for this project. Membership is reconciled from the participant list on every sync; anyone else is removed.</p>
            <div class="kv">
                <span class="k">Chat name</span><span class="v">${esc(info.label || (p.name || p.slug) + ' project chat')}</span>
                <span class="k">Room id</span><span class="v mono">${esc(info.room_id || '—')}</span>
                <span class="k">State</span><span class="v">${esc(info.state || '—')}</span>
                <span class="k">Managed by</span><span class="v mono">${esc(info.managed_by || '—')}</span>
                <span class="k">Manifest rev</span><span class="v">${esc(info.manifest_rev ?? 1)}</span>
                <span class="k">Last sync</span><span class="v">${esc(info.last_sync_at ? timeAgo(info.last_sync_at) : 'never')}</span>
            </div>
            <div class="chat-members-wrap"><span class="lab">Members</span>
                <div class="chat-members">${memberHtml}</div></div>
            ${extras.length ? `<p class="warn-text" style="font-size:.76rem">${extras.length} unexpected member(s) will be removed by the next sync. Use “Sync now” to reconcile immediately.</p>` : ''}
            ${info.error ? `<p class="warn-text" style="font-size:.76rem">Gateway: ${esc(info.error)}</p>` : ''}
            <div class="row" style="gap:10px;align-items:center;margin-top:10px">
                <button class="btn sm ghost" id="sp-sync" type="button">↻ Sync now</button>
                <span class="muted" id="sp-sync-note" style="font-size:.76rem"></span>
            </div>`;
        const syncBtn = host.querySelector('#sp-sync');
        if (syncBtn) syncBtn.addEventListener('click', () =>
            syncShared(syncBtn, host.querySelector('#sp-sync-note')));
    };
    renderChat(projectChat);
    renderConfigWarning(configStatus);

    const trusted = await loadTrustedAgents();
    const editor = participantEditor({
        initial: cfg.participants || [],
        trusted,
        onTrust: async (id) => {
            await api('/api/perunchat/trusted', { method: 'POST', body: { agent_id: id } });
        },
    });
    card.querySelector('#sp-participants').appendChild(editor);

    const depsContainer = card.querySelector('#sp-deps');
    const deps = (cfg.external_deps || []).map((d) => ({ path: d.path || '', access: d.access || 'rw', name: d.name || '' }));
    const renderDeps = () => {
        depsContainer.innerHTML = deps.map((d, i) => `
            <div class="dep-row" data-idx="${i}">
                <input type="text" class="sp-dep-path" value="${esc(d.path)}" placeholder="C:\\shared\\lib or relative\\path">
                <select class="sp-dep-access">
                    <option value="rw" ${d.access === 'rw' ? 'selected' : ''}>rw</option>
                    <option value="ro" ${d.access === 'ro' ? 'selected' : ''}>ro</option>
                </select>
                <button class="btn ghost sm" type="button" data-rm-dep>✕</button>
            </div>`).join('');
        depsContainer.querySelectorAll('[data-rm-dep]').forEach((b) => b.addEventListener('click', () => {
            deps.splice(parseInt(b.closest('.dep-row').dataset.idx), 1);
            renderDeps();
        }));
    };
    renderDeps();
    card.querySelector('#sp-add-dep').addEventListener('click', () => { deps.push({ path: '', access: 'rw', name: '' }); renderDeps(); });

    const enabledCb = card.querySelector('#sp-enabled');
    enabledCb.addEventListener('change', () => {
        card.querySelector('#sp-form').classList.toggle('hidden', !enabledCb.checked);
    });
    const workToggle = card.querySelector('#sp-work-toggle');
    if (workToggle) {
        workToggle.addEventListener('click', async () => {
            workToggle.disabled = true;
            try {
                const res = await api(`/api/projects/${p.slug}/work`, {
                    method: 'POST',
                    body: { paused: !p.work_paused },
                });
                toast(res.project && res.project.work_paused
                    ? 'Autonomous work paused' : 'Autonomous work resumed', 'ok');
                await loadProjects();
                viewProject(p.slug, 'shared');
            } catch (e) {
                toast(e.message, 'err');
                workToggle.disabled = false;
            }
        });
    }
    const autoApproveCb = card.querySelector('#sp-auto-approve');
    const policyAutoApproveCb = card.querySelector('#sp-policy-auto-approve');
    card.querySelector('#sp-save').addEventListener('click', async () => {
        const selected = editor.getValue();
        const depRows = depsContainer.querySelectorAll('.dep-row');
        const newDeps = [];
        depRows.forEach((row) => {
            const path = row.querySelector('.sp-dep-path').value.trim();
            if (!path) return;
            newDeps.push({ path, access: row.querySelector('.sp-dep-access').value, name: '' });
        });
        const saveBtn = card.querySelector('#sp-save');
        saveBtn.disabled = true;
        try {
            const res = await api(`/api/projects/${p.slug}/shared`, {
                method: 'PUT',
                body: {
                    shared: enabledCb.checked,
                    auto_approve: autoApproveCb.checked,
                    policy_auto_approve: policyAutoApproveCb.checked,
                    participants: selected,
                    external_deps: newDeps,
                },
            });
            if (res.project_chat) renderChat(res.project_chat);
            renderConfigWarning(res.shared_config);
            const sync = res.chat_sync || {};
            if (sync.ok === false) {
                toast(`Saved, but the chat sync failed: ${sync.error || 'unknown error'}`, 'err');
            } else {
                toast('Shared settings saved', 'ok');
            }
        } catch (e) {
            toast(e.message, 'err');
        } finally {
            saveBtn.disabled = false;
        }
    });
    return card;
}

let sharedPollTimer = null;
function scheduleSharedPoll(slug) {
    if (sharedPollTimer) clearTimeout(sharedPollTimer);
    sharedPollTimer = setTimeout(async () => {
        sharedPollTimer = null;
        if (!location.hash.startsWith(`#/project/${slug}/shared`)) return;
        try {
            const res = await api(`/api/projects/${slug}/proposals`);
            if ((res.proposals || []).some((prop) => prop.status === 'executing' || prop.status === 'claimed')) {
                viewProject(slug, 'shared');
            }
        } catch (e) {
            // Transient failure: the user can refresh; polling stops rather
            // than hammering a broken server.
        }
    }, 4000);
}

async function proposalsCard(detail) {
    const p = detail.project;
    const card = el('div', { class: 'card proposal-list' }, '<div class="card-head"><h4>Action proposals</h4></div>');
    let proposals = [];
    try {
        const res = await api(`/api/projects/${p.slug}/proposals`);
        proposals = res.proposals || [];
    } catch (e) {
        card.innerHTML += `<div class="empty">${esc(e.message)}</div>`;
        return card;
    }
    if (!proposals.length) {
        card.innerHTML += '<div class="empty">No action proposals yet.</div>';
        return card;
    }

    const activeStatuses = new Set(['pending', 'approved', 'executing', 'claimed']);
    const completedStatuses = new Set(['executed', 'failed', 'rejected', 'cancelled', 'expired']);
    let active = proposals.filter((prop) => activeStatuses.has(prop.status));
    let completed = proposals.filter((prop) => completedStatuses.has(prop.status));

    // Active: pending first, then approved, then claimed, then executing.
    const statusOrder = { pending: 0, approved: 1, claimed: 2, executing: 3 };
    active.sort((a, b) => statusOrder[a.status] - statusOrder[b.status]);
    // Completed: newest first.
    completed.sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));

    const renderRow = (prop) => {
        const operations = Array.isArray(prop.operations) ? prop.operations : [];
        const targetList = operations.length
            ? operations.map((op) => String(op.path || '').trim())
            : (prop.targets || []);
        const targetChips = operations.length
            ? operations.map((op) => {
                const target = String(op.path || '').trim();
                const kind = String(op.op || 'op');
                return `<button class="chip chip-link proposal-target" data-target="${esc(target)}" type="button" title="${esc(kind)} ${esc(target)}"><span class="muted">${esc(kind)}</span> ${esc(target)}</button>`;
              }).join('')
            : (targetList.length
                ? targetList.map((t) => {
                    const target = String(t).trim();
                    return `<button class="chip chip-link proposal-target" data-target="${esc(target)}" type="button" title="Open ${esc(target)}">${esc(target)}</button>`;
                  }).join('')
                : '<span class="muted">(no targets)</span>');
        const actions = [];
        const packageState = String(prop.package_state || '');
        const packagePending = prop.status === 'claimed' && packageState === 'pending';
        if (prop.status === 'pending') {
            actions.push(`<button class="btn sm primary" data-approve-run="${esc(prop.id)}">Approve &amp; run</button>`);
            actions.push(`<button class="btn sm ghost" data-approve="${esc(prop.id)}">Approve only</button>`);
            actions.push(`<button class="btn sm ghost" data-reject="${esc(prop.id)}">Reject</button>`);
            actions.push(`<button class="btn sm ghost danger" data-delete="${esc(prop.id)}">Delete</button>`);
        } else if (prop.status === 'approved') {
            actions.push(`<button class="btn sm primary" data-execute="${esc(prop.id)}">Run now</button>`);
            actions.push(`<button class="btn sm ghost" data-withdraw="${esc(prop.id)}" title="Withdraw this approval; the record stays in the audit trail">Withdraw</button>`);
            actions.push(`<button class="btn sm ghost danger" data-delete="${esc(prop.id)}">Delete</button>`);
        } else if (prop.status === 'claimed') {
            if (packagePending) {
                actions.push(`<button class="btn sm primary" data-package-approve="${esc(prop.id)}">Approve package</button>`);
                actions.push(`<button class="btn sm ghost" data-package-reject="${esc(prop.id)}">Reject package</button>`);
            }
            actions.push(`<button class="btn sm ghost" data-revoke-claim="${esc(prop.id)}">Revoke claim</button>`);
        } else if (prop.status === 'executing') {
            actions.push(`<button class="btn sm ghost" data-cancel="${esc(prop.id)}">Cancel</button>`);
        } else if (completedStatuses.has(prop.status)) {
            actions.push(`<button class="btn sm primary" data-retry="${esc(prop.id)}">Retry</button>`);
            actions.push(`<button class="btn sm ghost danger" data-delete="${esc(prop.id)}">Delete</button>`);
        }
        // Show the id in the row: a chat reference can be matched to it, and the
        // operator can copy it back to the room.
        const idChip = `<button class="chip chip-link proposal-id" data-copy="${esc(prop.id)}" type="button" title="Copy this proposal id">${esc(prop.id)}</button>`;
        const actionType = String(prop.action_type || (operations.length ? 'mixed' : 'unknown'));
        let meta = `${idChip} · <span class="chip">${esc(actionType)}</span> · ${esc(prop.requester)} · ${targetChips} · ${esc(timeAgo(prop.created_at))}`;
        if (['pending', 'approved', 'claimed', 'executing'].includes(prop.status)
            && prop.expires_at) {
            meta += ` · expires ${timeLeft(prop.expires_at)}`;
        }
        if (prop.status === 'executing') {
            meta += ' · headless run in progress…';
        }
        if (prop.status === 'claimed') {
            const executor = prop.executor_id || 'unknown';
            const expires = prop.claim_expires_at ? new Date(prop.claim_expires_at * 1000).toLocaleString() : '';
            meta += ` · claimed by ${esc(executor)}${expires ? ' · expires ' + esc(expires) : ''}`;
            if (packageState) {
                meta += ` · package ${esc(packageState)}`;
                if (prop.package_expires_at) {
                    meta += ` · review by ${esc(new Date(prop.package_expires_at * 1000).toLocaleString())}`;
                }
            }
        }
        if (prop.status === 'approved' || prop.status === 'rejected' || prop.status === 'cancelled') {
            const who = prop.approved_by || 'owner';
            if (prop.status === 'cancelled') {
                meta += ' · withdrawn' + (prop.result ? `: ${esc(prop.result)}` : '');
            } else {
                meta += ` · ${prop.status === 'rejected' ? 'rejected' : 'approved'} by ${esc(who)}`;
                if (prop.status === 'rejected' && prop.result) {
                    meta += `: ${esc(prop.result)}`;
                }
            }
        }
        // The approver must see exactly what they are approving: free-form
        // details from the chat are hidden nowhere, and the result of an
        // executed/failed run is visible without opening the run log.
        const details = prop.details && Object.keys(prop.details).length
            ? JSON.stringify(prop.details)
            : '';
        const outcome = (prop.status === 'executed' || prop.status === 'failed') && prop.result
            ? prop.result
            : '';
        const detailsHtml = details
            ? `<div class="meta truncate" title="${esc(details)}">details: ${esc(details)}</div>`
            : '';
        const outcomeHtml = outcome
            ? `<div class="meta truncate" title="${esc(outcome)}">result: ${esc(outcome)}</div>`
            : '';
        // Package review: the second gate of an external handoff. The operator
        // approves the exact archive digest and its server-derived operations,
        // never an agent-authored manifest.
        let packageHtml = '';
        if (packageState) {
            const operations = (prop.package_manifest && prop.package_manifest.operations) || [];
            const preview = Array.isArray(prop.package_preview) ? prop.package_preview : [];
            const digest = String(prop.package_digest || '');
            const digestShort = digest ? digest.slice(0, 16) + '…' : '';
            const opLines = operations.map((op) => {
                const pre = op.pre_sha256 ? String(op.pre_sha256).slice(0, 8) + '…' : '(absent)';
                const post = op.post_sha256 ? String(op.post_sha256).slice(0, 8) + '…' : '(deleted)';
                return `<li><span class="chip">${esc(op.op)}</span> ${esc(op.path)} <span class="muted">${esc(pre)} → ${esc(post)}</span></li>`;
            }).join('');
            const diffs = preview.map((entry) => {
                const summary = entry.summary || entry.op || '';
                const diff = entry.diff || '';
                const body = diff
                    ? `<pre class="pkg-diff">${esc(diff)}${entry.truncated ? '\n… (truncated)' : ''}</pre>`
                    : `<div class="meta">${esc(summary)}</div>`;
                return `<details class="pkg-file"><summary>${esc(entry.path || '')} · ${esc(summary)}</summary>${body}</details>`;
            }).join('');
            const err = prop.package_error
                ? `<div class="meta" style="color:var(--red)">${esc(prop.package_error)}</div>`
                : '';
            packageHtml = `<details class="pkg-review" ${packagePending ? 'open' : ''}>
                <summary>Uploaded package · ${esc(packageState)}${digestShort ? ' · ' + esc(digestShort) : ''}</summary>
                <div class="meta">sha256 ${esc(digest || '(unknown)')}</div>
                ${err}
                ${opLines ? `<ul class="pkg-ops">${opLines}</ul>` : ''}
                ${diffs}
            </details>`;
        }
        const row = el('div', { class: 'list-row' },
            `<span class="badge ${prop.status === 'executed' ? 'green' : prop.status === 'failed' ? 'red' : prop.status === 'approved' || prop.status === 'executing' ? 'blue' : prop.status === 'rejected' || prop.status === 'cancelled' ? 'muted' : 'amber'}">${esc(prop.status)}</span>
             <div class="intent"><div class="msg truncate">${esc(prop.intent)}</div>
             <div class="meta">${meta}</div>${detailsHtml}${outcomeHtml}${packageHtml}</div>
             <span class="row" style="gap:6px">${actions.join('')}</span>`);
        row.dataset.proposal = prop.id;
        row.querySelectorAll('.proposal-target').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                openManifestPath(p.path, btn.dataset.target);
            });
        });
        row.querySelector('.proposal-id')?.addEventListener('click', async (e) => {
            e.stopPropagation();
            try {
                await navigator.clipboard.writeText(prop.id);
                toast('Proposal id copied', 'ok');
            } catch (_) {
                toast(prop.id, 'ok');
            }
        });
        row.querySelector('[data-approve-run]')?.addEventListener('click', async () => {
            try {
                await api(`/api/projects/${p.slug}/proposals/${prop.id}/approve-and-execute`, { method: 'POST' });
                toast('Approved — headless run started', 'ok');
                viewProject(p.slug, 'shared');
            } catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-approve]')?.addEventListener('click', async () => {
            if (!confirm(`Approve this proposal without starting it?\n\n${prop.intent}\n\nTargets: ${targetList.join(', ') || '(none)'}`)) return;
            try { await api(`/api/projects/${p.slug}/proposals/${prop.id}/approve`, { method: 'POST' }); toast('Approved', 'ok'); viewProject(p.slug, 'shared'); }
            catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-reject]')?.addEventListener('click', async () => {
            try { await api(`/api/projects/${p.slug}/proposals/${prop.id}/reject`, { method: 'POST' }); toast('Rejected', 'ok'); viewProject(p.slug, 'shared'); }
            catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-withdraw]')?.addEventListener('click', async () => {
            if (!confirm(`Withdraw this approval?\n\n${prop.intent}\n\nThe proposal is cancelled for good; it stays in the audit trail.`)) return;
            try { await api(`/api/projects/${p.slug}/proposals/${prop.id}/cancel`, { method: 'POST', body: JSON.stringify({ reason: 'withdrawn from dashboard' }) }); toast('Withdrawn', 'ok'); viewProject(p.slug, 'shared'); }
            catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-execute]')?.addEventListener('click', async () => {
            try {
                await api(`/api/projects/${p.slug}/proposals/${prop.id}/execute`, { method: 'POST' });
                toast('Headless run started', 'ok');
                viewProject(p.slug, 'shared');
            } catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-package-approve]')?.addEventListener('click', async () => {
            const ops = (prop.package_manifest && prop.package_manifest.operations) || [];
            const detail = ops.map((op) => `  ${op.op} ${op.path}`).join('\n');
            if (!confirm(`Apply this external package?\n\n${prop.intent}\n\nPackage sha256: ${prop.package_digest || '(unknown)'}\n\n${detail || '(no operations)'}\n\nThe archive is applied atomically; a failed precondition rolls everything back.`)) return;
            try {
                await api(`/api/projects/${p.slug}/proposals/${prop.id}/package/approve`, { method: 'POST' });
                toast('Package approved and applied', 'ok');
                viewProject(p.slug, 'shared');
            } catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-package-reject]')?.addEventListener('click', async () => {
            const reason = prompt('Reject this package? Optional reason (the executor will see it):', '');
            if (reason === null) return;
            try {
                await api(`/api/projects/${p.slug}/proposals/${prop.id}/package/reject`, {
                    method: 'POST', body: JSON.stringify({ reason }),
                });
                toast('Package rejected', 'ok');
                viewProject(p.slug, 'shared');
            } catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-revoke-claim]')?.addEventListener('click', async () => {
            try {
                await api(`/api/projects/${p.slug}/proposals/${prop.id}/revoke-claim`, { method: 'POST' });
                toast('Claim revoked', 'ok');
                viewProject(p.slug, 'shared');
            } catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-cancel]')?.addEventListener('click', async () => {
            if (!confirm(`Cancel the running agent for this proposal?\n\n${prop.intent}`)) return;
            try {
                await api(`/api/runs/${prop.id}/cancel`, { method: 'POST' });
                toast('Cancelling…', 'ok');
                viewProject(p.slug, 'shared');
            } catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-retry]')?.addEventListener('click', async () => {
            try {
                await api(`/api/projects/${p.slug}/proposals/${prop.id}/retry`, { method: 'POST' });
                toast('Retry proposal created', 'ok');
                viewProject(p.slug, 'shared');
            } catch (e) { toast(e.message, 'err'); }
        });
        row.querySelector('[data-delete]')?.addEventListener('click', async () => {
            if (!confirm(`Delete this proposal permanently?\n\n${prop.intent}`)) return;
            try {
                await api(`/api/projects/${p.slug}/proposals/${prop.id}`, { method: 'DELETE' });
                toast('Proposal deleted', 'ok');
                viewProject(p.slug, 'shared');
            } catch (e) { toast(e.message, 'err'); }
        });
        return row;
    };

    const activeList = el('div', { class: 'list' });
    active.forEach((prop) => activeList.appendChild(renderRow(prop)));
    card.appendChild(activeList);

    if (completed.length) {
        const completedWrap = el('div', { class: 'completed-proposals mt' });
        const header = el('div', { class: 'card-head', style: 'cursor:pointer;user-select:none' },
            `<h4>Completed (${completed.length})</h4><span class="muted">▸</span>`);
        const completedList = el('div', { class: 'list hidden' });
        completed.forEach((prop) => completedList.appendChild(renderRow(prop)));
        header.addEventListener('click', () => {
            completedList.classList.toggle('hidden');
            header.querySelector('span').textContent = completedList.classList.contains('hidden') ? '▸' : '▾';
        });
        completedWrap.appendChild(header);
        completedWrap.appendChild(completedList);
        card.appendChild(completedWrap);
    }

    if (proposals.some((prop) => prop.status === 'executing' || prop.status === 'claimed')) scheduleSharedPoll(p.slug);
    return card;
}

function projectConfigForm(p) {
    const isControl = !!p.is_control;
    const delBtn = isControl ? '' : '<button class="btn danger" id="pc-delete">Delete project</button>';
    const note = isControl
        ? '<p class="muted mt">This is the Perun orchestrator (control plane). It cannot be removed.</p>'
        : '';
    const agentModels = (p.agent_models && typeof p.agent_models === 'object') ? p.agent_models : {};
    const card = el('div', { class: 'card' },
        `${isControl ? '<div class="card-head"><h4>Orchestrator</h4><span class="badge gold">control plane</span></div>' : ''}
         <label class="field"><span class="lab">Name</span><input type="text" id="pc-name" value="${esc(p.name)}"></label>
         <label class="field"><span class="lab">Description</span><input type="text" id="pc-desc" value="${esc(p.description || '')}"></label>
         <div class="form-row">
            <label class="field"><span class="lab">Plan model</span><span id="pc-plan-model-host"></span></label>
            <label class="field"><span class="lab">Build model</span><span id="pc-build-model-host"></span></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">Default model</span><span id="pc-model-host"></span></label>
            <label class="field"><span class="lab">Agent override</span><span id="pc-agent-host"></span></label>
         </div>
         ${note}
         <div class="form-actions">
            <button class="btn primary" id="pc-save">Save</button>
            ${delBtn}
         </div>`);
    const mPlan = modelSelect({ placeholder: '(use default)', initial: agentModels.plan || '' });
    card.querySelector('#pc-plan-model-host').appendChild(mPlan);
    const mBuild = modelSelect({ placeholder: '(use default)', initial: agentModels.build || '' });
    card.querySelector('#pc-build-model-host').appendChild(mBuild);
    const mModel = modelSelect({ placeholder: '(use default)', initial: p.model || '' });
    card.querySelector('#pc-model-host').appendChild(mModel);
    const mAgent = agentSelect({ placeholder: '(use default)', initial: p.agent || '' });
    card.querySelector('#pc-agent-host').appendChild(mAgent);
    if (!state.agents.length) loadAgents().then(() => mAgent.refresh?.()).catch(() => {});
    card.querySelector('#pc-save').addEventListener('click', async () => {
        // Merge with existing agent_models so custom agent defaults set via
        // manifest/MCP are preserved; empty strings clear plan/build defaults.
        const agent_models = { ...agentModels };
        agent_models.plan = mPlan.getValue();
        agent_models.build = mBuild.getValue();
        const res = await api(`/api/projects/${p.slug}`, { method: 'PATCH', body: {
            name: $('#pc-name').value, description: $('#pc-desc').value,
            model: mModel.getValue(), agent: mAgent.getValue(),
            agent_models: agent_models } });
        const cfg = res && res.opencode_config;
        if (cfg && cfg.warning) {
            toast('Saved — ' + cfg.warning, 'err');
        } else if (cfg && (cfg.action === 'created' || cfg.action === 'updated')) {
            toast('Saved — opencode.json updated', 'ok');
        } else {
            toast('Saved', 'ok');
        }
        await loadProjects(); viewProject(p.slug);
    });
    const del = card.querySelector('#pc-delete');
    if (del) del.addEventListener('click', () => {
        confirmDialog('Delete project', `Remove "${p.name}" from Perun? Memory and indexed docs for it are deleted. The files on disk are untouched.`, async () => {
            await api(`/api/projects/${p.slug}`, { method: 'DELETE' });
            toast('Deleted', 'ok'); await loadProjects(); goto('#/projects');
        }, 'Delete');
    });

    const ide = p.ide || {};
    const linked = !!ide.vscode_linked;
    const launchMode = ide.launch_mode === 'external-terminal'
        ? 'external-terminal' : 'vscode-terminal';
    const ideCard = el('div', { class: 'card' },
        `<div class="card-head"><h4>VS Code</h4>
            <span class="badge ${linked ? 'green' : 'muted'}" id="pc-vscode-badge">${linked ? 'linked' : 'not linked'}</span></div>
         <p class="secondary">Link this project to a VS Code workspace. Linked projects open VS Code next to the
            TUI, follow agent edits, and can host the TUI inside VS Code's terminal.</p>
         <div id="pc-vscode-status" class="meta">checking companion…</div>
         <label class="opt"><input type="checkbox" id="pc-vscode-linked" ${linked ? 'checked' : ''}> Link with Visual Studio Code</label>
         <div class="form-row">
            <label class="field"><span class="lab">Launch TUI in</span>
                <select id="pc-vscode-mode">
                    <option value="vscode-terminal" ${launchMode === 'vscode-terminal' ? 'selected' : ''}>VS Code terminal (seamless selection)</option>
                    <option value="external-terminal" ${launchMode === 'external-terminal' ? 'selected' : ''}>External terminal (TUI survives VS Code)</option>
                </select></label>
            <label class="opt"><input type="checkbox" id="pc-vscode-follow" ${ide.auto_follow_edits === false ? '' : 'checked'}> Follow agent edits</label>
         </div>
         <label class="field"><span class="lab">Workspace file</span>
            <input type="text" id="pc-vscode-workspace" readonly value="${esc(ide.workspace_path || '(generated on save)')}"></label>
         <label class="opt"><input type="checkbox" id="pc-vscode-inproject" ${ide.workspace_in_project ? 'checked' : ''}> Keep the workspace file inside the project (adds it to .git/info/exclude)</label>
         <div class="form-actions">
            <button class="btn primary" id="pc-vscode-save">Save VS Code settings</button>
            <button class="btn ghost" id="pc-vscode-open">Open in VS Code</button>
            <button class="btn ghost" id="pc-vscode-regenerate">Regenerate workspace</button>
         </div>`);
    const idePayload = () => ({
        launch_mode: ideCard.querySelector('#pc-vscode-mode').value,
        auto_follow_edits: ideCard.querySelector('#pc-vscode-follow').checked,
        workspace_in_project: ideCard.querySelector('#pc-vscode-inproject').checked,
    });
    ideCard.querySelector('#pc-vscode-save').addEventListener('click', async () => {
        try {
            if (!ideCard.querySelector('#pc-vscode-linked').checked) {
                await api(`/api/projects/${encodeURIComponent(p.slug)}/vscode/unlink`, { method: 'POST' });
                toast('VS Code unlinked', 'ok');
            } else {
                await api(`/api/projects/${encodeURIComponent(p.slug)}/vscode/link`, {
                    method: 'POST', body: idePayload(),
                });
                toast('VS Code settings saved', 'ok');
            }
            await loadProjects();
            viewProject(p.slug);
        } catch (e) { toast(e.message, 'err'); }
    });
    ideCard.querySelector('#pc-vscode-regenerate').addEventListener('click', async () => {
        try {
            await api(`/api/projects/${encodeURIComponent(p.slug)}/vscode/link`, {
                method: 'POST', body: idePayload(),
            });
            toast('Workspace regenerated', 'ok');
            await loadProjects();
            viewProject(p.slug);
        } catch (e) { toast(e.message, 'err'); }
    });
    ideCard.querySelector('#pc-vscode-open').addEventListener('click', () => openProjectVsCode(p));
    const ideBadge = ideCard.querySelector('#pc-vscode-badge');
    ideCard.querySelector('#pc-vscode-linked').addEventListener('change', (event) => {
        const want = !!event.target.checked;
        if (want === linked) {
            ideBadge.textContent = linked ? 'linked' : 'not linked';
            ideBadge.className = 'badge ' + (linked ? 'green' : 'muted');
            return;
        }
        // The badge reflects the *saved* state; show the pending edit instead of
        // contradicting the checkbox.
        ideBadge.textContent = want ? 'will link (save)' : 'will unlink (save)';
        ideBadge.className = 'badge gold';
    });
    setTimeout(refreshVSCodeStatus, 0);
    const wrap = el('div');
    wrap.append(card, ideCard);
    return wrap;
}

/* ========================================================= TASKS ====== */
async function mountTasks(container, p) {
    container.innerHTML = '<div class="empty"><span class="spinner"></span> loading tasks…</div>';
    let tasks = [];
    try {
        await Promise.all([loadSkills(p.slug), loadTools(p.slug)]);
        tasks = (await api(`/api/projects/${p.slug}/tasks`)).tasks || [];
    }
    catch (e) { container.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    renderTasks(container, p, tasks);
}

function renderTasks(container, p, tasks) {
    container.innerHTML = '';
    const head = el('div', { class: 'card' },
        `<div class="card-head"><h4>Project tasks</h4><span class="badge gold">reusable prompts, skills & tool calls</span></div>
         <p class="secondary">Tasks can be selected from the Agent Console and queued to run after a prompt. Skill tasks are invoked as <code>/skillname</code> slash commands in the TUI. They are saved per project.</p>`);
    container.appendChild(head);
    const list = el('div', { class: 'card list' });
    const refresh = async () => {
        try {
            const res = await api(`/api/projects/${p.slug}/tasks`);
            renderTasks(container, p, res.tasks || []);
        } catch (e) { toast(e.message, 'err'); }
    };
    const editForm = (task) => {
        const isNew = !task;
        const id = task ? task.id : 'new';
        const body = el('div', { class: 'card mt' },
            `<h5>${isNew ? 'New task' : 'Edit task'}</h5>
             <label class="field"><span class="lab">Name</span><input type="text" id="pt-name-${id}" value="${esc(task ? task.name : '')}" placeholder="e.g. run tests"></label>
             <label class="field"><span class="lab">Kind</span><select id="pt-kind-${id}">
                <option value="prompt" ${(!task || task.kind === 'prompt') ? 'selected' : ''}>Prompt</option>
                <option value="skill" ${task && task.kind === 'skill' ? 'selected' : ''}>Skill</option>
                <option value="tool" ${task && task.kind === 'tool' ? 'selected' : ''}>Tool call</option>
                <option value="combo" ${task && task.kind === 'combo' ? 'selected' : ''}>Combination</option>
             </select></label>
             <label class="field"><span class="lab">Prompt / instruction</span><textarea id="pt-prompt-${id}" rows="4" placeholder="What should the agent do? For Skill tasks the selected skills are invoked as /skillname in the TUI; add any extra context here.">${esc(task ? task.prompt : '')}</textarea></label>
             <label class="field" id="pt-tools-wrap-${id}"><span class="lab">Tools</span><span id="pt-tools-host-${id}"></span></label>
             <label class="field"><span class="lab">Skills</span><span id="pt-skills-host-${id}"></span></label>
             <label class="field"><span class="lab">Enabled</span><input type="checkbox" id="pt-enabled-${id}" ${!task || task.enabled ? 'checked' : ''}></label>
             <div class="form-actions">
                <button class="btn primary" id="pt-save-${id}">${isNew ? 'Add' : 'Save'}</button>
                <button class="btn ghost" id="pt-cancel-${id}">Cancel</button>
             </div>`);

        const toolsSel = multiSelect({
            id: `pt-tools-${id}`,
            placeholder: 'Select tools…',
            value: task ? task.tools : [],
            items: (state.tools || []).map((t) => ({ value: t.name, label: t.name, description: t.description })),
        });
        body.querySelector(`#pt-tools-host-${id}`).appendChild(toolsSel);

        const skillsSel = multiSelect({
            id: `pt-skills-${id}`,
            placeholder: 'Select skills…',
            value: task ? task.skills : [],
            items: (state.skills || []).map((s) => ({ value: s.name, label: s.name, description: s.description })),
        });
        body.querySelector(`#pt-skills-host-${id}`).appendChild(skillsSel);

        const toolsWrap = body.querySelector(`#pt-tools-wrap-${id}`);
        const kindSelect = body.querySelector(`#pt-kind-${id}`);
        const updateToolsVisibility = () => {
            // Tools are not applicable to slash-command skill tasks.
            toolsWrap.style.display = kindSelect.value === 'skill' ? 'none' : '';
        };
        kindSelect.addEventListener('change', updateToolsVisibility);
        updateToolsVisibility();

        body.querySelector(`#pt-save-${id}`).addEventListener('click', async () => {
            const payload = {
                name: $(`#pt-name-${id}`).value.trim(),
                kind: $(`#pt-kind-${id}`).value,
                prompt: $(`#pt-prompt-${id}`).value.trim(),
                tools: toolsSel.getValue(),
                skills: skillsSel.getValue(),
                enabled: $(`#pt-enabled-${id}`).checked ? 1 : 0,
            };
            if (!payload.name) { toast('Name is required', 'err'); return; }
            try {
                if (isNew) {
                    await api(`/api/projects/${p.slug}/tasks`, { method: 'POST', body: payload });
                } else {
                    await api(`/api/projects/${p.slug}/tasks/${task.id}`, { method: 'PATCH', body: payload });
                }
                toast('Saved', 'ok');
                body.remove();
                refresh();
            } catch (e) { toast(e.message, 'err'); }
        });
        body.querySelector(`#pt-cancel-${id}`).addEventListener('click', () => body.remove());
        return body;
    };
    for (const t of tasks) {
        const row = el('div', { class: 'list-row' },
            `<span class="badge ${t.enabled ? 'green' : 'muted'}">${esc(t.kind)}</span>
             <span class="grow">
                <div class="truncate"><b>${esc(t.name)}</b> · ${esc(t.prompt.slice(0, 80))}</b></div>
                <div class="muted" style="font-size:.72rem">${t.tools.length ? 'tools: ' + esc(t.tools.join(', ')) : ''} ${t.skills.length ? 'skills: ' + esc(t.skills.join(', ')) : ''}</div>
             </span>
             <button class="btn sm ghost" data-edit="${t.id}">Edit</button>
             <button class="btn sm danger ghost" data-del="${t.id}">✕</button>`);
        row.querySelector('[data-edit]').addEventListener('click', () => {
            // remove any existing edit form
            container.querySelectorAll('.card.mt').forEach((c) => c.remove());
            container.appendChild(editForm(t));
        });
        row.querySelector('[data-del]').addEventListener('click', async () => {
            confirmDialog('Delete task', `Remove "${t.name}"?`, async () => {
                try {
                    await api(`/api/projects/${p.slug}/tasks/${t.id}`, { method: 'DELETE' });
                    toast('Deleted', 'ok'); refresh();
                } catch (e) { toast(e.message, 'err'); }
            }, 'Delete');
        });
        list.appendChild(row);
    }
    if (!tasks.length) list.appendChild(el('div', { class: 'empty' }, 'No tasks yet. Add one to use it in the Agent Console.'));
    container.appendChild(list);
    const addBtn = el('button', { class: 'btn primary' }, '+ Add task');
    addBtn.addEventListener('click', () => {
        container.querySelectorAll('.card.mt').forEach((c) => c.remove());
        container.appendChild(editForm(null));
    });
    container.appendChild(addBtn);
}

/* ============================================================= GIT ====== */
async function mountGit(container, p) {
    container.innerHTML = '<div class="empty"><span class="spinner"></span> loading git…</div>';
    let info;
    try { info = (await api(`/api/projects/${p.slug}/git`)).git; }
    catch (e) { container.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    renderGit(container, p, info);
}

function renderGit(container, p, git) {
    container.innerHTML = '';
    if (!git.available) {
        container.appendChild(el('div', { class: 'card' },
            `<p class="secondary">Git is not installed or not on PATH. Install Git to enable version control for this project.</p>`));
        return;
    }
    const branch = git.branch || p.git_branch || '—';
    const status = el('div', { class: 'card mb' },
        `<div class="row wrap" style="gap:14px;align-items:center">
            <span class="badge ${git.repo ? 'green' : 'muted'}">${git.repo ? 'repository' : 'not initialised'}</span>
            ${git.repo ? `<span class="secondary">branch <b class="mono">${esc(branch)}</b></span>` : ''}
            ${git.repo ? `<span class="secondary">${git.change_count || 0} change(s)</span>` : ''}
            ${git.repo && git.ahead ? `<span class="secondary">↑${git.ahead}</span>` : ''}
            ${git.repo && git.behind ? `<span class="secondary">↓${git.behind}</span>` : ''}
            ${git.remote_url ? `<span class="secondary mono truncate" style="max-width:300px">${esc(git.remote_url)}</span>` : ''}
            <span class="grow"></span>
            <button class="btn sm ghost" id="git-refresh">⟳ Refresh</button>
         </div>`);
    container.appendChild(status);

    if (!git.repo) {
        const init = el('div', { class: 'card' },
            `<h4>Initialise repository</h4>
             <p class="secondary">Create a new Git repository in this folder. Files on disk are never touched beyond a new <span class="mono">.git</span> directory.</p>
             <div class="form-row">
                <label class="field"><span class="lab">Branch</span><input type="text" id="git-branch" value="${esc(p.git_branch || 'main')}"></label>
                <label class="field"><span class="lab">Remote (optional)</span><input type="text" id="git-remote" value="${esc(p.git_remote || '')}" placeholder="https://github.com/you/repo.git"></label>
             </div>
             <div class="form-actions"><button class="btn primary" id="git-init">Initialise</button></div>`);
        container.appendChild(init);
    } else {
        container.appendChild(el('div', { class: 'card mb' },
            `<h4>Remote</h4>
             <div class="form-row">
                <label class="field"><span class="lab">Remote URL</span><input type="text" id="git-remote" value="${esc(git.remote_url || p.git_remote || '')}" placeholder="https://github.com/you/repo.git"></label>
                <label class="field"><span class="lab">Branch</span><input type="text" id="git-branch" value="${esc(p.git_branch || git.branch || 'main')}"></label>
             </div>
             <div class="form-actions">
                <button class="btn primary" id="git-remote-save">Save remote</button>
                <button class="btn ghost" id="git-push">↑ Push</button>
             </div>`));
        container.appendChild(el('div', { class: 'card mb' },
            `<h4>Commit</h4>
             <label class="field"><span class="lab">Message</span><input type="text" id="git-message" placeholder="Describe your changes"></label>
             <div class="form-actions"><button class="btn primary" id="git-commit">Commit all changes</button></div>`));
        container.appendChild(el('div', { class: 'card' },
            `<h4>Recent commits</h4><div id="git-log"><div class="empty"><span class="spinner"></span></div></div>`));
        loadGitLog(container.querySelector('#git-log'), p.slug);
    }

    container.querySelector('#git-refresh').addEventListener('click', () => mountGit(container, p));
    const initBtn = container.querySelector('#git-init');
    if (initBtn) initBtn.addEventListener('click', async () => {
        const b = $('#git-branch').value.trim() || 'main';
        try {
            await api(`/api/projects/${p.slug}/git/init`, { method: 'POST', body: { branch: b } });
            const remote = $('#git-remote').value.trim();
            if (remote) await api(`/api/projects/${p.slug}/git/remote`, { method: 'POST', body: { url: remote, branch: b } });
            toast('Repository initialised', 'ok');
            await loadProjects(); mountGit(container, p);
        } catch (e) { toast(e.message, 'err'); }
    });
    const remoteSave = container.querySelector('#git-remote-save');
    if (remoteSave) remoteSave.addEventListener('click', async () => {
        const url = $('#git-remote').value.trim();
        if (!url) { toast('Enter a remote URL', 'err'); return; }
        try {
            await api(`/api/projects/${p.slug}/git/remote`, { method: 'POST', body: { url, branch: $('#git-branch').value.trim() } });
            toast('Remote saved', 'ok'); await loadProjects(); mountGit(container, p);
        } catch (e) { toast(e.message, 'err'); }
    });
    const commitBtn = container.querySelector('#git-commit');
    if (commitBtn) commitBtn.addEventListener('click', async () => {
        const message = $('#git-message').value.trim();
        if (!message) { toast('Enter a commit message', 'err'); return; }
        try {
            const res = await api(`/api/projects/${p.slug}/git/commit`, { method: 'POST', body: { message } });
            toast(res.committed ? `Committed ${res.sha}` : 'Nothing to commit', res.committed ? 'ok' : '');
            mountGit(container, p);
        } catch (e) { toast(e.message, 'err'); }
    });
    const pushBtn = container.querySelector('#git-push');
    if (pushBtn) pushBtn.addEventListener('click', async () => {
        try {
            const res = await api(`/api/projects/${p.slug}/git/push`, { method: 'POST', body: { branch: $('#git-branch').value.trim() } });
            toast(`Pushed ${res.branch || ''}`, 'ok');
        } catch (e) { toast(e.message, 'err'); }
    });
}

async function loadGitLog(container, slug) {
    if (!container) return;
    try {
        const commits = (await api(`/api/projects/${slug}/git/log?limit=30`)).commits || [];
        if (!commits.length) { container.innerHTML = '<div class="empty">No commits yet.</div>'; return; }
        container.innerHTML = commits.map((c) =>
            `<div class="list-row">
                <span class="badge muted mono">${esc((c.sha || '').slice(0, 7))}</span>
                <span class="grow msg truncate">${esc(c.subject)}</span>
                <span class="muted">${esc(c.author || '')}</span>
                <span class="ts">${esc(c.when || '')}</span>
             </div>`).join('');
    } catch (e) { container.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* ======================================================== MANIFEST ====== */
/* Open a file path from a manifest anchor/docs/link. We try a direct
   window.open to the OS handler (vscode://file/... or file://...).
   Relative targets are normalised so they cannot escape the project root;
   absolute targets (drive letter / UNC / POSIX) are used as written. */
function openManifestPath(projectRoot, relPath) {
    const raw = String(relPath || '').trim();
    if (!raw) return;
    const isAbs = /^[a-z]:[\\/]/i.test(raw) || raw.startsWith('\\\\');   // drive letter / UNC
    // Separators are normalised ("\" → "/", repeats collapsed). Relative targets
    // keep the anti-escape guard (leading "/" and ".." segments dropped); absolute
    // ones are trusted as written — the manifest is local, user-authored content
    // that explicitly names a location outside the project root.
    const slashed = raw.replace(/\\/g, '/').replace(/[\/]+/g, '/');
    const clean = isAbs ? slashed : slashed.replace(/^[\/]+/, '').replace(/\.{2,}(?:[\/]|$)/g, '');
    if (!clean) return;
    const root = String(projectRoot || '').replace(/[\/]+$/, '').replace(/\\/g, '/');
    // Collapse "/./" segments so the handler receives a clean path.
    const absolute = (isAbs ? clean : root + '/' + clean).replace(/\/\.(?=\/|$)/g, '');
    const vscodeUrl = 'vscode://file/' + absolute;
    const fileUrl = 'file:///' + absolute;
    const tryOpen = (url) => {
        const w = window.open(url, '_blank');
        return w;
    };
    const opened = tryOpen(vscodeUrl);
    if (!opened) {
        setTimeout(() => {
            if (!tryOpen(fileUrl)) {
                toast(`Could not open ${clean} — no local file handler`, 'err');
            }
        }, 200);
    }
}

/* Open a manifest link/anchor target: URLs open in a new tab, anything else
   is treated as a local path and launched via the OS handler. */
function openManifestLink(projectRoot, value) {
    const u = String(value || '').trim();
    if (!u) { toast('Enter a target first', 'err'); return; }
    if (u.startsWith('#')) return;                       // in-page anchor — nothing to launch
    if (/^(https?:|mailto:)/i.test(u)) {                 // external URL
        window.open(u, '_blank', 'noopener');            // noopener: a manifest URL is untrusted input
        return;
    }
    if (/^[a-z]:[\\/]/i.test(u) || u.startsWith('\\\\')) {
        openManifestPath(projectRoot, u);                // absolute local path
        return;
    }
    if (/^[a-z][a-z0-9+.-]*:/i.test(u)) {                // other scheme (vscode://, ftp:// …)
        const safe = safeUrl(u);
        if (safe === '#') { toast(`Blocked unsafe link: ${u}`, 'err'); return; }
        window.open(safe, '_blank');
        return;
    }
    openManifestPath(projectRoot, u);
}

/* Wire the links inside rendered manifest Markdown: external ones already open
   in a new tab (target=_blank + noopener), relative/local paths go through the
   OS handler instead of navigating the dashboard away, bare "#…" anchors are
   left alone. Shared by the Overview tab card and the editor's live preview. */
function wireManifestLinks(root, projectRoot) {
    if (!root) return;
    root.querySelectorAll('a[href]').forEach((a) => {
        const href = a.getAttribute('href') || '';
        if (/^https?:\/\//i.test(href) || /^mailto:/i.test(href)) return;
        a.addEventListener('click', (e) => {
            e.preventDefault();
            openManifestLink(projectRoot, href);
        });
    });
}

function manifestOverview(p) {
    const hasManifest = Boolean(p.has_manifest);
    const data = p.manifest || {};
    const body = p.manifest_body || '';
    const box = el('div', { class: 'card mb' });
    // Prefer the resolved on-disk path (absolute); fall back to the canonical
    // name so the header always offers a way to open the manifest itself, not
    // just the files it points at.
    const manifestPath = String(p.manifest_source || '').trim() || 'perun.md';
    const head = el('div', { class: 'card-head' },
        `<h4>Manifest</h4>
         <span class="row wrap" style="gap:8px">
            <span class="badge ${hasManifest ? 'gold' : 'muted'}">perun.md</span>
            ${p.manifest_source ? `<span class="mono muted truncate" style="max-width:260px;font-size:.72rem">${esc(p.manifest_source)}</span>` : ''}
         </span>`);
    if (hasManifest) {
        const open = el('button', { class: 'btn ghost sm', type: 'button', title: `Open ${manifestPath}` }, 'Open perun.md');
        open.addEventListener('click', () => openManifestPath(p.path, manifestPath));
        head.querySelector('.row').appendChild(open);
    }
    box.appendChild(head);

    if (!hasManifest) {
        box.appendChild(el('div', { class: 'empty' },
            'No manifest is synced for this project yet, so the Overview has nothing to show. ' +
            'Open the <b>Manifest</b> tab to read an existing <span class="mono">perun.md</span> or generate one.'));
        const actions = el('div', { class: 'form-actions' });
        const openTab = el('button', { class: 'btn primary sm', type: 'button' }, 'Open Manifest tab');
        openTab.addEventListener('click', () => goto(`#/project/${p.slug}/manifest`));
        actions.appendChild(openTab);
        box.appendChild(actions);
        return box;
    }

    const anchors = data.anchors || [];
    if (anchors.length) {
        box.appendChild(el('div', { class: 'subhead' }, 'Anchors'));
        const list = el('div', { class: 'anchor-list' });
        for (const a of anchors) {
            const target = a.path || a.url || a.command || '';
            const kind = a.kind || 'file';
            const isFile = kind === 'file' || kind === 'doc';
            const isUrl = kind === 'url';
            const external = /^(https?:|mailto:)/i.test(String(target).trim());
            const openable = (isFile || isUrl) && Boolean(target);   // symbol/command stay inert
            let targetHtml;
            if (openable && external) {
                targetHtml = `<a class="ai-target" href="${esc(safeUrl(target))}" target="_blank" rel="noopener noreferrer" title="${esc(target)}">${esc(target)}</a>`;
            } else if (openable) {
                targetHtml = `<button class="ai-target" data-path="${esc(target)}" title="Open ${esc(target)}" type="button">${esc(target)}</button>`;
            } else {
                targetHtml = `<span class="ai-target">${esc(target)}</span>`;
            }
            const item = el('div', { class: 'anchor-item' },
                `<span class="ai-kind">${esc(kind)}</span>
                 <span class="ai-label">${esc(a.label || '')}</span>
                 ${targetHtml}`);
            if (openable && !external) {
                const btn = item.querySelector('[data-path]');
                if (btn) btn.addEventListener('click', () => openManifestLink(p.path, btn.dataset.path));
            }
            list.appendChild(item);
        }
        box.appendChild(list);
    }

    const cmds = Object.entries(data.commands || {});
    if (cmds.length) {
        box.appendChild(el('div', { class: 'subhead' }, 'Commands (copyable — never auto-run)'));
        const row = el('div', { class: 'row wrap' });
        for (const [k, v] of cmds) row.appendChild(el('span', { class: 'chip', title: v }, `<b>${esc(k)}</b>`));
        box.appendChild(row);
    }

    const links = Object.entries(data.links || {});
    const docs = Object.entries(data.docs || {});
    if (docs.length || links.length) {
        box.appendChild(el('div', { class: 'subhead' }, 'Links & docs'));
        const row = el('div', { class: 'row wrap' });
        for (const [k, v] of links) {
            if (/^(https?:|mailto:)/i.test(String(v || '').trim())) {
                row.appendChild(el('a', { class: 'chip chip-link', href: safeUrl(v), target: '_blank', rel: 'noopener noreferrer', title: v }, `<b>${esc(k)}</b> ${esc(v)}`));
            } else {
                const chip = el('button', { class: 'chip chip-link', title: v, type: 'button' }, `<b>${esc(k)}</b> ${esc(v)}`);
                chip.addEventListener('click', () => openManifestLink(p.path, v));
                row.appendChild(chip);
            }
        }
        for (const [k, v] of docs) {
            const chip = el('button', { class: 'chip chip-link', title: v, type: 'button' }, `<b>${esc(k)}</b> ${esc(v)}`);
            chip.addEventListener('click', () => openManifestPath(p.path, v));
            row.appendChild(chip);
        }
        box.appendChild(row);
    }

    const hasContent = anchors.length || cmds.length || links.length || docs.length || body.trim();
    if (!hasContent) {
        box.appendChild(el('div', { class: 'empty' },
            'This manifest has no docs, links, commands, anchors or overview body yet. ' +
            'Open the <b>Manifest</b> tab to add content, or run ' +
            `<span class="mono">python -m perun backfill-docs ${esc(p.slug)}</span>.`));
    }

    if (body && body.trim()) {
        box.appendChild(el('div', { class: 'subhead' }, 'Overview'));
        const md = el('div', { class: 'md-body', html: renderMarkdown(body) });
        box.appendChild(md);
        wireManifestLinks(md, p.path);
    }
    return box;
}

async function mountManifest(container, p) {
    container.innerHTML = '<div class="empty"><span class="spinner"></span> loading manifest…</div>';
    // This tab is the editor. The read-only manifest card (anchors, links &
    // docs, rendered body) belongs to the Overview tab — see renderProjectTab.
    let snap;
    try {
        snap = (await api(`/api/projects/${p.slug}/manifest`)).manifest;
    }
    catch (e) { container.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    renderManifestEditor(container, p, snap || {});
}

function renderManifestEditor(container, p, snap) {
    container.innerHTML = '';
    const found = !!snap.found;
    const data = snap.data || {};
    const body = snap.body || '';
    const warnings = snap.warnings || [];

    // Typing anywhere in the editor marks it dirty. Reload/Generate rebuild the
    // editor from server state, so ask before discarding unsaved edits.
    let dirty = false;
    const confirmDiscard = (message, proceed) => {
        if (!dirty) { proceed(); return; }
        const body = el('div', {}, `<p class="secondary">${esc(message)}</p>`);
        modal('Unsaved changes', body, [
            { label: 'Cancel', class: 'ghost', onClick: closeModal },
            { label: 'Discard changes', class: 'primary', onClick: () => { closeModal(); proceed(); } },
        ]);
    };

    const status = el('div', { class: 'card mb' });
    status.appendChild(el('div', { class: 'card-head' },
        `<h4>Manifest</h4>
         <span class="row wrap" style="gap:8px">
            <span class="badge ${found ? 'green' : 'muted'}">${found ? 'on disk' : 'not found'}</span>
            ${snap.path ? `<span class="mono muted truncate" style="max-width:280px;font-size:.74rem">${esc(snap.path)}</span>` : ''}
         </span>`));
    status.appendChild(el('p', { class: 'secondary' },
        `A <span class="mono">perun.md</span> manifest lets a project describe itself: links, docs, commands, and <b>anchors</b> into its own content. Perun reads it automatically; edits here write back to disk explicitly.`));
    if (warnings.length) {
        status.appendChild(el('div', { class: 'mono-box', style: 'border-color:rgba(224,168,60,.4);color:var(--amber);margin-bottom:12px' },
            warnings.map((w) => '⚠ ' + esc(w)).join('\n')));
    }
    const actions = el('div', { class: 'form-actions' });
    if (found) {
        const reload = el('button', { class: 'btn ghost' }, '⟳ Reload from disk');
        reload.addEventListener('click', () => {
            confirmDiscard('Reloading from disk replaces this editor and discards unsaved edits. Continue?', async () => {
                try {
                    const r = await api(`/api/projects/${p.slug}/manifest/refresh`, { method: 'POST' });
                    toast('Reloaded from disk', 'ok');
                    await loadProjects();
                    renderManifestEditor(container, p, r.manifest);
                } catch (e) { toast(e.message, 'err'); }
            });
        });
        actions.appendChild(reload);
    } else {
        const gen = el('button', { class: 'btn primary' }, '＋ Generate from registry');
        gen.addEventListener('click', () => {
            confirmDiscard('Generating from the registry replaces this editor and discards unsaved edits. Continue?', async () => {
                try {
                    const r = await api(`/api/projects/${p.slug}/manifest/export`, { method: 'POST' });
                    toast('Manifest generated', 'ok');
                    await loadProjects();
                    renderManifestEditor(container, p, r.manifest);
                } catch (e) { toast(e.message, 'err'); }
            });
        });
        actions.appendChild(gen);
    }
    status.appendChild(actions);
    container.appendChild(status);

    // ---- editor ----
    const card = el('div', { class: 'card' });
    card.addEventListener('input', () => { dirty = true; });
    card.appendChild(el('div', { class: 'card-head' }, '<h4>Edit manifest</h4>'));
    card.innerHTML += `
        <div class="form-row">
            <label class="field"><span class="lab">Name</span><input type="text" id="mf-name" value="${esc(data.name || p.name)}"></label>
            <label class="field"><span class="lab">Kind</span><input type="text" id="mf-kind" value="${esc(data.kind || p.kind)}"></label>
        </div>
        <label class="field"><span class="lab">Description</span><input type="text" id="mf-desc" value="${esc(data.description || '')}"></label>
        <div class="form-row">
            <label class="field"><span class="lab">Model</span><input type="text" id="mf-model" value="${esc(data.model || '')}" placeholder="provider/model"></label>
            <label class="field"><span class="lab">Agent</span><input type="text" id="mf-agent" value="${esc(data.agent || '')}" placeholder="build"></label>
        </div>
        <label class="field"><span class="lab">Tags (comma separated)</span><input type="text" id="mf-tags" value="${esc((data.tags || []).join(', '))}"></label>`;

    const mkKvSection = (title, group, pairs, placeholder) => {
        card.appendChild(el('div', { class: 'subhead' }, title));
        const host = el('div', { class: 'kv-editor' });
        card.appendChild(host);
        for (const [k, v] of Object.entries(pairs || {})) addKvRow(host, group, k, v, placeholder, p.path);
        const add = el('button', { class: 'btn sm ghost' }, '＋ Add');
        add.addEventListener('click', () => addKvRow(host, group, '', '', placeholder, p.path));
        card.appendChild(add);
        return host;
    };
    mkKvSection('Documentation (key → relative path)', 'docs', data.docs, 'relative/path.md');
    mkKvSection('Commands (copyable — never auto-run)', 'commands', data.commands, 'python -m pytest');
    mkKvSection('Links', 'links', data.links, 'https://…');

    card.appendChild(el('div', { class: 'subhead' }, 'Anchors'));
    const anchorHost = el('div', { class: 'anchor-editor' });
    card.appendChild(anchorHost);
    for (const a of (data.anchors || [])) addAnchorRow(anchorHost, a, p.path);
    const anchorAdd = el('button', { class: 'btn sm ghost' }, '＋ Add anchor');
    anchorAdd.addEventListener('click', () => addAnchorRow(anchorHost, { kind: 'file' }, p.path));
    card.appendChild(anchorAdd);

    card.appendChild(el('div', { class: 'subhead' }, 'Overview body (Markdown)'));
    const ta = el('textarea', { id: 'mf-body', rows: '10', placeholder: '# Project overview\n\nFree-form Markdown shown on the project page.' });
    ta.value = body;
    card.appendChild(ta);

    // Live preview — the same rendering the Overview tab shows, so editing the
    // body isn't blind. renderMarkdown() escapes by default, so this adds no new
    // HTML surface; links are wired exactly like the read-only card's body.
    card.appendChild(el('div', { class: 'subhead' }, 'Preview'));
    const preview = el('div', { class: 'md-body manifest-preview' });
    card.appendChild(preview);
    const paintPreview = () => {
        const md = ta.value;
        preview.innerHTML = md.trim() ? renderMarkdown(md) : '<div class="empty">Nothing to preview yet.</div>';
        wireManifestLinks(preview, p.path);
    };
    // Debounced: one repaint per pause in typing, not per keystroke.
    let repaintTimer = null;
    ta.addEventListener('input', () => {
        clearTimeout(repaintTimer);
        repaintTimer = setTimeout(paintPreview, 150);
    });
    paintPreview();

    const saveRow = el('div', { class: 'form-actions' });
    const save = el('button', { class: 'btn primary' }, '💾 Save manifest to disk');
    save.addEventListener('click', async () => {
        const payload = gatherManifest(card);
        try {
            const r = await api(`/api/projects/${p.slug}/manifest`, { method: 'PUT', body: payload });
            toast('Manifest saved', 'ok');
            await loadProjects();
            renderManifestEditor(container, p, r.manifest);
        } catch (e) { toast(e.message, 'err'); }
    });
    saveRow.appendChild(save);
    card.appendChild(saveRow);
    container.appendChild(card);
}

function addKvRow(host, group, key, value, placeholder, projectRoot) {
    const row = el('div', { class: 'kv-edit-row', dataset: { group } });
    const k = el('input', { type: 'text', value: key || '', placeholder: 'key', dataset: { role: 'key' } });
    const v = el('input', { type: 'text', value: value || '', placeholder: placeholder || 'value', dataset: { role: 'value' } });
    const del = el('button', { class: 'icon-btn', title: 'Remove', type: 'button' }, '✕');
    del.addEventListener('click', () => row.remove());
    row.appendChild(k); row.appendChild(v); row.appendChild(del);
    // Documentation rows get an Open button so the file can be launched from the editor.
    if (group === 'docs' && projectRoot) {
        const open = el('button', { class: 'icon-btn open-btn', title: 'Open file', type: 'button' }, '↗');
        open.addEventListener('click', () => openManifestPath(projectRoot, v.value));
        row.appendChild(open);
    }
    // Link rows get an Open button (URL opens in a new tab, local path via the OS handler).
    if (group === 'links') {
        const open = el('button', { class: 'icon-btn open-btn', title: 'Open link', type: 'button' }, '↗');
        open.addEventListener('click', () => openManifestLink(projectRoot, v.value));
        row.appendChild(open);
    }
    host.appendChild(row);
    return row;
}

function gatherKv(host, group) {
    const out = {};
    host.querySelectorAll(`.kv-edit-row[data-group="${group}"]`).forEach((row) => {
        const k = row.querySelector('[data-role=key]');
        const v = row.querySelector('[data-role=value]');
        if (!k || !v) return;
        const key = k.value.trim();
        const val = v.value.trim();
        if (key && val) out[key] = val;
    });
    return out;
}

function addAnchorRow(host, a, projectRoot) {
    a = a || {};
    const row = el('div', { class: 'anchor-row' });
    const label = el('input', { type: 'text', value: a.label || '', placeholder: 'Label', dataset: { role: 'label' } });
    const kind = el('select', { dataset: { role: 'kind' } });
    for (const k of ['file', 'doc', 'symbol', 'url', 'command']) {
        const o = el('option', { value: k }, k);
        if (k === (a.kind || 'file')) o.selected = true;
        kind.appendChild(o);
    }
    const target = el('input', { type: 'text', value: a.path || a.url || a.command || '', placeholder: 'path / url / command', dataset: { role: 'target' } });
    const del = el('button', { class: 'icon-btn', title: 'Remove', type: 'button' }, '✕');
    del.addEventListener('click', () => row.remove());
    row.appendChild(label); row.appendChild(kind); row.appendChild(target); row.appendChild(del);
    // Open button — the action follows the currently selected kind.
    if (projectRoot) {
        const open = el('button', { class: 'icon-btn open-btn', title: 'Open target', type: 'button' }, '↗');
        open.addEventListener('click', () => {
            const value = target.value.trim();
            if (!value) { toast('Enter a target first', 'err'); return; }
            if (kind.value === 'command') copyCommandToClipboard(value);
            else openManifestLink(projectRoot, value);
        });
        row.appendChild(open);
    }
    host.appendChild(row);
    return row;
}

function gatherManifest(card) {
    const data = {
        version: 1,
        name: card.querySelector('#mf-name').value.trim(),
        kind: card.querySelector('#mf-kind').value.trim(),
        description: card.querySelector('#mf-desc').value.trim(),
        model: card.querySelector('#mf-model').value.trim(),
        agent: card.querySelector('#mf-agent').value.trim(),
        tags: card.querySelector('#mf-tags').value.split(',').map((s) => s.trim()).filter(Boolean),
        docs: gatherKv(card, 'docs'),
        commands: gatherKv(card, 'commands'),
        links: gatherKv(card, 'links'),
        anchors: [],
    };
    card.querySelectorAll('.anchor-row').forEach((row) => {
        const label = row.querySelector('[data-role=label]').value.trim();
        const kind = row.querySelector('[data-role=kind]').value;
        const target = row.querySelector('[data-role=target]').value.trim();
        if (!label || !target) return;
        const a = { label, kind };
        if (kind === 'url') a.url = target;
        else if (kind === 'command') a.command = target;
        else a.path = target;
        data.anchors.push(a);
    });
    return { data, body: card.querySelector('#mf-body').value };
}

/* ===================================================== ORCHESTRATOR ===== */
async function viewOrchestrator() {
    $('#topbar-title').textContent = 'Orchestrator';
    await Promise.all([loadHealth(), loadSettings(), loadProjects(), loadModels(), loadAgents()]);
    const controlSlug = (state.health && state.health.control) || '';
    const s = state.settings || {};
    const v = $('#view');
    v.innerHTML = '';

    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Control plane</div><h1>Perun Orchestrator</h1>
          <div class="sub">Talk to Perun directly — no project needed. It can list, describe, create, launch, and remove projects for you.</div></div>
         <div class="row wrap orch-head-tools">
            <span class="mselect-label">Model</span>
            <span id="orch-model-host"></span>
            <span class="mselect-label">Agent</span>
            <span id="orch-agent-host"></span>
            <button class="btn ghost sm" id="orch-new">＋ New session</button>
         </div>`));

    // Both pickers edit the machine-wide orchestrator settings (the same fields
    // as Settings → Orchestrator). They are shortcuts, not per-chat overrides.
    const msModel = modelSelect({
        value: s.orchestrator_model || s.default_model || '',
        placeholder: 'choose model',
        onSelect: (id) => persistOrchModel(id),
    });
    v.querySelector('#orch-model-host').appendChild(msModel);
    const msAgent = agentSelect({
        value: s.orchestrator_agent || '',
        placeholder: 'perun-orchestrator (default)',
        onSelect: (id) => persistOrchAgent(id),
    });
    v.querySelector('#orch-agent-host').appendChild(msAgent);

    const chat = el('div', { class: 'chat card' });
    const log = el('div', { class: 'chat-log' });
    const composer = el('div', { class: 'chat-composer' });
    const ta = el('textarea', { id: 'orch-input', rows: '1', placeholder: 'Ask Perun to do something…  (Enter to send · Shift+Enter for a newline)' });
    const sendBtn = el('button', { class: 'btn primary', id: 'orch-send' }, '➤ Send');
    composer.appendChild(ta); composer.appendChild(sendBtn);
    chat.appendChild(log); chat.appendChild(composer);
    v.appendChild(chat);

    const bubble = (role, text = '') => {
        const msg = el('div', { class: 'chat-msg ' + role });
        msg.appendChild(el('div', { class: 'chat-role' }, role === 'user' ? 'You' : 'Perun'));
        const body = el('div', { class: 'chat-body md-body' });
        if (text) body.innerHTML = renderMarkdown(text);
        msg.appendChild(body);
        log.appendChild(msg);
        log.scrollTop = log.scrollHeight;
        return body;
    };
    const greeting = () => {
        log.innerHTML = '';
        bubble('assistant', 'Ask me anything about your projects — or tell me to create, launch, or remove one. Use the model and agent pickers above to change how I run.');
    };
    greeting();

    v.querySelector('#orch-new').addEventListener('click', () => {
        state.orchestrator = { sessionPk: null, ocSid: '' };
        greeting();
        toast('New orchestrator session', 'ok');
    });

    if (!controlSlug) {
        log.appendChild(el('div', { class: 'empty' }, 'The orchestrator is not registered. Start Perun with `python -m perun serve`, then reload.'));
        sendBtn.disabled = true; ta.disabled = true;
        return;
    }

    let running = false;
    const setRunning = (on) => { running = on; sendBtn.disabled = on; ta.disabled = on; };

    const sendPrompt = async () => {
        const prompt = ta.value.trim();
        if (!prompt || running) return;
        ta.value = '';
        bubble('user', prompt);
        const asst = bubble('assistant');
        asst.innerHTML = '<span class="spinner"></span>';
        setRunning(true);
        let acc = '';
        try {
            const body = {
                project: controlSlug,
                prompt,
                model: msModel.getValue(),
                agent: msAgent.getValue(),
                session_pk: state.orchestrator.sessionPk,
                opencode_session_id: state.orchestrator.ocSid,
            };
            const res = await api('/api/runs', { method: 'POST', body });
            const uid = res.run.run_uid;
            refreshPresence();
            perunFx.flash($('#perun-sigil'));
            let replyFlashed = false;
            streamOrchestrator(uid, (ev) => {
                if (ev.type === 'text') {
                    if (!replyFlashed) {
                        replyFlashed = true;
                        perunFx.flash($('#perun-sigil'));
                    }
                    acc += ev.text || '';
                    asst.innerHTML = renderMarkdown(acc) || '<span class="spinner"></span>';
                    log.scrollTop = log.scrollHeight;
                } else if (ev.type === 'error') {
                    acc += `\n\n> ⚠ ${ev.text || 'error'}`;
                    asst.innerHTML = renderMarkdown(acc);
                }
            }, async (status) => {
                setRunning(false);
                if (!acc) asst.innerHTML = `<span class="muted">(${esc(status || 'no output')})</span>`;
                try {
                    const r = await api(`/api/runs/${uid}`);
                    const run = r.run || {};
                    state.orchestrator.sessionPk = run.session_pk || state.orchestrator.sessionPk;
                    state.orchestrator.ocSid = run.opencode_session_id || state.orchestrator.ocSid;
                } catch (_) { /* keep the previous session */ }
                loadProjects();
                refreshPresence();
            });
        } catch (e) {
            setRunning(false);
            asst.innerHTML = `<span class="err">${esc(e.message)}</span>`;
            toast(e.message, 'err');
        }
    };

    sendBtn.addEventListener('click', sendPrompt);
    ta.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendPrompt(); }
    });
    ta.focus();
}

async function persistOrchModel(id) {
    try {
        await api('/api/settings', { method: 'PATCH', body: { orchestrator_model: id } });
        if (state.settings) state.settings.orchestrator_model = id;
        toast('Orchestrator model saved', 'ok');
    } catch (e) { toast(e.message, 'err'); }
}

async function persistOrchAgent(id) {
    try {
        await api('/api/settings', { method: 'PATCH', body: { orchestrator_agent: id } });
        if (state.settings) state.settings.orchestrator_agent = id;
        toast('Orchestrator agent saved', 'ok');
    } catch (e) { toast(e.message, 'err'); }
}

function streamOrchestrator(uid, onEvent, onEnd, attempt = 0) {
    if (state.runSource) state.runSource.close();
    const src = new EventSource(`/api/runs/${uid}/stream`);
    state.runSource = src;
    src.onmessage = (ev) => {
        attempt = 0; // Reset backoff after a successful message.
        let d; try { d = JSON.parse(ev.data); } catch (_) { return; }
        if (onEvent) onEvent(d);
        if (d.type === 'end') {
            src.close();
            if (state.runSource === src) state.runSource = null;
            if (onEnd) onEnd(d.status);
        }
    };
    src.onerror = () => {
        // The run may still be alive server-side; reconnect with capped backoff.
        if (state.runSource === src) state.runSource = null;
        src.close();
        const delay = Math.min(1000 * Math.pow(2, attempt), 30000);
        if (attempt < 10) {
            setTimeout(() => streamOrchestrator(uid, onEvent, onEnd, attempt + 1), delay);
        }
    };
}

/* ========================================================= CONSOLE ====== */
async function viewConsole(slug) {
    $('#topbar-title').textContent = 'Agent Console';
    await Promise.all([loadProjects(), loadTerminals()]);
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Agent</div><h1>Agent Console</h1>
          <div class="sub">Start an OpenCode agent in any project and watch it work live.</div></div>`));
    const host = el('div');
    v.appendChild(host);
    const project = slug ? projectBySlug(slug) : state.projects[0];
    mountConsole(host, project);
}

function mountConsole(host, initial) {
    host.innerHTML = '';
    const options = state.projects.map((p) =>
        `<option value="${esc(p.slug)}" ${initial && p.slug === initial.slug ? 'selected' : ''}>${esc(p.name)} (${esc(p.slug)})</option>`).join('');

    const wrap = el('div', { class: 'console-wrap' });
    const header = el('div', { class: 'console-header card' },
        `<div class="form-row">
            <label class="field"><span class="lab">Project</span><select id="c-project">${options}</select></label>
            <label class="field"><span class="lab">Session</span><select id="c-session"><option value="">＋ New session</option></select></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">Terminal</span><select id="c-terminal"><option value="">${esc(state.terminals && state.terminals.configured_source === 'os' ? 'System default' : 'Custom...')}</option>${(state.terminals && state.terminals.terminals || []).map((t) => `<option value="${esc(t.id)}" ${t.id === (state.settings && state.settings.terminal || state.terminals && state.terminals.configured) ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}</select></label>
         </div>`);

    const log = el('div', { class: 'console-chat', id: 'c-chat' },
        '<div class="chat-placeholder muted">Agent output will appear here…</div>');
    const composer = el('div', { class: 'console-composer card' },
        `<div id="c-task-queue" class="task-queue"></div>
         <label class="field"><span class="lab">Prompt</span><textarea id="c-prompt" rows="3" placeholder="Optional prompt. Use ＋ Queue task to add reusable tasks (skills run as /slash commands), then click Run."></textarea></label>
         <div class="form-actions">
            <button class="btn primary" id="c-run">▶ Run</button>
            <button class="btn danger" id="c-stop" disabled>■ Stop</button>
            <button class="btn ghost" id="c-add-task" title="Add a reusable project task to the queue">＋ Queue task</button>
            <button class="btn ghost" id="c-clear">Clear</button>
         </div>`);
    wrap.appendChild(header);
    wrap.appendChild(log);
    wrap.appendChild(composer);
    host.appendChild(wrap);

    let currentRun = null;
    let taskQueue = [];
    let projectTasks = [];

    const logEl = () => $('#c-chat');
    const promptEl = () => $('#c-prompt');
    const queueEl = () => $('#c-task-queue');
    const runBtn = $('#c-run');
    const stopBtn = $('#c-stop');

    const logLine = (text) => {
        const d = el('div', { class: 'chat-sys' }, esc(text));
        logEl().appendChild(d);
        logEl().scrollTop = logEl().scrollHeight;
    };

    let dragIdx = null;
    const updateRunLabel = () => {
        runBtn.textContent = taskQueue.length ? `▶ Run ${taskQueue.length} task${taskQueue.length > 1 ? 's' : ''}` : '▶ Run';
    };
    const updateQueue = () => {
        const el = queueEl();
        el.innerHTML = taskQueue.length
            ? taskQueue.map((t, i) => `<span class="chip" draggable="true" data-idx="${i}"><span class="grip">≡</span> ${esc(t.name)} <b data-rm="${i}">×</b></span>`).join('')
            : '<span class="muted">No tasks queued</span>';
        updateRunLabel();
        el.querySelectorAll('[data-rm]').forEach((b) => b.addEventListener('click', (e) => {
            e.stopPropagation();
            taskQueue.splice(Number(b.dataset.rm), 1);
            updateQueue();
        }));
        el.querySelectorAll('.chip').forEach((chip) => {
            chip.addEventListener('dragstart', (e) => {
                dragIdx = Number(chip.dataset.idx);
                chip.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
            });
            chip.addEventListener('dragend', () => {
                chip.classList.remove('dragging');
                dragIdx = null;
                el.querySelectorAll('.drop-marker').forEach((m) => m.remove());
            });
            chip.addEventListener('dragover', (e) => {
                e.preventDefault();
                if (dragIdx === null) return;
                const targetIdx = Number(chip.dataset.idx);
                if (targetIdx === dragIdx) return;
                const rect = chip.getBoundingClientRect();
                const before = e.clientX < rect.left + rect.width / 2;
                // Move in array and re-render.
                const moved = taskQueue.splice(dragIdx, 1)[0];
                const newIdx = before ? targetIdx : targetIdx + 1;
                taskQueue.splice(newIdx, 0, moved);
                dragIdx = newIdx;
                updateQueue();
            });
        });
    };

    const renderTaskPicker = () => {
        const filtered = projectTasks.filter((t) => t.enabled && !taskQueue.some((q) => q.id === t.id));
        if (!filtered.length) { toast('No enabled tasks available', 'ok'); return; }
        const list = el('div', {},
            `<input type="text" id="task-search" class="field" placeholder="Search tasks…" style="width:100%;margin-bottom:8px">` +
            filtered.map((t) => `<div class="mselect-item" data-task="${t.id}"><b>${esc(t.name)}</b> <span class="muted">${esc(t.kind)} · ${esc(t.prompt.slice(0, 50))}</span></div>`).join('')
        );
        const update = () => {
            const q = $('#task-search').value.toLowerCase();
            list.querySelectorAll('[data-task]').forEach((it) => {
                const t = projectTasks.find((x) => x.id === Number(it.dataset.task));
                it.style.display = t && (t.name.toLowerCase().includes(q) || t.prompt.toLowerCase().includes(q)) ? '' : 'none';
            });
        };
        list.querySelector('#task-search').addEventListener('input', update);
        list.addEventListener('click', (e) => {
            const it = e.target.closest('[data-task]');
            if (!it) return;
            const t = projectTasks.find((x) => x.id === Number(it.dataset.task));
            if (t) { taskQueue.push(t); updateQueue(); closeModal(); }
        });
        modal('Add project task', list, [{ label: 'Close', class: 'ghost', onClick: closeModal }]);
    };

    const loadProjectTasks = async () => {
        const slug = $('#c-project').value;
        if (!slug) { projectTasks = []; return; }
        try { projectTasks = (await api(`/api/projects/${slug}/tasks`)).tasks || []; }
        catch (_) { projectTasks = []; }
    };

    async function loadSessions() {
        const slug = $('#c-project').value;
        const sel = $('#c-session');
        sel.innerHTML = '<option value="">＋ New session</option>';
        if (!slug) return;
        try {
            const detail = await api(`/api/projects/${slug}/sessions`);
            for (const s of detail.sessions || []) {
                // A session without an OpenCode id cannot be resumed: there is
                // nothing to pass to `opencode --session`.
                if (!String(s.opencode_session_id || '').trim()) continue;
                const origin = SessionPick.originLabel(s);
                const label = `${(s.title || '(untitled)').slice(0, 40)} · ${timeAgo(s.updated_at)}`
                    + (origin ? ` · ${origin}` : '');
                const o = el('option', { value: s.id }, esc(label));
                o.dataset.oc = s.opencode_session_id || '';
                o.dataset.tui = s.tui === false ? 'false' : (s.tui === true ? 'true' : '');
                sel.appendChild(o);
            }
            // Default to the latest resumable session (a headless-only one is
            // listed but never preselected); "New session" stays the first,
            // explicit choice.
            const latest = [...sel.options].find(
                (o) => o.dataset && o.dataset.oc && o.dataset.tui !== 'false');
            if (latest) sel.value = latest.value;
        } catch (_) { /* ignore */ }
        await loadProjectTasks();
    }

    $('#c-project').addEventListener('change', loadSessions);
    $('#c-add-task').addEventListener('click', renderTaskPicker);
    $('#c-clear').addEventListener('click', () => { logEl().innerHTML = ''; promptEl().value = ''; taskQueue = []; updateQueue(); });

    runBtn.addEventListener('click', async () => {
        const slug = $('#c-project').value;
        if (!slug) { toast('Pick a project', 'err'); return; }
        const p = projectBySlug(slug);
        if (!p) { toast('Unknown project', 'err'); return; }
        const prompt = buildPromptChain(taskQueue, promptEl().value.trim());
        if (!prompt) { toast('Enter a prompt or queue a task', 'err'); return; }
        logLine('› ' + (promptEl().value.trim() || 'Queued tasks only'));
        try {
            // Carry the session the chat is already on, so Finish can capture
            // this run's usage (a TUI launched without one has no session id).
            const sessionOpt = $('#c-session').selectedOptions[0];
            const sessionId = sessionOpt ? (sessionOpt.dataset.oc || '') : '';
            const sessionPk = sessionOpt && sessionOpt.value
                ? Number(sessionOpt.value) || null : null;
            const res = await api('/api/runs/tui', {
                method: 'POST',
                body: { project: slug, prompt, model: p.model, agent: p.agent,
                        opencode_session_id: sessionId, session_pk: sessionPk },
            });
            const runUid = res && res.run && res.run.run_uid;
            if (!runUid) { throw new Error('Perun did not return a run id'); }
            currentRun = runUid;
            runBtn.disabled = true; stopBtn.disabled = false;
            await refreshPresence();
            perunFx.flash($('#perun-sigil'));
            if (!p.path) { toast('Selected project has no folder path.', 'err'); return; }
            const opened = await launchInTerminal({
                project: p.slug, cwd: p.path, model: p.model, agent: p.agent,
                prompt, tui: true, runUid: currentRun, sessionId,
            });
            if (!opened || !opened.ok) {
                if (opened && opened.definitive) {
                    // No terminal exists: finalize the run record so the console
                    // does not show a phantom session.
                    try { await api(`/api/runs/${currentRun}/cancel`, { method: 'POST' }); }
                    catch (_) { /* best effort */ }
                    currentRun = null;
                    runBtn.disabled = false;
                    stopBtn.disabled = true;
                    logLine('terminal launch failed; run finalized');
                    await refreshPresence();
                } else {
                    // The terminal may have opened before the response was lost:
                    // keep the run so Stop can finalize it.
                    logLine('terminal launch outcome unknown; use Stop to finalize the run');
                }
                return;
            }
            promptEl().value = '';
            taskQueue = [];
            updateQueue();
            logLine(`[TUI session ${currentRun}] opened in ${slug}`);
        } catch (e) { logLine('error: ' + e.message); toast(e.message, 'err'); runBtn.disabled = false; stopBtn.disabled = true; currentRun = null; }
    });

    stopBtn.addEventListener('click', async () => {
        if (!currentRun) return;
        try {
            // /cancel finalises the run even if the terminal process was not
            // tracked, so the Agent Console state never leaves a zombie run.
            await api(`/api/runs/${currentRun}/cancel`, { method: 'POST' });
            logLine('[TUI session closed]');
        } catch (e) { logLine('stop warning: ' + e.message); }
        runBtn.disabled = false; stopBtn.disabled = true; currentRun = null;
        await refreshPresence(); await loadProjects();
    });

    loadSessions();
    updateQueue();
}

/* ========================================================== MEMORY ====== */
async function viewMemory() {
    $('#topbar-title').textContent = 'Memory';
    await loadProjects();
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Unified memory</div><h1>Memory Explorer</h1>
          <div class="sub">Facts and indexed documentation, searchable across every project.</div></div>`));

    const opts = ['<option value="">All scopes / current project</option>']
        .concat(state.projects.map((p) => `<option value="${esc(p.slug)}">${esc(p.name)}</option>`)).join('');
    v.appendChild(el('div', { class: 'card mb' },
        `<div class="form-row">
            <label class="field"><span class="lab">Query</span><input type="text" id="m-q" placeholder="e.g. how does the request gate prevent 429 storms"></label>
            <label class="field"><span class="lab">Project</span><select id="m-project">${opts}</select></label>
         </div>
         <div class="form-actions">
            <button class="btn primary" id="m-search">◇ Search</button>
            <button class="btn ghost" id="m-import">⤓ Import legacy memory</button>
            <button class="btn ghost" id="m-repair">⟳ Repair embeddings</button>
         </div>`));
    const results = el('div', { id: 'm-results' });
    v.appendChild(results);

    const loadData = async () => {
        const q = $('#m-q').value.trim();
        const project = $('#m-project').value || null;
        results.innerHTML = '<div class="empty"><span class="spinner"></span> loading…</div>';
        try {
            if (q) {
                const res = await api('/api/memory/search', {
                    method: 'POST',
                    body: { query: q, project, k: 8, all_projects: !project },
                });
                renderResults(results, res.results || []);
            } else {
                const params = project ? `?project=${encodeURIComponent(project)}&limit=40` : '?limit=40';
                const [recent, stats, usage] = await Promise.all([
                    api('/api/memory/recent' + params),
                    api('/api/memory/stats' + (project ? `?project=${encodeURIComponent(project)}` : '')),
                    api('/api/memory/usage' + (project ? `?project=${encodeURIComponent(project)}` : '')),
                ]);
                results.innerHTML = '';
                results.appendChild(renderMemoryDashboard(stats.stats || {}, usage.usage || {}, recent.facts || []));
            }
        } catch (e) { results.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    };

    $('#m-search').addEventListener('click', loadData);
    $('#m-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadData(); });
    $('#m-project').addEventListener('change', loadData);
    $('#m-import').addEventListener('click', async () => {
        try { const r = await api('/api/memory/import-legacy', { method: 'POST', body: {} }); toast(`Imported ${r.imported || 0} fact(s)`, 'ok'); }
        catch (e) { toast(e.message, 'err'); }
    });
    $('#m-repair').addEventListener('click', async () => {
        try {
            const r = await api('/api/memory/reembed', { method: 'POST', body: { limit: 200 } });
            toast(`Re-embedded ${r.embedded_facts ?? 0} facts, ${r.embedded_chunks ?? 0} chunks`, r.ok ? 'ok' : 'err');
            await loadData();
        } catch (e) { toast(e.message, 'err'); }
    });

    // Initial load uses the default “All scopes” selection.
    await loadData();
}

function renderMemoryDashboard(stats, usage, recent) {
    const wrap = el('div', { class: 'memory-dashboard' });

    // Core counts
    wrap.appendChild(el('div', { class: 'grid stats-grid mb' },
        `<div class="card stat"><div class="stat-label">Facts</div><div class="stat-value">${stats.facts_total ?? 0}</div></div>
         <div class="card stat"><div class="stat-label">Doc chunks</div><div class="stat-value">${stats.chunks_total ?? 0}</div></div>
         <div class="card stat"><div class="stat-label">Embedded</div><div class="stat-value">${stats.facts_embedded ?? 0}</div></div>
         <div class="card stat ${(stats.facts_pending_embed ?? 0) + (stats.chunks_pending_embed ?? 0) > 0 ? 'warn' : ''}"><div class="stat-label">Pending embed</div><div class="stat-value">${(stats.facts_pending_embed ?? 0) + (stats.chunks_pending_embed ?? 0)}</div></div>`));

    // Kind breakdown
    const byKind = stats.facts_by_kind || {};
    const kindRows = Object.entries(byKind).sort((a, b) => b[1] - a[1]).map(([k, n]) =>
        `<tr><td><span class="badge ${kindColor(k)}">${esc(k)}</span></td><td class="num">${n}</td></tr>`).join('') ||
        '<tr><td colspan="2" class="muted">No facts</td></tr>';
    const kindCard = el('div', { class: 'card mb' },
        `<h3 class="section-title">Facts by kind</h3>
         <table class="kv-table"><tbody>${kindRows}</tbody></table>`);

    // Usage window
    const zeroRate = Math.round((usage.zero_result_rate ?? 0) * 100);
    const usageRows = [
        `<tr><td>Searches in window</td><td class="num">${usage.total_searches ?? 0}</td></tr>`,
        `<tr><td>Zero-result rate</td><td class="num ${zeroRate > 30 ? 'warn' : ''}">${zeroRate}%</td></tr>`,
        `<tr><td>Avg results / search</td><td class="num">${usage.avg_results_per_search ?? 0}</td></tr>`,
    ].join('');
    const usageCard = el('div', { class: 'card mb' },
        `<h3 class="section-title">Recent usage</h3>
         <table class="kv-table"><tbody>${usageRows}</tbody></table>`);

    const middle = el('div', { class: 'grid two-col mb' });
    middle.appendChild(kindCard);
    middle.appendChild(usageCard);
    wrap.appendChild(middle);

    // Top-hit rules
    const top = (stats.facts_top_hits || []).filter(f => (f.hit_count || 0) > 0);
    if (top.length) {
        const topBox = el('div', { class: 'card mb' },
            `<h3 class="section-title">Top-hit rules</h3>
             <div class="list">${top.map(f =>
                `<div class="list-row"><span class="badge green">${f.hit_count}</span><span class="grow msg">${esc(f.content)}</span></div>`
             ).join('')}</div>`);
        wrap.appendChild(topBox);
    }

    // Zero-hit rules
    const zeroHit = recent.filter(f => (f.hit_count || 0) === 0 && ['rule', 'decision'].includes(f.kind || 'note'));
    if (zeroHit.length) {
        const zeroBox = el('div', { class: 'card mb' },
            `<h3 class="section-title">Zero-hit rules</h3>
             <div class="list">${zeroHit.slice(0, 10).map(f =>
                `<div class="list-row"><span class="badge muted">0</span><span class="grow msg">${esc(f.content)}</span><span class="ts">${esc(timeAgo(f.ts))}</span></div>`
             ).join('')}</div>`);
        wrap.appendChild(zeroBox);
    }

    // Recent facts
    wrap.appendChild(el('h3', { class: 'section-title' }, 'Recent facts'));
    wrap.appendChild(memoryList(recent));
    return wrap;
}

function kindColor(kind) {
    switch (kind) {
        case 'rule': return 'gold';
        case 'decision': return 'blue';
        case 'note': return 'green';
        case 'log': return 'muted';
        case 'transcript': return 'purple';
        default: return 'muted';
    }
}

function renderResults(container, results) {
    container.innerHTML = '';
    if (!results.length) { container.appendChild(el('div', { class: 'empty' }, 'No matches.')); return; }
    for (const r of results) {
        const tag = r.source_type === 'doc' ? 'DOC' : 'FACT';
        const where = r.source_file ? `${r.source_file} › ${r.section || ''}` : (r.topic || '');
        const node = el('div', { class: 'result' },
            `<div class="r-top"><span class="badge ${tag === 'DOC' ? 'blue' : 'gold'}">${tag}</span>
                <span>score ${r.score}</span><span class="muted">${esc(where)}</span></div>
             <div class="r-body">${esc(r.content)}</div>
             ${r.paths && r.paths.length ? `<div class="r-paths">▤ ${esc(r.paths.join('  ·  '))}</div>` : ''}`);
        container.appendChild(node);
    }
}

function memoryList(facts) {
    const box = el('div', { class: 'card' });
    if (!facts.length) { box.appendChild(el('div', { class: 'empty' }, 'No facts stored yet.')); return box; }
    const list = el('div', { class: 'list' });
    for (const f of facts) {
        const kind = f.kind || 'note';
        const hits = f.hit_count || 0;
        list.appendChild(el('div', { class: 'list-row' },
            `<span class="badge ${f.scope === 'global' ? 'gold' : 'muted'}">${esc(f.scope)}</span>
             <span class="badge ${kindColor(kind)}">${esc(kind)}</span>
             <span class="badge ${hits > 0 ? 'green' : 'muted'}">${hits} hit${hits === 1 ? '' : 's'}</span>
             <span class="grow msg">${esc(f.content)}</span>
             <span class="ts">${esc(timeAgo(f.ts))}</span>`));
    }
    box.appendChild(list);
    return box;
}

function memoryForm(slug) {
    const card = el('div', { class: 'card' },
        `<label class="field"><span class="lab">Fact</span><textarea id="mf-content" placeholder="A durable fact to remember…"></textarea></label>
         <div class="form-row">
            <label class="field"><span class="lab">Topic</span><input type="text" id="mf-topic" value="general"></label>
            <label class="field"><span class="lab">Scope</span><select id="mf-scope">
                <option value="project" ${slug ? '' : 'disabled'} ${slug ? 'selected' : ''}>This project</option>
                <option value="global">Global (all projects)</option>
            </select></label>
         </div>
         <div class="form-actions"><button class="btn primary" id="mf-save">Store fact</button></div>`);
    card.querySelector('#mf-save').addEventListener('click', async () => {
        const content = $('#mf-content').value.trim();
        if (!content) { toast('Enter a fact', 'err'); return; }
        const scope = $('#mf-scope').value;
        try {
            const res = await api('/api/memory/remember', { method: 'POST', body: { content, topic: $('#mf-topic').value, scope: scope === 'global' ? null : scope, project: scope === 'project' ? slug : null } });
            toast(res.duplicate ? 'Already stored' : 'Fact stored', 'ok');
            $('#mf-content').value = '';
        } catch (e) { toast(e.message, 'err'); }
    });
    return card;
}

/* ========================================================= ACTIVITY ===== */
async function viewActivity() {
    $('#topbar-title').textContent = 'Activity';
    await loadProjects();
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Live</div><h1>Activity Feed</h1>
          <div class="sub">Real-time events from every project and agent. <span class="badge green" id="live-badge">live</span></div></div>`));
    const feed = el('div', { class: 'card', id: 'feed' });
    const list = el('div', { class: 'list', id: 'feed-list' });
    feed.appendChild(list);
    v.appendChild(feed);

    const initial = await api('/api/activity?limit=60');
    for (const a of initial.activity || []) list.appendChild(activityRow(a));

    function connectActivity(attempt = 0) {
        const src = new EventSource('/api/stream');
        state.activitySource = src;
        src.onmessage = (ev) => {
            attempt = 0;
            let d; try { d = JSON.parse(ev.data); } catch (_) { return; }
            if (d.type === 'activity') {
                const row = activityRow(d);
                list.prepend(row);
                while (list.children.length > 200) list.lastChild.remove();
            }
            const b = $('#live-badge');
            if (b) { b.className = 'badge green'; b.textContent = 'live'; }
        };
        src.onerror = () => {
            const b = $('#live-badge');
            if (b) { b.className = 'badge red'; b.textContent = 'reconnecting'; }
            if (state.activitySource === src) state.activitySource = null;
            src.close();
            const delay = Math.min(1000 * Math.pow(2, attempt), 30000);
            if (attempt < 10) {
                setTimeout(() => connectActivity(attempt + 1), delay);
            }
        };
    }
    connectActivity();
}

function activityRow(a) {
    return el('div', { class: 'list-row' },
        `<span class="badge muted">${esc(a.kind)}</span>
         <span class="grow msg">${esc(a.message)}</span>
         <span class="ts">${esc(timeAgo(a.ts))}</span>`);
}

/* ============================================================ MCP ======= */
const MCP_STATUS_BADGE = {
    ready: 'green', remote: 'blue', disabled: 'muted', missing: 'amber', invalid: 'red',
};

async function viewMcp() {
    $('#topbar-title').textContent = 'MCP Servers';
    await loadProjects();
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Integrations</div><h1>MCP Servers</h1>
          <div class="sub">Every Model Context Protocol server OpenCode loads — machine-wide and per project.
          Status is read from config; <b>Test</b> starts a server and performs the real MCP handshake
          (<span class="mono">initialize</span> + <span class="mono">tools/list</span>).</div></div>`));
    const wrap = el('div');
    v.appendChild(wrap);

    const render = async () => {
        wrap.innerHTML = '';
        let data;
        try { data = await api('/api/mcp/servers'); }
        catch (e) {
            // A 404 means the running service predates this view's endpoint —
            // static JS is read live from disk, so the page can render while the
            // backend is still the old one. Say so instead of showing "no servers".
            const stale = e.status === 404;
            wrap.appendChild(el('div', { class: 'empty' }, stale
                ? 'Perun\'s API does not know this endpoint yet — the running service was started before this view existed.<br>'
                  + 'Restart Perun (<span class="mono">Perun.cmd</span> or <span class="mono">python -m perun serve</span>) and reload.'
                : esc(e.message || 'failed to load')));
            return;
        }

        const servers = data.servers || [];
        const counts = data.counts || {};
        const bar = el('div', { class: 'card' });
        bar.appendChild(el('div', { class: 'card-head' },
            `<h4>Configured servers</h4><span class="muted">${servers.length} total</span>`));
        const chips = el('div', { class: 'row', style: 'gap:8px;flex-wrap:wrap' });
        const chip = (label, n, cls) => el('span', { class: `badge ${cls}` }, `${esc(label)} · ${n}`);
        chips.appendChild(chip('ready', counts.ready || 0, 'green'));
        chips.appendChild(chip('remote', counts.remote || 0, 'blue'));
        chips.appendChild(chip('missing', counts.missing || 0, 'amber'));
        chips.appendChild(chip('disabled', counts.disabled || 0, 'muted'));
        if (counts.invalid) chips.appendChild(chip('invalid', counts.invalid, 'red'));
        bar.appendChild(chips);
        const files = ((data.config_files || {}).global || []);
        if (files.length) {
            bar.appendChild(el('div', { class: 'muted', style: 'font-size:.78rem;margin-top:10px' },
                `Global config: <span class="mono">${esc(files.join(', '))}</span>`));
        }
        const actions = el('div', { class: 'row', style: 'gap:8px;margin-top:12px' });
        const testAll = el('button', { class: 'btn ghost sm', title: 'Probe every enabled server in turn' }, '⚡ Test all enabled');
        testAll.addEventListener('click', async () => {
            testAll.disabled = true;
            const targets = servers.filter((s) => s.enabled);
            for (let i = 0; i < targets.length; i++) {
                testAll.textContent = `⚡ Testing ${i + 1}/${targets.length}…`;
                const host = document.getElementById(`mcp-host-${mcpId(targets[i])}`);
                if (host) await probeMcp(targets[i], host);
            }
            testAll.disabled = false;
            testAll.textContent = '⚡ Test all enabled';
            toast('MCP probe complete', 'ok');
        });
        actions.appendChild(testAll);
        bar.appendChild(actions);
        wrap.appendChild(bar);

        const perrs = data.parse_errors || [];
        if (perrs.length) {
            wrap.appendChild(el('div', { class: 'card', style: 'border-color:var(--amber,#d9a13b);margin-top:10px' },
                `<div class="card-head"><h4 style="color:var(--amber,#d9a13b)">⚠ Config not read</h4>
                 <span class="muted">${perrs.length} file${perrs.length > 1 ? 's' : ''}</span></div>
                 <div class="mono" style="font-size:.76rem;line-height:1.7;word-break:break-word">`
                + perrs.map((p) => `${esc(p.source)} → ${esc(p.error)}`).join('<br>')
                + '</div>'));
        }

        if (!servers.length) {
            wrap.appendChild(el('div', { class: 'empty' },
                perrs.length
                    ? 'No servers could be loaded because OpenCode\'s config could not be parsed — fix the file(s) above.'
                    : 'No MCP servers are configured in OpenCode.<br>Add one to <span class="mono">~/.config/opencode/opencode.json</span> under <span class="mono">mcp</span>, '
                      + 'or run <span class="mono">python -m perun install --machine</span> to register Perun.'));
            return;
        }

        const globals = servers.filter((s) => s.scope === 'global');
        const byProject = {};
        for (const s of servers.filter((s) => s.scope === 'project')) {
            (byProject[s.project] = byProject[s.project] || []).push(s);
        }

        const section = (title, sub, rows) => {
            if (!rows.length) return;
            wrap.appendChild(el('div', { class: 'view-head', style: 'margin-top:18px' },
                `<div><h4>${esc(title)}</h4><div class="sub">${esc(sub)}</div></div>`));
            for (const s of rows) wrap.appendChild(mcpCard(s));
        };

        section('Global', 'Loaded by every OpenCode project on this machine.', globals);
        for (const [slug, rows] of Object.entries(byProject)) {
            const name = rows[0].project_name || slug;
            section(`Project · ${name}`, slug, rows);
        }
    };

    await render();
}

const mcpId = (s) => `${s.scope}-${s.project || 'global'}-${s.name}`.replace(/[^a-zA-Z0-9_-]/g, '_');

function mcpCard(s) {
    const card = el('div', { class: 'card' });
    const head = el('div', { class: 'card-head' });
    head.appendChild(el('div', {},
        `<div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
            <b class="mono">${esc(s.name)}</b>
            <span class="badge ${MCP_STATUS_BADGE[s.status] || 'muted'}">${esc(s.status)}</span>
            <span class="badge muted">${esc(s.transport)}</span>
            ${s.scope === 'project' ? '<span class="badge blue">project</span>' : ''}
            ${s.enabled ? '' : '<span class="badge muted">disabled</span>'}
         </div>
         <div class="muted mono" style="font-size:.74rem;margin-top:4px">${esc(s.source)}</div>`));
    const test = el('button', { class: 'btn sm', title: 'Launch and handshake this server' }, '⚡ Test');
    head.appendChild(test);
    card.appendChild(head);

    const target = s.transport === 'remote' ? s.url : (s.command || []).join(' ');
    let kv = `<span class="k">Target</span><span class="v mono">${esc(target || '—')}</span>`;
    if (s.transport === 'local') {
        kv += `<span class="k">Resolves</span><span class="v mono">${esc(s.resolved || 'not found on PATH')}</span>`;
    }
    if (s.shadowed_by && s.shadowed_by.length) {
        kv += `<span class="k">Overridden by</span><span class="v">${esc(s.shadowed_by.join(', '))}</span>`;
    }
    card.appendChild(el('div', { class: 'kv' }, kv));

    const host = el('div', { class: 'mcp-probe', id: `mcp-host-${mcpId(s)}` });
    card.appendChild(host);
    test.addEventListener('click', () => probeMcp(s, host, test));
    return card;
}

async function probeMcp(s, host, btn) {
    const label = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '⚡ Testing…'; }
    host.innerHTML = '<div class="muted" style="font-size:.82rem">Launching and handshaking…</div>';
    try {
        const res = await api('/api/mcp/servers/probe', {
            method: 'POST',
            body: { name: s.name, scope: s.scope, project: s.project || '' },
        });
        renderMcpProbe(host, res);
    } catch (e) {
        host.innerHTML = `<div class="row" style="gap:8px;align-items:center">
            <span class="badge red">error</span>
            <span class="muted">${esc(e.message)}</span></div>`;
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = label; }
    }
}

function renderMcpProbe(host, res) {
    const ok = res.ok;
    const cls = { ok: 'green', busy: 'amber', unreachable: 'amber', missing: 'amber', disabled: 'muted', invalid: 'red', error: 'red' }[res.status] || (ok ? 'green' : 'red');
    const info = res.server_info && (res.server_info.name || res.server_info.version)
        ? `${esc(res.server_info.name || 'server')} ${esc(res.server_info.version || '')}`.trim() : '';
    let out = `<div class="row" style="gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px">
        <span class="badge ${cls}">${esc(res.status || (ok ? 'ok' : 'error'))}</span>
        <span class="muted" style="font-size:.8rem">${res.latency_ms != null ? `${res.latency_ms} ms` : ''}${info ? ` · ${info}` : ''}</span>
        ${ok ? `<span class="badge blue">${Number(res.tool_count || (res.tools || []).length)} tools</span>` : ''}</div>`;
    if (res.error) out += `<div class="muted" style="font-size:.82rem;margin-top:6px">${esc(res.error)}</div>`;
    if (ok && (res.tools || []).length) {
        out += `<details style="margin-top:8px"><summary class="muted" style="cursor:pointer;font-size:.82rem">Tools (${res.tools.length})</summary>
        <div class="mono" style="font-size:.78rem;margin-top:6px;line-height:1.7;word-break:break-word">${esc(res.tools.join(' · '))}</div></details>`;
    }
    if (res.stderr) out += `<pre class="mono" style="font-size:.72rem;margin-top:8px;white-space:pre-wrap;color:var(--text-muted)">${esc(res.stderr)}</pre>`;
    host.innerHTML = out;
}

/* ========================================================= TELEGRAM ===== */
async function viewTelegram() {
    $('#topbar-title').textContent = 'Telegram';
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Bridge</div><h1>Telegram Bridge</h1>
          <div class="sub">One shared Perun bot for <b>every</b> project — message it from your phone and steer any project's agent.</div></div>`));
    const host = el('div');
    v.appendChild(host);

    const reload = async () => {
        let status;
        try { status = await api('/api/telegram/status'); } catch (e) { status = { ok: false }; }
        mountTelegram(host, status, reload);
    };
    await reload();
}

function mountTelegram(host, status, reload) {
    host.innerHTML = '';
    const bot = status.bot || {};
    const task = status.bridge_task || {};
    const running = status.bridge_running;
    const runBadge = running === true
        ? '<span class="badge green">bridge running</span>'
        : (running === false ? '<span class="badge amber">bridge stopped</span>' : '');
    const botLabel = status.bot_name || bot.first_name || (bot.username ? '@' + bot.username : '') || 'Perun bot';

    host.appendChild(el('div', { class: 'card mb' },
        `<div class="row wrap" style="gap:16px">
            <span class="badge ${status.configured ? 'green' : 'red'}">${status.configured ? 'configured' : 'not configured'}</span>
            ${runBadge}
            ${task.exists ? '<span class="badge muted">logon task registered</span>' : ''}
            <span class="secondary">${esc(botLabel)}</span>
            <span class="secondary">${bot.username ? '@' + esc(bot.username) : 'no bot'}</span>
            <span class="secondary">chats: ${(status.allowed_chats || []).map(esc).join(', ') || 'none — pairing required'}</span>
         </div>
         ${(status.allowed_chats && status.allowed_chats.length) ? '' : `<p class="mt">🔑 <b>Pairing code:</b> <span class="mono">${esc(status.pair_code || '(not generated yet — restart the bridge)')}</span> — send <span class="mono">/pair &lt;code&gt;</span> from the Telegram chat you want to authorise. The code is one-time and is cleared once used.</p>`}
         <p class="muted mt">The bot is project-agnostic. Use <span class="mono">/projects</span> to list them, <span class="mono">/project &lt;slug&gt;</span> to pick one, then just chat — Perun routes each message to the selected project's agent session and remembers the routing per chat.</p>`));

    // ---- Bot settings (single source of truth for the Telegram bot) --------
    const srcNote = status.token_source === 'env'
        ? `<p class="muted mt">⚠ The token currently in use comes from the
             <span class="mono">TELEGRAM_BOT_TOKEN</span> environment variable, not from Perun.
             Saving a token here stores it in <span class="mono">config.json</span>, which takes
             precedence over the environment.</p>`
        : (status.token_source === 'settings-encrypted'
            ? `<p class="muted mt">🔒 The token is encrypted in <span class="mono">config.json</span>.
                 ${status.configured
                     ? 'The bridge is using the decrypted token.'
                     : '<button class="btn primary small" id="tg-unlock">Unlock token</button> <span class="muted">(or set <span class="mono">PERUN_PASSWORD</span>)</span>'}
                 If you change the password, paste the token again below to re-encrypt it.</p>`
            : '');
    const tokenPlaceholder = status.token_source === 'settings-encrypted'
        ? '••••••••  (encrypted — paste token + password to re-encrypt, blank to keep)'
        : (status.configured ? '••••••••  (hidden — leave blank to keep the current token)' : '123456789:ABC-DEF…');
    const sCard = el('div', { class: 'card mb' },
        `<div class="card-head"><h4>Bot settings</h4><span class="badge gold">machine-wide</span></div>
         <p class="secondary">Switch Perun to a <b>different</b> bot, or edit the one in use. Ask
            <span class="mono">@BotFather</span> for a token, paste it below, and it is verified before anything is saved.</p>
         ${srcNote}
         <label class="field"><span class="lab">Name <span class="muted">— a label to tell bots apart</span></span>
            <input type="text" id="tg-name" value="${esc(status.bot_name || '')}" placeholder="${esc(bot.first_name || 'Perun bot')}"></label>
         <label class="field"><span class="lab">API key (bot token) ${status.configured ? '<span class="badge green">set</span>' : ''}</span>
            <input type="password" id="tg-token" autocomplete="off" placeholder="${tokenPlaceholder}"></label>
         <label class="field"><span class="lab">Encryption password <span class="muted">— optional; if set, the token is encrypted in config.json. Set PERUN_PASSWORD at launch so the bridge can decrypt it.</span></span>
            <input type="password" id="tg-password" autocomplete="off"
                   placeholder="${status.token_source === 'settings-encrypted' ? 'paste token again to re-encrypt' : 'leave blank to store plain text'}"></label>
         <p class="muted mt">🔐 If you lose this password, you cannot recover the stored token. You must get a new API key from
            <span class="mono">@BotFather</span> and save it here again.</p>
         <label class="field"><span class="lab">Allowed chat ids <span class="muted">(comma separated${status.chats_source === 'env' ? ' — currently from the environment' : ''})</span></span>
            <input type="text" id="tg-chats" value="${esc((status.allowed_chats || []).join(', '))}"
                   placeholder="blank = no chat has access until it pairs with the code above"></label>
         <div class="form-actions">
            <button class="btn primary" id="tg-save">Save bot settings</button>
            ${status.token_source === 'settings-encrypted'
                ? `<button class="btn" id="tg-change-pw">Change password</button>`
                : ''}
            ${status.configured ? `<button class="btn danger ghost" id="tg-disconnect"${status.token_source === 'env' ? ' disabled title="The active token comes from the environment"' : ''}>Disconnect</button>` : ''}
         </div>`);
    host.appendChild(sCard);

    sCard.querySelector('#tg-save').addEventListener('click', async () => {
        const payload = { name: $('#tg-name').value.trim(), chats: $('#tg-chats').value };
        const tok = $('#tg-token').value.trim();
        const pwd = $('#tg-password').value.trim();
        if (tok) {
            payload.token = tok;
            if (pwd) payload.password = pwd;
        }
        const btn = sCard.querySelector('#tg-save');
        btn.disabled = true;
        try {
            const r = await api('/api/telegram/bot', { method: 'POST', body: payload });
            const uname = r.bot && r.bot.username ? '@' + r.bot.username : 'the bot';
            toast(`Saved — connected to ${uname}`, 'ok');
            if (r.restart_required && task.exists) toast('Restart the bridge to switch the running poller to this bot', 'ok');
            await reload();
        } catch (e) { toast(e.message, 'err'); btn.disabled = false; }
    });

    const unlockBtn = sCard.querySelector('#tg-unlock');
    if (unlockBtn) unlockBtn.addEventListener('click', () => promptForUnlockPassword(true));

    const pwBtn = sCard.querySelector('#tg-change-pw');
    if (pwBtn) pwBtn.addEventListener('click', () => {
        const body = el('div', {}, `
            <p class="secondary">Enter the current password to decrypt the stored token, then a new password to re-encrypt it. The token is never sent back to the browser.</p>
            <p class="muted">If you forgot the current password, disconnect the bot below and reconnect with a new API key from @BotFather.</p>
            <label class="field"><span class="lab">Current password</span><input type="password" id="pw-old"></label>
            <label class="field"><span class="lab">New password</span><input type="password" id="pw-new"></label>`);
        modal('Change encryption password', body, [
            { label: 'Cancel', class: 'ghost', onClick: closeModal },
            { label: 'Re-encrypt', class: 'primary', onClick: async () => {
                const oldPw = body.querySelector('#pw-old').value;
                const newPw = body.querySelector('#pw-new').value;
                if (!oldPw || !newPw) { toast('Both passwords are required', 'err'); return; }
                closeModal();
                pwBtn.disabled = true;
                try {
                    const r = await api('/api/telegram/bot/change-password', {
                        method: 'POST',
                        body: { old_password: oldPw, new_password: newPw },
                    });
                    toast(r.changed ? 'Password changed — click Restart bridge to apply it' : 'No change needed', r.changed ? 'ok' : 'ok');
                    await reload();
                } catch (e) { toast(e.message, 'err'); pwBtn.disabled = false; }
            }},
        ]);
    });

    const dbtn = sCard.querySelector('#tg-disconnect');
    if (dbtn) dbtn.addEventListener('click', () => confirmDialog('Disconnect the bot',
        'Clear the stored Telegram bot token? The bridge stops polling until a new token is saved.',
        async () => {
            try { await api('/api/telegram/bot', { method: 'POST', body: { token: '' } }); toast('Bot disconnected', 'ok'); await reload(); }
            catch (e) { toast(e.message, 'err'); }
        }, 'Disconnect'));

    // ---- Bridge process -----------------------------------------------------
    const inProcess = status.bridge_running;
    host.appendChild(el('div', { class: 'card mb' },
        `<div class="card-head"><h4>Bridge process</h4></div>
         <p class="secondary">The bridge resolves the bot token <b>once, at startup</b>. A restart makes it
            re-read the configured token/password.</p>
         ${inProcess
            ? `<p class="muted mt">🔌 The bridge is running <b>inside this dashboard process</b>.
                 <strong>Restart bridge</strong> will reload the token without closing the window.</p>`
            : `<p class="muted mt">The bridge is not currently running. ${task.exists
                ? 'You can restart the standalone <span class="mono">PerunBridge</span> logon task below.'
                : 'The bridge normally starts automatically with the dashboard. You can also start it by hand: <span class="mono">python -m perun bridge</span>. Use <span class="mono">python -m perun install --machine --task</span> to register an auto-start logon task.'}</p>`}
         <div class="form-actions">
            <button class="btn" id="tg-restart" ${(inProcess || task.exists) ? '' : 'disabled'}>⟳ Restart bridge</button>
         </div>
         ${task.exists
            ? `<p class="muted mt">Logon task command: <span class="mono">${esc(task.command || 'python')} ${esc(task.arguments || '-m perun bridge')}</span></p>`
            : ''}
         <p class="muted mt">Commands: <span class="mono">/projects</span>, <span class="mono">/project</span>,
            <span class="mono">/recall</span>, <span class="mono">/orchestrator</span>.</p>`));

    const rbtn = host.querySelector('#tg-restart');
    if (rbtn && (inProcess || task.exists)) rbtn.addEventListener('click', async () => {
        rbtn.disabled = true;
        try {
            const r = await api('/api/telegram/bridge/restart', { method: 'POST' });
            const ok = r.restart && (r.restart.reloaded || r.restart.ok);
            toast(ok ? 'Bridge restarted' : ((r.restart && r.restart.reason) || 'Restart failed'), ok ? 'ok' : 'err');
            setTimeout(reload, 1500);
        } catch (e) { toast(e.message, 'err'); rbtn.disabled = false; }
    });

    // ---- Send a test message -----------------------------------------------
    host.appendChild(el('div', { class: 'card' },
        `<label class="field"><span class="lab">Chat id <span class="muted">(optional — defaults to the first allowed chat)</span></span>
            <input type="text" id="tg-send-chat" placeholder="leave blank to use the configured chat"></label>
         <label class="field"><span class="lab">Message</span><textarea id="tg-msg" placeholder="Send a Markdown message via the Perun bot…"></textarea></label>
         <div class="form-actions"><button class="btn primary" id="tg-send">✈ Send via bot</button></div>`));
    $('#tg-send').addEventListener('click', async () => {
        const text = $('#tg-msg').value.trim();
        if (!text) { toast('Enter a message', 'err'); return; }
        try {
            const r = await api('/api/telegram/send', { method: 'POST', body: { text, chat_id: $('#tg-send-chat').value } });
            toast(r.sent ? 'Sent ✈' : 'Send failed', r.sent ? 'ok' : 'err');
        } catch (e) { toast(e.message, 'err'); }
    });
}

/* ========================================================= PERUNCHAT ==== */
async function viewPerunChat() {
    $('#topbar-title').textContent = 'PerunChat';
    const v = $('#view');
    v.innerHTML = '<div class="empty"><span class="spinner"></span> loading PerunChat…</div>';

    let status = {};
    let settings = state.settings || {};
    try {
        await loadSettings();
        settings = state.settings || {};
        status = (await api('/api/perunchat/status')) || {};
    } catch (e) {
        status = { configured: false };
    }
    const myAgentId = status.agent_id || '';
    // Populate the shared model/agent lists used by the dropdown pickers.
    try { await loadModels(); await loadAgents(); } catch (e) { /* best-effort */ }

    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Federation</div><h1>PerunChat</h1>
         <div class="sub">Manage your agent's social graph and chat through the PerunChat gateway.</div></div>`));

    // ---- Configuration -----------------------------------------------------
    const cfgCard = el('div', { class: 'card' },
        `<div class="card-head"><h4>Gateway credentials</h4><span class="badge gold">machine-wide</span></div>
         <p class="secondary">Register for a PerunChat API key on the <a href="https://peruninthemachine.com/perunchat/" target="_blank" rel="noopener">company website</a>. The local overseer stores the key and proxies social-management calls to the chat gateway; the WebSocket chat connects directly from this dashboard.</p>
         <label class="field"><span class="lab">API key <span id="pc-key-badge" class="badge"></span></span>
             <input type="password" id="pc-key" placeholder="prn_live_..." value=""></label>
         <label class="field"><span class="lab">Encryption password <span class="muted">— optional; if set, the key is encrypted in config.json. Set PERUN_PASSWORD at launch so the bridge can decrypt it.</span></span>
             <input type="password" id="pc-password" autocomplete="off" placeholder="leave blank to store plain text"></label>
         <label class="field"><span class="lab">WebSocket endpoint</span>
             <input type="text" id="pc-endpoint" value="${esc(settings.perunchat_api_endpoint || 'wss://peruninthemachine.com/v1/chat/ws')}"></label>
         <label class="field"><span class="lab">Autonomous mode</span>
             <label class="row"><input type="checkbox" id="pc-auto-reply" ${settings.perunchat_auto_reply ? 'checked' : ''}> <span>Let the Orchestrator reply to incoming chat messages</span></label></label>
         <label class="field"><span class="lab">Event ingestion</span>
             <label class="row"><input type="checkbox" id="pc-ingest-events" ${settings.perunchat_ingest_events !== false ? 'checked' : ''}> <span>Receive room messages for coroutine triggers even when replies are off</span></label></label>
         <label class="field"><span class="lab">Auto-join rooms <span class="muted">(joined by the Orchestrator on startup)</span></span>
             <span id="pc-autojoin-host"></span></label>
         <div class="form-row">
             <label class="field" style="flex:1"><span class="lab">Custom room id <span class="muted">(not in the catalog yet)</span></span>
                 <input type="text" id="pc-custom-room" placeholder="pub:tenant:channel"></label>
             <div style="align-self:flex-end"><button class="btn ghost" id="pc-add-room">Add room</button></div>
         </div>
         <div class="form-row">
             <label class="field" style="flex:1"><span class="lab">Chat model <span class="muted">(optional override)</span></span>
                 <span id="pc-chat-model-host"></span></label>
             <label class="field" style="flex:1"><span class="lab">Chat agent <span class="muted">(defaults to the perunchat agent)</span></span>
                 <span id="pc-chat-agent-host"></span></label>
         </div>
         <div class="form-actions">
             <button class="btn primary" id="pc-save">Save credentials</button>
             <button class="btn ghost" id="pc-test">Test connection</button>
         </div>
         <div id="pc-status" class="secondary mt"></div>`);
    v.appendChild(cfgCard);

    // Same dropdown widgets as the Settings page; both stay free-typed too.
    const chatModelSel = modelSelect({ value: settings.perunchat_chat_model || '', placeholder: 'orchestrator / default model' });
    $('#pc-chat-model-host').appendChild(chatModelSel);
    const chatAgentSel = agentSelect({ value: settings.perunchat_chat_agent || 'perunchat', placeholder: 'perunchat (chat persona)' });
    $('#pc-chat-agent-host').appendChild(chatAgentSel);

    // Auto-join picker: options come from the gateway's discoverable rooms.
    const autoJoinRooms = settings.perunchat_auto_join || [];
    const joinSel = multiSelect({
        id: 'pc-auto-join-sel',
        items: autoJoinRooms.map((r) => ({ value: r, label: r })),
        value: autoJoinRooms,
        placeholder: 'Select rooms…',
    });
    $('#pc-autojoin-host').appendChild(joinSel);

    let roomCatalog = [];
    // Shared-project rooms have machine ids (``grp:perun:<slug>:chat``). Label
    // them with the human project name so a chat can be identified at a glance
    // instead of pattern-matching hashes and slugs.
    function roomDisplayLabel(rid) {
        const m = /^grp:perun:([^:]+):chat$/.exec(rid || '');
        if (!m) return '';
        const proj = (state.projects || []).find((p) => p.slug === m[1]);
        return `${proj ? proj.name : m[1]} (project chat)`;
    }
    async function refreshRooms() {
        if (!status.configured) return;
        try {
            const r = await api('/api/perunchat/rooms');
            roomCatalog = (r.rooms || []).map((x) => ({
                value: x.room_id,
                label: roomDisplayLabel(x.room_id) || x.room_id,
                description: [
                    x.active ? 'live' : '',
                    x.joined ? 'joined' : '',
                    x.online_count != null ? `${x.online_count} online` : '',
                    x.members ? `${x.members} member${x.members === 1 ? '' : 's'}` : '',
                ].filter(Boolean).join(' · ') || undefined,
            }));
            // Keep rooms that are configured but not in the gateway catalog.
            const known = new Set(roomCatalog.map((x) => x.value));
            for (const rid of autoJoinRooms) {
                if (!known.has(rid)) {
                    roomCatalog.push({
                        value: rid,
                        label: roomDisplayLabel(rid) || rid,
                        description: 'configured',
                    });
                }
            }
            joinSel.setItems(roomCatalog);
            refreshRoomSelect();
        } catch (e) { /* room catalog is optional */ }
    }

    function isValidRoomId(rid) {
        return /^(priv|private|grp|pub):[a-zA-Z0-9._:-]+$/.test(rid);
    }

    $('#pc-add-room').addEventListener('click', () => {
        const rid = $('#pc-custom-room').value.trim();
        if (!rid) return;
        if (!isValidRoomId(rid)) {
            toast('Invalid room id. Must start with priv:/grp:/pub: and contain only letters, numbers, dots, underscores, colons and hyphens.', 'err');
            return;
        }
        if (!roomCatalog.some((x) => x.value === rid)) {
            roomCatalog.push({ value: rid, label: rid, description: 'custom' });
            joinSel.setItems(roomCatalog);
        }
        if (!joinSel.getValue().includes(rid)) joinSel.setValue([...joinSel.getValue(), rid]);
        $('#pc-custom-room').value = '';
    });

    const keyBadge = $('#pc-key-badge');
    if (settings.perunchat_api_key_set) {
        const agentLabel = status.agent_id ? `connected as ${status.agent_id}` : 'set';
        keyBadge.textContent = agentLabel;
        keyBadge.className = 'badge green';
        $('#pc-key').placeholder = 'leave blank to keep the stored key';
    } else {
        keyBadge.textContent = 'not set';
        keyBadge.className = 'badge';
    }

    $('#pc-save').addEventListener('click', async () => {
        const key = $('#pc-key').value.trim();
        const password = $('#pc-password').value.trim();
        const endpoint = $('#pc-endpoint').value.trim();
        const autoReply = $('#pc-auto-reply').checked;
        const ingestEvents = $('#pc-ingest-events').checked;
        const autoJoin = joinSel.getValue();
        const chatModel = chatModelSel.getValue();
        const chatAgent = chatAgentSel.getValue();
        const body = {
            perunchat_api_endpoint: endpoint,
            perunchat_auto_reply: autoReply,
            perunchat_ingest_events: ingestEvents,
            perunchat_auto_join: autoJoin,
            perunchat_chat_model: chatModel,
            perunchat_chat_agent: chatAgent,
        };
        if (key) body.perunchat_api_key = key;
        if (password) body.perunchat_password = password;
        try {
            await api('/api/settings', { method: 'PATCH', body });
            toast('PerunChat credentials saved', 'ok');
            // Tear down any live chat socket and re-render the whole view so
            // the saved key, status, room catalog and social lists take effect
            // immediately — no manual reload needed.
            wsExplicitClose = true;
            clearTimeout(wsReconnectTimer);
            wsReconnectTimer = null;
            if (ws) { try { ws.close(); } catch (err) { /* ignore */ } ws = null; }
            await viewPerunChat();
        } catch (e) { toast(e.message, 'err'); }
    });

    $('#pc-test').addEventListener('click', async () => {
        try {
            const r = await api('/api/perunchat/status');
            const bridge = (await api('/api/perunchat/bridge/status')) || {};
            $('#pc-status').textContent = r.configured
                ? `Gateway reachable: ${r.rest_base || r.endpoint}; bridge: ${bridge.running ? 'running as ' + (bridge.agent_id || '—') : 'stopped'}`
                : 'API key not configured';
        } catch (e) { $('#pc-status').textContent = 'Gateway unreachable: ' + e.message; }
    });

    // The social grid and chat panel are always visible so it is obvious what
    // exists here; without an API key they show a hint instead of live data.
    const pcConfigured = !!status.configured;
    const pcPlaceholder = '<p class="muted">Add your PerunChat API key above to load this list.</p>';
    if (!pcConfigured) {
        $('#pc-status').textContent = 'API key not configured — add your key above to enable friends, blocks and chat.';
    } else {
        $('#pc-status').textContent = `Gateway: ${status.rest_base || status.endpoint}`;
        refreshRooms();
    }

    // ---- Private Rooms management -------------------------------------------
    const privateRoomsCard = el('div', { class: 'card mt' },
        `<div class="card-head"><h4>Private Rooms</h4><span class="badge">manage</span></div>
         <p class="secondary" style="font-size:12px; margin:-4px 0 10px 0;">Create private rooms (bilateral or multi-party), invite/remove members.</p>
         <div class="form-row">
             <label class="field" style="flex:1"><span class="lab">Members (comma separated, include yourself optional)</span>
                 <input type="text" id="pc-priv-members" placeholder="agent-a, agent-b"></label>
         </div>
         <div class="form-row">
             <label class="field" style="flex:1"><span class="lab">Custom room id <span class="muted">(optional, must start with priv:)</span></span>
                 <input type="text" id="pc-priv-room-id" placeholder="leave empty for auto"></label>
             <div style="align-self:flex-end"><button class="btn primary" id="pc-priv-create">Create / Join</button></div>
         </div>
         <div class="row" style="gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px">
             <label class="field" style="flex:1; min-width:200px"><span class="lab">Your private rooms</span>
                 <select id="pc-priv-select"><option value="">— select —</option></select></label>
             <button class="btn ghost" id="pc-priv-refresh">Refresh</button>
             <button class="btn ghost" id="pc-priv-join-chat">Join in chat</button>
         </div>
         <div id="pc-priv-members-box" style="margin-top:10px"></div>
         <div class="form-row" style="margin-top:10px">
             <label class="field" style="flex:1"><span class="lab">Invite agent</span>
                 <input type="text" id="pc-priv-invite" placeholder="agent_id"></label>
             <div style="align-self:flex-end"><button class="btn success" id="pc-priv-invite-btn">Invite</button></div>
         </div>`);
    v.appendChild(privateRoomsCard);

    async function refreshPrivateRooms() {
        if (!pcConfigured) {
            const sel = document.getElementById('pc-priv-select');
            if (sel) sel.innerHTML = '<option value="">— select —</option>';
            const box = document.getElementById('pc-priv-members-box');
            if (box) box.innerHTML = '<p class="muted">Add API key to manage private rooms.</p>';
            return;
        }
        try {
            const res = await api('/api/perunchat/rooms/private');
            const rooms = res.rooms || [];
            const sel = document.getElementById('pc-priv-select');
            if (sel) {
                sel.innerHTML = '<option value="">— select —</option>';
                for (const r of rooms) {
                    const opt = el('option', { value: r }, esc(r));
                    sel.appendChild(opt);
                }
            }
            const box = document.getElementById('pc-priv-members-box');
            if (box) box.innerHTML = rooms.length ? '<p class="muted">Select a room to view members.</p>' : '<p class="muted">No private rooms yet.</p>';
        } catch (e) {
            // Don't spam error toasts for optional room management.
            const box = document.getElementById('pc-priv-members-box');
            if (box) box.innerHTML = `<p class="muted">Private rooms unavailable (${esc(e.message)}).</p>`;
        }
    }

    document.getElementById('pc-priv-refresh').addEventListener('click', refreshPrivateRooms);

    document.getElementById('pc-priv-create').addEventListener('click', async () => {
        const membersStr = document.getElementById('pc-priv-members').value;
        const roomId = document.getElementById('pc-priv-room-id').value.trim();
        if (roomId && !isValidRoomId(roomId)) {
            toast('Invalid custom room id. Must start with priv: and contain only letters, numbers, dots, underscores, colons and hyphens.', 'err');
            return;
        }
        const members = membersStr.split(',').map(s => s.trim()).filter(Boolean);
        try {
            const res = await api('/api/perunchat/rooms/private', { method: 'POST', body: { members, room_id: roomId || null } });
            toast('Private room created/joined', 'ok');
            document.getElementById('pc-priv-members').value = '';
            document.getElementById('pc-priv-room-id').value = '';
            await refreshPrivateRooms();
            if (res.room_id) {
                const sel = document.getElementById('pc-priv-select');
                if (sel) sel.value = res.room_id;
                await loadPrivateRoomMembers(res.room_id);
            }
            await refreshRooms();
        } catch (e) {
            toast(e.message, 'err');
        }
    });

    async function loadPrivateRoomMembers(roomId) {
        const box = document.getElementById('pc-priv-members-box');
        if (!box || !roomId) return;
        box.innerHTML = '<p class="muted">Loading members…</p>';
        try {
            const res = await api(`/api/perunchat/rooms/${encodeURIComponent(roomId)}/members`);
            const members = res.members || [];
            let html = `<div class="row" style="justify-content:space-between; align-items:center"><span class="lab">Members</span><span class="muted">${esc(roomId)}</span></div>`;
            if (!members.length) {
                html += '<p class="muted">No members.</p>';
            } else {
                for (const m of members) {
                    html += `<div class="row" style="justify-content:space-between; padding:4px 0; border-bottom:1px solid var(--border)"><span>${esc(m)}</span><button class="btn ghost danger small" data-remove-mem="${esc(m)}">Remove</button></div>`;
                }
            }
            box.innerHTML = html;
            box.querySelectorAll('[data-remove-mem]').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const mid = btn.getAttribute('data-remove-mem');
                    try {
                        await api(`/api/perunchat/rooms/${encodeURIComponent(roomId)}/members/${encodeURIComponent(mid)}`, { method: 'DELETE' });
                        toast('Member removed', 'ok');
                        await loadPrivateRoomMembers(roomId);
                        await refreshRooms();
                    } catch (e) {
                        toast(e.message, 'err');
                    }
                });
            });
        } catch (e) {
            box.innerHTML = `<p class="muted">Failed to load members: ${esc(e.message)}</p>`;
        }
    }

    document.getElementById('pc-priv-select').addEventListener('change', (e) => {
        loadPrivateRoomMembers(e.target.value);
    });

    document.getElementById('pc-priv-invite-btn').addEventListener('click', async () => {
        const roomId = document.getElementById('pc-priv-select').value;
        const agentId = document.getElementById('pc-priv-invite').value.trim();
        if (!roomId) { toast('Select a private room first', 'err'); return; }
        if (!agentId) { toast('Enter agent_id to invite', 'err'); return; }
        try {
            await api(`/api/perunchat/rooms/${encodeURIComponent(roomId)}/members`, { method: 'POST', body: { agent_id: agentId } });
            toast('Member invited', 'ok');
            document.getElementById('pc-priv-invite').value = '';
            await loadPrivateRoomMembers(roomId);
            await refreshRooms();
        } catch (e) {
            toast(e.message, 'err');
        }
    });

    document.getElementById('pc-priv-join-chat').addEventListener('click', () => {
        const roomId = document.getElementById('pc-priv-select').value;
        if (!roomId) { toast('Select a private room first', 'err'); return; }
        const typeSel = document.getElementById('pc-chat-type');
        if (typeSel) typeSel.value = 'PRIVATE';
        refreshRoomSelect();
        setCurrentRoomId(roomId);
        joinCurrentRoom();
        toast('Joined private room in chat', 'ok');
    });

    refreshPrivateRooms();

    // ---- Social grid -------------------------------------------------------
    const grid = el('div', { class: 'grid', style: 'grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px;' });

    const friendsCard = el('div', { class: 'card' },
        `<div class="card-head"><h4>Friends</h4></div>
         <div id="pc-friends-list" class="perunchat-list-box"><p class="muted">Loading…</p></div>`);
    grid.appendChild(friendsCard);

    const requestsCard = el('div', { class: 'card' },
        `<div class="card-head"><h4>Requests</h4></div>
         <div id="pc-incoming" class="perunchat-list-box"><p class="muted">Loading incoming…</p></div>
         <hr style="border-color:var(--border); margin:12px 0">
         <div id="pc-outgoing" class="perunchat-list-box"><p class="muted">Loading outgoing…</p></div>
         <label class="field"><span class="lab">Send friend request</span>
             <div class="form-row"><input type="text" id="pc-request-to" placeholder="agent_id"><button class="btn primary" id="pc-request-btn">Request</button></div></label>`);
    grid.appendChild(requestsCard);

    const blockedCard = el('div', { class: 'card' },
        `<div class="card-head"><h4>Blocked</h4></div>
         <div id="pc-blocked-list" class="perunchat-list-box"><p class="muted">Loading…</p></div>
         <label class="field"><span class="lab">Block agent</span>
             <div class="form-row"><input type="text" id="pc-block-id" placeholder="agent_id"><button class="btn danger" id="pc-block-btn">Block</button></div></label>`);
    grid.appendChild(blockedCard);

    const trustedCard = el('div', { class: 'card' },
        `<div class="card-head"><h4>Trusted</h4></div>
         <p class="secondary" style="font-size:12px; margin:-4px 0 10px 0;">Internal context is shared only with these agents, and only in private/group rooms. Public rooms never receive internal context.</p>
         <div id="pc-trusted-list" class="perunchat-list-box"><p class="muted">Loading…</p></div>
         <label class="field"><span class="lab">Trust agent</span>
             <div class="form-row"><input type="text" id="pc-trusted-id" placeholder="agent_id"><button class="btn success" id="pc-trusted-btn">Trust</button></div></label>`);
    grid.appendChild(trustedCard);

    v.appendChild(grid);

    // ---- Chat panel --------------------------------------------------------
    const chatCard = el('div', { class: 'card mt' },
        `<div class="card-head"><h4>Chat</h4><span id="pc-ws-status" class="badge">disconnected</span></div>
         <div id="pc-needs-you" class="perunchat-needs-you" style="display:none"></div>
         <div id="pc-chat-log" class="perunchat-chat-log"></div>
         <div id="pc-reply-bar" class="perunchat-reply-bar"></div>
         <div class="form-row" style="align-items:flex-end">
             <label class="field" style="flex:1"><span class="lab">Room <span class="muted">(type filters the list)</span></span>
                 <select id="pc-chat-room-select"><option value="">— choose a room —</option></select>
                 <input type="text" id="pc-chat-room-custom" placeholder="custom room id" style="display:none; margin-top:6px;"></label>
             <label class="field" style="width:130px"><span class="lab">Type <span class="muted">(auto)</span></span>
                 <select id="pc-chat-type"><option value="PUBLIC">PUBLIC</option><option value="GROUP">GROUP</option><option value="PRIVATE">PRIVATE</option></select></label>
             <button class="btn ghost" id="pc-chat-join">Join</button>
             <button class="btn ghost" id="pc-chat-leave">Leave</button>
         </div>
         <div id="pc-joined-status" class="secondary" style="font-size:12px; margin:-8px 0 8px 0;">Not joined to any room.</div>
         <div class="form-row">
             <input type="text" id="pc-chat-msg" placeholder="Message (emoji supported 👋)…" style="flex:1">
             <button class="btn primary" id="pc-chat-send">Send</button>
         </div>`);
    v.appendChild(chatCard);

    // Operator asks are durable inbox items now; surface the count in the chat
    // itself so an "add these checks" request cannot scroll away unnoticed.
    // `chatNeedsTimer` is module-level (next to attentionTimer) so a re-render
    // cannot leave the previous interval polling forever.
    const loadChatNeedsYou = async () => {
        const host = $('#pc-needs-you');
        if (!host) {
            if (chatNeedsTimer) { clearInterval(chatNeedsTimer); chatNeedsTimer = null; }
            return;
        }
        let res;
        try {
            res = await api('/api/attention?state=open&limit=50');
        } catch (_) {
            return;
        }
        const items = (res.items || []).filter((it) => it.target_kind === 'room');
        // Message time, not row-insertion order: backfilled items are written
        // later than the message they quote.
        items.sort((a, b) => {
            const at = Number((a.ref || {}).message_ts || 0)
                || Date.parse(a.created_at || '') || 0;
            const bt = Number((b.ref || {}).message_ts || 0)
                || Date.parse(b.created_at || '') || 0;
            return bt - at;
        });
        if (!items.length) {
            host.style.display = 'none';
            host.innerHTML = '';
            return;
        }
        const latest = items[0];
        host.style.display = 'block';
        host.innerHTML = `<div class="row" style="gap:8px;align-items:baseline">
            <span class="badge amber">${items.length}</span>
            <b>operator ask${items.length === 1 ? '' : 's'} for you</b>
            <span class="grow muted truncate">${esc(String(latest.question || '').slice(0, 140))}</span>
            <button class="btn sm primary" id="pc-needs-open">Open inbox</button>
        </div>`;
        const open = host.querySelector('#pc-needs-open');
        if (open) open.addEventListener('click', () => goto('#/attention'));
    };
    if (chatNeedsTimer) { clearInterval(chatNeedsTimer); chatNeedsTimer = null; }
    loadChatNeedsYou();
    chatNeedsTimer = setInterval(loadChatNeedsYou, 30000);

    function inferRoomType(roomId) {
        if (!roomId) return 'PUBLIC';
        const p = roomId.split(':')[0];
        if (p === 'priv') return 'PRIVATE';
        if (p === 'grp') return 'GROUP';
        return 'PUBLIC';
    }

    function currentRoomId() {
        const sel = $('#pc-chat-room-select');
        if (sel.value === '__custom__') {
            return $('#pc-chat-room-custom').value.trim();
        }
        return sel.value;
    }

    function setCurrentRoomId(roomId) {
        const sel = $('#pc-chat-room-select');
        const custom = $('#pc-chat-room-custom');
        if (!sel || !custom) return;
        if (roomId && Array.from(sel.options).some((o) => o.value === roomId)) {
            sel.value = roomId;
            custom.style.display = 'none';
        } else if (roomId) {
            sel.value = '__custom__';
            custom.value = roomId;
            custom.style.display = 'block';
        } else {
            sel.value = '';
            custom.value = '';
            custom.style.display = 'none';
        }
    }

    function refreshRoomSelect() {
        const sel = $('#pc-chat-room-select');
        if (!sel) return;
        const type = $('#pc-chat-type').value;
        const current = currentRoomId();
        let html = '<option value="">— choose a room —</option>';
        const filtered = roomCatalog.filter((item) => inferRoomType(item.value) === type);
        if (filtered.length) {
            html += `<optgroup label="${type} rooms">`;
            for (const item of filtered) {
                const active = item.description ? ` (${esc(item.description)})` : '';
                const suffix = item.label && item.label !== item.value ? ` — ${esc(item.value)}` : '';
                html += `<option value="${esc(item.value)}">${esc(item.label)}${suffix}${active}</option>`;
            }
            html += '</optgroup>';
        }
        html += '<option value="__custom__">Other / custom room…</option>';
        sel.innerHTML = html;
        setCurrentRoomId(current);
    }

    function updateJoinedStatus() {
        const box = $('#pc-joined-status');
        if (!box) return;
        box.textContent = joinedRoom
            ? `Joined: ${joinedRoom} (${joinedRoomType || inferRoomType(joinedRoom)})`
            : 'Not joined to any room.';
    }

    function joinCurrentRoom() {
        const room = currentRoomId();
        const type = $('#pc-chat-type').value;
        if (!room) return;
        if (joinedRoom === room && joinedRoomType === type) return;
        joinedRoom = room;
        joinedRoomType = type;
        messageHistory.length = 0;
        $('#pc-chat-log').innerHTML = '';
        restoreChat(room);
        updateJoinedStatus();
        try {
            localStorage.setItem('perunchat_joined_room', room);
            localStorage.setItem('perunchat_joined_room_type', type);
        } catch (e) { /* storage may be unavailable */ }
        sendFrame({ type: 'ROOM_JOIN', room_id: room, room_type: type, timestamp: Date.now() });
    }

    $('#pc-chat-room-select').addEventListener('change', () => {
        const sel = $('#pc-chat-room-select');
        const custom = $('#pc-chat-room-custom');
        if (sel.value === '__custom__') {
            custom.style.display = 'block';
            custom.focus();
        } else {
            custom.style.display = 'none';
            $('#pc-chat-type').value = inferRoomType(sel.value);
            // Auto-join when the user picks a known room.
            joinCurrentRoom();
        }
    });

    $('#pc-chat-room-custom').addEventListener('change', () => {
        $('#pc-chat-type').value = inferRoomType($('#pc-chat-room-custom').value);
        joinCurrentRoom();
    });

    $('#pc-chat-room-custom').addEventListener('input', () => {
        $('#pc-chat-type').value = inferRoomType($('#pc-chat-room-custom').value);
    });

    $('#pc-chat-type').addEventListener('change', () => {
        refreshRoomSelect();
        // If the newly selected type doesn't match the current room, clear it.
        const room = currentRoomId();
        if (room && inferRoomType(room) !== $('#pc-chat-type').value) {
            setCurrentRoomId('');
        }
    });

    // ---- Social refresh ----------------------------------------------------
    async function refreshSocial() {
        if (!pcConfigured) {
            for (const id of ['pc-friends-list', 'pc-incoming', 'pc-outgoing', 'pc-blocked-list', 'pc-trusted-list']) {
                const box = document.getElementById(id);
                if (box) box.innerHTML = pcPlaceholder;
            }
            return;
        }

        // Fetch each social endpoint independently so one failure does not blank
        // the entire social tab.
        const endpoints = [
            { key: 'friends', url: '/api/perunchat/social/friends', default: { friends: [] } },
            { key: 'incoming', url: '/api/perunchat/social/friends/requests?direction=incoming', default: { requests: [] } },
            { key: 'outgoing', url: '/api/perunchat/social/friends/requests?direction=outgoing', default: { requests: [] } },
            { key: 'fstatus', url: '/api/perunchat/social/friends/status', default: { friends: [] } },
            { key: 'blocked', url: '/api/perunchat/social/blocked', default: { blocked: [] } },
            { key: 'trusted', url: '/api/perunchat/trusted', default: { trusted: [] } },
        ];
        const results = {};
        await Promise.all(endpoints.map(async (ep) => {
            try {
                results[ep.key] = await api(ep.url);
            } catch (e) {
                results[ep.key] = ep.default;
                // Only surface a quiet toast for the trust list; friends/block
                // may be unavailable when the gateway is down and the page still
                // needs to load.
                if (ep.key === 'trusted') {
                    toast('Could not load trust list: ' + e.message, 'err');
                }
            }
        }));

        const { friends, incoming, outgoing, fstatus, blocked, trusted } = results;
        const onlineMap = Object.fromEntries((fstatus.friends || []).map(f => [f.agent_id, f.status]));

            const fList = $('#pc-friends-list');
            const fItems = friends.friends || [];
            fList.innerHTML = fItems.length ? '' : '<p class="muted">No friends yet.</p>';
            for (const id of fItems) {
                const st = onlineMap[id] || 'offline';
                const badge = st === 'online' ? '🟢' : '⚪';
                const row = el('div', { class: 'row', style: 'justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border);' },
                    `<span>${badge} ${esc(id)}</span><button class="btn ghost danger small" data-remove="${esc(id)}">Remove</button>`);
                row.querySelector('button').addEventListener('click', async () => {
                    try { await api(`/api/perunchat/social/friends/${encodeURIComponent(id)}`, { method: 'DELETE' }); toast('Friend removed', 'ok'); refreshSocial(); }
                    catch (e) { toast(e.message, 'err'); }
                });
                fList.appendChild(row);
            }

            const iList = $('#pc-incoming');
            const iReqs = incoming.requests || [];
            iList.innerHTML = iReqs.length ? '' : '<p class="muted">No incoming requests.</p>';
            for (const req of iReqs) {
                const row = el('div', { class: 'row', style: 'justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border);' },
                    `<span>← ${esc(req.from_agent_id)}</span>
                     <span><button class="btn ghost success small" data-accept="${esc(req.request_id)}">Accept</button>
                           <button class="btn ghost danger small" data-reject="${esc(req.request_id)}">Reject</button></span>`);
                row.querySelector('[data-accept]').addEventListener('click', async () => {
                    try { await api(`/api/perunchat/social/friends/requests/${esc(req.request_id)}/accept`, { method: 'POST' }); toast('Request accepted', 'ok'); refreshSocial(); }
                    catch (e) { toast(e.message, 'err'); }
                });
                row.querySelector('[data-reject]').addEventListener('click', async () => {
                    try { await api(`/api/perunchat/social/friends/requests/${esc(req.request_id)}/reject`, { method: 'POST' }); toast('Request rejected', 'ok'); refreshSocial(); }
                    catch (e) { toast(e.message, 'err'); }
                });
                iList.appendChild(row);
            }

            const oList = $('#pc-outgoing');
            const oReqs = outgoing.requests || [];
            oList.innerHTML = oReqs.length ? '' : '<p class="muted">No outgoing requests.</p>';
            for (const req of oReqs) {
                const row = el('div', { class: 'row', style: 'justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border);' },
                    `<span>→ ${esc(req.to_agent_id)} <code>${esc(req.status)}</code></span>
                     <button class="btn ghost danger small" data-cancel="${esc(req.request_id)}">Cancel</button>`);
                row.querySelector('button').addEventListener('click', async () => {
                    try { await api(`/api/perunchat/social/friends/requests/${esc(req.request_id)}/reject`, { method: 'POST' }); toast('Request cancelled', 'ok'); refreshSocial(); }
                    catch (e) { toast(e.message, 'err'); }
                });
                oList.appendChild(row);
            }

            const bList = $('#pc-blocked-list');
            const bItems = blocked.blocked || [];
            bList.innerHTML = bItems.length ? '' : '<p class="muted">No blocked agents.</p>';
            for (const id of bItems) {
                const row = el('div', { class: 'row', style: 'justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border);' },
                    `<span>${esc(id)}</span><button class="btn ghost small" data-unblock="${esc(id)}">Unblock</button>`);
                row.querySelector('button').addEventListener('click', async () => {
                    try { await api(`/api/perunchat/social/blocked/${encodeURIComponent(id)}`, { method: 'DELETE' }); toast('Unblocked', 'ok'); refreshSocial(); }
                    catch (e) { toast(e.message, 'err'); }
                });
                bList.appendChild(row);
            }

            const tList = $('#pc-trusted-list');
            const tItems = trusted.trusted || [];
            tList.innerHTML = tItems.length ? '' : '<p class="muted">No trusted agents.</p>';
            for (const id of tItems) {
                const row = el('div', { class: 'row', style: 'justify-content:space-between; padding:6px 0; border-bottom:1px solid var(--border);' },
                    `<span>🔒 ${esc(id)}</span><button class="btn ghost danger small" data-untrust="${esc(id)}">Remove</button>`);
                row.querySelector('button').addEventListener('click', async () => {
                    try { await api(`/api/perunchat/trusted/${encodeURIComponent(id)}`, { method: 'DELETE' }); toast('Removed from trusted', 'ok'); refreshSocial(); }
                    catch (e) { toast(e.message, 'err'); }
                });
                tList.appendChild(row);
            }
    }

    $('#pc-request-btn').addEventListener('click', async () => {
        const to = $('#pc-request-to').value.trim();
        if (!to) return;
        try { await api('/api/perunchat/social/friends/requests', { method: 'POST', body: { to_agent_id: to } }); toast('Friend request sent', 'ok'); $('#pc-request-to').value = ''; refreshSocial(); }
        catch (e) { toast(e.message, 'err'); }
    });

    $('#pc-block-btn').addEventListener('click', async () => {
        const id = $('#pc-block-id').value.trim();
        if (!id) return;
        try { await api('/api/perunchat/social/blocked', { method: 'POST', body: { blocked_agent_id: id } }); toast('Agent blocked', 'ok'); $('#pc-block-id').value = ''; refreshSocial(); }
        catch (e) { toast(e.message, 'err'); }
    });

    $('#pc-trusted-btn').addEventListener('click', async () => {
        const id = $('#pc-trusted-id').value.trim();
        if (!id) return;
        try { await api('/api/perunchat/trusted', { method: 'POST', body: { agent_id: id } }); toast('Agent trusted', 'ok'); $('#pc-trusted-id').value = ''; refreshSocial(); }
        catch (e) { toast(e.message, 'err'); }
    });

    refreshSocial();

    // ---- WebSocket chat ----------------------------------------------------
    let ws = null;
    let seq = 0;
    try {
        const savedSeq = parseInt(localStorage.getItem('perunchat_seq') || '0', 10);
        seq = Number.isFinite(savedSeq) && savedSeq > 0 ? savedSeq : 0;
    } catch (e) { seq = 0; }
    let joinedRoom = localStorage.getItem('perunchat_joined_room') || null;
    let joinedRoomType = localStorage.getItem('perunchat_joined_room_type') || 'PUBLIC';
    let replyToId = null;
    let replyToPreview = null;
    const messageHistory = []; // keep recent messages for reply rendering
    const operatorMessageIds = new Set(); // dashboard manual messages sent by the human operator
    const MAX_HISTORY = 500;
    const MAX_STORED_HISTORY = 500;
    let wsReconnectTimer = null;
    let wsReconnectDelay = 1000;
    let wsExplicitClose = false;

    function _msgTs(msg) {
        const ts = msg && msg.timestamp;
        if (typeof ts === 'number') return ts;
        const d = ts ? new Date(ts).getTime() : NaN;
        // Messages without a usable timestamp sort to the end instead of
        // appearing as the oldest message in the log.
        return isNaN(d) ? Number.MAX_SAFE_INTEGER : d;
    }

    function chatPayloadText(payload) {
        if (payload == null) return '';
        if (typeof payload === 'string') return payload;
        try {
            return JSON.stringify(payload);
        } catch (_) {
            return String(payload);
        }
    }

    function chatPayloadView(msg) {
        const payload = msg.payload;
        if (typeof payload === 'string') {
            let html = esc(payload);
            // Bound the fan-out: a hostile message could otherwise name hundreds
            // of ids and make the dashboard issue that many lookups.
            for (const id of ChatCore.proposalIds(payload).slice(0, 8)) {
                const chip = `<button class="chip chip-link proposal-ref" data-proposal="${esc(id)}" type="button" title="Open this proposal">${esc(id)}</button>`;
                html = html.split(id).join(chip);
            }
            return html;
        }
        let pretty = '';
        try {
            const serialized = JSON.stringify(payload, null, 2);
            // JSON.stringify(undefined) returns undefined, not a string; a
            // malformed frame with no payload must not throw in .length below.
            pretty = serialized === undefined ? String(payload) : serialized;
        } catch (_) {
            pretty = String(payload);
        }
        const type = String(
            (payload && (payload.type || payload.kind)) || msg.content_type || 'structured message'
        );
        const bits = [];
        for (const key of ['proposal_id', 'id', 'status', 'action', 'intent', 'message']) {
            if (payload && payload[key] != null) {
                bits.push(`${key}: ${String(payload[key]).slice(0, 80)}`);
            }
        }
        const candidate = String(
            (payload && (payload.proposal_id || payload.id)) || ''
        );
        const pid = /^prop_[0-9a-f]{12}$/.test(candidate) ? candidate : '';
        const chip = pid
            ? `<button class="chip chip-link proposal-ref" data-proposal="${esc(pid)}" type="button" title="Open this proposal">${esc(pid)}</button>`
            : '';
        const summary = esc(type + (bits.length ? ' · ' + bits.join(' · ') : ''));
        const body = esc(pretty.length > 20000 ? pretty.slice(0, 20000) + '\n…' : pretty);
        return `<div class="chat-structured">${chip}<details class="chat-json"><summary>${summary}</summary><pre>${body}</pre></details></div>`;
    }

    function sortChatLog(box) {
        if (!box) return;
        const children = Array.from(box.children);
        const timed = children.filter((child) => child.dataset && child.dataset.ts);
        if (timed.length < 2) return;
        const sorted = timed.slice().sort(
            (a, b) => Number(a.dataset.ts) - Number(b.dataset.ts)
        );
        if (sorted.every((node, index) => node === timed[index])) return;
        // Keep system/log lines after the timed messages.
        const anchor = children.find(
            (child) => !(child.dataset && child.dataset.ts)
        ) || null;
        for (const node of sorted) {
            if (anchor && anchor.parentNode === box) box.insertBefore(node, anchor);
            else box.appendChild(node);
        }
    }

    function chatHistoryKey(roomId) {
        return 'perunchat_chat_history:' + (roomId || 'none');
    }

    function persistChat() {
        try {
            const recent = messageHistory.slice(-MAX_STORED_HISTORY);
            localStorage.setItem(chatHistoryKey(joinedRoom), JSON.stringify(recent));
        } catch (e) { /* storage may be unavailable */ }
    }

    function restoreChat(roomId) {
        try {
            const raw = localStorage.getItem(chatHistoryKey(roomId));
            if (!raw) return;
            const stored = JSON.parse(raw);
            if (!Array.isArray(stored)) return;
            messageHistory.length = 0;
            const box = $('#pc-chat-log');
            box.innerHTML = '';
            // Sort by timestamp ascending so a corrupt/out-of-order localStorage
            // array does not render old messages at the bottom.
            const sorted = stored
                .slice(-MAX_HISTORY)
                .sort((a, b) => _msgTs(a) - _msgTs(b));
            for (const msg of sorted) renderMessage(msg, { skipPersist: true });
        } catch (e) { /* ignore corrupt storage */ }
    }

    const log = (html, meta) => {
        const box = $('#pc-chat-log');
        const line = el('div', {}, html);
        if (meta) line._meta = meta;
        box.appendChild(line);
        box.scrollTop = box.scrollHeight;
    };

    let typingIndicatorEl = null;
    let typingTimeoutId = null;
    function showTypingIndicator(msg) {
        const payload = msg && msg.payload;
        // An explicit idle/cancel payload removes the indicator.
        if (payload === 'idle' || payload === false || payload === 'false') {
            removeTypingIndicator();
            return;
        }
        removeTypingIndicator();
        const box = $('#pc-chat-log');
        const who = (msg.sender && msg.sender.agent_id) || 'Perun';
        // Treat any payload safely; ignore object payloads from older builds.
        const label = (typeof payload === 'string' && payload.trim() && payload !== 'typing') ? esc(payload) : 'typing…';
        typingIndicatorEl = el('div', { class: 'perunchat-typing', id: 'pc-typing-indicator' },
            `<span class="muted" style="font-size:12px;font-style:italic">${esc(who)} is ${label}</span>`);
        box.appendChild(typingIndicatorEl);
        box.scrollTop = box.scrollHeight;
        // Hard client-side cap so a lost/lagging indicator never persists forever.
        typingTimeoutId = window.setTimeout(removeTypingIndicator, 60000);
    }
    function removeTypingIndicator() {
        if (typingTimeoutId) {
            window.clearTimeout(typingTimeoutId);
            typingTimeoutId = null;
        }
        if (typingIndicatorEl) {
            typingIndicatorEl.remove();
            typingIndicatorEl = null;
        }
    }
    const setWsStatus = (text, cls) => {
        const badge = $('#pc-ws-status');
        badge.textContent = text;
        badge.className = 'badge ' + (cls || '');
    };

    const isOperatorRuntime = (rt) => rt === 'perun-operator' || (typeof rt === 'string' && rt.endsWith('-operator'));

    function isTypingFrame(msg) {
        return msg && msg.content_type === 'application/perun-typing';
    }

    function formatSenderLabel(sender, forceOperator = false) {
        const operator = forceOperator || (sender && isOperatorRuntime(sender.runtime));
        if (!operator) {
            return sender && sender.agent_id ? esc(sender.agent_id) : '?';
        }
        let label;
        if (sender && isOperatorRuntime(sender.runtime)) {
            label = esc(sender.runtime);
        } else if (myAgentId) {
            label = esc(`${myAgentId}-operator`);
        } else {
            label = 'perun-operator';
        }
        const isOwn = sender && sender.agent_id === myAgentId;
        return isOwn ? `${label} (you)` : label;
    }

    function renderMessage(msg, opts = {}) {
        // Dedupe/room filtering live in chat_core.js so they can be tested
        // without a DOM (node --test web/js/chat_core.test.js).
        if (!ChatCore.shouldRender(messageHistory, msg, joinedRoom)) return;
        // Typing indicators are transient UI state, not chat history.
        if (msg.content_type === 'application/perun-typing') {
            showTypingIndicator(msg);
            return;
        }
        removeTypingIndicator();
        // Insert in timestamp order so out-of-order replays / recovered digests
        // do not appear at the bottom.
        const msgTs = _msgTs(msg);
        let insertIndex = messageHistory.length;
        for (let i = 0; i < messageHistory.length; i++) {
            if (_msgTs(messageHistory[i]) > msgTs) {
                insertIndex = i;
                break;
            }
        }
        const box = $('#pc-chat-log');
        messageHistory.splice(insertIndex, 0, msg);
        let shifted = false;
        if (messageHistory.length > MAX_HISTORY) {
            const removed = messageHistory.shift();
            shifted = true;
            if (removed && removed.id === msg.id) {
                // The new message is older than the whole visible history;
                // evict it immediately without rendering.
                return;
            }
            if (removed && removed.id) {
                const oldLine = box.querySelector(`[data-reply-id="${CSS.escape(removed.id)}"]`);
                if (oldLine) oldLine.parentElement.remove();
            }
        }
        // Keep the in-memory operator set bounded to the visible history.
        const visibleIds = new Set(messageHistory.map((m) => m.id));
        const opsToRemove = [];
        for (const id of operatorMessageIds) {
            if (!visibleIds.has(id)) opsToRemove.push(id);
        }
        for (const id of opsToRemove) operatorMessageIds.delete(id);
        if (!opts.skipPersist) persistChat();

        let isOperator = (msg.sender && isOperatorRuntime(msg.sender.runtime)) || operatorMessageIds.has(msg.id) || msg._perunOperator === true;
        if (isOperator) msg._perunOperator = true;
        const own = formatSenderLabel(msg.sender, isOperator);
        const ownColor = isOperator ? 'var(--green)' : 'var(--gold)';
        const ts = msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString() : '';
        let replyHtml = '';
        if (msg.in_reply_to) {
            const original = messageHistory.find(m => m.id === msg.in_reply_to);
            const origOperator = original && ((original.sender && isOperatorRuntime(original.sender.runtime)) || operatorMessageIds.has(original.id) || original._perunOperator === true);
            const replyToWho = original ? formatSenderLabel(original.sender, origOperator) : 'unknown';
            const replyToText = original ? esc(chatPayloadText(original.payload).slice(0, 60)) : '…';
            replyHtml = `<div style="font-size:12px; color:var(--text-muted); margin-left:12px; border-left:2px solid var(--border-strong); padding-left:8px;">↩ replying to @${replyToWho}: ${replyToText}</div>`;
        }

        // Proposal ids are how the chat names work that needs the operator. Turn
        // each one into a chip; clicking it opens a status/intent card with an
        // "Open in project" jump to the exact Shared-tab row. Structured
        // payloads are rendered as an expandable JSON preview instead of
        // "[object Object]".
        const payloadHtml = chatPayloadView(msg);
        const lineHtml = `<div style="margin-bottom:4px;">
            <div class="row" style="gap:10px; align-items:baseline;">
                <span style="color:${ownColor}; font-weight:600;">@${own}</span>
                <span class="muted" style="font-size:11px;">${esc(ts)}</span>
                <button class="btn ghost small" data-reply-id="${esc(msg.id)}" data-reply-sender="${own}">Reply</button>
            </div>
            ${replyHtml}
            <div style="margin-left:12px; overflow-wrap:anywhere;">${payloadHtml}</div>
        </div>`;
        // Insert the DOM line at the same position so the visual order matches
        // the sorted messageHistory. If we just evicted the oldest line, the
        // remaining children shifted down by one.
        const line = el('div', {}, lineHtml);
        line._meta = { id: msg.id };
        line.dataset.ts = String(msgTs);
        const domIndex = shifted ? Math.max(0, insertIndex - 1) : insertIndex;
        const beforeNode = box.children[domIndex] || null;
        if (beforeNode) box.insertBefore(line, beforeNode);
        else box.appendChild(line);
        // Repair the whole log if an older client/restore left it out of
        // order; this is cheap and guarantees ascending timestamps on screen.
        sortChatLog(box);
        box.scrollTop = box.scrollHeight;

        // Attach listener to the reply button of the line we just inserted.
        const btn = box.querySelector(`[data-reply-id="${CSS.escape(msg.id)}"]`);
        if (btn) {
            btn.addEventListener('click', () => {
                replyToId = msg.id;
                replyToPreview = `@${own}: ${chatPayloadText(msg.payload).slice(0, 40)}`;
                const bar = $('#pc-reply-bar');
                if (bar) bar.innerHTML = `↩ ${esc(replyToPreview)} <button class="btn ghost small" id="pc-clear-reply">Cancel</button>`;
                bar.style.display = 'block';
                bar.querySelector('#pc-clear-reply').addEventListener('click', clearReply);
                $('#pc-chat-msg').focus();
            });
        }
        line.querySelectorAll('.proposal-ref').forEach((chip) => {
            const id = chip.dataset.proposal;
            chip.addEventListener('click', (e) => {
                e.stopPropagation();
                openProposalPeek(id);
            });
            // Colour the chip by status and name the project in the tooltip, so
            // the operator can triage a long message without opening each one.
            proposalLookup(id).then((res) => {
                if (!res) return;
                const prop = res.proposal || {};
                const project = res.project || {};
                syncProposalChip(id, prop.status, project.name, prop.expires_at);
            });
        });
    }

    function clearReply() {
        replyToId = null;
        replyToPreview = null;
        const bar = $('#pc-reply-bar');
        if (bar) { bar.innerHTML = ''; bar.style.display = 'none'; }
    }

    function scheduleReconnect() {
        if (wsExplicitClose || wsReconnectTimer) return;
        setWsStatus('reconnecting', 'orange');
        log(`<span class="muted">Reconnecting in ${Math.round(wsReconnectDelay / 1000)}s…</span>`);
        wsReconnectTimer = setTimeout(() => {
            wsReconnectTimer = null;
            connectChat();
        }, wsReconnectDelay);
        wsReconnectDelay = Math.min(wsReconnectDelay * 2, 30000);
    }

    async function connectChat() {
        // A deliberate reconnect or page re-render closes the old socket first;
        // unexpected closes schedule a reconnect from ws.onclose.
        wsExplicitClose = true;
        clearTimeout(wsReconnectTimer);
        wsReconnectTimer = null;
        if (ws) { try { ws.close(); } catch (err) { /* ignore */ } ws = null; }
        let endpoint;
        try {
            // Browsers cannot set Authorization on a WebSocket. Fetch a 30-second
            // single-use ticket instead of putting the long-lived key in the URL.
            const t = await api('/api/perunchat/ws-ticket', { method: 'POST', body: {} });
            const s = await api('/api/perunchat/status');
            endpoint = `${s.endpoint}${s.endpoint.includes('?') ? '&' : '?'}ticket=${encodeURIComponent(t.ticket)}`;
        } catch (e) {
            // A Perun restart or a 401 after key rotation used to leave the panel
            // dead until the operator navigated away and back.
            toast('Could not load chat credentials: ' + e.message, 'err');
            scheduleReconnect();
            return;
        }
        try {
            wsExplicitClose = false;
            wsReconnectDelay = 1000;
            // Reset sequence to a fresh monotonic base for this session. The gateway
            // enforces monotonic sequences per (agent_id, runtime); using a timestamp
            // base avoids stale localStorage values causing non_monotonic_sequence
            // errors after a page refresh or reconnect.
            seq = Math.max(seq, Date.now());
            ws = new WebSocket(endpoint);
            ws.onopen = () => {
                setWsStatus('connected', 'green');
                log('<span class="muted">Connected to PerunChat gateway.</span>');
                if (joinedRoom) {
                    sendFrame({ type: 'ROOM_JOIN', room_id: joinedRoom, room_type: joinedRoomType, timestamp: Date.now() });
                }
            };
            ws.onclose = () => {
                setWsStatus('disconnected', '');
                log('<span class="muted">Disconnected.</span>');
                ws = null;
                scheduleReconnect();
            };
            ws.onerror = (e) => {
                const detail = e && (e.message || e.type || '') ? ` (${e.message || e.type})` : '';
                log(`<span style="color:var(--danger)">WebSocket error${detail}. readyState=${ws ? ws.readyState : 'n/a'}</span>`);
            };
            ws.onmessage = (ev) => {
                try {
                    const msg = JSON.parse(ev.data);
                    if (msg.type === 'ERROR') {
                        log(`<span style="color:var(--danger)">Error: ${esc(msg.message)}${msg.room_id ? ' (' + esc(msg.room_id) + ')' : ''}</span>`);
                    } else if (msg.type === 'ROOM_JOINED') {
                        log(`<span class="muted">Joined ${esc(msg.room_id)}.</span>`);
                    } else if (msg.type === 'ROOM_LEFT') {
                        log(`<span class="muted">Left ${esc(msg.room_id)}.</span>`);
                    } else if (msg.type === 'PRESENCE_UPDATE') {
                        log(`<span class="muted">${esc(msg.agent_id)} is ${esc(msg.status)}.</span>`);
                    } else if (msg.sender && msg.payload !== undefined) {
                        renderMessage(msg);
                    } else {
                        log(`<span class="muted">${esc(ev.data)}</span>`);
                    }
                } catch (_) { log(`<span class="muted">${esc(ev.data)}</span>`); }
            };
        } catch (e) { toast('Could not open WebSocket: ' + e.message, 'err'); scheduleReconnect(); }
    }

    function sendFrame(frame) {
        if (!ws || ws.readyState !== WebSocket.OPEN) { toast('Not connected', 'err'); return; }
        ws.send(JSON.stringify(frame));
    }

    $('#pc-chat-join').addEventListener('click', () => {
        if (!currentRoomId()) { toast('Choose a room first', 'err'); return; }
        joinCurrentRoom();
    });

    $('#pc-chat-leave').addEventListener('click', () => {
        if (!joinedRoom) return;
        const type = joinedRoomType || $('#pc-chat-type').value;
        sendFrame({ type: 'ROOM_LEAVE', room_id: joinedRoom, room_type: type, timestamp: Date.now() });
        joinedRoom = null;
        joinedRoomType = 'PUBLIC';
        messageHistory.length = 0;
        $('#pc-chat-log').innerHTML = '';
        setCurrentRoomId('');
        updateJoinedStatus();
        try {
            localStorage.removeItem('perunchat_joined_room');
            localStorage.removeItem('perunchat_joined_room_type');
        } catch (e) { /* storage may be unavailable */ }
    });

    let _lastSentText = '';
    let _lastSentAt = 0;

    function sendChatMessage() {
        if (!joinedRoom) { toast('Join a room first', 'err'); return; }
        const text = $('#pc-chat-msg').value;
        const now = Date.now();
        // A double click / double Enter used to send the same text twice (two
        // ids, two rows). The guard lives in chat_core.js so it is testable.
        if (!ChatCore.shouldSend(_lastSentText, _lastSentAt, text, now)) return;
        _lastSentText = String(text).trim();
        _lastSentAt = now;
        seq += 1;
        try { localStorage.setItem('perunchat_seq', String(seq)); } catch (e) { /* ignore */ }
        const frame = {
            id: `msg_${now}`,
            trace_id: `trc_${now}`,
            thread_id: 'default',
            sequence_number: seq,
            timestamp: now,
            room_type: $('#pc-chat-type').value,
            room_id: joinedRoom,
            sender: { agent_id: myAgentId, runtime: myAgentId ? `${myAgentId}-operator` : 'perun-operator' },
            recipient_id: null,
            content_type: 'text/plain',
            turn_count: 0,
            payload: text,
        };
        if (replyToId) frame.in_reply_to = replyToId;
        // Mark this frame as a human operator message so the dashboard can
        // label it as the human administrator rather than as autonomous Perun.
        operatorMessageIds.add(frame.id);
        // The gateway now echoes every chat frame back to the sender, so the
        // message will appear once the round-trip completes.
        sendFrame(frame);
        $('#pc-chat-msg').value = '';
        clearReply();
    }

    $('#pc-chat-send').addEventListener('click', sendChatMessage);
    $('#pc-chat-msg').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            sendChatMessage();
        }
    });

    // Restore previously joined room and chat history into the panel.
    const restoredRoom = joinedRoom;
    if (restoredRoom) {
        $('#pc-chat-type').value = joinedRoomType || inferRoomType(restoredRoom);
        refreshRoomSelect();
        setCurrentRoomId(restoredRoom);
        restoreChat(restoredRoom);
    } else {
        refreshRoomSelect();
    }
    updateJoinedStatus();

    // Close the chat socket if the user navigates away from this page so it
    // does not keep reconnecting in the background.
    function cleanupPerunChat() {
        wsExplicitClose = true;
        clearTimeout(wsReconnectTimer);
        wsReconnectTimer = null;
        if (ws) { try { ws.close(); } catch (err) { /* ignore */ } ws = null; }
    }
    function onHashChange() {
        if (!location.hash.startsWith('#/perunchat')) {
            cleanupPerunChat();
            window.removeEventListener('hashchange', onHashChange);
        }
    }
    window.addEventListener('hashchange', onHashChange);

    if (pcConfigured) {
        connectChat();
    } else {
        log('<span class="muted">Add your PerunChat API key above and save, to enable live chat.</span>');
    }
}

/* ========================================================= PROVIDERS ==== */
function _providerStatusText(p) {
    if (p.status === 'connected & configured') return 'Connected & configured';
    if (p.status === 'connected') return 'Saved credential';
    if (p.status === 'configured') return 'Configured in OpenCode';
    if (p.status === 'configured (no key)') return 'Configured (no key)';
    return 'Not connected';
}

function providerConnectDialog(p, hadComments = false) {
    const body = el('div', {});
    body.dataset.kind = p.kind || 'api_key';
    const isCustom = p.kind === 'custom';
    const needsId = isCustom || !p.id;

    if (needsId) {
        body.appendChild(el('label', { class: 'field' },
            `<span class="lab">Provider ID</span><input type="text" id="prf-id" value="${esc(p.id || '')}" placeholder="my-provider">`));
    } else {
        body.appendChild(el('input', { type: 'hidden', id: 'prf-id', value: p.id }));
        body.appendChild(el('p', { class: 'secondary' },
            `Provider: <b>${esc(p.name || p.id)}</b> <span class="muted">(${esc(p.kind || 'api_key')})</span>`));
    }
    if (isCustom) {
        body.appendChild(el('label', { class: 'field' },
            `<span class="lab">Display name</span><input type="text" id="prf-name" value="${esc(p.name || '')}" placeholder="My Provider">`));
        body.appendChild(el('label', { class: 'field' },
            `<span class="lab">Package</span><input type="text" id="prf-package" value="${esc(p.package || '@opencode/ai/providers/openai-compatible')}" placeholder="@opencode/ai/providers/openai-compatible">`));
    }
    if (p.kind !== 'oauth') {
        const basePlaceholder = p.kind === 'local' ? 'http://127.0.0.1:11434/v1' : 'optional proxy / base URL';
        body.appendChild(el('label', { class: 'field' },
            `<span class="lab">Base URL</span><input type="text" id="prf-baseurl" value="${esc(p.default_base_url || p.base_url || '')}" placeholder="${basePlaceholder}">`));
        if (p.kind === 'api_key' || isCustom) {
            body.appendChild(el('label', { class: 'field' },
                `<span class="lab">API key</span><input type="password" id="prf-key" autocomplete="off" placeholder="paste key here">`));
        }
        body.appendChild(el('label', { class: 'field' },
            `<span class="lab">Models (comma-separated, optional)</span><input type="text" id="prf-models" placeholder="model-1, model-2">`));
    }
    body.appendChild(el('p', { class: 'muted mt' },
        'Credentials are written to your OpenCode config; Perun does not store them separately.'));
    if (hadComments) {
        body.appendChild(el('p', { class: 'muted mt' },
            'Note: your opencode.json[c] currently contains comments or trailing commas; saving will rewrite it as plain JSON and those will be removed.'));
    }

    const submit = async () => {
        const id = $('#prf-id').value.trim();
        if (!id) { toast('Provider ID is required.', 'err'); return; }
        const payload = {};
        const baseUrl = $('#prf-baseurl') ? $('#prf-baseurl').value.trim() : '';
        const apiKey = $('#prf-key') ? $('#prf-key').value.trim() : '';
        const models = $('#prf-models') ? $('#prf-models').value.split(',').map((s) => s.trim()).filter(Boolean) : [];
        if (baseUrl) payload.base_url = baseUrl;
        if (apiKey) payload.api_key = apiKey;
        if (models.length) payload.models = models;
        if ($('#prf-name')) {
            const name = $('#prf-name').value.trim();
            if (name) payload.name = name;
        }
        if ($('#prf-package')) {
            const pkg = $('#prf-package').value.trim();
            if (pkg) payload.package = pkg;
        }
        try {
            const res = await api('/api/providers/' + encodeURIComponent(id) + '/connect', {
                method: 'POST',
                body: payload,
            });
            const reloaded = res && res.reloaded;
            toast(`Provider saved${reloaded ? ' — OpenCode config reloaded' : ''}`, reloaded ? 'ok' : '');
            closeModal();
            await viewProviders(true);
            await loadModels(true);
        } catch (e) {
            toast(e.message, 'err');
        }
    };

    modal(p.id ? `Connect ${p.name || p.id}` : 'Add provider', body, [
        { label: 'Cancel', class: 'ghost', onClick: closeModal },
        { label: 'Save', class: 'primary', onClick: submit },
    ]);
    const firstInput = body.querySelector('input:not([type=hidden])');
    if (firstInput) firstInput.focus();
}

function addProviderDialog(catalog, hadComments = false) {
    const body = el('div', {});
    const options = [`<option value="">Choose a provider…</option>`]
        .concat(catalog.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} (${esc(p.kind)})</option>`))
        .concat(['<option value="__custom__">➕ Custom OpenAI-compatible provider</option>'])
        .join('');
    body.innerHTML = `<label class="field"><span class="lab">Provider</span><select id="pr-add-select">${options}</select></label>
        <p class="muted mt">Select a preset or configure a custom endpoint.</p>`;

    const select = $('#pr-add-select', body);
    select.addEventListener('change', () => {
        const id = select.value;
        closeModal();
        if (id === '__custom__') {
            providerConnectDialog({ id: '', name: '', kind: 'custom', package: '@opencode/ai/providers/openai-compatible' }, hadComments);
        } else if (id) {
            const p = catalog.find((c) => c.id === id);
            if (p) providerConnectDialog(p, hadComments);
        }
    });
    modal('Add provider', body, [
        { label: 'Cancel', class: 'ghost', onClick: closeModal },
    ]);
    select.focus();
}

async function viewProviders(refresh = false) {
    $('#topbar-title').textContent = 'Providers';
    const v = $('#view');
    v.innerHTML = '<div class="empty"><span class="spinner"></span> loading providers…</div>';
    let data;
    try {
        data = await api('/api/providers' + (refresh ? '?refresh=true' : ''));
    } catch (e) {
        v.innerHTML = `<div class="empty">Could not load providers: ${esc(e.message)}</div>`;
        return;
    }
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">LLM integrations</div><h1>Providers</h1>
         <div class="sub">Connect API-key providers and local endpoints directly; OAuth providers still use <code>opencode auth login</code>. Credentials live in your OpenCode config.</div></div>
         <div class="form-actions">
            <button class="btn ghost" id="pr-refresh">⟳ Refresh</button>
            <button class="btn primary" id="pr-add">＋ Add provider</button>
         </div>`));

    // Usage per provider: the service's own quota when it publishes one, and
    // always Perun's measured spend (attributed per message model, so a session
    // that switches provider is priced correctly).
    let measuredByProvider = {};
    let budgets = [];
    try {
        const usageInfo = await api('/api/providers/usage');
        for (const row of (usageInfo.measured || [])) measuredByProvider[row.provider] = row;
        const limits = usageInfo.limits || {};
        if (Array.isArray(limits.budgets)) budgets = limits.budgets;
    } catch (e) { /* usage is optional; the tab still works */ }

    const usageLine = (providerId) => {
        const measured = measuredByProvider[providerId];
        const parts = [];
        const budget = budgets.find((b) => (b.vendors || []).includes(providerId) || b.id === providerId);
        if (budget && budget.limit) {
            const pct = Math.round((Number(budget.spent || 0) / Number(budget.limit)) * 100);
            const reset = budget.resetsAt ? ` · resets ${esc(String(budget.resetsAt).slice(0, 10))}` : '';
            parts.push(`<span class="badge ${pct >= 90 ? 'warn' : 'ok'}" title="Reported by the provider">quota ${pct}%</span>
                        <span class="muted">$${Number(budget.spent || 0).toFixed(2)} of $${Number(budget.limit).toFixed(2)}${reset}</span>`);
        }
        if (measured) {
            if (!budget && measured.budget) {
                const pct = Math.round(Number(measured.budget_percent || 0) * 100);
                parts.push(`<span class="badge ${pct >= 90 ? 'warn' : ''}" title="Perun's spend against the budget you set in settings">budget ${pct}%</span>`);
            }
            parts.push(`<span class="muted" title="Measured by Perun from captured sessions (30d)">measured $${Number(measured.cost || 0).toFixed(4)} · ${Number(measured.tokens || 0).toLocaleString()} tok</span>`);
        }
        return parts.length ? `<div class="provider-meta">${parts.join(' ')}</div>` : '';
    };

    const active = data.providers || [];
    if (!active.length) {
        v.appendChild(el('div', { class: 'card empty' },
            data.error ? esc(data.error) : 'No providers connected yet. Add one below.'));
    } else {
        const grid = el('div', { class: 'grid providers-grid' });
        for (const p of active) {
            const connected = !!p.connected;
            const statusClass = connected ? 'ok' : 'warn';
            const statusText = _providerStatusText(p);
            const actions = [];
            if (p.kind === 'oauth') {
                actions.push(`<button class="btn ${connected ? 'ghost' : 'primary'}" data-action="login" data-id="${esc(p.id)}">${connected ? 'Re-connect' : 'Connect…'}</button>`);
            } else {
                actions.push(`<button class="btn ${connected ? 'ghost' : 'primary'}" data-action="connect" data-id="${esc(p.id)}">${connected ? 'Edit' : 'Connect…'}</button>`);
            }
            if (connected || p.configured) {
                actions.push(`<button class="btn ghost danger" data-action="disconnect" data-id="${esc(p.id)}">Disconnect</button>`);
            }
            const card = el('div', { class: 'card provider-card' },
                `<div class="card-head"><h4>${esc(p.name || p.id)}</h4><span class="badge ${statusClass}">${statusText}</span></div>
                 <div class="provider-meta">
                    <span class="mono">${esc(p.id)}</span>
                    ${p.connections && p.connections.length ? `<span class="muted">${esc(p.connections.map((c) => c.label || c.method || c.type).join(', '))}</span>` : ''}
                    ${p.base_url ? `<span class="muted">${esc(p.base_url)}</span>` : ''}
                 </div>
                 ${usageLine(p.id)}
                 <div class="form-actions">${actions.join('')}</div>`);
            grid.appendChild(card);
        }
        v.appendChild(grid);
    }

    const catalog = data.catalog || [];
    if (catalog.length) {
        v.appendChild(sectionTitle('Add a provider'));
        const catGrid = el('div', { class: 'grid providers-grid' });
        for (const p of catalog) {
            const card = el('div', { class: 'card provider-card' },
                `<div class="card-head"><h4>${esc(p.name)}</h4><span class="badge muted">${esc(p.kind)}</span></div>
                 <div class="provider-meta"><span class="mono">${esc(p.id)}</span></div>
                 <div class="form-actions">
                    <button class="btn primary" data-action="add" data-id="${esc(p.id)}">Add…</button>
                 </div>`);
            catGrid.appendChild(card);
        }
        v.appendChild(catGrid);
    }

    $('#pr-refresh').addEventListener('click', async () => { await viewProviders(true); });
    $('#pr-add').addEventListener('click', () => { addProviderDialog(catalog, data.config_had_comments); });

    v.querySelectorAll('button[data-action="login"]').forEach((b) => {
        b.addEventListener('click', () => {
            const id = b.dataset.id;
            const cmd = `opencode auth login ${esc(id)}`;
            confirmDialog('Connect provider', `Open a terminal to run <code>${cmd}</code>? Perun will not see your credentials.`, async () => {
                try {
                    await api('/api/run-in-terminal', {
                        method: 'POST',
                        body: { action: 'auth-login', provider: id, shell: terminalId() },
                    });
                    toast('Terminal opened — complete login there', 'ok');
                } catch (e) {
                    toast('Could not open terminal — command copied', 'ok');
                    copyCommandToClipboard(`opencode auth login ${id}`);
                }
            }, 'Open terminal');
        });
    });
    v.querySelectorAll('button[data-action="connect"]').forEach((b) => {
        b.addEventListener('click', () => {
            const id = b.dataset.id;
            const p = active.find((x) => x.id === id) || { id };
            providerConnectDialog(p, data.config_had_comments);
        });
    });
    v.querySelectorAll('button[data-action="add"]').forEach((b) => {
        b.addEventListener('click', () => {
            const id = b.dataset.id;
            const p = catalog.find((x) => x.id === id) || { id };
            providerConnectDialog(p, data.config_had_comments);
        });
    });
    v.querySelectorAll('button[data-action="disconnect"]').forEach((b) => {
        b.addEventListener('click', async () => {
            const id = b.dataset.id;
            confirmDialog('Disconnect provider',
                `Remove <b>${esc(id)}</b> from your OpenCode config and clear any saved credential?`,
                async () => {
                    try {
                        await api('/api/providers/' + encodeURIComponent(id) + '/disconnect', { method: 'POST' });
                        toast('Disconnected', 'ok');
                        await viewProviders(true);
                        await loadModels(true);
                    } catch (e) {
                        toast(e.message, 'err');
                    }
                }, 'Disconnect');
        });
    });
}

/* ========================================================= SETTINGS ===== */
let routingTimer = null;
// Usage cards refresh in place while their view is open. 60 s is the floor that
// makes sense: the server only adds new TUI numbers every 5 minutes, and a
// headless run's numbers land when it finalizes.
let dashboardUsageTimer = null;
let projectUsageTimer = null;
const USAGE_REFRESH_MS = 60000;

function stopRoutingTimer() {
    if (routingTimer) { clearInterval(routingTimer); routingTimer = null; }
}

async function loadRoutingPanel() {
    const host = $('#routing-panel');
    if (!host) return;
    stopRoutingTimer();
    host.innerHTML = '<div class="meta">loading routing decisions…</div>';
    let res;
    try {
        res = await api('/api/routing/decisions?hours=24&limit=25');
    } catch (e) {
        host.innerHTML = `<div class="meta">routing stats unavailable: ${esc(e.message)}</div>`;
        return;
    }
    const mode = $('#routing-mode');
    if (mode) {
        const enabled = res.enabled !== false;
        const shadowMode = res.shadow_mode !== false;
        mode.textContent = enabled ? (shadowMode ? 'shadow' : 'live') : 'off';
        if (enabled && !res.total) mode.textContent += ' · no traffic yet';
        mode.className = 'badge ' + (!enabled ? '' : shadowMode ? 'amber' : 'green');
    }
    const parts = Object.entries(res.by_action || {})
        .sort((a, b) => b[1] - a[1])
        .map(([k, v]) => `${esc(k)} ${v}`)
        .join(' · ');
    // Coverage is per source: rooms are conversational by nature, so their
    // "unknown" share is expected — the metric to watch is whether it drops
    // after a router change, not whether it is low.
    const coverage = Object.entries(res.coverage || {})
        .sort((a, b) => b[1].total - a[1].total)
        .map(([src, c]) => {
            const pct = c.total ? Math.round((c.coverage || 0) * 100) : 0;
            return `<span class="meta" title="${esc(src)}: ${c.total} message(s), ${c.unknown} unrecognised">${esc(src)} ${pct}% of ${c.total}</span>`;
        })
        .join(' · ');
    const head = `<div class="row wrap" style="gap:10px;align-items:center;margin:2px 0 8px">
        <strong>${res.total || 0}</strong> decision(s) in ${res.window_hours}h
        ${res.total ? `<span class="meta">unknown ${Math.round((res.unknown_share || 0) * 100)}%</span>` : ''}
        ${coverage ? `<span class="meta">today: ${coverage}</span>` : ''}
        <span class="meta">${parts}</span>
        <button class="btn sm ghost" id="routing-refresh" title="Refresh">⟳</button>
      </div>`;
    const rows = (res.decisions || []).map((d) => {
        const cls = d.action === 'unknown' ? 'muted' : d.action === 'clarify' ? 'amber' : 'blue';
        const conf = d.confidence == null ? '' : `${Math.round(d.confidence * 100)}%`;
        const detail = [d.matched_by || '', conf, d.source || '', timeAgo(d.ts)]
            .filter(Boolean).join(' · ');
        const report = d.reported
            ? `<button class="btn sm ghost" data-unmisroute="${d.id}" title="Reported as wrong — click to undo">↺</button>`
            : `<button class="btn sm ghost danger" data-misroute="${d.id}" title="This decision was wrong">✗</button>`;
        return `<div class="list-row">
            <span class="badge ${cls}">${esc(d.action || '?')}</span>
            <span class="intent"><div class="msg truncate">${esc(d.project || '—')}</div>
            <div class="meta">${esc(detail)}</div></span>
            <span class="row" style="gap:6px">${report}</span>
          </div>`;
    }).join('');
    host.innerHTML = head + (rows || '<div class="meta">No routing decisions yet. Send a Telegram message and it will appear here (shadow mode logs only).</div>');
    $('#routing-refresh')?.addEventListener('click', () => loadRoutingPanel());
    const reportOne = async (btn, method, url, okText) => {
        if (btn.disabled) return;
        btn.disabled = true;
        try {
            await api(url, { method, body: {} });
            toast(okText, 'ok');
            loadRoutingPanel();
        } catch (e) {
            btn.disabled = false;
            toast(e.message, 'err');
        }
    };
    host.querySelectorAll('[data-misroute]').forEach((btn) => {
        btn.addEventListener('click', () => reportOne(
            btn, 'POST', `/api/routing/decisions/${btn.dataset.misroute}/misroute`, 'Reported — thanks'));
    });
    host.querySelectorAll('[data-unmisroute]').forEach((btn) => {
        btn.addEventListener('click', () => reportOne(
            btn, 'DELETE', `/api/routing/decisions/${btn.dataset.unmisroute}/misroute`, 'Report cleared'));
    });
    // Refresh only while this panel is on screen; navigating away clears it.
    routingTimer = setInterval(() => {
        if (!document.getElementById('routing-panel')) { stopRoutingTimer(); return; }
        loadRoutingPanel();
    }, 60000);
}

let attentionTimer = null;
let chatNeedsTimer = null;

async function viewAttention() {
    $('#topbar-title').textContent = 'Needs you';
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'card' },
        `<div class="card-head"><h4>Needs you</h4><span class="badge" id="attention-count">—</span></div>
         <p class="secondary">Room mentions, questions, permission requests, forms, proposals and handoffs waiting on you. A reply goes back to the exact room or session.</p>
         <div id="attention-panel"><div class="meta">loading…</div></div>`));
    await loadAttentionPanel();
}

async function loadAttentionPanel() {
    const host = $('#attention-panel');
    if (!host) return;
    if (attentionTimer) { clearInterval(attentionTimer); attentionTimer = null; }
    let res;
    try {
        res = await api('/api/attention?state=open&limit=20');
    } catch (e) {
        host.innerHTML = `<div class="meta">inbox unavailable: ${esc(e.message)}</div>`;
        return;
    }
    const items = res.items || [];
    const badge = $('#attention-count');
    if (badge) {
        badge.textContent = items.length ? `${items.length} open` : 'clear';
        badge.className = 'badge ' + (items.length ? 'amber' : 'green');
    }
    if (!items.length) {
        host.innerHTML = '<div class="meta">Nothing is waiting on you.</div>';
        attentionTimer = setInterval(() => {
            if (!document.getElementById('attention-panel')) { clearInterval(attentionTimer); attentionTimer = null; return; }
            loadAttentionPanel();
        }, 30000);
        return;
    }
    // The panel refreshes every 30 s; keep whatever the operator has typed so a
    // tick cannot swallow a half-written reply.
    const drafts = {};
    host.querySelectorAll('[data-att-input]').forEach((input) => {
        drafts[input.dataset.attInput] = input.value;
    });
    host.innerHTML = items.map((it) => {
        const choices = (it.options || []).map((o) =>
            `<button class="btn sm" data-att-choose="${it.id}" data-att-option="${esc(o.id)}">${esc(o.label)}</button>`
        ).join(' ');
        const detail = [it.target_kind, `att_${it.id}`, timeAgo(it.created_at)].filter(Boolean).join(' · ');
        return `<div class="list-row" style="align-items:flex-start">
            <span class="badge ${it.target_kind === 'permission' ? 'amber' : 'blue'}">${esc(it.target_kind)}</span>
            <span class="intent">
              <div class="msg">${esc(it.question || '')}</div>
              <div class="meta">${esc(detail)}</div>
              <div class="row wrap" style="gap:6px;margin-top:6px">
                <input class="input" data-att-input="${it.id}" placeholder="reply…" style="min-width:240px">
                <button class="btn sm" data-att-reply="${it.id}">Send</button>
                ${choices}
                <button class="btn sm ghost danger" data-att-dismiss="${it.id}" title="Dismiss">✕</button>
              </div>
            </span>
          </div>`;
    }).join('');
    host.querySelectorAll('[data-att-input]').forEach((input) => {
        const draft = drafts[input.dataset.attInput];
        if (draft) input.value = draft;
    });
    const refresh = () => loadAttentionPanel();
    host.querySelectorAll('[data-att-reply]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.attReply;
            const input = host.querySelector(`[data-att-input="${id}"]`);
            const text = (input?.value || '').trim();
            if (!text) return;
            btn.disabled = true;
            try {
                await api(`/api/attention/${id}/reply`, { method: 'POST', body: { text } });
                toast('Answer delivered', 'ok');
                refresh();
            } catch (e) { btn.disabled = false; toast(e.message, 'err'); }
        });
    });
    const choose = async (btn, confirm) => {
        btn.disabled = true;
        try {
            await api(`/api/attention/${btn.dataset.attChoose}/choose`,
                { method: 'POST', body: { option: btn.dataset.attOption, confirm: !!confirm } });
            toast('Recorded', 'ok');
            refresh();
        } catch (e) { btn.disabled = false; toast(e.message, 'err'); }
    };
    host.querySelectorAll('[data-att-choose]').forEach((btn) => {
        btn.addEventListener('click', () => {
            if (btn.dataset.attOption === 'always') {
                confirmDialog('Always allow',
                    'This saves a persistent permission rule for the project, covering future requests too.',
                    () => choose(btn, true), 'Always allow');
            } else {
                choose(btn, false);
            }
        });
    });
    host.querySelectorAll('[data-att-dismiss]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            try {
                await api(`/api/attention/${btn.dataset.attDismiss}`, { method: 'DELETE' });
                refresh();
            } catch (e) { btn.disabled = false; toast(e.message, 'err'); }
        });
    });
    loadAttentionOutbox(host);
    attentionTimer = setInterval(() => {
        if (!document.getElementById('attention-panel')) { clearInterval(attentionTimer); attentionTimer = null; return; }
        loadAttentionPanel();
    }, 30000);
}

async function loadAttentionOutbox(host) {
    let res;
    try { res = await api('/api/attention/outbox?limit=10'); } catch (_) { return; }
    const rows = (res.items || []).filter((r) => !r.delivered_at);
    if (!rows.length) return;
    const section = document.createElement('div');
    section.innerHTML = `<div class="meta" style="margin-top:10px">Queued room replies</div>` +
        rows.map((r) => `<div class="list-row">
            <span class="badge ${r.attempts >= 10 ? 'amber' : 'blue'}">${r.attempts >= 10 ? 'retired' : 'queued'}</span>
            <span class="intent"><div class="msg truncate">${esc(r.target_id || '')}</div>
            <div class="meta">${esc((r.payload && r.payload.text) || '')} · attempts ${r.attempts || 0}${r.last_error ? ' · ' + esc(r.last_error) : ''}</div></span>
            <span class="row" style="gap:6px"><button class="btn sm ghost" data-outbox-retry="${r.id}">Retry</button></span>
          </div>`).join('');
    host.appendChild(section);
    section.querySelectorAll('[data-outbox-retry]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            try {
                await api(`/api/attention/outbox/${btn.dataset.outboxRetry}/retry`, { method: 'POST', body: {} });
                toast('Queued for retry', 'ok');
                loadAttentionPanel();
            } catch (e) { btn.disabled = false; toast(e.message, 'err'); }
        });
    });
}

function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
    if (n >= 1048576) return Math.round(n / 1048576) + ' MB';
    if (n >= 1024) return Math.round(n / 1024) + ' KB';
    return n + ' B';
}

function fmtTokens(n) {
    n = Number(n) || 0;
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 1000) return Math.round(n / 1000) + 'k';
    return String(n);
}

let localAiStatusCache = { ts: 0, data: null };

async function fetchLocalAiStatus(force = false) {
    if (!force && localAiStatusCache.data && Date.now() - localAiStatusCache.ts < 2000) {
        return localAiStatusCache.data;
    }
    const data = await api('/api/local-ai');
    localAiStatusCache = { ts: Date.now(), data };
    return data;
}

function localAiCard() {
    const s = state.settings || {};
    const card = el('div', { class: 'card', id: 'local-ai-card' },
        `<div class="card-head"><h4>Local AI</h4><span class="badge" id="la-badge">—</span></div>
         <p class="secondary">A small local model for bounded work only — changelog bullets and OpenCode's
         own small tasks. It is never on a run's critical path and never sees a provider credential.</p>
         <div class="row wrap" style="gap:18px;margin:6px 0 4px">
             <label class="row" style="gap:6px"><input type="checkbox" id="la-enabled" ${s.local_ai_enabled ? 'checked' : ''}> enable local AI</label>
             <label class="row" style="gap:6px"><input type="checkbox" id="la-shadow" ${s.local_ai_shadow !== false ? 'checked' : ''}> shadow (log only)</label>
             <label class="row" style="gap:6px"><input type="checkbox" id="la-docs" ${s.local_ai_docs !== false ? 'checked' : ''}> changelog bullets</label>
             <label class="row" style="gap:6px" title="Set OpenCode's small_model to the local model (titles and other small tasks)"><input type="checkbox" id="la-small" ${s.local_ai_small_model ? 'checked' : ''}> OpenCode small tasks</label>
         </div>
         <div class="form-row">
             <label class="field"><span class="lab">Model</span>
                 <select id="la-model"></select>
                 <input type="text" id="la-model-custom" placeholder="custom model / not installed yet"
                        style="margin-top:8px;display:none"></label>
             <label class="field"><span class="lab">Endpoint <span class="muted">(empty = Ollama /v1)</span></span>
                 <input type="text" id="la-endpoint" value="${esc(s.local_ai_base_url || '')}" placeholder="http://127.0.0.1:11434/v1"></label>
             <label class="field"><span class="lab">If the model is missing</span>
                 <select id="la-policy">
                     <option value="auto" ${(s.local_ai_download || 'auto') === 'auto' ? 'selected' : ''}>download automatically</option>
                     <option value="manual" ${s.local_ai_download === 'manual' ? 'selected' : ''}>I'll set it up myself</option>
                 </select></label>
         </div>
         <p class="meta" id="la-status">checking the local runtime…</p>
         <div id="la-progress"></div>
         <div id="la-commands"></div>
         <div class="form-actions">
             <button class="btn primary" id="la-save">Save local AI</button>
             <button class="btn ghost" id="la-prepare">Prepare</button>
             <button class="btn ghost" id="la-test">Test</button>
             <button class="btn ghost" id="la-remove">Remove model</button>
         </div>`);

    const renderCommands = (host, commands) => {
        if (!commands || !commands.length) { host.innerHTML = ''; return; }
        host.innerHTML = '<p class="meta">Run this in a terminal, then press Prepare again:</p>' +
            commands.map((c) => `<div class="row" style="gap:8px;align-items:center">
                <code class="mono">${esc(c)}</code>
                <button class="btn sm ghost" data-copy="${esc(c)}">Copy</button></div>`).join('');
        host.querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click', async () => {
            try { await navigator.clipboard.writeText(b.dataset.copy); toast('Copied', 'ok'); }
            catch (e) { toast('copy failed', 'err'); }
        }));
    };

    const modelSelectEl = card.querySelector('#la-model');
    const modelCustom = card.querySelector('#la-model-custom');
    const modelValue = () => (modelSelectEl.value === '__custom__'
        ? modelCustom.value.trim()
        : (modelSelectEl.value || ''));

    const renderModelOptions = (st) => {
        const current = ((state.settings || {}).local_ai_model || '').trim();
        const models = (st && st.models) || [];
        const options = ['<option value="">(choose a model)</option>'];
        for (const m of models) {
            const label = `${m.name}${m.size ? ' — ' + fmtBytes(m.size) : ''}`;
            options.push(`<option value="${esc(m.name)}">${esc(label)}</option>`);
        }
        if (current && !models.some((m) => m.name === current)) {
            options.push(`<option value="${esc(current)}">${esc(current)} (not installed)</option>`);
        }
        options.push('<option value="__custom__">Custom / not installed…</option>');
        modelSelectEl.innerHTML = options.join('');
        modelSelectEl.value = current || '__custom__';
        modelCustom.value = models.some((m) => m.name === current) ? '' : (current || '');
        modelCustom.style.display = modelSelectEl.value === '__custom__' ? '' : 'none';
    };

    modelSelectEl.addEventListener('change', () => {
        const custom = modelSelectEl.value === '__custom__';
        modelCustom.style.display = custom ? '' : 'none';
        if (custom) modelCustom.focus();
    });

    const render = async () => {
        let st = {};
        try { st = await fetchLocalAiStatus(); } catch (e) { st = { error: e.message }; }
        const badge = card.querySelector('#la-badge');
        const line = card.querySelector('#la-status');
        if (st.error) {
            badge.textContent = 'unknown'; badge.className = 'badge';
            line.textContent = 'could not read the local AI status: ' + st.error;
            return st;
        }
        badge.textContent = st.enabled ? (st.installed ? 'ready' : 'model missing') : 'off';
        badge.className = 'badge ' + (st.enabled ? (st.installed ? 'green' : 'gold') : '');
        if (st.available) {
            line.innerHTML = `Runtime at <span class="mono">${esc(st.endpoint || '')}</span> · model
                <b>${esc(st.model || '')}</b> ${st.installed ? `installed (${fmtBytes(st.size_bytes)})` : '<b>not installed</b>'}
                · free disk ${fmtBytes(st.disk_free_bytes)}`;
        } else {
            line.innerHTML = `<b>Local runtime not reachable</b> at <span class="mono">${esc(st.endpoint || '')}</span>
                — start Ollama, or point the endpoint at LM Studio / vLLM.`;
        }
        const endpoint = st.endpoint || '';
        if (endpoint && !/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(?=[:/]|$)/.test(endpoint)) {
            line.innerHTML += `<p class="meta warn-text" style="margin:4px 0 0">
                Remote endpoint: prompts from the local-AI consumers leave this machine.</p>`;
        }
        renderModelOptions(st);
        return st;
    };

    const save = async () => {
        try {
            await api('/api/settings', { method: 'PATCH', body: {
                local_ai_enabled: card.querySelector('#la-enabled').checked,
                local_ai_shadow: card.querySelector('#la-shadow').checked,
                local_ai_docs: card.querySelector('#la-docs').checked,
                local_ai_small_model: card.querySelector('#la-small').checked,
                local_ai_model: modelValue(),
                local_ai_base_url: card.querySelector('#la-endpoint').value.trim(),
                local_ai_download: card.querySelector('#la-policy').value,
            } });
            await loadSettings();
            localAiStatusCache = { ts: 0, data: null };
            toast('Local AI settings saved', 'ok');
            await render();
        } catch (e) { toast(e.message, 'err'); }
    };

    card.querySelector('#la-save').addEventListener('click', async () => {
        const st = localAiStatusCache.data || {};
        const wantsSmall = card.querySelector('#la-small').checked;
        if (wantsSmall && st.opencode_had_comments) {
            confirmDialog('Rewrite opencode.json',
                'Your OpenCode config contains comments. Saving will rewrite the file '
                + 'without them (a timestamped backup is kept next to it). Continue?',
                save, 'Save anyway');
            return;
        }
        await save();
    });

    card.querySelector('#la-prepare').addEventListener('click', async () => {
        const box = card.querySelector('#la-progress');
        const cmds = card.querySelector('#la-commands');
        cmds.innerHTML = '';
        box.innerHTML = '';
        try {
            const res = await api('/api/local-ai/prepare', { method: 'POST', body: { model: modelValue() } });
            if (res.manual) {
                renderCommands(cmds, res.commands || []);
                toast('Manual setup — run the command below', 'ok');
                return;
            }
            if (res.busy) { toast('A download is already running', 'err'); return; }
            if (!res.ok) { renderCommands(cmds, res.commands || []); toast(res.reason || 'could not start', 'err'); return; }
            if (res.installed) { toast('Model already installed', 'ok'); await render(); return; }
            const job = res.job;
            if (!job) { await render(); return; }
            box.innerHTML = `<p class="meta">downloading ${esc(job.model)}…</p>
                <progress max="100" value="0" style="width:100%"></progress>`;
            const bar = box.querySelector('progress');
            const timer = setInterval(async () => {
                if (!document.body.contains(card)) { clearInterval(timer); return; }
                try {
                    const r = await api(`/api/local-ai/jobs/${job.id}`);
                    const j = r.job || {};
                    bar.value = j.total ? Math.round(100 * j.completed / j.total) : 0;
                    if (j.state === 'done' || j.state === 'failed' || j.state === 'cancelled') {
                        clearInterval(timer);
                        localAiStatusCache = { ts: 0, data: null };
                        box.innerHTML = j.state === 'done'
                            ? '<p class="meta">installed</p>'
                            : `<p class="meta">${esc(j.error || j.state)}</p>`;
                        await render();
                        toast(j.state === 'done' ? 'Model installed' : 'Download failed',
                            j.state === 'done' ? 'ok' : 'err');
                    }
                } catch (e) { clearInterval(timer); toast(e.message, 'err'); }
            }, 1000);
        } catch (e) { toast(e.message, 'err'); }
    });

    card.querySelector('#la-test').addEventListener('click', async () => {
        try {
            const r = await api('/api/local-ai/test', { method: 'POST', body: {} });
            toast(r.ok ? `answered in ${r.latency_ms} ms: ${r.reply}` : 'no answer — check the endpoint and model',
                r.ok ? 'ok' : 'err');
        } catch (e) { toast(e.message, 'err'); }
    });

    card.querySelector('#la-remove').addEventListener('click', () => {
        const model = modelValue();
        if (!model) { toast('Set a model first', 'err'); return; }
        confirmDialog('Remove local model',
            `Delete ${model} from the local runtime? This frees disk space and cannot be undone.`,
            async () => {
                try {
                    const r = await api(`/api/local-ai/models/${encodeURIComponent(model)}`, { method: 'DELETE' });
                    toast(r.ok ? 'Model removed' : (r.reason || 'could not remove'), r.ok ? 'ok' : 'err');
                    await render();
                } catch (e) { toast(e.message, 'err'); }
            }, 'Remove');
    });

    // OpenCode's small_model is provider-specific. With a custom endpoint Perun
    // cannot know the provider id, so the toggle is disabled instead of silently
    // doing nothing.
    const endpointInput = card.querySelector('#la-endpoint');
    const smallToggle = card.querySelector('#la-small');
    const syncSmall = () => {
        const custom = endpointInput.value.trim() !== '';
        smallToggle.disabled = custom;
        if (custom) smallToggle.checked = false;
        smallToggle.title = custom
            ? "OpenCode's small_model needs the Ollama provider; clear the endpoint to use it"
            : "Set OpenCode's small_model to the local model (titles and other small tasks)";
    };
    endpointInput.addEventListener('input', syncSmall);
    syncSmall();

    render();
    return card;
}

function contextCard() {
    const s = state.settings || {};
    const card = el('div', { class: 'card', id: 'context-card' },
        `<div class="card-head"><h4>Context</h4><span class="badge" id="cx-badge">—</span></div>
         <p class="secondary">Boundary compaction for long sessions. OpenCode already compacts on overflow;
         the governor asks it to compact <b>earlier</b>, when a session grows past the threshold. Compaction
         rewrites the conversation and resets the provider's prompt cache, so it runs at most once per
         cooldown and never on an active session.</p>
         <div class="row wrap" style="gap:18px;margin:6px 0 4px">
             <label class="row" style="gap:6px"><input type="checkbox" id="cx-enabled" ${s.context_governor_enabled ? 'checked' : ''}> enable context governor</label>
             <label class="row" style="gap:6px" title="Log what would be compacted without calling OpenCode"><input type="checkbox" id="cx-shadow" ${s.context_governor_shadow !== false ? 'checked' : ''}> shadow (log only)</label>
         </div>
         <div class="form-row">
             <label class="field"><span class="lab">Compact above (tokens)</span>
                 <input type="number" id="cx-tokens" min="8000" step="1000" value="${esc(s.context_governor_tokens ?? 150000)}"></label>
             <label class="field"><span class="lab">Cooldown (messages)</span>
                 <input type="number" id="cx-turns" min="0" value="${esc(s.context_governor_turns ?? 100)}"></label>
             <label class="field"><span class="lab">Idle window (minutes)</span>
                 <input type="number" id="cx-idle" min="0" value="${esc(s.context_governor_idle_minutes ?? 30)}"></label>
             <label class="field"><span class="lab">Max per day</span>
                 <input type="number" id="cx-max" min="0" value="${esc(s.context_governor_max_per_day ?? 6)}"></label>
         </div>
         <div class="form-row">
             <label class="field"><span class="lab">Search output budget (chars)</span>
                 <input type="number" id="cx-tool" min="1000" value="${esc(s.context_tool_budget_chars ?? 20000)}"></label>
             <label class="field"><span class="lab">Memory facts budget (chars)</span>
                 <input type="number" id="cx-memory" min="1000" value="${esc(s.context_memory_budget_chars ?? 6000)}"></label>
         </div>
         <p class="meta" id="cx-last">no pass yet</p>
         <div id="cx-events"><p class="meta">loading recent compactions…</p></div>
         <div class="form-actions"><button class="btn primary" id="cx-save">Save context</button></div>`);

    const renderEvents = async () => {
        const host = card.querySelector('#cx-events');
        try {
            const st = await fetchLocalAiStatus();
            const gov = (st && st.governor) || {};
            const badge = card.querySelector('#cx-badge');
            badge.textContent = gov.enabled ? (gov.shadow ? 'shadow' : 'on') : 'off';
            badge.className = 'badge ' + (gov.enabled ? (gov.shadow ? 'gold' : 'green') : '');
            const last = gov.last || {};
            const lastEl = card.querySelector('#cx-last');
            if (last.ts) {
                const when = String(last.ts).slice(0, 16).replace('T', ' ');
                lastEl.textContent = `${when}: checked ${last.checked || 0} · compacted ${last.compacted || 0}`
                    + ` · would compact ${last.would || 0}${gov.shadow ? ' (shadow)' : ''}`;
            } else {
                lastEl.textContent = 'no pass yet';
            }
            const events = gov.events || [];
            if (!events.length) {
                host.innerHTML = '<p class="meta">No compactions recorded yet.</p>';
                return;
            }
            host.innerHTML = `<table class="table"><thead><tr><th>When</th><th>Session</th><th>Action</th>
                <th>Context before → after</th><th>Cost delta</th><th>Note</th></tr></thead>
                <tbody>${events.map((e) => `<tr>
                    <td class="mono">${esc(String(e.created_at || '').slice(0, 16).replace('T', ' '))}</td>
                    <td class="mono">#${esc(String(e.session_pk ?? '—'))}</td>
                    <td>${e.action === 'would-compact' ? 'would compact' : 'compacted'}</td>
                    <td class="mono">${e.tokens_before ? fmtTokens(e.tokens_before) + ' tok' : '—'} →
                        ${e.tokens_after ? fmtTokens(e.tokens_after) + ' tok' : '…'}</td>
                    <td class="mono">${e.cost_after !== undefined && e.cost_after !== null && Number(e.cost_after) > 0
                        ? '$' + (Number(e.cost_after) - Number(e.cost_before || 0)).toFixed(2) : '…'}</td>
                    <td>${esc(e.note || '')}</td></tr>`).join('')}</tbody></table>`;
        } catch (e) {
            host.innerHTML = `<p class="meta">${esc(e.message)}</p>`;
        }
    };

    card.querySelector('#cx-save').addEventListener('click', async () => {
        try {
            await api('/api/settings', { method: 'PATCH', body: {
                context_governor_enabled: card.querySelector('#cx-enabled').checked,
                context_governor_shadow: card.querySelector('#cx-shadow').checked,
                context_governor_tokens: Number(card.querySelector('#cx-tokens').value),
                context_governor_turns: Number(card.querySelector('#cx-turns').value),
                context_governor_idle_minutes: Number(card.querySelector('#cx-idle').value),
                context_governor_max_per_day: Number(card.querySelector('#cx-max').value),
                context_tool_budget_chars: Number(card.querySelector('#cx-tool').value),
                context_memory_budget_chars: Number(card.querySelector('#cx-memory').value),
            } });
            await loadSettings();
            localAiStatusCache = { ts: 0, data: null };
            toast('Context settings saved', 'ok');
            await renderEvents();
        } catch (e) { toast(e.message, 'err'); }
    });

    renderEvents();
    return card;
}

async function viewSettings() {
    $('#topbar-title').textContent = 'Settings';
    await Promise.all([loadSettings(), loadTerminals()]);
    const s = state.settings;
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Configuration</div><h1>Settings</h1>
          <div class="sub">Perun runtime lives at <span class="mono">${esc(state.health ? state.health.home : '~/.perun')}</span>.</div></div>`));

    const card = el('div', { class: 'card' },
        `<label class="field"><span class="lab">Project roots (one per line)</span>
            <textarea id="s-roots" rows="4">${esc((s.roots || []).join('\n'))}</textarea></label>
         <div class="form-row">
            <label class="field"><span class="lab">Host</span><input type="text" id="s-host" value="${esc(s.host)}"></label>
            <label class="field"><span class="lab">Port</span><input type="number" id="s-port" value="${esc(s.port)}"></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">Default model</span><span id="s-model-host"></span></label>
            <label class="field"><span class="lab">Default agent</span><span id="s-agent-host"></span></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">Embed model</span><select id="s-embed"><option value="nomic-embed-text" ${s.embed_model === 'nomic-embed-text' ? 'selected' : ''}>nomic-embed-text</option><option value="mxbai-embed-large" ${s.embed_model === 'mxbai-embed-large' ? 'selected' : ''}>mxbai-embed-large</option><option value="all-minilm" ${s.embed_model === 'all-minilm' ? 'selected' : ''}>all-minilm</option><option value="" ${!s.embed_model ? 'selected' : ''}>Custom…</option></select>
                <input type="text" id="s-embed-custom" value="${esc((s.embed_model && !['nomic-embed-text','mxbai-embed-large','all-minilm'].includes(s.embed_model)) ? s.embed_model : '')}" placeholder="custom embed model" style="margin-top:8px"></label>
            <label class="field"><span class="lab">Terminal / shell</span><select id="s-terminal"><option value="">${esc(state.terminals && state.terminals.configured_source === 'os' ? 'System default' : 'Custom path...')}</option>${(state.terminals && state.terminals.terminals || []).map((t) => `<option value="${esc(t.id)}" ${t.id === (s.terminal || state.terminals.configured) ? 'selected' : ''}>${esc(t.name)} — ${esc(t.path)}</option>`).join('')}</select>
                <input type="text" id="s-terminal-custom" value="${esc((s.terminal && !(state.terminals && state.terminals.terminals || []).some((tt) => tt.id === s.terminal)) ? s.terminal : '')}" placeholder="custom terminal path" style="margin-top:8px"></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">Max concurrent runs</span><input type="number" id="s-max" value="${esc(s.max_concurrent_runs)}"></label>
            <label class="field"><span class="lab">Run timeout (s)</span><input type="number" id="s-timeout" value="${esc(s.run_timeout)}"></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">External claim TTL (s) <span class="muted">(how long an rw participant holds a claimed proposal)</span></span><input type="number" id="s-claim-ttl" min="1" max="604800" value="${esc(s.external_execution_claim_ttl ?? 3600)}"></label>
            <label class="field"><span class="lab">Package slot idle TTL (s) <span class="muted">(reclaim a stalled upload slot)</span></span><input type="number" id="s-package-ttl" min="60" value="${esc(s.external_execution_package_ttl ?? 1800)}"></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">Ollama host</span><input type="text" id="s-ollama" value="${esc(s.ollama_host)}"></label>
            <label class="field"><span class="lab">Owner name <span class="muted">(shown on proposal approvals)</span></span><input type="text" id="s-owner" value="${esc(s.perun_owner_name || '')}" placeholder="owner"></label>
            <label class="field"><span class="lab">Provider budgets <span class="muted">(provider=USD per month)</span></span><input type="text" id="s-budgets" value="${esc(s.provider_budgets || '')}" placeholder="ollama-cloud=20, opencode=50"></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">HITL secret <span class="muted">(approval-token signing key)</span> <span id="hitl-secret-badge" class="badge ${s.perun_hitl_secret_set ? 'green' : ''}">${s.perun_hitl_secret_set ? 'set' : 'not set'}</span></span><input type="password" id="s-hitl-secret" value="" placeholder="leave blank to keep the current secret"></label>
            <label class="field"><span class="lab">HITL secret source</span><span class="mono" style="font-size:.78rem">PERUN_HITL_SECRET env var, then this field, then a hash of PERUN_HOME. If PERUN_PASSWORD is set, the value saved here is encrypted in config.json.</span></label>
         </div>
         <div class="row wrap" style="gap:18px;margin:6px 0 4px">
            <label class="row" style="gap:6px"><input type="checkbox" id="s-autodisc" ${s.auto_discover_on_start ? 'checked' : ''}> auto-discover on start</label>
            <label class="row" style="gap:6px"><input type="checkbox" id="s-indexdisc" ${s.index_docs_on_discover ? 'checked' : ''}> index docs on discover</label>
            <label class="row" style="gap:6px" title="Resolve &quot;run tests in perun&quot; / &quot;status&quot; / &quot;zapamietaj...&quot; locally instead of spending an agent run. Deterministic rules only."><input type="checkbox" id="s-routing" ${s.routing_enabled ? 'checked' : ''}> natural-language routing</label>
            <label class="row" style="gap:6px" title="Log what routing would have done without acting on it. Use this for a week before turning routing on."><input type="checkbox" id="s-routing-shadow" ${s.routing_shadow ? 'checked' : ''}> routing: log only (shadow)</label>
            <label class="row" style="gap:6px" title="Push anything that needs a human decision (permission requests, forms, agent questions, proposals) to your paired Telegram chats, and deliver the reply back to that exact session."><input type="checkbox" id="s-attention" ${s.attention_enabled ? 'checked' : ''}> operator inbox (Telegram)</label>
            <label class="row" style="gap:6px" title="Log what would be sent, send nothing. Turn this off once the cards look right."><input type="checkbox" id="s-attention-shadow" ${s.attention_shadow ? 'checked' : ''}> inbox: log only (shadow)</label>
            <label class="row" style="gap:6px" title="A room message that needs you waits this long, so a burst becomes one card per project (up to five items). 0 = send every card immediately. Permissions, forms, proposals and agent questions are never delayed.">inbox: group a burst for <input type="number" min="0" id="s-digest" style="width:5.5em" value="${esc(s.attention_digest_seconds ?? 0)}"> s</label>
            <label class="row" style="gap:6px" title="After an approved proposal is executed, Perun runs the installed /update-docs command in that project so CHANGELOG, CODEMAP and TECHNICAL follow the code. The executor is sandboxed to its targets and cannot write docs itself."><input type="checkbox" id="s-docs" ${s.docs_update_after_execution !== false ? 'checked' : ''}> docs: run /update-docs after an executed proposal</label>
         </div>
         <div class="form-actions">
            <button class="btn primary" id="s-save">Save settings</button>
            <button class="btn ghost" id="s-discover">⟳ Discover now</button>
            <button class="btn ghost" id="s-indexall">▤ Index all docs</button>
         </div>`);
    v.appendChild(card);
    v.appendChild(localAiCard());
    v.appendChild(contextCard());

    const routingPanel = el('div', { class: 'card' },
        `<div class="card-head"><h4>Routing decisions</h4><span class="badge" id="routing-mode">—</span></div>
         <div id="routing-panel"><div class="meta">loading…</div></div>`);
    v.appendChild(routingPanel);
    loadRoutingPanel();

    const attentionLink = el('div', { class: 'card' },
        `<div class="card-head"><h4>Needs you</h4></div>
         <p class="secondary">Questions, permissions, forms, proposals and handoffs waiting on you live in the dedicated inbox.</p>
         <a class="btn sm" href="#/attention">Open Needs you</a>`);
    v.appendChild(attentionLink);

    await loadModels();
    await loadAgents();
    const msDefault = modelSelect({ value: s.default_model || '', placeholder: 'provider/model' });
    card.querySelector('#s-model-host').appendChild(msDefault);
    const sAgent = agentSelect({ value: s.default_agent || '', placeholder: '(none)' });
    card.querySelector('#s-agent-host').appendChild(sAgent);
    const sOrchAgent = agentSelect({ value: s.orchestrator_agent || '', placeholder: 'perun-orchestrator (default)' });

    // ---- orchestrator (control plane) ----
    v.appendChild(sectionTitle('Orchestrator'));
    const orchCard = el('div', { class: 'card' },
        `<div class="card-head"><h4>Perun Orchestrator</h4><span class="badge gold">machine-wide</span></div>
         <p class="secondary">The orchestrator is the agent you talk to when <b>no project is selected</b> — from the <a href="#/orchestrator">Orchestrator chat</a> or Telegram. Its model and agent apply machine-wide; add custom instructions in the <b>Agent instructions</b> files below with scope <b>orchestrator</b>.</p>
         <div class="form-row">
            <label class="field"><span class="lab">Orchestrator model</span><span id="s-orch-model-host"></span></label>
            <label class="field"><span class="lab">Orchestrator agent</span><span id="s-orch-agent-host"></span></label>
         </div>
         <p class="meta" id="s-orch-agent-status">checking the agent file…</p>
         <div class="form-actions">
            <button class="btn primary" id="s-orch-save">Save orchestrator</button>
            <button class="btn ghost" id="s-orch-open">▶ Open orchestrator chat</button>
            <button class="btn ghost" id="s-orch-reset" title="Restore Perun's shipped orchestrator agent template. Any edits you made to the agent file are replaced.">↺ Reset agent file</button>
         </div>`);
    v.appendChild(orchCard);
    const msOrch = modelSelect({ value: s.orchestrator_model || '', placeholder: 'inherit default model' });
    orchCard.querySelector('#s-orch-model-host').appendChild(msOrch);
    orchCard.querySelector('#s-orch-agent-host').appendChild(sOrchAgent);

    async function loadOrchestratorAgentStatus() {
        const line = orchCard.querySelector('#s-orch-agent-status');
        try {
            const info = await api('/api/agents/orchestrator');
            const id = info.agent_id || 'perun-orchestrator';
            if (!info.installed) {
                line.innerHTML = `<b>${esc(id)}</b> is not installed yet — created on the next orchestrator run or <span class="mono">python -m perun install</span>.`;
            } else if (info.customized) {
                line.innerHTML = `<b>${esc(id)}</b> is <b>customized</b> — your edits are kept. <span class="mono">${esc(info.path || '')}</span>`;
            } else {
                line.innerHTML = `<b>${esc(id)}</b> installed (template v${esc(String(info.installed_version || info.template_version || 1))}).`;
            }
        } catch (e) {
            line.textContent = 'could not read the agent file state';
        }
    }
    loadOrchestratorAgentStatus();

    orchCard.querySelector('#s-orch-reset').addEventListener('click', () => confirmDialog(
        'Reset orchestrator agent',
        'Replace the orchestrator agent file with Perun\'s shipped template? Any edits you made to it are lost.',
        async () => {
            try {
                await api('/api/agents/orchestrator/reset', { method: 'POST', body: {} });
                await api('/api/agents?refresh=true');
                toast('Orchestrator agent reset', 'ok');
                await loadOrchestratorAgentStatus();
            } catch (e) { toast(e.message, 'err'); }
        }, 'Reset'));

    orchCard.querySelector('#s-orch-save').addEventListener('click', async () => {
        try {
            await api('/api/settings', { method: 'PATCH', body: {
                orchestrator_model: msOrch.getValue(),
                orchestrator_agent: sOrchAgent.getValue(),
            } });
            toast('Orchestrator saved', 'ok');
            await loadSettings();
        } catch (e) { toast(e.message, 'err'); }
    });
    orchCard.querySelector('#s-orch-open').addEventListener('click', () => goto('#/orchestrator'));

    // ---- global instruction Markdown files ----
    v.appendChild(sectionTitle('Agent instructions'));
    const instrCard = el('div', { class: 'card', id: 'global-instr-card' },
        `<div class="card-head"><h4>Global instruction files</h4><span class="badge gold">machine-wide</span></div>
         <p class="secondary">Markdown files stored under <b>~/.config/opencode/perun/instructions/</b>. Each has a scope (<b>orchestrator</b> = control-plane only, <b>all_projects</b> = every Perun-started session) and an enabled flag.</p>
         <p class="muted" style="font-size:.8rem">Click a filename to open it in your default editor.</p>
         <div id="s-instr-list"></div>
         <hr>
         <h4>Edit file</h4>
         <div class="form-row">
             <label class="field"><span class="lab">Filename</span><input type="text" id="s-instr-filename" placeholder="guidance.md"></label>
             <label class="field"><span class="lab">Name</span><input type="text" id="s-instr-name" placeholder="Display name"></label>
             <label class="field"><span class="lab">Scope</span><select id="s-instr-scope"><option value="all_projects">All projects</option><option value="orchestrator">Orchestrator only</option></select></label>
         </div>
         <label class="field"><span class="lab">Body</span>
             <textarea id="s-instr-body" rows="8" placeholder="# Markdown instructions..."></textarea></label>
         <div class="form-actions">
             <button class="btn primary" id="s-instr-save">Save file</button>
             <button class="btn ghost" id="s-instr-new">New file</button>
         </div>`);
    v.appendChild(instrCard);

    async function refreshInstrList() {
        const list = $('#s-instr-list');
        list.innerHTML = '';
        await loadGlobalInstructionsDir();
        const res = await api('/api/instructions');
        const files = res.instructions || [];
        if (!files.length) {
            list.innerHTML = '<p class="secondary">No instruction files yet. Use the editor below to create one.</p>';
            return;
        }
        const ul = el('ul', { class: 'instr-list' });
        for (const f of files) {
            const fileUrl = instructionFileUrl(f.filename, true);
            const li = el('li', { class: 'row', style: 'gap:12px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border)' },
                `<span class="row" style="gap:6px;flex:1;align-items:center"><input type="checkbox" ${f.enabled ? 'checked' : ''}> <a class="link instr-link" href="${esc(safeUrl(fileUrl))}" title="Click to open in editor">${esc(f.name)}</a> <span class="badge ${f.scope === 'orchestrator' ? 'blue' : 'green'}">${esc(f.scope)}</span></span>
                 <span class="secondary" style="font-size:12px">${esc(f.filename)}</span>
                 <button class="btn ghost" data-edit="${esc(f.filename)}">Edit</button>
                 <button class="btn ghost danger" data-del="${esc(f.filename)}">Delete</button>`);
            const cb = li.querySelector('input');
            cb.addEventListener('change', async () => {
                await api(`/api/instructions/${encodeURIComponent(f.filename)}/toggle`, {
                    method: 'PATCH', body: { enabled: cb.checked }
                });
                toast(`${f.name} ${cb.checked ? 'enabled' : 'disabled'}`, 'ok');
            });
            li.querySelector('[data-edit]').addEventListener('click', async () => {
                const d = await api(`/api/instructions/${encodeURIComponent(f.filename)}`);
                $('#s-instr-filename').value = d.filename;
                $('#s-instr-name').value = d.name;
                $('#s-instr-scope').value = d.scope;
                $('#s-instr-body').value = d.body;
            });
            li.querySelector('[data-del]').addEventListener('click', async () => {
                if (!confirm(`Delete ${f.name}?`)) return;
                await api(`/api/instructions/${encodeURIComponent(f.filename)}`, { method: 'DELETE' });
                refreshInstrList();
                toast(`${f.name} deleted`, 'ok');
            });
            ul.appendChild(li);
        }
        list.appendChild(ul);
    }
    refreshInstrList();

    $('#s-instr-save').addEventListener('click', async () => {
        const filename = $('#s-instr-filename').value.trim() || 'guidance.md';
        try {
            await api('/api/instructions', {
                method: 'POST',
                body: {
                    filename,
                    name: $('#s-instr-name').value.trim(),
                    scope: $('#s-instr-scope').value,
                    body: $('#s-instr-body').value,
                    enabled: true,
                }
            });
            toast('Instruction file saved', 'ok');
            refreshInstrList();
        } catch (e) { toast(e.message, 'err'); }
    });
    $('#s-instr-new').addEventListener('click', () => {
        $('#s-instr-filename').value = '';
        $('#s-instr-name').value = '';
        $('#s-instr-scope').value = 'all_projects';
        $('#s-instr-body').value = '';
    });

    v.appendChild(sectionTitle('Telegram'));
    v.appendChild(el('div', { class: 'card mb' },
        `<p class="secondary">The Telegram bot — its name, API key and chat ids — is configured on the
            <a href="#/telegram">Telegram</a> page, so the bot settings never drift between screens.</p>
         <div class="form-actions"><button class="btn ghost" id="s-telegram">✈ Open Telegram settings</button></div>`));
    $('#s-telegram').addEventListener('click', () => goto('#/telegram'));

    v.appendChild(sectionTitle('Global integration'));
    v.appendChild(el('div', { class: 'card' },
        `<p class="secondary">Install Perun into your user-level OpenCode config so <b>every</b> project inherits the MCP tools, memory, and commands.</p>
         <div class="form-actions"><button class="btn primary" id="s-install">⚙ Install globally</button>
         <button class="btn ghost" id="s-uninstall">Uninstall</button></div>
         <p class="muted mt mono">python -m perun install</p>`));

    $('#s-save').addEventListener('click', async () => {
        const payload = {
            roots: $('#s-roots').value.split('\n').map((x) => x.trim()).filter(Boolean),
            host: $('#s-host').value, port: Number($('#s-port').value),
            default_model: msDefault.getValue(), default_agent: sAgent.getValue(),
            terminal: $('#s-terminal').value || $('#s-terminal-custom').value.trim(),
            max_concurrent_runs: Number($('#s-max').value), run_timeout: Number($('#s-timeout').value),
            external_execution_claim_ttl: Number($('#s-claim-ttl').value),
            external_execution_package_ttl: Number($('#s-package-ttl').value),
            ollama_host: $('#s-ollama').value, embed_model: $('#s-embed').value || $('#s-embed-custom').value.trim(),
            perun_owner_name: $('#s-owner').value.trim(),
            auto_discover_on_start: $('#s-autodisc').checked, index_docs_on_discover: $('#s-indexdisc').checked,
            routing_enabled: $('#s-routing').checked,
            routing_shadow: $('#s-routing-shadow').checked,
            provider_budgets: $('#s-budgets').value.trim(),
            attention_enabled: $('#s-attention').checked,
            attention_shadow: $('#s-attention-shadow').checked,
            docs_update_after_execution: $('#s-docs').checked,
        };
        // An empty field means "leave it as it is"; 0 is a real value (send every
        // card immediately), so it must not be confused with "unset".
        const digestRaw = $('#s-digest').value.trim();
        if (digestRaw !== '' && Number.isFinite(Number(digestRaw))) {
            payload.attention_digest_seconds = Number(digestRaw);
        }
        const hitlSecret = $('#s-hitl-secret').value.trim();
        if (hitlSecret) payload.perun_hitl_secret = hitlSecret;
        try { await api('/api/settings', { method: 'PATCH', body: payload }); toast('Settings saved', 'ok'); }
        catch (e) { toast(e.message, 'err'); }
    });
    $('#s-discover').addEventListener('click', async () => {
        try { const r = await api('/api/discover', { method: 'POST', body: {} }); toast(`Discovered ${(r.created || []).length} new`, 'ok'); await loadProjects(); }
        catch (e) { toast(e.message, 'err'); }
    });
    $('#s-indexall').addEventListener('click', async () => {
        await loadProjects();
        let n = 0;
        for (const p of state.projects) { try { const r = await api(`/api/projects/${p.slug}/index`, { method: 'POST' }); n += r.chunks || 0; } catch (_) {} }
        toast(`Indexed ${n} chunk(s)`, 'ok');
    });
    $('#s-install').addEventListener('click', () => modal('Install globally', el('div', {},
        `<p class="secondary">Run this in a terminal (it edits your user-level OpenCode config and backs it up first):</p>
         <pre class="mono" style="background:var(--bg-inset);padding:12px;border-radius:8px;margin-top:10px">python -m perun install</pre>`),
        [{ label: 'Close', class: 'primary', onClick: closeModal }]));
    $('#s-uninstall').addEventListener('click', () => confirmDialog('Uninstall globally', 'Remove the perun MCP server and commands from your global OpenCode config? Runtime data is kept.', async () => {
        toast('Run `python -m perun install --uninstall` in a terminal', 'ok');
    }, 'OK'));

    // ---- about / updates ----
    v.appendChild(sectionTitle('About Perun'));
    const aboutCard = el('div', { class: 'card' },
        `<p class="secondary">Version <b>v${esc(state.health ? state.health.version : '?')}</b> · ${esc((state.update && state.update.latest) ? `latest: v${state.update.latest}` : 'checking releases…')}</p>
         <div class="form-actions"><button class="btn ghost" id="s-updates">↗ Updates</button></div>`);
    v.appendChild(aboutCard);
    $('#s-updates').addEventListener('click', () => goto('#/updates'));
}

/* ========================================================== BOOT ======= */
function wireChrome() {
    document.querySelectorAll('.nav-item').forEach((n) => n.addEventListener('click', () => goto(n.dataset.route)));
    $('#btn-refresh').addEventListener('click', router);
    $('#btn-update').addEventListener('click', () => goto('#/updates'));
    $('#btn-quick-run').addEventListener('click', () => goto('#/console'));
    $('#btn-shutdown').addEventListener('click', () => {
        confirmDialog('Shut down Perun?',
            'This stops the local Perun service. Restart it with your Perun shortcut or by running python -m perun serve.',
            async () => {
                try {
                    const r = await api('/api/shutdown', { method: 'POST' });
                    if (r.stopped) {
                        toast('Perun is shutting down… restart with your shortcut when ready.', 'ok');
                    } else {
                        toast('Could not signal Perun to shut down. Close the terminal/process manually.', 'err');
                    }
                } catch (e) { toast(e.message, 'err'); }
            },
            'Shut down');
    });
    $('#global-search').addEventListener('submit', (e) => {
        e.preventDefault();
        const q = $('#global-q').value.trim();
        if (!q) return;
        location.hash = '#/memory';
        setTimeout(() => { const i = $('#m-q'); if (i) { i.value = q; $('#m-search').click(); } }, 200);
    });
}

async function refreshHealth() {
    try {
        state.health = await api('/api/health');
        const dot = $('#health-dot'), txt = $('#health-text');
        dot.className = 'dot on';
        txt.textContent = `${state.health.projects} projects · up ${Math.floor(state.health.uptime / 60)}m`;
        $('#home-path').textContent = state.health.home;
    } catch (_) {
        $('#health-dot').className = 'dot off';
        $('#health-text').textContent = 'offline';
    }
}

async function refreshDashboardStats() {
    if (!location.hash.startsWith('#/dashboard')) return;
    try {
        const pr = await api('/api/providers');
        const active = pr.providers || [];
        const connected = active.filter((p) => p.connected).length;
        const total = active.length;
        const elp = $('#dash-stat-providers');
        if (elp) {
            elp.querySelector('.stat-value').textContent = total ? `${connected}/${total}` : '—';
            elp.querySelector('.stat-foot').textContent = total
                ? `${connected} connected, ${total - connected} not connected`
                : (pr.error || 'check opencode auth');
        }
        const mem = await api('/api/memory/stats');
        const stats = mem.stats || {};
        const elf = $('#dash-stat-facts');
        if (elf) elf.querySelector('.stat-value').textContent = stats.facts_total ?? 0;
        const elc = $('#dash-stat-chunks');
        if (elc) elc.querySelector('.stat-value').textContent = stats.chunks_total ?? 0;
        const ele = $('#dash-stat-embeddings');
        if (ele) {
            ele.querySelector('.stat-value').textContent = stats.ollama_ok ? 'Online' : 'Offline';
            ele.querySelector('.stat-foot').textContent = stats.ollama_ok ? 'Ollama ready' : 'start ollama serve';
        }
    } catch (e) {
        // Silent best-effort refresh; the next poll will retry.
    }
}

/* ========================================================= UPDATES ==== */
let _updatePollInterval = null;

async function checkForUpdate(refresh = false) {
    try {
        const info = await api('/api/update/check' + (refresh ? '?refresh=1' : ''));
        state.update = info;
        renderUpdatePill();
        return info;
    } catch (e) {
        // Keep any previous cached state; do not spam errors.
        if (!state.update) state.update = { ok: false, error: e.message };
        renderUpdatePill();
        return state.update;
    }
}

function renderUpdatePill() {
    const btn = $('#btn-update');
    if (!btn) return;
    const info = state.update;
    if (info && info.update_available && info.latest) {
        btn.classList.remove('hidden');
        btn.textContent = `↑ v${info.latest} available`;
        btn.title = `A new Perun release is available: v${info.latest}`;
    } else {
        btn.classList.add('hidden');
    }
}

function updateBanner() {
    const info = state.update;
    if (!info || !info.update_available || !info.latest) return el('div');
    const wrap = el('div', { class: 'update-banner' });
    wrap.appendChild(el('div', { class: 'info' },
        `<strong>Perun v${esc(info.latest)} is available</strong>
         <span class="muted">Current version: v${esc(info.current)}${info.stale ? ' · last check failed' : ''}</span>`));
    const actions = el('div', { class: 'actions' });
    actions.appendChild(el('button', { class: 'btn primary', onclick: () => goto('#/updates') }, 'View update'));
    if (info.stale && info.error) {
        actions.appendChild(el('span', { class: 'muted', title: esc(info.error) }, '(could not reach GitHub)'));
    }
    wrap.appendChild(actions);
    return wrap;
}

async function viewUpdates() {
    $('#topbar-title').textContent = 'Updates';
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Perun</div><h1>Self-update</h1>
         <div class="sub">Check for new releases and install them from the dashboard.</div></div>`));

    const info = await checkForUpdate();
    const status = await api('/api/update/status');
    const job = status.job || {};

    const card = el('div', { class: 'card' });
    card.innerHTML = `
        <div class="card-head"><h4>Current version</h4><span class="badge">v${esc(info.current)}</span></div>
        <div class="form-row">
            <div>
                <div class="stat-label">Installed from</div>
                <div class="mono">${esc((info.mode || {}).label || 'unknown')}</div>
            </div>
            <div>
                <div class="stat-label">Latest release</div>
                <div class="mono">${info.latest ? `v${esc(info.latest)}` : '—'}</div>
            </div>
        </div>
        <p class="secondary" id="update-message">${esc(job.message || infoMessage(info))}</p>
        ${updateAdminNote(info)}
        <div class="form-actions">
            <button class="btn primary" id="btn-check-update">⟳ Check for updates</button>
            <button class="btn ${info.update_available ? 'primary' : 'ghost'}" id="btn-install-update" ${info.update_available ? '' : 'disabled'}>Install update</button>
            <button class="btn ghost" id="btn-dismiss-update" ${job.state === 'done' || job.state === 'failed' ? '' : 'disabled'}>Dismiss</button>
        </div>
        <div class="mt" id="update-progress"></div>
        <div class="mt update-log" id="update-log" style="display:${job.log && job.log.length ? 'block' : 'none'}">${renderUpdateLog(job.log || [])}</div>`;
    v.appendChild(card);

    // What the *running* build contains. Bundled with the package, so this works
    // on an installed machine (the installer excludes docs/).
    let installedNotes = null;
    try {
        installedNotes = (await api('/api/update/notes')).notes || null;
    } catch (_) { installedNotes = null; }
    if (installedNotes && installedNotes.body) {
        v.appendChild(el('div', { class: 'card' },
            `<div class="card-head"><h4>What changed in v${esc(installedNotes.version)}</h4>
             <span class="muted">${esc(installedNotes.date || 'installed release')}</span></div>
             <div class="md-body">${renderMarkdown(installedNotes.body)}</div>`));
    }

    // The GitHub notes are for the *available* release; when Perun is already up
    // to date they would duplicate the card above.
    if (info.update_available && info.release && info.release.notes) {
        v.appendChild(el('div', { class: 'card' },
            `<div class="card-head"><h4>What's new in v${esc(info.latest)}</h4><span class="muted">${esc(timeAgo(info.release.published_at))}</span></div>
             <div class="md-body">${renderMarkdown(info.release.notes)}</div>`));
    }

    $('#btn-check-update').addEventListener('click', async () => {
        $('#btn-check-update').disabled = true;
        $('#update-message').textContent = 'Checking GitHub…';
        await checkForUpdate(true);
        await viewUpdates();
    });
    $('#btn-install-update').addEventListener('click', () => confirmUpdate());
    $('#btn-dismiss-update').addEventListener('click', async () => {
        await api('/api/update/dismiss', { method: 'POST' });
        state.update = null;
        renderUpdatePill();
        await viewUpdates();
    });

    startUpdatePoll();
}

function infoMessage(info) {
    if (!info.ok && info.error) return `Could not check for updates: ${info.error}`;
    if (info.update_available) return `Perun v${info.latest} is available.`;
    return 'Perun is up to date.';
}

// The Windows installer lives in Program Files, so it always needs elevation.
// A dashboard started normally cannot launch it with CreateProcess (Windows
// refuses with "requires elevation" and never shows the UAC prompt); the server
// elevates through the shell at apply time instead. Tell the user up front so
// the prompt is expected rather than surprising.
function updateNeedsAdmin(info) {
    const mode = (info && info.mode) || {};
    return Boolean(mode.requires_admin && !mode.elevated);
}

function updateAdminNote(info) {
    if (!updateNeedsAdmin(info)) return '';
    return `<p class="secondary" id="update-admin-note">Installing an update needs
        administrator rights. The dashboard closes first and Windows then shows a
        permission prompt — approve it to finish, and the dashboard comes back by
        itself. You do not need to start Perun as administrator.</p>`;
}

function confirmUpdate(force = false) {
    const info = state.update;
    if (!info || !info.update_available) return;
    const admin = updateNeedsAdmin(info)
        ? ' Installing the release needs administrator rights: after the dashboard closes, Windows shows a permission prompt — approve it to finish.'
        : '';
    confirmDialog('Install update',
        `This will update Perun from v${info.current} to v${info.latest}. The dashboard will restart automatically when the update finishes.${admin}`,
        () => doUpdate(force),
        'Install update');
}

async function doUpdate(force = false) {
    try {
        const res = await api('/api/update/apply', { method: 'POST', body: { force } });
        $('#update-message').textContent = res.job ? res.job.message : 'Update started…';
        $('#btn-install-update').disabled = true;
        startUpdatePoll();
        toast('Update started — the dashboard will restart shortly', 'ok');
    } catch (e) {
        toast(e.message, 'err');
    }
}

function startUpdatePoll() {
    if (_updatePollInterval) return;
    _updatePollInterval = setInterval(async () => {
        try {
            const st = await api('/api/update/status');
            if (!st) return;
            const job = st.job || {};
            const msg = $('#update-message');
            if (msg) msg.textContent = job.message || '';
            const bar = $('#update-progress');
            if (bar && job.progress > 0) {
                bar.innerHTML = `<div style="background:var(--bg-inset);border-radius:var(--radius-sm);overflow:hidden;height:10px;"><div style="width:${Math.round(job.progress * 100)}%;background:var(--gold-light);height:100%;transition:width .2s;"></div></div>`;
            }
            const logBox = $('#update-log');
            if (logBox && job.log && job.log.length) {
                logBox.style.display = 'block';
                logBox.innerHTML = renderUpdateLog(job.log);
            }
            if (job.state === 'done') {
                stopUpdatePoll();
                // Guard against an infinite reload loop: only auto-reload once
                // per update. If we already reloaded, dismiss the finished job
                // server-side so a later page load cannot loop again.
                if (sessionStorage.getItem('perun_update_reloaded')) {
                    sessionStorage.removeItem('perun_update_reloaded');
                    try {
                        await api('/api/update/dismiss', { method: 'POST' });
                    } catch (_) {
                        // Ignore: the important thing is that we stop reloading.
                    }
                    return;
                }
                toast(`Updated to v${esc(job.to_version)} — refreshing`, 'ok');
                sessionStorage.setItem('perun_update_reloaded', '1');
                setTimeout(() => location.reload(), 1200);
            } else if (job.state === 'failed') {
                stopUpdatePoll();
                toast(`Update failed: ${job.error || job.message}`, 'err');
                const btn = $('#btn-install-update');
                if (btn) btn.disabled = false;
            }
        } catch (e) {
            // Server may be restarting — keep polling for a while.
            const msg = $('#update-message');
            if (msg && msg.textContent !== 'Waiting for Perun to come back…') {
                msg.textContent = 'Waiting for Perun to come back…';
            }
        }
    }, 1200);
}

function stopUpdatePoll() {
    if (_updatePollInterval) { clearInterval(_updatePollInterval); _updatePollInterval = null; }
}

function renderUpdateLog(lines) {
    return lines.map((l) => {
        let stamp = '';
        try { stamp = l.at ? l.at.split('T')[1].split('.')[0] : ''; } catch (_) {}
        return `<div class="log-line ${esc(l.level || 'info')}">${esc(stamp)} ${esc(l.line)}</div>`;
    }).join('');
}

let _unlockPromptDismissed = false;

async function promptForUnlockPassword(force = false) {
    if (!force && _unlockPromptDismissed) return;
    _unlockPromptDismissed = false;
    let status;
    try { status = await api('/api/telegram/status'); } catch (e) { return; }
    if (!status || status.token_source !== 'settings-encrypted' || status.configured) return;

    const isWindows = status.platform === 'win32';
    const rememberHtml = isWindows
        ? `<label class="field row" style="gap:8px;align-items:center;margin-top:8px" title="Encrypts the password with Windows DPAPI and stores it for this user, so future launches skip this prompt.">
               <input type="checkbox" id="unlock-remember">
               <span class="muted">Remember on this PC</span>
           </label>`
        : `<p class="muted mt">Remember is only available on Windows. Set PERUN_PASSWORD in your environment to skip this prompt.</p>`;
    const body = el('div', {}, `
        <p class="secondary">The Telegram bot token in <span class="mono">config.json</span> is encrypted.
           Enter the password to enable the bridge.</p>
        <label class="field"><span class="lab">Password</span>
            <input type="password" id="unlock-password" autocomplete="off" placeholder="Perun encryption password"></label>
        ${rememberHtml}
        <p class="muted mt" id="unlock-error" style="color:var(--red);min-height:1.2em"></p>
    `);
    const submit = async () => {
        const pw = $('#unlock-password').value;
        const rememberBox = $('#unlock-remember');
        const remember = rememberBox ? rememberBox.checked : false;
        const err = $('#unlock-error');
        if (!pw) { err.textContent = 'Password is required.'; return; }
        try {
            await api('/api/telegram/unlock', { method: 'POST', body: { password: pw, remember } });
            closeModal();
            toast('Telegram bridge unlocked', 'ok');
            if (location.hash === '#/telegram') router();
        } catch (e) {
            err.textContent = e.message || 'Incorrect password.';
        }
    };
    modal('Unlock Telegram token', body, [
        { label: 'Cancel', class: 'ghost', onClick: () => { _unlockPromptDismissed = true; closeModal(); } },
        { label: 'Unlock', class: 'primary', onClick: submit },
    ]);
    $('#unlock-password').addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    $('#unlock-password').focus();
}

/* ========================================================== COROUTINES ==== */
const CS = window.CoroutineSchedule || {
    humanTrigger: () => 'Manual',
    modeHuman: () => 'Single run',
    triggerSentence: () => '',
    fmtDate: (e) => (e ? new Date(e * 1000).toLocaleString() : '—'),
    countdown: () => '',
    validateStep: () => null,
    presetCron: () => '',
    quickTrigger: (when, time, onceAt) => {
        if (when === 'once') {
            const ms = new Date(onceAt).getTime();
            if (!Number.isFinite(ms) || ms <= Date.now()) return null;
            return { trigger_type: 'once', trigger_expr: '', run_at_epoch: Math.floor(ms / 1000) };
        }
        const [h, m] = String(time || '12:00').split(':').map(Number);
        return { trigger_type: 'cron', trigger_expr: `${m || 0} ${h || 0} * * *` };
    },
    cronHuman: (e) => `Cron ${e}`,
    DAYS: ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'],
};

let coroutinePollTimer = null;
let coroutineFilters = { scope: '', status: '', trigger: '', q: '' };
let _coroutineRooms = null;
let _coroutinePreviewTimer = null;
let _coroutinePreviewSeq = 0;

function stopCoroutinePoll() {
    if (coroutinePollTimer) { clearInterval(coroutinePollTimer); coroutinePollTimer = null; }
    if (_coroutinePreviewTimer) { clearTimeout(_coroutinePreviewTimer); _coroutinePreviewTimer = null; }
}

function corPad(n) { return String(n).padStart(2, '0'); }

function corStateBadge(status) {
    const map = { active: 'green', paused: 'amber', stopped: 'muted', completed: 'blue', failed: 'red' };
    return `<span class="badge ${map[status] || 'muted'}">${esc(status || 'unknown')}</span>`;
}

function corProjectLabel(job) {
    if (job.scope === 'global') return `<span class="badge gold">global</span>`;
    return `<span class="badge muted">${esc(job.project_name || job.execution_project_name || 'project')}</span>`;
}

function corRunBadge(run) {
    if (!run) return '<span class="badge muted">never run</span>';
    const map = { done: 'green', running: 'amber', queued: 'muted', failed: 'red', timeout: 'red', cancelled: 'muted', interrupted: 'red' };
    return `<span class="badge ${map[run.status] || 'muted'}">${esc(run.status)}</span>`;
}

function corNextText(job) {
    if (job.active_run) return `running · ${esc(job.active_run.status)}`;
    if (job.trigger_type === 'event') return 'Listening for events';
    if (job.trigger_type === 'manual') return 'Manual — run on demand';
    if (job.status === 'paused') return 'Paused';
    if (job.status === 'stopped') return 'Stopped';
    if (job.status === 'failed') return 'Paused after failures';
    if (job.is_unlimited) return 'Worker — running until stopped';
    const next = (job.next_fires || [])[0];
    if (!next) return 'No upcoming fire';
    return `Next ${esc(CS.fmtDate(next.epoch, job.timezone))} · ${esc(CS.countdown(next.epoch))}`;
}

function corDuration(run) {
    if (!run || !run.started_at) return '—';
    const start = new Date(run.started_at).getTime();
    const end = run.ended_at ? new Date(run.ended_at).getTime() : Date.now();
    if (!Number.isFinite(start) || !Number.isFinite(end)) return '—';
    const s = Math.max(0, Math.round((end - start) / 1000));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
}

async function coroutineCall(uid, action, refresh, opts = {}) {
    try {
        if (action === 'run') {
            try {
                await api(`/api/coroutines/${uid}/run`, { method: 'POST', body: { force: false } });
            } catch (e) {
                if (e.status !== 409) throw e;
                if (!confirm('This coroutine is already running. Stop that run and start a new one?')) return;
                await api(`/api/coroutines/${uid}/run`, { method: 'POST', body: { force: true } });
            }
            toast('Coroutine run started', 'ok');
        } else if (action === 'pause') {
            await api(`/api/coroutines/${uid}/pause`, { method: 'POST', body: {} });
            toast('Coroutine paused', 'ok');
        } else if (action === 'resume') {
            await api(`/api/coroutines/${uid}/resume`, { method: 'POST', body: {} });
            toast('Coroutine resumed', 'ok');
        } else if (action === 'stop') {
            const res = await api(`/api/coroutines/${uid}/stop`, { method: 'POST', body: {} });
            toast(res.cancelled ? 'Coroutine run stopped' : 'Coroutine stopped', 'ok');
        } else if (action === 'delete') {
            if (!confirm('Delete this coroutine? Its run history is kept unless you purge it from the detail view.')) return;
            await api(`/api/coroutines/${uid}`, { method: 'DELETE' });
            toast('Coroutine deleted', 'ok');
            goto('#/coroutines');
            return;
        } else if (action === 'duplicate') {
            const res = await api(`/api/coroutines/${uid}/duplicate`, { method: 'POST' });
            toast('Coroutine duplicated (paused)', 'ok');
            if (res.coroutine && res.coroutine.webhook_url) showCoroutineWebhook(res.coroutine);
        } else if (action === 'test-event') {
            await api(`/api/coroutines/${uid}/test-event`, { method: 'POST', body: {} });
            toast('Test event sent', 'ok');
        } else if (action === 'rotate-secret') {
            const res = await api(`/api/coroutines/${uid}/rotate-secret`, { method: 'POST' });
            showCoroutineWebhook({ ...(opts.job || {}), webhook_url: res.webhook_url,
                                   webhook_secret: res.webhook_secret });
        }
        if (refresh) await refresh();
    } catch (e) {
        toast(e.message || 'Coroutine action failed', 'err');
    }
}

async function coroutineRooms() {
    if (_coroutineRooms) return _coroutineRooms;
    const rooms = new Set();
    try {
        const res = await api('/api/perunchat/rooms');
        const list = Array.isArray(res) ? res : (res.rooms || []);
        for (const item of list) {
            const id = typeof item === 'string' ? item : (item.room_id || item.id || '');
            if (id) rooms.add(id);
        }
    } catch (_) { /* gateway offline; manual room entry still works */ }
    try {
        const st = await api('/api/perunchat/bridge/status');
        for (const id of (st.rooms || [])) if (id) rooms.add(id);
    } catch (_) { /* ignore */ }
    _coroutineRooms = Array.from(rooms).sort();
    return _coroutineRooms;
}

function showCoroutineWebhook(job) {
    const url = job.webhook_url ? (location.origin + job.webhook_url) : '';
    const secret = job.webhook_secret || '';
    const body = el('div', {},
        `<p class="secondary">Send a JSON object to this URL to trigger <b>${esc(job.name || 'this coroutine')}</b>.</p>
         <p class="muted">The URL is safe to keep in scripts and logs. The secret is shown once and must be sent as the <span class="mono">X-Perun-Webhook-Secret</span> header.</p>
         <label class="field"><span class="lab">URL</span>
           <input class="mono" readonly value="${esc(url)}" style="width:100%" onclick="this.select()"></label>
         ${secret ? `<label class="field"><span class="lab">Secret (shown once)</span>
           <input class="mono" readonly value="${esc(secret)}" style="width:100%" onclick="this.select()"></label>` : ''}
         <p class="muted">Example:<br><span class="mono">curl -X POST -H "X-Perun-Webhook-Secret: &lt;secret&gt;" -H "Content-Type: application/json" -d '{}</span></p>`);
    const actions = [
        {
            label: 'Copy URL', class: 'primary', onClick: async () => {
                try { await navigator.clipboard.writeText(url); toast('URL copied', 'ok'); }
                catch (_) { toast('Select the URL and copy it manually', 'err'); }
            },
        },
    ];
    if (secret) {
        actions.push({
            label: 'Copy secret', class: 'ghost', onClick: async () => {
                try { await navigator.clipboard.writeText(secret); toast('Secret copied', 'ok'); }
                catch (_) { toast('Select the secret and copy it manually', 'err'); }
            },
        });
    }
    actions.push({ label: 'Done', class: 'ghost', onClick: closeModal });
    modal('Webhook URL & secret', body, actions);
}

/* ------------------------------------------------------------ list view -- */
async function viewCoroutines(uid) {
    $('#topbar-title').textContent = 'Coroutines';
    stopCoroutinePoll();
    if (uid) return viewCoroutineDetail(uid);
    await Promise.all([loadProjects(), loadModels(), loadAgents(), loadSettings()]);
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Automation</div><h1>Coroutines</h1>
          <div class="sub">Scheduled and event-triggered agent jobs. Pause, stop, inspect, and control every one.</div></div>
         <div class="row wrap">
           <button class="btn ghost" id="cor-toggle">❚❚ Pause all</button>
           <button class="btn ghost" id="cor-quick">⚡ Quick create</button>
           <button class="btn primary" id="cor-new">＋ New coroutine</button>
         </div>`));
    v.querySelector('#cor-quick').addEventListener('click', openCoroutineQuickCreate);
    v.querySelector('#cor-new').addEventListener('click', () => openCoroutineWizard(null));
    const toggle = v.querySelector('#cor-toggle');
    let globalPaused = false;
    toggle.addEventListener('click', async () => {
        try {
            await api('/api/settings', { method: 'PATCH', body: { coroutines_paused: !globalPaused } });
            toast(globalPaused ? 'Coroutines resumed' : 'All coroutines paused', 'ok');
            await refresh();
        } catch (e) { toast(e.message, 'err'); }
    });

    const alertHost = el('div', { id: 'cor-alert' });
    v.appendChild(alertHost);
    const stats = el('div', { id: 'cor-stats', class: 'grid stats-grid' });
    v.appendChild(stats);

    const filters = el('div', { class: 'card', style: 'margin-bottom:14px' });
    filters.appendChild(el('div', { class: 'row wrap' },
        `<span class="mselect-label">Scope</span>
         <select id="cor-f-scope" style="width:auto"><option value="">Any</option><option value="project">Project</option><option value="global">Global</option></select>
         <span class="mselect-label">Status</span>
         <select id="cor-f-status" style="width:auto"><option value="">Any</option><option>active</option><option>paused</option><option>stopped</option><option>completed</option><option>failed</option></select>
         <span class="mselect-label">Trigger</span>
         <select id="cor-f-trigger" style="width:auto"><option value="">Any</option><option>interval</option><option>cron</option><option>once</option><option>manual</option><option>event</option></select>
         <span class="grow"></span>
         <input id="cor-f-q" type="text" placeholder="Search name…" style="max-width:240px">`));
    v.appendChild(filters);
    const list = el('div', { id: 'cor-list' });
    v.appendChild(list);

    const fScope = filters.querySelector('#cor-f-scope');
    const fStatus = filters.querySelector('#cor-f-status');
    const fTrigger = filters.querySelector('#cor-f-trigger');
    const fQ = filters.querySelector('#cor-f-q');
    fScope.value = coroutineFilters.scope;
    fStatus.value = coroutineFilters.status;
    fTrigger.value = coroutineFilters.trigger;
    fQ.value = coroutineFilters.q;
    const readFilters = () => {
        coroutineFilters = {
            scope: fScope.value, status: fStatus.value,
            trigger: fTrigger.value, q: fQ.value.trim(),
        };
    };
    for (const node of [fScope, fStatus, fTrigger]) {
        node.addEventListener('change', () => { readFilters(); refresh(); });
    }
    fQ.addEventListener('input', () => { readFilters(); refresh(); });

    const refresh = async () => {
        if (!document.getElementById('cor-list')) { stopCoroutinePoll(); return; }
        try {
            const [data, status] = await Promise.all([
                api('/api/coroutines?limit=500'),
                api('/api/coroutines/status').catch(() => null),
            ]);
            if (status) {
                globalPaused = !!status.paused;
                toggle.textContent = globalPaused ? '▶ Resume all' : '❚❚ Pause all';
                toggle.disabled = status.enabled === false;
            }
            renderCoroutineStats(stats, data.counts || {}, status);
            renderCoroutineAlert(alertHost, status);
            const jobs = (data.coroutines || []).filter((j) => {
                if (coroutineFilters.scope && j.scope !== coroutineFilters.scope) return false;
                if (coroutineFilters.status && j.status !== coroutineFilters.status) return false;
                if (coroutineFilters.trigger && j.trigger_type !== coroutineFilters.trigger) return false;
                if (coroutineFilters.q) {
                    const q = coroutineFilters.q.toLowerCase();
                    if (!(`${j.name} ${j.description} ${j.uid}`.toLowerCase().includes(q))) return false;
                }
                return true;
            });
            list.innerHTML = '';
            if (!jobs.length) {
                list.appendChild(el('div', { class: 'empty' },
                    `<p>No coroutines match.</p>
                     <p class="muted">Create one to run an agent every day, on a schedule, or when a PerunChat message arrives.</p>
                     <button class="btn primary" id="cor-empty-new">＋ Create your first coroutine</button>`));
                const b = list.querySelector('#cor-empty-new');
                if (b) b.addEventListener('click', () => openCoroutineWizard(null));
                return;
            }
            for (const job of jobs) list.appendChild(coroutineCard(job, refresh));
        } catch (e) {
            list.innerHTML = `<div class="empty">Could not load coroutines: ${esc(e.message)}</div>`;
        }
    };
    await refresh();
    coroutinePollTimer = setInterval(() => {
        if (document.visibilityState === 'visible' && document.getElementById('cor-list')) refresh();
        else if (!document.getElementById('cor-list')) stopCoroutinePoll();
    }, 5000);
}

function renderCoroutineStats(host, counts, status) {
    if (!host) return;
    const hb = status && status.heartbeat ? status.heartbeat : {};
    host.innerHTML = '';
    const card = (label, value, foot) => el('div', { class: 'card stat' },
        `<div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(String(value))}</div><div class="stat-foot">${esc(foot || '')}</div>`);
    host.appendChild(card('Active', counts.active || 0, 'eligible to fire or listen'));
    host.appendChild(card('Running now', counts.running || 0, 'agent runs in flight'));
    host.appendChild(card('Paused', (counts.paused || 0) + (counts.stopped || 0), 'waiting for you'));
    host.appendChild(card('Failing', counts.failed || 0, 'auto-paused after failures'));
    const next = status && status.next_fire_at ? status.next_fire_at : '';
    host.appendChild(card('Scheduler', hb.stale ? 'stale' : (status && status.scheduler_running ? 'live' : 'off'),
        next ? `next ${next}` : '5 s tick'));
}

function renderCoroutineAlert(host, status) {
    if (!host) return;
    if (!status) { host.innerHTML = ''; return; }
    if (status.enabled === false) {
        host.innerHTML = '<div class="cor-warn">Coroutines are disabled in Settings. Existing jobs will not fire.</div>';
        return;
    }
    if (status.paused) {
        host.innerHTML = '<div class="cor-warn">The scheduler is paused globally (Settings → Coroutines). No new cycles or events will start.</div>';
        return;
    }
    if (status.heartbeat && status.heartbeat.stale) {
        host.innerHTML = '<div class="cor-warn">The coroutine scheduler is not ticking. Check the service log (it should run inside <span class="mono">perun serve</span>).</div>';
        return;
    }
    host.innerHTML = '';
}

function coroutineCard(job, refresh) {
    const card = el('div', { class: 'card cor-card', dataset: { uid: job.uid } });
    const head = el('div', { class: 'cor-head' });
    head.appendChild(el('div', { class: 'grow' },
        `<div class="cor-title"><a href="#/coroutines/${esc(job.uid)}">${esc(job.name)}</a>
          ${corStateBadge(job.status)} ${corProjectLabel(job)}</div>
         <div class="meta">${esc(CS.humanTrigger(job))} · ${esc(CS.modeHuman(job))}</div>`));
    const actions = el('div', { class: 'cor-actions' });
    const add = (label, cls, fn, title) => actions.appendChild(
        el('button', { class: 'btn ' + cls + ' sm', title: title || null, onclick: fn }, label));
    add('▶ Run now', 'primary', () => coroutineCall(job.uid, 'run', refresh));
    if (job.status === 'active') add('❚❚ Pause', 'ghost', () => coroutineCall(job.uid, 'pause', refresh));
    else add('▶ Resume', 'ghost', () => coroutineCall(job.uid, 'resume', refresh));
    if (job.active_run) add('■ Stop', 'danger ghost', () => coroutineCall(job.uid, 'stop', refresh));
    add('Edit', 'ghost', () => openCoroutineWizard(job, { onSaved: refresh }));
    add('⋯', 'ghost', () => coroutineMenu(job, refresh), 'More actions');
    head.appendChild(actions);
    card.appendChild(head);

    const meta = el('div', { class: 'cor-meta' });
    meta.innerHTML =
        `<div><span class="mselect-label">Next</span><div>${esc(corNextText(job))}</div></div>
         <div><span class="mselect-label">Last run</span><div>${corRunBadge(job.last_run)} ${job.last_run ? esc(timeAgo(job.last_run.ended_at || job.last_run.started_at)) : '<span class="muted">never</span>'}</div></div>
         <div><span class="mselect-label">7-day health</span><div>${job.stats && job.stats.total ? `${job.stats.done}/${job.stats.total} ok · ${job.stats.failed} failed` : '<span class="muted">no runs yet</span>'}</div></div>`;
    card.appendChild(meta);

    if (job.active_run) {
        const run = job.active_run;
        const bar = el('div', { class: 'cor-progress' });
        bar.appendChild(el('i', { style: 'width:35%' }));
        card.appendChild(el('div', { class: 'cor-running' },
            `<span class="dot amber"></span> ${esc(run.status)} · iteration ${esc(String(run.coroutine_iteration || 1))} · ${esc(corDuration(run))}`));
        card.appendChild(bar);
    }
    if (job.last_error) {
        card.appendChild(el('div', { class: 'cor-error' }, esc(job.last_error)));
    }
    return card;
}

function coroutineMenu(job, refresh) {
    const body = el('div', {},
        `<p class="secondary">${esc(job.name)}</p>
         <div class="wiz-options">
           ${job.trigger_type === 'event' ? '<button class="btn ghost" id="cor-m-test">Send test event</button>' : ''}
           ${job.trigger_type === 'event' && job.event_source === 'webhook' ? '<button class="btn ghost" id="cor-m-rotate">Rotate webhook secret</button>' : ''}
           <button class="btn ghost" id="cor-m-duplicate">Duplicate</button>
           <button class="btn danger ghost" id="cor-m-delete">Delete</button>
         </div>`);
    const box = modal('Coroutine actions', body, [{ label: 'Close', class: 'ghost', onClick: closeModal }]);
    const bind = (id, fn) => {
        const n = box.querySelector(id);
        if (n) n.addEventListener('click', () => { closeModal(); fn(); });
    };
    bind('#cor-m-test', () => coroutineCall(job.uid, 'test-event', refresh, { job }));
    bind('#cor-m-rotate', () => coroutineCall(job.uid, 'rotate-secret', refresh, { job }));
    bind('#cor-m-duplicate', () => coroutineCall(job.uid, 'duplicate', refresh));
    bind('#cor-m-delete', () => coroutineCall(job.uid, 'delete', refresh));
}

/* ---------------------------------------------------------- detail view -- */
async function viewCoroutineDetail(uid, opts = {}) {
    $('#topbar-title').textContent = 'Coroutine';
    stopCoroutinePoll();
    if (!opts.quiet) {
        await Promise.all([loadProjects(), loadModels(), loadAgents(), loadSettings()]);
    }
    let data;
    try { data = await api(`/api/coroutines/${encodeURIComponent(uid)}`); }
    catch (e) {
        $('#view').innerHTML = `<div class="empty">Could not load this coroutine: ${esc(e.message)}</div>`;
        return;
    }
    const job = data.coroutine;
    const status = await api('/api/coroutines/status').catch(() => null);
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow"><a href="#/coroutines">Coroutines</a> / detail</div>
          <h1>${esc(job.name)} ${corStateBadge(job.status)}</h1>
          <div class="sub">${esc(CS.humanTrigger(job))} · ${esc(CS.modeHuman(job))} · ${esc(job.execution_project_name || job.execution_project_slug || '')}</div></div>
         <div class="row wrap">
           <button class="btn primary sm" id="cor-d-run">▶ Run now</button>
           ${job.status === 'active'
               ? '<button class="btn ghost sm" id="cor-d-pause">❚❚ Pause</button>'
               : '<button class="btn ghost sm" id="cor-d-resume">▶ Resume</button>'}
           ${job.active_run ? '<button class="btn danger ghost sm" id="cor-d-stop">■ Stop</button>' : ''}
           <button class="btn ghost sm" id="cor-d-edit">Edit</button>
           <button class="btn ghost sm" id="cor-d-menu">⋯</button>
         </div>`));

    const refresh = () => viewCoroutineDetail(uid, { quiet: true });
    v.querySelector('#cor-d-run').addEventListener('click', () => coroutineCall(uid, 'run', refresh));
    v.querySelector('#cor-d-pause')?.addEventListener('click', () => coroutineCall(uid, 'pause', refresh));
    v.querySelector('#cor-d-resume')?.addEventListener('click', () => coroutineCall(uid, 'resume', refresh));
    v.querySelector('#cor-d-stop')?.addEventListener('click', () => coroutineCall(uid, 'stop', refresh));
    v.querySelector('#cor-d-edit').addEventListener('click', () => openCoroutineWizard(job, { onSaved: refresh }));
    v.querySelector('#cor-d-menu').addEventListener('click', () => coroutineMenu(job, refresh));

    renderCoroutineAlert(v, status);

    const grid = el('div', { class: 'cor-detail' });
    const main = el('div', {});
    const side = el('div', {});
    grid.appendChild(main);
    grid.appendChild(side);
    v.appendChild(grid);

    main.appendChild(coroutineCurrentCycle(job, refresh));
    main.appendChild(coroutineNextCard(job));
    main.appendChild(coroutineRunsCard(job));
    main.appendChild(coroutineEventsCard(job, refresh));
    side.appendChild(coroutineConfigCard(job));
    side.appendChild(coroutineHealthCard(job));
    if (job.room_outbox && job.room_outbox.length) side.appendChild(coroutineOutboxCard(job, refresh));
    side.appendChild(coroutineDangerCard(job, refresh));

    // Refresh the detail on a slow timer only when no run is streaming; the
    // stream itself calls back when the run ends.
    if (!job.active_run) {
        coroutinePollTimer = setInterval(() => {
            if (!document.getElementById('cor-d-run')) { stopCoroutinePoll(); return; }
            if (document.visibilityState === 'visible') refresh();
        }, 8000);
    }
}

function coroutineCurrentCycle(job, refresh) {
    const card = el('div', { class: 'card' });
    const run = job.active_run;
    card.appendChild(el('div', { class: 'card-head' },
        `<h4>Current cycle</h4><span class="meta">${run ? esc(run.status) : (job.active_cycle_id ? 'settling…' : 'idle')}</span>`));
    if (!run) {
        card.appendChild(el('div', { class: 'muted' },
            job.active_cycle_id
                ? 'The cycle is completing; accounting will schedule the next step.'
                : 'No run is active right now.'));
        return card;
    }
    const meta = el('div', { class: 'cor-run-meta' },
        `Iteration ${esc(String(run.coroutine_iteration || 1))} · started ${esc(timeAgo(run.started_at))} · ${esc(corDuration(run))}`);
    card.appendChild(meta);
    const log = el('pre', { class: 'cor-log' }, '');
    log.textContent = run.output || '';
    card.appendChild(log);
    const stop = el('button', { class: 'btn danger ghost sm', onclick: () => coroutineCall(job.uid, 'stop', refresh) }, '■ Stop run');
    card.appendChild(stop);

    let started = false;
    const stream = () => {
        if (started) return;
        started = true;
        streamOrchestrator(run.run_uid, (ev) => {
            if (ev.type === 'text') {
                log.textContent += ev.text || '';
                log.scrollTop = log.scrollHeight;
            } else if (ev.type === 'error') {
                log.textContent += `\n[error] ${ev.text || ''}`;
                log.scrollTop = log.scrollHeight;
            }
        }, () => {
            stopCoroutinePoll();
            refresh();
        });
    };
    stream();
    return card;
}

function coroutineNextCard(job) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'card-head' },
        `<h4>Next fires</h4><span class="meta">${esc(job.timezone || 'local time')}</span>`));
    const fires = job.next_fires || [];
    if (!fires.length) {
        card.appendChild(el('div', { class: 'muted' },
            job.trigger_type === 'event' ? 'Listening for events — no timed fires.'
                : job.trigger_type === 'manual' ? 'Manual only — use Run now.'
                : 'No upcoming fire.'));
        return card;
    }
    const rows = fires.map((f) =>
        `<div class="cor-next"><span>${esc(CS.fmtDate(f.epoch, job.timezone))}</span><span class="muted">${esc(CS.countdown(f.epoch))}</span></div>`).join('');
    card.appendChild(el('div', {}, rows));
    return card;
}

function coroutineRunsCard(job) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'card-head' },
        `<h4>Run history</h4><span class="meta">last ${esc(String((job.runs || []).length))}</span>`));
    if (!job.runs || !job.runs.length) {
        card.appendChild(el('div', { class: 'muted' }, 'No runs yet.'));
        return card;
    }
    const box = el('div', { class: 'cor-runs' });
    for (const run of job.runs) {
        const row = el('div', { class: 'cor-run' });
        row.innerHTML =
            `<div>${corRunBadge(run)} <span class="mono">${esc(run.run_uid)}</span>
                ${run.coroutine_iteration ? `<span class="chip">iter ${esc(String(run.coroutine_iteration))}</span>` : ''}</div>
             <div class="meta">${esc(timeAgo(run.started_at || run.created_at))} · ${esc(corDuration(run))}
                ${run.coroutine_trigger ? ` · ${esc(run.coroutine_trigger)}` : ''}</div>
             <div class="cor-run-out">${esc((run.output || run.error || '').slice(0, 320))}</div>`;
        box.appendChild(row);
    }
    card.appendChild(box);
    return card;
}

function coroutineEventsCard(job, refresh) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'card-head' },
        `<h4>Event inbox</h4><span class="meta">${esc(String((job.events || []).length))}</span>`));
    if (job.trigger_type !== 'event') {
        card.appendChild(el('div', { class: 'muted' }, 'This coroutine uses a timed trigger.'));
        return card;
    }
    if (!job.events || !job.events.length) {
        card.appendChild(el('div', { class: 'muted' }, 'No events received yet.'));
    } else {
        const box = el('div', { class: 'cor-events' });
        for (const ev of job.events) {
            let payload = {};
            try { payload = JSON.parse(ev.payload || '{}'); } catch (_) { payload = {}; }
            const text = payload.text || payload.intent || payload.question || ev.source;
            const status = ev.delivery_status || 'queued';
            const cls = { fired: 'green', skipped: 'muted', failed: 'red', queued: 'amber' }[status] || 'muted';
            const row = el('div', { class: 'cor-event' },
                `<span class="badge ${cls}">${esc(status)}</span>
                 <span class="grow">${esc(String(text).slice(0, 180))}</span>
                 ${status === 'failed' && ev.delivery_id ? '<button class="btn ghost sm" data-retry>Retry</button>' : ''}
                 <span class="muted">${esc(timeAgo(ev.received_at))}</span>`);
            const retry = row.querySelector('[data-retry]');
            if (retry) retry.addEventListener('click', async () => {
                try {
                    await api(`/api/coroutines/${job.uid}/events/${ev.delivery_id}/retry`, { method: 'POST', body: {} });
                    toast('Event delivery requeued', 'ok');
                    refresh();
                } catch (e) { toast(e.message, 'err'); }
            });
            box.appendChild(row);
        }
        card.appendChild(box);
    }
    const actions = el('div', { class: 'row wrap' });
    actions.appendChild(el('button', { class: 'btn ghost sm', onclick: () => coroutineCall(job.uid, 'test-event', refresh, { job }) }, 'Send test event'));
    if (job.event_source === 'webhook') {
        actions.appendChild(el('button', { class: 'btn ghost sm', onclick: () => coroutineCall(job.uid, 'rotate-secret', refresh, { job }) }, 'Rotate secret'));
    }
    card.appendChild(actions);
    return card;
}

function coroutineConfigCard(job) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'card-head' }, '<h4>Configuration</h4>'));
    const rows = [
        ['Trigger', CS.humanTrigger(job)],
        ['Mode', CS.modeHuman(job)],
        ['Execution project', job.execution_project_name || job.execution_project_slug || '—'],
        ['Scope', job.scope],
        ['Model', job.model || 'project default'],
        ['Agent', job.agent || 'project default'],
        ['Session', job.session_mode],
        ['Timeout', job.run_timeout_seconds ? `${job.run_timeout_seconds}s` : 'global default'],
        ['Failure policy', `${job.failure_policy} after ${job.failure_threshold}`],
        ['Overlap', job.overlap_policy],
        ['Catch-up', job.catch_up],
    ];
    card.appendChild(el('div', { class: 'cor-kv' },
        rows.map(([k, v]) => `<div><span class="mselect-label">${esc(k)}</span><div>${esc(String(v ?? ''))}</div></div>`).join('')));
    card.appendChild(el('div', { class: 'cor-prompt' }, esc((job.prompt || '').slice(0, 600))));
    return card;
}

function coroutineHealthCard(job) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'card-head' }, '<h4>Health</h4>'));
    const runs = job.runs || [];
    const done = runs.filter((r) => r.status === 'done').length;
    const failed = runs.filter((r) => ['failed', 'timeout', 'provider_error'].includes(r.status)).length;
    card.appendChild(el('div', { class: 'cor-kv' },
        `<div><span class="mselect-label">Runs today</span><div>${esc(String(job.runs_today || 0))}</div></div>
         <div><span class="mselect-label">Recent success</span><div>${done}/${done + failed || 0}</div></div>
         <div><span class="mselect-label">Failure streak</span><div>${esc(String(job.failure_streak || 0))}</div></div>
         <div><span class="mselect-label">Last error</span><div>${esc(job.last_error || '—')}</div></div>`));
    return card;
}

function coroutineOutboxCard(job, refresh) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'card-head' }, '<h4>Room deliveries</h4>'));
    const box = el('div', { class: 'cor-events' });
    for (const row of job.room_outbox) {
        const state = row.delivered_at ? 'delivered' : (Number(row.attempts) >= 10 ? 'failed' : 'queued');
        const cls = { delivered: 'green', failed: 'red', queued: 'amber' }[state];
        const line = el('div', { class: 'cor-event' },
            `<span class="badge ${cls}">${esc(state)}</span>
             <span class="grow">${esc(String(row.text || '').slice(0, 160))}</span>
             ${state === 'failed' ? '<button class="btn ghost sm" data-retry>Retry</button>' : ''}`);
        const retry = line.querySelector('[data-retry]');
        if (retry) retry.addEventListener('click', async () => {
            try {
                await api(`/api/coroutines/room-outbox/${row.id}/retry`, { method: 'POST', body: {} });
                toast('Room delivery requeued', 'ok');
                refresh();
            } catch (e) { toast(e.message, 'err'); }
        });
        box.appendChild(line);
    }
    card.appendChild(box);
    return card;
}

function coroutineDangerCard(job, refresh) {
    const card = el('div', { class: 'card' });
    card.appendChild(el('div', { class: 'card-head' }, '<h4>Danger zone</h4>'));
    const row = el('div', { class: 'row wrap' });
    row.appendChild(el('button', {
        class: 'btn danger ghost sm',
        onclick: async () => {
            if (!confirm(`Delete "${job.name}"? Run history is kept.`)) return;
            try {
                await api(`/api/coroutines/${job.uid}`, { method: 'DELETE' });
                toast('Coroutine deleted', 'ok');
                goto('#/coroutines');
            } catch (e) { toast(e.message, 'err'); }
        },
    }, 'Delete coroutine'));
    row.appendChild(el('button', {
        class: 'btn danger sm',
        onclick: async () => {
            if (!confirm(`Purge "${job.name}" and all of its run history? This cannot be undone.`)) return;
            try {
                await api(`/api/coroutines/${job.uid}?purge=true`, { method: 'DELETE' });
                toast('Coroutine purged', 'ok');
                goto('#/coroutines');
            } catch (e) { toast(e.message, 'err'); }
        },
    }, 'Purge history'));
    card.appendChild(row);
    return card;
}

/* --------------------------------------------------------------- wizard -- */
function corTimezone() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
    catch (_) { return ''; }
}

function corModalShell(title) {
    const root = $('#modal-root');
    root.innerHTML = '';
    const back = el('div', { class: 'modal-back' });
    const box = el('div', { class: 'modal wide' });
    box.appendChild(el('h3', {}, esc(title)));
    const body = el('div', { class: 'wizard' });
    const actions = el('div', { class: 'modal-actions' });
    box.appendChild(body);
    box.appendChild(actions);
    back.appendChild(box);
    root.appendChild(back);
    back.addEventListener('click', (e) => { if (e.target === back) closeModal(); });
    return { box, body, actions };
}

function corWizardDefaults(existing) {
    const tz = corTimezone();
    if (existing) {
        return {
            ...existing,
            timezone: existing.timezone || tz,
            run_at_epoch: Number(existing.run_at_epoch || 0),
            event_sender_allow: Array.isArray(existing.event_sender_allow) ? existing.event_sender_allow : [],
            event_filter: existing.event_filter || {},
            options: existing.options || {},
            tasks: [],
            _activate: existing.status === 'active',
            _step: 0,
        };
    }
    return {
        name: '', description: '', scope: 'project', project_id: null,
        execution_project_id: null, trigger_type: 'cron', trigger_expr: '0 12 * * *',
        timezone: tz, run_at_epoch: 0, prompt: '', model: '', agent: '',
        session_mode: 'fresh', run_timeout_seconds: 0,
        execution_mode: 'single', max_iterations: 1, iteration_delay_seconds: 5,
        completion_marker: '', stall_iterations: 3, cycle_max_seconds: 0,
        max_runs: 0, max_runs_per_day: 0,
        failure_policy: 'pause', failure_threshold: 3,
        restart_policy: 'resume', overlap_policy: 'skip', catch_up: 'skip',
        jitter_seconds: 0, event_source: 'room.message', event_room_id: '',
        event_mention_only: 0, event_sender_allow: [], event_min_interval_seconds: 5,
        event_filter: {}, post_result_to_room: 0, suppress_auto_reply: 1,
        post_on_failure: 0, options: {}, status: 'paused',
        tasks: [],
        _activate: false, _step: 0,
    };
}

function coroutineTaskBox(w, redraw) {
    const wrap = el('div', { style: 'display:flex;flex-direction:column;gap:8px' });
    const chips = el('div', { class: 'row wrap', style: 'gap:6px' });
    const selected = w.tasks || [];
    if (!selected.length) {
        chips.appendChild(el('span', { class: 'muted' }, 'No project tasks selected.'));
    }
    selected.forEach((task, index) => {
        chips.appendChild(el('span', { class: 'chip', style: 'background:var(--bg-card)' },
            `${esc(task.name)} <b data-crm="${index}" style="cursor:pointer">×</b>`));
    });
    chips.addEventListener('click', (e) => {
        const remove = e.target.closest('[data-crm]');
        if (!remove) return;
        w.tasks.splice(Number(remove.dataset.crm), 1);
        redraw();
    });
    const add = el('button', { class: 'btn sm ghost', type: 'button' }, '＋ Add project tasks');
    add.addEventListener('click', () => openCoroutineTaskPicker(w, redraw));
    wrap.appendChild(chips);
    wrap.appendChild(add);
    return wrap;
}

function openCoroutineTaskPicker(w, redraw) {
    const project = (state.projects || []).find(
        (p) => Number(p.id) === Number(w.execution_project_id));
    if (!project) { toast('Choose an execution project first', 'err'); return; }
    api(`/api/projects/${encodeURIComponent(project.slug)}/tasks`).then((res) => {
        const selected = new Set((w.tasks || []).map((t) => Number(t.id)));
        const tasks = (res.tasks || []).filter(
            (t) => t.enabled && !selected.has(Number(t.id)));
        if (!tasks.length) {
            toast('No enabled project tasks left to add', 'ok');
            return;
        }
        const body = el('div');
        for (const task of tasks) {
            body.appendChild(el('label', {
                class: 'opt',
                style: 'display:flex;gap:8px;align-items:flex-start;margin:6px 0',
            }, `<input type="checkbox" data-task="${esc(String(task.id))}">
                <span><b>${esc(task.name)}</b><span class="muted"> · ${esc(task.kind || 'prompt')}</span>
                <div class="meta">${esc(String(task.prompt || '').slice(0, 160))}</div></span>`));
        }
        const addSelected = () => {
            const chosen = [];
            body.querySelectorAll('input[data-task]:checked').forEach((cb) => {
                const task = tasks.find((t) => String(t.id) === cb.dataset.task);
                if (task) chosen.push(task);
            });
            if (!chosen.length) { toast('Select at least one task', 'err'); return; }
            w.tasks = [...(w.tasks || []), ...chosen];
            closeModal();
            redraw();
        };
        modal('Add project tasks', body, [
            { label: 'Add selected', class: 'primary', onClick: addSelected },
            { label: 'Cancel', class: 'ghost', onClick: closeModal },
        ]);
    }).catch((e) => toast(e.message, 'err'));
}

function openCoroutineWizard(existing, opts = {}) {
    const w = corWizardDefaults(existing);
    if (!w.execution_project_id && state.projects && state.projects.length) {
        w.execution_project_id = state.projects[0].id;
        if (w.scope === 'project') w.project_id = w.execution_project_id;
    }
    const isEdit = !!existing;
    const shell = corModalShell(isEdit ? `Edit ${existing.name}` : 'New coroutine');
    const STEPS = ['Basics', 'Work', 'Trigger', 'Safety', 'Review'];
    let saveBusy = false;

    const field = (label, node, hint) => {
        const wrap = el('label', { class: 'field' });
        wrap.appendChild(el('span', { class: 'lab' }, esc(label)));
        wrap.appendChild(node);
        if (hint) wrap.appendChild(el('span', { class: 'meta' }, esc(hint)));
        return wrap;
    };
    const text = (key, placeholder, type = 'text') => {
        const node = el('input', { type, placeholder: placeholder || '' });
        node.value = w[key] ?? '';
        node.addEventListener('input', () => { w[key] = node.value; });
        return node;
    };
    const num = (key, min, max, step = '1') => {
        const node = el('input', { type: 'number', min: String(min), max: String(max), step: String(step) });
        node.value = w[key] ?? '';
        node.addEventListener('input', () => { w[key] = node.value === '' ? 0 : Number(node.value); });
        return node;
    };
    const select = (key, options, onChange) => {
        const node = el('select', {});
        for (const [value, label] of options) {
            const opt = el('option', { value }, esc(label));
            if (String(w[key] ?? '') === String(value)) opt.selected = true;
            node.appendChild(opt);
        }
        node.addEventListener('change', () => { w[key] = node.value; if (onChange) onChange(node.value); });
        return node;
    };
    const check = (key, label, onChange) => {
        const node = el('input', { type: 'checkbox' });
        node.checked = !!w[key];
        node.addEventListener('change', () => { w[key] = node.checked ? 1 : 0; if (onChange) onChange(node.checked); });
        const wrap = el('label', { class: 'opt' });
        wrap.appendChild(node);
        wrap.appendChild(el('span', {}, esc(label)));
        return { node, wrap };
    };
    const projectOptions = () => (state.projects || []).map((p) => [p.id, `${p.name} (${p.slug})`]);

    const render = () => {
        shell.body.innerHTML = '';
        const steps = el('div', { class: 'wiz-steps' });
        STEPS.forEach((s, i) => steps.appendChild(el('span', {
            class: `wiz-step ${i === w._step ? 'active' : ''} ${i < w._step ? 'done' : ''}`,
        }, `<b>${i + 1}</b> ${esc(s)}`)));
        shell.body.appendChild(steps);
        const pane = el('div', { class: 'wiz-pane' });
        shell.body.appendChild(pane);
        const err = el('div', { class: 'cor-warn', id: 'cor-wiz-err', style: 'display:none' });
        shell.body.appendChild(err);
        const showError = (msg) => {
            if (!msg) { err.style.display = 'none'; err.textContent = ''; return; }
            err.style.display = 'block';
            err.textContent = msg;
        };
        // Defined before the step branches so the trigger form can schedule a
        // preview immediately; ``previewHost`` is created inside step 2.
        const preview = (host) => coroutinePreview(w, host);

        if (w._step === 0) {
            pane.appendChild(field('Name', text('name', 'e.g. Daily reminder')));
            pane.appendChild(field('Description', text('description', 'optional')));
            const scope = select('scope', [['project', 'Project'], ['global', 'Global']], () => {
                if (w.scope === 'global' && w.trigger_type === 'event') {
                    w.trigger_type = 'manual';
                    w.event_source = '';
                    w.event_room_id = '';
                }
                if (w.scope === 'global') w.tasks = [];
                render();
            });
            pane.appendChild(field('Scope', scope,
                'Project jobs run in one project. Global jobs run as the orchestrator in the Perun control plane and are not tied to a project. Event triggers are project-scoped.'));
            if (w.scope === 'global') {
                pane.appendChild(el('div', { class: 'cor-warn' },
                    'Global coroutines are run by the orchestrator, not inside a project. Use them for reminders, digests, reports, and machine-wide housekeeping.'));
            } else if (!state.projects || !state.projects.length) {
                pane.appendChild(el('div', { class: 'cor-warn' },
                    'No projects are registered. Add one from the Projects tab, or switch Scope to Global for an orchestrator job.'));
            } else {
                const exec = select('execution_project_id', projectOptions(), (value) => {
                    w.execution_project_id = Number(value);
                    if (w.scope === 'project') w.project_id = w.execution_project_id;
                    w.tasks = [];
                });
                pane.appendChild(field('Execution project', exec,
                    'The agent runs in this directory. Global jobs do not use a project; they run as the orchestrator.'));
            }
            const templates = el('div', { class: 'method-cards' });
            const tpl = w.scope === 'global'
                ? [
                    ['Daily reminder', 'Send me a short reminder via perun_telegram_send if it is configured; otherwise reply with it here. Reminder: <what should I be reminded of?>', '0 9 * * *'],
                    ['One-time reminder', 'Remind me once: <what should I be reminded of?>. Send it via perun_telegram_send if configured; otherwise reply here.', '', 'once'],
                    ['Morning digest', 'Summarize the most important state across all projects: open work, recent activity, and anything that needs me today. Use the perun_* tools.', '0 8 * * *'],
                    ['Machine health check', 'Check whether Perun, OpenCode, Ollama, the PerunChat bridge, and recent runs are healthy. Report anything that needs attention.', '0 */6 * * *'],
                ]
                : [
                    ['Daily project digest', 'Summarize what changed in this project today. List the most important commits, open questions, and a short next-step plan.', '0 9 * * *'],
                    ['Nightly dependency audit', 'Audit this project\'s dependencies for outdated, vulnerable, or unused packages. Do not modify files; write a short report.', '0 2 * * *'],
                    ['Continuous docs refresh', 'Review the recent code changes and update the project documentation where it is stale. Keep the edit focused and safe.', '0 */6 * * *'],
                    ['One-time task', 'Do this once: <what should be done?>.', '', 'once'],
                ];
            for (const [name, prompt, cron, triggerType] of tpl) {
                const when = triggerType === 'once' ? 'once' : cron;
                const b = el('button', { class: 'method-card', type: 'button', onclick: () => {
                    w.name = w.name || name;
                    w.prompt = prompt;
                    if (triggerType === 'once') {
                        w.trigger_type = 'once';
                        w.trigger_expr = '';
                        w.run_at_epoch = Math.floor(Date.now() / 1000) + 3600;
                    } else {
                        w.trigger_type = 'cron';
                        w.trigger_expr = cron;
                    }
                    render();
                } }, `<div class="mc-title">${esc(name)}</div><div class="mc-sub">${esc(when)}</div>`);
                templates.appendChild(b);
            }
            pane.appendChild(el('div', { class: 'mselect-label', style: 'margin-top:14px' }, 'Start from a template'));
            pane.appendChild(templates);
        } else if (w._step === 1) {
            if (w.scope === 'project') {
                pane.appendChild(field('Project tasks', coroutineTaskBox(w, render),
                    isEdit
                        ? 'Optional. Earlier selections are already part of the saved prompt; adding tasks appends new blocks (they are not shown as chips when editing).'
                        : 'Optional. Selected tasks are composed with the prompt. A skill task becomes a structured instruction for the headless run, not a TUI slash command.'));
            }
            const ta = el('textarea', { rows: '7', placeholder: 'What should the agent do on each run?' });
            ta.value = w.prompt || '';
            ta.addEventListener('input', () => { w.prompt = ta.value; });
            pane.appendChild(field(w.scope === 'project' ? 'Additional prompt' : 'Prompt', ta,
                w.scope === 'project'
                    ? 'Optional when project tasks are selected. It is placed before the task blocks.'
                    : ''));
            const modelHost = el('div', { class: 'select-host' });
            const ms = modelSelect({ value: w.model || '', placeholder: w.scope === 'global' ? 'orchestrator default model' : 'project default model', onSelect: (id) => { w.model = id; } });
            modelHost.appendChild(ms);
            w._modelNode = ms;
            pane.appendChild(field('Model', modelHost, 'Leave empty to use the project/agent default.'));
            const agentHost = el('div', { class: 'select-host' });
            const as = agentSelect({ value: w.agent || '', placeholder: w.scope === 'global' ? 'orchestrator default agent' : 'project default agent', onSelect: (id) => { w.agent = id; } });
            agentHost.appendChild(as);
            w._agentNode = as;
            pane.appendChild(field('Agent', agentHost,
                w.trigger_type === 'event'
                    ? 'Event content is untrusted, so event jobs default to the read-only plan agent. Pick a write-capable agent only if you trust the source.'
                    : 'Use a read-only/plan agent for reports if you do not want autonomous edits.'));
            pane.appendChild(field('Session', select('session_mode', [['fresh', 'Fresh session each cycle'], ['continue', 'Continue the job\'s previous session']])));
            pane.appendChild(field('Run timeout (seconds)', num('run_timeout_seconds', 0, 86400),
                '0 uses the global run timeout. The scheduler also enforces this as a watchdog.'));
        } else if (w._step === 2) {
            const types = [
                ['interval', 'Interval', 'Every N minutes, hours, or days'],
                ['cron', 'Schedule', 'Daily, weekdays, weekly, monthly, or custom cron'],
                ['once', 'Once', 'A single date and time'],
                ['manual', 'Manual', 'Only when you click Run now'],
                ['event', 'Event', 'Webhook, run, proposal, inbox, git, or room message'],
            ].filter(([id]) => !(w.scope === 'global' && id === 'event'));
            const grid = el('div', { class: 'method-cards' });
            for (const [id, title, sub] of types) {
                const b = el('button', {
                    class: 'method-card' + (w.trigger_type === id ? ' selected' : ''),
                    type: 'button',
                    onclick: () => {
                        w.trigger_type = id;
                        if (id === 'event' && !w.agent) w.agent = 'plan';
                        if (id === 'interval' && !w.trigger_expr) w.trigger_expr = '3600';
                        if (id === 'cron' && !String(w.trigger_expr || '').includes(' ')) w.trigger_expr = '0 12 * * *';
                        if (id === 'once' && !w.run_at_epoch) w.run_at_epoch = Math.floor(Date.now() / 1000) + 3600;
                        render();
                    },
                }, `<div class="mc-title">${esc(title)}</div><div class="mc-sub">${esc(sub)}</div>`);
                grid.appendChild(b);
            }
            pane.appendChild(grid);
            if (w.scope === 'global') {
                pane.appendChild(el('div', { class: 'cor-warn' },
                    'Event triggers are project-scoped. Global jobs are scheduled, one-shot, or manual; create a project-scoped job when the trigger is a room message, run, proposal, git event, or webhook.'));
            }
            const host = el('div', { class: 'cor-trigger-form' });
            pane.appendChild(host);
            const previewHost = el('div', { class: 'cor-next', id: 'cor-preview' });
            pane.appendChild(previewHost);
            const tzInput = text('timezone', 'Europe/Warsaw');
            tzInput.addEventListener('input', () => preview(previewHost));
            const tzField = field('Timezone', tzInput, 'IANA name. Use the browser zone shown by default.');
            if (w.trigger_type === 'interval') {
                const seconds = Number(w.trigger_expr || 3600);
                const unit = seconds >= 86400 && seconds % 86400 === 0 ? 'days' : (seconds >= 3600 && seconds % 3600 === 0 ? 'hours' : 'minutes');
                const value = unit === 'days' ? seconds / 86400 : (unit === 'hours' ? seconds / 3600 : Math.max(1, Math.round(seconds / 60)));
                const amount = el('input', { type: 'number', min: '1', max: '100000', value: String(value) });
                const unitSel = el('select', {});
                for (const [v, l] of [['minutes', 'minutes'], ['hours', 'hours'], ['days', 'days']]) unitSel.appendChild(el('option', { value: v }, l));
                unitSel.value = unit;
                const sync = () => {
                    const n = Math.max(1, Number(amount.value) || 1);
                    w.trigger_expr = String(n * (unitSel.value === 'days' ? 86400 : unitSel.value === 'hours' ? 3600 : 60));
                    preview(previewHost);
                };
                amount.addEventListener('input', sync);
                unitSel.addEventListener('change', sync);
                const row = el('div', { class: 'row wrap' });
                row.appendChild(el('span', { class: 'mselect-label' }, 'Every'));
                row.appendChild(amount);
                row.appendChild(unitSel);
                host.appendChild(row);
                host.appendChild(tzField);
            } else if (w.trigger_type === 'cron') {
                let parts = { preset: 'daily', time: '12:00', day: 1 };
                const p = String(w.trigger_expr || '').trim().split(/\s+/);
                if (p.length === 5) {
                    const [min, hour, dom, mon, dow] = p;
                    if (/^\d+$/.test(min) && /^\d+$/.test(hour)) {
                        parts.time = `${corPad(Number(hour))}:${corPad(Number(min))}`;
                    }
                    if (mon === '*' && dom === '*' && dow === '*') parts.preset = 'daily';
                    else if (mon === '*' && dom === '*' && dow === '1-5') parts.preset = 'weekdays';
                    else if (mon === '*' && dom === '*' && /^\d+$/.test(dow)) { parts.preset = 'weekly'; parts.day = Number(dow); }
                    else if (mon === '*' && /^\d+$/.test(dom) && dow === '*') { parts.preset = 'monthly'; parts.day = Number(dom); }
                    else parts.preset = 'custom';
                }
                const preset = el('select', {});
                for (const [v, l] of [['daily', 'Every day'], ['weekdays', 'Every weekday'], ['weekly', 'Every week'], ['monthly', 'Every month'], ['custom', 'Custom cron']]) preset.appendChild(el('option', { value: v }, l));
                preset.value = parts.preset;
                const time = el('input', { type: 'time', value: parts.time });
                const day = el('select', {});
                for (let i = 0; i < 7; i++) day.appendChild(el('option', { value: String(i) }, CS.DAYS[i]));
                for (let i = 1; i <= 28; i++) day.appendChild(el('option', { value: String(i) }, `day ${i}`));
                day.value = String(parts.day || 1);
                const custom = el('input', { type: 'text', placeholder: '0 12 * * *', value: w.trigger_expr || '' });
                const apply = () => {
                    if (preset.value === 'custom') w.trigger_expr = custom.value.trim();
                    else w.trigger_expr = CS.presetCron(preset.value, time.value, Number(day.value));
                    preview(previewHost);
                };
                for (const n of [preset, time, day]) n.addEventListener('change', () => { updateCronVisibility(); apply(); });
                custom.addEventListener('input', apply);
                const updateCronVisibility = () => {
                    time.style.display = preset.value === 'custom' ? 'none' : '';
                    day.style.display = preset.value === 'weekly' || preset.value === 'monthly' ? '' : 'none';
                    custom.style.display = preset.value === 'custom' ? '' : 'none';
                };
                const row = el('div', { class: 'row wrap' });
                row.appendChild(preset);
                row.appendChild(time);
                row.appendChild(day);
                host.appendChild(row);
                host.appendChild(custom);
                host.appendChild(tzField);
                updateCronVisibility();
            } else if (w.trigger_type === 'once') {
                const dt = el('input', { type: 'datetime-local' });
                const epoch = Number(w.run_at_epoch || 0);
                if (epoch) {
                    const d = new Date(epoch * 1000);
                    dt.value = `${d.getFullYear()}-${corPad(d.getMonth() + 1)}-${corPad(d.getDate())}T${corPad(d.getHours())}:${corPad(d.getMinutes())}`;
                }
                dt.addEventListener('change', () => {
                    const ms = new Date(dt.value).getTime();
                    w.run_at_epoch = Number.isFinite(ms) ? Math.floor(ms / 1000) : 0;
                    preview(previewHost);
                });
                host.appendChild(field('Date and time', dt));
                host.appendChild(tzField);
            } else if (w.trigger_type === 'manual') {
                host.appendChild(el('p', { class: 'muted' },
                    'Manual jobs never fire on a schedule. Start them with Run now, or from an API/script.'));
            } else if (w.trigger_type === 'event') {
                const sources = [
                    ['room.message', 'PerunChat room message'],
                    ['webhook', 'Webhook'],
                    ['run.finished', 'A run finishes'],
                    ['proposal.approved', 'A proposal is approved'],
                    ['proposal.executed', 'A proposal is executed'],
                    ['attention.created', 'An operator-inbox item is created'],
                    ['git.commit', 'A git commit'],
                    ['git.push', 'A git push'],
                ];
                host.appendChild(field('Event source', select('event_source', sources, () => render())));
                if (w.event_source === 'room.message') {
                    const rooms = el('input', { type: 'text', list: 'cor-rooms', placeholder: 'grp:…' });
                    rooms.value = w.event_room_id || '';
                    rooms.addEventListener('input', () => { w.event_room_id = rooms.value.trim(); });
                    const dl = el('datalist', { id: 'cor-rooms' });
                    coroutineRooms().then((list) => {
                        dl.innerHTML = list.map((r) => `<option value="${esc(r)}"></option>`).join('');
                    });
                    host.appendChild(field('Room', rooms, 'Only rooms this PerunChat bridge has joined can deliver events.'));
                    host.appendChild(dl);
                    const bridge = el('div', { class: 'meta' }, 'Checking the PerunChat bridge…');
                    api('/api/perunchat/bridge/status').then((st) => {
                        bridge.textContent = st.running ? `Bridge running as ${st.agent_id || 'Perun'} · ${(st.rooms || []).length} room(s)` : 'Bridge is not running. Messages will not arrive until it is started.';
                    }).catch(() => { bridge.textContent = 'Bridge status unavailable.'; });
                    host.appendChild(bridge);
                    const mention = check('event_mention_only', 'Only when the message mentions @perun, @overseer, or an operator');
                    host.appendChild(mention.wrap);
                    const allow = el('input', { type: 'text', placeholder: 'alice, bob (empty = any sender)' });
                    allow.value = (w.event_sender_allow || []).join(', ');
                    allow.addEventListener('input', () => {
                        w.event_sender_allow = allow.value.split(',').map((x) => x.trim()).filter(Boolean);
                    });
                    host.appendChild(field('Allowed senders', allow));
                } else if (w.event_source === 'webhook') {
                    host.appendChild(el('p', { class: 'muted' },
                        'After you create the job you will see a one-time webhook URL. Send a JSON object to it to trigger a cycle.'));
                } else {
                    const project = el('select', {});
                    project.appendChild(el('option', { value: '' }, 'Any project'));
                    for (const p of (state.projects || [])) project.appendChild(el('option', { value: String(p.id) }, `${p.name} (${p.slug})`));
                    project.value = String((w.event_filter || {}).project_id || '');
                    project.addEventListener('change', () => {
                        w.event_filter = { ...(w.event_filter || {}) };
                        if (project.value) w.event_filter.project_id = Number(project.value);
                        else delete w.event_filter.project_id;
                    });
                    host.appendChild(field('Project filter', project));
                    if (w.event_source === 'run.finished') {
                        const st = el('select', {});
                        st.appendChild(el('option', { value: '' }, 'Any status'));
                        for (const s of ['done', 'failed', 'timeout']) st.appendChild(el('option', { value: s }, s));
                        st.value = String((w.event_filter || {}).status?.[0] || '');
                        st.addEventListener('change', () => {
                            w.event_filter = { ...(w.event_filter || {}) };
                            if (st.value) w.event_filter.status = [st.value];
                            else delete w.event_filter.status;
                        });
                        host.appendChild(field('Run status', st));
                    }
                }
                host.appendChild(el('p', { class: 'muted' },
                    'The prompt receives the event as untrusted data. You can also use {{event.text}}, {{event.sender}}, {{event.room_id}}, {{event.message_id}}, {{event.kind}}, or {{event.payload}}.'));
            }
            pane.appendChild(host);
            preview(previewHost);
        } else if (w._step === 3) {
            const modes = [
                ['single', 'Single run', 'One agent run per cycle'],
                ['loop', 'Loop', 'Re-run in the same session until done'],
                ['continuous', 'Continuous', 'Loop until you stop it (with hard caps)'],
            ];
            const grid = el('div', { class: 'method-cards' });
            const current = w.execution_mode === 'loop' && Number(w.max_iterations) === 0 ? 'continuous'
                : (w.execution_mode === 'loop' ? 'loop' : 'single');
            for (const [id, title, sub] of modes) {
                const b = el('button', {
                    class: 'method-card' + (current === id ? ' selected' : ''), type: 'button',
                    onclick: () => {
                        if (id === 'single') { w.execution_mode = 'single'; w.max_iterations = 1; }
                        else if (id === 'loop') { w.execution_mode = 'loop'; w.max_iterations = w.max_iterations && Number(w.max_iterations) > 0 ? w.max_iterations : 10; }
                        else {
                            w.execution_mode = 'loop'; w.max_iterations = 0;
                            if (!Number(w.max_runs_per_day)) w.max_runs_per_day = 200;
                            if (!Number(w.cycle_max_seconds)) w.cycle_max_seconds = 21600;
                            w.restart_policy = 'pause';
                        }
                        render();
                    },
                }, `<div class="mc-title">${esc(title)}</div><div class="mc-sub">${esc(sub)}</div>`);
                grid.appendChild(b);
            }
            pane.appendChild(grid);
            if (w.execution_mode === 'loop') {
                pane.appendChild(field('Max iterations', num('max_iterations', 0, 100000),
                    '0 means unlimited (worker). Unlimited requires a daily cap and a cycle wall-clock cap.'));
                pane.appendChild(field('Delay between iterations (seconds)', num('iteration_delay_seconds', 0, 86400)));
                pane.appendChild(field('Completion marker', text('completion_marker', 'COROUTINE_COMPLETE'),
                    'The loop ends when the final line of a reply is exactly this text. Empty means run until the cap.'));
                pane.appendChild(field('Stall guard (iterations)', num('stall_iterations', 0, 20),
                    'Pause when this many consecutive iterations produce identical or empty output. 0 disables.'));
            }
            const caps = el('div', { class: 'form-row' });
            caps.appendChild(field('Max runs (lifetime, 0=∞)', num('max_runs', 0, 1000000)));
            caps.appendChild(field('Max runs per day (0=∞)', num('max_runs_per_day', 0, 100000)));
            pane.appendChild(caps);
            pane.appendChild(field('Cycle wall-clock cap (seconds, 0=∞)', num('cycle_max_seconds', 0, 604800),
                'Mandatory for unlimited loops; recommended for every loop.'));
            const fail = el('div', { class: 'form-row' });
            fail.appendChild(field('Failure policy', select('failure_policy', [['pause', 'Pause after threshold'], ['continue', 'Continue the schedule']])));
            fail.appendChild(field('Failure threshold', num('failure_threshold', 1, 100)));
            pane.appendChild(fail);
            pane.appendChild(field('Restart behaviour', select('restart_policy', [['resume', 'Resume after a restart'], ['pause', 'Pause after a restart']])));
            const opts = el('div', { class: 'form-row' });
            opts.appendChild(field('Catch up missed runs', select('catch_up', [['skip', 'Skip missed slots'], ['once', 'Run once after downtime']])));
            opts.appendChild(field('Jitter (seconds)', num('jitter_seconds', 0, 3600)));
            pane.appendChild(opts);
            if (w.scope === 'global' && w.trigger_type === 'event') {
                pane.appendChild(el('div', { class: 'cor-warn' },
                    'Global event jobs run as the orchestrator with its tool access on untrusted event content. Prefer a trusted room or webhook; for reminders, a scheduled global job is safer than an event-triggered one.'));
            }
            if (w.trigger_type === 'event' && w.event_source === 'room.message') {
                pane.appendChild(el('div', { class: 'mselect-label', style: 'margin-top:12px' }, 'Room result'));
                const post = check('post_result_to_room', 'Post the final result back into the room');
                pane.appendChild(post.wrap);
                const suppress = check('suppress_auto_reply', 'Suppress the bridge\'s automatic conversational reply');
                pane.appendChild(suppress.wrap);
                const onFail = check('post_on_failure', 'Also post a short failure note');
                pane.appendChild(onFail.wrap);
            }
            if (w.execution_mode === 'loop' && Number(w.max_iterations) === 0) {
                pane.appendChild(el('div', { class: 'cor-warn' },
                    'Unlimited loops are real workers: they consume an execution slot and can be expensive. The daily cap and cycle cap are enforced by the scheduler.'));
            }
        } else if (w._step === 4) {
            pane.appendChild(el('div', { class: 'cor-summary' },
                `<div class="eyebrow">Review</div><h3>${esc(w.name || 'Coroutine')}</h3>
                 <p>${esc(CS.triggerSentence(w))}</p>`));
            const rows = [
                ['Scope', w.scope],
                ['Execution project', w.scope === 'global'
                    ? 'Orchestrator (control plane)'
                    : ((state.projects.find((p) => Number(p.id) === Number(w.execution_project_id)) || {}).name || '—')],
                ['Trigger', CS.humanTrigger(w)],
                ['Mode', CS.modeHuman(w)],
                ['Model', w.model || 'project default'],
                ['Agent', w.agent || 'project default'],
                ...(w.scope === 'project' ? [[
                    'Project tasks',
                    (w.tasks || []).map((t) => t.name).join(', ') || 'none',
                ]] : []),
                ['Daily cap', Number(w.max_runs_per_day) ? `${w.max_runs_per_day} runs` : 'unlimited'],
                ['Failure policy', `${w.failure_policy} after ${w.failure_threshold}`],
            ];
            pane.appendChild(el('div', { class: 'cor-kv' },
                rows.map(([k, v]) => `<div><span class="mselect-label">${esc(k)}</span><div>${esc(String(v))}</div></div>`).join('')));
            if (w.trigger_type === 'event' && w.event_source === 'webhook') {
                pane.appendChild(el('div', { class: 'cor-warn' },
                    'The webhook URL and secret will be shown once after you create the job.'));
            }
            if (!isEdit) {
                const activate = check('_activate', 'Activate immediately (otherwise the job is created paused)');
                pane.appendChild(activate.wrap);
            } else {
                pane.appendChild(el('p', { class: 'muted' },
                    'Use Pause or Resume on the detail page to change the schedule state; editing does not activate a paused job automatically.'));
            }
            pane.appendChild(el('p', { class: 'muted' },
                'You can always run a paused job once with Run now, inspect the output, and activate it when you are happy.'));
        }

        shell.actions.innerHTML = '';
        if (w._step > 0) shell.actions.appendChild(el('button', {
            class: 'btn ghost', type: 'button',
            onclick: () => { w._step -= 1; render(); },
        }, '◀ Back'));
        shell.actions.appendChild(el('button', {
            class: 'btn ghost', type: 'button', onclick: closeModal,
        }, 'Cancel'));
        if (w._step < STEPS.length - 1) {
            shell.actions.appendChild(el('button', {
                class: 'btn primary', type: 'button',
                onclick: () => {
                    if (w._modelNode) w.model = w._modelNode.getValue();
                    if (w._agentNode) w.agent = w._agentNode.getValue();
                    const problem = CS.validateStep(w._step, coroutineValidationState(w));
                    if (problem) { showError(problem); return; }
                    if (w._step === 2 && w._preview && w._preview.valid === false) {
                        showError(w._preview.error || 'The trigger is not valid.'); return;
                    }
                    w._step += 1;
                    render();
                },
            }, 'Next ▶'));
        } else {
            shell.actions.appendChild(el('button', {
                class: 'btn primary', type: 'button',
                onclick: async () => {
                    if (saveBusy) return;
                    if (w._modelNode) w.model = w._modelNode.getValue();
                    if (w._agentNode) w.agent = w._agentNode.getValue();
                    const validation = coroutineValidationState(w);
                    const problem = CS.validateStep(3, validation) || CS.validateStep(2, validation) || CS.validateStep(1, validation) || CS.validateStep(0, validation);
                    if (problem) { showError(problem); return; }
                    saveBusy = true;
                    try {
                        const payload = corWizardPayload(w);
                        let res;
                        if (isEdit) {
                            payload.activate = !!w._activate && existing.status !== 'active';
                            res = await api(`/api/coroutines/${existing.uid}`, { method: 'PATCH', body: payload });
                        } else {
                            res = await api('/api/coroutines', { method: 'POST', body: { ...payload, activate: !!w._activate } });
                        }
                        const job = res.coroutine;
                        closeModal();
                        if (job && job.webhook_url) showCoroutineWebhook(job);
                        else toast(isEdit ? 'Coroutine saved' : 'Coroutine created', 'ok');
                        if (opts.onSaved) await opts.onSaved();
                        else goto(`#/coroutines/${job.uid}`);
                    } catch (e) {
                        const detail = e.message || 'Could not save the coroutine.';
                        showError(detail);
                        toast(detail, 'err');
                    } finally {
                        saveBusy = false;
                    }
                },
            }, isEdit ? 'Save changes' : 'Create coroutine'));
        }

    };
    render();
}

function coroutinePreview(w, host) {
    if (!host) return;
    if (w._previewTimer) clearTimeout(w._previewTimer);
    const seq = ++_coroutinePreviewSeq;
    w._previewTimer = setTimeout(async () => {
        try {
            const res = await api('/api/coroutines/preview', {
                method: 'POST',
                body: {
                    trigger_type: w.trigger_type,
                    trigger_expr: w.trigger_expr,
                    timezone: w.timezone,
                    run_at_epoch: w.run_at_epoch,
                },
            });
            if (seq !== _coroutinePreviewSeq || !document.body.contains(host)) return;
            w._preview = res;
            host.innerHTML = res.valid
                ? `<div class="meta">${res.next_fires && res.next_fires.length
                    ? 'Next: ' + res.next_fires.map((x) => esc(CS.fmtDate(x.epoch, w.timezone))).join(' · ')
                    : 'No timed fire (manual/event).'}</div>`
                : `<div class="err">${esc(res.error)}</div>`;
        } catch (e) {
            if (document.body.contains(host)) host.innerHTML = `<div class="err">${esc(e.message)}</div>`;
        }
    }, 250);
}

function coroutinePrompt(w) {
    // A project task list is composed into the job's prompt once, at save time.
    // Task edits later do not rewrite an existing coroutine.
    if (w.scope !== 'project') return String(w.prompt || '').trim();
    return buildCoroutinePrompt(w.tasks || [], w.prompt);
}

function coroutineValidationState(w) {
    // The step validator only sees ``prompt``; give it the composed text so a
    // job with tasks and no extra prompt is valid.
    return { ...w, prompt: coroutinePrompt(w) };
}

function corWizardPayload(w) {
    return {
        name: String(w.name || '').trim(),
        description: String(w.description || '').trim(),
        scope: w.scope,
        project_id: w.scope === 'project' ? Number(w.execution_project_id) : null,
        // Global jobs are resolved to the control plane by the backend; never
        // send a user project for them.
        execution_project_id: w.scope === 'global' ? 0 : Number(w.execution_project_id),
        trigger_type: w.trigger_type,
        trigger_expr: String(w.trigger_expr || ''),
        timezone: String(w.timezone || ''),
        run_at_epoch: Number(w.run_at_epoch || 0),
        prompt: coroutinePrompt(w),
        model: String(w.model || '').trim(),
        agent: String(w.agent || '').trim(),
        session_mode: w.session_mode,
        run_timeout_seconds: Number(w.run_timeout_seconds || 0),
        execution_mode: w.execution_mode,
        max_iterations: Number(w.max_iterations || 0),
        iteration_delay_seconds: Number(w.iteration_delay_seconds || 0),
        completion_marker: String(w.completion_marker || '').trim(),
        stall_iterations: Number(w.stall_iterations || 0),
        cycle_max_seconds: Number(w.cycle_max_seconds || 0),
        max_runs: Number(w.max_runs || 0),
        max_runs_per_day: Number(w.max_runs_per_day || 0),
        overlap_policy: w.overlap_policy || 'skip',
        catch_up: w.catch_up || 'skip',
        jitter_seconds: Number(w.jitter_seconds || 0),
        failure_policy: w.failure_policy || 'pause',
        failure_threshold: Number(w.failure_threshold || 3),
        restart_policy: w.restart_policy || 'resume',
        post_result_to_room: Number(w.post_result_to_room || 0),
        suppress_auto_reply: Number(w.suppress_auto_reply || 0),
        post_on_failure: Number(w.post_on_failure || 0),
        event_source: w.trigger_type === 'event' ? (w.event_source || '') : '',
        event_room_id: w.trigger_type === 'event' && w.event_source === 'room.message' ? String(w.event_room_id || '') : '',
        event_mention_only: Number(w.event_mention_only || 0),
        event_sender_allow: Array.isArray(w.event_sender_allow) ? w.event_sender_allow : [],
        event_min_interval_seconds: Number(w.event_min_interval_seconds || 5),
        event_filter: w.event_filter || {},
        options: w.options || {},
    };
}

function openCoroutineQuickCreate() {
    const shell = corModalShell('Quick create a coroutine');
    const field = (label, node) => {
        const wrap = el('label', { class: 'field' });
        wrap.appendChild(el('span', { class: 'lab' }, esc(label)));
        wrap.appendChild(node);
        return wrap;
    };
    const name = el('input', { type: 'text', placeholder: 'Daily reminder' });
    const scope = el('select', {});
    scope.appendChild(el('option', { value: 'project' }, 'Project'));
    scope.appendChild(el('option', { value: 'global' }, 'Global (orchestrator)'));
    const project = el('select', {});
    for (const p of (state.projects || [])) project.appendChild(el('option', { value: String(p.id) }, `${p.name} (${p.slug})`));
    const projectField = field('Execution project', project);
    const globalNote = el('div', { class: 'cor-warn' },
        'Global coroutines run as the orchestrator in the Perun control plane, not inside a project. Use them for reminders, digests, and machine-wide tasks.');
    globalNote.style.display = 'none';
    const prompt = el('textarea', { rows: '5', placeholder: 'e.g. Remind me to …' });
    const when = el('select', {});
    when.appendChild(el('option', { value: 'daily' }, 'Every day'));
    when.appendChild(el('option', { value: 'once' }, 'Once'));
    const time = el('input', { type: 'time', value: '12:00' });
    const onceAt = el('input', { type: 'datetime-local' });
    const defaultOnce = new Date(Date.now() + 3600 * 1000);
    onceAt.value = `${defaultOnce.getFullYear()}-${corPad(defaultOnce.getMonth() + 1)}-${corPad(defaultOnce.getDate())}T${corPad(defaultOnce.getHours())}:${corPad(defaultOnce.getMinutes())}`;
    const dailyField = field('Run every day at', time);
    const onceField = field('Run once at', onceAt);
    const whenHint = el('p', { class: 'muted' },
        'Runs every day at the chosen local time. The timezone is your browser zone.');
    const syncWhen = () => {
        const once = when.value === 'once';
        dailyField.style.display = once ? 'none' : '';
        onceField.style.display = once ? '' : 'none';
        whenHint.textContent = once
            ? 'Runs once at the chosen local time, then completes. The timezone is your browser zone.'
            : 'Runs every day at the chosen local time. The timezone is your browser zone.';
    };
    when.addEventListener('change', syncWhen);
    const syncScope = () => {
        const isGlobal = scope.value === 'global';
        projectField.style.display = isGlobal ? 'none' : '';
        globalNote.style.display = isGlobal ? '' : 'none';
    };
    scope.addEventListener('change', syncScope);
    shell.body.appendChild(field('Name', name));
    shell.body.appendChild(field('Scope', scope));
    shell.body.appendChild(projectField);
    shell.body.appendChild(globalNote);
    shell.body.appendChild(field('Prompt', prompt));
    shell.body.appendChild(field('Repeat', when));
    shell.body.appendChild(dailyField);
    shell.body.appendChild(onceField);
    shell.body.appendChild(whenHint);
    syncWhen();
    shell.body.appendChild(el('p', { class: 'muted' },
        'Creates a paused job. Review the next fire and activate it from the detail view.'));
    shell.actions.appendChild(el('button', { class: 'btn ghost', onclick: closeModal }, 'Cancel'));
    shell.actions.appendChild(el('button', {
        class: 'btn primary',
        onclick: async () => {
            if (!name.value.trim()) { toast('Give it a name', 'err'); return; }
            if (!prompt.value.trim()) { toast('Write a prompt', 'err'); return; }
            const isGlobal = scope.value === 'global';
            if (!isGlobal && (!state.projects || !state.projects.length)) {
                toast('Add a project, or choose Global', 'err');
                return;
            }
            const trigger = CS.quickTrigger(when.value, time.value, onceAt.value);
            if (!trigger) {
                toast('Choose a future date and time', 'err');
                return;
            }
            try {
                const res = await api('/api/coroutines', {
                    method: 'POST',
                    body: {
                        name: name.value.trim(),
                        scope: isGlobal ? 'global' : 'project',
                        project_id: isGlobal ? null : Number(project.value),
                        execution_project_id: isGlobal ? 0 : Number(project.value),
                        ...trigger,
                        timezone: corTimezone(),
                        prompt: prompt.value.trim(),
                        execution_mode: 'single',
                        activate: false,
                    },
                });
                closeModal();
                toast('Created paused — review it, then activate', 'ok');
                goto(`#/coroutines/${res.coroutine.uid}`);
            } catch (e) { toast(e.message, 'err'); }
        },
    }, 'Create paused'));
}

async function boot() {
    wireChrome();
    perunFx.init();
    // The sigil is a little altar: click it to summon the storm.
    const sigil = $('#perun-sigil');
    if (sigil) sigil.addEventListener('click', () => {
        perunFx.flash(sigil);
        for (let i = 0; i < 3; i++) {
            setTimeout(() => perunFx.strike(window.innerWidth * (0.2 + Math.random() * 0.6), -10,
                { len: window.innerHeight * 0.5, branches: 3 }), i * 130);
        }
    });
    if (!location.hash) location.hash = '#/dashboard';
    // modelListHtml / model-root were removed; the custom mselect covers model choice.
    loadModels();
    await refreshHealth();
    // Load projects first so presence knows which cards are awake, then presence.
    await loadProjects().catch(() => {});
    await refreshPresence();
    await router();
    promptForUnlockPassword();
    refreshDashboardStats();
    checkForUpdate();
    setInterval(refreshHealth, 15000);
    setInterval(refreshPresence, 4000);
    // A background tab throttles timers; refresh the moment the operator
    // returns instead of waiting for the next tick.
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshPresence();
    });
    window.addEventListener('focus', () => refreshPresence());
    setInterval(refreshDashboardStats, 10000);
    setInterval(checkForUpdate, 600000);  // 10 min
    window.addEventListener('hashchange', router);
}

document.addEventListener('DOMContentLoaded', boot);
