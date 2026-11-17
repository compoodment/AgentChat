"""The input box: keys, paste, chips, history, completion and commands."""

import curses
import difflib
import os
import re
import select
import sys
import time
from datetime import datetime

from agentchat import store
from agentchat.ui import keys as modified
from agentchat.ui.shiftkey import shift_held
from agentchat.ui.text import REPLY, cell_width, clean, duration, fit, parse_duration


QUIT_WINDOW = 2             # seconds for the second Ctrl+C
MENU_ROWS = 6               # commands shown at once in the / menu
COMMAND_HELP = {            # the / menu: command -> (argument hint, what it does); order is menu order
    "/reply": ("N text", "answer message N; Tab: the newest to you"), "/find": ("words", "jump to older messages"),
    "/open": ("N", "unfold long message N"), "/export": ("[A-B]", "save the chat as Markdown"),
    "/custom": ("N M D | off", "N agent messages per M min, for D min"),
    "/mode": ("normal | wait", "default rate, or wait for you"), "/pause": ("", "stop agent posts"),
    "/resume": ("", "let agents post again"), "/who": ("", "agents' state and unread counts"),
    "/mouse": ("on | off", "scroll the chat, or select text normally"),
    "/claim": ("path", "claim a shared file"), "/release": ("path", "release a claimed file"),
    "/claims": ("", "list claimed files"), "/help": ("", "keys and commands"), "/quit": ("", "leave"),
}
WHEEL_ROWS = 3              # rows per touchpad/wheel step
NAMES = ("@claude", "@codex", "@all")
COMMANDS = ("/claim", "/custom", "/export", "/release", "/claims", "/find", "/mode", "/mouse", "/open", "/pause", "/reply",
            "/resume", "/who", "/help", "/quit")
PASTE_START, PASTE_END = "[200~", "[201~"
REPLY_DRAFT = re.compile(r"/reply(?:\s+#?(\d+))?\s*")   # what Tab fills in /reply's number for
CHIP_CHARS, CHIP_LINES = 1000, 12    # larger pastes show as a chip and expand on send


