#!/usr/bin/env bash
# AI Agent Playbook -- One-Command Deploy Script
# Installs the full agent stack on a fresh Ubuntu 22.04+ VPS
#
# Usage:
#   curl -sL https://raw.githubusercontent.com/REPO/main/deploy/install.sh | bash
#   OR
#   git clone REPO && cd deploy && bash install.sh
#
# Prerequisites: Ubuntu 22.04+, root access, $24/mo+ DigitalOcean droplet (or equivalent)
# Time: ~5 minutes

set -euo pipefail

INSTALL_DIR="/opt/agent-stack"
REPO_DIR="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || echo "/tmp/agent-stack-repo")"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; exit 1; }

# ============================================================
# Pre-flight checks
# ============================================================

[[ $EUID -ne 0 ]] && err "Must run as root"
[[ ! -f /etc/os-release ]] && err "Cannot detect OS"

source /etc/os-release
[[ "$ID" != "ubuntu" && "$ID" != "debian" ]] && warn "Tested on Ubuntu 22.04+. Your OS: $PRETTY_NAME. Continuing anyway..."

log "Starting AI Agent Playbook installation..."

# ============================================================
# Load .env if exists, otherwise prompt
# ============================================================

ENV_FILE="$REPO_DIR/deploy/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    ENV_FILE="$(dirname "$0")/.env"
fi

if [[ -f "$ENV_FILE" ]]; then
    log "Loading config from $ENV_FILE"
    set -a
    source "$ENV_FILE"
    set +a
else
    warn "No .env file found. Using defaults (device-gate will need manual config)."
    warn "Copy deploy/.env.example to deploy/.env and re-run for full setup."
fi

DOMAIN="${DOMAIN:-localhost}"
SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
SLACK_CHANNEL_ID="${SLACK_CHANNEL_ID:-}"
PROJECTS_DIR="${PROJECTS_DIR:-/root/projects}"
WORKER_SESSIONS="${WORKER_SESSIONS:-worker1 worker2 worker3}"

# ============================================================
# System packages
# ============================================================

log "Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    tmux \
    sqlite3 \
    python3 \
    python3-pip \
    nodejs \
    npm \
    nginx \
    ufw \
    curl \
    git \
    jq \
    2>/dev/null

# Node.js 20+ check
NODE_VER=$(node -v 2>/dev/null | sed 's/v//' | cut -d. -f1)
if [[ "$NODE_VER" -lt 18 ]]; then
    log "Installing Node.js 20 LTS..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi

# Python packages for Nikipedia
log "Installing Python dependencies..."
pip3 install -q fastapi uvicorn python-multipart aiofiles 2>/dev/null

# ============================================================
# Create directory structure
# ============================================================

log "Setting up directory structure..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$PROJECTS_DIR"
mkdir -p /var/log

# ============================================================
# Component 1: Web Terminal (tty-web) -- port 4021
# ============================================================

log "Installing web terminal (tty-web)..."
if [[ -d "$REPO_DIR/components/tty-web" ]]; then
    cp -r "$REPO_DIR/components/tty-web" "$INSTALL_DIR/tty-web"
else
    warn "tty-web component not found in repo. Cloning from GitHub..."
    git clone --depth 1 https://github.com/nikitarogers333/tmux-dash.git "$INSTALL_DIR/tty-web" 2>/dev/null || warn "Could not clone tty-web repo"
fi

if [[ -d "$INSTALL_DIR/tty-web" ]]; then
    cd "$INSTALL_DIR/tty-web"
    npm install --production 2>/dev/null
    log "  tty-web installed at $INSTALL_DIR/tty-web"
fi

# ============================================================
# Component 2: Knowledge Base (Nikipedia) -- port 4090
# ============================================================

log "Installing knowledge base..."
if [[ -d "$REPO_DIR/components/knowledge" ]]; then
    cp -r "$REPO_DIR/components/knowledge" "$INSTALL_DIR/knowledge"
else
    mkdir -p "$INSTALL_DIR/knowledge"
    mkdir -p "$INSTALL_DIR/knowledge/wiki"
    mkdir -p "$INSTALL_DIR/knowledge/raw"
    mkdir -p "$INSTALL_DIR/knowledge/uploads"
    warn "Knowledge base component not in repo. Directory created but needs server.py."
fi
log "  knowledge base at $INSTALL_DIR/knowledge"

# ============================================================
# Component 3: Context Watcher (heartbeat)
# ============================================================

log "Installing context watcher..."
if [[ -f "$REPO_DIR/components/heartbeat.sh" ]]; then
    cp "$REPO_DIR/components/heartbeat.sh" "$INSTALL_DIR/heartbeat.sh"
    chmod +x "$INSTALL_DIR/heartbeat.sh"
    # Patch paths
    sed -i "s|PROJECTS_DIR=\"/root/projects\"|PROJECTS_DIR=\"$PROJECTS_DIR\"|g" "$INSTALL_DIR/heartbeat.sh"
