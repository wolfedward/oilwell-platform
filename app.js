import { api, ApiError } from "./frontend/api.js";
import { components, escapeHtml } from "./frontend/components.js";
import * as format from "./frontend/format.js";
import { router } from "./frontend/router.js";
import { store } from "./frontend/store.js";
import { renderLogin } from "./frontend/pages/login.js";

const NAVIGATION = [
  {
    id: "overview", label: "运行总览", icon: "总",
    pages: [{ id: "dashboard", label: "综合看板", description: "全场生产、设备、报警与数据时点概览" }],
  },
  {
    id: "wells", label: "油井管理", icon: "井",
    pages: [
      { id: "wells", label: "油井台账", description: "按单位、井组和运行状态检索油井" },
      { id: "well-detail", label: "单井工作台", description: "单井生产、设备、功图和处置链路" },
    ],
  },
  {
    id: "acquisition", label: "采集监控", icon: "采",
    pages: [
      { id: "acquisition", label: "采集实况", description: "终端报文、序号、校验与接收状态" },
      { id: "data-quality", label: "数据质量", description: "缺测、延迟、漂移与补传处置" },
      { id: "devices", label: "测控终端", description: "设备心跳、通道、寄存器与运行状态" },
    ],
  },
  {
    id: "dynagraph", label: "功图智能", icon: "图",
    pages: [
      { id: "dynagraphs", label: "功图档案", description: "电功图、示功图及来源追溯" },
      { id: "dynagraph-analysis", label: "智能分析", description: "转换过程、特征参数和诊断依据" },
      { id: "diagnosis-review", label: "诊断复核", description: "机器结论、候选工况与人工复核" },
    ],
  },
  {
    id: "energy", label: "节能控制", icon: "能",
    pages: [
      { id: "energy-overview", label: "节能总览", description: "低产井识别、节能量与产量保持" },
      { id: "recovery-analysis", label: "渗流分析", description: "关井恢复曲线拟合与候选制度" },
      { id: "strategy-center", label: "策略中心", description: "策略版本、审批、执行与效果" },
    ],
  },
  {
    id: "workflow", label: "协同处置", icon: "处",
    pages: [
      { id: "approvals", label: "审批中心", description: "待审批策略与独立复核意见" },
      { id: "commands", label: "指令中心", description: "签名下发、分阶段回执与失败重试" },
      { id: "alarms", label: "报警中心", description: "来源告警、确认与关联处置" },
      { id: "work-orders", label: "工单闭环", description: "派发、处理、验证和独立关闭" },
    ],
  },
  {
    id: "reports", label: "报表中心", icon: "表",
    pages: [{ id: "reports", label: "业务报表", description: "生产、诊断、质量、策略、节能与工单报表" }],
  },
  {
    id: "administration", label: "系统管理", icon: "管",
    pages: [
      { id: "users", label: "用户管理", description: "账号、岗位、状态与数据范围" },
      { id: "roles", label: "角色权限", description: "角色能力与操作权限配置" },
      { id: "audit", label: "审计日志", description: "登录、修改、审批、下发与导出记录" },
      { id: "system-health", label: "系统运行", description: "服务、数据库、队列与备份状态" },
    ],
  },
  {
    id: "acceptance", label: "验收中心", icon: "验",
    pages: [{ id: "acceptance-center", label: "成果证据", description: "年度任务、业务记录与可追溯证据映射" }],
  },
];

const PAGE_INDEX = new Map(
  NAVIGATION.flatMap((module) => module.pages.map((page) => [page.id, { ...page, module }])),
);
const app = document.querySelector("#app");
let shellReady = false;
let loginGeneration = 0;
let shellEventsBound = false;
let renderGeneration = 0;

function roleLabel(code) {
  return ({
    administrator: "系统管理员",
    production_engineer: "生产工程师",
    algorithm_engineer: "算法工程师",
    maintenance_engineer: "运维工程师",
    reviewer: "业务复核员",
  })[code] || code || "平台用户";
}

