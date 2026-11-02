"""Fresh, phase-local settings observations; no COM calls at import time.

Only reviewed scalar expressions are mapped. Missing fields are explicitly
read through the original expression; malformed/ambiguous XML fails closed.
Snapshot values never survive an Executor._checks call or a non-XML barrier.
"""
import copy,re,xml.etree.ElementTree as ET
from collections import Counter
from .core import HelperError
from .timing import timed

MACHINE="entity('machinetool','')"
STATE='simulationstate'
TOOL="entity('tool','')"
STATE_PATHS={
    STATE+'.'+path:tuple(path.split('.')) for path in (
        'Issues.Collisions.CheckToolAndHolder','Issues.Collisions.CheckCollisions',
        'Issues.CheckReconfigurations','ModelLocation.Name',
        'ToolControl.ToolAxisConstraint.Mode','ToolControl.OrientationVector.Mode',
        'ToolControl.OrientationVector.Space','ToolControl.OrientationVector.AngularTolerance')}
for index in range(3):
    STATE_PATHS[STATE+'.ToolControl.OrientationVector.ReferenceVector[%d]'%index]=('ToolControl','OrientationVector','ReferenceVector',index)

def mapping(check):
    # XML hashes include tags: a <Min> subtree is not the scalar API's <real>.
    if check.get('compare','text') not in ('text','number'):return None
    expression=check['expression']
    if expression in STATE_PATHS:return STATE,None,STATE_PATHS[expression]
    match=re.fullmatch(r"machine_axis\('([A-Z])'\)\.ControlInformation\.(Min|Max|Priority)",expression)
    if match:return MACHINE,match[1],(match[2],)
    return None

def child(node,name):
    found=node.findall(name)
    if len(found)>1:raise HelperError('Ambiguous settings snapshot field: '+name)
    return found[0] if found else None

class SettingsObservation:
    def __init__(self,transport,guard):
        self.transport=transport;self.guard=copy.deepcopy(guard);self.pending={};self.metrics=Counter()
    def clear(self):self.pending.clear()
    def report(self):return dict(self.metrics)
    def _read(self,expressions):
        values=self.transport.read_xml(expressions,self.guard)
        if not isinstance(values,list) or len(values)!=len(expressions) or any(not isinstance(v,str) or not v.strip() for v in values):raise HelperError('Incomplete settings observation batch')
        self.metrics['xml_expressions']+=len(expressions)
        return values
    @staticmethod
    def _tree(expression,raw):
        try:root=ET.fromstring(raw)
        except ET.ParseError as exc:raise HelperError('Malformed settings snapshot XML') from exc
        allowed={'machinetool','map'} if expression==MACHINE else {STATE}
        if root.tag not in allowed:raise HelperError('Unexpected settings snapshot root')
        return root
    @staticmethod
    def _axes(root):
        axes={}
        for axis in root.iter('Axis'):
            info=child(axis,'ControlInformation');address=child(info,'Address') if info is not None else None
            if address is None or list(address) or not re.fullmatch('[A-Z]',address.text or '') or address.text in axes:raise HelperError('Ambiguous settings snapshot axis identity')
            axes[address.text]=info
        if not axes:raise HelperError('Settings snapshot has no axis identities')
        return axes
    def _extract(self,description,trees,axes):
        root,address,path=description
        if address is not None:
            # A missing axis cannot be hidden by reading a similarly named axis.
            if address not in axes:raise HelperError('Settings snapshot axis missing: '+address)
            node=axes[address]
        else:node=trees[root]
        for key in path:
            if node is None:return None
            if type(key) is int:
                values=list(node)
                if len(values)!=3 or any(v.tag!='real' for v in values):raise HelperError('Invalid snapshot reference-vector layout')
                node=values[key]
            else:node=child(node,key)
        if node is None:return None
        if list(node):raise HelperError('Snapshot scalar contains child elements')
        return ET.tostring(node,encoding='unicode')
    @timed('settings_snapshot.read_checks')
    def read_checks(self,checks,*,with_contract=False):
        # Each contiguous XML batch starts a new observation. Do not reuse an
        # earlier read even if another batch happens to use the same expressions.
        self.clear();descriptions=[mapping(c) for c in checks]
        counts=Counter(d[0] for d in descriptions if d)
        enabled={root for root,count in counts.items() if count>1 or with_contract}
        requests=[];positions=[]
        for check,description in zip(checks,descriptions):
            if description and description[0] in enabled:
                positions.append(None)
            else:
                positions.append(len(requests));requests.append(check['expression'])
        # Read the complete contract last, after all other scalar/source reads.
        # This is the same final observation used by the recording verifier,
        # rather than an earlier snapshot cached across later COM requests.
        roots=[MACHINE,TOOL,STATE] if with_contract else [r for r in (MACHINE,STATE) if r in enabled]
        root_start=len(requests);values=self._read(requests+roots)
        snapshots=dict(zip(roots,values[root_start:]));trees={root:self._tree(root,snapshots[root]) for root in enabled}
        axes=self._axes(trees[MACHINE]) if MACHINE in trees else {}
        result=[];fallback=[]
        for index,(check,description,position) in enumerate(zip(checks,descriptions,positions)):
            if position is not None:result.append(values[position]);continue
            value=self._extract(description,trees,axes)
            result.append(value)
            if value is None:fallback.append(index)
            else:self.metrics['mapped_fields']+=1
        if fallback:
            originals=self._read([checks[i]['expression'] for i in fallback])
            for index,value in zip(fallback,originals):result[index]=value
            self.metrics['direct_missing_fields']+=len(fallback)
            # A fallback is another live read: refresh the complete snapshots
            # afterwards. Reject shape drift, never mix an absent field with a
            # new schema or retain an old collision state for the contract.
            snapshots=dict(zip(roots,self._read(roots)))
            trees={root:self._tree(root,snapshots[root]) for root in enabled}
            axes=self._axes(trees[MACHINE]) if MACHINE in trees else {}
            for index,(description,position) in enumerate(zip(descriptions,positions)):
                if position is not None:continue
                value=self._extract(description,trees,axes)
                if (value is None)!=(index in fallback):raise HelperError('Settings snapshot field availability changed during observation')
                if value is not None:result[index]=value
            self.metrics['snapshot_refresh_reads']+=len(roots)
        self.pending=snapshots if with_contract else {}
        self.metrics['snapshot_reads']+=len(roots)
        return result
    @timed('settings_snapshot.contract_values')
    def take_contract_values(self):
        pending=self.pending.copy();self.clear()
        expressions=[MACHINE,TOOL,STATE];missing=[e for e in expressions if e not in pending]
        current=dict(zip(missing,self._read(missing))) if missing else {}
        self.metrics['contract_roots_reused']+=sum(e in pending for e in expressions)
        return [pending.get(e,current.get(e)) for e in expressions]
