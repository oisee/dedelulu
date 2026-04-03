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
# Auto-approve everything — no API keys needed, no panel, just hooks
ddll claude "add tests for the auth module"

# Run any AI agent in yolo mode
ddll ask gemini "refactor auth to use JWT"
ddll ask codex "fix the failing tests"
ddll ask claude "add logging to the API"

# Talk to LLM APIs
ddll ask gpt54 "review @src/auth.py for security issues"
ddll ask gemini-api "explain this error"

# With supervisor (opt-in)
ddll --provider azure --supervise 60 claude "big refactor"

# Multi-worker — two agents collaborating
dedelulu-multi \
  --worker "api:.:implement CRUD endpoints" \
  --worker "tests:.:write pytest tests"
```

That's it. Open a terminal, cd to your project, run the command.
Hooks auto-approve, IPC messaging works, full-screen agent — no noise.

## How it works

```
ddll claude "your task"
  │
  ├── shows cheat-sheet (ddll send/ask/explore hints)
  │
  ├── installs hooks (.claude/settings.local.json or .gemini/)
  │     PreToolUse  → auto-approve (no prompt shown)
  │     PostToolUse → log tool actions
  │     Stop        → supervisor check (if enabled)
  │
  ├── spawns agent in full-screen PTY (you see everything)
  │     no panel, no banners, no noise — just the agent
  │
  ├── IPC messaging active (ddll send/ask from other agents)
  │
  └── opt-in features:
        --tmux          → foreman panel (status, logs, escalations)
        --no-hooks      → PTY pattern matching (y/n, npm, git prompts)
        --provider X    → LLM supervisor health checks
        --stale N       → nudge idle agents
        --style active  → enable supervisor + stale together
```

**Default mode: hooks-only.** dedelulu installs Claude Code hooks and
auto-approves everything. No patterns, no LLM, no panel, no noise.
Just a transparent pipe that says "yes" to every tool call.

Everything else is opt-in:

| Feature | Default | Enable with |
|---------|---------|-------------|
| Hook auto-approval | **on** | always (use `--no-hooks` to disable) |
| Pattern matching (y/n, Enter) | off | `--no-hooks` (PTY-only mode) |
| LLM supervisor | off | `--provider azure --supervise 60` |
| Stale nudge | off | `--stale 300` |
| Tmux foreman panel | off | `--tmux` |
| IPC messaging (ddll send) | **on** | always |

Or use presets: `--style active` (supervisor + stale), `--style strict` (short leash).

**Three layers (when enabled):**

| Layer | What it does | LLM needed? |
|-------|-------------|-------------|
| Hooks | Claude Code / Gemini CLI PreToolUse auto-approve | No |
| Doorman | PTY pattern-match for non-Claude prompts (`--no-hooks`) | No |
| Supervisor | Periodically checks if agent is on track (`--provider`) | Yes (cheap model) |

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

# Google Gemini
dedelulu --provider google claude "refactor auth"
dedelulu --provider gemini claude "add tests"

# Another Claude Code instance (Max subscription, no API cost)
dedelulu --provider claude-cli claude "refactor everything"
```

### Multi-worker: parallel agents

```bash
# Claude Code workers
dedelulu-multi \
  --worker "api:~/project:implement REST endpoints" \
  --worker "tests:~/project:write comprehensive tests" \
  --provider azure

# Gemini CLI workers
dedelulu-multi \
  --agent gemini \
  --worker "api:~/project:implement REST endpoints" \
  --worker "tests:~/project:write comprehensive tests" \
  --provider google
```
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
--style PRESET      auto (default) | passive | active | strict
--tmux              Enable foreman panel (off by default)
--no-hooks          Disable hooks, use PTY pattern matching instead
--provider          LLM for supervisor: none|ollama|anthropic|openai|azure|google|gemini|claude-cli
--model MODEL       Specific model (default: gpt-4o for azure, gemini-2.5-flash for google)
--supervise SECS    Supervisor check interval (0=off, try 30-120)
--stale SECS        Nudge idle agent after N seconds (0=off)
--goal GOAL         What the agent should accomplish (auto-extracted from command)
--idle SECS         Silence threshold for PTY pattern matching (default: 4, only with --no-hooks)
--dry-run           Detect prompts but don't send responses
--log FILE          Log file path (default: dedelulu.jsonl)
--no-log            Disable logging
--max-responses N   Stop auto-approving after N responses (0=unlimited)
```

### Environment variables

```bash
# Azure OpenAI (recommended)
export AZURE_OPENAI_ENDPOINT=https://your-instance.openai.azure.com
export AZURE_OPENAI_API_KEY=your-key
export AZURE_OPENAI_DEPLOYMENT=gpt-4o    # or gpt-5.2, gpt-5.4
export AZURE_OPENAI_API_VERSION=2024-12-01-preview

