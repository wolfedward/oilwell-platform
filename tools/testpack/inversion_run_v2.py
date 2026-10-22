"""Real owned HTTP intake, independent account review and worker execution.

Synthetic assertions never establish instrument calibration or field accuracy.
No database connections or approval-table writes are available in this runner.
"""
from fractions import Fraction as Q
from hashlib import sha256
from pathlib import Path
import time
from urllib.parse import quote
from uuid import uuid4

from backend.inversion.contracts import canonical_bytes as inversion_bytes
from backend.inversion.coverage_contracts import parameter_core_bytes
from backend.inversion.parameter_contracts_v2 import parse_parameter_set_v2
from backend.inversion.synthetic_registry import case_definition,case_ids
from backend.waveforms.quality_context import utc_ns
from backend.waveforms.wire import parse_frame
from tools.review_deploy_paths import private_acl
from .budget_v2 import inspect_v2
from .config_v2 import parse_config_v2
from .evidence_report import render_evidence_pdf
from .inversion_binding_v2 import coverage_for,bind_v2,native_evidence
from .inversion_http_v2 import InversionSession,InversionOutcome
from .lab import checked_pack,run_context,finish_context
from .lab_http import LabHttp,LabHttpError
from .storage import canonical_bytes,safe_path,strict_json,load_pack
from .validate_v2 import validate_pack_v2
from .waveform_run import WaveformSession,bind_channels,read_cycle,save_bound_file,common,waveform_outcome
from .waveforms import encode_cycle

AUTHOR='algorithm_engineer'
REVIEWER='reviewer'
POSITIVES=('cycle-base','cycle-balance','cycle-friction','cycle-speed')
FAULTS=('cycle-noise','cycle-missing','cycle-clipped','cycle-shifted')


def positive_case_ids(pack):
    registered=set(case_ids(sha256(inversion_bytes(pack.manifest['configuration'])).hexdigest()))
    cycles=[r for r in pack.manifest['cases'] if r['facts'].get('data_kind')=='cycle']
    positives=tuple(r['case_id'] for r in cycles if r['category']=='mechanistic')
    if (len(cycles)!=len(registered) or {r['case_id'] for r in cycles}!=registered or
            set(positives)!=registered-set(FAULTS)):raise ValueError('registered cycle coverage changed')
    return positives


def require_waveform_refusal(case_id,facts):
    if facts['dataset'] is not None or waveform_outcome(case_id,facts['pair']['body']['joint_result'])!='passed':
        raise ValueError('fault not refused for its required observed reason')


def save(root,name,value):
    path=safe_path(root/name,missing=True)
    with path.open('xb') as stream:stream.write(canonical_bytes(value))
    return path


class AuthorHttp(LabHttp):
    def request(self,*args,actor,**kwargs):
        # Legacy waveform helper names its author administrator. Only this
        # private session aliases it; review and permission-negative sessions do not.
        return super().request(*args,actor=AUTHOR if actor=='administrator' else actor,**kwargs)


class V2WaveformSession(WaveformSession):
    def __init__(self,lab,root):
        self.root=root;self.http=AuthorHttp(lab,'review');self.origin_mapping=None

    def evidence(self,kind,body):
        if kind=='origin':
            if self.origin_mapping is None:raise ValueError('actual original/bound mapping required')
            body=body|{'verification_method':'testpack_original_bound/1',
                       'verification_basis':inversion_bytes(self.origin_mapping).decode()}
        return super().evidence(kind,body)

    def pdf(self,summary,name):
        raw=render_evidence_pdf(summary,self.root/(name+'.pdf')).read_bytes()
        return self.http.request('POST','/api/waveforms/attachments',raw,actor=AUTHOR,headers={
            'Content-Type':'application/pdf','Idempotency-Key':str(uuid4()),'If-Match':'0',
            'X-Waveform-Reason':quote('Finite synthetic input budget, not field certification'),
            'X-Content-SHA256':sha256(raw).hexdigest()})['data']


