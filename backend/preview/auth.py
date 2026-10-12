"""Independent local-preview accounts. Never accepts an original application session."""
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import threading
import time
from .errors import PreviewError

ROLES=(('administrator','系统管理员'),('production_engineer','生产工程师'),('algorithm_engineer','算法工程师'),('maintenance_engineer','设备运维'),('reviewer','业务复核员'))


def initialize_accounts(private_root: Path) -> list[dict]:
    """Called only by explicit isolated-install preparation; refuses replacement."""
    path=Path(private_root)/'accounts.json'
    accounts=[]
    for index,(role,label) in enumerate(ROLES,1):
        password=secrets.token_urlsafe(18); salt=secrets.token_hex(16)
        accounts.append({'id':f'preview-user-{index}','username':role,'display_name':label,'role_code':role,
                         'salt':salt,'password_hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),300000).hex(),'password':password})
    # The clear initial credentials are restricted to this private handoff file, never served.
    with path.open('x',encoding='utf-8') as stream: json.dump(accounts,stream,ensure_ascii=False,indent=2)
    return accounts


class PreviewAuth:
    def __init__(self,path: Path,clock=time.monotonic):
        self.accounts=json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(self.accounts,list) or len(self.accounts)!=5: raise ValueError('预览身份配置缺失')
        self.clock=clock; self.sessions={}; self.failures={}; self.lock=threading.RLock()

    def login(self,username,password):
        if not isinstance(username,str) or not isinstance(password,str) or len(username)>80 or len(password)>256:
            raise PreviewError('invalid_login','用户名或密码不正确',401)
        with self.lock:
            attempts,until=self.failures.get(username,(0,0))
            if attempts>=10 and self.clock()<until: raise PreviewError('rate_limited','尝试过多，请稍后重试',429)
            account=next((x for x in self.accounts if x['username']==username),None)
            salt=account['salt'] if account else 'preview-dummy-salt'
            digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),300000).hex()
            if not account or not hmac.compare_digest(digest,account['password_hash']):
                self.failures[username]=(attempts+1,self.clock()+60)
                raise PreviewError('invalid_login','用户名或密码不正确',401)
            self.failures.pop(username,None)
            token=secrets.token_urlsafe(32)
            actor={key:account[key] for key in ('id','display_name','role_code')}
            actor['permissions']=list(account.get('permissions',[]))
            self.sessions[token]={'actor':actor,'csrf_token':secrets.token_urlsafe(32),'expires':self.clock()+8*3600}
            return token,self.session(token)

    def session(self,token):
        with self.lock:
            value=self.sessions.get(token)
            if not value or value['expires']<=self.clock():
                self.sessions.pop(token,None)
                raise PreviewError('unauthenticated','请登录独立页面预览',401)
            return {'actor':dict(value['actor']),'csrf_token':value['csrf_token']}

    def logout(self,token):
        with self.lock: self.sessions.pop(token,None)
