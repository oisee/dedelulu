#!/usr/bin/env python3
"""
dedelulu - Autonomous supervisor for interactive CLI agents.

Wraps any command in a PTY, passes output straight through,
detects when the program is waiting for input (via idle timeout),
and auto-responds with the right answer. No TUI wrapper, no
rendering conflicts — just a transparent pipe with a brain.

Usage:
    dedelulu claude "do the thing"
    dedelulu --idle 5 --provider anthropic npm install
    dedelulu --dry-run claude "refactor everything"
"""

import os
import pty
import sys
import re
import signal
import select
import time
import json
import struct
import fcntl
import termios
import argparse
import tempfile
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, Tuple


# =============================================================================
# ANSI stripping
# =============================================================================

# Matches CSI sequences, OSC sequences, and other escape codes
_ANSI_RE = re.compile(r"""
    \x1b        # ESC
    (?:
        \[          # CSI
        [0-9;?]*    # parameter bytes
        [A-Za-z~]   # final byte
    |
        \]          # OSC
        .*?         # payload
        (?:\x07|\x1b\\)  # ST (BEL or ESC\)
    |
        [()][AB012]  # charset selection
    |
        [=>Nc]       # other short sequences
    )
""", re.VERBOSE | re.DOTALL)

# Control characters (except newline/tab)
_CTRL_RE = re.compile(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]')


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes and control chars from text."""
    # Replace cursor-forward sequences with spaces (Claude Code uses these as spacing)
    text = re.sub(r'\x1b\[(\d+)C', lambda m: ' ' * int(m.group(1)), text)
    text = _ANSI_RE.sub('', text)
    text = _CTRL_RE.sub('', text)
    return text


# =============================================================================
# Fast pattern matching — no LLM needed for these
# =============================================================================

# Each entry: (compiled_regex, response_to_send)
# Checked against ANSI-stripped, lowercased last ~10 lines
FAST_PATTERNS = [
    # ── Claude Code specific ──
    # Claude Code permission prompts: "❯ 1. Yes" with "Enter to confirm · Esc to cancel"
    # The ❯ means option 1 is already highlighted — just press Enter
    (re.compile(r'enter\s+to\s+confirm\s*.*esc\s+to\s+cancel', re.IGNORECASE), ''),
    (re.compile(r'esc\s+to\s+cancel\s*.*enter\s+to\s+confirm', re.IGNORECASE), ''),
    # Claude Code: "Do you want to proceed?" with numbered options
    (re.compile(r'do you want to proceed\?', re.IGNORECASE), ''),
    (re.compile(r'do you want to execute', re.IGNORECASE), ''),
    # Claude Code: trust folder prompt
    (re.compile(r'yes,?\s+i\s+trust\s+this', re.IGNORECASE), ''),
    # Claude Code: tab to amend
    (re.compile(r'tab\s+to\s+amend', re.IGNORECASE), ''),

    # ── Press enter ──
    (re.compile(r'press\s+enter', re.IGNORECASE), ''),

    # ── Standard y/n prompts ──
    (re.compile(r'\[Y/n\]'), 'y'),
    (re.compile(r'\[y/N\]'), 'y'),
    (re.compile(r'\(y/n\)', re.IGNORECASE), 'y'),
    (re.compile(r'\(yes/no\)', re.IGNORECASE), 'yes'),
    (re.compile(r'(?:continue|proceed|confirm)\?\s*$', re.IGNORECASE), 'y'),
    (re.compile(r'are you sure', re.IGNORECASE), 'y'),

    # ── Esc to cancel (generic — current selection is correct) ──
    (re.compile(r'esc\s+to\s+cancel', re.IGNORECASE), ''),

    # ── npm/yarn ──
    (re.compile(r'ok to proceed\?', re.IGNORECASE), 'y'),
    (re.compile(r'is this ok\?', re.IGNORECASE), 'y'),

    # ── Git ──
    (re.compile(r'do you wish to continue', re.IGNORECASE), 'y'),

    # ── pip ──
    (re.compile(r'proceed\s*\(y/n\)', re.IGNORECASE), 'y'),

    # ── Generic numbered menus ──
    (re.compile(r'>\s*1\.\s*yes', re.IGNORECASE), ''),  # already selected, just Enter
    (re.compile(r'(?:choice|select|option)\s*:\s*$', re.IGNORECASE), '1'),
]


def fast_match(text: str) -> Optional[str]:
    """Try to match against known patterns. Returns response or None.
    Only checks the LAST 10 lines to avoid false matches from old output."""
    lines = text.strip().split('\n')
    tail = '\n'.join(lines[-10:])
    for pattern, response in FAST_PATTERNS:
        if pattern.search(tail):
            return response
    return None


# =============================================================================
# Heuristic: does this look like the program is waiting for input?
# =============================================================================

# Patterns that suggest a prompt (even if we don't know the exact answer)
_PROMPT_HINTS = [
    re.compile(r'\?\s*$', re.MULTILINE),
    re.compile(r':\s*$', re.MULTILINE),
    re.compile(r'\[.*\]\s*$', re.MULTILINE),
    re.compile(r'>\s*$', re.MULTILINE),
    re.compile(r'choice', re.IGNORECASE),
    re.compile(r'select', re.IGNORECASE),
    re.compile(r'enter\s', re.IGNORECASE),
    re.compile(r'input', re.IGNORECASE),
    re.compile(r'password', re.IGNORECASE),
    re.compile(r'approve', re.IGNORECASE),
]


def looks_like_prompt(text: str) -> bool:
    """Heuristic: does the tail of output look like it's waiting for input?"""
    # Check last 5 lines
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if not lines:
        return False
    tail = '\n'.join(lines[-5:])
    return any(p.search(tail) for p in _PROMPT_HINTS)


# =============================================================================
# LLM fallback — only called for ambiguous prompts
# =============================================================================

def _normalize_llm_response(response: str) -> str:
    """Normalize LLM response — handle escape sequences, special keys, quotes."""
    r = response.strip()
    # Strip wrapping quotes: 'y' → y, "no" → no
    if len(r) >= 2 and r[0] == r[-1] and r[0] in ('"', "'", '`'):
        r = r[1:-1]
    # Backslash escape sequences (LLM writes \n, we send the real char)
    _ESCAPES = {
        '\\n': '', '\\r': '', '\\t': '\t', '\\e': '\x1b',
        '\\x03': '\x03', '\\x1b': '\x1b',
    }
    if r in _ESCAPES:
        return _ESCAPES[r]
    upper = r.upper()
    # ENTER variants
    if upper in ('ENTER', '<ENTER>', '[ENTER]', 'RETURN',
                 'PRESS ENTER', '(ENTER)', '(PRESS ENTER)', ''):
        return ''
    # SKIP variants
    if upper in ('SKIP', '<SKIP>', '[SKIP]', 'N/A'):
        return '__SKIP__'
    # ESC variants
    if upper in ('ESC', 'ESCAPE', '<ESC>', '[ESC]'):
        return '\x1b'
    # TAB
    if upper in ('TAB', '<TAB>', '[TAB]'):
        return '\t'
    # Ctrl+C
    if upper in ('CTRL+C', 'CTRL-C', '^C'):
        return '\x03'
    return r


def ask_llm_prompt(context: str, provider: str = 'anthropic',
                   model: str = None, api_key: str = None,
                   system_instructions: str = None) -> Optional[str]:
    """Ask a cheap LLM what to type at a prompt. Returns the response string or None."""

    extra = ''
    if system_instructions:
        extra = f"\nADDITIONAL INSTRUCTIONS FROM USER:\n{system_instructions}\n"

    prompt = ("You are an autonomous supervisor for a CLI agent. "
              "The program below is waiting for user input. "
              "Decide what to type to let it continue productively.\n\n"
              "RULES:\n"
              "- Respond with ONLY the exact text to type (no quotes, no explanation)\n"
              "- For yes/no: respond y or yes\n"
              "- For numbered menus: respond with the number (e.g. 1)\n"
              "- For press enter: respond \\n\n"
              "- For escape/cancel: respond \\e\n"
              "- For tab: respond \\t\n"
              "- For Ctrl+C: respond \\x03\n"
              "- For text input: respond with a reasonable short answer\n"
              "- If the program seems stuck/looping (not actually waiting for input), respond SKIP\n"
              "- If the program is asking for something dangerous (delete production data, etc), respond SKIP\n"
              + extra +
              f"\nPROGRAM OUTPUT (last 30 lines):\n{context}\n\nYOUR INPUT:")

    try:
        if provider == 'claude-cli':
            return _ask_claude_cli(prompt, model)
        elif provider == 'anthropic':
            return _ask_anthropic(prompt, model or 'claude-haiku-4-5-20251001',
                                  api_key or os.getenv('ANTHROPIC_API_KEY'))
        elif provider == 'ollama':
            return _ask_ollama(prompt, model or 'ministral-3:8b')
        elif provider == 'openai':
            return _ask_openai(prompt, model or 'gpt-4o-mini',
                               api_key or os.getenv('OPENAI_API_KEY'))
        elif provider == 'azure':
            return _ask_azure(prompt, model or 'gpt-4o',
                              api_key or os.getenv('AZURE_OPENAI_API_KEY'))
        else:
            return None
    except Exception as e:
        log_event('llm_error', {'error': str(e), 'provider': provider})
        return None


@dataclass
class SupervisorVerdict:
    status: str       # "on_track", "stuck", "off_rails", "error_loop", "idle"
    action: str       # "continue", "interrupt", "message"
    message: str      # what to tell/type to the agent (if action != "continue")
    reasoning: str    # short explanation for the log


