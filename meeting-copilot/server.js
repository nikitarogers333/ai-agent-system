#!/usr/bin/env node
/**
 * Meeting Copilot -- Browser-based transcription + AI suggestions
 * Transcription handled by browser Web Speech API (no API keys needed).
 * Server receives transcript text, runs Claude for suggestions, stores sessions.
 */

const express = require('express');
const WebSocket = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const os = require('os');
const multer = require('multer');

const PORT = parseInt(process.env.PORT || process.env.COPILOT_PORT || '4051');
const SESSIONS_DIR = path.join(__dirname, 'sessions');
const UPLOADS_DIR = path.join(__dirname, 'uploads');
fs.mkdirSync(UPLOADS_DIR, { recursive: true });

const ALLOWED_MIMES = /^(image\/(png|jpe?g|gif|webp)|application\/(pdf|json|msword|vnd\.openxmlformats|vnd\.ms-excel)|text\/(plain|markdown|csv|html))/;
const upload = multer({
  storage: multer.diskStorage({
    destination: UPLOADS_DIR,
    filename: (req, file, cb) => cb(null, `file_${Date.now()}_${file.originalname.replace(/[^a-zA-Z0-9._-]/g, '')}`)
  }),
  limits: { fileSize: 10 * 1024 * 1024 },
  fileFilter: (req, file, cb) => cb(null, ALLOWED_MIMES.test(file.mimetype))
});

// -- Session persistence --
function sessionFile(date) {
  const d = date || new Date();
  const stamp = d.toISOString().slice(0, 13).replace('T', '-');
  return path.join(SESSIONS_DIR, `${stamp}.json`);
}

function loadTodaySession() {
  for (let h = 0; h < 8; h++) {
    const d = new Date(Date.now() - h * 3600000);
    const f = sessionFile(d);
    if (fs.existsSync(f)) {
      try {
        const data = JSON.parse(fs.readFileSync(f, 'utf8'));
        if (data.transcript && data.transcript.trim()) return data;
      } catch(e) {}
    }
  }
  return null;
}

function saveSession(session) {
  try {
    fs.mkdirSync(SESSIONS_DIR, { recursive: true });
    const f = sessionFile();
    const existing = fs.existsSync(f) ? JSON.parse(fs.readFileSync(f, 'utf8')) : {};
    fs.writeFileSync(f, JSON.stringify({
      ...existing,
      transcript: session.transcript,
      meetingContext: session.meetingContext,
      suggestions: session.suggestions || [],
      images: session.images || [],
      updatedAt: new Date().toISOString(),
    }));
  } catch(e) {}
}

