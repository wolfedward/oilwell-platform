import {element} from './forms.js';

// Keep this list aligned with backend.inversion.queries.DIAGNOSTIC_REASONS.
// Each entry gives the operator a concrete fact to check before a new run.
const REASONS={
  coverage_basis_mismatch:['有限输入证明依据不匹配','核对注册工况、原始编码、绑定摘要和误差来源；不得用参考曲线替代证明。'],
  proof_series_truncated:['原生证明序列不完整','核对全部原始样本、通道时间索引和归档分块，保留缺口，不补零重算。'],
  proof_nonfinite:['证明计算出现非有限量','核对原始数值和机械参数适用域；保留拒绝记录，不裁剪数值掩盖异常。'],
  proof_root_ambiguous:['机构几何分支无法唯一确认','核对安装几何、装配分支和完整周期；不得自动选择另一解。'],
  proof_rounding_ambiguous:['当前误差包围无法确认编码舍入','核对原始编码与量化规则，保留不确定结果，不缩小既定误差。'],
  proof_resource_limit:['证明计算达到固定资源上限','保留输入与诊断，检查采样规模和参数适用范围；不得自动扩大限额。'],
  proof_runtime_unavailable:['锁定的数值证明运行库不可用','核对专用环境、原生库版本及安装摘要；不得改用普通浮点替代。'],
  proof_context_required:['数值证明执行上下文缺失','检查固定精度与单线程执行上下文，保留记录并联系系统维护人员。'],
  full_cycle_unconfirmed:['完整物理周期尚未确认','检查角度时间序列、周期边界和同步缺口。'],
  no_valid_points:['没有可用的预测点','检查原始通道质量和逐点预测原因，再重新采集或配对。'],
  insufficient_coverage:['有效支撑不足','检查角度完整周期、原始通道质量与缺口。'],
  dataset_changed:['冻结数据集版本已变化','重新读取数据集冻结版本与内容摘要。'],
  member_changed:['冻结成员已变化','重新读取冻结成员序号和配对身份。'],
  pair_review_changed:['配对复核已变化','重新读取配对复核状态与版本。'],
  evidence_changed:['证据内容或审批已变化','核对来源附件内容摘要和当前审批。'],
  capture_changed:['采集记录已变化或受限','核对采集记录身份、质量与访问状态。'],
  applicability_changed:['观测适用的依赖事实已变化','重新检查本次观测所依赖的登记、选用和有效期。'],
  parameter_pin_changed:['参数内容或复核版本已变化','重新读取参数摘要及独立复核版本。'],
  model_pin_changed:['模型内容或复核版本已变化','重新读取模型摘要及独立复核版本。'],
  selection_changed:['持久化选用已变化','重新读取当前参数与模型选用版本。'],
  selection_incompatible:['当前选用与冻结输入不兼容','核对选用的总成、参数、模型及冻结成员。'],
  operating_conditions_unknown:['本次工况声明无法确认','选择已批准且覆盖本次观测的工况条件。'],
  operating_condition_interval:['工况有效期未覆盖观测','核对工况批准记录的起止时间和观测时间。'],
  channel_installation_changed:['通道安装关系已变化','核对观测时刻的通道安装记录。'],
  archive_capture_missing:['归档采集记录缺失','检查归档采集标识及对应原始记录。'],
  archive_manifest_changed:['归档清单已变化','重新核对归档清单摘要与冻结引用。'],
  archive_restricted:['归档记录当前受限','核对归档访问状态及授权。'],
  archive_source_changed:['归档来源已变化','核对归档来源身份和原始采集来源。'],
  archive_channel_changed:['归档通道已变化','核对归档通道与配对通道绑定。'],
  archive_range_changed:['归档时间范围已变化','核对归档起止时刻及冻结范围。'],
  archive_evidence_interval:['归档证据有效期未覆盖观测','核对归档证据起止时间和观测时间。'],
  archive_sync_invalid:['归档同步证据无效','核对同步记录、审批及通道时间对应关系。'],
  archive_sync_endpoint:['归档同步端点不匹配','核对同步记录两端的采集和通道身份。'],
  archive_sync_interval:['归档同步有效期未覆盖观测','核对同步记录的起止时间和观测时间。'],
  archive_sync_unapproved:['归档同步尚未批准','完成同步记录的独立复核后重新预检。'],
  archive_sync_source:['归档同步来源不匹配','核对同步记录的来源与归档采集来源。'],
  modbus_device_mismatch:['Modbus 设备身份不匹配','核对采集设备与登记设备标识。'],
  modbus_map_unapproved:['Modbus 点表未经批准','核对观测时刻生效的点表审批。'],
  modbus_point_missing:['Modbus 点位缺失','核对所需通道的点位编码与点表。'],
  modbus_assignment_unbounded:['Modbus 点位归属时间不明确','补充并核对点位归属有效期。'],
  modbus_assignment_interval:['Modbus 点表有效期未覆盖观测','核对点位归属起止时间和观测时间。'],
  registry_ineligible:['设备登记或井归属无法覆盖观测','核对设备登记状态与井归属历史。'],
  assignment_unbounded:['井归属时间不明确','补充并核对总成与井的归属有效期。'],
  assignment_interval:['总成与井的有效期未覆盖观测','核对井归属起止时间和观测时间。'],
  source_ineligible:['资料来源不具备本次用途资格','核对来源种类、用途和批准证据。'],
  assignment_well_mismatch:['井归属身份不匹配','核对冻结成员所属井与机械总成归属。'],
  parameter_missing:['固定参数版本不可用','核对参数版本身份和读取权限。'],
  model_missing:['固定模型版本不可用','核对模型版本身份和读取权限。'],
  parameter_interval:['参数有效期未覆盖观测','核对参数声明与生效有效期。'],
  physical_interval:['机械版本有效期未覆盖观测','核对机械版本生效有效期。'],
  source_mismatch:['参数来源与输入不匹配','核对参数来源、总成和冻结输入身份。'],
  attachment_missing:['来源附件缺失','补齐并核对参数或模型引用的附件。'],
  attachment_purpose:['附件用途不覆盖本次计算','核对附件声明用途及批准范围。'],
  attachment_mismatch:['附件身份或摘要不匹配','重新核对附件标识、版本和内容摘要。'],
  evidence_unavailable:['适用证据不可用','核对证据读取权限、状态与原始附件。'],
  evidence_binding:['适用证据绑定不匹配','核对证据与参数、总成及通道的绑定。'],
  evidence_interval:['适用证据有效期未覆盖观测','核对证据起止时间和观测时间。'],
  condition_domain:['观测工况超出模型适用域','核对功率、速度、温度等观测量与批准域。'],
  timing_envelope:['时间对齐超出允许范围','核对通道时钟、同步证据和误差上限。'],
  coverage_mismatch:['证据覆盖范围不匹配','核对证据覆盖的通道、工况和时间范围。'],
  calibration_binding:['标定绑定不匹配','核对标定记录与传感器及通道身份。'],
  calibration_incomplete:['标定资料不完整','补齐本次输入所需的标定记录及有效期。'],
  artifact_changed:['模型制品已变化','重新核对已安装制品版本和内容摘要。'],
  artifact_unavailable:['模型制品不可用','核对已安装制品与当前服务状态。'],
  model_not_installed:['指定模型未安装','选择服务端已登记并批准的模型。'],
};

export function diagnosticReason(code){
  const known=typeof code==='string'&&Object.hasOwn(REASONS,code);
  return {code:typeof code==='string'?code.slice(0,80):String(code).slice(0,80),
    cause:known?REASONS[code][0]:'未知原因',
    next:known?REASONS[code][1]:'重新读取运行及当前审批记录；保留原原因代码供核查。',known};
}

export function appendDiagnosticReasons(section,reasons){
  for(const code of reasons){
    const detail=diagnosticReason(code),row=element('div');
    row.dataset.diagnosticCode=detail.code;
    if(!detail.known)row.dataset.diagnosticUnknown='';
    row.append(element('p',`原因代码 ${detail.code}`));
    const cause=element('p',detail.cause),next=element('p',`下一步：${detail.next}`);
    cause.dataset.diagnosticCause='';next.dataset.diagnosticNextStep='';row.append(cause,next);section.append(row);
  }
}
