import {scanWaveformFile} from '../../waveforms/file-scan.js';
import {note,table} from '../ui.js';

export async function renderImportRehearsal(ctx,root){
 const objectId='preview-waveform-import/'+ctx.actorId;
 root.innerHTML=`<h2>文件导入 / 恢复演练</h2>${note('文件只在浏览器内进行格式、分块和摘要预检。保存的是文件元信息与流程状态，不上传原文，不产生新的录波或配对记录。')}<form data-preview-wave-import><div class="form-field"><label>OWP-WAVE 文件</label><input type="file" accept=".owpwave,.jsonl" required></div><div class="form-field"><label>用途说明</label><input name="reason" minlength="2" maxlength="500" required></div><button class="button primary">预检并保存输入</button><button type="button" class="button secondary" data-import-resume>核对原文件并恢复演练</button><button type="button" class="button secondary" data-import-cancel>取消演练会话</button></form><p data-import-status aria-live="polite"></p><div data-import-summary></div>`;
 const form=root.querySelector('form'),status=root.querySelector('[data-import-status]');let saved={},busy=false;
 const current=()=>ctx.isCurrent()&&root.isConnected&&root.contains(form);
 const display=()=>{root.querySelector('[data-import-summary]').innerHTML=table([['field','保存字段'],['value','元信息']],Object.entries(saved).map(([field,value])=>({field,value})));};
 try{const record=await ctx.api.get('/api/preview/rehearsals?object_id='+encodeURIComponent(objectId));if(!current())return;saved=record.values||{};display();}catch(error){if(current())status.textContent=error.message;}
 const run=async mode=>{
  if(busy||!current())return;busy=true;form.querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
   let values;
   if(mode==='cancelled'){if(!saved.file_sha256)throw Error('没有可取消的已保存输入');values={...saved,phase:mode};}
   else{
    if(!form.reportValidity())return;const file=form.querySelector('[type=file]').files[0];
    status.textContent='正在本地核验格式与完整摘要…';const descriptor=await scanWaveformFile(file);
    if(mode==='resumed'&&(!saved.file_sha256||descriptor.file_sha256!==saved.file_sha256))throw Error('恢复必须选择摘要一致的原文件');
    const keys=['file_sha256','bytes','line_count','final_lf','gateway_id','device_id','capture_id'];
    values={...Object.fromEntries(keys.map(k=>[k,descriptor[k]])),filename:file.name,reason:form.elements.reason.value,phase:mode,source_kind:'browser_preflight',raw_uploaded:false};
   }
   if(!current())return;
   const result=await ctx.actions.confirmRehearsal({action:'draft',objectId,values});
   if(result&&current()){saved=values;display();status.textContent=mode==='cancelled'?'演练会话已取消；未上传原文。':'本地预检通过，输入演练已保存；未创建录波。刷新页面后可核对原文件恢复。';}
  }catch(error){if(current())status.textContent='未保存：'+error.message;}
  finally{busy=false;if(current())form.querySelectorAll('button').forEach(b=>b.disabled=false);}
 };
 form.onsubmit=e=>{e.preventDefault();run('preflight_saved');};
 root.querySelector('[data-import-resume]').onclick=()=>run('resumed');root.querySelector('[data-import-cancel]').onclick=()=>run('cancelled');
}
