import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from contextlib import contextmanager, redirect_stdout

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('resume', ROOT / 'tmux_resume.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)


def eventually(fn, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError('timed out waiting for test process')


@contextmanager
def remote_listener(path=None, reject=False):
    """A real local WebSocket handshake endpoint, with no agent or daemon."""
    listener = socket.socket(socket.AF_UNIX if path else socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(str(path) if path else ('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(.1)
    endpoint = 'unix://' + str(path) if path else f'ws://127.0.0.1:{listener.getsockname()[1]}/control'
    requests, errors, stop = [], [], threading.Event()
    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with conn:
                    conn.settimeout(2)
                    request = b''
                    while b'\r\n\r\n' not in request:
                        chunk = conn.recv(4096)
                        if not chunk:
                            raise RuntimeError('client closed during handshake')
                        request += chunk
                    requests.append(request.decode())
                    key = next(line.split(': ', 1)[1] for line in request.decode().split('\r\n')
                               if line.startswith('Sec-WebSocket-Key:'))
                    accept = t.base64.b64encode(t.hashlib.sha1(
                        (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                    response = ('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n' if reject else
                                'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                                f'Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n')
                    conn.sendall(response.encode())
            except Exception as error:
                errors.append(error)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield endpoint, requests
    finally:
        stop.set()
        thread.join(3)
        listener.close()
        if errors:
            raise errors[0]


class RemotePreflightTests(unittest.TestCase):
    def record(self, endpoint='unix://', **extra):
        return dict(kind='codex', cwd='/tmp', argv=['codex', 'resume', 'thread-id', '--remote', endpoint], **extra)

    def test_local_and_non_codex_sessions_do_not_probe_remote(self):
        records = [dict(kind='codex', argv=['codex', 'resume', 'thread-id']),
                   dict(kind='codex', argv=None), dict(kind='shell'),
                   dict(kind='claude', argv=['claude', '--resume', 'thread-id']),
                   dict(kind='vim', argv=['vim', 'notes.txt'])]
        data = {'windows': {'@0': {'panes': [dict(restore=r) for r in records]}}}
        with mock.patch.object(t, 'probe_remote') as probe:
            t.check_remote_connections([(Path('/tmp'), data)])
            probe.assert_not_called()

    def test_default_endpoint_uses_saved_home_and_option_values_are_not_flags(self):
        record = self.record(env={'CODEX_HOME': '/saved/codex'})
        self.assertEqual(t.remote_connection(record), ('unix:///saved/codex/app-server-control/app-server-control.sock', None))
        record['argv'] = ['codex', 'resume', 'thread-id', '-c', '--remote']
        self.assertIsNone(t.remote_connection(record))
        record['argv'] = ['codex', 'resume', 'thread-id', '--remote=unix:///custom.sock']
        self.assertEqual(t.remote_connection(record), ('unix:///custom.sock', None))

    def test_missing_and_stale_unix_sockets_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'remote.sock'
            with self.assertRaises(OSError):
                t.probe_remote('unix://' + str(path))
            with socket.socket(socket.AF_UNIX) as stale:
                stale.bind(str(path))
            with self.assertRaises(OSError):
                t.probe_remote('unix://' + str(path))

    def test_websocket_handshake_unix_and_tcp_with_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in [Path(tmp) / 'remote.sock', None]:
                with remote_listener(path) as (endpoint, requests):
                    t.probe_remote(endpoint, 'test-token')
                    self.assertIn('Authorization: Bearer test-token', requests[0])
        with remote_listener(reject=True) as (endpoint, _):
            with self.assertRaisesRegex(ValueError, 'HTTP 403'):
                t.probe_remote(endpoint)

    def test_missing_authentication_and_duplicate_endpoints(self):
        record = self.record()
        record['argv'] += ['--remote-auth-token-env', 'TMUX_RESUME_TEST_TOKEN']
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'TMUX_RESUME_TEST_TOKEN is not set'):
                t.remote_connection(record)
        record['env'] = {'TMUX_RESUME_TEST_TOKEN': 'test-token'}
        data = {'windows': {'@0': {'panes': [dict(location=f'work:{i}', restore=record) for i in range(3)]}}}
        with mock.patch.object(t, 'probe_remote') as probe:
            t.check_remote_connections([(Path('/tmp'), data)])
            self.assertEqual(probe.call_count, 1)

    def test_failed_preflight_never_reaches_tmux_or_pm_build_including_dry_run(self):
        data = {'windows': {'@0': {'panes': [dict(location='pm-test:1', restore=self.record())]}}}
        for dry_run in [False, True]:
            with mock.patch.object(t, 'probe_remote', side_effect=ConnectionRefusedError('Connection refused')), \
                 mock.patch.object(t, 'restore_locked') as build:
                with self.assertRaisesRegex(RuntimeError, 'nothing restored'):
                    t.restore_all([(Path('/tmp'), dict(data, server='/tmp/test.sock|1|1'))], dry_run=dry_run)
                build.assert_not_called()


class CodexUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='resume-update-')
        self.root = Path(self.tmp.name)
        self.home = self.root / 'custom-codex'
        self.home.mkdir()
        self.client = self.root / 'codex'
        self.client.write_text('#!/bin/sh\nprintf "codex-cli 0.155.0\\n"\n')
        self.client.chmod(0o755)
        self.record = dict(kind='codex', cwd=str(self.root), env={'CODEX_HOME': str(self.home)},
                           argv=[str(self.client), 'resume', 'test-thread'])
        self.data = {'windows': {'@0': {'panes': [dict(location='work:0', restore=self.record)]}}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_automatic_codex_resumes_means_no_update_checks(self):
        records = [dict(kind='shell'), dict(kind='vim', argv=['vim', 'notes.txt']),
                   dict(kind='claude', argv=['claude', '--resume', 'thread-id']),
                   dict(kind='codex', argv=None)]
        data = {'windows': {'@0': {'panes': [dict(restore=r) for r in records]}}}
        with mock.patch.object(t, 'run') as execute, mock.patch.object(t, 'latest_codex_version') as latest:
            t.check_codex_updates([(self.root, data)])
            t.check_codex_updates([])
            execute.assert_not_called()
            latest.assert_not_called()

    def cache(self, version='0.155.1', stale=False):
        checked = t.dt.datetime.now(t.dt.timezone.utc) - t.dt.timedelta(hours=21 if stale else 0)
        t.write_json(self.home / 'version.json', dict(latest_version=version, last_checked_at=checked.isoformat()))

    def test_pending_update_blocks_and_installing_it_clears_guard(self):
        self.cache()
        with mock.patch.object(t, 'urlopen', side_effect=AssertionError('fresh cache must not use network')):
            with self.assertRaisesRegex(RuntimeError, '0.155.0 -> 0.155.1'):
                t.check_codex_updates([(self.root, self.data)])
            self.client.write_text('#!/bin/sh\nprintf "codex-cli 0.155.1\\n"\n')
            t.check_codex_updates([(self.root, self.data)])
        self.assertEqual(t.read_json(self.home / 'version.json')['latest_version'], '0.155.1')

    def test_version_comparison_numeric_and_newer_installed_is_allowed(self):
        self.assertGreater(t.codex_version('0.155.10'), t.codex_version('0.155.9'))
        self.cache('0.99.9')
        t.check_codex_updates([(self.root, self.data)])

    def test_stale_cache_refreshed_once_for_multiple_panes(self):
        self.cache(stale=True)
        self.data['windows']['@0']['panes'].append(dict(location='work:1', restore=self.record))
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"tag_name":"rust-v0.156.0"}'
        with mock.patch.object(t, 'urlopen', return_value=response) as fetch, mock.patch.object(t, 'run', wraps=t.run) as version:
            with self.assertRaisesRegex(RuntimeError, '0.156.0'):
                t.check_codex_updates([(self.root, self.data)])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(version.call_count, 1)

    def test_network_failure_uses_cache_or_requires_explicit_skip(self):
        self.cache('0.155.0', stale=True)
        with mock.patch.object(t, 'urlopen', side_effect=OSError('offline')):
            t.check_codex_updates([(self.root, self.data)])
            (self.home / 'version.json').unlink()
            self.data['windows']['@0']['panes'].append(dict(location='work:1', restore=self.record))
            with mock.patch.object(t, 'urlopen', side_effect=OSError('offline')) as fetch:
                with self.assertRaisesRegex(RuntimeError, '--skip-codex-update'):
                    t.check_codex_updates([(self.root, self.data)])
                self.assertEqual(fetch.call_count, 1)
        with mock.patch.object(t, 'run', side_effect=AssertionError('bypass must not execute version checks')):
            t.check_codex_updates([(self.root, self.data)], skip=True)

    def test_suppression_overrides_saved_true_without_mutating_or_growing_snapshot_argv(self):
        self.record['argv'] += ['-c', 'check_for_update_on_startup=true']
        original = list(self.record['argv'])
        actual = t.restore_argv(self.record)
        self.assertEqual(actual[-2:], ['-c', 'check_for_update_on_startup=false'])
        self.assertEqual(self.record['argv'], original)
        self.record['argv'] = actual
        self.assertEqual(t.restore_argv(self.record), actual)
        self.record['kind'] = 'claude'
        self.record['argv'] = ['claude', '--resume', 'thread-id']
        self.assertEqual(t.restore_argv(self.record), self.record['argv'])

    def test_local_sessions_and_newer_cache_are_not_ignored(self):
        self.cache('0.156.0', stale=True)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"tag_name":"rust-v0.155.0"}'
        with mock.patch.object(t, 'urlopen', return_value=response):
            with self.assertRaisesRegex(RuntimeError, '0.156.0'):
                t.check_codex_updates([(self.root, self.data)])


class OptionsTests(unittest.TestCase):
    def test_codex_alias_and_quoted_values(self):
        args = ['codex', '--remote', 'unix://', '-c', 'sandbox_workspace_write.network_access=true',
                '--sandbox', 'workspace-write', '--ask-for-approval', 'never', '--search', '--cd', '/a b',
                'resume', '--last', 'old prompt']
        options, omitted = t.resume_options(args, 'codex')
        self.assertEqual(options, args[1:12])
        self.assertEqual(omitted, ['resume', '--last', 'old prompt'])

    def test_remote_resume_uses_stored_permissions_and_keeps_other_options(self):
        argv = ['codex', 'resume', 'thread-id', '--remote', 'unix://', '--sandbox', 'workspace-write',
                '--ask-for-approval=never', '-c', 'sandbox_workspace_write.network_access=true',
                '-c', 'model_reasoning_effort="low"', '--model', 'test-model', '--search', '--cd', '/a b',
                '--add-dir=/extra', '-sread-only', '-anever', '--config=permissions.network.enabled=true']
        kept, dropped = t.remote_resume_arguments(argv)
        self.assertEqual(kept, ['codex', 'resume', 'thread-id', '--remote', 'unix://', '-c',
                               'model_reasoning_effort="low"', '--model', 'test-model', '--search', '--cd', '/a b'])
        self.assertIn('sandbox_workspace_write.network_access=true', dropped)
        local = ['codex', 'resume', 'thread-id', '--sandbox', 'workspace-write', '--ask-for-approval', 'never']
        self.assertEqual(t.remote_resume_arguments(local), (local, []))

    def test_existing_remote_snapshots_get_compatible_resume_without_rewriting_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            argv = ['codex', 'resume', 'thread-id', '--remote', 'unix://', '--sandbox', 'workspace-write']
            data = {'version': 1, 'warnings': [], 'windows': {'@0': {'panes': [
                {'location': 'test:0', 'restore': {'kind': 'codex', 'argv': argv}}]}}}
            t.write_json(path / 'snapshot.json', data)
            _, updated = t.find_snapshot(str(path))
            r = updated['windows']['@0']['panes'][0]['restore']
            self.assertEqual(r['argv'], argv[:-2])
            self.assertEqual(r['original_resume_argv'], argv)
            self.assertEqual(r['remote_permission_arguments'], argv[-2:])
            self.assertEqual(t.read_json(path / 'snapshot.json'), data)

    def test_claude_startup_is_not_replayed(self):
        options, omitted = t.resume_options(['claude', '--model', 'opus', '--effort=max',
            '--allowedTools', 'Bash(git *)', 'Read', '--resume', 'old', '--fork-session',
            '--session-id=new', '--', 'prompt'], 'claude')
        self.assertEqual(options, ['--model', 'opus', '--effort=max', '--allowedTools', 'Bash(git *)', 'Read'])
        self.assertIn('--fork-session', omitted)

    def test_unknown_flag_and_noninteractive_block(self):
        for argv in [['codex', '--new-unknown', 'value'], ['codex', 'exec', 'run it'], ['claude', '-p', 'run it']]:
            with self.assertRaises(ValueError):
                t.resume_options(argv, argv[0])

    def test_codex_ambiguous_transcripts_are_not_latest_by_directory(self):
        index = t.CodexIndex()
        sentence = 'This is a long assistant message that uniquely identifies the conversation displayed in the pane and contains enough text for identification.'
        proc = {'cwd': '/nonexistent/test', 'pid': 999999999, 'start': '0'}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            env = {'CODEX_HOME': str(home)}
            index.cache[(str(home), proc['cwd'])] = [('first', 'first.jsonl', [t.normalize(sentence)]), ('second', 'second.jsonl', [t.normalize('unrelated message')])]
            self.assertEqual(index.identify(proc, env, sentence)[0], 'first')
            index.cache[(str(home), proc['cwd'])][1][2].append(t.normalize(sentence))
            self.assertIsNone(index.identify(proc, env, sentence)[0])

    def test_prefill_prefers_foreground_job_and_supports_old_snapshots(self):
        pane = {'restore': {'kind': 'shell', 'cwd': '/project'}, 'processes': [
            {'argv': ['bash'], 'cwd': '/project', 'pgid': 1, 'tpgid': 3},
            {'argv': ['background-worker'], 'cwd': '/project', 'pgid': 2, 'tpgid': 3},
            {'argv': ['nano', 'notes'], 'cwd': '/other dir', 'pgid': 3, 'tpgid': 3}]}
        self.assertEqual(t.prefill_command(pane), "cd -- '/other dir' && nano notes")
        for proc in pane['processes']:
            proc.pop('pgid')
            proc.pop('tpgid')
        self.assertEqual(t.prefill_command(pane), 'background-worker')

    def test_interactive_shell_rcfile_is_not_a_running_script(self):
        for argv in [['bash', '--rcfile', '/path with spaces/init.bash', '-i'], ['bash', '-il'], ['bash', '-o', 'vi']]:
            self.assertTrue(t.idle_shell({'argv': argv}), argv)
        for argv in [['bash', '-lc', 'sleep 60'], ['bash', 'script.sh'], ['bash', '-ic', 'job']]:
            self.assertFalse(t.idle_shell({'argv': argv}), argv)

    def test_codex_shared_text_requires_distinguishing_evidence(self):
        shared = 'This is shared assistant text that is deliberately long enough to pass the matching threshold in both conversations without uniquely identifying either of the two conversations.'
        first = 'The first conversation has this specific substantial line about its own separate checkpoint and recovery marker.'
        second = 'The second conversation has this other substantial line about a different checkpoint and a different recovery marker.'
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            proc = {'cwd': '/nonexistent/test', 'pid': 999999999, 'start': '0'}
            index = t.CodexIndex()
            index.cache[(str(home), proc['cwd'])] = [
                ('first', 'first.jsonl', [t.normalize(shared), t.normalize(first)]),
                ('second', 'second.jsonl', [t.normalize(shared), t.normalize(second)])]
            self.assertEqual(index.identify(proc, {'CODEX_HOME': str(home)}, shared + '\n' + first)[0], 'first')
            self.assertIsNone(index.identify(proc, {'CODEX_HOME': str(home)}, shared)[0])
            self.assertIsNone(index.identify(proc, {'CODEX_HOME': str(home)}, shared + '\n' + first + '\n' + second)[0])

    def test_portable_install_and_separate_editor_installers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, HOME=str(root / 'home'), XDG_STATE_HOME=str(root / 'state'))
            prefix = root / 'prefix with spaces'
            subprocess.run(['sh', str(ROOT / 'install.sh'), '--prefix', str(prefix)], env=env, check=True, capture_output=True)
            installed = prefix / 'bin/tmux-resume'
            self.assertEqual(installed.resolve(), prefix / 'share/tmux-resume/tmux_resume.py')
            self.assertEqual((prefix / 'share/tmux-resume/prefill.bash').read_bytes(), (ROOT / 'prefill.bash').read_bytes())
            plugin = root / 'home/.vim/plugin/tmux_resume.vim'
            self.assertTrue(plugin.is_file())
            self.assertFalse(plugin.is_symlink())
            config = root / 'home/.config/tmux-resume/config.json'
            self.assertEqual(t.read_json(config), {'keep_checkpoints': 100})
            t.write_json(config, {'keep_checkpoints': 25})
            subprocess.run(['sh', str(ROOT / 'install.sh'), '--prefix', str(prefix)], env=env, check=True, capture_output=True)
            self.assertEqual(t.read_json(config), {'keep_checkpoints': 25})
            subprocess.run([str(installed), 'install-emacs-plugin'], env=env, check=True, capture_output=True)
            self.assertTrue((root / 'home/.emacs.d/tmux-resume.el').is_file())
            result = subprocess.run([str(installed), '--help'], cwd=directory, env=env, check=True, capture_output=True, text=True)
            self.assertIn('install-vim-plugin', result.stdout)

    def test_install_renames_history_config_and_helpers_without_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'home'
            prefix = home / '.local'
            old_bundle = prefix / 'share' / t.LEGACY_NAME
            old_bundle.mkdir(parents=True)
            old_script = old_bundle / (t.LEGACY_STEM + '.py')
            old_script.write_text('# previous installation\n')
            old_bin = prefix / 'bin' / t.LEGACY_NAME
            old_bin.parent.mkdir()
            old_bin.symlink_to(old_script)
            state_home = root / 'state'
            old_state = state_home / t.LEGACY_NAME
            checkpoint = old_state / '20260912-000000-abcdef'
            checkpoint.mkdir(parents=True)
            data = dict(version=1, created='2026-09-12T00:00:00+00:00', host='test',
                        server='/tmp/test.sock|1|0', windows={}, sessions=[], warnings=[], blockers=[])
            t.write_json(checkpoint / 'snapshot.json', data)
            t.write_json(old_state / 'restores.json', [{'retained': 'receipt data'}])
            (old_state / 'latest').symlink_to(checkpoint)
            config_home = root / 'config'
            config = config_home / t.LEGACY_NAME / 'config.json'
            config.parent.mkdir(parents=True)
            t.write_json(config, {'keep_checkpoints': 17})
            old_vim = home / '.vim/plugin' / (t.LEGACY_STEM + '.vim')
            old_emacs = home / '.emacs.d' / (t.LEGACY_NAME + '.el')
            for file in (old_vim, old_emacs):
                file.parent.mkdir(parents=True)
                file.write_text('previous helper')
            env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(state_home), XDG_CONFIG_HOME=str(config_home))
            subprocess.run(['sh', str(ROOT / 'install.sh'), '--emacs-plugin'], env=env, capture_output=True, check=True)
            new_state = state_home / 'tmux-resume'
            self.assertEqual((new_state / 'latest').resolve(), new_state / checkpoint.name)
            self.assertEqual(t.read_json(new_state / checkpoint.name / 'snapshot.json'), data)
            self.assertEqual(t.read_json(new_state / 'restores.json'), [{'retained': 'receipt data'}])
            self.assertEqual(t.read_json(config_home / 'tmux-resume/config.json'), {'keep_checkpoints': 17})
            for old in (old_bin, old_bundle, old_state, config.parent, old_vim, old_emacs):
                self.assertFalse(old.exists() or old.is_symlink(), old)
            self.assertEqual((home / '.vim/plugin/tmux_resume.vim').read_bytes(), (ROOT / 'tmux_resume.vim').read_bytes())
            self.assertEqual((home / '.emacs.d/tmux-resume.el').read_bytes(), (ROOT / 'tmux-resume.el').read_bytes())
            result = subprocess.run([str(prefix / 'bin/tmux-resume'), 'history'], env=env, capture_output=True, text=True, check=True)
            self.assertIn(str(new_state / checkpoint.name), result.stdout)
            # Reinstalling keeps the migrated checkpoint and configuration.
            subprocess.run(['sh', str(ROOT / 'install.sh'), '--emacs-plugin'], env=env, capture_output=True, check=True)
            self.assertEqual(t.read_json(config_home / 'tmux-resume/config.json'), {'keep_checkpoints': 17})

    def test_rename_conflict_preserves_both_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in (t.LEGACY_NAME, 'tmux-resume'):
                (root / 'state' / name).mkdir(parents=True)
                (root / 'state' / name / 'keep').write_text(name)
            with mock.patch.object(t, 'STATE', root / 'state/tmux-resume'), \
                 mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(root / 'config')}):
                with self.assertRaisesRegex(RuntimeError, 'nothing migrated'):
                    t.migrate_installation(root / 'prefix')
            for name in (t.LEGACY_NAME, 'tmux-resume'):
                self.assertEqual((root / 'state' / name / 'keep').read_text(), name)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tmux-resume-history-')
        self.root = Path(self.tmp.name)
        self.state_patch = mock.patch.object(t, 'STATE', self.root / 'state')
        self.state_patch.start()
        self.env_patch = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'config')})
        self.env_patch.start()
        t.private_dir(t.STATE)
        self.proc_patch = mock.patch.object(t, 'processes', return_value={})
        self.proc_patch.start()
        self.receipt_patch = mock.patch.object(t, 'active_restores', return_value=[])
        self.receipt_patch.start()

    def tearDown(self):
        self.receipt_patch.stop()
        self.proc_patch.stop()
        self.env_patch.stop()
        self.state_patch.stop()
        self.tmp.cleanup()

    def checkpoint(self, number, **values):
        path = t.STATE / f'20260912-000000-{number:06x}'
        path.mkdir()
        data = dict(version=1, created=f'2026-09-12T00:00:00.{number:06d}+00:00', host='test',
                    server='/tmp/test.sock|1|0', windows={}, sessions=[{'session_id': '$0'}], warnings=[], blockers=[])
        data.update(values)
        t.write_json(path / 'snapshot.json', data)
        return path

    def test_default_limit_and_config_override_validation(self):
        self.assertEqual(t.history_limit(), 100)
        config = self.root / 'config/tmux-resume/config.json'
        config.parent.mkdir(parents=True)
        for value in (2, 0):
            t.write_json(config, {'keep_checkpoints': value})
            self.assertEqual(t.history_limit(), value)
            self.assertEqual(t.history_limit(7), 7)
        for value in (-1, True, '100', None):
            t.write_json(config, {'keep_checkpoints': value})
            with self.assertRaisesRegex(RuntimeError, 'nonnegative integer'):
                t.history_limit()
        config.write_text('{invalid')
        with self.assertRaisesRegex(RuntimeError, 'Invalid JSON'):
            t.history_limit()

    def test_retention_keeps_newest_100_and_zero_keeps_all(self):
        paths = [self.checkpoint(i) for i in range(102)]
        t.prune_history(0)
        self.assertTrue(all(path.exists() for path in paths))
        with redirect_stdout(io.StringIO()):
            t.prune_history(t.history_limit())
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertEqual([entry[0] for entry in t.checkpoint_history()], paths[:1:-1])

    def test_retention_groups_close_check_and_preserves_exports_and_incomplete_data(self):
        old = self.checkpoint(1)
        final = old / 'close-check'
        final.mkdir()
        t.write_json(final / 'snapshot.json', t.read_json(old / 'snapshot.json'))
        (final / 'close.log').touch()
        self.assertEqual(t.checkpoint_history()[0][1], final)
        export = self.checkpoint(2, history_managed=False)
        incomplete = self.checkpoint(3)
        (incomplete / 'snapshot.json').write_text('{incomplete')
        external = self.root / 'export'
        external.mkdir()
        link = t.STATE / '20260912-000000-ffffff'
        link.symlink_to(external, target_is_directory=True)
        new = self.checkpoint(4)
        with redirect_stdout(io.StringIO()):
            t.prune_history(1)
        self.assertFalse(old.exists())
        self.assertTrue(all(p.exists() for p in (export, incomplete, link, external, new)))

    def test_retention_protects_latest_live_restore_and_pending_close(self):
        paths = [self.checkpoint(i) for i in range(6)]
        (t.STATE / 'latest').symlink_to(paths[0])
        receipt_data = t.read_json(paths[1] / 'snapshot.json')
        receipt = {'workspace': t.workspace_key(receipt_data, receipt_data['sessions'][0])}
        proc = {'argv': [sys.executable, str(ROOT / 'tmux_resume.py'), '_close-all', str(paths[2])]}
        runner = {'argv': [sys.executable, str(ROOT / 'tmux_resume.py'), '_run', str(paths[3]), '%0', '/bin/bash']}
        with mock.patch.object(t, 'active_restores', return_value=[receipt]), \
             mock.patch.object(t, 'processes', return_value={123: proc, 124: runner}), redirect_stdout(io.StringIO()):
            t.prune_history(1)
        self.assertTrue(all(paths[i].exists() for i in (0, 1, 2, 3, 5)))
        self.assertFalse(paths[4].exists())

    def test_history_orders_by_creation_time_not_random_directory_suffix(self):
        old = self.checkpoint(1)
        new = self.checkpoint(2)
        renamed = old.with_name('20260912-000000-ffffff')
        old.rename(renamed)
        self.assertEqual([entry[0] for entry in t.checkpoint_history()], [new, renamed])
        with redirect_stdout(io.StringIO()):
            t.prune_history(1)
        self.assertTrue(new.exists())
        self.assertFalse(renamed.exists())

    def test_retention_defers_during_restore(self):
        paths = [self.checkpoint(i) for i in range(2)]
        with t.restore_lock(), mock.patch('sys.stderr', new_callable=io.StringIO) as errors:
            t.prune_history(1)
        self.assertIn('pruning was deferred', errors.getvalue())
        self.assertTrue(all(path.exists() for path in paths))

    def test_systemd_generation_has_shutdown_ordering_and_safe_argument_quoting(self):
        account = t.pwd.getpwuid(os.getuid())
        prefix = self.root / 'prefix with % and $ spaces'
        command = prefix / 'bin/tmux-resume'
        command.parent.mkdir(parents=True)
        command.write_text('#!/bin/sh\nexit 0\n')
        command.chmod(0o755)
        destination = self.root / 'units'
        with redirect_stdout(io.StringIO()):
            t.install_systemd(account.pw_name, prefix, destination)
        name = f'tmux-resume-shutdown-{os.getuid()}.service'
        service = (destination / name).read_text()
        dropin = (destination / f'session-.scope.d/80-tmux-resume-{os.getuid()}.conf').read_text()
        self.assertIn('Before=' + name, dropin)
        self.assertIn(f'user@{os.getuid()}.service', service)
        self.assertIn('RemainAfterExit=yes', service)
        self.assertIn(f'User={os.getuid()}', service)
        self.assertIn(' save --if-running', service)
        self.assertNotIn('--close', service)
        self.assertIn('ExecStop=:"', service)
        self.assertIn('prefix with %% and $ spaces', service)
        if shutil.which('systemd-analyze'):
            result = subprocess.run(['systemd-analyze', 'verify', str(destination / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tmux-resume-test-')
        self.root = Path(self.tmp.name)
        self.sock = str(self.root / 'tmux.sock')
        self.tmux = t.Tmux(self.sock)
        self.original_state = t.STATE
        self.state = self.root / 'state'
        t.STATE = self.state / 'tmux-resume'
        # Background native compilation is unrelated to editor state checks
        # and can otherwise appear as a running Emacs process buffer.
        isolated = dict(XDG_STATE_HOME=str(self.state), XDG_CONFIG_HOME=str(self.root / 'config'),
                        EMACS_INHIBIT_AUTOMATIC_NATIVE_COMPILATION='1')
        self.env = dict(os.environ, **isolated)
        self.env.pop('TMUX', None)
        self.env_patch = mock.patch.dict(os.environ, isolated)
        self.env_patch.start()
        subprocess.run(['tmux', '-S', self.sock, '-f', '/dev/null', 'new-session', '-d', '-s', 'work',
                        '-x', '120', '-y', '40', '-c', str(self.root), '/bin/bash --noprofile --norc'],
                       env=self.env, check=True, capture_output=True)
        self.tmux.call('set-option', '-g', 'default-shell', '/bin/bash')
        self.tmux.call('set-option', '-g', 'default-command', '/bin/bash --noprofile --norc')

    def tearDown(self):
        self.tmux.call('kill-server', allow_fail=True)
        self.env_patch.stop()
        t.STATE = self.original_state
        self.tmp.cleanup()

    def snapshot(self, name='snap'):
        return t.snapshot(self.tmux, self.root / name)

    def test_layout_links_groups_names_and_collision(self):
        middle = self.root / 'middle pane'
        bottom = self.root / 'bottom pane'
        middle.mkdir()
        bottom.mkdir()
        self.tmux.call('rename-window', '-t', 'work:0', 'code "quoted" $HOME')
        self.tmux.call('split-window', '-h', '-t', 'work:0', '-c', str(middle), '/bin/bash --noprofile --norc')
        self.tmux.call('split-window', '-v', '-t', 'work:0.1', '-c', str(bottom), '/bin/bash --noprofile --norc')
        self.tmux.call('select-pane', '-t', 'work:0.1')
        self.tmux.call('new-window', '-d', '-t', 'work:4', '-n', 'notes')
        self.tmux.call('new-session', '-d', '-s', 'linked', '-t', 'work')
        self.tmux.call('new-session', '-d', '-s', 'other', '/bin/bash --noprofile --norc')
        self.tmux.call('link-window', '-s', 'work:0', '-t', 'other:3')
        self.tmux.call('select-window', '-t', 'work:4')
        self.tmux.call('select-window', '-t', 'linked:0')
        self.tmux.call('resize-pane', '-Z', '-t', 'linked:0.1')
        time.sleep(.2)
        data = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'already exist'):
            t.restore(self.tmux, self.root / 'snap', data)
        self.tmux.call('kill-server')
        t.restore(self.tmux, self.root / 'snap', data)
        time.sleep(.3)
        restored = self.snapshot('restored')
        def structure(d):
            return [(s['session_name'], [(l['index'], d['windows'][l['id']]['window_name'],
                    len(d['windows'][l['id']]['panes'])) for l in s['links']],
                    next(l['index'] for l in s['links'] if l['id'] == s['window_id'])) for s in d['sessions']]
        self.assertEqual(structure(data), structure(restored))
        self.assertEqual(len(data['windows']), len(restored['windows']))
        before = next(w for w in data['windows'].values() if len(w['panes']) == 3)
        after = next(w for w in restored['windows'].values() if len(w['panes']) == 3)
        geometry = lambda w: __import__('re').sub(r'(\d+x\d+,\d+,\d+),\d+', r'\1,P', w['window_layout'].split(',', 1)[1])
        self.assertEqual(geometry(before), geometry(after))
        self.assertEqual(before['window_zoomed_flag'], '1')
        self.assertEqual(after['window_zoomed_flag'], before['window_zoomed_flag'])
        active = lambda w: next(p['pane_index'] for p in w['panes'] if p['pane_active'] == '1')
        self.assertEqual(active(before), '1')
        self.assertEqual(active(after), active(before))
        positions = lambda w: [(p['pane_index'], p['restore']['cwd']) for p in w['panes']]
        self.assertEqual(positions(after), positions(before))

    def test_targeted_save_restore_and_close_leave_other_sessions_alone(self):
        self.tmux.call('new-session', '-d', '-s', 'unrelated', 'vim -Nu NONE -n -i NONE')
        eventually(lambda: self.tmux.call('display-message', '-p', '-t', 'unrelated:0', '#{pane_current_command}') == 'vim')
        data = t.snapshot(self.tmux, self.root / 'scoped', ['work'])
        self.assertEqual([s['session_name'] for s in data['sessions']], ['work'])
        self.assertEqual(data['blockers'], [])
        full = self.snapshot('full')
        scoped = t.select_snapshot(full, ['work'])
        self.assertEqual(scoped['blockers'], [])
        self.assertEqual(len(scoped['windows']), 1)
        with self.assertRaisesRegex(RuntimeError, 'Session not found'):
            t.select_snapshot(full, ['missing'])
        self.tmux.call('kill-session', '-t', '=work')
        t.restore(self.tmux, self.root / 'full', scoped)
        ready = self.root / 'shell-ready'
        self.tmux.call('send-keys', '-t', 'work:0', ': > ' + shlex.quote(str(ready)), 'Enter')
        eventually(lambda: ready.exists())
        self.assertEqual(self.tmux.call('display-message', '-p', '-t', 'unrelated:0', '#{pane_current_command}'), 'vim')
        result = subprocess.run(['python3', str(ROOT / 'tmux_resume.py'), '--socket', self.sock,
            'save', '--session', 'work', '--close', '--output', str(self.root / 'closing-scoped')],
            env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        eventually(lambda: self.tmux.call('list-sessions', '-F', '#{session_name}') == 'unrelated')

    def test_repeated_restore_blocks_other_socket_even_after_rename(self):
        data = self.snapshot()
        self.tmux.call('kill-server')
        t.restore(self.tmux, self.root / 'snap', data)
        self.tmux.call('rename-session', '-t', '=work', 'renamed')
        other = t.Tmux(str(self.root / 'other.sock'))
        with self.assertRaisesRegex(RuntimeError, 'already restored'):
            t.restore(other, self.root / 'snap', data)
        self.assertEqual(other.call('list-sessions', allow_fail=True), '')
        self.tmux.call('kill-server')
        try:
            t.restore(other, self.root / 'snap', data)
            self.assertEqual(other.call('list-sessions', '-F', '#{session_name}'), 'work')
        finally:
            other.call('kill-server', allow_fail=True)

    def test_skip_existing_restores_missing_session_without_touching_live_pane(self):
        self.tmux.call('new-session', '-d', '-s', 'missing', '/bin/bash --noprofile --norc')
        data = self.snapshot()
        original = json.dumps(data, sort_keys=True)
        existing = self.tmux.call('list-panes', '-t', 'work', '-F', '#{pane_id}|#{pane_pid}')
        self.tmux.call('kill-session', '-t', '=missing')
        with self.assertRaisesRegex(RuntimeError, 'already exist'):
            t.restore(self.tmux, self.root / 'snap', data)
        output = io.StringIO()
        with redirect_stdout(output):
            t.restore(self.tmux, self.root / 'snap', data, dry_run=True, skip_existing=True)
        self.assertIn('Skipping work', output.getvalue())
        self.assertIn('missing:', output.getvalue())
        self.assertEqual(self.tmux.call('list-sessions', '-F', '#{session_name}'), 'work')
        t.restore(self.tmux, self.root / 'snap', data, skip_existing=True)
        self.assertEqual(self.tmux.call('list-sessions', '-F', '#{session_name}').splitlines(), ['missing', 'work'])
        self.assertEqual(self.tmux.call('list-panes', '-t', 'work', '-F', '#{pane_id}|#{pane_pid}'), existing)
        before = self.tmux.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}')
        output = io.StringIO()
        with redirect_stdout(output):
            t.restore(self.tmux, self.root / 'snap', data, skip_existing=True)
        self.assertIn('No missing sessions', output.getvalue())
        self.assertEqual(self.tmux.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}'), before)
        self.assertEqual(json.dumps(data, sort_keys=True), original)

    def test_skip_existing_retains_duplicate_agent_guard_for_missing_sessions(self):
        self.tmux.call('new-session', '-d', '-s', 'missing', '/bin/bash --noprofile --norc')
        data = self.snapshot()
        self.tmux.call('kill-session', '-t', '=missing')
        with mock.patch.object(t, 'live_agent_conflicts', return_value=['conversation already running elsewhere']) as guard:
            with self.assertRaisesRegex(RuntimeError, 'conversation already running'):
                t.restore(self.tmux, self.root / 'snap', data, skip_existing=True)
        self.assertEqual([s['session_name'] for s in guard.call_args.args[0]['sessions']], ['missing'])
        self.assertEqual(self.tmux.call('list-sessions', '-F', '#{session_name}'), 'work')

    def test_skip_existing_receipt_recognizes_renamed_session_on_another_socket(self):
        self.tmux.call('new-session', '-d', '-s', 'missing', '/bin/bash --noprofile --norc')
        data = self.snapshot()
        self.tmux.call('kill-server')
        t.restore(self.tmux, self.root / 'snap', t.select_snapshot(data, ['work']))
        self.tmux.call('rename-session', '-t', '=work', 'renamed')
        existing = self.tmux.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}')
        other = t.Tmux(str(self.root / 'other.sock'))
        try:
            t.restore(other, self.root / 'snap', data, skip_existing=True)
            self.assertEqual(other.call('list-sessions', '-F', '#{session_name}'), 'missing')
            self.assertEqual(self.tmux.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}'), existing)
        finally:
            other.call('kill-server', allow_fail=True)

    def test_skip_existing_pm_attachment_skips_entire_workspace(self):
        base = 'pm-test-12345678'
        self.tmux.call('rename-session', '-t', 'work', base)
        self.tmux.call('new-session', '-d', '-s', base + '~1', '-t', base)
        self.tmux.call('new-session', '-d', '-s', 'missing', '/bin/bash --noprofile --norc')
        data = self.snapshot()
        session = next(s for s in data['sessions'] if s['session_name'] == base)
        pane = data['windows'][session['links'][0]['id']]['panes'][0]
        pane['restore'] = dict(kind='pm', cwd=str(self.root))
        t.write_json(self.root / 'snap/snapshot.json', data)
        self.tmux.call('rename-session', '-t', '=' + base + '~1', base + '~8')
        self.tmux.call('kill-session', '-t', '=' + base)
        self.tmux.call('kill-session', '-t', '=missing')
        existing = self.tmux.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}')
        with mock.patch.object(t, 'start_pm', side_effect=AssertionError('existing PM must not restart')):
            t.restore(self.tmux, self.root / 'snap', data, skip_existing=True)
        self.assertEqual(self.tmux.call('list-sessions', '-F', '#{session_name}').splitlines(), ['missing', base + '~8'])
        self.assertEqual(self.tmux.call('list-panes', '-t', '=' + base + '~8', '-F', '#{pane_id}|#{pane_pid}'), existing)

    def test_simultaneous_restore_refuses_without_creating_sessions(self):
        data = self.snapshot()
        self.tmux.call('kill-server')
        t.private_dir(t.STATE)
        with (t.STATE / 'restore.lock').open('a') as lock:
            t.fcntl.flock(lock, t.fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, 'restore is in progress'):
                t.restore(self.tmux, self.root / 'snap', data)
        self.assertEqual(self.tmux.call('list-sessions', allow_fail=True), '')

    @unittest.skipUnless(shutil.which('emacs') and shutil.which('emacsclient'), 'Emacs and emacsclient are not installed')
    def test_emacs_dirty_clean_and_reopen(self):
        note = self.root / 'emacs note.txt'
        note.write_text('saved text\n')
        setup = self.root / 'setup.el'
        setup.write_text('(load ' + json.dumps(str(ROOT / 'tmux-resume.el')) + ' nil t)\n'
                         '(with-current-buffer (get-buffer-create "*Async-native-compile-log*") (let ((inhibit-read-only t)) (insert "Generated compiler diagnostic") (setq buffer-read-only t)))\n'
                         '(find-file ' + json.dumps(str(note)) + ')\n(goto-char (point-max))\n(insert "unsaved text\\n")\n')
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', shlex.join(['emacs', '-Q', '-nw', '--load', str(setup)]))
        eventually(lambda: list((t.STATE / 'emacs').glob('*.json')))
        dirty = self.snapshot('dirty-emacs')
        self.assertTrue(any('UNSAVED ' + str(note) in b for b in dirty['blockers']), dirty['blockers'])
        self.assertEqual(note.read_text(), 'saved text\n')
        self.tmux.call('send-keys', '-t', 'work:0', 'C-x', 'C-s')
        eventually(lambda: 'unsaved text' in note.read_text())
        clean = self.snapshot('clean-emacs')
        self.assertEqual(clean['blockers'], [])
        self.tmux.call('kill-server')
        t.restore(self.tmux, self.root / 'clean-emacs', clean)
        eventually(lambda: 'unsaved text' in self.tmux.call('capture-pane', '-p', '-t', 'work:0'))
        self.assertIn('saved text', self.tmux.call('capture-pane', '-p', '-t', 'work:0'))
        self.assertEqual(self.snapshot('reopened-emacs')['blockers'], [])

    @unittest.skipUnless(shutil.which('emacs') and shutil.which('emacsclient'), 'Emacs and emacsclient are not installed')
    def test_emacs_queries_matching_server_without_preloaded_helper(self):
        note = self.root / 'server-note.txt'
        note.write_text('saved text\n')
        setup = self.root / 'server.el'
        socket = str(self.root / 'emacs.sock')
        setup.write_text('(require \'server)\n(setq server-name ' + json.dumps(socket) + ')\n(server-start)\n'
                         '(find-file ' + json.dumps(str(note)) + ')\n(insert "dirty ")\n')
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', shlex.join(['env', 'EMACS_SOCKET_NAME=' + socket,
            'emacs', '-Q', '-nw', '--load', str(setup)]))
        eventually(lambda: Path(socket).exists())
        self.assertEqual(list((t.STATE / 'emacs').glob('*.json')), [])
        data = self.snapshot()
        self.assertTrue(any('UNSAVED ' + str(note) in b for b in data['blockers']), data['blockers'])
        self.assertTrue(next(iter(data['windows'].values()))['panes'][0]['restore'].get('session_file'))
        self.assertTrue(list((t.STATE / 'emacs').glob('*.json')))

    @unittest.skipUnless(shutil.which('emacs'), 'Emacs is not installed')
    def test_untracked_emacs_warns_and_blocks_closing(self):
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', 'emacs -Q -nw')
        eventually(lambda: 'emacs' in self.tmux.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}'))
        data = self.snapshot()
        self.assertTrue(any('Emacs buffer state is unknown' in b for b in data['blockers']), data['blockers'])
        self.assertTrue(any('no responding Emacs helper/server' in w for w in data['warnings']))

    @unittest.skipUnless(shutil.which('emacs') and shutil.which('emacsclient'), 'Emacs and emacsclient are not installed')
    def test_emacsclient_queries_server_and_standalone_rejects_wrong_server(self):
        socket = str(self.root / 'emacs.sock')
        note = self.root / 'client-note.txt'
        note.write_text('saved text\n')
        setup = self.root / 'server.el'
        setup.write_text('(require \'server)\n(setq server-name ' + json.dumps(socket) + ')\n(server-start)\n'
                         '(find-file ' + json.dumps(str(note)) + ')\n(insert "dirty ")\n')
        self.tmux.call('new-session', '-d', '-s', 'editor-server', shlex.join(['emacs', '-Q', '-nw', '--load', str(setup)]))
        eventually(lambda: Path(socket).exists())
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', shlex.join(['env', 'EMACS_SOCKET_NAME=' + socket, 'emacs', '-Q', '-nw']))
        eventually(lambda: 'emacs' in self.tmux.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}'))
        unknown = t.snapshot(self.tmux, self.root / 'wrong-server', ['work'])
        self.assertTrue(any('Emacs buffer state is unknown' in b for b in unknown['blockers']), unknown['blockers'])
        self.assertEqual(list((t.STATE / 'emacs').glob('*.json')), [])
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', shlex.join(['emacsclient', '-t', '-s', socket, str(note)]))
        eventually(lambda: 'client' in self.tmux.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}'))
        data = t.snapshot(self.tmux, self.root / 'client', ['work'])
        self.assertTrue(any('UNSAVED ' + str(note) in b for b in data['blockers']), data['blockers'])
        self.assertTrue(next(iter(data['windows'].values()))['panes'][0]['restore'].get('session_file'))

    def test_vim_dirty_clean_and_reopen(self):
        note = self.root / 'a note $literal.txt'
        note.write_text('saved text\nsecond line\n')
        argv = ['vim', '-Nu', 'NONE', '-n', '-i', 'NONE', '--cmd', 'source ' + str(ROOT / 'tmux_resume.vim'), str(note)]
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', shlex.join(argv))
        eventually(lambda: list((t.STATE / 'vim').glob('*.json')))
        self.tmux.call('send-keys', '-t', 'work:0', 'G', 'o', 'unsaved text', 'Escape')
        time.sleep(.15)
        dirty = self.snapshot('dirty')
        self.assertTrue(any('UNSAVED' in x and str(note) in x for x in dirty['blockers']), dirty['blockers'])
        self.assertNotIn('unsaved text', note.read_text())
        refused = subprocess.run(['python3', str(ROOT / 'tmux_resume.py'), '--socket', self.sock,
            'save', '--close', '--output', str(self.root / 'refused')], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('nothing closed', refused.stderr)
        self.assertEqual(self.tmux.call('list-sessions', '-F', '#{session_name}'), 'work')
        self.tmux.call('send-keys', '-t', 'work:0', ':w', 'Enter')
        eventually(lambda: 'unsaved text' in note.read_text())
        clean = self.snapshot('clean')
        self.assertEqual(clean['blockers'], [])
        self.tmux.call('send-keys', '-t', 'work:0', ':qa', 'Enter')
        time.sleep(.15)
        self.tmux.call('kill-server', allow_fail=True)
        t.restore(self.tmux, self.root / 'clean', clean)
        eventually(lambda: 'unsaved text' in self.tmux.call('capture-pane', '-p', '-t', 'work:0'))
        self.assertIn('saved text', self.tmux.call('capture-pane', '-p', '-t', 'work:0'))
        self.assertEqual(self.snapshot('reopened-vim')['blockers'], [])

    def test_untracked_vim_blocks_close(self):
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', 'vim -Nu NONE -n -i NONE')
        eventually(lambda: self.tmux.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}') == 'vim')
        data = self.snapshot()
        self.assertTrue(any('unsaved buffers are unknown' in b for b in data['blockers']))
        self.tmux.call('kill-server')
        t.restore(self.tmux, self.root / 'snap', data)
        eventually(lambda: 'vim -Nu NONE -n -i NONE' in self.tmux.call('capture-pane', '-p', '-J', '-t', 'work:0'))
        self.assertEqual(self.tmux.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}'), 'bash')

    def test_prefill_waits_for_enter_and_preserves_arguments(self):
        self.check_prefill_mode('emacs')

    def test_prefill_vi_mode_has_no_warnings_and_waits_for_enter(self):
        self.check_prefill_mode('vi')

    def check_prefill_mode(self, mode):
        script = self.root / 'record.py'
        output = self.root / 'args.json'
        script.write_text('import sys,json,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\ntime.sleep(120)\n')
        args = ['spaces and café', '', "quote'$(touch SHOULD_NOT_EXIST)`touch ALSO_NOT`!", 'line\nbreak\t\r\x1b', '']
        argv = ['python3', str(script), str(output)] + args
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', shlex.join(argv))
        eventually(lambda: output.exists())
        data = self.snapshot()
        self.tmux.call('kill-server')
        output.unlink()
        shell_home = self.root / 'shell-home'
        shell_home.mkdir()
        (shell_home / '.bashrc').write_text(f'set -o {mode}\nPS1="PREFILL_{mode}> "\n')
        with mock.patch.dict(os.environ, {'HOME': str(shell_home), 'INPUTRC': '/dev/null'}):
            t.restore(self.tmux, self.root / 'snap', data)
        expected = t.prefill_command(next(iter(data['windows'].values()))['panes'][0])
        eventually(lambda: f'PREFILL_{mode}> {expected}' in self.tmux.call('capture-pane', '-p', '-J', '-t', 'work:0'))
        time.sleep(.15)
        screen = self.tmux.call('capture-pane', '-p', '-J', '-t', 'work:0')
        self.assertNotIn('bash: bind:', screen)
        self.assertNotIn('invalid keymap', screen)
        self.assertFalse(output.exists(), 'prefilling must not execute the command')
        self.assertEqual(self.snapshot('prefilled-shell')['blockers'], [])
        self.tmux.call('send-keys', '-t', 'work:0', 'Enter')
        eventually(lambda: output.exists())
        self.assertEqual(json.loads(output.read_text()), args)
        self.assertFalse((self.root / 'SHOULD_NOT_EXIST').exists())
        self.assertFalse((self.root / 'ALSO_NOT').exists())

    def test_close_completes_when_invoked_inside_tmux(self):
        self.tmux.call('new-session', '-d', '-s', 'zz-last', '/bin/bash --noprofile --norc')
        output = self.root / 'close-output'
        command = shlex.join(['python3', str(ROOT / 'tmux_resume.py'), '--socket', self.sock,
            'save', '--close', '--output', str(self.root / 'closing')])
        self.tmux.call('send-keys', '-t', 'work:0', command + ' > ' + shlex.quote(str(output)) + ' 2>&1', 'Enter')
        eventually(lambda: (self.root / 'closing/close-check/snapshot.json').exists())
        eventually(lambda: not self.tmux.call('list-sessions', '-F', '#{session_name}', allow_fail=True))
        self.assertEqual((self.root / 'closing/close-check/close.log').read_text(), '')

    def test_failed_remote_resume_explains_and_prefills_exact_retry_without_enter(self):
        self.check_remote_resume_exit(available=False)

    def test_successful_remote_exit_leaves_an_empty_prompt(self):
        self.check_remote_resume_exit(available=True)

    def check_remote_resume_exit(self, available):
        client = self.root / 'codex'
        attempts = self.root / 'attempts.jsonl'
        ready = self.root / 'remote-ready'
        client.write_text('#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n'
                          f'with Path({str(attempts)!r}).open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                          f'if not Path({str(ready)!r}).exists():\n'
                          ' print("Error: failed to connect to remote app server: Connection refused")\n'
                          ' sys.exit(1)\n'
                          'print("Remote conversation resumed")\n')
        client.chmod(0o755)
        if available:
            ready.touch()
        data = self.snapshot()
        pane = next(iter(data['windows'].values()))['panes'][0]
        sid = '11111111-2222-4333-8444-555555555555'
        argv = [str(client), 'resume', sid, '--remote', 'unix://', '--search', '-c', 'model_reasoning_effort="low"']
        pane['restore'] = dict(kind='codex', cwd=str(self.root), argv=argv, session_id=sid)
        t.write_json(self.root / 'snap/snapshot.json', data)
        self.tmux.call('kill-server')
        shell_home = self.root / 'retry-home'
        shell_home.mkdir()
        (shell_home / '.bashrc').write_text('PS1="RETRY_TEST> "\n')
        # Model a server that passed preflight but fails when the pane launches.
        with mock.patch.dict(os.environ, {'HOME': str(shell_home), 'INPUTRC': '/dev/null'}), \
             mock.patch.object(t, 'probe_remote'):
            t.restore(self.tmux, self.root / 'snap', data, skip_codex_update=True)
        argv = t.restore_argv(pane['restore'])
        expected = shlex.join(argv)
        def screen():
            return self.tmux.call('capture-pane', '-p', '-J', '-t', 'work:0')
        eventually(lambda: 'RETRY_TEST> ' + (expected if not available else '') in screen())
        time.sleep(.15)
        self.assertEqual([json.loads(line) for line in attempts.read_text().splitlines()], [argv[1:]])
        if available:
            self.assertNotIn('Command prefilled', screen())
            self.assertNotIn('Remote Codex could not be resumed', screen())
        else:
            self.assertIn('Connection refused', screen())
            self.assertIn('remote-control server may not be running or reachable', screen())
            self.assertIn('After remote control is started, use the command below to resume', screen())
            self.assertFalse(ready.exists())
            ready.touch()  # The user starts their server separately.
            self.tmux.call('send-keys', '-t', 'work:0', 'Enter')
            eventually(lambda: len(attempts.read_text().splitlines()) == 2)
            self.assertEqual([json.loads(line) for line in attempts.read_text().splitlines()], [argv[1:], argv[1:]])

    def test_vim_swap_fallback_reports_dirty_but_never_assumes_clean(self):
        note = self.root / 'swap note.txt'
        note.write_text('saved text\n')
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', shlex.join(['vim', '-Nu', 'NONE', '-i', 'NONE', str(note)]))
        eventually(lambda: (self.root / '.swap note.txt.swp').exists())
        self.tmux.call('send-keys', '-t', 'work:0', 'Go', 'new text', 'Escape', ':preserve', 'Enter')
        time.sleep(.15)
        dirty = self.snapshot('swap-dirty')
        self.assertTrue(any('UNSAVED (swap evidence)' in b and str(note) in b for b in dirty['blockers']), dirty['blockers'])
        self.assertTrue(any('Swap headers may lag' in w for w in dirty['warnings']))
        self.tmux.call('send-keys', '-t', 'work:0', ':w', 'Enter')
        eventually(lambda: 'new text' in note.read_text())
        clean = self.snapshot('swap-clean')
        self.assertTrue(any('unsaved buffers are unknown' in b for b in clean['blockers']))

    def test_pm_creates_workspace_and_keeps_dashboard_without_saved_attachments(self):
        base = 'pm-test-12345678'
        bindir = self.root / 'bin'
        bindir.mkdir()
        pm = bindir / 'pm'
        marker = self.root / 'pm-started.json'
        worker = self.root / 'worker-resumed.json'
        pm.write_text('''#!/usr/bin/env python3
import json,os,pathlib,shlex,subprocess,sys,time
root=pathlib.Path(__file__).parent.parent
base='pm-test-12345678'
args=sys.argv[1:]
if args == ['session','name']:
 print(base)
elif args == ['_tui']:
 print('PM DASHBOARD',flush=True); time.sleep(120)
elif args == ['session']:
 (root/'pm-started.json').write_text(json.dumps({'cwd':os.getcwd(),'args':args}))
 tmux=['tmux','-S',os.environ['PM_TMUX_SOCKET']]
 subprocess.run(tmux+['new-session','-d','-s',base,'-n','main','-c',os.getcwd(),shlex.join([sys.executable,__file__,'_tui'])],check=True)
 subprocess.run(tmux+['new-session','-d','-s',base+'~1','-t',base],check=True)
 subprocess.run(tmux+['attach-session','-t',base+'~1'],check=True)
else:
 (root/'worker-resumed.json').write_text(json.dumps(args)); time.sleep(120)
''')
        pm.chmod(0o755)
        self.tmux.call('rename-session', '-t', 'work', base)
        self.tmux.call('rename-window', '-t', base + ':0', 'main')
        self.tmux.call('set-window-option', '-t', base + ':0', 'automatic-rename', 'off')
        self.tmux.call('respawn-pane', '-k', '-t', base + ':0', shlex.join([str(pm), '_tui']))
        self.tmux.call('split-window', '-h', '-b', '-d', '-t', base + ':0', '/bin/bash --noprofile --norc')
        self.tmux.call('new-window', '-d', '-t', base + ':4', '-n', 'agent')
        for n in (1, 2, 3):
            self.tmux.call('new-session', '-d', '-s', base + '~' + str(n), '-t', base)
        eventually(lambda: 'PM DASHBOARD' in self.tmux.call('capture-pane', '-p', '-t', base + ':0'))
        data = self.snapshot()
        dashboard = next(p for w in data['windows'].values() for p in w['panes'] if p['restore']['kind'] == 'pm')
        self.assertEqual(len(t.select_snapshot(data, [base + '~2'])['sessions']), 4)
        alias_only = dict(data, sessions=[s for s in data['sessions'] if s['session_name'] == base + '~2'])
        self.assertEqual(t.restore_sessions(alias_only)[0]['session_name'], base)
        self.assertEqual(dashboard['restore']['kind'], 'pm')
        agent = next(w for w in data['windows'].values() if w['window_name'] == 'agent')['panes'][0]
        agent['restore'] = {'kind': 'claude', 'cwd': str(self.root), 'argv': [str(pm), '--resume', 'saved-id']}
        t.write_json(self.root / 'snap/snapshot.json', data)
        self.tmux.call('kill-server')
        with mock.patch.dict(os.environ, {'PATH': str(bindir) + ':' + os.environ['PATH']}):
            t.restore_all([(self.root / 'snap', data)])
        self.assertEqual(json.loads(marker.read_text()), {'cwd': str(self.root), 'args': ['session']})
        eventually(lambda: worker.exists())
        self.assertEqual(json.loads(worker.read_text()), ['--resume', 'saved-id'])
        names = self.tmux.call('list-sessions', '-F', '#{session_name}').splitlines()
        self.assertEqual(sorted(names), [base, base + '~1'])
        self.assertIn('PM DASHBOARD', self.tmux.call('capture-pane', '-p', '-t', base + ':0'))
        self.assertEqual(len(self.tmux.call('list-panes', '-t', base + ':0', '-F', '#{pane_id}').splitlines()), 2)
        self.tmux.call('rename-session', '-t', '=' + base + '~1', base + '~9')
        self.tmux.call('kill-session', '-t', '=' + base)
        with self.assertRaisesRegex(RuntimeError, 'already exist'):
            t.restore(self.tmux, self.root / 'snap', data)
        with self.assertRaisesRegex(RuntimeError, 'already restored'):
            t.restore(t.Tmux(str(self.root / 'other.sock')), self.root / 'snap', data)

    def test_original_argv_is_safe_and_claude_resumes_exact_id(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        fake = bindir / 'claude'
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, time, sys
home = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
(home / 'sessions').mkdir(parents=True, exist_ok=True)
start = pathlib.Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
(home / 'sessions' / (str(os.getpid()) + '.json')).write_text(json.dumps({'pid': os.getpid(), 'procStart': start, 'sessionId': '12345678-abcd-abcd-abcd-123456789abc'}))
(home / 'args.json').write_text(json.dumps(sys.argv))
time.sleep(120)
''')
        fake.chmod(0o755)
        home = self.root / 'claude-home'
        arg = 'literal $(touch SHOULD_NOT_EXIST) `touch ALSO_NOT` "quote"'
        command = shlex.join(['env', 'CLAUDE_CONFIG_DIR=' + str(home), 'PATH=' + str(bindir) + ':' + os.environ['PATH'], str(fake), '--append-system-prompt', arg, '--effort', 'max'])
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', command)
        eventually(lambda: (home / 'args.json').exists())
        data = self.snapshot()
        r = next(iter(data['windows'].values()))['panes'][0]['restore']
        self.assertEqual(data['blockers'], [])
        self.assertEqual(r['session_id'], '12345678-abcd-abcd-abcd-123456789abc')
        self.assertIn(arg, r['argv'])
        # Select the test executable explicitly; no real agent is ever launched.
        r['argv'][0] = str(fake)
        t.write_json(self.root / 'snap/snapshot.json', data)
        self.tmux.call('rename-session', '-t', '=work', 'renamed-agent')
        other = t.Tmux(str(self.root / 'other.sock'))
        with self.assertRaisesRegex(RuntimeError, 'already running at renamed-agent'):
            t.restore(other, self.root / 'snap', data)
        self.assertEqual(other.call('list-sessions', allow_fail=True), '')
        self.tmux.call('kill-server')
        t.restore(self.tmux, self.root / 'snap', data)
        args = eventually(lambda: (x if '--resume' in x else None) if (x := t.read_json(home / 'args.json')) else None)
        self.assertEqual(args, r['argv'])
        self.assertEqual(self.snapshot('resumed-agent')['blockers'], [])
        self.assertFalse((self.root / 'SHOULD_NOT_EXIST').exists())
        self.assertFalse((self.root / 'ALSO_NOT').exists())


class MultiServerTests(unittest.TestCase):
    def setUp(self):
        IntegrationTests.setUp(self)
        self.env['TMUX_TMPDIR'] = str(self.root / 'named-root')
        Path(self.env['TMUX_TMPDIR']).mkdir()
        subprocess.run(['tmux', '-L', 'resume', '-f', '/dev/null', 'new-session', '-d', '-s', 'work',
            '-x', '120', '-y', '40', '-c', str(self.root), '/bin/bash --noprofile --norc'],
            env=self.env, check=True, capture_output=True)
        self.named = t.Tmux(str(Path(self.env['TMUX_TMPDIR']) / f'tmux-{os.getuid()}' / 'resume'))
        self.sources = [self.tmux, self.named]

    def tearDown(self):
        self.named.call('kill-server', allow_fail=True)
        IntegrationTests.tearDown(self)

    def cli(self, *args):
        output = io.StringIO()
        children = []
        original_popen = subprocess.Popen
        def spawn(*positional, **keywords):
            child = original_popen(*positional, **keywords)
            if '_close-all' in positional[0]:
                children.append(child)
            return child
        # Default discovery is restricted to our two test servers here. Real
        # discovery has a separate read-only test; never close a user's server.
        with mock.patch.dict(os.environ, self.env, clear=True), \
             mock.patch.object(t, 'discover_servers', return_value=self.sources), \
             mock.patch.object(subprocess, 'Popen', side_effect=spawn), \
             mock.patch('sys.argv', ['tmux-resume', *map(str, args)]), redirect_stdout(output):
            try:
                t.main()
            finally:
                for child in children:
                    child.wait(timeout=6)
                    self.assertEqual(child.returncode, 0)
        return output.getvalue()

    def save(self, name='bundle'):
        path = self.root / name
        self.cli('save', '--output', path)
        _, data = t.find_snapshot(str(path))
        return path, data, t.snapshot_parts(path, data)

    def test_discovery_finds_named_and_custom_listeners_without_pane_environment(self):
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', 'env -u TMUX -u TMUX_PANE /bin/bash --noprofile --norc')
        stale = str(Path(self.named.prefix[2]).parent / 'stale')
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(stale)
        with mock.patch.dict(os.environ, dict(self.env, TMUX=self.sock + ',999,0'), clear=True):
            actual = [tmux.prefix[2] for tmux in t.discover_servers()]
        self.assertIn(self.sock, actual)
        self.assertIn(self.named.prefix[2], actual)
        self.assertNotIn(stale, actual)
        self.assertEqual(len(actual), len(set(actual)))

    def test_default_roundtrip_keeps_named_sockets_and_separate_pane_artifacts(self):
        for tmux, seconds in zip(self.sources, (121, 122)):
            tmux.call('respawn-pane', '-k', '-t', 'work:0', f'sleep {seconds}')
            eventually(lambda: tmux.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}') == 'sleep')
        path, data, parts = self.save()
        self.assertEqual(data['version'], 2)
        self.assertEqual(len(parts), 2)
        self.assertEqual({entry['socket'] for entry in data['servers']}, {self.sock, self.named.prefix[2]})
        self.assertEqual([p['sessions'][0]['session_name'] for _, p in parts], ['work', 'work'])
        for tmux in self.sources:
            tmux.call('kill-server')
        # Simulate the loss of a tmux socket directory during a reboot.
        Path(self.named.prefix[2]).unlink(missing_ok=True)
        Path(self.named.prefix[2]).parent.rmdir()
        preview = self.cli('restore', path, '--dry-run')
        self.assertIn(self.sock, preview)
        self.assertIn(self.named.prefix[2], preview)
        self.assertFalse(Path(self.named.prefix[2]).parent.exists())
        self.cli('restore', path)
        for tmux, seconds in zip(self.sources, (121, 122)):
            eventually(lambda: f'sleep {seconds}' in tmux.call('capture-pane', '-p', '-J', '-t', 'work:0'))
            self.assertEqual(tmux.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}'), 'bash')
        named_result = subprocess.run(['tmux', '-L', 'resume', 'list-sessions', '-F', '#{session_name}'],
                                      env=self.env, check=True, capture_output=True, text=True)
        self.assertEqual(named_result.stdout.strip(), 'work')
        with self.assertRaisesRegex(RuntimeError, 'already exist'):
            self.cli('restore', path)

    def test_automatic_history_retention_and_no_server_shutdown_save(self):
        paths = []
        for _ in range(3):
            self.cli('save', '--keep', '2')
            paths.append((t.STATE / 'latest').resolve())
        self.assertFalse(paths[0].exists())
        self.assertTrue(paths[1].exists())
        self.assertTrue(paths[2].exists())
        history = self.cli('history')
        self.assertLess(history.index(paths[2].name), history.index(paths[1].name))
        for tmux in self.sources:
            tmux.call('kill-server')
        self.sources = []
        output = self.cli('save', '--if-running')
        self.assertIn('unchanged', output)
        self.assertEqual((t.STATE / 'latest').resolve(), paths[2])
        self.assertEqual(len(t.checkpoint_history()), 2)

    def test_server_and_session_selectors_and_ambiguity(self):
        path, data, parts = self.save()
        with self.assertRaisesRegex(RuntimeError, 'multiple servers'):
            self.cli('restore', path, '--session', 'work', '--dry-run')
        with self.assertRaisesRegex(RuntimeError, 'multiple servers'):
            self.cli('save', '--session', 'work', '--output', self.root / 'ambiguous')
        preview = self.cli('--server', 'resume', 'restore', path, '--session', 'work', '--dry-run')
        self.assertIn(self.named.prefix[2], preview)
        self.assertNotIn(self.sock, preview)
        shown = self.cli('--socket', self.sock, 'show', path, '--session', 'work')
        self.assertIn(self.sock, shown)
        self.assertNotIn(self.named.prefix[2], shown)
        with self.assertRaisesRegex(RuntimeError, 'Saved server not found'):
            self.cli('--server', 'missing', 'restore', path)
        with self.assertRaisesRegex(RuntimeError, 'single selected server'):
            self.cli('--socket', self.root / 'new.sock', 'restore', path)
        self.cli('--server', 'resume', 'save', '--session', 'work', '--output', self.root / 'named-only')
        single_path, single = t.find_snapshot(str(self.root / 'named-only'))
        self.assertEqual([t.snapshot_socket(part) for _, part in t.snapshot_parts(single_path, single)], [self.named.prefix[2]])
        self.named.call('kill-server')
        self.cli('--server', 'resume', 'restore', path, '--session', 'work')
        self.assertEqual(self.named.call('list-sessions', '-F', '#{session_name}'), 'work')
        self.assertEqual(self.tmux.call('list-sessions', '-F', '#{session_name}'), 'work')

    def test_preflight_conflict_on_later_server_does_not_create_first(self):
        path, _, _ = self.save()
        self.tmux.call('kill-server')
        with self.assertRaisesRegex(RuntimeError, 'already exist'):
            self.cli('restore', path)
        self.assertEqual(self.tmux.call('list-sessions', allow_fail=True), '')
        self.assertEqual(self.named.call('list-sessions', '-F', '#{session_name}'), 'work')

    def test_skip_existing_cli_filters_per_server_before_remote_preflight(self):
        path, _, parts = self.save()
        existing_path, existing_data = parts[0]
        pane = next(iter(existing_data['windows'].values()))['panes'][0]
        pane['restore'] = dict(kind='codex', cwd='/missing/ignored-directory',
                               argv=['codex', 'resume', 'test-thread', '--remote', 'unix:///missing.sock'])
        t.write_json(existing_path / 'snapshot.json', existing_data)
        self.named.call('kill-server')
        before = self.tmux.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}')
        with mock.patch.object(t, 'probe_remote', side_effect=AssertionError('skipped remote must not be probed')), \
             mock.patch.object(t, 'latest_codex_version', side_effect=AssertionError('skipped Codex must not be checked')):
            preview = self.cli('restore', path, '--skip-existing', '--dry-run')
            self.assertIn('Skipping work', preview)
            self.assertEqual(self.named.call('list-sessions', allow_fail=True), '')
            self.cli('restore', path, '--skip-existing')
            self.assertIn('No missing sessions', self.cli('restore', path, '--skip-existing'))
        self.assertEqual(self.named.call('list-sessions', '-F', '#{session_name}'), 'work')
        self.assertEqual(self.tmux.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}'), before)

    def test_skip_existing_still_blocks_all_missing_sessions_when_remote_is_offline(self):
        self.named.call('new-session', '-d', '-s', 'missing', '/bin/bash --noprofile --norc')
        path, _, parts = self.save()
        remote_path, remote_data = parts[0]
        pane = next(iter(remote_data['windows'].values()))['panes'][0]
        pane['restore'] = dict(kind='codex', cwd=str(self.root),
                               argv=['codex', 'resume', 'test-thread', '--remote', 'unix://' + str(self.root / 'offline.sock')])
        t.write_json(remote_path / 'snapshot.json', remote_data)
        self.tmux.call('kill-server')
        self.named.call('kill-session', '-t', '=missing')
        before = self.named.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}')
        with self.assertRaisesRegex(RuntimeError, 'nothing restored'):
            self.cli('restore', path, '--skip-existing')
        self.assertEqual(self.tmux.call('list-sessions', allow_fail=True), '')
        self.assertEqual(self.named.call('list-sessions', '-F', '#{session_name}'), 'work')
        self.assertEqual(self.named.call('list-panes', '-a', '-F', '#{pane_id}|#{pane_pid}'), before)

    def test_remote_preflight_blocks_all_servers_then_allows_retry(self):
        path, _, parts = self.save()
        remote_path, remote_data = parts[1]
        pane = next(iter(remote_data['windows'].values()))['panes'][0]
        endpoint = 'unix://' + str(self.root / 'unavailable.sock')
        pane['restore'] = dict(kind='codex', cwd=str(self.root),
                               argv=['/bin/true', 'resume', 'test-thread', '--remote', endpoint])
        t.write_json(remote_path / 'snapshot.json', remote_data)
        for tmux in self.sources:
            tmux.call('kill-server')
        with self.assertRaisesRegex(RuntimeError, 'codex remote-control start'):
            self.cli('restore', path)
        for tmux in self.sources:
            self.assertEqual(tmux.call('list-sessions', allow_fail=True), '')
        # Selecting only a local workspace must not probe an unselected remote.
        with mock.patch.object(t, 'probe_remote', side_effect=AssertionError('unselected remote')):
            t.restore_all(parts[:1])
        self.sources[0].call('kill-server')
        with remote_listener(self.root / 'unavailable.sock'):
            self.cli('restore', path, '--skip-codex-update')
        for tmux in self.sources:
            self.assertEqual(tmux.call('list-sessions', '-F', '#{session_name}'), 'work')

    def test_update_preflight_blocks_all_servers_and_bypass_suppresses_pane_prompts(self):
        path, _, parts = self.save()
        home = self.root / 'codex-home'
        home.mkdir()
        config = home / 'config.toml'
        config.write_text('check_for_update_on_startup = true\n')
        t.write_json(home / 'version.json', dict(latest_version='1.2.1',
                     last_checked_at=t.dt.datetime.now(t.dt.timezone.utc).isoformat()))
        client = self.root / 'codex'
        marker = self.root / 'codex-started.json'
        def install(version):
            client.write_text('#!/usr/bin/env python3\nimport json,sys,time\nfrom pathlib import Path\n'
                              f'if sys.argv[1:]==["--version"]: print("codex-cli {version}"); sys.exit(0)\n'
                              f'Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]))\ntime.sleep(120)\n')
            client.chmod(0o755)
        install('1.2.0')
        part_path, data = parts[1]
        pane = next(iter(data['windows'].values()))['panes'][0]
        pane['restore'] = dict(kind='codex', cwd=str(self.root), env={'CODEX_HOME': str(home)},
                               argv=[str(client), 'resume', 'saved-conversation', '-c', 'check_for_update_on_startup=true'])
        t.write_json(part_path / 'snapshot.json', data)
        snapshot_bytes = (part_path / 'snapshot.json').read_bytes()
        for tmux in self.sources:
            tmux.call('kill-server')
        for flags in [[], ['--dry-run']]:
            with self.assertRaisesRegex(RuntimeError, '1.2.0 -> 1.2.1'):
                self.cli('restore', path, *flags)
            for tmux in self.sources:
                self.assertEqual(tmux.call('list-sessions', allow_fail=True), '')
            self.assertFalse(marker.exists())
        self.cli('restore', path, '--skip-codex-update')
        eventually(marker.exists)
        actual = json.loads(marker.read_text())
        self.assertEqual(actual, pane['restore']['argv'][1:] + ['-c', 'check_for_update_on_startup=false'])
        self.assertEqual((part_path / 'snapshot.json').read_bytes(), snapshot_bytes)
        self.assertEqual(config.read_text(), 'check_for_update_on_startup = true\n')
        # Simulating installation clears the gate without requiring the bypass.
        for tmux in self.sources:
            tmux.call('kill-server')
        marker.unlink()
        install('1.2.1')
        self.cli('restore', path)
        eventually(marker.exists)
        self.assertEqual(json.loads(marker.read_text()), actual)
        self.assertEqual(config.read_text(), 'check_for_update_on_startup = true\n')

    def test_later_build_failure_rolls_back_other_servers_before_launch(self):
        path, _, parts = self.save()
        for tmux in self.sources:
            tmux.call('kill-server')
        original_call = t.Tmux.call
        def fail_second(tmux, *args, **kwargs):
            if tmux.prefix[2] == self.named.prefix[2] and args[0] == 'new-window':
                raise RuntimeError('test failure while building second server')
            return original_call(tmux, *args, **kwargs)
        with mock.patch.object(t.Tmux, 'call', fail_second):
            with self.assertRaisesRegex(RuntimeError, 'building second server'):
                t.restore_all(parts)
        for tmux in self.sources:
            self.assertEqual(tmux.call('list-sessions', allow_fail=True), '')
        self.assertEqual(t.active_restores(), [])

    def test_close_checks_every_server_and_detached_cleanup_closes_all(self):
        self.named.call('respawn-pane', '-k', '-t', 'work:0', 'vim -Nu NONE -n -i NONE')
        eventually(lambda: self.named.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}') == 'vim')
        with self.assertRaisesRegex(RuntimeError, 'nothing closed'):
            self.cli('save', '--close', '--output', self.root / 'blocked')
        for tmux in self.sources:
            self.assertEqual(tmux.call('list-sessions', '-F', '#{session_name}'), 'work')
        self.named.call('respawn-pane', '-k', '-t', 'work:0', '/bin/bash --noprofile --norc')
        self.cli('save', '--close', '--output', self.root / 'closing')
        for tmux in self.sources:
            eventually(lambda: not tmux.call('list-sessions', allow_fail=True))
        self.assertEqual((self.root / 'closing/close-check/close.log').read_text(), '')

    def test_close_revalidates_all_server_identities_before_any_kill(self):
        path, data, parts = self.save()
        self.named.call('rename-session', '-t', '=work', 'changed')
        with self.assertRaisesRegex(RuntimeError, 'sessions changed'):
            t.close_all(str(path))
        self.assertEqual(self.tmux.call('list-sessions', '-F', '#{session_name}'), 'work')

    def test_force_close_checkpoints_dirty_editor_and_jobs_then_restores(self):
        note = self.root / 'force-close note.txt'
        note.write_text('saved file contents\n')
        argv = ['vim', '-Nu', 'NONE', '-n', '-i', 'NONE', '--cmd',
                'source ' + str(ROOT / 'tmux_resume.vim'), str(note)]
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', shlex.join(argv))
        eventually(lambda: list((t.STATE / 'vim').glob('*.json')))
        self.tmux.call('send-keys', '-t', 'work:0', 'G', 'o', 'unsaved force-close text', 'Escape')
        self.named.call('respawn-pane', '-k', '-t', 'work:0', 'sleep 123')
        eventually(lambda: self.named.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}') == 'sleep')
        with self.assertRaisesRegex(RuntimeError, 'nothing closed'):
            self.cli('save', '--close', '--output', self.root / 'refused')
        output = self.cli('save', '--force-close', '--output', self.root / 'forced')
        self.assertIn('WARNING: Force-closing', output)
        self.assertIn('UNSAVED', output)
        for tmux in self.sources:
            self.assertFalse(tmux.call('list-sessions', allow_fail=True))
        path, data = t.find_snapshot(str(self.root / 'forced/close-check'))
        self.assertTrue(data['force_close'])
        self.assertTrue(any('UNSAVED' in b for b in data['forced_blockers']))
        self.assertTrue(any('running sleep' in b for b in data['forced_blockers']))
        self.assertEqual((t.STATE / 'latest').resolve(), path)
        self.assertEqual(note.read_text(), 'saved file contents\n')
        self.cli('restore', path)
        eventually(lambda: 'saved file contents' in self.tmux.call('capture-pane', '-p', '-t', 'work:0'))
        self.assertNotIn('unsaved force-close text', self.tmux.call('capture-pane', '-p', '-t', 'work:0'))
        eventually(lambda: 'Command prefilled' in self.named.call('capture-pane', '-p', '-t', 'work:0'))
        self.assertEqual(self.named.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}'), 'bash')
        with self.assertRaisesRegex(RuntimeError, 'already exist'):
            self.cli('restore', path)

    def test_force_close_keeps_unselected_sessions_and_servers(self):
        self.tmux.call('new-session', '-d', '-s', 'keep', '/bin/bash --noprofile --norc')
        self.tmux.call('respawn-pane', '-k', '-t', 'work:0', 'sleep 123')
        eventually(lambda: self.tmux.call('display-message', '-p', '-t', 'work:0', '#{pane_current_command}') == 'sleep')
        self.cli('--socket', self.sock, 'save', '--session', 'work', '--close', '--force-close',
                 '--output', self.root / 'forced-scoped')
        self.assertEqual(self.tmux.call('list-sessions', '-F', '#{session_name}'), 'keep')
        self.assertEqual(self.named.call('list-sessions', '-F', '#{session_name}'), 'work')

    def test_force_close_refuses_failed_final_checkpoint(self):
        original_snapshot = t.snapshot_all
        def fail_verification(sources, path, selected):
            if path.name == 'close-check':
                raise RuntimeError('test checkpoint write failed')
            return original_snapshot(sources, path, selected)
        with mock.patch.object(t, 'snapshot_all', side_effect=fail_verification):
            with self.assertRaisesRegex(RuntimeError, 'checkpoint write failed'):
                self.cli('save', '--force-close', '--output', self.root / 'failed-force')
        for tmux in self.sources:
            self.assertEqual(tmux.call('list-sessions', '-F', '#{session_name}'), 'work')

    def test_server_scope_change_during_close_prevents_all_closing(self):
        original_snapshot = t.snapshot_all
        def add_server_after_checkpoint(*args, **kwargs):
            result = original_snapshot(*args, **kwargs)
            self.sources[:] = [self.tmux, self.named]
            return result
        for flag in ('--close', '--force-close'):
            self.sources = [self.tmux]
            with self.subTest(flag=flag), mock.patch.object(t, 'snapshot_all', side_effect=add_server_after_checkpoint):
                with self.assertRaisesRegex(RuntimeError, 'Workspace changed'):
                    self.cli('save', flag, '--output', self.root / ('changed' + flag))
        for tmux in self.sources:
            self.assertEqual(tmux.call('list-sessions', '-F', '#{session_name}'), 'work')

    def test_legacy_snapshot_restores_original_socket_and_explicit_override(self):
        path = self.root / 'legacy'
        data = t.snapshot(self.named, path)
        self.named.call('kill-server')
        # A version-1 snapshot already records its source socket.
        self.cli('restore', path)
        self.assertEqual(self.named.call('list-sessions', '-F', '#{session_name}'), 'work')
        self.named.call('kill-server')
        other = t.Tmux(str(self.root / 'override.sock'))
        try:
            self.cli('--socket', other.prefix[2], 'restore', path)
            self.assertEqual(other.call('list-sessions', '-F', '#{session_name}'), 'work')
        finally:
            other.call('kill-server', allow_fail=True)


if __name__ == '__main__':
    unittest.main()
