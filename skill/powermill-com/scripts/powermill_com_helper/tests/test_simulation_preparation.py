from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout,redirect_stderr
from io import StringIO
import copy,json,math,tempfile,unittest
from .test_simulation import contract,machine_xml,TOOL
from powermill_com_helper.core import HelperError,StateDrift,atomic_json,digest
from powermill_com_helper.simulation import sha256_file,machine_fingerprint
from powermill_com_helper.simulation_preparation import prepare_simulation
from powermill_com_helper.__main__ import main

class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.source=self.root/'reference.RobSim';self.contract=self.root/'contract.json'
        c=contract();c['machine_geometry_sha256']=machine_fingerprint(machine_xml());c['tool_xml_sha256']='1'*64;atomic_json(self.contract,c)
        rows=[]
        for i in range(20):
            tilt=math.radians(i*.4);z=[math.sin(tilt),0,math.cos(tilt)]
            for move,comp in [('5','3'),('4','3'),('12','1')]:
                rows.append('A0 B0 C0 D0 E0 F0 T0 R0 ('+','.join(map(str,[100+i,2,i]+z+[1,0,0]))+') MOVETYPE='+move+' COMPTYPE='+comp+' TOOL=10 FEED=1000\n')
        self.source.write_text('(Synthetic)\n'+''.join(rows)+'(End)\n')
        self.request={'schema':1,'source':{'path':str(self.source),'sha256':sha256_file(self.source)},'contract':{'path':str(self.contract),'sha256':sha256_file(self.contract)},'settings':{'native_tolerance':5},'required_passes':[7,15],'policy_by_pass':['plane' if i%2 else 'free' for i in range(20)]}
    def tearDown(self):self.tmp.cleanup()
    def test_extremes_regressions_policy_families_context_and_no_COM(self):
        with patch('powermill_com_helper.__main__.PowerMillTransport',side_effect=AssertionError('No COM')):
            prepare_simulation(self.request,self.root/'prepared')
        report=json.loads((self.root/'prepared/preparation.json').read_text());cases={c['pass'] for c in report['critical']['cases']}
        self.assertTrue({0,19,7,15}<=cases);self.assertFalse(report['full_run_ready']);self.assertTrue(report['offline_only']);self.assertEqual(report['source_records'],60)
        for w in report['critical']['windows']:
            self.assertLessEqual(w['last_pass']-w['first_pass']+1,9)
            for i in w['cases']:self.assertLessEqual(w['first_pass'],max(0,i-1));self.assertGreaterEqual(w['last_pass'],min(19,i+1))
    def test_compatible_profile_is_hint_and_contract_changes_fail(self):
        prepare_simulation(self.request,self.root/'first');path=self.root/'first/settings-profile.json';r=copy.deepcopy(self.request);r.pop('settings');r['candidate_profile']={'path':str(path),'sha256':sha256_file(path)}
        prepare_simulation(r,self.root/'second');j=json.loads((self.root/'second/preparation.json').read_text());self.assertTrue(j['profile_reused']);self.assertFalse(j['full_run_ready'])
        c=json.loads(self.contract.read_text());c['orientation']['max_X_degrees']=4;atomic_json(self.contract,c);r['contract']['sha256']=sha256_file(self.contract)
        with self.assertRaisesRegex(StateDrift,'incompatible'):prepare_simulation(r,self.root/'third')
        self.assertFalse((self.root/'third').exists())
    def test_source_tamper_and_existing_output_fail_without_overwrite(self):
        before=self.source.read_bytes();self.source.write_bytes(before+b'bad')
        with self.assertRaises(StateDrift):prepare_simulation(self.request,self.root/'bad')
        self.source.write_bytes(before);prepare_simulation(self.request,self.root/'ok');proof=(self.root/'ok/preparation.json').read_bytes()
        with self.assertRaises(HelperError):prepare_simulation(self.request,self.root/'ok')
        self.assertEqual((self.root/'ok/preparation.json').read_bytes(),proof)
    def test_cli_timers_offline_and_failed_preparation_export(self):
        request=self.root/'request.json';atomic_json(request,self.request);timer=self.root/'timer.json'
        with patch('powermill_com_helper.__main__.PowerMillTransport',side_effect=AssertionError('No COM')):
            with redirect_stdout(StringIO()):self.assertEqual(main(['prepare-simulation',str(request),str(self.root/'bundle'),'--timing-output',str(timer)]),0)
            report=json.loads(timer.read_text());self.assertEqual(report['exit_code'],0);self.assertIn('preparation.source_catalog',report['totals'])
            with redirect_stderr(StringIO()):self.assertEqual(main(['prepare-simulation',str(request),str(self.root/'bundle'),'--timing-output',str(self.root/'failed-timer.json')]),2)
        report=json.loads((self.root/'failed-timer.json').read_text());self.assertEqual(report['exit_code'],2);self.assertTrue(any(r.get('failed') for r in report['spans']))
    def test_timer_cannot_replace_inputs(self):
        request=self.root/'request.json';atomic_json(request,self.request);before=request.read_bytes()
        with redirect_stderr(StringIO()):self.assertEqual(main(['prepare-simulation',str(request),str(self.root/'bundle'),'--timing-output',str(request)]),2)
        self.assertEqual(before,request.read_bytes());self.assertFalse((self.root/'bundle').exists())
