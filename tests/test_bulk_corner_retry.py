import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import bulk_corner_retry as r


class CornerRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (r.stress.RUN/'result.json').exists():raise unittest.SkipTest('Local stress test missing')
        cls.directory=tempfile.TemporaryDirectory();cls.root=Path(cls.directory.name)
        cls.plan,cls.inputs=r.prepare(cls.root)

    @classmethod
    def tearDownClass(cls):cls.directory.cleanup()

    def test_preserves_cases_seeds_inputs_and_retains_only_passed_canaries(self):
        old,inputs=r.old_prepare(r.OLD_RUN)
        self.assertEqual(self.plan['cases'],old['cases']);self.assertEqual(self.inputs,inputs)
        self.assertEqual(len(self.plan['retained_cases']),19);self.assertEqual(self.plan['new_cases_to_run'],18)
        self.assertEqual(self.plan['independent_production_histories_added'],0)
        with patch.object(r.base,'RUN',self.root):
            completed=[r.base.cached(c) for c in self.plan['cases']]
        self.assertEqual(sum(x is not None for x in completed),19)
        self.assertTrue(all(x['phase']=='canary' and x['reused_from'] for x in completed if x))

    def test_budget_is_carried_and_not_reset_on_prepare(self):
        inherited=self.plan['inherited_used_seconds'];self.assertGreater(inherited,300)
        path=self.root/'budget.json';original=path.read_text()
        try:
            path.write_text(json.dumps(dict(used_seconds=inherited+200,limit_seconds=r.base.BUDGET)))
            r.prepare(self.root)
            self.assertEqual(json.loads(path.read_text())['used_seconds'],inherited+200)
            path.write_text(json.dumps(dict(used_seconds=0,limit_seconds=r.base.BUDGET)))
            with self.assertRaisesRegex(ValueError,'budget'):r.prepare(self.root)
        finally:path.write_text(original)

    def test_only_known_launch_command_is_rewritten(self):
        cwd=Path('/isolated/worker')
        cmd=['xvfb-run','-a','-e',str(cwd/'.xvfb.log'),'-s','-screen 0 1024x768x24',
             r.sys.executable,str(Path(r.base.__file__).resolve()),'worker','--runtime',str(cwd),'--name','test']
        with patch.object(r,'old_launch') as launch:
            r.display_launch(cmd,cwd,100,environment={'test':'value'},process_group=True)
            args,kwargs=launch.call_args
            self.assertEqual(args[0][5:],cmd[6:]);self.assertEqual(args[1:],(cwd,100))
            self.assertEqual(kwargs,dict(environment={'test':'value'},process_group=True))
            with self.assertRaisesRegex(ValueError,'Unexpected'):r.display_launch(['unknown'],cwd,100)

    def test_frozen_retry_rejects_plan_change(self):
        path=self.root/'plan.json';original=path.read_text()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):r.prepare(self.root)
        finally:path.write_text(original)


if __name__=='__main__':unittest.main()
