import {components,escapeHtml as esc} from '../components.js';
import {charts} from '../charts.js';

export const number=(value,digits=2)=>value===null||value===undefined||!Number.isFinite(Number(value))?'—':Number(value).toFixed(digits);
export const panel=(title,body,subtitle='',className='')=>`<article class="panel ${className}"><header><div><p class="eyebrow">OPERATIONS & EVIDENCE</p><h2>${esc(title)}</h2></div><span>${esc(subtitle)}</span></header><div class="panel-body">${body}</div></article>`;
export const metrics=items=>`<section class="preview-metrics">${items.map(([label,value,unit='',hint='',tone='primary'])=>`<article class="metric-card ${tone}"><p>${esc(label)}</p><strong>${esc(value)}<small>${esc(unit)}</small></strong><span>${esc(hint)}</span></article>`).join('')}</section>`;
export const table=(columns,rows)=>components.table(columns.map(column=>Array.isArray(column)?{key:column[0],label:column[1]}:column),rows);
export const note=text=>`<div class="preview-login-note">${esc(text)}</div>`;
export const links=items=>`<nav class="preview-subnav">${items.map(([id,label])=>`<button class="button secondary" data-page="${esc(id)}">${esc(label)}</button>`).join('')}</nav>`;

export function trend(items,{xKey='time',yKey='value',title='趋势',unit='',color='#1e6ee8'}={}){
  const valid=items.filter(p=>p[yKey]!==null&&p[yKey]!==undefined&&Number.isFinite(Number(p[yKey])));
  if(!valid.length)return components.empty('当前范围无有效观测值');
  const width=720,height=270,left=62,right=20,top=25,bottom=54;
  const values=valid.map(p=>Number(p[yKey]));let min=Math.min(...values),max=Math.max(...values);const margin=Math.max((max-min)*.15,Math.abs(max)*.03,1);
  min-=margin;max+=margin;
  const x=i=>left+i*(width-left-right)/Math.max(1,items.length-1),y=value=>top+(max-value)/(max-min)*(height-top-bottom);
  let path='',pen=false;
  for(let i=0;i<items.length;i++){const value=items[i][yKey];if(value===null||value===undefined||!Number.isFinite(Number(value))){pen=false;continue;}path+=`${pen?'L':'M'}${x(i)},${y(Number(value))} `;pen=true;}
  const ticks=Array.from({length:5},(_,i)=>{const v=min+(max-min)*i/4;return `<line x1="${left}" x2="${width-right}" y1="${y(v)}" y2="${y(v)}"/><text x="${left-8}" y="${y(v)+4}" text-anchor="end">${number(v)}</text>`;}).join('');
  const labels=items.map((p,i)=>i%Math.max(1,Math.ceil(items.length/5))===0||i===items.length-1?`<text x="${x(i)}" y="${height-bottom+22}" text-anchor="middle">${esc(String(p[xKey]).length>10?String(p[xKey]).slice(11,16):String(p[xKey]).slice(5))}</text>`:'').join('');
  const points=items.map((p,i)=>p[yKey]===null||p[yKey]===undefined||!Number.isFinite(Number(p[yKey]))?'':`<circle ${charts.pointAttributes({series:title,x:p[xKey],y:Number(p[yKey]),yUnit:unit})} cx="${x(i)}" cy="${y(Number(p[yKey]))}" r="7"/>`).join('');
  return `<figure class="chart"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}" data-plot-left="${left}" data-plot-right="${width-right}" data-plot-top="${top}" data-plot-bottom="${height-bottom}"><g class="chart-grid">${ticks}${labels}</g><path class="chart-line" style="--chart-color:${color}" d="${path}"/><g class="chart-points" style="--chart-color:${color}">${points}</g><text x="${left}" y="15" fill="#55708b" font-size="12">${esc(title)} (${esc(unit)})</text><text x="${width/2}" y="${height-5}" text-anchor="middle" fill="#55708b" font-size="12">时间</text></svg></figure>`;
}

export function filterMarkup(data,params,{dates=true}={}){
  const wells=data.wells||[];
  const scope=(key,label,nameKey)=>`<label>${label}<select name="${key}"><option value="">全部${label}</option>${[...new Map(wells.filter(w=>w[key]).map(w=>[w[key],w])).values()].map(w=>`<option value="${esc(w[key])}" ${w[key]===params[key]?'selected':''}>${esc(w[nameKey]||w[key])}</option>`).join('')}</select></label>`;
  return `<form class="preview-filter" data-preview-filter>${scope('unit_id','单位','unit_name')}${scope('group_id','井组','group_name')}<label>油井<select name="well_id"><option value="">全部 / 默认井</option>${wells.map(w=>`<option value="${esc(w.id)}" ${w.id===(params.well_id||params.id)?'selected':''}>${esc(w.code||w.id)} · ${esc(w.name)}</option>`).join('')}</select></label>${dates?`<label>开始日期<input type="date" name="from" value="${esc(params.from||data.from||'')}"></label><label>结束日期<input type="date" name="to" value="${esc(params.to||data.to||'')}"></label>`:''}<button class="button primary">查询</button><button type="button" class="button secondary" data-filter-reset>重置</button></form>`;
}
export function bindFilters(ctx){
  const form=ctx.workspace.querySelector('[data-preview-filter]');if(!form)return;
  form.onsubmit=event=>{event.preventDefault();ctx.navigate(ctx.page.id,{...ctx.params,...Object.fromEntries(new FormData(form))});};
  form.querySelector('[data-filter-reset]').onclick=()=>ctx.navigate(ctx.page.id,{});
}
