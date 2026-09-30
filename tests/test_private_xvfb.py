import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
from scripts import private_xvfb as x


class DisplayTests(unittest.TestCase):
    def test_readiness_number_requires_valid_response(self):
        for response, expected in [(b'123\n',123),(b'bad\n',None),(b'123junk\n',None),(b'999999\n',None)]:
            read,write=os.pipe()
            try:
                os.write(write,response)
                if expected is None:
                    with self.assertRaises(RuntimeError):x.ready_number(read,Mock(poll=lambda:None),.1)
                else:self.assertEqual(x.ready_number(read,Mock(poll=lambda:None),.1),expected)
            finally:os.close(read);os.close(write)

    def test_dead_server_and_timeout_rejected(self):
        read,write=os.pipe()
        try:
            with self.assertRaisesRegex(RuntimeError,'exited'):x.ready_number(read,Mock(poll=lambda:1),.1)
            with self.assertRaisesRegex(RuntimeError,'timeout'):x.ready_number(read,Mock(poll=lambda:None),.01)
        finally:os.close(read);os.close(write)

    def test_live_authenticated_display_and_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            with x.private_display(Path(td)) as (env,server,metadata):
                self.assertEqual(metadata['status'],'READY')
                self.assertEqual(subprocess.run(['xwininfo','-root'],env=env,capture_output=True).returncode,0)
                auth=Path(env['XAUTHORITY']);self.assertEqual(auth.stat().st_mode & 0o777,0o600)
            self.assertIsNotNone(server.poll());self.assertFalse(auth.exists())

    def test_cli_propagates_child_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            result=subprocess.run([sys.executable,x.__file__,'--runtime',td,'--',sys.executable,'-c','raise SystemExit(7)'],capture_output=True,timeout=20)
            self.assertEqual(result.returncode,7,result.stderr.decode())


if __name__=='__main__':unittest.main()
