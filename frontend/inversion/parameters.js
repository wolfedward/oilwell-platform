import {element, mountActionForm, errorText} from './forms.js';

const PAGE_SIZE = 20;
const GROUPS = ['provenance','geometry','phase','bodies','drive','domain','uncertainty','policy'];
const COVERAGE = ['loss_calibration','time_alignment','averaged_power_mismatch','motion_angle_policy','upstream_dynamics','load_range','geometry_envelope','crank_friction'];
const FIELDS = {
  provenance:['source_organization','source_date','attachment_pins','calibration_cycles','operating_conditions','channel_bindings','notes','coverage'],
  geometry:['pivot_x_m','pivot_y_m','crank_radius_m','pitman_length_m','rear_arm_m','horsehead_radius_m','branch','rotation_sign','displacement_sign','evidence_refs'],
  phase:['channel_pin','zero_reading_rad','encoder_direction','unwrap_kind','evidence_refs'],
  bodies:['crank_inertia_kg_m2','crank_first_moment_x_kg_m','crank_first_moment_y_kg_m','beam_inertia_kg_m2','beam_first_moment_x_kg_m','beam_first_moment_y_kg_m','pitman_mass_kg','pitman_com_fraction','pitman_inertia_kg_m2','gravity_m_s2','component_ledger','evidence_refs'],
  drive:['supply_kind','ratio','upstream_inertia_kg_m2','loss_grid','crank_friction_Nm','viscous_friction_Nm_s','evidence_refs'],
  domain:['power_W','speed_rad_s','acceleration_rad_s2','temperature_K','supply_voltage_V','supply_frequency_Hz','bandwidth_Hz','load_N','window_ns','evidence_refs'],
  uncertainty:['torque_bound_Nm','factor_bound_m','speed_bound_rad_s','numerical_budget_Nm','numerical_factor_budget_m','evidence_refs'],
  policy:['body','content_sha256'],
};
const UNITS = new Map([
  ['pivot_x_m','m'],['pivot_y_m','m'],['crank_radius_m','m'],['pitman_length_m','m'],
  ['rear_arm_m','m'],['horsehead_radius_m','m'],['zero_reading_rad','rad'],
  ['crank_inertia_kg_m2','kg·m²'],['beam_inertia_kg_m2','kg·m²'],['pitman_mass_kg','kg'],
  ['gravity_m_s2','m/s²'],['upstream_inertia_kg_m2','kg·m²'],['crank_friction_Nm','N·m'],
  ['viscous_friction_Nm_s','N·m·s'],['torque_bound_Nm','N·m'],['factor_bound_m','m'],
]);

