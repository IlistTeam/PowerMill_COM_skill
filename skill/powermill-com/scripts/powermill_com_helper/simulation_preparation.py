"""Offline preparation only: compatible settings hints and critical source windows.

No commands, COM transport or permission to run. Numeric bins group test cases;
they never replace full geometry, process, kinematic or collision verification.
"""
from pathlib import Path
import copy,json,math
from .core import HelperError,StateDrift,atomic_json,digest
from .simulation import Kinematics,parse_record,sha256_file,number,angle,signature
from .timing import timed

def pinned(spec):
    path=Path(spec['path']).resolve()
    if sha256_file(path)!=spec['sha256']:raise StateDrift('Preparation input changed: '+str(path))
    return path

def read_profile(path,contract):
    profile=json.loads(Path(path).read_text('utf-8-sig'));proof=profile.pop('integrity',None)
    if proof!=digest(profile) or profile.get('schema')!=1:raise StateDrift('Settings profile integrity changed')
    if profile.get('contract_sha256')!=digest(contract):raise StateDrift('Settings profile is incompatible with this contract')
    if not isinstance(profile.get('settings'),dict):raise HelperError('Profile settings must be an object')
    return profile

@timed('preparation.source_catalog')
def catalog(source,kin):
    rows=[];modal={};move=None;ended=False
    with source.open(encoding='utf-8-sig') as stream:
        for line in stream:
            line=line.strip()
            if line=='(End)':
                if ended:raise HelperError('Repeated source End')
                ended=True;continue
            if not line or line.startswith('('):continue
            if ended:raise HelperError('Records after source End')
            r=parse_record(line,kin.addresses,reference=True);signature(r,modal)
            move=r.fields.get('MOVETYPE',move)
            if move is None:raise HelperError('Initial source motion is unclassified')
            if kin.motion_regions and move not in kin.motion_regions:raise HelperError('Source motion is outside the explicit classification')
            rows.append((r,move))
    if not ended or not rows:raise HelperError('Completed nonempty source required')
    # A cut-start COMPTYPE is the stable pass boundary used by this catalog.
    # A source with another structure must supply a different reviewed reader.
    heads=[i for i,(r,m) in enumerate(rows) if m=='12' and r.fields.get('COMPTYPE')=='1']
    if not heads:raise HelperError('No supported MOVETYPE=12 / COMPTYPE=1 pass boundaries')
    passes=[];starts=[0]+heads[1:]
    for i,st in enumerate(starts):
        en=starts[i+1] if i+1<len(starts) else len(rows);cut=[(n,r) for n,(r,m) in enumerate(rows[st:en],st) if m=='12']
        if not cut:raise HelperError('Empty deposition pass')
        tilts=[math.degrees(math.atan2(math.hypot(r.pose[3],r.pose[4]),r.pose[5])) for _,r in cut]
        radii=[math.hypot(r.pose[0],r.pose[1]) for _,r in cut];heights=[r.pose[2] for _,r in cut]
        passes.append({'pass':i,'record_start':st+1,'record_end':en,'cut_head_record':heads[i]+1,
                       'tilt_min':min(tilts),'tilt_max':max(tilts),'radius_min':min(radii),'radius_max':max(radii),
                       'height_min':min(heights),'height_max':max(heights)})
    return rows,passes

