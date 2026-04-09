const http = require('node:http');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { URL } = require('node:url');

const PORT = 4444;
const HOST = '127.0.0.1';
const DATA_DIR = process.env.DEVICE_GATE_DIR || '/opt/agent-stack/device-gate';
const DEVICES_FILE = path.join(DATA_DIR, 'devices.json');
const PENDING_FILE = path.join(DATA_DIR, 'pending.json');
const SECRET_FILE = path.join(DATA_DIR, '.secret');
const COOKIE_NAME = 'device_token';
const COOKIE_MAX_AGE = 90 * 24 * 60 * 60; // 90 days
const CODE_EXPIRY = 10 * 60 * 1000; // 10 minutes
const DOMAIN = process.env.DOMAIN || 'localhost';
const APPROVE_PORT = parseInt(process.env.APPROVE_PORT || '4022');
const THREAD_FILE = path.join(DATA_DIR, '.slack-thread.json');
const SLACK_TOKEN = process.env.SLACK_BOT_TOKEN || '';
const SLACK_CHANNEL = process.env.SLACK_CHANNEL_ID || '';

// Generate or load secret
let SECRET;
try {
  SECRET = fs.readFileSync(SECRET_FILE, 'utf-8').trim();
} catch {
  SECRET = crypto.randomBytes(32).toString('hex');
  fs.writeFileSync(SECRET_FILE, SECRET, { mode: 0o600 });
}

// --- Helpers ---

function loadJSON(filepath) {
  try { return JSON.parse(fs.readFileSync(filepath, 'utf-8')); }
  catch { return {}; }
}

function saveJSON(filepath, data) {
  const tmp = filepath + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, filepath);
}

function generateCode() {
  const chars = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  let code = '';
  const bytes = crypto.randomBytes(6);
  for (let i = 0; i < 6; i++) code += chars[bytes[i] % chars.length];
  return code;
}

function generateDeviceId() {
  return crypto.randomBytes(16).toString('hex');
}

function signToken(deviceId, ts) {
  return crypto.createHmac('sha256', SECRET)
    .update(`${deviceId}.${ts}`)
    .digest('hex')
    .slice(0, 40);
}

function makeToken(deviceId, ts) {
  return `${deviceId}.${ts}.${signToken(deviceId, ts)}`;
}

function verifyToken(token) {
  if (!token) return null;
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const [deviceId, ts, sig] = parts;
  if (signToken(deviceId, ts) !== sig) return null;
  const devices = loadJSON(DEVICES_FILE);
  if (!devices[deviceId]) return null;
  return deviceId;
}

function parseCookies(header) {
  const cookies = {};
  if (!header) return cookies;
  header.split(';').forEach(c => {
    const [k, ...v] = c.trim().split('=');
    if (k) cookies[k.trim()] = v.join('=').trim();
  });
  return cookies;
}

function setCookieHeader(token) {
  return `${COOKIE_NAME}=${token}; Max-Age=${COOKIE_MAX_AGE}; Path=/; Domain=${DOMAIN}; SameSite=Lax; Secure; HttpOnly`;
}

function notifyAdmin(message) {
  // Threaded Slack notifications (same pattern as ip-watchdog)
  let threadTs = null;
  try {
    const td = JSON.parse(fs.readFileSync(THREAD_FILE, 'utf-8'));
    // Reuse thread if < 24 hours old
    if (Date.now() - td.created < 86400000) threadTs = td.ts;
  } catch {}

  const payload = { channel: SLACK_CHANNEL, text: message };
  if (threadTs) {
    payload.thread_ts = threadTs;
  } else {
    payload.text = 'Device pairing requests:\n' + message;
  }

  const body = JSON.stringify(payload);
  const req = require('node:https').request({
    hostname: 'slack.com', path: '/api/chat.postMessage', method: 'POST',
    headers: { 'Authorization': `Bearer ${SLACK_TOKEN}`, 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) }
  }, (res) => {
    let data = '';
    res.on('data', c => data += c);
    res.on('end', () => {
      try {
        const r = JSON.parse(data);
        if (r.ok && !threadTs) {
          fs.writeFileSync(THREAD_FILE, JSON.stringify({ ts: r.ts, created: Date.now() }));
        }
      } catch {}
    });
  });
  req.on('error', (err) => {
    console.error('[gate] slack error:', err.message);
    // Fallback to notify-nik
    execFile('python3', [(process.env.AGENT_STACK_DIR || '/opt/agent-stack') + '/notify.py', message], () => {});
  });
  req.write(body);
  req.end();
}

