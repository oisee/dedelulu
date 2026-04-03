# Universal AI Agent Wrapper — Technical Specification

## Vision

`ddll` becomes a universal dispatcher for AI CLI agents. One interface,
many backends. Each agent has its own sandbox rules, flags, and modes —
ddll abstracts the differences.

```
ddll ask claude "review this code"      # → claude -p "..." --allowedTools "Read,Glob,Grep"
ddll ask codex "fix the auth bug"       # → codex exec "..." --full-auto
ddll ask gemini "explain this"          # → gemini -p "..." --approval-mode=yolo
ddll ask gpt54 "summarize @README.md"   # → Azure OpenAI API (current implementation)

ddll send claude "review @src/auth.py"  # → launch claude, route response back via IPC
ddll explore                            # → show all: workers + agents + LLM endpoints
```

## Agent Types

### Current (API-only, stateless per call)

These already work — direct API calls, no CLI process:

| Name | Backend | How it works |
|------|---------|-------------|
| `gpt54` | Azure OpenAI API | HTTP call, built-in default |
| `gemini` | Google Gemini API | HTTP call, built-in when GEMINI_API_KEY set |
| Custom | Any provider | Via `DDLL_LLM_*` env vars |

### New: CLI Agent Backends (sandboxed, process-based)

These launch a CLI process with appropriate flags:

| Name | CLI | Chat mode | Autonomous mode |
|------|-----|-----------|-----------------|
| `claude` | `claude` | `claude -p "question" --allowedTools "Read,Glob,Grep"` | `dedelulu claude "task"` (full supervisor) |
| `codex` | `codex` | `codex exec "question"` | `codex exec "task" --full-auto` |
| `copilot` | `gh copilot` | `gh copilot suggest "question"` | TBD |
| `gemini` | `gemini` | `gemini -p "question" --approval-mode=plan` | `gemini -p "task" --approval-mode=yolo -s` |

## Modes

### 1. Chat mode (`ddll ask <agent> "question"`)

Quick Q&A — agent reads code but doesn't modify anything.

```bash
# Claude: read-only tools, no edit/write/bash
ddll ask claude "explain the auth flow in @src/auth.py"
# → claude -p "explain the auth flow in <file contents>" --allowedTools "Read,Glob,Grep"

# Codex: default sandbox (read-only)
ddll ask codex "what does this function do? @src/parser.go"
# → codex exec "what does this function do? <file contents>"

# Persistent session
ddll ask claude -s review "review @src/auth.py"
ddll ask claude -s review "what about the JWT validation?"
```

### 2. Autonomous mode (`ddll run <agent> "task"`)

Agent gets full permissions, supervised by dedelulu:

```bash
# Claude: full supervisor with hooks, tmux split, the works
ddll run claude "add tests for the auth module"
# → dedelulu --style auto claude "add tests for the auth module"

# Codex: full-auto mode
ddll run codex "fix the failing tests"
# → codex exec "fix the failing tests" --full-auto

# With explicit sandbox level
ddll run claude --sandbox read-write "refactor auth to JWT"
ddll run codex --yolo "just do it"
```

### 3. Send mode (`ddll send <agent> "message"`)

Same as chat mode, but routes response back via IPC to the sender:

```bash
# From inside a dedelulu worker:
ddll send claude "is this approach correct? @src/handler.go"
# → launches claude -p, captures output, delivers via IPC as [from:claude]
```

## Sandbox Levels

| Level | Claude flags | Codex flags | Gemini flags | What agent can do |
|-------|-------------|-------------|-------------|-------------------|
| `read-only` (default for ask) | `--allowedTools "Read,Glob,Grep" --bare` | (default sandbox) | `--approval-mode=plan` | Read files, search code |
| `read-write` | `--allowedTools "Read,Edit,Write,Glob,Grep" --bare` | `--full-auto` | `--approval-mode=auto_edit` | Read + edit files |
| `full` | `--allowedTools "Bash,Read,Edit,Write,Glob,Grep"` | `--full-auto` | `--approval-mode=yolo -s` | Everything, sandboxed |
| `yolo` | `--dangerously-skip-permissions` | `--yolo` | `--approval-mode=yolo` | No restrictions |

## `ddll explore` Output

```
$ ddll explore

SESSION      WORKER       TYPE     PID      HOST             DIR                      TASK
──────────── ──────────── ──────── ──────── ──────────────── ──────────────────────── ──────────────────
3fpxierq     main         claude   397759   (local)          ~/dev/minz-vir           claude --resume
fnm8yt76     main         claude   433055   m2.local         ~/dev/dedelulu           claude

AGENTS       STATUS       VERSION
──────────── ──────────── ────────────────────
claude       ready        claude-opus-4-6
codex        ready        codex 0.1.2
copilot      not found    —
gemini       not found    —

LLM          PROVIDER     MODEL                STATUS
──────────── ──────────── ──────────────────── ────────────────────
gpt54        azure        gpt-5.4              ready
gemini       google       gemini-2.5-flash     ready
```

## Agent Discovery

CLI agents are discovered by checking if the binary exists on PATH:

```python
class AgentRegistry:
    AGENTS = {
        'claude': {
            'binary': 'claude',
            'version_cmd': 'claude --version',
            'chat_cmd': lambda q, tools: ['claude', '-p', q, '--bare', '--allowedTools', tools],
            'auto_cmd': lambda task: ['dedelulu', '--style', 'auto', 'claude', task],
        },
        'codex': {
            'binary': 'codex',
            'version_cmd': 'codex --version',
            'chat_cmd': lambda q: ['codex', 'exec', q],
            'auto_cmd': lambda task: ['codex', 'exec', task, '--full-auto'],
        },
        'gemini': {
            'binary': 'gemini',
            'version_cmd': 'gemini --version',
            'chat_cmd': lambda q: ['gemini', '-p', q, '--approval-mode=plan'],
            'auto_cmd': lambda task: ['gemini', '-p', task, '--approval-mode=yolo', '-s'],
        },
    }
```

## Supervisor Defaults

**Default mode: no LLM supervisor** (pattern-only, `--style auto`).

When LLM supervision is needed (`--style active` or `--style strict`),
use the cheapest available model:

| Provider | Default supervisor model |
|----------|------------------------|
| Azure | `gpt-4o-mini` |
| OpenAI | `gpt-4o-mini` |
| Anthropic | `claude-haiku-4-5` |
| Ollama | `qwen3:4b` |
| Google | `gemini-2.5-flash` |

Never use large/expensive models for supervision. The supervisor sends
~1KB prompts and expects ~100 token JSON responses — mini/nano models
handle this perfectly.

## File Injection

Works uniformly across all backends:

```bash
ddll ask claude "review @src/auth.py"        # @file resolved, contents injected
ddll ask codex "explain @README.md"          # same mechanism
ddll ask gpt54 "summarize @README.md"        # already works (current implementation)
```

For CLI agents (claude, codex), file contents are prepended to the prompt.
For API agents (gpt54, gemini), injected into the messages array.

## Claude-specific: `--resume` for sessions

Claude Code supports `--resume <session-id>` to continue a previous session
natively (with full tool context, not just message history). This is superior
to our JSON-based session persistence for Claude:

```bash
ddll ask claude -s review "review @src/auth.py"
# First call: claude -p "..." --bare --allowedTools "Read,Glob,Grep"
# Captures session-id from output

ddll ask claude -s review "what about the JWT part?"
# Subsequent calls: claude --resume <session-id> -p "..."
# Full Claude context preserved (not just message concatenation)
```

This is a unique Claude advantage — codex and gemini don't have session resume.

## Session Persistence

Same mechanism for all backends — stored in `/tmp/dedelulu_llm_sessions/`:

```bash
ddll ask claude -s review "review @src/auth.py"     # → claude_review.json
ddll ask codex -s bugfix "find the null pointer"     # → codex_bugfix.json
ddll ask gpt54 -s paper "review @paper.md"           # → gpt54_paper.json (already works)
```

For CLI agents, session history is concatenated into the prompt
(same as current `_ask_claude_cli` implementation).

## Implementation Priority

1. **Phase 0 (done)**: API agents — gpt54, gemini, custom via env vars
2. **Phase 1**: `ddll ask claude` — launch claude -p in read-only mode, capture output
3. **Phase 2**: `ddll ask codex` — same pattern for codex CLI
4. **Phase 3**: `ddll run` — autonomous mode with full supervisor
5. **Phase 4**: Agent discovery in `ddll explore`
6. **Phase 5**: Sandbox levels, copilot/gemini-cli backends

## Default Launch Mode

When running `ddll claude "task"` (or just `ddll claude`):

**No tmux panel by default.** Full screen for the agent. The foreman log panel
is hidden — hooks handle approval silently in the background.

Before launching the agent, show a brief cheat-sheet:

```
┌─────────────────────────────────────────────────┐
│  dedelulu v0.2 — hooks active, auto-approving   │
│                                                  │
│  Ctrl+T  toggle foreman panel                    │
│  Ctrl+H  show this help                          │
│  Ctrl+L  show log tail                           │
│  ddll send <target> "msg"  message other agents  │
│  ddll explore              list workers + LLMs   │
└─────────────────────────────────────────────────┘
```

Then launch the agent in full-screen PTY.

**What runs silently:**
- Hooks: PreToolUse auto-approve, PostToolUse logging, Stop check
- IPC: message forwarding (ddll send/ask responses delivered to PTY)

**What does NOT write to PTY:**
- Supervisor verdicts ("on track", "stuck", etc.) — only logged to file
- Notifications — only shown if panel is toggled on
- No `[dedelulu]` banners mid-typing

**Toggling the panel:** Ctrl+T splits tmux and shows the foreman log.
Ctrl+T again hides it. Panel shows full supervisor output, events, IPC.

**Escalation override:** If supervisor detects `uncertain` or `escalate`,
it DOES interrupt — BEL + message. This is the only case where dedelulu
writes to PTY in default mode.

## Known Constraints

**Codex CLI + nested namespaces:** Codex uses `bwrap` (bubblewrap) sandbox which
conflicts with nested Linux namespaces. Running `codex exec` from inside a Claude
Code Bash tool call will fail. `ddll run codex` must spawn Codex in its own
terminal/PTY, not nested inside another agent's sandbox. This means:
- `ddll ask codex` from a standalone terminal: works
- `ddll send codex` from a dedelulu-supervised Claude Code session: must fork a
  separate process outside the PTY, not run inside Claude's Bash tool

**Claude Code --dangerously-skip-permissions:** No short alias (unlike Codex `--yolo`).
Must use the full flag. `ddll run claude --yolo` should translate to the long form.

## Non-Goals

- Not building a new agent framework — just wrapping existing CLIs
- Not replacing dedelulu's supervisor — `ddll run` IS dedelulu
- Not adding GUI — terminal-native only
- Not managing API keys for CLI agents — they handle their own auth