function shellMarkup(user) {
  return `<div class="app-shell">
    <header class="topbar">
      <a class="brand" href="#/dashboard" data-page="dashboard" aria-label="返回综合看板">
        <img src="/assets/heneng-logo.png" alt="合能科技" />
        <span><strong>油井数字智能化管理平台</strong><small>OIL WELL DIGITAL INTELLIGENCE</small></span>
      </a>
      <button id="collapseButton" class="icon-button" type="button" aria-label="折叠二级导航" aria-expanded="true">
        <span></span><span></span><span></span>
      </button>
      <nav id="topNav" class="topnav" aria-label="一级功能导航"></nav>
      <div class="top-actions">
        <span class="service-state"><i></i>本机服务正常</span>
        <div class="user-summary">
          <span class="avatar">${escapeHtml((user.display_name || user.username).slice(0, 1))}</span>
          <span><strong>${escapeHtml(user.display_name || user.username)}</strong><small>${escapeHtml(roleLabel(user.role_code))}</small></span>
        </div>
        <button id="logoutButton" class="text-button" type="button">退出</button>
      </div>
    </header>
    <div class="tabbar" aria-label="已打开工作页"><div id="tabs" class="tabs"></div></div>
    <div class="main-layout">
      <aside id="sidebar" class="sidebar" aria-label="二级功能导航"></aside>
      <main id="workspace" class="workspace" tabindex="-1" aria-live="polite"></main>
    </div>
  </div>`;
}

function activeContext() {
  const state = store.getState();
  return PAGE_INDEX.get(state.activePage) || PAGE_INDEX.get("dashboard");
}

function renderChrome() {
  if (!shellReady) return;
  const state = store.getState();
  const context = activeContext();
  document.querySelector(".app-shell")?.classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
  const collapse = document.querySelector("#collapseButton");
  if (collapse) collapse.setAttribute("aria-expanded", String(!state.sidebarCollapsed));
  document.querySelector("#topNav").innerHTML = NAVIGATION.map((module) => `
    <button type="button" data-module="${module.id}" class="${module.id === context.module.id ? "active" : ""}"
      aria-current="${module.id === context.module.id ? "page" : "false"}">
      <span>${module.icon}</span>${escapeHtml(module.label)}
    </button>`).join("");
  document.querySelector("#sidebar").innerHTML = `<div class="sidebar-head">
      <span>${context.module.icon}</span><div><strong>${escapeHtml(context.module.label)}</strong><small>业务工作区</small></div>
    </div><nav>${context.module.pages.map((page) => `
      <button type="button" data-page="${page.id}" class="${page.id === state.activePage ? "active" : ""}"
        aria-label="${escapeHtml(page.label)}" title="${escapeHtml(page.label)}"
        aria-current="${page.id === state.activePage ? "page" : "false"}">
        <span aria-hidden="true">${escapeHtml(page.label.slice(0, 1))}</span><div><strong>${escapeHtml(page.label)}</strong><small>${escapeHtml(page.description)}</small></div>
      </button>`).join("")}</nav>`;
  document.querySelector("#tabs").innerHTML = state.tabs.map((pageId) => {
    const page = PAGE_INDEX.get(pageId);
    if (!page) return "";
    return `<div class="tab ${pageId === state.activePage ? "active" : ""}">
      <button type="button" data-page="${pageId}" aria-current="${pageId === state.activePage ? "page" : "false"}">${escapeHtml(page.label)}</button>
      ${pageId === "dashboard" ? "" : `<button type="button" class="tab-close" data-close-tab="${pageId}" aria-label="关闭${escapeHtml(page.label)}">×</button>`}
    </div>`;
  }).join("");
}

function pageHeader(page, suffix = "") {
  return `<header class="page-header"><div>
    <nav class="breadcrumb" aria-label="面包屑"><span>${escapeHtml(page.module.label)}</span><b>/</b><span>${escapeHtml(page.label)}</span></nav>
    <h1 tabindex="-1">${escapeHtml(page.label)}</h1><p>${escapeHtml(page.description)}</p>
  </div>${suffix}</header>`;
}

