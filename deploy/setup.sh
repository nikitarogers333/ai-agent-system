#!/bin/bash
# AI Agent System -- Setup Script
# Installs tty-web (terminal), Nikipedia (wiki), and Meeting Copilot on your machine.
# Works on Mac, Linux, and WSL. Access from phone on same network.
#
# Usage:
#   ./setup.sh            # local install (default)
#   ./setup.sh --vps      # VPS install (adds systemd + nginx + UFW)
#
# After install:
#   Terminal:  http://localhost:4021  (or http://YOUR_IP:4021 from phone)
#   Wiki:      http://localhost:4090
#   Copilot:   http://localhost:4051  (needs DEEPGRAM_API_KEY)

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

MODE="local"
[ "${1:-}" = "--vps" ] && MODE="vps"

INSTALL_DIR="${INSTALL_DIR:-$HOME/ai-agent-system}"

info() { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[x]${NC} $*"; exit 1; }

# Wait for apt lock to be free (common on fresh systems / Chromebook Linux)
wait_for_apt() {
  local max_wait=60
  local waited=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do
    if [ $waited -eq 0 ]; then
      info "Waiting for package manager to finish..."
    fi
    sleep 2
    waited=$((waited + 2))
    if [ $waited -ge $max_wait ]; then
      warn "Package manager still locked after ${max_wait}s. Trying anyway..."
      break
    fi
  done
}

apt_install() {
  wait_for_apt
  sudo apt-get install -y "$@"
}

# =============================
# CHECK PREREQUISITES
# =============================
info "Checking prerequisites..."

# Node
if ! command -v node &>/dev/null; then
  warn "Node.js not found. Installing..."
  if command -v brew &>/dev/null; then
    brew install node
  elif command -v apt-get &>/dev/null; then
    wait_for_apt
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    apt_install nodejs
  else
    fail "Cannot install Node. Install Node.js 18+ manually: https://nodejs.org"
  fi
fi
NODE_VER=$(node --version)
info "Node: $NODE_VER"

# Python 3
if ! command -v python3 &>/dev/null; then
  warn "Python 3 not found. Installing..."
  if command -v brew &>/dev/null; then
    brew install python3
  elif command -v apt-get &>/dev/null; then
    apt_install python3 python3-pip
  else
    fail "Cannot install Python. Install Python 3.10+ manually."
  fi
fi
PY_VER=$(python3 --version)
info "Python: $PY_VER"

# tmux
if ! command -v tmux &>/dev/null; then
  warn "tmux not found. Installing..."
  if command -v brew &>/dev/null; then
    brew install tmux
  elif command -v apt-get &>/dev/null; then
    apt_install tmux
  else
    fail "Cannot install tmux. Install manually."
  fi
fi
TMUX_VER=$(tmux -V)
info "tmux: $TMUX_VER"

# Python packages
info "Installing Python dependencies..."
if command -v pip3 &>/dev/null; then
  pip3 install --quiet fastapi uvicorn python-multipart aiofiles 2>/dev/null || \
    pip3 install --user --quiet fastapi uvicorn python-multipart aiofiles
else
  python3 -m pip install --quiet fastapi uvicorn python-multipart aiofiles 2>/dev/null || \
    python3 -m pip install --user --quiet --break-system-packages fastapi uvicorn python-multipart aiofiles 2>/dev/null || \
    { apt_install python3-pip 2>/dev/null && pip3 install --quiet fastapi uvicorn python-multipart aiofiles; }
fi

# Claude CLI (check, don't install -- user needs their own account)
if command -v claude &>/dev/null; then
  info "Claude CLI: found"
else
  warn "Claude CLI not found."
  warn "Install it: npm install -g @anthropic-ai/claude-code"
  warn "Then run: claude login"
  warn "The system works without it but wiki compilation and AI features need it."
fi

# =============================
# INSTALL
# =============================
info "Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"

# Copy or check if files already exist (for git clone scenario)
if [ ! -f "$INSTALL_DIR/tty/server.js" ]; then
  # If running from repo, files are relative
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  REPO_ROOT="$(dirname "$SCRIPT_DIR")"

  if [ -d "$REPO_ROOT/tty" ]; then
    info "Copying from repo..."
    cp -r "$REPO_ROOT/tty" "$INSTALL_DIR/"
    cp -r "$REPO_ROOT/knowledge" "$INSTALL_DIR/"
    cp -r "$REPO_ROOT/meeting-copilot" "$INSTALL_DIR/"
  else
    fail "Source files not found. Run this from the repo directory or set INSTALL_DIR."
  fi
fi

# Install node dependencies
info "Installing tty-web dependencies..."
cd "$INSTALL_DIR/tty" && npm install --quiet 2>/dev/null

info "Installing meeting-copilot dependencies..."
cd "$INSTALL_DIR/meeting-copilot" && npm install --quiet 2>/dev/null

# =============================
# CONFIGURATION
# =============================
info "Configuring..."

# Create .env if not exists
ENV_FILE="$INSTALL_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'ENVEOF'
# AI Agent System Configuration
# Edit this file and restart services to apply changes.

# Meeting Copilot (optional -- get key at https://console.deepgram.com)
DEEPGRAM_API_KEY=

# Ports (change if conflicts)
TTY_PORT=4021
WIKI_PORT=4090
COPILOT_PORT=4050
ENVEOF
  info "Created $ENV_FILE -- edit to add API keys"
fi

# =============================
# CREATE START/STOP SCRIPTS
# =============================

cat > "$INSTALL_DIR/start.sh" <<'STARTEOF'
#!/bin/bash
# Start all services
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/.env" 2>/dev/null

echo "Starting AI Agent System..."

# Ensure tmux server is running
tmux new-session -d -s _init 2>/dev/null && tmux kill-session -t _init 2>/dev/null

# Start tty-web
cd "$DIR/tty"
PORT="${TTY_PORT:-4021}" nohup node server.js > "$DIR/logs/tty.log" 2>&1 &
echo $! > "$DIR/pids/tty.pid"
echo "  Terminal:  http://localhost:${TTY_PORT:-4021}"

# Start Nikipedia
cd "$DIR/knowledge"
nohup python3 server.py > "$DIR/logs/knowledge.log" 2>&1 &
echo $! > "$DIR/pids/knowledge.pid"
echo "  Wiki:      http://localhost:${WIKI_PORT:-4090}"

# Start Meeting Copilot (only if Deepgram key is set)
if [ -n "${DEEPGRAM_API_KEY:-}" ]; then
  cd "$DIR/meeting-copilot"
  nohup node server.js > "$DIR/logs/copilot.log" 2>&1 &
  echo $! > "$DIR/pids/copilot.pid"
  echo "  Copilot:   http://localhost:${COPILOT_PORT:-4050}"
else
  echo "  Copilot:   skipped (set DEEPGRAM_API_KEY in .env)"
fi

# Show LAN IP for phone access
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || ipconfig getifaddr en0 2>/dev/null || echo "unknown")
echo ""
echo "Access from phone (same WiFi): http://$LAN_IP:${TTY_PORT:-4021}"
echo "Logs: $DIR/logs/"
STARTEOF
chmod +x "$INSTALL_DIR/start.sh"

