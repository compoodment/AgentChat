"""How the screen looks: message blocks, activity row, footer and draw()."""

import curses
import re
import time
from datetime import datetime

from agentchat import store
from agentchat.ui.composer import COMMAND_HELP, MENU_ROWS, QUIT_WINDOW
from agentchat.ui.text import ISO, ME, MENTION, REPLY, TASK, clean, duration, fit, text_width, wrap


FOLD_ROWS, FOLD_SHOW = 16, 12   # agent messages longer than FOLD_ROWS show FOLD_SHOW rows until /open
INLINE = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*)")
COMPOSER_ROWS = 5           # the composer grows up to this many rows, then scrolls
HELP = ["Enter send · Ctrl+J or Alt+Enter new line · ↑↓ history · Tab completes names",
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
        """`code`, **bold** and @mentions inside one line of prose."""
        for part in INLINE.split(text):
            if not part:
                continue
            if part.startswith("`") and part.endswith("`") and len(part) > 2:
                out.append((part[1:-1], self.attr["code"]))
            elif part.startswith("**") and part.endswith("**") and len(part) > 4:
                self._inline(part[2:-2], attr | curses.A_BOLD, out)
            else:
                for piece in MENTION.split(part):
                    if not piece:
                        continue
                    if MENTION.fullmatch(piece):
                        out.append((piece, self.attr["me"] if piece.lower() in ME else attr | curses.A_BOLD))
                    elif self.highlight:        # /find matches stand out until Esc or the next send
                        for bit in re.split(f"({re.escape(self.highlight)})", piece, flags=re.IGNORECASE):
                            if bit:
                                out.append((bit, self.attr["found"] if bit.casefold() == self.highlight.casefold()
                                            else attr))
                    else:
                        out.append((piece, attr))

    def _spans(self, text, attr=0):
        """Light markdown: fenced code, `code`, **bold**, - lists, # headings and @mentions."""
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
            bullet = re.match(r"(\s*)[-*] (.*)", line)
            if heading:
                self._inline(heading.group(1), attr | curses.A_BOLD, out)
            elif bullet:
                out.append((bullet.group(1) + "• ", self.attr["dim"]))
                self._inline(bullet.group(2), attr, out)
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
            mine = self.addressed_to_me(message)
            if mine:
                header += [("  ", 0), (" → you ", self.attr["me"])]
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
            body = wrap([("  └  ", self.attr["dim"])], "     ", [(text, self.attr[kind_name])], room)
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

    def _is_proto(self, message):
        return message["kind"] != "msg"

    def _day(self, message):
        return message["ts"][:10]

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
            elif previous is None or self._day(previous) != self._day(message):
                separator = [([], None, False), self._date_rule(message, width), ([], None, False)]
            elif self._continues(previous, message):
                separator, block = [], self._without_header(block, width)
            elif not (self._is_proto(previous) and self._is_proto(message)):
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
        rows, exhausted = [], True
        for _, block in self.blocks_backward(width):
            rows[:0] = block
            if len(rows) >= need:
                exhausted = False
                break
        if exhausted:
            self.scroll = max(0, min(self.scroll, len(rows) - height))
        end = len(rows) - self.scroll
        return rows[max(0, end - height):end]

    def jump_to(self, message_id):
        """Scroll so the message's first row sits at the top of the transcript. False if not on screen."""
        below = 0
        for message, block in self.blocks_backward(self.cache_width or 80):
            below += len(block)
            if message["id"] == message_id:
                separator = next((n for n, row in enumerate(block) if row[0]), 0)
                self.scroll = max(0, below - separator - self.body_height)
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
            if band and self.attr["band"] and not attr & curses.A_COLOR:
                attr |= self.attr["band"]
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
            task = f" on #{held[0]}" if held else ""
            since = info.get("since")
            if state == "working":
                text = f"✻ {agent} working{task}" + (f" · {duration(now - since)}" if since else "")
                attr = self.attr[agent]
            elif state == "listening":
                text, attr = f"● {agent} listening" + (f" · has #{held[0]}" if held else ""), self.attr[agent]
            elif state == "idle":
                text, attr = f"○ {agent} idle", self.attr["dim"]
            else:
                text, attr = f"○ {agent} away", self.attr["dim"]
            if info.get("note"):
                text += f" · {clean(info['note'])}"
            parts.append((text, attr))
        return parts

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
            self.win.noutrefresh()
            curses.doupdate()
            return
        now = time.monotonic()
        self.notes = [n for n in self.notes if n[2] is None or n[2] > now]
        lines, (cursor_row, cursor_col) = self.composer_lines(width)
        shown = min(len(lines), COMPOSER_ROWS)
        top = max(0, min(cursor_row - shown + 1, len(lines) - shown))
        options = self.menu()
        if options:                         # the / menu: matching commands, the selected one highlighted
            selected = min(self.menu_index, len(options) - 1)
            first = max(0, min(selected - MENU_ROWS + 1, len(options) - MENU_ROWS))
            note_rows = [(fit(f"{name} {COMMAND_HELP[name][0]}".ljust(22) + COMMAND_HELP[name][1], width - 2),
                          "menu_on" if number == selected else "dim")
                         for number, name in enumerate(options) if first <= number < first + MENU_ROWS]
        elif self.help:
            note_rows = [(fit(line, width - 2), "dim") for line in HELP]
        else:
            note_rows = [(fit(text, width - 2), kind) for text, kind, _ in self.notes]
        bottom = 1 + 1 + shown + 1 + 1 + len(note_rows)     # footer, rule, composer, rule, activity, notes
        body = height - bottom
        self.body_height = body
        rows = self.body_rows(width, body)
        y = body - len(rows)
        for row in rows:
            self._put_row(y, row, width)
            y += 1
        if self.view is not None:
            waiting = sum(self.unseen.values())
            hint = f" History view{f' · {waiting} new' if waiting else ''} · Esc returns to latest "
        elif self.scroll and self.unseen:
            hint = f" ▼ {sum(self.unseen.values())} new · {', '.join(self.unseen)} · Esc to jump "
        elif self.scroll:
            hint = " ▼ Esc to jump to latest "
        else:
            hint = ""
        if hint:
            self._put(body - 1, width - text_width(hint), hint, curses.A_REVERSE)
        y = body
        for text, kind in note_rows:
            self._put(y, 2, text, self.attr[kind])
            y += 1
        x = 0
        for text, attr in self.activity(width):
            if x:
                x += 3
            self._put(y, x, text, attr)
            x += text_width(text)
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
        self.win.noutrefresh()
        curses.doupdate()
