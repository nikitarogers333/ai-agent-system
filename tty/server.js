'use strict';
const express = require('express');
const { WebSocketServer } = require('ws');
const pty = require('node-pty');
const path = require('path');
const http = require('http');
const { execSync, spawn } = require('child_process');
const fs = require('fs');

// Prevent crashes from unhandled errors — log and continue
process.on('uncaughtException', (err) => {
  console.error(`[!] uncaughtException: ${err.message}\n${err.stack}`);
});
process.on('unhandledRejection', (reason) => {
  console.error(`[!] unhandledRejection: ${reason}`);
});

// Track service shutdown — suppress 'exit' messages during restart
let shuttingDown = false;
process.on('SIGTERM', () => { shuttingDown = true; setTimeout(() => process.exit(0), 500); });
process.on('SIGINT',  () => { shuttingDown = true; setTimeout(() => process.exit(0), 500); });

const SESSION_DEFAULT = process.env.SESSION_DEFAULT || 'main';
const SESSION_PREFIX  = process.env.SESSION_PREFIX  || 's';

// Extract session name from request path/body/query
function getSessionFromReq(req) {
  const src = (req.body && req.body.path) || (req.query && req.query.path) || '/';
  const raw = src.replace(/^\//, '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32);
  return raw ? SESSION_PREFIX + raw : SESSION_DEFAULT;
}

const app = express();
const server = http.createServer(app);
const wss = new WebSocketServer({ server });

app.use(express.static(path.join(__dirname, 'public')));

// Multipart upload — no base64 overhead, handles large files and multiple files
app.post('/api/upload', (req, res) => {
  const boundary = (req.headers['content-type'] || '').split('boundary=')[1];
  if (!boundary) return res.status(400).json({ error: 'no boundary' });
  const dir = process.env.UPLOADS_DIR || require('os').homedir() + '/uploads';
  fs.mkdirSync(dir, { recursive: true });
  const chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => {
    try {
      const buf     = Buffer.concat(chunks);
      const delim   = Buffer.from('--' + boundary);
      const results = [];
      let pos = 0;
      while (pos < buf.length) {
        const start = buf.indexOf(delim, pos);
        if (start === -1) break;
        pos = start + delim.length;
        if (buf[pos] === 45 && buf[pos+1] === 45) break; // trailing --
        if (buf[pos] === 13) pos += 2;                   // skip \r\n
        const hdrEnd = buf.indexOf('\r\n\r\n', pos);
        if (hdrEnd === -1) break;
        const hdr = buf.slice(pos, hdrEnd).toString();
        pos = hdrEnd + 4;
        const nextBound = buf.indexOf('\r\n' + delim, pos);
        const fileEnd   = nextBound === -1 ? buf.length : nextBound;
        const fileData  = buf.slice(pos, fileEnd);
        pos = fileEnd;
        const nameMatch = hdr.match(/filename="([^"]+)"/);
        if (!nameMatch) continue;
        const safeName = nameMatch[1].replace(/[^a-zA-Z0-9._\-() ]/g, '_').slice(0, 255);
        const dest = path.join(dir, safeName);
        fs.writeFileSync(dest, fileData);
        results.push({ ok: true, path: dest, size: fileData.length, name: safeName });
      }
      if (!results.length) return res.status(400).json({ error: 'no files parsed' });
      res.json(results.length === 1 ? results[0] : { ok: true, files: results });
    } catch(e) { res.status(500).json({ error: e.message }); }
  });
});

app.use(express.json());

// ─── Knowledge files system ────────────────────────────────────────────────
// ---- MD files browser ----
app.get('/api/md-files', (req, res) => {
  const session = req.query.session || '';
  const map = readSessionMap();
  const project = map[session];
  const results = [];
  // Project MD files first (if session maps to a project), skip symlinks
  if (project) {
    const projDir = process.env.PROJECTS_DIR ? process.env.PROJECTS_DIR + '/' + project : (process.env.HOME + '/projects/' + project);
    try {
      fs.readdirSync(projDir).filter(f => {
        if (!f.endsWith('.md') || f.startsWith('.')) return false;
        try { return !fs.lstatSync(projDir + '/' + f).isSymbolicLink(); } catch(_) { return true; }
      }).sort().forEach(f => {
        results.push({ id: projDir + '/' + f, name: f, dir: project });
      });
    } catch(_) {}
  }
  // Root-level MD files
  const ROOT = '/root';
  try {
    fs.readdirSync(ROOT).filter(f => f.endsWith('.md') && !f.startsWith('.')).sort().forEach(f => {
      results.push({ id: ROOT + '/' + f, name: f, dir: '~' });
    });
  } catch(_) {}
  res.json(results);
});

