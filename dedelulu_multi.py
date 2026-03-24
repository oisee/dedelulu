#!/usr/bin/env python3
"""
dedelulu multi — Multi-worker orchestration for dedelulu.

Spawns N Claude Code workers in tmux panes with a shared foreman.
Workers can communicate through the foreman via /send and /broadcast.

Usage:
    dedelulu multi \
      --worker "auth:~/project:implement JWT auth" \
      --worker "tests:~/project:write tests for auth" \
      --provider ollama
"""

import os
import sys
import json
import time
import shlex
import shutil
import signal
import tempfile
import subprocess
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from dedelulu import IPC


# =============================================================================
# Session — manages the multi-worker session state on disk
# =============================================================================

@dataclass
class WorkerSpec:
    name: str
    directory: str
    task: str
    ipc_dir: str = ''
    groups: list = field(default_factory=list)


class Session:
    """Persistent session state for multi-worker orchestration.

    Layout:
        {session_dir}/session.json    — worker specs, groups
        {session_dir}/workers/{name}/ — per-worker IPC dirs
    """

    def __init__(self, session_dir: str):
        self.session_dir = session_dir
        self.session_file = os.path.join(session_dir, 'session.json')
        self.workers_dir = os.path.join(session_dir, 'workers')
        self.workers: dict[str, WorkerSpec] = {}
        self.groups: dict[str, list[str]] = {}  # group_name -> [worker_names]

    @classmethod
    def create(cls, workers: list[WorkerSpec]) -> 'Session':
        session_dir = tempfile.mkdtemp(prefix='dedelulu_multi_')
        session = cls(session_dir)
        os.makedirs(session.workers_dir, exist_ok=True)

        for w in workers:
            # Create IPC dir for each worker
            w_ipc_dir = os.path.join(session.workers_dir, w.name)
            os.makedirs(w_ipc_dir, exist_ok=True)
            # Touch IPC files
            for fname in ('events.jsonl', 'input.jsonl', 'pid'):
                open(os.path.join(w_ipc_dir, fname), 'w').close()
            w.ipc_dir = w_ipc_dir
            session.workers[w.name] = w

        # Default group: "all"
        session.groups['all'] = [w.name for w in workers]
        session.save()
        return session

    @classmethod
    def load(cls, session_dir: str) -> 'Session':
        session = cls(session_dir)
        with open(session.session_file) as f:
            data = json.load(f)
        for wd in data.get('workers', []):
            session.workers[wd['name']] = WorkerSpec(**wd)
        session.groups = data.get('groups', {})
        return session

    def save(self):
        data = {
            'workers': [
                {'name': w.name, 'directory': w.directory,
                 'task': w.task, 'ipc_dir': w.ipc_dir,
                 'groups': w.groups}
                for w in self.workers.values()
            ],
            'groups': self.groups,
        }
        with open(self.session_file, 'w') as f:
            json.dump(data, f, indent=2)

    def get_ipc(self, worker_name: str) -> IPC:
        w = self.workers[worker_name]
        return IPC.connect(w.ipc_dir)

    def send_to_worker(self, worker_name: str, message: str, sender: str = 'foreman'):
        ipc = self.get_ipc(worker_name)
        ipc.send_input(message, sender=sender)

    def send_to_group(self, group_name: str, message: str, sender: str = 'foreman'):
        members = self.groups.get(group_name, [])
        for name in members:
            self.send_to_worker(name, message, sender=sender)

    def broadcast(self, message: str, sender: str = 'foreman'):
        for name in self.workers:
            self.send_to_worker(name, message, sender=sender)

    def cleanup(self):
        shutil.rmtree(self.session_dir, ignore_errors=True)


# =============================================================================
# Multi-worker foreman
# =============================================================================

