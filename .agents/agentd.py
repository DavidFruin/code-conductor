#!/usr/bin/env python3
"""
agentd.py - Unix-native AI agent orchestration runtime supervisor

Manages Pi processes, PTYs, tmux windows/panes, JSONL communication,
and SQLite event persistence for project-local agent orchestration.

Architecture:
    start-agents.sh ──> tmux session
                    └──> agentd.py (runtime supervisor)
                        ├── Pi processes
                        ├── PTYs (tmux panes)
                        ├── JSONL channels
                        ├── SQLite database
                        └── tmux windows/panes

Usage:
    python3 agentd.py <command> [args...]

Commands:
    init              Initialize the runtime environment
    create-session    Create the initial tmux session (monitor + orchestrator)
    serve             Run the long-lived supervisor daemon
    spawn <name> <task>  Spawn a new sub-agent with a task
    status            Show status of all agents
    monitor           Render the live agent monitor panel (window 0)
    send <agent_id>   Send a message to an agent (reads from stdin)
    stop <agent_id>   Stop a specific agent
    kill-all          Stop all agents and clean up
    sessions          List tmux sessions
"""

import os
import sys
import json
import sqlite3
import subprocess
import threading
import time
import argparse
from pathlib import Path
from datetime import datetime
from uuid import uuid4


