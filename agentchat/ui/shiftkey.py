"""Whether Shift is held right now, for Shift+Enter (a new line instead of sending).

Some terminals (VTE-based ones, for example) send Enter and Shift+Enter as the same byte, so the chat asks the X
server for the keyboard's modifier state the moment Enter arrives (libX11 through ctypes: no extra dependency).
Without X, as on a console or over SSH, it answers False and Enter sends as before. AGENTCHAT_SHIFT_ENTER=0 turns it
off. For tests, AGENTCHAT_SHIFT_FILE names a file whose existence stands in for a held Shift.
"""

import ctypes
import ctypes.util
import os

SHIFT_MASK = 1
_x = None                               # (libX11, display, root) once opened; False when there's no X to ask


def shift_held():
    stand_in = os.environ.get("AGENTCHAT_SHIFT_FILE")
    if stand_in:
        return os.path.exists(stand_in)
    global _x
    if _x is None:
        _x = _open()
    if not _x:
        return False
    library, display, root = _x
    root_found, child = ctypes.c_ulong(), ctypes.c_ulong()
    root_x, root_y, x, y = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    mask = ctypes.c_uint()
    if not library.XQueryPointer(display, root, ctypes.byref(root_found), ctypes.byref(child), ctypes.byref(root_x),
                                 ctypes.byref(root_y), ctypes.byref(x), ctypes.byref(y), ctypes.byref(mask)):
        return False
    return bool(mask.value & SHIFT_MASK)


def _open():
    if os.environ.get("AGENTCHAT_SHIFT_ENTER") == "0" or not os.environ.get("DISPLAY"):
        return False
    name = ctypes.util.find_library("X11")
    if not name:
        return False
    try:
        library = ctypes.cdll.LoadLibrary(name)
    except OSError:
        return False
    library.XOpenDisplay.restype = ctypes.c_void_p
    library.XOpenDisplay.argtypes = [ctypes.c_char_p]
    library.XDefaultRootWindow.restype = ctypes.c_ulong
    library.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    library.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
    display = library.XOpenDisplay(None)
    if not display:
        return False
    return library, display, library.XDefaultRootWindow(display)
