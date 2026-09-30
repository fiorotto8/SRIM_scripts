from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import bulk_geometry_retry as r


class RetryTests(unittest.TestCase):
    def test_plan_preserves_physics_and_failed_run(self):
        if not (r.canary.RUN / 'result.json').exists(): self.skipTest('Local canary unavailable')
        original, original_inputs = r.original_prepare(r.ORIGINAL_RUN)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan, inputs = r.prepare(root)
            self.assertEqual(plan['cases'], original['cases'])
            self.assertEqual(inputs, original_inputs)
            self.assertEqual(plan['independent_production_histories_added'], 0)
            self.assertEqual(plan['stage'], 'C2b')
            self.assertIn(str(Path(r.completion.__file__)), plan['input_sha256'])
            self.assertEqual(r.prepare(root)[0], plan)
            (root / 'plan.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen'): r.prepare(root)

    def test_incomplete_canary_blocks_release(self):
        with patch.object(r, 'checked_result', side_effect=ValueError('Unexpected prerequisite state')):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaisesRegex(ValueError, 'prerequisite'): r.prepare(Path(td))

    def test_report_labels_retry_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(r, 'RUN', root), patch.object(r.bulk, 'RUN', root):
                r.publish({'status': 'TEST'}, [])
            text = (root / 'index.html').read_text()
            self.assertIn('C2b: completion-aware', text)
            self.assertIn('error dialogs are never dismissed', text)


if __name__ == '__main__': unittest.main()
