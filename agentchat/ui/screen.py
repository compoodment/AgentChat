"""Screen state and data refresh; drawing and input live in the Renderer and Composer mixins."""

import curses
import os
import queue
import threading
import time
from collections import deque

from agentchat import store, ui_state
from agentchat.ui.composer import QUIT_WINDOW, Composer
from agentchat.ui.desktop import Desktop
from agentchat.ui.render import Renderer
from agentchat.ui.text import ME, TASK, clean, fit


HISTORY = 1000              # messages kept in memory
STATUS_EVERY = 2            # seconds between presence/guard/latest-id refreshes (also the missed-wake bound)
NOTE_SECONDS = 20           # transient notes clear after this, or on the next key


class Writer(threading.Thread):
    """Runs disk writes (sends, draft saves) in order off the input thread; results return through a pipe."""

    def __init__(self, wake_fd):
        super().__init__(daemon=True)
        self.jobs, self.done, self.wake_fd = queue.Queue(), queue.Queue(), wake_fd

    def submit(self, kind, function, *args):
        self.jobs.put((kind, function, args))

    def run(self):
        while True:
            kind, function, args = self.jobs.get()
            try:
                self.done.put((kind, function(*args), None))
            except Exception as error:      # reported on screen by apply_writes
                self.done.put((kind, None, error))
            self.jobs.task_done()
            try:
                os.write(self.wake_fd, b"w")
            except BlockingIOError:
                pass                        # the main loop is already due to wake

    def drain(self, timeout):
        """Wait up to `timeout` seconds for queued writes (used when quitting). True if all finished."""
        deadline = time.monotonic() + timeout
        while self.jobs.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.jobs.unfinished_tasks


