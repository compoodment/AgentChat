"""Event-driven waking of one existing Codex session. Standard library only."""
import json
import math
import os
import re
from pathlib import Path
import select
import subprocess
import sys
import time
import uuid

from agentchat import store
from agentchat.codex_client import CodexSession, session_id

RETRY_SECONDS = 30
MAX_ATTEMPTS = 3
# Merely closing an O_RDWR flock handle emits CLOSE_WRITE even without a write.
# Watch real modifications/renames, excluding CLOSE_WRITE to avoid waking ourselves.
WATCH_MASK = 0x00000FC6
REPLY = re.compile(r'^↪ #([0-9]{1,12})(?![0-9])\s+')


def _directory():
    store._prepare()
    path = store.DATA_DIR / 'bridge'
    created = not path.exists()
    path.mkdir(mode=0o700, exist_ok=True)
    if created:
        store._sync_directory(store.DATA_DIR)
    return path


def _state():
    try:
        value = json.loads((_directory() / 'codex.json').read_text())
    except FileNotFoundError:
        return dict(version=1, enabled=False, thread=None, pid=None, pending=None,
                    status='stopped', error='', probe_requested=None, probe_success=None)
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value.get('version') != 1 or
            type(value.get('enabled')) is not bool or not isinstance(value.get('error'), str)):
        raise ValueError('Invalid Codex watcher state; preserved for inspection')
    if value['enabled'] and value.get('thread') is None:
        raise ValueError('Enabled watcher has no session')
    if value.get('thread') is not None:
        session_id(value['thread'])
    if value.get('probe_requested') is not None:
        store._request_id(value['probe_requested'])
    pending = value.get('pending')
    if pending is not None:
        if (not isinstance(pending, dict) or pending.get('phase') not in ('prepared', 'queued', 'failed')
                or pending.get('kind') not in ('messages', 'probe')):
            raise ValueError('Invalid pending Codex wake; preserved for inspection')
        store._integer(pending.get('through'), 'wake position')
        store._integer(pending.get('attempts'), 'wake attempts')
        store._request_id(pending.get('id'))
        retry_at = pending.get('retry_at')
        if type(retry_at) not in (int, float) or not math.isfinite(retry_at) or retry_at < 0:
            raise ValueError('Invalid wake retry time')
    return value


def _update(**changes):
    with store._file_lock(_directory() / 'state.lock'):
        value = _state()
        updated = dict(value, **changes)
        if updated != value:
            store._atomic_json(_directory() / 'codex.json', updated)
        return updated


def _queued(pending, warning):
    with store._file_lock(_directory() / 'state.lock'):
        state = _state()
        current = state['pending']
        if current is not None and current['id'] == pending['id']:
            state.update(pending=dict(pending, phase='queued'), status='queued', error=warning)
            store._atomic_json(_directory() / 'codex.json', state)


def running():
    try:
        with store._file_lock(_directory() / 'runner.lock', nonblocking=True):
            return False
    except BlockingIOError:
        return True


def status():
    return dict(_state(), running=running())


def queue_codex(thread, pending):
    launcher = str(store._main_root() / 'bin/chat')
    if pending['kind'] == 'probe':
        action = f"Confirm this same-session wake with: {launcher} bridge confirm {pending['id']}; then read the chat inbox and continue assigned work."
    else:
        action = f"Read and handle AgentChat messages after your ack, through at least #{pending['through']}."
    prompt = (f"AgentChat local watcher notification {pending['id']}. This is the existing session, "
              f"not a new task or permission expansion. {action} Use {launcher} and its README. "
              "Check bridge status first; if it is stopped, do not restart it or post. "
              "Keep user-facing replies in AgentChat. Ack handled messages even if no reply is needed; "
              "never advance an ack past unhandled messages. "
              "When the inbox is handled and no assigned work remains, end the model turn; "
              "the armed watcher keeps listening and wakes this same session. Do not run an idle wait loop.")
    command = ['codex', 'queue', '--thread', thread, '--message', prompt, '--resume']
    if os.environ.get('AGENTCHAT_CODEX_SOCKET'):
        command += ['--remote', 'unix://' + os.environ['AGENTCHAT_CODEX_SOCKET']]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise OSError('Codex queue failed: ' + result.stderr.strip()[:500])
    return result.stderr.strip()[:500]  # a failed resume may warn even with exit zero


