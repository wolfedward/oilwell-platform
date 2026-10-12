import {ApiError} from '../api.js';

const READ_PREFIXES=['dashboard','wells','acquisition','data-quality','central','devices','dynagraphs','diagnoses','model-runs','strategies','commands','alarms','work-orders','reports','users','roles','audit','health','acceptance','registry','ingest','ingest-reliability','waveforms','inversion','studies','production'];

function previewPath(path) {
  if(typeof path!=='string'||!path.startsWith('/api/')||path.startsWith('//')||/[\\\u0000-\u0020#]/.test(path)) throw new ApiError('只允许预览接口',{code:'invalid_path'});
  const pathname=path.split('?')[0];
  let decoded;
  try{decoded=decodeURIComponent(pathname);}catch{throw new ApiError('路径编码无效',{code:'invalid_path'});}
  if(decoded.includes('%')||decoded.includes('\\')||decoded.split('/').some(x=>x==='.'||x==='..')) throw new ApiError('路径越界',{code:'invalid_path'});
  if(decoded.startsWith('/api/preview/')) return path;
  if(!READ_PREFIXES.some(p=>decoded===`/api/${p}`||decoded.startsWith(`/api/${p}/`)||decoded===`/api/${p}.csv`)) throw new ApiError('未登记的页面接口',{code:'invalid_path'});
  return `/api/preview/legacy${path}`;
}

export function createPreviewApi({fetchImpl=fetch,csrfToken=()=>'',confirmRehearsal=async()=>null,signal:parentSignal}={}) {
  const controllers=new Set(); let disposed=false;
  const optionsForChild={fetchImpl,csrfToken,confirmRehearsal};
  async function send(path,options={},binary=false) {
    const mapped=previewPath(path), method=(options.method||'GET').toUpperCase();
    if(disposed||parentSignal?.aborted) throw new ApiError('页面已退出',{code:'cancelled'});
    if(method!=='GET'&&!path.startsWith('/api/preview/')) {
      const result=await confirmRehearsal({action:method.toLowerCase(),path,values:options.body,signal:parentSignal});
      if(disposed||parentSignal?.aborted||!result) throw new ApiError('已取消流程演练',{code:'cancelled'});
      return result;
    }
    const controller=new AbortController(); controllers.add(controller);
    const abort=()=>controller.abort();
    parentSignal?.addEventListener('abort',abort,{once:true}); options.signal?.addEventListener('abort',abort,{once:true});
    if(options.signal?.aborted) controller.abort();
    const timer=setTimeout(abort,options.timeoutMs??15000);
    const headers=new Headers(options.headers||{}); headers.set('Accept',binary?'*/*':'application/json');
    if(method!=='GET') headers.set('X-Preview-CSRF',typeof csrfToken==='function'?csrfToken():csrfToken);
    let body=options.body;
    if(body!==undefined){headers.set('Content-Type','application/json');body=JSON.stringify(body);}
    try {
      const response=await fetchImpl(mapped,{method,body,headers,credentials:'same-origin',signal:controller.signal});
      if(disposed||controller.signal.aborted) throw new ApiError('页面已退出',{code:'cancelled'});
      if(binary&&response.ok) return await response.blob();
      const payload=await response.json();
      if(!response.ok||payload.ok===false) throw new ApiError(payload.error?.message||'预览请求失败',{status:response.status,code:payload.error?.code,details:payload.error?.details});
      if(disposed||controller.signal.aborted) throw new ApiError('页面已退出',{code:'cancelled'});
      return payload.data;
    } catch(error) {
      if(controller.signal.aborted||error.name==='AbortError') throw new ApiError('请求已取消或超时',{code:'cancelled'});
      throw error;
    } finally {clearTimeout(timer);controllers.delete(controller);parentSignal?.removeEventListener('abort',abort);options.signal?.removeEventListener('abort',abort);}
  }
  return {
    request:send,
    get:(path,options)=>send(path,{...options,method:'GET'}),
    post:(path,body,options)=>send(path,{...options,method:'POST',body}),
    patch:(path,body,options)=>send(path,{...options,method:'PATCH',body}),
    raw:async(path,bytes,options)=>{
      const size=bytes?.byteLength??bytes?.size;
      if(!Number.isFinite(size)||size>8*1024*1024) throw new ApiError('原文预览上限 8 MiB',{code:'body_limit'});
      return send(path,{...options,method:'POST',body:{byte_length:size,preview_only:true}});
    },
    download:async(path,filename,options={})=>{
      const blob=await send(path,{...options,method:'GET'},true);
      if(options.save===false) return blob;
      const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=filename;
      link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);return blob;
    },
    withSignal:signal=>createPreviewApi({...optionsForChild,signal}),
    dispose(){disposed=true;for(const controller of controllers) controller.abort();controllers.clear();},
  };
}
