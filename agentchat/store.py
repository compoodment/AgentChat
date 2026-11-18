"""Shared, durable message storage.

Chat mutations use one flock; presence and UI state have separate locks.
Complete log records are append-only. After
an interrupted write, an incomplete suffix is preserved separately before the
next append repairs the tail. Metadata is replaced atomically.
"""

from bisect import bisect_right
import ctypes
import fcntl
import json
import math
import os
import re
import select
import struct
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_state_home = Path(os.environ.get("XDG_STATE_HOME", "")).expanduser()
if not _state_home.is_absolute():
    _state_home = Path.home() / ".local" / "state"
DATA_DIR = Path(os.environ.get("AGENTCHAT_DATA", str(_state_home / "agentchat"))).expanduser()
CHAT_DIR = DATA_DIR / "chat"
CURSOR_DIR = DATA_DIR / "cursors"
CHECKOUT_ROOT = Path(__file__).resolve().parents[1]

USER = "user"
AGENTS = ("claude", "codex")
SENDERS = (USER, "system") + AGENTS
KINDS = ("msg", "claim", "release", "system")
GUARD_LIMIT = 4
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_GUARD_NOTICE = "Agent loop guard: "
_RATE_NOTICE = "Agent rate guard: "
_PAUSE_NOTICE = "Agent pause guard: "
RATE_LIMIT = 50
RATE_WINDOW = 600
_cache_lock = threading.RLock()
_file_cache = {}
_record_signature = None
_record_cache = []
_summary_cache = None


class GuardBlocked(Exception):
    """An agent message was refused by the active posting policy."""


class ClaimConflict(Exception):
    """Another sender holds an overlapping claim, or a release is invalid."""


def _prepare():
    for path in (DATA_DIR, CHAT_DIR, CURSOR_DIR):
        # pathlib's exist_ok check can race a directory replacement: mkdir
        # sees the old directory, but is_dir sees the gap before its successor.
        for attempt in range(3):
            try:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                break
            except FileExistsError:
                if attempt == 2:
                    raise


