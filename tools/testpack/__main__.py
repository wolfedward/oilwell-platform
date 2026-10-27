"""python -m tools.testpack: offline input generation and actual verification."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .config import parse_config
from .storage import canonical_bytes,load_pack,safe_path,strict_json
from .build import build
from .validate import offline_passed,validate_pack


def main(argv=None):
    parser=argparse.ArgumentParser(description='Reproducible synthetic software test inputs; not field measurements')
    sub=parser.add_subparsers(dest='command',required=True)
    generate=sub.add_parser('generate')
    generate.add_argument('--config',required=True,type=Path)
    generate.add_argument('--output',required=True,type=Path)
    validate=sub.add_parser('validate')
    validate.add_argument('--pack',required=True,type=Path)
    validate.add_argument('--report',required=True,type=Path)
    initialize=sub.add_parser('lab-init')
    initialize.add_argument('--pack',required=True,type=Path)
    initialize.add_argument('--root',required=True,type=Path)
    initialize.add_argument('--source',required=True,type=Path)
    initialize.add_argument('--ports-file',type=Path)
    run=sub.add_parser('lab-run')
    run.add_argument('--pack',required=True,type=Path)
    run.add_argument('--root',required=True,type=Path)
    run.add_argument('--run',required=True,type=Path)
    run.add_argument('--stage',choices=('telemetry','waveforms','studies','all'),default='telemetry')
    sub.add_parser('lab-stop').add_argument('--root',required=True,type=Path)
    report=sub.add_parser('report')
    report.add_argument('--run',required=True,type=Path)
    report.add_argument('--output',required=True,type=Path)
    view=sub.add_parser('view')
    view.add_argument('--pack',required=True,type=Path)
    view.add_argument('--report',type=Path)
    view.add_argument('--results',type=Path,help='Read captured approved exports bound to --report; no current authorization check')
    view.add_argument('--port',type=int,default=0)
    args=parser.parse_args(argv)
    try:
        if args.command=='report':
            from .lab import load_lab
            from .report import export_public_report
            root=safe_path(args.run);lab=load_lab(root.parent)
            origin=strict_json((root/'run.json').read_bytes())
            if origin['lab_id']!=lab.lab_id:raise ValueError('run belongs to another lab')
            output=export_public_report(root,args.output)
            print(json.dumps({'report':str(output)},ensure_ascii=False));return 0
        if args.command=='view':
            from .viewer import serve_viewer
            viewer=serve_viewer(load_pack(args.pack),args.report,port=args.port,results_root=args.results)
            print(json.dumps({'url':f'http://127.0.0.1:{viewer.server_port}/','read_only':True},ensure_ascii=False),flush=True)
            try:viewer.viewer_thread.join()
            except KeyboardInterrupt:pass
            finally:viewer.shutdown();viewer.server_close()
            return 0
        if args.command.startswith('lab-'):
            from .lab import initialize_lab,load_lab,stop_lab,DEFAULT_PORTS
            from tools.review_deploy import launch
            if args.command=='lab-init':
                ports=DEFAULT_PORTS
                if args.ports_file is not None:
                    source=safe_path(args.ports_file.absolute())
                    if source.stat().st_size>4096:raise ValueError('port mapping too large')
                    ports=strict_json(source.read_bytes())
                lab=initialize_lab(args.root,pack=load_pack(args.pack),source=args.source,ports=ports)
                launch(lab.monitoring_root)
                launch(lab.review_root)
                print(json.dumps({'lab_id':lab.lab_id,'status':'running'},ensure_ascii=False))
                return 0
            lab=load_lab(args.root)
            if args.command=='lab-stop':
                stop_lab(lab)
                print(json.dumps({'lab_id':lab.lab_id,'status':'stopped'}))
                return 0
            if args.stage=='all':
                from .delivery import run_delivery
                from .report import overall_status
                checks,public=run_delivery(lab,load_pack(args.pack),args.run)
                status=overall_status(checks)
                print(json.dumps({'lab_id':lab.lab_id,'status':status,'report':str(public)},ensure_ascii=False))
                return 0 if status=='passed' else 1
            from .telemetry_run import run_telemetry
            from .waveform_run import run_waveforms
            from .study_run import run_studies
            selected={'telemetry':(run_telemetry,'telemetry-checks.json'),
                      'waveforms':(run_waveforms,'waveform-checks.json'),
                      'studies':(run_studies,'study-checks.json')}[args.stage]
            checks=selected[0](lab,load_pack(args.pack),args.run)
            passed=bool(checks) and all(c.status=='passed' for c in checks)
            from .report import overall_status
            print(json.dumps({'lab_id':lab.lab_id,'status':overall_status(checks),
                              'report':str(args.run/selected[1])},ensure_ascii=False))
            return 0 if passed else 1
        if args.command=='generate':
            source=safe_path(args.config.absolute())
            if source.stat().st_size>65536:
                raise ValueError('configuration too large')
            configuration=strict_json(source.read_bytes())
            if isinstance(configuration,dict) and configuration.get('schema')=='owp.testpack.config/2':
                from .config_v2 import parse_config_v2
                from .build_v2 import build_v2
                pack=build_v2(parse_config_v2(configuration),args.output)
            else:
                pack=build(parse_config(configuration),args.output)
            print(json.dumps({'pack_id':pack.manifest['pack_id'],'root':str(pack.root),'source':'synthetic'},ensure_ascii=False))
            return 0
        report=safe_path(args.report,missing=True)
        if report.exists():
            raise ValueError('report must be a new file')
        pack=load_pack(args.pack)
        from .validate_v2 import validate_versioned
        checks=validate_versioned(pack)
        passed=offline_passed(checks)
        result={'schema':'owp.testpack.offline-report/1','pack_id':pack.manifest['pack_id'],
                'status':'passed' if passed else 'failed','scope':'offline and pure numeric only',
                'checks':[asdict(c) for c in checks]}
        with report.open('xb') as stream:stream.write(canonical_bytes(result))
        print(json.dumps({'status':result['status'],'report':str(report),'scope':result['scope']},ensure_ascii=False))
        return 0 if passed else 1
    except Exception as exc:
        # Lab failures can contain connection settings; keep details private.
        print('testpack: '+(type(exc).__name__+'; inspect owned private artifacts' if args.command.startswith('lab-') else str(exc)),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
