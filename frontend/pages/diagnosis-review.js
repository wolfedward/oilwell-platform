import { escapeHtml } from "../components.js";

const CONDITIONS = [["normal", "正常"], ["supply_shortage", "供液不足"], ["intermittent", "间歇供液"], ["pump_leak", "泵阀漏失"], ["empty_pump", "抽空"]];
const conditionLabel = (value) => CONDITIONS.find(([code]) => code === value)?.[1] || value || "—";

function reviewModal(components, diagnosis) {
  return components.trusted(`<form id="diagnosisReviewForm" class="modal-form">
    <div class="inline-note">人工复核记录独立保存，不覆盖机器诊断结论、可信度或模型运行结果。</div>
    <div class="form-field"><label for="reviewCondition">复核工况</label><select id="reviewCondition" name="condition" required>${CONDITIONS.map(([value, label]) => `<option value="${value}" ${diagnosis.condition === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
    <div class="form-field"><label for="reviewComment">复核意见</label><textarea id="reviewComment" name="comment" rows="5" minlength="2" maxlength="500" required placeholder="说明曲线特征、现场依据及判断理由"></textarea><small>2—500 个字符，提交后形成不可覆盖的审计记录。</small></div>
    <input type="hidden" name="version" value="${escapeHtml(diagnosis.review_version)}" />
    <p id="reviewProgress" class="muted">当前复核版本 ${escapeHtml(diagnosis.review_version)}</p>
  </form>`);
}

export async function renderDiagnosisReview({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  let dynagraphId = params.id;
  if (!dynagraphId) {
    const list = await api.get("/api/dynagraphs?status=analyzed&page_size=1");
    dynagraphId = list.items?.[0]?.id;
    if (!dynagraphId) { workspace.innerHTML = `${pageHeader(page)}${components.empty("暂无可复核的诊断记录")}`; return true; }
  }
  const data = await api.get(`/api/dynagraphs/${encodeURIComponent(dynagraphId)}`);
  if (!isCurrent()) return false;
  const diagnosis = data.latest_diagnosis;
  if (!diagnosis) {
    workspace.innerHTML = `${pageHeader(page, '<button id="reviewBack" type="button" class="button secondary">返回功图</button>')}<section class="state-panel empty"><span class="state-icon">○</span><div><h2>该功图尚未形成诊断</h2><p>请先运行电功图转换、特征提取和智能诊断。</p><button id="goAnalysis" type="button" class="button primary">进入智能分析</button></div></section>`;
    workspace.querySelector("#reviewBack").addEventListener("click", () => navigate("dynagraphs", { well_id: data.well_id }));
    workspace.querySelector("#goAnalysis").addEventListener("click", () => navigate("dynagraph-analysis", { id: data.id }));
    return true;
  }
  const canReview = permissions.has("diagnosis.review");
  const candidateColumns = [{ key: "condition", label: "候选工况", render: (value) => conditionLabel(value) }, { key: "combined_score", label: "综合得分", render: (value) => format.percent(value) }, { key: "template_score", label: "模板相似度", render: (value) => format.percent(value) }, { key: "rule_score", label: "规则得分", render: (value) => format.percent(value) }, { key: "sample_count", label: "模板样本" }, { key: "template_version", label: "模板版本" }];
  const ruleColumns = [{ key: "description", label: "诊断判据" }, { key: "condition", label: "指向工况", render: (value) => conditionLabel(value) }, { key: "observed", label: "观测值", render: (value) => format.number(value, 4) }, { key: "threshold", label: "阈值", render: (value) => format.number(value, 4) }, { key: "severity", label: "强度", render: (value) => format.percent(value) }];
  const reviewColumns = [{ key: "id", label: "记录编号", render: (value) => `#${value}` }, { key: "diagnosis_version", label: "基于版本" }, { key: "reviewer_condition", label: "人工结论", render: (value) => conditionLabel(value) }, { key: "comment", label: "复核意见" }, { key: "reviewer_name", label: "复核人" }, { key: "created_at", label: "提交时间", render: (value) => format.dateTime(value) }];
  workspace.innerHTML = `${pageHeader(page, `<div class="header-actions"><button id="reviewBack" type="button" class="button secondary">返回分析</button>${canReview ? '<button id="createReview" type="button" class="button primary">提交人工复核</button>' : ""}</div>`)}
    <section class="detail-hero panel"><div><span class="object-code">${escapeHtml(diagnosis.id)}</span><h2>${escapeHtml(data.well_name)} · 诊断复核</h2><p>机器结果与人工意见分层留存，可回溯功图 ${escapeHtml(data.id)} 和模型运行 ${escapeHtml(data.latest_model_run_id)}</p></div><dl><div><dt>机器诊断结论</dt><dd>${escapeHtml(conditionLabel(diagnosis.condition))}</dd></div><div><dt>可信度</dt><dd>${escapeHtml(format.percent(diagnosis.confidence))}</dd></div><div><dt>诊断状态</dt><dd>${components.statusTag(diagnosis.status).html}</dd></div><div><dt>复核版本</dt><dd>V${escapeHtml(diagnosis.review_version)}</dd></div></dl></section>
    <section class="detail-grid"><article class="panel list-panel wide"><header><div><p class="eyebrow">CANDIDATE CONDITIONS</p><h2>候选工况</h2></div><span>${diagnosis.candidates?.length || 0} 项</span></header><div class="panel-body">${components.table(candidateColumns, diagnosis.candidates || [])}</div></article><article class="panel info-panel"><header><div><p class="eyebrow">MACHINE RESULT</p><h2>机器诊断结论</h2></div></header><dl><div><dt>模板编号</dt><dd class="wrap">${escapeHtml(diagnosis.template_id)}</dd></div><div><dt>模板版本</dt><dd>${escapeHtml(diagnosis.template_version)}</dd></div><div><dt>模型结论</dt><dd>${escapeHtml(conditionLabel(diagnosis.condition))}</dd></div><div><dt>可信度</dt><dd>${escapeHtml(format.percent(diagnosis.confidence))}</dd></div></dl><p class="source-note">${escapeHtml(diagnosis.recommendation)}</p></article></section>
    <section class="panel list-panel" style="margin-bottom:14px"><header><div><p class="eyebrow">RULE EVIDENCE</p><h2>诊断规则依据</h2></div><span>观测量与阈值并列展示</span></header><div class="panel-body">${components.table(ruleColumns, diagnosis.all_rule_hits || [], { emptyText: "本次诊断无显著规则命中" })}</div></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">MANUAL REVIEW HISTORY</p><h2>人工复核记录</h2></div><span>机器结论保持不变</span></header><div class="panel-body">${components.table(reviewColumns, data.reviews || [], { emptyText: canReview ? "尚无人工复核记录，可提交首条意见" : "尚无人工复核记录" })}</div></section>`;
  workspace.querySelector("#reviewBack").addEventListener("click", () => navigate("dynagraph-analysis", { id: data.id }));
  workspace.querySelector("#createReview")?.addEventListener("click", () => {
    components.modal({
      title: `复核诊断 ${diagnosis.id}`,
      content: reviewModal(components, diagnosis),
      actions: [
        { label: "取消", onClick: ({ close }) => close() },
        { label: "提交复核", kind: "primary", onClick: async ({ close, overlay }) => {
          const form = overlay.querySelector("#diagnosisReviewForm");
          if (!form.reportValidity()) return;
          const values = Object.fromEntries(new FormData(form));
          const action = overlay.querySelector(".button.primary");
          action.disabled = true;
          action.textContent = "提交中…";
          overlay.querySelector("#reviewProgress").textContent = "正在校验版本并写入独立复核记录。";
          try {
            await api.post(`/api/diagnoses/${encodeURIComponent(diagnosis.id)}/review`, { condition: values.condition, comment: values.comment.trim(), version: Number(values.version) });
            components.toast("人工复核已记录，机器结论未被覆盖");
            close();
            navigate("diagnosis-review", { id: data.id });
          } catch (error) {
            overlay.querySelector(".modal-content").innerHTML = components.errorPanel(error);
            action.disabled = true;
            action.textContent = "请关闭后重试";
          }
        } },
      ],
    });
  });
  return true;
}
