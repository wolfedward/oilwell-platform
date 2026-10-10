import {element} from './forms.js';

const SVG = 'http://www.w3.org/2000/svg';
function svgNode(tag, attributes = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key,value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

// Exact integer/rational times are subtracted BEFORE display-only conversion.
export function fraction(value) {
  const parts = String(value).split('/');
  if (!/^-?\d+$/.test(parts[0]) || parts.length > 2 || parts[1] && !/^\d+$/.test(parts[1])) throw Error('无效精确时间');
  const denominator = BigInt(parts[1] || '1');
  if (!denominator) throw Error('无效精确时间');
  return [BigInt(parts[0]), denominator];
}

export function numeric(value) {
  if (value === null || value === undefined || value === '') return null;
  try {
    const result = String(value).includes('/') ? (()=>{const [n,d]=fraction(value);return Number(n)/Number(d)})() : Number(value);
    return Number.isFinite(result) ? result : null;
  } catch {return null;}
}

export function twoDecimals(value) {
  const result = numeric(value);
  return result === null ? '未知' : result.toFixed(2);
}

function kilonewtons(value) {
  const newtons=numeric(value);
  return newtons===null?null:newtons/1000;
}

export function cycleRate(cycle) {
  try {
    const [an,ad]=fraction(cycle.start_ns),[bn,bd]=fraction(cycle.end_ns);
    const duration=bn*ad-an*bd;
    return duration > 0n ? numeric(`${60000000000n*ad*bd}/${duration}`) : null;
  } catch {return null;}
}

function relativeSeconds(value, origin) {
  try {
    const [n,d]=fraction(value),[on,od]=origin;
    return Number(n*od-on*d)/Number(d*od*1000000000n);
  } catch {return null;}
}

function contiguous(previous, point) {
  if (!previous) return false;
  try {return BigInt(point.power_index) === BigInt(previous.power_index)+1n;} catch {return false;}
}

function mountChart(root, {kind,title,xLabel,yLabel,traces,source}) {
  const chart=element('section',undefined,'inversion-chart');chart.dataset.chartKind=kind;
  chart.append(element('h3',title));
  const svg=svgNode('svg',{viewBox:'0 0 720 300',role:'img','aria-label':`${title}，${xLabel}，${yLabel}`});
  const left=70,right=695,top=20,bottom=252;
  const all=traces.flatMap(trace=>trace.points.filter(p=>p.x!==null&&p.y!==null&&p.valid));
  const extent=key=>{
    let low=Infinity,high=-Infinity;
    for(const point of all){low=Math.min(low,point[key]);high=Math.max(high,point[key]);}
    if(!Number.isFinite(low))return [0,1];
    if(low===high)return [low-1,high+1];
    const margin=(high-low)*0.06;return [low-margin,high+margin];
  };
  const [xmin,xmax]=extent('x'),[ymin,ymax]=extent('y');
  const x=value=>left+(value-xmin)/(xmax-xmin)*(right-left);
  const y=value=>bottom-(value-ymin)/(ymax-ymin)*(bottom-top);
  for(let i=0;i<=4;i++){
    const yy=top+(bottom-top)*i/4;
    svg.append(svgNode('line',{x1:left,y1:yy,x2:right,y2:yy,class:'inversion-grid'}));
    const label=svgNode('text',{x:left-8,y:yy+4,'text-anchor':'end'});label.textContent=twoDecimals(ymax-(ymax-ymin)*i/4);svg.append(label);
  }
  const xl=svgNode('text',{x:(left+right)/2,y:291,'text-anchor':'middle'});xl.textContent=xLabel;
  const yl=svgNode('text',{x:17,y:140,transform:'rotate(-90 17 140)','text-anchor':'middle'});yl.textContent=yLabel;
  svg.append(xl,yl);
  for(const [xx,value] of [[left,xmin],[right,xmax]]){
    const label=svgNode('text',{x:xx,y:272,'text-anchor':xx===left?'start':'end'});label.textContent=twoDecimals(value);svg.append(label);
  }
  const groups=new Map();
  for(const trace of traces){
    const group=svgNode('g',{'data-series':trace.id});group.hidden=trace.hidden===true;group.style.display=group.hidden?'none':'';
    let segment=[],previous=null;
    const flush=()=>{
      if(segment.length){
        const path=svgNode('path',{d:segment.map((point,i)=>`${i?'L':'M'}${x(point.x).toFixed(3)},${y(point.y).toFixed(3)}`).join(' '),fill:'none',stroke:trace.color,'stroke-width':'1.25','vector-effect':'non-scaling-stroke'});
        if(trace.dashed)path.setAttribute('stroke-dasharray','5 4');group.append(path);
      }
      segment=[];
    };
    for(const point of trace.points){
      if(!point.valid||point.x===null||point.y===null){flush();previous=null;continue;}
      if(previous&&!contiguous(previous,point)){flush();}
      segment.push(point);previous=point;
    }
    flush();groups.set(trace.id,group);svg.append(group);
  }
  const marker=svgNode('circle',{r:'2.5',fill:'#163a62',visibility:'hidden'});
  const hit=svgNode('rect',{x:left,y:top,width:right-left,height:bottom-top,fill:'transparent',tabindex:'0','data-chart-hit':'','aria-label':`${title}：左右方向键查看原始点，Home / End 到首末点`});
  svg.append(marker,hit);chart.append(svg);
  const tooltip=element('p','悬停或使用方向键查看原始索引、坐标与状态。','inversion-tooltip');tooltip.setAttribute('role','status');chart.append(tooltip);root.append(chart);
  let selected=0,disposed=false;
  const available=()=>traces.filter(trace=>!groups.get(trace.id).hidden).flatMap(trace=>trace.points.map(point=>({...point,trace:trace.label})));
  function select(points,index){
    if(disposed||!points.length)return;
    selected=Math.max(0,Math.min(points.length-1,index));const point=points[selected];
    const detail=point.raw;
    tooltip.textContent=`${point.trace} · ${xLabel} ${twoDecimals(point.x)} · ${yLabel} ${twoDecimals(point.y)} · 原始功率索引 ${point.power_index} · 时间 ${detail.time_ns} ns · 状态 ${detail.status ?? (detail.valid?'valid':'invalid')} ${(detail.reason_codes||detail.original_reason_codes||[]).join(', ')} · 来源 ${source || '未提供'}${detail.load_indices?' · 参考载荷索引 '+detail.load_indices.join(', '):''}${detail.displacement_indices?' · 参考位移索引 '+detail.displacement_indices.join(', '):''}${detail.periodic_assumption?' · periodic_assumption=true':''}`;
    marker.setAttribute('visibility',point.valid&&point.x!==null&&point.y!==null?'visible':'hidden');
    if(point.x!==null&&point.y!==null){marker.setAttribute('cx',x(point.x));marker.setAttribute('cy',y(point.y));}
  }
  function pointer(event){
    const points=available(),bounds=svg.getBoundingClientRect();
    const px=(event.clientX-bounds.left)/bounds.width*720,py=(event.clientY-bounds.top)/bounds.height*300;
    let best=Infinity,index=0;
    points.forEach((point,i)=>{if(point.x!==null&&point.y!==null){const distance=(x(point.x)-px)**2+(y(point.y)-py)**2;if(distance<best){best=distance;index=i;}}});
    select(points,index);
  }
  function keyboard(event){
    if(!['Home','End','ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;
    event.preventDefault();const points=available();
    select(points,event.key==='Home'?0:event.key==='End'?points.length-1:selected+(['ArrowLeft','ArrowDown'].includes(event.key)?-1:1));
  }
  hit.addEventListener('pointermove',pointer);hit.addEventListener('keydown',keyboard);
  const hide=()=>marker.setAttribute('visibility','hidden');hit.addEventListener('pointerleave',hide);hit.addEventListener('blur',hide);
  return {setVisibility(id,visible){const group=groups.get(id);if(group){group.hidden=!visible;group.style.display=visible?'':'none';hide();}},dispose(){disposed=true;hit.removeEventListener('pointermove',pointer);hit.removeEventListener('keydown',keyboard);hit.removeEventListener('pointerleave',hide);hit.removeEventListener('blur',hide);}};
}

export function mountComparison(elementRoot,{prediction,reference,filled,metadata={}}) {
  const root=element('div',undefined,'inversion-chart-grid');elementRoot.append(root);
  const controls=element('div',undefined,'inversion-trace-controls');root.append(controls);
  const charts=[];
  const points=(rows,x,y,valid)=>rows.map(raw=>({raw,power_index:raw.power_index,x:x(raw),y:y(raw),valid:valid(raw)}));
  const pred=points(prediction,p=>numeric(p.s_m),p=>kilonewtons(p.load_N),p=>p.status==='valid');
  const ref=points(reference,p=>numeric(p.displacement_value_si),p=>kilonewtons(p.load_value_si),p=>p.valid===true);
  const fillMap=new Map(filled.map(p=>[p.power_index,p]));
  const fillPoints=points(prediction.map(p=>fillMap.get(p.power_index)||p),p=>numeric(p.s_m),p=>kilonewtons(p.load_N),p=>fillMap.has(p.power_index)||p.status==='valid');
  // Only segments adjacent to an actual fill are display-only. Original trace
  // support remains separate, including all quality gaps and invalid samples.
  const display=fillPoints.map((p,i)=>({...p,valid:p.valid&&(fillMap.has(p.power_index)||fillMap.has(fillPoints[i-1]?.power_index)||fillMap.has(fillPoints[i+1]?.power_index))}));
  charts.push(mountChart(root,{kind:'comparison',title:'双图对比 · 原始有效支撑',xLabel:'位移（m）',yLabel:'载荷（kN）',source:metadata.source,traces:[
    {id:'prediction',label:'反演预测',points:pred,color:'#296aa3'},
    {id:'reference',label:'已对齐参考',points:ref,color:'#ba6931'},
    {id:'filled',label:'仅展示补绘',points:display,color:'#697e91',dashed:true,hidden:true}]}));
  for(const [id,label] of [['prediction','反演预测'],['reference','已对齐参考'],['filled','展示补绘（虚线，不进入主指标）']]){
    const labelNode=element('label',label),input=element('input');input.type='checkbox';input.checked=id!=='filled';input.dataset.traceToggle=id;
    input.addEventListener('change',()=>charts[0].setVisibility(id,input.checked));labelNode.prepend(input);controls.append(labelNode);
  }
  if(filled.some(point=>point.periodic_assumption))controls.append(element('span','补绘包含 periodic_assumption 周期假设；不进入主指标。'));
  let origin=[0n,1n];try{if(prediction.length)origin=fraction(prediction[0].time_ns);}catch{/* Invalid coordinates remain unknown. */}
  const time=p=>relativeSeconds(p.time_ns,origin);
  for(const [kind,title,key,label] of [
    ['power','电机端总有功 · 原生中心时刻','power_W','功率（W）'],
    ['motion','运动 · 曲柄角度','theta_rad','角度（rad）'],
    ['torque','中间转矩 · 驱动转矩','drive_Nm','转矩（N·m）']]){
    charts.push(mountChart(root,{kind,title,xLabel:'距首个功率中心时间（s）',yLabel:label,source:metadata.source,
      traces:[{id:kind,label:title,color:'#296aa3',points:points(prediction,time,p=>numeric(p[key]),p=>p.status==='valid')}]}));
  }
  return {setVisibility(id,visible){charts[0].setVisibility(id,visible);const input=controls.querySelector(`[data-trace-toggle="${id}"]`);if(input)input.checked=visible;},dispose(){charts.forEach(chart=>chart.dispose());}};
}
