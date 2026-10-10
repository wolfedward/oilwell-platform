import {productionInversionGate} from './production-parameters.js';
import {mountProductionResults} from '../inversion/results.js';
export {mountProductionResults};

const mounted=new WeakMap();
export async function renderProductionResults(ctx) {
  mounted.get(ctx.workspace)?.dispose();
  ctx.workspace.innerHTML=ctx.pageHeader(ctx.page);
  const gate=productionInversionGate(ctx);
  if(!gate.allowed){
    const state=document.createElement('section');state.className='inversion-state';state.dataset.inversionState=gate.state;state.textContent=gate.message;ctx.workspace.append(state);return false;
  }
  const mount=mountProductionResults(ctx);mounted.set(ctx.workspace,mount);ctx.registerDispose?.(()=>mount.dispose());await mount.ready;
  return (ctx.isCurrent?.()??true)&&!!ctx.workspace.querySelector('.production-results');
}
