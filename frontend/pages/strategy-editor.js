import { escapeHtml } from "../components.js";

const DEFAULT_CONSTRAINTS = {
  min_on_minutes: 20, max_on_minutes: 120,
  min_off_minutes: 15, max_off_minutes: 180,
  min_liquid_m3_day: 1, max_starts_per_day: 12, motor_kw: 18.5,
};

function numberField(name, label, value, { min, max, step = 1, unit = "" } = {}) {
  return `<div class="form-field"><label for="${name}">${escapeHtml(label)}</label><div class="input-unit"><input id="${name}" name="${name}" type="number" required value="${escapeHtml(value)}" min="${min}" max="${max}" step="${step}" /><span>${escapeHtml(unit)}</span></div></div>`;
}

function valuesFrom(form) {
  const raw = Object.fromEntries(new FormData(form));
  return {
    well_id: String(raw.well_id || "").trim(),
    constraints: {
      min_on_minutes: Number(raw.min_on_minutes), max_on_minutes: Number(raw.max_on_minutes),
      min_off_minutes: Number(raw.min_off_minutes), max_off_minutes: Number(raw.max_off_minutes),
      min_liquid_m3_day: Number(raw.min_liquid_m3_day),
      max_starts_per_day: Number(raw.max_starts_per_day), motor_kw: Number(raw.motor_kw),
    },
  };
}

async function perform(action, components) {
  try { await action(); } catch (error) { components.toast(error.message || "操作失败", "error"); }
}

