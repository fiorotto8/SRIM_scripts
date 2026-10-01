from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import trim_dat_completion_retry as c

TREE = '0xa00009 "End of TRIM.DAT calculation": ("trim.exe" "trim.exe") 178x76+423+359 +423+359'
VISIBLE = 'Map State: IsViewable'


def output(value):
    return subprocess.CompletedProcess(['xwininfo'], 0, stdout=value, stderr='')


def race(command, kind='BadWindow'):
    return subprocess.CalledProcessError(1, command, output='',
        stderr=f'X Error of failed request: {kind} (invalid Window parameter)\n')


class CompletionRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.original = Mock(return_value=[])
        self.check = c.CompletionChecker(self.runtime, self.original)
        self.capture = patch.object(c.legacy, 'capture').start()
        self.ack = patch.object(c.legacy, 'acknowledge').start()
        self.sleep = patch.object(c.time, 'sleep').start()
        self.addCleanup(patch.stopall)

    def probes(self):
        return json.loads((self.runtime / '.completion-probes.json').read_text())['inspections']

    def test_transient_tree_and_detail_races_restart_entire_inspection(self):
        tree_command = ['xwininfo', '-root', '-tree']
        detail_command = ['xwininfo', '-id', '0xa00009']
        with patch.object(c.subprocess, 'run', side_effect=[
            race(tree_command), output(TREE), race(detail_command, 'BadDrawable'),
            output(TREE), output(VISIBLE),
        ]) as probe:
            self.assertEqual(self.check(), [])
        self.assertEqual(self.original.call_count, 3)
        self.assertEqual([call.args[0] for call in probe.call_args_list],
                         [tree_command, tree_command, detail_command, tree_command, detail_command])
        self.assertTrue(all(call.kwargs == dict(capture_output=True, text=True, timeout=5, check=True)
                            for call in probe.call_args_list))
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.25, 0.5])
        self.ack.assert_called_once_with('0xa00009')
        record = self.probes()[0]
        self.assertEqual(record['status'], 'recovered')
        self.assertEqual(record['attempts'], 3)
        self.assertIn('BadDrawable', record['failures'][1]['stderr'])

    def test_original_error_dialog_wins_on_retry(self):
        self.original.side_effect = [[], ['0xbad']]
        with patch.object(c.subprocess, 'run', side_effect=race(['xwininfo', '-root', '-tree'])) as probe:
            self.assertEqual(self.check(), ['0xbad'])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(self.probes()[0]['status'], 'error_dialog')
        self.ack.assert_not_called()

    def test_actual_multiline_xwininfo_race_and_standard_style_recover(self):
        observed = 'X Error: 9: Bad Drawable\n  Request Major code: 14\n  ResourceID 0xa00009\nX Error: 3: Bad Window\n'
        for stderr in [observed, 'X Error of failed request: BadWindow (invalid Window parameter)\n']:
            with self.subTest(stderr=stderr):
                check = c.CompletionChecker(self.runtime, self.original)
                failure = subprocess.CalledProcessError(1, ['xwininfo', '-root', '-tree'], stderr=stderr)
                with patch.object(c.subprocess, 'run', side_effect=[failure, output('')]) as probe:
                    self.assertEqual(check(), [])
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(self.probes()[0]['status'], 'recovered')
                self.assertEqual(self.probes()[0]['failures'][0]['stderr'], stderr)
        self.ack.assert_not_called()

    def test_persistent_race_fails_after_three_probes(self):
        with patch.object(c.subprocess, 'run', side_effect=race(['xwininfo', '-root', '-tree'])) as probe:
            with self.assertRaisesRegex(RuntimeError, 'TRIM_WATCHDOG_ERROR'):
                self.check()
        self.assertEqual(probe.call_count, 3)
        self.assertEqual(self.original.call_count, 3)
        self.assertEqual(self.probes()[0]['status'], 'failed')
        self.assertEqual(len(self.probes()[0]['failures']), 3)
        self.ack.assert_not_called()

    def test_unknown_x11_error_display_loss_and_timeout_fail_without_retry(self):
        for failure in [race(['xwininfo'], 'BadAccess'),
                        subprocess.CalledProcessError(1, ['xwininfo'], stderr='X Error: 10: Bad Access\n'),
                        subprocess.CalledProcessError(1, ['xwininfo'], stderr='X Error: 9: Bad Window\n'),
                        subprocess.CalledProcessError(1, ['xwininfo'], stderr='unclassified failure mentions BadWindow\n'),
                        subprocess.CalledProcessError(1, ['xwininfo'], stderr='unable to open display'),
                        OSError('display unavailable'), subprocess.TimeoutExpired(['xwininfo'], 5)]:
            with self.subTest(failure=failure):
                check = c.CompletionChecker(self.runtime, self.original)
                with patch.object(c.subprocess, 'run', side_effect=failure) as probe:
                    with self.assertRaisesRegex(RuntimeError, 'TRIM_WATCHDOG_ERROR'):
                        check()
                self.assertEqual(probe.call_count, 1)
        self.sleep.assert_not_called()
        self.ack.assert_not_called()

    def test_exact_allowlist_hidden_and_ambiguous_windows(self):
        for changed in [TREE.replace('calculation"', 'calculation failed"'),
                        TREE.replace('trim.exe', 'other.exe'), TREE.replace('178x76', '1x1')]:
            with patch.object(c.subprocess, 'run', return_value=output(changed)) as probe:
                self.assertEqual(self.check(), [])
                self.assertEqual(probe.call_count, 1)
        with patch.object(c.subprocess, 'run', side_effect=[output(TREE), output('Map State: IsUnMapped')]):
            self.assertEqual(self.check(), [])
        with patch.object(c.subprocess, 'run', return_value=output(TREE + '\n' + TREE)):
            with self.assertRaisesRegex(RuntimeError, 'TRIM_WATCHDOG_ERROR: Ambiguous'):
                self.check()
        self.ack.assert_not_called()

    def test_post_ack_keeps_error_watchdog_without_secondary_probe_or_key(self):
        with patch.object(c.subprocess, 'run', side_effect=[output(TREE), output(VISIBLE)]) as probe:
            self.assertEqual(self.check(), [])
            self.assertEqual(self.check(), [])
            self.original.return_value = ['0xbad']
            self.assertEqual(self.check(), ['0xbad'])
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(self.original.call_count, 3)
        self.ack.assert_called_once()
        self.assertEqual(json.loads((self.runtime / 'completion-ack.json').read_text())['status'], 'sent')

    def test_key_injection_failure_is_never_retried(self):
        self.ack.side_effect = RuntimeError('Completion window disappeared before acknowledgement')
        with patch.object(c.subprocess, 'run', side_effect=[output(TREE), output(VISIBLE)]) as probe:
            with self.assertRaisesRegex(RuntimeError, 'TRIM_WATCHDOG_ERROR: completion acknowledgement failed'):
                self.check()
            with self.assertRaisesRegex(RuntimeError, 'no second key permitted'):
                self.check()
        self.assertEqual(probe.call_count, 2)
        self.ack.assert_called_once()
        self.sleep.assert_not_called()

    def test_evidence_capture_failure_stops_before_acknowledgement(self):
        self.capture.side_effect = OSError('display closed')
        with patch.object(c.subprocess, 'run', side_effect=[output(TREE), output(VISIBLE)]):
            with self.assertRaisesRegex(RuntimeError, 'TRIM_WATCHDOG_ERROR: cannot capture'):
                self.check()
        self.ack.assert_not_called()
        self.sleep.assert_not_called()

    def test_stall_deadline_preserved_and_capture_failure_does_not_mask_it(self):
        with patch.object(c.subprocess, 'run', side_effect=[output(TREE), output(VISIBLE)]) as probe:
            self.check()
            acknowledged = self.check.acknowledged_at
            self.capture.side_effect = OSError('display closed')
            with patch.object(c.time, 'monotonic', return_value=acknowledged + 21):
                with self.assertRaisesRegex(RuntimeError, 'TRIM_WATCHDOG_ERROR:.*20 seconds'):
                    self.check()
        self.assertEqual(self.check.acknowledged_at, acknowledged)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(self.original.call_count, 2)
        self.ack.assert_called_once()

    def test_worker_enforces_mode_and_restores_original_checker(self):
        import pysrim_runner as runner
        (self.runtime / 'TRIM.IN').write_text('Cascades(\n2 123 0\n')
        with self.assertRaisesRegex(ValueError, 'mode-5 only'):
            c.run(self.runtime, 'test')
        (self.runtime / 'TRIM.IN').write_text('Cascades(\n5 123 0\n')
        original = runner.check_trim_dialogs
        with patch.object(runner, 'configure_recoil_capacity') as capacity, \
             patch.object(runner, 'monitored_trim_run', side_effect=RuntimeError('worker failed')):
            with self.assertRaisesRegex(RuntimeError, 'worker failed'):
                c.run(self.runtime, 'test')
        capacity.assert_called_once_with(self.runtime)
        self.assertIs(runner.check_trim_dialogs, original)


if __name__ == '__main__':
    unittest.main()