def run_multi_foreman(session_dir: str):
    """Foreman for multi-worker session."""
    session = Session.load(session_dir)

    # Track file positions for each worker's events
    events_pos: dict[str, int] = {}
    for name, w in session.workers.items():
        events_path = os.path.join(w.ipc_dir, 'events.jsonl')
        events_pos[name] = 0

    C_RESET = '\033[0m'
    C_GRAY = '\033[90m'
    C_CYAN = '\033[36m'
    C_GREEN = '\033[32m'
    C_YELLOW = '\033[1;33m'
    C_RED = '\033[1;31m'
    C_BOLD = '\033[1m'
    C_MAGENTA = '\033[35m'

    # Worker name colors (cycle through)
    W_COLORS = ['\033[34m', '\033[35m', '\033[36m', '\033[33m',
                '\033[32m', '\033[91m', '\033[94m', '\033[95m']
    worker_color = {}
    for i, name in enumerate(session.workers):
        worker_color[name] = W_COLORS[i % len(W_COLORS)]

    n = len(session.workers)
    print(f"{C_BOLD}─── dedelulu foreman ─── {n} workers ───{C_RESET}")
    for name, w in session.workers.items():
        wc = worker_color[name]
        print(f"  {wc}[{name}]{C_RESET} {w.directory} — {w.task[:60]}")
    print()
    print(f"{C_GRAY}Commands: /send <worker> \"msg\"  /broadcast \"msg\"  /group create <name> <workers>")
    print(f"          /add <worker> <group>  /status  /focus <worker>  /help{C_RESET}")
    print()

    pending_escalation = None  # (worker_name, question)
    import select as sel

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
                try:
                    size = os.path.getsize(events_path)
                    if size <= events_pos[name]:
                        continue
                    with open(events_path) as f:
                        f.seek(events_pos[name])
                        new_lines = f.readlines()
                        events_pos[name] = f.tell()

                    wc = worker_color[name]
                    for line in new_lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        ts = ev.get('ts', '')
                        event = ev.get('event', '')

                        if event == 'hook_approve':
                            tool = ev.get('tool', '?')
                            print(f"  {C_GRAY}{ts}{C_RESET} {wc}[{name}]{C_RESET} {C_GREEN}✓{C_RESET} {tool}")

                        elif event == 'respond':
                            src = ev.get('source', '?')
                            resp = ev.get('response', '')
                            display = repr(resp) if resp else '↵'
                            count = ev.get('count', '?')
                            print(f"  {C_GRAY}{ts}{C_RESET} {wc}[{name}]{C_RESET} {C_CYAN}#{count}{C_RESET} sent {display} ({src})")

                        elif event == 'supervise' or event == 'hook_supervise':
                            status = ev.get('status', '?')
                            reasoning = ev.get('reasoning', '')
                            if status == 'on_track':
                                print(f"  {C_GRAY}{ts}{C_RESET} {wc}[{name}]{C_RESET} {C_GREEN}●{C_RESET} {C_GRAY}{reasoning}{C_RESET}")
                            else:
                                print(f"  {C_GRAY}{ts}{C_RESET} {wc}[{name}]{C_RESET} {C_YELLOW}● {status}{C_RESET} {reasoning}")

                        elif event == 'escalate':
                            question = ev.get('question', ev.get('reasoning', '?'))
                            print(f"\n  {C_YELLOW}{'─' * 50}")
                            print(f"  ⚠  {wc}[{name}]{C_YELLOW} NEEDS YOUR INPUT")
                            print(f"  {question}")
                            print(f"  {'─' * 50}{C_RESET}\n")
                            print('\a', end='', flush=True)
                            pending_escalation = (name, question)

                        elif event == 'intervene':
                            msg = ev.get('message', '')
                            print(f"  {C_GRAY}{ts}{C_RESET} {wc}[{name}]{C_RESET} {C_RED}▶ intervened{C_RESET} {msg[:60]}")

                        elif event == 'hook_post_tool':
                            tool = ev.get('tool', '?')
                            summary = ev.get('summary', '')
                            if summary:
                                print(f"  {C_GRAY}{ts}{C_RESET} {wc}[{name}]{C_RESET} {tool} {C_GRAY}{summary[:50]}{C_RESET}")

                        elif event in ('start', 'exit'):
                            print(f"  {C_GRAY}{ts}{C_RESET} {wc}[{name}]{C_RESET} {C_GRAY}[{event}]{C_RESET}")

                except Exception:
                    pass

            # Check for user input (commands or escalation responses)
            try:
                r, _, _ = sel.select([sys.stdin], [], [], 0.3)
                if r:
                    raw = sys.stdin.readline().strip()
                    if not raw:
                        pass
                    elif raw.startswith('/'):
                        _handle_command(raw, session, worker_color, C_RESET, C_CYAN, C_GREEN, C_YELLOW, C_RED, C_GRAY, C_BOLD)
                    elif pending_escalation:
                        worker_name = pending_escalation[0]
                        session.send_to_worker(worker_name, raw)
                        wc = worker_color[worker_name]
                        print(f"  {C_CYAN}→ {wc}[{worker_name}]{C_RESET} {raw}{C_RESET}\n")
                        pending_escalation = None
                    else:
                        print(f"  {C_GRAY}(use /send <worker> \"msg\" or /help){C_RESET}")
            except Exception:
                time.sleep(0.3)

    except KeyboardInterrupt:
        print(f"\n{C_GRAY}Foreman stopped.{C_RESET}")


