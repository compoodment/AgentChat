"""How the screen looks: message blocks, activity row, footer and draw()."""

import curses
import os
import re
import sys
import time
from datetime import datetime

from agentchat import __version__, store
from agentchat.ui.composer import COMMAND_HELP, MENU_ROWS, QUIT_WINDOW
from agentchat.ui.text import ISO, ME, MENTION, REPLY, TASK, clean, duration, fit, text_width, wrap


FOLD_ROWS, FOLD_SHOW = 16, 12   # agent messages longer than FOLD_ROWS show FOLD_SHOW rows until /open
INLINE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*|~~[^~\n]+~~|(?<![\w*])\*[^*\s][^*\n]*?\*(?![\w*])"
                    r"|(?<!\w)_[^_\s][^_\n]*?_(?!\w)|\[[^\[\]\n]+\]\(https?://[^)\s]+\)|https?://[^\s)>]+)")
# a link's label excludes '[' too: otherwise every '[' of a long unmatched run rescanned the rest (quadratic)
ITALIC = getattr(curses, "A_ITALIC", curses.A_DIM)    # older curses builds: dim instead
COMPOSER_ROWS = 5           # the composer grows up to this many rows, then scrolls
FRAME_START = b"\x1b[?2026h\x1b[?25l"   # begin synchronized output, hide the cursor (unknown modes are ignored)
FRAME_END = b"\x1b[?25h\x1b[?2026l"
HELP = ["Enter send · Ctrl+J or Alt+Enter new line (continues 1. and - lists) · Tab completes names",
        "↑↓ move a row, at the top or bottom jump to the start or end · Shift+↑↓ sent messages",
        "Scroll/PgUp/PgDn · Shift+drag selects · Esc to latest · Ctrl+W word · Ctrl+C ×2",
        "/reply N · /find · /custom 10 1 60 · /mode normal|wait · /pause · /export"]
PLACEHOLDER = "Message claude and codex · @ to mention · ? for shortcuts"


def _rows(body, meta=None, band=False):
    """Screen rows from wrapped rows: '#id time' on the first, an optional tinted band on all."""
    return [(row, meta if number == 0 else None, band) for number, row in enumerate(body)]


