from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, InvalidOperation
import copy,hashlib,json,math,os,re,tempfile,time,xml.etree.ElementTree as ET
from .timing import timed,span

class HelperError(RuntimeError):pass
class NeedsReconciliation(HelperError):pass
class NotReady(HelperError):pass
class StateDrift(HelperError):pass
class SessionLocked(HelperError):pass

def default_lock_dir():
    """One per-user queue shared by repository and installed helper copies."""
    return Path(tempfile.gettempdir())/'powermill-com-session-locks'

READ_FUNCTIONS=frozenset({'entity','entity_exists','folder','folder_exists','pathname','project_pathname','size','extract',
    'toolpath_component_count','segment_point_count','segment_get_point','segment_get_length','point_parameters',
    'segment_has_leadin','segment_has_leadout','abs','min','max','components','model_components_thicknessset',
    'smart_models','smart_levels','smart_colours','limits','explorer_selected_entities',
    'machine','machine_axis','simulation_location','simulation_issues',
    'tool_tip_position','tool_axis_direction','orientation_vector_direction',
    'number_selected','get_folders','active_folder','component_has_point_parameter'})

def validate_read_expression(expression):
    if not isinstance(expression,str) or not expression or any(c in expression for c in '\r\n\0;'):
        raise HelperError('Invalid read-only expression')
    stripped=re.sub(r"\"[^\"]*\"|'[^']*'",'',expression)
    if re.search(r'''['"]|(?<![=!<>])=(?!=)''',stripped):raise HelperError('Invalid read-only expression syntax')
    calls=re.findall(r'\b([A-Za-z_]\w*)\s*\(',stripped)
    if any(name.casefold() not in READ_FUNCTIONS for name in calls):raise HelperError('Expression function is not approved for read-only use')
    return expression

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()

def utcnow():return datetime.now(timezone.utc).isoformat()

@timed('io.atomic_json')
def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',encoding='utf-8',newline='\n') as f:
        with span('io.json_write'):json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
        with span('io.flush'):f.flush()
        with span('io.fsync'):os.fsync(f.fileno())
    with span('io.replace'):os.replace(tmp,path)

def native_error(code,output):
    return code!=0 or bool(re.search(r'(?im)unknown command|invalid command|unexpected input|ошибочная команда|ошибка макроса|break hit|выполнение прервано|macro aborted|(?:^|\n)\s*(?:ошибка|error)\s*:',output))

def root_context(output):
    tokens=set();header=False
    for line in output.splitlines():
        if re.fullmatch(r'[ \t]*Commands available[ \t]*:[ \t]*',line,re.I):header=True
        match=re.fullmatch(r'[ \t]*([A-Z_]+)(?:[ \t]+.*)?',line)
        if match:tokens.add(match[1])
    return header and {'ACTIVATE','CREATE','DEACTIVATE'}.issubset(tokens)