def _file_lock(path, *, exclusive=True, nonblocking=False):
    """Return an owned lock handle; closing it releases the shared/exclusive flock."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    stream = os.fdopen(fd, "r+b")
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, mode | (fcntl.LOCK_NB if nonblocking else 0))
        return stream
    except BaseException:
        stream.close()
        raise


@contextmanager
def _locked(exclusive=False):
    _prepare()
    with _file_lock(DATA_DIR / "lock", exclusive=exclusive):
        yield


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _request_id(value):
    if not isinstance(value, str) or REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id must be 32 lowercase hexadecimal characters")
    return value


def _records():
    """Read with a lock held. Ignore only an unterminated final record."""
    with _cache_lock:
        return _cached_records()


def _cached_records():
    global _record_signature, _record_cache
    paths = sorted(CHAT_DIR.glob("*.jsonl"))
    signature = tuple((str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                       st.st_ctime_ns) for path in paths for st in [path.stat()])
    if signature == _record_signature:
        return _record_cache
    messages = []
    seen, requests = set(), set()
    next_cache = {}
    for path, stamp in zip(paths, signature):
        previous = _file_cache.get(str(path))
        # App writes only append. A changed inode, truncation or same-size edit
        # requires a full reread; an incomplete line is retried from its start.
        incremental = (previous is not None and previous[0][1:3] == stamp[1:3]
                       and stamp[3] > previous[0][3])
        if previous is not None and previous[0] == stamp:
            records, offset, line_number = previous[1:]
        else:
            records = list(previous[1]) if incremental else []
            offset = previous[2] if incremental else 0
            line_number = previous[3] if incremental else 0
            records, offset, line_number = _read_file(path, records, offset, line_number)
        next_cache[str(path)] = (stamp, records, offset, line_number)
        for message in records:
            if message["id"] in seen:
                raise ValueError(f"Invalid log record at {path.name}: duplicate message id")
            seen.add(message["id"])
            request = message.get("request_id")
            if request is not None:
                if request in requests:
                    raise ValueError(f"Invalid log record at {path.name}: duplicate request id")
                requests.add(request)
        messages.extend(records)
    messages.sort(key=lambda message: message["id"])
    _file_cache.clear()
    _file_cache.update(next_cache)
    _record_signature, _record_cache = signature, messages
    return messages


def _read_file(path, messages, offset, line_number):
    with path.open("rb") as stream:
        stream.seek(offset)
        for line in stream:
            if not line.endswith(b"\n"):
                break
            line_number += 1
            messages.append(_parse_record(line, f"{path.name}:{line_number}"))
            offset += len(line)
    return messages, offset, line_number


def _parse_record(line, location):
    try:
        message = json.loads(line)
        if (not isinstance(message, dict) or type(message.get("id")) is not int
                or message["id"] < 1 or message.get("sender") not in SENDERS
                or message.get("kind") not in KINDS
                or not isinstance(message.get("ts"), str)
                or not isinstance(message.get("text"), str)):
            raise ValueError("invalid message fields")
        if "request_id" in message:
            _request_id(message["request_id"])
            if message["kind"] != "msg" or message["sender"] == "system":
                raise ValueError("request_id is only valid for participant messages")
        return message
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid log record at {location}: {error}") from error


def _file_tail(path, limit):
    chunks, newlines = [], 0
    with path.open("rb") as stream:
        position = stream.seek(0, os.SEEK_END)
        while position and newlines <= limit:
            start = max(0, position - 8192)
            stream.seek(start)
            chunk = stream.read(position - start)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
            position = start
    # The final split element is either empty or an uncommitted suffix.
    lines = b"".join(reversed(chunks)).split(b"\n")[:-1][-limit:]
    rows = [_parse_record(line, f"{path.name}:tail") for line in lines]
    if any(a["id"] >= b["id"] for a, b in zip(rows, rows[1:])):
        raise ValueError(f"Invalid log record at {path.name}: tail ids must increase")
    return rows


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix=".agentchat-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _atomic_json(path, value):
    _atomic_write(path, (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))


def _repair_tails():
    # A newline is the record's commit marker. Never alter committed records.
    for path in CHAT_DIR.glob("*.jsonl"):
        with path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if not size:
                continue
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) == b"\n":
                continue
            # Find the previous newline without loading the entire daily file.
            position = size
            boundary = 0
            while position:
                start = max(0, position - 8192)
                stream.seek(start)
                chunk = stream.read(position - start)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    boundary = start + newline + 1
                    break
                position = start
            stream.seek(boundary)
            suffix = stream.read()
            recovery = DATA_DIR / "recovery"
            recovery.mkdir(exist_ok=True, mode=0o700)
            _sync_directory(DATA_DIR)
            name = recovery / f"{path.name}.{time.time_ns()}.partial"
            _atomic_write(name, suffix)
            stream.truncate(boundary)
            stream.flush()
            os.fsync(stream.fileno())


def _summary(messages):
    """Reuse derived state; fold only appended records when the prefix survives."""
    global _summary_cache
    with _cache_lock:
        if _summary_cache is not None and _summary_cache[0] is messages:
            return _summary_cache[1]
        previous = _summary_cache
        incremental = (previous is not None and len(messages) >= len(previous[0])
                       and all(a is b for a, b in zip(messages, previous[0])))
        if incremental:
            old = previous[1]
            result = dict(old, claims=dict(old["claims"]), times=list(old["times"]),
                          rate_notices=list(old["rate_notices"]))
            tail = messages[len(previous[0]):]
        else:
            result = {"streak": 0, "noticed": False, "claims": {},
                      "times": [], "rate_notices": []}
            tail = messages
        now = time.time()
        for message in tail:
            sender, kind, text = message["sender"], message["kind"], message["text"]
            if kind == "msg" and sender == USER:
                result["streak"], result["noticed"] = 0, False
            elif kind == "msg" and sender in AGENTS:
                result["streak"] += 1
                result["times"].append(_timestamp(message, now))
            elif kind == "system" and sender == "system":
                if text.startswith(_GUARD_NOTICE):
                    result["noticed"] = True
                elif text.startswith(_RATE_NOTICE):
                    result["rate_notices"].append(_timestamp(message, 0))
            if kind == "claim":
                result["claims"][text] = sender
            elif kind == "release" and result["claims"].get(text) == sender:
                del result["claims"][text]
        result["times"].sort()
        result["rate_notices"].sort()
        _summary_cache = (messages, result)
        return result


def _timestamp(message, fallback):
    try:
        return datetime.fromisoformat(message["ts"]).timestamp()
    except (ValueError, OverflowError, OSError):
        return fallback


def _autonomy_expiry(now):
    try:
        value = json.loads((DATA_DIR / "autonomy.json").read_text())["expires_at"]
        if type(value) in (int, float) and math.isfinite(value) and now < value <= now + 86400:
            return value
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        pass
    return None


def _policy(mode="normal", *, start_after_id=0, **values):
    limit, window = {"normal": (4, 60), "wait": (4, None), "paused": (0, None), "custom": (None, None)}[mode]
    return dict(version=1, mode=mode, limit=limit, window_s=window, expires_at=None,
                start_after_id=start_after_id, **values)


def _validate_policy(value, *, nested=False):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value['version'] != 1:
        raise ValueError("Invalid policy version")
    mode = value.get("mode")
    if mode not in ("normal", "custom", "wait", "paused") or (nested and mode == "paused"):
        raise ValueError("Invalid policy mode")
    _integer(value.get("start_after_id"), "policy start id")
    _integer(value.get("limit"), "message limit")
    if mode == "custom":
        if value['limit'] < 1:
            raise ValueError("Message limit must be positive")
        for name in ("window_s", "expires_at"):
            number = value.get(name)
            if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
                raise ValueError(f"Invalid policy {name}")
        if value['expires_at'] > time.time() + 86400:
            raise ValueError('Policy expiry exceeds one day')
        if value['window_s'] > 86400:
            raise ValueError("Policy window exceeds one day")
    else:
        default = _policy(mode)
        if (value.get('limit'), value.get('window_s'), value.get('expires_at')) != (
                default['limit'], default['window_s'], None):
            raise ValueError("Invalid fixed policy parameters")
    if mode == "paused":
        _validate_policy(value.get('resume'), nested=True)
    return dict(value)


def _current_policy(now):
    try:
        value = _validate_policy(json.loads((DATA_DIR / "policy.json").read_text()))
    except FileNotFoundError:
        expires = _autonomy_expiry(now)
        return dict(_policy('custom'), limit=RATE_LIMIT, window_s=RATE_WINDOW, expires_at=expires) if expires else _policy()
    except (ValueError, KeyError, TypeError):
        return _policy('wait')  # damaged configuration must not silently resume quiet/auto work
    if value['mode'] == 'custom' and value['expires_at'] <= now:
        return _policy()
    return value


def _guard_state(messages):
    now = time.time()
    summary = _summary(messages)
    policy = _current_policy(now)
    if policy['start_after_id'] > (messages[-1]['id'] if messages else 0):
        policy = _policy('wait')
    mode, limit, window, expires = (policy[k] for k in ('mode', 'limit', 'window_s', 'expires_at'))
    result = dict(mode=mode, blocked=False, used=0, limit=limit, message_limit=limit,
                  window_seconds=window, expires_at=expires, retry_after=0,
                  agent_streak=summary['streak'])
    if mode == 'paused':
        noticed = any(m['id'] > policy['start_after_id'] and m['sender'] == 'system'
                      and m['text'].startswith(_PAUSE_NOTICE) for m in reversed(messages))
        result.update(blocked=True, retry_after=None)
    elif mode == 'wait':
        used, noticed = 0, False
        for message in reversed(messages):
            if message['id'] <= policy['start_after_id'] or (message['kind'] == 'msg' and message['sender'] == USER):
                break
            if message['kind'] == 'msg' and message['sender'] in AGENTS:
                used += 1
            if message['sender'] == 'system' and message['text'].startswith(_GUARD_NOTICE):
                noticed = True
        result.update(used=used, blocked=used >= limit, retry_after=None if used >= limit else 0)
    else:
        times = summary['times']
        active = times[bisect_right(times, now-window):bisect_right(times, now)]
        blocked = len(active) >= limit
        delay = max(0, active[-limit] + window-now) if blocked else 0
        if expires is not None:
            delay = min(delay, expires-now)
        notices = summary['rate_notices']
        noticed = blocked and bool(notices) and notices[-1] >= active[-limit]
        result.update(used=len(active), blocked=blocked, retry_after=delay)
    return result, noticed


def _save_policy(policy, messages):
    _atomic_json(DATA_DIR / "policy.json", policy)
    description = f"Posting policy: {policy['mode']}"
    if policy['window_s'] is not None:
        description += f"; {policy['limit']} agent messages per {policy['window_s']:g} seconds"
    if policy['expires_at'] is not None:
        description += f"; until {datetime.fromtimestamp(policy['expires_at']).astimezone().isoformat()}"
    try:
        _write_record('system', description + '.', 'system', messages)
    except OSError:
        pass  # The policy itself is already durable; the notice is auxiliary.
    return _guard_state(_records())[0]


def set_policy(mode, *, limit=None, window_s=None, duration_s=None):
    if not isinstance(mode, str):
        raise ValueError('Policy mode must be a string')
    mode = {'auto': 'custom', 'quiet': 'paused'}.get(mode, mode)
    if mode not in ('normal', 'custom', 'wait', 'paused'):
        raise ValueError("Policy mode must be normal, custom, wait or paused")
    if mode == 'custom':
        _integer(limit, 'message limit')
        if limit < 1:
            raise ValueError('Message limit must be positive')
        for name, number in [('window', window_s), ('duration', duration_s)]:
            if type(number) not in (int, float) or not math.isfinite(number) or not 0 < number <= 86400:
                raise ValueError(f'Policy {name} must be finite, positive and at most 86400 seconds')
    elif any(value is not None for value in (limit, window_s, duration_s)):
        raise ValueError('Only custom accepts custom rate/duration parameters')
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        current = _current_policy(time.time())
        if mode == 'paused' and current['mode'] == 'paused':
            return _guard_state(messages)[0]
        policy = _policy(mode, start_after_id=messages[-1]['id'] if messages else 0)
        if mode == 'custom':
            policy.update(limit=limit, window_s=window_s, expires_at=time.time()+duration_s)
        elif mode == 'paused':
            policy['resume'] = current
        return _save_policy(policy, messages)


def resume_policy():
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        current = _current_policy(time.time())
        if current['mode'] != 'paused':
            return _guard_state(messages)[0]
        policy = dict(current['resume'])
        if policy['mode'] == 'custom' and policy['expires_at'] <= time.time():
            policy = _policy()
        policy['start_after_id'] = messages[-1]['id'] if messages else 0
        return _save_policy(policy, messages)


def set_autonomy(seconds):
    """Compatibility for the original fixed 50-message/600-second auto policy."""
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 86400:
        raise ValueError('Autonomy duration must be finite, from 0 through 86400 seconds')
    return set_policy('custom', limit=RATE_LIMIT, window_s=RATE_WINDOW, duration_s=seconds) if seconds else set_policy('normal')


def _claims(messages):
    return dict(_summary(messages)["claims"])


def _main_root():
    """Recognize main-checkout absolute paths as well as this worktree's."""
    dotgit = CHECKOUT_ROOT / ".git"
    if dotgit.is_file():
        content = dotgit.read_text().strip()
        if content.startswith("gitdir: "):
            location = Path(content[8:])
            if not location.is_absolute():
                location = CHECKOUT_ROOT / location
            for parent in location.resolve().parents:
                if parent.name == ".git":
                    return parent.parent
    return CHECKOUT_ROOT


