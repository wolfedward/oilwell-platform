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
    run=sub.add_parser('lab-run')
    run.add_argument('--pack',required=True,type=Path)
    run.add_argument('--root',required=True,type=Path)
    run.add_argument('--run',required=True,type=Path)
    sub.add_parser('lab-stop').add_argument('--root',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command.startswith('lab-'):
            from .lab import initialize_lab,load_lab,stop_lab,DEFAULT_PORTS
            from tools.review_deploy import launch
            if args.command=='lab-init':
                lab=initialize_lab(args.root,pack=load_pack(args.pack),source=args.source,ports=DEFAULT_PORTS)
                launch(lab.monitoring_root)
                launch(lab.review_root)
                print(json.dumps({'lab_id':lab.lab_id,'status':'running'},ensure_ascii=False))
                return 0
            lab=load_lab(args.root)
            if args.command=='lab-stop':
                stop_lab(lab)
                print(json.dumps({'lab_id':lab.lab_id,'status':'stopped'}))
                return 0
            from .telemetry_run import run_telemetry
            checks=run_telemetry(lab,load_pack(args.pack),args.run)
            passed=bool(checks) and all(c.status=='passed' for c in checks)
            print(json.dumps({'lab_id':lab.lab_id,'status':'passed' if passed else 'failed',
                              'report':str(args.run/'telemetry-checks.json')},ensure_ascii=False))
            return 0 if passed else 1
        if args.command=='generate':
            source=safe_path(args.config.absolute())
            if source.stat().st_size>65536:
                raise ValueError('configuration too large')
            pack=build(parse_config(strict_json(source.read_bytes())),args.output)
            print(json.dumps({'pack_id':pack.manifest['pack_id'],'root':str(pack.root),'source':'synthetic'},ensure_ascii=False))
            return 0
        report=safe_path(args.report,missing=True)
        if report.exists():
            raise ValueError('report must be a new file')
        pack=load_pack(args.pack)
        checks=validate_pack(pack)
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
