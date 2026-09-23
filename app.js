import { api, ApiError } from "./frontend/api.js";
import { components, escapeHtml } from "./frontend/components.js";
import * as format from "./frontend/format.js";
import { router } from "./frontend/router.js";
import { store } from "./frontend/store.js";
import { renderLogin } from "./frontend/pages/login.js";
import { renderDashboard as renderDashboardPage } from "./frontend/pages/dashboard.js";
import { renderWellList } from "./frontend/pages/wells.js";
import { renderWellDetail } from "./frontend/pages/well-detail.js";
import { renderDeviceList } from "./frontend/pages/devices.js";
import { renderDeviceCommand } from "./frontend/pages/device-command.js";
import { renderReports } from "./frontend/pages/reports.js";
import { renderUsers } from "./frontend/pages/users.js";
import { renderRoles } from "./frontend/pages/roles.js";
import { renderAudit } from "./frontend/pages/audit.js";
import { renderSystemHealth } from "./frontend/pages/system-health.js";
import { renderAcceptanceCenter } from "./frontend/pages/acceptance-center.js";
import { renderEnergyOverview } from "./frontend/pages/energy-overview.js";
import { renderRecoveryAnalysis } from "./frontend/pages/recovery-analysis.js";
import { renderStrategyEditor } from "./frontend/pages/strategy-editor.js";
import { renderStrategyCenter } from "./frontend/pages/strategy-center.js";
import { renderApprovals } from "./frontend/pages/approvals.js";
import { renderCommands } from "./frontend/pages/commands.js";
import { renderAlarms } from "./frontend/pages/alarms.js";
import { renderWorkOrders } from "./frontend/pages/work-orders.js";
import { renderAcquisitionStream } from "./frontend/pages/acquisition.js";
import { renderFieldRegistry } from "./frontend/pages/field-registry.js";
import { renderQualityBoard } from "./frontend/pages/data-quality.js";
import { renderDynagraphList } from "./frontend/pages/dynagraphs.js";
import { renderDynagraphAnalysis } from "./frontend/pages/dynagraph-analysis.js";
import { bindChartInteractions } from "./frontend/charts.js";
import { renderDiagnosisReview } from "./frontend/pages/diagnosis-review.js";

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
      { id: "field-registry", label: "生产设备台账", description: "独立生产目录、点表版本、时效绑定与来源核查", permission: "registry.read" },
      { id: "devices", label: "测控终端", description: "设备心跳、通道、寄存器与运行状态" },
      { id: "device-command", label: "指令详情", description: "签名载荷、执行状态与分阶段回执", hidden: true },
    ],
  },
  {
    id: "dynagraph", label: "功图智能", icon: "图",
    pages: [
      { id: "dynagraphs", label: "功图档案", description: "采集来源、分析状态、诊断工况与功图下钻" },
      { id: "dynagraph-analysis", label: "智能分析", description: "电功图到人工复核的转换、诊断与产液闭环" },
      { id: "diagnosis-review", label: "诊断复核", description: "机器结论、候选工况与人工复核" },
    ],
  },
  {
    id: "energy", label: "节能控制", icon: "能",
    pages: [
      { id: "energy-overview", label: "节能总览", description: "低产井识别、节能量与产量保持" },
      { id: "recovery-analysis", label: "渗流分析", description: "关井恢复曲线拟合与候选制度" },
      { id: "strategy-center", label: "策略中心", description: "策略版本、审批、执行与效果" },
      { id: "strategy-editor", label: "策略编辑", description: "约束优化、候选周期和版本提交", hidden: true },
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
    </div><nav>${context.module.pages.filter((page) => !page.hidden && (!page.permission || state.user?.permissions?.includes(page.permission)) && (page.id !== "field-registry" || state.user?.capabilities?.registry?.enabled === true)).map((page) => `
      <button type="button" data-page="${page.id}" class="${page.id === state.activePage ? "active" : ""}"
        aria-label="${escapeHtml(page.label)}" title="${escapeHtml(page.label)}"
        aria-current="${page.id === state.activePage ? "page" : "false"}">
        <span aria-hidden="true">${escapeHtml(page.label.slice(0, 1))}</span><div><strong>${escapeHtml(page.label)}</strong><small>${escapeHtml(page.description)}</small></div>
      </button>`).join("")}</nav>`;
  document.querySelector("#tabs").innerHTML = state.tabs.map((pageId) => {
    const page = PAGE_INDEX.get(pageId);
    if (!page) return "";
    return `<div class="tab ${pageId === state.activePage ? "active" : ""}">
      <button type="button" data-tab-page="${pageId}" aria-current="${pageId === state.activePage ? "page" : "false"}">${escapeHtml(page.label)}</button>
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

async function renderModuleLanding(page) {
  const workspace = document.querySelector("#workspace");
  workspace.innerHTML = `${pageHeader(page)}
    <section class="panel module-landing"><div class="landing-title"><span>${page.module.icon}</span>
      <div><p class="eyebrow">${escapeHtml(page.module.label.toUpperCase())}</p><h2>${escapeHtml(page.label)}</h2><p>${escapeHtml(page.description)}</p></div></div>
      <div class="landing-links">${page.module.pages.filter(item => item.id !== "field-registry" || (store.getState().user?.capabilities?.registry?.enabled === true && store.getState().user?.permissions?.includes("registry.read"))).map((item) => `<button type="button" data-page="${item.id}" class="${item.id === page.id ? "active" : ""}">
        <strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.description)}</span><b>进入 →</b></button>`).join("")}</div>
      <div class="system-ready"><i></i><span><strong>服务接口已就绪</strong><small>页面读取当前登录账号的权限与本机业务数据。</small></span></div>
    </section>`;
}