class Screen(Renderer, Composer):
    def __init__(self, win):
        self.win = win
        self.messages = deque()
        self.by_id = {}
        self.tasks = {}             # user message id -> [(kind, agent)] from task claims/releases
        self.expanded = set()       # ids of long agent messages opened with /open
        self.body_height = 20       # transcript rows at the last draw, for /find jumps
        self.found = None           # (query, id) of the last /find match
        self.highlight = None       # the /find words shown highlighted, until Esc or a send
        self.view = None            # older messages shown by /find; None means the live transcript
        self.new_after = None       # the user's read marker at startup: a "new" rule goes after it
        self.marked = 0             # the read marker as last written
        self.seen = 0               # newest id the user has seen at the live bottom, waiting to be written
        self.seen_written_at = 0
        self.interacted = False     # set by real keys and focus-in; never by arrivals or focus-out
        self.last_key_at = time.monotonic()   # the status thread waits for a quiet moment before its cold scan
        self.drafts = None          # ui_state.DraftSession: the unsent draft survives crashes
        self.focused = True         # from the terminal's focus reports; notifications only when unfocused
        self.away = 0               # messages that arrived while unfocused, shown in the window title
        self.desktop = Desktop()    # popups coordinated with any other open screen of this chat
        self.title = "AgentChat"
        self.saved_state, self.edited_at = None, None
        self.writer = None          # Writer thread, set by the app; None means write in place (tests)
        self.sending = None         # draft snapshot of the send in flight; the draft stays until it commits
        self.closing = False        # quit asked while a write was in flight: waiting for it, visibly
        self.force_quit = False     # quit anyway: the unsaved text is printed on exit
        self.unsaved = ""           # text that a forced quit left neither sent nor saved
        self.cache = {}             # message id -> rows, valid for self.cache_width
        self.cache_width = None
        self.last_id = 0
        self.guard = {"mode": None}         # filled by the status thread; the footer says "checking mode…" until then
        self.retry_deadline = None          # monotonic time a rate refusal ends, for the countdown
        self.refused = False        # a guard refusal happened since the last user message
        self.claims = {}
        self.presence = {}
        self.status_wanted = threading.Event()   # set to make the status thread refresh now
        self.draft, self.cursor = "", 0
        self.history, self.history_pos, self.stash = [], None, ""
        self.scroll = 0
        self.unseen = {}            # sender -> count of messages that arrived while scrolled up
        self.notes = []             # transient rows: (text, attr_name, expires_at or None)
        self.help = False
        self.esc_at = self.ctrl_c_at = 0
        self.pending = []           # keys read but not yet handled (escape sequences)
        self.pending_tail = []      # keys fetched while finishing a split escape sequence
        self.menu_index = 0         # the selected row of the / command menu
        self.menu_closed = False    # Esc hides the menu until the next typed character
        self.pasting = None         # list of characters collected inside a bracketed paste
        self.chips = {}             # paste chip label -> the full pasted text
        self.chip_count = 0         # chip numbers never repeat within a session
        self.saved_draft = ""       # the draft in progress while browsing history
        self.drawn_minute = 0       # activity durations redraw once a minute
        self.running = True
        self._colors()

    def _colors(self):
        A = curses
        self.attr = {"user": A.A_BOLD, "claude": A.A_BOLD, "codex": A.A_BOLD, "dim": A.A_DIM,
                     "error": A.A_BOLD, "meta": A.A_DIM, "me": A.A_REVERSE | A.A_BOLD,
                     "band": 0, "rule": A.A_DIM, "key": A.A_BOLD, "code": A.A_BOLD, "menu_on": A.A_REVERSE,
                     "found": A.A_REVERSE, "sending": A.A_DIM}
        if os.environ.get("NO_COLOR") or not curses.has_colors():
            return
        curses.use_default_colors()
        rich = curses.COLORS >= 256
        light = os.environ.get("AGENTCHAT_THEME") == "light"   # set by app.detect_theme() unless the user chose
        palette = {"user": 28 if rich and light else curses.COLOR_GREEN,
                   "claude": (166 if light else 173) if rich else curses.COLOR_YELLOW,
                   "codex": (91 if light else 140) if rich else curses.COLOR_MAGENTA,
                   "error": curses.COLOR_RED}
        for number, (name, color) in enumerate(palette.items(), 1):
            curses.init_pair(number, color, -1)
            self.attr[name] |= curses.color_pair(number)
        curses.init_pair(10, (25 if light else 110) if rich else curses.COLOR_CYAN, -1)
        if rich:                            # /find matches: dark text on yellow, readable in both themes
            curses.init_pair(11, 16, 220)
            self.attr["found"] = curses.color_pair(11)
        self.attr["code"] = curses.color_pair(10)
        if rich:                            # a faint band behind your own messages, in either theme
            band = 254 if light else 236
            curses.init_pair(8, -1, band)
            self.attr["band"] = curses.color_pair(8)
            curses.init_pair(9, palette["user"], band)
            self.attr["user_band"] = curses.color_pair(9) | curses.A_BOLD

    # -- data -------------------------------------------------------------

    def refresh_data(self, initial=False):
        if initial and hasattr(store, "read_tail"):
            new = store.read_tail(HISTORY)  # only the newest messages: fast first paint on long logs
        else:
            new = store.read_messages(self.last_id)
        ring, notify = False, []
        for message in new:
            self._add(message)
            if not initial and message["sender"] != store.USER:
                if self.scroll or self.view is not None:
                    self.unseen[message["sender"]] = self.unseen.get(message["sender"], 0) + 1
                if self.addressed_to_me(message) or self._is_refusal(message):
                    ring = True
                if self.addressed_to_me(message) and not self.focused:
                    notify.append(message)
                if not self.focused and message["kind"] == "msg":
                    self.away += 1
        if new:
            self.last_id = new[-1]["id"]
        if not initial:                     # at startup the first draw comes first; status follows
            self.request_status()
        if ring:
            curses.beep()
        if notify:
            self.desktop_notify(notify)

    def _add(self, message):
        if len(self.messages) >= HISTORY:
            old = self.messages.popleft()
            self.by_id.pop(old["id"], None)
            self.cache.pop(old["id"], None)
        self.messages.append(message)
        self.by_id[message["id"]] = message
        kind, sender, text = message["kind"], message["sender"], message["text"]
        if kind == "msg" and sender == store.USER:
            self.refused = False
            self.history.append(text)
        elif self._is_refusal(message):
            self.refused = True
        task = TASK.match(text) if kind in ("claim", "release") else None
        if task:
            target = int(task.group(1))
            self.tasks.setdefault(target, []).append((kind, sender))
            self.cache.pop(target, None)

    @staticmethod
    def _is_refusal(message):
        return message["sender"] == "system" and message["text"].startswith(("Agent loop guard", "Agent rate guard"))

    def desktop_notify(self, messages):
        """A desktop notification for mentions while the window is unfocused. AGENTCHAT_NOTIFY=0 turns it off."""
        latest = messages[-1]
        title = f"{latest['sender']} mentioned you" + (f" (+{len(messages) - 1} more)" if len(messages) > 1 else "")
        self.desktop.notify(title, fit(" ".join(clean(latest["text"]).split()), 200))

    # -- crash-safe state: read marker and draft ----------------------------

    def open_state(self):
        try:
            marker = ui_state.get_read_marker()
            self.marked = self.seen = marker
            if self.messages and self.messages[0]["id"] <= marker < self.last_id:
                self.new_after = marker
        except (ValueError, OSError) as error:
            self.note(f"Read marker unavailable: {error}", "error")
        try:
            self.drafts = ui_state.DraftSession()
        except (ValueError, OSError) as error:
            self.note(f"Draft saving unavailable: {error}", "error")
            return
        for warning in self.drafts.warnings:
            self.note(warning, "error", sticky=True)
        restored = self.drafts.restored
        if restored:
            self.draft, self.cursor = restored["draft"], restored["cursor"]
            self.chips, self.chip_count = dict(restored["chips"]), restored["chip_count"]
            self.note("Restored your unsent draft")
        self.saved_state = self._draft_state()

    def _draft_state(self):
        return (self.draft, self.cursor, dict(self.chips), self.chip_count)

    def save_draft(self, now=False):
        """Save the draft about a second after the last edit, or right away with now=True."""
        if not self.drafts or self.sending:
            return                          # while sending, the send's journal holds this draft; a save
        state = self._draft_state()         # queued behind it would bring the sent text back as a ghost
        if state == self.saved_state:
            self.edited_at = None
            return
        if self.edited_at is None:
            self.edited_at = time.monotonic()
        if not now and time.monotonic() - self.edited_at < 1:
            return
        if self.writer:                     # off the input thread; a failure comes back via apply_writes
            self.writer.submit("save", self.drafts.save, *state)
            self.saved_state, self.edited_at = state, None
            return
        try:
            self.drafts.save(*state)
            self.saved_state, self.edited_at = state, None
        except (ValueError, OSError) as error:
            self.note(f"Draft not saved: {error}", "error")
            self.edited_at = time.monotonic()

    def apply_writes(self):
        """Take finished writes from the writer thread. True if the screen changed."""
        changed = False
        while self.writer and not self.writer.done.empty():
            kind, _, error = self.writer.done.get()
            changed = True
            if kind == "seen":              # the read marker is a convenience; nothing to show
                continue
            if kind == "save":
                if error:
                    self.saved_state = None     # try again on the next save
                    reason = error.strerror.lower() if isinstance(error, OSError) and error.strerror else str(error)
                    if self.closing:            # don't quit with unsaved text: stay open and say why
                        self.closing = False
                        self.note(f"Draft not saved: {reason} · your text is still here; Ctrl+C twice to retry",
                                  "error", sticky=True)
                    else:
                        self.note(f"Draft not saved: {reason}", "error")
                continue
            snapshot, self.sending = self.sending, None
            self.notes = [n for n in self.notes if n[1] != "sending"]
            if error:
                reason = error.strerror.lower() if isinstance(error, OSError) and error.strerror else str(error)
                self.note(f"Not sent: {reason} · your text is still here, press Enter to retry", "error", sticky=True)
                continue
            self.draft, self.cursor, self.history_pos, self.stash, self.chips = "", 0, None, "", {}
            self.saved_state = self._draft_state()  # the send cleared the saved draft
            self.scroll, self.unseen, self.view = 0, {}, None
            self.notes = [n for n in self.notes if n[1] != "error"]
            try:
                self.refresh_data()
            except (ValueError, OSError) as error:
                self.note(f"Sent · the screen couldn't refresh yet: {error}", "error", sticky=True)
        return changed

    def capture_seen(self):
        """After an interaction at the live bottom, everything on screen counts as seen."""
        if self.interacted and self.view is None and not self.scroll:
            self.seen = max(self.seen, self.last_id)
        self.interacted = False

    def flush_seen(self, now=False):
        """Write the captured id off the input thread, at most every 2 s; now=True writes it here (exit)."""
        if self.seen <= self.marked or (not now and time.monotonic() - self.seen_written_at < 2):
            return
        self.seen_written_at, target = time.monotonic(), self.seen

        def write():
            try:
                self.marked = max(self.marked, ui_state.mark_read(target))
            except (ValueError, OSError):
                pass                        # the marker is a convenience; never interrupt typing
        if now:
            write()
        else:
            threading.Thread(target=write, daemon=True).start()

    def writes_pending(self):
        return bool(self.writer and (self.sending or self.writer.jobs.unfinished_tasks))

    def queue_final_writes(self):
        """On a normal quit: queue the dirty draft and the read marker on the writer, then wait for them."""
        if not self.writer:
            return
        self.save_draft(now=True)           # queues; a no-op when the draft is already saved
        if self.seen > self.marked:
            self.writer.submit("seen", ui_state.mark_read, self.seen)
            self.marked = self.seen

    def close_state(self):
        if self.writer:
            finished = self.writer.drain(0 if self.force_quit else 15)
            self.apply_writes()
            self.writer = None
            dirty = bool(self.draft) and self._draft_state() != self.saved_state
            if self.force_quit or not finished:
                # Forced or signal exit, or a stuck disk: no writes from here (they could block on the same
                # disk) and no assumption that a journal landed. Print anything not known to be saved.
                if self.sending or dirty or not finished:
                    draft, _, chips, _ = self.sending or self._draft_state()
                    self.chips = chips      # print what the user typed and pasted, not chip labels
                    self.unsaved = self.expand(draft)
                return
            if self.drafts:                 # everything was written by the writer; just release the lock
                self.drafts.close()
            return
        self.save_draft(now=True)
        self.flush_seen(now=True)          # only what an interaction captured, never later arrivals
        if self.drafts:
            self.drafts.close()

    def request_status(self):
        """Ask the status thread for fresh guard/claims/presence; the store reads never block input."""
        self.status_wanted.set()

    def apply_status(self, result):
        """Take (guard, claims, presence, latest id) or an error from the status thread. True if it changed."""
        if isinstance(result, Exception):
            self.note(f"Store error: {result}", "error")
            return True
        guard, claims, presence, latest = result
        before = (self.guard, self.claims, self.presence)
        self.set_guard(guard)
        self.claims, self.presence = claims, presence
        changed = (guard, claims, presence) != before
        if latest > self.last_id:           # a wake was missed: catch up now instead of on the next key
            self.refresh_data()
            changed = True
        return changed

    def set_guard(self, guard):
        """Store a guard status. A blocked rate mode with a positive retry gets a deadline to count down to;
        paused and wait have no retry time, so they never get one (that would refresh in a loop)."""
        self.guard = guard
        retry = guard.get("retry_after")
        rate = guard.get("mode") in ("normal", "custom", "auto")
        timed = guard.get("blocked") and rate and isinstance(retry, (int, float)) and retry > 0
        self.retry_deadline = time.monotonic() + retry if timed else None

    def retry_left(self):
        deadline = getattr(self, "retry_deadline", None)
        return max(0, deadline - time.monotonic()) if deadline else 0

    def ticking(self):
        """True while something on screen changes by the second: a working agent's elapsed time, a rate
        countdown, or a two-press hint ('ctrl+c again to quit', 'esc again to clear') about to expire."""
        working = any(info.get("state") == "working" and info.get("since")
                      and time.time() - info["since"] < 3600 for info in self.presence.values())
        hint = time.time() - max(self.ctrl_c_at, self.esc_at) < QUIT_WINDOW + 1
        return working or self.retry_left() > 0 or hint

    def note(self, text, kind="dim", sticky=False):
        self.notes = [(text, kind, None if sticky else time.monotonic() + NOTE_SECONDS)]
