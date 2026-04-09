#!/bin/bash
# Task queue dispatcher -- finds idle sessions, dispatches queued tasks via claude -p
# Run: dispatcher.sh (single pass) or via systemd timer
export PATH="/root/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

STACK_DIR="${AGENT_STACK_DIR:-/opt/agent-stack}"
DB="$STACK_DIR/taskq/tasks.db"
RESULTS_DIR="$STACK_DIR/taskq/results"
WORKER_SESSIONS="${WORKER_SESSIONS:-worker1 worker2 worker3}"
LOG="/var/log/taskq-dispatcher.log"

mkdir -p "$RESULTS_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# Find idle tmux sessions (running bash, not claude)
get_idle_sessions() {
  for sess in $WORKER_SESSIONS; do
    if tmux has-session -t "$sess" 2>/dev/null; then
      CMD=$(tmux display-message -t "$sess" -p '#{pane_current_command}')
      if [ "$CMD" = "bash" ] || [ "$CMD" = "zsh" ]; then
        echo "$sess"
      fi
    fi
  done
}

# Get next task -- prefer assigned, then highest priority unassigned
get_next_task() {
  local sess="$1"
  ID=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT id FROM tasks WHERE status='queued' AND session='$sess' ORDER BY priority DESC, created_at ASC LIMIT 1;")
  if [ -n "$ID" ]; then echo "$ID"; return; fi
  ID=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT id FROM tasks WHERE status='queued' AND (session IS NULL OR session='') AND (depends_on IS NULL OR depends_on IN (SELECT id FROM tasks WHERE status='done')) ORDER BY priority DESC, created_at ASC LIMIT 1;")
  echo "$ID"
}

dispatch_task() {
  local TASK_ID="$1"
  local SESSION="$2"

  PROMPT=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT prompt FROM tasks WHERE id=$TASK_ID;")
  [ -z "$PROMPT" ] && return 1

  RESULT_FILE="$RESULTS_DIR/task-${TASK_ID}.md"

  sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE tasks SET status='running', session='$SESSION', started_at=datetime('now') WHERE id=$TASK_ID;"
  log "dispatching #$TASK_ID to $SESSION: $(echo "$PROMPT" | head -c 80)"

  PROJECTS_DIR="${PROJECTS_DIR:-/root/projects}"
  PROJ=$(python3 -c "import json; m=json.load(open('$PROJECTS_DIR/.session-map.json')); print(m.get('$SESSION',''))" 2>/dev/null)
  CWD="$PROJECTS_DIR/${PROJ:-terminal}"

  # 5 minute timeout per task
  timeout 300 bash -c "cd '$CWD' && claude -p --model sonnet '$(echo "$PROMPT" | sed "s/'/'\\\\''/g")'" > "$RESULT_FILE" 2>&1
  EXIT=$?

  if [ $EXIT -eq 0 ]; then
    sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE tasks SET status='done', completed_at=datetime('now'), result_file='$RESULT_FILE' WHERE id=$TASK_ID;"
    log "completed #$TASK_ID ($(wc -c < "$RESULT_FILE") bytes)"
  else
    ERR="exit code $EXIT"
    [ $EXIT -eq 124 ] && ERR="timeout (5min)"
    sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE tasks SET status='failed', completed_at=datetime('now'), result_file='$RESULT_FILE', error='$ERR' WHERE id=$TASK_ID;"
    log "failed #$TASK_ID: $ERR"
  fi
}

# Single pass: match idle sessions to queued tasks
DISPATCHED=0
for SESS in $(get_idle_sessions); do
  TASK_ID=$(get_next_task "$SESS")
  [ -z "$TASK_ID" ] && continue
  dispatch_task "$TASK_ID" "$SESS" &
  DISPATCHED=$((DISPATCHED + 1))
done

wait

if [ $DISPATCHED -gt 0 ]; then
  log "dispatched $DISPATCHED tasks"
else
  QUEUED=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT COUNT(*) FROM tasks WHERE status='queued';")
  [ "$QUEUED" -gt 0 ] && log "no idle sessions ($QUEUED queued)"
fi