async function renderDashboard(page, generation) {
  const workspace = document.querySelector("#workspace");
  const data = await api.get("/api/dashboard");
  if (generation !== renderGeneration) return false;
  const cards = [
    ["油井总数", format.number(data.total_wells), "口", "primary"],
    ["当前开井", format.number(data.active_wells), "口", "success"],
    ["低产井", format.number(data.low_output_wells), "口", "warning"],
    ["在线终端", format.number(data.online_devices), "台", "cyan"],
    ["活动报警", format.number(data.active_alarms), "条", data.active_alarms ? "danger" : "success"],
    ["当日耗电", format.number(data.power_kwh, 1), "kWh", "purple"],
  ];
  workspace.innerHTML = `${pageHeader(page, `<span class="data-time">数据时点 ${escapeHtml(format.dateTime(data.data_timestamp))}</span>`)}
    <section class="metric-grid" aria-label="生产运行指标">${cards.map(([label, value, unit, tone]) => `
      <article class="metric-card ${tone}"><p>${label}</p><strong>${value}<small>${unit}</small></strong><span>来源：平台业务库</span></article>`).join("")}</section>
    <section class="dashboard-layout">
      <article class="panel production-panel"><header><div><p class="eyebrow">LATEST PRODUCTION DAY</p><h2>最新生产日汇总</h2></div><span>${escapeHtml(data.data_day || "—")}</span></header>
        <div class="production-values">
          <div><span>日产液</span><strong>${format.number(data.daily_fluid, 1)}</strong><small>m³</small></div>
          <div><span>日产油</span><strong>${format.number(data.daily_oil, 1)}</strong><small>t</small></div>
          <div><span>平均泵效</span><strong>${format.percent(data.pump_efficiency, 1)}</strong><small>全井算术平均</small></div>
        </div>
        <p class="source-note">全部指标由服务端从最新生产日明细实时汇总，不在浏览器中独立生成。</p>
      </article>
      <article class="panel state-breakdown"><header><div><p class="eyebrow">OPERATING STATE</p><h2>运行状态</h2></div></header>
        <dl><div><dt>运行 / 间抽</dt><dd>${format.number(data.active_wells)}</dd></div>
          <div><dt>停机</dt><dd>${format.number(data.stopped_wells)}</dd></div>
          <div><dt>离线 / 维护</dt><dd>${format.number(data.offline_wells)}</dd></div></dl>
        <button class="button secondary" type="button" data-page="wells">进入油井台账</button>
      </article>
      <article class="panel quick-entry"><header><div><p class="eyebrow">QUICK ACCESS</p><h2>重点业务入口</h2></div></header>
        <div>${[
          ["dynagraph-analysis", "功图智能分析", "追溯电功图到诊断结论"],
          ["strategy-center", "节能策略中心", "查看审批、执行与效果"],
          ["work-orders", "工单闭环", "跟进处置和独立验证"],
          ["reports", "服务端报表", "按条件导出可对账明细"],
        ].map(([id, label, description]) => `<button type="button" data-page="${id}"><strong>${label}</strong><span>${description}</span><b>→</b></button>`).join("")}</div>
      </article>
    </section>`;
  return true;
}

async function renderModuleLanding(page) {
  const workspace = document.querySelector("#workspace");
  workspace.innerHTML = `${pageHeader(page)}
    <section class="panel module-landing"><div class="landing-title"><span>${page.module.icon}</span>
      <div><p class="eyebrow">${escapeHtml(page.module.label.toUpperCase())}</p><h2>${escapeHtml(page.label)}</h2><p>${escapeHtml(page.description)}</p></div></div>
      <div class="landing-links">${page.module.pages.map((item) => `<button type="button" data-page="${item.id}" class="${item.id === page.id ? "active" : ""}">
        <strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.description)}</span><b>进入 →</b></button>`).join("")}</div>
      <div class="system-ready"><i></i><span><strong>服务接口已就绪</strong><small>页面读取当前登录账号的权限与本机业务数据。</small></span></div>
    </section>`;
}

async function renderPage(pageId, params = {}) {
  const generation = ++renderGeneration;
  const page = PAGE_INDEX.get(pageId) || PAGE_INDEX.get("dashboard");
  const state = store.getState();
  const tabs = state.tabs.includes(page.id) ? state.tabs : [...state.tabs, page.id];
  store.setState({ activeModule: page.module.id, activePage: page.id, tabs });
  renderChrome();
  const workspace = document.querySelector("#workspace");
  workspace.setAttribute("aria-busy", "true");
  workspace.innerHTML = `${pageHeader(page)}${components.skeleton(6)}`;
  try {
    if (page.id === "dashboard") await renderDashboard(page, generation);
    else await renderModuleLanding(page, params);
    if (generation !== renderGeneration) return;
    document.title = `${page.label}｜合能油井数字智能化管理平台`;
  } catch (error) {
    if (generation !== renderGeneration) return;
    if (error instanceof ApiError && error.status === 401) return;
    workspace.innerHTML = `${pageHeader(page)}${components.errorPanel(error, { retry: true })}`;
  } finally {
    if (generation !== renderGeneration) return;
    workspace.setAttribute("aria-busy", "false");
    workspace.querySelector("h1")?.focus?.();
  }
}

