import {escapeHtml as esc} from '../components.js';

const number = value => Number.isFinite(value) ? Number(value).toFixed(2) : '—';
function bounded(points) {
  if(points.length<=500)return points;
  const stride=Math.ceil((points.length-2)/498),sample=[points[0]];
  for(let start=1;start<points.length-1;start+=stride){
    let peak=points[start];
    for(let index=start+1;index<Math.min(start+stride,points.length-1);index++)if(points[index].y>peak.y)peak=points[index];
    sample.push(peak);
  }
  sample.push(points.at(-1));return sample;
}
export function chart(title, series, xUnit, yUnit) {
  const original = series.map(line => ({...line, points: (line.points || []).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y))})).filter(line => line.points.length);
  if (!original.length) return `<p class="study-empty">${esc(title)}：没有可绘制的有限观测值。</p>`;
  let minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
  for(const line of original)for(const point of line.points){minX=Math.min(minX,point.x);maxX=Math.max(maxX,point.x);minY=Math.min(minY,point.y);maxY=Math.max(maxY,point.y);}
  const traces=original.map(line=>({...line,points:bounded(line.points)}));
  const xx = v => 48 + (v-minX)/(maxX-minX || 1)*690, yy = v => 250 - (v-minY)/(maxY-minY || 1)*210;
  const colors = ['#0e7490','#d97706','#7c3aed','#059669'];
  const paths = traces.map((line,index) => {
    return `<path d="${line.points.map((p,i)=>`${i?'L':'M'}${xx(p.x).toFixed(1)} ${yy(p.y).toFixed(1)}`).join(' ')}" fill="none" stroke="${colors[index%colors.length]}" stroke-width="2"/>`;
  }).join('');
  const controls = original.map((line,index) => `<label>${esc(line.name)} <input type="range" min="0" max="${line.points.length-1}" value="0" data-study-chart-point="${index}" aria-label="${esc(line.name)}数据点"/><span data-study-series-name="${esc(line.name)}">${esc(line.name)}：${number(line.points[0].x)} ${esc(xUnit)} · ${number(line.points[0].y)} ${esc(yUnit)}</span></label>`).join('');
  const encoded = esc(JSON.stringify(traces.map(line=>line.points)));
  const raw = esc(JSON.stringify(original.map(line=>line.points.map(point=>[point.x,point.y]))));
  return `<figure class="study-chart" data-study-chart data-x-unit="${esc(xUnit)}" data-y-unit="${esc(yUnit)}" data-min-x="${minX}" data-max-x="${maxX}" data-min-y="${minY}" data-max-y="${maxY}" data-points="${encoded}" data-raw-points="${raw}"><figcaption>${esc(title)}</figcaption><svg viewBox="0 0 760 290" role="img" aria-label="${esc(title)}">
    <path d="M48 40V250H738" fill="none" stroke="#9ca3af"/>${paths}<circle data-study-chart-marker r="5" fill="#b45309" stroke="white" stroke-width="2" visibility="hidden"/>
    <text x="48" y="272">${number(minX)} ${esc(xUnit)}</text><text x="660" y="272">${number(maxX)} ${esc(xUnit)}</text><text x="2" y="44">${number(maxY)}</text><text x="2" y="251">${number(minY)}</text></svg>
    <output data-study-chart-tooltip hidden></output><div class="study-chart-controls">${controls}</div><p class="study-chart-note">线条最多显示 500 个代表点；滑块与悬停读取全部原始有限数据点。</p></figure>`;
}

