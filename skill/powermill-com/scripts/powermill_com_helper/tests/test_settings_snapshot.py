from pathlib import Path
from unittest.mock import patch
import copy,hashlib,tempfile,unittest,xml.etree.ElementTree as ET
from powermill_com_helper.core import Executor,HelperError,StateDrift
from powermill_com_helper.settings_snapshot import SettingsObservation,MACHINE,STATE,TOOL
from .test_executor import Fake,GUARD,plan

STATE_XML='''<simulationstate><Issues><Collisions><CheckCollisions>1</CheckCollisions><CheckToolAndHolder>1</CheckToolAndHolder></Collisions><CheckReconfigurations>1</CheckReconfigurations></Issues><ModelLocation><Name>DEMO</Name></ModelLocation><ToolControl><ToolAxisConstraint><Mode>unconstrained</Mode></ToolAxisConstraint><OrientationVector><Mode>fixed_vector</Mode><Space>machine</Space><AngularTolerance>5,0</AngularTolerance><ReferenceVector><real>1,0</real><real>0,0</real><real>0,0</real></ReferenceVector></OrientationVector></ToolControl></simulationstate>'''
MACHINE_XML='<map><Axis><ControlInformation><Address>A</Address><Min>-30,0</Min><Max>-15,0</Max><Priority>high_priority</Priority></ControlInformation></Axis></map>'
SCALARS={
 'simulationstate.ToolControl.OrientationVector.Mode':'fixed_vector',
 'simulationstate.ToolControl.OrientationVector.Space':'machine',
 'simulationstate.ToolControl.OrientationVector.AngularTolerance':'5,0',
 'simulationstate.ToolControl.OrientationVector.ReferenceVector[0]':'1,0',
 'simulationstate.ToolControl.OrientationVector.ReferenceVector[1]':'0,0',
 'simulationstate.ToolControl.OrientationVector.ReferenceVector[2]':'0,0',
 'simulationstate.ToolControl.ToolAxisConstraint.ReferenceVector[0]':'1',
 'simulationstate.ToolControl.ToolAxisConstraint.ReferenceVector[1]':'0',
 'simulationstate.ToolControl.ToolAxisConstraint.ReferenceVector[2]':'0',
 "machine_axis('A').ControlInformation.Min":'-30,0',
 "machine_axis('A').ControlInformation.Max":'-15,0',
 "machine_axis('A').ControlInformation.Priority":'high_priority'}

def check(expression,value,compare='text'):
    return {'expression':expression,'expected':value,'compare':compare,'tolerance':.0001}
def controls():
    return [check('simulationstate.ToolControl.OrientationVector.Mode','fixed_vector'),
            check('simulationstate.ToolControl.OrientationVector.Space','machine'),
            check('simulationstate.ToolControl.OrientationVector.ReferenceVector[0]',1,'number'),
            check('simulationstate.ToolControl.OrientationVector.ReferenceVector[1]',0,'number'),
            check('simulationstate.ToolControl.OrientationVector.ReferenceVector[2]',0,'number'),
            check("machine_axis('A').ControlInformation.Min",-30,'number'),
            check("machine_axis('A').ControlInformation.Max",-15,'number'),
            check("machine_axis('A').ControlInformation.Priority",'high_priority')]

class SnapshotFake(Fake):
    def __init__(self):
        super().__init__();self.requests=[];self.state=STATE_XML;self.machine=MACHINE_XML;self.scalars=dict(SCALARS)
    def read_xml(self,expressions,guard):
        assert guard==GUARD
        self.requests.append(expressions[:]);values={STATE:self.state,MACHINE:self.machine,TOOL:'<tool><Name>DEMO</Name></tool>'}
        return [values[e] if e in values else '<scalar>'+self.scalars[e]+'</scalar>' if e in self.scalars else '<string>'+self.value+'</string>' for e in expressions]

class SettingsSnapshotTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()
    def run_plan(self,job,fake,name='run'):
        return Executor(job,self.root/name,fake,lock_dir=self.root/'locks').run(allow_write=True)
    def test_equivalent_controls_keep_all_conditions_with_fewer_actual_reads(self):
        counts=[]
        for enabled in (False,True):
            fake=SnapshotFake();job=plan();job['settings_snapshots']=enabled;job['operations'][0]['postchecks']=controls()
            self.run_plan(job,fake,str(enabled));counts.append(sum(map(len,fake.requests)))
            self.assertEqual(fake.calls,['one'])
        self.assertEqual(counts,[9,3]) # one precheck plus 8 fields / two complete snapshots
    def test_failure_of_each_scalar_still_blocks_the_next_command(self):
        for index,item in enumerate(controls()):
            fake=SnapshotFake();job=plan(two=True);checks=controls();checks[index]=dict(item,expected='wrong' if item['compare']=='text' else 999)
            job['operations'][0]['postchecks']=checks
            with self.subTest(expression=item['expression']),self.assertRaises(HelperError):self.run_plan(job,fake,'bad-'+str(index))
            self.assertEqual(fake.calls,['one'])
    def test_inactive_plane_vector_keeps_three_original_reads(self):
        fake=SnapshotFake();items=controls()+[check('simulationstate.ToolControl.ToolAxisConstraint.ReferenceVector[%d]'%i,v,'number') for i,v in enumerate([1,0,0])]
        values=SettingsObservation(fake,GUARD).read_checks(items)
        self.assertEqual(len(values),len(items));self.assertTrue(all(e in fake.requests[0] for e in SCALARS if '.ToolAxisConstraint.ReferenceVector' in e))
    def test_missing_supported_field_uses_fresh_original_query_not_expected_value(self):
        fake=SnapshotFake();fake.state=fake.state.replace('<Space>machine</Space>','');fake.scalars['simulationstate.ToolControl.OrientationVector.Space']='toolpath'
        job=plan();job['operations'][0]['postchecks']=controls()
        with self.assertRaises(HelperError):self.run_plan(job,fake)
        self.assertIn(['simulationstate.ToolControl.OrientationVector.Space'],fake.requests)
        self.assertEqual(fake.calls,['one'])
    def test_fallback_failure_is_not_suppressed(self):
        fake=SnapshotFake();fake.state=fake.state.replace('<Space>machine</Space>','');original=fake.read_xml
        def read(expressions,guard):
            if expressions==['simulationstate.ToolControl.OrientationVector.Space']:raise HelperError('Read failed')
            return original(expressions,guard)
        fake.read_xml=read
        with self.assertRaisesRegex(HelperError,'Read failed'):SettingsObservation(fake,GUARD).read_checks(controls())
    def test_new_checks_call_and_resumed_run_do_not_reuse_old_settings(self):
        fake=SnapshotFake();job=plan();job['operations'][0]['postchecks']=controls();self.run_plan(job,fake)
        fake.machine=fake.machine.replace('high_priority','low_priority')
        with self.assertRaises(StateDrift):self.run_plan(job,fake)
        self.assertEqual(fake.calls,['one']);self.assertEqual(sum(MACHINE in x for x in fake.requests),2)
    def test_same_observer_reads_a_new_batch_fresh(self):
        fake=SnapshotFake();observer=SettingsObservation(fake,GUARD)
        a=observer.read_checks(controls());fake.machine=fake.machine.replace('high_priority','low_priority');b=observer.read_checks(controls())
        self.assertNotEqual(a[-1],b[-1]);self.assertEqual(len(fake.requests),2)
    def test_contract_consumes_snapshots_once_and_clear_forces_fresh_reads(self):
        fake=SnapshotFake();observer=SettingsObservation(fake,GUARD);observer.read_checks(controls(),with_contract=True)
        before=len(fake.requests);values=observer.take_contract_values();self.assertEqual(values[0],MACHINE_XML);self.assertEqual(values[2],STATE_XML);self.assertEqual(len(fake.requests),before)
        self.assertEqual(fake.requests[-1][-3:],[MACHINE,TOOL,STATE])
        observer.take_contract_values();self.assertEqual(fake.requests[-1],[MACHINE,TOOL,STATE])
        observer.read_checks(controls());observer.clear();observer.take_contract_values();self.assertEqual(fake.requests[-1],[MACHINE,TOOL,STATE])
    def test_non_XML_barrier_invalidates_snapshot_before_contract(self):
        fake=SnapshotFake();job=plan();token='0'*32;receipt=self.root/'receipt';receipt.write_text(token)
        job['operations'][0]['postchecks']=controls()+[{'kind':'receipt','path':str(receipt),'token':token},{'kind':'recording'}]
        # Call _checks directly: recording preparation is mocked, never connect
        # or construct a synthetic live plan with unpinned recording inputs.
        valid=plan();engine=Executor(valid,self.root/'barrier',fake,lock_dir=self.root/'locks')
        class Audit:
            kin=type('Kin',(),{'contract':{}})()
            def poll(self,**kwargs):return {'records':0,'raw_records':0,'complete':False,'offset':0}
        record={'kind':'recording','source':{},'contract':{},'recording_path':'unused'}
        from powermill_com_helper.core import digest
        engine._recording_audits[digest({k:record[k] for k in ('source','contract','recording_path')})]=Audit()
        def verify(contract,transport,guard,*,values=None):self.assertEqual(fake.requests[-1],[MACHINE,TOOL,STATE])
        with patch.object(engine,'_prepare_recordings'),patch('powermill_com_helper.simulation.verify_live_contract',side_effect=verify),patch('powermill_com_helper.simulation.verify_native_issues',return_value={'count':0}):
            self.assertTrue(engine._checks(controls()+[{'kind':'receipt','path':str(receipt),'token':token},record]))
    def test_malformed_or_ambiguous_snapshots_fail_closed(self):
        for index,raw in enumerate(['<map/>','broken',STATE_XML.replace('<Space>machine</Space>','<Space>machine</Space><Space>toolpath</Space>')]):
            fake=SnapshotFake();fake.state=raw
            with self.subTest(raw=raw),self.assertRaises(HelperError):SettingsObservation(fake,GUARD).read_checks(controls())
        fake=SnapshotFake();fake.machine='<map>'+MACHINE_XML[5:-6]*2+'</map>'
        with self.assertRaisesRegex(HelperError,'axis identity'):SettingsObservation(fake,GUARD).read_checks(controls())
    def test_truncated_snapshot_batch_fails(self):
        fake=SnapshotFake();original=fake.read_xml;fake.read_xml=lambda e,g:original(e,g)[:-1]
        with self.assertRaises(HelperError):SettingsObservation(fake,GUARD).read_checks(controls())
    def test_entity_typed_machine_snapshot_has_same_control_values_as_map(self):
        fake=SnapshotFake();first=SettingsObservation(fake,GUARD).read_checks(controls())
        fake.machine=fake.machine.replace('<map>','<machinetool>').replace('</map>','</machinetool>')
        second=SettingsObservation(fake,GUARD).read_checks(controls());self.assertEqual(first,second)
    def test_XML_hash_checks_retain_original_scalar_API_representation(self):
        fake=SnapshotFake();expression='simulationstate.ToolControl.OrientationVector.Mode';raw='<scalar>fixed_vector</scalar>'
        expected=hashlib.sha256(ET.canonicalize(raw,strip_text=True).encode()).hexdigest()
        job=plan();job['operations'][0]['postchecks']=controls()+[check(expression,expected,'xml_sha256')]
        self.run_plan(job,fake);self.assertIn(expression,fake.requests[-1])
    def test_invalid_optimization_switch_is_rejected_before_execution(self):
        job=plan();job['settings_snapshots']='false';fake=SnapshotFake()
        with self.assertRaises(HelperError):self.run_plan(job,fake)
        self.assertEqual(fake.calls,[])
    def test_contract_refresh_after_fallback_catches_late_collision_setting_change(self):
        from .test_simulation import machine_xml
        from powermill_com_helper.simulation import machine_fingerprint,verify_live_contract
        fake=SnapshotFake();fake.machine=machine_xml().replace('<Address>A</Address>','<Address>A</Address><Min>-30</Min><Max>-15</Max><Priority>high_priority</Priority>')
        contract={'machine_geometry_sha256':machine_fingerprint(fake.machine),'tool_xml_sha256':hashlib.sha256(ET.canonicalize('<tool><Name>DEMO</Name></tool>',strip_text=True).encode()).hexdigest()}
        fake.state=fake.state.replace('<Space>machine</Space>','');original=fake.read_xml
        def read(expressions,guard):
            result=original(expressions,guard)
            if expressions==['simulationstate.ToolControl.OrientationVector.Space']:
                fake.state=fake.state.replace('<CheckCollisions>1</CheckCollisions>','<CheckCollisions>0</CheckCollisions>')
            return result
        fake.read_xml=read;observer=SettingsObservation(fake,GUARD);observer.read_checks(controls(),with_contract=True)
        values=observer.take_contract_values()
        self.assertEqual(ET.fromstring(values[2]).findtext('Issues/Collisions/CheckCollisions'),'0')
        self.assertEqual(fake.requests[-1],[MACHINE,TOOL,STATE])
        with self.assertRaisesRegex(HelperError,'collision checks'):verify_live_contract(contract,fake,GUARD,values=values)
    def test_field_availability_drift_during_fallback_fails_closed(self):
        fake=SnapshotFake();fake.state=fake.state.replace('<Space>machine</Space>','');original=fake.read_xml
        def read(expressions,guard):
            result=original(expressions,guard)
            if expressions==['simulationstate.ToolControl.OrientationVector.Space']:
                fake.state=fake.state.replace('<Mode>fixed_vector</Mode>','<Mode>fixed_vector</Mode><Space>machine</Space>')
            return result
        fake.read_xml=read
        with self.assertRaisesRegex(HelperError,'availability changed'):SettingsObservation(fake,GUARD).read_checks(controls(),with_contract=True)
