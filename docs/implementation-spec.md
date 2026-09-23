# Project-Local Unix Agent Orchestration System

## 1. Objective

Build a Unix-native, project-local agent orchestration environment using **ST terminal, tmux, Pi coding agents, Linux PTYs, JSONL communication, Python, Bash, SQLite, Git, and GitHub CLI (`gh`)**.

The system must avoid building a custom TUI. **tmux is the entire visual interface.** The system should feel like a collection of ordinary Unix processes connected together with pipes/sockets and displayed through tmux windows and panes.

The user launches the environment from inside a project directory such as:

```text
/home/dave/dev/new-project/
```

The project's agent history and orchestration state are stored locally inside that project.

---

# 2. Core Architecture

The system consists of three custom artifacts:

```text
start-agents.sh
.agents/agentd.py
.agents/skills/spawn-agent/SKILL.md
```

Existing dependencies:

```text
ST terminal
tmux
Pi coding agent
Python
Git
GitHub CLI (gh)
SQLite
Linux PTY facilities
```

The three custom artifacts have distinct responsibilities.

```text
start-agents.sh
        │
        ▼
      tmux
        │
        ▼
    agentd.py
        │
        ├── Pi processes
        ├── PTYs
        ├── JSONL communication
        ├── SQLite
        └── tmux windows/panes
```

The `SKILL.md` teaches the orchestrator Pi how to use the orchestration system.

---

# 3. Project Isolation

The system is always launched from a project directory.

Example:

```text
/home/dave/dev/new-project/
```

The project contains:

```text
new-project/
├── .git/
├── .agents/
│   ├── agentd.py
│   ├── agents.sqlite
│   └── skills/
│       └── spawn-agent/
│           └── SKILL.md
├── src/
└── ...
```

Every project has its own SQLite database.

Therefore:

```text
project A → project A/.agents/agents.sqlite
project B → project B/.agents/agents.sqlite
```

Agent history, messages, PTY events, tasks, agent relationships, and orchestration state must never be mixed between projects.

The project directory is also the default working directory for the orchestrator and its agents unless a task explicitly specifies another directory.

---

# 4. tmux Architecture

The launcher creates one tmux session for the project.

The initial windows are:

```text
window 0: monitor
window 1: orchestrator
```

No subagent windows are created initially.

The user normally remains in **window 0**.

## Window 0 — Monitor

This is the human's primary observation window.

It is not an agent.

It contains ordinary terminal processes that display information about agents and their JSONL communications.

Example:

```text
┌──────────────────────┬──────────────────────┐
│ agent-01             │ agent-02             │
│ ● working            │ ● working            │
│ ORCH → investigate   │ ORCH → write tests   │
│ AG → reading files   │ AG → running tests   │
├──────────────────────┼──────────────────────┤
│ agent-03             │ agent-04             │
│ ✓ complete           │ ● waiting            │
│ AG → PR ready        │ AG → awaiting task   │
└──────────────────────┴──────────────────────┘
```

The monitor is implemented with ordinary shell processes, log readers, or small `agentd.py` commands. It must not become a custom TUI.

## Window 1 — Orchestrator

The parent Pi agent lives here.

It contains three panes:

```text
┌─────────────────────────────┐
│ Pi Orchestrator              │
├─────────────────────────────┤
│ Orchestrator PTY             │
├─────────────────────────────┤
│ Orchestrator COMMS           │
└─────────────────────────────┘
```

The user can directly communicate with the orchestrator through the Pi pane.

The orchestrator decides when work should be delegated.

## Windows 2+ — Subagents

Each subagent receives its own tmux window dynamically.

Each contains:

```text
┌─────────────────────────────┐
│ Pi Agent                     │
├─────────────────────────────┤
│ Agent PTY                    │
├─────────────────────────────┤
│ Orchestrator ↔ Agent COMMS   │
└─────────────────────────────┘
```

The user can switch to any agent window and directly interact with its Pi process.

---

# 5. PTY Architecture

A PTY is **not a separate application that must be installed**.

Linux provides pseudo-terminal functionality, and `agentd.py` can create/manage PTYs using Python's OS facilities or an appropriate Python PTY library.

The PTY represents the terminal environment used by an agent.

The PTY serves two purposes:

1. It allows the agent to interact with a real terminal.
2. It allows `agentd.py` to observe and record terminal activity.

The PTY output can be displayed in a tmux pane while simultaneously being recorded.

Conceptually:

```text
Pi
 │
 ▼
PTY
 ├──→ visible terminal pane
 └──→ agentd → SQLite
```

PTY history and JSONL history are separate types of information.

---

# 6. JSONL Communication

Agent-to-agent and orchestrator-to-agent communication uses JSONL.

JSONL is the message format; the underlying transport may initially be pipes and can later use Unix domain sockets if bidirectional communication becomes preferable.

Each message is one JSON object followed by a newline.

Example:

```json
{"type":"task","from":"orchestrator","to":"agent-01","task":"Investigate authentication"}
{"type":"status","from":"agent-01","status":"working"}
{"type":"request","from":"agent-01","request":"Need clarification"}
{"type":"result","from":"agent-01","result":"Found authentication bug"}
```

The communication stream must be machine-readable but displayed to humans in a concise chat-like format.

Example:

```text
10:42:01 ORCH  → Investigate authentication
10:42:07 AGENT → Reading auth.ts
10:42:14 AGENT → Found possible issue
10:42:20 ORCH  → Check middleware
```

The communications pane is only a view of the event stream. It is not the authoritative storage.

---

# 7. SQLite Event Store

SQLite is the persistent source of truth for the project's agent history.

At minimum, the database should eventually represent:

```text
agents
agent_relationships
tasks
messages
pty_events
agent_events
```

Important information includes:

