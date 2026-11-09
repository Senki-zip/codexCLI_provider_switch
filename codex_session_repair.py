#!/usr/bin/env python3
"""Safely repair managed Codex sessions, without inference or config changes.

CLI: --home HOME --target-provider {openai,krillcode}
     (--all | --session-id ID) [--dry-run]
Python: repair_session(home, session_id, target_provider, dry_run=False)
        repair_all(home, target_provider, dry_run=False)

Only openai/krillcode threads are eligible. Live writers fail closed. Native
app-server replay and bidirectional pagination run in a credential-free,
isolated CODEX_HOME, before committing any production changes. The preserved
rollout `ordinal` field is authoritative, not the physical JSONL line number.
Failures raise RepairError (CLI exit 1); no errors are silently ignored.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

MANAGED = frozenset({'openai', 'krillcode'})
HISTORY_TABLES = ('thread_turns', 'thread_items',
                  'thread_history_projection_state', 'thread_realtime_items')
DROP = object()


class RepairError(RuntimeError):
    """A precondition, replay, verification, or commit failed."""


class MissingRolloutError(RepairError):
    """DB-only history is not a substitute for a verifiable source rollout."""

    def __init__(self, session_id, path, history_mode):
        self.details = {'session_id': session_id, 'rollout_path': str(path),
                        'history_mode': history_mode,
                        'reason': 'dangling_symlink' if path.is_symlink() else 'missing_rollout'}
        super().__init__(f'Missing rollout for session {session_id}: {path}; '
                         'existing DB-only history is preserved, not migrated; '
                         'restore the source rollout before explicit repair')


class ActiveWriterError(RepairError):
    def __init__(self, holders):
        self.holders = holders
        super().__init__('Active rollout writer(s): ' + json.dumps(holders))


def _require(condition, message):
    if not condition:
        raise RepairError(message)


@contextlib.contextmanager
def _ro(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        yield db
    finally:
        db.close()


@contextlib.contextmanager
def _writable(path, timeout=10):
    db = sqlite3.connect(path, timeout=timeout)
    try:
        with db:
            yield db
    finally:
        db.close()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _rows_hash(rows):
    return collections.Counter(_hash(repr(tuple(r)).encode()) for r in rows)


def _inspect_process(proc, path, identity):
    if proc.stat().st_uid != os.getuid():
        return []
    comm = (proc / 'comm').read_text().strip()
    found = []
    for fd in (proc / 'fd').iterdir():
        try:
            target = os.readlink(fd)
            if target.removesuffix(' (deleted)') != str(path):
                stat = fd.stat()
                if (stat.st_dev, stat.st_ino) != identity:
                    continue
            info = (proc / 'fdinfo' / fd.name).read_text()
            flags = int(next(s.split(':', 1)[1] for s in info.splitlines()
                             if s.startswith('flags:')), 8)
            if flags & os.O_ACCMODE:
                found.append({'pid': int(proc.name), 'fd': fd.name, 'process': comm})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return found


def _inspect_process_retry(proc, path, identity):
    for attempt in range(3):
        try:
            return _inspect_process(proc, path, identity)
        except (FileNotFoundError, ProcessLookupError):
            return []
        except PermissionError:
            if not proc.exists():
                return []
            if attempt < 2:
                time.sleep(0.02)
                continue
            # Preserve the existing infrastructure exceptions, never add a
            # process-name exception for an inaccessible temporary process.
            comm = ''
            try:
                comm = (proc / 'comm').read_text().strip()
            except (FileNotFoundError, ProcessLookupError):
                return []
            except PermissionError:
                pass
            if not proc.exists():
                return []
            _require(comm in {'systemd', '(sd-pam)', 'sshd', 'sftp-server'},
                     f'Cannot inspect PID {proc.name} ({comm}) after bounded retry')
            return []


def check_writers(path):
    """Inspect local user FDs; return no value or raise ActiveWriterError.

    Linux non-dumpable infrastructure processes cannot expose their FDs; only
    the known systemd/sshd helpers are exempted. An inaccessible Codex or other
    same-user process fails closed. No command lines/environments are read.
    """
    path = Path(path).resolve()
    identity = (path.stat().st_dev, path.stat().st_ino)
    holders = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        holders.extend(_inspect_process_retry(proc, path, identity))
    if holders:
        raise ActiveWriterError(holders)


def _encrypted_reasoning(value):
    return (isinstance(value, dict) and value.get('type') == 'reasoning'
            and 'encrypted_content' in value)


def _clean(value, counts):
    if _encrypted_reasoning(value):
        counts['encrypted_reasoning'] += 1
        # Never discard a nonempty summary or visible reasoning content.
        if not value.get('summary') and not value.get('content'):
            counts['removed_reasoning_objects'] += 1
            return DROP
        counts['preserved_reasoning_summaries'] += 1
        value = {k: v for k, v in value.items() if k != 'encrypted_content'}
    if isinstance(value, dict):
        return {k: new for k, v in value.items()
                if (new := _clean(v, counts)) is not DROP}
    if isinstance(value, list):
        return [new for v in value if (new := _clean(v, counts)) is not DROP]
    return value


def _provider_dicts(record):
    payload = record.get('payload', {})
    if record.get('type') == 'session_meta':
        yield payload, 'model_provider'
    if (record.get('type') == 'event_msg'
            and payload.get('type') == 'thread_settings_applied'):
        settings = payload.get('thread_settings')
        if isinstance(settings, dict):
            yield settings, 'model_provider_id'


def _normalized(record):
    record = json.loads(_canonical(record))
    if record.get('type') == 'session_meta' and record.get('payload', {}).get('model_provider') in (None, ''):
        record['payload']['model_provider'] = '<managed-provider>'
    for obj, key in _provider_dicts(record):
        if obj.get(key) in MANAGED:
            obj[key] = '<managed-provider>'
    return record


def _protected_content(records):
    """Independent multiset of all nonempty leaves except forbidden ciphertext.

    Empty encrypted reasoning objects are excluded; their parents' meaningful
    content (messages, tools, summaries, IDs and all other fields) is retained.
    Provider transitions at the two explicitly permitted paths are normalized.
    List indices are intentionally not included, so removals cannot shift keys.
    """
    result = collections.Counter()

    def walk(value, path):
        if _encrypted_reasoning(value):
            if not value.get('summary') and not value.get('content'):
                return
            value = {k: v for k, v in value.items() if k != 'encrypted_content'}
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + (key,))
        elif isinstance(value, list):
            for child in value:
                walk(child, path + ('[]',))
        else:
            result[(path, _hash(_canonical(value).encode()))] += 1

    for record in records:
        if (record.get('type') == 'response_item'
                and _encrypted_reasoning(record.get('payload'))
                and not record['payload'].get('summary')
                and not record['payload'].get('content')):
            continue
        walk(_normalized(record), ())
    return result


def transform_rollout(data, target_provider):
    """Pure transform; untouched records keep their original bytes."""
    _require(target_provider in MANAGED, 'Unsupported target provider')
    original = []
    output = []
    counts = collections.Counter()
    for line in data.splitlines(keepends=True):
        record = json.loads(line)
        _require(isinstance(record, dict), 'Rollout record is not an object')
        original.append(record)
        if (record.get('type') == 'response_item'
                and _encrypted_reasoning(record.get('payload'))):
            payload = _clean(record['payload'], counts)
            if payload is DROP:
                counts['removed_direct_records'] += 1
                continue
            new = dict(record, payload=payload)
        else:
            new = _clean(record, counts)
        _require(new is not DROP, 'Unexpected root reasoning object')
        if new.get('type') == 'session_meta' and new['payload'].get('model_provider') in (None, ''):
            new['payload']['model_provider'] = target_provider
            counts['provider_fields_changed'] += 1
        for obj, key in _provider_dicts(new):
            if obj.get(key) in MANAGED and obj[key] != target_provider:
                obj[key] = target_provider
                counts['provider_fields_changed'] += 1
        if new == record:
            output.append(line)
        else:
            output.append((_canonical(new) + '\n').encode())
    result = b''.join(output)
    parsed = [json.loads(line) for line in result.splitlines()]
    before = _protected_content(original)
    _require(before == _protected_content(parsed), 'Protected content multiset changed')
    counts['protected_content_leaves'] = sum(before.values())
    return result, dict(counts)


def _positions(data):
    starts, ends = {}, {}
    offset, previous = 0, -1
    for line in data.splitlines(keepends=True):
        record = json.loads(line)
        ordinal = record.get('ordinal')
        # Old rollout formats use physical ordinals when no field is stored.
        if ordinal is None:
            ordinal = previous + 1
        _require(isinstance(ordinal, int) and ordinal > previous,
                 'Rollout ordinals are not strictly increasing')
        starts[ordinal] = offset
        offset += len(line)
        ends[ordinal] = offset
        previous = ordinal
    return starts, ends, previous + 1


def _history_rows(db, session_id):
    return {table: db.execute(f'SELECT * FROM {table} WHERE thread_id=?',
                             (session_id,)).fetchall()
            for table in HISTORY_TABLES}


def _validate_projection(db, session_id, data):
    starts, ends, next_ordinal = _positions(data)
    state = db.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal '
                       'FROM thread_history_projection_state WHERE thread_id=?',
                       (session_id,)).fetchone()
    _require(state == (len(data), next_ordinal), 'Projection EOF/ordinal mismatch')
    turns = db.execute('SELECT rollout_ordinal,rollout_byte_offset,'
                       'rollout_end_ordinal,rollout_end_byte_offset '
                       'FROM thread_turns WHERE thread_id=?', (session_id,)).fetchall()
    for ordinal, offset, end_ordinal, end_offset in turns:
        _require(starts.get(ordinal) == offset and offset is not None,
                 'Turn start offset mismatch')
        if end_ordinal is not None and end_offset is not None:
            _require(ends.get(end_ordinal) == end_offset, 'Turn end offset mismatch')
    for ordinal, updated in db.execute('SELECT rollout_ordinal,updated_at_ordinal '
                                       'FROM thread_items WHERE thread_id=?', (session_id,)):
        _require(ordinal in starts and updated in starts, 'Item ordinal missing from rollout')
    has_turns = any(json.loads(line).get('payload', {}).get('type') == 'task_started'
                    for line in data.splitlines())
    _require(bool(turns) or not has_turns, 'Projection has no turns')
    return {'next_rollout_byte_offset': state[0], 'next_rollout_ordinal': state[1],
            'turn_count': len(turns)}


def _outside_snapshot(db, history):
    tables = HISTORY_TABLES if history else [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    result = {}
    for table in tables:
        columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
        key = 'thread_id' if history else ('id' if table == 'threads' else
                                          ('thread_id' if 'thread_id' in columns else None))
        result[table] = (key, _rows_hash(db.execute(f'SELECT * FROM "{table}"')))
    return result


def _outside_thread(db, session_id, history):
    result = {}
    for table, (key, _) in _outside_snapshot(db, history).items():
        sql = f'SELECT * FROM "{table}"' + (f' WHERE "{key}" != ?' if key else '')
        result[table] = _rows_hash(db.execute(sql, (session_id,) if key else ()))
    return result


def _native_binary(home):
    override = os.environ.get('CODEX_SWITCH_CODEX_BINARY') or os.environ.get('CODEX_REPAIR_BINARY')
    if override:
        candidates = [Path(override)]
    else:
        candidates = sorted((home / 'packages/app-server-daemon/releases').glob('*/bin/codex'),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        # Do not execute a codex planted in the current directory via PATH.
        safe_path = os.pathsep.join(p for p in os.get_exec_path() if Path(p).is_absolute())
        found = shutil.which('codex', path=safe_path)
        if not candidates and found:
            candidates = [Path(found)]
    _require(bool(candidates) and candidates[0].is_file() and
             os.access(candidates[0], os.X_OK),
             'Native Codex binary unavailable; set CODEX_SWITCH_CODEX_BINARY')
    return candidates[0].resolve()


class _AppServer:
    def __init__(self, binary, home, stderr_path, timeout=120, provider='openai'):
        # An isolated replay must never inherit API credentials or real config.
        env = {k: v for k, v in os.environ.items() if not any(
            word in k.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY',
                                           'AUTH', 'CODEX', 'OPENAI', 'KRILL'))}
        env['CODEX_HOME'] = str(home)
        self.stderr = stderr_path.open('wb')
        self.error_path = stderr_path.parent / 'native-rpc-error.json'
        command = [str(binary), 'app-server', '--stdio', '-c',
                   'model_reasoning_effort="low"', '-c', f'model_provider="{provider}"']
        if provider == 'krillcode':
            # Register the name without copying real provider config or credentials.
            # Replay/list RPCs never request inference; loopback port 9 fails closed.
            for setting in ('name="Offline replay"', 'base_url="http://127.0.0.1:9/v1"',
                            'wire_api="responses"', 'requires_openai_auth=false'):
                command.extend(['-c', 'model_providers.krillcode.' + setting])
        bwrap = shutil.which('bwrap')
        _require(bwrap is not None, 'Linux bubblewrap (bwrap) is required for offline native replay')
        command = [bwrap, '--die-with-parent', '--unshare-net', '--ro-bind', '/', '/',
                   '--bind', str(home), str(home), '--dev', '/dev', '--proc', '/proc',
                   '--'] + command
        self.proc = subprocess.Popen(command, env=env,
                                     cwd=home, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self.stderr)
        self.messages = queue.Queue()
        self.deadline = time.monotonic() + timeout
        self.seq = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                self.messages.put({'parse_error': True})
        self.messages.put({'eof': True})

    def rpc(self, method, params):
        _require(method in {'initialize', 'thread/resume', 'thread/read',
                            'thread/turns/list', 'thread/items/list'}, 'Unsafe RPC')
        self.seq += 1
        self.proc.stdin.write((json.dumps({'id': self.seq, 'method': method,
                                          'params': params}) + '\n').encode())
        self.proc.stdin.flush()
        while True:
            remaining = self.deadline - time.monotonic()
            _require(remaining > 0, 'Native replay exceeded 120 seconds')
            try:
                message = self.messages.get(timeout=min(30, remaining))
            except queue.Empty as exc:
                raise RepairError('Native replay response timeout') from exc
            _require(not message.get('eof'), 'Native app-server exited unexpectedly')
            _require(not message.get('parse_error'), 'Invalid native app-server JSON')
            if message.get('id') == self.seq:
                if 'error' in message:
                    # Server error messages may contain user content; do not print them.
                    self.error_path.write_text(json.dumps(message['error'], indent=2) + '\n')
                    raise RepairError(f'Native {method} failed, code '
                                      f'{message["error"].get("code")}')
                return message['result']

    def pages(self, method, session_id, direction, **extra):
        cursor, data, seen, pages = None, [], set(), 0
        while True:
            result = self.rpc(method, {'threadId': session_id, 'limit': 37,
                                      'sortDirection': direction, 'cursor': cursor, **extra})
            data.extend(result['data'])
            pages += 1
            cursor = result.get('nextCursor')
            if cursor is None:
                return data, pages
            _require(cursor not in seen, 'Native pagination cursor loop')
            seen.add(cursor)

    def close(self):
        with contextlib.suppress(BrokenPipeError):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.stderr.close()


def _validate_native_append(source, replayed, session_id, provider):
    """Accept only native settings suffixes; never reinterpret a changed prefix."""
    _require(replayed.startswith(source), 'Native replay modified existing source prefix')
    suffix = replayed[len(source):]
    if not suffix:
        return 0
    _require(not source or source.endswith(b'\n'), 'Native append is not a JSONL boundary')
    _require(suffix.endswith(b'\n'), 'Incomplete native settings append')
    settings_keys = {
        'model', 'model_provider_id', 'approval_policy', 'approvals_reviewer',
        'permission_profile', 'active_permission_profile', 'cwd', 'runtime_workspace_roots',
        'collaboration_mode', 'disabled_plugin_ids', 'reasoning_effort', 'effort',
        'service_tier', 'personality', 'sandbox_policy', 'multi_agent_mode',
    }
    forbidden_keys = {'encrypted_content', 'turn_id', 'root_turn_id', 'item', 'items',
                      'content', 'input', 'output', 'arguments', 'call_id',
                      'replacement_history', 'messages'}
    forbidden_types = {'message', 'reasoning', 'function_call', 'function_call_output',
                       'custom_tool_call', 'custom_tool_call_output', 'local_shell_call',
                       'web_search_call', 'task_started', 'task_complete', 'turn_started',
                       'turn_complete', 'item_completed'}

    def validate_metadata(value):
        if isinstance(value, dict):
            _require(not forbidden_keys.intersection(value), 'Non-settings data in native append')
            _require(value.get('type') not in forbidden_types, 'Forbidden native appended object type')
            if 'thread_id' in value:
                _require(value['thread_id'] == session_id, 'Native append has another thread ID')
            if 'model_provider_id' in value:
                _require(value['model_provider_id'] == provider and provider in MANAGED,
                         'Native append has wrong provider')
            if 'developer_instructions' in value:
                _require(value['developer_instructions'] is None,
                         'Native append introduces developer instructions')
            for child in value.values():
                validate_metadata(child)
        elif isinstance(value, list):
            for child in value:
                validate_metadata(child)

    previous = _positions(source)[2] - 1
    count = 0
    for line in suffix.splitlines():
        record = json.loads(line)
        _require(isinstance(record, dict) and set(record) == {'timestamp', 'ordinal', 'type', 'payload'},
                 'Unexpected native appended record fields')
        _require(record['type'] == 'event_msg' and isinstance(record['timestamp'], str),
                 'Native append is not a settings event')
        _require(type(record['ordinal']) is int and record['ordinal'] > previous,
                 'Native appended ordinal is not increasing')
        previous = record['ordinal']
        payload = record['payload']
        _require(isinstance(payload, dict) and
                 set(payload) == {'type', 'thread_id', 'thread_settings'} and
                 payload['type'] == 'thread_settings_applied' and payload['thread_id'] == session_id,
                 'Native append is not this thread settings event')
        settings = payload['thread_settings']
        _require(isinstance(settings, dict) and set(settings) <= settings_keys and
                 settings.get('model_provider_id') == provider and provider in MANAGED,
                 'Unexpected settings keys or native provider')
        validate_metadata(settings)
        count += 1
    return count


def _native_rebuild(home, backup, session_id, data):
    shadow = backup / 'isolated-rebuild-home'
    shadow.mkdir(mode=0o700)
    with _ro(backup / 'state_5.sqlite') as db:
        source_name = Path(db.execute('SELECT rollout_path FROM threads WHERE id=?',
                                      (session_id,)).fetchone()[0]).name
    rollout = shadow / 'sessions' / source_name
    rollout.parent.mkdir()
    rollout.write_bytes(data)
    with _ro(backup / 'state_5.sqlite') as src, _writable(shadow / 'state_5.sqlite') as dst:
        src.backup(dst)
    provider = next(json.loads(line)['payload'].get('model_provider', 'openai')
                    for line in data.splitlines()
                    if json.loads(line).get('type') == 'session_meta')
    _require(provider in MANAGED, 'Unmanaged isolated replay provider')
    with _writable(shadow / 'state_5.sqlite') as db:
        db.execute('DELETE FROM threads WHERE id != ?', (session_id,))
        db.execute("UPDATE threads SET rollout_path=?, model_provider=?, history_mode='paginated' WHERE id=?",
                   (str(rollout), provider, session_id))
    app = _AppServer(_native_binary(home), shadow, backup / 'native-replay.stderr', provider=provider)
    try:
        app.rpc('initialize', {'clientInfo': {'name': 'safe_session_repair', 'version': '1'},
                               'capabilities': {'experimentalApi': True}})
        app.proc.stdin.write(b'{"method":"initialized","params":{}}\n')
        app.proc.stdin.flush()
        response = app.rpc('thread/resume', {'threadId': session_id, 'path': str(rollout),
                                            'excludeTurns': True})
        _require(response['thread']['id'] == session_id, 'Native replay returned another thread')
        turn_pages, item_pages = [], []
        directions = {}
        for direction in ('asc', 'desc'):
            turns, nturns = app.pages('thread/turns/list', session_id, direction, itemsView='full')
            items, nitems = app.pages('thread/items/list', session_id, direction)
            turn_pages.append(nturns)
            item_pages.append(nitems)
            directions[direction] = (turns, items)
        for index in (0, 1):
            _require(collections.Counter(map(_canonical, directions['asc'][index])) ==
                     collections.Counter(map(_canonical, directions['desc'][index])),
                     'Bidirectional native pagination differs')
        turns, items = directions['asc']
    finally:
        app.close()
    replayed = rollout.read_bytes()
    appended = _validate_native_append(data, replayed, session_id, provider)
    with _ro(shadow / 'thread_history_1.sqlite') as db:
        validation = _validate_projection(db, session_id, replayed)
        rows = _history_rows(db, session_id)
        expected_turns = {r[0] for r in db.execute(
            'SELECT turn_id FROM thread_turns WHERE thread_id=?', (session_id,))}
        expected_items = {(r[0], r[1]) for r in db.execute(
            'SELECT turn_id,item_id FROM thread_items WHERE thread_id=?', (session_id,))}
        _require(len(turns) == len(expected_turns) and {t['id'] for t in turns} == expected_turns,
                 'Native turn pagination omitted/duplicated records')
        _require(len(items) == len(expected_items) and
                 {(i['turnId'], i['item']['id']) for i in items} == expected_items,
                 'Native item pagination omitted/duplicated records')
    validation.update(item_count=len(items), turn_pages_asc_desc=turn_pages,
                       item_pages_asc_desc=item_pages, bidirectional_pagination_equal=True,
                       native_model_turns_started=0, native_settings_appended=appended,
                       native_settings_append_bytes=len(replayed) - len(data),
                       original_prefix_unchanged=True)
    return rows, validation, replayed


def _plan(home, session_id, target_provider):
    _require(target_provider in MANAGED, 'Unsupported target provider')
    try:
        _require(str(uuid.UUID(session_id)) == session_id, 'Session ID must be a complete UUID')
    except ValueError as exc:
        raise RepairError('Invalid session ID') from exc
    with _ro(home / 'state_5.sqlite') as db:
        columns = [r[1] for r in db.execute('PRAGMA table_info(threads)')]
        row = db.execute('SELECT * FROM threads WHERE id=?', (session_id,)).fetchone()
    _require(row is not None, 'Session not found')
    thread = dict(zip(columns, row))
    _require(thread['model_provider'] in MANAGED, 'Session belongs to an unmanaged provider')
    source_path = Path(thread['rollout_path']).expanduser()
    try:
        source_path.stat()
    except FileNotFoundError as exc:
        raise MissingRolloutError(session_id, source_path, thread['history_mode']) from exc
    path = source_path.resolve()
    _require(path.is_relative_to(home), 'Rollout is outside the requested Codex home')
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise MissingRolloutError(session_id, source_path, thread['history_mode']) from exc
    records = [json.loads(line) for line in data.splitlines()]
    metas = [r.get('payload', {}) for r in records if r.get('type') == 'session_meta']
    _require(len(metas) == 1 and metas[0].get('id') == session_id, 'Rollout identity mismatch')
    _require(metas[0].get('model_provider') in MANAGED or metas[0].get('model_provider') in (None, ''),
             'Session metadata belongs to an unmanaged provider')
    cleaned, counts = transform_rollout(data, target_provider)
    drift = None
    with _ro(home / 'thread_history_1.sqlite') as db:
        try:
            projection = _validate_projection(db, session_id, data)
        except RepairError as exc:
            projection, drift = None, str(exc)
    return {'id': session_id, 'path': path, 'thread': thread, 'state_row': row,
            'state_columns': columns, 'original': data, 'cleaned': cleaned,
            'counts': counts, 'projection': projection, 'index_drift': drift,
            'changed': data != cleaned or drift is not None or
                       thread['model_provider'] != target_provider}


def _atomic_write(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.repair-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _put_rows(db, session_id, rows):
    for table in HISTORY_TABLES:
        db.execute(f'DELETE FROM {table} WHERE thread_id=?', (session_id,))
        if rows[table]:
            placeholders = ','.join('?' for _ in rows[table][0])
            db.executemany(f'INSERT INTO {table} VALUES ({placeholders})', rows[table])


def _apply(home, plan, target_provider, dry_run, prepared=None, stage_only=False):
    session_id, path = plan['id'], plan['path']
    report = {'session_id': session_id, 'target_provider': target_provider,
              'dry_run': dry_run, 'needs_repair': plan['changed'],
              'index_drift': plan['index_drift'], **plan['counts']}
    if dry_run or not plan['changed']:
        report['status'] = 'would_repair' if plan['changed'] else 'unchanged'
        return report
    check_writers(path)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup = Path(prepared['backup_path']) if prepared else home / 'repair-backups' / (stamp + '-' + session_id)
    if not prepared:
        backup.mkdir(parents=True, mode=0o700)
    report['backup_path'] = str(backup)
    mode = path.stat().st_mode & 0o777
    if not prepared:
        shutil.copy2(path, backup / path.name)
    _require((backup / path.name).read_bytes() == plan['original'],
             'Rollout changed while backing up')
    for name in ('state_5.sqlite', 'thread_history_1.sqlite', 'logs_2.sqlite'):
        if prepared:
            break
        if name == 'logs_2.sqlite' and not (home / name).exists():
            continue
        with _ro(home / name) as src, _writable(backup / name) as dst:
            src.backup(dst)
        os.chmod(backup / name, 0o600)
    with _ro(home / 'state_5.sqlite') as db:
        state_before = _outside_thread(db, session_id, False)
    with _ro(home / 'thread_history_1.sqlite') as db:
        history_before = _outside_thread(db, session_id, True)
        old_rows = _history_rows(db, session_id)
    touched = False
    commit_data = plan['cleaned']
    try:
        if prepared:
            new_rows, validation, commit_data = prepared['rows'], prepared['validation'], prepared['data']
        else:
            new_rows, validation, commit_data = _native_rebuild(home, backup, session_id, plan['cleaned'])
        _validate_native_append(plan['cleaned'], commit_data, session_id, target_provider)
        if stage_only:
            report['status'] = 'staged'
            (backup / 'repair-report.json').write_text(json.dumps(report, indent=2) + '\n')
            plan['commit_data'] = commit_data
            return {'backup_path': str(backup), 'rows': new_rows, 'validation': validation, 'data': commit_data}
        check_writers(path)
        _require(path.read_bytes() == plan['original'], 'Rollout changed during replay')
        # Keep state/history in one attached transaction for exception rollback.
        # Backups remain required: SQLite WAL cannot promise cross-file crash atomicity.
        db = sqlite3.connect(home / 'thread_history_1.sqlite', timeout=10)
        try:
            db.execute('ATTACH DATABASE ? AS state_db', (str(home / 'state_5.sqlite'),))
            db.execute('BEGIN IMMEDIATE')
            current_row = db.execute('SELECT * FROM state_db.threads WHERE id=?',
                                     (session_id,)).fetchone()
            _require(current_row == plan['state_row'], 'Target state changed during replay')
            _require(_history_rows(db, session_id) == old_rows,
                     'Target projection changed during replay')
            check_writers(path)
            _require(path.read_bytes() == plan['original'], 'Rollout changed before commit')
            _put_rows(db, session_id, new_rows)
            db.execute("UPDATE state_db.threads SET model_provider=?, history_mode='paginated' WHERE id=?",
                       (target_provider, session_id))
            touched = True
            if commit_data != plan['original']:
                _atomic_write(path, commit_data, mode)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        actual = path.read_bytes()
        _require(actual == commit_data, 'Committed rollout differs from validated source')
        _require(_protected_content([json.loads(l) for l in plan['original'].splitlines()]) ==
                 _protected_content([json.loads(l) for l in actual[:len(plan['cleaned'])].splitlines()]),
                 'Committed protected content multiset mismatch')
        with _ro(home / 'state_5.sqlite') as db:
            _require(_outside_thread(db, session_id, False) == state_before,
                     'Other session state changed (possibly a concurrent writer)')
        with _ro(home / 'thread_history_1.sqlite') as db:
            _require(_outside_thread(db, session_id, True) == history_before,
                     'Other session history changed (possibly a concurrent writer)')
            _require(_history_rows(db, session_id) == new_rows, 'Committed projection mismatch')
            _validate_projection(db, session_id, actual)
            _require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok',
                     'History database quick_check failed')
        report.update(status='repaired', before_sha256=_hash(plan['original']),
                      after_sha256=_hash(actual), before_bytes=len(plan['original']),
                      after_bytes=len(actual), content_multiset_equal=True,
                      other_sessions_unchanged=True, native_pagination=validation)
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__)
        if touched:
            try:
                check_writers(path)
                _require(path.read_bytes() in (commit_data, plan['original']),
                         'Cannot rollback externally modified rollout')
                with _writable(home / 'thread_history_1.sqlite') as db:
                    db.execute('ATTACH DATABASE ? AS state_db', (str(home / 'state_5.sqlite'),))
                    db.execute('BEGIN IMMEDIATE')
                    _put_rows(db, session_id, old_rows)
                    db.execute('UPDATE state_db.threads SET model_provider=?, history_mode=? WHERE id=?',
                               (plan['thread']['model_provider'], plan['thread']['history_mode'], session_id))
                    if path.read_bytes() != plan['original']:
                        _atomic_write(path, plan['original'], mode)
                report['rollback'] = 'restored_exact_target'
            except BaseException as rollback_error:
                report['rollback'] = 'failed'
                (backup / 'repair-report.json').write_text(json.dumps(report, indent=2) + '\n')
                raise RepairError(f'Repair and rollback failed; inspect {backup}; '
                                  f'rollback type {type(rollback_error).__name__}') from exc
        (backup / 'repair-report.json').write_text(json.dumps(report, indent=2) + '\n')
        raise RepairError(f'Repair failed ({type(exc).__name__}); backup: {backup}; {exc}') from exc
    (backup / 'repair-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


@contextlib.contextmanager
def _repair_lock(home, dry_run):
    if dry_run:
        yield
        return
    root = home / 'repair-backups'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.session-repair.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RepairError('Another session repair helper is running') from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def repair_session(home, session_id, target_provider, dry_run=False):
    """Repair one managed session; no inference. Raise on every unsafe condition."""
    home = Path(home).expanduser().resolve()
    with _repair_lock(home, dry_run):
        plan = _plan(home, session_id, target_provider)
        check_writers(plan['path'])
        return _apply(home, plan, target_provider, dry_run)


def _restore_batch(home, plans):
    """Restore only batch targets; never overwrite another session's database rows."""
    for plan in plans:
        check_writers(plan['path'])
        _require(plan['path'].read_bytes() in (plan['original'], plan['cleaned'], plan.get('commit_data')),
                 'Batch rollback refused: externally modified rollout')
    with _writable(home / 'thread_history_1.sqlite') as db:
        db.execute('ATTACH DATABASE ? AS state_db', (str(home / 'state_5.sqlite'),))
        db.execute('BEGIN IMMEDIATE')
        for plan in plans:
            with _ro(Path(plan['prepared']['backup_path']) / 'thread_history_1.sqlite') as src:
                _put_rows(db, plan['id'], _history_rows(src, plan['id']))
            db.execute('UPDATE state_db.threads SET model_provider=?, history_mode=? WHERE id=?',
                       (plan['thread']['model_provider'], plan['thread']['history_mode'], plan['id']))
            if plan['path'].read_bytes() != plan['original']:
                _atomic_write(plan['path'], plan['original'], plan['path'].stat().st_mode & 0o777)


