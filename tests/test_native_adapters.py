"""Portability and preservation contracts for the original CSV entry points."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import srim_gas_sweep as stopping
import trim_qf_sweep as trim


ROOT = Path(__file__).resolve().parents[1]


class NativeAdapterTests(unittest.TestCase):
    def test_inputs_have_exact_dos_line_endings(self):
        elements, _ = stopping.load_elements(ROOT / 'Data/ATOMDATA')
        gas = stopping.load_gases(ROOT / 'examples/GasDensities_minimal.csv', elements)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stopping.write_sr_in(root / 'SR.IN', 'output.txt', elements['C'], gas, elements, .1, 10., 1.)
            trim.write_trim_in(root / 'TRIM.IN', gas, elements['C'], elements, 10., 3, 2, 1e8, gas.density_g_cm3, 1.)
            trim.write_trim_auto(root / 'TRIMAUTO')
            for path in root.iterdir():
                data = path.read_bytes()
                self.assertTrue(data.endswith(b'\r\n'))
                self.assertNotIn(b'\r\r\n', data)
                self.assertNotIn(b'\n', data.replace(b'\r\n', b''))

    def test_dry_runs_preserve_existing_outputs_and_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / 'existing.csv'
            output.write_bytes(b'preserve results')
            work = root / 'work'
            work.mkdir()
            sentinel = work / 'checkpoint'
            sentinel.write_bytes(b'preserve checkpoint')
            common = ['--srim-root', str(ROOT), '--gas-csv', str(ROOT / 'examples/GasDensities_minimal.csv'),
                      '--ions', 'C', '--dry-run', '--no-plots', '--out', str(output), '--workdir', str(work)]
            for module, extra in [(stopping, ['--points', '3']), (trim, ['--energies-kev', '10'])]:
                with patch('sys.argv', [module.__file__, *common, *extra]), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(), 0)
                self.assertEqual(output.read_bytes(), b'preserve results')
                self.assertEqual(sentinel.read_bytes(), b'preserve checkpoint')
                self.assertEqual(list(root.iterdir()), [output, work])

    def test_nonempty_work_is_never_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / 'checkpoint'
            sentinel.write_bytes(b'saved')
            with self.assertRaises(FileExistsError):
                stopping.prepare_raw_workdir(root)
            self.assertEqual(sentinel.read_bytes(), b'saved')

    def test_locked_output_cleanup_never_terminates_processes(self):
        with patch.object(Path, 'unlink', side_effect=PermissionError('locked')), \
                patch.object(stopping.subprocess, 'run') as command:
            with self.assertRaises(PermissionError):
                stopping.unlink_with_retries(Path('locked.txt'), attempts=2, delay_s=0)
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
