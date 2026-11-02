"""Handoff regressions: all transports are fakes; never connect to PowerMill."""
from pathlib import Path
import copy,hashlib,json,tempfile,unittest
from .test_executor import GUARD,Fake,plan
from .test_planner import catalog,OrderFake,layer
from . import test_worker as worker
from powermill_com_helper.core import Executor,HelperError,NotReady,StateDrift,validate_plan,validate_read_expression
from powermill_com_helper.planner import validate_order_check,check_observed_order,create_bundle

class HandoffTests(unittest.TestCase):
    def test_supported_versions_require_exact_session_version(self):
        for version in (2023000,2023003,2023101):
            p=plan();p['guard']['version']=version;validate_plan(p)
            with tempfile.TemporaryDirectory() as d:
                fake=Fake();fake.guard['version']=version
                Executor(p,d,fake).run(allow_write=True)
                self.assertEqual(fake.calls,['one'])
            with tempfile.TemporaryDirectory() as d:
                fake=Fake();fake.guard['version']=2024000
                with self.assertRaises(NotReady):Executor(p,d,fake).run(allow_write=True)
                self.assertEqual(fake.calls,[])
        for version in (True,2023001,2024000,'2023000'):
            p=plan();p['guard']['version']=version
            with self.assertRaises(HelperError):validate_plan(p)

    def test_new_read_queries_do_not_admit_writes(self):
        for expression in ["get_folders('toolpath')","active_folder()","number_selected(folder('toolpath'))","component_has_point_parameter(entity('toolpath','T'),0,0,'udp_LaserPower')"]:
            self.assertEqual(validate_read_expression(expression),expression)
        for expression in ["point_add_parameter(entity('toolpath','T'),0,0,'x','1')","active_folder(); delete_model()","unknown_read()"]:
            with self.assertRaises(HelperError):validate_read_expression(expression)

    def test_geometry_identity_has_no_pp_dependency_and_persists(self):
        refs=catalog([0,1,2])
        for r in refs:r.pop('lp');r.pop('layer')
        class NoPP(OrderFake):
            def read_xml(self,expressions,guard):
                if any('point_parameters' in e for e in expressions):raise AssertionError('PP must not be read')
                return super().read_xml(expressions,guard)
        check={'kind':'order','name':'TEST','entity_id':'OWNED_ID','catalog':refs,'expected':[2,0,1],'identity_mode':'geometry'}
        fake=NoPP([2,0,1],refs);self.assertTrue(check_observed_order(check,fake,GUARD))
        fake.refs[0]['position'][0]+=.001
        with self.assertRaises(HelperError):check_observed_order(check,fake,GUARD)
        with tempfile.TemporaryDirectory() as d:
            create_bundle([{**layer([2,0,1],[0,1,2]),'identity_mode':'geometry'}],GUARD,Path(d)/'bundle')
            p=json.loads((Path(d)/'bundle/plan.json').read_text())
            self.assertEqual(p['operations'][0]['prechecks'][0]['identity_mode'],'geometry')
            self.assertIn('PP not read',p['operations'][0]['prechecks'][0]['coverage'])

    def test_geometry_identity_rejects_duplicate_xyz_even_with_different_pp(self):
        refs=catalog([0,1]);refs[1].update(position=[0,0,0],lp=81,layer=2)
        c={'name':'TEST','entity_id':'OWNED_ID','catalog':refs,'expected':[0,1],'identity_mode':'geometry'}
        with self.assertRaises(HelperError):validate_order_check(c)
        refs[1]['position']=[1,0,0];refs[0]['head_pp']={'udp_LaserPower':80}
        with self.assertRaises(HelperError):validate_order_check(c)
        c['identity_mode']='unknown'
        with self.assertRaises(HelperError):validate_order_check(c)

    def test_native_response_is_retained_even_when_postcheck_fails(self):
        p=plan();p['operations'][0]['postchecks'][0]['expected']='wrong'
        with tempfile.TemporaryDirectory() as d:
            fake=Fake()
            with self.assertRaises(HelperError):Executor(p,d,fake).run(allow_write=True)
            raw=json.loads(next(Path(d).glob('native-result-*.json')).read_text())
            self.assertEqual(raw['operation_id'],'one');self.assertEqual(raw['result']['code'],0);self.assertTrue(raw['result']['dispatched'])
    def test_native_response_filename_cannot_escape_run_directory(self):
        p=plan();p['operations'][0]['id']='../escape';p['operations'][0]['postchecks'][0]['expected']='../escape'
        with tempfile.TemporaryDirectory() as d:
            run=Path(d)/'run';Executor(p,run,Fake()).run(allow_write=True)
            native=list(run.glob('native-result-*.json'));self.assertEqual(len(native),1)
            self.assertEqual(json.loads(native[0].read_text())['operation_id'],'../escape')
            self.assertEqual(list(Path(d).glob('*.json')),[])

class ModelReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.receipt=self.root/'new-model.id';self.newid='a'*32
        self.p=plan();self.p['guard']['model_identity_receipts']=[{'name':'NEW_BASE','path':str(self.receipt)}]
        self.fake=Fake();original=self.fake.execute
        def execute(op,guard):
            result=original(op,guard);self.receipt.write_text(self.newid);self.fake.guard['model_ids'].append(self.newid);return result
        self.fake.execute=execute
        read=self.fake.read_xml
        self.fake.read_xml=lambda expressions,guard:['<string>'+self.newid+'</string>' if e=="entity('model','NEW_BASE').ID" else read([e],guard)[0] for e in expressions]
    def tearDown(self):self.tmp.cleanup()
    def engine(self):return Executor(self.p,self.root/'run',self.fake,lock_dir=self.root/'locks')
    def test_created_model_is_named_verified_pinned_and_resumes_without_replay(self):
        self.engine().run(allow_write=True);self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,['one'])
        pin=json.loads((self.root/'run/model-identities.json').read_text())
        self.assertEqual(next(iter(pin['identities'].values()))['id'],self.newid)
    def test_receipt_cannot_replace_created_model_on_resume(self):
        self.engine().run(allow_write=True);self.newid='b'*32;self.receipt.write_text(self.newid);self.fake.guard['model_ids'][-1]=self.newid
        with self.assertRaises(StateDrift):self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,['one'])
    def test_receipt_loss_and_original_model_loss_block_resume(self):
        self.engine().run(allow_write=True);self.receipt.unlink()
        with self.assertRaises(StateDrift):self.engine().run(allow_write=True)
        self.receipt.write_text(self.newid);self.fake.guard['model_ids'].remove('MODEL')
        with self.assertRaises(NotReady):self.engine().run(allow_write=True)
    def test_unreceipted_or_wrongly_named_model_is_not_authorized(self):
        self.fake.guard['model_ids'].append(self.newid)
        with self.assertRaises(NotReady):self.engine().run(allow_write=True)
        self.receipt.write_text(self.newid);self.fake.read_xml=lambda expressions,guard:['<string>'+('b'*32)+'</string>']
        with self.assertRaises(StateDrift):self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,[])
    def test_malformed_and_duplicate_receipts_are_rejected(self):
        for bad in [None,{},[{'name':"bad'name",'path':str(self.receipt)}],[{'name':'X','path':'relative.id'}],self.p['guard']['model_identity_receipts']*2]:
            p=copy.deepcopy(self.p);p['guard']['model_identity_receipts']=bad
            with self.assertRaises(HelperError):validate_plan(p)

@unittest.skipUnless(worker.os.name=='nt','Real PowerShell protocol uses a fake application')
class HandoffWorkerTests(unittest.TestCase):
    def test_worker_accepts_supported_versions_but_not_session_mismatch(self):
        for version in (2023000,2023003,2023101):
            channel=worker.WorkerProtocolTests().channel(fake_source=worker.FAKE.replace('Version=2023000','Version='+str(version)))
            guard={**GUARD,'version':version}
            try:
                self.assertTrue(channel.request('probe',{'guard':guard},10)['root_ready'])
                self.assertEqual(channel.request('read_xml',{'guard':guard,'expressions':["get_folders('toolpath')","active_folder()","number_selected(folder('toolpath'))","component_has_point_parameter(entity('toolpath','T'),0,0,'x')"]},10),['<string>before</string>']*4)
                bad={**guard,'version':2024000}
                result=channel.request('execute',{'guard':bad,'operation':{'command':'UNEXPECTED'}},10)
                self.assertFalse(result['dispatched'])
            finally:channel.close()
