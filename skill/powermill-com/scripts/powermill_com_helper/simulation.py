"""Incremental, read-only RobSim audit. No COM calls and no record rewriting.

The caller supplies a reviewed kinematic contract, not a cell-specific recipe.
Live collision settings, machine identity and source preservation remain plan
postconditions; a recording's NONE flags alone cannot establish those facts.
"""
from array import array
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import copy,hashlib,json,math,re,xml.etree.ElementTree as ET
from .core import HelperError,StateDrift,atomic_json,digest
from .timing import timed,span

@timed('hash.file')
def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def number(x):
    if isinstance(x,bool):raise HelperError('Boolean is not a simulation coordinate')
    try:value=float(x)
    except (TypeError,ValueError) as exc:raise HelperError('Invalid simulation number') from exc
    if not math.isfinite(value):raise HelperError('Nonfinite simulation value')
    return value

def vector(x):
    if not isinstance(x,(list,tuple)) or len(x)!=3:raise HelperError('Three coordinates required')
    return tuple(number(v) for v in x)

def add(a,b):return tuple(x+y for x,y in zip(a,b))
def sub(a,b):return tuple(x-y for x,y in zip(a,b))
def scale(a,k):return tuple(x*k for x in a)
def dot(a,b):return sum(x*y for x,y in zip(a,b))
def cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def norm(a):return math.sqrt(dot(a,a))
def unit(a):
    length=norm(a)
    if length<1e-12:raise HelperError('Zero simulation direction')
    return scale(a,1/length)
def angle(a,b):
    a,b=unit(a),unit(b)
    return math.degrees(math.atan2(norm(cross(a,b)),max(-1.,min(1.,dot(a,b)))))

@dataclass(frozen=True)
class Record:
    axes:tuple
    pose:tuple
    fields:dict
    time:object

@timed('recording.parse')
def parse_record(line,addresses,*,reference=False):
    pairs=re.findall(r'\b([A-Za-z_]\w*)=(\S+)',line);fields=dict(pairs)
    if len(pairs)!=len(fields):raise HelperError('Repeated RobSim field')
    values=re.findall(r'(?<![\w])([A-Z])([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)',line)
    axes={key:number(value) for key,value in values}
    if not reference and (len(values)!=len(axes) or set(axes)!=set(addresses)):raise HelperError('Missing, repeated or unexpected RobSim axis')
    match=re.search(r'\(([^()]*)\)',line)
    if not match:raise HelperError('RobSim pose missing')
    pose=tuple(number(x) for x in match[1].split(','))
    if len(pose)!=9:raise HelperError('RobSim pose must contain XYZ, Z and X')
    if abs(norm(pose[3:6])-1)>.001 or not reference and abs(norm(pose[6:9])-1)>.001:raise HelperError('RobSim direction is not a unit vector')
    return Record(() if reference else tuple(axes[a] for a in addresses),pose,fields,number(fields['TIME']) if 'TIME' in fields else None)

@timed('recording.signature')
def signature(record,modal):
    for key in ('TOOL','FEED','SPINDLESPEED'):
        if key in record.fields:modal[key]=number(record.fields[key])
    return digest({'modal':modal,'moves':{k:record.fields[k] for k in ('MOVETYPE','COMPTYPE') if k in record.fields},
                   'pp':{k:v for k,v in record.fields.items() if k.startswith('udp_')},'timed':record.time is not None})

