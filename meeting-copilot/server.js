#!/usr/bin/env node
/**
 * Meeting Copilot v3 — Two-tier suggestions: Sonnet (quick) + Opus (deep)
 * Port 4050 | Deepgram streaming → Tier1 every 15 words → Tier2 every 75 words
 */

const express = require('express');
const WebSocket = require('ws');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const os = require('os');
const multer = require('multer');

const PORT = 4050;
const DEEPGRAM_KEY = process.env.DEEPGRAM_API_KEY || '';
const SESSIONS_DIR = path.join(__dirname, 'sessions');
const UPLOADS_DIR = path.join(__dirname, 'uploads');
fs.mkdirSync(UPLOADS_DIR, { recursive: true });

// Multer config for file uploads (images + documents)
const ALLOWED_MIMES = /^(image\/(png|jpe?g|gif|webp)|application\/(pdf|json|msword|vnd\.openxmlformats|vnd\.ms-excel)|text\/(plain|markdown|csv|html))/;
const upload = multer({
  storage: multer.diskStorage({
    destination: UPLOADS_DIR,
    filename: (req, file, cb) => cb(null, `file_${Date.now()}_${file.originalname.replace(/[^a-zA-Z0-9._-]/g, '')}`)
  }),
  limits: { fileSize: 10 * 1024 * 1024 },
  fileFilter: (req, file, cb) => cb(null, ALLOWED_MIMES.test(file.mimetype))
});

// ─── Session persistence ───────────────────────────────────────────────────────
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

// Load VPS context (GLOBAL.md etc)
function loadContext() {
  const files = [process.env.HOME + '/GLOBAL.md', process.env.HOME + '/MASTER_PLAN.md', process.env.HOME + '/STRATEGY_STATE.md'];
  let ctx = '';
  for (const f of files) {
    try {
      const content = fs.readFileSync(f, 'utf8')
        .replace(/```[\s\S]*?```/g, '[block hidden]')
        .slice(0, 1500);
      ctx += `\n\n--- ${path.basename(f)} ---\n${content}`;
    } catch(e) {}
  }
  return ctx.trim();
}

// ─── Shared text cleaner ──────────────────────────────────────────────────────
function cleanResponse(text) {
  const cleaned = (text || '')
    .replace(/\b(NOW|NEXT|BACKGROUND|ANGLE):\s*/gi, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  if (!cleaned) return null;
  if (/^\[skip[\]—\-\s]/i.test(cleaned) || /^\[skip\]$/i.test(cleaned)) return null;
  if (/^\((timeout|no response|claude unavailable)\)/i.test(cleaned)) return null;
  return cleaned;
}

// ─── Claude CLI call (deep tier, uses Max subscription) ───────────────────────
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
      if (code !== 0) console.error(`[claude] exit code: ${code}`);
      resolve(cleanResponse(out));
    });
    proc.on('error', (e) => { console.error(`[claude] spawn error: ${e.message}`); resolve('(Claude unavailable)'); });
    const timer = setTimeout(() => { proc.kill(); resolve(null); }, model === 'opus' ? 60000 : 30000);
    proc.on('close', () => clearTimeout(timer));
  });
}

// ─── API-based quick racers ───────────────────────────────────────────────────
async function askClaudeAPI(userMsg, systemPrompt) {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': process.env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 200,
      system: systemPrompt,
      messages: [{ role: 'user', content: userMsg }],
    }),
    signal: AbortSignal.timeout(8000),
  });
  const data = await res.json();
  return cleanResponse(data.content?.[0]?.text);
}

async function askGPT(userMsg, systemPrompt) {
  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${process.env.OPENAI_API_KEY}`,
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: 'gpt-4o-mini',
      max_tokens: 200,
      messages: [
        { role: 'system', content: systemPrompt },
        { role: 'user', content: userMsg },
      ],
    }),
    signal: AbortSignal.timeout(8000),
  });
  const data = await res.json();
  return cleanResponse(data.choices?.[0]?.message?.content);
}

// Google OAuth token cache for Gemini
let _googleToken = null, _googleTokenExpiry = 0;
async function getGoogleToken() {
  if (_googleToken && Date.now() < _googleTokenExpiry) return _googleToken;
  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      client_id: process.env.GOOGLE_DRIVE_CLIENT_ID,
      client_secret: process.env.GOOGLE_DRIVE_CLIENT_SECRET,
      refresh_token: process.env.GOOGLE_DRIVE_REFRESH_TOKEN,
      grant_type: 'refresh_token',
    }),
    signal: AbortSignal.timeout(5000),
  });
  const data = await res.json();
  if (data.access_token) {
    _googleToken = data.access_token;
    _googleTokenExpiry = Date.now() + (data.expires_in - 60) * 1000;
  }
  return _googleToken;
}

async function askGemini(userMsg, systemPrompt) {
  const token = await getGoogleToken();
  if (!token) throw new Error('no google token');
  const res = await fetch('https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent', {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${token}`, 'content-type': 'application/json' },
    body: JSON.stringify({
      system_instruction: { parts: [{ text: systemPrompt }] },
      contents: [{ parts: [{ text: userMsg }] }],
      generationConfig: { maxOutputTokens: 200 },
    }),
    signal: AbortSignal.timeout(8000),
  });
  const data = await res.json();
  return cleanResponse(data.candidates?.[0]?.content?.parts?.[0]?.text);
}

