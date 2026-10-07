import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock,patch
from scripts import mixture_pressure_65_ten as g


def sample(temp=96,count=1):
    return dict(time='now',temperatures_c={'cpu':temp},critical_c={'cpu':105},
                reported_high_c={'cpu':85},throttle_counts={'cpu':count},software_temperature_cutoff_c=None)


class TenWorkerTests(unittest.TestCase):
    def test_no_startup_or_runtime_temperature_cutoff(self):
        for value in (75,95,100,105,110):
            g.check(sample(value,2),sample(60))
            g.check(sample(value),sample(value))

    def test_telemetry_changes_and_counter_reset_still_fail(self):
        changed=sample(); changed['critical_c']['cpu']=110
        missing=sample(); missing['temperatures_c']={}
        for current in (changed,missing,sample(count=0)):
            with self.assertRaises(RuntimeError): g.check(current,sample())
        with self.assertRaises(RuntimeError): g.check(sample(count=2),sample(),sample(count=3))

    def test_forecast_uses_ten_workers(self):
        jobs=[dict(estimated_seconds=3600) for _ in range(20)]
        self.assertEqual(g.forecast(jobs)['model_hours'],2)

    def test_unbounded_launcher_refused(self):
        with patch.object(g.r,'resource_sample',return_value={'cgroup':'/tmp/wrong'}):
            with self.assertRaises(RuntimeError): g.guard()

    def test_guard_continues_above_old_cutoff_and_records_correct_policy(self):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp); group=root/g.UNIT; group.mkdir()
            for name,text in [('cpu.max',g.c.CPU_MAX),('memory.max',str(32*g.r.GIB)),('memory.swap.max','0')]:
                (group/name).write_text(text)
            g.c.write_json(root/'plan.json',dict(inherited_used_seconds=100))
            g.c.write_json(root/'budget.json',dict(used_seconds=100,limit_seconds=g.r.BUDGET))
            complete=dict(status='REVIEW_REQUIRED')
            stack.enter_context(patch.object(g,'RUN',root))
            stack.enter_context(patch.object(g.r,'resource_sample',return_value={'cgroup':str(group)}))
            stack.enter_context(patch.object(g.os,'sched_getaffinity',return_value=set(range(10))))
            stack.enter_context(patch.dict(os.environ,g.thermal.THREAD_ENV))
            stack.enter_context(patch.object(g.signal,'signal'))
            stack.enter_context(patch.object(g,'snapshot',side_effect=[sample(96),sample(105,2)]))
            proc=Mock();proc.poll.return_value=0
            def start(*args,**kwargs):
                g.c.write_json(root/'status.json',complete); g.c.write_json(root/'completion.json',complete)
                return proc
            stack.enter_context(patch.object(g.subprocess,'Popen',side_effect=start))
            stack.enter_context(patch.object(g.subprocess,'check_output',return_value='test'))
            kill=stack.enter_context(patch.object(g.thermal,'kill_owned_workers'))
            self.assertEqual(g.guard(),0);kill.assert_not_called()
            state=g.c.read(root,'thermal-status.json')
            self.assertEqual(state['peak_temperature_c'],105)
            self.assertIsNone(state['software_temperature_cutoff_c'])
            self.assertEqual(state['worker_process_limit'],10)
            self.assertEqual(state['throttle_warning_samples'],1)

    def test_cold_plan_preserves_exact_inputs_and_full_budget(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(g,'RUN',Path(tmp)):
            plan,inputs,baseline,accepted=g.prepare()
            prior=g.c.read(g.PRIOR,'plan.json')
            self.assertEqual(plan['workers'],10)
            self.assertEqual(plan['cases'],prior['cases'])
            self.assertEqual(plan['pressure_screen'],prior['pressure_screen'])
            self.assertEqual((len(baseline),len(accepted)),(4536,251))
            self.assertEqual(plan['inherited_used_seconds'],g.c.read(g.PRIOR,'budget.json')['used_seconds'])
            self.assertIsNone(plan['thermal_policy']['software_temperature_cutoff_c'])
            self.assertEqual(plan['diagnostic_histories_scheduled'],0)
            self.assertLess(plan['inherited_used_seconds']/3600+plan['forecast']['planning_hours']+3.025,24)
            for case in plan['cases']:
                if case['name'] not in plan['retained_original_names']:
                    self.assertEqual(tuple((Path(tmp)/'inputs'/case['name']/n).read_bytes() for n in ('TRIM.IN','TRIM.DAT')),inputs[case['name']])
            g.r.old.gate.verify_hashes(plan['input_sha256'])


if __name__=='__main__':unittest.main()