class Kinematics:
    def __init__(self,contract):
        if contract.get('schema')!=1:raise HelperError('Unsupported simulation contract schema')
        self.contract=copy.deepcopy(contract);self.axes={}
        for item in contract['axes']:
            a=item['address']
            if not re.fullmatch('[A-Z]',a) or a in self.axes:raise HelperError('Unique single-letter axes required')
            lo,hi=map(number,item['limits'])
            if lo>hi or item['type'] not in ('rotary','linear'):raise HelperError('Invalid kinematic axis')
            self.axes[a]={**item,'origin':vector(item['origin']),'direction':unit(vector(item['direction'])),'limits':(lo,hi)}
        self.addresses=tuple(self.axes)
        if not self.addresses:raise HelperError('At least one kinematic axis is required')
        self.robot=contract['robot_chain'];self.table=contract['table_chain']
        for chain in (self.robot,self.table,contract['region']['undo_axes']):
            if len(chain)!=len(set(chain)) or any(a not in self.axes for a in chain):raise HelperError('Invalid kinematic chain')
        if set(self.robot)&set(self.table):raise HelperError('Robot and table chains must be disjoint')
        if set(self.robot)|set(self.table)!=set(self.axes):raise HelperError('Every recorded axis needs a kinematic role')
        self.point=vector(contract['head']['tcp']);self.x=unit(vector(contract['head']['x']));self.z=unit(vector(contract['head']['z']))
        if abs(dot(self.x,self.z))>1e-8:raise HelperError('Head axes are not orthogonal')
        self.origin=vector(contract['source_frame']['origin']);self.basis=tuple(vector(v) for v in contract['source_frame']['basis'])
        if any(abs(dot(a,b)-(1 if i==j else 0))>1e-8 for i,a in enumerate(self.basis) for j,b in enumerate(self.basis)) or dot(cross(self.basis[0],self.basis[1]),self.basis[2])<.999999:raise HelperError('Source frame is not a rigid right-handed basis')
        self.reference=unit(vector(contract['orientation']['reference_X']));self.max_x=number(contract['orientation']['max_X_degrees'])
        if not 0<=self.max_x<=180:raise HelperError('Invalid X angular limit')
        region=contract['region'];self.region_origin=vector(region['origin']);self.region_min=tuple(map(number,region['min']));self.region_max=tuple(map(number,region['max']))
        if len(self.region_min)!=2 or len(self.region_max)!=2 or any(a>b for a,b in zip(self.region_min,self.region_max)):raise HelperError('Invalid XY working region')
        self.tol={'source_xyz_mm':.01,'source_z_degrees':.01,'fk_xyz_mm':.01,'fk_direction_degrees':.01,'joint':.0001,**contract.get('tolerances',{})}
        if set(self.tol)!=set(['source_xyz_mm','source_z_degrees','fk_xyz_mm','fk_direction_degrees','joint']) or any(number(v)<0 for v in self.tol.values()):raise HelperError('Invalid simulation tolerances')
        self.tol={k:number(v) for k,v in self.tol.items()};self.fixed={a:number(v) for a,v in contract.get('fixed_axes',{}).items()}
        if any(a not in self.axes for a in self.fixed):raise HelperError('Unknown fixed axis')
        self.travel_axis=contract.get('travel_axis')
        if self.travel_axis is not None and (self.travel_axis not in self.axes or self.axes[self.travel_axis]['type']!='rotary'):raise HelperError('Travel axis must be rotary')
        self.min_travel=number(contract.get('min_travel_degrees',0))
        if self.min_travel<0 or (self.min_travel and self.travel_axis is None):raise HelperError('Invalid rotary travel requirement')
        self.motion_regions={}
        names=set()
        regions=contract.get('motion_regions',[])
        if not isinstance(regions,list) or 'motion_regions' in contract and not regions:raise HelperError('Explicit motion regions must be a nonempty list')
        for region in regions:
            if not isinstance(region,dict):raise HelperError('Motion region must be an object')
            name=region.get('name');moves=region.get('movetypes');lo=region.get('min');hi=region.get('max')
            if not isinstance(name,str) or not name or name in names:raise HelperError('Unique named motion regions required')
            if not isinstance(moves,list) or not moves or not isinstance(lo,list) or not isinstance(hi,list) or len(lo)!=2 or len(hi)!=2:raise HelperError('Invalid motion region')
            lo=tuple(map(number,lo));hi=tuple(map(number,hi));names.add(name)
            if any(a>b or a<base_lo or b>base_hi for a,b,base_lo,base_hi in zip(lo,hi,self.region_min,self.region_max)):raise HelperError('Motion region must be inside the all-moves region')
            for move in moves:
                if not isinstance(move,str) or not re.fullmatch('[1-9][0-9]*',move) or move in self.motion_regions:raise HelperError('Unique explicit MOVETYPE classification required')
                self.motion_regions[move]=(name,lo,hi)
    def transform(self,q,chain,values,*,vector_only=False,inverse=False):
        for name in (chain if inverse else reversed(chain)):
            axis=self.axes[name];value=values[name]*(-1 if inverse else 1);n=axis['direction']
            if axis['type']=='linear':
                if not vector_only:q=add(q,scale(n,value))
                continue
            o=(0.,0.,0.) if vector_only else axis['origin'];d=sub(q,o);a=math.radians(value);c,s=math.cos(a),math.sin(a)
            q=add(o,add(add(scale(d,c),scale(cross(n,d),s)),scale(n,(1-c)*dot(n,d))))
        return q
    def frame(self,q,vector_only=False):
        result=tuple(dot(row,q) for row in self.basis)
        return result if vector_only else add(self.origin,result)
    @timed('recording.pose')
    def verify(self,record,source):
        values=dict(zip(self.addresses,record.axes));tol=self.tol
        for name,value in values.items():
            lo,hi=self.axes[name]['limits']
            if value<lo-tol['joint'] or value>hi+tol['joint']:raise HelperError('Joint limit violation: '+name)
        if any(abs(values[a]-v)>tol['joint'] for a,v in self.fixed.items()):raise HelperError('Fixed axis moved')
        tcp=self.transform(self.point,self.robot,values);xt=self.transform(self.x,self.robot,values,vector_only=True);zt=self.transform(self.z,self.robot,values,vector_only=True)
        target=self.transform(self.frame(source[:3]),self.table,values);target_z=self.transform(self.frame(source[3:],True),self.table,values,vector_only=True);record_x=self.transform(self.frame(record.pose[6:],True),self.table,values,vector_only=True)
        result={'source_position_mm':norm(sub(record.pose[:3],source[:3])),'source_Z_degrees':angle(record.pose[3:6],source[3:]),'FK_position_mm':norm(sub(tcp,target)),
                'programmed_axis_degrees':angle(zt,target_z),'recorded_X_degrees':angle(xt,record_x),'X_degrees':angle(xt,self.reference)}
        limits={'source_position_mm':tol['source_xyz_mm'],'source_Z_degrees':tol['source_z_degrees'],'FK_position_mm':tol['fk_xyz_mm'],
                'programmed_axis_degrees':tol['fk_direction_degrees'],'recorded_X_degrees':tol['fk_direction_degrees'],'X_degrees':self.max_x}
        if any(result[k]>limit for k,limit in limits.items()):raise HelperError('Simulation pose violates contract: '+json.dumps(result))
        relative=sub(self.transform(tcp,self.contract['region']['undo_axes'],values,inverse=True),self.region_origin)
        if any(v<lo or v>hi for v,lo,hi in zip(relative[:2],self.region_min,self.region_max)):raise HelperError('TCP outside working region')
        return result,relative[:2]

