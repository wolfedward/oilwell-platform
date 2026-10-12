"""Preview uses the same explicit role capabilities, never production credentials."""
from backend.registry.permissions import REGISTRY_ROLE_PERMISSIONS
from backend.ingest_reliability.permissions import INGEST_ROLE_PERMISSIONS
from backend.waveforms.permissions import WAVEFORM_ROLE_PERMISSIONS
from backend.inversion.permissions import ROLE_PERMISSIONS as INVERSION_ROLE_PERMISSIONS

COMMON={'wells.read','dashboard.read','telemetry.read','devices.read','acquisition.read','data_quality.read','workflow.read','studies.read','reports.read'}
EXTRA={
    'production_engineer':{'wells.update','alarms.ack','strategy.create','strategy.submit','studies.create'},
    'algorithm_engineer':{'diagnosis.run','studies.create','studies.export'},
    'maintenance_engineer':{'wells.update','devices.control','devices.configure','commands.read','maintenance.manage','data_quality.repair','studies.create'},
    'reviewer':{'diagnosis.review','audit.read','commands.read','strategy.approve','studies.review','studies.select','studies.export'},
}


def role_permissions(role):
    if role=='administrator':
        result={'users.read','users.manage','roles.read','roles.manage','audit.read'}
        for code in EXTRA: result.update(role_permissions(code))
        for mapping in (REGISTRY_ROLE_PERMISSIONS,INGEST_ROLE_PERMISSIONS,WAVEFORM_ROLE_PERMISSIONS,INVERSION_ROLE_PERMISSIONS): result.update(mapping['administrator'])
        return sorted(result)
    if role not in EXTRA: return []
    result=COMMON|EXTRA[role]
    for mapping in (REGISTRY_ROLE_PERMISSIONS,INGEST_ROLE_PERMISSIONS,WAVEFORM_ROLE_PERMISSIONS,INVERSION_ROLE_PERMISSIONS): result.update(mapping[role])
    return sorted(result)


def allowed_action(role,action):
    if role=='administrator': return True
    if action in {'approve','reject','review'}: return role=='reviewer'
    if action in {'edit','create','submit','draft','calculate_input'}: return role in {'production_engineer','algorithm_engineer','maintenance_engineer'}
    if action in {'acknowledge','close','assign'}: return role in {'production_engineer','maintenance_engineer','reviewer'}
    if action in {'configure','scenario','control_plan'}: return role=='maintenance_engineer'
    return False

def required_read(path):
    for prefix,permission in [('users','users.read'),('roles','roles.read'),('audit','audit.read'),('commands','commands.read'),('registry','registry.read'),('ingest-reliability','ingest.read'),('waveforms','waveform.read'),('production-inversion','inversion.read')]:
        if '/api/'+prefix in path:
            if prefix=='ingest-reliability' and path.endswith('/raw'):return 'ingest.raw.read'
            if prefix=='waveforms' and ('/raw' in path or '/files/' in path):return 'waveform.raw.read'
            return permission
    return None

def require_read(actor,path):
    from .errors import PreviewError
    permission=required_read(path)
    if permission and permission not in actor.get('permissions',[]):raise PreviewError('forbidden','当前岗位无权读取该内容',403)

def require_object_action(actor,object_id,action):
    from .errors import PreviewError
    permission=None
    if '/api/users' in object_id:permission='users.manage'
    elif '/api/roles' in object_id:permission='roles.manage'
    elif '/api/commands' in object_id or action=='control_plan':permission='devices.control'
    if permission and permission not in actor.get('permissions',role_permissions(actor['role_code'])):raise PreviewError('forbidden','当前岗位无权修改此对象',403)

PAGE_READ={'users':'users.read','roles':'roles.read','audit':'audit.read','commands':'commands.read','device-command':'commands.read','field-registry':'registry.read','ingest-reliability':'ingest.read','waveforms':'waveform.read','production-parameters':'inversion.read','production-inversion':'inversion.read','production-results':'inversion.read'}
