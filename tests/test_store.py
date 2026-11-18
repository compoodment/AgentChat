"""Store regression tests. Every test uses a throwaway data root."""
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from agentchat import store


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='agentchat-test-')
        self.environment = mock.patch.dict(os.environ, AGENTCHAT_DATA=self.directory.name)
        self.environment.start()
        importlib.reload(store)

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def user(self, text='hello'):
        return store.append_message('user', text)

class StoreTests(StoreFixture):
    def test_guard_four_and_once_per_episode(self):
        store.set_policy('wait')
        self.user()
        for index in range(4):
            store.append_message(store.AGENTS[index % 2], str(index))
        for _ in range(3):
            with self.assertRaises(store.GuardBlocked):
                store.append_message('codex', 'blocked')
        notices = [m for m in store.read_messages() if m['text'].startswith(store._GUARD_NOTICE)]
        self.assertEqual(len(notices), 1)
        self.assertTrue(store.guard_status()['blocked'])
        self.user('reset')
        self.assertEqual(store.guard_status()['agent_streak'], 0)

    def test_cache_unchanged_and_independent_results(self):
        msg = self.user()
        with mock.patch.object(store, '_read_file', wraps=store._read_file) as read:
            result = store.read_messages()
            result[0]['text'] = 'changed outside'
            store.read_messages()
            self.assertEqual(read.call_count, 1)
        self.assertEqual(store.read_messages()[0], msg)

    def test_append_reads_only_new_bytes(self):
        self.user()
        store.read_messages()
        self.user('next')
        with mock.patch.object(store, '_read_file', wraps=store._read_file) as read:
            self.assertEqual(len(store.read_messages()), 2)
            self.assertGreater(read.call_args.args[2], 0)

    def test_replacement_truncation_same_size_and_removal(self):
        self.user('aaaa')
        old = store.read_messages()[0]
        path = next(store.CHAT_DIR.glob('*.jsonl'))
        text = path.read_text().replace('aaaa', 'bbbb')
        path.write_text(text)
        self.assertEqual(store.read_messages()[0]['text'], 'bbbb')
        replacement = path.with_suffix('.new')
        replacement.write_text(text.replace('bbbb', 'cccc'))
        replacement.replace(path)
        self.assertEqual(store.read_messages()[0]['text'], 'cccc')
        path.write_text('')
        self.assertEqual(store.last_id(), 0)
        path.unlink()
        self.assertEqual(store.read_messages(), [])

    def test_partial_tail_recovery(self):
        msg = self.user()
        path = next(store.CHAT_DIR.glob('*.jsonl'))
        with path.open('ab') as f:
            f.write(b'{"id":999')
        self.assertEqual(store.read_messages(), [msg])
        self.user('after recovery')
        self.assertEqual(len(store.read_messages()), 2)
        partials = list((store.DATA_DIR/'recovery').glob('*.partial'))
        self.assertEqual(partials[0].read_bytes(), b'{"id":999')

    def test_completed_partial_line_becomes_visible(self):
        self.user()
        path = next(store.CHAT_DIR.glob('*.jsonl'))
        m = {'id':2, 'ts':'2026-11-16', 'sender':'user', 'kind':'msg', 'text':'next'}
        data = json.dumps(m).encode()
        with path.open('ab') as f:f.write(data[:10])
        self.assertEqual(store.last_id(),1)
        with path.open('ab') as f:f.write(data[10:]+b'\n')
        self.assertEqual(store.last_id(),2)

    def test_duplicate_and_invalid_committed_record_rejected(self):
        self.user()
        path = next(store.CHAT_DIR.glob('*.jsonl'))
        with path.open('ab') as f:f.write(path.read_bytes())
        with self.assertRaisesRegex(ValueError,'duplicate'):store.read_messages()
        path.write_bytes(b'not json\n')
        with self.assertRaisesRegex(ValueError,'Invalid log'):store.read_messages()

    def test_ack_bounds_and_monotonicity(self):
        first=self.user()['id'];second=self.user()['id']
        store.set_ack('codex',second)
        with self.assertRaises(ValueError):store.set_ack('codex',first)
        with self.assertRaises(ValueError):store.set_ack('codex',second+1)
        self.assertEqual(store.get_ack('codex'),second)

    def test_concurrent_process_writers(self):
        code = 'from agentchat import store; [store.append_message("user", str(i)) for i in range(15)]'
        env=dict(os.environ, PYTHONPATH=str(Path(store.__file__).resolve().parents[1]))
        processes=[subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL) for _ in range(4)]
        for process in processes:self.assertEqual(process.wait(timeout=20),0)
        ids=[m['id'] for m in store.read_messages()]
        self.assertEqual(ids,list(range(1,61)))

    def test_quoted_and_partial_mentions_do_not_assign_a_task(self):
        examples = ['what does `@claude` mean?', '``@claude`` is code',
                    '```text\n@claude\n```', '~~~\n@claude\n~~~~',
                    '> @claude old quotation', '    @claude code',
                    'mail@claude.example', '@claude-old', r'\@claude literal']
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(store.mentioned_agents(text), set())
                request = store.append_message('user', text)
                claim = store.take_task('codex', request['id'])
                self.assertEqual(claim['sender'], 'codex')
                store.release_task('codex', request['id'])
        self.assertEqual(store.mentioned_agents('@ClAuDe please act'), {'claude'})
        self.assertEqual(store.mentioned_agents('@all please act'), {'claude', 'codex'})
        self.assertEqual(store.mentioned_agents('`@claude` example; @codex act'), {'codex'})
        self.assertEqual(store.mentioned_agents('```\n@codex unclosed'), set())

    def test_inline_code_uses_whole_backtick_runs(self):
        self.assertEqual(store.mentioned_agents('prefix `` @codex ` text ``'), set())
        self.assertEqual(store.mentioned_agents('@co`example`dex'), set())
        # One long unmatched run must remain prose without regex backtracking.
        self.assertEqual(store.mentioned_agents('prefix '+ '`'*16000 + ' @codex'), {'codex'})
        self.assertEqual(store.mentioned_agents('prefix ` example @claude'), {'claude'})

    def test_task_race_and_assignment(self):
        task=self.user('shared request')['id']
        def take(agent):
            try:store.take_task(agent,task);return True
            except store.ClaimConflict:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            wins=list(pool.map(take,store.AGENTS))
        self.assertEqual(sum(wins),1)
        targeted=self.user('@CoDeX only')['id']
        with self.assertRaises(store.ClaimConflict):store.take_task('claude',targeted)
        store.take_task('codex',targeted)

    def test_claim_overlap_release_and_escape(self):
        store.append_message('codex','agentchat','claim')
        with self.assertRaises(store.ClaimConflict):store.append_message('claude','agentchat/ui.py','claim')
        with self.assertRaises(store.ClaimConflict):store.append_message('claude','agentchat','release')
        with self.assertRaises(ValueError):store.append_message('codex','../outside','claim')
        store.append_message('codex','agentchat','release')
        self.assertEqual(store.active_claims(),{})

    def test_wait_arrival_and_timeout(self):
        self.assertFalse(store.wait_for_change(0,0.01))
        thread=threading.Thread(target=lambda:(time.sleep(.05),self.user()))
        thread.start()
        try:self.assertTrue(store.wait_for_change(0,2))
        finally:thread.join()
        self.assertTrue(store.wait_for_change(0,0))

    def test_poll_fallback(self):
        with mock.patch.object(store,'_inotify',return_value=None):
            thread=threading.Thread(target=lambda:(time.sleep(.05),self.user()))
            thread.start()
            try:self.assertTrue(store.wait_for_change(0,2))
            finally:thread.join()

    def test_presence_expiry_and_continuity(self):
        with mock.patch.object(store.time,'time',return_value=100):
            self.assertEqual(store.get_presence()['codex']['state'],'unknown')
            first=store.set_presence('codex','working',ttl=10)
        with mock.patch.object(store.time,'time',return_value=105):
            second=store.set_presence('codex','working',ttl=10)
            self.assertEqual(first['since'],second['since'])
        with mock.patch.object(store.time,'time',return_value=116):
            self.assertEqual(store.get_presence()['codex']['state'],'unknown')
            third=store.set_presence('codex','working',ttl=10)
            self.assertEqual(third['since'],116)
        self.assertEqual(store.last_id(),0)

    def test_presence_does_not_take_log_lock(self):
        store.set_presence('claude','listening')
        with mock.patch.object(store,'_locked',side_effect=AssertionError('log lock used')):
            self.assertEqual(store.get_presence()['claude']['state'],'listening')
            store.set_presence('codex','idle')

    def test_presence_malformed_and_validation(self):
        store.set_presence('codex','idle')
        path=store.DATA_DIR/'presence/codex.json'
        for value in ['{', 'null', '{"state":"working"}', '[]']:
            path.write_text(value)
            self.assertEqual(store.get_presence()['codex']['state'],'unknown')
        for ttl in [0,-1,float('nan'),float('inf'),True]:
            with self.assertRaises(ValueError):store.set_presence('codex','working',ttl=ttl)
        with self.assertRaises(ValueError):store.set_presence('codex','working','x'*201)

    def test_repeated_unacked_wait_preserves_working_since(self):
        self.user()
        launcher = Path(store.__file__).resolve().parents[1] / 'bin/chat'
        def wait():
            subprocess.run([str(launcher), 'wait', '--as', 'codex', '--timeout', '0.05'],
                           capture_output=True, text=True, check=True, timeout=10)
            return store.get_presence()['codex']
        first = wait()
        time.sleep(0.02)
        second = wait()
        self.assertEqual((first['state'], second['state']), ('working', 'working'))
        self.assertEqual(first['since'], second['since'])
        self.assertEqual(store.get_ack('codex'), 0)

    def test_compact_wait_preserves_every_record_text_order_and_ack(self):
        records = [store.append_message('user', 'hello\n"quotes" · violet', request_id='a'*32),
                   store.append_message('codex', 'README.md', 'claim'),
                   store.append_message('codex', 'README.md', 'release')]
        launcher = Path(store.__file__).resolve().parents[1] / 'bin/chat'
        command = [str(launcher), 'wait', '--as', 'codex', '--timeout', '0']
        full = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        compact = subprocess.run(command + ['--compact'], capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual([json.loads(line) for line in full.stdout.splitlines()], records)
        expected = [[row[key] for key in ('id', 'sender', 'kind', 'text')] for row in records]
        self.assertEqual([json.loads(line) for line in compact.stdout.splitlines()], expected)
        self.assertLess(len(compact.stdout.encode()), len(full.stdout.encode()))
        self.assertEqual(store.get_ack('codex'), 0)
        store.set_ack('codex', records[-1]['id'])
        empty = subprocess.run(command + ['--compact'], capture_output=True, text=True, timeout=10)
        self.assertEqual((empty.returncode, empty.stdout), (2, ''))

    def test_nonblocking_cli_wait_does_not_overwrite_watcher_presence(self):
        launcher = Path(store.__file__).resolve().parents[1] / 'bin/chat'
        lease = store.set_presence('codex', 'working', 'active turn')
        command = [str(launcher), 'wait', '--as', 'codex', '--timeout', '0', '--compact']
        empty = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual((empty.returncode, empty.stdout), (2, ''))
        self.assertEqual(store.get_presence()['codex'], lease)
        self.user()
        unread = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(unread.stdout)[:3], [1, 'user', 'msg'])
        self.assertEqual(store.get_presence()['codex'], lease)
        self.assertEqual(store.get_ack('codex'), 0)

    def test_cli_version_matches_release_file_without_creating_runtime_data(self):
        root=Path(store.__file__).resolve().parents[1]
        target=Path(self.directory.name)/'unused-version-data'
        result=subprocess.run([str(root/'bin/chat'),'--version'],cwd=self.directory.name,
                    env=dict(os.environ,AGENTCHAT_DATA=str(target)),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        expected=(root/'VERSION').read_text(encoding='ascii').strip()
        self.assertEqual(result.stdout,f'AgentChat {expected}\n')
        self.assertFalse(target.exists())

    def test_cli_wait_lease_and_exit_codes(self):
        launcher=Path(store.__file__).resolve().parents[1]/'bin/chat'
        result=subprocess.run([str(launcher),'wait','--as','codex','--timeout','0.05'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(store.get_presence()['codex']['state'],'listening')
        self.user()
        result=subprocess.run([str(launcher),'wait','--as','codex','--timeout','0.05'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(store.get_presence()['codex']['state'],'working')
        self.assertEqual(store.get_ack('codex'),0)

    def test_autonomy_rate_window_and_user_does_not_reset(self):
        with mock.patch.object(store.time,'time',return_value=time.time()):
            store.set_autonomy(3600)
        for index in range(store.RATE_LIMIT):store.append_message(store.AGENTS[index % 2],str(index))
        status=store.guard_status()
        self.assertEqual(status['mode'],'custom')
        self.assertTrue(status['blocked'])
        self.assertGreater(status['retry_after'],590)
        self.user('user still allowed')
        for _ in range(2):
            with self.assertRaisesRegex(store.GuardBlocked,'retry after'):
                store.append_message('codex','blocked')
        notices=[m for m in store.read_messages() if m['text'].startswith(store._RATE_NOTICE)]
        self.assertEqual(len(notices),1)
        with mock.patch.object(store.time,'time',return_value=time.time()+601):
            self.assertFalse(store.guard_status()['blocked'])
            store.append_message('codex','resumed without user')

    def test_autonomy_expiry_disable_and_malformed(self):
        start=time.time()
        store.set_autonomy(30)
        for i in range(4):store.append_message('codex',str(i))
        with mock.patch.object(store.time,'time',return_value=start+31):
            self.assertEqual(store.guard_status()['mode'],'normal')
            self.assertTrue(store.guard_status()['blocked'])
        store.set_autonomy(100)
        store.set_autonomy(0)
        self.assertEqual(store.guard_status()['mode'],'normal')
        for text in ['null','[]','{','{"expires_at":"forever"}']:
            (store.DATA_DIR/'policy.json').write_text(text)
            self.assertEqual(store.guard_status()['mode'],'wait')
        for value in [-1,86401,True,float('inf'),float('nan')]:
            with self.assertRaises(ValueError):store.set_autonomy(value)

    def test_watch_rearms_after_chat_directory_replacement(self):
        installed=threading.Event()
        original=store._inotify
        def watch():
            fd=original()
            installed.set()
            return fd
        def producer():
            installed.wait(2)
            store.CHAT_DIR.rename(store.DATA_DIR/'old-chat')
            store.CHAT_DIR.mkdir(exist_ok=True)
            time.sleep(.15)
            self.user('new directory')
        thread=threading.Thread(target=producer)
        thread.start()
        started=time.monotonic()
        try:
            with mock.patch.object(store,'_inotify',side_effect=watch):
                self.assertTrue(store.wait_for_change(0,3))
        finally:thread.join()
        self.assertLess(time.monotonic()-started,2,'watch stayed on the renamed directory')

    def test_malformed_ack_is_an_actionable_error(self):
        store.get_ack('codex')
        for text in ['null','[]','{','{}','{"acked":true}','{"acked":-1}']:
            (store.CURSOR_DIR/'codex.json').write_text(text)
            with self.assertRaisesRegex(ValueError,'Invalid acknowledgment file for codex'):
                store.get_ack('codex')

    def test_human_task_claim_has_clear_error(self):
        task=self.user()['id']
        with self.assertRaisesRegex(ValueError,'Task ownership is for agents'):
            store.append_message('user',f'task:{task}','claim')

    def test_derived_state_only_parses_new_agent_timestamps(self):
        self.user()
        store.append_message('codex','one')
        store.guard_status()
        with mock.patch.object(store,'_timestamp',wraps=store._timestamp) as stamp:
            store.guard_status()
            self.assertEqual(stamp.call_count,0)
            store.append_message('claude','two')
            self.assertEqual(store.guard_status()['agent_streak'],2)
            store.active_claims()
            self.assertEqual(stamp.call_count,1)

    def test_derived_claims_rebuild_on_replacement(self):
        store.append_message('codex','README.md','claim')
        claims=store.active_claims()
        claims.clear()
        self.assertEqual(store.active_claims(),{'README.md':'codex'})
        path=next(store.CHAT_DIR.glob('*.jsonl'))
        replacement=path.with_suffix('.new')
        replacement.write_text('')
        replacement.replace(path)
        self.assertEqual(store.active_claims(),{})
        self.assertEqual(store.guard_status()['agent_streak'],0)

    def test_committed_log_survives_sequence_metadata_failure(self):
        original=store._atomic_write
        def fail_sequence(path,data):
            if path.name=='seq':raise OSError('simulated interruption after log commit')
            original(path,data)
        with mock.patch.object(store,'_atomic_write',side_effect=fail_sequence):
            committed=self.user('committed despite metadata failure')
        self.assertEqual(committed['id'],1)
        self.assertEqual(store.read_messages(),[committed])
        self.assertEqual(store.last_id(),1)
        self.assertEqual(self.user('next')['id'],2)

    def test_log_durability_failure_still_propagates(self):
        original=store._sync_directory
        def fail_log_directory(path):
            if path==store.CHAT_DIR:raise OSError('simulated log-directory sync failure')
            original(path)
        with mock.patch.object(store,'_sync_directory',side_effect=fail_log_directory):
            with self.assertRaisesRegex(OSError,'log-directory sync failure'):
                self.user('durability not confirmed')

    def test_failed_recovery_save_never_truncates_original_tail(self):
        self.user()
        path=next(store.CHAT_DIR.glob('*.jsonl'))
        with path.open('ab') as stream:stream.write(b'partial suffix')
        original=path.read_bytes()
        with mock.patch.object(store,'_atomic_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.user('cannot recover yet')
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(self.user('retry after space restored')['id'],2)

    def test_dead_writer_releases_advisory_lock(self):
        store.get_ack('codex')
        code='from agentchat import store; import time; ctx=store._locked(exclusive=True); ctx.__enter__(); print("locked",flush=True); time.sleep(20)'
        env=dict(os.environ,PYTHONPATH=str(Path(store.__file__).resolve().parents[1]))
        process=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'locked')
            process.kill()
            process.wait(timeout=3)
            self.assertEqual(self.user('after dead writer')['id'],1)
        finally:
            if process.poll() is None:process.kill();process.wait(timeout=3)
            process.stdout.close()

    def test_history_inspection_is_bounded_and_does_not_ack(self):
        for number in range(5):self.user('message '+str(number))
        recent=store.read_recent(2)
        self.assertEqual([m['id'] for m in recent],[4,5])
        recent[0]['text']='external mutation'
        self.assertEqual(store.read_recent(2)[0]['text'],'message 3')
        self.assertEqual(store.read_recent(0),[])
        launcher=Path(store.__file__).resolve().parents[1]/'bin/chat'
        for args,expected in [(['log','--last','2'],[4,5]),(['show','2'],[2])]:
            result=subprocess.run([str(launcher),*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual([json.loads(line)['id'] for line in result.stdout.splitlines()],expected)
            self.assertEqual(store.get_ack('codex'),0)
            self.assertEqual(store.get_presence()['codex']['state'],'unknown')
        for args in [['show','999'],['show','0'],['log','--last','1001'],['log','--last','-1']]:
            result=subprocess.run([str(launcher),*args],capture_output=True,text=True)
            self.assertEqual(result.returncode,1)
            self.assertTrue(result.stderr.startswith('chat: '))

    def test_prepare_retries_directory_replacement_race(self):
        original=Path.mkdir
        raced=[]
        def mkdir(path,*args,**kwargs):
            if path==store.CHAT_DIR and not raced:
                raced.append(True)
                raise FileExistsError('simulated mkdir/is_dir replacement race')
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'mkdir',new=mkdir):store._prepare()
        self.assertTrue(store.CHAT_DIR.is_dir())
        self.assertTrue(raced)

    def test_prepare_refuses_real_file_collision(self):
        store.CHAT_DIR.write_text('not a directory')
        with self.assertRaises(FileExistsError):store._prepare()
        self.assertEqual(store.CHAT_DIR.read_text(),'not a directory')

    def fixture_log(self,name,ids):
        store._prepare()
        path=store.CHAT_DIR/name
        with path.open('w') as stream:
            for number in ids:
                stream.write(json.dumps({'id':number,'ts':'2026-11-16T10:00:00+01:00',
                                         'sender':'user','kind':'msg','text':'fixture '+str(number)})+'\n')
        return path

    def test_tail_decodes_only_bounded_rows(self):
        self.fixture_log('2026-11-16.jsonl',range(1,1001))
        with mock.patch.object(store,'_parse_record',wraps=store._parse_record) as parse:
            rows=store.read_tail(10)
            self.assertEqual([m['id'] for m in rows],list(range(991,1001)))
            self.assertEqual(parse.call_count,11)
        self.assertEqual(store._record_cache,[])
        rows[0]['text']='changed'
        self.assertNotEqual(store.read_tail(10)[0]['text'],'changed')

    def test_tail_merges_clock_rollback_files_by_id(self):
        self.fixture_log('2026-11-18.jsonl',[1,4,7])
        self.fixture_log('2026-11-16.jsonl',[2,5,8])
        self.fixture_log('2026-11-17.jsonl',[3,6,9])
        self.assertEqual([m['id'] for m in store.read_tail(4)],[6,7,8,9])
        self.assertEqual(len(store.read_tail(20)),9)

    def test_tail_ignores_partial_suffix_and_empty_files(self):
        path=self.fixture_log('2026-11-16.jsonl',[1,2])
        with path.open('ab') as stream:stream.write(b'{"id":3')
        (store.CHAT_DIR/'2026-11-17.jsonl').write_bytes(b'')
        self.assertEqual([m['id'] for m in store.read_tail(2)],[1,2])
        self.assertEqual(store.read_tail(0),[])

    def test_tail_rejects_selected_corruption(self):
        path=self.fixture_log('2026-11-16.jsonl',[1,2])
        with path.open('ab') as stream:stream.write(b'not json\n')
        with self.assertRaisesRegex(ValueError,'Invalid log record'):store.read_tail(2)
        self.fixture_log('2026-11-16.jsonl',[2,1])
        with self.assertRaisesRegex(ValueError,'must increase'):store.read_tail(2)

    def test_search_kind_filters_before_limit_across_large_control_backlog(self):
        store._prepare()
        records = [dict(id=i, ts='2026-11-17T00:00:00+00:00', sender='user' if i<3 else 'system',
                        kind='msg' if i<3 else 'system', text='Needle') for i in range(1,1005)]
        (store.CHAT_DIR/'2026-11-17.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in records))
        self.assertEqual(store.search_messages('needle',limit=1)[0]['id'],1004)
        found = store.search_messages('needle',limit=1,kind='msg')
        self.assertEqual(found[0]['id'],2)
        self.assertEqual(store.search_messages('needle',limit=1,before_id=2,kind='msg')[0]['id'],1)
        self.assertEqual([m['id'] for m in store.search_messages('needle',kind='msg')],[1,2])
        self.assertEqual(store.search_messages('needle',kind='claim'),[])
        found[0]['text']='changed by caller'
        self.assertEqual(store.search_messages('needle',limit=1,kind='msg')[0]['text'],'Needle')
        self.assertEqual(store.get_ack('codex'),0)
        for kind in ('bogus',False,[],1):
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'search kind'):
                store.search_messages('needle',limit=0,kind=kind)

    def test_search_literal_casefold_pagination_without_ack(self):
        self.user('Straße [literal] one');self.user('other');self.user('STRASSE [literal] two')
        self.assertEqual([m['id'] for m in store.search_messages('strasse')],[1,3])
        self.assertEqual([m['id'] for m in store.search_messages('[literal]',limit=1)],[3])
        self.assertEqual([m['id'] for m in store.search_messages('strasse',before_id=3)],[1])
        self.assertEqual(store.search_messages('strasse',limit=0),[])
        self.assertEqual(store.get_ack('codex'),0)
        with self.assertRaises(ValueError):store.search_messages('   ')
        launcher=Path(store.__file__).resolve().parents[1]/'bin/chat'
        result=subprocess.run([str(launcher),'search','strasse','--last','1','--before','3'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['id'],1)
        self.assertEqual(store.get_ack('codex'),0)


    def test_cold_recent_cursor_uses_tail_without_full_scan(self):
        self.fixture_log('2026-11-16.jsonl',range(1,2001))
        with mock.patch.object(store,'_records',side_effect=AssertionError('unexpected full scan')):
            self.assertEqual([m['id'] for m in store.read_messages(1998)],[1999,2000])
            self.assertEqual(store.read_messages(2000),[])

    def test_cold_old_cursor_does_not_lose_unread_backlog(self):
        self.fixture_log('2026-11-16.jsonl',range(1,2001))
        rows=store.read_messages(1)
        self.assertEqual(len(rows),1999)
        self.assertEqual(rows[0]['id'],2)
        self.assertEqual(rows[-1]['id'],2000)

    def test_request_retry_returns_original_without_second_post(self):
        token='a'*32
        first=store.append_message('user','one delivery',request_id=token)
        again=store.append_message('user','one delivery',request_id=token)
        self.assertEqual(first,again)
        self.assertEqual(len(store.read_messages()),1)
        self.assertEqual(store.find_request(token),first)
        with self.assertRaisesRegex(ValueError,'different message'):
            store.append_message('user','changed',request_id=token)
        legacy=store.append_message('user','legacy call')
        self.assertNotIn('request_id',legacy)

    def test_concurrent_request_retries_have_one_id(self):
        token='b'*32
        code=f'from agentchat import store; print(store.append_message("user","concurrent intent",request_id="{token}")["id"])'
        env=dict(os.environ,PYTHONPATH=str(Path(store.__file__).resolve().parents[1]))
        processes=[subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,text=True) for _ in range(2)]
        ids=[]
        for process in processes:
            output,_=process.communicate(timeout=10)
            self.assertEqual(process.returncode,0);ids.append(int(output))
        self.assertEqual(ids,[1,1])
        self.assertEqual(len(store.read_messages()),1)

    def test_duplicate_request_ids_in_log_are_rejected(self):
        message=store.append_message('user','original',request_id='c'*32)
        message['id']=2
        path=next(store.CHAT_DIR.glob('*.jsonl'))
        with path.open('a') as stream:stream.write(json.dumps(message)+'\n')
        with self.assertRaisesRegex(ValueError,'duplicate request id'):store.read_messages()

    def test_request_retry_must_confirm_durability(self):
        token='d'*32
        store.append_message('user','one durable intent',request_id=token)
        with mock.patch.object(store,'_sync_committed_logs',side_effect=OSError('sync unavailable')):
            with self.assertRaises(OSError):
                store.append_message('user','one durable intent',request_id=token)
        self.assertEqual(len(store.read_messages()),1)



class PolicyTests(StoreFixture):
    def test_default_normal_refills_without_user_reset(self):
        self.assertEqual(store.guard_status()['mode'], 'normal')
        for i in range(4):store.append_message(store.AGENTS[i%2], str(i))
        self.user('does not reset the rolling window')
        store.append_message('codex', 'README.md', 'claim')
        status=store.guard_status()
        self.assertTrue(status['blocked']);self.assertEqual(status['used'], 4)
        self.assertEqual(status['window_seconds'], 60)
        with self.assertRaises(store.GuardBlocked):store.append_message('claude', 'fifth')
        with mock.patch.object(store.time, 'time', return_value=time.time()+61):
            self.assertFalse(store.guard_status()['blocked'])

    def test_custom_limit_window_duration_and_expiry(self):
        start=time.time();store.set_policy('custom', limit=10, window_s=60, duration_s=3600)
        for i in range(10):store.append_message(store.AGENTS[i%2], str(i))
        status=store.guard_status();self.assertEqual(status['used'],10)
        self.assertTrue(status['blocked']);self.assertGreater(status['retry_after'],0)
        self.user('still does not reset')
        self.assertTrue(store.guard_status()['blocked'])
        with mock.patch.object(store.time,'time',return_value=start+61):
            self.assertFalse(store.guard_status()['blocked'])
        with mock.patch.object(store.time,'time',return_value=start+3601):
            self.assertEqual(store.guard_status()['mode'],'normal')

    def test_expiry_keeps_recent_message_usage(self):
        start=time.time();store.set_policy('custom',limit=10,window_s=60,duration_s=5)
        for i in range(6):store.append_message('codex',str(i))
        with mock.patch.object(store.time,'time',return_value=start+6):
            status=store.guard_status();self.assertEqual(status['mode'],'normal')
            self.assertTrue(status['blocked']);self.assertEqual(status['used'],6)

    def test_wait_starts_fresh_and_user_resets_it(self):
        for i in range(4):store.append_message('codex',str(i))
        store.set_policy('wait')
        for i in range(4):store.append_message('codex',str(i))
        self.assertTrue(store.guard_status()['blocked'])
        self.user();self.assertFalse(store.guard_status()['blocked'])
        store.append_message('claude','after user')

    def test_pause_does_not_resume_on_user_and_keeps_target(self):
        store.set_policy('custom',limit=10,window_s=60,duration_s=3600)
        first=store.set_policy('paused');again=store.set_policy('paused')
        self.assertEqual(first,again)
        for _ in range(2):
            self.user()
            with self.assertRaisesRegex(store.GuardBlocked,'paused'):store.append_message('codex','no')
        notices=[m for m in store.read_messages() if m['text'].startswith(store._PAUSE_NOTICE)]
        self.assertEqual(len(notices),1)
        status=store.resume_policy();self.assertEqual(status['mode'],'custom')
        self.assertEqual(status['limit'],10)
        store.append_message('codex','resumed')

    def test_expired_custom_resumes_as_normal(self):
        start=time.time();store.set_policy('custom',limit=10,window_s=60,duration_s=5)
        store.set_policy('paused')
        with mock.patch.object(store.time,'time',return_value=start+6):
            self.assertEqual(store.resume_policy()['mode'],'normal')

    def test_malformed_and_future_epoch_fail_closed(self):
        store.set_policy('normal')
        value=json.loads((store.DATA_DIR/'policy.json').read_text());value['start_after_id']=999999
        (store.DATA_DIR/'policy.json').write_text(json.dumps(value))
        self.assertEqual(store.guard_status()['mode'],'wait')
        value['start_after_id']=0;value['version']=True
        (store.DATA_DIR/'policy.json').write_text(json.dumps(value))
        self.assertEqual(store.guard_status()['mode'],'wait')

    def test_invalid_policy_has_no_side_effect(self):
        for mode,kwargs in [('wrong',{}),(None,{}),([],{}),('normal',{'limit':5}),
                ('custom',{}),('custom',dict(limit=True,window_s=60,duration_s=60)),
                ('custom',dict(limit=0,window_s=60,duration_s=60)),
                ('custom',dict(limit=5,window_s=float('nan'),duration_s=60)),
                ('custom',dict(limit=5,window_s=60,duration_s=86401))]:
            with self.assertRaises(ValueError):store.set_policy(mode,**kwargs)
        self.assertEqual(store.read_messages(),[])

    def test_durable_policy_survives_auxiliary_notice_failure(self):
        with mock.patch.object(store,'_write_record',side_effect=OSError('notice unavailable')):
            status=store.set_policy('paused')
        self.assertEqual(status['mode'],'paused')
        self.assertTrue(store.guard_status()['blocked'])

    def test_cli_custom_pause_resume(self):
        launcher=Path(store.__file__).resolve().parents[1]/'bin/chat'
        def command(*args):
            result=subprocess.run([str(launcher),*args],capture_output=True,text=True,check=True)
            return json.loads(result.stdout)
        self.assertEqual(command('custom','10','1','60')['limit'],10)
        self.assertEqual(command('pause')['mode'],'paused')
        self.assertEqual(command('resume')['mode'],'custom')
        self.assertEqual(command('mode','normal')['mode'],'normal')

    def test_shared_normal_rate_is_atomic_between_processes(self):
        launcher=Path(store.__file__).resolve().parents[1]/'bin/chat'
        children=[subprocess.Popen([str(launcher),'say','--from','codex',str(i)],
                  stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) for i in range(8)]
        self.assertEqual(sorted(p.wait(timeout=15) for p in children),[0]*4+[1]*4)
        self.assertEqual(len([m for m in store.read_messages() if m['kind']=='msg']),4)
        self.assertEqual(len([m for m in store.read_messages() if m['text'].startswith(store._RATE_NOTICE)]),1)

    def test_in_progress_ui_aliases_return_canonical_modes(self):
        self.assertEqual(store.set_policy('auto',limit=10,window_s=60,duration_s=60)['mode'],'custom')
        self.assertEqual(store.set_policy('quiet')['mode'],'paused')

if __name__ == '__main__':unittest.main()