class SimulationAudit:
    """Parse each new native record once; rehash checked bytes to detect rewrites.

    Checkpoint integrity binds source, recording, contract and checked prefix.
    On an error this object is poisoned; reconstruct from the last checkpoint.
    """
    @timed('recording.load_source')
    def __init__(self,source_path,source_sha256,recording_path,contract,*,checkpoint=None):
        self.source=Path(source_path).resolve();self.native=Path(recording_path).resolve();self.checkpoint=Path(checkpoint).resolve() if checkpoint else None
        if self.native==self.source or self.checkpoint in (self.source,self.native):raise HelperError('Audit inputs and checkpoint must be different files')
        if not re.fullmatch('[0-9a-f]{64}',source_sha256) or sha256_file(self.source)!=source_sha256:raise StateDrift('Simulation source hash changed')
        self.source_sha=source_sha256;self.kin=Kinematics(contract);self.key=digest({'source':str(self.source),'source_sha':source_sha256,'recording':str(self.native),'contract':contract})
        self.xyz_z=array('d');self.signatures=[];modal={}
        source_end=False
        with self.source.open('r',encoding='utf-8-sig') as stream:
            for line in stream:
                line=line.strip()
                if line=='(End)':
                    if source_end:raise HelperError('Repeated source End marker')
                    source_end=True;continue
                if not line or line.startswith('('):continue
                if source_end:raise HelperError('Source records follow End marker')
                record=parse_record(line,self.kin.addresses,reference=True);self.xyz_z.extend(record.pose[:6]);self.signatures.append(signature(record,modal))
        if not self.signatures or not source_end:raise HelperError('A nonempty, completed reference simulation is required')
        self.state={'key':self.key,'offset':0,'prefix_sha256':hashlib.sha256().hexdigest(),'identity':None,'records':0,'raw_records':0,'lines':0,'end':False,
                    'modal':{},'last':None,'last_time':None,'duplicates':[],'max_errors':{},'XY_min':None,'XY_max':None,'joint_min':{},'joint_max':{},'travel_degrees':0.,'wrap_count':0}
        if self.kin.motion_regions:self.state.update(motion=None,motion_region_counts={},motion_region_XY={})
        if self.checkpoint and self.checkpoint.exists():
            saved=json.loads(self.checkpoint.read_text('utf-8'));integrity=saved.pop('integrity',None)
            if integrity!=digest(saved) or saved.get('key')!=self.key:raise StateDrift('Simulation audit checkpoint changed')
            self.state=saved
            if type(saved.get('records')) is not int or not 0<=saved['records']<=len(self.signatures) or type(saved.get('offset')) is not int or saved['offset']<0:raise StateDrift('Invalid simulation audit cursor')
            if self.kin.motion_regions:
                counts=saved.get('motion_region_counts',{});regions=saved.get('motion_region_XY',{})
                if 'motion_region_counts' not in saved or 'motion_region_XY' not in saved or 'motion' not in saved or not isinstance(counts,dict) or not isinstance(regions,dict) or set(counts)!=set(regions) or any(type(v) is not int or v<0 for v in counts.values()) or sum(counts.values())!=saved['records'] or saved['records'] and saved.get('motion') not in self.kin.motion_regions:raise StateDrift('Invalid motion-region checkpoint')
        self.poisoned=False
    def _source(self,index):return tuple(self.xyz_z[index*6:index*6+6])
    def _consume(self,record):
        s=self.state;i=s['records'];modal=copy.deepcopy(s['modal']);sig=signature(record,modal)
        if record.fields.get('COLLISION_STATE')!='NONE':raise HelperError('Native collision state missing or not NONE')
        match=i<len(self.signatures) and sig==self.signatures[i] and norm(sub(record.pose[:3],self._source(i)[:3]))<=self.kin.tol['source_xyz_mm'] and angle(record.pose[3:6],self._source(i)[3:])<=self.kin.tol['source_z_degrees']
        if not match:
            last=s['last'];boundary='startup' if i==1 else 'terminal' if i==len(self.signatures) else None
            if not boundary or last is None or list(record.axes)!=last['axes'] or list(record.pose)!=last['pose'] or record.time!=last['time'] or modal!=s['modal'] or any(k in record.fields for k in ('MOVETYPE','COMPTYPE','GEOMETRY')) or any(k.startswith('udp_') for k in record.fields):raise HelperError('Native source sequence / movement / point parameters differ at record '+str(i+1))
            if len(s['duplicates'])>=16:raise HelperError('Too many boundary state duplicates')
            s['duplicates'].append({'line':s['lines'],'boundary':boundary,'proof':'identical joints, pose, time and modal values; no movement or point parameters; no source point consumed'})
        else:
            errors,xy=self.kin.verify(record,self._source(i))
            if self.kin.motion_regions:
                with span('recording.motion_region'):
                    move=record.fields.get('MOVETYPE',s.get('motion'))
                    if move not in self.kin.motion_regions:raise HelperError('Unclassified MOVETYPE: '+str(move))
                    name,lo,hi=self.kin.motion_regions[move]
                    if any(v<a or v>b for v,a,b in zip(xy,lo,hi)):raise HelperError('TCP outside motion region: '+name)
                    s['motion']=move;s['motion_region_counts'][name]=s['motion_region_counts'].get(name,0)+1
                    bounds=s['motion_region_XY'].setdefault(name,{'min':list(xy),'max':list(xy)})
                    bounds['min']=[min(a,b) for a,b in zip(bounds['min'],xy)];bounds['max']=[max(a,b) for a,b in zip(bounds['max'],xy)]
            if record.time is not None:
                if s['last_time'] is not None and record.time<s['last_time']:raise HelperError('Native simulation clock moved backwards')
                s['last_time']=record.time
            for k,value in errors.items():s['max_errors'][k]=max(s['max_errors'].get(k,0),value)
            s['XY_min']=[min(a,b) for a,b in zip(s['XY_min'],xy)] if s['XY_min'] else list(xy);s['XY_max']=[max(a,b) for a,b in zip(s['XY_max'],xy)] if s['XY_max'] else list(xy)
            for a,value in zip(self.kin.addresses,record.axes):s['joint_min'][a]=min(s['joint_min'].get(a,value),value);s['joint_max'][a]=max(s['joint_max'].get(a,value),value)
            if self.kin.travel_axis and s['last']:
                j=self.kin.addresses.index(self.kin.travel_axis);step=record.axes[j]-s['last']['axes'][j];s['travel_degrees']+=abs((step+180)%360-180);s['wrap_count']+=int(abs(step)>180)
            s['records']+=1;s['modal']=modal;s['last']={'axes':list(record.axes),'pose':list(record.pose),'time':record.time}
        s['raw_records']+=1
    @timed('recording.poll')
    def poll(self,*,complete=False,min_records=0):
        if self.poisoned:raise StateDrift('Simulation audit is poisoned; reconcile from the last checkpoint')
        if type(complete) is not bool or type(min_records) is not int or min_records<0:raise HelperError('Invalid recording completion requirement')
        try:
            if sha256_file(self.source)!=self.source_sha:raise StateDrift('Simulation source changed during audit')
            stat=self.native.stat();identity=[stat.st_dev,stat.st_ino];s=self.state
            if s['identity'] is not None and identity!=s['identity']:raise StateDrift('Native recording was replaced')
            if stat.st_size<s['offset']:raise StateDrift('Native recording was truncated')
            s['identity']=identity;h=hashlib.sha256()
            with self.native.open('rb') as stream:
                remaining=s['offset']
                while remaining:
                    block=stream.read(min(1024*1024,remaining))
                    if not block:raise StateDrift('Native checked prefix disappeared')
                    h.update(block);remaining-=len(block)
                if h.hexdigest()!=s['prefix_sha256']:raise StateDrift('Native checked prefix was rewritten')
                while True:
                    line=stream.readline(65537)
                    if len(line)>65536:raise HelperError('Oversized RobSim record')
                    if not line:break
                    if not line.endswith(b'\n'):
                        if complete:raise HelperError('Incomplete terminal RobSim line')
                        break
                    value=line.decode('utf-8-sig' if s['offset']==0 else 'utf-8').strip();s['lines']+=1
                    if value=='(End)':
                        if s['end']:raise HelperError('Repeated RobSim End marker')
                        s['end']=True
                    elif value and not value.startswith('('):
                        if s['end']:raise HelperError('Native records follow End marker')
                        self._consume(parse_record(value,self.kin.addresses))
                    h.update(line);s['offset']+=len(line)
                s['prefix_sha256']=h.hexdigest()
            if s['records']<min_records:raise HelperError('Not enough verified source records')
            if complete and (not s['end'] or s['records']!=len(self.signatures) or s['travel_degrees']<self.kin.min_travel):raise HelperError('Full simulation completion not proven')
            if self.checkpoint:atomic_json(self.checkpoint,{**s,'integrity':digest(s)})
            return {**copy.deepcopy(s),'source_records':len(self.signatures),'complete':s['end'] and s['records']==len(self.signatures),
                    'scope':'Source sequence, PP, joint limits, supplied FK, programmed axis, X, region and native collision flags. Live machine identity/settings and dynamics are separate checks.'}
        except BaseException:
            self.poisoned=True;raise