def channel_pins(selection,archive):
    def pin(ref):return {'id':ref['id'],'content_sha256':ref['content_sha256'],'review_version':int(ref['version'])}
    producer=archive['manifest']['streams'][0]['source']
    return {c['role']:dict(c['capture'],stream_id=c['stream_id'],channel_id=c['channel_id'],
        manifest_sha256=archive['manifest_sha256'],sampling_pin=pin(c['sampling_config']),calibration_pin=pin(c['calibration']),
        sync_pin=pin(selection['sync']),producer_endpoint={k:producer[k] for k in ('gateway_id','device_id')},
        first_index=int(c['first_index']),last_index=int(c['last_index'])) for c in selection['channels']}


def require_budget(report):
    if (report.get('passed') is not True or report.get('source_proof_status')!='verified' or
        report.get('native_points')!=1025 or Q(report.get('original_prediction_coverage','0'))<Q(4,5)):
        raise ValueError('finite budget is not closed; no approval permitted')


def budget_pdf_summary(case_id,budget,parameter):
    contract=parameter.coverage_contract
    summary={k:v for k,v in budget.items() if k!='points'}
    return summary|dict(case_id=case_id,parameter_core_sha256=parameter.parameter_core_sha256,
        contract_payload_sha256=contract.payload_sha256,checker_sha256=contract.body['binding']['checker_sha256'],
        policy_sha256=parameter.policy.raw_sha256,source_kind='synthetic',
        attachment_binding='Budget attachment identity excluded from payload digest; final parameter digest recorded after upload')


def run_request(parameter,model,pair,dataset,selection):
    return dict(dataset_version_id=dataset['id'],dataset_sha256=dataset['content_sha256'],member_ordinal=0,
        pair_id=pair['id'],pair_sha256=pair['content_sha256'],pair_review_version=int(pair['version']),
        assembly_id=parameter['assembly_id'],parameter_id=parameter['id'],parameter_sha256=parameter['content_sha256'],
        parameter_review_version=parameter['review_version'],model_id=model['id'],model_sha256=model['content_sha256'],
        model_review_version=model['review_version'],selection_version=selection['version'],operating_condition_ids=['condition-1'])


def post(session,path,body=None,*,version=None,actor=AUTHOR,status=201):
    return session.post(path,body,actor=actor,expected_version=version,key=str(uuid4()),expected_status=status)


def registered_model(session):
    models=session.get('/models?limit=100')['items']
    found=[m for m in models if m['installed_id']=='beam_energy_balance/2']
    if not found:
        receipt=post(session,'/models',{'installed_id':'beam_energy_balance/2'})
        found=[m for m in session.get('/models?limit=100')['items'] if m['id']==receipt['subject_id']]
    if len(found)!=1:raise ValueError('ambiguous installed v2 model')
    current=found[0]
    if current['review_action']!='approve':
        post(session,'/models/'+current['id']+'/review',{'decision':'approve'},version=current['review_version'],actor=REVIEWER,status=200)
        current=next(m for m in session.get('/models?limit=100')['items'] if m['id']==current['id'])
    if current['review_action']!='approve':raise ValueError('model not independently approved')
    return current


