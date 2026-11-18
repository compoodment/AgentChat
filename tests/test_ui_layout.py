"""Layout tests for text.py, render.py and desktop.py: narrow and short terminals, indents, popups.

Stdlib only; screen tests drive bin/chat in a pty with throwaway data (see tests/test_ui.py).
Run: python3 -m unittest discover -s tests -p test_ui_layout.py
"""

import json
import os
from pathlib import Path
import stat
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agentchat.ui import composer, desktop, render, text  # noqa: E402
from test_ui import ScreenCase, Terminal, seed  # noqa: E402


def flat(rows):
    return ["".join(part for part, _ in row) for row in rows]


class WrapIndentTests(unittest.TestCase):
    def test_leading_indent_of_a_line_is_kept(self):
        rows = text.wrap([("  ", 0)], "  ", [("  • nested", 0), ("\n", 0), ("    deeper", 0)], 40)
        self.assertEqual(flat(rows), ["    • nested", "      deeper"])

    def test_indent_is_kept_after_the_first_segment_and_in_a_user_row(self):
        rows = text.wrap([("❯ ", 0)], "  ", [("   a", 0)], 40)
        self.assertEqual(flat(rows), ["❯    a"])

    def test_whitespace_at_a_wrap_point_is_still_dropped(self):
        rows = text.wrap([("  ", 0)], "  ", [("aaaa bbbb cccc", 0)], 11)
        self.assertEqual(flat(rows), ["  aaaa bbbb", "  cccc"])


class WidthTests(unittest.TestCase):
    def test_skin_tone_modifier_adds_no_cells(self):
        self.assertEqual(text.text_width("👍"), 2)
        self.assertEqual(text.text_width("👍🏽"), 2)
        self.assertEqual(text.text_width("a👍🏽b"), 4)


class HintTests(unittest.TestCase):
    def test_hint_picks_the_longest_that_fits_and_cuts_the_last_resort(self):
        pick = render.Renderer._hint
        self.assertEqual(pick(30, " History view · Esc returns to latest ", " History · Esc "), " History · Esc ")
        self.assertEqual(pick(60, " History view · Esc returns to latest ", " History · Esc "),
                         " History view · Esc returns to latest ")
        self.assertEqual(text.text_width(pick(10, " long long long ", " also quite long ")), 10)

    def test_share_keeps_every_part_visible(self):
        parts = [("✻ claude working · 12s · a very long presence note indeed", 1), ("○ codex away", 2)]
        out = render.Renderer._share(parts, 40)
        self.assertEqual(out[1][0], "○ codex away")
        self.assertTrue(out[0][0].endswith("…"))
        self.assertLessEqual(sum(text.text_width(t) for t, _ in out) + 3, 40)
        self.assertEqual(render.Renderer._share(parts[1:], 40), parts[1:])


class NotifyTests(ScreenCase):
    def test_title_and_body_follow_a_double_dash(self):
        record = os.path.join(self.folder.name, "argv.json")
        tool = os.path.join(self.folder.name, "fake-notify")
        with open(tool, "w") as script:
            script.write(f"#!/usr/bin/env python3\nimport json, sys\njson.dump(sys.argv[1:], open({record!r}, 'w'))\n")
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IXUSR)
        old = {key: os.environ.get(key) for key in ("AGENTCHAT_NOTIFY_CMD", "AGENTCHAT_NOTIFY")}
        os.environ["AGENTCHAT_NOTIFY_CMD"] = tool
        os.environ.pop("AGENTCHAT_NOTIFY", None)
        from unittest import mock
        try:
            with mock.patch.object(desktop.store, "DATA_DIR", Path(self.data)):   # this chat's names, not the live one's
                shown = desktop.Desktop()
            shown._someone_focused = lambda: False
            self.assertTrue(shown.notify("claude", "-rf looks like an option"))
            for _ in range(100):
                if os.path.exists(record) and os.path.getsize(record):
                    break
                time.sleep(0.05)
            with open(record) as done:
                argv = json.load(done)
        finally:
            if shown.notifier:
                shown.notifier.close()
            for key, value in old.items():
                os.environ.pop(key, None) if value is None else os.environ.__setitem__(key, value)
        self.assertEqual(argv[-3:], ["--", "claude", "-rf looks like an option"])


