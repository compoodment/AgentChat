"""Same-session delivery, restart, rate-policy and native transport tests; no real model calls."""
import base64
import hashlib
import importlib
import json
import os
import select
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentchat import store, bridge
from agentchat.codex_client import CodexSession, MAX_MESSAGE, session_id

THREAD = '11111111-1111-4111-8111-111111111111'


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='agentchat-bridge-')
        self.env=mock.patch.dict(os.environ,AGENTCHAT_DATA=self.folder.name,CODEX_THREAD_ID=THREAD,AGENTCHAT_CODEX_SOCKET=str(Path(self.folder.name)/'absent.sock'));self.env.start();importlib.reload(store)
        bridge._update(enabled=True,thread=THREAD)
        self.now=1000.;self.calls=[];self.native=mock.Mock();self.native.status.return_value='idle'
        self.controller=bridge.Controller(self.native,enqueue=self.enqueue,clock=lambda:self.now)

    def tearDown(self):
        self.env.stop();self.folder.cleanup()

    def enqueue(self,thread,pending):
        self.calls.append((thread,dict(pending)));return ''

    def user(self,text='please respond'):
        return store.append_message('user',text)

    def test_silence_self_posts_and_control_records_never_wake_model(self):
        self.controller.tick();store.append_message('codex','self echo')
        store.append_message('codex','README.md','claim');store.append_message('codex','README.md','release')
        for _ in range(5):self.controller.tick()
        self.assertEqual(self.calls,[]);self.assertEqual(self.native.status.call_count,1)
        self.assertEqual(store.get_ack('codex'),0)

    def test_active_turn_prevents_queue_and_ack_clears_one_wake(self):
        message=self.user();self.native.status.return_value='active';self.controller.tick()
        self.assertEqual(self.calls,[])
        self.native.status.return_value='idle';self.controller.tick();self.controller.tick()
        self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][0],THREAD)
        store.set_ack('codex',message['id']);self.controller.tick()
        self.assertIsNone(bridge._state()['pending']);self.assertEqual(len(self.calls),1)

    def test_restart_and_failure_retry_keep_wake_id_and_unread_position(self):
        self.user();self.controller.enqueue=mock.Mock(side_effect=OSError('queue unavailable'))
        self.controller.tick();first=bridge._state()['pending']
        self.assertEqual(first['phase'],'prepared');self.assertEqual(store.get_ack('codex'),0)
        self.now+=31;self.native.status.return_value='active'
        restarted=bridge.Controller(self.native,enqueue=self.enqueue,clock=lambda:self.now)
        restarted.tick();self.assertEqual(self.calls,[])
        self.native.status.return_value='idle';restarted.tick()
        self.assertEqual(self.calls[0][1]['id'],first['id'])

    def test_queued_wake_is_not_repeated_while_native_turn_runs(self):
        self.user();self.controller.tick();self.now+=300
        self.native.status.return_value='active'
        for _ in range(10):self.controller.tick()
        self.assertEqual(len(self.calls),1)

    def test_missing_ack_retries_are_bounded_and_never_skip_messages(self):
        self.user()
        for _ in range(8):self.controller.tick();self.now+=31
        self.assertEqual(len(self.calls),3)
        self.assertEqual(bridge._state()['pending']['phase'],'failed')
        self.assertEqual(store.get_ack('codex'),0)
        self.assertEqual(len({call[1]['id'] for call in self.calls}),1)

    def test_pause_and_rate_limit_wait_without_waking_model(self):
        self.user();store.set_policy('paused');self.controller.tick()
        self.assertEqual(self.calls,[]);self.native.status.assert_not_called()
        store.resume_policy()
        for i in range(4):store.append_message('codex',str(i))
        self.controller.tick();self.assertEqual(self.calls,[])
        with mock.patch.object(store.time,'time',return_value=time.time()+61):self.controller.tick()
        self.assertEqual(len(self.calls),1)

    def test_probe_confirmation_cannot_be_resurrected_by_queue_return(self):
        token='a'*32;bridge._update(probe_requested=token)
        def immediate(thread,pending):bridge.confirm(pending['id']);return ''
        self.controller.enqueue=immediate;self.controller.tick()
        state=bridge._state();self.assertIsNone(state['pending'])
        self.assertEqual(state['probe_success']['id'],token)
        self.assertEqual(state['probe_success']['thread'],THREAD)

    def test_wrong_probe_confirmation_preserves_delivery(self):
        bridge._update(probe_requested='a'*32);self.controller.tick()
        with self.assertRaises(ValueError):bridge.confirm('b'*32)
        self.assertIsNotNone(bridge._state()['pending'])

    def test_resume_warning_is_visible_even_with_successful_queue(self):
        self.user();self.controller.enqueue=lambda *args:'resume failed'
        self.controller.tick();self.assertEqual(bridge._state()['error'],'resume failed')
        self.assertEqual(bridge._state()['pending']['phase'],'queued')

    def test_native_failure_does_not_enqueue_or_ack(self):
        self.user();self.native.status.side_effect=OSError('daemon down')
        with self.assertRaises(OSError):self.controller.tick()
        self.assertEqual(self.calls,[]);self.assertEqual(store.get_ack('codex'),0)

    def test_start_is_singleton_and_stop_never_signals_an_unrelated_pid(self):
        bridge._update(enabled=False);children=[];popen=subprocess.Popen
        def launch(*args,**kwargs):
            child=popen(*args,**kwargs);children.append(child);return child
        try:
            with mock.patch.object(bridge,'CodexSession',return_value=self.native),mock.patch.object(bridge.subprocess,'Popen',side_effect=launch):
                first=bridge.start(THREAD);again=bridge.start(THREAD)
            self.assertEqual(len(children),1);self.assertEqual(first['pid'],again['pid'])
            self.assertTrue(first['running']);self.assertFalse(bridge.stop()['running'])
        finally:
            bridge._update(enabled=False)
            for child in children:
                try:child.wait(timeout=3)
                except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=3)

    def test_new_user_message_recovers_failed_wake_without_skipping_old_inbox(self):
        first=self.user('first')
        for _ in range(4):self.controller.tick();self.now+=31
        old_id=bridge._state()['pending']['id']
        newer=self.user('please wake up');self.controller.tick()
        self.assertEqual(len(self.calls),4)
        self.assertNotEqual(self.calls[-1][1]['id'],old_id)
        self.assertEqual(self.calls[-1][1]['through'],newer['id'])
        self.assertEqual(store.get_ack('codex'),0)
        self.assertEqual([m['id'] for m in store.read_messages() if m['kind']=='msg'],[first['id'],newer['id']])

    def test_peer_updates_only_wake_on_target_mentions_or_replies_to_codex(self):
        store.set_policy('custom',limit=100,window_s=60,duration_s=3600)
        own=store.append_message('codex','request to peer')
        for text in ('FYI for user','name@agent.invalid','@codex-old'):
            store.append_message('claude',text)
        self.controller.tick();self.assertEqual(self.calls,[])
        directed=store.append_message('claude',f"↪ #{own['id']} done")
        self.controller.tick();self.assertEqual(self.calls[-1][1]['through'],directed['id'])
        self.assertTrue(bridge.relevant(dict(kind='msg',sender='claude',text='@CoDeX: please review')))
        self.assertTrue(bridge.relevant(dict(kind='msg',sender='claude',text='@all update')))

    def test_bursts_and_new_messages_during_active_turn_are_not_skipped(self):
        first=[self.user(str(i)) for i in range(3)];self.controller.tick()
        self.assertEqual(self.calls[0][1]['through'],first[-1]['id'])
        later=self.user('later');self.native.status.return_value='active'
        store.set_ack('codex',first[-1]['id']);self.controller.tick()
        self.assertEqual(len(self.calls),1)
        self.native.status.return_value='idle';self.controller.tick()
        self.assertEqual(len(self.calls),2);self.assertEqual(self.calls[-1][1]['through'],later['id'])
        self.assertEqual(store.get_ack('codex'),first[-1]['id'])

    def test_queue_command_preserves_thread_and_surfaces_resume_warning(self):
        pending=dict(kind='messages',id='a'*32,through=10)
        result=subprocess.CompletedProcess([],0,'queued','resume warning')
        with mock.patch.object(bridge.subprocess,'run',return_value=result) as run:
            self.assertEqual(bridge.queue_codex(THREAD,pending),'resume warning')
        command=run.call_args.args[0]
        self.assertEqual(command[:4],['codex','queue','--thread',THREAD])
        self.assertIn('--resume',command);self.assertNotIn('--model',command)
        self.assertNotIn('-m',command)
        with mock.patch.object(bridge.subprocess,'run',return_value=subprocess.CompletedProcess([],1,'','offline')):
            with self.assertRaisesRegex(OSError,'offline'):bridge.queue_codex(THREAD,pending)

    def test_other_session_cannot_confirm_probe_identity(self):
        bridge._update(probe_requested='a'*32);self.controller.tick()
        with mock.patch.dict(os.environ,CODEX_THREAD_ID='22222222-2222-4222-8222-222222222222'):
            with self.assertRaisesRegex(ValueError,'configured existing'):bridge.confirm('a'*32)
        self.assertIsNone(bridge._state()['probe_success'])

    def test_lock_reads_do_not_wake_control_watcher_but_real_changes_do(self):
        store.get_ack('codex');bridge.running();bridge._update(status='listening')
        watch=store._inotify((store.CHAT_DIR,store.CURSOR_DIR,store.DATA_DIR,bridge._directory()),event_mask=bridge.WATCH_MASK)
        if watch is None:self.skipTest('inotify unavailable')
        try:
            store.get_ack('codex');bridge.running();bridge._update(status='listening')
            self.assertFalse(select.select([watch],[],[],.1)[0], 'lock close created a self-wake')
            bridge._update(enabled=False)
            self.assertTrue(select.select([watch],[],[],.5)[0], 'real stop update did not wake watcher')
        finally:os.close(watch)

    def test_state_corruption_is_preserved_and_rejected(self):
        path=bridge._directory()/'codex.json';path.write_text('{')
        with self.assertRaises(ValueError):bridge.status()
        self.assertEqual(path.read_text(),'{')
        for value in ('other',THREAD.upper(),None):
            if value==THREAD:continue
            with self.assertRaises(ValueError):session_id(value)


