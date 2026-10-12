import {renderDynagraphList} from '../../pages/dynagraphs.js';
import {renderDynagraphAnalysis} from '../../pages/dynagraph-analysis.js';
import {renderDiagnosisReview} from '../../pages/diagnosis-review.js';
import {renderProductionParameters} from '../../pages/production-parameters.js';
import {renderProductionInversion} from '../../pages/production-inversion.js';
import {renderProductionResults} from '../../pages/production-results.js';
import {note,links} from '../ui.js';
import {cardMarkup} from '../pages/dynagraphs.js';
const renders={dynagraphs:renderDynagraphList,'dynagraph-analysis':renderDynagraphAnalysis,'diagnosis-review':renderDiagnosisReview,'production-parameters':renderProductionParameters,'production-inversion':renderProductionInversion,'production-results':renderProductionResults};
export async function renderDynagraphPreview(ctx){
 await renders[ctx.page.id](ctx);if(!ctx.isCurrent())return;
 ctx.workspace.querySelector('.page-header')?.insertAdjacentHTML('afterend',note('原始快照保持不变。参考形态和生产反演结果样例另列来源；修改参数后只保存输入，不运行算法、不形成新的计算结论。'));
 if(ctx.page.id==='dynagraphs'){
  const view=await ctx.api.get('/api/preview/views/dynagraphs');if(!ctx.isCurrent())return;
  const gallery=document.createElement('section');gallery.innerHTML='<div class="layer-heading"><span>图</span><div><p class="eyebrow">REFERENCE SHAPES</p><h2>补充典型工况图形</h2></div></div><div class="preview-card-grid">'+view.profiles.slice(0,6).map(cardMarkup).join('')+'</div>'+links([['standard-cards','查看全部典型工况'],['dynagraph-tracking','跨时点功图对比'],['production-metering','产液计量分析']]);
  ctx.workspace.append(gallery);
 }
 if(ctx.page.id==='production-inversion'){
  const runs=await ctx.api.get('/api/production-inversion/runs?limit=20&offset=0');if(!ctx.isCurrent())return;
  const bar=document.createElement('nav');bar.className='preview-subnav';
  for(const run of runs.items){const button=document.createElement('button');button.className='button secondary';button.dataset.previewRun=run.run_id;button.textContent=run.state==='completed'?'查看固定输入的双图样例':'查看拒算原因样例';button.onclick=()=>ctx.navigate('production-inversion',{run_id:run.run_id});bar.append(button);}ctx.workspace.querySelector('.page-header')?.after(bar);
 }
 const intercept=async event=>{
  const button=event.target.closest('[data-analyze-id],#analysisRun,[data-inversion-preflight]');if(!button)return;
  event.preventDefault();event.stopImmediatePropagation();
  const values={...ctx.params,selection: [...ctx.workspace.querySelectorAll('select')].map(el=>({name:el.name,value:el.value})),note:'新输入尚未计算，真实算法待页面确认后接入'};
  const saved=await ctx.actions.confirmRehearsal({action:'calculate_input',objectId:ctx.page.id+'/input',values});
  if(saved&&ctx.isCurrent()){let notice=ctx.workspace.querySelector('[data-input-saved]');if(!notice){notice=document.createElement('p');notice.dataset.inputSaved='';button.after(notice);}notice.textContent='输入已保存（演练），尚未计算。可查看固定输入的历史/软件样例。';}
 };
 ctx.workspace.addEventListener('click',intercept,true);ctx.registerDispose(()=>ctx.workspace.removeEventListener('click',intercept,true));
}
