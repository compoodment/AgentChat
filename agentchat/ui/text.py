"""Measuring and wrapping terminal text with stdlib unicodedata only."""

import re
import unicodedata


MENTION = re.compile(r"(?<![\w.])(@[\w-]+)")   # not inside an email address
ME = ("@user", "@you", "@all")
TASK = re.compile(r"^task:(\d+)$")
REPLY = re.compile(r"^↪ #(\d{1,12})(?!\d)\s*")   # '/reply 42 text' is stored as '↪ #42 text'; longer numbers are text
ISO = re.compile(r"\d{4}-\d\d-\d\dT(\d\d:\d\d)[\d:.]*(?:[+-]\d\d:\d\d|Z)?")



_widths = {}


def cell_width(ch):
    """Terminal cells for one character, using only unicodedata."""
    width = _widths.get(ch)
    if width is None:
        if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            width = 0
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            width = 2
        else:
            width = 1
        _widths[ch] = width
    return width


def text_width(text):
    return sum(cell_width(ch) for ch in text)


def clean(text):
    """Make agent text safe to draw: no control characters, no ZWJ/VS16 that VTE misdraws."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = text.replace("\u200d", "").replace("\ufe0f", "")
    return "".join(ch if ch == "\n" or unicodedata.category(ch) != "Cc" else "\ufffd" for ch in text)


def fit(text, width):
    """Cut text to `width` cells, ending in … when cut."""
    if text_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        if used + cell_width(ch) > width - 1:
            break
        out += ch
        used += cell_width(ch)
    return out + "…"


def wrap(first, indent, segments, width, first_width=None):
    """Wrap (text, attr) segments after the `first` segments; later rows start with `indent`.

    `first_width`, when given, limits only the first row (room for a right-aligned label).
    Returns a list of rows, each a list of (text, attr).
    """
    limit = width if first_width is None else min(width, first_width)
    rows, row = [], list(first)
    start = text_width(indent)
    used = sum(text_width(text) for text, _ in first)

    def newline():
        nonlocal row, used, limit
        rows.append(row)
        row, used, limit = [(indent, 0)] if indent else [], start, width

    for text, attr in segments:
        for number, line in enumerate(text.split("\n")):
            if number:
                newline()
            for word in re.split(r"(\s+)", line):
                if not word:
                    continue
                size = text_width(word)
                if word.isspace():
                    if used > start and used + size <= limit:
                        row.append((word, attr))
                        used += size
                    continue
                if used + size > limit and used > start:
                    newline()
                if used + size > limit:     # a word longer than a row: cut it, measuring each char once
                    begin, taken = 0, 0
                    for index, ch in enumerate(word):
                        cells = cell_width(ch)
                        if used + taken + cells > limit and index > begin:
                            row.append((word[begin:index], attr))
                            newline()
                            begin, taken = index, 0
                        taken += cells
                    word, size = word[begin:], taken
                if word:
                    row.append((word, attr))
                    used += size
    rows.append(row)
    return rows


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def parse_duration(text):
    """'2h', '90m', '1h30m', '45s', 'off' -> seconds, or None if unreadable."""
    text = text.strip().lower()
    if text in ("off", "0", "stop"):
        return 0
    parts = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", text)
    if not text or not parts or not any(parts.groups()):
        return int(text) * 60 if text.isdigit() else None
    hours, minutes, seconds = (int(value or 0) for value in parts.groups())
    return hours * 3600 + minutes * 60 + seconds
