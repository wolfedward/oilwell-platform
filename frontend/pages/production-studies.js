import {escapeHtml as esc} from '../components.js';
import {createStudiesClient} from '../studies/client.js';
import {KINDS,payloadEditor,readPayload,readPayloadFile} from '../studies/forms.js';
import {examplePayload} from '../studies/templates.js';
import {studyList,studyDetail,statusText} from '../studies/workbench.js';
import {bindStudyCharts} from '../studies/charts.js';

const mounted = new WeakMap();
const PAGES = {"engineering-overview":"工程总览","condition-lab":"工况研究","recovery-lab":"恢复与影子调度","motor-lab":"电机证据与保护回放","study-review":"独立复核"};
const KINDS_BY_PAGE = {"condition-lab":['condition_training','condition_prediction'],"recovery-lab":['recovery_fit','energy_schedule','energy_effects'],"motor-lab":['motor_spectrum','protection_replay']};
const SELECTABLE = new Set(['condition_training','energy_schedule','protection_replay']);
const APPROVABLE = {condition_training:'completed',condition_prediction:'completed',recovery_fit:'fitted',energy_schedule:'feasible',energy_effects:'descriptive',motor_spectrum:'completed',protection_replay:'completed'};
const uuid = () => crypto.randomUUID();
const errorText = error => error?.message || '操作失败；请检查输入及当前权限。';

