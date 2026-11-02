"""Offline stable block planner; native MOVE_END only, no eager execution."""
from pathlib import Path
import hashlib,json,math,uuid,xml.etree.ElementTree as ET
import itertools
from .core import HelperError,atomic_json,digest,validate_plan,assert_ready,utcnow

def safe_name(value):
    if not isinstance(value,str) or not value or any(c in value for c in '\r\n\0\'"'):raise HelperError('Unsupported native entity name')
    return value

def finite(value):
    try:value=float(str(value).replace(',','.'))
    except (ValueError,TypeError):raise HelperError('Invalid numeric observation')
    if not math.isfinite(value):raise HelperError('Nonfinite numeric observation')
    return value

def validate_uids(values):
    if any(type(v) not in (str,int) for v in values) or len(values)!=len(set(values)):raise HelperError('Curve UIDs must be unique strings/integers')

def apply_move(current,start,end):
    if not 0<=start<=end<len(current):raise HelperError('Invalid relative move range')
    return current[:start]+current[end+1:]+current[start:end+1]

def plan_reorder(initial,target):
    validate_uids(initial);validate_uids(target)
    if set(initial)!=set(target):raise HelperError('Target is not a permutation of the same curves')
    current=list(initial);keep=0
    for uid in current:
        if keep<len(target) and uid==target[keep]:keep+=1
    at=keep;moves=[]
    while at<len(target):
        start=current.index(target[at]);count=1
        while at+count<len(target) and start+count<len(current) and current[start+count]==target[at+count]:count+=1
        end=start+count-1
        if end!=len(current)-1:
            moves.append([start,end]);current=apply_move(current,start,end)
        at+=count
    if current!=list(target):raise HelperError('Offline permutation simulation failed')
    return moves

def target_order(records,policy):
    """Use explicit per-task policy; never copy another section's LP/CNT assignments implicitly."""
    keys=policy.get('closed_keys',[]);open_keys=policy.get('open_keys',[])
    if not keys or not open_keys:raise HelperError('Explicit closed/open sort keys are required')
    validate_uids([r['uid'] for r in records])
    def key(row):
        if row.get('kind') not in ('closed','open'):raise HelperError('Unknown curve kind')
        fields=keys if row['kind']=='closed' else open_keys
        result=[finite(row['layer']),int(row['kind']=='open')]
        for field in fields:
            descending=field.startswith('-');value=finite(row[field[1:] if descending else field]);result.append(-value if descending else value)
        return tuple(result)
    return [r['uid'] for r in sorted(records,key=key)]

def validate_order_check(check):
    safe_name(check.get('name'));safe_name(check.get('entity_id'))
    catalog=check.get('catalog',[]);expected=check.get('expected',[])
    validate_uids(expected);validate_uids([r.get('uid') for r in catalog])
    if set(expected)!={r['uid'] for r in catalog}:raise HelperError('Order catalog and expected curve identities differ')
    tolerance=finite(check.get('tolerance_mm',1e-5))
    if tolerance<=0:raise HelperError('Positive identity tolerance required')
    geometry_only=check.get('identity_mode')=='geometry'
    if check.get('identity_mode','layer_lp') not in ('geometry','layer_lp'):raise HelperError('Unknown order identity mode')
    bins={}
    for i,row in enumerate(catalog):
        if len(row.get('position',[]))!=3:raise HelperError('A complete head position is required')
        pos=[finite(v) for v in row['position']]
        if geometry_only and 'head_pp' in row:raise HelperError('Geometry identity cannot verify head_pp; use a separate PP check')
        lp=finite(row['lp']) if not geometry_only else None
        layer=finite(row['layer']) if not geometry_only else None
        if 'axis' in row and (len(row['axis'])!=3 or not all(math.isfinite(finite(x)) for x in row['axis'])):raise HelperError('Invalid head axis')
        key=tuple(math.floor(v/(2*tolerance)) for v in pos)
        nearby=[other for delta in itertools.product((-1,0,1),repeat=3) for other in bins.get(tuple(k+d for k,d in zip(key,delta)),[])] if geometry_only else catalog[:i]
        for other in nearby:
            if (geometry_only or lp==finite(other['lp']) and layer==finite(other['layer'])) and math.dist(pos,other['position'])<=2*tolerance:
                raise HelperError('Ambiguous head/LP/layer identity; capture a stronger identity before sorting')
        bins.setdefault(key,[]).append(row)
    return check

def _vector(node):return [finite(c.text) for c in node]