else
    warn "heartbeat.sh not found in repo components."
fi

# ============================================================
# Component 4: Task Queue
# ============================================================

log "Installing task queue..."
mkdir -p "$INSTALL_DIR/taskq/results"

if [[ -d "$REPO_DIR/components/taskq" ]]; then
    cp "$REPO_DIR/components/taskq/dispatcher.sh" "$INSTALL_DIR/taskq/dispatcher.sh" 2>/dev/null
    cp "$REPO_DIR/components/taskq/init.sql" "$INSTALL_DIR/taskq/init.sql" 2>/dev/null
    cp "$REPO_DIR/components/taskq/q" "$INSTALL_DIR/taskq/q" 2>/dev/null
    chmod +x "$INSTALL_DIR/taskq/dispatcher.sh" "$INSTALL_DIR/taskq/q" 2>/dev/null
else
    # Create init.sql inline
    cat > "$INSTALL_DIR/taskq/init.sql" <<'SQL'
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prompt TEXT NOT NULL,
  session TEXT DEFAULT NULL,
  status TEXT DEFAULT 'queued' CHECK(status IN ('queued','running','done','failed','cancelled')),
  priority INTEGER DEFAULT 5,
  depends_on TEXT DEFAULT NULL,
  created_at DATETIME DEFAULT (datetime('now')),
  started_at DATETIME DEFAULT NULL,
  completed_at DATETIME DEFAULT NULL,
  result_file TEXT DEFAULT NULL,
  error TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_priority ON tasks(priority DESC, created_at ASC);
SQL
    warn "Task queue scripts not found. Database schema created. Copy dispatcher.sh and q CLI manually."
fi

# Initialize task queue database
sqlite3 "$INSTALL_DIR/taskq/tasks.db" < "$INSTALL_DIR/taskq/init.sql" 2>/dev/null || true

# Install q CLI globally
if [[ -f "$INSTALL_DIR/taskq/q" ]]; then
    ln -sf "$INSTALL_DIR/taskq/q" /usr/local/bin/q
    # Patch DB path in q
    sed -i "s|DB=\"/opt/taskq/tasks.db\"|DB=\"$INSTALL_DIR/taskq/tasks.db\"|g" /usr/local/bin/q
    log "  q CLI installed globally"
fi

# Patch dispatcher worker sessions
if [[ -f "$INSTALL_DIR/taskq/dispatcher.sh" ]]; then
    sed -i "s|WORKER_SESSIONS=\"sterm sterm1 ster\"|WORKER_SESSIONS=\"$WORKER_SESSIONS\"|g" "$INSTALL_DIR/taskq/dispatcher.sh"
    sed -i "s|DB=\"/opt/taskq/tasks.db\"|DB=\"$INSTALL_DIR/taskq/tasks.db\"|g" "$INSTALL_DIR/taskq/dispatcher.sh"
fi

# ============================================================
# Component 5: Device Gate -- port 4444
# ============================================================

log "Installing device gate..."
if [[ -d "$REPO_DIR/components/device-gate" ]]; then
    cp -r "$REPO_DIR/components/device-gate" "$INSTALL_DIR/device-gate"
    # Parameterize hardcoded values
    if [[ -f "$INSTALL_DIR/device-gate/server.js" ]]; then
        sed -i "s|const DOMAIN = '.*';|const DOMAIN = process.env.DOMAIN \|\| '$DOMAIN';|g" "$INSTALL_DIR/device-gate/server.js"
        sed -i "s|const SLACK_TOKEN = '.*';|const SLACK_TOKEN = process.env.SLACK_BOT_TOKEN \|\| '';|g" "$INSTALL_DIR/device-gate/server.js"
        sed -i "s|const SLACK_CHANNEL = '.*';|const SLACK_CHANNEL = process.env.SLACK_CHANNEL_ID \|\| '';|g" "$INSTALL_DIR/device-gate/server.js"
        log "  device-gate parameterized (reads from env)"
    fi
else
    warn "device-gate component not found in repo."
fi

# ============================================================
# Component 6: Agent Orchestration
# ============================================================

log "Installing agent orchestration scripts..."
if [[ -d "$REPO_DIR/components/orchestration" ]]; then
    cp "$REPO_DIR/components/orchestration/agent-ask.sh" "$INSTALL_DIR/agent-ask.sh" 2>/dev/null
    chmod +x "$INSTALL_DIR/agent-ask.sh" 2>/dev/null
fi

