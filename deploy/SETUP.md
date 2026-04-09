# Setup Guide: AI Agent Stack

Complete walkthrough from purchase to running system. Target: under 30 minutes.

## Prerequisites

| Requirement | Details |
|---|---|
| VPS | Ubuntu 22.04+, 2GB RAM, root access. $24/mo DigitalOcean droplet works. |
| Domain | Optional but recommended. Can use IP directly during setup. |
| Slack workspace | Required for device-gate authentication. Free tier works. |
| Claude Max subscription | Required for agent features. Sign up at anthropic.com. |

## Step 1: Provision VPS (5 min)

Create a DigitalOcean droplet (or equivalent):
- Image: Ubuntu 22.04 LTS
- Plan: Basic, $24/mo (2 vCPU, 2GB RAM, 50GB SSD)
- Region: closest to you
- Authentication: SSH key (not password)

SSH in:
```bash
ssh root@YOUR_IP
```

## Step 2: Clone and Configure (2 min)

```bash
git clone https://github.com/YOUR_REPO agent-stack
cd agent-stack
cp deploy/.env.example deploy/.env
nano deploy/.env
```

Fill in three required values:

| Variable | Where to get it |
|---|---|
| `DOMAIN` | Your domain or VPS IP address |
| `SLACK_BOT_TOKEN` | api.slack.com/apps -> Create App -> OAuth & Permissions -> Bot User OAuth Token. Scopes needed: `chat:write`, `reactions:read` |
| `SLACK_CHANNEL_ID` | Right-click channel in Slack -> View channel details -> scroll to bottom |

Save and exit (`Ctrl+X`, `Y`, `Enter` in nano).

## Step 3: Run Install Script (5 min)

```bash
bash deploy/install.sh
```

The script will:
1. Install system packages (tmux, sqlite3, python3, nodejs, nginx, ufw)
2. Upgrade Node.js to v20 if needed
3. Install Python dependencies (FastAPI, uvicorn)
4. Copy all 6 components to `/opt/agent-stack/`
5. Install and start systemd services
6. Configure UFW firewall (opens ports 22, 4021, 4025, 4090)
7. Create tmux worker sessions
8. Verify all services respond

Watch for `[!]` warnings -- they indicate optional components that need attention.

## Step 4: Verify Installation (2 min)

Check each service:

```bash
# All services at once
systemctl status tty-web knowledge-base context-watcher device-gate

# Individual service logs
journalctl -u tty-web -n 20
journalctl -u knowledge-base -n 20
```

Open in browser:

| Service | URL | What you should see |
|---|---|---|
| Web Terminal | `http://YOUR_IP:4021` | tmux session in browser |
| Knowledge Base | `http://YOUR_IP:4090` | Wiki interface with search |
| Dashboard | `http://YOUR_IP:4025` | Project list with activity |

## Step 5: Install Claude CLI (3 min)

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

Follow the prompts to authenticate. This powers all agent features.

Test it:
```bash
claude -p "echo hello"
```

## Step 6: Test Task Queue (2 min)

```bash
# Queue a task
q "list files in /root"

# Check status
q status

# Watch it execute (in a tmux session)
tmux attach -t worker1
```

The task queue dispatcher runs on a systemd timer. Tasks get assigned to tmux worker sessions where Claude executes them.

## Step 7: Configure Device Gate (optional, 5 min)

Device gate provides Slack-based authentication. When someone accesses a protected service, they get a Slack message to approve/deny.

If you set SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in `.env`:
```bash
systemctl start device-gate
systemctl status device-gate
```

Test by visiting `http://YOUR_IP:4444` -- you should get a Slack approval request.

## Architecture Overview

```
/opt/agent-stack/
  .env                 # Configuration (DOMAIN, Slack tokens)
  tty-web/             # Web terminal -- browser-based tmux (port 4021)
  knowledge/           # Wiki + FTS search (port 4090)
    wiki/              # Markdown articles
    raw/               # Ingested raw content
    search.db          # SQLite FTS5 index
  heartbeat.sh         # Context watcher -- summarizes activity into CLAUDE.md
  taskq/               # Task queue system
    tasks.db           # SQLite task database
    dispatcher.sh      # Picks tasks, assigns to tmux sessions
    q                  # CLI: q "prompt" to queue, q status to check
  device-gate/         # Slack-based auth (port 4444)
    server.js          # Express server, reads from .env
  agent-ask.sh         # Orchestration: ask Claude questions from scripts
  notify.py            # Send Slack notifications from scripts
```