// ─── Context files system ────────────────────────────────────────────────────
const CONTEXTS_DIR = path.join(__dirname, 'contexts');
const SELECTION_FILE = path.join(__dirname, 'selected-files.json');

function getContextFiles() {
  const files = [];
  // Custom files in contexts/
  try {
    fs.readdirSync(CONTEXTS_DIR).filter(f => f.endsWith('.md')).sort().forEach(f => {
      files.push({ id: 'ctx:' + f, name: f.replace(/\.md$/, ''), path: path.join(CONTEXTS_DIR, f) });
    });
  } catch(e) {}
  // Project PROJECT.md files
  try {
    fs.readdirSync(process.env.PROJECTS_DIR || (process.env.HOME + '/projects'), { withFileTypes: true })
      .filter(d => d.isDirectory() && !d.name.startsWith('.'))
      .sort((a, b) => a.name.localeCompare(b.name))
      .forEach(d => {
        const p = path.join(process.env.PROJECTS_DIR || (process.env.HOME + '/projects'), d.name, 'PROJECT.md');
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
      imageSection = `\n\nUPLOADED FILES - Read each file path below to view them, then factor into your response:\n${valid.map((p, i) => `${i + 1}. ${p}`).join('\n')}`;
    }
  }

  return `${context}\n${txWindow}${custom}${prior}${imageSection}`;
}

// ─── Transcribe chunk (Whisper fallback) ──────────────────────────────────────
function transcribeChunk(audioPath) {
  return new Promise((resolve) => {
    const proc = spawn('python3', ['/opt/meeting-copilot/transcribe.py', audioPath], {
      env: { ...process.env, WHISPER_MODEL: 'tiny' }
    });
    let out = '';
    proc.stdout.on('data', d => out += d);
    proc.on('close', () => resolve(out.trim()));
    proc.on('error', () => resolve(''));
    setTimeout(() => { proc.kill(); resolve(''); }, 25000);
  });
}

// ─── Deepgram streaming session ──────────────────────────────────────────────
function createDeepgramSession(clientWs, session) {
  if (!DEEPGRAM_KEY) return null;

  const dgWs = new WebSocket(
    'wss://api.deepgram.com/v1/listen?' + new URLSearchParams({
      model: 'nova-3',
      language: 'en-US',
      punctuate: 'true',
      interim_results: 'true',
      endpointing: '300',
      smart_format: 'true',
    }).toString(),
    { headers: { Authorization: `Token ${DEEPGRAM_KEY}` } }
  );

  dgWs.on('open', () => {
    console.log('[deepgram] connected');
    clientWs.send(JSON.stringify({ type: 'status', text: '🟢 Live transcription active' }));
    session.dgPing = setInterval(() => {
      if (dgWs.readyState === WebSocket.OPEN) {
        dgWs.send(JSON.stringify({ type: 'KeepAlive' }));
      }
    }, 8000);
    // Idle timeout: close Deepgram after 5 min of no speech to stop billing
    session._idleTimer = setTimeout(() => {
      console.log('[copilot] idle timeout — closing Deepgram to save costs');
      if (dgWs.readyState === WebSocket.OPEN) dgWs.close();
      clientWs.send(JSON.stringify({ type: 'status', text: 'idle — reload to resume' }));
    }, 5 * 60 * 1000);
  });

  async function runSuggestion() {
    console.log(`[copilot] runSuggestion called, pending=${session._suggestionPending}`);
    if (session._suggestionPending) return;
    session._suggestionPending = true;
    console.log('[copilot] calling Opus...');
    clientWs.send(JSON.stringify({ type: 'status', text: '🧠 Thinking...' }));
    const prompt = buildPrompt(session.meetingContext, session.recentQuickSuggestions, session.transcript, session.images);
    console.log(`[copilot] prompt length: ${prompt.length}`);
    const s = await askClaude(prompt, 'opus');
    console.log(`[copilot] Opus returned: ${s ? s.slice(0, 100) : '(null)'}`);
    session._suggestionPending = false;
    if (s) {
      session.recentQuickSuggestions.push(s);
      if (session.recentQuickSuggestions.length > 6) session.recentQuickSuggestions.shift();
      session.suggestions.push({ text: s, tier: 'opus', ts: new Date().toISOString() });
      saveSession(session);
      clientWs.send(JSON.stringify({ type: 'suggestion', text: s }));
    }
    clientWs.send(JSON.stringify({ type: 'status', text: '🟢 Listening...' }));
  }

  dgWs.on('message', async (raw) => {
    try {
      const msg = JSON.parse(raw);
      const alt = msg?.channel?.alternatives?.[0];
      if (!alt) return;

      const transcript = alt.transcript?.trim();
      if (!transcript) return;

      if (msg.is_final) {
        if (session._idleTimer && transcript.split(/\s+/).length > 4) {
          clearTimeout(session._idleTimer);
          session._idleTimer = setTimeout(() => {
            console.log('[copilot] idle timeout — closing Deepgram to save costs');
            if (dgWs.readyState === WebSocket.OPEN) dgWs.close();
            clientWs.send(JSON.stringify({ type: 'status', text: 'idle — reload to resume' }));
          }, 5 * 60 * 1000);
        }
        console.log(`[deepgram] transcript: "${transcript.slice(0, 60)}"`);
        session.transcript += ' ' + transcript;
        clientWs.send(JSON.stringify({ type: 'transcript', text: transcript, final: true }));
        saveSession(session);
        // Fire after 2s pause in speech
        if (session._suggestionDebounce) clearTimeout(session._suggestionDebounce);
        session._suggestionDebounce = setTimeout(runSuggestion, 2000);
      } else {
        clientWs.send(JSON.stringify({ type: 'transcript', text: transcript, final: false }));
      }
    } catch(e) {}
  });

  dgWs.on('error', (e) => {
    console.error('[deepgram] error:', e.message);
    clientWs.send(JSON.stringify({ type: 'status', text: '⚠ Deepgram error — using local Whisper' }));
  });

  dgWs.on('close', () => {
    console.log('[deepgram] disconnected');
    if (session.dgPing) clearInterval(session.dgPing);
    if (session._suggestionDebounce) clearTimeout(session._suggestionDebounce);
    if (clientWs.readyState === WebSocket.OPEN) {
      setTimeout(() => {
        session.dgWs = createDeepgramSession(clientWs, session);
      }, 1000);
    }
  });

  return dgWs;
}

// ─── Markdown flush ──────────────────────────────────────────────────────────
function flushToMarkdown(session) {
  if (!session.transcript || !session.transcript.trim()) return;

  const now = new Date();
  const month = now.getMonth() + 1;
  const day = now.getDate();
  const dateStr = now.toLocaleDateString('en-US', { month: 'long', day: 'numeric' });
  const timeStr = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
  const filename = `process.env.HOME + '/copilot${month}-${day}.md`;
  const fileExists = fs.existsSync(filename);

  const lines = [];
  if (!fileExists) lines.push(`# Copilot — ${dateStr}`, '');
  lines.push(`## Session — ${timeStr}`);
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
    console.log(`[copilot] saved → ${filename}`);
    return filename;
  } catch(e) {
    console.error('[copilot] flush failed:', e.message);
    return null;
  }
}

