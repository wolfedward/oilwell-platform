import {renderEnergyOverview} from '../../pages/energy-overview.js';
import {renderRecoveryAnalysis} from '../../pages/recovery-analysis.js';
import {renderStrategyCenter} from '../../pages/strategy-center.js';
import {renderStrategyEditor} from '../../pages/strategy-editor.js';
import {note,links} from '../ui.js';
const renders={'energy-overview':renderEnergyOverview,'recovery-analysis':renderRecoveryAnalysis,'strategy-center':renderStrategyCenter,'strategy-editor':renderStrategyEditor};
export async function renderEnergyPreview(ctx){
 let shownId=ctx.params.id;
 const api={...ctx.api,get:async path=>{const result=await ctx.api.get(path);if(/^\/api\/strategies\/[^/?]+$/.test(path))shownId=result.id;return result;}};
 await renders[ctx.page.id]({...ctx,api});if(!ctx.isCurrent())return;
 ctx.workspace.querySelector('.page-header')?.insertAdjacentHTML('afterend',note('历史快照报表与 preview- 开头的软件制度样例分开。新输入不执行优化；所有审批、下发和回退仅为独立流程演练。')+links([['multiwell-schedule','多井调度'],['energy-effects','效果对照样例']]));
 const replace=(selector,oldText,newText)=>ctx.workspace.querySelectorAll(selector).forEach(el=>{if(el.textContent===oldText)el.textContent=newText;});
 replace('span','服务端实时计算','历史快照范围核算');replace('button','重新核算','查询快照');replace('button','优化计算并保存草稿','保存优化输入（未计算）');
 const submit=async event=>{if(!event.target.matches('#strategyOptimizeForm,#strategyDraftForm'))return;event.preventDefault();event.stopImmediatePropagation();const form=event.target;if(!form.reportValidity())return;
  const saved=await ctx.actions.confirmRehearsal({action:'calculate_input',objectId:ctx.params.id||'preview-strategy-input',values:Object.fromEntries(new FormData(form))});
  if(saved&&ctx.isCurrent()){let line=ctx.workspace.querySelector('[data-energy-saved]');if(!line){line=document.createElement('p');line.dataset.energySaved='';form.after(line);}line.textContent='输入已保存，尚未计算；原制度预测值未变化。';}};
 const click=async event=>{const b=event.target.closest('#submitStrategy,#submitCenterStrategy,#copyStrategy,#dispatchStrategy,#rollbackStrategy');if(!b)return;event.preventDefault();event.stopImmediatePropagation();
  const action=['dispatchStrategy','rollbackStrategy'].includes(b.id)?'control_plan':b.id==='copyStrategy'?'draft':'submit';
  if(!shownId)return;
  const saved=await ctx.actions.confirmRehearsal({action,objectId:'/api/strategies/'+shownId,values:{intent:b.textContent}});if(saved&&ctx.isCurrent()){ctx.components.toast('已保存流程演练记录；未改变历史策略、未发送设备。');await ctx.navigate(ctx.page.id,{...ctx.params,id:shownId});}};
 ctx.workspace.addEventListener('submit',submit,true);ctx.workspace.addEventListener('click',click,true);ctx.registerDispose(()=>{ctx.workspace.removeEventListener('submit',submit,true);ctx.workspace.removeEventListener('click',click,true);});
}
