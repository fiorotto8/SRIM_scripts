import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pysrim_runner


class TrimWatchdogTests(unittest.TestCase):
    def test_transient_window_race_is_retried(self):
        error = subprocess.CalledProcessError(1, ["xwininfo"], stderr="BadWindow (invalid Window parameter)")
        with patch('pysrim_runner._check_trim_dialogs', side_effect=[error, []]) as probe, \
             patch('pysrim_runner.time.sleep'):
            self.assertEqual(pysrim_runner.check_trim_dialogs(), [])
            self.assertEqual(probe.call_count, 2)

    def test_persistent_display_failure_fails_closed_with_diagnostics(self):
        error = subprocess.CalledProcessError(1, ["xwininfo"], stderr="unable to open display")
        with patch('pysrim_runner._check_trim_dialogs', side_effect=error) as probe, \
             patch('pysrim_runner.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'TRIM_WATCHDOG_ERROR.*unable to open display'):
                pysrim_runner.check_trim_dialogs()
            self.assertEqual(probe.call_count, 3)

    def test_normal_exit_does_not_probe_disappearing_windows(self):
        from unittest.mock import Mock
        process = Mock(pid=99999999)
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, DISPLAY=':test'), \
             patch('pysrim_runner.shutil.which', return_value='/usr/bin/xwininfo'), \
             patch('pysrim_runner.subprocess.Popen', return_value=process), \
             patch('pysrim_runner.check_trim_dialogs', return_value=[]) as probe:
            pysrim_runner.monitored_trim_run(Mock(), Path(directory), {'description': 'exit-test'})
            self.assertEqual(probe.call_count, 1)  # pre-launch only
            process.terminate.assert_not_called()

    def test_capacity_changes_only_allocation_and_preserves_crlf(self):
        original = b'colors\r\nMaximum number of secondary recoils (default=20000)\r\n 200000 \r\nNote: memory\r\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'Data').mkdir()
            cfg = root / 'Data' / 'TRIM.cfg'
            cfg.write_bytes(original)
            pysrim_runner.configure_recoil_capacity(root)
            self.assertEqual(cfg.read_bytes(), original.replace(b' 200000 ', b' 2000000 '))
            pysrim_runner.configure_recoil_capacity(root)
            self.assertEqual(cfg.read_bytes(), original.replace(b' 200000 ', b' 2000000 '))
            cfg.write_bytes(b'unknown config')
            with self.assertRaises(RuntimeError):
                pysrim_runner.configure_recoil_capacity(root)
            self.assertEqual(cfg.read_bytes(), b'unknown config')

    def test_dialog_detection_excludes_main_and_invisible_helpers(self):
        tree = '''
 0xa00001 "TRIM": ("trim.exe" "trim.exe") 1x1+512+384
 0xa00004 "SRIM-2013.00": ("trim.exe" "trim.exe") 987x746+0+0
 0xa00008 "Starting TRIM...": ("trim.exe" "trim.exe") 246x46+0+0
 0xa00009 "TRIM": ("trim.exe" "trim.exe") 288x258+368+268
'''
        self.assertEqual(pysrim_runner.dialog_candidates(tree), ['0xa00009'])

    def test_monitor_rejects_dialog_and_terminates_process(self):
        from unittest.mock import Mock
        process = Mock()
        process.pid = 99999999
        process.poll.return_value = None
        trim = Mock()
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with patch.dict(os.environ, DISPLAY=':test'), \
                 patch('pysrim_runner.shutil.which', return_value='/usr/bin/xwininfo'), \
                 patch('pysrim_runner.subprocess.Popen', return_value=process), \
                 patch('pysrim_runner.check_trim_dialogs', side_effect=[[], ['0xa00009']]), \
                 patch('PIL.ImageGrab.grab', side_effect=RuntimeError('test display')):
                with self.assertRaisesRegex(RuntimeError, 'TRIM_BLOCKING_DIALOG'):
                    pysrim_runner.monitored_trim_run(trim, runtime, {'description': 'test'})
            process.terminate.assert_called_once()
            self.assertEqual(json.loads((runtime / '.trim-progress.json').read_text())['status'], 'blocked_dialog')


