"""Durable human read position and per-window drafts."""
import json
import math
import re
import threading
import time
import uuid

from agentchat import store

VERSION = 1
SESSION_ID = re.compile(r'[0-9a-f]{32}')


def _directory():
    store._prepare()
    root = store.DATA_DIR / 'ui'
    created = not root.exists()
    root.mkdir(exist_ok=True, mode=0o700)
    if created:
        store._sync_directory(store.DATA_DIR)
    return root


def get_read_marker():
    path = _directory() / 'read.json'
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
            raise ValueError('unsupported marker version')
        return store._integer(value['read_through'], 'human read marker')
    except FileNotFoundError:
        return 0
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f'Invalid human read marker: {path}') from error


def mark_read(message_id):
    store._integer(message_id, 'human read marker')
    with store._file_lock(_directory() / 'read.lock'):
        current = get_read_marker()
        if message_id <= current:
            return current
        tail = store.read_tail(1)
        if not tail or message_id > tail[-1]['id']:
            raise ValueError('Human read marker cannot exceed the latest message')
        store._atomic_json(_directory() / 'read.json', {'version': VERSION, 'read_through': message_id})
        return message_id


def _payload(draft, cursor, chips, chip_count):
    if not isinstance(draft, str):
        raise ValueError('Draft must be text')
    store._integer(cursor, 'draft cursor')
    store._integer(chip_count, 'paste chip count')
    if cursor > len(draft):
        raise ValueError('Draft cursor is past the end of the text')
    chips = {} if chips is None else chips
    if not isinstance(chips, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                          for k, v in chips.items()):
        raise ValueError('Draft chips must map keys to text')
    return {'draft': draft, 'cursor': cursor, 'chips': dict(chips), 'chip_count': chip_count}


def _saved_payload(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
        raise ValueError('unsupported draft version')
    updated = value.get('updated_at')
    if type(updated) not in (int, float) or not math.isfinite(updated):
        raise ValueError('invalid draft timestamp')
    payload = _payload(value['draft'], value['cursor'], value['chips'], value['chip_count'])
    pending = value.get('pending_send')
    if pending is not None:
        if not isinstance(pending, dict) or not isinstance(pending.get('text'), str) or not pending['text'].strip():
            raise ValueError('invalid pending send')
        store._request_id(pending.get('request_id'))
        pending = dict(pending)
    return payload, pending


def _same_content(first, second):
    return first['draft'] == second['draft'] and first['chips'] == second['chips']


class DraftSession:
    """Own one draft lock; recover an inactive window rather than another live one."""

    def __init__(self):
        self.restored = None
        self.warnings = []
        self._mutex = threading.RLock()
        root = _directory()
        self.directory = root / 'drafts'
        created = not self.directory.exists()
        self.directory.mkdir(exist_ok=True, mode=0o700)
        if created:
            store._sync_directory(root)
        self._lock = None
        self._pending = None
        candidates = []
        for path in self.directory.glob('*.json'):
            if SESSION_ID.fullmatch(path.stem):
                try:
                    candidates.append((path.stat().st_mtime_ns, path))
                except FileNotFoundError:
                    pass
        for _, path in sorted(candidates, reverse=True):
            try:
                lock = store._file_lock(path.with_suffix('.lock'), nonblocking=True)
            except BlockingIOError:
                continue
            except OSError as error:
                self.warnings.append(f'Saved draft {path.name} could not be locked; preserved ({type(error).__name__})')
                continue
            try:
                saved, pending = _saved_payload(path)
                if not saved['draft']:
                    lock.close()
                    continue
            except FileNotFoundError:
                lock.close()
                continue
            except (ValueError, TypeError, KeyError, OSError) as error:
                lock.close()
                self.warnings.append(f'Saved draft {path.name} could not be read; preserved ({type(error).__name__})')
                continue
            if pending is not None:
                try:
                    committed = store.find_request(pending['request_id'])
                    if committed is not None:
                        if committed['sender'] != store.USER or committed['text'] != pending['text']:
                            raise ValueError('pending send does not match its committed record')
                        try:
                            path.unlink()
                            store._sync_directory(self.directory)
                        except OSError:
                            self.warnings.append('An already sent draft could not be cleaned up; it was not restored')
                        lock.close()
                        continue
                except (ValueError, OSError) as error:
                    self.warnings.append(f'Draft delivery could not be confirmed; preserved ({type(error).__name__})')
            self._pending = pending
            self.path, self._lock, self.restored = path, lock, saved
            break
        if self._lock is None:
            self.path = self.directory / (uuid.uuid4().hex + '.json')
            self._lock = store._file_lock(self.path.with_suffix('.lock'), nonblocking=True)
        self.session_id = self.path.stem
        self._last_payload = (dict(self.restored, chips=dict(self.restored['chips']))
                              if self.restored else None)

    def adopt_restored_representation(self, draft, cursor, chips, chip_count):
        """Treat a lossless UI format conversion as the same recovered draft.

        The next save or send can persist the new form. Until then the previous
        representation and its pending receipt remain available on disk.
        """
        payload = _payload(draft, cursor, chips, chip_count)
        with self._mutex:
            if self._lock is None or self.restored is None or not draft:
                raise ValueError('Draft representation can only be adopted after restoring a draft')
            self._last_payload = payload
            self.restored = None

    def save(self, draft, cursor, chips=None, chip_count=0):
        payload = _payload(draft, cursor, chips, chip_count)
        with self._mutex:
            if self._lock is None:
                raise ValueError('Draft session is closed')
            store._prepare()
            if payload == self._last_payload:
                return
            pending = None
            if not draft:
                try:
                    self.path.unlink()
                    store._sync_directory(self.directory)
                except FileNotFoundError:
                    pass
            else:
                record = dict(payload, version=VERSION, updated_at=time.time())
                if self._pending is not None and self._last_payload and _same_content(payload, self._last_payload):
                    pending = record['pending_send'] = self._pending
                store._atomic_json(self.path, record)
            self._pending = pending
            self._last_payload = payload
            self.restored = None

    def send(self, text, draft, cursor, chips=None, chip_count=0):
        """Journal the intent before append; a committed retry never becomes a second message."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Message text must not be empty')
        payload = _payload(draft, cursor, chips, chip_count)
        if not draft:
            raise ValueError('Sending requires a nonempty saved draft')
        with self._mutex:
            if self._lock is None:
                raise ValueError('Draft session is closed')
            store._prepare()
            pending = self._pending
            if pending is None or pending['text'] != text:
                pending = {'request_id': uuid.uuid4().hex, 'text': text}
            record = dict(payload, version=VERSION, updated_at=time.time(), pending_send=pending)
            store._atomic_json(self.path, record)
            self._pending, self._last_payload, self.restored = pending, payload, None
            message = store.append_message(store.USER, text, request_id=pending['request_id'])
            try:
                self.save('', 0)
            except OSError:
                # Disk still has a recognizable committed intent. In memory,
                # however, the next draft is a new intent even if its text is
                # identical; force its next save rather than reusing this token.
                self._pending = self._last_payload = self.restored = None
            return message

    def close(self):
        with self._mutex:
            if self._lock is not None:
                self._lock.close()
                self._lock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
