from pathlib import Path
from unittest.mock import patch
import os,tempfile,unittest
from powermill_com_helper.timing import Profiler,span,timed
from powermill_com_helper.core import Executor
from .test_executor import Fake,plan

class TimingTests(unittest.TestCase):
    def test_disabled_timer_does_not_read_clock_or_change_result(self):
        @timed('test')
        def f():return 42
        with patch('powermill_com_helper.timing.time.perf_counter',side_effect=AssertionError('Disabled')):self.assertEqual(f(),42)
    def test_nested_exclusive_times_and_exception_propagation(self):
        failure=RuntimeError('original')
        with Profiler() as p:
            with span('outer'):
                with span('inner'):pass
                try:
                    with span('failure'):raise failure
                except RuntimeError as exc:self.assertIs(exc,failure)
        out=p.snapshot();self.assertTrue(out['spans'][2]['failed']);self.assertEqual(out['spans'][1]['parent'],0)
        self.assertAlmostEqual(sum(s['self_seconds'] for s in out['spans']),out['spans'][0]['seconds'])
    def test_executor_calls_and_durable_write_count_are_identical(self):
        counts=[];calls=[]
        with tempfile.TemporaryDirectory() as tmp:
            for enabled in (False,True):
                fake=Fake();directory=Path(tmp)/str(enabled)
                with patch('powermill_com_helper.core.os.fsync',wraps=os.fsync) as sync:
                    if enabled:
                        with Profiler() as p:result=Executor(plan(two=True),directory,fake,lock_dir=Path(tmp)/'locks').run(allow_write=True)
                        self.assertEqual(p.snapshot()['totals']['io.fsync']['count'],sync.call_count)
                    else:result=Executor(plan(two=True),directory,fake,lock_dir=Path(tmp)/'locks').run(allow_write=True)
                    counts.append(sync.call_count);calls.append(fake.calls)
                self.assertTrue(all(x['status']=='completed' for x in result['operations'].values()))
        self.assertEqual(counts[0],counts[1]);self.assertEqual(calls,[['one','two'],['one','two']])