app.get('/api/md-file', (req, res) => {
  const id = req.query.id;
  if (!id || !id.endsWith('.md')) return res.status(400).send('bad id');
  // Only allow reading from /root/ and /root/projects/
  if (!id.startsWith(process.env.HOME + '/')) return res.status(403).send('forbidden');
  try { res.send(fs.readFileSync(id, 'utf8')); }
  catch(e) { res.status(404).send(e.message); }
});

app.post('/api/md-file', express.json(), (req, res) => {
  const { id, content } = req.body;
  if (!id || !id.endsWith('.md')) return res.status(400).json({ error: 'bad id' });
  if (!id.startsWith(process.env.HOME + '/')) return res.status(403).json({ error: 'forbidden' });
  try { fs.writeFileSync(id, content); res.json({ ok: true }); }
  catch(e) { res.status(500).json({ error: e.message }); }
});

const PROJECTS_DIR = process.env.PROJECTS_DIR || require('os').homedir() + '/projects';
const SESSION_MAP  = PROJECTS_DIR + '/.session-map.json';

function readSessionMap() {
  try { return JSON.parse(fs.readFileSync(SESSION_MAP, 'utf8')); } catch(_) { return {}; }
}
function writeSessionMap(map) {
  fs.mkdirSync(PROJECTS_DIR, { recursive: true });
  fs.writeFileSync(SESSION_MAP, JSON.stringify(map, null, 2));
}
function existingProjects() {
  try {
    return fs.readdirSync(PROJECTS_DIR, { withFileTypes: true })
      .filter(e => e.isDirectory() && !e.name.startsWith('.'))
      .map(e => e.name);
  } catch(_) { return []; }
}
function linkSessionToProject(sessionName, projectName) {
  // Create project dir + CLAUDE.md if needed
  const projDir = PROJECTS_DIR + '/' + projectName;
  fs.mkdirSync(projDir, { recursive: true });
  const claudeMd = projDir + '/CLAUDE.md';
  if (!fs.existsSync(claudeMd)) fs.writeFileSync(claudeMd, '# Project: ' + projectName + '\n\n## Context\n\n## Notes\n');
  // Update session map — remove old entry for this session if any, add new
  const map = readSessionMap();
  for (const k of Object.keys(map)) { if (k === sessionName) delete map[k]; }
  map[sessionName] = projectName;
  writeSessionMap(map);
}