# Google Gemini
export GEMINI_API_KEY=your-api-key-from-ai-studio

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
| `google` (flash) | ~1-2s | ~$0.0001/check | GEMINI_API_KEY |
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

## LLM Conversations: `ddll ask`, `ddll run`, and `ddll send`

Talk to AI agents and LLM APIs directly from the terminal. `ddll ask`, `ddll run`,
and `ddll send` are all equivalent for CLI agents — they launch the agent in
full-auto (yolo) mode. For API endpoints, they make HTTP calls.

### Two kinds of targets

**CLI agents** — launch a real CLI process in yolo mode:

| Target | Binary | Command |
|--------|--------|---------|
| `gemini` | `gemini` | `gemini --yolo -p "task"` |
| `codex` | `codex` | `codex exec --yolo "task"` |
| `claude` | `claude` | `claude -p --dangerously-skip-permissions "task"` |

**API endpoints** — HTTP calls, no process spawned:

| Target | Provider | Default model |
|--------|----------|---------------|
| `gpt54` | Azure OpenAI | gpt-5.4 |
| `gemini-api` | Google Gemini | gemini-2.5-flash |
| Custom | Via `DDLL_LLM_*` env vars | configurable |

### Quick examples

```bash
# CLI agents — full-auto mode
ddll ask gemini "add tests for the auth module"
ddll ask codex "fix the failing tests"
ddll ask claude "refactor auth to use JWT"

# API endpoints — quick Q&A
ddll ask gpt54 "what's the best way to structure a Z80 parser?"
ddll ask gemini-api "explain this error message"

# Persistent session — LLM remembers context across messages
ddll ask gpt54 -s review "my name is Alice, I'm reviewing auth code"
ddll ask gpt54 -s review "what should I look for in JWT validation?"

# File injection — inline @file refs or --file flag
ddll ask gpt54 "explain this @README.md"
ddll ask gpt54 "find bugs in @src/parser.go and @src/lexer.go"
ddll ask gpt54 --file src/auth.py "review this for security issues"

# Pipe from stdin
git diff | ddll ask gpt54 "review this diff"

# From another agent via messaging fabric (response routed back via IPC)
ddll send gpt54 "is this the right approach for error handling?"
ddll send gemini "review @src/handler.go for security issues"
```

### Verified results: CLI agents writing files

All three CLI agents were tested with `ddll ask <agent> "write 'hello from <agent>' into /tmp/test.txt"`:

```
$ ddll ask claude "write 'hello from claude' into /tmp/ddll_test_claude.txt"
[claude]
Done. The file /tmp/ddll_test_claude.txt has been created.

$ cat /tmp/ddll_test_claude.txt
hello from claude

$ ddll ask codex "write 'hello from codex' into /tmp/ddll_test_codex.txt"
[codex]
Wrote 'hello from codex' to /tmp/ddll_test_codex.txt and verified the contents.

$ cat /tmp/ddll_test_codex.txt
hello from codex

$ ddll ask gemini "write 'hello from gemini' into /tmp/ddll_test_gemini.txt"
[gemini]
I cannot write to /tmp/ddll_test_gemini.txt because it is outside the
allowed workspace. Wrote to ~/.gemini/tmp/project/ddll_test_gemini.txt instead.

$ cat ~/.gemini/tmp/project/ddll_test_gemini.txt
hello from gemini
```

Note: Gemini CLI enforces its own workspace sandbox even in yolo mode —
it auto-approves tool calls but still restricts writes to its workspace
directory. Claude and Codex write to the requested path directly.

