import {ApiError} from '../api.js';

const UUID='[0-9a-fA-F-]{36}';
const READS=[/^\/api\/auth\/me$/,/^\/api\/registry\/(wells|devices|audit|resolved-telemetry)$/,
 new RegExp(`^/api/registry/devices/${UUID}(/(state-history|bindings|point-maps|point-map-assignments|link-status))?$`),
 /^\/api\/production-monitoring\/(wells\.csv|quality|acquisition\/messages(?:\/[A-Za-z0-9_.:%-]+)?)$/,
 new RegExp(`^/api/production-monitoring/wells/${UUID}(/(latest|history|history.csv))?$`),
 new RegExp(`^/api/ingest-reliability/(health|cases|receipts|audit)(/${UUID})?$`),
 new RegExp(`^/api/ingest-reliability/facts/${UUID}/(preview|raw)$`)];
const WRITES=[/^\/api\/auth\/(login|logout)$/,/^\/api\/registry\/(wells|devices)$/,
 new RegExp(`^/api/registry/wells/${UUID}/changes$`),
 new RegExp(`^/api/registry/devices/${UUID}/(changes|state|bindings|unbind|point-maps|point-map-assignment)$`),
 new RegExp(`^/api/registry/devices/${UUID}/point-maps/[1-9][0-9]*/review$`),
 new RegExp(`^/api/ingest-reliability/cases/${UUID}/(ack|note|close|reopen)$`)];

export function createWorkbenchApi(baseApi,{signal,isCurrent=()=>true}={}) {
 const current=()=>!signal?.aborted&&isCurrent();
 function check(path,method){
  const pathname=typeof path==='string'?path.split('?')[0]:'';
  if(!path?.startsWith('/api/')||path.includes('#')||/[\\]/.test(path)||/%(?:2f|5c|2e)/i.test(pathname)||
     !(method==='GET'?READS:WRITES).some(rule=>rule.test(pathname)))
   throw new ApiError('此工作台不开放该操作',{status:403,code:'monitoring.path_denied'});
  if(!current())throw new ApiError('页面已关闭，请重新查询',{code:'cancelled'});
 }
 async function call(method,path,body,options={}){
  check(path,method==='post'?'POST':'GET');
  const combined=signal&&options.signal?AbortSignal.any([signal,options.signal]):signal||options.signal;
  const opts={...options,signal:combined,suppressUnauthorizedEvent:true};
  try {
   const result=method==='post'?await baseApi.post(path,body,opts):method==='download'?
    await baseApi.download(path,options.filename||path.split('?')[0].split('/').at(-1),opts):await baseApi.get(path,opts);
   if(!current()||combined?.aborted)throw new ApiError('已忽略旧页面结果',{code:'cancelled'});
   return result;
  }catch(error){
   if(!current()||combined?.aborted)throw new ApiError('已忽略旧页面结果',{code:'cancelled'});
   if(error.status===401&&path!=='/api/auth/login')window.dispatchEvent(new CustomEvent('workbench:unauthenticated'));
   throw error;
  }
 }
 return {get:(path,options)=>call('get',path,null,options),post:(path,body,options)=>call('post',path,body,options),
  download:(path,options)=>call('download',path,null,options)};
}