def _reuse_staged(home, batch_path, plans, target_provider):
    if batch_path is None:
        return {}
    batch_path = Path(batch_path).resolve()
    _require(batch_path.is_relative_to(home / 'repair-backups'), 'Reuse batch is outside repair backups')
    manifest = json.loads((batch_path / 'batch-report.json').read_text())
    _require(manifest.get('status') == 'failed' and
             manifest.get('rollback') == 'not_needed_no_production_changes',
             'Only a fully uncommitted batch can be reused')
    plans_by_id = {p['id']: p for p in plans}
    reused = {}
    for name in manifest['session_backups']:
        backup = Path(name).resolve()
        _require(backup.is_relative_to(home / 'repair-backups'), 'Invalid reusable session backup')
        report = json.loads((backup / 'repair-report.json').read_text())
        session_id = report['session_id']
        plan = plans_by_id.get(session_id)
        if plan is None or not plan['changed']:
            continue
        _require(report['status'] == 'staged' and report['target_provider'] == target_provider,
                 'Reusable stage has the wrong status/provider')
        _require(_hash((backup / plan['path'].name).read_bytes()) == plan['preflight_sha256'],
                 'Reusable stage source differs from current rollout')
        with _ro(backup / 'state_5.sqlite') as db:
            _require(db.execute('SELECT * FROM threads WHERE id=?', (session_id,)).fetchone() == plan['state_row'],
                     'Reusable stage source state differs from current state')
        original = (backup / plan['path'].name).read_bytes()
        cleaned, _ = transform_rollout(original, target_provider)
        shadow = backup / 'isolated-rebuild-home'
        replayed = (shadow / 'sessions' / plan['path'].name).read_bytes()
        appended = _validate_native_append(cleaned, replayed, session_id, target_provider)
        with _ro(shadow / 'thread_history_1.sqlite') as db:
            _require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'Reusable projection failed quick_check')
            validation = _validate_projection(db, session_id, replayed)
            rows = _history_rows(db, session_id)
            validation['item_count'] = len(rows['thread_items'])
        validation.update(reused_staged_batch=str(batch_path), native_settings_appended=appended,
                          native_settings_append_bytes=len(replayed) - len(cleaned),
                          original_prefix_unchanged=True, native_model_turns_started=0,
                          bidirectional_pagination_checked_in_original_stage=True)
        reused[session_id] = {'backup_path': str(backup), 'data': replayed,
                              'rows': rows, 'validation': validation}
    return reused