def _handle_command(raw: str, session: Session, worker_color: dict,
                    C_RESET, C_CYAN, C_GREEN, C_YELLOW, C_RED, C_GRAY, C_BOLD):
    """Handle foreman slash commands."""
    parts = raw.split(None, 2)
    cmd = parts[0].lower()

    if cmd == '/help':
        print(f"""
  {C_BOLD}Foreman commands:{C_RESET}
    /send <worker|group> "message"  — send message to worker or group
    /broadcast "message"            — send to all workers
    /group create <name> <w1> <w2>  — create a group
    /add <worker> <group>           — add worker to group
    /remove <worker> <group>        — remove from group
    /groups                         — list groups
    /status                         — worker status overview
    /focus <worker>                 — switch tmux to worker pane
    /log <worker>                   — last 15 events from worker
    /stale <worker|all> [secs]     — enable stale nudge (default 300s, 0=off)
    /hide                           — minimize foreman pane
    /show                           — restore foreman pane
    Prefix+F                        — toggle foreman pane (from any pane)
    Prefix+f                        — focus foreman pane
    /help                           — this help
""")

    elif cmd == '/send' and len(parts) >= 3:
        target = parts[1]
        # Extract message (handle quotes)
        msg = parts[2].strip('"\'')
        if target in session.workers:
            session.send_to_worker(target, msg)
            wc = worker_color.get(target, '')
            print(f"  → {wc}[{target}]{C_RESET} 📨 delivered")
        elif target in session.groups:
            session.send_to_group(target, msg)
            members = session.groups[target]
            for m in members:
                wc = worker_color.get(m, '')
                print(f"  → {wc}[{m}]{C_RESET} 📨 delivered (group: {target})")
        else:
            print(f"  {C_RED}unknown worker or group: {target}{C_RESET}")

    elif cmd == '/broadcast' and len(parts) >= 2:
        msg = ' '.join(parts[1:]).strip('"\'')
        session.broadcast(msg)
        print(f"  → all {len(session.workers)} workers 📨 delivered")

    elif cmd == '/group' and len(parts) >= 2:
        sub_parts = parts[1] if len(parts) == 2 else parts[1] + ' ' + parts[2]
        sub_parts = sub_parts.split()
        if sub_parts[0] == 'create' and len(sub_parts) >= 3:
            group_name = sub_parts[1]
            members = sub_parts[2:]
            valid = [m for m in members if m in session.workers]
            session.groups[group_name] = valid
            session.save()
            print(f"  {C_GREEN}✓{C_RESET} group '{group_name}' → {', '.join(valid)}")
        else:
            print(f"  {C_GRAY}usage: /group create <name> <worker1> <worker2> ...{C_RESET}")

    elif cmd == '/add' and len(parts) >= 3:
        worker_name, group_name = parts[1], parts[2]
        if worker_name not in session.workers:
            print(f"  {C_RED}unknown worker: {worker_name}{C_RESET}")
        else:
            if group_name not in session.groups:
                session.groups[group_name] = []
            if worker_name not in session.groups[group_name]:
                session.groups[group_name].append(worker_name)
                session.save()
            print(f"  {C_GREEN}✓{C_RESET} group '{group_name}' → {', '.join(session.groups[group_name])}")

    elif cmd == '/remove' and len(parts) >= 3:
        worker_name, group_name = parts[1], parts[2]
        if group_name in session.groups and worker_name in session.groups[group_name]:
            session.groups[group_name].remove(worker_name)
            session.save()
            print(f"  {C_GREEN}✓{C_RESET} removed {worker_name} from '{group_name}'")
        else:
            print(f"  {C_RED}not found{C_RESET}")

    elif cmd == '/groups':
        if not session.groups:
            print(f"  {C_GRAY}no groups{C_RESET}")
        for gname, members in session.groups.items():
            print(f"  {C_BOLD}{gname}{C_RESET}: {', '.join(members)}")

    elif cmd == '/status':
        print(f"  {C_BOLD}{'worker':<12} {'status':<10} {'groups':<15} {'task'}{C_RESET}")
        print(f"  {'─'*12} {'─'*10} {'─'*15} {'─'*30}")
        for name, w in session.workers.items():
            ipc = IPC(w.ipc_dir)
            alive = ipc.worker_alive()
            status = f"{C_GREEN}● active{C_RESET}" if alive else f"{C_GRAY}○ exited{C_RESET}"
            grps = [g for g, ms in session.groups.items() if name in ms and g != 'all']
            grps_str = ', '.join(grps) if grps else C_GRAY + '—' + C_RESET
            wc = worker_color.get(name, '')
            print(f"  {wc}{name:<12}{C_RESET} {status:<20} {grps_str:<15} {w.task[:30]}")

    elif cmd == '/focus' and len(parts) >= 2:
        worker_name = parts[1]
        names = list(session.workers.keys())
        if worker_name in names:
            idx = names.index(worker_name)
            # tmux select-pane
            subprocess.run(['tmux', 'select-pane', '-t', str(idx)],
                           capture_output=True)
            print(f"  {C_CYAN}focused on {worker_name}{C_RESET}")
        else:
            print(f"  {C_RED}unknown worker: {worker_name}{C_RESET}")

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

    elif cmd == '/stale':
        if len(parts) < 2:
            print(f"  {C_GRAY}usage: /stale <worker|all> [secs]  (default 300, 0=off){C_RESET}")
        else:
            target = parts[1]
            secs = float(parts[2]) if len(parts) >= 3 else 300.0
            targets = list(session.workers.keys()) if target == 'all' else [target]
            for name in targets:
                if name not in session.workers:
                    print(f"  {C_RED}unknown worker: {name}{C_RESET}")
                    continue
                w = session.workers[name]
                ipc = IPC(w.ipc_dir)
                ipc.send_input('', command='stale', value=str(secs))
                wc = worker_color.get(name, '')
                label = f'{int(secs)}s' if secs > 0 else 'off'
                print(f"  {wc}[{name}]{C_RESET} stale nudge → {label}")

    elif cmd == '/hide':
        subprocess.run(['tmux', 'resize-pane', '-y', '2'], capture_output=True)
        print(f"  {C_GRAY}foreman minimized (/show to restore){C_RESET}")

    elif cmd == '/show':
        subprocess.run(['tmux', 'resize-pane', '-y', '35%'], capture_output=True)
        print(f"  {C_GREEN}foreman restored{C_RESET}")

    else:
        print(f"  {C_GRAY}unknown command. /help for list{C_RESET}")


