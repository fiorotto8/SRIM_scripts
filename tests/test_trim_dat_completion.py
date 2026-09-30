from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import trim_dat_completion as c
from scripts.mode5_lifecycle_probe import one_ion_inputs
from scripts import bulk_geometry_probe as b

TREE = '0xa00009 "End of TRIM.DAT calculation": ("trim.exe" "trim.exe") 178x76+423+359 +423+359'


class CompletionTests(unittest.TestCase):
    def test_exact_allowlist_only(self):
        self.assertEqual(c.completion_candidates(TREE), ['0xa00009'])
        for change in [TREE.replace('End of TRIM.DAT calculation', 'Error'),
                       TREE.replace('calculation"', 'calculation failed"'),
                       TREE.replace('trim.exe', 'other.exe'), TREE.replace('178x76', '1x1')]:
            self.assertEqual(c.completion_candidates(change), [])

    def test_error_dialog_never_acknowledged(self):
        check = c.CompletionChecker(Path('/unused'), lambda: ['0xbad'])
        with patch.object(c, 'acknowledge') as ack:
            self.assertEqual(check(), ['0xbad']); ack.assert_not_called()

    def test_once_only_and_timeout_not_repeated_keys(self):
        with tempfile.TemporaryDirectory() as td:
            check = c.CompletionChecker(Path(td), lambda: [])
            with patch.object(c.subprocess, 'check_output', side_effect=[TREE, 'Map State: IsViewable', TREE, TREE]), \
                 patch.object(c, 'capture') as capture, patch.object(c, 'acknowledge') as ack:
                self.assertEqual(check(), [])
                self.assertEqual(check(), [])
                ack.assert_called_once_with('0xa00009')
                check.acknowledged_at -= 25
                with self.assertRaisesRegex(RuntimeError, '20 seconds'): check()
                self.assertEqual(capture.call_count, 2)
                ack.assert_called_once()

    def test_ambiguous_hidden_and_display_failure_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            check = c.CompletionChecker(Path(td), lambda: [])
            with patch.object(c, 'acknowledge') as ack:
                with patch.object(c.subprocess, 'check_output', return_value=TREE + '\n' + TREE):
                    with self.assertRaisesRegex(RuntimeError, 'Ambiguous'): check()
                with patch.object(c.subprocess, 'check_output', side_effect=[TREE, 'Map State: IsUnMapped']):
                    self.assertEqual(check(), [])
                with patch.object(c.subprocess, 'check_output', side_effect=OSError('display unavailable')):
                    with self.assertRaises(OSError): check()
                ack.assert_not_called()

    def test_one_ion_input_only_changes_count_and_events(self):
        original = b'Ion: Z Mass Energy Angle Number\r\n9 19 100 0 50 0 0\r\nCascades(\r\n5 123 0\r\n'
        content, events = one_ion_inputs(original, b.trim_dat(1))
        self.assertEqual(content, original.replace(b'0 50 0 0', b'0 1 0 0'))
        self.assertEqual(len(events[:-1].decode().splitlines()), 11)
        self.assertTrue(events.endswith(b'\r\n\x1a'))
        with self.assertRaises(ValueError): one_ion_inputs(original, b'broken')


if __name__ == '__main__': unittest.main()