export async function renderProductionStudies(context) {
  const {workspace,api,permissions,actorId,page,pageHeader,isCurrent} = context;
  mounted.get(workspace)?.dispose();
  workspace.innerHTML = pageHeader(page);
  const root=document.createElement('section');root.className='production-studies';workspace.append(root);
  if(!permissions.has('studies.read')){root.textContent='没有工程研究查看权限。';return;}
  const capability=context.capabilities?.production_studies;
  if(!capability?.enabled){root.textContent='工程研究服务未配置。';return;}
  if(capability.status!=='ready'){root.textContent='工程研究服务当前不可用。';return;}
  let disposed=false,selection=0,records=[],record=null,offset=0,kind=KINDS_BY_PAGE[page.id]?.[0]||'condition_training',payload=null,sourceKind='submitted_observation',sourceDescription='',sourceReferences='',title='',exampleLoaded=false;
  const current=()=>!disposed&&isCurrent()&&workspace.contains(root);
  const client=createStudiesClient(api,actorId,current);
  const cleanupChart=bindStudyCharts(root);
  const controller={dispose(){disposed=true;++selection;client.dispose();cleanupChart();}};
  mounted.set(workspace,controller);context.registerDispose?.(()=>controller.dispose());
  const pageId=page.id;
  const ownedKinds=KINDS_BY_PAGE[pageId]||Object.keys(KINDS);
  root.innerHTML=`<p class="study-boundary">软件研究与影子建议 · 来源、单位和质量由提交者声明 · 无现场验证、无设备控制权限</p>
    <nav class="study-workflow" aria-label="工程研究页面">${Object.entries(PAGES).map(([id,label])=>`<button type="button" data-page="${id}" class="${id===pageId?'active':''}">${esc(label)}</button>`).join('')}</nav>
    <div data-study-alert role="status"></div><div data-study-content></div>`;
  const content=root.querySelector('[data-study-content]');
  const notify=(message,problem=false)=>{const node=root.querySelector('[data-study-alert]');node.className=problem?'study-alert study-alert-error':'study-alert';node.textContent=message;};
  function eligibleDependencies() {
    return records.filter(item=>item.review_status==='approved'&&item.dependency_current!==false&&item.source?.kind===sourceKind&&
      (kind==='condition_prediction'?item.kind==='condition_training'&&item.is_selected:kind==='energy_schedule'?item.kind==='recovery_fit'&&item.status==='fitted':false));
  }
  function formMarkup() {
    const pending=client.pending('create');
    return `<section class="panel study-form"><header><div><p class="eyebrow">NEW SOFTWARE STUDY</p><h2>提交计算</h2></div></header><div class="panel-body">
      <label class="study-field">研究类型<select data-study-kind>${ownedKinds.map(id=>`<option value="${id}" ${id===kind?'selected':''}>${esc(KINDS[id].label)}</option>`).join('')}</select></label>
      <label class="study-field">标题<input data-study-title maxlength="160" value="${esc(title)}" placeholder="说明研究对象与时段"/></label>
      <div class="study-form-grid"><label class="study-field">来源性质<select data-study-source ${exampleLoaded?'disabled':''}><option value="submitted_observation" ${sourceKind==='submitted_observation'?'selected':''}>提交观测资料</option><option value="software_fixture" ${sourceKind==='software_fixture'?'selected':''}>软件测试向量</option></select></label>
      <label class="study-field">来源说明<input data-study-description maxlength="1000" value="${esc(sourceDescription)}" placeholder="采集条件、测量基准或样例说明"/></label>
      <label class="study-field study-field-wide">来源引用（每行一项）<textarea data-study-references rows="2" placeholder="清单编号、文件摘要或软件例子说明">${esc(sourceReferences)}</textarea></label></div>
      ${kind==='energy_schedule'?`<label class="study-field">批准的恢复拟合记录<select data-study-recovery><option value="">选择恢复记录以填入第一口井</option>${eligibleDependencies().map(item=>`<option value="${esc(item.id)}">${esc(item.title)} · ${esc(item.id.slice(0,8))}</option>`).join('')}</select></label>`:''}
      <div class="study-example-actions"><button type="button" class="button secondary" data-study-example>载入明确标注的软件样例</button><label class="button secondary">导入 payload JSON<input type="file" accept=".json,application/json" data-study-file hidden/></label></div>
      ${payloadEditor(kind,payload,eligibleDependencies())}
      <div class="study-form-actions"><button type="button" class="button primary" data-study-create ${permissions.has('studies.create')?'':'disabled'}>${pending?'查询回执并按原请求重试':'运行并保存不可覆盖的结果'}</button></div>
      ${pending?`<p class="study-alert">上次请求结果不确定。请求号 ${esc(pending.body.request_id)}；再次点击只使用保存的原始请求体。</p>`:''}
    </div></section>`;
  }
  function reviewMarkup() {
    if(!record)return '';
    const self=String(record.actor_id)===String(actorId);
    const mayReview=permissions.has('studies.review')&&!self;
    const maySelect=permissions.has('studies.select')&&SELECTABLE.has(record.kind)&&record.review_status==='approved'&&record.dependency_current!==false;
    const reviewPending=client.pending(`review:${record.id}`),selectPending=client.pending(`select:${record.kind}`);
    return `<section class="panel study-actions"><header><h3>复核、影子选用与导出</h3></header><div class="panel-body">
      ${self?'<p class="study-boundary">作者不能复核自己的结果，包括管理员。</p>':''}
      ${mayReview?`<label class="study-field">复核决定<select data-study-decision>${record.status===APPROVABLE[record.kind]?'<option value="approved">批准软件研究</option>':''}<option value="rejected">拒绝</option>${record.review_status==='approved'?'<option value="revoked">撤销批准</option>':''}</select></label>
        <label class="study-field">复核意见<input data-study-comment maxlength="1000" placeholder="说明证据与决定"/></label><button type="button" class="button secondary" data-study-review>${reviewPending?'按原请求重试复核':'提交独立复核'}</button>`:''}
      ${maySelect?`<label class="study-field">影子选用意见<input data-study-selection-comment maxlength="1000" placeholder="说明选用依据"/></label><button type="button" class="button secondary" data-study-select>${selectPending?'按原请求重试选用':'选用为影子版本'}</button>`:''}
      ${permissions.has('studies.select')&&record.is_selected?'<button type="button" class="button secondary" data-study-unselect>撤销当前影子选用</button>':''}
      ${permissions.has('studies.export')?'<button type="button" class="button secondary" data-study-export>导出已审计 JSON</button>':''}
    </div></section>`;
  }
  function paint() {
    if(!current())return;
    const header=`<div class="study-intro"><div><p class="eyebrow">ENGINEERING STUDIES</p><h2>${esc(PAGES[pageId]||'工程研究')}</h2><p>7 类有界算法，提交来源可追溯；复核与影子选用保留独立历史。</p></div><button class="button secondary" type="button" data-study-refresh>刷新记录</button></div>`;
    const summary=pageId==='engineering-overview'?`<div class="study-cards">${Object.entries(KINDS_BY_PAGE).map(([id,kinds])=>`<button type="button" data-page="${id}"><strong>${esc(PAGES[id])}</strong><span>${kinds.map(k=>KINDS[k].label).join(' · ')}</span><small>进入研究 →</small></button>`).join('')}</div>`:'';
    const list=`<section class="panel study-records"><header><h3>历史记录</h3><span>${records.length} 条（当前页）</span></header><div class="panel-body">${studyList(records,record?.id)}<div class="study-pagination"><button type="button" data-study-prev ${offset===0?'disabled':''}>上一页</button><span>${Math.floor(offset/50)+1}</span><button type="button" data-study-next ${records.length<50?'disabled':''}>下一页</button></div></div></section>`;
    content.innerHTML=`${header}${summary}<div class="study-columns">${KINDS_BY_PAGE[pageId]&&permissions.has('studies.create')?formMarkup():''}${list}</div>
      ${record?`<section class="panel study-detail-panel"><div class="panel-body">${studyDetail(record)}${reviewMarkup()}</div></section>`:''}`;
  }
  async function loadList() {
    const serial=++selection;notify('正在读取工程研究记录…');
    try {const data=await client.list(null,offset);if(!current()||serial!==selection)return;if(!Array.isArray(data))throw Error('列表响应格式不正确');records=data;notify('');paint();}
    catch(error){if(!current()||serial!==selection)return;notify(errorText(error),true);}
  }
  async function openRecord(id) {
    const serial=++selection;notify('正在读取结果与历史…');
    try {const item=await client.get(id);if(!current()||serial!==selection)return;record=item;notify('');paint();}
    catch(error){if(!current()||serial!==selection)return;notify(errorText(error),true);}
  }
  function captureMeta() {
    title=root.querySelector('[data-study-title]')?.value||title;
    sourceKind=root.querySelector('[data-study-source]')?.value||sourceKind;
    sourceDescription=root.querySelector('[data-study-description]')?.value||sourceDescription;
    sourceReferences=root.querySelector('[data-study-references]')?.value||sourceReferences;
  }
  root.addEventListener('change',async event=>{
    if(event.target.matches('[data-study-kind]')){captureMeta();kind=event.target.value;payload=null;exampleLoaded=false;paint();return;}
    if(event.target.matches('[data-study-source]')){
      sourceKind=event.target.value;
      for(const selector of ['[data-study-field="model_record_id"]','[data-study-recovery]']){
        const select=root.querySelector(selector);if(!select)continue;
        select.innerHTML=`<option value="">选择当前来源匹配的批准记录</option>${eligibleDependencies().map(item=>`<option value="${esc(item.id)}">${esc(item.title)} · ${esc(item.id.slice(0,8))}</option>`).join('')}`;
      }
      notify('来源性质已更新；请核对依赖记录与输入内容。');return;
    }
    if(event.target.matches('[data-study-recovery]')){
      try{captureMeta();payload=readPayload(root,kind);if(!payload.wells?.length)throw Error('先填写至少一口井');payload.wells[0].recovery_record_id=event.target.value;paint();notify('已填入第一口井的恢复记录编号；请核对井号与来源。');}
      catch(error){notify(errorText(error),true);}return;
    }
    if(event.target.matches('[data-study-file]')){
      try{captureMeta();payload=await readPayloadFile(event.target.files?.[0],kind);if(!current())return;exampleLoaded=false;paint();notify('JSON 已载入编辑区，请核对来源、单位与依赖编号。');}
      catch(error){notify(`导入失败：${errorText(error)}`,true);}return;
    }
  });
  root.addEventListener('click',async event=>{
    const target=event.target.closest('button');if(!target||!current())return;
    if(target.matches('[data-study-refresh]')){await loadList();return;}
    if(target.matches('[data-study-prev],[data-study-next]')){offset+=target.hasAttribute('data-study-next')?50:-50;await loadList();return;}
    if(target.matches('[data-study-record]')){await openRecord(target.dataset.studyRecord);return;}
    if(target.matches('[data-study-example]')){
      const dependency=eligibleDependencies()[0]?.id||'';
      payload=examplePayload(kind,dependency);sourceKind='software_fixture';exampleLoaded=true;sourceDescription='显式载入的内置软件测试向量；无现场测量资格';sourceReferences='内置软件向量 / studies/templates.js';title=`${KINDS[kind].label} · 软件样例`;paint();notify('已载入软件样例；可编辑，但来源仍须如实标记为软件测试向量。');return;
    }
    if(target.matches('[data-study-create]')){
      target.disabled=true;try{
        const prior=client.pending('create');
        let body;if(prior)body=prior.body;else{
          const next=readPayload(root,kind);const rawRefs=root.querySelector('[data-study-references]').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
          body={request_id:uuid(),kind,title:root.querySelector('[data-study-title]').value.trim(),source:{kind:exampleLoaded?'software_fixture':root.querySelector('[data-study-source]').value,description:root.querySelector('[data-study-description]').value.trim(),references:rawRefs},payload:next};
          if(!body.title)throw Error('请填写研究标题');if(body.source.kind==='submitted_observation'&&!rawRefs.length)throw Error('提交观测资料必须填写来源引用');
        }
        const saved=await client.create(body);if(!current())return;notify(`已保存研究 ${saved.id}`);await loadList();await openRecord(saved.id);
      }catch(error){if(current()){notify(errorText(error),true);paint();}}finally{if(current())target.disabled=false;}return;
    }
    if(target.matches('[data-study-export]')){try{await client.export(record.id);if(current())notify('已从已审计接口导出当前记录。');}catch(error){if(current())notify(errorText(error),true);}return;}
    if(target.matches('[data-study-review],[data-study-select],[data-study-unselect]')){
      if(!record)return;const id=record.id,serial=selection;target.disabled=true;
      try{
        if(target.matches('[data-study-review]')){
          if(String(record.actor_id)===String(actorId))throw Error('作者不能复核自己的研究');
          const prior=client.pending(`review:${id}`);const body=prior?.body||{request_id:uuid(),expected_version:record.version,decision:root.querySelector('[data-study-decision]').value,comment:root.querySelector('[data-study-comment]').value.trim()};
          if(!body.comment)throw Error('请填写复核意见');await client.review(id,body);
        }else{
          const prior=client.pending(`select:${record.kind}`);
          const body=prior?.body||{request_id:uuid(),expected_version:record.selection_version,record_id:target.matches('[data-study-unselect]')?null:id,comment:root.querySelector('[data-study-selection-comment]')?.value.trim()||''};
          if(!body.comment)throw Error('请填写影子选用意见');await client.select(record.kind,body);
        }
        if(!current()||serial!==selection)return;notify('操作已记录在不可覆盖的历史中。');await loadList();await openRecord(id);
      }catch(error){if(current()&&serial===selection){notify(errorText(error),true);paint();}}finally{if(current())target.disabled=false;}
    }
  });
  await loadList();
}
