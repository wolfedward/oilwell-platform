import {createInversionClient} from './client.js';
import {element,mountActionForm,errorText} from './forms.js';
import {mountComparison,numeric,twoDecimals,cycleRate,referenceSupported} from './charts.js';

const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SOURCE_LABELS={synthetic:'合成数据',bench_measurement:'台架测量',field_measurement:'现场测量',unknown:'来源未确认'};
const FIELD_LABELS={not_field_validated:'尚未通过现场验证',single_field_comparison:'单次现场比较（不构成精度认证）'};
export function runStateText(run) {
  if(run.cancelling||run.state==='running'&&run.reason_code==='cancel_requested')return '正在取消';
  if(run.state==='blocked'&&run.reason_code==='not_applicable')return '数据不合格';
  return {queued:'等待计算',running:'计算中',completed:'计算已完成',not_applicable:'数据不合格',failed:'计算失败',blocked:'运行受阻',cancelled:'已取消',timed_out:'计算超时'}[run.state] || `未计算（${run.state||'尚未提交'}）`;
}

function metadataList(root,entries) {
  const list=element('dl',undefined,'inversion-metadata');
  for(const [label,value] of entries){
    list.append(element('dt',label),element('dd',value===null||value===undefined?'未提供':typeof value==='object'?JSON.stringify(value):value));
  }
  root.append(list);
}

function renderMetricMap(root,title,metrics) {
  const section=element('section',undefined,'inversion-metrics');section.append(element('h3',title));
  for(const [name,metric] of Object.entries(metrics||{})){
    const row=element('p');row.dataset.metric=name;
    row.textContent=`${name}：${metric?.value===null||metric?.value===undefined?'未知（'+(metric?.reason||'未提供原因')+'）':twoDecimals(metric.value)} ${metric?.unit||''}`;
    section.append(row);
  }
  root.append(section);
}

function renderComparison(root,report,title) {
  if(!report){root.append(element('p',`${title}：未提供`));return;}
  const section=element('section',undefined,'inversion-report');section.append(element('h2',title));
  metadataList(section,[['指标范围',report.scope],['原预测状态',report.prediction_status],['原预测原因',report.prediction_reason_codes],['独立性依据',report.independence],['无效原因',report.invalid_reasons]]);
  renderMetricMap(section,'主误差',report.primary);
  renderMetricMap(section,'覆盖率：数值支撑与共同参考分别列出',report.coverage);
  renderMetricMap(section,'地面有符号功 ∫F ds（不代表井下功或产量）',report.work);
  for(const [stroke,metrics] of Object.entries(report.by_stroke||{}))renderMetricMap(section,`分段误差 · ${stroke}`,metrics);
  root.append(section);
}

function validateIdentity(detail,value) {
  if(value?.result_id!==detail.id||value?.content_sha256!==detail.content_sha256)throw Error('结果标识或摘要不一致，请重新读取。');
}

export async function loadResultPoints(client,detail,current) {
  const points={prediction:[],comparison:[],display_only_comparison:[],display_only_fills:[]};
  let total=null,offset=0;
  do{
    const page=await client.get(`/results/${detail.id}/points?offset=${offset}&limit=1000`);
    if(!page||!current())return null;
    validateIdentity(detail,page);
    if(!Number.isSafeInteger(page.total)||page.total<0||page.total>131072||page.offset!==offset||page.limit!==1000||!Array.isArray(page.rows)||page.rows.length!==Math.min(1000,page.total-offset))throw Error('结果分页数量不完整，请重新读取。');
    if(total!==null&&page.total!==total)throw Error('结果分页数量已变化，请重新读取。');
    total=page.total;
    for(const row of page.rows){
      if(!Object.hasOwn(points,row.kind)||!Array.isArray(row.columns)||!Array.isArray(row.values)||row.columns.length!==row.values.length||row.columns.length>64||new Set(row.columns).size!==row.columns.length||row.columns.some(name=>typeof name!=='string'||['__proto__','constructor','prototype'].includes(name)))throw Error('结果点格式无效。');
      const point=Object.fromEntries(row.columns.map((name,index)=>[name,row.values[index]]));
      if(typeof point.power_index!=='string'||!/^\d+$/.test(point.power_index)||typeof point.time_ns!=='string')throw Error('结果原始索引或时间格式无效。');
      points[row.kind].push(point);
      if(points[row.kind].length>32768)throw Error('单类结果点超过 32768 上限。');
    }
    offset+=page.rows.length;
  }while(offset<total);
  if(points.prediction.length!==detail.body.point_count)throw Error('预测点总数与结果不一致。');
  return {points,total};
}

