import {api} from '../api.js';
import {escapeHtml as esc,components} from '../components.js';
import {loadCatalog} from '../preview/catalog.js';
import {createWorkbenchApi} from './api.js';
import {createWorkbenchContext} from './context.js';
import {CONNECTED_IDS,renderWorkbenchRoute,renderError} from './routes.js';

export function createWorkbenchApp({root,catalog}) {
 let active=true,generation=0,current=null,auth=null,interval=null,loginController=null;
 const globalController=new AbortController();
 const client=createWorkbenchApi(api,{signal:globalController.signal,isCurrent:()=>active});
 const encode=(id,params)=>`#/${encodeURIComponent(id)}${Object.keys(params).length?'?'+new URLSearchParams(params):''}`;
 function locationRoute(){try{const [id,q='']=location.hash.replace(/^#\/?/,'').split('?');return {id:decodeURIComponent(id||'wells'),params:Object.fromEntries(new URLSearchParams(q))};}catch{return {id:'wells',params:{}};}}
 function shell(){
  const groups=[...new Set(catalog.map(p=>p.group))],environment=auth.capabilities?.monitoring?.environment;
  root.innerHTML=`<div class="wb-shell"><aside class="wb-sidebar"><a class="wb-brand" href="#/wells"><span class="wb-logo">井</span><span>油井数字平台<small>生产监控工作台</small></span></a><div class="wb-catalog-note">55 个页面 · 本批接入 8 页</div><nav aria-label="功能模块">${groups.map(group=>`<details open><summary>${esc(group)}</summary>${catalog.filter(p=>p.group===group&&!p.hidden).map(p=>`<a href="${encode(p.id,{})}" data-page="${esc(p.id)}"><span>${esc(p.label)}</span><small>${CONNECTED_IDS.includes(p.id)?'本批':'待接入'}</small></a>`).join('')}</details>`).join('')}</nav><a class="wb-preview-link" href="http://127.0.0.1:8772/" target="_blank" rel="noopener">打开独立完整预览 ↗</a></aside>
   <div class="wb-main"><header class="wb-topbar"><div><strong>生产监控</strong><span>井 · 终端 · 测点 · 采集事实</span></div><div class="wb-account"><span>${esc(auth.display_name)}</span><button class="button secondary" data-logout>退出登录</button></div></header>
   <div class="wb-environment"><span class="wb-env-tag">${environment==='test'?'功能测试环境':environment==='production'?'业务环境':'环境未就绪'}</span><span>来源 ${esc(auth.capabilities?.monitoring?.source_ref||'未配置')}</span><span data-query-context>数据截止：尚未查询</span><span>只读采集 · 无设备下行控制</span></div>
   <main id="workspace" class="wb-workspace" tabindex="-1"></main></div></div>`;
  root.querySelectorAll('[data-page]').forEach(link=>link.addEventListener('click',event=>{event.preventDefault();void open(link.dataset.page);}));
  root.querySelector('[data-logout]').addEventListener('click',async()=>{
   current?.dispose();++generation;
   try{await client.post('/api/auth/logout',{});showLogin();}catch(error){if(error.status===401)showLogin();else components.toast('退出结果未确认，请重试；本页查询已暂停。','error');}
  });
 }
 function showLogin(message=''){
  if(!active)return;++generation;current?.dispose();current=null;auth=null;clearInterval(interval);interval=null;loginController?.abort();loginController=new AbortController();
  const loginApi=createWorkbenchApi(api,{signal:loginController.signal,isCurrent:()=>active&&!auth});
  root.innerHTML=`<main class="wb-login"><section><div class="eyebrow">OIL WELL OPERATIONS</div><h1>生产监控工作台</h1><p>井、设备、点表与真实采集链路的统一工作入口。</p><p class="muted">独立账号与数据环境。演示版和完整预览继续保留。</p><a href="http://127.0.0.1:8772/" target="_blank" rel="noopener">打开完整预览 ↗</a></section><form class="wb-login-form"><h2>登录工作台</h2><label>用户名<input name="username" autocomplete="username" required></label><label>密码<input name="password" type="password" autocomplete="current-password" required></label><p role="alert" class="inline-error" data-login-error>${esc(message)}</p><button type="submit" class="button primary">登录</button></form></main>`;
  const form=root.querySelector('form'),errorBox=root.querySelector('[data-login-error]');
  form.addEventListener('submit',async event=>{event.preventDefault();if(!form.reportValidity())return;const button=form.querySelector('button');button.disabled=true;errorBox.textContent='';
   try{await loginApi.post('/api/auth/login',{username:form.elements.username.value.trim(),password:form.elements.password.value});const next=await loginApi.get('/api/auth/me');accept(next);shell();const route=locationRoute();await open(route.id,route.params);monitorSession();}
   catch(error){if(error.code!=='cancelled')errorBox.textContent=error.status===401?'用户名或密码不正确':error.message;}finally{button.disabled=false;}});
 }
 function accept(next){if(next.ui_mode!=='monitoring')throw new Error('连接的服务不是独立工作台，已停止。');auth=next;}
 function monitorSession(){
  clearInterval(interval);if(!active||!auth)return;
  interval=setInterval(async()=>{const before=auth;if(!active||!before)return;
   const checkApi=createWorkbenchApi(api,{signal:globalController.signal,isCurrent:()=>active&&auth===before});
   try{const fresh=await checkApi.get('/api/auth/me');if(auth!==before)return;
    if(JSON.stringify(fresh)!==JSON.stringify(before)){accept(fresh);const route=locationRoute();await open(route.id,route.params,true);}}
   catch(error){if(error.status===401)showLogin('登录已失效，请重新登录。');}
  },30000);
 }
 async function open(id,params={},replace=false){
  const turn=++generation;current?.dispose();current=null;
  const page=catalog.find(p=>p.id===id)||catalog.find(p=>p.id==='wells');
  try {
   const routeApi=createWorkbenchApi(api,{signal:globalController.signal,isCurrent:()=>active&&turn===generation});
   const fresh=await routeApi.get('/api/auth/me');if(!active||turn!==generation)return;accept(fresh);
   if(!root.querySelector('#workspace'))shell();
   const workspace=root.querySelector('#workspace');
   const context=createWorkbenchContext({auth,page,params,root:workspace,navigate:open});current=context;
   history[replace?'replaceState':'pushState'](null,'',encode(page.id,params));
   root.querySelectorAll('[data-page]').forEach(link=>{if(link.dataset.page===page.id)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});
   document.title=`${page.label}｜生产监控工作台`;
   await renderWorkbenchRoute(context);if(context.isCurrent())workspace.focus({preventScroll:true});
  }catch(error){if(!active||turn!==generation)return;if(error.status===401){showLogin('登录已失效，请重新登录。');return;}
   if(current)renderError(current,error);else root.innerHTML=`<main class="wb-state"><h2>工作台暂不可用</h2><p>${esc(error.message)}</p><button class="button" data-reload>重试连接</button></main>`;
   root.querySelector('[data-reload]')?.addEventListener('click',()=>start());
  }
 }
 async function start(){
  const turn=++generation;
  const startApi=createWorkbenchApi(api,{signal:globalController.signal,isCurrent:()=>active&&turn===generation});
  try{accept(await startApi.get('/api/auth/me'));shell();const route=locationRoute();await open(route.id,route.params,true);monitorSession();
  }catch(error){if(error.code==='cancelled')return;if(error.status===401)showLogin();else showLogin(error.message||'服务暂不可用');}
 }
 const onLocation=()=>{if(auth){const route=locationRoute();void open(route.id,route.params,true);}};
 const onExpired=()=>showLogin('登录已失效，请重新登录。');
 window.addEventListener('popstate',onLocation);window.addEventListener('hashchange',onLocation);window.addEventListener('workbench:unauthenticated',onExpired);
 return {catalog,connectedIds:[...CONNECTED_IDS],start,open,dispose(){active=false;++generation;current?.dispose();globalController.abort();loginController?.abort();clearInterval(interval);
  window.removeEventListener('popstate',onLocation);window.removeEventListener('hashchange',onLocation);window.removeEventListener('workbench:unauthenticated',onExpired);}};
}

if(document.documentElement.hasAttribute('data-monitoring-workbench')){
 const root=document.querySelector('#app');
 loadCatalog(new URL('../preview/catalog.json',import.meta.url)).then(catalog=>createWorkbenchApp({root,catalog}).start())
  .catch(error=>{root.textContent='工作台目录加载失败：'+error.message;});
}