class Composer:
    """The draft: keys, bracketed paste, chips, history, completion and sending. Mixed into Screen."""

    def composer_lines(self, width):
        """Visual rows of the draft and the cursor's (row, col)."""
        room = width - 2
        rows, cursor = [], None
        position = 0
        for line in self.draft.split("\n"):
            chunk, used, start = "", 0, position
            for offset, ch in enumerate(line + "\0"):
                if offset == len(line):
                    if position + offset == self.cursor and cursor is None:
                        cursor = (len(rows), used)
                    break
                size = cell_width(ch)
                if used + size > room:
                    rows.append(chunk)
                    chunk, used = "", 0
                if position + offset == self.cursor and cursor is None:
                    cursor = (len(rows), used)
                chunk += ch
                used += size
            rows.append(chunk)
            position = start + len(line) + 1
        return rows, cursor or (len(rows) - 1, 0)


    # -- input ------------------------------------------------------------

    def read_keys(self):
        while True:
            try:
                self.pending.append(self.win.get_wch())
            except curses.error:
                break

    def handle_pending(self):
        """Handle buffered keys. Returns False to quit."""
        keys = self.pending
        index = 0
        while index < len(keys):
            key = keys[index]
            if self.pasting is not None:
                if key == "\x1b" and "".join(str(k) for k in keys[index + 1:index + 6]) == PASTE_END:
                    self.paste("".join(self.pasting).replace("\r\n", "\n").replace("\r", "\n"))
                    self.interacted = True
                    self.pasting = None
                    index += 6
                    continue
                if key == "\x1b" and len(keys) - index < 6:
                    break                   # wait for the rest of a possible end marker
                if isinstance(key, str):
                    self.pasting.append(key)
                index += 1
                continue
            if key == "\x1b":
                rest = keys[index + 1:]
                started = "".join(k for k in rest[:5] if isinstance(k, str))
                if len(rest) < 5 and PASTE_START.startswith(started):
                    if self.more_input():
                        keys = self.pending = keys + self.pending_tail
                        continue            # a marker split across reads: look again with the rest
                    if started:
                        break               # "ESC[2..." so far: keep it until the rest of the marker arrives
                if "".join(str(k) for k in rest[:5]) == PASTE_START:
                    self.pasting = []
                    index += 6
                    continue
                alt = self.alt_keys().get(rest[0]) if rest else None
                if alt:                     # Alt+Enter new line, Alt+B/F word moves, Alt+Backspace delete word
                    alt()
                    self.interacted = True
                    index += 2
                    continue
                if rest and rest[0] == "]":         # a late OSC reply (e.g. the colour query): swallow it
                    end = index + 2
                    while end < len(keys) and keys[end] != "\x07" and not (keys[end] == "\\" and keys[end - 1] == "\x1b"):
                        end += 1
                    if end >= len(keys):
                        break                       # wait for the terminator
                    index = end + 1
                    continue
                if rest[:2] in (["[", "I"], ["[", "O"]):
                    self.focus(rest[1] == "I")      # raw focus report from the terminal
                    index += 3
                    continue
                ss3 = rest[:1] == ["O"] and len(rest) > 1 and rest[1] in ("A", "B", "C", "D", "H", "F")
                if rest and (rest[0] == "[" or ss3):    # CSI, or SS3 cursor keys curses didn't decode
                    end = index + 2
                    while not ss3 and end < len(keys) and not (isinstance(keys[end], str) and "@" <= keys[end] <= "~"):
                        end += 1
                    if end >= len(keys) and self.more_input():
                        keys = self.pending = keys + self.pending_tail
                        continue            # a report split across reads: look again with the rest
                    decoded = modified.decode("".join(str(k) for k in keys[index + 1:end + 1]))
                    index = end + 1         # an unknown escape sequence is ignored
                    if decoded is None:
                        continue
                    self.interacted = True
                    if decoded[0] == "newline":         # Shift/Alt/Ctrl+Enter from a terminal that tells them apart
                        self.insert("\n")
                    elif decoded[0] == "escape":
                        self.escape()
                    elif decoded[0] == "alt":
                        action = self.alt_keys().get(decoded[1])
                        if action:
                            action()
                    elif not self.key(decoded[1]):
                        self.pending = []
                        return False
                    continue
                self.escape()
                self.interacted = True
                index += 1
                continue
            if not self.key(key):
                self.pending = []
                return False
            index += 1
        self.pending = keys[index:]
        return True

    def alt_keys(self):
        newline = lambda: self.insert("\n")
        return {"\r": newline, "\n": newline, curses.KEY_ENTER: newline, "b": self.word_left,
                "f": self.word_right, "\x7f": self.delete_word, curses.KEY_BACKSPACE: self.delete_word}

    def more_input(self):
        """Wait briefly for the rest of an escape sequence. True if more keys arrived."""
        self.pending_tail = []
        if not select.select([sys.stdin], [], [], 0.03)[0]:
            return False
        while True:
            try:
                self.pending_tail.append(self.win.get_wch())
            except curses.error:
                break
        return bool(self.pending_tail)

    def wheel(self):
        try:
            _, _, _, _, buttons = curses.getmouse()
        except curses.error:
            return
        if buttons & curses.BUTTON4_PRESSED:
            self.scroll += WHEEL_ROWS
        elif buttons & curses.BUTTON5_PRESSED:
            self.scroll = max(0, self.scroll - WHEEL_ROWS)
            if not self.scroll:
                self.unseen = {}

    def set_mouse(self, on):
        """Wheel events scroll the chat; while on, selecting text with the mouse needs Shift+drag."""
        self.mouse = on
        curses.mousemask(curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED if on else 0)

    def focus(self, gained):
        """The terminal window gained or lost focus. Gaining it counts as the user looking."""
        self.focused = gained
        self.desktop.focused(gained)
        if gained:
            self.interacted, self.away = True, 0

    def paste(self, text):
        """Insert pasted text; a large paste becomes a chip so the composer stays fast."""
        if self.busy():                     # before any chip bookkeeping
            return
        lines = text.count("\n") + 1
        if len(text) <= CHIP_CHARS and lines <= CHIP_LINES:
            self.insert(text)
            return
        self.chip_count += 1
        label = f"[Pasted text #{self.chip_count} · {lines} lines]" if lines > 1 else \
                f"[Pasted text #{self.chip_count} · {len(text)} chars]"
        self.chips[label] = text
        self.insert(label)

    def expand(self, text):
        """Replace chip labels with their pasted text in one pass, so pasted text is never re-expanded."""
        if not self.chips:
            return text
        pattern = "|".join(re.escape(label) for label in sorted(self.chips, key=len, reverse=True))
        return re.sub(pattern, lambda match: self.chips[match.group(0)], text)

    def chip_spans(self):
        spans = []
        for label in self.chips:
            start = self.draft.find(label)
            while start >= 0:
                spans.append((start, start + len(label)))
                start = self.draft.find(label, start + 1)
        return spans

    def snap(self, position, forward=True):
        """Move a position out of the inside of a chip, so chips edit as one unit."""
        for start, end in self.chip_spans():
            if start < position < end:
                return end if forward else start
        return position

    def cut(self, start, end):
        """Delete draft[start:end], widened to whole chips; forget chips no longer in the draft."""
        if self.busy():
            return
        for chip_start, chip_end in self.chip_spans():
            if chip_start < end and start < chip_end:
                start, end = min(start, chip_start), max(end, chip_end)
        self.draft = self.draft[:start] + self.draft[end:]
        self.cursor = start
        self.chips = {label: text for label, text in self.chips.items() if label in self.draft}

    def busy(self):
        """True (with a note) while a send is in flight: the draft being sent can't change underneath it."""
        if self.sending:
            if not any(n[1] == "sending" for n in self.notes):
                self.notes.append(("sending…", "sending", None))
            return True
        return False

    def insert(self, text):
        if self.busy():
            return
        self.menu_index, self.menu_closed = 0, False
        self.cursor = self.snap(self.cursor)
        self.draft = self.draft[:self.cursor] + text + self.draft[self.cursor:]
        self.cursor += len(text)
        self.history_pos = None

    def word_left(self):
        while self.cursor and self.draft[self.cursor - 1].isspace():
            self.cursor -= 1
        while self.cursor and not self.draft[self.cursor - 1].isspace():
            self.cursor -= 1

    def word_right(self):
        size = len(self.draft)
        while self.cursor < size and self.draft[self.cursor].isspace():
            self.cursor += 1
        while self.cursor < size and not self.draft[self.cursor].isspace():
            self.cursor += 1

    def delete_word(self):
        end = self.cursor
        self.word_left()
        self.cut(self.cursor, end)

    def menu(self):
        """Commands matching the draft while a command name is being typed, else []."""
        if (self.menu_closed or not self.draft.startswith("/") or self.draft.startswith("//")
                or any(c.isspace() for c in self.draft)):
            return []
        return [name for name in COMMAND_HELP if name.startswith(self.draft.lower())]

    def menu_pick(self, run):
        """Fill in the selected command; with run=True, also run it when it takes no arguments."""
        if self.busy():
            return True
        options = self.menu()
        name = options[min(self.menu_index, len(options) - 1)]
        if run and not COMMAND_HELP[name][0]:
            self.draft, self.cursor = name, len(name)
            return self.submit(name)
        self.draft, self.cursor, self.menu_index = name + " ", len(name) + 1, 0
        if name == "/reply" and not run:     # Tab: ready to answer the newest message to the user
            self.pick_reply()
        return True

    def pick_reply(self):
        """Fill in /reply's number: the newest message that mentions the user or answers them, and on each
        further Tab the one before it."""
        targets = [message for message in reversed(self.messages)
                   if message["kind"] == "msg" and message["sender"] != store.USER and self.addressed_to_me(message)]
        if not targets:
            self.note("Nothing mentions or answers you yet")
            return
        shown = REPLY_DRAFT.fullmatch(self.draft)
        current = int(shown.group(1)) if shown and shown.group(1) else None
        numbers = [message["id"] for message in targets]
        target = targets[(numbers.index(current) + 1) % len(targets)] if current in numbers else targets[0]
        self.draft = f"/reply {target['id']} "
        self.cursor = len(self.draft)
        self.note(f"#{target['id']} {target['sender']}: {' '.join(REPLY.sub('', clean(target['text'])).split())}")

    def set_highlight(self, words):
        if words != self.highlight:
            self.highlight = words
            self.cache.clear()              # rows were drawn with the old highlight

    def escape(self):
        now = time.time()
        self.set_highlight(None)
        if self.help:
            self.help = False
        elif self.menu():
            self.menu_closed = True         # hide the list, keep the text
        elif self.view is not None:
            self.view, self.scroll, self.unseen = None, 0, {}
        elif self.scroll:
            self.scroll, self.unseen = 0, {}
        elif self.draft and now - self.esc_at < QUIT_WINDOW and not self.busy():
            self.stash, self.draft, self.cursor = self.draft, "", 0
            self.note("Draft cleared · ↑ brings it back")
            self.esc_at = 0
            return
        self.notes = []
        self.esc_at = now

    def _line_bounds(self):
        start = self.draft.rfind("\n", 0, self.cursor) + 1
        end = self.draft.find("\n", self.cursor)
        return start, len(self.draft) if end < 0 else end

    def vertical(self, step):
        start, end = self._line_bounds()
        column = self.cursor - start
        if step < 0 and start > 0:
            above = self.draft.rfind("\n", 0, start - 1) + 1
            self.cursor = min(above + column, start - 1)
            return
        if step > 0 and end < len(self.draft):
            below_end = self.draft.find("\n", end + 1)
            below_end = len(self.draft) if below_end < 0 else below_end
            self.cursor = min(end + 1 + column, below_end)
            return
        self.recall(step)

    def recall(self, step):
        if self.busy():
            return
        if not self.history and not self.stash:
            return
        entries = self.history + ([self.stash] if self.stash and self.stash not in self.history[-1:] else [])
        if self.history_pos is None:
            if step > 0:
                return
            self.saved_draft = self.draft
            self.history_pos = len(entries)
        self.history_pos += step
        if self.history_pos >= len(entries):
            self.history_pos = None
            self.draft = self.saved_draft
        else:
            self.history_pos = max(0, self.history_pos)
            self.draft = entries[self.history_pos]
        self.cursor = len(self.draft)

    def complete(self):
        if self.busy():
            return
        if self.cursor == len(self.draft) and REPLY_DRAFT.fullmatch(self.draft):
            self.pick_reply()
            return
        start = self.cursor
        while start and not self.draft[start - 1].isspace():
            start -= 1
        token = self.draft[start:self.cursor]
        if token.startswith("@"):
            pool = NAMES
        elif token.startswith("/") and start == 0:
            pool = COMMANDS
        else:
            return
        matches = [option for option in pool if option.startswith(token.lower())]
        if len(matches) == 1:
            self.draft = self.draft[:start] + matches[0] + " " + self.draft[self.cursor:]
            self.cursor = start + len(matches[0]) + 1
        elif matches:
            common = os.path.commonprefix(matches)
            self.draft = self.draft[:start] + common + self.draft[self.cursor:]
            self.cursor = start + len(common)
            self.note("  ".join(matches))

    def key(self, key):
        """Handle one key. Returns False to quit."""
        height = self.win.getmaxyx()[0]
        if isinstance(key, int) and key > 255:
            try:
                name = curses.keyname(key)
            except curses.error:            # no curses screen (unit tests)
                name = b""
            if name in (b"kxIN", b"kxOUT"):
                self.focus(name == b"kxIN")     # terminfo-decoded focus report
                return True
            word = {b"kLFT3": self.word_left, b"kLFT5": self.word_left, b"kRIT3": self.word_right,
                    b"kRIT5": self.word_right}.get(name)
            if word:                        # Alt/Ctrl+Left/Right, as curses decodes them: a word at a time
                self.interacted = True
                word()
                return True
        self.interacted = True
        if key == curses.KEY_MOUSE:          # touchpad / wheel: scroll the transcript, not the history
            self.wheel()
            return True
        if key not in ("\x03",) and self.notes and self.notes[0][2] is not None:
            self.notes = []
        if key == "\x03":                   # Ctrl+C
            if self.closing:                # already waiting for the disk: this one forces the quit
                self.force_quit = True
                return False
            if time.time() - self.ctrl_c_at < QUIT_WINDOW:
                return False
            self.ctrl_c_at = time.time()
            return True
        if key == "?" and not self.draft:
            self.help = not self.help
            return True
        self.help = False
        options = self.menu()
        if options and key in (curses.KEY_UP, curses.KEY_DOWN):
            step = 1 if key == curses.KEY_DOWN else -1
            self.menu_index = (min(self.menu_index, len(options) - 1) + step) % len(options)
            return True
        if options and key in ("\t", "\r", curses.KEY_ENTER):
            self.menu_pick(run=key != "\t")
            return self.running
        if key in ("\r", curses.KEY_ENTER):
            if self.busy():
                return True
            if shift_held():                # Shift+Enter: a new line (the terminal sends it as plain Enter)
                self.insert("\n")
                return True
            text = self.draft.strip("\n").rstrip()  # keep leading spaces: pasted code stays indented
            if text.strip():
                self.submit(text)
            return self.running
        if key == "\n":                     # Ctrl+J
            self.insert("\n")
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if self.cursor:                 # a chip goes in one press
                self.cut(self.cursor - 1, self.cursor)
        elif key == curses.KEY_DC or key == "\x04":
            if self.cursor < len(self.draft):
                self.cut(self.cursor, self.cursor + 1)
        elif key == curses.KEY_LEFT:
            self.cursor = self.snap(max(0, self.cursor - 1), forward=False)
        elif key == curses.KEY_RIGHT:
            self.cursor = self.snap(min(len(self.draft), self.cursor + 1))
        elif key in (curses.KEY_HOME, "\x01"):
            self.cursor = self._line_bounds()[0]
        elif key in (curses.KEY_END, "\x05"):
            self.cursor = self._line_bounds()[1]
        elif key == curses.KEY_UP:
            self.vertical(-1)
        elif key == curses.KEY_DOWN:
            self.vertical(1)
        elif key == "\x15":                 # Ctrl+U: delete to line start
            self.cut(self._line_bounds()[0], self.cursor)
        elif key == "\x0b":                 # Ctrl+K: delete to line end
            cursor = self.cursor
            self.cut(cursor, self._line_bounds()[1])
            self.cursor = min(cursor, self.cursor)
        elif key == "\x17":                 # Ctrl+W
            self.delete_word()
        elif key == "\t":
            self.complete()
        elif key == curses.KEY_PPAGE:
            self.scroll += max(1, (height - 8) // 2)
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - max(1, (height - 8) // 2))
            if not self.scroll:
                self.unseen = {}
        elif key == "\x0c":                 # Ctrl+L
            self.win.clear()
        elif key == curses.KEY_RESIZE:
            curses.update_lines_cols()
            self.win.clear()
        elif isinstance(key, str) and (key.isprintable() or key == " "):
            self.insert(key)
        return True

    def set_mode(self, command, argument):
        """/custom N M D, /custom off, /mode normal|wait, /pause, /resume. False (with a note) if unusable."""
        if not hasattr(store, "set_policy"):
            self.note("This version of the store does not support modes", "error")
            return False
        parts = argument.split()
        if command == "/custom" and parts == ["off"] or command == "/mode" and parts == ["normal"]:
            self.set_guard(store.set_policy("normal"))
        elif command == "/mode" and parts == ["wait"]:
            self.set_guard(store.set_policy("wait"))
        elif command == "/pause" and not parts:
            self.set_guard(store.set_policy("paused"))
        elif command == "/resume" and not parts:
            self.set_guard(store.resume_policy())
        elif command == "/custom" and len(parts) == 3 and all(part.isdigit() and int(part) > 0 for part in parts):
            limit, minutes, lasting = (int(part) for part in parts)
            self.set_guard(store.set_policy("custom", limit=limit, window_s=minutes * 60, duration_s=lasting * 60))
        else:
            usage = {"/custom": "/custom 10 1 60 (10 messages per 1 min, for 60 min) · /custom off",
                     "/mode": "/mode normal · /mode wait", "/pause": "/pause", "/resume": "/resume"}[command]
            self.note(f"Usage: {usage}", "error")
            return False
        return True

    EXPORTS = os.environ.get("AGENTCHAT_EXPORTS", os.path.join(store.DATA_DIR, "exports"))

    def export(self, argument):
        """Write the whole history, or ids 'A-B', to a Markdown file. Returns the path, or None after a note."""
        first, last = 1, None
        if argument:
            bounds = re.fullmatch(r"#?(\d{1,12})\s*-\s*#?(\d{1,12})", argument)
            if not bounds or int(bounds.group(1)) > int(bounds.group(2)):
                self.note("Usage: /export (everything) · /export 300-404", "error")
                return None
            first, last = int(bounds.group(1)), int(bounds.group(2))
        messages = [m for m in store.read_messages(first - 1) if last is None or m["id"] <= last]
        if not messages:
            self.note("Nothing to export in that range", "error")
            return None
        lines = [f"# AgentChat #{messages[0]['id']}–#{messages[-1]['id']}", ""]
        for message in messages:
            name = "you" if message["sender"] == store.USER else message["sender"]
            when = message["ts"][:16].replace("T", " ")
            if message["kind"] == "msg":
                lines += [f"**#{message['id']} {name}** · {when}", "", message["text"], ""]
            else:
                lines += [f"*#{message['id']} {name} {message['kind']}: {message['text']}* · {when}", ""]
        os.makedirs(self.EXPORTS, exist_ok=True)
        path = os.path.join(self.EXPORTS, f"{datetime.now():%Y-%m-%d-%H%M}-chat-{messages[0]['id']}-{messages[-1]['id']}.md")
        with open(path, "w", encoding="utf-8") as out:
            out.write("\n".join(lines))
        return path

    def who(self):
        """One line per agent: state, unread messages and when it last spoke."""
        parts = []
        for agent in store.AGENTS:
            state = self.presence.get(agent, {}).get("state", "unknown")
            acked = store.get_ack(agent)
            unread = sum(1 for m in self.messages if m["id"] > acked and m["kind"] == "msg" and m["sender"] != agent)
            spoke = next((m for m in reversed(self.messages) if m["sender"] == agent and m["kind"] == "msg"), None)
            ago = "not yet" if spoke is None else self._ago(spoke)
            parts.append(f"{agent}: {state} · {unread} unread · spoke {ago}")
        return "   ".join(parts)

    @staticmethod
    def _ago(message):
        try:
            seconds = time.time() - datetime.fromisoformat(message["ts"]).timestamp()
        except ValueError:
            return f"at #{message['id']}"
        return "just now" if seconds < 60 else f"{duration(seconds)} ago"

    def find(self, query):
        """Jump to the next older message containing `query`; repeat with a bare /find."""
        if not query and self.found:
            query, before = self.found
        elif query:
            before = None
        else:
            self.note("Usage: /find some words · /find again for older matches", "error")
            return
        needle = query.casefold()
        for message in reversed(self.view if self.view is not None else self.messages):
            if before is not None and message["id"] >= before:
                continue
            if message["kind"] == "msg" and needle in message["text"].casefold():
                self.found = (query, message["id"])
                self.set_highlight(query)
                self.jump_to(message["id"])
                self.note(f"Found #{message['id']} · /find for older · Esc to return")
                return
        shown = self.view if self.view is not None else self.messages
        oldest = shown[0]["id"] if shown else None
        hits = store.search_messages(query, limit=1, before_id=min(filter(None, (before, oldest)), default=None))
        older = hits[-1] if hits else None
        if older:                           # older than the screen holds: open a history view around it
            self.found = (query, older["id"])
            self.set_highlight(query)
            self.view = store.read_messages(max(0, older["id"] - 15), limit=40)
            self.jump_to(older["id"])
            self.note(f"Found #{older['id']} in history · /find for older · Esc returns to latest")
        else:
            self.found = None
            self.note(f"No older message contains \"{query}\"")

    def submit(self, text):
        """Send or run `text`. The draft is cleared only when this succeeds."""
        self.running = True
        command, _, argument = text.strip().partition(" ")
        if command == "/reply":             # '/reply 42 text' becomes the message '↪ #42 text'
            target, _, rest = argument.strip().partition(" ")
            if not target.lstrip("#").isdigit() or not rest.strip():
                self.note("Usage: /reply 42 your answer", "error")
                return
            text = f"↪ #{target.lstrip('#')} {rest.strip()}"
        command, _, argument = text.strip().partition(" ")
        argument = argument.strip()
        changed = False             # something was written: refresh after, outside the send's error path
        try:
            if not text.lstrip().startswith("/") or text.startswith("//"):
                final = self.expand(text[1:] if text.startswith("//") else text)
                if self.writer:                 # write off the input thread; the draft stays until it commits
                    self.sending = self._draft_state()
                    if self.drafts:
                        self.writer.submit("send", self.drafts.send, final, self.draft, self.cursor,
                                           self.chips, self.chip_count)
                    else:
                        self.writer.submit("send", store.append_message, store.USER, final)
                    self.set_highlight(None)
                    self.notes = [n for n in self.notes if n[1] != "error"]
                    self.notes.append(("sending…", "sending", None))
                    return
                if self.drafts:                 # journalled: a power cut can't duplicate or resurrect it
                    self.drafts.send(final, self.draft, self.cursor, self.chips, self.chip_count)
                    self.saved_state = ("", 0, {}, self.chip_count)
                else:
                    store.append_message(store.USER, final)
                self.scroll, self.unseen, self.view = 0, {}, None
                self.set_highlight(None)
                changed = True
            elif command == "/quit":
                self.running = False
            elif command in ("/custom", "/mode", "/pause", "/resume"):
                if not self.set_mode(command, argument):
                    return
                changed = True
            elif command in ("/claim", "/release") and argument:
                store.append_message(store.USER, argument, command[1:])
                changed = True
            elif command == "/open":
                ids = [int(part.lstrip("#")) for part in argument.split() if part.lstrip("#").isdigit()]
                if not ids:
                    self.note("Usage: /open 123 (the number shown on a folded message)", "error")
                    return
                for message_id in ids:
                    self.expanded ^= {message_id}
                    self.cache.pop(message_id, None)
            elif command == "/mouse" and argument in ("on", "off"):
                self.set_mouse(argument == "on")
                self.note("Scrolling moves the chat · Shift+drag selects text" if self.mouse
                          else "Mouse off · normal selection · PgUp/PgDn scroll")
            elif command == "/find":
                self.find(argument)
            elif command == "/claims":
                self.note("; ".join(f"{path} → {who}" for path, who in sorted(self.claims.items()))
                          or "No active claims.", sticky=True)
            elif command == "/export":
                path = self.export(argument)
                if path:
                    self.note(f"Saved to {path}", sticky=True)
            elif command == "/who":
                self.note(self.who(), sticky=True)
            elif command == "/help":
                self.help = True
            else:
                guess = difflib.get_close_matches(command, COMMANDS, n=1)
                hint = f" · did you mean {guess[0]}?" if guess else " · ? lists commands"
                self.note(f"Unknown command {command}{hint}", "error")
                return
        except (store.GuardBlocked, store.ClaimConflict, ValueError, OSError) as error:
            reason = error.strerror.lower() if isinstance(error, OSError) and error.strerror else str(error)
            self.note(f"Not sent: {reason} · your text is still here, press Enter to retry", "error", sticky=True)
            return
        if text not in self.history[-1:] and text.startswith("/"):
            self.history.append(text)
        self.draft, self.cursor, self.history_pos, self.stash = "", 0, None, ""
        self.chips = {}
        self.notes = [n for n in self.notes if n[1] != "error"]
        self.save_draft(now=True)           # a crash right after sending must not restore the sent text
        if changed:
            try:
                self.refresh_data()
            except (ValueError, OSError) as error:
                self.note(f"Sent · the screen couldn't refresh yet: {error}", "error", sticky=True)