def _normalize_claim(text):
    path = Path(text).expanduser()
    roots = (CHECKOUT_ROOT.resolve(), _main_root().resolve())
    if not path.is_absolute():
        # Resolving catches both '..' escapes and symlinks pointing outside.
        resolved = (roots[0] / path).resolve()
        roots = roots[:1]
    else:
        resolved = path.resolve()
    for root in roots:
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            pass
    raise ValueError("Claim path must stay inside the project checkout")


def _overlaps(first, second):
    if first.startswith("task:") or second.startswith("task:"):
        return first == second
    a, b = Path(first).parts, Path(second).parts
    return a[:len(b)] == b or b[:len(a)] == a


def _write_record(sender, text, kind, messages, request_id=None):
    seq_path = DATA_DIR / "seq"
    seq = _integer(int(seq_path.read_text().strip()), "sequence") if seq_path.exists() else 0
    last = messages[-1]["id"] if messages else 0
    now = datetime.now().astimezone()
    message = {"id": max(seq, last) + 1, "ts": now.isoformat(),
               "sender": sender, "kind": kind, "text": text}
    if request_id is not None:
        message["request_id"] = request_id
    path = CHAT_DIR / (now.date().isoformat() + ".jsonl")
    data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written == 0:
                raise OSError("Short write to chat log")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory(CHAT_DIR)
    # The log is now durable. Auxiliary sequence metadata must not turn a
    # committed message into a false failure (and invite a duplicate retry).
    try:
        _atomic_write(seq_path, f"{message['id']}\n".encode())
    except OSError:
        pass  # The next writer also reads the committed log's highest id.
    return message