def prepare_case(wave,session,pack,case_id,root,*,before_review=None):
    """Use actual returned evidence pins and physical identity; original pack is read-only."""
    config=parse_config_v2(pack.manifest['configuration']);cycle,original=read_cycle(pack,case_id)
    config_sha=sha256(inversion_bytes(pack.manifest['configuration'])).hexdigest()
    case=case_definition(config_sha,case_id)
    binding=bind_channels(wave,cycle.channels,config.start_utc)
    bound=encode_cycle(cycle,config,binding).artifacts[0].content
    bound_file=save_bound_file(root,case_id,bound)
    archive=wave.archive(bound)
    wave.origin_mapping=dict(schema='owp.testpack.binding/1',pack_id=pack.manifest['pack_id'],case_id=case_id,
        config_sha256=config_sha,generator_sha256=pack.manifest['generator']['source_sha256'],
        original_file_sha256=sha256(original).hexdigest(),bound_file_sha256=sha256(bound).hexdigest())
    physical=wave.attachment(dict(case_id=case_id,source_kind='synthetic',case_definition=strict_json(inversion_bytes(case.body)),
        binding=wave.origin_mapping,scope='Software definition and shared synthetic clock; no physical instrument'),case_id+'-physical')
    selected,profile=wave.selection(archive,cycle,binding,config.start_utc,physical)
    pair_bundle=wave.pair_and_freeze(archive,selected,case_id)
    pair=wave.get('/pairs/'+pair_bundle['pair']['id'])
    facts=dict(archive=archive,selection=selected,pair=pair,dataset=pair_bundle['dataset'],
        original_sha256=sha256(original).hexdigest(),bound=bound_file)
    save(root,case_id+'-intake.json',facts)
    if pair_bundle['dataset'] is None:return facts
    if pair['review_state']!='approve':raise ValueError('pair not independently approved')
    interval=common(config.start_utc);assembly_id=str(uuid4())
    attachment={k:physical[k] for k in ('id','content_sha256')}
    assembly=post(session,'/assemblies',dict(assembly_id=assembly_id,manufacturer='OWP software laboratory',
        model='Registered synthetic four-bar',asset_number=case_id,mechanism='conventional_crank_balanced',
        source_kind='synthetic',source_organization='OWP software laboratory',parent_version=None,
        valid_from=interval['valid_from'],valid_to=interval['valid_to'],installation_evidence=[attachment|{
            'purpose':'installation','source_description':'Synthetic software installation basis, not physical equipment'}]))
    post(session,'/assignments',dict(assembly_id=assembly_id,well_id='test:'+pack.manifest['pack_id']+':'+case_id,
        valid_from=interval['valid_from'],valid_to=interval['valid_to'],close_assignment_id=None))
    native_cycle=pair['body']['physical_cycles'][0]
    epoch=utc_ns(parse_frame(bound.splitlines()[0]))
    pins=dict(assembly_id=assembly_id,assembly_version=assembly['version'],valid_from=interval['valid_from'],valid_to=interval['valid_to'],
        attachment=attachment,channels=channel_pins(selected,archive),
        physical_cycle_id=native_cycle['physical_cycle_key'],
        cycle_start_ns=epoch+Q(native_cycle['support'][0]),cycle_end_ns=epoch+Q(native_cycle['support'][1]),
        notes='Synthetic software verification. Calibration and comparison share the same cycle; no independent field accuracy claim.',
        budget_attachment=dict(id=str(uuid4()),content_sha256='0'*64,purpose='uncertainty',source_description='Temporary in-memory PDF binding; never submitted'))
    evidence,_=native_evidence(original,bound,pins['channels'],pack_id=pack.manifest['pack_id'],case_id=case_id,
        config_sha256=config_sha,generator_sha256=pack.manifest['generator']['source_sha256'])
    contract=coverage_for(case,pins,evidence)
    parameter=parse_parameter_set_v2(bind_v2(case,pins,contract))
    budget=inspect_v2(evidence,parameter)
    save(root,case_id+'-bound-budget.json',budget)
    require_budget(budget)
    summary=budget_pdf_summary(case_id,budget,parameter)
    pdf=wave.pdf(summary,case_id+'-budget')
    pins['budget_attachment']={k:pdf[k] for k in ('id','content_sha256')}|{
        'purpose':'uncertainty','source_description':'Actual finite synthetic input budget PDF; no field accuracy claim'}
    final_contract=coverage_for(case,pins,evidence)
    raw=bind_v2(case,pins,final_contract)
    if (contract.payload_sha256!=final_contract.payload_sha256 or
        sha256(parameter_core_bytes(strict_json(raw))).hexdigest()!=parameter.parameter_core_sha256):
        raise ValueError('PDF binding changed core or coverage payload')
    save(root,case_id+'-parameter.json',strict_json(raw))
    receipt=post(session,'/parameter-versions',strict_json(raw))
    current=session.get('/parameters/'+receipt['subject_id'])
    if current['content_sha256']!=sha256(raw).hexdigest():raise ValueError('parameter server digest mismatch')
    post(session,'/parameters/'+current['id']+'/submit',version=current['review_version'],status=200)
    current=session.get('/parameters/'+current['id'])
    if before_review is not None:before_review(session,current)
    post(session,'/parameters/'+current['id']+'/review',{'decision':'approve'},version=current['review_version'],actor=REVIEWER,status=200)
    current=session.get('/parameters/'+current['id'])
    if current['review_state']!='approved':raise ValueError('parameter approval missing')
    facts.update(parameter=current,budget_summary=summary,budget_attachment=pdf,assembly=assembly)
    save(root,case_id+'-approved-input.json',facts)
    return facts


