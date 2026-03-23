# dedelulu

*dedelulu is the solulu* — autonomous supervisor for interactive CLI agents. Wraps any command in a PTY,
auto-approves prompts via Claude Code hooks, and watches for agents going off-rails
with a cheap LLM supervisor. Includes a **foreman TUI** (tmux split-pane)
and **multi-worker orchestration** for parallel agents.

## Install

```bash
cd ~/dev/dedelulu
pip install -e .
```

## Quick start

```bash
# Just auto-approve everything — no API keys needed
dedelulu claude "add tests for the auth module"

# With supervisor — watches for derailing
dedelulu --provider azure claude "add tests for the auth module"

# Multi-worker — two agents collaborating
dedelulu-multi \
  --worker "api:.:implement CRUD endpoints" \
  --worker "tests:.:write pytest tests" \
  --provider azure
```

That's it. Open a terminal, cd to your project, run the command.
dedelulu auto-splits tmux (top: Claude, bottom: foreman),
installs Claude Code hooks for instant approval, and logs every decision.

## How it works

```
dedelulu claude "your task"
  │
  ├── installs Claude Code hooks (.claude/settings.local.json)
  │     PreToolUse  → auto-approve (no prompt shown)
  │     PostToolUse → log tool actions to foreman
  │     Stop        → supervisor check when Claude pauses
  │
  ├── spawns claude in PTY (full passthrough, you see everything)
  │     PTY patterns catch non-Claude prompts (npm, git, pip)
  │
  ├── tmux auto-split (if tmux available)
  │     top pane  = Claude Code (you watch / type)
  │     bottom    = foreman (status, logs, escalations)
  │
  └── optional supervisor (--provider):
        every N seconds, asks cheap LLM:
        ├── on_track   → continue (logged)
        ├── off_rails  → Ctrl+C + redirect message
        ├── stuck      → Ctrl+C + interrupt
        └── uncertain  → ESCALATE: ask human (BEL + pause)
```

**Three layers:**

| Layer | What it does | LLM needed? |
|-------|-------------|-------------|
| Hooks | Claude Code PreToolUse auto-approve | No |
| Doorman | PTY pattern-match for non-Claude prompts | No |
| Supervisor | Periodically checks if agent is on track | Yes (cheap model) |

## Usage

### Basic: auto-approve only (no API key needed)

```bash
dedelulu claude "refactor auth to use JWT"
dedelulu claude                              # interactive mode
dedelulu npm init                            # works with any CLI
```

### With supervisor (watches for derailing)

```bash
# Azure OpenAI (gpt-4o, gpt-5.2, gpt-5.4)
dedelulu --provider azure claude "add JWT auth"
dedelulu --provider azure --model gpt-5.2 claude "big refactor"

# Local Ollama (free, fast)
dedelulu --provider ollama claude "add JWT auth"

# Anthropic API
dedelulu --provider anthropic claude "fix login bug"

# OpenAI API
dedelulu --provider openai claude "add tests"

# Another Claude Code instance (Max subscription, no API cost)
dedelulu --provider claude-cli claude "refactor everything"
```

### Multi-worker: parallel agents

```bash
dedelulu-multi \
  --worker "api:~/project:implement REST endpoints" \
  --worker "tests:~/project:write comprehensive tests" \
  --provider azure

# Foreman commands (in the bottom pane):
/send api "freeze the API, tests is starting"
/broadcast "commit and push"
/group create backend api tests
/send backend "coordinate on shared models"
/status
/focus api
/log tests
```

### Options

```
--idle SECS         Seconds of silence before auto-responding (default: 4)
--provider          LLM for supervisor: none|ollama|anthropic|openai|azure|claude-cli
--model MODEL       Specific model (default: gpt-4o for azure, qwen3:4b for ollama)
--goal GOAL         What the agent should accomplish (auto-extracted from claude command)
--supervise SECS    Supervisor check interval (default: 60s when provider is set)
--dry-run           Detect prompts but don't send responses
--log FILE          Log file path (default: dedelulu.jsonl)
--no-log            Disable logging
--max-responses N   Stop auto-approving after N responses (0=unlimited)
--no-hooks          Disable Claude Code hooks (PTY-only mode)
--no-tmux           Single-pane mode, no tmux split
```

### Environment variables

```bash
# Azure OpenAI (recommended)
export AZURE_OPENAI_ENDPOINT=https://your-instance.openai.azure.com
export AZURE_OPENAI_API_KEY=your-key
export AZURE_OPENAI_DEPLOYMENT=gpt-4o    # or gpt-5.2, gpt-5.4
export AZURE_OPENAI_API_VERSION=2024-12-01-preview

# Ollama
export OLLAMA_HOST=192.168.1.100     # bare IP, or http://host:port

# Anthropic API
export ANTHROPIC_API_KEY=sk-ant-...

# OpenAI API
export OPENAI_API_KEY=sk-...
```

