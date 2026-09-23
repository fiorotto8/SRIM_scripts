import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from scripts import repro_check as replay


class ReplayTests(unittest.TestCase):
    def test_binding_report_parses_all_elements(self):
        text='Target energies for target atom = C\r\n Displacement = 21 eV, Binding = 0 eV, Surface = 0 eV\r\nTarget energies for target atom = F\r\n Displacement = 21 eV, Binding = 2 eV, Surface = 4 eV'
        values=replay.binding_report(text)
        self.assertEqual(values['C']['lattice'],0)
        self.assertEqual(values['F']['surface'],4)
        self.assertEqual(replay.binding_report('incomplete'),{})

    def test_exact_replay_does_not_certify_binding_parameters(self):
        row=dict(job_id='test',noble_percent=80,random_seed=123,raw_archive='raw.tar.gz',
                 projected_range_mm=.1,qf_trim_ionization=.2,longitudinal_straggling_mm=.03)
        with patch.object(replay,'archive_data',return_value=b'unchanged output without binding confirmation'):
            result=replay.compare(Path('/old'),Path('/new'),row,row)
        self.assertTrue(result['exact_reproduction'])
        self.assertFalse(result['configured_zero_bindings_confirmed'])

    def test_changed_scientific_output_fails_replay(self):
        row=dict(job_id='test',noble_percent=80,random_seed=123,raw_archive='raw.tar.gz',
                 projected_range_mm=.1,qf_trim_ionization=.2,longitudinal_straggling_mm=.03)
        new={**row,'projected_range_mm':.12}
        with patch.object(replay,'archive_data',return_value=b'file'):
            self.assertFalse(replay.compare(Path('/old'),Path('/new'),row,new)['exact_reproduction'])

    def test_empty_selection_never_launches_full_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=replay.ReplayCampaign(Path(directory))
            with patch.object(worker,'phase') as phase:
                with self.assertRaises(RuntimeError):worker.selected('empty',Path('config'),Path(directory),'trim',[])
                phase.assert_not_called()
            self.assertEqual(worker.budget_limit,3600)
