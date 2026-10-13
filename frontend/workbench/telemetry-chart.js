import {escapeHtml as e} from '../components.js';
import {registryTime} from '../registry/client.js';

export function mountTelemetryChart(root,{items,unit,intervalSeconds,intervalBasis}){
 const rows=items.filter(x=>Number.isFinite(Date.parse(x.measured_at))).slice().sort((a,b)=>Date.parse(a.measured_at)-Date.parse(b.measured_at));
 const numeric=rows.filter(x=>typeof x.value==='number'&&Number.isFinite(x.value));
 let disposed=false,selected=-1;
 if(!numeric.length){root.innerHTML='<p class="muted">当前范围没有可绘制观测，未补零或生成曲线。</p>';return{dispose(){disposed=true;}};}
 const times=rows.map(x=>Date.parse(x.measured_at)),start=Math.min(...times),end=Math.max(...times),span=Math.max(1000,end-start);
 const differences=times.slice(1).map((t,i)=>(t-times[i])/1000).filter(x=>x>0).sort((a,b)=>a-b);
 let interval=Number.isFinite(intervalSeconds)&&intervalSeconds>0?intervalSeconds:null,basis=intervalBasis;
 if(!interval&&differences.length){const mid=Math.floor(differences.length/2);interval=differences.length%2?differences[mid]:(differences[mid-1]+differences[mid])/2;basis='observed_median_display_only';}
 const low=Math.min(...numeric.map(x=>x.value)),high=Math.max(...numeric.map(x=>x.value)),pad=Math.max((high-low)*.12,Math.abs(high)*.01,.01),min=low-pad,max=high+pad;
 const x=row=>78+(Date.parse(row.measured_at)-start)/span*890,y=row=>270-(row.value-min)/(max-min)*228;
 const valid=row=>typeof row.value==='number'&&Number.isFinite(row.value)&&row.quality!=='bad'&&row.unit===unit&&!row.reasons?.includes('unit_mismatch');
 const segments=[];
 for(let i=1;i<rows.length;i++){
  const a=rows[i-1],b=rows[i],gap=(Date.parse(b.measured_at)-Date.parse(a.measured_at))/1000;
  if(!valid(a)||!valid(b)||(interval&&gap>1.5*interval))continue;
  const uncertain=a.quality==='uncertain'||b.quality==='uncertain';
  segments.push(`<path data-trend-segment data-quality="${uncertain?'uncertain':'good'}" d="M${x(a)},${y(a)} L${x(b)},${y(b)}" fill="none" stroke="${uncertain?'#cb8716':'#2873e8'}" stroke-width="1.7" ${uncertain?'stroke-dasharray="5 4"':''}/>`);
 }
 const ticks=Array.from({length:5},(_,i)=>{const px=78+i*890/4,py=270-i*228/4;return `<path d="M78 ${py}H968 M${px} 42V270" stroke="#e0e8f2" stroke-dasharray="4 5"/><text x="67" y="${py+4}" text-anchor="end">${e((min+i*(max-min)/4).toFixed(2))}</text><text x="${px}" y="291" text-anchor="middle">${(i*span/4000).toFixed(2)}</text>`;}).join('');
 root.innerHTML=`<section class="panel"><p class="muted">${interval?`${basis==='observed_median_display_only'?'观测间隔中位数（仅用于绘图）':'参考更新周期'}：${interval.toFixed(2)} s；大于 1.5 倍间隔断线。`:'更新周期未配置；不足以推断缺口。'} 坏值或单位不符不连接有效趋势；橙色虚线表示不确定质量。</p><svg data-telemetry-chart viewBox="0 0 1000 332" style="display:block;width:100%;max-height:440px" tabindex="0" role="group" aria-label="历史观测趋势，左右键逐点查看，Home 或 End 跳到首末点"><g fill="#657d96" font-size="12">${ticks}<text x="510" y="324" text-anchor="middle">距首个采集时刻（s）</text><text x="20" y="155" transform="rotate(-90 20 155)" text-anchor="middle">工程值（${e(unit||'无单位')}）</text></g>${segments.join('')}${rows.map((row,i)=>Number.isFinite(row.value)?`<circle data-hit-index="${i}" cx="${x(row)}" cy="${y(row)}" r="10" fill="transparent"/><circle data-point-marker data-marker-index="${i}" visibility="hidden" pointer-events="none" cx="${x(row)}" cy="${y(row)}" r="3.6" fill="white" stroke="${!valid(row)?'#d55457':row.quality==='uncertain'?'#cb8716':'#2873e8'}" stroke-width="1.8"/>`:'').join('')}</svg><div data-chart-tooltip role="status" aria-live="polite" style="min-height:54px;padding:8px 16px;background:#f3f7fc"><span>鼠标移到曲线，或聚焦图表后使用方向键查看坐标。</span></div></section>`;
 const svg=root.querySelector('svg'),tooltip=root.querySelector('[data-chart-tooltip]'),listeners=[];
 function show(index){if(disposed||!rows.length)return;selected=Math.max(0,Math.min(rows.length-1,index));const row=rows[selected];
  root.querySelectorAll('[data-point-marker]').forEach(marker=>marker.setAttribute('visibility',Number(marker.dataset.markerIndex)===selected?'visible':'hidden'));
  tooltip.innerHTML=`<strong data-coordinate-y>${e(typeof row.value==='number'?row.value.toFixed(2):'缺测')} ${e(row.unit||'')}</strong> · <span data-coordinate-x>${((Date.parse(row.measured_at)-start)/1000).toFixed(2)} s</span> · ${e(registryTime(row.measured_at))}<br><small>${e(row.device_id)} / ${e(row.point_code)} · ${e(row.quality)} · ${e((row.reasons||[]).join('、'))} · 原始值 ${e(row.value)} · 消息 ${e(row.message_id)}</small>`;
 }
 root.querySelectorAll('[data-hit-index]').forEach(hit=>{const enter=()=>show(Number(hit.dataset.hitIndex));hit.addEventListener('pointerenter',enter);listeners.push(()=>hit.removeEventListener('pointerenter',enter));});
 const key=event=>{const next={ArrowRight:selected+1,ArrowLeft:selected-1,Home:0,End:rows.length-1}[event.key];if(next!==undefined){event.preventDefault();show(next);}};
 const leave=()=>{if(document.activeElement===svg)return;root.querySelectorAll('[data-point-marker]').forEach(marker=>marker.setAttribute('visibility','hidden'));};
 svg.addEventListener('keydown',key);svg.addEventListener('pointerleave',leave);
 return{dispose(){disposed=true;listeners.forEach(fn=>fn());svg.removeEventListener('keydown',key);svg.removeEventListener('pointerleave',leave);}};
}
