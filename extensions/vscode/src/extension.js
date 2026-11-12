'use strict';

const vscode = require('vscode');
const WebSocket = require('ws');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  normalizeEndpoint,
  toWsUrl,
  relativePath,
  selectionRef,
  backoffDelay,
  shellArg,
} = require('./protocol');

const EXTENSION_VERSION = '0.1.4';
const PROTOCOL_VERSION = 1;
const TERMINAL_STATE_KEY = 'perun.terminals';

/** @type {vscode.StatusBarItem | undefined} */
let statusItem;
let tuiItem;
let output;
let extensionContext;
/** @type {WebSocket | undefined} */
let socket;
let disposed = false;
let reconnectTimer;
let reconnectAttempt = 0;
const windowId = crypto.randomUUID();
const pendingSelections = new Map();
/** run_uid -> { terminal, terminalId } */
const terminals = new Map();
/** terminalId -> run_uid */
const terminalIds = new Map();
let projectSlug = '';
let activeRun = null;
let startingTui = false;

function userSetting(key, fallback) {
  // Never trust a workspace-level `perun.*` setting: a cloned repository could
  // redirect the endpoint or turn on automatic behaviour.
  const info = vscode.workspace.getConfiguration('perun').inspect(key);
  if (info && info.globalValue !== undefined) return info.globalValue;
  return fallback;
}

function endpoint() {
  return normalizeEndpoint(userSetting('perun.endpoint', 'http://127.0.0.1:8760'));
}

function perunHome() {
  return process.env.PERUN_HOME || path.join(os.homedir(), '.perun');
}

function apiToken() {
  const fromEnv = String(process.env.PERUN_API_TOKEN || '').trim();
  if (fromEnv) return fromEnv;
  try {
    return fs.readFileSync(path.join(perunHome(), 'api-token'), 'utf8').trim();
  } catch (_) {
    return '';
  }
}

function workspaceFolder() {
  const folders = vscode.workspace.workspaceFolders || [];
  return folders.length ? folders[0] : undefined;
}

function setStatus(text, tooltip) {
  if (!statusItem) return;
  statusItem.text = `$(plug) ${text}`;
  statusItem.tooltip = tooltip || 'Perun Overseer';
  statusItem.show();
  log(`${text}${tooltip ? ' — ' + tooltip : ''}`);
}

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}`;
  if (output) output.appendLine(line);
  try {
    const dir = path.join(perunHome(), 'vscode');
    const file = path.join(dir, 'extension.log');
    fs.mkdirSync(dir, { recursive: true });
    let big = false;
    try { big = fs.statSync(file).size > 1_000_000; } catch (_) { big = false; }
    if (big) fs.writeFileSync(file, line + '\n', 'utf8');
    else fs.appendFileSync(file, line + '\n', 'utf8');
  } catch (_) { /* diagnostics must never break the extension */ }
}

function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  try {
    socket.send(JSON.stringify(message));
    return true;
  } catch (_) {
    return false;
  }
}

async function revealPerunTab() {
  try {
    const groups = vscode.window.tabGroups.all || [];
    for (let groupIndex = 0; groupIndex < groups.length; groupIndex += 1) {
      const tabs = groups[groupIndex].tabs || [];
      const tabIndex = tabs.findIndex((tab) => {
        const input = tab.input;
        const viewType = input && String(input.viewType || '').toLowerCase();
        if (viewType && viewType.includes('simplebrowser')) return true;
        return String(tab.label || '').toLowerCase().includes('perun')
          && (!viewType || viewType.includes('webview'));
      });
      if (tabIndex < 0) continue;
      await vscode.commands.executeCommand('workbench.action.focusFirstEditorGroup');
      for (let i = 0; i < groupIndex; i += 1) {
        await vscode.commands.executeCommand('workbench.action.focusNextGroup');
      }
      await vscode.commands.executeCommand(`workbench.action.openEditorAtIndex${tabIndex + 1}`);
      return true;
    }
  } catch (_) { /* best effort; open a fresh tab below */ }
  return false;
}

function openDashboard() {
  (async () => {
    try {
      if (await revealPerunTab()) {
        log('dashboard: focused existing tab');
        return;
      }
      log('dashboard: requesting embed ticket');
      const data = await apiPost('/api/vscode/embed-ticket', {
        project: projectSlug,
        window_id: windowId,
      });
      const url = `${endpoint()}/?embed=${encodeURIComponent(String(data.ticket || ''))}`;
      // Simple Browser navigates the dashboard as a normal editor tab (no
      // wrapper/iframe page and no extra click). A webview reload can reuse the
      // embed ticket for its short TTL.
      await vscode.commands.executeCommand('simpleBrowser.show', url);
      log('dashboard: opened in Simple Browser');
    } catch (_) {
      log('dashboard: Simple Browser failed; opening the browser');
      const folder = workspaceFolder();
      const suffix = projectSlug ? `#/project/${encodeURIComponent(projectSlug)}` : '#/projects';
      vscode.env.openExternal(vscode.Uri.parse(`${endpoint()}/${suffix}`));
    }
  })();
}