def check_native_budget(body,prediction,proof):
    """Independently check downloaded native rows before issuing result review."""
    from backend.inversion.result_codec_v2 import PROOF_COLUMNS,PROOF_LIMITS
    if (body['source_proof']['status']!='verified' or body['source_proof']['reason_codes'] or
            len(prediction)!=1025 or len(proof)!=1025):raise ValueError('verified full native proof required')
    for i,(p,a) in enumerate(zip(prediction,proof)):
        if p['power_index']!=str(i) or a['power_index']!=str(i) or p['time_ns']!=a['time_ns']:
            raise ValueError('proof not bound to native prediction')
        if a['motion_status']=='valid':
            if (a['motion_reason_codes'] or len(a['angle_indices'])!=7 or
                    any(a[k] is None or len(a[k])!=3 for k in ('exact_center','base','physical','numerical')) or
                    not 0<=Q(a['base'][1])<=Q(1,100000)):raise ValueError('motion proof refused')
        if a['status']=='valid':
            if (a['motion_status']!='valid' or a['reason_codes'] or
                    any(a[k] is None or not 0<=Q(a[k])<=limit for k,limit in zip(PROOF_COLUMNS[-5:],PROOF_LIMITS))):
                raise ValueError('point exceeds frozen proof budget')
        if p['status']=='valid':
            if (a['status']!='valid' or p['reason_codes'] or any(p[k] is None for k in ('s_m','load_N','error_bound_N')) or
                    not 0<=Q(p['error_bound_N'])<=1000):raise ValueError('valid load lacks proof')
        elif p['load_N'] is not None or not p['reason_codes']:raise ValueError('invalid load must remain null')


def approved_execution(session,facts,model,root,case_id):
    parameter=facts['parameter']
    selection=post(session,'/selections',dict(assembly_id=parameter['assembly_id'],parameter_id=parameter['id'],
        parameter_sha256=parameter['content_sha256'],parameter_review_version=parameter['review_version'],
        model_id=model['id'],model_sha256=model['content_sha256'],model_review_version=model['review_version']))
    history=session.get('/assemblies/'+parameter['assembly_id']+'/selections?limit=100')['items']
    if not any(s['id']==selection['subject_id'] and s['version']==selection['version'] for s in history):
        raise ValueError('selection not durably readable')
    request=run_request(parameter,model,facts['pair'],facts['dataset'],selection)
    preflight=session.preflight(request)
    if preflight!=dict(allowed=True,missing=[],provisional=True,scope='fixed_input_metadata',calculation_performed=False):
        raise ValueError('fixed-pin preflight refused')
    started=time.monotonic()
    queued=post(session,'/runs',request,status=202)
    terminal=session.wait_run(queued['run_id'],deadline=time.monotonic()+210)
    enqueue_to_terminal_seconds=time.monotonic()-started
    save(root,case_id+'-run.json',dict(request=request,preflight=preflight,queued=queued,terminal=terminal))
    if terminal['state']!='completed' or not terminal.get('result_id'):raise ValueError('worker did not complete')
    result_id=terminal['result_id'];result=session.get('/results/'+result_id+'?include_input_metadata=true')
    prediction=session.read_all_points(result_id);proof=session.read_all_points(result_id,kind='proof')
    comparison=session.get('/results/'+result_id+'/comparison')
    if len(prediction)!=1025 or len(proof)!=1025:raise ValueError('native series truncated')
    if any(p['power_index']!=q['power_index'] or p['time_ns']!=q['time_ns'] for p,q in zip(prediction,proof)):
        raise ValueError('proof not bound to native prediction')
    body=result['body'];compare=comparison['comparison']
    check_native_budget(body,prediction,proof)
    if (result['execution']!='completed' or result['current_applicability']['state']!='applicable' or
        body['prediction_status']!='applicable' or Q(body['coverage_fraction'])<Q(4,5) or
        compare['scope']!='original_valid_support' or Q(compare['coverage']['joint_fraction']['value'])<Q(4,5) or
        compare['primary']['load_rmse']['value'] is None or Q(compare['primary']['load_rmse']['value'])>500 or
        result['field_validation']['certified'] is not False or result['field_validation']['independent_comparison'] is not False):
        raise ValueError('completed result does not satisfy frozen acceptance')
    post(session,'/results/'+result_id+'/review',{'decision':'approve'},version=None,actor=REVIEWER,status=200)
    reviewed=session.get('/results/'+result_id)
    if reviewed['review']['state']!='approved' or reviewed['content_sha256']!=result['content_sha256']:
        raise ValueError('independent result review mismatch')
    export=post(session,'/exports',dict(result_id=result_id,intent='quantitative',format='json'))
    payload=session.download(export['id'])
    if export['result_sha256']!=result['content_sha256']:raise ValueError('export not bound to actual result')
    with safe_path(root/(case_id+'-export.json'),missing=True).open('xb') as stream:stream.write(payload)
    output=dict(request=request,model=model,selection=selection,run=terminal,result=result,review=reviewed['review'],
        comparison=comparison,export=export,export_file_sha256=sha256(payload).hexdigest(),
        native_prediction_count=len(prediction),native_proof_count=len(proof),export_bytes=len(payload),
        enqueue_to_terminal_seconds=enqueue_to_terminal_seconds)
    save(root,case_id+'-predictions.json',list(prediction));save(root,case_id+'-proofs.json',list(proof))
    save(root,case_id+'-execution.json',output)
    return output


