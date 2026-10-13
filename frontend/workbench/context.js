import {api} from '../api.js';
import {components,escapeHtml} from '../components.js';
import * as format from '../format.js';
import {registryTime} from '../registry/client.js';
import {createWorkbenchApi} from './api.js';

export function createWorkbenchContext({auth,page,params,root,navigate}) {
 const controller=new AbortController(),disposers=[];let active=true;
 const isCurrent=()=>active&&!controller.signal.aborted;
 const scoped={...components,toast:(...args)=>{if(isCurrent())components.toast(...args);},
  modal:options=>{if(!isCurrent())throw new Error('页面已关闭');const modal=components.modal(options);disposers.push(()=>modal.close());return modal;}};
 return {api:createWorkbenchApi(api,{signal:controller.signal,isCurrent}),components:scoped,escapeHtml,
  format:{...format,dateTime:registryTime},workspace:root,page,params:params||{},permissions:new Set(auth.permissions||[]),
  actorId:String(auth.id),authSession:auth,capabilities:auth.capabilities||{},signal:controller.signal,isCurrent,navigate,
  registerDispose(dispose){if(isCurrent())disposers.push(dispose);else dispose();},
  pageHeader:(selected=page,extra='')=>`<header class="page-header"><div><div class="breadcrumb">${escapeHtml(selected.group)} / ${escapeHtml(selected.label)}</div><h1>${escapeHtml(selected.label)}</h1><p class="muted">生产台账与采集事实 · 北京时间（UTC+08:00）</p></div>${extra}</header>`,
  dispose(){if(!active)return;active=false;controller.abort();for(const dispose of disposers.splice(0)){try{dispose();}catch{ /* Continue disposing the remaining owned resources. */ }}},
 };
}