def validate_plan(plan):
    if not isinstance(plan,dict):raise HelperError('Plan must be a JSON object')
    if plan.get('schema')!=1:raise HelperError('Unsupported plan schema')
    if type(plan.get('settings_snapshots',True)) is not bool:raise HelperError('settings_snapshots must be boolean')
    guard=plan.get('guard',{})
    models=guard.get('model_ids')
    if guard.get('version') not in (2023000,2023003,2023101) or type(guard.get('process_id')) is not int or guard['process_id']<=0 or not isinstance(models,list) or not models or any(not isinstance(x,str) or not x for x in models) or len(models)!=len(set(x.casefold() for x in models)):
        raise HelperError('Explicit PowerMill 2023 PID and model IDs are required')
    if 'project_path' in guard and not isinstance(guard['project_path'],str):raise HelperError('Project pathname must be a string')
    receipts=guard.get('model_identity_receipts',[])
    if not isinstance(receipts,list):raise HelperError('Model identity receipts must be a list')
    receipt_paths=set();receipt_names=set()
    for receipt in receipts:
        if not isinstance(receipt,dict) or not isinstance(receipt.get('path'),str) or not Path(receipt['path']).is_absolute() or not isinstance(receipt.get('name'),str) or not receipt['name'] or re.search(r'''['"\r\n\0]''',receipt['name']):raise HelperError('Explicit absolute receipt and safe model name required')
        key=str(Path(receipt['path']).resolve()).casefold();name=receipt['name'].casefold()
        if key in receipt_paths or name in receipt_names:raise HelperError('Duplicate model identity receipt path or name')
        receipt_paths.add(key);receipt_names.add(name)
    if not isinstance(plan.get('operations'),list):raise HelperError('Operations must be a list')
    ids=[]
    for op in plan.get('operations',[]):
        ids.append(op.get('id'))
        if not isinstance(op.get('id'),str) or not op['id']:raise HelperError('Operation ID required')
        if type(op.get('timeout_s')) not in (int,float) or not math.isfinite(op['timeout_s']) or op['timeout_s']<=0:raise HelperError('Positive finite timeout required')
        if not op.get('postchecks'):raise HelperError('Every operation needs observed postconditions')
        cmd=op.get('command','')
        if not cmd or any(c in cmd for c in '\r\n\0'):raise HelperError('One complete native command or pinned macro per operation')
        if re.search(r'(?i)PROCESS\s+TOOLPATH\s+LEADS|\bCALCFORM\b',cmd):raise HelperError('Unsupported PowerMill 2023 command')
        if cmd.upper().startswith('MACRO '):
            artifact=op.get('artifact',{})
            if not artifact.get('path') or not Path(artifact['path']).is_absolute() or any(c in artifact['path'] for c in '\r\n\0"') or not re.fullmatch('[0-9a-f]{64}',artifact.get('sha256','')) or not op.get('completion_marker'):
                raise HelperError('Macro requires a pinned file hash and a completion marker')
            if cmd!='MACRO "'+artifact['path']+'"':raise HelperError('Macro command and pinned file differ')
        for check in op.get('prechecks',[])+op['postchecks']:
            if check.get('kind','xml') not in ('xml','order','receipt','surface','identity','recording'):raise HelperError('Unknown verification kind')
            if check.get('kind','xml')=='order':
                from .planner import validate_order_check
                validate_order_check(check)
            elif check.get('kind')=='receipt':
                if not Path(check.get('path','')).is_absolute() or not re.fullmatch('[0-9a-f]{32}',check.get('token','')):raise HelperError('Invalid completion receipt')
            elif check.get('kind')=='surface':
                from .surface import validate_surface_check
                validate_surface_check(check)
            elif check.get('kind')=='identity':
                if not Path(check.get('path','')).is_absolute():raise HelperError('Absolute identity receipt required')
                validate_read_expression(check.get('expression'))
            elif check.get('kind')=='recording':
                from .simulation import validate_recording_check
                validate_recording_check(check)
            else:
                if not check.get('expression') or check.get('compare','text') not in ('text','number','xml_sha256') or 'expected' not in check:
                    raise HelperError('Invalid read-only XML check')
                validate_read_expression(check['expression'])
        for artifact in op.get('dependencies',[]):
            if not Path(artifact.get('path','')).is_absolute() or any(c in artifact['path'] for c in '\r\n\0"') or not re.fullmatch('[0-9a-f]{64}',artifact.get('sha256','')):raise HelperError('Dependencies must be pinned absolute files')
    if len(ids)!=len(set(ids)):raise HelperError('Unique operation IDs required')
    digest(plan)
    return plan