class NarrowScreenTests(ScreenCase):
    def test_scroll_hint_has_its_own_row_and_stays_still_while_scrolling(self):
        screen = self.open(200)
        wheel_up = "\x1b[<64;10;5M"
        screen.type(wheel_up)
        self.assertTrue(screen.until(lambda: "Esc" in screen.term.text()))
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "Esc" in line)
        self.assertEqual(lines[row].split("▼")[0].strip(), "")   # nothing of a message under it
        start = len(screen.raw)
        screen.type(wheel_up)
        screen.pump(0.4)
        replay = Terminal(len(screen.term.grid), len(screen.term.grid[0]))
        replay.feed(screen.raw[:start].decode(errors="replace"))
        hint = replay.text().splitlines()[row]
        for ch in screen.raw[start:].decode(errors="replace"):   # every state the terminal passes through
            replay.feed(ch)
            self.assertEqual(replay.text().splitlines()[row], hint, "the hint row changed mid-scroll (flicker)")

    def test_scrolled_hint_with_new_arrivals_is_cut_not_dropped(self):
        screen = self.open(60, cols=30)
        screen.type("\x1b[5~")
        self.assertTrue(screen.until(lambda: "▼" in screen.term.text()))
        screen.chat("say", "--from", "codex", "arrived")
        self.assertTrue(screen.until(lambda: "new" in screen.term.text() and "▼ 1" in screen.term.text()))
        self.assertIn("Esc", next(line for line in screen.term.text().splitlines() if "▼ 1" in line))

    def test_history_hint_is_cut_not_dropped(self):
        seed(self.data, 1200)
        log = next((Path(self.data) / "chat").glob("*.jsonl"))
        lines = log.read_text().splitlines()
        record = json.loads(lines[4])
        record["text"] = "ancient needle here"
        lines[4] = json.dumps(record)
        log.write_text("\n".join(lines) + "\n")
        screen = self.open(cols=30)              # the full hint is 38 cells: it once vanished here
        screen.type("/find ancient needle\r")
        self.assertTrue(screen.until(lambda: "History" in screen.term.text()))
        self.assertIn("Esc", next(line for line in screen.term.text().splitlines() if "History" in line))

    def test_both_agents_stay_on_the_activity_row(self):
        screen = self.open(5, cols=40)
        screen.chat("status", "--as", "claude", "working", "--note", "on something with a very long presence note")
        screen.chat("status", "--as", "codex", "listening")
        def both():                         # the activity row, two above the input box (whose hint names both)
            lines = screen.term.text().splitlines()
            box = max((n for n, line in enumerate(lines) if line.startswith("❯")), default=None)   # your messages too
            return box is not None and box >= 2 and "claude" in lines[box - 2] and "codex" in lines[box - 2]
        self.assertTrue(screen.until(both))


class ShortScreenTests(ScreenCase):
    def test_menu_selection_and_activity_stay_visible_on_8_rows(self):
        screen = self.open(5, rows=8)
        names = list(composer.COMMAND_HELP)
        screen.type("/")
        self.assertTrue(screen.until(lambda: names[0] in screen.term.text()))
        for _ in range(4):
            screen.type("\x1b[B")
        self.assertTrue(screen.until(lambda: names[4] in screen.term.text()))
        lines = screen.term.text().splitlines()
        self.assertEqual(len(lines), 8)
        self.assertTrue(any("claude" in line or "checking" in line for line in lines))
        top = lines[0].strip()
        self.assertTrue(top and top not in "abcde" and set(top) != {"─"}, f"a transcript row remains: {top!r}")

    def test_five_row_composer_leaves_a_transcript_row(self):
        screen = self.open(5, rows=8)
        for word in "abcd":
            screen.type(word + "\n")        # Ctrl+J is a new line
        screen.type("e")
        self.assertTrue(screen.until(lambda: "  e" in screen.term.text().splitlines()[-3]))
        lines = screen.term.text().splitlines()
        self.assertEqual(len(lines), 8)
        top = lines[0].strip()
        self.assertTrue(top and top not in "abcde" and set(top) != {"─"}, f"a transcript row remains: {top!r}")


