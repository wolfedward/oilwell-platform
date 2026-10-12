import {renderProductionStudies} from '../../pages/production-studies.js';
import {readPayload} from '../../studies/forms.js';
import {note} from '../ui.js';

export async function renderStudiesPreview(ctx){
 await renderProductionStudies(ctx);if(!ctx.isCurrent())return;
 ctx.workspace.insertAdjacentHTML('afterbegin',note('固定软件输入与结果对供页面审查。编辑输入不会重新运行算法；保存仅形成独立演练记录，原结果仍属于原输入。'));
 const intercept=async event=>{
  const button=event.target.closest('[data-study-create],[data-study-review],[data-study-select],[data-study-unselect]');if(!button)return;
  event.preventDefault();event.stopImmediatePropagation();
  const root=ctx.workspace,kind=root.querySelector('[data-study-kind]')?.value,selected=root.querySelector('.study-list-row.active')?.dataset.studyRecord;
  try{
   let action='calculate_input',payload;
   if(button.matches('[data-study-create]'))payload={kind,title:root.querySelector('[data-study-title]')?.value,payload:readPayload(root,kind)};
   else {action=button.matches('[data-study-review]')?'review':'edit';payload={record_id:selected,decision:root.querySelector('[data-study-decision]')?.value,comment:root.querySelector('[data-study-comment]')?.value,selected:!button.matches('[data-study-unselect]')};}
   const saved=await ctx.actions.confirmRehearsal({objectId:selected||'preview-study-input',action,values:payload});
   if(saved&&ctx.isCurrent()){const alert=root.querySelector('[data-study-alert]');alert.className='study-alert';alert.textContent=action==='calculate_input'?'已保存输入 · 尚未计算。下方历史结果未变，不代表本次输入的计算结果。':'已保存独立演练记录；历史样例与影子版本未改变。';}
  }catch(e){root.querySelector('[data-study-alert]').textContent=e.message;}
 };
 ctx.workspace.addEventListener('click',intercept,true);ctx.registerDispose(()=>ctx.workspace.removeEventListener('click',intercept,true));
}
