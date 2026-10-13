import {createRegistryClient,registryError,registryTime} from '../../registry/client.js';
import {mountRegistryForms} from '../../registry/forms.js';

// Shared rendering helpers keep all text escaped and all counts/versions as strings.
export function queryContext(context,data){
 const stamp=data.context||data;
 const target=document.querySelector('[data-query-context]');
 if(context.isCurrent()&&target)target.textContent=`查询截止：${registryTime(stamp.received_before)} · 修订 ${stamp.registry_revision}`;
 return `<p class="muted">查询截止：${context.escapeHtml(registryTime(stamp.received_before))} · 台账修订 ${context.escapeHtml(stamp.registry_revision)}</p>`;
}
export function pageControls(context,result,attribute='data-page'){
 const {escapeHtml:e}=context,page=result.page||1,size=result.page_size||20,total=String(result.total??'0');
 const pages=(BigInt(total)+BigInt(size)-1n)/BigInt(size)||1n;
 return `<nav class="pagination"><span>共 ${e(total)} 条 · 第 ${page} / ${pages} 页</span><button type="button" ${attribute}="${page-1}" ${page<=1?'disabled':''}>上一页</button><button type="button" ${attribute}="${page+1}" ${BigInt(page)>=pages?'disabled':''}>下一页</button></nav>`;
}
export function withSnapshot(query,data,{total=false}={}){
 const stamp=data.context||data;
 query.set('revision',stamp.registry_revision);query.set('received_before',stamp.received_before);
 if(total)query.set('query_total',String(stamp.query_total??data.total));
 return query;
}
export function routeButton(context,page,label,params={}){
 return `<button class="text-button" type="button" data-open-page="${context.escapeHtml(page)}" data-params="${context.escapeHtml(JSON.stringify(params))}">${context.escapeHtml(label)}</button>`;
}
export function bindRoutes(context,root){
 const click=event=>{const button=event.target.closest('[data-open-page]');if(button&&context.isCurrent())context.navigate(button.dataset.openPage,JSON.parse(button.dataset.params||'{}'));};
 root.addEventListener('click',click);context.registerDispose(()=>root.removeEventListener('click',click));
}
export function formsFor(context,onChanged){
 const client=createRegistryClient(context.api,{isCurrent:context.isCurrent});
 const forms=mountRegistryForms({root:context.workspace,api:context.api,client,components:context.components,
  permissions:context.permissions,onChanged,isCurrent:context.isCurrent});
 context.registerDispose(()=>{forms.dispose();client.dispose();});return client;
}
export function action(root,detail){root.dispatchEvent(new CustomEvent('registry:action',{detail}));}
export const stateName=value=>({pending:'待核查登记',active:'台账已启用',suspended:'台账停用待复核',retired:'台账已退役'}[value]||value);
export const linkName=value=>({online:'链路在线',delayed:'上报延迟',offline:'链路离线',never_reported:'从未上报',clock_anomaly:'上报时钟异常'}[value]||'链路状态未知');

export async function renderWells(context){
 const {workspace:root,api,escapeHtml:e,components:c,permissions}=context;
 let result=null,generation=0,filters={};const manage=permissions.has('registry.wells.manage');
 root.innerHTML=context.pageHeader()+`<section class="field-registry"><p class="registry-boundary">生产井台账使用独立内部编号；建档不代表现场已投产，已绑定设备的井不能直接归档。</p><form class="registry-filters" data-filter><label>区块编号<input name="area_code"></label><label>井编号<input name="well_code"></label><label>状态<select name="status"><option value="">全部</option><option value="active">在用</option><option value="archived">已归档</option></select></label><button class="button primary">查询</button></form><div class="registry-actions">${manage?'<button class="button primary" data-action="create-well">登记生产井</button>':''}<button class="button secondary" data-export-wells disabled>导出当前筛选</button><button class="button secondary" data-refresh>刷新台账</button></div><p data-notice role="status"></p><div data-results></div></section>`;
 const target=root.querySelector('[data-results]'),notice=root.querySelector('[data-notice]'),exportButton=root.querySelector('[data-export-wells]');
 formsFor(context,async()=>{await load();context.components.toast('操作已提交，已重新读取台账。');});bindRoutes(context,root);
 async function load(page){
  const fresh=page===undefined;page??=1;
  const request=++generation;exportButton.disabled=true;notice.textContent='正在读取井台账…';
  const query=new URLSearchParams({...filters,page:String(page),page_size:'20'});
  if(!fresh&&result)withSnapshot(query,result);
  try{const data=await api.get(`/api/registry/wells?${query}`);if(!context.isCurrent()||request!==generation)return;
   result=data;notice.textContent='';exportButton.disabled=false;
   target.innerHTML=queryContext(context,data)+c.table([{key:'area_code',label:'区块'},{key:'well_code',label:'井编号'},{key:'name',label:'井名称'},
    {key:'status',label:'台账状态',render:x=>x==='active'?'在用':'已归档'},{key:'version',label:'版本'},
    {key:'id',label:'操作',render:(id,well)=>c.trusted(routeButton(context,'well-detail','单井详情',{well_id:id})+(manage&&well.status==='active'?` <button class="text-button" data-action="edit-well" data-well-id="${e(id)}">修改名称</button> <button class="text-button" data-action="archive-well" data-well-id="${e(id)}">归档</button>`:''))}],data.items)+pageControls(context,data);
  }catch(error){if(context.isCurrent()&&request===generation){result=null;target.innerHTML='';notice.textContent=registryError(error);}}
 }
 const filter=event=>{event.preventDefault();filters=Object.fromEntries([...new FormData(event.target)].filter(([,v])=>v.trim()).map(([k,v])=>[k,v.trim()]));load();};
 const click=async event=>{const button=event.target.closest('button');if(!button||button.disabled)return;
  if(button.dataset.action)action(root,{action:button.dataset.action,well:result?.items.find(x=>x.id===button.dataset.wellId)});
  else if(button.hasAttribute('data-page'))load(Number(button.dataset.page));
  else if(button.hasAttribute('data-refresh'))load();
  else if(button.hasAttribute('data-export-wells')&&result){button.disabled=true;try{await api.download(`/api/production-monitoring/wells.csv?${withSnapshot(new URLSearchParams(filters),result,{total:true})}`,{filename:'wells.csv'});}catch(error){if(context.isCurrent())notice.textContent=error.message;}finally{if(context.isCurrent()&&result)button.disabled=false;}}
 };
 root.querySelector('[data-filter]').addEventListener('submit',filter);root.addEventListener('click',click);context.registerDispose(()=>root.removeEventListener('click',click));await load();
}
