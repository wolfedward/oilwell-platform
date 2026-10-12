import {components,escapeHtml} from '../components.js';
import * as format from '../format.js';

export function createPreviewContext({page,params,session,workspace,generation,api,meta,navigate,actions}) {
  const disposers=[];const controller=new AbortController();let active=true;
  const versions=api.supportsPreviewVersionSnapshots?api.get('/api/preview/rehearsal-versions',{signal:controller.signal}).then(value=>({value}),error=>({error})):Promise.resolve({value:undefined});
  const confirm=async request=>{const snapshot=await versions;if(snapshot.error)throw snapshot.error;const result=await actions.confirmRehearsal({...request,expectedVersions:snapshot.value,signal:controller.signal});if(result?.object_id&&snapshot.value)snapshot.value[result.object_id]=result.version;return result;};
  const rawApi=api.withSignal?api.withSignal(controller.signal,actions?{confirmRehearsal:confirm}:{}):api;let lastRehearsal=null;
  const remember=value=>{if(value?.mode==='rehearsal')lastRehearsal=value;return value;};
  const pageApi={...rawApi};for(const key of ['post','patch','raw'])if(rawApi[key])pageApi[key]=async(...args)=>remember(await rawApi[key](...args));
  if(rawApi.request)pageApi.request=async(...args)=>remember(await rawApi.request(...args));
  const scopedComponents={...components,toast:(message,tone)=>{if(!active||!generation())return;if(lastRehearsal&&tone!=='error'){components.toast('演练已保存 · 历史记录未改变 · 未发送设备');lastRehearsal=null;}else components.toast(message,tone);},modal:options=>{const modal=components.modal(options);disposers.push(()=>modal.close());const password=options.content?.querySelector?.('input[type="password"]');if(password){password.disabled=true;password.required=false;password.placeholder='预览不创建真实登录凭据';}return modal;}};
  return {
    api:pageApi,components:scopedComponents,escapeHtml,format,workspace,page,params,meta,actions:actions?{...actions,confirmRehearsal:confirm}:actions,
    scenario:params.scenario||'normal',actorId:session.actor.id,authSession:session,
    permissions:new Set(session.actor.permissions||[]),
    capabilities:session.capabilities||{},navigate,
    isCurrent:()=>active&&generation(),
    registerDispose(dispose){if(active) disposers.push(dispose);else dispose();},
    pageHeader:(selected,extra='')=>`<header class="page-header"><div><div class="breadcrumb">${escapeHtml(selected.group)} / ${escapeHtml(selected.label)}</div><h1>${escapeHtml(selected.label)}</h1><p class="muted">${escapeHtml(selected.description||'业务页面内容审查 · 历史快照与软件测试场景')}</p></div>${extra}</header>`,
    dispose(){active=false;controller.abort();for(const dispose of disposers.splice(0))dispose();},
  };
}