function cleanExpired() {
  const pending = loadJSON(PENDING_FILE);
  const now = Date.now();
  let changed = false;
  for (const code in pending) {
    if (now - pending[code].createdAt > CODE_EXPIRY) {
      delete pending[code];
      changed = true;
    }
  }
  if (changed) saveJSON(PENDING_FILE, pending);
}

// --- HTML ---

function pairingPageHTML(code, returnUrl) {
  return `<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Device Pairing</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #0a0a0a; color: #fff;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { background: #1a1a1a; border: 1px solid #333; border-radius: 16px; padding: 40px;
          text-align: center; max-width: 380px; width: 90%; }
  h1 { font-size: 18px; color: #aaa; margin: 0 0 8px; font-weight: 400; }
  .code { font-size: 48px; font-family: monospace; letter-spacing: 8px; color: #4ade80;
          margin: 24px 0; font-weight: bold; }
  .status { color: #666; font-size: 14px; margin: 16px 0; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #333;
             border-top-color: #4ade80; border-radius: 50%; animation: spin 1s linear infinite;
             vertical-align: middle; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .timer { color: #555; font-size: 12px; margin-top: 12px; }
  .expired { color: #f87171; }
  .approved { color: #4ade80; }
</style>
</head><body>
<div class="card">
  <h1>New Device Detected</h1>
  <p style="color:#888;font-size:13px;">A pairing code has been sent to your Slack.</p>
  <div class="code" id="code">${code}</div>
  <div class="status" id="status">
    <span class="spinner"></span> Waiting for approval...
  </div>
  <div class="timer" id="timer"></div>
</div>
<script>
const code = "${code}";
const returnUrl = "${returnUrl || '/'}";
const expiry = Date.now() + ${CODE_EXPIRY};

function poll() {
  fetch('/gate/status/' + code)
    .then(r => r.json())
    .then(d => {
      if (d.approved) {
        document.getElementById('status').innerHTML = '<span class="approved">Approved! Redirecting...</span>';
        window.location.href = '/gate/claim/' + code + '?return=' + encodeURIComponent(returnUrl);
      } else if (Date.now() > expiry) {
        document.getElementById('status').innerHTML = '<span class="expired">Code expired. Refresh to try again.</span>';
      } else {
        setTimeout(poll, 2000);
      }
    })
    .catch(() => setTimeout(poll, 3000));
}

function updateTimer() {
  const left = Math.max(0, Math.floor((expiry - Date.now()) / 1000));
  const m = Math.floor(left / 60);
  const s = left % 60;
  document.getElementById('timer').textContent = left > 0 ? m + ':' + String(s).padStart(2, '0') + ' remaining' : '';
  if (left > 0) setTimeout(updateTimer, 1000);
}

poll();
updateTimer();
</script>
</body></html>`;
}

// --- Routes ---

function handleAuthCheck(req, res) {
  const cookies = parseCookies(req.headers.cookie);
  const deviceId = verifyToken(cookies[COOKIE_NAME]);
  if (deviceId) {
    res.writeHead(200, { 'X-Device-Id': deviceId });
    res.end('OK');
  } else {
    res.writeHead(401);
    res.end('Unauthorized');
  }
}

function handleGatePage(req, res) {
  cleanExpired();
  const url = new URL(req.url, `http://${HOST}`);
  const returnUrl = url.searchParams.get('return') || '/';
  const serverPort = req.headers['x-server-port'] || APPROVE_PORT;

  const code = generateCode();
  const deviceId = generateDeviceId();
  const ip = req.headers['x-real-ip'] || req.socket.remoteAddress;
  const ua = (req.headers['user-agent'] || '').slice(0, 80);

  const pending = loadJSON(PENDING_FILE);
  pending[code] = { deviceId, createdAt: Date.now(), returnUrl, ip, ua, approved: false };
  saveJSON(PENDING_FILE, pending);

  const approveUrl = `https://${DOMAIN}:${serverPort}/gate/approve/${code}`;
  notifyAdmin(`Device pairing request [${code}] from ${ip} (${ua}). Approve: ${approveUrl}`);

  res.writeHead(200, { 'Content-Type': 'text/html' });
  res.end(pairingPageHTML(code, returnUrl));
}

