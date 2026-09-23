# Code Conductor — Plan & Progress Notes

Status notes for the tmux + Pi agent orchestration system.
Two specs live in `docs/`; these notes track our implementation plan and what has shipped so far.

---

## The Plan

Build a project-local, tmux-based multi-agent orchestration system. Pi agents run in their own
tmux windows/panes; a Python daemon (`agentd.py`) owns the lifecycle registry (SQLite), JSONL
communication, and a live monitor window.

The work is phased. Both spec docs define phases; the plan below is the merged, ordered version
we are following.

| # | Phase | Description | Spec ref |
|---|-------|-------------|----------|
| P1 | tmux launcher | `start-agents.sh` creates the session (monitor + orchestrator panes) | impl §14 P1, pi MVP P1 |
| P2 | Registry | SQLite registry of agents; dynamic pane/window indexing; daemon supervision | impl §14 P2, pi MVP P2/P3 |
| P3 | Structured events | Full `agent.*` lifecycle taxonomy recorded in `agent_events` | pi MVP P5, impl §7 |
| P4 | Monitor window | Live per-agent panel in window 0 (name, status, task, last event, branch, pid) | pi MVP P3, impl §4.0 |
| P5 | Git/PR integration | `commits` + `pull_requests` tables, `agent.commit_created` / `agent.pr_created` events, branch/worktree isolation, completion notification → orchestrator review → merge | pi MVP P6, impl §10, impl §14 P7 |
| P6 | History & debugging | Inspect a finished agent's trail: events, messages, PTY excerpts, commits, PRs (`agentd.py history <id>`) | pi MVP P7 |
| P7 | Interactive sidebar | Select an agent in the monitor → jump to pane / view task / view events / view PR info | pi MVP P4, pi §Sidebar |
| P8 | Comms polish | Chat-style formatting of the comms panes; agent→agent routing UX | impl §14 P3, pi §Agent Communication |
| P9 | PTY hardening | ptylog auto-restart on pane pipe drop; input capture | impl §5 |
| P10 | Fail-open + README | Degrade gracefully without `.agents`/DB; document setup for a fresh project | pi §Fail open |

Non-goals (from the specs): no in-repo GitHub implementation — `git` and `gh` stay agent tools;
no terminal scraping of Pi's visual TUI; `agentd.py` must never affect the agents themselves.

---

## Progress

### Committed

**`3d8f154` — initial commit**
- Repo structure, `.gitignore`, spec docs added to `docs/`.

**`5f896e9` — spawn-agent skill**
- `.agents/skills/spawn-agent/SKILL.md`: teaches the orchestrator Pi when/how to delegate,
  the management interface (spawn/status/sessions/send/stop/kill-all), comms protocol,
  review-and-merge workflow, anti-patterns.

**`564c811` — P1 + P2: launcher, registry, daemon**
- `start-agents.sh` attaches to an existing session instead of killing it; starts the daemon
  if not running (PID file `.agents/agentd.pid`, log `.agents/agentd.log`).
- Agents are actually inserted into the registry (fixed from initial stub). Added columns:
  `parent_id`, `window_index`, `window_name`, `pid`, `branch`, `worktree`, `last_event`,
  `created_at`, `updated_at`, `started_at`, `ended_at` + idempotent `_migrate()`.
- SQLite in WAL mode + busy timeout (`_db()`).
- `kill-all` fixed to use non-terminal statuses; `stop_agent` marks `stopped` in the DB first,
  then kills the window so the daemon can't race it to `finished`.
- `send` validates the agent exists in the registry.
- `serve` daemon: `_session_alive`, `_pane_current_command`, `_active_agents`
  (status NOT IN `stopped`/`finished`/`failed`), comms monitor threads per agent, termination
  detection via pane roles + command tracking, marks `finished` on pane loss/pi exit.
- Dynamic indexing (works with `base-index 1` / `pane-base-index 1`): panes assigned by
  `pane_top` (pi = top, pty = middle, comms = bottom); windows via `_next_window_index()`.
- Comm messages carrying a `status` field drive registry lifecycle (`_apply_message_effects`).
- Daemon exits when the tmux session dies.
- Verified end-to-end in real tmux (create, spawn, comms ingestion, window kill → finished,
  daemon exit).

**`eae4243` — P3 + P4: structured events, live monitor**
- P3 — every lifecycle moment now uses the spec taxonomy from pi §Agent Lifecycle:
  `agent.created`, `agent.started`, `agent.status_changed`, `agent.blocked`, `agent.finished`,
  `agent.failed`, `agent.message`, `agent.exited`. `_mark_status(..., event=...)` threads the
  event name through `last_event` and `agent_events`; comms-driven status mappings added;
  `_apply_message_effects` maps generic statuses to structured events.
- P4 — new `agentd.py monitor` command renders the live panel; window 0 runs it on a 3s
  refresh loop (was a plain status dump). Shows per-agent mark, name, status, window, task,
  last event, branch, pid, and active count.
- Fixed a same-second `agent_id` collision (now `agent-<ts>-<uuid4[:6]>`). Verified: 3 rapid
  spawns → unique IDs, unique windows, correct event trail.

### In flight / not started

- **P3 exports**: `agent.tool_started/finished`, `agent.test_*`, `agent.commit_created`,
  `agent.pr_created` events are defined in the taxonomy but not emitted yet — they land with
  P5 (Git/PR) and tool/test instrumentation.
- **P5–P10**: see plan table. Spawn already creates an `agent-<id>-<name>` branch per agent;
  the rest of Git/PR integration (commits/pull_requests tables, PR tracking, notify-on-done,
  review→merge) is the biggest remaining chunk.

### Verification approach

- `python3 -m py_compile .agents/agentd.py` after every edit.
- Full-stack tests in real tmux under `/tmp/opencode/*` (clean sessions named `agents-*`,
  removed after each run): spawn → comms `status` JSON ingestion → registry updates →
  window kill → `finished` → daemon exit. Event trail and registry contents asserted via
  direct SQLite queries.

---

## Environment notes

- Repo: `github.com/DavidFruin/code-conductor` (public), branch `master`.
- tmux config uses `base-index 1` / `pane-base-index 1` — indexing is always dynamic.
- `gh` authenticated as `DavidFruin`; git identity `David Fruin <me@davidfruin.com>`.
- Push to GitHub after each completed phase.