def _sync_committed_logs():
    # A complete line may be visible after an interrupted fsync. Before an
    # idempotent retry is confirmed, establish durability rather than guessing.
    for path in CHAT_DIR.glob("*.jsonl"):
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    _sync_directory(CHAT_DIR)


def find_request(request_id):
    _request_id(request_id)
    with _locked():
        message = next((m for m in _records() if m.get("request_id") == request_id), None)
        if message is not None:
            _sync_committed_logs()
            return dict(message)
    return None


def _without_inline_code(line):
    # Match whole backtick runs using their lengths, not a backtracking regex.
    # An unmatched run stays prose; an equal closing run ends the code span.
    runs = list(re.finditer(r"`+", line))
    next_run, following = {}, {}
    for index in range(len(runs)-1, -1, -1):
        size = runs[index].end() - runs[index].start()
        if size in following:
            next_run[index] = following[size]
        following[size] = index
    result, cursor, index = [], 0, 0
    while index < len(runs):
        if index in next_run:
            end = next_run[index]
            result.append(line[cursor:runs[index].start()])
            result.append(" ")  # removing code must not manufacture a mention
            cursor, index = runs[end].end(), end+1
        else:
            index += 1
    result.append(line[cursor:])
    return ''.join(result)


def mentioned_agents(text):
    """Whole agent mentions in prose; quoted/code examples do not address an agent.

    This is deliberately a small routing rule, not a full Markdown renderer.
    Unclosed fenced blocks remain code; unmatched inline backticks remain prose.
    """
    prose = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is not None:
            if match and match[1][0] == fence[0] and len(match[1]) >= fence[1] and not match[2].strip():
                fence = None
            continue
        if match:
            fence = (match[1][0], len(match[1]))
            continue
        if line.lstrip().startswith('>') or line.startswith(('    ', '\t')):
            continue
        prose.append(_without_inline_code(line))
    names = re.findall(r"(?<![\w.\\])@(claude|codex|all)(?![\w-])", '\n'.join(prose), re.I)
    result = {name.lower() for name in names}
    if 'all' in result:
        result.remove('all')
        result.update(AGENTS)
    return result