export async function renderStrategyEditor({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  let strategy = null;
  if (params.id) strategy = await api.get(`/api/strategies/${encodeURIComponent(params.id)}`);
  if (!isCurrent()) return false;
  const config = strategy?.configuration || {};
  const constraints = { ...DEFAULT_CONSTRAINTS, ...(config.constraints || {}) };
  const selected = config.selected_candidate || {};
  const canCreate = permissions.has("strategy.create");
  const canSubmit = permissions.has("strategy.submit") && strategy?.status === "draft";
  const readonly = !canCreate || (strategy && strategy.status !== "draft");
  workspace.innerHTML = `${pageHeader(page, `<button type="button" id="backToStrategies" class="button secondary">返回策略中心</button>`)}
    <section class="detail-hero panel"><div><span class="object-code">${escapeHtml(strategy?.id || "NEW STRATEGY")}</span><h2>${strategy ? escapeHtml(strategy.name) : "低产井自适应间抽策略"}</h2><p>由恢复试验观测、渗流模型和安全约束共同生成，计算结果在服务端形成不可变版本。</p></div><dl><div><dt>当前状态</dt><dd>${strategy ? components.statusTag(strategy.status).html : "待优化"}</dd></div><div><dt>业务版本</dt><dd>${escapeHtml(strategy?.version || "—")}</dd></div><div><dt>模型版本</dt><dd>${escapeHtml(config.recovery_model?.model_version || "计算后生成")}</dd></div></dl></section>
    <section class="panel"><header><div><p class="eyebrow">ENGINEERING CONSTRAINTS</p><h2>安全约束与优化计算</h2></div><span>全部字段由后端再次执行范围与可行性校验</span></header><div class="panel-body"><form id="strategyOptimizeForm" class="form-stack">
      <div class="form-field"><label for="strategyWell">低产井编号</label><input id="strategyWell" name="well_id" required pattern="[A-Za-z0-9_-]+" value="${escapeHtml(strategy?.well_id || params.well_id || "")}" placeholder="如 W-033" ${strategy ? "readonly" : ""} /></div>
      <fieldset ${readonly ? "disabled" : ""}><legend>开关井范围</legend><div class="filter-grid">${numberField("min_on_minutes", "最短开井", constraints.min_on_minutes, { min: 20, max: 240, unit: "min" })}${numberField("max_on_minutes", "最长开井", constraints.max_on_minutes, { min: 20, max: 240, unit: "min" })}${numberField("min_off_minutes", "最短关井", constraints.min_off_minutes, { min: 15, max: 360, unit: "min" })}${numberField("max_off_minutes", "最长关井", constraints.max_off_minutes, { min: 15, max: 360, unit: "min" })}</div></fieldset>
      <fieldset ${readonly ? "disabled" : ""}><legend>生产与设备边界</legend><div class="filter-grid">${numberField("min_liquid_m3_day", "最低日产液", constraints.min_liquid_m3_day, { min: 0.01, max: 1000, step: 0.01, unit: "m³/d" })}${numberField("max_starts_per_day", "每日最多启停", constraints.max_starts_per_day, { min: 1, max: 24, unit: "次" })}${numberField("motor_kw", "电机额定功率", constraints.motor_kw, { min: 0.1, max: 200, step: 0.1, unit: "kW" })}</div></fieldset>
      ${!strategy && canCreate ? '<div class="form-actions"><button type="submit" class="button primary">优化计算并保存草稿</button></div>' : ""}
    </form></div></section>
    ${strategy ? `<section class="panel"><header><div><p class="eyebrow">SELECTED CYCLE</p><h2>保存草稿</h2></div><span>当前配置版本 ${escapeHtml(strategy.current_version_id)}</span></header><div class="panel-body"><form id="strategyDraftForm" class="filter-grid"><input type="hidden" name="version" value="${escapeHtml(strategy.version)}" />${numberField("on_minutes", "开井时长", selected.on_minutes, { min: constraints.min_on_minutes, max: constraints.max_on_minutes, unit: "min" })}${numberField("off_minutes", "关井时长", selected.off_minutes, { min: constraints.min_off_minutes, max: constraints.max_off_minutes, unit: "min" })}<div class="filter-actions">${canCreate && strategy.status === "draft" ? '<button type="submit" class="button secondary">保存草稿</button>' : ""}${canSubmit ? '<button type="button" id="submitStrategy" class="button primary">提交审批</button>' : ""}</div></form><p class="source-note">预计日产液 ${escapeHtml(format.number(selected.predicted_liquid_m3, 3))} m³，预计耗电 ${escapeHtml(format.number(selected.predicted_energy_kwh, 2))} kWh，日启停 ${escapeHtml(format.number(selected.starts_per_day))} 次。</p></div></section>` : ""}`;

  workspace.querySelector("#backToStrategies").addEventListener("click", () => navigate("strategy-center", strategy ? { id: strategy.id } : {}));
  workspace.querySelector("#strategyOptimizeForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (!event.currentTarget.reportValidity()) return;
    perform(async () => {
      const body = valuesFrom(event.currentTarget);
      if (body.constraints.min_on_minutes > body.constraints.max_on_minutes || body.constraints.min_off_minutes > body.constraints.max_off_minutes) throw new Error("最短时长不能大于最长时长");
      const created = await api.post("/api/strategies/optimize", body);
      components.toast("优化完成，策略草稿及候选排序已保存");
      await navigate("strategy-center", { id: created.id });
    }, components);
  });
  workspace.querySelector("#strategyDraftForm")?.addEventListener("submit", (event) => {
    event.preventDefault();
    perform(async () => {
      const data = Object.fromEntries(new FormData(event.currentTarget));
      const updated = await api.patch(`/api/strategies/${encodeURIComponent(strategy.id)}`, { version: Number(data.version), on_minutes: Number(data.on_minutes), off_minutes: Number(data.off_minutes) });
      components.toast("草稿已形成新的不可变配置版本");
      await navigate("strategy-editor", { id: updated.id });
    }, components);
  });
  workspace.querySelector("#submitStrategy")?.addEventListener("click", () => perform(async () => {
    const submitted = await api.post(`/api/strategies/${encodeURIComponent(strategy.id)}/submit`, { version: strategy.version });
    components.toast("策略已提交独立审批");
    await navigate("strategy-center", { id: submitted.id });
  }, components));
  return true;
}