function ranges(points,key) {
  let low=Infinity,high=-Infinity;
  for(const point of points){const value=numeric(point[key]);if(value!==null){low=Math.min(low,value);high=Math.max(high,value);}}
  return Number.isFinite(low)?[low,high]:null;
}

function renderSnapshot(root,metadata) {
  const section=element('section',undefined,'inversion-snapshot');section.append(element('h2','本次运行输入快照元数据'));
  metadataList(section,[['哈希含义','原始完整工件身份（不是本页投影哈希）'],['结果摘要',metadata.content_sha256],['输入快照摘要',metadata.snapshot_sha256],
    ['冻结请求',metadata.request],['历史井',metadata.applicability?.assignment?.well_id],['历史机械配置',metadata.applicability?.mechanical],
    ['历史选用 / 回退',metadata.applicability?.selection],['已声明并通过覆盖校验的工况',metadata.applicability?.operating_conditions],
    ['跨通道相对对齐界限（ns）',metadata.observation?.relative_alignment_ns],['相对时序分项',metadata.observation?.relative_timing_terms],
    ['绝对历史时间支持界限（ns）',metadata.observation?.uncertainty_ns],['绝对时序分项',metadata.observation?.absolute_timing_terms],
    ['观测时间（原始精确字符串）',metadata.observation]]);
  for(const [role,pin] of Object.entries(metadata.channel_pins||{})){
    const card=element('section',undefined,'inversion-source');card.append(element('h3',role));
    metadataList(card,[['录波 / 接收定位',{gateway_id:pin.gateway_id,device_id:pin.device_id,capture_id:pin.capture_id}],
      ['物理生产端点',pin.physical_endpoint],['来源方式',pin.source],['通道',pin.channel_id],['流',pin.stream_id],['原始索引范围',`${pin.first_index} — ${pin.last_index}`],
      ['标定 pin',pin.calibration_pin],['采样 pin',pin.sampling_pin],['同步 pin',pin.sync_pin]]);section.append(card);
  }
  root.append(section);
}

