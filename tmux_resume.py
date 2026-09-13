#!/usr/bin/env python3
"""Save and restore Linux tmux workspaces without replaying shell history."""
from __future__ import annotations

import argparse
import base64
import copy
from contextlib import contextmanager, ExitStack
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import pty
import pwd
import re
import select
import shlex
import shutil
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'tmux-resume'
LEGACY_NAME = 'tmux-travel'
LEGACY_STEM = 'tmux_travel'
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)
SHELLS = {'bash', 'zsh', 'fish', 'sh', 'dash', 'ksh'}
EDITORS = {'vim', 'vim.basic', 'vim.tiny', 'vi', 'nvim', 'view', 'gvim'}
EMACS = {'emacs', 'emacs-nox', 'emacsclient'}
AGENTS = {'codex', 'codex-rc', 'claude', 'claude-mixed'}
ENV_KEYS = {
    'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL',
    'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'CLAUDE_ROUTER_PORT', 'MIXED_MAIN_MODEL',
    'OPENROUTER_KEY_FILE', 'CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT',
    'CLAUDE_CODE_DISABLE_CLAUDE_MDS', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY',
}


def run(args, **kw):
    return subprocess.run([str(x) for x in args], text=True, capture_output=True, **kw)


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_json(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, indent=2, ensure_ascii=True)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def history_limit(override=None):
    config_path = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'tmux-resume/config.json'
    value = 100
    if override is None and config_path.exists():
        config = read_json(config_path)
        if not isinstance(config, dict):
            raise RuntimeError(f'Invalid JSON configuration: {config_path}')
        value = config.get('keep_checkpoints', value)
    elif override is not None:
        value = override
    if type(value) is not int or value < 0:
        raise RuntimeError('keep_checkpoints / --keep must be a nonnegative integer (0 keeps everything)')
    return value


