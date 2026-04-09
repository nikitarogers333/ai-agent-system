#!/usr/bin/env bash
# heartbeat.sh -- monitors tmux sessions and updates project context conservatively

set -euo pipefail

PROJECTS_DIR="${PROJECTS_DIR:-/root/projects}"
SESSION_MAP="${SESSION_MAP:-$PROJECTS_DIR/.session-map.json}"
STATE_DIR="${STATE_DIR:-/tmp/heartbeat-state}"
LOG_FILE="${LOG_FILE:-/tmp/tmux-agents.log}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
HEARTBEAT_MODEL="${HEARTBEAT_MODEL:-haiku}"
CHECK_INTERVAL="${CHECK_INTERVAL:-60}"
LINES_THRESHOLD="${LINES_THRESHOLD:-3000}"
CAPTURE_LINES="${CAPTURE_LINES:-1500}"
MAX_CHARS="${MAX_CHARS:-25000}"
SESSION_MIN_INTERVAL="${SESSION_MIN_INTERVAL:-3600}"
GLOBAL_MIN_INTERVAL="${GLOBAL_MIN_INTERVAL:-900}"
ALLOW_ACTIVE_AGENT="${ALLOW_ACTIVE_AGENT:-0}"
FORCE_SAVE_ON_CLAUDE_UPDATE="${FORCE_SAVE_ON_CLAUDE_UPDATE:-0}"
CONTEXT_FILE="${CONTEXT_FILE:-CLAUDE.md}"

mkdir -p "$STATE_DIR"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE" 2>/dev/null || true
}

tmux_bin=""
if command -v tmux >/dev/null 2>&1; then
  tmux_bin="$(command -v tmux)"
elif [[ -x /usr/bin/tmux ]]; then
  tmux_bin="/usr/bin/tmux"
elif [[ -x /usr/local/bin/tmux ]]; then
  tmux_bin="/usr/local/bin/tmux"
fi