export function mountResultDetail(ctx,root,client,id) {
  let disposed=false,chart=null;const forms=[];
  const current=()=>!disposed&&(ctx.isCurrent?.()??true)&&ctx.workspace.contains(root);
  client=createInversionClient({...ctx,isCurrent:current});
  function disposeContent(){chart?.dispose();chart=null;forms.splice(0).forEach(form=>form.dispose());}
  async function load(){
    disposeContent();root.replaceChildren(element('p','读取完整结果与原始支撑点…'));
    try{
      if(!UUID.test(id))throw Error('无效结果标识。');
      const detail=await client.get('/results/'+id);if(!detail||!current())return;
      const run=await client.get('/runs/'+detail.run_id);if(!run||!current())return;
      if(run.run_id!==detail.run_id||run.result_id!==detail.id)throw Error('结果与原运行标识不一致。');
      const comparison=await client.get('/results/'+id+'/comparison');if(!comparison||!current())return;validateIdentity(detail,comparison);
      const loaded=await loadResultPoints(client,detail,current);if(!loaded||!current())return;
      let metadata=null,metadataMessage='本次输入快照元数据权限不可用：需要 waveform.read。';
      if(ctx.permissions.has('waveform.read')){
        try{
          const value=await client.get(`/results/${id}?include_input_metadata=true`);if(!value||!current())return;
          if(value.id!==detail.id||value.content_sha256!==detail.content_sha256||value.snapshot_sha256!==detail.snapshot_sha256)throw Error('输入快照摘要不一致。');
          metadata=value.input_snapshot_metadata;validateIdentity(detail,metadata);
          if(metadata.snapshot_sha256!==detail.snapshot_sha256)throw Error('输入快照摘要不一致。');
        }catch(error){
          if(error.status===401||error.status===403)throw error;
          metadataMessage='本次输入快照元数据不可用：'+(error.code||error.message);
        }
      }
      if(!current())return;
      root.replaceChildren();root.dataset.loadedPoints=String(loaded.total);
      const approved=detail.review.state==='approved';
      const reviewLabel=approved?(detail.current_applicability.state==='restricted'?'已复核但当前受限':'已独立复核'):
        ({unreviewed:'未复核',rejected:'复核已拒绝',revoked:'复核已撤销'}[detail.review.state]||detail.review.state);
      const sourceLabel=SOURCE_LABELS[detail.source]||'来源状态未识别';
      const applicabilityLabel={applicable:'当前适用',restricted:'当前受限'}[detail.current_applicability.state]||'当前适用性未确认';
      root.append(element('h2',`计算已完成 · ${reviewLabel}`));
      metadataList(root,[['结果',detail.id],['来源类型',sourceLabel],['历史计算状态',runStateText({state:detail.execution})],['当前适用性',applicabilityLabel],
        ['当前限制原因',(detail.current_applicability.reasons||[]).join('；')||'未报告当前限制'],
        ['固定参数',run.request?.parameter_id],['固定模型',run.request?.model_id],['本次选用版本',run.request?.selection_version],
        ['复核状态',reviewLabel],['复核版本',detail.review.version],['现场验证状态',FIELD_LABELS[detail.field_validation.state]||'现场验证状态未确认'],['现场精度认证','未认证；数值比较与独立复核均不构成现场精度认证'],
        ['完整几何冲程','完整几何冲程未提供'],['本次物理周期折算冲次（次/分）',twoDecimals(cycleRate(detail.body.cycle))],['完整读取点数',loaded.total]]);
      const valid=loaded.points.prediction.filter(p=>p.status==='valid'),span=ranges(valid,'s_m'),loadRange=ranges(valid,'load_N');
      const referenceRange=ranges(loaded.points.comparison.filter(referenceSupported),'load_value_si');
      metadataList(root,[['有效预测点位移跨度',span?`${twoDecimals(span[1]-span[0])} m`:'未知'],['有效预测载荷范围',loadRange?loadRange.map(value=>twoDecimals(value/1000)).join(' — ')+' kN':'未知'],
        ['已对齐参考载荷范围',referenceRange?referenceRange.map(value=>twoDecimals(value/1000)).join(' — ')+' kN':'未知']]);
      chart=mountComparison(root,{prediction:loaded.points.prediction,reference:loaded.points.comparison,filled:loaded.points.display_only_fills,metadata:{source:sourceLabel}});
      renderComparison(root,comparison.comparison,'原始支撑比较');
      if(comparison.filled_comparison)renderComparison(root,comparison.filled_comparison,'仅展示补绘比较（不改变主指标或现场判定）');
      const states=element('details');states.append(element('summary','逐点状态（完整读取，按 100 点查看）'));root.append(states);
      let stateOffset=0;const stateBody=element('div'),previous=element('button','上一页','button'),next=element('button','下一页','button');states.append(stateBody,previous,next);
      function showStates(){stateBody.replaceChildren();for(const point of loaded.points.prediction.slice(stateOffset,stateOffset+100))stateBody.append(element('p',`索引 ${point.power_index} · ${point.time_ns} ns · ${point.status} · ${(point.reason_codes||[]).join(', ')}`));previous.disabled=stateOffset===0;next.disabled=stateOffset+100>=loaded.points.prediction.length;}
      previous.onclick=()=>{stateOffset=Math.max(0,stateOffset-100);showStates();};next.onclick=()=>{stateOffset+=100;showStates();};showStates();
      if(metadata)renderSnapshot(root,metadata);else root.append(element('p',metadataMessage));
      const history=element('section',undefined,'inversion-report');history.append(element('h2','机械与选用 / 回退历史'));root.append(history);
      const historyAssembly=metadata?.request?.assembly_id||run.request?.assembly_id;
      if(historyAssembly&&UUID.test(historyAssembly)){
        for(const [resource,label] of [['selections','持久化选用及回退'],['versions','机械声明与有效期']]){
          const block=element('section'),rows=element('div'),previous=element('button','上一页','button'),next=element('button','下一页','button');
          block.append(element('h3',label),element('p','偏移分页可能随新增版本移动；此页不声明完整历史。'),rows,previous,next);history.append(block);let offset=0;
          async function historyPage(){
            previous.disabled=next.disabled=true;
            try{
              const page=await client.get(`/assemblies/${historyAssembly}/${resource}?offset=${offset}&limit=20`);if(!page||!current())return;
              rows.replaceChildren();for(const item of page.items)metadataList(rows,resource==='selections'?
                [['选用版本',item.version],['选用记录',item.id],['固定 pins / 回退来源',item.body]]:
                [['机械版本',item.version],['原声明',item.declared],['有效期修订',item.effective]]);
              if(!page.items.length)rows.append(element('p','此页没有历史记录。'));
              previous.disabled=offset===0;next.disabled=page.items.length<20;
            }catch(error){if(current())rows.textContent=errorText(error);}
          }
          previous.onclick=()=>{offset=Math.max(0,offset-20);historyPage();};next.onclick=()=>{offset+=20;historyPage();};
          await historyPage();if(!current())return;
        }
      }else history.append(element('p','历史配置标识不可用；不能用当前配置替代本次输入快照。'));
      const actions=element('section',undefined,'inversion-result-actions');actions.append(element('h2','独立复核与完整结果导出'));root.append(actions);
      if(ctx.permissions.has('inversion.review')){
        const version=detail.review.version,expected=version?{kind:'match',version}:{kind:'absent'};
        if(detail.review.state==='unreviewed')forms.push(mountActionForm(actions,client,{action:'results.review',path:`/results/${id}/review`,permission:'inversion.review',expected,label:'独立复核',decisions:[['approve','批准'],['reject','拒绝']],slot:'result-decision:'+id},load));
        if(approved)forms.push(mountActionForm(actions,client,{action:'results.revoke',path:`/results/${id}/revoke`,permission:'inversion.review',expected,label:'撤销复核',slot:'result-decision:'+id},load));
      }
      if(['inversion.export','waveform.read','waveform.raw.read','waveform.export'].every(p=>ctx.permissions.has(p))){
        const options=element('div');actions.append(options);
        const format=element('select'),intent=element('select');format.setAttribute('aria-label','导出格式');intent.setAttribute('aria-label','导出用途');
        format.append(new Option('JSON 完整结果','json'),new Option('CSV 完整结果','csv'));intent.append(new Option('定量用途','quantitative'));
        if(ctx.permissions.has('inversion.audit.read'))intent.append(new Option('证据用途','evidence'));options.append(format,intent);
        const exportHost=element('div');actions.append(exportHost);let exportForm;
        const downloadState=element('p');downloadState.setAttribute('role','status');actions.append(downloadState);
        function exportOptions(){exportForm?.dispose();exportHost.replaceChildren();const saved=client.pending.get('result-export:'+id);const body=saved?.envelope.body||{result_id:id,intent:intent.value,format:format.value};format.value=body.format;intent.value=body.intent;format.disabled=intent.disabled=!!saved;
          exportForm=mountActionForm(exportHost,client,{action:'exports.create',path:'/exports',permission:'inversion.export',expected:{kind:'absent'},label:'导出完整结果',fixedBody:body,intent:body.intent,slot:'result-export:'+id},async value=>{
            const exportId=value.id||value.subject_id;
            if(!current())return;
            const retryDownload=element('button','下载同一导出记录','button');retryDownload.type='button';retryDownload.dataset.exportId=exportId;actions.append(retryDownload);
            async function download(){
              if(!current()||retryDownload.disabled)return;
              retryDownload.disabled=true;
              try{await client.downloadExport(exportId,`inversion-${id}.${body.format}`);if(current())downloadState.textContent='完整结果已交给浏览器下载。';}
              catch(error){if(current())downloadState.textContent=`导出记录 ${exportId} 已创建；下载未完成。${errorText(error)}`;}
              finally{if(current())retryDownload.disabled=client.denied;}
            }
            retryDownload.onclick=download;await download();
          });forms.push(exportForm);
        }
        format.onchange=intent.onchange=exportOptions;exportOptions();
      }
    }catch(error){if(current())root.replaceChildren(element('p',error.status?errorText(error):error.message,'inversion-error'));}
  }
  return {ready:load(),dispose(){disposed=true;disposeContent();client.dispose();}};
}

