from unittest.mock import patch
import copy,json
from . import test_simulation as fixtures
from powermill_com_helper.simulation import Kinematics,SimulationAudit,sha256_file,verify_native_issues
from powermill_com_helper.core import HelperError,StateDrift

class MotionRegionTests(fixtures.SimulationTests):
    def setUp(self):
        super().setUp()
        self.c['motion_regions']=[{'name':'deposition','movetypes':['12'],'min':[.9,-.1],'max':[1.1,.1]},
                                  {'name':'transition','movetypes':['1'],'min':[.9,-.1],'max':[1.1,.1]}]
    def test_same_pose_allowed_as_transition_rejected_as_deposition(self):
        self.c['motion_regions'][0]['max'][0]=.99
        self.write(self.rows[:1],end=False);a=self.audit();a.poll()
        self.assertEqual(a.state['motion_region_counts'],{'transition':1})
        with self.native.open('a') as f:f.write(self.rows[1])
        with self.assertRaisesRegex(HelperError,'deposition'):a.poll()
        self.assertEqual(json.loads(self.cp.read_text())['records'],1)
    def test_only_new_records_compute_FK_and_checkpoint_resumes_motion(self):
        self.write(self.rows[:2],end=False);a=self.audit()
        with patch.object(a.kin,'verify',wraps=a.kin.verify) as verify:
            a.poll();a.poll();self.assertEqual(verify.call_count,2)
        b=self.audit()
        with self.native.open('a') as f:f.write(''.join(self.rows[2:])+'(End)\n')
        with patch.object(b.kin,'verify',wraps=b.kin.verify) as verify:
            report=b.poll(complete=True);self.assertEqual(verify.call_count,4)
        self.assertEqual(report['motion_region_counts'],{'transition':1,'deposition':5})
        self.assertEqual(sum(report['motion_region_counts'].values()),report['records'])
    def test_unknown_matching_source_motion_is_rejected(self):
        self.rows[0]=self.rows[0].replace('MOVETYPE=1 ','MOVETYPE=99 ')
        self.source.write_text(self.header+''.join(self.rows)+'(End)\n');self.sha=sha256_file(self.source);self.write()
        with self.assertRaisesRegex(HelperError,'Unclassified'):self.audit().poll()
    def test_duplicate_classification_and_expansion_rejected(self):
        for change in ('duplicate','expand','missing'):
            c=copy.deepcopy(self.c)
            if change=='duplicate':c['motion_regions'][1]['movetypes'].append('12')
            elif change=='expand':c['motion_regions'][0]['max'][0]=2
            else:c['motion_regions'][0]['movetypes']=[]
            with self.subTest(change=change),self.assertRaises(HelperError):Kinematics(c)
    def test_motion_policy_change_cannot_resume_old_checkpoint(self):
        self.write(self.rows[:2],end=False);self.audit().poll();self.c['motion_regions'][0]['max'][0]=1.05
        with self.assertRaises(StateDrift):self.audit()
    def test_native_issues_fail_closed_and_warnings_are_reported(self):
        class Fake:
            def read_xml(self,expressions,guard):return [self.raw]
        f=Fake();warning='<map><Severity>warning</Severity><Type>reconfigure</Type><Ignored>0</Ignored></map>'
        f.raw='<array>'+warning+'</array>';r=verify_native_issues(f,{})
        self.assertEqual(r['count'],1);self.assertEqual(r['types'],{'reconfigure':1})
        for raw in ['<string/>','<array>bad</array>','<array><map/></array>','<array>'+warning.replace('reconfigure','collision')+'</array>','<array>'+warning.replace('warning','error')+'</array>']:
            f.raw=raw
            with self.subTest(raw=raw),self.assertRaises(HelperError):verify_native_issues(f,{})
