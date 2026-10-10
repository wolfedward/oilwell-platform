import {element, mountActionForm, errorText} from './forms.js';

const REQUIRED_GROUPS = ['provenance','geometry','phase','bodies','drive','domain','uncertainty','policy'];
const COVERAGE_SCOPES = ['loss_calibration','time_alignment','averaged_power_mismatch','motion_angle_policy','upstream_dynamics','load_range','geometry_envelope','crank_friction'];
const REQUIRED_FIELDS = {
  provenance:['source_organization','source_date','attachment_pins','calibration_cycles','operating_conditions','channel_bindings','notes','coverage'],
  geometry:['pivot_x_m','pivot_y_m','crank_radius_m','pitman_length_m','rear_arm_m','horsehead_radius_m','branch','rotation_sign','displacement_sign','evidence_refs'],
  phase:['channel_pin','zero_reading_rad','encoder_direction','unwrap_kind','evidence_refs'],
  bodies:['crank_inertia_kg_m2','crank_first_moment_x_kg_m','crank_first_moment_y_kg_m','beam_inertia_kg_m2','beam_first_moment_x_kg_m','beam_first_moment_y_kg_m','pitman_mass_kg','pitman_com_fraction','pitman_inertia_kg_m2','gravity_m_s2','component_ledger','evidence_refs'],
  drive:['supply_kind','ratio','upstream_inertia_kg_m2','loss_grid','crank_friction_Nm','viscous_friction_Nm_s','evidence_refs'],
  domain:['power_W','speed_rad_s','acceleration_rad_s2','temperature_K','supply_voltage_V','supply_frequency_Hz','bandwidth_Hz','load_N','window_ns','evidence_refs'],
  uncertainty:['torque_bound_Nm','factor_bound_m','speed_bound_rad_s','numerical_budget_Nm','numerical_factor_budget_m','evidence_refs'],
  policy:['body','content_sha256'],
};
function missingFields(body) {
  const missing=[];
  for(const key of ['schema','assembly_id','assembly_version','source_kind','valid_from','valid_to','parent_version'])if(!(key in body))missing.push(key);
  for(const group of REQUIRED_GROUPS){
    const value=body[group];if(!value){missing.push(group);continue;}
    for(const field of REQUIRED_FIELDS[group])if(value[field]===undefined||value[field]===null)missing.push(`${group}.${field}`);
  }
  for(const [root,fields] of [['provenance.coverage',['policy_sha256','domain_sha256','uncertainty_sha256','max_time_alignment_error_ns','scopes']],
    ['drive.loss_grid',['power_W','motor_speed_rad_s','loss_W','error_W','evidence_refs']]]){
    const nested=root.split('.').reduce((value,key)=>value?.[key],body);
    if(nested)for(const field of fields)if(nested[field]===undefined||nested[field]===null)missing.push(`${root}.${field}`);
  }
  const supplied=new Set((body.provenance?.coverage?.scopes||[]).map(item=>item.scope));
  for(const scope of COVERAGE_SCOPES)if(!supplied.has(scope))missing.push(`provenance.coverage.scopes.${scope}`);
  const roles=new Set((body.provenance?.channel_bindings||[]).map(item=>item.role));
  for(const role of ['active_power','crank_angle','load','displacement'])if(!roles.has(role))missing.push(`provenance.channel_bindings.${role}`);
  return missing;
}
const UNITS = [
  ['geometry','pivot_x_m','m'],['geometry','pivot_y_m','m'],['geometry','crank_radius_m','m'],
  ['geometry','pitman_length_m','m'],['geometry','rear_arm_m','m'],['geometry','horsehead_radius_m','m'],
  ['phase','zero_reading_rad','rad'],['bodies','crank_inertia_kg_m2','kg·m²'],
  ['bodies','beam_inertia_kg_m2','kg·m²'],['bodies','pitman_mass_kg','kg'],
  ['bodies','gravity_m_s2','m/s²'],['drive','upstream_inertia_kg_m2','kg·m²'],
  ['drive','crank_friction_Nm','N·m'],['drive','viscous_friction_Nm_s','N·m·s'],
  ['uncertainty','torque_bound_Nm','N·m'],['uncertainty','factor_bound_m','m'],
];

