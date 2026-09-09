const wellNames = [
  "HN-YJ-001",
  "HN-YJ-006",
  "HN-YJ-012",
  "HN-YJ-018",
  "HN-YJ-023",
  "HN-YJ-031",
  "HN-YJ-045",
  "HN-YJ-052",
];

const units = ["东营采油一区", "东营采油二区", "滨海低产井区", "胜北试验井区"];

const page = (id, title, type, desc, domain = "生产运行") => ({ id, title, type, desc, domain });

const modules = [
  {
    id: "home",
    label: "首页",
    icon: "H",
    groups: [
      {
        title: "平台门户",
        pages: [
          page("home-overview", "生产运行总览", "dashboard", "集中呈现产量、开井、报警、节能、设备在线等核心指标。", "平台门户"),
          page("home-map", "井场分布看板", "map", "按照单位、井组和设备状态查看井场分布与运行状态。", "平台门户"),
          page("home-model", "智能模型总览", "analysis", "查看功图转换、空抽识别、开停机优化等模型运行效果。", "平台门户"),
          page("home-task", "待办任务中心", "workflow", "汇总报警、巡检、维护、审批和报表待办事项。", "平台门户"),
          page("home-message", "消息通知中心", "table", "管理系统通知、策略推送、告警订阅和短信发送记录。", "平台门户"),
        ],
      },
    ],
  },
  {
    id: "monitor",
    label: "生产监控",
    icon: "M",
    groups: [
      {
        title: "采油监控",
        pages: [
          page("oil-system", "采油系统监控", "table", "统计采油单元开井、关井、通讯、长停、产液、产油和能耗。", "采油监控"),
          page("oil-run", "油井运行监控", "table", "按单位和井号监测抽油机井、电泵井、螺杆泵井运行状态。", "采油监控"),
          page("oil-well", "油井监控", "dashboard", "查看单井实时状态、运行曲线、告警和策略执行情况。", "采油监控"),
          page("oil-realtime", "油井实时数据", "table", "展示电流、电压、功率、冲次、载荷、产液、含水等实时数据。", "采油监控"),
          page("oil-history", "油井历史参数", "analysis", "按时间回放油井运行参数，支持趋势对比和异常定位。", "采油监控"),
          page("oil-card", "单井基础信息", "form", "维护单井基础档案、设备参数、地质参数和生产参数。", "采油监控"),
          page("oil-dynagraph", "油井功图数据", "analysis", "查看示功图、电功图、冲次软测量结果与模型解释。", "采油监控"),
          page("oil-dynagraph-track", "油井功图跟踪", "analysis", "连续追踪功图形态变化，识别供液不足、泵漏和空抽倾向。", "采油监控"),
          page("oil-dynagraph-diagnosis", "示功图智能诊断", "analysis", "根据功图形态、载荷变化和面积变化自动判断油井工况。", "采油监控"),
          page("oil-production-metering", "产液量计量分析", "analysis", "根据示功图有效面积、冲次、泵径和含水参数计算单井产液量。", "采油监控"),
          page("oil-dynagraph-alarm-rule", "功图报警规则", "config", "配置载荷范围、功图面积、产液量和开停井报警规则。", "采油监控"),
          page("oil-standard-card", "标准功图设置", "config", "选择标准示功图，设置载荷范围和产液量计算参数。", "采油监控"),
          page("oil-state-maintain", "油井运行状态维护", "form", "维护开井、关井、长停、作业、检修等状态变更记录。", "采油监控"),
          page("oil-tank", "单井拉油罐监控", "table", "监控拉油罐液位、库存、拉运计划和异常液位。", "采油监控"),
          page("oil-fluid", "动液面监控", "analysis", "跟踪动液面变化、沉没度和供液能力，支撑间抽控制。", "采油监控"),
        ],
      },
      {
        title: "注水监控",
        pages: [
          page("water-injection", "注水井监控", "table", "查看注水井压力、瞬时流量、日注水量和配注达标情况。", "注水监控"),
          page("water-realtime", "注水实时数据", "table", "展示注水井实时压力、流量、阀位、电参和通讯状态。", "注水监控"),
          page("water-history", "注水历史参数", "analysis", "分析注水趋势、压力波动和异常注水情况。", "注水监控"),
          page("water-standard", "配注达标分析", "analysis", "按井组和单井统计配注达标率、欠注和超注原因。", "注水监控"),
        ],
      },
      {
        title: "采气监控",
        pages: [
          page("gas-run", "气井运行监控", "table", "监控气井开关井、压力、产气量和安全联锁状态。", "采气监控"),
          page("gas-realtime", "气井实时数据", "table", "展示套压、油压、温度、瞬时流量和累计产气。", "采气监控"),
          page("gas-history", "气井历史参数", "analysis", "分析气井压力、产量和异常波动趋势。", "采气监控"),
        ],
      },
      {
        title: "集输监控",
        pages: [
          page("gather-station", "站库运行监控", "dashboard", "查看站库设备运行、液位、压力、输量和报警状态。", "集输监控"),
          page("gather-pipeline", "管线压力监控", "analysis", "监测集输管线压力、温度、流量和压降变化。", "集输监控"),
          page("gather-tank", "罐区液位监控", "table", "查看罐区液位、库存、进出液和高低液位报警。", "集输监控"),
          page("gather-pump", "输油泵监控", "table", "监控泵状态、电参、压力、流量和联锁保护。", "集输监控"),
        ],
      },
      {
        title: "视频监控",
        pages: [
          page("video-key", "重点井场视频", "map", "按井场查看重点视频点位、在线状态和轮巡画面。", "视频监控"),
          page("video-field", "井场视频监控", "table", "管理井场摄像头、抓拍记录、离线设备和云台控制。", "视频监控"),
          page("video-tour", "视频轮巡计划", "form", "配置视频轮巡路线、频率、责任人和异常处置规则。", "视频监控"),
        ],
      },
    ],
  },
  {
    id: "dynamic",
    label: "动态管理",
    icon: "D",
    groups: [
      {
        title: "岗位动态",
        pages: [
          page("shift-query", "上下班登记查询", "table", "查询岗位人员交接班、签到、签退和异常缺勤情况。", "岗位动态"),
          page("handover", "交接班管理", "form", "填写交接班内容、接收人员、短信通知和确认记录。", "岗位动态"),
          page("attendance", "人员考勤情况", "analysis", "统计班组人员出勤、迟到、缺勤和替班情况。", "岗位动态"),
          page("patrol-sign", "上下班补签", "form", "发起补签申请并提交班组长审批。", "岗位动态"),
          page("person-info", "人员信息维护", "form", "维护人员岗位、联系方式、所属单位和权限范围。", "岗位动态"),
          page("shift-schedule", "岗位排班维护", "table", "配置岗位排班、班次模板和轮班计划。", "岗位动态"),
        ],
      },
      {
        title: "生产组织",
        pages: [
          page("dispatch-log", "调度日志", "form", "记录生产调度指令、执行反馈和重要事项。", "生产组织"),
          page("key-work", "重点工作安排", "workflow", "分解重点工作、责任人、完成时限和闭环结果。", "生产组织"),
          page("meeting", "生产会议", "table", "管理会议议题、纪要、任务分解和落实跟踪。", "生产组织"),
          page("single-command", "单兵指挥", "map", "面向现场人员派发任务、定位协同和回传结果。", "生产组织"),
          page("operation-dynamic", "作业动态", "table", "跟踪检泵、修井、措施、关停恢复等作业动态。", "生产组织"),
          page("well-status", "油井当前状态", "table", "统一维护油井当前生产状态、原因和持续时长。", "生产组织"),
        ],
      },
    ],
  },
  {
    id: "collab",
    label: "协同管理",
    icon: "C",
    groups: [
      {
        title: "任务协同",
        pages: [
          page("work-order", "生产工单", "workflow", "从报警、诊断、巡检生成工单并跟踪处理闭环。", "任务协同"),
          page("strategy-order", "节能策略工单", "workflow", "对间抽策略、开停机建议发起执行和确认。", "任务协同"),
          page("diagnosis-order", "工况诊断工单", "workflow", "对功图异常、空抽倾向和设备隐患派发处置任务。", "任务协同"),
          page("approval-center", "审批中心", "table", "集中处理策略启用、参数调整和维护变更审批。", "任务协同"),
          page("sms-notice", "短信通知", "form", "向责任人发送报警、交接班和策略提醒短信。", "任务协同"),
        ],
      },
      {
        title: "知识协同",
        pages: [
          page("case-library", "故障案例库", "table", "沉淀典型油井故障、原因、功图特征和处理方案。", "知识协同"),
          page("model-feedback", "模型反馈管理", "form", "对模型识别结果进行人工复核、反馈和样本标注。", "知识协同"),
          page("document-center", "资料文档中心", "table", "管理制度、图纸、设备说明和操作手册。", "知识协同"),
          page("expert-consult", "专家会诊", "workflow", "发起多专业远程会诊，形成处置建议和结论。", "知识协同"),
          page("notice-board", "公告发布", "form", "发布生产通知、停电检修、巡检要求和安全提示。", "知识协同"),
        ],
      },
    ],
  },
  {
    id: "emergency",
    label: "应急处置",
    icon: "E",
    groups: [
      {
        title: "报警预警",
        pages: [
          page("alarm-disposal", "报警处置", "table", "确认、合并、派发和关闭各类生产报警。", "报警预警"),
          page("alarm-query", "报警查询", "table", "按单位、井号、类型、等级和时间查询报警记录。", "报警预警"),
          page("alarm-analysis", "报警统计分析", "analysis", "统计报警数量、来源、处置时长和重复报警。", "报警预警"),
          page("alarm-day", "报警日度统计", "report", "生成日度报警汇总、趋势和重点问题清单。", "报警预警"),
          page("alarm-supervise", "报警督导", "workflow", "对超时未处理、反复发生报警进行督办。", "报警预警"),
        ],
      },
      {
        title: "应急资源",
        pages: [
          page("plan-flow", "应急预案流程图", "workflow", "按突发事件类别查看处置流程、角色和节点。", "应急资源"),
          page("emergency-unit", "应急单位", "table", "维护应急单位、联系人、电话和服务范围。", "应急资源"),
          page("emergency-case", "应急案例", "table", "管理突发事件案例、处置过程和复盘结论。", "应急资源"),
          page("emergency-contact", "应急通讯录", "table", "维护应急联系人、班组、岗位和备用联系方式。", "应急资源"),
          page("emergency-team", "应急队伍", "table", "管理抢修队伍、值守状态、车辆和装备情况。", "应急资源"),
          page("material-inventory", "物资库存信息", "table", "查看应急物资库存、库房位置、有效期和领用记录。", "应急资源"),
        ],
      },
    ],
  },
  {
    id: "maintain",
    label: "维护配置",
    icon: "F",
    groups: [
      {
        title: "基础配置",
        pages: [
          page("company-base", "单位基础信息", "config", "维护单位、区块、井组、站库等组织基础信息。", "基础配置"),
          page("well-base", "单井基础信息", "config", "维护油井基础信息、井型、泵型、生产层位和设备归属。", "基础配置"),
          page("well-group", "井组基础信息", "config", "维护井组、区块、责任单位和管辖关系。", "基础配置"),
          page("station-base", "站库信息", "config", "维护站库、罐区、泵房、管线和计量点信息。", "基础配置"),
          page("device-base", "站库设备信息", "config", "维护站库设备台账、型号、参数和安装位置。", "基础配置"),
          page("pump-base", "泵基础信息", "config", "维护抽油泵、输油泵、电机、减速箱等设备档案。", "基础配置"),
          page("tank-base", "罐基础信息", "config", "维护储罐、拉油罐和液位计参数。", "基础配置"),
          page("meter-base", "流量计基础信息", "config", "维护流量计、压力表、电参仪和采集仪表。", "基础配置"),
        ],
      },
      {
        title: "设备运维",
        pages: [
          page("gateway", "网关设备监控", "table", "监控边缘网关在线状态、采集质量和断点续传。", "设备运维"),
          page("terminal", "智能测控终端", "table", "管理 DSP+MCU 测控单元、固件、策略版本和通信状态。", "设备运维"),
          page("firmware", "远程升级管理", "form", "创建远程升级任务，选择设备、固件包和执行窗口。", "设备运维"),
          page("parameter-template", "参数模板管理", "config", "配置采集频率、报警阈值、控制策略和设备参数模板。", "设备运维"),
          page("device-maintenance", "设备维护计划", "table", "制定设备巡检、保养、校准和检修计划。", "设备运维"),
          page("data-quality", "数据质量监控", "analysis", "分析缺测、漂移、离群、延迟和重复数据。", "设备运维"),
        ],
      },
      {
        title: "权限安全",
        pages: [
          page("role-auth", "角色权限配置", "config", "配置角色、菜单、数据范围和操作权限。", "权限安全"),
          page("user-auth", "用户账号管理", "table", "维护用户、岗位、手机号、单位和登录状态。", "权限安全"),
          page("audit-log", "操作审计日志", "table", "记录登录、参数修改、策略下发和数据导出行为。", "权限安全"),
        ],
      },
    ],
  },
  {
    id: "report",
    label: "报表管理",
    icon: "R",
    groups: [
      {
        title: "生产报表",
        pages: [
          page("oil-day-report", "采油日报", "report", "生成日度开井、产液、产油、含水、能耗和报警数据。", "生产报表"),
          page("oil-month-report", "采油月报", "report", "按月统计产量、开井率、能耗、策略执行和异常井。", "生产报表"),
          page("well-run-report", "油井运行报表", "report", "输出油井运行时长、启停次数、通讯质量和关键参数。", "生产报表"),
          page("dynagraph-report", "功图诊断报表", "report", "输出功图识别结果、异常类型、模型置信度和复核情况。", "生产报表"),
          page("fluid-report", "动液面分析报表", "report", "统计动液面、沉没度、供液能力和低产井变化。", "生产报表"),
        ],
      },
      {
        title: "节能报表",
        pages: [
          page("energy-day", "日耗电分析", "report", "按单井、井组和单位统计日耗电量与吨液耗电。", "节能报表"),
          page("energy-saving", "节能效果报表", "report", "对比策略执行前后耗电、开井时长和产量变化。", "节能报表"),
          page("pump-efficiency", "机采效率报表", "report", "统计系统效率、泵效、平衡合格率和优化建议。", "节能报表"),
          page("strategy-execute", "策略执行报表", "report", "输出开停机策略下发、执行、回退和人工确认记录。", "节能报表"),
        ],
      },
      {
        title: "管理报表",
        pages: [
          page("alarm-report", "报警处置报表", "report", "统计报警等级、来源、处置结果和闭环时长。", "管理报表"),
          page("maintenance-report", "设备维护报表", "report", "统计设备巡检、故障、维修、备件和停机影响。", "管理报表"),
          page("workorder-report", "工单闭环报表", "report", "统计工单来源、责任单位、完成率和超时情况。", "管理报表"),
          page("data-report", "数据质量报表", "report", "输出采集完整率、准确率、延迟率和异常数据明细。", "管理报表"),
        ],
      },
    ],
  },
  {
    id: "assessment",
    label: "评价考核",
    icon: "A",
    groups: [
      {
        title: "指标评价",
        pages: [
          page("unit-score", "单位指标考核", "analysis", "按单位评价产量、能耗、报警处置、数据质量和工单闭环。", "指标评价"),
          page("well-score", "单井运行评价", "analysis", "对单井运行效率、功图状态、能耗和异常频次进行评分。", "指标评价"),
          page("energy-score", "节能绩效评价", "analysis", "评价节能策略覆盖率、执行率、节电量和产量影响。", "指标评价"),
          page("alarm-score", "报警处置评价", "analysis", "评价报警确认时长、处置时长、重复报警和超时闭环。", "指标评价"),
          page("data-score", "数据质量评价", "analysis", "评价数据完整率、准确率、延迟率和仪表健康度。", "指标评价"),
        ],
      },
      {
        title: "考核配置",
        pages: [
          page("score-model", "评分模型配置", "config", "配置评分指标、权重、扣分规则和排名口径。", "考核配置"),
          page("score-template", "考核模板维护", "config", "维护单位、班组、岗位和单井维度的考核模板。", "考核配置"),
          page("score-result", "考核结果发布", "workflow", "生成考核结果，发起复核、确认和发布流程。", "考核配置"),
        ],
      },
    ],
  },
  {
    id: "morning",
    label: "生产晨报",
    icon: "B",
    groups: [
      {
        title: "晨报编制",
        pages: [
          page("morning-today", "今日生产晨报", "report", "自动汇总产量、开井、报警、作业、节能和重点井信息。", "晨报编制"),
          page("morning-edit", "晨报编辑", "form", "编辑晨报文字、重点问题、措施建议和附件。", "晨报编制"),
          page("morning-approve", "晨报审核", "workflow", "提交晨报审核、退回修改和发布确认。", "晨报编制"),
          page("morning-history", "历史晨报查询", "table", "查询历史晨报、导出附件和版本记录。", "晨报编制"),
          page("morning-template", "晨报模板配置", "config", "配置晨报指标、段落、图表和推送对象。", "晨报编制"),
          page("morning-push", "晨报推送记录", "table", "查看晨报推送对象、阅读状态和失败原因。", "晨报编制"),
        ],
      },
    ],
  },
];

