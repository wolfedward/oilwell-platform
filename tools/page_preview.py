"""Prepare/launch/status/stop the isolated, loopback-only page preview."""
import argparse,hashlib,http.client,io,json,os,secrets,socket,subprocess,sys,time,zipfile
from pathlib import Path
from backend.preview.snapshot import secure_directory,capture_snapshot,build_business_snapshot,verify_manifest,file_hash
from backend.preview.auth import initialize_accounts
from tools.build_open_interface_kit import PUBLIC_FILES
ROOT=Path(__file__).resolve().parents[1]
GIT=Path(os.environ.get('LOCALAPPDATA',''))/'hermes/git/cmd/git.exe'
_CHILDREN={}

def allowed(name):
    p=Path(name)
    return (name in {'preview.html','styles.css','assets/heneng-logo.png','tools/page_preview.py','tools/verify_page_preview.py','tools/page_preview_browser.cjs','tools/build_open_interface_kit.py','tools/__init__.py'} or
            (p.parts[0] in {'backend','frontend'} and p.suffix in {'.py','.sql','.js','.css','.json'} and '__pycache__' not in p.parts) or
            name in {'assets/open-interface/'+x for x in (*PUBLIC_FILES,'open-interface-r1.zip')})

def package(code_source,target,revision):
    files={}
    if revision is not None:
        rev=subprocess.check_output([str(GIT),'rev-parse',revision+'^{commit}'],cwd=code_source,text=True).strip()
        archive=subprocess.check_output([str(GIT),'archive','--format=zip',rev],cwd=code_source)
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            entries=[(n,z.read(n)) for n in z.namelist() if not n.endswith('/') and allowed(n)]
    else:
        rev='uncommitted-test-only';entries=[(p.relative_to(code_source).as_posix(),p.read_bytes()) for folder in ('backend','frontend','tools','assets') for p in (code_source/folder).rglob('*') if p.is_file() and not p.is_symlink() and allowed(p.relative_to(code_source).as_posix())]
        entries += [(n,(code_source/n).read_bytes()) for n in ('preview.html','styles.css')]
    target.mkdir(exist_ok=False)
    for name,raw in entries:
        dest=target/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw);files[name]=hashlib.sha256(raw).hexdigest()
    for required in ['preview.html','frontend/preview/renderers.js','tools/page_preview.py','backend/preview/server.py']:
        if required not in files:raise ValueError('固定版本缺少预览文件: '+required)
    return {'revision':rev,'files':files,'code_sha256':hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()}

def prepare(source_db,target,*,code_source=ROOT,revision='HEAD',progress=lambda x:None):
    source_db=Path(source_db).resolve(strict=True);target=Path(target).absolute()
    if target.exists():raise FileExistsError('目标已存在，不覆盖: '+str(target))
    secure_directory(target)
    progress('固定代码打包');code=package(Path(code_source),target/'code',revision)
    progress('获取演示库一致性只读快照');raw=capture_snapshot(source_db,target/'private')
    progress('脱敏业务副本与逐表摘要');business=build_business_snapshot(Path(raw['path']),target/'business.db')
    accounts=initialize_accounts(target/'private')
    # A separate minimal handoff file; salts/hashes never needed by the user.
    (target/'private/登录账号.json').write_text(json.dumps([{'岗位':a['display_name'],'账号':a['username'],'密码':a['password']} for a in accounts],ensure_ascii=False,indent=2),encoding='utf-8')
    install={'mode':'preview','application_pages':55,'open_interface_entries':1,'control_enabled':False,'code_path':'code',**code,'snapshot_id':business['snapshot_id'],'snapshot_at':business['captured_at'],'data_from':business['data_from'],'data_to':business['data_to']}
    (target/'installation.json').write_text(json.dumps(install,ensure_ascii=False,indent=2),encoding='utf-8')
    progress('准备完成');return {k:v for k,v in install.items() if k!='files'}

def installation(target):
    target=Path(target).resolve(strict=True);data=json.loads((target/'installation.json').read_text(encoding='utf-8'))
    if data.get('mode')!='preview' or data.get('control_enabled') is not False:raise ValueError('不是独立预览安装')
    code=(target/data.get('code_path','code')).resolve(strict=True)
    if not code.is_relative_to(target) or code.is_symlink():raise ValueError('代码目录越界')
    return target,data,code

def verify_code(target):
    target,data,code=installation(target)
    for name,digest in data['files'].items():
        path=code/name
        if not allowed(name) or not path.resolve().is_relative_to(code) or path.is_symlink() or file_hash(path)!=digest:raise ValueError('代码摘要不匹配: '+name)
    return data

