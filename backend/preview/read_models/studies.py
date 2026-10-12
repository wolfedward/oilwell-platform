from copy import deepcopy
from ..fixtures.studies import records
from ..errors import PreviewError

def register(repository):
    def listing(q,a):
        try:offset=int(q.get('offset',0));limit=int(q.get('limit',50))
        except ValueError:raise PreviewError('invalid_pagination','分页数值无效')
        if offset<0 or not 1<=limit<=100:raise PreviewError('invalid_pagination','分页范围无效')
        return deepcopy([r for r in records() if not q.get('kind') or r['kind']==q['kind']][offset:offset+limit])
    def detail(q,a,identifier):
        row=next((r for r in records() if r['id']==identifier),None)
        if row is None:raise PreviewError('not_found','研究记录不存在',404)
        return deepcopy(row)
    base='/api/preview/legacy/api/production-studies'
    repository.register('GET',base,listing)
    repository.register('GET',base+r'/(?P<identifier>[\w-]+)',detail)
    repository.register('GET',base+r'/(?P<identifier>[\w-]+)/export',detail)