// -- Claude CLI call --
function cleanResponse(text) {
  const cleaned = (text || '')
    .replace(/\b(NOW|NEXT|BACKGROUND|ANGLE):\s*/gi, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  if (!cleaned) return null;
  if (/^\[skip\]/i.test(cleaned)) return null;
  if (/^\((timeout|no response|claude unavailable)\)/i.test(cleaned)) return null;
  return cleaned;
}

async function askClaude(prompt, model = 'sonnet') {
  const args = ['--print', '--model', model, '-p', '-'];
  return new Promise((resolve) => {
    const proc = spawn('claude', args, { stdio: ['pipe', 'pipe', 'pipe'] });
    proc.stdin.write(prompt);
    proc.stdin.end();
    let out = '', err = '';
    proc.stdout.on('data', d => out += d);
    proc.stderr.on('data', d => err += d);
    proc.on('close', (code) => {
      if (err) console.error(`[claude] stderr: ${err.slice(0, 200)}`);
      resolve(cleanResponse(out));
    });
    proc.on('error', () => resolve(null));
    const timeout = model === 'opus' ? 60000 : 30000;
    const timer = setTimeout(() => { proc.kill(); resolve(null); }, timeout);
    proc.on('close', () => clearTimeout(timer));
  });
}

// -- Context files --
const CONTEXTS_DIR = path.join(__dirname, 'contexts');
const SELECTION_FILE = path.join(__dirname, 'selected-files.json');

function getContextFiles() {
  const files = [];
  try {
    fs.readdirSync(CONTEXTS_DIR).filter(f => f.endsWith('.md')).sort().forEach(f => {
      files.push({ id: 'ctx:' + f, name: f.replace(/\.md$/, ''), path: path.join(CONTEXTS_DIR, f) });
    });
  } catch(e) {}
  const projDir = process.env.PROJECTS_DIR || (os.homedir() + '/projects');
  try {
    fs.readdirSync(projDir, { withFileTypes: true })
      .filter(d => d.isDirectory() && !d.name.startsWith('.'))
      .sort((a, b) => a.name.localeCompare(b.name))
      .forEach(d => {
        const p = path.join(projDir, d.name, 'PROJECT.md');
        if (fs.existsSync(p)) files.push({ id: 'proj:' + d.name, name: d.name, path: p });
      });
  } catch(e) {}
  return files;
}

function getSelectedFileIds() {
  try { return JSON.parse(fs.readFileSync(SELECTION_FILE, 'utf8')); }
  catch(e) { return []; }
}

function saveSelectedFileIds(ids) {
  fs.writeFileSync(SELECTION_FILE, JSON.stringify(ids));
}

function loadSelectedContext() {
  const selected = getSelectedFileIds();
  const allFiles = getContextFiles();
  const parts = [];
  for (const id of selected) {
    const file = allFiles.find(f => f.id === id);
    if (!file) continue;
    try {
      const content = fs.readFileSync(file.path, 'utf8').trim();
      if (content) parts.push(`--- ${file.name} ---\n${content}`);
    } catch(e) {}
  }
  return parts.join('\n\n');
}

function buildPrompt(meetingContext, priorSuggestions, transcript, imagePaths) {
  const context = loadSelectedContext();
  const prior = priorSuggestions.length > 0
    ? `\nPRIOR SUGGESTIONS:\n${priorSuggestions.map((s, i) => `${i + 1}. ${s}`).join('\n')}`
    : '';
  const custom = meetingContext ? `\nCONTEXT: ${meetingContext}` : '';
  const txWindow = transcript ? `\nCONVERSATION SO FAR:\n"${transcript.slice(-4200)}"` : '';

  let imageSection = '';
  if (imagePaths && imagePaths.length > 0) {
    const valid = imagePaths.filter(p => fs.existsSync(p));
    if (valid.length > 0) {
      imageSection = `\n\nUPLOADED FILES:\n${valid.map((p, i) => `${i + 1}. ${p}`).join('\n')}`;
    }
  }

  return `${context}\n${txWindow}${custom}${prior}${imageSection}`;
}

// -- Markdown flush --
function flushToMarkdown(session) {
  if (!session.transcript || !session.transcript.trim()) return;

  const now = new Date();
  const month = now.getMonth() + 1;
  const day = now.getDate();
  const dateStr = now.toLocaleDateString('en-US', { month: 'long', day: 'numeric' });
  const timeStr = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
  const filename = `${os.homedir()}/copilot${month}-${day}.md`;
  const fileExists = fs.existsSync(filename);

  const lines = [];
  if (!fileExists) lines.push(`# Copilot -- ${dateStr}`, '');
  lines.push(`## Session -- ${timeStr}`);
  const selFiles = getSelectedFileIds();
  if (selFiles.length) lines.push(`**Context files:** ${selFiles.join(', ')}`);
  if (session.meetingContext && session.meetingContext.trim()) lines.push(`**Context:** ${session.meetingContext.trim()}`);
  lines.push('');
  lines.push('### Transcript');
  lines.push(session.transcript.trim());
  lines.push('');
  lines.push('### Suggestions');
  if (session.suggestions && session.suggestions.length > 0) {
    session.suggestions.forEach(s => lines.push(`- [${s.tier}] ${s.text}`));
  } else {
    lines.push('(none)');
  }

  const block = lines.join('\n');
  try {
    if (fileExists) {
      fs.appendFileSync(filename, '\n\n---\n\n' + block);
    } else {
      fs.writeFileSync(filename, block);
    }
    console.log(`[copilot] saved to ${filename}`);
    return filename;
  } catch(e) {
    console.error('[copilot] flush failed:', e.message);
    return null;
  }
}

// -- Express + WebSocket server --
const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

app.get('/api/session', (req, res) => {
  const data = loadTodaySession();
  res.json(data ? { transcript: data.transcript, suggestions: data.suggestions || [], context: data.meetingContext || '', images: data.images || [] } : {});
});

app.get('/api/context-files', (req, res) => {
  const files = getContextFiles();
  const selected = getSelectedFileIds();
  res.json(files.map(f => ({ ...f, selected: selected.includes(f.id), path: undefined })));
});

app.get('/api/context-file', (req, res) => {
  const file = getContextFiles().find(f => f.id === req.query.id);
  if (!file) return res.status(404).send('');
  try { res.type('text/markdown').send(fs.readFileSync(file.path, 'utf8')); }
  catch(e) { res.status(500).send(''); }
});

app.post('/api/context-file', (req, res) => {
  const file = getContextFiles().find(f => f.id === req.body.id);
  if (!file) return res.status(404).json({ error: 'not found' });
  try { fs.writeFileSync(file.path, req.body.content || ''); res.json({ ok: true }); }
  catch(e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/context-file/create', (req, res) => {
  const name = (req.body.name || '').replace(/[^a-zA-Z0-9_-]/g, '');
  if (!name) return res.status(400).json({ error: 'invalid name' });
  fs.mkdirSync(CONTEXTS_DIR, { recursive: true });
  const fpath = path.join(CONTEXTS_DIR, name + '.md');
  if (fs.existsSync(fpath)) return res.status(409).json({ error: 'exists' });
  fs.writeFileSync(fpath, '');
  res.json({ ok: true, id: 'ctx:' + name + '.md' });
});

app.post('/api/context-file/delete', (req, res) => {
  const id = req.body.id;
  if (!id || !id.startsWith('ctx:')) return res.status(400).json({ error: 'can only delete custom files' });
  const file = getContextFiles().find(f => f.id === id);
  if (!file) return res.status(404).json({ error: 'not found' });
  try { fs.unlinkSync(file.path); } catch(e) {}
  const selected = getSelectedFileIds().filter(s => s !== id);
  saveSelectedFileIds(selected);
  res.json({ ok: true });
});

app.post('/api/selected-files', (req, res) => {
  saveSelectedFileIds(req.body.selected || []);
  res.json({ ok: true });
});

app.post('/api/upload-image', upload.single('image'), (req, res) => {
  if (!req.file) return res.status(400).json({ error: 'no image' });
  res.json({ ok: true, path: req.file.path, name: req.file.originalname });
});

app.use('/uploads', express.static(UPLOADS_DIR));

const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

const activeSessions = new Set();

function flushAll() {
  activeSessions.forEach(s => flushToMarkdown(s));
}
process.on('SIGTERM', () => { flushAll(); process.exit(0); });
process.on('SIGINT',  () => { flushAll(); process.exit(0); });

wss.on('connection', (ws) => {
  const id = Date.now().toString();
  const lastSession = loadTodaySession();
  const session = {
    transcript: lastSession?.transcript || '',
    meetingContext: lastSession?.meetingContext || '',
    suggestions: lastSession?.suggestions || [],
    images: lastSession?.images || [],
    recentSuggestions: [],
    _pending: false,
  };
  activeSessions.add(session);
  console.log(`[copilot] client connected: ${id}${lastSession ? ' (resumed)' : ''}`);

  ws.send(JSON.stringify({ type: 'status', text: 'Connected -- start speaking' }));

  if (lastSession?.transcript) {
    ws.send(JSON.stringify({
      type: 'session_resume',
      transcript: lastSession.transcript,
      context: lastSession.meetingContext,
      suggestions: (lastSession.suggestions || []).slice(-20),
      images: lastSession.images || [],
    }));
  }

  let suggestionDebounce = null;

  async function runSuggestion() {
    if (session._pending) return;
    if (!session.transcript || session.transcript.trim().split(/\s+/).length < 10) return;
    session._pending = true;
    ws.send(JSON.stringify({ type: 'status', text: 'Thinking...' }));
    const prompt = buildPrompt(session.meetingContext, session.recentSuggestions, session.transcript, session.images);
    const s = await askClaude(prompt, 'opus');
    session._pending = false;
    if (s) {
      session.recentSuggestions.push(s);
      if (session.recentSuggestions.length > 6) session.recentSuggestions.shift();
      session.suggestions.push({ text: s, tier: 'opus', ts: new Date().toISOString() });
      saveSession(session);
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'suggestion', text: s }));
      }
    }
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'status', text: 'Listening...' }));
    }
  }

  ws.on('message', async (data) => {
    try {
      const msg = JSON.parse(data.toString());

      if (msg.type === 'transcript') {
        // Browser sends transcript text (from Web Speech API)
        const text = (msg.text || '').trim();
        if (!text) return;
        session.transcript += (session.transcript ? ' ' : '') + text;
        saveSession(session);
        // Auto-suggest after 3s pause in speech
        if (suggestionDebounce) clearTimeout(suggestionDebounce);
        suggestionDebounce = setTimeout(runSuggestion, 3000);

      } else if (msg.type === 'context') {
        session.meetingContext = msg.context || '';
        ws.send(JSON.stringify({ type: 'status', text: 'Context saved' }));

      } else if (msg.type === 'select_files') {
        saveSelectedFileIds(msg.selected || []);
        ws.send(JSON.stringify({ type: 'status', text: `${(msg.selected || []).length} file(s) active` }));

      } else if (msg.type === 'suggest') {
        // Manual suggestion trigger
        await runSuggestion();

      } else if (msg.type === 'clear') {
        const savedFile = flushToMarkdown(session);
        session.transcript = '';
        session.recentSuggestions = [];
        session.suggestions = [];
        if (session.images) {
          session.images.forEach(p => { try { fs.unlinkSync(p); } catch(e) {} });
        }
        session.images = [];
        try {
          const files = fs.readdirSync(SESSIONS_DIR);
          for (const f of files) fs.unlinkSync(path.join(SESSIONS_DIR, f));
        } catch(e) {}
        const label = savedFile ? `Saved to ${savedFile.replace(os.homedir() + '/', '~/')}` : 'Cleared';
        ws.send(JSON.stringify({ type: 'status', text: label }));

      } else if (msg.type === 'image_added') {
        if (!session.images) session.images = [];
        session.images.push(msg.path);
        saveSession(session);

      } else if (msg.type === 'image_removed') {
        if (session.images) session.images = session.images.filter(p => p !== msg.path);
        saveSession(session);
      }
    } catch(e) {}
  });

  ws.on('close', () => {
    if (suggestionDebounce) clearTimeout(suggestionDebounce);
    activeSessions.delete(session);
    console.log(`[copilot] disconnected: ${id}`);
  });
});

server.listen(PORT, () => {
  console.log(`[copilot] running on port ${PORT}`);
  console.log('[copilot] transcription: browser Web Speech API (no API keys needed)');
});
