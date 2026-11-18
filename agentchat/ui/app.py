"""The main loop: watcher thread, poll loop, resize, hangup and clean exit."""

import curses
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty

from agentchat import store
from agentchat.ui import keys as modified
from agentchat.ui.screen import STATUS_EVERY, Screen, Writer
from agentchat.ui.text import clean, fit


WATCH_TIMEOUT = 300         # seconds per wait_for_change call; only bounds thread wakeups
MODIFIED_KEYS = os.environ.get("AGENTCHAT_KEYS") != "legacy"   # ask terminals to report Shift+Enter and friends


# -- background watcher -----------------------------------------------------

class Watcher(threading.Thread):
    """Waits for messages newer than `seen` and writes a byte to `wake_fd`."""

    def __init__(self, seen, wake_fd):
        super().__init__(daemon=True)
        self.seen = seen
        self.wake_fd = wake_fd
        self.consumed = threading.Event()
        self.error = None

    def run(self):
        while True:
            try:
                changed = store.wait_for_change(self.seen, WATCH_TIMEOUT)
            except Exception as error:      # surfaced on screen, then retried
                self.error = error
                changed = True
                time.sleep(5)
            if changed:
                self.consumed.clear()
                os.write(self.wake_fd, b".")
                self.consumed.wait()

    def advance(self, seen):
        self.seen = seen
        self.consumed.set()



class StatusThread(threading.Thread):
    """Loads guard, claims and presence off the input thread, every few seconds or when asked."""

    QUIET = 1.5                             # seconds without a key before the cold first scan

    def __init__(self, wanted, wake_fd, last_key_at):
        super().__init__(daemon=True)
        self.wanted = wanted
        self.wake_fd = wake_fd
        self.last_key_at = last_key_at      # callable: monotonic time of the latest keypress
        self.result = None

    def run(self):
        # The first pass parses the whole log in pure Python and competes with typing for the GIL
        # (~100-200 ms echo on 5k messages). Wait for a quiet moment; warm passes later take ~4 ms.
        while time.monotonic() - self.last_key_at() < self.QUIET:
            time.sleep(0.25)
        while True:
            try:
                self.result = (store.guard_status(), store.active_claims(), store.get_presence(), store.last_id())
            except Exception as error:      # shown on screen by apply_status
                self.result = error
            os.write(self.wake_fd, b"s")
            self.wanted.wait(STATUS_EVERY)
            self.wanted.clear()


def _run(win, screen_box):
    curses.raw()
    curses.nonl()
    curses.curs_set(1)
    curses.set_escdelay(25)
    win.nodelay(True)
    win.keypad(True)
    curses.mouseinterval(0)                 # wheel steps arrive at once, no click-detection delay
    # paste mode, focus reports, title; and modified-key reports, so Shift+Enter can be told from Enter
    sys.stdout.write("\x1b[?2004h\x1b[?1004h\x1b]0;AgentChat\x07" + (modified.ENABLE if MODIFIED_KEYS else ""))
    sys.stdout.flush()
    screen = Screen(win)
    screen_box.append(screen)
    screen.refresh_data(initial=True)
    screen.set_mouse(os.environ.get("AGENTCHAT_MOUSE") != "0")
    screen.open_state()
    try:
        _loop(win, screen)
    finally:
        screen.close_state()


def _pipe():
    """A non-blocking pipe that background threads and signal handlers use to wake the main loop."""
    read, write = os.pipe()
    os.set_blocking(read, False)
    os.set_blocking(write, False)
    return read, write


def _drain(fd):
    try:
        os.read(fd, 4096)
    except BlockingIOError:
        pass


