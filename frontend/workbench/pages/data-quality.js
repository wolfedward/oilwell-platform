import {registryTime} from '../../registry/client.js';
import {createIngestClient,CASE_STATES,REASONS,ingestError} from '../../ingest/client.js';
import {queryContext,pageControls,withSnapshot} from './wells.js';
import {timeInputs,readFilters,factButtons,bindFactAccess} from './acquisition.js';

const VIEWS={observations:'原始观测质量',resolution:'历史台账解析',cases:'接收隔离与人工处置'};
const REASON_LABELS={not_registered:'未登记',unbound:'未绑定',state_not_active:'台账未启用',no_expected_map:'未关联点表',unknown_point:'未知测点',unit_mismatch:'单位不符',out_of_range:'超工程范围',clock_anomaly:'时钟异常',quality_bad:'原质量 bad',quality_uncertain:'原质量 uncertain'};
const ACTIONS={ack:'确认异常',note:'补充说明',close:'关闭事项',reopen:'重新打开'};
const allowed=(state,action)=>({open:['ack','note','close'],acknowledged:['note','close'],closed:['note','reopen']}[state]||[]).includes(action);

export async function renderDataQuality(context){
 const {workspace:root,api,escapeHtml:e,components:c,permissions,params}=context;
 const can=view=>view==='cases'?permissions.has('ingest.read'):permissions.has('data_quality.read')&&permissions.has('telemetry.read')&&(view!=='resolution'||permissions.has('registry.read'));
 let view=can(params.view)&&VIEWS[params.view]?params.view:Object.keys(VIEWS).find(can),result=null,filters={},generation=0,selection=0,selected=null,formModal=null;
 const pending=new Map(),client=createIngestClient(api,{isCurrent:context.isCurrent});context.registerDispose(()=>client.dispose());
 root.innerHTML=context.pageHeader()+`<section class="field-registry"><div class="registry-actions" role="group" aria-label="质量口径">${Object.entries(VIEWS).map(([key,label])=>`<button class="button secondary" data-quality-view="${key}" ${can(key)?'':'disabled'}>${label}${can(key)?'':'（无权查看）'}</button>`).join('')}</div><div data-quality-filter></div><p data-quality-status role="status"></p><div data-quality-results></div><div data-case-detail></div></section>`;
 const formHost=root.querySelector('[data-quality-filter]'),target=root.querySelector('[data-quality-results]'),status=root.querySelector('[data-quality-status]'),detail=root.querySelector('[data-case-detail]');bindFactAccess(context,root);
 function closeSelection(){++selection;selected=null;formModal?.close();formModal=null;detail.innerHTML='';}
 function buildFilter(){const reasons=view==='cases'?Object.entries(REASONS).filter(([key])=>key!=='accepted'):Object.entries(REASON_LABELS);
  formHost.innerHTML=`<p class="registry-boundary">${view==='cases'?'接收事实与人工处置状态：关闭不删除原文、不写入观测、不改变隔离结论。状态是查询时刻的当前状态，不是历史快照。':'接收时间窗口内的观测；原始质量与解析原因分别保留，不在此页修改测量值。'} 默认近 24 小时，最长 31 天。</p><form class="registry-filters" data-quality-filter-form><label>网关<input name="gateway_id" value="${e(params.gateway_id||'')}"></label><label>终端<input name="device_id" value="${e(params.device_id||'')}"></label>${timeInputs()}${view!=='cases'?'<label>测点<input name="point_code"></label><label>原质量<select name="quality"><option value="">全部</option><option>good</option><option>uncertain</option><option>bad</option></select></label>':'<label>当前处置状态<select name="state"><option value="">全部</option><option value="open">待处置</option><option value="acknowledged">已确认</option><option value="closed">人工已关闭</option></select></label>'}${view!=='observations'?`<label>原因<select name="reason_code"><option value="">全部</option>${reasons.map(([key,label])=>`<option value="${key}">${e(label)}</option>`).join('')}</select></label>`:''}${permissions.has('registry.read')?`<label>井内部编号<input name="well_id" value="${e(params.well_id||'')}"></label>`:''}<button class="button primary">查询</button>${permissions.has('registry.read')?'<button class="button secondary" type="button" data-unresolved>查看未解析归属</button>':''}</form>`;
 }
 function columns(){const base=[{key:'received_at',label:'接收时间',render:registryTime},{key:'gateway_id',label:'网关'},{key:'device_id',label:'终端'},{key:'message_id',label:'消息引用'}];
  if(view==='cases')return [{key:'id',label:'事项',render:id=>c.trusted(`<button class="text-button" data-case-id="${e(id)}">${e(id)}</button>`)},{key:'state',label:'人工状态',render:x=>CASE_STATES[x]||x},{key:'reason_code',label:'接收结论',render:x=>REASONS[x]||x},{key:'version',label:'版本'},...base,{key:'duplicate_count',label:'重复次数'}];
  return [...base,{key:'point_code',label:'测点'},{key:'value',label:'原工程值'},{key:'unit',label:'原单位'},{key:'quality',label:'原质量'},{key:'measured_at',label:'测量时间',render:registryTime},...(view==='resolution'?[{key:'reasons',label:'解析原因',render:x=>(x||[]).map(k=>REASON_LABELS[k]||k).join('、')||'匹配无异常'},{key:'well_id',label:'历史井编号'},{key:'application_status',label:'终端实际版本',render:()=> '未确认'}]:[])];
 }
 async function load(page=1){if(!view||!can(view))return;const request=++generation,query=new URLSearchParams({...filters,view,page:String(page),page_size:'20'});if(page>1&&result)withSnapshot(query,result,{total:true});status.textContent='正在查询该口径…';target.innerHTML='';
  try{const data=await api.get(`/api/production-monitoring/quality?${query}`);if(!context.isCurrent()||request!==generation)return;result=data;
   status.textContent=`${VIEWS[view]} · ${filters.unresolved==='true'?'未解析归属专栏 · ':''}${data.coverage==='selected_well_only'?'仅覆盖所选井，不含其他未解析数据。':'当前全局或设备范围。'} 接收窗口 ${registryTime(data.from_time)} — ${registryTime(data.to_time)}${view==='cases'?` · 当前状态查询时刻 ${registryTime(data.status_queried_at)}${filters.well_id?'；井筛选按事实接收时刻绑定，不代表样本归属。':''}`:''}`;
   target.innerHTML=queryContext(context,data)+c.table(columns(),data.items)+pageControls(context,data);
  }catch(error){if(context.isCurrent()&&request===generation){result=null;status.textContent=error.status===409?'查询结果已变化，请重新查询第一页；不混合新旧结果。':ingestError(error);}}
 }
 async function showCase(id){const token=++selection;formModal?.close();selected=null;detail.textContent='正在读取事项当前版本…';
  try{const data=await api.get(`/api/ingest-reliability/cases/${encodeURIComponent(id)}`);if(!context.isCurrent()||token!==selection)return;selected=data.entity;
   detail.innerHTML=`<hr><h2>事项详情 · ${e(CASE_STATES[selected.state])}</h2><p class="registry-identity">${e(selected.id)} · 当前版本 ${e(selected.version)}</p><p>接收结论：${e(REASONS[selected.reason_code]||selected.reason_code)} · 更新时间：${e(registryTime(selected.updated_at))}</p><p class="registry-digest">原文摘要：${e(selected.payload_sha256)}</p><p>只改变人工处理流程，不改变原文、原始测点和隔离结论。</p><div class="registry-actions">${permissions.has('ingest.manage')?Object.entries(ACTIONS).filter(([key])=>allowed(selected.state,key)).map(([key,label])=>`<button class="button secondary" data-case-action="${key}">${label}</button>`).join(''):''}${factButtons(context,selected.fact_id)}</div><div data-case-audit></div>`;
   if(permissions.has('audit.read')){
    const audit=detail.querySelector('[data-case-audit]');try{const records=await api.get(`/api/ingest-reliability/audit?case_id=${encodeURIComponent(id)}&page=1&page_size=20`);if(!context.isCurrent()||token!==selection)return;audit.innerHTML='<h3>最近处置审计（最多 20 条）</h3>'+c.table([{key:'id',label:'审计引用'},{key:'action',label:'操作'},{key:'reason',label:'原因'},{key:'recorded_at',label:'记录时间',render:registryTime}],records.items);}catch(error){if(context.isCurrent()&&token===selection)audit.textContent=ingestError(error);}
   }
  }catch(error){if(context.isCurrent()&&token===selection)detail.textContent=ingestError(error);}
 }
 function openAction(action){if(!selected||!permissions.has('ingest.manage')||!allowed(selected.state,action))return;
  const captured={...selected},token=selection;let entry=pending.get(captured.id)||null;action=entry?.action||action;
  const form=document.createElement('form');form.dataset.caseForm='';form.className='field-registry';form.innerHTML=`<p>${e(ACTIONS[action])} · 事项 ${e(captured.id)}</p><p data-case-version>当前版本 ${e(entry?.version||captured.version)}</p><label>原因（2—500 字符）<textarea name="reason" rows="3" style="width:100%"></textarea></label><p data-case-notice role="status"></p><div class="registry-actions"><button class="button primary" data-case-submit>确认提交</button><button class="button secondary" type="button" data-case-cancel>取消</button></div>`;
  const modal=c.modal({title:ACTIONS[action],content:form});formModal=modal;
  const reason=form.elements.reason,notice=form.querySelector('[data-case-notice]'),submit=form.querySelector('[data-case-submit]'),cancel=form.querySelector('[data-case-cancel]');let sending=false,terminal=false;
  const valid=()=>context.isCurrent()&&token===selection&&form.isConnected;reason.value=entry?.reason||'';
  const uncertain=()=>{reason.disabled=true;submit.disabled=false;submit.textContent='按原操作重试';notice.textContent=`结果尚未确认，原操作 ${entry.operation.key} 已保留，重试不能修改内容。离开工作台后请先核查审计。`;};if(entry)uncertain();
  cancel.addEventListener('click',()=>{if(entry)c.toast('未确认操作保留在当前工作台，请返回该事项核查或重试。');modal.close();});
  form.addEventListener('submit',async event=>{event.preventDefault();if(!valid()||sending||terminal)return;
   if(!entry){const text=reason.value;if([...text.trim()].length<2||[...text].length>500||/[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(text)){notice.textContent='原因须为 2—500 字符，不能包含换行或控制字符。';return;}
    if(!allowed(captured.state,action)){notice.textContent='当前状态不再支持该操作，请关闭后重新选择。';submit.disabled=true;return;}
    entry={action,reason:text,version:captured.version,operation:client.prepareChange(`/api/ingest-reliability/cases/${encodeURIComponent(captured.id)}/${action}`,{reason:text,expected_version:captured.version})};pending.set(captured.id,entry);
   }
   sending=true;submit.disabled=true;reason.disabled=true;notice.textContent=`正在确认操作 ${entry.operation.key}…`;
   try{await entry.operation.run();pending.delete(captured.id);entry=null;if(!valid())return;terminal=true;modal.close();c.toast('处置已提交，正在重新读取当前版本。');const chosen=selection;await load();if(context.isCurrent()&&chosen===selection)await showCase(captured.id);}
   catch(error){if(error.status&&error.status<500){pending.delete(captured.id);entry=null;}if(!valid())return;
    if(error.status===409){notice.textContent='记录已变化，正在读取最新版本；原因为您保留。';try{const current=await api.get(`/api/ingest-reliability/cases/${encodeURIComponent(captured.id)}`);if(!valid())return;Object.assign(captured,current.entity);selected=current.entity;form.querySelector('[data-case-version]').textContent=`最新版本 ${captured.version} · ${CASE_STATES[captured.state]}`;reason.disabled=false;submit.disabled=!allowed(captured.state,action);submit.textContent='核对最新版本后确认';notice.textContent='已读取最新版本，原因已保留；请核对后再次确认，不会自动重试。';}catch{if(valid()){terminal=true;notice.textContent='最新版本读取失败，原因已保留；请关闭后重新核查。';}}}
    else if(!error.status||error.status>=500)uncertain();
    else if(error.status===422){notice.textContent=ingestError(error,true);reason.disabled=false;submit.disabled=false;}
    else{terminal=true;notice.textContent=ingestError(error,true);submit.disabled=true;}
   }finally{sending=false;}
  });reason.focus();
 }
 const submit=event=>{if(!event.target.matches('[data-quality-filter-form]'))return;event.preventDefault();++generation;result=null;closeSelection();try{filters=readFilters(event.target);load();}catch(error){target.innerHTML='';status.textContent=error.message;}};
 const click=event=>{const b=event.target.closest('button');if(!b||b.disabled)return;
  if(b.dataset.qualityView&&can(b.dataset.qualityView)){view=b.dataset.qualityView;++generation;result=null;filters={};closeSelection();buildFilter();filters=readFilters(formHost.querySelector('form'));load();}
  else if(b.hasAttribute('data-unresolved')){++generation;result=null;closeSelection();try{filters=readFilters(formHost.querySelector('form'));delete filters.well_id;filters.unresolved='true';load();}catch(error){status.textContent=error.message;}}
  else if(b.hasAttribute('data-page')){closeSelection();load(Number(b.dataset.page));}
  else if(b.dataset.caseId)showCase(b.dataset.caseId);else if(b.dataset.caseAction)openAction(b.dataset.caseAction);
 };
 root.addEventListener('click',click);root.addEventListener('submit',submit);context.registerDispose(()=>{++selection;formModal?.close();root.removeEventListener('click',click);root.removeEventListener('submit',submit);pending.clear();});
 if(!view){status.textContent='没有任何质量口径的访问权限，不发起受限查询。';return;}buildFilter();filters=readFilters(formHost.querySelector('form'));await load();
}
