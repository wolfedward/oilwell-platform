import {escapeHtml as esc} from '../../components.js';

export function rehearsalMarkup(action,values) {
  return `<div class="modal-overlay"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="previewRehearsalTitle"><header><h2 id="previewRehearsalTitle">流程演练确认</h2></header><div class="modal-content"><div class="preview-login-note">仅保存演练结果，未发送至设备。<br>不会修改原演示数据或运行真实算法。</div><p>演练动作：${esc(action)}</p><pre style="max-height:220px;overflow:auto;white-space:pre-wrap">${esc(JSON.stringify(values,null,2))}</pre><p role="alert" data-rehearsal-error></p></div><footer class="modal-actions"><button class="button secondary" data-rehearsal-cancel>取消，保留表单</button><button class="button primary" data-rehearsal-submit>保存演练</button></footer></section></div>`;
}