# =============================================================================
# tmux launcher for multi-worker
# =============================================================================

def launch_multi_tmux(session: Session, extra_args: list[str]):
    """Launch multi-worker tmux session."""
    tmux = shutil.which('tmux')
    if not tmux:
        print("[dedelulu] tmux required for multi-worker mode")
        sys.exit(1)

    session_name = f'dedelulu-multi-{os.getpid()}'
    dedelulu_bin = os.path.abspath(
        os.path.join(os.path.dirname(__file__), 'dedelulu.py'))
    python = sys.executable

    workers = list(session.workers.values())

    # Build worker commands
    worker_cmds = []
    for w in workers:
        cmd_parts = [
            python, dedelulu_bin,
            '--ipc-dir', w.ipc_dir,
            '--no-tmux',
        ] + extra_args + [
            'claude', w.task,
        ]
        worker_cmds.append((w, ' '.join(shlex.quote(a) for a in cmd_parts)))

    # Foreman command
    multi_bin = os.path.abspath(__file__)
    foreman_cmd = f'{shlex.quote(python)} {shlex.quote(multi_bin)} --foreman {shlex.quote(session.session_dir)}'

    # Create tmux session with first worker
    first_cmd = f'cd {shlex.quote(workers[0].directory)} && {worker_cmds[0][1]}'
    subprocess.run([
        tmux, 'new-session', '-d', '-s', session_name,
        '-x', '220', '-y', '60',
        'bash', '-c', first_cmd,
    ])

    # Add remaining worker panes (split horizontally for 2-column grid)
    for i, (w, cmd) in enumerate(worker_cmds[1:], 1):
        target_pane = 0 if i % 2 == 1 else i - 1
        split_dir = '-h' if i % 2 == 1 else '-v'
        full_cmd = f'cd {shlex.quote(w.directory)} && {cmd}'
        subprocess.run([
            tmux, 'split-window', split_dir,
            '-t', f'{session_name}:{0}.{target_pane}',
            'bash', '-c', full_cmd,
        ])

    # Add foreman pane at the bottom
    subprocess.run([
        tmux, 'split-window', '-v',
        '-t', f'{session_name}:{0}',
        '-l', '35%',
        'bash', '-c', foreman_cmd,
    ])

    # Even out the layout
    subprocess.run([tmux, 'select-layout', '-t', session_name, 'tiled'])

    # Focus on foreman (last pane)
    total_panes = len(workers) + 1
    foreman_pane = total_panes - 1
    subprocess.run([tmux, 'select-pane', '-t', f'{session_name}:{0}.{foreman_pane}'])

    # Keybindings for foreman panel toggle (works from any pane)
    # Prefix+F = toggle foreman: if foreman is tiny → restore, else → minimize
    # Prefix+f = focus/select foreman pane
    toggle_cmd = (
        f'if [ "$(tmux display -p -t {session_name}:{0}.{foreman_pane} "#{{pane_height}}")" -le 3 ]; then '
        f'tmux resize-pane -t {session_name}:{0}.{foreman_pane} -y 35%; '
        f'else '
        f'tmux resize-pane -t {session_name}:{0}.{foreman_pane} -y 2; '
        f'fi'
    )
    subprocess.run([tmux, 'bind-key', '-T', 'prefix', 'F',
                    'run-shell', toggle_cmd], capture_output=True)
    subprocess.run([tmux, 'bind-key', '-T', 'prefix', 'f',
                    'select-pane', '-t', f'{session_name}:{0}.{foreman_pane}'],
                   capture_output=True)

    # Attach
    os.execvp(tmux, [tmux, 'attach-session', '-t', session_name])