### How it works

```
ddll ask gpt54 -s review "review @auth.py for security"
  │
  ├── LLMRegistry resolves "gpt54" → Azure OpenAI GPT-5.4
  ├── @auth.py extracted, file contents injected as context
  ├── LLMSession loads prior messages (if -s given)
  ├── API call with full conversation history
  ├── Response printed to stdout
  └── Session saved to /tmp/dedelulu_llm_sessions/gpt54_review.json

ddll send gpt54 "question"
  │
  ├── Same LLM call, but session auto-keyed by sender identity
  └── Response routed back to sender via IPC (appears as [from:gpt54])
```

### `ddll ask` options

```
ddll ask <llm> [options] <question>

  <llm>               LLM endpoint name (e.g. gpt54, qwen3-4b)
  <question>          Supports @file.ext inline refs anywhere in text
  -s, --session NAME  Persistent conversation (stored in /tmp/dedelulu_llm_sessions/)
  -f, --file PATH     Include file as context (repeatable)
  --max-tokens N      Max response tokens (default: 4096)
```

### `ddll send` to LLMs

When `ddll send` targets an LLM instead of a worker, it:
- Calls the LLM synchronously
- Maintains a persistent session keyed by sender (e.g. `gpt54_fnm8yt76:main.json`)
- Routes the response back to the sender via IPC

This means agents can have ongoing conversations with LLMs through the
same messaging fabric they use to talk to each other.

### `ddll explore` — discover available LLMs

```
$ ddll explore

SESSION      WORKER       TYPE     PID      DIR                            TASK
──────────── ──────────── ──────── ──────── ────────────────────────────── ──────────────────────────────
3fpxierq     main         claude   397759   ~/dev/minz-vir                 claude --resume
fnm8yt76     main         claude   433055   ~/dev/dedelulu                 claude

LLM          PROVIDER     MODEL                STATUS
──────────── ──────────── ──────────────────── ────────────────────
gpt54        azure        gpt-5.4              ready
qwen3-4b     ollama       qwen3:4b             ready
```

### Configuring LLM endpoints

**Built-in default**: `gpt54` maps to Azure OpenAI GPT-5.4
(uses your existing `AZURE_OPENAI_*` env vars).

**Custom endpoints** via `DDLL_LLM_<NAME>_*` env vars:

```bash
# Add a second Azure model
export DDLL_LLM_O4MINI_PROVIDER=azure
export DDLL_LLM_O4MINI_MODEL=o4-mini
export DDLL_LLM_O4MINI_DEPLOYMENT=o4-mini

# Add an Anthropic model
export DDLL_LLM_HAIKU_PROVIDER=anthropic
export DDLL_LLM_HAIKU_MODEL=claude-haiku-4-5-20251001

# Add an OpenAI model
export DDLL_LLM_GPT4_PROVIDER=openai
export DDLL_LLM_GPT4_MODEL=gpt-4o
```

**Ollama auto-discovery** — whitelist models to auto-detect from a running Ollama server:

```bash
export DDLL_OLLAMA_WHITELIST=qwen3:4b,llama3:8b
# These appear as "qwen3-4b" and "llama3-8b" in ddll explore/ask/send
```

### Real-world scenarios (observed)

**Paper review workflow** — multiple agents independently used file injection
to review research drafts, getting detailed multi-section feedback
(novelty assessment, gap analysis, venue suggestions):
```bash
ddll ask gpt54 -s paper @docs/research_statement.md "review for publication"
ddll ask gpt54 -s paper "find weaknesses in section 4"
ddll ask gpt54 -s paper "suggest improvements for the abstract"
```

**Cross-agent consultation** — agents asking GPT-5.4 for a second opinion
while working on their own tasks:
```bash
# From inside a dedelulu-managed Claude Code session:
ddll send gpt54 "is this the right approach for error handling in @src/handler.go?"
# Response arrives via IPC: [from:gpt54] Yes, but consider...
```

**Quick math/fact checking** — minz agent verified Z80 arithmetic via GPT-5.4
during assembly optimization work:
```bash
ddll send gpt54 "what is 42+7 in Z80 assembly, using ADD A,immediate?"
# [from:gpt54] ADD A, 7 — result in A = 49 (0x31)
```