def request(port,path='/api/preview/health',method='GET',token=None):
    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    try:
        headers={'Origin':f'http://127.0.0.1:{port}'}
        if token:headers['X-Preview-Shutdown']=token
        conn.request(method,path,headers=headers);response=conn.getresponse();payload=json.loads(response.read())
        if response.status!=200:raise ValueError('服务拒绝请求')
        return payload['data']
    finally:conn.close()

def status(target):
    target,data,code=installation(target);path=target/'private/runtime.json'
    if not path.exists():return {'status':'stopped'}
    runtime=json.loads(path.read_text(encoding='utf-8'))
    try:health=request(runtime['port'])
    except (OSError,ValueError):return {'status':'stopped','last_pid':runtime['pid']}
    if health.get('instance_id')!=runtime['instance_id'] or health.get('snapshot_id')!=data['snapshot_id']:raise ValueError('端口不属于此预览实例，拒绝操作')
    return {'status':'ready','pid':runtime['pid'],'port':runtime['port'],'instance_id':runtime['instance_id'],'url':f'http://127.0.0.1:{runtime["port"]}/','revision':data['revision']}

def launch(target,*,port=8772,timeout=120):
    target,data,code=installation(target)
    if status(target)['status']=='ready':return status(target)
    # Check before heavy validation; never stop a foreign listener.
    with socket.socket() as sock:sock.bind(('127.0.0.1',port));port=sock.getsockname()[1]
    verify_code(target);verify_manifest(target)
    instance=secrets.token_hex(16);token=secrets.token_urlsafe(32)
    runtime={'pid':0,'port':port,'instance_id':instance,'shutdown_token':token,'code_path':str(code)}
    path=target/'private/runtime.json';path.write_text(json.dumps(runtime),encoding='utf-8')
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (target/'private/server.log').open('ab') as log:
        child=subprocess.Popen([sys.executable,'-B','-m','tools.page_preview','serve','--target',str(target),'--instance-id',instance],cwd=code,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=flags)
    runtime['pid']=child.pid;path.write_text(json.dumps(runtime),encoding='utf-8')
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if child.poll() is not None:raise RuntimeError('预览启动异常退出，请查看 private/server.log')
        try:
            result=status(target)
            if result['status']=='ready':
                _CHILDREN[str(target)]=child
                return result
        except (OSError,ValueError):pass
        time.sleep(1)
    # This handle was created by this call, not recovered by a potentially reused PID.
    if child.poll() is None:child.terminate();child.wait(timeout=10)
    raise TimeoutError('预览在就绪期限内未启动；仅停止本次创建的子进程，日志保留')

def stop(target):
    target,data,code=installation(target);current=status(target)
    if current['status']=='stopped':return current
    runtime=json.loads((target/'private/runtime.json').read_text(encoding='utf-8'))
    request(runtime['port'],'/api/preview/lifecycle/stop','POST',runtime['shutdown_token'])
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        try:
            if status(target)['status']=='stopped':
                child=_CHILDREN.pop(str(target),None)
                if child is not None:child.wait(timeout=10)
                return {'status':'stopped'}
        except OSError:return {'status':'stopped'}
        time.sleep(.2)
    raise TimeoutError('所属实例尚未退出；未强制结束任何进程')

def serve(target,instance_id):
    from backend.preview.server import create_preview_server
    target,data,code=installation(target);runtime=json.loads((target/'private/runtime.json').read_text(encoding='utf-8'))
    if runtime['instance_id']!=instance_id or Path(__file__).resolve().parents[1]!=code:raise ValueError('启动归属不匹配')
    verify_code(target)
    server=create_preview_server(target,port=runtime['port']);server.repository.meta['instance_id']=instance_id;server.shutdown_token=runtime['shutdown_token']
    try:server.serve_forever()
    finally:server.server_close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['prepare','launch','status','stop','serve']);p.add_argument('--target',type=Path,required=True);p.add_argument('--source-db',type=Path);p.add_argument('--revision',default='HEAD');p.add_argument('--instance-id');p.add_argument('--port',type=int,default=8772);a=p.parse_args()
    if a.action=='prepare':
        if not a.source_db:p.error('prepare 需要显式 --source-db')
        result=prepare(a.source_db,a.target,revision=a.revision,progress=lambda text:print(json.dumps({'stage':text},ensure_ascii=False),flush=True))
    elif a.action=='serve':serve(a.target,a.instance_id);return
    else:result=launch(a.target,port=a.port) if a.action=='launch' else status(a.target) if a.action=='status' else stop(a.target)
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
