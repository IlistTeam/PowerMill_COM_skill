from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout,redirect_stderr
from io import StringIO
import copy,hashlib,json,math,tempfile,unittest
from powermill_com_helper.simulation import SimulationAudit,sha256_file,Kinematics,machine_fingerprint,verify_live_contract
from powermill_com_helper.core import HelperError,StateDrift,Executor
from powermill_com_helper.__main__ import main
from .test_executor import GUARD,Fake,plan

ADDRESSES='ABCDEFTR'
def contract():
    return {'schema':1,'axes':[{'address':a,'type':'rotary','origin':[0,0,0],'direction':[0,0,1],'limits':[0,360] if a=='R' else [-360,360]} for a in ADDRESSES],
        'robot_chain':list('ABCDEF'),'table_chain':['T','R'],'head':{'tcp':[1,0,0],'x':[1,0,0],'z':[0,0,1]},
        'source_frame':{'origin':[0,0,0],'basis':[[1,0,0],[0,1,0],[0,0,1]]},'region':{'undo_axes':['T'],'origin':[0,0,0],'min':[.9,-.1],'max':[1.1,.1]},
        'orientation':{'reference_X':[1,0,0],'max_X_degrees':.01},'fixed_axes':{'T':0},'travel_axis':'R','min_travel_degrees':350}

def line(i,r,*,plain=False,collision='NONE'):
    ang=math.radians(r);p=(math.cos(ang),-math.sin(ang),0);axes=' '.join(a+str(r if a=='R' else 0) for a in ADDRESSES)
    pose=','.join('%.12f'%v for v in p+(0,0,1)+p);attrs=''
    if not plain:
        attrs=' FEED=1500 SPINDLESPEED=1500 TOOL=10 MOVETYPE='+('1' if i==0 else '12')
        if i:attrs+=' COMPTYPE=1'
        if i==1:attrs+=' udp_LaserPower=42'
    if i:attrs+=' TIME='+str(i)
    return axes+' ('+pose+')'+attrs+' COLLISION_STATE='+collision+'\n'

def machine_xml():
    axes=''.join('<Axis><ControlInformation><Address>'+a+'</Address></ControlInformation><Type>simple_rotary</Type><X>0</X><Y>0</Y><Z>0</Z><I>0</I><J>0</J><K>1</K></Axis>' for a in ADDRESSES)
    frame='<X>0</X><Y>0</Y><Z>0</Z><I>0</I><J>0</J><K>1</K><U>1</U><V>0</V><W>0</W>'
    return '<map><MachineName>DEMO</MachineName>'+axes+'<HeadAttachPoint>'+frame+'</HeadAttachPoint><TableAttachPoint>'+frame+'</TableAttachPoint><CollisionExclusionList><Group>adjacent only</Group></CollisionExclusionList></map>'
TOOL='<tool><Name>DEMO</Name><Length>1</Length></tool>'
STATE='<simulationstate><Issues><Collisions><CheckCollisions>1</CheckCollisions><CheckToolAndHolder>1</CheckToolAndHolder></Collisions></Issues></simulationstate>'

