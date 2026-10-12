import {renderAcquisitionStream} from '../../pages/acquisition.js';
import {renderQualityBoard} from '../../pages/data-quality.js';
import {renderDeviceList} from '../../pages/devices.js';
import {renderFieldRegistry} from '../../pages/field-registry.js';
import {renderIngestReliability} from '../../pages/ingest-reliability.js';
import {renderWaveforms} from '../../pages/waveforms.js';
import {note} from '../ui.js';
import {renderImportRehearsal} from './waveform-import.js';

const renderers={acquisition:renderAcquisitionStream,'data-quality':renderQualityBoard,devices:renderDeviceList,'field-registry':renderFieldRegistry,'ingest-reliability':renderIngestReliability,waveforms:renderWaveforms};
export async function renderAcquisitionPreview(ctx){
  await renderers[ctx.page.id](ctx);if(!ctx.isCurrent())return;
  if(ctx.page.id==='waveforms'){
    const tab=ctx.workspace.querySelector('[data-wave-tab="imports"]'),body=ctx.workspace.querySelector('.wave-body');
    if(tab&&body){const show=()=>{ctx.workspace.querySelectorAll('[data-wave-tab]').forEach(t=>{t.classList.toggle('active',t===tab);t.setAttribute('aria-current',t===tab?'page':'false');});return renderImportRehearsal(ctx,body);};tab.addEventListener('click',e=>{e.stopImmediatePropagation();show();},true);if(ctx.params.view==='imports')await show();}
  }
  const header=ctx.workspace.querySelector('.page-header');
  header?.insertAdjacentHTML('afterend',note(['acquisition','devices','data-quality'].includes(ctx.page.id)?'读取保留的演示业务快照；画面刷新不表示现场在线。':'新增生产模块使用独立软件测试场景，未连接现场设备或消息代理。所有修改仅在预览库中演练。'));
  const stream=ctx.workspace.querySelector('#streamState');if(stream)stream.textContent='历史报文回看';
  const toggle=ctx.workspace.querySelector('#streamToggle');if(toggle)toggle.addEventListener('click',()=>{if(toggle.textContent==='暂停刷新')stream.textContent='历史报文回看';});
}