# =============================================================================
# CLI
# =============================================================================

def main():
    # Foreman subcommand
    if len(sys.argv) >= 3 and sys.argv[1] == '--foreman':
        run_multi_foreman(sys.argv[2])
        sys.exit(0)

    import argparse

    parser = argparse.ArgumentParser(
        prog='dedelulu multi',
        description='Multi-worker orchestration for dedelulu',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  dedelulu-multi \\
    --worker "auth:~/project:implement JWT authentication" \\
    --worker "tests:~/project:write tests for auth module"

  dedelulu-multi \\
    --worker "backend:~/project/backend:REST API for users" \\
    --worker "frontend:~/project/frontend:React UI for users" \\
    --worker "tests:~/project:e2e tests" \\
    --provider ollama
        """
    )

    parser.add_argument('--worker', action='append', required=True,
                        metavar='"name:dir:task"',
                        help='worker spec as "name:directory:task description" (repeat for each worker)')
    parser.add_argument('--provider',
                        choices=['none', 'claude-cli', 'anthropic', 'ollama', 'openai', 'azure'],
                        default='none',
                        help='LLM provider for supervisor')
    parser.add_argument('--model', help='specific model for supervisor')
    parser.add_argument('--idle', type=float, default=4.0)
    parser.add_argument('--supervise', type=float, default=0, metavar='SECS')
    parser.add_argument('--stale', type=float, default=0, metavar='SECS',
                        help='seconds of inactivity before nudging stale agent (default: 0=off)')
    parser.add_argument('--no-hooks', action='store_true')
    parser.add_argument('--log', default='dedelulu.jsonl')
    parser.add_argument('--no-log', action='store_true')

    args = parser.parse_args()

    # Parse worker specs
    workers = []
    for spec in args.worker:
        parts = spec.split(':', 2)
        if len(parts) != 3:
            parser.error(f"worker spec must be 'name:directory:task', got: {spec}")
        name, directory, task = parts
        directory = os.path.expanduser(directory)
        if not os.path.isdir(directory):
            parser.error(f"directory does not exist: {directory}")
        workers.append(WorkerSpec(name=name, directory=directory, task=task))

    # Check for duplicate names
    names = [w.name for w in workers]
    if len(names) != len(set(names)):
        parser.error("worker names must be unique")

    # Create session
    session = Session.create(workers)

    # Build extra args to pass to each worker
    extra_args = []
    if args.provider != 'none':
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

    # Launch tmux
    launch_multi_tmux(session, extra_args)


if __name__ == '__main__':
    main()
