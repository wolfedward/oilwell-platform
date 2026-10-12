import {charts} from '../../charts.js';
import {components,escapeHtml as esc} from '../../components.js';
import {panel,metrics,table,trend,number,note,links,filterMarkup,bindFilters} from '../ui.js';

const METRICS={active_power:['有功功率','kW'],power_kw:['有功功率','kW'],load:['悬点载荷','kN'],displacement:['位移','m'],current:['电流','A'],voltage:['电压','V'],temperature:['温度','℃'],stroke:['冲程','m'],strokes_per_minute:['冲次','min⁻¹']};

export async function renderMonitoring(ctx){
  const {api,workspace,page,params}=ctx;
  const data=await api.get('/api/preview/views/monitoring?'+new URLSearchParams(params));if(!ctx.isCurrent())return;
  const well=data.selected,production=data.production,last=production.at(-1),current={};
  const scopeWells=data.scope_wells||[];
  for(const row of data.telemetry)current[row.metric_code]=row;
  const head=ctx.pageHeader(page,`<span class="data-time">${esc(data.from)} — ${esc(data.to)}</span>`);
  const filter=filterMarkup(data,params);
  let body='';
  if(page.id==='production-monitor'){
    body=metrics([['井数',data.totals.total_wells,'口','快照内业务台账'],['运行井',data.totals.active_wells,'口','运行 / 间抽','success'],['低产井',data.totals.low_output_wells,'口','关联节能策略','warning'],['日耗电',number(data.totals.production.power_kwh),'kWh',data.totals.production.day||'无日报','cyan']])+
      `<div class="preview-grid">${panel('井组运行状态矩阵',`<div class="preview-well-matrix">${scopeWells.map(w=>`<button class="preview-well-tile ${esc(w.status)}" data-well="${esc(w.id)}"><strong>${esc(w.code||w.id)}</strong><span>${esc(w.name)}</span>${components.statusTag(w.status).html}<small>${esc(w.group_name)}</small></button>`).join('')}</div>`,'点击进入单井工作台','wide')}${panel('日产液趋势',trend(data.totals.trends,{xKey:'day',yKey:'daily_fluid',title:'日产液',unit:'m³'}))}${panel('日耗电趋势',trend(data.totals.trends,{xKey:'day',yKey:'power_kwh',title:'日耗电',unit:'kWh',color:'#10a58d'}))}${panel('运行状态分布',charts.bar([{label:'运行井',value:data.totals.active_wells},{label:'停机井',value:data.totals.stopped_wells},{label:'维护 / 离线',value:data.totals.offline_wells}],{title:'井状态',unit:'口'}))}${panel('产液与能耗排名',table([['well_id','油井'],['daily_fluid','产液(m³)'],['power_kwh','耗电(kWh)'],['day','生产日']],data.rankings))}</div>`;
  }else if(page.id==='well-realtime'){
    body=note('历史快照回看；暂停只影响当前画面，不停止设备或采集服务。')+
      `<div class="preview-subnav"><button class="button" data-pause>暂停画面</button><span data-pause-status>画面可查看</span></div>`+
      metrics(Object.entries(current).slice(0,8).map(([key,row])=>[METRICS[key]?.[0]||key,number(row.value),row.unit||METRICS[key]?.[1]||'',row.measured_at]))+
      `<div class="preview-grid">${Object.keys(current).slice(0,4).map(key=>panel(METRICS[key]?.[0]||key,trend(data.telemetry.filter(p=>p.metric_code===key),{xKey:'measured_at',title:METRICS[key]?.[0]||key,unit:current[key].unit||''}))).join('')}${panel('测点质量与时点',table([['metric_code','测点'],['value','数值'],['unit','单位'],['status','质量'],['measured_at','测量时间'],['source_type','来源']],Object.values(current)),'原快照记录','wide')}</div>`;
  }else if(page.id==='well-history'){
    body=metrics([['生产记录',production.length,'日','原始日报'],['遥测点',data.telemetry.length,'条','缺测保留为空'],['最新产液',number(last?.daily_fluid),'m³',last?.day||'—'],['最新耗电',number(last?.power_kwh),'kWh',last?.day||'—']])+
      `<div class="preview-grid">${panel('历史产液',trend(production,{xKey:'day',yKey:'daily_fluid',title:'日产液',unit:'m³'}))}${panel('历史耗电',trend(production,{xKey:'day',yKey:'power_kwh',title:'日耗电',unit:'kWh',color:'#10a58d'}))}${panel('逐日原始明细',table([['day','生产日'],['daily_fluid','产液(m³)'],['daily_oil','产油(t)'],['power_kwh','耗电(kWh)'],['pump_efficiency','泵效'],['stroke_m','冲程(m)'],['runtime_hours','运行(h)']],production),'同井同日口径','wide')}</div>`;
  }else if(page.id==='well-status'){
    body=metrics([['当前井',well?.code||well?.id||'—','','快照对象'],['原状态',well?.status||'—','','原始台账'],['运行时间',number(last?.runtime_hours),'h',last?.day||'—'],['状态样例',data.status_history.length,'条','补充软件场景']])+
      `<div class="preview-grid">${panel('运行状态维护',`<form data-status-form><div class="form-field"><label>目标状态</label><select name="status"><option value="running">运行</option><option value="stopped">停机</option><option value="maintenance">检修</option><option value="intermittent">间抽</option></select></div><div class="form-field"><label>原因说明</label><textarea name="reason" required minlength="2" placeholder="请输入状态调整原因"></textarea></div><button class="button primary">保存状态演练</button></form>`,'只保存预览状态')}${panel('状态历史样例',table([['time','时点'],['status','状态'],['reason','原因'],['duration','持续时间'],['actor','操作者']],data.status_history),'补充测试场景')}</div>`;
  }else if(page.id==='fluid-level'){
    const latest=data.fluid_level.at(-1);
    body=note('新增液面数据为软件测试场景；液面深度以井口为参考，不是产液率，也不是已接入液面仪的实测值。')+
      metrics([['最新液面深度',number(latest?.value),'m','井口以下深度'],['采样点',data.fluid_level.length,'点','小时测试序列'],['质量',latest?.quality||'—','','测试质量标志'],['变化范围',data.fluid_level.length?number(Math.max(...data.fluid_level.map(x=>x.value))-Math.min(...data.fluid_level.map(x=>x.value))):'—','m','当前时间范围']])+
      `<div class="preview-grid">${panel('液面深度与时间',trend(data.fluid_level,{title:'液面深度',unit:'m'}),'源类型 software_fixture','wide')}${panel('测量参考',`<dl class="preview-definitions"><dt>关联油井</dt><dd>${esc(well?.name)}</dd><dt>零点基准</dt><dd>井口</dd><dt>垂直方向</dt><dd>向下为正</dd><dt>当前时点</dt><dd>${esc(latest?.time||'无数据')}</dd></dl>`)}${panel('阈值事件样例',table([['time','事件时间'],['value','深度(m)'],['alarm','状态']],data.fluid_level.filter(r=>r.alarm==='warning')))}</div>`+links([['recovery-analysis','查看渗流恢复分析'],['recovery-lab','进入恢复研究']]);
  }else if(page.id==='haulage-monitor'){
    const latest=data.haulage.at(-1);
    body=note('罐监控与装卸记录为补充软件测试场景，可用于审查容量、累计和报警的页面呈现。')+
      metrics([['关联罐',latest?.tank_id||'—','','独立测试标识'],['液位',number(latest?.level_m),'m','测试场景'],['容积',number(latest?.volume_m3),'m³','非现场计量'],['设计容量',number(latest?.capacity_m3),'m³','测试参考']])+
      `<div class="preview-grid">${panel('罐液位趋势',trend(data.haulage,{yKey:'level_m',title:'液位',unit:'m'}))}${panel('容积趋势',trend(data.haulage,{yKey:'volume_m3',title:'容积',unit:'m³',color:'#0fa996'}))}${panel('液位与装卸记录',table([['tank_id','罐号'],['time','时间'],['operation','记录类型'],['level_m','液位(m)'],['volume_m3','容积(m³)'],['capacity_m3','容量(m³)']],data.haulage),'补充软件测试数据','wide')}</div>`;
  }
  workspace.innerHTML=head+filter+body;bindFilters(ctx);
  if(page.id==='production-monitor'){
    const matrix=workspace.querySelector('.preview-well-matrix');const rows=document.createElement('div');rows.hidden=true;
    rows.innerHTML=table([['code','井号'],['name','井名'],['group_name','井组'],['status','状态'],['is_low_output','低产井']],scopeWells);matrix.after(rows);
    const toggle=document.createElement('button');toggle.className='button secondary';toggle.dataset.monitorView='';toggle.textContent='切换列表';matrix.parentElement.before(toggle);
    toggle.onclick=()=>{rows.hidden=!rows.hidden;matrix.hidden=!rows.hidden;toggle.textContent=rows.hidden?'切换列表':'切换卡片';};
  }
  if(page.id==='well-history'){
    const button=document.createElement('button');button.className='button secondary';button.dataset.exportHistory='';button.textContent='导出当前历史明细';workspace.querySelector('.preview-filter').append(button);
    button.type='button';button.onclick=()=>{
      const fields=['day','well_id','daily_fluid','daily_oil','power_kwh','pump_efficiency','stroke_m','runtime_hours'];
      const cell=value=>'"'+String(value??'').replace(/^[=+@-]/,"'$&").replaceAll('"','""')+'"';
      const csv='\ufeff'+[fields,...production.map(row=>fields.map(key=>row[key]))].map(row=>row.map(cell).join(',')).join('\r\n');
      const link=document.createElement('a');link.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));link.download='历史参数-页面预览.csv';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);
    };
  }
  workspace.querySelectorAll('[data-well]').forEach(button=>button.onclick=()=>ctx.navigate('well-detail',{id:button.dataset.well}));
  const pause=workspace.querySelector('[data-pause]');if(pause)pause.onclick=()=>{const paused=pause.textContent==='暂停画面';pause.textContent=paused?'继续画面':'暂停画面';workspace.querySelector('[data-pause-status]').textContent=paused?'画面已暂停，历史数据保持不变':'画面可查看';};
  const form=workspace.querySelector('[data-status-form]');if(form)form.onsubmit=async event=>{event.preventDefault();if(!well||!form.reportValidity())return;const saved=await ctx.actions?.confirmRehearsal({action:'edit',objectId:'well-status/'+well.id,values:Object.fromEntries(new FormData(form))});if(saved){let receipt=workspace.querySelector('[data-state-receipt]');if(!receipt){receipt=document.createElement('p');receipt.dataset.stateReceipt='';form.after(receipt);}receipt.textContent='状态演练已保存：'+form.elements.status.selectedOptions[0].text+'（原始历史不变）';}};
}