# Notification script
if [[ -f "$REPO_DIR/components/notify.py" ]]; then
    cp "$REPO_DIR/components/notify.py" "$INSTALL_DIR/notify.py"
    chmod +x "$INSTALL_DIR/notify.py"
    log "  notify.py installed"
fi

# ============================================================
# Store .env for services
# ============================================================

log "Writing environment config..."
cat > "$INSTALL_DIR/.env" <<EOF
DOMAIN=$DOMAIN
SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN
SLACK_CHANNEL_ID=$SLACK_CHANNEL_ID
PROJECTS_DIR=$PROJECTS_DIR
WORKER_SESSIONS=$WORKER_SESSIONS
EOF
chmod 600 "$INSTALL_DIR/.env"

# ============================================================
# Install systemd services
# ============================================================

log "Installing systemd services..."
SERVICES_DIR="$REPO_DIR/deploy/services"

for svc in tty-web knowledge-base context-watcher device-gate taskq-dispatcher; do
    if [[ -f "$SERVICES_DIR/$svc.service" ]]; then
        cp "$SERVICES_DIR/$svc.service" "/etc/systemd/system/$svc.service"
        log "  installed $svc.service"
    fi
done

# Timer for task queue
if [[ -f "$SERVICES_DIR/taskq-dispatcher.timer" ]]; then
    cp "$SERVICES_DIR/taskq-dispatcher.timer" "/etc/systemd/system/taskq-dispatcher.timer"
fi

systemctl daemon-reload

# Enable and start core services
for svc in tty-web knowledge-base context-watcher; do
    if [[ -f "/etc/systemd/system/$svc.service" ]]; then
        systemctl enable "$svc" 2>/dev/null
        systemctl start "$svc" 2>/dev/null && log "  started $svc" || warn "  failed to start $svc (check: journalctl -u $svc)"
    fi
done

# Only start device-gate if Slack is configured
if [[ -n "$SLACK_BOT_TOKEN" ]]; then
    systemctl enable device-gate 2>/dev/null
    systemctl start device-gate 2>/dev/null && log "  started device-gate" || warn "  device-gate failed (check Slack token)"
else
    warn "  device-gate skipped (no SLACK_BOT_TOKEN in .env)"
fi

# Start task queue timer
systemctl enable taskq-dispatcher.timer 2>/dev/null
systemctl start taskq-dispatcher.timer 2>/dev/null

# ============================================================
# Firewall
# ============================================================

log "Configuring firewall..."
ufw allow 22/tcp   2>/dev/null  # SSH
ufw allow 4021/tcp 2>/dev/null  # tty-web
ufw allow 4025/tcp 2>/dev/null  # project dashboard
ufw allow 4090/tcp 2>/dev/null  # knowledge base
ufw --force enable 2>/dev/null || true

# ============================================================
# Create initial tmux sessions
# ============================================================

log "Creating tmux worker sessions..."
for sess in $WORKER_SESSIONS; do
    tmux new-session -d -s "$sess" 2>/dev/null || true
done
log "  created sessions: $WORKER_SESSIONS"

# ============================================================
# Claude CLI check
# ============================================================

if command -v claude &>/dev/null; then
    log "Claude CLI found at $(which claude)"
else
    warn "Claude CLI not found. Install it for full functionality:"
    warn "  npm install -g @anthropic-ai/claude-code"
    warn "  Then run: claude login"
fi

# ============================================================
# Verification
# ============================================================

log ""
log "============================================"
log "  AI Agent Playbook -- Installation Complete"
log "============================================"
log ""
log "Services:"

check_port() {
    local name=$1 port=$2
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port" 2>/dev/null | grep -q "200\|301\|302"; then
        echo -e "  ${GREEN}[OK]${NC} $name -- http://localhost:$port"
    else
        echo -e "  ${YELLOW}[--]${NC} $name -- http://localhost:$port (not responding yet, may need a moment)"
    fi
}

check_port "Web Terminal"    4021
check_port "Dashboard"       4025
check_port "Knowledge Base"  4090

log ""
log "Quick start:"
log "  1. Open http://YOUR_IP:4021 for the web terminal"
log "  2. Open http://YOUR_IP:4090 for the knowledge base"
log "  3. Run 'q \"hello world\"' to test the task queue"
log "  4. Check service status: systemctl status tty-web"
log ""
log "Logs:"
log "  journalctl -u tty-web -f"
log "  journalctl -u knowledge-base -f"
log "  journalctl -u context-watcher -f"
log ""

if [[ -z "$SLACK_BOT_TOKEN" ]]; then
    warn "Next: configure .env with Slack credentials for device-gate auth"
    warn "  nano $INSTALL_DIR/.env"
    warn "  systemctl start device-gate"
fi

log "Done. Total install time: ${SECONDS}s"
