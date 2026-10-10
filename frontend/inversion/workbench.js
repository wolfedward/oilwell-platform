import {createInversionClient} from './client.js';
import {element,mountActionForm,errorText} from './forms.js';
import {mountResultDetail,runStateText} from './results.js';

const RUN_PERMISSIONS=['inversion.run','waveform.read','waveform.raw.read','waveform.export'];
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const DIAGNOSTIC_CAUSES={
  insufficient_coverage:'有效支撑不足',no_valid_points:'没有可用的预测点',full_cycle_unconfirmed:'完整物理周期尚未确认',
  parameter_pin_changed:'参数内容或复核版本已变化',model_pin_changed:'模型内容或复核版本已变化',
  selection_changed:'持久化选用已变化',pair_review_changed:'配对复核已变化',evidence_changed:'证据内容或审批已变化',
  capture_changed:'采集记录已变化或受限',assignment_interval:'总成与井的有效期未覆盖观测',
  physical_interval:'机械版本有效期未覆盖观测',parameter_interval:'参数有效期未覆盖观测',
  operating_condition_interval:'工况有效期未覆盖观测',registry_ineligible:'设备登记或井归属无法覆盖观测',
  modbus_assignment_interval:'Modbus点表有效期未覆盖观测',applicability_changed:'观测适用的依赖事实已变化',
};

function appendRunDiagnostics(root,run) {
  const detail=run.terminal_diagnostics;
  if(!detail||detail.run_id!==run.run_id||detail.version!==run.version||detail.state!==run.state
    ||detail.reason_code!==run.reason_code||!Array.isArray(detail.reasons)||!detail.reasons.length||detail.reasons.length>32){
    root.append(element('p','诊断详情不可用。请重新读取运行，并核对当前输入与审批记录。'));return;
  }
  const section=element('section');section.dataset.runDiagnostics='';
  for(const code of detail.reasons)section.append(element('p',DIAGNOSTIC_CAUSES[code]||'输入证据或适用范围未通过当前核验'));
  if(run.reason_code==='not_applicable'){
    section.append(element('p',`有效支撑比例 ${detail.coverage_fraction} · 有效点 ${detail.valid_point_count} / ${detail.point_count}`),
      element('p','请检查角度完整周期、原始通道质量与缺口，并核对已批准的适用范围；确认后重新提交。'));
  }else section.append(element('p','请重新读取参数、模型选用、配对复核和有效期，核对变更后重新预检；原冻结输入不会自动改写。'));
  root.append(section);
}

function choice(root,label,name) {
  const wrap=element('label',label),select=element('select');select.name=name;wrap.append(select);root.append(wrap);return select;
}