@timed('recording.native_issues')
def verify_native_issues(transport,guard):
    values=transport.read_xml(['simulation_issues()'],guard)
    if not isinstance(values,list) or len(values)!=1:raise HelperError('Missing native simulation issues')
    try:root=ET.fromstring(values[0])
    except ET.ParseError as exc:raise HelperError('Invalid native simulation issues XML') from exc
    if root.tag!='array' or root.text and root.text.strip():raise HelperError('Native issues must be an array')
    types=Counter();severity=Counter();ignored=0
    for item in root:
        kind=item.findtext('Type');level=item.findtext('Severity');flag=item.findtext('Ignored')
        if item.tag!='map' or not kind or level not in ('error','warning','info','information') or flag not in ('0','1'):raise HelperError('Incomplete native simulation issue')
        if level=='error' or kind in ('collision','limit','unsolvable'):raise HelperError('Native simulation issue blocks recording: '+ET.tostring(item,encoding='unicode'))
        types[kind]+=1;severity[level]+=1;ignored+=int(flag)
    return {'count':len(root),'types':dict(types),'severity':dict(severity),'ignored':ignored}

def validate_recording_check(check):
    for key in ('source','contract'):
        value=check.get(key,{})
        if not Path(value.get('path','')).is_absolute() or not re.fullmatch('[0-9a-f]{64}',value.get('sha256','')):raise HelperError('Recording inputs require pinned absolute paths')
    if not Path(check.get('recording_path','')).is_absolute() or type(check.get('complete',False)) is not bool or type(check.get('min_records',0)) is not int or check.get('min_records',0)<0:raise HelperError('Invalid recording audit check')