class AgentOrchestrator:
    """Main runtime supervisor for managing Pi agents in tmux."""

    def __init__(self, project_dir: str):
        self.project_dir = Path(project_dir).resolve()
        self.session_name = f"agents-{self.project_dir.name}"
        self.db_path = self.project_dir / ".agents" / "agents.sqlite"
        self.agents_dir = self.project_dir / ".agents"
        self.skills_dir = self.agents_dir / "skills"
        self.comms_dir = self.agents_dir / "comms"
        self.pty_logs_dir = self.agents_dir / "pty_logs"
        self.log_path = self.agents_dir / "agentd.log"
        self.pid_path = self.agents_dir / "agentd.pid"

        self.agents = {}  # agent_id -> agent info
        self.processes = {}  # agent_id -> process info (pid, threads, etc.)
        self.next_window_index = 2  # fallback when tmux window discovery fails
        self.project_name = self.project_dir.name

    def _db(self):
        """Open a SQLite connection with WAL mode and a busy timeout.

        WAL allows the long-running daemon to write while CLI commands
        (spawn, status, send) read concurrently without locking each other.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_db(self):
        """Initialize SQLite database schema if needed."""
        schema = """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            parent_id TEXT,
            status TEXT DEFAULT 'created',
            cwd TEXT,
            window_index INTEGER,
            window_name TEXT,
            pid INTEGER,
            branch TEXT,
            worktree TEXT,
            last_event TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            FOREIGN KEY (parent_id) REFERENCES agents(id)
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            task TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            from_agent TEXT NOT NULL,
            to_agent TEXT,
            type TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );
        CREATE TABLE IF NOT EXISTS pty_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT,
            window_index INTEGER,
            pane_index INTEGER,
            input_output TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );
        CREATE TABLE IF NOT EXISTS agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agent_id) REFERENCES agents(id)
        );
        """

        conn = self._db()
        conn.executescript(schema)
        conn.commit()
        conn.close()
        self._migrate()

    def _migrate(self):
        """Add missing columns to an existing agents table (schema upgrades)."""
        conn = self._db()
        existing = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
        additions = {
            "name": "TEXT NOT NULL",
            "parent_id": "TEXT",
            "status": "TEXT DEFAULT 'created'",
            "cwd": "TEXT",
            "window_index": "INTEGER",
            "window_name": "TEXT",
            "pid": "INTEGER",
            "branch": "TEXT",
            "worktree": "TEXT",
            "last_event": "TEXT",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "updated_at": "TIMESTAMP",
            "started_at": "TIMESTAMP",
            "ended_at": "TIMESTAMP",
        }
        for col, coltype in additions.items():
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {coltype}")
                except sqlite3.OperationalError:
                    pass
        conn.commit()
        conn.close()

    def _init_directories(self):
        """Create required directory structure."""
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.comms_dir.mkdir(parents=True, exist_ok=True)
        self.pty_logs_dir.mkdir(parents=True, exist_ok=True)

    def log_agent_event(self, agent_id: str, event_type: str, payload: dict = None):
        """Log an event for an agent or session."""
        conn = self._db()
        conn.execute(
            "INSERT INTO agent_events (agent_id, event_type, payload) VALUES (?, ?, ?)",
            (agent_id, event_type, json.dumps(payload) if payload else None)
        )
        conn.commit()
        conn.close()

    def log_message(self, from_agent: str, to_agent: str, msg_type: str, payload: dict):
        """Log a communication message."""
        conn = self._db()
        conn.execute(
            "INSERT INTO messages (from_agent, to_agent, type, payload) VALUES (?, ?, ?, ?)",
            (from_agent, to_agent, msg_type, json.dumps(payload))
        )
        conn.commit()
        conn.close()

    def log_pty_event(self, agent_id: str, window_index: int, pane_index: int,
                      input_output: str, content: str):
        """Log PTY output event."""
        conn = self._db()
        conn.execute(
            "INSERT INTO pty_events (agent_id, window_index, pane_index, input_output, content) VALUES (?, ?, ?, ?, ?)",
            (agent_id, window_index, pane_index, input_output, content)
        )
        conn.commit()
        conn.close()

    def _mark_status(self, agent_id: str, status: str, ended: bool = False, event: str = "agent.status_changed"):
        """Update an agent's registry status and emit a lifecycle event."""
        conn = self._db()
        ended_sql = ", ended_at=CURRENT_TIMESTAMP" if ended else ""
        conn.execute(
            f"UPDATE agents SET status=?, last_event=?, updated_at=CURRENT_TIMESTAMP{ended_sql} WHERE id=?",
            (status, event, agent_id)
        )
        conn.commit()
        conn.close()
        self.log_agent_event(agent_id, event, {"status": status})
        print(f"agent {agent_id} -> {status}", file=sys.stderr)

    def tmux_command(self, *args):
        """Execute a tmux command."""
        try:
            result = subprocess.run(
                ["tmux", *args],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                print(f"tmux command failed: {' '.join(args)}", file=sys.stderr)
                print(f"  stderr: {result.stderr}", file=sys.stderr)
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            print(f"tmux command timed out: {' '.join(args)}", file=sys.stderr)
            return ""
        except Exception as e:
            print(f"tmux command failed: {e}", file=sys.stderr)
            return ""

    def _pane_roles(self, window_name: str):
        """Discover the pane index for each role in a window.

        Roles are determined by vertical position: the top pane is the Pi
        agent, the middle pane is the PTY viewer, and the bottom pane is the
        COMMS viewer. This works regardless of the tmux base-index and
        pane-base-index configuration.

        Returns {role: pane_index} or {} if the window has no panes.
        """
        roles = {}
        panes = self.tmux_command(
            "list-panes", f"-t={self.session_name}:{window_name}",
            "-F", "#{pane_index} #{pane_top}"
        )
        if not panes:
            return roles

        ordered = sorted(
            (line.split() for line in panes.splitlines() if line.strip()),
            key=lambda parts: int(parts[1])
        )
        for role, parts in zip(("pi", "pty", "comms"), ordered):
            roles[role] = int(parts[0])
        return roles

    def _create_pane_layout(self, session: str, window_name: str, window_index: int):
        """Create a 3-pane layout (top, middle, bottom) in a tmux window.

        Returns {role: pane_index} for the new window's panes.
        """
        target_window = f"{session}:{window_name}"

        # Start with one pane, then split into three
        # Top pane: Pi Agent
        self.tmux_command("split-window", f"-t={target_window}", "-v", "-p", "33")
        # Bottom pane: COMMS
        self.tmux_command("split-window", f"-t={target_window}", "-v", "-p", "50")

        roles = self._pane_roles(window_name)

        # Label panes
        if "pi" in roles:
            self.tmux_command("select-pane", f"-t={target_window}.{roles['pi']}", "-T", "Pi Agent")
        if "pty" in roles:
            self.tmux_command("select-pane", f"-t={target_window}.{roles['pty']}", "-T", "Agent PTY")
        if "comms" in roles:
            self.tmux_command("select-pane", f"-t={target_window}.{roles['comms']}", "-T", "Agent COMMS")

        return roles

    def create_session(self):
        """Create the initial tmux session with monitor and orchestrator windows."""
        # Ensure directories exist
        self._init_directories()

        # Initialize database
        self.init_db()

        # Create session with monitor window (window 0)
        monitor_script = f"""bash -c '
clear
echo "Agent Monitor - Project: {self.project_name}";
echo "";
# Keep monitoring - show live agent status
while true; do
  clear
  python3 "{self.agents_dir}/agentd.py" monitor 2>/dev/null || true
  echo ""
  sleep 3
done
'
"""

        self.tmux_command("new-session", "-d", "-s", self.session_name, "-n", "monitor", monitor_script)

        # Create orchestrator window (window 1)
        self.tmux_command("new-window", f"-t={self.session_name}", "-n", "orchestrator")

        # Create three panes in orchestrator
        roles = self._create_pane_layout(self.session_name, "orchestrator", 1)

        # Launch Pi in the orchestrator pane (top pane)
        pi_pane = roles.get("pi")
        pty_pane = roles.get("pty")
        comms_pane = roles.get("comms")

        if pi_pane is None:
            print("ERROR: could not determine orchestrator pane indexes", file=sys.stderr)
            return

        self._launch_pi_in_pane(f"{self.session_name}:orchestrator.{pi_pane}",
                                skills=self.skills_dir,
                                session_dir=self.agents_dir / "sessions" / "orchestrator",
                                session_name="orchestrator")

        # Launch PTY viewer and comms viewer in respective panes
        self.tmux_command("send-keys", f"-t={self.session_name}:orchestrator.{pty_pane}",
                         f"echo 'Orchestrator PTY Viewer - logs at {self.pty_logs_dir}/orchestrator.log'; tail -f {self.pty_logs_dir}/orchestrator.log 2>/dev/null || sleep infinity", "Enter")
        self.tmux_command("send-keys", f"-t={self.session_name}:orchestrator.{comms_pane}",
                         f"echo 'Orchestrator COMMS Viewer'; tail -f {self.comms_dir}/orchestrator.jsonl 2>/dev/null || echo 'No messages yet'; while true; do sleep 1; tail -n 20 {self.comms_dir}/orchestrator.jsonl 2>/dev/null; sleep 5; done", "Enter")

        # Set up PTY output capture for orchestrator pane
        self.tmux_command("pipe-pane", f"-t={self.session_name}:orchestrator.{pi_pane}",
                         f"cat >> {self.pty_logs_dir}/orchestrator.log 2>&1")

        # Select monitor window for user
        self.tmux_command("select-window", f"-t={self.session_name}:monitor")

        # Log session creation
        self.log_agent_event("session", "session_created", {
            "session_name": self.session_name,
            "project_dir": str(self.project_dir),
            "window_count": 2
        })

        print(f"Created session: {self.session_name}")
        monitor_index = self._window_index("monitor")
        orchestrator_index = self._window_index("orchestrator")
        print(f"  - Window {monitor_index}: monitor")
        print(f"  - Window {orchestrator_index}: orchestrator (3 panes)")
        print(f"  - Database: {self.db_path}")
        print(f"  - Comms dir: {self.comms_dir}")
        print(f"  - PTY logs: {self.pty_logs_dir}")

    def _window_index(self, window_name: str):
        """Return the tmux window index for a named window, or its name if unknown."""
        lines = self.tmux_command("list-windows", f"-t={self.session_name}", "-F", "#{window_index} #{window_name}")
        for line in lines.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == window_name:
                return parts[0]
        return window_name

    def _launch_pi_in_pane(self, target: str, skills: Path = None,
                           session_dir: Path = None, session_name: str = None):
        """Launch a Pi agent in the specified tmux pane."""
        # Build the command
        cmd_parts = ["pi"]

        if skills:
            cmd_parts += ["--skill", str(skills)]

        if session_dir:
            session_dir.mkdir(parents=True, exist_ok=True)
            cmd_parts += ["--session-dir", str(session_dir)]

        if session_name:
            cmd_parts += ["--name", session_name]

        # Join command parts safely for tmux send-keys
        cmd_str = " ".join(cmd_parts)

        self.tmux_command("send-keys", f"-t={target}", cmd_str, "Enter")

        return cmd_str

    def _start_comms_monitor(self, agent_name: str):
        """Start a background thread to monitor JSONL communication files."""
        comms_file = self.comms_dir / f"{agent_name}.jsonl"

        def monitor():
            """Monitor JSONL file and process messages."""
            try:
                if not comms_file.exists():
                    comms_file.touch()
            except OSError as e:
                print(f"Comms setup error: {e}", file=sys.stderr)

            size = 0
            while True:
                try:
                    if comms_file.stat().st_size > size:
                        with open(comms_file, 'r') as f:
                            f.seek(size)
                            for line in f:
                                line = line.strip()
                                if line:
                                    try:
                                        msg = json.loads(line)
                                        from_agent = msg.get("from", "unknown")
                                        to_agent = msg.get("to", agent_name)
                                        msg_type = msg.get("type", "unknown")

                                        self.log_message(from_agent, to_agent, msg_type, msg)
                                        self._apply_message_effects(agent_name, msg_type, msg)

                                        # Structured event for the audit trail.
                                        self.log_agent_event(agent_name, "agent.message", {
                                            "type": msg_type,
                                            "from": from_agent,
                                            "to": to_agent,
                                            **msg
                                        })

                                        print(f"Comms: {msg}", file=sys.stderr)
                                    except json.JSONDecodeError:
                                        pass
                            size = comms_file.stat().st_size
                    time.sleep(0.5)
                except Exception as e:
                    print(f"Comms monitor error: {e}", file=sys.stderr)
                    time.sleep(1)

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        return thread

    def _apply_message_effects(self, agent_id: str, msg_type: str, msg: dict):
        """Update the registry from structured agent messages.

        A message containing a "status" field drives the agent's lifecycle
        status (working, testing, blocked, finished, ...) in the registry.
        """
        status = msg.get("status")
        if not status:
            return

        conn = self._db()
        before = conn.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
        conn.execute(
            "UPDATE agents SET status=?, last_event=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, msg.get("type", msg_type), agent_id)
        )
        conn.commit()
        conn.close()

        if before and before["status"] != status:
            # Map lifecycle statuses to structured events.
            if status == "blocked":
                event = "agent.blocked"
            elif status in ("finished", "complete", "done"):
                event = "agent.finished"
            elif status == "failed":
                event = "agent.failed"
            else:
                event = "agent.status_changed"
            self.log_agent_event(agent_id, event, {"status": status})
            print(f"agent {agent_id} -> {status} (via comms)", file=sys.stderr)

    def _next_window_index(self):
        """Return the next free window index for a new agent window.

        Windows are discovered dynamically instead of assuming a fixed
        base-index, so this works regardless of the tmux base-index setting.
        """
        out = self.tmux_command("list-windows", f"-t={self.session_name}", "-F", "#{window_index}")
        if not out:
            return max(self.next_window_index, 1)
        indices = [int(line) for line in out.splitlines() if line.strip().isdigit()]
        return (max(indices) + 1) if indices else max(self.next_window_index, 1)

    def spawn_agent(self, name: str, task: str, parent: str = "orchestrator"):
        """Spawn a new sub-agent with a specific task."""
        agent_id = f"agent-{int(time.time())}-{uuid4().hex[:6]}"
        window_index = self._next_window_index()
        window_name = name.replace(" ", "-").lower()

        # Create tmux window for this agent
        self.tmux_command("new-window", f"-t={self.session_name}", "-d", "-n", window_name)

        # Create three panes (Pi, PTY, COMMS)
        target_window = f"{self.session_name}:{window_name}"
        roles = self._create_pane_layout(self.session_name, window_name, window_index)

        pi_pane = roles.get("pi")
        pty_pane = roles.get("pty")
        comms_pane = roles.get("comms")

        if pi_pane is None:
            print(f"ERROR: could not determine pane indexes for {window_name}", file=sys.stderr)
            return None

        # Launch Pi in the agent's pane (top pane)
        self._launch_pi_in_pane(f"{target_window}.{pi_pane}",
                                skills=self.skills_dir,
                                session_dir=self.agents_dir / "sessions" / agent_id,
                                session_name=agent_id)

        # Create branch for this agent
        branch_name = f"{agent_id}-{name.replace(' ', '-')}"
        self.tmux_command("send-keys", f"-t={target_window}.{pi_pane}",
                         f"cd {self.project_dir} && git checkout -b {branch_name}", "Enter")

        # Send initial task notification
        self._send_message_to_agent(agent_id, "orchestrator", "task", {
            "task": task,
            "agent_id": agent_id,
            "branch": branch_name,
            "cwd": str(self.project_dir)
        })

        # Set up PTY output capture
        self.tmux_command("pipe-pane", f"-t={target_window}.{pi_pane}",
                         f"python3 {self.agents_dir}/agentd.py ptylog {agent_id} {window_index} 0 >> /dev/null 2>&1")

        # Set up comms viewer in bottom pane
        comms_file = self.comms_dir / f"{agent_id}.jsonl"
        self.tmux_command("send-keys", f"-t={target_window}.{comms_pane}",
                         f"echo 'Agent COMMS ({name})'; tail -f {comms_file} 2>/dev/null || echo 'No messages'; while true; do sleep 1; done", "Enter")

        # Set up PTY log viewer in middle pane
        pty_log_file = self.pty_logs_dir / f"{agent_id}.log"
        self.tmux_command("send-keys", f"-t={target_window}.{pty_pane}",
                         f"echo 'Agent PTY Log ({name})'; tail -f {pty_log_file} 2>/dev/null || echo 'No PTY output yet'; while true; do sleep 1; done", "Enter")

        # Capture the Pi pane's PID for the daemon's termination detection.
        pane_pid = self.tmux_command("display-message", f"-t={target_window}.{pi_pane}", "-p", "#{pane_pid}")
        pid = int(pane_pid) if pane_pid and pane_pid.lstrip("-").isdigit() else None

        # Register agent in the registry (single source of truth).
        conn = self._db()
        conn.execute(
            "INSERT INTO agents (id, name, parent_id, status, cwd, window_index, window_name, pid, branch, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (agent_id, name, parent, "running", str(self.project_dir), window_index, window_name, pid, branch_name)
        )
        conn.commit()
        conn.close()

        # Store agent info
        self.agents[agent_id] = {
            "id": agent_id,
            "name": name,
            "task": task,
            "window_index": window_index,
            "window_name": window_name,
            "status": "running",
            "parent": parent,
            "branch": branch_name,
            "pid": pid,
            "created_at": datetime.now().isoformat()
        }

        # Log to database - structured lifecycle events
        self.log_agent_event(agent_id, "agent.created", {
            "name": name,
            "task": task,
            "window_index": window_index,
            "window_name": window_name,
            "parent": parent,
            "branch": branch_name,
            "pid": pid,
            "cwd": str(self.project_dir)
        })

        # Create task record
        conn = self._db()
        conn.execute(
            "INSERT INTO tasks (agent_id, task, status) VALUES (?, ?, ?)",
            (agent_id, task, "assigned")
        )
        conn.commit()
        conn.close()

        self.log_agent_event(agent_id, "agent.started", {
            "window_index": window_index,
            "window_name": window_name,
            "pid": pid
        })

        # Switch to the agent window
        self.tmux_command("select-window", f"-t={self.session_name}:{window_name}")

        print(f"Spawned agent '{name}' (ID: {agent_id})")
        print(f"  - Window: {window_name} (index {window_index})")
        print(f"  - Task: {task}")
        print(f"  - Branch: {branch_name}")
        print(f"  - Comms: {comms_file}")
        print(f"  - PTY log: {pty_log_file}")

        return agent_id

    def _send_message_to_agent(self, agent_id: str, from_agent: str, msg_type: str, payload: dict):
        """Send a message to an agent via its JSONL comms file."""
        comms_file = self.comms_dir / f"{agent_id}.jsonl"

        message = {
            "type": msg_type,
            "from": from_agent,
            "to": agent_id,
            "timestamp": datetime.now().isoformat(),
            **payload
        }

        comms_file.parent.mkdir(parents=True, exist_ok=True)
        with open(comms_file, 'a') as f:
            f.write(json.dumps(message) + '\n')

        self.log_message(from_agent, agent_id, msg_type, message)

    def ptylog(self, agent_id: str, window_index: int, pane_index: int):
        """
        PTY log handler - called by tmux pipe-pane.
        Reads stdin (pipe-pane output) and logs to SQLite.
        """
        import io

        while True:
            try:
                data = os.read(0, 4096)
                if not data:
                    break

                timestamp = datetime.now().isoformat()
                content = data.decode('utf-8', errors='replace')

                # Log to SQLite
                self.log_pty_event(agent_id, int(window_index), int(pane_index),
                                   "output", content)

                # Also write to the log file
                pty_log_file = self.pty_logs_dir / f"{agent_id}.log"
                pty_log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(pty_log_file, 'a') as f:
                    f.write(content)

            except OSError:
                break

    def status(self):
        """Show status of all agents."""
        conn = self._db()

        cursor = conn.execute("""
            SELECT id, name, status, cwd, window_index, created_at
            FROM agents
            ORDER BY created_at
        """)
        agents = cursor.fetchall()

        if not agents:
            print("No agents registered yet.")
            print("Use: agentd.py spawn <name> <task>")
        else:
            print(f"{'ID':<16} {'Name':<20} {'Status':<10} {'Window':<8} {'Created':<20} Task")
            print("-" * 100)
            for agent in agents:
                task_result = conn.execute(
                    "SELECT task FROM tasks WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
                    (agent['id'],)
                ).fetchone()
                task = task_result['task'] if task_result else "none"
                print(f"{agent['id']:<16} {agent['name']:<20} {agent['status']:<10} {str(agent['window_index'] or '-'):<8} {agent['created_at']:<20} {task}")

        conn.close()

    STATUS_MARKS = {
        "created": "○",
        "starting": "◐",
        "running": "●",
        "working": "●",
        "idle": "○",
        "waiting": "○",
        "blocked": "⚠",
        "testing": "◐",
        "review": "◆",
        "finished": "✓",
        "failed": "✗",
        "stopped": "■",
        "unknown": "?",
    }

    def monitor(self):
        """Render the agent monitor panel (window 0).

        Shows one compact box per agent with name, status, task, the last
        structured event, and the window index. This is the human's primary
        observation window; it must never affect the agents themselves.
        """
        conn = self._db()
        cursor = conn.execute("""
            SELECT id, name, status, window_index, window_name,
                   branch, pid, created_at, updated_at
            FROM agents
            ORDER BY window_index, created_at
        """)
        agents = cursor.fetchall()

        rows = []
        for agent in agents:
            task_row = conn.execute(
                "SELECT task FROM tasks WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
                (agent['id'],)
            ).fetchone()
            task = task_row['task'] if task_row else "none"

            event_row = conn.execute(
                "SELECT event_type, created_at FROM agent_events "
                "WHERE agent_id = ? ORDER BY id DESC LIMIT 1",
                (agent['id'],)
            ).fetchone()
            last_event = event_row['event_type'] if event_row else "none"
            last_at = event_row['created_at'] if event_row else ""

            status = agent['status']
            mark = self.STATUS_MARKS.get(status, self.STATUS_MARKS["unknown"])
            rows.append((agent, mark, task, last_event, last_at))

        conn.close()

        width = 74
        print(f"AGENTS  |  {self.project_name}")
        print("─" * width)
        if not rows:
            print("No agents registered yet.")
            print("  Spawn one: agentd.py spawn <name> <task>")
        else:
            for agent, mark, task, last_event, last_at in rows:
                window = agent['window_index'] if agent['window_index'] is not None else "-"
                branch = agent['branch'] or "-"
                pid = agent['pid'] or "-"
                print(f"{mark} {agent['name']:<18} [{agent['status']}] w{window}  {task}")
                print(f"    last: {last_event} {last_at}  branch: {branch}  pid: {pid}")
        print("─" * width)
        active = sum(1 for a, *_ in rows if a['status'] not in ("finished", "failed", "stopped"))
        print(f"{len(rows)} agent(s) | {active} active")
        print("jump: Ctrl+B then window number | detach: Ctrl+B D")

    def send_message(self, agent_id: str):
        """Send a message to an agent (reads from stdin)."""
        conn = self._db()
        row = conn.execute("SELECT id FROM agents WHERE id=?", (agent_id,)).fetchone()
        conn.close()

        if not row:
            print(f"Unknown agent: {agent_id}", file=sys.stderr)
            sys.exit(1)

        message = sys.stdin.read().strip()
        if not message:
            print("No message provided on stdin", file=sys.stderr)
            sys.exit(1)

        self._send_message_to_agent(agent_id, "orchestrator", "user_message", {
            "message": message
        })

        print(f"Message sent to {agent_id}")

    def stop_agent(self, agent_id: str):
        """Stop a specific agent."""
        conn = self._db()
        row = conn.execute("SELECT window_name FROM agents WHERE id=?", (agent_id,)).fetchone()

        # Mark stopped first so the daemon does not race us to "finished".
        conn.execute(
            "UPDATE agents SET status = 'stopped', ended_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (agent_id,)
        )
        conn.commit()
        conn.close()

        if row and row["window_name"]:
            self.tmux_command("kill-window", f"-t={self.session_name}:{row['window_name']}")

        self.agents.pop(agent_id, None)
        self.log_agent_event(agent_id, "agent.exited", {"reason": "stopped", "status": "stopped"})

        print(f"Stopped agent: {agent_id}")

    def kill_all(self):
        """Kill all active agents but keep the session."""
        conn = self._db()
        rows = conn.execute(
            "SELECT id FROM agents WHERE status NOT IN ('stopped', 'finished', 'failed')"
        ).fetchall()
        conn.close()

        for row in rows:
            self.stop_agent(row["id"])

        print("All agents stopped.")

    def list_sessions(self):
        """List tmux sessions."""
        sessions = self.tmux_command("list-sessions", "-F", "#{session_name}")
        if sessions:
            for session in sessions.split("\n"):
                print(f"  {session}")
        else:
            print("No tmux sessions found.")

    # ------------------------------------------------------------------
    # Daemon (serve) - the long-lived runtime supervisor
    # ------------------------------------------------------------------

    def _session_alive(self) -> bool:
        """Return True if the managed tmux session still exists."""
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", self.session_name],
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    def _pane_current_command(self, target: str):
        """Return the current command running in a tmux pane, or None."""
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return None
            return result.stdout.strip() or None
        except Exception:
            return None

    def _active_agents(self):
        """Return agents that are still being supervised.

        Includes every non-terminal status (running, working, waiting,
        blocked, testing, review, created...). Only stopped/finished/failed
        agents are excluded, so the daemon keeps watching agents whose comms
        messages have advanced their status.
        """
        conn = self._db()
        rows = conn.execute(
            "SELECT * FROM agents WHERE status NOT IN ('stopped', 'finished', 'failed')"
        ).fetchall()
        conn.close()
        return rows

    def _check_agent(self, row, seen_commands: dict):
        """Detect when a running agent's process/pane has ended and update the registry."""
        window_name = row["window_name"]
        if not window_name or not row["started_at"]:
            return

        target = None
        roles = self._pane_roles(window_name)
        if roles:
            pi_pane = roles.get("pi")
            if pi_pane is not None:
                target = f"{self.session_name}:{window_name}.{pi_pane}"
        cmd = self._pane_current_command(target) if target else None
        prev = seen_commands.get(row["id"])

        if cmd is None:
            # Pane/window is gone. Only conclude "finished" after we have seen
            # the pane at least once, so we don't kill brand-new agents.
            if prev is not None:
                self._mark_status(row["id"], "finished", ended=True, event="agent.finished")
                seen_commands.pop(row["id"], None)
            return

        seen_commands[row["id"]] = cmd

        # The pane started a Pi process and that process has now exited
        # (the shell is back at the prompt).
        if prev == "pi" and cmd != "pi":
            self._mark_status(row["id"], "finished", ended=True, event="agent.finished")
            seen_commands.pop(row["id"], None)

    def serve(self):
        """Run the long-lived supervisor daemon.

        Owns the comms monitors, detects agent termination, cleans up dead
        agents, and keeps the registry updated. Exits when the tmux session
        that owns this environment is gone.
        """
        print(f"agentd daemon started (pid={os.getpid()}) for session {self.session_name}")
        sys.stdout.flush()

        self._init_directories()
        self.init_db()

        monitors = {}  # agent_id -> comms monitor thread
        seen_commands = {}  # agent_id -> last seen pane_current_command

        while True:
            try:
                if not self._session_alive():
                    print("tmux session no longer exists; daemon exiting", file=sys.stderr)
                    sys.stderr.flush()
                    return

                agents = self._active_agents()
                active_ids = set()

                for row in agents:
                    agent_id = row["id"]
                    active_ids.add(agent_id)

                    # Ensure a persistent comms monitor exists for this agent.
                    monitor = monitors.get(agent_id)
                    if monitor is None or not monitor.is_alive():
                        monitors[agent_id] = self._start_comms_monitor(agent_id)

                    self._check_agent(row, seen_commands)

                # Drop monitors/state for agents that are no longer running.
                for agent_id in list(monitors):
                    if agent_id not in active_ids:
                        monitors.pop(agent_id, None)
                        seen_commands.pop(agent_id, None)

            except Exception as e:
                print(f"daemon error: {e}", file=sys.stderr)
                sys.stderr.flush()

            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description="Agent orchestration runtime supervisor")
    parser.add_argument("command", choices=["init", "create-session", "serve", "spawn", "send",
                                             "status", "monitor", "stop", "kill-all", "sessions", "ptylog"])
    parser.add_argument("name", nargs="?", default=None)
    parser.add_argument("task", nargs="*", default=None)
    parser.add_argument("--agent-id", dest="agent_id", default=None)
    parser.add_argument("--window-index", dest="window_index", default=None)
    parser.add_argument("--pane-index", dest="pane_index", default=None)

    args = parser.parse_args()

    project_dir = os.environ.get("PROJECT_DIR", os.getcwd())
    orchestrator = AgentOrchestrator(project_dir)

    if args.command == "init":
        orchestrator._init_directories()
        orchestrator.init_db()
        print("Initialized agent orchestration environment")

    elif args.command == "create-session":
        orchestrator.create_session()

    elif args.command == "serve":
        orchestrator.serve()

    elif args.command == "spawn":
        if not args.name or not args.task:
            print("Usage: agentd.py spawn <agent-name> <task-description>")
            sys.exit(1)
        agent_id = orchestrator.spawn_agent(args.name, " ".join(args.task))

    elif args.command == "send":
        if not args.name:
            print("Usage: agentd.py send <agent-id>")
            print("  (reads message from stdin)")
            sys.exit(1)
        orchestrator.send_message(args.name)

    elif args.command == "status":
        orchestrator.status()

    elif args.command == "monitor":
        orchestrator.monitor()

    elif args.command == "stop":
        if not args.name:
            print("Usage: agentd.py stop <agent-id>")
            sys.exit(1)
        orchestrator.stop_agent(args.name)

    elif args.command == "kill-all":
        orchestrator.kill_all()

    elif args.command == "sessions":
        orchestrator.list_sessions()

    elif args.command == "ptylog":
        # Specialized mode for tmux pipe-pane
        if not args.agent_id or not args.window_index or not args.pane_index:
            print("Usage: agentd.py ptylog --agent-id <id> --window-index <n> --pane-index <n>")
            sys.exit(1)
        orchestrator.ptylog(args.agent_id, args.window_index, args.pane_index)


if __name__ == "__main__":
    main()