"""Native Windows process identity used by campaign status and recovery.

Creation times distinguish reused PIDs. Command matching binds a coordinator
specifically to its driver, campaign action and --run-dir argument.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess


def windows_processes(pid=None):
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        return []
    selection = f' -Filter "ProcessId = {pid}"' if pid is not None else ''
    command = ("$ErrorActionPreference='Stop'; $items = @(Get-CimInstance Win32_Process"
               + selection + " | Select-Object ProcessId,CommandLine,ExecutablePath,Name,"
               "@{Name='CreationTime';Expression={$_.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString()}}); "
               'ConvertTo-Json -InputObject $items -Compress')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    value = json.loads(result.stdout.decode('utf-8-sig').strip() or 'null')
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def process_identity(pid=None):
    """Read creation time and image using native handles; no process is modified."""
    if os.name != 'nt':
        return None
    pid = os.getpid() if pid is None else pid
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel.OpenProcess.restype = wt.HANDLE
    kernel.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise ctypes.WinError()
    try:
        creation, exit_time, system, user = (wt.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                      ctypes.byref(system), ctypes.byref(user)):
            raise ctypes.WinError()
        buffer = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError()
        identity = {'pid': pid, 'creation_time': str((creation.dwHighDateTime << 32) | creation.dwLowDateTime),
                    'executable': buffer.value}
        if pid == os.getpid():
            kernel.GetCommandLineW.restype = wt.LPWSTR
            identity['command_line'] = kernel.GetCommandLineW()
        return identity
    finally:
        kernel.CloseHandle(handle)


def same_identity(process, identity):
    # CIM datetime values retain microseconds; native FILETIME has 100 ns units.
    if not identity:
        return True  # Legacy checkpoints still use exact command identity.
    return (process.get('ProcessId') == identity.get('pid')
            and int(process.get('CreationTime') or -1) // 10 == int(identity.get('creation_time') or -2) // 10
            and ntpath.normcase(process.get('ExecutablePath') or '')
            == ntpath.normcase(identity.get('executable') or '')
            and (not identity.get('command_line') or process.get('CommandLine') == identity['command_line']))


def argument_value(command, option):
    match = re.search(r'(?:^|\s)"?' + re.escape(option) + r'"?\s+(?:"([^"]*)"|([^"\s]+))(?=\s|$)',
                      command or '', re.IGNORECASE)
    return (match.group(1) if match.group(1) is not None else match.group(2)) if match else None


def campaign_command_matches(command, driver, run_dir, modes=('campaign', 'extend')):
    text = (command or '').replace('/', '\\')
    driver_pattern = r'(?:^|\s)"?' + re.escape(str(driver).replace('/', '\\')) + r'"?\s+"?(?:' + '|'.join(modes) + r')"?(?=\s|$)'
    directory = argument_value(text, '--run-dir')
    return bool(re.search(driver_pattern, text, re.IGNORECASE) and directory is not None
                and ntpath.normcase(ntpath.normpath(directory))
                == ntpath.normcase(ntpath.normpath(str(run_dir).replace('/', '\\'))))


def runtime_processes(processes, run_dir):
    """Find live executables or supervisors tied to this study's private runtime."""
    runtime = ntpath.normcase(ntpath.normpath(str(Path(run_dir).resolve() / 'runtime')))
    helper = Path(__file__).with_name('cygno_native_run.py')
    found = []
    for process in processes:
        executable = ntpath.normcase(ntpath.normpath(process.get('ExecutablePath') or ''))
        command = process.get('CommandLine') or ''
        cwd = argument_value(command, '--cwd') if str(helper).lower() in command.lower() else None
        candidates = [executable]
        if cwd:
            candidates.append(ntpath.normcase(ntpath.normpath(cwd)))
        if any(path == runtime or path.startswith(runtime + '\\') for path in candidates):
            found.append(process)
    return found