How services connect:
- **Heartbeat** watches tmux sessions, writes summaries to CLAUDE.md files
- **Task queue** dispatches work to tmux sessions where Claude executes
- **Knowledge base** indexes all wiki articles for search; Claude queries it for context
- **Device gate** protects web endpoints with Slack approval
- **Notify** sends alerts from any script via `python3 /opt/agent-stack/notify.py "message"`

## Service Management Reference

```bash
# Start/stop/restart
systemctl start tty-web
systemctl stop knowledge-base
systemctl restart context-watcher

# View logs (follow mode)
journalctl -u tty-web -f
journalctl -u knowledge-base -f
journalctl -u context-watcher -f

# Task queue
q "your task prompt"        # Queue a task
q status                     # List all tasks
q cancel 5                   # Cancel task #5
systemctl status taskq-dispatcher.timer  # Check dispatcher

# Tmux sessions
tmux ls                      # List all sessions
tmux attach -t worker1       # Attach to worker
# Ctrl+B, D to detach
```

## Ports Reference

| Port | Service | Firewall |
|---|---|---|
| 22 | SSH | Open |
| 4021 | Web Terminal (tty-web) | Open |
| 4025 | Project Dashboard | Open |
| 4090 | Knowledge Base | Open |
| 4444 | Device Gate (auth) | Closed by default |

To open additional ports:
```bash
ufw allow PORT/tcp
```

## Troubleshooting

### Service won't start
```bash
journalctl -u SERVICE_NAME -n 50
# Common: missing npm deps, wrong Node version, port conflict
```

### Port not accessible from outside
```bash
ufw status          # Check if port is allowed
ufw allow PORT/tcp  # Open it
```

### Node.js version too old
```bash
node -v  # Need 18+
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
```

### Knowledge base empty
```bash
# Create your first wiki article
cat > /opt/agent-stack/knowledge/wiki/hello.md << 'EOF'
# Hello World
This is your first wiki article.
EOF
# Restart to reindex
systemctl restart knowledge-base
```

### Task queue not processing
```bash
# Check timer is active
systemctl status taskq-dispatcher.timer
# Check for stuck tasks
q status
# Check worker sessions exist
tmux ls
```

### Claude CLI not found
```bash
npm install -g @anthropic-ai/claude-code
claude login
# Verify
which claude
claude -p "echo test"
```

### Device gate Slack messages not arriving
1. Verify bot token: `grep SLACK_BOT_TOKEN /opt/agent-stack/.env`
2. Verify channel ID: `grep SLACK_CHANNEL_ID /opt/agent-stack/.env`
3. Check bot is in the channel (invite it via `/invite @botname`)
4. Check logs: `journalctl -u device-gate -n 50`

## Optional: HTTPS with nginx

For production use, set up HTTPS:

```bash
apt-get install -y certbot python3-certbot-nginx

# Create nginx config for each service
cat > /etc/nginx/sites-available/agent-stack << 'NGINX'
server {
    server_name YOUR_DOMAIN;
    
    location /terminal/ {
        proxy_pass http://localhost:4021/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    
    location /wiki/ {
        proxy_pass http://localhost:4090/;
    }
    
    location /dashboard/ {
        proxy_pass http://localhost:4025/;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/agent-stack /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Get SSL cert
certbot --nginx -d YOUR_DOMAIN
```

## Optional: Add More Knowledge

Populate the wiki with your project documentation:

```bash
# Copy markdown files
cp ~/docs/*.md /opt/agent-stack/knowledge/wiki/

# Or use the ingest script (if installed)
python3 /opt/agent-stack/knowledge/ingest.py "https://some-url.com/article"
```

## What's Next

Once everything is running:
1. Create CLAUDE.md files in your project directories (Claude reads these for context)
2. Queue your first real task: `q "review the codebase in /root/projects/myapp and suggest improvements"`
3. Add wiki articles about your projects so Claude has context
4. Set up cron jobs for recurring tasks (e.g., daily code review, log analysis)

The system improves as you use it -- more wiki articles mean better context, more CLAUDE.md files mean better per-project behavior.
