"""Synthetic, temporary homes only. Never load a user's config or credentials."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import tomllib
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


repair = load('repair_under_test', ROOT / 'codex_session_repair.py')


@contextlib.contextmanager
def database(path):
    with contextlib.closing(sqlite3.connect(path)) as db:
        with db:
            yield db


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        # Import only after redirecting every possible credential location.
        self.env = mock.patch.dict(os.environ, {
            'CODEX_HOME': str(self.home),
            'CODEX_SWITCH_OPENCODE_AUTH': str(self.home / 'synthetic-auth.json'),
            'CODEX_SWITCH_NODE': '/synthetic/node',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.switch = load('switch_under_test', ROOT / 'codex-switch')
        with database(self.home / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, '
                       'model_provider TEXT, history_mode TEXT)')
        with database(self.home / 'thread_history_1.sqlite') as db:
            db.executescript('''
                CREATE TABLE thread_turns (thread_id TEXT, rollout_ordinal INT,
                    rollout_byte_offset INT, rollout_end_ordinal INT, rollout_end_byte_offset INT);
                CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INT, updated_at_ordinal INT);
                CREATE TABLE thread_history_projection_state (thread_id TEXT,
                    next_rollout_byte_offset INT, next_rollout_ordinal INT);
                CREATE TABLE thread_realtime_items (thread_id TEXT);
            ''')

    def session(self, number=1, missing=False, legacy=False):
        sid = str(uuid.UUID(int=number))
        path = self.home / ('legacy' if legacy else 'sessions') / f'synthetic-{number}.jsonl'
        data = (json.dumps({'type': 'session_meta', 'payload': {
            'id': sid, 'model_provider': 'openai'}}) + '\n').encode()
        if not missing:
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        with database(self.home / 'state_5.sqlite') as db:
            db.execute('INSERT INTO threads VALUES (?,?,?,?)',
                       (sid, str(path), 'openai', 'legacy' if legacy else 'paginated'))
        with database(self.home / 'thread_history_1.sqlite') as db:
            db.execute('INSERT INTO thread_history_projection_state VALUES (?,?,?)',
                       (sid, len(data), 1))
        return sid, path

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob('*') if p.is_file()}

    def logical(self):
        result = {}
        for name in ('state_5.sqlite', 'thread_history_1.sqlite'):
            with database(self.home / name) as db:
                result[name] = list(db.iterdump())
        return result

    def test_unknown_config_preserved(self):
        original = 'unknown = "retain"\n[features]\ncustom = true\n[other]\nvalues = [1, 2]\n'
        output = self.switch.build_config('krillcode', 'synthetic-model', '', original)
        parsed = tomllib.loads(output)
        self.assertEqual(parsed['unknown'], 'retain')
        self.assertEqual(parsed['other'], {'values': [1, 2]})
        self.assertTrue(parsed['features']['custom'])
        self.assertIn('[other]\nvalues = [1, 2]', output)

    def test_missing_rollout_skipped_without_writes(self):
        sid, _ = self.session(missing=True)
        before = self.snapshot()
        result = repair.repair_all(self.home, 'krillcode', dry_run=True)
        self.assertEqual(result['skipped_missing_rollouts'][0]['session_id'], sid)
        self.assertEqual(result['sessions'], [])
        self.assertEqual(before, self.snapshot())

    def test_legacy_path_plan(self):
        sid, path = self.session(legacy=True)
        plan = repair._plan(self.home, sid, 'krillcode')
        self.assertEqual(plan['path'], path)
        self.assertTrue(plan['changed'])

    def test_writer_blocks(self):
        sid, path = self.session()
        before = self.snapshot()
        inspect = repair._inspect_process_retry

        def own_process_only(proc, target, identity):
            # Inspect the real held FD, not unrelated host processes/permissions.
            return inspect(proc, target, identity) if proc.name == str(os.getpid()) else []

        with path.open('ab'), mock.patch.object(
                repair, '_inspect_process_retry', side_effect=own_process_only):
            with self.assertRaises(repair.ActiveWriterError):
                repair.repair_session(self.home, sid, 'krillcode')
        self.assertEqual(path.read_bytes(), before[str(path.relative_to(self.home))])
        self.assertEqual(repair._plan(self.home, sid, 'krillcode')['thread']['model_provider'], 'openai')

    def test_batch_rollback_mock_native(self):
        sessions = [self.session(n) for n in (1, 2)]
        original_db = self.logical()
        original_files = {path: path.read_bytes() for _, path in sessions}

        def native(home, backup, sid, data):
            rows = {table: [] for table in repair.HISTORY_TABLES}
            rows['thread_history_projection_state'] = [(sid, len(data), 1)]
            return rows, {'synthetic': True}, data

        def fail_commit():
            # Ensure both target commits happened before injecting failure.
            for sid, _ in sessions:
                self.assertEqual(repair._plan(self.home, sid, 'krillcode')['thread']['model_provider'],
                                 'krillcode')
            raise RuntimeError('synthetic config commit failure')

        with mock.patch.object(repair, 'check_writers'), mock.patch.object(
                repair, '_native_rebuild', side_effect=native) as replay:
            with self.assertRaisesRegex(repair.RepairError, 'restored_all_batch_targets'):
                repair.repair_all(self.home, 'krillcode', commit_callback=fail_commit)
            self.assertEqual(replay.call_count, 2)
        self.assertEqual(self.logical(), original_db)
        self.assertEqual({p: p.read_bytes() for p in original_files}, original_files)

    def test_dry_run_no_writes_or_native(self):
        self.session()
        before = self.snapshot()
        with mock.patch.object(repair, 'check_writers'), mock.patch.object(
                repair, '_native_rebuild') as native:
            result = repair.repair_all(self.home, 'krillcode', dry_run=True)
        native.assert_not_called()
        self.assertEqual(result['sessions'][0]['status'], 'would_repair')
        self.assertEqual(self.snapshot(), before)

    def test_portable_auth_and_no_key_output(self):
        marker = 'SYNTHETIC-TEST-MARKER-NOT-A-CREDENTIAL'
        self.switch.OPENCODE_AUTH.write_text(json.dumps({'krillcode': {'key': marker}}))
        self.switch.CONFIG.write_text('model_provider = "openai"\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.switch.cmd_status(None)
        config = self.switch.build_config('official', 'synthetic-model', '', '')
        auth = tomllib.loads(config)['model_providers']['krillcode']['auth']
        self.assertEqual(auth['command'], '/synthetic/node')
        self.assertEqual(auth['args'], self.switch.AUTH_ARGS)
        self.assertEqual(auth['args'][-1], str(self.switch.OPENCODE_AUTH))
        self.assertNotIn(marker, output.getvalue() + config)
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.home / 'xdg')}):
            with mock.patch.dict(os.environ):
                os.environ.pop('CODEX_SWITCH_OPENCODE_AUTH')
                module = load('switch_xdg', ROOT / 'codex-switch')
        self.assertEqual(module.OPENCODE_AUTH, self.home / 'xdg/opencode/auth.json')

    def test_binary_override_and_relative_path_exclusion(self):
        binary = self.home / 'synthetic-codex'
        binary.write_text('#!/bin/sh\nexit 1\n')
        binary.chmod(0o700)
        with mock.patch.dict(os.environ, {'CODEX_SWITCH_CODEX_BINARY': str(binary)}):
            self.assertEqual(repair._native_binary(self.home), binary)
        with mock.patch.dict(os.environ, {'PATH': '.:relative-directory'}, clear=True):
            with self.assertRaises(repair.RepairError):
                repair._native_binary(self.home)


if __name__ == '__main__':
    unittest.main()
