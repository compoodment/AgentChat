"""Chat screen tests: run the real bin/chat in a pseudo-terminal and read back what it drew.

Stdlib only. Every test uses its own throwaway data folder, never the live chat.
Run: python3 -I tests/test_ui.py
"""

import codecs
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import statistics
import struct
import subprocess
import tempfile
import termios
import time
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = str(ROOT / "bin" / "chat")
SCRATCH = tempfile.gettempdir()        # throwaway data folders go here (honours TMPDIR)


def _cells(ch):
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


class Terminal:
    """Just enough of xterm to turn ncurses output back into text."""

    def __init__(self, rows, cols):
        self.resize(rows, cols)
        self.cur, self.saved, self.buf, self.last = (0, 0), (0, 0), "", " "

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.top, self.bottom = 0, rows - 1
        clamp = lambda pos: (min(pos[0], rows - 1), min(pos[1], cols - 1))
        self.cur, self.saved = clamp(getattr(self, "cur", (0, 0))), clamp(getattr(self, "saved", (0, 0)))

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.grid)

    def _put(self, ch):
        row, col = self.cur
        row, col = min(row, self.rows - 1), min(col, self.cols - 1)
        width = _cells(ch)
        if not width:
            return
        self.grid[row][col] = ch
        if width == 2 and col + 1 < self.cols:
            self.grid[row][col + 1] = ""
        self.last, self.cur = ch, (row, min(col + width, self.cols))

    def _scroll(self, up=True):
        if up:
            del self.grid[self.top]
            self.grid.insert(self.bottom, [" "] * self.cols)
        else:
            del self.grid[self.bottom]
            self.grid.insert(self.top, [" "] * self.cols)

    def feed(self, data):
        data = self.buf + data
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                match = re.match(r"\x1b\[([?>=]?)([\d;]*)([@-~])", data[i:])
                if match:
                    if not match.group(1):
                        self._csi(match.group(2), match.group(3))
                    i += match.end()
                    continue
                match = re.match(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()]\w|\x1b[=>78DEMc]", data[i:])
                if match:
                    code = match.group(0)
                    if code == "\x1b7":
                        self.saved = self.cur
                    elif code == "\x1b8":
                        self.cur = self.saved
                    elif code == "\x1bM":
                        if self.cur[0] == self.top:
                            self._scroll(False)
                        else:
                            self.cur = (self.cur[0] - 1, self.cur[1])
                    i += match.end()
                    continue
                if i + 1 >= len(data) or len(data) - i < 16:
                    break                   # an incomplete sequence: wait for more
                i += 2
                continue
            if ch == "\r":
                self.cur = (self.cur[0], 0)
            elif ch == "\n":
                if self.cur[0] == self.bottom:
                    self._scroll()
                else:
                    self.cur = (self.cur[0] + 1, self.cur[1])
            elif ch == "\b":
                self.cur = (self.cur[0], max(0, self.cur[1] - 1))
            elif ch not in "\x07\x0e\x0f":
                self._put(ch)
            i += 1
        self.buf = data[i:]

    def _csi(self, args, final):
        self._csi_raw(args, final)
        # A real terminal clamps cursor moves to the screen, e.g. output drawn just before a resize.
        self.cur = (max(0, min(self.cur[0], self.rows - 1)), max(0, min(self.cur[1], self.cols)))

    def _csi_raw(self, args, final):
        values = [int(x) if x else 0 for x in args.split(";")] if args else []
        n = values[0] if values and values[0] else 1
        row, col = self.cur
        blank = [" "] * self.cols
        if final in "Hf":
            self.cur = ((values[0] or 1) - 1 if values else 0, (values[1] or 1) - 1 if len(values) > 1 else 0)
        elif final == "A":
            self.cur = (max(0, row - n), col)
        elif final == "B":
            self.cur = (min(self.rows - 1, row + n), col)
        elif final == "C":
            self.cur = (row, min(self.cols - 1, col + n))
        elif final == "D":
            self.cur = (row, max(0, col - n))
        elif final == "G":
            self.cur = (row, n - 1)
        elif final == "d":
            self.cur = (n - 1, col)
        elif final == "J":
            mode = values[0] if values else 0
            if mode == 0:
                self.grid[row][col:] = blank[col:]
                lines = range(row + 1, self.rows)
            else:
                lines = range(0, row) if mode == 1 else range(self.rows)
            for y in lines:
                self.grid[y] = list(blank)
        elif final == "K":
            mode = values[0] if values else 0
            if mode == 0:
                self.grid[row][col:] = blank[col:]
            elif mode == 1:
                self.grid[row][:col + 1] = blank[:col + 1]
            else:
                self.grid[row] = list(blank)
        elif final == "X":
            self.grid[row][col:col + n] = blank[:min(n, self.cols - col)]
        elif final == "b":
            for _ in range(n):
                self._put(self.last)
        elif final == "r":
            self.top = (values[0] or 1) - 1 if values else 0
            self.bottom = (values[1] or self.rows) - 1 if len(values) > 1 else self.rows - 1
            self.cur = (0, 0)
        elif final in "LM":
            for _ in range(n):
                if final == "L":
                    del self.grid[self.bottom]
                    self.grid.insert(row, list(blank))
                else:
                    del self.grid[row]
                    self.grid.insert(self.bottom, list(blank))
        elif final in "ST":
            for _ in range(n):
                self._scroll(final == "S")


class ChatScreen:
    """bin/chat running in a pty with its own data folder."""

    def __init__(self, data, rows=24, cols=80, background=None, **env):
        self.data = data
        self.env = dict(os.environ, AGENTCHAT_DATA=data, TERM="xterm-256color", **env)
        if background is None:              # the test pty can't answer the colour query: don't wait for it
            self.env.setdefault("AGENTCHAT_THEME", "dark")
        else:
            self.env.pop("AGENTCHAT_THEME", None)
        self.env.pop("NO_COLOR", None)
        self.term = Terminal(rows, cols)
        self.raw = b""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")   # a character may span reads
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.execve(LAUNCHER, [LAUNCHER], self.env)
        if background:                      # answer the colour query the way a terminal would
            self.until(lambda: b"\x1b]11;?" in self.raw, 10)
            os.write(self.fd, f"\x1b]11;rgb:{background}\x07".encode())
        self.pump_until("❯", 20)

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.01)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    return
                self.raw += chunk
                self.term.feed(self.decoder.decode(chunk))

    def pump_until(self, text, seconds=3):
        end = time.time() + seconds
        while text not in self.term.text() and time.time() < end:
            self.pump(0.05)
        return text in self.term.text()

    def type(self, data, settle=0.05):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)
        self.pump(settle)

    def until(self, condition, seconds=15):
        """Pump output until condition() holds; return whether it did. Replaces fixed sleeps.

        Generous by design: a passing check returns at once, but in the gate the store suite runs in
        parallel and its fsyncs can delay a large journalled send on a slow drive by several seconds.
        """
        end = time.time() + seconds
        while time.time() < end:
            try:
                if condition():
                    return True
            except (IndexError, KeyError, ValueError, OSError):
                pass
            self.pump(0.02)
        return condition()

    def resize(self, rows, cols):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.term.resize(rows, cols)
        os.kill(self.pid, signal.SIGWINCH)
        self.until(lambda: "─" * min(cols, 20) in self.term.text() and "❯" in self.term.text())
        self.pump(0.1)

    def chat(self, *args):
        result = subprocess.run([LAUNCHER, *args], env=self.env, capture_output=True, text=True)
        return json.loads(result.stdout) if result.stdout.strip().startswith("{") else result

    def messages(self):
        folder = Path(self.data) / "chat"
        return [json.loads(line) for path in sorted(folder.glob("*.jsonl"))
                for line in path.read_text().splitlines()]

    def quit_wait(self, seconds=10):
        """Wait for the process to exit on its own; return its exit code (None if it didn't)."""
        end = time.time() + seconds
        while time.time() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.pump(0.05)
                return os.waitstatus_to_exitcode(status)
            self.pump(0.02)
        return None

    def quit(self):
        self.type("\x03\x03", 0)
        end = time.time() + 5
        while time.time() < end:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if done:
                self.pump(0.05)
                return os.waitstatus_to_exitcode(status)
            self.pump(0.02)
        return None