let activeModuleId = "home";
let activePageId = "home-overview";
let openedTabs = ["home-overview"];

const allPages = modules.flatMap((mod) =>
  mod.groups.flatMap((group) => group.pages.map((item) => ({ ...item, moduleId: mod.id, moduleLabel: mod.label, groupTitle: group.title }))),
);

const pageMap = new Map(allPages.map((item) => [item.id, item]));

const dynagraphCases = [
  {
    id: "normal",
    title: "正常供液",
    well: "ST2-0-178",
    time: "2026-06-22 10:00:12",
    status: "正常生产",
    alarm: "无报警",
    level: "good",
    xMax: 4.0,
    yMax: 83.1,
    stroke: 3.63,
    frequency: 3.52,
    minLoad: 32.4,
    maxLoad: 63.9,
    area: 100.2,
    rodPower: 5.8,
    production: 13.6,
    efficiency: 82,
    confidence: 96,
    advice: "保持当前冲次与开井制度，持续跟踪载荷变化。",
    points: [
      [0.28, 54.6],
      [0.42, 59.1],
      [0.82, 61.0],
      [1.48, 62.4],
      [2.26, 63.1],
      [3.02, 62.2],
      [3.50, 58.8],
      [3.66, 50.4],
      [3.45, 42.5],
      [2.92, 35.6],
      [2.10, 32.9],
      [1.22, 33.4],
      [0.52, 36.5],
      [0.28, 43.6],
      [0.28, 54.6],
    ],
  },
  {
    id: "insufficient",
    title: "供液不足",
    well: "ST2-1-177",
    time: "2026-06-22 09:00:15",
    status: "低效生产",
    alarm: "供液不足报警",
    level: "warn",
    xMax: 5.0,
    yMax: 85.8,
    stroke: 4.12,
    frequency: 3.19,
    minLoad: 33.5,
    maxLoad: 66.0,
    area: 92.6,
    rodPower: 4.9,
    production: 5.8,
    efficiency: 46,
    confidence: 92,
    advice: "建议降低冲次或执行间抽策略，观察动液面恢复情况。",
    points: [
      [0.32, 39.6],
      [0.72, 58.2],
      [1.28, 62.0],
      [2.15, 61.4],
      [3.08, 59.6],
      [4.18, 52.2],
      [3.62, 42.8],
      [2.82, 37.2],
      [1.82, 33.8],
      [0.82, 37.0],
      [0.32, 39.6],
    ],
  },
  {
    id: "intermittent",
    title: "间歇供液",
    well: "STS3XR19",
    time: "2026-06-22 08:00:23",
    status: "波动生产",
    alarm: "间歇供液预警",
    level: "warn",
    xMax: 4.0,
    yMax: 82.4,
    stroke: 3.63,
    frequency: 3.52,
    minLoad: 32.7,
    maxLoad: 63.9,
    area: 98.5,
    rodPower: 5.8,
    production: 7.4,
    efficiency: 54,
    confidence: 89,
    advice: "建议启用自适应开停机策略，按供液恢复曲线优化间抽周期。",
    points: [
      [0.30, 55.0],
      [0.55, 59.4],
      [1.15, 61.0],
      [1.82, 62.3],
      [2.58, 61.2],
      [3.28, 57.6],
      [3.56, 49.2],
      [3.36, 40.2],
      [2.72, 34.8],
      [2.10, 33.5],
      [1.52, 37.5],
      [1.00, 31.8],
      [0.48, 35.5],
      [0.30, 44.2],
      [0.30, 55.0],
    ],
  },
  {
    id: "rod-broken",
    title: "抽油杆断脱",
    well: "NHT82-5X4",
    time: "2026-06-22 07:00:11",
    status: "停井风险",
    alarm: "载荷突降报警",
    level: "bad",
    xMax: 5.0,
    yMax: 87.6,
    stroke: 4.94,
    frequency: 1.95,
    minLoad: 33.4,
    maxLoad: 67.4,
    area: 19.8,
    rodPower: 3.9,
    production: 0.4,
    efficiency: 8,
    confidence: 95,
    advice: "建议立即停井核查，现场憋压验证并安排检修作业。",
    points: [
      [0.35, 35.0],
      [0.85, 36.2],
      [1.45, 35.0],
      [2.10, 36.0],
      [2.55, 34.4],
      [3.05, 35.6],
      [3.58, 34.8],
      [4.12, 36.6],
      [4.36, 33.8],
      [3.90, 32.8],
      [2.62, 32.2],
      [1.40, 33.0],
      [0.35, 35.0],
    ],
  },
  {
    id: "pump-leak",
    title: "泵漏",
    well: "S3-8-15",
    time: "2026-06-22 10:00:20",
    status: "异常生产",
    alarm: "泵效下降报警",
    level: "bad",
    xMax: 5.0,
    yMax: 83.3,
    stroke: 4.82,
    frequency: 2.73,
    minLoad: 33.1,
    maxLoad: 64.1,
    area: 93.0,
    rodPower: 4.2,
    production: 3.6,
    efficiency: 31,
    confidence: 91,
    advice: "建议核查泵阀密封，必要时碰泵、洗井或安排检泵。",
    points: [
      [0.28, 38.5],
      [0.62, 54.8],
      [1.25, 60.6],
      [2.10, 61.8],
      [3.10, 61.0],
      [4.62, 58.5],
      [4.38, 52.0],
      [3.85, 47.2],
      [3.28, 34.5],
      [2.72, 30.8],
      [2.18, 36.5],
      [1.58, 31.5],
      [1.08, 35.8],
      [0.55, 33.2],
      [0.28, 38.5],
    ],
  },
  {
    id: "pump-collision",
    title: "碰泵",
    well: "STT128X46",
    time: "2026-06-22 09:00:49",
    status: "设备冲击",
    alarm: "下死点冲击报警",
    level: "bad",
    xMax: 5.0,
    yMax: 105.0,
    stroke: 4.16,
    frequency: 2.18,
    minLoad: 40.6,
    maxLoad: 80.8,
    area: 128.1,
    rodPower: 4.6,
    production: 6.1,
    efficiency: 49,
    confidence: 88,
    advice: "建议调整防冲距和冲程参数，复核抽油杆柱及泵深。",
    points: [
      [0.22, 41.8],
      [0.78, 72.4],
      [1.65, 78.0],
      [2.62, 78.5],
      [3.48, 76.8],
      [4.12, 72.0],
      [3.52, 40.2],
      [2.66, 39.8],
      [1.58, 40.6],
      [0.78, 39.8],
      [0.22, 41.8],
    ],
  },
  {
    id: "hanger-offset",
    title: "悬绳器位置不正",
    well: "ST2-2-162",
    time: "2026-06-22 10:00:28",
    status: "机械异常",
    alarm: "上行末端载荷异常",
    level: "warn",
    xMax: 5.0,
    yMax: 87.6,
    stroke: 4.94,
    frequency: 1.95,
    minLoad: 33.4,
    maxLoad: 67.4,
    area: 119.8,
    rodPower: 3.9,
    production: 8.9,
    efficiency: 61,
    confidence: 86,
    advice: "建议现场检查悬绳器与驴头位置，排除上死点挤压。",
    points: [
      [0.24, 35.5],
      [0.66, 63.0],
      [1.12, 68.2],
      [1.72, 65.5],
      [2.28, 66.8],
      [2.86, 64.0],
      [3.28, 66.4],
      [3.78, 61.5],
      [4.42, 65.2],
      [4.05, 34.8],
      [3.62, 38.6],
      [3.12, 34.2],
      [2.40, 35.8],
      [1.55, 34.1],
      [0.72, 36.0],
      [0.24, 35.5],
    ],
  },
];

