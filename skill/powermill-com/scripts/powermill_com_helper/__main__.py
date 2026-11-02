from pathlib import Path
import argparse,json,sys,os
from .core import Executor,RunStore,HelperError,validate_plan,atomic_json,file_lease,default_lock_dir
from .planner import create_bundle,capture_sort_state,replan_bundle
from .transport import PowerMillTransport
from .surface import capture_profile,audit_geometry,create_surface_bundle
from . import __version__
from .timing import Profiler,span,enabled

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def _main(argv=None):
    parser=argparse.ArgumentParser(description='Direct PowerMill COM helper; planning and status are offline')
    parser.add_argument('--version',action='version',version=__version__)
    sub=parser.add_subparsers(dest='action',required=True)
    validate=sub.add_parser('validate');validate.add_argument('plan')
    status=sub.add_parser('status');status.add_argument('run_dir')
    session=sub.add_parser('session');session.add_argument('--connect',action='store_true');session.add_argument('--output');session.add_argument('--process-id',type=int)
    surface=sub.add_parser('capture-surface');surface.add_argument('guard');surface.add_argument('name');surface.add_argument('output');surface.add_argument('--connect',action='store_true');surface.add_argument('--geometry',action='store_true');surface.add_argument('--max-full-points',type=int,default=10000);surface.add_argument('--max-full-passes',type=int,default=100);surface.add_argument('--sample-passes',type=int,default=32);surface.add_argument('--audit-budget',type=float,default=120)
    surface_plan=sub.add_parser('plan-surface');surface_plan.add_argument('input');surface_plan.add_argument('output');surface_plan.add_argument('--enable-execution',action='store_true')
    sort=sub.add_parser('plan-sort');sort.add_argument('input');sort.add_argument('output');sort.add_argument('--max-moves',type=int,default=64);sort.add_argument('--enable-execution',action='store_true')
    capture=sub.add_parser('capture-sort');capture.add_argument('input');capture.add_argument('output');capture.add_argument('--connect',action='store_true')
    replan=sub.add_parser('replan-sort');replan.add_argument('input');replan.add_argument('observed');replan.add_argument('output');replan.add_argument('--max-moves',type=int,default=64);replan.add_argument('--enable-execution',action='store_true')
    audit=sub.add_parser('audit-simulation');audit.add_argument('check');audit.add_argument('output');audit.add_argument('--checkpoint')
    preparation=sub.add_parser('prepare-simulation');preparation.add_argument('input');preparation.add_argument('output')
    for name in ('run','reconcile'):
        p=sub.add_parser(name);p.add_argument('plan');p.add_argument('run_dir');p.add_argument('--connect',action='store_true')
        if name=='run':p.add_argument('--allow-write',action='store_true')
    args=parser.parse_args(argv)
    try:
        if args.action=='validate':
            plan=validate_plan(read(args.plan));result={'valid':True,'operations':len(plan['operations']),'offline_only':bool(plan.get('offline_only')),'connected':False}
        elif args.action=='prepare-simulation':
            from .simulation_preparation import prepare_simulation
            result=prepare_simulation(read(args.input),args.output)
        elif args.action=='audit-simulation':
            from .simulation import make_audit,validate_recording_check
            check=read(args.check);validate_recording_check(check)
            protected={Path(check[k]['path']).resolve() for k in ('source','contract')}|{Path(check['recording_path']).resolve(),Path(args.check).resolve()}
            output=Path(args.output).resolve();checkpoint=Path(args.checkpoint).resolve() if args.checkpoint else output.with_name(output.name+'.checkpoint.json')
            if output in protected or checkpoint in protected or output==checkpoint:raise HelperError('Audit outputs must not replace inputs')
            engine=make_audit(check,checkpoint);result=engine.poll(complete=check.get('complete',False),min_records=check.get('min_records',0));atomic_json(output,result)
            result={'saved':str(output),'records':result['records'],'complete':result['complete'],'connected':False,'read_only_inputs':True}
        elif args.action=='status':
            directory=Path(args.run_dir)
            if not (directory/'state.json').is_file():raise HelperError('No saved run state')
            store=RunStore(directory,read(directory/'plan.json'));result=store.load()
        elif args.action=='session':
            if not args.connect:raise HelperError('Read-only session discovery requires --connect')
            transport=PowerMillTransport(connect=True,process_id=args.process_id,profile=enabled())
            try:
                result=transport.session()
                if args.output:atomic_json(args.output,result)
            finally:transport.close()
        elif args.action=='capture-surface':
            if not args.connect:raise HelperError('Read-only profile capture requires --connect')
            guard=read(args.guard);transport=PowerMillTransport(connect=True,process_id=guard['process_id'],profile=enabled())
            try:
                with file_lease(default_lock_dir()/('pid-'+str(guard['process_id'])+'.lock')):
                    result=capture_profile(args.name,transport,guard)
                    if args.geometry:result['geometry']=audit_geometry(args.name,transport,guard,max_full_points=args.max_full_points,max_full_passes=args.max_full_passes,max_sample_passes=args.sample_passes,budget_s=args.audit_budget)
                    atomic_json(args.output,result)
            finally:transport.close()
            result={'saved':args.output,'name':result['name'],'connected':True,'read_only':True}
        elif args.action=='plan-surface':result=create_surface_bundle(read(args.input),args.output,enable_execution=args.enable_execution)
        elif args.action=='plan-sort':
            data=read(args.input);result=create_bundle(data['layers'],data['guard'],args.output,max_moves=args.max_moves,enable_execution=args.enable_execution)
        elif args.action=='replan-sort':result=replan_bundle(read(args.input),read(args.observed),args.output,max_moves=args.max_moves,enable_execution=args.enable_execution)
        elif args.action=='capture-sort':
            if not args.connect:raise HelperError('Read-only live capture requires explicit --connect')
            data=read(args.input);transport=PowerMillTransport(connect=True,process_id=data['guard']['process_id'],profile=enabled())
            try:
                with file_lease(default_lock_dir()/('pid-'+str(data['guard']['process_id'])+'.lock')):
                    result=capture_sort_state(data,transport);atomic_json(args.output,result)
            finally:transport.close()
        else:
            plan=validate_plan(read(args.plan))
            if plan.get('offline_only'):raise HelperError('Archived/offline-only plan cannot connect or execute')
            if not args.connect:raise HelperError('Explicit --connect is required')
            if args.action=='run' and not args.allow_write:raise HelperError('Explicit --allow-write is required')
            transport=PowerMillTransport(connect=True,process_id=plan['guard']['process_id'],profile=enabled())
            try:
                engine=Executor(plan,args.run_dir,transport)
                result=engine.run(allow_write=True) if args.action=='run' else engine.reconcile()
            finally:transport.close()
        print(json.dumps(result,ensure_ascii=True,indent=2));return 0
    except (HelperError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=True),file=sys.stderr);return 2

def main(argv=None):
    # A report must be new and distinct from positional inputs/outputs. Export
    # after execution (also on a handled failure); never replace run evidence.
    timer=argparse.ArgumentParser(add_help=False);timer.add_argument('--timing-output')
    options,rest=timer.parse_known_args(sys.argv[1:] if argv is None else argv)
    if options.timing_output is None:return _main(rest)
    target=Path(options.timing_output).resolve()
    if target.exists() or target in {Path(x).resolve() for x in rest if not x.startswith('--')}:
        print(json.dumps({'error':'Timing report must be a new separate file'}),file=sys.stderr);return 2
    result=None
    try:
        with Profiler() as profiler:
            try:
                with span('cli.total'):result=_main(rest)
            finally:
                report=profiler.snapshot();report['exit_code']=result
                with target.open('x',encoding='utf-8') as stream:
                    json.dump(report,stream,ensure_ascii=True,indent=2);stream.flush();os.fsync(stream.fileno())
        return result
    except (OSError,ValueError) as exc:
        print(json.dumps({'error':str(exc)}),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
