import {createPreviewApi} from './api.js';
import {loadCatalog} from './catalog.js';
import {mountPreviewShell} from './shell.js';
import {escapeHtml as esc} from '../components.js';
import {createPreviewActions} from './actions.js';

const root=document.querySelector('#app');let session=null,app=null;
const actions=createPreviewActions({api:{get:(...args)=>api.get(...args),post:(...args)=>api.post(...args)},isCurrent:()=>!!session});
const api=createPreviewApi({csrfToken:()=>session?.csrf_token||'',confirmRehearsal:request=>actions.confirmRehearsal(request)});
async function loginView(message='') {
  app?.dispose();session=null;
  root.innerHTML=`<main id="workspace" class="login-page"><section class="login-brand"><img src="/assets/heneng-logo.png" alt="合能科技"><p class="eyebrow">PAGE REVIEW WORKSPACE</p><h1>油井数字智能化平台</h1><p>采油监控 · 功图诊断 · 节能优化 · 电机保护</p><ul class="login-capabilities"><li><strong>55</strong><span>完整业务页面</span></li><li><strong>独立</strong><span>保留原演示版</span></li><li><strong>可追溯</strong><span>数据与页面审查</span></li></ul></section><section class="login-panel"><div class="login-card"><p class="eyebrow">ISOLATED PREVIEW</p><h2>登录页面预览</h2><div class="preview-login-note">页面预览 · 测试数据<br>表单和业务流程仅供审查演练，不发送设备指令。</div><form id="previewLogin"><div class="form-field"><label for="previewUser">账号</label><input id="previewUser" name="username" autocomplete="username" required></div><div class="form-field"><label for="previewPassword">密码</label><input id="previewPassword" name="password" type="password" autocomplete="current-password" required></div><p role="alert" class="inline-error">${esc(message)}</p><button class="button primary" type="submit">进入工作台</button></form><p class="muted">使用独立预览账号，原演示账号不受影响。</p></div></section></main>`;
  root.querySelector('form').onsubmit=async event=>{
    event.preventDefault();const form=event.currentTarget;const button=form.querySelector('button');button.disabled=true;
    try {session=await api.post('/api/preview/session',Object.fromEntries(new FormData(form)));await boot();}
    catch(error){form.querySelector('[role=alert]').textContent=error.message;button.disabled=false;}
  };
}
async function boot(){
  const [catalog,meta]=await Promise.all([api.get('/api/preview/catalog'),api.get('/api/preview/meta')]);
  app=await mountPreviewShell({root,catalog,meta,api,session,actions,onActorChange:async role=>{
    app?.dispose();session=await api.post('/api/preview/actor',{role_code:role});await boot();
  },onLogout:async()=>{
    try{await api.request('/api/preview/session',{method:'DELETE'});}finally{await loginView();}
  }});
}
try{session=await api.get('/api/preview/session');await boot();}catch(error){await loginView(error.status===401?'':error.message);}