ROOT = Path(__file__).resolve().parents[1]
PYSRIM_ROOT = Path(os.environ.get("PYSRIM_ROOT", ROOT.parent / "pysrim"))
SRIM_ROOT = Path(
    os.environ.get(
        "SRIM_ROOT",
        ROOT if (ROOT / "TRIM.exe").is_file() else ROOT.parent / "srim-2013",
    )
).resolve()


@unittest.skipUnless((PYSRIM_ROOT / "srim" / "__init__.py").is_file(), "pysrim checkout not available")
class PySRIMRunnerTests(unittest.TestCase):
    def environment(self):
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = str(PYSRIM_ROOT) + (os.pathsep + existing if existing else "")
        return environment

    def run_adapter(self, *arguments):
        return subprocess.run(
            [sys.executable, "-W", "ignore::SyntaxWarning", str(ROOT / "pysrim_runner.py"), *arguments],
            cwd=ROOT,
            env=self.environment(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

    def test_probe_handles_current_pyyaml(self):
        result = self.run_adapter("--probe")
        probe = json.loads(result.stdout)
        self.assertTrue(probe["yaml_loader_compatibility"])
        self.assertTrue(Path(probe["module_file"]).is_file())

    def test_write_only_preserves_scan_physics_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            shutil.copy2(SRIM_ROOT / "TRIM.exe", runtime / "TRIM.exe")
            (runtime / "SR Module").mkdir()
            spec = {
                "mode": "trim",
                "srim_root": str(runtime),
                "description": "test-job",
                "layer_name": "gas",
                "density_g_cm3": 0.004738334909447147,
                "elements": [
                    {"symbol": "C", "z": 6, "mass_u": 12.011, "stoich": 0.4,
                     "displacement_energy_ev": 21.0, "lattice_binding_energy_ev": 0.0,
                     "surface_binding_energy_ev": 0.0},
                    {"symbol": "F", "z": 9, "mass_u": 18.998, "stoich": 1.6,
                     "displacement_energy_ev": 21.0, "lattice_binding_energy_ev": 0.0,
                     "surface_binding_energy_ev": 0.0},
                    {"symbol": "Xe", "z": 54, "mass_u": 131.3, "stoich": 0.6,
                     "displacement_energy_ev": 21.0, "lattice_binding_energy_ev": 0.0,
                     "surface_binding_energy_ev": 0.0},
                ],
                "ion_symbol": "Xe",
                "ion_mass_u": 131.3,
                "energy_kev": 10.0,
                "n_ions": 10,
                "cascade_type": 2,
                "target_width_angstrom": 123456789.0,
                "compound_correction": 1.0,
                "angle_deg": 0.0,
                "random_seed": 12345,
                "save_3d_ranges": True,
            }
            spec_path = runtime / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            self.run_adapter("--spec", str(spec_path), "--write-only")
            trim_bytes = (runtime / "TRIM.IN").read_bytes()
            self.assertIn(b"2 12345 0\r\n", trim_bytes)
            self.assertIn(b"1 1 1 0 0 0\r\n", trim_bytes)
            self.assertIn(b"0  Target layer phases (0=Solid, 1=Gas)\r\n1\r\n", trim_bytes)
            self.assertIn(b"Individual target atom displacement energies (eV)\r\n 21.0 21.0 21.0", trim_bytes)
            layer_line = next(
                line for line in trim_bytes.decode("ascii").splitlines() if line.startswith('1 "gas"')
            )
            fractions = [float(value) for value in layer_line.split()[-3:]]
            self.assertAlmostEqual(sum(fractions), 1.0)
            self.assertAlmostEqual(fractions[0] / fractions[-1], 0.4 / 0.6)


if __name__ == "__main__":
    unittest.main()