def _loop(win, screen):
    wake_read, wake_write = _pipe()
    resize_read, resize_write = _pipe()
    signal.signal(signal.SIGWINCH, lambda signum, frame: os.write(resize_write, b"r"))
    watcher = Watcher(screen.last_id, wake_write)
    watcher.start()
    status_read, status_write = _pipe()
    writes_read, writes_write = _pipe()
    screen.writer = Writer(writes_write)
    screen.writer.start()
    status = StatusThread(screen.status_wanted, status_write, lambda: screen.last_key_at)
    status.start()
    screen.draw()                           # first paint: messages only; status arrives from its thread
    stdin = sys.stdin.fileno()
    poller = select.poll()
    for fd in (stdin, wake_read, resize_read, status_read, writes_read):
        poller.register(fd, select.POLLIN)
    hangup = select.POLLHUP | select.POLLERR | select.POLLNVAL
    while True:
        try:
            events = poller.poll(1000)
        except InterruptedError:
            events = []
        if any(fd == stdin and flags & hangup for fd, flags in events):
            return                          # the terminal is gone: exit instead of spinning
        ready = [fd for fd, flags in events if flags & select.POLLIN]
        dirty = False
        if resize_read in ready:
            _drain(resize_read)
            try:
                size = os.get_terminal_size(sys.stdout.fileno())
                curses.resizeterm(size.lines, size.columns)
            except (OSError, curses.error):  # a terminal can briefly report 0x0 mid-resize: draw at the old size
                pass
            win.clearok(True)               # a full repaint, inside one frame: clearing now flashed a blank screen
            screen.draw()                   # before the next key read, whose implicit refresh would repaint first
        if wake_read in ready:
            _drain(wake_read)
            if watcher.error:
                screen.note(f"Store error: {watcher.error}", "error")
                watcher.error = None
            try:
                screen.refresh_data()
            except (ValueError, OSError) as error:
                screen.note(f"Store error: {error}", "error")
            watcher.advance(screen.last_id)
            dirty = True
        screen.read_keys()
        if screen.pending:
            screen.last_key_at = time.monotonic()
            dirty = True
            if not screen.handle_pending():
                screen.capture_seen()       # the quit key itself is an interaction
                if not screen.force_quit:
                    screen.queue_final_writes()
                if screen.force_quit or not screen.writes_pending():
                    return
                screen.closing = True       # a send or save is still on the disk: wait for it, visibly
                screen.notes = [("still saving your message… · Ctrl+C again to quit anyway", "error", None)]
                dirty = True
        if screen.closing:
            dirty |= screen.apply_writes()  # see a failed save first: it cancels closing
            if screen.closing and not screen.writes_pending():
                return                      # the writer finished cleanly: now it's safe to quit
        screen.capture_seen()
        screen.flush_seen()
        screen.save_draft()
        if writes_read in ready:
            _drain(writes_read)
            if screen.apply_writes():
                dirty = True
        if status_read in ready:
            _drain(status_read)
            if screen.apply_status(status.result):
                dirty = True
        if screen.notes and any(expiry and expiry <= time.monotonic() for _, _, expiry in screen.notes):
            dirty = True
        title = f"AgentChat ({screen.away})" if screen.away else "AgentChat"
        if title != screen.title:           # the window title counts messages that arrived while away
            screen.title = title
            sys.stdout.write(f"\x1b]0;{title}\x07")
            sys.stdout.flush()
        if screen.retry_deadline and screen.retry_left() == 0:
            screen.retry_deadline = None    # the countdown ran out: ask for the fresh status once
            screen.request_status()
        # Redraw on change, every second while a seconds-level clock shows, else once a minute.
        tick = int(time.time()) if screen.ticking() else int(time.time()) // 60
        if dirty or tick != screen.drawn_minute:
            screen.drawn_minute = tick
            screen.draw()


def _goodbye(screen):
    """Leave the last messages in the terminal's scrollback, and any text that wasn't saved."""
    if screen.unsaved:
        print("AgentChat · NOT sent or saved (the disk was still busy when you quit). Your text:")
        print(screen.unsaved)
        print()
    recent = [m for m in list(screen.messages)[-12:] if m["kind"] == "msg"]
    if not recent:
        return
    print("AgentChat · last messages")
    for message in recent:
        name = "you" if message["sender"] == store.USER else message["sender"]
        text = " ".join(clean(message["text"]).split())
        print(f"  #{message['id']} {name}: {fit(text, 100)}")
    print("Reopen with: agentchat")


def detect_theme(timeout=0.25):
    """Ask the terminal for its background colour (OSC 11). 'light', 'dark', or None when it doesn't answer."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b]11;?\x07")
        sys.stdout.flush()
        reply, deadline = b"", time.monotonic() + timeout
        while time.monotonic() < deadline and not reply.endswith((b"\x07", b"\x1b\\")):
            if select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
                reply += os.read(fd, 64)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    match = re.search(rb"rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", reply)
    if not match:
        return None
    red, green, blue = (int(part, 16) / (16 ** len(part) - 1) for part in match.groups())
    return "light" if 0.2126 * red + 0.7152 * green + 0.0722 * blue > 0.5 else "dark"


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("chat: the chat screen needs a terminal", file=sys.stderr)
        return 1
    if "AGENTCHAT_THEME" not in os.environ and "NO_COLOR" not in os.environ:
        os.environ["AGENTCHAT_THEME"] = detect_theme() or "dark"

    def stop(signum, frame):
        if screen_box:                      # a signal means "go now": don't wait on a stuck disk
            screen_box[0].force_quit = True
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    screen_box = []
    try:
        curses.wrapper(_run, screen_box)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?2004l\x1b[?1004l" + (modified.DISABLE if MODIFIED_KEYS else ""))
        sys.stdout.flush()
        if screen_box:                      # also on SIGTERM/SIGHUP, whose SystemExit skips code after this block
            _goodbye(screen_box[0])
    return 0