def run_inversion_cases(lab,pack,run_root):
    pack=checked_pack(lab,pack);root=safe_path(Path(run_root),missing=True)
    if root.exists() or root.parent!=lab.root:raise ValueError('new owned run path required')
    root.mkdir();private_acl(root);save(root,'run.json',run_context(lab,pack))
    original_sha=sha256((pack.root/'manifest.json').read_bytes()).hexdigest()
    from dataclasses import asdict
    checks=validate_pack_v2(pack)
    save(root,'offline-checks.json',{'checks':[asdict(c) for c in checks]})
    budgets={c.check_id.removesuffix('-finite-inversion'):c for c in checks if c.check_id.endswith('-finite-inversion')}
    if any(c.status!='passed' for c in checks):raise ValueError('offline package assertions failed; no approvals issued')
    journal=root/'command-intents';journal.mkdir();private_acl(journal)
    outcomes=[];wave=V2WaveformSession(lab,root);session=InversionSession(LabHttp(lab,'review'),journal_root=journal)
    model=registered_model(session)
    for case_id in positive_case_ids(pack):
        require_budget(budgets[case_id].actual)
        facts=prepare_case(wave,session,pack,case_id,root)
        actual=approved_execution(session,facts,model,root,case_id)
        outcomes.append(InversionOutcome(case_id,'passed','completed','completed','export','', 'passed',actual))
        save(root,case_id+'-outcome.json',outcome_wire(outcomes[-1]))
    # Negative waveform cases must never create a parameter with a failed
    # finite budget. Adequate missing support is still subject to waveform eligibility.
    for case_id in FAULTS:
        if case_id not in budgets:continue
        report=budgets[case_id].actual
        if not report['passed']:
            outcome=InversionOutcome(case_id,'passed','refused','refused','budget',
                report['reasons'][0] if report['reasons'] else 'insufficient_coverage','refused',report)
        else:
            facts=prepare_case(wave,session,pack,case_id,root)
            require_waveform_refusal(case_id,facts)
            outcome=InversionOutcome(case_id,'passed','refused','refused','waveform','missing_samples','refused',facts)
        outcomes.append(outcome);save(root,case_id+'-outcome.json',outcome_wire(outcome))
    if sha256((pack.root/'manifest.json').read_bytes()).hexdigest()!=original_sha or load_pack(pack.root).manifest!=pack.manifest:
        raise ValueError('original pack changed')
    from .inversion_guards_v2 import run_guards
    outcomes.extend(run_guards(lab,pack,root,session,model))
    save(root,'inversion-outcomes.json',{'schema':'owp.testpack.inversion-outcomes/2','source_kind':'synthetic',
        'field_accuracy_certified':False,'outcomes':[outcome_wire(o) for o in outcomes]})
    finish_context(root)
    return tuple(outcomes)


def outcome_wire(outcome):
    from backend.inversion.queries import wire
    return wire(outcome)
