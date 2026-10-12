"""Illustrative, fixed-input card/parameter/result fixtures, never a run service."""
from functools import lru_cache
import math
from backend.algorithms.dynagraph_profiles import PROFILE_DEFINITIONS,build_profile_card
from .acquisition import uid,digest,SOURCE,STAMP

@lru_cache(maxsize=1)
def cards():
    output={}
    profiles={**PROFILE_DEFINITIONS,'intermittent':{'label':'间歇供液','diagnosis_code':'intermittent'},'pump_collision':{'label':'碰泵','diagnosis_code':'intermittent'}}
    for code,profile in profiles.items():
        payload=build_profile_card('downstroke_hang' if code=='pump_collision' else code,{'stroke_m':3.63,'strokes_per_minute':3.52},variation=.2)
        parameters=payload['mechanism'];points=[];intermediate=[]
        for sample,sensor in zip(payload['electric_samples'],payload['sensor_samples']):
            phase=sample['phase'];theta=math.radians(phase)
            load=(sensor['load']-.12-.07*math.sin(3*theta+.2))/1.006
            points.append({'displacement_m':3.63*(1-math.cos(theta))/2,'load_kn':load,'phase_deg':phase})
            intermediate.append({'phase_deg':phase,'power_kw':sample['power'],'motor_torque_nm':sample['power']*80,'crank_torque_nm':(load-36)*1815*math.sin(theta),'displacement_m':points[-1]['displacement_m'],'load_kn':load})
        loads=[p['load_kn'] for p in points]
        area=abs(sum(p['displacement_m']*q['load_kn']-q['displacement_m']*p['load_kn'] for p,q in zip(points,points[1:]+points[:1])))/2
        features={'max_load_kn':max(loads),'min_load_kn':min(loads),'load_difference_kn':max(loads)-min(loads),'area_kn_m':area,'fillage':.88 if code=='normal' else .58,'upstroke_load_kn':sum(loads[:60])/60,'downstroke_load_kn':sum(loads[60:])/60,'closure_error':0,'turning_points':4}
        condition=profile['diagnosis_code'];identifier='preview-card-'+code
        conversion={'points':points,'intermediate':intermediate,'parameters':parameters,'sensor_points':payload['sensor_samples'],'profile_code':code,'profile_label':profile['label'],'comparison':{'mae_kn':.42,'rmse_kn':.45,'max_error_kn':.61,'area_error_percent':.8,'stroke_error_m':.015,'correlation':.998,'upstroke_rmse_kn':.48,'downstroke_rmse_kn':.4},'model_version':'preview-shape-reference/1','energy_balance':{'available':False,'reason':'仅固定图形测试样例'}}
        diagnosis={'id':'preview-diagnosis-'+code,'condition':condition,'confidence':.92,'status':'reviewed','review_version':1,'template_id':'preview-template-'+code,'template_version':'1','recommendation':'软件形态样例，仅用于页面对照；实际诊断需现场数据与独立验证。','candidates':[{'condition':condition,'combined_score':.92,'template_score':.95,'rule_score':.88,'sample_count':1,'template_version':'1'}],'all_rule_hits':[{'description':'图形载荷差展示样例','condition':condition,'observed':features['load_difference_kn'],'threshold':20,'severity':.5}]}
        output[code]={'id':identifier,'well_id':'preview-reference','well_name':profile['label']+'参考井','device_id':'preview-terminal','captured_at':STAMP,'status':'analyzed','source_type':SOURCE,'source_kind':SOURCE,'model_version':'preview-shape-reference/1','profile_code':code,'profile_label':profile['label'],'parameters':parameters,'features':features,'converted_points':points,'sensor_points':payload['sensor_samples'],'comparison':conversion['comparison'],'sample_data':payload,'conversion':conversion,'latest_condition':condition,'latest_confidence':.92,'latest_diagnosis':diagnosis,'latest_model_run_id':'preview-run-'+code,'latest_model_run':None,'latest_production':{'estimated_m3_day':13.6 if code=='normal' else 5.8,'pump_efficiency':.61,'status':'illustrative'},'reviews':[]}
    return output


