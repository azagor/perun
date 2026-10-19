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

const state = {
    projects: [],
    settings: null,
    health: null,
    activeRuns: [],
    models: [],
    agents: [],
    skills: [],
    tools: [],
    runSource: null,
    activitySource: null,
    // Orchestrator chat continuity (session per dashboard visit).
    orchestrator: { sessionPk: null, ocSid: '' },
};

/* ---------------------------------------------------------------- api ---- */
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

/* --------------------------------------------------------- formatting --- */
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
    state.projects = (await api('/api/projects')).projects || [];
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
        return r;
    } catch (e) {
        toast(`Could not open terminal: ${e.message || 'unknown error'}`, 'err');
        return null;
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
    const working = new Set();
    for (const r of (state.activeRuns || [])) if (r.project_slug) working.add(r.project_slug);
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
        // Sync running banner and stop button with the current active run so
        // cards update live without requiring a full page re-render.
        const run = ps.working ? (state.activeRuns || []).find((r) => r.project_slug === slug) : null;
        let banner = card.querySelector('.pc-running');
        let stopBtn = card.querySelector('[data-stop]');
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
            if (!stopBtn) {
                stopBtn = el('button', {
                    class: 'btn sm danger',
                    dataset: { stop: '', runUid: run.run_uid },
                    title: 'Cancel this run',
                }, '■ Stop');
                const actions = card.querySelector('.pc-actions');
                const openBtn = actions && actions.querySelector('[data-open]');
                if (actions) actions.insertBefore(stopBtn, openBtn || null);
            } else {
                stopBtn.dataset.runUid = run.run_uid;
            }
        } else {
            if (banner) banner.remove();
            if (stopBtn) stopBtn.remove();
        }
    });
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
    try { state.activeRuns = (await api('/api/runs/active')).runs || []; }
    catch (_) { state.activeRuns = []; }
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
    input.value = opts.value || '';
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
    input.value = opts.value || '';
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
    '#/console': viewConsole,
    '#/memory': viewMemory,
    '#/activity': viewActivity,
    '#/mcp': viewMcp,
    '#/telegram': viewTelegram,
    '#/perunchat': viewPerunChat,
    '#/providers': viewProviders,
    '#/settings': viewSettings,
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
}