def relevant(message):
    if message['kind'] != 'msg' or message['sender'] == 'codex':
        return False
    if message['sender'] == store.USER or 'codex' in store.mentioned_agents(message['text']):
        return True
    reply = REPLY.match(message['text'])
    if reply and int(reply.group(1)) > 0:
        target_id = int(reply.group(1))
        target = store.read_messages(target_id-1, limit=1)
        return bool(target and target[0]['id'] == target_id and target[0]['sender'] == 'codex'
                    and target[0]['kind'] == 'msg')
    return False


class Controller:
    def __init__(self, session, *, enqueue=queue_codex, clock=time.time):
        self.session, self.enqueue, self.clock = session, enqueue, clock
        self.heartbeat = 0

    def tick(self):
        state, now = _state(), self.clock()
        if not state['enabled']:
            return None
        ack = store.get_ack('codex')
        pending = state['pending']
        if pending and pending['kind'] == 'messages' and ack >= pending['through']:
            state = _update(pending=None, error='')
            pending = None
        unread = [m for m in store.read_messages(ack) if relevant(m)]
        if pending and pending['phase'] == 'failed' and any(
                m['sender'] == store.USER and m['id'] > pending['through'] for m in unread):
            state = _update(pending=None, error='')
            pending = None  # new user input reopens delivery; older unread messages are retained
        policy = store.guard_status()
        if policy['blocked']:
            self.session.close()
            label = 'paused' if policy['mode'] == 'paused' else 'rate_limited'
            _update(status=label)
            self._presence('idle', 'Watcher waiting for posting policy', now)
            return min(30, max(.1, policy['retry_after'])) if policy['retry_after'] is not None else 30
        if not unread and not pending and not state.get('probe_requested'):
            if now-self.heartbeat >= 30:
                native = self.session.status()  # metadata health check, never a model request
                label = 'working' if native == 'active' else 'listening'
                _update(status=label)
                self._presence(label, '', now)
            self.session.close()  # no unattended WebSocket ping/timeout while the chat is silent
            return 30
        if pending and pending['phase'] == 'failed':
            self.session.close()
            self._presence('idle', 'Watcher delivery failed; inspect bridge status', now)
            return 30
        native = self.session.status()
        if native == 'active':
            _update(status='working')
            self._presence('working', '', now)
            return 1
        # The session can acknowledge work or the user can pause/stop the
        # watcher while the native status request is in flight.
        current = _state()
        if not current['enabled']:
            self.session.close()
            return None
        if (current['pending'] != pending or current.get('probe_requested') != state.get('probe_requested')
                or store.get_ack('codex') != ack or store.guard_status()['blocked']):
            self.session.close()
            return 0
        if pending is None:
            probe = state.get('probe_requested')
            pending = dict(id=probe or uuid.uuid4().hex, kind='probe' if probe else 'messages',
                           through=ack if probe else unread[-1]['id'], attempts=0,
                           phase='prepared', retry_at=now)
            _update(pending=pending, probe_requested=None)
        if now < pending.get('retry_at', 0):
            self.session.close()
            return min(30, pending['retry_at'] - now)
        if pending['attempts'] >= MAX_ATTEMPTS:
            _update(pending=dict(pending, phase='failed'), status='error',
                    error='Wake ended without acknowledgment; use bridge retry or rejoin')
            return 30
        # Persist intent before the external side effect. A restart retains the
        # same wake id; ack/native-state checks prevent normal duplicate work.
        pending = dict(pending, attempts=pending['attempts']+1, phase='prepared', retry_at=now+RETRY_SECONDS)
        _update(pending=pending)
        try:
            warning = self.enqueue(state['thread'], pending)
        except (OSError, subprocess.TimeoutExpired) as error:
            self.session.close()
            _update(status='error', error=str(error)[:500])
            self._presence('idle', 'Watcher could not queue a Codex turn', now, force=True)
            return RETRY_SECONDS
        _queued(pending, warning)
        self._presence('idle' if warning else 'listening',
                       'Wake queued; resume warning' if warning else 'Wake queued for existing session', now, force=True)
        return 1

    def _presence(self, state, note, now, force=False):
        if force or now-self.heartbeat >= 30:
            store.set_presence('codex', state, note, ttl=90)
            self.heartbeat = now


