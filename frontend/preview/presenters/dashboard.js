import {renderDashboard} from '../../pages/dashboard.js';
import {renderWellDetail} from '../../pages/well-detail.js';
import {escapeHtml as esc} from '../../components.js';
import {filterMarkup,bindFilters} from '../ui.js';

export async function renderDashboardPreview(ctx){
  const query=new URLSearchParams(ctx.params).toString();
  const api={...ctx.api,get:path=>ctx.api.get(path==='/api/dashboard'?path+(query?'?'+query:''):path)};
  await renderDashboard({...ctx,api});if(!ctx.isCurrent())return;
  const wells=await ctx.api.get('/api/wells?page=1&page_size=100');if(!ctx.isCurrent())return;
  const form=filterMarkup({wells:wells.items,from:ctx.meta.data_from,to:ctx.meta.data_to},ctx.params);
  ctx.workspace.querySelector('.page-header').insertAdjacentHTML('afterend',form);
  const short=ctx.workspace.querySelector('.quick-entry header>span');if(short)short.textContent='6 个业务模块';
  bindFilters(ctx);
}

export async function renderWellDetailPreview(ctx){
  await renderWellDetail(ctx);if(!ctx.isCurrent()||!ctx.permissions.has('wells.update'))return;
  const id=ctx.params.id||(await ctx.api.get('/api/wells?page_size=1')).items[0]?.id;if(!id)return;
  const detail=await ctx.api.get('/api/wells/'+encodeURIComponent(id));if(!ctx.isCurrent())return;
  const button=document.createElement('button');button.className='button secondary';button.dataset.editWell='';button.textContent='编辑基础资料（演练）';
  ctx.workspace.querySelector('.header-actions').append(button);
  button.onclick=()=>{
    const content=document.createElement('div');content.innerHTML=`<form data-well-edit-form><div class="form-field"><label>井号</label><input name="code" value="${esc(detail.well.code||'')}" required maxlength="80"></div><div class="form-field"><label>油井名称</label><input name="name" value="${esc(detail.well.name)}" required maxlength="160"></div><p class="muted">原始台账保持不变，修改仅形成独立演练记录。</p></form>`;
    const modal=ctx.components.modal({title:'单井基础资料',content,actions:[{label:'保存演练',kind:'primary',onClick:async()=>{
      const form=content.querySelector('form');if(!form.reportValidity())return;
      const result=await ctx.actions?.confirmRehearsal({action:'edit',objectId:'/api/wells/'+id,values:Object.fromEntries(new FormData(form))});
      if(result)modal.close();
    }}]});
    ctx.registerDispose(()=>modal.close());
  };
}
