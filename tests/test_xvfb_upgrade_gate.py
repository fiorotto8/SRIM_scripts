import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import historical_execution as h
from scripts import xvfb_upgrade_gate as p
import srim_scan as scan


class UpgradeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not h.EVIDENCE.exists() or not h.HISTORICAL.exists():
            raise unittest.SkipTest('Versioned upgrade evidence unavailable')
        cls.directory = tempfile.TemporaryDirectory(); cls.root = Path(cls.directory.name)
        cls.plan, cls.inputs = p.prepare(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_recovered_historical_and_current_executables_are_distinct(self):
        self.assertEqual(h.actual_sha256(h.HISTORICAL), h.OLD_SHA)
        self.assertEqual(h.actual_sha256(h.LIVE), h.NEW_SHA)
        self.assertNotEqual(h.OLD_SHA, h.NEW_SHA)
        self.assertEqual(h.evidence()['current_version'], '2:21.1.12-1ubuntu1.8')

    def test_context_only_resolves_historical_binary_and_restores_on_error(self):
        original = scan.sha256_file
        with tempfile.TemporaryDirectory() as td:
            file = Path(td)/'unrelated'; file.write_bytes(b'unchanged independent hash')
            expected = original(file)
            with self.assertRaisesRegex(RuntimeError, 'intentional'):
                with h.historical_sources():
                    self.assertEqual(scan.sha256_file(h.LIVE), h.OLD_SHA)
                    self.assertEqual(scan.sha256_file(file), expected)
                    raise RuntimeError('intentional')
            self.assertIs(scan.sha256_file, original)

    def test_unknown_current_binary_fails_closed(self):
        original = h.actual_sha256
        def changed(path):
            return 'unknown-upgrade' if Path(path) == h.LIVE else original(path)
        with patch.object(h, 'actual_sha256', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Current Xvfb changed'):
                h.evidence()

    def test_wrong_historical_copy_fails_closed(self):
        original = h.actual_sha256
        def changed(path):
            return 'not-historical' if Path(path) == h.HISTORICAL else original(path)
        with patch.object(h, 'actual_sha256', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'hash-identical'):
                h.evidence()

    def test_upgrade_before_completion_is_not_accepted(self):
        record = copy.deepcopy(h.evidence()); record['upgrade_at'] = '2026-10-02T12:00:00+02:00'
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'evidence.json'; path.write_text(json.dumps(record))
            with patch.object(h, 'EVIDENCE', path):
                with self.assertRaisesRegex(ValueError, 'after the completed'):
                    h.evidence()

    def test_new_manifest_pins_current_bytes_even_inside_historical_context(self):
        with tempfile.TemporaryDirectory() as td, h.historical_sources():
            plan, _ = p.prepare(Path(td))
        self.assertEqual(plan['current_execution_sha256'][str(h.LIVE)], h.NEW_SHA)
        self.assertEqual(plan['current_execution_sha256'][str(h.HISTORICAL)], h.OLD_SHA)

    def test_replay_keeps_exact_source_input_and_excludes_production_counts(self):
        case = self.plan['replay_case']; reference = Path(case['replay_reference'])
        self.assertEqual(self.inputs[0], (reference/'TRIM.IN').read_bytes())
        self.assertEqual(self.inputs[1], (reference/'TRIM.DAT').read_bytes())
        self.assertEqual(case['family'], 'Kr'); self.assertEqual(case['ion'], 'F')
        self.assertEqual(case['energy_kev'], 1.); self.assertEqual(case['ions'], 50)
        self.assertTrue(case['diagnostic']); self.assertEqual(self.plan['new_independent_production_histories'], 0)
        self.assertEqual(self.plan['display_waves']*self.plan['displays_per_wave'], 60)
        self.assertEqual(self.plan['budget_seconds'], 300)

    def test_frozen_gate_plan_cannot_be_replaced(self):
        path = self.root/'plan.json'; original = path.read_text()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen'):
                p.prepare(self.root)
        finally:
            path.write_text(original)

    def test_completed_pressure_review_retains_inconclusive_outcomes(self):
        from scripts import review_pressure_batches as review
        summary, rows, points, timing, checks = review.review()
        self.assertEqual(len(rows), 1044); self.assertEqual(len(points), 261); self.assertEqual(len(timing), 261)
        self.assertEqual(summary['pressure_points_meeting_precision'], 18)
        self.assertEqual(summary['all_precision_flags'], 46)
        self.assertEqual(summary['pressure_classifications'], {'within_working_margin': 17, 'inconclusive': 19})
        self.assertEqual(len(checks), 36)
        self.assertIn('CURRENT_ENVIRONMENT_GATE_REQUIRED', summary['status'])


if __name__ == '__main__':
    unittest.main()
