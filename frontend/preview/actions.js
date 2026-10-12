import {rehearsalMarkup} from './presenters/rehearsal.js';

function translate({action,path='',objectId,values={}}) {
  let operation=action;
  if(['post','patch','raw'].includes(action)){
    if(path.endsWith('/transition'))operation=values.target==='review'?'submit':values.target==='closed'?'close':'edit';
    else if(/retry|dispatch|commands|resend|reset/.test(path))operation='control_plan';
    else if(/analyze|calculate|runs|studies/.test(path)&&!(/review|revoke/.test(path)))operation='calculate_input';
    else if(/approve|reject|review/.test(path))operation=/reject/.test(path)?'reject':/approve/.test(path)?'approve':'review';
    else if(/submit/.test(path))operation='submit';
    else if(/scenario/.test(path))operation='scenario';
    else if(/channel|configure/.test(path))operation='configure';
    else if(/confirm|ack/.test(path))operation='acknowledge';
    else if(/close|complete/.test(path))operation='close';
    else operation=action==='patch'?'edit':'create';
  }
  const base=path.replace(/\/(submit|approve|reject|review|revoke|confirm|close|dispatch|retry|transition)$/,'');
  return {action:operation,objectId:objectId||base||'preview-draft',values};
}

export function createPreviewActions({api,isCurrent=()=>true}) {
  return {confirmRehearsal(request){
    const current=()=>isCurrent()&&!request.signal?.aborted;
    if(!current())return Promise.resolve(null);
    const normalized=translate(request), requestId=crypto.randomUUID();
    // Capture once, before confirmation; retries reuse exactly the same envelope.
    const versionPromise=request.expectedVersions!==undefined?Promise.resolve({version:request.expectedVersions[normalized.objectId]||0}):api.get('/api/preview/rehearsals?object_id='+encodeURIComponent(normalized.objectId));
    const envelope=versionPromise.then(state=>({body:{request_id:requestId,action:normalized.action,object_id:normalized.objectId,expected_version:state.version,values:structuredClone(normalized.values)}}),error=>({error}));
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
          const savedEnvelope=await envelope;if(savedEnvelope.error)throw savedEnvelope.error;
          if(!current()){finish(null);return;}
          const result=await api.post('/api/preview/rehearsals',savedEnvelope.body);
          if(!current()){finish(null);return;}
          finish(result);
        }catch(error){overlay.querySelector('[data-rehearsal-error]').textContent=error.message+(error.status===409?'。请取消并刷新页面后重新核对；旧表单不会覆盖新版本。':'');button.disabled=error.status===409;}
      };
    });
  }};
}