function shown(value) {
  return value === null || value === undefined ? '未提供' : typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function line(root,label,value) {
  const row=element('div',undefined,'inversion-row');
  row.append(element('dt',label),element('dd',shown(value)));
  root.append(row);
}
function panel(root,title) {
  const section=element('section',undefined,'panel inversion-panel');
  section.append(element('h2',title));root.append(section);return section;
}
function button(root,label,attribute,value,handler) {
  const item=element('button',label,'button inversion-history');item.type='button';
  item.setAttribute(attribute,value);item.addEventListener('click',handler);root.append(item);return item;
}
function expected(number) {return Number.isSafeInteger(number)&&number>0?{kind:'match',version:number}:{kind:'absent'};}
function requireBody(condition,message) {if(!condition)throw Object.assign(new Error(message),{code:'input_invalid'});}
function pagePath(path,offset,extra={}) {
  return `${path}?${new URLSearchParams({...extra,offset:String(offset),limit:String(PAGE_SIZE)})}`;
}
function missingFields(body) {
  const missing=[];
  for(const key of ['schema','assembly_id','assembly_version','source_kind','valid_from','valid_to','parent_version'])if(!(key in body))missing.push(key);
  for(const group of GROUPS){
    const value=body[group];if(!value){missing.push(group);continue;}
    for(const field of FIELDS[group])if(value[field]===undefined||value[field]===null)missing.push(`${group}.${field}`);
  }
  for(const [path,fields] of [['provenance.coverage',['policy_sha256','domain_sha256','uncertainty_sha256','max_time_alignment_error_ns','scopes']],
    ['drive.loss_grid',['power_W','motor_speed_rad_s','loss_W','error_W','evidence_refs']]]){
    const value=path.split('.').reduce((node,key)=>node?.[key],body);
    if(value)for(const field of fields)if(value[field]===undefined||value[field]===null)missing.push(`${path}.${field}`);
  }
  const scopes=new Set((body.provenance?.coverage?.scopes||[]).map(item=>item.scope));
  for(const scope of COVERAGE)if(!scopes.has(scope))missing.push(`provenance.coverage.scopes.${scope}`);
  const roles=new Set((body.provenance?.channel_bindings||[]).map(item=>item.role));
  for(const role of ['active_power','crank_angle','load','displacement'])if(!roles.has(role))missing.push(`provenance.channel_bindings.${role}`);
  return missing;
}
function flatten(value,prefix='',result=new Map()) {
  if(value && typeof value==='object' && !Array.isArray(value)) {
    for(const key of Object.keys(value))flatten(value[key],prefix?`${prefix}.${key}`:key,result);
  } else result.set(prefix,shown(value));
  return result;
}
function renderDifference(root,current,other) {
  const section=panel(root,`参数版本差异 · 第 ${other.version} 版 → 第 ${current.version} 版`);
  section.dataset.inversionDifference='';
  const left=flatten({...other.body,review:{state:other.review_state,version:other.review_version}});
  const right=flatten({...current.body,review:{state:current.review_state,version:current.review_version}});
  const paths=[...new Set([...left.keys(),...right.keys()])].sort();
  for(const [title,accept] of [
    ['物理参数与模型策略',path=>!/(^review\.|valid_from|valid_to|evidence|attachment|coverage|content_sha256)/.test(path)],
    ['物理有效期',path=>/(valid_from|valid_to)/.test(path)],
    ['来源附件与覆盖证据',path=>/(evidence|attachment|coverage|content_sha256)/.test(path)],
    ['独立复核状态',path=>path.startsWith('review.')],
  ]) {
    const group=element('details',undefined,'inversion-group');group.open=true;group.append(element('summary',title));
    const values=element('dl',undefined,'inversion-details');group.append(values);section.append(group);
    for(const path of paths.filter(accept)){
      const unit=UNITS.get(path.split('.').at(-1));
      line(values,`${path}${unit?` (${unit})`:''} · ${left.get(path)===right.get(path)?'未变化':'变化'}`,
        `旧 ${left.get(path)??'未提供'} → 新 ${right.get(path)??'未提供'}`);
    }
  }
}

export function mountProductionParameters(ctx,client) {
  const root=element('section',undefined,'production-parameters');ctx.workspace.append(root);
  const state={assemblyId:ctx.params?.assembly_id||null,parameterId:ctx.params?.id||null,compareId:null,
    assembliesOffset:0,parametersOffset:0,modelsOffset:0,mechanicalOffset:0,selectionsOffset:0};
  const forms=[];let disposed=false,generation=0;
  const current=()=>!disposed&&(ctx.isCurrent?.()??true)&&ctx.workspace.contains(root);
  const addForm=(host,spec)=>forms.push(mountActionForm(host,client,spec,load));

  function controls(host,page,key) {
    const bar=element('div',undefined,'inversion-pager');host.append(bar);
    if(page.offset>0)button(bar,'上一页','data-inversion-page',key+'-previous',()=>{
      state[key+'Offset']=Math.max(0,page.offset-PAGE_SIZE);load();
    });
    bar.append(element('span',`第 ${Math.floor(page.offset/PAGE_SIZE)+1} 页`));
    if(page.items.length===page.limit)button(bar,'下一页','data-inversion-page',key+'-next',()=>{
      state[key+'Offset']=page.offset+PAGE_SIZE;load();
    });
  }
  async function load() {
    const n=++generation;forms.splice(0).forEach(form=>form.dispose());
    root.replaceChildren(element('p','正在读取机械参数与审批状态…'));
    try {
      const [assemblies,models,selected]=await Promise.all([
        client.get(pagePath('/assemblies',state.assembliesOffset)),
        client.get(pagePath('/models',state.modelsOffset)),
        state.parameterId?client.get(`/parameters/${state.parameterId}`):null,
      ]);
      if(!current()||n!==generation||!assemblies||!models)return;
      const assemblyId=state.assemblyId||selected?.assembly_id||assemblies.items[0]?.id;
      const assembly=assemblyId?assemblies.items.find(item=>item.id===assemblyId)||await client.get(`/assemblies/${assemblyId}`):null;
      if(!current()||n!==generation)return;
      let parameters={offset:0,limit:PAGE_SIZE,items:[]},head={items:[]},mechanical={offset:0,limit:PAGE_SIZE,items:[]},selections={offset:0,limit:PAGE_SIZE,items:[]},selectionHead={items:[]};
      if(assembly){
        const filter={assembly_id:assembly.id};
        [parameters,head,mechanical,selections,selectionHead]=await Promise.all([
          client.get(pagePath('/parameters',state.parametersOffset,filter)),
          client.get(`/parameters?${new URLSearchParams({assembly_id:assembly.id,offset:'0',limit:'1'})}`),
          client.get(pagePath(`/assemblies/${assembly.id}/versions`,state.mechanicalOffset)),
          client.get(pagePath(`/assemblies/${assembly.id}/selections`,state.selectionsOffset)),
          client.get(`/assemblies/${assembly.id}/selections?offset=0&limit=1`),
        ]);
      }
      if(!current()||n!==generation)return;
      const selectedId=selected?.assembly_id===assembly?.id?selected.id:head.items[0]?.id;
      const detail=selectedId?(selected?.id===selectedId?selected:await client.get(`/parameters/${selectedId}`)):null;
      const comparison=state.compareId&&state.compareId!==detail?.id?await client.get(`/parameters/${state.compareId}`):null;
      if(!current()||n!==generation)return;
      render({assemblies,models,assembly,parameters,head,detail,mechanical,selections,selectionHead,
        comparison:comparison?.assembly_id===assembly?.id?comparison:null});
    } catch(error) {
      if(current()&&n===generation)root.replaceChildren(element('p',errorText(error),'inversion-state'));
    }
  }
  function render({assemblies,models,assembly,parameters,head,detail,mechanical,selections,selectionHead,comparison}) {
    root.replaceChildren(element('p','机械身份、来源附件、物理有效期、参数复核和模型选用分别记录。附件身份与用途不等于现场资格；独立复核也不认证现场精度。','inversion-notice'));
    const assemblyPanel=panel(root,'机械总成选择');
    for(const item of assemblies.items)button(assemblyPanel,`${item.body?.asset_number||item.id} · ${item.id}`,'data-inversion-assembly',item.id,()=>{
      state.assemblyId=item.id;state.parameterId=null;state.compareId=null;
      state.parametersOffset=state.mechanicalOffset=state.selectionsOffset=0;
      ctx.navigate?.('production-parameters',{assembly_id:item.id})||load();
    });
    controls(assemblyPanel,assemblies,'assemblies');
    const mechanicalPanel=panel(root,'当前机械配置与版本历史');
    if(!assembly)mechanicalPanel.append(element('p','暂无机械总成记录。'));
    else {
      const values=element('dl',undefined,'inversion-details');mechanicalPanel.append(values);
      line(values,'机械总成 ID',assembly.id);line(values,'当前版本',assembly.version);
      for(const key of ['manufacturer','model','asset_number','mechanism','source_kind','source_organization'])line(values,key,assembly.body?.[key]);
      line(values,'当前声明有效期',`${assembly.body?.valid_from||'未提供'} — ${assembly.body?.valid_to||'开放'}`);
      line(values,'当前生效有效期',`${assembly.valid_from||'未提供'} — ${assembly.valid_to||'开放'}`);
      for(const pin of assembly.body?.installation_evidence||[])line(values,'安装来源附件',pin);
      for(const item of mechanical.items){
        const history=element('details',undefined,'inversion-group');history.append(element('summary',`机械第 ${item.version} 版 · ${item.version_id}`));
        const facts=element('dl',undefined,'inversion-details');history.append(facts);mechanicalPanel.append(history);
        line(facts,'原声明摘要',item.content_sha256);
        line(facts,'原声明有效期',item.declared.interval_known?`${item.declared.valid_from} — ${item.declared.valid_to||'开放'}`:'原声明物理有效期未知');
        line(facts,'追加生效版本',item.effective?.revision);
        line(facts,'追加生效有效期',item.effective?`${item.effective.valid_from} — ${item.effective.valid_to||'开放'}`:'无有效期投影');
        for(const [key,value] of Object.entries(item.declared.body||{}))line(facts,`原声明 ${key}`,value);
      }
      controls(mechanicalPanel,mechanical,'mechanical');
    }
    if(ctx.permissions.has('inversion.parameters.manage'))renderManage(root,{assemblies,assembly,head});
    const history=panel(root,'参数版本历史');
    if(!parameters.items.length)history.append(element('p','尚无参数版本。'));
    for(const item of parameters.items){
      button(history,`${item.id} · 第 ${item.version} 版 · 复核版本 ${item.review_version??1} · ${item.review_action||'draft'}`,
        'data-inversion-parameter',item.id,()=>ctx.navigate?.('production-parameters',{assembly_id:assembly.id,id:item.id})||(
          state.parameterId=item.id,state.compareId=null,load()));
      if(detail&&item.id!==detail.id)button(history,`与第 ${item.version} 版比较`,'data-inversion-compare',item.id,()=>{
        state.compareId=item.id;load();
      });
    }
    if(assembly)controls(history,parameters,'parameters');
    renderParameter(root,detail);
    if(detail&&comparison)renderDifference(root,detail,comparison);
    renderModels(root,{models,assembly,selections,selectionHead});
  }
  function renderManage(host,{assemblies,assembly,head}) {
    const manage=panel(host,'机械登记与参数草稿');
    addForm(manage,{label:'登记新机械总成',action:'assemblies.create',path:'/assemblies',permission:'inversion.parameters.manage',bodyEditor:true,expected:{kind:'absent'},
      validateBody:body=>requireBody(body?.parent_version===null&&!assemblies.items.some(item=>item.id===body.assembly_id),'新机械总成需要新的 UUID 和 parent_version:null')});
    if(assembly){
      addForm(manage,{label:'登记机械后续版本',action:'assemblies.create',path:'/assemblies',permission:'inversion.parameters.manage',bodyEditor:true,
        expected:expected(assembly.version),slot:'assemblies.create:successor:'+assembly.id,
        validateBody:body=>requireBody(body?.assembly_id===assembly.id&&body?.parent_version===assembly.version,'后续机械版本必须指向当前总成和版本')});
      addForm(manage,{label:'登记新井归属',action:'assignments.create',path:'/assignments',permission:'inversion.parameters.manage',bodyEditor:true,expected:{kind:'absent'},
        validateBody:body=>requireBody(body?.assembly_id===assembly.id&&body?.close_assignment_id===null,'新井归属需指定当前总成；历史归属关闭需先核对其独立版本')});
      const latest=head.items[0]?.version||0;
      addForm(manage,{label:'保存参数草稿',action:'parameters.create',path:'/parameter-versions',permission:'inversion.parameters.manage',bodyEditor:true,bodyLabel:'inversion.parameter-set/1 严格 JSON',expected:expected(latest),
        validateBody:body=>requireBody(body?.assembly_id===assembly.id&&body?.parent_version===(latest||null),'参数草稿必须指向当前总成及其参数前序版本')});
    }
    manage.append(element('p','请填入完整 inversion.parameter-set/1 JSON。数值使用十进制字符串，不自动填入机械常数、来源或审批。','inversion-muted'));
  }
  function renderParameter(host,detail) {
    const physical=panel(host,'参数字段、单位与证据');
    if(!detail){physical.append(element('p','尚无参数草稿；必需组：'+GROUPS.join('、')));return;}
    const body=detail.body||{},facts=element('dl',undefined,'inversion-details');physical.append(facts);
    line(facts,'参数版本',`第 ${detail.version} 版 · 复核版本 ${detail.review_version} · ${detail.review_state}`);
    line(facts,'内容摘要',detail.content_sha256);line(facts,'来源',body.source_kind);
    line(facts,'声明有效期',`${body.valid_from||'未提供'} — ${body.valid_to||'开放'}`);
    line(facts,'缺少参数 / 证据',missingFields(body).join('、')||'当前必需字段均已提供；提交时仍需服务端核验字段、附件和覆盖范围');
    for(const group of GROUPS){
      const details=element('details',undefined,'inversion-group');details.open=group==='geometry'||group==='provenance';
      details.append(element('summary',group));const fields=element('dl',undefined,'inversion-details');details.append(fields);physical.append(details);
      for(const key of FIELDS[group])line(fields,`${group}.${key}${UNITS.has(key)?` (${UNITS.get(key)})`:''}`,body[group]?.[key]);
      for(const pin of body[group]?.evidence_refs||[])line(facts,`${group} 来源附件`,pin);
    }
    const coverage=body.provenance?.coverage;
    if(coverage){
      line(facts,'最大时间对齐误差 (ns)',coverage.max_time_alignment_error_ns);
      for(const scope of coverage.scopes||[])line(facts,`覆盖范围 ${scope.scope}`,`${scope.basis} · 附件 ${scope.attachment_pin?.id||'未提供'} · 用途 ${scope.attachment_pin?.purpose||'未提供'} · ${scope.attachment_pin?.source_description||''}`);
    }
    if(ctx.permissions.has('inversion.parameters.manage')&&detail.review_state==='draft')addForm(physical,{label:'提交参数复核',action:'parameters.submit',path:`/parameters/${detail.id}/submit`,permission:'inversion.parameters.manage',expected:expected(detail.review_version)});
    if(ctx.permissions.has('inversion.parameters.review')&&detail.review_state==='submitted'){
      physical.append(element('p','独立复核：作者或提交者不能审批自己的参数，管理员也不能绕过此限制。','inversion-muted'));
      addForm(physical,{label:'记录独立复核',action:'parameters.review',path:`/parameters/${detail.id}/review`,permission:'inversion.parameters.review',expected:expected(detail.review_version),decisions:[['approve','批准'],['reject','驳回']]});
    }
    if(ctx.permissions.has('inversion.parameters.review')&&detail.review_state==='approved')addForm(physical,{label:'撤销参数批准',action:'parameters.revoke',path:`/parameters/${detail.id}/revoke`,permission:'inversion.parameters.review',expected:expected(detail.review_version)});
  }
  function renderModels(host,{models,assembly,selections,selectionHead}) {
    const modelsPanel=panel(host,'已登记模型与当前选用');
    for(const item of models.items) {
      modelsPanel.append(element('p',`${item.installed_id} · ${item.id} · 第 ${item.version} 版 · 复核版本 ${item.review_version??1} · ${item.review_action||'unreviewed'}`));
      if(ctx.permissions.has('inversion.models.review')&&!item.review_action)addForm(modelsPanel,{label:`复核模型 ${item.id}`,action:'models.review',path:`/models/${item.id}/review`,permission:'inversion.models.review',expected:expected(item.review_version),decisions:[['approve','批准'],['reject','驳回']]});
    }
    if(!models.items.length)modelsPanel.append(element('p','本页尚无已登记模型。'));
    controls(modelsPanel,models,'models');
    if(ctx.permissions.has('inversion.models.manage')){
      modelsPanel.append(element('p','可登记的安装模型：beam_energy_balance/1（服务端安装清单；不接受上传代码或路径）。','inversion-muted'));
      addForm(modelsPanel,{label:'登记已安装模型',action:'models.create',path:'/models',permission:'inversion.models.manage',fixedBody:{installed_id:'beam_energy_balance/1'},expected:{kind:'absent'}});
    }
    if(!assembly)return;
    modelsPanel.append(element('h3','选用历史'));
    if(!selections.items.length)modelsPanel.append(element('p','本页尚无选用记录。'));
    for(const item of selections.items){
      modelsPanel.append(element('p',`第 ${item.version} 版 · ${item.id} · 参数 ${item.parameter_id} · 模型 ${item.model_id}`));
    }
    controls(modelsPanel,selections,'selections');
    if(ctx.permissions.has('inversion.models.manage')){
      // Selection writes require an exact current head, independent of the viewed page.
      const currentSelection=selectionHead.items[0];
      addForm(modelsPanel,{label:'选用已批准参数与模型',action:'selections.create',path:'/selections',permission:'inversion.models.manage',bodyEditor:true,expected:expected(currentSelection?.version)});
      for(const item of selections.items.filter(item=>item.id!==currentSelection?.id))addForm(modelsPanel,{label:`回退至历史选用 ${item.id}`,action:'selections.rollback',path:`/selections/${item.id}/rollback`,permission:'inversion.models.manage',expected:expected(currentSelection?.version),slot:`selections.rollback:${item.id}`});
    }
  }
  const ready=load();
  return {ready,dispose(){disposed=true;++generation;forms.forEach(form=>form.dispose());client.dispose();}};
}
