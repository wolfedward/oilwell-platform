from datetime import datetime,timezone
import json
from ..catalog import load_catalog,ANNUAL_TASKS
from .acquisition import page_query

TASK_TITLES={'Y1-01':'电功图转换及冲程、冲次、周期同步测量','Y1-02':'稳定自动化数字采集与数据处理','Y1-03':'安全信息传输与共享','Y1-04':'可靠决策支持系统','Y2-01':'低产井渗流恢复与自适应开停制度','Y2-02':'DSP + MCU 智能化抽油测控','Y3-01':'驱动电机安全保护与故障诊断控制','Y3-02':'MCU + DSP 节能控制与诊断集成','Y3-03':'电机故障电气特征及保护方案','Y3-04':'转子断条与定子电流频谱分析','Y3-05':'智能抽油技术集成系统','Y3-06':'系统示范平台与研究追溯'}
def register(repository):
    ds=repository.dataset;catalog=load_catalog()
    def roles(q,a):
        result=ds.query_all('SELECT id,code,name,status,version FROM roles ORDER BY id')
        for row in result:row['permissions']=ds.column('SELECT p.code FROM permissions p JOIN role_permissions rp ON rp.permission_id=p.id WHERE rp.role_id=? ORDER BY p.code',(row['id'],))
        return result
    def audit(q,a):
        pq=page_query(q);p=pq.get('page',1);size=pq.get('page_size',20);where=[];args=[]
        for key in ('action','object_type'):
            if q.get(key):where.append('l.'+key+'=?');args.append(q[key])
        clause=' WHERE '+' AND '.join(where) if where else ''
        total=ds.scalar('SELECT count(*) FROM audit_logs l'+clause,tuple(args))
        rows=ds.query_all('SELECT l.id,l.actor_id,u.display_name AS actor_name,l.action,l.object_type,l.object_id,l.details_json,l.created_at,l.source_type FROM audit_logs l LEFT JOIN users u ON u.id=l.actor_id'+clause+' ORDER BY l.id DESC LIMIT ? OFFSET ?',tuple(args)+(size,(p-1)*size))
        for row in rows:row['details']=json.loads(row.pop('details_json') or '{}')
        return {'items':rows,'total':total,'page':p,'page_size':size}
    def evidence(q,a):
        return {'requirements':[{'code':code,'year':'第 '+code[1]+' 年度','title':TASK_TITLES[code],'route':next(p['id'] for p in catalog if code in p['annual_tasks']),'evidence':[{'label':'关联设计页面（非研发完成证明）','count':sum(code in p['annual_tasks'] for p in catalog),'latest_id':None}]} for code in sorted(ANNUAL_TASKS)],'integrity':{'database':ds.scalar('PRAGMA integrity_check'),'foreign_keys':'passed' if not ds.query_all('PRAGMA foreign_key_check') else 'failed'},'generated_at':datetime.now(timezone.utc).isoformat(),'source_kind':'catalog_mapping'}
    base='/api/preview/legacy/api'
    repository.register('GET',base+'/users',lambda q,a:ds.query_all('SELECT u.id,u.username,u.display_name,u.role_id,r.code AS role_code,u.status,u.version FROM users u LEFT JOIN roles r ON r.id=u.role_id ORDER BY u.id'))
    repository.register('GET',base+'/roles',roles)
    repository.register('GET',base+'/audit',audit)
    repository.register('GET',base+'/health',lambda q,a:{'service':'ready','database':'read-only','simulator':'未启动','version':'page-preview-v1','time':datetime.now(timezone.utc).isoformat()})
    repository.register('GET',base+'/acceptance/evidence',evidence)
