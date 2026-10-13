import {registryTime} from '../../registry/client.js';
import {DELIVERY_STATES,ingestError} from '../../ingest/client.js';
import {queryContext,pageControls,withSnapshot,routeButton,bindRoutes} from './wells.js';

export const timeInputs=()=>'<label>接收开始（含时区）<input name="from_time" placeholder="默认近 24 小时"></label><label>接收结束（含时区）<input name="to_time" placeholder="默认本次查询时刻"></label>';
export function readFilters(form){
 const data=Object.fromEntries([...new FormData(form)].filter(([,v])=>v.trim()).map(([k,v])=>[k,v.trim()]));
 for(const key of ['from_time','to_time'])if(data[key]&&(!/(?:Z|[+-]\d{2}:\d{2})$/i.test(data[key])||!Number.isFinite(Date.parse(data[key]))))throw Error('时间必须包含有效日期和明确时区。');
 if(data.from_time&&data.to_time){const duration=Date.parse(data.to_time)-Date.parse(data.from_time);if(duration<0||duration>31*86400000)throw Error('接收窗口不能倒置或超过 31 天。');}
 return data;
}
export const optionalText=(part,render)=>part?.status==='ready'?render(part):({forbidden:'无权查看',unavailable:'暂不可用',not_recorded:'未记录'})[part?.status]||'未记录';
export function factButtons(context,factId){return factId&&context.permissions.has('ingest.read')&&context.permissions.has('ingest.raw.read')?`<button class="button secondary" data-preview-fact="${context.escapeHtml(factId)}">授权查看原文摘要</button><button class="button secondary" data-download-fact="${context.escapeHtml(factId)}">下载原文字节</button>`:'';}
export function bindFactAccess(context,root){
 const click=async event=>{const button=event.target.closest('[data-preview-fact],[data-download-fact]');if(!button||button.disabled||!context.isCurrent()||!context.permissions.has('ingest.read')||!context.permissions.has('ingest.raw.read'))return;
  const id=button.dataset.previewFact||button.dataset.downloadFact,path=`/api/ingest-reliability/facts/${encodeURIComponent(id)}`;
  if(button.dataset.downloadFact){button.disabled=true;try{await context.api.download(`${path}/raw`,{filename:`ingest-${id}.bin`});}catch(error){context.components.toast(ingestError(error),'error');}finally{if(context.isCurrent())button.disabled=false;}return;}
  const content=document.createElement('div'),preview=document.createElement('pre'),notice=document.createElement('p'),close=document.createElement('button');preview.dataset.rawPreview='';preview.textContent='正在授权读取…';preview.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto';close.textContent='关闭';close.className='button secondary';close.dataset.closeRaw='';content.append(notice,preview,close);
  const modal=context.components.modal({title:'报文原文（访问已审计）',content});close.addEventListener('click',()=>modal.close());
  try{const data=await context.api.get(`${path}/preview`);if(!context.isCurrent()||!content.isConnected)return;preview.textContent=data.text;notice.textContent=`SHA-256：${data.payload_sha256} · ${data.payload_bytes} 字节${data.truncated?' · 仅显示前 4096 字节':''}。摘要按 UTF-8 显示，精确字节请下载。`;}
  catch(error){if(context.isCurrent()&&content.isConnected)preview.textContent=ingestError(error);}
 };
 root.addEventListener('click',click);context.registerDispose(()=>root.removeEventListener('click',click));
}

