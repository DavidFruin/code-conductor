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
    spawn <name> <task>  Spawn a new sub-agent with a task
    status            Show status of all agents
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
import signal
import pty
import select
import atexit
import argparse
from pathlib import Path
from datetime import datetime


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
        
        self.agents = {}  # agent_id -> agent info
        self.processes = {}  # agent_id -> process info (pid, threads, etc.)
        self.next_window_index = 2  # Window 0 = monitor, 1 = orchestrator
        self.project_name = self.project_dir.name

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
        
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript(schema)
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
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO agent_events (agent_id, event_type, payload) VALUES (?, ?, ?)",
            (agent_id, event_type, json.dumps(payload) if payload else None)
        )
        conn.commit()
        conn.close()
    
    def log_message(self, from_agent: str, to_agent: str, msg_type: str, payload: dict):
        """Log a communication message."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO messages (from_agent, to_agent, type, payload) VALUES (?, ?, ?, ?)",
            (from_agent, to_agent, msg_type, json.dumps(payload))
        )
        conn.commit()
        conn.close()
    
    def log_pty_event(self, agent_id: str, window_index: int, pane_index: int, 
                      input_output: str, content: str):
        """Log PTY output event."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO pty_events (agent_id, window_index, pane_index, input_output, content) VALUES (?, ?, ?, ?, ?)",
            (agent_id, window_index, pane_index, input_output, content)
        )
        conn.commit()
        conn.close()

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

    def _create_pane_layout(self, session: str, window_name: str, window_index: int):
        """Create a 3-pane layout (top, middle, bottom) in a tmux window."""
        target_window = f"{session}:{window_name}"
        
        # Start with one pane, then split into three
        # Pane 0 (top): Pi Agent
        self.tmux_command("split-window", f"-t={target_window}", "-v", "-p", "33")
        # Pane 2 (bottom): COMMS
        self.tmux_command("split-window", f"-t={target_window}", "-v", "-p", "50")
        
        # Label panes
        self.tmux_command("select-pane", f"-t={target_window}.0", "-T", "Pi Agent")
        self.tmux_command("select-pane", f"-t={target_window}.1", "-T", "Agent PTY")
        self.tmux_command("select-pane", f"-t={target_window}.2", "-T", "Agent COMMS")
        
        return target_window

    def create_session(self):
        """Create the initial tmux session with monitor and orchestrator windows."""
        # Ensure directories exist
        self._init_directories()
        
        # Initialize database
        self.init_db()
        
        # Create session with monitor window (window 0)
        monitor_script = f"""bash -c '
echo "Agent Monitor - Project: {self.project_name}";
echo "No agents running yet.";
echo "";
echo "Available commands:";
echo "  agentd.py spawn <name> <task>   - Spawn a sub-agent";
echo "  agentd.py status                - Show agent status";
echo "";
echo "Waiting for agents to be spawned...";
echo "";
# Keep monitoring - show live agent status
while true; do
  sleep 5
  echo ""
  echo "=== $(date) ==="
  python3 "{self.agents_dir}/agentd.py" status 2>/dev/null || true
  echo ""
done
'
"""
        
        self.tmux_command("new-session", "-d", "-s", self.session_name, "-n", "monitor", monitor_script)
        
        # Create orchestrator window (window 1)
        self.tmux_command("new-window", f"-t={self.session_name}", "-n", "orchestrator")
        
        # Create three panes in orchestrator
        self._create_pane_layout(self.session_name, "orchestrator", 1)
        
        # Launch Pi in the orchestrator pane (pane 0)
        self._launch_pi_in_pane(f"{self.session_name}:orchestrator.0", 
                                skills=self.skills_dir,
                                session_dir=self.agents_dir / "sessions" / "orchestrator",
                                session_name="orchestrator")
        
        # Launch PTY viewer and comms viewer in respective panes
        self.tmux_command("send-keys", f"-t={self.session_name}:orchestrator.1",
                         f"echo 'Orchestrator PTY Viewer - logs at {self.pty_logs_dir}/orchestrator.log'; tail -f {self.pty_logs_dir}/orchestrator.log 2>/dev/null || sleep infinity", "Enter")
        self.tmux_command("send-keys", f"-t={self.session_name}:orchestrator.2",
                         f"echo 'Orchestrator COMMS Viewer'; tail -f {self.comms_dir}/orchestrator.jsonl 2>/dev/null || echo 'No messages yet'; while true; do sleep 1; tail -n 20 {self.comms_dir}/orchestrator.jsonl 2>/dev/null; sleep 5; done", "Enter")
        
        # Set up PTY output capture for orchestrator pane
        self.tmux_command("pipe-pane", f"-t={self.session_name}:orchestrator.0",
                         f"cat >> {self.pty_logs_dir}/orchestrator.log 2>&1")
        
        # Select monitor window for user
        self.tmux_command("select-window", f"-t={self.session_name}:monitor")
        
        # Log session creation
        self.log_agent_event("session", "session_created", {
            "session_name": self.session_name,
            "project_dir": str(self.project_dir),
            "window_count": 2
        })
        
        # Start the orchestrator's JSONL monitor
        self._start_comms_monitor("orchestrator")
        
        print(f"Created session: {self.session_name}")
        print(f"  - Window 0: monitor")
        print(f"  - Window 1: orchestrator (3 panes)")
        print(f"  - Database: {self.db_path}")
        print(f"  - Comms dir: {self.comms_dir}")
        print(f"  - PTY logs: {self.pty_logs_dir}")

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
            if not comms_file.exists():
                comms_file.touch()
            
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
                                        
                                        # If this is for us, log it
                                        if to_agent == agent_name:
                                            self.log_agent_event(agent_name, "message_received", msg)
                                        
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

    def spawn_agent(self, name: str, task: str, parent: str = "orchestrator"):
        """Spawn a new sub-agent with a specific task."""
        agent_id = f"agent-{int(time.time())}"
        window_index = self.next_window_index
        window_name = name.replace(" ", "-").lower()
        
        # Ensure window index doesn't conflict
        while self.tmux_command("list-windows", f"-t={self.session_name}", "-F", "#{window_index}") \
                and int(self.tmux_command("list-windows", f"-t={self.session_name}", "-F", "#{window_index}").split('\n')[-1]) >= window_index:
            window_index += 1
        
        # Create tmux window for this agent
        self.tmux_command("new-window", f"-t={self.session_name}", "-d", "-n", window_name)
        
        # Create three panes (Pi, PTY, COMMS)
        target_window = f"{self.session_name}:{window_name}"
        self._create_pane_layout(self.session_name, window_name, window_index)
        
        # Launch Pi in the agent's pane (pane 0)
        self._launch_pi_in_pane(f"{target_window}.0",
                                skills=self.skills_dir,
                                session_dir=self.agents_dir / "sessions" / agent_id,
                                session_name=agent_id)
        
        # Create branch for this agent
        branch_name = f"{agent_id}-{name.replace(' ', '-')}"
        self.tmux_command("send-keys", f"-t={target_window}.0", 
                         f"cd {self.project_dir} && git checkout -b {branch_name}", "Enter")
        
        # Send initial task notification
        self._send_message_to_agent(agent_id, "orchestrator", "task", {
            "task": task,
            "agent_id": agent_id,
            "branch": branch_name,
            "cwd": str(self.project_dir)
        })
        
        # Set up PTY output capture
        self.tmux_command("pipe-pane", f"-t={target_window}.0",
                         f"python3 {self.agents_dir}/agentd.py ptylog {agent_id} {window_index} 0 >> /dev/null 2>&1")
        
        # Set up comms viewer in pane 2
        comms_file = self.comms_dir / f"{agent_id}.jsonl"
        self.tmux_command("send-keys", f"-t={target_window}.2",
                         f"echo 'Agent COMMS ({name})'; tail -f {comms_file} 2>/dev/null || echo 'No messages'; while true; do sleep 1; done", "Enter")
        
        # Set up PTY log viewer in pane 1
        pty_log_file = self.pty_logs_dir / f"{agent_id}.log"
        self.tmux_command("send-keys", f"-t={target_window}.1",
                         f"echo 'Agent PTY Log ({name})'; tail -f {pty_log_file} 2>/dev/null || echo 'No PTY output yet'; while true; do sleep 1; done", "Enter")
        
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
            "created_at": datetime.now().isoformat()
        }
        
        # Log to database
        self.log_agent_event(agent_id, "spawned", {
            "name": name,
            "task": task,
            "window_index": window_index,
            "window_name": window_name,
            "parent": parent,
            "branch": branch_name,
            "cwd": str(self.project_dir)
        })
        
        # Create task record
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO tasks (agent_id, task, status) VALUES (?, ?, ?)",
            (agent_id, task, "assigned")
        )
        conn.commit()
        conn.close()
        
        # Start comms monitor for this agent
        self._start_comms_monitor(agent_id)
        
        self.next_window_index += 1
        
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
                with open(pty_log_file, 'a') as f:
                    f.write(content)
                    
            except OSError:
                break

    def status(self):
        """Show status of all agents."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        
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

    def send_message(self, agent_id: str):
        """Send a message to an agent (reads from stdin)."""
        if agent_id not in self.agents:
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
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.execute("SELECT window_index, window_name FROM agents WHERE id = ?", (agent_id,))
        agent = cursor.fetchone()
        conn.close()
        
        if not agent:
            print(f"Unknown agent: {agent_id}")
            return
        
        if agent['window_name']:
            self.tmux_command("kill-window", f"-t={self.session_name}:{agent['window_name']}")
        
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("UPDATE agents SET status = 'stopped', ended_at = CURRENT_TIMESTAMP WHERE id = ?", (agent_id,))
        conn.commit()
        conn.close()
        
        self.log_agent_event(agent_id, "stopped", {})
        
        print(f"Stopped agent: {agent_id}")

    def kill_all(self):
        """Kill all agents but keep the session."""
        for agent_id in list(self.agents.keys()):
            self.stop_agent(agent_id)
        
        # Also check database for stopped agents
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.execute("SELECT id FROM agents WHERE status = 'running'")
        for row in cursor.fetchall():
            self.tmux_command("kill-window", f"-t={self.session_name}:agent-*")
        conn.close()
        
        print("All agents stopped.")

    def list_sessions(self):
        """List tmux sessions."""
        sessions = self.tmux_command("list-sessions", "-F", "#{session_name}")
        if sessions:
            for session in sessions.split("\n"):
                print(f"  {session}")
        else:
            print("No tmux sessions found.")


def main():
    parser = argparse.ArgumentParser(description="Agent orchestration runtime supervisor")
    parser.add_argument("command", choices=["init", "create-session", "spawn", "send", 
                                             "status", "stop", "kill-all", "sessions", "ptylog"])
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
