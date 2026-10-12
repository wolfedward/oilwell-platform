"""Explicit preview read-model dispatch; unsupported routes never get fake success."""
import re
from .errors import PreviewError


class PreviewRepository:
    def __init__(self,dataset,meta):
        self.dataset=dataset; self.meta=meta; self.routes=[]

    def register(self,method: str,pattern: str,reader):
        if method!='GET': raise ValueError('读取模型只允许 GET')
        if any(p.pattern==pattern for p,_ in self.routes): raise ValueError('重复读取路由')
        self.routes.append((re.compile(pattern),reader))

    def read(self,path: str,query: dict,actor: dict):
        for pattern,reader in self.routes:
            match=pattern.fullmatch(path)
            if match: return reader(query,actor,**match.groupdict())
        raise PreviewError('not_found','此页面接口尚未装配',404)
