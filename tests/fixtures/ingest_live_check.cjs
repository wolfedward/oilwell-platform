// Real platform UI and HTTP/PG stack; no route interception or fake API.
const {chromium} = require('playwright');
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const [base, evidence] = process.argv.slice(2);
  assert.match(base, /^http:\/\/127\.0\.0\.1:\d+$/);
  fs.mkdirSync(evidence, {recursive: true});
  const executablePath = process.env.OILWELL_TEST_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}, locale: 'zh-CN'});
    page.setDefaultTimeout(10000);
    const errors = [], checks = [], screenshots = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/#/ingest-reliability');
    await page.locator('#username').fill('administrator');
    await page.locator('#password').fill('Admin@2026');
    await page.locator('#loginForm button[type=submit]').click();
    const detail = page.locator('[data-ingest-detail]');
    await page.locator('[data-ingest-select="cases"]').first().click();
    await detail.getByText('BROWSER', {exact: true}).waitFor();
    checks.push('live authenticated case query');
    await page.locator('[data-ingest-raw="preview"]').click();
    const raw = page.locator('[data-ingest-raw-text]');
    await raw.waitFor();
    assert.equal(JSON.parse(await raw.textContent()).message_id, 'BROWSER');
    checks.push('audited exact-text preview');
    const capture = async (label, locator = detail.locator('header')) => {
      const filename = path.join(evidence, label + '.png');
      // The workspace is a nested scrollport. Capturing a taller-than-viewport
      // element includes unpainted/clipped areas; capture the actual viewport.
      await locator.evaluate(element => element.scrollIntoView({block: 'start', behavior: 'instant'}));
      await page.screenshot({path: filename, animations: 'disabled'});
      screenshots.push(filename);
    };
    await capture('ingest-live-detail-1440');
    await capture('ingest-live-raw-1440', page.locator('.ingest-raw'));
    async function operation(action, reason) {
      await page.locator(`[data-ingest-action="${action}"]`).click();
      const form = page.locator('[data-ingest-action-form]');
      await form.locator('[name=reason]').fill(reason);
      await form.locator('button[type=submit]').click();
      await page.locator('[data-ingest-actions] .ingest-notice').filter({hasText: '操作已提交'}).waitFor();
    }
    await operation('close', '软件闭环测试：核对格式错误，保留原始记录');
    await detail.getByText('人工已关闭', {exact: true}).waitFor();
    checks.push('versioned close through real HTTP');
    await page.locator('[data-ingest-audit]').click();
    await page.locator('.ingest-audit-event').filter({hasText: 'raw.preview'}).waitFor();
    await page.locator('.ingest-audit-event').filter({hasText: 'case.close'}).waitFor();
    checks.push('persisted access and operation audit');
    await capture('ingest-live-audit-1440', page.locator('.ingest-audit'));
    await page.setViewportSize({width: 1024, height: 900});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal page overflow');
    await capture('ingest-live-detail-1024');
    checks.push('1024px detail without page overflow');
    await page.locator('[data-ingest-select="receipts"]').first().click();
    await operation('resend', '软件闭环测试：请求补发既有回执');
    await capture('ingest-live-receipt-1024');
    checks.push('audited resend of immutable receipt');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ok: true, checks, screenshots, page_errors: errors}));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error.stack); process.exitCode = 1;});
