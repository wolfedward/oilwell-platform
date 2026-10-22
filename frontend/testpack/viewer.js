import {mountComparison,numeric,twoDecimals} from '../inversion/charts.js';

const ns='http://www.w3.org/2000/svg';
const statusNames={passed:'通过',failed:'未通过',blocked:'阻断',not_run:'未运行',incomplete:'未完成'};
const kindNames={cycle:'功图周期',history:'产液与用电',current:'电流信号',study:'研究输入',shape_only:'典型轮廓',protocol_fault:'协议异常'};
const node=(tag,text,className)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;};
const svgNode=(tag,attrs={})=>{const e=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e;};
function metrics(root,values){const grid=node('div',undefined,'metrics');for(const [label,value] of values){const box=node('div',undefined,'metric');box.append(node('span',label),node('strong',value??'未知'));grid.append(box);}root.append(grid);}
function detail(root,label,value){const d=node('details');d.append(node('summary',label),node('pre',JSON.stringify(value,null,2)));root.append(d);}

const quantityNames={active_power:'有功功率',crank_angle:'曲柄角',load:'载荷',displacement:'位移'};
const areaReasons={open_endpoints:'首末点未闭合，不补闭合边',invalid_reference_samples:'载荷或位移含无效点，不跨缺口积分',insufficient_points:'有效轮廓点数不足',incomplete_cycle_support:'样本未精确覆盖声明周期，不作为完整功图'};
// Only coordinate tooltips use two decimals. Small budget quantities must remain visible.
function measurement(value){const n=numeric(value);return n===null?'报告未提供':n!==0&&Math.abs(n)<.01?n.toExponential(6):Number.isInteger(n)?String(n):n.toFixed(4);}

export function inversionEvidence(report,caseId){
 const exact=id=>{const rows=(report?.checks||[]).filter(c=>c.check_id===id&&c.layer==='numeric');return rows.length===1?rows[0]:null;};
 return {report_loaded:!!report,case_check:exact(caseId+'-approved-inversion'),overall_check:exact('inversion-comparison')};
}

function budgetPanel(root,evidence){
 const section=node('section',undefined,'budget-panel');section.dataset.budgetPanel='';section.append(node('h3','反演验证依据'));
 const check=evidence?.case_check;
 if(!evidence?.report_loaded)section.append(node('p','未加载匹配报告，无法判断验证结果。'));
 else if(!check)section.append(node('p','此报告没有该样本的逐项反演结果。'));
 else{
  section.append(node('p',`当前样本检查：${statusNames[check.status]||check.status} · ${check.check_id}`,'subtle'));
  const counts=check.actual?.counts||{};
  const table=node('table'),head=node('tr');for(const title of ['预算项目','观测 / 必要允差','既定上限','逐项说明'])head.append(node('th',title));table.append(head);
  for(const [label,observed,limit,unit] of [
   ['声明参数域内的必要转矩允差','domain_speed_torque_bound_Nm','fixed_torque_bound_Nm','N·m'],
   ['残差项扣除后仍未覆盖的速度误差','uncovered_speed_error_rad_s','fixed_speed_bound_rad_s','rad/s']]){
   const actual=numeric(counts[observed]),bound=numeric(counts[limit]),row=node('tr');
   const state=actual===null||bound===null?'报告未提供，不能判断':actual>bound?'超出上限，预算不覆盖':'本项未超限，不代表反演获批';
   row.append(node('td',label),node('td',`${measurement(actual)}${actual===null?'':' '+unit}`),node('td',`${measurement(bound)}${bound===null?'':' '+unit}`),node('td',state));table.append(row);
  }
  section.append(table);
  const submitted=check.actual?.flags?.parameter_submitted;
  section.append(node('p',`${submitted===false?'未提交参数':submitted===true?'报告记录已提交参数':'报告未提供参数提交状态'}；未提供反演曲线时，不显示反演误差或覆盖率。`));
 }
 if(evidence?.overall_check)section.append(node('p',`全包反演比较：${statusNames[evidence.overall_check.status]||evidence.overall_check.status}（不是当前样本的逐项结论）`,'subtle'));
 section.append(node('p','这里展示已绑定报告的固定阈值与结果，不修改审批规则；必要允差不是预测载荷或 RMSE，不代表现场反演精度。','subtle'));root.append(section);
}