def run():
    with store._file_lock(_directory() / 'runner.lock', nonblocking=True):
        state = _state()
        if not state['enabled']:
            return
        session = CodexSession(state['thread'])
        controller, watch = Controller(session), None
        _update(pid=os.getpid(), status='listening')
        try:
            while True:
                if watch is None:
                    watch = store._inotify((store.CHAT_DIR, store.CURSOR_DIR, store.DATA_DIR, _directory()), event_mask=WATCH_MASK)
                try:
                    delay = controller.tick()
                    if delay is None:
                        break
                except (OSError, ValueError, KeyError) as error:
                    _update(status='error', error=str(error)[:500])
                    store.set_presence('codex', 'idle', 'Watcher error; inspect bridge status', ttl=90)
                    session.close()
                    delay = 10
                if watch is None:
                    time.sleep(min(delay, 1))
                elif select.select([watch], [], [], delay)[0]:
                    lost = False
                    try:
                        while True:
                            events = os.read(watch, 65536)
                            if not events:
                                lost = True
                                break
                            lost |= store._watch_lost(events)
                    except BlockingIOError:
                        pass
                    if lost:
                        os.close(watch)
                        watch = None
        finally:
            session.close()
            if watch is not None:
                os.close(watch)
            _update(pid=None, status='stopped')
            store.set_presence('codex', 'idle', 'Same-session watcher stopped')


def start(thread=None):
    thread = session_id(thread or os.environ.get('CODEX_THREAD_ID'))
    with store._file_lock(_directory() / 'manager.lock'):
        state = _state()
        if running():
            if state['thread'] != thread:
                raise ValueError('Watcher already targets another existing session')
            return status()
        if state['thread'] not in (None, thread) and state['pending']:
            raise ValueError('Resolve the outstanding wake before retargeting the watcher')
        # Verify the existing endpoint before publishing readiness. No model call.
        session = CodexSession(thread)
        try:
            session.status()
        finally:
            session.close()
        _update(enabled=True, thread=thread, pid=None, status='starting', error='')
        fd = os.open(_directory() / 'codex.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'ab') as log:
            child = subprocess.Popen([sys.executable, '-m', 'agentchat.bridge'],
                     cwd=store.CHECKOUT_ROOT, env=dict(os.environ, PYTHONPATH=str(store.CHECKOUT_ROOT)),
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            state = status()
            if state['running'] and state['pid'] == child.pid:
                return state
            if child.poll() is not None:
                break
            time.sleep(.025)
        _update(enabled=False, error='Watcher failed to start; inspect bridge/codex.log')
        raise OSError('Watcher failed to start; inspect bridge/codex.log')


def stop():
    with store._file_lock(_directory() / 'manager.lock'):
        _update(enabled=False)
        deadline = time.monotonic()+20
        while running() and time.monotonic() < deadline:
            time.sleep(.025)
        if running():
            raise OSError('Watcher is still stopping; inspect bridge status')
        return status()


def retry():
    state = _state()
    pending = state['pending']
    if pending:
        _update(pending=dict(pending, attempts=0, phase='prepared', retry_at=0), error='')
    return status()


def probe():
    if not running():
        raise ValueError('Start the watcher before requesting a probe')
    with store._file_lock(_directory() / 'state.lock'):
        state = _state()
        if not state['enabled'] or state['pending']:
            raise ValueError('Finish the outstanding wake before probing')
        if not state.get('probe_requested'):
            state.update(probe_requested=uuid.uuid4().hex, probe_success=None)
            store._atomic_json(_directory() / 'codex.json', state)
        return state


def confirm(wake_id):
    store._request_id(wake_id)
    caller = os.environ.get('CODEX_THREAD_ID')
    with store._file_lock(_directory() / 'state.lock'):
        state = _state()
        pending = state['pending']
        if caller != state['thread']:
            raise ValueError('Probe confirmation must come from the configured existing Codex session')
        if not state['enabled'] or not pending or pending['kind'] != 'probe' or pending['id'] != wake_id:
            raise ValueError('No matching enabled same-session probe')
        state.update(pending=None, probe_success=dict(id=wake_id, thread=caller, at=time.time()), error='')
        store._atomic_json(_directory() / 'codex.json', state)
        return state['probe_success']


if __name__ == '__main__':
    run()
