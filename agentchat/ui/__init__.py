"""Curses chat screen for the user.

The layout is a plain transcript: one block per message, an activity row, the input box and a footer. The screen
only displays and sends: it never moves read positions or applies agent rules. Modules: text (measuring,
wrapping), render (how it looks), composer (the input box), screen (state), app (main loop).
"""

from agentchat.ui.app import main
from agentchat.ui.screen import Screen
from agentchat.ui.text import cell_width, clean, fit, parse_duration, text_width, wrap

__all__ = ["Screen", "cell_width", "clean", "fit", "main", "parse_duration", "text_width", "wrap"]