@timed('preparation.select_critical')
def select_critical(rows,passes,request):
    count=len(passes);policy=request.get('policy_by_pass',[None]*count)
    if not isinstance(policy,list) or len(policy)!=count:raise HelperError('Policy must describe every pass')
    bins=request.get('bins',{});width={k:number(bins.get(k,v)) for k,v in [('tilt_degrees',1),('radius_mm',10),('height_mm',20),('azimuth_degrees',45)]}
    if any(v<=0 for v in width.values()):raise HelperError('Critical-case bin widths must be positive')
    if width['azimuth_degrees']>360:raise HelperError('Azimuth bin width exceeds one turn')
    reasons={}
    def choose(i,reason):reasons.setdefault(i,set()).add(reason)
    choose(0,'first approach');choose(count-1,'last pass and departure')
    for key in ('tilt_min','tilt_max','radius_min','radius_max','height_min','height_max'):
        choose(min(range(count),key=lambda i:passes[i][key]),'minimum '+key)
        choose(max(range(count),key=lambda i:passes[i][key]),'maximum '+key)
    families={}
    for i,p in enumerate(passes):
        azimuths=tuple(sorted({math.floor((math.degrees(math.atan2(r.pose[4],r.pose[3]))%360)/width['azimuth_degrees'])
                               for r,m in rows[p['record_start']-1:p['record_end']] if m=='12' and math.hypot(r.pose[3],r.pose[4])>1e-6}))
        family=(math.floor(p['tilt_min']/width['tilt_degrees']),math.floor(p['tilt_max']/width['tilt_degrees']),
                math.floor(p['radius_min']/width['radius_mm']),math.floor(p['radius_max']/width['radius_mm']),
                math.floor(p['height_min']/width['height_mm']),math.floor(p['height_max']/width['height_mm']),
                azimuths,digest(policy[i-1] if i else None),digest(policy[i]))
        families.setdefault(family,[]).append(i)
    for members in families.values():
        choose(members[0],'geometry/control family start');choose(members[-1],'geometry/control family end')
    # Include the largest step in every Cartesian direction and programmed Z,
    # not just endpoints and extrema of deposition geometry.
    def owner(n):
        return next(p['pass'] for p in passes if p['record_start']<=n+1<=p['record_end'])
    for axis in range(3):
        choose(owner(min(range(len(rows)),key=lambda n:rows[n][0].pose[axis])),'minimum source XYZ '+str(axis))
        choose(owner(max(range(len(rows)),key=lambda n:rows[n][0].pose[axis])),'maximum source XYZ '+str(axis))
        if len(rows)>1:
            n=max(range(1,len(rows)),key=lambda n:abs(rows[n][0].pose[axis]-rows[n-1][0].pose[axis]))
            choose(owner(n),'largest XYZ step '+str(axis));choose(owner(n-1),'before largest XYZ step '+str(axis))
    if len(rows)>1:
        n=max(range(1,len(rows)),key=lambda n:angle(rows[n][0].pose[3:6],rows[n-1][0].pose[3:6]))
        choose(owner(n),'largest programmed Z step');choose(owner(n-1),'before largest Z step')
    required=request.get('required_passes',[])
    for i in required:
        if type(i) is not int or not 0<=i<count:raise HelperError('Required critical pass is out of range')
        choose(i,'explicit regression case')
    margin=request.get('context_passes',1)
    if type(margin) is not int or margin<1:raise HelperError('At least one context pass is required')
    windows=[]
    max_window=request.get('max_window_passes',9)
    if type(max_window) is not int or max_window<2*margin+1:raise HelperError('Maximum window cannot remove required context')
    for i in sorted(reasons):
        st=max(0,i-margin);en=min(count-1,i+margin)
        if windows and st<=windows[-1]['last_pass']+1 and max(en,windows[-1]['last_pass'])-windows[-1]['first_pass']+1<=max_window:
            windows[-1]['last_pass']=max(en,windows[-1]['last_pass']);windows[-1]['cases'].append(i)
        else:windows.append({'first_pass':st,'last_pass':en,'cases':[i]})
    for w in windows:
        w['record_start']=passes[w['first_pass']]['record_start'];w['record_end']=passes[w['last_pass']]['record_end']
    covered={i for w in windows for i in range(w['first_pass'],w['last_pass']+1)}
    return {'bins':width,'context_passes':margin,'cases':[{'pass':i,'reasons':sorted(r)} for i,r in sorted(reasons.items())],
            'windows':windows,'family_count':len(families),'covered_passes':len(covered),'coverage_fraction':len(covered)/count,
            'scope':'Candidate native tests, including adjacent transitions. Test-copy approaches must also be checked. Coverage is not proof of full solvability or collision clearance.'}

@timed('preparation.total')
def prepare_simulation(request,output):
    if request.get('schema')!=1:raise HelperError('Unsupported preparation schema')
    source=pinned(request['source']);contract_path=pinned(request['contract']);contract=json.loads(contract_path.read_text('utf-8-sig'));kin=Kinematics(contract)
    for key in ('machine_geometry_sha256','tool_xml_sha256'):
        if not isinstance(contract.get(key),str) or len(contract[key])!=64 or any(c not in '0123456789abcdef' for c in contract[key]):raise HelperError('Preparation requires pinned machine and tool definitions')
    settings=request.get('settings')
    reused=False
    if request.get('candidate_profile'):
        candidate=pinned(request['candidate_profile']);settings=read_profile(candidate,contract)['settings'];reused=True
        if 'settings' in request and request['settings']!=settings:raise HelperError('Explicit settings differ from candidate profile')
    if not isinstance(settings,dict) or not settings:raise HelperError('Explicit settings object or compatible profile required')
    rows,passes=catalog(source,kin);critical=select_critical(rows,passes,request)
    # Fail closed if files changed while cataloging. A profile is a starting
    # hint, never evidence that the current live cell or new path is safe.
    pinned(request['source']);pinned(request['contract'])
    if request.get('candidate_profile'):pinned(request['candidate_profile'])
    profile={'schema':1,'contract_sha256':digest(contract),'settings':copy.deepcopy(settings),'use':'starting settings only; fresh session and critical native tests required'}
    profile['integrity']=digest(profile)
    result={'schema':1,'offline_only':True,'full_run_ready':False,'source':copy.deepcopy(request['source']),
            'contract':copy.deepcopy(request['contract']),'profile_reused':reused,'source_records':len(rows),'passes':len(passes),
            'critical':critical,'pass_catalog':passes,'settings_sha256':digest(settings),
            'next_steps':['Verify fresh live session, cell, tool and source identities within user authorization.',
                          'Create fresh short copies for these windows; preserve original XYZ/Z/process and check native issues plus recorded FK/regions.',
                          'After all critical tests pass with pinned settings, use one immutable bounded plan and one Executor for the full copy.',
                          'After a repeated invalid state on an old copy, diagnose once on a fresh short copy; never replay an uncertain relative operation.',
                          'Keep all collision, source, orientation, region and session checks; independently audit the closed full recording before exclusive version save.']}
    destination=Path(output).resolve()
    if destination.exists():raise HelperError('Preparation output directory must be new')
    destination.mkdir(parents=True)
    atomic_json(destination/'settings-profile.json',profile);atomic_json(destination/'preparation.json',result)
    return {'directory':str(destination),'connected':False,'offline_only':True,'full_run_ready':False,'critical_windows':len(critical['windows']),'passes':len(passes),'source_records':len(rows)}