function scheduleReconnect() {
  if (disposed) return;
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, backoffDelay(reconnectAttempt));
  reconnectAttempt = Math.min(reconnectAttempt + 1, 10);
}

async function requestTicket(folder) {
  const token = apiToken();
  log(`ticket request for ${folder.uri.fsPath}; token=${token ? 'present' : 'missing'}; endpoint=${endpoint()}`);
  if (!token) {
    setStatus('Perun: no token', 'Open the Perun dashboard once to create the local API token.');
    return null;
  }
  const response = await fetch(`${endpoint()}/api/vscode/ticket`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Perun-Token': token,
    },
    body: JSON.stringify({ workspace_path: folder.uri.fsPath }),
  });
  log(`ticket response ${response.status}`);
  if (response.status === 404) {
    setStatus('Perun: not registered', 'This folder is not a registered Perun project. Link it from the dashboard.');
    return null;
  }
  if (response.status === 401) {
    setStatus('Perun: bad token', `Check ${path.join(perunHome(), 'api-token')}`);
    return null;
  }
  if (!response.ok) throw new Error(`ticket request failed (${response.status})`);
  return response.json();
}

async function connect() {
  if (disposed) return;
  const folder = workspaceFolder();
  if (!folder) {
    setStatus('Perun: no folder');
    return;
  }
  if (vscode.env.remoteName) {
    setStatus('Perun: remote unsupported', 'The Perun companion runs locally; remote/WSL windows are not supported yet.');
    return;
  }
  if (!vscode.workspace.isTrusted) {
    setStatus('Perun: untrusted workspace', 'Trust this workspace to connect to Perun.');
    return;
  }
  setStatus('Perun: connecting…');
  try {
    const ticket = await requestTicket(folder);
    if (!ticket || !ticket.ticket) {
      scheduleReconnect();
      return;
    }
    if (socket) {
      try { socket.close(); } catch (_) { /* ignore */ }
    }
    const ws = new WebSocket(toWsUrl(endpoint(), ticket.ticket));
    socket = ws;
    ws.on('open', () => {
      reconnectAttempt = 0;
      log('websocket open');
      send({
        type: 'hello',
        workspace_path: folder.uri.fsPath,
        window_id: windowId,
        protocol: PROTOCOL_VERSION,
        extension_version: EXTENSION_VERSION,
      });
    });
    ws.on('message', (raw) => {
      let message;
      try { message = JSON.parse(String(raw)); } catch (_) { return; }
      handleMessage(message, folder);
    });
    ws.on('close', () => {
      if (socket !== ws) return;  // superseded by a newer connection
      setStatus('Perun: offline', 'The local Perun service is not reachable; retrying.');
      scheduleReconnect();
    });
    ws.on('error', () => {
      // The close event schedules the retry; avoid duplicate timers.
    });
  } catch (err) {
    setStatus('Perun: offline', String(err.message || err));
    scheduleReconnect();
  }
}

function handleMessage(message, folder) {
  switch (message.type) {
    case 'hello.ok':
      projectSlug = String((message.project || {}).slug || '');
      activeRun = message.active_run || null;
      setStatus(`Perun: ${message.linked ? 'linked' : 'connected'}`,
        `${message.project ? message.project.name : 'Perun'} — ${message.linked ? 'VS Code integration linked' : 'open the dashboard to link this project'}`);
      // Re-register open terminals after a Perun restart: the server's
      // in-memory mapping is gone, but the VS Code terminals are still alive.
      for (const [runUid, entry] of terminals.entries()) {
        send({
          type: 'terminal.opened',
          run_uid: runUid,
          terminal_id: entry.terminalId,
          pid: Number(entry.pid) || 0,
        });
      }
      if (Array.isArray(message.recent_edits) && message.recent_edits.length) {
        revealEdit(message.recent_edits[message.recent_edits.length - 1], folder);
      }
      break;
    case 'edit.reveal':
      revealEdit(message, folder);
      break;
    case 'terminal.open':
      openTerminal(message);
      break;
    case 'terminal.focus': {
      const entry = findTerminal(message);
      if (entry) entry.terminal.show();
      break;
    }
    case 'terminal.close': {
      const entry = findTerminal(message);
      if (entry) entry.terminal.dispose();
      break;
    }
    case 'selection.ok':
      finishSelection(message);
      break;
    case 'error':
      setStatus('Perun: error', String(message.message || 'request failed'));
      break;
    default:
      break;
  }
}