def append_message(sender, text, kind="msg", *, request_id=None):
    if sender not in SENDERS:
        raise ValueError(f"Unknown sender: {sender}")
    if kind not in KINDS:
        raise ValueError(f"Unknown message kind: {kind}")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Message text must not be empty")
    if request_id is not None:
        _request_id(request_id)
        if kind != "msg" or sender == "system":
            raise ValueError("request_id is only valid for participant messages")
    task_id = None
    if kind in ("claim", "release"):
        text = text.strip()
        if text.startswith("task:"):
            number = text[5:]
            if not number.isascii() or not number.isdigit() or int(number) < 1:
                raise ValueError("Task claim must use task:<positive user message id>")
            task_id = int(number)
            text = f"task:{task_id}"
            if sender == USER:
                raise ValueError("Task ownership is for agents; use a file path for a user claim")
            _agent(sender)
        else:
            text = _normalize_claim(text)
    with _locked(exclusive=True):
        _repair_tails()
        messages = _records()
        if request_id is not None:
            existing = next((m for m in messages if m.get("request_id") == request_id), None)
            if existing is not None:
                if (existing["sender"], existing["kind"], existing["text"]) != (sender, kind, text):
                    raise ValueError("request_id was already used for different message content")
                _sync_committed_logs()
                return dict(existing)
        if task_id is not None and kind == "claim":
            source = next((m for m in messages if m["id"] == task_id), None)
            if source is None or source["sender"] != USER or source["kind"] != "msg":
                raise ValueError(f"Task {task_id} must refer to a user message")
            assigned = mentioned_agents(source["text"])
            if len(assigned) == 1 and sender not in assigned:
                raise ClaimConflict(f"Task {task_id} is addressed to {next(iter(assigned))}")
        status, noticed = _guard_state(messages)
        if kind == "msg" and sender in AGENTS and status["blocked"]:
            if status['mode'] in ('normal', 'custom'):
                prefix = _RATE_NOTICE
                reason = f"retry after {math.ceil(status['retry_after'])} seconds"
            elif status['mode'] == 'paused':
                prefix, reason = _PAUSE_NOTICE, 'posting is paused; use /resume or change policy'
            else:
                prefix, reason = _GUARD_NOTICE, 'wait for a user message'
            if not noticed:
                _write_record('system', f'{prefix}{sender} was refused; {reason}.', 'system', messages)
            raise GuardBlocked(f'Agent message limit reached; {reason}')
        if kind in ("claim", "release"):
            claims = _claims(messages)
            if kind == "claim":
                for held, holder in claims.items():
                    if holder != sender and _overlaps(text, held):
                        raise ClaimConflict(f"{text} overlaps {held}, held by {holder}")
            elif claims.get(text) != sender:
                holder = claims.get(text)
                raise ClaimConflict(f"{text} is held by {holder}" if holder else
                                    f"No active claim for {text}")
        return _write_record(sender, text, kind, messages, request_id)