export async function renderAcquisition(context){
 const {workspace:root,api,escapeHtml:e,components:c,permissions}=context;let result=null,details=null,filters={},generation=0,selection=0;
 root.innerHTML=context.pageHeader()+`<section class="field-registry"><p class="registry-boundary">这里统计已入库报文，不包含隔离报文。消息数、样本数和重复次数分别计量；回执交给 Broker 不代表现场设备已收到。</p><form class="registry-filters" data-acquisition-filter><label>网关<input name="gateway_id"></label><label>终端<input name="device_id"></label>${timeInputs()}<button class="button primary">查询</button></form><p data-acquisition-status role="status"></p><div data-messages></div><div data-message-detail></div></section>`;
 const target=root.querySelector('[data-messages]'),detail=root.querySelector('[data-message-detail]'),status=root.querySelector('[data-acquisition-status]');bindRoutes(context,root);bindFactAccess(context,root);
 const processing=(row,key)=>optionalText(row.processing,x=>key==='delivery_state'?(DELIVERY_STATES[x[key]]||'未安排'):x[key]??'未安排');
 async function load(page=1){const request=++generation;const query=new URLSearchParams({...filters,page:String(page),page_size:'20'});if(result)withSnapshot(query,result,{total:true});status.textContent='正在读取已入库报文…';
  try{const data=await api.get(`/api/production-monitoring/acquisition/messages?${query}`);if(!context.isCurrent()||request!==generation)return;result=data;status.textContent=`接收窗口：${registryTime(data.from_time)} — ${registryTime(data.to_time)}`;
   target.innerHTML=queryContext(context,data)+c.table([{key:'message_id',label:'消息编号',render:id=>c.trusted(`<button class="text-button" data-message="${e(id)}">${e(id)}</button>`)},{key:'gateway_id',label:'网关'},{key:'device_id',label:'终端'},{key:'received_at',label:'接收时间',render:registryTime},{key:'ingest_status',label:'入库状态',render:()=> '已入库'},
    {key:'sample_count',label:'样本数'},{key:'processing',label:'重复次数',render:(_,row)=>processing(row,'duplicate_count')},{key:'processing',label:'业务回执',render:(_,row)=>processing(row,'receipt_status')},{key:'processing',label:'Broker 发送',render:(_,row)=>processing(row,'delivery_state')},
    {key:'attribution',label:'测量时刻归属',render:part=>optionalText(part,x=>`${x.well_count} 口井 / ${x.unassigned_count} 条未归属`)}],data.items)+pageControls(context,data);
  }catch(error){if(context.isCurrent()&&request===generation){result=null;target.innerHTML='';status.textContent=error.message;}}
 }
 async function show(id,page){const paginate=page!==undefined;page??=1;const request=++selection;const query=new URLSearchParams({page:String(page),page_size:'20'});
  if(paginate&&details?.message_id===id)withSnapshot(query,details,{total:true});else if(result)withSnapshot(query,result);detail.textContent='正在读取报文详情…';
  try{const data=await api.get(`/api/production-monitoring/acquisition/messages/${encodeURIComponent(id)}?${query}`);if(!context.isCurrent()||request!==selection)return;details=data;
   detail.innerHTML=`<hr><h2>报文详情</h2><p class="registry-identity">${e(data.message_id)}</p><p class="registry-digest">SHA-256：${e(data.payload_sha256)}</p><p>网关 / 终端：${e(data.gateway_id)} / ${e(data.device_id)} · 序列 ${e(data.sequence_first)} — ${e(data.sequence_last)}</p><p>样本数 ${e(data.sample_count)} · 来源 ${e((data.sources||[]).join('、'))}</p><p>处理状态查询时刻：${e(data.processing?.status_queried_at?registryTime(data.processing.status_queried_at):'未查询')}</p>${permissions.has('registry.read')?`<h3>按样本测量时刻关联井集合</h3><p>${BigInt(data.well_count||'0')>1n?'跨井报文，不指定单一生产井。':'仅表示本包样本的历史归属，不代表当前绑定。'} 未归属样本：${e(data.unassigned_count??'暂不可用')}</p><div class="registry-actions">${(data.well_ids||[]).map(well=>routeButton(context,'well-detail',well,{well_id:well})).join('')}</div>${pageControls(context,{total:data.well_count,page:data.page,page_size:data.page_size},'data-detail-page')}`:'<p>井归属：无权查看</p>'}<div class="registry-actions">${factButtons(context,data.processing?.status==='ready'?data.processing.fact_id:null)}${permissions.has('ingest.read')?routeButton(context,'data-quality','隔离报文与人工处置',{view:'cases',gateway_id:data.gateway_id,device_id:data.device_id}):''}</div>`;
  }catch(error){if(context.isCurrent()&&request===selection){details=null;detail.innerHTML=`<p role="alert">${e(error.message)}</p>`;}}
 }
 const submit=event=>{event.preventDefault();++generation;++selection;result=null;details=null;target.innerHTML='';detail.innerHTML='';try{filters=readFilters(event.target);load();}catch(error){status.textContent=error.message;}};
 const click=event=>{const b=event.target.closest('button');if(!b||b.disabled)return;if(b.dataset.message)show(b.dataset.message);else if(b.hasAttribute('data-page')){++selection;details=null;detail.innerHTML='';load(Number(b.dataset.page));}else if(b.hasAttribute('data-detail-page')&&details)show(details.message_id,Number(b.dataset.detailPage));};
 root.querySelector('form').addEventListener('submit',submit);root.addEventListener('click',click);context.registerDispose(()=>root.removeEventListener('click',click));await load();
}