app.post('/api/rename', express.json(), (req, res) => {
  const fromRaw = ((req.body && req.body.from) || '/').replace(/^\//, '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32);
  const toRaw   = ((req.body && req.body.to)   || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32);
  if (!toRaw) return res.status(400).json({ error: 'invalid name' });
  const fromSession = fromRaw ? SESSION_PREFIX + fromRaw : SESSION_DEFAULT;
  const toSession   = SESSION_PREFIX + toRaw;
  try {
    execSync(`tmux rename-session -t ${fromSession} ${toSession} 2>/dev/null`);
    // Match to existing project by first 3 chars, else create new project with the given name
    const prefix = toRaw.slice(0, 3).toLowerCase();
    const match  = existingProjects().find(p => p.slice(0, 3).toLowerCase() === prefix);
    const project = match || toRaw;
    // Remove old session entry before linking new one
    const map = readSessionMap();
    delete map[fromSession];
    writeSessionMap(map);
    linkSessionToProject(toSession, project);
    res.json({ ok: true, session: toSession, path: '/' + toRaw, project });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Session-to-project mapping
app.get('/api/session-map', (req, res) => { res.json(readSessionMap()); });

// Session activity timestamps (for sorting by last used)
app.get('/api/session-activity-times', (req, res) => {
  try {
    const out = execSync("tmux list-sessions -F '#{session_name}:#{session_activity}' 2>/dev/null", { encoding: 'utf8', timeout: 2000 }).trim();
    const times = {};
    out.split('\n').filter(Boolean).forEach(line => {
      const [name, ts] = line.split(':');
      if (name && ts) times[name] = parseInt(ts);
    });
    res.json(times);
  } catch(_) { res.json({}); }
});

// List all sessions (mapped + live tmux)
app.get('/api/sessions', (req, res) => {
  try {
    // Get live tmux sessions
    let live = [];
    try {
      const out = execSync("tmux list-sessions -F '#{session_name}' 2>/dev/null", { encoding: 'utf8' }).trim();
      live = out ? out.split('\n').filter(Boolean) : [];
    } catch(_) {}
    // Get all mapped sessions from session-map
    const map = readSessionMap();
    const all = new Set([...live, ...Object.keys(map)]);
    res.json(Array.from(all));
  } catch(_) { res.json([]); }
});

// Create a new tmux session and link it to a project
app.post('/api/sessions/create', express.json(), (req, res) => {
  const nameRaw    = ((req.body && req.body.name)    || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32);
  const projectRaw = ((req.body && req.body.project) || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32);
  if (!nameRaw)    return res.status(400).json({ error: 'session name required' });
  if (!projectRaw) return res.status(400).json({ error: 'project name required' });
  const sessionName = SESSION_PREFIX + nameRaw;
  try {
    // Fail if session already exists
    const exists = execSync(`tmux has-session -t ${sessionName} 2>/dev/null; echo $?`).toString().trim();
    if (exists === '0') return res.status(409).json({ error: 'session already exists' });
    const projDir = PROJECTS_DIR + '/' + projectRaw;
    const startDir = fs.existsSync(projDir) ? projDir : PROJECTS_DIR;
    execSync(`tmux new-session -d -s ${sessionName} -c ${startDir}`);
    linkSessionToProject(sessionName, projectRaw);
    res.json({ ok: true, session: sessionName, path: '/' + nameRaw, project: projectRaw });
  } catch(e) { res.status(500).json({ error: e.message }); }
});

// Trigger context-watcher for a session (fire-and-forget, no await)
function triggerContextUpdate(sessionName) {
  // Override: add your own context watcher here
}

app.post('/api/delete', express.json(), (req, res) => {
  const sessionName = getSessionFromReq(req);
  // Capture content before killing, then trigger context-watcher
  try {
    const content = execSync(`tmux capture-pane -p -S -5000 -t ${sessionName} 2>/dev/null || true`).toString();
    if (content.trim()) {
      const tmp = `/tmp/context-watcher-delete-${sessionName}.txt`;
      fs.writeFileSync(tmp, content);
    }
  } catch(_) {}
  // Remove from session map
  try {
    const map = readSessionMap();
    delete map[sessionName];
    writeSessionMap(map);
  } catch(_) {}
  // Kill the session
  try { execSync(`tmux kill-session -t ${sessionName} 2>/dev/null`); } catch(_) {}
  res.json({ ok: true });
});

// Extract last Claude response as clean readable text (for TTS)
app.get('/api/last-response', (req, res) => {
  const sessionName = getSessionFromReq(req);
  try {
    const pane = execSync(`tmux capture-pane -p -S -200 -t ${sessionName} 2>/dev/null`, { encoding: 'utf8', timeout: 3000 });
    const lines = pane.split('\n');
    // Filter to only readable prose lines
    const readable = lines.filter(l => {
      const t = l.trim();
      if (!t || t.length < 3) return false;
      // Must start with a letter to be prose
      if (!/^[A-Za-z"']/.test(t)) return false;
      // Skip tool calls
      if (/^(Bash|Read|Edit|Write|Grep|Glob|Agent|Skill|Updated|Created|Found|Searched|Command)\s*[\(:]/.test(t)) return false;
      // Skip code keywords
      if (/^(const|let|var|function|if|for|while|return|import|export|async|await|class|try|catch)\s/.test(t)) return false;
      // Skip status words
      if (/^(active|inactive|enabled|disabled|loaded|running|dead|done|Length|EMPTY)\s*$/i.test(t)) return false;
      // Skip lines ending with code chars
      if (/[{}();=]$/.test(t)) return false;
      // Skip indented lines (code)
      if (/^\s{4,}/.test(l)) return false;
      return true;
    });
    // Get the last chunk of readable lines (the most recent response)
    const last100 = readable.slice(-60).join('\n').trim();
    res.json({ text: last100 });
  } catch(e) {
    res.json({ text: '' });
  }
});

app.post('/api/slack-last', express.json(), (req, res) => {
  const sessionName = getSessionFromReq(req);
  try {
    const pane = execSync(`tmux capture-pane -p -S -200 -t ${sessionName} 2>/dev/null`, { encoding: 'utf8', timeout: 3000 });
    const lines = pane.split('\n').filter(l => l.trim());
    const lastMsg = lines.slice(-50).join('\n').trim();
    if (!lastMsg) { res.json({ ok: false, error: 'no content' }); return; }
    const msg = `[${sessionName}]\n${lastMsg}`;
    // Write to temp file to avoid shell escaping issues
    const tmp = `/tmp/slack-msg-${Date.now()}.txt`;
    fs.writeFileSync(tmp, msg);
    const child = 
    child.on('close', () => { try { fs.unlinkSync(tmp); } catch(_) {} });
    res.json({ ok: true });
  } catch(e) {
    res.json({ ok: false, error: e.message });
  }
});

app.post('/api/context-update', express.json(), (req, res) => {
  const sessionName = getSessionFromReq(req);
  triggerContextUpdate(sessionName);
  res.json({ ok: true, session: sessionName });
});

app.get('/api/context-mtime', (req, res) => {
  const sessionName = getSessionFromReq(req);
  const map = readSessionMap();
  const projectName = map[sessionName];
  if (!projectName) { res.json({ mtime: 0 }); return; }
  const mdPath = PROJECTS_DIR + '/' + projectName + '/PROJECT.md';
  try {
    const mtime = fs.statSync(mdPath).mtimeMs;
    res.json({ mtime });
  } catch(_) { res.json({ mtime: 0 }); }
});

app.post('/api/reset', express.json(), (req, res) => {
  const sessionName = getSessionFromReq(req);
  // Capture pane content BEFORE killing the session, write to temp file for watcher
  try {
    const content = execSync(`tmux capture-pane -p -S -5000 -t ${sessionName} 2>/dev/null || true`).toString();
    if (content.trim()) {
      const tmp = `/tmp/context-watcher-reset-${sessionName}.txt`;
      fs.writeFileSync(tmp, content);
    }
  } catch(_) {}
  // Remove from session map so URL is freed up
  try { const map = readSessionMap(); delete map[sessionName]; writeSessionMap(map); } catch(_) {}
  try { execSync(`tmux kill-session -t ${sessionName} 2>/dev/null`); } catch(_) {}
  res.json({ ok: true, session: sessionName });
});

// Serve index.html for any path — session is extracted from URL path
app.use((req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

wss.on('connection', (ws, req) => {
  const ip = req.socket.remoteAddress;
  const url = new URL(req.url, 'http://localhost');
  const raw = url.pathname.replace(/^\//, '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 32);
  const sessionName = raw ? SESSION_PREFIX + raw : SESSION_DEFAULT;
  console.log(`[+] connect from ${ip} session=${sessionName}`);

  // Auto-link new session to project by 3-char prefix match, then set startCwd
  let startCwd = process.env.HOME || '/root';
  try {
    const map = readSessionMap();
    if (!map[sessionName] && raw.length >= 3) {
      const prefix = raw.slice(0, 3).toLowerCase();
      const match = existingProjects().find(p => p.slice(0, 3).toLowerCase() === prefix);
      if (match) linkSessionToProject(sessionName, match);
    }
    const proj = readSessionMap()[sessionName];
    if (proj) {
      const projDir = PROJECTS_DIR + '/' + proj;
      if (fs.existsSync(projDir)) startCwd = projDir;
    }
  } catch(_) {}

  // Defer PTY spawn until client sends actual dimensions — prevents tmux reflow jumble
  let shell = null;
  let cols = 80, rows = 24;
  let dataBuf = '', dataTimer = null;
  // Hoisted from spawnShell so ws.on('close') can access them
  let periodicSync = null;
  let syncTimer = null;

  function sendData(data) {
    dataBuf += data;
    if (!dataTimer) {
      dataTimer = setTimeout(() => {
        if (ws.readyState === 1) ws.send(JSON.stringify({ t: 'o', d: dataBuf }));
        dataBuf = '';
        dataTimer = null;
      }, 8);
    }
  }

  function spawnShell(c, r) {
    cols = c; rows = r;
    let ready = false;
    try {
      shell = pty.spawn('/usr/bin/tmux', ['new-session', '-A', '-s', sessionName], {
        name: 'xterm-256color',
        cols, rows,
        cwd: startCwd,
        env: { ...process.env, TERM: 'dumb' },
      });
    } catch(e) {
      console.error(`[!] pty.spawn failed: ${e.message}`);
      ws.close();
      return;
    }

    // Two sync modes:
    //   1. Quiet sync: 500ms after output stops (cleans up final state)
    //   2. Periodic sync: every 3s during active output (fixes jumbled text mid-generation)
    // Previous 1s periodic caused dots. 3s is infrequent enough to avoid visual glitches
    // but frequent enough to correct garbled rendering during long responses.
    // syncTimer and periodicSync hoisted to outer scope
    let outputActive = false;
    let lastSyncTime = 0;

    function doSync() {
      const now = Date.now();
      // Debounce: don't sync more than once per 2s
      if (now - lastSyncTime < 2000) return;
      lastSyncTime = now;
      try {
        const snap = execSync(`tmux capture-pane -p -S -5000 -t ${sessionName} 2>/dev/null`, { encoding: 'utf8', timeout: 2000 });
        if (snap.trim() && ws.readyState === 1)
          ws.send(JSON.stringify({ t: 'sync', d: snap }));
      } catch (_) {}
    }

    shell.onData((data) => {
      if (!ready) return;
      sendData(data);
      outputActive = true;
      // Start periodic sync during output (3s interval)
      if (!periodicSync) {
        periodicSync = setInterval(() => {
          if (outputActive) doSync();
          else { clearInterval(periodicSync); periodicSync = null; }
        }, 3000);
      }
      // Quiet sync: fires 500ms after output stops
      clearTimeout(syncTimer);
      syncTimer = setTimeout(() => {
        outputActive = false;
        doSync();
      }, 500);
    });

    shell.onExit(({ exitCode, signal }) => {
      console.log(`[-] shell exited for ${ip} session=${sessionName} code=${exitCode} signal=${signal}`);
      if (!shuttingDown && ws.readyState === 1) ws.send(JSON.stringify({ t: 'exit' }));
      ws.close();
    });

    // Send history after tmux settles at correct size, then start forwarding live output
    setTimeout(() => {
      try {
        const history = execSync(`tmux capture-pane -p -S -5000 -t ${sessionName} 2>/dev/null`, { encoding: 'utf8', timeout: 2000 });
        if (history.trim() && ws.readyState === 1)
          ws.send(JSON.stringify({ t: 'h', d: history }));
      } catch (_) {}
      ready = true;
    }, 150);
  }

  // Safety net: if client never sends resize within 5s, spawn with defaults
  const safetyTimer = setTimeout(() => {
    if (!shell) spawnShell(80, 24);
  }, 5000);

  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw);
      if (msg.t === 'r') {
        if (!shell) {
          // First resize — spawn PTY at exact client dimensions
          clearTimeout(safetyTimer);
          spawnShell(msg.cols, msg.rows);
        } else {
          cols = msg.cols; rows = msg.rows;
          shell.resize(cols, rows);
        }
      }
      if (msg.t === 'i' && shell) shell.write(msg.d);
    } catch (_) {}
  });

  ws.on('close', () => {
    clearTimeout(safetyTimer);
    clearTimeout(syncTimer);
    if (periodicSync) { clearInterval(periodicSync); periodicSync = null; }
    console.log(`[-] disconnect ${ip}`);
    try { if (shell) shell.kill(); } catch (_) {}
  });
  ws.on('error', (e) => {
    clearTimeout(safetyTimer);
    clearTimeout(syncTimer);
    if (periodicSync) { clearInterval(periodicSync); periodicSync = null; }
    console.log(`[!] ws error ${ip}: ${e.message}`);
    try { if (shell) shell.kill(); } catch (_) {}
  });
});

const PORT = process.env.PORT || 4021;
server.listen(PORT, () => console.log(`tty running on :${PORT}`));
