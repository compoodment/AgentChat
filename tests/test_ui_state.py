"""Human UI state tests: separate windows, abrupt exit and durable recovery."""
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentchat import store, ui_state


class UIStateTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(prefix='agentchat-state-')
        self.environment=mock.patch.dict(os.environ,AGENTCHAT_DATA=self.folder.name)
        self.environment.start()
        importlib.reload(store)

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def test_recovery_preserves_chips_cursor_and_private_mode(self):
        with ui_state.DraftSession() as draft:
            draft.save('before [chip] after',7,{'[chip]':'large pasted payload'},3)
            identity=draft.session_id
            self.assertEqual(draft.path.stat().st_mode & 0o777,0o600)
        with ui_state.DraftSession() as recovered:
            self.assertEqual(recovered.session_id,identity)
            self.assertEqual(recovered.restored,{'draft':'before [chip] after','cursor':7,
                                                 'chips':{'[chip]':'large pasted payload'},'chip_count':3})
        self.assertEqual(store.get_ack('codex'),0)

    def test_live_windows_do_not_steal_each_others_drafts(self):
        first=ui_state.DraftSession();second=third=None
        try:
            first.save('first window',3)
            second=ui_state.DraftSession()
            self.assertIsNone(second.restored)
            self.assertNotEqual(first.session_id,second.session_id)
            second.save('second live window',5)
            first.close()
            third=ui_state.DraftSession()
            self.assertEqual(third.restored['draft'],'first window')
        finally:
            first.close()
            if second:second.close()
            if third:third.close()

    def test_process_exit_releases_lock_and_preserves_saved_draft(self):
        code='from agentchat.ui_state import DraftSession; import os; s=DraftSession(); s.save("before abrupt exit",6); os._exit(0)'
        env=dict(os.environ,PYTHONPATH=str(Path(store.__file__).resolve().parents[1]))
        subprocess.run([sys.executable,'-c',code],env=env,check=True,timeout=10)
        with ui_state.DraftSession() as recovered:
            self.assertEqual(recovered.restored['draft'],'before abrupt exit')

    def test_clearing_draft_removes_saved_payload(self):
        with ui_state.DraftSession() as draft:
            draft.save('[chip]',6,{'[chip]':'x'*10000},1)
            path=draft.path
        with ui_state.DraftSession() as recovered:
            recovered.save('',0)
            self.assertFalse(path.exists())
            self.assertIsNone(recovered.restored)
        with ui_state.DraftSession() as empty:self.assertIsNone(empty.restored)

    def test_corrupt_draft_is_preserved_and_reported(self):
        root=ui_state._directory()/'drafts';root.mkdir(exist_ok=True)
        bad=root/('a'*32+'.json');bad.write_text('invalid saved data')
        with ui_state.DraftSession() as fresh:
            self.assertIsNone(fresh.restored)
            self.assertEqual(len(fresh.warnings),1)
            self.assertEqual(bad.read_text(),'invalid saved data')

    def test_failed_atomic_save_keeps_previous_version(self):
        with ui_state.DraftSession() as draft:
            draft.save('saved before failure',5)
            with mock.patch.object(store,'_atomic_write',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):draft.save('new unsaved text',2)
        with ui_state.DraftSession() as recovered:
            self.assertEqual(recovered.restored['draft'],'saved before failure')

    def test_human_marker_is_monotonic_and_separate_from_agent_ack(self):
        for index in range(10):store.append_message('user',str(index))
        self.assertEqual(ui_state.get_read_marker(),0)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(ui_state.mark_read,[10,5]))
        self.assertEqual(ui_state.get_read_marker(),10)
        self.assertEqual(ui_state.mark_read(2),10)
        self.assertEqual(store.get_ack('codex'),0)
        self.assertEqual(store.get_ack('claude'),0)
        with self.assertRaises(ValueError):ui_state.mark_read(11)

    def test_marker_corruption_and_invalid_payloads_are_not_overwritten(self):
        root=ui_state._directory();path=root/'read.json';path.write_text('null')
        with self.assertRaisesRegex(ValueError,'Invalid human'):ui_state.get_read_marker()
        self.assertEqual(path.read_text(),'null')
        with ui_state.DraftSession() as draft:
            with self.assertRaises(ValueError):draft.save('abc',4)
            with self.assertRaises(ValueError):draft.save('abc',True)
            with self.assertRaises(ValueError):draft.save('abc',1,{'label':False})

    def test_send_clear_failure_does_not_restore_a_committed_draft(self):
        with ui_state.DraftSession() as draft:
            draft.save('sent before cleanup failure',6)
            with mock.patch.object(draft,'save',side_effect=OSError('cleanup unavailable')):
                message=draft.send('sent before cleanup failure','sent before cleanup failure',6)
            self.assertEqual(message['id'],1)
            self.assertTrue(draft.path.exists())
        with ui_state.DraftSession() as recovered:self.assertIsNone(recovered.restored)
        self.assertEqual(len(store.read_messages()),1)

    def test_uncommitted_pending_send_restores_and_retries_same_token(self):
        with ui_state.DraftSession() as draft:
            with mock.patch.object(store,'append_message',side_effect=OSError('before append')):
                with self.assertRaises(OSError):draft.send('safe retry','safe retry',4)
            token=json.loads(draft.path.read_text())['pending_send']['request_id']
        with ui_state.DraftSession() as recovered:
            self.assertEqual(recovered.restored['draft'],'safe retry')
            message=recovered.send('safe retry','safe retry',4)
            self.assertEqual(message['request_id'],token)
        self.assertEqual(len(store.read_messages()),1)

    def test_new_identical_draft_is_not_a_retry_after_cleanup_failure(self):
        with ui_state.DraftSession() as draft:
            with mock.patch.object(draft,'save',side_effect=OSError('cleanup unavailable')):
                first=draft.send('same text','same text',4)
            draft.save('same text',4)
            self.assertNotIn('pending_send',json.loads(draft.path.read_text()))
            second=draft.send('same text','same text',4)
            self.assertNotEqual(first['request_id'],second['request_id'])
        self.assertEqual(len(store.read_messages()),2)

    def test_pending_draft_survives_uncertain_log_confirmation(self):
        with ui_state.DraftSession() as draft:
            with mock.patch.object(draft,'save',side_effect=OSError('cleanup unavailable')):
                draft.send('already committed','already committed',5)
        with mock.patch.object(store,'find_request',side_effect=OSError('temporary read error')):
            with ui_state.DraftSession() as recovered:
                self.assertEqual(recovered.restored['draft'],'already committed')
                self.assertTrue(recovered.warnings)
                with mock.patch.object(store, '_atomic_json', side_effect=OSError('disk unavailable')):
                    with self.assertRaises(OSError):
                        recovered.save('edit that did not save', 3)
                recovered.send('already committed','already committed',5)
        self.assertEqual(len(store.read_messages()),1)

    def test_draft_representation_change_keeps_pending_token_across_cursor_save(self):
        label, marker = '[old label]', chr(0xf0002)
        text = 'before\npasted text'
        old_draft = 'before' + label
        new_draft = 'before' + marker
        with ui_state.DraftSession() as draft:
            with mock.patch.object(draft, 'save', side_effect=OSError('cleanup unavailable')):
                first = draft.send(text, old_draft, len(old_draft), {label: '\npasted text'}, 1)
        with mock.patch.object(store, 'find_request', side_effect=OSError('temporary read error')):
            with ui_state.DraftSession() as recovered:
                recovered.adopt_restored_representation(new_draft, len(new_draft),
                                                        {marker: '\npasted text'}, 2)
                recovered.save(new_draft, len(new_draft) - 1, {marker: '\npasted text'}, 2)
                saved = json.loads(recovered.path.read_text())
                self.assertEqual(saved['pending_send']['request_id'], first['request_id'])
                second = recovered.send(text, new_draft, len(new_draft) - 1, {marker: '\npasted text'}, 2)
                self.assertEqual(second, first)
        self.assertEqual(store.read_messages(), [first])


if __name__=='__main__':unittest.main()