const dynagraphMap = new Map(dynagraphCases.map((item) => [item.id, item]));

function $(selector) {
  return document.querySelector(selector);
}

function metricSet(item) {
  const seed = item.id.split("").reduce((sum, char) => sum + char.charCodeAt(0), 0);
  return [
    { label: "监控对象", value: `${(seed % 43) + 18}`, unit: "个", note: "在线接入" },
    { label: "今日告警", value: `${seed % 17}`, unit: "条", note: "待确认" },
    { label: "数据完整率", value: `${92 + (seed % 7)}.${seed % 10}`, unit: "%", note: "实时采集" },
    { label: "策略执行率", value: `${84 + (seed % 13)}.${(seed + 3) % 10}`, unit: "%", note: "闭环统计" },
  ];
}

function chartValues(item) {
  const seed = item.id.length + item.title.length;
  return Array.from({ length: 10 }, (_, index) => 28 + ((seed * (index + 3) + index * 11) % 68));
}

function makeRows(item, count = 8) {
  return Array.from({ length: count }, (_, index) => {
    const seed = item.id.length * 17 + index * 9;
    const well = wellNames[index % wellNames.length];
    return {
      index: index + 1,
      unit: units[index % units.length],
      well,
      status: ["正常", "待确认", "已处置"][index % 3],
      time: `2026-06-${String(10 + index).padStart(2, "0")} ${String(8 + index).padStart(2, "0")}:30`,
      valueA: (seed % 95) + 12,
      valueB: ((seed * 1.37) % 56).toFixed(2),
      valueC: ((seed * 2.11) % 38).toFixed(2),
      owner: ["张工", "李工", "王工", "刘工"][index % 4],
      remark: `${item.title}运行记录 ${String(index + 1).padStart(2, "0")}`,
    };
  });
}

