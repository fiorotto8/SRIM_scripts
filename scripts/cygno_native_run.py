#!/usr/bin/env python3
"""Supervise one owned native TRIM process and its verified completion dialog.

SRIM mode 5 displays 'End of TRIM.DAT calculation' even with TRIMAUTO=1.
Only acknowledge that exact dialog when its completed count equals the request.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


def process_windows(pid):
    """Return only visible dialogs belonging to the launched process."""
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user.GetDlgCtrlID.argtypes = [wt.HWND]
    user.IsWindowVisible.argtypes = [wt.HWND]
    user.EnumWindows.argtypes = [callback_type, wt.LPARAM]
    user.EnumChildWindows.argtypes = [wt.HWND, callback_type, wt.LPARAM]
    result = []

    def read(hwnd):
        text = ctypes.create_unicode_buffer(4096)
        user.GetWindowTextW(hwnd, text, len(text))
        return text.value

    def inspect(hwnd, parameter):
        owner = wt.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        cls = ctypes.create_unicode_buffer(200)
        user.GetClassNameW(hwnd, cls, len(cls))
        if owner.value == pid and cls.value == '#32770' and user.IsWindowVisible(hwnd):
            children = []

            def child(ch, lp):
                children.append({'handle': int(ch), 'text': read(ch), 'control_id': user.GetDlgCtrlID(ch)})
                return True

            callback = callback_type(child)
            user.EnumChildWindows(hwnd, callback, 0)
            result.append({'handle': int(hwnd), 'title': read(hwnd), 'children': children})
        return True

    callback = callback_type(inspect)
    user.EnumWindows(callback, 0)
    return result


def write_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + f'.tmp-{os.getpid()}')
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    for attempt in range(50):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(.1)


def completion_acknowledgement(dialog, ions):
    """Validate an owned dialog before returning its sole OK button and count."""
    text = ' '.join(child['text'] for child in dialog['children'])
    if dialog['title'] != 'End of TRIM.DAT calculation':
        raise RuntimeError('Unexpected native TRIM dialog: ' + dialog['title'] + ' ' + text)
    match = re.search(r'Calculation Complete\s*\((\d+) Ions?\)', text)
    if not match or int(match.group(1)) != ions:
        raise RuntimeError('TRIM completion count does not match requested histories: ' + text)
    buttons = [child for child in dialog['children']
               if child['text'].strip().replace('&', '') == 'OK']
    if len(buttons) != 1:
        raise RuntimeError('Ambiguous completion acknowledgement button')
    return buttons[0], int(match.group(1)), text


def acknowledge_completion(dialog, button):
    user = ctypes.windll.user32
    user.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user.PostMessageW.restype = wt.BOOL
    if not user.PostMessageW(dialog['handle'], 0x0111, button['control_id'], button['handle']):
        raise ctypes.WinError()


def supervise_trim(executable, cwd, timeout_s, ions, log_path, stop_requested=None):
    """Run native TRIM, persisting progress and terminating only its owned handle.

    ``stop_requested`` is an optional callable for hard interruption. Campaign
    graceful stops are handled between batches and do not cancel this function.
    stdout/stderr use temporary files so a verbose child cannot block on pipes.
    """
    if os.name != 'nt':
        raise RuntimeError('This supervisor runs under native Windows Python')
    if ions < 1 or timeout_s <= 0:
        raise ValueError('TRIM histories and timeout must be positive')
    executable, cwd = Path(executable).resolve(), Path(cwd).resolve()
    start = time.monotonic()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen([str(executable)], cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr)
        state = {'pid': process.pid, 'executable': str(executable), 'cwd': str(cwd),
                 'requested_ions': ions, 'completed_dialog_ions': None, 'status': 'running', 'dialogs': []}
        # The Popen handle owns the process throughout supervision; PID reuse
        # cannot redirect terminate()/kill() at a different Windows process.
        try:
            write_json(log_path, state)
            acknowledged = set()
            while process.poll() is None:
                for dialog in process_windows(process.pid):
                    button, completed, text = completion_acknowledgement(dialog, ions)
                    if dialog['handle'] in acknowledged:
                        continue
                    acknowledge_completion(dialog, button)
                    acknowledged.add(dialog['handle'])
                    state['completed_dialog_ions'] = completed
                    state['dialogs'].append({'title': dialog['title'], 'text': text,
                                            'action': 'acknowledged verified completion',
                                            'control_id': button['control_id']})
                state['elapsed_s'] = time.monotonic() - start
                write_json(log_path, state)
                if stop_requested is not None and stop_requested():
                    raise InterruptedError('Native TRIM interruption requested')
                if state['elapsed_s'] > timeout_s:
                    raise TimeoutError('Native TRIM runtime exceeded configured timeout')
                time.sleep(.25)
            process.wait(timeout=10)
            if process.returncode:
                stderr.seek(0)
                raise RuntimeError(f'Native TRIM exit status {process.returncode}: '
                                   + stderr.read().decode('latin-1', errors='replace')[-3000:])
            state.update(status='complete', exit_code=process.returncode, elapsed_s=time.monotonic() - start)
            write_json(log_path, state)
            return state
        except BaseException as error:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            state.update(status='failed', elapsed_s=time.monotonic() - start, error=str(error))
            write_json(log_path, state)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--cwd', type=Path, required=True)
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--ions', type=int, required=True)
    parser.add_argument('--log', type=Path, required=True)
    args = parser.parse_args()
    supervise_trim(args.exe, args.cwd, args.timeout, args.ions, args.log)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
