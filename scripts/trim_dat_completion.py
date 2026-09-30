"""Opt-in mode-5 completion handling; never acknowledge error dialogs.

The legacy runner stays immutable. Install this checker only inside a private
mode-5 worker. One exact, observed normal-completion dialog is acknowledged;
successful process exit and independently checked outputs are still required.
"""
from __future__ import annotations
import ctypes
import ctypes.util
import os
from pathlib import Path
import re
import subprocess
import time

from scripts.preliminary_report import atomic_text
from scripts.staged_scan import write_json

TITLE = 'End of TRIM.DAT calculation'


def completion_candidates(tree):
    pattern = (r'(0x[0-9a-f]+) "' + re.escape(TITLE) +
               r'": \("trim\.exe" "trim\.exe"\)\s+(\d+)x(\d+)[+-]')
    return [m[1] for m in re.finditer(pattern, tree)
            if int(m[2]) > 20 and int(m[3]) > 20]


def capture(runtime, prefix, tree):
    from PIL import ImageGrab
    atomic_text(runtime / f'{prefix}-window-tree.txt', tree)
    ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(runtime / f'{prefix}-screen.png')


def acknowledge(window):
    """Send one Return to the revalidated, focused window on this private Xvfb."""
    x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
    xtst = ctypes.CDLL(ctypes.util.find_library('Xtst'))
    ptr, win = ctypes.c_void_p, ctypes.c_ulong
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]; x11.XOpenDisplay.restype = ptr
    x11.XFetchName.argtypes = [ptr, win, ctypes.POINTER(ctypes.c_char_p)]
    x11.XFree.argtypes = [ptr]
    x11.XSetInputFocus.argtypes = [ptr, win, ctypes.c_int, win]
    x11.XGetInputFocus.argtypes = [ptr, ctypes.POINTER(win), ctypes.POINTER(ctypes.c_int)]
    x11.XKeysymToKeycode.argtypes = [ptr, win]; x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XSync.argtypes = [ptr, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ptr]
    xtst.XTestFakeKeyEvent.argtypes = [ptr, ctypes.c_uint, ctypes.c_int, win]
    display = x11.XOpenDisplay(os.environ['DISPLAY'].encode())
    if not display: raise RuntimeError('Cannot open private display for completion acknowledgement')
    try:
        name = ctypes.c_char_p()
        if not x11.XFetchName(display, int(window, 16), ctypes.byref(name)):
            raise RuntimeError('Completion window disappeared before acknowledgement')
        try:
            if name.value != TITLE.encode(): raise RuntimeError('Completion title changed')
        finally: x11.XFree(ctypes.cast(name, ptr))
        x11.XSetInputFocus(display, int(window, 16), 2, 0)
        x11.XSync(display, 0)
        focused, revert = win(), ctypes.c_int()
        x11.XGetInputFocus(display, ctypes.byref(focused), ctypes.byref(revert))
        if focused.value != int(window, 16): raise RuntimeError('Could not focus exact completion dialog')
        key = x11.XKeysymToKeycode(display, 0xff0d)  # Return
        if not key: raise RuntimeError('Return key unavailable')
        if not xtst.XTestFakeKeyEvent(display, key, 1, 0): raise RuntimeError('Completion key press failed')
        if not xtst.XTestFakeKeyEvent(display, key, 0, 0): raise RuntimeError('Completion key release failed')
        x11.XSync(display, 0)
    finally: x11.XCloseDisplay(display)


class CompletionChecker:
    def __init__(self, runtime, original):
        self.runtime, self.original = runtime, original
        self.acknowledged_at = None

    def __call__(self):
        errors = self.original()
        if errors: return errors  # Original fail-closed error path always wins.
        tree = subprocess.check_output(['xwininfo', '-root', '-tree'], text=True, timeout=5)
        windows = completion_candidates(tree)
        if len(windows) > 1: raise RuntimeError('Ambiguous completion windows; no action')
        if self.acknowledged_at is not None:
            if time.monotonic() - self.acknowledged_at > 20:
                capture(self.runtime, 'post-completion-stall', tree)
                raise RuntimeError('TRIM did not exit within 20 seconds after completion acknowledgement')
            return []  # Never send a second key, even if the window persists.
        if windows:
            details = subprocess.check_output(['xwininfo', '-id', windows[0]], text=True, timeout=5)
            if 'Map State: IsViewable' not in details: return []
            capture(self.runtime, 'normal-completion', tree)
            write_json(self.runtime / 'completion-ack.json', dict(title=TITLE, window=windows[0],
                       action='one Return to exact visible completion window', status='attempting', epoch=time.time()))
            acknowledge(windows[0])
            self.acknowledged_at = time.monotonic()
            write_json(self.runtime / 'completion-ack.json', dict(title=TITLE, window=windows[0],
                       action='one Return to exact visible completion window', status='sent', epoch=time.time()))
        return []


def run(runtime: Path, description: str):
    import pysrim_runner as runner
    from scripts.binding_probe import ArchivedInput
    lines = (runtime / 'TRIM.IN').read_text(encoding='ascii').splitlines()
    mode = lines[next(i for i, line in enumerate(lines) if line.startswith('Cascades(')) + 1].split()[0]
    if mode != '5': raise ValueError('Completion acknowledgement is mode-5 only')
    checker = CompletionChecker(runtime, runner.check_trim_dialogs)
    original = runner.check_trim_dialogs
    runner.check_trim_dialogs = checker
    try:
        runner.configure_recoil_capacity(runtime)
        runner.monitored_trim_run(ArchivedInput(), runtime, dict(description=description))
        if checker.acknowledged_at is None: raise RuntimeError('Mode-5 exited without expected completion evidence')
    finally: runner.check_trim_dialogs = original