def take_task(agent, msg_id):
    """Atomically claim a user request before announcing or starting work."""
    _agent(agent)
    _integer(msg_id, "task id")
    if msg_id == 0:
        raise ValueError("Task id must be positive")
    return append_message(agent, f"task:{msg_id}", kind="claim")


def release_task(agent, msg_id):
    """Release a request when work is finished or explicitly handed off."""
    _agent(agent)
    _integer(msg_id, "task id")
    if msg_id == 0:
        raise ValueError("Task id must be positive")
    return append_message(agent, f"task:{msg_id}", kind="release")


def read_messages(after_id=None, limit=None):
    after_id = 0 if after_id is None else _integer(after_id, "after_id")
    if limit is not None:
        _integer(limit, "limit")
        if limit == 0:
            return []
    if after_id and _record_signature is None:
        # A fresh UI/CLI can answer a recent cursor from bounded tails. If the
        # cursor is older than that range, preserve the full unread contract.
        tail_limit = max(1000, limit or 0)
        tail = read_tail(tail_limit)
        if not tail or len(tail) < tail_limit or after_id >= tail[0]["id"] - 1:
            rows = [message for message in tail if message["id"] > after_id]
            return rows if limit is None else rows[:limit]
    with _locked():
        records = _records()
        start = bisect_right(records, after_id, key=lambda message: message["id"])
        messages = records[start:] if limit is None else records[start:start + limit]
    return [dict(message) for message in messages]


