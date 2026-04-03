# Using Gemini with dedelulu

You can use Google Gemini as the supervisor for your agents. This guide explains how to set it up.

## Quick start

1.  **Get a Google AI Studio API key** from [aistudio.google.com](https://aistudio.google.com/).
2.  **Export the API key** in your shell:
    ```bash
    export GEMINI_API_KEY=your-api-key-here
    ```
3.  **Run dedelulu** with the `gemini` or `google` provider:
    ```bash
    # Use the default Gemini model (gemini-2.5-flash)
    dedelulu --provider gemini claude "add tests for the auth module"

    # Specify a different model (e.g. gemini-1.5-pro)
    dedelulu --provider google --model gemini-1.5-pro-002 claude "complex task"
    ```

## External LLM Chat (ddll ask)

You can also use Gemini directly for one-off questions or context-aware queries:

```bash
# Simple question (uses default gemini-2.5-flash)
ddll ask gemini "how do I use pexpect for terminal automation?"

# File context
ddll ask gemini "review @dedelulu.py for security issues"
```

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Your Google API key. |
| `DDLL_LLM_GEMINI_MODEL` | Override the default model used when `gemini` is targeted. |

### Custom Gemini Endpoints

You can define multiple Gemini-based endpoints using the `DDLL_LLM_*` prefix:

```bash
export DDLL_LLM_MYPRO_PROVIDER=google
export DDLL_LLM_MYPRO_MODEL=gemini-1.5-pro
export DDLL_LLM_MYPRO_API_KEY=your-key

# Use it
ddll ask mypro "complex reasoning question"
```

## Using dedelulu as a wrapper for gemini CLI

You can run the `gemini` CLI under the `dedelulu` wrapper to gain autonomous supervision and auto-approval of prompts.

```bash
# Wrap gemini CLI for a task
dedelulu gemini -p "implement a robust parser for Z80 assembly"

# With a specific supervisor provider
dedelulu --provider google gemini -p "refactor the auth module"
```

### What happens when you wrap gemini:

- **Goal Extraction**: `dedelulu` automatically extracts your task from the `-p` flag.
- **Auto-Approval**: Common prompts like `Allow [tool] to run? [Y/n]` are automatically answered.
- **Hooks**: `dedelulu` installs temporary hooks in `.gemini/settings.local.json` to monitor tool usage and provide context to the supervisor.
- **Supervisor**: If a provider is set, the supervisor will monitor your `gemini` agent and intervene if it goes off-rails.

---

## Why use Gemini?

- **Speed:** `gemini-2.5-flash` is extremely fast, making supervisor checks almost instant.
- **Context:** Gemini models have a massive context window, which is useful for deep codebase analysis.
- **Cost:** Often cheaper for high-frequency supervisor checks.
