"""Fixed, explicitly illustrative input/output pairs; no algorithm execution."""
from copy import deepcopy
from functools import lru_cache
import hashlib,json,math
from .acquisition import uid,STAMP

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

@lru_cache(maxsize=1)
def records():
    labels=['normal','supply_shortage','rod_break'];metrics={}
    for key,matrix in [('rule',[[9,1,0,0],[1,7,0,2],[0,0,9,1]]),('centroid',[[9,0,0,1],[0,8,1,1],[0,0,9,1]]),('bp',[[9,0,0,1],[0,9,0,1],[0,0,9,1]])]:
        accepted=sum(sum(r[:3]) for r in matrix);correct=sum(matrix[i][i] for i in range(3));metrics[key]={'coverage':accepted/30,'accuracy':correct/30,'accepted_accuracy':correct/accepted,'macro_F1':sum(2*matrix[i][i]/(sum(matrix[i])+sum(r[i] for r in matrix)) for i in range(3))/3,'unknown_count':30-accepted,'labels':labels,'prediction_columns':labels+['unknown'],'confusion_matrix':matrix}
    recovery=[{'at':f'2026-09-10T00:{i*5:02d}:00Z','observed_m':round(1050+160*math.exp(-i/3)+math.sin(i)*2,3),'predicted_m':round(1050+160*math.exp(-i/3),3),'residual_m':round(math.sin(i)*2,3)} for i in range(12)]
    spectrum={'frequency_hz':[i*.25 for i in range(321)],'amplitude_A_peak':[round(.02+12*math.exp(-((i*.25-50)/.3)**2)+.5*math.exp(-((i*.25-48)/.3)**2)+.45*math.exp(-((i*.25-52)/.3)**2),5) for i in range(321)],'bin_spacing_hz':.25}
    specs=[
      ('condition_training','分井分时隔离工况训练',{'split_policy':'well_and_time_disjoint','train_wells':['T01','T02'],'validation_wells':['V01'],'test_wells':['E01'],'labels':labels,'epochs':24},
       {'status':'completed','training_curve':[{'epoch':i,'train_loss':round(.95*math.exp(-i/6)+.08,4),'validation_loss':round(.9*math.exp(-i/7)+.13+max(0,i-18)*.008,4)} for i in range(1,25)],'metrics':metrics,'best_epoch':18,'data_digest':digest(labels)}),
      ('condition_prediction','已知工况与拒识样本',{'model_record_id':uid('study-condition_training'),'samples':['A01','A02','A03','A04']},
       {'status':'completed','predictions':[{'sample_id':'A01','label':'normal','score':.91,'reasons':[]},{'sample_id':'A02','label':'supply_shortage','score':.86,'reasons':[]},{'sample_id':'A03','label':'rod_break','score':.95,'reasons':[]},{'sample_id':'A04','label':'unknown','score':None,'reasons':['outside_training_domain']}]}),
      ('recovery_fit','关井液面恢复经验拟合',{'well_id':'TEST-019','observations':[{k:r[k] for k in ('at','observed_m')} for r in recovery],'depth_datum':'wellhead','unit':'m'},
       {'status':'fitted','parameters':{'tau_minutes':15,'asymptotic_depth_m':1050},'metrics':{'rmse_m':round((sum(r['residual_m']**2 for r in recovery)/12)**.5,4),'r_squared':.999},'fitted_points':recovery,'accepted_count':12,'excluded_count':0}),
      ('energy_schedule','多井影子启停组合',{'recovery_record_id':uid('study-recovery_fit'),'max_concurrent_wells':2,'max_starts_per_day':16,'candidate_on_minutes':[45,60],'candidate_off_minutes':[30,45]},
       {'status':'feasible','evaluated_combinations':16,'ranked_candidates':[{'candidate_id':'schedule-A','total_energy_kwh':312,'peak_power_kw':22,'total_starts':24,'schedule_by_well':{w:{'segments':[{'state':'on','duration_minutes':60},{'state':'off','duration_minutes':45},{'state':'on','duration_minutes':60}]} for w in ['TEST-019','TEST-020']},'daily_liquid_m3':{'2026-09-10':24.8},'daily_energy_kwh':{'2026-09-10':312},'daily_starts':{'2026-09-10':24}}]}),
      ('energy_effects','基线与处理窗口描述性比较',{'baseline':['2026-09-01','2026-09-07'],'treatment':['2026-09-08','2026-09-14'],'minimum_coverage':.9},
       {'status':'descriptive','baseline':{'coverage_fraction':.96,'observed_hours':161.28,'liquid_m3':87.1,'energy_kwh':1480,'energy_kwh_per_observed_hour':1480/161.28},'treatment':{'coverage_fraction':.95,'observed_hours':159.6,'liquid_m3':85.8,'energy_kwh':1310,'energy_kwh_per_observed_hour':1310/159.6},'comparison':{'energy_kwh_per_observed_hour_difference':1310/159.6-1480/161.28},'reason_codes':['descriptive_only_no_causal_claim']}),
      ('motor_spectrum','单相电流侧频证据',{'sample_rate_hz':512,'sample_count':2048,'mains_hz':50,'slip':.02,'unit':'A peak','source':'fixed spectrum illustration'},
       {'status':'completed','support':'software_fixture_only','finding':'侧频示例，不能单独确诊断条','spectrum':spectrum,'reasons':['侧频间隔约 2 Hz'],'confounders':['负载波动与变频器谐波可能造成相似谱线','需要转速、负载和重复采样验证']}),
      ('protection_replay','过流资格、延时和本地复位回放',{'threshold_A':40,'trip_delay_ms':300,'reset_policy':'local_manual'},
       {'status':'completed','timeline':[{'frame_index':i,'at':f'2026-09-10T02:00:0{i}Z','qualification':'valid' if i!=5 else 'stale','rules':{'overcurrent':{'state':s}},'inhibit':i==5,'reset_decision':'local_ack_required' if i>2 else 'not_applicable'} for i,s in enumerate(['normal','qualifying','qualifying','would_trip','latched','inhibited','latched','reset'])],'events':[{'frame_index':3,'kind':'would_trip','rule_id':'overcurrent'},{'frame_index':7,'kind':'local_reset','rule_id':'overcurrent'}]})]
    result=[]
    for kind,title,payload,out in specs:
        selected=kind in {'condition_training','energy_schedule','protection_replay'}
        result.append({'id':uid('study-'+kind),'kind':kind,'title':title,'actor_id':'preview-fixture-author','created_at':STAMP,'schema_version':'1.0','algorithm_version':'fixed-reference-vector-v1','source':{'kind':'software_fixture','description':'固定页面输入结果对，用于页面与交互审查；不是本次计算或现场验证结果。','references':['preview/fixtures/studies.py v1']},'source_kind':'software_fixture','status':out['status'],'payload':payload,'result':out,'input_sha256':digest(payload),'result_sha256':digest(out),'version':1,'review_status':'approved','is_selected':selected,'selection_version':1,'review_scope':'software_study_only','selection_scope':'shadow_only','field_validation':False,'control_eligible':False,'dependency_current':True,'eligible':True,'dependencies':([{'record_id':uid('study-condition_training' if kind=='condition_prediction' else 'study-recovery_fit'),'kind':'condition_training' if kind=='condition_prediction' else 'recovery_fit','review_version':1}] if kind in {'condition_prediction','energy_schedule'} else []),'review_history':[{'version':1,'decision':'approved','actor_id':'preview-fixture-reviewer','created_at':STAMP,'comment':'仅用于软件页面测试'}],'selection_history':[{'version':1,'record_id':uid('study-'+kind),'actor_id':'preview-fixture-reviewer','created_at':STAMP,'comment':'固定影子测试版本'}] if selected else []})
    for kind,status,reasons in [('recovery_fit','rejected',['insufficient_observations']),('energy_schedule','no_feasible_candidate',['peak_power_exceeded']),('energy_effects','insufficient_evidence',['coverage_below_threshold'])]:
        row=deepcopy(next(r for r in result if r['kind']==kind));row.update(id=uid('study-rejected-'+kind),title=row['title']+' · 证据不足',status=status,review_status='rejected',is_selected=False,eligible=False,selection_history=[])
        row['result']={'status':status,'reason_codes':reasons,'accepted_count':2,'excluded_count':10,'evaluated_combinations':16,'no_feasible_evidence':{'rejection_counts':{'peak_power_exceeded':16}}};row['result_sha256']=digest(row['result']);row['review_history'][0]['decision']='rejected';result.append(row)
    return result
