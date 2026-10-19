"""Predeclared synthetic study inputs. Templates contain no service approvals."""
from copy import deepcopy
from datetime import datetime,timedelta
from hashlib import sha256
import json
import math
import re
from uuid import NAMESPACE_URL,UUID,uuid5

from backend.studies.contracts import validate_envelope
from .contracts import Artifact,Case
from .config import config_dict
from .mechanics import forward_state
from .storage import canonical_bytes,derive_uniform,generator_fingerprint,pack_identity
from .waveforms import decode_channels


def _time(start,minutes=0):
    return (datetime.fromisoformat(start)+timedelta(minutes=minutes)).isoformat(timespec='microseconds').replace('+00:00','Z')


def _training(config,cycles):
    labels=['合成基准','合成高摩擦','合成失衡']
    models=[cycles[i].model for i in (0,2,1)]
    rows=[]
    for split_index,partition in enumerate(('train','validation','test')):
        for i in range(30):
            label_index=i%3
            case=f'bp-{partition}-{i:02d}'
            model=deepcopy(models[label_index])
            for key,amplitude in (('mean_N',300),('half_span_N',100)):
                model['load'][key]+=amplitude*(2*derive_uniform(config.seed,case,key,0)-1)
            model['drive']['crank_friction_Nm']*=.98+.04*derive_uniform(config.seed,case,'friction',0)
            model['bodies']['crank_first_moment_x_kg_m']*=.99+.02*derive_uniform(config.seed,case,'balance',0)
            points=[forward_state(model,8*j/64) for j in range(64)]
            powers=[p['power_W'] for p in points]
            values=[math.fsum(powers)/64,max(powers),min(powers),
                    max(p['load_N'] for p in points)-min(p['load_N'] for p in points)]
            rows.append({'sample_id':case,'cycle_id':case+'-cycle','well_id':f'BP-{partition}-{i//3:02d}',
                         'observed_at':_time(config.start_utc,split_index*1440+i*10),
                         'partition':partition,'label':labels[label_index],'values':values})
    return {'schema':'condition.training/1','features':[{'name':name,'unit':unit} for name,unit in (
        ('mean_power','W'),('peak_power','W'),('minimum_power','W'),('load_span','N'))],
        'rows':rows,'rules':[{'label':label,'clauses':[{'feature':'mean_power','op':'ge','value':value}]}
                            for label,value in zip(labels,(1000,1500,2000))],
        'options':{'seed':config.seed,'hidden_units':8,'epochs':80,'learning_rate':.05,'l2':.001,
                   'confidence_min':.6,'ood_margin':.25}}


def _recovery(config,well):
    return {'schema':'recovery.depth/1','well_id':well,'datum':'Synthetic wellhead datum; depth positive downward',
        'unit':'m','shutdown_at':config.start_utc,'resolution_m':.1,'accuracy_m':.1,
        'policy':{'tau_min_minutes':60,'tau_max_minutes':240,'tau_grid_points':17,'min_r_squared':.99,
                  'max_rmse_m':.01,'max_gap_minutes':60,'max_relative_tau_spread':.1,'max_equilibrium_spread_m':1},
        'observations':[{'at':_time(config.start_utc,t),'depth_m':100+40*math.exp(-t/120),
                         'quality':'valid','pump_state':'off'} for t in (0,30,60,90,120,180,240,300)]}


def _schedule(config,well):
    return {'schema':'energy.schedule/1','horizon_start':config.start_utc,'horizon_end':_time(config.start_utc,1440),
        'production_day_timezone':'Asia/Shanghai','site_power_limit_kw':20,'max_simultaneous_starts':1,
        'wells':[{'well_id':well,'parameter_basis':'Fixed software scenario; no field control settings',
            'initial_depth_m':120,'initial_state':'off','initial_state_elapsed_minutes':60,
            'annulus_storage_area_m2':1,'pump_intake_depth_m':300,'minimum_submergence_m':10,
            'pumping_liquid_rate_m3_h':12,'run_input_power_kw':10,'standby_input_power_kw':1,'startup_input_power_kw':15,
            'startup_duration_minutes':1,'min_on_minutes':120,'min_off_minutes':60,'max_starts_per_calendar_day':8,
            'minimum_liquid_m3_per_calendar_day':192,'terminal_max_depth_m':140,
            'candidates':[{'candidate_id':'on120-off60','segments':
                [{'state':state,'duration_minutes':duration} for _ in range(8) for state,duration in (('on',120),('off',60))]}]}]}