function line(root, label, value) {
  const shown=value===null||value===undefined?'未提供':typeof value==='object'?JSON.stringify(value):value;
  const row=element('div',undefined,'inversion-row'); row.append(element('dt',label),element('dd',shown)); root.append(row);
}
function panel(root,title) {const section=element('section',undefined,'panel inversion-panel');section.append(element('h2',title));root.append(section);return section;}
function status(root,text) {root.replaceChildren(element('p',text,'inversion-state'));}
function version(number){return Number.isSafeInteger(number)&&number>0?{kind:'match',version:number}:{kind:'absent'};}
function requireBody(condition,message){if(!condition)throw Object.assign(new Error(message),{code:'input_invalid'});}

export function mountProductionParameters(ctx, client) {
  const root=element('section',undefined,'production-parameters');ctx.workspace.append(root);
  let disposed=false,generation=0;const forms=[];
  const current=()=>!disposed&&(ctx.isCurrent?.()??true)&&ctx.workspace.contains(root);
  async function load() {
    const n=++generation;forms.splice(0).forEach(form=>form.dispose());root.replaceChildren(element('p','正在读取机械参数与审批状态…'));
    try {
      const [assemblies,parameters,models]=await Promise.all([
        client.get('/assemblies'),client.get('/parameters'),client.get('/models')]);
      if(!current()||n!==generation||!assemblies||!parameters||!models)return;
      const chosen=parameters.items.find(item=>item.id===ctx.params?.id)||parameters.items[0];
      const assembly=assemblies.items.find(item=>item.id===(ctx.params?.assembly_id||chosen?.assembly_id))||assemblies.items[0];
      const detail=chosen?await client.get(`/parameters/${chosen.id}`):null;
      const selections=assembly&&ctx.permissions.has('inversion.models.manage')?await client.get(`/assemblies/${assembly.id}/selections`,'inversion.models.manage'):null;
      if(!current()||n!==generation)return;
      render({assemblies,parameters,models,assembly,detail,selections});
    }catch(error){if(current()&&n===generation)status(root,errorText(error));}
  }
  function addForm(host,spec){forms.push(mountActionForm(host,client,spec,load));}
  function render({assemblies,parameters,models,assembly,detail,selections}) {
    root.replaceChildren();root.append(element('p','机械身份、来源附件、物理有效期、参数复核和模型选用分别记录。附件身份与用途不等于现场资格；独立复核也不认证现场精度。','inversion-notice'));
    const mechanical=panel(root,'当前机械配置与有效期');
    if(!assembly)mechanical.append(element('p','暂无机械总成记录。'));
    else {
      const body=assembly.body||{},dl=element('dl',undefined,'inversion-details');mechanical.append(dl);
      for(const key of ['id','version'])line(dl,key,assembly[key]);
      for(const key of ['manufacturer','model','asset_number','mechanism','source_kind','source_organization'])line(dl,key,body[key]);
      line(dl,'声明有效期',`${body.valid_from||'未提供'} — ${body.valid_to||'开放'}`);
      line(dl,'生效有效期',`${assembly.valid_from||'未提供'} — ${assembly.valid_to||'开放'}`);
      const evidence=body.installation_evidence||[];
      for(const pin of evidence)line(dl,'安装来源附件',`${pin.id} · ${pin.purpose} · ${pin.content_sha256} · ${pin.source_description}`);
    }
    if(ctx.permissions.has('inversion.parameters.manage')) {
      const manage=panel(root,'机械登记与参数草稿');
      addForm(manage,{label:'登记新机械总成',action:'assemblies.create',path:'/assemblies',permission:'inversion.parameters.manage',bodyEditor:true,bodyLabel:'机械身份、物理有效期和安装附件 JSON',expected:{kind:'absent'},
        validateBody:body=>requireBody(body?.parent_version===null&&!assemblies.items.some(item=>item.id===body.assembly_id),'新机械总成需要新的 UUID 和 parent_version:null')});
      if(assembly){
        addForm(manage,{label:'登记机械后续版本',action:'assemblies.create',path:'/assemblies',permission:'inversion.parameters.manage',bodyEditor:true,bodyLabel:'后续机械声明与真实安装附件 JSON',expected:version(assembly.version),slot:'assemblies.create:successor:'+assembly.id,
          validateBody:body=>requireBody(body?.assembly_id===assembly.id&&body?.parent_version===assembly.version,'后续机械版本必须指向当前总成和版本')});
        addForm(manage,{label:'登记新井归属',action:'assignments.create',path:'/assignments',permission:'inversion.parameters.manage',bodyEditor:true,bodyLabel:'井 UUID 或 test:<name> 与归属有效期 JSON',expected:{kind:'absent'},
          validateBody:body=>requireBody(body?.assembly_id===assembly.id&&body?.close_assignment_id===null,'新井归属需指定当前总成；历史归属关闭需先核对其独立版本')});
        const latest=chosenVersion(parameters,assembly);
        addForm(manage,{label:'保存参数草稿',action:'parameters.create',path:'/parameter-versions',permission:'inversion.parameters.manage',bodyEditor:true,bodyLabel:'inversion.parameter-set/1 严格 JSON',expected:version(latest),
          validateBody:body=>requireBody(body?.assembly_id===assembly.id&&body?.parent_version===(latest||null),'参数草稿必须指向当前总成及其参数前序版本')});
      }
      manage.append(element('p','请填入完整 inversion.parameter-set/1 JSON。数值按接口使用十进制字符串；不自动填入机械常数、来源或审批。','inversion-muted'));
    }
    const history=panel(root,'参数版本历史');
    const rows=(parameters.items||[]).filter(item=>!assembly||item.assembly_id===assembly.id);
    if(!rows.length)history.append(element('p','尚无参数版本。'));
    for(const item of rows){
      const row=element('button',`${item.id} · 第 ${item.version} 版 · 复核版本 ${item.review_version??1} · ${item.review_action||'draft'}`,'button inversion-history');
      row.type='button';row.addEventListener('click',()=>ctx.navigate?.('production-parameters',{id:item.id,assembly_id:item.assembly_id}));history.append(row);
    }
    const physical=panel(root,'参数字段、单位与证据');
    if(!detail)physical.append(element('p','尚无参数草稿；必需组：'+REQUIRED_GROUPS.join('、')));
    else {
      const body=detail.body||{},dl=element('dl',undefined,'inversion-details');physical.append(dl);
      line(dl,'参数版本',`第 ${detail.version} 版 · 复核版本 ${detail.review_version} · ${detail.review_state}`);
      line(dl,'内容摘要',detail.content_sha256);
      line(dl,'来源',body.source_kind);
      line(dl,'声明有效期',`${body.valid_from||'未提供'} — ${body.valid_to||'开放'}`);
      line(dl,'缺少参数 / 证据',missingFields(body).join('、')||'当前必需字段均已提供；提交时仍需服务端核验字段、附件和覆盖范围');
      for(const group of REQUIRED_GROUPS){
        const details=element('details',undefined,'inversion-group');details.open=group==='geometry'||group==='provenance';
        details.append(element('summary',group));const fields=element('dl',undefined,'inversion-details');details.append(fields);physical.append(details);
        for(const key of REQUIRED_FIELDS[group]){
          const unit=UNITS.find(item=>item[0]===group&&item[1]===key)?.[2];
          line(fields,`${group}.${key}${unit?` (${unit})`:''}`,body[group]?.[key]);
        }
        const refs=body[group]?.evidence_refs||[];
        for(const pin of refs)line(dl,`${group} 来源附件`,`${pin.id} · ${pin.purpose} · ${pin.content_sha256} · ${pin.source_description}`);
      }
      const coverage=body.provenance?.coverage;
      if(coverage){
        line(dl,'最大时间对齐误差 (ns)',coverage.max_time_alignment_error_ns);
        for(const scope of coverage.scopes||[])line(dl,`覆盖范围 ${scope.scope}`,`${scope.basis} · 附件 ${scope.attachment_pin?.id||'未提供'} · 用途 ${scope.attachment_pin?.purpose||'未提供'} · ${scope.attachment_pin?.source_description||''}`);
      }
      if(ctx.permissions.has('inversion.parameters.manage')&&detail.review_state==='draft')addForm(physical,{label:'提交参数复核',action:'parameters.submit',path:`/parameters/${detail.id}/submit`,permission:'inversion.parameters.manage',expected:version(detail.review_version)});
      if(ctx.permissions.has('inversion.parameters.review')&&detail.review_state==='submitted'){
        physical.append(element('p','独立复核：作者或提交者不能审批自己的参数，管理员也不能绕过此限制。','inversion-muted'));
        addForm(physical,{label:'记录独立复核',action:'parameters.review',path:`/parameters/${detail.id}/review`,permission:'inversion.parameters.review',expected:version(detail.review_version),decisions:[['approve','批准'],['reject','驳回']]});
      }
      if(ctx.permissions.has('inversion.parameters.review')&&detail.review_state==='approved')addForm(physical,{label:'撤销参数批准',action:'parameters.revoke',path:`/parameters/${detail.id}/revoke`,permission:'inversion.parameters.review',expected:version(detail.review_version)});
    }
    const modelPanel=panel(root,'已登记模型与当前选用');
    for(const item of models.items||[])modelPanel.append(element('p',`${item.installed_id} · ${item.id} · 第 ${item.version} 版 · 复核版本 ${item.review_version??1} · ${item.review_action||'unreviewed'}`));
    if(!(models.items||[]).length)modelPanel.append(element('p','尚无已登记模型。'));
    if(ctx.permissions.has('inversion.models.manage')){
      modelPanel.append(element('p','可登记的安装模型：beam_energy_balance/1（服务端安装清单；不接受上传代码或路径）。','inversion-muted'));
      addForm(modelPanel,{label:'登记已安装模型',action:'models.create',path:'/models',permission:'inversion.models.manage',fixedBody:{installed_id:'beam_energy_balance/1'},expected:{kind:'absent'}});
    }
    if(ctx.permissions.has('inversion.models.review'))for(const item of models.items||[])if(!item.review_action)addForm(modelPanel,{label:`复核模型 ${item.id}`,action:'models.review',path:`/models/${item.id}/review`,permission:'inversion.models.review',expected:version(item.review_version),decisions:[['approve','批准'],['reject','驳回']]});
    if(selections){
      modelPanel.append(element('h3','选用历史'));
      for(const item of selections.items||[])modelPanel.append(element('p',`第 ${item.version} 版 · ${item.id} · 参数 ${item.parameter_id} · 模型 ${item.model_id}`));
      if(!(selections.items||[]).length)modelPanel.append(element('p','尚未选用模型。'));
      addForm(modelPanel,{label:'选用已批准参数与模型',action:'selections.create',path:'/selections',permission:'inversion.models.manage',bodyEditor:true,bodyLabel:'当前总成、参数及模型摘要与复核版本 JSON',expected:version(selections.items?.[0]?.version)});
      for(const item of (selections.items||[]).slice(1))addForm(modelPanel,{label:`回退至历史选用 ${item.id}`,action:'selections.rollback',path:`/selections/${item.id}/rollback`,permission:'inversion.models.manage',expected:version(selections.items[0].version)});
    }
  }
  const ready=load();return {ready,dispose(){disposed=true;++generation;forms.forEach(form=>form.dispose());client.dispose();}};
}

function chosenVersion(parameters,assembly){
  const rows=(parameters.items||[]).filter(item=>!assembly||item.assembly_id===assembly.id);
  return rows.reduce((max,item)=>Math.max(max,item.version||0),0);
}