def repair_all(home, target_provider, dry_run=False, commit_callback=None, reuse_batch=None):
    """Repair affected managed sessions; preflight all writers before any edit.

    Stage all native replays before committing any production change. Any later
    failure restores all batch targets. An optional commit_callback lets the
    switch adapter commit its config inside this rollback boundary.
    Missing rollouts are explicitly skipped, without migrating DB-only history.
    """
    home = Path(home).expanduser().resolve()
    _require(target_provider in MANAGED, 'Unsupported target provider')
    with _repair_lock(home, dry_run):
        with _ro(home / 'state_5.sqlite') as db:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM threads WHERE model_provider IN ('openai','krillcode') ORDER BY id")]
        plans, skipped, missing = [], [], []
        for session_id in ids:
            try:
                plan = _plan(home, session_id, target_provider)
            except MissingRolloutError as exc:
                missing.append(exc.details)
                continue
            except RepairError as exc:
                if str(exc) == 'Session metadata belongs to an unmanaged provider':
                    skipped.append(session_id)
                    continue
                raise
            # Do not retain every large rollout twice throughout an --all run.
            # Re-plan one changed session at a time after the batch preflight.
            plan['preflight_sha256'] = _hash(plan.pop('original'))
            plan.pop('cleaned')
            plans.append(plan)
        for plan in plans:
            if plan['changed']:
                check_writers(plan['path'])
        reusable = _reuse_staged(home, reuse_batch, plans, target_provider) if not dry_run else {}
        results, staged = [], []
        batch = None
        committed = False
        try:
            if not dry_run and (any(p['changed'] for p in plans) or commit_callback):
                stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
                batch = home / 'repair-backups' / (stamp + '-batch-' + target_provider)
                batch.mkdir(mode=0o700)
                for name in ('state_5.sqlite', 'thread_history_1.sqlite'):
                    with _ro(home / name) as src, _writable(batch / name) as dst:
                        src.backup(dst)
                    os.chmod(batch / name, 0o600)
                for candidate in plans:
                    if candidate['changed']:
                        destination = batch / 'rollouts' / candidate['id']
                        destination.mkdir(parents=True, mode=0o700)
                        shutil.copy2(candidate['path'], destination / candidate['path'].name)
                        _require(_hash((destination / candidate['path'].name).read_bytes()) ==
                                 candidate['preflight_sha256'], 'Rollout changed during batch backup')
            for plan in plans:
                if plan['changed']:
                    try:
                        fresh = _plan(home, plan['id'], target_provider)
                    except MissingRolloutError as exc:
                        missing.append(exc.details)
                        continue
                    _require(_hash(fresh['original']) == plan['preflight_sha256'] and
                             fresh['state_row'] == plan['state_row'],
                             'Session changed after batch preflight')
                    plan = fresh
                if dry_run or not plan['changed']:
                    results.append(_apply(home, plan, target_provider, dry_run))
                else:
                    if plan['id'] in reusable:
                        print(f'Repair reuse staging {plan["id"]}', file=__import__('sys').stderr, flush=True)
                        plan['prepared'] = reusable[plan['id']]
                        plan['commit_data'] = plan['prepared']['data']
                    else:
                        print(f'Repair staging {plan["id"]}', file=__import__('sys').stderr, flush=True)
                        plan['prepared'] = _apply(home, plan, target_provider, False, stage_only=True)
                    staged.append(plan)
            if batch:
                manifest = {'status': 'staged', 'target_provider': target_provider,
                            'sessions': [{'session_id': p['id'], 'backup_path': p['prepared']['backup_path']}
                                         for p in staged], 'skipped_missing_rollouts': missing}
                (batch / 'batch-report.json').write_text(json.dumps(manifest, indent=2) + '\n')
            for plan in staged:
                check_writers(plan['path'])
            for plan in staged:
                committed = True
                results.append(_apply(home, plan, target_provider, False, prepared=plan['prepared']))
            if not dry_run and commit_callback:
                commit_callback()
            if batch:
                manifest['status'] = 'committed'
                (batch / 'batch-report.json').write_text(json.dumps(manifest, indent=2) + '\n')
        except BaseException as exc:
            rollback = 'not_needed_no_production_changes'
            if committed:
                try:
                    _restore_batch(home, staged)
                    rollback = 'restored_all_batch_targets'
                except BaseException as restore_error:
                    rollback = 'FAILED_' + type(restore_error).__name__
            if batch:
                (batch / 'batch-report.json').write_text(json.dumps({
                    'status': 'failed', 'rollback': rollback, 'error_type': type(exc).__name__,
                    'session_backups': [p['prepared']['backup_path'] for p in staged],
                    'skipped_missing_rollouts': missing}, indent=2) + '\n')
            raise RepairError(f'Batch failed; {rollback}; backup: {batch}; {exc}') from exc
        return {'sessions': results, 'skipped_unmanaged_metadata': skipped,
                'skipped_missing_rollouts': missing,
                'batch_backup_path': str(batch) if batch else None,
                'dry_run': dry_run}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--target-provider', required=True, choices=sorted(MANAGED))
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument('--all', action='store_true')
    selector.add_argument('--session-id')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.all:
            result = repair_all(args.home, args.target_provider, args.dry_run)
        else:
            result = repair_session(args.home, args.session_id, args.target_provider, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Never dump source records, credentials or a subprocess's stderr.
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__,
                          'error': str(exc)}, ensure_ascii=False), file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