class ProcessDeliveryTests(unittest.TestCase):
    """Real watcher + CLI + Unix transport + queue process, with isolated native/model boundary."""
    def setUp(self):
        from bridge_native_fixture import NativeFixture
        self.folder = tempfile.TemporaryDirectory(prefix='agentchat-delivery-')
        self.native = NativeFixture(self.folder.name, THREAD)
        self.env = mock.patch.dict(os.environ, AGENTCHAT_DATA=self.folder.name,
            CODEX_THREAD_ID=THREAD, AGENTCHAT_CODEX_SOCKET=str(self.native.path),
            FAKE_NATIVE_ROOT=self.folder.name, PATH=str(self.native.bin)+os.pathsep+os.environ['PATH'])
        self.env.start(); importlib.reload(store)
        self.children=[]
        self.popen=subprocess.Popen
        def launch(*args, **kwargs):
            child=self.popen(*args, **kwargs); self.children.append(child); return child
        self.launch=mock.patch.object(bridge.subprocess,'Popen',side_effect=launch)
        self.launch.start()

    def tearDown(self):
        try:
            bridge.stop()
            for child in self.children:
                try: child.wait(timeout=4)
                except subprocess.TimeoutExpired: child.terminate();child.wait(timeout=4)
        finally:
            self.launch.stop();self.native.close();self.env.stop();self.folder.cleanup()

    def until(self, predicate):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=predicate()
            if result:return result
            time.sleep(.025)
        self.fail('delivery condition timed out: '+json.dumps(bridge.status()))

    def cli(self, *args, code=0):
        result=subprocess.run([sys.executable,'-m','agentchat.cli',*args],
            cwd=store.CHECKOUT_ROOT,capture_output=True,text=True,timeout=8)
        self.assertEqual(result.returncode,code,result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_real_wake_inbox_ack_restart_and_new_arrival_during_active_turn(self):
        first=store.append_message('user','first request')
        started=bridge.start(THREAD)
        self.assertTrue(started['running'])
        self.until(lambda:len(self.native.queues())==1)
        queued=self.until(lambda:(bridge.status()['pending'] or {}).get('phase')=='queued')
        args=self.native.queues()[0]
        self.assertEqual(args[:3],['queue','--thread',THREAD])
        self.assertIn('--resume',args);self.assertNotIn('--model',args)
        self.assertIn('unix://'+str(self.native.path),args)
        prompt=args[args.index('--message')+1]
        self.assertIn('through at least #'+str(first['id']),prompt)
        self.assertEqual(store.get_ack('codex'),0)
        self.assertEqual(self.cli('wait','--as','codex','--timeout','0'),[first])
        later=store.append_message('user','arrived while the model is active')
        self.cli('ack','--as','codex',str(first['id']))
        self.until(lambda:bridge.status()['pending'] is None)
        self.assertEqual(len(self.native.queues()),1)
        bridge.stop()
        self.assertEqual(store.get_ack('codex'),first['id'])
        self.native.active.unlink()
        bridge.start(THREAD)
        self.until(lambda:len(self.native.queues())==2)
        pending=self.until(lambda:bridge.status()['pending'])
        self.assertEqual(pending['through'],later['id'])
        self.assertEqual(self.cli('wait','--as','codex','--timeout','0'),[later])
        self.cli('ack','--as','codex',str(later['id']))
        self.until(lambda:bridge.status()['pending'] is None)
        self.cli('wait','--as','codex','--timeout','0',code=2)
        self.assertEqual(len(self.native.queues()),2)
        self.assertEqual(self.native.errors,[])

    def test_process_restart_retains_unacknowledged_wake_identity(self):
        message=store.append_message('user','must survive a watcher restart')
        bridge.start(THREAD)
        self.until(lambda:len(self.native.queues())==1)
        self.until(lambda:(bridge.status()['pending'] or {}).get('phase')=='queued')
        original=bridge.status()['pending']['id']
        bridge.stop();bridge.start(THREAD)
        self.assertEqual(bridge.status()['pending']['id'],original)
        self.assertEqual(len(self.native.queues()),1)
        self.assertEqual(store.get_ack('codex'),0)
        self.native.active.unlink()
        self.cli('bridge','retry')
        self.until(lambda:len(self.native.queues())==2)
        self.assertIn(original,self.native.queues()[1][4])
        self.assertEqual(self.cli('wait','--as','codex','--timeout','0'),[message])
        self.cli('ack','--as','codex',str(message['id']))
        self.until(lambda:bridge.status()['pending'] is None)
        self.assertEqual(self.native.errors,[])

    def test_probe_through_real_queue_process_confirms_only_same_session(self):
        bridge.start(THREAD)
        probe=bridge.probe()['probe_requested']
        self.until(lambda:len(self.native.queues())==1)
        self.until(lambda:(bridge.status()['pending'] or {}).get('phase')=='queued')
        self.assertIn('bridge confirm '+probe,self.native.queues()[0][4])
        proof=self.cli('bridge','confirm',probe)[0]
        self.assertEqual(proof['thread'],THREAD)
        self.assertEqual(proof['id'],probe)
        self.assertIsNone(bridge.status()['pending'])
        self.assertEqual(store.get_ack('codex'),0)
        self.assertEqual(self.native.errors,[])


class TransportTests(unittest.TestCase):
    def test_real_unix_websocket_handshake_ping_fragmentation_and_thread_identity(self):
        with tempfile.TemporaryDirectory(prefix='agentchat-ws-') as directory:
            path=Path(directory)/'s';server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(path));server.listen(1)
            failures=[]
            def serve():
                try:
                    conn,_=server.accept();conn.settimeout(3)
                    with conn:
                        header=b''
                        while b'\r\n\r\n' not in header:header+=conn.recv(4096)
                        key=next(line.split(b':',1)[1].strip() for line in header.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
                        accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                        conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
                        def read(n):
                            value=b''
                            while len(value)<n:
                                chunk=conn.recv(n-len(value))
                                if not chunk:raise OSError('client closed')
                                value+=chunk
                            return value
                        def frame(data,opcode=129):
                            head=bytes((opcode,len(data))) if len(data)<126 else bytes((opcode,126))+struct.pack('!H',len(data))
                            conn.sendall(head+data)
                        seen=0
                        while seen<2:
                            first,length=read(2);count=length&127
                            if count==126:count=struct.unpack('!H',read(2))[0]
                            elif count==127:count=struct.unpack('!Q',read(8))[0]
                            mask=read(4);raw=read(count);raw=bytes(b^mask[i%4] for i,b in enumerate(raw))
                            if first&15==10:continue
                            request=json.loads(raw)
                            if 'id' not in request:continue
                            if request['method']=='initialize':result={}
                            else:
                                self.assertEqual(request['params']['threadId'],THREAD)
                                self.assertFalse(request['params']['includeTurns'])
                                result=dict(thread=dict(id=THREAD,status=dict(type=('active','idle')[seen]),preview='日本語'))
                                seen+=1
                            data=json.dumps(dict(id=request['id'],result=result),ensure_ascii=False).encode()
                            if request['method']=='initialize':frame(b'ping',137)
                            frame(data[:15],1);frame(data[15:],128)
                except BaseException as error:failures.append(error)
            thread=threading.Thread(target=serve,daemon=True);thread.start();client=CodexSession(THREAD,socket_path=path)
            try:self.assertEqual(client.status(),'active');self.assertEqual(client.status(),'idle')
            finally:client.close();thread.join(4);server.close()
            self.assertFalse(thread.is_alive());self.assertEqual(failures,[])

    def test_oversized_frame_is_rejected_before_allocating_payload(self):
        left,right=socket.socketpair();client=CodexSession(THREAD);client.sock=left
        try:
            right.sendall(bytes((129,127))+struct.pack('!Q',MAX_MESSAGE+1))
            with self.assertRaisesRegex(OSError,'limit'):client._receive(time.monotonic()+1)
        finally:client.close();right.close()


if __name__=='__main__':unittest.main()