export function mountProductionInversion(ctx,client=createInversionClient(ctx)) {
  const root=element('section',undefined,'production-inversion');ctx.workspace.append(root);
  const setup=element('section',undefined,'panel inversion-setup'),status=element('p','','inversion-status');status.setAttribute('role','status');
  setup.append(element('h2','固定输入与预检'),element('p','选择已冻结成员与已持久化选用。预检只核对元数据，不执行计算；运行仍会重新核验权限、版本和证据。'));
  const selectors=element('div',undefined,'inversion-selectors');setup.append(selectors);
  const assembly=choice(selectors,'机械配置','assembly_id'),dataset=choice(selectors,'冻结数据集','dataset_id'),member=choice(selectors,'冻结配对成员','member_ordinal');
  const lineage=element('div'),conditions=element('fieldset');conditions.append(element('legend','本次声明的已批准工况条件（必须显式选择）'));
  const preflight=element('button','预检固定输入','button primary');preflight.type='button';preflight.dataset.inversionPreflight='';
  const refresh=element('button','重新读取选用与冻结成员','button');refresh.type='button';
  const commandRoot=element('section'),runRoot=element('section',undefined,'panel inversion-run'),resultRoot=element('section');
  runRoot.append(element('h2','未计算'),element('p','完成固定输入预检后，可提交单成员运行。'));
  setup.append(lineage,conditions,preflight,refresh,status,commandRoot);root.append(setup,runRoot,resultRoot);
  let disposed=false,timer=null,runGeneration=0,runId=null,runVersion=null,pollRequest=null;
  let selectedPins=null,frozenVersion=null,members=[],selectedPair=null,inputRevision=0,runForm=null,resultMount=null;
  const scopes=new Map(),runForms=[];
  const current=()=>!disposed&&(ctx.isCurrent?.()??true)&&ctx.workspace.contains(root);
  const canRun=()=>RUN_PERMISSIONS.every(permission=>ctx.permissions.has(permission));
  function invalidate(){inputRevision++;runForm?.dispose();runForm=null;commandRoot.replaceChildren();}
  function scope(name){scopes.get(name)?.dispose();const child=createInversionClient({...ctx,isCurrent:()=>current()&&scopes.get(name)===child});scopes.set(name,child);return child;}
  function showError(error){if(current())status.textContent=error.status===409?'选用、配对复核或冻结列表已变化。请重新读取并核对，再预检。':errorText(error);}
  async function loadAssembly(){
    invalidate();selectedPins=null;lineage.replaceChildren();conditions.replaceChildren(element('legend','本次声明的已批准工况条件（必须显式选择）'));
    const scoped=scope('assembly');if(!UUID.test(assembly.value))return;
    try{
      const selections=await scoped.get(`/assemblies/${assembly.value}/selections?offset=0&limit=1`);if(!selections||!current())return;
      const selected=selections.items[0];if(!selected){lineage.textContent='尚无持久化选用；请先到机械参数中心独立审核并选用。';return;}
      const parameter=await scoped.get('/parameters/'+selected.body.parameter_id);if(!parameter||!current())return;
      if(parameter.content_sha256!==selected.body.parameter_sha256)throw Object.assign(Error('parameter_pin_changed'),{status:409});
      selectedPins={...selected.body,selection_version:selected.version};
      lineage.append(element('p',`固定参数 ${selected.body.parameter_id} · 摘要 ${selected.body.parameter_sha256} · 参数复核版本 ${selected.body.parameter_review_version}`),
        element('p',`固定模型 ${selected.body.model_id} · 摘要 ${selected.body.model_sha256} · 模型复核版本 ${selected.body.model_review_version} · 持久化选用版本 ${selected.version}`));
      for(const item of parameter.body.provenance?.operating_conditions||[]){
        const label=element('label',`${item.id} · ${item.valid_from} — ${item.valid_to}`),input=element('input');input.type='checkbox';input.name='operating_condition_ids';input.value=item.id;input.addEventListener('change',invalidate);label.prepend(input);conditions.append(label);
      }
    }catch(error){showError(error);}
  }
  async function loadPair(){
    invalidate();selectedPair=null;const chosen=members.find(row=>String(row.ordinal)===member.value);if(!chosen)return;
    const scoped=scope('pair');
    try{
      const pair=await scoped.waveformGet('/pairs/'+chosen.reference_id);if(!pair||!current())return;
      if(pair.id!==chosen.reference_id||pair.review_state!=='approved'||!Number.isSafeInteger(pair.version))throw Object.assign(Error('pair_review_changed'),{status:409});
      selectedPair=pair;
      status.textContent=`冻结成员序号 ${chosen.ordinal} · 配对 ${pair.id} · 当前复核版本 ${pair.version}。请选择本次工况并预检。`;
    }catch(error){showError(error);}
  }
  async function loadDataset(){
    invalidate();frozenVersion=null;members=[];selectedPair=null;member.replaceChildren();scopes.get('pair')?.dispose();
    const scoped=scope('dataset');if(!UUID.test(dataset.value))return;
    try{
      const summary=await scoped.waveformGet('/datasets/'+dataset.value);if(!summary||!current())return;
      if(!summary.version||summary.draft.state!=='frozen')throw Object.assign(Error('dataset_not_frozen'),{status:409});
      const version=summary.version;let page=1,prior=null;
      do{
        if(page>10)throw Error('冻结成员分页超出 1000 成员边界');
        const query=new URLSearchParams({view:'members',version_id:version.id,page:String(page),page_size:'100'});
        if(prior)for(const key of ['snapshot_revision','received_before','received_after'])if(prior[key]!==undefined)query.set(key,prior[key]);
        const result=await scoped.waveformGet(`/datasets/${dataset.value}?${query}`);if(!result||!current())return;
        if(result.page!==page||result.page_size!==100||result.items.length>100)throw Error('冻结成员分页超出边界');
        if(prior&&(result.snapshot_revision!==prior.snapshot_revision||result.received_before!==prior.received_before))throw Object.assign(Error('frozen_snapshot_changed'),{status:409});
        // Frozen database members are dense zero-based ordinals. Retain page
        // position BEFORE filtering kinds; draft WITH ORDINALITY is not used.
        result.items.forEach((item,index)=>{if(item.kind==='pair')members.push({...item,ordinal:(page-1)*100+index});});
        prior=result;page++;
      }while(prior.has_more);
      frozenVersion=version;
      for(const item of members)member.append(new Option(`成员 ${item.ordinal} · ${item.reference_id}`,String(item.ordinal)));
      if(!members.length){status.textContent='此冻结版本没有配对成员。请重新建立包含功率、角度、载荷和位移的配对并冻结。';return;}
      await loadPair();
    }catch(error){members=[];member.replaceChildren();showError(error);}
  }
  function requestBody(){
    const chosen=members.find(row=>String(row.ordinal)===member.value),ids=[...conditions.querySelectorAll('input:checked')].map(input=>input.value);
    if(!canRun()||!chosen||!frozenVersion||!selectedPins||!selectedPair||!ids.length)throw Object.assign(Error('请选择冻结配对、固定选用与本次工况；需要运行和波形读取/原始/导出权限。'),{code:'input_invalid'});
    const body={dataset_version_id:frozenVersion.id,dataset_sha256:frozenVersion.content_sha256,member_ordinal:chosen.ordinal,
      pair_id:selectedPair.id,pair_sha256:selectedPair.content_sha256,pair_review_version:selectedPair.version,
      selection_version:selectedPins.selection_version,operating_condition_ids:ids};
    for(const key of ['assembly_id','parameter_id','parameter_sha256','parameter_review_version','model_id','model_sha256','model_review_version'])body[key]=selectedPins[key];
    return body;
  }
  preflight.addEventListener('click',async()=>{
    if(!current()||preflight.disabled)return;
    invalidate();const revision=inputRevision;preflight.disabled=true;
    try{
      const body=requestBody(),result=await client.preflight(body);if(!result||!current()||revision!==inputRevision)return;
      if(!result.allowed){status.textContent='数据不合格 / 预检未通过：'+(result.missing||[]).join(', ')+'。请核对参数证据或重建包含角度的配对。';return;}
      status.textContent='预检通过（临时元数据检查，尚未计算）。提交时将再次核验。';
      runForm=mountActionForm(commandRoot,client,{action:'runs.create',path:'/runs',permission:'inversion.run',expected:{kind:'absent'},label:'提交单成员运行',fixedBody:body,slot:'workbench-run'},async value=>{const run=value.run||value;if(run.run_id)await monitor(run.run_id,run);});
    }catch(error){showError(error);}finally{if(current())preflight.disabled=!canRun();}
  });
  function clearRunForms(){runForms.splice(0).forEach(form=>form.dispose());}
  function showRun(run,own=runGeneration){
    if(!current()||own!==runGeneration||run.run_id!==runId)return;
    const signature=run.version+':'+run.state+':'+run.reason_code+':'+JSON.stringify(run.terminal_diagnostics??null);
    if(runVersion===signature)return;runVersion=signature;clearRunForms();
    runRoot.replaceChildren(element('h2',runStateText(run)),element('p',`运行 ${run.run_id} · ${run.reason_code||'无附加原因'}`));
    if(run.state==='blocked')appendRunDiagnostics(runRoot,run);
    const expected={kind:'match',version:run.version};
    if(['queued','running'].includes(run.state)&&!run.cancelling&&run.reason_code!=='cancel_requested'&&ctx.permissions.has('inversion.run')){
      runForms.push(mountActionForm(runRoot,client,{action:'runs.cancel',path:`/runs/${run.run_id}/cancel`,permission:'inversion.run',expected,label:'取消运行'},value=>{showRun(value.run||value,own);}));
    }else if(['failed','blocked','cancelled'].includes(run.state)&&canRun()){
      runForms.push(mountActionForm(runRoot,client,{action:'runs.retry',path:`/runs/${run.run_id}/retry`,permission:'inversion.run',expected,label:'按原冻结输入重试'},value=>monitor((value.run||value).run_id,value.run||value)));
    }
    if(run.result_id){
      const link=element('button','打开结果与版本复核','button');link.onclick=()=>ctx.navigate?.('production-results',{result_id:run.result_id});runRoot.append(link);
      resultMount?.dispose();resultRoot.replaceChildren();
      resultMount=mountResultDetail({...ctx,isCurrent:()=>current()&&own===runGeneration&&run.run_id===runId},resultRoot,client,run.result_id);
    }
  }
  async function monitor(id,initial){
    if(!UUID.test(id)){status.textContent='无效运行标识。';return;}
    clearTimeout(timer);const own=++runGeneration;runId=id;runVersion=null;
    resultMount?.dispose();resultMount=null;resultRoot.replaceChildren();delete resultRoot.dataset.loadedPoints;
    clearRunForms();runRoot.replaceChildren();
    const previousRequest=pollRequest,monitorClient=scope('run-monitor');
    if(initial)showRun(initial,own);
    async function poll(){
      if(!current()||own!==runGeneration)return;
      const request=monitorClient.get('/runs/'+id);pollRequest=request;
      try{
        const run=await request;if(!run||!current()||own!==runGeneration)return;
        showRun(run,own);
        if(['queued','running'].includes(run.state))timer=setTimeout(poll,1500);
      }catch(error){if(current()&&own===runGeneration)runRoot.append(element('p',errorText(error)));}
      finally{if(pollRequest===request)pollRequest=null;}
    }
    // Abort the old generation via its scoped client, then wait for it to
    // settle before starting the next request. Late responses cannot paint.
    if(previousRequest)await previousRequest.catch(()=>null);
    await poll();
  }
  assembly.onchange=loadAssembly;dataset.onchange=loadDataset;member.onchange=loadPair;
  refresh.onclick=async()=>{status.textContent='正在重新读取…';await Promise.all([loadAssembly(),loadDataset()]);};
  async function initialize(){
    preflight.disabled=!canRun();
    try{
      let assemblyOffset=0,datasetPage=1,datasetPrevious=null;
      const nav=element('div',undefined,'inversion-list-pages');selectors.append(nav);
      async function assembliesPage(){
        const listClient=scope('assemblies-list');
        const value=await listClient.get(`/assemblies?offset=${assemblyOffset}&limit=100`);if(!value||!current())return;
        assembly.replaceChildren();for(const item of value.items)assembly.append(new Option(`${item.body?.asset_number||item.id} · 版本 ${item.version}`,item.id));
        assemblyPrev.disabled=assemblyOffset===0;assemblyNext.disabled=value.items.length<100;await loadAssembly();
      }
      async function datasetsPage(){
        if(!ctx.permissions.has('waveform.read')){status.textContent='冻结成员读取权限不可用：需要 waveform.read。';return;}
        const query=new URLSearchParams({state:'frozen',page:String(datasetPage),page_size:'100'});
        if(datasetPage>1&&datasetPrevious)for(const key of ['snapshot_revision','received_before'])query.set(key,datasetPrevious[key]);
        const listClient=scope('datasets-list');
        const value=await listClient.waveformGet('/datasets?'+query);if(!value||!current())return;
        datasetPrevious=value;dataset.replaceChildren();for(const item of value.items)dataset.append(new Option(item.title||item.dataset_id,item.dataset_id));
        datasetPrev.disabled=datasetPage===1;datasetNext.disabled=!value.has_more;await loadDataset();
      }
      const assemblyPrev=element('button','上一页机械配置','button'),assemblyNext=element('button','下一页机械配置','button'),datasetPrev=element('button','上一页数据集','button'),datasetNext=element('button','下一页数据集','button');
      nav.append(assemblyPrev,assemblyNext,datasetPrev,datasetNext);
      assemblyPrev.onclick=()=>{assemblyOffset=Math.max(0,assemblyOffset-100);assembliesPage().catch(showError);};assemblyNext.onclick=()=>{assemblyOffset+=100;assembliesPage().catch(showError);};
      datasetPrev.onclick=()=>{datasetPage=Math.max(1,datasetPage-1);datasetsPage().catch(showError);};datasetNext.onclick=()=>{datasetPage++;datasetsPage().catch(showError);};
      await Promise.all([assembliesPage(),datasetsPage()]);
      if(current()&&ctx.params?.run_id)await monitor(ctx.params.run_id);
      const saved=client.pending.get('workbench-run');
      if(current()&&saved&&!runForm)runForm=mountActionForm(commandRoot,client,{action:'runs.create',path:'/runs',permission:'inversion.run',expected:{kind:'absent'},label:'恢复原运行提交',fixedBody:saved.envelope.body,slot:'workbench-run'},value=>{const run=value.run||value;if(run.run_id)monitor(run.run_id,run);});
    }catch(error){showError(error);}
  }
  return {ready:initialize(),dispose(){disposed=true;runGeneration++;clearTimeout(timer);runForm?.dispose();clearRunForms();resultMount?.dispose();scopes.forEach(child=>child.dispose());client.dispose();}};
}
