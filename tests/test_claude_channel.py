"""Claude channel tests: the MCP server runs as a subprocess on a throwaway data root."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agentchat-channel-")
        self.env = dict(os.environ, AGENTCHAT_DATA=self.directory.name, CHANNEL_QUIET_S="1",
                        CHANNEL_HOLD_S="3", PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen([sys.executable, "-m", "agentchat.claude_channel"], cwd=ROOT, env=self.env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True)
        self.lines, self.raw, self.next_id = queue.Queue(), [], 1
        threading.Thread(target=self.pump, daemon=True).start()
        # a store in this process on the same data root, for posting as other senders
        os.environ["AGENTCHAT_DATA"] = self.directory.name
        from agentchat import store
        import importlib
        self.store = importlib.reload(store)

    def tearDown(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()
        os.environ.pop("AGENTCHAT_DATA", None)
        self.directory.cleanup()

    def pump(self):
        for line in self.proc.stdout:
            self.raw.append(line)
            self.lines.put(line)

    def get(self, timeout=5):
        try:
            return json.loads(self.lines.get(timeout=timeout))
        except queue.Empty:
            return None

    def request(self, method, params=None):
        ident, self.next_id = self.next_id, self.next_id + 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            message = self.get()
            self.assertIsNotNone(message, f"no reply to {method}")
            if message.get("id") == ident:
                return message

    def start(self):
        reply = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "0"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        return reply

    def notification(self, timeout):
        while True:
            message = self.get(timeout)
            if message is None or message.get("method") == "notifications/claude/channel":
                return message

    def test_initialize_and_tools(self):
        result = self.start()["result"]
        self.assertIn("claude/channel", result["capabilities"]["experimental"])
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        names = {t["name"] for t in self.request("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"chat_reply", "chat_ack", "chat_take", "chat_read"})
        self.assertEqual(self.request("ping")["result"], {})

    def test_unknown_method_is_error(self):
        self.start()
        self.assertEqual(self.request("nope/nothing")["error"]["code"], -32601)

    def test_reply_appends_as_claude(self):
        self.start()
        result = self.request("tools/call", {"name": "chat_reply", "arguments": {"text": "hi there"}})["result"]
        self.assertFalse(result["isError"])
        last = self.store.read_messages()[-1]
        self.assertEqual((last["sender"], last["text"]), ("claude", "hi there"))

    def test_user_message_notifies_quickly_and_ack_works(self):
        self.start()
        user = self.store.append_message("user", "hello claude")
        note = self.notification(2.5)
        self.assertIsNotNone(note)
        self.assertEqual(note["params"]["meta"]["last_id"], str(user["id"]))
        self.assertIn(f"#{user['id']} user:\nhello claude", note["params"]["content"])
        self.assertEqual(self.store.get_ack("claude"), 0)  # never acked by the server itself
        ack = self.request("tools/call", {"name": "chat_ack", "arguments": {"id": user["id"]}})["result"]
        self.assertFalse(ack["isError"])
        self.assertEqual(self.store.get_ack("claude"), user["id"])
        bad = self.request("tools/call", {"name": "chat_ack", "arguments": {"id": 999}})["result"]
        self.assertTrue(bad["isError"])

    def test_restart_replays_unacked(self):
        user = self.store.append_message("user", "before start")
        self.start()
        note = self.notification(3)
        self.assertIn(f"#{user['id']}", note["params"]["content"])

    def test_routine_messages_batched_and_claims_skipped(self):
        self.start()
        self.store.append_message("codex", "one")
        self.store.append_message("codex", "src/a.py", kind="claim")
        self.store.append_message("codex", "two")
        self.assertIsNone(self.notification(0.5))  # held, not immediate
        note = self.notification(6)
        self.assertIsNotNone(note)
        content = note["params"]["content"]
        self.assertEqual(len(content.split("\n\n")), 2)     # two messages, each a header and its text
        self.assertIn("one", content)
        self.assertIn("two", content)
        self.assertNotIn("src/a.py", content)
        self.assertEqual(note["params"]["meta"]["senders"], "codex")

    def test_events_carry_exact_text(self):
        # Exercise the full text, including a literal ⏎ and a line break.
        self.start()
        text = "burst A: " + "0123456789" * 55 + " — exact tail 🟣\nreal newline, literal ⏎ kept"
        self.store.append_message("user", text)
        note = self.notification(3)
        self.assertIn(text, note["params"]["content"])
        read = self.request("tools/call", {"name": "chat_read", "arguments": {"after": 0}})["result"]
        self.assertIn(text, read["content"][0]["text"])

    def test_no_message_is_cut_or_skipped_at_the_event_size_limit(self):
        # Oversize records and omitted records differ. The claimed last id must match the last delivered record.
        self.start()
        big = "x" * 16050
        sent = [self.store.append_message("user", big)["id"], self.store.append_message("user", "small")["id"],
                self.store.append_message("user", "y" * 16050)["id"]]
        seen = []
        for _ in range(3):
            note = self.notification(4)
            self.assertIsNotNone(note, f"after {seen}")
            ids = [int(n) for n in __import__("re").findall(r"^#(\d+) user:$", note["params"]["content"], 8)]
            self.assertEqual(int(note["params"]["meta"]["last_id"]), ids[-1])   # last_id = last carried
            seen += ids
            if seen == sent:
                break
        self.assertEqual(seen, sent)
        self.assertIn(big, "".join(self.raw_contents()))

    def raw_contents(self):
        return [json.loads(line)["params"]["content"] for line in self.raw if '"notifications/claude/channel"' in line]

    def test_read_after_zero_starts_at_the_beginning(self):
        for n in range(21):
            self.store.append_message("user", f"record {n}")
        self.start()
        text = self.request("tools/call", {"name": "chat_read", "arguments": {"after": 0}})["result"]["content"][0]["text"]
        self.assertTrue(text.startswith("#1 user:"), text[:30])

    def test_acking_what_was_delivered_shows_listening_at_once(self):
        # the real run: Claude replied (its own messages are never acked) and acked, but stayed "working" for good
        self.start()
        user = self.store.append_message("user", "a question")
        self.assertIsNotNone(self.notification(3))
        self.assertEqual(self.store.get_presence()["claude"]["state"], "working")
        self.request("tools/call", {"name": "chat_reply", "arguments": {"text": "an answer"}})
        self.request("tools/call", {"name": "chat_ack", "arguments": {"id": user["id"]}})
        self.assertEqual(self.store.get_presence()["claude"]["state"], "listening")

    def test_an_ack_cannot_mark_listening_over_a_newer_delivery(self):
        # Codex's forced interleaving: ack #1 decides "caught up", delivery of #2 marks working, the ack's
        # listening then lands on top. The decision and its write now share a lock with delivery.
        import io
        from unittest import mock
        from agentchat import claude_channel
        channel = claude_channel.Channel(out=io.StringIO())
        first, second = (self.store.append_message("user", text) for text in ("one", "two"))
        channel.notified = first["id"]
        writes, gate, release = [], threading.Event(), threading.Event()
        real = self.store.set_presence

        def set_presence(agent, state, *args, **kwargs):
            if state == "listening":
                gate.set()                       # the ack has decided; let the delivery try to run now
                release.wait(2)
            writes.append(state)
            return real(agent, state, *args, **kwargs)

        with mock.patch.object(claude_channel.store, "set_presence", set_presence):
            acking = threading.Thread(target=channel.call_tool, args=("chat_ack", {"id": first["id"]}))
            acking.start()
            gate.wait(2)

            def deliver():
                with channel.presence:
                    channel.notified = second["id"]
                    claude_channel.store.set_presence("claude", "working")

            delivering = threading.Thread(target=deliver)
            delivering.start()
            time.sleep(0.2)                      # without the lock, delivery would finish here, before the ack
            release.set()
            acking.join(3)
            delivering.join(3)
        self.assertEqual(writes, ["listening", "working"])
        self.assertEqual(self.store.get_presence()["claude"]["state"], "working")

    def test_a_failing_status_write_does_not_stop_delivery(self):
        # Failed presence metadata must not halt delivery.
        import io
        from unittest import mock
        from agentchat import claude_channel
        out = io.StringIO()
        channel = claude_channel.Channel(out=out)
        wait = claude_channel.store.wait_for_change
        with mock.patch.object(claude_channel.store, "set_presence", side_effect=OSError("drive busy")), \
                mock.patch.object(claude_channel.store, "wait_for_change",   # short, so it can stop promptly
                                  lambda after, timeout: wait(after, timeout=min(timeout, 0.2))):
            watcher = threading.Thread(target=channel.watch, daemon=True)
            watcher.start()
            try:
                for text in ("first", "second"):
                    self.store.append_message("user", text)
                    deadline = time.monotonic() + 10
                    while text not in out.getvalue() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertIn(text, out.getvalue())             # delivered, and still watching after
            finally:
                channel.stopping.set()                              # nothing may keep writing into this folder
                watcher.join(5)
        self.assertFalse(watcher.is_alive())

    def test_an_ack_succeeds_even_when_the_status_write_fails(self):
        # A saved cursor is a successful acknowledgment even if presence cannot be written.
        import io
        from unittest import mock
        from agentchat import claude_channel
        channel = claude_channel.Channel(out=io.StringIO())
        request = self.store.append_message("user", "a question")
        channel.notified = request["id"]
        with mock.patch.object(claude_channel.store, "set_presence", side_effect=OSError("drive busy")):
            text, failed = channel.call_tool("chat_ack", {"id": request["id"]})
        self.assertFalse(failed, text)
        self.assertEqual(self.store.get_ack("claude"), request["id"])

    def test_code_and_quoted_mentions_do_not_wake(self):
        self.start()
        self.store.append_message("codex", "a code example: `@all` and `@claude`")
        self.assertIsNone(self.notification(0.8))           # held like any routine message
        self.store.append_message("codex", "@claude this one is real")
        self.assertIsNotNone(self.notification(2.5))

    def test_a_burst_of_urgent_messages_wakes_once(self):
        self.start()
        for n in range(3):                                   # short enough to form one burst
            self.store.append_message("user", f"burst {n}")
            time.sleep(0.04)
        note = self.notification(3)
        self.assertEqual(note["params"]["meta"]["count"], "3")
        self.assertIsNone(self.notification(1))

    def test_own_messages_not_echoed(self):
        self.start()
        self.request("tools/call", {"name": "chat_reply", "arguments": {"text": "mine"}})
        self.assertIsNone(self.notification(2.5))

    def test_take_and_read(self):
        self.start()
        user = self.store.append_message("user", "please do x")
        self.notification(3)
        taken = self.request("tools/call", {"name": "chat_take", "arguments": {"id": user["id"]}})["result"]
        self.assertFalse(taken["isError"])
        again = self.request("tools/call", {"name": "chat_take", "arguments": {"id": 99}})["result"]
        self.assertTrue(again["isError"])
        text = self.request("tools/call", {"name": "chat_read", "arguments": {}})["result"]["content"][0]["text"]
        self.assertIn("please do x", text)

    def test_stdout_is_only_json(self):
        self.start()
        self.store.append_message("user", "x")
        self.notification(3)
        self.proc.stdin.write("garbage\n")
        self.proc.stdin.flush()
        self.assertEqual(self.get()["error"]["code"], -32700)
        for raw in self.raw:
            self.assertEqual(json.loads(raw)["jsonrpc"], "2.0")


if __name__ == "__main__":
    unittest.main()
