#!/usr/bin/env python3
"""Versioned authenticated Xvfb launcher without last-client server resets.

The frozen launcher is retained. Only the Xvfb argument vector changes; display
allocation, authorization, readiness and process cleanup retain its policy.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import private_xvfb as original


def server_command(write_fd, auth):
    return ['Xvfb', '-displayfd', str(write_fd), '-screen', '0',
            '1024x768x24', '-nolisten', 'tcp', '-auth', str(auth), '-noreset']


@contextmanager
def private_display(runtime):
    runtime = Path(runtime); started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='srim-xauth-noreset-') as temporary:
        auth = Path(temporary)/'Xauthority'; auth.touch(mode=0o600)
        cookie = secrets.token_hex(16); original.add_auth(auth, 0, cookie)
        read_fd, write_fd = os.pipe(); server = None
        try:
            with (runtime/'.xvfb.log').open('wb') as log:
                server = subprocess.Popen(server_command(write_fd, auth), pass_fds=(write_fd,), stdout=log, stderr=log)
                os.close(write_fd); write_fd = None
                number = original.ready_number(read_fd, server)
                original.add_auth(auth, number, cookie)
                env = dict(os.environ, DISPLAY=f':{number}', XAUTHORITY=str(auth))
                subprocess.run(['xwininfo', '-root', '-tree'], env=env, capture_output=True,
                               check=True, text=True, timeout=5)
                if server.poll() is not None:
                    raise RuntimeError('Xvfb died after readiness check')
                metadata = dict(status='READY', display=f':{number}', server_pid=server.pid,
                                allocation='Xvfb -displayfd', authenticated=True, tcp_listening=False,
                                reset_on_last_client=False, helper=str(Path(__file__).resolve()),
                                ready_seconds=time.monotonic()-started)
                (runtime/'.display-startup.json').write_text(json.dumps(metadata, indent=2)+'\n')
                yield env, server, metadata
        finally:
            original.stop_process(server)
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)


def run(runtime, command):
    saved = original.private_display
    original.private_display = private_display
    try:
        return original.run(runtime, command)
    finally:
        original.private_display = saved


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    raise SystemExit(run(args.runtime, command))
