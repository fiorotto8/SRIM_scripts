"""Native campaign lifecycle and ownership; never execute SRIM or TRIM."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import cygno_campaign as campaign
import cygno_native_run as native
import cygno_process as processes
import cygno_study as study


class ProcessIdentityTests(unittest.TestCase):
    def test_command_binds_the_exact_run_dir_option_and_campaign_action(self):
        driver = r'C:\SRIM work\scripts\cygno_study.py'
        out = r'C:\SRIM work\runs\study'
        good = f'python.exe -u "{driver}" campaign --run-dir "{out}"'
        self.assertTrue(processes.campaign_command_matches(good, driver, out))
        self.assertTrue(processes.campaign_command_matches(good.replace('campaign', 'extend'), driver, out))
        for bad in (good.replace('campaign', 'report'), good.replace('--run-dir', '--config'),
                    good.replace(out, out + '_other'), good + '_other'):
            self.assertFalse(processes.campaign_command_matches(bad, driver, out), bad)

    def test_identity_checks_creation_time_and_executable_as_well_as_pid(self):
        identity = {'pid': 123, 'creation_time': '134368974752489342', 'executable': r'C:\Python\python.exe'}
        snapshot = {'ProcessId': 123, 'CreationTime': '134368974752489340', 'ExecutablePath': r'c:\python\PYTHON.EXE'}
        self.assertTrue(processes.same_identity(snapshot, identity))
        for change in ({'ProcessId': 124}, {'CreationTime': '134368974752589340'}, {'ExecutablePath': r'C:\other.exe'}):
            self.assertFalse(processes.same_identity({**snapshot, **change}, identity))

    def test_saved_original_command_identity_supports_relative_driver_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            identity = {'pid': 123, 'creation_time': '1000', 'executable': r'C:\Python\python.exe',
                        'command_line': 'python.exe scripts\\cygno_study.py campaign --run-dir runs\\study'}
            snapshot = {'ProcessId': 123, 'CreationTime': '1000', 'ExecutablePath': r'C:\Python\python.exe',
                        'CommandLine': identity['command_line']}
            study.core.atomic_write_json(out / 'campaign_state.json',
                                         {'pid': 123, 'coordinator_platform': 'nt', 'status': 'running',
                                          'process_identity': identity})
            with mock.patch.object(processes, 'windows_processes', return_value=[snapshot]):
                self.assertTrue(study.study_status(out)['campaign_process_active'])
            snapshot['CommandLine'] += ' --other'
            with mock.patch.object(processes, 'windows_processes', return_value=[snapshot]):
                self.assertFalse(study.study_status(out)['campaign_process_active'])

    def test_private_runtime_detection_excludes_installed_and_sibling_trim(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'study'
            snapshots = [
                {'ProcessId': 1, 'ExecutablePath': str(out / 'runtime/worker-000/TRIM.exe')},
                {'ProcessId': 2, 'ExecutablePath': str(out / 'runtime-other/TRIM.exe')},
                {'ProcessId': 3, 'ExecutablePath': str(ROOT / 'TRIM.exe')},
            ]
            self.assertEqual([p['ProcessId'] for p in processes.runtime_processes(snapshots, out)], [1])


class RecoveryTests(unittest.TestCase):
    def make_stale(self, out):
        (out / '.simulation-lock').mkdir()
        study.core.atomic_write_json(out / '.simulation-lock/owner.json', {'pid': 1234})
        study.core.atomic_write_json(out / 'campaign_state.json', {'pid': 1234, 'status': 'running'})
        study.core.atomic_write_json(out / 'progress/worker-000.json', {'status': 'running', 'job': {'seed': 99}})
        study.core.atomic_write_json(out / 'batches/done.json', {'job': {'stage': 'production'}, 'seed': 99})

    def test_recovery_preserves_batches_and_archives_stale_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.make_stale(out)
            checkpoint = (out / 'batches/done.json').read_bytes()
            with mock.patch.object(processes, 'windows_processes', return_value=[]), \
                 mock.patch.object(os, 'kill', side_effect=AssertionError('must never terminate by PID')):
                result = campaign.recover(out)
            self.assertTrue(result['completed_batches_preserved'])
            self.assertFalse((out / '.simulation-lock').exists())
            self.assertEqual((out / 'batches/done.json').read_bytes(), checkpoint)
            self.assertEqual(campaign.read_json(out / 'progress/worker-000.json')['status'], 'interrupted')
            self.assertEqual(campaign.read_json(out / 'campaign_state.json')['status'], 'interrupted_or_process_missing')
            evidence = list((out / 'recovery').glob('*.json'))
            self.assertEqual(len(evidence), 1)
            self.assertEqual(campaign.read_json(evidence[0])['ownership'][0]['owner']['pid'], 1234)

    def test_recovery_refuses_live_campaign_or_owned_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.make_stale(out)
            for process in (
                {'ProcessId': 1234, 'CommandLine': f'python.exe "{campaign.DRIVER}" campaign --run-dir "{out}"'},
                {'ProcessId': 4567, 'ExecutablePath': str(out / 'runtime/worker-000/TRIM.exe')},
            ):
                with self.subTest(process=process), mock.patch.object(processes, 'windows_processes', return_value=[process]):
                    with self.assertRaisesRegex(RuntimeError, 'still active'):
                        campaign.recover(out)
                self.assertTrue((out / '.simulation-lock/owner.json').exists())

    def test_recovery_refuses_uninspectable_recorded_or_trim_process(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.make_stale(out)
            for process in ({'ProcessId': 1234, 'CommandLine': None, 'ExecutablePath': None},
                            {'ProcessId': 999, 'Name': 'TRIM.exe', 'CommandLine': None, 'ExecutablePath': None}):
                with self.subTest(process=process), mock.patch.object(processes, 'windows_processes', return_value=[process]):
                    with self.assertRaisesRegex(RuntimeError, 'Cannot'):
                        campaign.recover(out)
                self.assertTrue((out / '.simulation-lock/owner.json').exists())

    def test_legacy_live_lock_owner_with_relative_command_cannot_be_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.make_stale(out)
            legacy = {'ProcessId': 1234, 'ExecutablePath': sys.executable,
                      'CommandLine': r'python.exe scripts\cygno_study.py campaign --run-dir runs\study'}
            self.assertFalse(processes.campaign_command_matches(legacy['CommandLine'], campaign.DRIVER, out))
            original_owner = (out / '.simulation-lock/owner.json').read_bytes()
            with mock.patch.object(processes, 'windows_processes', return_value=[legacy]), \
                 mock.patch.object(os, 'kill', side_effect=AssertionError('must never terminate by PID')):
                with self.assertRaisesRegex(RuntimeError, 'Legacy lock owner.*creation-time identity'):
                    campaign.recover(out)
            self.assertEqual((out / '.simulation-lock/owner.json').read_bytes(), original_owner)
            self.assertFalse((out / 'recovery').exists())

    def test_reused_pid_of_unrelated_process_does_not_block_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.make_stale(out)
            owner = {'pid': 1234, 'process_identity': {'pid': 1234, 'creation_time': '1000', 'executable': r'C:\Python\python.exe'}}
            study.core.atomic_write_json(out / '.simulation-lock/owner.json', owner)
            unrelated = {'ProcessId': 1234, 'CreationTime': '2000', 'ExecutablePath': r'C:\Python\python.exe',
                         'CommandLine': 'python.exe unrelated.py'}
            with mock.patch.object(processes, 'windows_processes', return_value=[unrelated]), \
                 mock.patch.object(os, 'kill', side_effect=AssertionError('must never terminate by PID')):
                campaign.recover(out)
            self.assertFalse((out / '.simulation-lock').exists())


class NativeDialogTests(unittest.TestCase):
    def dialog(self, text='Calculation Complete (3 Ions)', title='End of TRIM.DAT calculation'):
        return {'handle': 100, 'title': title, 'children': [
            {'handle': 101, 'text': text, 'control_id': 10},
            {'handle': 102, 'text': '&OK', 'control_id': 1},
        ]}

    def test_only_exact_completion_and_history_count_can_be_acknowledged(self):
        button, count, _ = native.completion_acknowledgement(self.dialog(), 3)
        self.assertEqual((button['control_id'], count), (1, 3))
        for dialog in (self.dialog(title='TRIM error'), self.dialog(text='Calculation Complete (2 Ions)'),
                       self.dialog(text='Calculation Complete (unknown Ions)')):
            with self.subTest(dialog=dialog), self.assertRaises(RuntimeError):
                native.completion_acknowledgement(dialog, 3)
        dialog = self.dialog()
        dialog['children'].append({'handle': 103, 'text': 'OK', 'control_id': 2})
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            native.completion_acknowledgement(dialog, 3)

    @unittest.skipUnless(os.name == 'nt', 'native Windows supervisor')
    def test_verified_completion_is_acknowledged_once_and_count_is_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            process = mock.Mock(pid=1234, returncode=0)
            process.poll.side_effect = [None, None, 0]
            with mock.patch.object(native.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(native, 'process_windows', return_value=[self.dialog()]), \
                 mock.patch.object(native, 'acknowledge_completion') as acknowledge, \
                 mock.patch.object(native.time, 'sleep'):
                state = native.supervise_trim(out / 'TRIM.exe', out, 5, 3, out / 'process.json')
            self.assertEqual(acknowledge.call_count, 1)
            self.assertEqual(state['completed_dialog_ions'], 3)
            self.assertEqual(campaign.read_json(out / 'process.json')['status'], 'complete')
            process.terminate.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'native Windows supervisor')
    def test_explicit_interruption_stops_only_the_owned_process(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            process = mock.Mock(pid=1234, returncode=0)
            process.poll.return_value = None
            with mock.patch.object(native.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(native, 'process_windows', return_value=[]), \
                 mock.patch.object(os, 'kill', side_effect=AssertionError('must never terminate by PID')):
                with self.assertRaises(InterruptedError):
                    native.supervise_trim(out / 'TRIM.exe', out, 5, 3, out / 'process.json', stop_requested=lambda: True)
            process.terminate.assert_called_once_with()
            self.assertIn('interruption requested', campaign.read_json(out / 'process.json')['error'])

    @unittest.skipUnless(os.name == 'nt', 'native Windows supervisor')
    def test_unknown_dialog_fails_and_terminates_only_its_owned_popen_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            process = mock.Mock(pid=1234, returncode=0)
            process.poll.return_value = None
            with mock.patch.object(native.subprocess, 'Popen', return_value=process), \
                 mock.patch.object(native, 'process_windows', return_value=[self.dialog(title='TRIM error')]), \
                 mock.patch.object(native, 'acknowledge_completion') as acknowledge, \
                 mock.patch.object(os, 'kill', side_effect=AssertionError('must never terminate by PID')):
                with self.assertRaisesRegex(RuntimeError, 'Unexpected native TRIM dialog'):
                    native.supervise_trim(out / 'TRIM.exe', out, 5, 3, out / 'process.json')
            acknowledge.assert_not_called()
            process.terminate.assert_called_once_with()
            process.kill.assert_not_called()
            self.assertEqual(campaign.read_json(out / 'process.json')['status'], 'failed')


@unittest.skipUnless(os.name == 'nt', 'native Windows detached process test')
class DetachedLifecycleTests(unittest.TestCase):
    def wait_stopped(self, out):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if campaign.read_json(out / 'campaign_state.json').get('status') == 'stopped':
                return
            time.sleep(.05)
        self.fail('Harmless fixture coordinator did not stop')

    def test_detached_start_status_stop_resume_keeps_completed_checkpoint(self):
        # A Python-only fixture reproduces the coordinator protocol. The fake
        # executable files below are only validated for existence and never run.
        with tempfile.TemporaryDirectory(prefix='cygno-lifecycle-') as directory:
            root = Path(directory)
            source, out = root / 'source', root / 'study'
            for name in ('TRIM.exe', 'SR Module/SRModule.exe', 'Data/ATOMDATA'):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'not an executable; never launched')
            driver = root / 'fixture_coordinator.py'
            driver.write_text('''import argparse, json, os, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('mode'); p.add_argument('--config'); p.add_argument('--run-dir',type=Path); p.add_argument('--srim-root'); p.add_argument('--workers'); a=p.parse_args(); out=a.run_dir
def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True); temp=path.with_suffix('.tmp'); temp.write_text(json.dumps(value)); temp.replace(path)
lock=out/'.simulation-lock'; lock.mkdir(); write(lock/'owner.json',{'pid':os.getpid()})
checkpoint=out/'batches/done.json'
if not checkpoint.exists(): write(checkpoint,{'job':{'stage':'production'},'seed':123,'value':'completed once'})
write(out/'campaign_state.json',{'pid':os.getpid(),'coordinator_platform':'nt','status':'running'})
print('fixture coordinator running',flush=True)
deadline=time.monotonic()+20
while time.monotonic()<deadline and not (out/'.stop-requested').exists(): time.sleep(.05)
(lock/'owner.json').unlink(); lock.rmdir(); write(out/'campaign_state.json',{'pid':os.getpid(),'coordinator_platform':'nt','status':'stopped'})
''', encoding='utf-8')
            config = ROOT / 'configs/cygno_hecf4_900mbar.toml'
            with mock.patch.object(campaign, 'DRIVER', driver), mock.patch.object(study, '__file__', str(driver)):
                try:
                    launched = campaign.start(out, config, source, workers=1)
                    self.assertEqual(launched['command'][0], sys.executable)
                    self.assertNotIn('--defer-report', launched['command'])
                    self.assertEqual(launched['status'], 'running')
                    self.assertTrue(study.study_status(out)['campaign_process_active'])
                    checkpoint = (out / 'batches/done.json').read_bytes()
                    campaign.stop(out)
                    self.wait_stopped(out)
                    self.assertFalse((out / '.simulation-lock').exists())
                    resumed = campaign.start(out, resume=True)
                    self.assertNotEqual(launched['pid'], resumed['pid'])
                    self.assertEqual(resumed['workers'], 1)
                    self.assertEqual(Path(resumed['config']), config)
                    self.assertEqual((out / 'batches/done.json').read_bytes(), checkpoint)
                    campaign.stop(out)
                    self.wait_stopped(out)
                    self.assertTrue(Path(launched['stdout_log']).read_text().strip())
                    self.assertFalse(study.study_status(out)['campaign_process_active'])
                finally:
                    if out.exists():
                        (out / '.stop-requested').touch()
                        self.wait_stopped(out)


if __name__ == '__main__':
    unittest.main()
