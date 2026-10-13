import {registryTime} from '../../registry/client.js';
import {queryContext,pageControls,withSnapshot,routeButton,bindRoutes} from './wells.js';
import {selectWell} from './well-realtime.js';
import {mountTelemetryChart} from '../telemetry-chart.js';

export async function renderWellHistory(context){
 const {workspace:root,api,escapeHtml:e,components:c,params}=context,well=params.well_id||params.wellId;
 if(!well)return selectWell(context);
 const end=params.to_time||new Date().toISOString(),start=params.from_time||new Date(Date.parse(end)-86400000).toISOString();
 let result=null,filters=null,generation=0,chart=null,controller=null;
 root.innerHTML=context.pageHeader()+`<section class="field-registry"><div class="registry-actions">${routeButton(context,'well-detail','单井详情',{well_id:well})}${routeButton(context,'well-realtime','从实时测点选择',{well_id:well})}${routeButton(context,'well-history','切换生产井')}</div><form class="registry-filters" data-history-filter><label>设备内部编号<input name="registry_device_id" value="${e(params.registry_device_id||'')}" required></label><label>测点编码<input name="point_code" value="${e(params.point_code||'')}" required></label><label>开始（含时区）<input name="from_time" value="${e(start)}" required></label><label>结束（含时区）<input name="to_time" value="${e(end)}" required></label><label>原始质量<select name="quality"><option value="">全部</option><option value="good">good</option><option value="uncertain">uncertain</option><option value="bad">bad</option></select></label><button class="button primary">查询历史</button></form><p class="registry-boundary">按测量时刻匹配历史井归属。时间区间左闭右开，最多 31 天；每页 200 条，趋势最多绘制最近 600 点；导出上限 10,000 条或 10 MiB，超过时请缩小范围。</p><button class="button secondary" data-export-history disabled>导出同一查询范围</button><p data-history-status role="status"></p><div data-history-results></div></section>`;
 const form=root.querySelector('form'),target=root.querySelector('[data-history-results]'),status=root.querySelector('[data-history-status]'),exportButton=root.querySelector('[data-export-history]');bindRoutes(context,root);
 function invalidate(){++generation;controller?.abort();result=null;filters=null;chart?.dispose();chart=null;exportButton.disabled=true;target.innerHTML='';}
 function values(){const next=Object.fromEntries([...new FormData(form)].filter(([,v])=>v.trim()).map(([k,v])=>[k,v.trim()]));
  if(!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(next.registry_device_id||''))throw Error('请选择终端或填写完整的设备内部编号。');
  if(!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(next.point_code||''))throw Error('测点编码无效。');
  for(const name of ['from_time','to_time'])if(!/(?:Z|[+-]\d{2}:\d{2})$/i.test(next[name]||'')||!Number.isFinite(Date.parse(next[name])))throw Error('时间须包含有效日期和明确时区。');
  const duration=Date.parse(next.to_time)-Date.parse(next.from_time);if(duration<0||duration>31*86400000)throw Error('查询时间不能倒置或超过 31 天。');return next;
 }
 async function load(page=1){if(!context.isCurrent()||!filters)return;controller?.abort();controller=new AbortController();const request=++generation,query=new URLSearchParams({...filters,page:String(page),page_size:'200'});
  if(result)withSnapshot(query,result,{total:true});exportButton.disabled=true;status.textContent='正在查询历史记录…';chart?.dispose();target.innerHTML='';
  try{const data=await api.get(`/api/production-monitoring/wells/${encodeURIComponent(well)}/history?${query}`,{signal:controller.signal});if(!context.isCurrent()||request!==generation)return;result=data;
   const trend=data.trend;status.textContent=`表格共 ${data.total} 条；曲线 ${trend.point_count} 点${trend.truncated?'（仅截取最近 600 点）':'（当前查询全部观测）'}。绘图范围：${trend.from_time?registryTime(trend.from_time):'无'} — ${trend.to_time?registryTime(trend.to_time):'无'}`;
   target.innerHTML=queryContext(context,data)+'<div data-history-chart></div>'+c.table([{key:'measured_at',label:'采集时刻',render:registryTime},{key:'received_at',label:'接收时刻',render:registryTime},
    {key:'value',label:'原工程值'},{key:'unit',label:'原单位'},{key:'quality',label:'原质量'},{key:'reasons',label:'解析原因',render:v=>(v||[]).join('、')||'无'},
    {key:'sequence',label:'原序号'},{key:'message_id',label:'消息编号'},{key:'source',label:'采集来源'},{key:'expected_map_version',label:'历史期望点表'}],data.items)+pageControls(context,data)+`<p class="muted">终端实际版本未确认；仅供人工核查，不开放自动控制。生产日报尚未接入。</p>`;
   const unit=trend.items.find(x=>x.expected_unit!==null&&x.expected_unit!==undefined)?.expected_unit??trend.items[0]?.unit??null;
   chart=mountTelemetryChart(target.querySelector('[data-history-chart]'),{items:trend.items,unit,intervalSeconds:trend.interval_seconds,intervalBasis:trend.interval_basis});exportButton.disabled=false;
  }catch(error){if(context.isCurrent()&&request===generation&&error.code!=='cancelled'){result=null;status.textContent=error.status===409?'查询结果已变化，请重新查询第一页；旧页面与导出已清除。':error.message;}}
 }
 const submit=event=>{event?.preventDefault();invalidate();try{filters=values();load();}catch(error){status.textContent=error.message;}};
 const dirty=()=>{invalidate();status.textContent='筛选已修改，请重新查询。';};form.addEventListener('submit',submit);form.addEventListener('input',dirty);
 const click=async event=>{const b=event.target.closest('button');if(!b||b.disabled)return;if(b.hasAttribute('data-page'))load(Number(b.dataset.page));
  if(b.hasAttribute('data-export-history')&&result&&filters){b.disabled=true;try{const query=withSnapshot(new URLSearchParams(filters),result,{total:true});await api.download(`/api/production-monitoring/wells/${encodeURIComponent(well)}/history.csv?${query}`,{filename:'well-history.csv'});}catch(error){if(context.isCurrent()){status.textContent=error.message;if(error.status===409)invalidate();}}finally{if(context.isCurrent()&&result)b.disabled=false;}}
 };
 root.addEventListener('click',click);context.registerDispose(()=>{++generation;controller?.abort();chart?.dispose();root.removeEventListener('click',click);});
 if(params.registry_device_id&&params.point_code){try{filters=values();await load();}catch(error){status.textContent=error.message;}}else status.textContent='从实时页面选择设备＋测点，或填写已知的历史终端内部编号后查询。';
}