def make_audit(check,checkpoint):
    validate_recording_check(check);contract=Path(check['contract']['path'])
    if sha256_file(contract)!=check['contract']['sha256']:raise StateDrift('Kinematic contract changed')
    if Path(checkpoint).resolve()==contract.resolve():raise HelperError('Checkpoint must not replace kinematic contract')
    return SimulationAudit(check['source']['path'],check['source']['sha256'],check['recording_path'],json.loads(contract.read_text('utf-8-sig')),checkpoint=checkpoint)

@timed('check.machine_fingerprint')
def machine_fingerprint(raw):
    """Static pivots, mount frames, model references and collision exclusions.

    Current joint values / min-max history / solver priorities are intentionally
    excluded; their actual values are checked per recorded pose by the contract.
    """
    root=ET.fromstring(raw);axes={}
    def scalar(node,key):return number(node.findtext(key).replace(',','.'))
    for node in root.iter('Axis'):
        address=node.findtext('ControlInformation/Address')
        if not address or address in axes:raise HelperError('Invalid live machine axis identity')
        axes[address]={key:scalar(node,key) for key in 'XYZIJK'}
        axes[address]['type']=node.findtext('Type')
    frames={}
    for tag in ('HeadAttachPoint','TableAttachPoint'):
        node=root.find(tag)
        if node is None:raise HelperError('Live machine mount frame missing')
        frames[tag]={key:scalar(node,key) for key in 'XYZIJKUVW' if node.find(key) is not None}
    exclusions=root.find('CollisionExclusionList')
    if not axes or exclusions is None:raise HelperError('Live machine geometry / collision exclusions missing')
    def hierarchy(node):
        if node.tag=='Axis':return {'axis':node.findtext('ControlInformation/Address')}
        return {'tag':node.tag,'attributes':dict(node.attrib),'text':(node.text or '').strip(),'children':[hierarchy(c) for c in node]}
    return digest({'name':root.findtext('MachineName'),'axes':axes,'frames':frames,
                   'parts':[hierarchy(node) for node in root.findall('MachinePart')],
                   'model_paths':sorted(node.text or '' for node in root.findall('.//ModelList//Path/File')),
                   'exclusions':ET.canonicalize(ET.tostring(exclusions,encoding='unicode'),strip_text=True)})

@timed('check.live_contract')
def verify_live_contract(contract,transport,guard,*,values=None):
    for key in ('machine_geometry_sha256','tool_xml_sha256'):
        if not re.fullmatch('[0-9a-f]{64}',contract.get(key,'')):raise HelperError('Live recording audit requires pinned machine geometry and tool definition')
    if values is None:values=transport.read_xml(["entity('machinetool','')","entity('tool','')",'simulationstate'],guard)
    if not isinstance(values,list) or len(values)!=3:raise HelperError('Incomplete live simulation contract response')
    if machine_fingerprint(values[0])!=contract['machine_geometry_sha256']:raise StateDrift('Machine geometry or collision exclusions changed')
    if hashlib.sha256(ET.canonicalize(values[1],strip_text=True).encode('utf-8')).hexdigest()!=contract['tool_xml_sha256']:raise StateDrift('Active tool definition changed')
    state=ET.fromstring(values[2])
    if any(state.findtext('Issues/Collisions/'+key)!='1' for key in ('CheckCollisions','CheckToolAndHolder')):raise HelperError('Live collision checks are not enabled')