## Provider comparison

| Provider | Speed | Cost | Setup |
|----------|-------|------|-------|
| `none` | instant | free | nothing |
| `ollama` (qwen3:4b) | ~0.5s | free | `ollama pull qwen3:4b` |
| `azure` (gpt-4o) | ~1-2s | pay-per-use | endpoint + key |
| `azure` (gpt-5.2) | ~2s | pay-per-use | endpoint + key |
| `anthropic` (haiku) | ~1-2s | ~$0.001/check | API key |
| `openai` (gpt-4o-mini) | ~1-2s | ~$0.001/check | API key |
| `claude-cli` | ~3-5s | free (Max sub) | `claude` on PATH |

## Architecture

### Single worker

```
┌──────────────────────────────────┐
│  Claude Code (full passthrough)  │
│  hooks auto-approve everything   │
├──────────────────────────────────┤
│  foreman                         │
│  14:30 ✓ Write src/app.py        │
│  14:31 ● on track                │
│  14:35 ⚠ NEEDS YOUR INPUT:       │
│    "Delete migrations?" [y/n]    │
│  > yes                           │
└──────────────────────────────────┘
```

### Multi-worker

```
┌──────────────────┬───────────────────┐
│  [api] Claude     │  [tests] Claude   │
│  writing routes   │  writing tests    │
├──────────────────┴───────────────────┤
│  foreman — 2 workers online          │
│  14:30 [api]   ✓ Write routes.py     │
│  14:31 [tests] ✓ Read routes.py      │
│  /send api "API is frozen"           │
│  → [api] 📨 delivered                │
└──────────────────────────────────────┘
```

Foreman commands:

```
/send <worker|group> "msg"   — message a worker or group
/broadcast "msg"             — message all workers
/group create <name> <w...>  — create a group
/add <worker> <group>        — add to group
/status                      — worker status overview
/focus <worker>              — switch tmux to that pane
/log <worker>                — recent events
```

## Hooks

When wrapping `claude`, dedelulu auto-installs Claude Code hooks
(and removes them on exit):

| Hook | Purpose |
|------|---------|
| `PreToolUse` | Auto-approve all tools (100% reliable, no ANSI parsing) |
| `PostToolUse` | Log tool actions, feed supervisor context |
| `Stop` | Supervisor check when Claude pauses |

Use `--no-hooks` to disable and fall back to PTY pattern matching only.

## Escalation

When the supervisor is uncertain about what the agent is doing:

1. Foreman shows **yellow banner**: `⚠ NEEDS YOUR INPUT: ...`
2. Terminal **BEL** sounds (beep / window flash)
3. Auto-approval **pauses** until you respond
4. You type your answer in the foreman pane
5. Response forwarded to Claude, auto-approval resumes

## Log format

Every decision is logged to `dedelulu.jsonl` (one JSON object per line):

```json
{"ts": "...", "event": "hook_approve", "tool": "Write"}
{"ts": "...", "event": "respond", "response": "", "source": "pattern", "context": "Esc to cancel", "count": 1}
{"ts": "...", "event": "supervise", "status": "on_track", "action": "continue", "reasoning": "..."}
{"ts": "...", "event": "escalate", "question": "Agent wants to delete migrations, is this intended?"}
{"ts": "...", "event": "intervene", "type": "message", "message": "Focus on the login bug"}
```

## Demo

### Single worker

```bash
mkdir -p /tmp/dedelulu-demo && cd /tmp/dedelulu-demo
dedelulu claude "Create a Flask CRUD API for users with pytest tests."
```

### Multi-worker collaboration

```bash
cd ~/dev/dedelulu
./demo_multi.sh                        # with real Claude Code
./demo_multi.sh --provider azure       # with Azure supervisor
```

## Safety

- Hooks auto-approve at the Claude Code API level — no ANSI parsing fragility
- Supervisor is conservative: only interrupts when clearly stuck/wrong
- Escalation: asks human when uncertain instead of guessing
- `--max-responses N` caps total auto-approvals
- `--dry-run` shows what would happen without doing it
- Rail detection: pauses if too many responses/minute or repeated prompts
- You can always type into the terminal yourself — dedelulu backs off
- Hooks are auto-removed on exit (original settings restored)

## E2E Guide

See **[E2E_GUIDE.md](E2E_GUIDE.md)** for complete walkthroughs: simple tasks,
supervised sessions, overnight autonomy, and parallel agents.
