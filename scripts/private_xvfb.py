#!/usr/bin/env python3
"""Allocate Xvfb atomically with -displayfd, authenticated and readiness-checked."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import select
import signal
import subprocess
import sys
import tempfile
import time


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)


def ready_number(fd, process, timeout=20):
    deadline = time.monotonic() + timeout; data = b''
    while b'\n' not in data:
        if process.poll() is not None: raise RuntimeError('Xvfb exited before readiness')
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise RuntimeError('Xvfb displayfd readiness timeout')
        if not select.select([fd], [], [], min(remaining, 1))[0]: continue
        part = os.read(fd, 128)
        if not part: raise RuntimeError('Xvfb closed displayfd before readiness')
        data += part
        if len(data) > 32: raise RuntimeError('Invalid Xvfb displayfd response')
    number = data.strip()
    if not number.isdigit() or int(number) > 65535: raise RuntimeError('Invalid Xvfb display number')
    return int(number)


def add_auth(path, display, cookie):
    # Keep the cookie out of process arguments and logs. The server reads the
    # cookie from the initial record; the actual client display is added later.
    subprocess.run(['xauth', '-f', str(path), 'source', '-'],
                   input=f'add :{display} MIT-MAGIC-COOKIE-1 {cookie}\n',
                   text=True, capture_output=True, check=True, timeout=5)


@contextmanager
def private_display(runtime):
    runtime = Path(runtime); started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='srim-xauth-') as temporary:
        auth = Path(temporary) / 'Xauthority'; auth.touch(mode=0o600)
        cookie = secrets.token_hex(16); add_auth(auth, 0, cookie)
        read_fd, write_fd = os.pipe(); server = None
        try:
            with (runtime / '.xvfb.log').open('wb') as log:
                server = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0',
                                           '1024x768x24', '-nolisten', 'tcp', '-auth', str(auth)],
                                          pass_fds=(write_fd,), stdout=log, stderr=log)
                os.close(write_fd); write_fd = None
                number = ready_number(read_fd, server)
                add_auth(auth, number, cookie)
                env = dict(os.environ, DISPLAY=f':{number}', XAUTHORITY=str(auth))
                subprocess.run(['xwininfo', '-root', '-tree'], env=env, capture_output=True,
                               check=True, text=True, timeout=5)
                if server.poll() is not None: raise RuntimeError('Xvfb died after readiness check')
                metadata = dict(status='READY', display=f':{number}', server_pid=server.pid,
                                allocation='Xvfb -displayfd', authenticated=True, tcp_listening=False,
                                ready_seconds=time.monotonic() - started)
                (runtime / '.display-startup.json').write_text(json.dumps(metadata, indent=2) + '\n')
                yield env, server, metadata
        finally:
            stop_process(server)
            os.close(read_fd)
            if write_fd is not None: os.close(write_fd)


def run(runtime, command):
    if not command: raise ValueError('Missing child command')
    child = None
    def stop(sig, frame): raise InterruptedError(f'Private display interrupted by signal {sig}')
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, stop)
    try:
        with private_display(runtime) as (env, server, metadata):
            child = subprocess.Popen(command, env=env)
            try:
                while child.poll() is None:
                    if server.poll() is not None: raise RuntimeError('Private Xvfb died during worker execution')
                    try: child.wait(timeout=.5)
                    except subprocess.TimeoutExpired: pass
                return child.returncode
            finally: stop_process(child)
    except InterruptedError:
        stop_process(child); return 130


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    raise SystemExit(run(args.runtime, command))
