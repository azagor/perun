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

const state = {
    projects: [],
    settings: null,
    health: null,
    activeRuns: [],
    runSource: null,
    activitySource: null,
};

/* ---------------------------------------------------------------- api ---- */
async function api(path, opts = {}) {
    const init = { headers: {} };
    if (opts.method) init.method = opts.method;
    if (opts.body !== undefined) {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, init);
    let data = null;
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) {
        const detail = (data && (data.detail || data.reason)) || res.statusText;
        throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
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
    back.addEventListener('mousedown', (e) => { if (e.target === back) root.innerHTML = ''; });
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
const KIND_BADGE = { opencode: 'gold', git: 'blue', dir: 'muted' };
const STATUS_BADGE = { done: 'green', queued: 'muted', running: 'amber', failed: 'red', cancelled: 'red', timeout: 'red' };

/* --------------------------------------------------------------- data --- */
async function loadProjects() { state.projects = (await api('/api/projects')).projects || []; }
async function loadHealth() { state.health = await api('/api/health'); }
async function loadSettings() { state.settings = (await api('/api/settings')).settings; }

function projectBySlug(slug) { return state.projects.find((p) => p.slug === slug); }

/* ============================================================ ROUTER ==== */
const routes = {
    '#/dashboard': viewDashboard,
    '#/projects': viewProjects,
    '#/console': viewConsole,
    '#/memory': viewMemory,
    '#/activity': viewActivity,
    '#/telegram': viewTelegram,
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
    if (base === '#/project') { fn = viewProject; args = [sub]; }
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
    const v = $('#view');
    v.innerHTML = '';
    const h = state.health;
    const mem = await api('/api/memory/stats');
    const stats = mem.stats || {};

    const head = el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Overseer</div><h1>Command Dashboard</h1>
         <div class="sub">Every project, every agent, every memory — in one place.</div></div>`);
    v.appendChild(head);

    const grid = el('div', { class: 'grid stats-grid' });
    const stat = (label, value, foot) => el('div', { class: 'card stat' },
        `<div class="stat-label">${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${esc(foot || '')}</div>`);
    const opencodeCount = state.projects.filter((p) => p.kind === 'opencode').length;
    grid.appendChild(stat('Projects', state.projects.length, `${opencodeCount} OpenCode project(s)`));
    grid.appendChild(stat('Active runs', (h.active_runs || []).length, h.active_runs && h.active_runs.length ? 'agent(s) working' : 'idle'));
    grid.appendChild(stat('Facts', stats.facts_total ?? 0, `${stats.facts_global ?? 0} global`));
    grid.appendChild(stat('Doc chunks', stats.chunks_total ?? 0, 'semantic index'));
    grid.appendChild(stat('Embeddings', stats.ollama_ok ? 'Online' : 'Offline', stats.ollama_ok ? 'Ollama ready' : 'start ollama serve'));
    v.appendChild(grid);

    // Active runs
    if (h.active_runs && h.active_runs.length) {
        v.appendChild(sectionTitle('Active runs'));
        const box = el('div', { class: 'card' });
        const list = el('div', { class: 'list' });
        for (const uid of h.active_runs) {
            list.appendChild(el('div', { class: 'list-row' },
                `<span class="badge amber">running</span><span class="grow mono">${esc(uid)}</span>
                 <button class="btn sm ghost" data-run="${esc(uid)}">View</button>`));
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

    v.querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', () => goto('#/console')));
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
    const badge = KIND_BADGE[p.kind] || 'muted';
    const fav = p.favorite ? '★ ' : '';
    const card = el('div', { class: 'card clickable proj-card' },
        `<div class="pc-top">
            <div><div class="pc-name">${fav}${esc(p.name)}</div><div class="pc-path">${esc(p.path)}</div></div>
            <span class="badge ${badge}">${esc(p.kind)}</span>
         </div>
         <div class="pc-desc">${esc(p.description || 'No description.')}</div>
         <div class="pc-meta">
            <span>◧ ${p.session_count || 0} sessions</span>
            <span>◉ ${p.memory_count || 0} facts</span>
            <span>▤ ${p.chunk_count || 0} chunks</span>
         </div>
         <div class="pc-actions">
            <button class="btn sm primary" data-open>Open</button>
            <button class="btn sm" data-ask>Ask</button>
            <button class="btn sm ghost" data-index>Index docs</button>
         </div>`);
    card.addEventListener('click', (e) => {
        if (e.target.closest('[data-ask]')) { goto('#/console/' + p.slug); return; }
        if (e.target.closest('[data-index]')) { indexProject(p); return; }
        goto('#/project/' + p.slug);
    });
    return card;
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
    $('#btn-add').addEventListener('click', addProjectModal);
    $('#btn-discover').addEventListener('click', async () => {
        try {
            const res = await api('/api/discover', { method: 'POST', body: {} });
            toast(`Discovered: ${(res.created || []).length} new, ${(res.updated || []).length} updated`, 'ok');
            await loadProjects();
            render($('#proj-filter').value);
        } catch (e) { toast(e.message, 'err'); }
    });
}

function addProjectModal() {
    const body = el('div', {},
        `<label class="field"><span class="lab">Name</span><input type="text" id="np-name" placeholder="My Project"></label>
         <label class="field"><span class="lab">Path</span><input type="text" id="np-path" placeholder="C:\\my_apps\\my_project"></label>
         <label class="field"><span class="lab">Description</span><input type="text" id="np-desc" placeholder="optional"></label>
         <div class="form-row">
            <label class="field"><span class="lab">Model (optional)</span><input type="text" id="np-model" placeholder="provider/model"></label>
            <label class="field"><span class="lab">Agent (optional)</span><input type="text" id="np-agent" placeholder="build"></label>
         </div>`);
    modal('Add project', body, [
        { label: 'Cancel', class: 'ghost', onClick: closeModal },
        { label: 'Add', class: 'primary', onClick: async () => {
            const payload = { name: $('#np-name').value, path: $('#np-path').value, description: $('#np-desc').value, model: $('#np-model').value, agent: $('#np-agent').value };
            if (!payload.path) { toast('Path is required', 'err'); return; }
            try { await api('/api/projects', { method: 'POST', body: payload }); closeModal(); toast('Project registered', 'ok'); await loadProjects(); router(); }
            catch (e) { toast(e.message, 'err'); }
        } },
    ]);
}

/* ===================================================== PROJECT DETAIL === */
async function viewProject(slug) {
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
        ['overview', 'Overview'], ['console', 'Console'], ['sessions', 'Sessions'],
        ['memory', 'Memory'], ['activity', 'Activity'], ['config', 'Config'],
    ];
    const tabs = el('div', { class: 'tabs' });
    const pane = el('div');
    let active = 'overview';
    const setTab = (name) => {
        active = name;
        tabs.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
        pane.innerHTML = '';
        pane.appendChild(renderProjectTab(name, detail));
        if (name === 'console') mountConsole(pane, p);
    };
    for (const [name, label] of tabDefs) tabs.appendChild(el('div', { class: 'tab', dataset: { tab: name }, onclick: () => setTab(name) }, label));
    v.appendChild(tabs);
    v.appendChild(pane);
    setTab('overview');

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
    } else if (name === 'console') {
        // mounted separately
    } else if (name === 'config') {
        wrap.appendChild(projectConfigForm(p));
    }
    return wrap;
}

function projectConfigForm(p) {
    const body = el('div', { class: 'card' },
        `<label class="field"><span class="lab">Name</span><input type="text" id="pc-name" value="${esc(p.name)}"></label>
         <label class="field"><span class="lab">Description</span><input type="text" id="pc-desc" value="${esc(p.description || '')}"></label>
         <div class="form-row">
            <label class="field"><span class="lab">Model override</span><input type="text" id="pc-model" value="${esc(p.model || '')}" placeholder="provider/model"></label>
            <label class="field"><span class="lab">Agent override</span><input type="text" id="pc-agent" value="${esc(p.agent || '')}" placeholder="build"></label>
         </div>
         <div class="form-actions">
            <button class="btn primary" id="pc-save">Save</button>
            <button class="btn danger" id="pc-delete">Delete project</button>
         </div>`);
    body.querySelector('#pc-save').addEventListener('click', async () => {
        await api(`/api/projects/${p.slug}`, { method: 'PATCH', body: {
            name: $('#pc-name').value, description: $('#pc-desc').value,
            model: $('#pc-model').value, agent: $('#pc-agent').value } });
        toast('Saved', 'ok'); await loadProjects(); viewProject(p.slug);
    });
    body.querySelector('#pc-delete').addEventListener('click', () => {
        confirmDialog('Delete project', `Remove "${p.name}" from Perun? Memory and indexed docs for it are deleted. The files on disk are untouched.`, async () => {
            await api(`/api/projects/${p.slug}`, { method: 'DELETE' });
            toast('Deleted', 'ok'); await loadProjects(); goto('#/projects');
        }, 'Delete');
    });
    return body;
}

/* ========================================================= CONSOLE ====== */
async function viewConsole(slug) {
    $('#topbar-title').textContent = 'Agent Console';
    await loadProjects();
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Orchestrator</div><h1>Agent Console</h1>
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
    const grid = el('div', { class: 'console-grid' });
    const left = el('div', { class: 'card' },
        `<label class="field"><span class="lab">Project</span><select id="c-project">${options}</select></label>
         <label class="field"><span class="lab">Session</span><select id="c-session"><option value="">＋ New session</option></select></label>
         <div class="form-row">
            <label class="field"><span class="lab">Model</span><input type="text" id="c-model" placeholder="(project default)"></label>
            <label class="field"><span class="lab">Agent</span><input type="text" id="c-agent" placeholder="(project default)"></label>
         </div>
         <label class="field"><span class="lab">Prompt</span><textarea id="c-prompt" placeholder="What should the agent do?"></textarea></label>
         <div class="form-actions">
            <button class="btn primary" id="c-run">▶ Run</button>
            <button class="btn danger" id="c-stop" disabled>■ Stop</button>
            <button class="btn ghost" id="c-clear">Clear output</button>
         </div>`);
    const term = el('div', { class: 'terminal', id: 'c-term' }, `<div class="term-line sys">Ready. Select a project and send a prompt.</div>`);
    grid.appendChild(left);
    grid.appendChild(term);
    host.appendChild(grid);

    let currentRun = null;

    const termEl = () => $('#c-term');
    const line = (text, cls = '') => {
        const d = el('div', { class: 'term-line ' + cls }, esc(text));
        const t = termEl(); t.appendChild(d); t.scrollTop = t.scrollHeight;
        return d;
    };

    async function loadSessions() {
        const slug = $('#c-project').value;
        const sel = $('#c-session');
        sel.innerHTML = '<option value="">＋ New session</option>';
        if (!slug) return;
        try {
            const detail = await api(`/api/projects/${slug}/sessions`);
            for (const s of detail.sessions || []) {
                const o = el('option', { value: s.id }, `${(s.title || '(untitled)').slice(0, 40)} · ${timeAgo(s.updated_at)}`);
                o.dataset.oc = s.opencode_session_id || '';
                sel.appendChild(o);
            }
        } catch (_) { /* ignore */ }
    }

    $('#c-project').addEventListener('change', loadSessions);
    loadSessions();

    const stopBtn = $('#c-stop');
    const runBtn = $('#c-run');

    runBtn.addEventListener('click', async () => {
        const slug = $('#c-project').value;
        const prompt = $('#c-prompt').value.trim();
        if (!slug) { toast('Pick a project', 'err'); return; }
        if (!prompt) { toast('Enter a prompt', 'err'); return; }
        const sessionOpt = $('#c-session').selectedOptions[0];
        const body = {
            project: slug, prompt,
            model: $('#c-model').value.trim(),
            agent: $('#c-agent').value.trim(),
            session_pk: $('#c-session').value ? Number($('#c-session').value) : null,
            opencode_session_id: sessionOpt ? (sessionOpt.dataset.oc || '') : '',
        };
        termEl().innerHTML = '';
        line('› ' + prompt, 'you');
        try {
            const res = await api('/api/runs', { method: 'POST', body });
            currentRun = res.run.run_uid;
            runBtn.disabled = true; stopBtn.disabled = false;
            line(`[run ${currentRun}] starting in ${slug}…`, 'sys');
            streamRun(currentRun);
        } catch (e) { line('error: ' + e.message, 'err'); toast(e.message, 'err'); }
    });

    stopBtn.addEventListener('click', async () => {
        if (!currentRun) return;
        await api(`/api/runs/${currentRun}/cancel`, { method: 'POST' });
        line('[cancel requested]', 'sys');
    });
    $('#c-clear').addEventListener('click', () => { termEl().innerHTML = ''; });

    function streamRun(uid) {
        if (state.runSource) state.runSource.close();
        const src = new EventSource(`/api/runs/${uid}/stream`);
        state.runSource = src;
        let toolLine = null;
        src.onmessage = (ev) => {
            let d; try { d = JSON.parse(ev.data); } catch (_) { return; }
            if (d.type === 'text') { if (d.text) line(d.text); }
            else if (d.type === 'tool') { line(`⚙ ${d.tool || 'tool'}${d.status ? ' · ' + d.status : ''}`, 'tool'); }
            else if (d.type === 'session') { line(`[session ${d.session_id}]`, 'sys'); }
            else if (d.type === 'error') { line('⚠ ' + (d.text || 'error'), 'err'); }
            else if (d.type === 'start') { line(`[cwd ${d.cwd}]`, 'sys'); }
            else if (d.type === 'end') {
                line(`[${d.status}]`, d.status === 'done' ? 'ok' : 'err');
                runBtn.disabled = false; stopBtn.disabled = true; currentRun = null;
                src.close(); state.runSource = null;
                loadSessions(); loadProjects();
            }
        };
        src.onerror = () => { /* keep open; server pings */ };
    }
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

    const doSearch = async () => {
        const q = $('#m-q').value.trim();
        if (!q) { toast('Enter a query', 'err'); return; }
        const project = $('#m-project').value || null;
        results.innerHTML = '<div class="empty"><span class="spinner"></span> searching…</div>';
        try {
            const res = await api('/api/memory/search', { method: 'POST', body: { query: q, project, k: 8, all_projects: !project } });
            renderResults(results, res.results || []);
        } catch (e) { results.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    };
    $('#m-search').addEventListener('click', doSearch);
    $('#m-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
    $('#m-import').addEventListener('click', async () => {
        try { const r = await api('/api/memory/import-legacy', { method: 'POST', body: {} }); toast(`Imported ${r.imported || 0} fact(s)`, 'ok'); }
        catch (e) { toast(e.message, 'err'); }
    });

    const recent = await api('/api/memory/recent?limit=40');
    const stats = await api('/api/memory/stats');
    v.appendChild(el('div', { class: 'grid stats-grid mb' },
        `<div class="card stat"><div class="stat-label">Facts</div><div class="stat-value">${stats.stats.facts_total ?? 0}</div></div>
         <div class="card stat"><div class="stat-label">Doc chunks</div><div class="stat-value">${stats.stats.chunks_total ?? 0}</div></div>
         <div class="card stat"><div class="stat-label">Embedded</div><div class="stat-value">${stats.stats.facts_embedded ?? 0}</div></div>`));
    v.appendChild(sectionTitle('Recent facts'));
    v.appendChild(memoryList(recent.facts || []));
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
                <option value="global">Global (all projects)</option>
                <option value="project" ${slug ? '' : 'disabled'}>This project</option>
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

    const src = new EventSource('/api/stream');
    state.activitySource = src;
    src.onmessage = (ev) => {
        let d; try { d = JSON.parse(ev.data); } catch (_) { return; }
        if (d.type === 'activity') {
            const row = activityRow(d);
            list.prepend(row);
            while (list.children.length > 200) list.lastChild.remove();
        }
    };
    src.onerror = () => { const b = $('#live-badge'); if (b) { b.className = 'badge red'; b.textContent = 'reconnecting'; } };
}

function activityRow(a) {
    return el('div', { class: 'list-row' },
        `<span class="badge muted">${esc(a.kind)}</span>
         <span class="grow msg">${esc(a.message)}</span>
         <span class="ts">${esc(timeAgo(a.ts))}</span>`);
}

/* ========================================================= TELEGRAM ===== */
async function viewTelegram() {
    $('#topbar-title').textContent = 'Telegram';
    const v = $('#view');
    v.innerHTML = '';
    v.appendChild(el('div', { class: 'view-head' },
        `<div><div class="eyebrow">Bridge</div><h1>Telegram Bridge</h1>
          <div class="sub">One shared bridge — talk to any project from your phone.</div></div>`));
    let status;
    try { status = await api('/api/telegram/status'); } catch (e) { status = { ok: false }; }
    const bot = status.bot || {};
    v.appendChild(el('div', { class: 'card mb' },
        `<div class="row wrap" style="gap:16px">
            <span class="badge ${status.configured ? 'green' : 'red'}">${status.configured ? 'configured' : 'not configured'}</span>
            <span class="secondary">${bot.username ? '@' + esc(bot.username) : 'no bot'}</span>
            <span class="secondary">chats: ${(status.allowed_chats || []).join(', ') || 'pair-on-first-message'}</span>
         </div>`));
    v.appendChild(el('div', { class: 'card' },
        `<label class="field"><span class="lab">Chat id (optional)</span><input type="text" id="tg-chat" placeholder="defaults to configured chat"></label>
         <label class="field"><span class="lab">Message</span><textarea id="tg-msg" placeholder="Send a Markdown message via the Perun bot…"></textarea></label>
         <div class="form-actions"><button class="btn primary" id="tg-send">✈ Send via bot</button></div>
         <p class="muted mt">The full two-way bridge (commands like <span class="mono">/projects</span>, <span class="mono">/project</span>, <span class="mono">/recall</span>) runs as <span class="mono">python -m perun bridge</span> or with <span class="mono">python -m perun serve --bridge</span>.</p>`));
    $('#tg-send').addEventListener('click', async () => {
        const text = $('#tg-msg').value.trim();
        if (!text) { toast('Enter a message', 'err'); return; }
        try { const r = await api('/api/telegram/send', { method: 'POST', body: { text, chat_id: $('#tg-chat').value } }); toast(r.sent ? 'Sent ✈' : 'Send failed', r.sent ? 'ok' : 'err'); }
        catch (e) { toast(e.message, 'err'); }
    });
}

/* ========================================================= SETTINGS ===== */
async function viewSettings() {
    $('#topbar-title').textContent = 'Settings';
    await loadSettings();
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
            <label class="field"><span class="lab">Default model</span><input type="text" id="s-model" value="${esc(s.default_model || '')}"></label>
            <label class="field"><span class="lab">Default agent</span><input type="text" id="s-agent" value="${esc(s.default_agent || '')}"></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">Max concurrent runs</span><input type="number" id="s-max" value="${esc(s.max_concurrent_runs)}"></label>
            <label class="field"><span class="lab">Run timeout (s)</span><input type="number" id="s-timeout" value="${esc(s.run_timeout)}"></label>
         </div>
         <div class="form-row">
            <label class="field"><span class="lab">Ollama host</span><input type="text" id="s-ollama" value="${esc(s.ollama_host)}"></label>
            <label class="field"><span class="lab">Embed model</span><input type="text" id="s-embed" value="${esc(s.embed_model)}"></label>
         </div>
         <label class="field"><span class="lab">Telegram token ${s.telegram_token_set ? '<span class="badge green">set</span>' : ''}</span>
            <input type="password" id="s-tgtoken" placeholder="${s.telegram_token_set ? '•••••• (leave blank to keep)' : 'bot token'}"></label>
         <label class="field"><span class="lab">Telegram allowed chats (comma separated)</span><input type="text" id="s-tgchats" value="${esc((s.telegram_allowed_chats || []).join(', '))}"></label>
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
            default_model: $('#s-model').value, default_agent: $('#s-agent').value,
            max_concurrent_runs: Number($('#s-max').value), run_timeout: Number($('#s-timeout').value),
            ollama_host: $('#s-ollama').value, embed_model: $('#s-embed').value,
            telegram_allowed_chats: $('#s-tgchats').value.split(',').map((x) => x.trim()).filter(Boolean),
            auto_discover_on_start: $('#s-autodisc').checked, index_docs_on_discover: $('#s-indexdisc').checked,
        };
        const tok = $('#s-tgtoken').value.trim();
        if (tok) payload.telegram_token = tok;
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
    $('#btn-quick-ask').addEventListener('click', () => goto('#/console'));
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

async function boot() {
    wireChrome();
    if (!location.hash) location.hash = '#/dashboard';
    await refreshHealth();
    await router();
    setInterval(refreshHealth, 15000);
    window.addEventListener('hashchange', router);
}

document.addEventListener('DOMContentLoaded', boot);