def inversion_fixtures():
    a,p,m,run,result=[uid(x) for x in ['assembly','parameter','model','inversion-run','inversion-result']]
    start='2026-01-01T00:00:00Z';end='2027-01-01T00:00:00Z'
    evidence={'id':uid('evidence'),'content_sha256':digest('fixture-evidence'),'purpose':'software_fixture','source_description':'固定软件场景，无现场认证'}
    mechanical={'asset_number':'PREVIEW-CYJ-01','manufacturer':'开放参考设计','model':'游梁式参考总成','mechanism':'conventional_crank_balanced','source_kind':'synthetic','source_organization':'软件测试场景','valid_from':start,'valid_to':end,'installation_evidence':[evidence]}
    body={'schema':'inversion.parameter-set/1','assembly_id':a,'assembly_version':1,'source_kind':'synthetic','valid_from':start,'valid_to':end,'parent_version':None,
          'geometry':{'pivot_x_m':'1.25','pivot_y_m':'3.5','crank_radius_m':'1.2','pitman_length_m':'3.2','rear_arm_m':'2.4','horsehead_radius_m':'3.1','branch':1,'rotation_sign':1,'displacement_sign':1,'evidence_refs':[evidence]},
          'phase':{'zero_reading_rad':'0','encoder_direction':1,'unwrap_kind':'wrapped_2pi','evidence_refs':[evidence]},
          'bodies':{'crank_inertia_kg_m2':'420','beam_inertia_kg_m2':'3800','pitman_mass_kg':'520','pitman_com_fraction':'0.5','pitman_inertia_kg_m2':'85','gravity_m_s2':'9.80665','evidence_refs':[evidence]},
          'drive':{'supply_kind':'direct_line_induction','ratio':'30','upstream_inertia_kg_m2':'8','crank_friction_Nm':'15','viscous_friction_Nm_s':'2','evidence_refs':[evidence]},
          'domain':{'load_N':{'min':'20000','max':'90000'},'power_W':{'min':'-15000','max':'30000'},'temperature_K':{'min':'273','max':'323'},'evidence_refs':[evidence]},
          'uncertainty':{'torque_bound_Nm':'25','factor_bound_m':'0.01','speed_bound_rad_s':'0.002','numerical_budget_Nm':'1','numerical_factor_budget_m':'0.001','evidence_refs':[evidence]},
          'provenance':{'source_organization':'软件测试场景','notes':'参数用于页面审查，不构成经现场核验的可执行参数集','operating_conditions':[{'id':'preview-condition','valid_from':start,'valid_to':end,'temperature_K':{'min':'273','max':'323'},'supply_voltage_V':{'min':'360','max':'400'}}],'attachment_pins':[evidence],'channel_bindings':[],'calibration_cycles':[],'coverage':{'max_time_alignment_error_ns':'500000','scopes':[]}}}
    parameter={'id':p,'assembly_id':a,'assembly_version':1,'version':1,'content_sha256':digest(body),'review_version':1,'review_state':'approved','review_action':'approve','valid_from':start,'valid_to':end,'body':body}
    model={'id':m,'installed_id':'beam_energy_balance/1','version':1,'content_sha256':digest('preview-model'),'review_version':1,'review_action':'approve','review_state':'approved'}
    selection={'id':uid('selection'),'version':1,'body':{'assembly_id':a,'parameter_id':p,'parameter_sha256':parameter['content_sha256'],'parameter_review_version':1,'model_id':m,'model_sha256':model['content_sha256'],'model_review_version':1}}
    assembly={'id':a,'version':1,'body':mechanical,'valid_from':start,'valid_to':end};base='/api/production-inversion'
    page=lambda items:{'items':items,'offset':0,'limit':20}
    routes={base+'/assemblies':page([assembly]),base+'/assemblies/'+a:assembly,base+'/parameters':page([parameter]),base+'/parameters/'+p:parameter,base+'/models':page([model]),base+'/models/'+m:model,base+'/assemblies/'+a+'/selections':page([selection]),base+'/assemblies/'+a+'/versions':page([{'assembly_id':a,'version_id':uid('mechanical-version'),'version':1,'content_sha256':digest(mechanical),'declared':{'interval_known':True,'body':mechanical,'valid_from':start,'valid_to':end},'effective':{'revision':1,'valid_from':start,'valid_to':end}}])}
    rows=[];card=cards()['normal']
    for i,point in enumerate(card['conversion']['points']):
        rows.append({'kind':'prediction','columns':['time_ns','power_index','s_m','load_N','status','reason_codes','power_W','theta_rad','drive_Nm'],'values':[str(i*100000000),str(i),str(point['displacement_m']),str(point['load_kn']*1000),'valid',[],str(card['sample_data']['electric_samples'][i]['power']*1000),str(math.radians(i*3)),str(card['conversion']['intermediate'][i]['crank_torque_nm'])]})
    for i,point in enumerate(card['sensor_points']):
        rows.append({'kind':'comparison','columns':['time_ns','power_index','valid','reason_codes','load_value_si','load_indices','load_source','displacement_value_si','displacement_indices','displacement_source'],'values':[str(i*100000000),str(i),True,[],str(point['load']*1000),[str(i)],0,str(point['displacement']),[str(i)],0]})
    metric=lambda value,unit='N',reason=None:{'value':value,'unit':unit,'reason':reason}
    comparison={'scope':'original_valid_support','prediction_status':'applicable','prediction_reason_codes':[],'primary':{'load_mae':metric('420'),'load_correlation':metric('0.998','1'),'load_relative_mae_percent':metric('0.8','%')},'by_stroke':{'up':{'load_mae':metric('480')},'down':{'load_mae':metric('400')}},'coverage':{'joint_fraction':metric('1','1'),'prediction_numerical_fraction':metric('1','1')},'work':{'prediction_complete_cycle':metric(None,'J','software_fixture_only')},'invalid_reasons':{},'independence':{'status':'not_independent','reason':'software_fixture_only'}}
    hashed=digest(rows);runrow={'run_id':run,'version':1,'state':'completed','result_id':result,'source_kind':SOURCE,'request':{'assembly_id':a,'selection_version':1}}
    resultrow={'id':result,'run_id':run,'content_sha256':hashed,'snapshot_sha256':digest(body),'request_sha256':digest(selection),'execution':'completed','source':'synthetic','review':{'state':'unreviewed','version':0},'current_applicability':{'state':'restricted','reasons':['software_fixture_only']},'field_validation':{'state':'not_field_validated','certified':False},'body':{'point_count':120,'valid_point_count':120,'prediction_status':'applicable','prediction_reason_codes':[],'coverage_fraction':'1','cycle':{'start_ns':'0','end_ns':'12000000000'},'reference_sources':[{'gateway_id':'preview-gateway-01','device_id':'preview-terminal-001'}],'comparison':comparison,'filled_comparison':None},'source_kind':SOURCE}
    routes.update({base+'/runs':page([runrow,{'run_id':uid('blocked-run'),'state':'blocked','result_id':None,'reason_code':'not_applicable','source_kind':SOURCE}]),base+'/runs/'+run:runrow,base+'/runs/'+uid('blocked-run'):{'run_id':uid('blocked-run'),'version':1,'state':'blocked','reason_code':'not_applicable','result_id':None,'terminal_diagnostics':{'run_id':uid('blocked-run'),'version':1,'state':'blocked','reason_code':'not_applicable','reasons':['insufficient_coverage'],'coverage_fraction':'1/3','point_count':120,'valid_point_count':40}},base+'/results/'+result:resultrow,base+'/results/'+result+'/comparison':{'result_id':result,'content_sha256':hashed,'comparison':comparison,'filled_comparison':None},base+'/results/'+result+'/points':{'result_id':result,'content_sha256':hashed,'offset':0,'limit':1000,'total':len(rows),'rows':rows}})
    return routes
