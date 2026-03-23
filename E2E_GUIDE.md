# End-to-End Guide: Autonomous Claude Code with dedelulu

## Setup (one time)

```bash
# 1. Install dedelulu
cd ~/dev/dedelulu
pip install -e .

# 2. Choose your supervisor provider:

# Option A: Azure OpenAI (recommended — gpt-4o/5.2/5.4)
export AZURE_OPENAI_ENDPOINT=https://your-instance.openai.azure.com
export AZURE_OPENAI_API_KEY=your-key
export AZURE_OPENAI_DEPLOYMENT=gpt-4o

# Option B: Ollama (free, private, fast)
export OLLAMA_HOST=192.168.8.107
ollama pull qwen3:4b   # run on the ollama machine

# Option C: Anthropic API
export ANTHROPIC_API_KEY=sk-ant-...

# Option D: Another Claude Code instance (from regular terminal only)
# No setup needed — uses your Max subscription

# 3. Verify it works
dedelulu --idle 2 --no-log python3 -c "
r = input('Continue? [Y/n]: ')
print(f'got: {r}')
"
# Should auto-answer "y" after 2 seconds
```

## Use Case 1: Simple task, just auto-approve

No supervisor needed. Just let Claude work and approve everything.

```bash
cd ~/my-project
dedelulu claude "add a health check endpoint at /api/health"
```

What happens:
- tmux splits: top = Claude, bottom = foreman
- Claude Code hooks auto-approve all tool uses (no prompts shown)
- Foreman shows each approval in real-time
- Claude finishes, you review with `git diff`

## Use Case 2: Bigger task with supervisor

For longer tasks where Claude might get distracted.

```bash
cd ~/my-project
dedelulu --provider azure claude "add JWT authentication with middleware and tests"
```

What happens:
- Same as above, plus supervisor checks every 60s
- If Claude goes off-rails: Ctrl+C + redirect message
- If uncertain: foreman asks you (BEL + yellow banner)
- Everything logged to `dedelulu.jsonl`

## Use Case 3: Overnight autonomous session

Leave Claude working on a big task while you sleep.

```bash
cd ~/my-project

# Create a focused CLAUDE.md for the task
cat > CLAUDE.md << 'EOF'
# Task: Comprehensive Test Suite
Add unit tests for all modules in src/. Target 80% coverage.
Focus on: auth, api, database modules.
Do NOT refactor existing code. Only add tests.
EOF

# Launch with supervisor, safety limits, and detailed logging
dedelulu \
  --provider azure --model gpt-5.2 \
  --supervise 30 \
  --max-responses 100 \
  --idle 6 \
  --log overnight-$(date +%Y%m%d).jsonl \
  claude "Follow the instructions in CLAUDE.md"

# Next morning: review
cat overnight-*.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    ev = d['event']
    if ev == 'hook_approve':
        pass  # skip noise
    elif ev == 'respond':
        print(f\"{d['ts'][11:19]} AUTO: sent {repr(d['response'])} ({d['source']})\")
    elif ev == 'supervise' and d.get('status') != 'on_track':
        print(f\"{d['ts'][11:19]} WARN: {d['status']} — {d.get('reasoning','')}\")
    elif ev == 'escalate':
        print(f\"{d['ts'][11:19]} ESCALATE: {d.get('question','')}\")
    elif ev == 'intervene':
        print(f\"{d['ts'][11:19]} INTERVENE: {d.get('message','')[:80]}\")
    elif ev in ('start', 'exit'):
        print(f\"{d['ts'][11:19]} {ev.upper()}\")
"
git diff --stat
```

## Use Case 4: Multi-worker collaboration

Two agents working on the same project, coordinated by the foreman.

```bash
cd ~/my-project

dedelulu-multi \
  --worker "api:.:implement REST CRUD endpoints in app.py" \
  --worker "tests:.:write pytest tests in test_app.py" \
  --provider azure

# In the foreman pane:
/status                                    # see who's doing what
/send tests "api worker finished, check app.py"  # coordinate
/group create backend api tests            # create a group
/send backend "freeze interfaces"          # message the group
/broadcast "commit your changes"           # message everyone
/focus api                                 # switch tmux to api pane
/log tests                                 # last 15 events from tests
```

