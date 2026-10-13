import {escapeHtml as esc} from '../components.js';

export const CONNECTED_IDS=Object.freeze(['wells','well-detail','field-registry','channel-mapping','acquisition','data-quality','well-realtime','well-history']);
const RIGHTS={wells:['registry.read'],'well-detail':['registry.read'],'field-registry':['registry.read'],'channel-mapping':['registry.read'],
 acquisition:['acquisition.read'],'well-realtime':['registry.read','telemetry.read'],'well-history':['registry.read','telemetry.read']};
const UUID=/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;

export function renderState(context,state,message,{retry=false}={}) {
 if(!context.isCurrent())return;
 const labels={loading:'正在查询',empty:'暂无记录',forbidden:'没有访问权限',unconfigured:'服务未配置',unavailable:'服务暂不可用',
  stale:'保留的查询结果',conflict:'数据已变化',invalid:'请检查输入',missing:'记录不存在',pending:'待接入',connecting:'功能接入中'};
 context.workspace.innerHTML=context.pageHeader()+`<section class="wb-state" data-state="${esc(state)}" role="status"><span class="wb-state-icon">${state==='forbidden'?'!':'○'}</span><h2>${esc(labels[state]||'提示')}</h2><p>${esc(message)}</p>${retry?'<button class="button primary" data-retry>重新查询</button>':''}</section>`;
 context.workspace.querySelector('[data-retry]')?.addEventListener('click',()=>context.navigate(context.page.id,context.params));
}

export function renderError(context,error){
 const state=({401:'forbidden',403:'forbidden',404:'missing',409:'conflict',422:'invalid',503:'unavailable'})[error.status]||'unavailable';
 if(error.code==='cancelled')return;
 renderState(context,state,error.message||'连接暂不可用，请保留条件后重试。',{retry:!([401,403,422].includes(error.status))});
}

export async function renderWorkbenchRoute(context) {
 const {page,workspace,params,permissions,capabilities}=context;
 if(!CONNECTED_IDS.includes(page.id)){
  workspace.innerHTML=context.pageHeader()+`<section class="wb-state" data-state="pending"><span class="eyebrow">PAGE ROADMAP</span><h2>页面已规划 · 实际功能待接入</h2><p>此入口保留在 55 页产品目录中，当前工作台不混入演示业务数据。</p><a class="button primary" href="http://127.0.0.1:8772/#/${esc(page.id)}" target="_blank" rel="noopener">在独立窗口查看完整页面预览</a><p class="muted">原完整预览：8772 · 原演示版：8765 · 两者数据保持独立</p></section>`;return;
 }
 const required=RIGHTS[page.id]||[];
 if(required.some(right=>!permissions.has(right))||page.id==='data-quality'&&!(permissions.has('ingest.read')||(permissions.has('data_quality.read')&&permissions.has('telemetry.read')))){
  renderState(context,'forbidden','当前岗位未获得本页读取权限，不会查询或显示受限数据。');return;
 }
 const well=params.well_id||params.wellId;
 if(well&&!UUID.test(well)){renderState(context,'invalid','环境不匹配：这是其他版本的旧井链接，请在当前台账中选择生产井，或返回原完整预览。');
  workspace.querySelector('.wb-state').insertAdjacentHTML('beforeend','<a class="button secondary" href="http://127.0.0.1:8772/" target="_blank" rel="noopener">打开原完整预览</a>');return;}
 const capability=['wells','field-registry','channel-mapping'].includes(page.id)?'registry':'monitoring';
 const status=capabilities[capability]?.status;
 if(status!=='ready'){renderState(context,status==='disabled'||!status?'unconfigured':'unavailable','本页依赖的独立生产服务尚未就绪，不会回退到演示数据。',{retry:true});return;}
 renderState(context,'connecting','该页面属于本批 8 个接入入口，业务界面正在接通已验证的服务。');
}