async function router() {
    const { base, sub, rest } = parseHash();
    closeStreams();
    let fn = routes[base];
    let args = [];
    if (base === '#/project') { fn = viewProject; args = [sub, rest]; }
    else if (base === '#/console' && sub) { args = [sub]; }
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

    const grid = el('div', { class: 'grid stats-grid' });
    const stat = (label, value, foot) => el('div', { class: 'card stat' },
        `<div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${esc(foot || '')}</div>`);
    const opencodeCount = state.projects.filter((p) => p.kind === 'opencode').length;
    let providerSummary;
    try {
        const pr = await api('/api/providers');
        const connected = (pr.providers || []).filter((p) => p.connected).length;
        const total = (pr.providers || []).length;
        const prOk = pr.ok && total > 0;
        providerSummary = stat('Providers', prOk ? `${connected}/${total}` : '—',
            prOk ? `${connected} connected, ${total - connected} not connected` : (pr.error || 'check opencode auth'));
    } catch (e) {
        providerSummary = stat('Providers', '—', 'unavailable');
    }
    grid.appendChild(stat('Projects', state.projects.length, `${opencodeCount} OpenCode project(s)`));
    grid.appendChild(stat('Working now', presence.working.size, presence.working.size ? 'agent(s) executing' : 'no active runs'));
    grid.appendChild(stat('Awake', presence.awake.size, 'active in the last 15 min'));
    grid.appendChild(stat('Facts', stats.facts_total ?? 0, `${stats.facts_global ?? 0} global`));
    grid.appendChild(stat('Doc chunks', stats.chunks_total ?? 0, 'semantic index'));
    grid.appendChild(providerSummary);
    grid.appendChild(stat('Embeddings', stats.ollama_ok ? 'Online' : 'Offline', stats.ollama_ok ? 'Ollama ready' : 'start ollama serve'));
    v.appendChild(grid);

    // Working now — live runs, each mapped to its project
    if ((state.activeRuns || []).length) {
        v.appendChild(sectionTitle('Working now'));
        const box = el('div', { class: 'card' });
        const list = el('div', { class: 'list' });
        for (const r of state.activeRuns) {
            list.appendChild(el('div', { class: 'list-row' },
                `<span class="badge amber">${esc(r.status)}</span>
                 <span class="grow"><b>${esc(r.project_name || r.project_slug)}</b>
                 <span class="muted"> — ${esc((r.prompt || '').slice(0, 90))}</span></span>
                 <button class="btn sm ghost" data-run="${esc(r.run_uid)}" data-project="${esc(r.project_slug || '')}">View</button>`));
        }
        box.appendChild(list);
        v.appendChild(box);
    }

    // Projects grid
    v.appendChild(sectionTitle('Projects', el('button', { class: 'btn sm ghost', onclick: () => goto('#/projects') }, 'Manage →')));
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

function projectGrid(projects) {
    if (!projects.length) return el('div', { class: 'empty' }, 'No projects yet — run discovery to register your projects.');
    const grid = el('div', { class: 'grid proj-grid' });
    for (const p of projects) grid.appendChild(projectCard(p));
    return grid;
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
    const running = run ? `<div class="pc-running">⚡ <span class="pr-prompt">${esc((run.prompt || '').slice(0, 140))}</span></div>` : '';
    const stopBtn = ps.working && run ? `<button class="btn sm danger" data-stop data-run-uid="${esc(run.run_uid)}" title="Cancel this run">■ Stop</button>` : '';
    const card = el('div', { class: 'card clickable proj-card' + stateCls, dataset: { slug: p.slug } },
        `<div class="pc-top">
            <div><div class="pc-name">${fav}${esc(p.name)}</div><div class="pc-path">${esc(p.path)}</div></div>
            <div class="pc-flags">${pip}</div>
         </div>
         <div class="pc-desc">${esc(p.description || 'No description.')}</div>
         ${running}
         <div class="pc-meta">
            <span class="badge ${badge}">${esc(badgeText)}</span>
            <span>◧ ${p.session_count || 0} sessions</span>
            <span>◉ ${p.memory_count || 0} facts</span>
            <span>▤ ${p.chunk_count || 0} chunks</span>
         </div>
         <div class="pc-actions">
            <button class="btn sm primary" data-tui title="Run OpenCode in this project's directory">▶ Run</button>
            ${stopBtn}
            <button class="btn sm" data-open>Open</button>
            <button class="btn sm ghost" data-index>Index docs</button>
            ${delBtn}
         </div>`);
    card.addEventListener('click', (e) => {
        const stop = e.target.closest('[data-stop]');
        if (stop) { e.stopPropagation(); cancelProjectRun(stop.dataset.runUid); return; }
        if (e.target.closest('[data-del]')) { e.stopPropagation(); deleteProject(p); return; }
        if (e.target.closest('[data-index]')) { indexProject(p); return; }
        if (e.target.closest('[data-tui]')) {
            e.stopPropagation();
            openProjectTui(p);
            return;
        }
        goto('#/project/' + p.slug);
    });
    return card;
}

async function openProjectTui(p) {
    try {
        if (!p || !p.path) {
            toast('This project has no folder path. Re-register or rediscover it.', 'err');
            return;
        }
        // Project-card launch behaves like running `opencode` in the project
        // directory: no forced prompt, so OpenCode uses its own session/model
        // resolution instead of starting a fresh chat.
        const res = await api('/api/runs/tui', {
            method: 'POST',
            body: { project: p.slug, model: p.model, agent: p.agent },
        });
        const runUid = res.run && res.run.run_uid;
        if (!runUid) { throw new Error('Perun did not return a run id'); }
        await launchInTerminal({ project: p.slug, cwd: p.path, model: p.model, agent: p.agent, tui: true, runUid });
        await refreshPresence();
        if (runUid) {
            // Optimistically show working state so the card lights up immediately.
            const card = document.querySelector(`.proj-card[data-slug="${CSS.escape(p.slug)}"]`);
            if (card) {
                card.classList.add('is-working');
                card.classList.remove('is-awake');
                const pip = card.querySelector('.card-pip');
                if (pip) { pip.classList.add('working'); pip.classList.remove('awake'); }
                const lbl = pip && pip.querySelector('[data-pip-label]');
                if (lbl) lbl.textContent = 'working';
            }
        }
    } catch (e) { toast(e.message, 'err'); }
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
    }
    try {
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

    const detailsStep = () => `
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
            <label class="field"><span class="lab">Default model</span><input type="text" data-field="model" value="${esc(wiz.model)}" placeholder="provider/model (optional)"></label>
            <label class="field"><span class="lab">Agent</span><input type="text" data-field="agent" value="${esc(wiz.agent)}" placeholder="build (optional)"></label>
        </div>
        <label class="field"><span class="lab">Tags</span><input type="text" data-field="tags" value="${esc(wiz.tags)}" placeholder="python, trading (optional)"></label>
        <div class="wiz-options">
            ${wiz.method === 'new' ? `<label class="opt"><input type="checkbox" data-field="bootstrap" ${wiz.bootstrap ? 'checked' : ''}> Bootstrap starter files (README, AGENTS.md, opencode.json)</label>` : ''}
            ${wiz.method !== 'clone' ? `<label class="opt"><input type="checkbox" data-field="git_init" ${wiz.git_init ? 'checked' : ''}> Initialise a Git repository</label>` : ''}
            <label class="opt"><input type="checkbox" data-field="index_docs" ${wiz.index_docs ? 'checked' : ''}> Index documentation now</label>
        </div>
        ${wiz.method !== 'clone' ? `
        <div class="form-row">
            <label class="field"><span class="lab">Git remote (optional)</span><input type="text" data-field="git_remote" value="${esc(wiz.git_remote)}" placeholder="https://github.com/you/repo.git"></label>
            <label class="field"><span class="lab">Branch</span><input type="text" data-field="git_branch" value="${esc(wiz.git_branch)}" placeholder="main"></label>
        </div>` : ''}`;

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

    const render = () => {
        const body = el('div', { class: 'wizard' });
        body.appendChild(el('div', { class: 'wiz-steps' }, STEPS.map((s, i) =>
            `<span class="wiz-step ${i === wiz.step ? 'active' : ''} ${i < wiz.step ? 'done' : ''}"><b>${i + 1}</b> ${esc(s)}</span>`).join('')));
        const pane = el('div', { class: 'wiz-pane' });
        pane.innerHTML = [methodStep, locationStep, detailsStep, reviewStep][wiz.step]();
        body.appendChild(pane);

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
    if (!payload.path) { toast('Folder path is required', 'err'); return; }
    try {
        const res = await api('/api/projects', { method: 'POST', body: payload });
        closeModal();
        toast(res.created ? 'Project created' : 'Project registered', 'ok');
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

    const tabDefs = [
        ['overview', 'Overview'], ['tasks', 'Tasks'], ['console', 'Console'], ['sessions', 'Sessions'],
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
        const expected = `#/project/${slug}/${name}`;
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
        if (p.has_manifest) {
            wrap.appendChild(manifestOverview(p));
        }
        wrap.appendChild(sectionTitle('Recent runs'));
        const box = el('div', { class: 'card' });
        const list = el('div', { class: 'list' });
        for (const r of (detail.runs || []).slice(0, 8)) {
            list.appendChild(el('div', { class: 'list-row' },
                `<span class="badge ${STATUS_BADGE[r.status] || 'muted'}">${esc(r.status)}</span>
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

function projectConfigForm(p) {
    const isControl = !!p.is_control;
    const delBtn = isControl ? '' : '<button class="btn danger" id="pc-delete">Delete project</button>';
    const note = isControl
        ? '<p class="muted mt">This is the Perun orchestrator (control plane). It cannot be removed.</p>'
        : '';
    const card = el('div', { class: 'card' },
        `${isControl ? '<div class="card-head"><h4>Orchestrator</h4><span class="badge gold">control plane</span></div>' : ''}
         <label class="field"><span class="lab">Name</span><input type="text" id="pc-name" value="${esc(p.name)}"></label>
         <label class="field"><span class="lab">Description</span><input type="text" id="pc-desc" value="${esc(p.description || '')}"></label>
         <div class="form-row">
            <label class="field"><span class="lab">Model override</span><span id="pc-model-host"></span></label>
            <label class="field"><span class="lab">Agent override</span><span id="pc-agent-host"></span></label>
         </div>
         ${note}
         <div class="form-actions">
            <button class="btn primary" id="pc-save">Save</button>
            ${delBtn}
         </div>`);
    const mModel = modelSelect({ placeholder: '(use default)', initial: p.model || '' });
    card.querySelector('#pc-model-host').appendChild(mModel);
    const mAgent = agentSelect({ placeholder: '(use default)', initial: p.agent || '' });
    card.querySelector('#pc-agent-host').appendChild(mAgent);
    if (!state.agents.length) loadAgents().then(() => mAgent.refresh?.()).catch(() => {});
    card.querySelector('#pc-save').addEventListener('click', async () => {
        const res = await api(`/api/projects/${p.slug}`, { method: 'PATCH', body: {
            name: $('#pc-name').value, description: $('#pc-desc').value,
            model: mModel.getValue(), agent: mAgent.getValue() } });
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
    return card;
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
/* Open a file path from a manifest anchor. We try a direct window.open to the
   OS handler (vscode://file/... or file://...). The path is normalised so it
   cannot escape the project root. */
function openManifestPath(projectRoot, relPath) {
    let clean = String(relPath || '').replace(/^[\/]+/, '').replace(/\.{2,}(?:[\/]|$)/g, '').replace(/[\/]+/g, '/');
    if (!clean) return;
    const root = String(projectRoot || '').replace(/[\/]+$/, '').replace(/\\/g, '/');
    const absolute = root + '/' + clean;
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

function manifestOverview(p) {
    const data = p.manifest || {};
    const body = p.manifest_body || '';
    const box = el('div', { class: 'card mb' });
    box.appendChild(el('div', { class: 'card-head' },
        `<h4>Manifest</h4>
         <span class="row wrap" style="gap:8px">
            <span class="badge gold">perun.md</span>
            ${p.manifest_source ? `<span class="mono muted truncate" style="max-width:260px;font-size:.72rem">${esc(p.manifest_source)}</span>` : ''}
         </span>`));

    const anchors = data.anchors || [];
    if (anchors.length) {
        box.appendChild(el('div', { class: 'subhead' }, 'Anchors'));
        const list = el('div', { class: 'anchor-list' });
        for (const a of anchors) {
            const target = a.path || a.url || a.command || '';
            const kind = a.kind || 'file';
            const isFile = kind === 'file' || kind === 'doc';
            const isUrl = kind === 'url';
            let targetHtml;
            if (isUrl) {
                targetHtml = `<a class="ai-target" href="${esc(safeUrl(target))}" target="_blank" rel="noopener noreferrer" title="${esc(target)}">${esc(target)}</a>`;
            } else if (isFile) {
                targetHtml = `<button class="ai-target" data-path="${esc(target)}" title="Open ${esc(target)}" type="button">${esc(target)}</button>`;
            } else {
                targetHtml = `<span class="ai-target">${esc(target)}</span>`;
            }
            const item = el('div', { class: 'anchor-item' },
                `<span class="ai-kind">${esc(kind)}</span>
                 <span class="ai-label">${esc(a.label || '')}</span>
                 ${targetHtml}`);
            if (isFile) {
                const btn = item.querySelector('[data-path]');
                if (btn) btn.addEventListener('click', () => openManifestPath(p.path, btn.dataset.path));
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
        for (const [k, v] of links) row.appendChild(el('a', { class: 'chip', href: safeUrl(v), target: '_blank', rel: 'noopener noreferrer' }, `<b>${esc(k)}</b>`));
        for (const [k, v] of docs) {
            const chip = el('button', { class: 'chip chip-link', title: v, type: 'button' }, `<b>${esc(k)}</b> ${esc(v)}`);
            chip.addEventListener('click', () => openManifestPath(p.path, v));
            row.appendChild(chip);
        }
        box.appendChild(row);
    }

    if (body && body.trim()) {
        box.appendChild(el('div', { class: 'subhead' }, 'Overview'));
        const md = el('div', { class: 'md-body', html: renderMarkdown(body) });
        box.appendChild(md);
        // Make relative/local links in the overview body open via the OS handler.
        md.querySelectorAll('a[href]').forEach((a) => {
            const href = a.getAttribute('href') || '';
            if (!/^https?:\/\//i.test(href) && !/^mailto:/i.test(href)) {
                a.addEventListener('click', (e) => {
                    e.preventDefault();
                    openManifestPath(p.path, href);
                });
            }
        });
    }
    return box;
}

async function mountManifest(container, p) {
    container.innerHTML = '<div class="empty"><span class="spinner"></span> loading manifest…</div>';
    let detail, snap;
    try {
        detail = await api(`/api/projects/${p.slug}`);
        snap = (await api(`/api/projects/${p.slug}/manifest`)).manifest;
    }
    catch (e) { container.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    container.innerHTML = '';
    // Show the same read-only manifest card that appears on the Overview tab.
    if (detail.project && detail.project.has_manifest) {
        container.appendChild(manifestOverview(detail.project));
    }
    renderManifestEditor(container, p, snap || {});
}

function renderManifestEditor(container, p, snap) {
    container.innerHTML = '';
    const found = !!snap.found;
    const data = snap.data || {};
    const body = snap.body || '';
    const warnings = snap.warnings || [];

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
        reload.addEventListener('click', async () => {
            try {
                const r = await api(`/api/projects/${p.slug}/manifest/refresh`, { method: 'POST' });
                toast('Reloaded from disk', 'ok');
                await loadProjects();
                renderManifestEditor(container, p, r.manifest);
            } catch (e) { toast(e.message, 'err'); }
        });
        actions.appendChild(reload);
    } else {
        const gen = el('button', { class: 'btn primary' }, '＋ Generate from registry');
        gen.addEventListener('click', async () => {
            try {
                const r = await api(`/api/projects/${p.slug}/manifest/export`, { method: 'POST' });
                toast('Manifest generated', 'ok');
                await loadProjects();
                renderManifestEditor(container, p, r.manifest);
            } catch (e) { toast(e.message, 'err'); }
        });
        actions.appendChild(gen);
    }
    status.appendChild(actions);
    container.appendChild(status);

    // ---- editor ----
    const card = el('div', { class: 'card' });
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
    for (const a of (data.anchors || [])) addAnchorRow(anchorHost, a);
    const anchorAdd = el('button', { class: 'btn sm ghost' }, '＋ Add anchor');
    anchorAdd.addEventListener('click', () => addAnchorRow(anchorHost, { kind: 'file' }));
    card.appendChild(anchorAdd);

    card.appendChild(el('div', { class: 'subhead' }, 'Overview body (Markdown)'));
    const ta = el('textarea', { id: 'mf-body', rows: '10', placeholder: '# Project overview\n\nFree-form Markdown shown on the project page.' });
    ta.value = body;
    card.appendChild(ta);

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
        const open = el('button', { class: 'icon-btn', title: 'Open file', type: 'button' }, '↗');
        open.addEventListener('click', () => openManifestPath(projectRoot, v.value));
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

function addAnchorRow(host, a) {
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
            <button class="btn ghost sm" id="orch-new">＋ New session</button>
         </div>`));

    const msModel = modelSelect({
        value: s.orchestrator_model || s.default_model || '',
        placeholder: 'choose model',
        onSelect: (id) => persistOrchModel(id),
    });
    v.querySelector('#orch-model-host').appendChild(msModel);

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
        bubble('assistant', 'Ask me anything about your projects — or tell me to create, launch, or remove one. Use the model picker above to choose which model I run on.');
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
                agent: s.orchestrator_agent || '',
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
                const o = el('option', { value: s.id }, esc(`${(s.title || '(untitled)').slice(0, 40)} · ${timeAgo(s.updated_at)}`));
                o.dataset.oc = s.opencode_session_id || '';
                sel.appendChild(o);
            }
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
            const res = await api('/api/runs/tui', {
                method: 'POST',
                body: { project: slug, prompt, model: p.model, agent: p.agent },
            });
            const runUid = res && res.run && res.run.run_uid;
            if (!runUid) { throw new Error('Perun did not return a run id'); }
            currentRun = runUid;
            runBtn.disabled = true; stopBtn.disabled = false;
            await refreshPresence();
            perunFx.flash($('#perun-sigil'));
            const sessionOpt = $('#c-session').selectedOptions[0];
            const sessionId = sessionOpt ? (sessionOpt.dataset.oc || '') : '';
            if (!p.path) { toast('Selected project has no folder path.', 'err'); return; }
            await launchInTerminal({ project: p.slug, cwd: p.path, model: p.model, agent: p.agent, prompt, tui: true, runUid: currentRun, sessionId });
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
                const [recent, stats] = await Promise.all([
                    api('/api/memory/recent' + params),
                    api('/api/memory/stats' + (project ? `?project=${encodeURIComponent(project)}` : '')),
                ]);
                results.innerHTML = '';
                results.appendChild(el('div', { class: 'grid stats-grid mb' },
                    `<div class="card stat"><div class="stat-label">Facts</div><div class="stat-value">${stats.stats.facts_total ?? 0}</div></div>
                     <div class="card stat"><div class="stat-label">Doc chunks</div><div class="stat-value">${stats.stats.chunks_total ?? 0}</div></div>
                     <div class="card stat"><div class="stat-label">Embedded</div><div class="stat-value">${stats.stats.facts_embedded ?? 0}</div></div>`));
                results.appendChild(sectionTitle(project ? 'Recent facts for project' : 'Recent facts'));
                results.appendChild(memoryList(recent.facts || []));
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

    // Initial load uses the default “All scopes” selection.
    await loadData();
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
        list.appendChild(el('div', { class: 'list-row' },
            `<span class="badge ${f.scope === 'global' ? 'gold' : 'muted'}">${esc(f.scope)}</span>
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
             <label class="field" style="flex:1"><span class="lab">Chat agent <span class="muted">(optional override)</span></span>
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
    const chatAgentSel = agentSelect({ value: settings.perunchat_chat_agent || '', placeholder: 'orchestrator / default agent' });
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
    async function refreshRooms() {
        if (!status.configured) return;
        try {
            const r = await api('/api/perunchat/rooms');
            roomCatalog = (r.rooms || []).map((x) => ({
                value: x.room_id,
                label: x.room_id,
                description: [
                    x.active ? 'live' : '',
                    x.joined ? 'joined' : '',
                    x.members ? `${x.members} member${x.members === 1 ? '' : 's'}` : '',
                ].filter(Boolean).join(' · ') || undefined,
            }));
            // Keep rooms that are configured but not in the gateway catalog.
            const known = new Set(roomCatalog.map((x) => x.value));
            for (const rid of autoJoinRooms) {
                if (!known.has(rid)) roomCatalog.push({ value: rid, label: rid, description: 'configured' });
            }
            joinSel.setItems(roomCatalog);
            refreshRoomSelect();
        } catch (e) { /* room catalog is optional */ }
    }

    $('#pc-add-room').addEventListener('click', () => {
        const rid = $('#pc-custom-room').value.trim();
        if (!rid) return;
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
        const autoJoin = joinSel.getValue();
        const chatModel = chatModelSel.getValue();
        const chatAgent = chatAgentSel.getValue();
        const body = {
            perunchat_api_endpoint: endpoint,
            perunchat_auto_reply: autoReply,
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

    v.appendChild(grid);

    // ---- Chat panel --------------------------------------------------------
    const chatCard = el('div', { class: 'card mt' },
        `<div class="card-head"><h4>Chat</h4><span id="pc-ws-status" class="badge">disconnected</span></div>
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
                html += `<option value="${esc(item.value)}">${esc(item.value)}${active}</option>`;
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
            for (const id of ['pc-friends-list', 'pc-incoming', 'pc-outgoing', 'pc-blocked-list']) {
                const box = document.getElementById(id);
                if (box) box.innerHTML = pcPlaceholder;
            }
            return;
        }
        try {
            const [friends, incoming, outgoing, fstatus, blocked] = await Promise.all([
                api('/api/perunchat/social/friends'),
                api('/api/perunchat/social/friends/requests?direction=incoming'),
                api('/api/perunchat/social/friends/requests?direction=outgoing'),
                api('/api/perunchat/social/friends/status'),
                api('/api/perunchat/social/blocked'),
            ]);
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
        } catch (e) { toast(e.message, 'err'); }
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
    const MAX_HISTORY = 200;
    const MAX_STORED_HISTORY = 100;

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
            messageHistory.push(...stored.slice(-MAX_HISTORY));
            const box = $('#pc-chat-log');
            box.innerHTML = '';
            for (const msg of messageHistory) renderMessage(msg);
        } catch (e) { /* ignore corrupt storage */ }
    }

    const log = (html, meta) => {
        const box = $('#pc-chat-log');
        const line = el('div', {}, html);
        if (meta) line._meta = meta;
        box.appendChild(line);
        box.scrollTop = box.scrollHeight;
    };
    const setWsStatus = (text, cls) => {
        const badge = $('#pc-ws-status');
        badge.textContent = text;
        badge.className = 'badge ' + (cls || '');
    };

    function renderMessage(msg) {
        if (!msg.id) return;
        if (messageHistory.some((m) => m.id === msg.id)) return;
        messageHistory.push(msg);
        if (messageHistory.length > MAX_HISTORY) messageHistory.shift();
        persistChat();

        const own = msg.sender && msg.sender.agent_id ? esc(msg.sender.agent_id) : '?';
        const ts = msg.timestamp ? new Date(msg.timestamp).toLocaleTimeString() : '';
        let replyHtml = '';
        if (msg.in_reply_to) {
            const original = messageHistory.find(m => m.id === msg.in_reply_to);
            const replyToWho = original ? esc(original.sender.agent_id) : 'unknown';
            const replyToText = original ? esc(String(original.payload).slice(0, 60)) : '…';
            replyHtml = `<div style="font-size:12px; color:var(--text-muted); margin-left:12px; border-left:2px solid var(--border-strong); padding-left:8px;">↩ replying to @${replyToWho}: ${replyToText}</div>`;
        }

        const lineHtml = `<div style="margin-bottom:4px;">
            <div class="row" style="gap:10px; align-items:baseline;">
                <span style="color:var(--gold); font-weight:600;">@${own}</span>
                <span class="muted" style="font-size:11px;">${esc(ts)}</span>
                <button class="btn ghost small" data-reply-id="${esc(msg.id)}" data-reply-sender="${own}">Reply</button>
            </div>
            ${replyHtml}
            <div style="margin-left:12px; overflow-wrap:anywhere;">${esc(String(msg.payload))}</div>
        </div>`;
        log(lineHtml, { id: msg.id });

        // Attach listener to the reply button of the line we just appended.
        const box = $('#pc-chat-log');
        const btn = box.querySelector(`[data-reply-id="${CSS.escape(msg.id)}"]`);
        if (btn) {
            btn.addEventListener('click', () => {
                replyToId = msg.id;
                replyToPreview = `@${own}: ${String(msg.payload).slice(0, 40)}`;
                const bar = $('#pc-reply-bar');
                if (bar) bar.innerHTML = `↩ ${esc(replyToPreview)} <button class="btn ghost small" id="pc-clear-reply">Cancel</button>`;
                bar.style.display = 'block';
                bar.querySelector('#pc-clear-reply').addEventListener('click', clearReply);
                $('#pc-chat-msg').focus();
            });
        }
    }

    function clearReply() {
        replyToId = null;
        replyToPreview = null;
        const bar = $('#pc-reply-bar');
        if (bar) { bar.innerHTML = ''; bar.style.display = 'none'; }
    }

    async function connectChat() {
        if (ws) { ws.close(); ws = null; }
        let key, endpoint;
        try {
            // The PerunChat gateway accepts the API key via query parameter because
            // browser WebSocket cannot set custom headers. The key is revealed only
            // to loopback clients.
            const k = await api('/api/perunchat/key');
            key = encodeURIComponent(k.key);
            const s = await api('/api/perunchat/status');
            endpoint = `${s.endpoint}${s.endpoint.includes('?') ? '&' : '?'}token=${key}`;
        } catch (e) { toast('Could not load chat credentials: ' + e.message, 'err'); return; }
        try {
            ws = new WebSocket(endpoint);
            ws.onopen = () => {
                setWsStatus('connected', 'green');
                log('<span class="muted">Connected to PerunChat gateway.</span>');
                if (joinedRoom) {
                    sendFrame({ type: 'ROOM_JOIN', room_id: joinedRoom, room_type: joinedRoomType, timestamp: Date.now() });
                }
            };
            ws.onclose = () => { setWsStatus('disconnected', ''); log('<span class="muted">Disconnected.</span>'); ws = null; };
            ws.onerror = (e) => { log('<span style="color:var(--danger)">WebSocket error.</span>'); };
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
        } catch (e) { toast('Could not open WebSocket: ' + e.message, 'err'); }
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

    $('#pc-chat-send').addEventListener('click', () => {
        if (!joinedRoom) { toast('Join a room first', 'err'); return; }
        const text = $('#pc-chat-msg').value;
        if (!text.trim()) return;
        seq += 1;
        try { localStorage.setItem('perunchat_seq', String(seq)); } catch (e) { /* ignore */ }
        const now = Date.now();
        const frame = {
            id: `msg_${now}`,
            trace_id: `trc_${now}`,
            thread_id: 'default',
            sequence_number: seq,
            timestamp: now,
            room_type: $('#pc-chat-type').value,
            room_id: joinedRoom,
            sender: {},
            recipient_id: null,
            content_type: 'text/plain',
            turn_count: 0,
            payload: text,
        };
        if (replyToId) frame.in_reply_to = replyToId;
        // The gateway now echoes every chat frame back to the sender, so the
        // message will appear once the round-trip completes.
        sendFrame(frame);
        $('#pc-chat-msg').value = '';
        clearReply();
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

    if (pcConfigured) {
        connectChat();
    } else {
        log('<span class="muted">Add your PerunChat API key above and save, to enable live chat.</span>');
    }
}

/* ========================================================= PROVIDERS ==== */
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
         <div class="sub">Connection status from OpenCode auth. A provider shown as “saved” has credentials stored by OpenCode; Perun never stores API keys.</div></div>
         <div class="form-actions"><button class="btn ghost" id="pr-refresh">⟳ Refresh</button></div>`));

    const providers = data.providers || [];
    if (!providers.length) {
        v.appendChild(el('div', { class: 'card empty' },
            data.error ? esc(data.error) : 'No providers reported by OpenCode.'));
        return;
    }

    const grid = el('div', { class: 'grid providers-grid' });
    for (const p of providers) {
        const connected = !!p.connected;
        const statusClass = connected ? 'ok' : 'warn';
        const statusText = connected ? 'Saved credential' : 'No credential';
        const card = el('div', { class: 'card provider-card' },
            `<div class="card-head"><h4>${esc(p.name || p.id)}</h4><span class="badge ${statusClass}">${statusText}</span></div>
             <div class="provider-meta">
                <span class="mono">${esc(p.id)}</span>
                ${p.connections && p.connections.length ? `<span class="muted">${esc(p.connections.map((c) => c.label || c.method || c.type).join(', '))}</span>` : ''}
             </div>
             <div class="form-actions">
                ${connected
                    ? `<button class="btn ghost" data-action="logout" data-id="${esc(p.id)}">Disconnect</button>`
                    : `<button class="btn primary" data-action="login" data-id="${esc(p.id)}">Connect…</button>`}
             </div>`);
        grid.appendChild(card);
    }
    v.appendChild(grid);

    $('#pr-refresh').addEventListener('click', async () => {
        await viewProviders(true);
    });

    v.querySelectorAll('button[data-action="login"]').forEach((b) => {
        b.addEventListener('click', () => {
            const id = b.dataset.id;
            const cmd = `opencode auth login ${esc(id)}`;
            confirmDialog('Connect provider', `Open a terminal to run <code>${cmd}</code>? Perun will not see your API key.`, async () => {
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
    v.querySelectorAll('button[data-action="logout"]').forEach((b) => {
        b.addEventListener('click', async () => {
            const id = b.dataset.id;
            try {
                await api('/api/providers/' + encodeURIComponent(id) + '/logout', { method: 'POST' });
                toast('Disconnected', 'ok');
                await viewProviders();
            } catch (e) {
                toast(e.message, 'err');
            }
        });
    });
}

/* ========================================================= SETTINGS ===== */
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
            <label class="field"><span class="lab">Ollama host</span><input type="text" id="s-ollama" value="${esc(s.ollama_host)}"></label>
         </div>
         <div class="row wrap" style="gap:18px;margin:6px 0 4px">
            <label class="row" style="gap:6px"><input type="checkbox" id="s-autodisc" ${s.auto_discover_on_start ? 'checked' : ''}> auto-discover on start</label>
            <label class="row" style="gap:6px"><input type="checkbox" id="s-indexdisc" ${s.index_docs_on_discover ? 'checked' : ''}> index docs on discover</label>
         </div>
         <div class="form-actions">
            <button class="btn primary" id="s-save">Save settings</button>
            <button class="btn ghost" id="s-discover">⟳ Discover now</button>
            <button class="btn ghost" id="s-indexall">▤ Index all docs</button>
         </div>`);
    v.appendChild(card);

    await loadModels();
    await loadAgents();
    const msDefault = modelSelect({ value: s.default_model || '', placeholder: 'provider/model' });
    card.querySelector('#s-model-host').appendChild(msDefault);
    const sAgent = agentSelect({ value: s.default_agent || '', placeholder: '(none)' });
    card.querySelector('#s-agent-host').appendChild(sAgent);
    const sOrchAgent = agentSelect({ value: s.orchestrator_agent || '', placeholder: '(none)' });

    // ---- orchestrator (control plane) ----
    v.appendChild(sectionTitle('Orchestrator'));
    const orchCard = el('div', { class: 'card' },
        `<div class="card-head"><h4>Perun Orchestrator</h4><span class="badge gold">machine-wide</span></div>
         <p class="secondary">The orchestrator is the agent you talk to when <b>no project is selected</b> — from the <a href="#/orchestrator">Orchestrator chat</a> or Telegram. Its model and agent apply machine-wide; add custom instructions in the <b>Agent instructions</b> files below with scope <b>orchestrator</b>.</p>
         <div class="form-row">
            <label class="field"><span class="lab">Orchestrator model</span><span id="s-orch-model-host"></span></label>
            <label class="field"><span class="lab">Orchestrator agent</span><span id="s-orch-agent-host"></span></label>
         </div>
         <div class="form-actions">
            <button class="btn primary" id="s-orch-save">Save orchestrator</button>
            <button class="btn ghost" id="s-orch-open">▶ Open orchestrator chat</button>
         </div>`);
    v.appendChild(orchCard);
    const msOrch = modelSelect({ value: s.orchestrator_model || '', placeholder: 'inherit default model' });
    orchCard.querySelector('#s-orch-model-host').appendChild(msOrch);
    orchCard.querySelector('#s-orch-agent-host').appendChild(sOrchAgent);

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
            ollama_host: $('#s-ollama').value, embed_model: $('#s-embed').value || $('#s-embed-custom').value.trim(),
            auto_discover_on_start: $('#s-autodisc').checked, index_docs_on_discover: $('#s-indexdisc').checked,
        };
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
}

/* ========================================================== BOOT ======= */
function wireChrome() {
    document.querySelectorAll('.nav-item').forEach((n) => n.addEventListener('click', () => goto(n.dataset.route)));
    $('#btn-refresh').addEventListener('click', router);
    $('#btn-quick-run').addEventListener('click', () => goto('#/console'));
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
    setInterval(refreshHealth, 15000);
    setInterval(refreshPresence, 4000);
    window.addEventListener('hashchange', router);
}

document.addEventListener('DOMContentLoaded', boot);