class TranscriptTests(ScreenCase):
    """Activity rows, message spacing, reply buttons and quote jumps."""

    def scene(self):
        screen = self.open(0, rows=30, cols=78)
        request = screen.chat("say", "--from", "user", "@claude please look")
        screen.chat("say", "--from", "claude", "first thought")
        screen.chat("say", "--from", "claude", "second thought")
        screen.chat("say", "--from", "codex", "--kind", "claim", "agentchat/store.py")
        screen.chat("take", "--as", "claude", str(request["id"]))
        screen.chat("say", "--from", "codex", "↪ #2 agreed with that")
        self.assertTrue(screen.until(lambda: "agreed with that" in screen.term.text()))
        return screen

    def click(self, screen, y, x):
        screen.type(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m")   # SGR press and release, 1-based

    def test_activity_rows_and_breaks(self):
        lines = self.scene().term.text().splitlines()
        self.assertIn("⚑ codex claimed agentchat/store.py", "\n".join(lines))
        self.assertIn("└  claude is on it", "\n".join(lines))          # still under the request it answers
        second = next(n for n, line in enumerate(lines) if "second thought" in line)
        self.assertEqual(lines[second - 1].strip(), "")              # the same agent again: a blank line
        first = next(n for n, line in enumerate(lines) if line.startswith("● claude"))
        self.assertEqual(lines[first - 1].strip(), "")               # the speaker changed: a blank line, a name

    def test_coloured_words_in_your_messages_keep_the_band_behind_them(self):
        screen = self.open(0, rows=20, cols=78)
        screen.chat("say", "--from", "user", "ping @claude and `zxqcode` here")   # words drawn nowhere else
        self.assertTrue(screen.until(lambda: "ping @claude" in screen.term.text()))
        screen.pump(0.5)
        import re
        for word in (b"@claude", b"zxqcode"):        # the escape right before each coloured word
            before = screen.raw[:screen.raw.rindex(word)]
            sgr = re.findall(rb"\x1b\[([0-9;]*)m", before)[-1]
            self.assertIn(b"48;5;236", sgr, word)

    def test_clicking_an_id_starts_a_reply_and_a_quote_jumps(self):
        screen = self.scene()
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "#6 " in line)
        self.click(screen, row, len(lines[row]) - 2)
        self.assertTrue(screen.until(lambda: "❯ /reply 6" in screen.term.text()))   # lines are trimmed
        screen.chat("custom", "60", "1", "5")
        for n in range(25):                                          # push #2 off the screen
            screen.chat("say", "--from", "user", f"filler {n}")
        self.assertTrue(screen.until(lambda: "filler 24" in screen.term.text()))
        self.assertNotIn("first thought", screen.term.text())
        for _ in range(8):                                           # page up until the quote shows
            if screen.until(lambda: "↪ #2 claude" in screen.term.text(), 1):
                break
            screen.type("\x1b[5~")
        self.assertIn("↪ #2 claude", screen.term.text())
        lines = screen.term.text().splitlines()
        row = next(n for n, line in enumerate(lines) if "↪ #2 claude" in line)
        self.click(screen, row, 4)
        self.assertTrue(screen.until(lambda: "first thought" in screen.term.text()
                                     and "#2 · Esc returns" in screen.term.text()))


class ActivityTests(unittest.TestCase):
    def test_the_task_in_hand_is_the_newest_with_older_ones_counted(self):
        from agentchat import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.attr = {"claude": 1, "codex": 2, "dim": 3}
        screen.presence = {"claude": {"state": "working", "since": time.time()}, "codex": {"state": "listening"}}
        screen.claims = {"task:4": "claude", "task:28": "claude", "task:11": "claude", "task:16": "codex",
                         "docs/usage.md": "claude"}
        claude, codex = (text for text, _ in screen.activity(80))
        self.assertTrue(claude.startswith("✻ claude working on #28 +2"), claude)
        self.assertEqual(codex, "● codex listening · has #16")


class FormattingTests(unittest.TestCase):
    """Markdown-ish styles and coloured agent mentions in message text."""

    def spans(self, text):
        import curses
        from agentchat import ui
        screen = ui.Screen.__new__(ui.Screen)
        screen.highlight = None
        screen.attr = {"code": 1 << 20, "dim": 1 << 21, "me": 1 << 22, "claude_mention": 1 << 23, "codex_mention": 1 << 24}
        self.curses = curses
        return [(t, a) for t, a in screen._spans(text) if t != "\n"]

    def test_inline_styles(self):
        c = self.spans("a *it* _also_ ~~gone~~ snake_case_name 2*3*4 [docs](https://docs.example.invalid/a) https://example.invalid")
        styled = {t: a for t, a in c}
        self.assertTrue(styled["it"] & render.ITALIC and styled["also"] & render.ITALIC)
        self.assertTrue(styled["gone"] & self.curses.A_DIM)
        self.assertTrue(styled["docs"] & self.curses.A_UNDERLINE)
        self.assertIn(" (https://docs.example.invalid/a)", styled)
        self.assertTrue(styled["https://example.invalid"] & self.curses.A_UNDERLINE)
        plain = "".join(t for t, _ in c)
        self.assertIn("snake_case_name 2*3*4", plain)            # words with _ or * inside stay as written

    def test_line_styles(self):
        c = self.spans("> quoted words\n3. third\n- [ ] todo\n- [x] done\n---\n# Title")
        texts = [t for t, _ in c]
        self.assertEqual(texts[:2], ["▎ ", "quoted words"])
        self.assertTrue(c[1][1] & render.ITALIC)
        for marker in ("3. ", "☐ ", "☑ ", "─" * 12):
            self.assertIn(marker, texts)
        self.assertTrue(dict(c)["Title"] & self.curses.A_UNDERLINE)

    def test_long_unmatched_runs_stay_fast(self):
        for text_ in ("[" * 16000, "*a" * 8000, " _a" * 6000, "~~a" * 6000):
            started = time.monotonic()
            self.spans(text_)
            self.assertLess(time.monotonic() - started, 0.5, text_[:4])   # a backtracking pattern took 3.7 s

    def test_agent_mentions_have_their_own_colours(self):
        c = dict(self.spans("ping @claude and @codex, cc @user"))
        self.assertEqual((c["@claude"], c["@codex"], c["@user"]), (1 << 23, 1 << 24, 1 << 22))


if __name__ == "__main__":
    unittest.main()