JOURNAL_SHIM = """
import os, time
from agentchat import store
_atomic_json = store._atomic_json

def _journal(path, value):
    # Only the pending-send journal write: that is the step that blocks when the disk stalls.
    if isinstance(value, dict) and "pending_send" in value:
        while os.path.exists(os.environ["AGENTCHAT_TEST_GATE"]):
            time.sleep(0.05)
        if os.path.exists(os.environ["AGENTCHAT_TEST_FAIL"]):
            raise OSError(5, "injected journal failure")
    elif isinstance(value, dict) and "draft" in value:      # an ordinary draft save
        if os.path.exists(os.environ["AGENTCHAT_TEST_FAIL"] + "-saves"):
            raise OSError(5, "injected save failure")
    return _atomic_json(path, value)

store._atomic_json = _journal
"""


def seed(data, count):
    """Write `count` plain messages straight into a throwaway log (fast, no fsync per line)."""
    folder = Path(data) / "chat"
    folder.mkdir(parents=True)
    day = time.strftime("%Y-%m-%d")
    words = "paste store cache wrap screen claim task review café 日本語 🙂 merged tests pass".split()
    with (folder / f"{day}.jsonl").open("w") as log:
        for number in range(1, count + 1):
            sender = ("user", "claude", "codex")[number % 3]
            text = " ".join(words[(number * k) % len(words)] for k in range(1, 4 + number % 30))
            log.write(json.dumps({"id": number, "ts": f"{day}T09:{number % 60:02d}:00+01:00",
                                  "sender": sender, "kind": "msg", "text": text}) + "\n")
    (Path(data) / "seq").write_text(f"{count}\n")


class SubmitTests(unittest.TestCase):
    """Screen.submit without a terminal: a refresh failure must not look like a failed send."""

    def test_refresh_failure_after_a_successful_send_clears_the_draft(self):
        import sys
        from unittest import mock
        sys.path.insert(0, str(ROOT))
        from agentchat import store, ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        screen.draft, screen.cursor, screen.history, screen.notes = "hello", 5, [], []
        screen.history_pos, screen.stash, screen.scroll, screen.unseen = None, "", 0, {}
        screen.chips, screen.chip_count = {}, 0
        screen.drafts, screen.view = None, None
        sent = []
        with mock.patch.object(store, "append_message", lambda *a: sent.append(a)), \
                mock.patch.object(screen, "refresh_data", side_effect=OSError(5, "Input/output error"), create=True):
            screen.submit("hello")
        self.assertEqual(len(sent), 1)
        self.assertEqual(screen.draft, "")
        self.assertIn("Sent", screen.notes[0][0])


