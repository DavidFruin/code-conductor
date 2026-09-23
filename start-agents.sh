#!/bin/bash
# start-agents.sh
# Unix-native AI agent orchestration environment launcher.
# Composes tmux + Pi + PTY + SQLite + JSONL for project-local agent management.

set -euo pipefail

# Determine project directory (where this script lives)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PROJECT_DIR
PROJECT_NAME="$(basename "$PROJECT_DIR")"
export PROJECT_NAME

# Set up paths
AGENTS_DIR="$PROJECT_DIR/.agents"
export AGENTS_DIR
DB_PATH="$AGENTS_DIR/agents.sqlite"
AGENTD="$AGENTS_DIR/agentd.py"
LOG_FILE="$AGENTS_DIR/agentd.log"
PID_FILE="$AGENTS_DIR/agentd.pid"

# tmux session name (based on project name)
SESSION_NAME="agents-${PROJECT_NAME}"
export SESSION_NAME

# Ensure .agents directory structure exists
init_project() {
    mkdir -p "$AGENTS_DIR/skills/spawn-agent"

    # Initialize SQLite database if it doesn't exist
    if [ ! -f "$DB_PATH" ]; then
        "$AGENTD" init
    fi
}

# Check whether the tmux session already exists
session_exists() {
    tmux has-session -t "$SESSION_NAME" 2>/dev/null
}

# Check whether the agentd daemon is already running
daemon_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# Start the long-lived agentd supervisor daemon if it isn't already running
start_daemon() {
    if daemon_running; then
        echo "agentd daemon already running (pid $(cat "$PID_FILE"))"
        return
    fi

    echo "Starting agentd daemon..."
    PROJECT_DIR="$PROJECT_DIR" nohup "$AGENTD" serve >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 0.5
    if ! daemon_running; then
        echo "WARNING: agentd daemon failed to start (check $LOG_FILE)" >&2
    fi
}

# Tell agentd to create the initial session structure
# (agentd handles all tmux window/pane creation)
create_session() {
    PROJECT_DIR="$PROJECT_DIR" PROJECT_NAME="$PROJECT_NAME" "$AGENTD" create-session
}

# Attach to an existing session
attach_session() {
    echo "Attaching to existing session: $SESSION_NAME"
    echo "(Use Ctrl+B, D to detach)"
    tmux attach-session -t "$SESSION_NAME"
}

# Main execution
main() {
    init_project

    # If the session already exists, reattach rather than destroying it
    if session_exists; then
        start_daemon
        attach_session
        exit 0
    fi

    echo "Starting agent orchestration environment for project: $PROJECT_NAME"
    echo "Project directory: $PROJECT_DIR"

    create_session
    start_daemon

    echo "Session created: $SESSION_NAME"
    echo "  - monitor window"
    echo "  - orchestrator window (3 panes)"
    echo ""
    echo "Attaching to tmux session..."
    echo "(Use Ctrl+B, D to detach)"

    tmux attach-session -t "$SESSION_NAME"
}

main "$@"