export function mountProductionResults(ctx,client=createInversionClient(ctx)) {
  const root=element('section',undefined,'production-results');ctx.workspace.append(root);
  let disposed=false,detailMount=null,offset=0,generation=0;
  const current=()=>!disposed&&(ctx.isCurrent?.()??true)&&ctx.workspace.contains(root);
  const list=element('section'),detail=element('section');root.append(list,detail);
  async function open(id){detailMount?.dispose();detail.replaceChildren();detailMount=mountResultDetail(ctx,detail,client,id);await detailMount.ready;}
  async function load(){
    const own=++generation;
    try{
      const page=await client.get(`/runs?offset=${offset}&limit=20`);if(!page||!current()||own!==generation)return;
      list.replaceChildren(element('h2','运行与结果'));
      if(!page.items.length)list.append(element('p','未计算。请到反演与双图验证选择冻结成员并预检。'));
      for(const run of page.items){const item=element('article',undefined,'inversion-run-item');item.append(element('p',`${runStateText(run)} · ${run.run_id} · ${run.reason_code||''}`));
        if(run.result_id){const button=element('button','查看结果','button');button.onclick=()=>open(run.result_id);item.append(button);}
        else{const button=element('button','查看运行 / 处理输入','button');button.onclick=()=>ctx.navigate?.('production-inversion',{run_id:run.run_id});item.append(button);}list.append(item);
      }
      const previous=element('button','上一页','button'),next=element('button','下一页','button');previous.disabled=!offset;next.disabled=page.items.length<20;
      previous.onclick=()=>{offset=Math.max(0,offset-20);load();};next.onclick=()=>{offset+=20;load();};list.append(previous,next);
    }catch(error){if(current())list.replaceChildren(element('p',errorText(error)));}
  }
  const ready=ctx.params?.result_id?open(ctx.params.result_id):load();
  return {ready,dispose(){disposed=true;generation++;detailMount?.dispose();client.dispose();}};
}
