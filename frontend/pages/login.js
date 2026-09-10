import { ApiError } from "../api.js";

export async function renderLogin(container, { api, onSuccess }) {
  document.title = "登录｜合能油井数字智能化管理平台";
  container.innerHTML = `<main id="workspace" class="login-page" tabindex="-1">
    <section class="login-brand" aria-label="平台简介">
      <img src="/assets/heneng-logo.png" alt="合能科技" />
      <p class="eyebrow">OIL WELL DIGITAL INTELLIGENCE</p>
      <h1>油井数字智能化管理平台</h1>
      <p>覆盖采集传输、功图分析、产液计量、低产井节能控制与处置闭环。</p>
      <ul class="login-capabilities">
        <li><strong>全链路</strong><span>采集、分析、控制、处置</span></li>
        <li><strong>可追溯</strong><span>原始记录、版本、审计关联</span></li>
        <li><strong>本地化</strong><span>离线运行与本机数据持久化</span></li>
      </ul>
    </section>
    <section class="login-panel" aria-labelledby="loginTitle">
      <div class="login-card">
        <p class="eyebrow">LOCAL OPERATIONS</p>
        <h2 id="loginTitle">登录管理平台</h2>
        <p class="muted">账号权限决定可查看范围和可执行操作。</p>
        <form id="loginForm" novalidate>
          <div class="form-field"><label for="username">用户名</label>
            <input id="username" name="username" autocomplete="username" required /></div>
          <div class="form-field"><label for="password">密码</label>
            <input id="password" name="password" type="password" autocomplete="current-password" required /></div>
          <div id="loginError" class="inline-error" role="alert" hidden></div>
          <button class="button primary login-submit" type="submit">登录</button>
        </form>
        <div class="local-note"><span></span><p><strong>本机离线服务</strong><br />业务数据和操作日志保存在当前验收电脑。</p></div>
      </div>
    </section>
  </main>`;
  const form = container.querySelector("#loginForm");
  const errorBox = container.querySelector("#loginError");
  const submit = form.querySelector("button[type=submit]");
  container.querySelector("#username").focus();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    errorBox.hidden = true;
    submit.disabled = true;
    submit.textContent = "正在验证…";
    try {
      const fields = new FormData(form);
      await api.post("/api/auth/login", {
        username: String(fields.get("username") || "").trim(),
        password: String(fields.get("password") || ""),
      });
      const user = await api.get("/api/auth/me");
      await onSuccess(user);
    } catch (error) {
      errorBox.hidden = false;
      errorBox.textContent = error instanceof ApiError && error.status === 401
        ? "用户名或密码不正确"
        : error.message || "登录失败，请稍后重试";
    } finally {
      submit.disabled = false;
      submit.textContent = "登录";
    }
  });
}