async function renderPage(pageId, params = {}) {
  const generation = ++renderGeneration;
  components.closeAllModals();
  const page = PAGE_INDEX.get(pageId) || PAGE_INDEX.get("dashboard");
  const state = store.getState();
  const tabs = state.tabs.includes(page.id) ? state.tabs : [...state.tabs, page.id];
  const tabParams = { ...(state.tabParams || {}), [page.id]: { ...params } };
  store.setState({ activeModule: page.module.id, activePage: page.id, tabs, tabParams });
  renderChrome();
  const workspace = document.querySelector("#workspace");
  workspace.setAttribute("aria-busy", "true");
  workspace.innerHTML = `${pageHeader(page)}${components.skeleton(6)}`;
  try {
    const context = {
      api, components, format, escapeHtml, workspace, page, pageHeader, params,
      permissions: new Set(state.user?.permissions || []),
      capabilities: state.user?.capabilities || {registry: {enabled: false, status: "disabled"}},
      navigate: openPage,
      isCurrent: () => generation === renderGeneration,
    };
    const renderer = ({
      dashboard: renderDashboardPage,
      wells: renderWellList,
      "well-detail": renderWellDetail,
      devices: renderDeviceList,
      "device-command": renderDeviceCommand,
      reports: renderReports,
      users: renderUsers,
      roles: renderRoles,
      audit: renderAudit,
      "system-health": renderSystemHealth,
      "acceptance-center": renderAcceptanceCenter,
      "energy-overview": renderEnergyOverview,
      "recovery-analysis": renderRecoveryAnalysis,
      "strategy-editor": renderStrategyEditor,
      "strategy-center": renderStrategyCenter,
      approvals: renderApprovals,
      commands: renderCommands,
      alarms: renderAlarms,
      "work-orders": renderWorkOrders,
      acquisition: renderAcquisitionStream,
      "field-registry": renderFieldRegistry,
      "data-quality": renderQualityBoard,
      dynagraphs: renderDynagraphList,
      "dynagraph-analysis": renderDynagraphAnalysis,
      "diagnosis-review": renderDiagnosisReview,
    })[page.id];
    if (renderer) await renderer(context);
    else await renderModuleLanding(page, params);
    if (generation !== renderGeneration) return;
    bindChartInteractions(workspace);
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

async function openPage(pageId, params = {}) {
  const destination = PAGE_INDEX.has(pageId) ? pageId : "dashboard";
  try {
    await router.open(destination, params);
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
    const tabButton = event.target.closest("[data-tab-page]");
    if (tabButton) {
      event.preventDefault();
      const state = store.getState();
      await openPage(tabButton.dataset.tabPage, state.tabParams?.[tabButton.dataset.tabPage] || {});
      return;
    }
    const pageButton = event.target.closest("[data-page]");
    if (pageButton) {
      event.preventDefault();
      const params = {};
      if (pageButton.dataset.id) params.id = pageButton.dataset.id;
      for (const attribute of pageButton.attributes) {
        if (!attribute.name.startsWith("data-param-")) continue;
        params[attribute.name.slice("data-param-".length).replaceAll("-", "_")] = attribute.value;
      }
      await openPage(pageButton.dataset.page, params);
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
      const tabParams = { ...(state.tabParams || {}) };
      delete tabParams[closing];
      store.setState({ tabs, tabParams });
      if (state.activePage === closing) {
        const target = tabs.at(-1) || "dashboard";
        await openPage(target, tabParams[target] || {});
      }
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
  components.closeAllModals();
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