function registerRoutes() {
  PAGE_INDEX.forEach((page) => router.register(page.id, (params) => renderPage(page.id, params)));
}

async function openPage(pageId) {
  const destination = PAGE_INDEX.has(pageId) ? pageId : "dashboard";
  try {
    await router.open(destination);
  } catch (error) {
    const workspace = document.querySelector("#workspace");
    if (workspace) workspace.innerHTML = components.errorPanel(error, { retry: true });
  }
}

function bindShellEvents() {
  if (shellEventsBound) return;
  shellEventsBound = true;
  app.addEventListener("click", async (event) => {
    if (event.target.closest("#logoutButton")) {
      try { await api.post("/api/auth/logout", {}); } catch (error) { /* session may already be invalid */ }
      await showLogin();
      return;
    }
    const pageButton = event.target.closest("[data-page]");
    if (pageButton) {
      event.preventDefault();
      await openPage(pageButton.dataset.page);
      return;
    }
    const moduleButton = event.target.closest("[data-module]");
    if (moduleButton) {
      const module = NAVIGATION.find((item) => item.id === moduleButton.dataset.module);
      if (module) await openPage(module.pages[0].id);
      return;
    }
    const closeButton = event.target.closest("[data-close-tab]");
    if (closeButton) {
      const closing = closeButton.dataset.closeTab;
      const state = store.getState();
      const tabs = state.tabs.filter((id) => id !== closing);
      store.setState({ tabs });
      if (state.activePage === closing) await openPage(tabs.at(-1) || "dashboard");
      else renderChrome();
      return;
    }
    if (event.target.closest("#collapseButton")) {
      store.setState((state) => ({ sidebarCollapsed: !state.sidebarCollapsed }));
      renderChrome();
      return;
    }
    if (event.target.closest("[data-action=retry]")) {
      await router.refresh();
    }
  });
}

async function showShell(user) {
  loginGeneration += 1;
  store.setState({ user });
  app.innerHTML = shellMarkup(user);
  app.setAttribute("aria-busy", "false");
  shellReady = true;
  bindShellEvents();
  renderChrome();
  await router.restore();
}

async function showLogin() {
  const generation = ++loginGeneration;
  shellReady = false;
  store.reset();
  app.setAttribute("aria-busy", "false");
  await renderLogin(app, {
    api,
    onSuccess: async (user) => {
      if (generation === loginGeneration) await showShell(user);
    },
  });
}

async function bootstrap() {
  registerRoutes();
  try {
    const user = await api.get("/api/auth/me");
    await showShell(user);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await showLogin();
      return;
    }
    app.setAttribute("aria-busy", "false");
    app.innerHTML = `<main id="workspace" class="boot-failure">${components.errorPanel(error, { retry: true })}</main>`;
    app.querySelector("[data-action=retry]")?.addEventListener("click", () => window.location.reload());
  }
}

document.querySelector(".skip-link")?.addEventListener("click", (event) => {
  event.preventDefault();
  document.querySelector("#workspace")?.focus();
});
function showGlobalFailure() {
  const error = new ApiError("页面运行时发生异常，请刷新后恢复工作区", { code: "client_error" });
  if (shellReady) {
    const workspace = document.querySelector("#workspace");
    if (workspace) workspace.innerHTML = components.errorPanel(error, { retry: true });
    return;
  }
  app.setAttribute("aria-busy", "false");
  app.innerHTML = `<main id="workspace" class="boot-failure">${components.errorPanel(error, { retry: true })}</main>`;
  app.querySelector("[data-action=retry]")?.addEventListener("click", () => window.location.reload());
}
window.addEventListener("error", () => showGlobalFailure());
window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault();
  showGlobalFailure();
});
window.addEventListener("platform:unauthenticated", () => {
  if (shellReady) showLogin();
});
bootstrap();
