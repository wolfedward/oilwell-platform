import {registryTime} from '../../registry/client.js';
import {queryContext,pageControls,withSnapshot,routeButton,bindRoutes} from './wells.js';

export async function selectWell(context){
 const {workspace:root,escapeHtml:e,api}=context;let data=null,generation=0,filters={};
 root.innerHTML=context.pageHeader()+`<section class="field-registry"><h2>选择生产井</h2><form class="registry-filters" data-well-filter><label>区块<input name="area_code"></label><label>井编号<input name="well_code"></label><button class="button primary">查询</button></form><div data-well-results></div></section>`;
 const target=root.querySelector('[data-well-results]');
 async function load(page=1){const request=++generation,query=new URLSearchParams({...filters,status:'active',page:String(page),page_size:'20'});if(page>1&&data)withSnapshot(query,data);target.textContent='正在查询…';
  try{const found=await api.get(`/api/registry/wells?${query}`);if(!context.isCurrent()||request!==generation)return;data=found;target.innerHTML=context.components.table([{key:'area_code',label:'区块'},{key:'well_code',label:'井编号'},{key:'name',label:'井名称'},{key:'id',label:'选择',render:id=>context.components.trusted(routeButton(context,context.page.id,'打开',{well_id:id}))}],found.items)+pageControls(context,found);}
  catch(error){if(context.isCurrent()&&request===generation)target.innerHTML=`<p role="alert">${e(error.message)}</p>`;}
 }
 bindRoutes(context,root);root.querySelector('form').addEventListener('submit',event=>{event.preventDefault();filters=Object.fromEntries([...new FormData(event.target)].filter(([,v])=>v.trim()));load();});
 const click=event=>{const b=event.target.closest('[data-page]');if(b&&!b.disabled)load(Number(b.dataset.page));};root.addEventListener('click',click);context.registerDispose(()=>root.removeEventListener('click',click));await load();
}
export async function renderWellRealtime(context){
 const {workspace:root,api,escapeHtml:e,components:c,params}=context,well=params.well_id||params.wellId;
 if(!well)return selectWell(context);
 let result=null,page=1,generation=0,paused=false,timer=null,requestController=null;
 root.innerHTML=context.pageHeader()+`<section class="field-registry"><div class="registry-actions">${routeButton(context,'well-detail','单井详情',{well_id:well})}${routeButton(context,'well-realtime','切换生产井')}<label>界面刷新<select data-refresh-interval><option value="5">5 秒</option><option value="10" selected>10 秒</option><option value="30">30 秒</option><option value="60">60 秒</option></select></label><button class="button secondary" data-pause>暂停刷新</button><button class="button primary" data-refresh-now>立即刷新</button></div><p class="registry-boundary">一次最多 32 个设备＋测点组合。界面刷新不改变设备采样周期；链路在线不代表数据新鲜；缺测不补零，最新坏值不会被较早好值替代。</p><p data-live-status role="status"></p><div data-latest></div></section>`;
 const target=root.querySelector('[data-latest]'),status=root.querySelector('[data-live-status]'),pauseButton=root.querySelector('[data-pause]'),interval=root.querySelector('[data-refresh-interval]');bindRoutes(context,root);
 function stop(){if(timer!==null)clearInterval(timer);timer=null;}
 function schedule(){stop();if(!paused&&context.isCurrent())timer=setInterval(()=>{if(context.isCurrent()&&!paused)load(1);},Number(interval.value)*1000);}
 function label(){status.textContent=`${paused?'已暂停，停留于':'最近查询截止'}：${result?registryTime(result.context.received_before):'尚未查询'}${page>1?' · 翻页期间暂停自动刷新':''}`;pauseButton.textContent=paused?'恢复刷新':'暂停刷新';}
 async function load(nextPage=1){
  if(!context.isCurrent())return;requestController?.abort();requestController=new AbortController();const request=++generation;
  const query=new URLSearchParams({page:String(nextPage),page_size:'32'});if(nextPage>1&&result)withSnapshot(query,result,{total:true});status.textContent='正在查询观测…';
  try{const data=await api.get(`/api/production-monitoring/wells/${encodeURIComponent(well)}/latest?${query}`,{signal:requestController.signal});if(!context.isCurrent()||request!==generation)return;
   result=data;page=nextPage;
   target.innerHTML=queryContext(context,data)+c.table([{key:'gateway_id',label:'网关'},{key:'device_id',label:'终端'},{key:'point_code',label:'测点'},{key:'expected_unit',label:'期望单位'},
    {key:'observation',label:'最新原工程值 / 单位',render:sample=>sample?`${sample.value} ${sample.unit??''}`:'缺测（null）'},
    {key:'observation',label:'原质量 / 解析原因',render:sample=>sample?`${sample.quality} / ${(sample.reasons||[]).join('、')||'无'}`:'未收到观测'},
    {key:'observation',label:'采集时刻',render:s=>s?registryTime(s.measured_at):'—'},{key:'observation',label:'接收时刻',render:s=>s?registryTime(s.received_at):'—'},
    {key:'age_seconds',label:'数据年龄',render:age=>age===null?'未知':`${Number(age).toFixed(2)} s${age<0?'（时钟异常）':''}`},
    {key:'observation',label:'原始引用 / 序号',render:s=>s?`${s.message_id} / ${s.sequence}`:'—'},
    {key:'registry_device_id',label:'核查',render:(id,slot)=>c.trusted(`<span data-slot-key="${e(id+'/'+slot.point_code)}">${routeButton(context,'well-history','查看历史',{well_id:well,registry_device_id:id,point_code:slot.point_code})}</span>`)}],data.items)+pageControls(context,data)+`<p class="muted">更新周期未配置。平台期望点表不等于终端实际采用版本；当前观测不作为自动控制资格。生产日报待接入。</p>`;label();
  }catch(error){if(context.isCurrent()&&request===generation&&error.code!=='cancelled'){
   paused=true;stop();pauseButton.textContent='恢复刷新';status.textContent=`查询失败：${error.message}${result?'；下方保留旧查询结果，请勿视为实时值。':''}`;
   if([401,403,409].includes(error.status)){result=null;target.innerHTML='';}
  }}
 }
 const click=event=>{const b=event.target.closest('button');if(!b||b.disabled)return;
  if(b.hasAttribute('data-pause')){paused=!paused;if(paused){stop();requestController?.abort();++generation;label();}else{load(1);schedule();}}
  else if(b.hasAttribute('data-refresh-now'))load(1);
  else if(b.hasAttribute('data-page')){paused=true;stop();load(Number(b.dataset.page));}
 };
 interval.addEventListener('change',schedule);root.addEventListener('click',click);context.registerDispose(()=>{stop();++generation;requestController?.abort();root.removeEventListener('click',click);interval.removeEventListener('change',schedule);});await load();schedule();
}
