# Spawn-Agent Skill

Teach the orchestrator Pi when and how to delegate work to subagents, and how to manage them through the agent orchestration system.

## Concept

The orchestration system manages subagents for you. You should think:

> "I need another worker."

and invoke the agent-management capability. You do **not** need to manage tmux, panes, or PTYs directly. Those are handled for you by `agentd.py`.

Subagents are independent Pi processes running in their own workspace. Each performs one clear task and reports back.

## When to delegate

Delegate work to a subagent when:

- A task is well-defined and can be worked on independently.
- You are blocked and a parallel worker can make progress elsewhere.
- A piece of work can be meaningfully verified on its own (tests, PR).
- The work does not depend on uncommitted changes you are current making.

Do **not** delegate when:

- The task is small enough to do yourself.
- The task needs context only you currently hold.
- The task depends on another agent's uncommitted work.

## Creating a task description

A good task description is:

- **Specific**: states exactly what to build or investigate.
- **Bounded**: defines what is in scope and what is not.
- **Verifiable**: describes how the work can be checked (tests, expected output).
- **Context-rich**: includes the working directory, relevant files, and any constraints.

Example:

```text
Investigate the authentication flow in src/auth/. Find the bug causing
session tokens to expire early. Do not change code. Report the root
cause and the specific files/lines involved.
```

## Choosing an agent name

Use a short, descriptive name reflecting the task, not the parent task:

```text
auth-worker      (not: my-new-agent-1)
db-schema
frontend-login
api-tests
```

## The agent management interface

All commands are run with `agentd.py` from the project directory as the orchestrator.

### Spawn an agent

```bash
python3 .agents/agentd.py spawn <name> <task-description>
```

The task description can be multiple words and will be joined into a single task string.

### List agents and status

```bash
python3 .agents/agentd.py status
```

Shows each agent's ID, name, status, window, creation time, and its current task.

### Check active tmux sessions

```bash
python3 .agents/agentd.py sessions
```

Lists live tmux sessions managed by the orchestration system.

### Send a message to an agent

```bash
echo "Your message" | python3 .agents/agentd.py send <agent-id>
```

Messages are delivered over the agent's JSONL communication channel.

### Stop an agent

```bash
python3 .agents/agentd.py stop <agent-id>
```

Stops the agent, removes its tmux window, and marks it stopped in the registry.

### Stop all agents

```bash
python3 .agents/agentd.py kill-all
```

Stops every running agent but keeps the session alive.

## Where agents work

Each agent gets:

- Its own tmux window with a Pi pane, a PTY viewer, and a communications viewer.
- Its own working session and communication channel in `.agents/`.
- Its own git branch, created at spawn time, to keep work isolated.

Agents do not share a working directory unless the task explicitly says so.

## Communication protocol

- Messages to agents are structured JSONL over the agent's communication channel.
- Send messages through the management interface described above.
- Do not scrape terminal output to understand an agent; rely on direct communication and status.

## Monitoring progress

- Use `status` to see whether an agent is starting, working, waiting, blocked, testing, finished, or failed.
- Use direct messages to request an update from an agent.
- If an agent is blocked, ask what is blocking it before intervening.

## Requesting results

When an agent reports completion:

1. Ask the agent to summarize what changed and where.
2. Inspect its work directly (files, branch, tests, PR).
3. Verify claims yourself — do not trust a completion report blindly.

## Reviewing and integrating work

Each agent works on its own branch. Review the branch marked with that agent.

Possible workflow:

1. Pull the agent's changes from its branch.
2. Run the relevant tests.
3. Review the diff.
4. When satisfied, merge or ask the agent to open a PR with `gh`.
5. Ask the agent to report its PR URL so you can track integration.

If work is unacceptable, tell the agent precisely what to fix and re-delegate or stop the agent as appropriate.

## Stopping agents

Stop an agent when:

- Its task is done and its result has been integrated.
- It is blocked with no clear path forward and you will handle the work yourself.
- It is no longer needed.

Stopping an agent does not erase its history; the registry retains its record and communication log.

## Anti-patterns

Do not:

- Manually create or destroy tmux panes/windows for agents.
- Reason about pane indices, PTY file descriptors, or terminal layout.
- Assume an agent's terminal format or scrape its UI.
- Spawn agents without a clear, verifiable task.
- Leave finished agents running indefinitely — stop them when their work is integrated.