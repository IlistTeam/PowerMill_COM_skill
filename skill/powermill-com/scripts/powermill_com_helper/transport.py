"""Isolated JSON-lines worker with an external deadline; no eager COM connection."""
from pathlib import Path
import json,math,os,queue,subprocess,threading,time,uuid
from .core import HelperError,validate_read_expression
from .timing import timed,worker_sample

class TransportTimeout(HelperError):pass
class ProtocolError(HelperError):pass

class JsonLineProcess:
    def __init__(self,command):
        self.command=command;self.process=None;self.responses=queue.Queue();self.poisoned=False;self.closed=False;self.request_lock=threading.Lock()
    @timed('transport.start')
    def _start(self):
        if self.process is not None:return
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        self.process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,encoding='utf-8',bufsize=1,creationflags=flags)
        def reader():
            try:
                for line in self.process.stdout:
                    try:self.responses.put(json.loads(line.lstrip('\ufeff')))
                    except (ValueError,TypeError):self.responses.put({'protocol_error':'Worker emitted non-JSON output'})
            except (OSError,ValueError):pass
            finally:self.responses.put({'protocol_error':'Worker channel closed'})
        threading.Thread(target=reader,daemon=True).start()
    def request(self,method,payload,timeout_s):
        if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or timeout_s<=0:raise ProtocolError('Positive finite deadline required')
        with self.request_lock:return self._request(method,payload,timeout_s)
    @timed('transport.roundtrip')
    def _request(self,method,payload,timeout_s):
        if self.closed:raise ProtocolError('Worker channel is closed; create a new transport and reconcile')
        if self.poisoned:raise ProtocolError('Worker is quarantined; create a new channel and reconcile first')
        deadline=time.monotonic()+timeout_s;self._start();request_id=str(uuid.uuid4())
        sent=queue.Queue()
        def writer():
            try:
                self.process.stdin.write(json.dumps({'id':request_id,'method':method,'payload':payload},ensure_ascii=False)+'\n');self.process.stdin.flush();sent.put(None)
            except (OSError,ValueError) as exc:sent.put(exc)
        threading.Thread(target=writer,daemon=True).start()
        try:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise queue.Empty
            error=sent.get(timeout=remaining)
            if error:raise error
            remaining=deadline-time.monotonic()
            if remaining<=0:raise queue.Empty
            response=self.responses.get(timeout=remaining)
        except queue.Empty as exc:
            self.poisoned=True;self.close()
            raise TransportTimeout('Worker deadline exceeded. PowerMill outcome is unknown; only the helper worker was stopped.') from exc
        except (OSError,ValueError) as exc:
            self.poisoned=True;self.close();raise ProtocolError('Worker connection lost') from exc
        if response.get('id')!=request_id:
            self.poisoned=True;self.close();raise ProtocolError(response.get('protocol_error','Unexpected response ID'))
        worker_sample(method,response.get('profile'))
        if not response.get('ok'):
            # execute returns its own dispatched flag so the executor can distinguish
            # a server-side guard rejection from an uncertain native operation.
            if method=='execute':return {'dispatched':response.get('dispatched',True),'code':-1,'output':response.get('error','Worker error'),'error':response.get('error','Worker error')}
            raise HelperError(response.get('error','Worker error'))
        return response['result']
    @timed('transport.close')
    def close(self):
        self.closed=True
        process=self.process
        if process is None:return
        if process.poll() is None:
            # This is precisely the child we started, never the PowerMill process.
            process.terminate()
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
        for stream in (process.stdin,process.stdout):
            if stream:
                try:stream.close()
                except OSError:pass

class PowerMillTransport:
    def __init__(self,*,connect=False,read_timeout_s=60,powershell=None,process_id=None,profile=False,session_model_read='xml'):
        if not connect:raise HelperError('Live COM requires an explicit connect=True')
        if os.name!='nt':raise HelperError('Direct PowerMill COM requires Windows')
        if type(read_timeout_s) not in (int,float) or not math.isfinite(read_timeout_s) or read_timeout_s<=0:raise HelperError('Positive finite read deadline required')
        if process_id is not None and (type(process_id) is not int or process_id<=0):raise HelperError('Positive integer target PID required')
        if session_model_read not in ('xml','com'):raise HelperError('Session model reader must be xml or com')
        import base64
        script=Path(__file__).with_name('worker.ps1').read_text(encoding='utf-8')
        if process_id is not None:script=script.replace('$targetProcessId=0','$targetProcessId='+str(process_id),1)
        if profile:script=script.replace('$profileEnabled=$false','$profileEnabled=$true',1)
        if session_model_read=='com':script=script.replace("$sessionModelReadMode='xml'","$sessionModelReadMode='com'",1)
        encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
        executable=powershell or str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe')
        self.channel=JsonLineProcess([executable,'-NoLogo','-NoProfile','-NonInteractive','-STA','-EncodedCommand',encoded])
        self.read_timeout_s=read_timeout_s
        self.process_id=process_id
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
    def _guard_target(self,guard):
        if self.process_id is not None and guard.get('process_id')!=self.process_id:raise HelperError('Guard PID differs from the pinned transport PID')
    def session(self):
        result=self.channel.request('session',{},self.read_timeout_s)
        if self.process_id is not None and result.get('process_id')!=self.process_id:
            self.channel.poisoned=True;self.close();raise ProtocolError('Bound PowerMill PID differs from requested PID')
        return result
    def probe(self,guard):
        self._guard_target(guard);return self.channel.request('probe',{'guard':guard},self.read_timeout_s)
    def read_xml(self,expressions,guard):
        self._guard_target(guard)
        expressions=[validate_read_expression(expression) for expression in expressions]
        result=self.channel.request('read_xml',{'guard':guard,'expressions':expressions},self.read_timeout_s)
        if not isinstance(result,list) or len(result)!=len(expressions):
            self.channel.poisoned=True;self.close();raise ProtocolError('Incomplete XML response batch')
        return result
    def execute(self,op,guard):
        self._guard_target(guard);return self.channel.request('execute',{'guard':guard,'operation':op},op['timeout_s'])
    def close(self):self.channel.close()
