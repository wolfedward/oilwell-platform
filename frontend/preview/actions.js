import {rehearsalMarkup} from './presenters/rehearsal.js';

function translate({action,path='',objectId,values={}}) {
  let operation=action;
  if(['post','patch','raw'].includes(action)){
    if(/retry|dispatch|commands|resend|reset/.test(path))operation='control_plan';
    else if(/analyze|calculate|runs|studies/.test(path)&&!(/review|revoke/.test(path)))operation='calculate_input';
    else if(/approve|reject|review/.test(path))operation=/reject/.test(path)?'reject':/approve/.test(path)?'approve':'review';
    else if(/submit/.test(path))operation='submit';
    else if(/scenario/.test(path))operation='scenario';
    else if(/channel|configure/.test(path))operation='configure';
    else if(/confirm|ack/.test(path))operation='acknowledge';
    else if(/close|complete/.test(path))operation='close';
    else operation=action==='patch'?'edit':'create';
  }
  const base=path.replace(/\/(submit|approve|reject|review|revoke|confirm|close|dispatch|retry)$/,'');
  return {action:operation,objectId:objectId||base||'preview-draft',values};
}

export function createPreviewActions({api,isCurrent=()=>true}) {
  return {confirmRehearsal(request){
    const current=()=>isCurrent()&&!request.signal?.aborted;
    if(!current())return Promise.resolve(null);
    const normalized=translate(request), requestId=crypto.randomUUID();
    const wrap=document.createElement('div');wrap.innerHTML=rehearsalMarkup(normalized.action,normalized.values);
    const overlay=wrap.firstElementChild;document.body.append(overlay);
    return new Promise(resolve=>{
      const cancelled=()=>finish(null);
      const finish=value=>{request.signal?.removeEventListener('abort',cancelled);overlay.remove();resolve(value);};
      request.signal?.addEventListener('abort',cancelled,{once:true});
      overlay.querySelector('[data-rehearsal-cancel]').onclick=()=>finish(null);
      overlay.querySelector('[data-rehearsal-submit]').onclick=async event=>{
        if(!current()){finish(null);return;}
        const button=event.currentTarget;button.disabled=true;
        try{
          const state=await api.get('/api/preview/rehearsals?object_id='+encodeURIComponent(normalized.objectId));
          if(!current()){finish(null);return;}
          const result=await api.post('/api/preview/rehearsals',{request_id:requestId,action:normalized.action,object_id:normalized.objectId,expected_version:state.version,values:normalized.values});
          if(!current()){finish(null);return;}
          finish(result);
        }catch(error){overlay.querySelector('[data-rehearsal-error]').textContent=error.message;button.disabled=false;}
      };
    });
  }};
}
