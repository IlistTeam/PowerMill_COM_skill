"""Exercise the real PowerShell protocol with an in-memory fake app, never COM."""
from pathlib import Path
import base64,gzip,os,re,subprocess,unittest
from powermill_com_helper.transport import JsonLineProcess
from .test_executor import GUARD
from powermill_com_helper.core import HelperError
from powermill_com_helper.timing import Profiler

FAKE=r'''
function New-FakeApplication {
 $app=[pscustomobject]@{Version=2023000;Busy=$false;Debug=[pscustomobject]@{ProcessID=1234};Project=[pscustomobject]@{Models=@([pscustomobject]@{ID='MODEL'})};ModelXmlOverride=$null;Value='before';Calls=(New-Object 'Collections.Generic.List[string]');HelpText="Commands available :`r`r`nACTIVATE`r`r`nCREATE`t`"Создать объекты`"`r`r`nDEACTIVATE"}
 $app|Add-Member -MemberType ScriptMethod -Name GetParameterXML -Value {
  param($expression)
  $this.Calls.Add('XML:'+$expression)
  if($expression -eq 'trace'){return ('<string>'+($this.Calls -join '|')+'</string>')}
  if($expression -eq 'project_pathname(0)'){return '<string></string>'}
  if($expression -eq "extract(folder('model'),'ID')"){
   if($null -ne $this.ModelXmlOverride){return $this.ModelXmlOverride}
   return ('<array>'+((@($this.Project.Models)|ForEach-Object{'<string>'+[Security.SecurityElement]::Escape($_.ID)+'</string>'}) -join '')+'</array>')
  }
  if($expression -eq 'duplicate_model_id'){$this.ModelXmlOverride='<array><string>MODEL</string><string>model</string></array>'}
  if($expression -eq 'empty_model_id'){$this.ModelXmlOverride='<array><string></string></array>'}
  if($expression -eq 'bad_model_schema'){$this.ModelXmlOverride='<string>MODEL</string>'}
  if($expression -eq 'nested_model_id'){$this.ModelXmlOverride='<array><string><value>MODEL</value></string></array>'}
  if($expression -eq 'malformed_models'){$this.ModelXmlOverride='<array>'}
  if($expression -eq 'add_model'){$this.Project.Models+=@([pscustomobject]@{ID='ADDED'})}
  if($expression -eq 'remove_model'){$this.Project.Models=@()}
  if($expression -eq 'hide_model'){$this.Project.Models+=@([pscustomobject]@{ID='ADDED'});$this.ModelXmlOverride='<array><string>MODEL</string></array>'}
  if($expression -eq 'become_busy'){$this.Busy=$true}
  if($expression -eq 'swap_model'){$this.Project.Models=@([pscustomobject]@{ID='OTHER'})}
  return ('<string>'+$this.Value+'</string>')
 }
 $app|Add-Member -MemberType ScriptMethod -Name DoCommandEx -Value {
  param($command,[ref]$output)
  $this.Calls.Add('CMD:'+$command)
  if($command -eq 'HELP'){$output.Value=$this.HelpText;return 0}
  if($command -eq 'MAKE_BUSY'){$this.Busy=$true;$output.Value='';return 0}
  $this.Value='Папка СС';$output.Value='';return 0
 }
 return $app
}
'''