function captureDetails(root,data){
 const c=data.capture;if(!c)return;
 const band=node('div',undefined,'capture-heading');band.append(node('strong',`仿真设备 ${c.device_id}`),node('span',`仿真起始时间 ${c.simulation_start_local?.replace('T',' ')||'未知'} · Asia/Shanghai`));root.append(band);
 const states=node('div',undefined,'state-grid');
 for(const [label,text] of [['场景 / 注入条件',data.scene?.label||data.case_id],['样本质量',data.quality_summary?.status==='usable_samples'?'质量码均有效（不等于工况正常）':'存在缺测或异常样本'],data.kind==='cycle'?['反演结果',data.inversion_evidence?.case_check?.status==='blocked'?'已阻断 · 无批准曲线':'未提供批准曲线']:['物理故障确认','未进行']]){
  const block=node('div');block.append(node('span',label),node('strong',text));states.append(block);
 }root.append(states);
 root.append(node('p','场景名称是生成条件，不是现场诊断；质量码有效也不等同于采样标定、配对或反演审批通过。','subtle'));
 detail(root,'原始采样声明与标识',c);
}

function qualityTable(root,data){
 const summary=data.quality_summary;if(!summary)return;
 const section=node('section',undefined,'quality-panel');section.append(node('h3','原始通道质量统计'));
 section.append(node('p','按原始质量字节计数；有效值须质量码为 0 且数值有限。异常合计包括削顶、坏值、不确定及数值不一致。','subtle'));
 const table=node('table');table.dataset.qualityTable='';const head=node('tr');
 for(const label of ['通道 / 单位','总数','有效值','缺测 (1)','异常合计','削顶 (2)','坏值 (3)','不确定 (4)','码 0 数值异常'])head.append(node('th',label));table.append(head);
 for(const r of summary.channels){const row=node('tr');for(const v of [`${quantityNames[r.role]||r.role} / ${r.unit}`,r.total,r.valid,r.missing,r.abnormal,r.codes['2'],r.codes['3'],r.codes['4'],r.invalid_numeric])row.append(node('td',v));table.append(row);}
 const wrap=node('div',undefined,'table-scroll');wrap.append(table);section.append(wrap);root.append(section);
}