@contextmanager
def checkpoint_lock():
    private_dir(STATE)
    # Saves and retention share a lock; a shutdown save waits for an earlier
    # checkpoint to finish instead of competing over latest or deleting it.
    with (STATE / 'checkpoint.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def checkpoint_history():
    entries = []
    if not STATE.is_dir():
        return entries
    for directory in STATE.iterdir():
        if (not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{6}', directory.name) or
                directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.getuid()):
            continue
        data = read_json(directory / 'snapshot.json')
        if (not isinstance(data, dict) or data.get('version') not in {1, 2} or
                not isinstance(data.get('created'), str) or data.get('history_managed') is False):
            continue
        path = directory
        if data.get('final_checkpoint') == 'close-check' or (directory / 'close-check/close.log').is_file():
            path = directory / 'close-check'
        try:
            path, final = find_snapshot(str(path))
            parts = snapshot_parts(path, final)
            if not parts or any(not p.is_relative_to(directory.resolve()) for p, _ in parts):
                continue
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
            continue  # Incomplete or unfamiliar data must never be pruned.
        entries.append((directory, path, parts))
    return sorted(entries, key=lambda entry: ((read_json(entry[0] / 'snapshot.json') or {}).get('created', entry[0].name), entry[0].name), reverse=True)


def protected_checkpoints(entries):
    protected = set()
    paths = [(STATE / 'latest').resolve()]
    scripts = runner_scripts()
    for proc in processes().values():
        argv = proc['argv']
        if (len(argv) >= 4 and Path(argv[0]).name.startswith('python') and
                Path(argv[1]).resolve() in scripts and argv[2] in {'_run', '_close-all', '_close'}):
            index = 4 if argv[2] == '_close' else 3
            if len(argv) > index:
                paths.append(Path(argv[index]).resolve())
    receipts = {tuple(record['workspace']) for record in active_restores()}
    for directory, _, parts in entries:
        if any(p.is_relative_to(directory.resolve()) for p in paths):
            protected.add(directory)
        root_path, root_data = find_snapshot(str(directory))
        receipt_parts = parts + snapshot_parts(root_path, root_data)
        if any(tuple(workspace_key(data, session)) in receipts
               for _, data in receipt_parts for session in data['sessions']):
            protected.add(directory)
    return protected


def prune_history(keep):
    if keep == 0:
        return
    entries = checkpoint_history()
    if len(entries) <= keep:
        return
    # Do not remove assets while a restore is building its panes/receipts.
    try:
        with restore_lock():
            protected = protected_checkpoints(entries)
            for directory, _, _ in entries[keep:]:
                if directory not in protected:
                    shutil.rmtree(directory)
                    print(f'Pruned checkpoint: {directory}')
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        print(f'WARNING: Checkpoint saved, but history pruning was deferred: {error}', file=sys.stderr)


def show_history():
    with checkpoint_lock():
        entries = checkpoint_history()
        latest = (STATE / 'latest').resolve()
        print(f'Checkpoint history: {STATE} (keep {history_limit() or "all"})')
        for directory, path, parts in entries:
            sessions = sum(len(data['sessions']) for _, data in parts)
            marker = '*' if latest.is_relative_to(directory.resolve()) else ' '
            print(f'{marker} {directory.name}: {len(parts)} servers, {sessions} sessions  {path}')
        if not entries:
            print('No completed automatic checkpoints.')
        if latest.exists() and not any(latest.is_relative_to(d.resolve()) for d, _, _ in entries):
            print(f'* Latest checkpoint outside managed history: {latest}')


class Tmux:
    def __init__(self, socket=None):
        self.prefix = ['tmux'] + (['-S', socket] if socket else [])

    def call(self, *args, allow_fail=False):
        r = run(self.prefix + list(args))
        if r.returncode and not allow_fail:
            raise RuntimeError(r.stderr.strip() or 'tmux command failed')
        return r.stdout.rstrip('\n')

    def rows(self, command, fields, *args):
        # tmux's q modifier is shell escaping, not JSON escaping. Dedicated
        # separators preserve spaces, quotes, tabs and newlines verbatim.
        delimiter = '__tmux_resume_' + uuid.uuid4().hex + '__'
        end = '__tmux_resume_end_' + uuid.uuid4().hex + '__'
        fmt = delimiter.join('#{' + x + '}' for x in fields) + end
        output = self.call(command, *args, '-F', fmt)
        rows = []
        for record in output.split(end):
            record = record.removeprefix('\n')
            if not record:
                continue
            # tmux's output sanitizer adds a backslash before dollar signs.
            values = [x.replace('\\$', '$') for x in record.split(delimiter)]
            if len(values) != len(fields):
                raise RuntimeError('tmux name/path contains an unsupported record separator')
            rows.append(dict(zip(fields, values)))
        return rows


def named_socket(name):
    if not name or name in {'.', '..'} or '/' in name:
        raise RuntimeError('A tmux server name must be a nonempty filename without slashes')
    return str(Path(os.environ.get('TMUX_TMPDIR') or '/tmp').absolute() / f'tmux-{os.getuid()}' / name)


def server_name(socket):
    path = Path(socket)
    return path.name if path.parent.name == f'tmux-{os.getuid()}' else None


def discover_servers():
    """Find this user's live tmux listeners, including arbitrary -S paths."""
    candidates, expected = set(), set()
    directories = {Path('/tmp') / f'tmux-{os.getuid()}', Path(named_socket('default')).parent}
    current = os.environ.get('TMUX', '').split(',', 1)[0]
    if current:
        candidates.add(current)
    # Match tmux's open listening FDs with kernel UNIX-socket metadata. This
    # also finds detached servers whose pane processes have unset TMUX.
    listeners = {}
    for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
        fields = line.split(None, 7)
        if len(fields) == 8 and fields[3] == '00010000' and fields[7].startswith('/'):
            listeners['socket:[' + fields[6] + ']'] = fields[7]
    for proc in Path('/proc').iterdir():
        try:
            if not proc.name.isdigit() or proc.stat().st_uid != os.getuid():
                continue
            comm = (proc / 'comm').read_text().strip()
            if comm != 'tmux: server':
                continue
            expected.add(proc.name)
            candidates.update(listeners[fd] for fd in fds(int(proc.name)) if fd in listeners)
            env = proc_env(int(proc.name))
            if env.get('TMUX_TMPDIR'):
                directories.add(Path(env['TMUX_TMPDIR']) / f'tmux-{os.getuid()}')
        except (OSError, ValueError):
            continue
    for directory in directories:
        try:
            candidates.update(str(path) for path in directory.iterdir())
        except OSError:
            pass
    found, observed = {}, set()
    for socket in sorted(candidates):
        try:
            info = Path(socket).stat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                continue
            result = run(['tmux', '-S', socket, 'display-message', '-p', '#{socket_path}|#{pid}'], timeout=2)
            if result.returncode:
                continue
            actual, pid = result.stdout.strip().rsplit('|', 1)
            observed.add(pid)
            tmux = Tmux(actual)
            if tmux.call('list-sessions', '-F', '#{session_name}', allow_fail=True):
                found[pid] = tmux
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    # A known server that cannot be inspected must not silently disappear
    # from an all-server save/close operation. Ignore processes that exited.
    missing = [pid for pid in expected - observed if Path('/proc', pid, 'comm').exists()]
    if missing:
        raise RuntimeError('Cannot inspect tmux server PID(s) ' + ', '.join(sorted(missing)) +
                           '; check its socket, or use --socket/--server to select a reachable server')
    return sorted(found.values(), key=lambda tmux: tmux.prefix[2])


def processes():
    result = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            raw_argv = (path / 'cmdline').read_bytes().split(b'\0')
            if raw_argv[-1] == b'':
                raw_argv.pop()  # Remove the terminator, preserving empty arguments.
            argv = [os.fsdecode(x) for x in raw_argv]
            result[int(path.name)] = dict(pid=int(path.name), ppid=int(stat[1]),
                pgid=int(stat[2]), tty=int(stat[4]), tpgid=int(stat[5]),
                start=stat[19], argv=argv, cwd=os.readlink(path / 'cwd'))
        except (OSError, ValueError):
            continue
    return result


def proc_env(pid):
    try:
        return dict(x.split('=', 1) for x in Path(f'/proc/{pid}/environ').read_text().split('\0') if '=' in x)
    except OSError:
        return {}


def fds(pid):
    result = []
    try:
        for p in Path(f'/proc/{pid}/fd').iterdir():
            try:
                result.append(os.readlink(p))
            except OSError:
                pass
    except OSError:
        pass
    return result


def program(proc):
    argv = proc['argv']
    if not argv:
        return ''
    name = Path(argv[0]).name.lstrip('-')
    if name == 'emacsclient.emacs':
        return 'emacsclient'
    if re.fullmatch(r'emacs(?:-gtk|-lucid|-[0-9]+(?:\.[0-9]+)*|\.nox)', name):
        return 'emacs'
    if name in {'node', 'nodejs', 'python3', 'bash', 'sh'} and len(argv) > 1:
        script = Path(argv[1]).name
        if script in AGENTS or script in {'codex.js', 'cli.js'}:
            if script == 'cli.js' and 'claude' not in argv[1]:
                return name
            return 'codex' if script == 'codex.js' else 'claude' if script == 'cli.js' else script
    return name


def children(procs, root):
    found = []
    todo = [root]
    while todo:
        pid = todo.pop(0)
        if pid in procs:
            found.append(procs[pid])
        todo.extend(p['pid'] for p in procs.values() if p['ppid'] == pid)
    return found


def idle_shell(proc):
    argv = proc.get('argv', [])
    if program(proc) not in SHELLS:
        return False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in {'--rcfile', '--init-file', '-o', '+o', '-O', '+O'}:
            i += 2
            continue
        if (not arg.startswith(('-', '+')) or arg == '--command' or
                (arg.startswith('-') and not arg.startswith('--') and 'c' in arg[1:])):
            return False
        i += 1
    return True


def runner_scripts():
    scripts = {ROOT / 'tmux_resume.py'}
    installed = shutil.which('tmux-resume')
    if installed:
        scripts.add(Path(installed).resolve())
    return {path.resolve() for path in scripts}


def resume_runner(proc):
    argv = proc.get('argv', [])
    if len(argv) != 6 or not Path(argv[0]).name.startswith('python') or argv[2] != '_run':
        return False
    return Path(argv[1]).resolve() in runner_scripts()


def prefill_process(pane):
    """Choose the outer foreground job, with a fallback for older snapshots."""
    r = pane['restore']
    if r.get('argv') or r.get('kind') == 'pm':
        return None
    if r.get('original_argv'):
        return {'argv': r['original_argv'], 'cwd': r['cwd']}
    jobs = [p for p in pane.get('processes', []) if p.get('argv') and not idle_shell(p)]
    foreground = [p for p in jobs if p.get('pgid', 0) > 0 and p['pgid'] == p.get('tpgid')]
    return next(iter(foreground or jobs), None)


def quote_prefill(arg):
    # Keep control characters out of both the terminal and the command line.
    # Bash expands these ANSI-C escapes only after the user submits the line.
    if any(ord(c) < 32 or ord(c) == 127 for c in arg):
        escaped = ''.join(f'\\x{ord(c):02x}' if ord(c) < 32 or ord(c) == 127
                          else '\\' + c if c in "\\'" else c for c in arg)
        return "$'" + escaped + "'"
    return shlex.quote(arg)


def prefill_command(pane):
    proc = prefill_process(pane)
    if not proc:
        return ''
    command = ' '.join(quote_prefill(arg) for arg in proc['argv'])
    cwd = proc.get('cwd', pane['restore']['cwd'])
    if cwd != pane['restore']['cwd']:
        command = 'cd -- ' + quote_prefill(cwd) + ' && ' + command
    return command


# Explicit arities stop prompts, quoted option values, and subcommands being
# confused with each other. Unknown flags are recorded but block automatic replay.
CODEX_VALUES = set('config c enable disable model m profile p sandbox s ask-for-approval a cd C add-dir remote remote-auth-token-env local-provider oss-provider'.split())
CODEX_BOOLS = set('search no-alt-screen oss yolo dangerously-bypass-approvals-and-sandbox dangerously-bypass-hook-trust full-auto approve-for-me strict-config ephemeral'.split())
CLAUDE_VALUES = set('model effort permission-mode agent agents append-system-prompt system-prompt system-prompt-file append-system-prompt-file autocompact fallback-model name n settings setting-sources output-format input-format json-schema max-budget-usd max-turns debug-file environment remote-control-session-name-prefix system-prompt-snapshot'.split())
CLAUDE_BOOLS = set('dangerously-skip-permissions allow-dangerously-skip-permissions verbose chrome no-chrome strict-mcp-config disable-slash-commands ide brief bare safe-mode restricted ax-screen-reader include-partial-messages replay-user-messages forward-subagent-text exclude-dynamic-system-prompt-sections'.split())
CLAUDE_MULTI = set('add-dir allowedTools allowed-tools disallowedTools disallowed-tools tools mcp-config plugin-dir betas'.split())


def resume_options(argv, kind):
    """Return original behavior options; never re-submit the startup prompt."""
    codex = kind.startswith('codex')
    values = CODEX_VALUES if codex else CLAUDE_VALUES
    bools = CODEX_BOOLS if codex else CLAUDE_BOOLS
    multi = set() if codex else CLAUDE_MULTI
    kept, dropped = [], []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            dropped += argv[i:]
            break
        if not arg.startswith('-'):
            if codex and arg in {'exec', 'e', 'review', 'app-server', 'remote-control', 'mcp'}:
                raise ValueError('non-interactive Codex command')
            dropped.append(arg)
            i += 1
            continue
        flag = arg.lstrip('-').split('=', 1)[0]
        inline = '=' in arg
        # Common attached short values: -mMODEL, -cKEY=VALUE, -C/path.
        if arg.startswith('-') and not arg.startswith('--') and len(arg) > 2 and arg[1] in values:
            kept.append(arg)
            i += 1
            continue
        remove_optional = {'r', 'resume', 'w', 'worktree', 'from-pr', 'teleport'} if not codex else set()
        remove_value = {'session-id'} if not codex else {'i', 'image'}
        remove_bool = {'c', 'continue', 'fork-session'} if not codex else {'last', 'all', 'include-non-interactive', 'worktree'}
        if flag in remove_optional | remove_value | remove_bool:
            dropped.append(arg)
            i += 1
            if not inline and flag in remove_value | remove_optional and i < len(argv) and not argv[i].startswith('-'):
                dropped.append(argv[i])
                i += 1
            continue
        if flag in values:
            kept.append(arg)
            i += 1
            if not inline:
                if i == len(argv):
                    raise ValueError(f'missing value for {arg}')
                kept.append(argv[i])
                i += 1
        elif flag in bools:
            kept.append(arg)
            i += 1
        elif flag in multi or (not codex and flag in {'debug', 'd', 'remote-control'}):
            kept.append(arg)
            i += 1
            if not inline:
                while i < len(argv) and not argv[i].startswith('-'):
                    kept.append(argv[i])
                    i += 1
                    if flag in {'debug', 'd', 'remote-control'}:
                        break
        else:
            raise ValueError(f'unrecognized option {arg}; original arguments retained for manual recovery')
    return kept, dropped


def normalize(text):
    return re.sub(r'[^\w]', '', text).lower()


def remote_resume_arguments(argv):
    """Remote tasks retain their stored permissions and reject CLI overrides."""
    if not any(arg == '--remote' or arg.startswith('--remote=') for arg in argv):
        return argv[:], []
    value_flags = {'--sandbox', '-s', '--ask-for-approval', '-a', '--add-dir'}
    bool_flags = {'--yolo', '--dangerously-bypass-approvals-and-sandbox', '--full-auto', '--approve-for-me'}
    policy_keys = {'sandbox_mode', 'approval_policy', 'permissions', 'sandbox_workspace_write'}
    kept, dropped = [], []
    i = 0
    while i < len(argv):
        arg = argv[i]
        flag = arg.split('=', 1)[0]
        count = 1
        omit = flag in bool_flags or flag in value_flags
        if flag in value_flags and '=' not in arg:
            count = 2
        elif arg.startswith(('-s', '-a')) and not arg.startswith('--') and len(arg) > 2:
            omit = True
        if arg in {'-c', '--config'} or arg.startswith(('-c', '--config=')):
            if arg in {'-c', '--config'}:
                count = 2
                config = argv[i + 1] if i + 1 < len(argv) else ''
            else:
                config = arg[2:] if arg.startswith('-c') else arg.split('=', 1)[1]
            key = config.split('=', 1)[0].strip().split('.', 1)[0]
            omit = key in policy_keys
        (dropped if omit else kept).extend(argv[i:i + count])
        i += count
    return kept, dropped


def prepare_remote_resumes(data):
    for window in data['windows'].values():
        for pane in window['panes']:
            record = pane['restore']
            if not record['kind'].startswith('codex') or not record.get('argv'):
                continue
            argv, dropped = remote_resume_arguments(record['argv'])
            if dropped:
                record.setdefault('original_resume_argv', record['argv'][:])
                record['argv'] = argv
                record['remote_permission_arguments'] = dropped
            if record.get('remote_permission_arguments'):
                message = (f"{pane['location']}: remote Codex uses the task's stored permissions; "
                           "unsupported permission override arguments are retained in the snapshot instead of passed to resume")
                if message not in data['warnings']:
                    data['warnings'].append(message)


class CodexIndex:
    def __init__(self):
        self.cache = {}

    def transcripts(self, home, cwd):
        key = (str(home), cwd)
        if key in self.cache:
            return self.cache[key]
        found = []
        for db in sorted(home.glob('state_*.sqlite'), reverse=True):
            try:
                with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as c:
                    rows = c.execute('SELECT id, rollout_path FROM threads WHERE cwd=? AND archived=0', (cwd,)).fetchall()
                for sid, path in rows:
                    messages = []
                    try:
                        with open(path) as f:
                            for line in f:
                                try:
                                    d = json.loads(line)
                                except ValueError:
                                    continue
                                p = d.get('payload', {})
                                if d.get('type') == 'response_item' and p.get('type') == 'message' and p.get('role') == 'assistant':
                                    messages.append(normalize(''.join(x.get('text', '') for x in p.get('content', []) if isinstance(x, dict))))
                    except OSError:
                        continue
                    found.append((sid, path, messages))
                break
            except sqlite3.Error:
                continue
        self.cache[key] = found
        return found

    def identify(self, proc, env, screen):
        home = Path(env.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
        # Match substantial, visible assistant text against every conversation
        # in this directory. Never use the latest conversation by cwd.
        needles = [normalize(x) for x in screen.splitlines() if len(normalize(x)) >= 55][-300:]
        transcripts = self.transcripts(home, proc['cwd'])
        matches = []
        for sid, path, messages in transcripts:
            hits = {n for n in needles if any(n in m for m in messages)}
            if sum(map(len, hits)) >= 110:
                matches.append((sid, path, hits))
        if len(matches) == 1:
            return matches[0][0], 'unique visible assistant transcript', matches[0][1]
        if len(matches) > 1:
            # Shared boilerplate can make several transcripts pass the size
            # threshold. Accept one only when it alone also has distinguishing
            # screen text; mixed evidence from two threads remains ambiguous.
            specific = [(sid, path) for sid, path, hits in matches
                        if hits - set().union(*(other_hits for other_sid, _, other_hits in matches if other_sid != sid))]
            if len(specific) == 1:
                return specific[0][0], 'unique distinguishing assistant transcript', specific[0][1]
        # Local CLI versions may keep their current rollout open.
        opened = [(UUID.findall(p)[-1], p) for p in fds(proc['pid']) if 'rollout-' in p and p.endswith('.jsonl') and UUID.search(p)]
        if len(set(x[0] for x in opened)) == 1:
            return opened[0][0], 'open rollout file', opened[0][1]
        # PID-scoped TUI history events identify the displayed thread; reject
        # conflicting IDs instead of treating other threads as interchangeable.
        for db in sorted(home.glob('logs_*.sqlite'), reverse=True):
            try:
                # PID numbers can be reused after processes exit or reboot.
                boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime '))
                started = boot + int(proc['start']) / os.sysconf('SC_CLK_TCK')
                with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as c:
                    ids = {r[0] for r in c.execute("SELECT DISTINCT thread_id FROM logs WHERE process_uuid LIKE ? AND ts>=? AND target IN ('codex_tui::app::history_pagination', 'codex_tui::app::resize_reflow') AND thread_id IS NOT NULL", (f"pid:{proc['pid']}:%", int(started)))}
                if len(ids) == 1:
                    sid = ids.pop()
                    if not matches or sid in {m[0] for m in matches}:
                        return sid, 'PID-scoped TUI history log', next((p for s, p, _ in transcripts if s == sid), None)
                break
            except sqlite3.Error:
                continue
        return None, 'cannot determine the selected conversation', None


def claude_id(proc, env):
    home = Path(env.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
    d = read_json(home / 'sessions' / f"{proc['pid']}.json")
    if d and d.get('pid') == proc['pid'] and str(d.get('procStart', proc['start'])) == proc['start'] and UUID.fullmatch(d.get('sessionId', '')):
        sid = d['sessionId']
        return sid, 'Claude PID session registry', next((str(p) for p in (home / 'projects').glob(f'*/{sid}.jsonl')), None)
    ids = set()
    for path in fds(proc['pid']):
        if '/claude-' in path and '/tasks' in path:
            ids.update(UUID.findall(path))
    if len(ids) == 1:
        sid = ids.pop()
        return sid, 'open Claude task directory', next((str(p) for p in (home / 'projects').glob(f'*/{sid}.jsonl')), None)
    return None, 'cannot determine the selected conversation', None


def vim_checkpoint(proc, destination, editor='vim'):
    live = STATE / editor
    status_path = live / f"{proc['pid']}.json"
    status = read_json(status_path)
    if not status or str(status.get('start')) != proc['start']:
        return None
    nonce = uuid.uuid4().hex
    request = live / f"{proc['pid']}.request.json"
    write_json(request, {'nonce': nonce, 'directory': str(destination)})
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = read_json(status_path)
        if status and status.get('nonce') == nonce:
            return status
        time.sleep(0.05)
    request.unlink(missing_ok=True)
    return None


def vim_swap_check(proc, destination):
    """Ask a separate, clean Vim to read headers of this process's open swaps."""
    paths = [x for x in fds(proc['pid']) if re.search(r'\.s[a-w][a-z]$', x)]
    results = []
    vim = shutil.which('vim')
    if not paths or not vim:
        return results
    request = destination / 'swap-paths.json'
    response = destination / 'swaps.json'
    script = destination / 'inspect-swaps.vim'
    write_json(request, paths)
    script.write_text('let paths = json_decode(join(readfile($TMUX_RESUME_SWAP_INPUT), "\\n"))\n'
                      "let results = map(paths, '{\"path\": v:val, \"info\": swapinfo(v:val)}')\n"
                      "call writefile([json_encode(results)], $TMUX_RESUME_SWAP_OUTPUT)\nqa!\n")
    env = dict(os.environ, TMUX_RESUME_SWAP_INPUT=str(request), TMUX_RESUME_SWAP_OUTPUT=str(response))
    try:
        r = run([vim, '-Nu', 'NONE', '-n', '-i', 'NONE', '-es', '-S', script], env=env, timeout=5)
        if r.returncode == 0:
            results = read_json(response) or []
    except subprocess.TimeoutExpired:
        pass
    for item in results:
        info = item.get('info', {})
        if info.get('pid') != proc['pid']:
            info['error'] = 'swap PID does not match the running editor'
    return results


def emacs_checkpoint(proc, destination):
    status = vim_checkpoint(proc, destination, editor='emacs')
    if status:
        return status
    client = shutil.which('emacsclient')
    if not client:
        return None
    argv = proc['argv']
    options = []
    for i, arg in enumerate(argv[1:], 1):
        if arg in {'-s', '--socket-name', '-f', '--server-file'} and i + 1 < len(argv):
            options = [arg, argv[i + 1]]
        elif arg.startswith(('--socket-name=', '--server-file=')):
            options = [arg]
    nonce = uuid.uuid4().hex
    response = destination / 'emacs-query.json'
    # A standalone Emacs must match the queried server's PID. For emacsclient,
    # use its explicit endpoint (or its normal default) and inspect that server.
    check = 't' if program(proc) == 'emacsclient' else f'(= (emacs-pid) {proc["pid"]})'
    quote = lambda text: json.dumps(str(text), ensure_ascii=False)
    expression = (f'(when {check} (load {quote(ROOT / "tmux-resume.el")} nil t) '
                  f'(tmux-resume-checkpoint {quote(destination)} {quote(nonce)}) '
                  f'(copy-file (concat (tmux-resume--base) ".json") {quote(response)} t))')
    env = os.environ.copy()
    source_env = proc_env(proc['pid'])
    for key in ('EMACS_SOCKET_NAME', 'EMACS_SERVER_FILE', 'XDG_RUNTIME_DIR'):
        if key in source_env:
            env[key] = source_env[key]
    try:
        result = run([client, *options, '--eval', expression], env=env, timeout=4)
    except subprocess.TimeoutExpired:
        return None
    status = read_json(response) if result.returncode == 0 else None
    return status if status and status.get('nonce') == nonce else None


def pm_tui(proc):
    argv = proc.get('argv', [])
    return any(Path(arg).name == 'pm' and argv[i + 1:i + 2] == ['_tui'] for i, arg in enumerate(argv))


def pm_workspace(session, data):
    if not session['session_name'].startswith('pm-'):
        return None
    for link in session['links']:
        for pane in data['windows'][link['id']]['panes']:
            proc = next((p for p in pane.get('processes', []) if pm_tui(p)), None)
            if pane['restore']['kind'] == 'pm' or proc:
                return dict(name=session['session_name'].split('~', 1)[0],
                            cwd=session.get('session_path') or (proc or pane['restore'])['cwd'],
                            window=link['id'], pane=pane['pane_id'])
    return None


def restore_sessions(data):
    # PM creates its own attachment session; saved ~N copies share the same
    # windows and must not be independently reconstructed.
    result, seen = [], set()
    for session in data['sessions']:
        workspace = pm_workspace(session, data)
        if not workspace:
            result.append(session)
        elif workspace['name'] not in seen:
            seen.add(workspace['name'])
            main = next((s for s in data['sessions'] if s['session_name'] == workspace['name']), session)
            result.append(dict(main, session_name=workspace['name']))
    return result


def pm_environment(tmux):
    env = os.environ.copy()
    for key in ('TMUX', 'TMUX_PANE', 'PM_PROJECT', 'PM_SHARE_MODE', 'PM_IN_TMUX_SESSION'):
        env.pop(key, None)
    socket = tmux.prefix[2] if len(tmux.prefix) > 1 else None
    if not socket:
        value = tmux.call('display-message', '-p', '#{socket_path}', allow_fail=True)
        socket = value or (os.environ.get('TMUX', '').split(',', 1)[0] or None)
    if socket:
        env['PM_TMUX_SOCKET'] = socket
    else:
        env.pop('PM_TMUX_SOCKET', None)
    if env.get('TERM', 'dumb') == 'dumb':
        env['TERM'] = 'xterm-256color'
    return env


def start_pm(tmux, workspace, path):
    """Run the real PM entrypoint, detaching only our temporary PTY client."""
    env = pm_environment(tmux)
    master, slave = pty.openpty()
    tty = os.ttyname(slave)
    proc = subprocess.Popen(['pm', 'session'], cwd=workspace['cwd'], env=env,
                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    detached = False
    try:
        with (path / ('pm-' + uuid.uuid4().hex[:8] + '.log')).open('wb') as log:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    try:
                        chunk = os.read(master, 65536)
                        log.write(chunk)
                    except OSError:
                        pass
                clients = tmux.call('list-clients', '-F', '#{client_tty}', allow_fail=True).splitlines()
                if tty in clients:
                    tmux.call('detach-client', '-t', tty)
                    detached = True
                if proc.poll() is not None:
                    if proc.returncode != 0 or not detached:
                        raise RuntimeError(f"pm session failed in {workspace['cwd']}; see {log.name}")
                    return tmux.call('display-message', '-p', '-t', '=' + workspace['name'], '#{session_id}')
            raise RuntimeError(f"Timed out starting pm session in {workspace['cwd']}; see {log.name}")
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def agent_record(proc, index, screen):
    kind = program(proc)
    argv = proc['argv'][:]
    if Path(argv[0]).name in {'node', 'nodejs', 'bash', 'sh', 'python3'}:
        argv = [kind] + argv[2:]
    env = proc_env(proc['pid'])
    # exec replaces the claude-mixed wrapper. Its routing environment survives.
    mixed_settings = str(Path.home() / '.config/claude-router/claude-settings.json')
    if kind == 'claude' and mixed_settings in argv and re.match(r'https?://(127\.0\.0\.1|localhost):', env.get('ANTHROPIC_BASE_URL', '')):
        kind = 'claude-mixed'
    record = dict(kind=kind, original_argv=argv, env={k: v for k, v in env.items() if k in ENV_KEYS}, unset=[], cwd=proc['cwd'])
    if kind.startswith('claude'):
        record['unset'] = [k for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN') if k not in env]
    sid, evidence, transcript = index.identify(proc, env, screen) if kind.startswith('codex') else claude_id(proc, env)
    record.update(session_id=sid, evidence=evidence, transcript=transcript)
    try:
        options, dropped = resume_options(argv, kind)
        record['omitted_startup_arguments'] = dropped
        if kind == 'claude-mixed' and '--settings' in options:
            i = options.index('--settings')
            if options[i + 1:i + 2] == [mixed_settings]:
                del options[i:i + 2]  # The wrapper supplies this once.
        executable = 'codex' if kind == 'codex-rc' else kind
        if sid:
            record['argv'] = [executable, 'resume', sid] + options if kind.startswith('codex') else [executable] + options + ['--resume', sid]
        else:
            record['problem'] = evidence
    except ValueError as e:
        record['problem'] = str(e)
    return record


def select_session_records(sessions, names):
    if not names:
        return sessions
    selected = set()
    available = {s['session_name'] for s in sessions}
    for name in names:
        if name not in available:
            raise RuntimeError(f'Session not found: {name}; available: {", ".join(sorted(available))}')
        if name.startswith('pm-'):
            base = name.split('~', 1)[0]
            selected.update(s for s in available if s == base or s.startswith(base + '~'))
        else:
            selected.add(name)
    return [s for s in sessions if s['session_name'] in selected]


def select_snapshot(data, names):
    if not names:
        return data
    data = copy.deepcopy(data)
    data['sessions'] = select_session_records(data['sessions'], names)
    windows = {link['id'] for s in data['sessions'] for link in s['links']}
    data['windows'] = {key: value for key, value in data['windows'].items() if key in windows}
    locations = tuple(p['location'] + ':' for w in data['windows'].values() for p in w['panes'])
    for kind in ('warnings', 'blockers'):
        data[kind] = [message for message in data[kind] if message.startswith(locations)]
    return data


def live_agent_conflicts(data):
    """Find existing terminals for the requested conversations, on any socket."""
    wanted = {p['restore']['session_id']: p['restore'] for w in data['windows'].values()
              for p in w['panes'] if p['restore'].get('session_id') and p['restore'].get('argv')}
    if not wanted:
        return []
    procs = processes()
    index = CodexIndex()
    conflicts = set()
    for proc in procs.values():
        kind = program(proc)
        if kind not in AGENTS or not proc['tty']:
            continue
        env = proc_env(proc['pid'])
        pane = env.get('TMUX_PANE', '')
        socket = env.get('TMUX', '').split(',', 1)[0]
        if not socket or not re.fullmatch(r'%\d+', pane):
            continue
        tmux = Tmux(socket)
        root = tmux.call('display-message', '-p', '-t', pane, '#{pane_pid}', allow_fail=True)
        if not root.isdigit() or int(root) not in procs or procs[int(root)]['tty'] != proc['tty']:
            continue
        if proc['pid'] not in {p['pid'] for p in children(procs, int(root))}:
            continue
        if kind.startswith('claude'):
            sid, _, _ = claude_id(proc, env)
        else:
            sid, _, _ = index.identify(proc, env, tmux.call('capture-pane', '-p', '-t', pane, allow_fail=True))
            if not sid:
                sid, _, _ = index.identify(proc, env, tmux.call('capture-pane', '-p', '-S', '-', '-t', pane, allow_fail=True))
        location = tmux.call('display-message', '-p', '-t', pane, '#{session_name}:#{window_index}.#{pane_index}', allow_fail=True)
        if sid in wanted:
            conflicts.add(f'{sid} already running at {location} (socket {socket})')
        elif not sid and any(r['cwd'] == proc['cwd'] and r['kind'].startswith('codex' if kind.startswith('codex') else 'claude') for r in wanted.values()):
            conflicts.add(f'cannot identify the live {kind} at {location} in {proc["cwd"]}; duplicate resume cannot be ruled out')
    return sorted(conflicts)


def snapshot(tmux, destination, selected=None):
    private_dir(destination)
    procs = processes()
    own = set()
    pid = os.getpid()
    while pid in procs:
        own.add(pid)
        pid = procs[pid]['ppid']
    # Exclude the saver itself, its helper commands and shell wrappers.
    own.update(p['pid'] for p in children(procs, os.getpid()))
    data = dict(version=1, created=dt.datetime.now(dt.timezone.utc).isoformat(), host=os.uname().nodename,
                server=tmux.call('display-message', '-p', '#{socket_path}|#{pid}|#{start_time}'), sessions=[], windows={}, warnings=[], blockers=[])
    sessions = select_session_records(tmux.rows('list-sessions', ['session_id', 'session_name', 'session_group', 'window_id', 'session_path']), selected)
    for s in sessions:
        s['links'] = []
        data['sessions'].append(s)
    by_id = {s['session_id']: s for s in sessions}
    fields = ['session_id', 'window_id', 'window_index', 'window_name', 'window_layout', 'window_zoomed_flag', 'window_width', 'window_height']
    for w in tmux.rows('list-windows', fields, '-a'):
        if w['session_id'] not in by_id:
            continue
        by_id[w['session_id']]['links'].append(dict(id=w['window_id'], index=w['window_index']))
        if w['window_id'] not in data['windows']:
            w['panes'] = []
            data['windows'][w['window_id']] = w
    index = CodexIndex()
    seen = set()
    for p in tmux.rows('list-panes', ['window_id', 'pane_id', 'pane_index', 'pane_pid', 'pane_current_path', 'pane_active', 'pane_title', 'pane_dead'], '-a'):
        if p['window_id'] not in data['windows']:
            continue
        if p['pane_id'] in seen:
            continue
        seen.add(p['pane_id'])
        window = data['windows'][p['window_id']]
        window['panes'].append(p)
        location = f"{by_id[window['session_id']]['session_name']}:{window['window_index']} ({window['window_name']}) pane {p['pane_index']}"
        p['location'] = location
        history = tmux.call('capture-pane', '-p', '-t', p['pane_id'], '-S', '-', allow_fail=True)
        history_file = destination / (p['pane_id'][1:] + '.txt')
        history_file.write_text(history + '\n')
        history_file.chmod(0o600)
        p['history'] = history_file.name
        tree = children(procs, int(p['pane_pid']))
        root = procs.get(int(p['pane_pid']))
        # AI tool children can have their own PTYs: only consider programs
        # attached to the pane's terminal, and choose the outermost agent.
        attached = [x for x in tree if root and x['tty'] == root['tty'] and x['pid'] not in own]
        agents = [x for x in attached if program(x) in AGENTS]
        editors = [x for x in attached if program(x) in EDITORS]
        emacs = [x for x in attached if program(x) in EMACS]
        managers = [x for x in attached if pm_tui(x) and by_id[window['session_id']]['session_name'].startswith('pm-')]
        p['processes'] = [{k: x[k] for k in ('pid', 'ppid', 'pgid', 'tpgid', 'argv', 'cwd')} for x in attached]
        if managers:
            p['restore'] = dict(kind='pm', cwd=managers[0]['cwd'], argv=None)
        elif agents:
            proc = agents[0]
            p['restore'] = agent_record(proc, index, tmux.call('capture-pane', '-p', '-t', p['pane_id']))
            if p['restore'].get('problem') == 'cannot determine the selected conversation' and program(proc).startswith('codex'):
                # A tool's output can fill the visible screen. Search saved
                # scrollback as a fallback, still requiring a unique match.
                p['restore'] = agent_record(proc, index, history)
                if p['restore'].get('session_id'):
                    p['restore']['evidence'] = 'unique assistant transcript in pane scrollback'
            if len(agents) > 1 and not all(x['pid'] in {c['pid'] for c in children(procs, proc['pid'])} for x in agents):
                data['blockers'].append(f'{location}: multiple independent agents in one pane')
        elif emacs:
            proc = emacs[0]
            assets = destination / ('emacs-' + p['pane_id'][1:])
            private_dir(assets)
            status = emacs_checkpoint(proc, assets)
            p['restore'] = dict(kind='emacs', cwd=proc['cwd'], original_argv=proc['argv'], argv=None, status=status)
            if status:
                for buffer in status['buffers']:
                    if buffer['modified']:
                        data['blockers'].append(f"{location}: UNSAVED {buffer['name']}")
                if status.get('error'):
                    data['blockers'].append(f"{location}: Emacs checkpoint: {status['error']}")
                executable = shutil.which('emacs') or shutil.which('emacs-nox')
                if executable and (assets / 'session.el').is_file():
                    p['restore']['session_file'] = str(assets.relative_to(destination) / 'session.el')
                    p['restore']['argv'] = [executable, '-nw', '--load', str(assets / 'session.el')]
                else:
                    p['restore']['problem'] = 'Emacs checkpoint cannot be reopened; emacs executable or session file is missing'
            else:
                p['restore']['problem'] = 'Emacs buffer state is unknown. Enable server-start or load the helper installed by tmux-resume install-emacs-plugin.'
                data['warnings'].append(f'{location}: no responding Emacs helper/server; command will be prefilled, not executed')
            if len(emacs) > 1:
                data['blockers'].append(f'{location}: multiple Emacs processes in one pane')
        elif editors:
            proc = editors[0]
            assets = destination / ('vim-' + p['pane_id'][1:])
            private_dir(assets)
            status = vim_checkpoint(proc, assets)
            p['restore'] = dict(kind='vim', cwd=proc['cwd'], original_argv=proc['argv'], argv=None, status=status)
            if status:
                for b in status['buffers']:
                    if b['modified']:
                        data['blockers'].append(f"{location}: UNSAVED {b['name'] or '[unnamed buffer]'}")
                if status.get('error'):
                    data['blockers'].append(f"{location}: Vim checkpoint: {status['error']}")
                if (assets / 'session.vim').is_file():
                    p['restore']['session_file'] = str(assets.relative_to(destination) / 'session.vim')
                    p['restore']['argv'] = [shutil.which(program(proc)) or proc['argv'][0], '-S', str(assets / 'session.vim')]
            else:
                swaps = vim_swap_check(proc, assets)
                p['restore']['swap_check'] = swaps
                p['restore']['swap_files'] = [x['path'] for x in swaps]
                for item in swaps:
                    if not item['info'].get('error') and item['info'].get('dirty'):
                        data['blockers'].append(f"{location}: UNSAVED (swap evidence) {item['info'].get('fname', item['path'])}")
                data['warnings'].append(f'{location}: Vim plugin missing or unresponsive; inspected {len(swaps)} swap files. Swap headers may lag and cannot cover buffers without swaps.')
                p['restore']['problem'] = 'Vim has no responding checkpoint plugin; unsaved buffers are unknown even if swaps look clean. Install with tmux-resume install-vim-plugin, then run :runtime plugin/tmux_resume.vim in this editor.'
            if len(editors) > 1:
                data['blockers'].append(f'{location}: multiple editors in one pane')
        else:
            other = [x for x in attached if program(x) not in SHELLS]
            p['restore'] = dict(kind='shell', cwd=p['pane_current_path'], argv=None)
            # A shell executing a script is also a running job.
            scripts = [x for x in attached if program(x) in SHELLS and not idle_shell(x)]
            if other or scripts:
                names = ', '.join(dict.fromkeys(program(x) for x in other + scripts))
                data['blockers'].append(f'{location}: running {names}; command recorded, restored as a shell')
        if p['restore'].get('problem'):
            data['blockers'].append(f"{location}: {p['restore']['problem']}")
        if (agents or managers) and (editors or emacs):
            data['blockers'].append(f'{location}: editor running inside an agent; finish editing first')
        if agents or editors or emacs or managers:
            selected = (managers or agents or editors or emacs)[0]
            selected_tree = {x['pid'] for x in children(procs, selected['pid'])}
            runners = {x['pid'] for x in attached if resume_runner(x) and
                       selected['pid'] in {c['pid'] for c in children(procs, x['pid'])}}
            other_jobs = [x for x in attached if x['pid'] not in selected_tree | runners and program(x) not in SHELLS]
            if other_jobs:
                data['blockers'].append(f"{location}: other jobs in the pane: {', '.join(sorted({program(x) for x in other_jobs}))}")
        # Shells and arbitrary jobs suspended behind an editor/agent are visible
        # in the inventory but are not blindly restarted.
    prepare_remote_resumes(data)
    write_json(destination / 'snapshot.json', data)
    return data


def report(data, path):
    if data['version'] == 2:
        parts = snapshot_parts(path, data)
        print(f'Saved {len(parts)} tmux servers. Checkpoint: {path}')
        for part_path, part in parts:
            socket = snapshot_socket(part)
            print(f'\nServer: {server_name(socket) or socket} (socket {socket})')
            report(part, part_path)
        return
    panes = [p for w in data['windows'].values() for p in w['panes']]
    agents = [p for p in panes if p['restore']['kind'] in AGENTS]
    print(f"Saved {len(data['sessions'])} sessions, {len(data['windows'])} windows, {len(panes)} panes.")
    print(f"Identified {sum(bool(p['restore'].get('session_id')) for p in agents)}/{len(agents)} agent conversations.")
    print(f'Snapshot: {path}')
    for item in data['warnings']:
        print('WARNING: ' + item)
    for item in data['blockers']:
        print('CLOSE BLOCKED: ' + item)


def find_snapshot(value):
    path = Path(value).expanduser().resolve() if value else (STATE / 'latest').resolve()
    if path.is_file():
        path = path.parent
    data = read_json(path / 'snapshot.json')
    if not data or data.get('version') not in {1, 2}:
        raise RuntimeError(f'No supported snapshot at {path}')
    if data['version'] == 1:
        prepare_remote_resumes(data)
    return path, data


def snapshot_socket(data):
    return data['server'].rsplit('|', 2)[0]


def snapshot_parts(path, data):
    if data['version'] == 1:
        return [(path, data)]
    parts = []
    for entry in data['servers']:
        part_path, part = find_snapshot(str(path / entry['directory']))
        if part['version'] != 1:
            raise RuntimeError(f'Expected a single-server snapshot at {part_path}')
        parts.append((part_path, part))
    return parts


def session_targets(inventories, names):
    """Resolve exact names across servers; ambiguous names need a selector."""
    if not names:
        return {i: None for i in range(len(inventories))}
    targets = {}
    for name in dict.fromkeys(names):
        matches = [i for i, (socket, sessions) in enumerate(inventories)
                   if any(s['session_name'] == name for s in sessions)]
        if not matches:
            raise RuntimeError(f'Session not found: {name}')
        if len(matches) > 1:
            sockets = ', '.join(inventories[i][0] for i in matches)
            raise RuntimeError(f'Session {name} exists on multiple servers ({sockets}); select one with --server or --socket')
        targets.setdefault(matches[0], []).append(name)
    return targets


def snapshot_all(sources, destination, names=None):
    if not sources:
        raise RuntimeError('No running tmux servers with sessions found')
    inventories = [(tmux.prefix[2], tmux.rows('list-sessions', ['session_name'])) for tmux in sources]
    targets = session_targets(inventories, names)
    private_dir(destination)
    data = dict(version=2, created=dt.datetime.now(dt.timezone.utc).isoformat(), host=os.uname().nodename, servers=[])
    for i, selected in targets.items():
        directory = f'servers/{i:03d}'
        part = snapshot(sources[i], destination / directory, selected)
        socket = snapshot_socket(part)
        data['servers'].append(dict(socket=socket, name=server_name(socket), directory=directory))
    write_json(destination / 'snapshot.json', data)
    return data


def select_parts(parts, names=None, server=None, socket=None):
    if server is not None:
        parts = [(path, data) for path, data in parts if server_name(snapshot_socket(data)) == server]
        if not parts:
            raise RuntimeError(f'Saved server not found: {server}')
        if len(parts) > 1:
            raise RuntimeError(f'Server name {server} is ambiguous; select its full --socket path')
    if socket:
        matching = [(path, data) for path, data in parts if snapshot_socket(data) == socket]
        if matching:
            parts = matching
    targets = session_targets([(snapshot_socket(data), data['sessions']) for _, data in parts], names)
    parts = [(parts[i][0], select_snapshot(parts[i][1], selected)) for i, selected in targets.items()]
    if socket and len(parts) != 1:
        raise RuntimeError('--socket must match a saved server or target a single selected server; nothing restored')
    return parts


def checkpoint_blockers(path, data):
    return [f'{snapshot_socket(part)}: {message}' for _, part in snapshot_parts(path, data) for message in part['blockers']]


def checkpoint_structure(path, data):
    return sorted((part['server'], part['sessions'],
                   sorted((p['pane_id'], p['pane_pid']) for w in part['windows'].values() for p in w['panes']))
                  for _, part in snapshot_parts(path, data))


def workspace_key(data, session):
    return [data['host'], data['created'], session['session_id']]


def active_restores():
    active = []
    servers = {}
    for record in read_json(STATE / 'restores.json') or []:
        socket = record['socket']
        if socket not in servers:
            tmux = Tmux(socket)
            signature = tmux.call('display-message', '-p', '#{socket_path}|#{pid}|#{start_time}', allow_fail=True)
            ids = tmux.call('list-sessions', '-F', '#{session_id}', allow_fail=True).splitlines()
            windows = tmux.call('list-windows', '-a', '-F', '#{window_id}', allow_fail=True).splitlines()
            servers[socket] = (signature, ids, windows)
        signature, ids, windows = servers[socket]
        if signature == record['server'] and (record['session_id'] in ids or set(record.get('windows', [])) & set(windows)):
            active.append(record)
    return active


@contextmanager
def restore_lock():
    private_dir(STATE)
    # Serialize our restores across sockets and snapshots. Receipts also cover
    # the interval before a freshly launched agent publishes its session ID.
    with (STATE / 'restore.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another tmux-resume restore is in progress; retry when it finishes') from None
        yield


def remote_connection(record):
    """Resolve the endpoint using the environment the restored pane will use."""
    if not record['kind'].startswith('codex'):
        return None
    options = {}
    args = iter((record.get('argv') or [])[1:])
    for arg in args:
        if arg == '--':
            break
        name, equals, value = arg.lstrip('-').partition('=')
        if arg.startswith('-') and name in CODEX_VALUES:
            options[name] = value if equals else next(args, '')
    if 'remote' not in options:
        return None
    env = dict(os.environ, **record.get('env', {}))
    for name in record.get('unset', []):
        env.pop(name, None)
    endpoint = options['remote']
    if endpoint == 'unix://':
        home = Path(env.get('CODEX_HOME') or str(Path.home() / '.codex'))
        if not home.is_absolute():
            home = Path(record['cwd']) / home
        endpoint += str(home / 'app-server-control/app-server-control.sock')
    token_name = options.get('remote-auth-token-env')
    token = env.get(token_name) if token_name else None
    if token_name and not token:
        raise ValueError(f'remote authentication environment variable {token_name} is not set')
    return endpoint, token


def probe_remote(endpoint, token=None):
    """Perform only a WebSocket handshake; never start or resume a task."""
    with ExitStack() as stack:
        if endpoint.startswith('unix://'):
            conn = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
            conn.settimeout(3)
            conn.connect(endpoint[len('unix://'):])
            host, target = 'localhost', '/'
        else:
            url = urlsplit(endpoint)
            if url.scheme not in {'ws', 'wss'} or not url.hostname or url.username or url.password:
                raise ValueError('expected unix://, unix://PATH, ws://HOST:PORT, or wss://HOST:PORT')
            conn = stack.enter_context(socket.create_connection(
                (url.hostname, url.port or (443 if url.scheme == 'wss' else 80)), timeout=3))
            if url.scheme == 'wss':
                conn = stack.enter_context(ssl.create_default_context().wrap_socket(conn, server_hostname=url.hostname))
            host = url.netloc
            target = (url.path or '/') + ('?' + url.query if url.query else '')
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        headers = [f'GET {target} HTTP/1.1', f'Host: {host}', 'Upgrade: websocket',
                   'Connection: Upgrade', f'Sec-WebSocket-Key: {key}', 'Sec-WebSocket-Version: 13']
        if token:
            headers.append('Authorization: Bearer ' + token)
        if any('\r' in header or '\n' in header for header in headers):
            raise ValueError('invalid remote address or authentication header')
        conn.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode('utf-8'))
        response = stack.enter_context(http.client.HTTPResponse(conn))
        response.begin()
        expected = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
        if response.status != 101 or response.getheader('Sec-WebSocket-Accept') != expected:
            raise ValueError(f'remote endpoint refused WebSocket connection (HTTP {response.status})')


def check_remote_connections(parts):
    checked, failures = {}, []
    for _, data in parts:
        for window in data['windows'].values():
            for pane in window['panes']:
                try:
                    connection = remote_connection(pane['restore'])
                    if connection is None:
                        continue
                    if connection not in checked:
                        try:
                            probe_remote(*connection)
                            checked[connection] = None
                        except (OSError, ValueError, http.client.HTTPException) as error:
                            checked[connection] = str(error)
                    if checked[connection]:
                        failures.append(f"{pane['location']}: {checked[connection]}")
                except ValueError as error:
                    failures.append(f"{pane['location']}: {error}")
    if failures:
        raise RuntimeError('Remote Codex server is not reachable or ready; nothing restored.\n' +
                           '\n'.join(failures) +
                           '\nStart local remote control with `codex remote-control start` '
                           '(use the saved CODEX_HOME if customized), or start/fix the configured remote endpoint. '
                           'Then rerun the same tmux-resume restore command.')


def restore(tmux, path, data, dry_run=False):
    check_remote_connections([(path, data)])
    if dry_run:
        return restore_locked(tmux, path, data, True)
    with restore_lock():
        return restore_locked(tmux, path, data)


def restore_all(parts, socket=None, dry_run=False):
    parts = list(parts)
    check_remote_connections(parts)
    plans = [(Tmux(socket or snapshot_socket(data)), path, data) for path, data in parts]
    if dry_run:
        for tmux, path, data in plans:
            print(f'\nServer: {server_name(tmux.prefix[2]) or tmux.prefix[2]} (socket {tmux.prefix[2]})')
            restore_locked(tmux, path, data, True)
        return
    with restore_lock():
        # Validate every server before creating even the first placeholder.
        for tmux, path, data in plans:
            restore_locked(tmux, path, data, preflight_only=True)
        pending = []
        try:
            for tmux, path, data in plans:
                pending.append(restore_locked(tmux, path, data, defer_launch=True))
        except Exception:
            for _, rollback in reversed(pending):
                rollback()
            raise
        # After this point failures leave visible sessions for inspection;
        # never kill agents that have already begun running.
        for launch, _ in pending:
            launch()


def restore_locked(tmux, path, data, dry_run=False, preflight_only=False, defer_launch=False):
    if not (path / 'snapshot.json').is_file():
        raise RuntimeError(f'Checkpoint no longer exists: {path}; nothing restored')
    sessions = restore_sessions(data)
    pm_workspaces = {s['session_id']: pm_workspace(s, data) for s in sessions if pm_workspace(s, data)}
    existing = set(tmux.call('list-sessions', '-F', '#{session_name}', allow_fail=True).splitlines())
    conflicts = existing & {s['session_name'] for s in data['sessions']}
    for workspace in pm_workspaces.values():
        conflicts.update(name for name in existing if name == workspace['name'] or name.startswith(workspace['name'] + '~'))
    receipts = active_restores()
    repeated = [r for r in receipts if r['workspace'] in [workspace_key(data, s) for s in sessions]]
    if dry_run:
        for workspace in pm_workspaces.values():
            print(f"{workspace['name']}: cd {shlex.quote(workspace['cwd'])} && pm session (reuse PM's TUI; skip saved ~N attachments)")
        for w in data['windows'].values():
            for p in w['panes']:
                if any(p['pane_id'] == x['pane'] for x in pm_workspaces.values()):
                    print(f"{p['location']}: [PM creates this dashboard]")
                    continue
                r = p['restore']
                command = shlex.join(r['argv']) if r.get('argv') else '[shell]'
                prefill = prefill_command(p)
                if prefill:
                    command = '[prefill; waits for Enter] ' + prefill
                print(f"{p['location']}: {command}")
        if conflicts:
            print('Existing session names (restore will refuse): ' + ', '.join(sorted(conflicts)))
        elif repeated:
            print('RESTORE BLOCKED: this checkpoint is already restored in a running tmux session')
        else:
            for conflict in live_agent_conflicts(data):
                print('RESTORE BLOCKED: ' + conflict)
        return
    if conflicts:
        raise RuntimeError('Session names already exist; nothing restored: ' + ', '.join(sorted(conflicts)))
    if repeated:
        raise RuntimeError('Nothing restored: this checkpoint is already restored in a running tmux session: ' +
                           ', '.join(r['socket'] + ' ' + r['session_id'] for r in repeated))
    agent_conflicts = live_agent_conflicts(data)
    if agent_conflicts:
        raise RuntimeError('Nothing restored: ' + '; '.join(agent_conflicts))
    for workspace in pm_workspaces.values():
        if not shutil.which('pm'):
            raise RuntimeError('PM is required to restore this workspace; install pm first')
        result = run(['pm', 'session', 'name'], cwd=workspace['cwd'], env=pm_environment(tmux), timeout=15)
        if result.returncode or result.stdout.strip() != workspace['name']:
            raise RuntimeError(f"pm session name in {workspace['cwd']} does not match {workspace['name']}; nothing restored")
    for w in data['windows'].values():
        for p in w['panes']:
            r = p['restore']
            if not Path(r['cwd']).is_dir():
                raise RuntimeError(f"Directory missing: {r['cwd']}")
            if r.get('argv') and not shutil.which(r['argv'][0]):
                raise RuntimeError(f"Required program missing: {r['argv'][0]}")
            if r.get('session_file') and not (path / r['session_file']).is_file():
                raise RuntimeError(f"Editor session missing: {r['session_file']}")
    if preflight_only:
        return
    if len(tmux.prefix) > 1:
        # /tmp socket directories disappear on reboot. Recreate just the
        # missing parent, with private permissions; preserve existing modes.
        Path(tmux.prefix[2]).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    new_windows, new_panes, new_sessions, groups = {}, {}, {}, {}
    placeholders = []
    pm_panes = set()
    pm_created = []
    def rollback():
        for sid in reversed(list(new_sessions.values())):
            tmux.call('kill-session', '-t', sid, allow_fail=True)
        for base in pm_created:
            for name in tmux.call('list-sessions', '-F', '#{session_name}', allow_fail=True).splitlines():
                if name == base or name.startswith(base + '~'):
                    tmux.call('kill-session', '-t', '=' + name, allow_fail=True)
    shell = tmux.call('show-options', '-gv', 'default-shell', allow_fail=True) or os.environ.get('SHELL', '/bin/bash')
    try:
        # Keep inert placeholders until the complete structure exists.
        for s in sessions:
            name = s['session_name']
            group = s['session_group']
            workspace = pm_workspaces.get(s['session_id'])
            if group and group in groups:
                sid = tmux.call('new-session', '-d', '-P', '-F', '#{session_id}', '-s', name, '-t', groups[group])
                new_sessions[s['session_id']] = sid
                continue
            first = data['windows'][s['links'][0]['id']]
            if workspace:
                pm_created.append(workspace['name'])
                sid = start_pm(tmux, workspace, path)
            else:
                sid = tmux.call('new-session', '-d', '-P', '-F', '#{session_id}', '-s', name, '-x', first['window_width'], '-y', first['window_height'], 'sleep 2147483647')
            new_sessions[s['session_id']] = sid
            if group:
                groups[group] = sid
            tmux.call('set-option', '-t', sid, 'renumber-windows', 'off')
            placeholder = tmux.call('display-message', '-p', '-t', sid, '#{window_id}')
            pm_tui_pane = tmux.call('display-message', '-p', '-t', sid, '#{pane_id}') if workspace else None
            placeholders.append(placeholder)
            # Move temporary window away from every saved index.
            used = {int(x['index']) for x in s['links']}
            temp_index = max(used | {0}) + 100
            tmux.call('move-window', '-s', placeholder, '-t', sid + ':' + str(temp_index))
            for link in s['links']:
                target = sid + ':' + link['index']
                old = link['id']
                if old in new_windows:
                    tmux.call('link-window', '-s', new_windows[old], '-t', target)
                    continue
                w = data['windows'][old]
                reuse_pm = workspace and old == workspace['window']
                if reuse_pm:
                    wid = placeholder
                    tmux.call('move-window', '-s', wid, '-t', target)
                    tmux.call('rename-window', '-t', wid, w['window_name'])
                    tmux.call('resize-window', '-t', wid, '-x', w['window_width'], '-y', w['window_height'])
                else:
                    wid = tmux.call('new-window', '-d', '-P', '-F', '#{window_id}', '-t', target, '-n', w['window_name'], '-c', w['panes'][0]['restore']['cwd'], 'sleep 2147483647')
                new_windows[old] = wid
                tmux.call('set-window-option', '-t', wid, 'automatic-rename', 'off')
                tmux.call('set-window-option', '-t', wid, 'allow-rename', 'off')
                first_pane = tmux.call('display-message', '-p', '-t', wid, '#{pane_id}')
                for i, p in enumerate(w['panes']):
                    if reuse_pm and p['pane_id'] == workspace['pane']:
                        pane = pm_tui_pane
                        pm_panes.add(p['pane_id'])
                    elif i == 0 and not reuse_pm:
                        pane = first_pane
                    else:
                        pane = tmux.call('split-window', '-d', '-P', '-F', '#{pane_id}', '-t', wid, '-c', p['restore']['cwd'], 'sleep 2147483647')
                        tmux.call('select-layout', '-t', wid, 'tiled')
                    new_panes[p['pane_id']] = pane
                # Each split inserts after the target pane, so creation order
                # need not match pane indices. PM's dashboard may also start
                # in a different position. Reorder before assigning the layout.
                for i, p in enumerate(w['panes']):
                    current = tmux.call('list-panes', '-t', wid, '-F', '#{pane_id}').splitlines()
                    if current[i] != new_panes[p['pane_id']]:
                        tmux.call('swap-pane', '-d', '-s', new_panes[p['pane_id']], '-t', current[i])
                # tmux assigns the current panes to layout leaves in order.
                tmux.call('select-layout', '-t', wid, w['window_layout'])
                for p in w['panes']:
                    if p['pane_active'] == '1':
                        tmux.call('select-pane', '-t', new_panes[p['pane_id']])
                if w['window_zoomed_flag'] == '1':
                    tmux.call('resize-pane', '-Z', '-t', wid)
            if not workspace:
                tmux.call('kill-window', '-t', placeholder)
            placeholders.remove(placeholder)
        for s in sessions:
            tmux.call('select-window', '-t', new_sessions[s['session_id']] + ':' + next(x['index'] for x in s['links'] if x['id'] == s['window_id']))
    except Exception:
        # No user programs have been launched yet, so rollback is safe.
        rollback()
        raise
    # Execute argv via Python, never via send-keys or eval. Keep a shell when
    # the resumed editor/agent exits. A failed launch leaves a visible message.
    try:
        server = tmux.call('display-message', '-p', '#{socket_path}|#{pid}|#{start_time}')
        receipts.extend(dict(workspace=workspace_key(data, s), server=server,
                             socket=server.rsplit('|', 2)[0], session_id=new_sessions[s['session_id']],
                             windows=[new_windows[link['id']] for link in s['links']]) for s in sessions)
        write_json(STATE / 'restores.json', receipts)
    except Exception:
        rollback()
        raise
    def launch():
        for w in data['windows'].values():
            for p in w['panes']:
                if p['pane_id'] in pm_panes:
                    continue
                command = shlex.join([sys.executable, str(ROOT / 'tmux_resume.py'), '_run', str(path), p['pane_id'], shell])
                tmux.call('respawn-pane', '-k', '-t', new_panes[p['pane_id']], '-c', p['restore']['cwd'], command)
        print(f"Restored {len(sessions)} workspaces. Attach with: {shlex.join(tmux.prefix + ['attach', '-t', sessions[0]['session_name']])}")
    if defer_launch:
        return launch, rollback
    launch()


def run_pane(path, pane_id, shell):
    path, data = find_snapshot(path)
    p = next(p for w in data['windows'].values() for p in w['panes'] if p['pane_id'] == pane_id)
    r = p['restore']
    os.chdir(r['cwd'])
    env = os.environ.copy()
    env.update(r.get('env', {}))
    for k in r.get('unset', []):
        env.pop(k, None)
    argv = r.get('argv')
    if r.get('session_file'):
        if r['kind'] == 'emacs':
            argv = [argv[0], '-nw', '--load', str(ROOT / 'tmux-resume.el'), '--load', str(path / r['session_file'])]
        else:
            env['TMUX_RESUME_VIM_HELPER'] = str(ROOT / 'tmux_resume.vim')
            argv = [argv[0], '--cmd', "execute 'source ' . fnameescape($TMUX_RESUME_VIM_HELPER)", '-S', str(path / r['session_file'])]
    print(f"[tmux-resume] {p['location']}; scrollback saved in {path / p['history']}", flush=True)
    retry_argv = None
    if argv:
        if r.get('remote_permission_arguments'):
            print('[tmux-resume] Remote task retains its stored permissions; original override flags are recorded in the snapshot.', flush=True)
        try:
            result = subprocess.run(argv, env=env)
            print(f'[tmux-resume] Program exited ({result.returncode}).', flush=True)
            if result.returncode:
                retry_argv = argv
        except OSError as e:
            print(f'[tmux-resume] Could not launch: {e}', flush=True)
            retry_argv = argv
    elif r.get('problem'):
        print('[tmux-resume] ' + r['problem'], flush=True)
    if retry_argv:
        if r['kind'].startswith('codex') and any(arg == '--remote' or arg.startswith('--remote=') for arg in argv):
            print('[tmux-resume] Remote Codex could not be resumed; see the error above. The remote-control server may not be running or reachable.', flush=True)
            print('[tmux-resume] After remote control is started, use the command below to resume the saved conversation.', flush=True)
        else:
            print('[tmux-resume] Resume failed. Fix the reported error, then retry the prepared command.', flush=True)
    prefill = ' '.join(quote_prefill(arg) for arg in retry_argv) if retry_argv else prefill_command(p)
    if prefill and Path(shell).name == 'bash':
        env['TMUX_RESUME_PREFILL'] = prefill
        env['TMUX_RESUME_PREFILL_KEY'] = '[tmux-resume-' + uuid.uuid4().hex + '~'
        print('[tmux-resume] Command prefilled. Press Enter to run, or Ctrl-C to discard.', flush=True)
        os.execvpe(shell, [shell, '--rcfile', str(ROOT / 'prefill.bash'), '-i'], env)
    elif prefill:
        print('[tmux-resume] Command prefill requires Bash. Recorded command: ' + prefill, flush=True)
    os.execvpe(shell, [shell, '-i'], env)


def close_saved(socket, path):
    """Run detached so closing the invoking pane cannot interrupt cleanup."""
    path, data = find_snapshot(path)
    tmux = Tmux(socket)
    tmux.call(*close_commands(tmux, data))


def close_commands(tmux, data):
    if tmux.call('display-message', '-p', '#{socket_path}|#{pid}|#{start_time}') != data['server']:
        raise RuntimeError('tmux server changed; nothing closed')
    commands = []
    for s in data['sessions']:
        actual = tmux.call('display-message', '-p', '-t', s['session_id'], '#{session_name}')
        if actual != s['session_name']:
            raise RuntimeError('tmux sessions changed; nothing closed')
        if commands:
            commands.append(';')
        commands += ['kill-session', '-t', s['session_id']]
    return commands


def close_all(path):
    path, data = find_snapshot(path)
    plans = []
    # Validate identities on all servers before closing any of them.
    for _, part in snapshot_parts(path, data):
        tmux = Tmux(snapshot_socket(part))
        plans.append((tmux, close_commands(tmux, part)))
    for tmux, commands in plans:
        tmux.call(*commands)


def install_copy(source, target, mode=0o644):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != source.read_bytes():
        backup = target.with_name(target.name + '.bak-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        shutil.copy2(target, backup)
        print(f'Previous file backed up to {backup}')
    if source.resolve() == target.resolve() and not target.is_symlink():
        return
    tmp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    shutil.copyfile(source, tmp)
    tmp.chmod(mode)
    tmp.replace(target)


def migrate_installation(prefix):
    with ExitStack() as locks:
        old_state = STATE.parent / LEGACY_NAME
        if old_state.is_dir():
            for name in ('checkpoint.lock', 'restore.lock'):
                lock = locks.enter_context((old_state / name).open('a'))
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError('A checkpoint or restore is in progress; retry the installation after it finishes') from None
        migrate_installation_locked(prefix)


def migrate_installation_locked(prefix):
    config_home = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    pairs = [(prefix / 'share' / LEGACY_NAME, prefix / 'share/tmux-resume'),
             (STATE.parent / LEGACY_NAME, STATE),
             (config_home / LEGACY_NAME, config_home / 'tmux-resume'),
             (prefix / 'bin' / LEGACY_NAME, prefix / 'bin/tmux-resume')]
    pending = []
    for old, new in pairs:
        if not old.exists() and not old.is_symlink():
            continue
        if new.exists() or new.is_symlink():
            raise RuntimeError(f'Both {old} and {new} exist; nothing migrated. Resolve this naming conflict before installing.')
        pending.append((old, new))
    # Preflight every destination before moving any files. This is a clean
    # rename: no aliases or old-name loaders are installed.
    for old, new in pending:
        old.rename(new)
        print(f'Renamed {old} to {new}')
    latest = STATE / 'latest'
    if latest.is_symlink():
        target = Path(os.readlink(latest))
        old_state = STATE.parent / LEGACY_NAME
        if target.is_absolute() and target.is_relative_to(old_state):
            replacement = latest.with_name('latest-' + uuid.uuid4().hex)
            replacement.symlink_to(STATE / target.relative_to(old_state))
            replacement.replace(latest)
    bundle = prefix / 'share/tmux-resume'
    for old, new in [(LEGACY_STEM + '.py', 'tmux_resume.py'),
                     (LEGACY_STEM + '.vim', 'tmux_resume.vim'),
                     (LEGACY_NAME + '.el', 'tmux-resume.el')]:
        if (bundle / old).exists() and not (bundle / new).exists():
            (bundle / old).rename(bundle / new)


def install_plugin(editor, directory=None):
    if editor == 'vim':
        folder = Path(directory).expanduser() if directory else Path.home() / '.vim/plugin'
        old = folder / (LEGACY_STEM + '.vim')
        if old.exists() and not (folder / 'tmux_resume.vim').exists():
            old.rename(folder / 'tmux_resume.vim')
        install_copy(ROOT / 'tmux_resume.vim', folder / 'tmux_resume.vim')
        print(f'Vim plugin installed: {folder / "tmux_resume.vim"}')
        print('Existing Vim instances: :runtime plugin/tmux_resume.vim')
    else:
        folder = Path(directory).expanduser() if directory else Path.home() / '.emacs.d'
        old = folder / (LEGACY_NAME + '.el')
        if old.exists() and not (folder / 'tmux-resume.el').exists():
            old.rename(folder / 'tmux-resume.el')
        install_copy(ROOT / 'tmux-resume.el', folder / 'tmux-resume.el')
        print(f'Emacs helper installed: {folder / "tmux-resume.el"}')
        print('Load with M-x load-file, or add to init.el: (load ' + json.dumps(str(folder / 'tmux-resume.el')) + ' nil t)')


def install(prefix=None, vim_plugin=True, emacs_plugin=False):
    prefix = Path(prefix).expanduser().resolve() if prefix else Path.home() / '.local'
    migrate_installation(prefix)
    bin_dir = prefix / 'bin'
    bundle = prefix / 'share/tmux-resume'
    for name in ('tmux_resume.py', 'tmux_resume.vim', 'tmux-resume.el', 'prefill.bash', 'README.md'):
        install_copy(ROOT / name, bundle / name, 0o755 if name.endswith('.py') else 0o644)
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / 'tmux-resume'
    if target.exists() and not target.is_symlink():
        backup = target.with_name(target.name + '.bak-' + uuid.uuid4().hex[:8])
        shutil.copy2(target, backup)
        print(f'Previous command backed up to {backup}')
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.symlink_to(bundle / 'tmux_resume.py')
    temporary.replace(target)
    if vim_plugin:
        install_plugin('vim')
    if emacs_plugin:
        install_plugin('emacs')
    private_dir(STATE)
    private_dir(STATE / 'vim')
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'tmux-resume/config.json'
    if not config.exists():
        private_dir(config.parent)
        write_json(config, {'keep_checkpoints': 100})
        print(f'Created configuration: {config}')
    print(f'Installed {target}; keep {bin_dir} on PATH. The source checkout can now be moved or removed.')


def systemd_quote(value):
    value = str(value).replace('%', '%%')
    return json.dumps(value, ensure_ascii=False)


def install_systemd(user, prefix=None, directory=None, enable=True):
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        raise RuntimeError(f'Unknown system user: {user}') from None
    if directory is None and os.geteuid() != 0:
        raise RuntimeError('Installing the shutdown service requires root. Run sudo tmux-resume install-systemd --user ' + shlex.quote(user) +
                           ', or use --directory PATH to generate reviewable unit files without installing them.')
    home = Path(account.pw_dir)
    prefix = Path(prefix).expanduser().resolve() if prefix else home / '.local'
    executable = prefix / 'bin/tmux-resume'
    if not executable.is_file():
        raise RuntimeError(f'Install tmux-resume for {user} first; command missing: {executable}')
    uid = account.pw_uid
    name = f'tmux-resume-shutdown-{uid}.service'
    unit = f'''[Unit]
Description=Checkpoint tmux workspaces for {user} before shutdown
After=systemd-user-sessions.service systemd-logind.service user@{uid}.service network.target
RequiresMountsFor={systemd_quote(home)} {systemd_quote(prefix)}

[Service]
Type=oneshot
RemainAfterExit=yes
User={uid}
Group={account.pw_gid}
WorkingDirectory={str(home).replace('%', '%%')}
Environment={systemd_quote('HOME=' + str(home))}
Environment={systemd_quote('XDG_STATE_HOME=' + str(home / '.local/state'))}
Environment={systemd_quote('XDG_CONFIG_HOME=' + str(home / '.config'))}
Environment={systemd_quote('PATH=' + str(prefix / 'bin') + ':' + str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin')}
ExecStart=/usr/bin/true
ExecStop=:{systemd_quote(executable)} save --if-running
TimeoutStopSec=300
UMask=0077

[Install]
WantedBy=multi-user.target
'''
    # Login scopes stop before user@.service. Ordering only against user@ would
    # race those scopes. A prefix drop-in also orders all current/future login
    # scopes after this checkpoint during shutdown (reverse startup ordering).
    dropin = f'''[Unit]
# Ordering only: does not start the checkpoint service or stop login sessions.
Before={name}
'''
    destination = Path(directory).expanduser().resolve() if directory else Path('/etc/systemd/system')
    files = {name: unit, f'session-.scope.d/80-tmux-resume-{uid}.conf': dropin}
    with tempfile.TemporaryDirectory(prefix='tmux-resume-systemd-') as temporary:
        for relative, contents in files.items():
            source = Path(temporary) / Path(relative).name
            source.write_text(contents)
            install_copy(source, destination / relative)
            print(f'Wrote {destination / relative}')
    if directory is None:
        for command in [['systemctl', 'daemon-reload'], *([['systemctl', 'enable', '--now', name]] if enable else [])]:
            result = run(command)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f'{shlex.join(command)} failed')
        print(f'Shutdown checkpoint service {"enabled" if enable else "installed"}: {name}')
        print(f'Test without shutting down: sudo systemctl restart {name}')
    else:
        print('Generated for review only; no system service was installed or enabled.')


def save_checkpoint(args, sources):
    keep = history_limit(args.keep)
    initial_sources = sources()
    if not initial_sources and getattr(args, 'if_running', False):
        print('No running tmux servers; existing checkpoint history and latest are unchanged.')
        return
    private_dir(STATE)
    path = Path(args.output).expanduser().resolve() if args.output else STATE / (dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    if path.exists():
        raise RuntimeError(f'Snapshot destination already exists: {path}')
    data = snapshot_all(initial_sources, path, args.session)
    data['history_managed'] = not bool(args.output)
    write_json(path / 'snapshot.json', data)
    latest = STATE / ('latest-' + uuid.uuid4().hex)
    latest.symlink_to(path)
    latest.replace(STATE / 'latest')
    report(data, path)
    force_close = getattr(args, 'force_close', False)
    if getattr(args, 'close', False) or force_close:
        if checkpoint_blockers(path, data) and not force_close:
            raise RuntimeError('Snapshot saved; nothing closed. Resolve the blockers and run save --close again.')
        # A final fresh checkpoint catches edits/jobs changed during discovery.
        verify_path = path / 'close-check'
        verified = snapshot_all(sources(), verify_path, args.session)
        final_blockers = checkpoint_blockers(verify_path, verified)
        if (final_blockers and not force_close) or checkpoint_structure(path, data) != checkpoint_structure(verify_path, verified):
            raise RuntimeError('Workspace changed during close checks; nothing closed. Run save --close again.')
        if force_close:
            verified['force_close'] = True
            verified['forced_blockers'] = final_blockers
            write_json(verify_path / 'snapshot.json', verified)
            print('WARNING: Force-closing selected sessions. Unsaved text is not saved by this checkpoint and may be lost; running jobs will be terminated.', flush=True)
            for blocker in final_blockers:
                print('FORCE CLOSE: ' + blocker, flush=True)
        data['final_checkpoint'] = 'close-check'
        write_json(path / 'snapshot.json', data)
        # Use the latest editor checkpoints, including cursor positions.
        latest = STATE / ('latest-' + uuid.uuid4().hex)
        latest.symlink_to(verify_path)
        latest.replace(STATE / 'latest')
        print(f'Closing saved sessions. Final snapshot: {verify_path}', flush=True)
        with (verify_path / 'close.log').open('w') as log:
            subprocess.Popen([sys.executable, str(ROOT / 'tmux_resume.py'), '_close-all', str(verify_path)],
                             start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    prune_history(keep)
    if args.command == 'check' and checkpoint_blockers(path, data):
        raise SystemExit(2)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '_run':
        run_pane(*sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == '_close':
        close_saved(*sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == '_close-all':
        close_all(*sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--socket', help='select one tmux socket; override destination for a single-server restore')
    scope.add_argument('--server', '-L', help='select a named tmux server (tmux -L NAME); default: all servers')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('servers', help='list discovered live servers, sockets, and session names')
    sub.add_parser('history', help='list completed automatic checkpoints, newest first')
    for name in ('save', 'check'):
        p = sub.add_parser(name, help='checkpoint workspace' if name == 'save' else 'checkpoint and report close blockers')
        p.add_argument('--output', help='new snapshot directory')
        p.add_argument('--session', action='append', help='exact tmux/PM session name; repeat to select several')
        p.add_argument('--keep', type=int, help='automatic checkpoint history limit; 0 keeps all (default: config or 100)')
        if name == 'save':
            p.add_argument('--close', action='store_true', help='close saved sessions only if every pane passes checks')
            p.add_argument('--force-close', action='store_true', help='checkpoint and close despite pane blockers; unsaved text may be lost')
            p.add_argument('--if-running', action='store_true', help='leave latest unchanged and succeed when no tmux servers are running')
    p = sub.add_parser('restore')
    p.add_argument('snapshot', nargs='?')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--session', action='append', help='restore only this saved tmux/PM workspace; repeatable')
    p = sub.add_parser('show')
    p.add_argument('snapshot', nargs='?')
    p.add_argument('--session', action='append', help='show only this saved tmux/PM workspace; repeatable')
    p = sub.add_parser('install', help='install a standalone copy; no root privileges needed')
    p.add_argument('--prefix', help='installation prefix (default: ~/.local)')
    p.add_argument('--without-vim-plugin', action='store_true')
    p.add_argument('--emacs-plugin', action='store_true')
    p = sub.add_parser('install-systemd', help='install and enable a shutdown checkpoint system service (requires root)')
    p.add_argument('--user', required=True, help='account whose tmux sessions should be saved')
    p.add_argument('--prefix', help='existing installation prefix (default: selected user\'s ~/.local)')
    p.add_argument('--directory', help='generate unit files here for review instead of installing/enabling')
    p.add_argument('--no-enable', action='store_true', help='install unit files without enabling or starting the service')
    for name in ('install-vim-plugin', 'install-emacs-plugin'):
        p = sub.add_parser(name)
        p.add_argument('--directory', help='override the editor plugin installation directory')
    args = parser.parse_args()
    if args.command == 'history':
        show_history()
        return
    if args.command == 'install-systemd':
        install_systemd(args.user, args.prefix, args.directory, not args.no_enable)
        return
    if args.command == 'install':
        install(args.prefix, not args.without_vim_plugin, args.emacs_plugin)
        return
    if args.command in {'install-vim-plugin', 'install-emacs-plugin'}:
        install_plugin('vim' if args.command == 'install-vim-plugin' else 'emacs', args.directory)
        return
    socket = str(Path(args.socket).expanduser().absolute()) if args.socket else None
    if args.command in {'restore', 'show'}:
        path, data = find_snapshot(args.snapshot)
        parts = select_parts(snapshot_parts(path, data), args.session, args.server, socket)
        if args.command == 'show':
            print(f'Checkpoint: {path}; {len(parts)} selected servers')
            for part_path, part in parts:
                print(f'\nServer: {server_name(snapshot_socket(part)) or snapshot_socket(part)} (socket {snapshot_socket(part)})')
                report(part, part_path)
        else:
            restore_all(parts, socket, args.dry_run)
        return
    def sources():
        if socket or args.server:
            return [Tmux(socket or named_socket(args.server))]
        return discover_servers()
    if args.command == 'servers':
        for tmux in sources():
            print(f'{server_name(tmux.prefix[2]) or "[custom socket]"}: {tmux.prefix[2]}')
            print('  Sessions: ' + ', '.join(tmux.call('list-sessions', '-F', '#{session_name}').splitlines()))
        return
    with checkpoint_lock():
        save_checkpoint(args, sources)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        print('tmux-resume: ' + str(e), file=sys.stderr)
        sys.exit(1)