cat > "$INSTALL_DIR/stop.sh" <<'STOPEOF'
#!/bin/bash
# Stop all services
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Stopping AI Agent System..."
for pidfile in "$DIR/pids/"*.pid; do
  [ -f "$pidfile" ] || continue
  PID=$(cat "$pidfile")
  kill "$PID" 2>/dev/null && echo "  Stopped $(basename "$pidfile" .pid) (PID $PID)"
  rm -f "$pidfile"
done
echo "Done."
STOPEOF
chmod +x "$INSTALL_DIR/stop.sh"

cat > "$INSTALL_DIR/status.sh" <<'STATUSEOF'
#!/bin/bash
# Check service status
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Service Status:"
for pidfile in "$DIR/pids/"*.pid; do
  [ -f "$pidfile" ] || continue
  NAME=$(basename "$pidfile" .pid)
  PID=$(cat "$pidfile")
  if kill -0 "$PID" 2>/dev/null; then
    echo "  $NAME: running (PID $PID)"
  else
    echo "  $NAME: dead (stale PID $PID)"
    rm -f "$pidfile"
  fi
done
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || ipconfig getifaddr en0 2>/dev/null || echo "unknown")
echo ""
echo "LAN access: http://$LAN_IP:4021"
STATUSEOF
chmod +x "$INSTALL_DIR/status.sh"

mkdir -p "$INSTALL_DIR/logs" "$INSTALL_DIR/pids"

# =============================
# VPS-SPECIFIC SETUP
# =============================
if [ "$MODE" = "vps" ]; then
  info "Setting up VPS services..."

  # Systemd services
  for svc in tty knowledge copilot; do
    case $svc in
      tty)
        EXEC="node $INSTALL_DIR/tty/server.js"
        WDIR="$INSTALL_DIR/tty"
        ;;
      knowledge)
        EXEC="python3 $INSTALL_DIR/knowledge/server.py"
        WDIR="$INSTALL_DIR/knowledge"
        ;;
      copilot)
        EXEC="node $INSTALL_DIR/meeting-copilot/server.js"
        WDIR="$INSTALL_DIR/meeting-copilot"
        ;;
    esac

    sudo tee "/etc/systemd/system/ai-${svc}.service" > /dev/null <<SVCEOF
[Unit]
Description=AI Agent System - $svc
After=network.target

[Service]
Type=simple
WorkingDirectory=$WDIR
ExecStart=$EXEC
EnvironmentFile=$INSTALL_DIR/.env
Restart=always
RestartSec=5
KillMode=process

[Install]
WantedBy=multi-user.target
SVCEOF
  done

  sudo systemctl daemon-reload
  sudo systemctl enable ai-tty ai-knowledge ai-copilot
  sudo systemctl start ai-tty ai-knowledge

  # UFW
  if command -v ufw &>/dev/null; then
    sudo ufw allow 4021/tcp
    sudo ufw allow 4090/tcp
    sudo ufw allow 4051/tcp
    info "Opened ports 4021, 4090, 4051"
  fi

  info "VPS services configured. Use: sudo systemctl status ai-tty"
fi

# =============================
# DONE
# =============================
echo ""
echo "============================================"
info "Installation complete!"
echo ""
echo "  Start:   $INSTALL_DIR/start.sh"
echo "  Stop:    $INSTALL_DIR/stop.sh"
echo "  Status:  $INSTALL_DIR/status.sh"
echo ""
echo "  Terminal:  http://localhost:4021"
echo "  Wiki:      http://localhost:4090"
echo "  Copilot:   http://localhost:4051 (needs DEEPGRAM_API_KEY)"
echo ""

LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || ipconfig getifaddr en0 2>/dev/null || echo "unknown")
echo "  Phone access (same WiFi): http://$LAN_IP:4021"
echo ""

if ! command -v claude &>/dev/null; then
  warn "Claude CLI not installed. Wiki compilation and AI features need it."
  warn "Install: npm install -g @anthropic-ai/claude-code && claude login"
fi

echo "============================================"
