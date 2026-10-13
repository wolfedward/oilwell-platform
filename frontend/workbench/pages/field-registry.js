import {registryError,registryTime} from '../../registry/client.js';
import {mountRegistryHistory} from '../../registry/history.js';
import {queryContext,pageControls,withSnapshot,routeButton,bindRoutes,formsFor,action,stateName,linkName} from './wells.js';

export async function renderDeviceWorkspace(context,{mapping=false}={}){
 const {workspace:root,api,escapeHtml:e,components:c,permissions,params}=context;
 let result=null,filters={},listGeneration=0,selection=0,selected=null,maps=null,history=null;
 const can=right=>permissions.has(right),button=(action,label,visible=true)=>visible?`<button class="button secondary" data-action="${action}">${label}</button>`:'';
 root.innerHTML=context.pageHeader()+`<section class="field-registry"><p class="registry-boundary">${mapping?'点表由服务器保存不可变版本和摘要，经另一身份独立复核后关联。只登记平台期望版本，不向设备下发。':'登记状态、通信链路和物理运行状态分别呈现。所有操作只变更平台台账，不启停现场设备。'}</p><form class="registry-filters" data-filter><label>网关编号<input name="gateway_id"></label><label>终端编号<input name="device_id"></label><label>状态<select name="status"><option value="">全部</option><option value="pending">待核查</option><option value="active">已启用</option><option value="suspended">停用待复核</option><option value="retired">已退役</option></select></label><button class="button primary">查询</button></form><div class="registry-actions">${button('create-device','登记终端',!mapping&&can('registry.devices.manage'))}<button class="button secondary" data-refresh>刷新</button></div><div data-device-list></div><div data-device-detail><p class="muted">请选择终端查看资料。</p></div></section>`;
 const listing=root.querySelector('[data-device-list]'),detail=root.querySelector('[data-device-detail]');
 bindRoutes(context,root);
 const client=formsFor(context,async({deviceId})=>{const chosen=selection;await load();if(context.isCurrent()&&chosen===selection&&deviceId)await select(deviceId);});
 context.registerDispose(()=>history?.dispose());
 async function load(page=1){const request=++listGeneration,query=new URLSearchParams({...filters,page:String(page),page_size:'20'});if(page>1&&result)withSnapshot(query,result);
  listing.textContent='正在读取终端台账…';
  try{const data=await api.get(`/api/registry/devices?${query}`);if(!context.isCurrent()||request!==listGeneration)return;result=data;
   listing.innerHTML=queryContext(context,data)+c.table([{key:'gateway_id',label:'网关'},{key:'device_id',label:'终端编号'},{key:'name',label:'名称'},{key:'transport',label:'接入方式'},
    {key:'status',label:'登记状态',render:stateName},{key:'version',label:'版本'},{key:'id',label:'资料',render:id=>c.trusted(`<button class="text-button" data-select-device="${e(id)}">${mapping?'点表与复核':'查看台账'}</button>`)}],data.items)+pageControls(context,data);
  }catch(error){if(context.isCurrent()&&request===listGeneration){result=null;listing.innerHTML=`<p class="inline-error" role="alert">${e(registryError(error))}</p>`;}}
 }
 async function select(id){
  const request=++selection;selected=null;maps=null;history?.dispose();root.dispatchEvent(new CustomEvent('registry:selection-start'));detail.textContent='正在读取终端资料…';
  try{const device=await client.selectDevice(id);if(!device||!context.isCurrent()||request!==selection)return;
   const query=withSnapshot(new URLSearchParams({page:'1',page_size:'20'}),device);
   const [bindings,assignments,definitions,link]=await Promise.all([
    api.get(`/api/registry/devices/${encodeURIComponent(id)}/bindings?${query}`),api.get(`/api/registry/devices/${encodeURIComponent(id)}/point-map-assignments?${query}`),api.get(`/api/registry/devices/${encodeURIComponent(id)}/point-maps?${query}`),
    can('acquisition.read')?api.get(`/api/registry/devices/${encodeURIComponent(id)}/link-status`).catch(()=>({unavailable:true})):Promise.resolve({forbidden:true})]);
   if(!context.isCurrent()||request!==selection)return;selected=device;maps=definitions;
   const retired=device.status==='retired',bound=bindings.items.find(x=>x.valid_to===null),assigned=assignments.items.find(x=>x.valid_to===null);
   detail.innerHTML=`<hr><h2>${e(device.name)}</h2><p class="registry-identity">${e(device.gateway_id)} / ${e(device.device_id)} · 资料版本 ${e(device.version)}</p><div class="registry-status-grid"><section class="metric-card"><span>登记状态</span><strong>${e(stateName(device.status))}</strong></section><section class="metric-card"><span>通信链路</span><strong>${link.forbidden?'无权查看':link.unavailable?'暂不可用':e(linkName(link.link_status))}</strong></section><section class="metric-card"><span>物理运行</span><strong>运行状态未接入</strong></section></div><p>接入：${e(device.transport)} · 用途：${device.usage==='production'?'生产用途':'调试用途'} · 厂家：${e(device.manufacturer||'未登记')} · 型号：${e(device.model||'未登记')}</p><p>最近资料变更：${e(registryTime(device.updated_at))} · 当前井：${bound?routeButton(context,'well-detail',bound.well_id,{well_id:bound.well_id}):'未绑定'}</p><p>平台期望点表：${e(assigned?.map_version||'未关联')} · 终端实际版本未确认</p><div class="registry-actions">${button('edit-device','编辑资料',!mapping&&!retired&&can('registry.devices.manage'))}${button('bind-device','绑定 / 迁移井',!mapping&&!retired&&can('registry.bindings.manage'))}${button('unbind-device','解绑',!mapping&&!retired&&!!bound&&can('registry.bindings.manage'))}${button('enable-device','人工核查后启用台账',!mapping&&!retired&&device.status!=='active'&&can('registry.review'))}${button('suspend-device','停用台账',!mapping&&device.status==='active'&&(can('registry.review')||can('registry.devices.manage')))}${button('retire-device','退役台账',!mapping&&!retired&&can('registry.review'))}${button('create-map','新增点表版本',!retired&&can('registry.maps.manage'))}${routeButton(context,mapping?'field-registry':'channel-mapping',mapping?'设备资料':'点表配置',{device_id:id})}</div>${mapping?'<p class="registry-boundary">边缘 IP、串口、波特率、采样周期：未接入。本页不保存未经设备确认的连接配置。</p>':''}<div data-device-history></div>`;
   history=mountRegistryHistory({root:detail.querySelector('[data-device-history]'),api,permissions,device,isCurrent:()=>context.isCurrent()&&request===selection});
  }catch(error){if(context.isCurrent()&&request===selection){selected=null;detail.innerHTML=`<p class="inline-error" role="alert">${e(registryError(error))}</p><button class="button secondary" data-select-device="${e(id)}">重新读取资料</button>`;}}
 }
 const submit=event=>{event.preventDefault();filters=Object.fromEntries([...new FormData(event.target)].filter(([,v])=>v.trim()).map(([k,v])=>[k,v.trim()]));++selection;selected=null;history?.dispose();root.dispatchEvent(new CustomEvent('registry:selection-start'));detail.textContent='请从查询结果选择终端。';load();};
 const click=event=>{const b=event.target.closest('button');if(!b||b.disabled)return;
  if(b.dataset.selectDevice)select(b.dataset.selectDevice);else if(b.hasAttribute('data-page'))load(Number(b.dataset.page));
  else if(b.hasAttribute('data-refresh')){const id=selected?.id;load().then(()=>{if(id&&context.isCurrent())select(id);});}
  else if(b.dataset.action)action(root,{action:b.dataset.action,device:selected,maps});
 };
 root.querySelector('[data-filter]').addEventListener('submit',submit);root.addEventListener('click',click);context.registerDispose(()=>root.removeEventListener('click',click));
 await load();if(context.isCurrent()&&params.device_id)await select(params.device_id);
}
export async function renderFieldRegistry(context){return renderDeviceWorkspace(context);}
