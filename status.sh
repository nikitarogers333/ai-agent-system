#!/bin/bash
# Check AI Agent System service status
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/.env" 2>/dev/null

TTY_PORT="${TTY_PORT:-4021}"
WIKI_PORT="${WIKI_PORT:-4090}"
COPILOT_PORT="${COPILOT_PORT:-4051}"

echo "AI Agent System Status"
echo "======================"

found=0
for pidfile in "$DIR/pids/"*.pid; do
  [ -f "$pidfile" ] || continue
  found=1
  NAME=$(basename "$pidfile" .pid)
  PID=$(cat "$pidfile")
  if kill -0 "$PID" 2>/dev/null; then
    echo "  $NAME: running (PID $PID)"
  else
    echo "  $NAME: dead (stale PID $PID)"
    rm -f "$pidfile"
  fi
done

[ "$found" -eq 0 ] && echo "  No services running."

echo ""
echo "Ports:"
echo "  Terminal:  http://localhost:$TTY_PORT"
echo "  Wiki:      http://localhost:$WIKI_PORT"
echo "  Copilot:   http://localhost:$COPILOT_PORT"

LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || ipconfig getifaddr en0 2>/dev/null || echo "unknown")
echo ""
echo "Phone (same WiFi): http://$LAN_IP:$TTY_PORT"
