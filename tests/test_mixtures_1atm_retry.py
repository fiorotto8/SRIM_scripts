import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import mixtures_1atm_retry as r


class MixtureRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (r.OLD_RUN/'status.json').exists():
            raise unittest.SkipTest('Original formatting-failure evidence unavailable')
        cls.directory = tempfile.TemporaryDirectory(); cls.root = Path(cls.directory.name)
        cls.plan, cls.inputs, cls.baseline = r.prepare(cls.root)
        cls.original = json.loads((r.OLD_RUN/'plan.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_frozen_cases_inputs_seeds_and_original_budget_are_unchanged(self):
        self.assertEqual(self.plan['cases'], self.original['cases'])
        self.assertEqual(len(self.plan['cases']), 1260)
        self.assertEqual(self.plan['new_cases_to_run'], 1250)
        self.assertEqual(len(self.plan['retained_original_cases']), 10)
        self.assertEqual(len(self.baseline), 1044)
        self.assertGreater(self.plan['inherited_used_seconds'], 40)
        self.assertEqual(self.plan['budget_hours'], 24)
        self.assertLess(self.plan['forecast']['planning_including_spent_hours'], 21)

    def test_both_exact_integer_headers_pass_all_raw_audits(self):
        for name in r.FAILED:
            row = json.loads((self.root/'cases'/name/'result.json').read_text())
            case = next(c for c in self.plan['cases'] if c['name'] == name)
            directory = Path(row['attempt']); outputs = {p.name.upper(): p for p in directory.iterdir()}
            with self.assertRaisesRegex(ValueError, 'atomic fraction'):
                r.original_composition(case, outputs)
            fresh = r.check_composition(case, outputs)
            fluorine = fresh['reported_composition']['F']
            self.assertEqual(fluorine['atomic_percent'], 50)
            self.assertEqual(fluorine['atomic_resolution_percent'], 1)
            self.assertEqual(fluorine['atomic_acceptance_margin_percent'], .1)
            with r.parser_context():
                r.p.audit_case(case, row, self.inputs[name])
            self.assertEqual(row['status'], 'COMPLETE'); self.assertEqual(row['ions_confirmed'], 50)
            self.assertEqual(row['endpoint_count'], 50); self.assertTrue(row['normal_completion_acknowledged'])
            self.assertEqual(json.loads((r.OLD_RUN/'cases'/name/'result.json').read_text())['status'], 'FAILED')

    def test_integer_format_does_not_permit_one_percentage_point_error(self):
        name = next(iter(r.FAILED)); row = json.loads((self.root/'cases'/name/'result.json').read_text())
        case = next(c for c in self.plan['cases'] if c['name'] == name)
        original = (Path(row['attempt'])/'TDATA.TXT').read_text()
        with tempfile.TemporaryDirectory() as td:
            file = Path(td)/'TDATA.TXT'; file.write_text(original.replace('F = 50 ', 'F = 51 '))
            with self.assertRaisesRegex(ValueError, 'atomic fraction'):
                r.check_composition(case, {'TDATA.TXT': file})
            file.write_text(original.replace('F = 50 ', 'F = 49.89 '))
            with self.assertRaisesRegex(ValueError, 'atomic fraction'):
                r.check_composition(case, {'TDATA.TXT': file})

    def test_eight_prior_successes_and_two_revalidated_cases_count_once(self):
        review = self.plan['failure_review']
        self.assertEqual(review['previously_complete'], 8)
        self.assertEqual(review['completed_raw_batches_revalidated'], 2)
        self.assertEqual(review['accepted_independent_batches'], 10)
        self.assertEqual(review['stopped_incomplete_cases'], 7)
        self.assertEqual(review['unlaunched_cases'], 1243)
        rows = [json.loads((self.root/'cases'/name/'result.json').read_text()) for name in self.plan['retained_original_cases']]
        self.assertEqual(len({x['seed'] for x in rows}), 10)
        self.assertTrue(all(x['composition_parser'] == r.PARSER for x in rows))
        self.assertTrue(all(x['phase'] == 'composition_gate' for x in rows))
        self.assertEqual(len(r.p.b.p.point_summary(self.baseline+rows)), 271)

    def test_interrupted_cases_are_not_promoted_or_silently_reused(self):
        original = json.loads((r.OLD_RUN/'results.json').read_text())
        stopped = {x['name'] for x in original if x['status'] == 'STOPPED'}
        self.assertFalse(stopped & set(self.plan['retained_original_cases']))
        self.assertTrue(stopped <= {c['name'] for c in self.plan['cases']})
        self.assertEqual(len(stopped), 7)

    def test_frozen_registry_has_same_predecessor_fingerprint_and_restores(self):
        original = r.p.known_seeds
        with r.frozen_seed_context():
            seeds, paths = r.p.known_seeds(self.baseline)
            self.assertEqual(len(seeds), self.original['forbidden_previous_seed_count'])
            self.assertNotIn(r.RUN/'plan.json', paths)
            self.assertFalse(seeds & {c['seed'] for c in self.plan['cases']})
        self.assertIs(r.p.known_seeds, original)

    def test_changed_attempt_hash_is_rejected(self):
        target = Path(json.loads((r.OLD_RUN/'cases'/next(iter(r.FAILED))/'result.json').read_text())['attempt'])/'TDATA.TXT'
        original = r.p.history.actual_sha256
        with patch.object(r.p.history, 'actual_sha256', side_effect=lambda path: 'tampered' if Path(path) == target else original(path)):
            with self.assertRaisesRegex(ValueError, 'raw artifact changed'):
                r.review_failed()

    def test_frozen_continuation_and_inherited_budget_cannot_change(self):
        file = self.root/'plan.json'; old = file.read_text()
        try:
            file.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen M3 continuation'):
                r.prepare(self.root)
        finally:
            file.write_text(old)
        budget = self.root/'budget.json'; old = budget.read_text()
        try:
            budget.write_text('{"used_seconds":0}')
            with self.assertRaisesRegex(ValueError, 'reset prior elapsed'):
                r.prepare(self.root)
        finally:
            budget.write_text(old)


if __name__ == '__main__':
    unittest.main()