function handleApprove(req, res, code) {
  // Only already-authenticated devices can approve new ones
  const cookies = parseCookies(req.headers.cookie);
  const approverDeviceId = verifyToken(cookies[COOKIE_NAME]);
  if (!approverDeviceId) {
    res.writeHead(403, { 'Content-Type': 'text/html' });
    res.end('<html><body style="background:#0a0a0a;color:#f87171;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><h2>Not authorized. You must be on an already-paired device to approve.</h2></body></html>');
    return;
  }

  const pending = loadJSON(PENDING_FILE);
  const entry = pending[code];

  if (!entry) {
    res.writeHead(404, { 'Content-Type': 'text/html' });
    res.end('<html><body style="background:#0a0a0a;color:#f87171;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><h2>Code not found or expired.</h2></body></html>');
    return;
  }

  if (Date.now() - entry.createdAt > CODE_EXPIRY) {
    delete pending[code];
    saveJSON(PENDING_FILE, pending);
    res.writeHead(410, { 'Content-Type': 'text/html' });
    res.end('<html><body style="background:#0a0a0a;color:#f87171;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><h2>Code expired.</h2></body></html>');
    return;
  }

  // Mark approved
  entry.approved = true;
  entry.approvedAt = Date.now();
  saveJSON(PENDING_FILE, pending);

  // Add to devices
  const devices = loadJSON(DEVICES_FILE);
  devices[entry.deviceId] = {
    deviceId: entry.deviceId,
    ip: entry.ip,
    userAgent: entry.ua,
    createdAt: entry.createdAt,
    approvedAt: entry.approvedAt
  };
  saveJSON(DEVICES_FILE, devices);

  console.log(`[gate] Approved device ${entry.deviceId} (${entry.ip}) via code ${code}`);

  res.writeHead(200, { 'Content-Type': 'text/html' });
  res.end(`<html><body style="background:#0a0a0a;color:#4ade80;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;flex-direction:column">
    <h2>Device approved.</h2><p style="color:#888">You can close this tab.</p>
  </body></html>`);
}

function handleClaim(req, res, code) {
  const url = new URL(req.url, `http://${HOST}`);
  const returnUrl = url.searchParams.get('return') || '/';
  const pending = loadJSON(PENDING_FILE);
  const entry = pending[code];

  if (!entry || !entry.approved) {
    res.writeHead(403, { 'Content-Type': 'text/plain' });
    res.end('Not approved or code not found');
    return;
  }

  const ts = String(entry.approvedAt || Date.now());
  const token = makeToken(entry.deviceId, ts);

  // Clean up pending
  delete pending[code];
  saveJSON(PENDING_FILE, pending);

  res.writeHead(302, {
    'Set-Cookie': setCookieHeader(token),
    'Location': returnUrl
  });
  res.end();
}

function handleStatus(req, res, code) {
  const pending = loadJSON(PENDING_FILE);
  const entry = pending[code];
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ approved: entry ? entry.approved : false }));
}

function handleDevices(req, res) {
  // Simple admin endpoint to list paired devices
  const cookies = parseCookies(req.headers.cookie);
  const deviceId = verifyToken(cookies[COOKIE_NAME]);
  if (!deviceId) {
    res.writeHead(401, { 'Content-Type': 'text/plain' });
    res.end('Unauthorized');
    return;
  }
  const devices = loadJSON(DEVICES_FILE);
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(devices, null, 2));
}

// --- Server ---

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://${HOST}`);
  const p = url.pathname;

  try {
    if (p === '/auth/check') return handleAuthCheck(req, res);
    if (p === '/gate/' || p === '/gate') return handleGatePage(req, res);
    if (p.startsWith('/gate/approve/')) return handleApprove(req, res, p.split('/')[3]);
    if (p.startsWith('/gate/claim/')) return handleClaim(req, res, p.split('/')[3]);
    if (p.startsWith('/gate/status/')) return handleStatus(req, res, p.split('/')[3]);
    if (p === '/gate/devices') return handleDevices(req, res);

    res.writeHead(404);
    res.end('Not found');
  } catch (err) {
    console.error('[gate] Error:', err);
    res.writeHead(500);
    res.end('Internal error');
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[device-gate] Auth service running on ${HOST}:${PORT}`);
});
