import {router} from '../router.js';
import {components,escapeHtml as esc} from '../components.js';
import {bindChartInteractions} from '../charts.js';
import {createPreviewContext} from './context.js';
import {renderers as installedRenderers} from './renderers.js';

export async function mountPreviewShell({root,catalog,api,meta,session={actor:{}},renderers=installedRenderers,onLogout,onActorChange,actions}) {
  const groups=[...new Set(catalog.map(p=>p.group))];
  let generation=0,context=null,disposed=false;const tabs=new Map();
  root.innerHTML=`<div class="app-shell preview-shell"><header class="topbar"><a class="brand" href="#/dashboard"><img src="/assets/heneng-logo.png" alt="合能科技"><span><strong>油井数字智能化平台</strong><small>OIL WELL DIGITAL INTELLIGENCE</small></span></a><span class="preview-tag">页面预览 · 测试数据</span><span class="preview-top-note">先审页面，后接入业务</span><div class="top-actions"><span>${esc(session.actor.display_name||'预览用户')}</span><button class="text-button" data-logout>退出</button></div></header>
    <div class="preview-toolbar"><div class="preview-dates">数据日期 <b>${esc(meta.data_from||'—')} — ${esc(meta.data_to||'—')}</b></div><div class="tabs" data-tabs></div><label>页面场景 <select data-scenario><option value="normal">正常数据</option><option value="empty">空数据</option><option value="error">读取错误</option><option value="denied">无权限</option><option value="stale">陈旧数据</option><option value="unconnected">未接入</option></select></label></div>
    <div class="main-layout"><aside class="sidebar preview-sidebar"><div class="sidebar-head"><span>井</span><div><strong>完整业务工作台</strong><small>55 PAGES · REVIEW FIRST</small></div></div><nav>${groups.map(group=>`<details open data-nav-group><summary>${esc(group)}</summary>${catalog.filter(p=>p.group===group&&!p.hidden).map(p=>`<button data-nav="${p.id}"><span>${esc(p.label[0])}</span><div><strong>${esc(p.label)}</strong></div></button>`).join('')}</details>`).join('')}<a class="preview-open-link" href="/assets/open-interface/index.html#hardware" target="_blank" rel="noopener">厂家开放接口与设备参考 ↗</a></nav></aside><main id="workspace" class="workspace" tabindex="-1"></main></div></div>`;
  const workspace=root.querySelector('#workspace');
  if(session.can_switch_actor){
    const select=document.createElement('select');select.setAttribute('aria-label','预览操作者');select.className='preview-role';
    for(const [value,label] of [['administrator','系统管理员'],['production_engineer','生产工程师'],['algorithm_engineer','算法工程师'],['maintenance_engineer','设备运维'],['reviewer','业务复核员']])select.add(new Option(label,value));
    select.value=session.actor.role_code;
    select.onchange=()=>{if(window.confirm('切换到另一位预览操作者；当前未保存表单将关闭。'))onActorChange?.(select.value);else select.value=session.actor.role_code;};
    root.querySelector('.top-actions').prepend(select);
  }
  async function render(page,params) {
    if(disposed)return;
    const thisGeneration=++generation;context?.dispose();
    root.querySelectorAll('.modal-overlay').forEach(x=>x.remove());
    tabs.set(page.id,{page,params});if(tabs.size>12)tabs.delete(tabs.keys().next().value);
    root.querySelector('[data-tabs]').innerHTML=[...tabs.values()].map(t=>`<div class="tab ${t.page.id===page.id?'active':''}"><button data-tab="${t.page.id}">${esc(t.page.label)}</button></div>`).join('');
    root.querySelectorAll('[data-nav]').forEach(x=>x.classList.toggle('active',x.dataset.nav===page.id));
    root.querySelector('[data-scenario]').value=params.scenario||'normal';
    const staging=document.createElement('section');staging.className='preview-page';
    context=createPreviewContext({page,params,session,workspace:staging,generation:()=>thisGeneration===generation&&!disposed,api,meta,navigate,actions});
    const local=context;
    staging.innerHTML=local.pageHeader(page)+components.skeleton(6);
    workspace.replaceChildren(staging);
    try {
      const renderer=renderers[page.id];
      const scenario=params.scenario||'normal';
      if(['empty','error','denied','unconnected'].includes(scenario)) {
        const messages={empty:'当前筛选范围暂无数据',error:'读取失败场景：请检查连接后重试',denied:'当前岗位无权查看此内容',unconnected:'页面已设计，真实业务接口尚未接入'};
        staging.innerHTML=local.pageHeader(page)+`<article class="panel preview-state" role="status"><h2>${messages[scenario]}</h2><p>这是页面状态检查，不改变原始业务数据。</p><button class="button" data-normal>返回正常场景</button></article>`;
        staging.querySelector('[data-normal]').onclick=()=>navigate(page.id,{...params,scenario:'normal'});
      } else if(renderer) await renderer(local);
      else staging.innerHTML=local.pageHeader(page)+`<article class="panel preview-state"><h2>页面正在制作</h2><p>此页尚未交付，不计为已完成页面。</p></article>`;
      if(!local.isCurrent())return;
      workspace.replaceChildren(staging);
      if(scenario==='stale')staging.insertAdjacentHTML('afterbegin','<div class="notice warning">陈旧数据场景：以下为历史快照，不代表设备当前状态。</div>');
      bindChartInteractions(staging);
      local.registerDispose(()=>bindChartInteractions(staging));
      document.title=`${page.label} · 油井平台页面预览`;
    } catch(error) {
      if(!local.isCurrent())return;
      workspace.innerHTML=local.pageHeader(page)+`<article class="panel preview-state"><h2>页面读取失败</h2><p role="alert">${esc(error.message)}</p><button class="button" data-retry>重试</button></article>`;
      workspace.querySelector('[data-retry]').onclick=()=>navigate(page.id,params);
    }
  }
  async function navigate(pageId,params={}) {
    const page=catalog.find(p=>p.id===pageId)||catalog.find(p=>p.id==='dashboard');
    return router.open(page.id,params);
  }
  for(const page of catalog)router.register(page.id,params=>render(page,params));
  const click=event=>{
    const nav=event.target.closest('[data-nav],[data-tab]');
    if(nav){event.preventDefault();const id=nav.dataset.nav||nav.dataset.tab;void navigate(id,tabs.get(id)?.params||{});return;}
    const target=event.target.closest('button[data-page]');
    if(target&&catalog.some(p=>p.id===target.dataset.page)){
      event.preventDefault();const params=target.dataset.id?{id:target.dataset.id}:{};for(const [key,value] of Object.entries(target.dataset)){if(key.startsWith('param'))params[key.slice(5).replace(/[A-Z]/g,(x,i)=>(i?'_':'')+x.toLowerCase())]=value;}
      void navigate(target.dataset.page,params);
    }
    if(event.target.closest('[data-logout]')){dispose();onLogout?.();}
  };
  root.addEventListener('click',click);
  root.querySelector('[data-scenario]').onchange=event=>{const current=router.current();void navigate(current.pageId,{...current.params,scenario:event.target.value});};
  function dispose(){disposed=true;++generation;context?.dispose();root.removeEventListener('click',click);}
  await router.restore();
  return {navigate,dispose};
}