def last_id():
    """Latest committed id; sequence metadata can lag the durable log."""
    with _locked():
        messages = _records()
        return messages[-1]["id"] if messages else 0


def read_recent(limit=20):
    """Compatibility name for the bounded tail reader."""
    return read_tail(limit)


def read_tail(limit=1000):
    """Read the newest ids without decoding unrelated older history."""
    _integer(limit, "limit")
    if limit == 0:
        return []
    with _locked():
        files = []
        for path in CHAT_DIR.glob("*.jsonl"):
            end = _file_tail(path, 1)
            if end:
                files.append((end[0]["id"], path))
        candidates, seen = [], set()
        for maximum, path in sorted(files, reverse=True):
            if len(candidates) >= limit and maximum < candidates[0]["id"]:
                break
            for message in _file_tail(path, limit):
                if message["id"] in seen:
                    raise ValueError(f"Invalid log record at {path.name}: duplicate message id")
                seen.add(message["id"])
                candidates.append(message)
            candidates.sort(key=lambda message: message["id"])
            candidates = candidates[-limit:]
        return candidates


def search_messages(query, limit=50, before_id=None, *, kind=None):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Search query must contain text")
    _integer(limit, "limit")
    if before_id is not None:
        _integer(before_id, "before_id")
    if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
        raise ValueError("Invalid search kind")
    if limit == 0:
        return []
    needle = query.casefold()
    with _locked():
        matches = []
        for message in reversed(_records()):
            if (before_id is not None and message["id"] >= before_id) or (kind is not None and message["kind"] != kind):
                continue
            if needle in message["text"].casefold():
                matches.append(dict(message))
                if len(matches) == limit:
                    break
        return matches[::-1]


def set_presence(agent, state, note="", *, ttl=900):
    _agent(agent)
    if state not in ("listening", "working", "idle"):
        raise ValueError("Presence state must be listening, working or idle")
    if not isinstance(note, str) or len(note) > 200:
        raise ValueError("Presence note must be a string of at most 200 characters")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("Presence lease must be finite and positive")
    _prepare()
    directory = DATA_DIR / "presence"
    directory.mkdir(exist_ok=True, mode=0o700)
    with _file_lock(directory / f"{agent}.lock"):
        now = time.time()
        previous = get_presence()[agent]
        since = previous["since"] if previous["state"] == state else now
        presence = {"state": state, "since": since, "updated_at": now,
                    "expires_at": now + ttl, "note": note}
        _atomic_json(directory / f"{agent}.json", presence)
        return presence


def get_presence():
    """Read atomic presence files without acquiring the log lock."""
    _prepare()
    result = {}
    now = time.time()
    for agent in AGENTS:
        unknown = {"state": "unknown", "since": None, "updated_at": None,
                   "expires_at": None, "note": ""}
        try:
            value = json.loads((DATA_DIR / "presence" / f"{agent}.json").read_text())
            valid = (isinstance(value, dict)
                     and value.get("state") in ("listening", "working", "idle")
                     and isinstance(value.get("note"), str) and len(value["note"]) <= 200
                     and all(type(value.get(key)) in (int, float) and math.isfinite(value[key])
                             for key in ("since", "updated_at", "expires_at"))
                     and value["since"] <= value["updated_at"] < value["expires_at"]
                     and value["updated_at"] <= now < value["expires_at"])
            result[agent] = value if valid else unknown
        except (FileNotFoundError, ValueError, UnicodeError):
            result[agent] = unknown
    return result