**Multi-file code review** — injecting multiple files for holistic analysis:
```bash
ddll ask gpt54 "are these consistent? @src/types.go @src/handler.go @src/routes.go"
```

## Comparison: dedelulu vs NVIDIA NemoClaw

[NemoClaw](https://github.com/NVIDIA/NemoClaw) is NVIDIA's open-source reference stack
for running OpenClaw agents securely inside sandboxed containers. dedelulu and NemoClaw
solve **different halves of the same problem** — they're complementary, not competing.

| Aspect | **NemoClaw** | **dedelulu** |
|---|---|---|
| Core purpose | Sandbox security for agents | Autonomous supervision of agents |
| What it controls | What the agent *can't* do | What the agent *should* do |
| Agent | OpenClaw | Claude Code (or any CLI) |
| Isolation | Full container (Landlock, seccomp, netns) | PTY wrapper, no isolation |
| Network policy | Declarative YAML egress control | None |
| Inference routing | Gateway intercepts all LLM calls | Direct `ddll ask`/`ddll send` |
| Approval flow | TUI for operator approval of blocked requests | Auto-approve patterns + LLM supervisor |
| Behavior supervision | None | 3-level: patterns, LLM supervisor, interventor |
| Stale detection | None | Yes, with LLM-powered nudging |
| Multi-agent | Single sandbox per agent | Multi-worker sessions, cross-agent messaging |
| External LLM access | Provider-routed through gateway | `ddll ask` (Azure, OpenAI, Anthropic, Gemini, Ollama) |
| Credential handling | Keys on host, sandbox sees `inference.local` only | Keys in env vars, shared with agent |

### What dedelulu could learn from NemoClaw

- **Network egress policy** — declarative YAML controlling what the agent can access.
  Currently dedelulu trusts agents completely on network. Even lightweight egress
  logging (not blocking) would improve auditability.
- **Inference interception** — NemoClaw's gateway intercepts all LLM API calls from the
  agent. Useful for logging, cost tracking, and enforcing model policies.
- **Credential isolation** — keeping API keys on the host and exposing only a local
  proxy endpoint to the agent. Important for multi-agent setups where workers
  shouldn't see each other's (or the user's) keys.

### What NemoClaw could learn from dedelulu

- **LLM-based behavior supervision** — NemoClaw has zero behavior monitoring. If the
  agent goes off-rails, nobody notices until damage is done. dedelulu's 3-level
  supervision (fast patterns, LLM health checks, human escalation) would catch
  stuck/derailing agents early.
- **Cross-agent messaging** — NemoClaw is single-sandbox with no agent-to-agent
  communication. dedelulu's IPC fabric (`ddll send`/`ddll explore`) enables
  multi-agent collaboration and LLM consultation.
- **Stale detection + nudging** — agents can hang forever in NemoClaw with no detection.
  dedelulu detects idle agents and nudges them (mimicking the user's style).
- **Auto-approval patterns** — NemoClaw requires manual TUI approval for many operations.
  dedelulu's 70+ regex patterns auto-approve common safe prompts (y/n, press enter,
  npm proceed, etc.) without human intervention.
- **External LLM consultation** — agents inside NemoClaw's sandbox can't reach external
  LLMs (blocked by policy). `ddll ask` via the host could provide a controlled,
  policy-compliant channel for cross-model consultation.

### The dream combo

NemoClaw's sandbox + dedelulu's supervision = an agent that **can't do bad things**
(security) AND **stays on track** (behavior). A potential integration path:

```
NemoClaw sandbox (security envelope)
  └── dedelulu supervisor (behavior envelope)
        └── Claude Code / OpenClaw (the agent)
              ├── auto-approved by dedelulu patterns + hooks
              ├── behavior-checked by LLM supervisor
              ├── network-controlled by NemoClaw policy
              └── inference-routed through NemoClaw gateway
```

## E2E Guide

See **[E2E_GUIDE.md](E2E_GUIDE.md)** for complete walkthroughs: simple tasks,
supervised sessions, overnight autonomy, and parallel agents.
