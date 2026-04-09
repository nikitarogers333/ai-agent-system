#!/bin/bash
# Start all AI Agent System services
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/.env" 2>/dev/null

echo "Starting AI Agent System..."

mkdir -p "$DIR/logs" "$DIR/pids"
mkdir -p "$DIR/knowledge/raw" "$DIR/knowledge/wiki" "$DIR/knowledge/uploads" 2>/dev/null

# Ensure tmux server is running
tmux new-session -d -s _init 2>/dev/null && tmux kill-session -t _init 2>/dev/null

# Terminal (tty-web)
cd "$DIR/tty"
PORT="${TTY_PORT:-4021}" nohup node server.js > "$DIR/logs/tty.log" 2>&1 &
echo $! > "$DIR/pids/tty.pid"
echo "  Terminal:  http://localhost:${TTY_PORT:-4021}"

# Knowledge base (wiki)
cd "$DIR/knowledge"
PYTHON="$DIR/.venv/bin/python3"
[ ! -f "$PYTHON" ] && PYTHON="python3"
PORT="${WIKI_PORT:-4090}" nohup $PYTHON server.py > "$DIR/logs/knowledge.log" 2>&1 &
echo $! > "$DIR/pids/knowledge.pid"
echo "  Wiki:      http://localhost:${WIKI_PORT:-4090}"

# Meeting copilot (uses browser Web Speech API -- no API key needed)
cd "$DIR/meeting-copilot"
PORT="${COPILOT_PORT:-4051}" nohup node server.js > "$DIR/logs/copilot.log" 2>&1 &
echo $! > "$DIR/pids/copilot.pid"
echo "  Copilot:   http://localhost:${COPILOT_PORT:-4051}"

# Show LAN IP for phone access
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || ipconfig getifaddr en0 2>/dev/null || echo "unknown")
echo ""
echo "Phone (same WiFi): http://$LAN_IP:${TTY_PORT:-4021}"
echo "Logs: $DIR/logs/"