export function bindStudyCharts(root) {
  const cache=new WeakMap();
  function raw(figure){
    if(!cache.has(figure))cache.set(figure,JSON.parse(figure.dataset.rawPoints).map(values=>({values,sorted:values.every((point,index)=>!index||point[0]>=values[index-1][0])})));
    return cache.get(figure);
  }
  function reveal(figure,lineIndex,point){
    const minX=Number(figure.dataset.minX),maxX=Number(figure.dataset.maxX),minY=Number(figure.dataset.minY),maxY=Number(figure.dataset.maxY);
    const marker=figure.querySelector('[data-study-chart-marker]');
    marker.setAttribute('cx',String(48+(point[0]-minX)/(maxX-minX||1)*690));
    marker.setAttribute('cy',String(250-(point[1]-minY)/(maxY-minY||1)*210));
    marker.setAttribute('visibility','visible');
    const name=figure.querySelector(`[data-study-chart-point="${lineIndex}"]`)?.closest('label')?.querySelector('span')?.dataset.studySeriesName||'';
    const tooltip=figure.querySelector('[data-study-chart-tooltip]');
    tooltip.textContent=`${name}：${number(point[0])} ${figure.dataset.xUnit} · ${number(point[1])} ${figure.dataset.yUnit}`;tooltip.hidden=false;
  }
  const update = event => {
    const input = event.target.closest('[data-study-chart-point]'); if (!input) return;
    const figure = input.closest('[data-study-chart]');
    const points = raw(figure)[Number(input.dataset.studyChartPoint)].values;
    const point = points[Number(input.value)];
    const span=input.closest('label').querySelector('span');
    span.textContent = `${span.dataset.studySeriesName}：${number(point[0])} ${figure.dataset.xUnit} · ${number(point[1])} ${figure.dataset.yUnit}`;
    reveal(figure,Number(input.dataset.studyChartPoint),point);
  };
  const pointer=event=>{
    const svg=event.target.closest('svg');if(!svg)return;
    const figure=svg.closest('[data-study-chart]');if(!figure)return;
    const bounds=svg.getBoundingClientRect();
    const minX=Number(figure.dataset.minX),maxX=Number(figure.dataset.maxX);
    const minY=Number(figure.dataset.minY),maxY=Number(figure.dataset.maxY);
    const cursorX=(event.clientX-bounds.left)/bounds.width*760;
    const cursorY=(event.clientY-bounds.top)/bounds.height*290;
    const cursor=minX+(cursorX-48)/690*(maxX-minX);
    let nearest=null;
    const xScreen=x=>48+(x-minX)/(maxX-minX||1)*690;
    const yScreen=y=>250-(y-minY)/(maxY-minY||1)*210;
    raw(figure).forEach((line,lineIndex)=>{
      const points=line.values;
      const consider=index=>{if(index<0||index>=points.length)return;const point=points[index];const distance=(xScreen(point[0])-cursorX)**2+(yScreen(point[1])-cursorY)**2;if(!nearest||distance<nearest.distance)nearest={lineIndex,index,point,distance};};
      if(line.sorted){
        let low=0,high=points.length;while(low<high){const mid=(low+high)>>1;if(points[mid][0]<cursor)low=mid+1;else high=mid;}
        consider(low);consider(low-1);
        for(let i=low-2;i>=0&&Math.abs(xScreen(points[i][0])-cursorX)<=12;i--)consider(i);
        for(let i=low+1;i<points.length&&Math.abs(xScreen(points[i][0])-cursorX)<=12;i++)consider(i);
      }else for(let i=0;i<points.length;i++)consider(i);
    });
    if(nearest){reveal(figure,nearest.lineIndex,nearest.point);const slider=figure.querySelector(`[data-study-chart-point="${nearest.lineIndex}"]`);if(slider)slider.value=String(nearest.index);}
  };
  const leave=event=>{if(!event.target.matches?.('svg'))return;const figure=event.target.closest('[data-study-chart]');if(!figure)return;figure.querySelector('[data-study-chart-tooltip]').hidden=true;figure.querySelector('[data-study-chart-marker]').setAttribute('visibility','hidden');};
  root.addEventListener('input',update);root.addEventListener('change',update);
  root.addEventListener('pointermove',pointer);root.addEventListener('pointerleave',leave,true);
  return () => {root.removeEventListener('input',update);root.removeEventListener('change',update);root.removeEventListener('pointermove',pointer);root.removeEventListener('pointerleave',leave,true);};
}