function findTerminal(message) {
  const runUid = String(message.run_uid || '');
  if (runUid && terminals.has(runUid)) return terminals.get(runUid);
  const terminalId = String(message.terminal_id || '');
  if (terminalId && terminalIds.has(terminalId)) {
    return terminals.get(terminalIds.get(terminalId));
  }
  for (const entry of terminals.values()) {
    if (entry.terminalId === terminalId) return entry;
  }
  return undefined;
}

function openTerminal(message) {
  const folder = workspaceFolder();
  const cwd = String(message.cwd || (folder ? folder.uri.fsPath : ''));
  const runUid = String(message.run_uid || '');
  activeRun = { run_uid: runUid };
  const terminal = vscode.window.createTerminal({
    name: terminalName(message.project_slug, runUid),
    cwd,
    location: { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
    env: {
      PERUN_RUN_UID: runUid,
      PERUN_VSCODE_TERMINAL: '1',
    },
  });
  const terminalId = crypto.randomUUID();
  terminals.set(runUid, {
    terminal,
    terminalId,
    project: String(message.project_slug || ''),
    pid: 0,
  });
  terminalIds.set(terminalId, runUid);
  saveTerminalState();
  terminal.show();
  log(`terminal: opened ${terminal.name} (${cwd})`);
  const command = message.session_id
    ? `opencode --session ${shellArg(message.session_id)}`
    : 'opencode';
  // Give VS Code a moment to size the pty; a TUI that reads 1 column at start
  // renders vertically and does not always reflow.
  setTimeout(() => {
    try { terminal.sendText(command, true); } catch (_) { /* terminal closed */ }
  }, 150);
  const registration = {
    type: 'terminal.opened',
    run_uid: runUid,
    terminal_id: terminalId,
    pid: 0,
  };
  if (terminal.processId && typeof terminal.processId.then === 'function') {
    terminal.processId.then((pid) => {
      const entry = terminals.get(runUid);
      if (entry) {
        entry.pid = Number(pid) || 0;
        saveTerminalState();
      }
      if (pid) send({ ...registration, pid: Number(pid) || 0 });
    }, () => { /* processId is optional */ });
  } else {
    send(registration);
  }
}

function terminalName(slug, runUid) {
  const name = String(slug || 'opencode');
  const suffix = String(runUid || '').slice(0, 6);
  return suffix ? `Perun: ${name} · ${suffix}` : `Perun: ${name}`;
}

function saveTerminalState() {
  if (!extensionContext) return;
  const rows = Array.from(terminals.entries()).map(([runUid, entry]) => ({
    run_uid: runUid,
    terminal_id: entry.terminalId,
    project: entry.project || '',
    pid: entry.pid || 0,
    name: entry.terminal && entry.terminal.name,
  }));
  extensionContext.workspaceState.update(TERMINAL_STATE_KEY, rows);
}

function rebindTerminal(terminal) {
  if (!extensionContext || !terminal) return;
  for (const [runUid, entry] of terminals) {
    if (entry.terminal === terminal) return;  // already tracked
  }
  const rows = extensionContext.workspaceState.get(TERMINAL_STATE_KEY, []) || [];
  const match = rows.find((row) => row.name === terminal.name && !terminals.has(row.run_uid));
  if (!match) return;
  const runUid = String(match.run_uid || '');
  const terminalId = String(match.terminal_id || crypto.randomUUID());
  terminals.set(runUid, {
    terminal,
    terminalId,
    project: String(match.project || ''),
    pid: Number(match.pid) || 0,
  });
  terminalIds.set(terminalId, runUid);
  activeRun = { run_uid: runUid };
  send({
    type: 'terminal.opened',
    run_uid: runUid,
    terminal_id: terminalId,
    pid: Number(match.pid) || 0,
  });
  if (terminal.processId && typeof terminal.processId.then === 'function') {
    terminal.processId.then((pid) => {
      const entry = terminals.get(runUid);
      if (entry) {
        entry.pid = Number(pid) || 0;
        saveTerminalState();
      }
    }, () => { /* processId is optional */ });
  }
}

function rebindExistingTerminals() {
  for (const terminal of vscode.window.terminals) {
    if (String(terminal.name || '').startsWith('Perun:')) rebindTerminal(terminal);
  }
}

function revealEdit(message, folder) {
  if (!userSetting('perun.autoFollowEdits', true)) return;
  if (userSetting('perun.revealMode', 'open') === 'off') return;
  const rel = String(message.path || '');
  if (!rel) return;
  const target = path.join(folder.uri.fsPath, rel);
  if (!fs.existsSync(target)) return;
  const start = Math.max(1, Number(message.start_line) || 1);
  const end = Math.max(start, Number(message.end_line) || start);
  const range = new vscode.Range(start - 1, 0, end - 1, 0);
  const uri = vscode.Uri.file(target);
  vscode.window.showTextDocument(uri, { preview: true, preserveFocus: true }).then((editor) => {
    editor.revealRange(range, vscode.TextEditorRevealType.InCenterIfOutsideViewport);
    if (userSetting('perun.revealMode', 'open') === 'reveal') return;
    const decoration = vscode.window.createTextEditorDecorationType({
      isWholeLine: true,
      backgroundColor: new vscode.ThemeColor(
        message.kind === 'deleted'
          ? 'diffEditor.removedTextBackground'
          : 'diffEditor.insertedTextBackground'),
      overviewRulerColor: new vscode.ThemeColor('editorOverviewRuler.infoForeground'),
      overviewRulerLane: vscode.OverviewRulerLane.Right,
    });
    editor.setDecorations(decoration, [range]);
    setTimeout(() => {
      try {
        editor.setDecorations(decoration, []);
        decoration.dispose();
      } catch (_) { /* editor closed */ }
    }, 8000);
  }, () => { /* the editor may refuse a preview; not fatal */ });
}

function sendSelection() {
  const editor = vscode.window.activeTextEditor;
  const folder = workspaceFolder();
  if (!editor || !folder) {
    vscode.window.showInformationMessage('Perun: open a file in a workspace folder first.');
    return;
  }
  const selection = editor.selection;
  if (!selection || selection.isEmpty) {
    vscode.window.showInformationMessage('Perun: select some code first.');
    return;
  }
  const rel = relativePath(folder.uri.fsPath, editor.document.uri.fsPath);
  if (!rel) {
    vscode.window.showWarningMessage('Perun: the selected file is outside this workspace.');
    return;
  }
  const start = selection.start.line + 1;
  const end = selection.end.line + 1;
  const ref = selectionRef(rel, start, end);
  const requestId = crypto.randomUUID();
  const timer = setTimeout(() => {
    pendingSelections.delete(requestId);
    vscode.env.clipboard.writeText(ref);
    vscode.window.showInformationMessage(`Perun: copied ${ref} — paste it into OpenCode.`);
  }, 5000);
  pendingSelections.set(requestId, { ref, timer, rel, start, end, run_uid: activeRun ? activeRun.run_uid : '' });
  const sent = send({
    type: 'selection.shared',
    request_id: requestId,
    run_uid: activeRun ? String(activeRun.run_uid || '') : '',
    path: rel,
    start_line: start,
    end_line: end,
    language: editor.document.languageId || '',
    text: editor.document.getText(selection),
    dirty: editor.document.isDirty,
  });
  if (!sent) {
    clearTimeout(timer);
    pendingSelections.delete(requestId);
    vscode.env.clipboard.writeText(ref);
    vscode.window.showInformationMessage(`Perun: copied ${ref} — paste it into OpenCode.`);
  }
}

function finishSelection(message) {
  const requestId = String(message.request_id || '');
  let pending = requestId ? pendingSelections.get(requestId) : undefined;
  if (!pending && pendingSelections.size) {
    pending = Array.from(pendingSelections.values())[pendingSelections.size - 1];
  }
  if (!pending) return;
  pendingSelections.delete(requestId || '');
  clearTimeout(pending.timer);
  const ref = String(message.ref || pending.ref);
  if (message.action === 'insert') {
    const runUid = String(message.run_uid || pending.run_uid || '');
    const entry = (runUid && terminals.get(runUid))
      || Array.from(terminals.values()).reverse().find((item) => item.project === projectSlug)
      || Array.from(terminals.values())[terminals.size - 1];
    if (entry && entry.terminal) {
      entry.terminal.sendText(ref, false);
      entry.terminal.show();
      log(`selection: inserted ${ref}`);
      vscode.window.showInformationMessage(`Perun: attached ${ref} to the OpenCode terminal.`);
      return;
    }
  }
  const text = String(message.text || '');
  vscode.env.clipboard.writeText(text ? `${ref}\n\n${text}` : ref);
  log(`selection: copied ${ref}${text ? ' with snippet' : ''}`);
  vscode.window.showInformationMessage(
    `Perun: copied ${ref} — paste it into OpenCode${text ? ' (includes the selection)' : ''}.`);
}

async function apiPost(pathname, body) {
  const token = apiToken();
  if (!token) throw new Error(`Perun API token not found in ${perunHome()}`);
  const response = await fetch(`${endpoint()}${pathname}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Perun-Token': token,
    },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = {}; }
  if (!response.ok) {
    throw new Error(data.detail || `${pathname} failed (${response.status})`);
  }
  return data;
}

async function apiGet(pathname) {
  const token = apiToken();
  if (!token) throw new Error(`Perun API token not found in ${perunHome()}`);
  const response = await fetch(`${endpoint()}${pathname}`, {
    headers: { 'X-Perun-Token': token },
  });
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = {}; }
  if (!response.ok) throw new Error(data.detail || `${pathname} failed (${response.status})`);
  return data;
}

async function startTui() {
  if (!projectSlug) {
    vscode.window.showInformationMessage(
      'Perun: open a registered Perun project folder first (the status bar says "not registered" otherwise).');
    return;
  }
  if (startingTui) return;
  const existing = activeRun && terminals.get(String(activeRun.run_uid || ''));
  if (existing && existing.terminal) {
    existing.terminal.show();
    return;
  }
  startingTui = true;
  try {
    // Reopen the latest session rather than silently starting a blank one.
    let sessionId = '';
    let sessionPk = null;
    try {
      const data = await apiGet(`/api/projects/${encodeURIComponent(projectSlug)}/sessions`);
      const sessions = (data.sessions || []).filter((s) => s.opencode_session_id);
      if (sessions.length) {
        sessionId = String(sessions[0].opencode_session_id || '');
        sessionPk = sessions[0].id || null;
      }
    } catch (_) { /* no sessions yet */ }
    const created = await apiPost('/api/runs/tui', {
      project: projectSlug,
      prompt: 'Opened from VS Code',
      opencode_session_id: sessionId,
      session_pk: sessionPk,
    });
    const runUid = created && created.run && created.run.run_uid;
    if (!runUid) throw new Error('Perun did not return a run id');
    await apiPost(`/api/runs/${encodeURIComponent(runUid)}/vscode/open`, {});
    vscode.window.showInformationMessage('Perun: opening the OpenCode TUI in this window…');
  } catch (err) {
    vscode.window.showErrorMessage(`Perun: could not open the TUI — ${err.message || err}`);
  } finally {
    startingTui = false;
  }
}

function activate(context) {
  extensionContext = context;
  output = vscode.window.createOutputChannel('Perun');
  context.subscriptions.push(output);
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 40);
  statusItem.command = 'perun.openDashboard';
  setStatus('Perun: starting…');
  context.subscriptions.push(statusItem);
  tuiItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 39);
  tuiItem.text = '$(terminal) Perun TUI';
  tuiItem.tooltip = 'Open the tracked OpenCode TUI in this window';
  tuiItem.command = 'perun.openTui';
  tuiItem.show();
  context.subscriptions.push(tuiItem);
  context.subscriptions.push(
    vscode.commands.registerCommand('perun.openDashboard', openDashboard),
    vscode.commands.registerCommand('perun.sendSelection', sendSelection),
    vscode.commands.registerCommand('perun.openTui', startTui),
    vscode.commands.registerCommand('perun.reconnect', () => {
      reconnectAttempt = 0;
      connect();
    }),
  );
  context.subscriptions.push(vscode.window.onDidCloseTerminal((terminal) => {
    for (const [runUid, entry] of Array.from(terminals.entries())) {
      if (entry.terminal === terminal) {
        terminals.delete(runUid);
        terminalIds.delete(entry.terminalId);
        if (activeRun && String(activeRun.run_uid || '') === runUid) activeRun = null;
        saveTerminalState();
        send({ type: 'terminal.closed', run_uid: runUid, terminal_id: entry.terminalId });
      }
    }
  }));
  context.subscriptions.push(vscode.window.onDidOpenTerminal((terminal) => {
    if (String(terminal.name || '').startsWith('Perun:')) rebindTerminal(terminal);
  }));
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => connect()));
  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((event) => {
    if (event.affectsConfiguration('perun.endpoint')) connect();
  }));
  context.subscriptions.push({ dispose: () => { disposed = true; clearTimeout(reconnectTimer); if (socket) socket.close(); } });
  rebindExistingTerminals();
  connect();
}

function deactivate() {
  disposed = true;
  clearTimeout(reconnectTimer);
  if (socket) {
    try { socket.close(); } catch (_) { /* ignore */ }
  }
}

module.exports = { activate, deactivate };