### Ready-to-run multi-worker demo

```bash
cd ~/dev/dedelulu
./demo_multi.sh                      # with real Claude Code
./demo_multi.sh --provider azure     # with Azure supervisor
```

This creates a git repo with a Flask scaffold, clones it into two dirs,
and launches two Claude agents: one builds the API, the other writes tests.

## Use Case 5: Dry run first, then go

See what would be auto-approved before committing to it.

```bash
cd ~/my-project

# Dry run — see what prompts Claude triggers
dedelulu --dry-run --idle 6 \
  claude "delete all unused dependencies and clean up imports"
# Watch for [DRY RUN] messages, Ctrl+C when satisfied

# If it looks safe:
dedelulu --provider azure \
  claude "delete all unused dependencies and clean up imports"
```

## Use Case 6: Non-Claude CLI tools

dedelulu works with any interactive CLI, not just Claude Code.

```bash
# npm/yarn
dedelulu npm init
dedelulu npx create-next-app my-app

# git interactive
dedelulu git rebase -i HEAD~5

# Any script with prompts
dedelulu ./setup.sh
```

For non-Claude tools, PTY pattern matching handles prompts (hooks are
Claude Code specific).

## Foreman reference

The foreman pane shows real-time events and accepts commands:

### Event display

```
14:30 ✓ Write (src/app.py)          — hook auto-approved a tool
14:31 #3 sent ↵ (pattern)           — PTY auto-responded to a prompt
14:32 ● on track — writing tests    — supervisor check passed
14:35 ⚠ NEEDS YOUR INPUT:           — supervisor escalation
      "Agent wants to delete DB migrations. Proceed?"
> yes, old migrations are obsolete   — your response, forwarded to Claude
```

### Colors

| Color | Meaning |
|-------|---------|
| Gray | routine (auto-approve, on_track) |
| Cyan | informational |
| **Yellow bold** | escalation — needs your input (+ BEL) |
| **Red bold** | intervention — supervisor acted (+ BEL) |

### Commands (multi-worker)

```
/send <worker|group> "msg"   — message a worker or group
/broadcast "msg"             — message all workers
/group create <name> <w...>  — create a group
/add <worker> <group>        — add worker to group
/remove <worker> <group>     — remove from group
/groups                      — list groups
/status                      — worker overview
/focus <worker>              — switch tmux pane
/log <worker>                — last 15 events
/help                        — command list
```

## Troubleshooting

### dedelulu doesn't respond to prompts
- Increase `--idle` (maybe Claude is still outputting)
- Check `dedelulu.jsonl` for what's being detected
- Try `--dry-run` to see pattern matches without sending

### Hooks not working
- Check `.claude/settings.local.json` was created
- Use `--no-hooks` to fall back to PTY-only mode
- Hooks are auto-removed on exit; if dedelulu crashed, manually
  restore `.claude/settings.local.json`

### Supervisor keeps intervening unnecessarily
- Increase `--supervise` interval (e.g., 120 seconds)
- Make `--goal` more specific
- Supervisor is conservative by design; false positives are rare

### Claude Code refuses to start (nested session error)
- dedelulu clears CLAUDECODE env var automatically
- If still failing, run from a regular terminal, not inside Claude Code

### Azure connection issues
- Verify: `curl -H "api-key: $AZURE_OPENAI_API_KEY" "$AZURE_OPENAI_ENDPOINT/openai/deployments/$AZURE_OPENAI_DEPLOYMENT/chat/completions?api-version=$AZURE_OPENAI_API_VERSION" -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":5}' -H 'Content-Type: application/json'`
- Check `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT` are set

### Ollama connection issues
- Check: `curl http://$OLLAMA_HOST:11434/api/tags`
- OLLAMA_HOST can be bare IP, host:port, or full URL
- Make sure the model is pulled: `ollama pull qwen3:4b`