@contextmanager
def file_lease(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    f=path.open('a+b');locked=False
    try:
        f.seek(0,2)
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            locked=True
        except OSError as exc:raise SessionLocked('Another helper owns this session/run') from exc
        yield
    finally:
        if locked:
            f.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(f.fileno(),fcntl.LOCK_UN)
        f.close()

class RunStore:
    @timed('store.init')
    def __init__(self,directory,plan):
        self.directory=Path(directory);self.plan=copy.deepcopy(validate_plan(plan));self.plan_hash=digest(self.plan)
        self.directory.mkdir(parents=True,exist_ok=True)
    @timed('store.load')
    def load(self):
        path=self.directory/'state.json'
        if path.exists():
            state=json.loads(path.read_text(encoding='utf-8'))
            payload={k:v for k,v in state.items() if k!='integrity'}
            if state.get('integrity')!=digest(payload):raise StateDrift('Checkpoint integrity check failed')
            if state.get('plan_hash')!=self.plan_hash:raise StateDrift('Plan changed; existing checkpoint cannot be reused')
            if set(state.get('operations',{}))!={o['id'] for o in self.plan['operations']}:raise StateDrift('Checkpoint operation set changed')
            tail=False;uncertain=0
            for op in self.plan['operations']:
                status=state['operations'][op['id']].get('status')
                if status not in ('pending','running','uncertain','completed'):raise StateDrift('Invalid checkpoint status')
                if status=='completed' and tail:raise StateDrift('Nonsequential checkpoint')
                if status!='completed':tail=True
                if status in ('running','uncertain'):uncertain+=1
            if uncertain>1:raise StateDrift('More than one uncertain operation')
            return state
        atomic_json(self.directory/'plan.json',self.plan)
        state={'schema':1,'plan_hash':self.plan_hash,'created_at':utcnow(),'operations':{o['id']:{'status':'pending','attempts':0} for o in self.plan['operations']}}
        self.save(state);return state
    @timed('store.save')
    def save(self,state):
        state['updated_at']=utcnow();state['integrity']=digest({k:v for k,v in state.items() if k!='integrity'});atomic_json(self.directory/'state.json',state)
    @timed('io.event_log')
    def log(self,event,**details):
        with (self.directory/'events.jsonl').open('a',encoding='utf-8') as f:
            with span('io.event_json_write'):f.write(json.dumps({'at':utcnow(),'event':event,**details},ensure_ascii=False,allow_nan=False)+'\n')
            with span('io.flush'):f.flush()
            with span('io.fsync'):os.fsync(f.fileno())

def assert_ready(snapshot,guard):
    if snapshot.get('process_id')!=guard['process_id'] or snapshot.get('version')!=guard['version']:
        raise NotReady('Process/version changed')
    if snapshot.get('busy'):raise NotReady('PowerMill is busy; no command dispatched')
    if sorted(str(x).casefold() for x in snapshot.get('model_ids',[]))!=sorted(str(x).casefold() for x in guard['model_ids']):
        raise NotReady('Project model identity changed')
    if 'project_path' in guard and snapshot.get('project_path','').replace('/','\\').casefold()!=guard['project_path'].replace('/','\\').casefold():
        raise NotReady('Project pathname changed')
    if not snapshot.get('root_ready'):raise NotReady('Parser is not at the root; no blind NEW/QUIT recovery')

@timed('check.xml_match')
def xml_match(raw,check):
    try:
        root=ET.fromstring(raw)
        mode=check.get('compare','text');expected=check['expected']
        if mode=='text':return ''.join(root.itertext())==str(expected)
        if mode=='number':
            actual=Decimal(''.join(root.itertext()).replace(',','.'));want=Decimal(str(expected).replace(',','.'));tol=Decimal(str(check.get('tolerance',0)))
            return actual.is_finite() and want.is_finite() and tol.is_finite() and tol>=0 and abs(actual-want)<=tol
        if mode=='xml_sha256':return hashlib.sha256(ET.canonicalize(raw,strip_text=True).encode('utf-8')).hexdigest()==expected
    except (ET.ParseError,InvalidOperation,ValueError,TypeError):return False
    return False

class Executor:
    """Transport is injected. Construction and local status do not connect to COM."""
    def __init__(self,plan,run_dir,transport,*,lock_dir=None):
        self.store=RunStore(run_dir,plan);self.plan=self.store.plan;self.transport=transport
        self._recording_audits={}
        self._live_recordings=set()
        self.lock_dir=Path(lock_dir) if lock_dir else default_lock_dir()
    @contextmanager
    def _locked(self):
        with file_lease(self.lock_dir/('pid-'+str(self.plan['guard']['process_id'])+'.lock')):
            with file_lease(self.store.directory/'run.lock'):yield
    def _guard(self):
        guard=copy.deepcopy(self.plan['guard']);pins_path=self.store.directory/'model-identities.json';pins={}
        if pins_path.exists():
            saved=json.loads(pins_path.read_text(encoding='utf-8'))
            if saved.get('plan_hash')!=self.store.plan_hash or saved.get('integrity')!=digest({k:v for k,v in saved.items() if k!='integrity'}) or not isinstance(saved.get('identities'),dict):raise StateDrift('Created-model identity checkpoint changed')
            pins=saved['identities']
        observed={};seen={x.casefold() for x in guard['model_ids']}
        for receipt in guard.get('model_identity_receipts',[]):
            path=Path(receipt['path']);key=str(path.resolve()).casefold()
            if not path.is_file():
                if key in pins:raise StateDrift('Created-model identity receipt disappeared')
                continue
            identity=path.read_text(encoding='utf-8-sig').strip().casefold()
            if not re.fullmatch('[0-9a-f]{32}',identity):raise StateDrift('Invalid created-model identity receipt')
            value={'id':identity,'name':receipt['name']}
            if key in pins and pins[key]!=value:raise StateDrift('Created-model identity receipt changed')
            if identity in seen:raise StateDrift('Created-model ID overlaps another protected identity')
            observed[key]=value;seen.add(identity);guard['model_ids'].append(identity)
        if set(pins)-set(observed):raise StateDrift('Created-model identity checkpoint lost a receipt')
        return guard,observed
    def _current_guard(self):return self._guard()[0]
    @timed('executor.ready')
    def _ready(self):
        start=time.perf_counter();guard,identities=self._guard();snapshot=self.transport.probe(guard);assert_ready(snapshot,guard)
        for value in identities.values():
            raw=self.transport.read_xml(["entity('model','"+value['name']+"').ID"],guard)[0]
            if ''.join(ET.fromstring(raw).itertext()).strip().casefold()!=value['id']:raise StateDrift('Created model name/ID mismatch')
        if identities:
            # Pin only after full project identity and every named live ID agree.
            saved={'plan_hash':self.store.plan_hash,'identities':identities};saved['integrity']=digest(saved)
            atomic_json(self.store.directory/'model-identities.json',saved)
        self.store.log('ready_verified',seconds=time.perf_counter()-start);return snapshot
    @timed('executor.prepare_recordings')
    def _prepare_recordings(self,checks,*,verify_current=False):
        from .simulation import make_audit,sha256_file,verify_live_contract
        for check in checks:
            if check.get('kind')!='recording':continue
            if sha256_file(check['contract']['path'])!=check['contract']['sha256']:raise StateDrift('Kinematic contract changed')
            key=digest({k:check[k] for k in ('source','contract','recording_path')})
            if key not in self._recording_audits:self._recording_audits[key]=make_audit(check,self.store.directory/('recording-'+key+'.json'))
            audit=self._recording_audits[key]
            for field in ('machine_geometry_sha256','tool_xml_sha256'):
                if not re.fullmatch('[0-9a-f]{64}',audit.kin.contract.get(field,'')):raise HelperError('Live recording audit requires pinned machine geometry and tool definition')
            if sha256_file(check['source']['path'])!=check['source']['sha256']:raise StateDrift('Simulation source changed')
            if verify_current and key in self._live_recordings:verify_live_contract(audit.kin.contract,self.transport,self._current_guard())
    @timed('executor.checks')
    def _checks(self,checks):
        start=time.perf_counter()
        from .settings_snapshot import SettingsObservation
        observation=SettingsObservation(self.transport,self._current_guard()) if self.plan.get('settings_snapshots',True) else None
        index=0
        while index<len(checks):
            check=checks[index]
            if check.get('kind','xml')=='xml':
                end=index
                while end<len(checks) and checks[end].get('kind','xml')=='xml':end+=1
                batch=checks[index:end]
                contract_next=end<len(checks) and checks[end].get('kind')=='recording'
                values=observation.read_checks(batch,with_contract=contract_next) if observation else self.transport.read_xml([item['expression'] for item in batch],self._current_guard())
                if not isinstance(values,list) or len(values)!=len(batch):raise HelperError('Incomplete XML verification batch')
                for item,raw in zip(batch,values):
                    if not xml_match(raw,item):
                        self.store.log('verification_failed',label=item.get('label',''),seconds=time.perf_counter()-start);return False
                index=end;continue
            if observation and check.get('kind')!='recording':observation.clear()
            if check.get('kind','xml')=='order':
                from .planner import check_observed_order
                ok=check_observed_order(check,self.transport,self._current_guard())
            elif check.get('kind')=='receipt':
                path=Path(check['path']);ok=path.is_file() and path.read_text(encoding='utf-8-sig').strip()==check['token']
            elif check.get('kind')=='surface':
                from .surface import verify_surface
                ok=verify_surface(check,self.transport,self._current_guard())
            elif check.get('kind')=='identity':
                path=Path(check['path']);expected=path.read_text(encoding='utf-8-sig').strip() if path.is_file() else ''
                ok=bool(expected) and xml_match(self.transport.read_xml([check['expression']],self._current_guard())[0],{'expected':expected})
            elif check.get('kind')=='recording':
                from .simulation import verify_live_contract,verify_native_issues
                self._prepare_recordings([check])
                key=digest({k:check[k] for k in ('source','contract','recording_path')})
                audit=self._recording_audits[key]
                values=observation.take_contract_values() if observation else None
                verify_live_contract(audit.kin.contract,self.transport,self._current_guard(),values=values)
                self._live_recordings.add(key)
                result=audit.poll(complete=check.get('complete',False),min_records=check.get('min_records',0))
                issues=verify_native_issues(self.transport,self._current_guard())
                self.store.log('native_issues_verified',**issues)
                self.store.log('recording_verified',records=result['records'],raw_records=result['raw_records'],complete=result['complete'],offset=result['offset'],live_contract_verified=True)
                ok=True
            else:
                raw=self.transport.read_xml([check['expression']],self._current_guard())[0];ok=xml_match(raw,check)
            if not ok:
                self.store.log('verification_failed',label=check.get('label',''),seconds=time.perf_counter()-start);return False
            index+=1
        self.store.log('verification_passed',checks=len(checks),seconds=time.perf_counter()-start,settings_snapshot=observation.report() if observation else {})
        return True
    def _reconcile(self,state,op):
        self._ready()
        entry=state['operations'][op['id']]
        if self._checks(op['postchecks']):
            entry.update(status='completed',reconciled_at=utcnow(),completion='observed_postconditions_after_interruption')
            self.store.save(state);self.store.log('reconciled_complete',operation=op['id']);return
        entry['status']='uncertain';self.store.save(state)
        self.store.log('reconcile_unresolved',operation=op['id'])
        raise NeedsReconciliation('Observed state is not the required result. Relative commands will not be replayed.')
    def reconcile(self):
        if self.plan.get('offline_only'):raise HelperError('This archived plan is offline-only')
        with self._locked():
            state=self.store.load()
            for op in self.plan['operations']:
                if state['operations'][op['id']]['status'] in ('running','uncertain'):self._reconcile(state,op)
            return state
    @timed('executor.run')
    def run(self,*,allow_write=False):
        if self.plan.get('offline_only'):raise HelperError('This archived plan is offline-only')
        if not allow_write:raise HelperError('Write execution is disabled; validate/plan/status are offline')
        with self._locked():
            state=self.store.load()
            uncertain=[o for o in self.plan['operations'] if state['operations'][o['id']]['status'] in ('running','uncertain')]
            for op in uncertain:self._reconcile(state,op)
            completed=[o for o in self.plan['operations'] if state['operations'][o['id']]['status']=='completed']
            # Check the latest cumulative checkpoint, not superseded earlier orders.
            if completed:
                self._ready()
                latest={o.get('checkpoint_key','project'):o for o in completed}
                for op in latest.values():
                    if not self._checks(op['postchecks']):raise StateDrift('Latest completed checkpoint no longer matches; no replay')
            for op in self.plan['operations']:
                entry=state['operations'][op['id']]
                if entry['status']=='completed':continue
                try:
                    self._ready()
                    self._prepare_recordings(op.get('prechecks',[])+op['postchecks'],verify_current=True)
                    if not self._checks(op.get('prechecks',[])):raise StateDrift('Preconditions do not match')
                    for check in op['postchecks']:
                        if check.get('kind')=='receipt' and Path(check['path']).exists():raise StateDrift('Receipt already exists; refusing to replay this macro in a new run')
                    artifact=op.get('artifact')
                    if artifact and hashlib.sha256(Path(artifact['path']).read_bytes()).hexdigest()!=artifact['sha256']:
                        raise StateDrift('Pinned macro file changed')
                    for dependency in op.get('dependencies',[]):
                        if hashlib.sha256(Path(dependency['path']).read_bytes()).hexdigest()!=dependency['sha256']:raise StateDrift('Pinned macro dependency changed')
                except Exception as exc:
                    state['last_block']={'operation':op['id'],'at':utcnow(),'reason':str(exc),'dispatched':False};self.store.save(state)
                    self.store.log('blocked_before_dispatch',operation=op['id'],reason=str(exc));raise
                entry.update(status='running',started_at=utcnow(),attempts=entry['attempts']+1)
                # Durable write-ahead checkpoint, BEFORE any native command.
                self.store.save(state);self.store.log('dispatch',operation=op['id'],command=op['command'])
                operation_start=time.perf_counter();result=None;phase='native'
                try:
                    with span('executor.native',operation=op['id']):result=self.transport.execute(op,self._current_guard())
                    phase='native_result'
                    result_name='native-result-'+hashlib.sha256(op['id'].encode('utf-8')).hexdigest()+'.json'
                    atomic_json(self.store.directory/result_name,{'operation_id':op['id'],'result':result})
                    entry['native_result_file']=result_name
                    self.store.log('native_returned',operation=op['id'],native_seconds=result.get('seconds'),code=result.get('code'))
                    if result.get('dispatched') is False:
                        entry.update(status='pending',last_error=result.get('error','Rejected before dispatch'))
                        self.store.save(state);raise NotReady(entry['last_error'])
                    if native_error(result.get('code',-1),result.get('output','')):raise HelperError('Native error: '+result.get('output',''))
                    marker=op.get('completion_marker')
                    if marker and marker not in result.get('output',''):raise HelperError('Macro completion marker missing')
                    phase='postchecks'
                    self._ready()
                    if not self._checks(op['postchecks']):raise HelperError('Observed postconditions failed')
                except BaseException as exc:
                    entry.update(operation_seconds=time.perf_counter()-operation_start,failure_phase=phase,native_seconds=result.get('seconds') if isinstance(result,dict) else None)
                    if entry['status']!='pending':entry.update(status='uncertain',last_error=str(exc))
                    self.store.save(state)
                    self.store.log('halt',operation=op['id'],status=entry['status'],reason=str(exc));raise
                entry.update(status='completed',completed_at=utcnow(),completion='native_return_and_observed_postconditions',native_seconds=result.get('seconds'))
                entry['operation_seconds']=time.perf_counter()-operation_start
                state.pop('last_block',None)
                self.store.save(state);self.store.log('completed',operation=op['id'],native_seconds=result.get('seconds'))
            return state