function columnsFor(item) {
  if (item.type === "report") {
    return ["序号", "单位", "报表名称", "统计口径", "生成时间", "审核状态", "操作"];
  }
  if (item.type === "workflow") {
    return ["序号", "流程编号", "流程类型", "发起人", "当前节点", "处理状态", "发起时间"];
  }
  if (item.type === "form" || item.type === "config") {
    return ["序号", "配置项", "所属单位", "参数值", "维护人", "更新时间", "状态"];
  }
  if (item.type === "analysis") {
    return ["序号", "对象", "分析指标", "当前值", "对比值", "趋势", "建议"];
  }
  return ["序号", "单位", "井号/设备", "运行状态", "采集时间", "电参", "产量/流量", "处置人"];
}

function rowCells(item, row) {
  if (item.type === "report") {
    return [row.index, row.unit, item.title, "日度 / 月度", row.time, statusCell(row.index % 2 ? "已生成" : "待审核"), "查看 导出"];
  }
  if (item.type === "workflow") {
    return [row.index, `LC-${item.id.toUpperCase()}-${1000 + row.index}`, item.title, row.owner, ["发起", "处理中", "复核", "关闭"][row.index % 4], statusCell(row.status), row.time];
  }
  if (item.type === "form" || item.type === "config") {
    return [row.index, `${item.title}参数${row.index}`, row.unit, row.valueA, row.owner, row.time, statusCell(row.status)];
  }
  if (item.type === "analysis") {
    return [row.index, row.well, item.title, row.valueB, row.valueC, row.index % 2 ? "上升" : "下降", "建议复核策略"];
  }
  return [row.index, row.unit, row.well, statusCell(row.status), row.time, row.valueA, row.valueB, row.owner];
}

function statusCell(text) {
  const cls = text.includes("正常") || text.includes("已") ? "good" : text.includes("待") ? "warn" : "bad";
  return `<span class="status ${cls}">${text}</span>`;
}

function renderTopNav() {
  $("#topNav").innerHTML = modules
    .map(
      (mod) => `
        <button type="button" class="${mod.id === activeModuleId ? "active" : ""}" data-module="${mod.id}">
          <span class="top-icon">${mod.icon}</span>${mod.label}
        </button>
      `,
    )
    .join("");
}

function renderSidebar() {
  const mod = modules.find((item) => item.id === activeModuleId);
  $("#sidebar").innerHTML = mod.groups
    .map(
      (group) => `
        <section class="menu-group">
          <div class="menu-title"><span>${group.title}</span><b class="arrow">⌄</b></div>
          ${group.pages
            .map(
              (item) => `
                <button class="menu-item ${item.id === activePageId ? "active" : ""}" type="button" data-page="${item.id}">
                  <span class="menu-icon">${item.title.slice(0, 1)}</span>
                  <span class="menu-label">${item.title}</span>
                </button>
              `,
            )
            .join("")}
        </section>
      `,
    )
    .join("");
}

function renderTabs() {
  $("#tabs").innerHTML = openedTabs
    .map((id) => {
      const item = pageMap.get(id);
      return `
        <button class="tab ${id === activePageId ? "active" : ""}" type="button" data-tab="${id}">
          <span>${item.title}</span>
          ${id === "home-overview" ? "" : `<span class="close" data-close="${id}">×</span>`}
        </button>
      `;
    })
    .join("");
}

function renderWorkspace() {
  const item = pageMap.get(activePageId);
  const metrics = metricSet(item);
  $("#workspace").innerHTML = `
    <section class="page-head">
      <div>
        <h1>${item.title}</h1>
        <div class="breadcrumb">${item.moduleLabel} / ${item.groupTitle} / ${item.title}</div>
      </div>
      <div class="head-actions">
        <button class="btn">刷新</button>
        <button class="btn primary">查询</button>
        <button class="btn success">导出</button>
      </div>
    </section>
    ${item.id === "home-overview" ? "" : renderMetricGrid(metrics)}
    ${renderPageBody(item)}
  `;
}

function renderMetricGrid(metrics) {
  return `
    <section class="metric-grid">
      ${metrics
        .map(
          (metric) => `
            <article class="metric-card">
              <span>${metric.label}</span>
              <strong>${metric.value}<small>${metric.unit}</small></strong>
              <small>${metric.note}</small>
            </article>
          `,
        )
        .join("")}
    </section>
  `;
}

function renderPageBody(item) {
  if (item.id === "home-overview") return renderHomeDashboard(item);
  if (item.id === "oil-realtime") return renderOilRealtime(item);
  if (item.id === "oil-dynagraph") return renderDynagraphData(item);
  if (item.id === "oil-dynagraph-track") return renderDynagraphTrack(item);
  if (item.id === "oil-dynagraph-diagnosis") return renderDynagraphDiagnosis(item);
  if (item.id === "oil-production-metering") return renderProductionMetering(item);
  if (item.id === "oil-dynagraph-alarm-rule" || item.id === "oil-standard-card") return renderDynagraphConfig(item);
  if (item.type === "dashboard") return renderDashboard(item);
  if (item.type === "map") return renderMap(item);
  if (item.type === "workflow") return renderWorkflow(item);
  if (item.type === "form") return renderForm(item);
  if (item.type === "config") return renderConfig(item);
  if (item.type === "analysis") return renderAnalysis(item);
  if (item.type === "report") return renderReport(item);
  return renderTablePage(item);
}