// ─── Express + WebSocket server ───────────────────────────────────────────────
const app = express();
app.use(express.json());
app.use((req, res, next) => { resetShutdownTimer(); next(); });
app.use(express.static(path.join(__dirname, 'public')));

app.get('/api/session', (req, res) => {
  const data = loadTodaySession();
  res.json(data ? { transcript: data.transcript, suggestions: data.suggestions || [], context: data.meetingContext || '', images: data.images || [] } : {});
});

// Context files API
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

// ─── Image upload API ────────────────────────────────────────────────────────
app.post('/api/upload-image', upload.single('image'), (req, res) => {
  if (!req.file) return res.status(400).json({ error: 'no image' });
  const imgPath = req.file.path;
  const imgName = req.file.originalname;
  // Add to all active sessions
  activeSessions.forEach(s => {
    if (!s.images) s.images = [];
    s.images.push(imgPath);
    saveSession(s);
  });
  // Notify connected clients
  wss.clients.forEach(c => {
    if (c.readyState === WebSocket.OPEN) {
      c.send(JSON.stringify({ type: 'image_added', path: imgPath, name: imgName, count: [...activeSessions][0]?.images?.length || 0 }));
    }
  });
  res.json({ ok: true, path: imgPath, name: imgName });
});

app.post('/api/remove-image', (req, res) => {
  const imgPath = req.body.path;
  if (!imgPath) return res.status(400).json({ error: 'no path' });
  activeSessions.forEach(s => {
    if (s.images) s.images = s.images.filter(p => p !== imgPath);
    saveSession(s);
  });
  // Delete file
  try { fs.unlinkSync(imgPath); } catch(e) {}
  wss.clients.forEach(c => {
    if (c.readyState === WebSocket.OPEN) {
      c.send(JSON.stringify({ type: 'image_removed', path: imgPath, count: [...activeSessions][0]?.images?.length || 0 }));
    }
  });
  res.json({ ok: true });
});

