import {createInversionClient} from '../inversion/client.js';
import {mountProductionParameters} from '../inversion/parameters.js';

const mounted=new WeakMap();
export function productionInversionGate({permissions,capabilities}) {
  if(!permissions?.has('inversion.read'))return {allowed:false,state:'forbidden',message:'没有生产反演参数查看权限。'};
  const capability=capabilities?.production_inversion;
  if(capability?.enabled!==true)return {allowed:false,state:'disabled',message:'生产反演模块未配置；请联系管理员。'};
  if(capability.status!=='ready')return {allowed:false,state:'unavailable',message:'生产反演模块暂时不可用，请检查独立配置与数据库。'};
  return {allowed:true,state:'ready',message:''};
}
export const productionParametersGate = productionInversionGate;

export async function renderProductionParameters(ctx) {
  mounted.get(ctx.workspace)?.dispose();ctx.workspace.innerHTML=ctx.pageHeader(ctx.page);
  const gate=productionInversionGate(ctx);
  if(!gate.allowed){const state=document.createElement('section');state.className='production-parameters inversion-state';state.dataset.inversionState=gate.state;state.textContent=gate.message;ctx.workspace.append(state);return false;}
  const client=createInversionClient(ctx);
  const mount=mountProductionParameters(ctx,client);mounted.set(ctx.workspace,mount);
  ctx.registerDispose?.(()=>mount.dispose());
  await mount.ready;
  return !!(ctx.isCurrent?.()??true)&&ctx.workspace.contains(ctx.workspace.querySelector('.production-parameters'));
}