def observe_order(check,transport,guard):
    validate_order_check(check)
    name=safe_name(check['name']);e="entity('toolpath','"+name+"')"
    raw=transport.read_xml([e+'.ID',"toolpath_component_count("+e+",'segments')"],guard)
    if ''.join(ET.fromstring(raw[0]).itertext())!=check['entity_id']:raise HelperError('Toolpath ID changed')
    if int(finite(''.join(ET.fromstring(raw[1]).itertext())))!=len(check['expected']):raise HelperError('Curve count changed')
    tolerance=check.get('tolerance_mm',1e-5);observed=[]
    geometry_only=check.get('identity_mode')=='geometry';bins={}
    if geometry_only:
        for row in check['catalog']:
            key=tuple(math.floor(finite(v)/(2*tolerance)) for v in row['position'])
            bins.setdefault(key,[]).append(row)
    for start in range(0,len(check['expected']),64):
        stop=min(len(check['expected']),start+64)
        geometry='{'+','.join(f'segment_get_point({e},{i},0)' for i in range(start,stop))+'}'
        parameters='{'+','.join(f'point_parameters({e},{i},0)' for i in range(start,stop))+'}'
        raw=transport.read_xml([geometry] if geometry_only else [geometry,parameters],guard)
        g=ET.fromstring(raw[0]).findall('map');p=[None]*len(g) if geometry_only else ET.fromstring(raw[1]).findall('map')
        if len(g)!=stop-start or len(p)!=stop-start:raise HelperError('Head response count changed')
        for offset,(actual,pars) in enumerate(zip(g,p)):
            position=actual.find('Position')
            if position is None:raise HelperError('Head geometry missing')
            pos=_vector(position)
            if geometry_only:
                key=tuple(math.floor(v/(2*tolerance)) for v in pos)
                candidates=[r for delta in itertools.product((-1,0,1),repeat=3) for r in bins.get(tuple(k+d for k,d in zip(key,delta)),[]) if math.dist(pos,r['position'])<=tolerance]
            else:
                candidates=[r for r in check['catalog'] if math.dist(pos,r['position'])<=tolerance and finite(pars.findtext('_LAYER'))==finite(r['layer']) and finite(pars.findtext('udp_LaserPower'))==finite(r['lp'])]
            if len(candidates)!=1:raise HelperError('Actual curve head is missing, changed or ambiguous')
            ref=candidates[0]
            if 'axis' in ref and math.dist(_vector(actual.find('ToolAxis')),ref['axis'])>1e-6:raise HelperError('Head axis changed')
            if 'head_pp' in ref and not geometry_only:
                values={n.tag:n.text for n in pars}
                if set(values)!=set(ref['head_pp']):raise HelperError('Head PP keys changed')
                for key,want in ref['head_pp'].items():
                    if isinstance(want,(int,float)):
                        if finite(values[key])!=finite(want):raise HelperError('Head PP value changed')
                    elif values[key]!=want:raise HelperError('Head PP value changed')
            observed.append(ref['uid'])
    validate_uids(observed)
    return observed

def check_observed_order(check,transport,guard):return observe_order(check,transport,guard)==check['expected']

def capture_sort_state(data,transport):
    """Read-only; caller must hold the shared session lease for live COM."""
    assert_ready(transport.probe(data['guard']),data['guard']);layers=[]
    for layer in data['layers']:
        check={'kind':'order','name':layer['name'],'entity_id':layer['entity_id'],'catalog':layer['catalog'],'expected':layer['initial'],'identity_mode':layer.get('identity_mode','layer_lp')}
        actual=observe_order(check,transport,data['guard'])
        layers.append({'entity_id':layer['entity_id'],'catalog_hash':digest(layer['catalog']),'actual':actual})
    assert_ready(transport.probe(data['guard']),data['guard'])
    return {'schema':1,'guard':data['guard'],'captured_at':utcnow(),'coverage':'curve heads, optional head axes/PP; not full geometry/PP audit','layers':layers}

def replan_bundle(data,observed,output,*,max_moves=64,enable_execution=False):
    if observed.get('schema')!=1 or digest(observed.get('guard'))!=digest(data['guard']):raise HelperError('Observed snapshot belongs to a different project/session')
    rows={x['entity_id']:x for x in observed['layers']}
    if len(rows)!=len(observed['layers']) or set(rows)!={x['entity_id'] for x in data['layers']}:raise HelperError('Observed layer set changed')
    layers=[]
    for layer in data['layers']:
        row=rows[layer['entity_id']]
        if row['catalog_hash']!=digest(layer['catalog']):raise HelperError('Observation catalog changed')
        if set(row['actual'])!=set(layer['target']):raise HelperError('Partial result lost or duplicated curves')
        validate_uids(row['actual']);layers.append({**layer,'initial':row['actual']})
    result=create_bundle(layers,data['guard'],output,max_moves=max_moves,enable_execution=enable_execution)
    plan_path=Path(output)/'plan.json';plan=json.loads(plan_path.read_text(encoding='utf-8'))
    plan['continuation']={'observed_sha256':digest(observed),'captured_at':observed.get('captured_at')}
    validate_plan(plan);atomic_json(plan_path,plan)
    atomic_json(Path(output)/'replanned-from.json',observed);return result

