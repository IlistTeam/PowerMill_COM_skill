from pathlib import Path
import copy,hashlib,json,tempfile,unittest
from powermill_com_helper.core import Executor,RunStore,HelperError,NeedsReconciliation,NotReady,StateDrift,SessionLocked,file_lease,root_context
from powermill_com_helper.transport import TransportTimeout,PowerMillTransport

GUARD={'version':2023000,'process_id':1234,'model_ids':['MODEL'],'project_path':''}
def check(value):return {'expression':'result','expected':value}
def plan(two=False):
    ops=[{'id':'one','command':'CREATE FOLDER "Toolpath" "DEMO"','timeout_s':1,'prechecks':[check('before')],'postchecks':[check('one')]}]
    if two:ops.append({'id':'two','command':'EDIT FOLDER "Toolpath\\DEMO"','timeout_s':1,'prechecks':[check('one')],'postchecks':[check('two')]})
    return {'schema':1,'guard':copy.deepcopy(GUARD),'operations':ops}

class Fake:
    def __init__(self):self.value='before';self.calls=[];self.mode='ok';self.busy=False;self.root=True;self.guard=copy.deepcopy(GUARD)
    def probe(self,guard):return {**self.guard,'busy':self.busy,'root_ready':self.root}
    def read_xml(self,expressions,guard):return ['<string>'+self.value+'</string>' for _ in expressions]
    def execute(self,op,guard):
        self.calls.append(op['id'])
        if self.mode=='reject':return {'dispatched':False,'error':'busy before command'}
        if self.mode in ('ok','timeout_applied','code0_error'):self.value=op['id']
        if self.mode.startswith('timeout'):raise TransportTimeout('simulated deadline')
        if self.mode=='crash':raise SystemExit('simulated client interruption')
        return {'dispatched':True,'code':0,'output':'Ошибка: bad context' if self.mode=='code0_error' else '', 'seconds':.01}

class ExecutorTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.fake=Fake()
    def tearDown(self):self.tmp.cleanup()
    def engine(self,p=None,fake=None):return Executor(p or plan(),self.root/'run',fake or self.fake,lock_dir=self.root/'locks')
    def test_offline_default_never_dispatches(self):
        with self.assertRaises(HelperError):self.engine().run()
        self.assertEqual(self.fake.calls,[])
        with self.assertRaises(HelperError):PowerMillTransport()
    def test_success_and_completed_resume_does_not_replay(self):
        self.engine().run(allow_write=True);self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,['one'])
    def test_busy_or_nonroot_stops_before_write(self):
        for key in ('busy','root'):
            setattr(self.fake,key,key=='busy')
            with self.assertRaises(NotReady):self.engine().run(allow_write=True)
            setattr(self.fake,key,key=='root')
        self.assertEqual(self.fake.calls,[])
    def test_project_swap_rejected(self):
        self.fake.guard['model_ids']=['OTHER']
        with self.assertRaises(NotReady):self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,[])
    def test_timeout_applied_reconciles_and_continues_without_replay(self):
        p=plan(two=True);self.fake.mode='timeout_applied'
        with self.assertRaises(TransportTimeout):self.engine(p).run(allow_write=True)
        self.assertEqual(self.engine(p).store.load()['operations']['one']['status'],'uncertain')
        self.fake.mode='ok';self.engine(p).run(allow_write=True)
        self.assertEqual(self.fake.calls,['one','two'])
    def test_timeout_before_effect_is_not_replayed_just_because_prestate_matches(self):
        self.fake.mode='timeout_not_applied'
        with self.assertRaises(TransportTimeout):self.engine().run(allow_write=True)
        self.fake.mode='ok'
        with self.assertRaises(NeedsReconciliation):self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,['one'])
    def test_native_code_zero_with_error_halts(self):
        self.fake.mode='code0_error'
        with self.assertRaises(HelperError):self.engine().run(allow_write=True)
        self.assertEqual(self.engine().store.load()['operations']['one']['status'],'uncertain')
    def test_failed_verification_retains_native_time_and_failure_phase(self):
        p=plan();p['operations'][0]['postchecks']=[check('wrong')]
        with self.assertRaises(HelperError):self.engine(p).run(allow_write=True)
        state=self.engine(p).store.load()['operations']['one']
        self.assertEqual(state['native_seconds'],.01);self.assertEqual(state['failure_phase'],'postchecks');self.assertGreaterEqual(state['operation_seconds'],0)
        events=[json.loads(l) for l in (self.root/'run/events.jsonl').read_text().splitlines()]
        self.assertTrue(any(e['event']=='native_returned' and e['native_seconds']==.01 for e in events))
    def test_timeout_duration_is_not_reported_as_known_native_time(self):
        self.fake.mode='timeout_not_applied'
        with self.assertRaises(TransportTimeout):self.engine().run(allow_write=True)
        state=self.engine().store.load()['operations']['one']
        self.assertIsNone(state['native_seconds']);self.assertEqual(state['failure_phase'],'native');self.assertGreaterEqual(state['operation_seconds'],0)
    def test_client_interruption_survives_restart(self):
        self.fake.mode='crash'
        with self.assertRaises(SystemExit):self.engine().run(allow_write=True)
        self.fake.mode='ok'
        with self.assertRaises(NeedsReconciliation):self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,['one'])
    def test_latest_checkpoint_drift_blocks_new_steps(self):
        self.engine().run(allow_write=True);self.fake.value='externally changed'
        with self.assertRaises(StateDrift):self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,['one'])
    def test_explicit_before_dispatch_rejection_is_retryable(self):
        self.fake.mode='reject'
        with self.assertRaises(NotReady):self.engine().run(allow_write=True)
        self.assertEqual(self.engine().store.load()['operations']['one']['status'],'pending')
        self.fake.mode='ok';self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,['one','one'])
    def test_changed_plan_is_rejected(self):
        self.engine().run(allow_write=True);p=plan();p['operations'][0]['command']='OTHER'
        with self.assertRaises(StateDrift):self.engine(p).run(allow_write=True)
    def test_checkpoint_corruption_detected(self):
        self.engine().run(allow_write=True);f=self.root/'run'/'state.json';data=json.loads(f.read_text());data['operations']['one']['status']='pending';f.write_text(json.dumps(data))
        with self.assertRaises(StateDrift):self.engine().run(allow_write=True)
    def test_caller_cannot_mutate_validated_plan(self):
        p=plan();e=self.engine(p);p['operations'][0]['command']='OTHER';e.run(allow_write=True)
        self.assertNotEqual(e.plan['operations'][0]['command'],'OTHER')
    def test_session_lease_prevents_concurrent_dispatch(self):
        with file_lease(self.root/'locks'/'pid-1234.lock'):
            with self.assertRaises(SessionLocked):self.engine().run(allow_write=True)
        self.assertEqual(self.fake.calls,[])
    def test_known_bad_command_is_rejected_offline(self):
        p=plan();p['operations'][0]['command']='PROCESS TOOLPATH LEADS'
        with self.assertRaises(HelperError):self.engine(p)
    def test_readonly_checks_reject_mutating_functions(self):
        from powermill_com_helper.core import validate_read_expression
        for expression in ['point_add_parameter(1,2)', 'segment_create_point_at_distance(1,2)', 'reset_toolpath_safety_status(1)', 'select_components(1)', 'deselect_components(1)', 'value=4', 'project_pathname(0);evil()']:
            with self.subTest(expression=expression):
                with self.assertRaises(HelperError):validate_read_expression(expression)
        expression="size(folder('Toolpath\\P1 (original)'))"
        self.assertEqual(validate_read_expression(expression),expression)
        expression="components(model_components_thicknessset(entity('toolpath','P1_PODDERZHKA'),'surfaces',1))"
        self.assertEqual(validate_read_expression(expression),expression)
    def test_changed_macro_is_blocked_before_dispatch(self):
        file=self.root/'test.mac';file.write_bytes(b'original')
        p=plan();p['operations'][0].update(command='MACRO "'+str(file)+'"',artifact={'path':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()},completion_marker='COMPLETE')
        e=self.engine(p);file.write_bytes(b'changed')
        with self.assertRaises(StateDrift):e.run(allow_write=True)
        self.assertEqual(self.fake.calls,[])
    def test_missing_macro_completion_marker_is_uncertain(self):
        file=self.root/'test.mac';file.write_bytes(b'original')
        p=plan();p['operations'][0].update(command='MACRO "'+str(file)+'"',artifact={'path':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()},completion_marker='COMPLETE')
        with self.assertRaises(HelperError):self.engine(p).run(allow_write=True)
        self.assertEqual(self.engine(p).store.load()['operations']['one']['status'],'uncertain')
    def test_changed_nested_macro_is_blocked_before_dispatch(self):
        dependency=self.root/'selection.mac';dependency.write_bytes(b'recorded')
        p=plan();p['operations'][0]['dependencies']=[{'path':str(dependency),'sha256':hashlib.sha256(dependency.read_bytes()).hexdigest()}]
        e=self.engine(p);dependency.write_bytes(b'changed')
        with self.assertRaises(StateDrift):e.run(allow_write=True)
        self.assertEqual(self.fake.calls,[])
    def test_identity_receipt_blocks_replaced_entity_with_same_name(self):
        f=self.root/'identity.txt';f.write_text('one',encoding='utf-8')
        p=plan();p['operations'][0]['postchecks'].append({'kind':'identity','path':str(f),'expression':'result'})
        e=self.engine(p);e.run(allow_write=True)
        self.fake.value='same name, different ID'
        with self.assertRaises(StateDrift):e.run(allow_write=True)
    def test_xml_postconditions_are_batched_without_dropping_checks(self):
        fake=self.fake;calls=[];original=fake.read_xml
        def read(expressions,guard):calls.append(expressions);return original(expressions,guard)
        fake.read_xml=read;p=plan();p['operations'][0]['postchecks']=[check('one') for _ in range(22)]
        self.engine(p).run(allow_write=True)
        self.assertEqual([len(x) for x in calls],[1,22])
    def test_truncated_batch_cannot_pass_postconditions(self):
        original=self.fake.read_xml
        self.fake.read_xml=lambda expressions,guard:original(expressions,guard)[:-1] if len(expressions)>1 else original(expressions,guard)
        p=plan();p['operations'][0]['postchecks']=[check('one'),check('one')]
        with self.assertRaises(HelperError):self.engine(p).run(allow_write=True)
        self.assertEqual(self.engine(p).store.load()['operations']['one']['status'],'uncertain')
    def test_batched_failing_last_check_blocks_next_command(self):
        p=plan(two=True);p['operations'][0]['postchecks']=[check('one'),check('wrong')]
        with self.assertRaises(HelperError):self.engine(p).run(allow_write=True)
        self.assertEqual(self.fake.calls,['one'])
    def test_root_parser_classification(self):
        self.assertTrue(root_context('Commands available :\nACTIVATE\nCREATE\nDEACTIVATE'))
        self.assertTrue(root_context('Commands available :\r\r\n\tACTIVATE\r\r\n\tCREATE\t"Создать объекты"\r\r\n\tDEACTIVATE'))
        self.assertFalse(root_context('NEW\nSERIES\nTOGGLE'))
        self.assertFalse(root_context('Commands available :\nACTIVATE_TOOLPATH\nCREATE\nDEACTIVATE'))
        self.assertFalse(root_context('Commands available after command [X] :\nACTIVATE\nCREATE\nDEACTIVATE'))
    def test_archived_real_help_root_and_waiting_selection(self):
        fixtures=Path(__file__).with_name('fixtures')
        self.assertTrue(root_context((fixtures/'help-root.txt').read_text(encoding='utf-8')))
        self.assertFalse(root_context((fixtures/'help-style.txt').read_text(encoding='utf-8')))

if __name__=='__main__':unittest.main()
