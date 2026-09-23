# Pi Multi-Agent Tmux Orchestration

## Goal

Build a Unix-native multi-agent coding environment around the **Pi coding agent**, using **tmux** for process isolation and panes, plus a lightweight orchestration/monitoring layer inspired by Herdr.

The system should let a parent Pi agent spawn multiple subagents, give each subagent its own tmux pane, monitor their progress from a sidebar, and coordinate their work without damaging the original TUI of any agent.

## Core Architecture

```text
                         ┌──────────────────────┐
                         │      Parent Pi       │
                         │     ORCHESTRATOR     │
                         └──────────┬───────────┘
                                    │
                    spawn / assign / monitor
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
              ▼                     ▼                     ▼
        ┌───────────┐         ┌───────────┐         ┌───────────┐
        │ tmux pane │         │ tmux pane │         │ tmux pane │
        │ Agent A   │         │ Agent B   │         │ Agent C   │
        │ Pi        │         │ Pi        │         │ Pi        │
        └───────────┘         └───────────┘         └───────────┘
              │                     │                     │
              └─────────────────────┼─────────────────────┘
                                    │
                              event / listener
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │   Agent Monitor      │
                         │   / Sidebar UI       │
                         └──────────┬───────────┘
                                    │
                                    ▼
                              ┌───────────┐
                              │  SQLite   │
                              │  History  │
                              └───────────┘
```

## Design Principles

### 1. tmux is the process layer

Do not try to replace tmux.

tmux should remain responsible for:

- Creating panes/windows
- Running agent processes
- Attaching/detaching sessions
- Terminal input/output
- Keeping agents alive independently
- Giving each subagent an isolated working environment

The orchestration system should sit **above tmux**.

### 2. Pi is the agent layer

Pi remains responsible for:

- Talking to the LLM
- Using coding tools
- Reading/writing files
- Running commands
- Reasoning about tasks
- Spawning or requesting subagents

The orchestration system should not unnecessarily duplicate Pi's agent functionality.

### 3. The monitor is an observation layer

The monitor should observe agents without requiring the agents to change their normal TUI.

This is extremely important.

An agent's original TUI should remain usable when the monitoring system cannot understand it.

**Never transform, filter, or rewrite an agent's terminal UI just to make it fit the sidebar.**

The monitor should prefer structured events/status information. Terminal scraping can be a fallback, not the foundation.

### 4. Fail open

If the monitor cannot understand an agent:

```text
Agent status: UNKNOWN
```

It should **not**:

- Break the TUI
- Rewrite terminal output
- Hide the agent
- Inject incompatible colors
- Assume a particular terminal format
- Require Unicode support

The original agent should continue working normally.

## Agent Registry

Maintain a registry describing every managed agent.

Example conceptual record:

```text
agent_id
parent_id
tmux_session
tmux_window
tmux_pane
task
status
started_at
updated_at
branch
worktree
pid
last_event
```

Useful statuses:

```text
starting
idle
working
waiting
blocked
testing
review
finished
failed
unknown
```

The registry can initially live in SQLite.

## Agent Lifecycle

A typical lifecycle:

```text
Parent Pi
   │
   ├── create task
   │
   ├── create git branch/worktree
   │
   ├── create tmux pane
   │
   ├── start Pi subagent
   │
   ├── register agent
   │
   └── monitor
          │
          ├── working
          ├── testing
          ├── blocked
          └── finished
                  │
                  ▼
              PR / result
                  │
                  ▼
             Parent Pi review
```

## tmux Layout

The exact layout should remain flexible.

One possible arrangement:

```text
┌──────────────────────────────────────────────────────────────┐
│                         Main Pi                              │
├───────────────────────────────┬──────────────────────────────┤
│                               │                              │
│                               │  AGENTS                      │
│                               │                              │
│        Main terminal          │  ● auth       working         │
│        / Parent Pi            │  ● database   testing         │
│                               │  ✓ api        done            │
│                               │  ⚠ tests      blocked         │
│                               │                              │
│                               │  Current: database            │
│                               │                              │
└───────────────────────────────┴──────────────────────────────┘
```

However, the sidebar should not require agents to be displayed inside the same tmux pane.

It could instead be:

- A tmux pane
- A separate terminal application
- A TUI attached to the same session
- A small daemon feeding another UI

The architecture should keep these choices interchangeable.

## Agent Communication

Prefer structured communication over scraping terminal output.

Possible event types:

```text
agent.created
agent.started
agent.status_changed
agent.tool_started
agent.tool_finished
agent.message
agent.blocked
agent.test_started
agent.test_finished
agent.commit_created
agent.pr_created
agent.finished
agent.failed
agent.exited
```

Example event:

```json
{
  "event": "agent.status_changed",
  "agent_id": "agent-042",
  "status": "testing",
  "timestamp": "..."
}
```

The monitor consumes these events and updates the registry/UI.

## Fallback Observation

If structured events are unavailable, the monitor can inspect tmux.

Potential fallback information:

```bash
tmux list-panes
tmux display-message
tmux capture-pane
```

This can determine things such as:

- Whether the process is alive
- Whether the pane exists
- Last visible terminal output
- Whether the agent appears idle
- Whether the agent exited

But terminal parsing should be deliberately limited.

Do not make the system dependent on parsing Pi's visual TUI.

## Sidebar Requirements

The sidebar should be compact and useful at a glance.

Example:

```text
AGENTS
────────────────────────────
● 01 auth       coding
● 02 database   testing
● 03 api        review
✓ 04 frontend   done
⚠ 05 tests      blocked
────────────────────────────
5 agents | 3 active
```

