import {components,escapeHtml} from '../components.js';
import * as format from '../format.js';

export function createPreviewContext({page,params,session,workspace,generation,api,meta,navigate,actions}) {
  const disposers=[];const controller=new AbortController();let active=true;
  const pageApi=api.withSignal?api.withSignal(controller.signal):api;
  return {
    api:pageApi,components,escapeHtml,format,workspace,page,params,meta,actions,
    scenario:params.scenario||'normal',actorId:session.actor.id,authSession:session,
    permissions:new Set(session.actor.permissions||[]),
    capabilities:session.capabilities||{},navigate,
    isCurrent:()=>active&&generation(),
    registerDispose(dispose){if(active) disposers.push(dispose);else dispose();},
    pageHeader:(selected,extra='')=>`<header class="page-header"><div><div class="breadcrumb">${escapeHtml(selected.group)} / ${escapeHtml(selected.label)}</div><h1>${escapeHtml(selected.label)}</h1><p class="muted">${escapeHtml(selected.description||'业务页面内容审查 · 历史快照与软件测试场景')}</p></div>${extra}</header>`,
    dispose(){active=false;controller.abort();for(const dispose of disposers.splice(0))dispose();},
  };
}
