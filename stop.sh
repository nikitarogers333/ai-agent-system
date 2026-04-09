#!/bin/bash
# Stop all AI Agent System services
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Stopping AI Agent System..."
for pidfile in "$DIR/pids/"*.pid; do
  [ -f "$pidfile" ] || continue
  NAME=$(basename "$pidfile" .pid)
  PID=$(cat "$pidfile")
  if kill "$PID" 2>/dev/null; then
    echo "  Stopped $NAME (PID $PID)"
  else
    echo "  $NAME already stopped"
  fi
  rm -f "$pidfile"
done
echo "Done."