def _effects(config,history):
    metadata=json.loads(next(a.content for a in history[0].artifacts if a.path.endswith('.json')))
    first=metadata['daily'][0]
    duration=120 if config.profile=='smoke' else 1440
    periods=[]
    for offset,liquid,energy in ((0,first['liquid_m3'],first['net_kwh']),
                                  (1440,first['liquid_m3']*.98,first['net_kwh']*.9)):
        start,end=_time(config.start_utc,offset),_time(config.start_utc,offset+duration)
        periods.append({'start':start,'end':end,'intervals':[{'start':start,'end':end,
                        'liquid_m3':liquid,'energy_kwh':energy,'quality':'valid'}]})
    return {'schema':'energy.effects/1','well_id':metadata['well_id'],'baseline':periods[0],'treatment':periods[1],
            'min_coverage_fraction':1,'comparability':{'same_meter':True,'consistent_units':True,
            'similar_operating_conditions':True,'other_changes_recorded':True}}


def _spectrum(config,current):
    data=decode_channels(current)['current_a']
    raw=next(a.content for a in current.artifacts if a.path.endswith('.owpwave'))
    manifest=json.loads(raw.splitlines()[0])
    return {'schema':'motor.spectrum/1','capture':{'capture_id':manifest['capture_id'],'well_id':'TESTPACK-WELL-001',
        'device_id':'TESTPACK-PUMP-001','started_at':config.start_utc,'sample_rate_hz':current.facts['sample_rate_hz'],
        'samples_A':[v if v is not None else 0 for v in data['values']],
        'quality':['valid' if q==0 else 'invalid' for q in data['quality']],
        'clipped':[q==2 for q in data['quality']],
        'provenance':{'sample_kind':'instantaneous','uniform_sampling_confirmed':True,'clock_error_ppm':0,
            'calibration_reference':'synthetic numeric scale 0.0001 A; not a calibration certificate',
            'anti_alias_reference':'analytic band-limited signal; no physical ADC',
            'anti_alias_passband_hz':400,'manifest_sha256':sha256(raw.splitlines()[0]).hexdigest()}},
        'motor':{'type':'squirrel_cage_induction','supply':'line_fed','pole_count':4,'speed_rpm':1440,
                 'speed_uncertainty_rpm':0,'supply_frequency_hz':50,'frequency_uncertainty_hz':0,
                 'steady_state_declared':True,'periodic_load_excluded':True,'basis_reference':'Declared software signal assumptions'},
        'policy':{'maximum_clock_error_ppm':100,'maximum_rms_variation_ratio':1,'minimum_fundamental_A':.1,
                  'minimum_sideband_dbc':-60,'minimum_sideband_to_noise_db':3}}


def _protection(config):
    return {'schema':'motor.protection/1','settings':{
        'version':'synthetic-settings/1','basis_reference':'Software state-machine exercise only; outputs disabled',
        'approved_by':'synthetic actor label; not actual human approval','effective_from':config.start_utc,
        'effective_until':_time(config.start_utc,1440),'maximum_frame_gap_ms':1000,'reset_safe_ms':1000,
        'rules':[{'id':'test-overcurrent','metric':'current_max_A','direction':'high','pickup':20,'dropout':18,'delay_ms':1000}]},
        'frames':[{'at':_time(config.start_utc,i/60),'sequence':i,'boot_id':'synthetic-boot','quality':'valid',
            'configuration_valid':True,'hardware_trip':False,'reset_request':'local' if i==9 else 'none',
            'measurements':{'currents_A':[amps]*3,'voltages_V':[400]*3,'temperature_C':45,'speed_rpm':1440,'frequency_hz':50}}
                  for i,amps in enumerate((10,10,25,25,25,25,10,10,10,10,10))]}