def create_bundle(layers,guard,output,*,max_moves=64,enable_execution=False):
    validate_plan({'schema':1,'guard':guard,'operations':[]})
    if type(max_moves) is not int or max_moves<=0:raise HelperError('Positive integer block size required')
    output=Path(output).resolve()
    if output.exists():raise HelperError('Use a new bundle directory; never overwrite dispatched macros/receipts')
    prepared=[];all_uids=[]
    for layer in layers:
        safe_name(layer['name']);safe_name(layer['entity_id'])
        check={'kind':'order','name':layer['name'],'entity_id':layer['entity_id'],'expected':layer['initial'],'catalog':layer['catalog'],'identity_mode':layer.get('identity_mode','layer_lp')}
        validate_order_check(check);moves=plan_reorder(layer['initial'],layer['target'])
        prepared.append((layer,moves));all_uids.extend(layer['initial'])
    validate_uids(all_uids)
    if len({x['entity_id'] for x in layers})!=len(layers):raise HelperError('Duplicate native toolpath ID')
    if len({x['name'] for x in layers})!=len(layers):raise HelperError('Duplicate native toolpath name')
    # Validate all inputs before creating any files. Offline artifacts only.
    output.mkdir(parents=True);(output/'macros').mkdir();(output/'receipts').mkdir()
    operations=[];snapshots=[]
    for index,(layer,moves) in enumerate(prepared):
        current=list(layer['initial'])
        for offset in range(0,len(moves),max_moves):
            opid=f'sort-{index:04d}-{offset//max_moves:04d}';before=list(current);block=moves[offset:offset+max_moves]
            lines=['function Main(){',f'ACTIVATE TOOLPATH "{layer["name"]}"','FORM TPLIST']
            for start,end in block:
                lines+=['EDIT TPSELECT ; TPLIST UPDATE',f'{start} NEW']
                if end!=start:lines+=['EDIT TPSELECT ; TPLIST UPDATE',f'{end} SERIES']
                lines+=['EDIT TOOLPATH REORDER MOVE_END'];current=apply_move(current,start,end)
            token=uuid.uuid4().hex;receipt=output/'receipts'/(opid+'.done');handle='pmreceipt_'+token[:12]
            marker='PMCOM COMPLETE '+opid+' '+token
            lines+=['FORM ACCEPT TPLIST',f'STRING receipt = "{token}"',f'FILE OPEN "{receipt}" FOR WRITE AS {handle}',
                    f'FILE WRITE $receipt TO {handle}',f'FILE CLOSE {handle}',f'PRINT "{marker}"','}']
            path=output/'macros'/(opid+'.mac');data=('\r\n'.join(lines)+'\r\n').encode('cp1251');path.write_bytes(data)
            coverage='all unique curve head XYZ and optional axis; PP not read' if layer.get('identity_mode')=='geometry' else 'all curve heads, layer and LP; optional head axis/PP; not a full geometry/PP audit'
            base={'kind':'order','name':layer['name'],'entity_id':layer['entity_id'],'catalog':layer['catalog'],'identity_mode':layer.get('identity_mode','layer_lp'),'coverage':coverage}
            operations.append({'id':opid,'checkpoint_key':layer['entity_id'],'command':f'MACRO "{path}"','artifact':{'path':str(path),'sha256':hashlib.sha256(data).hexdigest()},
                'trust_macro':True,'completion_marker':marker,'timeout_s':max(30.,len(block)*2.),
                'prechecks':[{**base,'expected':before}],
                'postchecks':[{**base,'expected':list(current)},{'kind':'receipt','path':str(receipt),'token':token}]})
            snapshots.append({'id':opid,'moves':block,'before_hash':digest(before),'after_hash':digest(current),'before':before,'after':list(current)})
        if current!=layer['target']:raise HelperError('Final generated permutation differs from target')
    plan={'schema':1,'guard':guard,'operations':operations,'offline_only':not enable_execution,'coverage':'order/head verification only; full final geometry/axes/all-point PP audit is separate'}
    validate_plan(plan);atomic_json(output/'plan.json',plan);atomic_json(output/'sort-snapshots.json',snapshots)
    atomic_json(output/'sort-inputs.json',{'schema':1,'guard':guard,'layers':layers})
    summary={'layers':len(layers),'passes':len(all_uids),'moves':sum(len(m) for _,m in prepared),'chunks':len(operations),
        'no_changes_needed':not operations,'simulation_verified':True,'native_executed':False,'power_mill_connected':False}
    atomic_json(output/'summary.json',summary);return summary
