import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import mixtures_1atm_noreset as n


class NoResetContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (n.gate.RUN/'completion.json').exists():
            raise unittest.SkipTest('Completed C1e gate unavailable')
        cls.reviewed = n.review_gate()
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        with patch.object(n, 'review_gate', return_value=cls.reviewed):
            cls.plan, cls.inputs, cls.baseline = n.prepare(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_gate_passed_without_causal_overclaim(self):
        review = self.reviewed[-1]
        self.assertFalse(review['reset_hypothesis_reproduced'])
        self.assertTrue(review['exact_reference_replay_passed'])
        self.assertTrue(review['failed_input_replay_passed'])
        self.assertEqual(review['diagnostic_histories_excluded'], 100)

    def test_frozen_cohort_and_seeds_unchanged(self):
        original = json.loads((n.OLD_RUN/'plan.json').read_text())
        self.assertEqual(self.plan['cases'], original['cases'])
        self.assertEqual(len(self.plan['cases']), 1260)
        self.assertEqual(len({c['seed'] for c in self.plan['cases']}), 1260)
        self.assertEqual(self.plan['new_cases_to_run'], 859)
        self.assertEqual(len(self.plan['retained_original_cases']), 401)
        self.assertEqual(len(self.baseline), 1044)

    def test_diagnostic_failure_replay_not_promoted(self):
        self.assertNotIn(n.gate.FAILED, self.plan['retained_original_cases'])
        self.assertFalse((self.root/'cases'/n.gate.FAILED/'result.json').exists())
        self.assertTrue(all(not c['diagnostic'] for c in self.plan['cases']))
        row = json.loads((self.root/'cases'/n.gate.REFERENCE/'result.json').read_text())
        original = json.loads((n.OLD_RUN/'cases'/n.gate.REFERENCE/'result.json').read_text())
        self.assertEqual(row['attempt'], original['attempt'])
        self.assertNotIn(str(n.gate.RUN), row['attempt'])

    def test_retained_raw_audit_and_tamper_rejection(self):
        name = n.gate.REFERENCE
        case = next(c for c in self.plan['cases'] if c['name'] == name)
        row = json.loads((self.root/'cases'/name/'result.json').read_text())
        with n.r.parser_context():
            n.audit_case(case, row, self.inputs[name])
            row['retained_from'] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'Retained row'):
                n.audit_case(case, row, self.inputs[name])

    def test_new_batch_requires_new_launcher_and_owned_attempt(self):
        case = next(c for c in self.plan['cases'] if c['name'] == n.gate.FAILED)
        row = dict(attempt=str(n.RUN/'cases'/case['name']/'attempt-test'),
                   display_startup=dict(reset_on_last_client=False, helper=str(Path(n.gate.fixed_display.__file__))))
        with patch.object(n, 'original_audit'):
            n.audit_case(case, row, self.inputs[case['name']])
            row['display_startup']['reset_on_last_client'] = True
            with self.assertRaisesRegex(ValueError, 'noreset display evidence'):
                n.audit_case(case, row, self.inputs[case['name']])
            row['display_startup']['reset_on_last_client'] = False
            row['attempt'] = str(n.gate.RUN/'cases'/case['name']/'attempt-test')
            with self.assertRaisesRegex(ValueError, 'noreset display evidence'):
                n.audit_case(case, row, self.inputs[case['name']])

    def test_production_plus_diagnostic_budget_and_no_reset(self):
        review = self.reviewed[-1]
        self.assertAlmostEqual(self.plan['inherited_used_seconds'], 30285.081173705+388.3468270380108)
        self.assertEqual(self.plan['budget_hours'], 24)
        self.assertLess(self.plan['forecast']['planning_including_spent_hours']+3.025, 24)
        with tempfile.TemporaryDirectory() as td, patch.object(n, 'review_gate', return_value=self.reviewed):
            root = Path(td); n.prepare(root)
            (root/'budget.json').write_text(json.dumps(dict(used_seconds=review['inherited_production_seconds'], limit_seconds=86400)))
            with self.assertRaisesRegex(ValueError, 'budget reset'):
                n.prepare(root)

    def test_frozen_plan_change_rejected(self):
        with tempfile.TemporaryDirectory() as td, patch.object(n, 'review_gate', return_value=self.reviewed):
            root = Path(td); plan, *_ = n.prepare(root)
            plan['cases'][0]['seed'] += 1
            (root/'plan.json').write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, 'Frozen M3R2'):
                n.prepare(root)

    def test_historical_context_restored_after_error(self):
        sentinel = object()
        with patch.object(n.p, 'audit_case', sentinel):
            with self.assertRaisesRegex(RuntimeError, 'test'):
                with n.historical_audits():
                    self.assertIs(n.p.audit_case, n.original_audit)
                    raise RuntimeError('test')
            self.assertIs(n.p.audit_case, sentinel)

    def test_prepare_under_live_supervisor_bindings(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(n.p, RUN=n.RUN, UNIT=n.UNIT, prepare=n.prepare, publish=n.publish,
                                audit_case=n.audit_case, check_composition=n.r.check_composition), \
                    patch.object(n.p.b, 'RUN', n.RUN), patch.object(n.p.b.base, 'RUN', n.RUN), \
                    patch.object(n.p.scan, 'launch_command', n.gate.launch):
                plan, inputs, baseline = n.prepare(Path(td))
                self.assertEqual(plan, self.plan)
                self.assertEqual(len(baseline), 1044)
                self.assertEqual(inputs, self.inputs)


if __name__ == '__main__':
    unittest.main()