def guard_status():
    with _locked():
        return _guard_state(_records())[0]


def active_claims():
    with _locked():
        return _claims(_records())


def _agent(agent):
    if agent not in AGENTS:
        raise ValueError(f"Unknown agent: {agent}")


def _ack(agent):
    path = CURSOR_DIR / f"{agent}.json"
    if not path.exists():
        return 0
    try:
        return _integer(json.loads(path.read_text())["acked"], "acked id")
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f"Invalid acknowledgment file for {agent}: {path}") from error


def get_ack(agent):
    _agent(agent)
    with _locked():
        return _ack(agent)


def set_ack(agent, msg_id):
    _agent(agent)
    _integer(msg_id, "message id")
    with _locked(exclusive=True):
        messages = _records()
        last = messages[-1]["id"] if messages else 0
        if msg_id > last:
            raise ValueError(f"Cannot acknowledge {msg_id}; last message is {last}")
        if msg_id < _ack(agent):
            raise ValueError("Acknowledgments cannot move backwards")
        _atomic_json(CURSOR_DIR / f"{agent}.json", {"acked": msg_id})


def _inotify(directories=None, *, event_mask=0x00000FCE):
    """None means inotify is unavailable; use the portable polling fallback."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add = libc.inotify_add_watch
        add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add.restype = ctypes.c_int
        fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            return None
        # File content, close, creation, deletion and rename events; directory too.
        for directory in ((CHAT_DIR,) if directories is None else directories):
            if add(fd, os.fsencode(directory), event_mask) < 0:
                os.close(fd)
                return None
        return fd
    except (AttributeError, OSError):
        return None


def _signature():
    try:
        return tuple((p.name, s.st_ino, s.st_size, s.st_mtime_ns)
                     for p in sorted(CHAT_DIR.glob("*.jsonl")) for s in [p.stat()])
    except FileNotFoundError:
        return None


def _watch_lost(events):
    position = 0
    while position + 16 <= len(events):
        _, mask, _, length = struct.unpack_from("iIII", events, position)
        if mask & (0x400 | 0x800 | 0x8000):  # DELETE_SELF, MOVE_SELF, IGNORED
            return True
        position += 16 + length
    return False


def wait_for_change(after_id, timeout=25):
    _integer(after_id, "after_id")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite non-negative number")
    _prepare()
    # Install watch BEFORE checking for unread records; no read/watch gap.
    fd = _inotify()
    deadline = time.monotonic() + timeout
    try:
        # In polling mode, snapshot before reading for the same race protection.
        signature = _signature() if fd is None else None
        if read_messages(after_id, limit=1):
            return True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return bool(read_messages(after_id, limit=1))
            if fd is None:
                time.sleep(min(0.5, remaining))
                current = _signature()
                if current == signature:
                    continue
                signature = current
            else:
                try:
                    ready, _, _ = select.select([fd], [], [], remaining)
                except InterruptedError:
                    continue
                if not ready:
                    return bool(read_messages(after_id, limit=1))
                lost = False
                try:
                    while True:
                        events = os.read(fd, 65536)
                        if not events:
                            lost = True
                            break
                        lost = _watch_lost(events) or lost
                except BlockingIOError:
                    pass
                if lost:
                    os.close(fd)
                    fd = _inotify()
                    signature = _signature() if fd is None else None
            if read_messages(after_id, limit=1):
                return True
    finally:
        if fd is not None:
            os.close(fd)