// Native values only: null or bad-quality samples split a path and are never filled.
export function lineChart(root,{title,xLabel,yLabel,points,color='#2476a8'}){
 const section=node('section',undefined,'line-chart');section.append(node('h3',title));
 const svg=svgNode('svg',{viewBox:'0 0 720 300',role:'img','aria-label':`${title}，${xLabel}，${yLabel}`});
 const valid=points.filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)&&p.valid!==false);
 function bounds(key){let lo=Infinity,hi=-Infinity;for(const p of valid){lo=Math.min(lo,p[key]);hi=Math.max(hi,p[key]);}if(!Number.isFinite(lo))return [0,1];const pad=(hi-lo||1)*.08;return [lo-pad,hi+pad];}
 const [xmin,xmax]=bounds('x'),[ymin,ymax]=bounds('y'),x=v=>70+(v-xmin)/(xmax-xmin)*625,y=v=>250-(v-ymin)/(ymax-ymin)*225;
 for(let i=0;i<=4;i++){
  const xx=70+i*625/4,yy=25+i*225/4;
  svg.append(svgNode('line',{x1:70,y1:yy,x2:695,y2:yy,class:'grid'}));
  const yt=svgNode('text',{x:61,y:yy+4,'text-anchor':'end'});yt.textContent=twoDecimals(ymax-(ymax-ymin)*i/4);svg.append(yt);
  const xt=svgNode('text',{x:xx,y:270,'text-anchor':'middle'});xt.textContent=twoDecimals(xmin+(xmax-xmin)*i/4);svg.append(xt);
 }
 for(const [text,attrs] of [[xLabel,{x:380,y:294,'text-anchor':'middle'}],[yLabel,{x:17,y:140,transform:'rotate(-90 17 140)','text-anchor':'middle'}]]){const t=svgNode('text',attrs);t.textContent=text;svg.append(t);}
 let path='',previous=false;
 for(const p of points){if(!Number.isFinite(p.x)||!Number.isFinite(p.y)||p.valid===false){previous=false;continue;}path+=`${previous?'L':'M'}${x(p.x).toFixed(3)},${y(p.y).toFixed(3)} `;previous=true;}
 svg.append(svgNode('path',{d:path,fill:'none',stroke:color,'stroke-width':'1.25','vector-effect':'non-scaling-stroke','data-native-curve':''}));
 const dot=svgNode('circle',{r:2.5,fill:color,visibility:'hidden'}),hit=svgNode('rect',{x:70,y:25,width:625,height:225,fill:'transparent',tabindex:0,'aria-label':'左右方向键查看坐标；Home / End 到首末点','data-chart-hit':''});
 svg.append(dot,hit);const tip=node('p','悬停查看原始点；坐标显示两位小数，缺测不补零。','chart-tooltip');tip.setAttribute('role','status');section.append(svg,tip);root.append(section);let selected=0;
 function select(i){if(!points.length)return;selected=Math.min(points.length-1,Math.max(0,i));const p=points[selected];tip.textContent=`${p.label??`索引 ${selected}`} · ${xLabel} ${twoDecimals(p.x)} · ${yLabel} ${twoDecimals(p.y)} · ${p.valid===false?'质量无效':'原始输入'}`;const good=Number.isFinite(p.x)&&Number.isFinite(p.y)&&p.valid!==false;dot.setAttribute('visibility',good?'visible':'hidden');if(good){dot.setAttribute('cx',x(p.x));dot.setAttribute('cy',y(p.y));}}
 hit.addEventListener('pointermove',e=>{const b=svg.getBoundingClientRect(),px=(e.clientX-b.left)/b.width*720,py=(e.clientY-b.top)/b.height*300;let best=Infinity,index=0;points.forEach((p,i)=>{if(!Number.isFinite(p.x)||!Number.isFinite(p.y))return;const d=(x(p.x)-px)**2+(y(p.y)-py)**2;if(d<best){best=d;index=i;}});select(index);});
 hit.addEventListener('keydown',e=>{if(!['Home','End','ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();select(e.key==='Home'?0:e.key==='End'?points.length-1:selected+(e.key==='ArrowRight'?1:-1));});
 for(const event of ['pointerleave','blur'])hit.addEventListener(event,()=>dot.setAttribute('visibility','hidden'));
 if(!valid.length)section.append(node('p','暂无有效坐标，不绘制替代曲线。','warning'));
}

export function renderCase(root,data){
 root.replaceChildren();root.append(node('h2',data.case_id),node('p',`${kindNames[data.kind]||data.kind} · 软件生成输入 · synthetic`,'subtle'));
 if(data.kind==='cycle'){
  const meta=data.metadata,channels=data.channels,reference=data.reference,c=data.capture,s=data.reference_summary;
  captureDetails(root,data);
  metrics(root,[['原始采样点',c?.frame_count??meta.frame_count],['采样率',`${c?.sample_rate_hz??meta.sample_rate_hz} Hz`],['有效点载荷范围',s?.load_range_kn?`${twoDecimals(s.load_range_kn[0])}–${twoDecimals(s.load_range_kn[1])} kN`:'未知'],['完整参考冲程',s?.stroke_m==null?'未知':`${twoDecimals(s.stroke_m)} m`],['声明周期时长',c?.cycle_duration_s==null?'未知':`${twoDecimals(c.cycle_duration_s)} s`],['声明冲次',c?.strokes_per_min==null?'未知':`${twoDecimals(c.strokes_per_min)} 次/min`]]);
  root.append(node('p',data.inversion_summary?.approved_execution_status==='passed'?
   '本报告有已批准的软件反演结果；本页仅加载原始数据，完整反演双图请在实际结果页面查看。软件同源比较不代表现场精度。':
   '当前没有已批准的电功率反演曲线。下图仅显示仿真载荷位移参考；不得据此认定现场反演精度。','warning'));
  if(data.inversion_summary?.assertion_status)inversionOutcomes(root,[data.inversion_summary]);
  const comparison=node('div');root.append(comparison);
  const mounted=mountComparison(comparison,{prediction:data.prediction||[],reference,filled:[],metadata:{source:'synthetic / 软件模型'},labels:{prediction:'电功率反演结果',reference:'仿真载荷位移参考'}});
  if(!data.prediction?.length){mounted.setVisibility('prediction',false);comparison.querySelector('[data-trace-toggle="prediction"]').disabled=true;}
  comparison.querySelector('[data-trace-toggle="filled"]').closest('label').hidden=true;
  for(const kind of ['power','motion','torque'])comparison.querySelector(`[data-chart-kind="${kind}"]`).hidden=true;
  const area=node('p',s?.area_kj==null?`参考功图面积：不可用 · ${areaReasons[s?.area_reason]||'未提供完整参考统计'}`:`参考功图面积（有向积分绝对值）：${twoDecimals(s.area_kj)} kJ · 有向积分 ${twoDecimals(s.signed_work_kj)} kJ`,'area-status');area.dataset.areaStatus='';root.append(area);
  root.append(node('p','仅对样本精确覆盖声明周期、全部载荷/位移点有效且首末点精确重合的轮廓积分；kN·m = kJ，不等于电能、反演误差或设备效率。','subtle'));
  const power=channels.active_power;
  lineChart(root,{title:'原始电功率 · 原生时间采样',xLabel:'时间（s）',yLabel:'有功功率（kW）',points:power.values.map((v,i)=>({x:c?(numeric(c.offset_ns)+i*numeric(c.sample_period_ns))/1e9:i/meta.sample_rate_hz,y:numeric(v)===null?null:power.unit==='W'?numeric(v)/1000:power.unit==='kW'?numeric(v):null,valid:power.quality[i]===0}))});
  qualityTable(root,data);budgetPanel(root,data.inversion_evidence);
  detail(root,'模型参数与数值诊断（不等同现场标定）',{model:meta.model,diagnostics:meta.diagnostics});
 }else if(data.kind==='history'){
  const daily=data.metadata.daily;
  metrics(root,[['原始区间数',data.metadata.frame_count],['积分日记录',daily.length],['日界线','Asia/Shanghai'],['来源','软件区间均值']]);
  root.append(node('p','仅对已覆盖区间积分；不完整日不外推成全天。此处是生成数据量，不代表实际入库数量。','subtle'));
  const pair=node('div',undefined,'chart-pair');root.append(pair);
  for(const [key,title,unit,color] of [['liquid_m3','产液积分','m³','#2476a8'],['net_kwh','净用电积分','kWh','#129c91']])lineChart(pair,{title,xLabel:'日序号',yLabel:unit,color,points:daily.map((r,i)=>({x:i+1,y:numeric(r[key]),label:r.date}))});
  detail(root,'每日积分与覆盖情况',daily);
 }else if(data.kind==='current'){
  captureDetails(root,data);
  metrics(root,[['采样率',`${data.metadata.sample_rate_hz} Hz`],['采样点',data.metadata.frame_count],['物理故障确认','未进行']]);
  for(const [role,channel] of Object.entries(data.channels))lineChart(root,{title:role+' · 原始瞬时电流',xLabel:'时间（s）',yLabel:'电流（A）',points:channel.values.map((v,i)=>({x:i/data.metadata.sample_rate_hz,y:numeric(v),valid:channel.quality[i]===0}))});
  qualityTable(root,data);detail(root,'信号假设',data.metadata);
 }else if(data.kind==='shape_only'){
  root.append(node('p','仅用于界面与工况轮廓检查，不计算反演精度，不作为物理机理或现场诊断证据。','warning'));
  const rows=data.shape.points||data.shape.points_normalized||[];
  lineChart(root,{title:data.shape.label||'典型轮廓',xLabel:`位移（${data.shape.x_unit||'归一化'}）`,yLabel:`载荷（${data.shape.y_unit||'归一化'}）`,points:rows.map(p=>({x:numeric(p[0]),y:numeric(p[1])}))});
  detail(root,'轮廓来源声明',data.shape);
 }else if(data.kind==='study'){
  root.append(node('h3',data.title),node('p','以下是研究输入模板与预期。实际运行状态请查看“实际运行报告”，模板存在不等于接口测试通过。','warning'));
  detail(root,'输入来源与依赖',{kind:data.study_kind,source:data.source,dependencies:data.dependencies,expected:data.expected});
  if(data.actual_records?.length){root.append(node('h3','同类研究的实际运行记录'));studyTable(root,data.actual_records);}
  else root.append(node('p','未绑定该类研究的实际运行记录。','subtle'));
 }else detail(root,'协议异常预期',data.expected);
 const files=node('details',undefined,'downloads');files.append(node('summary','原始文件与摘要'));
 for(const file of data.downloads||[]){const a=node('a',`${file.path} · ${file.bytes} bytes`);a.href='/pack/'+file.path;a.download=file.path.split('/').pop();files.append(a,node('p',`SHA-256 ${file.sha256}`,'subtle'));}root.append(files);
}

function studyTable(root,records){
 const table=node('table'),head=node('tr');for(const title of ['研究类型','实际计算状态','当前依赖 / 可选用','结果摘要'])head.append(node('th',title));table.append(head);
 for(const r of records){const row=node('tr');row.append(node('td',r.kind),node('td',r.status),node('td',`${r.dependency_current?'有效':'已失效'} / ${r.eligible?'是':'否'}`));const td=node('td');td.append(node('span',Object.entries(r.metrics).map(([k,v])=>`${k}: ${twoDecimals(v)}`).join('；')||'摘要见详情'));detail(td,'记录与校验摘要',r);row.append(td);table.append(row);}root.append(table);
}

function inversionOutcomes(root,outcomes){
 if(!outcomes?.length)return;
 const section=node('section');section.append(node('h3','反演实际执行与拒绝断言'));
 section.append(node('p','合成数据软件验证；同源周期比较不是独立现场比较，软件账号复核不是人工签名。','warning'));
 const execution={passed:'执行通过',refused:'已拒绝（非成功反演）',not_run:'尚未执行',failed:'执行失败'};
 for(const value of outcomes){
  const card=node('section',undefined,'budget-panel');card.dataset.inversionOutcome=value.case_id;
  card.append(node('h4',value.case_id),node('p',`断言${statusNames[value.assertion_status]||value.assertion_status} · ${execution[value.approved_execution_status]||'状态未知'}`),
   node('p',`阶段 ${value.phase} · 预期 ${value.expected_outcome} · 实际 ${value.observed_outcome} · 原因 ${value.reason_code||'无'}`));
  const metrics=value.metrics||{};
  if(value.approved_execution_status==='passed')card.append(node('p',`原始覆盖率 ${metrics.original_coverage} · 共同参考覆盖率 ${metrics.joint_coverage} · 载荷 RMSE ${metrics.load_rmse_N} N`));
  if(Object.keys(value.proof_summary||{}).length)detail(card,'逐点最大误差界（原始全精度，不四舍五入为零）',value.proof_summary);
  detail(card,'实际记录与摘要',{record_ids:value.record_ids,digests:value.digests,evidence_sha256:value.evidence_sha256});section.append(card);
 }root.append(section);
}

export function renderReport(root,report){
 root.replaceChildren();root.append(node('h2','实际运行报告'));
 root.append(node('p','此处读取原有运行报告；查看器页面升级不代表重新执行算法、网络链路或硬件验证。报告版本及运行标识见下方。','subtle'));
 if(!report){root.append(node('p','尚未绑定报告。数据包可以查看，但不能将离线数据量作为网络入库或接口通过的证据。','warning'));return;}
 const headline=node('p',`整体状态：${statusNames[report.status]||report.status}`,'status '+report.status);root.append(headline);
 metrics(root,Object.entries(report.layers).map(([layer,states])=>[layer,Object.entries(states).map(([s,n])=>`${statusNames[s]} ${n}`).join(' / ')||'未运行']));
 root.append(node('p','硬件验证：未运行。软件账号复核不代表人工签名；未执行远程控制。','warning'));
 inversionOutcomes(root,report.inversion_outcomes);
 const studies=report.checks.flatMap(c=>c.actual.study_records||[]);if(studies.length){root.append(node('h3','实际研究结果与依赖状态'));studyTable(root,studies);}
 const budgets=report.checks.filter(c=>c.layer==='numeric'&&c.check_id.endsWith('-approved-inversion'));
 if(budgets.length){const group=node('details');group.append(node('summary',`逐样本反演预算说明（${budgets.length} 项）`));for(const check of budgets)budgetPanel(group,{report_loaded:true,case_check:check});root.append(group);}
 const table=node('table'),head=node('tr');for(const title of ['检查项目','验证层','结果','观测值'])head.append(node('th',title));table.append(head);
 for(const item of report.checks){const row=node('tr');row.append(node('td',item.check_id),node('td',item.layer));const td=node('td');td.append(node('span',statusNames[item.status]||item.status,'status '+item.status));row.append(td,node('td',Object.entries(item.actual.counts).map(([k,v])=>`${k}: ${measurement(v)}`).join('；')||'详见摘要'));table.append(row);}
 root.append(table);detail(root,'版本与运行标识',report.run_metadata);detail(root,'可公开的完整报告',report);
}

async function start(){
 const get=async path=>{const r=await fetch(path,{cache:'no-store'});if(!r.ok)throw Error(`读取失败 (${r.status})`);return r.json();};
 const manifest=await get('/data/manifest.json'),report=await get('/data/report.json');
 metrics(document.querySelector('#summary'),[['数据包',manifest.profile],['场景数',manifest.cases.length],['文件数',manifest.artifacts.length],['报告',report?statusNames[report.status]:'未绑定']]);
 document.querySelector('#identity').textContent=`${manifest.pack_id} · 生成代码 ${manifest.generator.source_commit||manifest.generator.git_commit||'见 manifest'} · 仅回环地址只读访问`;
 renderReport(document.querySelector('#report'),report);let generation=0;
 async function show(c,button){const current=++generation;document.querySelector('#scene').replaceChildren(node('p','读取并核验原始文件…'));try{const data=await get('/data/cases/'+c.case_id+'.json');if(current!==generation)return;if(data.kind==='study')data.actual_records=(report?.checks||[]).flatMap(r=>r.actual.study_records||[]).filter(r=>r.kind===data.study_kind);if(data.kind==='cycle')data.inversion_evidence=inversionEvidence(report,c.case_id);renderCase(document.querySelector('#scene'),data);for(const b of document.querySelectorAll('#cases button'))b.classList.toggle('active',b===button);}catch(error){if(current===generation)document.querySelector('#scene').replaceChildren(node('p',error.message,'warning'));}}
 function catalog(){const nav=document.querySelector('#cases'),filter=document.querySelector('#filter').value;nav.replaceChildren();for(const c of manifest.cases){const kind=c.facts.data_kind||c.category;if(filter!=='all'&&kind!==filter)continue;const b=node('button',`${kindNames[kind]||kind} · ${c.case_id}`);b.addEventListener('click',()=>show(c,b));nav.append(b);}const first=nav.querySelector('button');if(first)first.click();else{generation++;document.querySelector('#scene').replaceChildren(node('p','本包不包含该类场景。典型轮廓在 extended 扩展包中提供，不以其他曲线代替。','warning'));}}
 document.querySelector('#filter').addEventListener('change',catalog);catalog();
 for(const name of ['scene','report'])document.querySelector('#'+name+'-tab').addEventListener('click',()=>{for(const other of ['scene','report']){document.querySelector('#'+other).hidden=other!==name;document.querySelector('#'+other+'-tab').classList.toggle('active',other===name);}});
 document.querySelector('#scene-tab').classList.add('active');
}
if(document.querySelector('#cases'))start().catch(error=>document.querySelector('#scene').replaceChildren(node('p',error.message,'warning')));