app.use('/uploads', express.static(UPLOADS_DIR));

const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

const activeSessions = new Set();

// ─── Auto-shutdown after 10 min of no activity ─────────────────────────────
const IDLE_SHUTDOWN_MS = 10 * 60 * 1000;
let _shutdownTimer = null;
function resetShutdownTimer() {
  if (_shutdownTimer) clearTimeout(_shutdownTimer);
  _shutdownTimer = setTimeout(() => {
    console.log('[copilot] no activity for 10 min — shutting down');
    flushAll();
    process.exit(0);
  }, IDLE_SHUTDOWN_MS);
}
resetShutdownTimer();

function flushAll() {
  activeSessions.forEach(s => flushToMarkdown(s));
}
process.on('SIGTERM', () => { flushAll(); process.exit(0); });
process.on('SIGINT',  () => { flushAll(); process.exit(0); });

wss.on('connection', (ws) => {
  resetShutdownTimer();
  const id = Date.now().toString();

  const lastSession = loadTodaySession();
  const session = {
    transcript: lastSession?.transcript || '',
    meetingContext: lastSession?.meetingContext || '',
    suggestions: lastSession?.suggestions || [],
    images: lastSession?.images || [],
    recentQuickSuggestions: [],
    _suggestionPending: false,
    dgWs: null, processing: false, chunks: []
  };
  activeSessions.add(session);
  ws.on('close', () => activeSessions.delete(session));
  console.log(`[copilot] client connected: ${id}${lastSession ? ' (resumed session)' : ''}`);

  session.dgWs = createDeepgramSession(ws, session);

  const mode = session.dgWs ? 'deepgram' : 'whisper';
  ws.send(JSON.stringify({
    type: 'status',
    text: session.dgWs ? '🟢 Live transcription active' : '✓ Connected (10s chunks via Whisper)'
  }));
  ws.send(JSON.stringify({ type: 'mode', mode }));

  if (lastSession?.transcript) {
    const allSugs = lastSession.suggestions || [];
    const resumeMsg = { type: 'session_resume', transcript: lastSession.transcript, context: lastSession.meetingContext, suggestions: allSugs.slice(-20), images: lastSession.images || [] };
    console.log(`[copilot] sending session_resume: ${resumeMsg.suggestions.length} suggestions, transcript ${resumeMsg.transcript.length} chars`);
    ws.send(JSON.stringify(resumeMsg));
  }

  ws.on('message', async (data) => {
    resetShutdownTimer();
    const isJson = (typeof data === 'string' && data[0] === '{') ||
                   (Buffer.isBuffer(data) && data[0] === 123);
    if (isJson) {
      try {
        const msg = JSON.parse(data.toString());
        if (msg.type === 'debug') {
          console.log(`[client:${id}] ${msg.msg}`);
          return;
        }
        if (msg.type === 'context') {
          session.meetingContext = msg.context || '';
          ws.send(JSON.stringify({ type: 'status', text: 'Context saved ✓' }));
        } else if (msg.type === 'select_files') {
          saveSelectedFileIds(msg.selected || []);
          const count = (msg.selected || []).length;
          console.log(`[copilot] ${count} context file(s) selected`);
          ws.send(JSON.stringify({ type: 'status', text: `${count} file(s) active ✓` }));
        } else if (msg.type === 'clear') {
          const savedFile = flushToMarkdown(session);
          session.transcript = '';
          session.recentQuickSuggestions = [];
          session.suggestions = [];
          // Clean up uploaded images
          if (session.images) {
            session.images.forEach(p => { try { fs.unlinkSync(p); } catch(e) {} });
          }
          session.images = [];
          // Delete all session files so old data doesn't reload
          try {
            const files = fs.readdirSync(SESSIONS_DIR);
            for (const f of files) fs.unlinkSync(path.join(SESSIONS_DIR, f));
          } catch(e) {}
          const label = savedFile ? `Saved → ${savedFile.replace(process.env.HOME + '/', '~/')} ✓` : 'Cleared ✓';
          ws.send(JSON.stringify({ type: 'status', text: label }));
        } else if (msg.type === 'suggest') {
          ws.send(JSON.stringify({ type: 'status', text: '🧠 Thinking...' }));
          const s = await askClaude(buildPrompt(session.meetingContext, session.recentQuickSuggestions, session.transcript, session.images), 'opus');
          if (s) {
            session.recentQuickSuggestions.push(s);
            if (session.recentQuickSuggestions.length > 6) session.recentQuickSuggestions.shift();
            session.suggestions.push({ text: s, tier: 'opus', ts: new Date().toISOString() });
            saveSession(session);
            ws.send(JSON.stringify({ type: 'suggestion', text: s }));
          }
          ws.send(JSON.stringify({ type: 'status', text: mode === 'deepgram' ? '🟢 Listening...' : '✓ Ready' }));
        }
        return;
      } catch(e) {}
    }

    // Audio data
    if (!session._firstAudioLogged) {
      session._firstAudioLogged = true;
      console.log(`[copilot] first audio chunk from ${id}: ${Buffer.byteLength(data)} bytes, dgWs=${session.dgWs?.readyState}`);
    }
    if (mode === 'deepgram' && session.dgWs?.readyState === WebSocket.OPEN) {
      session.dgWs.send(data);
      return;
    }

    // Fallback: Whisper 10s chunks
    if (session.processing) return;
    session.processing = true;

    const tmpFile = path.join(os.tmpdir(), `copilot_${id}_${Date.now()}.webm`);
    fs.writeFileSync(tmpFile, data);

    try {
      ws.send(JSON.stringify({ type: 'status', text: '🎤 Transcribing...' }));
      const text = transcribeChunk(tmpFile);
      if (text && text.length > 3) {
        session.transcript += ' ' + text;
        saveSession(session);
        ws.send(JSON.stringify({ type: 'transcript', text, final: true }));
        ws.send(JSON.stringify({ type: 'status', text: '🧠 Thinking...' }));
        const s = await askClaude(buildPrompt(session.meetingContext, session.recentQuickSuggestions, session.transcript, session.images), 'opus');
        if (s) {
          session.recentQuickSuggestions.push(s);
          if (session.recentQuickSuggestions.length > 6) session.recentQuickSuggestions.shift();
          session.suggestions.push({ text: s, tier: 'opus', ts: new Date().toISOString() });
          saveSession(session);
          ws.send(JSON.stringify({ type: 'suggestion', text: s }));
        }
      }
      ws.send(JSON.stringify({ type: 'status', text: '✓ Ready' }));
    } catch(e) {
      ws.send(JSON.stringify({ type: 'status', text: '⚠ ' + e.message }));
    } finally {
      session.processing = false;
      try { fs.unlinkSync(tmpFile); } catch(e) {}
    }
  });

  ws.on('close', () => {
    if (session.dgWs) {
      try { session.dgWs.close(); } catch(e) {}
    }
    if (session._suggestionDebounce) clearTimeout(session._suggestionDebounce);
    activeSessions.delete(session);
    console.log(`[copilot] disconnected: ${id}`);
    // Shut down when last client leaves
    if (wss.clients.size === 0) {
      console.log('[copilot] last client disconnected — shutting down');
      flushAll();
      process.exit(0);
    }
  });
});

// Support systemd socket activation (fd 3) or direct listen
const listenTarget = process.env.LISTEN_FDS ? { fd: 3 } : PORT;
server.listen(listenTarget, () => {
  const transcriptionMode = DEEPGRAM_KEY ? 'Deepgram live streaming' : 'local Whisper (10s chunks)';
  console.log(`[copilot] running on port ${PORT}${process.env.LISTEN_FDS ? ' (socket-activated)' : ''}`);
  console.log(`[copilot] transcription: ${transcriptionMode}`);
});