class ChipTests(unittest.TestCase):
    """Paste chips edit as one unit and expand exactly once."""

    def screen(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agentchat import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        screen.draft, screen.cursor, screen.history_pos = "", 0, None
        screen.chips, screen.chip_count = {}, 0
        screen.notes, screen.help, screen.ctrl_c_at, screen.running = [], False, 0, True
        screen.menu_index, screen.menu_closed = 0, False
        screen.win = type("Window", (), {"getmaxyx": lambda self: (24, 80)})()
        return screen

    def test_pasted_text_that_looks_like_a_label_is_not_expanded_again(self):
        screen = self.screen()
        first = "a" * 1001 + "[Pasted text #2 · 1001 chars]"
        screen.paste(first)
        screen.paste("b" * 1001)
        self.assertEqual(screen.expand(screen.draft), first + "b" * 1001)

    def test_deleting_a_chip_forgets_its_text(self):
        screen = self.screen()
        screen.paste("x" * 5000)
        screen.key("\x7f")
        self.assertEqual((screen.draft, screen.chips), ("", {}))

    def test_edits_inside_a_label_act_on_the_whole_chip(self):
        screen = self.screen()
        screen.insert("hi ")
        screen.paste("y" * 2000)
        label = next(iter(screen.chips))
        screen.cursor = 3 + 5               # inside the label
        screen.insert("Z")                  # typing jumps past the chip
        self.assertIn(label, screen.draft)
        screen.cursor = 3 + 5
        screen.key(curses_key("KEY_DC"))
        self.assertEqual((screen.draft, screen.chips), ("hi Z", {}))


def curses_key(name):
    import curses
    return getattr(curses, name)


class GroupingTests(unittest.TestCase):
    def test_mixed_timestamp_kinds_never_group_or_crash(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agentchat import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        aware = {"kind": "msg", "sender": "codex", "text": "a", "ts": "2026-11-16T10:00:00+01:00"}
        naive = dict(aware, ts="2026-11-16T10:00:30")
        self.assertFalse(screen._continues(aware, naive))
        self.assertTrue(screen._continues(aware, dict(aware, ts="2026-11-16T10:01:00+01:00")))


class ReplyCommandTests(unittest.TestCase):
    def test_only_the_exact_reply_command_is_rewritten(self):
        import sys
        from unittest import mock
        sys.path.insert(0, str(ROOT))
        from agentchat import store, ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        screen.draft, screen.cursor, screen.history, screen.notes = "", 0, [], []
        screen.history_pos, screen.stash, screen.scroll, screen.unseen = None, "", 0, {}
        screen.chips, screen.chip_count, screen.drafts, screen.view = {}, 0, None, None
        screen.running, screen.claims, screen.presence, screen.found = True, {}, {}, None
        sent = []
        with mock.patch.object(store, "append_message", lambda *a: sent.append(a[1])), \
                mock.patch.object(screen, "refresh_data", create=True):
            screen.submit("/reply 7 sure")
            screen.submit("  /reply 8   with spaces")
            screen.submit("/reply7 nope")
            screen.submit("/replying")
        self.assertEqual(sent, ["↪ #7 sure", "↪ #8 with spaces"])
        self.assertIn("Unknown command", screen.notes[0][0])


class ModeTests(unittest.TestCase):
    """Mode commands and the footer, against a stand-in for the store's set_policy API."""

    def screen(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agentchat import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight, screen.cache = None, {}
        screen.writer, screen.sending = None, None
        screen.notes, screen.refused = [], False
        screen.attr = {"dim": 1, "error": 2}
        return screen

    def test_mode_commands_call_the_policy(self):
        from unittest import mock
        from agentchat import store
        screen, calls = self.screen(), []
        policy = lambda mode, **kw: calls.append((mode, kw)) or {"mode": mode}
        with mock.patch.object(store, "set_policy", policy, create=True), \
                mock.patch.object(store, "resume_policy", lambda: calls.append(("resume", {})) or {}, create=True):
            for command, argument in [("/custom", "10 1 60"), ("/custom", "off"), ("/mode", "wait"),
                                      ("/pause", ""), ("/resume", ""), ("/custom", "2h"), ("/custom", "0 1 5")]:
                screen.set_mode(command, argument)
        self.assertEqual(calls, [("custom", {"limit": 10, "window_s": 60, "duration_s": 3600}), ("normal", {}),
                                 ("wait", {}), ("paused", {}), ("resume", {})])
        self.assertIn("Usage: /custom 10 1 60", screen.notes[-1][0])     # the last two were rejected

    def test_footer_describes_each_mode(self):
        screen = self.screen()
        screen.set_guard({"mode": "normal", "used": 1, "message_limit": 4, "window_seconds": 60})
        self.assertEqual(screen.footer()[0], "normal · 1/4 per min")
        screen.set_guard({"mode": "auto", "used": 3, "message_limit": 10, "window_seconds": 120,
                          "expires_at": 1794837600, "blocked": True, "retry_after": 30})
        self.assertTrue(screen.footer()[0].startswith("custom · 3/10 per 2 min until "))
        self.assertRegex(screen.footer()[0], r"full, agents wait (29|30)s")   # counting down from 30
        screen.set_guard({"mode": "quiet"})
        self.assertEqual(screen.footer()[0], "paused · agents can't post · /resume")


class ModifiedKeyTests(unittest.TestCase):
    """Modified-key reports (kitty CSI u, xterm modifyOtherKeys) decode to the keys the chat already knows."""

    def test_reports_decode_to_familiar_keys(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agentchat.ui.keys import decode
        cases = {
            "[13;2u": ("newline",), "[27;2;13~": ("newline",), "[13;3u": ("newline",), "[13;5u": ("newline",),
            "[13u": ("key", "\r"), "[57414;2u": ("newline",), "[27u": ("escape",), "[99;5u": ("key", "\x03"),
            "[106;5u": ("key", "\n"), "[98;3u": ("alt", "b"), "[127;3u": ("alt", "\x7f"), "[127u": ("key", "\x7f"),
            "[9u": ("key", "\t"), "[97u": ("key", "a"), "[27;5;99~": ("key", "\x03"),
            "[200~": None, "[>1u": None, "[2~": None, "[15~": None, "[57399;5u": None, "[P": None,
        }
        import curses
        cases.update({                  # cursor keys, sent as CSI in kitty's mode and normal cursor mode
            "[A": ("key", curses.KEY_UP), "[B": ("key", curses.KEY_DOWN), "OC": ("key", curses.KEY_RIGHT),
            "[D": ("key", curses.KEY_LEFT), "[H": ("key", curses.KEY_HOME), "OF": ("key", curses.KEY_END),
            "[1~": ("key", curses.KEY_HOME), "[4~": ("key", curses.KEY_END), "[3~": ("key", curses.KEY_DC),
            "[3;5~": ("key", curses.KEY_DC), "[5~": ("key", curses.KEY_PPAGE), "[6~": ("key", curses.KEY_NPAGE),
            "[1;2A": ("key", curses.KEY_UP), "[1;5D": ("alt", "b"), "[1;3C": ("alt", "f"),
        })
        for sequence, expected in cases.items():
            self.assertEqual(decode(sequence), expected, sequence)


class MentionTests(unittest.TestCase):
    def test_only_exact_mentions_of_the_user_count(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from agentchat import ui
        def mine(text):
            return ui.Screen.mentions_me({"text": text})
        self.assertTrue(mine("@user look"))
        self.assertTrue(mine("ping @ALL."))
        self.assertFalse(mine("@user-name is someone else"))
        self.assertFalse(mine("@all-team"))
        self.assertFalse(mine("mail user@all.example"))


class ScreenCase(unittest.TestCase):
    """Starts bin/chat in a pty with its own throwaway data. Holds no tests itself."""
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="agentchat-ui-", dir=SCRATCH)
        self.data = os.path.join(self.folder.name, "data")
        self.screen = None
        self.others = []                    # extra screens a test opens; stopped before the folder goes

    def tearDown(self):
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None)
        failed = result and any(test is self for test, _ in result.failures + result.errors)
        if failed and self.screen:          # leave evidence: what the screen showed when the test gave up
            import sys
            print(f"\n--- screen at failure of {self.id()} ---\n{self.screen.term.text()}\n--- end ---",
                  file=sys.stderr)
        for screen in [self.screen] + self.others:
            if not screen:
                continue
            try:
                os.kill(screen.pid, signal.SIGKILL)
                os.waitpid(screen.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
            try:
                os.close(screen.fd)             # the pty master; reopened screens (crash tests) close theirs too
            except OSError:
                pass
        self.folder.cleanup()               # only now: a live screen could still be writing into it

    def open(self, count=0, **options):
        if count:
            seed(self.data, count)
        self.screen = ChatScreen(self.data, **options)
        return self.screen


class ChatScreenTests(ScreenCase):
    """Functional screen tests; safe to run alongside other suites."""
    def test_paste_stays_in_the_draft_and_sends_once(self):
        screen = self.open()
        screen.type("\x1b[20", 0.25)        # a start marker split across reads
        screen.type("0~first line\rsecond line\x1b[201~")
        self.assertTrue(screen.until(lambda: "second line" in screen.term.text()))
        self.assertEqual(screen.messages(), [])
        screen.type("\r")
        self.assertTrue(screen.until(lambda: screen.messages()))
        self.assertEqual([m["text"] for m in screen.messages()], ["first line\nsecond line"])

    def test_large_paste_becomes_a_chip_and_sends_in_full(self):
        screen = self.open()
        big = "\n".join(f"line {n} " + "x" * 60 for n in range(200))
        start = time.time()
        screen.type("\x1b[200~" + big + "\x1b[201~")
        self.assertTrue(screen.pump_until("[Pasted text #1 · 200 lines]"))
        screen.type("a")
        self.assertLess(time.time() - start, 3)
        screen.type("\x7f\r")
        self.assertTrue(screen.until(lambda: screen.messages()), "the send never reached the log")
        self.assertEqual(screen.messages()[-1]["text"], big)

    def test_pasted_code_keeps_its_indentation(self):
        screen = self.open()
        screen.type("\x1b[200~    def f():\n        return 1\n\x1b[201~\r")
        screen.until(lambda: screen.messages())
        self.assertEqual(screen.messages()[-1]["text"], "    def f():\n        return 1")

    def test_failed_send_keeps_the_draft(self):
        screen = self.open()
        screen.chat("say", "--from", "user", "first")
        screen.until(lambda: "first" in screen.term.text())
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        log.chmod(0o444)
        screen.type("keep this text")
        screen.type("\r")
        self.assertTrue(screen.until(lambda: "Not sent" in screen.term.text()))
        log.chmod(0o644)
        self.assertIn("keep this text", screen.term.text())
        screen.type("\r")
        screen.until(lambda: screen.messages()[-1]["text"] == "keep this text")
        self.assertEqual(screen.messages()[-1]["text"], "keep this text")

    def test_mentions_of_the_user_are_marked(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "@user please look at this")
        self.assertTrue(screen.until(lambda: "→ you" in screen.term.text() and "▌" in screen.term.text()))

    def test_resizing_down_and_up_redraws(self):
        screen = self.open(50)
        screen.resize(12, 40)
        self.assertIn("❯", screen.term.text())
        screen.resize(24, 80)
        self.assertIn("❯", screen.term.text())
        self.assertGreater(sum(1 for line in screen.term.text().splitlines() if line.strip()), 12)

    def test_task_claims_fold_under_the_request(self):
        screen = self.open()
        request = screen.chat("say", "--from", "user", "@claude fix the bug")
        screen.chat("take", "--as", "claude", str(request["id"]))
        self.assertTrue(screen.pump_until("claude is on it"))
        self.assertNotIn("claimed task:", screen.term.text())

    def test_exits_when_the_terminal_disappears(self):
        screen = self.open()
        os.close(screen.fd)                 # the terminal window closes without sending SIGHUP
        os.kill(screen.pid, signal.SIGCONT)
        deadline = time.time() + 5
        while time.time() < deadline:
            done, _ = os.waitpid(screen.pid, os.WNOHANG)
            if done:
                break
            time.sleep(0.1)
        self.assertTrue(done, "chat screen kept running after its terminal closed")

    def test_light_markdown_is_rendered(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "Run `chat show 5` **now**\n- first item\n```py\nx = 1\n```")
        self.assertTrue(screen.until(lambda: "• first item" in screen.term.text()))
        text = screen.term.text()
        self.assertIn("Run chat show 5 now", text)
        self.assertIn("│ x = 1", text)
        self.assertNotIn("```", text)

    def test_long_agent_messages_fold_and_open(self):
        screen = self.open()
        posted = screen.chat("say", "--from", "claude", "\n".join(f"row {n}" for n in range(40)))
        fold = f"/open {posted['id']}"
        self.assertTrue(screen.until(lambda: fold in screen.term.text()))
        screen.type(fold + "\r")
        self.assertTrue(screen.until(lambda: "row 39" in screen.term.text() and fold not in screen.term.text()))

    def test_find_jumps_to_an_older_message(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "the needle is here")
        for n in range(30):
            screen.chat("say", "--from", "user", f"filler message {n}")
        self.assertTrue(screen.until(lambda: "filler message 29" in screen.term.text()))
        self.assertNotIn("the needle is here", screen.term.text())
        screen.type("/find NEEDLE\r")
        self.assertTrue(screen.until(lambda: "the needle is here" in screen.term.text()
                                     and "Found #1" in screen.term.text()))

    def test_unsent_draft_survives_a_crash(self):
        screen = self.open()
        screen.type("half-written thought")
        drafts = Path(self.data) / "ui" / "drafts"
        saved = lambda: any("half-written thought" in path.read_text() for path in drafts.glob("*.json"))
        self.assertTrue(screen.until(saved, 5))     # the draft saves about a second after the last edit
        os.kill(screen.pid, signal.SIGKILL)
        os.waitpid(screen.pid, 0)
        self.screen = again = ChatScreen(self.data)
        self.assertTrue(again.until(lambda: "Restored your unsent draft" in again.term.text()
                                    and "half-written thought" in again.term.text()))

    def test_new_rule_marks_where_the_user_stopped_reading(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "already read")
        screen.until(lambda: "already read" in screen.term.text())
        screen.type("x\x7f")               # interacting marks everything so far as read
        screen.pump(0.3)
        self.assertEqual(screen.quit(), 0)
        screen.chat("say", "--from", "claude", "arrived while away")
        self.screen = again = ChatScreen(self.data)
        self.assertTrue(again.until(lambda: "arrived while away" in again.term.text()))
        lines = again.term.text().splitlines()
        rule = next(n for n, line in enumerate(lines) if " new " in line and "─" in line)
        self.assertLess(next(n for n, line in enumerate(lines) if "already read" in line), rule)
        self.assertGreater(next(n for n, line in enumerate(lines) if "arrived while away" in line), rule)

    def test_arrivals_while_away_stay_unread_even_after_a_signal(self):
        screen = self.open(60)              # enough history that PageUp really scrolls
        screen.chat("say", "--from", "codex", "seen this one")
        screen.until(lambda: "seen this one" in screen.term.text())
        screen.type("x\x7f")               # an interaction at the bottom: #1 is seen
        screen.pump(0.3)
        screen.chat("say", "--from", "claude", "came in while away")
        screen.until(lambda: "came in while away" in screen.term.text())
        screen.type("\x1b[5~")             # scrolled up: keys here don't mark live arrivals read
        self.assertTrue(screen.until(lambda: "Esc to jump" in screen.term.text()))
        screen.type("y\x7f")
        screen.pump(2.5)                    # past the 2 s write throttle: nothing may be written now
        os.kill(screen.pid, signal.SIGTERM)
        os.waitpid(screen.pid, 0)
        self.screen = again = ChatScreen(self.data)
        self.assertTrue(again.until(lambda: "came in while away" in again.term.text()))
        lines = again.term.text().splitlines()
        rule = next(n for n, line in enumerate(lines) if " new " in line and "─" in line)
        self.assertLess(next(n for n, line in enumerate(lines) if "seen this one" in line), rule)
        self.assertGreater(next(n for n, line in enumerate(lines) if "came in while away" in line), rule)

    def test_find_opens_history_older_than_the_screen_holds(self):
        seed(self.data, 1200)               # the screen keeps 1,000; #5 is outside it
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        lines = log.read_text().splitlines()
        record = json.loads(lines[4])
        record["text"] = "ancient needle here"
        lines[4] = json.dumps(record)
        log.write_text("\n".join(lines) + "\n")
        screen = self.open()
        screen.type("/find ancient needle\r")
        # wait for the whole frame: the match can be on screen before the hint row of the same draw arrives
        self.assertTrue(screen.until(lambda: "ancient needle here" in screen.term.text()
                                     and "History view" in screen.term.text()))
        screen.type("\x1b")
        self.assertTrue(screen.until(lambda: "History view" not in screen.term.text()))

    def test_mentions_notify_the_desktop_only_when_unfocused(self):
        record = os.path.join(self.folder.name, "notified.txt")
        fake = os.path.join(self.folder.name, "fake-notify")
        Path(fake).write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" >> {record}\necho >> {record}\n")
        os.chmod(fake, 0o755)
        screen = self.open(AGENTCHAT_NOTIFY_CMD=fake)
        screen.type("\x1b[O")              # the terminal window loses focus
        screen.chat("say", "--from", "codex", "@user are you there?")
        self.assertTrue(screen.until(lambda: os.path.exists(record) and "are you there" in Path(record).read_text()))
        self.assertIn("codex mentioned you", Path(record).read_text())
        screen.type("\x1b[I")              # focus comes back: no more notifications
        screen.chat("say", "--from", "claude", "@user second ping")
        self.assertTrue(screen.until(lambda: "second ping" in screen.term.text()))
        screen.pump(0.3)
        self.assertNotIn("second ping", Path(record).read_text())
        self.assertNotIn("[O", screen.term.text())   # focus reports never leak into the draft

    def test_modified_key_reports_are_requested_decoded_and_restored(self):
        screen = self.open()
        self.assertTrue(screen.until(lambda: b"\x1b[>1u\x1b[>4;1m" in screen.raw))   # asked for at start
        screen.type("/")
        self.assertTrue(screen.until(lambda: "/reply" in screen.term.text()))
        screen.type("\x1b[27u")            # kitty's Esc closes the menu, as Esc does
        self.assertTrue(screen.until(lambda: "/reply" not in screen.term.text()))
        screen.type("\x7f")                # the "/" that opened it
        screen.type("one")
        screen.type("\x1b[27;2;13~")       # xterm's Shift+Enter
        screen.type("two")
        screen.type("\x1b[106;5u")         # kitty's Ctrl+J
        screen.type("three\x1b[13;3u")     # kitty's Alt+Enter
        self.assertTrue(screen.until(lambda: "three" in screen.term.text()))
        screen.type("\x1b[13u")            # kitty's plain Enter sends
        self.assertTrue(screen.until(lambda: screen.messages()))
        self.assertEqual([m["text"] for m in screen.messages()], ["one\ntwo\nthree"])
        screen.type("\x1b[99;5u")          # kitty's Ctrl+C, twice: quits
        screen.type("\x1b[99;5u")
        self.assertTrue(screen.until(lambda: b"\x1b[<u\x1b[>4m" in screen.raw, 10))     # and restored on the way out

    def test_arrow_keys_sent_as_csi_move_through_the_text(self):
        # Asked for kitty's mode, terminals send arrows as CSI A rather than the SS3 form curses decodes
        screen = self.open()
        self.assertTrue(screen.until(lambda: b"\x1b[>1u" in screen.raw))
        screen.type("one two")
        screen.type("\x1b[1;5D")          # Ctrl+Left: back a word
        screen.type("big ")
        screen.type("\x1b[F\nthree")      # End, a new line
        screen.type("\x1b[A\x1b[H")       # Up, Home
        screen.type("> ")
        screen.type("\x1bOB\x1b[4~")      # Down (SS3), End (numbered)
        screen.type("!\x1b[D\x1b[D\x1b[3~")   # two back, Delete
        self.assertTrue(screen.until(lambda: "three!" not in screen.term.text() and "thre!" in screen.term.text()))
        screen.type("\r")
        self.assertTrue(screen.until(lambda: screen.messages()))
        self.assertEqual([m["text"] for m in screen.messages()], ["> one big two\nthre!"])

    def test_tab_after_reply_fills_in_messages_to_the_user_newest_first(self):
        screen = self.open()
        mine = screen.chat("say", "--from", "user", "a question")["id"]
        mention = screen.chat("say", "--from", "codex", "@user have a look")["id"]
        screen.chat("say", "--from", "codex", "a note for claude")
        answer = screen.chat("say", "--from", "claude", f"↪ #{mine} the answer")["id"]
        self.assertTrue(screen.until(lambda: "the answer" in screen.term.text()))
        def drafted(number):                # the screen trims trailing spaces, so match the number's end
            return re.search(rf"/reply {number}(?!\d)", screen.term.text()) is not None

        screen.type("/rep")
        screen.type("\t")                  # the menu's Tab: /reply, and the newest message to the user
        self.assertTrue(screen.until(lambda: drafted(answer)))
        screen.type("\t")                  # again: the one before, skipping what wasn't for them
        self.assertTrue(screen.until(lambda: drafted(mention)))
        screen.type("\t")                  # and round to the newest
        self.assertTrue(screen.until(lambda: drafted(answer)))
        screen.type("\x7f" * (len(str(answer)) + 1) + f"{mention} thanks\r")
        self.assertTrue(screen.until(lambda: len([m for m in screen.messages() if m["sender"] == "user"]) == 2))
        self.assertEqual([m["text"] for m in screen.messages() if m["sender"] == "user"][-1], f"↪ #{mention} thanks")

    def test_shift_enter_starts_a_new_line_and_enter_alone_sends(self):
        shift = os.path.join(self.folder.name, "shift-held")
        screen = self.open(AGENTCHAT_SHIFT_FILE=shift)
        screen.type("first line")
        Path(shift).touch()                 # Shift is down: this Enter is a new line
        screen.type("\r")
        os.remove(shift)
        screen.type("second line")
        screen.type("\x1b[13;2u")           # a terminal that sends Shift+Enter apart
        screen.type("third line")
        self.assertTrue(screen.until(lambda: "third line" in screen.term.text()))
        self.assertEqual(screen.messages(), [])
        screen.type("\r")                   # Enter alone sends all three lines as one message
        self.assertTrue(screen.until(lambda: screen.messages()))
        self.assertEqual([m["text"] for m in screen.messages()], ["first line\nsecond line\nthird line"])

    def test_two_screens_of_one_chat_pop_up_once_and_not_while_either_is_focused(self):
        record = os.path.join(self.folder.name, "notified.txt")
        fake = os.path.join(self.folder.name, "fake-notify")
        Path(fake).write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" >> {record}\necho >> {record}\n")
        os.chmod(fake, 0o755)
        first = self.open(AGENTCHAT_NOTIFY_CMD=fake)
        second = ChatScreen(self.data, AGENTCHAT_NOTIFY_CMD=fake)
        self.others.append(second)
        for screen in (first, second):
            self.assertTrue(screen.until(lambda: screen.term.text().strip()))     # drawn and listening
            screen.type("\x1b[O")         # both terminal windows lose focus
        first.chat("say", "--from", "codex", "@user one ping")
        self.assertTrue(first.until(lambda: "one ping" in first.term.text()) and
                        second.until(lambda: "one ping" in second.term.text()))
        first.pump(0.5)
        second.pump(0.5)
        self.assertEqual(Path(record).read_text().count("one ping"), 1)
        second.type("\x1b[I")             # the user looks at the other screen
        second.pump(0.3)
        first.chat("say", "--from", "claude", "@user while you look")
        self.assertTrue(first.until(lambda: "while you look" in first.term.text()))
        first.pump(0.5)
        self.assertNotIn("while you look", Path(record).read_text())

    def test_window_title_counts_messages_while_away(self):
        screen = self.open()
        screen.type("\x1b[O")
        screen.chat("say", "--from", "codex", "one")
        screen.chat("say", "--from", "claude", "two")
        self.assertTrue(screen.until(lambda: b"\x1b]0;AgentChat (2)\x07" in screen.raw))
        screen.type("\x1b[I")
        self.assertTrue(screen.until(lambda: screen.raw.rfind(b"\x1b]0;AgentChat\x07")
                                     > screen.raw.rfind(b"AgentChat (2)")))

    def test_close_messages_from_one_agent_share_a_header(self):
        screen = self.open()
        long = "a sentence that is long enough to fill the whole first row of the transcript at eighty columns"
        ids = [screen.chat("say", "--from", "codex", text)["id"] for text in ("first", long, "third " + long)]
        screen.chat("say", "--from", "claude", "different sender")
        self.assertTrue(screen.until(lambda: "different sender" in screen.term.text()))
        text = screen.term.text()
        self.assertEqual(text.count("● codex"), 1)
        self.assertEqual(text.count("● claude"), 1)
        for message_id in ids:                       # every message keeps its own number
            self.assertIn(f"#{message_id} ", text)

    def test_reply_quotes_its_target_without_borrowing_its_mentions(self):
        screen = self.open()
        original = screen.chat("say", "--from", "codex", "@user should we ship the store fix?")
        screen.until(lambda: "ship the store fix" in screen.term.text())
        screen.type(f"/reply {original['id']} yes, ship it\r")
        self.assertTrue(screen.until(lambda: screen.messages()[-1]["sender"] == "user"))
        self.assertEqual(screen.messages()[-1]["text"], f"↪ #{original['id']} yes, ship it")
        self.assertTrue(screen.until(lambda: f"↪ #{original['id']} codex: @user should we ship" in screen.term.text()))
        screen.chat("say", "--from", "claude", f"↪ #{original['id']} agreed")
        self.assertTrue(screen.until(lambda: "agreed" in screen.term.text()))
        self.assertEqual(screen.term.text().count("→ you"), 1)    # only codex's original mentioned the user

    def test_oversized_reply_reference_is_plain_text(self):
        screen = self.open()
        posted = screen.chat("say", "--from", "codex", "↪ #" + "9" * 5000 + " still readable")
        self.assertTrue(screen.until(lambda: f"/open {posted['id']}" in screen.term.text()))   # long: folded
        self.assertIsNone(os.waitpid(screen.pid, os.WNOHANG)[0] or None)   # the screen is still running

    def test_touchpad_scrolls_the_chat_not_the_history(self):
        screen = self.open(60)
        screen.chat("say", "--from", "user", "an earlier message of mine")
        screen.until(lambda: "an earlier message of mine" in screen.term.text())
        self.assertIn(b"\x1b[?1006", screen.raw)              # mouse reporting is on
        screen.type("\x1b[<64;10;5M\x1b[<64;10;5M")          # two wheel-up steps
        self.assertTrue(screen.until(lambda: "Esc to jump" in screen.term.text()))
        self.assertNotIn("❯ an earlier message of mine", screen.term.text().splitlines()[-3])  # draft untouched
        for _ in range(4):
            screen.type("\x1b[<65;10;5M")                     # wheel down back to the bottom
        self.assertTrue(screen.until(lambda: "Esc to jump" not in screen.term.text()))

    def test_light_terminal_gets_the_light_palette(self):
        self.screen = screen = ChatScreen(self.data, background="ffff/ffff/ffff")
        screen.type("hello\r")
        self.assertTrue(screen.until(lambda: b"48;5;254" in screen.raw))   # light band behind your message
        self.assertNotIn(b"48;5;236", screen.raw)

    def test_dark_terminal_gets_the_dark_palette(self):
        self.screen = screen = ChatScreen(self.data, background="1616/1616/1d1d")
        screen.type("hello\r")
        self.assertTrue(screen.until(lambda: b"48;5;236" in screen.raw))

    def test_late_colour_reply_never_reaches_the_draft(self):
        screen = self.open()
        screen.type("\x1b]11;rgb:0000/0000/0000\x07")
        screen.type("x")
        self.assertTrue(screen.until(lambda: "❯ x" in screen.term.text()))
        self.assertNotIn("rgb:", screen.term.text())

    def test_mode_commands_update_the_footer_at_once(self):
        screen = self.open()
        footer = lambda: screen.term.text().splitlines()[-1]
        for command, expect in [("/custom 10 1 60", "custom · 0/10 per min until"), ("/pause", "paused ·"),
                                ("/resume", "custom · "), ("/custom off", "normal · 0/4 per min")]:
            screen.type(command + "\r")
            self.assertTrue(screen.until(lambda: expect in footer(), 5), f"{command}: {footer()}")

    def test_export_writes_a_markdown_range_and_who_counts_unread(self):
        exports = os.path.join(self.folder.name, "exports")
        screen = self.open(AGENTCHAT_EXPORTS=exports)
        for text in ("first", "second", "third"):
            screen.chat("say", "--from", "codex", text)
        screen.until(lambda: "third" in screen.term.text())
        screen.type("/export 2-3\r")
        self.assertTrue(screen.until(lambda: "Saved to" in screen.term.text()))
        [name] = os.listdir(exports)
        text = Path(exports, name).read_text()
        self.assertIn("**#2 codex**", text)
        self.assertIn("third", text)
        self.assertNotIn("first", text)
        screen.type("/who\r")
        self.assertTrue(screen.until(lambda: "claude:" in screen.term.text() and "unread" in screen.term.text()))
        self.assertIn("claude: ", screen.term.text())
        self.assertIn("3 unread", screen.term.text())          # claude hasn't acked codex's three messages

    def test_slash_opens_a_command_menu(self):
        screen = self.open()
        screen.type("/")
        self.assertTrue(screen.until(lambda: "/reply N text" in screen.term.text()
                                     and "answer message N" in screen.term.text()))
        screen.type("re")                   # narrows to /reply, /resume, /release
        self.assertTrue(screen.until(lambda: "/find" not in screen.term.text() and "/resume" in screen.term.text()))
        screen.type("\x1bOB")               # ↓ (keypad mode, as the terminal sends it) selects the second match
        screen.type("\t")                   # Tab fills it in
        # filled in with a trailing space (screen lines are compared right-trimmed), so the menu closes
        self.assertTrue(screen.until(lambda: "❯ /resume" in screen.term.text()
                                     and "let agents post again" not in screen.term.text()))
        screen.type("\x15/wh\r")            # Enter on a no-argument command runs it
        self.assertTrue(screen.until(lambda: "unread" in screen.term.text()))

    def test_find_highlights_the_words_until_esc(self):
        screen = self.open()
        screen.chat("say", "--from", "codex", "the needle is here")
        screen.until(lambda: "the needle is here" in screen.term.text())
        screen.type("/find needle\r")
        self.assertTrue(screen.until(lambda: b"48;5;220" in screen.raw and "Found #1" in screen.term.text()))
        mark = len(screen.raw)
        screen.type("\x1b")
        self.assertTrue(screen.until(lambda: b"needle" in screen.raw[mark:]))   # redrawn without the yellow
        screen.pump(0.3)
        self.assertNotIn(b"48;5;220", screen.raw[mark:])

    def test_a_reply_to_the_user_counts_as_addressed_to_them(self):
        screen = self.open()
        mine = screen.chat("say", "--from", "user", "which theme should we use?")
        screen.chat("say", "--from", "codex", f"↪ #{mine['id']} the dark one")
        self.assertTrue(screen.until(lambda: "the dark one" in screen.term.text()
                                     and f"↪ #{mine['id']} you: which theme" in screen.term.text()
                                     and "→ you" in screen.term.text()))

    def journal_shim(self):
        """Paths of a gate file (blocks the journal write while present) and a fail file (makes it raise)."""
        shim = os.path.join(self.folder.name, "shim")
        os.makedirs(shim)
        Path(shim, "sitecustomize.py").write_text(JOURNAL_SHIM)
        gate, fail = os.path.join(shim, "gate"), os.path.join(shim, "fail")
        return dict(PYTHONPATH=shim, AGENTCHAT_TEST_GATE=gate, AGENTCHAT_TEST_FAIL=fail), gate, fail

    def test_a_stalled_send_keeps_the_screen_responsive(self):
        env, gate, _ = self.journal_shim()
        Path(gate).touch()                  # the disk "stalls" inside the pending-journal write
        screen = self.open(60, **env)
        screen.type("hello\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        start = time.time()
        screen.type("\x1b[5~")              # the screen still scrolls while the send is stuck
        self.assertTrue(screen.until(lambda: "Esc to jump" in screen.term.text(), 3))
        self.assertLess(time.time() - start, 1.0)
        self.assertFalse([m for m in screen.messages() if m["text"] == "hello"])
        os.remove(gate)                     # the disk recovers
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()].count("hello") == 1))
        self.assertTrue(screen.until(lambda: "sending…" not in screen.term.text()))
        screen.pump(0.5)
        self.assertEqual([m["text"] for m in screen.messages()].count("hello"), 1)   # exactly once

    def test_a_slow_send_leaves_no_ghost_draft(self):
        env, gate, _ = self.journal_shim()
        Path(gate).touch()
        screen = self.open(**env)
        screen.type("ghost check\r")       # Enter before the 1 s draft save has run
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.pump(2.0)                    # the save delay passes while the send is stuck
        os.remove(gate)
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()] == ["ghost check"]))
        self.assertTrue(screen.until(lambda: "sending…" not in screen.term.text()))
        screen.pump(1.5)                    # give any wrongly queued save time to land
        os.kill(screen.pid, signal.SIGKILL)
        os.waitpid(screen.pid, 0)
        self.screen = again = ChatScreen(self.data, **env)
        again.pump(1.5)
        self.assertNotIn("Restored your unsent draft", again.term.text())
        composer = again.term.text().splitlines()[-3]    # the input box line, not the transcript
        self.assertIn("Message claude and codex", composer)

    def test_history_recall_waits_while_a_send_is_in_flight(self):
        env, gate, _ = self.journal_shim()
        screen = self.open(**env)
        screen.chat("say", "--from", "user", "an older message")
        screen.until(lambda: "an older message" in screen.term.text())
        Path(gate).touch()
        screen.type("new one\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.type("\x1bOA\t\x1b\x1b")      # ↑ recall, Tab, double Esc: none may change the draft now
        screen.pump(0.5)
        self.assertIn("❯ new one", screen.term.text().splitlines()[-3])
        os.remove(gate)
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()][-1] == "new one"))
        self.assertTrue(screen.until(lambda: "Message claude and codex" in screen.term.text().splitlines()[-3]))

    def test_quitting_during_a_stuck_send_waits_then_exits(self):
        env, gate, _ = self.journal_shim()
        Path(gate).touch()
        screen = self.open(**env)
        screen.type("keep me safe\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.type("\x03\x03", 0)
        self.assertTrue(screen.until(lambda: "still saving your message" in screen.term.text()))
        screen.pump(16)                     # over 15 s: the screen keeps waiting rather than quit and lose it
        self.assertEqual(os.waitpid(screen.pid, os.WNOHANG)[0], 0, "quit before the send was saved")
        self.assertIn("still saving your message", screen.term.text())
        os.remove(gate)                     # the disk recovers: the screen finishes and quits by itself
        self.assertEqual(screen.quit_wait(), 0)
        self.assertEqual([m["text"] for m in screen.messages()], ["keep me safe"])

    def test_forcing_quit_during_a_stuck_send_prints_the_unsent_text(self):
        env, gate, _ = self.journal_shim()
        Path(gate).touch()
        screen = self.open(**env)
        big = "\n".join(f"pasted line {n} " + "y" * 50 for n in range(40))
        screen.type("do not lose this ")
        screen.type("\x1b[200~" + big + "\x1b[201~")
        self.assertTrue(screen.until(lambda: "[Pasted text #1" in screen.term.text()))
        screen.type("\r")
        self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
        screen.type("\x03\x03", 0)
        self.assertTrue(screen.until(lambda: "still saving your message" in screen.term.text()))
        screen.type("\x03", 0)              # quit anyway
        self.assertEqual(screen.quit_wait(), 0)
        tail = screen.raw.decode("utf-8", "replace")
        self.assertIn("NOT sent or saved", tail)
        printed = tail.split("NOT sent or saved")[-1]
        self.assertIn("do not lose this", printed)
        self.assertIn("pasted line 39", printed)          # the paste itself, not its chip label
        self.assertNotIn("[Pasted text #1", printed)
        os.remove(gate)

    def test_signals_during_a_stuck_send_print_the_unsent_text(self):
        env, gate, _ = self.journal_shim()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            Path(gate).touch()
            self.screen = screen = ChatScreen(self.data, **env)
            screen.type(f"text before {sig.name}\r")
            self.assertTrue(screen.until(lambda: "sending…" in screen.term.text()))
            os.kill(screen.pid, sig)
            self.assertEqual(screen.quit_wait(), 0, f"{sig.name}: did not exit promptly")
            printed = screen.raw.decode("utf-8", "replace").split("NOT sent or saved")[-1]
            self.assertIn(f"text before {sig.name}", printed, sig.name)
            os.remove(gate)
            os.close(screen.fd)
            self.screen = None

    def test_fresh_text_survives_quit_and_is_printed_on_a_signal(self):
        screen = self.open()
        screen.type("fresh words")          # quit before the 1 s draft save
        screen.type("\x03\x03", 0)
        self.assertEqual(screen.quit_wait(), 0)
        os.close(screen.fd)
        self.screen = again = ChatScreen(self.data)
        self.assertTrue(again.until(lambda: "Restored your unsent draft" in again.term.text()
                                    and "fresh words" in again.term.text()))
        again.type("\x15more fresh words")
        os.kill(again.pid, signal.SIGTERM)  # a signal before the save: printed, never written in place
        self.assertEqual(again.quit_wait(), 0)
        self.assertIn("more fresh words", again.raw.decode("utf-8", "replace").split("NOT sent or saved")[-1])

    def test_a_failed_final_save_keeps_the_screen_open(self):
        env, _, fail = self.journal_shim()
        Path(fail + "-saves").touch()
        screen = self.open(**env)
        screen.type("unsaved thought")
        screen.type("\x03\x03", 0)         # quit before the debounce: the final save is queued and fails
        self.assertTrue(screen.until(lambda: "Draft not saved: injected save failure" in screen.term.text()))
        screen.pump(1.0)
        self.assertEqual(os.waitpid(screen.pid, os.WNOHANG)[0], 0, "quit with unsaved text")
        self.assertIn("❯ unsaved thought", screen.term.text())
        os.remove(fail + "-saves")
        screen.type("\x03\x03", 0)         # retry: now it saves and quits
        self.assertEqual(screen.quit_wait(), 0)
        os.close(screen.fd)
        self.screen = again = ChatScreen(self.data, **env)
        self.assertTrue(again.until(lambda: "unsaved thought" in again.term.text()
                                    and "Restored your unsent draft" in again.term.text()))

    def test_a_failed_journal_write_keeps_the_draft_for_retry(self):
        env, _, fail = self.journal_shim()
        Path(fail).touch()
        screen = self.open(**env)
        screen.type("retry me\r")
        self.assertTrue(screen.until(lambda: "Not sent: injected journal failure" in screen.term.text()
                                     and "❯ retry me" in screen.term.text()))
        self.assertEqual(screen.messages(), [])
        os.remove(fail)
        screen.type("\r")
        self.assertTrue(screen.until(lambda: [m["text"] for m in screen.messages()] == ["retry me"]))

    def test_clocks_tick_without_typing(self):
        screen = self.open()
        screen.chat("status", "--as", "claude", "working")
        screen.chat("custom", "1", "1", "5")     # 1 agent message per minute: the second post is refused
        screen.chat("say", "--from", "codex", "first")
        screen.chat("say", "--from", "codex", "second")
        self.assertTrue(screen.until(lambda: "working" in screen.term.text() and "agents wait" in screen.term.text()))
        def seconds(pattern):
            found = re.search(pattern, screen.term.text())
            return int(found.group(1)) if found else None
        first_work, first_wait = seconds(r"working · (\d+)s"), seconds(r"agents wait (\d+)s")
        screen.pump(3.2)                    # no keys pressed
        self.assertGreaterEqual(seconds(r"working · (\d+)s"), first_work + 2)
        self.assertLessEqual(seconds(r"agents wait (\d+)s"), first_wait - 2)

    def test_paused_and_wait_modes_stay_idle(self):
        screen = self.open()
        for mode in ("/pause", "/mode wait"):
            screen.type(mode + "\r")
            screen.pump(1.5)
            ticks = lambda: sum(map(int, Path(f"/proc/{screen.pid}/stat").read_text().split()[13:15]))
            before = ticks()
            screen.pump(4.0)                # no keys: a refresh loop would burn CPU here
            self.assertLess(ticks() - before, 40, f"{mode}: busy while idle")

    def test_two_press_hints_expire_without_typing(self):
        screen = self.open()
        screen.type("\x03")                 # one Ctrl+C
        self.assertTrue(screen.until(lambda: "ctrl+c again to quit" in screen.term.text()))
        self.assertTrue(screen.until(lambda: "ctrl+c again to quit" not in screen.term.text(), 5))

    def test_quit_restores_the_terminal(self):
        screen = self.open()
        screen.chat("say", "--from", "claude", "a message to keep")
        screen.until(lambda: "a message to keep" in screen.term.text())
        self.assertEqual(screen.quit(), 0)
        tail = screen.raw.decode("utf-8", "replace")
        self.assertIn("\x1b[?2004l", tail)
        self.assertIn("a message to keep", tail.split("AgentChat · last messages")[-1])



class LatencyTests(ScreenCase):
    """Timing-sensitive: run alone, not in parallel with other suites.

    Functional:  python3 -I tests/test_ui.py --functional   (every class except this one)
    Latency:     python3 -I tests/test_ui.py --latency
    """

    def test_keystrokes_stay_fast_with_long_history(self):
        screen = self.open(3000)
        delays = []
        for ch in "quick brown fox":
            before = len(screen.raw)
            start = time.time()
            os.write(screen.fd, ch.encode())
            while len(screen.raw) == before and time.time() - start < 2:
                screen.pump(0.002)
            delays.append(time.time() - start)
            screen.pump(0.02)
        self.assertLess(statistics.median(delays), 0.05, delays)


def _selected(latency):
    """Every test class in this module: only LatencyTests, or everything else. New classes join automatically."""
    loader, suite = unittest.TestLoader(), unittest.TestSuite()
    for case in globals().values():
        if isinstance(case, type) and issubclass(case, unittest.TestCase) and case is not ScreenCase:
            if issubclass(case, LatencyTests) == latency:
                suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


if __name__ == "__main__":
    import sys
    if sys.argv[1:] in (["--functional"], ["--latency"]):
        result = unittest.TextTestRunner().run(_selected(sys.argv[1] == "--latency"))
        sys.exit(0 if result.wasSuccessful() else 1)
    unittest.main()                         # no arguments: everything
