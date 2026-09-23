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

# Kill existing session if it exists
kill_existing_session() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session $SESSION_NAME already exists. Killing..."
        tmux kill-session -t "$SESSION_NAME"
    fi
}

# Tell agentd to create the initial session structure
# (agentd handles all tmux window/pane creation)
create_session() {
    PROJECT_DIR="$PROJECT_DIR" PROJECT_NAME="$PROJECT_NAME" "$AGENTD" create-session
}

# Main execution
main() {
    echo "Starting agent orchestration environment for project: $PROJECT_NAME"
    echo "Project directory: $PROJECT_DIR"
    
    init_project
    kill_existing_session
    create_session
    
    echo "Session created: $SESSION_NAME"
    echo "  - Window 0: monitor"
    echo "  - Window 1: orchestrator (3 panes)"
    echo ""
    echo "Attaching to tmux session..."
    echo "(Use Ctrl+B, D to detach)"
    
    tmux attach-session -t "$SESSION_NAME"
}

main "$@"