def generate_studies(config,cycles,history,currents):
    pack_id=pack_identity(config,generator_fingerprint())
    refs=[pack_id,'configuration-sha256:'+sha256(canonical_bytes(config_dict(config))).hexdigest()]
    training=_training(config,cycles)
    well='TESTPACK-WELL-001'
    prediction={'schema':'condition.prediction/1','rows':[
        {k:v for k,v in row.items() if k not in ('partition','label')} for row in training['rows'][-6:]],
        'confidence_min':.6,'ood_margin':.25}
    recovery=_recovery(config,well);schedule=_schedule(config,well)
    effects=_effects(config,history);spectrum=_spectrum(config,currents[2]);protection=_protection(config)
    items=[('condition-training','condition_training',training,[], 'completed'),
           ('condition-prediction','condition_prediction',prediction,[{'path':'model_record_id','record_key':'condition_training'}],'completed'),
           ('recovery-fit','recovery_fit',recovery,[],'fitted'),
           ('energy-schedule','energy_schedule',schedule,[{'path':'wells/0/recovery_record_id','record_key':'recovery_fit:'+well}],'feasible'),
           ('energy-effects','energy_effects',effects,[],'descriptive'),
           ('motor-spectrum','motor_spectrum',spectrum,[],'completed'),
           ('protection-replay','protection_replay',protection,[],'completed')]
    short=deepcopy(recovery)
    for row in short['observations'][4:]:row['quality']='invalid'
    infeasible=deepcopy(schedule);infeasible['site_power_limit_kw']=5
    low=deepcopy(effects);low['treatment']['intervals'][0]['quality']='invalid'
    unavailable=deepcopy(spectrum);unavailable['motor']['speed_rpm']=None
    invalid=deepcopy(protection);invalid['settings']['rules'][0]['dropout']=21
    items.extend([('recovery-short','recovery_fit',short,[],'rejected'),
        ('schedule-infeasible','energy_schedule',infeasible,items[3][3],'no_feasible_candidate'),
        ('effects-low-coverage','energy_effects',low,[],'insufficient_evidence'),
        ('spectrum-missing-info','motor_spectrum',unavailable,[],'completed'),
        ('protection-invalid','protection_replay',invalid,[],'invalid_field')])
    cases=[]
    for name,kind,payload,dependencies,status in items:
        case_id='study-'+name
        envelope={'request_id':str(uuid5(NAMESPACE_URL,pack_id+'/'+case_id+'/request')),
            'kind':kind,'title':'合成软件研究：'+name,
            'source':{'kind':'software_fixture','description':'Reproducible synthetic inputs, not field measurements or human approvals',
                      'references':refs.copy()},'payload':payload}
        template={'schema':'owp.testpack.study-template/1','envelope':envelope,'dependencies':dependencies}
        path=f'templates/{case_id}.json'
        artifacts=[Artifact(path,'application/json',canonical_bytes(template))]
        if not dependencies:
            validate_envelope(envelope)
            artifacts.append(Artifact(f'studies/{case_id}.json','application/json',canonical_bytes(envelope)))
        category='protocol_fault' if name=='protection-invalid' else 'signal_fault' if name in (
            'recovery-short','spectrum-missing-info','effects-low-coverage') else 'mechanistic'
        cases.append(Case(case_id,category,tuple(artifacts),{'data_kind':'study','study_kind':kind,
            'requires_binding':bool(dependencies),'source_kind':'software_fixture',
            'input_note':'effects treatment is a fixed hypothetical 0.98 liquid / 0.90 energy scenario; not measured savings'},
            ({'check_id':case_id+'-engine','layer':'numeric','assertion':'rejects' if name=='protection-invalid' else 'equals',
              'target':path,'expected':status},)))
    return tuple(cases)


def bind_study(template:dict,record_ids:dict[str,str],pack_sha256:str)->dict:
    if (not isinstance(template,dict) or set(template)!={'schema','envelope','dependencies'}
            or template['schema']!='owp.testpack.study-template/1' or not re.fullmatch('[0-9a-f]{64}',pack_sha256)):
        raise ValueError('invalid study binding template/hash')
    envelope=deepcopy(template['envelope'])
    for dependency in template['dependencies']:
        if set(dependency)!={'path','record_key'} or dependency['record_key'] not in record_ids:
            raise ValueError('actual dependency record ID required')
        record=record_ids[dependency['record_key']]
        UUID(record)
        path=dependency['path']
        if path=='model_record_id' and envelope['kind']=='condition_prediction':
            envelope['payload'][path]=record
        elif re.fullmatch(r'wells/[0-3]/recovery_record_id',path) and envelope['kind']=='energy_schedule':
            index=int(path.split('/')[1]);well=envelope['payload']['wells'][index]
            if dependency['record_key']!='recovery_fit:'+well['well_id']:
                raise ValueError('recovery dependency belongs to another well')
            well['recovery_record_id']=record
        else:
            raise ValueError('unsupported dependency location')
    envelope['source']['references'].append('manifest-sha256:'+pack_sha256)
    # Binding content, not wall-clock time, makes retries stable.
    envelope['request_id']=str(uuid5(NAMESPACE_URL,sha256(canonical_bytes(envelope)).hexdigest()))
    validate_envelope(envelope)
    return envelope