def ask_llm_supervise(goal: str, recent_output: str, provider: str = 'anthropic',
                      model: str = None, api_key: str = None,
                      system_instructions: str = None,
                      consecutive_stuck: int = 0,
                      intervention_history: list = None) -> Optional[SupervisorVerdict]:
    """Supervisor LLM: assess whether the agent is on track toward the goal."""

    system_extra = ''
    if system_instructions:
        system_extra = f"\nADDITIONAL INSTRUCTIONS FROM USER:\n{system_instructions}\n"

    # First stuck detection → gentle message. Second+ → interrupt.
    if consecutive_stuck == 0:
        stuck_rule = (
            '- "stuck" + "message": agent seems stuck or not making progress. '
            'Send a helpful message to redirect it. Put the redirect in "message". '
            'Do NOT interrupt on first detection — give the agent a chance to self-correct.'
        )
        error_rule = (
            '- "error_loop" + "message": agent keeps hitting the same error. '
            'Send a message suggesting a different approach. Do NOT interrupt yet.'
        )
    else:
        stuck_rule = (
            '- "stuck" + "interrupt": agent is STILL stuck after a previous redirect message. '
            'Interrupt with Ctrl+C and provide a corrective message.'
        )
        error_rule = (
            '- "error_loop" + "interrupt": agent keeps hitting the same error despite redirection. '
            'Interrupt with Ctrl+C and suggest a different approach in "message".'
        )

    history_ctx = ''
    if intervention_history:
        lines = []
        for h in intervention_history[-7:]:  # last 7 entries
            if h.get('msg'):
                lines.append(f"  [{h['ts']}] you said: \"{h['msg']}\" (status: {h.get('status', '?')})")
            else:
                lines.append(f"  [{h['ts']}] checked: {h.get('status', '?')} — {h.get('reasoning', '')}")
        history_ctx = "\nYOUR PREVIOUS CHECKS & MESSAGES (do NOT repeat — try a different angle each time):\n" + '\n'.join(lines) + "\n"

    prompt = f"""You are supervising an AI coding agent (Claude Code). The user gave it a task and you need to check if it's on track.

GOAL:
{goal}
{system_extra}{history_ctx}
AGENT CONTEXT (activity timeline + recent terminal output):
{recent_output}

Assess the agent's status and respond in EXACTLY this JSON format (no other text):
{{"status": "<on_track|stuck|off_rails|error_loop|idle|uncertain>", "action": "<continue|interrupt|message|escalate>", "message": "<text to type if action is message, or question for human if escalate, or empty>", "reasoning": "<1 sentence explanation>"}}

RULES:
- "on_track" + "continue": agent is making progress toward the goal. This is the most common case.
{stuck_rule}
- "off_rails" + "message": agent is working on something unrelated to the goal. message should redirect it.
{error_rule}
- "idle" + "continue": agent finished or is waiting for a new prompt from the user. Do nothing.
- "uncertain" + "escalate": you're not sure if this is right or wrong, or the agent is about to do something risky (destructive operations, major architectural changes, unclear requirements). Ask the human. Put your question in "message".
- Be conservative — only interrupt if clearly stuck/wrong. False positives are worse than being patient.
- Use "escalate" when the situation is ambiguous or risky — let the human decide.
- If you see the agent actively writing code, running tests, reading files — that's "on_track".
- If the agent is idle/waiting at a prompt and the goal is open-ended or unclear — that's "idle" + "continue". Do NOT nag.
- If you already sent a message and the agent acknowledged it — do NOT repeat yourself. That's "on_track" or "idle".
- NEVER repeat the same message. Each intervention must be DIFFERENT — try a new angle, suggest a concrete next step, or ask a specific question. Vary your tone and approach like a real colleague would.
- IMPORTANT: when action is "message", always provide a helpful, specific redirect in "message" — don't leave it empty.
- Be warm and human, not robotic. Talk like a helpful colleague, not a system alert. No bureaucratic language. No referencing goal IDs, session IDs, or "original goal". Just be natural.

JSON:"""

    try:
        if provider == 'claude-cli':
            raw = _ask_claude_cli(prompt, model)
        elif provider == 'anthropic':
            raw = _ask_anthropic(prompt, model or 'claude-haiku-4-5-20251001',
                                 api_key or os.getenv('ANTHROPIC_API_KEY'))
        elif provider == 'ollama':
            raw = _ask_ollama(prompt, model or 'ministral-3:8b')
        elif provider == 'openai':
            raw = _ask_openai(prompt, model or 'gpt-4o-mini',
                              api_key or os.getenv('OPENAI_API_KEY'))
        elif provider == 'azure':
            raw = _ask_azure(prompt, model or 'gpt-4o-mini',
                             api_key or os.getenv('AZURE_OPENAI_API_KEY'))
        else:
            return None

        if not raw:
            return None

        # Parse JSON from response (handle markdown code blocks)
        raw = raw.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```\w*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)
        data = json.loads(raw)
        return SupervisorVerdict(
            status=data.get('status', 'on_track'),
            action=data.get('action', 'continue'),
            message=data.get('message', ''),
            reasoning=data.get('reasoning', '')
        )
    except (json.JSONDecodeError, KeyError) as e:
        log_event('supervisor_parse_error', {'error': str(e), 'raw': raw[:200] if raw else ''})
        return None
    except Exception as e:
        log_event('supervisor_error', {'error': str(e)})
        return None


def _ask_anthropic(prompt: str, model: str, api_key: str) -> Optional[str]:
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            temperature=0.0,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return resp.content[0].text.strip()
    except ImportError:
        # Fallback to raw HTTP
        import urllib.request
        data = json.dumps({
            'model': model,
            'max_tokens': 200,
            'temperature': 0.0,
            'messages': [{'role': 'user', 'content': prompt}]
        }).encode()
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=data,
            headers={
                'Content-Type': 'application/json',
                'X-API-Key': api_key,
                'anthropic-version': '2023-06-01'
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            return body['content'][0]['text'].strip()


def _ask_claude_cli(prompt: str, model: str = None) -> Optional[str]:
    """Use 'claude -p' (Claude Code CLI) as the LLM. No API key needed — uses Max subscription."""
    import subprocess
    cmd = ['claude', '-p', prompt]
    if model:
        cmd.extend(['--model', model])
    env = os.environ.copy()
    # Allow nested claude invocation
    env.pop('CLAUDECODE', None)
    env.pop('CLAUDE_CODE', None)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=env
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        log_event('claude_cli_error', {'error': str(e)})
        return None


def _normalize_ollama_host(host: str) -> str:
    """Normalize OLLAMA_HOST to a full URL — handles bare IP, host:port, etc."""
    host = host.strip().rstrip('/')
    if not host:
        return 'http://localhost:11434'
    if not host.startswith(('http://', 'https://')):
        host = 'http://' + host
    # Add default port if none specified
    from urllib.parse import urlparse
    parsed = urlparse(host)
    if not parsed.port:
        host = host + ':11434'
    return host


def _ask_ollama(prompt: str, model: str) -> Optional[str]:
    import urllib.request
    host = _normalize_ollama_host(os.getenv('OLLAMA_HOST', ''))
    # Suppress thinking for qwen3 models — append /no_think
    effective_prompt = prompt
    if 'qwen3' in model.lower():
        effective_prompt = prompt + ' /no_think'
    data = json.dumps({
        'model': model,
        'prompt': effective_prompt,
        'stream': False,
        'options': {'temperature': 0.0, 'num_predict': 300}
    }).encode()
    req = urllib.request.Request(
        f'{host}/api/generate',
        data=data,
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
        result = body.get('response', '').strip()
        # Strip thinking tags from reasoning models
        if '<think>' in result:
            result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL).strip()
        return result or None


def _ask_openai(prompt: str, model: str, api_key: str) -> Optional[str]:
    if not api_key:
        return None
    import urllib.request
    data = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.0,
        'max_tokens': 200
    }).encode()
    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=data,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
        return body['choices'][0]['message']['content'].strip()


def _ask_azure(prompt: str, model: str, api_key: str) -> Optional[str]:
    """Azure OpenAI API.

    Env vars:
        AZURE_OPENAI_API_KEY    — API key
        AZURE_OPENAI_ENDPOINT   — e.g. https://myinstance.openai.azure.com
        AZURE_OPENAI_API_VERSION — e.g. 2024-12-01-preview (default)
        AZURE_OPENAI_DEPLOYMENT — deployment name (default: same as model)
    """
    if not api_key:
        return None
    import urllib.request
    endpoint = os.getenv('AZURE_OPENAI_ENDPOINT', '').rstrip('/')
    if not endpoint:
        log_event('azure_error', {'error': 'AZURE_OPENAI_ENDPOINT not set'})
        return None
    api_version = os.getenv('AZURE_OPENAI_API_VERSION', '2024-12-01-preview')
    deployment = os.getenv('AZURE_OPENAI_DEPLOYMENT', model)

    url = f'{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}'
    # Reasoning models (o-series) don't support temperature, use max_completion_tokens
    is_reasoning = deployment.startswith('o')
    payload = {
        'messages': [{'role': 'user', 'content': prompt}],
        'max_completion_tokens': 300,
    }
    if not is_reasoning:
        payload['temperature'] = 0.0
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/json',
            'api-key': api_key
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
        return body['choices'][0]['message']['content'].strip()


# =============================================================================
# Logging
# =============================================================================

_log_file = None
_log_ipc = None  # IPC instance for forwarding events to foreman


def init_log(path: str):
    global _log_file
    _log_file = open(path, 'a')


def set_log_ipc(ipc):
    global _log_ipc
    _log_ipc = ipc


def log_event(event: str, data: dict = None):
    entry = {
        'ts': datetime.now().isoformat(),
        'event': event,
        **(data or {})
    }
    if _log_file:
        _log_file.write(json.dumps(entry) + '\n')
        _log_file.flush()
    if _log_ipc:
        try:
            _log_ipc.send_event(event, **(data or {}))
        except Exception:
            pass


# =============================================================================
# IPC — communication between worker (PTY) and foreman (status pane)
# =============================================================================

class IPC:
    """File-based IPC between worker and foreman processes.

    Directory layout:
        {ipc_dir}/events.jsonl  — worker appends, foreman tails
        {ipc_dir}/input.jsonl   — foreman appends, worker polls
        {ipc_dir}/pid           — worker PID (for foreman to check liveness)
    """

    def __init__(self, ipc_dir: str):
        self.ipc_dir = ipc_dir
        self.events_path = os.path.join(ipc_dir, 'events.jsonl')
        self.input_path = os.path.join(ipc_dir, 'input.jsonl')
        self.pid_path = os.path.join(ipc_dir, 'pid')
        self._input_pos = 0  # file position for polling input

    @classmethod
    def create(cls) -> 'IPC':
        """Create a new IPC directory."""
        ipc_dir = tempfile.mkdtemp(prefix='dedelulu_')
        ipc = cls(ipc_dir)
        # Touch files
        open(ipc.events_path, 'w').close()
        open(ipc.input_path, 'w').close()
        with open(ipc.pid_path, 'w') as f:
            f.write(str(os.getpid()))
        return ipc

    @classmethod
    def connect(cls, ipc_dir: str) -> 'IPC':
        """Connect to existing IPC directory."""
        ipc = cls(ipc_dir)
        # Start reading input from end (only new messages)
        if os.path.exists(ipc.input_path):
            ipc._input_pos = os.path.getsize(ipc.input_path)
        return ipc

    def send_event(self, event: str, **data):
        """Worker → Foreman: append event."""
        entry = {'ts': datetime.now().strftime('%H:%M:%S'), 'event': event, **data}
        with open(self.events_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def poll_input(self) -> Optional[dict]:
        """Worker polls for foreman responses. Returns newest message or None."""
        try:
            size = os.path.getsize(self.input_path)
            if size <= self._input_pos:
                return None
            with open(self.input_path) as f:
                f.seek(self._input_pos)
                lines = f.readlines()
                self._input_pos = f.tell()
            # Return last message
            for line in reversed(lines):
                line = line.strip()
                if line:
                    return json.loads(line)
        except Exception:
            pass
        return None

    def send_input(self, message: str, **data):
        """Foreman → Worker: send response."""
        entry = {'ts': datetime.now().strftime('%H:%M:%S'), 'message': message, **data}
        with open(self.input_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def worker_alive(self) -> bool:
        """Check if worker process is still running."""
        try:
            with open(self.pid_path) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError):
            return False

    def cleanup(self):
        """Remove IPC directory."""
        import shutil
        try:
            shutil.rmtree(self.ipc_dir, ignore_errors=True)
        except Exception:
            pass


# =============================================================================
# Session — multi-worker state on disk (shared by foreman + workers)
# =============================================================================

@dataclass
class WorkerSpec:
    name: str
    directory: str
    task: str
    ipc_dir: str = ''


class Session:
    """Persistent session state. Created automatically, even for single worker.

    Layout:
        {session_dir}/session.json    — worker specs, system instructions
        {session_dir}/workers/{name}/ — per-worker IPC dirs
    """

    def __init__(self, session_dir: str):
        self.session_dir = session_dir
        self.session_file = os.path.join(session_dir, 'session.json')
        self.workers_dir = os.path.join(session_dir, 'workers')
        self.workers: dict[str, WorkerSpec] = {}
        self.system_instructions: str = ''
        self.extra_args: list[str] = []  # args to pass to new workers

    @classmethod
    def create(cls, name: str, directory: str, task: str,
               system_instructions: str = '', extra_args: list[str] = None) -> 'Session':
        session_dir = tempfile.mkdtemp(prefix='dedelulu_session_')
        session = cls(session_dir)
        os.makedirs(session.workers_dir, exist_ok=True)
        session.system_instructions = system_instructions or ''
        session.extra_args = extra_args or []
        session.add_worker(name, directory, task)
        return session

    @classmethod
    def load(cls, session_dir: str) -> 'Session':
        session = cls(session_dir)
        with open(session.session_file) as f:
            data = json.load(f)
        for wd in data.get('workers', []):
            session.workers[wd['name']] = WorkerSpec(**wd)
        session.system_instructions = data.get('system_instructions', '')
        session.extra_args = data.get('extra_args', [])
        return session

    def save(self):
        data = {
            'workers': [
                {'name': w.name, 'directory': w.directory,
                 'task': w.task, 'ipc_dir': w.ipc_dir}
                for w in self.workers.values()
            ],
            'system_instructions': self.system_instructions,
            'extra_args': self.extra_args,
        }
        with open(self.session_file, 'w') as f:
            json.dump(data, f, indent=2)

    def add_worker(self, name: str, directory: str, task: str) -> WorkerSpec:
        """Register a new worker — creates IPC dir, saves session."""
        w_ipc_dir = os.path.join(self.workers_dir, name)
        os.makedirs(w_ipc_dir, exist_ok=True)
        for fname in ('events.jsonl', 'input.jsonl', 'pid'):
            path = os.path.join(w_ipc_dir, fname)
            if not os.path.exists(path):
                open(path, 'w').close()
        w = WorkerSpec(name=name, directory=directory, task=task, ipc_dir=w_ipc_dir)
        self.workers[name] = w
        self.save()
        return w

    def get_ipc(self, worker_name: str) -> IPC:
        w = self.workers[worker_name]
        return IPC.connect(w.ipc_dir)

    def send_to_worker(self, worker_name: str, message: str, sender: str = 'foreman'):
        ipc = self.get_ipc(worker_name)
        ipc.send_input(message, sender=sender)

    def broadcast(self, message: str, sender: str = 'foreman'):
        for name in self.workers:
            self.send_to_worker(name, message, sender=sender)


# =============================================================================
# Foreman — interactive status pane (manages all workers in session)
# =============================================================================

def run_foreman(session_dir: str):
    """Foreman process: shows events from all workers, handles commands."""
    session = Session.load(session_dir)

    # Track file positions for each worker's events
    events_pos: dict[str, int] = {}
    for name in session.workers:
        events_pos[name] = 0

    # Colors
    C_RESET = '\033[0m'
    C_GRAY = '\033[90m'
    C_CYAN = '\033[36m'
    C_GREEN = '\033[32m'
    C_YELLOW = '\033[1;33m'
    C_RED = '\033[1;31m'
    C_BOLD = '\033[1m'

    W_COLORS = ['\033[34m', '\033[35m', '\033[36m', '\033[33m',
                '\033[32m', '\033[91m', '\033[94m', '\033[95m']
    worker_color: dict[str, str] = {}
    for i, name in enumerate(session.workers):
        worker_color[name] = W_COLORS[i % len(W_COLORS)]

    n = len(session.workers)
    print(f"{C_BOLD}─── dedelulu foreman ─── {n} worker{'s' if n != 1 else ''} ───{C_RESET}")
    for name, w in session.workers.items():
        wc = worker_color[name]
        print(f"  {wc}[{name}]{C_RESET} {w.directory} — {w.task[:60]}")
    print()
    print(f"{C_GRAY}Commands: /send <worker> msg  /add name:dir:task  /system new instructions")
    print(f"          /broadcast msg  /status  /help{C_RESET}")
    print()

    pending_escalation = None  # (worker_name,) or None
    import select as sel

    def _print_event(worker_name, ev):
        ts = ev.get('ts', '')
        event = ev.get('event', '')
        wc = worker_color.get(worker_name, '')
        tag = f"{wc}[{worker_name}]{C_RESET}" if len(session.workers) > 1 else ''

        if event == 'hook_approve':
            tool = ev.get('tool', '?')
            print(f"  {C_GRAY}{ts}{C_RESET} {tag} {C_GREEN}✓{C_RESET} {tool}")
        elif event == 'respond':
            src = ev.get('source', '?')
            resp = ev.get('response', '')
            display = repr(resp) if resp else '↵'
            count = ev.get('count', '?')
            print(f"  {C_GRAY}{ts}{C_RESET} {tag} {C_CYAN}#{count}{C_RESET} sent {display} ({src})")
        elif event == 'supervise':
            status = ev.get('status', '?')
            reasoning = ev.get('reasoning', '')
            if status == 'on_track':
                print(f"  {C_GRAY}{ts}{C_RESET} {tag} {C_GREEN}●{C_RESET} {C_GRAY}{reasoning}{C_RESET}")
            else:
                print(f"  {C_GRAY}{ts}{C_RESET} {tag} {C_YELLOW}● {status}{C_RESET} {reasoning}")
        elif event == 'escalate':
            question = ev.get('question', ev.get('reasoning', '?'))
            print(f"\n  {C_YELLOW}{'─' * 50}")
            print(f"  ⚠  {tag} {C_YELLOW}NEEDS YOUR INPUT")
            print(f"  {question}")
            print(f"  {'─' * 50}{C_RESET}\n")
            print('\a', end='', flush=True)
        elif event == 'intervene':
            msg = ev.get('message', '')
            print(f"  {C_GRAY}{ts}{C_RESET} {tag} {C_RED}▶ intervened{C_RESET} {msg[:60]}")
        elif event in ('start', 'exit', 'hooks_installed', 'hooks_uninstalled'):
            print(f"  {C_GRAY}{ts}{C_RESET} {tag} {C_GRAY}[{event}]{C_RESET}")
        else:
            print(f"  {C_GRAY}{ts}{C_RESET} {tag} {C_GRAY}{event}{C_RESET}")

    try:
        while True:
            # Check if any workers alive
            any_alive = False
            for name, w in session.workers.items():
                ipc = IPC(w.ipc_dir)
                if ipc.worker_alive():
                    any_alive = True
                    break
            if not any_alive:
                print(f"\n{C_GRAY}All workers exited.{C_RESET}")
                break

            # Read new events from all workers
            for name, w in session.workers.items():
                events_path = os.path.join(w.ipc_dir, 'events.jsonl')
                if name not in events_pos:
                    events_pos[name] = 0
                try:
                    size = os.path.getsize(events_path)
                    if size <= events_pos[name]:
                        continue
                    with open(events_path) as f:
                        f.seek(events_pos[name])
                        new_lines = f.readlines()
                        events_pos[name] = f.tell()
                    for line in new_lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if ev.get('event') == 'escalate':
                            pending_escalation = name
                        _print_event(name, ev)
                except Exception:
                    pass

            # Check for user input
            try:
                r, _, _ = sel.select([sys.stdin], [], [], 0.3)
                if r:
                    raw = sys.stdin.readline().strip()
                    if not raw:
                        pass
                    elif raw.startswith('/'):
                        _foreman_command(raw, session, session_dir, events_pos,
                                         worker_color, W_COLORS,
                                         C_RESET, C_CYAN, C_GREEN, C_YELLOW, C_RED, C_GRAY, C_BOLD)
                    elif pending_escalation:
                        session.send_to_worker(pending_escalation, raw)
                        wc = worker_color.get(pending_escalation, '')
                        print(f"  {C_CYAN}→ {wc}[{pending_escalation}]{C_RESET} {raw}\n")
                        pending_escalation = None
                    elif len(session.workers) == 1:
                        # Single worker — send directly
                        name = next(iter(session.workers))
                        session.send_to_worker(name, raw)
                        print(f"  {C_CYAN}→ sent{C_RESET}")
                    else:
                        print(f"  {C_GRAY}use /send <worker> msg  or /help{C_RESET}")
            except Exception:
                time.sleep(0.3)

    except KeyboardInterrupt:
        print(f"\n{C_GRAY}Foreman stopped.{C_RESET}")


def _foreman_command(raw: str, session: Session, session_dir: str,
                     events_pos: dict, worker_color: dict, W_COLORS: list,
                     C_RESET, C_CYAN, C_GREEN, C_YELLOW, C_RED, C_GRAY, C_BOLD):
    """Handle foreman slash commands."""
    parts = raw.split(None, 2)
    cmd = parts[0].lower()

    if cmd == '/help':
        print(f"""
  {C_BOLD}Foreman commands:{C_RESET}
    /send <worker> message        — send message to worker
    /broadcast message            — send to all workers
    /add name:dir[:task]          — spawn a new worker (no task = interactive)
    /system new instructions      — change system instructions live
    /status                       — worker status overview
    /log <worker>                 — last 15 events from worker
    /help                         — this help

  {C_GRAY}Without /, text goes to the single worker (or pending escalation).{C_RESET}
""")

    elif cmd == '/send' and len(parts) >= 3:
        target = parts[1]
        msg = parts[2]
        if target in session.workers:
            session.send_to_worker(target, msg)
            wc = worker_color.get(target, '')
            print(f"  → {wc}[{target}]{C_RESET} delivered")
        else:
            print(f"  {C_RED}unknown worker: {target}{C_RESET}")

    elif cmd == '/broadcast' and len(parts) >= 2:
        msg = ' '.join(parts[1:])
        session.broadcast(msg)
        print(f"  → all {len(session.workers)} workers delivered")

    elif cmd == '/add' and len(parts) >= 2:
        spec = parts[1] if len(parts) == 2 else parts[1] + ':' + parts[2]
        spec_parts = spec.split(':', 2)
        if len(spec_parts) == 2:
            name, directory = spec_parts
            task = ''  # interactive — user types prompt themselves
        elif len(spec_parts) == 3:
            name, directory, task = spec_parts
        else:
            print(f"  {C_RED}usage: /add name:dir[:task]{C_RESET}")
            return
        directory = os.path.expanduser(directory)
        if name in session.workers:
            print(f"  {C_RED}worker '{name}' already exists{C_RESET}")
            return
        if not os.path.isdir(directory):
            print(f"  {C_RED}directory not found: {directory}{C_RESET}")
            return

        # Register worker in session
        w = session.add_worker(name, directory, task)
        events_pos[name] = 0
        worker_color[name] = W_COLORS[len(worker_color) % len(W_COLORS)]

        # Spawn tmux pane
        _spawn_worker_pane(w, session, session_dir)

        wc = worker_color[name]
        mode = 'interactive' if not task else task[:60]
        print(f"  {C_GREEN}✓{C_RESET} spawned {wc}[{name}]{C_RESET} → {directory}")
        print(f"    {C_GRAY}{mode}{C_RESET}")

    elif cmd == '/system' and len(parts) >= 2:
        new_system = ' '.join(parts[1:])
        session.system_instructions = new_system
        session.save()
        # Notify all workers by writing to their state
        print(f"  {C_GREEN}✓{C_RESET} system instructions updated")
        print(f"  {C_GRAY}{new_system[:80]}{C_RESET}")

    elif cmd == '/status':
        # Reload session to pick up new workers
        session_fresh = Session.load(session.session_dir)
        print(f"  {C_BOLD}{'worker':<12} {'status':<10} {'task'}{C_RESET}")
        print(f"  {'─'*12} {'─'*10} {'─'*30}")
        for name, w in session_fresh.workers.items():
            ipc = IPC(w.ipc_dir)
            alive = ipc.worker_alive()
            status = f"{C_GREEN}● alive{C_RESET}" if alive else f"{C_GRAY}○ exited{C_RESET}"
            wc = worker_color.get(name, '')
            print(f"  {wc}{name:<12}{C_RESET} {status:<20} {w.task[:40]}")
        if session_fresh.system_instructions:
            print(f"\n  {C_GRAY}system: {session_fresh.system_instructions[:60]}{C_RESET}")

    elif cmd == '/log' and len(parts) >= 2:
        worker_name = parts[1]
        if worker_name not in session.workers:
            print(f"  {C_RED}unknown worker: {worker_name}{C_RESET}")
            return
        w = session.workers[worker_name]
        events_path = os.path.join(w.ipc_dir, 'events.jsonl')
        try:
            with open(events_path) as f:
                lines = f.readlines()
            for line in lines[-15:]:
                line = line.strip()
                if line:
                    ev = json.loads(line)
                    ts = ev.get('ts', '')
                    event = ev.get('event', '')
                    wc = worker_color.get(worker_name, '')
                    print(f"  {C_GRAY}{ts}{C_RESET} {wc}[{worker_name}]{C_RESET} {event}")
        except Exception as e:
            print(f"  {C_RED}error: {e}{C_RESET}")

    else:
        print(f"  {C_GRAY}unknown command. /help for list{C_RESET}")


def _spawn_worker_pane(w: WorkerSpec, session: Session, session_dir: str):
    """Spawn a new tmux pane running dedelulu for this worker."""
    import subprocess
    import shutil
    tmux = shutil.which('tmux')
    if not tmux:
        return
    dedelulu_bin = os.path.abspath(__file__)
    python = sys.executable

    cmd_parts = [
        python, dedelulu_bin,
        '--ipc-dir', w.ipc_dir,
        '--session-dir', session_dir,
        '--no-tmux',
    ] + session.extra_args + ['--', 'claude']
    if w.task:
        cmd_parts.append(w.task)
    worker_cmd = f'cd {_shell_quote(w.directory)} && {" ".join(_shell_quote(a) for a in cmd_parts)}'

    # Split from the foreman pane (current pane) — add worker to the left
    subprocess.run([
        tmux, 'split-window', '-h', '-b',
        '-l', '70%',
        'bash', '-c', worker_cmd,
    ])
    # Focus back to foreman (rightmost pane)
    subprocess.run([tmux, 'select-pane', '-l'])


# =============================================================================
# tmux launcher — auto-split terminal
# =============================================================================

def launch_tmux(args_list: list[str], session_dir: str, ipc_dir: str):
    """Launch dedelulu in a tmux session with worker (left) + foreman (right)."""
    import subprocess
    import shutil

    tmux = shutil.which('tmux')
    if not tmux:
        print("[dedelulu] tmux not found — run 'dedelulu --foreman' in another tab")
        print(f"  session: {session_dir}")
        return None

    session_name = f'dedelulu-{os.getpid()}'
    dedelulu_bin = os.path.abspath(__file__)
    python = sys.executable

    # Build worker command (pass through all original args + --ipc-dir + --session-dir)
    worker_args = [python, dedelulu_bin,
                   '--ipc-dir', ipc_dir,
                   '--session-dir', session_dir] + args_list
    worker_cmd = ' '.join(_shell_quote(a) for a in worker_args)

    # Foreman command
    foreman_cmd = f'{_shell_quote(python)} {_shell_quote(dedelulu_bin)} --foreman {_shell_quote(session_dir)}'

    # Create tmux session: left pane = worker, right 20% = foreman
    subprocess.run([
        tmux, 'new-session', '-d', '-s', session_name,
        '-x', '200', '-y', '50',
        worker_cmd,
    ])
    subprocess.run([
        tmux, 'split-window', '-h', '-t', session_name,
        '-l', '20%',
        foreman_cmd,
    ])
    # Focus on left pane (worker/claude)
    subprocess.run([tmux, 'select-pane', '-t', f'{session_name}:.0'])

    # Attach
    os.execvp(tmux, [tmux, 'attach-session', '-t', session_name])


def _launch_foreman_pane(session_dir: str):
    """When already inside tmux, split a 20% right pane running the foreman."""
    import subprocess
    import shutil
    tmux = shutil.which('tmux')
    if not tmux:
        return
    dedelulu_bin = os.path.abspath(__file__)
    python = sys.executable
    foreman_cmd = f'{_shell_quote(python)} {_shell_quote(dedelulu_bin)} --foreman {_shell_quote(session_dir)}'
    # Split current pane: right 20% = foreman
    subprocess.run([
        tmux, 'split-window', '-h', '-l', '20%', foreman_cmd,
    ])
    # Focus back on the left pane (the main worker)
    subprocess.run([tmux, 'select-pane', '-L'])


def _shell_quote(s: str) -> str:
    """Shell-quote a string."""
    if not s:
        return "''"
    import shlex
    return shlex.quote(s)


# =============================================================================
# Rail detection — is the agent going off track?
# =============================================================================

class RailDetector:
    """Detect if the agent is stuck in a loop or going off-rails."""

    def __init__(self, max_responses_per_minute: int = 10,
                 repeat_threshold: int = 3):
        self.response_times = []      # timestamps of auto-responses
        self.recent_contexts = []     # last N context hashes
        self.max_rpm = max_responses_per_minute
        self.repeat_threshold = repeat_threshold

    def record_response(self, context_snippet: str):
        now = time.time()
        self.response_times.append(now)
        # Keep last 60s
        self.response_times = [t for t in self.response_times if now - t < 60]

        # Track context similarity (simple: first 200 chars)
        sig = context_snippet[:200].strip()
        self.recent_contexts.append(sig)
        if len(self.recent_contexts) > 20:
            self.recent_contexts = self.recent_contexts[-20:]

    def is_looping(self) -> bool:
        """Too many responses too fast?"""
        return len(self.response_times) > self.max_rpm

    def is_repeating(self) -> bool:
        """Same prompt appearing over and over?"""
        if len(self.recent_contexts) < self.repeat_threshold:
            return False
        last = self.recent_contexts[-1]
        recent_same = sum(1 for c in self.recent_contexts[-5:] if c == last)
        return recent_same >= self.repeat_threshold


# =============================================================================
# Window size propagation
# =============================================================================

def get_winsize() -> Tuple[int, int]:
    """Get terminal window size (rows, cols)."""
    try:
        packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ,
                             b'\x00' * 8)
        rows, cols = struct.unpack('HHHH', packed)[:2]
        return rows, cols
    except Exception:
        return 24, 80


def set_winsize(fd: int, rows: int, cols: int):
    """Set window size on a PTY fd."""
    try:
        packed = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
    except Exception:
        pass


# =============================================================================
# Main supervisor loop
# =============================================================================

_DEFAULT_SUPERVISOR_SYSTEM = (
    "You are a supervisor that approves or denies tool actions. "
    "By default, approve safe actions and deny dangerous ones "
    "(e.g. deleting production data, force-pushing, rm -rf /). "
    "Do NOT send unsolicited messages or motivational nudges to the agent."
)


class Supervisor:
    def __init__(self, command: list[str], idle_seconds: float = 4.0,
                 provider: str = 'none', model: str = None,
                 api_key: str = None, dry_run: bool = False,
                 log_path: str = None, max_responses: int = 0,
                 goal: str = None, supervise_interval: float = 0,
                 no_hooks: bool = False, ipc_dir: str = None,
                 llm_only: bool = False,
                 system_instructions: str = None,
                 stale_timeout: float = 0,
                 session_dir: str = None,
                 worker_name: str = 'main'):
        self.command = command
        self.idle_seconds = idle_seconds
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.dry_run = dry_run
        self.max_responses = max_responses  # 0 = unlimited
        self.goal = goal                    # what the agent should be doing
        self.supervise_interval = supervise_interval  # seconds between health checks (0=off)
        self.log_path = log_path
        self.ipc = IPC.connect(ipc_dir) if ipc_dir else None
        self.llm_only = llm_only  # skip patterns, let LLM decide everything
        self.system_instructions = system_instructions  # extra instructions for LLM
        self.stale_timeout = stale_timeout  # seconds before nudging a stale agent
        self.session_dir = session_dir      # session dir for multi-agent discovery
        self.worker_name = worker_name      # this worker's name in the session

        self.master_fd = None
        self.child_pid = None
        self.buffer = []          # rolling buffer of recent output lines
        self.full_buffer = []     # full buffer for supervisor (not cleared on response)
        self.max_buffer = 200     # keep last N lines (cleared on response)
        self.max_full_buffer = 2000  # rolling terminal scrollback for supervisor
        self.hook_timeline = []      # [{ts, tool, summary}, ...] from PostToolUse
        self.max_hook_timeline = 200
        self.last_output_time = time.time()
        self.last_user_input_time = 0.0    # when user last typed something
        self.idle_handled = False  # already responded to this idle period?
        self.last_stale_nudge_time = 0.0   # when we last sent a stale nudge
        self.total_responses = 0
        self.total_interventions = 0
        self.rail_detector = RailDetector()
        self.running = True
        self.last_supervise_time = 0.0  # when we last ran a supervisor check
        self.consecutive_stuck = 0     # how many times in a row supervisor said stuck/error_loop
        self.last_intervention_msg = '' # last message we sent to the agent (anti-repeat)
        self.intervention_history = []  # rolling history: [{'msg': ..., 'verdict': ..., 'ts': ...}]
        self.max_intervention_history = 10

        # Hooks state
        self._use_hooks = (not no_hooks
                           and command and command[0] in ('claude', 'claude-code'))
        self._settings_backup = None
        self._settings_path = None
        self._state_file = None  # temp file for PostToolUse state
        self._state_file_pos = 0  # file position for reading state file

        if log_path:
            init_log(log_path)
        if self.ipc:
            set_log_ipc(self.ipc)

    def _install_hooks(self):
        """Install Claude Code hooks for auto-approval and supervisor."""
        settings_dir = os.path.join(os.getcwd(), '.claude')
        self._settings_path = os.path.join(settings_dir, 'settings.local.json')

        # Backup existing settings
        if os.path.exists(self._settings_path):
            with open(self._settings_path) as f:
                self._settings_backup = f.read()

        settings = {}
        if self._settings_backup:
            try:
                settings = json.loads(self._settings_backup)
            except json.JSONDecodeError:
                settings = {}

        # Build hook command pointing to this script
        hook_bin = os.path.abspath(__file__)
        python = sys.executable

        hooks = {
            'PreToolUse': [{
                'matcher': '',
                'hooks': [{'type': 'command',
                           'command': f'{python} {hook_bin} --hook-pre-tool'}]
            }],
        }

        # PostToolUse: feed supervisor state
        if self.goal and self.provider != 'none':
            self._state_file = tempfile.NamedTemporaryFile(
                mode='w', prefix='dedelulu_state_', suffix='.jsonl',
                delete=False)
            self._state_file.close()
            hooks['PostToolUse'] = [{
                'matcher': '',
                'hooks': [{'type': 'command',
                           'command': f'{python} {hook_bin} --hook-post-tool'}]
            }]
            # Stop hook: supervisor check when Claude is idle
            hooks['Stop'] = [{
                'matcher': '',
                'hooks': [{'type': 'command',
                           'command': f'{python} {hook_bin} --hook-stop'}]
            }]

        # Preserve existing hooks, add ours
        existing_hooks = settings.get('hooks', {})
        for event, hook_list in hooks.items():
            existing = existing_hooks.get(event, [])
            existing_hooks[event] = existing + hook_list
        settings['hooks'] = existing_hooks

        os.makedirs(settings_dir, exist_ok=True)
        with open(self._settings_path, 'w') as f:
            json.dump(settings, f, indent=2)

        log_event('hooks_installed', {
            'path': self._settings_path,
            'events': list(hooks.keys()),
        })

    def _uninstall_hooks(self):
        """Restore original settings after claude exits."""
        if not self._settings_path:
            return
        try:
            if self._settings_backup:
                with open(self._settings_path, 'w') as f:
                    f.write(self._settings_backup)
            elif os.path.exists(self._settings_path):
                os.remove(self._settings_path)
        except Exception as e:
            log_event('hooks_uninstall_error', {'error': str(e)})

        # Clean up state file
        if self._state_file:
            try:
                os.unlink(self._state_file.name)
            except Exception:
                pass

        log_event('hooks_uninstalled')

    def _get_hook_env(self) -> dict:
        """Extra env vars for child process so hooks can find log/state/IPC."""
        env = {}
        if self.log_path:
            env['TERMICLAUDE_LOG'] = os.path.abspath(self.log_path)
        if self._state_file:
            env['TERMICLAUDE_STATE'] = self._state_file.name
        if self.goal:
            env['TERMICLAUDE_GOAL'] = self.goal
        if self.provider and self.provider != 'none':
            env['TERMICLAUDE_PROVIDER'] = self.provider
        if self.model:
            env['TERMICLAUDE_MODEL'] = self.model
        if self.api_key:
            env['TERMICLAUDE_API_KEY'] = self.api_key
        if self.ipc:
            env['TERMICLAUDE_IPC'] = self.ipc.ipc_dir
        if self.system_instructions:
            env['TERMICLAUDE_SYSTEM'] = self.system_instructions
        if self.session_dir:
            env['DEDELULU_SESSION'] = self.session_dir
        if self.worker_name:
            env['DEDELULU_WORKER'] = self.worker_name
        return env

    def start(self) -> int:
        """Spawn child and run the supervisor loop. Returns exit code."""
        # Install hooks before spawning claude
        if self._use_hooks:
            self._install_hooks()

        # Save original terminal settings
        old_attrs = None
        try:
            old_attrs = termios.tcgetattr(sys.stdin.fileno())
        except Exception:
            pass

        # Write our PID so foreman can check liveness
        if self.ipc:
            try:
                with open(self.ipc.pid_path, 'w') as f:
                    f.write(str(os.getpid()))
            except Exception:
                pass

        # Fork PTY
        self.child_pid, self.master_fd = pty.fork()

        if self.child_pid == 0:
            # Pass session env vars (always, for dedelulu-send)
            if self.session_dir:
                os.environ['DEDELULU_SESSION'] = self.session_dir
            if self.worker_name:
                os.environ['DEDELULU_WORKER'] = self.worker_name
            # Pass hook env vars
            if self._use_hooks:
                os.environ.update(self._get_hook_env())
            os.execvp(self.command[0], self.command)
            sys.exit(127)  # unreachable unless exec fails

        # Parent: set up
        rows, cols = get_winsize()
        set_winsize(self.master_fd, rows, cols)

        # Handle SIGWINCH — propagate terminal resizes
        def on_winch(signum, frame):
            r, c = get_winsize()
            set_winsize(self.master_fd, r, c)
        signal.signal(signal.SIGWINCH, on_winch)

        # Put stdin in raw mode so keystrokes pass through immediately
        try:
            import tty
            tty.setraw(sys.stdin.fileno())
        except Exception:
            pass

        log_event('start', {'command': self.command, 'pid': self.child_pid,
                            'provider': self.provider, 'idle_seconds': self.idle_seconds})

        exit_code = 0
        try:
            exit_code = self._loop()
        except KeyboardInterrupt:
            # Forward Ctrl+C to child
            try:
                os.kill(self.child_pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            exit_code = 130
        finally:
            # Restore terminal
            if old_attrs:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN,
                                      old_attrs)
                except Exception:
                    pass
            # Clean up child
            try:
                os.close(self.master_fd)
            except Exception:
                pass
            try:
                os.waitpid(self.child_pid, 0)
            except Exception:
                pass
            # Uninstall hooks
            if self._use_hooks:
                self._uninstall_hooks()

            log_event('exit', {'code': exit_code,
                               'total_responses': self.total_responses})

        return exit_code

    def _loop(self) -> int:
        """Main select loop."""
        while self.running:
            # Build fd list
            fds = [self.master_fd]
            try:
                fds.append(sys.stdin.fileno())
            except Exception:
                pass

            try:
                readable, _, _ = select.select(fds, [], [], 0.5)
            except (OSError, ValueError):
                break

            for fd in readable:
                if fd == self.master_fd:
                    # Output from child
                    try:
                        data = os.read(self.master_fd, 16384)
                    except OSError:
                        self.running = False
                        break
                    if not data:
                        self.running = False
                        break

                    # Pass through to real stdout
                    os.write(sys.stdout.fileno(), data)

                    # Buffer for analysis
                    text = data.decode('utf-8', errors='replace')
                    self._buffer_output(text)
                    self.last_output_time = time.time()
                    self.last_stale_nudge_time = 0.0  # reset stale timer on new output
                    self.idle_handled = False

                elif fd == sys.stdin.fileno():
                    # Input from user — pass through to child
                    try:
                        data = os.read(sys.stdin.fileno(), 4096)
                    except OSError:
                        break
                    if data:
                        try:
                            os.write(self.master_fd, data)
                        except OSError:
                            self.running = False
                            break
                        # User typed something — reset idle
                        self.last_output_time = time.time()
                        self.last_user_input_time = time.time()
                        self.idle_handled = True  # don't auto-respond after user input

            # Check for idle (prompt auto-response)
            if not self.idle_handled and self.buffer:
                elapsed = time.time() - self.last_output_time
                if elapsed >= self.idle_seconds:
                    self._handle_idle()

            # Ingest hook events into timeline
            self._ingest_hook_timeline()

            # Periodic supervisor health check
            if (self.supervise_interval > 0 and self.goal
                    and self.provider != 'none'):
                now = time.time()
                if now - self.last_supervise_time >= self.supervise_interval:
                    self._supervise()
                    self.last_supervise_time = now

            # Stale agent detection — nudge if idle too long and user not typing
            if self.stale_timeout > 0 and self.provider != 'none':
                now = time.time()
                time_since_output = now - self.last_output_time
                time_since_user = now - self.last_user_input_time if self.last_user_input_time else float('inf')
                time_since_nudge = now - self.last_stale_nudge_time if self.last_stale_nudge_time else float('inf')
                if (time_since_output >= self.stale_timeout
                        and time_since_user >= self.stale_timeout
                        and time_since_nudge >= self.stale_timeout):
                    if self._agent_waiting_for_user():
                        # Agent is at prompt — let LLM decide if task is done or needs push
                        self._intervene(trigger='stale_at_prompt',
                                        reasoning='agent at prompt, checking if task complete')
                    else:
                        self._intervene(trigger='stale', reasoning='agent stale, no activity')
                    self.last_stale_nudge_time = now

            # Poll foreman input (escalation responses + commands)
            if self.ipc:
                msg = self.ipc.poll_input()
                if msg:
                    if msg.get('command') == 'stale':
                        new_timeout = float(msg.get('value', 300))
                        self.stale_timeout = new_timeout
                        self.last_stale_nudge_time = 0.0
                        label = f'{int(new_timeout)}s' if new_timeout > 0 else 'off'
                        self._notify(f"[dedelulu] stale nudge: {label}", 'info')
                        log_event('stale_config', {'timeout': new_timeout})
                    elif msg.get('message'):
                        response_text = msg['message']
                        try:
                            os.write(self.master_fd, (response_text + '\r').encode())
                        except OSError:
                            pass
                        self.idle_handled = False  # resume auto-approvals
                        self._notify(f"[dedelulu] foreman response: {response_text[:60]}", 'info')
                        log_event('foreman_response', {'message': response_text})

            # Check if child exited
            try:
                pid, status = os.waitpid(self.child_pid, os.WNOHANG)
                if pid != 0:
                    # Drain remaining output
                    try:
                        while True:
                            r, _, _ = select.select([self.master_fd], [], [], 0.1)
                            if not r:
                                break
                            data = os.read(self.master_fd, 16384)
                            if not data:
                                break
                            os.write(sys.stdout.fileno(), data)
                    except Exception:
                        pass
                    return os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                return 0

        return 0

    def _buffer_output(self, text: str):
        """Add text to rolling line buffers."""
        lines = text.split('\n')
        for buf in (self.buffer, self.full_buffer):
            if lines and buf:
                buf[-1] += lines[0]
                buf.extend(lines[1:])
            else:
                buf.extend(lines)
        if len(self.buffer) > self.max_buffer:
            self.buffer = self.buffer[-self.max_buffer:]
        if len(self.full_buffer) > self.max_full_buffer:
            self.full_buffer = self.full_buffer[-self.max_full_buffer:]

    def _handle_idle(self):
        """Program has been quiet — check if it's waiting for input."""
        self.idle_handled = True

        # Check response limit
        if self.max_responses and self.total_responses >= self.max_responses:
            log_event('limit_reached', {'max': self.max_responses})
            return

        # Get clean text for analysis
        raw_tail = '\n'.join(self.buffer[-30:])
        clean_tail = strip_ansi(raw_tail)
        clean_tail_stripped = clean_tail.strip()

        if not clean_tail_stripped:
            return

        # Check for rail issues
        if self.rail_detector.is_looping():
            log_event('rail_looping', {'context': clean_tail_stripped[-200:]})
            self._notify("[dedelulu] Too many auto-responses/min — pausing automation", 'alert')
            return

        if self.rail_detector.is_repeating():
            log_event('rail_repeating', {'context': clean_tail_stripped[-200:]})
            self._notify("[dedelulu] Repeated prompt detected — pausing automation", 'alert')
            return

        # Try fast pattern match first (unless --llm-only)
        response = None if self.llm_only else fast_match(clean_tail_stripped)
        source = 'pattern'

        if response is None:
            # Check if it even looks like a prompt
            if not looks_like_prompt(clean_tail_stripped):
                return  # Probably just slow output, not a prompt

            # LLM fallback
            if self.provider == 'none':
                # No LLM configured, but it looks like a prompt — default to yes
                response = 'y'
                source = 'default'
            else:
                context = '\n'.join(self.buffer[-30:])
                clean_context = strip_ansi(context)
                response = ask_llm_prompt(clean_context, provider=self.provider,
                                   model=self.model, api_key=self.api_key,
                                   system_instructions=self.system_instructions)
                source = 'llm'

                if response is not None:
                    response = _normalize_llm_response(response)

                if response == '__SKIP__':
                    log_event('llm_skip', {'context': clean_tail_stripped[-200:]})
                    return

                if response is None:
                    # LLM failed or returned nothing — default to "y"
                    response = 'y'
                    source = 'default'

        # Dry run?
        if self.dry_run:
            display = repr(response) if response else "'\\n'"
            self._notify(f"[dedelulu] DRY RUN: would send {display} (source: {source})")
            log_event('dry_run', {'response': response, 'source': source,
                                  'context': clean_tail_stripped[-200:]})
            return

        # Send the response
        # Use \r (carriage return) — in raw terminal mode, Enter = \r not \n
        try:
            to_send = response + '\r' if response else '\r'
            os.write(self.master_fd, to_send.encode())
        except OSError:
            return

        self.total_responses += 1
        self.rail_detector.record_response(clean_tail_stripped)

        # Clear buffer so old prompts don't cause false matches next time
        self.buffer.clear()

        display = repr(response) if response else '↵'
        log_event('respond', {
            'response': response,
            'source': source,
            'context': clean_tail_stripped[-100:],
            'count': self.total_responses
        })

        # Brief visual indicator (sent to stderr so it doesn't mix with PTY)
        source_info = f"{source}:{self.provider}" if source == 'llm' else source
        self._notify(f"[dedelulu] #{self.total_responses} sent {display} ({source_info})", 'ok')

    # Patterns that indicate the agent is done and waiting for user input (don't nudge)
    _AGENT_WAITING_PATTERNS = [
        re.compile(r'❯\s*$'),                          # Claude Code prompt
        re.compile(r'^\s*>\s*$', re.MULTILINE),         # generic prompt
        re.compile(r'Cogitated\s+for', re.IGNORECASE),  # Claude Code "Cogitated for Xm Ys"
        re.compile(r'↓\s+to\s+manage'),                 # Claude Code "↓ to manage"
        re.compile(r'tab\s+to\s+amend', re.IGNORECASE), # Claude Code waiting for next prompt
    ]

    def _agent_waiting_for_user(self) -> bool:
        """Check if the agent's last output looks like it's waiting for the USER (not stuck)."""
        if not self.buffer:
            return False
        # Check last 5 lines of raw output
        tail = '\n'.join(self.buffer[-5:])
        clean = strip_ansi(tail)
        return any(p.search(clean) for p in self._AGENT_WAITING_PATTERNS)

    def _ingest_hook_timeline(self):
        """Read new PostToolUse entries from the state file into hook_timeline."""
        if not self._state_file:
            return
        try:
            path = self._state_file.name
            size = os.path.getsize(path)
            if size <= self._state_file_pos:
                return
            with open(path) as f:
                f.seek(self._state_file_pos)
                lines = f.readlines()
                self._state_file_pos = f.tell()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    self.hook_timeline.append({
                        'ts': datetime.fromisoformat(entry['ts']).strftime('%H:%M:%S'),
                        'tool': entry.get('tool', '?'),
                        'summary': entry.get('input_summary', '')[:120],
                    })
                except (json.JSONDecodeError, KeyError):
                    pass
            if len(self.hook_timeline) > self.max_hook_timeline:
                self.hook_timeline = self.hook_timeline[-self.max_hook_timeline:]
        except Exception:
            pass

    # ── Context builder for LLM calls ──

    def _build_llm_context(self) -> str:
        """Build rich context for supervisor LLM: hook timeline + terminal scrollback."""
        parts = []

        # 1. Hook timeline (compact activity log)
        if self.hook_timeline:
            tl_lines = []
            for h in self.hook_timeline[-50:]:
                tl_lines.append(f"  {h['ts']} {h['tool']}: {h['summary']}")
            parts.append("AGENT ACTIVITY TIMELINE (tool calls):\n" + '\n'.join(tl_lines))

        # 2. Terminal scrollback — ~3 pages worth
        if self.full_buffer:
            # ~200 lines ≈ 3 terminal pages (assuming 60-row terminal)
            raw = '\n'.join(self.full_buffer[-200:])
            clean = strip_ansi(raw).strip()
            if clean:
                parts.append(f"TERMINAL OUTPUT (last ~200 lines, ANSI-stripped):\n{clean}")

        return '\n\n'.join(parts) if parts else '(no output yet)'

    # ── Level 2: Supervisor (health check only, never touches PTY) ──

    def _supervise(self):
        """Periodic health check — diagnose only, escalate to interventor if needed."""
        if not self.full_buffer:
            return

        context = self._build_llm_context()
        if not context.strip():
            return

        verdict = ask_llm_supervise(
            goal=self.goal,
            recent_output=context,
            provider=self.provider,
            model=self.model,
            api_key=self.api_key,
            system_instructions=self.system_instructions,
            consecutive_stuck=self.consecutive_stuck,
            intervention_history=self.intervention_history,
        )

        if not verdict:
            return

        log_event('supervise', {
            'status': verdict.status,
            'action': verdict.action,
            'message': verdict.message,
            'reasoning': verdict.reasoning,
            'consecutive_stuck': self.consecutive_stuck,
        })

        # Track consecutive stuck/error_loop detections
        if verdict.status in ('stuck', 'error_loop', 'off_rails'):
            self.consecutive_stuck += 1
        else:
            self.consecutive_stuck = 0

        if verdict.action == 'continue':
            self._notify(f"[dedelulu] on track — {verdict.reasoning}", 'ok')
            # Record in history so next check sees the arc
            self.intervention_history.append({
                'ts': datetime.now().strftime('%H:%M:%S'),
                'trigger': 'supervisor',
                'status': verdict.status,
                'msg': '',
                'reasoning': verdict.reasoning,
            })
            if len(self.intervention_history) > self.max_intervention_history:
                self.intervention_history = self.intervention_history[-self.max_intervention_history:]
            return

        # Anti-loop: if we've been stuck 3+ times, stop nagging — escalate to human
        if self.consecutive_stuck >= 3 and verdict.action != 'escalate':
            self._notify(f"[dedelulu] stuck {self.consecutive_stuck}x — escalating to human", 'escalate')
            verdict.action = 'escalate'
            verdict.message = (f"Agent appears stuck ({self.consecutive_stuck} checks). "
                              f"Last diagnosis: {verdict.reasoning}")

        # Everything else → escalate to interventor (level 3)
        self._intervene(
            trigger='supervisor',
            action=verdict.action,
            message=verdict.message,
            reasoning=verdict.reasoning,
            status=verdict.status,
        )

    # ── Level 3: Interventor (single place that talks to the agent) ──

    def _get_timing_stats(self) -> dict:
        """Collect timing stats for the interventor."""
        now = time.time()
        return {
            'since_output': int(now - self.last_output_time),
            'since_user_input': int(now - self.last_user_input_time) if self.last_user_input_time else None,
            'since_last_nudge': int(now - self.last_stale_nudge_time) if self.last_stale_nudge_time else None,
            'since_supervise': int(now - self.last_supervise_time) if self.last_supervise_time else None,
            'total_responses': self.total_responses,
            'total_interventions': self.total_interventions,
            'consecutive_stuck': self.consecutive_stuck,
        }

    def _intervene(self, trigger: str, action: str = 'message',
                   message: str = '', reasoning: str = '', status: str = ''):
        """Level 3: actually send messages / interrupts to the agent.

        Args:
            trigger: what caused this — 'supervisor', 'stale', or 'stale_supervisor'
            action: 'message', 'interrupt', 'escalate'
            message: text to send (if empty and trigger is 'stale', ask LLM)
            reasoning: why we're intervening
            status: supervisor diagnosis (stuck, off_rails, etc.)
        """
        stats = self._get_timing_stats()

        # ── Escalate to human (pause automation, ring bell) ──
        if action == 'escalate':
            self.total_interventions += 1
            question = message or reasoning
            self._notify(
                f"[dedelulu] NEEDS YOUR INPUT: {question}",
                'escalate')
            self.idle_handled = True
            log_event('escalate', {
                'trigger': trigger,
                'question': question,
                'reasoning': reasoning,
                'stats': stats,
            })
            return

        # ── Stale trigger with no message → ask LLM what to say ──
        if trigger.startswith('stale') and not message:
            message = self._ask_stale_nudge(stats, at_prompt=(trigger == 'stale_at_prompt'))
            if message is None:
                return  # LLM said SKIP or failed

        if not message:
            return

        # ── Interrupt (Ctrl+C then message) ──
        if action == 'interrupt':
            self.total_interventions += 1
            self._notify(
                f"[dedelulu] INTERRUPTING: {status} — {reasoning}",
                'alert')
            try:
                os.write(self.master_fd, b'\x03')
            except OSError:
                pass
            time.sleep(1)
            try:
                os.write(self.master_fd, (message + '\r').encode())
            except OSError:
                pass
            self._notify(
                f"[dedelulu] sent redirect: {message[:80]}", 'info')

        # ── Message (type into agent prompt) ──
        else:
            self.total_interventions += 1
            label = 'nudge' if trigger == 'stale' else 'redirect'
            self._notify(
                f"[dedelulu] {label}: {reasoning or message[:60]}", 'info')
            try:
                os.write(self.master_fd, (message + '\r').encode())
            except OSError:
                pass

        self.idle_handled = False  # allow auto-approvals after intervention
        self.last_intervention_msg = message  # remember for anti-repeat
        self.intervention_history.append({
            'ts': datetime.now().strftime('%H:%M:%S'),
            'trigger': trigger,
            'status': status,
            'msg': message,
            'reasoning': reasoning,
        })
        if len(self.intervention_history) > self.max_intervention_history:
            self.intervention_history = self.intervention_history[-self.max_intervention_history:]

        log_event('intervene', {
            'trigger': trigger,
            'type': action,
            'message': message,
            'reasoning': reasoning,
            'stats': stats,
        })

    def _ask_stale_nudge(self, stats: dict, at_prompt: bool = False) -> Optional[str]:
        """Ask LLM what to say to a stale agent. Returns message or None (skip)."""
        context = self._build_llm_context()
        sys_instr = self.system_instructions or _DEFAULT_SUPERVISOR_SYSTEM

        history_ctx = ''
        if self.intervention_history:
            lines = []
            for h in self.intervention_history[-5:]:
                if h.get('msg'):
                    lines.append(f"  [{h['ts']}] you said: \"{h['msg']}\"")
                else:
                    lines.append(f"  [{h['ts']}] checked: {h.get('status', '?')}")
            history_ctx = "\nYOUR PREVIOUS MESSAGES (do NOT repeat — try a different angle):\n" + '\n'.join(lines) + "\n"

        if at_prompt:
            situation = (
                "The agent is sitting at an input prompt (❯) and has not started new work.\n"
                "First decide: is the GOAL fully achieved based on the activity timeline and output?\n"
                "- If YES and nothing remains: respond SKIP\n"
                "- If NO or unclear: write a message telling the agent what's still left to do. "
                "Be specific — name the files, tests, or steps that still need work."
            )
        else:
            situation = (
                "The agent appears stuck mid-task with no output.\n"
                "- If it finished: suggest verifying work or what to do next\n"
                "- If stuck: suggest a concrete next step\n"
                "- If clearly done and nothing needed: respond SKIP"
            )

        prompt = f"""You are supervising an AI coding agent (Claude Code). It has gone stale.

GOAL: {self.goal or '(no specific goal set)'}

INSTRUCTIONS: {sys_instr}
{history_ctx}
SITUATION: {situation}

TIMING:
- No output for {stats['since_output']}s ({stats['since_output'] // 60}min)
- User last typed: {f"{stats['since_user_input']}s ago" if stats['since_user_input'] is not None else 'never this session'}
- Total auto-responses so far: {stats['total_responses']}
- Total interventions so far: {stats['total_interventions']}

AGENT CONTEXT:
{context[-3000:]}

RULES:
- Match the language the user/agent are using (if they speak Russian, write in Russian, etc.)
- Do NOT repeat or rephrase previous messages — try a completely different angle
- Talk like a helpful colleague, not a system. Be natural and concise (1-2 sentences)
- Reply with ONLY the message text (no quotes, no explanation). Or SKIP if done."""

        try:
            if self.provider == 'claude-cli':
                raw = _ask_claude_cli(prompt, self.model)
            elif self.provider == 'anthropic':
                raw = _ask_anthropic(prompt, self.model or 'claude-haiku-4-5-20251001',
                                     self.api_key or os.getenv('ANTHROPIC_API_KEY'))
            elif self.provider == 'ollama':
                raw = _ask_ollama(prompt, self.model or 'ministral-3:8b')
            elif self.provider == 'openai':
                raw = _ask_openai(prompt, self.model or 'gpt-4o-mini',
                                  self.api_key or os.getenv('OPENAI_API_KEY'))
            elif self.provider == 'azure':
                raw = _ask_azure(prompt, self.model or 'gpt-4o-mini',
                                 self.api_key or os.getenv('AZURE_OPENAI_API_KEY'))
            else:
                raw = None
        except Exception as e:
            log_event('stale_nudge_error', {'error': str(e)})
            raw = None

        if not raw:
            return "Hey, looks like you've paused — please continue working on the task, or let me know if you're done!"

        raw = raw.strip()
        if raw.upper() == 'SKIP':
            log_event('stale_skip', {'reason': 'LLM says agent is done'})
            self._notify("[dedelulu] stale check: agent appears done, not nudging", 'ok')
            return None

        # Strip wrapping quotes
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            raw = raw[1:-1]
        return raw

    # ANSI color codes for notification levels
    _NOTIFY_STYLES = {
        'ok':       '\x1b[90m',        # gray — all good, low noise
        'info':     '\x1b[36m',        # cyan — informational
        'alert':    '\x1b[1;31m',      # bold red — intervention
        'escalate': '\x1b[1;33m',      # bold yellow — needs human
    }

    def _notify(self, msg: str, level: str = 'info'):
        """Print a supervisor message with appropriate urgency."""
        try:
            style = self._NOTIFY_STYLES.get(level, '\x1b[90m')
            bel = '\x07' if level in ('alert', 'escalate') else ''
            line = f"\r\n{style}{msg}\x1b[0m{bel}\r\n"
            sys.stderr.buffer.write(line.encode())
            sys.stderr.buffer.flush()
        except Exception:
            pass
        # Forward to foreman via IPC
        if self.ipc:
            self.ipc.send_event('notify', msg=msg, level=level)


# =============================================================================
# CLI
# =============================================================================

def _hook_write_event(event: str, **data):
    """Write event to log and IPC from hook subprocess."""
    entry = {'ts': datetime.now().isoformat(), 'event': event, **data}
    log_path = os.environ.get('TERMICLAUDE_LOG')
    if log_path:
        with open(log_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    ipc_dir = os.environ.get('TERMICLAUDE_IPC')
    if ipc_dir:
        events_path = os.path.join(ipc_dir, 'events.jsonl')
        ipc_entry = {'ts': datetime.now().strftime('%H:%M:%S'), 'event': event, **data}
        with open(events_path, 'a') as f:
            f.write(json.dumps(ipc_entry) + '\n')


def _hook_pre_tool_use():
    """Hook entry point for Claude Code PreToolUse — auto-approves all tools."""
    try:
        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get('tool_name', 'unknown')
        _hook_write_event('hook_approve', tool=tool_name)
        json.dump({'decision': 'approve'}, sys.stdout)
    except Exception:
        json.dump({'decision': 'approve'}, sys.stdout)
    sys.exit(0)


def _hook_post_tool_use():
    """Hook entry point for Claude Code PostToolUse — feeds supervisor."""
    state_path = os.environ.get('TERMICLAUDE_STATE')
    if not state_path:
        sys.exit(0)

    try:
        input_data = json.loads(sys.stdin.read())
        tool_name = input_data.get('tool_name', 'unknown')
        tool_input = input_data.get('tool_input', {})
        summary = _summarize_tool_input(tool_name, tool_input)

        # Append to shared state file for supervisor to read
        with open(state_path, 'a') as f:
            f.write(json.dumps({
                'ts': datetime.now().isoformat(),
                'tool': tool_name,
                'input_summary': summary,
            }) + '\n')

        _hook_write_event('hook_post_tool', tool=tool_name, summary=summary)
    except Exception:
        pass
    sys.exit(0)


def _hook_stop():
    """Hook entry point for Claude Code Stop — runs supervisor check."""
    state_path = os.environ.get('TERMICLAUDE_STATE')
    log_path = os.environ.get('TERMICLAUDE_LOG')
    goal = os.environ.get('TERMICLAUDE_GOAL')
    provider = os.environ.get('TERMICLAUDE_PROVIDER')
    model = os.environ.get('TERMICLAUDE_MODEL')
    api_key = os.environ.get('TERMICLAUDE_API_KEY')

    if not (state_path and goal and provider):
        sys.exit(0)

    try:
        # Read accumulated tool actions from state file
        if not os.path.exists(state_path):
            sys.exit(0)
        with open(state_path) as f:
            lines = f.readlines()
        if not lines:
            sys.exit(0)

        # Build context from recent tool actions
        recent = lines[-30:]  # last 30 tool actions
        context = '\n'.join(line.strip() for line in recent)

        # Also read stdin for stop event data
        try:
            stop_data = json.loads(sys.stdin.read())
        except Exception:
            stop_data = {}

        system_instructions = os.environ.get('TERMICLAUDE_SYSTEM')
        verdict = ask_llm_supervise(
            goal=goal,
            recent_output=f"Recent tool actions:\n{context}",
            provider=provider,
            model=model,
            api_key=api_key,
            system_instructions=system_instructions,
        )

        if verdict and log_path:
            with open(log_path, 'a') as f:
                f.write(json.dumps({
                    'ts': datetime.now().isoformat(),
                    'event': 'hook_supervise',
                    'status': verdict.status,
                    'action': verdict.action,
                    'reasoning': verdict.reasoning,
                }) + '\n')

        # Clear state file after check
        with open(state_path, 'w') as f:
            pass

        # Notify user based on verdict
        if verdict and verdict.action == 'escalate':
            msg = verdict.message or verdict.reasoning
            sys.stderr.write(
                f"\r\n\x1b[1;33m[dedelulu] NEEDS YOUR INPUT: {msg}\x1b[0m\x07\r\n")
            sys.stderr.flush()
        elif verdict and verdict.action in ('interrupt', 'message'):
            sys.stderr.write(
                f"\r\n\x1b[1;31m[dedelulu] SUPERVISOR: {verdict.reasoning}\x1b[0m\x07\r\n")
            sys.stderr.flush()

    except Exception:
        pass
    sys.exit(0)


def _summarize_tool_input(tool_name: str, tool_input: dict) -> str:
    """Short summary of tool input for supervisor context."""
    if tool_name in ('Read', 'Glob', 'Grep'):
        return tool_input.get('file_path', tool_input.get('pattern', str(tool_input)[:100]))
    if tool_name == 'Write':
        path = tool_input.get('file_path', '?')
        size = len(tool_input.get('content', ''))
        return f'{path} ({size} chars)'
    if tool_name == 'Edit':
        return tool_input.get('file_path', '?')
    if tool_name == 'Bash':
        return tool_input.get('command', '')[:120]
    return str(tool_input)[:100]


def main():
    # Handle hook subcommands before argparse (they read stdin, must be fast)
    if len(sys.argv) >= 2 and sys.argv[1] == '--hook-pre-tool':
        _hook_pre_tool_use()
    if len(sys.argv) >= 2 and sys.argv[1] == '--hook-post-tool':
        _hook_post_tool_use()
    if len(sys.argv) >= 2 and sys.argv[1] == '--hook-stop':
        _hook_stop()
    # Foreman mode: dedelulu --foreman <ipc_dir>
    if len(sys.argv) >= 3 and sys.argv[1] == '--foreman':
        run_foreman(sys.argv[2])
        sys.exit(0)

    parser = argparse.ArgumentParser(
        prog='dedelulu',
        description='Autonomous supervisor for interactive CLI agents',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
usage:
  cd ~/your-project
  dedelulu claude "refactor the auth module"

  That's it. dedelulu wraps claude (or any CLI), auto-approves
  prompts, and logs every decision to dedelulu.jsonl.

  With tmux installed, dedelulu auto-splits into two panes:
    left 80%  = Claude Code (full passthrough, you see everything)
    right 20% = Foreman (status, logs, answers your questions)

more examples:
  dedelulu claude "add tests for auth"     # auto-approve, goal auto-extracted
  dedelulu --idle 8 claude "big refactor"  # more patience before responding
  dedelulu --dry-run claude "delete stuff" # see what it would approve
  dedelulu --no-log npm init               # wrap any interactive CLI
  dedelulu --no-tmux claude "quick fix"    # single-pane mode (no split)

with supervisor (watches output, intervenes if agent goes off-rails):
  dedelulu --provider ollama claude "add JWT auth"
  dedelulu --provider ollama --supervise 30 claude "big refactor"
  dedelulu --provider anthropic --goal "fix login bug" claude

stale agent nudging (off by default, enable via --stale or /stale in foreman):
  dedelulu --stale 300 --provider ollama claude "task"   # nudge after 5min
  dedelulu --stale 600 --provider ollama claude "task"   # nudge after 10min
  # or enable at runtime from foreman: /stale worker1 300

multi-agent (add workers dynamically from foreman pane):
  dedelulu claude "build the API"
  # in foreman pane (Ctrl-B →):
  #   /add tests:~/project:write pytest tests
  #   /send tests "API is ready, start testing"
  #   /broadcast "wrap up and commit"
  #   /system "be more careful with destructive operations"
  #   /status

with system instructions (put dedelulu flags BEFORE the command):
  dedelulu --system "always say yes" -- claude "refactor auth"
  dedelulu --system "use screenshots" --goal "fix TUI" -- claude "fix it"
        """
    )

    parser.add_argument('command', nargs=argparse.REMAINDER,
                        help='command to run and supervise (use -- before commands with flags)')
    # NOTE: dedelulu flags (--system, --goal, etc) must come BEFORE the command.
    # Use -- to separate: dedelulu --system "..." -- claude "prompt"
    parser.add_argument('--idle', type=float, default=4.0,
                        help='seconds of silence before checking for prompt (default: 4)')
    parser.add_argument('--provider',
                        choices=['none', 'claude-cli', 'anthropic', 'ollama', 'openai', 'azure'],
                        default=None,
                        help='LLM provider for supervisor & ambiguous prompts '
                             '(default: azure if env vars set, else none)')
    parser.add_argument('--model', help='specific model to use with LLM provider')
    parser.add_argument('--api-key', help='API key (or use env var)')
    parser.add_argument('--dry-run', action='store_true',
                        help='detect prompts but don\'t send responses')
    parser.add_argument('--log', default='dedelulu.jsonl',
                        help='log file path (default: dedelulu.jsonl)')
    parser.add_argument('--no-log', action='store_true',
                        help='disable logging')
    parser.add_argument('--max-responses', type=int, default=0,
                        help='max auto-responses before stopping (0=unlimited)')
    parser.add_argument('--goal',
                        help='describe what the agent should accomplish '
                             '(enables supervisor health checks)')
    parser.add_argument('--supervise', type=float, default=0, metavar='SECS',
                        help='supervisor check interval in seconds '
                             '(default: 0=off, try 30-120)')
    parser.add_argument('--no-hooks', action='store_true',
                        help='disable Claude Code hooks (use PTY-only mode)')
    parser.add_argument('--no-tmux', action='store_true',
                        help='single-pane mode, no tmux split')
    parser.add_argument('--llm-only', action='store_true',
                        help='skip pattern matching, let LLM decide every response')
    parser.add_argument('--system', metavar='TEXT',
                        help='extra instructions for the supervisor LLM '
                             '(e.g. "always answer no", "choose option 2")')
    parser.add_argument('--stale', type=float, default=0, metavar='SECS',
                        help='seconds of inactivity before nudging stale agent '
                             '(default: 0=off, e.g. 300 for 5min)')
    parser.add_argument('--name', default='main',
                        help='worker name for multi-agent sessions (default: main)')
    parser.add_argument('--ipc-dir',
                        help=argparse.SUPPRESS)  # internal: set by tmux launcher
    parser.add_argument('--session-dir',
                        help=argparse.SUPPRESS)  # internal: session directory

    args = parser.parse_args()

    # Auto-detect provider: azure if env vars present, else none
    if args.provider is None:
        if os.getenv('AZURE_OPENAI_API_KEY') and os.getenv('AZURE_OPENAI_ENDPOINT'):
            args.provider = 'azure'
        else:
            args.provider = 'none'
            sys.stderr.write(
                "\x1b[33m[dedelulu] AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT not set.\n"
                "  Running without LLM supervisor (pattern-only).\n"
                "  To enable: export AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=...\n"
                "  Or use: --provider ollama / --provider anthropic\x1b[0m\n"
            )

    log_path = None if args.no_log else args.log

    # Strip leading '--' from REMAINDER
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        parser.error('no command specified')

    # Auto-extract goal from command if wrapping claude and no explicit --goal
    goal = args.goal
    if not goal and len(command) >= 2 and command[0] in ('claude', 'claude-code'):
        non_flag_args = [a for a in command[1:] if not a.startswith('-')]
        if non_flag_args:
            goal = ' '.join(non_flag_args)

    # If goal provided but no supervise interval, default to 60s
    supervise_interval = args.supervise
    if goal and supervise_interval == 0 and args.provider != 'none':
        supervise_interval = 120.0

    # Build extra_args to pass when spawning new workers via /add
    extra_args = []
    if args.provider and args.provider != 'none':
        extra_args += ['--provider', args.provider]
    if args.model:
        extra_args += ['--model', args.model]
    if args.idle != 4.0:
        extra_args += ['--idle', str(args.idle)]
    if args.supervise > 0:
        extra_args += ['--supervise', str(args.supervise)]
    if args.stale != 0:
        extra_args += ['--stale', str(args.stale)]
    if args.no_hooks:
        extra_args.append('--no-hooks')
    if args.no_log:
        extra_args.append('--no-log')
    elif args.log != 'dedelulu.jsonl':
        extra_args += ['--log', args.log]

    # tmux auto-split: launch in tmux if not already there and not disabled
    if not args.no_tmux and not args.ipc_dir and not os.environ.get('TMUX'):
        # Create session (first worker)
        session = Session.create(
            name=args.name, directory=os.getcwd(),
            task=goal or ' '.join(command),
            system_instructions=args.system or '',
            extra_args=extra_args,
        )
        w = session.workers[args.name]
        # Rebuild args for the worker
        worker_args = sys.argv[1:]
        launch_tmux(worker_args, session.session_dir, w.ipc_dir)
        # launch_tmux does execvp, so we only get here if tmux not found
        args.ipc_dir = None
    elif not args.no_tmux and not args.ipc_dir and os.environ.get('TMUX'):
        # Already inside tmux — create session, split foreman pane
        session = Session.create(
            name=args.name, directory=os.getcwd(),
            task=goal or ' '.join(command),
            system_instructions=args.system or '',
            extra_args=extra_args,
        )
        w = session.workers[args.name]
        args.ipc_dir = w.ipc_dir
        args.session_dir = session.session_dir
        _launch_foreman_pane(session.session_dir)

    sup = Supervisor(
        command=command,
        idle_seconds=args.idle,
        provider=args.provider,
        model=args.model,
        api_key=args.api_key,
        dry_run=args.dry_run,
        log_path=log_path,
        max_responses=args.max_responses,
        goal=goal,
        supervise_interval=supervise_interval,
        no_hooks=args.no_hooks,
        ipc_dir=args.ipc_dir,
        llm_only=args.llm_only,
        system_instructions=args.system,
        stale_timeout=args.stale,
        session_dir=getattr(args, 'session_dir', None),
        worker_name=args.name,
    )

    exit_code = sup.start()
    # Clean up IPC if we created it
    if args.ipc_dir:
        try:
            IPC(args.ipc_dir).cleanup()
        except Exception:
            pass
    sys.exit(exit_code)


def send_main():
    """Entry point for dedelulu-send: send a message to another worker.

    Usage: dedelulu-send <worker-name> <message>
    Uses DEDELULU_SESSION env var to find the session.
    """
    if len(sys.argv) < 3:
        print("usage: dedelulu-send <worker-name> <message>", file=sys.stderr)
        sys.exit(1)

    session_dir = os.environ.get('DEDELULU_SESSION')
    if not session_dir:
        print("error: DEDELULU_SESSION not set (are you running inside dedelulu?)",
              file=sys.stderr)
        sys.exit(1)

    target = sys.argv[1]
    message = ' '.join(sys.argv[2:])

    try:
        session = Session.load(session_dir)
    except Exception as e:
        print(f"error: cannot load session: {e}", file=sys.stderr)
        sys.exit(1)

    if target not in session.workers:
        available = ', '.join(session.workers.keys())
        print(f"error: unknown worker '{target}' (available: {available})",
              file=sys.stderr)
        sys.exit(1)

    sender = os.environ.get('DEDELULU_WORKER', 'unknown')
    session.send_to_worker(target, message, sender=sender)
    print(f"sent to [{target}]")


if __name__ == '__main__':
    main()