class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.source=self.root/'source.RobSim';self.native=self.root/'native.RobSim';self.cp=self.root/'audit.json';self.c=contract();self.rows=[line(i,r) for i,r in enumerate([0,90,180,270,359,0])];self.header='(Simulation Output File)\n';self.source.write_text(self.header+''.join(self.rows)+'(End)\n',encoding='utf-8');self.sha=sha256_file(self.source)
    def tearDown(self):self.tmp.cleanup()
    def audit(self,checkpoint=True):return SimulationAudit(self.source,self.sha,self.native,self.c,checkpoint=self.cp if checkpoint else None)
    def write(self,rows=None,end=True):self.native.write_text(self.header+''.join(self.rows if rows is None else rows)+('(End)\n' if end else ''),encoding='utf-8')
    def test_incremental_partial_line_then_checkpoint_resume(self):
        cut=len(self.rows[2])//2;self.native.write_text(self.header+''.join(self.rows[:2])+self.rows[2][:cut],encoding='utf-8')
        a=self.audit();first=a.poll();self.assertEqual(first['records'],2);self.assertFalse(first['complete'])
        b=self.audit()
        with self.native.open('a',encoding='utf-8') as f:f.write(self.rows[2][cut:]+''.join(self.rows[3:])+'(End)\n')
        with patch.object(b.kin,'verify',wraps=b.kin.verify) as verify:result=b.poll(complete=True);self.assertEqual(verify.call_count,4)
        self.assertEqual(result['records'],6);self.assertTrue(result['complete']);self.assertAlmostEqual(result['travel_degrees'],360);self.assertEqual(result['wrap_count'],1)
    def test_only_proven_boundary_state_duplicates_are_not_source_points(self):
        self.write([self.rows[0],line(0,0,plain=True)]+self.rows[1:]+[line(5,0,plain=True)])
        result=self.audit().poll(complete=True);self.assertEqual(result['records'],6);self.assertEqual(result['raw_records'],8);self.assertEqual([x['boundary'] for x in result['duplicates']],['startup','terminal'])
    def test_middle_duplicate_is_not_silently_removed(self):
        self.write(self.rows[:3]+[line(2,180,plain=True)]+self.rows[3:])
        with self.assertRaises(HelperError):self.audit().poll(complete=True)
    def test_old_reference_joint_layout_is_not_used_as_new_native_solution(self):
        original=self.source.read_text('utf-8');self.source.write_text(original.replace('A0 B0 C0 D0 E0 F0 T0','A25 B8 C9'));self.sha=sha256_file(self.source);self.write()
        self.assertTrue(self.audit(False).poll(complete=True)['complete'])
        self.write([row.replace('A0 ','',1) for row in self.rows])
        with self.assertRaises(HelperError):self.audit(False).poll(complete=True)
    def test_collision_on_duplicate_fails(self):
        self.write([self.rows[0],line(0,0,plain=True,collision='COLLISION')]+self.rows[1:])
        with self.assertRaises(HelperError):self.audit().poll()
    def test_source_pp_and_movement_changes_fail(self):
        for old,new in [('udp_LaserPower=42','udp_LaserPower=41'),('MOVETYPE=12','MOVETYPE=5'),('TOOL=10','TOOL=11')]:
            with self.subTest(field=old):
                rows=list(self.rows);rows[1]=rows[1].replace(old,new);self.write(rows)
                with self.assertRaises(HelperError):self.audit(False).poll(complete=True)
    def test_pose_limits_axis_and_region_fail(self):
        for field in ['position','axis','joint','region','X']:
            self.c=contract();rows=list(self.rows)
            if field=='position':rows[2]=rows[2].replace('-1.000000000000','-2.000000000000',1)
            if field=='axis':rows[2]=rows[2].replace('0.000000000000,0.000000000000,1.000000000000','0.000000000000,1.000000000000,0.000000000000')
            if field=='joint':self.c['axes'][0]['limits']=[1,2]
            if field=='region':self.c['region']['min']=[2,-.1];self.c['region']['max']=[3,.1]
            if field=='X':rows[2]=rows[2].replace('F0 ','F0.1 ')
            self.write(rows)
            with self.subTest(field=field):
                with self.assertRaises(HelperError):self.audit(False).poll(complete=True)
    def test_source_rewrite_and_checked_native_rewrite_fail(self):
        self.write(self.rows[:2],end=False);a=self.audit();a.poll();original=self.native.read_bytes();self.native.write_bytes(original.replace(b'A0',b'A1',1))
        with self.assertRaises(StateDrift):a.poll()
        with self.assertRaises(StateDrift):a.poll()
        self.native.write_bytes(original);b=self.audit();self.source.write_text('changed')
        with self.assertRaises(StateDrift):b.poll()
    def test_replaced_truncated_and_corrupt_checkpoint_fail(self):
        self.write(self.rows[:2],end=False);self.audit().poll();old=self.native.read_bytes();replacement=self.root/'new';replacement.write_bytes(old);replacement.replace(self.native)
        with self.assertRaises(StateDrift):self.audit().poll()
        self.native.write_bytes(b'')
        with self.assertRaises(StateDrift):self.audit().poll()
        state=json.loads(self.cp.read_text());state['records']=5;self.cp.write_text(json.dumps(state))
        with self.assertRaises(StateDrift):self.audit()
    def test_incomplete_end_and_records_after_end_fail(self):
        self.write(end=False);a=self.audit();a.poll()
        with self.assertRaises(HelperError):a.poll(complete=True)
        self.write();self.native.write_text(self.native.read_text()+self.rows[-1])
        with self.assertRaises(HelperError):self.audit(False).poll(complete=True)
    def test_nonfinite_and_nonrigid_geometry_fail(self):
        self.write([self.rows[0].replace('1.000000000000','nan',1)])
        with self.assertRaises(HelperError):self.audit(False).poll()
        self.c['source_frame']['basis'][0]=[2,0,0]
        with self.assertRaises(HelperError):self.audit()
    def test_offline_cli_never_constructs_com_or_overwrites_input(self):
        self.write();p=self.root/'contract.json';p.write_text(json.dumps(self.c));check={'source':{'path':str(self.source),'sha256':self.sha},'contract':{'path':str(p),'sha256':sha256_file(p)},'recording_path':str(self.native),'complete':True};request=self.root/'request.json';request.write_text(json.dumps(check));output=self.root/'report.json';before=self.native.read_bytes()
        with patch('powermill_com_helper.__main__.PowerMillTransport',side_effect=AssertionError('No COM')):
            with redirect_stdout(StringIO()):self.assertEqual(main(['audit-simulation',str(request),str(output)]),0)
            with redirect_stderr(StringIO()):self.assertEqual(main(['audit-simulation',str(request),str(self.native)]),2)
        self.assertEqual(self.native.read_bytes(),before);self.assertTrue(json.loads(output.read_text())['complete'])
    def test_live_geometry_tool_and_collision_settings_are_required(self):
        c=contract();c['machine_geometry_sha256']=machine_fingerprint(machine_xml());c['tool_xml_sha256']=hashlib.sha256(TOOL.encode()).hexdigest()
        class Live:
            def read_xml(self,expressions,guard):return [machine_xml(),TOOL,STATE]
        verify_live_contract(c,Live(),GUARD)
        for mode in ('tool','geometry','collisions'):
            class Bad:
                def read_xml(self,expressions,guard):return [machine_xml().replace('adjacent only','ALL') if mode=='geometry' else machine_xml(),TOOL.replace('DEMO','OTHER') if mode=='tool' else TOOL,STATE.replace('>1<','>0<') if mode=='collisions' else STATE]
            with self.subTest(mode=mode):
                with self.assertRaises(HelperError):verify_live_contract(c,Bad(),GUARD)
    def test_fingerprint_detects_part_hierarchy_and_model_changes_but_not_joint_values(self):
        raw=machine_xml().replace('<CollisionExclusionList>','<MachinePart><Name>base</Name><ModelList><DmtFile><Path><File>DEMO.dmt</File></Path></DmtFile></ModelList></MachinePart><CollisionExclusionList>')
        h=machine_fingerprint(raw)
        self.assertNotEqual(h,machine_fingerprint(raw.replace('DEMO.dmt','OTHER.dmt')))
        self.assertNotEqual(h,machine_fingerprint(raw.replace('<Name>base</Name>','<Name>arm</Name>')))
        self.assertEqual(h,machine_fingerprint(raw.replace('<Address>A</Address>','<Address>A</Address><Value>42</Value><Priority>auto_priority</Priority>')))
    def test_executor_refuses_missing_live_contract_before_dispatch(self):
        p=self.root/'contract.json';p.write_text(json.dumps(self.c));request=plan();request['operations'][0]['postchecks'].append({'kind':'recording','source':{'path':str(self.source),'sha256':self.sha},'contract':{'path':str(p),'sha256':sha256_file(p)},'recording_path':str(self.native)})
        fake=Fake()
        with self.assertRaises(HelperError):Executor(request,self.root/'run',fake,lock_dir=self.root/'locks').run(allow_write=True)
        self.assertEqual(fake.calls,[])
    def test_executor_reuses_stream_audit_and_blocks_settings_drift_before_write(self):
        c=contract();c['machine_geometry_sha256']=machine_fingerprint(machine_xml());c['tool_xml_sha256']=hashlib.sha256(TOOL.encode()).hexdigest();p=self.root/'contract.json';p.write_text(json.dumps(c))
        check={'kind':'recording','source':{'path':str(self.source),'sha256':self.sha},'contract':{'path':str(p),'sha256':sha256_file(p)},'recording_path':str(self.native),'complete':False,'min_records':2};job=plan(two=True)
        job['operations'][0]['postchecks'].append(check);job['operations'][1]['postchecks'].append({**check,'complete':True,'min_records':6})
        owner=self
        class Flow(Fake):
            def __init__(self,drift=False):super().__init__();self.probes=0;self.drift=drift;self.off=False
            def probe(self,guard):
                self.probes+=1
                if self.drift and self.probes==3:self.off=True
                return super().probe(guard)
            def read_xml(self,expressions,guard):
                values={"entity('machinetool','')":machine_xml(),"entity('tool','')":TOOL,'simulation_issues()':'<array/>','simulationstate':STATE.replace('>1<','>0<') if self.off else STATE}
                return [values.get(e,'<string>'+self.value+'</string>') for e in expressions]
            def execute(self,op,guard):
                result=super().execute(op,guard)
                if op['id']=='one':owner.write(owner.rows[:2],end=False)
                else:
                    with owner.native.open('a') as stream:stream.write(''.join(owner.rows[2:])+'(End)\n')
                return result
        fake=Flow();engine=Executor(job,self.root/'flow',fake,lock_dir=self.root/'locks');engine.run(allow_write=True)
        self.assertEqual(fake.calls,['one','two']);self.assertEqual(len(engine._recording_audits),1)
        fake=Flow(True)
        with self.assertRaises(HelperError):Executor(job,self.root/'drift',fake,lock_dir=self.root/'locks').run(allow_write=True)
        self.assertEqual(fake.calls,['one'])
        class IssueFlow(Flow):
            def read_xml(self,expressions,guard):
                values=super().read_xml(expressions,guard)
                return ['<array><map><Severity>error</Severity><Type>unsolvable</Type><Ignored>1</Ignored></map></array>' if e=='simulation_issues()' else v for e,v in zip(expressions,values)]
        fake=IssueFlow()
        with self.assertRaisesRegex(HelperError,'Native simulation issue'):Executor(job,self.root/'native-issue',fake,lock_dir=self.root/'locks').run(allow_write=True)
        self.assertEqual(fake.calls,['one'])

if __name__=='__main__':unittest.main()
