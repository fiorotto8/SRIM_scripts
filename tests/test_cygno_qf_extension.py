"""Bounded extension planning; never launches a native executable."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cygno_qf_extension', ROOT/'scripts/cygno_qf_extension.py')
extension = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extension)


class ExtensionPlanTests(unittest.TestCase):
    def test_fixed_budget_and_independent_reproducible_seeds(self):
        cfg = extension.study.load_configuration(ROOT/'configs/cygno_hecf4_900mbar.toml')
        jobs = extension.extension_jobs({'points': []}, cfg)
        self.assertEqual(len(jobs), 45)
        self.assertEqual(sum(j['n_ions'] for j in jobs), 4500)
        self.assertEqual({j['energy_kevnr'] for j in jobs}, {.025, .04, .063})
        self.assertEqual({j['medium'] for j in jobs}, {'baseline'})
        self.assertEqual(len({j['seed'] for j in jobs}), 45)
        self.assertEqual(jobs, extension.extension_jobs({'points': []}, cfg))

    def test_existing_requested_point_is_not_resimulated(self):
        cfg = extension.study.load_configuration(ROOT/'configs/cygno_hecf4_900mbar.toml')
        jobs = extension.extension_jobs({'points': [{'species': 'H', 'energy_keV': .025}]}, cfg)
        self.assertEqual(len(jobs), 42)
        self.assertFalse(any(j['species']=='H' and j['energy_kevnr']==.025 for j in jobs))

    def test_completed_full_request_launches_no_additional_batches(self):
        cfg=extension.study.load_configuration(ROOT/'configs/cygno_hecf4_900mbar.toml')
        existing={'points':[dict(species=s,energy_keV=e) for s in cfg['study']['species'] for e in [.025,.04,.063]]}
        self.assertEqual(extension.extension_jobs(existing,cfg),[])


if __name__=='__main__':
    unittest.main()