```text
agent ID
parent agent
timestamp
event type
message direction
JSON payload
PTY stream
command/output information
task ID
status
working directory
```

The system must record both:

```text
JSONL communication
```

and:

```text
PTY activity
```

This allows an entire agent session to be reconstructed later.

Example history:

```text
14:34:01 parent → investigate auth
14:34:04 agent → reading auth.ts
14:34:08 PTY → cat auth.ts
14:34:09 PTY → file contents...
14:34:15 agent → found issue
14:34:17 parent → inspect middleware
```

---

# 8. agentd.py Responsibilities

`agentd.py` is the runtime supervisor.

It should handle:

- creating agent processes
- creating and deleting tmux windows
- creating the three panes
- starting Pi
- creating/managing PTYs
- establishing JSONL communication
- routing messages
- monitoring agent processes
- detecting agent termination
- recording events in SQLite
- tracking parent/child relationships
- exposing commands for spawning/stopping/communicating with agents
- cleaning up dead agents
- updating monitor streams

The orchestrator should not need to know how tmux or PTYs are implemented.

It should request an agent at a higher level:

```text
create agent:
name = auth-worker
task = investigate authentication
cwd = /home/dave/dev/new-project
```

`agentd.py` translates that request into the required tmux panes, Pi process, PTY, communication channel, and database records.

---

# 9. Orchestrator Skill

The orchestrator Pi receives a skill file:

```text
.agents/skills/spawn-agent/SKILL.md
```

The skill explains how and when the orchestrator should delegate work.

It should teach concepts such as:

- recognize when a task should be delegated
- create a clear task description
- choose an agent name
- specify working directory
- communicate with the new agent
- monitor progress
- request results
- stop agents when appropriate
- inspect agent work
- review pull requests
- integrate completed work

The skill should describe the **agent management interface**, not internal tmux implementation details.

The orchestrator should think:

```text
"I need another worker."
```

and invoke the agent-management capability.

It should not need to reason about:

```text
tmux split-window
tmux select-pane
PTY file descriptors
```

Those are `agentd.py` responsibilities.

---

# 10. Git and GitHub

Each project remains a normal Git repository.

Agents may use:

```text
git
gh
```

to perform development work.

The orchestrator can delegate work to agents and require agents to create branches and pull requests.

A possible workflow is:

```text
Orchestrator
    ↓
spawn agent
    ↓
agent creates branch
    ↓
agent modifies code
    ↓
agent runs tests
    ↓
agent creates PR with gh
    ↓
agent reports result through JSONL
    ↓
orchestrator reviews PR
    ↓
orchestrator merges/integrates work
```

`agentd.py` should not become a GitHub implementation. Git and `gh` remain agent tools.

---

# 11. Startup Sequence

The user runs:

```bash
cd /home/dave/dev/new-project
./start-agents.sh
```

The launcher:

1. Determines the current project directory.
2. Determines the project name.
3. Creates/attaches the project's tmux session.
4. Initializes `.agents/` if necessary.
5. Initializes the SQLite database if necessary.
6. Starts `agentd.py`.
7. Creates window 0: monitor.
8. Creates window 1: orchestrator.
9. Creates the orchestrator's three panes.
10. Launches Pi in the orchestrator pane.
11. Launches the orchestrator PTY process.
12. Launches the orchestrator communication viewer.
13. Selects the monitor window for the user.

No subagents are automatically created.

---

# 12. Dynamic Agent Creation

The orchestrator creates subagents only when needed.

Example:

```text
User
 ↓
Orchestrator Pi
 ↓
spawn-agent skill
 ↓
agentd.py
 ↓
tmux creates agent-01 window
 ↓
three panes created
 ↓
Pi starts
 ↓
PTY starts
 ↓
JSONL channel starts
 ↓
SQLite records agent
 ↓
monitor window receives status
```

When an agent finishes, `agentd.py` can retain its historical record while removing its active tmux window.

This separates:

```text
active runtime
```

from:

```text
persistent history
```

---

# 13. Design Principles

The system should follow these principles:

**tmux is the UI.**

Do not build a custom TUI.

**Pi is the intelligence.**

Do not duplicate Pi's reasoning or coding functionality.

**agentd is the runtime.**

It manages processes, PTYs, tmux, communication, and persistence.

**JSONL is the communication protocol.**

Messages should remain structured and machine-readable.

**SQLite is persistent memory/history.**

Do not depend on tmux scrollback for historical data.

**The project directory is the isolation boundary.**

Each project gets its own agents, database, configuration, and history.

**The orchestrator owns delegation.**

Subagents should only be created when the parent agent determines that delegation is useful.

**Human interaction remains Unix-native.**

The user should be able to interact directly with Pi, switch tmux windows, inspect terminals, watch communication, and use normal shell commands.

---

# 14. Initial Implementation Order

Build incrementally.

### Phase 1

Create `start-agents.sh`.

Successfully produce:

```text
window 0 monitor
window 1 orchestrator
```

with the orchestrator's three panes.

### Phase 2

Implement `agentd.py` with one manually spawned subagent.

### Phase 3

Implement JSONL communication and the chat-style communications panes.

### Phase 4

Add SQLite recording for JSONL and PTY events.

### Phase 5

Implement dynamic agent creation/deletion.

### Phase 6

Write and test the orchestrator `spawn-agent` skill.

### Phase 7

Add Git/GitHub workflow conventions and PR-based delegation.

The first milestone is not a sophisticated multi-agent system. The first milestone is:

```text
One project
    ↓
tmux
    ↓
orchestrator
    ↓
one subagent
    ↓
Pi + PTY + JSONL
    ↓
SQLite
    ↓
everything visible through ordinary tmux panes
```

Once that works reliably, additional agents become a scaling problem rather than an architectural problem.