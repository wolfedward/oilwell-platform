"""Private verification evidence for a fixed preview package."""
import argparse,json,os,subprocess
from pathlib import Path
from backend.preview.snapshot import verify_manifest
from tools.page_preview import verify_code,status,ROOT
def browser_check(target,port,*,screenshot_dir=None):
    accounts=json.loads((Path(target)/'private/accounts.json').read_text(encoding='utf-8'))
    runtime=Path(os.environ['USERPROFILE'])/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    result=subprocess.run([str(runtime/'bin/node.exe'),str(ROOT/'tools/page_preview_browser.cjs')],input=json.dumps({'url':f'http://127.0.0.1:{port}','username':accounts[0]['username'],'password':accounts[0]['password'],'screens':str(screenshot_dir or '')}),text=True,encoding='utf-8',capture_output=True,env={**os.environ,'NODE_PATH':str(runtime/'node_modules')},timeout=600)
    if result.returncode:raise AssertionError(result.stderr)
    return json.loads(result.stdout)
def main():
    p=argparse.ArgumentParser();p.add_argument('--target',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    code=verify_code(a.target);manifest=verify_manifest(a.target);runtime=status(a.target)
    if runtime['status']!='ready':raise RuntimeError('预览尚未启动')
    result=browser_check(a.target,runtime['port'],screenshot_dir=a.target/'private/screens')
    evidence={'revision':code['revision'],'snapshot_at':manifest['captured_at'],'tables':{k:v['rows'] for k,v in manifest['tables'].items()},**result}
    a.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'pages_checked':len(result['pages']),'failures':len(result['failures']),'page_errors':len(result['page_errors']),'outside_preview_requests':len(result['outside_preview_requests']),'overflow':len(result['overflow']),'report':str(a.output)},ensure_ascii=False))
    if any(result[k] for k in ['failures','page_errors','outside_preview_requests','overflow']):raise SystemExit(1)
if __name__=='__main__':main()
