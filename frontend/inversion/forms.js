export const element = (tag, text, className) => {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = String(text);
  if (className) item.className = className;
  return item;
};

export function input(form, label, name, {multiline = false, value = '', required = false} = {}) {
  const wrap = element('label', label), field = element(multiline ? 'textarea' : 'input');
  field.name = name; field.value = value; field.required = required;
  if (!multiline) field.type = 'text';
  wrap.append(field); form.append(wrap); return field;
}

export function errorText(error) {
  if (error?.code === 'input_invalid') return error.message;
  if (error instanceof SyntaxError) return 'JSON 格式不正确，请核对输入。';
  if (error?.status === 401) return '登录已失效。重新登录后可查询原操作。';
  if (error?.status === 403) return '当前登录会话无权继续，或不允许自我复核。';
  if (error?.status === 409) return '版本、证据或独立复核条件不满足；请核对原操作。';
  if (error?.status === 400) return '输入不符合接口要求，请核对字段。';
  return '结果待确认；请先查询原操作，再决定是否用同一请求重试。';
}

export function mountActionForm(root, client, spec, onChanged) {
  const form = element('form', undefined, 'inversion-command'); form.dataset.inversionAction = spec.action;
  const body = spec.bodyEditor ? input(form, spec.bodyLabel || '严格 JSON（保留数值字符串与来源附件）', 'body', {multiline: true, value: spec.bodyText || '{}', required: true}) : null;
  const reason = input(form, `${spec.label}理由`, 'reason', {required: true});
  const decision = spec.decisions ? element('select') : null;
  if (decision) {decision.name = 'decision'; for (const [value,label] of spec.decisions) decision.append(new Option(label,value)); form.append(decision);}
  const send = element('button', spec.label, 'button primary'); send.type = 'submit'; form.append(send);
  const recover = element('button','查询原操作','button'); recover.type='button'; recover.dataset.inversionRecover=''; form.append(recover);
  const replay = element('button','使用原请求重试','button'); replay.type='button'; replay.dataset.inversionReplay=''; form.append(replay);
  const savedView = element('pre', '', 'inversion-saved'); savedView.dataset.inversionSaved=''; form.append(savedView);
  const status = element('p', '', 'inversion-command-status'); status.setAttribute('role','status'); form.append(status); root.append(form);
  let disposed = false, busy = false;
  const slot = spec.slot || spec.action + ':' + spec.path;
  const pending = () => client.pending.get(slot);
  function update() {
    if (disposed) return;
    const saved = pending(); body && (body.disabled = !!saved); reason.disabled = !!saved; decision && (decision.disabled = !!saved);
    send.disabled = busy || !!saved; recover.hidden = !saved; replay.hidden = !saved; recover.disabled = replay.disabled = busy || !saved;
    if (saved) {
      reason.value = saved.envelope.command.reason;
      if (body) body.value = JSON.stringify(saved.envelope.body, null, 2);
      if (decision) decision.value = saved.envelope.body?.decision || '';
      savedView.hidden = false;
      savedView.textContent = JSON.stringify({action:saved.action,target:saved.path,
        command:saved.envelope.command,body:saved.envelope.body},null,2);
      status.textContent ||= '待确认原操作；先查询其提交结果。';
    } else savedView.hidden = true;
  }
  async function execute(retry = false) {
    if (disposed || busy) return;
    busy = true; update();
    try {
      let value;
      if (retry) value = await client.submit({slot, replay:true});
      else {
        const request = {...spec, slot, reason: reason.value};
        if (body) request.body = JSON.parse(body.value);
        else if (spec.fixedBody) request.body = structuredClone(spec.fixedBody);
        else if (decision) request.body = {decision: decision.value};
        spec.validateBody?.(request.body);
        value = await client.submit(request);
      }
      if (!disposed && value) {status.textContent = value.state === 'unknown' ? '原操作仍待确认；可使用完全相同的请求重试。' : '操作已提交。'; if (value.state !== 'unknown') await onChanged?.(value);}
    } catch (error) {if (!disposed) status.textContent = errorText(error);}
    finally {busy = false; update();}
  }
  form.addEventListener('submit', event => {event.preventDefault(); execute();});
  recover.addEventListener('click', async () => {
    if (disposed || busy) return; busy=true; update();
    try {const value=await client.recoverPending(slot); if (!disposed) {status.textContent=value?.state==='committed'?'原操作已提交。':'待确认原操作：服务尚未确认提交，可使用原请求重试。'; if (value?.state==='committed') await onChanged?.(value);}}
    catch(error){if(!disposed)status.textContent=errorText(error);} finally {busy=false;update();}
  });
  replay.addEventListener('click',()=>execute(true)); update();
  return {dispose(){disposed=true;}};
}