function renderFilters(item) {
  return `
    <section class="panel">
      <div class="panel-body">
        <div class="filter-row">
          <div class="field"><label>单位</label><select><option>全部单位</option><option>东营采油一区</option><option>滨海低产井区</option></select></div>
          <div class="field"><label>对象</label><input value="${item.domain}" /></div>
          <div class="field"><label>开始时间</label><input value="2026-06-01 00:00" /></div>
          <div class="field"><label>结束时间</label><input value="2026-06-22 23:59" /></div>
          <div class="field"><label>状态</label><select><option>全部</option><option>正常</option><option>待确认</option><option>已处置</option></select></div>
          <div><button class="btn primary">查询</button></div>
        </div>
      </div>
    </section>
  `;
}

function renderTable(item, count = 8) {
  const cols = columnsFor(item);
  const rows = makeRows(item, count);
  return `
    <table class="data-table">
      <thead><tr>${cols.map((col) => `<th>${col}</th>`).join("")}</tr></thead>
      <tbody>
        ${rows.map((row) => `<tr>${rowCells(item, row).map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}
      </tbody>
    </table>
  `;
}

function renderTablePage(item) {
  return `
    ${renderFilters(item)}
    <section class="panel">
      <div class="panel-title"><span>${item.title}列表</span><small>${item.desc}</small></div>
      <div class="panel-body">${renderTable(item, 10)}</div>
    </section>
  `;
}

function svgPoint(value, index, values, box, min, max) {
  const x = box.left + (index / Math.max(values.length - 1, 1)) * box.width;
  const y = box.top + box.height - ((value - min) / (max - min || 1)) * box.height;
  return [x, y];
}

function renderAxisTicks({ box, min = 0, max = 100, xLabels = [], yTicks = 5, unit = "" }) {
  const yTickValues = Array.from({ length: yTicks + 1 }, (_, index) => min + ((max - min) / yTicks) * index);
  const xStep = Math.max(1, Math.ceil(xLabels.length / 7));
  return `
    ${yTickValues
      .map((tick) => {
        const y = box.top + box.height - ((tick - min) / (max - min || 1)) * box.height;
        return `<path class="chart-grid-line" d="M${box.left} ${y.toFixed(1)} H${box.left + box.width}" /><text class="chart-tick" x="${box.left - 10}" y="${(y + 4).toFixed(1)}" text-anchor="end">${formatTick(tick)}</text>`;
      })
      .join("")}
    ${xLabels
      .map((label, index) => {
        if (index % xStep !== 0 && index !== xLabels.length - 1) return "";
        const x = box.left + (index / Math.max(xLabels.length - 1, 1)) * box.width;
        return `<text class="chart-tick" x="${x.toFixed(1)}" y="${box.top + box.height + 24}" text-anchor="middle">${label}</text>`;
      })
      .join("")}
    <path class="chart-axis" d="M${box.left} ${box.top} V${box.top + box.height} H${box.left + box.width}" />
    <text class="chart-unit" x="${box.left}" y="${box.top - 10}">${unit}</text>
  `;
}

function renderLineChart({ xLabels, series, min = 0, max = 100, unit = "", height = 280 }) {
  const box = { left: 58, top: 30, width: 610, height: height - 82 };
  const colors = ["#6b9cff", "#43d2a5", "#ff5c5c", "#ff9f2f"];
  return `
    <svg class="svg-chart line-chart" viewBox="0 0 700 ${height}" role="img" aria-label="趋势图">
      ${renderAxisTicks({ box, min, max, xLabels, unit })}
      ${series
        .map((serie, sIndex) => {
          const points = serie.values.map((value, index) => svgPoint(value, index, serie.values, box, min, max));
          const d = points.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
          const color = serie.color || colors[sIndex % colors.length];
          return `
            <path class="chart-line" d="${d}" stroke="${color}" />
            ${points.map(([x, y]) => `<circle class="chart-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.5" fill="${color}" />`).join("")}
          `;
        })
        .join("")}
      <g class="chart-legend" transform="translate(${box.left + box.width - 230}, 12)">
        ${series
          .map((serie, index) => `<circle cx="${index * 76}" cy="0" r="5" fill="${serie.color || colors[index % colors.length]}" /><text x="${index * 76 + 9}" y="4">${serie.name}</text>`)
          .join("")}
      </g>
    </svg>
  `;
}

function renderBarChart({ labels, values, max = 100, unit = "", threshold = null, color = "#2368df" }) {
  const box = { left: 56, top: 26, width: 610, height: 210 };
  const barWidth = Math.max(16, box.width / values.length - 18);
  const min = 0;
  return `
    <svg class="svg-chart bar-chart" viewBox="0 0 700 290" role="img" aria-label="柱状图">
      ${renderAxisTicks({ box, min, max, xLabels: labels, unit })}
      ${
        threshold === null
          ? ""
          : (() => {
              const y = box.top + box.height - ((threshold - min) / (max - min || 1)) * box.height;
              return `<path class="threshold-line" d="M${box.left} ${y.toFixed(1)} H${box.left + box.width}" /><text class="threshold-label" x="${box.left + box.width - 8}" y="${(y - 6).toFixed(1)}" text-anchor="end">阈值 ${threshold}</text>`;
            })()
      }
      ${values
        .map((value, index) => {
          const x = box.left + (index + 0.5) * (box.width / values.length) - barWidth / 2;
          const h = ((value - min) / (max - min || 1)) * box.height;
          const y = box.top + box.height - h;
          const fill = index % 3 === 1 ? "#43d2a5" : index % 5 === 0 ? "#ff7b7b" : color;
          return `<rect class="chart-bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${h.toFixed(1)}" fill="${fill}" /><text class="bar-value" x="${(x + barWidth / 2).toFixed(1)}" y="${(y - 6).toFixed(1)}" text-anchor="middle">${formatTick(value)}</text>`;
        })
        .join("")}
    </svg>
  `;
}

function renderGaugePanel() {
  return `
    <div class="production-gauge">
      <div class="gauge-metric top-left"><strong>2,486</strong><span>日产液量 t</span><small>比昨：-14.10↓</small></div>
      <div class="gauge-metric top-right"><strong>824</strong><span>日产油量 t</span><small class="up">比昨：34.01↑</small></div>
      <div class="gauge-metric bottom-left"><strong>18.6</strong><span>日耗电量 万kW·h</span><small>比昨：-8.58↓</small></div>
      <div class="gauge-metric bottom-right"><strong>39.8%</strong><span>综合含水</span><small>比昨：0.00</small></div>
      <div class="gauge-wheel">
        <span>日产液量</span>
        <span>日产油量</span>
        <b>抽油机</b>
        <span>日耗电量</span>
        <span>综合含水</span>
      </div>
    </div>
  `;
}

function renderOperationTiles() {
  const tiles = [
    ["日", "今日交井", "18", "blue"],
    ["月", "月交井", "126", "purple"],
    ["年", "年交井", "689", "cyan"],
    ["待", "待作业", "12", "red"],
    ["正", "正作业", "8", "orange"],
    ["开", "今日开井", "42", "green"],
  ];
  return `<div class="operation-tiles">${tiles.map(([icon, title, value, tone]) => `<div class="operation-tile ${tone}"><b>${icon}</b><span>${title}</span><strong>${value}</strong></div>`).join("")}</div>`;
}

function renderHomeDashboard(item) {
  const days = ["05-12", "05-15", "05-18", "05-21", "05-24", "05-27", "05-30", "06-02", "06-05", "06-08", "06-11"];
  const productionDays = ["05-12", "05-16", "05-20", "05-24", "05-28", "06-01", "06-05", "06-09"];
  return `
    <section class="home-layout">
      <article class="panel home-card">
        <div class="panel-title"><span>产量运行</span></div>
        <div class="panel-body">${renderGaugePanel()}</div>
      </article>
      <article class="panel home-card home-trend">
        <div class="panel-title"><span>生产趋势</span><small>日度 / 月度切换</small></div>
        <div class="panel-body">
          <div class="well-summary">
            <div><b>油井</b><span>总井 368 口 / 开井 292 口</span></div>
            <div><b>水井</b><span>总井 118 口 / 开井 91 口</span></div>
            <div><b>气井</b><span>总井 42 口 / 开井 36 口</span></div>
          </div>
          ${renderLineChart({
            xLabels: days,
            min: 280,
            max: 1010,
            unit: "井数/产量/开井率",
            height: 330,
            series: [
              { name: "油井开井数", color: "#6b9cff", values: [288, 286, 292, 291, 289, 293, 297, 300, 298, 301, 299] },
              { name: "日产油量", color: "#43d2a5", values: [705, 740, 890, 870, 835, 865, 880, 842, 920, 935, 910] },
              { name: "开井率", color: "#ff5c5c", values: [392, 398, 405, 412, 402, 410, 418, 426, 435, 448, 440] },
            ],
          })}
        </div>
      </article>
      <article class="panel home-card">
        <div class="panel-title"><span>作业动态</span></div>
        <div class="panel-body">${renderOperationTiles()}</div>
      </article>
      <article class="panel home-card">
        <div class="panel-title"><span>报警统计</span><small>今日报警 210，今日处置 200</small></div>
        <div class="panel-body">
          ${renderLineChart({
            xLabels: productionDays,
            min: 0,
            max: 620,
            unit: "条",
            height: 250,
            series: [
              { name: "报警数", color: "#6b9cff", values: [360, 280, 240, 245, 232, 246, 530, 210] },
              { name: "处置数", color: "#43d2a5", values: [250, 180, 450, 240, 260, 230, 260, 200] },
            ],
          })}
        </div>
      </article>
      <article class="panel home-card">
        <div class="panel-title"><span>重点视频</span></div>
        <div class="panel-body"><div class="video-icons"><span>重点站井</span><span>重点风险</span><span>重点施工</span><span>重点作业</span></div></div>
      </article>
      <article class="panel home-card">
        <div class="panel-title"><span>油井动态</span></div>
        <div class="panel-body"><div class="well-dynamic"><span>新井开井<b>7口</b></span><span>措施井开井<b>13口</b></span><span>关停井<b>5口</b></span><span>新井产量<b>42t</b></span><span>措施井产量<b>96t</b></span><span>影响产量<b>18t</b></span></div></div>
      </article>
      <article class="panel home-card home-index">
        <div class="panel-title"><span>指标运行</span><small>抽油机泵效</small></div>
        <div class="panel-body">${renderBarChart({ labels: ["抽油机泵效"], values: [20.4], max: 100, unit: "%", threshold: 20 })}</div>
      </article>
      <article class="panel home-card">
        <div class="panel-title"><span>重点监控</span><small>产油 Top5</small></div>
        <div class="panel-body">${renderTopTable()}</div>
      </article>
    </section>
  `;
}

function renderTopTable() {
  const rows = [
    ["HN-YJ-018", "东营采油一区", "15.21"],
    ["HN-YJ-031", "东营采油一区", "11.30"],
    ["HN-YJ-045", "滨海低产井区", "8.68"],
    ["HN-YJ-052", "胜北试验井区", "8.14"],
    ["HN-YJ-006", "东营采油二区", "7.22"],
  ];
  return `
    <table class="data-table compact-table">
      <thead><tr><th>井号</th><th>单位</th><th>产油/t</th></tr></thead>
      <tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody>
    </table>
  `;
}

function renderDashboard(item) {
  return `
    <section class="dashboard-grid">
      <article class="panel">
        <div class="panel-title"><span>产量运行</span></div>
        <div class="panel-body ring-board">
          <div class="ring"><strong>${92 + (item.id.length % 6)}%</strong><span>运行健康度</span></div>
        </div>
      </article>
      <article class="panel wide">
        <div class="panel-title"><span>趋势监控</span><small>${item.desc}</small></div>
        <div class="panel-body">${renderChart(item)}</div>
      </article>
      <article class="panel">
        <div class="panel-title"><span>作业动态</span></div>
        <div class="panel-body">${renderWorkCards(item)}</div>
      </article>
      <article class="panel wide">
        <div class="panel-title"><span>重点对象</span></div>
        <div class="panel-body">${renderTable(item, 7)}</div>
      </article>
    </section>
  `;
}

function renderChart(item) {
  const labels = ["05-12", "05-16", "05-20", "05-24", "05-28", "06-01", "06-05", "06-09"];
  const values = chartValues(item).slice(0, labels.length);
  return `
    <div class="chart">
      ${renderBarChart({ labels, values, max: 110, unit: item.type === "report" ? "t / kW·h" : "% / 次", threshold: 20 })}
    </div>
  `;
}

function renderWorkCards(item) {
  const cards = [
    ["今日开井", "实时统计开井、关井、长停和通讯状态。"],
    ["报警处置", "异常事件自动分级，支持确认、派发和关闭。"],
    ["策略执行", "展示开停机策略下发、反馈和回退状态。"],
    ["重点监控", "跟踪重点井、重点设备和重点作业。"],
  ];
  return `<div class="work-card-grid">${cards.map(([title, text]) => `<div class="work-card"><strong>${title}</strong><p>${text}</p></div>`).join("")}</div>`;
}

function renderMap(item) {
  return `
    ${renderFilters(item)}
    <section class="panel">
      <div class="panel-title"><span>井场分布与状态</span><small>${item.desc}</small></div>
      <div class="panel-body">
        <div class="work-card-grid">
          ${wellNames
            .map(
              (well, index) => `
                <article class="work-card">
                  <strong>${well}</strong>
                  <p>${units[index % units.length]}</p>
                  <p>通讯：${index % 4 === 0 ? "离线待查" : "在线"}；策略：${index % 2 ? "自动" : "人工确认"}</p>
                </article>
              `,
            )
            .join("")}
        </div>
      </div>
    </section>
  `;
}

function renderAnalysis(item) {
  return `
    ${renderFilters(item)}
    <section class="dashboard-grid">
      <article class="panel wide">
        <div class="panel-title"><span>${item.title}趋势</span><small>${item.desc}</small></div>
        <div class="panel-body">${renderChart(item)}</div>
      </article>
      <article class="panel">
        <div class="panel-title"><span>模型解释</span></div>
        <div class="panel-body">
          <div class="work-card"><strong>主要结论</strong><p>系统根据运行曲线、功图特征和历史样本输出诊断结果，并给出操作建议。</p></div>
        </div>
      </article>
      <article class="panel wide">
        <div class="panel-title"><span>分析明细</span></div>
        <div class="panel-body">${renderTable(item, 9)}</div>
      </article>
    </section>
  `;
}

function renderForm(item) {
  return `
    <section class="panel">
      <div class="panel-title"><span>${item.title}</span><small>${item.desc}</small></div>
      <div class="panel-body">
        <div class="form-grid">
          <div class="field"><label>所属单位</label><select><option>东营采油一区</option><option>滨海低产井区</option></select></div>
          <div class="field"><label>业务对象</label><input value="${item.domain}" /></div>
          <div class="field"><label>责任人</label><input value="值班工程师" /></div>
          <div class="field"><label>联系电话</label><input value="0546-0000000" /></div>
          <div class="field"><label>处理级别</label><select><option>普通</option><option>重要</option><option>紧急</option></select></div>
          <div class="field"><label>计划时间</label><input value="2026-06-22 10:00" /></div>
          <div class="field full"><label>内容说明</label><textarea>${item.desc}</textarea></div>
        </div>
        <div class="head-actions" style="margin-top:16px"><button class="btn primary">保存</button><button class="btn success">提交</button><button class="btn">重置</button></div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-title"><span>最近记录</span></div>
      <div class="panel-body">${renderTable(item, 6)}</div>
    </section>
  `;
}

function renderWorkflow(item) {
  const steps = ["发起", "确认", "派发", "执行", "关闭"];
  return `
    <section class="panel">
      <div class="panel-title"><span>${item.title}流程</span><small>${item.desc}</small></div>
      <div class="panel-body">
        <div class="workflow">
          ${steps.map((step, index) => `<div class="workflow-step"><strong>${index + 1}. ${step}</strong><span>${item.title}节点处理</span></div>`).join("")}
        </div>
      </div>
    </section>
    <section class="two-col">
      <div class="panel">
        <div class="panel-title"><span>流程目录</span></div>
        <div class="panel-body"><div class="tree-box">
          <div class="tree-node active">${item.title}</div>
          <div class="tree-node">待处理</div>
          <div class="tree-node">处理中</div>
          <div class="tree-node">已关闭</div>
          <div class="tree-node">超时督办</div>
        </div></div>
      </div>
      <div class="panel">
        <div class="panel-title"><span>流程台账</span></div>
        <div class="panel-body">${renderTable(item, 9)}</div>
      </div>
    </section>
  `;
}

function renderConfig(item) {
  return `
    <section class="two-col">
      <div class="panel">
        <div class="panel-title"><span>配置目录</span></div>
        <div class="panel-body"><div class="tree-box">
          <div class="tree-node active">${item.domain}</div>
          <div class="tree-node">组织关系</div>
          <div class="tree-node">设备档案</div>
          <div class="tree-node">参数模板</div>
          <div class="tree-node">权限范围</div>
        </div></div>
      </div>
      <div>
        ${renderForm(item)}
      </div>
    </section>
  `;
}

function renderReport(item) {
  return `
    ${renderFilters(item)}
    <section class="dashboard-grid">
      <article class="panel wide">
        <div class="panel-title"><span>${item.title}图表</span><small>${item.desc}</small></div>
        <div class="panel-body">${renderChart(item)}</div>
      </article>
      <article class="panel">
        <div class="panel-title"><span>报表操作</span></div>
        <div class="panel-body">
          <div class="work-card"><strong>自动生成</strong><p>支持定时生成、人工重算、导出和推送。</p></div>
        </div>
      </article>
      <article class="panel wide">
        <div class="panel-title"><span>报表明细</span></div>
        <div class="panel-body">${renderTable(item, 8)}</div>
      </article>
    </section>
  `;
}

function scalePoint(point, sample) {
  const [x, y] = point;
  const plot = { left: 52, top: 38, width: 228, height: 148 };
  return [
    plot.left + (x / sample.xMax) * plot.width,
    plot.top + plot.height - (y / sample.yMax) * plot.height,
  ];
}

function dynagraphPath(points, sample) {
  return points
    .map((point, index) => {
      const [x, y] = scalePoint(point, sample);
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}

function tickValues(max, count = 5) {
  return Array.from({ length: count + 1 }, (_, index) => (max / count) * index);
}

function formatTick(value) {
  if (Math.abs(value - Math.round(value)) < 0.05) return String(Math.round(value));
  return value.toFixed(1);
}

function renderDynagraphSvg(sample, options = {}) {
  const stroke = options.stroke || "#168a2f";
  const secondary = options.secondary ? dynagraphMap.get(options.secondary) : null;
  const plot = { left: 52, top: 38, width: 228, height: 148 };
  const xTicks = tickValues(sample.xMax, sample.xMax >= 5 ? 5 : 4);
  const yTicks = tickValues(sample.yMax, 5);
  return `
    <svg class="dynagraph-svg" viewBox="0 0 340 270" role="img" aria-label="${sample.title}示功图">
      <text x="170" y="17" class="dynagraph-title">${sample.well} 地面示功图</text>
      <text x="170" y="32" class="dynagraph-time">${sample.time}</text>
      <rect x="${plot.left}" y="${plot.top}" width="${plot.width}" height="${plot.height}" fill="#fff" stroke="#6f7c8a" />
      ${xTicks
        .map((tick) => {
          const x = plot.left + (tick / sample.xMax) * plot.width;
          return `<path d="M${x.toFixed(1)} ${plot.top} V${plot.top + plot.height}" class="grid-line" /><text x="${x.toFixed(1)}" y="${plot.top + plot.height + 16}" class="tick-label" text-anchor="middle">${formatTick(tick)}</text>`;
        })
        .join("")}
      ${yTicks
        .map((tick) => {
          const y = plot.top + plot.height - (tick / sample.yMax) * plot.height;
          return `<path d="M${plot.left} ${y.toFixed(1)} H${plot.left + plot.width}" class="grid-line" /><text x="${plot.left - 8}" y="${(y + 4).toFixed(1)}" class="tick-label" text-anchor="end">${formatTick(tick)}</text>`;
        })
        .join("")}
      <path d="M${plot.left} ${plot.top + plot.height} H${plot.left + plot.width} M${plot.left} ${plot.top} V${plot.top + plot.height}" fill="none" stroke="#1f2f46" stroke-width="1.2" />
      <text x="${plot.left + 6}" y="${plot.top - 6}" class="axis-label">载荷(kN)</text>
      <text x="${plot.left + plot.width - 2}" y="${plot.top + plot.height + 32}" class="axis-label" text-anchor="end">位移(m)</text>
      ${secondary ? `<path d="${dynagraphPath(secondary.points, sample)}" fill="none" stroke="#8796a8" stroke-width="1.6" stroke-dasharray="4 4" vector-effect="non-scaling-stroke" />` : ""}
      <path d="${dynagraphPath(sample.points, sample)}" fill="none" stroke="${stroke}" stroke-width="2.4" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
      ${sample.points.map((point) => {
        const [x, y] = scalePoint(point, sample);
        return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.7" fill="${stroke}" />`;
      }).join("")}
      <text x="52" y="226" class="stat-label">冲程：${sample.stroke} (米)</text>
      <text x="176" y="226" class="stat-label">冲次：${sample.frequency} (次)</text>
      <text x="52" y="242" class="stat-label">最小载荷：${sample.minLoad} (kN)</text>
      <text x="176" y="242" class="stat-label">最大载荷：${sample.maxLoad} (kN)</text>
      <text x="52" y="258" class="stat-label">功图面积：${sample.area}</text>
      <text x="176" y="258" class="stat-label">光杆功率：${sample.rodPower}</text>
    </svg>
  `;
}

function renderDynagraphCard(sample, compact = false) {
  return `
    <article class="dynagraph-card">
      <div class="dynagraph-head">
        <strong>${sample.title}</strong>
        <span class="status ${sample.level}">${sample.alarm}</span>
      </div>
      ${renderDynagraphSvg(sample, { secondary: compact ? null : "normal", suffix: compact ? "small" : "card" })}
      <div class="dynagraph-meta">
        <span>状态：<b>${sample.status}</b></span>
        <span>产液量：<b>${sample.production} m³/d</b></span>
        <span>载荷：<b>${sample.minLoad}-${sample.maxLoad} kN</b></span>
      </div>
      <p>${sample.advice}</p>
    </article>
  `;
}

function renderDynagraphSummary(sample) {
  return `
    <section class="metric-grid">
      <article class="metric-card"><span>诊断工况</span><strong>${sample.status}</strong><small>${sample.title}</small></article>
      <article class="metric-card"><span>估算产液量</span><strong>${sample.production}<small>m³/d</small></strong><small>功图面积 + 冲次 + 泵径</small></article>
      <article class="metric-card"><span>泵效估算</span><strong>${sample.efficiency}<small>%</small></strong><small>模型置信度 ${sample.confidence}%</small></article>
      <article class="metric-card"><span>报警状态</span><strong>${sample.alarm}</strong><small>自动生成处置建议</small></article>
    </section>
  `;
}

function renderDynagraphTable() {
  return `
    <table class="data-table">
      <thead>
        <tr><th>序号</th><th>井号</th><th>功图类型</th><th>生产状态</th><th>估算产液量</th><th>泵效</th><th>报警</th><th>处置建议</th></tr>
      </thead>
      <tbody>
        ${dynagraphCases
          .map(
            (sample, index) => `
              <tr>
                <td>${index + 1}</td>
                <td>${wellNames[index % wellNames.length]}</td>
                <td>${sample.title}</td>
                <td>${sample.status}</td>
                <td>${sample.production} m³/d</td>
                <td>${sample.efficiency}%</td>
                <td><span class="status ${sample.level}">${sample.alarm}</span></td>
                <td>${sample.advice}</td>
              </tr>
            `,
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderOilRealtime(item) {
  const current = dynagraphMap.get("insufficient");
  return `
    ${renderFilters(item)}
    ${renderDynagraphSummary(current)}
    <section class="dashboard-grid">
      <article class="panel">
        <div class="panel-title"><span>实时示功图</span><small>最新上传时间：2026-06-22 10:10:00</small></div>
        <div class="panel-body">${renderDynagraphCard(current)}</div>
      </article>
      <article class="panel wide">
        <div class="panel-title"><span>实时数据与状态判断</span><small>根据载荷、位移、功图面积和冲次综合判断</small></div>
        <div class="panel-body">${renderTable(item, 8)}</div>
      </article>
      <article class="panel wide">
        <div class="panel-title"><span>功图报警联动</span><small>报警自动进入处置流程</small></div>
        <div class="panel-body">${renderDynagraphTable()}</div>
      </article>
    </section>
  `;
}

function renderDynagraphData(item) {
  return `
    ${renderFilters(item)}
    <section class="panel">
      <div class="panel-title"><span>典型油井工况示功图</span><small>由系统根据工况样本生成，用于诊断、对比和报警规则验证</small></div>
      <div class="panel-body">
        <div class="dynagraph-grid">
          ${dynagraphCases.map((sample) => renderDynagraphCard(sample, true)).join("")}
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-title"><span>功图数据列表</span><small>全天、即时、定时示功图统一查询</small></div>
      <div class="panel-body">${renderDynagraphTable()}</div>
    </section>
  `;
}

function renderDynagraphTrack(item) {
  const track = ["normal", "intermittent", "insufficient", "pump-leak"].map((id) => dynagraphMap.get(id));
  return `
    ${renderFilters(item)}
    <section class="panel">
      <div class="panel-title"><span>最近功图跟踪</span><small>最近三小时与标准功图叠加对比，识别变化趋势</small></div>
      <div class="panel-body">
        <div class="dynagraph-track">
          <div class="track-main">
            ${track.map((sample, index) => `<div class="track-frame">${renderDynagraphSvg(sample, { secondary: "normal", suffix: `track${index}` })}<strong>${index === 0 ? "标准功图" : `${index}小时前`}</strong><span>${sample.title}</span></div>`).join("")}
          </div>
          <div class="track-side">
            <h3>趋势判断</h3>
            <p>功图有效面积连续缩小，最小载荷下降，系统判断供液能力下降并出现泵漏风险。</p>
            <ul>
              <li>建议降低冲次，观察动液面恢复。</li>
              <li>若泵效继续下降，派发检泵工单。</li>
              <li>报警进入“待确认”，超时自动督办。</li>
            </ul>
          </div>
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-title"><span>跟踪明细</span></div>
      <div class="panel-body">${renderDynagraphTable()}</div>
    </section>
  `;
}

function renderDynagraphDiagnosis(item) {
  return `
    ${renderFilters(item)}
    <section class="dashboard-grid">
      <article class="panel wide">
        <div class="panel-title"><span>诊断样本库</span><small>典型工况功图自动匹配</small></div>
        <div class="panel-body"><div class="dynagraph-grid">${dynagraphCases.map((sample) => renderDynagraphCard(sample, true)).join("")}</div></div>
      </article>
      <article class="panel">
        <div class="panel-title"><span>诊断规则</span></div>
        <div class="panel-body">
          <div class="rule-list">
            <p><b>载荷突降：</b>抽油杆断脱、泵脱、严重漏失。</p>
            <p><b>面积缩小：</b>供液不足、间歇供液、空抽风险。</p>
            <p><b>上行末端尖峰：</b>悬绳器不正或机械挤压。</p>
            <p><b>下死点冲击：</b>碰泵或防冲距异常。</p>
          </div>
        </div>
      </article>
      <article class="panel wide">
        <div class="panel-title"><span>诊断结果</span></div>
        <div class="panel-body">${renderDynagraphTable()}</div>
      </article>
    </section>
  `;
}

function renderProductionMetering(item) {
  return `
    ${renderFilters(item)}
    <section class="panel">
      <div class="panel-title"><span>产液量计量模型</span><small>由示功图有效面积、冲程、冲次、泵径、含水率和校准系数综合计算</small></div>
      <div class="panel-body">
        <div class="formula-grid">
          <div class="formula-card"><strong>输入</strong><span>示功图面积 / 最大载荷 / 最小载荷 / 冲次 / 泵径</span></div>
          <div class="formula-card"><strong>计算</strong><span>有效冲程识别 + 泵效估算 + 产液量修正</span></div>
          <div class="formula-card"><strong>输出</strong><span>小时产液、日均产液、泵效、异常产量报警</span></div>
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-title"><span>计量结果</span></div>
      <div class="panel-body">${renderDynagraphTable()}</div>
    </section>
  `;
}

function renderDynagraphConfig(item) {
  return `
    <section class="two-col">
      <div class="panel">
        <div class="panel-title"><span>标准功图库</span></div>
        <div class="panel-body"><div class="dynagraph-grid single">${renderDynagraphCard(dynagraphMap.get("normal"), true)}${renderDynagraphCard(dynagraphMap.get("insufficient"), true)}</div></div>
      </div>
      <div class="panel">
        <div class="panel-title"><span>${item.title}</span><small>${item.desc}</small></div>
        <div class="panel-body">
          <div class="form-grid">
            <div class="field"><label>井号</label><select><option>HN-YJ-001</option><option>HN-YJ-006</option></select></div>
            <div class="field"><label>最大载荷上限(kN)</label><input value="120" /></div>
            <div class="field"><label>最小载荷下限(kN)</label><input value="18" /></div>
            <div class="field"><label>功图面积波动阈值(%)</label><input value="15" /></div>
            <div class="field"><label>产液量校准系数</label><input value="1.00" /></div>
            <div class="field"><label>报警级别</label><select><option>预警</option><option>报警</option><option>严重报警</option></select></div>
            <div class="field full"><label>特殊井报警范围说明</label><textarea>特殊井载荷范围扩大时必须填写说明，并保留审批记录。</textarea></div>
          </div>
          <div class="head-actions" style="margin-top:16px"><button class="btn primary">选择为标准功图</button><button class="btn success">保存规则</button><button class="btn warn">生成报警测试</button></div>
        </div>
      </div>
    </section>
    <section class="panel">
      <div class="panel-title"><span>报警规则明细</span></div>
      <div class="panel-body">${renderDynagraphTable()}</div>
    </section>
  `;
}

function openPage(pageId) {
  const item = pageMap.get(pageId);
  if (!item) return;
  activePageId = pageId;
  activeModuleId = item.moduleId;
  if (!openedTabs.includes(pageId)) openedTabs.push(pageId);
  render();
  $("#workspace").focus();
}

function closeTab(pageId) {
  openedTabs = openedTabs.filter((id) => id !== pageId);
  if (activePageId === pageId) activePageId = openedTabs[openedTabs.length - 1] || "home-overview";
  activeModuleId = pageMap.get(activePageId).moduleId;
  render();
}

function render() {
  renderTopNav();
  renderSidebar();
  renderTabs();
  renderWorkspace();
}

document.addEventListener("click", (event) => {
  const moduleButton = event.target.closest("[data-module]");
  if (moduleButton) {
    const mod = modules.find((item) => item.id === moduleButton.dataset.module);
    activeModuleId = mod.id;
    openPage(mod.groups[0].pages[0].id);
    return;
  }

  const pageButton = event.target.closest("[data-page]");
  if (pageButton) {
    openPage(pageButton.dataset.page);
    return;
  }

  const closeButton = event.target.closest("[data-close]");
  if (closeButton) {
    event.stopPropagation();
    closeTab(closeButton.dataset.close);
    return;
  }

  const tabButton = event.target.closest("[data-tab]");
  if (tabButton) {
    openPage(tabButton.dataset.tab);
    return;
  }

  if (event.target.closest("#collapseButton")) {
    $(".app-shell").classList.toggle("collapsed");
  }
});

render();

console.info(`功能页面数量：${allPages.length}`);
