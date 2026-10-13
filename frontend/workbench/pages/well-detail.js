import {registryTime,registryError} from '../../registry/client.js';
import {queryContext,pageControls,withSnapshot,routeButton,bindRoutes,stateName,linkName} from './wells.js';

export async function renderWellDetail(context){
 const {workspace:root,api,escapeHtml:e,components:c,params,permissions}=context;
 const wellId=params.well_id||params.wellId;if(!wellId){context.navigate('wells',{});return;}
 let result=null,generation=0,scope='current';
 root.innerHTML=context.pageHeader()+`<div class="registry-actions">${routeButton(context,'wells','返回井台账')}</div><div data-detail></div>`;bindRoutes(context,root);
 const target=root.querySelector('[data-detail]');
 async function load(page=1){const request=++generation;const query=new URLSearchParams({binding_scope:scope,page:String(page),page_size:'20'});if(page>1&&result)withSnapshot(query,result,{total:true});
  target.innerHTML='<p role="status">正在读取单井资料…</p>';
  try{const data=await api.get(`/api/production-monitoring/wells/${encodeURIComponent(wellId)}?${query}`);if(!context.isCurrent()||request!==generation)return;result=data;
   const well=data.well,availability=data.availability;
   const summary=(key,label,render)=>{const part=availability[key];return `<section class="metric-card"><span>${label}</span><strong>${part?.status==='ready'?render(part):part?.status==='forbidden'?'无权查看':'暂不可用'}</strong></section>`;};
   target.innerHTML=`<section class="field-registry"><h2>${e(well.name)}</h2><p>${e(well.area_code)} / ${e(well.well_code)} · ${well.status==='active'?'在用':'已归档'} · 版本 ${e(well.version)}</p><p class="muted">内部编号 ${e(well.id)}</p>${queryContext(context,data)}<div class="registry-status-grid">${summary('telemetry','历史归属样本',x=>e(x.sample_count))}${summary('link','当前绑定终端链路',x=>linkName(x.link_status))}${summary('cases','当前绑定终端未闭环事项',x=>e(x.total))}</div><p class="muted">事项汇总按当前绑定设备统计，不代表事项发生时的历史井归属。设备运行状态未接入。</p><div class="registry-actions">${permissions.has('telemetry.read')?routeButton(context,'well-realtime','实时测点',{well_id:wellId})+routeButton(context,'well-history','历史参数',{well_id:wellId}):''}</div><h3>设备绑定</h3><div class="registry-actions"><button class="button secondary" data-scope="current" ${scope==='current'?'disabled':''}>当前绑定</button><button class="button secondary" data-scope="history" ${scope==='history'?'disabled':''}>全部历史</button></div>${c.table([{key:'gateway_id',label:'网关'},{key:'device_code',label:'终端'},{key:'name',label:'名称'},{key:'status',label:'当前登记状态',render:stateName},{key:'valid_from',label:'绑定开始',render:registryTime},{key:'valid_to',label:'绑定结束',render:registryTime},{key:'device_id',label:'操作',render:id=>c.trusted(routeButton(context,'field-registry','设备台账',{device_id:id}))}],data.bindings.items)}${pageControls(context,data.bindings)}<p class="registry-boundary">产液计量、功图诊断、节能策略与组织井组仍待接入，不以采集数据替代这些业务结果。</p></section>`;
  }catch(error){if(context.isCurrent()&&request===generation){result=null;target.innerHTML=`<p role="alert" class="inline-error">${e(registryError(error))}</p><button class="button secondary" data-reload>重新查询</button>`;}}
 }
 const click=event=>{const b=event.target.closest('button');if(!b||b.disabled)return;if(b.dataset.scope){scope=b.dataset.scope;result=null;load();}else if(b.hasAttribute('data-page'))load(Number(b.dataset.page));else if(b.hasAttribute('data-reload'))load();};
 root.addEventListener('click',click);context.registerDispose(()=>root.removeEventListener('click',click));await load();
}