if [[ "$CLAUDE_BIN" != */* ]]; then
  CLAUDE_BIN="$(command -v "$CLAUDE_BIN" 2>/dev/null || true)"
fi

if [[ -z "$tmux_bin" ]] || [[ -z "$CLAUDE_BIN" ]] || [[ ! -x "$CLAUDE_BIN" ]]; then
  exit 0
fi

strip_ansi() {
  sed 's/\x1b\[[0-9;]*[mGKHFABCDJsurh]//g; s/\x1b[()][AB012]//g; s/\x1b\]0;[^\x07]*\x07//g; s/\r//g' | \
  grep -v '^[[:space:]]*$' | \
  tr -s ' '
}

get_project_for_session() {
  local session="$1"
  python3 - "$SESSION_MAP" "$session" <<'PY'
import json
import sys

mapping_path = sys.argv[1]
session = sys.argv[2]

try:
    with open(mapping_path) as fh:
        mapping = json.load(fh)
except Exception:
    print("")
    raise SystemExit

print(mapping.get(session, ""))
PY
}

get_line_count() {
  local session="$1"
  "$tmux_bin" capture-pane -p -S "-$CAPTURE_LINES" -t "$session" 2>/dev/null | wc -l || echo 0
}

read_int_file() {
  local path="$1"
  if [[ -f "$path" ]]; then
    cat "$path" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

hash_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    shasum -a 256 "$path" | awk '{print $1}'
  fi
}

get_active_command() {
  local session="$1"
  "$tmux_bin" display-message -p -t "$session" '#{pane_current_command}' 2>/dev/null | tr '[:upper:]' '[:lower:]'
}

run_update() {
  local context_path="$1"
  local session_content="$2"
  local current_hash="$3"
  local curr_count="$4"
  local state_file="$5"
  local last_run_file="$6"
  local last_hash_file="$7"
  local global_last_run_file="$8"
  local now="$9"

  local current_content
  current_content="$(cat "$context_path" 2>/dev/null || echo "")"

  local prompt_file output_file
  prompt_file="$(mktemp)"
  output_file="$(mktemp)"

  cat > "$prompt_file" <<EOF
You are updating a project context file that serves as living memory for AI agents working on this project.

Current file:
<current>
$current_content
</current>

Recent terminal session content:
<session>
$session_content
</session>

Task:
1. Extract only meaningful new information.
2. Merge it into the existing file.
3. Keep the document concise and factual.
4. Preserve all sections unless newer facts replace them.
5. If nothing meaningful changed, return the current file exactly as-is.
6. Output raw markdown only.
EOF

  if "$CLAUDE_BIN" --print --model "$HEARTBEAT_MODEL" "$(cat "$prompt_file")" > "$output_file" 2>>"$LOG_FILE"; then
    if [[ -s "$output_file" ]]; then
      local current_len response_len
      current_len="$(wc -c < "$context_path")"
      response_len="$(wc -c < "$output_file")"
      if (( current_len > 0 && response_len < current_len / 2 )); then
        rm -f "$prompt_file" "$output_file"
        return
      fi
      if ! cmp -s "$output_file" "$context_path"; then
        cp "$context_path" "${context_path}.bak" 2>/dev/null || true
        mv "$output_file" "$context_path"
        log "Updated $context_path"
      fi
      printf '%s\n' "$curr_count" > "$state_file"
      printf '%s\n' "$now" > "$last_run_file"
      printf '%s\n' "$current_hash" > "$last_hash_file"
      printf '%s\n' "$now" > "$global_last_run_file"
    fi
  fi

  rm -f "$prompt_file" "$output_file"
}

process_session() {
  local session="$1"
  local force_mode="${2:-0}"
  local capture_file="${3:-}"

  (
    local safe_session session_lock_dir pending_file state_file last_run_file last_hash_file
    local global_lock_dir global_last_run_file global_lock_owned=0
    safe_session="$(printf '%s' "$session" | tr -c '[:alnum:]._- ' '_')"
    session_lock_dir="$STATE_DIR/${safe_session}.lock"
    pending_file="$STATE_DIR/${safe_session}.pending"
    state_file="$STATE_DIR/${safe_session}.lines"
    last_run_file="$STATE_DIR/${safe_session}.last_run"
    last_hash_file="$STATE_DIR/${safe_session}.last_hash"
    global_lock_dir="$STATE_DIR/.global.lock"
    global_last_run_file="$STATE_DIR/.global.last_run"

    cleanup() {
      rm -rf "$session_lock_dir"
      if [[ "$global_lock_owned" == "1" ]]; then
        rm -rf "$global_lock_dir"
      fi
      rm -f "$pending_file"
    }

    if ! mkdir "$session_lock_dir" 2>/dev/null; then
      exit 0
    fi
    trap cleanup EXIT

    local project
    project="$(get_project_for_session "$session")"
    [[ -z "$project" ]] && exit 0

    local context_path="$PROJECTS_DIR/$project/$CONTEXT_FILE"
    [[ -f "$context_path" ]] || exit 0

    local prev_count=0 curr_count=0 new_lines=0
    curr_count="$(get_line_count "$session")"
    if [[ "$force_mode" != "1" ]]; then
      prev_count="$(read_int_file "$state_file")"
      new_lines=$(( curr_count - prev_count ))
      if (( new_lines < LINES_THRESHOLD )); then
        if (( curr_count < prev_count )); then
          printf '%s\n' "$curr_count" > "$state_file"
        fi
        exit 0
      fi
    fi

    local raw_content
    if [[ -n "$capture_file" && -f "$capture_file" ]]; then
      raw_content="$(cat "$capture_file")"
      rm -f "$capture_file"
    else
      raw_content="$("$tmux_bin" capture-pane -p -S "-$CAPTURE_LINES" -t "$session" 2>/dev/null || true)"
    fi
    [[ -z "${raw_content//[[:space:]]/}" ]] && exit 0

    printf '%s' "$raw_content" | strip_ansi | tail -c "$MAX_CHARS" > "$pending_file"
    [[ ! -s "$pending_file" ]] && exit 0

    local current_hash last_hash
    current_hash="$(hash_file "$pending_file")"
    last_hash="$(cat "$last_hash_file" 2>/dev/null || true)"
    if [[ "$current_hash" == "$last_hash" ]]; then
      printf '%s\n' "$curr_count" > "$state_file"
      exit 0
    fi

    if [[ "$force_mode" != "1" && "$ALLOW_ACTIVE_AGENT" != "1" ]]; then
      case "$(get_active_command "$session")" in
        claude|codex|gemini|opencode)
          exit 0
          ;;
      esac
    fi

    local now last_run global_last_run
    now="$(date +%s)"
    last_run="$(read_int_file "$last_run_file")"
    global_last_run="$(read_int_file "$global_last_run_file")"
    if [[ "$force_mode" != "1" ]]; then
      if (( now - last_run < SESSION_MIN_INTERVAL )); then
        exit 0
      fi
      if (( now - global_last_run < GLOBAL_MIN_INTERVAL )); then
        exit 0
      fi
    fi

    if ! mkdir "$global_lock_dir" 2>/dev/null; then
      exit 0
    fi
    global_lock_owned=1

    run_update "$context_path" "$(cat "$pending_file")" "$current_hash" "$curr_count" "$state_file" \
      "$last_run_file" "$last_hash_file" "$global_last_run_file" "$now"
  )
}

get_active_sessions() {
  "$tmux_bin" list-sessions -F '#{session_name}' 2>/dev/null || true
}

if [[ -n "${1:-}" ]]; then
  force_mode="${HEARTBEAT_FORCE:-0}"
  if [[ -n "${2:-}" && -f "${2:-}" ]]; then
    force_mode="1"
  fi
  process_session "$1" "$force_mode" "${2:-}"
  exit 0
fi

log "heartbeat started (threshold=${LINES_THRESHOLD} lines, interval=${CHECK_INTERVAL}s, model=${HEARTBEAT_MODEL})"

claude_target="$(readlink "$CLAUDE_BIN" 2>/dev/null || echo "")"

while true; do
  current_target="$(readlink "$CLAUDE_BIN" 2>/dev/null || echo "")"
  if [[ "$FORCE_SAVE_ON_CLAUDE_UPDATE" == "1" && -n "$claude_target" && "$current_target" != "$claude_target" ]]; then
    log "Claude binary changed: $claude_target -> $current_target"
    claude_target="$current_target"
    while IFS= read -r session; do
      [[ -n "$session" ]] && process_session "$session" "1"
    done < <(get_active_sessions)
    wait
    sleep "$CHECK_INTERVAL"
    continue
  fi
  claude_target="$current_target"

  while IFS= read -r session; do
    [[ -n "$session" ]] && process_session "$session" &
  done < <(get_active_sessions)
  wait
  sleep "$CHECK_INTERVAL"
done
