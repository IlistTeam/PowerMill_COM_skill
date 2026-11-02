from pathlib import Path
import base64,json,os,sys,tempfile,time,unittest
from unittest.mock import Mock
from powermill_com_helper.transport import JsonLineProcess,TransportTimeout,ProtocolError,PowerMillTransport
from powermill_com_helper.core import HelperError
from powermill_com_helper.core import Executor
from .test_executor import GUARD,plan

WORKER=Path(__file__).with_name('json_worker.py')
class Adapter:
    def __init__(self,file):self.file=file;self.channel=JsonLineProcess([sys.executable,'-u',str(WORKER)])
    def probe(self,guard):return self.channel.request('probe',{'guard':guard},5)
    def read_xml(self,expressions,guard):return self.channel.request('read_xml',{'guard':guard,'expressions':expressions,'effect_file':str(self.file)},5)
    def execute(self,op,guard):return self.channel.request('execute',{'operation':op,'guard':guard},op['timeout_s'])

class TransportTests(unittest.TestCase):
    def test_channel_is_persistent_and_does_not_restart_after_close(self):
        channel=JsonLineProcess([sys.executable,'-u',str(WORKER)])
        self.assertEqual(channel.request('echo',{'n':1},5),{'n':1});pid=channel.process.pid
        self.assertEqual(channel.request('echo',{'n':2},5),{'n':2});self.assertEqual(channel.process.pid,pid);channel.close()
        with self.assertRaises(ProtocolError):channel.request('echo',{},5)
    @unittest.skipUnless(os.name=='nt','Windows transport construction')
    def test_pid_pinning_and_guard_mismatch_do_not_start_com(self):
        with PowerMillTransport(connect=True,process_id=1234) as transport:
            script=base64.b64decode(transport.channel.command[-1]).decode('utf-16le');self.assertIn('$targetProcessId=1234',script);self.assertIsNone(transport.channel.process)
            self.assertLess(sum(len(x)+3 for x in transport.channel.command),32760)
            with self.assertRaises(HelperError):transport.probe({**GUARD,'process_id':5678})
            self.assertIsNone(transport.channel.process)
        self.assertTrue(transport.channel.closed)
        for pid in [True,0,-1,'1234']:
            with self.assertRaises(HelperError):PowerMillTransport(connect=True,process_id=pid)
        with self.assertRaises(HelperError):PowerMillTransport(connect=True,session_model_read='cached')
    @unittest.skipUnless(os.name=='nt','Windows transport construction')
    def test_incomplete_xml_transport_response_is_quarantined(self):
        transport=PowerMillTransport(connect=True,process_id=1234);fake=Mock();fake.request.return_value=['<string>one</string>'];transport.channel=fake
        with self.assertRaises(ProtocolError):transport.read_xml(['one','two'],GUARD)
        self.assertTrue(fake.poisoned);fake.close.assert_called_once()
    def test_deadline_includes_a_blocked_large_pipe_write(self):
        channel=JsonLineProcess([sys.executable,'-u',str(WORKER),'--never-read'])
        try:
            start=time.monotonic()
            with self.assertRaises(TransportTimeout):channel.request('echo',{'large':'x'*1000000},.2)
            self.assertLess(time.monotonic()-start,3)
        finally:channel.close()
    def test_unicode_protocol_and_external_deadline(self):
        channel=JsonLineProcess([sys.executable,'-u',str(WORKER)])
        try:
            self.assertEqual(channel.request('echo',{'текст':'Папка СС'},5),{'текст':'Папка СС'})
            start=time.monotonic()
            with self.assertRaises(TransportTimeout):channel.request('hold',{},.1)
            self.assertLess(time.monotonic()-start,3)
            self.assertIsNotNone(channel.process.poll())
            with self.assertRaises(ProtocolError):channel.request('echo',{},1)
        finally:channel.close()
    def test_actual_subprocess_timeout_after_effect_reconciles_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);effect=root/'effect.txt';p=plan(two=True)
            for op in p['operations']:op['effect_file']=str(effect)
            p['operations'][0].update(timeout_s=.1,simulated_delay=5)
            first=Adapter(effect)
            try:
                e=Executor(p,root/'run',first,lock_dir=root/'locks')
                with self.assertRaises(TransportTimeout):e.run(allow_write=True)
                self.assertEqual(effect.read_text(),'one')
                self.assertEqual(e.store.load()['operations']['one']['status'],'uncertain')
            finally:first.channel.close()
            second=Adapter(effect)
            try:Executor(p,root/'run',second,lock_dir=root/'locks').run(allow_write=True)
            finally:second.channel.close()
            self.assertEqual(effect.with_suffix('.calls').read_text().splitlines(),['one','two'])