@unittest.skipUnless(os.name=='nt','PowerShell 5 protocol integration requires Windows')
class WorkerProtocolTests(unittest.TestCase):
    def test_rot_selector_compiles_without_calling_com(self):
        source=Path(__file__).resolve().parents[1].joinpath('worker.ps1').read_text('utf-8');csharp=re.search("Add-Type -TypeDefinition @'\n(.*?)\n'@",source,re.S)[1]
        code="Add-Type -TypeDefinition @'\n"+csharp+"\n'@\n[PowerMillComRot].FullName";encoded=base64.b64encode(code.encode('utf-16le')).decode('ascii');executable=str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
        result=subprocess.run([executable,'-NoProfile','-NonInteractive','-STA','-EncodedCommand',encoded],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'));self.assertIn(b'PowerMillComRot',result.stdout)
    def test_project_or_busy_change_inside_read_batch_is_rejected(self):
        for expression in ['become_busy','swap_model']:
            channel=self.channel()
            try:
                with self.assertRaises(HelperError):channel.request('read_xml',{'guard':GUARD,'expressions':['value',expression,'value']},10)
            finally:channel.close()
    def channel(self,fixture=None,profile=False,model_reader='xml',fake_source=None):
        source=Path(__file__).resolve().parents[1].joinpath('worker.ps1').read_text(encoding='utf-8')
        # If the fake is ever bypassed, the worker errors rather than using COM.
        source=source.replace('GetActiveObject','COM_FORBIDDEN_IN_TEST').replace('[PowerMillComRot]::Find','[COM_FORBIDDEN_IN_TEST]::Find').replace('$pm=$null','$pm=New-FakeApplication',1)
        if profile:source=source.replace('$profileEnabled=$false','$profileEnabled=$true',1)
        if model_reader=='com':source=source.replace("$sessionModelReadMode='xml'","$sessionModelReadMode='com'",1)
        if fixture:
            path=str(Path(__file__).with_name('fixtures')/fixture).replace("'","''")
            source=source.replace('$pm=New-FakeApplication',"$pm=New-FakeApplication; $pm.HelpText=[IO.File]::ReadAllText('"+path+"')",1)
        # Fake + complete worker exceeds Win32's argument limit; unpack in memory.
        payload=base64.b64encode(gzip.compress(((fake_source or FAKE)+'\n'+source).encode('utf-8'))).decode('ascii')
        bootstrap="$m=[IO.MemoryStream]::new([Convert]::FromBase64String('"+payload+"'));$z=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);$r=[IO.StreamReader]::new($z,[Text.Encoding]::UTF8);$s=$r.ReadToEnd();$r.Dispose();. ([scriptblock]::Create($s))"
        encoded=base64.b64encode(bootstrap.encode('utf-16le')).decode('ascii')
        executable=str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
        return JsonLineProcess([executable,'-NoProfile','-NonInteractive','-STA','-EncodedCommand',encoded])
    def test_model_readers_return_same_complete_sessions(self):
        variants=[FAKE,FAKE.replace("Models=@([pscustomobject]@{ID='MODEL'})","Models=@([pscustomobject]@{ID='SECOND'},[pscustomobject]@{ID='MODEL'})"),FAKE.replace("Models=@([pscustomobject]@{ID='MODEL'})","Models=@()")]
        for fake in variants:
            sessions=[]
            for mode in ('com','xml'):
                channel=self.channel(model_reader=mode,fake_source=fake)
                try:sessions.append(channel.request('session',{},10))
                finally:channel.close()
            self.assertEqual(sessions[0],sessions[1])
    def test_invalid_model_xml_fails_closed_without_fallback_before_command(self):
        for expression in ('duplicate_model_id','empty_model_id','bad_model_schema','nested_model_id','malformed_models'):
            with self.subTest(expression=expression):
                channel=self.channel()
                try:
                    with self.assertRaises(HelperError):channel.request('read_xml',{'guard':GUARD,'expressions':[expression]},10)
                    out=channel.request('execute',{'guard':GUARD,'operation':{'command':'UNEXPECTED'}},10)
                    self.assertFalse(out['dispatched'])
                finally:channel.close()
    def test_both_model_readers_detect_addition_and_replacement_inside_batch(self):
        for mode in ('com','xml'):
            for expression in ('add_model','swap_model','remove_model'):
                channel=self.channel(model_reader=mode)
                try:
                    with self.assertRaises(HelperError):channel.request('read_xml',{'guard':GUARD,'expressions':['value',expression,'value']},10)
                    out=channel.request('execute',{'guard':GUARD,'operation':{'command':'UNEXPECTED'}},10)
                    self.assertFalse(out['dispatched'])
                finally:channel.close()
    def test_native_collection_omission_cannot_authorize_discovery_or_command(self):
        channel=self.channel()
        try:
            channel.request('read_xml',{'guard':GUARD,'expressions':['hide_model']},10)
            with self.assertRaises(HelperError):channel.request('session',{},10)
            out=channel.request('execute',{'guard':GUARD,'operation':{'command':'UNEXPECTED'}},10)
            self.assertFalse(out['dispatched']);self.assertIn('Native/COM',out['output'])
        finally:channel.close()
    def test_profile_has_same_read_and_command_trace_and_all_batch_guards(self):
        traces=[]
        for enabled in (False,True):
            channel=self.channel(profile=enabled)
            try:
                with Profiler() as p:
                    channel.request('probe',{'guard':GUARD},10)
                    channel.request('read_xml',{'guard':GUARD,'expressions':['value','value']},10)
                    channel.request('execute',{'guard':GUARD,'operation':{'command':'SET_TEST_VALUE'}},10)
                    traces.append(channel.request('read_xml',{'guard':GUARD,'expressions':['trace']},10))
                samples=p.snapshot()['worker_requests']
                if enabled:
                    metrics=samples[1]['timing']['metrics'];self.assertEqual(metrics['session.total']['count'],3);self.assertEqual(metrics['guard.idle']['count'],3);self.assertEqual(metrics['xml.target.other']['count'],2)
                else:self.assertEqual(samples,[])
            finally:channel.close()
        self.assertEqual(traces[0],traces[1])
    def test_actual_archived_contexts_in_powershell_classifier(self):
        for fixture,want in [('help-root.txt',True),('help-style.txt',False)]:
            channel=self.channel(fixture)
            try:self.assertEqual(channel.request('probe',{'guard':GUARD},10)['root_ready'],want)
            finally:channel.close()
    def test_actual_powershell_probe_utf8_reads_and_before_dispatch_rejection(self):
        channel=self.channel()
        try:
            session=channel.request('session',{},10)
            self.assertEqual(session['process_id'],1234);self.assertEqual(session['model_ids'],['MODEL'])
            state=channel.request('probe',{'guard':GUARD},10)
            self.assertTrue(state['root_ready']);self.assertFalse(state['busy']);self.assertEqual(state['model_ids'],['MODEL'])
            self.assertEqual(channel.request('read_xml',{'guard':GUARD,'expressions':['value']},10),['<string>before</string>'])
            with self.assertRaises(HelperError):
                channel.request('read_xml',{'guard':GUARD,'expressions':['point_add_parameter(1,2)']},10)
            with self.assertRaises(HelperError):
                channel.request('read_xml',{'guard':GUARD,'expressions':['select_components(1)']},10)
            expression="components(model_components_thicknessset(entity('toolpath','P1_PODDERZHKA'),'surfaces',1))"
            self.assertEqual(channel.request('read_xml',{'guard':GUARD,'expressions':[expression]},10),['<string>before</string>'])
            out=channel.request('execute',{'guard':GUARD,'operation':{'command':'SET_TEST_VALUE'}},10)
            self.assertTrue(out['dispatched']);self.assertEqual(out['code'],0)
            self.assertEqual(channel.request('read_xml',{'guard':GUARD,'expressions':['value','value']},10),['<string>Папка СС</string>']*2)
            wrong={**GUARD,'process_id':999}
            out=channel.request('execute',{'guard':wrong,'operation':{'command':'UNEXPECTED'}},10)
            self.assertFalse(out['dispatched'])
            channel.request('execute',{'guard':GUARD,'operation':{'command':'MAKE_BUSY'}},10)
            state=channel.request('probe',{'guard':GUARD},10)
            self.assertTrue(state['busy']);self.assertFalse(state['root_ready'])
        finally:channel.close()