class Renderer:
    """Message blocks, transcript rows and the whole-screen draw. Mixed into Screen."""

    # -- message rendering --------------------------------------------------

    def _inline(self, text, attr, out):
        """`code`, **bold**, *italic*, ~~struck~~, links and @mentions inside one line of prose."""
        for part in INLINE.split(text):
            if not part:
                continue
            link = re.fullmatch(r"\[([^\[\]\n]+)\]\((https?://[^)\s]+)\)", part)
            if part.startswith("`") and part.endswith("`") and len(part) > 2:
                out.append((part[1:-1], self.attr["code"]))
            elif part.startswith("**") and part.endswith("**") and len(part) > 4:
                self._inline(part[2:-2], attr | curses.A_BOLD, out)
            elif part.startswith("~~") and part.endswith("~~") and len(part) > 4:
                self._inline(part[2:-2], attr | curses.A_DIM, out)   # curses has no strikethrough: faded
            elif len(part) > 2 and part[0] == part[-1] and part[0] in "*_":
                self._inline(part[1:-1], attr | ITALIC, out)
            elif link:                      # [text](url): the words underlined, the address dim after them
                out += [(link.group(1), attr | curses.A_UNDERLINE), (f" ({link.group(2)})", self.attr["dim"])]
            elif part.startswith(("http://", "https://")):
                out.append((part, attr | curses.A_UNDERLINE))
            else:
                for piece in MENTION.split(part):
                    if not piece:
                        continue
                    if MENTION.fullmatch(piece):
                        name = piece.lower()[1:]
                        out.append((piece, self.attr["me"] if piece.lower() in ME else
                                    self.attr.get(f"{name}_mention", attr | curses.A_BOLD)))
                    elif self.highlight:        # /find matches stand out until Esc or the next send
                        for bit in re.split(f"({re.escape(self.highlight)})", piece, flags=re.IGNORECASE):
                            if bit:
                                out.append((bit, self.attr["found"] if bit.casefold() == self.highlight.casefold()
                                            else attr))
                    else:
                        out.append((piece, attr))

    def _spans(self, text, attr=0):
        """Light markdown: fenced code, inline styles, - and 1. lists, [ ] tasks, > quotes, # headings, ---."""
        out, fenced = [], False
        for number, line in enumerate(clean(text).split("\n")):
            if number:
                out.append(("\n", attr))
            if line.lstrip().startswith("```"):
                fenced = not fenced
                label = line.strip()[3:].strip()
                out.append((f"── {label} " if fenced and label else "──", self.attr["dim"]))
                continue
            if fenced:
                out += [("│ ", self.attr["dim"]), (line, self.attr["code"])]
                continue
            heading = re.match(r"#{1,4} (.*)", line)
            task = re.match(r"(\s*)[-*] \[([ xX])\] (.*)", line)
            bullet = re.match(r"(\s*)[-*] (.*)", line)
            numbered = re.match(r"(\s*)(\d{1,4}[.)]) (.*)", line)
            quote = re.match(r"\s*> ?(.*)", line)
            if heading:
                self._inline(heading.group(1), attr | curses.A_BOLD | curses.A_UNDERLINE, out)
            elif re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
                out.append(("─" * 12, self.attr["dim"]))
            elif task:
                out.append((task.group(1) + ("☑ " if task.group(2) != " " else "☐ "), self.attr["dim"]))
                self._inline(task.group(3), attr | curses.A_DIM if task.group(2) != " " else attr, out)
            elif bullet:
                out.append((bullet.group(1) + "• ", self.attr["dim"]))
                self._inline(bullet.group(2), attr, out)
            elif numbered:
                out.append((f"{numbered.group(1)}{numbered.group(2)} ", self.attr["dim"]))
                self._inline(numbered.group(3), attr, out)
            elif quote:                     # a quoted line: a dim bar, the words in italics
                out.append(("▎ ", self.attr["dim"]))
                self._inline(quote.group(1), attr | ITALIC, out)
            else:
                self._inline(line, attr, out)
        return out

    def _meta(self, message):
        try:
            stamp = datetime.fromisoformat(message["ts"]).strftime("%H:%M")
        except ValueError:
            stamp = "--:--"
        return f"#{message['id']} {stamp}"

    def _proto_text(self, message):
        sender, kind, text = message["sender"], message["kind"], clean(message["text"])
        if kind == "claim":
            return f"{sender} claimed {text}", "dim"
        if kind == "release":
            return f"{sender} released {text}", "dim"
        text = ISO.sub(lambda match: match.group(1), text)
        text = re.sub(r"(\d+) seconds", lambda m: f"{int(m.group(1)) // 60} min" if int(m.group(1)) % 60 == 0
                      else duration(int(m.group(1))), text)
        text = text.replace("Agent loop guard: ", "Loop guard: ").replace("Agent rate guard: ", "Rate limit: ")
        text = text.replace("Autonomous mode enabled until", "Auto mode on until")
        text = text.replace("Autonomous mode disabled", "Auto mode off").replace(" agent messages per ", " agent messages / ")
        text = text.replace("; ", " · ").rstrip(".")
        return text, ("error" if self._is_refusal(message) else "dim")

    def block(self, message, width):
        """Rows for one message, as (segments, right_text, band). Cached per width."""
        rows = self.cache.get(message["id"])
        if rows is not None:
            return rows
        meta = self._meta(message)
        room = width - text_width(meta) - 2
        sender, kind = message["sender"], message["kind"]
        task = TASK.match(message["text"]) if kind in ("claim", "release") else None
        text, quote = self._reply(message, width) if kind == "msg" else (message["text"], [])
        if kind == "msg" and sender == store.USER:
            arrow = self.attr.get("user_band", self.attr["user"])
            body = wrap([("❯ ", arrow)], "  ", self._spans(text), room)
            rows = quote + _rows(body, meta, band=True)
            events = self.tasks.get(message["id"], [])
            if events:
                rows.append(([("  └  ", self.attr["dim"]),
                              (self._task_summary(events), self.attr["dim"])], None, False))
        elif kind == "msg" and sender in store.AGENTS:
            header = [("● " + sender, self.attr[sender])]
            mine = self.addressed_to_me(message)    # marked by the bar below, not a badge as well
            rows = [(header, meta, False)] + quote
            # the first row leaves room for '#id time', so a grouped message can drop its header
            body = wrap([("  ", 0)], "  ", self._spans(text), width, first_width=room)
            if len(body) > FOLD_ROWS and message["id"] not in self.expanded:
                hidden = len(body) - FOLD_SHOW
                body = body[:FOLD_SHOW] + [[("  … ", self.attr["dim"]),
                                            (f"+{hidden} lines · /open {message['id']}", self.attr["dim"])]]
            if mine:                        # a bar down the left edge marks messages addressed to you
                body = [[("▌", self.attr["user"]), (row[0][0][1:], row[0][1])] + row[1:] for row in body]
            rows += _rows(body)
        elif task and int(task.group(1)) in self.by_id:
            rows = []                       # folded under the request it refers to
        else:
            text, kind_name = self._proto_text(message)
            symbol = "⚑ " if kind in ("claim", "release") else "· "   # not └: these don't answer the row above
            body = wrap([(symbol, self.attr["dim"])], "  ", [(text, self.attr[kind_name])], room)
            rows = _rows(body, meta)
        self.cache[message["id"]] = rows
        return rows

    def _reply(self, message, width):
        """Split '↪ #42 text' into the text and a dim one-row quote of #42 (just '#42' if not on screen)."""
        match = REPLY.match(message["text"])
        if not match:
            return message["text"], []
        target = self.by_id.get(int(match.group(1)))
        quote = [("  ", 0), (f"↪ #{match.group(1)}", self.attr["dim"])]
        if target:
            mine = target["sender"] == store.USER
            name = "you" if mine else target["sender"]
            snippet = " ".join(REPLY.sub("", clean(target["text"])).split())
            used = 2 + text_width(quote[1][0]) + 1 + text_width(name) + 2
            quote += [(" ", 0), (name, self.attr["me"] if mine else self.attr["dim"]),
                      (": " + fit(snippet, max(1, width - used - 2)), self.attr["dim"])]
        return message["text"][match.end():], [(quote, None, False)]

    def addressed_to_me(self, message):
        """An @user/@all mention, or a reply to one of the user's own messages."""
        if self.mentions_me(message):
            return True
        match = REPLY.match(message["text"]) if message.get("kind") == "msg" else None
        target = self.by_id.get(int(match.group(1))) if match else None
        return bool(target) and target["sender"] == store.USER and message["sender"] != store.USER

    @staticmethod
    def mentions_me(message):
        return any(token.lower() in ME for token in MENTION.findall(message["text"]))

    @staticmethod
    def _task_summary(events):
        held, order = {}, []
        for kind, agent in events:
            if kind == "claim":
                held[agent] = True
                order.append(agent)
            elif agent in held:
                held[agent] = False
        names = " → ".join(dict.fromkeys(order))
        if any(held.values()):
            return f"{names} is on it" if len(order) == 1 else f"{names} (on it)"
        return f"{names} took this · done ✓"

    def _date_rule(self, message, width):
        try:
            label = datetime.fromisoformat(message["ts"]).strftime("%a %d %b %Y")
        except ValueError:
            label = message["ts"][:10]
        return self._rule(label, self.attr["rule"], width)

    def _rule(self, label, attr, width):
        """A full-width ─── label ─── row."""
        label = f" {label} "
        side = max(2, (width - text_width(label)) // 2)
        return ([("─" * side + label + "─" * (width - side - text_width(label)), attr)], None, False)

    def blocks_backward(self, width):
        """Yield (message, rows) from newest to oldest; rows include the separator above the message."""
        if width != self.cache_width:
            self.cache.clear()
            self.cache_width = width
        messages = self.view if self.view is not None else self.messages
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            block = self.block(message, width)
            if not block:
                continue
            previous = None
            for back in range(index - 1, -1, -1):
                if self.block(messages[back], width):
                    previous = messages[back]
                    break
            if (self.view is None and previous is not None and self.new_after is not None
                    and previous["id"] <= self.new_after < message["id"]):
                separator = [([], None, False), self._rule("new", self.attr["user"], width), ([], None, False)]
            elif previous is None or previous["ts"][:10] != message["ts"][:10]:
                separator = [([], None, False), self._date_rule(message, width), ([], None, False)]
            elif self._continues(previous, message):    # the same agent again: a blank line, no repeated name
                separator, block = [([], None, False)], self._without_header(block, width)
            elif previous["kind"] == "msg" or message["kind"] == "msg":   # protocol rows sit together
                separator = [([], None, False)]
            else:
                separator = []
            yield message, separator + block

    GROUP_SECONDS = 180             # an agent's messages this close together share one header

    def _continues(self, previous, message):
        """True when `message` continues `previous`: same agent, both plain messages, close in time."""
        if not (message["kind"] == previous["kind"] == "msg" and message["sender"] == previous["sender"]
                and message["sender"] in store.AGENTS and not self.addressed_to_me(message)):
            return False
        try:
            gap = datetime.fromisoformat(message["ts"]) - datetime.fromisoformat(previous["ts"])
        except (ValueError, TypeError):     # unparsable, or naive mixed with aware timestamps
            return False
        return 0 <= gap.total_seconds() <= self.GROUP_SECONDS

    def _without_header(self, block, width):
        """Drop the '● sender' row, moving '#id time' onto the first text row when it fits."""
        header, body = block[0], block[1:]
        meta = header[1]
        if not body or not meta:
            return block
        segments, _, band = body[0]
        if sum(text_width(text) for text, _ in segments) + text_width(meta) + 2 > width:
            return block                    # no room: keep the header so the id stays visible
        return [(segments, meta, band)] + body[1:]

    def body_rows(self, width, height):
        """Rows ending at the bottom of the transcript, enough for `height` + scroll."""
        need = height + self.scroll
        rows, owners, exhausted = [], [], True
        for message, block in self.blocks_backward(width):
            rows[:0] = block
            owners[:0] = [message["id"]] * len(block)
            if len(rows) >= need:
                exhausted = False
                break
        if exhausted:
            self.scroll = max(0, min(self.scroll, len(rows) - height))
        end = len(rows) - self.scroll
        self.row_owners = owners[max(0, end - height):end]   # which message each shown row belongs to (clicks)
        return rows[max(0, end - height):end]

    def clicked(self, y, x):
        """A click on the transcript: '#N time' starts '/reply N', a quoted '↪ #N' line jumps to #N."""
        index = y - self.rows_top
        if not 0 <= index < len(self.shown_rows):
            return False
        segments, right, _ = self.shown_rows[index]
        width = self.win.getmaxyx()[1]
        if right and x >= width - text_width(right) - 1:
            self.start_reply(self.row_owners[index])
            return True
        quoted = next((REPLY.match(text + " ") for text, _ in segments if text.startswith("↪ #")), None)
        if quoted:
            self.go_to(int(quoted.group(1)))
            return True
        return False

    def go_to(self, message_id):
        """Show message #N: on screen if loaded, else a history view around it (Esc returns)."""
        if not self.jump_to(message_id):
            self.view = store.read_messages(max(0, message_id - 15), limit=40)
            if not self.jump_to(message_id):
                self.view = None
                self.note(f"#{message_id} isn't in the chat log")
                return
        self.note(f"#{message_id} · Esc returns to latest")

    def jump_to(self, message_id):
        """Scroll so the message's first row sits at the top of the transcript. False if not on screen."""
        below = 0
        for message, block in self.blocks_backward(self.cache_width or 80):
            below += len(block)
            if message["id"] == message_id:
                separator = next((n for n, row in enumerate(block) if row[0]), 0)
                rows = self.body_height - (0 if self.hint_shown else 1)   # once scrolled, the hint takes a row
                self.scroll = max(0, below - separator - rows)
                return True
        return False

    # -- drawing ----------------------------------------------------------

    def _put(self, y, x, text, attr=0):
        height, width = self.win.getmaxyx()
        if 0 <= y < height and 0 <= x < width:
            try:
                self.win.addstr(y, x, fit(text, width - x) if text_width(text) > width - x else text, attr)
            except curses.error:            # the bottom-right cell
                pass

    def _put_row(self, y, row, width):
        segments, right, band = row
        if band and self.attr["band"]:
            self._put(y, 0, " " * width, self.attr["band"])
        x = 0
        for text, attr in segments:
            if band and self.attr["band"]:
                pair = curses.pair_number(attr & curses.A_COLOR)
                if not pair:
                    attr |= self.attr["band"]
                elif pair in self.on_band:  # a coloured word keeps its colour and the band behind it
                    attr = (attr & ~curses.A_COLOR) | curses.color_pair(self.on_band[pair])
            self._put(y, x, text, attr)
            x += text_width(text)
        if right:
            self._put(y, width - text_width(right), right,
                      self.attr["meta"] | (self.attr["band"] if band else 0))


    def activity(self, width):
        if not self.presence:               # status hasn't arrived yet: don't claim anyone is away
            return [("○ checking who's here…", self.attr["dim"])]
        parts = []
        now = time.time()
        for agent in store.AGENTS:
            info = self.presence.get(agent, {})
            state = info.get("state", "unknown")
            held = sorted(int(path[5:]) for path, who in self.claims.items()
                          if who == agent and path.startswith("task:"))
            # the newest task is the one in hand; older ones still held are counted, not shown
            newest = f"#{held[-1]}" + (f" +{len(held) - 1}" if len(held) > 1 else "") if held else ""
            task = f" on {newest}" if held else ""
            since = info.get("since")
            if state == "working":
                text = f"✻ {agent} working{task}" + (f" · {duration(now - since)}" if since else "")
                attr = self.attr[agent]
            elif state == "listening":
                text, attr = f"● {agent} listening" + (f" · has {newest}" if held else ""), self.attr[agent]
            elif state == "idle":
                text, attr = f"○ {agent} idle", self.attr["dim"]
            else:
                text, attr = f"○ {agent} away", self.attr["dim"]
            if info.get("note"):
                text += f" · {clean(info['note'])}"
            parts.append((text, attr))
        return parts

    @staticmethod
    def _hint(width, *options):
        """The first wording that fits, longest first; the shortest is cut as a last resort."""
        return next((text for text in options if text_width(text) <= width), None) or fit(options[-1], width)

    @staticmethod
    def _share(parts, width, gap=3):
        """Cut the longest (text, attr) parts so all of them fit on one row, each keeping its start."""
        room = width - gap * (len(parts) - 1)
        left, out = len(parts), {}
        for number in sorted(range(len(parts)), key=lambda n: text_width(parts[n][0])):
            cap = min(text_width(parts[number][0]), max(1, room // left))
            out[number] = (fit(parts[number][0], cap), parts[number][1])
            room, left = room - cap, left - 1
        return [out[number] for number in range(len(parts))]

    def footer(self):
        guard = self.guard
        mode = guard.get("mode")
        if mode is None:
            return "checking mode…", self.attr["dim"]
        if mode in ("normal", "auto", "custom"):    # rate modes: how much of the window the agents have used
            label = "custom" if mode == "auto" else mode
            window = guard.get("window_seconds") or 60
            per = "min" if window == 60 else f"{window // 60} min" if window % 60 == 0 else f"{window} s"
            left = f"{label} · {guard.get('used', 0)}/{guard.get('message_limit')} per {per}"
            if label == "custom" and guard.get("expires_at"):
                left += f" until {datetime.fromtimestamp(guard['expires_at']).strftime('%H:%M')}"
            if guard.get("blocked") and self.retry_left() > 0:
                return f"{left} · full, agents wait {duration(self.retry_left())}", self.attr["error"]
            return left, self.attr["dim"]
        if mode in ("quiet", "paused"):
            return "paused · agents can't post · /resume", self.attr["error"]
        if mode == "autonomous":                # the older store's timed mode
            left = f"auto until {datetime.fromtimestamp(guard['expires_at']).strftime('%H:%M')}"
            if guard.get("blocked") and self.retry_left() > 0:
                left += f" · agents rate-limited for {duration(self.retry_left())}"
                return left, self.attr["error"]
            return left, self.attr["dim"]
        if guard.get("blocked"):
            if self.refused:
                return "agents paused · your turn", self.attr["error"]
            return "your turn", self.attr["dim"]
        return f"agents {guard.get('agent_streak', 0)}/{guard.get('limit', store.GUARD_LIMIT)} before your turn", self.attr["dim"]

    def draw(self):
        self.win.erase()
        height, width = self.win.getmaxyx()
        if height < 8 or width < 30:
            self._put(0, 0, "Please enlarge the terminal")
            self._show()
            return
        now = time.monotonic()
        self.notes = [n for n in self.notes if n[2] is None or n[2] > now]
        lines, (cursor_row, cursor_col) = self.composer_lines(width)
        options = self.menu()
        if options:                         # the / menu: matching commands, the selected one highlighted
            want = min(len(options), MENU_ROWS)
        else:
            want = len(HELP) + 1 if self.help else len(self.notes)
        # footer, 2 rules, the activity row (and a hint) are fixed; the transcript keeps at least 1 row, so the
        # composer shrinks first (its cursor row stays visible), then the menu/notes
        if self.view is not None:
            count = sum(self.unseen.values())
            waiting = f" · {count} new" if count else ""
            hint = self._hint(width, f" History view{waiting} · Esc returns to latest ", f" History{waiting} · Esc ")
        elif self.scroll and self.unseen:
            count = sum(self.unseen.values())
            hint = self._hint(width, f" ▼ {count} new · {', '.join(self.unseen)} · Esc to jump ",
                              f" ▼ {count} new · Esc to jump ", f" ▼ {count} new · Esc ")
        elif self.scroll:
            hint = self._hint(width, " ▼ Esc to jump to latest ", " ▼ Esc ")
        else:
            hint = ""
        room = height - 5 - bool(hint)
        shown = max(1, min(len(lines), COMPOSER_ROWS, room - want))
        want = min(want, room - shown)
        top = max(0, min(cursor_row - shown + 1, len(lines) - shown))
        if options:
            selected = min(self.menu_index, len(options) - 1)
            first = max(0, min(selected - want + 1, len(options) - want))
            note_rows = [(fit(f"{name} {COMMAND_HELP[name][0]}".ljust(22) + COMMAND_HELP[name][1], width - 2),
                          "menu_on" if number == selected else "dim")
                         for number, name in enumerate(options) if first <= number < first + want]
        elif self.help:
            note_rows = [(fit(line, width - 2), "dim") for line in (HELP + [f"AgentChat {__version__}"])[:want]]
        else:
            note_rows = [(fit(text, width - 2), kind) for text, kind, _ in self.notes[len(self.notes) - want:]]
        # the hint gets a row of its own, outside the messages: drawn over them, it scrolled along with them
        # (curses moves lines in the terminal) and then jumped back, which flickered
        bottom = 1 + 1 + shown + 1 + 1 + len(note_rows) + bool(hint)   # footer, rules, composer, activity, notes
        body = max(1, height - bottom)
        self.body_height, self.hint_shown = body, bool(hint)
        rows = self.body_rows(width, body)
        y = body - len(rows)
        self.rows_top, self.shown_rows = y, rows
        for row in rows:
            self._put_row(y, row, width)
            y += 1
        if hint:
            self._put(body, width - text_width(hint), hint, curses.A_REVERSE)
        y = body + bool(hint)
        for text, kind in note_rows:
            self._put(y, 2, text, self.attr[kind])
            y += 1
        x = 0
        for text, attr in self._share(self.activity(width), width):
            self._put(y, x, text, attr)
            x += text_width(text) + 3
        y += 1
        self._put(y, 0, "─" * width, self.attr["rule"])
        y += 1
        composer_top = y
        for number in range(shown):
            index = top + number
            prefix = "❯ " if index == 0 else "  "
            self._put(y, 0, prefix, self.attr["user"])
            self._put(y, 2, lines[index], 0)
            y += 1
        if not self.draft:
            self._put(composer_top, 2, fit(PLACEHOLDER, width - 3), self.attr["dim"])
        self._put(y, 0, "─" * width, self.attr["rule"])
        y += 1
        left, left_attr = self.footer()
        hints = "? for shortcuts · ctrl+j newline"
        if time.time() - self.ctrl_c_at < QUIT_WINDOW:
            hints = "ctrl+c again to quit"
        elif self.draft and time.time() - self.esc_at < QUIT_WINDOW:
            hints = "esc again to clear"
        self._put(y, 2, left, left_attr)
        if text_width(left) + text_width(hints) + 6 <= width:
            self._put(y, width - text_width(hints) - 1, hints, self.attr["dim"])
        self.win.move(composer_top + cursor_row - top, min(width - 1, 2 + cursor_col))
        self._show()

    def _show(self):
        """Send the frame as one update: the cursor hides while it moves around (a clock ticking once made it
        blink there every second), and terminals with synchronized output show the frame all at once."""
        os.write(sys.stdout.fileno(), FRAME_START)
        self.win.noutrefresh()
        curses.doupdate()
        os.write(sys.stdout.fileno(), FRAME_END)