Selecting an agent should ideally allow:

- Jumping to its tmux pane
- Viewing its task
- Viewing its current status
- Viewing recent events
- Viewing branch/worktree
- Viewing recent commits
- Viewing PR information
- Sending a message/command if supported

## Git Workflow

Each subagent should ideally work in isolation.

Preferred model:

```text
main
 │
 ├── agent/auth
 ├── agent/database
 ├── agent/api
 └── agent/frontend
```

For stronger isolation, use separate Git worktrees:

```text
project/
project-agent-auth/
project-agent-database/
project-agent-api/
```

Each agent can then:

1. Receive a task.
2. Work independently.
3. Commit changes.
4. Run tests.
5. Create a PR or equivalent result.
6. Report completion to the parent.
7. Parent Pi reviews the result.
8. Parent Pi merges when appropriate.

## SQLite

SQLite should store historical orchestration data rather than being required for the agents to function.

Potential tables:

```text
agents
events
tasks
commits
pull_requests
sessions
```

The database should allow later investigation:

- What did each agent do?
- What task was it given?
- What tools did it use?
- When did it become blocked?
- What commits did it create?
- What PR resulted?
- What did the parent agent do afterward?

This creates an audit/debugging trail for the multi-agent system.

## Screenshots and Rich Data

If screenshots or other binary artifacts are recorded, avoid unnecessarily bloating SQLite.

Possible approach:

```text
SQLite
  └── metadata + path/hash

filesystem
  └── screenshots/
        ├── agent-01/
        ├── agent-02/
        └── ...
```

The database can reference the artifact rather than storing every large binary directly.

## Parent/Subagent Responsibilities

### Parent Pi

The parent should:

- Understand the overall objective
- Break work into tasks
- Spawn agents
- Assign tasks
- Monitor progress
- Resolve dependencies
- Review results
- Decide whether work is acceptable
- Merge completed work
- Reassign failed/blocked tasks

### Subagent

A subagent should:

- Focus on one clearly defined task
- Work in its own environment
- Report meaningful state changes
- Commit its work
- Run relevant tests
- Report blockers
- Produce a clear completion result

## Important Separation

Keep these layers separate:

```text
Pi
│
├── Agent reasoning
│
├── Tools
│
└── Subagents
      │
      ▼
Orchestrator
│
├── Agent registry
├── Event system
├── Lifecycle management
└── Git coordination
      │
      ▼
tmux
│
├── Sessions
├── Windows
└── Panes
      │
      ▼
Terminal
```

This makes it possible to replace one layer without rebuilding everything.

For example:

- Replace tmux with another process manager.
- Replace the sidebar with another TUI.
- Replace SQLite with another database.
- Replace Pi with another coding agent.

## MVP

Do not build everything at once.

### Phase 1 — tmux launcher

Build a small command that:

1. Creates a tmux session.
2. Creates a pane.
3. Starts a Pi agent.
4. Gives it a task.
5. Records the agent ID and pane ID.

### Phase 2 — Registry

Add SQLite.

Track:

```text
agent_id
pane_id
pid
task
status
created_at
updated_at
```

### Phase 3 — Monitor

Create a simple monitor that discovers:

- Running agents
- Dead agents
- Pane state
- Basic status

### Phase 4 — Sidebar

Build a minimal TUI showing:

```text
agent ID
task
status
runtime
```

Allow selecting an agent and jumping to its pane.

### Phase 5 — Structured Events

Add proper agent lifecycle events.

Move away from terminal scraping wherever possible.

### Phase 6 — Git Integration

Add:

- Branch/worktree creation
- Commit tracking
- PR tracking
- Parent-agent notifications

### Phase 7 — History and Debugging

Record agent events and interactions in SQLite.

Add the ability to inspect an agent's history after it finishes.

## Non-Goals

The first version should **not** attempt to:

- Rebuild Pi's TUI
- Parse every terminal character
- Replace tmux
- Build a complete Git hosting platform
- Build a general-purpose distributed agent framework
- Add unnecessary network services
- Require a cloud service

The system should remain **local-first, Unix-native, lightweight, and composable**.

## Key Design Constraint

> **The monitoring system must never be allowed to break the underlying agent.**

If the sidebar crashes, agents continue running.

If the event listener crashes, agents continue running.

If SQLite becomes unavailable, agents continue running.

If the monitor cannot understand a TUI, the TUI remains untouched.

The orchestration layer should be **observational and additive**, not a fragile dependency of the agents themselves.

## Success Criteria

The system is successful when I can run something conceptually like:

```bash
agent spawn "Implement the authentication API"
agent spawn "Create the database schema"
agent spawn "Write frontend login components"
```

and automatically get:

```text
TMUX
├── Parent Pi
├── Agent: authentication
├── Agent: database
└── Agent: frontend
```

while a sidebar continuously shows:

```text
AGENTS

● authentication   working
● database         testing
● frontend         waiting

3 agents | 2 active
```

I should be able to select an agent, jump directly to its tmux pane, inspect its work, and eventually see:

```text
✓ authentication   PR #12
✓ database         PR #13
✓ frontend         PR #14
```

with the parent Pi able to review and integrate the completed work.

## Guiding Philosophy

**Unix primitives underneath. Agent intelligence above them.**

Use tmux for terminals.

Use Git for isolation and integration.

Use SQLite for durable history.

Use structured events for orchestration.

Use a TUI for visibility.

Use Pi for reasoning.

Keep every component replaceable and make failures degrade gracefully.
