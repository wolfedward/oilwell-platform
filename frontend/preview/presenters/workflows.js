import {renderApprovals} from '../../pages/approvals.js';
import {renderCommands} from '../../pages/commands.js';
import {renderDeviceCommand} from '../../pages/device-command.js';
import {renderAlarms} from '../../pages/alarms.js';
import {renderWorkOrders} from '../../pages/work-orders.js';
import {renderReports} from '../../pages/reports.js';
import {note} from '../ui.js';
const renders={approvals:renderApprovals,commands:renderCommands,'device-command':renderDeviceCommand,alarms:renderAlarms,'work-orders':renderWorkOrders,reports:renderReports};
export async function renderWorkflowPreview(ctx){
 let scoped=ctx;
 if(ctx.page.id==='reports'){
  const withScope=path=>{const u=new URL(path,'http://preview');for(const key of ['unit_id','well_id','from','to'])if(ctx.params[key])u.searchParams.set(key,ctx.params[key]);return u.pathname+u.search;};
  scoped={...ctx,api:{...ctx.api,get:path=>ctx.api.get(withScope(path)),download:(path,...args)=>ctx.api.download(withScope(path),...args)},navigate:(id,params)=>ctx.navigate(id,{...ctx.params,...params})};
 }
 await renders[ctx.page.id](scoped);if(!ctx.isCurrent())return;
 ctx.workspace.querySelector('.page-header')?.insertAdjacentHTML('afterend',note('以下为历史快照台账。编辑、确认、审批及处置仅保存独立演练记录；不会修改原记录、创建真实账号或重发任何历史指令。'));
 if(['commands','device-command'].includes(ctx.page.id)){
  ctx.workspace.querySelectorAll('.check-list').forEach(el=>el.outerHTML=note('签名与幂等凭据已脱敏；本预览不执行验签，不确认历史回执的现场真实性。'));
  ctx.workspace.querySelectorAll('dd.mono').forEach(el=>el.textContent='已脱敏，不提供重放凭据');
  ctx.workspace.querySelectorAll('.inline-note').forEach(el=>{if(el.textContent.includes('新下发指令'))el.textContent='仅保留历史业务摘要；本预览没有下发通道。';});
  const select=ctx.workspace.querySelector('#commandState');for(const option of [...(select?.options||[])])if(['received','validated','executing'].includes(option.value))option.remove();
 }
 if(ctx.page.id==='reports'){
  const form=ctx.workspace.querySelector('#reportFilters');for(const [key,label] of [['unit_id','单位编号'],['well_id','油井编号']]){const node=document.createElement('label');node.textContent=label;const input=document.createElement('input');input.name=key;input.value=ctx.params[key]||'';node.append(input);form.prepend(node);}
 }
}
