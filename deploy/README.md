# Deploy -- AI Agent Playbook

One-command deploy for the full AI agent stack on a fresh Ubuntu 22.04+ VPS.

## Quick Start

```bash
# 1. Clone the repo
git clone YOUR_REPO_URL agent-stack && cd agent-stack

# 2. Configure
cp deploy/.env.example deploy/.env
nano deploy/.env  # fill in DOMAIN, SLACK_BOT_TOKEN, SLACK_CHANNEL_ID

# 3. Deploy
bash deploy/install.sh
```

## What Gets Installed

| Component | Port | Description |
|-----------|------|-------------|
| Web Terminal (tty-web) | 4021 | Browser-based tmux access, mobile-optimized |
| Project Dashboard | 4025 | Auto-discovers projects, shows agent activity |
| Knowledge Base | 4090 | Wiki + full-text search + LLM-powered ingestion |
| Context Watcher | -- | Auto-summarizes terminal activity into CLAUDE.md |
| Task Queue | -- | SQLite queue + `q` CLI + cron dispatcher |
| Device Gate | 4444 | Slack-based device auth (no VPN needed) |

## Requirements

- Ubuntu 22.04+ (Debian works too)
- Root access
- $24/mo+ DigitalOcean droplet (1GB RAM minimum, 2GB recommended)
- Claude CLI (`npm install -g @anthropic-ai/claude-code`) for agent features
- Slack workspace (for device-gate auth notifications)

## Directory Layout After Install

```
/opt/agent-stack/
  .env              # your config
  tty-web/          # web terminal + dashboard
  knowledge/        # wiki + search
  heartbeat.sh      # context watcher
  taskq/            # task queue + dispatcher
  device-gate/      # auth service
  agent-ask.sh      # agent orchestration
```

## Service Management

```bash
systemctl status tty-web         # check web terminal
systemctl restart knowledge-base # restart wiki
journalctl -u context-watcher -f # watch heartbeat logs
q "run tests"                    # queue a task
q status                         # check task queue
```

## Ports to Open

The install script opens these via UFW:
- 22 (SSH)
- 4021 (Web Terminal)
- 4025 (Dashboard)
- 4090 (Knowledge Base)

## Troubleshooting

**Service won't start**: `journalctl -u SERVICE_NAME -n 50`
**Port not accessible**: `ufw allow PORT/tcp`
**Node version too old**: Script auto-upgrades to Node 20 if needed
**Claude CLI not found**: `npm install -g @anthropic-ai/claude-code && claude login`
